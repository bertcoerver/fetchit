"""Tests for fetchit.youtube (yt-dlp is monkeypatched — it is never run).

The format list is a trimmed copy of the shape ``yt-dlp -J`` really reports:
separate video and audio above 360p, one progressive rung left over, and VP9
duplicates of the resolutions that also exist in H.264. Sizes are rounded but
the relationships between them are the ones that decide the outcome.
"""

import json
import os
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from fetchit import cache, download, feeds, youtube
from fetchit.feeds import FeedError, FeedItem, SourceBlocked
from fetchit.media import VideoTooShort

from fetchit.quality import Quality


def _budget(part_bytes):
    """rssignal's rule: the fewest parts of ``part_bytes``, then the sharpest."""
    return Quality(max_height=None, part_bytes=part_bytes)


# What most of these tests were written against, and the rule that exercises
# the most of the choosing.
BUDGET = _budget(95 * 1024 * 1024)

VIDEO_ID = "BFcjfKZ0BeI"
WATCH = f"https://www.youtube.com/watch?v={VIDEO_ID}"
DURATION = 600

AUDIO = {
    "format_id": "140",
    "ext": "m4a",
    "vcodec": "none",
    "acodec": "mp4a.40.2",
    "filesize": 9_700_000,
}
FORMATS = [
    {**AUDIO},
    # Opus: fine for the web, wrong for an mp4 going to Signal.
    {
        "format_id": "251",
        "ext": "webm",
        "vcodec": "none",
        "acodec": "opus",
        "filesize": 6_000_000,
    },
    {
        "format_id": "18",
        "ext": "mp4",
        "vcodec": "avc1.42001E",
        "acodec": "mp4a.40.2",
        "width": 640,
        "height": 360,
        "filesize": 30_000_000,
    },
    {
        "format_id": "134",
        "ext": "mp4",
        "vcodec": "avc1.4d401e",
        "acodec": "none",
        "width": 640,
        "height": 360,
        "filesize": 20_000_000,
    },
    {
        "format_id": "135",
        "ext": "mp4",
        "vcodec": "avc1.4d401f",
        "acodec": "none",
        "width": 854,
        "height": 480,
        "filesize": 40_000_000,
    },
    {
        "format_id": "136",
        "ext": "mp4",
        "vcodec": "avc1.4d401f",
        "acodec": "none",
        "width": 1280,
        "height": 720,
        "filesize": 85_000_000,
    },
    {
        "format_id": "137",
        "ext": "mp4",
        "vcodec": "avc1.640028",
        "acodec": "none",
        "width": 1920,
        "height": 1080,
        "filesize": 200_000_000,
    },
    # Small enough to win on size alone — and must still lose, because it is VP9.
    {
        "format_id": "248",
        "ext": "webm",
        "vcodec": "vp9",
        "acodec": "none",
        "width": 1920,
        "height": 1080,
        "filesize": 30_000_000,
    },
]

INFO = {"id": VIDEO_ID, "title": "A video", "duration": DURATION, "formats": FORMATS}


@pytest.fixture(autouse=True)
def _signed_out(monkeypatch):
    """Whoever runs the tests may have cookies configured; the tests do not."""
    monkeypatch.delenv(youtube.COOKIES_ENV, raising=False)
    monkeypatch.delenv(youtube.COOKIES_FROM_BROWSER_ENV, raising=False)


def _patch_ytdlp(monkeypatch, *, info=None, returncode=0, stderr="", payload=b"video"):
    """Stand in for the yt-dlp binary, recording every command it was given."""
    calls = []
    info = INFO if info is None else info

    monkeypatch.setattr(youtube.shutil, "which", lambda name: f"/usr/bin/{name}")

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if "-J" in cmd:
            return subprocess.CompletedProcess(cmd, returncode, json.dumps(info), stderr)
        if payload is not None and "--output" in cmd:
            template = cmd[cmd.index("--output") + 1]
            with open(template.replace("%(ext)s", "mp4"), "wb") as fh:
                fh.write(payload)
        return subprocess.CompletedProcess(cmd, returncode, "", stderr)

    monkeypatch.setattr(youtube.subprocess, "run", fake_run)
    return calls


def _item(link=WATCH):
    return FeedItem(title="A video", description="d", link=link)


# --- link and channel detection -------------------------------------------


@pytest.mark.parametrize(
    "link, expected",
    [
        (WATCH, VIDEO_ID),
        (f"https://youtu.be/{VIDEO_ID}", VIDEO_ID),
        (f"https://www.youtube.com/shorts/{VIDEO_ID}", VIDEO_ID),
        (f"https://m.youtube.com/watch?v={VIDEO_ID}", VIDEO_ID),
        (f"https://www.youtube.com/watch?list=PL1&v={VIDEO_ID}", VIDEO_ID),
        (f"https://www.youtube.com/watch?v={VIDEO_ID}&t=42s", VIDEO_ID),
        ("https://www.youtube.com/@veritasium", None),
        ("https://vimeo.com/123456789", None),
        (None, None),
    ],
)
def test_youtube_video_id(link, expected):
    assert youtube.youtube_video_id(link) == expected


