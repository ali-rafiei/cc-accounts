import http.client
import http.server
import json
import subprocess
import threading
import time
import urllib.parse
from contextlib import contextmanager

import pytest

import cc_use
import mcp_gateway


class _Upstream(http.server.BaseHTTPRequestHandler):
    """Stands in for a hosted MCP server: records each request, answers with a session id.
    A GET opens a server-sent event stream whose second event waits for `release`."""

    seen: list[dict] = []
    release = threading.Event()

    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.end_headers()
        self.wfile.write(b'data: one\n\n')
        self.wfile.flush()
        _Upstream.release.wait(timeout=10)
        self.wfile.write(b'data: two\n\n')

    def do_POST(self):
        body = self.rfile.read(int(self.headers['Content-Length']))
        _Upstream.seen.append({'path': self.path, 'auth': self.headers.get('Authorization'), 'body': body})
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Mcp-Session-Id', 'session-1')
        self.end_headers()
        self.wfile.write(b'{"result": "ok"}')

    def log_message(self, *args):
        pass


@contextmanager
def _serve(handler):
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()


@pytest.fixture
def gateway(monkeypatch):
    """The gateway with one route, 'tracker', to a local upstream, and a fake Keychain."""
    _Upstream.seen = []
    _Upstream.release = threading.Event()
    keychain = {'tracker': 'secret-token'}

    def keychain_token(name):
        if name not in keychain:
            raise subprocess.CalledProcessError(44, 'security')
        return keychain[name]

    monkeypatch.setattr(mcp_gateway, 'keychain_token', keychain_token)
    monkeypatch.setattr(mcp_gateway.Gateway, 'log_message', lambda *args: None)
    with _serve(_Upstream) as upstream_port:
        monkeypatch.setattr(mcp_gateway, 'UPSTREAMS', {'tracker': f'http://127.0.0.1:{upstream_port}/mcp'})
        with _serve(mcp_gateway.Gateway) as port:
            yield {'port': port, 'keychain': keychain}


def _post(port, path, body=b'{"method": "initialize"}', headers=None):
    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
    conn.request('POST', path, body=body, headers={'Content-Type': 'application/json', **(headers or {})})
    resp = conn.getresponse()
    return resp.status, dict(resp.getheaders()), resp.read()


def test__gateway__forwards_to_the_upstream_with_the_keychain_token(gateway):
    # Act: a token the client sends itself is replaced, not forwarded.
    status, headers, body = _post(gateway['port'], '/tracker', headers={'Authorization': 'Bearer client-token'})

    # Assert
    assert (status, body, headers['Mcp-Session-Id']) == (200, b'{"result": "ok"}', 'session-1')
    assert _Upstream.seen == [{'path': '/mcp', 'auth': 'Bearer secret-token', 'body': b'{"method": "initialize"}'}]


def test__gateway__unknown_server_is_404_and_reaches_no_upstream(gateway):
    # Act
    status, _, body = _post(gateway['port'], '/missing')

    # Assert
    assert status == 404
    assert b'unknown MCP server: missing' in body
    assert _Upstream.seen == []


def test__gateway__missing_token_names_the_setup_command(gateway):
    # Arrange
    del gateway['keychain']['tracker']

    # Act
    status, _, body = _post(gateway['port'], '/tracker')

    # Assert
    assert status == 401
    assert b'mcp_gateway_setup.sh tracker' in body
    assert _Upstream.seen == []


def test__gateway__unreachable_upstream_is_502(gateway, monkeypatch):
    # Arrange: nothing listens on port 1.
    monkeypatch.setattr(mcp_gateway, 'UPSTREAMS', {'tracker': 'http://127.0.0.1:1/mcp'})

    # Act
    status, _, body = _post(gateway['port'], '/tracker')

    # Assert
    assert status == 502
    assert b'tracker' in body


@pytest.mark.parametrize(
    'headers',
    [{'Host': 'evil.example:8765'}, {'Origin': 'http://evil.example'}],
    ids=['rebound-host', 'browser-origin'],
)
def test__gateway__refuses_browser_requests_and_never_uses_the_token(gateway, headers):
    # Act: what a web page that rebinds its DNS name to 127.0.0.1 would send.
    status, _, _ = _post(gateway['port'], '/tracker', headers=headers)

    # Assert
    assert status == 403
    assert _Upstream.seen == []


def test__gateway__streams_each_event_as_the_upstream_sends_it(gateway):
    # Arrange
    conn = http.client.HTTPConnection('127.0.0.1', gateway['port'], timeout=3)
    conn.request('GET', '/tracker', headers={'Accept': 'text/event-stream'})
    resp = conn.getresponse()

    # Act: the first event must arrive while the upstream still holds back the second.
    first = resp.fp.readline() + resp.fp.readline()
    _Upstream.release.set()
    rest = resp.read()

    # Assert
    assert (resp.status, first, rest) == (200, b'data: one\n\n', b'data: two\n\n')


ROTATED = {'accessToken': 'fresh-at', 'refreshToken': 'rt-2'}


