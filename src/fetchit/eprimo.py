"""Read a school's blog on e-Primo as a feed of its posts, photos and all.

e-Primo (``ent.e-primo.fr``) is the school platform of the Nantes académie's
primary schools, and a school's blog on it is for its families only::

    https://ent.e-primo.fr/blog/id/86566c46-40f9-4aed-a7a4-af2c3bdd84ea

A post's own link works as well, and reads as the blog it is in.

**Signing in.** A parent signs in to e-Primo through ÉduConnect, the ministry's
login: e-Primo sends the browser there, ÉduConnect asks for an identifier and a
password in an ordinary form, and sends the browser back with a signed note
that e-Primo trades for a session cookie. :func:`sign_in` does those same steps
and keeps *only* the e-Primo session, in a file of its own (see
:data:`SESSION_ENV`). The password is used for the one sign-in and kept
nowhere, so what a stolen file is worth is the blog and the rest of e-Primo
until the session ends — not the ÉduConnect account.

A session that has ended is not an error fetchit can put right by itself: the
feed stops with :class:`SignedOut`, which says to sign in again — with
``fetchit eprimo-login`` or from the page ``fetchit serve`` shows. Whoever
would rather never be asked can put the identifier and password in
:data:`USER_ENV` and :data:`PASSWORD_ENV`; then a session that has ended is
replaced without a word.

**Reading.** The blog answers in JSON to the calls its own page makes: one for
the blog (its title, its picture), one for its posts, newest first, a page at a
time. A post's text is HTML out of a rich-text editor — paragraphs, headings
and photos in the order they were written, in a handwriting font and colours
that are dropped here. It is kept as a **body**: a list of blocks, each a
heading, a paragraph of plain text, or a file (a photo, sometimes a video).
The files are behind the same session, and are downloaded with it when the
post is filed (:func:`fetch_body`), so the library holds the whole post and a
reader of it needs no session at all.

A post is a text item: nothing to play, filed under ``articles/``.
"""

from __future__ import annotations

import http.cookiejar
import json
import os
import re
import tempfile
import threading
from datetime import datetime, timezone
from html import unescape
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import HTTPCookieProcessor, Request, build_opener

from . import cache
from .download import download_to, fetch_text
from .feeds import FeedConfig, FeedError, FeedItem, ParsedFeed

SITE_URL = "https://ent.e-primo.fr"
HOST = "ent.e-primo.fr"
EDUCONNECT_HOST = "educonnect.education.gouv.fr"
# Where the "legal guardian" button of e-Primo's sign-in page leads.
SIGN_IN_URL = f"{SITE_URL}/auth/saml/authn/relative"

# The file the e-Primo session is kept in. Beside the cache when not set.
SESSION_ENV = "FETCHIT_EPRIMO_SESSION"
SESSION_FILE = "eprimo-session.json"
# For signing in again without being asked. Optional, and both or neither.
USER_ENV = "FETCHIT_EDUCONNECT_USER"
PASSWORD_ENV = "FETCHIT_EDUCONNECT_PASSWORD"

# ÉduConnect is asked the way a browser asks: its pages are written for one.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/18.0 Safari/605.1.15"
)
TIMEOUT = 30
# A sign-in is four or five pages; this is only so a loop of them ends.
MAX_STEPS = 8
# A backfill reads back a page at a time; this is far past a school year.
MAX_PAGES = 30

# The cookies that are the session. Anything else e-Primo sets is housekeeping.
SESSION_COOKIES = ("oneSessionId", "authenticated", "XSRF-TOKEN")

_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_BLOG_PATH_RE = re.compile(rf"^/blog/id/(?P<blog>{_UUID})(?:/post/(?P<post>{_UUID}))?/?$", re.I)

_FORM_RE = re.compile(r"<form\b(?P<attrs>[^>]*)>(?P<body>.*?)</form>", re.I | re.S)
_INPUT_RE = re.compile(r"<input\b[^>]*>", re.I)
_ATTR_RE = re.compile(r'([\w:.-]+)\s*=\s*"([^"]*)"')

_lock = threading.Lock()


class EprimoError(FeedError):
    """Raised when an e-Primo blog cannot be read."""


class SignedOut(EprimoError):
    """Raised when e-Primo wants a sign-in fetchit cannot do by itself."""


SIGN_IN_ADVICE = (
    "Sign in again: `fetchit eprimo-login`, or the e-Primo box on fetchit's page."
)


# --- the link ---------------------------------------------------------------


