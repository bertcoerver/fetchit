"""Tests for fetchit.server (runs are faked; HTTP is real, on localhost)."""

import json
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from fetchit import server
from fetchit.feeds import FeedConfig, FeedItem, ParsedFeed
from fetchit.library import write_item
from fetchit.run import Summary
from fetchit.server import Service, next_slot, parse_schedule


def test_parse_schedule_and_next_slot():
    tz = timezone(timedelta(hours=1))
    slots = parse_schedule("14:30, 01:00")
    assert slots == [(1, 0), (14, 30)]
    assert next_slot(slots, datetime(2026, 11, 7, 15, 0, tzinfo=tz)) == datetime(2026, 11, 8, 1, 0, tzinfo=tz)
    with pytest.raises(ValueError):
        parse_schedule("noon")


@pytest.fixture
def running(tmp_path):
    library = tmp_path / "library"
    config = tmp_path / "fetchit.json"
    config.write_text(json.dumps({
        "library": str(library),
        "keep_days": 30,
        "feeds": [{"name": "Blog", "url": "https://blog.example/feed"},
                  {"name": "Channel", "url": "https://www.youtube.com/@x", "since": "2020-01-01",
                   "quality": {"part_size_mb": 95}}],
    }))
    started = []

    def start(**kwargs):
        service = Service(str(config), **kwargs)
        httpd = server.make_server(service, "127.0.0.1", 0)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        service.start()
        started.append((httpd, service))
        return f"http://127.0.0.1:{httpd.server_address[1]}", service

    yield start, library
    for httpd, service in started:
        httpd.shutdown()
        httpd.server_close()
        service.stop()


def _call(base, path, body=None, method=None):
    data = json.dumps(body).encode() if body is not None else None
    request = Request(base + path, data=data, headers={"Content-Type": "application/json"}, method=method)
    try:
        with urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read() or b"{}")
    except HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def _wait(base, job):
    for _ in range(200):
        status, job = _call(base, f"/api/jobs/{job['id']}")
        if not job["running"]:
            return job
        time.sleep(0.02)
    raise AssertionError("job never finished")


def test_page_and_status(running):
    start, library = running
    base, _ = start(schedule=[(6, 0)])
    with urlopen(base + "/", timeout=10) as response:
        assert "<title>fetchit</title>" in response.read().decode()
    status, info = _call(base, "/api/status")
    assert status == 200
    assert info["library"] == str(library)
    assert info["feeds"] == 2 and info["keep_days"] == 30
    assert info["schedule"] == ["06:00"] and info["next_run"]


def test_feeds_show_what_the_library_holds(running, tmp_path):
    start, library = running
    item = FeedItem(title="A post", description="", link="https://blog.example/1",
                    published=datetime(2026, 11, 6, tzinfo=timezone.utc))
    write_item(str(library), FeedConfig(url="https://blog.example/feed", name="Blog"),
               ParsedFeed(items=[]), item, "text", item_id="1")
    base, _ = start()
    status, reply = _call(base, "/api/feeds")
    blog, channel = reply["feeds"]
    assert blog["items"] == 1 and blog["newest"] == "A post" and blog["kinds"] == ["articles"]
    assert channel["since"] == "2020-01-01" and "95 MB parts" in channel["quality"]
    _, recent = _call(base, "/api/recent")
    assert recent["recent"][0]["title"] == "A post"


def test_a_run_from_the_gui_reports_what_it_printed(running, monkeypatch):
    start, _ = running
    asked = []

    def fake_run(config, *, dry_run=False, only=None):
        asked.append((dry_run, only))
        print("[Blog] 2026-11-06  A post")
        return Summary(fetched=1)

    monkeypatch.setattr(server, "run", fake_run)
    base, _ = start()
    status, job = _call(base, "/api/run", {"dry_run": True, "feeds": ["Blog"]})
    assert status == 202
    job = _wait(base, job)
    assert job["ok"] and job["result"] == "1 item(s) would be fetched."
    assert "[Blog] 2026-11-06  A post" in job["output"]
    assert asked == [(True, ["Blog"])]


