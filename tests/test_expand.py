"""Checks for SHP PhotoCraft Crop: frame geometry, settings, ComfyUI graph and client (against a stub), the
web endpoints and the PhotoCraft send sequence.
Run:  uv run python tests/test_expand.py   (no real ComfyUI or PhotoCraft needed)"""
import io
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import numpy as np
from PIL import Image

from shp_photocraft_tools.modules.crop import expand as X
from shp_photocraft_tools.modules.crop import server as S


def raises(fn, *a, **k):
    try:
        fn(*a, **k)
    except X.ExpandError:
        return True
    return False


def test_geometry():
    m, size = X.frame_margins(3000, 2000, (1, 1), 0, -500, 3000)
    assert m == (0, 500, 0, 500) and size == (3000, 3000)
    m, size = X.frame_margins(1080, 1080, (4, 5), 0, -135, 1080)  # square photo to Instagram 4:5
    assert size == (1080, 1350) and m == (0, 135, 0, 135), (m, size)
    m, size = X.frame_margins(3000, 2000, (4, 5), 300, -500, 2400)  # crops the sides, adds top and bottom
    assert size == (2400, 3000) and m == (-300, 500, -300, 500), (m, size)
    assert raises(X.frame_margins, 3000, 2000, (4, 5), 300, 0, 1600)  # only crops: nothing to generate
    m, size = X.frame_margins(3000, 2000, (1, 1), 100, -500, 3000)  # crops the left, adds on the right
    assert m == (-100, 500, 100, 500), m
    assert raises(X.frame_margins, 3000, 2000, (1, 1), 2950, -500, 3000)  # keeps almost nothing of the picture
    assert raises(X.frame_margins, 3000, 2000, (3, 2), 0, 0, 3000)  # nothing to add
    assert raises(X.frame_margins, 3000, 2000, (1, 1), 0, 0, 2000)  # frame smaller than the picture
    assert raises(X.frame_margins, 1000, 1000, (16, 9), 0, 0, 20000)  # beyond the per-side limit
    m, size = X.frame_margins(3000, 2000, None, 100, -300, 2700, 2500)  # free frame: crops x, adds 300/200 on y
    assert m == (-100, 300, -200, 200) and size == (2700, 2500), (m, size)
    assert raises(X.frame_margins, 3000, 2000, None, 0, 0, 3000)  # free frame without a height
    m, size = X.frame_margins(1639, 2000, (4, 5), 0, -24, 1639, 2048)  # 1639 * 5/4 = 2048.75: snapped height accepted
    assert m == (0, 24, 0, 24) and size == (1639, 2048), (m, size)
    assert X.frame_margins(1639, 2000, (4, 5), 0, -24, 1639, 2100)[1] == (1639, 2049)  # far off: height from the ratio


def test_settings(tmp):
    path = tmp / "settings.json"
    assert X.fill_settings(path) == X.DEFAULTS and not path.exists()
    got = X.fill_settings(path, {"guidance": "2.5", "lora": False, "steps": 20})
    assert got["guidance"] == 2.5 and got["lora"] is False and got["steps"] == 20 and path.exists()
    assert X.load_settings(path) == got
    assert raises(X.fill_settings, path, {"bogus": 1}) and raises(X.fill_settings, path, {"steps": 0})
    assert raises(X.fill_settings, path, {"megapixels": "x"}) and X.load_settings(path)["steps"] == 20
    assert X.fill_settings(path, reset=True) == X.DEFAULTS
    path.write_text("not json")
    assert X.load_settings(path) == X.DEFAULTS


def test_graph():
    for lora in (True, False):
        for seam in (True, False):
            s = dict(X.DEFAULTS, lora=lora, seam_correction=seam)
            g = X.build_graph("shp/pic.png", (200, 0, 200, 0), "a beach", 7, s)
            for nid, node in g.items():  # every link points at a node that exists
                for v in node["inputs"].values():
                    if isinstance(v, list):
                        assert v[0] in g, (nid, v)
            assert g["2"]["inputs"]["left"] == 200 and g["2"]["inputs"]["feathering"] == 128
            assert g["16"]["inputs"]["noise_seed"] == 7 and "a beach" in g["8"]["inputs"]["text"]
            assert ("20" in g) == lora and ("27" in g) == seam
            assert g["12"]["inputs"]["model"][0] == ("20" if lora else "5")
            assert g["26"]["inputs"]["destination"][0] == ("27" if seam else "25")
    assert "new area should show" not in X.build_graph("p.png", (1, 0, 0, 0), "  ", 1, X.DEFAULTS)["8"]["inputs"]["text"]


