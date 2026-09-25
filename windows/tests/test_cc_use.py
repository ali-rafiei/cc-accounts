import http.client
import http.server
import json
import os
import threading
import time
from contextlib import contextmanager

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


@pytest.mark.parametrize('name', ['bin', 'default', '.hidden', '_scratch', 'missing', '', 'work/', 'work\\', 'work/.'])
def test__load__rejects_names_that_are_not_profiles(machine, name):
    # Arrange
    for folder in ('bin', '_scratch', '.hidden'):
        (machine['profiles'] / folder).mkdir()

    # Act / Assert
    with pytest.raises(cc_use.SwapError, match='no such profile'):
        cc_use.load(name)


def test__load__records_the_profile_under_its_own_spelling(machine):
    # Act: Windows opens work's folder for WORK too.
    cc_use.load('WORK')

    # Assert
    assert cc_use.loaded_profile() == 'work'
    assert 'already' in cc_use.load('work')


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


def test__load__a_failed_config_write_leaves_the_slot_as_it_was(machine, monkeypatch):
    # Arrange: another program holds ~/.claude.json open until the rename retries give up, once.
    original_config = machine['global_config'].read_text()
    real_replace, failures = cc_use._replace, []

    def config_stays_locked(source, destination):
        if destination == machine['global_config'] and not failures:
            failures.append(destination)
            raise cc_use.SwapError(f'{destination} stayed locked by another program; retry')
        real_replace(source, destination)

    monkeypatch.setattr(cc_use, '_replace', config_stays_locked)

    # Act
    with pytest.raises(cc_use.SwapError, match='stayed locked'):
        cc_use.load('work')

    # Assert: the slot still holds the user's login, so a plain retry goes through.
    assert machine['default'].read_text() == HOME_SECRET
    assert machine['global_config'].read_text() == original_config
    assert cc_use.loaded_profile() is None
    cc_use.load('work')
    assert machine['default'].read_text() == WORK_SECRET


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


def test__lock_dir__gives_up_on_a_stale_lock_it_cannot_remove(tmp_path, monkeypatch):
    # Arrange: a stale lock that is not an empty directory, so rmdir keeps failing.
    monkeypatch.setattr(cc_use, 'LOCK_TIMEOUT_S', 0.3)
    lock = tmp_path / 'x.lock'
    lock.mkdir()
    (lock / 'stray').write_text('')
    old = time.time() - 120
    os.utime(lock, (old, old))

    # Act / Assert
    with pytest.raises(cc_use.SwapError, match='stayed held'):
        with cc_use._lock_dir(lock, stale_s=60):
            pass


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


def test__load__writes_the_account_record_before_the_stash(machine, monkeypatch):
    # Arrange: uninstall runs `forget` off these files, so no stash may exist without its record.
    real_write = cc_use._write_private

    def write_private(path, text):
        if path == machine['profiles'] / '.home-account.json':
            raise OSError(28, 'No space left on device')
        real_write(path, text)

    monkeypatch.setattr(cc_use, '_write_private', write_private)

    # Act
    with pytest.raises(OSError):
        cc_use.load('work')

    # Assert
    assert not machine['stash'].exists()


def test__load__sees_a_session_started_while_waiting_for_the_lock(machine, monkeypatch):
    # Arrange: `cc work` starts a session between cc-use's first look and the swap.
    sessions = machine['profiles'] / 'work' / 'sessions'
    sessions.mkdir()
    _on_lock(monkeypatch, lambda: (sessions / f'{os.getpid()}.json').write_text('{}'))

    # Act / Assert
    with pytest.raises(cc_use.SwapError, match='running session'):
        cc_use.load('work')
    assert machine['default'].read_text() == HOME_SECRET


def test__load__moves_the_incoming_login_as_it_stands_under_the_lock(machine, monkeypatch):
    # Arrange: a work session refreshes (rotates) work's own token and exits before the lock is taken.
    rotated = json.dumps({'claudeAiOauth': {'accessToken': 'work-at-2', 'refreshToken': 'work-rt-2'}})
    _on_lock(monkeypatch, lambda: (machine['profiles'] / 'work' / '.credentials.json').write_text(rotated))

    # Act
    cc_use.load('work')

    # Assert
    assert machine['default'].read_text() == rotated


def test__fetch_identity__refuses_a_redirect_and_keeps_the_token(monkeypatch):
    # Arrange: a local stand-in for the profile endpoint that redirects elsewhere.
    _RedirectingProfileServer.seen_auth = []
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), _RedirectingProfileServer)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(cc_use, 'PROFILE_URL', f'http://127.0.0.1:{server.server_port}/profile')

    # Act
    try:
        identity = cc_use._fetch_identity('secret-token')
    finally:
        server.shutdown()
        server.server_close()

    # Assert
    assert identity is None
    assert _RedirectingProfileServer.seen_auth == []


@pytest.mark.parametrize(
    'failure',
    [http.client.IncompleteRead(b'{'), ConnectionResetError(10054, 'reset'), None],
    ids=['incomplete', 'reset', 'bad-utf8'],
)
def test__fetch_identity__treats_a_broken_response_as_unknown(monkeypatch, failure):
    # Arrange: the request goes out, but the answer never arrives whole.
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            if failure is None:
                return b'\xff\xfe'
            raise failure

    opener = type('Opener', (), {'open': lambda self, request, timeout: Response()})()
    monkeypatch.setattr(cc_use.urllib.request, 'build_opener', lambda *handlers: opener)

    # Act / Assert
    assert cc_use._fetch_identity('token') is None


def test__status__treats_a_null_oauth_account_as_no_account(machine):
    # Arrange
    machine['global_config'].write_text(json.dumps({'oauthAccount': None}))

    # Act / Assert
    assert cc_use.status() == 'default slot: default (None)'


