"""Follow a YouTube channel, and attach the video behind each of its items.

Discovery needs no API key and no scraping: YouTube publishes an Atom feed per
channel at ``/feeds/videos.xml?channel_id=UC…`` carrying the last 15 uploads with
their real publication dates, titles, descriptions and thumbnails. So a channel
url is not parsed here at all — it is *rewritten* to that address and handed to
feedparser like any other feed, which is why filters, templates, extracts and
previews all work on a YouTube feed without knowing it is one.

A key would buy handle resolution, durations and backfill past 15 items. All
three are had here without one — the first two from yt-dlp's flat listings (see
:func:`youtube_feed_url` and :func:`with_durations`), the third from the
channel's uploads playlist (see :func:`archive_items`).

The backfill is only ever asked for by a feed with ``since`` set, because for
anything else it would be a mistake: a channel newly followed should start with
its latest video, not a decade of them. A series watched from the beginning is
the exception that wants the decade.

Downloading is yt-dlp's job. It is run as a command rather than imported, for
the same reason ffmpeg is: ``subprocess.run`` gives the wall-clock timeout its
python API has no knob for, ``-J`` is a stable documented interface where that
api is not across yt-dlp's weekly releases, and a missing binary then reads the
same way a missing ffmpeg does. It is run signed out unless the environment says
otherwise (see :data:`COOKIES_ENV`), which is enough until YouTube decides an
address looks like a robot.

Which quality: the same "sharpest that fits" rule as ARTE, over a different
catalogue. YouTube serves video and audio separately above 360p, so a plan names
a pair of formats and yt-dlp muxes them; the sizes are usually exact rather than
estimated, because ``-J`` reports them.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from . import cache, timing
from .feeds import FeedError, FeedItem, SourceBlocked
from .media import SIZE_ESTIMATE_MARGIN, VideoTooShort
from .quality import Quality, choose, in_parts, too_big

# A YouTube video, in any of the shapes a feed or a human might write it.
YOUTUBE_LINK_RE = re.compile(
    r"https?://(?:(?:www|m|music)\.)?(?:"
    r"youtube\.com/(?:watch\?(?:[^#\s]*&)?v=|shorts/|embed/|live/|v/)"
    r"|youtu\.be/"
    r")(?P<video_id>[\w-]{11})",
    re.IGNORECASE,
)

# A channel, in the four forms YouTube still hands out: the canonical id, the
# modern @handle, and the two legacy vanity paths.
YOUTUBE_CHANNEL_RE = re.compile(
    r"https?://(?:(?:www|m)\.)?youtube\.com/(?:"
    r"channel/(?P<channel_id>UC[\w-]+)"
    r"|@(?P<handle>[\w.-]+)"
    r"|c/(?P<vanity>[\w.-]+)"
    r"|user/(?P<user>[\w.-]+)"
    r")(?:/[\w-]*)?(?:[/?#]|$)",
    re.IGNORECASE,
)

YOUTUBE_FEED = "https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
YOUTUBE_WATCH = "https://www.youtube.com/watch?v={video_id}"
YOUTUBE_UPLOADS = "https://www.youtube.com/channel/{channel_id}/videos"

# Every video a channel has posted, newest first. Not the ``/videos`` tab above:
# that one is for the latest handful and is *not* dependable in order past it —
# asked for its three-hundredth video it answers with something from the middle
# of the channel's history. The uploads playlist, whose id is the channel's with
# its "UC" swapped for "UU", is the ordered one, and is what a backfill
# walks. See :func:`uploads`.
YOUTUBE_PLAYLIST = "https://www.youtube.com/playlist?list=UU{suffix}"

# What the reachability probe asks for. The host is the whole question, so it
# asks for as little of it as there is.
YOUTUBE_PROBE = "https://www.youtube.com/"

# Hosts a YouTube feed or video is read from, and so the ones a network-level
# block takes out. Matched as whole labels, not substrings, so that a lookalike
# domain can't pass for one of these.
YOUTUBE_HOSTS = frozenset(
    {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "youtu.be",
    }
)

# The channel id, read back out of the feed url fetchit rewrote a channel to.
FEED_CHANNEL_RE = re.compile(r"[?&]channel_id=(?P<channel_id>UC[\w-]+)")

# How many uploads the Atom feed carries, and so how far down the channel's own
# listing the durations for them can be.
FEED_VIDEO_COUNT = 15

# Below this, an upload is a Short or a teaser rather than something worth its
# own message. A compromise, and worth knowing it is one: Shorts now run to
# three minutes, but a three-minute floor would throw away real short videos.
MIN_VIDEO_SECONDS = 90

# Reading a channel's id or a video's formats is one small request on yt-dlp's
# side; it should not be able to hang a run.
YTDLP_QUERY_TIMEOUT = 60

# Listing a whole channel is several requests deep — yt-dlp pages through it a
# hundred videos at a time — so it gets its own, longer, allowance. A thousand
# videos comes back in well under ten seconds; this is the ceiling for a channel
# many times that size on a bad line, not the expected cost.
YTDLP_LISTING_TIMEOUT = 300

# How many archive videos may be dated per run. Each costs a yt-dlp launch and a
# request, and the dates are kept forever, so this is the rate a back catalogue
# is opened up at rather than a limit on anything. Twenty-five a run stays far
# ahead of a feed releasing an episode every few days, and gets through even a
# very long channel inside a day of ordinary runs.
ARCHIVE_DATE_BUDGET = 25

# A refreshed listing that has lost more than this fraction of what was cached is
# not believed — see :func:`uploads`.
ARCHIVE_SHRINK_LIMIT = 0.9

# YouTube increasingly refuses anonymous requests from datacentre and repeat-
# visitor addresses, asking them to "confirm you're not a bot". The only answer
# it accepts is a signed-in session, so these hand yt-dlp one: a cookies.txt
# export, or a browser profile to read the cookies out of. Neither is set by
# default — most channels never ask — and every yt-dlp call gets them, because
# the listing that fills in durations is challenged the same way a download is.
COOKIES_ENV = "FETCHIT_YOUTUBE_COOKIES"
COOKIES_FROM_BROWSER_ENV = "FETCHIT_YOUTUBE_COOKIES_FROM_BROWSER"

# The distinctive part of that refusal, matched loosely because the wording
# around it moves and the apostrophe is a typographic one.
_BOT_CHECK = "not a bot"

# Ways YouTube says a video is not ours to have. These are told apart from
# ordinary failures because they are permanent and the difference decides
# whether a series can move: see :func:`_published`. Matched loosely, on the
# distinctive middle of each phrase, since the wording around them moves.
_INACCESSIBLE = (
    "confirm your age",
    "private video",
    "members-only",
    "join this channel",
    "video unavailable",
    "video has been removed",
    "removed by the uploader",
    "account associated with this video has been terminated",
)

# Video codecs to leave alone. VP9 and AV1 rungs duplicate resolutions that also
# exist in H.264 and play back unevenly on phones and televisions; Opus audio has
# no place in an mp4. An avc1/m4a pair is always sitting next to them.
_VIDEO_CODEC = "avc1"
_AUDIO_CODEC = "mp4a"

# Cache namespaces: what channel an address names, how long that channel's
# latest uploads are, everything it has ever posted in order, and when a given
# video went up. See :mod:`fetchit.cache`.
_CHANNEL_NS = "youtube_channel_id"
_DURATIONS_NS = "youtube_durations"
_UPLOADS_NS = "youtube_uploads"
_PUBLISHED_NS = "youtube_published"
_UNDATED_NS = "youtube_undated"

# Where a video's artwork lives. Derived rather than fetched: the address is the
# video id in a fixed shape, and an archive listing carries no thumbnails.
YOUTUBE_THUMBNAIL = "https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"


@dataclass(frozen=True)
class YoutubePlan:
    """The formats a YouTube item would be fetched at.

    ``selector`` is yt-dlp's ``-f`` argument: either a single progressive format
    id, or ``"<video>+<audio>"`` for the separate streams YouTube serves above
    360p, which yt-dlp muxes into one mp4 without re-encoding.
    """

    url: str
    selector: str
    width: int
    height: int
    estimated_bytes: int
    parts: int = 1

    @property
    def resolution(self) -> str:
        return f"{self.width}x{self.height}"

    def describe(self) -> str:
        mb = self.estimated_bytes / (1024 * 1024)
        return f"{self.resolution}, ~{mb:.0f} MB{in_parts(self.parts)}"

    def fetch(self, into: str, *, timeout: float) -> str:
        """Download the chosen formats into ``into``, returning the file's path."""
        return _run_download(self.url, self.selector, into, timeout=timeout)


