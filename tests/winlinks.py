"""Link helpers so symlink-based tests run on Windows without the symlink privilege."""
import os
import subprocess
import sys
import unittest


def _no_privilege(error):
    return sys.platform == "win32" and getattr(error, "winerror", None) == 1314


def file_symlink(link, target):
    """A real symlink when permitted; otherwise a hard link, which still makes the name an alias of the
    target so any write through it would be visible in the target."""
    try:
        link.symlink_to(target)
    except OSError as error:
        if not _no_privilege(error):
            raise
        os.link(target, link)


def directory_alias(link, target):
    """A symlink when permitted, else an NTFS junction (also a reparse point)."""
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError as error:
        if not _no_privilege(error):
            raise
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True)


def require_file_symlink(test, directory):
    """Skip when the real file symlink semantics are needed but the privilege is missing."""
    probe = directory / ".symlink-probe"
    target = directory / ".symlink-probe-target"
    target.write_bytes(b"")
    try:
        probe.symlink_to(target)
    except OSError as error:
        if _no_privilege(error):
            raise unittest.SkipTest("file symlinks need Developer Mode or elevation on this Windows account")
        raise
    finally:
        for path in (probe, target):
            try:
                path.unlink()
            except OSError:
                pass
