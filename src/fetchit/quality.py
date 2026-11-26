"""Which rendition of a video to download.

Every video source offers the same video at several qualities, and which one is
right depends on where it is going. A media server wants the sharpest one a
television will show; a chat app that refuses attachments over 100 MB wants the
sharpest one that will still arrive. So the choice is a setting, per feed or
for the whole library:

``max_height``
    The tallest picture worth having, 1080 by default. A source with nothing
    that short offers its shortest instead.
``part_size_mb``
    Off by default. Set, it switches to rssignal's rule — **the fewest parts of
    this size possible, then the best quality that fits in that many** — for a
    feed whose videos are also sent to Signal (``95`` fits its 100 MB limit
    with room for an estimate that ran low). Splitting the file into those
    parts is the sender's job; this only makes sure the file it is handed
    splits into as few as it can.
``max_parts``
    With ``part_size_mb``, the most parts worth having: a video whose smallest
    rendition needs more is not downloaded at all. 4 by default — about 380 MB
    at 95 MB a part, which is a two-hour documentary at 360p.

``small``
    Off by default. An object of the three settings above, for a **second,
    smaller copy** of every video, downloaded beside the first: the feed that
    goes to a media server at 1080p and to Signal too sets
    ``"small": {"part_size_mb": 95}`` and has a file for each. The copy is filed
    out of the media server's sight (see :mod:`fetchit.library`) and named in
    the item's record, where rssignal and db-podcast find it. Where both
    settings arrive at the same rendition there is nothing to download twice,
    and the item has the one file.

Sizes are each source's own estimates — exact where it reports a file size,
bitrate times duration where it doesn't — so they are decided before
downloading anything, and a dry run can say what a video would arrive at.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, TypeVar

T = TypeVar("T")

MB = 1024 * 1024

DEFAULT_MAX_HEIGHT = 1080
DEFAULT_MAX_PARTS = 4

QUALITY_KEYS = ("max_height", "part_size_mb", "max_parts", "small")


@dataclass(frozen=True)
class Quality:
    """How to choose between the renditions of a video. See the module docstring.

    ``part_bytes`` is ``part_size_mb`` in bytes, or ``None`` for no size budget.
    ``max_height`` ``None`` means no cap at all. ``small`` is the quality of
    the second, smaller copy, or ``None`` for no second copy.
    """

    max_height: int | None = DEFAULT_MAX_HEIGHT
    part_bytes: int | None = None
    max_parts: int = DEFAULT_MAX_PARTS
    small: Quality | None = None


def parse_quality(
    raw: object, where: str, base: Quality = Quality(), *, _keys: tuple[str, ...] = QUALITY_KEYS,
    _name: str = "quality",
) -> Quality:
    """Read a ``quality`` config object on top of ``base``, or raise ValueError.

    Keys left out keep ``base``'s value, so a feed can override one setting of
    the library-wide default without restating the others. ``null`` for
    ``max_height`` or ``part_size_mb`` switches that limit off, and for
    ``small`` it switches the second copy off.
    """
    if raw is None:
        return base
    if not isinstance(raw, dict):
        raise ValueError(
            f"{where} {_name} must be an object with any of {list(_keys)}."
        )
    unknown = set(raw) - set(_keys)
    if unknown:
        raise ValueError(
            f"{where} {_name} has unknown key(s) {sorted(unknown)}; expected "
            f"{list(_keys)}."
        )

    max_height = base.max_height
    if "max_height" in raw:
        max_height = _whole(raw["max_height"], f"{where} {_name}.max_height", nullable=True)

    part_bytes = base.part_bytes
    if "part_size_mb" in raw:
        value = raw["part_size_mb"]
        if value is None:
            part_bytes = None
        elif isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise ValueError(
                f"{where} {_name}.part_size_mb must be a positive number of MB, or null."
            )
        else:
            part_bytes = int(value * MB)

    max_parts = base.max_parts
    if "max_parts" in raw:
        max_parts = _whole(raw["max_parts"], f"{where} {_name}.max_parts")

    small = base.small
    if "small" in raw:
        if raw["small"] is None:
            small = None
        else:
            # Read on top of the library's own small copy, where it has one;
            # otherwise it starts from nothing taller than the main copy.
            small = parse_quality(
                raw["small"],
                where,
                small or Quality(max_height=max_height),
                _keys=QUALITY_KEYS[:-1],
                _name="quality.small",
            )

    return Quality(
        max_height=max_height, part_bytes=part_bytes, max_parts=max_parts, small=small
    )


def _whole(value: object, what: str, *, nullable: bool = False) -> int | None:
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        extra = ", or null" if nullable else ""
        raise ValueError(f"{what} must be a whole number, 1 or more{extra}.")
    return value


def parts_needed(size: int, max_bytes: int) -> int:
    """How many parts of at most ``max_bytes`` a ``size``-byte file needs."""
    return max(1, math.ceil(size / max_bytes))


def choose(
    candidates: Sequence[T],
    *,
    size: Callable[[T], int],
    rank: Callable[[T], Any],
    height: Callable[[T], int],
    quality: Quality,
) -> tuple[T, int] | None:
    """Pick ``(candidate, parts)`` under ``quality``.

    ``size`` estimates a candidate's bytes, ``rank`` orders them best-last, and
    ``height`` is the picture's height in pixels (0 for one that doesn't say,
    which no cap rules out).

    Without a size budget the answer is the best candidate under the height cap,
    in one part. With one it is the fewest parts any of those fits in, then the
    best that fits in that many. ``None`` means even the smallest needs more
    than ``max_parts``; the caller says why in its own words, since it knows
    what the candidates were.
    """
    if not candidates:
        return None

    pool = list(candidates)
    if quality.max_height is not None:
        under = [c for c in pool if height(c) <= quality.max_height]
        if not under:
            # Nothing that short: the next best thing is the shortest there is.
            lowest = min(height(c) for c in pool)
            under = [c for c in pool if height(c) == lowest]
        pool = under

    if quality.part_bytes is None:
        return max(pool, key=rank), 1

    parts = parts_needed(min(size(c) for c in pool), quality.part_bytes)
    if parts > quality.max_parts:
        return None
    budget = parts * quality.part_bytes
    fitting = [c for c in pool if size(c) <= budget]
    return max(fitting, key=rank), parts


def in_parts(parts: int) -> str:
    """The suffix a dry run adds to a plan's description: ``", in 2 parts"``."""
    return f", in {parts} parts" if parts > 1 else ""


def too_big(what: str, smallest: int, quality: Quality):
    """The :class:`~fetchit.media.VideoTooBig` for ``what`` whose smallest option is ``smallest``."""
    from .media import VideoTooBig

    part = (quality.part_bytes or 0) / MB
    return VideoTooBig(
        f"{what} is too big: {smallest / MB:.0f} MB is over "
        f"{quality.max_parts} parts of {part:.0f} MB"
    )
