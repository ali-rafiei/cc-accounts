#!/usr/bin/env python3
"""Load a profile's Claude login into the default slot: `cc-use work`, `cc-use default`. macOS only.

The default slot (the `Claude Code-credentials` Keychain item plus `oauthAccount` in
~/.claude.json) is what VS Code and every plain `claude` run as. A login is moved, never
copied: the one leaving the slot is written back to its owner first, because a session
using it may have refreshed (rotated) its token. The lock protocol and the Keychain I/O
follow claude-swap (MIT, github.com/realiti4/claude-swap).
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import random
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import urllib.request
from contextlib import contextmanager
from pathlib import Path

try:
    import pwd
except ImportError:  # Windows: cc-use refuses to run there anyway
    pwd = None  # type: ignore[assignment]

PROFILES_DIR = Path(os.environ.get('CLAUDE_PROFILES') or Path.home() / '.claude-profiles').expanduser()
LOADED_FILE = PROFILES_DIR / '.loaded'
HOME_ACCOUNT_FILE = PROFILES_DIR / '.home-account.json'
RESERVED_NAMES = {'bin', 'default'}
GLOBAL_CONFIG = Path.home() / '.claude.json'
# Claude Code's own lock directories: two guard a token refresh, one guards ~/.claude.json.
OAUTH_REFRESH_LOCK = Path.home() / '.claude' / '.oauth_refresh.lock'
LEGACY_CREDENTIALS_LOCK = Path.home() / '.claude.lock'
CONFIG_LOCK = Path.home() / '.claude.json.lock'

DEFAULT_SERVICE = 'Claude Code-credentials'
HOME_STASH_SERVICE = 'Claude Code-credentials-cc-use-home'
SECURITY = '/usr/bin/security'
# `security -i` reads stdin through a 4096-byte line buffer and truncates past it.
SECURITY_STDIN_LINE_LIMIT = 4096 - 64
KEYCHAIN_NOT_FOUND = 44
KEYCHAIN_TIMEOUT_S = 5.0

# Claude Code's proper-lockfile settings: credential locks go stale after 60s, the
# ~/.claude.json lock after 10s, and live holders touch theirs every 5s.
CREDENTIALS_LOCK_STALE_S = 60.0
CONFIG_LOCK_STALE_S = 10.0
LOCK_TOUCH_S = 3.0
LOCK_TIMEOUT_S = 9.0

PROFILE_URL = 'https://api.anthropic.com/api/oauth/profile'
USAGE = """usage: cc-use                 show which account is in the default slot
       cc-use <profile>       load that profile's login into the default slot
       cc-use default         put your own login back
       cc-use forget          delete cc-use's stashed copy of your login (used by uninstall)
exit status 3: it could not confirm whose login is in the default slot (offline, say)"""


class SwapError(Exception):
    """A swap refused or failed before it could leave the slot half-changed."""

    exit_code = 1


class UnconfirmedError(SwapError):
    """The slot's owner could not be checked (offline, or an expired token), as opposed to checked and wrong."""

    exit_code = 3


class _Terminated(BaseException):
    """SIGTERM or SIGHUP arrived mid-swap; raised so the rollback and the lock cleanup still run."""

    def __init__(self, signum: int):
        super().__init__(signum)
        self.signum = signum


