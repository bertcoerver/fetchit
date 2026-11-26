"""The library: where downloads are filed, and the record kept beside each one.

The layout is the contract between fetchit and whatever reads it — rssignal,
db-podcast, a media server — so it is written down here once::

    <library>/
        shows/<Feed>/                       video: a Jellyfin/Emby "Shows" library
            feed.json                       what the feed says about itself
            tvshow.nfo  poster.jpg  fanart.jpg
            Season 2026/
                <Feed> - S2026E101901 - <Title>.mp4
                <Feed> - S2026E101901 - <Title>.nfo
                <Feed> - S2026E101901 - <Title>-thumb.jpg
                <Feed> - S2026E101901 - <Title>.json      the item's record
        podcasts/<Feed>/                    audio: a Jellyfin "Music" library
            feed.json  cover.jpg
            2026-10-19 - <Title>.mp3        tagged: album, title, artist, date
            2026-10-19 - <Title>.jpg
            2026-10-19 - <Title>.json
        articles/<Feed>/                    text: items with nothing to play
            feed.json  cover.jpg
            2026-10-19 - <Title>.json
            2026-10-19 - <Title>-1.jpg      a post's photos, where it has a body
        small/<Feed>/Season 2026/           the smaller copies, where a feed has them
            <Feed> - S2026E101901 - <Title>.mp4
        .fetchit/                           fetchit's own: state, lock, staging

**An item exists when its record does.** Everything an item consists of is
written to a staging folder on the same disk and moved into place, and the
``.json`` record is moved in last. A reader that lists records therefore never
sees half an item, however its run and fetchit's overlap. Pruning goes the
other way: the record is removed first.

**The record** holds everything the source said about the item, so a reader
needs nothing else: ``title``, ``description``, ``published`` (ISO 8601),
``link``, ``kind`` (``video``, ``audio`` or ``text``), ``media`` and ``image``
(file names beside the record), ``season``/``episode`` where the item has a
place in a series, ``number`` where the source numbers its episodes,
``duration`` in seconds, and ``fields`` — every field the source gave, as the
strings a filter sees. ``format`` says which version of this layout it is.

**A video may come in two sizes.** A feed with ``quality.small`` set (see
:mod:`fetchit.quality`) downloads a second, smaller rendition of each video —
the one that fits a chat app's attachment limit, beside the one worth watching
on a television. A media server shows two files in a season's folder as two
episodes, so the small one is filed outside ``shows/`` altogether, under
``small/``, in the same folders and by the same name. The record names it:
``small`` is its path from the library's root, with forward slashes. It is a
copy and nothing more — no record, picture or ``.nfo`` of its own — and it
comes and goes with its item. A reader that wants the smaller file takes
``small`` where a record has it and the file is there, and ``media`` otherwise;
one that knows nothing of it reads the library as before.

**A post with pictures in it has a body.** ``body`` is the item as it was
written, in order: a list of ``{"type": "heading" | "text", "text": …}`` and
``{"type": "image" | "video" | "audio", "file": …}``, each ``file`` a name
beside the record. ``description`` still holds all of its text, and ``image``
names its first picture, so a reader that knows nothing of bodies has a title,
a text and a picture to show. Only a source that says so writes one — today
that is a blog on e-Primo.

**A video is filed as an episode.** Jellyfin and Emby build a series out of
``Season NN`` folders and ``SnnEnn`` names, with the ``.nfo`` files saying the
rest. A source that says which episode of which season an item is (NPO does)
gets those numbers; everything else is numbered by date, the season being the
year and the episode the month, day and a counter — ``S2026E101901`` is the
first of 19 October. The ``.nfo`` files are marked locked, so the server takes
them as they are rather than searching online for a series of that name: a
daily broadcast is in no database, and the nearest match is someone else's
programme.

**Audio is filed as an album.** A Jellyfin music library reads the tags, so
each file is tagged with the feed as album and artist, the episode as title,
and its date; ``cover.jpg`` is the album's artwork. The same folder is what
db-podcast publishes as a podcast.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone

from .errorlog import log_line
from .feeds import FeedConfig, FeedItem, ParsedFeed, as_number, item_fields
from .media import AUDIO, TEXT, VIDEO

# Bumped when a reader written against the layout above would misread a
# library written by a newer fetchit.
FORMAT = 1

KIND_DIRS = {VIDEO: "shows", AUDIO: "podcasts", TEXT: "articles"}

# Where the second, smaller copy of a video goes: beside the kinds' folders,
# not in them, so that nothing reading a kind's folder for media comes across it.
SMALL_DIR = "small"

STATE_DIR = ".fetchit"
STAGING_DIR = os.path.join(STATE_DIR, "staging")

FEED_FILE = "feed.json"
RECORD_EXT = ".json"
NFO_EXT = ".nfo"
SHOW_NFO = "tvshow.nfo"
THUMB_SUFFIX = "-thumb"

# The pictures a feed's folder is shown by: a series by its poster and the
# backdrop behind its page, an album or a feed of articles by its cover. The
# suffix is the usual one; a picture that is a PNG is written as .png (see
# :func:`image_suffix`).
FEED_IMAGES = {VIDEO: ("poster.jpg", "fanart.jpg"), AUDIO: ("cover.jpg",), TEXT: ("cover.jpg",)}

IMAGE_EXTS = (".jpg", ".jpeg", ".png")


def image_suffix(path: str) -> str | None:
    """The suffix that fits what is in the picture ``path``: ``.jpg`` or ``.png``.

    By its first bytes, not its name: a source's URL may well say .jpg of a
    PNG, and whatever shows the file by its name — a web server, Jellyfin, a
    podcast app — goes by the name. ``None`` for anything else.
    """
    try:
        with open(path, "rb") as file:
            head = file.read(8)
    except OSError:
        return None
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if head.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    return None


def feed_artwork(folder: str, name: str) -> str | None:
    """The file in ``folder`` that is the feed picture ``name``, whatever its suffix."""
    stem = os.path.splitext(name)[0]
    for ext in IMAGE_EXTS:
        path = os.path.join(folder, stem + ext)
        if os.path.isfile(path):
            return path
    return None


def misnamed(path: str) -> bool:
    """Whether the picture ``path`` is named for another format than it is."""
    actual = image_suffix(path)
    named = os.path.splitext(path)[1].lower()
    return actual is not None and actual != {".jpeg": ".jpg"}.get(named, named)

# Long enough to tell two episodes of the same show apart, short enough that a
# feed name, an episode number and a title stay under the 255 bytes most
# filesystems allow a name, accents and all.
TITLE_MAX = 80
FEED_MAX = 60
NAME_MAX_BYTES = 200

# Half-finished downloads left by a run that was killed. Nothing else will
# ever come looking for them.
STAGING_MAX_AGE = 24 * 60 * 60

# XML 1.0 has no way to write these at all, escaped or not, and a synopsis
# pasted from somewhere odd occasionally carries one. One of them makes the
# whole file unreadable.
_XML_UNSAFE_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

# What a file name can't hold on one filesystem or another, or holds badly.
_NAME_UNSAFE_RE = re.compile(r'[\x00-\x1f<>"/\\|?*]+')


@dataclass(frozen=True)
class Placement:
    """Where an item goes: its folder, the stem its files share, its numbers."""

    folder: str
    stem: str
    season: int | None = None
    episode: int | None = None

    def path(self, suffix: str) -> str:
        return os.path.join(self.folder, self.stem + suffix)


def safe_name(text: str, *, limit: int = TITLE_MAX) -> str:
    """``text`` as something every filesystem and media server will take as a name.

    Kept readable — accents, spaces and capitals stay, because these names are
    what a media browser shows — and only what a filesystem refuses, or a
    server misreads, is changed: path separators and the characters Windows
    and Samba forbid go, a colon becomes a dash, and a name may not end in a
    dot or a space.
    """
    text = unicodedata.normalize("NFC", text or "")
    text = text.replace(": ", " - ").replace(":", "-")
    text = _NAME_UNSAFE_RE.sub(" ", text)
    text = " ".join(text.split())
    if len(text) > limit:
        cut = text[:limit]
        # On a word, where there is one in the second half.
        space = cut.rfind(" ")
        text = cut[:space] if space > limit // 2 else cut
    text = text.strip(" .-")
    return text or "item"


def feed_folder(root: str, feed: str, kind: str) -> str:
    """The folder ``feed``'s items of ``kind`` are filed in."""
    return os.path.join(root, KIND_DIRS[kind], safe_name(feed, limit=FEED_MAX))


