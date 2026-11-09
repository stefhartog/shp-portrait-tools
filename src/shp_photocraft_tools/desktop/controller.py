"""Run with system Python: GTK3/WebKitGTK/AppIndicator, not the project's ML Python."""

import argparse
import hashlib
import json
import os
import signal
import sys
import threading
import uuid
import subprocess
import select
import time
from pathlib import Path
from urllib.parse import urlsplit
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

try:
    import gi

    gi.require_version("Gtk", "3.0")
    gi.require_version("WebKit2", "4.1")
    gi.require_version("AppIndicator3", "0.1")
    from gi.repository import AppIndicator3, Gdk, Gio, GLib, Gtk, WebKit2
except (ImportError, ValueError) as error:
    raise SystemExit(f"Tools desktop needs system Python with GTK3, WebKit2 4.1 and AppIndicator3 GI bindings: {error}")


def validate_url(url):
    parts = urlsplit(url)
    if (parts.scheme != "http" or parts.hostname != "127.0.0.1" or not parts.port
            or parts.username or parts.password or parts.path != "/" or parts.query or parts.fragment):
        raise ValueError("Desktop URL must be http://127.0.0.1:<port>/")
    return url.rstrip("/")


class Connection:
    def __init__(self, base):
        self.base = base
        self.client = "desktop-" + uuid.uuid4().hex
        self.opener = build_opener(ProxyHandler({}))
        self.stopped = threading.Event()
        self.lease_lock = threading.Lock()
        self.error = None

    def request(self, path, body=None, timeout=5):
        headers = {"Origin": self.base, "Content-Type": "application/json"}
        if path == "/api/ui/native-quit":
            headers["X-SHP-Control"] = "1"
        request = Request(self.base + path, json.dumps(body).encode() if body is not None else None, headers)
        try:
            with self.opener.open(request, timeout=timeout) as response:
                return json.load(response)
        except HTTPError as error:
            if path != "/api/ui/native-quit":
                raise
            with error:
                raise ValueError(json.load(error).get("error", str(error))) from error

    def heartbeat(self):
        while not self.stopped.is_set():
            try:
                with self.lease_lock:
                    if self.stopped.is_set():
                        return
                    result = self.request("/api/ui/heartbeat", {"client": self.client}, timeout=25)
                if not result.get("accepted") or not result.get("managed"):
                    raise ValueError("Supervisor refused the desktop lifetime lease")
                self.error = None
                self.stopped.wait(20)
            except (OSError, ValueError) as error:
                self.error = str(error)
                self.stopped.wait(3)

    def control(self, name, action, body):
        request = Request(self.base + f"/api/modules/{name}/{action}",
                          json.dumps(body).encode(),
                          {"Origin": self.base, "Content-Type": "application/json", "X-SHP-Control": "1"})
        try:
            with self.opener.open(request, timeout=60) as response:
                return True, json.load(response)
        except HTTPError as error:
            with error:
                return False, json.load(error)

    def close(self):
        self.stopped.set()
        try:
            with self.lease_lock:
                self.request("/api/ui/close", {"client": self.client}, timeout=3)
        except (OSError, ValueError) as error:
            print(f"Desktop lease release failed; server will use expiry: {error}", file=sys.stderr, flush=True)


def work_signature(status):
    return {name: (state.get("instance"), state.get("revision"), state.get("loaded"))
            for name, state in status["modules"].items()}


def busy_modules(status):
    busy = [name for name, state in status["modules"].items()
            if state.get("busy") or state.get("phase") in ("starting", "stopping", "unavailable")]
    comfy = status.get("external", {}).get("comfy", {})
    if comfy.get("phase") in ("running", "unavailable") and comfy.get("busy"):
        busy.append("Tools ComfyUI")
    return busy

def external_lines(external):
    crop, dnb = (external.get(key) for key in ("photocraft_crop", "photocraft_dnb"))
    if not crop or not dnb:
        photocraft = "PhotoCraft: Checking"
    elif crop["address"] != dnb["address"]:
        photocraft = "PhotoCraft: configuration mismatch (align SHPC_PC_PORT / DNB_PC_PORT with SHP_PC_PORT)"
    else:
        photocraft = "PhotoCraft: " + ("Reachable" if crop["reachable"] and dnb["reachable"] else "Unreachable") + " (TCP)"
        errors = list(dict.fromkeys(state["error"] for state in (crop, dnb) if state.get("error")))
        if errors:
            photocraft += " - " + "; ".join(errors)
    comfy = external.get("comfy")
    comfy_line = "ComfyUI: Checking" if not comfy else (
        "ComfyUI: " + ("Reachable" if comfy["reachable"] else "Unreachable") + " (TCP)"
        + (" - " + comfy["error"] if comfy.get("error") else ""))
    if comfy and "phase" in comfy:
        phase = "busy" if comfy.get("busy") and comfy["phase"] == "running" else comfy["phase"]
        comfy_line = f"ComfyUI (Tools): {phase.capitalize()}"
        if comfy.get("error"):
            comfy_line += " - " + comfy["error"]
    return photocraft, comfy_line


