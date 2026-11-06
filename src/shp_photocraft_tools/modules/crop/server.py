"""SHP PhotoCraft Crop: local web UI that grows a picture and lets your ComfyUI fill the new area.

Run: uv run shp-photocraft-crop [--port 8768] [--comfy URL] [--dir pictures ...]
Then open http://127.0.0.1:8768. The server only listens on 127.0.0.1.

Pictures come from PhotoCraft (Get from PhotoCraft: the active document, merged) or from the --dir folders. Send to
PhotoCraft grows that document's canvas and adds the generated area as one layer (one undo step). Save writes the
expanded picture as PNG to $SHPC_DATA/results (default ~/.local/share/shp-photocraft-tools/crop/results).
"""
import argparse
import datetime
import io
import json
import os
import subprocess
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
from PIL import Image
from shp_photocraft_tools.shared.ui import stylesheet, theme_script
from shp_photocraft_tools.shared.health import identity
from shp_photocraft_tools.shared.lifecycle import BrowserHandler, BrowserServer, script

from . import expand as X
from . import photocraft_bridge as PCB

HTML = Path(__file__).resolve().parent / "ui" / "index.html"
DATA = Path(os.environ.get("SHPC_DATA", Path.home() / ".local" / "share" / "shp-photocraft-tools" / "crop")).expanduser()
SETTINGS_FILE = DATA / "settings.json"
PRESETS_FILE = DATA / "presets.json"
IMG_EXT = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp"}
LOCK = threading.Lock()
DIRS = []
BRIDGE = {"b": None}
STATE = {"img": None, "name": None, "doc": None, "job": X.Job(), "thread": None, "seq": 0, "imgs": {}}


def bridge():
    if BRIDGE["b"] is None:
        BRIDGE["b"] = PCB.Bridge()
    return BRIDGE["b"]


def list_images():
    out = []
    for d in DIRS:
        for p in sorted(Path(d).expanduser().glob("*")):
            if p.suffix.lower() in IMG_EXT:
                out.append({"name": p.name, "path": str(p)})
    return out


def jpeg_bytes(im, q=90, max_side=None):
    im = im.convert("RGB")
    if max_side and max(im.size) > max_side:
        k = max_side / max(im.size)
        im = im.resize((max(1, round(im.width * k)), max(1, round(im.height * k))), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=q)
    return buf.getvalue()


def running():
    return STATE["thread"] is not None and STATE["thread"].is_alive()


def api_load(req):
    path = Path(req["path"]).expanduser()
    if path.suffix.lower() not in IMG_EXT or not path.is_file():
        raise ValueError(f"not an image file: {path}")
    if running():
        raise ValueError("wait for the running expansion to finish or cancel it")
    with Image.open(path) as im:
        im.load()
        rgb = X.to_srgb(im)
    STATE.update(img=rgb, name=path.name, doc=None, seq=STATE["seq"] + 1, job=X.Job())
    STATE["imgs"]["xorig"] = jpeg_bytes(rgb, 92, 2400)
    for k in [k for k in STATE["imgs"] if k.startswith("xvar")]:
        del STATE["imgs"][k]
    STATE["imgs"].pop("xresult", None)
    return {"name": path.name, "width": rgb.width, "height": rgb.height, "seq": STATE["seq"],
            "ratios": X.RATIOS, "limit": X.EXPAND_LIMIT}


def api_fetch(req):
    if running():
        raise ValueError("wait for the running expansion to finish or cancel it")
    path, doc = bridge().fetch(DATA / "inbox")
    res = api_load({"path": str(path)})
    STATE["doc"] = doc
    res["pc_doc"] = doc
    return res


def api_run(req):
    if STATE["img"] is None:
        raise ValueError("load a picture first")
    if running():
        raise ValueError("an expansion is already running")
    img = STATE["img"]
    margins, size = X.frame_margins(img.width, img.height, req.get("ratio"), req["x"], req["y"], req["w"], req.get("h"))
    job = X.Job()
    job.state, job.text = "running", "Contacting ComfyUI..."
    STATE["job"] = job
    STATE["imgs"].pop("xresult", None)
    prompt, seed = str(req.get("prompt", "")), int(req.get("seed", -1))
    settings = X.load_settings(SETTINGS_FILE)
    variations = int(req.get("variations", 1))
    if not 1 <= variations <= X.MAX_VARIATIONS:
        raise ValueError(f"variations must be between 1 and {X.MAX_VARIATIONS}")
    for k in [k for k in STATE["imgs"] if k.startswith("xvar")]:
        del STATE["imgs"][k]

    def publish(index, result):
        preview = jpeg_bytes(result["canvas"], 92, 2400)
        STATE["imgs"][f"xvar{index}"] = preview
        if index == 0:
            STATE["imgs"]["xresult"] = preview

    def work():
        try:
            X.expand_variations(img, margins, prompt, job, seed, settings, variations, on_result=publish)
            with job.lock:
                job.state, job.fraction, job.text, job.message = "done", 1.0, "Done", ""
        except Exception as e:  # shown on the page
            cancelled = job.cancel or isinstance(e, X.Cancelled)
            with job.lock:
                job.state = "cancelled" if cancelled else "error"
                if cancelled:
                    job.message = (f"Cancelled. Kept {len(job.results)} completed result(s)." if job.results
                                   else "Cancelled: nothing was changed.")
                else:
                    job.message = str(e) if isinstance(e, X.ExpandError) else f"{type(e).__name__}: {e}"

    STATE["thread"] = threading.Thread(target=work, daemon=True)
    STATE["thread"].start()
    return {"margins": margins, "size": size}


