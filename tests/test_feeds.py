"""Tests for fetchit.feeds (feedparser is monkeypatched — no network)."""

import json
from datetime import datetime, timedelta, timezone
from urllib.error import URLError

import pytest

from fetchit import feeds
from fetchit.config import load_config
from fetchit.feeds import (
    FeedConfig,
    FeedError,
    FeedFilter,
    FeedItem,
    FieldExtract,
    is_audio_item,
    item_fields,
    parse_feed,
    strip_html,
)


def _write_config(tmp_path, data):
    path = tmp_path / "fetchit.json"
    path.write_text(json.dumps(data))
    return str(path)


def load_feeds(path):
    """The feeds of the config file at ``path``, the way a run reads them."""
    return load_config(path, library="/nonexistent/library").feeds


def test_load_feeds_valid(tmp_path):
    path = _write_config(
        tmp_path,
        {
            "feeds": [
                {"name": "Blog", "url": "https://a/rss"},
                {"name": "Pod", "url": "https://b/rss"},
            ]
        },
    )

    configs = load_feeds(path)

    assert configs[0] == FeedConfig(url="https://a/rss", name="Blog")
    assert configs[1] == FeedConfig(url="https://b/rss", name="Pod")


def test_load_feeds_missing_file_raises():
    with pytest.raises(FeedError):
        load_feeds("/nonexistent/feeds.json")


def test_load_feeds_missing_url_raises(tmp_path):
    path = _write_config(tmp_path, {"feeds": [{"name": "F"}]})
    with pytest.raises(FeedError):
        load_feeds(path)


def test_load_feeds_not_an_object_raises(tmp_path):
    path = _write_config(tmp_path, [{"name": "F", "url": "https://a"}])
    with pytest.raises(FeedError):
        load_feeds(path)


def test_strip_html():
    assert strip_html("<p>Hello <b>world</b> &amp; more</p>") == "Hello world & more"
    assert strip_html("") == ""


def test_parse_feed_regular(monkeypatch):
    fake = type("Parsed", (), {})()
    fake.bozo = False
    fake.entries = [
        {
            "title": "Post one",
            "summary": "<p>Body &amp; text</p>",
            "link": "https://a/1",
            "published_parsed": (2026, 7, 20, 10, 0, 0, 0, 0, 0),
        }
    ]
    monkeypatch.setattr(feeds.feedparser, "parse", lambda url, **kwargs: fake)

    items = parse_feed(FeedConfig(url="https://a")).items

    assert items == [
        FeedItem(
            title="Post one",
            description="Body & text",
            link="https://a/1",
            published=datetime(2026, 7, 20, 10, 0, 0, tzinfo=timezone.utc),
        )
    ]


def test_parse_feed_podcast_enclosure(monkeypatch):
    fake = type("Parsed", (), {})()
    fake.bozo = False
    fake.entries = [
        {
            "title": "Episode 1",
            "description": "Show notes",
            "published_parsed": (2026, 7, 21, 8, 0, 0, 0, 0, 0),
            "enclosures": [{"href": "https://a/ep1.mp3", "type": "audio/mpeg"}],
        }
    ]
    monkeypatch.setattr(feeds.feedparser, "parse", lambda url, **kwargs: fake)

    item = parse_feed(FeedConfig(url="https://a")).items[0]

    assert item.enclosure_url == "https://a/ep1.mp3"
    assert item.enclosure_type == "audio/mpeg"


