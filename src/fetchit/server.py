"""``fetchit serve``: run on a schedule, and show what is going on in a web GUI.

One long-lived process instead of a scheduler loop in a shell script: it runs
fetchit at the times in :data:`SCHEDULE_ENV`, and serves a page — the Home
Assistant add-on's sidebar panel, through ingress — that shows every feed and
what the library holds of it, and has buttons to run, dry-run or prune now.

Only one job runs at a time; asking for a second while one is going shows the
one that is going. The GUI answers only the addresses in :data:`GUI_FROM_ENV`:
this machine by default, Home Assistant's ingress proxy in the add-on, which
has checked who you are before a request gets here.
"""

from __future__ import annotations

import io
import hashlib
import ipaddress
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from urllib.parse import urlsplit

from . import __version__
from .bayam import is_bayam_link
from .config import Config, ConfigError, config_path, load_config
from .errorlog import log_exception, log_line, log_path
from .feeds import FeedError
from .library import KIND_DIRS, feed_folders, prune, records
from .run import AlreadyRunning, run
from .state import State

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8099

SCHEDULE_ENV = "FETCHIT_SCHEDULE"
GUI_FROM_ENV = "FETCHIT_GUI_FROM"
UPDATE_YT_DLP_ENV = "FETCHIT_UPDATE_YT_DLP"
DEFAULT_FROM = "127.0.0.1,::1"

# yt-dlp breaks whenever YouTube changes, so a pinned one rots within weeks.
# With UPDATE_YT_DLP_ENV set, a scheduled run upgrades it first, once a day.
YT_DLP_EVERY = 24 * 60 * 60


# --- small things -----------------------------------------------------------


def parse_schedule(value: str | None) -> list[tuple[int, int]]:
    """``"01:00, 06:00"`` as ``[(1, 0), (6, 0)]``; nonsense is refused."""
    slots = []
    for part in (value or "").split(","):
        part = part.strip()
        if not part:
            continue
        hour, sep, minute = part.partition(":")
        try:
            h, m = int(hour), int(minute)
        except ValueError:
            h = m = -1
        if not sep or not (0 <= h < 24 and 0 <= m < 60):
            raise ValueError(f"{part!r} is not a time like 06:30.")
        slots.append((h, m))
    return sorted(set(slots))


def next_slot(slots: list[tuple[int, int]], now: datetime) -> datetime | None:
    """The first slot after ``now``, in ``now``'s time zone."""
    for days in (0, 1):
        day = now.date() + timedelta(days=days)
        for h, m in slots:
            when = datetime(day.year, day.month, day.day, h, m, tzinfo=now.tzinfo)
            if when > now:
                return when
    return None


def parse_networks(value: str) -> list:
    nets = []
    for part in (value or "").split(","):
        if part.strip():
            try:
                nets.append(ipaddress.ip_network(part.strip(), strict=False))
            except ValueError:
                print(f"warning: {part.strip()!r} is not an address; ignored.", file=sys.stderr)
    return nets


def allowed(address: str, networks: list) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return any(ip in net for net in networks if net.version == ip.version)


def _iso(epoch: float | None) -> str | None:
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch).astimezone().isoformat(timespec="seconds")


class _Tee(io.TextIOBase):
    """``sys.stdout``/``sys.stderr`` that also hand everything to the running job.

    Not per thread: a run reads its feeds on worker threads, and what they say
    belongs to the run too. Jobs run one at a time, so there is one place for
    it to go.
    """

    def __init__(self, target) -> None:
        self._target = target
        self.sink: list[str] | None = None

    def write(self, text: str) -> int:
        sink = self.sink
        if sink is not None:
            sink.append(text)
        self._target.write(text)
        return len(text)

    def flush(self) -> None:
        self._target.flush()

    def __getattr__(self, name):
        return getattr(self._target, name)


