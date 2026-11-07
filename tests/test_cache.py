"""Tests for the between-runs speed cache.

The property that matters most here is the one in the module's own docstring:
nothing in the cache decides what gets sent. So these check that a miss is
indistinguishable from an empty cache, that a corrupt file is treated as one, and
that failing to write is not an error — every path where the cache's answer is
"ask again", which is always safe.
"""

from __future__ import annotations

import json
import time

from fetchit import cache


def _next_run() -> None:
    """Put the module back how a freshly started process would find it.

    Not the same as :func:`fetchit.cache.clear`, which wipes what is remembered
    *and* means it — it marks the cache dirty so the wipe reaches the file. These
    tests want the opposite: nothing in memory, nothing pending, and the file on
    disk left exactly as the previous run wrote it.
    """
    cache._data = None  # noqa: SLF001
    cache._dirty = False  # noqa: SLF001


def test_a_value_survives_a_save_and_reload(tmp_path, monkeypatch):
    monkeypatch.setenv(cache.CACHE_ENV, str(tmp_path / "c.json"))
    cache.clear()
    cache.put("ns", "key", "value")
    cache.save()

    _next_run()
    assert cache.get("ns", "key") == "value"


def test_a_missing_key_is_none(tmp_path, monkeypatch):
    monkeypatch.setenv(cache.CACHE_ENV, str(tmp_path / "c.json"))
    cache.clear()
    assert cache.get("ns", "nothing") is None


def test_an_entry_past_its_max_age_is_a_miss(tmp_path, monkeypatch):
    monkeypatch.setenv(cache.CACHE_ENV, str(tmp_path / "c.json"))
    cache.clear()
    cache.put("ns", "key", "value")

    assert cache.get("ns", "key", max_age=60) == "value"
    assert cache.get("ns", "key", max_age=-1) is None
    # No max_age at all means the answer never goes stale.
    assert cache.get("ns", "key") == "value"


def test_a_stale_entry_is_a_miss_not_an_error(tmp_path, monkeypatch):
    path = tmp_path / "c.json"
    monkeypatch.setenv(cache.CACHE_ENV, str(path))
    path.write_text(
        json.dumps({"ns": {"key": {"value": "old", "stored_at": time.time() - 9999}}})
    )
    _next_run()

    assert cache.get("ns", "key", max_age=10) is None


def test_a_corrupt_file_reads_as_an_empty_cache(tmp_path, monkeypatch):
    path = tmp_path / "c.json"
    monkeypatch.setenv(cache.CACHE_ENV, str(path))
    path.write_text("{not json at all")
    _next_run()

    # The point: garbage on disk costs a slow run, never a failed one.
    assert cache.get("ns", "key") is None
    cache.put("ns", "key", "fresh")
    assert cache.get("ns", "key") == "fresh"


def test_saving_somewhere_unwritable_is_not_an_error(tmp_path, monkeypatch):
    # A file where the cache expects a directory: makedirs cannot win here.
    wall = tmp_path / "wall"
    wall.write_text("")
    monkeypatch.setenv(cache.CACHE_ENV, str(wall / "sub" / "c.json"))
    cache.clear()
    cache.put("ns", "key", "value")

    cache.save()  # must not raise


def test_save_writes_nothing_when_nothing_changed(tmp_path, monkeypatch):
    path = tmp_path / "c.json"
    monkeypatch.setenv(cache.CACHE_ENV, str(path))
    _next_run()

    cache.get("ns", "key")
    cache.save()

    assert not path.exists()


def test_a_dict_value_round_trips(tmp_path, monkeypatch):
    """Durations are stored as a mapping, so the value need not be a scalar."""
    monkeypatch.setenv(cache.CACHE_ENV, str(tmp_path / "c.json"))
    cache.clear()
    cache.put("ns", "channel", {"abc": 61.0, "def": 3600.0})
    cache.save()
    cache._data = None  # noqa: SLF001

    assert cache.get("ns", "channel") == {"abc": 61.0, "def": 3600.0}


def test_the_path_honours_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv(cache.CACHE_ENV, str(tmp_path / "mine.json"))
    assert cache.path() == str(tmp_path / "mine.json")

    monkeypatch.delenv(cache.CACHE_ENV, raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    assert cache.path() == str(tmp_path / "fetchit" / "cache.json")
