"""Compatible PhotoCraft JSON-lines transport, session handling and merged-document fetch."""

import itertools
import hashlib
import hmac
import errno
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

from shp_portrait_tools.shared.platform_paths import photocraft_config_dir
from shp_portrait_tools.shared.filelock import file_lock

OPENFILL = Path.home() / ".config" / "openfill-photocraft"


class ControlError(RuntimeError):
    """PhotoCraft refused a request or could not be reached."""

    code = "control"


class AuthenticationError(ControlError):
    """A running PhotoCraft rejected authentication, not a local-open request."""

    code = "authentication"


def detect_config(prefix, module, environ=None):
    """Module-specific env overrides shared SHP_PC_*; existing OpenFill auth is reused."""
    environ = os.environ if environ is None else environ
    # PhotoCraft is configured against these token/exchange folders, so they keep the pre-rename name.
    own = (photocraft_config_dir() if sys.platform == "win32"
           else Path.home() / ".config" / "shp-photocraft-tools") / module

    def env(key, default=None):
        return environ.get(f"{prefix}_PC_{key}", environ.get(f"SHP_PC_{key}", default))

    port = int(env("PORT", "7878"))
    token = env("TOKEN_FILE")
    exchange = env("EXCHANGE")
    of_token = OPENFILL / "control.token"
    if token is None and sys.platform != "win32":
        token = of_token if of_token.exists() else own / "control.token"
    elif token is None:
        token = own / "control.token"
    token = Path(token).expanduser()
    if exchange is None:
        exchange = OPENFILL / "exchange" if token == of_token else own / "exchange"
    exchange = Path(exchange).expanduser()
    home = Path.home().resolve()
    read_root = env("READ_ROOT")
    if read_root is None and sys.platform == "win32":
        read_root = exchange
    elif read_root is None:
        try:
            exchange.resolve().relative_to(home)
            read_root = home
        except ValueError:
            read_root = exchange
    return {"port": port, "token_file": token, "exchange": exchange,
            "read_root": Path(read_root).expanduser(), "config_dir": own}


def _redact(value, secret):
   if isinstance(value, str):
       return value.replace(secret, "[redacted]") if secret else value
   if isinstance(value, list):
       return [_redact(item, secret) for item in value]
   if isinstance(value, dict):
       return {_redact(key, secret): _redact(item, secret) for key, item in value.items()}
   return value


