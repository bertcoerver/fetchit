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


def _call(base, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    request = Request(base + path, data=data, headers={"Content-Type": "application/json"})
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
