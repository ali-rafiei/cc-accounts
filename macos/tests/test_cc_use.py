import hashlib
import json
import os
import time
import unicodedata

import pytest

import cc_use

HOME_SECRET = json.dumps({'claudeAiOauth': {'accessToken': 'home-at', 'refreshToken': 'home-rt'}})
WORK_SECRET = json.dumps({'claudeAiOauth': {'accessToken': 'work-at', 'refreshToken': 'work-rt'}})
HOME_ACCOUNT = {'accountUuid': 'uuid-home', 'emailAddress': 'me@example.com'}
WORK_ACCOUNT = {'accountUuid': 'uuid-work', 'emailAddress': 'work@example.com'}


@pytest.fixture
def machine(tmp_path, monkeypatch):
    """A fake machine: one 'work' profile, the user's own login in the default slot, a dict Keychain."""
    profiles = tmp_path / 'profiles'
    (profiles / 'work').mkdir(parents=True)
    (profiles / 'work' / '.claude.json').write_text(json.dumps({'oauthAccount': WORK_ACCOUNT}))
    global_config = tmp_path / '.claude.json'
    global_config.write_text(json.dumps({'oauthAccount': HOME_ACCOUNT, 'numStartups': 7}))
    (tmp_path / '.claude').mkdir()

    monkeypatch.setattr(cc_use, 'PROFILES_DIR', profiles)
    monkeypatch.setattr(cc_use, 'LOADED_FILE', profiles / '.loaded')
    monkeypatch.setattr(cc_use, 'HOME_ACCOUNT_FILE', profiles / '.home-account.json')
    monkeypatch.setattr(cc_use, 'GLOBAL_CONFIG', global_config)
    monkeypatch.setattr(cc_use, 'OAUTH_REFRESH_LOCK', tmp_path / '.claude' / '.oauth_refresh.lock')
    monkeypatch.setattr(cc_use, 'LEGACY_CREDENTIALS_LOCK', tmp_path / '.claude.lock')
    monkeypatch.setattr(cc_use, 'CONFIG_LOCK', tmp_path / '.claude.json.lock')

    keychain = {
        cc_use.DEFAULT_SERVICE: HOME_SECRET,
        cc_use._owner_service('work'): WORK_SECRET,
    }
    monkeypatch.setattr(cc_use, '_keychain_get', keychain.get)
    monkeypatch.setattr(cc_use, '_keychain_set', keychain.__setitem__)
    monkeypatch.setattr(cc_use, '_keychain_delete', lambda service: keychain.pop(service, None))
    identities = {'home-at': 'uuid-home', 'work-at': 'uuid-work'}
    monkeypatch.setattr(
        cc_use,
        '_fetch_identity',
        lambda token: {'uuid': identities[token], 'email': token} if token in identities else None,
    )
    return {'keychain': keychain, 'profiles': profiles, 'global_config': global_config, 'identities': identities}


def test__owner_service__matches_claude_codes_per_config_dir_item(machine):
    # Arrange
    expected_digest = hashlib.sha256(str(machine['profiles'] / 'work').encode()).hexdigest()[:8]

    # Act
    service = cc_use._owner_service('work')

    # Assert
    assert service == f'Claude Code-credentials-{expected_digest}'


def test__owner_service__default_login_goes_to_the_stash(machine):
    # Act / Assert
    assert cc_use._owner_service(None) == cc_use.HOME_STASH_SERVICE


@pytest.mark.parametrize('name', ['bin', 'default', '.hidden', '_scratch', 'missing', '', 'work/', 'work/.'])
def test__load__rejects_names_that_are_not_profiles(machine, name):
    # Arrange
    (machine['profiles'] / 'bin').mkdir()
    (machine['profiles'] / '_scratch').mkdir()
    (machine['profiles'] / '.hidden').mkdir()

    # Act / Assert
    with pytest.raises(cc_use.SwapError, match='no such profile'):
        cc_use.load(name)


