"""graph-me: a local knowledge graph of your personal data, for AI agents."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("graph-me")
except PackageNotFoundError:  # running from a source tree without install
    __version__ = "0.0.0"