def test_parse_feed_unescapes_a_doubly_escaped_enclosure(monkeypatch):
    # NPO writes "&amp;amp;" in its podcast feeds, so feedparser hands back a URL
    # with a literal "&amp;" in the query — which their file server 404s on.
    fake = type("Parsed", (), {})()
    fake.bozo = False
    fake.entries = [
        {
            "title": "Episode 1",
            "published_parsed": (2026, 7, 21, 8, 0, 0, 0, 0, 0),
            "link": "https://a/1?x=1&amp;y=2",
            "enclosures": [
                {"href": "https://a/ep1.mp3?a=1&amp;b=2", "type": "audio/mpeg"}
            ],
            "image": {"href": "https://a/art.png?a=1&amp;b=2"},
        }
    ]
    monkeypatch.setattr(feeds.feedparser, "parse", lambda url, **kwargs: fake)

    item = parse_feed(FeedConfig(url="https://a")).items[0]

    assert item.enclosure_url == "https://a/ep1.mp3?a=1&b=2"
    assert item.link == "https://a/1?x=1&y=2"
    assert item.image_url == "https://a/art.png?a=1&b=2"


def test_parse_feed_bozo_no_entries_raises(monkeypatch):
    fake = type("Parsed", (), {})()
    fake.bozo = True
    fake.bozo_exception = "malformed"
    fake.entries = []
    monkeypatch.setattr(feeds.feedparser, "parse", lambda url, **kwargs: fake)

    with pytest.raises(FeedError):
        parse_feed(FeedConfig(url="https://a"))


def test_parse_feed_html_in_a_feeds_place_is_a_block_not_a_failure(monkeypatch):
    # A DNS filter's block page parses as badly as a corrupt feed and means
    # something completely different: nothing is broken and the next run inside
    # the open window reads the feed fine.
    fake = type("Parsed", (), {})()
    fake.bozo = True
    fake.bozo_exception = "not well-formed (invalid token)"
    fake.entries = []
    fake.headers = {"content-type": "text/html; charset=utf-8"}
    monkeypatch.setattr(feeds.feedparser, "parse", lambda url, **kwargs: fake)

    with pytest.raises(feeds.SourceBlocked) as caught:
        parse_feed(FeedConfig(url="https://a"))
    assert "text/html" in str(caught.value)


def test_parse_feed_broken_xml_is_still_a_failure(monkeypatch):
    # The feed says it is a feed. That it doesn't parse is a real fault and
    # keeps its traceback — only HTML gets the benefit of the doubt.
    fake = type("Parsed", (), {})()
    fake.bozo = True
    fake.bozo_exception = "mismatched tag"
    fake.entries = []
    fake.headers = {"content-type": "application/rss+xml"}
    monkeypatch.setattr(feeds.feedparser, "parse", lambda url, **kwargs: fake)

    with pytest.raises(FeedError) as caught:
        parse_feed(FeedConfig(url="https://a"))
    assert not isinstance(caught.value, feeds.SourceBlocked)


def test_parse_feed_without_a_content_type_keeps_the_old_behaviour(monkeypatch):
    # No header is no evidence — a feed read from a file has none either.
    fake = type("Parsed", (), {})()
    fake.bozo = True
    fake.bozo_exception = "malformed"
    fake.entries = []
    fake.headers = {}
    monkeypatch.setattr(feeds.feedparser, "parse", lambda url, **kwargs: fake)

    with pytest.raises(FeedError) as caught:
        parse_feed(FeedConfig(url="https://a"))
    assert not isinstance(caught.value, feeds.SourceBlocked)


def _unreadable(exception):
    fake = type("Parsed", (), {})()
    fake.bozo = True
    fake.bozo_exception = exception
    fake.entries = []
    return fake


def test_parse_feed_retries_when_the_feed_could_not_be_reached(monkeypatch):
    calls = []

    def flaky(url, **kwargs):
        calls.append(url)
        if len(calls) < 3:
            return _unreadable(URLError("nodename nor servname provided"))
        return _parsed([{"title": "Ep", "link": _MP3}])

    monkeypatch.setattr(feeds.feedparser, "parse", flaky)

    assert parse_feed(FeedConfig(url="https://a")).items
    assert len(calls) == 3


def test_parse_feed_does_not_retry_malformed_markup(monkeypatch):
    calls = []

    def malformed(url, **kwargs):
        calls.append(url)
        return _unreadable("malformed")

    monkeypatch.setattr(feeds.feedparser, "parse", malformed)

    with pytest.raises(FeedError):
        parse_feed(FeedConfig(url="https://a"))
    assert len(calls) == 1


