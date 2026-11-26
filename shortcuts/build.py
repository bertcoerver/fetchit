"""Build "Save for later.shortcut": share a video's page to it, and fetchit fetches it.

    python shortcuts/build.py            # writes and signs shortcuts/Save for later.shortcut

The shortcut sends links to ``POST /api/save`` of ``fetchit serve`` (see
``fetchit.server.Service.save``), which only answers on the home network. So it
keeps a queue, in Shortcuts' global stored content:

1. Add the links shared with it, if any, to the queue. Run with nothing, as an
   automation does, it only sends what is waiting.
2. Nothing waiting: stop, without a word.
3. Not on the home Wi-Fi: say the link is kept, and stop.
4. Send the whole queue. fetchit answers with a ``message`` for everything it
   took in — saved, already saved, or not a video it reads — and only then is
   the queue emptied. Anything else (no answer, a wrong token) leaves it as it
   is for the next try.

To have the queue sent on coming home, make a personal automation in the
Shortcuts app — When: Wi-Fi, your network, Run Immediately; Do: Run Shortcut
"Save for later". An automation can't be put in a file.

The address, the token and the Wi-Fi name are asked for when the shortcut is
added, and are the three Text actions at its top afterwards.

A .shortcut file is a property list of actions, signed with macOS'
``shortcuts sign``. Apple doesn't document the format; the identifiers and keys
here are the ones the Shortcuts app writes.
"""

from __future__ import annotations

import plistlib
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

NAME = "Save for later"
# The key of the queue in global stored content: links, one a line.
QUEUE_KEY = "fetchit saved for later"
# What an emptied queue holds. Storing an empty text stores nothing.
EMPTY = "-"

_OBJECT = "￼"  # marks where a variable sits in a text
_CONTAINS = 99
_HAS_ANY_VALUE = 100

SHORTCUT_INPUT = {"Type": "ExtensionInput"}


def _uid() -> str:
    return str(uuid.uuid4()).upper()


def attachment(ref: dict) -> dict:
    return {"Value": ref, "WFSerializationType": "WFTextTokenAttachment"}


def text(*parts: str | dict) -> dict:
    """A text field mixing literal strings and variables / action outputs."""
    string, attachments = "", {}
    for part in parts:
        if isinstance(part, str):
            string += part
        else:
            # Ranges count UTF-16 code units, as NSString does.
            attachments[f"{{{len(string.encode('utf-16-le')) // 2}, 1}}"] = part
            string += _OBJECT
    return {"Value": {"string": string, "attachmentsByRange": attachments}, "WFSerializationType": "WFTextTokenString"}


def fields(**values: dict) -> dict:
    """The key/value table of a request's headers or JSON body."""
    items = [{"WFItemType": 0, "WFKey": text(key), "WFValue": value} for key, value in values.items()]
    return {"Value": {"WFDictionaryFieldValueItems": items}, "WFSerializationType": "WFDictionaryFieldValue"}


class Builder:
    def __init__(self) -> None:
        self.actions: list[dict] = []
        self.questions: list[dict] = []

    def action(self, name: str, output: str | None = None, **params) -> dict:
        """Append an action; with ``output``, return a reference to what it gives."""
        ref = {}
        if output:
            params["UUID"] = _uid()
            params["CustomOutputName"] = output
            ref = {"Type": "ActionOutput", "OutputUUID": params["UUID"], "OutputName": output}
        self.actions.append({"WFWorkflowActionIdentifier": f"is.workflow.actions.{name}",
                             "WFWorkflowActionParameters": params})
        return ref

    def setting(self, output: str, question: str, default: str) -> dict:
        """A Text action whose text is asked for when the shortcut is added."""
        self.questions.append({"ActionIndex": len(self.actions), "Category": "Parameter",
                               "ParameterKey": "WFTextActionText", "DefaultValue": default, "Text": question})
        return self.action("gettext", output, WFTextActionText=default)

    def if_start(self, ref: dict, condition: int, string=None) -> str:
        group = _uid()
        params = {"GroupingIdentifier": group, "WFControlFlowMode": 0, "WFCondition": condition,
                  "WFInput": {"Type": "Variable", "Variable": attachment(ref)}}
        if string is not None:
            params["WFConditionalActionString"] = string
        self.action("conditional", **params)
        return group

    def otherwise(self, group: str) -> None:
        self.action("conditional", GroupingIdentifier=group, WFControlFlowMode=1)

    def end(self, group: str) -> None:
        self.action("conditional", GroupingIdentifier=group, WFControlFlowMode=2)

    def store(self, value: dict) -> None:
        # A text field, as the Shortcuts app writes it; a bare variable stores nothing.
        self.action("setstoredcontent", WFInput=value, WFStoredContentKey=QUEUE_KEY, WFStoredContentGlobalValue=True)

    def notify(self, body: dict) -> None:
        self.action("notification", WFNotificationActionTitle=NAME, WFNotificationActionBody=body,
                    WFNotificationActionSound=False)

    def value(self, dictionary: dict, key: str, output: str) -> dict:
        return self.action("getvalueforkey", output, WFInput=attachment(dictionary), WFDictionaryKey=key,
                           WFGetDictionaryValueType="Value")


