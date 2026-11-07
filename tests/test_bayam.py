"""Tests for the Bayam scraper that do not need network access."""

from datetime import date, timedelta

import pytest

from fetchit.bayam import (
    BayamError,
    _access_code_from_url,
    _audio,
    _clean_title,
    _encode_uri,
    _released,
)


def test_access_code_from_url():
    """Test that the access code is read from the redirect target."""
    url = "https://app.bayam.tv/mag/almyre310?af_xp=qr&deep_link_value=mag"
    assert _access_code_from_url(url) == "almyre310"


@pytest.mark.parametrize(
    "url",
    [
        "https://app.bayam.tv/",
        "https://app.bayam.tv/mag/",
        "https://app.bayam.tv/book/abc123",
    ],
)
def test_access_code_from_url_rejects_other_links(url):
    """Test that links that are not magazine links are rejected."""
    with pytest.raises(BayamError):
        _access_code_from_url(url)


def test_encode_uri():
    """Test that URLs are encoded like JavaScript's encodeURI."""
    url = "https://secure.bayard.io/audio/L'été 1.mp3?Policy=a_b&Signature=c~d-e"
    assert _encode_uri(url) == (
        "https://secure.bayard.io/audio/L'%C3%A9t%C3%A9%201.mp3?Policy=a_b&Signature=c~d-e"
    )


@pytest.mark.parametrize(
    ("name", "title"),
    [
        ("01-Un petit air de banjo", "Un petit air de banjo"),
        ("02 - Il pleut, il mouille !", "Il pleut, il mouille !"),
        ("Grosse fâcherie à Bois-Joli", "Grosse fâcherie à Bois-Joli"),
        ("101 dalmatiens", "101 dalmatiens"),
    ],
)
def test_clean_title(name, title):
    """Test that only a track number prefix is dropped from a title."""
    assert _clean_title(name) == title


def test_audio_prefers_the_track_over_its_chapters():
    """Test that chapters are only used for a track without a file of its own."""
    content = {
        "tracks": [
            {
                "name": "Whole",
                "audio_url": "https://x.test/whole.mp3",
                "chapters": [{"name": "Part", "audio_url": "https://x.test/part.mp3"}],
            },
            {
                "name": "Split",
                "audio_url": None,
                "chapters": [
                    {"name": "One", "audio_url": "https://x.test/1.mp3"},
                    {"name": "Empty", "audio_url": None},
                    {"name": "Two", "audio_url": "https://x.test/2.mp3"},
                ],
            },
        ]
    }
    assert _audio(content) == [
        ("Whole", "https://x.test/whole.mp3"),
        ("One", "https://x.test/1.mp3"),
        ("Two", "https://x.test/2.mp3"),
    ]


@pytest.mark.parametrize(
    ("value", "day", "utc_offset"),
    [
        ("2026-08-31T22:00:00", date(2026, 9, 1), 2),
        ("2026-10-31T23:00:00", date(2026, 11, 1), 1),
    ],
)
def test_released_is_midnight_in_paris(value, day, utc_offset):
    """Test that a release date is read as UTC and given in Paris time."""
    released = _released(value)
    assert released.date() == day
    assert (released.hour, released.minute) == (0, 0)
    assert released.utcoffset() == timedelta(hours=utc_offset)


def test_released_is_optional():
    """Test that an issue without a release date has none."""
    assert _released(None) is None


# --- a magazine link as a feed ----------------------------------------------

LINK = "https://flyer.bayam.tv/4sYB/dj8"

CONTENT = {
    "title": "Mes premières Belles Histoires n°288",
    "cover_url": "https://cdn.bayam/covers/MBH288 couverture.jpg",
    "release_date_prev": "2026-08-31T22:00:00",
    "magazine_number": 288,
    "collection": {"name": "Mes premières Belles Histoires"},
    "tracks": [
        {"name": "01-Un petit air", "audio_url": "https://secure.bayard.io/a/1.mp3", "chapters": []},
        {"name": "02-Le loup", "audio_url": "https://secure.bayard.io/a/2.mp3", "chapters": []},
    ],
}


@pytest.fixture
def magazine(monkeypatch):
    """Stand in for Bayam's three lookups, counting them."""
    from fetchit import bayam

    calls = []
    monkeypatch.setattr(bayam, "_resolve_access_code", lambda url: calls.append(url) or "code1")
    monkeypatch.setattr(bayam, "_fetch_sku", lambda code: "MBH288")
    monkeypatch.setattr(bayam, "_fetch_content", lambda sku: CONTENT)
    return calls


def test_parse_bayam_makes_a_track_an_item(magazine):
    from fetchit.bayam import parse_bayam
    from fetchit.feeds import FeedConfig

    feed = parse_bayam(FeedConfig(url=LINK, name="Belles Histoires"))

    assert [item.title for item in feed.items] == ["Un petit air", "Le loup"]
    first = feed.items[0]
    assert first.description == "Mes premières Belles Histoires n° 288"
    assert first.extra["id"] == "bayam:MBH288:1"
    assert first.extra["number"] == "288"
    assert first.enclosure_url == "https://secure.bayard.io/a/1.mp3"
    assert first.image_url == "https://cdn.bayam/covers/MBH288%20couverture.jpg"
    assert feed.title == "Mes premières Belles Histoires"
    assert feed.language == "fr"


def test_parse_bayam_dates_the_tracks_of_an_issue_a_second_apart(magazine):
    """Same release day for all; a reader going by date must still see them all."""
    from fetchit.bayam import parse_bayam
    from fetchit.feeds import FeedConfig

    first, second = parse_bayam(FeedConfig(url=LINK, name="B")).items
    assert first.published.date() == date(2026, 9, 1)
    assert second.published - first.published == timedelta(seconds=1)


def test_parse_bayam_asks_about_an_issue_once(magazine):
    from fetchit.bayam import parse_bayam
    from fetchit.feeds import FeedConfig

    parse_bayam(FeedConfig(url=LINK, name="B"))
    parse_bayam(FeedConfig(url=LINK, name="B"))
    assert magazine == [LINK]


def test_a_track_is_signed_when_it_is_downloaded(magazine, monkeypatch, tmp_path):
    from fetchit import bayam
    from fetchit.feeds import FeedConfig
    from fetchit.media import resolve

    item = bayam.parse_bayam(FeedConfig(url=LINK, name="B")).items[0]
    monkeypatch.setattr(bayam, "_fetch_signature", lambda sku, url: "?Signature=s1")
    fetched = []

    def fake_download_to(url, path, **kwargs):
        fetched.append((url, kwargs["headers"]["User-Agent"]))
        with open(path, "wb") as fh:
            fh.write(b"mp3")

    monkeypatch.setattr(bayam, "download_to", fake_download_to)
    path = resolve(item).fetch(str(tmp_path), timeout=60)

    assert fetched[0][0] == "https://secure.bayard.io/a/1.mp3?Signature=s1"
    assert "Chrome" in fetched[0][1]
    assert path.endswith("1.mp3")


def test_is_bayam_link():
    from fetchit.bayam import is_bayam_link

    assert is_bayam_link(LINK)
    assert is_bayam_link("https://app.bayam.tv/mag/almyre310")
    assert not is_bayam_link("https://www.bayam.tv/")
    assert not is_bayam_link(None)
