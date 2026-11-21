"""Windows desktop host: a pywebview (WebView2) window and a pystray tray icon for the Tools shell.

Run with the project's Python (`python -m shp_portrait_tools.desktop.windows_controller`, normally
started by the launcher). It speaks the same loopback shell protocol as the GTK controller; the
supervisor, not this process, owns and stops the services. Shared behaviour (geometry, status, tray
model, module controls, quit/restart flow) is in `host.DesktopHost`; this module adds only the Win32
dialogs, WebView2 hooks, pystray widget and Windows process helpers.
"""

import argparse
import ctypes
import hashlib
import json
import sys
import threading
from pathlib import Path
from urllib.parse import urlsplit

from .host import MODULE_LABELS, DesktopHost, answer_open_prompt  # noqa: F401 (re-exported for callers/tests)
from .icons import icon_image, write_icon
from .protocol import IMAGE_EXTENSIONS, OPEN_PROMPTS, validate_url

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


def tray_image():
    return icon_image(tray=True, size=64)


class Host(DesktopHost):
    def __init__(self, url, data, launcher_python=None, launcher_config=None, dialogs=None, picker_factory=None):
        super().__init__(url, data, launcher_python, launcher_config)
        self.dialogs = dialogs or Dialogs(self.owner_handle)
        self.picker_factory = picker_factory or (lambda window: Picker(window, self.dialogs))

    # Platform hooks ---------------------------------------------------------------------------

    def owner_handle(self):
        try:
            return int(self.window.native.Handle.ToInt64())
        except Exception:
            return None

    def notify(self, text):
        if self.icon is not None and self.tray_ready:
            self.icon.notify(text[:250], "SHP Portrait Tools")

    def raise_window(self):
        hwnd = self.owner_handle()
        if hwnd:
            ctypes.windll.user32.SetForegroundWindow(hwnd)

    def own_identity(self):
        import os
        from ..shared.windows_process import process_identity
        return os.getpid(), process_identity(os.getpid())[1]

    def process_alive(self, pid, created):
        from ..shared.windows_process import is_alive
        return is_alive(pid, created)

    def spawn_detached(self, command, log):
        import subprocess
        from ..shared.windows_process import detached_options
        subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, **detached_options())

    # WebView2 hooks ---------------------------------------------------------------------------

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

    # Tray (pystray widget over the shared tray model) -----------------------------------------

    def create_tray(self):
        import pystray
        Item, Menu = pystray.MenuItem, pystray.Menu

        def module_menu(name):
            def action_item(action):
                return Item(action.capitalize(), lambda *_: self.request_module(name, action),
                            enabled=lambda item: self.module_action_enabled(name, action))

            return Item(lambda item: self.module_title(name), Menu(
                Item("Load on startup", lambda *_: self.request_module(name, "startup"),
                     checked=lambda item: self.startup_checked(name), enabled=lambda item: self.module_idle(name)),
                action_item("start"), action_item("stop"), action_item("restart")))

        actions = [Item(label, lambda *_, run=run: run(), default=index == 0)
                   for index, (label, run) in enumerate(self.window_actions())]
        menu = Menu(Item(lambda item: self.external_lines[0], None, enabled=False),
                    *(module_menu(name) for name in MODULE_LABELS), *actions)
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

    def stop_tray(self):
        if self.icon is not None:
            self.icon.stop()

    # Entry ------------------------------------------------------------------------------------

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