def feed_folders(root: str, feed: str) -> list[tuple[str, str]]:
    """``(kind, folder)`` for each folder ``feed`` has in the library so far."""
    found = []
    for kind in KIND_DIRS:
        folder = feed_folder(root, feed, kind)
        if os.path.isdir(folder):
            found.append((kind, folder))
    return found


def staging_dir(root: str) -> str:
    """Where downloads land first: inside the library, so moving them is a rename."""
    path = os.path.join(root, STAGING_DIR)
    os.makedirs(path, exist_ok=True)
    return path


def place(root: str, feed: str, item: FeedItem, kind: str, *, item_id: str) -> Placement:
    """Work out where ``item`` would be filed — see the module docstring.

    An item filed before (the same ``item_id``, downloaded again) gets its old
    place back, so it replaces itself rather than appearing twice. Two
    different items that would share a name are told apart with ``(2)``.
    """
    folder = feed_folder(root, feed, kind)
    title = safe_name(item.title)
    when = item.published or datetime.now(timezone.utc)

    if kind == VIDEO:
        show = safe_name(feed, limit=FEED_MAX)
        numbered = numbers(item)
        if numbered is not None:
            season, episode = numbered
        else:
            season = when.year
            episode = _next_in_day(
                os.path.join(folder, _season_dir(season)), when, item_id
            )
        folder = os.path.join(folder, _season_dir(season))
        stem = _fit(f"{show} - S{season:02d}E{episode:02d} - ", title)
        placement = Placement(folder, stem, season, episode)
    else:
        stem = _fit(f"{when:%Y-%m-%d} - ", title)
        placement = Placement(folder, stem)

    return _unclaimed(placement, item_id)


