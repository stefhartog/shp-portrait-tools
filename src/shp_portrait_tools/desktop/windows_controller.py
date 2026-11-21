"""Windows desktop host: a pywebview (WebView2) window and a pystray tray icon for the Tools shell.

Run with the project's Python (`python -m shp_portrait_tools.desktop.windows_controller`, normally
started by the launcher). It speaks the same loopback shell protocol as the GTK controller; the
supervisor, not this process, owns and stops the services.
"""

import argparse
import ctypes
import hashlib
import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from .icons import icon_image, write_icon
from .protocol import (IMAGE_EXTENSIONS, OPEN_PROMPTS, Connection, busy_modules, external_lines, validate_url,
                       work_signature)

MODULE_LABELS = {"dnb": "Skin Tools"}
WEBVIEW2_CLIENT = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
MB_OK, MB_OKCANCEL, MB_YESNOCANCEL, MB_YESNO = 0, 1, 3, 4
MB_ICONERROR, MB_ICONQUESTION, MB_ICONWARNING, MB_DEFBUTTON2, MB_SETFOREGROUND = 0x10, 0x20, 0x30, 0x100, 0x10000
IDYES, IDNO = 6, 7


def webview2_version():
    import winreg
    for hive, view in ((winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_32KEY), (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY),
                       (winreg.HKEY_CURRENT_USER, 0)):
        try:
            with winreg.OpenKey(hive, rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_CLIENT}", 0,
                                winreg.KEY_READ | view) as key:
                version = winreg.QueryValueEx(key, "pv")[0]
                if version and version != "0.0.0.0":
                    return version
        except OSError:
            continue
    return None


class Dialogs:
    """Native message boxes; Yes always means the action named in the text, No means keep working."""

    def __init__(self, owner=lambda: None):
        self.owner = owner

    def _box(self, title, text, flags):
        return ctypes.windll.user32.MessageBoxW(self.owner(), text, title, flags | MB_SETFOREGROUND)

    def confirm(self, title, text, action):
        return self._box(title, f"{text}\n\nYes: {action}\nNo: Keep working", MB_YESNO | MB_ICONWARNING | MB_DEFBUTTON2) == IDYES

    def error(self, title, text):
        self._box(title, text, MB_OK | MB_ICONERROR)

    def alert(self, text):
        self._box("SHP Portrait Tools", text, MB_OK)

    def ok_cancel(self, text):
        return self._box("SHP Portrait Tools", text, MB_OKCANCEL) == 1

    def batch_source(self):
        answer = self._box("Choose batch source",
                           "Choose one folder's direct images, or select image files from one folder.\n\n"
                           "Yes: Folder\nNo: Images\nCancel: Cancel", MB_YESNOCANCEL | MB_ICONQUESTION)
        return {IDYES: "folder", IDNO: "images"}.get(answer)


class Picker:
    """pywebview file/folder pickers; each returns None when cancelled."""

    def __init__(self, window, dialogs):
        self.window, self.dialogs = window, dialogs

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

    def batch_source(self):
        return self.dialogs.batch_source()


def answer_open_prompt(request, picker):
    """The text for window.prompt(request) exactly as the GTK controller answers it; None means cancelled."""
    batch = request == "SHP_OPEN_BATCH_SOURCE_V1"
    if batch:
        choice = picker.batch_source()
        if choice is None:
            return None
        request = "SHP_OPEN_BATCH_FOLDER_V1" if choice == "folder" else "SHP_OPEN_BATCH_IMAGES_V1"
    if request == "SHP_OPEN_BATCH_FOLDER_V1":
        folder = picker.folder()
        return None if folder is None else json.dumps({"folder": folder}) if batch else folder
    if request == "SHP_OPEN_BATCH_IMAGES_V1":
        paths = picker.images()
        return None if not paths else json.dumps({"paths": paths} if batch else paths)
    return picker.image()


def tray_image():
    return icon_image(tray=True, size=64)