class _RefuseRedirect(urllib.request.HTTPRedirectHandler):
    """urllib would re-send the Bearer token to wherever a redirect points; a 3xx becomes an HTTPError instead."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def main(argv: list[str]) -> int:
    if argv and argv[0] in ('-h', '--help'):
        print(USAGE)
        return 0
    if len(argv) > 1:
        print(USAGE, file=sys.stderr)
        return 2
    if sys.platform != 'darwin':
        print('cc-use: macOS only (it swaps the login in the macOS Keychain)', file=sys.stderr)
        return 1
    try:
        if not argv or argv[0] == 'status':
            print(status())
        elif argv[0] == 'forget':
            print(forget())
        else:
            print(load(None if argv[0] == 'default' else argv[0]))
    except SwapError as exc:
        print(f'cc-use: {exc}', file=sys.stderr)
        return exc.exit_code
    except _Terminated as exc:
        print('cc-use: interrupted; the swap was undone', file=sys.stderr)
        return 128 + exc.signum
    return 0


def status() -> str:
    email = (_read_json(GLOBAL_CONFIG).get('oauthAccount') or {}).get('emailAddress')
    return f'default slot: {_label(loaded_profile())} ({email})'


def load(target: str | None) -> str:
    owner = loaded_profile()
    stash = _keychain_get(HOME_STASH_SERVICE) if target is None and owner is None else None
    if stash is not None and (slot := _keychain_get(DEFAULT_SERVICE)) is not None:
        owner = _interrupted_swap_owner(slot, stash)
    if target == owner:
        return f'{_label(target)} is already in the default slot'
    if target is not None:
        _require_profile(target)
    current = _keychain_get(DEFAULT_SERVICE)
    if current is None:
        raise SwapError('the default slot holds no login; run `claude auth login` first')
    # A deleted profile leaves no account to check against; its login is kept in its own item below.
    if not (target is None and owner is not None and not (PROFILES_DIR / owner).is_dir()):
        _verify_owner(current, owner)

    with _signals_raise(), _credentials_lock(), _config_lock():
        if _keychain_get(DEFAULT_SERVICE) != current:
            raise SwapError('a session refreshed the default login mid-swap; retry')
        # Checked under the lock, so a `cc <profile>` started, or a refresh made, since cc-use began is seen.
        if target is not None:
            _refuse_if_running(target)
        incoming = _keychain_get(_owner_service(target))
        if incoming is None:
            raise SwapError(
                f'{target} is not logged in (cc-login {target})' if target else 'no stashed login to restore'
            )
        incoming_account = _profile_account(target) if target else _read_json(HOME_ACCOUNT_FILE)
        config = _read_json(GLOBAL_CONFIG)
        # The record goes first: uninstall runs `forget` only when it exists, so no stash may outlive it.
        if owner is None:
            home_account = config.get('oauthAccount')
            if not isinstance(home_account, dict) or not home_account:
                raise SwapError(f'{GLOBAL_CONFIG} records no oauthAccount for the default login; not swapping')
            _write_json(HOME_ACCOUNT_FILE, home_account)
        _keychain_set(_owner_service(owner), current)
        # From here on the slot, ~/.claude.json and .loaded must change together; a
        # half-done swap would make _verify_owner refuse every later run.
        config_written = False
        try:
            _keychain_set(DEFAULT_SERVICE, incoming)
            _write_json(GLOBAL_CONFIG, {**config, 'oauthAccount': incoming_account})
            config_written = True
            _write_loaded(target)
        except BaseException:
            _undo(_keychain_set, DEFAULT_SERVICE, current)
            if config_written:
                _undo(_write_json, GLOBAL_CONFIG, config)
                _undo(_write_loaded, owner)
            raise
        # Your login is back in the slot, so the stash is a stale copy from here on.
        note = ''
        if target is None:
            try:
                _keychain_delete(HOME_STASH_SERVICE)
                HOME_ACCOUNT_FILE.unlink(missing_ok=True)
            except (SwapError, OSError) as exc:
                note = f' The stale stash was not deleted ({exc}); `cc-use forget` deletes it.'

    return (
        f'default slot: {_label(owner)} -> {_label(target)} '
        f'({incoming_account.get("emailAddress")}). Running sessions and VS Code pick it up '
        f'within ~30s; reopen the Claude tab to switch at once.{note}'
    )


def forget() -> str:
    """Delete a leftover stash (a killed swap's, or one an older cc-use kept after a round trip)."""
    loaded = loaded_profile()
    if loaded is not None:
        raise SwapError(f'{loaded} is loaded, so the stash holds your only login; run `cc-use default` first')
    if _keychain_get(HOME_STASH_SERVICE) is None:
        HOME_ACCOUNT_FILE.unlink(missing_ok=True)
        return 'nothing stashed; nothing to delete'
    # A swap killed before it wrote .loaded leaves a profile in the slot and your login only in the stash.
    current = _keychain_get(DEFAULT_SERVICE)
    if current is None:
        raise SwapError('the default slot holds no login, so the stash is your only copy; not deleting it')
    _verify_owner(current, None, doing='deleting the stash')
    _keychain_delete(HOME_STASH_SERVICE)
    HOME_ACCOUNT_FILE.unlink(missing_ok=True)
    return 'deleted the stashed copy of your login; your default login is untouched'


def _interrupted_swap_owner(slot: str, stash: str) -> str | None:
    """The profile a swap killed before writing .loaded left in the slot; None when the slot holds your own login."""
    identity = _fetch_identity(_oauth(slot).get('accessToken'))
    if identity is None:
        if _oauth(stash).get('refreshToken') == _oauth(slot).get('refreshToken'):
            return None
        raise UnconfirmedError(
            'could not confirm whose login is in the default slot (offline, or its access token '
            'expired), and cc-use keeps a stashed login; retry online'
        )
    if identity['uuid'] == _home_account(stash).get('accountUuid'):
        return None
    holders = _profiles_holding(identity['uuid'])
    if len(holders) != 1:
        _verify_owner(slot, None, doing='restoring the stash')  # refuses, naming the way out
    return holders[0]


def _profiles_holding(account_uuid: str) -> list[str]:
    """The profiles whose recorded account is this one."""
    names = []
    for path in sorted(PROFILES_DIR.iterdir()) if PROFILES_DIR.is_dir() else []:
        if not path.is_dir() or path.name in RESERVED_NAMES or path.name[:1] in ('.', '_'):
            continue
        try:
            account = _read_json(path / '.claude.json').get('oauthAccount')
        except SwapError:
            continue
        if isinstance(account, dict) and account.get('accountUuid') == account_uuid:
            names.append(path.name)
    return names


def _undo(step, *args) -> None:
    """One rollback step; a failure is reported and the next step still runs."""
    try:
        step(*args)
    except Exception as exc:  # the caller re-raises the error that started the rollback
        print(f'cc-use: could not undo {step.__name__}({args[0]!r}): {exc}', file=sys.stderr)


def loaded_profile() -> str | None:
    try:
        return LOADED_FILE.read_text().rstrip('\n') or None  # only the newline: cc compares the name exactly
    except FileNotFoundError:
        return None


def _label(profile: str | None) -> str:
    return profile or 'default'


def _owner_service(profile: str | None) -> str:
    """Where a login lives while it is out of the slot: its profile's own item, or the stash."""
    if profile is None:
        return HOME_STASH_SERVICE
    config_dir = unicodedata.normalize('NFC', str(PROFILES_DIR / profile))  # Claude Code hashes the NFC form
    digest = hashlib.sha256(config_dir.encode()).hexdigest()[:8]
    return f'{DEFAULT_SERVICE}-{digest}'


def _require_profile(name: str) -> None:
    # A name with a slash ("work/") finds the same dir, but .loaded would then not match the name `cc` compares.
    malformed = not name or '/' in name or name[:1] in ('.', '_')
    # The folder lookup ignores case and Unicode form; the Keychain item and .loaded do not.
    exact = (PROFILES_DIR / name).is_dir() and name in os.listdir(PROFILES_DIR)
    if malformed or name in RESERVED_NAMES or not exact:
        raise SwapError(f'no such profile: {name}')


def _refuse_if_running(name: str) -> None:
    live = [pid for pid in _session_pids(PROFILES_DIR / name / 'sessions') if _alive(pid)]
    if live:
        raise SwapError(
            f'{name} has a running session (pid {", ".join(map(str, live))}); exit it '
            'first, since two live copies of one login strand one of them'
        )


def _session_pids(sessions_dir: Path) -> list[int]:
    return [int(f.stem) for f in sessions_dir.glob('*.json') if f.stem.isdigit()]


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _verify_owner(current: str, owner: str | None, doing: str = 'swapping') -> None:
    """Refuse when the slot's login is not the account cc-use last put there.

    Writing it back would otherwise hand one account's login to another's profile.
    """
    stored = _keychain_get(_owner_service(owner))
    expected = _profile_account(owner) if owner else _home_account(stored)
    identity = _fetch_identity(_oauth(current).get('accessToken'))
    if identity is not None:
        if identity['uuid'] != expected.get('accountUuid'):
            message = (
                f'the default slot holds {identity["email"]}, but cc-use recorded '
                f'{_label(owner)} ({expected.get("emailAddress")}); not {doing}'
            )
            if owner is None and stored is not None:
                holders = _profiles_holding(identity['uuid'])
                if len(holders) == 1:
                    # A swap to it was killed before .loaded; the stash is your login's only copy.
                    message += f'. A swap to {holders[0]} was cut short: run `cc-use default` to finish it'
                elif holders:
                    message += f'. Profiles {", ".join(holders)} all record that account; fix their logins first'
                else:
                    message += (
                        '. If you signed the default login in as a different account on purpose, drop the old '
                        f"stash and retry: security delete-generic-password -s '{HOME_STASH_SERVICE}'; "
                        f"rm '{HOME_ACCOUNT_FILE}'"
                    )
            raise SwapError(message)
        return
    if stored is None or _oauth(stored).get('refreshToken') == _oauth(current).get('refreshToken'):
        return
    raise UnconfirmedError(
        'could not confirm whose login is in the default slot (offline, or its '
        'access token expired); send one message in any default session, then retry'
    )


def _home_account(stash: str | None) -> dict:
    """The user's own account: the stash's record once there is a stash.

    ~/.claude.json is not proof, since a swap killed before .loaded may already have
    written the loaded profile's account there.
    """
    if stash is not None and HOME_ACCOUNT_FILE.exists():
        return _read_json(HOME_ACCOUNT_FILE)
    return _read_json(GLOBAL_CONFIG).get('oauthAccount') or {}


def _fetch_identity(access_token: str | None) -> dict | None:
    if not access_token:
        return None
    request = urllib.request.Request(
        PROFILE_URL, headers={'Authorization': f'Bearer {access_token}', 'Content-Type': 'application/json'}
    )
    try:
        with urllib.request.build_opener(_RefuseRedirect).open(request, timeout=5) as response:
            body = json.loads(response.read().decode())
    # OSError covers URLError, timeouts and a reset mid-read; ValueError covers bad JSON and bad UTF-8.
    except (OSError, ValueError, http.client.HTTPException):
        return None
    account = body.get('account') if isinstance(body, dict) else None
    if not isinstance(account, dict) or not account.get('uuid'):
        return None
    return {'uuid': account['uuid'], 'email': account.get('email')}


def _oauth(credentials: str) -> dict:
    try:
        return json.loads(credentials).get('claudeAiOauth') or {}
    except json.JSONDecodeError:
        return {}


def _profile_account(name: str) -> dict:
    account = _read_json(PROFILES_DIR / name / '.claude.json').get('oauthAccount')
    if not account:
        raise SwapError(f'{name} is not logged in (cc-login {name})')
    return account


def _write_loaded(profile: str | None) -> None:
    if profile is None:
        LOADED_FILE.unlink(missing_ok=True)
    else:
        LOADED_FILE.write_text(profile + '\n')


def _read_json(path: Path) -> dict:
    try:
        with path.open() as fh:
            data = json.load(fh)
    except FileNotFoundError as exc:
        raise SwapError(f'missing {path}') from exc
    except json.JSONDecodeError as exc:
        raise SwapError(f'{path} is not valid JSON ({exc}); not touching it') from exc
    if not isinstance(data, dict):
        raise SwapError(f'{path} holds {type(data).__name__}, not a JSON object; not touching it')
    return data


def _write_json(path: Path, data: dict) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f'.{path.name}.')
    try:
        with os.fdopen(fd, 'w') as fh:
            json.dump(data, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _keychain_account() -> str:
    """The account name Claude Code files its Keychain items under: $USER, else the login name.

    Claude Code substitutes a fixed name for one outside [a-zA-Z0-9._-].
    """
    name = os.environ.get('USER') or pwd.getpwuid(os.geteuid()).pw_name
    return name if re.fullmatch(r'[a-zA-Z0-9._-]+', name) else 'claude-code-user'


def _keychain_get(service: str) -> str | None:
    done = _security(['find-generic-password', '-a', _keychain_account(), '-w', '-s', service])
    if done.returncode == 0:
        return done.stdout.removesuffix('\n')
    if done.returncode == KEYCHAIN_NOT_FOUND:
        return None
    raise SwapError(f'Keychain read of {service!r} failed (rc={done.returncode}): {done.stderr.strip()}')


def _keychain_set(service: str, secret: str) -> None:
    hex_secret = secret.encode().hex()
    account = _keychain_account()
    command = f'add-generic-password -U -a "{account}" -s "{service}" -X {hex_secret}\n'
    if len(command.encode()) <= SECURITY_STDIN_LINE_LIMIT:
        done = _security(['-i'], stdin=command)
    else:
        done = _security(['add-generic-password', '-U', '-a', account, '-s', service, '-X', hex_secret])
    if done.returncode != 0:
        raise SwapError(f'Keychain write of {service!r} failed (rc={done.returncode}): {done.stderr.strip()}')


def _keychain_delete(service: str) -> None:
    done = _security(['delete-generic-password', '-a', _keychain_account(), '-s', service])
    if done.returncode not in (0, KEYCHAIN_NOT_FOUND):
        raise SwapError(f'Keychain delete of {service!r} failed (rc={done.returncode}): {done.stderr.strip()}')


def _security(args: list[str], stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            [SECURITY, *args], input=stdin, capture_output=True, text=True, timeout=KEYCHAIN_TIMEOUT_S
        )
    except subprocess.TimeoutExpired as exc:
        raise SwapError(f'Keychain did not answer within {KEYCHAIN_TIMEOUT_S}s (locked?)') from exc
    except OSError as exc:
        raise SwapError(f'could not run {SECURITY}: {exc}') from exc


@contextmanager
def _signals_raise():
    """Turn SIGTERM and SIGHUP into _Terminated, so a killed swap still rolls back and frees its locks."""

    def handler(signum, frame):
        raise _Terminated(signum)

    previous = {sig: signal.signal(sig, handler) for sig in (signal.SIGTERM, signal.SIGHUP)}
    try:
        yield
    finally:
        for sig, old in previous.items():
            signal.signal(sig, old)


@contextmanager
def _credentials_lock():
    """Claude Code's two token-refresh locks, taken in its own order so neither side deadlocks."""
    with (
        _lock_dir(OAUTH_REFRESH_LOCK, CREDENTIALS_LOCK_STALE_S),
        _lock_dir(LEGACY_CREDENTIALS_LOCK, CREDENTIALS_LOCK_STALE_S),
    ):
        yield


@contextmanager
def _config_lock():
    with _lock_dir(CONFIG_LOCK, CONFIG_LOCK_STALE_S):
        yield


@contextmanager
def _lock_dir(path: Path, stale_s: float):
    """A proper-lockfile lock: the directory is the mutex, and its mtime says the holder is alive."""
    deadline = time.monotonic() + LOCK_TIMEOUT_S
    while True:
        try:
            os.mkdir(path)
            break
        except FileExistsError:
            pass
        try:
            held_for = time.time() - path.stat().st_mtime
        except FileNotFoundError:
            continue
        if time.monotonic() > deadline:
            raise SwapError(f'{path.name} stayed held (Claude Code is refreshing a login); retry shortly')
        if held_for > stale_s:
            try:
                os.rmdir(path)
            except OSError:
                time.sleep(0.05)
            continue
        time.sleep(0.25 + random.random() * 0.25)

    stop = threading.Event()

    def keep_alive() -> None:
        while not stop.wait(LOCK_TOUCH_S):
            try:
                os.utime(path)
            except OSError:
                return

    toucher = threading.Thread(target=keep_alive, daemon=True)
    toucher.start()
    try:
        yield
    finally:
        stop.set()
        toucher.join(timeout=1.0)
        try:
            os.rmdir(path)
        except FileNotFoundError:
            pass


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
