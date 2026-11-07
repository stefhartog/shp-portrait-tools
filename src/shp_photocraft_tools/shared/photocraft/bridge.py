"""Compatible PhotoCraft JSON-lines transport, session handling and merged-document fetch."""

import itertools
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

OPENFILL = Path.home() / ".config" / "openfill-photocraft"


class ControlError(RuntimeError):
    """PhotoCraft refused a request or could not be reached."""


def detect_config(prefix, module):
    """Module-specific env overrides shared SHP_PC_*; existing OpenFill auth is reused."""
    own = Path.home() / ".config" / "shp-photocraft-tools" / module

    def env(key, default=None):
        return os.environ.get(f"{prefix}_PC_{key}", os.environ.get(f"SHP_PC_{key}", default))

    port = int(env("PORT", "7878"))
    token = env("TOKEN_FILE")
    exchange = env("EXCHANGE")
    of_token = OPENFILL / "control.token"
    if token is None:
        token = of_token if of_token.exists() else own / "control.token"
    token = Path(token).expanduser()
    if exchange is None:
        exchange = OPENFILL / "exchange" if token == of_token else own / "exchange"
    exchange = Path(exchange).expanduser()
    home = Path.home().resolve()
    read_root = env("READ_ROOT")
    if read_root is None:
        try:
            exchange.resolve().relative_to(home)
            read_root = home
        except ValueError:
            read_root = exchange
    return {"port": port, "token_file": token, "exchange": exchange,
            "read_root": Path(read_root).expanduser(), "config_dir": own}


class Client:
    """One authenticated JSON-lines connection (reconnects when it drops)."""

    def __init__(self, port, token_file, host="127.0.0.1", timeout=180.0):
        self.host, self.port, self.token_file, self.timeout = host, port, Path(token_file), timeout
        self._sock = self._file = None
        self._id = itertools.count(1)

    def _connect(self):
        self.close()
        try:
            token = self.token_file.read_text(encoding="utf-8").strip()
        except OSError as e:
            raise ControlError(f"no control token at {self.token_file}: {e}") from e
        try:
            self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        except OSError as e:
            raise ControlError(f"PhotoCraft not running or contollable (port {self.port})") from e
        self._file = self._sock.makefile("rwb")
        reply = self._exchange({"id": "auth", "method": "auth", "params": {"token": token}})
        if not reply.get("ok"):
            self.close()
            raise ControlError(f"PhotoCraft refused the control token: {reply.get('error')}")

    def close(self):
        for t in (self._file, self._sock):
            try:
                if t is not None:
                    t.close()
            except OSError:
                pass
        self._file = self._sock = None

    def _exchange(self, msg):
        self._file.write((json.dumps(msg) + "\n").encode())
        self._file.flush()
        line = self._file.readline()
        if not line:
            raise ConnectionError("PhotoCraft closed the control connection")
        return json.loads(line)

    def call(self, method, **params):
        return self._call(method, params, 2)

    def _call(self, method, params, attempts):
        for attempt in range(1, attempts + 1):
            if self._sock is None:
                self._connect()
            try:
                reply = self._exchange({"id": next(self._id), "method": method, "params": params})
            except (OSError, ConnectionError, ValueError) as e:
                self.close()
                if attempt == attempts:
                    note = "; command may have executed; it was not retried" if attempts == 1 else ""
                    raise ControlError(f"lost the connection to PhotoCraft: {e}{note}") from e
                continue
            if not reply.get("ok"):
                raise ControlError(f"{method}: {reply.get('error')}")
            return reply.get("result")

    def run(self, command, **params):
        return self.call("engine.execute", command=command, params=params)

    def run_once(self, command, **params):
        """Do not replay a possibly executed mutation after losing its reply."""
        return self._call("engine.execute", {"command": command, "params": params}, 1)


