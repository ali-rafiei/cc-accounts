#!/usr/bin/env python3
"""Local MCP gateway shared by every Claude Code account on this machine.

Claude Code connects to http://127.0.0.1:8765/<name> with no credentials; the gateway
adds the token for <name> from the macOS Keychain (item `claude-mcp-<name>`) and
forwards the request to the upstream MCP server. Tokens stay on this machine and never
enter a claude.ai account, so they stay private even on a shared account.

Install and store tokens with mcp_gateway_setup.sh; register a route once with
`claude mcp add --scope user --transport http <name> http://127.0.0.1:8765/<name>`.

An OAuth route (OAUTH_UPSTREAMS) uses the sign-in the default login made through `/mcp`
instead: the entry in the default slot's `mcpOAuth` (Keychain item `Claude Code-credentials`).
These servers rotate the refresh token on every refresh, so that entry stays the only copy:
the gateway refreshes it under Claude Code's own locks and writes the result back to the slot.
"""

from __future__ import annotations

import http.client
import json
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import cc_use

PORT = 8765
UPSTREAMS = {
    'linear': 'https://mcp.linear.app/mcp',
    'github': 'https://api.githubcopilot.com/mcp/',
}
# route: (the server's name in Claude Code's mcpOAuth, its URL). Both must match the entry.
OAUTH_UPSTREAMS = {
    'forgefirst': ('plugin:alta:forgefirst', 'https://altaml.ca.altaforge.com/forgefirst/mcp'),
    'fellow': ('fellow', 'https://fellow.app/mcp'),
}
# The default login's config dir, where Claude Code keeps the locks below.
CLAUDE_DIR = Path.home() / '.claude'
# Claude Code (2.1.332) refreshes an MCP sign-in under a per-server lock and saves any
# credential under a store-wide one; both are proper-lockfile directories.
MCP_REFRESH_LOCK_STALE_S = 60.0
STORAGE_WRITE_LOCK_STALE_S = 15.0
# Claude Code treats a token with less than this left as due for a refresh.
REFRESH_MARGIN_S = 300
FORWARD_REQUEST_HEADERS = ('content-type', 'accept', 'mcp-session-id', 'mcp-protocol-version', 'last-event-id')
DROP_RESPONSE_HEADERS = {'transfer-encoding', 'connection'}