def test_youtube_feed_url_rewrites_a_channel_id_without_asking_anyone(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("the id is already in the url; nothing to look up")

    monkeypatch.setattr(youtube.subprocess, "run", fail)

    assert youtube.youtube_feed_url(
        "https://www.youtube.com/channel/UCHnyfMqiRRG1u-2MsSQLbXA/videos"
    ) == (
        "https://www.youtube.com/feeds/videos.xml?channel_id=UCHnyfMqiRRG1u-2MsSQLbXA"
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/@veritasium",
        "https://www.youtube.com/@veritasium/videos",
        "https://www.youtube.com/c/veritasium",
        "https://www.youtube.com/user/1veritasium",
    ],
)
def test_youtube_feed_url_asks_ytdlp_for_a_named_channel(monkeypatch, url):
    calls = _patch_ytdlp(monkeypatch, info={"channel_id": "UCHnyfMqiRRG1u-2MsSQLbXA"})

    assert youtube.youtube_feed_url(url).endswith("UCHnyfMqiRRG1u-2MsSQLbXA")

    cmd = calls[0]
    assert "-J" in cmd and "--flat-playlist" in cmd
    # One item, because it is the channel's own fields that are wanted.
    assert cmd[cmd.index("--playlist-items") + 1] == "1"


def test_youtube_feed_url_leaves_the_feed_itself_alone():
    feed = "https://www.youtube.com/feeds/videos.xml?channel_id=UCHnyfMqiRRG1u-2MsSQLbXA"
    assert youtube.youtube_feed_url(feed) is None


@pytest.mark.parametrize(
    "url",
    [WATCH, "https://waitbutwhy.com/feed", "https://www.youtube.com/", None],
)
def test_youtube_feed_url_ignores_what_is_not_a_channel(url):
    assert youtube.youtube_feed_url(url) is None


def test_youtube_feed_url_without_an_id_raises(monkeypatch):
    _patch_ytdlp(monkeypatch, info={"title": "some channel"})

    with pytest.raises(FeedError, match="Could not work out the channel id"):
        youtube.youtube_feed_url("https://www.youtube.com/@veritasium")


# --- durations ------------------------------------------------------------

CHANNEL_FEED = (
    "https://www.youtube.com/feeds/videos.xml?channel_id=UCHnyfMqiRRG1u-2MsSQLbXA"
)
LISTING = {
    "entries": [
        {"id": VIDEO_ID, "duration": 1200.0},
        {"id": "aaaaaaaaaaa", "duration": 61.0},
    ]
}


def test_with_durations_puts_the_channel_listing_on_the_items(monkeypatch):
    calls = _patch_ytdlp(monkeypatch, info=LISTING)
    items = [_item(), _item(link="https://youtu.be/aaaaaaaaaaa")]

    annotated = youtube.with_durations(CHANNEL_FEED, items)

    assert [item.extra["duration_seconds"] for item in annotated] == ["1200", "61"]
    # One request for the whole feed, not one per video.
    cmd = calls[0]
    assert len(calls) == 1 and "--flat-playlist" in cmd
    assert cmd[-1] == (
        "https://www.youtube.com/channel/UCHnyfMqiRRG1u-2MsSQLbXA/videos"
    )


def test_with_durations_matches_on_video_id_not_position(monkeypatch):
    _patch_ytdlp(monkeypatch, info=LISTING)
    items = [_item(link="https://youtu.be/aaaaaaaaaaa"), _item()]

    annotated = youtube.with_durations(CHANNEL_FEED, items)

    assert [item.extra["duration_seconds"] for item in annotated] == ["61", "1200"]


def test_with_durations_follows_a_nested_listing(monkeypatch):
    _patch_ytdlp(monkeypatch, info={"entries": [{"entries": LISTING["entries"]}]})

    annotated = youtube.with_durations(CHANNEL_FEED, [_item()])

    assert annotated[0].extra["duration_seconds"] == "1200"


def test_with_durations_leaves_an_unlisted_video_alone(monkeypatch):
    _patch_ytdlp(monkeypatch, info={"entries": []})
    item = _item()

    assert youtube.with_durations(CHANNEL_FEED, [item]) == [item]


