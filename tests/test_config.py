"""Tests for fetchit.config: the config file, and what overrides it."""

import json
import os
from datetime import datetime, timezone

import pytest

from fetchit.config import ConfigError, LIBRARY_ENV, load_config
from fetchit.feeds import DEFAULT_MAX_PER_RUN, FeedError
from fetchit.quality import MB, Quality


def _config(tmp_path, data):
    path = tmp_path / "fetchit.json"
    path.write_text(json.dumps(data))
    return str(path)


def _feeds(*feeds, **top):
    return {"feeds": list(feeds), **top}


FEED = {"name": "Blog", "url": "https://a/rss"}


def test_defaults(tmp_path):
    config = load_config(_config(tmp_path, _feeds(FEED)))
    assert config.library == os.path.expanduser("~/fetchit")
    assert config.keep_days == 0
    assert config.quality == Quality()
    (feed,) = config.feeds
    assert feed.name == "Blog" and feed.url == "https://a/rss"
    assert feed.since is None and feed.max_per_run == DEFAULT_MAX_PER_RUN
    assert feed.keep_days is None and config.keep_days_for(feed) == 0


def test_library_from_the_file_then_the_environment_then_the_argument(tmp_path, monkeypatch):
    path = _config(tmp_path, _feeds(FEED, library=str(tmp_path / "file")))
    assert load_config(path).library == str(tmp_path / "file")

    monkeypatch.setenv(LIBRARY_ENV, str(tmp_path / "env"))
    assert load_config(path).library == str(tmp_path / "env")

    assert load_config(path, library=str(tmp_path / "arg")).library == str(tmp_path / "arg")


def test_feed_settings(tmp_path):
    feed = {
        **FEED,
        "since": "2019-01-01",
        "max_per_run": 3,
        "keep_days": 7,
        "refresh_image": False,
        "language": "fr",
        "title_contains": "x",
    }
    config = load_config(_config(tmp_path, _feeds(feed, keep_days=30)))
    (cfg,) = config.feeds
    assert cfg.since == datetime(2019, 1, 1, tzinfo=timezone.utc)
    assert cfg.max_per_run == 3
    assert config.keep_days_for(cfg) == 7
    assert cfg.refresh_image is False
    assert cfg.language == "fr"
    assert [f.field for f in cfg.filters] == ["title"]


def test_a_feeds_quality_is_read_on_top_of_the_librarys(tmp_path):
    data = _feeds(
        FEED,
        {"name": "Signal", "url": "https://b", "quality": {"part_size_mb": 95}},
        quality={"max_height": 720},
    )
    blog, signal = load_config(_config(tmp_path, data)).feeds
    assert blog.quality == Quality(max_height=720)
    assert signal.quality == Quality(max_height=720, part_bytes=95 * MB)


def test_urls_make_one_feed_of_several_addresses(tmp_path):
    feed = {"name": "Mags", "urls": ["https://flyer.bayam.tv/a", "https://flyer.bayam.tv/b"]}
    (cfg,) = load_config(_config(tmp_path, _feeds(feed))).feeds
    assert cfg.url == "https://flyer.bayam.tv/a"
    assert cfg.sources == ("https://flyer.bayam.tv/a", "https://flyer.bayam.tv/b")


def test_names_have_to_be_unique(tmp_path):
    data = _feeds(FEED, {"name": "blog", "url": "https://b"})
    with pytest.raises(FeedError, match="unique"):
        load_config(_config(tmp_path, data))


def test_rssignal_keys_say_where_they_belong(tmp_path):
    data = _feeds({**FEED, "message_template": "{title}"})
    with pytest.raises(FeedError, match="rssignal's feeds.json"):
        load_config(_config(tmp_path, data))


@pytest.mark.parametrize(
    "feed, message",
    [
        ({"name": "x"}, "missing a \"url\""),
        ({"url": "https://a"}, "missing a non-empty \"name\""),
        ({**FEED, "max_per_run": 0}, "max_per_run"),
        ({**FEED, "max_per_day": 0}, "max_per_day"),
        ({**FEED, "keep_days": -1}, "keep_days"),
        ({**FEED, "since": "last week"}, "since"),
        ({**FEED, "urls": "https://b"}, "urls must be a list"),
    ],
)
def test_bad_feed_settings_are_named(tmp_path, feed, message):
    with pytest.raises(FeedError, match=message):
        load_config(_config(tmp_path, _feeds(feed)))


def test_unknown_top_level_keys_are_rejected(tmp_path):
    with pytest.raises(ConfigError, match="unknown key"):
        load_config(_config(tmp_path, _feeds(FEED, keep=3)))


def test_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="fetchit.example.json"):
        load_config(str(tmp_path / "nope.json"))


def test_keep_days_from_the_environment_when_the_file_does_not_say(tmp_path, monkeypatch):
    monkeypatch.setenv("FETCHIT_KEEP_DAYS", "21")
    assert load_config(_config(tmp_path, _feeds(FEED))).keep_days == 21
    assert load_config(_config(tmp_path, _feeds(FEED, keep_days=3))).keep_days == 3
