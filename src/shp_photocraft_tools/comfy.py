"""Dedicated ComfyUI child and parent-loss watchdog; never adopts an existing listener."""

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from urllib.request import ProxyHandler, build_opener


def runtime(directory):
    root = Path(directory)
    python = next((root / name / "bin/python" for name in ("venv", ".venv")
                   if (root / name / "bin/python").is_file()), None)
    if not root.is_absolute() or not (root / "main.py").is_file() or python is None:
        raise ValueError(f"No ComfyUI main.py and venv/.venv Python in {root}")
    return python


def listening(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=.3):
            return True
    except ConnectionRefusedError:
        return False


def owns_listener(pid, port):
    """Linux socket ownership prevents treating a port-race winner as our ComfyUI."""
    inodes = set()
    for line in Path("/proc/net/tcp").read_text().splitlines()[1:]:
        fields = line.split()
        if fields[1] == f"0100007F:{port:04X}" and fields[3] == "0A":
            inodes.add(fields[9])
    for fd in Path(f"/proc/{pid}/fd").iterdir():
        try:
            target = fd.readlink().as_posix()
        except FileNotFoundError:
            continue
        if target in {f"socket:[{inode}]" for inode in inodes}:
            return True
    return False


def queue_busy(port):
    opener = build_opener(ProxyHandler({}))
    with opener.open(f"http://127.0.0.1:{port}/queue", timeout=2) as response:
        queue = json.load(response)
    if not isinstance(queue.get("queue_running"), list) or not isinstance(queue.get("queue_pending"), list):
        raise ValueError("Invalid ComfyUI queue response")
    return bool(queue["queue_running"] or queue["queue_pending"])


class ManagedComfy:
    def __init__(self, config, logs):
        self.config, self.logs = config, logs
        self.process = None
        self.instance = None
        self.path = None
        self.lock = threading.Lock()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.config['port']}"

    def status(self):
        with self.lock:
            return self._status()

    def _status(self):
        if self.path is None:
            return {"phase": "stopped", "busy": False, "reachable": False}
        try:
            result = json.loads(self.path.read_text())
            if result.get("instance") != self.instance:
                raise ValueError("ComfyUI watchdog identity changed")
        except FileNotFoundError:
            result = {"phase": "starting", "busy": True, "reachable": False}
        except (OSError, ValueError) as error:
            result = {"phase": "unavailable", "busy": True, "reachable": False, "error": str(error)}
        if self.process is not None and self.process.poll() is not None and result["phase"] not in ("stopped", "error"):
            result.update(phase="error", busy=False, reachable=False,
                          error=f"ComfyUI watchdog exited ({self.process.returncode}); see comfy.log")
        return result

    def start(self):
        with self.lock:
            if self.process is not None and self.process.poll() is None:
                return {"started": False, "message": "Tools ComfyUI is running or starting"}
            if self.path is not None:
                self.path.unlink(missing_ok=True)
            self.instance = uuid.uuid4().hex
            self.path = self.logs / f"comfy-{self.instance}.json"
            command = [sys.executable, "-u", "-m", "shp_photocraft_tools.comfy",
                       "--directory", self.config["directory"], "--port", str(self.config["port"]),
                       "--status", str(self.path), "--instance", self.instance, "--parent-pid", str(os.getpid())]
            try:
                with (self.logs / "comfy.log").open("ab") as log:
                    self.process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT,
                                                    stdin=subprocess.DEVNULL, start_new_session=True)
            except OSError as error:
                self.path.write_text(json.dumps({"instance": self.instance, "phase": "error",
                                                "busy": False, "reachable": False, "error": str(error)}))
                raise
            return {"started": True, "message": "Starting dedicated Tools ComfyUI"}

    def close(self):
        with self.lock:
            if self.process is not None and self.process.poll() is None:
                self.process.terminate()
                # The watchdog drains its own queue, including after supervisor loss.
                self.process.wait()
            if self.path is not None:
                self.path.unlink(missing_ok=True)


def run(directory, port, path, instance, parent):
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    child = None
    ready = False

    def publish(phase, busy=False, error=None):
        state = {"instance": instance, "phase": phase, "busy": busy,
                 "reachable": ready and phase == "running", "pid": child.pid if child else None}
        if error:
            state["error"] = error
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(state))
        temporary.replace(path)

    try:
        if listening(port):
            raise ValueError(f"Dedicated ComfyUI port {port} is occupied. No existing instance was adopted or stopped.")
        python = runtime(directory)
        for name in ("user", "output", "input", "temp"):
            (path.parent / f"comfy-{name}").mkdir(parents=True, exist_ok=True)
        publish("starting", True)
        child = subprocess.Popen([str(python), "main.py", "--listen", "127.0.0.1", "--port", str(port),
                                  "--disable-auto-launch",
                                  "--user-directory", str(path.parent / "comfy-user"),
                                  "--output-directory", str(path.parent / "comfy-output"),
                                  "--input-directory", str(path.parent / "comfy-input"),
                                  "--temp-directory", str(path.parent / "comfy-temp")], cwd=directory)
        deadline = time.monotonic() + 180
        while child.poll() is None:
            if os.getppid() != parent:
                stop.set()
            try:
                if owns_listener(child.pid, port):
                    busy = queue_busy(port)
                    ready = True
                    if stop.is_set() and not busy:
                        break
                    publish("stopping" if stop.is_set() else "running", busy)
                elif ready:
                    publish("unavailable", True, "Owned ComfyUI listener disappeared; see comfy.log")
                elif stop.is_set():
                    break
                elif time.monotonic() >= deadline:
                    raise ValueError("Dedicated ComfyUI startup timed out; see comfy.log")
            except (OSError, ValueError) as error:
                if not ready and time.monotonic() >= deadline:
                    raise ValueError(f"ComfyUI startup timed out: {error}") from error
                publish("unavailable" if ready else "starting", True, str(error))
            time.sleep(.5)
        if child.poll() is not None and not stop.is_set():
            raise ValueError(f"Dedicated ComfyUI exited ({child.returncode}); see comfy.log")
        if child.poll() is None:
            publish("stopping")
            child.terminate()
            try:
                child.wait(timeout=15)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        ready = False
        publish("stopped")
    except (OSError, ValueError) as error:
        # A failed startup has never been exposed to Crop as a ready service.
        if child is not None and child.poll() is None and not ready:
            child.terminate()
            try:
                child.wait(timeout=15)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        publish("error", error=str(error))
        print(error, file=sys.stderr, flush=True)
        raise SystemExit(1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--status", type=Path, required=True)
    parser.add_argument("--instance", required=True)
    parser.add_argument("--parent-pid", type=int, required=True)
    args = parser.parse_args()
    run(args.directory, args.port, args.status, args.instance, args.parent_pid)


if __name__ == "__main__":
    main()
