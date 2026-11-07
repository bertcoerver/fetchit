"""Tests for fetchit.npo (the page and Downloadgemist are faked — no network)."""

import json
import re
from datetime import datetime, timezone

import pytest

from fetchit import downloadgemist, media as video, npo
from fetchit.quality import Quality

BUDGET = Quality(max_height=None, part_bytes=95 * 1024 * 1024)
from fetchit.feeds import FeedConfig, FeedError, FeedItem, FieldExtract
from fetchit.media import VideoTooBig

SERIES_URL = "https://npo.nl/start/serie/bureau-buitenland/afleveringen"
SEASON_URL = "https://npo.nl/start/serie/bureau-buitenland/afleveringen/seizoen-2"
EPISODE_LINK = "https://npo.nl/start/afspelen/bureau-buitenland_825"

SERIES = {
    "title": "Bureau Buitenland",
    "slug": "bureau-buitenland",
    "synopsis": "Sophie Derkzen en Bram Vermeulen bespreken de wereldpolitiek.",
    "images": [
        {"url": "https://assets/title.png", "role": "title"},
        {"url": "https://assets/default.jpg", "role": "default"},
    ],
}

PROGRAMS = [
    {
        "title": "26.000 sancties tegen Rusland, en toch is het niet genoeg",
        "slug": "bureau-buitenland_825",
        "productId": "VPWON_1365951",
        "synopsis": {"short": "Kort.", "long": "<p>Lang verhaal.</p>"},
        "durationInSeconds": 1540,
        "firstBroadcastDate": 1790537400,
        "publishedDateTime": 1790537400,
        "images": [{"url": "https://assets/825.jpg", "role": "default"}],
        "programKey": "28",
        "season": {"slug": "seizoen-2", "seasonKey": "2"},
        "broadcasters": [{"name": "VPRO"}],
        "genres": [
            {
                "name": "Informatief",
                "type": "primary",
                "secondaries": [{"name": "Nieuws/actualiteiten"}],
            }
        ],
    },
    {
        "title": "China heeft hele andere belangen in de AI-wedloop",
        "slug": "bureau-buitenland_824",
        "productId": "VPWON_1365950",
        "synopsis": {"short": "Alleen kort.", "long": None},
        "durationInSeconds": 1544,
        "firstBroadcastDate": None,
        "publishedDateTime": 1789932300,
        "images": [],
    },
]


def _page(queries):
    data = {"props": {"pageProps": {"dehydratedState": {"queries": queries}}}}
    return (
        "<html><head></head><body>"
        f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script>'
        "</body></html>"
    )


def _flight_page(queries):
    """The page as NPO has served it since November 2026: a Next.js App Router
    page, its data streamed in pieces through ``self.__next_f.push``, with the
    react-query cache handed to a client component somewhere in the stream."""
    line = json.dumps(
        ["$", "$L20", None, {"state": {"mutations": [], "queries": queries}, "children": "$L21"}]
    )
    stream = '0:{"P":null}\n1a:["$","title","0",{"children":"Bureau Buitenland"}]\n1f:' + line + "\n"
    third = len(stream) // 3
    pieces = [stream[:third], stream[third : 2 * third], stream[2 * third :]]
    scripts = ['<script>(self.__next_f=self.__next_f||[]).push([0])</script>'] + [
        f"<script>self.__next_f.push({json.dumps([1, piece])})</script>" for piece in pieces
    ]
    return "<html><head></head><body>" + "".join(scripts) + "</body></html>"


def _queries(programs=PROGRAMS, series=SERIES):
    return [
        {"queryKey": ["series:detail-bureau-buitenland"], "state": {"data": series}},
        {"queryKey": ["banners-by-entity:series-x"], "state": {"data": []}},
        {
            "queryKey": ["series:seasons-bureau-buitenland-include-premium"],
            "state": {"data": [{"slug": "seizoen-2"}, {"slug": "seizoen-1"}]},
        },
        {
            "queryKey": ["programs:season-2252c713-include-premium-timebound_series"],
            "state": {"data": programs},
        },
    ]


def _patch_page(monkeypatch, html=None):
    seen = []

    def fake_fetch_text(url, *, timeout=30, headers=None):
        seen.append((url, headers))
        return html if html is not None else _page(_queries())

    monkeypatch.setattr(npo, "fetch_text", fake_fetch_text)
    return seen


# --- recognising urls -----------------------------------------------------


@pytest.mark.parametrize(
    "url, expected",
    [
        (SERIES_URL, ("bureau-buitenland", None)),
        (SERIES_URL + "/", ("bureau-buitenland", None)),
        ("https://npo.nl/start/serie/bureau-buitenland", ("bureau-buitenland", None)),
        (SEASON_URL, ("bureau-buitenland", "seizoen-2")),
        ("https://www.npo.nl/start/serie/bureau-buitenland/afleveringen/seizoen-2?x=1",
         ("bureau-buitenland", "seizoen-2")),
        # An episode is not a series.
        ("https://npo.nl/start/serie/bureau-buitenland/seizoen-2/bureau-buitenland_825/afspelen", None),
        (EPISODE_LINK, None),
        ("https://example.com/start/serie/x", None),
        (None, None),
    ],
)
def test_npo_series(url, expected):
    assert npo.npo_series(url) == expected


