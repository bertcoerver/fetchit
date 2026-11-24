"""Tell other programs that a run downloaded something.

After a run that filed at least one new item, fetchit POSTs to every URL in
``FETCHIT_NOTIFY`` (comma- or space-separated)::

    {"source": "fetchit", "fetched": 2, "feeds": ["Bureau Buitenland", "3Blue1Brown"]}

``feeds`` are the names in fetchit.json of the feeds that got something new —
the names the library's ``feed.json`` files record, so a reader of the library
can tell whether the news is for it. rssignal's ``/api/fetched`` takes exactly
this, and starts a run of its own when one of the feeds is one it sends from.

``FETCHIT_NOTIFY_TOKEN``, if set, goes along as ``Authorization: Bearer …``.

Best effort: a URL that doesn't answer is logged and passed over. The download
happened either way, and whoever missed the news finds it on its own schedule.
"""

from __future__ import annotations

import json
import os
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from . import __version__
from .errorlog import log_line

NOTIFY_ENV = "FETCHIT_NOTIFY"
NOTIFY_TOKEN_ENV = "FETCHIT_NOTIFY_TOKEN"
TIMEOUT = 10


def targets() -> list[str]:
    """The URLs in :data:`NOTIFY_ENV`, in the order given."""
    return [u for u in re.split(r"[\s,]+", os.environ.get(NOTIFY_ENV, "")) if u]


def notify(feeds: list[str], fetched: int) -> int:
    """POST the news to every target. Returns how many took it."""
    urls = targets()
    if not urls or not feeds:
        return 0
    body = json.dumps({"source": "fetchit", "fetched": fetched, "feeds": feeds}).encode()
    headers = {"Content-Type": "application/json", "User-Agent": f"fetchit/{__version__}"}
    token = os.environ.get(NOTIFY_TOKEN_ENV, "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    told = 0
    for url in urls:
        try:
            with urlopen(Request(url, data=body, headers=headers, method="POST"), timeout=TIMEOUT) as response:
                response.read()
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace").strip()[:200]
            _missed(url, f"HTTP {exc.code}{': ' + detail if detail else ''}")
        except (URLError, OSError, ValueError) as exc:
            _missed(url, str(getattr(exc, "reason", exc)))
        else:
            told += 1
            print(f"Told {url} about {', '.join(feeds)}.")
            log_line(f"notified {url}: {', '.join(feeds)}")
    return told


def _missed(url: str, why: str) -> None:
    print(f"Could not tell {url} about the new downloads: {why}", file=sys.stderr)
    log_line(f"notify {url} failed: {why}")
