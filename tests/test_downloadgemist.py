"""Tests for fetchit.downloadgemist: the call sequence, and asking gently.

The endpoint is faked at :func:`downloadgemist._call` and the file download at
:func:`downloadgemist._download`; the clock is a fake one the tests move by
hand, so the waits and backoffs are tested without anyone waiting.
"""

import io
import json
import os

import pytest

from fetchit import cache, downloadgemist
from fetchit.feeds import FeedError
from fetchit.media import VideoGone

EPISODE = "https://npo.nl/start/afspelen/bureau-buitenland_825"
OTHER = "https://npo.nl/start/afspelen/bureau-buitenland_826"
FILE_URL = "https://downloadgemist.nl/cache/abc/Bureau%20Buitenland.mp4"

_REAL_DOWNLOAD = downloadgemist._download

STREAMS = {
    "sessionID": "1790595490-7408",
    "title": "Bureau Buitenland s02e28",
    "date": "2026-09-27",
    "duration": 1517,
    "streams": [
        {"streamlabel": "720p", "bitrate": 2424017},
        {"streamlabel": "360p", "bitrate": 399307},
        {"streamlabel": "128Kbps", "bitrate": 128000},
    ],
    "subtitles": True,
}


class Clock:
    """Stands in for the ``time`` module inside downloadgemist."""

    def __init__(self, now=1_790_000_000.0):
        self.now = now
        self.slept = []

    def time(self):
        return self.now

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture(autouse=True)
def no_live_sessions(monkeypatch):
    """Every test starts as a fresh process would: no session in hand."""
    monkeypatch.setattr(downloadgemist, "_live_sessions", set())


@pytest.fixture
def clock(monkeypatch):
    fake = Clock()
    monkeypatch.setattr(downloadgemist, "time", fake)
    return fake


class Site:
    """A scripted hyperbridge.php: answers each mode from a queue or a default."""

    def __init__(self):
        self.calls = []
        self.answers = {
            "get_streams": [STREAMS],
            "download_stream": [{"download_progress": "100%", "URL": FILE_URL}],
            "get_cachestatus": ["100%"],
        }
        self.downloads = []

    def call(self, **form):
        self.calls.append(form)
        queue = self.answers[form["mode"]]
        answer = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(answer, Exception):
            raise answer
        return answer

    def download(self, file_url, path, *, timeout):
        self.downloads.append(file_url)
        with open(path, "wb") as fh:
            fh.write(b"episode")

    def modes(self):
        return [c["mode"] for c in self.calls]


@pytest.fixture
def site(monkeypatch):
    fake = Site()
    monkeypatch.setattr(downloadgemist, "_call", fake.call)
    monkeypatch.setattr(downloadgemist, "_download", fake.download)
    return fake


# --- looking up -----------------------------------------------------------


def test_streams_reads_the_qualities_on_offer(clock, site):
    lookup = downloadgemist.streams(EPISODE)

    assert site.calls == [{"mode": "get_streams", "URL": EPISODE, "sessionID": "false"}]
    assert lookup.session_id == "1790595490-7408"
    assert lookup.duration == 1517
    assert [r.label for r in lookup.video] == ["720p", "360p"]
    assert [r.label for r in lookup.audio] == ["128Kbps"]
    assert lookup.video[1].height == 360


def test_streams_asks_once_for_a_few_hours(clock, site):
    downloadgemist.streams(EPISODE)
    downloadgemist.streams(EPISODE)

    assert site.modes() == ["get_streams"]


def test_streams_survives_the_cache_being_saved_and_read_again(clock, site):
    downloadgemist.streams(EPISODE)
    cache.save()
    cache._data = None  # as a new run would find it

    lookup = downloadgemist.streams(EPISODE)

    assert site.modes() == ["get_streams"]
    assert lookup.video[0].label == "720p"


# --- fetching -------------------------------------------------------------


