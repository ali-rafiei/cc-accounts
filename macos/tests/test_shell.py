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


def test__cc__runs_under_the_users_nounset_option(home):
    # Act: a bare `cc` reads $1, which nounset turns into an error.
    out = _zsh('setopt nounset; cc', home, check=False)

    # Assert
    assert 'usage: cc' in out
    assert 'parameter not set' not in out


def test__cc__lists_profiles_under_the_users_no_bare_glob_qual_option(home):
    # Arrange
    (home / '.claude-profiles' / 'work').mkdir()

    # Act
    out = _zsh('setopt no_bare_glob_qual; cc', home, check=False)

    # Assert
    assert 'profiles: default work' in out


def test__cc__default_drops_an_inherited_config_dir(home):
    # Arrange: a shell started from inside a profile's session inherits its config dir.
    inherited = {'CLAUDE_CONFIG_DIR': str(home / '.claude-profiles' / 'work')}

    # Act
    out = _zsh('cc default -p hi', home, extra_env=inherited)

    # Assert
    assert out == 'CONFIG=none ARGS=-p hi'


def test__cc__refuses_the_loaded_profile_named_with_a_trailing_slash(home):
    # Arrange
    (home / '.claude-profiles' / 'work').mkdir()
    (home / '.claude-profiles' / '.loaded').write_text('work\n')

    # Act
    out = _zsh('cc work/', home, check=False)

    # Assert
    assert 'CONFIG=' not in out


def test__cc__returns_claudes_exit_status(home):
    # Arrange
    (home / '.claude-profiles' / 'work').mkdir()

    # Act
    out = _zsh('claude() { return 7; }; cc work -p hi; print rc=$?', home)

    # Assert
    assert out == 'rc=7'


def test__ccusage_all__raw_keeps_a_profile_name_with_spaces_whole(home):
    # Arrange
    (home / '.claude-profiles' / 'my work').mkdir()

    # Act
    out = _zsh('ccusage-all --raw', home)

    # Assert
    assert f'CONFIG={home}/.claude-profiles/my work ARGS=-p /usage' in out
    assert f'CONFIG={home}/.claude-profiles/my ARGS' not in out


def test__ccusage_all__raw_default_drops_an_inherited_config_dir(home):
    # Arrange
    inherited = {'CLAUDE_CONFIG_DIR': str(home / '.claude-profiles' / 'work')}

    # Act
    out = _zsh('ccusage-all --raw', home, extra_env=inherited)

    # Assert
    assert out.splitlines()[:2] == ['=== default ===', 'CONFIG=none ARGS=-p /usage']


def test__install__relative_location_is_made_absolute(tmp_path):
    # Arrange
    (tmp_path / '.claude').mkdir()
    cwd = tmp_path / 'cwd'
    cwd.mkdir()

    # Act
    _run(['bash', str(REPO / 'install.sh')], tmp_path, extra_env={'CLAUDE_PROFILES': 'profs'}, cwd=cwd)
    (cwd / 'profs' / 'work').mkdir()
    out = _zsh('cc work', tmp_path)

    # Assert
    assert out == f'CONFIG={cwd}/profs/work ARGS='


def test__install__expands_a_quoted_tilde_in_the_location(tmp_path):
    # Arrange
    (tmp_path / '.claude').mkdir()
    cwd = tmp_path / 'cwd'
    cwd.mkdir()

    # Act
    _run(['bash', str(REPO / 'install.sh')], tmp_path, extra_env={'CLAUDE_PROFILES': '~/elsewhere'}, cwd=cwd)

    # Assert
    assert (tmp_path / 'elsewhere' / 'profiles.zsh').is_file()
    assert not (cwd / '~').exists()


def test__install__ignores_an_exported_cdpath(tmp_path):
    # Arrange
    (tmp_path / '.claude').mkdir()

    # Act: a relative script path makes `cd` consult CDPATH, which then prints the directory.
    _run(['bash', 'macos/install.sh'], tmp_path, extra_env={'CDPATH': '.'}, cwd=REPO.parent)

    # Assert
    assert (tmp_path / '.claude-profiles' / 'profiles.zsh').is_file()


def test__uninstall__warns_about_a_hand_written_source_line(tmp_path):
    # Arrange
    (tmp_path / '.claude').mkdir()
    (tmp_path / '.zshrc').write_text('source ~/.claude-profiles/profiles.zsh\n')
    _run(['bash', str(REPO / 'install.sh')], tmp_path)

    # Act
    out = _run(['bash', str(REPO / 'install.sh'), '--uninstall'], tmp_path).stdout

    # Assert: the line now sources a deleted file on every shell start, so say so.
    assert 'source ~/.claude-profiles/profiles.zsh' in out


