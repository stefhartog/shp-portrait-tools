"""Linux desktop host: a pywebview (Qt WebEngine) window and a QSystemTrayIcon for SHP Portrait Tools.

Run with the project's Python (`python -m shp_portrait_tools.desktop.qt_controller`, normally started by the
launcher). Shared behaviour (geometry, status, tray model, module controls, quit/restart flow) is in
`host.DesktopHost`; this module adds only the Qt dialogs, WebEngine hooks, tray widget, single-instance
socket and Linux process helpers. The supervisor, not this process, owns and stops the services.

Threading: this process creates the QApplication on the main thread before pywebview starts, so the tray,
the single-instance server and the `Invoker` live on the GUI thread. pywebview's window calls block until the
GUI thread handles them, so they are made only from worker threads; tray actions therefore run on a short
worker thread, and worker threads reach Qt widgets through `Invoker`.
"""

import argparse
import hashlib
import io
import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from urllib.parse import urlsplit

from .host import MODULE_LABELS, WINDOW_TITLE, DesktopHost
from .protocol import validate_url

APP_ID = "shp-photocraft-tools"  # matches the installed .desktop file and its StartupWMClass


def navigation_allowed(url, trusted_origin):
    """The pages the window may show: the two Tools origins, about:blank and their blob: URLs."""
    if url == "about:blank":
        return True
    inner = url[5:] if url.startswith("blob:") else url
    parts = urlsplit(inner)
    if parts.scheme != "http" or parts.username or parts.password:
        return False
    return trusted_origin(f"{parts.scheme}://{parts.netloc}")


def document_script(page_origin):
    """Flags the page reads at document creation (same as the Windows host)."""
    return ("if (window === window.top) window.__shpOwnedDesktop = true;"
            f"if (location.origin === {json.dumps(page_origin)}) window.__shpNativeFileDialog = true;")


def single_instance_name(base):
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    name = "shp-portrait-tools-" + hashlib.sha256(base.encode()).hexdigest()[:16]
    return str(Path(runtime) / name) if runtime and Path(runtime).is_dir() else name


class Invoker:
    """Runs callables on the GUI thread; `wait` also returns the result to a worker thread."""

    def __init__(self):
        from qtpy.QtCore import QObject, Qt, QThread, Signal

        class Relay(QObject):
            call = Signal(object)

        self._gui_thread = QThread.currentThread()
        self._current = QThread.currentThread
        self._relay = Relay()
        self._relay.call.connect(lambda function: function(), Qt.ConnectionType.QueuedConnection)

    def later(self, function):
        self._relay.call.emit(function)

    def wait(self, function):
        if self._current() == self._gui_thread:
            return function()
        done, box = threading.Event(), {}

        def run():
            try:
                box["value"] = function()
            except BaseException as error:  # handed back to the waiting thread
                box["error"] = error
            finally:
                done.set()

        self.later(run)
        done.wait()
        if "error" in box:
            raise box["error"]
        return box.get("value")


class Dialogs:
    """Qt message boxes, shown on the GUI thread; the default button keeps working."""

    def __init__(self, invoker, owner=lambda: None):
        self.invoker, self.owner = invoker, owner

    def confirm(self, title, text, action):
        def ask():
            from qtpy.QtWidgets import QMessageBox
            box = QMessageBox(QMessageBox.Icon.Warning, title, text, QMessageBox.StandardButton.NoButton, self.owner())
            accept = box.addButton(action, QMessageBox.ButtonRole.AcceptRole)
            keep = box.addButton("Keep working", QMessageBox.ButtonRole.RejectRole)
            box.setDefaultButton(keep)
            box.setEscapeButton(keep)
            box.exec()
            return box.clickedButton() is accept
        return self.invoker.wait(ask)

    def error(self, title, text):
        def show():
            from qtpy.QtWidgets import QMessageBox
            QMessageBox.critical(self.owner(), title, text)
        self.invoker.wait(show)


def qt_icon(tray=False):
    from qtpy.QtGui import QIcon, QPixmap
    from .icons import icon_image
    buffer = io.BytesIO()
    icon_image(tray=tray, size=64 if tray else 256).save(buffer, format="PNG")
    pixmap = QPixmap()
    pixmap.loadFromData(buffer.getvalue(), "PNG")
    return QIcon(pixmap)


