import json
import os
import sys

import pytest

import cc_run

LOGIN_ENV = {'ANTHROPIC_API_KEY': 'sk-ant', 'ANTHROPIC_AUTH_TOKEN': 'bearer', 'CLAUDE_CODE_OAUTH_TOKEN': 'oauth'}
STUB = f"""import json, os, sys
stdin = '' if sys.stdin is None or sys.stdin.isatty() else sys.stdin.read()
login_env = sorted(k for k in os.environ if k in {sorted(LOGIN_ENV)!r})
config = os.environ.get('CLAUDE_CONFIG_DIR')
print(json.dumps({{'config': config, 'args': sys.argv[1:], 'stdin': stdin, 'login_env': login_env}}))
"""
# On Windows the stand-in is an npm shim, which cc_run runs through node rather than cmd.exe.
STUB_JS = f"""const fs = require('fs');
const env = process.env;
const stdin = process.stdin.isTTY ? '' : fs.readFileSync(0, 'utf8');
const loginEnv = {json.dumps(sorted(LOGIN_ENV))}.filter((k) => k in env);
const config = env.CLAUDE_CONFIG_DIR || null;
console.log(JSON.stringify({{config, args: process.argv.slice(2), stdin, login_env: loginEnv}}));
"""
NPM_SHIM = (
    r"""@ECHO off
GOTO start
:find_dp0
SET dp0=%~dp0
EXIT /b
:start
SETLOCAL
CALL :find_dp0

IF EXIST "%dp0%\node.exe" (
  SET "_prog=%dp0%\node.exe"
) ELSE (
  SET "_prog=node"
  SET PATHEXT=%PATHEXT:;.JS;=;%
)

endLocal & goto #_undefined_# 2>NUL || title %COMSPEC% & "%_prog%" """
    r""" "%dp0%\node_modules\@anthropic-ai\claude-code\cli.js" %*
"""
)
OLD_NPM_SHIM = r"""@IF EXIST "%~dp0\node.exe" (
  "%~dp0\node.exe"  "%~dp0\node_modules\@anthropic-ai\claude-code\cli.js" %*
) ELSE (
  @SETLOCAL
  @SET PATHEXT=%PATHEXT:;.JS;=;%
  node  "%~dp0\node_modules\@anthropic-ai\claude-code\cli.js" %*
)
"""


@pytest.fixture
def machine(tmp_path, monkeypatch):
    """Fake profiles and ~/.claude, with a stand-in `claude` on PATH that reports what it was given."""
    profiles = tmp_path / 'profiles'
    profiles.mkdir()
    claude_home = tmp_path / '.claude'
    (claude_home / 'skills').mkdir(parents=True)
    (claude_home / 'plugins').mkdir()
    (claude_home / 'settings.json').write_text(json.dumps({'theme': 'dark', 'enabledPlugins': {'tool@market': True}}))
    monkeypatch.setattr(cc_run, 'PROFILES_DIR', profiles)
    monkeypatch.setattr(cc_run, 'LOADED_FILE', profiles / '.loaded')
    monkeypatch.setattr(cc_run, 'CLAUDE_HOME', claude_home)

    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    if sys.platform == 'win32':
        (bin_dir / 'claude_stub.js').write_text(STUB_JS)
        (bin_dir / 'claude.cmd').write_text('@"%~dp0\\claude_stub.js" %*\r\n')
    else:
        (bin_dir / 'claude_stub.py').write_text(STUB)
        stub = bin_dir / 'claude'
        stub.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{bin_dir / "claude_stub.py"}" "$@"\n')
        stub.chmod(0o755)
    monkeypatch.setenv('PATH', str(bin_dir) + os.pathsep + os.environ['PATH'])
    return {'profiles': profiles, 'claude_home': claude_home, 'tmp': tmp_path}


@pytest.fixture
def windows(tmp_path, monkeypatch):
    """Resolve programs as Windows does, from inside a cloned repo that ships its own claude.bat."""
    monkeypatch.setattr(sys, 'platform', 'win32')
    monkeypatch.setenv('PATHEXT', '.COM;.EXE;.BAT;.CMD')
    repo = tmp_path / 'untrusted-repo'
    repo.mkdir()
    (repo / 'claude.bat').write_text('@calc.exe\r\n')
    (repo / 'node.exe').write_text('')
    monkeypatch.chdir(repo)
    return tmp_path


def test__run__launches_claude_with_the_profiles_config_dir(machine, capfd):
    # Arrange
    (machine['profiles'] / 'work').mkdir()

    # Act
    code = cc_run.run('work', ['-p', 'hi'])

    # Assert
    seen = _seen(capfd)
    assert code == 0
    assert seen['config'] == str(machine['profiles'] / 'work')
    assert seen['args'] == ['-p', 'hi']


