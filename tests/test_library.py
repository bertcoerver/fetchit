"""Tests for fetchit.library: where things are filed, and what is written beside them."""

import json
import os
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import pytest

from fetchit import library
from fetchit.feeds import FeedConfig, FeedItem, ParsedFeed
from fetchit.library import place, prune, records, safe_name, update_artwork, write_feed, write_item
from fetchit.media import AUDIO, TEXT, VIDEO

WHEN = datetime(2026, 10, 19, 8, 30, tzinfo=timezone.utc)
CFG = FeedConfig(url="https://a/feed", name="Le Dessous des Images")
PARSED = ParsedFeed(items=[], title="Le dessous des images", description="Sonia raconte.")


def _item(title="La bataille du drapeau", when=WHEN, **kwargs):
    return FeedItem(title=title, description="Retiré sur ordre.", link="https://a/1", published=when, **kwargs)


def _file(path, data=b"x"):
    with open(path, "wb") as fh:
        fh.write(data)
    return str(path)


def _staged(root, name, data=b"x"):
    """A file in the library's staging folder, as a download would leave it."""
    return _file(os.path.join(library.staging_dir(str(root)), name), data)


def _record(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


# --- names ----------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("A Closer Look: The Week", "A Closer Look - The Week"),
        ("AC/DC live", "AC DC live"),
        ('What "is" it?', "What is it"),
        ("Ends with a dot.", "Ends with a dot"),
        ("Déjà vu", "Déjà vu"),
        ("", "item"),
        ("???", "item"),
    ],
)
def test_safe_name(text, expected):
    assert safe_name(text) == expected


def test_safe_name_shortens_on_a_word():
    name = safe_name("word " * 40, limit=22)
    assert name == "word word word word"


# --- where things go ------------------------------------------------------


def test_a_dated_video_is_numbered_by_its_day(tmp_path):
    placement = place(str(tmp_path), CFG.name, _item(), VIDEO, item_id="a")
    assert placement.folder == str(tmp_path / "shows" / "Le Dessous des Images" / "Season 2026")
    assert placement.stem == "Le Dessous des Images - S2026E101901 - La bataille du drapeau"
    assert (placement.season, placement.episode) == (2026, 101901)


def test_a_second_video_the_same_day_counts_up_and_a_refiled_one_keeps_its_number(tmp_path):
    root = str(tmp_path)
    write_item(root, CFG, PARSED, _item(), VIDEO, item_id="first", media=_staged(root, "a.mp4"))

    second = place(root, CFG.name, _item("Autre", when=WHEN + timedelta(hours=3)), VIDEO, item_id="second")
    again = place(root, CFG.name, _item(), VIDEO, item_id="first")
    assert second.episode == 101902
    assert again.episode == 101901


def test_a_numbered_episode_is_filed_by_its_season(tmp_path):
    item = _item("26.000 sancties", extra={"season": "2", "episode": "28"})
    placement = place(str(tmp_path), "Bureau Buitenland", item, VIDEO, item_id="npo:x")
    assert placement.folder.endswith(os.path.join("shows", "Bureau Buitenland", "Season 02"))
    assert placement.stem == "Bureau Buitenland - S02E28 - 26.000 sancties"


def test_audio_and_text_are_filed_by_date(tmp_path):
    audio = place(str(tmp_path), "Pod", _item("Ep 1"), AUDIO, item_id="a")
    text = place(str(tmp_path), "Blog", _item("Post"), TEXT, item_id="b")
    assert audio.folder == str(tmp_path / "podcasts" / "Pod")
    assert audio.stem == "2026-10-19 - Ep 1"
    assert text.folder == str(tmp_path / "articles" / "Blog")


def test_two_items_sharing_a_name_are_told_apart(tmp_path):
    root = str(tmp_path)
    write_item(root, CFG, PARSED, _item("Ep"), AUDIO, item_id="one", media=_staged(root, "a.mp3"))
    other = place(root, CFG.name, _item("Ep"), AUDIO, item_id="two")
    same = place(root, CFG.name, _item("Ep"), AUDIO, item_id="one")
    assert other.stem == "2026-10-19 - Ep (2)"
    assert same.stem == "2026-10-19 - Ep"


def test_a_very_long_title_still_fits_a_file_name(tmp_path):
    placement = place(str(tmp_path), CFG.name, _item("é" * 300), VIDEO, item_id="a")
    assert len((placement.stem + "-thumb.jpg").encode()) < 255


# --- filing an item ---------------------------------------------------------


