"""Exclusive numbered outputs beside a loaded local source."""

import os
from pathlib import Path


def write_local_result(source, suffix, data, expected_directory=None):
    """Allocate beside the loaded source, never following or replacing an existing output."""
    source = Path(source)
    directory = os.open(source.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        opened = os.fstat(directory)
        if expected_directory is not None and (opened.st_dev, opened.st_ino) != expected_directory:
            raise ValueError("the source folder changed; load the image again before saving")
        number = 1
        while True:
            name = f"{source.stem}_{suffix}_{number:03d}.png"
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