def install_page_class(host):
    """Swap pywebview's WebEngine page class for one with the Tools navigation, popup and permission rules.

    This subclasses QWebEnginePage directly: pywebview's own page calls `super(BrowserView.WebPage, self)`,
    which would recurse once the attribute names a subclass. Its extras are not needed here: it grants
    microphone/camera requests (Tools denies all), turns popups into navigation (Tools blocks them), and
    handles transparency and user agents, which Tools does not set.
    """
    from qtpy.QtWebEngineCore import QWebEnginePage, QWebEngineScript
    from webview.platforms import qt as backend

    if getattr(backend.BrowserView.WebPage, "shp_guarded", False):
        return

    class GuardedPage(QWebEnginePage):
        shp_guarded = True

        def __init__(self, parent=None, profile=None):
            super().__init__(profile, parent.webview)
            self.featurePermissionRequested.connect(self.onFeaturePermissionRequested)
            self.permissionRequested.connect(self.deny_permission)
            script = QWebEngineScript()
            script.setName("shp-tools-flags")
            script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
            script.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
            script.setRunsOnSubFrames(False)
            script.setSourceCode(document_script(host.page or host.base))
            self.scripts().insert(script)

        def acceptNavigationRequest(self, url, navigation_type, is_main_frame):
            target = url.toString()
            # Before the hooks are armed the window only shows its own placeholder HTML.
            if not host.hooked or navigation_allowed(target, host.trusted_origin):
                return True
            host.message(f"Blocked navigation outside the owned Tools window: {target}")
            return False

        def createWindow(self, window_type):
            host.message("Tools does not open popup windows. Use its embedded module controls.")
            return None

        def onFeaturePermissionRequested(self, url, feature):
            self.setFeaturePermission(url, feature, QWebEnginePage.PermissionPolicy.PermissionDeniedByUser)
            host.message("Browser device/notification permission denied; Tools does not require it.")

        def deny_permission(self, permission):
            permission.deny()
            host.message("Browser device/notification permission denied; Tools does not require it.")

    backend.BrowserView.WebPage = GuardedPage


