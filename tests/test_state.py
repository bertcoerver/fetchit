"""Tests for fetchit.state: what a feed is done with, kept beside the library."""

import json
import os

from fetchit.state import State


def test_an_empty_library_has_an_empty_state(tmp_path):
    state = State.load(str(tmp_path))
    assert not state.known("Feed")
    assert not state.seen("Feed", "a")


def test_marks_survive_a_save_and_a_load(tmp_path):
    state = State.load(str(tmp_path))
    state.mark("Feed", "a")
    state.mark("Feed", "b", "skipped: a Short")
    state.save()

    again = State.load(str(tmp_path))
    assert again.seen("Feed", "a") and again.seen("Feed", "b")
    assert not again.seen("Feed", "c")
    assert not again.seen("Other", "a")


def test_a_starting_point_makes_a_feed_known(tmp_path):
    from datetime import datetime, timezone

    when = datetime(2026, 10, 1, tzinfo=timezone.utc)
    state = State.load(str(tmp_path))
    state.set_after("Feed", when)
    state.save()
    again = State.load(str(tmp_path))
    assert again.known("Feed") and again.after("Feed") == when
    assert not again.seen("Feed", "a")


def test_artwork_is_remembered_with_when_it_was_checked(tmp_path):
    state = State.load(str(tmp_path))
    state.set_artwork("Feed", {"shows/Feed/poster.jpg": "abc"})
    state.save()
    again = State.load(str(tmp_path))
    assert again.artwork("Feed") == {"shows/Feed/poster.jpg": "abc"}
    assert again.artwork_checked("Feed") is not None


def test_an_unreadable_state_is_moved_aside_not_silently_lost(tmp_path, capsys):
    folder = tmp_path / ".fetchit"
    folder.mkdir()
    (folder / "state.json").write_text("{not json")

    state = State.load(str(tmp_path))

    assert not state.known("Feed")
    assert any(name.startswith("state.json.broken-") for name in os.listdir(folder))
    assert "could not be read" in capsys.readouterr().err


def test_save_writes_valid_json_in_place(tmp_path):
    state = State.load(str(tmp_path))
    state.mark("Feed", "a")
    state.save()
    path = tmp_path / ".fetchit" / "state.json"
    data = json.loads(path.read_text())
    assert "a" in data["feeds"]["Feed"]["seen"]
    assert [n for n in os.listdir(path.parent) if n.startswith(".state-")] == []
