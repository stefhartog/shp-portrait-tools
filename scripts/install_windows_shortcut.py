"""Create or remove a Start Menu (and optionally Desktop) shortcut that starts Tools with no console window.

The shortcut runs `<repo>\\.venv\\Scripts\\pythonw.exe -m shp_photocraft_tools.launcher --config <launcher.json>`
with the repository as the working directory. Nothing is written without being listed first by --dry-run;
an existing shortcut is never overwritten without --force.
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

from shp_photocraft_tools.desktop.icons import write_icon
from shp_photocraft_tools.desktop.windows_identity import APP_ID, shortcut_app_id

ROOT = Path(__file__).resolve().parent.parent
NAME = "SHP Portrait Tools"
ICON_NAME = "shp-photocraft-tools.ico"

CREATE_SCRIPT = (
    "$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:SHP_LNK);"
    "$s.TargetPath=$env:SHP_TARGET;$s.Arguments=$env:SHP_ARGS;$s.WorkingDirectory=$env:SHP_CWD;"
    "$s.Description=$env:SHP_DESC;if($env:SHP_ICON){$s.IconLocation=$env:SHP_ICON};$s.Save()")
READ_SCRIPT = (
    "$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:SHP_LNK);"
    "$s.TargetPath;$s.Arguments;$s.WorkingDirectory;$s.IconLocation")


def appdata_config():
    return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "shp-photocraft-tools" / "launcher.json"


def start_menu_dir():
    base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs"


def desktop_dir():
    return Path(os.environ.get("USERPROFILE") or Path.home()) / "Desktop"


def shortcut_arguments(config):
    return f'-m shp_photocraft_tools.launcher --config "{config}"'


def powershell(script, **env):
    result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                            env={**os.environ, **env}, capture_output=True, text=True, timeout=60,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return result.stdout.splitlines()


def read_shortcut(path):
    target, arguments, cwd, icon = (powershell(READ_SCRIPT, SHP_LNK=str(path)) + [""] * 4)[:4]
    return {"target": target, "arguments": arguments, "cwd": cwd, "icon": icon,
            "app_id": shortcut_app_id(path)}


def locations(args):
    if args.output_dir:
        base = Path(args.output_dir)
        places = [base / "Start Menu"]
        if args.desktop:
            places.append(base / "Desktop")
        return places, base
    places = [start_menu_dir()]
    if args.desktop:
        places.append(desktop_dir())
    return places, args.config.parent


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--desktop", action="store_true", help="also create a Desktop shortcut")
    parser.add_argument("--remove", action="store_true", help="delete the shortcut(s) this script creates")
    parser.add_argument("--force", action="store_true", help="overwrite an existing shortcut")
    parser.add_argument("--dry-run", action="store_true", help="print what would happen and change nothing")
    parser.add_argument("--no-icon", action="store_true", help="do not generate or use an icon")
    parser.add_argument("--output-dir", type=Path,
                        help="write to <dir>\\Start Menu and <dir>\\Desktop (and the icon to <dir>) instead of the real folders")
    parser.add_argument("--repo", type=Path, default=ROOT, help="repository containing .venv (default: this one)")
    parser.add_argument("--config", type=Path, default=appdata_config(), help="launcher.json the shortcut passes")
    args = parser.parse_args(argv)
    if sys.platform != "win32":
        print("This installer is for Windows only (Linux: scripts/install_desktop.py).", file=sys.stderr)
        return 2

    repo = args.repo.resolve()
    pythonw = repo / ".venv" / "Scripts" / "pythonw.exe"
    places, icon_dir = locations(args)
    links = [place / f"{NAME}.lnk" for place in places]
    icon = icon_dir / ICON_NAME
    status = 0

    if args.remove:
        for link in links:
            if not link.exists():
                print(f"Not present: {link}")
            elif args.dry_run:
                print(f"Would remove: {link}")
            else:
                link.unlink()
                print(f"Removed: {link}")
        if icon.exists() and args.output_dir is None and not args.no_icon:
            print(f"Left in place (harmless): {icon}")
        return 0

    if not pythonw.exists():
        print(f"Warning: {pythonw} does not exist; run `python -m uv sync --locked --extra gpu` in {repo} first.",
              file=sys.stderr)
        status = 1
    if not args.config.exists():
        print(f"Note: {args.config} does not exist yet; run `python -m uv run --no-sync shp-photocraft-tools "
              "--init-config` before launching.", file=sys.stderr)
    print(f"Target:     {pythonw}\nArguments:  {shortcut_arguments(args.config)}\nStart in:   {repo}")
    for link in links:
        if link.exists() and not args.force:
            print(f"Exists (skipped, use --force to replace): {link}")
            status = status or 1
        elif args.dry_run:
            print(f"Would {'replace' if link.exists() else 'create'}: {link}")
        else:
            continue
    if args.dry_run:
        if not args.no_icon:
            print(f"Would write icon: {icon}")
        return status
    if status == 1 and not pythonw.exists():
        return status

    pending = [link for link in links if not link.exists() or args.force]
    if not pending:
        return status
    use_icon = not args.no_icon
    if use_icon:
        try:
            write_icon(icon)
        except Exception as error:
            print(f"Icon skipped: {error}", file=sys.stderr)
            use_icon = False
    for link in pending:
        link.parent.mkdir(parents=True, exist_ok=True)
        powershell(CREATE_SCRIPT, SHP_LNK=str(link), SHP_TARGET=str(pythonw), SHP_ARGS=shortcut_arguments(args.config),
                   SHP_CWD=str(repo), SHP_DESC="SHP Portrait Tools (no console window)",
                   SHP_ICON=f"{icon},0" if use_icon else "")
        shortcut_app_id(link, APP_ID)
        print(f"Created: {link}")
    return status


if __name__ == "__main__":
    sys.exit(main())
