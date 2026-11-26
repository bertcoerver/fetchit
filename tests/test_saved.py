"""Tests for fetchit.saved (the sources are faked — no network)."""

from datetime import datetime, timedelta, timezone

import pytest

from fetchit import arte, cache, feeds, media, npo, run, saved, youtube
from fetchit.config import Config
from fetchit.feeds import FeedConfig, FeedError, FeedItem, SourceBlocked
from fetchit.library import records
from fetchit.state import State

VIDEO = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
OTHER = "https://www.youtube.com/watch?v=aaaaaaaaaaa"
PROGRAMME = "https://www.arte.tv/fr/videos/127395-115-A/le-dessous-des-images/"
EPISODE = "https://npo.nl/start/afspelen/hijskraan_1"
WENT_UP = datetime(2019, 5, 4, 8, 0, tzinfo=timezone.utc)


def _upload(url, title="A video", **extra):
    return FeedItem(
        title=title, description="About it.", link=url, published=WENT_UP,
        image_url="https://i.ytimg.com/vi/x/hqdefault.jpg", author="A channel",
        extra={"id": "yt:video:x", "duration_seconds": "600", "show_title": "A channel", **extra},
    )


@pytest.fixture
def sources(monkeypatch):
    """Sources that answer from ``sources.items`` and count how often they are asked."""

    class Sources:
        items: dict[str, FeedItem] = {}
        errors: dict[str, Exception] = {}
        asked: list[str] = []

    s = Sources()
    s.items, s.errors, s.asked = {VIDEO: _upload(VIDEO)}, {}, []

    def look_up(url, *, feed_name=""):
        s.asked.append(url)
        if url in s.errors:
            raise s.errors[url]
        return s.items[url]

    monkeypatch.setattr(youtube, "video_item", look_up)
    monkeypatch.setattr(arte, "program_item", look_up)
    monkeypatch.setattr(npo, "episode_item", look_up)
    monkeypatch.setattr(media, "check_source_available", lambda url: None)
    monkeypatch.setattr(saved, "_last_stamp", None)
    return s


# --- which links ----------------------------------------------------------


@pytest.mark.parametrize("url, source", [
    (VIDEO, "YouTube"),
    ("https://youtu.be/dQw4w9WgXcQ?si=abc", "YouTube"),
    ("https://www.youtube.com/shorts/dQw4w9WgXcQ", "YouTube"),
    (PROGRAMME, "ARTE"),
    (EPISODE, "NPO Start"),
    ("https://npo.nl/start/serie/het-klokhuis/seizoen-2026/hijskraan_1/afspelen", "NPO Start"),
    ("https://www.youtube.com/@channel", None),
    ("https://www.arte.tv/fr/videos/RC-023176/le-dessous-des-images/", None),
    ("https://npo.nl/start/serie/het-klokhuis/afleveringen", None),
    ("https://example.com/watch?v=dQw4w9WgXcQ", None),
    ("https://invidious.nerdvpn.de/watch?v=dQw4w9WgXcQ", None),
    ("", None),
])
def test_the_link_of_one_item_is_told_from_the_address(url, source):
    assert saved.item_source(url) == source
    assert media.is_item_link(url) is (source is not None)


def test_a_link_list_takes_the_link_of_one_item_too():
    assert media.link_source(VIDEO) == "YouTube"
    assert media.link_source(EPISODE) == "NPO Start"


@pytest.mark.parametrize("url, plain", [
    ("https://youtu.be/dQw4w9WgXcQ?si=abc&t=42", VIDEO),
    ("https://m.youtube.com/watch?feature=share&v=dQw4w9WgXcQ&list=PL1", VIDEO),
    ("https://npo.nl/start/serie/het-klokhuis/seizoen-2026/hijskraan_1/afspelen", EPISODE),
    ("https://invidious.nerdvpn.de/watch?v=dQw4w9WgXcQ", VIDEO),
    ("https://yewtu.be/watch?v=dQw4w9WgXcQ&t=42&listen=1", VIDEO),
    ("https://invidious.nerdvpn.de/watch?v=tooshort", "https://invidious.nerdvpn.de/watch?v=tooshort"),
    ("https://invidious.nerdvpn.de/channel/UCabc", "https://invidious.nerdvpn.de/channel/UCabc"),
    (PROGRAMME, PROGRAMME),
    ("https://www.youtube.com/@channel", "https://www.youtube.com/@channel"),
])
def test_a_saved_link_is_written_the_one_plain_way(url, plain):
    assert saved.canonical(url) == plain


# --- the item -------------------------------------------------------------


def test_a_saved_item_is_dated_when_it_was_saved_and_says_when_it_went_up(sources):
    before = datetime.now(timezone.utc).replace(microsecond=0)
    feed = saved.parse_item(FeedConfig(url=VIDEO, name="Saved for later"))

    (item,) = feed.items
    assert item.title == "A video" and item.author == "A channel"
    assert item.feed_name == "Saved for later"
    assert before <= item.published <= datetime.now(timezone.utc)
    assert item.extra["saved"] == item.published.isoformat()
    assert item.extra["originally_published"] == WENT_UP.isoformat()
    # A video is not a show: the feed goes by the name its config gives it.
    assert (feed.title, feed.image_url, feed.description) == ("", None, "")


def test_a_saved_link_is_looked_up_once(sources):
    cfg = FeedConfig(url=VIDEO, name="Saved for later")
    first = saved.parse_item(cfg).items[0]
    sources.errors[VIDEO] = FeedError("taken down since")

    again = saved.parse_item(cfg).items[0]

    assert sources.asked == [VIDEO]
    assert again == first


