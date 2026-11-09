"""Loopback shell, fixed module proxy and capability-guarded child supervision."""

import argparse
import contextlib
import http.client
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler
from importlib.resources import files
from pathlib import Path
from urllib.parse import urlsplit

from .launcher import MODULES, LaunchError, probe, read_config, verify, set_startup
from .shared.health import identity
from .shared.lifecycle import BrowserHandler, BrowserServer, script
from .shared.owned_process import process_identity, pin_session, drain_pinned, require_session_stopped
from .shared.photocraft.bridge import detect_config
from .shared.supervision import ControlError
from .shared.ui import stylesheet, theme_script
from .comfy import ManagedComfy

SHELL_PROTOCOL = 1


@dataclass
class Module:
    process: subprocess.Popen | None = None
    instance: str | None = None
    token: str = ""
    phase: str = "stopped"
    error: str | None = None
    process_identity: tuple | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)


class Modules:
    def __init__(self, config, data, logs, instance, config_path=None):
        self.config, self.data, self.logs, self.instance = config, data, logs, instance
        self.config_path = config_path
        self.preferences_lock = threading.Lock()
        self.modules = {name: Module() for name in MODULES}
        self.finished = threading.Event()
        self.cache_lock = threading.Lock()
        self.cache = {"modules": {name: {"phase": "starting"} for name in MODULES}, "external": {}}
        data.mkdir(parents=True, exist_ok=True)
        logs.mkdir(parents=True, exist_ok=True)
        self.comfy = ManagedComfy(config["comfy"], logs) if config.get("comfy") else None

    def expected(self, name):
        return identity(name, self.data / name, self.config["models"] if name == "dnb" else None)

    def checked(self, name):
        module = self.modules[name]
        if module.process is None or module.process.poll() is not None:
            raise LaunchError(f"{name} is not running. Use Start. See {self.logs / (name + '.log')}")
        health = probe(self.config["ports"][name])
        if health is None:
            raise LaunchError(f"{name} is not responding. See {self.logs / (name + '.log')}")
        verify(health, self.expected(name), self.config["ports"][name])
        if (health.get("pid") != module.process.pid or health.get("instance") != module.instance
                or health.get("supervisor") != self.instance or health.get("owner_protocol") != 1):
            raise LaunchError(f"{name} ownership changed. No unrelated process will be controlled.")
        return health

    def owner(self, name, action, body=None):
        self.checked(name)
        connection = http.client.HTTPConnection("127.0.0.1", self.config["ports"][name], timeout=3)
        try:
            connection.request("POST", f"/api/owner/{action}", json.dumps(body or {}),
                               {"Content-Type": "application/json", "X-SHP-Owner": self.modules[name].token})
            response = connection.getresponse()
            result = json.loads(response.read(16384))
            if response.status != 200:
                details = {key: value for key, value in result.items() if key not in ("error", "code")}
                raise ControlError(result.get("error", "Module control failed"), result.get("code", "unavailable"),
                                   response.status, **details)
            return result
        except (OSError, http.client.HTTPException, ValueError) as error:
            raise LaunchError(f"{name} control connection failed: {error}") from error
        finally:
            connection.close()

    def start(self, name):
        module = self.modules[name]
        if module.process is not None and module.process.poll() is None:
            self.checked(name)
            return
        module.phase, module.error = "starting", None
        if name == "crop" and self.comfy:
            try:
                self.comfy.start()
            except OSError as error:
                print(f"Tools ComfyUI startup failed: {error}", flush=True)
        port = self.config["ports"][name]
        try:
            if probe(port) is not None:
                raise LaunchError(f"Port {port} is already occupied. This supervisor did not start that process; "
                                  "finish its work and stop it yourself, or configure a different port.")
            module.instance, module.token = uuid.uuid4().hex, secrets.token_hex(32)
            env = dict(os.environ, SHPC_DATA=str(self.data / "crop"), DNB_DATA=str(self.data / "dnb"),
                       DNB_MODELS=self.config["models"], SHP_SERVER_INSTANCE=module.instance,
                       SHP_SUPERVISOR_INSTANCE=self.instance, SHP_SUPERVISOR_TOKEN=module.token,
                       SHP_SUPERVISOR_PID=str(os.getpid()))
            if self.config.get("exiftool"):
                env["DNB_EXIFTOOL"] = self.config["exiftool"]
            if self.comfy:
                env["SHPC_COMFY"] = self.comfy.url
                env["SHPC_TOOLS_COMFY"] = "1"
            log_path = self.logs / f"{name}.log"
            with log_path.open("ab") as log:
                module.process = subprocess.Popen(
                    [sys.executable, "-u", "-m", "shp_photocraft_tools.modules." + MODULES[name],
                     "--port", str(port)], env=env, cwd=str(self.data),
                    stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
            module.process_identity = process_identity(module.process.pid)
            deadline, failure = time.monotonic() + 40, None
            while time.monotonic() < deadline:
                if module.process.poll() is not None:
                    raise LaunchError(f"{name} exited with status {module.process.returncode}. See {log_path}")
                try:
                    self.checked(name)
                    module.phase = "running"
                    return
                except LaunchError as error:
                    failure = error
                time.sleep(0.15)
            raise LaunchError(f"{name} startup timed out: {failure}. See {log_path}")
        except (LaunchError, OSError) as error:
            # Only a child that has never been exposed through the proxy is eligible for startup cleanup.
            if module.process is not None and module.process.poll() is None:
                module.process.terminate()
                try:
                    module.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    module.process.kill()
                    module.process.wait()
            module.phase, module.error = "error", str(error)
            raise LaunchError(str(error)) from error

    def stop(self, name, confirm=False, revision=None):
        module = self.modules[name]
        if module.process is None or module.process.poll() is not None:
            module.phase, module.error = "stopped", None
            return
        self.owner(name, "stop", {"confirm": confirm, "revision": revision})
        module.phase = "stopping"
        try:
            module.process.wait(timeout=8)
        except subprocess.TimeoutExpired as error:
            raise LaunchError(f"{name} is still stopping. No force-kill was sent; inspect its log.") from error
        module.phase, module.error = "stopped", None

    def control(self, name, action, request):
        if name not in MODULES or action not in ("start", "stop", "restart", "startup"):
            raise ControlError("Unknown module or action", "invalid", 400)
        module = self.modules[name]
        if not module.lock.acquire(blocking=False):
            raise ControlError("Another action is already running for this module.", "busy")
        try:
            if self.finished.is_set():
                raise ControlError("Tools is draining; module controls are closed.", "stopping")
            if action == "startup":
                if self.config_path is None:
                    raise LaunchError("Startup preferences are unavailable without a launcher configuration")
                with self.preferences_lock:
                    startup = set_startup(self.config_path, name, request.get("enabled"))
                    self.config["startup"] = startup
                return self.snapshot(name)
            if action != "start":
                if request.get("instance") != module.instance:
                    raise ControlError("Module instance changed. Refresh its status and try again.", "changed")
                self.stop(name, request.get("confirm", False), request.get("revision"))
            if action != "stop":
                self.start(name)
            return self.snapshot(name)
        except LaunchError as error:
            if action != "startup":
                module.error = str(error)
            raise
        finally:
            module.lock.release()

    def snapshot(self, name):
        module = self.modules[name]
        if module.phase == "stopping" and (module.process is None or module.process.poll() is not None):
            module.phase, module.error = "stopped", None
        result = {"phase": module.phase, "instance": module.instance, "busy": False, "loaded": False,
                  "revision": None, "error": module.error,
                  "startup_enabled": self.config.get("startup", {}).get(name, True)}
        if module.phase == "stopped":
            return result
        if module.phase in ("starting", "stopping"):
            result["busy"] = True
        elif module.process is not None and module.process.poll() is None:
            try:
                result.update(self.owner(name, "status"))
                result["phase"] = "stopping" if result["stopping"] else "running"
            except (LaunchError, ControlError) as error:
                result.update(phase="unavailable", busy=True, error=str(error))
        elif module.phase == "running":
            result.update(phase="error", error=f"Server exited ({module.process.returncode}). See {name}.log")
        return result

    def busy(self):
        return (any(self.snapshot(name)["busy"] for name in MODULES)
                or bool(self.comfy and self.comfy.status().get("busy")))

    def start_all(self):
        for name in MODULES:
            if self.finished.is_set():
                return
            if not self.config.get("startup", {}).get(name, True):
                continue
            try:
                self.control(name, "start", {})
            except (LaunchError, ControlError) as error:
                print(f"{name} startup: {error}", flush=True)

    def monitor(self):
        while not self.finished.is_set():
            status = external_status(self.comfy.url if self.comfy else None)
            if self.comfy:
                state = self.comfy.status()
                status["comfy"].update(state)
            with self.cache_lock:
                self.cache["external"] = status
            self.finished.wait(2)

    def status(self):
        with self.cache_lock:
            external = self.cache["external"]
        return {"modules": {name: self.snapshot(name) for name in MODULES}, "external": external}

    def close(self, force=False, strict=False):
        self.finished.set()
        if force:
            with contextlib.ExitStack() as stack:
                locks = [module.lock for module in self.modules.values()]
                if self.comfy:
                    locks.append(self.comfy.lock)
                for lock in locks:
                    if not lock.acquire(timeout=3):
                        raise RuntimeError("Module lifecycle action did not settle; refusing unsafe force restart")
                    stack.callback(lock.release)
                if self.comfy:
                    self.comfy.closed = True
                return self.force_close()
        for name, module in self.modules.items():
            with module.lock:
                deadline = time.monotonic() + 30
                while True:
                    try:
                        self.stop(name, confirm=True)
                        if strict:
                            require_session_stopped(module.process)
                        break
                    except ControlError as error:
                        if error.code == "busy":
                            if strict and time.monotonic() >= deadline:
                                raise RuntimeError(f"{name} remained busy; normal shutdown did not force-stop it") from error
                            time.sleep(.2)
                            continue
                        print(f"{name} shutdown deferred: {error}", flush=True)
                        if strict:
                            raise RuntimeError(f"{name} did not stop safely: {error}") from error
                        break
                    except LaunchError as error:
                        # If control is unreachable the child detects parent exit and drains itself.
                        print(f"{name} shutdown deferred: {error}", flush=True)
                        if strict:
                            raise RuntimeError(f"{name} did not stop safely: {error}") from error
                        break
        if self.comfy:
            self.comfy.close(timeout=30 if strict else None)
            if strict:
                require_session_stopped(self.comfy.process)

    def force_close(self):
        targets = [(name, module.process, module.process_identity) for name, module in self.modules.items()]
        if self.comfy:
            targets.append(("comfy", self.comfy.process, self.comfy.process_identity))
        pinned = []
        try:
            for name, process, expected in targets:
                pinned.append((name, process, pin_session(process, expected)))
        except BaseException:
            for _, _, handles in pinned:
                for fd in handles:
                    os.close(fd)
            raise
        errors = []
        for name, process, handles in pinned:
            def graceful():
                if handles and name != "comfy":
                    try:
                        self.owner(name, "stop", {"confirm": True})
                    except (ControlError, LaunchError) as error:
                        print(f"Force restart: {name} graceful stop: {error}", flush=True)
            try:
                drain_pinned(handles, graceful)
                if process is not None:
                    process.wait(timeout=1)
                    require_session_stopped(process)
                print(f"Force restart: {name} stopped", flush=True)
            except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
                errors.append(f"{name}: {error}")
        if errors:
            raise RuntimeError("; ".join(errors))


def external_status(comfy_url=None):
    """TCP reachability only: no authentication, document access or service management."""
    def comfy_address():
        url = urlsplit(comfy_url or os.environ.get("SHPC_COMFY", "http://127.0.0.1:8188"))
        if url.scheme not in ("http", "https") or not url.hostname:
            raise ValueError("SHPC_COMFY must be an HTTP(S) URL with a hostname")
        return url.hostname, url.port or (443 if url.scheme == "https" else 80)

    targets = {"photocraft_crop": lambda: ("127.0.0.1", detect_config("SHPC", "crop")["port"]),
               "photocraft_dnb": lambda: ("127.0.0.1", detect_config("DNB", "dnb")["port"]),
               "comfy": comfy_address}
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
    def __init__(self, address, modules, **kwargs):
        self.modules = modules
        self.force_shutdown = False
        self.native_cleaned = False
        self.shutdown_error = None
        self.drain_client = None
        self.drain_thread = None
        self.shutdown_lock = threading.Lock()
        super().__init__(address, ShellHandler, close_with_ui=True, busy=modules.busy, **kwargs)

    def native_drain(self):
        try:
            print(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} stopping owned services", flush=True)
            self.modules.close(force=self.force_shutdown, strict=True)
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
            pass  # Closing a shell/iframe aborts its pending HTTP responses.

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
            modules = self.server.modules
            result = {**identity("shell", modules.data, modules.config["models"]), "shell_protocol": SHELL_PROTOCOL,
                      "ports": modules.config["ports"], "exiftool": modules.config.get("exiftool"),
                      "comfy": modules.config.get("comfy")}
            result.update(draining=self.server.lifetime.stopping, shutdown_error=self.server.shutdown_error,
                          process_identity=process_identity(os.getpid()))
            return self.send(200, json.dumps(result).encode(), "application/json")
        if path == "/api/status":
            result = self.server.modules.status()
            with self.server.lifetime.lock:
                result["lifecycle"] = {"draining": self.server.lifetime.stopping,
                    "leases": sum(expiry > time.monotonic() for expiry in self.server.lifetime.clients.values())}
            return self.send(200, json.dumps(result).encode(), "application/json")
        resources = {"/": ("index.html", "text/html; charset=utf-8"),
                     "/static/shell.js": ("shell.js", "text/javascript; charset=utf-8"),
                     "/static/shell.css": ("shell.css", "text/css; charset=utf-8")}
        if path in resources:
            name, mime = resources[path]
            return self.send(200, files("shp_photocraft_tools.shell").joinpath(name).read_bytes(), mime)
        if path == "/static/shp-ui.css":
            return self.send(200, stylesheet(), "text/css; charset=utf-8")
        if path == "/static/theme.js":
            return self.send(200, theme_script(), "text/javascript; charset=utf-8")
        if path == "/static/lifecycle.js":
            return self.send(200, script(), "text/javascript; charset=utf-8")
        if path in ("/crop", "/dnb"):
            self.send_response(308)
            self.send_header("Location", path + "/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.proxy()

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
                    status = self.server.modules.status()
                    signature = {name: [state.get("instance"), state.get("revision"), state.get("loaded")]
                                 for name, state in status["modules"].items()}
                    if req.get("expected") != signature:
                        raise ControlError("Work changed while confirming shutdown; review it and try again.", "changed")
                    return self.server.modules.busy()
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
                result = self.server.modules.control(parts[3], parts[4], request)
                return self.send(200, json.dumps(result).encode(), "application/json")
            except (LaunchError, ControlError) as error:
                return self.fail(error)
            except OSError as error:
                return self.fail(LaunchError(f"Could not save module startup preference: {error}"))
            except (ValueError, TypeError) as error:
                return self.send(400, json.dumps({"error": str(error)}).encode(), "application/json")
        self.proxy()

    def proxy(self):
        parts = self.path.split("/", 2)
        if len(parts) != 3 or parts[1] not in MODULES:
            return self.send(404, b'{"error":"Not found"}', "application/json")
        name, path = parts[1], "/" + parts[2]
        if path.startswith("/api/owner/") or "%" in path or ".." in urlsplit(path).path or path.startswith("//"):
            return self.send(403, b'{"error":"Private or invalid module route"}', "application/json")
        if not (path == "/" or path == "/index.html" or path.startswith(("/api/", "/img/", "/static/"))):
            return self.send(404, b'{"error":"Not found"}', "application/json")
        modules = self.server.modules
        connection = None
        try:
            phase = modules.modules[name].phase
            if phase in ("stopped", "stopping"):
                if self.command == "POST" and path in ("/api/ui/close", "/api/ui/heartbeat"):
                    return self.send(200, b'{"managed":false,"accepted":true}', "application/json")
                raise ControlError(f"{name} is {phase}. Use its Start control when stopped.",
                                   "module_" + phase, 409, module=name, phase=phase)
            if modules.comfy and path == "/api/comfy/start" and name == "crop" and self.command == "POST":
                return self.send(200, json.dumps(modules.comfy.start()).encode(), "application/json")
            if modules.comfy and path == "/api/run" and name == "crop" and self.command == "POST":
                if modules.comfy.status()["phase"] != "running":
                    raise LaunchError("Tools ComfyUI is not ready; check the tray status and comfy.log.")
            if modules.modules[name].phase != "running":
                raise LaunchError(f"{name} is not ready. Use its Start control.")
            modules.checked(name)
            connection = http.client.HTTPConnection("127.0.0.1", modules.config["ports"][name],
                                                   timeout=None if name == "dnb" and path == "/api/batch" else 900)
            body = self.rfile.read(int(self.headers.get("Content-Length", 0))) if self.command == "POST" else None
            connection.request(self.command, path, body, {"Content-Type": "application/json",
                               "Origin": f"http://127.0.0.1:{modules.config['ports'][name]}"})
            response = connection.getresponse()
            content = response.read()
            self.send(response.status, content, response.getheader("Content-Type", "application/octet-stream"))
        except ControlError as error:
            self.fail(error)
        except (LaunchError, OSError, http.client.HTTPException) as error:
            self.fail(LaunchError(f"{name}: {error}"))
        finally:
            if connection:
                connection.close()


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
    instance = os.environ.get("SHP_SERVER_INSTANCE", uuid.uuid4().hex)
    modules = Modules(config, args.data, args.state, instance, args.config)
    server = ShellServer(("127.0.0.1", config["shell_port"]), modules)
    threads = [threading.Thread(target=modules.start_all), threading.Thread(target=modules.monitor, daemon=True)]
    for thread in threads:
        thread.start()
    print(f"SHP PhotoCraft Tools: http://127.0.0.1:{server.server_port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Closing Tools; active module work will finish before shutdown.", flush=True)
    finally:
        if not server.native_cleaned:
            modules.close(force=server.force_shutdown)
        server.server_close()
        for thread in threads:
            thread.join(timeout=10)


if __name__ == "__main__":
    main()
