"""Exclusive numbered outputs beside a loaded local source."""

import json
import os
from contextlib import contextmanager
from pathlib import Path


def write_local_result(source, suffix, data, expected_directory=None, extension=".png"):
    """Allocate beside the loaded source, never following or replacing an existing output."""
    source = Path(source)
    if extension not in (".png", ".tiff"):
        raise ValueError("result extension must be .png or .tiff")
    directory = os.open(source.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        opened = os.fstat(directory)
        if expected_directory is not None and (opened.st_dev, opened.st_ino) != expected_directory:
            raise ValueError("the source folder changed; load the image again before saving")
        number = 1
        while True:
            name = f"{source.stem}_{suffix}_{number:03d}{extension}"
            try:
                fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o666, dir_fd=directory)
                break
            except FileExistsError:
                number += 1
        created = os.fstat(fd)
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
        except BaseException:
            current = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if (current.st_dev, current.st_ino) == (created.st_dev, created.st_ino):
                os.unlink(name, dir_fd=directory)
            raise
        return str(source.parent / name)
    finally:
        os.close(directory)


@contextmanager
def batch_output(source_folder):
    """Pin exclusive batch outputs and atomically maintain one owned preset/manifest."""
    source_folder = Path(source_folder)
    parent = os.open(source_folder, os.O_RDONLY | os.O_DIRECTORY)
    output = None
    try:
        number = 1
        while True:
            name = f"shpcombine_batch_{number:03d}"
            try:
                os.mkdir(name, dir_fd=parent)
                break
            except FileExistsError:
                number += 1
        output = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        folder = source_folder / name
        identity = os.fstat(parent)

        def check_directory():
            current = source_folder.stat()
            if (current.st_dev, current.st_ino) != (identity.st_dev, identity.st_ino):
                raise ValueError("the input folder changed during the batch")
            current_output = folder.stat(follow_symlinks=False)
            pinned_output = os.fstat(output)
            if (current_output.st_dev, current_output.st_ino) != (pinned_output.st_dev, pinned_output.st_ino):
                raise ValueError("the output folder changed during the batch")

        def write_file(filename, data):
            check_directory()
            fd = os.open(filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o666, dir_fd=output)
            created = os.fstat(fd)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.fsync(output)
            except BaseException:
                current = os.stat(filename, dir_fd=output, follow_symlinks=False)
                if (current.st_dev, current.st_ino) == (created.st_dev, created.st_ino):
                    os.unlink(filename, dir_fd=output)
                raise

        def write(index, source, data, extension=".png"):
            if extension not in (".png", ".tiff"):
                raise ValueError("result extension must be .png or .tiff")
            filename = f"{Path(source).stem[:40]}_{number:03d}{extension}"
            write_file(filename, data)
            return str(folder / filename)

        preset_version = None

        def version():
            stat = os.stat("preset.json", dir_fd=output, follow_symlinks=False)
            return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns

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
                temporary = os.stat(".preset.json.tmp", dir_fd=output, follow_symlinks=False)
                try:
                    if version() != preset_version:
                        raise ValueError("the batch preset changed externally; refusing to overwrite it")
                    os.replace(".preset.json.tmp", "preset.json", src_dir_fd=output, dst_dir_fd=output)
                    os.fsync(output)
                finally:
                    try:
                        current = os.stat(".preset.json.tmp", dir_fd=output, follow_symlinks=False)
                    except FileNotFoundError:
                        current = None
                    if current is not None and (current.st_dev, current.st_ino) == (temporary.st_dev, temporary.st_ino):
                        os.unlink(".preset.json.tmp", dir_fd=output)
            preset_version = version()
            return str(folder / "preset.json")

        yield folder, write, write_preset
    finally:
        if output is not None:
            os.close(output)
        os.close(parent)