def test_write_item_files_a_video_with_its_record_nfo_and_thumb(tmp_path):
    root = str(tmp_path)
    media = _staged(root, "video.mp4", b"video")
    image = _staged(root, "image.jpg", b"jpg")
    item = _item(categories=("Société",), extra={"duration_seconds": "641"})

    path = write_item(root, CFG, PARSED, item, VIDEO, item_id="arte:fr:1", media=media, image=image)

    folder = os.path.dirname(path)
    stem = "Le Dessous des Images - S2026E101901 - La bataille du drapeau"
    assert sorted(os.listdir(folder)) == sorted(
        [stem + ".mp4", stem + "-thumb.jpg", stem + ".nfo", stem + ".json"]
    )
    assert not os.path.exists(media) and not os.path.exists(image)

    record = _record(path)
    assert record["format"] == 1
    assert record["id"] == "arte:fr:1"
    assert record["kind"] == "video"
    assert record["feed"] == CFG.name
    assert record["media"] == stem + ".mp4"
    assert record["image"] == stem + "-thumb.jpg"
    assert record["published"] == "2026-10-19T08:30:00+00:00"
    assert (record["season"], record["episode"]) == (2026, 101901)
    assert record["duration"] == 641
    assert record["fields"]["title"] == "La bataille du drapeau"
    assert "fetched" in record

    nfo = ET.parse(os.path.join(folder, stem + ".nfo")).getroot()
    assert nfo.tag == "episodedetails"
    assert nfo.findtext("showtitle") == "Le dessous des images"
    assert nfo.findtext("season") == "2026"
    assert nfo.findtext("episode") == "101901"
    assert nfo.findtext("aired") == "2026-10-19"
    assert nfo.findtext("runtime") == "11"
    assert nfo.findtext("genre") == "Société"
    assert nfo.findtext("lockdata") == "true"


def test_write_item_files_text_as_a_record_alone(tmp_path):
    path = write_item(str(tmp_path), CFG, PARSED, _item("Post"), TEXT, item_id="t")
    assert os.listdir(os.path.dirname(path)) == [os.path.basename(path)]
    assert "media" not in _record(path)


def test_write_item_keeps_the_episode_number_a_podcast_app_shows(tmp_path):
    root = str(tmp_path)
    item = _item("Un petit air", extra={"number": "288"})
    path = write_item(root, CFG, PARSED, item, AUDIO, item_id="b", media=_staged(root, "a.mp3"))
    assert _record(path)["number"] == 288


def test_write_item_skips_a_picture_of_a_kind_players_may_not_read(tmp_path):
    root = str(tmp_path)
    path = write_item(
        root, CFG, PARSED, _item(), AUDIO, item_id="a",
        media=_staged(root, "a.mp3"), image=_staged(root, "i.webp"),
    )
    assert "image" not in _record(path)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg to make a real m4a")
def test_write_item_tags_audio_for_a_music_library(tmp_path):
    import mutagen

    root = str(tmp_path)
    source = os.path.join(library.staging_dir(root), "a.m4a")
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "anullsrc=r=8000:cl=mono",
         "-t", "1", "-c:a", "aac", source],
        check=True,
    )
    item = _item("Un petit air", author="Bayard", extra={"number": "288"})
    path = write_item(root, CFG, PARSED, item, AUDIO, item_id="a", media=source)

    record = _record(path)
    tags = mutagen.File(os.path.join(os.path.dirname(path), record["media"]), easy=True)
    assert tags["title"] == ["Un petit air"]
    assert tags["album"] == ["Le dessous des images"]
    assert tags["albumartist"] == ["Le dessous des images"]
    assert tags["artist"] == ["Bayard"]
    assert tags["date"] == ["2026-10-19"]
    assert record["duration"] == 1


# --- describing the feed ----------------------------------------------------


def test_write_feed_describes_the_feed_in_each_folder(tmp_path):
    root = str(tmp_path)
    write_feed(root, CFG, PARSED, {VIDEO, TEXT})

    for kind in ("shows", "articles"):
        meta = _record(tmp_path / kind / CFG.name / "feed.json")
        assert meta["name"] == CFG.name
        assert meta["title"] == "Le dessous des images"
        assert meta["description"] == "Sonia raconte."
        assert meta["url"] == "https://a/feed"
    assert _record(tmp_path / "shows" / CFG.name / "feed.json")["image"] == "poster.jpg"
    assert _record(tmp_path / "articles" / CFG.name / "feed.json")["image"] == "cover.jpg"

    show = ET.parse(tmp_path / "shows" / CFG.name / "tvshow.nfo").getroot()
    assert show.findtext("title") == "Le dessous des images"
    assert show.findtext("lockdata") == "true"


def test_write_feed_keeps_what_a_feed_said_before_when_it_says_nothing(tmp_path):
    root = str(tmp_path)
    write_feed(root, CFG, PARSED, {AUDIO})
    write_feed(root, CFG, ParsedFeed(items=[]), {AUDIO})  # a "nothing new" answer
    meta = _record(tmp_path / "podcasts" / CFG.name / "feed.json")
    assert meta["title"] == "Le dessous des images"
    assert meta["description"] == "Sonia raconte."


