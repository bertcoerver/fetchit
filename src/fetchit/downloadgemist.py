"""Fetch an NPO Start episode through downloadgemist.nl, gently.

NPO's own player hands out streams through a token dance that changes whenever
NPO feels like it. Downloadgemist is a free, hobby-run site that has kept up with
that for years, and behind its page is one small endpoint,
``core/hyperbridge.php``, that the page itself posts to:

1. ``get_streams`` with the episode url — a session id, the duration, and the
   qualities on offer (``1080p`` … ``360p``, and audio-only ``256Kbps`` /
   ``128Kbps``), each with a bitrate;
2. ``download_stream`` with one of those — either the file's url straight away,
   or the news that the site is fetching it into its own cache first;
3. ``get_cachestatus`` until that says ``100%``, then ``download_stream`` again
   for the url.

The session id is only half of a session: the other half is a PHP session
cookie set by the first answer, without which the second is a bare
``download_stream_empty_check``. So every call goes through one cookie jar, and
a session remembered from an earlier run — whose cookie died with it — is
looked up afresh before it is used.

It is somebody's free service, and fetchit runs several times a day unattended,
so everything here leans towards asking less:

- **Only when there is something to download.** The listing comes from npo.nl (see
  :mod:`fetchit.npo`); this module is only reached for an episode that is new.
- **The site's own wait.** Its page makes a visitor wait as long as the episode
  lasts before the next download. The page enforces that in the browser, where a
  script would never see it, so it is kept here instead: no new download starts
  until the previous one's duration has passed. A run that meets the wait leaves
  the episode for a later run.
- **Backing off an episode that fails.** One hour, then two, four, … up to a
  day, without a single request in between. An episode that has failed for
  :data:`GIVE_UP_AFTER` is let go for good (:class:`~fetchit.media.VideoGone`)
  so it can't hold its feed's later episodes back forever.
- **One lookup per episode per few hours.** A dry run, and the resolve that
  precedes every real download, share one remembered ``get_streams`` answer.
- **Dry runs never start a download.** Only ``download_stream`` makes the site do
  work, and only :func:`fetch` calls it.

The waits and failures are kept in :mod:`fetchit.cache`. They decide *when* an
episode is asked for, never *whether* it is wanted, which is what that file
is for; losing it costs at most one request sooner than planned.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime
from http.client import HTTPException
from http.cookiejar import CookieJar
from urllib.error import URLError
from urllib.parse import urlencode, urljoin, urlparse
from urllib.request import HTTPCookieProcessor, Request, build_opener

from . import cache, timing
from .download import safe_url
from .feeds import FeedError, retrying, strip_html
from .media import VideoGone

SITE = "https://downloadgemist.nl/"
HYPERBRIDGE = "https://downloadgemist.nl/core/hyperbridge.php"

# Who is asking, said plainly, and from where the site's own page would ask.
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; fetchit)",
    "Referer": "https://downloadgemist.nl/",
    "X-Requested-With": "XMLHttpRequest",
}

# The page polls the cache status every eight seconds; a little slower here.
POLL_INTERVAL = 10

# A small json answer; a minute is already generous.
CALL_TIMEOUT = 60

# How long a ``get_streams`` answer is trusted for describing an episode. The
# qualities on offer don't change; the session id in it goes stale sooner, which
# is why :func:`fetch` asks afresh when its lookup is older than SESSION_FRESH.
LOOKUP_TTL = 6 * 60 * 60
SESSION_FRESH = 10 * 60

# The backoff for an episode that fails, doubling from the first to the last.
BACKOFF_FIRST = 60 * 60
BACKOFF_MAX = 24 * 60 * 60

# How long an episode may keep failing before it is let go for good.
GIVE_UP_AFTER = 3 * 24 * 60 * 60

# The site's PHP session, shared by every call this process makes, and the
# session ids that were handed out under it — the only ones worth using.
_cookies = CookieJar()
_opener = build_opener(HTTPCookieProcessor(_cookies))
_live_sessions: set[str] = set()

_LOOKUP_NS = "downloadgemist_lookup"
_LAST_NS = "downloadgemist_last"
_FAILED_NS = "downloadgemist_failed"
_LAST_KEY = "download"


@dataclass(frozen=True)
class Rung:
    """One quality Downloadgemist offers: its label, and bits per second."""

    label: str
    bitrate: int

    @property
    def is_audio(self) -> bool:
        return "kbps" in self.label.lower()

    @property
    def height(self) -> int:
        digits = "".join(ch for ch in self.label.split("p")[0] if ch.isdigit())
        return int(digits) if digits and not self.is_audio else 0


@dataclass(frozen=True)
class Lookup:
    """A ``get_streams`` answer for one episode url."""

    url: str
    session_id: str
    duration: float
    rungs: tuple[Rung, ...]
    looked_up_at: float

    @property
    def video(self) -> list[Rung]:
        return [r for r in self.rungs if not r.is_audio]

    @property
    def audio(self) -> list[Rung]:
        return [r for r in self.rungs if r.is_audio]


def streams(url: str) -> Lookup:
    """What Downloadgemist offers for episode ``url``, remembered for a while.

    Raises :class:`~fetchit.feeds.FeedError` while the site's wait is on or the
    episode is backing off (both without a request), or if the site says no;
    :class:`~fetchit.media.VideoGone` once the episode has failed for too long.
    """
    _check_allowed(url)

    hit = cache.get(_LOOKUP_NS, url, max_age=LOOKUP_TTL)
    if isinstance(hit, dict):
        try:
            return _lookup_from(url, hit)
        except (KeyError, TypeError, ValueError):
            pass  # A remembered answer in an old shape is no answer.

    return _look_up(url)


def fetch(lookup: Lookup, label: str, into: str, *, timeout: float) -> str:
    """Download ``label`` of ``lookup``'s episode into ``into``; return its path.

    Raises :class:`~fetchit.feeds.FeedError` if the site says no, the file is
    still being prepared when ``timeout`` runs out, or the download fails — all
    of which the next run will try again, subject to the backoff.
    """
    deadline = time.monotonic() + timeout
    url = lookup.url
    _check_allowed(url)

    stale = time.time() - lookup.looked_up_at > SESSION_FRESH
    if stale or lookup.session_id not in _live_sessions:
        lookup = _look_up(url)
    session = lookup.session_id

    data = _guarded(
        url,
        lambda: _call(
            mode="download_stream",
            stream=label,
            sessionID=session,
            download_subtitles="false",
            download_with_audiodescription="false",
        ),
    )
    if _field(data, "download_progress") != "100%":
        while True:
            if time.monotonic() + POLL_INTERVAL > deadline:
                raise _failed(
                    url,
                    FeedError(
                        "Downloadgemist is still preparing the file; "
                        "trying again next run"
                    ),
                )
            time.sleep(POLL_INTERVAL)
            progress = _guarded(
                url, lambda: _call(mode="get_cachestatus", sessionID=session)
            )
            if progress == "100%":
                break
        data = _guarded(
            url, lambda: _call(mode="download_stream", sessionID=session)
        )

    file_url = _field(data, "URL")
    if not file_url:
        raise _failed(url, FeedError("Downloadgemist gave no file to download"))

    # The site's clock starts when a visitor gets the file, so this one does too.
    _started(lookup.duration)

    path = os.path.join(into, "video.mp4")
    remaining = max(60.0, deadline - time.monotonic())
    try:
        _download(file_url, path, timeout=remaining)
    except FeedError as exc:
        raise _failed(url, exc) from exc

    _succeeded(url)
    return path


def wait_left(now: float | None = None) -> float:
    """Seconds until the site's wait after the last download is over."""
    last = cache.get(_LAST_NS, _LAST_KEY)
    if not isinstance(last, dict):
        return 0.0
    try:
        until = float(last["at"]) + float(last["wait"])
    except (KeyError, TypeError, ValueError):
        return 0.0
    return max(0.0, until - (time.time() if now is None else now))


