"""Tests for fetchit.run (feeds and downloads are faked — no network)."""

import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from fetchit import run
from fetchit.config import Config, ConfigError
from fetchit.feeds import FeedConfig, FeedError, FeedFilter, FeedItem, ParsedFeed, SourceBlocked
from fetchit.library import records
from fetchit.media import VideoTooShort
from fetchit.run import AlreadyRunning, single_run
from fetchit.state import State

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)


def _video(n, **kwargs):
    """The n-th upload of a channel: a YouTube link, published n days ago."""
    return FeedItem(
        title=f"Video {n}",
        description=f"About {n}.",
        link=f"https://www.youtube.com/watch?v=video{n:06d}",
        published=NOW - timedelta(days=n),
        **kwargs,
    )


def _post(n):
    return FeedItem(title=f"Post {n}", description="", link=f"https://blog/{n}",
                    published=NOW - timedelta(days=n))


class FakePlan:
    def __init__(self, item, error=None):
        self.item = item
        self.error = error

    def describe(self):
        return "1280x720, ~80 MB"

    def fetch(self, into, *, timeout):
        if self.error:
            raise self.error
        path = os.path.join(into, "video.mp4")
        with open(path, "wb") as fh:
            fh.write(self.item.title.encode())
        return path


@pytest.fixture
def world(monkeypatch, tmp_path):
    """A library in tmp_path, feeds that list what ``world.items`` says, fake downloads."""

    class World:
        root = str(tmp_path / "library")
        items: dict[str, list[FeedItem]] = {}
        failures: dict[str, Exception] = {}
        resolved: list[str] = []
        parsed_errors: dict[str, Exception] = {}

        def config(self, *feeds, keep_days=0):
            return Config(library=self.root, keep_days=keep_days, feeds=list(feeds))

        def titles(self, feed):
            return [r["title"] for r in records(self.root, feed)]

    w = World()

    def fake_parse(cfg):
        if cfg.url in w.parsed_errors:
            raise w.parsed_errors[cfg.url]
        return ParsedFeed(items=list(w.items.get(cfg.url, [])), title=cfg.name, description="About.")

    def fake_resolve(item, quality=None):
        w.resolved.append(item.title)
        error = w.failures.get(item.title)
        if isinstance(error, VideoTooShort):
            raise error
        return FakePlan(item, error)

    monkeypatch.setattr(run, "parse_feed", fake_parse)
    monkeypatch.setattr(run, "resolve", fake_resolve)
    monkeypatch.setattr(run, "feed_image", lambda cfg: None)
    return w


CHANNEL = FeedConfig(url="https://www.youtube.com/@channel", name="Channel")


def test_a_feed_seen_for_the_first_time_takes_only_its_newest_item(world):
    world.items[CHANNEL.url] = [_video(3), _video(1), _video(2)]
    summary = run.run(world.config(CHANNEL))

    assert summary.fetched == 1
    assert world.titles("Channel") == ["Video 1"]
    state = State.load(world.root)
    assert state.after("Channel") == NOW - timedelta(days=1)


def test_a_feed_with_nothing_dated_yet_starts_when_it_has(world):
    world.items[CHANNEL.url] = []
    run.run(world.config(CHANNEL))
    assert not State.load(world.root).known("Channel")
    world.items[CHANNEL.url] = [_video(2), _video(1)]
    run.run(world.config(CHANNEL))
    assert world.titles("Channel") == ["Video 1"]


def test_an_item_dated_before_the_starting_point_is_not_wanted(world):
    world.items[CHANNEL.url] = [_video(2)]
    run.run(world.config(CHANNEL))
    world.items[CHANNEL.url] = [_video(2), _video(5)]  # turned up late, dated early
    assert run.run(world.config(CHANNEL)).fetched == 0


def test_after_that_everything_new_is_taken_oldest_first(world):
    world.items[CHANNEL.url] = [_video(5)]
    run.run(world.config(CHANNEL))

    world.items[CHANNEL.url] = [_video(5), _video(3), _video(4), _video(2)]
    world.resolved.clear()
    summary = run.run(world.config(CHANNEL))

    assert summary.fetched == 3
    assert world.resolved == ["Video 4", "Video 3", "Video 2"]
    assert world.titles("Channel") == ["Video 5", "Video 4", "Video 3", "Video 2"]


def test_nothing_is_fetched_twice(world):
    world.items[CHANNEL.url] = [_video(1)]
    run.run(world.config(CHANNEL))
    world.resolved.clear()
    assert run.run(world.config(CHANNEL)).fetched == 0
    assert world.resolved == []


