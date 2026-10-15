"""Owned process/write-limit tests run tiny temporary executables, never a model."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import stat
import sys
import threading
import time

import pytest

fcntl = pytest.importorskip("fcntl")

from src.local_inference.config import OwnerConfig, RedisConfig
from src.local_inference.models import LocalInferenceError
from src.local_inference.process import (OwnedProcess, REQUIRED_FLAGS, child_environment,
                                         fingerprint_file, bounded_wait, create_spool_lease,
                                         private_directory, reconcile_inactive_spools, spool_key, port_available)


def test_port_probe_allows_owned_time_wait_but_refuses_live_listener():
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    try:
        assert not port_available(port)
        with socket.create_connection(("127.0.0.1", port), timeout=2) as client:
            accepted, _ = listener.accept()
            accepted.close()  # Active server close leaves the server port in TIME_WAIT.
            assert client.recv(1) == b""
    finally:
        listener.close()
    assert port_available(port)


def fake_executable(tmp_path, *, ignore_term=False, handler_delay=0, write_bytes=0, version="build 11429, commit d81235049"):
    binary = tmp_path / "fake-engine"
    binary.write_text(f"""#!{sys.executable}
import os, signal, sys, time
if '--version' in sys.argv:
    print({version!r})
    raise SystemExit(0)
if '--help' in sys.argv:
    print({' '.join(REQUIRED_FLAGS)!r})
    raise SystemExit(0)
if {ignore_term!r}:
    time.sleep({handler_delay!r})
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    with open({str(tmp_path / "signal-handler-ready")!r}, 'wb') as ready:
        ready.write(b'ready')
if {write_bytes!r}:
    destination = sys.argv[sys.argv.index('--slot-save-path') + 1] + '/limit-test.bin'
    with open(destination, 'wb') as stream:
        stream.write(b'x' * {write_bytes!r})
else:
    os.write(1, b'x' * 200000)
    os.write(2, b'x' * 200000)
    while True:
        time.sleep(0.1)