# --- the gentle part --------------------------------------------------------


def _check_allowed(url: str) -> None:
    """Refuse — without a request — while waiting, backing off, or given up."""
    failed = cache.get(_FAILED_NS, url)
    if isinstance(failed, dict):
        now = time.time()
        first = float(failed.get("first_at") or now)
        if now - first >= GIVE_UP_AFTER:
            raise VideoGone(
                f"Downloadgemist has failed on this episode since "
                f"{_clock(first)} ({failed.get('reason') or 'no reason given'}); "
                "giving up on it"
            )
        next_at = float(failed.get("next_at") or 0)
        if now < next_at:
            raise FeedError(
                f"Downloadgemist failed on this episode {failed.get('count', 1)}x "
                f"({failed.get('reason') or 'no reason given'}); not asking "
                f"again before {_clock(next_at)}"
            )

    left = wait_left()
    if left > 0:
        raise FeedError(
            f"waiting for Downloadgemist: {left / 60:.0f} min left of the pause "
            "after the last download"
        )


def _guarded(url: str, call):
    """Run ``call``; if it fails, count it against ``url`` and re-raise."""
    try:
        return call()
    except FeedError as exc:
        raise _failed(url, exc) from exc


def _failed(url: str, exc: FeedError) -> FeedError:
    """Note a failure for ``url`` and schedule its next attempt; return ``exc``."""
    now = time.time()
    previous = cache.get(_FAILED_NS, url)
    count = 1
    first = now
    if isinstance(previous, dict):
        count = int(previous.get("count") or 0) + 1
        first = float(previous.get("first_at") or now)
    wait = min(BACKOFF_MAX, BACKOFF_FIRST * 2 ** (count - 1))
    cache.put(
        _FAILED_NS,
        url,
        {"count": count, "first_at": first, "next_at": now + wait, "reason": str(exc)},
    )
    # Written now rather than at the end of the run: a run killed after this
    # must not forget that it should leave the site alone for a while.
    cache.save()
    return exc


