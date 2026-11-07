"""Basic import and version tests."""

import fetchit


def test_version():
    """Test that version is defined."""
    assert hasattr(fetchit, "__version__")
    assert isinstance(fetchit.__version__, str)
