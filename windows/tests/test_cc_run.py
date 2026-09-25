import json
import os
import sys

import pytest

import cc_run

STUB = """import json, os, sys
stdin = '' if sys.stdin is None or sys.stdin.isatty() else sys.stdin.read()
print(json.dumps({'config': os.environ.get('CLAUDE_CONFIG_DIR'), 'args': sys.argv[1:], 'stdin': stdin}))
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
    (bin_dir / 'claude_stub.py').write_text(STUB)
    if sys.platform == 'win32':
        (bin_dir / 'claude.cmd').write_text(f'@"{sys.executable}" "%~dp0claude_stub.py" %*\r\n')
    else:
        stub = bin_dir / 'claude'
        stub.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{bin_dir / "claude_stub.py"}" "$@"\n')
        stub.chmod(0o755)
    monkeypatch.setenv('PATH', str(bin_dir) + os.pathsep + os.environ['PATH'])
    return {'profiles': profiles, 'claude_home': claude_home, 'tmp': tmp_path}


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


def test__run__refuses_the_profile_cc_use_has_loaded(machine, capfd):
    # Arrange
    (machine['profiles'] / 'work').mkdir()
    (machine['profiles'] / '.loaded').write_text('work\n')

    # Act / Assert
    with pytest.raises(cc_run.ProfileError, match='loaded into the default login'):
        cc_run.run('work', [])
    assert capfd.readouterr().out == ''


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


@pytest.mark.parametrize('name', ['default', 'bin', '_x', '.x', 'a/b'])
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


def _seen(capfd) -> dict:
    return json.loads(capfd.readouterr().out.strip().splitlines()[-1])