def numbers(item: FeedItem) -> tuple[int, int] | None:
    """``(season, episode)`` if ``item`` says which episode of a series it is.

    A source that knows puts both in the item's ``extra``, as ``season`` and
    ``episode``; NPO does. Anything else — a podcast, an upload, a film — has no
    such place, and is numbered by its date instead.
    """
    try:
        season, episode = int(item.extra["season"]), int(item.extra["episode"])
    except (KeyError, ValueError):
        return None
    return (season, episode) if season >= 0 and episode >= 0 else None


def _season_dir(season: int) -> str:
    return f"Season {season:02d}"


def _fit(prefix: str, title: str) -> str:
    """``prefix + title``, with the title shortened until the name fits in bytes."""
    name = prefix + title
    while len(name.encode("utf-8")) > NAME_MAX_BYTES and title:
        title = title[:-1].rstrip(" .-")
        name = prefix + title
    return name.rstrip(" -") if not title else name


def _next_in_day(season_folder: str, when: datetime, item_id: str) -> int:
    """The episode number for the next item of ``when``'s day: ``MMDD`` and a counter.

    The counter is one more than the highest already filed for that day, so an
    upload that came out in the evening after one that came out in the morning
    sorts after it. An item already filed keeps the number it has.
    """
    day = when.month * 100 + when.day
    used = []
    for record in _records_in(season_folder):
        episode = record.get("episode")
        if not isinstance(episode, int) or episode // 100 != day:
            continue
        if record.get("id") == item_id:
            return episode
        used.append(episode % 100)
    counter = max(used, default=0) + 1
    return day * 100 + min(counter, 99)