def blog_id(url: str | None) -> str | None:
    """The id of the blog ``url`` names — its own link or one of its posts' — or ``None``."""
    if not url:
        return None
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or (parts.hostname or "").lower() != HOST:
        return None
    match = _BLOG_PATH_RE.match(parts.path)
    return match.group("blog").lower() if match else None


def is_eprimo_link(url: str | None) -> bool:
    """Whether ``url`` is an e-Primo blog this module reads."""
    return blog_id(url) is not None


def is_eprimo_item(item: FeedItem) -> bool:
    """Whether ``item`` is a post read from an e-Primo blog."""
    return "eprimo_blog" in item.extra


def is_eprimo_file(url: str | None) -> bool:
    """Whether ``url`` is a file on e-Primo, which only a session can have."""
    return bool(url) and (urlsplit(url).hostname or "").lower() == HOST


# --- the session ------------------------------------------------------------


def session_path() -> str:
    """Where the session is kept: :data:`SESSION_ENV`, or beside the cache."""
    override = os.environ.get(SESSION_ENV, "").strip()
    if override:
        return os.path.expanduser(override)
    return os.path.join(os.path.dirname(cache.path()), SESSION_FILE)


def _load_session() -> dict | None:
    try:
        with open(session_path(), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    cookies = data.get("cookies") if isinstance(data, dict) else None
    if not isinstance(cookies, dict) or not cookies.get("oneSessionId"):
        return None
    return data


def _save_session(cookies: dict[str, str]) -> dict:
    """Write the session where only this user can read it."""
    data = {
        "cookies": cookies,
        "signed_in": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    path = session_path()
    folder = os.path.dirname(path) or "."
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".eprimo-")  # mkstemp: mode 0600
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return data


def sign_out() -> None:
    """Forget the session."""
    try:
        os.unlink(session_path())
    except OSError:
        pass


def signed_in_since() -> str | None:
    """When the kept session was made (ISO 8601), or ``None`` without one.

    Says nothing of whether e-Primo still honours it; :func:`check` asks.
    """
    session = _load_session()
    return str(session.get("signed_in") or "") or None if session else None


def has_stored_login() -> bool:
    """Whether the environment holds a login to sign in again with."""
    return bool(_stored_login())


def _stored_login() -> tuple[str, str] | None:
    user = os.environ.get(USER_ENV, "").strip()
    password = os.environ.get(PASSWORD_ENV, "")
    return (user, password) if user and password else None


def auth_headers(url: str | None = None) -> dict[str, str]:
    """The headers that make a request to e-Primo one of the kept session.

    Empty for an address that is not e-Primo's — the session is nobody else's
    business — and when there is no session.
    """
    if url is not None and not is_eprimo_file(url):
        return {}
    session = _load_session()
    if session is None:
        return {}
    cookie = "; ".join(f"{name}={value}" for name, value in session["cookies"].items())
    return {"Cookie": cookie, "User-Agent": USER_AGENT}


# --- signing in -------------------------------------------------------------


def _attrs(tag: str) -> dict[str, str]:
    return {name.lower(): unescape(value) for name, value in _ATTR_RE.findall(tag)}


def _forms(html: str) -> list[dict]:
    """Each form of a page: where it posts to, and the fields it would send."""
    found = []
    for match in _FORM_RE.finditer(html):
        fields = {}
        for tag in _INPUT_RE.findall(match.group("body")):
            attrs = _attrs(tag)
            if attrs.get("name") and attrs.get("type", "text").lower() not in ("submit", "button"):
                fields[attrs["name"]] = attrs.get("value", "")
        found.append({"action": _attrs(match.group("attrs")).get("action", ""), "fields": fields})
    return found


def _opener(jar: http.cookiejar.CookieJar):
    """What makes the sign-in's requests. Its own function so a test can stand in."""
    return build_opener(HTTPCookieProcessor(jar))


def _page(opener, url: str, data: dict | None = None) -> tuple[str, str]:
    """Ask for ``url`` (posting ``data`` if given). Returns ``(where it ended up, html)``."""
    body = urlencode(data).encode() if data is not None else None
    request = Request(url, data=body, headers={"User-Agent": USER_AGENT})
    try:
        with opener.open(request, timeout=TIMEOUT) as response:
            return response.geturl(), response.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        raise EprimoError(
            f"The sign-in stopped: {urlsplit(url).hostname} answered {exc.code}."
        ) from exc
    except (URLError, OSError) as exc:
        raise EprimoError(f"The sign-in stopped: {urlsplit(url).hostname} did not answer ({exc}).") from exc


def sign_in(username: str, password: str) -> str:
    """Sign in to e-Primo through ÉduConnect and keep the session. Returns when (ISO 8601).

    ``username`` and ``password`` are the ÉduConnect ones. Neither is kept, and
    neither appears in what this raises: :class:`EprimoError`, saying which step
    of the way it stopped at.
    """
    username = (username or "").strip()
    if not username or not password:
        raise EprimoError("An ÉduConnect identifier and password are both needed to sign in.")

    jar = http.cookiejar.CookieJar()
    opener = _opener(jar)
    url, html = _page(opener, SIGN_IN_URL)
    tried = False

    for _ in range(MAX_STEPS):
        host = (urlsplit(url).hostname or "").lower()
        forms = _forms(html)
        handover = next((f for f in forms if "SAMLResponse" in f["fields"]), None)
        login = next((f for f in forms if "j_username" in f["fields"]), None)
        # ÉduConnect's first page only asks what the browser remembers of it.
        storage = next(
            (f for f in forms if any(name.startswith("shib_idp_ls_") for name in f["fields"])), None
        )

        if host == HOST and handover is None:
            break  # back at e-Primo
        if handover is not None:
            url, html = _page(opener, urljoin(url, handover["action"]), handover["fields"])
        elif login is not None:
            if tried:
                raise EprimoError("ÉduConnect did not accept the identifier and password.")
            tried = True
            fields = dict(login["fields"])
            fields.update(j_username=username, j_password=password, _eventId_proceed="")
            url, html = _page(opener, urljoin(url, login["action"]), fields)
        elif storage is not None and host == EDUCONNECT_HOST:
            fields = dict(storage["fields"])
            fields["_eventId_proceed"] = ""
            url, html = _page(opener, urljoin(url, storage["action"]), fields)
        else:
            raise EprimoError(
                f"The sign-in stopped at a page fetchit does not know, on {host or 'nowhere'}. "
                "ÉduConnect or e-Primo may have changed how signing in goes."
            )
    else:
        raise EprimoError(f"The sign-in was still not done after {MAX_STEPS} pages.")

    cookies = {
        cookie.name: cookie.value
        for cookie in jar
        if cookie.name in SESSION_COOKIES and HOST.endswith(cookie.domain.lstrip("."))
        and cookie.value
    }
    if "oneSessionId" not in cookies:
        raise EprimoError("ÉduConnect accepted the sign-in, but e-Primo gave no session for it.")
    with _lock:
        return _save_session(cookies)["signed_in"]


def _sign_in_again() -> bool:
    """Replace an ended session from the stored login, if there is one."""
    login = _stored_login()
    if login is None:
        return False
    sign_in(*login)
    return True


# --- asking e-Primo ---------------------------------------------------------


def _ask(path: str):
    """The JSON e-Primo answers ``path`` with, as the kept session."""
    headers = auth_headers()
    if not headers:
        raise SignedOut(f"Not signed in to e-Primo. {SIGN_IN_ADVICE}")
    text = fetch_text(SITE_URL + path, timeout=TIMEOUT, headers=headers)
    try:
        return json.loads(text)
    except ValueError:
        # Asked without a session it honours, e-Primo sends its sign-in page.
        raise SignedOut(f"e-Primo has ended the session. {SIGN_IN_ADVICE}") from None


def _api(path: str):
    """:func:`_ask`, signing in again first where a stored login allows it."""
    try:
        return _ask(path)
    except SignedOut:
        if not _sign_in_again():
            raise
    return _ask(path)


def check() -> bool:
    """Whether e-Primo honours the kept session right now. Asks it."""
    try:
        return isinstance(_ask("/auth/oauth2/userinfo"), dict)
    except FeedError:
        return False


# --- a post's text ----------------------------------------------------------

_HEADINGS = ("h1", "h2", "h3", "h4", "h5", "h6")
# Where one paragraph ends and the next begins.
_BREAKS = ("p", "div", "blockquote", "ul", "ol", "table", "tr", "td", "th", "pre", "figure", "section")
_FILE_TAGS = {"img": "image", "video": "video", "audio": "audio"}
# What an editor leaves behind that is not text: zero-width spaces and joiners.
_INVISIBLE_RE = re.compile("[​‌‍⁠﻿]")
_SPACES_RE = re.compile(r"[ \t\r\f\v ]+")


class _Body(HTMLParser):
    """Turns a post's HTML into blocks — see :func:`body_blocks`."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[dict] = []
        self._text: list[str] = []
        self._heading = False
        self._link: str | None = None
        self._link_text: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        attributes = {name: value or "" for name, value in attrs}
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in _HEADINGS:
            self._flush()
            self._heading = True
        elif tag in _BREAKS:
            self._flush()
        elif tag == "li":
            self._flush()
            self._text.append("• ")
        elif tag == "br":
            self._text.append("\n")
        elif tag in _FILE_TAGS:
            source = attributes.get("src", "").strip()
            if source and not source.startswith("data:"):
                self._flush()
                self.blocks.append({"type": _FILE_TAGS[tag], "url": urljoin(SITE_URL, source)})
        elif tag == "iframe":
            source = attributes.get("src", "").strip()
            if source:
                self._flush()
                self.blocks.append({"type": "text", "text": urljoin(SITE_URL, source)})
        elif tag == "a":
            href = attributes.get("href", "").strip()
            self._link = urljoin(SITE_URL, href) if href and not href.startswith(("#", "javascript:")) else None
            self._link_text = []

    def handle_startendtag(self, tag: str, attrs) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
        elif tag in _HEADINGS:
            self._flush()
        elif tag in _BREAKS or tag == "li":
            self._flush()
        elif tag == "a" and self._link:
            shown = "".join(self._link_text).strip()
            # A link that shows its own address needs no second copy of it.
            if self._link not in shown and shown not in self._link:
                self._text.append(f" ({self._link})")
            elif not shown:
                self._text.append(self._link)
            self._link = None

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        self._text.append(data)
        if self._link is not None:
            self._link_text.append(data)

    def _flush(self) -> None:
        text = _INVISIBLE_RE.sub("", "".join(self._text))
        lines = [_SPACES_RE.sub(" ", line).strip() for line in text.split("\n")]
        text = "\n".join(line for line in lines if line)
        if text:
            self.blocks.append({"type": "heading" if self._heading else "text", "text": text})
        self._text = []
        self._heading = False

    def close(self) -> None:
        super().close()
        self._flush()


def body_blocks(html: str) -> tuple[dict, ...]:
    """A post's HTML as blocks, in the order it was written.

    Each block is ``{"type": "heading" | "text", "text": …}`` or ``{"type":
    "image" | "video" | "audio", "url": …}``. Text is plain: the editor's fonts,
    sizes and colours say nothing a chat message can repeat, and its empty
    paragraphs, put there for spacing, are dropped. A link keeps its address,
    in brackets after its words.
    """
    parser = _Body()
    parser.feed(html or "")
    parser.close()
    return tuple(parser.blocks)


def _plain(blocks: tuple[dict, ...]) -> str:
    """The text of a body, for a reader that takes a description and no more."""
    return "\n\n".join(block["text"] for block in blocks if "text" in block)


# --- the feed ---------------------------------------------------------------


def _date(value) -> datetime | None:
    """A date as e-Primo's database writes it: ``{"$date": "2026-11-10T17:56:07.124Z"}``."""
    if isinstance(value, dict):
        value = value.get("$date")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    if not isinstance(value, str) or not value:
        return None
    try:
        when = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def _published(post: dict) -> datetime | None:
    """When a post went up: when it was first published, not when it was begun."""
    for key in ("firstPublishDate", "modified", "created"):
        when = _date(post.get(key))
        if when is not None:
            return when
    return None


def _posts(blog: str, since: datetime | None) -> list[dict]:
    """The blog's posts, newest first: the first page, and back to ``since`` if given."""
    found: list[dict] = []
    for page in range(MAX_PAGES):
        batch = _api(
            f"/blog/post/list/all/{blog}?page={page}&content=true&comments=false&nbComments=false"
        )
        if not isinstance(batch, list):
            raise EprimoError("e-Primo answered with something other than a list of posts.")
        batch = [post for post in batch if isinstance(post, dict)]
        found += batch
        if not batch or since is None:
            break
        oldest = min((_published(p) for p in batch if _published(p)), default=None)
        if oldest is not None and oldest < since:
            break
    return found


def parse_eprimo(cfg: FeedConfig) -> ParsedFeed:
    """Read the blog behind ``cfg.url`` as a feed of its published posts, newest first."""
    blog = blog_id(cfg.url)
    if blog is None:
        raise EprimoError(f"Not an e-Primo blog: {cfg.url!r}")
    link = f"{SITE_URL}/blog/id/{blog}"

    about = _api(f"/blog/{blog}")
    about = about if isinstance(about, dict) else {}

    items: list[FeedItem] = []
    for post in _posts(blog, cfg.since):
        post_id = str(post.get("_id") or "")
        if not post_id or str(post.get("state") or "PUBLISHED") != "PUBLISHED":
            continue
        body = body_blocks(str(post.get("content") or ""))
        author = post.get("author") if isinstance(post.get("author"), dict) else {}
        first_picture = next((b["url"] for b in body if b["type"] == "image"), None)
        items.append(
            FeedItem(
                title=" ".join(str(post.get("title") or "").split()) or "(untitled)",
                description=_plain(body),
                link=f"{link}/post/{post_id}",
                published=_published(post),
                image_url=first_picture,
                author=" ".join(str(author.get("username") or "").split()) or None,
                feed_name=cfg.name,
                extra={"id": f"eprimo:{post_id}", "eprimo_blog": blog},
                body=body,
            )
        )

    picture = str(about.get("thumbnail") or "").strip()
    return ParsedFeed(
        items=items,
        image_url=urljoin(SITE_URL, picture) if picture else None,
        description=_plain(body_blocks(str(about.get("description") or ""))),
        title=" ".join(str(about.get("title") or "").split()) or cfg.name,
        link=link,
        language="fr",
    )


def blog_image(url: str) -> str | None:
    """The picture the blog behind ``url`` shows for itself, or ``None``."""
    blog = blog_id(url)
    if blog is None:
        return None
    about = _api(f"/blog/{blog}")
    picture = str(about.get("thumbnail") or "").strip() if isinstance(about, dict) else ""
    return urljoin(SITE_URL, picture) if picture else None


# --- a post's files ---------------------------------------------------------

# What a file is, by how it starts. A name on e-Primo is an id and says nothing.
_SIGNATURES = (
    (b"\xff\xd8\xff", 0, ".jpg"),
    (b"\x89PNG\r\n\x1a\n", 0, ".png"),
    (b"GIF8", 0, ".gif"),
    (b"WEBP", 8, ".webp"),
    (b"ftypqt", 4, ".mov"),
    (b"ftyp", 4, ".mp4"),
    (b"\x1a\x45\xdf\xa3", 0, ".webm"),
    (b"ID3", 0, ".mp3"),
    (b"OggS", 0, ".ogg"),
)
_DEFAULT_SUFFIX = {"image": ".jpg", "video": ".mp4", "audio": ".mp3"}


def _suffix(path: str) -> str | None:
    """The suffix for what ``path`` holds; ``None`` for a web page, ``""`` for the unknown."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(16)
    except OSError:
        return None
    for magic, offset, suffix in _SIGNATURES:
        if head[offset:offset + len(magic)] == magic:
            return suffix
    if head.lstrip()[:1] == b"<":
        return None
    return ""


def _gone(exc: FeedError) -> bool:
    """Whether a download failed because the file is not there any more."""
    cause = exc.__cause__
    return isinstance(cause, HTTPError) and cause.code in (403, 404, 410)


def _fetch_file(url: str, path: str) -> str | None:
    """Download one of a post's files to ``path``. Returns its suffix, or ``None`` if signed out."""
    download_to(url, path, timeout=TIMEOUT * 4, headers=auth_headers(url))
    return _suffix(path)


def fetch_body(item: FeedItem, into: str) -> list[dict]:
    """Download the files of ``item``'s body into ``into``.

    Returns the body with each file block's ``url`` replaced by the ``path`` it
    was downloaded to. A file that is gone from e-Primo — taken down after the
    post was written — is left out, with a line to say so; one that cannot be
    had *right now* raises, because a post without its photos is not the post,
    and the next run can do better.
    """
    out: list[dict] = []
    number = 0
    for block in item.body:
        url = block.get("url")
        if not url:
            out.append(dict(block))
            continue
        number += 1
        path = os.path.join(into, f"body-{number}")
        try:
            suffix = _fetch_file(url, path)
            if suffix is None and is_eprimo_file(url) and _sign_in_again():
                suffix = _fetch_file(url, path)
        except FeedError as exc:
            if _gone(exc):
                print(f"[{item.feed_name}] {item.title!r}: a file is gone, left out ({url})")
                continue
            raise
        if suffix is None:
            if is_eprimo_file(url):
                raise SignedOut(f"e-Primo has ended the session. {SIGN_IN_ADVICE}")
            raise EprimoError(f"{url} is a web page, not the {block['type']} the post shows.")
        named = path + (suffix or _DEFAULT_SUFFIX.get(block["type"], ".bin"))
        os.replace(path, named)
        out.append({"type": block["type"], "path": named})
    return out