def is_youtube_url(url: str | None) -> bool:
    """Whether ``url`` is one that would be read from YouTube."""
    if not url:
        return False
    return (urlparse(url).hostname or "").lower() in YOUTUBE_HOSTS


def check_available(url: str) -> None:
    """Raise :class:`SourceBlocked` if ``url`` is YouTube's and YouTube is not up.

    Reading a YouTube feed means several requests through yt-dlp, each with its
    own minute-long timeout. When the host is not reachable at all, those spend
    minutes arriving at what one HEAD request answers in milliseconds, and leave
    a page of traceback for a condition that isn't a fault.

    The case this is really for is a scheduled DNS-level block — a NextDNS
    profile that closes YouTube during the day and opens it at night. A run that
    lands inside the closed window should say so in one line and move on to the
    other feeds; the next run inside the open window reads the feed and picks up
    everything it missed, having lost nothing.

    Anything that isn't a YouTube url returns immediately, so a config with no
    YouTube in it never pays for this.
    """
    if not is_youtube_url(url):
        return

    from .download import reachable

    if not reachable(YOUTUBE_PROBE):
        raise SourceBlocked(
            f"{YOUTUBE_PROBE} is not reachable from here — a DNS or firewall "
            "block, not a broken feed. Skipping until it lifts."
        )


def youtube_video_id(link: str | None) -> str | None:
    """Return the eleven-character video id in ``link``, else ``None``."""
    if not link:
        return None
    match = YOUTUBE_LINK_RE.search(link)
    return match["video_id"] if match else None


