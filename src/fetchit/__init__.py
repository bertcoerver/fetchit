"""fetchit: download feeds into a media library that other tools can read."""

from importlib.metadata import version, PackageNotFoundError

try:
    __version__ = version("fetchit")
except PackageNotFoundError:
    __version__ = "unknown"
