"""Shared path helpers for maintenance scripts."""

from pathlib import Path


def resolve_path(primary: Path, fallback: Path) -> Path:
    """Use the new path if it exists, fall back to legacy .cursor/ path."""
    if primary.exists():
        return primary
    if fallback.exists():
        return fallback
    return primary
