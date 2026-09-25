import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.skipif(shutil.which('zsh') is None, reason='the cc functions are zsh')

# Stands in for the real CLI so the tests see which config dir `cc` hands it.
CLAUDE_STUB = 'claude() { print -r -- "CONFIG=${CLAUDE_CONFIG_DIR:-none} ARGS=$*"; }'


@pytest.fixture
def home(tmp_path):
    (tmp_path / '.claude' / 'skills').mkdir(parents=True)
    (tmp_path / '.claude' / 'settings.json').write_text(
        json.dumps({'theme': 'dark', 'enabledPlugins': {'tool@market': True}})
    )
    (tmp_path / '.zshrc').write_text('# existing\n')
    _run(['bash', str(REPO / 'install.sh')], tmp_path)
    return tmp_path


def test__install__sources_the_functions_from_zshrc_once(home):
    # Act: a second install must not add a second line.
    _run(['bash', str(REPO / 'install.sh')], home)

    # Assert
    zshrc = (home / '.zshrc').read_text()
    assert zshrc.count('# claude-multi-account') == 1
    assert '"$HOME/.claude-profiles/profiles.zsh"' in zshrc
    assert (home / '.claude-profiles' / 'cc_use.py').is_file()


def test__install__keeps_a_hand_written_source_line(tmp_path):
    # Arrange: an existing setup that sources profiles.zsh without our marker.
    (tmp_path / '.claude').mkdir()
    (tmp_path / '.zshrc').write_text('source "$HOME/.claude-profiles/profiles.zsh"\n')

    # Act
    _run(['bash', str(REPO / 'install.sh')], tmp_path)

    # Assert
    assert (tmp_path / '.zshrc').read_text() == 'source "$HOME/.claude-profiles/profiles.zsh"\n'


def test__install__backs_up_a_locally_edited_script(home):
    # Arrange
    installed = home / '.claude-profiles' / 'usage_table.py'
    installed.write_text('# my edit\n')

    # Act
    _run(['bash', str(REPO / 'install.sh')], home)

    # Assert
    backups = list(installed.parent.glob('usage_table.py.bak-*'))
    assert [b.read_text() for b in backups] == ['# my edit\n']


def test__cc__runs_claude_with_the_profiles_config_dir(home):
    # Arrange
    (home / '.claude-profiles' / 'work').mkdir()

    # Act
    out = _zsh('cc work -p hi', home)

    # Assert
    assert out == f'CONFIG={home}/.claude-profiles/work ARGS=-p hi'


def test__cc__default_runs_claude_with_no_config_dir(home):
    # Act / Assert
    assert _zsh('cc default --version', home) == 'CONFIG=none ARGS=--version'


def test__cc__lists_only_real_profiles(home):
    # Arrange
    for name in ('work', 'personal', '_scratch', '.hidden'):
        (home / '.claude-profiles' / name).mkdir()

    # Act
    out = _zsh('cc', home, check=False)

    # Assert
    assert 'profiles: default personal work' in out


def test__cc__shares_skills_plugins_and_plugin_settings(home):
    # Arrange
    profile = home / '.claude-profiles' / 'work'
    profile.mkdir()
    (profile / 'settings.json').write_text(json.dumps({'theme': 'light'}))

    # Act
    _zsh('cc work', home)

    # Assert
    assert os.readlink(profile / 'skills') == str(home / '.claude' / 'skills')
    assert os.readlink(profile / 'plugins') == str(home / '.claude' / 'plugins')
    settings = json.loads((profile / 'settings.json').read_text())
    assert settings == {'theme': 'light', 'enabledPlugins': {'tool@market': True}}


def test__cc__refuses_the_profile_cc_use_has_loaded(home):
    # Arrange
    (home / '.claude-profiles' / 'work').mkdir()
    (home / '.claude-profiles' / '.loaded').write_text('work\n')

    # Act
    out = _zsh('cc work', home, check=False)

    # Assert
    assert 'loaded into the default login' in out
    assert 'CONFIG=' not in out


def test__uninstall__removes_the_scripts_and_keeps_the_profiles(home):
    # Arrange
    (home / '.claude-profiles' / 'work').mkdir()

    # Act
    _run(['bash', str(REPO / 'install.sh'), '--uninstall'], home)

    # Assert
    assert '# claude-multi-account' not in (home / '.zshrc').read_text()
    assert not (home / '.claude-profiles' / 'profiles.zsh').exists()
    assert (home / '.claude-profiles' / 'work').is_dir()


def test__uninstall__keeps_a_symlinked_zshrc_a_symlink(home):
    # Arrange: ~/.zshrc lives in a dotfiles repo.
    dotfiles = home / 'dotfiles'
    dotfiles.mkdir()
    real = dotfiles / 'zshrc'
    real.write_text((home / '.zshrc').read_text())
    (home / '.zshrc').unlink()
    (home / '.zshrc').symlink_to(real)

    # Act
    _run(['bash', str(REPO / 'install.sh'), '--uninstall'], home)

    # Assert
    assert (home / '.zshrc').is_symlink()
    assert '# claude-multi-account' not in real.read_text()
    assert '# existing' in real.read_text()


def test__install__custom_location_exports_it_for_the_functions(tmp_path):
    # Arrange
    (tmp_path / '.claude').mkdir()
    custom = tmp_path / 'elsewhere'

    # Act
    _run(['bash', str(REPO / 'install.sh')], tmp_path, extra_env={'CLAUDE_PROFILES': str(custom)})
    (custom / 'work').mkdir()
    out = _zsh('cc work', tmp_path)

    # Assert
    assert out == f'CONFIG={custom}/work ARGS='


def test__uninstall__refuses_while_a_profile_is_loaded(home):
    # Arrange
    (home / '.claude-profiles' / '.loaded').write_text('work\n')

    # Act
    done = _run(['bash', str(REPO / 'install.sh'), '--uninstall'], home, check=False)

    # Assert
    assert done.returncode == 1
    assert (home / '.claude-profiles' / 'profiles.zsh').exists()


def _zsh(command: str, home: Path, check: bool = True) -> str:
    script = f'source "$HOME/.zshrc"; {CLAUDE_STUB}; {command}'
    return _run(['zsh', '-c', script], home, check=check).stdout.strip()


def _run(
    args: list[str], home: Path, check: bool = True, extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k not in ('CLAUDE_PROFILES', 'CLAUDE_CONFIG_DIR', 'ZDOTDIR')}
    env.update(HOME=str(home), SHELL='/bin/zsh', **(extra_env or {}))
    done = subprocess.run(args, env=env, capture_output=True, text=True, timeout=30)
    if check:
        assert done.returncode == 0, done.stdout + done.stderr
    done.stdout = done.stdout + done.stderr
    return done
