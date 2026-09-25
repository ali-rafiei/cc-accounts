#!/usr/bin/env python3
"""Print Claude Code limit usage for every configured account profile.

The default account uses no CLAUDE_CONFIG_DIR (its config is ~/.claude.json at the home
root). Every other account is a directory under $CLAUDE_PROFILES (default
~/.claude-profiles). The default login earns a row of its own only when it holds an account
no named profile covers. A star marks the account this session runs as, and `[default]`
the profile cc-use has loaded into the default login.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

PROFILES_DIR = Path(os.environ.get('CLAUDE_PROFILES') or Path.home() / '.claude-profiles').expanduser()
RESERVED_NAMES = {'bin', 'default'}  # never account profiles; neither is anything starting with . or _
MAX_PARALLEL = 6
SESSION = re.compile(r'Current session:\s*(\d+)%')
WEEK_ALL = re.compile(r'Current week \(all models\):\s*(\d+)%')
WEEK_OTHER = re.compile(r'Current week \((?!all models)([^)]+)\):\s*(\d+)%')
WEEK_RESET = re.compile(r'Current week \(all models\).*?resets ([^(]+)')


def main() -> None:
    rows = list(_collect(_discover()))
    current = _current_account()
    for row in rows:
        row['current'] = current is not None and row['account'] == current
    rows.sort(key=lambda r: (r['reset_at'] is None, r['reset_at'] or datetime.max))
    _render(rows)


def _discover() -> list[tuple[str, Path | None]]:
    """Return (profile_name, config_dir) pairs; config_dir None means the default account.

    The default config dir is dropped when a named profile already holds the same
    account, so one account is never probed (or printed) twice.
    """
    named: list[tuple[str, Path | None]] = []
    loaded = _loaded_profile()
    if PROFILES_DIR.is_dir():
        # A profile cc-use has in the default slot is probed through that slot: its own
        # Keychain copy may be stale, and refreshing it would strand the live one.
        named = [(p.name, None if p.name == loaded else p) for p in sorted(PROFILES_DIR.iterdir()) if _is_profile(p)]
    default_email = _account_email(None)
    covered = default_email is not None and any(_account_email(config_dir) == default_email for _, config_dir in named)
    return named if covered else [('default', None)] + named


def _is_profile(path: Path) -> bool:
    return path.is_dir() and path.name not in RESERVED_NAMES and path.name[:1] not in ('.', '_')


def _collect(profiles: list[tuple[str, Path | None]]) -> list[dict]:
    with ThreadPoolExecutor(max_workers=MAX_PARALLEL) as pool:
        return list(pool.map(lambda p: _probe(*p), profiles))


def _probe(name: str, config_dir: Path | None) -> dict:
    account_dir = None if name == 'default' else PROFILES_DIR / name
    row = {
        'profile': name,
        'account': _account_email(account_dir) or name,
        'session': None,
        'week': None,
        'other': '',
        'resets': '',
        'reset_at': None,
        'note': '',
    }
    if not _logged_in(config_dir):
        row['note'] = 'not logged in'
        return row
    out = _claude(['-p', '/usage'], config_dir)
    if m := SESSION.search(out):
        row['session'] = int(m.group(1))
    if m := WEEK_ALL.search(out):
        row['week'] = int(m.group(1))
    if m := WEEK_RESET.search(out):
        row['reset_at'], row['resets'] = _parse_reset(m.group(1).strip())
    row['other'] = ', '.join(f'{n.strip()} {p}%' for n, p in WEEK_OTHER.findall(out))
    if row['week'] is None:
        row['note'] = 'no limit data'
    return row


def _current_account() -> str | None:
    """Email of the account this session runs as, per CLAUDE_CONFIG_DIR (unset = default)."""
    raw = os.environ.get('CLAUDE_CONFIG_DIR')
    if raw:
        return _account_email(Path(raw))
    loaded = _loaded_profile()
    return _account_email(PROFILES_DIR / loaded if loaded else None)


def _loaded_profile() -> str | None:
    """The profile cc-use has put in the default slot, if any."""
    try:
        return (PROFILES_DIR / '.loaded').read_text().strip() or None
    except FileNotFoundError:
        return None


def _parse_reset(reset: str) -> tuple[datetime | None, str]:
    """Parse a '<Mon> <D> at <time>' reset into (absolute datetime, '<Weekday> at <time>').

    The source string carries no year, so pick the one that puts the reset in the
    near future rather than the recent past -- that resolved datetime is what lets
    callers sort resets chronologically instead of alphabetically by weekday name.
    """
    parts = reset.split(' at ', 1)
    if len(parts) != 2:
        return None, reset
    date_part, time_part = parts
    try:
        month_day = datetime.strptime(date_part.strip(), '%b %d').date()
    except ValueError:
        return None, reset
    time_of_day = None
    for fmt in ('%I%p', '%I:%M%p'):
        try:
            time_of_day = datetime.strptime(time_part.strip().upper(), fmt).time()
            break
        except ValueError:
            continue
    if time_of_day is None:
        return None, reset
    now = datetime.now()
    for year in (now.year, now.year + 1):
        try:
            day = month_day.replace(year=year)
        except ValueError:  # Feb 29 in a non-leap year
            continue
        candidate = datetime.combine(day, time_of_day)
        if candidate >= now - timedelta(days=7):
            return candidate, f'{day.strftime("%A")} at {time_part.strip()}'
    return None, reset


def _account_email(config_dir: Path | None) -> str | None:
    path = (config_dir / '.claude.json') if config_dir else (Path.home() / '.claude.json')
    try:
        with path.open() as fh:
            return json.load(fh).get('oauthAccount', {}).get('emailAddress')
    except (OSError, json.JSONDecodeError):
        return None


def _logged_in(config_dir: Path | None) -> bool:
    try:
        return json.loads(_claude(['auth', 'status'], config_dir)).get('loggedIn') is True
    except json.JSONDecodeError:
        return False


def _claude(args: list[str], config_dir: Path | None) -> str:
    env = dict(os.environ)
    env.pop('CLAUDE_CONFIG_DIR', None)
    if config_dir:
        env['CLAUDE_CONFIG_DIR'] = str(config_dir)
    try:
        done = subprocess.run(
            ['claude', *args], capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL, timeout=120
        )
        return done.stdout + done.stderr
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        return f'error: {exc}'


def _render(rows: list[dict]) -> None:
    headers = ('ACCOUNT', 'PROFILE', 'SESSION', 'WEEK', 'OTHER LIMITS', 'WEEK RESETS')
    loaded = _loaded_profile()
    table = [headers] + [
        (
            r['account'],
            r['profile'] + (' [default]' if r['profile'] == loaded else '') + (' *' if r.get('current') else ''),
            r['note'] or (f'{r["session"]}%' if r['session'] is not None else '-'),
            '' if r['note'] else (f'{r["week"]}%' if r['week'] is not None else '-'),
            r['other'],
            r['resets'],
        )
        for r in rows
    ]
    widths = [max(len(row[i]) for row in table) for i in range(len(headers))]
    for i, row in enumerate(table):
        print('  '.join(cell.ljust(w) for cell, w in zip(row, widths)).rstrip())
        if i == 0:
            print('  '.join('-' * w for w in widths))


if __name__ == '__main__':
    main()
