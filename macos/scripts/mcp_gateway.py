#!/usr/bin/env python3
"""Local MCP gateway shared by every Claude Code account on this machine.

Claude Code connects to http://127.0.0.1:8765/<name> with no credentials; the gateway
adds the token for <name> from the macOS Keychain (item `claude-mcp-<name>`) and
forwards the request to the upstream MCP server. Tokens stay on this machine and never
enter a claude.ai account, so they stay private even on a shared account.

Install and store tokens with mcp_gateway_setup.sh; register a route once with
`claude mcp add --scope user --transport http <name> http://127.0.0.1:8765/<name>`.
"""

import http.client
import subprocess
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 8765
UPSTREAMS = {
    'linear': 'https://mcp.linear.app/mcp',
    'github': 'https://api.githubcopilot.com/mcp/',
}
FORWARD_REQUEST_HEADERS = ('content-type', 'accept', 'mcp-session-id', 'mcp-protocol-version', 'last-event-id')
DROP_RESPONSE_HEADERS = {'transfer-encoding', 'connection'}


def keychain_token(name: str) -> str:
    return subprocess.run(
        ['security', 'find-generic-password', '-s', f'claude-mcp-{name}', '-w'],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


class Gateway(BaseHTTPRequestHandler):
    def _forward(self) -> None:
        name = self.path.strip('/').split('?')[0]
        if name not in UPSTREAMS:
            self.send_error(404, f'unknown MCP server: {name}')
            return
        try:
            token = keychain_token(name)
        except subprocess.CalledProcessError:
            self.send_error(401, f'no token for {name}: run mcp_gateway_setup.sh {name}')
            return
        url = urllib.parse.urlsplit(UPSTREAMS[name])
        headers = {k: v for k, v in self.headers.items() if k.lower() in FORWARD_REQUEST_HEADERS}
        headers['Authorization'] = f'Bearer {token}'
        length = int(self.headers.get('Content-Length') or 0)
        body = self.rfile.read(length) if length else None

        connection = http.client.HTTPSConnection if url.scheme == 'https' else http.client.HTTPConnection
        upstream = connection(url.netloc, timeout=300)
        try:
            upstream.request(self.command, url.path, body=body, headers=headers)
            resp = upstream.getresponse()
        except OSError as exc:
            upstream.close()
            self.send_error(502, f'{name} upstream unreachable: {exc}')
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

    do_GET = do_POST = do_DELETE = _forward


if __name__ == '__main__':
    ThreadingHTTPServer(('127.0.0.1', PORT), Gateway).serve_forever()