# --- detecting audio -------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "mime", "expected"),
    [
        ("https://a/ep.mp3", "audio/mpeg", True),
        ("https://a/ep.m4a", "AUDIO/MP4", True),
        # Feeds really do serve mp3s as octet-stream, so the URL gets a say.
        ("https://a/ep.mp3", "application/octet-stream", True),
        ("https://a/ep.mp3", None, True),
        # …and CDNs really do bolt tracking parameters onto every enclosure.
        ("https://a/ep.mp3?source=rss&x=1", None, True),
        ("https://a/ep.opus", "", True),
        # Declared as something else: an ordinary feed with an attachment.
        ("https://a/cover.jpg", "image/jpeg", False),
        ("https://a/talk.mp4", "video/mp4", False),
        ("https://a/paper.pdf", "application/pdf", False),
        ("https://a/post", None, False),
    ],
)
def test_is_audio_item(url, mime, expected):
    item = FeedItem(
        title="t", description="d", enclosure_url=url, enclosure_type=mime
    )
    assert is_audio_item(item) is expected


def test_is_audio_item_without_an_enclosure():
    assert is_audio_item(FeedItem(title="t", description="d")) is False


# --- item fields -----------------------------------------------------------


def test_parse_feed_lifts_author_categories_and_extras(monkeypatch):
    fake = type("Parsed", (), {})()
    fake.bozo = False
    fake.entries = [
        {
            "title": "Episode 1",
            "description": "Show notes",
            "author": "Example Media",
            "tags": [{"term": "news"}, {"term": "politics"}],
            "id": "urn:uuid:1234",
            "itunes_duration": "00:42:11",
            "itunes_episode": 402,
            "published_parsed": (2026, 7, 21, 8, 0, 0, 0, 0, 0),
            "title_detail": {"value": "Episode 1"},
        }
    ]
    monkeypatch.setattr(feeds.feedparser, "parse", lambda url, **kwargs: fake)

    item = parse_feed(FeedConfig(url="https://a", name="Pod")).items[0]

    assert item.author == "Example Media"
    assert item.categories == ("news", "politics")
    assert item.feed_name == "Pod"
    assert item.extra == {
        "id": "urn:uuid:1234",
        "itunes_duration": "00:42:11",
        "itunes_episode": "402",
    }
    # Structured mirrors feedparser adds carry no useful string form.
    assert "title_detail" not in item.extra


def test_item_fields_includes_core_and_extras():
    item = FeedItem(
        title="T",
        description="D",
        published=datetime(2026, 7, 22, 6, 0, 0, tzinfo=timezone.utc),
        categories=("news", "politics"),
        feed_name="Pod",
        extra={"itunes_duration": "00:42:11"},
    )

    fields = item_fields(item)

    assert fields["title"] == "T"
    assert fields["published"] == "2026-07-22T06:00:00+00:00"
    assert fields["published_date"] == "2026-07-22"
    assert fields["categories"] == "news, politics"
    assert fields["feed_name"] == "Pod"
    assert fields["itunes_duration"] == "00:42:11"
    # Absent values are empty strings, never None.
    assert fields["link"] == ""
    assert fields["author"] == ""


def test_item_fields_extra_cannot_shadow_a_core_field():
    item = FeedItem(title="Real", description="D", extra={"title": "Impostor"})
    assert item_fields(item)["title"] == "Real"


# --- filter configuration --------------------------------------------------


def test_load_feeds_parses_filters(tmp_path):
    path = _write_config(
        tmp_path,
        {
            "feeds": [
                {
                    "name": "F",
                    "url": "https://a",
                    "title_contains": ["one", "two"],
                    "description_excludes": "rerun",
                    "title_matches": r"^Ep \d+",
                }
            ]
        },
    )

    filters = load_feeds(path)[0].filters

    assert FeedFilter("title", "contains", ("one", "two")) in filters
    # A bare string becomes a single-value filter.
    assert FeedFilter("description", "excludes", ("rerun",)) in filters
    assert FeedFilter("title", "matches", (r"^Ep \d+",)) in filters