def test_with_durations_ignores_a_feed_that_is_not_a_channel(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("nothing to look up for a feed with no channel in it")

    monkeypatch.setattr(youtube.subprocess, "run", fail)
    items = [_item()]

    assert youtube.with_durations("https://waitbutwhy.com/feed", items) == items


def test_with_durations_survives_a_failed_lookup(monkeypatch, capsys):
    _patch_ytdlp(monkeypatch, info={}, returncode=1, stderr="yt-dlp: unavailable")
    items = [_item()]

    assert youtube.with_durations(CHANNEL_FEED, items) == items
    assert "duration_seconds" in capsys.readouterr().err


# --- quality choice -------------------------------------------------------


def test_resolve_takes_the_sharpest_pair_that_fits(monkeypatch):
    calls = _patch_ytdlp(monkeypatch)

    plan = youtube.resolve(_item(), quality=BUDGET)

    assert calls[0][-1] == WATCH
    # 720p (85 MB) plus the audio (9.7 MB) is 94.7 MB and fits; 1080p doesn't.
    assert plan.selector == "136+140"
    assert plan.resolution == "1280x720"
    assert plan.describe() == "1280x720, ~90 MB"


def test_resolve_never_picks_vp9(monkeypatch):
    """The VP9 1080p rung is the smallest of the lot and must still lose."""
    _patch_ytdlp(monkeypatch)

    plan = youtube.resolve(_item(), quality=_budget(200 * 1024 * 1024))

    assert plan.resolution == "1920x1080"
    assert plan.selector == "137+140"


def test_resolve_drops_a_rung_for_a_smaller_budget(monkeypatch):
    _patch_ytdlp(monkeypatch)

    plan = youtube.resolve(_item(), quality=_budget(60 * 1024 * 1024))

    assert plan.selector == "135+140"


def test_resolve_splits_rather_than_skips_when_nothing_fits_whole(monkeypatch):
    _patch_ytdlp(monkeypatch)

    # 25 MB is under even 360p (29.7 MB with its audio), so two parts — and two
    # parts' 50 MB is room for 480p (49.7 MB) but not 720p.
    plan = youtube.resolve(_item(), quality=_budget(25_000_000))

    assert plan.selector == "135+140"
    assert plan.parts == 2
    assert plan.describe().endswith(", in 2 parts")


def test_resolve_muxes_the_original_language_not_the_smallest_dub(monkeypatch):
    """A dubbed video lists each language as its own stream, a few bytes apart."""
    dubs = [
        {**AUDIO, "format_id": "140-0", "language": "hi", "language_preference": -1,
         "filesize": AUDIO["filesize"] - 300},
        {**AUDIO, "format_id": "140-1", "language": "es", "language_preference": -1,
         "filesize": AUDIO["filesize"] - 100},
        {**AUDIO, "format_id": "140-2", "language": "en", "language_preference": 10},
    ]
    formats = dubs + [f for f in FORMATS if f["format_id"] != "140"]
    _patch_ytdlp(monkeypatch, info={**INFO, "formats": formats})

    plan = youtube.resolve(_item(), quality=BUDGET)

    assert plan.selector == "136+140-2"


def test_resolve_falls_back_to_a_progressive_format(monkeypatch):
    """Nothing to mux: no separate H.264 video, no AAC audio, just itag 18."""
    formats = [f for f in FORMATS if f["format_id"] in ("18", "251", "248")]
    _patch_ytdlp(monkeypatch, info={**INFO, "formats": formats})

    plan = youtube.resolve(_item(), quality=BUDGET)

    assert plan.selector == "18"
    assert plan.resolution == "640x360"


def test_resolve_estimates_from_bitrate_when_no_size_is_given(monkeypatch):
    formats = [
        {**AUDIO, "filesize": None, "tbr": 128},
        {
            "format_id": "134",
            "vcodec": "avc1.4d401e",
            "acodec": "none",
            "width": 640,
            "height": 360,
            "tbr": 600,
        },
    ]
    _patch_ytdlp(monkeypatch, info={**INFO, "formats": formats})

    plan = youtube.resolve(_item(), quality=BUDGET)

    # (600 + 128) kbps over 600s, with the 10% margin, is about 57 MB.
    assert plan.selector == "134+140"
    assert 55 < plan.estimated_bytes / 1024 / 1024 < 60


def test_resolve_raises_when_nothing_fits(monkeypatch):
    _patch_ytdlp(monkeypatch)

    with pytest.raises(FeedError, match="too big"):
        youtube.resolve(_item(), quality=_budget(1024))


def test_resolve_raises_when_no_format_is_usable(monkeypatch):
    formats = [f for f in FORMATS if f["format_id"] in ("251", "248")]
    _patch_ytdlp(monkeypatch, info={**INFO, "formats": formats})

    with pytest.raises(FeedError, match="No usable format"):
        youtube.resolve(_item(), quality=BUDGET)


def test_resolve_without_formats_raises(monkeypatch):
    _patch_ytdlp(monkeypatch, info={"id": VIDEO_ID, "duration": 600, "formats": []})

    with pytest.raises(FeedError, match="no formats"):
        youtube.resolve(_item(), quality=BUDGET)


def test_resolve_rejects_a_non_youtube_link():
    with pytest.raises(FeedError, match="Not a YouTube link"):
        youtube.resolve(_item("https://example.com/post"), quality=BUDGET)


# --- Shorts ---------------------------------------------------------------


def test_resolve_refuses_a_short(monkeypatch):
    _patch_ytdlp(monkeypatch, info={**INFO, "duration": 47})

    with pytest.raises(VideoTooShort, match="47s"):
        youtube.resolve(_item(), quality=BUDGET)


def test_resolve_keeps_a_video_just_over_the_floor(monkeypatch):
    _patch_ytdlp(monkeypatch, info={**INFO, "duration": youtube.MIN_VIDEO_SECONDS})

    assert youtube.resolve(_item(), quality=BUDGET).selector == "136+140"


# --- downloading ----------------------------------------------------------


def test_fetch_runs_ytdlp_and_returns_the_file(monkeypatch, tmp_path):
    calls = _patch_ytdlp(monkeypatch)
    plan = youtube.resolve(_item(), quality=BUDGET)

    path = plan.fetch(str(tmp_path), timeout=900)

    assert os.path.basename(path) == "video.mp4"
    with open(path, "rb") as fh:
        assert fh.read() == b"video"

    cmd = calls[-1]
    assert cmd[0] == "/usr/bin/yt-dlp"
    assert cmd[cmd.index("--format") + 1] == "136+140"
    assert cmd[cmd.index("--merge-output-format") + 1] == "mp4"
    assert cmd[cmd.index("--output") + 1] == os.path.join(str(tmp_path), "video.%(ext)s")
    assert cmd[-1] == WATCH


def test_fetch_takes_the_biggest_file_when_the_mux_left_parts_behind(
    monkeypatch, tmp_path
):
    _patch_ytdlp(monkeypatch, payload=None)
    plan = youtube.resolve(_item(), quality=BUDGET)

    (tmp_path / "video.f136.mp4").write_bytes(b"x" * 50)
    (tmp_path / "video.mp4").write_bytes(b"x" * 500)
    (tmp_path / "video.f140.m4a").write_bytes(b"x" * 10)

    assert plan.fetch(str(tmp_path), timeout=900).endswith("video.mp4")


def test_fetch_without_output_raises(monkeypatch, tmp_path):
    _patch_ytdlp(monkeypatch, payload=None)
    plan = youtube.resolve(_item(), quality=BUDGET)

    with pytest.raises(FeedError, match="produced no video"):
        plan.fetch(str(tmp_path), timeout=900)


def test_ytdlp_failure_reports_its_last_line(monkeypatch, tmp_path):
    _patch_ytdlp(monkeypatch)
    plan = youtube.resolve(_item(), quality=BUDGET)
    _patch_ytdlp(
        monkeypatch,
        returncode=1,
        stderr="WARNING: falling back\nERROR: Sign in to confirm you're not a bot\n",
    )

    with pytest.raises(FeedError, match="not a bot"):
        plan.fetch(str(tmp_path), timeout=900)


def test_ytdlp_failure_on_the_bot_check_says_how_to_sign_in(monkeypatch, tmp_path):
    _patch_ytdlp(monkeypatch)
    plan = youtube.resolve(_item(), quality=BUDGET)
    _patch_ytdlp(
        monkeypatch,
        returncode=1,
        stderr="ERROR: [youtube] BFcjfKZ0BeI: Sign in to confirm you’re not a bot.\n",
    )

    with pytest.raises(FeedError) as excinfo:
        plan.fetch(str(tmp_path), timeout=900)

    assert youtube.COOKIES_FROM_BROWSER_ENV in str(excinfo.value)
    assert youtube.COOKIES_ENV in str(excinfo.value)


def test_a_cookies_file_is_passed_to_every_ytdlp_call(monkeypatch, tmp_path):
    cookies = tmp_path / "cookies.txt"
    cookies.write_text("# Netscape HTTP Cookie File\n")
    monkeypatch.setenv(youtube.COOKIES_ENV, str(cookies))
    calls = _patch_ytdlp(monkeypatch)

    plan = youtube.resolve(_item(), quality=BUDGET)
    plan.fetch(str(tmp_path), timeout=900)

    assert len(calls) == 2
    for cmd in calls:
        assert cmd[cmd.index("--cookies") + 1] == str(cookies)


def test_a_browser_is_passed_when_there_is_no_cookies_file(monkeypatch):
    monkeypatch.setenv(youtube.COOKIES_FROM_BROWSER_ENV, "firefox")
    calls = _patch_ytdlp(monkeypatch)

    youtube.resolve(_item(), quality=BUDGET)

    assert calls[0][calls[0].index("--cookies-from-browser") + 1] == "firefox"


def test_a_cookies_file_wins_over_a_browser(monkeypatch, tmp_path):
    cookies = tmp_path / "cookies.txt"
    cookies.write_text("# Netscape HTTP Cookie File\n")
    monkeypatch.setenv(youtube.COOKIES_ENV, str(cookies))
    monkeypatch.setenv(youtube.COOKIES_FROM_BROWSER_ENV, "firefox")
    calls = _patch_ytdlp(monkeypatch)

    youtube.resolve(_item(), quality=BUDGET)

    assert "--cookies" in calls[0]
    assert "--cookies-from-browser" not in calls[0]


def test_a_cookies_file_that_is_not_there_raises(monkeypatch, tmp_path):
    monkeypatch.setenv(youtube.COOKIES_ENV, str(tmp_path / "nope.txt"))
    _patch_ytdlp(monkeypatch)

    with pytest.raises(FeedError, match="not a file"):
        youtube.resolve(_item(), quality=BUDGET)


def test_no_cookies_configured_signs_nothing_in(monkeypatch):
    calls = _patch_ytdlp(monkeypatch)

    youtube.resolve(_item(), quality=BUDGET)

    assert "--cookies" not in calls[0]
    assert "--cookies-from-browser" not in calls[0]


def test_ytdlp_timeout_raises(monkeypatch, tmp_path):
    _patch_ytdlp(monkeypatch)
    plan = youtube.resolve(_item(), quality=BUDGET)

    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 900)

    monkeypatch.setattr(youtube.subprocess, "run", fake_run)

    with pytest.raises(FeedError, match="timed out"):
        plan.fetch(str(tmp_path), timeout=900)