def test__run__default_clears_an_inherited_config_dir(machine, capfd, monkeypatch):
    # Arrange: e.g. launched from inside a profile's own session.
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(machine['tmp'] / 'somewhere'))

    # Act
    cc_run.run('default', ['--version'])

    # Assert
    seen = _seen(capfd)
    assert seen['config'] is None
    assert seen['args'] == ['--version']


def test__run__feeds_a_prompt_file_on_stdin(machine, capfd):
    # Arrange
    (machine['profiles'] / 'work').mkdir()
    prompt = machine['tmp'] / 'prompt.txt'
    prompt.write_text('say "hi" & exit')

    # Act
    cc_run.run('work', ['--prompt-file', str(prompt), '-p'])

    # Assert
    seen = _seen(capfd)
    assert seen['stdin'] == 'say "hi" & exit'
    assert seen['args'] == ['-p']


def test__main__takes_its_arguments_as_json_from_the_environment(machine, capfd, monkeypatch):
    # Arrange: how profiles.ps1 passes them, since Windows PowerShell 5.1 splits an argument
    # with embedded quotes and drops an empty one.
    (machine['profiles'] / 'work').mkdir()
    monkeypatch.setenv(cc_run.ARGV_ENV, json.dumps(['run', 'work', '-p', 'say "hi there"', '']))

    # Act
    code = cc_run.main(['--argv-from-env'])

    # Assert: claude gets them intact, and not the variable itself.
    assert code == 0
    assert _seen(capfd)['args'] == ['-p', 'say "hi there"', '']
    assert cc_run.ARGV_ENV not in os.environ


def test__run__refuses_the_profile_cc_use_has_loaded(machine, capfd):
    # Arrange
    (machine['profiles'] / 'work').mkdir()
    (machine['profiles'] / '.loaded').write_text('work\n')

    # Act / Assert
    with pytest.raises(cc_run.ProfileError, match='loaded into the default login'):
        cc_run.run('work', [])
    assert capfd.readouterr().out == ''


@pytest.mark.parametrize('action', ['login', 'logout'])
def test__run__default_refuses_to_log_in_or_out_while_a_profile_is_loaded(machine, capfd, action):
    # Arrange: the default slot holds work's only live login.
    (machine['profiles'] / 'work').mkdir()
    (machine['profiles'] / '.loaded').write_text('work\n')

    # Act / Assert
    with pytest.raises(cc_run.ProfileError, match='cc-use default'):
        cc_run.run('default', ['auth', action])
    assert capfd.readouterr().out == ''


def test__main__login_default_refuses_while_a_profile_is_loaded(machine, capfd):
    # Arrange
    (machine['profiles'] / 'work').mkdir()
    (machine['profiles'] / '.loaded').write_text('work\n')

    # Act
    code = cc_run.main(['login', 'default'])

    # Assert
    assert code == 1
    assert 'cc-use default' in capfd.readouterr().err


def test__run__default_runs_other_commands_while_a_profile_is_loaded(machine, capfd):
    # Arrange
    (machine['profiles'] / 'work').mkdir()
    (machine['profiles'] / '.loaded').write_text('work\n')

    # Act
    code = cc_run.run('default', ['auth', 'status'])

    # Assert
    assert code == 0
    assert _seen(capfd)['args'] == ['auth', 'status']


@pytest.mark.parametrize(
    'recorded',
    [
        'work/',
        pytest.param('work\\', marks=pytest.mark.skipif(sys.platform != 'win32', reason='a separator on Windows')),
        'WORK',
    ],
)
def test__run__refuses_the_loaded_profile_under_another_spelling(machine, recorded):
    # Arrange: every spelling here opens the same folder on Windows.
    (machine['profiles'] / 'work').mkdir()
    (machine['profiles'] / '.loaded').write_text(recorded + '\n')

    # Act / Assert
    with pytest.raises(cc_run.ProfileError, match='loaded into the default login'):
        cc_run.run('work', [])


@pytest.mark.parametrize('folder', ['alice@corp', 'jos\u00e9', 'work+2', "o'brien", '-x', 'old & copy'])
def test__run__accepts_an_existing_folder_made_before_names_were_narrowed(machine, capfd, folder):
    # Arrange: cc-add no longer makes these names, but a folder already there is still a profile.
    (machine['profiles'] / folder).mkdir()

    # Act
    code = cc_run.run(folder, ['-p', 'hi'])

    # Assert
    assert code == 0
    assert _seen(capfd)['config'] == str(machine['profiles'] / folder)
    assert folder in cc_run.list_profiles()


