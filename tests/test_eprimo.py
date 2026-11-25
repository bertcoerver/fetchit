"""Tests for the e-Primo blog reader that do not need network access."""

import io
import json
import os
import stat
from datetime import datetime, timezone
from urllib.error import HTTPError

import pytest

from fetchit import cli, eprimo, library, media
from fetchit.eprimo import (
    EprimoError,
    SignedOut,
    blog_id,
    body_blocks,
    fetch_body,
    is_eprimo_link,
    parse_eprimo,
    sign_in,
)
from fetchit.feeds import FeedConfig, FeedError, FeedItem, ParsedFeed
from fetchit.library import prune, write_item
from fetchit.media import TEXT, item_id, kind_of

BLOG = "86566c46-40f9-4aed-a7a4-af2c3bdd84ea"
POST = "19cea87a-ad38-49f0-bbb4-39ee8f160431"
BLOG_URL = f"https://ent.e-primo.fr/blog/id/{BLOG}"
PHOTO = "https://ent.e-primo.fr/workspace/document/978dac38-f363-46d5-88dc-df68da1fd476?timestamp=1"
SIGN_IN_PAGE = "<!doctype html><html><body><div id='root'></div></body></html>"
JPEG = b"\xff\xd8\xff\xe0" + b"0" * 32

# A post as the blog's editor writes one: a font on everything, empty
# paragraphs for spacing, a photo inside the paragraph it follows.
CONTENT = (
    '<p><span style="font-family: Marelle">Dans le cadre du programme, l&apos;école a participé.</span></p>'
    '<p style="text-align: justify"><span style="font-family: Marelle">​</span></p>'
    '<h2><span style="color: rgb(100, 143, 255)"><strong><u>Atelier 1: débat</u></strong></span>'
    '<span style="font-family: Marelle">​</span></h2><p></p>'
    '<p><span>Les élèves ont échangé.</span>'
    '<img class="custom-image" src="/workspace/document/978dac38-f363-46d5-88dc-df68da1fd476?timestamp=1" width="500" height="NaN"></p>'
    '<p><br></p>'
    '<h2><span>Atelier 2: arts plastiques</span></h2>'
    '<p>Un arbre   collectif.</p>'
    '<p><img class="custom-image" src="/workspace/document/b" width="350">'
    '<img class="custom-image" src="/workspace/document/c" width="350"></p>'
    '<p>Voir <a href="https://example.org/album">l\'album</a>.</p>'
)


def _post(post_id=POST, title="Journée pHARe", content=CONTENT, published="2026-11-10T17:56:07.124Z", **extra):
    return {
        "_id": post_id,
        "title": title,
        "content": content,
        "created": {"$date": "2026-11-08T16:34:09.448Z"},
        "modified": {"$date": "2026-11-10T17:56:07.024Z"},
        "firstPublishDate": {"$date": published},
        "author": {"userId": "u", "username": "SACHOT ANNIE", "login": "annie.sachot3"},
        "state": "PUBLISHED",
        **extra,
    }


ABOUT = {"_id": BLOG, "title": "BLOG ECOLE LES ACANTHES", "description": "", "thumbnail": "/workspace/document/t"}


@pytest.fixture
def session(tmp_path, monkeypatch):
    """A kept session, in a file of this test's own."""
    path = tmp_path / "eprimo-session.json"
    monkeypatch.setenv(eprimo.SESSION_ENV, str(path))
    path.write_text(json.dumps({"cookies": {"oneSessionId": "abc", "authenticated": "true"},
                                "signed_in": "2026-11-25T15:16:00+00:00"}))
    return path


def _answers(monkeypatch, pages):
    """Answer e-Primo's calls from ``pages``: path prefix -> JSON-able or text."""
    asked = []

    def fake_fetch_text(url, *, timeout=30, headers=None):
        asked.append((url, headers or {}))
        path = url[len(eprimo.SITE_URL):]
        for prefix, answer in pages.items():
            if path.startswith(prefix):
                answer = answer(path) if callable(answer) else answer
                return answer if isinstance(answer, str) else json.dumps(answer)
        raise AssertionError(f"unexpected call {path}")

    monkeypatch.setattr(eprimo, "fetch_text", fake_fetch_text)
    return asked


# --- the link ---------------------------------------------------------------


