import http.client
import http.server
import subprocess
import threading
from contextlib import contextmanager

import pytest

import mcp_gateway


class _Upstream(http.server.BaseHTTPRequestHandler):
    """Stands in for a hosted MCP server: records each request, answers with a session id."""

    seen: list[dict] = []

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


def _post(port, path, body=b'{"method": "initialize"}'):
    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
    conn.request('POST', path, body=body, headers={'Content-Type': 'application/json'})
    resp = conn.getresponse()
    return resp.status, dict(resp.getheaders()), resp.read()


def test__gateway__forwards_to_the_upstream_with_the_keychain_token(gateway):
    # Act
    status, headers, body = _post(gateway['port'], '/tracker')

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
