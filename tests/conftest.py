"""Shared fixtures: keep every test away from the real cache, log, network and dotenv."""

import pytest

from fetchit import cache, config, download, feeds
from fetchit.errorlog import DEFAULT_LOG_PATH, LOG_ENV


@pytest.fixture(autouse=True)
def clean_cache(tmp_path, monkeypatch):
    """Give every test its own empty cache, and never the real one.

    A remembered channel id or ETag would otherwise silently skip the very
    lookup a test is about, and it would pass for the wrong reason. Both the
    file and the in-memory copy are reset, since the module keeps what it read.
    """
    monkeypatch.setenv(cache.CACHE_ENV, str(tmp_path / "cache.json"))
    cache.clear()
    yield
    cache.clear()


@pytest.fixture(autouse=True)
def error_log(tmp_path, monkeypatch):
    """Redirect the traceback log into ``tmp_path``, and yield its path."""
    path = tmp_path / DEFAULT_LOG_PATH
    monkeypatch.setenv(LOG_ENV, str(path))
    return path


@pytest.fixture(autouse=True)
def fresh_dotenv(monkeypatch):
    """Don't let one test's ``.env``, or the developer's environment, leak in."""
    for name in ("FETCHIT_LIBRARY", "FETCHIT_CONFIG", "FETCHIT_KEEP_DAYS", "FETCHIT_SCHEDULE", "FETCHIT_YOUTUBE_COOKIES",
                 "FETCHIT_YOUTUBE_COOKIES_FROM_BROWSER"):
        monkeypatch.delenv(name, raising=False)
    config.forget_dotenv()
    yield
    config.forget_dotenv()


@pytest.fixture(autouse=True)
def no_retry_backoff(monkeypatch):
    """Keep the network retry, drop its wait."""
    monkeypatch.setattr(feeds.time, "sleep", lambda _seconds: None)


@pytest.fixture(autouse=True)
def source_reachable(monkeypatch):
    """Answer the source reachability probe without a network.

    :func:`fetchit.youtube.check_available` runs ahead of every feed parse, and
    the probe behind it is a real request. Tests that care about the blocked
    path override this with ``lambda url, **kw: False``.
    """
    monkeypatch.setattr(download, "reachable", lambda url, **kwargs: True)
