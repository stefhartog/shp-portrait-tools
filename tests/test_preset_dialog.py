"""Exercise the preset form handlers against disposable module servers."""

from contextlib import ExitStack
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from shp_photocraft_tools.modules.dnb import ui_server as skin
from test_dnb import PARAMS


class PresetDialogTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "win32", "Windows only")
    def test_windows_host_cancels_the_old_preset_prompt(self):
        from shp_photocraft_tools.desktop.windows_controller import Host

        # A non-allowlisted WebView2 prompt is cancelled when the event returns unaccepted.
        def unexpected():
            self.fail("ordinary prompts must not enter the file picker or be accepted")

        host = SimpleNamespace(base="http://127.0.0.1:18760")
        args = SimpleNamespace(Message="Name for the new preset:", Kind=2,
                               Uri=host.base + "/dnb/", Accept=unexpected, GetDeferral=unexpected)
        Host.on_script_dialog(host, None, args)

    @unittest.skipUnless(shutil.which("node"), "Node.js is unavailable")
    def test_save_new_cancel_validation_confirmation_persistence_and_error(self):
        with tempfile.TemporaryDirectory() as folder, ExitStack() as stack:
            root = Path(folder)
            for module, files in (
                (skin, {"PRESETS": root / "skin.json", "GLOBAL_SETTINGS": root / "global.json"}),
            ):
                for key, path in files.items():
                    stack.enter_context(patch.object(module, key, path))
            for module, mode, path in (
                (skin, "skin", skin.PRESETS),
            ):
                with self.subTest(mode=mode), ExitStack() as server_stack:
                    server = server_stack.enter_context(ThreadingHTTPServer(("127.0.0.1", 0), module.Handler))
                    thread = threading.Thread(target=server.serve_forever)
                    thread.start()
                    server_stack.callback(thread.join)
                    server_stack.callback(server.shutdown)
                    if module is skin:
                        module.api_preset_save({"name": "Existing", "settings": {**PARAMS, "radius": 17}})
                        module.api_preset_save({"name": "Keep", "settings": PARAMS})
                    else:
                        module.api_settings({"mode": mode, "values": {"prompt": "original"}})
                        module.api_preset_save({"mode": mode, "name": "Existing"})
                        module.api_preset_save({"mode": mode, "name": "Keep"})
                        module.api_settings({"mode": mode, "values": {"prompt": mode}})
                    original = json.loads(path.read_text())["presets"]
                    html = module.HTML.read_text(encoding="utf-8")
                    form = re.search(r'<dialog id="presetNameDialog".*?</dialog>', html, re.S)[0]
                    self.assertIn('<form method="dialog">', form)
                    self.assertIn('value="cancel" formnovalidate', form)
                    source = re.search(r"async function api\(path, body\) \{.*?^\}", html, re.S | re.M)[0]
                    name = "storePreset" if module is skin else "presetCall"
                    source += "\n" + re.search(rf"async function {name}\(.*?^\}}", html, re.S | re.M)[0]
                    source += "\n$('presetNew').onclick" + html.split(
                        "$('presetNew').onclick", 1)[1].split("$('presetRemove').onclick", 1)[0]
                    config = json.dumps({"mode": mode, "base": f"http://127.0.0.1:{server.server_port}/",
                                         "settings": PARAMS, "existing": original})
                    result = subprocess.run([shutil.which("node"), "-"], input=(
                        f"const config = {config};\n" + SETUP + source + ASSERTIONS
                    ), text=True, encoding="utf-8", capture_output=True, timeout=30)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    stored = json.loads(path.read_text())
                    self.assertEqual(stored["presets"]["Keep"], original["Keep"])
                    if module is skin:
                        self.assertEqual(stored["presets"]["New portrait"], PARAMS)
                    else:
                        self.assertEqual(stored["presets"]["New portrait"]["prompt"], mode)
                        self.assertEqual(stored["last"], "Existing")


SETUP = r"""
const assert = require('node:assert/strict');
let calls = 0, confirmations = 0, accept = false, fail = false, selection;
const errors = [], statuses = [];
const realFetch = globalThis.fetch;
globalThis.fetch = (path, args) => {
  calls++;
  return fail ? Promise.resolve(new Response('{"error":"Preset store unavailable"}', {status:500}))
    : realFetch(new URL(path, config.base), args);
};
const prompt = () => null; // Windows cancels the legacy script prompt.
const confirm = () => { confirmations++; return accept; };
const input = {value:'', validity:'', setCustomValidity(message){this.validity=message;},
  reportValidity(){assert.ok(this.validity);}};
const form = {};
const dialog = {open:false, showModal(){this.open=true;}, close(){this.open=false;},
  querySelector(selector){assert.equal(selector,'form');return form;}};
const controls = {presetNew:{},presetNameInput:input,presetNameDialog:dialog};
const $ = key => controls[key];
const presets = {presets:config.existing};
const X = {mode:config.mode,presetsReady:true};
const busy = false, getSettings = () => config.settings;
const fillPresetList = value => {selection=value || 'p:'+presets.last;};
const snapNow = () => {}, presetSnapNow = () => {}, xButtons = () => {}, xInfo = () => {};
const loadStatus = async () => { statuses.push(X.mode); };
const setStatus = (message, error) => {if(error)errors.push(message);};
const presetErr = error => setStatus(error.message, true);
"""

ASSERTIONS = r"""
const open = () => {$('presetNew').onclick();assert.ok(dialog.open);assert.equal(input.value,'');};
const type = value => {input.value=value;input.oninput({target:input});};
const submit = async (value='save') => {
  let prevented = false;
  await form.onsubmit({submitter:{value},preventDefault(){prevented=true;}});
  if (!prevented) dialog.close(); // model the default dialog-form cancel action
};
(async () => {
  open();type('would not be saved');await submit('cancel');
  assert.equal(dialog.open,false);assert.equal(calls,0);
  open();type('  ');await submit();assert.match(input.validity,/1-60/);
  type('x'.repeat(61));await submit();assert.match(input.validity,/1-60/);
  assert.equal(dialog.open,true);assert.equal(calls,0);
  type(' Existing ');await submit();
  assert.equal(confirmations,1);assert.equal(calls,0);assert.ok(dialog.open);
  type(' New portrait ');await submit();
  assert.equal(selection,'p:New portrait');assert.equal(calls,1);assert.equal(dialog.open,false);
  assert.ok(Object.hasOwn(presets.presets,'New portrait'));
  if(config.mode!=='skin')assert.deepEqual(statuses,[config.mode]);
  open();type('Existing');accept=true;await submit();
  assert.equal(confirmations,2);assert.equal(calls,2);assert.equal(selection,'p:Existing');
  open();type('Broken');fail=true;await submit();
  assert.deepEqual(errors,['Preset store unavailable']);assert.ok(!Object.hasOwn(presets.presets,'Broken'));
})().catch(error => {console.error(error);process.exitCode=1;});
"""
