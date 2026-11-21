"""Platform-neutral desktop host: window geometry, status, tray model and the quit flow.

The window first shows the supervisor's status page (`base`), which moves on to the Skin Tools page served
directly by the worker (`page`) once it is running; after a stop or restart the host shows the status page
again.

A platform host (`windows_controller.Host` on Windows, `qt_controller.Host` on Linux) subclasses `DesktopHost` and
supplies only what differs per platform: native dialogs and file pickers, webview engine hooks, the tray
widget, bringing the window to the front, single-instance handling, starting the GUI loop, and the
process-identity helpers used to wait for a drained supervisor before a restart. The loopback shell
protocol, the supervisor's ownership checks and every confirmation rule live here once.
"""

import json
import sys
import threading
import time

from .protocol import IMAGE_EXTENSIONS, Connection, busy_modules, external_lines, validate_url, work_signature

MODULE_LABELS = {"skin": "Skin Tools"}
WINDOW_TITLE = "SHP Portrait Tools"
DEFAULT_SIZE, MINIMUM_SIZE, BACKGROUND = (1399, 949), (363, 626), "#1b1b1b"


class Picker:
    """pywebview's native file/folder dialogs; each returns None when cancelled."""

    def __init__(self, window):
        self.window = window

    def _open(self, kind, multiple=False):
        import webview
        filters = (f"Images ({';'.join('*.' + extension for extension in IMAGE_EXTENSIONS)})",)
        dialog = webview.FileDialog.FOLDER if kind == "folder" else webview.FileDialog.OPEN
        result = self.window.create_file_dialog(dialog, allow_multiple=multiple,
                                                file_types=() if kind == "folder" else filters)
        return list(result) if result else None

    def image(self):
        paths = self._open("image")
        return paths[0] if paths else None

    def images(self):
        return self._open("images", True)

    def folder(self):
        paths = self._open("folder")
        return paths[0] if paths else None


class PickerApi:
    """The only calls the page can make through pywebview's JS bridge (`window.pywebview.api`).

    pywebview exposes this object's public methods; the host reference is private so nothing else is
    reachable from JavaScript. Each call returns a path, a list of paths, or null when cancelled.
    """

    def __init__(self, host):
        self._host = host

    def pick_image(self):
        return self._host.pick("image")

    def pick_images(self):
        return self._host.pick("images")

    def pick_folder(self):
        return self._host.pick("folder")