@pytest.mark.parametrize(
    "link, expected",
    [
        (EPISODE_LINK, "bureau-buitenland_825"),
        ("https://npo.nl/start/serie/bureau-buitenland/seizoen-2/bureau-buitenland_825/afspelen",
         "bureau-buitenland_825"),
        (SERIES_URL, None),
        ("https://www.arte.tv/fr/videos/127395-052-A/x/", None),
        (None, None),
    ],
)
def test_npo_episode_slug(link, expected):
    assert npo.npo_episode_slug(link) == expected


def test_an_episode_link_is_a_video_item():
    assert video.is_video_item(FeedItem(title="t", description="", link=EPISODE_LINK))


# --- reading a series -----------------------------------------------------


def test_parse_reads_every_episode_of_the_season(monkeypatch):
    seen = _patch_page(monkeypatch)

    feed = npo.parse_npo_series(FeedConfig(url=SEASON_URL, name="BB"))

    assert seen[0][0] == SEASON_URL
    # Asked like a browser would, not as Python-urllib.
    assert "Mozilla" in seen[0][1]["User-Agent"]
    assert feed.image_url == "https://assets/default.jpg"
    assert feed.description.startswith("Sophie Derkzen")

    first, second = feed.items
    assert first.title == "26.000 sancties tegen Rusland, en toch is het niet genoeg"
    assert first.link == EPISODE_LINK
    assert first.description == "Lang verhaal."
    assert first.published == datetime.fromtimestamp(1790537400, tz=timezone.utc)
    assert first.image_url == "https://assets/825.jpg"
    assert first.feed_name == "BB"
    assert first.extra["id"] == "VPWON_1365951"
    assert first.extra["duration_seconds"] == "1540"
    assert first.extra["show_title"] == "Bureau Buitenland"
    # Its place in the series, and what kind of programme it is: what the
    # archive needs to file it as one.
    assert (first.extra["season"], first.extra["episode"]) == ("2", "28")
    assert first.categories == ("Informatief", "Nieuws/actualiteiten")
    assert first.author == "VPRO"

    # No long synopsis, no first broadcast: the fallbacks.
    assert second.description == "Alleen kort."
    assert second.published == datetime.fromtimestamp(1789932300, tz=timezone.utc)
    assert second.image_url is None
    # And an episode the page doesn't number is simply not numbered.
    assert "season" not in second.extra and "episode" not in second.extra
    assert (second.categories, second.author) == ((), None)


def test_parse_is_what_the_feed_reader_uses_for_a_series(monkeypatch):
    _patch_page(monkeypatch)

    feed = video.collection_feed(FeedConfig(url=SERIES_URL, name="BB"))

    assert feed is not None
    assert len(feed.items) == 2


def test_parse_applies_extracts(monkeypatch):
    _patch_page(monkeypatch)
    cfg = FeedConfig(
        url=SERIES_URL,
        name="BB",
        extract=(FieldExtract(name="number", source="link", pattern=re.compile(r"_(\d+)$")),),
    )

    feed = npo.parse_npo_series(cfg)

    assert feed.items[0].extra["number"] == "825"


def test_parse_reads_the_streamed_page_npo_serves_now(monkeypatch):
    """No __NEXT_DATA__ any more: the same data, in the App Router's flight stream."""
    _patch_page(monkeypatch, html=_flight_page(_queries()))

    feed = npo.parse_npo_series(FeedConfig(url=SERIES_URL, name="BB"))

    assert [item.title for item in feed.items] == [p["title"] for p in PROGRAMS]
    assert feed.description
    assert feed.title == SERIES["title"]


def test_parse_of_a_streamed_page_without_the_series_fails_loudly(monkeypatch):
    queries = [q for q in _queries() if not q["queryKey"][0].startswith("series:")]
    _patch_page(monkeypatch, html=_flight_page(queries))

    with pytest.raises(FeedError, match="NPO has changed its pages again"):
        npo.parse_npo_series(FeedConfig(url=SERIES_URL, name="BB"))


def test_parse_without_page_data_fails_loudly(monkeypatch):
    _patch_page(monkeypatch, html="<html>redesigned</html>")

    with pytest.raises(FeedError, match="__NEXT_DATA__"):
        npo.parse_npo_series(FeedConfig(url=SERIES_URL, name="BB"))


def test_parse_without_episodes_fails_loudly(monkeypatch):
    queries = [q for q in _queries() if not q["queryKey"][0].startswith("programs:")]
    _patch_page(monkeypatch, html=_page(queries))

    with pytest.raises(FeedError, match="lists no episodes"):
        npo.parse_npo_series(FeedConfig(url=SERIES_URL, name="BB"))


