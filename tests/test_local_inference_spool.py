"""Bounded native spool recovery uses private files and tiny fake children only."""
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

fcntl = pytest.importorskip("fcntl")

from src.local_inference.config import OwnerConfig, RedisConfig
from src.local_inference.process import (MAX_SPOOL_RUNS, SpoolStateError, create_spool_lease,
                                         native_spool_bytes, private_directory,
                                         reconcile_inactive_spools, spool_key)
from src.local_inference.redis_store import CacheFailure
from src.local_inference.snapshots import SnapshotManager


def native_state(tmp_path):
    root = tmp_path / "state" / "native"
    private_directory(root)
    return root, spool_key(root, create=True)


def owned_run(root, key, name, *, size=0):
    run = root / ("engine-" + name)
    run.mkdir(mode=0o700)
    fd = create_spool_lease(run, key)
    spool = run / "slots"
    spool.mkdir(mode=0o700)
    if size:
        (spool / ("snapshot-" + "a" * 32 + ".bin")).write_bytes(b"x" * size)
    return run, spool, fd


def manager_for(root, *, maximum=1024):
    config = OwnerConfig(redis=RedisConfig(enabled=True, chunk_bytes=64, max_snapshot_bytes=256,
                                          max_spool_bytes=maximum))
    return SnapshotManager(config, root.parent)


def test_default_backend_and_manager_charge_the_same_installation_spool(tmp_path):
    from src.local_inference.llama import LlamaBackend

    state = tmp_path / "production-state"
    config = OwnerConfig(redis=RedisConfig(enabled=True, chunk_bytes=64, max_snapshot_bytes=256,
                                          max_spool_bytes=1024))
    backend = LlamaBackend(config, state)
    root = backend.process.directory
    assert root == state / "native"
    private_directory(root)
    key = spool_key(root, create=True)
    old, _, old_fd = owned_run(root, key, "old", size=900)
    os.close(old_fd)
    _, spool, fd = owned_run(root, key, "current")
    manager = SnapshotManager(config, state)
    try:
        with pytest.raises(CacheFailure) as caught:
            manager._reserve(spool, 272)
        assert caught.value.code == "snapshot_spool_capacity" and old.is_dir()
        assert manager._reserved == 0
        reconcile_inactive_spools(root, key)
        assert not old.exists()
        manager._reserve(spool, 272)
        assert manager._reserved == 272
    finally:
        os.close(fd)


def test_authenticated_inactive_orphan_bytes_are_charged_until_startup_cleanup(tmp_path):
    root, key = native_state(tmp_path)
    old, _, old_fd = owned_run(root, key, "old", size=900)
    os.close(old_fd)
    current, spool, fd = owned_run(root, key, "current")
    manager = manager_for(root)
    try:
        assert native_spool_bytes(root, spool) == 900
        with pytest.raises(CacheFailure) as caught:
            manager._reserve(spool, 272)
        assert caught.value.code == "snapshot_spool_capacity" and manager._reserved == 0
        assert old.is_dir()
        reconcile_inactive_spools(root, key)
        assert not old.exists() and current.is_dir()
        manager._reserve(spool, 272)
        assert manager._reserved == 272
        probe = os.open(current / "lease", os.O_RDWR | os.O_NOFOLLOW)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(probe)
    finally:
        os.close(fd)


def test_live_old_lease_declines_admission_and_is_never_deleted(tmp_path):
    root, key = native_state(tmp_path)
    old, old_spool, old_fd = owned_run(root, key, "active", size=1)
    _, spool, fd = owned_run(root, key, "current")
    try:
        reconcile_inactive_spools(root, key)
        assert old.is_dir() and next(old_spool.iterdir()).stat().st_size == 1
        with pytest.raises(CacheFailure) as caught:
            manager_for(root)._reserve(spool, 1)
        assert caught.value.code == "snapshot_spool_capacity"
    finally:
        os.close(old_fd)
        os.close(fd)


@pytest.mark.parametrize("kind", ["legacy", "bad_signature", "copied_marker", "symlink", "directory", "fifo",
                                  "hardlink", "writable", "unknown_file", "unsafe_directory", "replaced_lease"])
