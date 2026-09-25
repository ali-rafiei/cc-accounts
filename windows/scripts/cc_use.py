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

import json
import os
import random
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path

PROFILES_DIR = Path(os.environ.get('CLAUDE_PROFILES') or Path.home() / '.claude-profiles').expanduser()
LOADED_FILE = PROFILES_DIR / '.loaded'
HOME_ACCOUNT_FILE = PROFILES_DIR / '.home-account.json'
HOME_STASH_FILE = PROFILES_DIR / '.home-credentials.json'
RESERVED_NAMES = {'bin', 'default'}
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

PROFILE_URL = 'https://api.anthropic.com/api/oauth/profile'
USAGE = """usage: cc-use                 show which account is in the default slot
       cc-use <profile>       load that profile's login into the default slot
       cc-use default         put your own login back
       cc-use forget          delete cc-use's stashed copy of your login (used by uninstall)"""


class SwapError(Exception):
    """A swap refused or failed before it could leave the slot half-changed."""


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
    except SwapError as exc:
        print(f'cc-use: {exc}', file=sys.stderr)
        return 1
    return 0


def status() -> str:
    email = _read_json(GLOBAL_CONFIG).get('oauthAccount', {}).get('emailAddress')
    return f'default slot: {_label(loaded_profile())} ({email})'


def load(target: str | None) -> str:
    owner = loaded_profile()
    if target == owner:
        return f'{_label(target)} is already in the default slot'
    if target is not None:
        _require_profile(target)
        _refuse_if_running(target)
    current = _read_secret(DEFAULT_CREDENTIALS)
    if current is None:
        raise SwapError('the default slot holds no login; run `claude auth login` first')
    _verify_owner(current, owner)
    incoming = _read_secret(_owner_file(target))
    if incoming is None:
        raise SwapError(f'{target} is not logged in (cc-login {target})' if target else 'no stashed login to restore')
    incoming_account = _profile_account(target) if target else _read_json(HOME_ACCOUNT_FILE)

    with _credentials_lock(), _config_lock():
        if _read_secret(DEFAULT_CREDENTIALS) != current:
            raise SwapError('a session refreshed the default login mid-swap; retry')
        config = _read_json(GLOBAL_CONFIG)
        _write_private(_owner_file(owner), current)
        if owner is None:
            _write_private(HOME_ACCOUNT_FILE, json.dumps(config['oauthAccount'], indent=2))
        _write_private(DEFAULT_CREDENTIALS, incoming)
        config['oauthAccount'] = incoming_account
        _write_private(GLOBAL_CONFIG, json.dumps(config, indent=2))
        _write_loaded(target)

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
    HOME_STASH_FILE.unlink(missing_ok=True)
    HOME_ACCOUNT_FILE.unlink(missing_ok=True)
    return 'deleted the stashed copy of your login; your default login is untouched'


def loaded_profile() -> str | None:
    try:
        return LOADED_FILE.read_text().strip() or None
    except FileNotFoundError:
        return None


def _label(profile: str | None) -> str:
    return profile or 'default'


def _owner_file(profile: str | None) -> Path:
    """Where a login lives while it is out of the slot: its profile's own file, or the stash."""
    if profile is None:
        return HOME_STASH_FILE
    return PROFILES_DIR / profile / CREDENTIALS_FILE_NAME


def _require_profile(name: str) -> None:
    if name in RESERVED_NAMES or name[:1] in ('.', '_') or not (PROFILES_DIR / name).is_dir():
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


def _verify_owner(current: str, owner: str | None) -> None:
    """Refuse when the slot's login is not the account cc-use last put there.

    Writing it back would otherwise hand one account's login to another's profile.
    """
    expected = _profile_account(owner) if owner else _read_json(GLOBAL_CONFIG).get('oauthAccount', {})
    identity = _fetch_identity(_oauth(current).get('accessToken'))
    if identity is not None:
        if identity['uuid'] != expected.get('accountUuid'):
            raise SwapError(
                f'the default slot holds {identity["email"]}, but cc-use recorded '
                f'{_label(owner)} ({expected.get("emailAddress")}); not swapping'
            )
        return
    stored = _read_secret(_owner_file(owner))
    if stored is None or _oauth(stored).get('refreshToken') == _oauth(current).get('refreshToken'):
        return
    raise SwapError(
        'could not confirm whose login is in the default slot (offline, or its '
        'access token expired); send one message in any default session, then retry'
    )


def _fetch_identity(access_token: str | None) -> dict | None:
    if not access_token:
        return None
    request = urllib.request.Request(
        PROFILE_URL, headers={'Authorization': f'Bearer {access_token}', 'Content-Type': 'application/json'}
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            body = json.loads(response.read().decode())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
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
        return json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError as exc:
        raise SwapError(f'missing {path}') from exc


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
        if time.monotonic() > deadline:
            raise SwapError(f'{path.name} stayed held (Claude Code is refreshing a login); retry shortly')
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
