"""Loopback control server and capability-guarded supervision of the one Skin Tools worker.

The desktop window and browser tabs load the Skin Tools page directly from the worker. This server only
serves health/status, module controls, the acknowledged native quit and UI leases, and a small status page at
`/` that moves on to the worker page once Skin Tools is running.
"""

import argparse
import http.client
import json
import os
import secrets
import signal
import socket
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlsplit

from .launcher import (MODULE, SERVER_MODULE, LaunchError, apply_photocraft_env, background_python, probe,
                       read_config, set_startup, verify)
from .shared.health import identity
from .shared.lifecycle import BrowserHandler, BrowserServer
from .shared.owned_process import (adopt, drain_pinned, owns, pin_session, process_identity,
                                   require_session_stopped, spawn_options)
from .shared.photocraft.bridge import detect_config
from .shared.platform_paths import module_folder
from .shared.supervision import ControlError

SHELL_PROTOCOL = 1

# Shown at `/` while Skin Tools is starting, stopped or failed; it moves on to the worker page once running.
# It also hands this origin's saved browser preferences (theme, view state) to the worker page once: before
# the shell was removed, the Skin Tools page ran on this origin, so its localStorage lives here.
STATUS_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>SHP Portrait Tools</title>
<style>
html, body { margin:0; height:100%; background:#1b1b1b; color:#ddd; font:14px/1.5 system-ui, sans-serif; }
main { max-width:640px; padding:48px 32px; margin:0 auto; }
h1 { font-size:18px; margin:0 0 12px; }
#message { color:#bac2ca; }
</style></head>
<body><main><h1>SHP Portrait Tools</h1><p id="message" role="status" aria-live="polite">Starting Skin Tools&hellip;</p></main>
<script>
const PAGE = __PAGE__;
function handoff() {
  try {
    if (localStorage.getItem('shp-prefs-handed-off')) return '';
    const prefs = {};
    for (let i = 0; i < localStorage.length; i++) {
      const key = localStorage.key(i);
      if (key === 'shp-tools-theme' || key.startsWith('dnb-')) prefs[key] = localStorage.getItem(key);
    }
    localStorage.setItem('shp-prefs-handed-off', '1');
    return Object.keys(prefs).length ? '#shp-prefs=' + encodeURIComponent(JSON.stringify(prefs)) : '';
  } catch (error) { return ''; }
}
function show(text) { document.getElementById('message').textContent = text; }
async function poll() {
  try {
    const response = await fetch('api/status', {cache: 'no-store'});
    const skin = ((await response.json()).modules || {}).skin || {};
    if (skin.phase === 'running') { location.replace(PAGE + handoff()); return; }
    show(skin.phase === 'stopped'
      ? 'Skin Tools is stopped. Use Start in the Tools tray menu to load it again.' +
        (skin.startup_enabled === false ? ' Load on startup is disabled; a manual Start does not change it.' : '')
      : skin.phase === 'error' || skin.phase === 'unavailable'
        ? 'Skin Tools: ' + (skin.error || skin.phase) + '. Use the Tools tray menu for service controls.'
        : 'Starting Skin Tools\u2026');
  } catch (error) {
    show('Tools is not responding. Reopen SHP Portrait Tools if this does not recover.');
  }
  setTimeout(poll, 1000);
}
poll();
</script></body></html>
"""


def photocraft_environment(config, environ):
    env = dict(environ)
    pc = config.get("photocraft")
    if pc is not None:
        for key, name in (("port", "SHP_PC_PORT"), ("token_file", "SHP_PC_TOKEN_FILE"),
                          ("exchange", "SHP_PC_EXCHANGE"), ("read_root", "SHP_PC_READ_ROOT"),
                          ("executable", "PHOTOCRAFT_BIN"), ("editor_config", "PHOTOCRAFT_CONFIG_DIR")):
            env.setdefault(name, str(pc[key]))
    return env


class SkinWorker:
    """The one supervised service: start, ownership-checked control, status and draining shutdown."""

    name = MODULE

    def __init__(self, config, data, logs, instance, config_path=None):
        self.config, self.data, self.logs, self.instance = config, data, logs, instance
        self.config_path = config_path
        self.preferences_lock = threading.Lock()
        self.lock = threading.Lock()  # one lifecycle action at a time
        self.process = None
        self.process_identity = None
        self.worker_instance = None
        self.token = ""
        self.phase = "stopped"
        self.error = None
        self.finished = threading.Event()
        self.cache_lock = threading.Lock()
        self.cache = {"external": {}}
        data.mkdir(parents=True, exist_ok=True)
        logs.mkdir(parents=True, exist_ok=True)

    @property
    def port(self):
        return self.config["ports"][MODULE]

    @property
    def log_path(self):
        return self.logs / f"{MODULE}.log"

    def alive(self):
        return self.process is not None and self.process.poll() is None

    def expected(self):
        return identity(MODULE, module_folder(self.data, MODULE), self.config["models"])

    def checked(self):
        if not self.alive():
            raise LaunchError(f"{MODULE} is not running. Use Start. See {self.log_path}")
        health = probe(self.port)
        if health is None:
            raise LaunchError(f"{MODULE} is not responding. See {self.log_path}")
        verify(health, self.expected(), self.port)
        if (not owns(self.process, health.get("pid")) or health.get("instance") != self.worker_instance
                or health.get("supervisor") != self.instance or health.get("owner_protocol") != 1):
            raise LaunchError(f"{MODULE} ownership changed. No unrelated process will be controlled.")
        return health

    def owner(self, action, body=None):
        self.checked()
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            connection.request("POST", f"/api/owner/{action}", json.dumps(body or {}),
                               {"Content-Type": "application/json", "X-SHP-Owner": self.token})
            response = connection.getresponse()
            result = json.loads(response.read(16384))
            if response.status != 200:
                details = {key: value for key, value in result.items() if key not in ("error", "code")}
                raise ControlError(result.get("error", "Module control failed"), result.get("code", "unavailable"),
                                   response.status, **details)
            return result
        except (OSError, http.client.HTTPException, ValueError) as error:
            raise LaunchError(f"{MODULE} control connection failed: {error}") from error
        finally:
            connection.close()

    def start(self):
        if self.alive():
            self.checked()
            return
        self.phase, self.error = "starting", None
        try:
            if probe(self.port) is not None:
                raise LaunchError(f"Port {self.port} is already occupied. This supervisor did not start that process; "
                                  "finish its work and stop it yourself, or configure a different port.")
            self.worker_instance, self.token = uuid.uuid4().hex, secrets.token_hex(32)
            env = dict(photocraft_environment(self.config, os.environ),
                       SKIN_DATA=str(module_folder(self.data, MODULE)),
                       SKIN_MODELS=self.config["models"], SHP_SERVER_INSTANCE=self.worker_instance,
                       SHP_SUPERVISOR_INSTANCE=self.instance, SHP_SUPERVISOR_TOKEN=self.token,
                       SHP_SUPERVISOR_PID=str(os.getpid()),
                       SHP_SUPERVISOR_CREATED=str(process_identity(os.getpid())[1]))
            if self.config.get("exiftool"):
                env["SKIN_EXIFTOOL"] = self.config["exiftool"]
            with self.log_path.open("ab") as log:
                self.process = subprocess.Popen(
                    [background_python(), "-u", "-m", SERVER_MODULE, "--port", str(self.port)],
                    env=env, cwd=str(self.data),
                    stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, **spawn_options())
            self.process_identity = adopt(self.process) or process_identity(self.process.pid)
            deadline, failure = time.monotonic() + 40, None
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise LaunchError(f"{MODULE} exited with status {self.process.returncode}. See {self.log_path}")
                try:
                    self.checked()
                    self.phase = "running"
                    return
                except LaunchError as error:
                    failure = error
                time.sleep(0.15)
            raise LaunchError(f"{MODULE} startup timed out: {failure}. See {self.log_path}")
        except (LaunchError, OSError) as error:
            # Only a child that has never been reported ready to the UI is eligible for startup cleanup.
            if self.alive():
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()
            self.phase, self.error = "error", str(error)
            raise LaunchError(str(error)) from error

    def stop(self, confirm=False, revision=None):
        if not self.alive():
            self.phase, self.error = "stopped", None
            return
        self.owner("stop", {"confirm": confirm, "revision": revision})
        self.phase = "stopping"
        try:
            self.process.wait(timeout=8)
        except subprocess.TimeoutExpired as error:
            raise LaunchError(f"{MODULE} is still stopping. No force-kill was sent; inspect its log.") from error
        self.phase, self.error = "stopped", None

    def control(self, action, request):
        if action not in ("start", "stop", "restart", "startup"):
            raise ControlError("Unknown module or action", "invalid", 400)
        if not self.lock.acquire(blocking=False):
            raise ControlError("Another action is already running for this module.", "busy")
        try:
            if self.finished.is_set():
                raise ControlError("Tools is draining; module controls are closed.", "stopping")
            if action == "startup":
                if self.config_path is None:
                    raise LaunchError("Startup preferences are unavailable without a launcher configuration")
                with self.preferences_lock:
                    self.config["startup"] = set_startup(self.config_path, MODULE, request.get("enabled"))
                return self.snapshot()
            if action != "start":
                if request.get("instance") != self.worker_instance:
                    raise ControlError("Module instance changed. Refresh its status and try again.", "changed")
                self.stop(request.get("confirm", False), request.get("revision"))
            if action != "stop":
                self.start()
            return self.snapshot()
        except LaunchError as error:
            if action != "startup":
                self.error = str(error)
            raise
        finally:
            self.lock.release()

    def snapshot(self):
        if self.phase == "stopping" and not self.alive():
            self.phase, self.error = "stopped", None
        result = {"phase": self.phase, "instance": self.worker_instance, "busy": False, "loaded": False,
                  "revision": None, "error": self.error,
                  "startup_enabled": self.config.get("startup", {}).get(MODULE, True)}
        if self.phase == "stopped":
            return result
        if self.phase in ("starting", "stopping"):
            result["busy"] = True
        elif self.alive():
            try:
                result.update(self.owner("status"))
                result["phase"] = "stopping" if result["stopping"] else "running"
            except (LaunchError, ControlError) as error:
                result.update(phase="unavailable", busy=True, error=str(error))
        elif self.phase == "running":
            result.update(phase="error", error=f"Server exited ({self.process.returncode}). See {MODULE}.log")
        return result

    def busy(self):
        return self.snapshot()["busy"]

    def keep_alive(self):
        """Busy work or a browser tab open directly on the worker page keeps the supervisor running."""
        state = self.snapshot()
        return bool(state["busy"] or state.get("ui_clients"))

    def page_url(self):
        return f"http://127.0.0.1:{self.port}/"

    def start_on_launch(self):
        if self.finished.is_set() or not self.config.get("startup", {}).get(MODULE, True):
            return
        try:
            self.control("start", {})
        except (LaunchError, ControlError) as error:
            print(f"{MODULE} startup: {error}", flush=True)

    def monitor(self):
        while not self.finished.is_set():
            status = external_status(photocraft_environment(self.config, os.environ))
            with self.cache_lock:
                self.cache["external"] = status
            self.finished.wait(2)

    def status(self):
        """Shell/tray status. The `modules` mapping keeps its shape so both desktop hosts stay compatible."""
        with self.cache_lock:
            external = self.cache["external"]
        return {"modules": {MODULE: self.snapshot()}, "external": external}

    def close(self, force=False, strict=False):
        self.finished.set()
        if force:
            if not self.lock.acquire(timeout=3):
                raise RuntimeError("Module lifecycle action did not settle; refusing unsafe force restart")
            try:
                return self.force_close()
            finally:
                self.lock.release()
        with self.lock:
            deadline = time.monotonic() + 30
            while True:
                try:
                    self.stop(confirm=True)
                    if strict:
                        require_session_stopped(self.process)
                    return
                except ControlError as error:
                    if error.code == "busy":
                        if strict and time.monotonic() >= deadline:
                            raise RuntimeError(f"{MODULE} remained busy; normal shutdown did not force-stop it") from error
                        time.sleep(.2)
                        continue
                    print(f"{MODULE} shutdown deferred: {error}", flush=True)
                    if strict:
                        raise RuntimeError(f"{MODULE} did not stop safely: {error}") from error
                    return
                except LaunchError as error:
                    # If control is unreachable the child detects parent exit and drains itself.
                    print(f"{MODULE} shutdown deferred: {error}", flush=True)
                    if strict:
                        raise RuntimeError(f"{MODULE} did not stop safely: {error}") from error
                    return

    def force_close(self):
        handles = pin_session(self.process, self.process_identity)

        def graceful():
            if handles:
                try:
                    self.owner("stop", {"confirm": True})
                except (ControlError, LaunchError) as error:
                    print(f"Force restart: {MODULE} graceful stop: {error}", flush=True)
        try:
            drain_pinned(handles, graceful)
            if self.process is not None:
                self.process.wait(timeout=1)
                require_session_stopped(self.process)
            print(f"Force restart: {MODULE} stopped", flush=True)
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
            raise RuntimeError(f"{MODULE}: {error}") from error


def external_status(environ=None):
    """TCP reachability only: no authentication, document access or service management."""
    # PhotoCraft settings keep the pre-rename DNB/dnb namespace (PhotoCraft is configured against it).
    targets = {"photocraft_skin": lambda: ("127.0.0.1", detect_config("DNB", "dnb", environ)["port"])}
    result = {}
    probes = {}
    for name, address in targets.items():
        target = "invalid configuration"
        try:
            host, port = address()
            target = f"{host}:{port}"
            if target not in probes:
                try:
                    with socket.create_connection((host, port), timeout=0.3):
                        pass
                    probes[target] = {"reachable": True, "address": target}
                except OSError as error:
                    probes[target] = {"reachable": False, "address": target, "error": str(error)}
            result[name] = dict(probes[target])
        except (OSError, ValueError, OverflowError) as error:
            result[name] = {"reachable": False, "address": target, "error": str(error)}
    return result


class ShellServer(BrowserServer):
    def __init__(self, address, worker, **kwargs):
        self.worker = worker
        self.force_shutdown = False
        self.native_cleaned = False
        self.shutdown_error = None
        self.drain_client = None
        self.drain_thread = None
        self.shutdown_lock = threading.Lock()
        super().__init__(address, ShellHandler, close_with_ui=True, busy=worker.keep_alive, **kwargs)

    def native_drain(self):
        try:
            print(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} stopping owned services", flush=True)
            self.worker.close(force=self.force_shutdown, strict=True)
            self.native_cleaned = True
            print(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} owned services stopped", flush=True)
            self.shutdown()
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            self.shutdown_error = str(error)
            print(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} shutdown failed: {error}", flush=True)

    def monitor(self):
        # Native drain owns cleanup once acknowledged; don't race the browser grace monitor.
        while not self.finished.wait(1):
            with self.lifetime.lock:
                draining = self.lifetime.stopping
            if draining:
                continue
            if self.lifetime.should_stop(self.busy):
                print(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} Last UI tab closed; stopping server.", flush=True)
                self.shutdown()
                return


class ShellHandler(BrowserHandler, BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        if len(args) > 1 and str(args[1]).startswith("5"):
            super().log_message(fmt, *args)

    @property
    def origin(self):
        return f"http://127.0.0.1:{self.server.server_port}"

    def send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("Content-Security-Policy", "frame-ancestors 'self'")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # A closing client aborts its pending HTTP response.

    def fail(self, error):
        if isinstance(error, ControlError):
            self.send(error.status, json.dumps({"error": str(error), "code": error.code,
                                               **error.details}).encode(), "application/json")
        else:
            self.send(503, json.dumps({"error": str(error), "code": "unavailable"}).encode(), "application/json")

    def valid_host(self):
        if self.headers.get("Host") != f"127.0.0.1:{self.server.server_port}":
            self.send(403, b'{"error":"Use the launcher loopback URL"}', "application/json")
            return False
        return True

    def do_GET(self):
        if not self.valid_host():
            return
        path = urlsplit(self.path).path
        if path == "/api/health":
            worker = self.server.worker
            result = {**identity("shell", worker.data, worker.config["models"]), "shell_protocol": SHELL_PROTOCOL,
                      "ports": worker.config["ports"], "exiftool": worker.config.get("exiftool")}
            result.update(draining=self.server.lifetime.stopping, shutdown_error=self.server.shutdown_error,
                          process_identity=process_identity(os.getpid()))
            return self.send(200, json.dumps(result).encode(), "application/json")
        if path == "/api/status":
            result = self.server.worker.status()
            with self.server.lifetime.lock:
                result["lifecycle"] = {"draining": self.server.lifetime.stopping,
                    "leases": sum(expiry > time.monotonic() for expiry in self.server.lifetime.clients.values())}
            return self.send(200, json.dumps(result).encode(), "application/json")
        if path == "/":
            page = STATUS_PAGE.replace("__PAGE__", json.dumps(self.server.worker.page_url()))
            return self.send(200, page.encode(), "text/html; charset=utf-8")
        self.send(404, b'{"error":"Not found"}', "application/json")

    def do_POST(self):
        if not self.valid_host():
            return
        if (self.headers.get("Origin") != self.origin
                or self.headers.get("Content-Type", "").split(";")[0] != "application/json"
                or self.headers.get("Transfer-Encoding")):
            return self.send(403, b'{"error":"Same-origin JSON requests required"}', "application/json")
        try:
            length = int(self.headers.get("Content-Length", 0))
            if not 0 <= length <= 1024 * 1024:
                raise ValueError("Request body exceeds limit")
        except ValueError as error:
            return self.send(400, json.dumps({"error": str(error)}).encode(), "application/json")
        if self.path == "/api/ui/native-quit":
            if not self.server.shutdown_lock.acquire(blocking=False):
                return self.send(409, b'{"error":"Shutdown request already pending"}', "application/json")
            try:
                req = json.loads(self.rfile.read(length))
                if (self.headers.get("X-SHP-Control") != "1" or not isinstance(req, dict)
                        or not isinstance(req.get("client"), str)
                        or any(type(req.get(key, False)) is not bool for key in ("restart", "force", "confirm"))):
                    raise ValueError("Invalid native shutdown request")
                if not req.get("confirm"):
                    raise ValueError("Explicit shutdown confirmation required")
                if req.get("force") and not req.get("restart"):
                    raise ValueError("Force is available only for explicitly confirmed restart")
                def safe_to_stop():
                    state = self.server.worker.snapshot()
                    signature = {MODULE: [state.get("instance"), state.get("revision"), state.get("loaded")]}
                    if req.get("expected") != signature:
                        raise ControlError("Work changed while confirming shutdown; review it and try again.", "changed")
                    return state["busy"]
                if (req.get("force") and self.server.shutdown_error
                        and self.server.drain_client == req["client"]
                        and self.server.drain_thread is not None and not self.server.drain_thread.is_alive()):
                    result = {"draining": True, "others": len(self.server.lifetime.clients)}
                    self.server.shutdown_error = None
                else:
                    result = self.server.lifetime.native_quit(req["client"], safe_to_stop,
                        restart=req.get("restart", False), force=req.get("force", False))
                if result["draining"]:
                    self.server.drain_client = req["client"]
                    self.server.force_shutdown = req.get("force", False)
                    if self.server.force_shutdown:
                        self.server.daemon_threads = True
                        self.server.block_on_close = False
                print(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} native shutdown: "
                      f"draining={result['draining']} other_leases={result['others']} "
                      f"restart={req.get('restart', False)} force={req.get('force', False)}", flush=True)
                self.send(200, json.dumps({**result, "pid": os.getpid(),
                    "process_identity": process_identity(os.getpid())}).encode(), "application/json")
                if result["draining"]:
                    self.server.drain_thread = threading.Thread(target=self.server.native_drain, daemon=True)
                    self.server.drain_thread.start()
                return
            except ControlError as error:
                return self.fail(error)
            except (OSError, ValueError, TypeError) as error:
                return self.send(400, json.dumps({"error": str(error)}).encode(), "application/json")
            finally:
                self.server.shutdown_lock.release()
        super().do_POST()

    def do_api_POST(self):
        if self.path.startswith("/api/modules/"):
            if self.headers.get("X-SHP-Control") != "1":
                return self.send(403, b'{"error":"Control header required"}', "application/json")
            try:
                if self.server.lifetime.stopping:
                    raise ControlError("Tools is draining; module controls are closed.", "stopping")
                parts = self.path.split("/")
                if len(parts) != 5:
                    raise ControlError("Invalid module control path", "invalid", 400)
                request = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                if (not isinstance(request, dict) or type(request.get("confirm", False)) is not bool
                        or (request.get("revision") is not None and type(request["revision"]) is not int)):
                    raise ValueError("Invalid module confirmation")
                if parts[3] != MODULE:
                    raise ControlError("Unknown module or action", "invalid", 400)
                result = self.server.worker.control(parts[4], request)
                return self.send(200, json.dumps(result).encode(), "application/json")
            except (LaunchError, ControlError) as error:
                return self.fail(error)
            except OSError as error:
                return self.fail(LaunchError(f"Could not save module startup preference: {error}"))
            except (ValueError, TypeError) as error:
                return self.send(400, json.dumps({"error": str(error)}).encode(), "application/json")
        self.send(404, b'{"error":"Not found"}', "application/json")


def main():
    def interrupt(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupt)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    args = parser.parse_args()
    config = read_config(args.config)
    apply_photocraft_env(config)
    instance = os.environ.get("SHP_SERVER_INSTANCE", uuid.uuid4().hex)
    worker = SkinWorker(config, args.data, args.state, instance, args.config)
    server = ShellServer(("127.0.0.1", config["shell_port"]), worker)
    threads = [threading.Thread(target=worker.start_on_launch), threading.Thread(target=worker.monitor, daemon=True)]
    for thread in threads:
        thread.start()
    print(f"SHP Portrait Tools: http://127.0.0.1:{server.server_port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Closing Tools; active module work will finish before shutdown.", flush=True)
    finally:
        if not server.native_cleaned:
            worker.close(force=server.force_shutdown)
        server.server_close()
        for thread in threads:
            thread.join(timeout=10)


if __name__ == "__main__":
    main()
