"""Read NPO Start: a series as a feed, and an episode's video via Downloadgemist.

NPO publishes no RSS for its television, so following a series means reading the
series page itself. That is less fragile than it sounds: npo.nl is a Next.js
site, and every page carries the data it was rendered from in a
``<script id="__NEXT_DATA__">`` block — the series, its seasons, and every
episode of the season on show, each with its title, synopsis, artwork, duration
and the moment it was first broadcast. One request, and no HTML is parsed.

Two kinds of url are accepted, and they behave differently on purpose:

- ``https://npo.nl/start/serie/<series>/afleveringen`` (or just
  ``…/serie/<series>``) follows the **newest season**: the page shows that one
  first, so when season 3 starts the feed moves on with it.
- ``https://npo.nl/start/serie/<series>/afleveringen/seizoen-2`` stays on that
  season.

Each episode's link is its player page, ``https://npo.nl/start/afspelen/<slug>``,
and that is what :func:`~fetchit.media.is_video_item` recognises. The video is
fetched through :mod:`fetchit.downloadgemist`, which knows how to get it out of
NPO — and how to ask for it politely. The quality is chosen by the rule in
:mod:`fetchit.quality`: the fewest messages any quality fits in, then the best
quality that fits in that many.

The dispatch that decides an item is NPO's at all lives in :mod:`fetchit.media`;
this module only answers about NPO.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from . import downloadgemist
from .download import fetch_text
from .feeds import (
    FeedConfig,
    FeedError,
    FeedItem,
    ParsedFeed,
    apply_extracts,
    strip_html,
)
from .media import SIZE_ESTIMATE_MARGIN
from .quality import Quality, choose, in_parts, too_big

# A series page, with or without the episodes tab and a season. Anything past the
# season (an episode page) is deliberately not matched.
NPO_SERIES_RE = re.compile(
    r"^https?://(?:www\.)?npo\.nl/start/serie/(?P<series>[\w-]+)"
    r"(?:/afleveringen(?:/(?P<season>[\w-]+))?)?/?(?:[?#].*)?$",
    re.IGNORECASE,
)

# An episode's player page, in both the short form the site links to and the
# long form under its series and season.
NPO_EPISODE_RE = re.compile(
    r"^https?://(?:www\.)?npo\.nl/start/(?:afspelen/(?P<slug>[\w-]+)"
    r"|serie/[\w-]+/[\w-]+/(?P<long_slug>[\w-]+)/afspelen)/?(?:[?#].*)?$",
    re.IGNORECASE,
)

NPO_EPISODE_URL = "https://npo.nl/start/afspelen/{slug}"

# The page is behind Cloudflare, which is happier with a browser-shaped agent.
PAGE_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; fetchit)"}

_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__"[^>]*>(?P<json>.*?)</script>', re.DOTALL
)

# Since November 2026 the page is a Next.js App Router page: no __NEXT_DATA__,
# but its data streamed in as "flight" chunks, each a JSON array whose second
# element is a piece of the stream, in <script>self.__next_f.push([1,"…"])</script>.
_FLIGHT_RE = re.compile(r"self\.__next_f\.push\((?P<json>\[.*?\])\)\s*</script>", re.DOTALL)
_QUERIES_RE = re.compile(r'"queries"\s*:\s*\[')

# Downloadgemist lists video rungs without saying whether their bitrate includes
# the sound. Assuming it doesn't costs, at worst, a rung of quality; assuming it
# does could cost an extra part. See `resolve`.
AUDIO_ALLOWANCE = 128_000


@dataclass(frozen=True)
class NpoPlan:
    """What would be downloaded for an NPO episode, worked out without doing it.

    One implementation of the :class:`~fetchit.media.VideoPlan` protocol.
    """

    lookup: downloadgemist.Lookup
    rung: downloadgemist.Rung
    estimated_bytes: int
    parts: int = 1

    def describe(self) -> str:
        mb = self.estimated_bytes / (1024 * 1024)
        return f"{self.rung.label}, ~{mb:.0f} MB{in_parts(self.parts)}"

    def fetch(self, into: str, *, timeout: float) -> str:
        return downloadgemist.fetch(self.lookup, self.rung.label, into, timeout=timeout)


def npo_series(url: str | None) -> tuple[str, str | None] | None:
    """Return ``(series_slug, season_slug_or_None)`` for a series url, else ``None``."""
    if not url:
        return None
    match = NPO_SERIES_RE.match(url.strip())
    if not match:
        return None
    return match["series"].lower(), (match["season"] or None)


def npo_episode_slug(link: str | None) -> str | None:
    """Return the episode slug for an NPO Start player link, else ``None``."""
    if not link:
        return None
    match = NPO_EPISODE_RE.match(link.strip())
    if not match:
        return None
    return match["slug"] or match["long_slug"]


def parse_npo_series(cfg: FeedConfig) -> ParsedFeed:
    """Read an NPO Start series (or one season of it) as if it were a feed."""
    queries = _queries(cfg.url)
    series = _series(queries, cfg.url)
    programs = _programs(queries, cfg.url)

    show_title = str(series.get("title") or "")
    items = [
        apply_extracts(_item(program, cfg.name, show_title), cfg.extract)
        for program in programs
        if program.get("slug")
    ]
    return ParsedFeed(
        items=items,
        image_url=_image(series),
        description=strip_html(str(series.get("synopsis") or "")),
        title=show_title,
        link=cfg.url,
        author="NPO",
        language="nl",
    )


def series_image(url: str) -> str:
    """The series' own artwork, as the page shows it right now."""
    image = _image(_series(_queries(url), url))
    if image is None:
        raise FeedError(f"NPO series {url!r} has no image")
    return image


