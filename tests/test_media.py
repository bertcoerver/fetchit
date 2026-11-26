"""Tests for fetchit.media, the front door (the sources themselves are faked).

What matters here is only the dispatch: what kind an item is, which module it
is handed to, and what it is called. What each source then does is
tests/test_arte.py, tests/test_youtube.py and the rest.
"""

import os
from datetime import datetime, timezone

import pytest

from fetchit import arte, download, media, npo, youtube
from fetchit.feeds import FeedConfig, FeedError, FeedItem
from fetchit.media import (
    AUDIO,
    TEXT,
    VIDEO,
    EnclosurePlan,
    channel_feed_url,
    collection_feed,
    is_video_item,
    item_id,
    kind_of,
    resolve,
)
from fetchit.quality import Quality

ARTE_LINK = "https://www.arte.tv/fr/videos/127395-052-A/le-dessous-des-images/"
YOUTUBE_LINK = "https://www.youtube.com/watch?v=BFcjfKZ0BeI"
NPO_LINK = "https://npo.nl/start/afspelen/bureau-buitenland_281"


def _item(link=ARTE_LINK, **kwargs):
    return FeedItem(title="t", description="d", link=link, **kwargs)


# --- what an item is --------------------------------------------------------


@pytest.mark.parametrize(
    "link, expected",
    [
        (ARTE_LINK, True),
        (YOUTUBE_LINK, True),
        ("https://youtu.be/BFcjfKZ0BeI", True),
        ("https://waitbutwhy.com/2026/07/post.html", False),
        (None, False),
    ],
)
def test_is_video_item(link, expected):
    assert is_video_item(_item(link)) is expected


def test_kind_of_an_audio_enclosure_is_audio_even_with_a_video_link():
    """A podcast whose items link to the episode on YouTube is still a podcast."""
    item = _item(YOUTUBE_LINK, enclosure_url="https://cdn/ep.mp3", enclosure_type="audio/mpeg")
    assert kind_of(item) == AUDIO


def test_kind_of_a_video_link_is_video():
    assert kind_of(_item(ARTE_LINK)) == VIDEO


def test_kind_of_a_video_enclosure_is_video():
    item = _item("https://example.com/ep", enclosure_url="https://cdn/ep.mp4")
    assert kind_of(item) == VIDEO


def test_kind_of_anything_else_is_text():
    assert kind_of(_item("https://waitbutwhy.com/post")) == TEXT


def test_kind_of_a_bayam_track_is_audio():
    item = _item("https://flyer.bayam.tv/4sYB/dj8", extra={"bayam_sku": "MBH288"})
    assert kind_of(item) == AUDIO


# --- what an item is called -------------------------------------------------


def test_item_id_of_a_youtube_video_is_its_video_id_whatever_the_feed_says():
    """The feed's id and the listing's id differ; the video does not."""
    from_feed = _item(YOUTUBE_LINK, extra={"id": "yt:video:BFcjfKZ0BeI"})
    from_listing = _item(YOUTUBE_LINK)
    assert item_id(from_feed) == item_id(from_listing) == "youtube:BFcjfKZ0BeI"


def test_item_id_of_an_arte_programme_includes_its_language():
    assert item_id(_item(ARTE_LINK)) == "arte:fr:127395-052-A"


def test_item_id_of_an_npo_episode_is_its_slug():
    assert item_id(_item(NPO_LINK)) == "npo:bureau-buitenland_281"


def test_item_id_of_an_ordinary_item_is_its_entry_id_then_its_link():
    assert item_id(_item("https://a/1", extra={"id": "urn:1"})) == "urn:1"
    assert item_id(_item("https://a/1")) == "https://a/1"


def test_item_id_of_an_item_with_nothing_else_is_its_title_and_date():
    when = datetime(2026, 7, 1, tzinfo=timezone.utc)
    assert item_id(_item(None, published=when)) == f"t|{when.isoformat()}"


# --- which source is asked --------------------------------------------------


def test_resolve_asks_the_right_source(monkeypatch):
    asked = []
    monkeypatch.setattr(arte, "resolve", lambda item, **kw: asked.append(("arte", kw)))
    monkeypatch.setattr(youtube, "resolve", lambda item, **kw: asked.append(("youtube", kw)))
    monkeypatch.setattr(npo, "resolve", lambda item, **kw: asked.append(("npo", kw)))
    quality = Quality(max_height=720)

    resolve(_item(ARTE_LINK), quality)
    resolve(_item(YOUTUBE_LINK), quality)
    resolve(_item(NPO_LINK), quality)

    assert asked == [
        ("arte", {"quality": quality}),
        ("youtube", {"quality": quality}),
        ("npo", {"quality": quality}),
    ]