class _TokenEndpoint(http.server.BaseHTTPRequestHandler):
    """Stands in for an OAuth server: serves its metadata and rotates the refresh token on each refresh.
    `during` runs while a refresh is in flight, as another process would."""

    refreshes: list[dict] = []
    during = None

    def do_GET(self):
        port = self.server.server_address[1]
        self._answer(200, {'token_endpoint': f'http://127.0.0.1:{port}/token'})

    def do_POST(self):
        form = urllib.parse.parse_qs(self.rfile.read(int(self.headers['Content-Length'])).decode())
        _TokenEndpoint.refreshes.append({k: v[0] for k, v in form.items()})
        if _TokenEndpoint.during:
            _TokenEndpoint.during()
        self._answer(200, {'access_token': 'fresh-at', 'refresh_token': 'rt-2', 'expires_in': 3600})

    def _answer(self, status, body):
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def log_message(self, *args):
        pass


class _OAuthUpstream(_Upstream):
    """A hosted MCP server that accepts only `valid` as a bearer token."""

    valid = 'fresh-at'
    on_reject = None

    def do_POST(self):
        if self.headers.get('Authorization') != f'Bearer {_OAuthUpstream.valid}':
            self.rfile.read(int(self.headers['Content-Length']))
            _Upstream.seen.append({'auth': self.headers.get('Authorization'), 'status': 401})
            if _OAuthUpstream.on_reject:
                _OAuthUpstream.on_reject()
            self.send_response(401)
            self.send_header('WWW-Authenticate', 'Bearer resource_metadata="https://upstream.example/prm"')
            self.end_headers()
            return
        super().do_POST()


@pytest.fixture
def oauth_gateway(monkeypatch, tmp_path):
    """The gateway with one OAuth route, 'notes', whose sign-in lives in a fake default slot."""
    _Upstream.seen = []
    _TokenEndpoint.refreshes = []
    _TokenEndpoint.during = None
    _OAuthUpstream.valid = 'fresh-at'
    _OAuthUpstream.on_reject = None
    (tmp_path / '.claude').mkdir()
    monkeypatch.setattr(mcp_gateway, 'CLAUDE_DIR', tmp_path / '.claude')
    monkeypatch.setattr(cc_use, 'OAUTH_REFRESH_LOCK', tmp_path / '.claude' / '.oauth_refresh.lock')
    monkeypatch.setattr(cc_use, 'LEGACY_CREDENTIALS_LOCK', tmp_path / '.claude.lock')
    monkeypatch.setattr(mcp_gateway.Gateway, 'log_message', lambda *args: None)
    keychain = {}
    held = []

    def keychain_set(service, secret):
        # Every lock a write must hold, as Claude Code's own writers and cc-use see them.
        held.append(
            sorted(p.name for p in (tmp_path / '.claude').iterdir())
            + sorted(p.name for p in tmp_path.glob('.claude.lock'))
        )
        keychain[service] = secret

    monkeypatch.setattr(cc_use, '_keychain_get', keychain.get)
    monkeypatch.setattr(cc_use, '_keychain_set', keychain_set)
    monkeypatch.setattr(mcp_gateway, 'keychain_token', lambda name: pytest.fail('an OAuth route read a static token'))
    with _serve(_OAuthUpstream) as upstream_port, _serve(_TokenEndpoint) as auth_port:
        url = f'http://127.0.0.1:{upstream_port}/mcp'
        monkeypatch.setattr(mcp_gateway, 'UPSTREAMS', {})
        monkeypatch.setattr(mcp_gateway, 'OAUTH_UPSTREAMS', {'notes': ('plugin:notes:notes', url)})
        entry = {
            'serverName': 'plugin:notes:notes',
            'serverUrl': url,
            'clientId': 'client-1',
            'accessToken': 'old-at',
            'refreshToken': 'rt-1',
            'expiresAt': int((time.time() + 3600) * 1000),
            'discoveryState': {'authorizationServerUrl': f'http://127.0.0.1:{auth_port}'},
        }
        keychain[cc_use.DEFAULT_SERVICE] = json.dumps(
            {'claudeAiOauth': {'accessToken': 'login-at'}, 'mcpOAuth': {'plugin:notes:notes|abc123': entry}}
        )
        with _serve(mcp_gateway.Gateway) as port:
            yield {'port': port, 'keychain': keychain, 'held': held}


def _slot(keychain):
    return json.loads(keychain[cc_use.DEFAULT_SERVICE])


def _set_entry(keychain, **fields):
    data = _slot(keychain)
    data['mcpOAuth']['plugin:notes:notes|abc123'].update(fields)
    keychain[cc_use.DEFAULT_SERVICE] = json.dumps(data)


def test__oauth_route__forwards_the_default_slots_valid_token_without_refreshing(oauth_gateway):
    # Arrange
    _OAuthUpstream.valid = 'old-at'
    before = oauth_gateway['keychain'][cc_use.DEFAULT_SERVICE]

    # Act: a token the client sends itself is replaced, not forwarded.
    status, _, body = _post(oauth_gateway['port'], '/notes', headers={'Authorization': 'Bearer client-token'})

    # Assert
    assert (status, body) == (200, b'{"result": "ok"}')
    assert [seen['auth'] for seen in _Upstream.seen] == ['Bearer old-at']
    assert _TokenEndpoint.refreshes == []
    assert oauth_gateway['keychain'][cc_use.DEFAULT_SERVICE] == before