def test__share__links_under_a_path_cmd_would_split(machine, monkeypatch):
    # Arrange: & and % mean something to cmd.exe, even inside a folder name.
    profiles = machine['tmp'] / 'O&Brien%PATH%'
    profile = profiles / 'work'
    profile.mkdir(parents=True)
    monkeypatch.setattr(cc_run, 'PROFILES_DIR', profiles)

    # Act
    cc_run.share(profile)

    # Assert
    assert os.path.samefile(profile / 'skills', machine['claude_home'] / 'skills')


@pytest.mark.parametrize('name', ['bin', '_scratch', '.hidden', 'missing'])
def test__run__rejects_names_that_are_not_profiles(machine, name):
    # Arrange
    for folder in ('bin', '_scratch', '.hidden'):
        (machine['profiles'] / folder).mkdir()

    # Act / Assert
    with pytest.raises(cc_run.ProfileError, match='no such profile'):
        cc_run.run(name, [])


def test__run__shares_skills_plugins_and_plugin_settings(machine, capfd):
    # Arrange
    profile = machine['profiles'] / 'work'
    profile.mkdir()
    (profile / 'settings.json').write_text(json.dumps({'theme': 'light'}))

    # Act
    cc_run.run('work', [])

    # Assert: on Windows these are junctions, elsewhere symlinks; either way they are the same dirs.
    assert os.path.samefile(profile / 'skills', machine['claude_home'] / 'skills')
    assert os.path.samefile(profile / 'plugins', machine['claude_home'] / 'plugins')
    settings = json.loads((profile / 'settings.json').read_text())
    assert settings == {'theme': 'light', 'enabledPlugins': {'tool@market': True}}


def test__run__reads_plugin_settings_saved_with_a_bom(machine, capfd):
    # Arrange: Notepad and Windows PowerShell 5.1's `Set-Content -Encoding UTF8` write a BOM.
    (machine['profiles'] / 'work').mkdir()
    (machine['claude_home'] / 'settings.json').write_text(
        json.dumps({'enabledPlugins': {'tool@market': True}}), encoding='utf-8-sig'
    )

    # Act
    code = cc_run.run('work', [])

    # Assert
    assert code == 0
    settings = json.loads((machine['profiles'] / 'work' / 'settings.json').read_text(encoding='utf-8'))
    assert settings == {'enabledPlugins': {'tool@market': True}}


def test__share__leaves_a_real_directory_alone(machine):
    # Arrange
    profile = machine['profiles'] / 'work'
    (profile / 'skills').mkdir(parents=True)
    (profile / 'skills' / 'mine.md').write_text('keep')

    # Act
    cc_run.share(profile)

    # Assert
    assert (profile / 'skills' / 'mine.md').read_text() == 'keep'
    assert not os.path.samefile(profile / 'skills', machine['claude_home'] / 'skills')


def test__share__a_link_reaches_files_added_later(machine):
    # Arrange
    profile = machine['profiles'] / 'work'
    profile.mkdir()
    cc_run.share(profile)

    # Act
    (machine['claude_home'] / 'skills' / 'new-skill.md').write_text('hello')

    # Assert
    assert (profile / 'skills' / 'new-skill.md').read_text() == 'hello'


def test__list_profiles__skips_names_that_are_not_profiles(machine):
    # Arrange
    for folder in ('work', 'personal', 'bin', '_scratch', '.hidden'):
        (machine['profiles'] / folder).mkdir()

    # Act / Assert
    assert cc_run.list_profiles() == ['personal', 'work']


def test__add__creates_the_profile_and_starts_a_login(machine, capfd):
    # Act
    cc_run.add('work')

    # Assert
    assert (machine['profiles'] / 'work').is_dir()
    assert _seen(capfd)['args'] == ['auth', 'login']


@pytest.mark.parametrize('name', ['default', 'bin', '_x', '.x', 'a/b', 'x&calc', 'a%PATH%', 'work.', 'work '])
def test__add__rejects_unusable_names(machine, name):
    # Act / Assert
    with pytest.raises(cc_run.ProfileError, match='not a usable profile name'):
        cc_run.add(name)


def test__run__says_so_when_claude_is_not_installed(machine, monkeypatch):
    # Arrange
    (machine['profiles'] / 'work').mkdir()
    monkeypatch.setenv('PATH', str(machine['tmp'] / 'empty'))

    # Act / Assert
    with pytest.raises(cc_run.ProfileError, match='claude is not on PATH'):
        cc_run.run('work', [])


def test__run__drops_login_overrides_for_a_named_profile(machine, capfd, monkeypatch):
    # Arrange: any of these would run the profile on that credential instead of its own login.
    (machine['profiles'] / 'work').mkdir()
    for name, value in LOGIN_ENV.items():
        monkeypatch.setenv(name, value)

    # Act
    cc_run.run('work', ['-p', 'hi'])

    # Assert
    assert _seen(capfd)['login_env'] == []


