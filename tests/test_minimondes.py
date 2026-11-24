"""Tests for the Les Mini Mondes reader that do not need network access."""

from datetime import datetime

import pytest

from fetchit import media, minimondes
from fetchit.feeds import FeedConfig
from fetchit.minimondes import (
    MiniMondesError,
    _card_links,
    _clean_title,
    _country_name,
    _read_page,
    _uploaded,
    is_minimondes_link,
    parse_minimondes,
)

QR = (
    "https://activites.lesminimondes.fr/activites/?categorie=&minAge=1&maxAge=12&pays=MEXIQUE"
    "&utm_source=offline&utm_medium=referral&utm_campaign=qr_code_mexique_activites_gratuites"
)
STORY = "https://activites.lesminimondes.fr/activites/histoire-audio-enfant-mexique-drole-de-fete-les-mini-mondes/"
CRAFT = "https://activites.lesminimondes.fr/activites/diy-lanterne-mexicaine-du-mexique-pdf-les-mini-mondes/"
SONG = "https://activites.lesminimondes.fr/activites/chanson-mexique/"
LEGEND = "https://activites.lesminimondes.fr/activites/la-legende-du-mais/"
MP3 = "https://activites.lesminimondes.fr/wp-content/uploads/2026/09/Mini-Mondes-Mexique.mp3"


def _card(href: str, title: str) -> str:
    return (
        f'<article  class="post-1 activites" style="background-image: url(https://x.test/c.png)">\n'
        f'\t<a href="{href}" alt="{title}">\n\t\t<h2 class="entry-title">{title}</h2>\n\t</a>\n</article>\n'
    )


LISTING = f"""
<input class="sr-only" type="radio"  name="pays" value="MX" id="country-13" />
<label for="country-13">Mexique (ancien)</label>
<input class="sr-only" type="radio"  name="pays" value="MEXIQUE" id="country-14" checked="checked" />
<label for="country-14">Mexique</label>
{_card(CRAFT, "DIY Lanterne")}
{_card(STORY, "Histoire audio")}
<div class="loadmore" data-page="2" data-url="https://activites.lesminimondes.fr/wp-admin/admin-ajax.php?action=loadmoreactivities&amp;pays=MEXIQUE">Chargement...</div>
"""


def _page(title: str, *, category: str, audio: str = "", ages: str = "4-8 ans", description: str = "") -> str:
    return f"""
<div class="bloc-image">
  <div class="image" style="background-image: url(https://activites.lesminimondes.fr/wp-content/uploads/2026/09/cover.png)">
</div>
<h1 class="entry-title">{title}</h1>
<div class="categories"><span class="icon">{ages}</span><span class="icon"><img src="https://activites.lesminimondes.fr/wp-content/themes/activites/assets/img/indoor_{category}_white.png" alt="" /></span></div>
<p class="description">{description}</p>
<div class="player-download">{audio}</div>
<div class="bloc-activites-similaires">
  <span class="icon"><img src="https://activites.lesminimondes.fr/wp-content/themes/activites/assets/img/indoor_story_white.png" alt="" /></span>
</div>
"""


PAGES = {
    STORY: _page(
        "Histoire audio enfant &ndash; Mexique &ndash; Dr&ocirc;le de f&ecirc;te | Les Mini Mondes",
        category="story",
        audio=f'<audio controls class="audio">\n<source src="{MP3}" type="audio/mpeg">\n</audio>',
    ),
    CRAFT: _page("DIY Lanterne mexicaine du Mexique [PDF] | Les Mini Mondes", category="diy"),
    SONG: _page(
        "Chanson du Mexique | Les Mini Mondes",
        category="music",
        audio='<audio controls src="/wp-content/uploads/2026/10/chanson.mp3"></audio>',
        description="<strong>Une chanson</strong> &amp; sa musique",
    ),
    LEGEND: _page(
        "La légende du maïs | Les Mini Mondes",
        category="story",
        audio='<audio controls src="/wp-content/uploads/2026/10/legende.mp3"></audio>',
    ),
}