def test_without_ytdlp_raises(monkeypatch):
    monkeypatch.setattr(youtube.shutil, "which", lambda name: None)

    with pytest.raises(FeedError, match="yt-dlp is needed"):
        youtube.resolve(_item(), quality=BUDGET)


def test_unreadable_ytdlp_output_raises(monkeypatch):
    monkeypatch.setattr(youtube.shutil, "which", lambda name: "/usr/bin/yt-dlp")
    monkeypatch.setattr(
        youtube.subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "not json", ""),
    )

    with pytest.raises(FeedError, match="Could not read yt-dlp's output"):
        youtube.resolve(_item(), quality=BUDGET)


# --- as a feed ------------------------------------------------------------


def test_parse_feed_rewrites_a_channel_url(monkeypatch):
    """A channel url reaches feedparser as the Atom feed, not as the page."""
    from fetchit import feeds

    _patch_ytdlp(monkeypatch, info={"channel_id": "UCHnyfMqiRRG1u-2MsSQLbXA"})

    seen = []

    class FakeParsed:
        bozo = False
        entries = []
        feed = {}

    def fake_parse(url, **kwargs):
        seen.append(url)
        return FakeParsed()

    monkeypatch.setattr(feeds.feedparser, "parse", fake_parse)

    feeds.parse_feed(
        feeds.FeedConfig(url="https://www.youtube.com/@veritasium", name="Veritasium")
    )

    assert seen == [
        "https://www.youtube.com/feeds/videos.xml?channel_id=UCHnyfMqiRRG1u-2MsSQLbXA"
    ]


