"""System-Python real Wayland/WebKit smoke. Called by test_desktop with synthetic services."""

import importlib.util
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

HELPER = Path(sys.argv[1])
URL = sys.argv[2]
ROOT = Path(sys.argv[3])
spec = importlib.util.spec_from_file_location("tools_desktop", HELPER)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
Gtk, GLib, Gdk, WebKit2 = module.Gtk, module.GLib, module.Gdk, module.WebKit2


class Smoke(module.Desktop):
    def __init__(self):
        super().__init__(URL, ROOT / "webview")
        self.failures = []
        self.steps = []
        self.started = time.monotonic()
        self.phase = "ready"
        self.pending = False
        self.dialog_action = None
        self.activations = 0
        self.second = None
        self.original_request = self.connection.request

    def do_activate(self):
        super().do_activate()
        self.activations += 1
        if self.activations == 1:
            GLib.timeout_add(100, self.tick)
            GLib.timeout_add(100, self.dialogs)

    def evaluate(self, script, callback):
        self.pending = True

        def complete(view, result, *_):
            self.pending = False
            try:
                value = view.evaluate_javascript_finish(result)
                parsed = json.loads(value.to_string())
                callback(parsed)
            except Exception as error:
                self.fail(error)

        self.view.evaluate_javascript(f"JSON.stringify((()=>{{{script}}})())", -1, None, self.base, None, complete, None)

    def fail(self, error):
        self.failures.append(str(error))
        print("Desktop smoke failure:", error, file=sys.stderr, flush=True)
        self.finish_quit()

    def expect(self, condition, message):
        if not condition:
            raise AssertionError(message)

    def tick(self):
        if self.quitting:
            return GLib.SOURCE_REMOVE
        if time.monotonic() - self.started > 100:
            self.fail(f"Timeout in {self.phase}")
            return GLib.SOURCE_REMOVE
        if self.pending:
            return GLib.SOURCE_CONTINUE
        try:
            self.advance()
        except Exception as error:
            self.fail(error)
        return GLib.SOURCE_CONTINUE

    def dialogs(self):
        if self.quitting:
            return GLib.SOURCE_REMOVE
        if not self.dialog_action:
            return GLib.SOURCE_CONTINUE
        for window in Gtk.Window.list_toplevels():
            if not isinstance(window, Gtk.Dialog) or not window.get_visible():
                continue
            action = self.dialog_action
            self.dialog_action = None
            if action == "quit-changed":
                self.original_request("/crop/api/fetch", {})
            if isinstance(window, Gtk.FileChooserDialog):
                window.set_current_folder(str(ROOT))
                window.set_current_name("test-download.txt")
                GLib.timeout_add(150, lambda: (window.response(Gtk.ResponseType.ACCEPT), False)[1])
                return GLib.SOURCE_CONTINUE
            for child in window.get_content_area().get_children():
                if isinstance(child, Gtk.Entry):
                    child.set_text("WebKit fixture preset")
            window.response(Gtk.ResponseType.ACCEPT if action in ("quit", "quit-changed", "control") else
                            Gtk.ResponseType.CANCEL if action == "cancel" else Gtk.ResponseType.OK)
            if action == "quit-changed" and self.control_pending:
                self.dialog_action = "ok"
            return GLib.SOURCE_CONTINUE
        return GLib.SOURCE_CONTINUE

    def advance(self):
        if self.phase == "ready":
            self.evaluate("""
                const frames=[...document.querySelectorAll('iframe')];
                const crop=frames.find(f=>f.title.endsWith('Crop'))?.contentDocument;
                const dnb=frames.find(f=>f.title.endsWith('D&B'));
                const toggles=['slora','sseam_correction'].map(id=>{
                  const input=crop?.getElementById(id);
                  if(!input)return null;
                  const row=input.parentElement,box=input.getBoundingClientRect(),bounds=row.getBoundingClientRect();
                  return {right:bounds.right-box.right,top:bounds.top,bottom:bounds.bottom,
                    label:!!row.querySelector(`label[for="${id}"]`)};
                });
                return {ready:frames.length===2&&frames.every(f=>f.contentDocument?.readyState==='complete'
                    &&f.contentDocument.getElementById(f.title.endsWith('Crop')?'xGet':'pcGet')),
                  cropInfo:crop?.getElementById('xInfo')?.textContent,toggles,
                  modelTop:crop?.getElementById('sunet')?.parentElement.getBoundingClientRect().top,
                  native:window.__shpOwnedDesktop,crypto:typeof crypto.randomUUID,beacon:typeof navigator.sendBeacon,
                  repeatedTitle:!!document.querySelector('.shell-header strong'),
                  dnbHeaders:[...(dnb?.contentDocument?.querySelectorAll('details.grp > summary')||[])].map(summary=>({
                    arrow:dnb.contentWindow.getComputedStyle(summary,'::after').display,
                    label:summary.querySelector('label')?.textContent.trim()||''})),
                  switches:frames.map(f=>[...f.contentDocument.querySelectorAll('input[type=checkbox]')].map(input=>{
                    const style=f.contentWindow.getComputedStyle(input);
                    return {appearance:style.appearance,width:style.width,height:style.height,
                      background:style.backgroundColor,checked:input.checked,disabled:input.disabled,
                      transitioning:input.getAnimations().some(animation=>animation.playState==='running'),
                      cursor:style.cursor,opacity:style.opacity};
                  })),
                  barBackground:document.querySelector('.shell-header')
                    ?getComputedStyle(document.querySelector('.shell-header')).backgroundColor:null,
                  tabs:[...document.querySelectorAll('[role=tab]')].map(t=>({
                    radius:getComputedStyle(t).borderTopLeftRadius,
                    stripe:getComputedStyle(t,'::before').content,
                    bottom:getComputedStyle(t).borderBottomColor,
                    background:getComputedStyle(t).backgroundColor})),
                  selects:frames.map(f=>{const s=f.contentDocument.querySelector('select');
                    if(!s)return null;
                    const style=f.contentWindow.getComputedStyle(s);
                    const preset=f.contentDocument.getElementById('presetSel');
                    return {scheme:style.colorScheme,appearance:style.appearance,background:style.backgroundColor,
                      presetHeight:preset.getBoundingClientRect().height,
                      buttonHeights:[...f.contentDocument.querySelectorAll('#presetBar button')]
                        .map(b=>b.getBoundingClientRect().height)};})};
                """, self.ready)
        elif self.phase == "load":
            self.evaluate("""
                const c=document.querySelector('iframe[title="SHP PhotoCraft Crop"]');
                const d=document.querySelector('iframe[title="SHP PhotoCraft D&B"]');
                return {loaded:c.contentWindow.eval('!!X.img')&&d.contentWindow.eval('state.version>0&&!state.busy'),
                        error:c.contentDocument.getElementById('status').textContent};
                """, self.loaded)
        elif self.phase == "result":
            self.evaluate("""
                const c=window.fixtureCrop;
                return {done:c.contentWindow.eval('X.hasResult&&!X.running'),
                        status:c.contentDocument.getElementById('status').textContent};
                """, self.result)
        elif self.phase == "dialog":
            self.dialog_action = "ok"
            self.phase = "dialog-wait"
            self.evaluate("""
                const v=prompt('Fixture prompt','initial'); window.fixturePrompt=v;
                return {value:v};
                """, lambda r: self.after_dialog(r))
        elif self.phase == "pointer-check":
            self.evaluate("""
                return {events:window.fixtureCrop.contentWindow.pointerEvents||[],
                        capture:window.fixtureCrop.contentWindow.didCapture||false};
                """, self.pointer_checked)
        elif self.phase == "download":
            self.dialog_action = "save"
            self.phase = "download-wait"
            self.evaluate("""
                const a=document.createElement('a');a.href=URL.createObjectURL(new Blob(['fixture-download']));
                a.download='fixture.txt';a.click();return true;
                """, lambda _: None)
        elif self.phase == "download-wait":
            path = ROOT / "test-download.txt"
            if path.exists():
                self.expect(path.read_text() == "fixture-download", "Download content mismatch")
                self.steps.append("download destination chooser and file")
                self.phase = "hide"
        elif self.phase == "hide":
            self.evaluate("""
                const c=window.fixtureCrop,d=window.fixtureDnb;
                window.beforeHide={crop:c.contentWindow.eval('JSON.stringify({seq:X.seq,rv:X.rv,view:X.view})'),
                  dnb:d.contentWindow.eval('JSON.stringify({view,version:state.version})')};
                return true;
                """, lambda _: self.hidden())
        elif self.phase == "hidden":
            if time.monotonic() - self.hidden_at > 17:
                self.expect(not self.window.get_visible(), "Hidden window reappeared unexpectedly")
                self.expect(self.connection.request("/api/status")["modules"]["crop"]["phase"] == "running",
                            "Hiding lost controller lease")
                self.steps.append("hidden window retained services beyond close grace")
                self.second = subprocess.Popen(
                    ["/usr/bin/python3", str(HELPER), "--url", URL, "--data-dir", str(ROOT / "webview")],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                self.phase = "reopen"
        elif self.phase == "reopen":
            if self.second.poll() is not None:
                out, err = self.second.communicate()
                self.expect(self.second.returncode == 0, f"Remote activation failed: {out} {err}")
                self.expect(self.activations == 2 and self.window.get_visible(), "Second launch did not present same instance")
                self.evaluate("""
                    return {crop:fixtureCrop===document.querySelector('iframe[title="SHP PhotoCraft Crop"]')
                      &&beforeHide.crop===fixtureCrop.contentWindow.eval('JSON.stringify({seq:X.seq,rv:X.rv,view:X.view})'),
                      dnb:fixtureDnb===document.querySelector('iframe[title="SHP PhotoCraft D&B"]')
                      &&beforeHide.dnb===fixtureDnb.contentWindow.eval('JSON.stringify({view,version:state.version})'),
                      local:localStorage.getItem('native-fixture')};
                    """, self.reopened)
                self.phase = "reopen-wait"
        elif self.phase == "quit-cancel":
            self.dialog_action = "cancel"
            self.request_quit()
            self.phase = "quit-cancel-wait"
        elif self.phase == "quit-cancel-wait":
            if not self.dialog_open:
                self.expect(not self.quitting, "Quit ignored cancellation")
                self.steps.append("unsent-work Quit cancellation")
                self.dialog_action = "quit-changed"
                self.request_quit()
                self.phase = "quit-changed-wait"
        elif self.phase == "quit-changed-wait":
            if not self.dialog_open:
                self.expect(not self.quitting and "Work changed" in self.message_bar.get_text(),
                            "Quit confirmation accepted stale work")
                self.steps.append("changed-work Quit confirmation rejected")
                self.original_request("/crop/api/test/job", {"seconds": 1.5})
                self.request_quit()
                self.phase = "quit-busy-wait"
        elif self.phase == "quit-busy-wait":
            if not self.dialog_open:
                self.expect(not self.quitting and "Cannot quit" in self.message_bar.get_text(), "Busy quit not blocked")
                self.steps.append("busy Quit guard")
                self.phase = "wait-job"
        elif self.phase == "wait-job":
            if not module.busy_modules(self.original_request("/api/status")):
                self.before_recovery = module.work_signature(self.original_request("/api/status"))
                self.view.terminate_web_process()
                self.phase = "renderer-failed"
        elif self.phase == "renderer-failed":
            if self.reload_available:
                self.expect(self.reload_button.get_visible(), "Renderer recovery control is hidden")
                self.dialog_action = "cancel"
                self.request_quit("reload")
                self.phase = "reload-cancel"
        elif self.phase == "reload-cancel":
            if not self.dialog_open:
                self.expect(self.reload_available and not self.quitting, "Reload ignored cancellation")
                self.dialog_action = "quit"
                self.request_quit("reload")
                self.phase = "reload-wait"
        elif self.phase == "reload-wait":
            if not self.dialog_open and not self.reload_available:
                self.evaluate("""
                    const frames=[...document.querySelectorAll('iframe')];
                    const ready=frames.length===2&&frames.every(f=>f.contentDocument?.readyState==='complete'
                        &&f.contentDocument.getElementById(f.title.endsWith('Crop')?'xGet':'pcGet'));
                    return {ready,stored:ready?localStorage.getItem('native-fixture'):null};
                    """, self.recovered)
        elif self.phase == "tray-cancel-wait":
            if not self.control_pending:
                self.expect(self.original_request("/api/status")["modules"]["crop"]["instance"] == self.tray_crop,
                            "Canceled tray Stop changed module")
                self.dialog_action = "quit-changed"
                self.request_module("crop", "stop")
                self.phase = "tray-stale-wait"
        elif self.phase == "tray-stale-wait":
            if not self.control_pending:
                self.expect("changed" in self.message_bar.get_text().lower(), "Stale tray confirmation was accepted")
                self.expect(self.original_request("/api/status")["modules"]["crop"]["instance"] == self.tray_crop,
                            "Stale tray confirmation stopped module")
                self.modules["crop"]["instance"] = "wrong-instance-fixture"
                self.dialog_action = "ok"
                self.request_module("crop", "stop")
                self.phase = "tray-instance-wait"
        elif self.phase == "tray-instance-wait":
            if not self.control_pending:
                self.expect("instance changed" in self.message_bar.get_text().lower(),
                            "Wrong module instance was not refused")
                self.modules = self.original_request("/api/status")["modules"]
                self.original_request("/crop/api/test/job", {"seconds": 1.5})
                self.dialog_action = "ok"
                self.request_module("crop", "restart")
                self.phase = "tray-busy-wait"
        elif self.phase == "tray-busy-wait":
            if not self.control_pending:
                self.expect("busy" in self.message_bar.get_text().lower(), "Busy tray action was not refused")
                self.phase = "tray-job-wait"
        elif self.phase == "tray-job-wait":
            if not module.busy_modules(self.original_request("/api/status")):
                self.dialog_action = "control"
                self.request_module("crop", "restart")
                self.phase = "tray-restart-wait"
        elif self.phase == "tray-restart-wait":
            if not self.control_pending:
                state = self.original_request("/api/status")["modules"]
                self.expect(state["crop"]["instance"] != self.tray_crop and state["crop"]["phase"] == "running",
                            "Tray Restart failed")
                self.expect(state["dnb"]["instance"] == self.tray_dnb, "Tray Restart changed other module")
                self.original_request("/crop/api/fetch", {})
                self.dialog_action = "control"
                self.request_module("crop", "stop")
                self.phase = "tray-stop-wait"
        elif self.phase == "tray-stop-wait":
            if not self.control_pending:
                self.expect(self.modules["crop"]["phase"] == "stopped", "Tray Stop failed")
                self.expect(self.module_actions["crop"]["start"].get_sensitive()
                            and not self.module_actions["crop"]["stop"].get_sensitive(), "Stopped tray actions wrong")
                self.module_actions["crop"]["start"].activate()
                self.phase = "tray-start-wait"
        elif self.phase == "tray-start-wait":
            if not self.control_pending:
                self.expect(self.modules["crop"]["phase"] == "running", "Tray Start failed")
                self.expect(self.original_request("/api/status")["modules"]["dnb"]["instance"] == self.tray_dnb,
                            "Tray controls changed D&B")
                self.saved_control = self.connection.control
                self.connection.control = lambda *_: (False, {"code": "startup", "error": "Fixture startup failure; see crop.log"})
                self.dialog_action = "ok"
                self.request_module("crop", "start")
                self.phase = "tray-failure-wait"
        elif self.phase == "tray-failure-wait":
            if not self.control_pending:
                self.expect("crop.log" in self.message_bar.get_text(), "Start failure was not surfaced")
                self.connection.control = self.unreachable_control
                self.dialog_action = "ok"
                self.request_module("crop", "stop")
                self.phase = "tray-unreachable-wait"
        elif self.phase == "tray-unreachable-wait":
            if not self.control_pending:
                self.connection.control = self.saved_control
                self.expect("Supervisor request failed" in self.message_bar.get_text(), "Connection error not surfaced")
                self.expect(not any(b.get_sensitive() for actions in self.module_actions.values() for b in actions.values()),
                            "Disconnected tray controls enabled")
                self.phase = "tray-status-wait"
        elif self.phase == "tray-status-wait":
            if self.modules.get("crop", {}).get("phase") == "running":
                self.expect(self.module_items["crop"].get_label() == "Crop - Running", "Live tray status not refreshed")
                self.expect(self.module_items["dnb"].get_label().startswith("D&B - "), "D&B tray status missing")
                self.steps.append("real tray submenus, live refresh, Start/Stop/Restart, canceled/stale/busy guards, "
                                  "module isolation and unreachable supervisor error/reconnect")
                self.phase = "no-tray"
        elif self.phase == "no-tray":
            available = self.tray_available
            self.tray_available = lambda: False
            self.hide()
            self.expect(self.window.get_visible(), "No-tray hide made app inaccessible")
            self.tray_available = available
            self.steps.append("tray unavailable fallback remains visible")
            self.dialog_action = "quit"
            self.phase = "quit-final"
            self.request_quit()

    def ready(self, result):
        if not result["ready"]:
            return
        if any(switch["transitioning"] for switches in result["switches"] for switch in switches):
            return
        self.expect(result["native"] and result["crypto"] == "function" and result["beacon"] == "function",
                    f"Missing browser APIs: {result}")
        self.expect(not result["repeatedTitle"], "Repeated shell title remains")
        self.expect(len(result["dnbHeaders"]) == 6
                    and all(header["arrow"] == "none" and header["label"] == ""
                            for header in result["dnbHeaders"]),
                    f"D&B collapse arrows or static switch labels remain: {result['dnbHeaders']}")
        self.expect([len(switches) for switches in result["switches"]] == [2, 8],
                    "All Crop and D&B checkboxes must be covered")
        self.expect(all(switch["appearance"] == "none" and switch["width"] == "32px"
                        and switch["height"] == "18px"
                        and switch["background"] == ("rgb(76, 141, 255)" if switch["checked"] else "rgb(58, 63, 75)")
                        and (not switch["disabled"] or (switch["cursor"] == "default" and switch["opacity"] == "0.5"))
                        for switches in result["switches"] for switch in switches),
                    f"Global switch styling or disabled state missing: {result['switches']}")
        self.expect(result["cropInfo"] == "", "Empty Crop instructions remain")
        toggles = result["toggles"]
        self.expect(all(toggle and toggle["label"] and abs(toggle["right"]) < 1 for toggle in toggles)
                    and toggles[0]["bottom"] <= toggles[1]["top"]
                    and toggles[1]["bottom"] <= result["modelTop"],
                    f"Crop toggles must use separate right-aligned rows above models: {toggles}")
        self.expect(result["tabs"][0]["radius"] == "6px"
                    and result["tabs"][0]["background"] != result["tabs"][1]["background"], "Module tab styling missing")
        self.expect(result["tabs"][0]["background"] == result["barBackground"]
                    and result["tabs"][0]["bottom"] == result["barBackground"]
                    and result["tabs"][0]["stripe"] == "none", "Active tab must match bar without top stripe")
        self.expect(all(s and s["scheme"] == "dark" and s["appearance"] == "none"
                        and s["background"] == "rgb(18, 20, 26)" for s in result["selects"]),
                    f"Dropdown dark styling missing: {result['selects']}")
        self.expect(all(len(s["buttonHeights"]) == 3 and all(abs(height-(s["presetHeight"]-1)) < 1
                        for height in s["buttonHeights"]) for s in result["selects"]),
                    f"Preset buttons must be one pixel shorter than dropdown: {result['selects']}")
        self.expect(Gtk.Settings.get_default().get_property("gtk-application-prefer-dark-theme"),
                    "Native WebKit popup theme is not dark")
        self.expect(self.tray_available(), "Actual tray host unavailable")
        for reachable in (True, False):
            endpoint = {"address": "127.0.0.1:7878", "reachable": reachable}
            status = {"modules": self.original_request("/api/status")["modules"],
                      "external": {"photocraft_crop": endpoint, "photocraft_dnb": endpoint,
                                   "comfy": dict(endpoint, address="127.0.0.1:8188")}}
            self.update_status("", status)
            self.expect(len(self.external_items) == 2, "Tray must have two separate external lines")
            self.expect(self.external_items[0].get_label() == f"PhotoCraft: {'Reachable' if reachable else 'Unreachable'} (TCP)",
                        "PhotoCraft consolidated line incorrect")
            self.expect(self.external_items[1].get_label().startswith("ComfyUI: "), "ComfyUI line missing")
        status["external"]["photocraft_dnb"] = dict(endpoint, address="127.0.0.1:9999")
        self.update_status("", status)
        self.expect("mismatch" in self.external_items[0].get_label(), "Distinct endpoints silently conflated")
        self.update_status("Tools connection lost: fixture")
        self.expect("unavailable" in self.external_items[0].get_label(), "Disconnected external status misleading")
        status["external"]["comfy"] = {"phase": "running", "busy": True, "reachable": True}
        self.update_status("", status)
        self.expect(self.external_items[1].get_label() == "ComfyUI (Tools): Busy"
                    and "Tools ComfyUI" in module.busy_modules(status), "Owned ComfyUI busy state not protected")
        self.steps.append("Wayland GTK/WebKit window, iframe load, native lease, crypto/beacon and tray")
        if len(sys.argv) > 4 and sys.argv[4] == "storage-only":
            self.phase = "storage-check"
            self.evaluate("return localStorage.getItem('native-fixture');", self.stored)
            return
        self.phase = "load"
        self.evaluate("""
            window.fixtureCrop=document.querySelector('iframe[title="SHP PhotoCraft Crop"]');
            window.fixtureDnb=document.querySelector('iframe[title="SHP PhotoCraft D&B"]');
            if(document.querySelector('#services,#services-toggle,#confirm,[data-control]'))
              throw Error('Shell service details remain');
            if(fixtureCrop.contentDocument.querySelector('#xComfy,#xModelMsg'))
              throw Error('Crop ComfyUI startup/warning block remains');
            for(const group of fixtureDnb.contentDocument.querySelectorAll('details.grp')){
              const summary=group.querySelector('summary'),open=group.open;
              summary.click();
              if(group.open===open)throw Error(`Header did not toggle ${group.id}`);
              summary.click();
              if(group.open!==open)throw Error(`Header did not restore ${group.id}`);
              const input=summary.querySelector('input[type=checkbox]');
              if(input&&!input.disabled){
                const checked=input.checked;
                input.parentElement.click();
                if(input.checked===checked||group.open!==open)
                  throw Error(`Switch label incorrectly toggled ${group.id}`);
                input.parentElement.click();
                if(input.checked!==checked||group.open!==open)
                  throw Error(`Switch label did not restore ${group.id}`);
              }
            }
            localStorage.setItem('native-fixture','persistent');
            fixtureCrop.contentDocument.getElementById('xGet').click();
            document.getElementById('tab-dnb').click();
            fixtureDnb.contentDocument.getElementById('pcGet').click();
            return true;
            """, lambda _: None)

    def stored(self, result):
        self.expect(result == "persistent", f"WebKit storage did not persist across desktop restart: {result}")
        geometry = json.loads((self.data / "window.json").read_text())
        self.expect(tuple(self.window.get_size()) == (geometry["width"], geometry["height"]),
                    "Saved window size was not restored")
        self.steps.append("dedicated WebKit localStorage persisted across process restart")
        self.finish_quit()

    def recovered(self, result):
        if not result["ready"]:
            return
        self.expect(result["stored"] == "persistent", "Renderer recovery lost persistent storage")
        self.expect(module.work_signature(self.original_request("/api/status")) == self.before_recovery,
                    "Renderer recovery restarted services or changed loaded work")
        self.steps.append("actual renderer termination, guarded Reload cancellation and recovery without service restart")
        for name in ("crop", "dnb"):
            children = self.module_items[name].get_submenu().get_children()
            self.expect([c.get_label() for c in children] == ["Start", "Stop", "Restart"], "Tray submenu structure wrong")
        status = self.original_request("/api/status")
        self.tray_crop = status["modules"]["crop"]["instance"]
        self.tray_dnb = status["modules"]["dnb"]["instance"]
        for phase in ("starting", "stopping", "unavailable", "error", "stopped"):
            altered = dict(status, modules=dict(status["modules"],
                crop=dict(status["modules"]["crop"], phase=phase, busy=phase != "error")))
            self.update_status("Fixture status", altered)
            self.expect(self.module_items["crop"].get_label() == f"Crop - {phase.capitalize()}",
                        "Tray phase label wrong")
            if phase in ("starting", "stopping", "unavailable"):
                self.expect(not any(b.get_sensitive() for b in self.module_actions["crop"].values()),
                            "Unavailable/changing module has enabled actions")
        self.update_status("Fixture status", status)
        control = self.connection.control

        def checked_control(*args):
            self.expect(threading.current_thread() is not threading.main_thread(), "Control request blocked GTK thread")
            return control(*args)

        self.connection.control = checked_control
        self.dialog_action = "cancel"
        self.module_actions["crop"]["stop"].activate()
        self.phase = "tray-cancel-wait"

    @staticmethod
    def unreachable_control(*_):
        raise OSError("Fixture connection refused")

    def loaded(self, result):
        if not result["loaded"]:
            return
        self.phase = "result"
        self.evaluate("""
            document.getElementById('tab-crop').click();
            fixtureCrop.contentDocument.querySelector('#xRatios [data-i="3"]').click();
            fixtureCrop.contentDocument.getElementById('xRun').click();
            return true;
            """, lambda _: None)

    def result(self, result):
        if not result["done"]:
            return
        self.phase = "result-check"
        self.evaluate("""
            const c=fixtureCrop,d=fixtureDnb;
            const canvas=c.contentDocument.getElementById('xCanvas');
            canvas.dispatchEvent(new WheelEvent('wheel',{deltaY:-150,clientX:700,clientY:300,cancelable:true,bubbles:true}));
            const crop=c.contentWindow.eval('JSON.stringify({rv:X.rv,seq:X.seq,view:X.view})');
            document.getElementById('tab-dnb').click();
            d.contentDocument.getElementById('p1').dispatchEvent(new WheelEvent('wheel',
              {deltaY:-180,clientX:900,clientY:400,cancelable:true,bubbles:true}));
            const dnb=d.contentWindow.eval('JSON.stringify({view,version:state.version})');
            document.getElementById('tab-crop').click();
            const cropKept=crop===c.contentWindow.eval('JSON.stringify({rv:X.rv,seq:X.seq,view:X.view})');
            document.getElementById('tab-dnb').click();
            const dnbKept=dnb===d.contentWindow.eval('JSON.stringify({view,version:state.version})');
            document.getElementById('tab-crop').click();
            return {cropKept,dnbKept,canvas:!!canvas.getContext('2d').getImageData(0,0,1,1),
                    zoom:d.contentWindow.eval('view.z'),pointer:typeof canvas.setPointerCapture};
            """, self.result_checked)

    def result_checked(self, result):
        self.expect(result["cropKept"] and result["dnbKept"] and result["canvas"] and result["zoom"] > 1,
                    f"Renderer/switch/wheel failed: {result}")
        self.expect(result["pointer"] == "function", "Pointer capture missing")
        self.steps.append("synthetic Crop generation, D&B CPU previews, canvas draw/readback, wheel zoom, iframe state retention")
        self.phase = "pointer-setup"
        self.evaluate("""
            const canvas=fixtureCrop.contentDocument.getElementById('xCanvas');
            fixtureCrop.contentWindow.pointerEvents=[];fixtureCrop.contentWindow.didCapture=false;
            canvas.addEventListener('pointerdown',e=>{fixtureCrop.contentWindow.pointerEvents.push(e.type);
              fixtureCrop.contentWindow.didCapture=canvas.hasPointerCapture(e.pointerId)});
            canvas.addEventListener('pointermove',e=>fixtureCrop.contentWindow.pointerEvents.push(e.type));
            const a=canvas.getBoundingClientRect(),b=fixtureCrop.getBoundingClientRect();
            return {x:a.x+b.x+a.width/2,y:a.y+b.y+a.height/2};
            """, self.pointer_input)

    def pointer_input(self, point):
        x, y = point["x"], point["y"]
        window = self.view.get_window()
        device = Gdk.Display.get_default().get_default_seat().get_pointer()

        def inject(kind, dx=0, dy=0, state=0):
            event = Gdk.Event.new(kind)
            event.window = window
            event.send_event = True
            event.time = Gdk.CURRENT_TIME
            event.x, event.y = x + dx, y + dy
            event.state = state
            event.set_device(device)
            if kind in (Gdk.EventType.BUTTON_PRESS, Gdk.EventType.BUTTON_RELEASE):
                event.button = 1
            Gtk.main_do_event(event)
            return GLib.SOURCE_REMOVE

        inject(Gdk.EventType.MOTION_NOTIFY)
        GLib.timeout_add(100, inject, Gdk.EventType.BUTTON_PRESS)
        GLib.timeout_add(200, inject, Gdk.EventType.MOTION_NOTIFY, 30, 20, Gdk.ModifierType.BUTTON1_MASK)
        GLib.timeout_add(300, inject, Gdk.EventType.BUTTON_RELEASE, 30, 20)
        GLib.timeout_add(500, lambda: (setattr(self, "phase", "pointer-check"), False)[1])

    def pointer_checked(self, result):
        self.expect("pointerdown" in result["events"] and result["capture"], f"Actual GTK pointer capture failed: {result}")
        self.steps.append("GTK input delivery and active pointer capture in iframe canvas")
        self.phase = "dialog"

    def after_dialog(self, result):
        self.expect(result["value"] == "WebKit fixture preset", f"Native JS prompt failed: {result}")
        self.steps.append("native JavaScript prompt dialog")
        self.phase = "download"

    def hidden(self):
        self.window_close()
        self.expect(not self.window.get_visible(), "X did not hide to tray")
        self.hidden_at = time.monotonic()
        self.phase = "hidden"

    def reopened(self, result):
        self.expect(result["crop"] and result["dnb"] and result["local"] == "persistent",
                    f"Reopen lost iframe/zoom/storage: {result}")
        self.steps.append("single-instance DBus activation retains window, loaded images/zooms and localStorage")
        self.phase = "quit-cancel"


app = Smoke()
code = app.run([sys.argv[0]])
print(json.dumps({"steps": app.steps, "failures": app.failures, "exit": code}), flush=True)
raise SystemExit(1 if app.failures else code)
