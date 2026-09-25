import json
import os
import time

import pytest

import cc_use

HOME_SECRET = json.dumps({'claudeAiOauth': {'accessToken': 'home-at', 'refreshToken': 'home-rt'}})
WORK_SECRET = json.dumps({'claudeAiOauth': {'accessToken': 'work-at', 'refreshToken': 'work-rt'}})
HOME_ACCOUNT = {'accountUuid': 'uuid-home', 'emailAddress': 'me@example.com'}
WORK_ACCOUNT = {'accountUuid': 'uuid-work', 'emailAddress': 'work@example.com'}


@pytest.fixture
def machine(tmp_path, monkeypatch):
    """A fake machine: one 'work' profile and the user's own login in the default slot, all as files."""
    profiles = tmp_path / 'profiles'
    work = profiles / 'work'
    work.mkdir(parents=True)
    (work / '.claude.json').write_text(json.dumps({'oauthAccount': WORK_ACCOUNT}))
    (work / '.credentials.json').write_text(WORK_SECRET)
    (tmp_path / '.claude').mkdir()
    default_credentials = tmp_path / '.claude' / '.credentials.json'
    default_credentials.write_text(HOME_SECRET)
    global_config = tmp_path / '.claude.json'
    global_config.write_text(json.dumps({'oauthAccount': HOME_ACCOUNT, 'numStartups': 7}))

    monkeypatch.setattr(cc_use, 'PROFILES_DIR', profiles)
    monkeypatch.setattr(cc_use, 'LOADED_FILE', profiles / '.loaded')
    monkeypatch.setattr(cc_use, 'HOME_ACCOUNT_FILE', profiles / '.home-account.json')
    monkeypatch.setattr(cc_use, 'HOME_STASH_FILE', profiles / '.home-credentials.json')
    monkeypatch.setattr(cc_use, 'GLOBAL_CONFIG', global_config)
    monkeypatch.setattr(cc_use, 'DEFAULT_CREDENTIALS', default_credentials)
    monkeypatch.setattr(cc_use, 'OAUTH_REFRESH_LOCK', tmp_path / '.claude' / '.oauth_refresh.lock')
    monkeypatch.setattr(cc_use, 'LEGACY_CREDENTIALS_LOCK', tmp_path / '.claude.lock')
    monkeypatch.setattr(cc_use, 'CONFIG_LOCK', tmp_path / '.claude.json.lock')

    identities = {'home-at': 'uuid-home', 'work-at': 'uuid-work'}
    monkeypatch.setattr(
        cc_use,
        '_fetch_identity',
        lambda token: {'uuid': identities[token], 'email': token} if token in identities else None,
    )
    return {
        'profiles': profiles,
        'default': default_credentials,
        'global_config': global_config,
        'stash': profiles / '.home-credentials.json',
        'identities': identities,
    }


def test__load__puts_the_profile_login_in_the_default_slot(machine):
    # Act
    cc_use.load('work')

    # Assert
    assert machine['default'].read_text() == WORK_SECRET
    assert machine['stash'].read_text() == HOME_SECRET
    config = json.loads(machine['global_config'].read_text())
    assert config['oauthAccount'] == WORK_ACCOUNT
    assert config['numStartups'] == 7
    assert cc_use.loaded_profile() == 'work'


def test__load__default_writes_a_refreshed_token_back_to_its_profile(machine):
    # Arrange: work is loaded, then a session refreshes (rotates) its token in the slot.
    cc_use.load('work')
    rotated = json.dumps({'claudeAiOauth': {'accessToken': 'work-at-2', 'refreshToken': 'work-rt-2'}})
    machine['default'].write_text(rotated)
    machine['identities']['work-at-2'] = 'uuid-work'

    # Act
    cc_use.load(None)

    # Assert
    assert (machine['profiles'] / 'work' / '.credentials.json').read_text() == rotated
    assert machine['default'].read_text() == HOME_SECRET
    assert json.loads(machine['global_config'].read_text())['oauthAccount'] == HOME_ACCOUNT
    assert cc_use.loaded_profile() is None


def test__load__switching_between_profiles_leaves_the_stashed_login_alone(machine):
    # Arrange
    other = machine['profiles'] / 'other'
    other.mkdir()
    other_secret = json.dumps({'claudeAiOauth': {'accessToken': 'other-at', 'refreshToken': 'other-rt'}})
    (other / '.claude.json').write_text(json.dumps({'oauthAccount': {'accountUuid': 'uuid-other'}}))
    (other / '.credentials.json').write_text(other_secret)
    machine['identities']['other-at'] = 'uuid-other'
    cc_use.load('work')

    # Act
    cc_use.load('other')

    # Assert
    assert machine['default'].read_text() == other_secret
    assert (machine['profiles'] / 'work' / '.credentials.json').read_text() == WORK_SECRET
    assert machine['stash'].read_text() == HOME_SECRET


@pytest.mark.parametrize('name', ['bin', 'default', '.hidden', '_scratch', 'missing'])
def test__load__rejects_names_that_are_not_profiles(machine, name):
    # Arrange
    for folder in ('bin', '_scratch', '.hidden'):
        (machine['profiles'] / folder).mkdir()

    # Act / Assert
    with pytest.raises(cc_use.SwapError, match='no such profile'):
        cc_use.load(name)


def test__load__names_the_fix_for_a_profile_that_never_logged_in(machine):
    # Arrange
    (machine['profiles'] / 'fresh').mkdir()

    # Act / Assert
    with pytest.raises(cc_use.SwapError, match=r'fresh is not logged in \(cc-login fresh\)'):
        cc_use.load('fresh')