class Host(DesktopHost):
    def __init__(self, url, data, launcher_python=None, launcher_config=None, dialogs=None, picker_factory=None):
        super().__init__(url, data, launcher_python, launcher_config)
        self.invoker = None
        self.dialogs = dialogs
        if picker_factory:
            self.picker_factory = picker_factory
        self.menu = self.menu_items = self.single_server = None

    # Platform hooks ---------------------------------------------------------------------------

    def native(self):
        return getattr(self.window, "native", None) if self.window is not None else None

    def notify(self, text):
        if self.icon is not None and self.tray_ready:
            self.invoker.later(lambda: self.icon.showMessage(WINDOW_TITLE, text[:250]))

    def raise_window(self):
        def bring():
            native = self.native()
            if native is not None:
                native.raise_()
                native.activateWindow()
        if self.invoker:
            self.invoker.later(bring)

    def own_identity(self):
        from ..shared.owned_process import process_identity
        return os.getpid(), process_identity(os.getpid())[1]

    def process_alive(self, pid, created):
        from ..shared.owned_process import process_identity
        try:
            return process_identity(pid)[1] == created
        except (FileNotFoundError, ProcessLookupError):
            return False

    def spawn_detached(self, command, log):
        subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                         start_new_session=True)

    # Tray (QSystemTrayIcon over the shared tray model) -----------------------------------------

    def in_worker(self, function):
        """Tray callbacks run on the GUI thread; pywebview calls must not, so hand them to a worker."""
        threading.Thread(target=function, daemon=True).start()

    def create_tray(self):
        from qtpy.QtWidgets import QMenu, QSystemTrayIcon
        if not QSystemTrayIcon.isSystemTrayAvailable():
            print("No system tray is available (on GNOME, enable the AppIndicator extension); "
                  "closing the window quits Tools.", file=sys.stderr, flush=True)
            return
        menu = QMenu()
        external = menu.addAction("")
        external.setEnabled(False)
        modules = {}
        for name in MODULE_LABELS:
            submenu = menu.addMenu("")
            startup = submenu.addAction("Load on startup")
            startup.setCheckable(True)
            startup.triggered.connect(lambda *_, name=name: self.request_module(name, "startup"))
            actions = {}
            for action in ("start", "stop", "restart"):
                item = submenu.addAction(action.capitalize())
                item.triggered.connect(lambda *_, name=name, action=action: self.request_module(name, action))
                actions[action] = item
            modules[name] = (submenu, startup, actions)
        rows = self.window_actions()
        for label, run in rows:
            menu.addAction(label).triggered.connect(lambda *_, run=run: self.in_worker(run))
        self.menu, self.menu_items = menu, (external, modules)
        menu.aboutToShow.connect(self.sync_tray)
        icon = QSystemTrayIcon(qt_icon(tray=True))
        icon.setToolTip(WINDOW_TITLE)
        icon.setContextMenu(menu)
        default = rows[0][1]
        icon.activated.connect(lambda reason: self.in_worker(default)
                               if reason == QSystemTrayIcon.ActivationReason.Trigger else None)
        self.icon = icon
        self.sync_tray()
        icon.show()
        self.tray_ready = True

    def sync_tray(self):
        if self.menu_items is None:
            return
        external, modules = self.menu_items
        external.setText(self.external_lines[0])
        for name, (submenu, startup, actions) in modules.items():
            submenu.setTitle(self.module_title(name))
            startup.setChecked(self.startup_checked(name))
            startup.setEnabled(self.module_idle(name))
            for action, item in actions.items():
                item.setEnabled(self.module_action_enabled(name, action))

    def refresh_tray(self):
        if self.invoker is not None and self.icon is not None:
            self.invoker.later(self.sync_tray)

    def stop_tray(self):
        if self.invoker is not None and self.icon is not None:
            self.invoker.wait(self.icon.hide)

    # Single instance --------------------------------------------------------------------------

    def claim_single_instance(self):
        """Become the one window for this supervisor, or ask the existing one to show itself."""
        from qtpy.QtNetwork import QLocalServer, QLocalSocket
        from ..shared.filelock import file_lock
        name = single_instance_name(self.base)
        with file_lock(self.data / "single-instance.lock", timeout=10):
            probe = QLocalSocket()
            probe.connectToServer(name)
            if probe.waitForConnected(1000):
                probe.write(b"show\n")
                probe.waitForBytesWritten(1000)
                probe.disconnectFromServer()
                return False
            QLocalServer.removeServer(name)  # a stale socket left by a crashed host
            server = QLocalServer()
            server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
            if not server.listen(name):
                raise RuntimeError(f"Cannot open the single-instance socket {name}: {server.errorString()}")
        server.newConnection.connect(self.on_second_launch)
        self.single_server = server
        return True

    def on_second_launch(self):
        while self.single_server.hasPendingConnections():
            connection = self.single_server.nextPendingConnection()
            connection.disconnected.connect(connection.deleteLater)
            connection.readyRead.connect(lambda connection=connection: (
                connection.readAll(), self.in_worker(self.show)))

    # Entry ------------------------------------------------------------------------------------

    def run(self):
        from qtpy.QtWidgets import QApplication
        import webview
        self.data.mkdir(parents=True, exist_ok=True)
        # Without a platform theme Qt draws its own light file dialogs; the portal theme gives the desktop's
        # native file chooser and colour scheme. Must be set before the QApplication exists.
        os.environ.setdefault("QT_QPA_PLATFORMTHEME", "xdgdesktopportal")
        app = QApplication.instance() or QApplication([APP_ID])
        app.setApplicationName(APP_ID)
        app.setApplicationDisplayName(WINDOW_TITLE)
        app.setDesktopFileName(APP_ID)
        app.setWindowIcon(qt_icon())
        app.setQuitOnLastWindowClosed(False)  # hiding to the tray must not end the host
        if not self.claim_single_instance():
            print("Tools is already open; asked the existing window to show itself.", flush=True)
            return 0
        self.invoker = Invoker()
        self.dialogs = self.dialogs or Dialogs(self.invoker, self.native)
        self.resolve_page()
        install_page_class(self)
        self.create_window()
        self.create_tray()
        for target in (self.connection.heartbeat, self.monitor):
            threading.Thread(target=target, daemon=True).start()
        webview.start(gui="qt", private_mode=False, storage_path=str(self.data / "data"))
        self.connection.stopped.set()
        return 0


def check_runtime():
    try:
        import webview  # noqa: F401
        from qtpy import API_NAME, QtCore
        from qtpy.QtWebEngineWidgets import QWebEngineView  # noqa: F401
    except ImportError as error:
        raise SystemExit(f"Missing Linux desktop dependency: {error}. Run `uv sync --locked`.")
    print(f"pywebview and {API_NAME} {QtCore.qVersion()} with Qt WebEngine available")


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
