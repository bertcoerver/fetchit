"""Tests for fetchit.quality: reading the setting, and choosing by it."""

import pytest

from fetchit.quality import MB, Quality, choose, parse_quality

# (height, megabytes) per rendition, best last by height.
RUNGS = [(216, 20), (360, 45), (720, 120), (1080, 260), (2160, 900)]


def _choose(quality, rungs=RUNGS):
    return choose(
        rungs,
        size=lambda r: r[1] * MB,
        rank=lambda r: r[0],
        height=lambda r: r[0],
        quality=quality,
    )


def test_default_takes_the_sharpest_up_to_1080p_in_one_part():
    assert _choose(Quality()) == ((1080, 260), 1)


def test_no_cap_takes_the_sharpest_there_is():
    assert _choose(Quality(max_height=None)) == ((2160, 900), 1)


def test_a_cap_below_everything_takes_the_shortest():
    assert _choose(Quality(max_height=100)) == ((216, 20), 1)


def test_a_budget_takes_the_fewest_parts_then_the_sharpest_in_them():
    # 20 MB fits one 95 MB part; so does 45; 120 would need two.
    assert _choose(Quality(max_height=None, part_bytes=95 * MB)) == ((360, 45), 1)


def test_a_budget_goes_to_more_parts_only_when_it_must():
    rungs = [(360, 105), (720, 180), (1080, 300)]
    # The smallest needs two parts; within two parts' 190 MB, 720p fits.
    assert _choose(Quality(max_height=None, part_bytes=95 * MB), rungs) == ((720, 180), 2)


def test_a_budget_is_applied_under_the_cap():
    assert _choose(Quality(max_height=360, part_bytes=500 * MB)) == ((360, 45), 1)


def test_too_big_for_the_most_parts_is_none():
    rungs = [(360, 500)]
    assert _choose(Quality(part_bytes=95 * MB, max_parts=4), rungs) is None


def test_nothing_to_choose_from_is_none():
    assert _choose(Quality(), []) is None


def test_parse_quality_defaults():
    assert parse_quality(None, "x") == Quality()


def test_parse_quality_reads_every_key():
    quality = parse_quality({"max_height": 720, "part_size_mb": 95, "max_parts": 3}, "x")
    assert quality == Quality(max_height=720, part_bytes=95 * MB, max_parts=3)


def test_parse_quality_keeps_what_it_is_not_told_from_the_base():
    base = Quality(max_height=720, part_bytes=95 * MB)
    assert parse_quality({"max_parts": 2}, "x", base) == Quality(720, 95 * MB, 2)


def test_parse_quality_null_switches_a_limit_off():
    base = Quality(max_height=720, part_bytes=95 * MB)
    quality = parse_quality({"max_height": None, "part_size_mb": None}, "x", base)
    assert quality.max_height is None and quality.part_bytes is None


@pytest.mark.parametrize(
    "raw",
    [
        "720p",
        {"height": 720},
        {"max_height": "720"},
        {"max_height": 0},
        {"part_size_mb": -1},
        {"part_size_mb": True},
        {"max_parts": 0},
    ],
)
def test_parse_quality_rejects_nonsense(raw):
    with pytest.raises(ValueError, match="feeds\\[0\\] quality"):
        parse_quality(raw, "feeds[0]")


# --- a floor under the picture ---------------------------------------------

LONG = [(216, 60), (360, 130), (720, 300), (1080, 700)]


def test_a_floor_costs_parts_rather_than_picture():
    floor = Quality(max_height=1080, min_height=360, part_bytes=95 * MB)
    assert _choose(floor) == ((360, 45), 1)
    # 360p needs two parts, and in two parts nothing better fits.
    assert _choose(floor, LONG) == ((360, 130), 2)
    # Without the floor the same video would have gone down to 216p for one.
    assert _choose(Quality(part_bytes=95 * MB), LONG) == ((216, 60), 1)


def test_within_the_parts_the_floor_asks_for_the_best_is_taken():
    rungs = [(216, 60), (360, 100), (540, 180), (720, 300)]
    assert _choose(Quality(min_height=360, part_bytes=95 * MB), rungs) == ((540, 180), 2)


def test_a_floor_above_everything_takes_the_tallest():
    assert _choose(Quality(min_height=720, part_bytes=95 * MB), [(216, 20), (360, 45)]) == ((360, 45), 1)


def test_a_floor_alone_changes_nothing():
    assert _choose(Quality(min_height=360)) == ((1080, 260), 1)


def test_however_many_parts_unless_max_parts_says_otherwise():
    rungs = [(360, 500)]
    assert _choose(Quality(part_bytes=95 * MB), rungs) == ((360, 500), 6)
    assert _choose(Quality(part_bytes=95 * MB, max_parts=4), rungs) is None


def test_parse_quality_reads_the_floor():
    quality = parse_quality({"max_height": 1080, "min_height": 360, "part_size_mb": 95}, "x")
    assert quality == Quality(max_height=1080, min_height=360, part_bytes=95 * MB)
    assert parse_quality({"part_size_mb": None, "min_height": None}, "x", quality) == Quality()
    assert parse_quality({"max_parts": None}, "x", Quality(max_parts=4)).max_parts is None


@pytest.mark.parametrize(
    "raw",
    [
        {"min_height": 1440},
        {"min_height": "360"},
        {"try_smaller_than_mb": 95},
        {"small": {"part_size_mb": 95}},
    ],
)
def test_parse_quality_rejects_a_floor_that_makes_no_sense_and_keys_it_does_not_know(raw):
    with pytest.raises(ValueError, match="feeds\\[0\\] quality"):
        parse_quality(raw, "feeds[0]")