def test_fetch_downloads_a_file_that_is_ready(clock, site, tmp_path):
    lookup = downloadgemist.streams(EPISODE)

    path = downloadgemist.fetch(lookup, "360p", str(tmp_path), timeout=900)

    assert path == os.path.join(tmp_path, "video.mp4")
    assert site.downloads == [FILE_URL]
    assert site.calls[1] == {
        "mode": "download_stream",
        "stream": "360p",
        "sessionID": "1790595490-7408",
        "download_subtitles": "false",
        "download_with_audiodescription": "false",
    }


def test_fetch_waits_for_the_site_to_fetch_the_file_first(clock, site, tmp_path):
    site.answers["download_stream"] = [
        {"download_progress": "0%"},
        {"download_progress": "100%", "URL": FILE_URL},
    ]
    site.answers["get_cachestatus"] = ["20%", "70%", "100%"]
    lookup = downloadgemist.streams(EPISODE)

    downloadgemist.fetch(lookup, "360p", str(tmp_path), timeout=900)

    assert site.modes() == [
        "get_streams",
        "download_stream",
        "get_cachestatus",
        "get_cachestatus",
        "get_cachestatus",
        "download_stream",
    ]
    # Polled at the site's own leisurely pace, not in a tight loop.
    assert clock.slept == [downloadgemist.POLL_INTERVAL] * 3
    # The second ask is for the url only: the quality was chosen already.
    assert site.calls[-1] == {"mode": "download_stream", "sessionID": "1790595490-7408"}


def test_fetch_gives_up_for_this_run_if_the_file_takes_too_long(clock, site, tmp_path):
    site.answers["download_stream"] = [{"download_progress": "0%"}]
    site.answers["get_cachestatus"] = ["10%"]
    lookup = downloadgemist.streams(EPISODE)

    with pytest.raises(FeedError, match="still preparing"):
        downloadgemist.fetch(lookup, "360p", str(tmp_path), timeout=60)

    assert site.downloads == []


def test_fetch_asks_afresh_for_a_session_from_an_earlier_run(clock, site, tmp_path):
    """The session's cookie died with the run that got it."""
    downloadgemist.streams(EPISODE)
    downloadgemist._live_sessions.clear()
    lookup = downloadgemist.streams(EPISODE)  # from the cache

    downloadgemist.fetch(lookup, "360p", str(tmp_path), timeout=900)

    assert site.modes() == ["get_streams", "get_streams", "download_stream"]


def test_fetch_asks_afresh_when_its_session_has_gone_stale(clock, site, tmp_path):
    lookup = downloadgemist.streams(EPISODE)
    clock.advance(downloadgemist.SESSION_FRESH + 1)

    downloadgemist.fetch(lookup, "360p", str(tmp_path), timeout=900)

    assert site.modes() == ["get_streams", "get_streams", "download_stream"]


# --- the site's own wait --------------------------------------------------


def test_a_download_starts_the_sites_wait(clock, site, tmp_path):
    lookup = downloadgemist.streams(EPISODE)
    downloadgemist.fetch(lookup, "360p", str(tmp_path), timeout=900)

    assert downloadgemist.wait_left() == pytest.approx(1517)

    # The next episode is not even looked up while it runs.
    with pytest.raises(FeedError, match="min left"):
        downloadgemist.streams(OTHER)
    assert site.modes() == ["get_streams", "download_stream"]


def test_the_wait_ends(clock, site, tmp_path):
    lookup = downloadgemist.streams(EPISODE)
    downloadgemist.fetch(lookup, "360p", str(tmp_path), timeout=900)
    clock.advance(1518)

    downloadgemist.streams(OTHER)

    assert site.modes()[-1] == "get_streams"


def test_the_wait_is_written_down_at_once(clock, site, tmp_path):
    """A run killed right after a download must still leave the site alone."""
    lookup = downloadgemist.streams(EPISODE)
    downloadgemist.fetch(lookup, "360p", str(tmp_path), timeout=900)
    cache._data = None  # forget what was in memory; only the file remains

    assert downloadgemist.wait_left() > 0


# --- backing off ----------------------------------------------------------


