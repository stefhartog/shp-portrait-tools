"""Supervisor test children: synthetic data/stub services only, never real inference."""

import json
import os
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

import numpy as np


def main():
    name = sys.argv.pop(1)
    root = Path(os.environ["SHPC_DATA"]).parent
    source = root / "synthetic.png"
    doc = {"index": 1, "name": source.name, "width": 800, "height": 600}

    class FakeBridge:
        def fetch(self, dest):
            return source, doc

        def status(self):
            return {"connected": True, "doc": doc, "headless": True}

        def send_expand(self, layer, document, margins):
            return {"layer": 9, "document": document["name"], "width": layer.shape[1], "height": layer.shape[0]}

        def send(self, layer, mask, document, *, source_icc=None):
            return {"layer": 9, "document": document["name"]}

        def send_mask(self, mask, document, *, source_icc=None):
            assert mask.shape == (600, 800) and mask.dtype == np.uint8
            return {"layer": 10, "document": document["name"]}

        def send_frequency(self, layers, mask, document, source_icc):
            assert set(layers) == {"color", "blur", "detail"}
            assert all(a.shape == (600, 800, 3) and a.dtype == np.uint16 for a in layers.values())
            return {"layer": 11, "document": document["name"]}

        def send_skintone(self, layers, settings, mask, document, source_icc):
            assert mask.shape == (600, 800) and mask.dtype == np.uint8
            assert set(layers) == {"color", "blur", "detail"}
            assert all(a.shape == (600,800,3) and a.dtype == np.uint16 for a in layers.values())
            assert set(settings) == {"hue_center","hue_width","hue_softness","hue_amount"}
            return {"layer": 12, "document": document["name"]}

    if name == "crop":
        from shp_photocraft_tools.modules.crop import server as module
        from test_expand import StubComfy

        stub = ThreadingHTTPServer(("127.0.0.1", 0), StubComfy)
        threading.Thread(target=stub.serve_forever, daemon=True).start()
        if os.environ.get("SHPC_TOOLS_COMFY") != "1":
            module.X.COMFY_URL = f"http://127.0.0.1:{stub.server_port}"

        def job(request):
            thread = threading.Thread(target=lambda: time.sleep(min(5, request.get("seconds", 1))))
            module.STATE["thread"] = thread
            thread.start()
            return {"started": True}

        module.ROUTES["/api/test/job"] = job
        def variation_delay(request):
            StubComfy.state["delay"] = min(3, max(0, float(request.get("seconds", 0))))
            return {}

        module.ROUTES["/api/test/variation-delay"] = variation_delay
    else:
        from shp_photocraft_tools.modules.dnb import ui_server as module
        import cv2

        for hue in (18,43):
            pixels=cv2.cvtColor(np.full((600,800,3),[hue,.6,.8],np.float32),cv2.COLOR_HSV2BGR)
            cv2.imwrite(str(root/f"hue-{hue}.png"),module.FS.rgb16(pixels))

        module.M.pick_target = lambda *a, **kw: {"S": 180, "conf": "fixture", "desc": "Synthetic test image",
                                          "centre": (400, 300), "face_info": None, "seen": None}
        module.M.person_box = lambda *a: (0, 0, 800, 600)

        def fake_mask(img, *args):
            mask = np.ones(img.shape[:2], np.float32)
            return {"A": mask, "skin": mask, "features": np.zeros_like(mask), "colour": mask}

        module.K.skin_mask = fake_mask
    module.BRIDGE["b"] = FakeBridge()
    module.DIRS.append(str(root))
    original = module.Handler

    class FixtureHandler(original):
        hue_route_available = True

        def do_api_POST(self):
            if self.path == "/api/test/hue-route":
                request = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                FixtureHandler.hue_route_available = request["available"]
                self.send(200, b"{}", "application/json")
            elif self.path == "/api/hue-center" and not FixtureHandler.hue_route_available:
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                self.send(404, b"not found", "text/plain")
            elif self.path == "/api/test/work":
                time.sleep(1)
                self.send(200, json.dumps({"done": True}).encode(), "application/json")
            else:
                super().do_api_POST()

    module.Handler = FixtureHandler
    module.main()


if __name__ == "__main__":
    main()