class Desktop(Gtk.Application):
    def __init__(self, url, data, icon=None, tray_icon=None, launcher_python=None, launcher_config=None):
        self.base = validate_url(url)
        # GTK3's Wayland app_id follows the program class, not the D-Bus application ID.
        GLib.set_prgname("shp-photocraft-tools")
        GLib.set_application_name("SHP PhotoCraft Tools")
        Gdk.set_program_class("shp-photocraft-tools")
        identifier = hashlib.sha256(self.base.encode()).hexdigest()[:16]
        super().__init__(application_id=f"org.shp.PhotoCraftTools.p{identifier}",
                         flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.data, self.icon = data, icon
        self.tray_icon = tray_icon
        self.connection = Connection(self.base)
        self.restart_command = ([launcher_python, "-m", "shp_photocraft_tools.launcher",
                                 "--config", str(launcher_config)] if launcher_python and launcher_config else None)
        self.window = self.view = self.indicator = None
        self.quitting = self.dialog_open = False
        self.pending_action = "quit"
        self.reload_available = False
        self.status_text = "Connecting to Tools..."
        self.message_bar = None
        self.notice_timer = None
        self.notice_generation = 0
        self.notice_key = None
        self.external_items = []
        self.module_items = {}
        self.module_actions = {}
        self.syncing_startup = False
        self.modules = {}
        self.control_pending = False
        self.pending_module = None
        self.status_epoch = 0
        self.geometry_timer = None
        self.geometry = {}
        self.watch_wake_tokens = {}

    def do_startup(self):
        Gtk.Application.do_startup(self)
        Gtk.Settings.get_default().set_property("gtk-application-prefer-dark-theme", True)
        self.hold()
        threading.Thread(target=self.connection.heartbeat, daemon=True).start()
        threading.Thread(target=self.monitor, daemon=True).start()

    def do_activate(self):
        if self.window is None:
            self.create_window()
            self.create_tray()
            GLib.timeout_add_seconds(5, self.check_tray)
        self.show()

    def create_window(self):
        self.data.mkdir(parents=True, exist_ok=True)
        manager = WebKit2.WebsiteDataManager(base_data_directory=str(self.data / "data"),
                                            base_cache_directory=str(self.data / "cache"))
        context = WebKit2.WebContext.new_with_website_data_manager(manager)
        context.connect("download-started", self.download)
        self.view = WebKit2.WebView.new_with_context(context)
        self.view.set_size_request(363, 626)
        self.view.get_user_content_manager().add_script(WebKit2.UserScript.new(
            "window.__shpOwnedDesktop = true;", WebKit2.UserContentInjectedFrames.TOP_FRAME,
            WebKit2.UserScriptInjectionTime.START, None, None))
        self.view.get_user_content_manager().add_script(WebKit2.UserScript.new(
            f"if (location.origin === {json.dumps(self.base)}) window.__shpNativeFileDialog = true;",
            WebKit2.UserContentInjectedFrames.ALL_FRAMES,
            WebKit2.UserScriptInjectionTime.START, None, None))
        self.view.get_settings().set_enable_html5_local_storage(True)
        self.view.connect("decide-policy", self.policy)
        self.view.connect("create", self.popup)
        self.view.connect("permission-request", self.permission)
        self.view.connect("script-dialog", self.script_dialog)
        self.view.connect("load-failed", self.load_failed)
        self.view.connect("web-process-terminated", self.web_failed)
        self.window = Gtk.ApplicationWindow(application=self, title="SHP PhotoCraft Tools")
        geometry_path = self.data / "window.json"
        try:
            geometry = json.loads(geometry_path.read_text())
            if (not isinstance(geometry, dict)
                    or any(type(geometry.get(key)) is not int or geometry[key] <= 0 for key in ("width", "height"))
                    or any(key in geometry and type(geometry[key]) is not int for key in ("x", "y"))
                    or (("x" in geometry) != ("y" in geometry))):
                raise ValueError("Invalid saved window geometry")
            self.geometry = geometry
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as error:
            print(f"Cannot restore window size from {geometry_path}: {error}", file=sys.stderr, flush=True)
        self.window.set_default_size(self.geometry.get("width", 1399), self.geometry.get("height", 949))
        if self.position_supported() and "x" in self.geometry and "y" in self.geometry:
            self.window.move(self.geometry["x"], self.geometry["y"])
        self.window.connect("configure-event", self.window_configured)
        self.window.set_icon_name("shp-photocraft-tools")
        if self.icon and self.icon.is_file():
            self.window.set_icon_from_file(str(self.icon))
        self.window.connect("delete-event", self.window_close)
        layout = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.message_bar = Gtk.Label(xalign=0)
        self.message_bar.set_line_wrap(True)
        self.message_bar.set_margin_start(10)
        self.message_bar.set_margin_end(10)
        notices = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        notices.pack_start(self.message_bar, True, True, 0)
        self.reload_button = Gtk.Button(label="Reload UI...")
        self.reload_button.connect("clicked", lambda *_: self.request_quit("reload"))
        notices.pack_start(self.reload_button, False, False, 4)
        self.notice_frame = Gtk.Frame()
        self.notice_frame.get_style_context().add_class("background")
        self.notice_frame.set_no_show_all(True)
        self.notice_frame.add(notices)
        notices.show_all()
        layout.pack_start(self.notice_frame, False, False, 0)
        layout.pack_start(self.view, True, True, 0)
        self.window.add(layout)
        self.view.load_uri(self.base + "/")

    def position_supported(self):
        return Gdk.Display.get_default().__class__.__name__ == "X11Display"

    def window_configured(self, window, event):
        if window.is_maximized() or window.get_window().get_state() & Gdk.WindowState.FULLSCREEN:
            return False
        width, height = window.get_size()
        self.geometry = {"width": width, "height": height}
        if self.position_supported():
            self.geometry.update(zip(("x", "y"), window.get_position()))
        if self.geometry_timer:
            GLib.source_remove(self.geometry_timer)
        self.geometry_timer = GLib.timeout_add(500, self.save_geometry)
        return False

    def save_geometry(self):
        self.geometry_timer = None
        if not self.geometry:
            return GLib.SOURCE_REMOVE
        path = self.data / "window.json"
        temporary = path.with_suffix(".tmp")
        try:
            temporary.write_text(json.dumps(self.geometry) + "\n")
            temporary.replace(path)
        except OSError as error:
            self.message(f"Could not save window size: {error}")
        return GLib.SOURCE_REMOVE

    def tray_available(self):
        try:
            bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            reply = bus.call_sync("org.kde.StatusNotifierWatcher", "/StatusNotifierWatcher",
                                  "org.freedesktop.DBus.Properties", "Get",
                                  GLib.Variant("(ss)", ("org.kde.StatusNotifierWatcher",
                                                       "IsStatusNotifierHostRegistered")),
                                  None, Gio.DBusCallFlags.NONE, 1000, None)
            return bool(reply.unpack()[0])
        except GLib.Error:
            return False

    def create_tray(self):
        self.indicator = AppIndicator3.Indicator.new(
            self.get_application_id(), str(self.tray_icon) if self.tray_icon and self.tray_icon.is_file() else "shp-photocraft-tools-tray",
            AppIndicator3.IndicatorCategory.APPLICATION_STATUS)
        self.indicator.set_title("SHP PhotoCraft Tools")
        menu = Gtk.Menu()
        for label in external_lines({}):
            item = Gtk.MenuItem(label=label)
            item.set_sensitive(False)
            menu.append(item)
            self.external_items.append(item)
        for name, label in (("crop", "Crop"), ("dnb", "D&B")):
            item = Gtk.MenuItem(label=f"{label} - Connecting")
            submenu = Gtk.Menu()
            self.module_items[name] = item
            self.module_actions[name] = {}
            startup = Gtk.CheckMenuItem(label="Load on startup")
            startup.set_tooltip_text("Applies next launch; does not stop current work." +
                                    (" Skin Mask, FS + Skintone and D&B share this service." if name == "dnb" else ""))
            startup.connect("toggled", lambda button, n=name:
                            self.request_module(n, "startup") if not self.syncing_startup else None)
            submenu.append(startup)
            self.module_actions[name]["startup"] = startup
            for action in ("start", "stop", "restart"):
                button = Gtk.MenuItem(label=action.capitalize())
                button.set_sensitive(False)
                button.connect("activate", lambda _, n=name, a=action: self.request_module(n, a))
                submenu.append(button)
                self.module_actions[name][action] = button
            item.set_submenu(submenu)
            menu.append(item)
        for label, callback in (("Show Tools", lambda *_: self.show()),
                                ("Hide Tools", lambda *_: self.hide()),
                                ("Restart Tools...", lambda *_: self.request_quit("restart")),
                                ("Force Restart Tools...", lambda *_: self.request_quit("force-restart")),
                                ("Quit Tools...", lambda *_: self.request_quit())):
            item = Gtk.MenuItem(label=label)
            item.connect("activate", callback)
            menu.append(item)
        menu.show_all()
        self.indicator.set_menu(menu)
        self.indicator.set_status(AppIndicator3.IndicatorStatus.ACTIVE)

    def show(self):
        self.window.show_all()
        self.message_bar.set_visible(bool(self.message_bar.get_text()))
        self.reload_button.set_visible(self.reload_available)
        self.notice_frame.set_visible(bool(self.message_bar.get_text()) or self.reload_available)
        self.window.present_with_time(Gtk.get_current_event_time())

    def hide(self):
        if self.tray_available():
            self.window.hide()
        else:
            self.show()
            self.message("No tray host is available. The window stays accessible; use Close to quit safely.")

    def check_tray(self):
        if self.quitting:
            return GLib.SOURCE_REMOVE
        if not self.tray_available():
            if not self.window.get_visible():
                self.show()
            self.message("Tray unavailable. Enable GNOME AppIndicator support to hide Tools; Close will offer Quit.")
        return GLib.SOURCE_CONTINUE

    def window_close(self, *_):
        if self.tray_available():
            self.hide()
        else:
            self.request_quit()
        return True

    def cancel_notice_timer(self):
        self.notice_generation += 1
        if self.notice_timer is not None:
            GLib.source_remove(self.notice_timer)
            self.notice_timer = None

    def expire_notice(self, generation):
        if self.quitting or generation != self.notice_generation:
            return GLib.SOURCE_REMOVE
        self.notice_timer = None
        self.message_bar.set_text("")
        self.message_bar.hide()
        self.notice_frame.set_visible(self.reload_available)
        return GLib.SOURCE_REMOVE

    def message(self, text, persistent=False):
        key = (text, persistent or self.reload_available)
        if self.quitting or self.notice_key == key:
            return GLib.SOURCE_REMOVE
        self.cancel_notice_timer()
        self.notice_key = key
        self.message_bar.set_text(text)
        self.message_bar.set_visible(bool(text))
        self.notice_frame.set_visible(bool(text) or self.reload_available)
        if text:
            print(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {text}", file=sys.stderr, flush=True)
            if not key[1]:
                self.notice_timer = GLib.timeout_add(1000, self.expire_notice, self.notice_generation)
        return GLib.SOURCE_REMOVE

    def monitor(self):
        while not self.connection.stopped.is_set():
            epoch = self.status_epoch
            try:
                status = self.connection.request("/api/status")
                summary = ""
                if self.connection.error:
                    summary += " | Desktop connection: " + self.connection.error
                GLib.idle_add(self.update_status, summary, status, epoch)
                states = status.get("modules", {})
                if not any(state.get("busy") for state in states.values()):
                    for name in ("dnb", "crop"):
                        if states.get(name, {}).get("phase") == "running":
                            inbox = self.connection.request(f"/{name}/api/watch-folder")
                            inbox["service"] = name
                            GLib.idle_add(self.wake_watch_folder, inbox, epoch)
            except (OSError, ValueError, KeyError) as error:
                GLib.idle_add(self.update_status, "Tools connection lost: " + str(error), None, epoch)
            self.connection.stopped.wait(3)

    def wake_watch_folder(self, inbox, epoch):
        if self.quitting or self.control_pending or epoch != self.status_epoch or self.window is None:
            return GLib.SOURCE_REMOVE
        service = inbox.get("service", "dnb")
        token = inbox.get("token") if inbox.get("ready") else None
        if token is None:
            self.watch_wake_tokens.pop(service, None)
        elif token != self.watch_wake_tokens.get(service):
            self.watch_wake_tokens[service] = token
            self.show()
        return GLib.SOURCE_REMOVE

    def update_status(self, summary, status=None, epoch=None):
        if self.quitting or self.control_pending or (epoch is not None and epoch != self.status_epoch):
            return GLib.SOURCE_REMOVE
        self.status_text = summary
        self.modules = status["modules"] if status else {}
        lines = external_lines(status.get("external", {})) if status else (
            "PhotoCraft: Status unavailable", "ComfyUI: Status unavailable")
        for item, text in zip(self.external_items, lines):
            item.set_label(text)
        self.render_modules()
        if "connection" in summary.lower():
            self.message(summary, persistent=True)
        elif self.message_bar and self.message_bar.get_text().startswith("Tools connection lost:"):
            self.message("")
        return GLib.SOURCE_REMOVE

    def render_modules(self):
        for name, item in self.module_items.items():
            state = self.modules.get(name, {})
            phase = state.get("phase", "disconnected")
            label = "Crop" if name == "crop" else "D&B"
            status = "busy" if phase == "running" and state.get("busy") else phase
            if (self.control_pending and self.pending_module and self.pending_module[0] == name
                    and self.pending_module[1] != "startup"):
                status = {"start": "starting", "stop": "stopping", "restart": "restarting"}[self.pending_module[1]]
            item.set_label(f"{label} - {status.capitalize()}")
            item.set_tooltip_text(state.get("error") or ("Image loaded / previews in memory" if state.get("loaded") else ""))
            for action, button in self.module_actions[name].items():
                if action == "startup":
                    self.syncing_startup = True
                    try:
                        button.set_active(state.get("startup_enabled", True))
                    finally:
                        self.syncing_startup = False
                    button.set_sensitive(bool(state) and not (self.control_pending or self.dialog_open))
                    continue
                enabled = bool(state) and not (self.control_pending or self.dialog_open
                                               or phase in ("starting", "stopping", "disconnected"))
                if action == "start":
                    enabled = enabled and phase not in ("running", "unavailable")
                else:
                    enabled = enabled and phase != "stopped" and not state.get("busy")
                button.set_sensitive(enabled)

    def request_module(self, name, action):
        if self.quitting or self.dialog_open or self.control_pending:
            return
        if name not in self.module_actions or action not in self.module_actions[name]:
            raise ValueError("Unknown tray module action")
        enabled = not self.modules.get(name, {}).get("startup_enabled", True)
        self.control_pending = True
        self.pending_module = (name, action)
        self.status_epoch += 1
        self.render_modules()
        body = {"instance": self.modules.get(name, {}).get("instance"), "confirm": False, "revision": None}
        if action == "startup":
            body["enabled"] = enabled
        threading.Thread(target=self.module_worker, args=(name, action, body), daemon=True).start()

    def module_worker(self, name, action, body):
        try:
            accepted, result = self.connection.control(name, action, body)
            GLib.idle_add(self.module_result, name, action, body, accepted, result)
        except (OSError, ValueError, KeyError) as error:
            GLib.idle_add(self.module_result, name, action, body, False,
                          {"error": f"Supervisor request failed: {error}. Check shell.log and reopen the launcher if needed.",
                           "code": "connection"})

    def module_result(self, name, action, body, accepted, result):
        if self.quitting:
            return GLib.SOURCE_REMOVE
        label = "Crop" if name == "crop" else "D&B"
        if not accepted and result.get("code") == "confirmation" and not body["confirm"]:
            self.dialog_open = True
            self.show()
            dialog = Gtk.MessageDialog(transient_for=self.window, modal=True,
                                       message_type=Gtk.MessageType.WARNING,
                                       text=f"{action.capitalize()} {label}?")
            dialog.format_secondary_text(result["error"] + "\nThis affects every window using this module.")
            dialog.add_button("Keep working", Gtk.ResponseType.CANCEL)
            dialog.add_button(action.capitalize(), Gtk.ResponseType.ACCEPT)
            dialog.set_default_response(Gtk.ResponseType.CANCEL)
            response = dialog.run()
            dialog.destroy()
            self.dialog_open = False
            if response == Gtk.ResponseType.ACCEPT:
                confirmed = dict(body, confirm=True, revision=result["revision"])
                threading.Thread(target=self.module_worker, args=(name, action, confirmed), daemon=True).start()
                return GLib.SOURCE_REMOVE
        elif not accepted:
            if result.get("code") == "connection":
                self.modules = {}
            self.dialog_open = True
            self.show()
            error = result.get("error", "Invalid supervisor response")
            self.message(f"{label} {action} failed: {error}")
            dialog = Gtk.MessageDialog(transient_for=self.window, modal=True,
                                       message_type=Gtk.MessageType.ERROR,
                                       buttons=Gtk.ButtonsType.CLOSE, text=f"Cannot {action} {label}")
            dialog.format_secondary_text(error)
            dialog.run()
            dialog.destroy()
            self.dialog_open = False
        else:
            self.modules[name] = result
            self.message(f"{label} load on startup {'enabled' if result['startup_enabled'] else 'disabled'}; current work unchanged."
                         if action == "startup" else f"{label} {'stopped' if action == 'stop' else 'ready'}.")
        self.control_pending = False
        self.pending_module = None
        self.status_epoch += 1
        self.render_modules()
        return GLib.SOURCE_REMOVE

    def request_quit(self, action="quit"):
        if self.quitting or self.dialog_open or self.control_pending:
            self.message("A Tools action is already pending. Please wait or finish the open dialog.")
            return
        if action in ("restart", "force-restart") and not self.restart_command:
            self.message("Restart is unavailable in this older controller. Relaunch Tools once to activate it.")
            return
        self.show()
        self.dialog_open = True
        self.pending_action = action
        threading.Thread(target=self.check_quit, daemon=True).start()

    def check_quit(self):
        try:
            status = self.connection.request("/api/status")
            GLib.idle_add(self.confirm_quit, status)
        except (OSError, ValueError, KeyError) as error:
            GLib.idle_add(self.quit_unavailable, str(error))

    def confirm_quit(self, status):
        self.quit_signature = work_signature(status)
        busy = busy_modules(status)
        force = self.pending_action == "force-restart"
        restart = self.pending_action in ("restart", "force-restart")
        if busy and not force:
            self.dialog_open = False
            self.message(f"Cannot {self.pending_action} while {', '.join(busy)} is busy or unavailable. Finish/cancel work first.")
            return GLib.SOURCE_REMOVE
        dialog = Gtk.MessageDialog(transient_for=self.window, modal=True,
                                   message_type=Gtk.MessageType.WARNING,
                                   text="Force restart Tools?" if force else "Restart Tools?" if restart else "Reload Tools UI?" if self.pending_action == "reload" else "Quit SHP PhotoCraft Tools?")
        dialog.format_secondary_text(
            ("Force Restart interrupts processing and discards unsaved previews/variations in ALL Tools windows. "
             "Interrupted Send may leave scratch documents or exchange remnants. PhotoCraft is never terminated. "
             f"Other active leases: {max(0, status.get('lifecycle', {}).get('leases', 1)-1)}. "
             "Only this supervisor's verified owned processes may be escalated. "
             if force else "Restart discards this window's unsent previews and restarts owned Tools services. "
             "Other connected windows must be closed first. "
             if restart else "Reloading resets this window's previews and view state without restarting servers. "
             if self.pending_action == "reload" else "This closes this window and discards its unsent previews and view state. ")
            + "Saved settings/results stay intact. "
            + ("The controller keeps the servers running."
               if self.pending_action == "reload" else
               "Other browser windows keep services alive on normal Quit; otherwise shutdown begins immediately."))
        dialog.add_button("Keep working", Gtk.ResponseType.CANCEL)
        dialog.add_button("Force Restart" if force else "Restart Tools" if restart else "Reload UI" if self.pending_action == "reload" else "Quit Tools", Gtk.ResponseType.ACCEPT)
        dialog.set_default_response(Gtk.ResponseType.CANCEL)
        response = dialog.run()
        dialog.destroy()
        if response == Gtk.ResponseType.ACCEPT:
            threading.Thread(target=self.finish_quit_check, args=(status,), daemon=True).start()
        else:
            self.dialog_open = False
        return GLib.SOURCE_REMOVE

    def finish_quit_check(self, before):
        try:
            after = self.connection.request("/api/status")
            if self.pending_action != "force-restart" and (busy_modules(after) or work_signature(before) != work_signature(after)):
                GLib.idle_add(self.quit_changed)
                return
            GLib.idle_add(self.reload_view if self.pending_action == "reload" else self.finish_quit)
        except (OSError, ValueError, KeyError) as error:
            GLib.idle_add(self.quit_unavailable, str(error))

    def quit_changed(self):
        self.dialog_open = False
        action = "Reload UI" if self.pending_action == "reload" else "Quit"
        self.message(f"Work changed or became busy while confirming. Review it and choose {action} again.")
        return GLib.SOURCE_REMOVE

    def quit_unavailable(self, error):
        if self.pending_action == "reload":
            self.dialog_open = False
            self.message("Cannot reload until the supervisor responds: " + error)
            return GLib.SOURCE_REMOVE
        dialog = Gtk.MessageDialog(transient_for=self.window, modal=True,
                                   message_type=Gtk.MessageType.WARNING, text="Tools supervisor is not responding")
        dialog.format_secondary_text(f"{error}\nClose only this desktop window? No process will be force-stopped. "
                                     "Active server work will finish under supervisor protection.")
        dialog.add_button("Keep open", Gtk.ResponseType.CANCEL)
        dialog.add_button("Close desktop", Gtk.ResponseType.ACCEPT)
        accepted = dialog.run() == Gtk.ResponseType.ACCEPT
        dialog.destroy()
        self.dialog_open = False
        if accepted:
            threading.Thread(target=self.connection.close, daemon=False).start()
            self.destroy_after_quit()
        return GLib.SOURCE_REMOVE

    def finish_quit(self):
        self.view.set_sensitive(False)
        self.message("Requesting acknowledged Tools shutdown...", persistent=True)
        threading.Thread(target=self.native_shutdown, daemon=True).start()
        return GLib.SOURCE_REMOVE

    def native_shutdown(self):
        try:
            restart = self.pending_action in ("restart", "force-restart")
            with self.connection.lease_lock:
                result = self.connection.request("/api/ui/native-quit", {
                    "client": self.connection.client, "confirm": True, "restart": restart,
                    "force": self.pending_action == "force-restart", "expected": self.quit_signature}, timeout=10)
                self.connection.stopped.set()
            if result.get("draining"):
                GLib.idle_add(self.message, "Stopping owned Tools services..." +
                              (" A fresh Tools window will open when shutdown completes." if restart else ""), True)
                self.wait_for_shutdown(result)
            if restart:
                if not result.get("draining"):
                    raise ValueError("Restart was not admitted; other windows retain the services")
                fields = Path(f"/proc/{os.getpid()}/stat").read_text().rsplit(")", 1)[1].split()
                command = [*self.restart_command, "--wait-for-exit", str(result["pid"]),
                           str(result["process_identity"][1]), "--wait-for-exit", str(os.getpid()), fields[19]]
                with (self.data / "restart.log").open("ab") as log:
                    subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT,
                                     stdin=subprocess.DEVNULL, start_new_session=True)
            GLib.idle_add(self.destroy_after_quit)
        except (OSError, ValueError, KeyError) as error:
            GLib.idle_add(self.shutdown_failed, str(error))

    def wait_for_shutdown(self, result):
        pid = result["pid"]
        try:
            fd = os.pidfd_open(pid)
        except ProcessLookupError:
            return
        try:
            try:
                fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
            except FileNotFoundError:
                return
            if int(fields[19]) != result["process_identity"][1]:
                return
            deadline = time.monotonic() + 90
            while not select.select([fd], [], [], 1)[0]:
                try:
                    health = self.connection.request("/api/health", timeout=2)
                    if health.get("shutdown_error"):
                        raise ValueError(health["shutdown_error"])
                except OSError:
                    pass  # The listening socket closes just before the supervisor exits.
                if time.monotonic() >= deadline:
                    raise ValueError("Services are still draining after 90 seconds; no duplicate restart was launched")
        finally:
            os.close(fd)

    def shutdown_failed(self, error):
        self.dialog_open = False
        self.view.set_sensitive(True)
        self.message("Tools shutdown/restart failed: " + error + ". No replacement instance was launched. Review the error before retrying.", persistent=True)
        return GLib.SOURCE_REMOVE

    def destroy_after_quit(self):
        self.cancel_notice_timer()
        self.quitting = True
        if self.indicator:
            self.indicator.set_status(AppIndicator3.IndicatorStatus.PASSIVE)
        self.window.destroy()
        self.release()
        self.quit()
        return GLib.SOURCE_REMOVE

    def reload_view(self):
        self.dialog_open = self.reload_available = False
        self.reload_button.hide()
        self.message("")
        self.view.reload()
        return GLib.SOURCE_REMOVE

    def do_shutdown(self):
        self.cancel_notice_timer()
        if self.geometry_timer:
            GLib.source_remove(self.geometry_timer)
            self.save_geometry()
        self.connection.stopped.set()
        Gtk.Application.do_shutdown(self)

    def policy(self, view, decision, kind):
        if kind in (WebKit2.PolicyDecisionType.NAVIGATION_ACTION, WebKit2.PolicyDecisionType.NEW_WINDOW_ACTION):
            uri = decision.get_navigation_action().get_request().get_uri()
            parts = urlsplit(uri)
            if uri.startswith("blob:" + self.base + "/") and kind != WebKit2.PolicyDecisionType.NEW_WINDOW_ACTION:
                decision.download()
                return True
            if uri == "about:blank" or (f"{parts.scheme}://{parts.netloc}" == self.base
                                        and not parts.username and not parts.password):
                if kind != WebKit2.PolicyDecisionType.NEW_WINDOW_ACTION:
                    decision.use()
                    return True
            decision.ignore()
            self.message(f"Blocked navigation outside the owned Tools window: {uri}")
            return True
        return False

    def popup(self, *_):
        self.message("Tools does not open popup windows. Use its embedded module controls.")
        return None

    def permission(self, view, request):
        request.deny()
        self.message("Browser device/notification permission denied; Tools does not require it.")
        return True

    def script_dialog(self, view, script):
        kind = script.get_dialog_type()
        request = script.get_message()
        if (kind == WebKit2.ScriptDialogType.PROMPT
                and request in ("SHP_OPEN_IMAGE_V1", "SHP_OPEN_BATCH_FOLDER_V1", "SHP_OPEN_BATCH_IMAGES_V1",
                                "SHP_OPEN_BATCH_SOURCE_V1")
                and view.get_uri() == self.base + "/"):
            self.dialog_open = True
            batch_source = request == "SHP_OPEN_BATCH_SOURCE_V1"
            if batch_source:
                mode = Gtk.MessageDialog(transient_for=self.window, modal=True,
                                         message_type=Gtk.MessageType.QUESTION, text="Choose batch source")
                mode.format_secondary_text("Choose one folder's direct images, or select image files from one folder.")
                mode.add_button("Cancel", Gtk.ResponseType.CANCEL)
                mode.add_button("Folder", 1)
                mode.add_button("Images", 2)
                response = mode.run()
                mode.destroy()
                if response not in (1, 2):
                    self.dialog_open = False
                    return True
                request = "SHP_OPEN_BATCH_FOLDER_V1" if response == 1 else "SHP_OPEN_BATCH_IMAGES_V1"
            folder = request == "SHP_OPEN_BATCH_FOLDER_V1"
            multiple = request == "SHP_OPEN_BATCH_IMAGES_V1"
            chooser = Gtk.FileChooserDialog(title="Choose batch folder" if folder else "Choose batch images" if multiple else "Open image",
                                            transient_for=self.window, modal=True,
                                            action=Gtk.FileChooserAction.SELECT_FOLDER if folder else Gtk.FileChooserAction.OPEN)
            if multiple:
                chooser.set_select_multiple(True)
            chooser.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Open", Gtk.ResponseType.ACCEPT)
            images = Gtk.FileFilter()
            images.set_name("Images")
            for extension in ("jpg", "jpeg", "png", "tif", "tiff", "webp"):
                images.add_pattern(f"*.{extension}")
                images.add_pattern(f"*.{extension.upper()}")
            if not folder:
                chooser.add_filter(images)
            try:
                if chooser.run() == Gtk.ResponseType.ACCEPT:
                    selected = ({"folder": chooser.get_filename()} if folder else {"paths": chooser.get_filenames()})
                    script.prompt_set_text(json.dumps(selected) if batch_source else
                                           json.dumps(chooser.get_filenames()) if multiple else chooser.get_filename())
            finally:
                chooser.destroy()
                self.dialog_open = False
            return True
        dialog = Gtk.MessageDialog(transient_for=self.window, modal=True,
                                   message_type=Gtk.MessageType.QUESTION, text=script.get_message())
        entry = None
        if kind != WebKit2.ScriptDialogType.ALERT:
            dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("OK", Gtk.ResponseType.OK)
        if kind == WebKit2.ScriptDialogType.PROMPT:
            entry = Gtk.Entry()
            entry.set_text(script.prompt_get_default_text() or "")
            dialog.get_content_area().pack_start(entry, False, False, 8)
            entry.show()
        accepted = dialog.run() == Gtk.ResponseType.OK
        if kind in (WebKit2.ScriptDialogType.CONFIRM, WebKit2.ScriptDialogType.BEFORE_UNLOAD_CONFIRM):
            script.confirm_set_confirmed(accepted)
        elif kind == WebKit2.ScriptDialogType.PROMPT and accepted:
            script.prompt_set_text(entry.get_text())
        dialog.destroy()
        return True

    def download(self, context, download):
        failed = [False]

        def destination(item, suggested):
            chooser = Gtk.FileChooserDialog(title="Save Tools download", transient_for=self.window,
                                            action=Gtk.FileChooserAction.SAVE)
            chooser.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Save", Gtk.ResponseType.ACCEPT)
            chooser.set_do_overwrite_confirmation(True)
            chooser.set_current_name(Path(suggested).name)
            if chooser.run() == Gtk.ResponseType.ACCEPT:
                item.set_destination(Path(chooser.get_filename()).as_uri())
            else:
                item.cancel()
            chooser.destroy()
            return True

        def failure(item, error):
            failed[0] = True
            self.message(f"Download stopped: {error.message}")

        download.connect("decide-destination", destination)
        download.connect("failed", failure)
        download.connect("finished", lambda *_: None if failed[0] else self.message("Download finished."))

    def load_failed(self, view, event, uri, error):
        self.reload_available = True
        self.reload_button.show()
        self.message(f"Could not load Tools: {error.message}. Reopen the launcher to check servers, then Reload UI.")
        return False

    def web_failed(self, view, reason):
        self.reload_available = True
        self.show()
        self.message(f"WebKit renderer exited ({reason}). Check desktop.log, then Reload UI; "
                     "server work is not force-stopped.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--icon", type=Path)
    parser.add_argument("--tray-icon", type=Path)
    parser.add_argument("--launcher-python")
    parser.add_argument("--launcher-config", type=Path)
    parser.add_argument("--check", action="store_true", help="check bindings/display only; no window or services")
    args = parser.parse_args()
    validate_url(args.url)
    ok, _ = Gtk.init_check([])
    if not ok:
        raise SystemExit("Tools desktop cannot connect to the graphical display")
    if args.check:
        print("GTK3/WebKit2 4.1/AppIndicator3 and graphical display available")
        return
    app = Desktop(args.url, args.data_dir, args.icon, args.tray_icon, args.launcher_python, args.launcher_config)
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, lambda: (app.request_quit(), True)[1])
    raise SystemExit(app.run([sys.argv[0]]))


if __name__ == "__main__":
    main()