def youtube_feed_url(url: str | None) -> str | None:
    """The Atom feed for a YouTube channel page, or ``None`` if not one.

    A ``/channel/UC…`` url is rewritten with no network at all — the id is
    already in it. The other three forms name the channel without identifying
    it, so yt-dlp is asked for the id, listing exactly one video because it is
    the channel's own fields that are wanted, not its contents.
    """
    if not url:
        return None
    # The feed itself is a youtube.com url and must be left alone, or reading a
    # feed would mean resolving it all over again.
    if "/feeds/videos.xml" in url:
        return None

    match = YOUTUBE_CHANNEL_RE.search(url)
    if not match:
        return None

    channel_id = match["channel_id"] or _channel_id(url)
    return YOUTUBE_FEED.format(channel_id=channel_id)


def channel_image(url: str | None) -> str | None:
    """The channel's own picture, for a YouTube channel page or feed url.

    The Atom feed describes the videos and not the channel — it carries no
    artwork of its own — so a group named after a YouTube feed would otherwise
    start out with no picture at all. yt-dlp's listing of the channel does carry
    it, beside the banners, and asking that listing for no videos at all keeps
    this to one small request.

    ``None`` for a url that isn't a YouTube channel. Raises
    :class:`~fetchit.feeds.FeedError` if the lookup fails, or if the channel
    turns out to have no picture.
    """
    if not is_youtube_url(url):
        return None
    assert url is not None
    feed = FEED_CHANNEL_RE.search(url)
    if feed:
        channel_id = feed["channel_id"]
    else:
        page = YOUTUBE_CHANNEL_RE.search(url)
        if not page:
            return None
        channel_id = page["channel_id"] or _channel_id(url)

    info = _ytdlp_json(
        [
            "--flat-playlist",
            "--playlist-items",
            "0",
            YOUTUBE_UPLOADS.format(channel_id=channel_id),
        ],
        timeout=YTDLP_QUERY_TIMEOUT,
    )
    avatar = _avatar(info.get("thumbnails") or [])
    if avatar is None:
        raise FeedError(f"YouTube reported no picture for channel {channel_id}")
    return avatar


def _avatar(thumbnails: list) -> str | None:
    """Pick the channel's avatar out of a listing's thumbnails.

    The listing mixes the avatar in with half a dozen crops of the banner. yt-dlp
    names the full-size avatar ``avatar_uncropped``; failing that, the avatar is
    the largest square one, since every banner crop is several times wider than
    it is tall.
    """
    candidates = [t for t in thumbnails if isinstance(t, dict) and t.get("url")]
    for thumb in candidates:
        if thumb.get("id") == "avatar_uncropped":
            return str(thumb["url"])
    square = [
        t for t in candidates if t.get("width") and t.get("width") == t.get("height")
    ]
    if not square:
        return None
    return str(max(square, key=lambda t: t["width"])["url"])


def with_durations(feed_url: str, items: list[FeedItem]) -> list[FeedItem]:
    """Return ``items`` with ``duration_seconds`` filled in, if they are YouTube's.

    The Atom feed says how a video is titled, described and thumbnailed, and
    nothing at all about how long it is — which is the one thing a channel that
    posts both clips and full episodes has to be filtered on. yt-dlp's flat
    listing of the channel does say, for every upload at once, so this costs one
    small request per feed rather than one per video.

    Items keep the id they already carry: the listing is matched to the feed by
    video id, not by position, so the two disagreeing about order or about which
    fifteen uploads are the latest costs a duration rather than mismatching one.

    ``feed_url`` that isn't a YouTube channel feed leaves ``items`` alone. So
    does a lookup that fails: a duration is worth a request, not worth losing
    the feed over — though a filter that needs one will then drop the items,
    which is what the warning is for.
    """
    match = FEED_CHANNEL_RE.search(feed_url or "")
    if not match or not items:
        return items
    if all("duration_seconds" in item.extra for item in items):
        # Built from a listing already (see latest_items), which said.
        return items

    try:
        durations = _channel_durations(match["channel_id"])
    except FeedError as exc:
        print(
            f"Could not read how long the videos in {feed_url} are: {exc}\n"
            "They have no duration_seconds this run.",
            file=sys.stderr,
        )
        return items

    annotated = []
    for item in items:
        seconds = durations.get(youtube_video_id(item.link) or "")
        if seconds is None:
            annotated.append(item)
            continue
        annotated.append(
            replace(item, extra={**item.extra, "duration_seconds": str(int(seconds))})
        )
    return annotated


@dataclass(frozen=True)
class Upload:
    """One video in a channel's uploads playlist: what a flat listing gives.

    Deliberately thin, because that is all there is. A flat listing names the
    video, titles it and says how long it is; it carries no description and no
    date, which is what :func:`_published` and the notes on
    :func:`archive_items` are about.
    """

    video_id: str
    title: str
    duration: float | None = None


