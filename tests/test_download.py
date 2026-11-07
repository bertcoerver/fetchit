"""Tests for fetchit.download (urlopen is monkeypatched — no network)."""

import http.client
import io
import os
import socket
from urllib.error import HTTPError, URLError

import pytest

from fetchit import download, feeds
from fetchit.download import download_temp
from fetchit.feeds import FeedError

# conftest stubs download.reachable out for the whole suite, so that no test
# quietly depends on a host being up. The tests for the probe itself want the
# real one, taken here before any fixture has had a chance to replace it.
probe = download.reachable


class _FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def test_download_temp_writes_and_cleans_up(monkeypatch):
    monkeypatch.setattr(
        download, "urlopen", lambda url, timeout=30: _FakeResponse(b"audio-bytes")
    )

    seen_path = None
    with download_temp("https://a/ep1.mp3") as path:
        seen_path = path
        assert path.endswith(".mp3")
        with open(path, "rb") as fh:
            assert fh.read() == b"audio-bytes"

    # File is removed once the context exits.
    assert not os.path.exists(seen_path)


def test_download_temp_default_suffix(monkeypatch):
    monkeypatch.setattr(
        download, "urlopen", lambda url, timeout=30: _FakeResponse(b"x")
    )
    with download_temp("https://a/stream?id=5") as path:
        assert path.endswith(".mp3")


def test_download_temp_error_raises_and_cleans_up(monkeypatch):
    def boom(url, timeout=30):
        raise OSError("connection reset")

    monkeypatch.setattr(download, "urlopen", boom)

    with pytest.raises(FeedError):
        with download_temp("https://a/ep1.mp3"):
            pass


def test_fetch_text_retries_a_network_failure(monkeypatch):
    calls = []

    def flaky(url, timeout=30):
        calls.append(url)
        if len(calls) < 3:
            raise OSError("nodename nor servname provided, or not known")
        return _FakeResponse(b"ok")

    monkeypatch.setattr(download, "urlopen", flaky)

    assert download.fetch_text("https://a/playlist") == "ok"
    assert len(calls) == 3


def test_fetch_text_gives_up_after_the_last_attempt(monkeypatch):
    calls = []

    def boom(url, timeout=30):
        calls.append(url)
        raise OSError("connection reset")

    monkeypatch.setattr(download, "urlopen", boom)

    with pytest.raises(FeedError):
        download.fetch_text("https://a/playlist")
    assert len(calls) == feeds.RETRY_ATTEMPTS


def test_fetch_text_does_not_retry_a_bad_url(monkeypatch):
    calls = []

    def boom(url, timeout=30):
        calls.append(url)
        raise ValueError("unknown url type")

    monkeypatch.setattr(download, "urlopen", boom)

    with pytest.raises(FeedError):
        download.fetch_text("gopher://a")
    assert len(calls) == 1


def test_download_temp_retry_does_not_append_to_a_partial_file(monkeypatch):
    """A download cut off halfway must not leave its first bytes in the file."""
    calls = []

    def flaky(url, timeout=30):
        calls.append(url)
        if len(calls) == 1:
            return _HalfWayResponse(b"partial-")
        return _FakeResponse(b"whole-file")

    monkeypatch.setattr(download, "urlopen", flaky)

    with download_temp("https://a/ep1.mp3") as path:
        with open(path, "rb") as fh:
            assert fh.read() == b"whole-file"


class _HalfWayResponse(_FakeResponse):
    """Hands over some bytes, then dies the way a reset connection does."""

    def read(self, size=-1):
        chunk = super().read(size)
        if not chunk:
            raise ConnectionResetError("connection reset by peer")
        return chunk


def test_reachable_when_the_host_answers(monkeypatch):
    seen = {}

    def head(request, timeout=30):
        seen["method"] = request.get_method()
        seen["url"] = request.full_url
        return _FakeResponse(b"")

    monkeypatch.setattr(download, "urlopen", head)

    assert probe("https://www.youtube.com/") is True
    # Headers only — the probe must not pull the page down to answer.
    assert seen["method"] == "HEAD"
    assert seen["url"] == "https://www.youtube.com/"


def test_reachable_counts_an_http_error_as_an_answer(monkeypatch):
    def refused(request, timeout=30):
        raise HTTPError(request.full_url, 403, "Forbidden", {}, None)

    monkeypatch.setattr(download, "urlopen", refused)

    # 403 is the host talking, which is the whole question being asked.
    assert probe("https://www.youtube.com/") is True