def test_a_remembered_link_asks_nothing_of_its_source(sources, monkeypatch):
    cfg = FeedConfig(url=VIDEO, name="Saved for later")
    saved.parse_item(cfg)
    monkeypatch.setattr(media, "check_source_available", lambda url: pytest.fail("asked"))

    assert feeds.parse_feed(cfg).items[0].title == "A video"


def test_a_link_that_cannot_be_read_is_not_remembered(sources):
    cfg = FeedConfig(url=VIDEO, name="Saved for later")
    sources.errors[VIDEO] = FeedError("yt-dlp failed")
    with pytest.raises(FeedError, match="yt-dlp failed"):
        saved.parse_item(cfg)

    del sources.errors[VIDEO]
    assert saved.parse_item(cfg).items[0].title == "A video"
    assert sources.asked == [VIDEO, VIDEO]


def test_two_links_read_in_the_same_second_are_two_moments(sources):
    sources.items[OTHER] = _upload(OTHER, "Another")

    one = saved.parse_item(FeedConfig(url=VIDEO, name="S")).items[0]
    two = saved.parse_item(FeedConfig(url=OTHER, name="S")).items[0]

    assert two.published - one.published >= timedelta(seconds=1)
    assert one.published.microsecond == two.published.microsecond == 0


def test_an_episode_is_named_with_its_show_and_loses_its_place_in_the_series(sources):
    sources.items[EPISODE] = FeedItem(
        title="Hijskraan", description="", link=EPISODE, published=WENT_UP, author="NTR",
        extra={"show_title": "Het Klokhuis", "season": "2026", "episode": "248"},
    )

    item = saved.parse_item(FeedConfig(url=EPISODE, name="S")).items[0]

    assert item.title == "Het Klokhuis - Hijskraan"
    assert "season" not in item.extra and "episode" not in item.extra
    assert (item.extra["source_season"], item.extra["source_episode"]) == ("2026", "248")


def test_a_show_already_in_the_title_is_not_said_twice(sources):
    sources.items[PROGRAMME] = FeedItem(
        title="Le dessous des images", description="", link=PROGRAMME, published=WENT_UP,
        extra={"show_title": "Le dessous des images"},
    )
    assert saved.parse_item(FeedConfig(url=PROGRAMME, name="S")).items[0].title == "Le dessous des images"


def test_something_else_in_the_cache_is_asked_for_again(sources):
    cache.put("saved_item", VIDEO, {"title": "half an entry"})
    assert saved.parse_item(FeedConfig(url=VIDEO, name="S")).items[0].title == "A video"
    assert sources.asked == [VIDEO]


# --- a list of them -------------------------------------------------------


def _list(tmp_path, *links):
    return FeedConfig(url=links[0], urls=links if len(links) > 1 else (), name="Saved for later",
                      urls_file=str(tmp_path / "saved.txt"))


def test_a_link_that_cannot_be_read_does_not_hold_up_the_rest_of_the_list(sources, tmp_path, capsys):
    sources.items[OTHER] = _upload(OTHER, "Another")
    sources.errors[VIDEO] = SourceBlocked("YouTube is not reachable")

    feed = feeds.parse_feed(_list(tmp_path, VIDEO, OTHER))

    assert [i.title for i in feed.items] == ["Another"]
    assert f"{VIDEO} left for the next run: YouTube is not reachable" in capsys.readouterr().err

    # Read at last, it is dated after the one that was read meanwhile, not behind it.
    del sources.errors[VIDEO]
    late, other = feeds.parse_feed(_list(tmp_path, VIDEO, OTHER)).items
    assert late.title == "A video" and late.published > other.published


def test_a_list_of_one_link_that_cannot_be_read_fails_as_a_feed(sources, tmp_path):
    sources.errors[VIDEO] = FeedError("yt-dlp failed")
    with pytest.raises(FeedError, match="yt-dlp failed"):
        feeds.parse_feed(_list(tmp_path, VIDEO))


def test_a_feed_of_saved_links_has_no_artwork_to_ask_for(sources):
    assert feeds.feed_image(FeedConfig(url=VIDEO, name="Saved for later")) is None
    assert sources.asked == []


def test_saved_links_are_downloaded_in_the_order_they_were_saved(sources, tmp_path, monkeypatch):
    """The whole way: a list, a run, the library — and a link added later."""

    class Plan:
        def __init__(self, item):
            self.item = item

        def fetch(self, into, *, timeout):
            path = f"{into}/video.mp4"
            with open(path, "wb") as fh:
                fh.write(self.item.title.encode())
            return path

    monkeypatch.setattr(run, "resolve", lambda item, quality=None: Plan(item))
    monkeypatch.setattr(run, "_download_image", lambda url, into, label: None)
    root = str(tmp_path / "library")
    sources.items[OTHER] = _upload(OTHER, "Another", id="yt:video:y")

    def go(*links):
        return run.run(Config(library=root, feeds=[_list(tmp_path, *links)]))

    assert go(VIDEO).fetched == 1
    # An upload of 2019, saved now: filed as this year's, under today's date.
    (record,) = records(root, "Saved for later")
    assert record["published"][:4] == f"{datetime.now(timezone.utc):%Y}"
    assert record["fields"]["originally_published"] == WENT_UP.isoformat()
    assert State.load(root).seen("Saved for later", "youtube:dQw4w9WgXcQ")

    assert go(VIDEO, OTHER).fetched == 1
    assert [r["title"] for r in records(root, "Saved for later")] == ["A video", "Another"]
    assert go(VIDEO, OTHER).fetched == 0
    assert sources.asked == [VIDEO, OTHER]