def test_one_job_at_a_time(running, monkeypatch):
    start, _ = running
    release = threading.Event()

    def slow_run(config, *, dry_run=False, only=None):
        release.wait(5)
        return Summary()

    monkeypatch.setattr(server, "run", slow_run)
    base, _ = start()
    _, first = _call(base, "/api/run", {})
    _, second = _call(base, "/api/prune", {})
    assert second["id"] == first["id"]
    release.set()
    _wait(base, first)


def test_the_gui_is_only_for_its_own_addresses(running):
    start, _ = running
    base, _ = start(gui_from="10.9.9.9")
    assert _call(base, "/api/status")[0] == 403


def test_a_broken_config_is_shown_not_fatal(running, tmp_path):
    start, _ = running
    base, service = start()
    (tmp_path / "fetchit.json").write_text("{nope")
    status, info = _call(base, "/api/status")
    assert status == 200 and "not valid JSON" in info["problem"]


def test_the_panel_edits_the_config_file(running, tmp_path):
    start, _ = running
    base, _ = start()
    config = tmp_path / "fetchit.json"
    before = config.read_text()

    status, view = _call(base, "/api/config")
    assert status == 200 and view["text"] == before and view["exists"] and view["version"]

    data = json.loads(before)
    data["feeds"].append({"name": "Podcast", "url": "https://podcast.example/feed.xml"})
    text = json.dumps(data, indent=2)
    status, saved = _call(base, "/api/config", {"text": text, "version": view["version"]}, "PUT")
    assert status == 200, saved
    assert saved["feeds"] == 3 and saved["version"] != view["version"]
    assert config.read_text() == text + "\n"
    assert (tmp_path / "fetchit.json.bak").read_text() == before
    _, feeds = _call(base, "/api/feeds")
    assert [f["name"] for f in feeds["feeds"]][-1] == "Podcast"
    _, activity = _call(base, "/api/activity")
    assert activity["activity"][0]["kind"] == "config"
    assert not list(tmp_path.glob(".fetchit-*"))


@pytest.mark.parametrize("text, says", [
    ("{\"feeds\": [", "not valid JSON"),
    ('{"feeds": [], "colour": 1}', "unknown key"),
    ('{"feeds": [{"url": "https://x.example/feed"}]}', "name"),
    ("   ", "empty"),
])
def test_a_config_a_run_would_refuse_is_not_saved(running, tmp_path, text, says):
    start, _ = running
    base, _ = start()
    config = tmp_path / "fetchit.json"
    before = config.read_text()
    _, view = _call(base, "/api/config")
    status, answer = _call(base, "/api/config", {"text": text, "version": view["version"]}, "PUT")
    assert status == 422
    assert says.lower() in answer["error"].lower()
    assert ".fetchit-" not in answer["error"]
    assert config.read_text() == before
    assert not (tmp_path / "fetchit.json.bak").exists()
    assert not list(tmp_path.glob(".fetchit-*"))


def test_an_edit_made_elsewhere_is_not_overwritten(running, tmp_path):
    start, _ = running
    base, _ = start()
    config = tmp_path / "fetchit.json"
    _, view = _call(base, "/api/config")
    config.write_text(json.dumps({"feeds": []}))  # the File Editor, meanwhile
    status, answer = _call(base, "/api/config", {"text": view["text"], "version": view["version"]}, "PUT")
    assert status == 409 and "changed" in answer["error"]
    assert json.loads(config.read_text()) == {"feeds": []}


def test_a_first_config_can_be_written_from_the_panel(running, tmp_path):
    start, _ = running
    base, _ = start()
    config = tmp_path / "fetchit.json"
    config.unlink()
    _, view = _call(base, "/api/config")
    assert view == {"path": str(config), "text": "", "version": "", "exists": False}
    status, saved = _call(base, "/api/config", {"text": '{"feeds": []}', "version": ""}, "PUT")
    assert status == 200 and saved["exists"] and saved["feeds"] == 0