""")
    binary.chmod(0o700)
    model = tmp_path / "fake-model.gguf"
    model.write_bytes(b"GGUFsynthetic-model")
    config = OwnerConfig(enabled=True, llama_server_binary=str(binary), model_path=str(model),
                         max_output_tokens=8, context_tokens=128, shutdown_grace_seconds=0.2,
                         redis=RedisConfig(chunk_bytes=4096, max_snapshot_bytes=4096,
                                           max_namespace_bytes=16384, max_spool_bytes=16384))
    return binary, model, config


@pytest.mark.asyncio
async def test_concurrent_start_private_state_and_owned_reaping(tmp_path, monkeypatch):
    _, _, config = fake_executable(tmp_path)
    monkeypatch.setenv("LLAMA_ARG_MODEL", "foreign-secret-model")
    monkeypatch.setenv("HTTP_PROXY", "http://secret-proxy")
    process = OwnedProcess(config, tmp_path / "state", check_port=lambda _: True)
    try:
        await asyncio.gather(*(process.ensure_started() for _ in range(5)))
        child = process._child
        assert child is not None and process.alive and process.native_write_limit
        assert process.epoch and process.spool.is_dir()
        assert stat.S_IMODE(process.spool.stat().st_mode) == 0o700
        key = process.spool.parent / "api.key"
        assert stat.S_IMODE(key.stat().st_mode) == 0o600
        assert key.read_text().strip() == process.api_key
        assert len(list(process.directory.glob("engine-*"))) == 1
        assert "foreign-secret-model" not in str(process.argv)
        assert "secret-proxy" not in str(child_environment())
        await process.ensure_started()
        assert process._child is child
        spool = process.spool
        assert await process.fence() is True
        assert child.returncode is not None and not spool.exists()
        assert not process.alive and not process.native_write_limit
    finally:
        await process.close()


@pytest.mark.asyncio
async def test_occupied_port_refuses_without_touching_foreign_listener(tmp_path):
    _, _, config = fake_executable(tmp_path)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0)); listener.listen()
    config = config.model_copy(update={"port": listener.getsockname()[1]})
    process = OwnedProcess(config, tmp_path / "state")
    try:
        with pytest.raises(LocalInferenceError) as caught:
            await process.ensure_started()
        assert caught.value.code == "backend_unavailable" and process._child is None
        assert listener.fileno() >= 0
        with socket.socket() as client:
            client.settimeout(0.1)
            client.connect(listener.getsockname())
    finally:
        listener.close()
        await process.close()


@pytest.mark.asyncio
async def test_ignored_terminate_kills_and_reaps_only_owned_child(tmp_path):
    _, _, config = fake_executable(tmp_path, ignore_term=True, handler_delay=0.1)
    process = OwnedProcess(config, tmp_path / "state", check_port=lambda _: True)
    try:
        await process.ensure_started()
        child = process._child
        # The wrapper acknowledges its write limit before exec, so wait for
        # the fake engine itself to confirm that SIGTERM is now ignored.
        async with asyncio.timeout(5):
            while not (tmp_path / "signal-handler-ready").exists():
                assert child.returncode is None
                await asyncio.sleep(0.01)
        started = time.monotonic()
        assert await process.fence() is True
        assert time.monotonic() - started < 0.6
        assert child.returncode == -signal.SIGKILL
    finally:
        await process.close()


@pytest.mark.asyncio
async def test_actual_child_file_size_limit_is_enforced(tmp_path):
    _, _, config = fake_executable(tmp_path, write_bytes=16384)
    process = OwnedProcess(config, tmp_path / "state", check_port=lambda _: True)
    try:
        await process.ensure_started()
        spool = process.spool
        assert process.native_write_limit
        await bounded_wait(process._child.wait(), 1)
        output = spool / "limit-test.bin"
        assert output.exists() and output.stat().st_size <= 4096
        assert process._child.returncode != 0
    finally:
        await process.close()


@pytest.mark.asyncio
async def test_file_drift_rehashes_and_restarts_with_new_epoch(tmp_path):
    _, model, config = fake_executable(tmp_path)
    process = OwnedProcess(config, tmp_path / "state", check_port=lambda _: True)
    try:
        await process.ensure_started()
        old_child, old_epoch, old_files = process._child, process.epoch, process.files
        model.write_bytes(b"GGUFchanged-model")
        await process.ensure_started()
        assert old_child.returncode is not None and process._child is not old_child
        assert process.epoch != old_epoch and process.files[1].sha256 != old_files[1].sha256
    finally:
        await process.close()


@pytest.mark.asyncio
async def test_wrong_build_and_symlink_state_are_refused(tmp_path):
    _, _, config = fake_executable(tmp_path, version="build 1, commit deadbeef")
    process = OwnedProcess(config, tmp_path / "state", check_port=lambda _: True)
    try:
        with pytest.raises(LocalInferenceError):
            await process.ensure_started()
        assert not process.alive
    finally:
        await process.close()
    _, _, config = fake_executable(tmp_path)
    outside = tmp_path / "outside"; outside.mkdir()
    state = tmp_path / "state"; state.mkdir(exist_ok=True)
    (state / "native").symlink_to(outside, target_is_directory=True)
    process = OwnedProcess(config, state, check_port=lambda _: True)
    try:
        with pytest.raises(LocalInferenceError):
            await process.ensure_started()
        assert not process.alive and not list(outside.iterdir())
    finally:
        await process.close()


def test_fingerprint_rejects_nonregular_and_cancellation(tmp_path):
    path = tmp_path / "model"; path.write_bytes(b"GGUFhello")
    assert fingerprint_file(str(path)).sha256 == hashlib.sha256(b"GGUFhello").hexdigest()
    link = tmp_path / "link"; link.symlink_to(path)
    with pytest.raises(OSError):
        fingerprint_file(str(link))
    stop = threading.Event(); stop.set()
    with pytest.raises(ValueError):
        fingerprint_file(str(path), stop=stop)


@pytest.mark.asyncio
async def test_hashing_uses_private_executor_and_checks_cancellation(tmp_path, monkeypatch):
    _, _, config = fake_executable(tmp_path)
    process = OwnedProcess(config, tmp_path / "state", check_port=lambda _: True)
    started = threading.Event()
    exited = threading.Event()
    def hash_wait(path, *, stop=None):
        assert threading.current_thread().name.startswith("local-native-files")
        started.set()
        while not stop.wait(0.01):
            pass
        exited.set()
        raise ValueError("cancelled")
    monkeypatch.setattr("src.local_inference.process.fingerprint_file", hash_wait)
    work = asyncio.create_task(process.ensure_started())
    while not started.is_set():
        await asyncio.sleep(0.005)
    work.cancel()
    with pytest.raises(asyncio.CancelledError):
        await work
    for _ in range(20):
        if exited.is_set():
            break
        await asyncio.sleep(0.005)
    assert exited.is_set() and not process.alive
    await process.close()


@pytest.mark.asyncio
async def test_unconfirmed_reaping_refuses_new_process(tmp_path):
    _, _, config = fake_executable(tmp_path)
    process = OwnedProcess(config, tmp_path / "state", check_port=lambda _: True)
    gate = asyncio.Event()
    class Unreapable:
        returncode = None
        terminated = killed = False
        def terminate(self): self.terminated = True
        def kill(self): self.killed = True
        async def wait(self):
            while not gate.is_set():
                try:
                    await gate.wait()
                except asyncio.CancelledError:
                    pass
            self.returncode = 0
            return 0
    child = Unreapable(); process._child = child
    try:
        started = time.monotonic()
        assert await process.fence() is False
        assert time.monotonic() - started < 0.5
        assert child.terminated and child.killed and process.state == "unavailable"
        with pytest.raises(LocalInferenceError):
            await process.ensure_started()
        assert process._child is child
    finally:
        gate.set()
        await process.close()


@pytest.mark.asyncio
async def test_disabled_process_performs_no_hash_spawn_or_state_write(tmp_path):
    def refuse(*args, **kwargs):
        raise AssertionError("disabled backend reached a side effect")
    process = OwnedProcess(OwnerConfig(), tmp_path, spawn=refuse, check_port=refuse)
    with pytest.raises(LocalInferenceError) as caught:
        await process.ensure_started()
    assert caught.value.code == "disabled" and not process.directory.exists()
    await process.close()


@pytest.mark.asyncio
async def test_child_spawn_after_close_is_eventually_killed_and_reaped(tmp_path):
    _, _, config = fake_executable(tmp_path)
    process = OwnedProcess(config, tmp_path / "state", check_port=lambda _: True)
    gate = asyncio.Event()
    class LateChild:
        returncode = None
        killed = reaped = False
        def kill(self):
            self.killed = True; self.returncode = -signal.SIGKILL
        def terminate(self): self.kill()
        async def wait(self): self.reaped = True; return self.returncode
    child = LateChild()
    async def spawn_late():
        await gate.wait()
        return child
    process._spawn_task = asyncio.create_task(spawn_late())
    assert await process.close() is False
    assert not child.killed
    gate.set()
    for _ in range(100):
        if child.reaped:
            break
        await asyncio.sleep(0.005)
    assert child.killed and child.reaped and process._child is None


@pytest.mark.asyncio
async def test_native_exec_inherits_lease_after_supervisor_descriptor_is_closed(tmp_path):
    root = tmp_path / "state" / "native"
    private_directory(root)
    key = spool_key(root, create=True)
    run = root / "engine-orphan"
    run.mkdir(mode=0o700)
    (run / "slots").mkdir(mode=0o700)
    lease_fd = create_spool_lease(run, key)
    binary = tmp_path / "tiny-child"
    binary.write_text(f"""#!{sys.executable}