class Bridge:
    """Common session and file exchange operations; applying results belongs to each module."""

    env_prefix = ""
    module = ""
    app_name = ""

    def __init__(self, port=None, token_file=None, exchange=None, read_root=None):
        cfg = detect_config(self.env_prefix, self.module)
        self.port = port or cfg["port"]
        self.token_file = Path(token_file) if token_file else cfg["token_file"]
        self.exchange = Path(exchange) if exchange else cfg["exchange"]
        self.read_root = Path(read_root) if read_root else cfg["read_root"]
        self.config_dir = cfg["config_dir"]
        self.client = Client(self.port, self.token_file)
        self.headless = False
        self._names = itertools.count(1)
        self.exchange.mkdir(parents=True, exist_ok=True)

    def session(self):
        if not self.headless:
            try:
                return self.client.call("ui.inspect").get("session") or {}
            except ControlError as e:
                if "unknown" not in str(e).lower():
                    raise
                self.headless = True
        return self.client.call("session.list")

    def active_doc(self):
        s = self.session()
        for d in s.get("documents") or []:
            if d.get("index") == s.get("active"):
                return d
        return None

    def status(self):
        doc = self.active_doc()
        return {"connected": True, "doc": doc, "port": self.port, "headless": self.headless}

    def _find(self, index, name):
        docs = self.session().get("documents") or []
        for d in docs:
            if d.get("index") == index and d.get("name") == name:
                return d
        for d in docs:
            if d.get("name") == name:
                return d
        raise ControlError(f"the document '{name}' is no longer open in PhotoCraft")

    def _activate(self, index):
        self.client.run("document.activate", document=int(index))

    def _file(self, stem):
        return f"{self.module} {stem} {os.getpid()}-{next(self._names)}.png"

    def _open(self, rel):
        path = os.path.relpath(self.exchange / rel, self.read_root).replace(os.sep, "/")
        self.client.call("doc.open" if self.headless else "app.open", path=path)
        return int(self.session()["active"])

    def _save(self, rel, index):
        if self.headless:
            self.client.call("doc.save", path=rel, index=index)
        else:
            self.client.call("app.save", path=rel)

    def _close(self, index):
        try:
            self.client.run("file.close", document=int(index))
        except ControlError:
            try:
                self.client.call("doc.close", index=int(index))
            except ControlError:
                pass

    def fetch(self, dest_dir):
        """Save the active document, merged, as PNG. Returns (path, document info)."""
        orig = self.active_doc()
        if orig is None:
            raise ControlError("PhotoCraft has no open document")
        self.client.run("image.duplicate", name=f"{self.app_name} export", mergedOnly=True)
        dup = int(self.session()["active"])
        rel = self._file("export")
        try:
            self._save(rel, dup)
        finally:
            self._close(dup)
            self._activate(orig["index"])
        src = self.exchange / rel
        for _ in range(100):
            if src.is_file():
                break
            time.sleep(0.05)
        if not src.is_file():
            raise ControlError("PhotoCraft did not write the exported picture")
        stem = Path(str(orig.get("name") or "photocraft")).stem or "photocraft"
        stem = "".join(c if c.isalnum() or c in "-_ ." else "_" for c in stem)
        dest = Path(dest_dir)
        dest.mkdir(parents=True, exist_ok=True)
        out = dest / f"{stem}.png"
        shutil.move(str(src), str(out))
        return out, orig

    def launch(self):
        """Explicitly start PhotoCraft with the control channel on, as the OpenFill launcher does."""
        cmd = os.environ.get("PHOTOCRAFT_BIN") or shutil.which("photocraft") or str(Path.home() / ".local" / "bin" / "photocraft")
        if not Path(cmd).exists() and not shutil.which(cmd):
            raise ControlError("cannot find PhotoCraft (set PHOTOCRAFT_BIN)")
        self.token_file.parent.mkdir(parents=True, exist_ok=True)
        env = {k: v for k, v in os.environ.items() if not k.startswith("PHOTOCRAFT_CONTROL") and not k.startswith("PHOTOCRAFT_AUTOMATION")}
        args = [cmd, "--control", str(self.port), "--control-token-file", str(self.token_file),
                "--automation-read-root", str(self.read_root), "--automation-write-root", str(self.exchange)]
        self.config_dir.mkdir(parents=True, exist_ok=True)
        with open(self.config_dir / "photocraft.log", "ab") as log:
            subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT, env=env, start_new_session=sys.platform != "win32")
        return {"launched": cmd, "port": self.port}
