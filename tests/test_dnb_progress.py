"""Focused non-GUI progress tests; no PhotoCraft process or model inference."""

import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
import json
import ctypes
import ctypes.util
import re
from importlib.resources import files

import numpy as np

import test_dnb as T
from test_photocraft import FakePhotoCraft
from shp_photocraft_tools.modules.dnb import progress
from shp_photocraft_tools.modules.dnb.photocraft_bridge import Bridge
from shp_photocraft_tools.shared.ui import stylesheet, theme_script


class ProgressTests(unittest.TestCase):
    setUp = T.DnbTests.setUp
    close_server = T.DnbTests.close_server
    get = T.DnbTests.get
    post = T.DnbTests.post

    def test_feedback_summary_log_and_inline_syntax_without_browser(self):
        library = ctypes.util.find_library("javascriptcoregtk-4.1") or ctypes.util.find_library("javascriptcoregtk-4.0")
        if not library:
            self.skipTest("JavaScriptCore library unavailable")
        js = ctypes.CDLL(library)
        js.JSGlobalContextCreate.argtypes = [ctypes.c_void_p]
        js.JSGlobalContextCreate.restype = ctypes.c_void_p
        js.JSStringCreateWithUTF8CString.argtypes = [ctypes.c_char_p]
        js.JSStringCreateWithUTF8CString.restype = ctypes.c_void_p
        js.JSCheckScriptSyntax.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                                         ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)]
        js.JSCheckScriptSyntax.restype = ctypes.c_bool
        js.JSEvaluateScript.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                                       ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)]
        js.JSEvaluateScript.restype = ctypes.c_void_p
        js.JSStringRelease.argtypes = [ctypes.c_void_p]
        js.JSGlobalContextRelease.argtypes = [ctypes.c_void_p]
        html = T.S.HTML.read_text()
        self.assertNotIn('id="loadTimings"', html.split('id="g-file"')[1].split('</details>')[0])
        self.assertNotIn('id="loadTimings"', html)
        self.assertNotIn('id="sendTimings"', html)
        analysis = html.split('id="g-file"')[1].split('<div id="manual"')[0]
        self.assertIn('<details id="temporaryLog">', analysis)
        self.assertIn('<summary>Log</summary>', analysis)
        self.assertNotIn('<button id="temporaryLog"', html)
        self.assertNotIn('downloadTemporaryLog', html)
        self.assertNotIn('skintoneSummary', html)
        self.assertIn("if (state.skintoneError) logNote(state.skintoneError);", html)
        self.assertIn("if (data?.effect) logNote(data.effect.note);", html)
        self.assertIn("if (data && !populated) logNote(", html)
        self.assertNotIn('data-k="blur"', html)
        right_tabs = html.split("function rightPreviewKinds(mode) {", 1)[1].split("function paneKinds()", 1)[0]
        preview_harness = """
          (() => {
            const state={mode:'dnb'},tabSel={A:'result',B:'mask'};
            const tabs=['mask','analysis','layer','layerm','color','detail'].map(k=>({dataset:{k},hidden:false}));
            const left=['orig','result'].map(k=>({dataset:{k},hidden:false}));
            const $=id=>({querySelectorAll:()=>id==='tabsA'?left:tabs});
        """ + "function rightPreviewKinds(mode) {" + right_tabs + """
            for(const [mode,expected] of [['frequency','color,detail'],['dnb','layer,layerm'],['mask','mask,analysis']]) {
              for(const previous of ['mask','analysis','layer','layerm','color','detail','blur',null]) {
                state.mode=mode;tabSel.B=previous;syncRightTabs();
                if(tabs.filter(t=>!t.hidden).map(t=>t.dataset.k).join(',')!==expected)throw Error('Visible previews');
                const allowed=expected.split(',');
                if(tabSel.B!==(allowed.includes(previous)?previous:allowed[0]))throw Error('Invalid fallback');
              }
              for(const previous of ['orig','result']) {
                state.mode=mode;tabSel.A=previous;syncLeftTabs();
                if(left.filter(t=>!t.hidden).map(t=>t.dataset.k).join(',')!==(mode==='mask'?'orig':'orig,result'))throw Error('Left previews');
                if(tabSel.A!==(mode==='mask'?'orig':previous))throw Error('Left fallback');
              }
            }
          })();
        """
        self.assertIn("$('g-file').hidden = state.mode !== 'mask'", html)
        self.assertIn("const processingStarted = loadingPreview ? progress.started : started;", html)
        self.assertIn("else finishProcessing(processingStarted);", html)
        self.assertIn("logNote(`Sent to ${r.document}.`);\n    finishProcessing(started);", html)
        helpers = html.split("const temporaryLog =")[1].split("function slider(")[0]
        harness = """
          const elements = {};
          const $ = id => elements[id] ||= {textContent:'',value:'',checked:true};
          let statusOk = false;
          const performance = {now:()=>3456};
          const state = {busy:false, estS:181, imageInfo:{name:'<portrait>.png',
            width:800,height:600,colour:'native ICC',confidence:'high',desc:'iris cue',
            has_mask:true,mask_pct:73}, skintone:{hue_center:25.8},tone:{mid:.52},
            color:{hue:30,saturation:.6},hueCenterNote:'Estimated from original Color'};
          $('S').value='181';
        """ + "const temporaryLog =" + helpers + """
          function check(ok){if(!ok)throw Error('Feedback regression');}
          renderAnalysisSummary();
          check($('info').textContent.includes('<portrait>.png'));
          check($('info').textContent.includes('25.8°'));
          check($('info').textContent.includes('52.0%'));
          check($('info').textContent.includes('(estimated)'));
          $('S').value='190.5';state.skintone.hue_center=32;state.color.hue=null;
          renderAnalysisSummary();
          check($('info').textContent.includes('manual/landmark override'));
          check($('info').textContent.includes('32.0°'));
          check($('info').textContent.includes('hue unavailable'));
          finishProcessing(1000);
          check($('status').textContent==='Finished processing in 2.5 seconds.');
          check($('status').className==='ok');
          setStatus('Actual failure',true);check($('status').className==='err');
          check(temporaryLogText().includes('Actual failure'));
          check($('temporaryLogText').textContent.includes('Actual failure'));
          for(let i=0;i<120;i++)logNote('entry '+i);
          check(temporaryLog.notes.length===100);
          temporaryLog.load='Image loaded in 1.20s';
          temporaryLog.send='Send completed in 2.30s';
          check(temporaryLogText().includes('Image loaded in 1.20s'));
          check(temporaryLogText().includes('Send completed in 2.30s'));
          renderTemporaryLog();
          check($('temporaryLogText').textContent===temporaryLogText());
          check($('status').textContent==='Actual failure');
        """
        theme = theme_script().decode()
        theme_harness = """
        (() => {
          function check(ok){if(!ok)throw Error('Theme regression');}
          function page(saved, embedded=false) {
            const handlers={}, dom={}, stored=new Map(), frames=[], sent=[], inserted=[];
            if(saved)stored.set('shp-tools-theme',saved);
            const localStorage={getItem:k=>stored.get(k)||null,setItem:(k,v)=>stored.set(k,v)};
            const location={origin:'http://127.0.0.1:9999'};
            const window={addEventListener:(k,fn)=>handlers[k]=fn};
            const parent=embedded ? {postMessage:(data,origin)=>sent.push({data,origin})} : window;
            const host={querySelector:()=>null,insertBefore:label=>inserted.push(label)};
            const document={
              documentElement:{dataset:{}}, querySelectorAll:()=>frames,
              querySelector:()=>host, getElementById:()=>host,
              addEventListener:(k,fn)=>dom[k]=fn,
              createElement:()=>({children:[],append(child){this.children.push(child);},setAttribute(){}})
            };
        """ + theme + """
            dom.DOMContentLoaded();
            return {handlers,stored,document,frames,sent,inserted,parent,location};
          }
          const shell=page(null);
          check(shell.document.documentElement.dataset.theme==='photocraft');
          check(page('current').document.documentElement.dataset.theme==='current');
          const picker=shell.inserted[0].children[0];
          check(picker.children.map(o=>o.value).join(',')==='current,photocraft');
          const received=[];
          const frame={contentWindow:{postMessage:(data,origin)=>received.push({data,origin})}};
          shell.frames.push(frame);
          picker.value='photocraft';picker.onchange();
          check(shell.stored.get('shp-tools-theme')==='photocraft');
          check(received.at(-1).data.theme==='photocraft');
          check(received.at(-1).origin===shell.location.origin);
          check(page(shell.stored.get('shp-tools-theme')).document.documentElement.dataset.theme==='photocraft');
          received.length=0;
          shell.handlers.message({origin:shell.location.origin,source:{},data:{type:'shp-tools-theme-ready'}});
          check(received.length===0);
          shell.handlers.message({origin:'http://other',source:frame.contentWindow,data:{type:'shp-tools-theme-ready'}});
          check(received.length===0);
          shell.handlers.message({origin:shell.location.origin,source:frame.contentWindow,data:{type:'shp-tools-theme-ready'}});
          check(received.length===1 && received[0].data.theme==='photocraft');
          const child=page('current',true);
          check(child.inserted.length===0 && child.sent[0].data.type==='shp-tools-theme-ready');
          child.handlers.message({origin:child.location.origin,source:child.parent,data:received[0].data});
          check(child.document.documentElement.dataset.theme==='photocraft');
          child.handlers.message({origin:child.location.origin,source:child.parent,data:{type:'shp-tools-theme',theme:'invalid'}});
          check(child.document.documentElement.dataset.theme==='photocraft');
          shell.handlers.storage({key:'shp-tools-theme',newValue:'current'});
          check(picker.value==='current' && received.at(-1).data.theme==='current');
          shell.handlers.storage({key:null,newValue:null});
          check(shell.document.documentElement.dataset.theme==='photocraft');
        })();
        """
        for package in ("shell", "modules.crop.ui", "modules.dnb.ui"):
            page = files("shp_photocraft_tools."+package).joinpath("index.html").read_text()
            self.assertIn('<script src="static/theme.js"></script>', page)
        css = stylesheet().decode()
        self.assertIn("button.primary, button.workflow:not(:disabled)", css)
        self.assertIn("button.workflow:not(:disabled):hover", css)
        self.assertIn("button.workflow:not(:disabled):active", css)
        self.assertIn("button:where(:not(:disabled)):hover", css)
        self.assertIn(".tab:where(:not(:disabled)):hover", css)
        self.assertNotIn("button:hover", css)
        self.assertNotIn(".tab:hover", css)
        self.assertNotIn("button.workflow:disabled", css)
        for package, expected in (
            ("modules.crop.ui", {"xGet", "xSend"}),
            ("modules.dnb.ui", {"pcGet", "pcSend", "loadBtn", "save"}),
        ):
            page = files("shp_photocraft_tools."+package).joinpath("index.html").read_text()
            workflow = set()
            for tag in re.findall(r"<button\b[^>]*>", page):
                button_id = re.search(r'\bid="([^"]+)"', tag)
                classes = re.search(r'\bclass="([^"]+)"', tag)
                if classes and "workflow" in classes[1].split():
                    workflow.add(button_id[1])
                if button_id and button_id[1].startswith("preset"):
                    self.assertIsNone(classes, "Preset buttons must retain their styling")
            self.assertEqual(workflow, expected)
        self.assertIn("border-bottom-color:var(--workspace)", css)
        self.assertIn(".tab { margin-bottom:-1px;", css)
        self.assertIn("flex-wrap:wrap-reverse;", css)
        self.assertNotIn("box-shadow:0 1px 0 var(--workspace)", css)
        for page_package in ("modules.crop.ui", "modules.dnb.ui"):
            self.assertIn("border-top:0;", files("shp_photocraft_tools."+page_package).joinpath("index.html").read_text())
        self.assertNotIn('.tab.on { border-color:var(--acc)', css)
        self.assertIn(":focus-visible", css)
        shell_css = files("shp_photocraft_tools.shell").joinpath("shell.css").read_text()
        self.assertNotIn(".badge", shell_css)
        self.assertIn('[role="tab"][aria-busy="true"] { color:color-mix', shell_css)
        self.assertNotIn('[aria-busy="true"] { background:', shell_css)
        self.assertIn("--status-symbol-size:11px", css)
        self.assertIn("width:var(--status-symbol-size); height:var(--status-symbol-size);", css)
        self.assertIn("--status-accent:#6a9fc5", css)
        self.assertIn("#status.ok::after", css)
        shell_js = files("shp_photocraft_tools.shell").joinpath("shell.js").read_text()
        badge_helpers = shell_js.split("(() => {", 1)[1].split("  const names =", 1)[0]
        badge_harness = badge_helpers + """
          (() => {
            function check(ok){if(!ok)throw Error('Badge regression');}
            const tab={textContent:'Crop',setAttribute(k,v){this[k]=v;}};
            for(const [phase,busy,expected,working] of [
              ['running',false,'running',false],['running',true,'busy',true],
              ['starting',false,'starting',true],['stopping',false,'stopping',true],
              ['stopped',false,'stopped',false],['error',false,'error',false],
              ['unavailable',false,'unavailable',false],['unexpected',false,'unknown',false],
              [undefined,false,'unknown',false]]) {
              renderTabStatus(tab,{phase,busy});
              check(moduleStatus({phase,busy}).status===expected);
              check(tab['aria-busy']===String(working));
              check(tab.textContent==='Crop');
              check(tab['aria-label']==='Crop: '+tab.title && tab.title.length>0);
            }
            renderTabStatus(tab,{phase:'error',error:'Connection lost'});
            check(tab.title==='Error: Connection lost');
          })();
        """
        shell_page = files("shp_photocraft_tools.shell").joinpath("index.html").read_text()
        self.assertEqual(re.findall(r'data-view="([^"]+)"', shell_page), ['mask', 'frequency', 'dnb', 'crop'])
        self.assertIn("const views = ['mask', 'frequency', 'dnb', 'crop'];", shell_js)
        self.assertIn("const modes = views.filter(view => view !== 'crop');", shell_js)
        self.assertNotIn('class="badge"', shell_page)
        self.assertEqual(shell_page.count('title="Starting" data-service='), 4)
        self.assertIn('[role="tab"]:where(:not(:disabled)):hover', shell_css)
        self.assertNotIn('[role="tab"]:hover', shell_css)
        self.assertNotIn("border-color:var(--acc)", shell_css)
        self.assertIn("border-bottom-color:var(--panel)", shell_css)
        for colour in ("#272727", "#515151", "#444444", "#626262", "#e0e0e0", "#b8b8b8", "#6a9fc5"):
            self.assertIn(colour, css)
        self.assertNotIn("filter:", css)
        from shp_photocraft_tools.modules.crop.server import Handler as CropHandler
        from shp_photocraft_tools.supervisor import ShellHandler
        for handler_type in (T.S.Handler, CropHandler, ShellHandler):
            handler = object.__new__(handler_type)
            handler.path = "/static/theme.js"
            handler.valid_host = lambda: True
            with patch.object(handler, "send") as send:
                handler.do_GET()
                send.assert_called_once_with(200, theme_script(), "text/javascript; charset=utf-8")
        context = js.JSGlobalContextCreate(None)
        try:
            for script in re.findall(r"<script[^>]*>(.*?)</script>", html, re.S) + [harness, theme_harness, shell_js, badge_harness, preview_harness]:
                source = js.JSStringCreateWithUTF8CString(script.encode())
                error = ctypes.c_void_p()
                try:
                    self.assertTrue(js.JSCheckScriptSyntax(context, source, None, 1, ctypes.byref(error)))
                    if script in (harness, theme_harness, badge_harness, preview_harness):
                        js.JSEvaluateScript(context, source, None, None, 1, ctypes.byref(error))
                        self.assertFalse(error.value, "Headless feedback logic failed")
                finally:
                    js.JSStringRelease(source)
        finally:
            js.JSGlobalContextRelease(context)

    def test_live_snapshot_is_readable_while_processing_lock_is_held(self):
        entered, release = threading.Event(), threading.Event()
        token = "load-progress-fixture-123"
        result = []

        def detect(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise TimeoutError("test did not release detector")
            return None

        with patch.object(T.S.M, "pick_target", side_effect=detect):
            thread = threading.Thread(target=lambda: result.append(self.post(
                "/api/load", {"path": str(self.image), "_progress": token})))
            thread.start()
            try:
                self.assertTrue(entered.wait(5))
                snapshot = json.loads(self.get("/api/progress?token=" + token)[0])
                self.assertEqual(snapshot["active"], "Analyzing image / estimating S")
                self.assertEqual(snapshot["steps"][0]["label"], "Loading image / preparing profile")
                self.assertFalse(snapshot["done"])
                self.assertTrue(T.S.LOCK.locked())
                self.assertTrue(json.loads(self.get("/api/progress?token=other-operation")[0])["pending"])
            finally:
                release.set()
                thread.join(5)
        final = result[0]["_progress"]
        self.assertTrue(final["done"])
        self.assertIsNone(final["error"])
        self.assertTrue(any("Mask not generated" in step["label"] for step in final["steps"]))
        self.assertTrue(progress.snapshot(token)["pending"], "Final response should consume the record")
        for step in final["steps"]:
            self.assertGreaterEqual(step["seconds"], 0)

    def test_send_stages_and_failure_cleanup_preserve_final_error(self):
        bridge = Bridge(port=1, token_file=self.root / "unused",
                        exchange=self.root / "exchange", read_root=self.root)
        bridge.client = FakePhotoCraft(bridge, fail_copy=True)
        token = "send-progress-fixture-123"
        with self.assertRaisesRegex(T.S.PCB.ControlError, "copy failed"):
            with progress.operation(token):
                bridge.send(np.full((48, 64, 3), 32768, np.uint16),
                            np.ones((48, 64), np.uint8), bridge.client.documents[0],
                            source_icc=bridge.client.icc)
        snapshot = progress.snapshot(token, consume=True)
        self.assertTrue(snapshot["done"])
        self.assertIn("copy failed", snapshot["error"])
        labels = [step["label"] for step in snapshot["steps"]]
        self.assertTrue(any(label.startswith("Encoding dnb layer") for label in labels))
        self.assertIn("Attaching Soft Light mask", labels)
        self.assertIn("Inserting final D&B layer", labels)
        self.assertEqual(labels[-1], "Cleaning up scratch documents")
        self.assertTrue(any(step["error"] == "copy failed" for step in snapshot["steps"]))
        self.assertEqual(len(bridge.client.documents), 1)
        self.assertEqual(list(bridge.exchange.iterdir()), [])
        with patch.object(T.S.M, "pick_target", side_effect=ValueError("analysis failed")):
            with self.assertRaises(HTTPError) as error:
                self.post("/api/load", {"path": str(self.image), "_progress": token})
        failed = json.load(error.exception)
        self.assertIn("analysis failed", failed["_progress"]["error"])
        self.assertTrue(failed["_progress"]["done"])
        self.assertTrue(progress.snapshot(token)["pending"])
