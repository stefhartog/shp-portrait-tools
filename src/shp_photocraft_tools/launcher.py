"""Start/reuse Tools and present its single-instance owned desktop window."""

import argparse
import errno
import fcntl
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from importlib.resources import files
from pathlib import Path
from urllib.error import URLError
from urllib.request import ProxyHandler, Request, build_opener

from .shared.health import identity

APP = "SHP PhotoCraft Tools"
DEFAULT_PORTS = {"crop": 18768, "dnb": 18765}
SHELL_PORT = 18760
MODULES = {"crop": "crop.server", "dnb": "dnb.ui_server"}
MODEL_FILES = ("yunet.onnx", "face_landmarks.onnx", "selfie_multiclass_256x256.onnx")
CHROMIUM_FLATPAK = "io.github.ungoogled_software.ungoogled_chromium"


class LaunchError(RuntimeError):
    """Actionable startup or identity failure."""


def read_config(path):
    try:
        config = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        raise LaunchError(f"Cannot read launcher configuration {path}: {error}. Run scripts/install_desktop.py.") from error
    if not isinstance(config, dict):
        raise LaunchError(f"Launcher configuration must be a JSON object: {path}")
    models = config.get("models")
    if not isinstance(models, str) or not Path(models).is_absolute():
        raise LaunchError(f"Set an absolute 'models' directory in {path}")
    for filename in MODEL_FILES:
        model = Path(models) / filename
        if not model.is_file() or not os.access(model, os.R_OK):
            raise LaunchError(f"Missing or unreadable D&B model: {model}. Update {path}; weights are not copied.")
    ports = config.get("ports", DEFAULT_PORTS)
    if not isinstance(ports, dict) or set(ports) != set(MODULES):
        raise LaunchError(f"'ports' must contain crop and dnb in {path}")
    if any(type(port) is not int or not 1024 <= port <= 65535 for port in ports.values()):
        raise LaunchError(f"Launcher ports must be integers between 1024 and 65535 in {path}")
    if len(set(ports.values())) != 2:
        raise LaunchError("Crop and D&B must use different ports")
    config["ports"] = ports
    shell_port = config.get("shell_port", SHELL_PORT)
    if type(shell_port) is not int or not 1024 <= shell_port <= 65535 or shell_port in ports.values():
        raise LaunchError("'shell_port' must be a distinct integer port between 1024 and 65535")
    config["shell_port"] = shell_port
    comfy = config.get("comfy")
    if comfy is not None:
        if (not isinstance(comfy, dict) or set(comfy) != {"directory", "port"}
                or not isinstance(comfy["directory"], str) or not Path(comfy["directory"]).is_absolute()
                or type(comfy["port"]) is not int or not 1024 <= comfy["port"] <= 65535
                or comfy["port"] in (*ports.values(), shell_port, 8188)):
            raise LaunchError("'comfy' requires an absolute directory and a distinct dedicated port (not 8188)")
        from .comfy import runtime
        try:
            runtime(comfy["directory"])
        except ValueError as error:
            raise LaunchError(str(error)) from error
    exiftool = config.get("exiftool")
    if exiftool is not None and (not isinstance(exiftool, str) or not Path(exiftool).is_file()):
        raise LaunchError(f"Configured ExifTool does not exist: {exiftool}")
    return config