def test_is_youtube_url_matches_only_youtube_hosts():
    assert youtube.is_youtube_url("https://www.youtube.com/@veritasium")
    assert youtube.is_youtube_url(
        "https://www.youtube.com/feeds/videos.xml?channel_id=UCHnyfMqiRRG1u-2MsSQLbXA"
    )
    assert youtube.is_youtube_url("https://youtu.be/BFcjfKZ0BeI")
    # A lookalike domain must not be taken for the real one.
    assert not youtube.is_youtube_url("https://youtube.com.evil.test/@a")
    assert not youtube.is_youtube_url("https://api.arte.tv/x")
    assert not youtube.is_youtube_url(None)


def test_check_available_passes_when_youtube_answers(monkeypatch):
    monkeypatch.setattr(download, "reachable", lambda url, **kwargs: True)
    youtube.check_available("https://www.youtube.com/@veritasium")


def test_check_available_raises_when_youtube_is_blocked(monkeypatch):
    monkeypatch.setattr(download, "reachable", lambda url, **kwargs: False)

    with pytest.raises(SourceBlocked):
        youtube.check_available("https://www.youtube.com/@veritasium")


def test_check_available_never_probes_for_a_non_youtube_url(monkeypatch):
    def fail(url, **kwargs):
        raise AssertionError("a non-YouTube feed must not pay for the probe")

    monkeypatch.setattr(download, "reachable", fail)
    youtube.check_available("https://waitbutwhy.com/feed")


def test_a_blocked_youtube_feed_never_reaches_yt_dlp(monkeypatch):
    """The point of the probe: no minute-long yt-dlp timeouts behind a block."""
    monkeypatch.setattr(download, "reachable", lambda url, **kwargs: False)

    def fail(*args, **kwargs):
        raise AssertionError("yt-dlp must not run when YouTube is unreachable")

    monkeypatch.setattr(youtube, "_run_ytdlp", fail)
    monkeypatch.setattr(feeds.feedparser, "parse", fail)

    with pytest.raises(SourceBlocked):
        feeds.parse_feed(
            feeds.FeedConfig(url="https://www.youtube.com/@veritasium", name="V")
        )


# --- the back catalogue, for an episodic feed ------------------------------

ARCHIVE_CHANNEL = "UC3XTzVzaHQEd30rQbuvCtTQ"
ARCHIVE_FEED = f"https://www.youtube.com/feeds/videos.xml?channel_id={ARCHIVE_CHANNEL}"

# Newest first, the order the uploads playlist reports. Ids are stand-ins of the
# right shape; the real ones are eleven characters of the same alphabet.
CATALOGUE = [f"vid{n:08d}" for n in range(5, 0, -1)]

# When each went up, oldest to newest — what one --print costs a run each.
DATES = {
    f"vid{n:08d}": 1_400_000_000 + n * 86_400 for n in range(1, 6)
}


def _patch_archive(monkeypatch, *, catalogue=None, dated=None, listing_error=None):
    """Answer both yt-dlp calls the archive makes, and count them.

    ``dated`` limits which videos will give up a timestamp; anything outside it
    answers "NA", the way a private or removed video does.
    """
    catalogue = CATALOGUE if catalogue is None else catalogue
    dated = DATES if dated is None else dated
    asked = []

    def fake_json(args, *, timeout):
        if listing_error is not None:
            raise FeedError(listing_error)
        asked.append("listing")
        return {
            "entries": [
                {"id": vid, "title": f"Title {vid}", "duration": 1800}
                for vid in catalogue
            ]
        }

    def fake_run(args, *, timeout):
        asked.append(args[-1])
        video_id = args[-1].rsplit("=", 1)[-1]
        stamp = dated.get(video_id)
        return subprocess.CompletedProcess(
            args, 0, f"{stamp}\n" if stamp else "NA\n", ""
        )

    monkeypatch.setattr(youtube, "_ytdlp_json", fake_json)
    monkeypatch.setattr(youtube, "_run_ytdlp", fake_run)
    return asked


def test_archive_puts_the_whole_channel_in_front_oldest_first(monkeypatch):
    _patch_archive(monkeypatch)

    items = youtube.archive_items(ARCHIVE_FEED, [], feed_name="Show")

    assert [i.title for i in items] == [f"Title vid{n:08d}" for n in range(1, 6)]
    assert [i.published.timestamp() for i in items] == sorted(DATES.values())
    assert items[0].link == "https://www.youtube.com/watch?v=vid00000001"
    assert items[0].extra["duration_seconds"] == "1800"


def test_archive_asks_the_uploads_playlist_not_the_videos_tab(monkeypatch):
    """The /videos tab does not stay in order past its first page."""
    seen = {}

    def fake_json(args, *, timeout):
        seen["url"] = args[-1]
        return {"entries": [{"id": CATALOGUE[0], "title": "T", "duration": 60}]}

    monkeypatch.setattr(youtube, "_ytdlp_json", fake_json)
    youtube.uploads(ARCHIVE_CHANNEL)

    assert seen["url"] == f"https://www.youtube.com/playlist?list=UU{ARCHIVE_CHANNEL[2:]}"


def test_a_video_that_has_no_date_is_dropped_rather_than_waited_for(monkeypatch):
    """Asked and answered with nothing: private or removed, so not in the queue."""
    _patch_archive(
        monkeypatch, dated={k: v for k, v in DATES.items() if k != "vid00000003"}
    )

    items = youtube.archive_items(ARCHIVE_FEED, [], feed_name="Show")

    assert [i.title for i in items] == [
        "Title vid00000001",
        "Title vid00000002",
        "Title vid00000004",
        "Title vid00000005",
    ]


