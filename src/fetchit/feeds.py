"""Read feed configuration, parse RSS/Atom feeds, and filter their items.

This module turns the ``feeds`` of a config file plus the sources they name
into lists of :class:`FeedItem` objects, and holds the helpers for filtering
them. What happens to an item after that — choosing a quality, downloading it,
filing it in the library — is :mod:`fetchit.run`'s business.

Parsing is delegated to `feedparser <https://feedparser.readthedocs.io>`_, which
handles the many RSS/Atom dialects, malformed markup, date formats, and podcast
enclosures that show up in real-world feeds.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urlsplit

import feedparser

from . import cache, timing
from .quality import Quality, parse_quality

# Cache namespace for the ETag / Last-Modified a feed last handed out.
_HTTP_NS = "feed_http"

# How hard to lean on a network read before calling the feed lost. Three tries
# two and four seconds apart covers the gap between a machine waking and its
# network coming up, without holding a run open long enough to matter.
RETRY_ATTEMPTS = 3
RETRY_DELAY = 2.0

# Extensions that mark an enclosure as audio when its declared type doesn't.
AUDIO_SUFFIXES = (
    ".mp3",
    ".m4a",
    ".m4b",
    ".aac",
    ".ogg",
    ".oga",
    ".opus",
    ".wav",
    ".flac",
)

# The same, for an enclosure that is a video: a video podcast.
VIDEO_SUFFIXES = (".mp4", ".m4v", ".mov", ".webm", ".mkv")

# Suffixes that turn a feed config key into a filter, e.g. "title_contains".
FILTER_OPS = ("contains", "excludes", "matches", "min", "max")

# The ops that read their field as a number rather than as text, so they take
# one bound rather than a list of alternatives.
NUMERIC_OPS = ("min", "max")

# Day names for the published_weekday field. Written out rather than taken from
# strftime("%A"), which answers in whatever language the machine happens to be
# set to — a filter in the config must mean the same thing everywhere it runs.
WEEKDAYS = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)

# Config keys that are settings rather than filters.
KNOWN_KEYS = (
    "url",
    "urls",
    "name",
    "extract",
    "since",
    "max_per_run",
    "max_per_day",
    "quality",
    "keep_days",
    "refresh_image",
    "language",
)

# Keys that belong in rssignal's feeds.json rather than here. Somebody splitting
# one config into two will paste a few of these across, and "unknown key" alone
# would not say where they went.
RSSIGNAL_KEYS = (
    "message_template",
    "link_preview",
    "preview_url",
    "preview_title",
    "preview_description",
    "episodic",
    "start",
    "source",
)

# How many new items one feed may download in one run, unless it says. A feed
# that suddenly lists two hundred items it never had — a back catalogue opened
# up, a feed that renumbered itself — gets them over the next runs rather than
# in one sitting.
DEFAULT_MAX_PER_RUN = 10

# The field names every item has, in the order item_fields lists them.
CORE_FIELDS = (
    "title",
    "description",
    "link",
    "published",
    "published_date",
    "published_weekday",
    "enclosure_url",
    "enclosure_type",
    "image_url",
    "author",
    "categories",
    "feed_name",
)

# Entry keys already mapped onto FeedItem, so they don't repeat in ``extra``.
_MAPPED_ENTRY_KEYS = frozenset(
    {
        "title",
        "summary",
        "description",
        "link",
        "published",
        "published_parsed",
        "enclosures",
        "author",
        "tags",
        "image",
        "media_thumbnail",
    }
)


class FeedError(Exception):
    """Raised when feed configuration or a feed itself cannot be read."""


class SourceBlocked(FeedError):
    """Raised when a source isn't reachable from this machine on purpose.

    Distinct from a plain :class:`FeedError` because it means something
    different to the caller and to whoever reads the log: nothing is broken. A
    network-level filter — a DNS profile, a firewall, a schedule that only opens
    at night — is doing exactly what it was set up to do, and the feed will read
    fine the next time the window is open. So this is reported as a skip rather
    than a failure, and leaves no traceback behind.
    """


def retrying[T](
    work: Callable[[], T], *, attempts: int = RETRY_ATTEMPTS, delay: float = RETRY_DELAY
) -> T:
    """Call ``work``, trying again if it fails for a reason the network caused.

    This runs unattended, over a link that occasionally isn't there yet: a name
    that won't resolve, a connection refused a second after wake, a reset
    mid-read. Those cost a feed its whole run, and they are almost always gone
    by the time anyone looks. A couple of seconds spent asking again is cheaper
    than that.

    Only :class:`OSError` causes are retried — ``URLError``, timeouts, resets.
    A feed whose XML doesn't parse will not parse any better the second time, so
    it fails immediately.
    """
    for attempt in range(1, attempts + 1):
        try:
            return work()
        except FeedError as exc:
            if attempt == attempts or not isinstance(exc.__cause__, OSError):
                raise
            time.sleep(delay * attempt)
    raise AssertionError("unreachable")  # pragma: no cover


@dataclass(frozen=True)
class FeedFilter:
    """One field test from the config, e.g. ``"title_contains": "Ukraine"``.

    ``values`` holds the alternatives for the test: ``contains`` and ``matches``
    keep an item when *any* value hits, ``excludes`` keeps it when *none* do.
    The numeric ops (:data:`NUMERIC_OPS`) compare rather than match, so they take
    a single bound — alternatives mean nothing to them.
    """

    field: str
    op: str
    values: tuple[str, ...]


@dataclass(frozen=True)
class FieldExtract:
    """A new field cut out of an existing one, e.g. an id buried in a URL.

    ``pattern`` is matched against the ``source`` field; the new field takes the
    first capture group, or the whole match when the regex has no groups. Feeds
    routinely bury the only per-item identifier inside a media URL, and this is
    what makes it addressable as a filter — and, since the item record keeps
    every field, as ``{name}`` in a consumer's template.
    """

    name: str
    source: str
    pattern: str


@dataclass(frozen=True)
class FeedConfig:
    """Configuration for a single feed, as read from the config file.

    ``name`` is the feed's folder in the library, so :func:`load_feeds`
    requires it. It defaults to empty here only so that the pure helpers can be
    exercised without one.

    ``url`` is where the feed is read from. ``urls``, when given, is every such
    address with ``url`` first — for a source that is a list of links rather
    than one feed, such as the Bayam magazine links printed in each issue.

    ``since`` turns the feed into a backfill: everything published after it is
    wanted, oldest first, ``max_per_run`` at a time — and, if ``max_per_day``
    is set, no more than that in any 24 hours, which is how a backfill is kept
    in step with something sending it on at a pace (an episodic feed in
    rssignal) rather than racing ahead of it and being pruned before it is
    sent. Without ``since``, a feed seen for the first time takes only its
    newest item, and everything after that.

    ``quality`` is which rendition of a video to download (see
    :mod:`fetchit.quality`); ``keep_days`` how long a download stays in the
    library, ``None`` for the library's default. ``refresh_image`` off keeps the
    feed's artwork as it was first fetched, for a source whose "artwork" is
    whatever it is showing this week. ``language`` is written into the feed's
    metadata where the source does not say.
    """

    url: str
    name: str = ""
    filters: tuple[FeedFilter, ...] = ()
    extract: tuple[FieldExtract, ...] = ()
    since: datetime | None = None
    max_per_run: int = DEFAULT_MAX_PER_RUN
    max_per_day: int | None = None
    quality: Quality = Quality()
    keep_days: int | None = None
    refresh_image: bool = True
    language: str | None = None
    urls: tuple[str, ...] = ()

    @property
    def sources(self) -> tuple[str, ...]:
        """Every address this feed is read from, ``url`` first."""
        return self.urls or (self.url,)


@dataclass(frozen=True)
class FeedItem:
    """A single normalized entry from a feed.

    The named fields are the ones every feed kind is expected to have; anything
    else the feed happens to publish (``itunes_duration``, ``id``, …) lands in
    ``extra``. Use :func:`item_fields` to get the flat, stringified view that
    filters see, and that the library keeps in each item's record.
    """

    title: str
    description: str
    link: str | None = None
    published: datetime | None = None
    enclosure_url: str | None = None
    enclosure_type: str | None = None
    image_url: str | None = None
    author: str | None = None
    categories: tuple[str, ...] = ()
    feed_name: str = ""
    extra: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ParsedFeed:
    """A parsed feed: its entries plus what the feed says about itself.

    ``image_url`` is the show's / site's own artwork and ``description`` is what
    the feed says about itself. Items deliberately never inherit either (see
    :func:`_build_feed_item`): on an episode the same logo and blurb every time
    say nothing. They describe the feed's folder instead — its poster, its
    ``tvshow.nfo``, its ``feed.json``.
    """

    items: list[FeedItem]
    image_url: str | None = None
    description: str = ""
    title: str = ""
    link: str | None = None
    author: str | None = None
    language: str | None = None
    validators: dict[str, dict] = field(default_factory=dict)


def is_audio_item(item: FeedItem) -> bool:
    """Whether ``item`` carries audio to download — a podcast episode.

    This is asked per item rather than per feed: there is no ``type`` setting
    to declare. A show that posts the occasional written note therefore gets
    that note filed as text rather than as an episode with nothing to play.

    An enclosure alone is not enough — plenty of ordinary feeds attach an image
    or a PDF. The declared type decides when there is one, and the URL only gets
    a say when the feed didn't declare anything useful, which is common enough:
    ``application/octet-stream`` on an mp3 is a real thing feeds do.
    """
    return _enclosure_kind(item) == "audio"


def is_video_enclosure(item: FeedItem) -> bool:
    """Whether ``item`` carries a video file of its own — a video podcast.

    The counterpart to :func:`is_audio_item`, for an enclosure that is the
    video itself, as opposed to a link to a page with a video behind it (which
    is :func:`fetchit.media.is_video_item`'s question).
    """
    return _enclosure_kind(item) == "video"


def _enclosure_kind(item: FeedItem) -> str | None:
    """``"audio"``, ``"video"`` or ``None``, from the enclosure's type or name."""
    if not item.enclosure_url:
        return None

    mime = (item.enclosure_type or "").strip().lower()
    if mime.startswith("audio/"):
        return "audio"
    if mime.startswith("video/"):
        return "video"
    if mime and not mime.startswith("application/"):
        # image/, text/ — declared, and declared as something else.
        return None

    # Split the URL first: podcast CDNs bolt tracking parameters onto every
    # enclosure, and "…/ep.mp3?source=rss" doesn't end in ".mp3".
    path = urlsplit(item.enclosure_url).path.lower()
    if path.endswith(AUDIO_SUFFIXES):
        return "audio"
    if path.endswith(VIDEO_SUFFIXES):
        return "video"
    return None


def load_feeds(
    raw_feeds: object, where: str = "feeds", *, quality: Quality = Quality()
) -> list[FeedConfig]:
    """Validate the ``feeds`` array of a config file into :class:`FeedConfig`\\ s.

    Each feed needs a ``url`` (or ``urls``) and a ``name``, and two feeds can't
    share a name: the name is the feed's folder. ``quality`` is the library-wide
    default a feed's own ``quality`` is read on top of. Raises
    :class:`FeedError` on a malformed entry, naming it.
    """
    if not isinstance(raw_feeds, list):
        raise FeedError(f"{where} must be an array of feeds.")

    feeds = [
        _build_feed_config(raw, f"{where}[{i}]", quality)
        for i, raw in enumerate(raw_feeds)
    ]
    seen: dict[str, str] = {}
    for index, cfg in enumerate(feeds):
        key = cfg.name.casefold()
        if key in seen:
            raise FeedError(
                f"{where}[{index}] is named {cfg.name!r}, like {seen[key]}. A feed's "
                "name is its folder in the library, so names have to be unique."
            )
        seen[key] = f"{where}[{index}]"
    return feeds


def _build_feed_config(raw: object, where: str, base: Quality = Quality()) -> FeedConfig:
    """Validate one raw feed entry and turn it into a :class:`FeedConfig`."""
    if not isinstance(raw, dict):
        raise FeedError(f"{where} must be an object.")

    urls = _build_urls(raw, where)

    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        raise FeedError(
            f"{where} is missing a non-empty \"name\". The name is the feed's "
            "folder in the library, and what rssignal and db-podcast call it by."
        )

    refresh_image = raw.get("refresh_image", True)
    if not isinstance(refresh_image, bool):
        raise FeedError(f"{where} refresh_image must be true or false.")

    language = raw.get("language")
    if language is not None and (not isinstance(language, str) or not language.strip()):
        raise FeedError(f"{where} language must be a language code like \"fr\".")

    max_per_run = raw.get("max_per_run", DEFAULT_MAX_PER_RUN)
    if isinstance(max_per_run, bool) or not isinstance(max_per_run, int) or max_per_run < 1:
        raise FeedError(f"{where} max_per_run must be a whole number, 1 or more.")

    max_per_day = raw.get("max_per_day")
    if max_per_day is not None and (
        isinstance(max_per_day, bool) or not isinstance(max_per_day, int) or max_per_day < 1
    ):
        raise FeedError(f"{where} max_per_day must be a whole number, 1 or more.")

    keep_days = raw.get("keep_days")
    if keep_days is not None and (
        isinstance(keep_days, bool) or not isinstance(keep_days, int) or keep_days < 0
    ):
        raise FeedError(f"{where} keep_days must be a whole number of days; 0 keeps everything.")

    try:
        quality = parse_quality(raw.get("quality"), where, base)
    except ValueError as exc:
        raise FeedError(str(exc)) from exc

    return FeedConfig(
        url=urls[0],
        urls=urls if len(urls) > 1 else (),
        name=name.strip(),
        filters=_build_filters(raw, where),
        extract=_build_extracts(raw.get("extract"), where),
        since=_build_since(raw.get("since"), where),
        max_per_run=max_per_run,
        max_per_day=max_per_day,
        quality=quality,
        keep_days=keep_days,
        refresh_image=refresh_image,
        language=language.strip() if isinstance(language, str) else None,
    )


def _build_urls(raw: dict, where: str) -> tuple[str, ...]:
    """The addresses a feed is read from: ``url``, ``urls``, or both."""
    urls: list[str] = []
    url = raw.get("url")
    if url is not None:
        if not isinstance(url, str) or not url.strip():
            raise FeedError(f"{where} url must be a non-empty string.")
        urls.append(url.strip())
    more = raw.get("urls")
    if more is not None:
        if not isinstance(more, list) or not all(
            isinstance(u, str) and u.strip() for u in more
        ):
            raise FeedError(f"{where} urls must be a list of non-empty strings.")
        urls.extend(u.strip() for u in more if u.strip() not in urls)
    if not urls:
        raise FeedError(f"{where} is missing a \"url\" (or a \"urls\" list).")
    return tuple(urls)


def _build_since(raw: object, where: str) -> datetime | None:
    """Validate the ``since`` date a backfill starts from.

    A bare date means midnight UTC, which is what somebody writing
    ``2019-01-01`` means by it. The stamp is compared against feed timestamps,
    so it has to be aware; a naive one is read as UTC, as everywhere else here.
    """
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise FeedError(f"{where} since must be a date like \"2019-01-01\".")
    try:
        when = datetime.fromisoformat(raw.strip())
    except ValueError as exc:
        raise FeedError(
            f"{where} since {raw!r} is not a date fetchit can read. Use "
            "\"2019-01-01\", or a full \"2019-01-01T12:00:00+00:00\"."
        ) from exc
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def _build_extracts(raw: object, where: str) -> tuple[FieldExtract, ...]:
    """Validate the ``extract`` block into :class:`FieldExtract` rules.

    Patterns are compiled here rather than per item: a typo in a regex should
    fail the run up front, not halfway through a feed.
    """
    if raw is None:
        return ()
    if not isinstance(raw, dict):
        raise FeedError(
            f"{where} extract must be an object mapping a field name to "
            "{\"from\": ..., \"pattern\": ...}."
        )

    rules: list[FieldExtract] = []
    for name, spec in raw.items():
        at = f"{where} extract[{name!r}]"
        if not isinstance(spec, dict):
            raise FeedError(f"{at} must be an object with \"from\" and \"pattern\".")

        source = spec.get("from")
        pattern = spec.get("pattern")
        for key, value in (("from", source), ("pattern", pattern)):
            if not isinstance(value, str) or not value:
                raise FeedError(f"{at} is missing a non-empty string {key!r}.")

        unknown = set(spec) - {"from", "pattern"}
        if unknown:
            raise FeedError(f"{at} has unknown key(s) {sorted(unknown)}.")

        try:
            re.compile(pattern)
        except re.error as exc:
            raise FeedError(f"{at} has an invalid regex {pattern!r}: {exc}") from exc

        rules.append(FieldExtract(name=name, source=source, pattern=pattern))
    return tuple(rules)


def _build_filters(raw: dict, where: str) -> tuple[FeedFilter, ...]:
    """Collect the ``<field>_<op>`` keys of ``raw`` into :class:`FeedFilter`\\ s.

    Any key that is neither a known setting nor a filter suffix is an error —
    silently ignoring a typo like ``title_contain`` would quietly download the
    wrong items.
    """
    filters: list[FeedFilter] = []
    for key, value in raw.items():
        if key in KNOWN_KEYS:
            continue
        if key in RSSIGNAL_KEYS:
            raise FeedError(
                f"{where} has {key!r}, which is about sending rather than "
                "downloading. It belongs on this feed in rssignal's feeds.json; "
                "fetchit only decides what is downloaded and where it is filed."
            )

        field_name, _, op = key.rpartition("_")
        if not field_name or op not in FILTER_OPS:
            raise FeedError(
                f"{where} has unknown key {key!r}. Expected one of {KNOWN_KEYS}, "
                f"or a filter named <field>_<op> with op in {FILTER_OPS}."
            )

        if op in NUMERIC_OPS:
            filters.append(_numeric_filter(field_name, op, value, where, key))
            continue

        values = [value] if isinstance(value, str) else value
        if not isinstance(values, list) or not all(
            isinstance(v, str) for v in values
        ):
            raise FeedError(f"{where} {key!r} must be a string or a list of strings.")
        if op == "matches":
            for pattern in values:
                try:
                    re.compile(pattern)
                except re.error as exc:
                    raise FeedError(
                        f"{where} {key!r} has an invalid regex {pattern!r}: {exc}"
                    ) from exc

        filters.append(FeedFilter(field=field_name, op=op, values=tuple(values)))
    return tuple(filters)


def _numeric_filter(
    field_name: str, op: str, value: object, where: str, key: str
) -> FeedFilter:
    """Validate one ``<field>_min`` / ``<field>_max`` bound into a filter.

    The bound is a number of seconds, or a duration written with colons, and
    JSON may give either as a number or a string. It is checked here rather than
    per item so ``"duration_seconds_max": "half an hour"`` fails the run up front
    instead of quietly dropping every item.
    """
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise FeedError(
            f"{where} {key!r} must be a single bound, not a list — a number of "
            "seconds, or a duration like \"30:00\"."
        )

    text = str(value)
    if as_number(text) is None:
        raise FeedError(
            f"{where} {key!r} is not a number or a duration: {value!r}."
        )
    return FeedFilter(field=field_name, op=op, values=(text,))


def _read(url: str, *, conditional: bool = True) -> feedparser.FeedParserDict:
    """Fetch and parse ``url``, raising :class:`FeedError` if it can't be read.

    feedparser doesn't raise on network or parse trouble; it records it on the
    result instead. Turning that back into an exception — and chaining the cause
    it recorded — is what lets :func:`retrying` tell a link that was briefly
    down from a feed that is genuinely broken.

    The validators the feed handed out last time go back with the request, so a
    feed with nothing new answers ``304`` and sends no body at all. fetchit is
    built to be run often and mostly find nothing, which is exactly the case
    that pays for.

    ``conditional`` off asks for the body regardless. That is for a backfill,
    whose work does not depend on the feed having changed: it is walking a back
    catalogue years behind the newest item, and "nothing new" is both true and
    beside the point.

    The validators this answer came with are *not* remembered here. They go
    back to the caller on the :class:`ParsedFeed` and are only kept once the
    run has dealt with the items — see :func:`remember_validators`. Kept any
    sooner, a feed whose newest episode failed to download, or that was only
    looked at in a dry run, would answer "nothing new" next time and the
    episode would wait until the feed changed.
    """
    known = (cache.get(_HTTP_NS, url) or {}) if conditional else {}
    # Passed only when there is something to pass, so a feed nobody has a
    # validator for is fetched exactly as it always was.
    conditional_args = {
        name: known[name]
        for name in ("etag", "modified")
        if isinstance(known, dict) and known.get(name)
    }
    parsed = feedparser.parse(url, **conditional_args)

    if getattr(parsed, "status", None) == 304:
        # Unchanged since last time; there is no body to look at and no reason to
        # have wanted one. Handled before the bozo check below, which would
        # otherwise read an empty 304 as a feed that failed to parse.
        return parsed

    if getattr(parsed, "bozo", False) and not parsed.entries:
        exc = getattr(parsed, "bozo_exception", None)
        served = _content_type(parsed)
        if served and not _is_feed_type(served):
            # Not a feed that failed to parse — something else answering in a
            # feed's place. A DNS filter's block page, a captive portal, a
            # sign-in wall. The parse error underneath it is real and completely
            # uninformative: it describes the first stray angle bracket in a
            # page of HTML, and says nothing about why the page is there. The
            # status does say something — a 404 or a 500 is the source's own
            # error page, not a filter's — so it goes in the message.
            status = getattr(parsed, "status", None)
            answer = f"HTTP {status} {served}" if status else served
            raise SourceBlocked(
                f"{url} answered with {answer} rather than a feed — a block "
                "page, a captive portal or a sign-in wall standing in front of "
                "the source, not a broken feed. Skipping until it lifts."
            ) from (exc if isinstance(exc, BaseException) else None)
        raise FeedError(
            f"Could not read feed {url!r}: {exc or 'unknown error'}"
        ) from (exc if isinstance(exc, BaseException) else None)
    return parsed


# The media types that are never a feed and always a page. Written as the
# things to rule out rather than the things to accept, because the accept list
# is the open-ended one: feeds arrive as application/rss+xml, application/atom+xml,
# text/xml, application/json, text/plain, and whatever else a server has been
# left misconfigured to say. A page announcing itself as HTML is the only answer
# unambiguous enough to act on.
_PAGE_CONTENT_TYPES = ("text/html", "application/xhtml+xml")


def _content_type(parsed: feedparser.FeedParserDict) -> str:
    """The media type ``parsed`` came back as, lowercased, without parameters.

    Empty when the response carried no ``Content-Type`` — which is what a feed
    read from a file or a string looks like, and is not evidence of anything.
    """
    headers = getattr(parsed, "headers", None) or {}
    value = headers.get("content-type") or headers.get("Content-Type") or ""
    return value.split(";")[0].strip().lower()


def _is_feed_type(content_type: str) -> bool:
    """Whether ``content_type`` leaves it possible that this was meant as a feed.

    The question is not "is this a valid feed media type" but "is this so
    clearly a web page that the parse error underneath it is beside the point".
    Anything that isn't HTML gets the benefit of the doubt and the old
    behaviour — a feed served as ``text/plain`` is odd, not a block page.
    """
    return content_type not in _PAGE_CONTENT_TYPES


def parse_feed(cfg: FeedConfig) -> ParsedFeed:
    """Fetch and parse every one of ``cfg``'s addresses into one :class:`ParsedFeed`.

    One address is the usual case. A feed made of several — a list of magazine
    links — is each of them read in turn and their items put together, with
    the first address speaking for the feed as a whole.
    """
    if len(cfg.sources) == 1:
        return _parse_one(cfg)

    parts = [_parse_one(replace(cfg, url=url, urls=())) for url in cfg.sources]
    first = next((p for p in parts if p.title or p.image_url), parts[0])
    validators = {url: v for part in parts for url, v in part.validators.items()}
    return replace(
        first, items=[item for part in parts for item in part.items], validators=validators
    )


def remember_validators(parsed: ParsedFeed) -> None:
    """Keep the ETags ``parsed`` came with, so the next read can be answered "unchanged".

    Called once the items have been dealt with — not when they were read; see
    :func:`_read` for why.
    """
    for url, known in parsed.validators.items():
        cache.put(_HTTP_NS, url, known)


def _parse_one(cfg: FeedConfig) -> ParsedFeed:
    """Fetch and parse ``cfg.url`` into a :class:`ParsedFeed`.

    Usually that means RSS or Atom, and a video source's own page counts as
    either a substitute for one or a signpost to one: an ARTE collection page is
    read directly, since ARTE publishes no feed at all, and a YouTube channel
    page is swapped for the feed YouTube publishes at an address nobody would
    guess. Either way, following a show needs nothing in the config but the
    address of its page.

    A video item also gets the one thing its feed tends to leave out — how long
    the video is — so that ``duration_seconds`` is an ordinary field like any
    other. See :func:`~fetchit.media.annotate_durations`.

    A backfill (``since`` set) gets one thing more: everything the source
    published before the handful its feed still lists. See
    :func:`~fetchit.media.with_archive`.

    Raises :class:`SourceBlocked` before doing any of that, if the source turns
    out to be one this machine currently can't reach at all. A YouTube feed that
    is reachable but not answering with a feed is read through yt-dlp instead —
    see :func:`~fetchit.media.listed_items`.
    """
    # Imported here, not at the top: fetchit.media builds FeedItems and so
    # imports this module. Deferring it keeps that one-way and leaves feeds.py
    # free of any knowledge of what sources exist.
    from .media import (
        annotate_durations,
        channel_feed_url,
        check_source_available,
        collection_feed,
        with_archive,
    )

    check_source_available(cfg.url)

    collection = collection_feed(cfg)
    if collection is not None:
        return collection

    feed_url = channel_feed_url(cfg.url)
    if feed_url:
        cfg = replace(cfg, url=feed_url)

    meta: dict[str, str | None] = {}
    validators: dict[str, dict] = {}
    try:
        with timing.step("feedparser", cfg.name or cfg.url):
            parsed = retrying(
                lambda: _read(cfg.url, conditional=cfg.since is None)
            )
    except FeedError as exc:
        # A feed a source can list some other way is worth that other way: see
        # :func:`~fetchit.media.listed_items`. Anything else fails as it did.
        items = _listed_instead(cfg, exc)
        image_url, description = None, ""
    else:
        if getattr(parsed, "status", None) == 304:
            # Nothing has changed since the last run, so there is nothing to
            # build and — the part worth the early return — nothing to look
            # durations up for either, which is a yt-dlp launch this feed now
            # never pays.
            return ParsedFeed(items=[], image_url=None, description="")

        etag = getattr(parsed, "etag", None)
        modified = getattr(parsed, "modified", None)
        if cfg.since is None and (etag or modified):
            validators = {cfg.url: {"etag": etag, "modified": modified}}
        channel = getattr(parsed, "feed", None) or {}
        cget = channel.get if hasattr(channel, "get") else lambda k, d=None: getattr(channel, k, d)

        # "summary" is the channel's <description>; "subtitle" is the shorter
        # <itunes:subtitle>, which some feeds fill in instead.
        description = strip_html(cget("summary") or cget("subtitle") or "")
        image_url = _href(cget("image"))
        meta = {
            "title": (cget("title") or "").strip(),
            "link": _clean_url(cget("link")),
            "author": cget("author") or None,
            "language": cget("language") or None,
        }
        items = [_build_feed_item(entry, cfg.name) for entry in parsed.entries]

    # Durations come before extracts so a rule can read one, and after the items
    # exist because they are looked up per feed, not per entry.
    items = annotate_durations(cfg.url, items)

    # The back catalogue, for a backfill only, and after durations because the
    # archive brings its own — a listing reports them, so the items that come
    # back from here are already annotated and filter the same as the rest.
    if cfg.since is not None:
        items = with_archive(cfg.url, items, feed_name=cfg.name)

    return ParsedFeed(
        items=[apply_extracts(item, cfg.extract) for item in items],
        image_url=image_url,
        description=description,
        title=meta.get("title") or "",
        link=meta.get("link"),
        author=meta.get("author"),
        language=meta.get("language"),
        validators=validators,
    )


def feed_image(cfg: FeedConfig) -> str | None:
    """The artwork ``cfg``'s source shows for itself right now, or ``None``.

    What :func:`parse_feed` puts in :attr:`ParsedFeed.image_url`, without the
    rest of the work — no items built, no durations looked up, no back catalogue
    walked — plus the one thing it can't do: a YouTube channel's picture, which
    its feed doesn't carry. The feed is read unconditionally, because one that
    hasn't changed since the last run would otherwise answer 304 and say nothing
    at all.

    Raises :class:`SourceBlocked` or :class:`FeedError` if the source can't be
    read.
    """
    from .media import check_source_available, source_image

    check_source_available(cfg.url)
    image = source_image(cfg.url)
    if image is not None:
        return image

    parsed = retrying(lambda: _read(cfg.url, conditional=False))
    channel = getattr(parsed, "feed", None) or {}
    cget = channel.get if hasattr(channel, "get") else lambda k, d=None: getattr(channel, k, d)
    return _href(cget("image"))


def _listed_instead(cfg: FeedConfig, failure: FeedError) -> list[FeedItem]:
    """The items ``cfg``'s source lists without its feed, or ``failure`` raised.

    Re-raised as the same kind of error it was, so a feed that answered with a
    block page and couldn't be listed either is still a skip rather than a
    failure — with the reason the listing gave added to it.
    """
    from .media import listed_items

    try:
        items = listed_items(cfg.url, feed_name=cfg.name)
    except FeedError as exc:
        raise type(failure)(
            f"{failure}\nListing it through yt-dlp instead failed too: {exc}"
        ) from failure
    if items is None:
        raise failure
    return items


def _clean_url(url: object) -> str | None:
    """A URL out of a feed, with any over-escaping undone.

    XML escapes an ampersand once and the parser unescapes it once, so a URL
    with a query string normally survives the round trip unchanged. NPO's
    podcast feeds escape twice — ``&amp;amp;`` in the document — which leaves a
    literal ``&amp;`` in the query once feedparser has had its turn, renaming
    the second parameter to ``amp;awEpisodeid``. Their file server answers that
    with a 404, so the episode can't be downloaded at all.

    No URL means ``&amp;`` where it means ``&``, so unescaping until nothing is
    left to unescape is safe, and a no-op for every feed that got it right.
    """
    if not url:
        return None
    text = str(url)
    while "&amp;" in text:
        text = text.replace("&amp;", "&")
    return text or None


def _href(value: object) -> str | None:
    """Pull a URL out of a feedparser image/thumbnail mapping, if there is one."""
    if not value:
        return None
    get = value.get if hasattr(value, "get") else lambda k, d=None: getattr(value, k, d)
    return _clean_url(get("href") or get("url"))


def _build_feed_item(entry: object, feed_name: str = "") -> FeedItem:
    """Map a single feedparser entry to a :class:`FeedItem`."""
    get = entry.get if isinstance(entry, dict) else lambda k, d=None: getattr(entry, k, d)

    title = (get("title") or "").strip()
    description = strip_html(get("summary") or get("description") or "")
    link = _clean_url(get("link"))

    published = None
    published_parsed = get("published_parsed")
    if published_parsed is not None:
        # published_parsed is a time.struct_time in UTC.
        published = datetime(*published_parsed[:6], tzinfo=timezone.utc)

    enclosure_url = None
    enclosure_type = None
    enclosures = get("enclosures") or []
    if enclosures:
        first = enclosures[0]
        fget = first.get if isinstance(first, dict) else lambda k, d=None: getattr(first, k, d)
        enclosure_url = _clean_url(fget("href") or fget("url"))
        enclosure_type = fget("type") or None

    # Episode artwork only: <itunes:image> lands in "image", media RSS in
    # "media_thumbnail". The channel's artwork is deliberately not a fallback —
    # the same show logo on every item says nothing about the item.
    thumbnails = get("media_thumbnail") or []
    image_url = _href(get("image")) or _href(thumbnails[0] if thumbnails else None)

    # feedparser exposes categories as tags: [{"term": "news", ...}, ...].
    categories = []
    for tag in get("tags") or []:
        tget = tag.get if isinstance(tag, dict) else lambda k, d=None: getattr(tag, k, d)
        term = tget("term")
        if term:
            categories.append(str(term))

    return FeedItem(
        title=title,
        description=description,
        link=link,
        published=published,
        enclosure_url=enclosure_url,
        enclosure_type=enclosure_type,
        image_url=image_url,
        author=get("author") or None,
        categories=tuple(categories),
        feed_name=feed_name,
        extra=_extra_fields(entry),
    )


def _extra_fields(entry: object) -> dict[str, str]:
    """Collect the feed's own scalar keys that FeedItem doesn't name.

    Feeds carry all sorts of extra data (``itunes_duration``, ``id``, episode
    numbers). Keeping the scalar ones makes them available to filters and to
    whatever reads the item's record later; the structured ``*_detail`` /
    ``*_parsed`` mirrors feedparser adds are skipped, since they have no useful
    string form.
    """
    items = entry.items() if hasattr(entry, "items") else vars(entry).items()
    extra: dict[str, str] = {}
    for key, value in items:
        if key in _MAPPED_ENTRY_KEYS or key.endswith(("_detail", "_parsed")):
            continue
        if isinstance(value, str):
            extra[key] = strip_html(value) if "<" in value else value
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            extra[key] = str(value)
    return extra


class _TextExtractor(HTMLParser):
    """Collect text content from HTML, dropping tags."""

    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self._parts.append(data)

    def text(self) -> str:
        return "".join(self._parts)


def strip_html(text: str) -> str:
    """Return ``text`` with HTML tags removed and entities unescaped.

    Feed descriptions are frequently HTML; plain text is what a media server's
    synopsis and a chat message both want. Whitespace is collapsed to single
    spaces.
    """
    if not text:
        return ""
    extractor = _TextExtractor()
    extractor.feed(text)
    plain = unescape(extractor.text())
    return " ".join(plain.split())


def item_fields(item: FeedItem) -> dict[str, str]:
    """Return ``item`` as a flat mapping of field name to string value.

    This is the single view of an item shared by filters, the ``fetchit
    fields`` command, and the record the library keeps beside each download —
    so what that command prints is exactly what a filter here, or a template in
    rssignal, can use. Named fields come first; the feed's own extras fill in
    behind them without overwriting a named field.

    The three ``published*`` fields are the one date in three shapes: the full
    timestamp, the day it fell on, and the name of that day — which is there so
    a show with a broadcast week can be filtered down to it. All three are UTC,
    like every date fetchit reads, so a late-evening upload can belong to the
    next day.
    """
    fields: dict[str, str] = {
        "title": item.title,
        "description": item.description,
        "link": item.link or "",
        "published": item.published.isoformat() if item.published else "",
        "published_date": item.published.strftime("%Y-%m-%d") if item.published else "",
        "published_weekday": WEEKDAYS[item.published.weekday()] if item.published else "",
        "enclosure_url": item.enclosure_url or "",
        "enclosure_type": item.enclosure_type or "",
        "image_url": item.image_url or "",
        "author": item.author or "",
        "categories": ", ".join(item.categories),
        "feed_name": item.feed_name,
    }
    for key, value in item.extra.items():
        fields.setdefault(key, value)
    return fields


def apply_extracts(item: FeedItem, rules: tuple[FieldExtract, ...]) -> FeedItem:
    """Return ``item`` with the fields ``rules`` cut out of it added to ``extra``.

    Landing them in ``extra`` is what makes them ordinary fields: filters,
    ``fetchit fields`` and the library's record all read :func:`item_fields`,
    which merges extras behind the named fields. A rule can therefore never
    shadow ``title`` or ``link``, and it reads the item as it came off the feed
    — extracts do not chain into one another.

    A pattern that doesn't match yields an empty field rather than an error.
    """
    if not rules:
        return item

    fields = item_fields(item)
    extra = dict(item.extra)
    for rule in rules:
        match = re.search(rule.pattern, fields.get(rule.source, ""))
        if match is None:
            extra[rule.name] = ""
        else:
            extra[rule.name] = match.group(1) if match.re.groups else match.group(0)
    return replace(item, extra=extra)


def apply_filters(
    items: list[FeedItem], filters: tuple[FeedFilter, ...]
) -> list[FeedItem]:
    """Return the items passing every filter (filters are ANDed).

    Matching is case-insensitive throughout. A field an item doesn't have counts
    as empty, so ``contains`` drops it while ``excludes`` keeps it — and ``min``
    and ``max`` drop it too, since nothing is not a number.
    """
    if not filters:
        return list(items)
    return [
        item
        for item in items
        if all(_passes(item_fields(item), f) for f in filters)
    ]


def _passes(fields: dict[str, str], flt: FeedFilter) -> bool:
    """Test one item's ``fields`` against a single filter."""
    text = fields.get(flt.field, "")
    if flt.op in NUMERIC_OPS:
        value = as_number(text)
        bound = as_number(flt.values[0])
        if value is None or bound is None:
            return False
        return value >= bound if flt.op == "min" else value <= bound

    if flt.op == "matches":
        return any(re.search(v, text, re.IGNORECASE) for v in flt.values)

    lowered = text.lower()
    hit = any(v.lower() in lowered for v in flt.values)
    return hit if flt.op == "contains" else not hit


def as_number(text: str) -> float | None:
    """Read ``text`` as a plain number, or as a clock duration like ``00:42:11``.

    Feeds write a length both ways — YouTube and ARTE report seconds, podcasts
    usually write ``itunes:duration`` with colons — and a ``min`` / ``max``
    filter shouldn't care which, in either the field or the bound it is compared
    against. ``None`` means "not a number", which is how a missing field and a
    field holding prose both end up failing the test.
    """
    text = (text or "").strip()
    if not text:
        return None

    parts = text.split(":")
    if len(parts) > 3:
        return None

    total = 0.0
    for part in parts:
        try:
            total = total * 60 + float(part)
        except ValueError:
            return None
    return total