def test_series_image_is_the_default_picture(monkeypatch):
    _patch_page(monkeypatch)

    assert video.source_image(SERIES_URL) == "https://assets/default.jpg"


# --- resolving an episode -------------------------------------------------


def _lookup(duration=1517.0, rungs=None):
    rungs = rungs or [
        ("1080p", 3899595),
        ("720p", 2424017),
        ("540p", 1460716),
        ("480p", 988953),
        ("360p", 399307),
        ("256Kbps", 256001),
        ("128Kbps", 128000),
    ]
    return downloadgemist.Lookup(
        url=EPISODE_LINK,
        session_id="1790595490-7408",
        duration=duration,
        rungs=tuple(downloadgemist.Rung(label, rate) for label, rate in rungs),
        looked_up_at=0.0,
    )


def _patch_streams(monkeypatch, lookup):
    asked = []

    def fake_streams(url):
        asked.append(url)
        return lookup

    monkeypatch.setattr(npo.downloadgemist, "streams", fake_streams)
    return asked


def test_resolve_picks_the_fewest_parts_then_the_best_rung(monkeypatch):
    asked = _patch_streams(monkeypatch, _lookup())
    item = FeedItem(title="t", description="", link=EPISODE_LINK)

    plan = video.resolve(item, BUDGET)

    assert asked == [EPISODE_LINK]
    # A 25-minute episode: 360p (~105 MB) doesn't fit one message, and two
    # parts' 190 MB don't stretch to 480p (~240 MB).
    assert plan.rung.label == "360p"
    assert plan.parts == 2
    assert plan.describe() == "360p, ~105 MB, in 2 parts"


def test_resolve_takes_the_best_rung_when_one_message_is_enough(monkeypatch):
    _patch_streams(monkeypatch, _lookup(duration=600.0))
    item = FeedItem(title="t", description="", link=EPISODE_LINK)

    plan = npo.resolve(item, quality=BUDGET)

    assert (plan.rung.label, plan.parts) == ("480p", 1)


def test_resolve_passes_over_a_rung_without_a_bitrate(monkeypatch):
    # As Downloadgemist really lists it: a second 720p, bitrate 0. Sized by
    # that it would be the smallest rung of all, and the 317 MB it is arrives
    # in four parts.
    rungs = [("720p_2", 0), ("720p", 2414455), ("480p", 991167), ("360p", 400171)]
    _patch_streams(monkeypatch, _lookup(duration=1524.0, rungs=rungs))

    plan = npo.resolve(FeedItem(title="t", description="", link=EPISODE_LINK), quality=BUDGET)

    assert (plan.rung.label, plan.parts) == ("360p", 2)


def test_resolve_never_picks_audio_only(monkeypatch):
    _patch_streams(monkeypatch, _lookup(duration=60.0))

    plan = npo.resolve(FeedItem(title="t", description="", link=EPISODE_LINK), quality=BUDGET)

    assert plan.rung.label == "1080p"


def test_resolve_asks_with_the_short_link_whatever_it_was_given(monkeypatch):
    asked = _patch_streams(monkeypatch, _lookup())
    long_link = (
        "https://npo.nl/start/serie/bureau-buitenland/seizoen-2/"
        "bureau-buitenland_825/afspelen"
    )

    npo.resolve(FeedItem(title="t", description="", link=long_link), quality=BUDGET)

    assert asked == [EPISODE_LINK]


def test_resolve_refuses_what_no_parts_can_hold(monkeypatch):
    _patch_streams(monkeypatch, _lookup(duration=6 * 60 * 60))

    with pytest.raises(VideoTooBig, match="bureau-buitenland_825"):
        npo.resolve(FeedItem(title="t", description="", link=EPISODE_LINK), quality=BUDGET)


def test_resolve_needs_some_video(monkeypatch):
    _patch_streams(monkeypatch, _lookup(rungs=[("128Kbps", 128000)]))

    with pytest.raises(FeedError, match="no video"):
        npo.resolve(FeedItem(title="t", description="", link=EPISODE_LINK), quality=BUDGET)


def test_plan_fetches_the_chosen_rung(monkeypatch):
    lookup = _lookup()
    _patch_streams(monkeypatch, lookup)
    fetched = []

    def fake_fetch(got, label, into, *, timeout):
        fetched.append((got, label, into, timeout))
        return f"{into}/video.mp4"

    monkeypatch.setattr(npo.downloadgemist, "fetch", fake_fetch)
    plan = npo.resolve(FeedItem(title="t", description="", link=EPISODE_LINK), quality=BUDGET)

    assert plan.fetch("/tmp/x", timeout=5) == "/tmp/x/video.mp4"
    assert fetched == [(lookup, "360p", "/tmp/x", 5)]
