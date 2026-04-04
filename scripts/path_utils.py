"""Shared path helpers for maintenance scripts."""

from pathlib import Path


def resolve_path(primary: Path, fallback: Path = None) -> Path:
    """Return the primary path. The fallback parameter is kept for call-site compatibility but ignored."""
    return primary