def test_a_pruned_item_is_not_fetched_again(world):
    world.items[CHANNEL.url] = [_video(1)]
    run.run(world.config(CHANNEL))
    for record in records(world.root, "Channel"):
        os.unlink(record["_path"])
    assert run.run(world.config(CHANNEL)).fetched == 0


def test_since_backfills_from_a_date_oldest_first_a_few_at_a_time(world):
    cfg = FeedConfig(url=CHANNEL.url, name="Channel", since=NOW - timedelta(days=10), max_per_run=2)
    world.items[cfg.url] = [_video(n) for n in (1, 5, 9, 12, 3)]

    run.run(world.config(cfg))
    assert world.titles("Channel") == ["Video 9", "Video 5"]
    run.run(world.config(cfg))
    assert world.titles("Channel") == ["Video 9", "Video 5", "Video 3", "Video 1"]


def test_max_per_run_holds_the_rest_for_the_next_run(world, capsys):
    cfg = FeedConfig(url=CHANNEL.url, name="Channel", max_per_run=1)
    world.items[cfg.url] = [_video(9)]
    run.run(world.config(cfg))
    world.items[cfg.url] = [_video(n) for n in (9, 3, 2, 1)]

    run.run(world.config(cfg))
    assert "2 more item(s) wait (the next run)" in capsys.readouterr().out
    assert world.titles("Channel") == ["Video 9", "Video 3"]


def test_an_item_that_fails_stops_its_feed_so_nothing_newer_overtakes_it(world, capsys):
    world.items[CHANNEL.url] = [_video(9)]
    run.run(world.config(CHANNEL))
    world.items[CHANNEL.url] = [_video(n) for n in (9, 3, 2, 1)]
    world.failures["Video 3"] = FeedError("yt-dlp failed: 403")

    summary = run.run(world.config(CHANNEL))
    assert summary.failed == 1
    assert world.titles("Channel") == ["Video 9"]
    assert "[Channel] stopped: yt-dlp failed: 403" in capsys.readouterr().err

    del world.failures["Video 3"]
    run.run(world.config(CHANNEL))
    assert world.titles("Channel") == ["Video 9", "Video 3", "Video 2", "Video 1"]


def test_a_permanent_answer_is_stepped_over_for_good(world):
    world.items[CHANNEL.url] = [_video(9)]
    run.run(world.config(CHANNEL))
    world.items[CHANNEL.url] = [_video(n) for n in (9, 3, 2)]
    world.failures["Video 3"] = VideoTooShort("a Short")

    summary = run.run(world.config(CHANNEL))
    assert (summary.fetched, summary.skipped) == (1, 1)
    assert world.titles("Channel") == ["Video 9", "Video 2"]

    world.resolved.clear()
    run.run(world.config(CHANNEL))
    assert world.resolved == []


def test_one_failing_feed_does_not_stop_the_others(world):
    blog = FeedConfig(url="https://blog/feed", name="Blog")
    world.parsed_errors[CHANNEL.url] = FeedError("down")
    world.items[blog.url] = [_post(1)]

    summary = run.run(world.config(CHANNEL, blog))
    assert summary.failed == 1
    assert world.titles("Blog") == ["Post 1"]


def test_a_blocked_source_is_a_skip_not_a_failure(world):
    world.parsed_errors[CHANNEL.url] = SourceBlocked("not reachable")
    summary = run.run(world.config(CHANNEL))
    assert (summary.blocked, summary.failed) == (1, 0)


def test_filters_decide_what_is_fetched(world):
    cfg = FeedConfig(url=CHANNEL.url, name="Channel",
                     filters=(FeedFilter("title", "excludes", ("2",)),))
    world.items[cfg.url] = [_video(5)]
    run.run(world.config(cfg))
    world.items[cfg.url] = [_video(n) for n in (5, 3, 2, 1)]
    run.run(world.config(cfg))
    assert world.titles("Channel") == ["Video 5", "Video 3", "Video 1"]


def test_text_items_are_filed_without_a_download(world):
    blog = FeedConfig(url="https://blog/feed", name="Blog")
    world.items[blog.url] = [_post(1)]
    run.run(world.config(blog))
    (record,) = records(world.root, "Blog")
    assert record["kind"] == "text"
    assert world.resolved == []


def test_a_run_describes_the_feed_in_its_folder(world):
    world.items[CHANNEL.url] = [_video(1)]
    run.run(world.config(CHANNEL))
    meta = json.loads(open(os.path.join(world.root, "shows", "Channel", "feed.json")).read())
    assert meta["title"] == "Channel"
    assert os.path.exists(os.path.join(world.root, "shows", "Channel", "tvshow.nfo"))


