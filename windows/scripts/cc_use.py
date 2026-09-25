#!/usr/bin/env python3
"""Load a profile's Claude login into the default slot (Windows): `cc-use work`, `cc-use default`.

On Windows, Claude Code keeps a login as plain JSON in `<config dir>/.credentials.json`:
~/.claude/.credentials.json for the default login, `<profile>/.credentials.json` for a
profile. The default slot is that file plus `oauthAccount` in ~/.claude.json, and it is
what VS Code and every plain `claude` run as. A login is moved, not duplicated: the one
leaving the slot is written back to its owner first, because a session using it may have
refreshed (rotated) its token. The lock protocol follows claude-swap
(MIT, github.com/realiti4/claude-swap).
"""

from __future__ import annotations

import http.client
import json
import os
import random
import sys
import tempfile
import threading
import time
import urllib.request
from contextlib import contextmanager
from pathlib import Path

from cc_run import is_existing_profile_name

PROFILES_DIR = Path(os.environ.get('CLAUDE_PROFILES') or Path.home() / '.claude-profiles').expanduser()
LOADED_FILE = PROFILES_DIR / '.loaded'
HOME_ACCOUNT_FILE = PROFILES_DIR / '.home-account.json'
HOME_STASH_FILE = PROFILES_DIR / '.home-credentials.json'
GLOBAL_CONFIG = Path.home() / '.claude.json'
DEFAULT_CREDENTIALS = Path.home() / '.claude' / '.credentials.json'
CREDENTIALS_FILE_NAME = '.credentials.json'
# Claude Code's own lock directories: two guard a token refresh, one guards ~/.claude.json.
OAUTH_REFRESH_LOCK = Path.home() / '.claude' / '.oauth_refresh.lock'
LEGACY_CREDENTIALS_LOCK = Path.home() / '.claude.lock'
CONFIG_LOCK = Path.home() / '.claude.json.lock'

# Claude Code's proper-lockfile settings: credential locks go stale after 60s, the
# ~/.claude.json lock after 10s, and live holders touch theirs every 5s.
CREDENTIALS_LOCK_STALE_S = 60.0
CONFIG_LOCK_STALE_S = 10.0
LOCK_TOUCH_S = 3.0
LOCK_TIMEOUT_S = 9.0
# Another process holding the file open (an editor, an indexer, Claude itself mid-read)
# makes a Windows rename fail for a moment; retry briefly before giving up.
REPLACE_ATTEMPTS = 10

# `cc-use forget` exits with this when it only could not check the slot, so uninstall can go on.
UNCONFIRMED_EXIT = 3
PROFILE_URL = 'https://api.anthropic.com/api/oauth/profile'
USAGE = """usage: cc-use                 show which account is in the default slot
       cc-use <profile>       load that profile's login into the default slot
       cc-use default         put your own login back
       cc-use forget          delete cc-use's stashed copy of your login (used by uninstall)"""


class SwapError(Exception):
    """A swap refused or failed before it could leave the slot half-changed."""


class UnconfirmedOwner(SwapError):
    """Whose login the slot holds could not be checked (offline); a later retry can succeed."""


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
    if sys.platform == 'darwin':
        print('cc-use: this is the Windows version; macOS keeps logins in the Keychain', file=sys.stderr)
        return 1
    try:
        if not argv or argv[0] == 'status':
            print(status())
        elif argv[0] == 'forget':
            print(forget())
        else:
            print(load(None if argv[0] == 'default' else argv[0]))
    except UnconfirmedOwner as exc:
        print(f'cc-use: {exc}', file=sys.stderr)
        return UNCONFIRMED_EXIT
    except SwapError as exc:
        print(f'cc-use: {exc}', file=sys.stderr)
        return 1
    return 0


def status() -> str:
    email = (_read_json(GLOBAL_CONFIG).get('oauthAccount') or {}).get('emailAddress')
    return f'default slot: {_label(loaded_profile())} ({email})'


