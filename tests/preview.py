"""Safe synthetic Skin Tools browser fixture. No PhotoCraft, model downloads or GPU inference.

Run with the new environment: uv run python tests/preview.py
Uses temporary data and an ephemeral port; prints the URL as JSON.
"""

import json
import os
import signal
import shutil
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import numpy as np
from PIL import Image


def main():
    def interrupt(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupt)
    with tempfile.TemporaryDirectory(prefix="shp-photocraft-preview-") as tmp:
        root = Path(tmp)
        os.environ.update(SKIN_DATA=tmp + "/skin", SKIN_MODELS=tmp + "/models",
                          SHP_PC_PORT="1", SHP_PC_TOKEN_FILE=tmp + "/token", SHP_PC_EXCHANGE=tmp + "/exchange",
                          SHP_PC_READ_ROOT=tmp)
        from shp_portrait_tools.modules.skin import ui_server as D

        y, x = np.mgrid[:600, :800]
        wave = 0.06 * np.sin(y / 16) * np.cos(x / 20)
        rgb = np.stack([0.6 + x / 5000 + wave, 0.3 + y / 5000 + wave, 0.2 + wave], axis=2)
        source = root / "synthetic.png"
        Image.fromarray(np.round(np.clip(rgb, 0, 1) * 255).astype(np.uint8)).save(source)
        doc = {"index": 1, "name": source.name, "width": 800, "height": 600}

        class FakeBridge:
            def fetch(self, dest):
                dest = Path(dest)
                dest.mkdir(parents=True, exist_ok=True)
                target = dest / source.name
                shutil.copyfile(source, target)
                return target, doc

            def send(self, layer, mask, document, *, source_icc=None):
                return {"layer": 9, "document": document["name"]}

            def status(self):
                return {"connected": True, "doc": doc, "headless": True}

        D.BRIDGE["b"] = FakeBridge()
        D.DIRS.append(str(root))
        D.M.pick_target = lambda *a: {"S": 180, "conf": "fixture", "desc": "Synthetic test image",
                                     "centre": (400, 300), "face_info": None, "seen": None}
        D.M.person_box = lambda *a: (0, 0, 800, 600)

        def fake_mask(img, *args):
            mask = np.ones(img.shape[:2], np.float32)
            mask[:, :80] = 0
            return {"A": mask, "skin": mask, "features": np.zeros_like(mask), "colour": np.ones_like(mask)}

        D.K.skin_mask = fake_mask
        servers = [ThreadingHTTPServer(("127.0.0.1", 0), D.Handler)]
        threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in servers]
        for thread in threads:
            thread.start()
        print(json.dumps({"dnb": f"http://127.0.0.1:{servers[0].server_port}", "data": tmp}), flush=True)
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass
        finally:
            for server, thread in zip(servers, threads):
                server.shutdown()
                server.server_close()
                thread.join()


if __name__ == "__main__":
    main()
