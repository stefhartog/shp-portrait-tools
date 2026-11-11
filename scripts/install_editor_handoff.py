"""Install the opted-in PhotoCraft automation launcher and LightCraft handoff."""

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import tempfile

ROOT = Path(__file__).resolve().parent.parent


def atomic_write(path, content, mode):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(content)
            stream.flush()
            os.fchmod(stream.fileno(), mode)
            os.fsync(stream.fileno())
        except BaseException:
            temporary.unlink()
            raise
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def install(home, backup, python, *, defer_editor=False):
    executable = home / ".local/bin"
    desktop = home / ".local/share/applications/ai.storyteller.photocraft.desktop"
    settings = home / ".config/lightcraft/ui.json"
    launcher = executable / "photocraft-dnb"
    helper = executable / "photocraft-lightcraft"
    originals = {}
    for path in (desktop, settings, launcher, helper):
        if path.is_symlink():
            raise ValueError(f"Refusing a symlink: {path}")
        originals[path] = (path.read_bytes(), path.stat().st_mode & 0o777) if path.exists() else None
    if originals[desktop] is None or originals[settings] is None:
        raise ValueError("Existing PhotoCraft desktop entry and LightCraft settings are required.")
    if not python.is_file() or not (executable / "photocraft").is_file():
        raise ValueError("Installed worktree Python and PhotoCraft executable must exist.")
    for path in (launcher, helper):
        if originals[path] is not None:
            raise ValueError(f"Existing launcher requires manual review; not overwritten: {path}")
    desktop_text = originals[desktop][0].decode()
    commands = re.findall(r"^Exec=(.*)$", desktop_text, re.M)
    allowed = [f'"{executable / name}" %F' for name in ("photocraft", "photocraft-dnb")]
    if len(commands) != 1 or commands[0] not in allowed:
        raise ValueError("Custom desktop Exec/actions require manual review.")
    desktop_text = desktop_text.replace("Exec=" + commands[0], f'Exec="{launcher}" %F', 1)
    settings_text = originals[settings][0].decode()
    value = json.loads(settings_text)
    previous = value.get("settings", {}).get("externalEditor")
    if previous not in ("PhotoCraft", str(executable / "photocraft"), str(launcher)):
        raise ValueError("LightCraft editor selection changed; review before replacing.")
    pattern = r'("externalEditor"\s*:\s*)' + re.escape(json.dumps(previous))
    settings_text, count = re.subn(pattern, lambda match: match[1] + json.dumps(str(helper)), settings_text)
    if count != 1:
        raise ValueError("Expected exactly one LightCraft externalEditor field.")
    expected = json.loads(originals[settings][0])
    expected["settings"]["externalEditor"] = str(helper)
    if json.loads(settings_text) != expected:
        raise ValueError("Unexpected LightCraft settings changes.")
    command = f"exec {shlex.quote(str(python))} -m shp_photocraft_tools.editor_handoff"
    marker = "# Managed by SHP Portrait Tools scripts/install_editor_handoff.py\n"
    updates = {
        desktop: (desktop_text.encode(), originals[desktop][1]),
        launcher: (("#!/bin/sh\n" + marker + command + ' --launch -- "$@"\n').encode(), 0o755),
        helper: (("#!/bin/sh\n" + marker + command + ' -- "$@"\n').encode(), 0o755),
    }
    if not defer_editor:
        updates[settings] = (settings_text.encode(), originals[settings][1])
    backup.mkdir(mode=0o700, parents=True, exist_ok=False)
    for path, original in originals.items():
        if original is not None:
            destination = backup / path.relative_to(home)
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            destination.write_bytes(original[0])
            destination.chmod(0o600)
    (backup / "manifest.json").write_text(json.dumps({
        str(path): {"existed": original is not None, "mode": original[1] if original else None}
        for path, original in originals.items()}, indent=2) + "\n")
    # Check every target again before changing any of them.
    for path, original in originals.items():
        if path.is_symlink() or (path.exists() != (original is not None)):
            raise ValueError(f"Target changed during install: {path}")
        if original is not None and (path.read_bytes(), path.stat().st_mode & 0o777) != original:
            raise ValueError(f"Target changed during install: {path}")
    written = []
    try:
        for path, (content, mode) in updates.items():
            original = originals[path]
            if path.is_symlink() or (path.exists() != (original is not None)):
                raise ValueError(f"Target changed during install: {path}")
            if original is not None and (path.read_bytes(), path.stat().st_mode & 0o777) != original:
                raise ValueError(f"Target changed during install: {path}")
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(path, content, mode)
            written.append(path)
    except BaseException:
        for path in reversed(written):
            if path.read_bytes() != updates[path][0]:
                raise RuntimeError(f"Concurrent update at {path}; restore manually from {backup}")
            original = originals[path]
            if original is None:
                path.unlink()
            else:
                atomic_write(path, *original)
        raise
    return list(updates)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup-dir", type=Path, required=True, help="new private backup directory")
    parser.add_argument("--defer-editor-setting", action="store_true",
                        help="leave a running LightCraft's settings untouched; select helper in its GUI")
    args = parser.parse_args()
    try:
        updated = install(Path.home(), args.backup_dir, ROOT / ".venv/bin/python",
                          defer_editor=args.defer_editor_setting)
    except (OSError, ValueError) as error:
        parser.exit(1, str(error) + "\n")
    print("Updated:\n" + "\n".join(str(path) for path in updated))
    print(f"Rollback originals: {args.backup_dir}")
    if args.defer_editor_setting:
        print(f"In LightCraft Settings > External Editing select {Path.home() / '.local/bin/photocraft-lightcraft'}")
    if shutil.which("update-desktop-database"):
        import subprocess
        subprocess.run(["update-desktop-database", str(Path.home() / ".local/share/applications")], check=True)


if __name__ == "__main__":
    main()
