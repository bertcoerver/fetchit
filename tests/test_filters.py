"""Tests for per-field filtering (fetchit.feeds.apply_filters)."""

from datetime import datetime, timezone

import pytest

from fetchit.feeds import FeedFilter, FeedItem, apply_filters

INTERVIEW = FeedItem(title="Episode 12: The Interview", description="A long talk")
RERUN = FeedItem(title="Episode 13: Special", description="This is a RERUN")
PLAIN = FeedItem(title="Episode 14", description="Something else")
ALL = [INTERVIEW, RERUN, PLAIN]


def _filter(field, op, *values):
    return (FeedFilter(field=field, op=op, values=values),)


def test_no_filters_passes_everything():
    assert apply_filters(ALL, ()) == ALL


def test_contains_keeps_matching_items():
    assert apply_filters(ALL, _filter("title", "contains", "Interview")) == [INTERVIEW]


def test_contains_is_any_of():
    result = apply_filters(ALL, _filter("title", "contains", "Interview", "Special"))
    assert result == [INTERVIEW, RERUN]


def test_contains_is_case_insensitive():
    assert apply_filters(ALL, _filter("title", "contains", "iNtErViEw")) == [INTERVIEW]


def test_excludes_drops_matching_items():
    result = apply_filters(ALL, _filter("description", "excludes", "rerun"))
    assert result == [INTERVIEW, PLAIN]


def test_excludes_is_none_of():
    result = apply_filters(ALL, _filter("description", "excludes", "rerun", "long"))
    assert result == [PLAIN]


def test_matches_uses_regex():
    result = apply_filters(ALL, _filter("title", "matches", r"^Episode 1[23]:"))
    assert result == [INTERVIEW, RERUN]


def test_multiple_filters_are_anded():
    filters = (
        FeedFilter(field="title", op="contains", values=("Episode",)),
        FeedFilter(field="description", op="excludes", values=("rerun",)),
        FeedFilter(field="title", op="matches", values=(r"\d{2}",)),
    )
    assert apply_filters(ALL, filters) == [INTERVIEW, PLAIN]


def test_missing_field_counts_as_empty():
    # No item has an "itunes_duration", so contains drops all and excludes keeps all.
    assert apply_filters(ALL, _filter("itunes_duration", "contains", "42")) == []
    assert apply_filters(ALL, _filter("itunes_duration", "excludes", "42")) == ALL


def test_min_and_max_compare_a_field_as_a_number():
    short = FeedItem(title="Clip", description="", extra={"duration_seconds": "120"})
    episode = FeedItem(title="Ep", description="", extra={"duration_seconds": "1200"})
    long = FeedItem(title="Live", description="", extra={"duration_seconds": "7200"})
    items = [short, episode, long]

    window = (
        FeedFilter(field="duration_seconds", op="min", values=("420",)),
        FeedFilter(field="duration_seconds", op="max", values=("1800",)),
    )
    assert apply_filters(items, window) == [episode]


def test_min_and_max_are_inclusive():
    item = FeedItem(title="Ep", description="", extra={"duration_seconds": "420"})
    assert apply_filters([item], _filter("duration_seconds", "min", "420")) == [item]
    assert apply_filters([item], _filter("duration_seconds", "max", "420")) == [item]


@pytest.mark.parametrize(
    "bound, kept",
    [("2400", True), ("40:00", True), ("00:40:00", True), ("39:59", False)],
)
def test_a_bound_can_be_written_as_a_clock_duration(bound, kept):
    item = FeedItem(title="Ep", description="", extra={"duration_seconds": "2400"})
    assert apply_filters([item], _filter("duration_seconds", "max", bound)) == (
        [item] if kept else []
    )


def test_a_field_can_be_written_as_a_clock_duration_too():
    # itunes:duration is usually h:mm:ss, and comparing it shouldn't need an
    # extract to turn it into seconds first.
    item = FeedItem(title="Ep", description="", extra={"itunes_duration": "01:20:00"})
    assert apply_filters([item], _filter("itunes_duration", "min", "3600")) == [item]
    assert apply_filters([item], _filter("itunes_duration", "max", "3600")) == []


def test_min_and_max_drop_what_is_not_a_number():
    missing = FeedItem(title="No duration", description="")
    prose = FeedItem(title="Ep", description="", extra={"duration_seconds": "a while"})

    assert apply_filters([missing, prose], _filter("duration_seconds", "min", "1")) == []
    assert apply_filters([missing, prose], _filter("duration_seconds", "max", "1")) == []


def test_published_weekday_names_the_day():
    days = [
        FeedItem(
            title=f"Day {day}",
            description="",
            published=datetime(2026, 7, 20 + day, tzinfo=timezone.utc),
        )
        for day in range(7)  # 2026-07-20 is a Monday.
    ]

    kept = apply_filters(
        days,
        _filter("published_weekday", "contains", "Tuesday", "Wednesday", "Friday"),
    )
    assert [item.title for item in kept] == ["Day 1", "Day 2", "Day 4"]


def test_published_weekday_is_empty_without_a_date():
    undated = FeedItem(title="Undated", description="")
    monday = _filter("published_weekday", "contains", "Monday")
    assert apply_filters([undated], monday) == []


def test_filters_can_read_extra_fields():
    long_ep = FeedItem(title="Long", description="", extra={"itunes_duration": "01:20:00"})
    short_ep = FeedItem(title="Short", description="", extra={"itunes_duration": "00:05:00"})
    result = apply_filters(
        [long_ep, short_ep], _filter("itunes_duration", "matches", r"^01:")
    )
    assert result == [long_ep]