def test__load__refuses_a_profile_with_a_running_session(machine):
    # Arrange: Claude Code keeps sessions/<pid>.json for each live session in a config dir.
    sessions = machine['profiles'] / 'work' / 'sessions'
    sessions.mkdir()
    (sessions / f'{os.getpid()}.json').write_text('{}')

    # Act / Assert
    with pytest.raises(cc_use.SwapError, match='running session'):
        cc_use.load('work')
    assert machine['default'].read_text() == HOME_SECRET


def test__load__ignores_session_files_of_dead_processes(machine):
    # Arrange
    sessions = machine['profiles'] / 'work' / 'sessions'
    sessions.mkdir()
    (sessions / '999999.json').write_text('{}')

    # Act
    cc_use.load('work')

    # Assert
    assert cc_use.loaded_profile() == 'work'


def test__load__refuses_when_the_slot_holds_an_unexpected_account(machine):
    # Arrange: work is recorded as loaded, but something wrote the user's own login back.
    cc_use.load('work')
    machine['default'].write_text(HOME_SECRET)

    # Act / Assert
    with pytest.raises(cc_use.SwapError, match='cc-use recorded work'):
        cc_use.load(None)
    assert (machine['profiles'] / 'work' / '.credentials.json').read_text() == WORK_SECRET


def test__load__reads_a_global_config_saved_with_a_bom(machine):
    # Arrange: Notepad and Windows PowerShell 5.1's `Set-Content -Encoding UTF8` write a BOM.
    machine['global_config'].write_text(json.dumps({'oauthAccount': HOME_ACCOUNT}), encoding='utf-8-sig')

    # Act
    cc_use.load('work')

    # Assert
    assert json.loads(machine['global_config'].read_text(encoding='utf-8'))['oauthAccount'] == WORK_ACCOUNT


def test__verify_owner__reads_credentials_saved_with_a_bom(machine):
    # Arrange: the slot's login is work's own, but its file starts with a BOM.
    current = '\ufeff' + WORK_SECRET

    # Act / Assert: no exception, the identity check sees work's access token
    cc_use._verify_owner(current, 'work')


def test__load__already_loaded_is_a_no_op(machine):
    # Arrange
    cc_use.load('work')

    # Act / Assert
    assert 'already' in cc_use.load('work')


def test__verify_owner__offline_accepts_an_unchanged_login(machine):
    # Arrange: identity cannot be resolved, but the refresh token matches the stored copy.
    machine['identities'].clear()

    # Act / Assert: no exception
    cc_use._verify_owner(WORK_SECRET, 'work')


def test__verify_owner__offline_refuses_a_changed_login(machine):
    # Arrange
    machine['identities'].clear()
    changed = json.dumps({'claudeAiOauth': {'accessToken': 'x', 'refreshToken': 'other'}})

    # Act / Assert
    with pytest.raises(cc_use.SwapError, match='could not confirm'):
        cc_use._verify_owner(changed, 'work')


def test__forget__deletes_the_stashed_login_and_its_record(machine):
    # Arrange: a round trip leaves a stale copy of the user's login in the stash.
    cc_use.load('work')
    cc_use.load(None)

    # Act
    cc_use.forget()

    # Assert
    assert not machine['stash'].exists()
    assert not (machine['profiles'] / '.home-account.json').exists()
    assert machine['default'].read_text() == HOME_SECRET


def test__forget__refuses_while_a_profile_is_loaded(machine):
    # Arrange: while work is loaded, the stash holds the only copy of the user's login.
    cc_use.load('work')

    # Act / Assert
    with pytest.raises(cc_use.SwapError, match='cc-use default'):
        cc_use.forget()
    assert machine['stash'].read_text() == HOME_SECRET


def test__replace__retries_while_another_program_holds_the_file(tmp_path, monkeypatch):
    # Arrange: the first two renames fail the way Windows fails them when a file is open.
    source, destination = tmp_path / 'new', tmp_path / 'target'
    source.write_text('new')
    real_replace, calls = os.replace, []

    def flaky_replace(src, dst):
        calls.append(src)
        if len(calls) < 3:
            raise PermissionError('in use')
        real_replace(src, dst)

    monkeypatch.setattr(cc_use.os, 'replace', flaky_replace)
    monkeypatch.setattr(cc_use.time, 'sleep', lambda _: None)

    # Act
    cc_use._replace(str(source), destination)

    # Assert
    assert destination.read_text() == 'new'
    assert len(calls) == 3


def test__alive__is_true_for_this_process():
    # Act / Assert: on Windows this goes through OpenProcess, never os.kill.
    assert cc_use._alive(os.getpid())


def test__alive__is_false_for_a_process_that_does_not_exist():
    # Act / Assert
    assert not cc_use._alive(999999)


def test__lock_dir__takes_over_a_stale_lock(tmp_path):
    # Arrange: a lock directory whose holder stopped touching it long ago.
    lock = tmp_path / 'x.lock'
    lock.mkdir()
    old = time.time() - 120
    os.utime(lock, (old, old))

    # Act
    with cc_use._lock_dir(lock, stale_s=60):
        held = lock.is_dir()

    # Assert
    assert held
    assert not lock.exists()


def test__lock_dir__gives_up_on_a_live_lock(tmp_path, monkeypatch):
    # Arrange
    monkeypatch.setattr(cc_use, 'LOCK_TIMEOUT_S', 0.3)
    lock = tmp_path / 'x.lock'
    lock.mkdir()

    # Act / Assert
    with pytest.raises(cc_use.SwapError, match='stayed held'):
        with cc_use._lock_dir(lock, stale_s=60):
            pass
    assert lock.is_dir()