def test_load_feeds_parses_numeric_bounds(tmp_path):
    path = _write_config(
        tmp_path,
        {
            "feeds": [
                {
                    "name": "F",
                    "url": "https://a",
                    "duration_seconds_min": 420,
                    "duration_seconds_max": "30:00",
                }
            ]
        },
    )

    filters = load_feeds(path)[0].filters

    # JSON may write a bound as a number or as a duration; both keep their text.
    assert FeedFilter("duration_seconds", "min", ("420",)) in filters
    assert FeedFilter("duration_seconds", "max", ("30:00",)) in filters


@pytest.mark.parametrize("bound", ["half an hour", ["420", "1800"], True])
def test_load_feeds_bad_numeric_bound_raises(tmp_path, bound):
    path = _write_config(
        tmp_path,
        {"feeds": [{"name": "F", "url": "https://a", "duration_seconds_min": bound}]},
    )
    with pytest.raises(FeedError) as excinfo:
        load_feeds(path)
    assert "duration_seconds_min" in str(excinfo.value)


def test_load_feeds_no_filters_is_empty_tuple(tmp_path):
    path = _write_config(tmp_path, {"feeds": [{"name": "F", "url": "https://a"}]})
    assert load_feeds(path)[0].filters == ()


def test_load_feeds_unknown_key_raises(tmp_path):
    # A typo like title_contain must not be silently ignored.
    path = _write_config(
        tmp_path, {"feeds": [{"name": "F", "url": "https://a", "title_contain": "x"}]}
    )
    with pytest.raises(FeedError) as excinfo:
        load_feeds(path)
    assert "title_contain" in str(excinfo.value)


def test_load_feeds_bad_regex_raises(tmp_path):
    path = _write_config(
        tmp_path,
        {"feeds": [{"name": "F", "url": "https://a", "title_matches": "([unclosed"}]},
    )
    with pytest.raises(FeedError):
        load_feeds(path)


def test_load_feeds_filter_value_must_be_string_or_list(tmp_path):
    path = _write_config(
        tmp_path,
        {"feeds": [{"name": "F", "url": "https://a", "title_contains": 42}]},
    )
    with pytest.raises(FeedError):
        load_feeds(path)


# --- artwork ---------------------------------------------------------------


def _parsed(entries, feed=None):
    """A stand-in for a feedparser result."""
    fake = type("Parsed", (), {})()
    fake.bozo = False
    fake.entries = entries
    fake.feed = feed or {}
    return fake


def _parse_one(monkeypatch, entry, feed=None):
    monkeypatch.setattr(
        feeds.feedparser, "parse", lambda url, **kwargs: _parsed([entry], feed)
    )
    return parse_feed(FeedConfig(url="https://a")).items[0]


def test_parse_feed_takes_episode_itunes_image(monkeypatch):
    item = _parse_one(
        monkeypatch,
        {"title": "Ep", "image": {"href": "https://a/ep.jpg"}},
        feed={"image": {"href": "https://a/show.jpg"}},
    )
    # The episode's own artwork wins over the channel's.
    assert item.image_url == "https://a/ep.jpg"


def test_parse_feed_takes_media_thumbnail(monkeypatch):
    item = _parse_one(
        monkeypatch,
        {"title": "Ep", "media_thumbnail": [{"url": "https://a/thumb.jpg"}]},
    )
    assert item.image_url == "https://a/thumb.jpg"


def test_parse_feed_ignores_channel_artwork(monkeypatch):
    # Most podcasts set artwork once on the show. That logo says nothing about
    # the episode, so an item without its own image gets none.
    item = _parse_one(
        monkeypatch, {"title": "Ep"}, feed={"image": {"href": "https://a/show.jpg"}}
    )
    assert item.image_url is None