@dataclass
class Job:
    id: str
    kind: str
    started: float = field(default_factory=time.time)
    finished: float | None = None
    ok: bool | None = None
    result: str = ""
    output: list[str] = field(default_factory=list)

    def view(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "started": _iso(self.started),
            "finished": _iso(self.finished),
            "running": self.finished is None,
            "ok": self.ok,
            "result": self.result,
            "output": "".join(self.output)[-200_000:],
        }


# --- the service ------------------------------------------------------------


class Service:
    """Everything ``fetchit serve`` does, apart from speaking HTTP."""

    def __init__(
        self,
        config_file: str | None = None,
        *,
        library: str | None = None,
        schedule: list[tuple[int, int]] | None = None,
        gui_from: str = DEFAULT_FROM,
        update_yt_dlp: bool = False,
    ) -> None:
        self.config_file = config_path(config_file)
        self.library = library
        self.schedule = schedule or []
        self.gui_from = parse_networks(gui_from)
        self.update_yt_dlp = update_yt_dlp
        self.activity: deque[dict] = deque(maxlen=100)
        self.jobs: dict[str, Job] = {}
        self._order: deque[str] = deque()
        self._current: Job | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._tees: tuple[_Tee, _Tee] | None = None
        self._yt_dlp_updated = 0.0
        self._config_lock = threading.Lock()

    def config(self) -> Config:
        return load_config(self.config_file, library=self.library)

    # life cycle

    def start(self) -> None:
        out, err = _Tee(sys.stdout), _Tee(sys.stderr)
        self._tees = (out, err)
        sys.stdout, sys.stderr = out, err
        if self.schedule:
            threading.Thread(target=self._schedule_loop, daemon=True, name="fetchit-schedule").start()

    def stop(self) -> None:
        self._stop.set()
        if self._tees is not None:
            sys.stdout, sys.stderr = self._tees[0]._target, self._tees[1]._target
            self._tees = None

    # jobs

    def start_job(self, kind: str, work) -> Job:
        """Run ``work()`` on a thread, unless a job is going — then that job."""
        with self._lock:
            if self._current is not None and self._current.finished is None:
                return self._current
            job = Job(id=uuid.uuid4().hex[:12], kind=kind)
            self._current = job
            self.jobs[job.id] = job
            self._order.append(job.id)
            while len(self._order) > 20:
                self.jobs.pop(self._order.popleft(), None)

        def body() -> None:
            if self._tees:
                for tee in self._tees:
                    tee.sink = job.output
            try:
                job.result = str(work())
                job.ok = True
            except Exception as exc:  # reported in the GUI and the log
                job.ok = False
                job.result = f"{type(exc).__name__}: {exc}"
                job.output.append("\n" + traceback.format_exc())
                log_exception(f"serve: {kind}", exc)
            finally:
                if self._tees:
                    for tee in self._tees:
                        tee.sink = None
                job.finished = time.time()
                self.activity.appendleft(
                    {"when": _iso(job.finished), "kind": kind, "text": job.result, "ok": bool(job.ok)}
                )

        threading.Thread(target=body, daemon=True, name=f"fetchit-{kind}").start()
        return job

    def current(self) -> Job | None:
        job = self._current
        return job if job is not None and job.finished is None else None

    def run(self, *, dry_run: bool = False, only: list[str] | None = None, scheduled: bool = False) -> Job:
        def work() -> str:
            if scheduled:
                self._maybe_update_yt_dlp()
            try:
                summary = run(self.config(), dry_run=dry_run, only=only or None)
            except AlreadyRunning as exc:
                return f"Nothing to do: {exc}."
            verb = "would be fetched" if dry_run else "fetched"
            parts = [f"{summary.fetched} item(s) {verb}"]
            for count, what in (
                (summary.skipped, "skipped for good"),
                (summary.failed, "feed(s) failed"),
                (summary.blocked, "feed(s) blocked"),
                (summary.pruned, "expired"),
            ):
                if count:
                    parts.append(f"{count} {what}")
            return ", ".join(parts) + "."

        kind = "dry-run" if dry_run else "run"
        if only:
            kind += f" ({', '.join(only)})"
        return self.start_job(kind, work)

    def prune(self) -> Job:
        def work() -> str:
            config = self.config()
            from .library import FEED_MAX, safe_name

            removed = prune(
                config.library,
                {safe_name(c.name, limit=FEED_MAX): config.keep_days_for(c) for c in config.feeds},
                default=config.keep_days,
            )
            return f"{removed} item(s) expired."

        return self.start_job("prune", work)

    def _maybe_update_yt_dlp(self) -> None:
        if not self.update_yt_dlp or time.time() - self._yt_dlp_updated < YT_DLP_EVERY:
            return
        print("Updating yt-dlp...")
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--quiet", "--no-cache-dir", "--upgrade", "yt-dlp"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            self._yt_dlp_updated = time.time()
        else:
            # The installed one worked yesterday; not a reason to skip the run.
            print(f"yt-dlp update failed; using the installed one. {result.stderr.strip()[-300:]}")

    def _schedule_loop(self) -> None:
        log_line(f"serve: scheduled at {', '.join(f'{h:02d}:{m:02d}' for h, m in self.schedule)}")
        while not self._stop.is_set():
            when = next_slot(self.schedule, datetime.now().astimezone())
            if when is None:
                return
            while not self._stop.is_set():
                left = (when - datetime.now().astimezone()).total_seconds()
                if left <= 0:
                    break
                self._stop.wait(min(left, 60))
            if self._stop.is_set():
                return
            job = self.run(scheduled=True)
            while job.finished is None and not self._stop.is_set():
                self._stop.wait(5)
            self._stop.wait(1)

    def next_run(self) -> str | None:
        when = next_slot(self.schedule, datetime.now().astimezone()) if self.schedule else None
        return when.isoformat(timespec="minutes") if when else None

    # what the GUI shows

    def status(self) -> dict:
        info: dict = {
            "version": __version__,
            "config": os.path.abspath(self.config_file),
            "schedule": [f"{h:02d}:{m:02d}" for h, m in self.schedule],
            "next_run": self.next_run(),
            "running": self.current().view() if self.current() else None,
            "log": os.path.abspath(log_path()),
            "tools": {name: bool(shutil.which(name)) for name in ("ffmpeg", "yt-dlp")},
            "problem": None,
        }
        try:
            config = self.config()
        except FeedError as exc:
            info["problem"] = str(exc)
            return info
        info["library"] = config.library
        info["feeds"] = len(config.feeds)
        info["keep_days"] = config.keep_days
        try:
            usage = shutil.disk_usage(config.library if os.path.isdir(config.library) else os.path.dirname(config.library) or "/")
            info["disk"] = {"free": usage.free, "total": usage.total}
        except OSError:
            info["disk"] = None
        return info

    def feeds(self) -> list[dict]:
        config = self.config()
        state = State.load(config.library)
        out = []
        for cfg in config.feeds:
            found = records(config.library, cfg.name)
            size = 0
            for record in found:
                media = record.get("media")
                if media:
                    try:
                        size += os.path.getsize(os.path.join(os.path.dirname(record["_path"]), media))
                    except OSError:
                        pass
            newest = max(found, key=lambda r: str(r.get("published") or ""), default=None)
            q = cfg.quality
            quality = (f"≤{q.max_height}p" if q.max_height else "best") + (
                f", {q.part_bytes // (1024 * 1024)} MB parts" if q.part_bytes else ""
            )
            after = state.after(cfg.name)
            out.append({
                "name": cfg.name,
                "url": cfg.url,
                "bayam": all(is_bayam_link(url) for url in cfg.sources),
                "urls": len(cfg.sources),
                "kinds": [KIND_DIRS[kind] for kind, _ in feed_folders(config.library, cfg.name)],
                "items": len(found),
                "bytes": size,
                "newest": newest.get("title") if newest else None,
                "newest_date": str(newest.get("published") or "")[:10] if newest else None,
                "fetched": max((str(r.get("fetched") or "") for r in found), default=None) or None,
                "since": cfg.since.date().isoformat() if cfg.since else None,
                "after": after.isoformat(timespec="minutes") if after else None,
                "keep_days": config.keep_days_for(cfg),
                "quality": quality,
                "filters": len(cfg.filters),
            })
        return out

    def recent(self, limit: int = 30) -> list[dict]:
        config = self.config()
        found = []
        for cfg in config.feeds:
            for record in records(config.library, cfg.name):
                found.append({
                    "feed": cfg.name,
                    "title": record.get("title"),
                    "kind": record.get("kind"),
                    "published": record.get("published"),
                    "fetched": record.get("fetched"),
                    "path": os.path.relpath(record["_path"], config.library)[: -len(".json")],
                })
        found.sort(key=lambda r: str(r.get("fetched") or ""), reverse=True)
        return found[:limit]

    def log_tail(self, lines: int = 200) -> str:
        try:
            with open(log_path(), encoding="utf-8", errors="replace") as fh:
                return "".join(deque(fh, maxlen=lines))
        except OSError:
            return ""

    def config_text(self) -> str:
        try:
            with open(self.config_file, encoding="utf-8") as fh:
                return fh.read()
        except OSError as exc:
            return f"(cannot read {self.config_file}: {exc})"

    def config_view(self) -> dict:
        """The config file as the panel's editor shows it.

        ``version`` is a hash of the file as read; :meth:`save_config` refuses
        a save made against an older one, so an edit made meanwhile in the File
        Editor or over Samba is not silently overwritten. ``""`` means there is
        no file yet.
        """
        try:
            with open(self.config_file, "rb") as fh:
                raw = fh.read()
        except FileNotFoundError:
            return {"path": self.config_file, "text": "", "version": "", "exists": False}
        except OSError as exc:
            return {"path": self.config_file, "text": f"(cannot read {self.config_file}: {exc})",
                    "version": None, "exists": True}
        return {"path": self.config_file, "text": raw.decode("utf-8", "replace"),
                "version": _version(raw), "exists": True}

    def save_config(self, text: str, version: str | None) -> dict:
        """Replace the config file with ``text``, if it is a config a run accepts.

        The text is checked by the same :func:`load_config` a run uses, from a
        temporary file beside the real one, which then takes the real one's
        place in a single rename: a run starting meanwhile reads either the old
        file or the new one, never half of one. The old file is kept as
        ``<name>.bak``. Raises :class:`ConfigError` naming what is wrong, and
        :class:`ConfigConflict` if the file changed since ``version`` was read.
        """
        if not isinstance(text, str) or not text.strip():
            raise ConfigError("The configuration is empty.")
        if not text.endswith("\n"):
            text += "\n"
        folder = os.path.dirname(os.path.abspath(self.config_file))
        with self._config_lock:
            current = self.config_view()
            if version is None or current["version"] != version:
                raise ConfigConflict(
                    "The file changed since this page loaded it (in the File Editor, over "
                    "Samba, or in another tab). Reload it, and make your change again."
                )
            fd, tmp = tempfile.mkstemp(prefix=".fetchit-", suffix=".json", dir=folder)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(text)
                config = load_config(tmp, library=self.library)
                if current["exists"]:
                    shutil.copy2(self.config_file, self.config_file + ".bak")
                    shutil.copymode(self.config_file, tmp)
                os.replace(tmp, self.config_file)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        self.activity.appendleft({
            "when": _iso(time.time()),
            "kind": "config", "text": f"saved, {len(config.feeds)} feed(s).", "ok": True,
        })
        log_line(f"config saved from the panel: {len(config.feeds)} feed(s)")
        return {**self.config_view(), "feeds": len(config.feeds)}

    def add_bayam_links(self, name: str, links: list[str]) -> dict:
        """Add magazine links to the Bayam feed called ``name``, in the config file.

        What typing them into the editor's ``urls`` list does, without the
        typing: a new issue of a magazine is one more link on a feed that is
        already there. A feed that is not there yet is made, kept for good
        (``keep_days`` 0) — an issue is read once, and a story pruned after a
        month does not come back. Links the feed already has are left alone.

        Saved through :meth:`save_config`, so the result is checked and backed
        up like any other edit; the file is rewritten as indented JSON, which is
        what the editor's Format button makes of it too.
        """
        name = name.strip() if isinstance(name, str) else ""
        if not name:
            raise ConfigError("Which feed? Give it a name.")
        wanted: list[str] = []
        for link in links if isinstance(links, list) else []:
            link = link.strip() if isinstance(link, str) else ""
            if not link:
                continue
            if not is_bayam_link(link):
                raise ConfigError(
                    f"{link!r} is not a Bayam magazine link (https://flyer.bayam.tv/... "
                    "or https://app.bayam.tv/...)."
                )
            if link not in wanted:
                wanted.append(link)
        if not wanted:
            raise ConfigError("No links to add.")

        view = self.config_view()
        if view["version"] is None:
            raise ConfigError(view["text"])
        try:
            data = json.loads(view["text"]) if view["exists"] else {"feeds": []}
        except ValueError:
            raise ConfigError("The configuration file is not valid JSON; fix it in the editor first.")
        feeds = data.get("feeds") if isinstance(data, dict) else None
        if not isinstance(feeds, list):
            raise ConfigError('The configuration file has no "feeds" list; fix it in the editor first.')

        at = next((i for i, f in enumerate(feeds) if isinstance(f, dict) and f.get("name") == name), None)
        if at is None:
            have: list[str] = []
            entry: dict = {"name": name, "keep_days": 0}
        else:
            entry = feeds[at]
            more = entry.get("urls")
            have = [u for u in [entry.get("url"), *(more if isinstance(more, list) else [])]
                    if isinstance(u, str) and u.strip()]
            if not all(is_bayam_link(u) for u in have):
                raise ConfigError(f"{name!r} is not a Bayam feed; its links are not Bayam's.")
        added = [link for link in wanted if link not in have]
        result = {"feed": name, "added": added, "already": len(wanted) - len(added), "created": at is None}
        if not added:
            return result

        # Where the feed said "url" or "urls" it now says "urls", in the same place.
        merged: dict = {}
        for key, value in entry.items():
            if key in ("url", "urls"):
                merged.setdefault("urls", have + added)
            else:
                merged[key] = value
        if "urls" not in merged:
            merged = {"name": merged.pop("name"), "urls": added, **merged}
        if at is None:
            feeds.append(merged)
        else:
            feeds[at] = merged
        self.save_config(json.dumps(data, indent=2, ensure_ascii=False), view["version"])
        return result


