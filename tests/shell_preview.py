"""Isolated real supervisor + synthetic module children for browser checks."""

import contextlib
import json
import os
import signal
import socket
import sys
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PIL import Image

from shp_photocraft_tools import launcher, supervisor


@contextlib.contextmanager
def fixture(comfy=None):
    with tempfile.TemporaryDirectory(prefix="shp-shell-preview-") as tmp:
        root = Path(tmp)
        y, x = np.mgrid[:600, :800]
        wave = .06 * np.sin(y / 16) * np.cos(x / 20)
        rgb = np.stack([.6 + x / 5000 + wave, .3 + y / 5000 + wave, .2 + wave], axis=2)
        Image.fromarray(np.round(np.clip(rgb, 0, 1) * 255).astype(np.uint8)).save(root / "synthetic.png")
        models = root / "models"
        models.mkdir()
        for filename in launcher.MODEL_FILES:
            (models / filename).write_bytes(b"fixture only - never loaded")
        sockets = [socket.socket() for _ in range(2)]
        for sock in sockets:
            sock.bind(("127.0.0.1", 0))
        ports = {name: sock.getsockname()[1] for name, sock in zip(launcher.MODULES, sockets)}
        for sock in sockets:
            sock.close()
        config = {"models": str(models), "ports": ports, "shell_port": 0}
        if comfy:
            config["comfy"] = comfy
        env = {"SHPC_DATA": str(root / "crop"), "DNB_DATA": str(root / "dnb"),
               "SHP_PC_PORT": "1", "SHP_PC_TOKEN_FILE": str(root / "token"),
               "SHP_PC_EXCHANGE": str(root / "exchange"), "SHP_PC_READ_ROOT": str(root),
               "SHPC_COMFY": "http://127.0.0.1:1"}
        env.update({f"{prefix}_PC_PORT": "1" for prefix in ("SHPC", "DNB")})
        spawn = supervisor.subprocess.Popen

        def child(command, **kwargs):
            expected = "shp_photocraft_tools.modules."
            if len(command) > 3 and command[2] == "-m" and command[3].startswith(expected):
                name = command[3][len(expected):].split(".")[0]
                command = [command[0], "-u", str(Path(__file__).with_name("shell_child.py")), name, *command[4:]]
            return spawn(command, **kwargs)

        with patch.dict(os.environ, env), patch.object(supervisor.subprocess, "Popen", side_effect=child):
            modules = supervisor.Modules(config, root, root / "logs", "synthetic-shell-fixture")
            server = supervisor.ShellServer(("127.0.0.1", 0), modules)
            threads = [threading.Thread(target=server.serve_forever),
                       threading.Thread(target=modules.start_all),
                       threading.Thread(target=modules.monitor)]
            for thread in threads:
                thread.start()
            try:
                yield server, root
            finally:
                server.shutdown()
                modules.close()
                server.server_close()
                for thread in threads:
                    thread.join(timeout=15)


def main():
    def interrupt(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupt)
    with fixture() as (server, root):
        print(json.dumps({"url": f"http://127.0.0.1:{server.server_port}/", "data": str(root)}), flush=True)
        try:
            server.finished.wait()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
