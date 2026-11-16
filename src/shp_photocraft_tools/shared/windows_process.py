"""Windows process identity and Job Object ownership for services the supervisor spawned.

Identity is (pid, creation FILETIME): a recycled pid always has a different creation time.
Every spawned service gets its own Job Object, and only members of that job are ever terminated.
"""

import ctypes
import subprocess
import sys
import threading
import time
from ctypes import wintypes

if sys.platform != "win32":
    raise ImportError("windows_process is only available on Windows")

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_SET_QUOTA = 0x0100
PROCESS_TERMINATE = 0x0001
SYNCHRONIZE = 0x00100000
STILL_ACTIVE = 259
JobObjectBasicAccountingInformation = 1
JobObjectBasicProcessIdList = 3
PROCESS_SUSPEND_RESUME = 0x0800
CREATE_SUSPENDED = 0x00000004
ERROR_INVALID_PARAMETER = 87

CREATE_NEW_PROCESS_GROUP = subprocess.CREATE_NEW_PROCESS_GROUP
CREATE_NO_WINDOW = subprocess.CREATE_NO_WINDOW
DETACHED_PROCESS = subprocess.DETACHED_PROCESS

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_ntdll = ctypes.WinDLL("ntdll")
_ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
_k32.OpenProcess.restype = wintypes.HANDLE
_k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_k32.CloseHandle.argtypes = [wintypes.HANDLE]
_k32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
_k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
_k32.CreateJobObjectW.restype = wintypes.HANDLE
_k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
_k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
_k32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
_k32.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                           ctypes.POINTER(wintypes.DWORD)]
_k32.CreateMutexW.restype = wintypes.HANDLE
_k32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
_k32.CreateEventW.restype = wintypes.HANDLE
_k32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
_k32.SetEvent.argtypes = [wintypes.HANDLE]
_k32.OpenEventW.restype = wintypes.HANDLE
_k32.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
_k32.WaitForSingleObject.restype = wintypes.DWORD
_k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]

ERROR_ALREADY_EXISTS = 183
EVENT_MODIFY_STATE = 0x0002


class _BasicAccounting(ctypes.Structure):
    _fields_ = [("TotalUserTime", ctypes.c_int64), ("TotalKernelTime", ctypes.c_int64),
                ("ThisPeriodTotalUserTime", ctypes.c_int64), ("ThisPeriodTotalKernelTime", ctypes.c_int64),
                ("TotalPageFaultCount", wintypes.DWORD), ("TotalProcesses", wintypes.DWORD),
                ("ActiveProcesses", wintypes.DWORD), ("TotalTerminatedProcesses", wintypes.DWORD)]


def _filetime(value):
    return (value.dwHighDateTime << 32) | value.dwLowDateTime


def process_identity(pid):
    """(pid, creation time). Raises ProcessLookupError when the process does not exist or has exited."""
    handle = _k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        if ctypes.get_last_error() == ERROR_INVALID_PARAMETER:
            raise ProcessLookupError(pid)
        raise OSError(ctypes.get_last_error(), f"Cannot open process {pid}")
    try:
        code = wintypes.DWORD()
        if not _k32.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != STILL_ACTIVE:
            raise ProcessLookupError(pid)
        created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
        if not _k32.GetProcessTimes(handle, *(ctypes.byref(v) for v in (created, exited, kernel, user))):
            raise OSError(ctypes.get_last_error(), f"Cannot read process {pid} times")
        return pid, _filetime(created)
    finally:
        _k32.CloseHandle(handle)


def is_alive(pid, created):
    try:
        return process_identity(pid)[1] == created
    except ProcessLookupError:
        return False


class Job:
    """A Job Object holding one spawned service (and any children it creates after assignment)."""

    def __init__(self, process, identity):
        self.pid, self.identity = process.pid, identity
        self.handle = _k32.CreateJobObjectW(None, None)
        if not self.handle:
            raise OSError(ctypes.get_last_error(), "CreateJobObject failed")
        try:
            handle = _k32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, process.pid)
            if not handle:
                raise OSError(ctypes.get_last_error(), "Cannot open spawned service for job assignment")
            try:
                if not _k32.AssignProcessToJobObject(self.handle, handle):
                    raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject failed")
            finally:
                _k32.CloseHandle(handle)
        except BaseException:
            self.close()
            raise

    def active(self):
        info = _BasicAccounting()
        if not _k32.QueryInformationJobObject(self.handle, JobObjectBasicAccountingInformation,
                                              ctypes.byref(info), ctypes.sizeof(info), None):
            raise OSError(ctypes.get_last_error(), "QueryInformationJobObject failed")
        return info.ActiveProcesses

    def pids(self):
        class ProcessIdList(ctypes.Structure):
            _fields_ = [("NumberOfAssignedProcesses", wintypes.DWORD), ("NumberOfProcessIdsInList", wintypes.DWORD),
                        ("ProcessIdList", ctypes.c_size_t * 256)]
        info = ProcessIdList()
        if not _k32.QueryInformationJobObject(self.handle, JobObjectBasicProcessIdList,
                                              ctypes.byref(info), ctypes.sizeof(info), None):
            raise OSError(ctypes.get_last_error(), "QueryInformationJobObject failed")
        return set(info.ProcessIdList[:info.NumberOfProcessIdsInList])

    def terminate(self):
        if not _k32.TerminateJobObject(self.handle, 1):
            raise OSError(ctypes.get_last_error(), "TerminateJobObject failed")

    def close(self):
        if self.handle:
            _k32.CloseHandle(self.handle)
            self.handle = None