def build() -> dict:
    s = Builder()
    s.action("comment", WFCommentActionText=(
        "Saves the shared link in fetchit's \"Saved for later\". Away from home the link waits in a queue; "
        "run without a link (from a Wi-Fi automation) this sends what is waiting.\n\n"
        "The three texts below: fetchit's address, its save_token, and the name of the home Wi-Fi."))
    # An IP address, not homeassistant.local: that name also answers with IPv6
    # addresses, which a phone tries first, and Home Assistant publishes an
    # add-on's port on IPv4 only — iOS then says "the network connection was lost".
    server = s.setting("Server", "The address of fetchit: http://<Home Assistant's IP address>:<the port set "
                       "under the add-on's Network>", "http://<IP address>:8099")
    token = s.setting("Token", "The save_token set in the fetchit add-on's configuration", "")
    home = s.setting("Home Wi-Fi", "The name of your home Wi-Fi. Links wait until you are on it.", "")

    waiting = s.action("getstoredcontent", "Waiting", WFStoredContentKey=QUEUE_KEY, WFStoredContentGlobalValue=True)
    shared = s.action("detect.link", "Shared links", WFInput=attachment(SHORTCUT_INPUT))
    queue = s.action("gettext", "Queue", WFTextActionText=text(waiting, "\n", shared))

    has_links = s.if_start(queue, _CONTAINS, string="http")
    s.store(text(queue))
    wifi = s.action("getwifi", "Network")
    network = s.action("gettext", "Network name", WFTextActionText=text(wifi))

    at_home = s.if_start(network, _CONTAINS, string=text(home))
    response = s.action(
        "downloadurl", "Response", WFURL=text(server, "/api/save"), WFHTTPMethod="POST", WFHTTPBodyType="JSON",
        WFJSONValues=fields(links=text(queue)), WFHTTPHeaders=fields(Authorization=text("Bearer ", token)),
        ShowHeaders=True)
    answer = s.action("detect.dictionary", "Answer", WFInput=attachment(response))
    message = s.value(answer, "message", "Message")

    taken = s.if_start(message, _HAS_ANY_VALUE)
    s.store(text(EMPTY))
    s.notify(text(message))
    s.otherwise(taken)
    error = s.value(answer, "error", "Error")
    s.notify(text("Not sent; kept for the next try. ", error))
    s.end(taken)

    s.otherwise(at_home)
    s.notify(text("Kept until you are on ", home, "."))
    s.end(at_home)
    s.end(has_links)

    return {
        "WFWorkflowActions": s.actions,
        "WFWorkflowClientVersion": "4407",
        "WFWorkflowMinimumClientVersion": 900,
        "WFWorkflowMinimumClientVersionString": "900",
        "WFWorkflowIcon": {"WFWorkflowIconStartColor": 463140863, "WFWorkflowIconGlyphNumber": 59511},
        "WFWorkflowTypes": ["ActionExtension", "WFWorkflowTypeShowInSearch"],
        "WFWorkflowInputContentItemClasses": ["WFURLContentItem", "WFSafariWebPageContentItem",
                                              "WFStringContentItem"],
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
