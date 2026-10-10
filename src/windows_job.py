"""Child processes that end with the process that started them (Windows job objects).

A ``LockFileEx`` lock belongs to the process that took it: a child that inherits the handle
does not hold it, and the lock ends when that process exits. On POSIX an updater's child keeps
an inherited ``flock`` lease after its parent dies; on Windows the child runs in a job object
that the system ends when the last handle to it closes, at the latest when the process holding
the lease exits. A process starts suspended and runs only once it is in the job, so none of its
descendants escapes. `replace_process` uses such a job in place of exec, which Windows lacks.
Standard library only.
"""
import ctypes
from ctypes import wintypes
import os
import subprocess
import sys
import time

if os.name == "nt":
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _ntdll = ctypes.WinDLL("ntdll")
    _kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    _kernel32.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                                    ctypes.POINTER(wintypes.DWORD)]
    _kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    # psutil resumes a process the same way; the documented alternative resumes each thread.
    _ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
    _ntdll.NtResumeProcess.restype = ctypes.c_long

CREATE_SUSPENDED = 0x00000004
_PROCESS_TERMINATE, _PROCESS_SET_QUOTA, _PROCESS_SUSPEND_RESUME = 0x0001, 0x0100, 0x0800
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_BASIC_ACCOUNTING, _EXTENDED_LIMIT = 1, 9
_POLL = 0.05


class _IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class _BasicLimit(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD)]


class _ExtendedLimit(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _BasicLimit), ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


class _Accounting(ctypes.Structure):
    _fields_ = [("TotalUserTime", ctypes.c_longlong), ("TotalKernelTime", ctypes.c_longlong),
                ("ThisPeriodTotalUserTime", ctypes.c_longlong), ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
                ("TotalPageFaultCount", wintypes.DWORD), ("TotalProcesses", wintypes.DWORD),
                ("ActiveProcesses", wintypes.DWORD), ("TotalTerminatedProcesses", wintypes.DWORD)]


def _check(succeeded):
    if not succeeded:
        raise ctypes.WinError(ctypes.get_last_error())
    return succeeded


class Job:
    """A job object whose processes end when it closes: by ``close``, or when this process exits."""

    def __init__(self):
        self.handle = _check(_kernel32.CreateJobObjectW(None, None))
        limits = _ExtendedLimit()
        limits.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        try:
            _check(_kernel32.SetInformationJobObject(self.handle, _EXTENDED_LIMIT, ctypes.byref(limits),
                                                     ctypes.sizeof(limits)))
        except BaseException:
            self.close()
            raise

    def start(self, args, **options) -> subprocess.Popen:
        """``subprocess.Popen(args, **options)`` with the process and its descendants in this job.

        Raises ``OSError`` without leaving the process running when it cannot join the job.
        """
        options["creationflags"] = options.get("creationflags", 0) | CREATE_SUSPENDED
        process = subprocess.Popen(args, **options)
        try:
            handle = _check(_kernel32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE | _PROCESS_SUSPEND_RESUME,
                                                  False, process.pid))
            try:
                _check(_kernel32.AssignProcessToJobObject(self.handle, handle))
                status = _ntdll.NtResumeProcess(handle)
                if status < 0:
                    raise OSError(f"NtResumeProcess failed: NTSTATUS 0x{status & 0xFFFFFFFF:08X}")
            finally:
                _kernel32.CloseHandle(handle)
        except BaseException:
            process.kill()
            process.wait()
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream:
                    stream.close()
            raise
        return process

    def active(self) -> int:
        """How many processes of the job are still running."""
        accounting = _Accounting()
        _check(_kernel32.QueryInformationJobObject(self.handle, _BASIC_ACCOUNTING, ctypes.byref(accounting),
                                                   ctypes.sizeof(accounting), None))
        return accounting.ActiveProcesses

    def wait_empty(self, timeout) -> bool:
        """Whether every process of the job ended within ``timeout`` seconds."""
        deadline = time.monotonic() + timeout
        while self.active():
            if time.monotonic() >= deadline:
                return False
            time.sleep(_POLL)
        return True

    def terminate(self, timeout=30) -> bool:
        """End every process of the job; whether they all ended within ``timeout`` seconds."""
        _kernel32.TerminateJobObject(self.handle, 1)
        return self.wait_empty(timeout)

    def close(self) -> None:
        if self.handle:
            handle, self.handle = self.handle, None
            _kernel32.CloseHandle(handle)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()


def replace_process(argv) -> None:
    """The closest Windows has to exec: let *argv* serve this process's standard handles.

    ``os.execv`` there starts a new process and ends this one, so a client waiting on this
    process would see it exit. Instead *argv* runs as a child on the same standard handles, in a
    job that ends it with this process, and this process exits with its exit code. Never
    returns; raises OSError when the child cannot start.
    """
    for stream in (sys.stdout, sys.stderr):
        if stream is not None:
            stream.flush()
    job = Job()
    stdin, stdout, stderr = (fd if _is_open(fd) else subprocess.DEVNULL for fd in (0, 1, 2))
    try:
        process = job.start(argv, stdin=stdin, stdout=stdout, stderr=stderr, creationflags=subprocess.CREATE_NO_WINDOW)
    except BaseException:
        job.close()  # a caller that serves on by itself keeps no empty job
        raise
    while True:
        try:
            # An untimed wait cannot be interrupted: Ctrl+C in a console ends this
            # process between the timed waits, and with it the job and the child.
            code = process.wait(timeout=1)
            break
        except subprocess.TimeoutExpired:
            continue
    # A Windows exit code is unsigned; os._exit takes a C int.
    os._exit(code - 2**32 if code >= 2**31 else code)


def _is_open(fd: int) -> bool:
    """Whether this process has descriptor *fd*; a client may start a server without stderr."""
    try:
        os.fstat(fd)
    except OSError:
        return False
    return True
