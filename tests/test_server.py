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


def test_the_panel_adds_bayam_links_to_a_feed(running, tmp_path):
    start, _ = running
    base, _ = start()
    config = tmp_path / "fetchit.json"
    before = config.read_text()
    one, two, three = (f"https://flyer.bayam.tv/4sYB/{code}" for code in ("dj8", "pmo", "3v2"))

    # A feed that isn't there yet is made, and kept for good.
    status, answer = _call(base, "/api/bayam", {"feed": "Histoires", "links": [one]})
    assert status == 200, answer
    assert answer == {"feed": "Histoires", "added": [one], "already": 0, "created": True}
    assert json.loads(config.read_text())["feeds"][-1] == {"name": "Histoires", "urls": [one], "keep_days": 0}
    assert (tmp_path / "fetchit.json.bak").read_text() == before

    # A new issue is one more link; one it has is not added twice.
    status, answer = _call(base, "/api/bayam", {"feed": "Histoires", "links": [two, one, f" {three} "]})
    assert status == 200, answer
    assert answer == {"feed": "Histoires", "added": [two, three], "already": 1, "created": False}
    data = json.loads(config.read_text())
    assert data["feeds"][-1] == {"name": "Histoires", "urls": [one, two, three], "keep_days": 0}
    assert len(data["feeds"]) == 3 and data["keep_days"] == 30

    _, feeds = _call(base, "/api/feeds")
    assert [(f["name"], f["bayam"], f["urls"]) for f in feeds["feeds"]] == [
        ("Blog", False, 1), ("Channel", False, 1), ("Histoires", True, 3)]


def test_a_feed_that_said_url_says_urls_in_the_same_place(running, tmp_path):
    start, _ = running
    base, _ = start()
    config = tmp_path / "fetchit.json"
    data = json.loads(config.read_text())
    data["feeds"].insert(0, {"name": "Mag", "url": "https://flyer.bayam.tv/4sYB/dj8", "keep_days": 0})
    config.write_text(json.dumps(data))
    status, answer = _call(base, "/api/bayam", {"feed": "Mag", "links": ["https://app.bayam.tv/mag/abc"]})
    assert status == 200, answer
    entry = json.loads(config.read_text())["feeds"][0]
    assert list(entry) == ["name", "urls", "keep_days"]
    assert entry["urls"] == ["https://flyer.bayam.tv/4sYB/dj8", "https://app.bayam.tv/mag/abc"]


@pytest.mark.parametrize("body, says", [
    ({"feed": "Blog", "links": ["https://flyer.bayam.tv/4sYB/dj8"]}, "not a Bayam feed"),
    ({"feed": "Mag", "links": ["https://example.com/4sYB/dj8"]}, "not a Bayam magazine link"),
    ({"feed": "Mag", "links": []}, "No links"),
    ({"feed": " ", "links": ["https://flyer.bayam.tv/4sYB/dj8"]}, "name"),
])
def test_bayam_links_that_cannot_be_added_change_nothing(running, tmp_path, body, says):
    start, _ = running
    base, _ = start()
    config = tmp_path / "fetchit.json"
    before = config.read_text()
    status, answer = _call(base, "/api/bayam", body)
    assert status == 422 and says in answer["error"]
    assert config.read_text() == before