@pytest.mark.parametrize(
    "error",
    [
        URLError(socket.gaierror("nodename nor servname provided")),
        ConnectionRefusedError("connection refused"),
        TimeoutError("timed out"),
    ],
)
def test_reachable_is_false_when_no_connection_can_be_made(monkeypatch, error):
    def boom(request, timeout=30):
        raise error

    monkeypatch.setattr(download, "urlopen", boom)

    assert probe("https://www.youtube.com/") is False


def test_reachable_does_not_retry(monkeypatch):
    """It exists to fail fast; retrying it would defeat the point."""
    calls = []

    def boom(request, timeout=30):
        calls.append(request.full_url)
        raise ConnectionRefusedError("connection refused")

    monkeypatch.setattr(download, "urlopen", boom)

    assert probe("https://www.youtube.com/") is False
    assert len(calls) == 1


# --- urls written for people ----------------------------------------------


@pytest.mark.parametrize(
    "given, expected",
    [
        # Spaces are what http.client refuses outright.
        ("https://a/My Show/ep 1.mp3", "https://a/My%20Show/ep%201.mp3"),
        # Brackets and parentheses travel as they are, as a browser sends them.
        (
            "https://a/Show [2026-10-04] Episode (720p_2).mp4",
            "https://a/Show%20[2026-10-04]%20Episode%20(720p_2).mp4",
        ),
        ("https://a/Oekraïne.mp3", "https://a/Oekra%C3%AFne.mp3"),
        ("https://a/get?file=a b.mp3&t=1", "https://a/get?file=a%20b.mp3&t=1"),
        # The fragment is never sent.
        ("https://a/ep.mp3#t=30", "https://a/ep.mp3"),
        ("  https://a/ep.mp3\n", "https://a/ep.mp3"),
    ],
)
def test_safe_url_escapes_what_cannot_be_sent(given, expected):
    assert download.safe_url(given) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://a/ep1.mp3",
        # Already escaped: not escaped twice.
        "https://a/My%20Show/ep%201.mp3",
        # A signed link is signed over its exact bytes, so nothing that could
        # be sent as it stood may be rewritten.
        "https://cdn.example.com/e/ep.mp3?Expires=1790000000&Signature=aB3~x-_Y%2Bz==&Key-Pair-Id=K1",
        "https://vod.example.com/p/a,b;c=d/x.m3u8?hdnts=exp=1~acl=/p/*~hmac=ab12",
        "https://user:pw@a:8443/x/~me/file.mp3",
    ],
)
def test_safe_url_leaves_a_sendable_url_exactly_alone(url):
    assert download.safe_url(url) == url


def test_download_temp_asks_for_a_url_with_spaces_in_it_escaped(monkeypatch):
    asked = []

    def fake_urlopen(url, timeout=30):
        asked.append(url)
        return _FakeResponse(b"audio-bytes")

    monkeypatch.setattr(download, "urlopen", fake_urlopen)

    with download_temp("https://a/My Show/ep 1.mp3") as path:
        # The suffix still comes from the name as it was written.
        assert path.endswith(".mp3")

    assert asked == ["https://a/My%20Show/ep%201.mp3"]


def test_fetch_text_asks_for_a_url_with_spaces_in_it_escaped(monkeypatch):
    asked = []

    def fake_urlopen(request, timeout=30):
        asked.append(request.full_url)
        return _FakeResponse(b"ok")

    monkeypatch.setattr(download, "urlopen", fake_urlopen)

    assert download.fetch_text("https://a/play list.m3u8") == "ok"
    assert asked == ["https://a/play%20list.m3u8"]


def test_a_complaint_from_http_client_is_a_feed_error(monkeypatch):
    """Not an OSError, so it used to get out as itself and take the feed down."""

    def refuses(url, timeout=30):
        raise http.client.IncompleteRead(b"half")

    monkeypatch.setattr(download, "urlopen", refuses)

    with pytest.raises(FeedError, match="Could not download"):
        with download_temp("https://a/ep1.mp3"):
            pass
    with pytest.raises(FeedError, match="Could not fetch"):
        download.fetch_text("https://a/playlist")


def test_reachable_is_false_for_a_complaint_from_http_client(monkeypatch):
    def refuses(request, timeout=5):
        raise http.client.InvalidURL("nope")

    monkeypatch.setattr(download, "urlopen", refuses)

    assert probe("https://a/") is False


def test_download_temp_really_gets_a_spaced_url_past_http_client(monkeypatch):
    """The real urlopen, stopped just short of the network."""

    def no_network(*args, **kwargs):
        raise OSError("no network in tests")

    monkeypatch.setattr(socket, "create_connection", no_network)

    with pytest.raises(FeedError, match="no network in tests"):
        with download_temp("http://a.invalid/My Show/ep 1.mp3"):
            pass