def _unclaimed(placement: Placement, item_id: str) -> Placement:
    """``placement``, or the first ``(2)``-style variant no other item holds."""
    candidate = placement
    for n in range(2, 100):
        record = _read_json(candidate.path(RECORD_EXT))
        if record is None or record.get("id") == item_id:
            return candidate
        candidate = Placement(
            placement.folder, f"{placement.stem} ({n})", placement.season, placement.episode
        )
    return candidate


def write_item(
    root: str,
    cfg: FeedConfig,
    parsed: ParsedFeed,
    item: FeedItem,
    kind: str,
    *,
    item_id: str,
    media: str | None = None,
    small: str | None = None,
    image: str | None = None,
    body: list[dict] | None = None,
) -> str:
    """File ``item`` in the library and return the path of its record.

    ``media`` and ``image`` are local files — the download and the item's own
    picture — and are *moved* into the library, so they should be in its
    staging folder (see :func:`staging_dir`) for that to be a rename. Either may
    be missing: a text item has no media, and plenty of items have no picture.
    ``small`` is the video's second, smaller copy, where it has one; it is
    filed under ``small/`` and named in the record.

    ``body`` is the item's body with its files downloaded (each file block a
    ``path``); they are moved in the same way, as ``<stem>-1.jpg`` and so on.
    Without an ``image`` of its own, the item's picture is the body's first.

    Everything is assembled in staging and moved into place with the record
    last; see the module docstring for why.
    """
    placement = place(root, cfg.name, item, kind, item_id=item_id)
    os.makedirs(placement.folder, exist_ok=True)

    with tempfile.TemporaryDirectory(dir=staging_dir(root), prefix="item-") as work:
        moves: list[tuple[str, str]] = []
        files: dict[str, str] = {}
        staged_media = None

        if media:
            ext = os.path.splitext(media)[1].lower() or ".bin"
            staged_media = os.path.join(work, "media" + ext)
            shutil.move(media, staged_media)
            if kind == AUDIO:
                tag_audio(staged_media, item, feed_title=parsed.title or cfg.name)
            moves.append((staged_media, placement.path(ext)))
            files["media"] = placement.stem + ext

        if small and media:
            ext = os.path.splitext(small)[1].lower() or ".bin"
            staged = os.path.join(work, "small" + ext)
            shutil.move(small, staged)
            target = small_path(root, placement.path(ext))
            os.makedirs(os.path.dirname(target), exist_ok=True)
            moves.append((staged, target))
            files["small"] = os.path.relpath(target, root).replace(os.sep, "/")
        else:
            # Filed again without one: the copy it had before is not its copy now.
            _unlink_small(root, _read_json(placement.path(RECORD_EXT)))

        if image:
            ext = os.path.splitext(image)[1].lower()
            if ext in IMAGE_EXTS:
                suffix = (THUMB_SUFFIX if kind == VIDEO else "") + ext
                staged = os.path.join(work, "image" + ext)
                shutil.move(image, staged)
                moves.append((staged, placement.path(suffix)))
                files["image"] = placement.stem + suffix

        blocks: list[dict] = []
        for block in body or ():
            source = block.get("path")
            if not source:
                blocks.append({"type": block["type"], "text": block.get("text", "")})
                continue
            number = sum(1 for b in blocks if "file" in b) + 1
            ext = os.path.splitext(source)[1].lower() or ".bin"
            suffix = f"-{number}{ext}"
            staged = os.path.join(work, f"body{suffix}")
            shutil.move(source, staged)
            moves.append((staged, placement.path(suffix)))
            blocks.append({"type": block["type"], "file": placement.stem + suffix})
            if "image" not in files and block["type"] == "image" and ext in IMAGE_EXTS:
                files["image"] = placement.stem + suffix

        duration = _duration(item, staged_media)
        record = _record(cfg, item, kind, item_id, placement, duration, **files)
        if blocks:
            record["body"] = blocks

        if kind == VIDEO:
            staged = os.path.join(work, "episode.nfo")
            _write_xml(_episode_nfo(item, parsed, cfg, placement, duration), staged)
            moves.append((staged, placement.path(NFO_EXT)))

        staged = os.path.join(work, "record.json")
        _write_json(staged, record)
        moves.append((staged, placement.path(RECORD_EXT)))

        for source, target in moves:
            os.replace(source, target)

    return placement.path(RECORD_EXT)