class ConfigConflict(Exception):
    """The config file changed since the editor read it."""


def _version(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()[:16]


def _untemp(message: str) -> str:
    import re

    return re.sub(r"'[^']*\.fetchit-[^']*\.json'", "the configuration", message)


# --- HTTP -------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    service: Service
    server_version = f"fetchit/{__version__}"

    def log_message(self, format, *args):  # noqa: A002 - the stdlib's name
        pass

    def _json(self, status: int, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _allowed(self) -> bool:
        if allowed(self.client_address[0], self.service.gui_from):
            return True
        self._json(HTTPStatus.FORBIDDEN, {"error": "The GUI is not open to this address."})
        return False

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            data = json.loads(self.rfile.read(min(length, 1_000_000)))
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def do_GET(self) -> None:  # noqa: N802 - the stdlib's name
        if not self._allowed():
            return
        path = urlsplit(self.path).path.rstrip("/") or "/"
        svc = self.service
        try:
            if path in ("/", "/index.html"):
                page = resources.files("fetchit").joinpath("ui/index.html").read_bytes()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(page)))
                self.end_headers()
                self.wfile.write(page)
            elif path == "/api/status":
                self._json(HTTPStatus.OK, svc.status())
            elif path == "/api/feeds":
                self._json(HTTPStatus.OK, {"feeds": svc.feeds()})
            elif path == "/api/recent":
                self._json(HTTPStatus.OK, {"recent": svc.recent()})
            elif path == "/api/activity":
                self._json(HTTPStatus.OK, {"activity": list(svc.activity)})
            elif path == "/api/log":
                self._json(HTTPStatus.OK, {"log": svc.log_tail()})
            elif path == "/api/config":
                self._json(HTTPStatus.OK, svc.config_view())
            elif path.startswith("/api/jobs/"):
                job = svc.jobs.get(path.rsplit("/", 1)[-1])
                if job is None:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "No such job."})
                else:
                    self._json(HTTPStatus.OK, job.view())
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": "Not found."})
        except FeedError as exc:
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

    def do_POST(self) -> None:  # noqa: N802 - the stdlib's name
        if not self._allowed():
            return
        path = urlsplit(self.path).path.rstrip("/")
        body = self._body()
        svc = self.service
        if path == "/api/run":
            only = body.get("feeds")
            only = [str(f) for f in only] if isinstance(only, list) else None
            self._json(HTTPStatus.ACCEPTED, svc.run(dry_run=bool(body.get("dry_run")), only=only).view())
        elif path == "/api/prune":
            self._json(HTTPStatus.ACCEPTED, svc.prune().view())
        elif path == "/api/bayam":
            try:
                self._json(HTTPStatus.OK, svc.add_bayam_links(body.get("feed"), body.get("links")))
            except ConfigConflict as exc:
                self._json(HTTPStatus.CONFLICT, {"error": str(exc)})
            except (FeedError, ValueError, TypeError) as exc:
                self._json(HTTPStatus.UNPROCESSABLE_ENTITY, {"error": _untemp(str(exc))})
            except OSError as exc:
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"Could not save: {exc}"})
        else:
            self._json(HTTPStatus.NOT_FOUND, {"error": "Not found."})

    def do_PUT(self) -> None:  # noqa: N802 - the stdlib's name
        if not self._allowed():
            return
        path = urlsplit(self.path).path.rstrip("/")
        if path != "/api/config":
            self._json(HTTPStatus.NOT_FOUND, {"error": "Not found."})
            return
        body = self._body()
        try:
            self._json(HTTPStatus.OK, self.service.save_config(body.get("text"), body.get("version")))
        except ConfigConflict as exc:
            self._json(HTTPStatus.CONFLICT, {"error": str(exc)})
        except (FeedError, ValueError, TypeError) as exc:
            # A config a run would refuse. Name the problem, not the temporary
            # file it was found in.
            self._json(HTTPStatus.UNPROCESSABLE_ENTITY, {"error": _untemp(str(exc))})
        except OSError as exc:
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"Could not save: {exc}"})


