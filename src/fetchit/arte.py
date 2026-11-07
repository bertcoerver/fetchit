"""Read ARTE: a show as a feed, and a programme's video behind its link.

ARTE items carry no enclosure at all — an item like
``.../fr/videos/127395-052-A/le-dessous-des-images/`` links to a player page and
the video itself lives behind a streaming manifest — so without this the whole
point of such a feed never reaches the library.

How the video is found, and why not by scraping: the programme id is right there
in the link, and ARTE publishes a player API keyed by it that returns the same
manifest url the page's ``<video>`` element uses, plus the duration. That is two
small json/text fetches against a documented-shaped endpoint instead of parsing
a megabyte of rendered React that changes whenever the site is redesigned.

Which quality, and why it is chosen here rather than left to ffmpeg: the
manifest is a multi-variant HLS master offering the same programme from ~380x216
up to 1080p, and which of those is wanted is the feed's ``quality`` setting.
ffmpeg's HLS demuxer has no "sharpest under this height" or "biggest that fits"
switch, so the master is parsed here, the size of each rung is estimated from
its advertised average bandwidth times the duration, the choice follows
:mod:`fetchit.quality`, and it is handed to ffmpeg by program index. Streams
are copied, never re-encoded: the rungs
already exist at sensible bitrates, and re-encoding would trade minutes of cpu
for nothing.

The dispatch that decides an item is ARTE's at all lives in
:mod:`fetchit.media`; this module only answers about ARTE.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from . import cache, timing
from .download import fetch_text
from .feeds import (
    FeedConfig,
    FeedError,
    FeedItem,
    ParsedFeed,
    apply_extracts,
    strip_html,
)
from .media import SIZE_ESTIMATE_MARGIN, VideoGone
from .quality import Quality, choose, in_parts, too_big

# An ARTE programme page: language, then the programme id. Collection pages
# (``RC-023176``) are deliberately not matched — they are a listing, not a
# programme, and the player api has nothing to say about them.
ARTE_LINK_RE = re.compile(
    r"https?://(?:www\.)?arte\.tv/(?P<lang>[a-z]{2})/videos/"
    r"(?P<program_id>\d{4,}-\d{3}-[A-Z])(?:[/?#]|$)",
    re.IGNORECASE,
)

# An ARTE collection page — the show itself rather than one episode. This is
# what you get from the site's "toutes les vidéos" link, and it is the url to
# put in feeds.json for a show that publishes no usable RSS of its own.
ARTE_COLLECTION_RE = re.compile(
    r"https?://(?:www\.)?arte\.tv/(?P<lang>[a-z]{2})/videos/"
    r"(?P<collection_id>RC-\d+)(?:[/?#]|$)",
    re.IGNORECASE,
)

ARTE_PLAYER_API = "https://api.arte.tv/api/player/v2/config/{lang}/{program_id}"
ARTE_PLAYLIST_API = "https://api.arte.tv/api/player/v2/playlist/{lang}/{collection_id}"

# How many of a collection's newest episodes get a publication date looked up.
#
# The playlist lists a show's whole back catalogue — a hundred episodes for a
# long-running one — but carries no dates, and a date costs one request per
# episode. The list is newest-first, so dating the top slice is enough to tell
# what is new: anything below it is old by construction. Undated items are
# never downloaded anyway, which is exactly the right answer for the back
# catalogue — a show newly followed should not open with a hundred videos.
ARTE_DATED_ITEMS = 12

# Cache namespaces: the date a programme became available, and the fact that one
# had none to give. See :mod:`fetchit.cache`.
_PUBLISHED_NS = "arte_published"
_UNDATED_NS = "arte_undated"

# How long a programme ARTE lists without a video is waited for.
#
# A missing stream is normally ARTE running behind its own schedule, and the
# video turns up within a day or two. Not always: 125533-026-A, a rerun of an
# episode from May, was given a new three-week rights window and no video at
# all, and for as long as it was retried nothing published after it could be
# downloaded — a feed is filled oldest first. A week is every chance a late video needs,
# and it is the whole of a weekly show's cycle: wait any longer and the next
# episode is the one being held up.
STREAMS_PATIENCE = timedelta(days=7)

# H.265 rungs duplicate resolutions that also exist in H.264, at no useful size
# saving here, and play back unevenly on phones and televisions. Not worth the
# gamble when an equivalent avc1 rung is always sitting next to it.
_H265_CODECS = ("hev1", "hvc1")


@dataclass(frozen=True)
class Variant:
    """One rung of an HLS master playlist.

    ``index`` is its position among the ``EXT-X-STREAM-INF`` entries, which is
    also the program index ffmpeg gives it — the two orders are the same, and
    that correspondence is the only reason a rung can be requested by number.
    """

    index: int
    bandwidth: int
    width: int
    height: int
    codecs: str

    @property
    def resolution(self) -> str:
        return f"{self.width}x{self.height}"

    def estimated_bytes(self, duration: float) -> int:
        """Roughly how big this rung is over ``duration`` seconds."""
        return int(self.bandwidth / 8 * duration * SIZE_ESTIMATE_MARGIN)


@dataclass(frozen=True)
class HlsPlan:
    """What would be downloaded for an ARTE item, worked out without downloading.

    Resolving is two small fetches, so a dry run can report the real resolution
    and size an item would arrive at rather than promising "a video". One
    implementation of the :class:`~fetchit.media.VideoPlan` protocol.
    """

    master_url: str
    variant: Variant
    duration: float
    parts: int = 1

    @property
    def estimated_bytes(self) -> int:
        return self.variant.estimated_bytes(self.duration)

    def describe(self) -> str:
        mb = self.estimated_bytes / (1024 * 1024)
        return f"{self.variant.resolution}, ~{mb:.0f} MB{in_parts(self.parts)}"

    def fetch(self, into: str, *, timeout: float) -> str:
        """Download the chosen rung into directory ``into``, returning its path."""
        out_path = os.path.join(into, "video.mp4")
        _run_ffmpeg(self.master_url, self.variant.index, out_path, timeout=timeout)
        return out_path


def arte_program_id(link: str | None) -> tuple[str, str] | None:
    """Return ``(program_id, lang)`` for an ARTE programme link, else ``None``."""
    if not link:
        return None
    match = ARTE_LINK_RE.search(link)
    if not match:
        return None
    return match["program_id"].upper(), match["lang"].lower()


class _NoRights(FeedError):
    """ARTE has no rights to show the programme right now — see :func:`resolve`."""


class _NoStreams(FeedError):
    """ARTE lists the programme but has no video for it — see :func:`resolve`."""


def resolve(item: FeedItem, *, quality: Quality = Quality()) -> HlsPlan:
    """Work out which stream and quality ``item``'s video would be fetched at.

    Raises :class:`FeedError` if the item is not an ARTE programme, the player
    api has nothing usable, or every rung is over the size budget, and
    :class:`VideoGone` if its rights have run out or its video never arrived.
    """
    found = arte_program_id(item.link)
    if not found:
        raise FeedError(f"Not an ARTE video link: {item.link!r}")
    program_id, lang = found

    try:
        master_url, duration = _player_config(program_id, lang)
    except _NoRights as exc:
        # ARTE says the same for a programme whose rights have closed and for
        # one whose rights have not opened yet. Only the first is permanent, and
        # an item with a date in the past is one that has been out.
        released = item.published
        if released is not None and released <= datetime.now(timezone.utc):
            raise VideoGone(
                f"{program_id} is no longer on ARTE: its rights have expired"
            ) from exc
        raise
    except _NoStreams as exc:
        # Worth waiting for, but not for ever: see STREAMS_PATIENCE. An item
        # with no date can't say how long it has been, and is left to retry.
        released = item.published
        waited = datetime.now(timezone.utc) - released if released else None
        if waited is not None and waited > STREAMS_PATIENCE:
            raise VideoGone(
                f"ARTE has listed {program_id} for {waited.days} days without "
                "a video for it; not waiting for it any longer"
            ) from exc
        raise
    variants = _variants(fetch_text(master_url))
    variant, parts = _pick_variant(variants, duration, quality, program_id)
    return HlsPlan(
        master_url=master_url, variant=variant, duration=duration, parts=parts
    )


def _config_attributes(program_id: str, lang: str) -> dict:
    """Fetch one programme's player config and return its ``attributes``."""
    url = ARTE_PLAYER_API.format(lang=lang, program_id=program_id)
    raw = fetch_text(url)
    try:
        return json.loads(raw)["data"]["attributes"]
    except (ValueError, KeyError, TypeError) as exc:
        raise FeedError(f"Unexpected player config for {program_id}: {exc}") from exc


