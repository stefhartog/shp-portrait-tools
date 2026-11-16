"""Exclusive advisory file lock: flock on POSIX, msvcrt byte-range lock on Windows."""

import contextlib
import sys
import time

if sys.platform == "win32":
    import msvcrt

    def _acquire(handle, timeout):
        deadline = time.monotonic() + timeout
        handle.seek(0)
        while True:
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Timed out waiting for another Tools launcher to finish") from None
                time.sleep(0.05)

    def _release(handle):
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _acquire(handle, timeout):
        fcntl.flock(handle, fcntl.LOCK_EX)

    def _release(handle):
        fcntl.flock(handle, fcntl.LOCK_UN)


@contextlib.contextmanager
def file_lock(path, timeout=60, acquire=_acquire, release=_release):
    """Hold an exclusive lock on `path` (created if needed); the OS drops it if the process dies."""
    with open(path, "a+b") as handle:
        acquire(handle, timeout)
        try:
            yield
        finally:
            release(handle)