def test_parse_feed_without_any_image(monkeypatch):
    assert _parse_one(monkeypatch, {"title": "Ep"}).image_url is None


def test_image_keys_do_not_leak_into_extra(monkeypatch):
    item = _parse_one(
        monkeypatch,
        {"title": "Ep", "image": {"href": "https://a/ep.jpg"}, "id": "abc"},
    )
    assert "image" not in item.extra
    assert item.extra["id"] == "abc"


def test_item_fields_includes_image_url():
    item = FeedItem(title="Ep", description="d", image_url="https://a/ep.jpg")
    assert item_fields(item)["image_url"] == "https://a/ep.jpg"


# --- link previews ---------------------------------------------------------

# The same item without the audio: whether a card is sent is decided per item,
# so the two fixtures are what the default branches on.
# --- feed settings -------------------------------------------------------


def test_load_feeds_refresh_image_defaults_to_on_and_can_be_turned_off(tmp_path):
    path = _write_config(
        tmp_path,
        {
            "feeds": [
                {"name": "F", "url": "https://a"},
                {"name": "G", "url": "https://b", "refresh_image": False},
            ]
        },
    )

    kept, pinned = load_feeds(path)

    assert kept.refresh_image is True
    assert pinned.refresh_image is False


def test_load_feeds_refresh_image_must_be_boolean(tmp_path):
    path = _write_config(
        tmp_path,
        {"feeds": [{"name": "F", "url": "https://a", "refresh_image": "no"}]},
    )
    with pytest.raises(FeedError, match="refresh_image must be true or false"):
        load_feeds(path)


# --- field extraction ------------------------------------------------------

# Podcast feeds routinely bury the only per-episode id inside the media url.
_MP3 = "https://pod.example/file/show/54321/an-episode.mp3?token=abc"


def _extracted(monkeypatch, pattern):
    """Parse a one-item feed through a single ``episode_id`` rule."""
    monkeypatch.setattr(
        feeds.feedparser, "parse", lambda url, **kwargs: _parsed([{"title": "Ep", "link": _MP3}])
    )
    cfg = FeedConfig(
        url="https://a",
        extract=(FieldExtract(name="episode_id", source="link", pattern=pattern),),
    )
    return parse_feed(cfg).items[0]


def test_extract_takes_the_first_capture_group(monkeypatch):
    item = _extracted(monkeypatch, r"/file/[^/]+/(\d+)/")

    assert item.extra["episode_id"] == "54321"
    # And it is a field like any other, so templates and filters can see it.
    assert item_fields(item)["episode_id"] == "54321"


def test_extract_without_a_group_takes_the_whole_match(monkeypatch):
    assert _extracted(monkeypatch, r"\d+").extra["episode_id"] == "54321"


def test_extract_without_a_match_is_empty(monkeypatch):
    assert _extracted(monkeypatch, r"/episode/(\d+)/").extra["episode_id"] == ""


def test_extract_cannot_shadow_a_core_field():
    item = FeedItem(title="Real title", description="d", link=_MP3)
    rules = (FieldExtract(name="title", source="link", pattern=r"(\d+)"),)

    assert item_fields(feeds.apply_extracts(item, rules))["title"] == "Real title"


def test_extract_reads_the_item_not_other_extracts():
    # Rules all see the item as it came off the feed, so ordering can't matter.
    item = FeedItem(title="Ep", description="d", link=_MP3)
    rules = (
        FieldExtract(name="episode_id", source="link", pattern=r"/(\d+)/"),
        FieldExtract(name="derived", source="episode_id", pattern=r"\d"),
    )

    result = feeds.apply_extracts(item, rules)

    assert result.extra["episode_id"] == "54321"
    assert result.extra["derived"] == ""