_jobs = {}
_jobs_lock = threading.Lock()


def adopt(process):
    """Put a process created with spawn_options() in its own job, then resume it; returns its identity.

    The process starts suspended, so nothing it spawns (including the venv launcher's real
    interpreter child) can exist before it is in the job.
    """
    try:
        identity = process_identity(process.pid)
        job = Job(process, identity)
        with _jobs_lock:
            _jobs[process.pid] = job
        handle = _k32.OpenProcess(PROCESS_SUSPEND_RESUME, False, process.pid)
        if not handle:
            raise OSError(ctypes.get_last_error(), "Cannot open spawned service for resume")
        try:
            if _ntdll.NtResumeProcess(handle) != 0:
                raise OSError("NtResumeProcess failed")
        finally:
            _k32.CloseHandle(handle)
    except BaseException:
        process.kill()
        raise
    return identity


def owns(process, pid):
    """True when `pid` is the spawned process or a member of its job (the venv launcher adds a child)."""
    if pid == process.pid:
        return True
    job = _job_for(process)
    return bool(job and job.handle and pid in job.pids())


def _job_for(process):
    with _jobs_lock:
        return _jobs.get(process.pid)


def require_session_stopped(process, wait=10):
    if process is None:
        return
    job = _job_for(process)
    if job is None or not job.handle:
        return
    deadline = time.monotonic() + wait
    while job.active() and time.monotonic() < deadline:
        time.sleep(.1)  # the venv launcher and its interpreter exit a moment apart
    if job.active():
        raise RuntimeError("Owned job still has processes; refusing replacement")


def pin_session(process, expected):
    """Verify the spawned service is still the one we started and return its job as the only handle."""
    if process is None:
        return []
    job = _job_for(process)
    if process.poll() is not None:
        require_session_stopped(process)
        return []
    if expected is None or job is None or job.identity != expected or process_identity(process.pid) != expected:
        raise RuntimeError("Owned process identity changed; refusing forced shutdown")
    return [job]


def drain_pinned(handles, graceful, grace=3, kill_wait=2):
    def settled(job):
        return job.active() == 0

    def wait(seconds):
        deadline = time.monotonic() + seconds
        while not all(settled(job) for job in handles) and time.monotonic() < deadline:
            time.sleep(.05)

    graceful()
    wait(grace)
    for job in handles:
        if not settled(job):
            job.terminate()
    wait(kill_wait)
    if not all(settled(job) for job in handles):
        raise RuntimeError("Owned processes did not exit after bounded escalation")


def close_handle(job):
    job.close()


def spawn_options():
    """Creation flags for a service that must be passed to adopt() before it runs."""
    return {"creationflags": CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW | CREATE_SUSPENDED}


def detached_options():
    return {"creationflags": CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS | CREATE_NO_WINDOW}


def background_python(executable=None):
    """pythonw.exe next to the interpreter, or the interpreter itself.

    The venv python.exe is a console-subsystem redirector whose real child gets its own console (a visible
    Windows Terminal window when the parent has none, regardless of CREATE_NO_WINDOW on the redirector).
    pythonw.exe is GUI-subsystem, so neither it nor its child ever gets a console."""
    import os
    executable = executable or sys.executable
    candidate = os.path.join(os.path.dirname(executable), "pythonw.exe")
    return candidate if os.path.basename(executable).lower() == "python.exe" and os.path.exists(candidate) else executable


class SingleInstance:
    """Named mutex plus a named event used to ask the first instance to show its window."""

    def __init__(self, name):
        self.mutex = _k32.CreateMutexW(None, False, f"Local\\{name}")
        self.primary = bool(self.mutex) and ctypes.get_last_error() != ERROR_ALREADY_EXISTS
        self.event_name = f"Local\\{name}.show"
        self.event = _k32.CreateEventW(None, False, False, self.event_name) if self.primary else None

    def signal_primary(self):
        handle = _k32.OpenEventW(EVENT_MODIFY_STATE, False, self.event_name)
        if not handle:
            return False
        try:
            return bool(_k32.SetEvent(handle))
        finally:
            _k32.CloseHandle(handle)

    def wait_for_show(self, milliseconds=500):
        return _k32.WaitForSingleObject(self.event, milliseconds) == 0