class Client:
    """One authenticated JSON-lines connection (reconnects when it drops)."""

    def __init__(self, port, token_file, host="127.0.0.1", timeout=180.0):
        self.host, self.port, self.token_file, self.timeout = host, port, Path(token_file), timeout
        self._sock = self._file = None
        self._redaction_token = None
        self._id = itertools.count(1)

    def _connect(self):
        self.close()
        guard = os.environ.get("SHP_PC_VERIFY_FLATPAK")
        if guard not in (None, "1"):
            raise AuthenticationError("Invalid PhotoCraft Flatpak verification setting")
        if guard and sys.platform == "win32":
            raise AuthenticationError("PhotoCraft Flatpak verification is not available on Windows")
        if guard and self.host != "127.0.0.1":
            raise AuthenticationError("PhotoCraft Flatpak verification requires IPv4 loopback")
        try:
            token = self.token_file.read_text(encoding="utf-8").strip()
        except OSError as e:
            raise ControlError(f"no control token at {self.token_file}: {e}") from e
        try:
            self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        except OSError as e:
            raise ControlError(f"PhotoCraft not running or contollable (port {self.port})") from e
        if guard:
            from .flatpak_discovery import DiscoveryError, find_flatpak_process, pinned_user_deployment
            from .flatpak_peer import PeerIdentityError, verify_flatpak_peer

            try:
                executable, commit = pinned_user_deployment()
                pid = find_flatpak_process(executable, commit, Path(f"/run/user/{os.getuid()}"))
                verify_flatpak_peer(self._sock, pid, executable)
            except (DiscoveryError, PeerIdentityError, OSError) as e:
                self.close()
                raise AuthenticationError("PhotoCraft process identity could not be verified; no token was sent") from e
        self._redaction_token = token if guard or sys.platform == "win32" else None
        try:
            self._file = self._sock.makefile("rwb")
            if sys.platform == "win32":
                reply = self._challenge_auth(token)
            else:
                reply = self._exchange({"id": "auth", "method": "auth", "params": {"token": token}})
        except AuthenticationError:
            self.close()
            raise
        except (OSError, ConnectionError, ValueError) as e:
            self.close()
            raise ControlError("PhotoCraft authentication connection failed") from e
        if not isinstance(reply, dict):
            self.close()
            raise AuthenticationError("PhotoCraft sent an invalid authentication reply")
        if not reply.get("ok"):
            self.close()
            error = str(reply.get("error"))
            if token:
                error = error.replace(token, "[redacted]")
            raise AuthenticationError(f"PhotoCraft refused the control token: {error}")

    def _challenge_auth(self, token):
        if not re.fullmatch(r"[0-9a-fA-F]{64}", token):
            raise AuthenticationError("PhotoCraft control token must contain 64 hexadecimal characters")
        client_nonce = secrets.token_hex(32)
        reply = self._exchange({"id": "challenge", "method": "auth.challenge",
                                "params": {"clientNonce": client_nonce}})
        result = reply.get("result") if isinstance(reply, dict) and reply.get("ok") else None
        if not isinstance(result, dict):
            raise AuthenticationError("PhotoCraft did not provide an authentication challenge; no token was sent")
        nonce, proof = result.get("serverNonce"), result.get("serverProof")
        if not isinstance(nonce, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", nonce):
            raise AuthenticationError("PhotoCraft sent an invalid authentication challenge; no token was sent")
        def signature(role):
            message = f"photocraft-control-v1 {role}\n{client_nonce}\n{nonce.lower()}".encode("ascii")
            return hmac.new(token.lower().encode("ascii"), message, hashlib.sha256).hexdigest()
        if (not isinstance(proof, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", proof)
                or not hmac.compare_digest(signature("server"), proof.lower())):
            raise AuthenticationError("PhotoCraft could not prove its control token; no token was sent")
        return self._exchange({"id": "auth", "method": "auth", "params": {"proof": signature("client")}})

    def close(self):
        for t in (self._file, self._sock):
            try:
                if t is not None:
                    t.close()
            except OSError:
                pass
        self._file = self._sock = None
        self._redaction_token = None

    def _exchange(self, msg):
        self._file.write((json.dumps(msg) + "\n").encode())
        self._file.flush()
        line = self._file.readline()
        if not line:
            raise ConnectionError("PhotoCraft closed the control connection")
        reply = json.loads(line)
        if self._redaction_token and isinstance(reply, dict) and "error" in reply:
            reply["error"] = _redact(reply["error"], self._redaction_token)
        return reply

    def call(self, method, **params):
        if (os.environ.get("SHP_PC_VERIFY_FLATPAK") or sys.platform == "win32") and method in (
                "app.open", "app.save", "doc.open", "doc.save", "doc.close"):
            return self.call_once(method, **params)
        return self._call(method, params, 2)

    def call_once(self, method, **params):
        """Do not replay an operation if its acknowledgement is lost."""
        return self._call(method, params, 1)

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
        if os.environ.get("SHP_PC_VERIFY_FLATPAK") or sys.platform == "win32":
            return self.run_once(command, **params)
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
        full = Path(os.path.abspath(self.exchange / rel))
        root = Path(os.path.abspath(self.read_root))
        if not full.is_relative_to(root):
            raise ControlError("the exchange folder is outside the PhotoCraft read root")
        if self.headless:
            self.client.call("doc.open", path=full.relative_to(root).as_posix())
        else:
            # Absolute, so PhotoCraft does not remember a cwd-relative folder for its Open dialog.
            self.client.call("app.open", path=str(full))
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
        if sys.platform == "win32":
            return self._launch_windows()
        guard = os.environ.get("SHP_PC_VERIFY_FLATPAK")
        if guard not in (None, "1"):
            raise ControlError("Invalid PhotoCraft Flatpak verification setting")
        if guard and sys.platform == "win32":
            raise ControlError("PhotoCraft Flatpak verification is not available on Windows")
        if guard:
            cmd = os.environ.get("SHP_PC_FLATPAK_LAUNCHER", "")
            if not Path(cmd).is_absolute() or not Path(cmd).is_file() or not os.access(cmd, os.X_OK):
                raise ControlError("Cannot find the guarded PhotoCraft Flatpak launcher")
            args = [cmd]
        else:
            fallback = (Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "PhotoCraft" / "photocraft.exe"
                        if sys.platform == "win32" else Path.home() / ".local" / "bin" / "photocraft")
            cmd = os.environ.get("PHOTOCRAFT_BIN") or shutil.which("photocraft") or str(fallback)
            if not Path(cmd).exists() and not shutil.which(cmd):
                raise ControlError("cannot find PhotoCraft (set PHOTOCRAFT_BIN)")
            if sys.platform == "win32" and not self.token_file.is_file():
                raise ControlError("create a private PhotoCraft control token file before launching; see Windows setup")
            self.token_file.parent.mkdir(parents=True, exist_ok=True)
            args = [cmd, "--control", str(self.port), "--control-token-file", str(self.token_file),
                    "--automation-read-root", str(self.read_root), "--automation-write-root", str(self.exchange)]
        env = {k: v for k, v in os.environ.items() if not k.startswith("PHOTOCRAFT_CONTROL") and not k.startswith("PHOTOCRAFT_AUTOMATION")}
        self.config_dir.mkdir(parents=True, exist_ok=True)
        with open(self.config_dir / "photocraft.log", "ab") as log:
            subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT, env=env, start_new_session=sys.platform != "win32")
        return {"launched": cmd, "port": self.port}

    def _launch_windows(self):
        if os.environ.get("SHP_PC_VERIFY_FLATPAK") is not None:
            raise ControlError("PhotoCraft Flatpak verification is not available on Windows")
        if not self.token_file.is_file():
            raise ControlError("create a private PhotoCraft control token file before launching; see Windows setup")
        self.config_dir.mkdir(parents=True, exist_ok=True)
        with file_lock(self.config_dir / "photocraft-launch.lock", timeout=40):
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=3):
                    occupied = True
            except OSError as error:
                if isinstance(error, ConnectionRefusedError) or error.errno in (errno.ECONNREFUSED, 10061):
                    occupied = False
                else:
                    raise ControlError(f"Cannot check PhotoCraft port {self.port}: {error}") from error
            if occupied:
                self._verify_windows_editor()
                return {"reused": True, "port": self.port}
            fallback = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "PhotoCraft" / "photocraft.exe"
            cmd = os.environ.get("PHOTOCRAFT_BIN") or shutil.which("photocraft") or str(fallback)
            if not Path(cmd).is_file() and not shutil.which(cmd):
                raise ControlError("cannot find PhotoCraft (set PHOTOCRAFT_BIN)")
            editor_config = os.environ.get("PHOTOCRAFT_CONFIG_DIR")
            if not editor_config or not Path(editor_config).is_absolute() or not Path(editor_config).is_dir():
                raise ControlError("configure a separate existing PhotoCraft editor settings directory before launching")
            args = [cmd, "--control", str(self.port), "--control-token-file", str(self.token_file),
                    "--automation-read-root", str(self.read_root), "--automation-write-root", str(self.exchange)]
            env = {k: v for k, v in os.environ.items()
                   if not k.startswith("PHOTOCRAFT_CONTROL") and not k.startswith("PHOTOCRAFT_AUTOMATION")}
            log_path = self.config_dir / "photocraft.log"
            with log_path.open("ab") as log:
                process = subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT, env=env)
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise ControlError(f"PhotoCraft exited before connecting; see {log_path}")
                try:
                    self._verify_windows_editor()
                    return {"launched": cmd, "port": self.port}
                except AuthenticationError:
                    raise
                except ControlError:
                    time.sleep(0.25)
            raise ControlError(f"PhotoCraft did not become ready; see {log_path}. No process was stopped.")

    def _verify_windows_editor(self):
        client = Client(self.port, self.token_file, timeout=3)
        try:
            result = client.call_once("ui.inspect")
            if not isinstance(result, dict) or not isinstance(result.get("session"), dict):
                raise ControlError("The authenticated listener is not a PhotoCraft desktop session")
        finally:
            client.close()
