"""Discovery of the expected PhotoCraft Flatpak process."""

import configparser
import os
from pathlib import Path
import re
import stat
import subprocess
import xml.etree.ElementTree as ET


APP_ID = "ai.storyteller.photocraft"
SUPPORTED_RELEASE_LINE = (0, 23)


class DiscoveryError(RuntimeError):
    """The expected installed editor process cannot be identified safely."""


def _deployment_executable(executable, commit):
    executable = Path(executable)
    if (not executable.is_absolute() or executable.resolve(strict=True) != executable
            or executable.parts[-8:] != ("app", APP_ID, "x86_64", "stable", commit,
                                         "files", "bin", "photocraft")):
        raise DiscoveryError("Invalid pinned PhotoCraft executable path")
    location = executable.parents[2]
    metadata = location / f"files/share/metainfo/{APP_ID}.metainfo.xml"
    if metadata.resolve(strict=True) != metadata or not metadata.is_file():
        raise DiscoveryError("Invalid Flatpak deployment metadata")
    info = executable.stat()
    if not stat.S_ISREG(info.st_mode) or not info.st_mode & 0o111:
        raise DiscoveryError("PhotoCraft deployment executable is not executable")
    root = ET.parse(metadata).getroot()
    releases = root.findall("./releases/release")
    versions = []
    for release in releases:
        match = re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)",
                             release.get("version", ""))
        if match is None:
            raise DiscoveryError("Unsupported PhotoCraft release metadata")
        versions.append(tuple(map(int, match.groups())))
    if root.findtext("./id") != APP_ID or not versions or max(versions)[:2] != SUPPORTED_RELEASE_LINE:
        raise DiscoveryError("Only PhotoCraft 0.23.x is supported")
    return info


def _info(option):
    result = subprocess.run(
        ["/usr/bin/flatpak", "info", "--user", "--arch=x86_64", option, APP_ID, "stable"],
        capture_output=True, text=True, timeout=5, check=True,
    )
    return result.stdout.strip()


def pinned_user_deployment():
    """Resolve the active compatible user deployment; its commit may change on app updates."""
    try:
        ref = _info("--show-ref")
        commit = _info("--show-commit")
        location = Path(_info("--show-location"))
        if (ref != f"app/{APP_ID}/x86_64/stable" or not re.fullmatch(r"[0-9a-f]{64}", commit)
                or not location.is_absolute() or location.resolve(strict=True) != location
                or location.parts[-5:] != ("app", APP_ID, "x86_64", "stable", commit)):
            raise DiscoveryError("Unsupported or changed PhotoCraft user deployment")
        executable = location / "files/bin/photocraft"
        _deployment_executable(executable, commit)
        if _info("--show-commit") != commit or Path(_info("--show-location")) != location:
            raise DiscoveryError("PhotoCraft deployment changed during inspection")
        return executable, commit
    except DiscoveryError:
        raise
    except (OSError, ValueError, ET.ParseError, subprocess.SubprocessError) as error:
        raise DiscoveryError("Could not verify the PhotoCraft user deployment") from error


def _runtime_dir(value):
    runtime = Path(value)
    if runtime != Path(f"/run/user/{os.getuid()}") or runtime.resolve(strict=True) != runtime:
        raise DiscoveryError("Unsupported Flatpak session runtime directory")
    info = runtime.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise DiscoveryError("Flatpak runtime directory must be private and owner-controlled")
    return runtime


def _instances(runtime_dir):
    runtime_dir = _runtime_dir(runtime_dir)
    result = subprocess.run(
        ["/usr/bin/flatpak", "ps", "--columns=instance,application,child-pid,arch,branch"],
        env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "XDG_RUNTIME_DIR": str(runtime_dir)},
        capture_output=True, text=True, timeout=5, check=True,
    )
    rows = []
    for line in result.stdout.splitlines():
        fields = line.split("\t")
        if len(fields) != 5:
            raise DiscoveryError("Unexpected Flatpak process listing")
        instance, app, child, arch, branch = fields
        if app == APP_ID:
            if not instance.isdecimal() or not child.isdecimal() or int(child) <= 0:
                raise DiscoveryError("Invalid Flatpak process listing")
            rows.append((instance, int(child), arch, branch))
    return rows


def _descendants(root_pid):
    queue = [root_pid]
    seen = set()
    while queue:
        pid = queue.pop()
        if pid in seen or len(seen) >= 512:
            raise DiscoveryError("Ambiguous Flatpak process tree")
        seen.add(pid)
        proc = Path(f"/proc/{pid}")
        if proc.stat().st_uid != os.getuid():
            raise DiscoveryError("Flatpak process tree has an unexpected owner")
        for task in (proc / "task").iterdir():
            text = (task / "children").read_text()
            for value in text.split():
                if not value.isdecimal():
                    raise DiscoveryError("Invalid Flatpak process tree")
                queue.append(int(value))
    return seen


def _sandbox_identity(pid):
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    with Path(f"/proc/{pid}/root/.flatpak-info").open() as stream:
        parser.read_file(stream)
    return (
        parser["Application"]["name"],
        parser["Instance"]["instance-id"],
        parser["Instance"]["app-commit"],
        parser["Instance"]["arch"],
        parser["Instance"]["branch"],
    )


def find_flatpak_process(expected_executable, expected_commit, runtime_dir):
    """Find one candidate from a previously verified active user deployment.

    Identification is non-cryptographic and still requires the separate socket
    ownership check immediately before each authentication exchange.
    """
    try:
        runtime = _runtime_dir(runtime_dir)
        if not isinstance(expected_commit, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_commit):
            raise DiscoveryError("Invalid pinned deployment commit")
        info = _deployment_executable(expected_executable, expected_commit)
        expected = (info.st_dev, info.st_ino)
        instances = _instances(runtime)
        if len(instances) != 1:
            raise DiscoveryError("Expected exactly one running PhotoCraft Flatpak instance")
        instance, wrapper, arch, branch = instances[0]
        if (arch, branch) != ("x86_64", "stable"):
            raise DiscoveryError("Unexpected Flatpak architecture or branch")
        matches = []
        for pid in _descendants(wrapper):
            binary = Path(f"/proc/{pid}/exe").stat()
            if (binary.st_dev, binary.st_ino) != expected:
                continue
            if _sandbox_identity(pid) != (APP_ID, instance, expected_commit, arch, branch):
                raise DiscoveryError("Flatpak process metadata does not match the pinned deployment")
            matches.append(pid)
        if len(matches) != 1 or _instances(runtime) != instances:
            raise DiscoveryError("PhotoCraft process identity is ambiguous or changed")
        return matches[0]
    except DiscoveryError:
        raise
    except (OSError, ValueError, KeyError, ET.ParseError, configparser.Error, subprocess.SubprocessError) as error:
        raise DiscoveryError("Could not verify the PhotoCraft Flatpak process") from error
