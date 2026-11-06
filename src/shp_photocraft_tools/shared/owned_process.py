"""Bounded escalation of pinned members of a session created by Tools."""

import os
from pathlib import Path
import select
import signal
import time


def process_identity(pid):
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    return int(fields[3]), int(fields[19])  # session ID and kernel start ticks


def require_session_stopped(process):
    if process is None:
        return
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            try:
                if process_identity(int(entry.name))[0] == process.pid:
                    raise RuntimeError("Owned session still has processes; refusing replacement")
            except (FileNotFoundError, ProcessLookupError):
                continue


def pin_session(process, expected):
    if process is None:
        return []
    if process.poll() is not None:
        require_session_stopped(process)
        return []
    if expected is None or process_identity(process.pid) != expected or expected[0] != process.pid:
        raise RuntimeError("Owned process identity changed; refusing forced shutdown")
    handles = []
    try:
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            try:
                before = process_identity(pid)
                if before[0] != process.pid:
                    continue
                fd = os.pidfd_open(pid)
                try:
                    if process_identity(pid) != before:
                        raise RuntimeError("Process changed while pinning; refusing forced shutdown")
                except BaseException:
                    os.close(fd)
                    raise
                handles.append(fd)
            except ProcessLookupError:
                continue
            except FileNotFoundError:
                continue
        if process_identity(process.pid) != expected:
            raise RuntimeError("Owned session leader changed; refusing forced shutdown")
        return handles
    except BaseException:
        for fd in handles:
            os.close(fd)
        raise


def drain_pinned(handles, graceful, grace=3, kill_wait=2):
    """PID descriptors cannot signal a reused PID, even after the leader exits."""
    def pending():
        return [fd for fd in handles if not select.select([fd], [], [], 0)[0]]

    def wait(seconds):
        deadline = time.monotonic() + seconds
        while pending() and time.monotonic() < deadline:
            time.sleep(.05)

    try:
        graceful()
        wait(grace)
        for fd in pending():
            try:
                signal.pidfd_send_signal(fd, signal.SIGTERM)
            except ProcessLookupError:
                pass
        wait(grace)
        for fd in pending():
            try:
                signal.pidfd_send_signal(fd, signal.SIGKILL)
            except ProcessLookupError:
                pass
        wait(kill_wait)
        if pending():
            raise RuntimeError("Owned processes did not exit after bounded escalation")
    finally:
        for fd in handles:
            os.close(fd)