def archive_items(
    feed_url: str,
    items: list[FeedItem],
    *,
    feed_name: str = "",
    budget: int = ARCHIVE_DATE_BUDGET,
) -> list[FeedItem]:
    """Return ``items`` with the channel's back catalogue in front of them.

    Only for a backfill (a feed with ``since``), and this is the whole of what
    makes one possible on YouTube: the Atom feed carries fifteen uploads, and a
    series watched from the beginning needs all of them.

    The catalogue is walked oldest first and stops at the first video whose date
    isn't known yet — the **frontier**. That is the load-bearing rule here.
    Undated items are never downloaded, and the library is filled in
    publication order because whatever reads it — rssignal walking a series —
    goes by publication date: an undated video left sitting in the middle of the
    queue would be stepped over, and once it *did* get a date it would land
    behind everything its reader had already moved past. Stopping short instead
    costs a run; skipping costs an episode, quietly, which is much worse.

    So a feed whose archive isn't fully dated yet is offered only the part of it
    that is, and none of the Atom items — they are years newer, and letting them
    past would be the same silent step over everything in between. Dates are
    filled in ``budget`` at a time and kept forever, so this state lasts hours,
    not long.

    Items from the Atom feed win wherever the two overlap: same video, but with a
    description and a real publication date rather than a derived one.

    Dates are also nudged apart as they are frozen, by
    :func:`_distinct` — YouTube really does publish several videos in the same
    second, and two items sharing a timestamp would cost one of them.

    A ``feed_url`` that isn't a YouTube channel feed leaves ``items`` alone, and
    so does a listing that can't be read — an archive is worth a request, not
    worth losing the feed's newest fifteen over.
    """
    match = FEED_CHANNEL_RE.search(feed_url or "")
    if not match:
        return items

    try:
        catalogue = uploads(match["channel_id"])
    except FeedError as exc:
        print(
            f"Could not read the back catalogue of {feed_url}: {exc}\n"
            "This run has only the videos the feed itself lists.",
            file=sys.stderr,
        )
        return items

    published = {
        video_id: item
        for item in items
        if (video_id := youtube_video_id(item.link)) and item.published is not None
    }

    out: list[FeedItem] = []
    spent = 0
    complete = True
    last: datetime | None = None  # the date of the item behind this one
    for upload in reversed(catalogue):  # oldest first, the order it is walked in
        known = published.get(upload.video_id)
        if known is not None:
            out.append(known)
            last = known.published
            continue

        remembered = _remembered_date(upload.video_id)
        when = remembered
        if when is None:
            if spent >= budget:
                complete = False
                break
            spent += 1
            try:
                when = _published(upload.video_id)
            except FeedError as exc:
                # Asking failed, which is not the same as there being no answer:
                # stop here rather than stepping over a video that may well have
                # a date next run.
                print(
                    f"Could not read when {upload.video_id} went up: {exc}",
                    file=sys.stderr,
                )
                complete = False
                break
            if when is None:
                # Asked, and told this one is not ours to have — age-gated,
                # private, removed. It can never be ordered or sent, and the
                # answer will not change, so it is not a gap in the queue; it is
                # not in the queue. See :func:`_published`.
                continue

        when = _distinct(when, last)
        if when != remembered:
            # Freeze whatever this walk settled on, nudge included, so the next
            # run reads back the same order it sent in.
            cache.put(_PUBLISHED_NS, upload.video_id, when.isoformat())
        out.append(_archive_item(upload, when, feed_name))
        last = when

    if complete:
        # The whole catalogue is dated, so anything the feed lists that the
        # playlist copy hasn't caught up with yet belongs on the end.
        seen = {youtube_video_id(item.link) for item in out}
        out.extend(item for item in items if youtube_video_id(item.link) not in seen)
    return out


def latest_items(feed_url: str, *, feed_name: str = "") -> list[FeedItem] | None:
    """The newest uploads of ``feed_url``'s channel, built without the Atom feed.

    The feed endpoint has an outage all of its own now and then: for hours it
    answers every channel with an HTML error page while the rest of YouTube —
    and yt-dlp with it — works fine. Nothing else about the channel has changed,
    so rather than skip it, this reads the same fifteen uploads off the uploads
    playlist and dates each one the way :func:`archive_items` does.

    Those dates are the Atom feed's to the second (both are the video's publish
    time), which is what makes this safe to switch to and back from: an item
    one of them listed, the other lists the same way. Nothing is nudged apart with
    :func:`_distinct` here, for the same reason — the feed doesn't either.

    A date costs a yt-dlp launch the first time and nothing after
    (:data:`~fetchit.cache.YOUTUBE_PUBLISHED_TTL`), so a run on the listing
    costs one launch per *new* video, once the channel's latest are known. The
    items are thinner than the feed's — no description, as with an archive item.

    Dated oldest first, stopping at the first video that can't be asked about:
    the frontier rule from :func:`archive_items`, and for the same reason. The
    library is filled in publication order, so a video skipped now while a
    newer one went in would land behind it by the time it could be dated.

    ``None`` for a url that isn't a YouTube channel feed. Raises
    :class:`~fetchit.feeds.FeedError` if the listing itself can't be read.
    """
    match = FEED_CHANNEL_RE.search(feed_url or "")
    if not match or not is_youtube_url(feed_url):
        return None

    listed = _list_uploads(match["channel_id"], limit=FEED_VIDEO_COUNT)
    print(
        f"{feed_url} is not answering with a feed; read the channel's latest "
        f"{len(listed)} uploads through yt-dlp instead.",
        file=sys.stderr,
    )

    out: list[FeedItem] = []
    for upload in reversed(listed):  # oldest first, the order it is walked in
        when = _remembered_date(upload.video_id)
        if when is None:
            try:
                when = _published(upload.video_id)
            except FeedError as exc:
                print(
                    f"Could not read when {upload.video_id} went up: {exc}\n"
                    "It and anything newer wait for the next run.",
                    file=sys.stderr,
                )
                break
            if when is None:
                continue  # not ours to have — see _published
        out.append(_archive_item(upload, when, feed_name))
    return out