def _player_config(program_id: str, lang: str) -> tuple[str, float]:
    """Return the ``(master_playlist_url, duration_seconds)`` ARTE reports.

    Of the streams offered, the original-language one (``VOF``) is preferred;
    the others are dubs of the same programme. Any HLS stream will do if that
    isn't there.
    """
    attributes = _config_attributes(program_id, lang)
    streams = attributes.get("streams")

    if not streams:
        # ARTE says why in `error.code`, and the two it gives mean opposite
        # things: NO_RIGHTS is a programme outside its rights window, while
        # STREAMS_MISSING is one inside it whose video ARTE has not got to yet —
        # seen on a Dessous des Cartes three days after its rights opened, and
        # still eleven days after.
        code = (attributes.get("error") or {}).get("code")
        if code == "ERROR_NO_RIGHTS":
            raise _NoRights(f"{program_id} is not on ARTE at the moment ({code})")
        if code == "ERROR_STREAMS_MISSING":
            raise _NoStreams(
                f"ARTE lists {program_id} but has no video for it yet ({code}); "
                "trying again next run"
            )
        raise FeedError(
            f"No stream available for {program_id} ({code or 'ARTE gave no reason'})"
        )

    chosen = next(
        (s for s in streams if _version_code(s).startswith("VOF")),
        streams[0],
    )
    master_url = chosen.get("url")
    if not master_url:
        raise FeedError(f"Player config for {program_id} has no stream url")

    duration = attributes.get("metadata", {}).get("duration", {}).get("seconds")
    if not duration:
        raise FeedError(f"Player config for {program_id} has no duration")
    return master_url, float(duration)