def test_the_panel_makes_a_link_list_and_adds_to_it(running, tmp_path):
    start, _ = running
    base, _ = start()
    config = tmp_path / "fetchit.json"
    before = config.read_text()
    one, two, three = (f"https://flyer.bayam.tv/4sYB/{code}" for code in ("dj8", "pmo", "3v2"))

    # A feed that isn't there yet is made, with its links in a file, kept for good.
    status, answer = _call(base, "/api/links", {"feed": "Mes premières Histoires", "links": [one]})
    assert status == 200, answer
    assert answer == {"feed": "Mes premières Histoires", "added": [one], "already": 0, "created": True,
                      "file": "lists/mes-premieres-histoires.txt"}
    assert json.loads(config.read_text())["feeds"][-1] == {
        "name": "Mes premières Histoires", "urls_file": "lists/mes-premieres-histoires.txt", "keep_days": 0}
    links = tmp_path / "lists" / "mes-premieres-histoires.txt"
    assert links.read_text() == one + "\n"
    assert (tmp_path / "fetchit.json.bak").read_text() == before

    # A new issue is one more line in the file; the config is not touched.
    saved = config.read_text()
    links.write_text(f"# September\n{one}\n")
    status, answer = _call(base, "/api/links", {"feed": "Mes premières Histoires", "links": [two, one, f" {three} "]})
    assert status == 200, answer
    assert answer["added"] == [two, three] and answer["already"] == 1 and not answer["created"]
    assert links.read_text() == f"# September\n{one}\n{two}\n{three}\n"
    assert config.read_text() == saved

    _, feeds = _call(base, "/api/feeds")
    assert [(f["name"], f["urls"], f["list"]) for f in feeds["feeds"]] == [
        ("Blog", 1, None), ("Channel", 1, None),
        ("Mes premières Histoires", 3, {"file": "lists/mes-premieres-histoires.txt", "links": [one, two, three]})]

    # Taking one out keeps the rest, and the notes.
    status, answer = _call(base, "/api/links/remove", {"feed": "Mes premières Histoires", "links": [two]})
    assert status == 200 and answer["removed"] == [two]
    assert links.read_text() == f"# September\n{one}\n{three}\n"


def test_saved_for_later_is_a_link_list_of_single_videos(running, tmp_path):
    start, _ = running
    base, _ = start()
    video = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    episode = "https://npo.nl/start/afspelen/hijskraan_1"
    programme = "https://www.arte.tv/fr/videos/127395-115-A/le-dessous-des-images/"

    # However it was pasted, a video is saved the one plain way, and once.
    status, answer = _call(base, "/api/links", {"feed": "Saved for later", "links": [
        "https://youtu.be/dQw4w9WgXcQ?si=abc", video + "&t=42s", episode]})
    assert status == 200, answer
    assert answer == {"feed": "Saved for later", "added": [video, episode], "already": 0, "created": True,
                      "file": "lists/saved-for-later.txt"}
    assert json.loads((tmp_path / "fetchit.json").read_text())["feeds"][-1] == {
        "name": "Saved for later", "urls_file": "lists/saved-for-later.txt", "keep_days": 0}

    status, answer = _call(base, "/api/links", {"feed": "Saved for later", "links": [
        "https://www.youtube.com/shorts/dQw4w9WgXcQ", programme]})
    assert status == 200 and answer["added"] == [programme] and answer["already"] == 1
    assert (tmp_path / "lists" / "saved-for-later.txt").read_text() == f"{video}\n{episode}\n{programme}\n"


def test_two_link_lists_are_two_feeds(running, tmp_path):
    start, _ = running
    base, _ = start()
    _call(base, "/api/links", {"feed": "Histoires", "links": ["https://flyer.bayam.tv/4sYB/dj8"]})
    _call(base, "/api/links", {"feed": "Talks", "links": ["https://www.youtube.com/@talks"]})
    _, feeds = _call(base, "/api/feeds")
    assert [f["list"]["file"] for f in feeds["feeds"] if f["list"]] == ["lists/histoires.txt", "lists/talks.txt"]