def test_write_feed_takes_the_configured_language_over_the_feeds(tmp_path):
    cfg = FeedConfig(url="https://a", name="Pod", language="fr")
    write_feed(str(tmp_path), cfg, ParsedFeed(items=[], language="en"), {AUDIO})
    assert _record(tmp_path / "podcasts" / "Pod" / "feed.json")["language"] == "fr"


# --- the feed's artwork -----------------------------------------------------


def test_update_artwork_writes_whatever_is_missing(tmp_path):
    root = str(tmp_path)
    image = _file(tmp_path / "new.jpg", b"v1")
    written = update_artwork(root, CFG, {VIDEO}, image, {})
    folder = tmp_path / "shows" / CFG.name
    assert (folder / "poster.jpg").read_bytes() == b"v1"
    assert (folder / "fanart.jpg").read_bytes() == b"v1"
    assert set(written) == {
        os.path.join("shows", CFG.name, "poster.jpg"),
        os.path.join("shows", CFG.name, "fanart.jpg"),
    }


def test_update_artwork_follows_the_source_but_not_over_a_picture_somebody_chose(tmp_path):
    root = str(tmp_path)
    written = update_artwork(root, CFG, {VIDEO}, _file(tmp_path / "v1.jpg", b"v1"), {})
    folder = tmp_path / "shows" / CFG.name
    (folder / "poster.jpg").write_bytes(b"a tall poster, by hand")

    update_artwork(root, CFG, {VIDEO}, _file(tmp_path / "v2.jpg", b"v2"), written)
    assert (folder / "poster.jpg").read_bytes() == b"a tall poster, by hand"
    assert (folder / "fanart.jpg").read_bytes() == b"v2"


def test_update_artwork_leaves_a_feed_that_says_not_to_alone(tmp_path):
    root = str(tmp_path)
    cfg = FeedConfig(url="https://a", name="Cartes", refresh_image=False)
    written = update_artwork(root, cfg, {AUDIO}, _file(tmp_path / "v1.jpg", b"v1"), {})
    update_artwork(root, cfg, {AUDIO}, _file(tmp_path / "v2.jpg", b"v2"), written)
    assert (tmp_path / "podcasts" / "Cartes" / "cover.jpg").read_bytes() == b"v1"


# --- reading and pruning ----------------------------------------------------


def _filed(root, title, when, kind=VIDEO, fetched_days_ago=0):
    media = _staged(root, f"{title}.mp4") if kind != TEXT else None
    path = write_item(root, CFG, PARSED, _item(title, when=when), kind, item_id=title, media=media)
    if fetched_days_ago:
        record = _record(path)
        stamp = datetime.now(timezone.utc) - timedelta(days=fetched_days_ago)
        record["fetched"] = stamp.isoformat()
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(record, fh)
    return path


def test_records_are_every_item_oldest_first(tmp_path):
    root = str(tmp_path)
    _filed(root, "later", WHEN)
    _filed(root, "earlier", WHEN - timedelta(days=400))
    _filed(root, "note", WHEN - timedelta(days=1), kind=TEXT)
    write_feed(root, CFG, PARSED, {VIDEO})
    assert [r["title"] for r in records(root, CFG.name)] == ["earlier", "note", "later"]


def test_prune_removes_what_has_outlived_its_feeds_days_and_nothing_else(tmp_path):
    root = str(tmp_path)
    write_feed(root, CFG, PARSED, {VIDEO})
    old = _filed(root, "old", WHEN - timedelta(days=400), fetched_days_ago=20)
    new = _filed(root, "new", WHEN, fetched_days_ago=2)

    assert prune(root, {CFG.name: 14}) == 1

    assert not os.path.exists(old)
    assert not any(n.startswith(os.path.basename(old)[:-5]) for n in os.listdir(os.path.dirname(new)))
    assert os.path.exists(new)
    # The season the old one was in is gone; the feed's own files stay.
    assert not os.path.exists(os.path.dirname(old))
    assert os.path.exists(tmp_path / "shows" / CFG.name / "feed.json")


def test_prune_keeps_everything_for_a_feed_with_no_limit(tmp_path):
    root = str(tmp_path)
    path = _filed(root, "old", WHEN, fetched_days_ago=1000)
    assert prune(root, {CFG.name: 0}, default=14) == 0
    assert os.path.exists(path)


def test_prune_uses_the_default_for_a_feed_no_longer_configured(tmp_path):
    root = str(tmp_path)
    path = _filed(root, "old", WHEN, fetched_days_ago=30)
    assert prune(root, {}, default=14) == 1
    assert not os.path.exists(path)


def test_prune_clears_what_a_killed_run_left_in_staging(tmp_path):
    root = str(tmp_path)
    leftover = _staged(root, "half.mp4.part")
    fresh = _staged(root, "now.mp4.part")
    old = time.time() - 2 * 86400
    os.utime(leftover, (old, old))
    prune(root, {})
    assert not os.path.exists(leftover)
    assert os.path.exists(fresh)