def test_a_failure_backs_off_without_asking_again(clock, site):
    site.answers["get_streams"] = [FeedError("Downloadgemist said: Er ging iets mis")]

    with pytest.raises(FeedError, match="iets mis"):
        downloadgemist.streams(EPISODE)
    with pytest.raises(FeedError, match="not asking again before"):
        downloadgemist.streams(EPISODE)

    assert site.modes() == ["get_streams"]


def test_the_backoff_doubles_and_is_capped(clock, site):
    site.answers["get_streams"] = [FeedError("nope")]
    waits = []
    # Seven failures: 1 + 2 + 4 + 8 + 16 + 24 hours is still inside the three
    # days an episode is given before it is let go.
    for _ in range(7):
        with pytest.raises(FeedError):
            downloadgemist.streams(EPISODE)
        failed = cache.get("downloadgemist_failed", EPISODE)
        waits.append(failed["next_at"] - clock.now)
        clock.advance(failed["next_at"] - clock.now)

    hour = 60 * 60
    assert waits[:5] == [hour, 2 * hour, 4 * hour, 8 * hour, 16 * hour]
    assert waits[5:] == [24 * hour, 24 * hour]


def test_an_episode_failing_for_days_is_let_go(clock, site):
    site.answers["get_streams"] = [FeedError("nope")]
    with pytest.raises(FeedError):
        downloadgemist.streams(EPISODE)

    clock.advance(downloadgemist.GIVE_UP_AFTER)

    with pytest.raises(VideoGone, match="giving up"):
        downloadgemist.streams(EPISODE)


def test_one_episode_failing_does_not_hold_back_another(clock, site):
    site.answers["get_streams"] = [FeedError("nope"), STREAMS]
    with pytest.raises(FeedError):
        downloadgemist.streams(EPISODE)

    assert downloadgemist.streams(OTHER).session_id == "1790595490-7408"


def test_a_failed_download_counts_too(clock, site, tmp_path, monkeypatch):
    lookup = downloadgemist.streams(EPISODE)

    def broken(file_url, path, *, timeout):
        raise FeedError("Could not download: reset")

    monkeypatch.setattr(downloadgemist, "_download", broken)

    with pytest.raises(FeedError, match="reset"):
        downloadgemist.fetch(lookup, "360p", str(tmp_path), timeout=900)
    assert cache.get("downloadgemist_failed", EPISODE)["count"] == 1


def test_success_clears_the_record(clock, site, tmp_path):
    site.answers["get_streams"] = [FeedError("nope"), STREAMS]
    with pytest.raises(FeedError):
        downloadgemist.streams(EPISODE)
    clock.advance(downloadgemist.BACKOFF_FIRST)

    lookup = downloadgemist.streams(EPISODE)
    downloadgemist.fetch(lookup, "360p", str(tmp_path), timeout=900)

    assert cache.get("downloadgemist_failed", EPISODE) is None


# --- the wire -------------------------------------------------------------


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_call_posts_the_form_and_returns_the_data(monkeypatch):
    sent = []

    def fake_open(request, timeout):
        sent.append(request)
        return _Response(json.dumps({"status": "OK", "data": {"x": 1}}).encode())

    monkeypatch.setattr(downloadgemist, "_open", fake_open)

    assert downloadgemist._call(mode="get_streams", URL=EPISODE) == {"x": 1}
    request = sent[0]
    assert request.full_url == downloadgemist.HYPERBRIDGE
    assert b"mode=get_streams" in request.data
    assert request.get_header("Referer") == "https://downloadgemist.nl/"


def test_call_passes_on_what_the_site_said(monkeypatch):
    monkeypatch.setattr(
        downloadgemist,
        "_open",
        lambda request, timeout: _Response(
            json.dumps({"status": "error", "data": "Geen afleveringsinformatie"}).encode()
        ),
    )

    with pytest.raises(FeedError, match="Geen afleveringsinformatie"):
        downloadgemist._call(mode="get_streams", URL=EPISODE)


def test_call_complains_about_something_that_is_not_json(monkeypatch):
    monkeypatch.setattr(
        downloadgemist, "_open", lambda request, timeout: _Response(b"<html>")
    )

    with pytest.raises(FeedError, match="isn't json"):
        downloadgemist._call(mode="get_streams", URL=EPISODE)