@pytest.mark.parametrize(
    "url, expected",
    [
        (BLOG_URL, BLOG),
        (BLOG_URL + "/", BLOG),
        (f"{BLOG_URL}/post/{POST}", BLOG),
        (BLOG_URL.upper().replace("HTTPS://ENT.E-PRIMO.FR/BLOG/ID", "https://ent.e-primo.fr/blog/id"), BLOG),
        ("https://ent.e-primo.fr/blog", None),
        ("https://ent.e-primo.fr/timeline/timeline", None),
        (f"https://example.org/blog/id/{BLOG}", None),
        (f"ftp://ent.e-primo.fr/blog/id/{BLOG}", None),
        ("", None),
        (None, None),
    ],
)
def test_a_blog_is_known_by_its_link_or_a_posts(url, expected):
    assert blog_id(url) == expected
    assert is_eprimo_link(url) is (expected is not None)


def test_a_blog_is_not_a_line_for_a_link_list():
    assert media.link_source(BLOG_URL) is None


# --- a post's text ----------------------------------------------------------


def test_a_post_becomes_its_headings_paragraphs_and_photos_in_order():
    assert body_blocks(CONTENT) == (
        {"type": "text", "text": "Dans le cadre du programme, l'école a participé."},
        {"type": "heading", "text": "Atelier 1: débat"},
        {"type": "text", "text": "Les élèves ont échangé."},
        {"type": "image", "url": PHOTO},
        {"type": "heading", "text": "Atelier 2: arts plastiques"},
        {"type": "text", "text": "Un arbre collectif."},
        {"type": "image", "url": "https://ent.e-primo.fr/workspace/document/b"},
        {"type": "image", "url": "https://ent.e-primo.fr/workspace/document/c"},
        {"type": "text", "text": "Voir l'album (https://example.org/album)."},
    )


def test_a_video_a_list_and_a_line_break_are_kept():
    html = (
        '<div class="video-wrapper"><video src="/workspace/document/v" controls="true"></video></div>'
        "<ul><li>Un</li><li>Deux</li></ul>"
        "<p>Ligne une<br>ligne deux</p>"
        '<p><a href="https://example.org/x">https://example.org/x</a></p>'
        '<p><img alt=""></p><script>alert(1)</script>'
    )
    assert body_blocks(html) == (
        {"type": "video", "url": "https://ent.e-primo.fr/workspace/document/v"},
        {"type": "text", "text": "• Un"},
        {"type": "text", "text": "• Deux"},
        {"type": "text", "text": "Ligne une\nligne deux"},
        {"type": "text", "text": "https://example.org/x"},
    )


def test_an_empty_post_has_no_body():
    assert body_blocks("") == ()
    assert body_blocks("<p>​</p><p><br></p>") == ()


# --- the feed ---------------------------------------------------------------


def test_a_blog_is_read_as_its_published_posts(session, monkeypatch):
    draft = _post("11111111-1111-1111-1111-111111111111", title="Brouillon", state="DRAFT")
    asked = _answers(monkeypatch, {"/blog/post/list/all/": [_post(), draft], f"/blog/{BLOG}": ABOUT})

    parsed = parse_eprimo(FeedConfig(url=f"{BLOG_URL}/post/{POST}", name="Blog Les Acanthes"))

    assert parsed.title == "BLOG ECOLE LES ACANTHES"
    assert parsed.link == BLOG_URL
    assert parsed.image_url == "https://ent.e-primo.fr/workspace/document/t"
    (item,) = parsed.items
    assert item.title == "Journée pHARe"
    assert item.link == f"{BLOG_URL}/post/{POST}"
    assert item.published == datetime(2026, 11, 10, 17, 56, 7, 124000, tzinfo=timezone.utc)
    assert item.author == "SACHOT ANNIE"
    assert item.image_url == PHOTO
    assert item.description.startswith("Dans le cadre du programme")
    assert "Atelier 2: arts plastiques\n\nUn arbre collectif." in item.description
    assert [b["type"] for b in item.body].count("image") == 3
    assert kind_of(item) == TEXT
    assert item_id(item) == f"eprimo:{POST}"
    # Asked as the kept session, and one page of posts is enough without `since`.
    assert all("oneSessionId=abc" in headers["Cookie"] for _url, headers in asked)
    assert sum("/blog/post/list/all/" in url for url, _ in asked) == 1