def api_job(req):
    return STATE["job"].snapshot()


def api_cancel(req):
    job = STATE["job"]
    with job.lock:
        job.cancel = True
        job.text = "Cancelling..."
    return {}


def api_select(req):
    """Choose which finished variation Send and Save use; the main view shows it."""
    job = STATE["job"]
    i = int(req.get("index", -1))
    with job.lock:
        if not job.results or not 0 <= i < len(job.results):
            raise ValueError("no such variation")
        job.selected, job.result = i, job.results[i]
        canvas = job.result["canvas"]
    STATE["imgs"]["xresult"] = jpeg_bytes(canvas, 92, 2400)
    return {"selected": i}


def api_send(req):
    res = STATE["job"].result
    if res is None:
        raise ValueError("expand the picture first")
    if STATE["doc"] is None:
        raise ValueError("get a picture from PhotoCraft first")
    return {"sent": True, **bridge().send_expand(np.asarray(res["layer"], np.uint8), STATE["doc"], res["margins"])}


def api_save(req):
    res = STATE["job"].result
    if res is None:
        raise ValueError("expand the picture first")
    out = DATA / "results" / f"{Path(STATE['name']).stem}_expand_{datetime.datetime.now():%Y%m%d_%H%M%S}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    res["canvas"].save(out)
    return {"path": str(out)}


def api_status(req):
    """Is ComfyUI reachable and complete; plus the settings and the model lists for the page."""
    settings = X.load_settings(SETTINGS_FILE)
    return {**X.status(settings), "settings": settings, "defaults": X.DEFAULTS, "comfy": X.COMFY_URL}


COMFY_DIR = Path(os.environ.get("SHPC_COMFY_DIR", "~/ComfyUI")).expanduser()
COMFY_PROC = {"p": None}


def api_comfy_start(req):
    """Start the local ComfyUI in the background (own session, so it keeps running if this page's server stops)."""
    if os.environ.get("SHPC_TOOLS_COMFY") == "1":
        raise ValueError("Tools owns this ComfyUI instance. Use Crop through the Tools shell to start/retry it.")
    if X.status(X.load_settings(SETTINGS_FILE))["reachable"]:
        return {"started": False, "message": "ComfyUI is already running"}
    p = COMFY_PROC["p"]
    if p and p.poll() is None:
        return {"started": False, "message": "ComfyUI is still starting"}
    u = urllib.parse.urlparse(X.COMFY_URL)
    if u.hostname not in ("127.0.0.1", "localhost"):
        raise ValueError(f"ComfyUI at {X.COMFY_URL} is not on this machine; start it there")
    py = next((COMFY_DIR / v / "bin" / "python" for v in ("venv", ".venv") if (COMFY_DIR / v / "bin" / "python").exists()), None)
    if py is None or not (COMFY_DIR / "main.py").exists():
        raise ValueError(f"no ComfyUI found in {COMFY_DIR} (set SHPC_COMFY_DIR)")
    DATA.mkdir(parents=True, exist_ok=True)
    with open(DATA / "comfy.log", "ab") as log:
        COMFY_PROC["p"] = subprocess.Popen([str(py), "main.py", "--port", str(u.port or 8188)], cwd=COMFY_DIR, stdout=log,
                                           stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
    return {"started": True, "message": "Starting ComfyUI"}


def api_settings(req):
    if (req.get("values") or req.get("reset")) and running():
        raise ValueError("wait for the running expansion to finish or cancel it")
    return X.fill_settings(SETTINGS_FILE, req.get("values") or None, bool(req.get("reset")))


def read_presets():
    try:
        d = json.loads(PRESETS_FILE.read_text())
    except (OSError, ValueError):
        d = {}
    presets = d.get("presets") if isinstance(d.get("presets"), dict) else {}
    last = d.get("last") if d.get("last") in presets else None
    return {"presets": presets, "last": last}


def write_presets(d):
    PRESETS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = PRESETS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=2))
    tmp.replace(PRESETS_FILE)