def test_resolve_an_enclosure_downloads_the_enclosure():
    plan = resolve(_item("https://a/ep", enclosure_url="https://cdn/ep.mp3", enclosure_type="audio/mpeg"))
    assert plan == EnclosurePlan("https://cdn/ep.mp3", ".mp3")


def test_resolve_rejects_an_item_with_nothing_to_download():
    with pytest.raises(FeedError, match="Nothing to download"):
        resolve(_item("https://example.com/post"))


def test_enclosure_plan_fetch_downloads_into_the_folder(monkeypatch, tmp_path):
    seen = []

    def fake_download_to(url, path, **kwargs):
        seen.append(url)
        with open(path, "wb") as fh:
            fh.write(b"audio")

    monkeypatch.setattr(download, "download_to", fake_download_to)
    path = EnclosurePlan("https://cdn/ep.m4a?x=1").fetch(str(tmp_path), timeout=5)

    assert seen == ["https://cdn/ep.m4a?x=1"]
    assert os.path.dirname(path) == str(tmp_path)
    assert path.endswith(".m4a")


def test_collection_feed_leaves_an_ordinary_url_alone():
    assert collection_feed(FeedConfig(url="https://waitbutwhy.com/feed")) is None


def test_channel_feed_url_leaves_an_ordinary_url_alone():
    assert channel_feed_url("https://waitbutwhy.com/feed") is None


def test_permanent_reasons_are_the_three_not_worth_retrying():
    assert set(media.PERMANENT) == {media.VideoTooShort, media.VideoTooBig, media.VideoGone}


@pytest.mark.parametrize("url, source", [
    ("https://flyer.bayam.tv/4sYB/dj8", "Bayam"),
    ("https://app.bayam.tv/mag/abc", "Bayam"),
    ("https://www.arte.tv/fr/videos/RC-014035/le-dessous-des-cartes/", "ARTE"),
    ("https://npo.nl/start/serie/nos-journaal", "NPO Start"),
    ("https://npo.nl/start/serie/nos-journaal/afleveringen/seizoen-1", "NPO Start"),
    ("https://www.youtube.com/@talks", "YouTube"),
    ("https://www.youtube.com/channel/UCabcdefghijklmnopqrstuv", "YouTube"),
    ("https://flyer.bayam.tv/", None),
    ("https://app.bayam.tv/", None),
    ("https://app.bayam.tv/home/abc", None),
    ("https://www.youtube.com/feeds/videos.xml?channel_id=UCabc", None),
    # One video is a feed of one item: see tests/test_saved.py.
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "YouTube"),
    ("https://example.com/feed.xml", None),
    ("ftp://flyer.bayam.tv/4sYB/dj8", None),
    ("", None),
])
def test_link_source_is_told_from_the_address(url, source):
    from fetchit.media import link_source

    assert link_source(url) == source


# --- a second, smaller copy -------------------------------------------------


class _Rendition:
    def __init__(self, rendition):
        self.rendition = rendition


def _small_resolves_to(monkeypatch, answer):
    def fake(item, quality=Quality()):
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(media, "resolve", fake)


SMALL = Quality(small=Quality(part_bytes=95 * 1024 * 1024))
ANY = FeedItem(title="t", description="", link="https://a/1")


def test_no_small_copy_unless_the_feed_asks_for_one(monkeypatch):
    _small_resolves_to(monkeypatch, _Rendition("360p"))
    assert media.resolve_small(ANY, Quality(), _Rendition("1080p")) is None


def test_a_small_copy_is_another_rendition_than_the_main_one(monkeypatch):
    small = _Rendition("360p")
    _small_resolves_to(monkeypatch, small)
    assert media.resolve_small(ANY, SMALL, _Rendition("1080p")) is small
    assert media.resolve_small(ANY, SMALL, _Rendition("360p")) is None


def test_no_small_copy_of_what_is_too_big_for_one_or_comes_as_one_file(monkeypatch):
    _small_resolves_to(monkeypatch, media.VideoTooBig("too big"))
    assert media.resolve_small(ANY, SMALL, _Rendition("1080p")) is None
    assert media.resolve_small(ANY, SMALL, media.EnclosurePlan("https://a/1.mp4")) is None
