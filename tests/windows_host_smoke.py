"""Real Windows host smoke run (not a unit test): shell + Skin Tools + WebView2 window + tray, fake dialogs.

Usage: python tests/windows_host_smoke.py --models <dir with the three Skin Tools onnx files>
Everything lives in a temp tree; the services are stopped through the host's own Quit path.
"""

import argparse
import json
import os
import socket
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok)))
    print(("PASS " if ok else "FAIL ") + name + (f" - {detail}" if detail else ""), flush=True)


def listening(port):
    with socket.socket() as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def wait_for(predicate, seconds=30):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            value = predicate()
        except Exception:
            value = None
        if value:
            return value
        time.sleep(0.25)
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--models", required=True)
    args = parser.parse_args()
    root = Path(tempfile.mkdtemp(prefix="shp-smoke-"))
    inbox = root / "inbox"
    inbox.mkdir()
    os.environ.update(SHP_TOOLS_DATA=str(root / "data"), DNB_WATCH_DIRECTORY=str(inbox))
    config = root / "launcher.json"
    config.write_text(json.dumps({"models": args.models, "ports": {"dnb": 18765}, "shell_port": 18760,
                                  "startup": {"dnb": True}}))
    from shp_photocraft_tools import launcher
    from shp_photocraft_tools.desktop import windows_controller as wc

    for port in (18760, 18765):
        if listening(port):
            sys.exit(f"port {port} already in use; refusing to run")
    launcher.launch(config, no_browser=True)
    check("shell listening", wait_for(lambda: listening(18760)))
    picks = {"image": str(root / "pick.png"), "folder": str(root), "images": [str(root / "a.png"), str(root / "b.png")]}

    class FakeDialogs:
        def confirm(self, *a):
            return True
        def error(self, *a):
            print("ERRORBOX", a, flush=True)
        def batch_source(self):
            return "folder"

    class FakePicker:
        def __init__(self, window):
            pass
        def image(self): return picks["image"]
        def images(self): return picks["images"]
        def folder(self): return picks["folder"]
        def batch_source(self): return "images"

    host = wc.Host("http://127.0.0.1:18760/", root / "data" / "webview", dialogs=FakeDialogs(), picker_factory=FakePicker)

    def js(code):
        return host.window.evaluate_js(code)

    def driver():
        try:
            check("window loaded shell", wait_for(lambda: js("location.origin") == "http://127.0.0.1:18760", 40))
            check("__shpOwnedDesktop", js("window.__shpOwnedDesktop === true"))
            check("__shpNativeFileDialog", js("window.__shpNativeFileDialog === true"))
            check("shell title", True, js("document.title"))
            check("tray created", wait_for(lambda: host.tray_ready, 15))
            frame = wait_for(lambda: js("(document.querySelector('iframe')||{}).src || ''"), 30)
            check("Skin Tools iframe", frame and "/dnb/" in frame, str(frame))
            if frame:
                time.sleep(4)
                inner = js("(()=>{const f=document.querySelector('iframe');try{return f.contentDocument.title+'|'+f.contentWindow.__shpNativeFileDialog}catch(e){return 'x:'+e}})()")
                check("iframe native flag", str(inner).endswith("|true"), str(inner))
            for request in ("SHP_OPEN_IMAGE_V1", "SHP_OPEN_BATCH_FOLDER_V1", "SHP_OPEN_BATCH_IMAGES_V1", "SHP_OPEN_BATCH_SOURCE_V1"):
                answer = js(f"window.prompt('{request}')")
                print("  prompt", request, "->", answer, flush=True)
                expected = {"SHP_OPEN_IMAGE_V1": picks["image"], "SHP_OPEN_BATCH_FOLDER_V1": picks["folder"],
                            "SHP_OPEN_BATCH_IMAGES_V1": json.dumps(picks["images"]),
                            "SHP_OPEN_BATCH_SOURCE_V1": json.dumps({"paths": picks["images"]})}[request]
                check("prompt " + request, answer == expected)
            host.hide()
            time.sleep(1)
            check("hide to tray", host.window.native.Visible is False)
            host.show()
            time.sleep(1)
            check("show", host.window.native.Visible is True)
            host.window.native.Close  # noqa: B018 - closing handled via on_closing
            check("modules reported", host.modules.get("dnb", {}).get("phase") == "running", str(host.modules.get("dnb", {}).get("phase")))
            host.request_quit()
            check("services stopped on quit", wait_for(lambda: not listening(18765) and not listening(18760), 60))
        except Exception:
            traceback.print_exc()
            check("driver", False)
            host.quitting = True
            try:
                host.window.destroy()
            except Exception:
                pass

    threading.Thread(target=driver, daemon=True).start()
    host.run()
    time.sleep(2)
    check("no listener 18765", not listening(18765))
    check("no listener 18760", not listening(18760))
    failed = [name for name, ok in results if not ok]
    print("FAILED:" if failed else "ALL PASSED", failed, flush=True)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
