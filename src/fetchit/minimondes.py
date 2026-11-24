"""Read a Les Mini Mondes magazine link as a feed of its audio stories.

Each issue of the magazine is about one country, and its QR code leads to that
country's free activities on ``activites.lesminimondes.fr``::

    https://activites.lesminimondes.fr/activites/?categorie=&minAge=1&maxAge=12&pays=MEXIQUE

That is a WordPress listing: a page of cards, each linking to one activity,
and a "load more" call (``admin-ajax.php?action=loadmoreactivities``) that
answers with the next cards as JSON until its ``nextpage`` is ``-1``. Most
activities are a PDF to print — a recipe, a craft — and some have an
``<audio>`` player with an mp3 in it: a story, but also a song, its
instrumental, a language lesson. Only the stories become items. The site files
each activity under one category (``story``, ``music``, ``language``,
``cooking``…), shown as an icon on its page; the listing is asked for the
``story`` category only, as the site's own filter does, and each page's icon
is checked as well — a single activity's link goes by that alone.

The listing is read on every run, because a country gains activities after its
issue is out. An activity page is read once and remembered for good: what it
holds does not change.

The site publishes no date that can be read without trouble (its REST API is
off and its RSS answers with a redirect to the home page), so an item is dated
by the month its mp3 was uploaded, which WordPress puts in the file's address
(``/wp-content/uploads/2026/09/…``).

A single activity page works as a link too, for a story on its own.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from html import unescape
from urllib.parse import parse_qs, parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from . import cache
from .download import download_to, fetch_text, safe_url
from .feeds import FeedConfig, FeedError, FeedItem, ParsedFeed, apply_extracts, strip_html

SITE_URL = "https://activites.lesminimondes.fr"
HOST = "activites.lesminimondes.fr"
LISTING_PATH = "/activites/"
STORY = "story"
AUTHOR = "Les Mini Mondes"

# The site's firewall turns away Python's own User-Agent (403), mp3s included.
USER_AGENT = "fetchit"
HEADERS = {"User-Agent": USER_AGENT}
TIMEOUT = 30

# A listing grows a page at a time; this is far beyond any country's activities
# and only there so a site that never says "last page" can't keep a run going.
MAX_PAGES = 50

UPLOAD_TIMEZONE = ZoneInfo("Europe/Paris")

# What an activity page holds, by its address. Read once: it does not change.
_PAGE_NS = "minimondes_page"

_CARD_LINK_RE = re.compile(
    r'<article\b[^>]*>\s*<a\s+href="(?P<href>[^"]+)"', re.IGNORECASE
)
_LOADMORE_RE = re.compile(
    r'class="loadmore"[^>]*\bdata-page="(?P<page>-?\d+)"[^>]*\bdata-url="(?P<url>[^"]+)"',
    re.IGNORECASE,
)
_COUNTRY_RE = re.compile(
    r'<input\b[^>]*name="pays"[^>]*value="(?P<value>[^"]*)"[^>]*checked[^>]*/?>\s*'
    r'<label\b[^>]*>(?P<label>[^<]+)</label>',
    re.IGNORECASE,
)
_AUDIO_RE = re.compile(
    r'<audio\b[^>]*>(?P<body>.*?)</audio>', re.IGNORECASE | re.DOTALL
)
_SOURCE_RE = re.compile(r'<source\b[^>]*\bsrc="(?P<src>[^"]+)"', re.IGNORECASE)
_AUDIO_SRC_RE = re.compile(r'<audio\b[^>]*\bsrc="(?P<src>[^"]+)"', re.IGNORECASE)
_TITLE_RE = re.compile(r'<h1\b[^>]*class="entry-title"[^>]*>(?P<title>.*?)</h1>', re.IGNORECASE | re.DOTALL)
_IMAGE_RE = re.compile(
    r'class="bloc-image".*?background-image:\s*url\((?P<url>[^)]+)\)', re.IGNORECASE | re.DOTALL
)
_DESCRIPTION_RE = re.compile(
    r'<p\b[^>]*class="description"[^>]*>(?P<text>.*?)</p>', re.IGNORECASE | re.DOTALL
)
# The category icon in an activity's header: indoor_story_white.png and so on.
_CATEGORY_RE = re.compile(r"/assets/img/(?:in|out)door_(?P<category>[a-z]+)_white\.png", re.IGNORECASE)
# Where an activity page stops being about itself: the carousel of others.
_SIMILAR_MARK = "bloc-activites-similaires"
_AGES_RE = re.compile(r'<span class="icon">\s*(?P<ages>\d+\s*-\s*\d+\s*ans)\s*</span>', re.IGNORECASE)
_UPLOAD_MONTH_RE = re.compile(r"/uploads/(?P<year>\d{4})/(?P<month>\d{2})/")
_SITE_SUFFIX_RE = re.compile(r"\s*[|–-]\s*Les Mini Mondes\s*$", re.IGNORECASE)


class MiniMondesError(FeedError):
    """Raised when the stories behind a Les Mini Mondes link cannot be read."""


@dataclass(frozen=True)
class MiniMondesPlan:
    """One story's mp3, asked for in a way the site lets through."""

    url: str

    def describe(self) -> str:
        return f"Les Mini Mondes story {_filename(self.url)}"

    def fetch(self, into: str, *, timeout: float) -> str:
        path = os.path.join(into, _filename(self.url) or "story.mp3")
        download_to(self.url, path, timeout=TIMEOUT, headers=HEADERS)
        return path