def test_a_backfill_reads_back_to_its_date(session, monkeypatch):
    pages = {
        "0": [_post("00000000-0000-0000-0000-000000000001", published="2026-11-10T10:00:00Z")],
        "1": [_post("00000000-0000-0000-0000-000000000002", published="2026-09-10T10:00:00Z")],
        "2": [_post("00000000-0000-0000-0000-000000000003", published="2026-06-10T10:00:00Z")],
    }
    asked = _answers(monkeypatch, {
        "/blog/post/list/all/": lambda path: pages[path.split("page=")[1].split("&")[0]],
        f"/blog/{BLOG}": ABOUT,
    })
    since = datetime(2026, 10, 1, tzinfo=timezone.utc)

    parsed = parse_eprimo(FeedConfig(url=BLOG_URL, name="Blog", since=since))

    # Page 1 reached back past the date, so page 2 was never asked for.
    assert len(parsed.items) == 2
    assert sum("/blog/post/list/all/" in url for url, _ in asked) == 2


def test_it_is_the_front_door_for_a_blog_link(session, monkeypatch):
    _answers(monkeypatch, {"/blog/post/list/all/": [_post()], f"/blog/{BLOG}": ABOUT})
    parsed = media.collection_feed(FeedConfig(url=BLOG_URL, name="Blog"))
    assert parsed is not None and len(parsed.items) == 1
    assert media.source_image(BLOG_URL) == "https://ent.e-primo.fr/workspace/document/t"


def test_without_a_session_the_feed_says_to_sign_in(tmp_path, monkeypatch):
    monkeypatch.setenv(eprimo.SESSION_ENV, str(tmp_path / "none.json"))
    with pytest.raises(SignedOut, match="eprimo-login"):
        parse_eprimo(FeedConfig(url=BLOG_URL, name="Blog"))


def test_a_session_that_has_ended_says_so(session, monkeypatch):
    _answers(monkeypatch, {"/blog/": SIGN_IN_PAGE, "/auth/": SIGN_IN_PAGE})
    with pytest.raises(SignedOut, match="ended the session"):
        parse_eprimo(FeedConfig(url=BLOG_URL, name="Blog"))
    assert eprimo.check() is False


def test_a_stored_login_signs_in_again_without_a_word(session, monkeypatch):
    monkeypatch.setenv(eprimo.USER_ENV, "p.nom12")
    monkeypatch.setenv(eprimo.PASSWORD_ENV, "s3cret")
    signed = []

    def fake_sign_in(user, password):
        signed.append((user, password))

    monkeypatch.setattr(eprimo, "sign_in", fake_sign_in)
    _answers(monkeypatch, {
        "/blog/post/list/all/": [_post()],
        f"/blog/{BLOG}": lambda path: ABOUT if signed else SIGN_IN_PAGE,
    })

    parsed = parse_eprimo(FeedConfig(url=BLOG_URL, name="Blog"))

    assert signed == [("p.nom12", "s3cret")]
    assert len(parsed.items) == 1
    assert eprimo.has_stored_login()


def test_the_session_is_only_for_e_primo(session):
    assert "Cookie" in eprimo.auth_headers(PHOTO)
    assert eprimo.auth_headers("https://example.org/a.jpg") == {}
    assert media.request_headers("https://example.org/a.jpg") == {}
    assert "oneSessionId=abc" in media.request_headers(PHOTO)["Cookie"]


# --- signing in -------------------------------------------------------------

EDU = "https://educonnect.education.gouv.fr/idp/profile/SAML2/Redirect/SSO"
STORAGE_PAGE = (
    '<form name="form1" action="/idp/profile/SAML2/Redirect/SSO?execution=e1s1" method="post">'
    '<input type="hidden" name="csrf_token" value="_t1" />'
    '<input name="shib_idp_ls_supported" type="hidden" />'
    '<input name="_eventId_proceed" type="hidden" /><input type="submit" value="Continue" /></form>'
)
LOGIN_PAGE = (
    '<script src="/idp-static/js/captchetat-js.js"></script>'
    '<form id="validerAuth" action="/idp/profile/SAML2/Redirect/SSO?execution=e1s2" method="post">'
    '<input type="hidden" name="csrf_token" value="_t2" />'
    '<input type="text" id="username" name="j_username" /><input type="password" name="j_password" />'
    '<button type="submit" name="_eventId_proceed">Se connecter</button></form>'
)
HANDOVER_PAGE = (
    '<form action="https://ent.e-primo.fr/auth/saml/acs" method="post">'
    '<input type="hidden" name="RelayState" value="r"/><input type="hidden" name="SAMLResponse" value="PHNhbWw+"/></form>'
)


