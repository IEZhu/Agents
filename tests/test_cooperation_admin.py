"""Offline cleanup's process check never signals a Windows execution."""
import ctypes
from ctypes import wintypes
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.cooperation import admin


@pytest.fixture
def windows_probe(monkeypatch):
    kernel32 = SimpleNamespace(
        OpenProcess=Mock(return_value=1 << 40),
        WaitForSingleObject=Mock(return_value=258),  # WAIT_TIMEOUT: still live.
        CloseHandle=Mock(return_value=1),
    )
    loader = Mock(return_value=kernel32)
    last_error = Mock(return_value=0)
    # Replace only admin's reference: changing os.name globally breaks pathlib
    # and would prevent these boundary tests from running on POSIX hosts.
    monkeypatch.setattr(admin, "os", SimpleNamespace(name="nt", kill=Mock(
        side_effect=AssertionError("Windows status probes must not signal a process"))))
    monkeypatch.setattr(ctypes, "WinDLL", loader, raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", last_error, raising=False)
    return kernel32, loader, last_error


@pytest.mark.parametrize("error, stopped", [(87, True), (5, False), (6, False), (0, False)])
def test_windows_open_process_absent_inaccessible_or_unknown(windows_probe, error, stopped):
    kernel32, loader, last_error = windows_probe
    kernel32.OpenProcess.return_value = None
    last_error.return_value = error

    assert admin.execution_stopped(1234) is stopped

    loader.assert_called_once_with("kernel32", use_last_error=True)
    kernel32.OpenProcess.assert_called_once_with(0x00100000, False, 1234)
    kernel32.WaitForSingleObject.assert_not_called()
    kernel32.CloseHandle.assert_not_called()
    admin.os.kill.assert_not_called()


@pytest.mark.parametrize("wait_result, stopped", [
    (0, True),  # Signaled process handle: execution exited.
    (258, False),  # WAIT_TIMEOUT: live or reused PID.
    (0xFFFFFFFF, False),  # WAIT_FAILED.
    (128, False),  # Any unexpected state fails closed.
])
def test_windows_process_state_and_handle_cleanup(windows_probe, wait_result, stopped):
    kernel32, _, last_error = windows_probe
    kernel32.WaitForSingleObject.return_value = wait_result

    assert admin.execution_stopped(1234) is stopped

    handle = kernel32.OpenProcess.return_value
    kernel32.WaitForSingleObject.assert_called_once_with(handle, 0)
    kernel32.CloseHandle.assert_called_once_with(handle)
    assert kernel32.OpenProcess.restype is wintypes.HANDLE
    assert kernel32.WaitForSingleObject.argtypes == [wintypes.HANDLE, wintypes.DWORD]
    assert kernel32.CloseHandle.argtypes == [wintypes.HANDLE]
    last_error.assert_not_called()
    admin.os.kill.assert_not_called()


@pytest.mark.parametrize("pid", [True, False, None, "1234", 1.5, 0, -1, 1 << 32, (1 << 32) + 1234])
def test_windows_invalid_pid_does_not_query_or_signal(windows_probe, pid):
    _, loader, _ = windows_probe

    assert admin.execution_stopped(pid) is False

    loader.assert_not_called()
    admin.os.kill.assert_not_called()


@pytest.mark.parametrize("error", [OSError("load failed"), AttributeError("API unavailable")])
def test_windows_unavailable_query_fails_closed(windows_probe, error):
    _, loader, _ = windows_probe
    loader.side_effect = error

    assert admin.execution_stopped(1234) is False
    admin.os.kill.assert_not_called()


def test_windows_query_exception_still_closes_handle(windows_probe):
    kernel32, _, _ = windows_probe
    kernel32.WaitForSingleObject.side_effect = OSError("query failed")

    assert admin.execution_stopped(1234) is False

    kernel32.CloseHandle.assert_called_once_with(kernel32.OpenProcess.return_value)
    admin.os.kill.assert_not_called()


def test_windows_handle_close_failure_fails_closed(windows_probe):
    kernel32, _, _ = windows_probe
    kernel32.WaitForSingleObject.return_value = 0
    kernel32.CloseHandle.return_value = 0

    assert admin.execution_stopped(1234) is False
    admin.os.kill.assert_not_called()


@pytest.mark.parametrize("error, stopped", [
    (None, False),
    (ProcessLookupError(), True),
    (PermissionError(), False),
    (OSError(), False),
])
def test_posix_probe_preserves_existing_status_behavior(monkeypatch, error, stopped):
    kill = Mock(side_effect=error)
    monkeypatch.setattr(admin, "os", SimpleNamespace(name="posix", kill=kill))

    assert admin.execution_stopped(1234) is stopped

    kill.assert_called_once_with(1234, 0)