import os, stat, sys, time
assert stat.S_ISREG(os.fstat(int(sys.argv[1])).st_mode)
print('exec-ready', flush=True)
while True:
    time.sleep(0.01)
""")
    binary.chmod(0o700)
    wrapper = Path(__import__("src.local_inference.child_exec", fromlist=["main"]).__file__)
    child = None
    try:
        child = await asyncio.create_subprocess_exec(
            sys.executable, "-I", str(wrapper), "4096", "--lease-fd", str(lease_fd), str(binary), str(lease_fd),
            pass_fds=(lease_fd,), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=child_environment())
        assert await bounded_wait(child.stdout.readline(), 1) == b"agents-core/native-fsize/1:4096\n"
        assert await bounded_wait(child.stdout.readline(), 1) == b"exec-ready\n"
        os.close(lease_fd)
        lease_fd = None  # Simulate supervisor death; only the exec'd child holds it.
        probe = os.open(run / "lease", os.O_RDWR | os.O_NOFOLLOW)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            reconcile_inactive_spools(root, key)
            assert run.is_dir() and child.returncode is None
        finally:
            os.close(probe)
        child.kill()
        await bounded_wait(child.wait(), 1)
        reconcile_inactive_spools(root, key)
        assert not run.exists()
    finally:
        if lease_fd is not None:
            os.close(lease_fd)
        if child is not None and child.returncode is None:
            child.kill()
            await bounded_wait(child.wait(), 1)


def assign_private_run(process):
    private_directory(process.directory)
    key = spool_key(process.directory, create=True)
    process._run_directory = process.directory / "engine-delayed"
    process._run_directory.mkdir(mode=0o700)
    info = process._run_directory.lstat()
    process._run_identity = (info.st_dev, info.st_ino)
    process.spool = process._run_directory / "slots"
    process.spool.mkdir(mode=0o700)
    process._lease_fd = create_spool_lease(process._run_directory, key)
    return process._run_directory, key


@pytest.mark.asyncio
@pytest.mark.parametrize("failed", [False, True])
async def test_uncertain_spawn_retains_lease_until_exact_completion_and_reap(tmp_path, failed):
    _, _, config = fake_executable(tmp_path)
    process = OwnedProcess(config, tmp_path / "state", check_port=lambda _: True)
    run, key = assign_private_run(process)
    gate = asyncio.Event()
    class LateChild:
        returncode = None
        killed = reaped = False
        def kill(self):
            self.killed = True; self.returncode = -signal.SIGKILL
        def terminate(self): self.kill()
        async def wait(self): self.reaped = True; return self.returncode
    child = LateChild()
    async def spawn_late():
        await gate.wait()
        if failed:
            raise OSError("fake spawn failed")
        return child
    process._spawn_task = asyncio.create_task(spawn_late())
    try:
        assert await process.close() is False
        assert process._lease_fd is not None and run.is_dir()
        probe = os.open(run / "lease", os.O_RDWR | os.O_NOFOLLOW)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(probe)
        reconcile_inactive_spools(process.directory, key)
        assert run.is_dir()
        gate.set()
        for _ in range(100):
            if process._lease_fd is None:
                break
            await asyncio.sleep(0.005)
        assert process._lease_fd is None and process._spawn_task is None and not run.exists()
        if not failed:
            assert child.killed and child.reaped
    finally:
        gate.set()
        await process.close()


@pytest.mark.asyncio
async def test_unconfirmed_child_reap_keeps_run_lease_and_orphan_bytes(tmp_path):
    _, _, config = fake_executable(tmp_path)
    process = OwnedProcess(config, tmp_path / "state", check_port=lambda _: True)
    run, key = assign_private_run(process)
    gate = asyncio.Event()
    class Unreapable:
        returncode = None
        def terminate(self): pass
        def kill(self): pass
        async def wait(self):
            while not gate.is_set():
                try:
                    await gate.wait()
                except asyncio.CancelledError:
                    pass
            self.returncode = 0
            return 0
    process._child = Unreapable()
    try:
        assert await process.fence() is False
        assert process._lease_fd is not None and run.is_dir()
        reconcile_inactive_spools(process.directory, key)
        assert run.is_dir()
        gate.set()
        assert await process.close() is True
        assert process._lease_fd is None and not run.exists()
    finally:
        gate.set()
        await process.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("unsafe_key", [False, True])
async def test_legacy_spool_does_not_disable_cold_native_startup(tmp_path, unsafe_key):
    _, _, config = fake_executable(tmp_path)
    process = OwnedProcess(config, tmp_path / "state", check_port=lambda _: True)
    private_directory(process.directory)
    legacy = process.directory / "engine-legacy"
    legacy.mkdir(mode=0o700)
    (legacy / "slots").mkdir(mode=0o700)
    partial = legacy / "slots" / ("snapshot-" + "f" * 32 + ".bin")
    partial.write_bytes(b"old uncertain bytes")
    if unsafe_key:
        foreign = tmp_path / "foreign-key"
        foreign.write_bytes(b"k" * 32)
        (process.directory / ".spool-key").symlink_to(foreign)
    try:
        await process.ensure_started()
        assert process.alive and partial.read_bytes() == b"old uncertain bytes"
    finally:
        await process.close()


@pytest.mark.asyncio
async def test_startup_reclaims_authenticated_inactive_run_before_new_engine(tmp_path):
    _, _, config = fake_executable(tmp_path)
    process = OwnedProcess(config, tmp_path / "state", check_port=lambda _: True)
    old, _ = assign_private_run(process)
    (process.spool / ("snapshot-" + "f" * 32 + ".bin")).write_bytes(b"old partial")
    os.close(process._lease_fd)
    process._lease_fd = process._run_directory = process.spool = None
    try:
        await process.ensure_started()
        assert process.alive and not old.exists() and process._lease_fd is not None
    finally:
        await process.close()


@pytest.mark.asyncio
async def test_confirmed_stop_never_deletes_a_replaced_run_directory(tmp_path):
    _, _, config = fake_executable(tmp_path)
    process = OwnedProcess(config, tmp_path / "state", check_port=lambda _: True)
    run, _ = assign_private_run(process)
    old = run.with_name("engine-renamed")
    run.rename(old)
    run.mkdir(mode=0o700)
    sentinel = run / "foreign-sentinel"
    sentinel.write_bytes(b"keep")
    assert await process.close() is True
    assert sentinel.read_bytes() == b"keep" and old.is_dir()
    assert process._lease_fd is None