@pytest.fixture
def site(monkeypatch, tmp_path):
    """The site, answered from the pages above; records what was asked."""
    monkeypatch.setenv("FETCHIT_CACHE", str(tmp_path / "cache.json"))
    from fetchit import cache

    cache.clear()
    asked = []

    def get(url):
        asked.append(url)
        return LISTING if "pays=" in url else PAGES[url]

    def post(url, form):
        asked.append((url, form))
        if form["page"] == "2":
            # The site's filter is asked for stories; were a song to slip
            # through, the page's own icon still keeps it out.
            html = _card(SONG, "Chanson") + _card(LEGEND, "Légende")
            return {"success": 1, "page": "2", "nextpage": 3, "html": html}
        return {"success": 1, "page": form["page"], "nextpage": -1, "html": ""}

    monkeypatch.setattr(minimondes, "_get", get)
    monkeypatch.setattr(minimondes, "_post_json", post)
    return asked


@pytest.mark.parametrize("url, ok", [
    (QR, True),
    ("https://activites.lesminimondes.fr/activites/?pays=FR", True),
    (STORY, True),
    ("http://activites.lesminimondes.fr/activites/histoire", True),
    ("https://activites.lesminimondes.fr/activites/", False),
    ("https://activites.lesminimondes.fr/activites/?categorie=story&pays=", False),
    ("https://activites.lesminimondes.fr/", False),
    ("https://activites.lesminimondes.fr/wp-content/uploads/2026/09/Mini-Mondes-Mexique.mp3", False),
    ("https://www.lesminimondes.fr/activites/?pays=MEXIQUE", False),
    ("ftp://activites.lesminimondes.fr/activites/?pays=MEXIQUE", False),
    (None, False),
])
def test_is_minimondes_link(url, ok):
    assert is_minimondes_link(url) is ok


def test_a_magazine_link_is_one_a_link_list_takes():
    assert media.link_source(QR) == "Les Mini Mondes"


def test_card_links_are_the_activity_pages_in_order():
    assert _card_links(LISTING) == [CRAFT, STORY]


def test_the_country_is_named_as_the_site_names_it():
    assert _country_name(LISTING, "MEXIQUE") == "Mexique"
    assert _country_name("", "ILE-MAURICE") == "Ile Maurice"


def test_a_story_page_is_read():
    page = _read_page(PAGES[STORY], STORY)
    assert page == {
        "title": "Histoire audio enfant – Mexique – Drôle de fête",
        "category": "story",
        "audio": [MP3],
        "image": "https://activites.lesminimondes.fr/wp-content/uploads/2026/09/cover.png",
        "description": "",
        "ages": "4-8 ans",
    }


def test_an_audio_src_of_its_own_counts_and_is_made_absolute():
    page = _read_page(PAGES[SONG], SONG)
    assert page["audio"] == ["https://activites.lesminimondes.fr/wp-content/uploads/2026/10/chanson.mp3"]
    assert page["description"] == "Une chanson & sa musique"


def test_a_page_without_a_player_has_no_audio():
    assert _read_page(PAGES[CRAFT], CRAFT)["audio"] == []


def test_the_category_is_the_pages_own_not_a_similar_activitys():
    assert _read_page(PAGES[SONG], SONG)["category"] == "music"
    assert _read_page(PAGES[CRAFT], CRAFT)["category"] == "diy"


def test_the_listing_is_asked_for_stories_only(site):
    parse_minimondes(FeedConfig(url=QR, name="x"))
    listing = site[0]
    assert "categorie=story" in listing and "categorie=&" not in listing
    assert "pays=MEXIQUE" in listing and "utm_campaign=" in listing


@pytest.mark.parametrize("title, clean", [
    ("Histoire audio – Mexique | Les Mini Mondes", "Histoire audio – Mexique"),
    ("Coloriage – Les Mini Mondes", "Coloriage"),
    ("Sans suffixe", "Sans suffixe"),
])
def test_clean_title(title, clean):
    assert _clean_title(title) == clean