def test__load__puts_the_profile_login_in_the_default_slot(machine):
    # Act
    cc_use.load('work')

    # Assert
    keychain = machine['keychain']
    assert keychain[cc_use.DEFAULT_SERVICE] == WORK_SECRET
    assert keychain[cc_use.HOME_STASH_SERVICE] == HOME_SECRET
    config = json.loads(machine['global_config'].read_text())
    assert config['oauthAccount'] == WORK_ACCOUNT
    assert config['numStartups'] == 7
    assert cc_use.loaded_profile() == 'work'


def test__load__default_writes_a_refreshed_token_back_to_its_profile(machine):
    # Arrange: work is loaded, then a session refreshes (rotates) its token in the slot.
    cc_use.load('work')
    rotated = json.dumps({'claudeAiOauth': {'accessToken': 'work-at-2', 'refreshToken': 'work-rt-2'}})
    machine['keychain'][cc_use.DEFAULT_SERVICE] = rotated
    machine['identities']['work-at-2'] = 'uuid-work'

    # Act
    cc_use.load(None)

    # Assert
    keychain = machine['keychain']
    assert keychain[cc_use._owner_service('work')] == rotated
    assert keychain[cc_use.DEFAULT_SERVICE] == HOME_SECRET
    assert json.loads(machine['global_config'].read_text())['oauthAccount'] == HOME_ACCOUNT
    assert cc_use.loaded_profile() is None


def test__load__switching_between_profiles_leaves_the_stashed_login_alone(machine):
    # Arrange
    other_secret = json.dumps({'claudeAiOauth': {'accessToken': 'other-at', 'refreshToken': 'other-rt'}})
    other_account = {'accountUuid': 'uuid-other', 'emailAddress': 'other@example.com'}
    (machine['profiles'] / 'other').mkdir()
    (machine['profiles'] / 'other' / '.claude.json').write_text(json.dumps({'oauthAccount': other_account}))
    machine['keychain'][cc_use._owner_service('other')] = other_secret
    machine['identities']['other-at'] = 'uuid-other'
    cc_use.load('work')

    # Act
    cc_use.load('other')

    # Assert
    keychain = machine['keychain']
    assert keychain[cc_use.DEFAULT_SERVICE] == other_secret
    assert keychain[cc_use._owner_service('work')] == WORK_SECRET
    assert keychain[cc_use.HOME_STASH_SERVICE] == HOME_SECRET
    assert cc_use.loaded_profile() == 'other'


def test__load__names_the_fix_for_a_profile_that_never_logged_in(machine):
    # Arrange
    (machine['profiles'] / 'fresh').mkdir()

    # Act / Assert
    with pytest.raises(cc_use.SwapError, match=r'fresh is not logged in \(cc-login fresh\)'):
        cc_use.load('fresh')


def test__forget__deletes_the_stashed_login_and_its_record(machine):
    # Arrange: a round trip leaves a stale copy of the user's login in the stash.
    cc_use.load('work')
    cc_use.load(None)

    # Act
    cc_use.forget()

    # Assert
    assert cc_use.HOME_STASH_SERVICE not in machine['keychain']
    assert not (machine['profiles'] / '.home-account.json').exists()
    assert machine['keychain'][cc_use.DEFAULT_SERVICE] == HOME_SECRET


def test__forget__refuses_while_a_profile_is_loaded(machine):
    # Arrange: while work is loaded, the stash holds the only copy of the user's login.
    cc_use.load('work')

    # Act / Assert
    with pytest.raises(cc_use.SwapError, match='cc-use default'):
        cc_use.forget()
    assert machine['keychain'][cc_use.HOME_STASH_SERVICE] == HOME_SECRET


def test__load__already_loaded_is_a_no_op(machine):
    # Arrange
    cc_use.load('work')

    # Act
    message = cc_use.load('work')

    # Assert
    assert 'already' in message


