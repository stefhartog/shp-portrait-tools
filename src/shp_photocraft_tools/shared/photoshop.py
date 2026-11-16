"""Authenticated, bounded UXP command transport owned by the Skin Tools HTTP server."""

from collections import OrderedDict
import ctypes
from ctypes import wintypes
from dataclasses import dataclass, field
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import tempfile
import threading
import time
from urllib.parse import parse_qs, urlsplit

from .photocraft import ControlError
from .platform_paths import global_config_dir

PROTOCOL = 1
CHUNK = 2 * 1024 * 1024
MAX_PIXELS = 40_000_000
MAX_TRANSFER = 2 * 1024**3
MAX_JSON = 256 * 1024
IDENTIFIER = re.compile(r"[a-zA-Z0-9_-]{1,80}\Z")


class CredentialStore:
    """DPAPI user-bound storage; no plaintext credential fallback on other hosts."""

    def __init__(self, path=None):
        self.path = Path(path) if path else global_config_dir() / "photoshop" / "credential.dpapi"

    @staticmethod
    def _crypt(data, decrypt=False):
        if os.name != "nt":
            raise ControlError("Persistent Photoshop pairing currently requires Windows DPAPI")

        class Blob(ctypes.Structure):
            _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

        buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
        source, target = Blob(len(data), buffer), Blob()
        crypt = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
        function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                             ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        function.restype = wintypes.BOOL
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree.restype = ctypes.c_void_p
        if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
            raise ControlError("Windows could not protect/unprotect the Photoshop credential")
        try:
            return ctypes.string_at(target.data, target.size)
        finally:
            kernel.LocalFree(target.data)

    def load(self):
        if not self.path.exists():
            return None
        return self._crypt(self.path.read_bytes(), decrypt=True).decode("ascii")

    def save(self, token):
        protected = self._crypt(token.encode("ascii"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_bytes(protected)
        temporary.replace(self.path)

    def clear(self):
        self.path.unlink(missing_ok=True)


@dataclass
class Request:
    id: str
    kind: str
    params: dict
    session: str
    directory: Path
    state: str = "queued"
    result: dict | None = None
    error: str | None = None
    transferred: int = 0
    complete: threading.Event = field(default_factory=threading.Event)

    def blob(self, name):
        if not isinstance(name, str) or not IDENTIFIER.fullmatch(name):
            raise ValueError("Invalid binary object identifier")
        return self.directory / name


class Broker:
    def __init__(self, store=None, *, timeout=600, clock=time.monotonic):
        self.store = store if store is not None else CredentialStore()
        self.token = self.store.load()
        self.timeout, self.clock = timeout, clock
        self.lock = threading.RLock()
        self.operation = threading.Lock()
        self.session = None
        self.capabilities = frozenset()
        self.last_seen = 0
        self.pairing = None
        self.pending = None
        self.records = OrderedDict()

    def pairing_code(self):
        with self.lock:
            self.pairing = [secrets.token_urlsafe(12), self.clock() + 120, 0]
            return {"code": self.pairing[0], "expires_seconds": 120}

    def pair(self, code):
        with self.lock:
            pair = self.pairing
            if pair is None or pair[1] < self.clock() or pair[2] >= 5:
                raise ValueError("Generate a fresh pairing code in Tools")
            pair[2] += 1
            if not isinstance(code, str) or not hmac.compare_digest(code, pair[0]):
                raise ValueError("Incorrect pairing code")
            token = secrets.token_urlsafe(32)
            self.store.save(token)
            self._invalidate("Photoshop was paired again; Get Image again")
            self.token, self.pairing = token, None
            return {"token": token, "protocol": PROTOCOL}

    def revoke(self):
        with self.lock:
            self.store.clear()
            self.token = self.pairing = None
            self._invalidate("Photoshop pairing revoked")
            return {"revoked": True}

    def _invalidate(self, reason):
        self.session = None
        self.capabilities = frozenset()
        if self.pending and not self.pending.complete.is_set():
            self.pending.state, self.pending.error = "uncertain", reason
            self.pending.complete.set()

    def authorize(self, header):
        with self.lock:
            return bool(self.token and isinstance(header, str)
                        and hmac.compare_digest(header, "Bearer " + self.token))

    def hello(self, session, version, capabilities=None):
        if (type(version) is not int or version != PROTOCOL or not isinstance(session, str)
                or not IDENTIFIER.fullmatch(session)):
            raise ValueError("Incompatible Photoshop connector protocol/session")
        if capabilities is None:
            capabilities = []
        if (not isinstance(capabilities, list) or len(capabilities) > 16
                or any(not isinstance(item, str) or not IDENTIFIER.fullmatch(item) for item in capabilities)):
            raise ValueError("Invalid Photoshop connector capabilities")
        with self.lock:
            if session != self.session:
                self._invalidate("Photoshop connector restarted; Get Image again")
                self.session = session
            self.capabilities = frozenset(capabilities)
            self.last_seen = self.clock()
        return {"protocol": PROTOCOL, "chunk_bytes": CHUNK, "max_pixels": MAX_PIXELS}

    def connected(self):
        with self.lock:
            return bool(self.session and self.clock() - self.last_seen < 15)

    def poll(self):
        with self.lock:
            self.last_seen = self.clock()
            job = self.pending
            if job and job.state == "queued":
                job.state = "running"
                return {"request": job.id, "kind": job.kind, "params": job.params,
                        "protocol": PROTOCOL, "session": job.session}
        return None

    def request_status(self, request_id):
        with self.lock:
            if self.pending and self.pending.id == request_id:
                return {"request": request_id, "state": self.pending.state}
            return self.records.get(request_id, {"request": request_id, "state": "unknown"})

    def execute(self, kind, params, *, prepare=None, finish=None):
        if not self.operation.acquire(blocking=False):
            raise ControlError("A Photoshop transfer is already in progress")
        try:
            with tempfile.TemporaryDirectory(prefix="shp-photoshop-") as directory:
                with self.lock:
                    if not self.connected():
                        raise ControlError("Open the paired Skin Tools Connector panel in Photoshop")
                    job = Request(secrets.token_hex(16), kind, params, self.session, Path(directory))
                if prepare:
                    prepare(job)
                with self.lock:
                    if self.session != job.session:
                        raise ControlError("Photoshop connector restarted during preparation; Get Image again")
                    self.pending = job
                try:
                    if not job.complete.wait(self.timeout):
                        with self.lock:
                            if not job.complete.is_set():
                                job.state = "uncertain"
                                job.error = ("Photoshop response timed out; the command was NOT replayed. "
                                             "Inspect the document and Get Image again before sending.")
                    if job.error:
                        raise ControlError(f"{job.error} (request {job.id})")
                    return finish(job, job.result) if finish else job.result
                finally:
                    with self.lock:
                        self.records[job.id] = {"request": job.id, "state": job.state, "error": job.error}
                        while len(self.records) > 32:
                            self.records.popitem(last=False)
                        self.pending = None
        finally:
            self.operation.release()

    def reply(self, request_id, value):
        with self.lock:
            job = self._job(request_id)
            if not isinstance(value, dict) or set(value) - {"result", "error"}:
                raise ValueError("Invalid Photoshop response")
            if bool("result" in value) == bool("error" in value):
                raise ValueError("Supply exactly one Photoshop result or error")
            if "result" in value and not isinstance(value["result"], dict):
                raise ValueError("Photoshop result must be an object")
            if "error" in value and (not isinstance(value["error"], str) or len(value["error"]) > 2000):
                raise ValueError("Invalid Photoshop error")
            job.result, job.error = value.get("result"), value.get("error")
            job.state = "failed" if job.error else "completed"
            job.complete.set()
            return {"accepted": True, "state": job.state}

    def _job(self, request_id):
        job = self.pending
        if not job or job.id != request_id or job.session != self.session or job.state != "running":
            raise ValueError("Expired or unknown Photoshop request; it must not be replayed")
        return job

    def upload(self, request_id, name, offset, data):
        with self.lock:
            job = self._job(request_id)
            if job.kind != "capture" or not 0 < len(data) <= CHUNK:
                raise ValueError("Invalid capture chunk")
            path = job.blob(name)
            if not path.exists() and len(list(job.directory.iterdir())) >= 4:
                raise ValueError("Too many capture objects")
            size = path.stat().st_size if path.exists() else 0
            if offset != size or job.transferred + len(data) > MAX_TRANSFER:
                raise ValueError("Out-of-order or oversized capture transfer")
            with path.open("ab") as stream:
                stream.write(data)
            job.transferred += len(data)
        return {"offset": offset + len(data)}

    def download(self, request_id, name, offset, length=CHUNK):
        with self.lock:
            job = self._job(request_id)
            if job.kind != "apply":
                raise ValueError("Not a layer import")
            path = job.blob(name)
            if (not path.is_file() or offset < 0 or offset >= path.stat().st_size
                    or not 0 < length <= CHUNK):
                raise ValueError("Invalid layer chunk offset")
            with path.open("rb") as stream:
                stream.seek(offset)
                return stream.read(length)


def handle_http(handler, broker):
    """Handle only connector routes, outside the image-processing lock."""
    split = urlsplit(handler.path)
    if not split.path.startswith("/api/photoshop/"):
        return False
    try:
        if (handler.headers.get("Host") not in (f"127.0.0.1:{handler.server.server_port}",
                                                f"localhost:{handler.server.server_port}")
                or handler.client_address[0] != "127.0.0.1"
                or handler.headers.get("Origin") not in (None, "null")):
            handler.send(403, b'{"error":"Loopback connector requests only"}', "application/json")
            return True
        route = split.path.removeprefix("/api/photoshop/")
        if route != "pair" and not broker.authorize(handler.headers.get("Authorization")):
            handler.send(401, b'{"error":"Pair the Photoshop connector in Tools"}', "application/json")
            return True
        if route not in ("pair", "hello"):
            if not broker.session or handler.headers.get("X-SHP-Session") != broker.session:
                raise ValueError("Photoshop session expired; reconnect and Get Image again")
        if handler.command == "GET":
            query = parse_qs(split.query, strict_parsing=True)
            if route == "poll":
                result = broker.poll()
            elif route == "status":
                result = broker.request_status(query["request"][0])
            elif route == "chunk":
                data = broker.download(query["request"][0], query["name"][0], int(query["offset"][0]),
                                       int(query.get("length", [CHUNK])[0]))
                handler.send(200, data, "application/octet-stream")
                return True
            else:
                raise ValueError("Unknown Photoshop route")
        elif handler.command == "POST":
            length = int(handler.headers.get("Content-Length", "0"))
            binary = route == "chunk"
            expected = "application/octet-stream" if binary else "application/json"
            if handler.headers.get("Content-Type") != expected or not 0 < length <= (CHUNK if binary else MAX_JSON):
                raise ValueError("Invalid Photoshop request content type or size")
            handler.connection.settimeout(15)
            data = handler.rfile.read(length)
            if len(data) != length:
                raise ValueError("Incomplete Photoshop request")
            if binary:
                query = parse_qs(split.query, strict_parsing=True)
                result = broker.upload(query["request"][0], query["name"][0], int(query["offset"][0]), data)
            else:
                value = json.loads(data)
                if not isinstance(value, dict):
                    raise ValueError("Expected a JSON object")
                if route == "pair":
                    result = broker.pair(value.get("code"))
                elif route == "hello":
                    result = broker.hello(value.get("session"), value.get("protocol"), value.get("capabilities"))
                elif route == "reply":
                    result = broker.reply(value.get("request"), value.get("value"))
                else:
                    raise ValueError("Unknown Photoshop route")
        else:
            raise ValueError("Unsupported Photoshop HTTP method")
        handler.send(200, json.dumps(result).encode(), "application/json")
    except (ValueError, KeyError, TypeError, OSError, ControlError) as error:
        handler.send(400, json.dumps({"error": str(error)}).encode(), "application/json")
    return True