def api_presets(req):
    return read_presets()


def api_preset_save(req):
    """Store the current settings under a name (new or overwrite) and make it the selected preset."""
    name = str(req.get("name", "")).strip()
    if not name or len(name) > 60:
        raise ValueError("give the preset a name (1-60 characters)")
    d = read_presets()
    d["presets"][name] = X.load_settings(SETTINGS_FILE)
    d["last"] = name
    write_presets(d)
    return d


def api_preset_load(req):
    """Make a saved preset the current settings (None = factory settings)."""
    if running():
        raise ValueError("wait for the running expansion to finish or cancel it")
    d = read_presets()
    name = req.get("name")
    if name is None:
        X.fill_settings(SETTINGS_FILE, reset=True)
    elif name in d["presets"]:
        X.fill_settings(SETTINGS_FILE, {**X.DEFAULTS, **d["presets"][name]})
    else:
        raise ValueError("no such preset")
    d["last"] = name
    write_presets(d)
    return d


def api_preset_delete(req):
    d = read_presets()
    if req.get("name") not in d["presets"]:
        raise ValueError("no such preset")
    del d["presets"][req["name"]]
    if d["last"] == req["name"]:
        d["last"] = None
    write_presets(d)
    return d


ROUTES = {"/api/comfy/start": api_comfy_start, "/api/presets": api_presets, "/api/presets/save": api_preset_save, "/api/presets/load": api_preset_load, "/api/presets/delete": api_preset_delete, "/api/select": api_select, "/api/settings": api_settings, "/api/status": api_status, "/api/load": api_load, "/api/fetch": api_fetch, "/api/run": api_run, "/api/job": api_job,
          "/api/cancel": api_cancel, "/api/send": api_send, "/api/save": api_save}
NO_LOCK = {"/api/job", "/api/cancel"}  # must answer while a run is going


class Handler(BrowserHandler, BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/api/health":
            return self.send(200, json.dumps({**identity("crop", DATA),
                "ui_lifecycle": 1 if getattr(self.server, "lifetime", None) else 0}).encode(), "application/json")
        if path == "/static/lifecycle.js":
            return self.send(200, script(), "text/javascript; charset=utf-8")
        if path in ("/", "/index.html"):
            return self.send(200, HTML.read_bytes(), "text/html; charset=utf-8")
        if path == "/static/shp-ui.css":
            return self.send(200, stylesheet(), "text/css; charset=utf-8")
        if path == "/static/theme.js":
            return self.send(200, theme_script(), "text/javascript; charset=utf-8")
        if path == "/api/images":
            return self.send(200, json.dumps(list_images()).encode(), "application/json")
        if path in ("/img/xorig", "/img/xresult") or (path.startswith("/img/xvar") and path[9:].isdigit()):
            data = STATE["imgs"].get(path[5:])
            if data is None:
                return self.send(404, b"no image yet", "text/plain")
            return self.send(200, data, "image/jpeg")
        self.send(404, b"not found", "text/plain")

    def do_api_POST(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(n) or b"{}")
            fn = ROUTES.get(self.path)
            if fn is None:
                return self.send(404, b"not found", "text/plain")
            if self.path in NO_LOCK:
                res = fn(req)
            else:
                with LOCK:
                    res = fn(req)
            self.send(200, json.dumps(res).encode(), "application/json")
        except Exception as e:  # report to the page instead of crashing the server
            self.send(400, json.dumps({"error": f"{type(e).__name__}: {e}"}).encode(), "application/json")


def main():
    ap = argparse.ArgumentParser(description="SHP PhotoCraft Crop web UI")
    ap.add_argument("--port", type=int, default=8768)
    ap.add_argument("--close-with-ui", action="store_true", help="stop after the last UI tab closes (launcher use)")
    ap.add_argument("--comfy", default=None, help="ComfyUI address (default http://127.0.0.1:8188 or $SHPC_COMFY)")
    ap.add_argument("--dir", nargs="*", default=[], help="folders whose pictures can be opened from the page")
    a = ap.parse_args()
    DIRS.extend(a.dir)
    if a.comfy:
        X.COMFY_URL = a.comfy.rstrip("/")
    srv = BrowserServer(("127.0.0.1", a.port), Handler, close_with_ui=a.close_with_ui,
                        busy=running, loaded=lambda: STATE["img"] is not None)
    print(f"SHP PhotoCraft Crop: http://127.0.0.1:{srv.server_port}  (Ctrl+C to stop)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