def uploads(channel_id: str) -> list[Upload]:
    """Every video a channel has posted, newest first.

    One request's worth of yt-dlp for a whole channel — a thousand videos come
    back in a few seconds — against the uploads playlist rather than the
    ``/videos`` tab, which does not stay in order past its first page.

    Kept for a week, because a playlist only grows at the newest end: a copy that
    is a few days old is a few days short, never wrong about the order of what it
    has. A refresh that fails falls back to that copy rather than losing the
    archive, and one that comes back much shorter than the copy is not believed —
    a partial listing would look exactly like a channel that had deleted most of
    itself, and acting on it would drop the middle out of a series.
    """
    remembered = _cached_uploads(cache.get(_UPLOADS_NS, channel_id))
    fresh = _cached_uploads(
        cache.get(_UPLOADS_NS, channel_id, max_age=cache.YOUTUBE_UPLOADS_TTL)
    )
    if fresh:
        return fresh

    try:
        listed = _list_uploads(channel_id)
    except FeedError:
        if remembered:
            return remembered
        raise

    merged = _merge_uploads(remembered, listed)
    # Stored even when the listing was rejected, so a channel that keeps giving
    # short answers is asked once a week rather than once a run.
    cache.put(
        _UPLOADS_NS,
        channel_id,
        [[u.video_id, u.title, u.duration] for u in merged],
    )
    return merged


def _merge_uploads(remembered: list[Upload], listed: list[Upload]) -> list[Upload]:
    """Reconcile a fresh listing with the copy already held.

    Normally the fresh one simply wins: it is longer, in the same order, and has
    dropped whatever the channel has deleted. A listing that has lost a real part
    of the archive is the case worth handling — see :func:`uploads` — and there
    the held order is kept, with anything genuinely new put in front of it.
    """
    if not remembered:
        return listed
    if len(listed) >= len(remembered) * ARCHIVE_SHRINK_LIMIT:
        return listed

    known = {upload.video_id for upload in remembered}
    return [u for u in listed if u.video_id not in known] + remembered


def _cached_uploads(raw: object) -> list[Upload]:
    """Rebuild an uploads listing from what the cache holds, or nothing."""
    if not isinstance(raw, list):
        return []
    out: list[Upload] = []
    for row in raw:
        if not isinstance(row, list) or not row or not isinstance(row[0], str):
            continue
        duration = row[2] if len(row) > 2 else None
        out.append(
            Upload(
                video_id=row[0],
                title=str(row[1]) if len(row) > 1 and row[1] else "",
                duration=float(duration) if isinstance(duration, (int, float)) else None,
            )
        )
    return out


def _list_uploads(channel_id: str, *, limit: int | None = None) -> list[Upload]:
    """Ask yt-dlp for the channel's uploads playlist: in full, or its newest ``limit``."""
    window = ["--playlist-items", f"1:{limit}"] if limit else []
    info = _ytdlp_json(
        [
            "--flat-playlist",
            *window,
            YOUTUBE_PLAYLIST.format(suffix=channel_id[2:]),
        ],
        timeout=YTDLP_QUERY_TIMEOUT if limit else YTDLP_LISTING_TIMEOUT,
    )

    out: list[Upload] = []
    for entry in _videos(info):
        video_id = entry.get("id")
        if not video_id:
            continue
        duration = entry.get("duration")
        out.append(
            Upload(
                video_id=str(video_id),
                title=str(entry.get("title") or "").strip(),
                duration=float(duration) if duration else None,
            )
        )
    if not out:
        raise FeedError(f"The uploads playlist for {channel_id} listed no videos")
    return out


def _distinct(when: datetime, last: datetime | None) -> datetime:
    """Push ``when`` past ``last`` if they collide, by the smallest step there is.

    A channel that uploads a batch really does stamp several videos with the same
    second — three such pairs turned up in the first three hundred of one
    channel's archive. Whatever reads the library by publication date compares
    strictly — rssignal's watermark does — so two items sharing a publication
    date means the second one is behind the mark the first one set, and is
    never sent.

    The playlist order is what settles which comes first; this only makes that
    order representable as a date. A second's drift on a video from 2014 costs
    nothing, and the nudged value is what gets frozen, so it does not have to be
    worked out the same way twice.
    """
    if last is None or when > last:
        return when
    return last + timedelta(seconds=1)


