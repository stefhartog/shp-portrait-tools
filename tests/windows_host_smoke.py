"""Real Windows host smoke run (not a unit test): supervisor + Skin Tools + WebView2 window + tray, fake dialogs.

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
    # Every Tools folder in the temp tree: data/state, global settings and the inbox.
    os.environ.update(SHP_TOOLS_DATA=str(root / "data"), SKIN_WATCH_DIRECTORY=str(inbox),
                      LOCALAPPDATA=str(root / "local"), APPDATA=str(root / "roaming"))
    config = root / "launcher.json"
    config.write_text(json.dumps({"models": args.models, "ports": {"skin": 18765}, "shell_port": 18760,
                                  "startup": {"skin": True}}))
    from shp_portrait_tools import launcher
    from shp_portrait_tools.desktop import windows_controller as wc

    for port in (18760, 18765):
        if listening(port):
            sys.exit(f"port {port} already in use; refusing to run")
    launcher.launch(config, no_browser=True)
    check("supervisor listening", wait_for(lambda: listening(18760)))
    picks = {"image": str(root / "pick.png"), "folder": str(root), "images": [str(root / "a.png"), str(root / "b.png")]}

    class FakeDialogs:
        def confirm(self, *a):
            return True
        def error(self, *a):
            print("ERRORBOX", a, flush=True)

    class FakePicker:
        def __init__(self, window):
            pass
        def image(self): return picks["image"]
        def images(self): return picks["images"]
        def folder(self): return picks["folder"]

    host = wc.Host("http://127.0.0.1:18760/", root / "data" / "webview", dialogs=FakeDialogs(), picker_factory=FakePicker)

    def js(code):
        return host.window.evaluate_js(code)

    def driver():
        try:
            check("window reached the Skin Tools page", wait_for(lambda: js("location.origin") == host.page, 60),
                  str(js("location.href")))
            check("__shpOwnedDesktop", js("window.__shpOwnedDesktop === true"))
            check("__shpNativeFileDialog", js("window.__shpNativeFileDialog === true"))
            check("tabs in the page", js("document.querySelectorAll('#viewTabs [role=tab]').length") == 5)
            check("tray created", wait_for(lambda: host.tray_ready, 15))
            reach = js("(()=>{try{return desktopPicker()!==null}catch(e){return 'x:'+e}})()")
            check("page reaches the JS picker bridge", reach is True, str(reach))
            for method, expected in (("pick_image", picks["image"]), ("pick_folder", picks["folder"]),
                                     ("pick_images", picks["images"])):
                answer = js(f"window.pywebview.api.{method}()")
                print("  picker", method, "->", answer, flush=True)
                check("picker " + method, answer == expected)
            check("old prompt is cancelled", js("window.prompt('SHP_OPEN_IMAGE_V1')") is None)
            host.hide()
            time.sleep(1)
            check("hide to tray", host.window.native.Visible is False)
            host.show()
            time.sleep(1)
            check("show", host.window.native.Visible is True)
            host.window.native.Close  # noqa: B018 - closing handled via on_closing
            check("modules reported", host.modules.get("skin", {}).get("phase") == "running", str(host.modules.get("skin", {}).get("phase")))
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
