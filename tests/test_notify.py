"""Tests for fetchit.notify (a real HTTP server on localhost)."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from fetchit import notify


@pytest.fixture
def listener():
    got = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            got.append((self.path, self.headers.get("Authorization"), body))
            self.send_response(403 if self.path == "/no" else 202)
            self.end_headers()
            self.wfile.write(b'{"error": "nope"}' if self.path == "/no" else b"{}")

        def log_message(self, *args):
            pass

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", got
    httpd.shutdown()
    httpd.server_close()


def test_nothing_configured_tells_nobody(monkeypatch):
    monkeypatch.delenv(notify.NOTIFY_ENV, raising=False)
    assert notify.notify(["A"], 1) == 0


def test_posts_the_feeds_to_every_target(listener, monkeypatch, capsys):
    base, got = listener
    monkeypatch.setenv(notify.NOTIFY_ENV, f"{base}/api/fetched, {base}/no  http://127.0.0.1:9/")
    monkeypatch.setenv(notify.NOTIFY_TOKEN_ENV, "s3cret")
    assert notify.notify(["Bureau Buitenland", "3Blue1Brown"], 3) == 1
    assert got[0] == ("/api/fetched", "Bearer s3cret",
                      {"source": "fetchit", "fetched": 3, "feeds": ["Bureau Buitenland", "3Blue1Brown"]})
    err = capsys.readouterr().err
    assert "HTTP 403: " in err and "nope" in err and "127.0.0.1:9" in err