def _remembered_date(video_id: str) -> datetime | None:
    """The date already held for ``video_id``, without asking YouTube anything."""
    hit = cache.get(_PUBLISHED_NS, video_id, max_age=cache.YOUTUBE_PUBLISHED_TTL)
    if not isinstance(hit, str):
        return None
    try:
        when = datetime.fromisoformat(hit)
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def _published(video_id: str) -> datetime | None:
    """When ``video_id`` went up, asking YouTube if nothing is remembered.

    ``%(timestamp)s`` rather than ``%(upload_date)s``: the second is a date and
    nothing finer, and a channel that posts three videos in a morning would give
    all three the same one — and whatever reads the library by publication
    date compares strictly, so two of the three would never be sent on. The
    timestamp is to the second.

    ``None`` means the video is not ours to have: age-gated, private,
    members-only, removed. That is remembered for good (see
    :data:`~fetchit.cache.YOUTUBE_UNDATED_TTL`) and the caller steps over it,
    which is safe precisely because the answer will never change. It matters more
    than it sounds: a single age-gated video in a channel's back catalogue would
    otherwise be a wall the series could never get past, since
    :func:`archive_items` stops at anything it can't date.

    Anything else — a timeout, a refused connection, a bot check — raises, and
    the walk stops there rather than stepping over a video that will very
    probably have a date next run.
    """
    if cache.get(_UNDATED_NS, video_id, max_age=cache.YOUTUBE_UNDATED_TTL):
        return None

    try:
        result = _run_ytdlp(
            [
                "--skip-download",
                "--no-warnings",
                "--no-playlist",
                "--print",
                "%(timestamp)s",
                YOUTUBE_WATCH.format(video_id=video_id),
            ],
            timeout=YTDLP_QUERY_TIMEOUT,
        )
    except FeedError as exc:
        if not _inaccessible(str(exc)):
            raise
        cache.put(_UNDATED_NS, video_id, True)
        return None

    stamp = _last_line(result.stdout)
    try:
        when = datetime.fromtimestamp(int(stamp), timezone.utc)
    except (ValueError, OSError, OverflowError):
        # yt-dlp prints "NA" for a field it hasn't got.
        cache.put(_UNDATED_NS, video_id, True)
        return None

    cache.put(_PUBLISHED_NS, video_id, when.isoformat())
    return when


def _inaccessible(complaint: str) -> bool:
    """Whether yt-dlp is saying this video is not ours to have, permanently."""
    lowered = complaint.lower()
    return any(phrase in lowered for phrase in _INACCESSIBLE)


def _archive_item(upload: Upload, when: datetime, feed_name: str) -> FeedItem:
    """Build the item for an archive video, from a listing and a date.

    Thinner than the Atom feed's version of the same video, and knowingly so:
    there is no description to be had from a flat listing, so a
    ``description_contains`` or ``description_excludes`` filter has nothing to
    read on these and will treat every one of them the same way. Titles,
    durations and dates are all real. The thumbnail address is derived from the
    id rather than fetched, so a preview card still has artwork.

    ``id`` is written in the shape YouTube's own Atom feed uses, so that an
    item's identity does not change under it as the walk crosses from the archive
    into the feed's own fifteen.
    """
    extra = {"id": f"yt:video:{upload.video_id}"}
    if upload.duration:
        extra["duration_seconds"] = str(int(upload.duration))

    return FeedItem(
        title=upload.title,
        description="",
        link=YOUTUBE_WATCH.format(video_id=upload.video_id),
        published=when,
        image_url=YOUTUBE_THUMBNAIL.format(video_id=upload.video_id),
        feed_name=feed_name,
        extra=extra,
    )


def resolve(item: FeedItem, *, quality: Quality = Quality()) -> YoutubePlan:
    """Work out which formats ``item``'s video would be fetched at.

    Raises :class:`VideoTooShort` for a Short, and
    :class:`~fetchit.feeds.FeedError` if the item is not a YouTube video, the
    video can't be read, or nothing fits ``quality``'s size budget.
    """
    video_id = youtube_video_id(item.link)
    if not video_id:
        raise FeedError(f"Not a YouTube link: {item.link!r}")

    url = YOUTUBE_WATCH.format(video_id=video_id)
    info = _video_info(url)

    duration = info.get("duration")
    if duration and float(duration) < MIN_VIDEO_SECONDS:
        raise VideoTooShort(
            f"{video_id} is {float(duration):.0f}s — a Short, not downloading it"
        )

    formats = info.get("formats") or []
    if not formats:
        raise FeedError(f"YouTube offered no formats for {video_id}")

    return _pick(formats, float(duration or 0), quality, url, video_id)


def _channel_id(url: str) -> str:
    """Ask yt-dlp what channel ``url`` names, or recall what it said last time.

    Worth remembering because the answer is fixed — a handle names the channel it
    was created for — while asking costs a yt-dlp launch and a signed-out request
    to YouTube, on every run, for a feed whose address never changes.
    """
    hit = cache.get(_CHANNEL_NS, url, max_age=cache.YOUTUBE_CHANNEL_TTL)
    if isinstance(hit, str) and hit.startswith("UC"):
        return hit

    info = _ytdlp_json(
        ["--flat-playlist", "--playlist-items", "1", url],
        timeout=YTDLP_QUERY_TIMEOUT,
    )
    channel_id = info.get("channel_id") or info.get("uploader_id")
    if not channel_id or not str(channel_id).startswith("UC"):
        raise FeedError(f"Could not work out the channel id for {url!r}")
    cache.put(_CHANNEL_NS, url, str(channel_id))
    return str(channel_id)