def test_call_strips_the_html_from_what_the_site_said(monkeypatch):
    busy = (
        "Er zijn op het moment 6 van de 5 actieve downloads op de server. "
        "Als je een <a href='?pagina=donaties'>donatie</a> hebt gedaan kun je verder."
    )
    monkeypatch.setattr(
        downloadgemist,
        "_open",
        lambda request, timeout: _Response(
            json.dumps({"status": "error", "data": busy}).encode()
        ),
    )

    with pytest.raises(FeedError) as caught:
        downloadgemist._call(mode="download_stream", sessionID="1")

    assert "6 van de 5 actieve downloads" in str(caught.value)
    assert "<a" not in str(caught.value)


# --- the file's own url ---------------------------------------------------

RAW_FILE_URL = (
    "https://downloadgemist.nl/cache/2026/10/04/"
    "DownloadGemist [2026-10-04] Bureau Buitenland s02e29 (720p_2).mp4"
)


@pytest.mark.parametrize(
    "given, expected",
    [
        # As the site really hands it over: spaces and brackets, for a browser.
        (
            RAW_FILE_URL,
            "https://downloadgemist.nl/cache/2026/10/04/DownloadGemist%20[2026-10-04]"
            "%20Bureau%20Buitenland%20s02e29%20(720p_2).mp4",
        ),
        # Already escaped: left exactly as it is, not escaped twice.
        (FILE_URL, FILE_URL),
        # A title with an accent in it.
        (
            "https://downloadgemist.nl/cache/Oekraïne.mp4",
            "https://downloadgemist.nl/cache/Oekra%C3%AFne.mp4",
        ),
        # Relative to the site.
        ("cache/a b.mp4", "https://downloadgemist.nl/cache/a%20b.mp4"),
        # A query survives as a query.
        (
            "https://files.example.com/get?file=a b.mp4&token=x%2Fy",
            "https://files.example.com/get?file=a%20b.mp4&token=x%2Fy",
        ),
    ],
)
def test_safe_url(given, expected):
    assert downloadgemist._safe_url(given) == expected


def test_download_asks_for_a_url_with_spaces_in_it_escaped(monkeypatch, tmp_path):
    asked = []

    def fake_open(request, timeout):
        asked.append(request.full_url)
        return _Response(b"episode")

    monkeypatch.setattr(downloadgemist, "_open", fake_open)
    path = str(tmp_path / "video.mp4")

    downloadgemist._download(RAW_FILE_URL, path, timeout=60)

    assert " " not in asked[0]
    assert asked[0].endswith("%20(720p_2).mp4")
    with open(path, "rb") as fh:
        assert fh.read() == b"episode"


def test_download_really_gets_past_http_client_with_such_a_url(monkeypatch, tmp_path):
    """The real opener, stopped just short of the network.

    The unescaped url never got as far as a socket: http.client refused to put
    it on the wire. Escaped, it has to reach the connect.
    """
    import socket

    def no_network(*args, **kwargs):
        raise OSError("no network in tests")

    monkeypatch.setattr(socket, "create_connection", no_network)

    with pytest.raises(FeedError, match="no network in tests"):
        downloadgemist._download(RAW_FILE_URL, str(tmp_path / "v.mp4"), timeout=5)


def test_a_complaint_from_http_client_is_a_failure_like_any_other(
    clock, site, tmp_path, monkeypatch
):
    """Counted, so the episode backs off instead of being asked for every run."""
    import http.client

    def refuses(request, timeout):
        raise http.client.InvalidURL("URL can't contain control characters.")

    lookup = downloadgemist.streams(EPISODE)
    # The site fixture fakes the download whole; this test wants the real one,
    # failing where the real one failed.
    monkeypatch.setattr(downloadgemist, "_download", _REAL_DOWNLOAD)
    monkeypatch.setattr(downloadgemist, "_open", refuses)

    with pytest.raises(FeedError, match="control characters"):
        downloadgemist.fetch(lookup, "360p", str(tmp_path), timeout=900)

    assert cache.get("downloadgemist_failed", EPISODE)["count"] == 1
