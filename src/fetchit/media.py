"""Decide what an item is, and how its media would be downloaded.

An item is one of three kinds, decided per item from what it already says, with
nothing to configure:

``audio``
    It carries an audio enclosure — a podcast episode — or is a Bayam track.
``video``
    It links to a video fetchit knows how to get (an ARTE programme, a YouTube
    video, an NPO Start episode), or carries a video enclosure of its own.
``text``
    Anything else: an article, a written note. Nothing to download; the item's
    record is the whole of it.

Some feeds are about video but carry none: the item links to a player page and
the video itself lives behind a streaming manifest or a format catalogue.
:mod:`fetchit.arte`, :mod:`fetchit.youtube` and :mod:`fetchit.npo` know how to
talk to each, and this module is the front door that decides which — so nothing
outside has to know how many sources exist or which one an item came from.

What every source has in common lives here: the :class:`Plan` — what *would* be
downloaded, worked out without downloading it, so a dry run can report the
resolution and size an item will really arrive at — and the three permanent
reasons not to download something at all.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .feeds import FeedConfig, FeedError, FeedItem, ParsedFeed, is_audio_item, is_video_enclosure
from .quality import Quality

# Bandwidth figures are averages, so a talky ten minutes lands under one and an
# action-heavy one over. The headroom absorbs that, plus container overhead.
SIZE_ESTIMATE_MARGIN = 1.1

# Generous: this is a whole video pulled a segment at a time, and the
# alternative to waiting is losing the episode.
VIDEO_TIMEOUT = 900

AUDIO = "audio"
VIDEO = "video"
TEXT = "text"


class VideoTooShort(FeedError):
    """Raised for a video not worth having at all — a Short, or a teaser.

    Distinct from a plain :class:`~fetchit.feeds.FeedError` because it means
    something different to the caller: a ``FeedError`` is "the video couldn't
    be had this time, try again next run", this is "there is nothing here worth
    downloading", and trying again tomorrow would reach the same conclusion.
    """


class VideoTooBig(FeedError):
    """Raised for a video no rendition of which fits the feed's size budget.

    Only possible with ``quality.part_size_mb`` set (see :mod:`fetchit.quality`):
    an item is only this big when its smallest rendition needs more than
    ``max_parts`` parts.

    The other permanent answer, told apart from a plain ``FeedError`` for the
    same reason as :class:`VideoTooShort`: a two-hour documentary is not 90 MB
    tomorrow either.
    """


class VideoGone(FeedError):
    """Raised for a video its source has taken down for good.

    The third permanent answer. An ARTE programme whose rights window has closed
    does not come back, and one left to retry would hold every later episode of
    its feed behind it from then on. The same goes for one that was listed and
    never turned up: at some point that is an answer too.
    """


# The reasons an item is stepped over for good rather than retried.
PERMANENT = (VideoTooShort, VideoTooBig, VideoGone)


@runtime_checkable
class Plan(Protocol):
    """What would be downloaded for an item, decided before downloading it.

    Working this out costs a couple of small requests at most, which buys two
    things: a dry run that names the real resolution and size, and the chance
    to refuse an item that can't fit before spending a download on finding out.
    """

    def describe(self) -> str:
        """One line for a dry run, e.g. ``"640x360, ~63 MB"``."""

    def fetch(self, into: str, *, timeout: float) -> str:
        """Download into directory ``into`` and return the file's path."""


# Kept under its old name: the sources were written against it.
VideoPlan = Plan


@dataclass(frozen=True)
class EnclosurePlan:
    """An item that carries its own file: a podcast episode, a video podcast.

    Nothing to choose — the feed offers one file — so nothing to ask before
    downloading it either.
    """

    url: str
    default_suffix: str = ".mp3"

    def describe(self) -> str:
        return f"file {self.url}"

    def fetch(self, into: str, *, timeout: float) -> str:
        from .download import download_to, suffix_for

        path = os.path.join(into, "media" + suffix_for(self.url, self.default_suffix))
        download_to(self.url, path)
        return path


def kind_of(item: FeedItem) -> str:
    """``"audio"``, ``"video"`` or ``"text"`` — see the module docstring.

    An audio enclosure wins over a video link: a podcast whose items link to the
    episode's page on YouTube is still a podcast.
    """
    from .bayam import is_bayam_item

    if is_bayam_item(item) or is_audio_item(item):
        return AUDIO
    if is_video_item(item) or is_video_enclosure(item):
        return VIDEO
    return TEXT


def item_id(item: FeedItem) -> str:
    """A name for ``item`` that stays the same however it was read.

    For a video source that is the video's own id, taken from the link: a
    YouTube upload is the same upload whether it came off the channel's feed,
    its yt-dlp listing or its back catalogue, and those carry different ``id``
    fields. Anything else goes by what the feed calls it — its entry id or
    guid — and failing that by its link, its enclosure, or its title and date.
    """
    from .arte import arte_program_id
    from .bayam import is_bayam_item
    from .npo import npo_episode_slug
    from .youtube import youtube_video_id

    if is_bayam_item(item) and item.extra.get("id"):
        return item.extra["id"]
    if not is_audio_item(item):
        if video := youtube_video_id(item.link):
            return f"youtube:{video}"
        if arte := arte_program_id(item.link):
            program, lang = arte
            return f"arte:{lang}:{program}"
        if slug := npo_episode_slug(item.link):
            return f"npo:{slug}"
    for key in ("id", "guid"):
        if item.extra.get(key):
            return item.extra[key]
    if item.link:
        return item.link
    if item.enclosure_url:
        return item.enclosure_url
    when = item.published.isoformat() if item.published else ""
    return f"{item.title}|{when}"


