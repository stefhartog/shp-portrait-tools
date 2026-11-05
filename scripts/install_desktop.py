"""Install a distinct per-user launcher/icon; never edits existing PhotoCraft launchers."""

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NAME = "shp-photocraft-tools"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", type=Path, required=True, help="existing ONNX model directory (read-only; not copied)")
    parser.add_argument("--exiftool", type=Path, help="optional existing Perl ExifTool executable (not copied)")
    parser.add_argument("--desktop-dir", type=Path, help="override xdg-user-dir DESKTOP")
    parser.add_argument("--comfy-dir", type=Path, help="existing ComfyUI checkout for Tools-owned instance on port 18788")
    args = parser.parse_args()
    python = ROOT / ".venv" / "bin" / "python"
    if not python.is_file():
        parser.error("Run uv sync --extra gpu in this repository first")
    models = args.models.expanduser().resolve()
    for filename in ("yunet.onnx", "face_landmarks.onnx", "selfie_multiclass_256x256.onnx"):
        if not (models / filename).is_file():
            parser.error(f"Missing model {models / filename}; weights are not copied by this installer")
    if args.exiftool and not args.exiftool.is_file():
        parser.error(f"ExifTool does not exist: {args.exiftool}")
    home = Path.home()
    data = Path(os.environ.get("XDG_DATA_HOME", home / ".local" / "share"))
    config_root = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config"))
    launcher = home / ".local" / "bin" / NAME
    config_path = config_root / NAME / "launcher.json"
    icon = data / "icons" / "hicolor" / "scalable" / "apps" / f"{NAME}.svg"
    application = data / "applications" / f"{NAME}.desktop"
    if args.desktop_dir:
        desktop = args.desktop_dir.expanduser()
    else:
        result = subprocess.run(["xdg-user-dir", "DESKTOP"], check=True, capture_output=True, text=True)
        desktop = Path(result.stdout.strip())
    if not desktop.is_absolute() or desktop == home:
        parser.error("A distinct absolute desktop folder is required (use --desktop-dir)")
    config = {"models": str(models), "ports": {"crop": 18768, "dnb": 18765}}
    if args.exiftool:
        config["exiftool"] = str(args.exiftool.resolve())
    if args.comfy_dir:
        from shp_photocraft_tools.comfy import runtime
        directory = args.comfy_dir.expanduser().resolve()
        try:
            runtime(directory)
        except ValueError as error:
            parser.error(str(error))
        config["comfy"] = {"directory": str(directory), "port": 18788}
    wrapper = ("#!/bin/sh\n# Managed by SHP PhotoCraft Tools scripts/install_desktop.py\n"
               f"exec {shlex.quote(str(python))} -m shp_photocraft_tools.launcher "
               f"--config {shlex.quote(str(config_path))} \"$@\"\n")
    quoted_launcher = '"' + str(launcher).replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`").replace("$", "\\$").replace("%", "%%") + '"'
    entry = (ROOT / "desktop" / f"{NAME}.desktop.in").read_text().replace("@LAUNCHER@", quoted_launcher)
    files = {launcher: wrapper, config_path: json.dumps(config, indent=2) + "\n",
             application: entry, desktop / f"{NAME}.desktop": entry, icon: (ROOT / "desktop" / f"{NAME}.svg").read_text()}
    files[icon.with_name(f"{NAME}-tray.svg")] = (ROOT / "desktop" / f"{NAME}-tray.svg").read_text()
    for path, content in files.items():
        if path.exists() and path.read_text() != content:
            parser.error(f"Refusing to overwrite an existing different file: {path}. Review/remove that specific new-tools file to reinstall.")
    for path, content in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    launcher.chmod(0o755)
    shortcut = desktop / f"{NAME}.desktop"
    shortcut.chmod(0o755)
    if shutil.which("desktop-file-validate"):
        subprocess.run(["desktop-file-validate", str(application), str(shortcut)], check=True)
    if shutil.which("update-desktop-database"):
        subprocess.run(["update-desktop-database", str(application.parent)], check=True)
    if shutil.which("gio"):
        trusted = subprocess.run(["gio", "set", str(shortcut), "metadata::trusted", "true"], capture_output=True, text=True)
        if trusted.returncode:
            print(f"Desktop trust could not be set: {trusted.stderr.strip()}. If needed, use Allow Launching on the shortcut.", file=sys.stderr)
    print(json.dumps({"launcher": str(launcher), "application": str(application), "desktop": str(shortcut),
                      "icon": str(icon), "config": str(config_path)}, indent=2))


if __name__ == "__main__":
    main()