@pytest.mark.parametrize('trigger', ['expired', 'rejected'])
def test__oauth_route__refreshes_once_under_claude_codes_locks_and_writes_back(oauth_gateway, trigger):
    # Arrange: expired by its own clock, or still in date but answered with a 401.
    if trigger == 'expired':
        _set_entry(oauth_gateway['keychain'], expiresAt=int((time.time() + 60) * 1000))

    # Act
    status, _, body = _post(oauth_gateway['port'], '/notes')

    # Assert
    assert (status, body) == (200, b'{"result": "ok"}')
    assert [r['refresh_token'] for r in _TokenEndpoint.refreshes] == ['rt-1']
    assert _TokenEndpoint.refreshes[0]['grant_type'] == 'refresh_token'
    assert _TokenEndpoint.refreshes[0]['client_id'] == 'client-1'
    assert _Upstream.seen[-1]['auth'] == 'Bearer fresh-at'
    slot = _slot(oauth_gateway['keychain'])
    entry = slot['mcpOAuth']['plugin:notes:notes|abc123']
    assert {k: entry[k] for k in ROTATED} == ROTATED
    assert entry['expiresAt'] > (time.time() + 3000) * 1000
    assert slot['claudeAiOauth'] == {'accessToken': 'login-at'}
    assert oauth_gateway['held'] == [
        [
            '.oauth_refresh.lock',
            '.storage-write.lock',
            'mcp-refresh-plugin_notes_notes_abc123.lock.lock',
            '.claude.lock',
        ]
    ]


def test__oauth_route__keeps_a_newer_sign_in_another_process_wrote_mid_refresh(oauth_gateway):
    # Arrange: while the gateway's refresh is in flight, a session signs in again and a
    # login refresh rewrites the same item.
    keychain = oauth_gateway['keychain']
    _set_entry(keychain, expiresAt=0)
    _OAuthUpstream.valid = 'their-at'

    def another_process():
        _set_entry(
            keychain, accessToken='their-at', refreshToken='their-rt', expiresAt=int((time.time() + 3600) * 1000)
        )
        data = _slot(keychain)
        data['claudeAiOauth'] = {'accessToken': 'login-at-2'}
        keychain[cc_use.DEFAULT_SERVICE] = json.dumps(data)

    _TokenEndpoint.during = another_process

    # Act
    status, _, _ = _post(oauth_gateway['port'], '/notes')

    # Assert: their sign-in and their login refresh both survive, and the request uses theirs.
    assert status == 200
    slot = _slot(keychain)
    entry = slot['mcpOAuth']['plugin:notes:notes|abc123']
    assert (entry['accessToken'], entry['refreshToken']) == ('their-at', 'their-rt')
    assert slot['claudeAiOauth'] == {'accessToken': 'login-at-2'}
    assert _Upstream.seen[-1]['auth'] == 'Bearer their-at'


def test__oauth_route__uses_a_token_another_process_refreshed_before_it_could(oauth_gateway):
    # Arrange: a session refreshes the sign-in just as the upstream rejects the gateway's old token.
    _OAuthUpstream.on_reject = lambda: _set_entry(
        oauth_gateway['keychain'], accessToken='fresh-at', refreshToken='rt-2'
    )

    # Act
    status, _, _ = _post(oauth_gateway['port'], '/notes')

    # Assert
    assert status == 200
    assert _TokenEndpoint.refreshes == []
    assert [seen['auth'] for seen in _Upstream.seen] == ['Bearer old-at', 'Bearer fresh-at']


def test__oauth_route__no_sign_in_in_the_default_slot_is_401_naming_the_fix(oauth_gateway):
    # Arrange
    oauth_gateway['keychain'][cc_use.DEFAULT_SERVICE] = json.dumps({'claudeAiOauth': {}})

    # Act
    status, headers, body = _post(oauth_gateway['port'], '/notes')

    # Assert
    assert status == 401
    assert b'plugin:notes:notes' in body and b'/mcp' in body
    assert 'WWW-Authenticate' not in headers
    assert _Upstream.seen == []


def test__oauth_route__a_second_401_is_not_passed_on_as_a_sign_in_challenge(oauth_gateway):
    # Arrange: the upstream refuses even the refreshed token.
    _OAuthUpstream.valid = 'never'

    # Act
    status, headers, _ = _post(oauth_gateway['port'], '/notes')

    # Assert: no challenge reaches the client, so it never starts its own sign-in.
    assert status == 401
    assert 'WWW-Authenticate' not in headers
    assert len(_TokenEndpoint.refreshes) == 1


def test__static_route__never_reads_the_default_slot(gateway, monkeypatch):
    # Arrange
    monkeypatch.setattr(cc_use, '_keychain_get', lambda service: pytest.fail('a static route read the slot'))

    # Act
    status, _, _ = _post(gateway['port'], '/tracker')

    # Assert
    assert status == 200
    assert _Upstream.seen[0]['auth'] == 'Bearer secret-token'
