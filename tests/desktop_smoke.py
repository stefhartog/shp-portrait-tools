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
                        partial:c.contentWindow.eval('X.hasResult&&X.running'),
                        selected:c.contentWindow.eval('X.sel'),
                        count:c.contentDocument.getElementById('xResTabs').children.length,
                        status:c.contentDocument.getElementById('status').textContent};
                """, self.result)
        elif self.phase == "fill-settings":
            self.evaluate("""
                const crop=fixtureCrop.contentWindow.fillSettingsCheck,skin=fixtureDnb.contentWindow.skinToolsCheck;
                return crop&&skin ? {crop,skin} : null;
                """, self.fill_settings_checked)
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
                    if(!window.recoverySkinRequested&&document.getElementById('tab-frequency')?.onclick){
                      document.getElementById('tab-frequency').click();window.recoverySkinRequested=true;
                    }
                    const frames=[...document.querySelectorAll('iframe')];
                    const ready=frames.length===2&&frames.every(f=>f.contentDocument?.readyState==='complete'
                        &&f.contentDocument.getElementById(f.title.endsWith('Crop')?'xGet':'pcGet'))
                        &&frames.find(f=>f.title.endsWith('D&B')).contentDocument.getElementById('toolModeRow').hidden
                        &&frames.find(f=>f.title.endsWith('D&B')).contentWindow.eval("state.mode==='frequency'");
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
        # WebKit can report no running animation one paint before the final color.
        if not hasattr(self, "switch_color_deadline"):
            self.switch_color_deadline = time.monotonic() + 1
        if time.monotonic() < self.switch_color_deadline and any(
                switch["background"] != ("rgb(76, 141, 255)" if switch["checked"] else "rgb(58, 63, 75)")
                for switches in result["switches"] for switch in switches):
            return
        self.expect(result["native"] and result["crypto"] == "function" and result["beacon"] == "function",
                    f"Missing browser APIs: {result}")
        self.expect(not result["repeatedTitle"], "Repeated shell title remains")
        self.expect(len(result["dnbHeaders"]) == 8
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
            fixtureDnb.contentWindow.eval(`{
              const original=getSettings(),check=(ok,msg)=>{if(!ok)throw Error(msg);};
              check(sliders.tau.num.value==='1','tau default not shown');
              check(sliders.tau.rng.min==='0.01'&&sliders.tau.rng.max==='0.4'
                &&sliders.tau.rng.step==='0.01','tau range incorrect');
              snapNow();
              sliders.tau.rng.value='0.35';sliders.tau.rng.dispatchEvent(new Event('input'));
              check(state.tau===0.35&&params().tau===0.35,'tau not wired to request');
              check($('presetSel').selectedOptions[0].textContent.endsWith(' *'),'tau preset star missing');
              const saved=getSettings();applySettings(original);applySettings(saved);
              check(state.tau===0.35&&sliders.tau.num.value==='0.35','tau preset load missing');
              for(const tau of [3,0.005]){
                applySettings({...original,tau});
                check(state.tau===tau&&getSettings().tau===tau&&params().tau===tau
                  &&+sliders.tau.num.value===tau&&sliders.tau.rng.max==='0.4'
                  &&sliders.tau.rng.min==='0.01','Legacy tau lost or bounds changed');
              }
              sliders.tau.rng.value='0.4';sliders.tau.rng.dispatchEvent(new Event('input'));
              check(state.tau===0.4&&+sliders.tau.num.value===0.4,'Legacy tau slider edit failed');
              applySettings(original);snapNow();
            }`);
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
        self.steps.append("recreated skin frame handshake honors queued Frequency tab")
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
        self.phase = "fill-settings"
        self.evaluate("""
            fixtureDnb.contentWindow.eval(`(async()=>{
              const check=(ok,message)=>{if(!ok)throw Error(message);};
              try{
                const wait=async test=>{
                  for(let i=0;i<200;i++){if(test())return;await new Promise(r=>setTimeout(r,25));}
                  throw Error('Shell mode synchronization timed out');
                };
                const shell=parent.document,frame=parent.fixtureDnb,src=frame.src,identity=state.pcFetched;
                await wait(()=>$('toolModeRow').hidden);
                check(getComputedStyle($('toolModeRow')).display==='none','Embedded selector still visible');
                check([...shell.querySelectorAll('[role="tab"]')].map(t=>t.dataset.view).join(',')===
                  'crop,mask,frequency,dnb','Shell tab order wrong');
                const sharedBefore=JSON.stringify([params().mask,params().tone,params().color,params().structure]);
                const zoomBefore=JSON.stringify(view);
                for(const mode of ['mask','frequency','dnb']){
                  shell.getElementById('tab-'+mode).click();
                  await wait(()=>state.mode===mode&&!state.busy);
                  check(shell.querySelectorAll('iframe').length===2&&frame===parent.fixtureDnb&&frame.src===src,
                    'Shell duplicated/reloaded skin frame');
                  check(state.pcFetched===identity&&JSON.stringify(view)===zoomBefore&&
                    JSON.stringify([params().mask,params().tone,params().color,params().structure])===sharedBefore,
                    'Shell navigation lost loaded identity/mask/view');
                  check(shell.getElementById('panel-dnb').getAttribute('aria-labelledby')==='tab-'+mode&&
                    !shell.getElementById('panel-dnb').inert,'Shared panel ARIA/inert wrong');
                }
                const selected=shell.getElementById('tab-dnb');
                selected.dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowRight',bubbles:true}));
                check(shell.getElementById('tab-crop').getAttribute('aria-selected')==='true','Arrow wrap failed');
                shell.getElementById('tab-crop').dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowLeft',bubbles:true}));
                await wait(()=>shell.getElementById('tab-dnb').getAttribute('aria-selected')==='true');
                setBusy(true);
                shell.getElementById('tab-mask').click();shell.getElementById('tab-frequency').click();
                await new Promise(r=>setTimeout(r,50));
                check(state.mode==='dnb','Shell switched modes while busy');
                setBusy(false);clearTimeout(markDirty.t);
                await wait(()=>state.mode==='frequency'&&shell.getElementById('tab-frequency').getAttribute('aria-selected')==='true');
                shell.getElementById('tab-dnb').click();
                await wait(()=>state.mode==='dnb');clearTimeout(markDirty.t);
                const standalone=shell.createElement('iframe');standalone.hidden=true;standalone.src='dnb/';
                shell.body.append(standalone);
                await wait(()=>standalone.contentDocument?.readyState==='complete'&&
                  standalone.contentDocument?.querySelector('#toolMode'));
                check(!standalone.contentDocument.getElementById('toolModeRow').hidden,
                  'Unconnected standalone selector hidden');
                window.postMessage({type:'shp-tools-mode',mode:'mask',request:999},location.origin);
                standalone.contentWindow.eval("parent.postMessage({type:'shp-skin-mode',mode:'mask',request:null},location.origin)");
                await new Promise(r=>setTimeout(r,50));
                check(state.mode==='dnb'&&shell.getElementById('tab-dnb').getAttribute('aria-selected')==='true',
                  'Untrusted message source changed mode');
                standalone.contentWindow.eval("$('toolMode').value='frequency';$('toolMode').onchange()");
                check(standalone.contentWindow.eval('state.mode')==='frequency','Standalone mode selector broken');
                standalone.contentWindow.eval("applySettings({mode:'skintone',skintone:{sat_min:93,sat_max:2,sat_amount:87}});clearTimeout(markDirty.t)");
                check(standalone.contentWindow.eval("state.mode==='frequency'&&getSettings().skintone.sat_min===93&&getSettings().skintone.sat_max===2"),
                  'Legacy mode/saturation preset not preserved in standalone');
                check(standalone.contentDocument.querySelectorAll('#toolMode option').length===3,
                  'Redundant standalone mode remains');
                const routeLoaded=await api('/api/pc/fetch',{});
                const routeBody={mode:'frequency',S:routeLoaded.S,generation:routeLoaded.generation};
                await api('/api/test/hue-route',{available:false});
                try {
                  const raw=await fetch('api/hue-center',{method:'POST',
                    headers:{'Content-Type':'application/json'},body:JSON.stringify(routeBody)});
                  check(raw.status===404&&raw.headers.get('Content-Type')==='text/plain',
                    'Old-backend route response not reproduced through actual proxy');
                  let parseFailure;
                  try {await raw.json();} catch(e) {parseFailure={name:e.name,message:e.message,stack:e.stack};}
                  check(parseFailure?.name==='SyntaxError'&&parseFailure.message==='The string did not match the expected pattern.',
                    'Reported WebKit JSON error not reproduced: '+JSON.stringify(parseFailure));
                  console.log('Reproduced original Hue Center failure',JSON.stringify(parseFailure));
                  for(const child of [window,standalone.contentWindow]){
                    let error;
                    try {await child.eval("api('/api/hue-center',"+JSON.stringify(routeBody)+")");}
                    catch(e){error=e.message;}
                    check(error?.includes('HTTP 404')&&error.includes('Restart Tools/D&B')&&
                      !error.includes('expected pattern'),'Missing endpoint diagnostic unclear: '+error);
                  }
                  applyLoaded(routeLoaded);state.pcFetched=true;
                  const previous=state.skintone.hue_center;
                  await initializeHueCenter();
                  check(state.skintone.hue_center===previous&&state.hueCenterNote.includes('Restart Tools/D&B'),
                    'Old backend invented a Center or lost restart explanation');
                } finally {await api('/api/test/hue-route',{available:true});}
                const routeEstimate=await api('/api/hue-center',routeBody);
                check(Number.isFinite(routeEstimate.center),'Real current proxy route rejected valid estimate');
                standalone.remove();
                const initialS=$('S').value;
                await $('landmarkOpen').onclick();
                await wait(()=>$('p0').querySelector('img')?.naturalWidth>0);
                check(landmarkEdit&&!$('landmarkEditor').hidden,'Landmark editor unavailable');
                $('landmarkSubject').value='0';$('landmarkSubject').onchange();
                landmarkDraft().points={
                  l_eye:{x:300,y:200,visible:true,source:'manual'},
                  r_eye:{x:480,y:200,visible:true,source:'manual'}
                };
                landmarkChanged();await wait(()=>!$('landmarkApply').disabled);
                check(Math.abs(landmarkEdit.proposal.S-180)<0.001,'Manual eye scale incorrect');
                $('landmarkName').value='r_eye';
                $('p0').dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowRight',shiftKey:true,bubbles:true}));
                await wait(()=>!$('landmarkApply').disabled);
                check(landmarkCurrentPoints().r_eye.x===490,'Landmark nudge failed');
                $('landmarkMissing').click();
                await wait(()=>landmarkEdit.proposal!==null);
                check($('landmarkApply').disabled,'Missing eyes produced scale');
                $('landmarkUndo').click();await wait(()=>!$('landmarkApply').disabled);
                await $('landmarkApply').onclick();clearTimeout(markDirty.t);
                await wait(()=>!state.busy);
                check(!landmarkEdit&&Math.abs(+$('S').value-190)<0.001,'Apply landmark scale failed');
                check(state.frequency.radius===Math.round(state.frequency.radius_at_s181*190/181),
                  'Landmark scale did not update FS radius');
                await $('landmarkOpen').onclick();
                $('landmarkSubject').value='0';$('landmarkSubject').onchange();
                check(landmarkCurrentPoints().r_eye.x===490,'Applied landmarks not retained');
                $('landmarkReset').click();
                $('landmarkCancel').click();
                check(!landmarkEdit&&+$('S').value===190,'Cancel changed applied scale');
                $('S').value=initialS;$('S').onchange();clearTimeout(markDirty.t);
                while(state.busy)await new Promise(r=>setTimeout(r,25));
                const original=getSettings(),S=$('S').value;
                check(sliders.layerBlur.lab.textContent==='Layer blur'&&
                  sliders.layerBlur.rng.min==='0'&&sliders.layerBlur.rng.max==='20'&&
                  sliders.layerBlur.rng.step==='0.1'&&params().layer_blur_at_s181===0,
                  'D&B Layer blur bounds/default incorrect');
                snapNow();
                const fsBeforeBlur=JSON.stringify(state.frequency);
                sliders.layerBlur.rng.value='2.3';
                sliders.layerBlur.rng.dispatchEvent(new Event('input'));
                clearTimeout(markDirty.t);
                check(params().layer_blur_at_s181===2.3&&
                  $('presetSel').selectedOptions[0].textContent.endsWith(' *')&&
                  sliders.layerBlur.lab.title.includes('px at S181')&&
                  JSON.stringify(state.frequency)===fsBeforeBlur,'Layer blur not independently wired/starred');
                snapNow();
                $('S').value=90.5;$('S').onchange();clearTimeout(markDirty.t);
                check(sliders.layerBlur.lab.title.includes('1.15 px')&&
                  !$('presetSel').selectedOptions[0].textContent.endsWith(' *'),
                  'S-derived Layer blur incorrect or dirtied preset');
                await storePreset('p:Layer blur fixture','Layer blur fixture');
                applySettings({});clearTimeout(markDirty.t);
                check(state.layerBlur===0,'Legacy settings did not default Layer blur to zero');
                $('presetSel').value='p:Layer blur fixture';$('presetSel').onchange();clearTimeout(markDirty.t);
                check(state.layerBlur===2.3,'Layer blur preset not restored');
                await api('/api/presets/delete',{name:'Layer blur fixture'});
                applySettings(original);$('S').value=S;$('S').onchange();clearTimeout(markDirty.t);
                const hueFiles=await api('/api/images');
                for(const hue of [18,43]){
                  const file=hueFiles.find(item=>item.name==='hue-'+hue+'.png');
                  check(file,'Hue load fixture missing');
                  $('imgPath').value=file.path;
                  const realShowImages=showImages;
                  let sawDisplayWait=false;
                  try {
                    showImages=async()=>{
                      const shown=await realShowImages();
                      check(!temporaryLog.load.includes('Ready in '),
                        'Ready was reported before display completion');
                      sawDisplayWait=true;
                      await new Promise(r=>setTimeout(r,40));
                      return shown;
                    };
                    await loadImage();
                    while(state.busy)await new Promise(r=>setTimeout(r,25));
                    check(sawDisplayWait,'Automatic preview did not await pane loading');
                  } finally {showImages=realShowImages;}
                  check(/Image loaded in [0-9]+[.][0-9]{2}s/.test(temporaryLog.load)&&
                    temporaryLog.load.includes('Image analyzed in ')&&
                    temporaryLog.load.includes('Mask generated in ')&&
                    temporaryLog.load.includes('Hue Center initialized in ')&&
                    temporaryLog.load.includes('Preview computed in ')&&
                    temporaryLog.load.includes('Preview images loaded in ')&&
                    temporaryLog.load.includes('Ready in '),
                    'Per-stage Load timings missing after automatic preview: '+temporaryLog.load);
                  check(pans.every(p=>p.querySelector('img')?.complete&&
                    p.querySelector('img')?.naturalWidth>0),'Ready before preview images loaded');
                  const retainedTimings=temporaryLog.load;
                  check(Math.abs(state.skintone.hue_center-hue)<.2&&
                    Math.abs(+skintoneSliders.hue_center.num.value-hue)<.2,
                    'New local image did not initialize measured Hue Center');
                  skintoneSliders.hue_center.rng.value='32';
                  skintoneSliders.hue_center.rng.dispatchEvent(new Event('input'));
                  clearTimeout(markDirty.t);await apply();
                  while(state.busy)await new Promise(r=>setTimeout(r,25));
                  check(state.skintone.hue_center===32,'Preview overwrote manual Hue Center');
                  check(temporaryLog.load===retainedTimings,
                    'Automatic/manual Apply replaced persistent load timings');
                  switchMode('mask');clearTimeout(markDirty.t);await apply();
                  while(state.busy)await new Promise(r=>setTimeout(r,25));
                  check(state.skintone.hue_center===32,'Mask/mode recomputation overwrote manual Center');
                }
                const versionBeforeFailedLoad=state.version;
                $('imgPath').value='/missing-stage-fixture.png';
                await loadImage();
                check($('status').classList.contains('err')&&
                  temporaryLog.load.includes('Image load/analysis failed:')&&
                  !temporaryLog.load.includes('Ready in ')&&
                  state.version===versionBeforeFailedLoad,'Failed load claimed success or applied old image');
                const failedTimings=temporaryLog.load;
                await apply();
                check(temporaryLog.load===failedTimings,
                  'Later Apply claimed failed Load was ready');
                await pcGet();while(state.busy)await new Promise(r=>setTimeout(r,25));
                check(state.pcFetched&&state.hueCenterNote.includes('estimated from original Color')&&
                  state.skintone.hue_center!==32,'Get did not re-estimate Center');
                check(temporaryLog.load.includes('Image loaded in ')&&
                  temporaryLog.load.includes('Ready in '),'Get timings missing');
                const realDisplay=showImages;
                try {
                  beginLoadTimings('Testing image display failure');
                  showImages=()=>{
                    const shown=realDisplay();
                    pans[0].querySelector('img').src='img/missing-timing-fixture';
                    return shown;
                  };
                  await apply();
                  check($('status').classList.contains('err')&&
                    temporaryLog.load.includes('Preview display failed')&&
                    !temporaryLog.load.includes('Ready in '),
                    'Image display failure was overwritten with Ready');
                } finally {showImages=realDisplay;}
                beginLoadTimings('Testing absent detection');
                applyLoaded({generation:state.imageGeneration+1,S:null,has_mask:false,
                  name:'no-scale fixture',width:800,height:600,colour:'test',confidence:'none',
                  desc:'no detected subject',timings:{loaded:.1,analyzed:.2,mask:null}});
                await apply();
                check(temporaryLog.load.includes('Mask not generated:')&&
                  temporaryLog.load.includes('Preview not generated:')&&
                  !temporaryLog.load.includes('Ready in '),'Missing detection reported generated mask');
                await pcGet();while(state.busy)await new Promise(r=>setTimeout(r,25));
                const beforeEstimateApi=api;
                let releaseEstimate;
                try {
                  api=async(path,body)=>path==='/api/hue-center'
                    ?new Promise(resolve=>{releaseEstimate=(center=49,note='fixture')=>resolve({generation:body.generation,center,note});})
                    :beforeEstimateApi(path,body);
                  const estimating=initializeHueCenter();
                  skintoneSliders.hue_center.rng.value='28';
                  skintoneSliders.hue_center.rng.dispatchEvent(new Event('input'));
                  clearTimeout(markDirty.t);releaseEstimate();await estimating;
                  check(state.skintone.hue_center===28,'Late estimate overwrote manual edit');
                  const stale=initializeHueCenter();
                  applyLoaded({generation:state.imageGeneration+1,S:180,has_mask:true,name:'new fixture',
                    width:800,height:600,seconds:0,colour:'test',confidence:'high',desc:'test'});
                  releaseEstimate();await stale;
                  check(state.skintone.hue_center===28,'Stale image estimate was applied');
                  const outside=initializeHueCenter();releaseEstimate(355);await outside;
                  check(state.skintone.hue_center===355&&skintoneSliders.hue_center.num.value==='355',
                    'Measured hue outside slider window silently clamped');
                  const empty=initializeHueCenter();
                  releaseEstimate(null,'Hue Center not estimated: no usable masked coloured skin samples; previous Center retained.');
                  await empty;
                  check(state.skintone.hue_center===355&&temporaryLogText().includes('no usable masked'),
                    'Empty estimate silently invented a Center or hid its explanation');
                } finally {api=beforeEstimateApi;}
                await pcGet();while(state.busy)await new Promise(r=>setTimeout(r,25));
                applySettings({...original,skintone:{...original.skintone,hue_center:37}});
                clearTimeout(markDirty.t);await apply();
                while(state.busy)await new Promise(r=>setTimeout(r,25));
                check(state.skintone.hue_center===37,'Preset Center overwritten by image estimate');
                applySettings(original);clearTimeout(markDirty.t);
                check(state.frequency.radius_at_s181===6,'Calibration default missing');
                check(!sliders.frequencyRadius&&!sliders.blurRadius,'Direct px sliders remain');
                check(sliders.blurPercent.rng.min==='100'&&sliders.blurPercent.rng.max==='500'
                  &&state.frequency.blur_percent===200,'Blur ratio bounds/default wrong');
                for(const [scale,radius] of [[181,6],[362,12],[90.5,3],[10000,100],[1,1]]){
                  $('S').value=scale;$('S').onchange();clearTimeout(markDirty.t);
                  check(state.frequency.radius===radius,'S scaling/rounding/bounds wrong');
                  check(state.frequency.blur_radius===2*radius,'Blur not derived from actual radius');
                }
                check(sliders.frequencyCalibration.lab.title.includes('limited'),'Clamp not explained');
                check(!$('frequencyScaleNote')&&!$('g-frequency').querySelector('p'),'Frequency info text remains');
                check(sliders.frequencyCalibration.lab.textContent==='Radius'&&sliders.blurPercent.lab.textContent==='Blur'
                  &&sliders.frequencyCalibration.lab.title.includes('S = 181')&&sliders.blurPercent.lab.title.includes('percentage'),
                  'Frequency slider names or label tooltips wrong');
                check(![...document.querySelectorAll('aside .row, aside .row input, aside .row select')].some(e=>e.title),
                  'Option tooltip outside label column');
                check([...document.querySelectorAll('aside .row')].filter(r=>r.querySelector('input[type=range]'))
                  .every(r=>r.querySelector('label').title),'Slider label tooltip missing');
                $('S').value=181;$('S').onchange();clearTimeout(markDirty.t);
                $('toolMode').value='frequency';$('toolMode').onchange();clearTimeout(markDirty.t);
                check(state.frequency.radius===6,'Mode switch changed radius');
                sliders.frequencyCalibration.rng.value='8';sliders.frequencyCalibration.rng.dispatchEvent(new Event('input'));
                clearTimeout(markDirty.t);
                check(state.frequency.radius===8&&params().frequency.radius_at_s181===8,'Calibration edit not applied');
                applySettings({...original,frequency:{method:'median',radius:17,blur_radius:9}});
                clearTimeout(markDirty.t);
                check(state.frequency.radius_at_s181===6&&state.frequency.radius===6&&state.frequency.blur_percent===100,
                  'Legacy ratio migration or derived radius incorrect');
                applySettings({...original,frequency:{...original.frequency,radius:19,radius_at_s181:8}});
                clearTimeout(markDirty.t);
                check(state.frequency.radius===8&&state.frequency.blur_radius===16,'Stale preset radius overrides calculation');
                const fetched=await api('/api/pc/fetch',{});
                applyLoaded({...fetched,S:362});state.pcFetched=true;
                check(state.frequency.radius===16&&state.frequency.blur_radius===32,
                  'New image did not derive both radii');
                sliders.blurPercent.rng.value='500';sliders.blurPercent.rng.dispatchEvent(new Event('input'));
                $('S').value=10000;$('S').onchange();clearTimeout(markDirty.t);
                check(state.frequency.radius===100&&state.frequency.blur_radius===500,'Blur silently capped');
                applyLoaded({...fetched,S:null});state.pcFetched=true;
                check($('S').value===''&&sliders.frequencyCalibration.lab.title.includes('No valid scale'),
                  'Missing scale guessed silently');
                check(!$('skintoneSummary'),
                  'New image retained old Skintone analysis');
                const rangeError='Skintone needs normalized RGB in 0-1; this source ranges from 0 to 1.5. Use an explicitly tone-mapped SDR copy for Skintone; other skin modes remain available.';
                applyLoaded({...fetched,skintone_error:rangeError});state.pcFetched=true;
                switchMode('skintone');clearTimeout(markDirty.t);
                check(temporaryLogText().includes(rangeError),'Unsupported source explanation missing');
                const rangeApi=api;
                try {
                  api=async(path,body)=>{
                    if(path==='/api/run'&&body.mode==='frequency')throw Error(rangeError);
                    return rangeApi(path,body);
                  };
                  await apply();
                  check($('status').classList.contains('err')&&$('status').textContent===rangeError&&!state.busy,
                    'Unsupported Skintone range was not a handled UI error');
                  switchMode('mask');clearTimeout(markDirty.t);await apply();
                  check(!$('status').classList.contains('err'),'Unsupported Skintone blocked other modes');
                } finally {api=rangeApi;}
                applySettings(original);clearTimeout(markDirty.t);
                applyLoaded(fetched);state.pcFetched=true;
                check(state.frequency.radius===Math.max(1,Math.round(6*fetched.S/181)),
                  'New load used last manual radius');
                const idle=async()=>{while(state.busy)await new Promise(r=>setTimeout(r,25));};
                const choose=async mode=>{
                  $('toolMode').value=mode;$('toolMode').dispatchEvent(new Event('change'));
                  clearTimeout(markDirty.t);await apply();await idle();
                  check(!$('status').classList.contains('err'),$('status').textContent);
                  check(['g-mask','g-structure','g-range','g-color'].every(id=>$(id).hidden===(mode!=='mask')),
                    'Mask control visibility incorrect for '+mode+' (now '+state.mode+')');
                };
                await choose('mask');
                sliders.feather.rng.value='2.3';sliders.feather.rng.dispatchEvent(new Event('input'));
                sliders.toneStrength.rng.value='0.7';sliders.toneStrength.rng.dispatchEvent(new Event('input'));
                $('structureOn').checked=true;$('structureOn').onchange();
                sliders.protect.rng.value='0.4';sliders.protect.rng.dispatchEvent(new Event('input'));
                clearTimeout(markDirty.t);await apply();await idle();
                const shared=()=>JSON.stringify([params().mask,params().tone,params().color,params().structure]);
                const edited=shared();
                const maskPixels=await (await fetch('img/mask')).text();
                for(const mode of ['dnb','frequency']){
                  await choose(mode);
                  check(shared()===edited,'Hidden shared mask state changed in '+mode);
                  check(await (await fetch('img/mask')).text()===maskPixels,'Shared mask processing changed in '+mode);
                }
                check(!$('g-skintone').hidden&&$('g-bands').hidden&&!$('g-frequency').hidden,
                  'Combined controls not visible together');
                for(const kind of ['color','detail']){
                  check(!$('tabsB').querySelector('[data-k="'+kind+'"]').hidden,
                    'Skintone '+kind+' preview unavailable');
                }
                check(!$('satDistribution')&&!$('skintoneSaturation')&&
                  $('g-skintone').textContent.includes('One editable Hue LUT'),
                  'Removed saturation section remains or combined stack is unexplained');
                for(const [key,min,max] of [['hue_center',10,50],['hue_width',1,8]]){
                  check(+skintoneSliders[key].rng.min===min&&+skintoneSliders[key].rng.max===max,
                    'Wrong Skintone slider window: '+key);
                }
                check(state.skintone.hue_center===original.skintone.hue_center,'Saved image Center changed unexpectedly');
                check($('hueDistribution').dataset.axisMin==='10'&&$('hueDistribution').dataset.axisMax==='50',
                  'Chart windows do not match requested units/limits');
                const legacyTone={...state.skintone,hue_center:355,hue_width:90,sat_min:5,sat_max:95};
                const beforeLegacy=getSettings();
                applySettings({...beforeLegacy,skintone:legacyTone});clearTimeout(markDirty.t);
                check(JSON.stringify(state.skintone)===JSON.stringify(legacyTone)&&
                  skintoneSliders.hue_center.num.value==='355'&&skintoneSliders.hue_width.num.value==='90',
                  'Legacy preset silently clamped to slider window');
                applySettings({...beforeLegacy,skintone:{...state.skintone,hue_width:0,sat_min:50,sat_max:50}});
                clearTimeout(markDirty.t);await apply();await idle();
                check(state.skintone.hue_width===0&&skintoneSliders.hue_width.num.value==='0'&&
                  !$('status').classList.contains('err'),'Zero width/legacy saturation failed');
                applySettings(beforeLegacy);clearTimeout(markDirty.t);await apply();await idle();
                check($('hueDistribution').getContext('2d').getImageData(80,80,1,1).data[3]>0,
                  'Skintone distribution not rendered');
                const pixels=async kind=>{
                  const image=new Image();image.src='img/'+kind+'?probe='+state.version;
                  await image.decode();
                  const canvas=document.createElement('canvas');canvas.width=image.naturalWidth;canvas.height=image.naturalHeight;
                  const ctx=canvas.getContext('2d');ctx.drawImage(image,0,0);
                  return ctx.getImageData(0,0,canvas.width,canvas.height).data;
                };
                const beforeEffect=getSettings();
                applySettings({...beforeEffect,skintone:{...state.skintone,hue_center:50,hue_width:3,sat_min:5,sat_max:20}});
                clearTimeout(markDirty.t);await apply();await idle();
                tabSel.A='result';syncTabs();showImages();
                await wait(()=>$('p0').querySelector('img')?.complete&&$('p0').querySelector('img').naturalWidth>0);
                const displayed=$('p0').querySelector('img');
                check(!displayed.hidden&&displayed.getBoundingClientRect().width>0&&!$('p0').querySelector('.empty'),
                  'Result is not a displayed decoded image');
                const response=await fetch('img/result?v='+state.version);
                check(response.headers.get('Content-Type')==='image/png','PNG Result has wrong MIME');
                const originalPixels=await pixels('orig'),correctedPixels=await pixels('result');
                let difference=0,lit=0;
                for(let i=0;i<correctedPixels.length;i+=4){
                  difference=Math.max(difference,...[0,1,2].map(c=>Math.abs(correctedPixels[i+c]-originalPixels[i+c])));
                  if(correctedPixels[i]+correctedPixels[i+1]+correctedPixels[i+2]>20)lit++;
                }
                check(difference>20&&lit>1000,'Nonidentity Skintone result is blank or unchanged');
                check((await pixels('color')).length===correctedPixels.length&&
                  (await pixels('detail')).length===correctedPixels.length&&
                  (await pixels('blur')).length===correctedPixels.length,'Color/Detail/Blur previews not decoded');
                check($('status').textContent.startsWith('Visible correction'),'Correction effectiveness not reported');
                applySettings({...getSettings(),skintone:{...state.skintone,hue_amount:0,sat_amount:0}});
                clearTimeout(markDirty.t);await apply();await idle();
                check($('status').textContent.includes('Hue Amount is zero'),'Hue-off Blur-only behavior is unexplained');
                const neutralPixels=await pixels('result');
                let neutralDifference=0;
                for(let i=0;i<neutralPixels.length;i+=4)
                  for(let c=0;c<3;c++)neutralDifference=Math.max(neutralDifference,Math.abs(neutralPixels[i+c]-originalPixels[i+c]));
                check(neutralDifference<20,'Hue-off result differs excessively from softly blurred fixture');
                displayed.dispatchEvent(new Event('error'));
                check($('status').classList.contains('err')&&$('p0').querySelector('.empty'),
                  'Preview load failure is silent');
                applySettings(beforeEffect);clearTimeout(markDirty.t);await apply();await idle();
                snapNow();
                skintoneSliders.hue_center.rng.value='25';
                skintoneSliders.hue_center.rng.dispatchEvent(new Event('input'));
                clearTimeout(markDirty.t);
                check(params().skintone.hue_center===25&&$('presetSel').selectedOptions[0].textContent.endsWith(' *'),
                  'Skintone edit not wired/starred');
                check(Object.keys(skintoneSliders).every(key=>key.startsWith('hue')),
                  'Saturation correction controls remain');
                await apply();await idle();await pcSend();
                check(!$('status').classList.contains('err'),'Skintone Send failed');
                const originalApi=api,requests=[];
                try {
                  api=async(path,body)=>{
                    if(path==='/api/run')requests.push(body.skintone.hue_center);
                    return originalApi(path,body);
                  };
                  const running=apply();
                  skintoneSliders.hue_center.rng.value='32';
                  skintoneSliders.hue_center.rng.dispatchEvent(new Event('input'));
                  await running;
                  await wait(()=>!state.busy&&!state.dirty);
                  check(requests.length>=2&&requests[requests.length-1]===32,
                    'Slider edit during processing left a stale preview');
                } finally {api=originalApi;}
                await storePreset('p:Tone fixture','Tone fixture');
                const toneSaved=getSettings();
                await choose('dnb');
                check(!$('presetSel').selectedOptions[0].textContent.endsWith(' *'),'Skintone navigation dirtied preset');
                $('presetSel').value='p:Tone fixture';$('presetSel').onchange();
                clearTimeout(markDirty.t);await apply();await idle();
                await wait(()=>shell.getElementById('tab-frequency').getAttribute('aria-selected')==='true');
                check(JSON.stringify(getSettings())===JSON.stringify(toneSaved),'Skintone preset did not restore');
                applySettings({...toneSaved,mode:'skintone',skintone:{...toneSaved.skintone,sat_min:93,sat_max:2,sat_amount:87}});
                clearTimeout(markDirty.t);await apply();await idle();
                await wait(()=>shell.getElementById('tab-frequency').getAttribute('aria-selected')==='true');
                check(state.mode==='frequency'&&getSettings().skintone.sat_amount===87,
                  'Legacy mode did not synchronize combined tab or discarded archived saturation');
                await storePreset('p:Tone fixture','Tone fixture');
                const legacyStored=await api('/api/presets');
                check(legacyStored.presets['Tone fixture'].skintone.sat_min===93&&
                  legacyStored.presets['Tone fixture'].skintone.sat_max===2,
                  'Saving new hue settings destructively dropped legacy saturation values');
                await api('/api/presets/delete',{name:'Tone fixture'});
                applySettings({...getSettings(),skintone:original.skintone});
                clearTimeout(markDirty.t);
                await choose('frequency');
                check($('g-bands').hidden&&!$('g-frequency').hidden,'Frequency controls not isolated');
                check($('pcSend').textContent==='Send Result'&&state.pcFetched,'Frequency send unavailable');
                check([...$('tabsB').querySelectorAll('[data-k="color"],[data-k="detail"]')].every(t=>!t.hidden),
                  'Frequency layer previews hidden');
                snapNow();
                sliders.frequencyCalibration.rng.value='5';sliders.frequencyCalibration.rng.dispatchEvent(new Event('input'));
                sliders.blurPercent.rng.value='300';sliders.blurPercent.rng.dispatchEvent(new Event('input'));
                $('frequencyMethod').value='median';$('frequencyMethod').dispatchEvent(new Event('change'));
                check(params().frequency.radius===5&&params().frequency.blur_radius===15&&params().frequency.method==='median',
                  'Frequency controls not wired');
                check($('presetSel').selectedOptions[0].textContent.endsWith(' *'),'Frequency preset star missing');
                const saved=getSettings();
                clearTimeout(markDirty.t);await apply();await idle();
                await pcSend();check(!$('status').classList.contains('err'),'Frequency Send failed');
                await storePreset('p:Skin fixture','Skin fixture');
                const starred=()=>$('presetSel').selectedOptions[0].textContent.endsWith(' *');
                const border=id=>getComputedStyle($(id)).borderTopWidth;
                check(!starred(),'Saved preset still starred');
                const loadedS=$('S').value;
                await choose('dnb');
                check(!starred(),'Mode navigation marked saved preset unsaved');
                check(border('g-bands')==='0px'&&border('g-file')!=='0px','Detail level divider wrong');
                $('S').value=+loadedS+20;$('S').onchange();clearTimeout(markDirty.t);
                check(!starred(),'Derived S radius marked preset unsaved');
                sliders.feather.rng.value='3.1';sliders.feather.rng.dispatchEvent(new Event('input'));clearTimeout(markDirty.t);
                for(const mode of ['dnb','frequency','mask']){await choose(mode);check(starred(),'Hidden shared edit not starred in '+mode);}
                check(border('g-mask')==='0px','Skin mask divider remains');
                await storePreset('p:Skin fixture','Skin fixture');
                for(const mode of ['frequency','dnb','mask']){await choose(mode);check(!starred(),'Save did not clear star in '+mode);}
                sliders.feather.rng.value='2.3';sliders.feather.rng.dispatchEvent(new Event('input'));
                $('S').value=loadedS;$('S').onchange();clearTimeout(markDirty.t);
                await choose('frequency');
                check(JSON.stringify(getSettings())===JSON.stringify(saved),'Fixture state not restored before final save');
                await storePreset('p:Skin fixture','Skin fixture');
                await choose('mask');
                check($('g-bands').hidden&&$('g-frequency').hidden&&$('pcSend').textContent==='Send Result',
                  'Mask mode has correction controls');
                check(!paneKinds().some(k=>['layer','layerm','detail'].includes(k)),'Stale mode preview');
                await pcSend();check(!$('status').classList.contains('err'),'Mask Send failed');
                $('presetSel').value='p:Skin fixture';$('presetSel').onchange();
                clearTimeout(markDirty.t);await apply();await idle();
                await wait(()=>shell.getElementById('tab-frequency').getAttribute('aria-selected')==='true');
                check(state.mode==='frequency'&&JSON.stringify(getSettings())===JSON.stringify(saved),
                  'Skin tool preset did not restore');
                await api('/api/presets/delete',{name:'Skin fixture'});
                applySettings(original);clearTimeout(markDirty.t);await apply();await idle();snapNow();
                check(state.mode==='dnb'&&!$('g-bands').hidden&&$('g-frequency').hidden&&$('S').value===S,
                  'D&B or loaded scale lost');
                window.skinToolsCheck={ok:true};
              }catch(e){window.skinToolsCheck={error:String(e)+' '+(e.stack||'')};}
            })()`);
            fixtureCrop.contentWindow.eval(`(async()=>{
              const check=(ok,message)=>{if(!ok)throw Error(message);};
              try{
                const original=(await api('/api/status',{})).settings,seq=X.seq;
                check(![...document.querySelectorAll('aside .row, aside .row input, aside .row select')].some(e=>e.title)
                  &&[...document.querySelectorAll('aside .row')].filter(r=>r.querySelector('input[type=range]'))
                  .every(r=>r.querySelector('label').title),'Crop option tooltip outside label column');
                const select=$('smethod');
                select.value='flux1_fill';select.dispatchEvent(new Event('change'));
                while(X.settingsSaving)await new Promise(r=>setTimeout(r,25));
                check(isFill()&&X.ready,'Fill method did not become ready');
                check(+$('sguidance').value===30&&+$('ssteps').value===28
                  &&+$('sfeathering').value===64,'Fill defaults were not shown');
                check($('sunet').value==='flux1-fill-dev.safetensors','Wrong Fill model');
                check(['lora','seam_correction'].every(k=>
                  getComputedStyle($('s'+k).closest('.row')).display==='none'),
                  'FLUX.2-only controls still visible');
                check($('sguidance').max==='100','Fill guidance range incorrect');
                const upscale=$('supscale');
                check(upscale.value==='lanczos'&&!upscale.closest('.row').hidden,
                  'Fill Lanczos default missing');
                upscale.value=original.upscale;upscale.dispatchEvent(new Event('change'));
                while(X.settingsSaving)await new Promise(r=>setTimeout(r,25));
                check((await api('/api/status',{})).settings.fill_upscale===original.upscale,
                  'Fill upscale option not saved');
                const device=$('sfill_clip_device');
                check(device.value==='cpu'&&!$('fillDeviceRow').hidden,'Fill device default missing');
                device.value='default';device.dispatchEvent(new Event('change'));
                while(X.settingsSaving)await new Promise(r=>setTimeout(r,25));
                check((await api('/api/status',{})).settings.fill_clip_device==='default',
                  'Fill encoding device not saved');
                $('ssteps').value=31;$('ssteps').dispatchEvent(new Event('change'));
                while(X.settingsSaving)await new Promise(r=>setTimeout(r,25));
                const saved=(await api('/api/status',{})).settings;
                check(saved.fill_steps===31&&saved.steps===original.steps,
                  'Method settings overwritten');
                await presetCall('/api/presets/save',{name:'Fill fixture'});
                await saveSettings({method:'flux2'});
                check($('sguidance').max==='10'&&+$('sfeathering').value===original.feathering,
                  'FLUX.2 settings not restored');
                check(getComputedStyle($('supscale').closest('.row')).display!=='none',
                  'FLUX.2 upscaler did not reappear');
                upscale.value='lanczos';upscale.dispatchEvent(new Event('change'));
                while(X.settingsSaving)await new Promise(r=>setTimeout(r,25));
                const flux2Saved=(await api('/api/status',{})).settings;
                check(X.ready&&flux2Saved.upscale==='lanczos'
                  &&flux2Saved.fill_upscale===original.upscale,'FLUX.2 Lanczos not independent');
                await presetCall('/api/presets/load',{name:'Fill fixture'});
                check(isFill()&&+$('ssteps').value===31,'Fill preset not restored');
                check($('sfill_clip_device').value==='default','Fill preset lost encoding device');
                check($('supscale').value===original.upscale,'Fill preset lost upscaler');
                $('ssteps').value=32;$('ssteps').dispatchEvent(new Event('input'));
                check($('presetSel').selectedOptions[0].textContent.endsWith(' *'),'Preset star missing');
                await saveSettings(original);
                await presetCall('/api/presets/delete',{name:'Fill fixture'});
                check(X.seq===seq&&!!X.img,'Method selection lost loaded image');
                window.fillSettingsCheck={ok:true};
              }catch(e){window.fillSettingsCheck={error:String(e)};}
            })()`);
            return true;
            """, lambda _: None)

    def fill_settings_checked(self, result):
        if result is None:
            return
        self.expect(result["crop"].get("ok"), f"Fill method UI: {result['crop']}")
        self.expect(result["skin"].get("ok"), f"Skin tools UI: {result['skin']}")
        self.steps.append("Fill method controls, separate settings, preset save/load/star and retained image")
        self.steps.append("D&B / Skin Mask / Frequency modes, independent pixel radii, median preview, full-resolution Send and preset roundtrip")
        self.original_request("/crop/api/test/variation-delay", {"seconds": 1})
        self.saw_partial = False
        self.phase = "result"
        self.evaluate("""
            document.getElementById('tab-crop').click();
            fixtureCrop.contentDocument.querySelector('#xRatios [data-i="3"]').click();
            fixtureCrop.contentWindow.eval('setVariations(4)');
            fixtureCrop.contentDocument.getElementById('xRun').click();
            return true;
            """, lambda _: None)

    def result(self, result):
        if result.get("partial") and not self.saw_partial:
            self.saw_partial = True
            self.expect(result["count"] < 4, "Partial results waited for whole batch")
            self.steps.append("completed variation visible while remaining variations generate")
        if result.get("partial") and result["count"] >= 2 and not getattr(self, "picked_partial", False):
            self.picked_partial = True
            self.evaluate("""
                fixtureCrop.contentDocument.getElementById('xResTabs').children[1].click();
                return true;
                """, lambda _: None)
            return
        if not result["done"]:
            return
        self.expect(self.saw_partial and result["count"] == 4, "Incremental four-result preview failed")
        self.expect(getattr(self, "picked_partial", False) and result["selected"] == 1,
                    "Later results stole the selected partial preview")
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