class _Response(io.BytesIO):
    def __init__(self, url, html):
        super().__init__(html.encode())
        self._url = url

    def geturl(self):
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Cookie:
    def __init__(self, name, value, domain="ent.e-primo.fr"):
        self.name, self.value, self.domain = name, value, domain


def _fake_browser(monkeypatch, *, accepts=True, cookies=True):
    """Stand in for ÉduConnect and e-Primo. Returns what was posted, in order."""
    posted = []

    class Opener:
        def __init__(self, jar):
            self.jar = jar

        def open(self, request, timeout=None):
            url = request.full_url
            fields = dict(pair.split("=", 1) for pair in request.data.decode().split("&")) if request.data else None
            posted.append((url, fields))
            if url == eprimo.SIGN_IN_URL:
                return _Response(f"{EDU}?execution=e1s1", STORAGE_PAGE)
            if url.endswith("e1s1"):
                return _Response(f"{EDU}?execution=e1s2", LOGIN_PAGE)
            if url.endswith("e1s2"):
                if not accepts:
                    return _Response(f"{EDU}?execution=e1s3", LOGIN_PAGE)
                return _Response(f"{EDU}?execution=e1s3", HANDOVER_PAGE)
            if url == "https://ent.e-primo.fr/auth/saml/acs":
                if cookies:
                    self.jar.extend([_Cookie("oneSessionId", "fresh"), _Cookie("authenticated", "true"),
                                     _Cookie("webviewignored", "x"), _Cookie("JSESSIONID", "edu", EDUCONNECT)])
                return _Response("https://ent.e-primo.fr/timeline/timeline", "<html>Fil de nouveautés</html>")
            raise AssertionError(f"unexpected request {url}")

    monkeypatch.setattr(eprimo, "_opener", Opener)
    monkeypatch.setattr(eprimo.http.cookiejar, "CookieJar", list)
    return posted


EDUCONNECT = "educonnect.education.gouv.fr"


def test_signing_in_keeps_the_session_and_nothing_else(tmp_path, monkeypatch):
    path = tmp_path / "state" / "eprimo-session.json"
    monkeypatch.setenv(eprimo.SESSION_ENV, str(path))
    posted = _fake_browser(monkeypatch)

    when = sign_in(" p.nom12 ", "s3cret")

    kept = json.loads(path.read_text())
    assert kept == {"cookies": {"oneSessionId": "fresh", "authenticated": "true"}, "signed_in": when}
    assert "s3cret" not in path.read_text() and "p.nom12" not in path.read_text()
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert eprimo.signed_in_since() == when
    # The form went back as it came, with the two answers filled in.
    login = next(fields for url, fields in posted if url.endswith("e1s2"))
    assert login == {"csrf_token": "_t2", "j_username": "p.nom12", "j_password": "s3cret", "_eventId_proceed": ""}
    assert posted[-1] == ("https://ent.e-primo.fr/auth/saml/acs", {"RelayState": "r", "SAMLResponse": "PHNhbWw%2B"})


def test_a_refused_password_is_said_without_repeating_it(tmp_path, monkeypatch):
    monkeypatch.setenv(eprimo.SESSION_ENV, str(tmp_path / "eprimo-session.json"))
    _fake_browser(monkeypatch, accepts=False)

    with pytest.raises(EprimoError, match="did not accept") as raised:
        sign_in("p.nom12", "s3cret")

    assert "s3cret" not in str(raised.value)
    assert eprimo.signed_in_since() is None


def test_a_sign_in_that_gives_no_session_is_not_kept(tmp_path, monkeypatch):
    monkeypatch.setenv(eprimo.SESSION_ENV, str(tmp_path / "eprimo-session.json"))
    _fake_browser(monkeypatch, cookies=False)
    with pytest.raises(EprimoError, match="gave no session"):
        sign_in("p.nom12", "s3cret")
    assert eprimo.signed_in_since() is None