def arte_collection_id(url: str | None) -> tuple[str, str] | None:
    """Return ``(collection_id, lang)`` for an ARTE collection url, else ``None``."""
    if not url:
        return None
    match = ARTE_COLLECTION_RE.search(url)
    if not match:
        return None
    return match["collection_id"].upper(), match["lang"].lower()


def parse_arte_collection(cfg: FeedConfig) -> ParsedFeed:
    """Read an ARTE collection as if it were a feed.

    ARTE publishes no RSS, so following a show otherwise means a third-party
    feed-generator scraping the page — which yields the site's furniture instead
    of the synopsis, and stamps every item with the time it was scraped rather
    than when the episode aired. The playlist endpoint the player itself uses
    has the real thing: every episode, newest first, with its own subtitle,
    synopsis, artwork and duration.

    What it has not got is a date, which is what fetchit needs to tell new from
    old. That lives in each programme's own config, as the start of its rights
    window — the day it became available — so the newest
    :data:`ARTE_DATED_ITEMS` are looked up individually and the rest are left
    undated, and therefore never downloaded. See :data:`ARTE_DATED_ITEMS` for
    why that is the behaviour you want rather than a limitation.
    """
    collection_id, lang, attributes = _playlist(cfg.url)
    entries = attributes["items"]
    if not entries:
        raise FeedError(f"ARTE collection {collection_id} listed no episodes")

    show = attributes.get("metadata") or {}
    items = [
        apply_extracts(
            _collection_item(entry, cfg.name, lang, dated=index < ARTE_DATED_ITEMS),
            cfg.extract,
        )
        for index, entry in enumerate(entries)
    ]

    return ParsedFeed(
        items=items,
        image_url=_first_image(show),
        description=strip_html(show.get("description") or ""),
        title=str(show.get("title") or ""),
        link=cfg.url,
        author="ARTE",
        language=lang,
    )


def collection_image(url: str) -> str:
    """The show's own artwork for an ARTE collection url, as it is right now.

    The same playlist :func:`parse_arte_collection` reads, without dating any of
    its episodes — the picture is all that is wanted.
    """
    collection_id, _, attributes = _playlist(url)
    image = _first_image(attributes.get("metadata") or {})
    if image is None:
        raise FeedError(f"ARTE collection {collection_id} has no image")
    return image