def probe(port):
    """None means no listener; an occupied but wrong/unhealthy port is an explicit failure."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            pass
    except OSError as error:
        if error.errno == errno.ECONNREFUSED:
            return None
        raise LaunchError(f"Cannot check local port {port}: {error}") from error
    try:
        opener = build_opener(ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{port}/api/health", timeout=2) as response:
            result = json.loads(response.read(16384))
    except (OSError, URLError, ValueError) as error:
        raise LaunchError(f"Port {port} is occupied but is not a healthy new-module server: {error}. "
                          "No process was stopped. Choose different ports in launcher.json.") from error
    if not isinstance(result, dict):
        raise LaunchError(f"Port {port} returned an invalid server identity")
    return result


def verify(found, expected, port):
    for key in ("application", "module", "package", "python", "data", "models"):
        if found.get(key) != expected.get(key):
            raise LaunchError(f"Port {port} belongs to a different server/configuration ({key} mismatch). "
                              "No process was stopped; choose different ports in launcher.json.")


def ensure_shell(config_path, data, config, logs):
    port = config["shell_port"]
    expected = identity("shell", data, config["models"])

    def check(found):
        verify(found, expected, port)
        if (found.get("shell_protocol") != 1 or found.get("ports") != config["ports"]
                or found.get("exiftool") != config.get("exiftool")
                or found.get("comfy") != config.get("comfy")):
            raise LaunchError(f"Port {port} belongs to an incompatible Tools supervisor. "
                              "Close its work before changing launcher configuration.")

    found = probe(port)
    if found is not None:
        check(found)
        with build_opener(ProxyHandler({})).open(
                Request(f"http://127.0.0.1:{port}/api/ui/reserve", data=b"{}", method="POST",
                        headers={"Origin": f"http://127.0.0.1:{port}", "Content-Type": "application/json"}),
                timeout=3) as response:
            if not json.load(response)["accepted"]:
                raise LaunchError(f"Server on port {port} is stopping; reopen the launcher")
        return found
    env = dict(os.environ)
    env["SHP_SERVER_INSTANCE"] = uuid.uuid4().hex
    log_path = logs / "shell.log"
    with log_path.open("ab") as log:
        process = subprocess.Popen(
            [sys.executable, "-u", "-m", "shp_photocraft_tools.supervisor",
             "--config", str(config_path.resolve()), "--data", str(data), "--state", str(logs)],
            env=env, cwd=str(data), stdout=log, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, start_new_session=True,
        )
    deadline = time.monotonic() + 40
    failure = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise LaunchError(f"Tools supervisor exited with status {process.returncode}. See {log_path}")
        try:
            found = probe(port)
            if found is not None:
                check(found)
                if found.get("instance") != env["SHP_SERVER_INSTANCE"] or found.get("pid") != process.pid:
                    raise LaunchError(f"Another process acquired port {port} while Tools was starting")
                threading.Thread(target=process.wait, daemon=True).start()
                return found
        except LaunchError as error:
            failure = error
        time.sleep(0.15)
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
    raise LaunchError(f"Tools supervisor did not become ready. See {log_path}. {failure or ''}")


def chromium_command(config, urls):
    binary = config.get("ungoogled_chromium")
    if binary is not None and (not isinstance(binary, str) or not binary):
        raise LaunchError("'ungoogled_chromium' must be an executable name or path")
    executable = next((found for name in ([binary] if binary else ["ungoogled-chromium", "ungoogled-chromium-browser"])
                       if (found := shutil.which(name))), None)
    if executable:
        command = [executable]
        profile = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "shp-photocraft-tools" / "ungoogled-chromium-profile"
    elif binary is None and (flatpak := shutil.which("flatpak")):
        result = subprocess.run([flatpak, "info", CHROMIUM_FLATPAK], capture_output=True, timeout=10)
        if result.returncode:
            raise LaunchError("Ungoogled Chromium is not installed. Configure 'ungoogled_chromium' in launcher.json.")
        command = [flatpak, "run", CHROMIUM_FLATPAK]
        profile = Path.home() / ".var/app" / CHROMIUM_FLATPAK / "shp-photocraft-tools-profile"
    else:
        raise LaunchError(f"Ungoogled Chromium executable not found: {binary or 'ungoogled-chromium'}")
    return [*command, f"--user-data-dir={profile}", "--class=shp-photocraft-tools",
            f"--app={urls[0]}", "--window-size=1347,994", "--no-first-run", "--no-default-browser-check"]


def desktop_command(url):
    helper = files("shp_photocraft_tools.desktop").joinpath("controller.py")
    data = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "shp-photocraft-tools" / "webview"
    icon = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "icons/hicolor/scalable/apps/shp-photocraft-tools.svg"
    command = ["/usr/bin/python3", str(helper), "--url", url, "--data-dir", str(data), "--icon", str(icon)]
    checked = subprocess.run([*command, "--check"], capture_output=True, text=True, timeout=15)
    if checked.returncode:
        raise LaunchError(f"Desktop runtime unavailable: {checked.stderr.strip()}. "
                          "Use --browser for Ungoogled Chromium or --no-browser and the loopback URL.")
    return command


def launch(config_path, no_browser=False, browser=False):
    config = read_config(config_path)
    data = Path.home() / ".local" / "share" / "shp-photocraft-tools"
    state = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "shp-photocraft-tools"
    state.mkdir(parents=True, exist_ok=True)
    data.mkdir(parents=True, exist_ok=True)
    urls = [f"http://127.0.0.1:{config['shell_port']}/"]
    command = None if no_browser else (chromium_command(config, urls) if browser else desktop_command(urls[0]))
    log_name = "chromium.log" if browser else "desktop.log"
    with (state / "launcher.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        shell = ensure_shell(config_path, data, config, state)
    if command:
        with (state / log_name).open("ab") as log:
            process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT,
                                       stdin=subprocess.DEVNULL, start_new_session=True)
        try:
            code = process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            code = None  # The owning desktop/browser process remains attached while running.
            threading.Thread(target=process.wait, daemon=True).start()
        if code not in (0, None):
            raise LaunchError(f"Tools window exited with status {code}. Servers remain available; see {state / log_name}")
    return {"urls": urls, "servers": {"shell": shell["pid"]}, "logs": str(state)}


def main():
    parser = argparse.ArgumentParser(description=f"Show the {APP} desktop window and tray controller")
    default_config = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "shp-photocraft-tools" / "launcher.json"
    parser.add_argument("--config", type=Path, default=default_config)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--no-browser", action="store_true", help="start/reuse the shell without opening a window")
    mode.add_argument("--browser", action="store_true", help="open Ungoogled Chromium app instead of the owned desktop")
    args = parser.parse_args()
    try:
        result = launch(args.config, args.no_browser, args.browser)
    except (LaunchError, OSError) as error:
        message = f"{APP}: {error}"
        print(message, file=sys.stderr)
        if shutil.which("notify-send"):
            subprocess.run(["notify-send", "--urgency=critical", APP, str(error)], check=False)
        raise SystemExit(1)
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
