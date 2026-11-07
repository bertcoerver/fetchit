"""Command-line interface.

Subcommands:
    run             read every feed and download what is new into the library
    fields          list the fields a feed's items have, for writing filters
    prune           delete what has outlived its feed's keep_days
    doctor          check the tools, the config and the library
    serve           run on a schedule, with a web GUI
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys

from . import __version__, timing
from .config import ConfigError, config_path, load_config
from .errorlog import log_path
from .feeds import FeedConfig, FeedError, apply_filters, item_fields, parse_feed
from .library import FEED_MAX, prune, safe_name
from .media import TEXT, item_id, kind_of
from .run import AlreadyRunning, run


def _cmd_run(args: argparse.Namespace) -> int:
    config = load_config(args.config, library=args.library)
    if args.timings:
        timing.enable()
    try:
        summary = run(config, dry_run=args.dry_run, only=args.feed)
    except AlreadyRunning as exc:
        # The run that is going will get there; this one being redundant is not
        # a failure, and an unattended scheduler should not read it as one.
        print(f"Nothing to do: {exc}.", file=sys.stderr)
        return 0
    finally:
        if args.timings:
            timing.report()
    verb = "would be fetched" if args.dry_run else "fetched"
    print(f"{summary.fetched} item(s) {verb}.")
    return 0


def _cmd_fields(args: argparse.Namespace) -> int:
    if args.url:
        cfg = FeedConfig(url=args.url, name=args.feed or "feed")
    else:
        config = load_config(args.config, library=args.library)
        if not config.feeds:
            raise ConfigError("The config has no feeds.")
        cfg = config.feeds[0]
        if args.feed:
            wanted = args.feed.casefold()
            matches = [c for c in config.feeds if wanted in (c.name.casefold(), c.url.casefold())]
            if not matches:
                raise ConfigError(f"No feed called {args.feed!r} in the config.")
            cfg = matches[0]

    parsed = parse_feed(cfg)
    items = parsed.items
    if not items:
        print(f"{cfg.name}: the feed has no items (or nothing changed since the last run).")
        return 0
    index = min(max(args.item, 1), len(items)) - 1
    item = items[index]
    kept = "kept by this feed's filters" if apply_filters([item], cfg.filters) else "dropped by this feed's filters"

    print(f"Fields for {cfg.name} (item {index + 1} of {len(items)}, {kept}):\n")
    fields = item_fields(item)
    width = max(len(key) for key in fields)
    core = set(list(fields)[:12])
    for key, value in fields.items():
        shown = value if len(value) <= 70 else value[:67] + "..."
        extra = "" if key in core else "   (extra)"
        print(f"  {key:<{width}}  {shown}{extra}")
    kind = kind_of(item)
    print(f"\n  kind: {kind}{'' if kind != TEXT else ' (nothing to download)'}")
    print(f"  id:   {item_id(item)}")
    return 0


def _cmd_prune(args: argparse.Namespace) -> int:
    config = load_config(args.config, library=args.library)
    removed = prune(
        config.library,
        {safe_name(c.name, limit=FEED_MAX): config.keep_days_for(c) for c in config.feeds},
        default=config.keep_days,
    )
    print(f"{removed} item(s) expired.")
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    ok = True
    print(f"fetchit {__version__}")
    for tool, why in (
        ("ffmpeg", "ARTE videos, and joining YouTube's video and audio"),
        ("ffprobe", "reading a downloaded file's length"),
        ("yt-dlp", "YouTube feeds (`pip install fetchit[video]`)"),
    ):
        found = shutil.which(tool)
        print(f"  {tool:<8} {found or 'not found'}  — needed for {why}")
    path = config_path(args.config)
    try:
        config = load_config(args.config, library=args.library)
    except FeedError as exc:
        print(f"  config   {path}: {exc}")
        return 1
    print(f"  config   {path}: {len(config.feeds)} feed(s)")
    print(f"  library  {config.library}")
    try:
        os.makedirs(config.library, exist_ok=True)
        probe = os.path.join(config.library, ".fetchit-write-test")
        with open(probe, "w") as fh:
            fh.write("ok")
        os.unlink(probe)
    except OSError as exc:
        print(f"           not writable: {exc}")
        ok = False
    keep = config.keep_days
    print(f"  keep     {'everything' if keep == 0 else f'{keep} day(s)'} unless a feed says otherwise")
    print(f"  log      {log_path()}")
    return 0 if ok else 1


def _cmd_serve(args: argparse.Namespace) -> int:
    from .server import serve

    serve(args.config, library=args.library, host=args.host, port=args.port, on_start=args.on_start)
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fetchit", description="Download feeds into a media library."
    )
    parser.add_argument("--version", action="version", version=f"fetchit {__version__}")
    parser.add_argument("--config", help="config file (default: $FETCHIT_CONFIG or fetchit.json)")
    parser.add_argument("--library", help="library folder (default: $FETCHIT_LIBRARY, the config's, or ~/fetchit)")
    commands = parser.add_subparsers(dest="command", required=True)

    run_ = commands.add_parser("run", help="download what is new")
    run_.add_argument("--dry-run", action="store_true", help="say what would be downloaded, and download nothing")
    run_.add_argument("--feed", action="append", metavar="NAME", help="only this feed (repeatable)")
    run_.add_argument("--timings", action="store_true", help="report where the run's time went")
    run_.set_defaults(handler=_cmd_run)

    fields = commands.add_parser("fields", help="list the fields of a feed's items")
    fields.add_argument("--feed", help="the feed, by name or url (default: the first)")
    fields.add_argument("--url", help="a feed that is not in the config yet")
    fields.add_argument("--item", type=int, default=1, help="which item to sample (default: 1)")
    fields.set_defaults(handler=_cmd_fields)

    prune_ = commands.add_parser("prune", help="delete what has outlived keep_days")
    prune_.set_defaults(handler=_cmd_prune)

    doctor = commands.add_parser("doctor", help="check the tools, the config and the library")
    doctor.set_defaults(handler=_cmd_doctor)

    serve = commands.add_parser(
        "serve",
        help="run on a schedule, with a web GUI",
        description="Run at the times in FETCHIT_SCHEDULE (e.g. 01:00,06:00,14:30) and serve a GUI.",
    )
    serve.add_argument("--host", default="127.0.0.1", help="address to listen on (default: 127.0.0.1)")
    serve.add_argument("--port", type=int, default=8099, help="port to listen on (default: 8099)")
    serve.add_argument("--on-start", choices=("wait", "dry-run", "run"), default="wait",
                       help="what to do at once, before the schedule takes over (default: wait)")
    serve.set_defaults(handler=_cmd_serve)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return args.handler(args)
    except FeedError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