def test_a_dry_run_changes_nothing(world, capsys):
    world.items[CHANNEL.url] = [_video(2), _video(1)]
    summary = run.run(world.config(CHANNEL), dry_run=True)

    assert summary.fetched == 1
    out = capsys.readouterr().out
    assert "Video 1" in out and "1280x720, ~80 MB" in out
    assert "shows/Channel/Season 2026/Channel - S2026E1018" in out
    assert not os.path.exists(world.root) or records(world.root, "Channel") == []
    assert not State.load(world.root).known("Channel")


def test_only_runs_the_feeds_it_names(world):
    blog = FeedConfig(url="https://blog/feed", name="Blog")
    world.items[blog.url] = [_post(1)]
    world.items[CHANNEL.url] = [_video(1)]
    run.run(world.config(CHANNEL, blog), only=["blog"])
    assert world.titles("Blog") == ["Post 1"]
    assert world.titles("Channel") == []


def test_only_rejects_a_name_that_is_not_a_feed(world):
    with pytest.raises(ConfigError, match="No feed called 'Nope'"):
        run.run(world.config(CHANNEL), only=["Nope"])


def test_a_run_prunes_by_each_feeds_keep_days(world):
    cfg = FeedConfig(url=CHANNEL.url, name="Channel", keep_days=7)
    world.items[cfg.url] = [_video(1)]
    run.run(world.config(cfg))
    (record,) = records(world.root, "Channel")
    data = json.loads(open(record["_path"]).read())
    data["fetched"] = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    with open(record["_path"], "w") as fh:
        json.dump(data, fh)

    assert run.run(world.config(cfg)).pruned == 1
    assert world.titles("Channel") == []


def test_a_second_run_stands_aside_while_one_is_going(world):
    with single_run(world.root):
        with pytest.raises(AlreadyRunning):
            run.run(world.config(CHANNEL))


def test_artwork_is_fetched_once_a_feed_has_a_folder(world, monkeypatch):
    fetched = []

    def fake_download(url, path, **kwargs):
        fetched.append(url)
        with open(path, "wb") as fh:
            fh.write(b"jpg")

    monkeypatch.setattr(run, "download_to", fake_download)
    monkeypatch.setattr(run, "feed_image", lambda cfg: "https://img/channel.jpg")
    world.items[CHANNEL.url] = [_video(1)]
    run.run(world.config(CHANNEL))

    folder = os.path.join(world.root, "shows", "Channel")
    assert open(os.path.join(folder, "poster.jpg"), "rb").read() == b"jpg"
    assert "https://img/channel.jpg" in fetched

    fetched.clear()
    run.run(world.config(CHANNEL))
    assert fetched == []  # checked again in a week, not every run


def _with_etag(world, monkeypatch):
    real = run.parse_feed

    def parse(cfg):
        parsed = real(cfg)
        return ParsedFeed(items=parsed.items, title=parsed.title,
                          validators={cfg.url: {"etag": "e1", "modified": None}})

    monkeypatch.setattr(run, "parse_feed", parse)


def test_an_etag_is_remembered_once_the_feed_is_dealt_with(world, monkeypatch):
    from fetchit import cache, feeds

    _with_etag(world, monkeypatch)
    world.items[CHANNEL.url] = [_video(1)]
    run.run(world.config(CHANNEL))
    assert cache.get(feeds._HTTP_NS, CHANNEL.url) == {"etag": "e1", "modified": None}


def test_an_etag_is_not_remembered_when_the_feed_stopped_on_a_failure(world, monkeypatch):
    """Or the next run would hear "nothing new" and never retry the episode."""
    from fetchit import cache, feeds

    world.items[CHANNEL.url] = [_video(9)]
    run.run(world.config(CHANNEL))
    _with_etag(world, monkeypatch)
    world.items[CHANNEL.url] = [_video(9), _video(1)]
    world.failures["Video 1"] = FeedError("reset")

    run.run(world.config(CHANNEL))
    assert cache.get(feeds._HTTP_NS, CHANNEL.url) is None


def test_an_etag_is_not_remembered_by_a_dry_run(world, monkeypatch):
    from fetchit import cache, feeds

    _with_etag(world, monkeypatch)
    world.items[CHANNEL.url] = [_video(1)]
    run.run(world.config(CHANNEL), dry_run=True)
    assert cache.get(feeds._HTTP_NS, CHANNEL.url) is None


def test_max_per_day_keeps_a_backfill_in_step_with_its_reader(world):
    cfg = FeedConfig(url=CHANNEL.url, name="Channel", since=NOW - timedelta(days=30), max_per_day=1)
    world.items[cfg.url] = [_video(n) for n in (20, 10, 5)]

    run.run(world.config(cfg))
    run.run(world.config(cfg))  # the same day: nothing more
    assert world.titles("Channel") == ["Video 20"]