def _playlist(url: str) -> tuple[str, str, dict]:
    """Fetch an ARTE collection's playlist: ``(collection_id, lang, attributes)``."""
    found = arte_collection_id(url)
    if not found:
        raise FeedError(f"Not an ARTE collection url: {url!r}")
    collection_id, lang = found

    raw = fetch_text(ARTE_PLAYLIST_API.format(lang=lang, collection_id=collection_id))
    try:
        attributes = json.loads(raw)["data"]["attributes"]
        # Touched here so a playlist without episodes reads as malformed, the
        # same as one without attributes, whichever caller wanted it.
        attributes["items"]
    except (ValueError, KeyError, TypeError) as exc:
        raise FeedError(f"Unexpected playlist for {collection_id}: {exc}") from exc
    return collection_id, lang, attributes


def _collection_item(
    entry: dict, feed_name: str, lang: str, *, dated: bool
) -> FeedItem:
    """Map one playlist entry to a :class:`~fetchit.feeds.FeedItem`."""
    program_id = str(entry.get("providerId") or "")
    # The show's name is on every entry as "title"; the episode's own name is
    # the subtitle. Using the subtitle means a chat list shows which episode
    # arrived rather than the same show name over and over.
    show_title = str(entry.get("title") or "")
    title = str(entry.get("subtitle") or "").strip() or show_title

    published = _published(program_id, lang) if dated and program_id else None

    duration = (entry.get("duration") or {}).get("seconds")
    extra = {"program_id": program_id, "show_title": show_title}
    if duration:
        extra["duration_seconds"] = str(duration)

    return FeedItem(
        title=title,
        description=strip_html(str(entry.get("description") or "")),
        link=((entry.get("link") or {}).get("url")) or None,
        published=published,
        image_url=_first_image(entry),
        feed_name=feed_name,
        extra=extra,
    )


def _published(program_id: str, lang: str) -> datetime | None:
    """When a programme became available, from its rights window.

    A date fetchit can't read is worse than no date — it would either fetch
    forever or never — so a programme that won't give one up is simply
    left undated rather than guessed at, and a failed lookup doesn't take the
    whole feed down with it.
    """
    # A date is the one thing here worth remembering between runs: the playlist
    # carries none, so every run would otherwise re-ask ARTE for a dozen of them
    # per collection, and the answer — the day a rights window opened — has been
    # settled since before the episode aired.
    hit = cache.get(_PUBLISHED_NS, program_id, max_age=cache.ARTE_PUBLISHED_TTL)
    if hit is not None:
        return _as_datetime(hit)
    if cache.get(_UNDATED_NS, program_id, max_age=cache.ARTE_UNDATED_TTL):
        return None

    try:
        attributes = _config_attributes(program_id, lang)
    except FeedError:
        return None

    begin = (attributes.get("rights") or {}).get("begin")
    published = _as_datetime(begin) if begin else None
    if published is None:
        # Remembered too, but briefly: usually this means the rights window has
        # not been announced yet, and that changes.
        cache.put(_UNDATED_NS, program_id, True)
        return None

    cache.put(_PUBLISHED_NS, program_id, begin)
    return published


def _as_datetime(begin: object) -> datetime | None:
    """Read an ARTE rights-window timestamp, or ``None`` if it isn't one."""
    try:
        return datetime.fromisoformat(str(begin)).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _first_image(entry: dict) -> str | None:
    images = entry.get("images") or []
    if not images:
        return None
    return images[0].get("url") or None


def _version_code(stream: dict) -> str:
    versions = stream.get("versions") or []
    if not versions:
        return ""
    return str(versions[0].get("code") or "")