def resolve(item: FeedItem, *, quality: Quality = Quality()) -> NpoPlan:
    """Work out which quality ``item`` would be fetched at, and in how many parts.

    One ``get_streams`` question to Downloadgemist, and none at all if it was
    asked in the last few hours. Raises :class:`~fetchit.feeds.FeedError` if
    the item is not an NPO episode or Downloadgemist can't (or mustn't yet) be
    asked, :class:`~fetchit.media.VideoGone` if it has failed for days, and
    :class:`~fetchit.media.VideoTooBig` if no quality fits even split.
    """
    slug = npo_episode_slug(item.link)
    if not slug:
        raise FeedError(f"Not an NPO Start episode link: {item.link!r}")

    lookup = downloadgemist.streams(NPO_EPISODE_URL.format(slug=slug))
    # A rung listed without a bitrate (``720p_2`` is) has no size to estimate,
    # and counted as its sound alone it would look like the smallest thing on
    # offer rather than the 300 MB it is.
    rungs = [rung for rung in lookup.video if rung.bitrate > 0]
    if not rungs:
        raise FeedError(f"Downloadgemist offered no video for {slug}")

    def size(rung: downloadgemist.Rung) -> int:
        bits = (rung.bitrate + AUDIO_ALLOWANCE) * lookup.duration
        return int(bits / 8 * SIZE_ESTIMATE_MARGIN)

    picked = choose(
        rungs,
        size=size,
        rank=lambda r: (r.height, r.bitrate),
        height=lambda r: r.height,
        quality=quality,
    )
    if picked is None:
        smallest = min(rungs, key=size)
        raise too_big(
            f"Episode {slug} (even at {smallest.label})",
            size(smallest),
            quality,
        )
    rung, parts = picked
    return NpoPlan(lookup=lookup, rung=rung, estimated_bytes=size(rung), parts=parts)


# --- reading the page -------------------------------------------------------


def _queries(url: str) -> list[dict]:
    """The page's dehydrated react-query cache: where all the data lives."""
    found = npo_series(url)
    if not found:
        raise FeedError(f"Not an NPO Start series url: {url!r}")

    html = fetch_text(url, headers=PAGE_HEADERS)
    match = _NEXT_DATA_RE.search(html)
    if match:
        try:
            data = json.loads(match["json"])
            queries = data["props"]["pageProps"]["dehydratedState"]["queries"]
            if not isinstance(queries, list):
                raise TypeError("queries is not a list")
        except (ValueError, KeyError, TypeError) as exc:
            raise FeedError(f"Unexpected NPO page data for {url!r}: {exc}") from exc
        return queries

    queries = _flight_queries(html)
    if queries is None:
        raise FeedError(
            f"NPO page {url!r} carries neither __NEXT_DATA__ nor the series data "
            "in its streamed page data — NPO has changed its pages again."
        )
    return queries