def _channel_durations(channel_id: str) -> dict[str, float]:
    """Ask yt-dlp how long each of a channel's latest uploads is.

    ``--flat-playlist`` is what makes this one request: yt-dlp lists the channel
    without opening any of the videos, and still reports each one's duration.

    Held only briefly between runs — unlike the other things fetchit remembers,
    this one really does go stale, because which fifteen uploads are the latest
    changes every time the channel posts.
    """
    hit = cache.get(_DURATIONS_NS, channel_id, max_age=cache.YOUTUBE_DURATIONS_TTL)
    if isinstance(hit, dict):
        return {str(k): float(v) for k, v in hit.items()}

    info = _ytdlp_json(
        [
            "--flat-playlist",
            "--playlist-items",
            f"1:{FEED_VIDEO_COUNT}",
            YOUTUBE_UPLOADS.format(channel_id=channel_id),
        ],
        timeout=YTDLP_QUERY_TIMEOUT,
    )

    durations: dict[str, float] = {}
    for entry in _videos(info):
        video_id = entry.get("id")
        duration = entry.get("duration")
        if video_id and duration:
            durations[str(video_id)] = float(duration)
    cache.put(_DURATIONS_NS, channel_id, durations)
    return durations


def _videos(playlist: dict) -> Iterator[dict]:
    """Walk a yt-dlp listing down to the videos in it.

    A channel url can list its *tabs* rather than its uploads, one level of
    playlist deeper, so the entries are followed until they stop nesting.
    """
    for entry in playlist.get("entries") or []:
        if not isinstance(entry, dict):
            continue
        if entry.get("entries"):
            yield from _videos(entry)
        else:
            yield entry


def _video_info(url: str) -> dict:
    return _ytdlp_json(["--no-playlist", url], timeout=YTDLP_QUERY_TIMEOUT)


def _pick(
    formats: list[dict], duration: float, quality: Quality, url: str, video_id: str
) -> YoutubePlan:
    """Choose the video (plus audio) to fetch, and how many parts it will be.

    The sharpest usable pairing under ``quality`` — see :mod:`fetchit.quality`.
    """
    audio = _smallest_audio(formats, duration)

    candidates: list[tuple[tuple[int, int], YoutubePlan]] = []
    for fmt in formats:
        pair = _pairing(fmt, audio, duration)
        if pair is None:
            continue
        selector, size = pair
        plan = YoutubePlan(
            url=url,
            selector=selector,
            width=int(fmt.get("width") or 0),
            height=int(fmt.get("height") or 0),
            estimated_bytes=size,
        )
        candidates.append(((plan.height, int(fmt.get("tbr") or 0)), plan))

    if not candidates:
        raise FeedError(f"No usable format for {video_id}")

    picked = choose(
        candidates,
        size=lambda c: c[1].estimated_bytes,
        rank=lambda c: c[0],
        height=lambda c: c[1].height,
        quality=quality,
    )
    if picked is None:
        smallest = min(plan.estimated_bytes for _, plan in candidates)
        raise too_big(
            f"Video {video_id} (even its smallest usable format)",
            smallest,
            quality,
        )
    (_, plan), parts = picked
    return replace(plan, parts=parts)


def _pairing(
    fmt: dict, audio: dict | None, duration: float
) -> tuple[str, int] | None:
    """``(selector, bytes)`` for ``fmt``, or ``None`` if it isn't usable.

    Two shapes qualify: an H.264 video-only stream, which needs the audio
    stream muxed alongside it, and a progressive format that already has both —
    the 360p one YouTube still serves, and the only thing left when a video has
    no separate H.264 rungs at all.
    """
    if not str(fmt.get("vcodec") or "none").startswith(_VIDEO_CODEC):
        return None
    if not fmt.get("height"):
        return None

    size = _size(fmt, duration)
    if size is None:
        return None
    format_id = str(fmt.get("format_id") or "")
    if not format_id:
        return None

    acodec = str(fmt.get("acodec") or "none")
    if acodec == "none":
        if audio is None:
            return None
        audio_size = _size(audio, duration)
        if audio_size is None:
            return None
        return f"{format_id}+{audio['format_id']}", size + audio_size

    if not acodec.startswith(_AUDIO_CODEC):
        return None
    return format_id, size


def _smallest_audio(formats: list[dict], duration: float) -> dict | None:
    """The leanest AAC audio-only stream in the original language.

    Audio is a rounding error next to video here — roughly 2 MB for ten minutes
    against 60 — so the cheapest one buys the most room for picture.

    But only among the original's own renditions. A dubbed video lists every
    language as a separate stream of near-identical size, so size alone picks a
    dub at random. yt-dlp ranks them in ``language_preference``: the original
    highest, then the default track, the dubs below that. A video without dubs
    has one language, and the ranking changes nothing.
    """
    candidates = [
        fmt
        for fmt in formats
        if str(fmt.get("vcodec") or "none") == "none"
        and str(fmt.get("acodec") or "none").startswith(_AUDIO_CODEC)
        and fmt.get("format_id")
        and _size(fmt, duration) is not None
    ]
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda fmt: (-_language_preference(fmt), _size(fmt, duration) or 0),
    )