def test_unknown_or_unsafe_old_runs_are_preserved_and_fail_closed(tmp_path, kind):
    root, key = native_state(tmp_path)
    old, old_spool, old_fd = owned_run(root, key, "old", size=600)
    os.close(old_fd)
    snapshot = next(old_spool.iterdir())
    outside = tmp_path / "outside"
    outside.write_bytes(b"foreign sentinel")
    if kind == "legacy":
        (old / "lease").unlink()
    elif kind == "bad_signature":
        value = (old / "lease").read_bytes()
        (old / "lease").write_bytes(value[:-2] + b"z\n")
    elif kind == "replaced_lease":
        value = (old / "lease").read_bytes()
        (old / "lease").rename(tmp_path / "detached-lease")
        (old / "lease").write_bytes(value)
        (old / "lease").chmod(0o600)
    elif kind == "copied_marker":
        donor, _, donor_fd = owned_run(root, key, "donor")
        (old / "lease").write_bytes((donor / "lease").read_bytes())
        os.close(donor_fd)
    elif kind == "symlink":
        snapshot.unlink(); snapshot.symlink_to(outside)
    elif kind == "directory":
        snapshot.unlink(); snapshot.mkdir()
    elif kind == "fifo":
        snapshot.unlink(); os.mkfifo(snapshot)
    elif kind == "hardlink":
        snapshot.unlink(); os.link(outside, snapshot)
    elif kind == "writable":
        snapshot.chmod(0o666)
    elif kind == "unknown_file":
        (old_spool / "foreign.bin").write_bytes(b"keep")
    elif kind == "unsafe_directory":
        old_spool.chmod(0o777)
    _, spool, fd = owned_run(root, key, "current")
    try:
        reconcile_inactive_spools(root, key)
        assert old.is_dir() and outside.read_bytes() == b"foreign sentinel"
        with pytest.raises(CacheFailure) as caught:
            manager_for(root)._reserve(spool, 1)
        assert caught.value.code == "snapshot_spool_capacity"
    finally:
        os.close(fd)


def test_bounded_scan_refuses_excess_entries_without_deleting_a_partial_run(tmp_path, monkeypatch):
    root, key = native_state(tmp_path)
    old, old_spool, fd = owned_run(root, key, "old", size=1)
    os.close(fd)
    for index in range(10):
        (old_spool / ("snapshot-" + f"{index:032x}" + ".bin")).write_bytes(b"x")
    monkeypatch.setattr("src.local_inference.process.MAX_SPOOL_ENTRIES", 5)
    reconcile_inactive_spools(root, key)
    assert old.is_dir() and len(list(old_spool.iterdir())) == 11
    with pytest.raises(SpoolStateError):
        native_spool_bytes(root, old_spool)


def test_bounded_run_scan_refuses_optional_admission(tmp_path):
    root, key = native_state(tmp_path)
    _, spool, fd = owned_run(root, key, "current")
    try:
        for index in range(MAX_SPOOL_RUNS):
            old, _, old_fd = owned_run(root, key, str(index))
            # Retain inactive owned runs for one complete accounting scan;
            # startup reconciliation is deliberately not invoked here.
            os.close(old_fd)
        with pytest.raises(CacheFailure):
            manager_for(root)._reserve(spool, 1)
    finally:
        os.close(fd)


def test_current_spool_scan_is_bounded_and_never_follows_nonregular_files(tmp_path, monkeypatch):
    spool = tmp_path / "spool"
    spool.mkdir(mode=0o700)
    for index in range(4):
        (spool / f"unrelated-{index}").write_bytes(b"x")
    manager = SnapshotManager(OwnerConfig(), tmp_path / "state")
    monkeypatch.setattr("src.local_inference.snapshots.MAX_SPOOL_ENTRIES", 3)
    with pytest.raises(CacheFailure) as caught:
        manager._spool(SimpleNamespace(spool=spool))
    assert caught.value.code == "snapshot_capacity"


def test_symlink_native_namespace_and_key_refuse_snapshot_admission(tmp_path):
    state = tmp_path / "state"
    private_directory(state)
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    spool = tmp_path / "spool"
    spool.mkdir(mode=0o700)
    (state / "native").symlink_to(outside, target_is_directory=True)
    with pytest.raises(CacheFailure):
        manager_for(state / "native")._reserve(spool, 1)
    (state / "native").unlink()
    root, _ = native_state(tmp_path)
    (root / ".spool-key").unlink()
    (root / ".spool-key").symlink_to(tmp_path / "missing")
    with pytest.raises(CacheFailure):
        manager_for(root)._reserve(spool, 1)
