"""Windows implementation of shared.local_output (loaded only on win32).

Same guarantees as the POSIX code: never overwrite or follow an existing output, keep the pinned
source/output folders from being swapped, and replace the batch preset atomically. Folders are pinned
with share-delete-less handles (see winfs); files are created with CREATE_NEW, which fails on any
existing name, including a symlink/junction and case-insensitive collisions.
"""

import json
import os
import stat
from contextlib import contextmanager
from pathlib import Path

from . import winfs

_CREATE = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)


def _exclusive_write(path, data):
    """Create path exclusively, write durably, and remove only our own file if writing fails."""
    fd = os.open(winfs.extended(path), _CREATE, 0o666)
    created = os.fstat(fd)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        try:
            current = os.lstat(winfs.extended(path))
        except FileNotFoundError:
            current = None
        if current is not None and (current.st_dev, current.st_ino) == (created.st_dev, created.st_ino):
            os.unlink(winfs.extended(path))
        raise


def _create_next(directory, make_name, data):
    number = 1
    while True:
        name = winfs.validate_name(make_name(number))
        path = directory / name
        try:
            _exclusive_write(path, data)
            return path
        except FileExistsError:
            number += 1
        except PermissionError:
            # CREATE_NEW reports access denied for an existing directory of that name.
            if not os.path.lexists(winfs.extended(path)):
                raise
            number += 1


def write_local_result(source, suffix, data, expected_directory=None, extension=".png"):
    source = Path(source)
    pin = winfs.pin_directory(source.parent)
    try:
        opened = os.stat(winfs.extended(source.parent))
        if not stat.S_ISDIR(opened.st_mode):
            raise NotADirectoryError(str(source.parent))
        if expected_directory is not None and (opened.st_dev, opened.st_ino) != expected_directory:
            raise ValueError("the source folder changed; load the image again before saving")
        path = _create_next(source.parent, lambda n: f"{source.stem}_{suffix}_{n:03d}{extension}", data)
        return str(path)
    finally:
        winfs.close_handle(pin)


@contextmanager
def batch_output(source_folder):
    source_folder = Path(source_folder)
    parent_pin = winfs.pin_directory(source_folder)
    output_pin = None
    try:
        identity = os.stat(winfs.extended(source_folder))
        if not stat.S_ISDIR(identity.st_mode):
            raise NotADirectoryError(str(source_folder))
        identity = identity.st_dev, identity.st_ino
        number = 1
        while True:
            name = f"shpcombine_batch_{number:03d}"
            try:
                os.mkdir(winfs.extended(source_folder / name))
                break
            except FileExistsError:
                number += 1
        folder = source_folder / name
        output_pin = winfs.pin_directory(folder, follow_links=False)
        output_info = os.lstat(winfs.extended(folder))
        if winfs.is_reparse_point(output_info) or not stat.S_ISDIR(output_info.st_mode):
            raise ValueError("the output folder is not a plain directory")
        output_identity = output_info.st_dev, output_info.st_ino

        def check_directory():
            current = os.stat(winfs.extended(source_folder))
            if (current.st_dev, current.st_ino) != identity:
                raise ValueError("the input folder changed during the batch")
            current_output = os.lstat(winfs.extended(folder))
            if winfs.is_reparse_point(current_output) or (
                    current_output.st_dev, current_output.st_ino) != output_identity:
                raise ValueError("the output folder changed during the batch")

        def write_file(filename, data):
            check_directory()
            _exclusive_write(folder / winfs.validate_name(filename), data)

        def write(index, source, data, extension=".png"):
            if extension not in (".png", ".tiff"):
                raise ValueError("result extension must be .png or .tiff")
            filename = f"{Path(source).stem[:40].rstrip(' .')}_{number:03d}{extension}"
            write_file(filename, data)
            return str(folder / filename)

        preset = folder / "preset.json"
        temporary_path = folder / ".preset.json.tmp"
        preset_version = None

        def version():
            info = os.lstat(winfs.extended(preset))
            return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns

        def write_preset(settings):
            nonlocal preset_version
            data = json.dumps(settings, indent=2).encode()
            if preset_version is None:
                write_file("preset.json", data)
            else:
                check_directory()
                if version() != preset_version:
                    raise ValueError("the batch preset changed externally; refusing to overwrite it")
                write_file(".preset.json.tmp", data)
                temporary = os.lstat(winfs.extended(temporary_path))
                try:
                    if version() != preset_version:
                        raise ValueError("the batch preset changed externally; refusing to overwrite it")
                    os.replace(winfs.extended(temporary_path), winfs.extended(preset))
                finally:
                    try:
                        current = os.lstat(winfs.extended(temporary_path))
                    except FileNotFoundError:
                        current = None
                    if current is not None and (current.st_dev, current.st_ino) == (temporary.st_dev, temporary.st_ino):
                        os.unlink(winfs.extended(temporary_path))
            preset_version = version()
            return str(preset)

        yield folder, write, write_preset
    finally:
        if output_pin is not None:
            winfs.close_handle(output_pin)
        winfs.close_handle(parent_pin)
