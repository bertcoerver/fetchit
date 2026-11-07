"""Read a Bayam (Bayard Jeunesse) magazine link as a feed of its audio stories.

The link printed in the magazine (e.g. ``https://flyer.bayam.tv/4sYB/3v2``)
redirects to ``https://app.bayam.tv/mag/<access code>``. From there the web
app makes three calls, which this module repeats:

1. access code -> product (SKU), via the Bayam REST API;
2. SKU -> issue details, cover and tracks with their mp3 URLs, via the Bayam
   CMS (GraphQL);
3. SKU -> a short-lived signature that has to be appended to the mp3 URLs.

The first two describe the issue and never change, so they are what reading the
link costs, once — the answer is cached for good. The third is asked for only
when a track is actually downloaded, because a signature does not outlive the
download it was issued for.

A feed of magazines is a list of such links, one per issue, under ``urls``:
each track becomes an item, dated with the issue's release, numbered with the
issue number, and described with the magazine it came with.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from . import cache
from .download import download_to
from .feeds import FeedConfig, FeedError, FeedItem, ParsedFeed, apply_extracts

# Client configuration of the public web app, taken from
# https://app.bayam.tv/bundle.web.js. Update these if Bayam rotates them.
REST_API_BASE = "https://services.bayam.tv/api/"
REST_API_APP_SECRET = "eeadc441-808a-42b0-a2c0-cfcf9a11530a"
CMS_GRAPHQL_URL = "https://bayam.directus.app/graphql"
CMS_TOKEN = "ac3JjikgRnhq9NDhOCJKrqnxAmjMK39S"
HORIZON_API_BASE = "https://horizon.bayard.io/api/"

BAYAM_URL = "https://www.bayam.tv"
BAYAM_HOSTS = frozenset({"flyer.bayam.tv", "app.bayam.tv"})

# The short link only redirects to the web app for desktop browsers.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
TIMEOUT = 30

MAGAZINE_PATH = "mag"
MAGAZINE_TYPE = "audio-magazine"

# The CMS stores release dates as UTC without saying so: an issue released on
# 1 September reads 2026-08-31T22:00:00, which is midnight in Paris.
RELEASE_TIMEZONE = ZoneInfo("Europe/Paris")

# What an issue says about itself, by link. It is a printed magazine: once out,
# its tracks and cover don't change.
_MAGAZINE_NS = "bayam_magazine"

TRACKS_QUERY = """
query ($sku: String) {
  mbbooks_audio_press_contents(filter: { cgr3: { _eq: $sku } }) {
    title
    cover_url
    release_date_prev
    magazine_number
    collection {
      name
    }
    tracks(sort: ["order"]) {
      name
      audio_url
      chapters(sort: "number") {
        name
        audio_url
      }
    }
  }
}
"""

# Characters JavaScript's encodeURI() leaves untouched; the signature is
# issued for the URL as the web app encodes it.
_ENCODE_URI_SAFE = ";,/?:@&=+$-_.!~*'()#"


class BayamError(FeedError):
    """Raised when the audio behind a Bayam link cannot be found."""


@dataclass(frozen=True)
class BayamPlan:
    """One track to download: its unsigned address, signed at download time."""

    sku: str
    audio_url: str

    def describe(self) -> str:
        return f"Bayam track {_filename(self.audio_url)}"

    def fetch(self, into: str, *, timeout: float) -> str:
        signature = _fetch_signature(self.sku, self.audio_url)
        path = os.path.join(into, _filename(self.audio_url) or "track.mp3")
        download_to(
            _encode_uri(self.audio_url + signature),
            path,
            timeout=TIMEOUT,
            headers={"User-Agent": USER_AGENT},
        )
        return path


def is_bayam_link(url: str | None) -> bool:
    """Whether ``url`` is a magazine link — the printed one or the app's."""
    if not url:
        return False
    return (urlsplit(url).hostname or "").lower() in BAYAM_HOSTS


def is_bayam_item(item: FeedItem) -> bool:
    """Whether ``item`` is a track read from a magazine link."""
    return "bayam_sku" in item.extra


def parse_bayam(cfg: FeedConfig) -> ParsedFeed:
    """Read the magazine behind ``cfg.url`` as a feed of its tracks."""
    magazine = _magazine(cfg.url)
    collection = magazine["collection"] or ""
    number = magazine["number"]
    issue = " ".join(
        part for part in (collection, f"n° {number}" if number is not None else "") if part
    )
    released = _released(magazine["released"])

    items = []
    for index, (name, audio_url) in enumerate(magazine["audio"]):
        extra = {
            "id": f"bayam:{magazine['sku']}:{index + 1}",
            "bayam_sku": magazine["sku"],
            "bayam_link": cfg.url,
            "track": str(index + 1),
        }
        if number is not None:
            extra["number"] = str(number)
        if collection:
            extra["collection"] = collection
        items.append(
            FeedItem(
                title=_clean_title(name),
                description=issue or _clean_title(magazine["title"]),
                link=cfg.url,
                # A second apart, in playing order: every track of an issue
                # comes out the same day, and a reader that goes by date would
                # otherwise see one track and the rest as already behind it.
                published=released + timedelta(seconds=index) if released else None,
                enclosure_url=audio_url,
                enclosure_type="audio/mpeg",
                image_url=magazine["cover"],
                author="Bayard Jeunesse",
                feed_name=cfg.name,
                extra=extra,
            )
        )

    return ParsedFeed(
        items=[apply_extracts(item, cfg.extract) for item in items],
        image_url=magazine["cover"],
        description=f"Histoires audio du magazine {collection}." if collection else "",
        title=collection,
        link=BAYAM_URL,
        author="Bayard Jeunesse",
        language="fr",
    )


