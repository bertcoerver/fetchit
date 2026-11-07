"""Tests for fetchit.errorlog (the traceback file an unattended run leaves)."""

import os

from fetchit import errorlog
from fetchit.errorlog import DEFAULT_LOG_PATH, log_exception, log_path


def _boom(message="kaboom"):
    """A real raised exception, so there is a real traceback to format."""
    try:
        raise ValueError(message)
    except ValueError as exc:
        return exc


def test_log_path_defaults_next_to_the_env_file(monkeypatch, tmp_path):
    monkeypatch.delenv("FETCHIT_LOG", raising=False)
    monkeypatch.chdir(tmp_path)

    assert log_path() == DEFAULT_LOG_PATH


def test_log_path_can_be_set_in_the_env_file(monkeypatch, tmp_path):
    monkeypatch.delenv("FETCHIT_LOG", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("FETCHIT_LOG=/var/log/fetchit.log\n")

    assert log_path() == "/var/log/fetchit.log"


def test_log_exception_writes_the_whole_traceback(error_log):
    written = log_exception("feed 'Blog'", _boom())

    assert written == str(error_log)
    logged = error_log.read_text()
    assert "feed 'Blog'" in logged
    assert "Traceback (most recent call last)" in logged
    assert "ValueError: kaboom" in logged
    assert "_boom" in logged  # the frame it came from, not just the message


def test_log_exception_keeps_the_cause(error_log):
    try:
        try:
            raise OSError("connection reset")
        except OSError as cause:
            raise ValueError("could not read the feed") from cause
    except ValueError as exc:
        log_exception("feed 'Blog'", exc)

    logged = error_log.read_text()
    assert "connection reset" in logged
    assert "direct cause" in logged


def test_log_exception_appends(error_log):
    log_exception("first", _boom("one"))
    log_exception("second", _boom("two"))

    logged = error_log.read_text()
    assert logged.index("one") < logged.index("two")


def test_log_rotates_once_it_gets_big(monkeypatch, error_log):
    monkeypatch.setattr(errorlog, "MAX_LOG_BYTES", 100)
    error_log.write_text("x" * 200)

    log_exception("feed 'Blog'", _boom())

    # The old contents moved aside rather than being thrown away: the failure
    # somebody came looking for is usually the older one.
    assert error_log.with_suffix(".log.1").read_text() == "x" * 200
    assert "ValueError: kaboom" in error_log.read_text()


def test_log_that_cannot_be_written_is_reported_not_raised(
    monkeypatch, capsys, tmp_path
):
    monkeypatch.setenv("FETCHIT_LOG", str(tmp_path / "missing-dir" / "fetchit.log"))

    assert log_exception("feed 'Blog'", _boom()) is None
    assert "could not write to" in capsys.readouterr().err


def test_log_path_is_absolute_so_it_can_be_pasted(error_log):
    written = log_exception("feed 'Blog'", _boom())

    assert written is not None and os.path.isabs(written)
