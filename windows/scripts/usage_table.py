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
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path

from cc_run import is_profile_name

PROFILES_DIR = Path(os.environ.get('CLAUDE_PROFILES') or Path.home() / '.claude-profiles').expanduser()
MAX_PARALLEL = 6
SESSION = re.compile(r'Current session:\s*(\d+)%')
WEEK_ALL = re.compile(r'Current week \(all models\):\s*(\d+)%')
WEEK_OTHER = re.compile(r'Current week \((?!all models)([^)]+)\):\s*(\d+)%')
WEEK_RESET = re.compile(r'Current week \(all models\).*?resets ([^(\n]+)')


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
    # cc's own rule, so the table probes exactly the folders cc and cc-use accept.
    return path.is_dir() and is_profile_name(path.name)


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
    status = _claude(['auth', 'status'], config_dir)
    if not _logged_in(status):
        row['note'] = _error_line(status) or 'not logged in'
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
        row['note'] = _error_line(out) or 'no limit data'
    return row


def _current_account() -> str | None:
    """Email of the account this session runs as, per CLAUDE_CONFIG_DIR (unset = default)."""
    raw = os.environ.get('CLAUDE_CONFIG_DIR')
    if raw:
        return _account_email(Path(raw))
    loaded = _loaded_profile()
    # A .loaded left naming a deleted profile still means the default login is the one in use.
    return (loaded and _account_email(PROFILES_DIR / loaded)) or _account_email(None)


def _loaded_profile() -> str | None:
    """The profile cc-use has put in the default slot, if any, spelled as its folder is.

    Compared by folder like cc's guard, since Windows opens work's folder for WORK too.
    """
    try:
        loaded = (PROFILES_DIR / '.loaded').read_text().strip()
    except FileNotFoundError:
        return None
    folder = PROFILES_DIR / loaded
    if not loaded or not folder.is_dir():
        return loaded or None
    return next((p.name for p in PROFILES_DIR.iterdir() if p.is_dir() and os.path.samefile(p, folder)), loaded)


def _parse_reset(reset: str) -> tuple[datetime | None, str]:
    """Parse a '[<Mon> <D>[, <YYYY>] at ]<time>' reset into (absolute datetime, '<Weekday> at <time>').

    A bare time resets today. A date with no year takes the year that puts the reset in
    the near future rather than the recent past -- that resolved datetime is what lets
    callers sort resets chronologically instead of alphabetically by weekday name.
    """
    date_part, _, time_part = reset.rpartition(' at ')
    time_part = time_part.strip()
    time_of_day = None
    for fmt in ('%I%p', '%I:%M%p'):
        try:
            time_of_day = datetime.strptime(time_part.upper(), fmt).time()
            break
        except ValueError:
            continue
    if time_of_day is None:
        return None, reset
    now = datetime.now()
    for day in _reset_days(date_part.strip(), now):
        candidate = datetime.combine(day, time_of_day)
        if candidate >= now - timedelta(days=7):
            return candidate, f'{day.strftime("%A")} at {time_part}'
    return None, reset


def _reset_days(date_part: str, now: datetime) -> list[date]:
    """The calendar days a reset's date text can mean, earliest first."""
    if not date_part:
        return [now.date()]
    try:
        return [datetime.strptime(date_part, '%b %d, %Y').date()]
    except ValueError:
        pass
    try:
        # Parse against a leap year, so Feb 29 is a valid day before the real year is chosen.
        month_day = datetime.strptime(f'{date_part} 2000', '%b %d %Y').date()
    except ValueError:
        return []
    days = []
    for year in (now.year - 1, now.year, now.year + 1):
        try:
            days.append(month_day.replace(year=year))
        except ValueError:  # Feb 29 in a non-leap year
            continue
    return days


def _account_email(config_dir: Path | None) -> str | None:
    path = (config_dir / '.claude.json') if config_dir else (Path.home() / '.claude.json')
    try:
        # Claude Code writes UTF-8, not the locale's code page; utf-8-sig also takes a BOM.
        with path.open(encoding='utf-8-sig') as fh:
            config = json.load(fh)
    except (OSError, ValueError):  # ValueError: bad JSON or bytes that are not UTF-8
        return None
    account = config.get('oauthAccount') if isinstance(config, dict) else None
    return account.get('emailAddress') if isinstance(account, dict) else None


def _logged_in(status: str) -> bool:
    """Read `claude auth status` output, which may carry warning lines before its JSON."""
    start = status.find('{')
    if start < 0:
        return False
    try:
        parsed, _ = json.JSONDecoder().raw_decode(status, start)
    except json.JSONDecodeError:
        return False
    return parsed.get('loggedIn') is True


def _error_line(out: str) -> str:
    """The first line of an `error: ...` string from _claude, else ''."""
    return out.splitlines()[0] if out.startswith('error: ') else ''


def _claude(args: list[str], config_dir: Path | None) -> str:
    env = dict(os.environ)
    env.pop('CLAUDE_CONFIG_DIR', None)
    if config_dir:
        env['CLAUDE_CONFIG_DIR'] = str(config_dir)
    # shutil.which honours PATHEXT, so it finds claude.exe or an npm-installed claude.cmd.
    claude = shutil.which('claude')
    if claude is None:
        return 'error: claude is not on PATH'
    try:
        done = subprocess.run(
            [claude, *args],
            capture_output=True,
            encoding='utf-8',
            errors='replace',
            env=env,
            stdin=subprocess.DEVNULL,
            timeout=120,
        )
        return done.stdout + done.stderr
    except (subprocess.TimeoutExpired, OSError) as exc:
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