def magazine_image(url: str) -> str:
    """The cover of the issue behind ``url``."""
    cover = _magazine(url)["cover"]
    if not cover:
        raise BayamError(f"Bayam has no cover for {url}")
    return cover


def resolve(item: FeedItem) -> BayamPlan:
    """The track ``item`` would be downloaded as."""
    if not is_bayam_item(item) or not item.enclosure_url:
        raise BayamError(f"Not a Bayam track: {item.title!r}")
    return BayamPlan(sku=item.extra["bayam_sku"], audio_url=item.enclosure_url)


def _magazine(url: str) -> dict:
    """What the issue behind ``url`` holds, asked once and remembered for good."""
    hit = cache.get(_MAGAZINE_NS, url)
    if isinstance(hit, dict) and {"sku", "audio", "cover"} <= set(hit):
        return hit

    access_code = _resolve_access_code(url)
    sku = _fetch_sku(access_code)
    content = _fetch_content(sku)
    audio = _audio(content)
    if not audio:
        raise BayamError(f"No audio found for {sku}")

    magazine = {
        "sku": sku,
        "title": content["title"] or "",
        "audio": [list(pair) for pair in audio],
        "cover": _encode_uri(content["cover_url"]) if content["cover_url"] else None,
        "released": content["release_date_prev"],
        "number": content["magazine_number"],
        "collection": (content["collection"] or {}).get("name"),
    }
    cache.put(_MAGAZINE_NS, url, magazine)
    return magazine


def _resolve_access_code(url: str) -> str:
    """Follow the redirects of ``url`` and return the magazine access code."""
    try:
        with _open(url) as response:
            final_url = response.url
    except OSError as exc:
        raise BayamError(f"Could not open {url}: {exc}") from exc
    return _access_code_from_url(final_url)


def _access_code_from_url(url: str) -> str:
    """Return the access code from an ``https://app.bayam.tv/mag/<code>`` URL."""
    parts = [part for part in urlsplit(url).path.split("/") if part]
    if len(parts) != 2 or parts[0] != MAGAZINE_PATH:
        raise BayamError(f"Not a Bayam magazine link: {url}")
    return parts[1]


def _fetch_sku(access_code: str) -> str:
    """Return the SKU of the magazine belonging to ``access_code``."""
    query = urlencode({"accessCode": access_code})
    products = _get_json(f"{REST_API_BASE}product_access_codes?{query}")
    if not products:
        raise BayamError(f"Unknown access code: {access_code}")
    product = products[0]
    if product["type"] != MAGAZINE_TYPE:
        raise BayamError(
            f"Access code {access_code} is for a {product['type']}, "
            f"not a {MAGAZINE_TYPE}"
        )
    return product["sku"]


def _fetch_content(sku: str) -> dict:
    """Return the issue details, cover URL and tracks of the magazine ``sku``."""
    query = urlencode({"query": TRACKS_QUERY, "variables": json.dumps({"sku": sku})})
    result = _get_json(
        f"{CMS_GRAPHQL_URL}?{query}",
        headers={"Authorization": f"Bearer {CMS_TOKEN}"},
    )
    if result.get("errors"):
        raise BayamError(f"CMS query for {sku} failed: {result['errors']}")
    contents = result["data"]["mbbooks_audio_press_contents"]
    if not contents:
        raise BayamError(f"No audio found for {sku}")
    return contents[0]


def _audio(content: dict) -> list[tuple[str, str]]:
    """Return the name and (unsigned) mp3 URL of each audio file, in playing order."""
    audio = []
    for track in content["tracks"]:
        # A track is either a single file or split into chapters.
        if track["audio_url"]:
            audio.append((track["name"], track["audio_url"]))
        else:
            audio.extend(
                (chapter["name"], chapter["audio_url"])
                for chapter in track["chapters"]
                if chapter["audio_url"]
            )
    return audio


def _released(value: str | None) -> datetime | None:
    """Return a release date of the CMS as a time in Paris."""
    if not value:
        return None
    released = datetime.fromisoformat(value)
    if released.tzinfo is None:
        released = released.replace(tzinfo=timezone.utc)
    return released.astimezone(RELEASE_TIMEZONE)


def _clean_title(name: str) -> str:
    """Drop the track number Bayam prefixes some titles with (``01-Title``)."""
    return re.sub(r"^\d+\s*-\s*", "", name).strip()


def _fetch_signature(sku: str, audio_url: str) -> str:
    """Return the query string that grants access to all audio files of ``sku``."""
    query = urlencode({"client_secret": REST_API_APP_SECRET})
    token = _get_json(f"{REST_API_BASE}signature/token?{query}")["token"]
    result = _get_json(
        f"{HORIZON_API_BASE}rs/v1/skus/{sku}/signatures?url={_encode_uri(audio_url)}",
        headers={"Authorization": f"Bearer {token}"},
    )
    return result["signature"]


def _filename(url: str) -> str:
    """Return the file name ``url`` points to."""
    return os.path.basename(urlsplit(url).path)


def _encode_uri(url: str) -> str:
    """Encode ``url`` like JavaScript's ``encodeURI``."""
    return quote(url, safe=_ENCODE_URI_SAFE)


def _get_json(url: str, headers: dict[str, str] | None = None):
    headers = {"Accept": "application/json", **(headers or {})}
    try:
        with _open(url, headers=headers) as response:
            return json.load(response)
    except OSError as exc:
        raise BayamError(f"Could not read {urlsplit(url).netloc}: {exc}") from exc
    except ValueError as exc:
        raise BayamError(f"{urlsplit(url).netloc} answered with something not JSON") from exc


def _open(url: str, headers: dict[str, str] | None = None):
    request = Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    return urlopen(request, timeout=TIMEOUT)