class Host:
    def __init__(self, url, data, launcher_python=None, launcher_config=None, dialogs=None, picker_factory=None):
        self.base = validate_url(url)
        self.data = data
        self.connection = Connection(self.base)
        self.restart_command = ([launcher_python, "-m", "shp_portrait_tools.launcher",
                                 "--config", str(launcher_config)] if launcher_python and launcher_config else None)
        self.window = self.icon = self.single = None
        self.dialogs = dialogs or Dialogs(self.owner_handle)
        self.picker_factory = picker_factory or (lambda window: Picker(window, self.dialogs))
        self.quitting = self.dialog_open = self.control_pending = self.tray_ready = self.hooked = False
        self.pending_action = "quit"
        self.status_epoch = 0
        self.modules = {}
        self.external_lines = external_lines({})
        self.watch_wake_tokens = {}
        self.geometry = {}
        self.geometry_timer = None
        self.last_notice = None
        self.quit_signature = None

    def owner_handle(self):
        try:
            return int(self.window.native.Handle.ToInt64())
        except Exception:
            return None

    def message(self, text, persistent=False):
        if not text or self.quitting or text == self.last_notice:
            return
        self.last_notice = text
        print(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {text}", file=sys.stderr, flush=True)
        if self.icon is not None and self.tray_ready:
            try:
                self.icon.notify(text[:250], "SHP Portrait Tools")
            except Exception as error:
                print(f"Tray notification failed: {error}", file=sys.stderr, flush=True)

    # Window ---------------------------------------------------------------------------------

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
        import webview
        self.data.mkdir(parents=True, exist_ok=True)
        self.load_geometry()
        webview.settings["ALLOW_DOWNLOADS"] = True
        options = {}
        if "x" in self.geometry:
            options.update(x=self.geometry["x"], y=self.geometry["y"])
        self.window = webview.create_window(
            "SHP Portrait Tools", html="<body style='background:#1b1b1b'></body>",
            width=self.geometry.get("width", 1399), height=self.geometry.get("height", 949),
            min_size=(363, 626), background_color="#1b1b1b", **options)
        events = self.window.events
        events.closing += self.on_closing
        events.loaded += self.on_loaded
        events.resized += lambda width, height: self.remember(width=width, height=height)
        events.moved += lambda x, y: self.remember(x=x, y=y)

    def on_closing(self):
        if self.quitting:
            return True
        if self.tray_ready:
            self.hide()
        else:
            self.request_quit()
        return False

    def on_loaded(self):
        # The first (placeholder) load only installs the native hooks, then the shell is loaded with them active.
        if self.hooked:
            return
        self.hooked = True
        from System import Action, Func, Type
        from System.Threading.Tasks import Task
        form = self.window.native

        def loaded_script(task):
            self.window.load_url(self.base + "/")

        def attach():
            core = form.webview.CoreWebView2
            # WebView2 raises ScriptDialogOpening only when its built-in dialogs are off; we then own all of them.
            core.Settings.AreDefaultScriptDialogsEnabled = False
            core.ScriptDialogOpening += self.on_script_dialog
            core.PermissionRequested += self.on_permission
            core.NewWindowRequested += self.on_new_window
            form.webview.NavigationStarting += self.on_navigation
            # Navigate only once the document-created script is registered so the first page already has the flags.
            core.AddScriptToExecuteOnDocumentCreatedAsync(
                "if (window === window.top) window.__shpOwnedDesktop = true;"
                f"if (location.origin === {json.dumps(self.base)}) window.__shpNativeFileDialog = true;"
            ).ContinueWith(Action[Task](loaded_script))

        form.Invoke(Func[Type](attach))

    def on_permission(self, sender, args):
        args.State = 2  # Deny
        self.message("Browser device/notification permission denied; Tools does not require it.")

    def on_new_window(self, sender, args):
        args.Handled = True
        self.message("Tools does not open popup windows. Use its embedded module controls.")

    def on_navigation(self, sender, args):
        uri = str(args.Uri)
        parts = urlsplit(uri)
        if (uri == "about:blank" or uri.startswith("blob:" + self.base + "/")
                or (f"{parts.scheme}://{parts.netloc}" == self.base and not parts.username and not parts.password)):
            return
        args.Cancel = True
        self.message(f"Blocked navigation outside the owned Tools window: {uri}")

    def on_script_dialog(self, sender, args):
        request, kind = str(args.Message), int(args.Kind)  # Alert=0, Confirm=1, Prompt=2, Beforeunload=3
        origin = urlsplit(str(args.Uri))
        if kind == 2 and request in OPEN_PROMPTS and f"{origin.scheme}://{origin.netloc}" == self.base:
            deferral = args.GetDeferral()
            threading.Thread(target=self.answer_prompt, args=(args, deferral, request), daemon=True).start()
        elif kind == 0:
            self.dialogs.alert(request)
            args.Accept()
        elif kind == 1:
            if self.dialogs.ok_cancel(request):
                args.Accept()
        elif kind == 3:
            args.Accept()
        # Other prompts stay cancelled (null): WebView2 only raises this event when its own dialogs are disabled.

    def answer_prompt(self, args, deferral, request):
        from System import Func, Type
        self.dialog_open = True
        answer = None
        try:
            answer = answer_open_prompt(request, self.picker_factory(self.window))
        except Exception as error:
            self.message(f"File chooser failed: {error}")
        finally:
            self.dialog_open = False

        def finish():
            if answer is not None:
                args.ResultText = answer
                args.Accept()
            deferral.Complete()

        self.window.native.Invoke(Func[Type](finish))

    def show(self):
        if self.window is None:
            return
        self.window.show()
        self.window.restore()
        hwnd = self.owner_handle()
        if hwnd:
            ctypes.windll.user32.SetForegroundWindow(hwnd)

    def hide(self):
        if self.tray_ready:
            self.window.hide()
        else:
            self.show()
            self.message("No tray icon is available. The window stays accessible; use Close to quit safely.")

    # Tray -----------------------------------------------------------------------------------

    def create_tray(self):
        import pystray
        Item, Menu = pystray.MenuItem, pystray.Menu

        def module_menu(name):
            label = MODULE_LABELS[name]
            idle = lambda item: bool(self.modules.get(name)) and not (self.control_pending or self.dialog_open)

            def action_item(action):
                def enabled(item):
                    state = self.modules.get(name, {})
                    phase = state.get("phase", "disconnected")
                    if not idle(item) or phase in ("starting", "stopping", "disconnected"):
                        return False
                    if action == "start":
                        return phase not in ("running", "unavailable")
                    return phase != "stopped" and not state.get("busy")
                return Item(action.capitalize(), lambda *_: self.request_module(name, action), enabled=enabled)

            def title(item):
                state = self.modules.get(name, {})
                phase = state.get("phase", "disconnected")
                status = "busy" if phase == "running" and state.get("busy") else phase
                if self.control_pending and getattr(self, "pending_module", None) and self.pending_module[0] == name \
                        and self.pending_module[1] != "startup":
                    status = {"start": "starting", "stop": "stopping", "restart": "restarting"}[self.pending_module[1]]
                return f"{label} - {status.capitalize()}"

            return Item(title, Menu(
                Item("Load on startup", lambda *_: self.request_module(name, "startup"),
                     checked=lambda item: self.modules.get(name, {}).get("startup_enabled", True), enabled=idle),
                action_item("start"), action_item("stop"), action_item("restart")))

        menu = Menu(
            Item(lambda item: self.external_lines[0], None, enabled=False),
            module_menu("dnb"),
            Item("Show Tools", lambda *_: self.show(), default=True),
            Item("Hide Tools", lambda *_: self.hide()),
            Item("Reload UI...", lambda *_: self.request_quit("reload")),
            Item("Restart Tools...", lambda *_: self.request_quit("restart")),
            Item("Force Restart Tools...", lambda *_: self.request_quit("force-restart")),
            Item("Quit Tools...", lambda *_: self.request_quit()))
        self.icon = pystray.Icon("shp-photocraft-tools", tray_image(), "SHP Portrait Tools", menu)
        self.icon.run_detached(setup=self.tray_started)

    def tray_started(self, icon):
        icon.visible = True
        self.tray_ready = True

    def refresh_tray(self):
        if self.icon is not None and self.tray_ready:
            try:
                self.icon.update_menu()
            except Exception:
                pass

    # Status and wake --------------------------------------------------------------------------

    def monitor(self):
        while not self.connection.stopped.is_set():
            epoch = self.status_epoch
            try:
                status = self.connection.request("/api/status")
                self.update_status(status, epoch)
                states = status.get("modules", {})
                if not any(state.get("busy") for state in states.values()):
                    for name in ("dnb",):
                        if states.get(name, {}).get("phase") == "running":
                            inbox = self.connection.request(f"/{name}/api/watch-folder")
                            inbox["service"] = name
                            self.wake_watch_folder(inbox, epoch)
            except (OSError, ValueError, KeyError) as error:
                self.update_status(None, epoch, "Tools connection lost: " + str(error))
            self.connection.stopped.wait(3)

    def wake_watch_folder(self, inbox, epoch):
        if self.quitting or self.control_pending or epoch != self.status_epoch or self.window is None:
            return
        service = inbox.get("service", "dnb")
        token = inbox.get("token") if inbox.get("ready") else None
        if token is None:
            self.watch_wake_tokens.pop(service, None)
        elif token != self.watch_wake_tokens.get(service):
            self.watch_wake_tokens[service] = token
            self.show()

    def update_status(self, status, epoch, problem=None):
        if self.quitting or self.control_pending or epoch != self.status_epoch:
            return
        self.modules = status["modules"] if status else {}
        self.external_lines = external_lines(status.get("external", {})) if status else ("PhotoCraft: Status unavailable",)
        self.refresh_tray()
        if problem:
            self.message(problem)
        elif self.connection.error:
            self.message("Desktop connection: " + self.connection.error)

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
        from ..shared.windows_process import process_identity
        try:
            restart = self.pending_action in ("restart", "force-restart")
            with self.connection.lease_lock:
                result = self.connection.request("/api/ui/native-quit", {
                    "client": self.connection.client, "confirm": True, "restart": restart,
                    "force": self.pending_action == "force-restart", "expected": self.quit_signature}, timeout=10)
                self.connection.stopped.set()
            if result.get("draining"):
                self.message("Stopping owned Tools services..." +
                             (" A fresh Tools window will open when shutdown completes." if restart else ""), True)
                self.wait_for_shutdown(result)
            if restart:
                if not result.get("draining"):
                    raise ValueError("Restart was not admitted; other windows retain the services")
                import os
                command = [*self.restart_command, "--wait-for-exit", str(result["pid"]),
                           str(result["process_identity"][1]), "--wait-for-exit", str(os.getpid()),
                           str(process_identity(os.getpid())[1])]
                from ..shared.windows_process import detached_options
                with (self.data / "restart.log").open("ab") as log:
                    subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT,
                                     stdin=subprocess.DEVNULL, **detached_options())
            self.destroy_after_quit()
        except (OSError, ValueError, KeyError) as error:
            self.dialog_open = False
            self.message("Tools shutdown/restart failed: " + str(error) +
                         ". No replacement instance was launched. Review the error before retrying.", True)

    def wait_for_shutdown(self, result):
        from ..shared.windows_process import is_alive
        pid, created = result["pid"], result["process_identity"][1]
        deadline = time.monotonic() + 90
        while is_alive(pid, created):
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
        if self.icon is not None:
            self.icon.stop()
        self.window.destroy()

    # Entry ------------------------------------------------------------------------------------

    def listen_for_second_launch(self):
        while not self.quitting:
            if self.single.wait_for_show(500):
                self.show()

    def run(self):
        from .windows_identity import set_process_app_id
        # Set the child host's identity before importing/creating any native GUI objects.
        set_process_app_id()
        import webview
        from ..shared.windows_process import SingleInstance
        self.single = SingleInstance("SHP.PhotoCraftTools.p" + hashlib.sha256(self.base.encode()).hexdigest()[:16])
        if not self.single.primary:
            self.single.signal_primary()
            print("Tools is already open; asked the existing window to show itself.", flush=True)
            return 0
        self.data.mkdir(parents=True, exist_ok=True)
        native_icon = self.data / "shp-photocraft-tools.ico"
        write_icon(native_icon)
        self.create_window()
        self.create_tray()
        for target in (self.connection.heartbeat, self.monitor, self.listen_for_second_launch):
            threading.Thread(target=target, daemon=True).start()
        webview.start(gui="edgechromium", private_mode=False, storage_path=str(self.data / "data"),
                      icon=str(native_icon))
        self.connection.stopped.set()
        return 0


def check_runtime():
    try:
        import pystray  # noqa: F401
        import webview  # noqa: F401
    except ImportError as error:
        raise SystemExit(f"Missing Windows desktop dependency: {error}")
    version = webview2_version()
    if version is None:
        raise SystemExit("Microsoft Edge WebView2 Runtime is not installed")
    print(f"pywebview, pystray and WebView2 {version} available")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--launcher-python")
    parser.add_argument("--launcher-config", type=Path)
    parser.add_argument("--check", action="store_true", help="check dependencies/runtime only; no window or services")
    args = parser.parse_args(argv)
    validate_url(args.url)
    if args.check:
        return check_runtime()
    raise SystemExit(Host(args.url, args.data_dir, args.launcher_python, args.launcher_config).run())


if __name__ == "__main__":
    main()