def test_archive_stops_where_it_could_not_ask(monkeypatch, capsys):
    """The frontier rule: never step over a video that may be dated next run.

    Stepping over it would send the ones behind it, put the watermark past this
    one, and then its real date would be *older* than the watermark — so it would
    never be sent at all. Stopping short costs a run instead.
    """
    _patch_archive(monkeypatch)

    real_run = youtube._run_ytdlp

    def refuse_the_third(args, *, timeout):
        if args[-1].endswith("vid00000003"):
            raise FeedError("yt-dlp failed: temporarily unavailable")
        return real_run(args, timeout=timeout)

    monkeypatch.setattr(youtube, "_run_ytdlp", refuse_the_third)

    items = youtube.archive_items(ARCHIVE_FEED, [], feed_name="Show")

    assert [i.title for i in items] == ["Title vid00000001", "Title vid00000002"]
    assert "vid00000003" in capsys.readouterr().err


def test_archive_dates_only_its_budget_per_run(monkeypatch):
    asked = _patch_archive(monkeypatch)

    items = youtube.archive_items(ARCHIVE_FEED, [], feed_name="Show", budget=2)

    assert [i.title for i in items] == ["Title vid00000001", "Title vid00000002"]
    assert len([a for a in asked if a != "listing"]) == 2


def test_the_dates_it_did_get_are_kept_for_the_next_run(monkeypatch):
    asked = _patch_archive(monkeypatch)
    youtube.archive_items(ARCHIVE_FEED, [], budget=2)

    spent_first = len([a for a in asked if a != "listing"])
    items = youtube.archive_items(ARCHIVE_FEED, [], budget=2)

    # The budget buys two *new* dates each run, on top of what is remembered.
    assert spent_first == 2
    assert len([i for i in items]) == 4


def test_the_feeds_own_items_win_where_the_two_overlap(monkeypatch):
    """Same video, but with a description and a real date rather than a derived one."""
    _patch_archive(monkeypatch)
    real = FeedItem(
        title="From the feed",
        description="the description a listing does not carry",
        link="https://www.youtube.com/watch?v=vid00000005",
        published=datetime.fromtimestamp(DATES["vid00000005"], timezone.utc),
    )

    items = youtube.archive_items(ARCHIVE_FEED, [real])

    assert items[-1] is real
    assert sum(1 for i in items if "vid00000005" in (i.link or "")) == 1


def test_the_feeds_head_is_held_back_until_the_archive_is_walkable(monkeypatch):
    """Otherwise the watermark jumps years, over everything in between."""
    _patch_archive(monkeypatch)
    head = FeedItem(
        title="Yesterday",
        description="d",
        link="https://www.youtube.com/watch?v=brandnew01",
        published=datetime.now(timezone.utc),
    )

    items = youtube.archive_items(ARCHIVE_FEED, [head], budget=2)

    assert head not in items
    assert [i.title for i in items] == ["Title vid00000001", "Title vid00000002"]


def test_once_the_archive_is_complete_the_feeds_newest_come_along(monkeypatch):
    _patch_archive(monkeypatch)
    head = FeedItem(
        title="Yesterday",
        description="d",
        link="https://www.youtube.com/watch?v=brandnew01",
        published=datetime.now(timezone.utc),
    )

    items = youtube.archive_items(ARCHIVE_FEED, [head])

    assert items[-1] is head


def test_a_listing_that_cannot_be_read_leaves_the_feed_alone(monkeypatch, capsys):
    _patch_archive(monkeypatch, listing_error="yt-dlp failed: nope")
    head = FeedItem(title="Yesterday", description="d", link="https://y/1")

    assert youtube.archive_items(ARCHIVE_FEED, [head]) == [head]
    assert "back catalogue" in capsys.readouterr().err