def make_server(service: Service, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"service": service})
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    server_class = type("Server", (ThreadingHTTPServer,), {"address_family": family, "daemon_threads": True})
    return server_class((host, port), handler)


def serve(
    config_file: str | None = None,
    *,
    library: str | None = None,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    on_start: str = "wait",
) -> None:
    """Run the service until interrupted. See the module docstring."""
    service = Service(
        config_file,
        library=library,
        schedule=parse_schedule(os.environ.get(SCHEDULE_ENV, "")),
        gui_from=os.environ.get(GUI_FROM_ENV, "").strip() or DEFAULT_FROM,
        update_yt_dlp=os.environ.get(UPDATE_YT_DLP_ENV, "").strip().lower() in {"1", "true", "yes"},
    )
    try:
        server = make_server(service, host, port)
    except OSError as exc:
        raise SystemExit(f"error: cannot listen on {host}:{port}: {exc.strerror or exc}.")
    service.start()
    when = service.next_run()
    print(
        f"fetchit {__version__} serving on http://{host}:{server.server_address[1]}/ — "
        + (f"next run {when}." if when else "no schedule; runs only when asked.")
    )
    log_line(f"serve: started on {host}:{server.server_address[1]}")
    if on_start in ("run", "dry-run"):
        service.run(dry_run=on_start == "dry-run", scheduled=on_start == "run")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        service.stop()
        log_line("serve: stopped")
