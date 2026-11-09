"""Stable, direct PNG inbox imports driven by the connected UI, without worker threads."""

import os
from copy import deepcopy
from pathlib import Path
import stat
import struct
import tempfile
import threading
import time
import uuid
import zlib


class ChangedFile(ValueError):
    pass


def capture_target(pc):
    before = pc.active_doc()
    if before is None:
        raise ValueError("open the exported image's document in PhotoCraft before importing")
    inspected = pc.client.run("document.inspect")
    after = pc.active_doc()
    if after is None or any(before.get(key) != after.get(key) for key in ("index", "name", "path")):
        raise ValueError("PhotoCraft document changed while checking the watch-folder target")
    if type(inspected.get("revision")) is not int:
        raise ValueError("PhotoCraft cannot report the document identity/revision needed for safe watch-folder association")
    if inspected.get("name") != before.get("name") or inspected.get("path") != before.get("path"):
        raise ValueError("PhotoCraft document changed while inspecting the watch-folder target")
    identity = {key: inspected.get(key) for key in ("revision", "width", "height")}
    identity["id"] = inspected.get("id") or {
        key: before.get(key) for key in ("index", "name", "path")}
    return {"document": before, "identity": identity}


def version(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def verify_png(path):
    """Reject partial PNGs even when an image decoder tolerates truncation."""
    with path.open("rb") as stream:
        if stream.read(8) != b"\x89PNG\r\n\x1a\n":
            raise ValueError("watch-folder input is not a PNG")
        first = True
        image_data = False
        while True:
            header = stream.read(8)
            if len(header) != 8:
                raise ValueError("PNG export is incomplete (missing chunk or IEND)")
            size, kind = struct.unpack(">I4s", header)
            if first and (kind != b"IHDR" or size != 13):
                raise ValueError("PNG has no valid image header")
            first = False
            checksum = zlib.crc32(kind)
            remaining = size
            while remaining:
                block = stream.read(min(remaining, 1024 * 1024))
                if not block:
                    raise ValueError("PNG export is incomplete (truncated chunk)")
                checksum = zlib.crc32(block, checksum)
                remaining -= len(block)
            crc = stream.read(4)
            if len(crc) != 4 or struct.unpack(">I", crc)[0] != checksum:
                raise ValueError("PNG export is incomplete or corrupt (chunk checksum)")
            image_data |= kind == b"IDAT"
            if kind == b"IEND":
                if size or not image_data or stream.read(1):
                    raise ValueError("PNG export has an invalid ending")
                return


class WatchFolder:
    def __init__(self, directory, storage, *, clock=time.monotonic, stable_seconds=2):
        self.directory, self.storage = Path(directory), Path(storage)
        self.clock, self.stable_seconds = clock, stable_seconds
        self.lock = threading.Lock()
        self.seen = {}
        self.identity = None
        self.closed = False

    def close(self):
        with self.lock:
            self.closed = True

    def _open_directory(self):
        fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            identity = info.st_dev, info.st_ino
            if self.identity is not None and self.identity != identity:
                raise ValueError("watch folder changed; restart Tools before importing from it")
            self.identity = identity
            return fd
        except BaseException:
            os.close(fd)
            raise

    def _check_directory(self):
        current = self.directory.stat(follow_symlinks=False)
        if (current.st_dev, current.st_ino) != self.identity:
            raise ChangedFile("watch folder changed; inbox file was retained")

    def status(self):
        with self.lock:
            if self.closed:
                return {"ready": False}
            try:
                directory = self._open_directory()
            except FileNotFoundError:
                return {"ready": False, "error": f"Watch folder does not exist: {self.directory}"}
            except (OSError, ValueError) as error:
                return {"ready": False, "error": str(error)}
            try:
                names = sorted(name for name in os.listdir(directory) if Path(name).suffix.lower() == ".png")
                self.seen = {name: record for name, record in self.seen.items() if name in names}
                errors = []
                ready = None
                for name in names:
                    try:
                        info = os.stat(name, dir_fd=directory, follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    current = version(info)
                    record = self.seen.get(name)
                    if record is None or record["version"] != current:
                        record = {"version": current, "since": self.clock(), "token": uuid.uuid4().hex}
                        self.seen[name] = record
                    if not stat.S_ISREG(info.st_mode):
                        record["error"] = "only direct regular PNG files are accepted (no symlinks)"
                    if record.get("error"):
                        errors.append(f"{name}: {record['error']}")
                    if not record.get("done") and (not record.get("error") or record.get("retryable")) and ready is None and (
                            info.st_size > 0 and self.clock() - record["since"] >= self.stable_seconds):
                        if not record.get("validated"):
                            fd = None
                            try:
                                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                                if version(os.fstat(fd)) != current:
                                    continue
                                verify_png(Path(f"/proc/self/fd/{fd}"))
                                if version(os.fstat(fd)) != current:
                                    continue
                                record["validated"] = True
                            except (OSError, ValueError) as error:
                                record["error"] = str(error)
                                errors.append(f"{name}: {error}")
                                continue
                            finally:
                                if fd is not None:
                                    os.close(fd)
                        ready = {"token": record["token"], "name": name}
                return {"ready": bool(ready), **(ready or {}), "errors": errors}
            except OSError as error:
                return {"ready": False, "error": f"Cannot inspect watch folder: {error}"}
            finally:
                os.close(directory)

    def load(self, token, loader, *, retry_key=None):
        """Snapshot before decoding; consume exactly that version only after loader succeeds."""
        with self.lock:
            if self.closed:
                raise ValueError("watch folder is stopping")
            selected = next(((name, record) for name, record in self.seen.items()
                             if record["token"] == token), None)
            if selected is None:
                raise ValueError("watch-folder selection expired")
            name, record = selected
            if record.get("done") or (record.get("error") and (
                    not record.get("retryable") or record.get("retry_key") == retry_key)):
                return {"skipped": True}
            record.pop("error", None)
            record.pop("retryable", None)
            if self.clock() - record["since"] < self.stable_seconds:
                return {"skipped": True}
            directory = None
            staged = None
            loading = False
            try:
                directory = self._open_directory()
                source = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                with os.fdopen(source, "rb") as stream:
                    before = os.fstat(stream.fileno())
                    if not stat.S_ISREG(before.st_mode) or version(before) != record["version"]:
                        raise ChangedFile("PNG export changed; waiting for a stable file")
                    self.storage.mkdir(parents=True, exist_ok=True)
                    staged = Path(tempfile.mkdtemp(prefix="watch-", dir=self.storage))
                    snapshot = staged / name
                    with snapshot.open("xb") as output:
                        while block := stream.read(1024 * 1024):
                            output.write(block)
                    if version(os.fstat(stream.fileno())) != record["version"]:
                        raise ChangedFile("PNG export changed while copying; waiting for it to finish")
                verify_png(snapshot)
                # Check the directory entry again before publishing any editor state.
                self._check_directory()
                if version(os.stat(name, dir_fd=directory, follow_symlinks=False)) != record["version"]:
                    raise ChangedFile("PNG export was replaced; waiting for a stable file")
                loading = True
                result = loader(snapshot)
                record["done"] = True
                staged = None  # The successful import owns its retained private source copy.
                try:
                    self._check_directory()
                    if version(os.stat(name, dir_fd=directory, follow_symlinks=False)) != record["version"]:
                        raise ChangedFile("PNG export changed during load; new file was retained")
                    os.unlink(name, dir_fd=directory)
                    result["watch_cleanup"] = True
                except (OSError, ValueError) as error:
                    result["watch_cleanup"] = False
                    result["watch_error"] = f"Image loaded, but inbox file was retained: {error}"
                result["watch_name"] = name
                return result
            except (ChangedFile, FileNotFoundError):
                self.seen.pop(name, None)
                return {"skipped": True}
            except Exception as error:
                record["error"] = f"{type(error).__name__}: {error}"
                record["retryable"] = loading
                record["retry_key"] = deepcopy(retry_key)
                raise
            finally:
                if directory is not None:
                    os.close(directory)
                if staged is not None:
                    (staged / name).unlink(missing_ok=True)
                    staged.rmdir()