class DesktopHost:
    """Shared host behaviour. Subclasses must set `self.dialogs` and implement the platform hooks below."""

    restart_message = " A fresh Tools window will open when shutdown completes."

    def __init__(self, url, data, launcher_python=None, launcher_config=None):
        self.base = validate_url(url)  # supervisor: status page, controls, quit, leases
        self.page = None              # worker: the Skin Tools page, from the supervisor's health
        self.shown_instance = None
        self.data = data
        self.connection = Connection(self.base)
        self.restart_command = ([launcher_python, "-m", "shp_portrait_tools.launcher",
                                 "--config", str(launcher_config)] if launcher_python and launcher_config else None)
        self.window = self.icon = self.single = None
        self.dialogs = None
        self.picker_factory = Picker
        self.quitting = self.dialog_open = self.control_pending = self.tray_ready = self.hooked = False
        self.pending_action = "quit"
        self.pending_module = None
        self.status_epoch = 0
        self.modules = {}
        self.external_lines = external_lines({})
        self.geometry = {}
        self.geometry_timer = None
        self.last_notice = None
        self.quit_signature = None

    # Platform hooks ---------------------------------------------------------------------------

    def notify(self, text):
        """Show a short notification (tray balloon/toast); optional."""

    def raise_window(self):
        """Bring the shown window to the foreground; optional."""

    def refresh_tray(self):
        """Re-read the tray model after state changes; optional."""

    def stop_tray(self):
        """Remove the tray icon on quit; optional."""

    def own_identity(self):
        """(pid, creation token) of this host process, for the launcher's --wait-for-exit."""
        raise NotImplementedError

    def process_alive(self, pid, created):
        raise NotImplementedError

    def spawn_detached(self, command, log):
        raise NotImplementedError

    # Page address -----------------------------------------------------------------------------

    def resolve_page(self, attempts=40, delay=0.25):
        """The Skin Tools page origin from the supervisor's configured worker port."""
        for _ in range(attempts):
            try:
                port = self.connection.request("/api/health", timeout=2)["ports"]["skin"]
                self.page = validate_url(f"http://127.0.0.1:{int(port)}/")
                return self.page
            except (OSError, ValueError, KeyError, TypeError):
                time.sleep(delay)
        self.message("Could not read the Skin Tools address from Tools; the window shows the status page only.")
        return None

    def trusted_origin(self, origin):
        return origin == self.base or (self.page is not None and origin == self.page)

    def show_status_page(self):
        if self.window is not None and not self.quitting:
            self.window.load_url(self.base + "/")

    # Messages ---------------------------------------------------------------------------------

    def message(self, text, persistent=False):
        if not text or self.quitting or text == self.last_notice:
            return
        self.last_notice = text
        print(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {text}", file=sys.stderr, flush=True)
        try:
            self.notify(text)
        except Exception as error:
            print(f"Desktop notification failed: {error}", file=sys.stderr, flush=True)

    # Window -----------------------------------------------------------------------------------

    def load_geometry(self):
        path = self.data / "window.json"
        try:
            geometry = json.loads(path.read_text())
            if (not isinstance(geometry, dict)
                    or any(type(geometry.get(key)) is not int or geometry[key] <= 0 for key in ("width", "height"))
                    or any(key in geometry and type(geometry[key]) is not int for key in ("x", "y"))
                    or (("x" in geometry) != ("y" in geometry))):
                raise ValueError("Invalid saved window geometry")
            self.geometry = geometry
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as error:
            print(f"Cannot restore window size from {path}: {error}", file=sys.stderr, flush=True)

    def remember(self, **values):
        if any(v <= -30000 for v in values.values()):  # minimized windows report off-screen coordinates
            return
        self.geometry.update(values)
        if self.geometry_timer:
            self.geometry_timer.cancel()
        self.geometry_timer = threading.Timer(0.5, self.save_geometry)
        self.geometry_timer.daemon = True
        self.geometry_timer.start()

    def save_geometry(self):
        if not {"width", "height"} <= set(self.geometry):
            return
        path = self.data / "window.json"
        temporary = path.with_suffix(".tmp")
        try:
            temporary.write_text(json.dumps(self.geometry) + "\n")
            temporary.replace(path)
        except OSError as error:
            self.message(f"Could not save window size: {error}")

    def create_window(self):
        """A pywebview window on a blank page; `on_loaded` installs engine hooks, then loads the shell."""
        import webview
        self.data.mkdir(parents=True, exist_ok=True)
        self.load_geometry()
        webview.settings["ALLOW_DOWNLOADS"] = True
        options = {}
        if "x" in self.geometry:
            options.update(x=self.geometry["x"], y=self.geometry["y"])
        self.window = webview.create_window(
            WINDOW_TITLE, html=f"<body style='background:{BACKGROUND}'></body>",
            width=self.geometry.get("width", DEFAULT_SIZE[0]), height=self.geometry.get("height", DEFAULT_SIZE[1]),
            min_size=MINIMUM_SIZE, background_color=BACKGROUND, js_api=PickerApi(self), **options)
        events = self.window.events
        events.closing += self.on_closing
        events.loaded += self.on_loaded
        events.resized += lambda width, height: self.remember(width=width, height=height)
        events.moved += lambda x, y: self.remember(x=x, y=y)

    def on_loaded(self):
        """Default: no engine hooks, load the status page once."""
        if self.hooked:
            return
        self.hooked = True
        self.window.load_url(self.base + "/")

    def pick(self, kind):
        """Run one native picker for the page; tray actions wait while it is open."""
        if self.quitting or self.dialog_open or kind not in ("image", "images", "folder"):
            return None
        self.dialog_open = True
        try:
            result = getattr(self.picker_factory(self.window), kind)()
        except Exception as error:
            self.message(f"File chooser failed: {error}")
            return None
        finally:
            self.dialog_open = False
        if kind == "images":
            return [str(path) for path in result] if result else None
        return str(result) if result else None

    def on_closing(self):
        if self.quitting:
            return True
        if self.tray_ready:
            self.hide()
        else:
            self.request_quit()
        return False

    def show(self):
        if self.window is None:
            return
        self.window.show()
        self.window.restore()
        self.raise_window()

    def hide(self):
        if self.tray_ready:
            self.window.hide()
        else:
            self.show()
            self.message("No tray icon is available. The window stays accessible; use Close to quit safely.")

    # Tray model (the widget is platform-specific) ------------------------------------------------

    def module_idle(self, name):
        return bool(self.modules.get(name)) and not (self.control_pending or self.dialog_open)

    def module_action_enabled(self, name, action):
        state = self.modules.get(name, {})
        phase = state.get("phase", "disconnected")
        if not self.module_idle(name) or phase in ("starting", "stopping", "disconnected"):
            return False
        if action == "start":
            return phase not in ("running", "unavailable")
        return phase != "stopped" and not state.get("busy")

    def module_title(self, name):
        state = self.modules.get(name, {})
        phase = state.get("phase", "disconnected")
        status = "busy" if phase == "running" and state.get("busy") else phase
        if self.control_pending and self.pending_module and self.pending_module[0] == name \
                and self.pending_module[1] != "startup":
            status = {"start": "starting", "stop": "stopping", "restart": "restarting"}[self.pending_module[1]]
        return f"{MODULE_LABELS[name]} - {status.capitalize()}"

    def startup_checked(self, name):
        return self.modules.get(name, {}).get("startup_enabled", True)

    def window_actions(self):
        """(label, callback) rows after the module submenu; the first one is the default action."""
        return [("Show Tools", self.show), ("Hide Tools", self.hide),
                ("Reload UI...", lambda: self.request_quit("reload")),
                ("Restart Tools...", lambda: self.request_quit("restart")),
                ("Force Restart Tools...", lambda: self.request_quit("force-restart")),
                ("Quit Tools...", self.request_quit)]

    # Status and wake --------------------------------------------------------------------------

    def monitor(self):
        while not self.connection.stopped.is_set():
            epoch = self.status_epoch
            try:
                status = self.connection.request("/api/status")
                self.update_status(status, epoch)
            except (OSError, ValueError, KeyError) as error:
                self.update_status(None, epoch, "Tools connection lost: " + str(error))
            self.connection.stopped.wait(3)

    def update_status(self, status, epoch, problem=None):
        if self.quitting or self.control_pending or epoch != self.status_epoch:
            return
        self.modules = status["modules"] if status else {}
        self.external_lines = external_lines(status.get("external", {})) if status else ("PhotoCraft: Status unavailable",)
        self.follow_worker(self.modules.get("skin", {}))
        self.refresh_tray()
        if problem:
            self.message(problem)
        elif self.connection.error:
            self.message("Desktop connection: " + self.connection.error)

    def follow_worker(self, state):
        """Back to the status page when Skin Tools stops or is replaced; it moves on to the page when ready."""
        phase, instance = state.get("phase"), state.get("instance")
        if phase == "running" and instance:
            if self.shown_instance not in (None, instance):
                self.show_status_page()
            self.shown_instance = instance
        elif phase in ("stopped", "error") and self.shown_instance is not None:
            self.shown_instance = None
            self.show_status_page()

    # Module controls --------------------------------------------------------------------------

    def request_module(self, name, action):
        if self.quitting or self.dialog_open or self.control_pending or name not in MODULE_LABELS:
            return
        enabled = not self.modules.get(name, {}).get("startup_enabled", True)
        self.control_pending = True
        self.pending_module = (name, action)
        self.status_epoch += 1
        body = {"instance": self.modules.get(name, {}).get("instance"), "confirm": False, "revision": None}
        if action == "startup":
            body["enabled"] = enabled
        self.refresh_tray()
        threading.Thread(target=self.module_worker, args=(name, action, body), daemon=True).start()

    def module_worker(self, name, action, body):
        try:
            accepted, result = self.connection.control(name, action, body)
        except (OSError, ValueError, KeyError) as error:
            accepted, result = False, {"error": f"Supervisor request failed: {error}. Check shell.log.",
                                       "code": "connection"}
        label = MODULE_LABELS[name]
        try:
            if not accepted and result.get("code") == "confirmation" and not body["confirm"]:
                self.dialog_open = True
                self.show()
                ok = self.dialogs.confirm(f"{action.capitalize()} {label}?",
                                          result["error"] + "\nThis affects every window using this module.",
                                          action.capitalize())
                self.dialog_open = False
                if ok:
                    return self.module_worker(name, action, dict(body, confirm=True, revision=result["revision"]))
            elif not accepted:
                if result.get("code") == "connection":
                    self.modules = {}
                error = result.get("error", "Invalid supervisor response")
                self.message(f"{label} {action} failed: {error}")
                self.dialog_open = True
                self.show()
                self.dialogs.error(f"Cannot {action} {label}", error)
                self.dialog_open = False
            else:
                self.modules[name] = result
                self.message(f"{label} load on startup {'enabled' if result['startup_enabled'] else 'disabled'}; "
                             "current work unchanged." if action == "startup"
                             else f"{label} {'stopped' if action == 'stop' else 'ready'}.")
        finally:
            self.dialog_open = False
            self.control_pending = False
            self.pending_module = None
            self.status_epoch += 1
            self.refresh_tray()

    # Quit / restart / reload ------------------------------------------------------------------

    def request_quit(self, action="quit"):
        if self.quitting or self.dialog_open or self.control_pending:
            self.message("A Tools action is already pending. Please wait or finish the open dialog.")
            return
        if action in ("restart", "force-restart") and not self.restart_command:
            self.message("Restart is unavailable: this host was not started by the launcher.")
            return
        self.dialog_open = True
        self.pending_action = action
        threading.Thread(target=self.check_quit, daemon=True).start()

    def check_quit(self):
        try:
            status = self.connection.request("/api/status")
        except (OSError, ValueError, KeyError) as error:
            return self.quit_unavailable(str(error))
        self.show()
        self.confirm_quit(status)

    def confirm_quit(self, status):
        self.quit_signature = work_signature(status)
        busy = busy_modules(status)
        action = self.pending_action
        force, restart = action == "force-restart", action in ("restart", "force-restart")
        if busy and not force:
            self.dialog_open = False
            self.message(f"Cannot {action} while {', '.join(busy)} is busy or unavailable. Finish/cancel work first.")
            return
        leases = max(0, status.get("lifecycle", {}).get("leases", 1) - 1)
        if force:
            title = "Force restart Tools?"
            text = ("Force Restart interrupts processing and discards unsaved previews/variations in ALL Tools windows. "
                    "Interrupted Send may leave scratch documents or exchange remnants. PhotoCraft is never terminated. "
                    f"Other active leases: {leases}. Only this supervisor's verified owned processes may be escalated. ")
        elif restart:
            title = "Restart Tools?"
            text = ("Restart discards this window's unsent previews and restarts owned Tools services. "
                    "Other connected windows must be closed first. ")
        elif action == "reload":
            title = "Reload Tools UI?"
            text = "Reloading resets this window's previews and view state without restarting servers. "
        else:
            title = "Quit SHP Portrait Tools?"
            text = "This closes this window and discards its unsent previews and view state. "
        text += "Saved settings/results stay intact. " + (
            "The controller keeps the servers running." if action == "reload" else
            "Other browser windows keep services alive on normal Quit; otherwise shutdown begins immediately.")
        label = {"force-restart": "Force Restart", "restart": "Restart Tools", "reload": "Reload UI"}.get(action, "Quit Tools")
        if not self.dialogs.confirm(title, text, label):
            self.dialog_open = False
            return
        self.finish_quit_check(status)

    def finish_quit_check(self, before):
        try:
            after = self.connection.request("/api/status")
        except (OSError, ValueError, KeyError) as error:
            return self.quit_unavailable(str(error))
        if self.pending_action != "force-restart" and (busy_modules(after) or work_signature(before) != work_signature(after)):
            self.dialog_open = False
            self.message("Work changed or became busy while confirming. Review it and choose again.")
        elif self.pending_action == "reload":
            self.dialog_open = False
            self.window.evaluate_js("location.reload()")
        else:
            self.message("Requesting acknowledged Tools shutdown...", persistent=True)
            self.native_shutdown()

    def quit_unavailable(self, error):
        if self.pending_action == "reload":
            self.dialog_open = False
            return self.message("Cannot reload until the supervisor responds: " + error)
        self.show()
        if self.dialogs.confirm("Tools supervisor is not responding",
                                f"{error}\nClose only this desktop window? No process will be force-stopped. "
                                "Active server work will finish under supervisor protection.", "Close desktop"):
            self.connection.close()
            self.destroy_after_quit()
        else:
            self.dialog_open = False

    def native_shutdown(self):
        try:
            restart = self.pending_action in ("restart", "force-restart")
            with self.connection.lease_lock:
                result = self.connection.request("/api/ui/native-quit", {
                    "client": self.connection.client, "confirm": True, "restart": restart,
                    "force": self.pending_action == "force-restart", "expected": self.quit_signature}, timeout=10)
                self.connection.stopped.set()
            if result.get("draining"):
                self.message("Stopping owned Tools services..." + (self.restart_message if restart else ""), True)
                self.wait_for_shutdown(result)
            if restart:
                if not result.get("draining"):
                    raise ValueError("Restart was not admitted; other windows retain the services")
                pid, created = self.own_identity()
                command = [*self.restart_command, "--wait-for-exit", str(result["pid"]),
                           str(result["process_identity"][1]), "--wait-for-exit", str(pid), str(created)]
                with (self.data / "restart.log").open("ab") as log:
                    self.spawn_detached(command, log)
            self.destroy_after_quit()
        except (OSError, ValueError, KeyError) as error:
            self.dialog_open = False
            self.message("Tools shutdown/restart failed: " + str(error) +
                         ". No replacement instance was launched. Review the error before retrying.", True)

    def wait_for_shutdown(self, result):
        pid, created = result["pid"], result["process_identity"][1]
        deadline = time.monotonic() + 90
        while self.process_alive(pid, created):
            try:
                health = self.connection.request("/api/health", timeout=2)
                if health.get("shutdown_error"):
                    raise ValueError(health["shutdown_error"])
            except OSError:
                pass  # The listening socket closes just before the supervisor exits.
            if time.monotonic() >= deadline:
                raise ValueError("Services are still draining after 90 seconds; no duplicate restart was launched")
            time.sleep(0.2)

    def destroy_after_quit(self):
        self.quitting = True
        if self.geometry_timer:
            self.geometry_timer.cancel()
            self.save_geometry()
        self.stop_tray()
        self.window.destroy()

    def listen_for_second_launch(self):
        while not self.quitting:
            if self.single.wait_for_show(500):
                self.show()
