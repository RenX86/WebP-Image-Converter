"""Batch image-to-WebP converter with CLI and Textual TUI."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("webp-tui")
except PackageNotFoundError:
    __version__ = "2.0.2"