def test__load__refuses_a_profile_with_a_running_session(machine):
    # Arrange: Claude Code keeps sessions/<pid>.json for each live session in a config dir.
    sessions = machine['profiles'] / 'work' / 'sessions'
    sessions.mkdir()
    (sessions / f'{os.getpid()}.json').write_text('{}')

    # Act / Assert
    with pytest.raises(cc_use.SwapError, match='running session'):
        cc_use.load('work')
    assert machine['keychain'][cc_use.DEFAULT_SERVICE] == HOME_SECRET


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
    machine['keychain'][cc_use.DEFAULT_SERVICE] = HOME_SECRET

    # Act / Assert
    with pytest.raises(cc_use.SwapError, match='cc-use recorded work'):
        cc_use.load(None)
    assert machine['keychain'][cc_use._owner_service('work')] == WORK_SECRET


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


def test__load__recorded_name_matches_the_one_the_shell_guard_compares(machine):
    # Arrange: `cc work` refuses while .loaded says exactly "work"; "work/" names the same dir.
    # Act
    with pytest.raises(cc_use.SwapError, match='no such profile'):
        cc_use.load('work/')

    # Assert
    assert machine['keychain'][cc_use.DEFAULT_SERVICE] == HOME_SECRET
    assert cc_use.loaded_profile() is None


def test__owner_service__hashes_the_nfc_form_like_claude_code(machine):
    # Arrange: Claude Code hashes CLAUDE_CONFIG_DIR after .normalize('NFC'); this name is typed decomposed.
    decomposed = 'cafe\u0301'
    composed_dir = unicodedata.normalize('NFC', str(machine['profiles'] / decomposed))
    expected_digest = hashlib.sha256(composed_dir.encode()).hexdigest()[:8]

    # Act
    service = cc_use._owner_service(decomposed)

    # Assert
    assert service == f'Claude Code-credentials-{expected_digest}'


@pytest.mark.parametrize('user', ['jane doe', 'j\u00f6rg', 'corp\\jane', 'a"b'])
def test__keychain_account__falls_back_like_claude_code_for_an_unusual_user(monkeypatch, user):
    # Arrange: Claude Code files its items under "claude-code-user" when $USER is not [a-zA-Z0-9._-]+.
    monkeypatch.setenv('USER', user)

    # Act
    account = cc_use._keychain_account()

    # Assert
    assert account == 'claude-code-user'


def test__keychain_account__uses_a_plain_user_name_as_is(monkeypatch):
    # Arrange
    monkeypatch.setenv('USER', 'jane.doe_2-x')

    # Act / Assert
    assert cc_use._keychain_account() == 'jane.doe_2-x'


def test__load__failed_config_write_puts_the_slot_back(machine, monkeypatch):
    # Arrange: writing ~/.claude.json fails after the slot already holds the incoming login.
    real_write_json = cc_use._write_json

    def write_json(path, data):
        if path == machine['global_config']:
            raise OSError(28, 'No space left on device')
        real_write_json(path, data)

    monkeypatch.setattr(cc_use, '_write_json', write_json)

    # Act
    with pytest.raises(OSError, match='No space left'):
        cc_use.load('work')

    # Assert: the slot, the config and .loaded all still agree on the user's own login.
    assert machine['keychain'][cc_use.DEFAULT_SERVICE] == HOME_SECRET
    assert json.loads(machine['global_config'].read_text())['oauthAccount'] == HOME_ACCOUNT
    assert cc_use.loaded_profile() is None


def test__load__failed_loaded_write_puts_the_slot_and_config_back(machine, monkeypatch):
    # Arrange: recording .loaded is the last write, so the slot and the config already changed.
    def write_loaded(profile):
        if profile is not None:
            raise KeyboardInterrupt

    monkeypatch.setattr(cc_use, '_write_loaded', write_loaded)

    # Act
    with pytest.raises(KeyboardInterrupt):
        cc_use.load('work')

    # Assert
    assert machine['keychain'][cc_use.DEFAULT_SERVICE] == HOME_SECRET
    assert json.loads(machine['global_config'].read_text())['oauthAccount'] == HOME_ACCOUNT
