"""One persisted UTC calendar version for the server, plugin and release tag."""
from datetime import datetime, timezone
from pathlib import Path
import re

FORMAT = "%y.%m.%d.%H%M"


def parse(value: str) -> datetime:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{2}\.[0-9]{2}\.[0-9]{2}\.[0-9]{4}", value):
        raise ValueError("Version must have the form YY.MM.DD.HHMM")
    return datetime.strptime(value, FORMAT).replace(tzinfo=timezone.utc)


def read(root: Path) -> str | None:
    try:
        value = (root / "VERSION").read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    parse(value)
    return value
