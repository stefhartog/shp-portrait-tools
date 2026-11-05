"""Private, atomic work/stop gate for supervisor-owned module processes."""

import contextlib
import hmac
import json
import os
import threading


class ControlError(RuntimeError):
    def __init__(self, message, code="busy", status=409, **details):
        super().__init__(message)
        self.code, self.status, self.details = code, status, details


class OwnerControl:
    READ_ONLY = {"/api/job", "/api/status", "/api/pc/status", "/api/profile", "/api/tone", "/api/presets"}

    def __init__(self, token, *, busy, loaded):
        self.token, self.busy, self.loaded = token, busy, loaded
        self.lock = threading.RLock()
        self.active = self.revision = 0
        self.stopping = False

    def snapshot(self):
        with self.lock:
            return {"busy": bool(self.active or self.busy()), "loaded": bool(self.loaded()),
                    "revision": self.revision, "stopping": self.stopping}

    @contextlib.contextmanager
    def work(self, path):
        with self.lock:
            if self.stopping:
                raise ControlError("Module is stopping; start it again from Tools.", "stopping")
            self.active += 1
            if path not in self.READ_ONLY:
                self.revision += 1
        try:
            yield
        finally:
            with self.lock:
                self.active -= 1

    def prepare_stop(self, confirm=False, revision=None):
        with self.lock:
            state = self.snapshot()
            if state["busy"]:
                raise ControlError("Module is busy. Finish or cancel its work first.", **state)
            if revision is not None and revision != self.revision:
                raise ControlError("Module work changed. Review the current work before confirming again.",
                                   "changed", **state)
            if state["loaded"] and not confirm:
                raise ControlError("Loaded images and unsent previews will be lost. Saved settings and results remain.",
                                   "confirmation", **state)
            self.stopping = True


def handle_owner(handler):
    """Only the owning supervisor knows this per-process capability; never proxy it."""
    owner = handler.server.owner
    supplied = handler.headers.get("X-SHP-Owner", "")
    if owner is None or not hmac.compare_digest(supplied, owner.token):
        handler.send(403, b'{"error":"Supervisor capability required"}', "application/json")
        return
    try:
        if handler.path == "/api/owner/status":
            result = owner.snapshot()
        elif handler.path == "/api/owner/stop":
            length = int(handler.headers.get("Content-Length", 0))
            if not 0 < length <= 2048:
                raise ValueError("Invalid control body")
            request = json.loads(handler.rfile.read(length))
            if not isinstance(request, dict) or type(request.get("confirm", False)) is not bool:
                raise ValueError("Invalid stop confirmation")
            owner.prepare_stop(request.get("confirm", False), request.get("revision"))
            result = {"stopping": True}
        else:
            handler.send(404, b'{"error":"Unknown owner action"}', "application/json")
            return
        handler.send(200, json.dumps(result).encode(), "application/json")
        if handler.path == "/api/owner/stop":
            threading.Thread(target=handler.server.shutdown, daemon=True).start()
    except ControlError as error:
        handler.send(error.status, json.dumps({"error": str(error), "code": error.code,
                                               **error.details}).encode(), "application/json")
    except (ValueError, TypeError) as error:
        handler.send(400, json.dumps({"error": str(error)}).encode(), "application/json")


def parent_alive():
    return os.getppid() == int(os.environ["SHP_SUPERVISOR_PID"])