def keychain_token(name: str) -> str:
    return subprocess.run(
        ['security', 'find-generic-password', '-s', f'claude-mcp-{name}', '-w'],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


class SignInError(Exception):
    """The default slot holds no usable sign-in for an OAuth route."""


def oauth_access_token(route: str, rejected: str | None = None) -> str:
    """The default slot's access token for an OAuth route, refreshed first when it is due or was `rejected`."""
    server, url = OAUTH_UPSTREAMS[route]
    key, entry = _slot_entry(server, url)
    if _usable(entry, rejected):
        return entry['accessToken']
    return _refresh(server, url, key, rejected)


def _refresh(server: str, url: str, key: str, rejected: str | None) -> str:
    lock = CLAUDE_DIR / f'mcp-refresh-{re.sub(r"[^a-zA-Z0-9]", "_", key)}.lock.lock'
    with cc_use._lock_dir(lock, MCP_REFRESH_LOCK_STALE_S):
        # A session may have refreshed it while this waited for the lock.
        _, entry = _slot_entry(server, url, key)
        if _usable(entry, rejected):
            return entry['accessToken']
        if not entry.get('refreshToken'):
            raise SignInError(f'the sign-in to {server} has expired: sign in again with /mcp on your default login')
        tokens = _post_refresh(server, entry)
        # cc-use's locks too, so a swap in progress can't write back the slot it read before this.
        storage_write = CLAUDE_DIR / '.storage-write.lock'
        with cc_use._credentials_lock(), cc_use._lock_dir(storage_write, STORAGE_WRITE_LOCK_STALE_S):
            data = json.loads(cc_use._keychain_get(cc_use.DEFAULT_SERVICE) or '{}')
            current = (data.get('mcpOAuth') or {}).get(key) or {}
            if current.get('refreshToken') != entry['refreshToken']:
                # Signed in again meanwhile: that sign-in is newer than this refresh, so it stays.
                return current.get('accessToken') or tokens['access_token']
            refreshed = {
                **current,
                'accessToken': tokens['access_token'],
                'refreshToken': tokens.get('refresh_token') or entry['refreshToken'],
            }
            if tokens.get('expires_in'):
                refreshed['expiresAt'] = int((time.time() + tokens['expires_in']) * 1000)
            data['mcpOAuth'][key] = refreshed
            cc_use._keychain_set(cc_use.DEFAULT_SERVICE, json.dumps(data))
        return tokens['access_token']


def _slot_entry(server: str, url: str, key: str | None = None) -> tuple[str, dict]:
    """The default slot's mcpOAuth entry for this server (by `key` once known), and its key."""
    secret = cc_use._keychain_get(cc_use.DEFAULT_SERVICE)
    entries = (json.loads(secret).get('mcpOAuth') or {}) if secret else {}
    found = [
        (k, e)
        for k, e in entries.items()
        if (k == key if key else e.get('serverName') == server and e.get('serverUrl') == url)
        and (e.get('accessToken') or e.get('refreshToken'))
    ]
    if not found:
        raise SignInError(f'no sign-in to {server} in the default login: sign in with /mcp in a plain `claude` session')
    return max(found, key=lambda item: item[1].get('expiresAt') or 0)


def _usable(entry: dict, rejected: str | None) -> bool:
    expires_at = entry.get('expiresAt')
    due = expires_at is not None and expires_at / 1000 - time.time() < REFRESH_MARGIN_S
    return bool(entry.get('accessToken')) and entry['accessToken'] != rejected and not due


def _post_refresh(server: str, entry: dict) -> dict:
    discovery = entry.get('discoveryState') or {}
    metadata_url = discovery.get('authServerMetadataUrl') or (
        discovery.get('authorizationServerUrl', '').rstrip('/') + '/.well-known/oauth-authorization-server'
    )
    token_endpoint = _get_json(urllib.request.Request(metadata_url)).get('token_endpoint')
    if not token_endpoint:
        raise SignInError(f'{server} publishes no token endpoint at {metadata_url}')
    form = {'grant_type': 'refresh_token', 'refresh_token': entry['refreshToken'], 'client_id': entry.get('clientId')}
    if discovery.get('resourceMetadataUrl'):
        form['resource'] = entry['serverUrl']  # RFC 8707, as Claude Code's MCP client sends it
    request = urllib.request.Request(
        token_endpoint,
        data=urllib.parse.urlencode({k: v for k, v in form.items() if v}).encode(),
        headers={'Content-Type': 'application/x-www-form-urlencoded', 'Accept': 'application/json'},
    )
    try:
        tokens = _get_json(request)
    except urllib.error.HTTPError as exc:
        if exc.code in (400, 401):
            raise SignInError(
                f'{server} refused to refresh the sign-in ({exc.code}): sign in again with /mcp on your default login'
            ) from exc
        raise
    if not tokens.get('access_token'):
        raise SignInError(f'{server} answered the refresh with no access token')
    return tokens


def _get_json(request: urllib.request.Request) -> dict:
    # A redirect would carry the refresh token to wherever it points; it is refused instead.
    with urllib.request.build_opener(cc_use._RefuseRedirect).open(request, timeout=30) as response:
        body = json.loads(response.read().decode())
    return body if isinstance(body, dict) else {}


class Gateway(BaseHTTPRequestHandler):
    def _forward(self) -> None:
        # A web page can reach 127.0.0.1 by rebinding its own DNS name to it. Its requests carry
        # that name as Host, and an Origin, which Claude Code's client never sends.
        port = self.server.server_address[1]
        if self.headers.get('Host') not in (f'127.0.0.1:{port}', f'localhost:{port}') or 'Origin' in self.headers:
            self.send_error(403, 'only local MCP clients may use this gateway')
            return
        name = self.path.strip('/').split('?')[0]
        oauth = name in OAUTH_UPSTREAMS
        if name not in UPSTREAMS and not oauth:
            self.send_error(404, f'unknown MCP server: {name}')
            return
        length = int(self.headers.get('Content-Length') or 0)
        body = self.rfile.read(length) if length else None
        token = self._token(name)
        if token is None:
            return
        upstream, resp = self._send(name, token, body)
        if resp is not None and resp.status == 401 and oauth:
            # Revoked or expired early: refresh once and retry, never hand the client the
            # upstream's challenge, which would start a sign-in of its own.
            upstream.close()
            token = self._token(name, rejected=token)
            if token is None:
                return
            upstream, resp = self._send(name, token, body)
            if resp is not None and resp.status == 401:
                upstream.close()
                self.send_error(401, f"{name} refused the default login's sign-in: sign in again with /mcp there")
                return
        if resp is None:
            return
        self.send_response(resp.status)
        for key, value in resp.getheaders():
            if key.lower() not in DROP_RESPONSE_HEADERS:
                self.send_header(key, value)
        self.end_headers()
        # Chunk by chunk, so server-sent events reach Claude Code as they arrive.
        while chunk := resp.read1(65536):
            self.wfile.write(chunk)
            self.wfile.flush()
        upstream.close()

    def _token(self, name: str, rejected: str | None = None) -> str | None:
        """The bearer token for a route, or None once an error has been sent."""
        if name in UPSTREAMS:
            try:
                return keychain_token(name)
            except subprocess.CalledProcessError:
                self.send_error(401, f'no token for {name}: run mcp_gateway_setup.sh {name}')
                return None
        try:
            return oauth_access_token(name, rejected)
        except SignInError as exc:
            self.send_error(401, str(exc))
        except cc_use.SwapError as exc:  # the Keychain or a lock did not answer
            self.send_error(503, f'{name}: {exc}')
        except (OSError, ValueError, http.client.HTTPException) as exc:
            self.send_error(502, f'{name} sign-in server unreachable: {exc}')
        return None

    def _send(self, name: str, token: str, body: bytes | None):
        """Forward the request; (connection, response), or (None, None) once a 502 has been sent."""
        url = urllib.parse.urlsplit(UPSTREAMS.get(name) or OAUTH_UPSTREAMS[name][1])
        headers = {k: v for k, v in self.headers.items() if k.lower() in FORWARD_REQUEST_HEADERS}
        headers['Authorization'] = f'Bearer {token}'
        connection = http.client.HTTPSConnection if url.scheme == 'https' else http.client.HTTPConnection
        upstream = connection(url.netloc, timeout=300)
        try:
            upstream.request(self.command, url.path, body=body, headers=headers)
            return upstream, upstream.getresponse()
        except OSError as exc:
            upstream.close()
            self.send_error(502, f'{name} upstream unreachable: {exc}')
            return None, None

    do_GET = do_POST = do_DELETE = _forward


if __name__ == '__main__':
    ThreadingHTTPServer(('127.0.0.1', PORT), Gateway).serve_forever()