def is_minimondes_link(url: str | None) -> bool:
    """Whether ``url`` is one this module reads: a country's listing or one activity.

    A listing must name its country (``pays``): without one it is every
    activity of every issue, which is not what a magazine's link means.
    """
    if not url:
        return False
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or (parts.hostname or "").lower() != HOST:
        return False
    path = [part for part in parts.path.split("/") if part]
    if not path or path[0] != LISTING_PATH.strip("/"):
        return False
    if len(path) == 1:
        return bool(_country_code(url))
    return len(path) == 2


def is_minimondes_item(item: FeedItem) -> bool:
    """Whether ``item`` is a story read from a Les Mini Mondes link."""
    return "minimondes_page" in item.extra


def resolve(item: FeedItem) -> MiniMondesPlan:
    """The mp3 ``item`` would be downloaded as."""
    if not is_minimondes_item(item) or not item.enclosure_url:
        raise MiniMondesError(f"Not a Les Mini Mondes story: {item.title!r}")
    return MiniMondesPlan(url=item.enclosure_url)


def parse_minimondes(cfg: FeedConfig) -> ParsedFeed:
    """Read the stories behind ``cfg.url`` as a feed of them, newest first."""
    if _is_listing(cfg.url):
        country, pages = _listing(cfg.url)
    else:
        country, pages = "", [_canonical(cfg.url)]

    items: list[FeedItem] = []
    for page_url in pages:
        page = _page(page_url)
        if page["category"] != STORY:
            continue
        audio = page["audio"]
        for index, mp3 in enumerate(audio):
            slug = _slug(page_url)
            extra = {
                "id": f"minimondes:{slug}" + (f":{index + 1}" if len(audio) > 1 else ""),
                "minimondes_page": page_url,
            }
            if len(audio) > 1:
                extra["track"] = str(index + 1)
            if country:
                extra["country"] = country
            if page["ages"]:
                extra["ages"] = page["ages"]
            items.append(
                FeedItem(
                    title=page["title"] + (f" ({index + 1})" if len(audio) > 1 else ""),
                    description=page["description"] or _about(country),
                    link=page_url,
                    published=_uploaded(mp3),
                    enclosure_url=mp3,
                    enclosure_type="audio/mpeg",
                    image_url=page["image"],
                    author=AUTHOR,
                    feed_name=cfg.name,
                    extra=extra,
                )
            )

    if not items:
        raise MiniMondesError(f"No audio story behind {cfg.url}")

    # A second apart, newest first as the listing has them: a month's uploads
    # share a date, and a reader that goes by date would otherwise put them in
    # any order at all.
    items = [
        replace(item, published=item.published + timedelta(seconds=len(items) - index))
        if item.published else item
        for index, item in enumerate(items)
    ]

    return ParsedFeed(
        items=[apply_extracts(item, cfg.extract) for item in items],
        image_url=items[0].image_url,
        description=_about(country),
        title=AUTHOR,
        link=SITE_URL,
        author=AUTHOR,
        language="fr",
    )


def story_image(url: str) -> str | None:
    """Artwork for the feed ``url`` makes: its newest story's picture."""
    return parse_minimondes(FeedConfig(url=url, name="")).image_url


# --- the listing -------------------------------------------------------------


def _is_listing(url: str) -> bool:
    return [part for part in urlsplit(url).path.split("/") if part] == [LISTING_PATH.strip("/")]


def _country_code(url: str) -> str:
    values = parse_qs(urlsplit(url).query).get("pays") or [""]
    return values[0].strip()


def _listing(url: str) -> tuple[str, list[str]]:
    """The country's name and the address of every activity the listing at ``url`` has.

    The first page is the listing itself; the rest come from its "load more"
    call, as the site's own script asks for them.
    """
    html = _get(_stories_only(url))
    country = _country_name(html, _country_code(url))
    pages = _card_links(html)

    more = _LOADMORE_RE.search(html)
    if more:
        page, ajax_url = int(more["page"]), unescape(more["url"])
        for _ in range(MAX_PAGES):
            if page < 0:
                break
            answer = _post_json(ajax_url, {"page": str(page)})
            if not isinstance(answer, dict) or answer.get("success") != 1:
                break
            pages += _card_links(answer.get("html") or "")
            page = int(answer.get("nextpage", -1))

    seen: set[str] = set()
    unique = [p for p in pages if not (p in seen or seen.add(p))]
    return country, unique