def test__run__default_keeps_login_overrides(machine, capfd, monkeypatch):
    # Arrange
    for name, value in LOGIN_ENV.items():
        monkeypatch.setenv(name, value)

    # Act
    cc_run.run('default', ['--version'])

    # Assert
    assert _seen(capfd)['login_env'] == sorted(LOGIN_ENV)


def test__run__passes_cmd_syntax_through_to_claude_as_text(machine, capfd):
    # Arrange: through cmd.exe, & would start another command and %PATH% would expand.
    (machine['profiles'] / 'work').mkdir()
    args = ['-p', 'a" & echo pwned & "b', '%PATH%']

    # Act
    cc_run.run('work', args)

    # Assert
    assert _seen(capfd)['args'] == args


def test__claude_command__never_runs_a_claude_from_the_current_directory(windows, monkeypatch):
    # Arrange
    installed = windows / 'bin' / 'claude.exe'
    installed.parent.mkdir()
    installed.write_text('')
    monkeypatch.setenv('PATH', str(installed.parent))

    # Act
    command = cc_run.claude_command()

    # Assert
    assert command == [str(installed)]


def test__claude_command__skips_empty_and_relative_path_entries(windows, monkeypatch):
    # Arrange: each of these would resolve against the current directory.
    (windows / 'untrusted-repo' / 'bin').mkdir()
    (windows / 'untrusted-repo' / 'bin' / 'claude.exe').write_text('')
    installed = windows / 'bin' / 'claude.exe'
    installed.parent.mkdir()
    installed.write_text('')
    monkeypatch.setenv('PATH', os.pathsep.join(['', '.', 'bin', str(installed.parent)]))

    # Act
    command = cc_run.claude_command()

    # Assert
    assert command == [str(installed)]


@pytest.mark.parametrize('shim', [NPM_SHIM, OLD_NPM_SHIM], ids=['npm7+', 'npm6'])
def test__claude_command__runs_an_npm_shims_script_with_the_node_beside_it(windows, monkeypatch, shim):
    # Arrange
    npm = windows / 'npm'
    script = npm / 'node_modules' / '@anthropic-ai' / 'claude-code' / 'cli.js'
    script.parent.mkdir(parents=True)
    script.write_text('')
    (npm / 'node.exe').write_text('')
    (npm / 'claude.cmd').write_text(shim)
    monkeypatch.setenv('PATH', str(npm))

    # Act
    command = cc_run.claude_command()

    # Assert
    assert command == [str(npm / 'node.exe'), str(script)]


def test__claude_command__runs_an_npm_shims_script_with_node_from_path(windows, monkeypatch):
    # Arrange: no node.exe beside the shim, and the one in the current directory must not count.
    npm = windows / 'npm'
    script = npm / 'node_modules' / '@anthropic-ai' / 'claude-code' / 'cli.js'
    script.parent.mkdir(parents=True)
    script.write_text('')
    (npm / 'claude.cmd').write_text(NPM_SHIM)
    node = windows / 'nodejs' / 'node.exe'
    node.parent.mkdir()
    node.write_text('')
    monkeypatch.setenv('PATH', os.pathsep.join([str(npm), str(node.parent)]))

    # Act
    command = cc_run.claude_command()

    # Assert
    assert command == [str(node), str(script)]


def test__claude_command__runs_an_npm_shims_exe_directly(windows, monkeypatch):
    # Arrange
    npm = windows / 'npm'
    exe = npm / 'node_modules' / '@anthropic-ai' / 'claude-code' / 'bin' / 'claude.exe'
    exe.parent.mkdir(parents=True)
    exe.write_text('')
    (npm / 'claude.cmd').write_text(NPM_SHIM.replace('"%_prog%"  ', '').replace('cli.js', 'bin\\claude.exe'))
    monkeypatch.setenv('PATH', str(npm))

    # Act
    command = cc_run.claude_command()

    # Assert
    assert command == [str(exe)]


@pytest.mark.parametrize(
    'shim',
    ['@echo off\r\ncalc.exe %*\r\n', '@"%~dp0\\claude_stub.py" %*\r\n', NPM_SHIM],
    ids=['no-target', 'not-exe-or-js', 'missing-target'],
)
def test__claude_command__refuses_a_shim_it_cannot_read(windows, monkeypatch, shim):
    # Arrange: running it through cmd.exe instead would let argument text run commands.
    npm = windows / 'npm'
    npm.mkdir()
    (npm / 'claude_stub.py').write_text('')
    (npm / 'claude.cmd').write_text(shim)
    monkeypatch.setenv('PATH', str(npm))

    # Act / Assert
    with pytest.raises(cc_run.ProfileError, match='claude.cmd'):
        cc_run.claude_command()


def _seen(capfd) -> dict:
    return json.loads(capfd.readouterr().out.strip().splitlines()[-1])