def small_path(root: str, path: str) -> str:
    """Where the smaller copy of the library file ``path`` goes: its place under ``small/``."""
    kind_dir, _, rest = os.path.relpath(path, root).partition(os.sep)
    return os.path.join(root, SMALL_DIR, rest)


def _small_file(root: str, record: dict | None) -> str | None:
    """The path of the smaller copy ``record`` names, if it names one inside ``small/``."""
    named = record.get("small") if record else None
    if not isinstance(named, str) or not named:
        return None
    base = os.path.realpath(os.path.join(root, SMALL_DIR))
    path = os.path.realpath(os.path.join(root, *named.split("/")))
    return path if path.startswith(base + os.sep) else None


def _unlink_small(root: str, record: dict | None) -> None:
    """Delete the smaller copy ``record`` names, and the folders that leaves empty."""
    path = _small_file(root, record)
    if path is None:
        return
    try:
        os.unlink(path)
    except OSError:
        return
    base = os.path.realpath(os.path.join(root, SMALL_DIR))
    folder = os.path.dirname(path)
    while folder != base and folder.startswith(base + os.sep):
        try:
            os.rmdir(folder)
        except OSError:
            break
        folder = os.path.dirname(folder)


def _record(
    cfg: FeedConfig,
    item: FeedItem,
    kind: str,
    item_id: str,
    placement: Placement,
    duration: int | None,
    **files: str,
) -> dict:
    """The ``.json`` record of an item — see the module docstring."""
    record: dict = {
        "format": FORMAT,
        "id": item_id,
        "feed": cfg.name,
        "kind": kind,
        "title": item.title,
        "description": item.description,
    }
    if item.published is not None:
        record["published"] = item.published.isoformat(timespec="seconds")
    if item.link:
        record["link"] = item.link
    record.update(files)
    if placement.season is not None:
        record["season"] = placement.season
        record["episode"] = placement.episode
    number = _number(item)
    if number is not None:
        record["number"] = number
    if duration is not None:
        record["duration"] = duration
    if item.author:
        record["author"] = item.author
    if item.categories:
        record["categories"] = list(item.categories)
    record["fetched"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    record["fields"] = item_fields(item)
    return record


def _number(item: FeedItem) -> int | None:
    """The episode number a podcast app would show, where the source has one."""
    for key in ("number", "episode", "itunes_episode"):
        try:
            value = int(item.extra[key])
        except (KeyError, ValueError):
            continue
        if value >= 0:
            return value
    return None


def _duration(item: FeedItem, media: str | None) -> int | None:
    """How long the item is in seconds: what the source said, or what the file says."""
    for key in ("duration_seconds", "itunes_duration"):
        seconds = as_number(item.extra.get(key, ""))
        if seconds:
            return int(seconds)
    if not media:
        return None
    try:
        import mutagen

        info = mutagen.File(media)
    except Exception:  # mutagen raises a zoo of its own for files it can't read
        return None
    length = getattr(getattr(info, "info", None), "length", None)
    return int(length) if length else None


def tag_audio(path: str, item: FeedItem, *, feed_title: str) -> None:
    """Tag an audio file so a music library files it under its feed.

    Album and album artist are the feed, so every episode lands in one album
    however its own artist tag reads; title and date are the episode's. A file
    whose format mutagen can't tag is left as it is — it plays the same, it
    just sorts by its file name.
    """
    try:
        import mutagen

        audio = mutagen.File(path, easy=True)
        if audio is None:
            return
        if audio.tags is None:
            audio.add_tags()
        tags = {
            "title": item.title,
            "album": feed_title,
            "albumartist": feed_title,
            "artist": item.author or feed_title,
            "genre": "Podcast",
        }
        if item.published is not None:
            tags["date"] = item.published.strftime("%Y-%m-%d")
        number = _number(item)
        if number is not None:
            tags["tracknumber"] = str(number)
        for key, value in tags.items():
            try:
                audio[key] = value
            except (KeyError, ValueError):
                pass  # a key this format has no easy name for
        audio.save()
    except Exception as exc:
        log_line(f"library: could not tag {os.path.basename(path)}: {exc}")


def _episode_nfo(
    item: FeedItem, parsed: ParsedFeed, cfg: FeedConfig, placement: Placement, duration: int | None
) -> ET.Element:
    """The Kodi ``episodedetails`` a media server reads for a video."""
    root = ET.Element("episodedetails")
    _child(root, "title", item.title)
    _child(root, "showtitle", item.extra.get("show_title") or parsed.title or cfg.name)
    _child(root, "season", str(placement.season))
    _child(root, "episode", str(placement.episode))
    _child(root, "plot", item.description)
    if item.published is not None:
        _child(root, "aired", item.published.strftime("%Y-%m-%d"))
    if duration:
        _child(root, "runtime", str(max(1, round(duration / 60))))
    for genre in item.categories:
        _child(root, "genre", genre)
    _child(root, "studio", item.author or "")
    _locked(root)
    return root


def write_feed(root: str, cfg: FeedConfig, parsed: ParsedFeed, kinds: set[str]) -> None:
    """Describe the feed in each of its folders of ``kinds``: ``feed.json``, and a
    ``tvshow.nfo`` for a series.

    What a feed says about itself only arrives when it has been read in full —
    a feed that answered "nothing new" says nothing — so a field the source
    left out this time keeps what it said before rather than being blanked.
    Files are only rewritten when they would change, so a media server is not
    told a series changed every run.
    """
    for kind in sorted(kinds):
        folder = feed_folder(root, cfg.name, kind)
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, FEED_FILE)
        old = _read_json(path) or {}
        meta = {
            "format": FORMAT,
            "name": cfg.name,
            "title": parsed.title or old.get("title") or cfg.name,
            "description": parsed.description or old.get("description") or "",
            "url": cfg.url,
            "link": parsed.link or old.get("link") or "",
            "author": parsed.author or old.get("author") or "",
            "language": cfg.language or parsed.language or old.get("language") or "",
            "explicit": bool(old.get("explicit", False)),
            "image": os.path.basename(
                feed_artwork(folder, FEED_IMAGES[kind][0]) or FEED_IMAGES[kind][0]
            ),
        }
        if meta != old:
            _write_json_atomic(path, meta)

        if kind == VIDEO:
            show = ET.Element("tvshow")
            _child(show, "title", meta["title"])
            _child(show, "plot", meta["description"])
            _child(show, "studio", meta["author"])
            _locked(show)
            nfo = os.path.join(folder, SHOW_NFO)
            data = _xml_bytes(show)
            if _read_bytes(nfo) != data:
                _write_bytes_atomic(nfo, data)