def test_a_feed_with_links_in_the_config_has_them_moved_to_a_file(running, tmp_path):
    start, _ = running
    base, _ = start()
    config = tmp_path / "fetchit.json"
    data = json.loads(config.read_text())
    data["feeds"].insert(0, {"name": "Mag", "urls": ["https://flyer.bayam.tv/4sYB/dj8"], "keep_days": 0})
    config.write_text(json.dumps(data))
    status, answer = _call(base, "/api/links", {"feed": "Mag", "links": ["https://app.bayam.tv/mag/abc"]})
    assert status == 200, answer
    entry = json.loads(config.read_text())["feeds"][0]
    assert entry == {"name": "Mag", "urls_file": "lists/mag.txt", "keep_days": 0}
    assert (tmp_path / "lists" / "mag.txt").read_text() == (
        "https://flyer.bayam.tv/4sYB/dj8\nhttps://app.bayam.tv/mag/abc\n")


def test_the_last_link_of_a_list_stays(running, tmp_path):
    start, _ = running
    base, _ = start()
    one = "https://flyer.bayam.tv/4sYB/dj8"
    _call(base, "/api/links", {"feed": "Mag", "links": [one]})
    status, answer = _call(base, "/api/links/remove", {"feed": "Mag", "links": [one]})
    assert status == 422 and "at least one link" in answer["error"]
    assert (tmp_path / "lists" / "mag.txt").read_text() == one + "\n"
    status, answer = _call(base, "/api/links/remove", {"feed": "Blog", "links": [one]})
    assert status == 422 and "not a feed with a link list" in answer["error"]


@pytest.mark.parametrize("body, says", [
    ({"feed": "Mag", "links": ["flyer.bayam.tv/4sYB/dj8"]}, "not a link"),
    ({"feed": "Mag", "links": ["https://example.com/feed.xml"]}, "Not a link fetchit can read"),
    ({"feed": "Mag", "links": ["https://flyer.bayam.tv/4sYB/dj8", "https://app.bayam.tv/"]}, "https://app.bayam.tv/"),
    ({"feed": "Mag", "links": []}, "No links"),
    ({"feed": " ", "links": ["https://flyer.bayam.tv/4sYB/dj8"]}, "name"),
])
def test_links_that_cannot_be_added_change_nothing(running, tmp_path, body, says):
    start, _ = running
    base, _ = start()
    config = tmp_path / "fetchit.json"
    before = config.read_text()
    status, answer = _call(base, "/api/links", body)
    assert status == 422 and says in answer["error"]
    assert config.read_text() == before
    assert not (tmp_path / "lists").exists()


def _blog_post(library, title, item_id, day):
    item = FeedItem(title=title, description="", link=f"https://blog.example/{item_id}",
                    published=datetime(2026, 11, day, tzinfo=timezone.utc))
    write_item(str(library), FeedConfig(url="https://blog.example/feed", name="Blog"),
               ParsedFeed(items=[]), item, "text", item_id=item_id)


def test_the_panel_lists_a_feeds_episodes(running):
    start, library = running
    _blog_post(library, "Older", "1", 5)
    _blog_post(library, "Newer", "2", 6)
    base, _ = start()
    status, reply = _call(base, "/api/episodes?feed=Blog")
    assert status == 200
    assert [(e["title"], e["id"], e["picture"], e["in_library"]) for e in reply["episodes"]] == [
        ("Newer", "2", False, True), ("Older", "1", False, True)]
    status, reply = _call(base, "/api/episodes?feed=Nope")
    assert status == 404 and "No feed called" in reply["error"]


