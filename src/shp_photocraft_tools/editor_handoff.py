"""Permissioned PhotoCraft launch and acknowledged LightCraft file handoff."""

import argparse
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys

from .shared.photocraft import Client, ControlError, detect_config


def input_paths(files, read_root):
    root = Path(read_root).resolve(strict=True)
    paths = []
    for filename in files:
        path = Path(filename).expanduser().resolve(strict=True)
        if not path.is_file():
            raise ValueError(f"Not a file: {filename}")
        if not path.is_relative_to(root):
            raise ValueError(f"File is outside PhotoCraft's automation read root ({root}): {filename}")
        paths.append(str(path))
    return paths


def handoff(paths, config):
    client = Client(config["port"], config["token_file"])
    opened = 0
    try:
        for path in paths:
            try:
                reply = client.call_once("app.open", path=path)
            except ControlError as error:
                raise ControlError(
                    f"{opened} of {len(paths)} files acknowledged. {error}. "
                    "Check PhotoCraft before retrying; an unacknowledged file may already be open."
                ) from error
            if not isinstance(reply, dict) or reply.get("path") != path or not reply.get("name"):
                raise ControlError("PhotoCraft did not acknowledge the requested file. Check its window before retrying.")
            opened += 1
            for warning in reply.get("warnings") or []:
                print(f"PhotoCraft import warning: {warning}", file=sys.stderr)
        client.call_once("ui.focus")
    finally:
        client.close()
    return opened


def launch(paths, config):
    """Only a genuinely absent listener permits starting the permissioned instance."""
    try:
        with socket.create_connection(("127.0.0.1", config["port"]), timeout=1):
            pass
    except ConnectionRefusedError:
        pass
    else:
        return handoff(paths, config)
    binary = os.environ.get("PHOTOCRAFT_BIN") or shutil.which("photocraft")
    if not binary:
        raise ControlError("Cannot find PhotoCraft; set PHOTOCRAFT_BIN to its executable.")
    token = Path(config["token_file"])
    exchange = Path(config["exchange"])
    token.parent.mkdir(parents=True, exist_ok=True)
    exchange.mkdir(parents=True, exist_ok=True)
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("PHOTOCRAFT_CONTROL", "PHOTOCRAFT_AUTOMATION"))}
    args = [binary, "--control", str(config["port"]), "--control-token-file", str(token),
            "--automation-read-root", str(config["read_root"]), "--automation-write-root", str(exchange),
            "--", *paths]
    os.execvpe(binary, args, env)


def report_error(message):
    text = ("PhotoCraft handoff failed: " + message +
            "\nSave and close an incompatible PhotoCraft instance, then start PhotoCraft "
            "with the Portrait Tools desktop launcher. Do not retry blindly.")
    print(text, file=sys.stderr)
    notifier = shutil.which("notify-send")
    if notifier:
        result = subprocess.run([notifier, "--urgency=critical", "--", "PhotoCraft handoff failed", text],
                                check=False, capture_output=True, text=True)
        if result.returncode:
            print("Desktop notification failed: " + result.stderr.strip(), file=sys.stderr)
    else:
        print("Desktop notifications unavailable; see this error in the launching terminal.", file=sys.stderr)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--launch", action="store_true", help="start permissioned PhotoCraft if not running")
    parser.add_argument("files", nargs="*")
    args = parser.parse_args(argv)
    try:
        if not args.launch and not args.files:
            raise ValueError("LightCraft must pass at least one filename; configure the helper path without placeholders.")
        config = detect_config("SHPC", "crop")
        paths = input_paths(args.files, config["read_root"])
        if args.launch:
            launch(paths, config)
        else:
            opened = handoff(paths, config)
            print(f"PhotoCraft acknowledged {opened} file(s).")
    except (ControlError, OSError, ValueError) as error:
        report_error(str(error))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
