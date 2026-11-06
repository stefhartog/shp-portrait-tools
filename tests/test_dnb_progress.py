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
        js.JSValueToStringCopy.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        js.JSValueToStringCopy.restype = ctypes.c_void_p
        js.JSStringGetUTF8CString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t]
        js.JSStringRelease.argtypes = [ctypes.c_void_p]
        js.JSGlobalContextRelease.argtypes = [ctypes.c_void_p]
        html = T.S.HTML.read_text()
        self.assertIn("#pcGet, #pcSend, #xGet, #xRun, #xSend { flex:0 0 calc(11ch + 26px); white-space:nowrap; }", stylesheet().decode())
        self.assertIn("#statusWrap { flex:1 1 0; min-width:0;", stylesheet().decode())
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
        self.assertIn("finishProcessing(processingStarted);", html)
        self.assertIn("setStatus('Preview restored from cache.', false, true);", html)
        self.assertIn("if (epoch === state.loadEpoch && revision === settingsRevision)", html)
        self.assertIn("apply({quiet:true})", html)
        self.assertNotIn("state.version = 0; clearPanels(); markDirty()", html)
        self.assertIn("} finally {\n    feedback.finish();", html)
        feedback_helper = "function previewFeedback(quiet) {" + html.split(
            "function previewFeedback(quiet) {", 1)[1].split("function analysisFlags()", 1)[0]
        feedback_harness = """
          (() => {
            const state={busy:false}, listeners=new Map(), timers=new Map(), statuses=[],flips=[];
            let next=0;
            const document={
              addEventListener:(type,fn)=>listeners.set(type,fn),
              removeEventListener:(type,fn)=>{if(listeners.get(type)===fn)listeners.delete(type);}
            };
            const setTimeout=(fn,ms)=>{if(ms!==150)throw Error('Delay');timers.set(++next,fn);return next;};
            const clearTimeout=id=>timers.delete(id);
            const setStatus=text=>statuses.push(text);
            const setBusy=busy=>{state.busy=busy;flips.push(busy);};
            function check(ok){if(!ok)throw Error('Quiet preview feedback');}
        """ + feedback_helper + """
            const fast=previewFeedback(true);
            check(state.busy && !flips.length && !statuses.length);
            let prevented=false,stopped=false;
            listeners.get('click')({preventDefault:()=>prevented=true,stopImmediatePropagation:()=>stopped=true});
            check(prevented&&stopped);
            fast.stage('Loading preview images...');
            check(!statuses.length);
            fast.finish();
            check(!state.busy && !timers.size && !listeners.size && !flips.includes(true));
            const slow=previewFeedback(true);
            [...timers.values()][0]();
            check(flips.at(-1)===true && statuses.at(-1)==='Computing...');
            slow.stage('Loading preview images...');
            check(statuses.at(-1)==='Loading preview images...');
            setStatus('Actual failure');
            slow.finish();
            check(!state.busy && !timers.size && !listeners.size && statuses.at(-1)==='Actual failure');
            const nextOperation=previewFeedback(true);
            nextOperation.finish();
            check(!state.busy && !timers.size && !listeners.size);
          })();
        """
        self.assertIn("logNote(r.saved ? `Saved result: ${r.path}` : `Sent to ${r.document}.`);\n    finishProcessing(started);", html)
        self.assertIn("$('pcSend').textContent = 'Send Result';", html)
        crop = files("shp_photocraft_tools.modules.crop.ui").joinpath("index.html").read_text()
        self.assertNotIn('id="xRun"', crop.split('<aside>')[0])
        crop_panel, ai_panel = crop.split('<section class="crop-section">')[1].split('<details class="crop-section" open>')
        self.assertIn('id="xRun"', crop_panel)
        self.assertIn('id="xInfo"', crop_panel)
        self.assertLess(crop_panel.index('id="xRatios"'), crop_panel.index('id="xInfo"'))
        self.assertLess(crop_panel.index('id="xInfo"'), crop_panel.index('id="xGenerate"'))
        self.assertIn('#xVariations { display:flex; flex:1; min-width:0;', crop)
        self.assertIn('#xVariations button { flex:1; min-width:0;', crop)
        self.assertIn('grid-template-columns:minmax(0, 1fr) auto minmax(0, 1fr)', crop)
        self.assertIn('grid-template-areas:". top ." "left . right" ". bottom ."', crop)
        self.assertNotIn('#xInfo b', crop)
        self.assertNotIn('= cropped', crop)
        self.assertNotIn('id="xInfo"', ai_panel)
        self.assertEqual(crop.count('id="xRun"'), 1)
        self.assertNotIn('<summary>', crop_panel)
        self.assertIn('<summary>AI SETTINGS</summary>', ai_panel)
        self.assertIn('id="xAdv"', ai_panel.split('</details>')[0])
        self.assertIn('id="xPrompt"', ai_panel.split('</details>')[0])
        self.assertIn('.crop-section > summary { display:block; list-style:none; cursor:pointer;', crop)
        self.assertIn('.crop-section > summary::-webkit-details-marker { display:none; }', crop)
        self.assertIn('.crop-section > summary:focus-visible { outline:2px solid var(--acc);', crop)
        self.assertIn('padding-inline:clamp(16px, 10%, 40px)', crop)
        self.assertNotIn('#xInfo:empty', crop)
        self.assertIn('xButtons(); xSyncSize(); xInfo(); xDraw();\nloadList()', crop)
        self.assertIn('". bottom ."; margin-top:16px;', crop)
        self.assertNotIn('id="xVar"', crop)
        self.assertNotIn('fillInfo', crop)
        self.assertIn("root.querySelectorAll('.model-row').forEach(row => root.insertBefore(row, firstSlider));", crop)
        variation_code = "let variations = 1;" + crop.split("let variations = 1;", 1)[1].split("/* view:", 1)[0]
        run_code = "async function xRun() {" + crop.split("async function xRun() {", 1)[1].split("async function xPoll()", 1)[0]
        variation_harness = """
          (() => {
            const buttons=[1,2,3].map(count=>({
              dataset:{count:String(count)},classList:{toggle(k,v){this[k]=v;}},
              setAttribute(k,v){this[k]=v;}
            }));
            const note={},document={querySelectorAll:()=>buttons};
            const $=id=>id==='xVariationNote'?note:{value:'prompt'};
            const stored=new Map([['shpx-var','4']]);
            const localStorage={getItem:k=>stored.get(k),setItem:(k,v)=>stored.set(k,v)};
            const X={running:false,img:true,free:false,fx:0,fy:0,fw:100};let busy=false;
            function check(ok){if(!ok)throw Error('Crop variations');}
        """ + variation_code + """
            check(variations===4 && !note.hidden && note.textContent.includes('4 variations'));
            check(buttons.every(b=>b['aria-pressed']==='false'));
            check(stored.get('shpx-var')==='4');
            buttons[1].onclick();
            check(variations===2 && note.hidden && buttons[1]['aria-pressed']==='true');
            check(buttons.filter(b=>b.classList.on).length===1 && stored.get('shpx-var')==='2');
            X.running=true;buttons[2].onclick();check(variations===2);
            X.running=false;setVariations(3);check(variations===3);
            const xRatio=()=>[4,3], xMargins=()=>[0,0,10,10], fH=()=>100;
            let payload=null;
            const api=(path,body)=>{payload=body;return new Promise(()=>{});};
        """ + run_code + """
            xRun();check(payload.variations===3);
            setVariations(4);xRun();check(payload.variations===4);
          })();
        """
        info_code = "function xInfo() {" + crop.split("function xInfo() {", 1)[1].split("function xButtons()", 1)[0]
        variation_harness += """
          (() => {
            const info={},run={},$=id=>id==='xInfo'?info:run;let busy=false;
            const X={img:true,W:100,H:80,fw:110,fx:-20,fy:5,ready:true,running:false};
            const fH=()=>90,xRatio=()=>[4,3];
            const xMargins=()=>[-X.fx,-X.fy,X.fx+X.fw-X.W,X.fy+fH()-X.H];
            function check(ok){if(!ok)throw Error('Crop info layout');}
        """ + info_code + """
            xInfo();
            check(info.innerHTML.includes('title="Original resolution">100 × 80</span>'));
            check(info.innerHTML.includes('title="New resolution (4:3)">110 × 90</span>'));
            for(const [side,value] of [['top','−5'],['left','+20'],['right','−10'],['bottom','+15']]){
              check(info.innerHTML.includes(`aria-label="${side} shift: ${value} pixels">${value}</span>`));
            }
            check(!info.innerHTML.includes('<b>') && !run.disabled);
            X.fx=0;X.fy=0;X.fw=100;X.H=90;xInfo();
            check(info.innerHTML.includes('shift: 0 pixels">0</span>'));
            check(info.innerHTML.includes('Nothing to generate') && run.disabled);
            X.img=null;X.W=500;X.H=400;X.fw=600;X.fx=-50;X.fy=-20;xInfo();
            check(info.innerHTML.includes('title="Original resolution">0 × 0</span>'));
            check(info.innerHTML.includes('title="New resolution (4:3)">0 × 0</span>'));
            for(const side of ['top','left','right','bottom']){
              check(info.innerHTML.includes(`aria-label="${side} shift: 0 pixels">0</span>`));
            }
            check(!info.innerHTML.includes('Nothing to generate') && run.disabled);
          })();
        """
        transfer_code = "async function pcGet() {" + html.split("async function pcGet() {", 1)[1].split("$('pcGet').onclick", 1)[0]
        variation_harness += """
          (async () => {
            const state={busy:false,loaded:true,sourceKind:'photocraft',imageGeneration:42,mode:'dnb'};
            const original=state.sourceKind, calls=[],statuses=[],notes=[];
            let loadProgress=null,connected=true,selected=null,applied=0,failure=null;
            const window={__shpNativeFileDialog:true,prompt:()=>{calls.push('chooser');return selected;}};
            const performance={now:()=>1},temporaryLog={send:null};
            const syncTransferTitles=()=>{},setBusy=b=>state.busy=b,setStatus=t=>statuses.push(t);
            const beginLoadTimings=t=>loadProgress={finished:false},recordLoadStage=()=>{};
            const applyLoaded=r=>{state.sourceKind=r.source_kind;calls.push('loaded');};
            const initializeHueCenter=async()=>{},apply=()=>applied++;
            const params=()=>({S:181}),logNote=t=>notes.push(t),finishProcessing=()=>{};
            const renderTemporaryLog=()=>{};
            const api=async()=>({connected});
            const operationApi=async(path,payload)=>{
              calls.push(path);
              if(failure)throw Error(failure);
              if(path==='/api/send-result'){
                check(payload.generation===42);
                return state.sourceKind==='local'?{saved:true,path:'/chosen/source_shpdnb_001.png'}:{document:'PC'};
              }
              return {source_kind:path==='/api/load'?'local':'photocraft'};
            };
            function check(ok){if(!ok)throw Error('Local transfer routing: '+calls.join(','));}
        """ + transfer_code + """
            await pcGet();check(!calls.includes('chooser')&&calls.includes('/api/pc/fetch')&&applied===1);
            connected=false;calls.length=0;selected=null;await pcGet();
            check(calls.join(',')==='chooser'&&state.sourceKind===original&&!state.busy&&applied===1);
            selected='/chosen/source.png';calls.length=0;await pcGet();
            check(calls.includes('/api/load')&&state.sourceKind==='local'&&applied===2&&!state.busy);
            calls.length=0;await pcSend();
            check(calls[0]==='/api/send-result'&&notes.includes('Saved result: /chosen/source_shpdnb_001.png'));
            check(statuses.at(-1)==='Saved result: /chosen/source_shpdnb_001.png'&&!state.busy);
            window.__shpNativeFileDialog=false;calls.length=0;await pcGet();
            check(!calls.length&&statuses.at(-1).includes('Tools desktop')&&!state.busy);
            window.__shpNativeFileDialog=true;failure='cannot decode';await pcGet();
            check(state.sourceKind==='local'&&statuses.at(-1)==='cannot decode'&&!state.busy&&applied===2);
            await pcSend();check(statuses.at(-1)==='cannot decode'&&!state.busy);
          })().then(()=>globalThis.localTransferCheck='ok',e=>globalThis.localTransferCheck=e.message);
        """
        crop_get_code = "async function xGet() {" + crop.split("async function xGet() {", 1)[1].split("async function xRun()", 1)[0]
        crop_send_code = "async function xSend() {" + crop.split("async function xSend() {", 1)[1].split("$('xSend').onclick", 1)[0]
        self.assertIn('id="xFreeMemory"', crop.split("<summary>AI SETTINGS</summary>", 1)[1])
        self.assertIn("The next generation reloads models; not all RAM/VRAM can be freed", crop)
        self.assertIn("$('xFreeMemory').disabled = X.running || busy || X.settingsSaving;", crop)
        free_code = "async function xFreeMemory() {" + crop.split("async function xFreeMemory() {", 1)[1].split("$('xFreeMemory').onclick", 1)[0]
        variation_harness += """
          (async () => {
            const X={running:false,settingsSaving:false,hasResult:true,seq:7};
            let busy=false,failure=null,acknowledged=true,release=null;
            const calls=[],statuses=[],xButtons=()=>{},xInfo=()=>{};
            const setBusy=b=>busy=b,setStatus=t=>statuses.push(t);
            const api=async(path,payload)=>{
              calls.push(path);
              if(release)await new Promise(resolve=>{release=resolve;});
              if(failure)throw Error(failure);
              return {requested:acknowledged};
            };
            const check=ok=>{if(!ok)throw Error('Comfy memory UI: '+statuses.join(','));};
        """ + free_code + """
            for(const flag of ['running','settingsSaving']){X[flag]=true;await xFreeMemory();check(!calls.length);X[flag]=false;}
            busy=true;await xFreeMemory();check(!calls.length);busy=false;
            release=true;const pending=xFreeMemory();check(busy&&calls.length===1);
            await xFreeMemory();check(calls.length===1);release();release=null;await pending;
            check(!busy&&statuses.at(-1)==='Memory release requested');
            check(calls[0]==='/api/comfy/free'&&X.seq===7&&X.hasResult);
            failure='Comfy has running or queued work';await xFreeMemory();
            check(!busy&&statuses.at(-1)===failure);
            failure='Comfy is not reachable';await xFreeMemory();check(!busy&&statuses.at(-1)===failure);
            failure=null;acknowledged=false;await xFreeMemory();
            check(!busy&&statuses.at(-1)==='Memory release was not acknowledged.');
          })().then(()=>globalThis.comfyFreeCheck='ok',e=>globalThis.comfyFreeCheck=e.message);
        """
        variation_harness += """
          (async () => {
            const X={running:false,hasResult:true,sourceKind:'photocraft',seq:7};
            let busy=false,connected=false,selected=null,error=null;
            const calls=[],statuses=[],elements={};
            const $=id=>elements[id]||={};
            const window={__shpNativeFileDialog:true,prompt:()=>{calls.push('chooser');return selected;}};
            const setBusy=b=>busy=b,xButtons=()=>{},xInfo=()=>{},setStatus=t=>statuses.push(t);
            const xLoaded=r=>{X.sourceKind=r.source_kind;calls.push('loaded');};
            const api=async(path,payload)=>{
              calls.push(path);
              if(error)throw Error(error);
              if(path==='/api/pc/status')return {connected};
              if(path==='/api/send-result'){check(payload.seq===7);return {saved:true,path:'/chosen/image_shpcrop_001.png'};}
              return {source_kind:path==='/api/load'?'local':'photocraft'};
            };
            function check(ok){if(!ok)throw Error('Crop local routing: '+calls.join(','));}
        """ + crop_get_code + crop_send_code + """
            await xGet();check(X.sourceKind==='photocraft'&&!busy&&!X.transferring&&!calls.includes('loaded'));
            selected='/chosen/image.png';calls.length=0;await xGet();
            check(X.sourceKind==='local'&&calls.includes('/api/load')&&!busy);
            connected=true;calls.length=0;await xSend();
            check(calls.join(',')==='/api/send-result'&&statuses.at(-1).includes('image_shpcrop_001.png')&&!busy);
            calls.length=0;await xGet();check(!calls.includes('chooser')&&calls.includes('/api/fetch'));
            connected=false;window.__shpNativeFileDialog=false;await xGet();
            check(statuses.at(-1).includes('Tools desktop')&&!busy);
            error='bad image';await xGet();check(statuses.at(-1)==='bad image'&&!busy);
            busy=true;calls.length=0;await xGet();await xSend();check(!calls.length);
          })().then(()=>globalThis.cropTransferCheck='ok',e=>globalThis.cropTransferCheck=e.message);
        """
        geometry_code = crop.split("function xRatio()", 1)[1].split("function xSyncSize()", 1)[0]
        variation_harness += """
          (() => {
            const X={img:true,limit:999,land:true,free:false,aspect:1};
            function check(ok){if(!ok)throw Error('Crop expansion cap: '+JSON.stringify(X));}
        """ + "function xRatio()" + geometry_code + """
            for(const [W,H] of [[300,200],[3000,2000],[6000,4000],[200,6000],[6000,200]]){
              X.W=W;X.H=H;
              for(const ratio of [[1,1],[4,3],[3,2],[16,9],[5,4]]){
                X.ratio=ratio;
                for(const land of [true,false]){
                  X.land=land;X.free=false;xResetFrame();
                  for(const requested of [xWmin(),xWmax(),20000]){
                    X.fw=requested;
                    for(const pos of [-5000,0,5000]){
                      X.fx=pos;X.fy=-pos;xClamp();
                      check(xMargins().every(m=>m<=999));
                      check(Math.min(X.fx+X.fw,W)-Math.max(X.fx,0)>=KEEP);
                      check(Math.min(X.fy+fH(),H)-Math.max(X.fy,0)>=KEEP);
                      const [a,b]=xRatio();
                      check(Math.abs(fH()-Math.round(X.fw*b/a))<=SNAP);
                    }
                  }
                }
              }
              X.free=true;X.fw=20000;X.fh=20000;X.fx=-1000;X.fy=1000;xClamp();
              check(xMargins().every(m=>m<=999));
            }
            X.W=3000;X.H=2000;X.free=true;X.fw=3999;X.fh=2000;X.fx=-999;X.fy=0;xClamp();
            check(xMargins()[0]===999);
            X.fx=-1000;xClamp();check(xMargins()[0]===999);
            X.W=6000;X.H=4000;X.fw=4000;X.fh=4999;X.fx=2000;X.fy=-999;xClamp();
            check(xMargins()[0]===-2000 && xMargins()[1]===999);
          })();
        """
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
        self.assertIn("button.primary:not(:disabled), button.workflow:not(:disabled)", css)
        self.assertIn("button.primary, button.workflow { font-weight:600; }", css)
        workflow_colours = css.split("button.primary:not(:disabled), button.workflow:not(:disabled) {", 1)[1].split("}", 1)[0]
        self.assertNotIn("font-weight", workflow_colours)
        for geometry in ("padding:", "border-width:", "font-size:", "width:"):
            self.assertNotIn(geometry, workflow_colours)
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
            if package == "modules.crop.ui":
                self.assertNotIn("#xRun { width:", page)
                self.assertIn("#xRun.cancel { background:#b3363b; border-color:#b3363b; color:#fff; }", page)
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
        render_code = "function render() {" + shell_js.split("function render() {", 1)[1].split("async function poll()", 1)[0]
        poll_code = "async function poll() {" + shell_js.split("async function poll() {", 1)[1].split("  poll();", 1)[0]
        isolation_harness = """
          (async () => {
            const names={crop:'Crop',dnb:'D&B'},frames=new Map(),reports=[],placeholders={crop:{},dnb:{}};
            let modules={},pending=null,initialSelection=true,active='crop';
            const select=name=>{active=name;initialSelection=false;};
            const tabs={crop:{},dnb:{}};
            const document={querySelectorAll:selector=>selector.includes('="crop"')?[tabs.crop]:
              selector.includes('="dnb"')?[tabs.dnb]:Object.values(tabs),
              createElement:()=>({dataset:{},remove(){this.removed=true;}})};
            const panels=Object.fromEntries(Object.keys(names).map(name=>[name,
              {querySelector:()=>placeholders[name],append:frame=>{frame.appended=true;}}]));
            const $=id=>panels[id.replace('panel-','')];
            const renderTabStatus=(tab,state)=>tab.phase=state.phase;
            const report=(message,error=false)=>reports.push([message,error]);
            const setTimeout=()=>{},location={origin:'http://local'};
            const fetch=async()=>({ok:true,json:async()=>({modules})});
            const skin={dataset:{instance:'skin'},remove(){this.removed=true;}};
            const crop={dataset:{instance:'crop'},remove(){this.removed=true;}};
            frames.set('crop',crop);frames.set('dnb',skin);
            const check=ok=>{if(!ok)throw Error('Shell module isolation');};
        """ + render_code + poll_code + """
            modules={crop:{phase:'stopping',instance:'crop'},dnb:{phase:'running',instance:'skin'}};
            await poll();check(crop.removed&&!frames.has('crop')&&frames.get('dnb')===skin&&!skin.removed);
            modules.crop={phase:'stopped',instance:'crop'};await poll();
            check(placeholders.crop.textContent.includes('unloaded')&&!placeholders.crop.hidden);
            check(placeholders.dnb.hidden&&tabs.dnb.phase==='running'&&reports.at(-1)[0]==='');
            modules.crop={phase:'error',instance:'crop',error:'Child failed'};await poll();
            check(placeholders.crop.textContent.includes('Child failed')&&frames.get('dnb')===skin);
            modules.crop={phase:'running',instance:'crop-new'};await poll();
            check(frames.get('crop').dataset.instance==='crop-new'&&placeholders.crop.hidden);
            modules.dnb={phase:'stopped',instance:'skin'};await poll();
            check(skin.removed&&placeholders.dnb.textContent.includes('share this service'));
            initialSelection=true;modules.crop={phase:'stopped',startup_enabled:false};
            modules.dnb={phase:'running',instance:'skin-new',startup_enabled:true};await poll();
            check(active==='dnb'&&placeholders.crop.textContent.includes('startup is disabled'));
            active='crop';await poll();check(active==='crop');
            initialSelection=true;modules.dnb={phase:'stopped',startup_enabled:false};await poll();
            check(active==='crop'&&!placeholders.crop.hidden&&!placeholders.dnb.hidden);
          })().then(()=>globalThis.moduleIsolationCheck='ok',e=>globalThis.moduleIsolationCheck=e.message);
        """
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
            routing_check = "for(const result of [globalThis.localTransferCheck,globalThis.cropTransferCheck,globalThis.comfyFreeCheck,globalThis.moduleIsolationCheck])if(result!=='ok')throw Error(result||'UI request check incomplete');"
            for script in re.findall(r"<script[^>]*>(.*?)</script>", html + crop, re.S) + [harness, theme_harness, shell_js, badge_harness, preview_harness, feedback_harness, variation_harness, isolation_harness, routing_check]:
                source = js.JSStringCreateWithUTF8CString(script.encode())
                error = ctypes.c_void_p()
                try:
                    self.assertTrue(js.JSCheckScriptSyntax(context, source, None, 1, ctypes.byref(error)))
                    if script in (harness, theme_harness, badge_harness, preview_harness, feedback_harness, variation_harness, isolation_harness, routing_check):
                        js.JSEvaluateScript(context, source, None, None, 1, ctypes.byref(error))
                        if error.value:
                            message = js.JSValueToStringCopy(context, error, None)
                            try:
                                buffer = ctypes.create_string_buffer(4096)
                                js.JSStringGetUTF8CString(message, buffer, len(buffer))
                                self.fail(buffer.value.decode())
                            finally:
                                js.JSStringRelease(message)
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