def _language_preference(fmt: dict) -> int:
    """yt-dlp's rank for ``fmt``'s audio language, lowest when it gives none."""
    try:
        return int(fmt.get("language_preference"))
    except (TypeError, ValueError):
        return -1


def _size(fmt: dict, duration: float) -> int | None:
    """How big ``fmt`` is, best available answer, or ``None`` if there isn't one.

    ``filesize`` is exact and taken as it stands. The other two are estimates
    and carry :data:`~fetchit.media.SIZE_ESTIMATE_MARGIN` accordingly.
    """
    exact = fmt.get("filesize")
    if exact:
        return int(exact)

    approx = fmt.get("filesize_approx")
    if approx:
        return int(int(approx) * SIZE_ESTIMATE_MARGIN)

    tbr = fmt.get("tbr")
    if tbr and duration:
        return int(float(tbr) * 1000 / 8 * duration * SIZE_ESTIMATE_MARGIN)
    return None


def _ytdlp_json(args: list[str], *, timeout: float) -> dict:
    """Run ``yt-dlp -J`` with ``args`` and return what it printed."""
    result = _run_ytdlp(["-J", *args], timeout=timeout)
    try:
        info = json.loads(result.stdout)
    except ValueError as exc:
        raise FeedError(f"Could not read yt-dlp's output: {exc}") from exc
    if not isinstance(info, dict):
        raise FeedError("yt-dlp reported something that isn't a video")
    return info


def _run_download(url: str, selector: str, into: str, *, timeout: float) -> str:
    """Download ``selector`` of ``url`` into ``into``, returning the file's path.

    ``--merge-output-format mp4`` is what turns a separate video and audio
    stream into one file; yt-dlp calls ffmpeg to do it and copies the streams
    rather than re-encoding, so this costs bandwidth and seconds rather than
    minutes of cpu. The extension is left to yt-dlp and the file found
    afterwards, because it has the last word on what it wrote.
    """
    _run_ytdlp(
        [
            "--no-playlist",
            "--no-warnings",
            "--no-progress",
            "--quiet",
            "--format",
            selector,
            "--merge-output-format",
            "mp4",
            "--output",
            os.path.join(into, "video.%(ext)s"),
            url,
        ],
        timeout=timeout,
    )

    files = [
        os.path.join(into, name)
        for name in sorted(os.listdir(into))
        if os.path.isfile(os.path.join(into, name))
    ]
    if not files:
        raise FeedError("yt-dlp produced no video")
    # More than one means the mux didn't happen and the parts were left behind;
    # the biggest is the video, and sending it beats sending nothing.
    return max(files, key=os.path.getsize)


def _run_ytdlp(args: list[str], *, timeout: float) -> subprocess.CompletedProcess:
    binary = shutil.which("yt-dlp")
    if binary is None:
        raise FeedError(
            "yt-dlp is needed to fetch YouTube videos but was not found on PATH "
            "(`pip install fetchit[video]`)"
        )

    try:
        with timing.step("yt-dlp", args[0] if args else ""):
            result = subprocess.run(
                [binary, *_cookie_args(), *args],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
    except subprocess.TimeoutExpired as exc:
        raise FeedError(f"yt-dlp timed out after {timeout:.0f}s") from exc
    except OSError as exc:
        raise FeedError(f"Could not run yt-dlp: {exc}") from exc

    if result.returncode != 0:
        raise FeedError(f"yt-dlp failed: {_complaint(result.stderr)}")
    return result


def _cookie_args() -> list[str]:
    """How yt-dlp should sign in, from the environment, or nothing if it shouldn't.

    A cookies file wins over a browser profile when both are set: it is the
    explicit one, and the one that works where there is no browser to read.
    """
    path = os.environ.get(COOKIES_ENV, "").strip()
    if path:
        expanded = os.path.expanduser(path)
        if not os.path.isfile(expanded):
            raise FeedError(
                f"{COOKIES_ENV} is set to {path!r}, which is not a file. Point it "
                "at a cookies.txt export, or unset it."
            )
        return ["--cookies", expanded]

    browser = os.environ.get(COOKIES_FROM_BROWSER_ENV, "").strip()
    if browser:
        return ["--cookies-from-browser", browser]
    return []


def _complaint(stderr: str) -> str:
    """What went wrong, and for the bot check, what to do about it.

    That one is worth expanding because the fix is configuration rather than
    anything about the video, and yt-dlp's own advice is for someone running
    yt-dlp by hand.
    """
    last = _last_line(stderr)
    if _BOT_CHECK not in last.lower():
        return last
    return (
        f"{last}\nYouTube wants a signed-in session for this one. Set "
        f"{COOKIES_FROM_BROWSER_ENV} to a browser you are logged into "
        f"(e.g. firefox, chrome, safari, or 'firefox:<profile>'), or export a "
        f"cookies.txt from that browser and set {COOKIES_ENV} to its path."
    )


def _last_line(output: str) -> str:
    """The final non-empty line yt-dlp wrote.

    On stderr that is the one that says what went wrong; on stdout it is the
    answer to a ``--print``, below whatever notes preceded it.
    """
    lines = [line for line in (output or "").splitlines() if line.strip()]
    return lines[-1] if lines else "no error output"