def is_video_item(item: FeedItem) -> bool:
    """Whether ``item`` links to a video fetchit knows how to fetch.

    The counterpart to :func:`fetchit.feeds.is_audio_item`, and asked the same
    way: per item, from what the item already says, with nothing to configure.
    """
    from .arte import arte_program_id
    from .npo import npo_episode_slug
    from .youtube import youtube_video_id

    link = item.link
    return (
        arte_program_id(link) is not None
        or youtube_video_id(link) is not None
        or npo_episode_slug(link) is not None
    )


def resolve(item: FeedItem, quality: Quality = Quality()) -> Plan:
    """Work out what ``item``'s media would be fetched as, without fetching it.

    Raises :class:`~fetchit.feeds.FeedError` if the item has no media, its
    source has nothing usable right now, or (with a size budget) nothing fits;
    one of :data:`PERMANENT` if it never will.
    """
    from . import arte, bayam, npo, youtube

    if bayam.is_bayam_item(item):
        return bayam.resolve(item)
    if is_audio_item(item):
        return EnclosurePlan(item.enclosure_url or "", ".mp3")
    if arte.arte_program_id(item.link):
        return arte.resolve(item, quality=quality)
    if youtube.youtube_video_id(item.link):
        return youtube.resolve(item, quality=quality)
    if npo.npo_episode_slug(item.link):
        return npo.resolve(item, quality=quality)
    if is_video_enclosure(item):
        return EnclosurePlan(item.enclosure_url or "", ".mp4")
    raise FeedError(f"Nothing to download for {item.title!r}")


def collection_feed(cfg: FeedConfig) -> ParsedFeed | None:
    """Read ``cfg.url`` as a source's own listing, if it is one.

    ``None`` means "not one of those" and the url should be parsed as a feed.
    """
    from .arte import arte_collection_id, parse_arte_collection
    from .bayam import is_bayam_link, parse_bayam
    from .npo import npo_series, parse_npo_series

    if arte_collection_id(cfg.url):
        return parse_arte_collection(cfg)
    if npo_series(cfg.url):
        return parse_npo_series(cfg)
    if is_bayam_link(cfg.url):
        return parse_bayam(cfg)
    return None


def annotate_durations(feed_url: str, items: list[FeedItem]) -> list[FeedItem]:
    """Fill in how long each item's video is, for the source that doesn't say.

    ARTE's own listing carries a duration and puts it on the item as it reads it
    (see :func:`~fetchit.arte.parse_arte_collection`); YouTube's Atom feed has
    none, so it is looked up. Either way an item ends up with the same
    ``duration_seconds`` field, which is what makes ``duration_seconds_min`` a
    filter you can write without knowing where the item came from.

    Items from anything else come back untouched — a feed that has no video in
    it pays nothing for this.
    """
    from .youtube import with_durations

    return with_durations(feed_url, items)


def check_source_available(url: str) -> None:
    """Fail fast if ``url``'s source can't be reached from this machine at all.

    Sources differ in what they cost to find that out. ARTE is read over plain
    HTTP with a short timeout, so an unreachable one announces itself in seconds
    and needs nothing here. YouTube is read through yt-dlp, several requests
    deep, each willing to wait a minute — so it gets a probe first. See
    :func:`~fetchit.youtube.check_available`.

    Raises :class:`~fetchit.feeds.SourceBlocked`, and raises nothing at all for
    a url whose source has no such check.
    """
    from .youtube import check_available

    check_available(url)


def channel_feed_url(url: str) -> str | None:
    """The feed url behind a channel page, if ``url`` is one fetchit knows.

    Where :func:`collection_feed` is for a source with no feed at all, this is
    for one that has a perfectly good feed at an address nobody would guess —
    YouTube's. Rewriting the url means everything downstream sees plain Atom.
    """
    from .youtube import youtube_feed_url

    return youtube_feed_url(url)


def source_image(url: str) -> str | None:
    """The artwork a source shows for the channel or show ``url`` names.

    ``None`` means "not one of those" and the url's feed should say instead.
    YouTube's feed has no artwork at all, which is the case this is for; ARTE's
    collection already carries it, but this reads it without dating a dozen
    episodes to get there. Raises :class:`~fetchit.feeds.FeedError` for a
    source that should have a picture and couldn't produce one.
    """
    from .arte import arte_collection_id, collection_image
    from .bayam import is_bayam_link, magazine_image
    from .npo import npo_series, series_image
    from .youtube import channel_image

    if arte_collection_id(url):
        return collection_image(url)
    if npo_series(url):
        return series_image(url)
    if is_bayam_link(url):
        return magazine_image(url)
    return channel_image(url)


def listed_items(feed_url: str, *, feed_name: str = "") -> list[FeedItem] | None:
    """The items a channel's feed would have held, read without the feed.

    For when that feed is the one part of a source that isn't answering. ``None``
    means ``feed_url`` has no other way in and its failure stands; raises
    :class:`~fetchit.feeds.FeedError` if there was one and it failed too. See
    :func:`~fetchit.youtube.latest_items`.
    """
    from .youtube import latest_items

    return latest_items(feed_url, feed_name=feed_name)


def with_archive(
    feed_url: str, items: list[FeedItem], *, feed_name: str = ""
) -> list[FeedItem]:
    """Put a channel's back catalogue in front of ``items``, if it has one.

    Asked only for a backfill (a feed with ``since``), which is the only kind
    with any use for what a source published years ago. Anything but a YouTube
    channel feed comes back untouched — ARTE's collections already carry as
    much of their own history as they carry.
    """
    from .youtube import archive_items

    return archive_items(feed_url, items, feed_name=feed_name)