def _variants(master: str) -> list[Variant]:
    """Parse the ``EXT-X-STREAM-INF`` rungs out of an HLS master playlist.

    ``EXT-X-I-FRAME-STREAM-INF`` entries are skipped: they are trick-play
    indexes, not playable renditions, and ffmpeg does not count them as
    programs — including them would shift every index after the first one.
    """
    variants: list[Variant] = []
    lines = [line.strip() for line in master.splitlines()]

    for pos, line in enumerate(lines):
        if not line.startswith("#EXT-X-STREAM-INF:"):
            continue
        # The rendition url is the next non-blank, non-comment line.
        url = next(
            (nxt for nxt in lines[pos + 1 :] if nxt and not nxt.startswith("#")),
            None,
        )
        if url is None:
            continue

        attrs = line.split(":", 1)[1]
        bandwidth = _attr_int(attrs, "AVERAGE-BANDWIDTH") or _attr_int(
            attrs, "BANDWIDTH"
        )
        width, height = _attr_resolution(attrs)
        if not bandwidth or not height:
            continue

        variants.append(
            Variant(
                index=len(variants),
                bandwidth=bandwidth,
                width=width,
                height=height,
                codecs=_attr_str(attrs, "CODECS"),
            )
        )

    if not variants:
        raise FeedError("HLS master playlist listed no playable variants")
    return variants


def _attr_str(attrs: str, name: str) -> str:
    match = re.search(rf'\b{name}=("([^"]*)"|[^,]*)', attrs)
    if not match:
        return ""
    return match[2] if match[2] is not None else match[1]


def _attr_int(attrs: str, name: str) -> int:
    value = _attr_str(attrs, name)
    return int(value) if value.isdigit() else 0


def _attr_resolution(attrs: str) -> tuple[int, int]:
    match = re.search(r"\bRESOLUTION=(\d+)x(\d+)", attrs)
    if not match:
        return 0, 0
    return int(match[1]), int(match[2])


def _pick_variant(
    variants: list[Variant], duration: float, quality: Quality, program_id: str
) -> tuple[Variant, int]:
    """Pick ``(rung, parts)`` under ``quality`` — see :mod:`fetchit.quality`."""
    usable = [v for v in variants if not v.codecs.startswith(_H265_CODECS)]
    if not usable:
        usable = variants

    picked = choose(
        usable,
        size=lambda v: v.estimated_bytes(duration),
        rank=lambda v: (v.width * v.height, v.bandwidth),
        height=lambda v: v.height,
        quality=quality,
    )
    if picked is None:
        smallest = min(usable, key=lambda v: v.bandwidth)
        raise too_big(
            f"Video {program_id} (even at {smallest.resolution})",
            smallest.estimated_bytes(duration),
            quality,
        )
    return picked


def _run_ffmpeg(
    master_url: str, index: int, out_path: str, *, timeout: float
) -> None:
    """Copy one rung of ``master_url`` into ``out_path`` as an mp4.

    Both maps are explicit and both are needed. ``-map p:N`` on its own takes the
    program's video *and* every audio rendition attached to it — ARTE ships four
    (french, german, and two "confort audio" mixes), which would triple the file
    for no benefit. ``:a:0`` keeps the first, which is the one the playlist marks
    default.

    ``aac_adtstoasc`` rewrites the AAC headers HLS uses into the form mp4 wants;
    without it the copy produces a file that plays silently or not at all.
    ``+faststart`` moves the index to the front so the video starts playing
    before it has finished loading.
    """
    binary = shutil.which("ffmpeg")
    if binary is None:
        raise FeedError(
            "ffmpeg is needed to fetch video items but was not found on PATH "
            "(e.g. `brew install ffmpeg`)"
        )

    cmd = [
        binary,
        "-nostdin",
        "-loglevel",
        "error",
        "-y",
        "-i",
        master_url,
        "-map",
        f"p:{index}:v:0",
        "-map",
        f"p:{index}:a:0",
        "-c",
        "copy",
        "-bsf:a",
        "aac_adtstoasc",
        "-movflags",
        "+faststart",
        out_path,
    ]

    try:
        with timing.step("ffmpeg"):
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout, check=False
            )
    except subprocess.TimeoutExpired as exc:
        raise FeedError(f"ffmpeg timed out after {timeout:.0f}s") from exc
    except OSError as exc:
        raise FeedError(f"Could not run ffmpeg: {exc}") from exc

    if result.returncode != 0:
        raise FeedError(f"ffmpeg failed: {_last_line(result.stderr)}")
    if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        raise FeedError("ffmpeg produced no video")


def _last_line(stderr: str) -> str:
    """The final line of ffmpeg's complaint — the one that says what went wrong."""
    lines = [line for line in (stderr or "").splitlines() if line.strip()]
    return lines[-1] if lines else "no error output"
