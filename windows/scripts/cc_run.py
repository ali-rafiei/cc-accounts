#!/usr/bin/env python3
"""Run Claude Code as a profile's account (Windows): the logic behind `cc`, `cc-add` and `cc-login`.

A profile is a directory under %CLAUDE_PROFILES% (default ~/.claude-profiles) passed to
Claude Code as CLAUDE_CONFIG_DIR. Before each launch the profile gets junctions to
~/.claude/skills and ~/.claude/plugins, and a copy of the plugin on/off keys from
~/.claude/settings.json, so every profile sees your skills and plugins. PowerShell
(profiles.ps1) and Git Bash (profiles.sh) both call this script.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

PROFILES_DIR = Path(os.environ.get('CLAUDE_PROFILES') or Path.home() / '.claude-profiles').expanduser()
LOADED_FILE = PROFILES_DIR / '.loaded'
CLAUDE_HOME = Path.home() / '.claude'
RESERVED_NAMES = {'bin', 'default'}
SHARED_DIRS = ('skills', 'plugins')
MIRRORED_SETTINGS = ('enabledPlugins', 'extraKnownMarketplaces')
LOGIN_HINT = (
    'Sign in with the account this profile is for. If your browser is already signed into a '
    'different Claude account, copy the sign-in link Claude shows into a private window instead.'
)
USAGE = """usage: cc <profile|default> [--prompt-file FILE] [claude args...]
       cc-add <profile>      create a profile and log it in
       cc-login <profile>    log a profile in again"""


class ProfileError(Exception):
    """A request this script refuses, with the reason for the user."""


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ('-h', '--help'):
        print(USAGE)
        print(f'profiles: default {" ".join(list_profiles())}')
        return 0 if argv else 1
    command, rest = argv[0], argv[1:]
    try:
        if command == 'run' and rest:
            return run(rest[0], rest[1:])
        if command == 'add' and len(rest) == 1:
            return add(rest[0])
        if command == 'login' and len(rest) == 1:
            print(LOGIN_HINT, file=sys.stderr)
            return run(rest[0], ['auth', 'login'])
        if command == 'list':
            print('\n'.join(list_profiles()))
            return 0
    except ProfileError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(USAGE, file=sys.stderr)
    return 2


def run(profile: str, claude_args: list[str]) -> int:
    """Launch `claude` as the profile (or as the default login) and return its exit code."""
    prompt_file = None
    if claude_args[:1] == ['--prompt-file']:
        if len(claude_args) < 2:
            raise ProfileError('--prompt-file needs a path')
        prompt_file, claude_args = Path(claude_args[1]), claude_args[2:]
    env = {k: v for k, v in os.environ.items() if k != 'CLAUDE_CONFIG_DIR'}
    if profile != 'default':
        profile_dir = require_profile(profile)
        if loaded_profile() == profile:
            raise ProfileError(
                f'{profile} is loaded into the default login by cc-use: run `cc default`, or `cc-use default` first'
            )
        share(profile_dir)
        env['CLAUDE_CONFIG_DIR'] = str(profile_dir)
    return _launch(_claude_executable(), claude_args, env, prompt_file)


def add(profile: str) -> int:
    if not is_profile_name(profile):
        raise ProfileError(f'not a usable profile name: {profile!r} (not default or bin, no leading . or _)')
    (PROFILES_DIR / profile).mkdir(parents=True, exist_ok=True)
    print(LOGIN_HINT, file=sys.stderr)
    return run(profile, ['auth', 'login'])


def share(profile_dir: Path) -> None:
    """Point the profile at your skills and plugins, and mirror which plugins are on."""
    for name in SHARED_DIRS:
        link = profile_dir / name
        if os.path.lexists(link):
            continue
        target = CLAUDE_HOME / name
        target.mkdir(parents=True, exist_ok=True)
        _link_dir(link, target)
    _mirror_plugin_settings(profile_dir / 'settings.json')


def list_profiles() -> list[str]:
    if not PROFILES_DIR.is_dir():
        return []
    return sorted(p.name for p in PROFILES_DIR.iterdir() if p.is_dir() and is_profile_name(p.name))


def is_profile_name(name: str) -> bool:
    return (
        bool(name)
        and name not in RESERVED_NAMES
        and name[:1] not in ('.', '_')
        and '/' not in name
        and '\\' not in name
    )


def require_profile(name: str) -> Path:
    path = PROFILES_DIR / name
    if not is_profile_name(name) or not path.is_dir():
        raise ProfileError(f'no such profile: {name} (create it with: cc-add {name})')
    return path


def loaded_profile() -> str | None:
    try:
        return LOADED_FILE.read_text().strip() or None
    except FileNotFoundError:
        return None


def _mirror_plugin_settings(target_path: Path) -> None:
    source_path = CLAUDE_HOME / 'settings.json'
    # utf-8-sig: Notepad and Windows PowerShell 5.1 save UTF-8 with a BOM, which json.loads rejects.
    source = json.loads(source_path.read_text(encoding='utf-8-sig')) if source_path.exists() else {}
    target = json.loads(target_path.read_text(encoding='utf-8-sig')) if target_path.exists() else {}
    merged = {**target, **{k: source[k] for k in MIRRORED_SETTINGS if k in source}}
    if merged != target:
        target_path.write_text(json.dumps(merged, indent=2) + '\n', encoding='utf-8')


def _link_dir(link: Path, target: Path) -> None:
    """A directory junction on Windows (no admin or Developer Mode needed), a symlink elsewhere."""
    if sys.platform != 'win32':
        link.symlink_to(target, target_is_directory=True)
        return
    done = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(target)], capture_output=True, text=True)
    if done.returncode != 0:
        raise ProfileError(f'could not link {link} to {target}: {(done.stdout + done.stderr).strip()}')


def _claude_executable() -> str:
    # shutil.which honours PATHEXT, so it finds claude.exe or an npm-installed claude.cmd.
    found = shutil.which('claude')
    if found is None:
        raise ProfileError('claude is not on PATH; install Claude Code first')
    return found


def _launch(claude: str, args: list[str], env: dict[str, str], prompt_file: Path | None) -> int:
    # Ctrl+C belongs to claude, which shares this console; this process only waits for it.
    previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        if prompt_file is None:
            return subprocess.run([claude, *args], env=env).returncode
        with prompt_file.open('rb') as stdin:
            return subprocess.run([claude, *args], env=env, stdin=stdin).returncode
    finally:
        signal.signal(signal.SIGINT, previous)


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
