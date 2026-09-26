from datetime import datetime, timezone
import json
import logging
import os
import uuid

import pytest

from src.daemon.diagnostics import prune_debug
from src.utils import debug_logger


@pytest.fixture
def debug_clock(monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, 21, 10, 11, 12, tzinfo=timezone.utc)

    monkeypatch.setattr(debug_logger, "AGENTS_DEBUG", True)
    monkeypatch.setattr(debug_logger, "datetime", Clock)
    monkeypatch.setattr(uuid, "uuid4", lambda: uuid.UUID(int=1))
    return "2026-09-21/10-11-12.000_00000000000000000000000000000001_route_req.json"


@pytest.mark.parametrize("component", ["ancestor", "root", "date", "file"])
def test_debug_writer_does_not_follow_symlinks(tmp_path, debug_clock, component, caplog):
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "debug"
    target = outside / "keep.json"
    target.write_text("preserve target")
    if component == "ancestor":
        alias = tmp_path / "alias"
        alias.symlink_to(outside, target_is_directory=True)
        root = alias / "debug"
    elif component == "root":
        root.symlink_to(outside, target_is_directory=True)
    elif component == "date":
        root.mkdir()
        (root / "2026-09-21").symlink_to(outside, target_is_directory=True)
    else:
        path = root / debug_clock
        path.parent.mkdir(parents=True)
        path.symlink_to(target)

    with caplog.at_level(logging.WARNING, logger=debug_logger.__name__):
        debug_logger._write_debug("route", "req", {"value": "new"}, directory=root)

    assert target.read_text() == "preserve target"
    assert set(outside.iterdir()) == {target}
    assert len(caplog.records) == 1, "a refused snapshot must say why it is missing"
    if component == "file":
        assert path.is_symlink()


def test_debug_writer_logs_a_failed_write_instead_of_raising(tmp_path, debug_clock, caplog):
    root = tmp_path / "debug"
    root.write_text("a file where the log directory should be")

    with caplog.at_level(logging.WARNING, logger=debug_logger.__name__):
        debug_logger._write_debug("route", "req", {"value": "new"}, directory=root)

    [record] = caplog.records
    assert record.levelno == logging.WARNING
    assert "route/req" in record.getMessage()
    assert record.exc_info is not None


def test_debug_writer_creates_private_json_without_overwriting(tmp_path, debug_clock):
    root = tmp_path / "debug"
    debug_logger._write_debug("route", "req", {"value": "first"}, directory=root)
    path = root / debug_clock
    assert json.loads(path.read_text())["data"] == {"value": "first"}
    assert path.stat().st_mode & 0o777 == 0o600

    debug_logger._write_debug("route", "req", {"value": "second"}, directory=root)

    assert json.loads(path.read_text())["data"] == {"value": "first"}


@pytest.mark.parametrize("days,max_bytes", [(0, 1000), (7, 0)])
def test_debug_pruning_skips_symlinks_and_keeps_normal_retention(tmp_path, days, max_bytes):
    root = tmp_path / "debug"
    date = root / "2026-09-21"
    date.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "keep.json"
    target.write_text("preserve target")
    ordinary = date / "ordinary.json"
    ordinary.write_text("remove this log")
    if days == 0:
        os.utime(ordinary, (1, 1))
    linked_date = root / "2026-09-20"
    linked_date.symlink_to(outside, target_is_directory=True)
    linked_file = date / "linked.json"
    linked_file.symlink_to(target)
    dangling = date / "dangling.json"
    dangling.symlink_to(outside / "missing.json")
    directory_entry = date / "directory.json"
    directory_entry.mkdir()

    prune_debug(root, days=days, max_bytes=max_bytes)

    assert target.read_text() == "preserve target"
    assert linked_date.is_symlink() and linked_file.is_symlink() and dangling.is_symlink()
    assert directory_entry.is_dir()
    assert not ordinary.exists()


@pytest.mark.parametrize("nested", [False, True])
def test_debug_pruning_skips_symlinked_roots_and_ancestors(tmp_path, nested):
    outside = tmp_path / "outside"
    root = outside / "debug" if nested else outside
    date = root / "2026-09-21"
    date.mkdir(parents=True)
    log = date / "keep.json"
    log.write_text("preserve log")
    alias = tmp_path / "alias"
    alias.symlink_to(outside, target_is_directory=True)

    prune_debug(alias / "debug" if nested else alias, days=0, max_bytes=0)

    assert log.read_text() == "preserve log"
