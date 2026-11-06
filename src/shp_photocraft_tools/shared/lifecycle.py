"""Browser leases for launcher-owned servers, independent of the Firefox process."""

import contextlib
import json
import os
import threading
import time
from http.server import ThreadingHTTPServer
from importlib.resources import files

from .supervision import ControlError, OwnerControl, handle_owner, parent_alive

PROTOCOL = 1


class BrowserLifetime:
    def __init__(self, *, clock=time.monotonic, lease=90, grace=15, startup=120):
        self.clock, self.lease, self.grace = clock, lease, grace
        self.lock = threading.Lock()
        self.clients = {}
        self.deadline = clock() + startup
        self.active = 0
        self.stopping = False

    def touch(self, client):
        with self.lock:
            if self.stopping:
                return False
            self.clients[client] = self.clock() + self.lease
            return True

    def close(self, client):
        with self.lock:
            if client in self.clients:
                del self.clients[client]
                if not self.clients:
                    self.deadline = self.clock() + self.grace

    def reserve(self):
        with self.lock:
            if self.stopping:
                return False
            self.deadline = self.clock() + 120
            return True

    def native_quit(self, client, busy, *, restart=False, force=False):
        with self.lock:
            if self.stopping:
                raise ControlError("Tools is already draining; wait for shutdown.", "stopping")
            now = self.clock()
            clients = {key: expiry for key, expiry in self.clients.items() if expiry > now}
            if client not in clients or not client.startswith("desktop-"):
                raise ControlError("Desktop lease expired; reconnect before quitting.", "lease", 409)
            others = len(clients) - 1
            if others and restart and not force:
                raise ControlError(f"{others} other Tools window(s) still connected. Close them before restarting.",
                                   "other_windows", others=others)
            if not force and (self.active or busy()):
                raise ControlError("Tools is busy. Finish/cancel work before quitting or restarting.", "busy")
            self.clients = clients
            del self.clients[client]
            if others and not force:
                return {"draining": False, "others": others}
            self.stopping = True
            return {"draining": True, "others": others}

    @contextlib.contextmanager
    def work(self):
        with self.lock:
            if self.stopping:
                raise RuntimeError("Server is shutting down; reopen SHP PhotoCraft Tools")
            self.active += 1
        try:
            yield
        finally:
            with self.lock:
                self.active -= 1

    def should_stop(self, busy):
        with self.lock:
            now = self.clock()
            expired = [client for client, deadline in self.clients.items() if deadline <= now]
            for client in expired:
                del self.clients[client]
            if expired and not self.clients:
                self.deadline = max(self.deadline, now + self.grace)
            if self.clients or now < self.deadline or self.active or busy():
                return False
            self.stopping = True
            return True


class BrowserServer(ThreadingHTTPServer):
    def __init__(self, *args, close_with_ui=False, busy=lambda: False, loaded=lambda: False, **kwargs):
        super().__init__(*args, **kwargs)
        token = os.environ.get("SHP_SUPERVISOR_TOKEN")
        self.owner = OwnerControl(token, busy=busy, loaded=loaded) if token else None
        self.lifetime = BrowserLifetime() if close_with_ui and not self.owner else None
        self.busy = busy
        self.finished = threading.Event()

    def serve_forever(self, poll_interval=0.5):
        if self.lifetime or self.owner:
            threading.Thread(target=self.monitor, daemon=True).start()
        try:
            super().serve_forever(poll_interval)
        finally:
            self.finished.set()

    def monitor(self):
        while not self.finished.wait(1):
            if self.owner:
                if parent_alive():
                    continue
                try:
                    self.owner.prepare_stop(confirm=True)
                except ControlError:
                    continue  # An orphan finishes active work before exiting.
            elif not self.lifetime.should_stop(self.busy):
                continue
            if self.owner or self.lifetime:
                print("Last UI tab closed; stopping server.", flush=True)
                self.shutdown()
                return

    def server_close(self):
        self.finished.set()
        super().server_close()


def script():
    return files("shp_photocraft_tools.shared.ui").joinpath("lifecycle.js").read_bytes()


class BrowserHandler:
    """Keep long-poll heartbeats outside module locks and processing accounting."""

    def do_POST(self):
        lifetime = getattr(self.server, "lifetime", None)
        owner = getattr(self.server, "owner", None)
        if self.path.startswith("/api/owner/"):
            return handle_owner(self)
        if self.path.startswith("/api/ui/"):
            try:
                if self.path == "/api/ui/reserve":
                    accepted = lifetime.reserve() if lifetime else True
                elif self.path in ("/api/ui/heartbeat", "/api/ui/close"):
                    length = int(self.headers.get("Content-Length", 0))
                    if not 0 < length <= 1024:
                        raise ValueError("Invalid UI heartbeat size")
                    client = json.loads(self.rfile.read(length)).get("client")
                    if not isinstance(client, str) or not 1 <= len(client) <= 100:
                        raise ValueError("Invalid UI tab identity")
                    if self.path.endswith("/close"):
                        if lifetime:
                            lifetime.close(client)
                        accepted = True
                    else:
                        accepted = lifetime.touch(client) if lifetime else True
                        if accepted and lifetime and not client.startswith("desktop-"):
                            self.server.finished.wait(20)
                else:
                    return self.send(404, b"not found", "text/plain")
                return self.send(200 if accepted else 409, json.dumps({"managed": lifetime is not None,
                                                                     "accepted": accepted}).encode(), "application/json")
            except (ValueError, TypeError, AttributeError) as error:
                return self.send(400, json.dumps({"error": str(error)}).encode(), "application/json")
            except (BrokenPipeError, ConnectionResetError):
                return  # A tab close/refresh normally aborts its pending heartbeat response.
        try:
            if owner:
                with owner.work(self.path):
                    self.do_api_POST()
            elif lifetime:
                with lifetime.work():
                    self.do_api_POST()
            else:
                self.do_api_POST()
        except ControlError as error:
            self.send(error.status, json.dumps({"error": str(error), "code": error.code}).encode(), "application/json")
        except RuntimeError as error:
            self.send(503, json.dumps({"error": str(error)}).encode(), "application/json")