def update_artwork(
    root: str, cfg: FeedConfig, kinds: set[str], image: str, last_written: dict[str, str]
) -> dict[str, str]:
    """Put the local picture ``image`` up as the feed's artwork, where it should go.

    ``last_written`` maps each artwork file (relative to the library) to the
    hash of what fetchit last put there, and the updated mapping is returned.
    A file that no longer holds what fetchit wrote was replaced by somebody —
    a better poster, a tall one — and is left alone. One that is missing is
    always written; one that holds an older picture is brought up to date
    unless the feed says ``"refresh_image": false``.

    The file is named for what ``image`` is: ``cover.png`` for a PNG, and a
    ``cover.jpg`` fetchit wrote before is then taken away. One somebody put
    there, under either name, still stands.
    """
    digest = _hash_file(image)
    suffix = image_suffix(image) or ".jpg"
    written = dict(last_written)
    for kind in sorted(kinds):
        folder = feed_folder(root, cfg.name, kind)
        for name in FEED_IMAGES[kind]:
            stem = os.path.splitext(name)[0]
            path = os.path.join(folder, stem + suffix)
            key = os.path.relpath(path, root)
            existing = {}
            for ext in IMAGE_EXTS:
                other = os.path.join(folder, stem + ext)
                if os.path.isfile(other):
                    existing[other] = _hash_file(other)
            ours = {
                other: h for other, h in existing.items()
                if h == digest or h == written.get(os.path.relpath(other, root))
            }
            if len(ours) < len(existing):
                continue  # somebody chose this picture
            if existing and not cfg.refresh_image:
                # Keep the picture, but under the name of what it is.
                for other, h in ours.items():
                    if misnamed(other):
                        renamed = os.path.join(folder, stem + image_suffix(other))
                        written.pop(os.path.relpath(other, root), None)
                        os.replace(other, renamed)
                        written[os.path.relpath(renamed, root)] = h
                continue
            if existing.get(path) != digest:
                os.makedirs(folder, exist_ok=True)
                _copy_atomic(image, path)
            written[key] = digest
            for other in ours:
                if other != path:
                    os.remove(other)
                    written.pop(os.path.relpath(other, root), None)
    return written