@pytest.mark.parametrize('online', [True, False], ids=['online', 'offline'])
def test__load__refuses_a_default_login_with_no_oauth_account(machine, online):
    # Arrange
    machine['global_config'].write_text(json.dumps({'oauthAccount': None}))
    if not online:
        machine['identities'].clear()

    # Act / Assert
    with pytest.raises(cc_use.SwapError, match='oauthAccount|not swapping'):
        cc_use.load('work')
    assert machine['default'].read_text() == HOME_SECRET
    assert not machine['stash'].exists()
    assert not (machine['profiles'] / '.home-account.json').exists()


def test__forget__refuses_when_the_slot_holds_another_account(machine):
    # Arrange: the stash is the only copy of the user's login, and .loaded never got written.
    _killed_mid_swap(machine, HOME_ACCOUNT)

    # Act / Assert
    with pytest.raises(cc_use.SwapError, match=r'recorded default \(me@example.com\); not deleting'):
        cc_use.forget()
    assert machine['stash'].read_text() == HOME_SECRET
    assert (machine['profiles'] / '.home-account.json').exists()


def test__forget__offline_refuses_when_the_slot_login_is_not_the_stashed_one(machine):
    # Arrange
    _killed_mid_swap(machine, HOME_ACCOUNT)
    machine['identities'].clear()

    # Act / Assert
    with pytest.raises(cc_use.SwapError, match='could not confirm'):
        cc_use.forget()
    assert machine['stash'].read_text() == HOME_SECRET


def test__forget__refuses_while_the_slot_is_empty(machine):
    # Arrange: a stash, and no login in the default slot at all.
    cc_use.load('work')
    cc_use.load(None)
    machine['default'].unlink()

    # Act / Assert
    with pytest.raises(cc_use.SwapError, match='holds no login'):
        cc_use.forget()
    assert machine['stash'].read_text() == HOME_SECRET


def test__load__refuses_to_overwrite_the_stash_with_a_profile_login(machine):
    # Arrange: the killed swap also wrote work's account into ~/.claude.json, so it looks consistent.
    _killed_mid_swap(machine, WORK_ACCOUNT)
    other = machine['profiles'] / 'other'
    other.mkdir()
    (other / '.claude.json').write_text(json.dumps({'oauthAccount': {'accountUuid': 'uuid-other'}}))
    (other / '.credentials.json').write_text(json.dumps({'claudeAiOauth': {'accessToken': 'other-at'}}))

    # Act / Assert: the refusal names the two files to delete if the stash really is stale.
    with pytest.raises(cc_use.SwapError, match=r'recorded default \(me@example.com\); not swapping') as refused:
        cc_use.load('other')
    assert str(machine['stash']) in str(refused.value)
    assert str(machine['profiles'] / '.home-account.json') in str(refused.value)
    assert machine['stash'].read_text() == HOME_SECRET


def test__load__a_loaded_profile_recorded_in_another_spelling_is_already_loaded(machine):
    # Arrange: an older cc-use recorded WORK; then a session rotated work's token in the slot.
    cc_use.load('work')
    (machine['profiles'] / '.loaded').write_text('WORK\n')
    rotated = json.dumps({'claudeAiOauth': {'accessToken': 'work-at-2', 'refreshToken': 'work-rt-2'}})
    machine['default'].write_text(rotated)
    machine['identities']['work-at-2'] = 'uuid-work'

    # Act
    message = cc_use.load('work')

    # Assert: no swap of the folder with itself, which would put the stale copy in the slot.
    assert 'already' in message
    assert machine['default'].read_text() == rotated


@pytest.mark.parametrize('content', ['', '{"oauthAccount": ', '[]', 'null'])
def test__status__reports_a_corrupt_global_config_as_a_swap_error(machine, content):
    # Arrange
    machine['global_config'].write_text(content)

    # Act / Assert
    with pytest.raises(cc_use.SwapError, match='.claude.json'):
        cc_use.status()


def test__load__default_refuses_a_non_object_account_record_before_writing(machine):
    # Arrange: work is loaded and the stashed account record is a JSON list, not an object.
    cc_use.load('work')
    (machine['profiles'] / '.home-account.json').write_text('[]')

    # Act
    with pytest.raises(cc_use.SwapError, match='home-account'):
        cc_use.load(None)

    # Assert
    assert machine['default'].read_text() == WORK_SECRET
    assert json.loads(machine['global_config'].read_text())['oauthAccount'] == WORK_ACCOUNT
    assert cc_use.loaded_profile() == 'work'


class _RedirectingProfileServer(http.server.BaseHTTPRequestHandler):
    seen_auth: list = []

    def do_GET(self):
        if self.path == '/profile':
            self.send_response(302)
            self.send_header('Location', '/elsewhere')
            self.end_headers()
            return
        _RedirectingProfileServer.seen_auth.append(self.headers.get('Authorization'))
        body = json.dumps({'account': {'uuid': 'uuid-x', 'email': 'x@example.com'}}).encode()
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def _on_lock(monkeypatch, action):
    """Run `action` as the credential locks are taken, i.e. after every check made before them."""
    real_lock = cc_use._credentials_lock

    @contextmanager
    def lock():
        with real_lock():
            action()
            yield

    monkeypatch.setattr(cc_use, '_credentials_lock', lock)


def _killed_mid_swap(machine, config_account):
    """The state a swap to work leaves when killed after the slot write and before .loaded."""
    machine['stash'].write_text(HOME_SECRET)
    (machine['profiles'] / '.home-account.json').write_text(json.dumps(HOME_ACCOUNT))
    machine['default'].write_text(WORK_SECRET)
    machine['global_config'].write_text(json.dumps({'oauthAccount': config_account}))
