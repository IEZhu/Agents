"""Own exactly one private POSIX engine; never attach to an existing listener."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import hashlib
import hmac
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import sys
import tempfile
import threading

from .child_exec import LIMIT_ACK
from .config import OwnerConfig
from .models import LocalInferenceError
from .profile import new_engine_epoch

PINNED_COMMIT = "d81235049384534c167caea52b85a694f6103d14"
PINNED_BUILD = 11429
REQUIRED_FLAGS = ("--model", "--host", "--parallel", "--ctx-size", "--gpu-layers",
                  "--threads", "--threads-batch", "--cache-type-k", "--cache-type-v",
                  "--api-key-file", "--slot-save-path", "--cache-ram", "--no-cache-idle-slots",
                  "--no-context-shift", "--no-ui", "--no-agent", "--no-warmup", "--offline",
                  "--log-disable", "--cache-reuse", "--flash-attn", "--reasoning")


def private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) & 0o077):
        raise LocalInferenceError("backend_unavailable", "Private backend state is unavailable.")


def socket_directory() -> Path:
    """Allocate a short private namespace below protected system ancestors."""
    parent = Path("/tmp").resolve(strict=True)
    for path in (parent, *parent.parents):
        info = path.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, os.geteuid())
                or (stat.S_IMODE(info.st_mode) & 0o022 and not info.st_mode & stat.S_ISVTX)):
            raise LocalInferenceError("backend_unavailable", "The native socket namespace is unsafe.")
    return Path(tempfile.mkdtemp(prefix="agents-native-", dir=parent))


def child_environment() -> dict[str, str]:
    # No inherited LLAMA_*, proxy, HF credentials, DYLD/LD injection or Python
    # startup variables. Both executable paths are explicit, and downloads are off.
    return {"PATH": os.defpath, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}


def file_identity(path: str) -> tuple[int, ...]:
    value = os.stat(path, follow_symlinks=False)
    if not stat.S_ISREG(value.st_mode):
        raise ValueError("not a regular file")
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


@dataclass(frozen=True)
class FileFingerprint:
    identity: tuple[int, ...]
    sha256: str


def fingerprint_file(path: str, *, stop: threading.Event | None = None) -> FileFingerprint:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("not a regular file")
        identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
        digest = hashlib.sha256()
        while True:
            if stop is not None and stop.is_set():
                raise ValueError("hashing cancelled")
            block = os.read(fd, 1024 * 1024)
            if not block:
                break
            digest.update(block)
        after = os.fstat(fd)
        actual = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
        if identity != actual or file_identity(path) != identity:
            raise ValueError("file changed while hashing")
        return FileFingerprint(identity, digest.hexdigest())
    finally:
        os.close(fd)


async def bounded_wait(awaitable, seconds: float):
    """A deadline bounds the caller even when an injected awaitable ignores cancellation."""
    task = asyncio.ensure_future(awaitable)
    try:
        done, _ = await asyncio.wait((task,), timeout=seconds)
    except BaseException:
        task.cancel()
        task.add_done_callback(lambda value: None if value.cancelled() else value.exception())
        raise
    if task not in done:
        task.cancel()
        task.add_done_callback(lambda value: None if value.cancelled() else value.exception())
        raise asyncio.TimeoutError()
    return task.result()


# Native run leases survive supervisor death through the inherited open file
# description. Neither PID reuse nor a directory name is proof of ownership.
_SPOOL_KEY = ".spool-key"
_LEASE = "lease"
_RUN_NAME = re.compile(r"^engine-[A-Za-z0-9_-]{1,64}$")
_SNAPSHOT_NAME = re.compile(r"^snapshot-[a-f0-9]{32}\.bin$")
MAX_SPOOL_RUNS = 128
MAX_SPOOL_ENTRIES = 1024


class SpoolStateError(ValueError):
    pass


def _private_info(path: Path, *, directory=False):
    value = path.lstat()
    valid = stat.S_ISDIR(value.st_mode) if directory else stat.S_ISREG(value.st_mode)
    if (not valid or value.st_uid != os.geteuid() or stat.S_IMODE(value.st_mode) & 0o077
            or (not directory and value.st_nlink != 1)):
        raise SpoolStateError("unsafe native state")
    return value


def bounded_spool_entries(path: Path, budget: list[int]):
    """Never materialize or traverse an unbounded directory listing."""
    with os.scandir(path) as entries:
        for entry in entries:
            if budget[0] <= 0:
                raise SpoolStateError("native state entry limit")
            budget[0] -= 1
            yield Path(entry.path), entry.stat(follow_symlinks=False)


def spool_key(directory: Path, *, create=False) -> bytes:
    path = directory / _SPOOL_KEY
    if create:
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "wb") as stream:
                stream.write(secrets.token_bytes(32))
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        value = os.fstat(fd)
        if (not stat.S_ISREG(value.st_mode) or value.st_uid != os.geteuid()
                or stat.S_IMODE(value.st_mode) & 0o077 or value.st_nlink != 1 or value.st_size != 32):
            raise SpoolStateError("unsafe native key")
        key = os.read(fd, 33)
        if len(key) != 32:
            raise SpoolStateError("unsafe native key")
        return key
    finally:
        os.close(fd)


def create_spool_lease(run: Path, key: bytes | None) -> int:
    import fcntl

    value = _private_info(run, directory=True)
    fd = os.open(run / _LEASE, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lease = os.fstat(fd)
        marker = (f"agents-core/native-lease/1:{run.name}:{value.st_dev}:{value.st_ino}:"
                  f"{lease.st_dev}:{lease.st_ino}:{secrets.token_hex(16)}\n").encode("ascii")
        # An unavailable installation key must not disable generation. Such a
        # run remains unauthenticated for future cleanup/admission.
        signature = hmac.new(key or secrets.token_bytes(32), marker, hashlib.sha256).hexdigest().encode("ascii")
        os.write(fd, marker + signature + b"\n")
        return fd
    except BaseException:
        os.close(fd)
        raise


def _open_spool_lease(run: Path, key: bytes) -> int:
    value = _private_info(run, directory=True)
    fd = os.open(run / _LEASE, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        lease = os.fstat(fd)
        if (not stat.S_ISREG(lease.st_mode) or lease.st_uid != os.geteuid() or lease.st_nlink != 1
                or stat.S_IMODE(lease.st_mode) & 0o077 or not 1 <= lease.st_size <= 512):
            raise SpoolStateError("unsafe native lease")
        data = os.pread(fd, 513, 0)
        parts = data.split(b"\n")
        if len(parts) != 3:
            raise SpoolStateError("malformed native lease")
        marker, signature, empty = parts
        prefix = (f"agents-core/native-lease/1:{run.name}:{value.st_dev}:{value.st_ino}:"
                  f"{lease.st_dev}:{lease.st_ino}:").encode("ascii")
        if (empty or not marker.startswith(prefix) or len(marker) != len(prefix) + 32
                or re.fullmatch(b"[a-f0-9]{32}", marker[len(prefix):]) is None
                or not hmac.compare_digest(signature, hmac.new(key, marker + b"\n", hashlib.sha256)
                                           .hexdigest().encode("ascii"))):
            raise SpoolStateError("unauthenticated native lease")
        return fd
    except BaseException:
        os.close(fd)
        raise


def _slot_files(spool: Path, budget: list[int], *, owned_only=False):
    _private_info(spool, directory=True)
    files = []
    for entry, value in bounded_spool_entries(spool, budget):
        if (not stat.S_ISREG(value.st_mode) or value.st_uid != os.geteuid() or value.st_nlink != 1
                or stat.S_IMODE(value.st_mode) & 0o022 or (owned_only and not _SNAPSHOT_NAME.fullmatch(entry.name))):
            raise SpoolStateError("unsafe native spool")
        files.append((entry, value))
    return files


def _owned_run_files(run: Path, budget: list[int]):
    """Authenticate separately, then accept only the layout this version creates."""
    files, slots = [], None
    for entry, value in bounded_spool_entries(run, budget):
        if entry.name == "slots":
            slots = entry
            files.extend(_slot_files(entry, budget, owned_only=True))
        elif entry.name in {_LEASE, "api.key"}:
            _private_info(entry)
            files.append((entry, value))
        else:
            raise SpoolStateError("unknown native run state")
    return files, slots


def reconcile_inactive_spools(directory: Path, key: bytes) -> None:
    """Best effort, bounded, and never signal a process discovered on disk."""
    import fcntl

    budget = [MAX_SPOOL_ENTRIES]
    try:
        _private_info(directory, directory=True)
        for index, (run, _) in enumerate(bounded_spool_entries(directory, budget)):
            if index >= MAX_SPOOL_RUNS:
                return
            if run.name == _SPOOL_KEY or not _RUN_NAME.fullmatch(run.name):
                continue
            fd = None
            try:
                fd = _open_spool_lease(run, key)
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                files, slots = _owned_run_files(run, budget)
                # No holder can still launch/write this run: the supervisor owns
                # the lease before spawning and passes that same lock through exec.
                for path, before in files:
                    after = path.lstat()
                    if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                        raise SpoolStateError("native state changed")
                for path, _ in files:
                    if path.name != _LEASE:
                        path.unlink()
                if slots is not None:
                    slots.rmdir()
                (run / _LEASE).unlink()
                run.rmdir()
            except (OSError, ValueError):
                # Legacy, busy, malformed and unsafe runs remain charged or make
                # optional admission fail closed; none justify deleting state.
                pass
            finally:
                if fd is not None:
                    os.close(fd)
    except (OSError, ValueError):
        return


def native_spool_bytes(directory: Path, current: Path) -> int:
    """Count installation bytes; uncertain writers/state forbid optional admission."""
    import fcntl

    budget = [MAX_SPOOL_ENTRIES]
    current_info = _private_info(current, directory=True)
    current_identity = (current_info.st_dev, current_info.st_ino)
    try:
        directory.lstat()
    except FileNotFoundError:
        return sum(value.st_size for _, value in _slot_files(current, budget))
    _private_info(directory, directory=True)
    key = spool_key(directory)
    total, seen = 0, False
    for index, (run, _) in enumerate(bounded_spool_entries(directory, budget)):
        if index >= MAX_SPOOL_RUNS:
            raise SpoolStateError("native state run limit")
        if run.name == _SPOOL_KEY:
            continue
        if not _RUN_NAME.fullmatch(run.name):
            raise SpoolStateError("unknown native state")
        fd = _open_spool_lease(run, key)
        try:
            spool = run / "slots"
            value = _private_info(spool, directory=True)
            is_current = (value.st_dev, value.st_ino) == current_identity
            if is_current:
                seen = True
            else:
                # A live old supervisor/engine can grow another partial after a
                # scan. Its remaining capacity is unknown, so decline admission.
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                files, _ = _owned_run_files(run, budget)
                total += sum(value.st_size for path, value in files if path.parent == spool)
                continue
            total += sum(value.st_size for _, value in _slot_files(spool, budget))
        finally:
            os.close(fd)
    if not seen:
        total += sum(value.st_size for _, value in _slot_files(current, budget))
    return total


class OwnedProcess:
    def __init__(self, config: OwnerConfig, directory: Path | str, *,
                 spawn=asyncio.create_subprocess_exec):
        self.config = config
        self.directory = Path(directory) / "native"
        self._spawn = spawn
        self._lock = asyncio.Lock()
        self._pid = os.getpid()
        self._child = None
        self._spawn_task = None
        self._drains = []
        self._run_directory = None
        self._run_identity = None
        self._socket_directory = None
        self._socket_identity = None
        self._lease_fd = None
        self.spool = None
        self.api_key = None
        self.epoch = new_engine_epoch()
        self.files: tuple[FileFingerprint, FileFingerprint] | None = None
        self.build = None
        self.native_write_limit = False
        self.state = "not_started"
        self._closed = False
        self._files_executor = None
        self._hash_stop = None
        self._late_spawn_cleanup = None

    async def _file(self, function):
        if self._files_executor is None:
            self._files_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="local-native-files")
        return await asyncio.get_running_loop().run_in_executor(self._files_executor, function)

    async def _hash_files(self):
        stop = threading.Event()
        self._hash_stop = stop
        try:
            return await self._file(lambda: (fingerprint_file(self.config.llama_server_binary, stop=stop),
                                            fingerprint_file(self.config.model_path, stop=stop)))
        finally:
            stop.set()
            self._hash_stop = None

    @property
    def alive(self) -> bool:
        return self._child is not None and self._child.returncode is None

    @property
    def base_url(self) -> str:
        # HTTP authority only. The client must use the private Unix transport.
        return "http://local-engine"

    @property
    def socket_path(self) -> Path:
        """Return only this epoch's private socket path, never a TCP fallback."""
        if self._socket_directory is None:
            raise LocalInferenceError("backend_unavailable", "The private native endpoint is unavailable.")
        try:
            info = self._socket_directory.lstat()
        except OSError:
            raise LocalInferenceError("backend_unavailable", "The private native endpoint is unavailable.") from None
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) & 0o077
                or (info.st_dev, info.st_ino) != self._socket_identity):
            raise LocalInferenceError("backend_unavailable", "The private native endpoint changed.")
        path = self._socket_directory / "engine.sock"
        try:
            endpoint = path.lstat()
        except FileNotFoundError:
            return path  # The wrapper acknowledgement can precede native bind.
        except OSError:
            raise LocalInferenceError("backend_unavailable", "The private native endpoint is unavailable.") from None
        if not stat.S_ISSOCK(endpoint.st_mode) or endpoint.st_uid != os.geteuid():
            raise LocalInferenceError("backend_unavailable", "The private native endpoint changed.")
        return path

    @property
    def argv(self) -> tuple[str, ...]:
        """Exact effective engine arguments, excluding the private key location."""
        return self._arguments("<private-api-key-file>", "<private-slot-directory>", "<private-engine.sock>")

    def _arguments(self, key_path: str, spool: str, endpoint: str) -> tuple[str, ...]:
        c = self.config
        return (c.llama_server_binary, "--model", c.model_path, "--host", endpoint,
                "--parallel", "1", "--ctx-size", str(c.context_tokens), "--gpu-layers", str(c.gpu_layers),
                "--threads", str(c.threads), "--threads-batch", str(c.threads),
                "--cache-type-k", c.kv_type_k, "--cache-type-v", c.kv_type_v,
                "--flash-attn", "on", "--cache-ram", "0", "--cache-reuse", "0",
                "--no-cache-idle-slots", "--no-context-shift", "--no-ui", "--no-agent", "--no-warmup",
                "--offline", "--log-disable", "--reasoning", "off", "--api-key-file", key_path,
                "--slot-save-path", spool)

    async def _inspect(self, flag: str) -> str:
        self._spawn_task = asyncio.create_task(self._spawn(
            self.config.llama_server_binary, flag, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=child_environment(), limit=262144))
        child = await asyncio.shield(self._spawn_task)
        self._child, self._spawn_task = child, None
        async def collect(stream):
            chunks, count = [], 0
            while chunk := await stream.read(65536):
                count += len(chunk)
                if count > 262144:
                    raise ValueError("inspection output too large")
                chunks.append(chunk)
            return b"".join(chunks)
        try:
            async with asyncio.timeout(10):
                out, err = await asyncio.gather(collect(child.stdout), collect(child.stderr))
                await child.wait()
            if child.returncode != 0:
                raise ValueError("inspection failed")
            return (out + err).decode("utf-8", errors="strict")
        finally:
            if child.returncode is None:
                child.kill()
                try:
                    await bounded_wait(child.wait(), 1)
                except asyncio.TimeoutError:
                    raise LocalInferenceError("backend_unavailable", "Engine inspection could not be reaped.") from None
            if child.returncode is not None:
                self._child = None

    async def _drain(self, stream):
        # Bounded reads; prompt/error output is discarded rather than retained.
        while await stream.read(65536):
            pass

    async def ensure_started(self) -> None:
        if os.getpid() != self._pid:
            raise LocalInferenceError("backend_unavailable", "The backend cannot be used after process fork.")
        async with self._lock:
            if self._closed:
                raise LocalInferenceError("backend_unavailable", "The backend is stopping.")
            if not self.config.enabled:
                raise LocalInferenceError("disabled", "Local inference is disabled.")
            if os.name != "posix" or sys.platform not in ("darwin", "linux"):
                raise LocalInferenceError("unsupported", "The local backend requires POSIX macOS or Linux.")
            try:
                identities = await self._file(lambda: (file_identity(self.config.llama_server_binary),
                                                       file_identity(self.config.model_path)))
                changed = (self.files is None or self.build is None
                           or identities != tuple(item.identity for item in self.files))
                if self.alive and not changed:
                    return
                if self._child is not None or self._spawn_task is not None:
                    if not await self._stop_locked():
                        raise LocalInferenceError("backend_unavailable", "The preceding engine could not be reaped.")
                self.state = "starting"
                if changed:
                    self.build = None
                    self.files = await self._hash_files()
                    version = await self._inspect("--version")
                    if not re.search(r"build 11429, commit d81235049\b", version):
                        raise ValueError("unsupported engine build")
                    help_text = await self._inspect("--help")
                    if any(flag not in help_text for flag in REQUIRED_FLAGS):
                        raise ValueError("missing engine flags")
                    self.build = f"b{PINNED_BUILD}-{PINNED_COMMIT}"
                if await self._file(lambda: (file_identity(self.config.llama_server_binary),
                                             file_identity(self.config.model_path))) != tuple(item.identity for item in self.files):
                    raise ValueError("native files changed before launch")
                private_directory(self.directory)
                try:
                    lease_key = spool_key(self.directory, create=True)
                    reconcile_inactive_spools(self.directory, lease_key)
                except (OSError, ValueError):
                    lease_key = None
                self._run_directory = Path(tempfile.mkdtemp(prefix="engine-", dir=self.directory))
                run_info = self._run_directory.lstat()
                self._run_identity = (run_info.st_dev, run_info.st_ino)
                # Keep the sockaddr_un path below macOS's 104-byte limit even
                # when the owner's persistent state path is much longer.
                self._socket_directory = socket_directory()
                private_directory(self._socket_directory)
                socket_info = self._socket_directory.lstat()
                self._socket_identity = (socket_info.st_dev, socket_info.st_ino)
                endpoint = self.socket_path
                if len(os.fsencode(endpoint)) >= 104 or "," in str(endpoint):
                    raise ValueError("unsupported native socket path")
                self._lease_fd = create_spool_lease(self._run_directory, lease_key)
                self.spool = self._run_directory / "slots"
                self.spool.mkdir(mode=0o700)
                self.api_key = secrets.token_urlsafe(32)
                key = self._run_directory / "api.key"
                fd = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w", encoding="ascii") as stream:
                    stream.write(self.api_key + "\n")
                limit = self.config.redis.max_snapshot_bytes
                engine_argv = self._arguments(str(key), str(self.spool), str(endpoint))
                self._spawn_task = asyncio.create_task(self._spawn(
                    sys.executable, "-I", str(Path(__file__).with_name("child_exec.py")), str(limit), "--lease-fd", str(self._lease_fd), *engine_argv,
                    cwd=str(self._run_directory), env=child_environment(), stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, limit=65536,
                    pass_fds=(self._lease_fd,)))
                self._child = await asyncio.shield(self._spawn_task)
                self._spawn_task = None
                self._drains.append(asyncio.create_task(self._drain(self._child.stderr)))
                ack = await bounded_wait(self._child.stdout.readline(), 5)
                if ack != f"{LIMIT_ACK}{limit}\n".encode("ascii"):
                    raise ValueError("native write limit not confirmed")
                self.native_write_limit = True
                self._drains.append(asyncio.create_task(self._drain(self._child.stdout)))
                self.epoch = new_engine_epoch()
            except BaseException as error:
                await asyncio.shield(self._stop_locked())
                self.state = "unavailable"
                if isinstance(error, (LocalInferenceError, asyncio.CancelledError)):
                    raise
                raise LocalInferenceError("backend_unavailable", "The owned engine could not be started.") from None

    async def _stop_locked(self) -> bool:
        self.native_write_limit = False
        if self._spawn_task is not None:
            try:
                self._child = await bounded_wait(asyncio.shield(self._spawn_task), 0.2)
                self._spawn_task = None
            except asyncio.TimeoutError:
                # Keep the handle to this uncertain spawn; subsequent starts are
                # refused until fencing can acquire and reap that exact child.
                self.state = "unavailable"
                pending = self._spawn_task
                if self._late_spawn_cleanup is not pending:
                    self._late_spawn_cleanup = pending
                    def clean_late_spawn(completed):
                        if completed.cancelled():
                            return
                        try:
                            child = completed.result()
                        except Exception:
                            child = None
                        # Signal this exact owned child immediately, even if its
                        # async spawn completed after close returned unconfirmed.
                        if child is not None and child.returncode is None:
                            try:
                                child.kill()
                            except ProcessLookupError:
                                pass
                        async def reap():
                            async with self._lock:
                                if self._spawn_task is completed:
                                    await self._stop_locked()
                        task = asyncio.create_task(reap(), name="local-native-late-reap")
                        task.add_done_callback(lambda value: None if value.cancelled() else value.exception())
                    pending.add_done_callback(clean_late_spawn)
                return False
            except Exception:
                self._spawn_task = None
        child = self._child
        if child is not None:
            grace = self.config.shutdown_grace_seconds
            try:
                if child.returncode is None:
                    try:
                        child.terminate()
                    except ProcessLookupError:
                        pass
                    try:
                        await bounded_wait(child.wait(), grace * 0.6)
                    except asyncio.TimeoutError:
                        try:
                            child.kill()
                        except ProcessLookupError:
                            pass
                        await bounded_wait(child.wait(), grace * 0.3)
                else:
                    await bounded_wait(child.wait(), grace * 0.3)
            except (OSError, asyncio.TimeoutError):
                self.state = "unavailable"
                return False
        self._child = None
        for task in self._drains:
            task.cancel()
        if self._drains:
            await asyncio.wait(self._drains, timeout=0.05)
        self._drains.clear()
        try:
            if self._run_directory is not None:
                try:
                    run_info = self._run_directory.lstat()
                except FileNotFoundError:
                    pass
                else:
                    if (stat.S_ISDIR(run_info.st_mode)
                            and (run_info.st_dev, run_info.st_ino) == self._run_identity):
                        shutil.rmtree(self._run_directory)
            if self._socket_directory is not None:
                try:
                    socket_info = self._socket_directory.lstat()
                except FileNotFoundError:
                    pass
                else:
                    if (stat.S_ISDIR(socket_info.st_mode)
                            and (socket_info.st_dev, socket_info.st_ino) == self._socket_identity):
                        shutil.rmtree(self._socket_directory)
        finally:
            if self._lease_fd is not None:
                os.close(self._lease_fd)
                self._lease_fd = None
        self._run_directory = self._run_identity = self.spool = self.api_key = None
        self._socket_directory = self._socket_identity = None
        self.epoch = new_engine_epoch()
        self.state = "not_started"
        return True

    async def fence(self) -> bool:
        async with self._lock:
            self.state = "restarting"
            return await self._stop_locked()

    async def close(self) -> bool:
        self._closed = True
        if self._hash_stop is not None:
            self._hash_stop.set()
        confirmed = await self.fence()
        if self._files_executor is not None:
            self._files_executor.shutdown(wait=False, cancel_futures=True)
            self._files_executor = None
        self.state = "stopping" if confirmed else "unavailable"
        return confirmed