@pytest.mark.parametrize(('browser', 'flag'), [('firefox', '--private-window'), ('microsoft edge', '--inprivate')])
def test__open__lowercase_browser_name_still_gets_its_private_flag(tmp_path, browser, flag):
    # Arrange
    shim = _open_shim(tmp_path)

    # Act
    out = _run(['sh', str(shim), 'https://example.com/oauth'], tmp_path, extra_env={'CC_LOGIN_BROWSER': browser})

    # Assert
    args = out.stdout.splitlines()
    assert args[:3] == ['-na', browser, '--args']
    assert flag in args
    assert args[-1] == 'https://example.com/oauth'


@pytest.mark.parametrize(('browser', 'profile_flag'), [('Google Chrome', '--user-data-dir='), ('Firefox', '-profile')])
def test__open__gives_each_login_its_own_browser_profile(tmp_path, browser, profile_flag):
    # Arrange: private windows share one cookie jar, so only a fresh profile is a clean login.
    shim = _open_shim(tmp_path)
    env = {'CC_LOGIN_BROWSER': browser, 'TMPDIR': str(tmp_path)}

    # Act
    runs = [_run(['sh', str(shim), 'https://example.com/oauth'], tmp_path, extra_env=env) for _ in range(2)]

    # Assert
    profiles = [_profile_dir(run.stdout.splitlines(), profile_flag) for run in runs]
    assert profiles[0] != profiles[1]
    assert all(Path(profile).is_dir() for profile in profiles)


def test__open__passes_a_non_url_through_to_the_real_open(tmp_path):
    # Arrange
    shim = _open_shim(tmp_path)

    # Act
    out = _run(['sh', str(shim), '-R', '/Applications'], tmp_path)

    # Assert
    assert out.stdout.splitlines() == ['-R', '/Applications']


def test__install__quotes_a_custom_location_with_shell_metacharacters(tmp_path):
    # Arrange
    (tmp_path / '.claude').mkdir()
    custom = tmp_path / 'it\'s "odd" $(touch pwned) `touch pwned2`'

    # Act
    _run(['bash', str(REPO / 'install.sh')], tmp_path, extra_env={'CLAUDE_PROFILES': str(custom)})
    (custom / 'work').mkdir()
    out = _zsh('cc work', tmp_path)

    # Assert
    assert out == f'CONFIG={custom}/work ARGS='
    assert not (tmp_path / 'pwned').exists()
    assert not (tmp_path / 'pwned2').exists()


def test__install__adds_the_line_when_zshrc_only_has_it_commented_out(tmp_path):
    # Arrange
    (tmp_path / '.claude').mkdir()
    (tmp_path / '.zshrc').write_text('# source ~/.claude-profiles/profiles.zsh\n')

    # Act
    _run(['bash', str(REPO / 'install.sh')], tmp_path)

    # Assert
    assert '# claude-multi-account' in (tmp_path / '.zshrc').read_text()


def _zsh(command: str, home: Path, check: bool = True, extra_env: dict[str, str] | None = None) -> str:
    script = f'source "$HOME/.zshrc"; {CLAUDE_STUB}; {command}'
    return _run(['zsh', '-c', script], home, check=check, extra_env=extra_env).stdout.strip()


def _run(
    args: list[str],
    home: Path,
    check: bool = True,
    extra_env: dict[str, str] | None = None,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k not in ('CLAUDE_PROFILES', 'CLAUDE_CONFIG_DIR', 'ZDOTDIR')}
    env.update(HOME=str(home), SHELL='/bin/zsh', **(extra_env or {}))
    done = subprocess.run(args, env=env, capture_output=True, text=True, timeout=30, cwd=cwd or home)
    if check:
        assert done.returncode == 0, done.stdout + done.stderr
    done.stdout = done.stdout + done.stderr
    return done


def _open_shim(tmp_path: Path) -> Path:
    fake_open = tmp_path / 'fake-open'
    fake_open.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
    fake_open.chmod(0o755)
    shim = tmp_path / 'open'
    shim.write_text((REPO / 'scripts' / 'bin' / 'open').read_text().replace('/usr/bin/open', str(fake_open)))
    return shim


def _profile_dir(args: list[str], profile_flag: str) -> str:
    if profile_flag.endswith('='):
        return next(arg.removeprefix(profile_flag) for arg in args if arg.startswith(profile_flag))
    return args[args.index(profile_flag) + 1]