class StubComfy(BaseHTTPRequestHandler):
    """Enough of ComfyUI's HTTP API: object_info, upload, prompt, history, view, queue, interrupt."""
    state = {}

    def log_message(self, *a):
        pass

    def reply(self, body, code=200, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        st = StubComfy.state
        if self.path == "/object_info":
            info = {n: {"input": {"required": {}}} for n in X.REQUIRED_NODES}
            info["UnetLoaderGGUF"]["input"]["required"]["unet_name"] = [[X.DEFAULTS["unet"], "other.gguf"]]
            info["UpscaleModelLoader"]["input"]["required"]["model_name"] = [[X.DEFAULTS["upscale"]]]
            info["CLIPLoader"]["input"]["required"]["clip_name"] = [[X.CLIP_NAME]]
            info["VAELoader"]["input"]["required"]["vae_name"] = [[X.VAE_NAME]]
            info["LoraLoaderModelOnly"]["input"]["required"]["lora_name"] = [[X.LORA_NAME]]
            for n in st.get("drop", []):
                info.pop(n, None)
            return self.reply(info)
        if self.path.startswith("/history/"):
            pid = self.path.rsplit("/", 1)[1]
            return self.reply({pid: st["history"]} if st.get("history") and st["prompt_id"] == pid else {})
        if self.path.startswith("/view?"):
            name = re.search(r"filename=([^&]+)", self.path).group(1)
            return self.reply(st["files"][name], ctype="image/png")
        self.reply({}, 404)

    def do_POST(self):
        st = StubComfy.state
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if self.path == "/upload/image":
            m = re.search(rb"\r\n\r\n(\x89PNG.*?)\r\n--", body, re.S)
            st["uploaded"] = Image.open(io.BytesIO(m.group(1))).size
            return self.reply({"name": "up.png", "subfolder": "", "type": "input"})
        if self.path == "/prompt":
            req = json.loads(body)
            st["graph"] = g = req["prompt"]
            if st.get("refuse"):
                return self.reply({"error": {"message": "Prompt outputs failed validation"},
                                   "node_errors": {"5": {"class_type": "UnetLoaderGGUF", "errors": [
                                       {"message": "Value not in list", "details": "unet_name: 'x'"}]}}}, 400)
            l, t, r, b = (g["2"]["inputs"][k] for k in ("left", "top", "right", "bottom"))
            w, h = st["uploaded"][0] + l + r, st["uploaded"][1] + t + b
            canvas, mask = Image.new("RGB", (w, h), (0, 150, 0)), Image.new("L", (w, h), 255)
            mask.paste(0, (l, t, l + st["uploaded"][0], t + st["uploaded"][1]))
            for key, im in (("out.png", canvas), ("mask.png", mask.convert("RGB"))):
                buf = io.BytesIO()
                im.save(buf, "PNG")
                st.setdefault("files", {})[key] = buf.getvalue()
            st["prompt_id"] = f"p{len(st.setdefault('seeds', []))}"
            st["seeds"].append(g["16"]["inputs"]["noise_seed"])
            if st.get("fail"):
                st["history"] = {"status": {"status_str": "error", "messages": [
                    ["execution_error", {"node_type": "VAEDecode", "exception_message": "out of memory"}]]}, "outputs": {}}
            elif st.get("hang"):
                st["history"] = None
            else:
                st["history"] = {"status": {"status_str": "success", "completed": True}, "outputs": {
                    "out": {"images": [{"filename": "out.png", "subfolder": "", "type": "temp"}]},
                    "mask": {"images": [{"filename": "mask.png", "subfolder": "", "type": "temp"}]}}}
            return self.reply({"prompt_id": st["prompt_id"]})
        if self.path == "/queue":
            st["deleted"] = json.loads(body)
        if self.path == "/interrupt":
            st["interrupted"] = True
        self.reply({})


def stub_server():
    StubComfy.state = {}
    srv = ThreadingHTTPServer(("127.0.0.1", 0), StubComfy)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}"