def test_a_story_is_dated_by_the_month_it_was_uploaded():
    assert _uploaded(MP3) == datetime(2026, 9, 1, tzinfo=minimondes.UPLOAD_TIMEZONE)
    assert _uploaded("https://x.test/a.mp3") is None


def test_a_listing_is_read_as_its_stories(site):
    parsed = parse_minimondes(FeedConfig(url=QR, name="Mini Mondes"))

    assert [item.link for item in parsed.items] == [STORY, LEGEND]
    story, legend = parsed.items
    assert story.title == "Histoire audio enfant – Mexique – Drôle de fête"
    assert story.enclosure_url == MP3 and story.enclosure_type == "audio/mpeg"
    assert story.description == "Histoires audio des Mini Mondes – Mexique."
    assert story.extra["country"] == "Mexique" and story.extra["ages"] == "4-8 ans"
    assert story.feed_name == "Mini Mondes"
    # Dated by upload month, a second apart in listing order (newest first).
    paris = minimondes.UPLOAD_TIMEZONE
    assert story.published == datetime(2026, 9, 1, 0, 0, 2, tzinfo=paris)
    assert legend.published == datetime(2026, 10, 1, 0, 0, 1, tzinfo=paris)
    assert parsed.title == "Les Mini Mondes" and parsed.language == "fr"
    # Each story shows its own picture; the feed, the magazine's logo.
    assert story.image_url == "https://activites.lesminimondes.fr/wp-content/uploads/2026/09/cover.png"
    assert parsed.image_url == minimondes.LOGO_URL

    assert media.kind_of(story) == "audio"
    assert media.item_id(story) == "minimondes:histoire-audio-enfant-mexique-drole-de-fete-les-mini-mondes"
    plan = media.resolve(story)
    assert isinstance(plan, minimondes.MiniMondesPlan) and plan.url == MP3


def test_the_listing_is_read_each_time_and_a_page_once(site):
    parse_minimondes(FeedConfig(url=QR, name="x"))
    site.clear()
    parse_minimondes(FeedConfig(url=QR, name="x"))
    assert [a for a in site if isinstance(a, str)] == [site[0]] and "pays=MEXIQUE" in site[0]


def test_paging_stops_when_the_site_says_last(site):
    parse_minimondes(FeedConfig(url=QR, name="x"))
    assert [form["page"] for _, form in (a for a in site if isinstance(a, tuple))] == ["2", "3"]


def test_one_activity_page_is_a_feed_of_its_own(site):
    parsed = parse_minimondes(FeedConfig(url=STORY + "?utm_source=qr", name="x"))
    assert [item.enclosure_url for item in parsed.items] == [MP3]
    assert "country" not in parsed.items[0].extra
    assert parsed.image_url == minimondes.LOGO_URL


def test_the_feed_s_artwork_is_the_logo_without_reading_the_site(site):
    assert media.source_image(QR) == minimondes.LOGO_URL
    assert site == []


@pytest.mark.parametrize("page", [CRAFT, SONG])
def test_a_link_with_no_story_behind_it_is_an_error(site, page):
    with pytest.raises(MiniMondesError, match="No audio story"):
        parse_minimondes(FeedConfig(url=page, name="x"))


def test_the_download_asks_with_a_user_agent(monkeypatch, tmp_path):
    seen = {}

    def download_to(url, path, *, timeout, headers):
        seen.update(url=url, headers=headers)
        open(path, "wb").close()

    monkeypatch.setattr(minimondes, "download_to", download_to)
    path = minimondes.MiniMondesPlan(MP3).fetch(str(tmp_path), timeout=30)
    assert path.endswith("Mini-Mondes-Mexique.mp3")
    assert seen["url"] == MP3 and "Python" not in seen["headers"]["User-Agent"]