def records(root: str, feed: str) -> list[dict]:
    """Every item ``feed`` has in the library, oldest first, with ``_path`` set.

    ``_path`` is the record's own path, for the caller that wants the files
    beside it. Records that can't be read are skipped.
    """
    found = []
    for _kind, folder in feed_folders(root, feed):
        for record in _walk_records(folder):
            found.append(record)
    found.sort(key=lambda r: (str(r.get("published") or ""), r["_path"]))
    return found


def _walk_records(folder: str):
    for dirpath, dirnames, filenames in os.walk(folder):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for name in sorted(filenames):
            if name.endswith(RECORD_EXT) and name != FEED_FILE and not name.startswith("."):
                path = os.path.join(dirpath, name)
                record = _read_json(path)
                if record is not None and "id" in record:
                    record["_path"] = path
                    yield record


def _records_in(folder: str) -> list[dict]:
    """The records directly in ``folder``, not below it."""
    out = []
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return out
    for name in names:
        if name.endswith(RECORD_EXT) and name != FEED_FILE:
            record = _read_json(os.path.join(folder, name))
            if record is not None:
                out.append(record)
    return out


def prune(root: str, keep_days: dict[str, int], *, default: int = 0, now: float | None = None) -> int:
    """Delete items older than their feed's ``keep_days``. Returns how many went.

    ``keep_days`` maps a feed folder's name to its days; a folder not in it —
    a feed no longer in the config — gets ``default``. ``0`` keeps everything.
    Age is counted from when the item was *fetched*, not when it was
    published: a backfill fetches a back catalogue years after it went up, and
    a fortnight that had already expired before the episode arrived would be a
    strange kind of library.

    The record goes first, so a reader stops seeing the item before its files
    go; then its media and smaller copy, picture and ``.nfo``; then any season folder left
    empty. A feed's own files — ``feed.json``, its artwork — stay. Leftovers in
    staging from a run that was killed are cleared too. Best-effort
    throughout: a file that can't be deleted is skipped.
    """
    now = time.time() if now is None else now
    removed = 0
    for kind_dir in KIND_DIRS.values():
        base = os.path.join(root, kind_dir)
        try:
            folders = sorted(os.listdir(base))
        except OSError:
            continue
        for name in folders:
            folder = os.path.join(base, name)
            days = keep_days.get(name, default)
            if days <= 0 or not os.path.isdir(folder):
                continue
            cutoff = now - days * 86400
            for record in list(_walk_records(folder)):
                if _fetched(record, record["_path"]) >= cutoff:
                    continue
                if _remove_item(record, root):
                    removed += 1
            _drop_empty_seasons(folder)
    _clear_staging(root, now)
    return removed