def test_client():
    srv, url = stub_server()
    st = StubComfy.state
    try:
        pic = Image.new("RGB", (300, 200), (10, 120, 200))
        assert X.status(X.DEFAULTS, url)["ok"]
        st["drop"] = ["SeamColorCorrect"]
        s = X.status(X.DEFAULTS, url)
        assert not s["ok"] and "SeamColorCorrect" in s["message"] and s["unets"] == [X.DEFAULTS["unet"], "other.gguf"]
        st["drop"] = []
        assert "not installed" in X.status(dict(X.DEFAULTS, unet="nope.gguf"), url)["message"]
        assert not X.status(X.DEFAULTS, "http://127.0.0.1:1")["ok"] and X.status(X.DEFAULTS, url)["reachable"]
        assert not X.status(X.DEFAULTS, "http://127.0.0.1:1")["reachable"]
        assert "not reachable" in X.status(X.DEFAULTS, "http://127.0.0.1:1")["message"]

        job = X.Job()
        res = X.expand_variations(pic, (0, 50, 0, 50), "", job, seed=10, settings=X.DEFAULTS, variations=2, url=url)
        assert len(res) == 2 and st["seeds"] == [10, 11] and st["uploaded"] == (300, 200)
        r = res[0]
        assert r["canvas"].size == (300, 300) and r["layer"].size == (300, 300) and r["margins"] == (0, 50, 0, 50)
        a = np.asarray(r["layer"].getchannel("A"))
        assert a[0, 0] == 255 and a[150, 150] == 0 and job.result is res[0] and len(job.results) == 2

        res = X.expand_variations(pic, (-100, 50, -100, 50), "", X.Job(), 5, X.DEFAULTS, 1, url)[0]  # crops x, adds y
        assert st["uploaded"] == (100, 200) and res["canvas"].size == (100, 300) and res["margins"] == (-100, 50, -100, 50)
        assert st["graph"]["2"]["inputs"]["left"] == 0 and st["graph"]["2"]["inputs"]["top"] == 50

        st["refuse"] = True  # ComfyUI rejects the graph: its reason reaches the user
        try:
            X.expand_variations(pic, (0, 50, 0, 50), "", X.Job(), 1, X.DEFAULTS, 1, url)
        except X.ExpandError as e:
            assert "UnetLoaderGGUF" in str(e) and "Value not in list" in str(e), e
        else:
            raise AssertionError("graph refusal not reported")
        st["refuse"], st["fail"] = False, True
        try:
            X.expand_variations(pic, (0, 50, 0, 50), "", X.Job(), 1, X.DEFAULTS, 1, url)
        except X.ExpandError as e:
            assert "out of memory" in str(e)
        else:
            raise AssertionError("execution error not reported")

        st["fail"], st["hang"] = False, True  # a run that never finishes can be cancelled
        job = X.Job()
        threading.Timer(1.0, lambda: setattr(job, "cancel", True)).start()
        try:
            X.expand_variations(pic, (0, 50, 0, 50), "", job, 1, X.DEFAULTS, 1, url)
        except X.Cancelled:
            assert st.get("interrupted") and st["deleted"] == {"delete": [st["prompt_id"]]}
        else:
            raise AssertionError("cancel ignored")
    finally:
        srv.shutdown()


def test_websocket_frames():
    ws = X._WS.__new__(X._WS)
    msg = json.dumps({"type": "progress", "data": {"value": 3, "max": 8}}).encode()
    ws.buf = bytes([0x81, len(msg)]) + msg[:10]
    assert ws._frame() is None  # incomplete frame waits for more bytes
    ws.buf += msg[10:] + bytes([0x81, 126]) + (300).to_bytes(2, "big") + b"x" * 300
    op, payload = ws._frame()
    assert op == 1 and json.loads(payload)["data"]["max"] == 8
    op, payload = ws._frame()
    assert op == 1 and len(payload) == 300 and ws._frame() is None


def test_static_assets():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), S.Handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{srv.server_port}"
    try:
        for path in ("/", "/index.html"):
            with urlopen(base + path) as response:
                html = response.read().decode()
                assert response.headers["Content-Type"] == "text/html; charset=utf-8"
                assert html.index('<link rel="stylesheet" href="static/shp-ui.css">') < html.index("<style>")
                assert '<div class="tabs pane-header">' in html
        for path in ("/static/shp-ui.css", "/static/shp-ui.css?v=1"):
            with urlopen(base + path) as response:
                css = response.read()
                assert css == S.stylesheet() and css
                assert b".pane-header {" in css
                assert response.headers["Content-Type"] == "text/css; charset=utf-8"
                assert int(response.headers["Content-Length"]) == len(css)
        for path in ("/static/missing.css", "/static/../pyproject.toml"):
            try:
                urlopen(base + path)
            except HTTPError as error:
                assert error.code == 404
            else:
                raise AssertionError(f"unexpected static asset: {path}")
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join()


