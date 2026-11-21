"""The Skin Tools page's own tab bar and Settings & Info view (formerly in the Tools shell), run in Node."""

import re
import shutil
import subprocess
import unittest
from importlib.resources import files

HTML = files("shp_portrait_tools.modules.skin.ui").joinpath("index.html").read_text(encoding="utf-8")


@unittest.skipUnless(shutil.which("node"), "Node.js is unavailable")
class SkinTabsTests(unittest.TestCase):
    def test_tabs_views_cache_indicators_lock_and_keyboard(self):
        ui_mode = "function uiMode(mode) {" + HTML.split("function uiMode(mode) {", 1)[1].split("const state = {", 1)[0]
        tabs = "const cacheTabs =" + HTML.split("const cacheTabs =", 1)[1].split("function resetCacheTabs()", 1)[0]
        script = r"""
const assert = require('node:assert/strict');
const views = ['mask','frequency','dnb','combine','settings'];
function node(id){ return {id, hidden:false, attrs:{}, dataset:{}, disabled:false, tabIndex:-1, focused:false,
  textContent:id, setAttribute(k,v){ this.attrs[k]=v; }, focus(){ this.focused=true; },
  querySelector(selector){ return this.indicator || null; }}; }
const nodes = {app: node('app'), settingsView: node('settingsView')};
for (const view of views) {
  const tab = node('tab-'+view); tab.dataset.view = view; tab.textContent = view;
  if (view !== 'settings') tab.indicator = {dataset:{state:'idle'}};
  nodes['tab-'+view] = tab;
}
const $ = id => nodes[id];
const document = {querySelectorAll: selector => { assert.equal(selector, '#viewTabs [role="tab"]');
  return views.map(view => nodes['tab-'+view]); }};
const state = {mode:'mask', busy:false};
let settingsOpen = false;
const switched = [];
function switchMode(mode) { switched.push(mode); state.mode = uiMode(mode); renderTabs(); }
""" + ui_mode + tabs + r"""
const tab = view => nodes['tab-'+view];
renderTabs();
assert.equal(tab('mask').attrs['aria-selected'], 'true');
assert.equal(tab('mask').tabIndex, 0);
assert.equal(nodes.settingsView.hidden, true);
// Clicking a mode switches the page mode; Settings & Info swaps the workspace for the settings view.
tab('dnb').onclick();
assert.deepEqual(switched, ['dnb']);
assert.equal(tab('dnb').attrs['aria-selected'], 'true');
tab('settings').onclick();
assert.equal(nodes.app.hidden, true);
assert.equal(nodes.settingsView.hidden, false);
assert.equal(tab('settings').attrs['aria-selected'], 'true');
assert.equal(state.mode, 'dnb', 'opening settings keeps the current mode');
assert.deepEqual(switched, ['dnb']);
tab('combine').onclick();
assert.equal(nodes.app.hidden, false);
assert.equal(nodes.settingsView.hidden, true);
// Cache indicators follow the page's cache state.
cacheTabs.frequency = {state:'ready', label:'FS ready'};
cacheTabs.dnb = {state:'unavailable', label:'Not enough RAM'};
announceCacheTabs();
assert.equal(tab('frequency').indicator.dataset.state, 'ready');
assert.equal(tab('frequency').title, 'FS ready');
assert.equal(tab('frequency').attrs['aria-label'], 'frequency: FS ready');
assert.equal(tab('dnb').indicator.dataset.state, 'unavailable');
assert.equal(tab('mask').indicator.dataset.state, 'idle');
// Busy work locks every tab, including keyboard navigation.
state.busy = true; renderTabs();
assert.ok(views.every(view => tab(view).disabled && tab(view).attrs['aria-disabled'] === 'true'));
tab('mask').onclick();
let prevented = 0;
tab('combine').onkeydown({key:'ArrowRight', preventDefault(){ prevented++; }});
assert.equal(prevented, 1);
assert.equal(state.mode, 'combine');
state.busy = false; renderTabs();
assert.ok(views.every(view => !tab(view).disabled));
// Arrow keys wrap through the five views, including Settings & Info.
tab('combine').onkeydown({key:'ArrowRight', preventDefault(){}});
assert.equal(nodes.settingsView.hidden, false);
assert.ok(tab('settings').focused);
tab('settings').onkeydown({key:'ArrowRight', preventDefault(){}});
assert.equal(state.mode, 'mask');
tab('mask').onkeydown({key:'ArrowLeft', preventDefault(){}});
assert.equal(nodes.settingsView.hidden, false);
"""
        result = subprocess.run([shutil.which("node"), "-"], input=script, text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_connection_decides_send_buttons_and_batch_panel(self):
        code = "function syncTransferButtons() {" + HTML.split("function syncTransferButtons() {", 1)[1].split(
            "[['A', $('tabsA')]", 1)[0]
        script = r"""
const assert = require('node:assert/strict');
const nodes = {}; const $ = id => nodes[id] ??= {id, hidden:false, disabled:false, title:'', value:''};
const state = {busy:false, loaded:true, sourceKind:'local', mode:'combine'};
""" + code + r"""
const visible = () => ['pcSend','pcSendLayers','g-batch'].filter(id => !$(id).hidden);
$('editorSelect').value = 'standalone'; syncTransferTitles();
assert.deepEqual(visible(), ['pcSend','g-batch']);
assert.equal($('pcSend').disabled, false);
state.mode = 'dnb'; syncTransferTitles();
assert.deepEqual(visible(), ['pcSend'], 'batch stays a Combine panel');
for (const editor of ['photoshop', 'photocraft']) {
  $('editorSelect').value = editor; state.mode = 'combine'; state.sourceKind = 'local'; syncTransferTitles();
  assert.deepEqual(visible(), ['pcSendLayers']);
  assert.equal($('pcSendLayers').disabled, true, 'a local source cannot be sent to an editor');
  state.sourceKind = editor; state.mode = 'mask'; syncTransferTitles();
  assert.deepEqual(visible(), ['pcSendLayers'], 'the mask tab sends the mask through Send Layers');
  assert.equal($('pcSendLayers').disabled, false);
  assert.match($('pcSendLayers').title, /skin mask layer/);
}
"""
        result = subprocess.run([shutil.which("node"), "-"], input=script, text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('id="pcConnect"', HTML)
        self.assertEqual(re.findall(r'<select id="editorSelect">(.*?)</select>', HTML)[0],
                         '<option value="photoshop">Photoshop</option><option value="photocraft">PhotoCraft</option>'
                         '<option value="standalone">Standalone</option>')

    def test_markup_has_one_tab_per_view_and_settings_live_in_the_page(self):
        self.assertEqual(re.findall(r'role="tab" aria-selected="[^"]+" data-view="([^"]+)"', HTML),
                         ["mask", "frequency", "dnb", "combine", "settings"])
        settings = HTML.split('<section id="settingsView"', 1)[1].split('<div id="app"', 1)[0]
        for control in ("themeSettings", "editorSelect", "photoshopPair", "calculationLogText", "photoshopTransferLogText"):
            self.assertIn(f'id="{control}"', settings)
        self.assertNotIn('id="toolMode"', HTML)
        self.assertNotIn("parent.postMessage", HTML)
        self.assertNotIn("parent.document", HTML)


if __name__ == "__main__":
    unittest.main()