def remove_items(root: str, feed: str, ids: set[str]) -> list[dict]:
    """Delete ``feed``'s items whose ``id`` is in ``ids``, as pruning does.

    Record first, then media, its smaller copy, picture and ``.nfo``, then any season folder
    left empty. Returns the records of the items that went.
    """
    removed = [
        r for r in records(root, feed) if r.get("id") in ids and _remove_item(r, root)
    ]
    if removed:
        for _kind, folder in feed_folders(root, feed):
            _drop_empty_seasons(folder)
    return removed


def _fetched(record: dict, path: str) -> float:
    try:
        return datetime.fromisoformat(record["fetched"]).timestamp()
    except (KeyError, TypeError, ValueError):
        try:
            return os.path.getmtime(path)
        except OSError:
            return time.time()


def _remove_item(record: dict, root: str) -> bool:
    path = record["_path"]
    folder = os.path.dirname(path)
    stem = os.path.basename(path)[: -len(RECORD_EXT)]
    try:
        os.unlink(path)
    except OSError:
        return False
    names = [record.get("media"), record.get("image"), stem + NFO_EXT]
    body = record.get("body")
    names += [b.get("file") for b in body if isinstance(b, dict)] if isinstance(body, list) else []
    for name in filter(None, names):
        try:
            os.unlink(os.path.join(folder, os.path.basename(str(name))))
        except OSError:
            pass
    _unlink_small(root, record)
    return True


def _drop_empty_seasons(folder: str) -> None:
    try:
        names = os.listdir(folder)
    except OSError:
        return
    for name in names:
        path = os.path.join(folder, name)
        if name.startswith("Season ") and os.path.isdir(path):
            try:
                os.rmdir(path)
            except OSError:
                pass


def _clear_staging(root: str, now: float) -> None:
    staging = os.path.join(root, STAGING_DIR)
    try:
        names = os.listdir(staging)
    except OSError:
        return
    for name in names:
        path = os.path.join(staging, name)
        try:
            if now - os.path.getmtime(path) < STAGING_MAX_AGE:
                continue
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
            else:
                os.unlink(path)
        except OSError:
            pass


def _child(parent: ET.Element, tag: str, text: str) -> None:
    """Add ``<tag>text</tag>`` to ``parent`` — unless there is nothing to say."""
    text = _XML_UNSAFE_RE.sub("", text or "").strip()
    if text and text != "None":
        ET.SubElement(parent, tag).text = text


def _locked(root: ET.Element) -> None:
    """Tell a media server this file is the whole story, and not to go looking."""
    ET.SubElement(root, "lockdata").text = "true"


def _xml_bytes(root: ET.Element) -> bytes:
    ET.indent(root, space="    ")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True) + b"\n"


def _write_xml(root: ET.Element, path: str) -> None:
    with open(path, "wb") as fh:
        fh.write(_xml_bytes(root))


def _read_json(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _read_bytes(path: str) -> bytes | None:
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError:
        return None


def _write_json(path: str, data: dict) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


def _write_json_atomic(path: str, data: dict) -> None:
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    _write_bytes_atomic(path, text.encode("utf-8"))


def _write_bytes_atomic(path: str, data: bytes) -> None:
    folder = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _copy_atomic(source: str, path: str) -> None:
    with open(source, "rb") as fh:
        _write_bytes_atomic(path, fh.read())


def _hash_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def warn(text: str) -> None:
    """One line on stderr and in the log, for something that cost a nicety."""
    print(text, file=sys.stderr)
    log_line(text)