def test_signing_in_needs_both_answers():
    with pytest.raises(EprimoError, match="both needed"):
        sign_in("p.nom12", "")


def test_a_page_nobody_knows_stops_the_sign_in(tmp_path, monkeypatch):
    monkeypatch.setenv(eprimo.SESSION_ENV, str(tmp_path / "eprimo-session.json"))

    class Opener:
        def open(self, request, timeout=None):
            return _Response(f"{EDU}?execution=e1s1", "<html>Maintenance</html>")

    monkeypatch.setattr(eprimo, "_opener", lambda jar: Opener())
    with pytest.raises(EprimoError, match="does not know"):
        sign_in("p.nom12", "s3cret")


def test_the_session_can_be_forgotten(session):
    assert eprimo.signed_in_since() == "2026-11-25T15:16:00+00:00"
    eprimo.sign_out()
    assert eprimo.signed_in_since() is None
    eprimo.sign_out()  # twice is fine


# --- a post's files ---------------------------------------------------------


def _item(body):
    return FeedItem(
        title="Journée pHARe", description="texte", link=f"{BLOG_URL}/post/{POST}",
        published=datetime(2026, 11, 10, 17, 56, tzinfo=timezone.utc), feed_name="Blog",
        extra={"id": f"eprimo:{POST}", "eprimo_blog": BLOG}, body=tuple(body),
    )


def _downloads(monkeypatch, files):
    """Answer downloads from ``files``: url -> bytes, or an exception to raise."""
    asked = []

    def fake_download_to(url, path, *, timeout=30, headers=None):
        asked.append((url, headers or {}))
        answer = files[url]
        if isinstance(answer, Exception):
            raise FeedError(f"Could not download {url!r}") from answer
        with open(path, "wb") as fh:
            fh.write(answer)

    monkeypatch.setattr(eprimo, "download_to", fake_download_to)
    return asked


def test_a_posts_files_are_downloaded_as_the_session_and_named_for_what_they_are(session, monkeypatch, tmp_path):
    video = "https://ent.e-primo.fr/workspace/document/v"
    outside = "https://example.org/pic"
    asked = _downloads(monkeypatch, {
        PHOTO: JPEG,
        video: b"\x00\x00\x00\x20ftypisom" + b"0" * 16,
        outside: b"\x89PNG\r\n\x1a\n" + b"0" * 16,
    })
    item = _item([
        {"type": "text", "text": "Un"}, {"type": "image", "url": PHOTO},
        {"type": "video", "url": video}, {"type": "image", "url": outside},
    ])

    body = media.fetch_body(item, str(tmp_path))

    assert body == [
        {"type": "text", "text": "Un"},
        {"type": "image", "path": str(tmp_path / "body-1.jpg")},
        {"type": "video", "path": str(tmp_path / "body-2.mp4")},
        {"type": "image", "path": str(tmp_path / "body-3.png")},
    ]
    assert all(os.path.isfile(b["path"]) for b in body if "path" in b)
    by_url = dict(asked)
    assert "oneSessionId=abc" in by_url[PHOTO]["Cookie"]
    assert by_url[outside] == {}  # nobody else is shown the session


def test_a_sign_in_page_in_place_of_a_photo_stops_the_post(session, monkeypatch, tmp_path):
    _downloads(monkeypatch, {PHOTO: SIGN_IN_PAGE.encode()})
    with pytest.raises(SignedOut):
        fetch_body(_item([{"type": "image", "url": PHOTO}]), str(tmp_path))


def test_a_photo_taken_down_is_left_out(session, monkeypatch, tmp_path, capsys):
    gone = HTTPError(PHOTO, 404, "Not Found", None, None)
    other = "https://ent.e-primo.fr/workspace/document/b"
    _downloads(monkeypatch, {PHOTO: gone, other: JPEG})

    body = fetch_body(_item([{"type": "image", "url": PHOTO}, {"type": "image", "url": other}]), str(tmp_path))

    assert body == [{"type": "image", "path": str(tmp_path / "body-2.jpg")}]
    assert "left out" in capsys.readouterr().out


def test_a_photo_that_fails_for_now_stops_the_post(session, monkeypatch, tmp_path):
    _downloads(monkeypatch, {PHOTO: TimeoutError("slow")})
    with pytest.raises(FeedError):
        fetch_body(_item([{"type": "image", "url": PHOTO}]), str(tmp_path))


