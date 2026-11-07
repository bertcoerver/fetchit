"""Write tracebacks and run boundaries to a file, so an unattended run leaves
evidence.

A feed that fails prints one line on stderr — enough to see *that* something
went wrong, useless for working out *why*. When the run is a scheduled job,
stderr is usually thrown away, and the traceback with it: the feed quietly
stops arriving and there is nothing left to look at.

So every failure is also appended here, in full, with a timestamp. The file sits
next to ``.env`` — the working directory fetchit is already run from — and is
named ``fetchit.log``; ``FETCHIT_LOG`` moves it, and can be set in ``.env``
alongside everything else. Logging is best-effort in the strongest sense: a log
that can't be written must never be the reason a download doesn't happen, so
every failure in here is swallowed.
"""

from __future__ import annotations

import os
import sys
import traceback
from datetime import datetime

from .config import load_dotenv

DEFAULT_LOG_PATH = "fetchit.log"
LOG_ENV = "FETCHIT_LOG"

# Rotated rather than truncated, so the failure that made someone come looking
# is still there after the run that followed it. One old file is kept: this is a
# breadcrumb trail, not an archive.
MAX_LOG_BYTES = 1024 * 1024


def log_path() -> str:
    """Where tracebacks are written: ``FETCHIT_LOG``, or ``./fetchit.log``.

    ``.env`` is loaded first so the setting can live there with the rest of the
    configuration. That is a no-op when something has already loaded it, and
    never overrides a real environment variable.
    """
    try:
        load_dotenv()
    except OSError:
        pass
    return os.environ.get(LOG_ENV, "").strip() or DEFAULT_LOG_PATH


def log_exception(context: str, exc: BaseException) -> str | None:
    """Append ``exc``'s full traceback under a ``context`` heading.

    Returns the path written to, or ``None`` if it could not be written — the
    caller uses that to decide whether to point a human at the file, and has
    nothing to do about the failure either way.
    """
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
    body = "".join(traceback.format_exception(exc))
    return _append(f"\n{'=' * 72}\n{stamp}  {context}\n{'=' * 72}\n{body}")


def log_line(text: str) -> str | None:
    """Append one stamped line — for facts about a run, not failures of one.

    A traceback says why a feed went wrong; it says nothing about a run that
    never got to have one. When the process is killed partway — a Shortcut that
    hit its watchdog, a laptop that slept mid-upload — nothing is caught and
    nothing is written, and the log afterwards is indistinguishable from a run
    that had a quiet, successful evening.

    So a run says when it started and when it stopped. A "started" line with no
    "finished" under it is the trace a killed run leaves behind, and the only
    way to tell that case from the quiet one.
    """
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
    return _append(f"{stamp}  {text}\n")


def _append(text: str) -> str | None:
    path = log_path()
    try:
        _rotate(path)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(text)
    except OSError as exc:
        # Reported once, on stderr, and then let go: the run is more important
        # than the record of it.
        print(f"warning: could not write to {path}: {exc}", file=sys.stderr)
        return None
    return os.path.abspath(path)


def _rotate(path: str) -> None:
    """Move the log aside once it gets big, keeping one generation."""
    try:
        if os.path.getsize(path) < MAX_LOG_BYTES:
            return
    except OSError:
        return  # Doesn't exist yet, or can't be read; either way, nothing to move.
    os.replace(path, f"{path}.1")
