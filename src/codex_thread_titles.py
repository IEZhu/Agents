"""Persist Codex titles through the public, metadata-only App Server API.

This file is also installed as a standalone sibling of the hook runtime. It
uses no client storage formats and never starts or resumes a model turn.
"""
from __future__ import annotations

import json
import math
import os
import queue
import signal
import subprocess
import threading
import time
import uuid
from typing import Callable


_MAX_MESSAGE_BYTES = 65536
_MAX_TITLE_CHARS = 1024


def _remaining(deadline: float) -> float:
    return max(0.0, deadline - time.monotonic())


def _request(process, request_id: int, method: str, params: dict, deadline: float) -> dict:
    if not _remaining(deadline):
        raise RuntimeError("Codex title update timed out")
    message = {"id": request_id, "method": method, "params": params}
    process.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
    process.stdin.flush()
    while _remaining(deadline):
        line = process.stdout.readline(_MAX_MESSAGE_BYTES + 1)
        if not line:
            raise RuntimeError("Codex App Server closed before answering " + method)
        if len(line) > _MAX_MESSAGE_BYTES or not line.endswith(b"\n"):
            raise RuntimeError("Codex App Server returned an oversized or incomplete message")
        try:
            response = json.loads(line)
        except (ValueError, UnicodeError) as error:
            raise RuntimeError("Codex App Server returned invalid JSON") from error
        if not isinstance(response, dict):
            raise RuntimeError("Codex App Server returned an invalid response")
        if "id" not in response and isinstance(response.get("method"), str):
            continue  # Notifications carry no approval or execution authority.
        if type(response.get("id")) is not int or response["id"] != request_id:
            raise RuntimeError("Codex App Server returned an unexpected response identity")
        if "error" in response:
            error = response["error"]
            code = error.get("code") if isinstance(error, dict) else None
            raise RuntimeError(f"Codex App Server {method} failed (code {code})")
        if not isinstance(response.get("result"), dict):
            raise RuntimeError("Codex App Server returned no result for " + method)
        return response["result"]
    raise RuntimeError("Codex title update timed out")


def _thread_name(result: dict, session_id: str) -> str:
    thread = result.get("thread")
    if not isinstance(thread, dict) or thread.get("id") != session_id:
        raise RuntimeError("Codex thread/read did not confirm the requested thread identity")
    name = thread.get("name")
    if not isinstance(name, str) or not name.strip():
        raise RuntimeError("Codex thread/read returned no known nonempty thread.name")
    return name


def _exchange(process, session_id: str, status: str, formatter, deadline: float) -> dict:
    _request(process, 0, "initialize", {
        "clientInfo": {"name": "agents_core_thread_titles", "version": "1.0"},
    }, deadline)
    process.stdin.write(b'{"method":"initialized","params":{}}\n')
    process.stdin.flush()
    params = {"threadId": session_id, "includeTurns": False}
    current = _thread_name(_request(process, 1, "thread/read", params, deadline), session_id)
    title = formatter(current, status)
    if (not isinstance(title, str) or not title.strip() or len(title) > _MAX_TITLE_CHARS
            or any(ord(char) < 32 or ord(char) == 127 for char in title)):
        raise ValueError("The formatted Codex title must be one bounded nonempty line")
    changed = title != current
    if changed:
        _request(process, 2, "thread/name/set", {"threadId": session_id, "name": title}, deadline)
    check = _thread_name(_request(process, 3, "thread/read", params, deadline), session_id)
    if check != title:
        raise RuntimeError("Codex title readback did not confirm the requested title")
    return {"client": "codex", "session_id": session_id, "status": status,
            "title": title, "changed": changed, "persisted": True,
            "live_ui_refresh_verified": False}


def _stop(process, deadline: float, *, completed: bool) -> None:
    """Close stdio, then terminate/kill within the same overall deadline."""
    if completed:
        process.stdin.close()
        try:
            process.wait(timeout=min(0.03, _remaining(deadline)))
        except subprocess.TimeoutExpired:
            pass
    if os.name == "posix":
        # The direct process may exit while its local helper still owns a pipe.
        # The isolated group must therefore be cleaned even after wait succeeds.
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(process.pid, sig)
            except ProcessLookupError:
                pass
            if sig == signal.SIGTERM:
                try:
                    process.wait(timeout=min(0.03, _remaining(deadline)))
                except subprocess.TimeoutExpired:
                    pass
        try:
            process.wait(timeout=_remaining(deadline))
        except subprocess.TimeoutExpired as error:
            raise RuntimeError("Codex App Server did not exit within the cleanup deadline") from error
        return
    if process.poll() is None:
        try:
            process.terminate()
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=min(0.03, _remaining(deadline)))
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=_remaining(deadline))
            except subprocess.TimeoutExpired as error:
                raise RuntimeError("Codex App Server did not exit within the cleanup deadline") from error


def rename_codex_thread(session_id: str, status: str, formatter: Callable[[str, str], str], *,
                        timeout: float = 2.5, executable: str = "codex") -> dict:
    """Read, format, persist and confirm the current thread title via stdio.

    The total deadline starts before process creation and includes initialization
    and cleanup. Part of it is reserved for closing/terminating the child. No
    success result establishes refresh in a separate, already-open desktop UI.
    """
    started = time.monotonic()
    try:
        canonical = str(uuid.UUID(session_id))
    except (ValueError, AttributeError, TypeError) as error:
        raise ValueError("A canonical UUID of the current Codex thread is required") from error
    if canonical != session_id:
        raise ValueError("A canonical UUID of the current Codex thread is required")
    if not isinstance(status, str) or not status or not callable(formatter):
        raise ValueError("A status and title formatter are required")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("The Codex title timeout must be finite and positive")
    if not isinstance(executable, str) or not executable:
        raise ValueError("A Codex executable is required")
    deadline = started + timeout
    exchange_deadline = deadline - min(0.2, timeout / 3)
    options = {"start_new_session": True} if os.name == "posix" else {
        "creationflags": subprocess.CREATE_NO_WINDOW,
    }
    process = subprocess.Popen([executable, "app-server", "--listen", "stdio://"],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, bufsize=0, close_fds=True, **options)
    outcome = queue.Queue(maxsize=1)

    def exchange() -> None:
        try:
            outcome.put((True, _exchange(process, session_id, status, formatter, exchange_deadline)))
        except Exception as error:
            outcome.put((False, error))

    worker = threading.Thread(target=exchange, name="codex-title-stdio", daemon=True)
    completed = False
    try:
        worker.start()
        try:
            successful, value = outcome.get(timeout=_remaining(exchange_deadline))
            completed = True
        except queue.Empty as error:
            raise RuntimeError("Codex title update timed out") from error
        if not successful:
            raise value
        return value
    finally:
        try:
            _stop(process, deadline, completed=completed)
        finally:
            worker.join(timeout=_remaining(deadline))
            process.stdin.close()
            process.stdout.close()
