"""Build "Add to qBittorrent.shortcut": share or copy a magnet link, and qBittorrent downloads it.

    python shortcuts/build_qbittorrent.py    # writes and signs shortcuts/Add to qBittorrent.shortcut

Nothing to do with fetchit, but built from the same parts as ``build.py``. The
shortcut talks to qBittorrent's own Web API, on the Web UI's port:

1. Take the magnet links out of what was shared with it; shared nothing (run
   from the home screen), out of the clipboard.
2. No magnet link: say so, and stop.
3. Log in (``POST /api/v2/auth/login``). qBittorrent answers with a cookie,
   which Shortcuts keeps for the next request.
4. Add the links (``POST /api/v2/torrents/add``), and say what came of it.

There is no queue: away from home, or with qBittorrent stopped, the link is not
kept.

The address, the username and the password are asked for when the shortcut is
added, and are the three Text actions at its top afterwards.
"""

from __future__ import annotations

import plistlib
import subprocess
import sys
import tempfile
from pathlib import Path

from build import SHORTCUT_INPUT, Builder, _CONTAINS, _HAS_ANY_VALUE, attachment, fields, text

NAME = "Add to qBittorrent"
MAGNET = r"magnet:\?[^\s\"'<>]+"
# What a branch found, for the actions after it.
MAGNETS = {"Type": "Variable", "VariableName": "Magnets"}


def notify(s: Builder, body: dict) -> None:
    s.action("notification", WFNotificationActionTitle=NAME, WFNotificationActionBody=body,
             WFNotificationActionSound=False)


def magnets(s: Builder, source: dict, output: str) -> dict:
    return s.action("text.match", output, WFMatchTextPattern=MAGNET, text=text(source))


def post(s: Builder, output: str, url: dict, **form: dict) -> dict:
    return s.action("downloadurl", output, WFURL=url, WFHTTPMethod="POST", WFHTTPBodyType="Form",
                    WFFormValues=fields(**form), ShowHeaders=True)


def build() -> dict:
    s = Builder()
    s.action("comment", WFCommentActionText=(
        "Adds the shared magnet link to qBittorrent; run with nothing shared, the one on the clipboard.\n\n"
        "The three texts below: the address of qBittorrent's Web UI, its username and its password."))
    # An IP address, not homeassistant.local: see build.py.
    server = s.setting("Server", "The address of qBittorrent's Web UI: http://<Home Assistant's IP address>:<the "
                       "port set under the add-on's Network>, without a / at the end", "http://<IP address>:8080")
    user = s.setting("Username", "The username of qBittorrent's Web UI", "admin")
    password = s.setting("Password", "The password of qBittorrent's Web UI", "")

    shared = magnets(s, SHORTCUT_INPUT, "Shared magnets")
    was_shared = s.if_start(shared, _HAS_ANY_VALUE)
    s.action("setvariable", WFVariableName="Magnets", WFInput=attachment(shared))
    s.otherwise(was_shared)
    clipboard = s.action("getclipboard", "Clipboard")
    copied = magnets(s, clipboard, "Copied magnets")
    s.action("setvariable", WFVariableName="Magnets", WFInput=attachment(copied))
    s.end(was_shared)

    # Several links become one a line, which is how qBittorrent wants them.
    links = s.action("gettext", "Links", WFTextActionText=text(MAGNETS))

    has_links = s.if_start(links, _CONTAINS, string="magnet:")
    post(s, "Login", text(server, "/api/v2/auth/login"), username=text(user), password=text(password))
    response = post(s, "Response", text(server, "/api/v2/torrents/add"), urls=text(links))
    answer = s.action("gettext", "Answer", WFTextActionText=text(response))

    added = s.if_start(answer, _CONTAINS, string="Ok.")
    notify(s, text("Added."))
    s.otherwise(added)
    notify(s, text("Not added. qBittorrent answered: ", answer))
    s.end(added)

    s.otherwise(has_links)
    notify(s, text("No magnet link in what was shared, nor on the clipboard."))
    s.end(has_links)

    return {
        "WFWorkflowActions": s.actions,
        "WFWorkflowClientVersion": "4407",
        "WFWorkflowMinimumClientVersion": 900,
        "WFWorkflowMinimumClientVersionString": "900",
        "WFWorkflowIcon": {"WFWorkflowIconStartColor": 463140863, "WFWorkflowIconGlyphNumber": 59511},
        "WFWorkflowTypes": ["ActionExtension", "WFWorkflowTypeShowInSearch"],
        "WFWorkflowInputContentItemClasses": ["WFURLContentItem", "WFStringContentItem"],
        "WFWorkflowOutputContentItemClasses": [],
        "WFWorkflowHasOutputFallback": False,
        "WFWorkflowHasShortcutInputVariables": True,
        "WFWorkflowImportQuestions": s.questions,
        "WFQuickActionSurfaces": [],
    }


def write(path: Path) -> Path:
    """Write the shortcut to ``path``, signed so that any device imports it."""
    with tempfile.TemporaryDirectory() as tmp:
        unsigned = Path(tmp) / "unsigned.shortcut"
        unsigned.write_bytes(plistlib.dumps(build(), fmt=plistlib.FMT_BINARY))
        subprocess.run(["shortcuts", "sign", "--mode", "anyone", "--input", unsigned, "--output", path],
                       check=True, capture_output=True)
    return path


if __name__ == "__main__":
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).with_name(f"{NAME}.shortcut")
    print(write(out))