def test_fetch_again_forgets_then_runs_the_feed_as_one_job(running, monkeypatch):
    start, library = running
    _blog_post(library, "A post", "1", 6)
    asked = []

    def fake_run(config, *, dry_run=False, only=None):
        asked.append(only)
        return Summary(fetched=1)

    monkeypatch.setattr(server, "run", fake_run)
    base, _ = start()
    status, job = _call(base, "/api/forget", {"feed": "Blog", "ids": ["1"]})
    assert status == 202
    job = _wait(base, job)
    assert job["ok"], job
    assert job["kind"] == "fetch again (Blog)" and job["result"] == "1 forgotten, 1 fetched again."
    assert "Forgot A post" in job["output"]
    assert asked == [["Blog"]]
    _, reply = _call(base, "/api/episodes?feed=Blog")
    assert reply["episodes"] == []


def test_fetch_again_waits_for_no_one(running, monkeypatch):
    start, library = running
    _blog_post(library, "A post", "1", 6)
    release = threading.Event()

    def slow_run(config, *, dry_run=False, only=None):
        release.wait(5)
        return Summary()

    monkeypatch.setattr(server, "run", slow_run)
    base, _ = start()
    _, first = _call(base, "/api/run", {})
    status, reply = _call(base, "/api/forget", {"feed": "Blog", "ids": ["1"]})
    release.set()
    _wait(base, first)
    assert status == 409 and "is going" in reply["error"]
    _, reply = _call(base, "/api/episodes?feed=Blog")
    assert [e["title"] for e in reply["episodes"]] == ["A post"]


@pytest.mark.parametrize("body", [{}, {"feed": "Blog"}, {"feed": "Blog", "ids": []}, {"feed": "", "ids": ["1"]}])
def test_fetch_again_needs_a_feed_and_episodes(running, body):
    start, _ = running
    base, _ = start()
    status, reply = _call(base, "/api/forget", body)
    assert status == 422 and reply["error"]


# --- the e-Primo box ---------------------------------------------------------


def test_the_e_primo_box_is_only_for_a_config_with_a_blog(running, tmp_path, monkeypatch):
    from fetchit import eprimo

    monkeypatch.setenv(eprimo.SESSION_ENV, str(tmp_path / "eprimo-session.json"))
    start, _library = running
    base, _service = start()
    assert _call(base, "/api/eprimo") == (200, {"wanted": False, "signed_in": None, "accepted": False, "stored_login": False})

    config = json.loads((tmp_path / "fetchit.json").read_text())
    config["feeds"].append({"name": "School", "url": "https://ent.e-primo.fr/blog/id/86566c46-40f9-4aed-a7a4-af2c3bdd84ea"})
    (tmp_path / "fetchit.json").write_text(json.dumps(config))
    status, box = _call(base, "/api/eprimo")
    assert (status, box["wanted"], box["signed_in"]) == (200, True, None)


def test_signing_in_from_the_panel_keeps_no_password(running, tmp_path, monkeypatch):
    from fetchit import eprimo

    monkeypatch.setenv(eprimo.SESSION_ENV, str(tmp_path / "eprimo-session.json"))
    seen = []

    def fake_sign_in(username, password):
        if password != "s3cret":
            raise eprimo.EprimoError("ÉduConnect did not accept the identifier and password.")
        seen.append(username)
        (tmp_path / "eprimo-session.json").write_text(
            json.dumps({"cookies": {"oneSessionId": "abc"}, "signed_in": "2026-11-25T15:16:00+00:00"})
        )

    monkeypatch.setattr(eprimo, "sign_in", fake_sign_in)
    monkeypatch.setattr(eprimo, "check", lambda: True)
    start, _library = running
    base, service = start()

    status, refused = _call(base, "/api/eprimo/login", {"username": "p.nom12", "password": "wrong"})
    assert status == 422 and "did not accept" in refused["error"]
    assert _call(base, "/api/eprimo/login", {"username": "p.nom12"})[0] == 422

    status, box = _call(base, "/api/eprimo/login", {"username": "p.nom12", "password": "s3cret"})
    assert (status, box["signed_in"], seen) == (200, "2026-11-25T15:16:00+00:00", ["p.nom12"])
    assert "s3cret" not in json.dumps(list(service.activity))
