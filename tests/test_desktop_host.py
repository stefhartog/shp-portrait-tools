"""Shared desktop-host behaviour (tray model, module controls, quit/restart flow) against a fake platform."""

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from importlib.resources import files
from pathlib import Path
from unittest.mock import Mock

from shp_portrait_tools.desktop.host import DesktopHost, PickerApi

RUNNING = {"phase": "running", "instance": "one", "busy": False, "loaded": True, "revision": 4, "startup_enabled": True}


def status(**skin):
    return {"modules": {"skin": dict(RUNNING, **skin)}, "external": {}, "lifecycle": {"leases": 1}}


class FakeHost(DesktopHost):
    def __init__(self, data, **kwargs):
        super().__init__("http://127.0.0.1:18760/", data, **kwargs)
        self.dialogs = Mock()
        self.dialogs.confirm.return_value = True
        self.window = Mock()
        self.connection = Mock(client="desktop-test", error=None)
        self.connection.lease_lock = Mock(__enter__=Mock(), __exit__=Mock(return_value=False))
        self.spawned, self.alive = [], []

    def own_identity(self):
        return 77, 7700

    def process_alive(self, pid, created):
        return self.alive.pop(0) if self.alive else False

    def spawn_detached(self, command, log):
        self.spawned.append(command)


class DesktopHostTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.host = FakeHost(Path(self.temp.name), launcher_python="pythonw.exe", launcher_config="launcher.json")

    def test_tray_model_follows_status_and_pending_actions(self):
        host = self.host
        self.assertEqual(host.module_title("skin"), "Skin Tools - Disconnected")
        self.assertFalse(host.module_action_enabled("skin", "start"))
        host.update_status(status(), host.status_epoch)
        self.assertEqual(host.module_title("skin"), "Skin Tools - Running")
        self.assertFalse(host.module_action_enabled("skin", "start"))
        self.assertTrue(host.module_action_enabled("skin", "stop"))
        host.update_status(status(busy=True), host.status_epoch)
        self.assertEqual(host.module_title("skin"), "Skin Tools - Busy")
        self.assertFalse(host.module_action_enabled("skin", "restart"))
        host.update_status(status(phase="stopped", startup_enabled=False), host.status_epoch)
        self.assertTrue(host.module_action_enabled("skin", "start"))
        self.assertFalse(host.startup_checked("skin"))
        host.control_pending, host.pending_module = True, ("skin", "restart")
        self.assertEqual(host.module_title("skin"), "Skin Tools - Restarting")
        self.assertFalse(host.module_idle("skin"))
        self.assertEqual([label for label, _ in host.window_actions()][0], "Show Tools")

    def test_stale_status_epoch_is_ignored(self):
        host = self.host
        epoch = host.status_epoch
        host.status_epoch += 1
        host.update_status(status(), epoch)
        self.assertEqual(host.modules, {})

    def test_module_control_retries_only_after_confirmation(self):
        host = self.host
        host.connection.control.side_effect = [
            (False, {"code": "confirmation", "error": "Image loaded", "revision": 9}),
            (True, dict(RUNNING, instance="two"))]
        host.module_worker("skin", "restart", {"instance": "one", "confirm": False, "revision": None})
        second = host.connection.control.call_args_list[1].args
        self.assertEqual(second[2], {"instance": "one", "confirm": True, "revision": 9})
        self.assertEqual(host.modules["skin"]["instance"], "two")
        self.assertFalse(host.control_pending or host.dialog_open)
        host.dialogs.confirm.return_value = False
        host.connection.control.reset_mock(side_effect=True)
        host.connection.control.return_value = (False, {"code": "confirmation", "error": "x", "revision": 1})
        host.module_worker("skin", "stop", {"instance": "two", "confirm": False, "revision": None})
        self.assertEqual(host.connection.control.call_count, 1)

    def test_quit_is_refused_while_busy_and_reload_reloads(self):
        host = self.host
        host.pending_action, host.dialog_open = "quit", True
        host.confirm_quit(status(busy=True))
        host.dialogs.confirm.assert_not_called()
        self.assertFalse(host.dialog_open)
        host.pending_action, host.dialog_open = "reload", True
        host.connection.request.return_value = status()
        host.confirm_quit(status())
        host.window.evaluate_js.assert_called_once_with("location.reload()")
        self.assertFalse(host.dialog_open)

    def test_changed_work_during_confirmation_cancels_quit(self):
        host = self.host
        host.pending_action = "quit"
        host.connection.request.return_value = status(revision=5)
        host.confirm_quit(status())
        self.assertFalse(any(call.args[0] == "/api/ui/native-quit" for call in host.connection.request.call_args_list))

    def test_restart_waits_for_drain_then_launches_once_with_both_identities(self):
        host = self.host
        host.pending_action, host.quit_signature = "restart", {"skin": ["one", 4, True]}
        host.alive = [True, False]
        host.connection.request.side_effect = [
            {"draining": True, "pid": 50, "process_identity": [50, 5000]}, {"shutdown_error": None}]
        host.native_shutdown()
        quit_body = host.connection.request.call_args_list[0].args[1]
        self.assertEqual(quit_body["expected"], {"skin": ["one", 4, True]})
        self.assertTrue(quit_body["restart"] and not quit_body["force"])
        self.assertEqual(host.spawned, [["pythonw.exe", "-m", "shp_portrait_tools.launcher", "--config", "launcher.json",
                                         "--wait-for-exit", "50", "5000", "--wait-for-exit", "77", "7700"]])
        self.assertTrue(host.quitting)
        host.window.destroy.assert_called_once()

    def test_restart_not_admitted_launches_nothing(self):
        host = self.host
        host.pending_action = "restart"
        host.connection.request.return_value = {"draining": False, "others": 1}
        host.native_shutdown()
        self.assertEqual(host.spawned, [])
        self.assertFalse(host.quitting)

    def test_picker_api_exposes_only_pickers_and_blocks_while_a_dialog_is_open(self):
        host = self.host
        picks = {"image": Path("C:/a.png"), "images": [Path("C:/a.png"), Path("C:/b.png")], "folder": Path("C:/dir")}
        seen = []

        class Fake:
            def __init__(self, window):
                seen.append(window)

            def image(self):
                return picks["image"]

            def images(self):
                return picks["images"]

            def folder(self):
                return picks["folder"]

        host.picker_factory = Fake
        api = PickerApi(host)
        self.assertEqual(sorted(name for name in dir(api) if not name.startswith("_")),
                         ["pick_folder", "pick_image", "pick_images"])
        self.assertEqual(api.pick_image(), str(picks["image"]))
        self.assertEqual(api.pick_images(), [str(path) for path in picks["images"]])
        self.assertEqual(api.pick_folder(), str(picks["folder"]))
        self.assertTrue(all(window is host.window for window in seen))
        self.assertFalse(host.dialog_open)
        host.dialog_open = True  # a tray dialog is already open: no second modal
        self.assertIsNone(api.pick_image())
        host.dialog_open = False
        Fake.image = lambda self: None
        self.assertIsNone(api.pick_image())
        Fake.folder = lambda self: (_ for _ in ()).throw(OSError("dialog failed"))
        self.assertIsNone(api.pick_folder())
        self.assertFalse(host.dialog_open)

    @unittest.skipUnless(shutil.which("node"), "Node.js is unavailable")
    def test_page_uses_js_bridge_and_keeps_prompt_fallback(self):
        html = files("shp_portrait_tools.modules.skin.ui").joinpath("index.html").read_text(encoding="utf-8")
        code = re.search(r"function desktopPicker\(\) \{.*?\nlet batchSelection = null;", html, re.S)[0]
        script = """
const assert = require('node:assert/strict');
const calls = [];
let dialogChoice = 'folder', closeHandler = null;
const dialog = {returnValue:'', addEventListener(type, handler){ closeHandler = handler; },
  showModal(){ this.returnValue = dialogChoice; closeHandler(); }};
const $ = id => { assert.equal(id, 'batchSourceDialog'); return dialog; };
const bridge = {pick_image: async () => (calls.push('image'), 'C:/a.png'),
  pick_images: async () => (calls.push('images'), ['C:/a.png', 'C:/b.png']),
  pick_folder: async () => (calls.push('folder'), 'C:/dir')};
let top = {pywebview: {api: bridge}}, prompts = [];
const window = {get top(){ return top; }, prompt: request => (prompts.push(request), request === 'SHP_OPEN_IMAGE_V1' ? '/p.png' : '{"folder":"/x"}')};
""" + code + """
(async () => {
  assert.equal(await chooseImage(), 'C:/a.png');
  assert.deepEqual(await chooseBatchSource(), {folder:'C:/dir'});
  dialogChoice = 'images'; assert.deepEqual(await chooseBatchSource(), {paths:['C:/a.png','C:/b.png']});
  dialogChoice = 'cancel'; assert.equal(await chooseBatchSource(), null);
  assert.deepEqual(calls, ['image', 'folder', 'images']);
  assert.deepEqual(prompts, []);
  top = {};  // a host without the bridge (Linux GTK) answers the old prompts
  assert.equal(await chooseImage(), '/p.png');
  assert.deepEqual(await chooseBatchSource(), {folder:'/x'});
  assert.deepEqual(prompts, ['SHP_OPEN_IMAGE_V1', 'SHP_OPEN_BATCH_SOURCE_V1']);
  top = {get pywebview(){ throw new Error('cross-origin'); }};
  assert.equal(desktopPicker(), null);
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
        result = subprocess.run([shutil.which("node"), "-"], input=script, text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_page_address_trusted_origins_and_following_the_worker(self):
        host = self.host
        host.connection.request.return_value = {"ports": {"skin": 18765}}
        self.assertEqual(host.resolve_page(), "http://127.0.0.1:18765")
        self.assertTrue(host.trusted_origin("http://127.0.0.1:18760"))
        self.assertTrue(host.trusted_origin("http://127.0.0.1:18765"))
        self.assertFalse(host.trusted_origin("http://127.0.0.1:9999"))
        host.update_status(status(instance="one"), host.status_epoch)
        host.window.load_url.assert_not_called()  # the status page moves on to the page by itself
        host.update_status(status(instance="one"), host.status_epoch)
        host.window.load_url.assert_not_called()
        host.update_status(status(instance="two"), host.status_epoch)  # restarted: back to the status page
        host.window.load_url.assert_called_once_with("http://127.0.0.1:18760/")
        host.update_status(status(phase="stopped", instance="two"), host.status_epoch)
        self.assertEqual(host.window.load_url.call_count, 2)
        host.update_status(status(phase="stopped", instance="two"), host.status_epoch)
        self.assertEqual(host.window.load_url.call_count, 2)
        host.connection.request.side_effect = OSError("down")
        host.message = Mock()
        self.assertIsNone(host.resolve_page(attempts=2, delay=0))
        host.message.assert_called_once()


if __name__ == "__main__":
    unittest.main()