def _stories_only(url: str) -> str:
    """``url`` with its listing narrowed to stories, as the site's own filter does it."""
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "categorie"]
    return urlunsplit(parts._replace(query=urlencode([("categorie", STORY), *query])))


def _card_links(html: str) -> list[str]:
    """The activity pages the cards in ``html`` link to, in listing order."""
    links = []
    for match in _CARD_LINK_RE.finditer(html):
        href = _canonical(urljoin(SITE_URL, unescape(match["href"])))
        if is_minimondes_link(href) and not _is_listing(href):
            links.append(href)
    return links


def _country_name(html: str, code: str) -> str:
    """How the site names the country ``code`` (``MEXIQUE`` -> ``Mexique``)."""
    for match in _COUNTRY_RE.finditer(html):
        if match["value"].strip().upper() == code.upper():
            return unescape(match["label"]).strip()
    return code.replace("-", " ").title()


# --- an activity --------------------------------------------------------------


def _page(url: str) -> dict:
    """What the activity page at ``url`` holds, asked once and remembered for good."""
    hit = cache.get(_PAGE_NS, url)
    if isinstance(hit, dict) and {"title", "audio", "category"} <= set(hit):
        return hit
    page = _read_page(_get(url), url)
    cache.put(_PAGE_NS, url, page)
    return page


def _read_page(html: str, url: str) -> dict:
    """The title, category, picture, ages, text and mp3s of an activity page's ``html``."""
    html = html.split(_SIMILAR_MARK, 1)[0]
    audio: list[str] = []
    for player in _AUDIO_RE.finditer(html):
        sources = [s["src"] for s in _SOURCE_RE.finditer(player["body"])]
        own = _AUDIO_SRC_RE.search(player.group(0))
        for src in ([own["src"]] if own else []) + sources:
            mp3 = urljoin(url, unescape(src).strip())
            if mp3 not in audio:
                audio.append(mp3)

    title = _TITLE_RE.search(html)
    image = _IMAGE_RE.search(html)
    description = _DESCRIPTION_RE.search(html)
    ages = _AGES_RE.search(html)
    category = _CATEGORY_RE.search(html)
    return {
        "title": _clean_title(strip_html(title["title"])) if title else _slug(url),
        "category": category["category"].lower() if category else "",
        "audio": audio,
        "image": urljoin(url, unescape(image["url"]).strip("'\" ")) if image else None,
        "description": strip_html(description["text"]) if description else "",
        "ages": re.sub(r"\s+", " ", ages["ages"]) if ages else "",
    }


def _clean_title(title: str) -> str:
    """Drop the `` | Les Mini Mondes`` every page title ends with."""
    return _SITE_SUFFIX_RE.sub("", title).strip()


def _uploaded(mp3: str) -> datetime | None:
    """The month ``mp3`` was uploaded in, as its first day in Paris."""
    match = _UPLOAD_MONTH_RE.search(urlsplit(mp3).path)
    if not match:
        return None
    return datetime(int(match["year"]), int(match["month"]), 1, tzinfo=UPLOAD_TIMEZONE)


def _about(country: str) -> str:
    return f"Histoires audio des Mini Mondes – {country}." if country else "Histoires audio des Mini Mondes."


def _canonical(url: str) -> str:
    """``url`` without query or fragment, ending in a slash, as WordPress links it."""
    parts = urlsplit(url)
    path = parts.path if parts.path.endswith("/") else parts.path + "/"
    return f"https://{(parts.hostname or HOST).lower()}{path}"


def _slug(url: str) -> str:
    return [part for part in urlsplit(url).path.split("/") if part][-1]


def _filename(url: str) -> str:
    return os.path.basename(urlsplit(url).path)


# --- HTTP ---------------------------------------------------------------------


def _get(url: str) -> str:
    return fetch_text(url, timeout=TIMEOUT, headers=HEADERS)


def _post_json(url: str, form: dict[str, str]):
    """POST ``form`` to ``url`` and return the JSON it answers with."""
    request = Request(
        safe_url(url),
        data=urlencode(form).encode(),
        headers={**HEADERS, "Accept": "application/json"},
    )
    try:
        with urlopen(request, timeout=TIMEOUT) as response:
            return json.load(response)
    except OSError as exc:
        raise MiniMondesError(f"Could not read more of {urlsplit(url).netloc}: {exc}") from exc
    except ValueError as exc:
        raise MiniMondesError(f"{urlsplit(url).netloc} answered with something not JSON") from exc