def load(target: str | None) -> str:
    owner = loaded_profile()
    if owner is None and target is None:
        # A swap killed before it wrote .loaded: finish putting the user's login back.
        slot = _read_secret(DEFAULT_CREDENTIALS)
        owner = _interrupted_swap_owner(slot) if slot is not None else None
    if target == owner:
        return f'{_label(target)} is already in the default slot'
    if target is not None:
        target = _require_profile(target)
        if target == owner:
            return f'{target} is already in the default slot'
    current = _read_secret(DEFAULT_CREDENTIALS)
    if current is None:
        raise SwapError('the default slot holds no login; run `claude auth login` first')
    # The identity request stays outside the lock, which Claude Code's own refreshes wait on.
    _verify_owner(current, owner)

    with _credentials_lock(), _config_lock():
        if _read_secret(DEFAULT_CREDENTIALS) != current:
            raise SwapError('a session refreshed the default login mid-swap; retry')
        # Checked under the lock, so a `cc <profile>` started, or a refresh made, since cc-use began is seen.
        if target is not None:
            _refuse_if_running(target)
        incoming = _read_secret(_owner_file(target))
        if incoming is None:
            raise SwapError(
                f'{target} is not logged in (cc-login {target})' if target else 'no stashed login to restore'
            )
        incoming_account = _profile_account(target) if target else _read_json(HOME_ACCOUNT_FILE)
        config_text = _read_secret(GLOBAL_CONFIG)
        config = _read_json(GLOBAL_CONFIG)
        # The record goes first: uninstall runs `forget` off these files, so no stash may outlive it.
        if owner is None:
            home_account = config.get('oauthAccount')
            if not isinstance(home_account, dict) or not home_account:
                raise SwapError(f'{GLOBAL_CONFIG} records no oauthAccount for the default login; not swapping')
            _write_private(HOME_ACCOUNT_FILE, json.dumps(home_account, indent=2))
        _write_private(_owner_file(owner), current)
        try:
            _write_private(DEFAULT_CREDENTIALS, incoming)
            config['oauthAccount'] = incoming_account
            _write_private(GLOBAL_CONFIG, json.dumps(config, indent=2))
            _write_loaded(target)
        except BaseException:
            # Half a swap strands the slot: its login would no longer match what cc-use
            # recorded, and every retry would be refused. Put all three back as they were,
            # each on its own, so one restore that fails does not skip the others.
            restores = (
                (DEFAULT_CREDENTIALS, lambda: _write_private(DEFAULT_CREDENTIALS, current)),
                (GLOBAL_CONFIG, lambda: _write_private(GLOBAL_CONFIG, config_text)),
                (LOADED_FILE, lambda: _write_loaded(owner)),
            )
            for path, restore in restores:
                try:
                    restore()
                except Exception as exc:  # the swap's own error is the one raised below
                    print(f'cc-use: could not put {path} back ({exc})', file=sys.stderr)
            raise
        if target is None:
            # The slot holds the user's own login again, so the stash is now a copy that goes
            # stale as that login rotates. The stash goes first: no stash may outlive its record.
            HOME_STASH_FILE.unlink(missing_ok=True)
            HOME_ACCOUNT_FILE.unlink(missing_ok=True)

    return (
        f'default slot: {_label(owner)} -> {_label(target)} '
        f'({incoming_account.get("emailAddress")}). New sessions start on it now, and running '
        'ones pick it up on their next message; reopen the Claude tab in VS Code to be sure.'
    )


def forget() -> str:
    """Delete the stash left by a round trip; it is a stale copy once your own login is back."""
    loaded = loaded_profile()
    if loaded is not None:
        raise SwapError(f'{loaded} is loaded, so the stash holds your only login; run `cc-use default` first')
    if not HOME_STASH_FILE.exists():
        HOME_ACCOUNT_FILE.unlink(missing_ok=True)
        return 'nothing stashed; nothing to delete'
    # A swap killed before it wrote .loaded leaves a profile in the slot and your login only in the stash.
    current = _read_secret(DEFAULT_CREDENTIALS)
    if current is None:
        raise SwapError('the default slot holds no login, so the stash is your only copy; not deleting it')
    swapped_in = _interrupted_swap_owner(current)
    if swapped_in is not None:
        raise SwapError(
            f"the default slot holds {swapped_in}'s login, left by an interrupted swap, so the stash "
            'is your only copy; run `cc-use default` to put yours back'
        )
    _verify_owner(current, None, doing='deleting the stash')
    HOME_STASH_FILE.unlink(missing_ok=True)
    HOME_ACCOUNT_FILE.unlink(missing_ok=True)
    return 'deleted the stashed copy of your login; your default login is untouched'


def loaded_profile() -> str | None:
    """The profile in the default slot, spelled as its folder is, so every comparison is by folder.

    An older cc-use recorded the name as typed (WORK), which Windows opens as work's folder.
    """
    try:
        loaded = LOADED_FILE.read_text().strip()
    except FileNotFoundError:
        return None
    folder = PROFILES_DIR / loaded
    if loaded and folder.is_dir():
        for candidate in PROFILES_DIR.iterdir():
            if candidate.is_dir() and os.path.samefile(candidate, folder):
                return candidate.name
    return loaded or None


def _label(profile: str | None) -> str:
    return profile or 'default'


def _owner_file(profile: str | None) -> Path:
    """Where a login lives while it is out of the slot: its profile's own file, or the stash."""
    if profile is None:
        return HOME_STASH_FILE
    return PROFILES_DIR / profile / CREDENTIALS_FILE_NAME


def _require_profile(name: str) -> str:
    """The profile's name as its folder spells it, since Windows opens work's folder for WORK too."""
    if is_existing_profile_name(name) and (PROFILES_DIR / name).is_dir():
        for folder in PROFILES_DIR.iterdir():
            if folder.name.lower() == name.lower():
                return folder.name
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
    if sys.platform == 'win32':
        return _alive_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _alive_windows(pid: int) -> bool:
    # Never os.kill(pid, 0) here: on Windows it terminates the process.
    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    still_active = 259
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True
        return code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