def test_extracted_field_can_be_filtered_on():
    item = FeedItem(title="Ep", description="d", link=_MP3)
    rules = (FieldExtract(name="episode_id", source="link", pattern=r"/(\d+)/"),)
    item = feeds.apply_extracts(item, rules)

    keep = (FeedFilter("episode_id", "matches", (r"^5",)),)
    drop = (FeedFilter("episode_id", "matches", (r"^9",)),)

    assert feeds.apply_filters([item], keep) == [item]
    assert feeds.apply_filters([item], drop) == []


def test_load_feeds_parses_extract(tmp_path):
    path = _write_config(
        tmp_path,
        {
            "feeds": [
                {
                    "name": "F",
                    "url": "https://a",
                    "extract": {
                        "episode_id": {"from": "link", "pattern": r"/(\d+)/"}
                    },
                }
            ]
        },
    )

    cfg = load_feeds(path)[0]

    assert cfg.extract == (
        FieldExtract(name="episode_id", source="link", pattern=r"/(\d+)/"),
    )


def test_load_feeds_extract_defaults_to_empty(tmp_path):
    path = _write_config(tmp_path, {"feeds": [{"name": "F", "url": "https://a"}]})
    assert load_feeds(path)[0].extract == ()


@pytest.mark.parametrize(
    "spec",
    [
        "link:/(\\d+)/",  # not an object
        {"pattern": "/(\\d+)/"},  # no source field
        {"from": "link"},  # no pattern
        {"from": "link", "pattern": ""},  # empty pattern
        {"from": "link", "pattern": "([0-9]+"},  # unbalanced parenthesis
        {"from": "link", "pattern": "/(\\d+)/", "flags": "i"},  # unknown key
    ],
)
def test_load_feeds_rejects_a_malformed_extract(tmp_path, spec):
    path = _write_config(
        tmp_path,
        {
            "feeds": [
                {"name": "F", "url": "https://a", "extract": {"episode_id": spec}}
            ]
        },
    )
    with pytest.raises(FeedError) as excinfo:
        load_feeds(path)
    assert "episode_id" in str(excinfo.value)


def test_load_feeds_extract_must_be_an_object(tmp_path):
    path = _write_config(
        tmp_path,
        {"feeds": [{"name": "F", "url": "https://a", "extract": ["episode_id"]}]},
    )
    with pytest.raises(FeedError):
        load_feeds(path)


# --- channel description ---------------------------------------------------


def test_parse_feed_takes_the_channel_description(monkeypatch):
    monkeypatch.setattr(
        feeds.feedparser,
        "parse",
        lambda url, **kwargs: _parsed([{"title": "Ep"}], {"summary": "<p>A daily &amp; show</p>"}),
    )
    parsed = parse_feed(FeedConfig(url="https://a"))
    # HTML is stripped: it becomes a Signal group description, not a web page.
    assert parsed.description == "A daily & show"


def test_parse_feed_falls_back_to_the_channel_subtitle(monkeypatch):
    monkeypatch.setattr(
        feeds.feedparser,
        "parse",
        lambda url, **kwargs: _parsed([{"title": "Ep"}], {"subtitle": "Short blurb"}),
    )
    assert parse_feed(FeedConfig(url="https://a")).description == (
        "Short blurb"
    )


def test_parse_feed_without_a_channel_description(monkeypatch):
    monkeypatch.setattr(
        feeds.feedparser, "parse", lambda url, **kwargs: _parsed([{"title": "Ep"}])
    )
    assert parse_feed(FeedConfig(url="https://a")).description == ""


def test_channel_description_does_not_leak_into_items(monkeypatch):
    # The show's blurb is not the episode's notes.
    monkeypatch.setattr(
        feeds.feedparser,
        "parse",
        lambda url, **kwargs: _parsed([{"title": "Ep"}], {"summary": "Show blurb"}),
    )
    assert parse_feed(FeedConfig(url="https://a")).items[0].description == ""


# --- the feed's own artwork -------------------------------------------------


