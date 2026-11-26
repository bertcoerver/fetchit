"""Read one link as one item: what a "Saved for later" feed is made of.

Every other source here is something to *follow* — a channel, a series, a
magazine — and its link stands for whatever it publishes. This module is for
the link that stands for itself: one YouTube video, one ARTE programme, one NPO
Start episode, pasted because somebody wants that one. Put in a
link list (a feed with a ``urls_file``), each such link is
a feed of exactly one item, and the list is a feed of whatever was saved.

Two things about such an item differ from the same video read off its channel.

**It is dated the moment it was saved**, not the day it went up. A saved feed
is read in the order things were put in it: a documentary from 2019 saved this
morning is this morning's item. It also has to be, for the promise
:mod:`fetchit.run` makes — a feed's items reach the library oldest first, and
whatever reads the library by date only moves forward, so an item filed today
under a date years back would never be sent on. When the source published it is
kept beside, as the ``originally_published`` field. So is where it sat in its
series (``source_season``, ``source_episode``): in a feed of loose episodes
from different series those numbers would collide, so it is numbered by its
date like any other video without a place.

"Saved" means read for the first time, not pasted: a link that could not be
read yet — its source blocked just now — is dated when it finally is, which
puts it after the ones that were read meanwhile rather than behind them.

**It is looked up once.** A feed is read every run, and a list of saved links
only grows; asking YouTube about each of them each time would be a yt-dlp
launch per link per run, for ever, to learn nothing. What the source said and
the moment it was saved are kept in the cache (see
:data:`~fetchit.cache.SAVED_ITEM_TTL` for why they may be), so a link that has
been read costs nothing from then on.
"""

from __future__ import annotations

import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from . import cache
from .feeds import FeedConfig, FeedError, FeedItem, ParsedFeed, apply_extracts

# Cache namespace: a saved link -> what its source said, and when it was saved.
_ITEM_NS = "saved_item"

_stamp_lock = threading.Lock()
_last_stamp: datetime | None = None


def item_source(url: str | None) -> str | None:
    """Which source reads ``url`` as one item, told from the address alone.

    ``None`` for anything else — a channel or a series, which is a feed to
    follow rather than an item to save, or a page fetchit doesn't know.
    """
    from .arte import arte_program_id
    from .npo import npo_episode_slug
    from .youtube import youtube_video_id

    if not url:
        return None
    if youtube_video_id(url):
        return "YouTube"
    if arte_program_id(url):
        return "ARTE"
    if npo_episode_slug(url):
        return "NPO Start"
    return None


def canonical(url: str) -> str:
    """``url`` as its source writes it, where there is one way to.

    A YouTube video has half a dozen addresses (``youtu.be/…``, ``/shorts/…``,
    ``watch?v=…&t=42s&si=…``) and an NPO episode two; saved in their one plain
    form, the same video pasted twice is the same line of the list. Anything
    else comes back as it was.

    A YouTube video linked on an Invidious front end is written as YouTube's
    own link too (:func:`~fetchit.youtube.mirrored_video_id`): the video is
    read from YouTube, whichever window it was seen through.
    """
    from .npo import NPO_EPISODE_URL, npo_episode_slug
    from .youtube import YOUTUBE_WATCH, mirrored_video_id, youtube_video_id

    if video_id := youtube_video_id(url) or mirrored_video_id(url):
        return YOUTUBE_WATCH.format(video_id=video_id)
    if slug := npo_episode_slug(url):
        return NPO_EPISODE_URL.format(slug=slug)
    return url


def parse_item(cfg: FeedConfig) -> ParsedFeed:
    """Read the one item behind ``cfg.url`` as a feed of it.

    The feed says nothing about itself — no title, no artwork — because a
    single video is not a show: a feed of saved links goes by the name its
    config gives it.

    Raises :class:`~fetchit.feeds.SourceBlocked` or
    :class:`~fetchit.feeds.FeedError` if the link has not been read before and
    can't be now; nothing is remembered of that, and the next run asks again.
    """
    url = cfg.url
    item = _remembered(url)
    if item is None:
        item = _as_saved(_look_up(url), _stamp())
        cache.put(_ITEM_NS, url, _to_json(item))
    item = replace(item, feed_name=cfg.name)
    return ParsedFeed(items=[apply_extracts(item, cfg.extract)])


def _look_up(url: str) -> FeedItem:
    """Ask ``url``'s source about the one item it names."""
    from . import arte, npo, youtube
    from .media import check_source_available

    check_source_available(url)
    if youtube.youtube_video_id(url):
        return youtube.video_item(url)
    if arte.arte_program_id(url):
        return arte.program_item(url)
    if npo.npo_episode_slug(url):
        return npo.episode_item(url)
    raise FeedError(f"Not the link of one video fetchit can read: {url!r}")


def _as_saved(item: FeedItem, saved: datetime) -> FeedItem:
    """``item`` as a saved feed files it — see the module docstring."""
    extra = dict(item.extra)
    extra["saved"] = saved.isoformat()
    if item.published is not None:
        extra["originally_published"] = item.published.isoformat()
    for key in ("season", "episode"):
        if key in extra:
            extra[f"source_{key}"] = extra.pop(key)

    # Among loose episodes of different shows, "Aflevering 5" names nothing.
    title = item.title
    show = extra.get("show_title", "").strip()
    if show and item.author != show and show.casefold() not in title.casefold():
        title = f"{show} - {title}"
    return replace(item, title=title, published=saved, extra=extra)


def _stamp() -> datetime:
    """Now, to the second, and never the same second twice.

    A record's date is written to the second and whatever reads the library by
    date compares strictly, so two links read within one second would otherwise
    be one moment and the second of them never sent on.
    """
    global _last_stamp
    with _stamp_lock:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        if _last_stamp is not None and now <= _last_stamp:
            now = _last_stamp + timedelta(seconds=1)
        _last_stamp = now
        return now


def _to_json(item: FeedItem) -> dict:
    return {
        "title": item.title,
        "description": item.description,
        "link": item.link,
        "published": item.published.isoformat() if item.published else None,
        "image_url": item.image_url,
        "author": item.author,
        "categories": list(item.categories),
        "extra": dict(item.extra),
    }


def _remembered(url: str) -> FeedItem | None:
    """The item ``url`` was read as before, or ``None`` if it has to be asked for."""
    stored = cache.get(_ITEM_NS, url, max_age=cache.SAVED_ITEM_TTL)
    if not isinstance(stored, dict):
        return None
    try:
        published = datetime.fromisoformat(stored["published"])
        extra = {str(k): str(v) for k, v in stored["extra"].items()}
        return FeedItem(
            title=str(stored["title"]),
            description=str(stored.get("description") or ""),
            link=stored.get("link") or url,
            published=published if published.tzinfo else published.replace(tzinfo=timezone.utc),
            image_url=stored.get("image_url") or None,
            author=stored.get("author") or None,
            categories=tuple(str(c) for c in stored.get("categories") or ()),
            extra=extra,
        )
    except (KeyError, TypeError, ValueError, AttributeError):
        # Not something this version wrote: ask again, as if it were not there.
        return None