def test_an_item_without_a_body_has_nothing_to_fetch(tmp_path):
    plain = FeedItem(title="T", description="d", link="https://a/1")
    assert media.fetch_body(plain, str(tmp_path)) == []


# --- in the library ---------------------------------------------------------


def test_a_post_is_filed_with_its_photos_beside_its_record(tmp_path, session, monkeypatch):
    root = str(tmp_path / "library")
    cfg = FeedConfig(url=BLOG_URL, name="Blog Les Acanthes")
    _downloads(monkeypatch, {PHOTO: JPEG, "https://ent.e-primo.fr/workspace/document/b": JPEG + b"2"})
    item = _item([
        {"type": "heading", "text": "Atelier 1"}, {"type": "text", "text": "Un débat."},
        {"type": "image", "url": PHOTO}, {"type": "text", "text": "Puis."},
        {"type": "image", "url": "https://ent.e-primo.fr/workspace/document/b"},
    ])
    staging = library.staging_dir(root)
    body = fetch_body(item, staging)

    path = write_item(root, cfg, ParsedFeed(items=[]), item, TEXT, item_id=item_id(item), body=body)

    folder = os.path.dirname(path)
    stem = "2026-11-10 - Journée pHARe"
    assert os.path.basename(path) == stem + ".json"
    assert os.path.dirname(path).endswith(os.path.join("articles", "Blog Les Acanthes"))
    with open(path, encoding="utf-8") as fh:
        record = json.load(fh)
    assert record["kind"] == "text"
    assert record["image"] == f"{stem}-1.jpg"
    assert record["body"] == [
        {"type": "heading", "text": "Atelier 1"}, {"type": "text", "text": "Un débat."},
        {"type": "image", "file": f"{stem}-1.jpg"}, {"type": "text", "text": "Puis."},
        {"type": "image", "file": f"{stem}-2.jpg"},
    ]
    assert sorted(os.listdir(folder)) == [f"{stem}-1.jpg", f"{stem}-2.jpg", f"{stem}.json"]
    assert [n for n in os.listdir(staging) if n.startswith("body-")] == []

    # Pruned, the photos go with the record.
    assert prune(root, {"Blog Les Acanthes": 1}, now=datetime(2030, 1, 1).timestamp()) == 1
    assert os.listdir(folder) == []


def test_an_item_without_a_body_is_filed_as_before(tmp_path):
    root = str(tmp_path / "library")
    item = FeedItem(title="T", description="d", link="https://a/1",
                    published=datetime(2026, 11, 10, tzinfo=timezone.utc))
    path = write_item(root, FeedConfig(url="https://a/feed", name="A"), ParsedFeed(items=[]), item, TEXT, item_id="x")
    with open(path, encoding="utf-8") as fh:
        assert "body" not in json.load(fh)


# --- the command ------------------------------------------------------------


def test_the_command_says_when_nobody_is_signed_in(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(eprimo.SESSION_ENV, str(tmp_path / "none.json"))
    assert cli.main(["eprimo-login", "--status"]) == 1
    assert "Not signed in" in capsys.readouterr().out


def test_the_command_signs_in_with_what_it_is_told(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(eprimo.SESSION_ENV, str(tmp_path / "eprimo-session.json"))
    seen = []
    monkeypatch.setattr(eprimo, "sign_in", lambda user, password: seen.append((user, password)))
    monkeypatch.setattr("getpass.getpass", lambda prompt: "s3cret")

    assert cli.main(["eprimo-login", "--user", "p.nom12"]) == 0

    assert seen == [("p.nom12", "s3cret")]
    out = capsys.readouterr().out
    assert "password is not kept" in out and "s3cret" not in out


def test_the_command_reports_a_refused_sign_in(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(eprimo.SESSION_ENV, str(tmp_path / "eprimo-session.json"))

    def refuse(user, password):
        raise EprimoError("ÉduConnect did not accept the identifier and password.")

    monkeypatch.setattr(eprimo, "sign_in", refuse)
    monkeypatch.setattr("getpass.getpass", lambda prompt: "wrong")
    assert cli.main(["eprimo-login", "--user", "p.nom12"]) == 1
    assert "did not accept" in capsys.readouterr().err