def _flight_queries(html: str) -> list[dict] | None:
    """The react-query cache out of an App Router page's flight stream.

    The stream is the chunks' text put back together. Somewhere in it a client
    component is handed the dehydrated cache, ``{"mutations": [...],
    "queries": [...]}``, as plain JSON: the list that holds the series' own
    query is the one wanted. ``None`` if there is no such list.
    """
    stream = []
    for match in _FLIGHT_RE.finditer(html):
        try:
            chunk = json.loads(match["json"])
        except ValueError:
            continue
        if isinstance(chunk, list) and len(chunk) > 1 and isinstance(chunk[1], str):
            stream.append(chunk[1])
    text = "".join(stream)

    decoder = json.JSONDecoder()
    for found in _QUERIES_RE.finditer(text):
        try:
            queries, _ = decoder.raw_decode(text, found.end() - 1)
        except ValueError:
            continue
        if not isinstance(queries, list):
            continue
        queries = [q for q in queries if isinstance(q, dict)]
        if _query(queries, "series:detail-") is not None:
            return queries
    return None


def _query(queries: list[dict], prefix: str):
    """The data of the first query whose key starts with ``prefix``, or ``None``."""
    for query in queries:
        key = query.get("queryKey") or []
        if key and isinstance(key[0], str) and key[0].startswith(prefix):
            return (query.get("state") or {}).get("data")
    return None


def _series(queries: list[dict], url: str) -> dict:
    series = _query(queries, "series:detail-")
    if not isinstance(series, dict):
        raise FeedError(f"NPO page {url!r} has no series details")
    return series


def _programs(queries: list[dict], url: str) -> list[dict]:
    programs = _query(queries, "programs:season-")
    if not isinstance(programs, list):
        raise FeedError(f"NPO page {url!r} lists no episodes")
    return [p for p in programs if isinstance(p, dict)]


def _item(program: dict, feed_name: str, show_title: str) -> FeedItem:
    """Map one episode of the page's season to a :class:`~fetchit.feeds.FeedItem`."""
    synopsis = program.get("synopsis") or {}
    description = synopsis.get("long") or synopsis.get("short") or ""

    extra = {"show_title": show_title}
    product_id = program.get("productId")
    if product_id:
        # `id` is what a consumer of the library can recognise an item by.
        extra["id"] = str(product_id)
        extra["product_id"] = str(product_id)
    duration = program.get("durationInSeconds")
    if duration:
        extra["duration_seconds"] = str(duration)
    # Which episode of which season this is: what lets the library file it
    # as part of a series. Both or neither — half a place is no place.
    season = (program.get("season") or {}).get("seasonKey")
    episode = program.get("programKey")
    if season and episode:
        extra["season"] = str(season)
        extra["episode"] = str(episode)

    return FeedItem(
        title=str(program.get("title") or show_title).strip(),
        description=strip_html(str(description)),
        link=NPO_EPISODE_URL.format(slug=program["slug"]),
        published=_when(
            program.get("firstBroadcastDate") or program.get("publishedDateTime")
        ),
        image_url=_image(program),
        author=", ".join(_names(program.get("broadcasters"))) or None,
        categories=tuple(_genres(program)),
        feed_name=feed_name,
        extra=extra,
    )


def _names(entries: object) -> list[str]:
    """The ``name`` of each entry in one of the page's lists, in order."""
    if not isinstance(entries, list):
        return []
    return [str(e["name"]) for e in entries if isinstance(e, dict) and e.get("name")]


def _genres(program: dict) -> list[str]:
    """An episode's genres, each main one followed by the finer ones under it."""
    found: list[str] = []
    genres = program.get("genres")
    for genre in genres if isinstance(genres, list) else []:
        if isinstance(genre, dict):
            for name in [*_names([genre]), *_names(genre.get("secondaries"))]:
                if name not in found:
                    found.append(name)
    return found


def _when(epoch: object) -> datetime | None:
    """An NPO timestamp (seconds since the epoch) as a UTC datetime, or ``None``."""
    try:
        return datetime.fromtimestamp(int(epoch), tz=timezone.utc)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _image(entry: dict) -> str | None:
    """The ``default`` picture of a series or episode, else its first one."""
    images = [i for i in (entry.get("images") or []) if isinstance(i, dict) and i.get("url")]
    if not images:
        return None
    default = next((i for i in images if i.get("role") == "default"), images[0])
    return str(default["url"])
