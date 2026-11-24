"""Tests for fetchit.cli's forget command (no network: the library is written directly)."""

import json
from datetime import datetime, timezone

import pytest

from fetchit.cli import main
from fetchit.feeds import FeedConfig, FeedItem, ParsedFeed
from fetchit.library import records, write_item
from fetchit.state import State

BLOG = FeedConfig(url="https://blog.example/feed", name="Blog")


@pytest.fixture
def library(tmp_path):
    root = tmp_path / "library"
    config = tmp_path / "fetchit.json"
    config.write_text(json.dumps({"library": str(root), "feeds": [{"name": "Blog", "url": BLOG.url}]}))
    state = State.load(str(root))
    for item_id, title, day in (("1", "Drôle de fête", 5), ("2", "Seuls au monde", 6), ("3", "Seuls en mer", 7)):
        item = FeedItem(title=title, description="", link=f"https://blog.example/{item_id}",
                        published=datetime(2026, 11, day, tzinfo=timezone.utc))
        write_item(str(root), BLOG, ParsedFeed(items=[]), item, "text", item_id=item_id)
        state.mark("Blog", item_id)
    state.save()
    return ["--config", str(config)], str(root)


def test_forget_without_an_episode_lists_the_feeds(library, capsys):
    args, _ = library
    assert main([*args, "forget", "Blog"]) == 0
    out = capsys.readouterr().out
    assert out.index("Seuls en mer") < out.index("Seuls au monde") < out.index("Drôle de fête")
    assert "id: 1" in out and "[no picture]" in out


def test_forget_by_title_text_and_by_id(library, capsys):
    args, root = library
    assert main([*args, "forget", "Blog", "drôle DE", "3"]) == 0
    out = capsys.readouterr().out
    assert "Forgot Drôle de fête" in out and "Forgot Seuls en mer" in out and "2 episode(s)" in out
    assert [r["title"] for r in records(root, "Blog")] == ["Seuls au monde"]
    state = State.load(root)
    assert not state.seen("Blog", "1") and state.seen("Blog", "2") and not state.seen("Blog", "3")


def test_text_that_matches_several_episodes_forgets_none(library, capsys):
    args, root = library
    assert main([*args, "forget", "Blog", "Seuls"]) == 1
    err = capsys.readouterr().err
    assert "matches 2 episodes" in err and "id: 2" in err and "id: 3" in err
    assert len(records(root, "Blog")) == 3


def test_text_that_matches_nothing_forgets_nothing(library, capsys):
    args, root = library
    assert main([*args, "forget", "Blog", "1", "Atlantide"]) == 1
    assert "No episode of 'Blog' is 'Atlantide'" in capsys.readouterr().err
    assert len(records(root, "Blog")) == 3