def test_a_non_channel_feed_is_left_alone(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("nothing to list for a feed that isn't a channel")

    monkeypatch.setattr(youtube, "_ytdlp_json", fail)
    items = [FeedItem(title="A post", description="d", link="https://a/1")]

    assert youtube.archive_items("https://waitbutwhy.com/feed", items) == items


def test_a_stale_listing_beats_no_listing(monkeypatch):
    _patch_archive(monkeypatch)
    youtube.uploads(ARCHIVE_CHANNEL)  # fills the cache

    def fail(args, *, timeout):
        raise FeedError("YouTube said no")

    monkeypatch.setattr(youtube, "_ytdlp_json", fail)
    monkeypatch.setattr(cache, "YOUTUBE_UPLOADS_TTL", -1)  # everything is stale

    assert [u.video_id for u in youtube.uploads(ARCHIVE_CHANNEL)] == CATALOGUE


def test_a_listing_that_lost_most_of_the_channel_is_not_believed(monkeypatch):
    """A partial answer looks exactly like a channel that deleted its history."""
    _patch_archive(monkeypatch)
    youtube.uploads(ARCHIVE_CHANNEL)

    _patch_archive(monkeypatch, catalogue=["vid00000009", CATALOGUE[0]])
    monkeypatch.setattr(cache, "YOUTUBE_UPLOADS_TTL", -1)

    after = [u.video_id for u in youtube.uploads(ARCHIVE_CHANNEL)]

    # The new one is taken; the archive behind it is kept rather than dropped.
    assert after == ["vid00000009", *CATALOGUE]


def test_published_prefers_a_timestamp_over_a_date(monkeypatch):
    """upload_date is a day, and a channel posting twice a day would collide."""
    asked = _patch_archive(monkeypatch)
    when = youtube._published("vid00000001")

    assert when == datetime.fromtimestamp(DATES["vid00000001"], timezone.utc)
    assert when.tzinfo is timezone.utc
    assert asked  # and it went and asked




def test_an_age_gated_video_is_stepped_over_rather_than_walling_the_series(
    monkeypatch,
):
    """A single age-gated video would otherwise be a wall nothing gets past."""
    _patch_archive(monkeypatch)
    real_run = youtube._run_ytdlp

    def age_gate_the_third(args, *, timeout):
        if args[-1].endswith("vid00000003"):
            raise FeedError(
                "yt-dlp failed: ERROR: [youtube] vid00000003: Sign in to confirm "
                "your age. This video may be inappropriate for some users."
            )
        return real_run(args, timeout=timeout)

    monkeypatch.setattr(youtube, "_run_ytdlp", age_gate_the_third)

    items = youtube.archive_items(ARCHIVE_FEED, [], feed_name="Show")

    assert [i.title for i in items] == [
        "Title vid00000001",
        "Title vid00000002",
        "Title vid00000004",
        "Title vid00000005",
    ]


@pytest.mark.parametrize(
    "complaint",
    [
        "Sign in to confirm your age",
        "Private video. Sign in if you've been granted access",
        "Join this channel to get access to members-only content",
        "Video unavailable",
        "This video has been removed by the uploader",
    ],
)
def test_the_permanent_refusals_are_told_apart_from_the_rest(complaint):
    assert youtube._inaccessible(f"yt-dlp failed: ERROR: [youtube] x: {complaint}")


@pytest.mark.parametrize(
    "complaint",
    [
        "yt-dlp timed out after 60s",
        "Could not run yt-dlp: [Errno 2] No such file",
        "ERROR: unable to download video data: HTTP Error 503",
        "Sign in to confirm you're not a bot",
    ],
)
def test_an_ordinary_failure_is_not_mistaken_for_a_refusal(complaint):
    assert not youtube._inaccessible(complaint)


def test_a_video_ruled_out_is_never_asked_about_again(monkeypatch):
    """It is remembered for good: re-asking is what would lose the episode."""
    asked = _patch_archive(monkeypatch, dated={})

    assert youtube._published("vid00000001") is None
    assert youtube._published("vid00000001") is None
    assert len(asked) == 1


def test_videos_stamped_with_the_same_second_are_nudged_apart(monkeypatch):
    """YouTube batch-publishes; equal dates would cost one of the two episodes."""
    same = 1_500_000_000
    _patch_archive(
        monkeypatch,
        dated={"vid00000001": same, "vid00000002": same, "vid00000003": same + 5},
        catalogue=["vid00000003", "vid00000002", "vid00000001"],
    )

    items = youtube.archive_items(ARCHIVE_FEED, [])
    dates = [i.published for i in items]

    assert [i.title for i in items] == [
        "Title vid00000001",
        "Title vid00000002",
        "Title vid00000003",
    ]
    assert all(a < b for a, b in zip(dates, dates[1:]))
    # Only by the smallest step there is — the playlist order is what decides.
    assert (dates[1] - dates[0]).total_seconds() == 1


def test_the_nudged_date_is_the_one_that_is_remembered(monkeypatch):
    """Or the next run would read back a different order than it sent in."""
    same = 1_500_000_000
    _patch_archive(
        monkeypatch,
        dated={"vid00000001": same, "vid00000002": same},
        catalogue=["vid00000002", "vid00000001"],
    )
    first = [i.published for i in youtube.archive_items(ARCHIVE_FEED, [])]

    monkeypatch.setattr(
        youtube, "_run_ytdlp", lambda *a, **k: pytest.fail("already dated")
    )
    second = [i.published for i in youtube.archive_items(ARCHIVE_FEED, [])]

    assert first == second


def test_distinct_leaves_an_ordinary_gap_alone():
    when = datetime(2014, 5, 12, 3, 30, 1, tzinfo=timezone.utc)
    assert youtube._distinct(when, None) is when
    assert youtube._distinct(when, when - timedelta(days=1)) is when


# --- the listing that stands in for a feed that isn't answering -------------


def _feed_answers_html(monkeypatch, status=404):
    """The feed endpoint's own outage: YouTube is up, the feed is an error page."""
    fake = type("Parsed", (), {})()
    fake.bozo = True
    fake.bozo_exception = "not well-formed (invalid token)"
    fake.entries = []
    fake.status = status
    fake.headers = {"content-type": "text/html; charset=utf-8"}
    monkeypatch.setattr(feeds.feedparser, "parse", lambda url, **kwargs: fake)
    monkeypatch.setattr(download, "reachable", lambda url, **kwargs: True)


def _channel_cfg(**extra):
    return feeds.FeedConfig(
        url=f"https://www.youtube.com/channel/{ARCHIVE_CHANNEL}", name="Show", **extra
    )


def test_a_feed_answering_html_is_read_through_the_listing_instead(monkeypatch):
    _feed_answers_html(monkeypatch)
    asked = _patch_archive(monkeypatch)

    parsed = feeds.parse_feed(_channel_cfg())

    assert [i.title for i in parsed.items] == [f"Title vid{n:08d}" for n in range(1, 6)]
    assert [i.published.timestamp() for i in parsed.items] == sorted(DATES.values())
    assert parsed.items[0].extra["id"] == "yt:video:vid00000001"
    assert parsed.items[0].extra["duration_seconds"] == "1800"
    # One listing, one date each — and no second listing for the durations,
    # which the first one already carried.
    assert asked.count("listing") == 1
    assert len(asked) == 1 + len(CATALOGUE)


def test_the_listing_asks_for_the_newest_fifteen_of_the_uploads_playlist(monkeypatch):
    _feed_answers_html(monkeypatch)
    seen = {}

    def fake_json(args, *, timeout):
        seen["args"] = args
        return {"entries": []}

    monkeypatch.setattr(youtube, "_ytdlp_json", fake_json)
    with pytest.raises(SourceBlocked):  # a listing of nothing is not believed
        feeds.parse_feed(_channel_cfg())

    assert seen["args"][-1] == f"https://www.youtube.com/playlist?list=UU{ARCHIVE_CHANNEL[2:]}"
    assert "1:15" in seen["args"]


def test_the_listing_dates_each_video_only_once(monkeypatch):
    """Dates are kept for good, so a run on the listing costs only new videos."""
    _feed_answers_html(monkeypatch)
    _patch_archive(monkeypatch)
    first = feeds.parse_feed(_channel_cfg()).items

    asked = _patch_archive(monkeypatch)
    second = feeds.parse_feed(_channel_cfg()).items

    assert asked == ["listing"]
    assert [i.published for i in first] == [i.published for i in second]


def test_the_listing_stops_where_it_could_not_ask(monkeypatch, capsys):
    """The frontier rule: nothing newer than a video that may be dated next run."""
    _feed_answers_html(monkeypatch)
    _patch_archive(monkeypatch)
    real_run = youtube._run_ytdlp

    def refuse_the_third(args, *, timeout):
        if args[-1].endswith("vid00000003"):
            raise FeedError("yt-dlp failed: Sign in to confirm you're not a bot")
        return real_run(args, timeout=timeout)

    monkeypatch.setattr(youtube, "_run_ytdlp", refuse_the_third)

    items = feeds.parse_feed(_channel_cfg()).items

    assert [i.title for i in items] == ["Title vid00000001", "Title vid00000002"]
    assert "vid00000003" in capsys.readouterr().err


def test_a_listing_that_fails_too_leaves_the_feed_skipped(monkeypatch):
    _feed_answers_html(monkeypatch, status=500)
    _patch_archive(monkeypatch, listing_error="yt-dlp failed: HTTP Error 429")

    with pytest.raises(SourceBlocked) as caught:
        feeds.parse_feed(_channel_cfg())

    message = str(caught.value)
    assert "HTTP 500 text/html" in message
    assert "HTTP Error 429" in message


def test_a_feed_that_is_not_youtube_has_no_listing_to_fall_back_on(monkeypatch):
    _feed_answers_html(monkeypatch)
    monkeypatch.setattr(
        youtube, "_ytdlp_json", lambda *a, **k: pytest.fail("not a YouTube feed")
    )

    with pytest.raises(SourceBlocked):
        feeds.parse_feed(feeds.FeedConfig(url="https://waitbutwhy.com/feed", name="W"))


# --- the channel's picture -------------------------------------------------

AVATAR = "https://yt3.googleusercontent.com/avatar=s0"
CHANNEL_THUMBNAILS = [
    {"id": "0", "url": "https://yt3/banner-1060", "width": 1060, "height": 175},
    {"id": "banner_uncropped", "url": "https://yt3/banner=s0"},
    {"id": "7", "url": "https://yt3/avatar-900", "width": 900, "height": 900},
    {"id": "avatar_uncropped", "url": AVATAR},
]


def _patch_channel(monkeypatch, thumbnails=CHANNEL_THUMBNAILS):
    asked = []

    def fake_json(args, *, timeout):
        asked.append(args)
        return {"id": "UCabc", "thumbnails": thumbnails}

    monkeypatch.setattr(youtube, "_ytdlp_json", fake_json)
    return asked


def test_channel_image_is_the_uncropped_avatar(monkeypatch):
    asked = _patch_channel(monkeypatch)

    url = youtube.channel_image("https://www.youtube.com/channel/UCabc")

    assert url == AVATAR
    # No videos: the channel's own fields are all that is wanted.
    assert asked == [
        [
            "--flat-playlist",
            "--playlist-items",
            "0",
            "https://www.youtube.com/channel/UCabc/videos",
        ]
    ]


def test_channel_image_reads_the_channel_out_of_a_feed_url(monkeypatch):
    asked = _patch_channel(monkeypatch)

    youtube.channel_image(youtube.YOUTUBE_FEED.format(channel_id="UCabc"))

    assert asked[0][-1] == "https://www.youtube.com/channel/UCabc/videos"


def test_channel_image_falls_back_to_the_largest_square(monkeypatch):
    _patch_channel(
        monkeypatch,
        [t for t in CHANNEL_THUMBNAILS if t["id"] != "avatar_uncropped"]
        + [{"id": "8", "url": "https://yt3/avatar-88", "width": 88, "height": 88}],
    )

    assert youtube.channel_image("https://www.youtube.com/channel/UCabc") == (
        "https://yt3/avatar-900"
    )


def test_channel_image_without_an_avatar_raises(monkeypatch):
    _patch_channel(monkeypatch, CHANNEL_THUMBNAILS[:2])

    with pytest.raises(FeedError, match="no picture"):
        youtube.channel_image("https://www.youtube.com/channel/UCabc")


@pytest.mark.parametrize(
    "url", ["https://a/feed.xml", "https://www.youtube.com/watch?v=BFcjfKZ0BeI", None]
)
def test_channel_image_ignores_anything_but_a_channel(monkeypatch, url):
    monkeypatch.setattr(
        youtube, "_ytdlp_json", lambda *a, **k: pytest.fail("not a channel")
    )

    assert youtube.channel_image(url) is None
