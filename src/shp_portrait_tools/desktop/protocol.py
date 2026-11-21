"""Desktop-neutral pieces of the shell protocol shared by native hosts (no GTK/WebView imports)."""

import json
import threading
import uuid
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

OPEN_PROMPTS = ("SHP_OPEN_IMAGE_V1", "SHP_OPEN_BATCH_FOLDER_V1", "SHP_OPEN_BATCH_IMAGES_V1", "SHP_OPEN_BATCH_SOURCE_V1")
IMAGE_EXTENSIONS = ("jpg", "jpeg", "png", "tif", "tiff", "webp")


def validate_url(url):
    parts = urlsplit(url)
    if (parts.scheme != "http" or parts.hostname != "127.0.0.1" or not parts.port
            or parts.username or parts.password or parts.path != "/" or parts.query or parts.fragment):
        raise ValueError("Desktop URL must be http://127.0.0.1:<port>/")
    return url.rstrip("/")


class Connection:
    def __init__(self, base):
        self.base = base
        self.client = "desktop-" + uuid.uuid4().hex
        self.opener = build_opener(ProxyHandler({}))
        self.stopped = threading.Event()
        self.lease_lock = threading.Lock()
        self.error = None

    def request(self, path, body=None, timeout=5):
        headers = {"Origin": self.base, "Content-Type": "application/json"}
        if path == "/api/ui/native-quit":
            headers["X-SHP-Control"] = "1"
        request = Request(self.base + path, json.dumps(body).encode() if body is not None else None, headers)
        try:
            with self.opener.open(request, timeout=timeout) as response:
                return json.load(response)
        except HTTPError as error:
            if path != "/api/ui/native-quit":
                raise
            with error:
                raise ValueError(json.load(error).get("error", str(error))) from error

    def fetch(self, url, timeout=5):
        """GET JSON from another loopback origin (the Skin Tools worker), without the supervisor's Origin."""
        with self.opener.open(Request(url), timeout=timeout) as response:
            return json.load(response)

    def heartbeat(self):
        while not self.stopped.is_set():
            try:
                with self.lease_lock:
                    if self.stopped.is_set():
                        return
                    result = self.request("/api/ui/heartbeat", {"client": self.client}, timeout=25)
                if not result.get("accepted") or not result.get("managed"):
                    raise ValueError("Supervisor refused the desktop lifetime lease")
                self.error = None
                self.stopped.wait(20)
            except (OSError, ValueError) as error:
                self.error = str(error)
                self.stopped.wait(3)

    def control(self, name, action, body):
        request = Request(self.base + f"/api/modules/{name}/{action}",
                          json.dumps(body).encode(),
                          {"Origin": self.base, "Content-Type": "application/json", "X-SHP-Control": "1"})
        try:
            with self.opener.open(request, timeout=60) as response:
                return True, json.load(response)
        except HTTPError as error:
            with error:
                return False, json.load(error)

    def close(self):
        self.stopped.set()
        try:
            with self.lease_lock:
                self.request("/api/ui/close", {"client": self.client}, timeout=3)
        except (OSError, ValueError) as error:
            import sys
            print(f"Desktop lease release failed; server will use expiry: {error}", file=sys.stderr, flush=True)


def work_signature(status):
    return {name: (state.get("instance"), state.get("revision"), state.get("loaded"))
            for name, state in status["modules"].items()}


def busy_modules(status):
    busy = [name for name, state in status["modules"].items()
            if state.get("busy") or state.get("phase") in ("starting", "stopping", "unavailable")]
    return busy


def external_lines(external):
    skin = external.get("photocraft_skin")
    if not skin:
        photocraft = "PhotoCraft: Checking"
    else:
        photocraft = "PhotoCraft: " + ("Reachable" if skin["reachable"] else "Unreachable") + " (TCP)"
        if skin.get("error"):
            photocraft += " - " + skin["error"]
    return (photocraft,)