def _channel_with_image(href):
    fake = type("Parsed", (), {})()
    fake.bozo = False
    fake.entries = []
    fake.feed = {"image": {"href": href}} if href else {}
    fake.etag = "new-etag"
    return fake


def test_feed_image_reads_the_channel_artwork(monkeypatch):
    fake = _channel_with_image("https://a/show.jpg")
    monkeypatch.setattr(feeds.feedparser, "parse", lambda url, **kwargs: fake)

    assert feeds.feed_image(FeedConfig(url="https://a", name="Pod")) == (
        "https://a/show.jpg"
    )


def test_feed_image_of_a_feed_without_artwork_is_none(monkeypatch):
    fake = _channel_with_image(None)
    monkeypatch.setattr(feeds.feedparser, "parse", lambda url, **kwargs: fake)

    assert feeds.feed_image(FeedConfig(url="https://a", name="Pod")) is None


def test_feed_image_reads_unconditionally_and_leaves_the_etag_alone(monkeypatch):
    # A 304 would say nothing about the picture; and handing the run a fresh
    # ETag would make it answer "nothing new" about items it never saw.
    feeds.cache.put(feeds._HTTP_NS, "https://a", {"etag": "old-etag", "modified": None})
    asked = []

    def fake_parse(url, **kwargs):
        asked.append(kwargs)
        return _channel_with_image("https://a/show.jpg")

    monkeypatch.setattr(feeds.feedparser, "parse", fake_parse)

    feeds.feed_image(FeedConfig(url="https://a", name="Pod"))

    assert asked == [{}]
    assert feeds.cache.get(feeds._HTTP_NS, "https://a")["etag"] == "old-etag"


def test_feed_image_asks_a_video_source_instead_of_its_feed(monkeypatch):
    from fetchit import media as video

    monkeypatch.setattr(video, "source_image", lambda url: "https://yt3/avatar")
    monkeypatch.setattr(
        feeds.feedparser, "parse", lambda *a, **k: pytest.fail("feed read")
    )

    cfg = FeedConfig(url="https://www.youtube.com/channel/UCabc", name="Chan")
    assert feeds.feed_image(cfg) == "https://yt3/avatar"


# --- validators -------------------------------------------------------------


def _feed_with_etag(etag="e1"):
    fake = type("Parsed", (), {})()
    fake.bozo = False
    fake.entries = []
    fake.feed = {}
    fake.etag = etag
    return fake


def test_parse_feed_hands_the_etag_back_without_remembering_it(monkeypatch):
    monkeypatch.setattr(feeds.feedparser, "parse", lambda url, **kwargs: _feed_with_etag())

    parsed = parse_feed(FeedConfig(url="https://a", name="Pod"))

    assert parsed.validators == {"https://a": {"etag": "e1", "modified": None}}
    assert feeds.cache.get(feeds._HTTP_NS, "https://a") is None


def test_remembered_validators_go_back_with_the_next_read(monkeypatch):
    asked = []

    def fake_parse(url, **kwargs):
        asked.append(kwargs)
        return _feed_with_etag()

    monkeypatch.setattr(feeds.feedparser, "parse", fake_parse)
    feeds.remember_validators(parse_feed(FeedConfig(url="https://a", name="Pod")))
    parse_feed(FeedConfig(url="https://a", name="Pod"))

    assert asked == [{}, {"etag": "e1"}]


def test_a_backfill_neither_asks_conditionally_nor_hands_validators_back(monkeypatch):
    asked = []
    monkeypatch.setattr(
        feeds.feedparser, "parse", lambda url, **kw: asked.append(kw) or _feed_with_etag()
    )
    feeds.cache.put(feeds._HTTP_NS, "https://a", {"etag": "old", "modified": None})
    cfg = FeedConfig(url="https://a", name="Pod", since=datetime(2020, 1, 1, tzinfo=timezone.utc))

    assert parse_feed(cfg).validators == {}
    assert asked == [{}]
