"""Configuration: the config file that names the feeds, and the environment.

The config file says *what* to fetch, and is the same wherever fetchit runs::

    {
      "keep_days": 30,
      "quality": {"max_height": 1080},
      "feeds": [
        {"name": "Le Dessous des Images",
         "url": "https://www.arte.tv/fr/videos/RC-023176/le-dessous-des-images/"}
      ]
    }

The environment says where things are on this machine: ``FETCHIT_LIBRARY``
for the library (overriding the file's ``library``), plus the cache, log and
YouTube cookie settings each module reads for itself. Environment variables can
be put in a ``.env`` file beside the config, which is read once per process.
"""

from __future__ import annotations

import functools
import json
import os
from dataclasses import dataclass, field

from .feeds import FeedConfig, FeedError, load_feeds
from .quality import Quality, parse_quality

CONFIG_ENV = "FETCHIT_CONFIG"
LIBRARY_ENV = "FETCHIT_LIBRARY"
# The library-wide keep_days when the config file does not say: how the
# Home Assistant add-on's option reaches a config file that stays the same
# one a laptop uses.
KEEP_DAYS_ENV = "FETCHIT_KEEP_DAYS"

DEFAULT_CONFIG = "fetchit.json"
DEFAULT_LIBRARY = "~/fetchit"

TOP_LEVEL_KEYS = ("library", "keep_days", "quality", "feeds")


class ConfigError(FeedError):
    """Raised when the config file is missing, unreadable, or malformed."""


@dataclass(frozen=True)
class Config:
    """What one run works from.

    ``keep_days`` is how long a download stays in the library unless its feed
    says otherwise; ``0`` keeps everything, and is the default, because a tool
    that deletes files nobody asked it to delete is a worse surprise than a
    full disk.
    """

    library: str
    keep_days: int = 0
    quality: Quality = Quality()
    feeds: list[FeedConfig] = field(default_factory=list)

    def keep_days_for(self, feed: FeedConfig) -> int:
        """How long ``feed``'s downloads stay, in days; ``0`` is for ever."""
        return self.keep_days if feed.keep_days is None else feed.keep_days


def config_path(path: str | None = None) -> str:
    """The config file to read: ``path``, ``$FETCHIT_CONFIG``, or ``fetchit.json``."""
    return path or os.environ.get(CONFIG_ENV, "").strip() or DEFAULT_CONFIG


def load_config(path: str | None = None, *, library: str | None = None) -> Config:
    """Read the config file at ``path`` (see :func:`config_path`).

    ``library`` overrides where the library is; failing that, so does
    ``$FETCHIT_LIBRARY``, then the file's own ``library``, then ``~/fetchit``.
    Raises :class:`ConfigError` naming what is wrong.
    """
    load_dotenv_once()
    path = config_path(path)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError as exc:
        raise ConfigError(
            f"Config not found at {path!r}. Copy fetchit.example.json to "
            "fetchit.json and edit it, or pass --config."
        ) from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Config {path!r} is not valid JSON: {exc}") from exc

    if not isinstance(data, dict) or not isinstance(data.get("feeds"), list):
        raise ConfigError(f"Config {path!r} must be an object with a \"feeds\" array.")
    unknown = set(data) - set(TOP_LEVEL_KEYS)
    if unknown:
        raise ConfigError(
            f"Config {path!r} has unknown key(s) {sorted(unknown)}; expected "
            f"{list(TOP_LEVEL_KEYS)}."
        )

    keep_days = data.get("keep_days")
    if keep_days is None:
        try:
            keep_days = int(os.environ.get(KEEP_DAYS_ENV, "").strip() or 0)
        except ValueError as exc:
            raise ConfigError(f"{KEEP_DAYS_ENV} must be a whole number of days.") from exc
    if isinstance(keep_days, bool) or not isinstance(keep_days, int) or keep_days < 0:
        raise ConfigError("keep_days must be a whole number of days; 0 keeps everything.")

    try:
        quality = parse_quality(data.get("quality"), "config")
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc

    root = (
        library
        or os.environ.get(LIBRARY_ENV, "").strip()
        or data.get("library")
        or DEFAULT_LIBRARY
    )
    if not isinstance(root, str):
        raise ConfigError("library must be the path of a folder.")

    return Config(
        library=os.path.abspath(os.path.expanduser(root)),
        keep_days=keep_days,
        quality=quality,
        feeds=load_feeds(data["feeds"], quality=quality),
    )


def load_dotenv(path: str = ".env", *, override: bool = False) -> dict[str, str]:
    """Load ``KEY=VALUE`` pairs from a `.env` file into ``os.environ``.

    Lines that are blank or start with ``#`` are ignored. Values may be wrapped
    in matching single or double quotes, which are stripped. A missing file is
    not an error and yields an empty result.

    By default existing environment variables win (so real env vars set by the
    shell or an add-on's options are not clobbered); pass ``override=True`` to
    let the file take precedence.

    Returns the mapping that was parsed from the file.
    """
    parsed: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.readlines()
    except FileNotFoundError:
        return parsed

    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        parsed[key] = value
        if override or key not in os.environ:
            os.environ[key] = value

    return parsed


@functools.lru_cache(maxsize=None)
def load_dotenv_once(path: str = ".env") -> None:
    """:func:`load_dotenv`, but only the first time for a given path."""
    load_dotenv(path)


def forget_dotenv() -> None:
    """Let the next :func:`load_dotenv_once` read the ``.env`` file again."""
    load_dotenv_once.cache_clear()