def test_server(tmp):
    from unittest import mock
    src = tmp / "pic.png"
    Image.new("RGB", (300, 200), (10, 120, 200)).save(src)
    seeds = []

    def fake_expand(comfy, name, size, margins, prompt, job, seed, s):
        l, t, r, b = margins
        canvas = Image.new("RGBA", (size[0] + l + r, size[1] + t + b), (0, 100 + seed % 100, 0, 255))
        seeds.append(seed)
        alpha = np.full((canvas.height, canvas.width), 255, np.uint8)
        alpha[t:t + size[1], l:l + size[0]] = 0
        layer = canvas.copy()
        layer.putalpha(Image.fromarray(alpha))
        return {"canvas": canvas.convert("RGB"), "layer": layer, "margins": margins}

    S.DATA, S.SETTINGS_FILE, S.PRESETS_FILE = tmp, tmp / "settings.json", tmp / "presets.json"
    srv = ThreadingHTTPServer(("127.0.0.1", 0), S.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"

    def post(path, body):
        req = Request(base + path, json.dumps(body).encode(), {"Content-Type": "application/json"})
        try:
            return json.load(urlopen(req))
        except Exception as e:
            return json.load(e)

    ok = {"ok": True, "message": "", "unets": [], "upscalers": []}
    try:
        assert "error" in post("/api/run", {"ratio": [1, 1], "x": 0, "y": 0, "w": 300})  # no picture yet
        r = post("/api/load", {"path": str(src)})
        assert (r["width"], r["height"]) == (300, 200) and r["limit"] == X.EXPAND_LIMIT
        assert urlopen(base + "/img/xorig").read()[:2] == b"\xff\xd8"
        assert "error" in post("/api/run", {"ratio": [1, 1], "x": 5000, "y": 0, "w": 300})  # frame misses the picture
        with mock.patch.object(X, "expand", fake_expand), mock.patch.object(X, "status", lambda *a, **k: ok), \
                mock.patch.object(X.Comfy, "upload", lambda *a, **k: "up.png"):
            r = post("/api/run", {"ratio": [1, 1], "x": 0, "y": -50, "w": 300})
            assert r["margins"] == [0, 50, 0, 50], r
            S.STATE["thread"].join(10)
            assert post("/api/job", {})["state"] == "done"
            assert urlopen(base + "/img/xresult").read()[:2] == b"\xff\xd8"
            assert "error" in post("/api/send", {})  # not fetched from PhotoCraft
            out = Path(post("/api/save", {})["path"])
            try:
                assert Image.open(out).size == (300, 300)
            finally:
                out.unlink()

            S.STATE["doc"] = {"index": 1, "name": "pic.png"}
            S.BRIDGE["b"] = type("B", (), {"send_expand": lambda self, layer, doc, m: {"document": "d", "width": 300, "height": 300}})()
            assert post("/api/send", {})["sent"]
            assert "error" in post("/api/run", {"ratio": [1, 1], "x": 0, "y": -50, "w": 300, "variations": 5})
            seeds.clear()
            post("/api/run", {"ratio": [1, 1], "x": 0, "y": -50, "w": 300, "variations": 3, "seed": 10})
            S.STATE["thread"].join(10)
            j = post("/api/job", {})
            assert j["state"] == "done" and j["count"] == 3 and seeds == [10, 11, 12], (j, seeds)
            assert "error" in post("/api/select", {"index": 3}) and post("/api/select", {"index": 2}) == {"selected": 2}
            out = Path(post("/api/save", {})["path"])
            try:
                assert Image.open(out).getpixel((150, 10)) == (0, 112, 0)  # variation 3 (seed 12)
            finally:
                out.unlink()

        # a run reports why it could not start
        with mock.patch.object(X, "status", lambda *a, **k: {**ok, "ok": False, "message": "ComfyUI lacks the nodes: X"}):
            post("/api/run", {"ratio": [1, 1], "x": 0, "y": -50, "w": 300})
            S.STATE["thread"].join(10)
            j = post("/api/job", {})
            assert j["state"] == "error" and "lacks the nodes" in j["message"]

        # starting ComfyUI: refuses when it already runs, when remote, or when not installed; otherwise spawns it
        with mock.patch.object(X, "status", lambda *a, **k: {**ok, "reachable": True}):
            assert post("/api/comfy/start", {})["started"] is False
        down = {**ok, "ok": False, "reachable": False}
        spawned = []
        fake_proc = type("P", (), {"poll": lambda self: None})()
        with mock.patch.object(X, "status", lambda *a, **k: down), mock.patch.object(S.subprocess, "Popen", lambda *a, **k: spawned.append((a, k)) or fake_proc):
            S.COMFY_PROC["p"] = None
            S.COMFY_DIR = tmp / "nocomfy"
            assert "no ComfyUI found" in post("/api/comfy/start", {})["error"]
            (tmp / "cf" / "venv" / "bin").mkdir(parents=True)
            (tmp / "cf" / "venv" / "bin" / "python").write_text("")
            (tmp / "cf" / "main.py").write_text("")
            S.COMFY_DIR = tmp / "cf"
            assert post("/api/comfy/start", {})["started"] and spawned[0][0][0][1:] == ["main.py", "--port", str(urlparse(X.COMFY_URL).port or 8188)]
            assert spawned[0][1]["start_new_session"] and not post("/api/comfy/start", {})["started"]  # still starting
            S.COMFY_PROC["p"] = None
            old, X.COMFY_URL = X.COMFY_URL, "http://gpu-box:8188"
            assert "not on this machine" in post("/api/comfy/start", {})["error"]
            X.COMFY_URL = old
        # settings and status endpoints
        with mock.patch.object(X, "status", lambda *a, **k: ok):
            st = post("/api/status", {})
        assert st["ok"] and st["settings"] == X.DEFAULTS and st["defaults"] == X.DEFAULTS
        assert post("/api/settings", {"values": {"steps": 20}})["steps"] == 20
        assert "error" in post("/api/settings", {"values": {"steps": 0}})
        assert post("/api/settings", {"reset": True}) == X.DEFAULTS
        # presets: snapshot the current settings, switch, delete
        S.PRESETS_FILE = tmp / "presets.json"
        assert post("/api/presets", {}) == {"presets": {}, "last": None}
        post("/api/settings", {"values": {"steps": 20, "guidance": 2.5}})
        r = post("/api/presets/save", {"name": "slow"})
        assert r["last"] == "slow" and r["presets"]["slow"]["steps"] == 20
        assert "error" in post("/api/presets/save", {"name": " "})
        assert post("/api/presets/load", {"name": None})["last"] is None and S.X.load_settings(S.SETTINGS_FILE) == X.DEFAULTS
        assert post("/api/presets/load", {"name": "slow"})["last"] == "slow" and S.X.load_settings(S.SETTINGS_FILE)["steps"] == 20
        assert "error" in post("/api/presets/load", {"name": "nope"})
        assert post("/api/presets", {})["last"] == "slow"  # remembered
        assert post("/api/presets/delete", {"name": "slow"}) == {"presets": {}, "last": None}
        assert "error" in post("/api/presets/delete", {"name": "slow"})
    finally:
        srv.shutdown()
        S.BRIDGE["b"] = None


def test_bridge_send_expand():
    import tempfile
    from shp_photocraft_tools.modules.crop import photocraft_bridge as B
    temporary = tempfile.TemporaryDirectory()
    tmp = Path(temporary.name)
    calls = []

    class FakeClient:
        def call(self, method, **p):
            calls.append((method, p))
            if method == "ui.inspect":
                return {"session": {"active": 2, "documents": [{"index": 1, "name": "pic.png", "width": 300, "height": 200},
                                                               {"index": 2, "name": "scratch", "width": 300, "height": 300}]}}
            return {}

        def run(self, command, **p):
            calls.append((command, p))
            if command == "layer.list":
                return {"layers": [{"id": 7}]}
            if command == "layer.copyToDocument":
                return {"layers": [9]}
            return {}

    b = B.Bridge(port=1, token_file=tmp / "t", exchange=tmp / "ex", read_root=tmp)
    b.client = FakeClient()
    layer = np.zeros((300, 300, 4), np.uint8)
    r = b.send_expand(layer, {"index": 1, "name": "pic.png"}, (0, 50, 0, 50))
    assert r["layer"] == 9 and (r["width"], r["height"]) == (300, 300)
    crop = [p for c, p in calls if c == "image.crop"]
    assert crop == [{"x": 0, "y": -50, "width": 300, "height": 300}], crop
    assert not list((tmp / "ex").iterdir())  # the exchange file is cleaned up
    try:
        b.send_expand(layer, {"index": 1, "name": "pic.png"}, (10, 0, 0, 0))  # starting size does not match the document
    except B.ControlError:
        pass
    else:
        raise AssertionError("size mismatch not detected")
    temporary.cleanup()


if __name__ == "__main__":
    import tempfile
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            with tempfile.TemporaryDirectory() as tmp:
                fn(Path(tmp)) if fn.__code__.co_argcount else fn()
            print("ok", name)
