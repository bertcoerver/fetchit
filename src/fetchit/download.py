"""Fetch things over HTTP: attachments to a temporary path, small text inline.

Every network read outside feedparser goes through this module's ``urlopen``, so
there is one place to patch in tests and one place to change if this ever needs a
session, a user agent, or a retry.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from . import timing
from .feeds import FeedError, retrying

# What a fetch can fail with. ``HTTPException`` is on the list because
# http.client's own complaints — a url it won't put on the wire, a response cut
# short — are not OSErrors, and one that got out as itself would not be a
# :class:`FeedError` to anything that knows how to handle those.
_FETCH_ERRORS = (URLError, OSError, ValueError, HTTPException)

# Everything a url's path or query may carry as it stands. Deliberately
# generous: the point of :func:`safe_url` is to fix what cannot be sent, not to
# normalise what can, because a signed link is signed over its exact bytes.
_PATH_SAFE = "/%:@!$&'()*+,;=~[]"
_QUERY_SAFE = _PATH_SAFE + "?"


def safe_url(url: str) -> str:
    """``url`` as something that can be requested, changed as little as possible.

    Links are written for people as often as for programs. A feed's enclosure
    or a download site's file link can arrive as ``…/Show [2026-10-04] Episode
    (720p).mp4``, spaces and all, on the understanding that a browser will tidy
    it up. Nothing tidied it up here, and http.client refuses a url with a space
    in it outright.

    So this does what a browser does and no more: spaces, non-ASCII and the few
    characters that can't travel are percent-encoded; the fragment, which is
    never sent, is dropped. ``%`` is left alone so a link that arrives already
    escaped isn't escaped twice, and a url that was fine comes back unchanged.
    """
    parts = urlsplit(url.strip())
    return urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            quote(parts.path, safe=_PATH_SAFE),
            quote(parts.query, safe=_QUERY_SAFE),
            "",
        )
    )


# A reachability probe is answering one question — is this host there — and must
# not become the slowest part of finding out that it isn't.
PROBE_TIMEOUT = 5


def reachable(url: str, *, timeout: float = PROBE_TIMEOUT) -> bool:
    """Whether ``url``'s host answers a HEAD request right now.

    The cheap equivalent of ``curl -sI``: headers only, one attempt, no retry —
    this exists to find out quickly that something is unreachable, so retrying it
    would defeat the point.

    An HTTP error status counts as reachable. A 403 or a 404 is the host
    answering, which is all that is being asked; only a connection that cannot be
    made at all — a name that won't resolve, a refused port, a timeout — is a
    "no".
    """
    try:
        with urlopen(Request(safe_url(url), method="HEAD"), timeout=timeout):
            return True
    except HTTPError:
        return True
    except _FETCH_ERRORS:
        return False


def suffix_for(url: str, default: str = ".mp3") -> str:
    """Return a filename suffix for ``url`` (e.g. ``.mp3``), or ``default``."""
    path = urlparse(url).path
    _, dot, ext = path.rpartition(".")
    if dot and ext and len(ext) <= 5 and "/" not in ext:
        return "." + ext
    return default


def fetch_text(
    url: str, *, timeout: float = 30, headers: dict[str, str] | None = None
) -> str:
    """Fetch ``url`` and return its body decoded as UTF-8.

    For the small text resources that describe media rather than being it — an
    API response, an HLS playlist. Anything big enough to be worth streaming to
    disk wants :func:`download_temp` instead. ``headers`` are sent with the
    request, for a site that wants to be asked in a particular way.

    Raises :class:`FeedError` if the fetch fails, so callers can treat a dead
    endpoint the same way they treat a dead feed. A fetch that fails for a
    network reason is retried first; see :func:`~fetchit.feeds.retrying`.
    """

    def once() -> str:
        try:
            request = Request(safe_url(url), headers=headers or {})
            with urlopen(request, timeout=timeout) as response:
                return response.read().decode("utf-8", errors="replace")
        except _FETCH_ERRORS as exc:
            raise FeedError(f"Could not fetch {url!r}: {exc}") from exc

    with timing.step("http fetch", urlparse(url).netloc):
        return retrying(once)


@contextmanager
def download_temp(
    url: str, *, timeout: float = 30, default_suffix: str = ".mp3"
) -> Iterator[str]:
    """Download ``url`` to a temp file, yield its path, and delete it on exit.

    ``default_suffix`` is used when the url has no usable extension; what reads
    the file afterwards goes by its name, so artwork downloads pass an image
    suffix.

    Raises :class:`FeedError` if the download fails. The file is always removed
    when the ``with`` block ends, whether or not sending succeeded. A download
    cut short by the network is retried from the top — the partial bytes are
    thrown away first, so a retry never appends to half a file.
    """
    fd, path = tempfile.mkstemp(suffix=suffix_for(url, default_suffix))
    try:
        # os.fdopen takes ownership of fd, closing it when ``out`` closes.
        with os.fdopen(fd, "wb") as out:

            def once() -> None:
                out.seek(0)
                out.truncate()
                try:
                    with urlopen(safe_url(url), timeout=timeout) as response:
                        while chunk := response.read(64 * 1024):
                            out.write(chunk)
                except _FETCH_ERRORS as exc:
                    raise FeedError(f"Could not download {url!r}: {exc}") from exc

            with timing.step("http download", urlparse(url).netloc):
                retrying(once)
        yield path
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def download_to(
    url: str, path: str, *, timeout: float = 30, headers: dict[str, str] | None = None
) -> None:
    """Download ``url`` to ``path``, leaving nothing at ``path`` unless it all arrived.

    The bytes go to a ``.part`` file beside ``path`` and are renamed over it
    once complete, so whatever reads the library never sees half a file under
    the real name. ``headers`` go with the request, for a site that wants to
    be asked in a particular way. Raises :class:`FeedError` if the download
    fails; a download cut short by the network is retried from the top first.
    """
    partial = path + ".part"
    try:
        with open(partial, "wb") as out:

            def once() -> None:
                out.seek(0)
                out.truncate()
                request = Request(safe_url(url), headers=headers or {})
                try:
                    with urlopen(request, timeout=timeout) as response:
                        while chunk := response.read(64 * 1024):
                            out.write(chunk)
                except _FETCH_ERRORS as exc:
                    raise FeedError(f"Could not download {url!r}: {exc}") from exc

            with timing.step("http download", urlparse(url).netloc):
                retrying(once)
        os.replace(partial, path)
    finally:
        try:
            os.unlink(partial)
        except OSError:
            pass