def _succeeded(url: str) -> None:
    cache.put(_FAILED_NS, url, None)
    cache.save()


def _started(duration: float) -> None:
    cache.put(_LAST_NS, _LAST_KEY, {"at": time.time(), "wait": float(duration)})
    cache.save()


def _clock(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M")


# --- talking to it ----------------------------------------------------------


def _look_up(url: str) -> Lookup:
    data = _guarded(url, lambda: _call(mode="get_streams", URL=url, sessionID="false"))
    try:
        stored = {
            "session_id": str(data["sessionID"]),
            "duration": float(data["duration"]),
            "rungs": [
                [str(s["streamlabel"]), int(s.get("bitrate") or 0)]
                for s in data["streams"]
            ],
            "looked_up_at": time.time(),
        }
        lookup = _lookup_from(url, stored)
    except (KeyError, TypeError, ValueError) as exc:
        raise _failed(
            url, FeedError(f"Unexpected answer from Downloadgemist: {exc}")
        ) from exc
    if not lookup.rungs:
        raise _failed(url, FeedError("Downloadgemist offered no streams"))
    cache.put(_LOOKUP_NS, url, stored)
    _live_sessions.add(lookup.session_id)
    return lookup


def _lookup_from(url: str, stored: dict) -> Lookup:
    return Lookup(
        url=url,
        session_id=str(stored["session_id"]),
        duration=float(stored["duration"]),
        rungs=tuple(Rung(str(label), int(rate)) for label, rate in stored["rungs"]),
        looked_up_at=float(stored["looked_up_at"]),
    )


def _field(data: object, name: str) -> str:
    if isinstance(data, dict):
        value = data.get(name)
        return str(value) if value is not None else ""
    return ""


def _call(**form: str):
    """Post ``form`` to the endpoint and return the ``data`` of an OK answer."""

    def once():
        request = Request(
            HYPERBRIDGE,
            data=urlencode(form).encode(),
            headers={
                **HEADERS,
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            },
        )
        try:
            with _open(request, timeout=CALL_TIMEOUT) as response:
                return response.read().decode("utf-8", errors="replace")
        except (URLError, OSError, ValueError, HTTPException) as exc:
            raise FeedError(f"Could not reach Downloadgemist: {exc}") from exc

    with timing.step("downloadgemist", form.get("mode", "")):
        raw = retrying(once)
    try:
        answer = json.loads(raw)
    except ValueError as exc:
        raise FeedError(
            f"Downloadgemist answered with something that isn't json: {raw[:120]!r}"
        ) from exc
    if not isinstance(answer, dict) or answer.get("status") != "OK":
        # Written for a web page — "6 van de 5 actieve downloads" arrives with a
        # donation link in it — and read here in a log.
        message = answer.get("data") if isinstance(answer, dict) else answer
        raise FeedError(f"Downloadgemist said: {strip_html(str(message))}")
    return answer.get("data")


def _open(request: Request, *, timeout: float):
    """Open ``request`` with the site's session cookie along."""
    return _opener.open(request, timeout=timeout)


def _safe_url(file_url: str) -> str:
    """``file_url`` as something that can be requested.

    The site names its files for people — ``…/DownloadGemist [2026-10-04] Bureau
    Buitenland s02e29 (720p_2).mp4`` — and hands the link over exactly like
    that, spaces and all, for a browser to tidy up. See
    :func:`~fetchit.download.safe_url`, which does the tidying; this adds only
    that a link without a host is the site's own.
    """
    return safe_url(urljoin(SITE, file_url.strip()))


def _download(file_url: str, path: str, *, timeout: float) -> None:
    """Stream ``file_url`` into ``path``, starting over if the network drops."""
    file_url = _safe_url(file_url)

    def once() -> None:
        request = Request(file_url, headers=HEADERS)
        try:
            with _open(request, timeout=timeout) as response, open(path, "wb") as out:
                while chunk := response.read(256 * 1024):
                    out.write(chunk)
        # HTTPException too: http.client's complaints (a url it won't send, a
        # response cut short) are not OSErrors, and one that escaped as itself
        # would skip the backoff below and be asked for again every run.
        except (URLError, OSError, ValueError, HTTPException) as exc:
            raise FeedError(f"Could not download {file_url!r}: {exc}") from exc

    with timing.step("http download", urlparse(file_url).netloc):
        retrying(once)
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        raise FeedError("Downloadgemist's file came back empty")