def _verify_owner(current: str, owner: str | None, doing: str = 'swapping') -> None:
    """Refuse when the slot's login is not the account cc-use last put there.

    Writing it back would otherwise hand one account's login to another's profile.
    """
    stored = _read_secret(_owner_file(owner))
    expected = _profile_account(owner) if owner else _home_account(stored)
    identity = _fetch_identity(_oauth(current).get('accessToken'))
    if identity is not None:
        if identity['uuid'] != expected.get('accountUuid'):
            hint = ''
            swapped_in = _profiles_holding(current, identity) if owner is None and stored is not None else []
            if swapped_in:
                hint = (
                    f". It holds {swapped_in[0]}'s login, left by an interrupted swap; "
                    'run `cc-use default` to put yours back'
                )
            elif owner is None and stored is not None:
                hint = (
                    f'. If you have since logged in as yourself again and the stash is stale, '
                    f'delete {HOME_STASH_FILE} and {HOME_ACCOUNT_FILE} by hand'
                )
            raise SwapError(
                f'the default slot holds {identity["email"]}, but cc-use recorded '
                f'{_label(owner)} ({expected.get("emailAddress")}); not {doing}{hint}'
            )
        return
    if stored is None or _oauth(stored).get('refreshToken') == _oauth(current).get('refreshToken'):
        return
    raise UnconfirmedOwner(
        'could not confirm whose login is in the default slot (offline, or its '
        'access token expired); send one message in any default session, then retry'
    )


def _interrupted_swap_owner(current: str) -> str | None:
    """The profile a swap killed before it wrote .loaded left in the slot, when exactly one fits.

    None when there is no stash, or the slot holds the user's own account: a profile of that
    same account is not proof of an interrupted swap.
    """
    stash = _read_secret(HOME_STASH_FILE)
    if stash is None:
        return None
    identity = _fetch_identity(_oauth(current).get('accessToken'))
    if identity is not None and identity['uuid'] == _home_account(stash).get('accountUuid'):
        return None
    matches = _profiles_holding(current, identity)
    return matches[0] if len(matches) == 1 else None


def _profiles_holding(current: str, identity: dict | None) -> list[str]:
    """The profiles whose login the slot holds: by account online, by refresh token offline."""
    refresh_token = _oauth(current).get('refreshToken')
    matches = []
    for folder in sorted(PROFILES_DIR.iterdir()):
        if not (folder.is_dir() and is_existing_profile_name(folder.name)):
            continue
        if identity is not None:
            try:
                account = _read_json(folder / '.claude.json').get('oauthAccount')
            except SwapError:
                continue
            if isinstance(account, dict) and account.get('accountUuid') == identity['uuid']:
                matches.append(folder.name)
        elif refresh_token:
            stored = _read_secret(folder / CREDENTIALS_FILE_NAME)
            if stored is not None and _oauth(stored).get('refreshToken') == refresh_token:
                matches.append(folder.name)
    return matches


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
        return json.loads(credentials.lstrip('\ufeff')).get('claudeAiOauth') or {}
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
        # utf-8-sig: Notepad and Windows PowerShell 5.1 save UTF-8 with a BOM, which json.loads rejects.
        data = json.loads(path.read_text(encoding='utf-8-sig'))
    except FileNotFoundError as exc:
        raise SwapError(f'missing {path}') from exc
    except ValueError as exc:  # bad JSON, or bytes that are not UTF-8
        raise SwapError(f'{path} is not valid JSON ({exc}); not touching it') from exc
    if not isinstance(data, dict):
        raise SwapError(f'{path} holds {type(data).__name__}, not a JSON object; not touching it')
    return data


def _read_secret(path: Path) -> str | None:
    try:
        return path.read_text(encoding='utf-8')
    except FileNotFoundError:
        return None


def _write_private(path: Path, text: str) -> None:
    """Write through a temp file and an atomic rename, so no reader ever sees half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f'.{path.name}.')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        _replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _replace(source: str, destination: Path) -> None:
    for attempt in range(REPLACE_ATTEMPTS):
        try:
            os.replace(source, destination)
            return
        except PermissionError:
            if attempt == REPLACE_ATTEMPTS - 1:
                raise SwapError(f'{destination} stayed locked by another program; retry') from None
            time.sleep(0.1)


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
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + LOCK_TIMEOUT_S
    while True:
        try:
            os.mkdir(path)
            break
        except FileExistsError:
            pass
        # Checked first: a stale lock that cannot be removed (a file, or not empty) would
        # otherwise be retried forever.
        if time.monotonic() > deadline:
            raise SwapError(f'{path.name} stayed held (Claude Code is refreshing a login); retry shortly')
        try:
            held_for = time.time() - path.stat().st_mtime
        except FileNotFoundError:
            continue
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
