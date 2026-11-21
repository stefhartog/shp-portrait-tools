"""Exercise editor selection in the shell with disposable browser state."""

import html
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading
import unittest


EDGE = Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Microsoft" / "Edge" / "Application" / "msedge.exe"
ROOT = Path(__file__).resolve().parents[1] / "src" / "shp_portrait_tools"


@unittest.skipUnless(EDGE.is_file(), "Microsoft Edge is not installed")
class PhotoshopShellUiTests(unittest.TestCase):
    def test_editor_selection_unlocks_navigation_after_shell_moves_settings(self):
        skin = (ROOT / "modules" / "dnb" / "ui" / "index.html").read_text(encoding="utf-8")
        shell = (ROOT / "shell" / "index.html").read_text(encoding="utf-8")
        shell_js = (ROOT / "shell" / "shell.js").read_text(encoding="utf-8")
        shell_css = (ROOT / "shell" / "shell.css").read_text(encoding="utf-8")
        common_css = (ROOT / "shared" / "ui" / "common.css").read_text(encoding="utf-8")
        lookup = "const generalSettingIds =" + skin.split("const generalSettingIds =", 1)[1].split("const SKINTONE_DEFAULTS", 1)[0]
        busy = "let panelOperationBusy =" + skin.split("let panelOperationBusy =", 1)[1].split("function previewFeedback", 1)[0]
        handlers = "$('editorSelect').onchange =" + skin.split("$('editorSelect').onchange =", 1)[1].split("let watchFolderErrors", 1)[0]
        markup = skin.split("<body>", 1)[1].split("<script>", 1)[0]
        child = '<!doctype html><meta charset="utf-8">' + markup + "<script>" + """
          const state={busy:false,loaded:false,sourceKind:null};
          let shellConnected=true,statusOk=false;
          window.selected='photocraft';
          window.api=async(path,body)=>{
            if(path==='/api/editor/pair')return {code:'fixture',expires_seconds:120};
            if(path==='/api/editor/revoke')return {};
            if(body.editor) {
              if(window.rejectNext){window.rejectNext=false;throw Error('fixture error');}
              if(window.delayNext){
                window.delayNext=false;
                await new Promise(resolve=>window.releaseSelection=resolve);
              }
              window.selected=body.editor;
            }
            return {editor:window.selected};
          };
          const setStatus=(message,error=false)=>window.testStatus={message,error};
        """ + lookup + busy + handlers + """
          syncPanelAvailability();
          window.testState=state;
          window.addEventListener('message',event=>{
            if(event.source===parent&&event.data.type==='shp-tools-probe')
              parent.postMessage({type:'shp-skin-ready'},parent.location.origin);
          });
        </script>"""
        child = child.replace("location.origin", "parent.location.origin").replace("parent.parent.location.origin", "parent.location.origin")
        body = shell.split("<body>", 1)[1].split("</body>", 1)[0]
        fixture = """
          const create=document.createElement.bind(document);
          document.createElement=tag=>{const node=create(tag);if(tag==='iframe')node.srcdoc=CHILD;return node;};
          window.fetch=async()=>({ok:true,json:async()=>({modules:{
            crop:{phase:'stopped'},dnb:{phase:'running',instance:'fixture'}
          }})});
        """.replace("CHILD", json.dumps(child))
        checks = """
          const wait=ms=>new Promise(resolve=>setTimeout(resolve,ms));
          (async()=>{
            for(let i=0;i<100&&!document.getElementById('editorSelect');i++)await wait(20);
            const get=id=>document.getElementById(id);
            const frame=document.querySelector('#panel-dnb iframe'), child=frame.contentWindow;
            const select=get('editorSelect');
            if(!select)throw Error('settings not mounted');
            const tabRects=[...document.querySelectorAll('.shell-header [role=tab]:not([hidden])')]
              .map(tab=>tab.getBoundingClientRect());
            if(tabRects.some((rect,index)=>index && rect.left < tabRects[index-1].right-1))
              throw Error('tab headers overlap at narrow width');
            child.eval(`parent.postMessage({type:'shp-skin-cache',tabs:{
              mask:{state:'ready',label:'Mask cached'},
              frequency:{state:'warming',label:'Preparing FS'},
              dnb:{state:'unavailable',label:'D&B skipped'},
              combine:{state:'idle',label:'Combine not checked'}
            }},parent.location.origin)`);
            await wait(30);
            for(const [mode,status,label] of [
              ['mask','ready','Mask cached'],['frequency','warming','Preparing FS'],
              ['dnb','unavailable','D&B skipped'],['combine','idle','Combine not checked']]) {
              const tab=get('tab-'+mode);
              if(tab.querySelector('.cache-indicator').dataset.state!==status ||
                 !tab.getAttribute('aria-label').includes(label) || !tab.title.includes(label))
                throw Error('incorrect cache indicator for '+mode);
            }
            const luminance=color=>color.match(/[0-9]+/g).slice(0,3).map(n=>n/255)
              .map(v=>v<=.04045?v/12.92:((v+.055)/1.055)**2.4)
              .reduce((sum,v,i)=>sum+v*[.2126,.7152,.0722][i],0);
            for(const theme of ['', 'photocraft']) {
              document.documentElement.dataset.theme=theme;
              const symbols=new Set();
              for(const indicator of document.querySelectorAll('.cache-indicator')) {
                const tab=indicator.closest('[role=tab]'), style=getComputedStyle(indicator,'::before');
                const color=style.color.match(/[0-9]+/g).slice(0,3);
                const a=luminance(style.color), b=luminance(getComputedStyle(tab).backgroundColor);
                if(new Set(color).size!==1 || (Math.max(a,b)+.05)/(Math.min(a,b)+.05)<3 ||
                   parseFloat(style.fontSize)>=parseFloat(getComputedStyle(tab).fontSize))
                  throw Error('cache indicator is not smaller, gray, and legible: '+theme);
                symbols.add(style.content);
              }
              if(symbols.size!==4) throw Error('cache symbols are not distinct');
            }
            delete document.documentElement.dataset.theme;
            window.dispatchEvent(new MessageEvent('message',{origin:location.origin,source:window,
              data:{type:'shp-skin-cache',tabs:{mask:{state:'unavailable',label:'spoof'}}}}));
            if(get('tab-mask').querySelector('.cache-indicator').dataset.state!=='ready')
              throw Error('accepted cache status from wrong window');
            child.setEditorUi({editor:'photoshop'});
            if(get('photoshopHueMode') || get('photoshopCaptureMode')) throw Error('Retired selector present');
            if(get('photoshopPairing').hidden) throw Error('Pairing options hidden');
            child.delayNext=true;select.value='photoshop';
            const change=select.onchange();
            let selectionError=null;
            change.catch(error=>selectionError=error);
            await wait(30);
            if(selectionError)throw Error('selection failed while busy: '+selectionError.message);
            if(!child.testState.busy || !get('tab-mask').disabled || !select.disabled ||
               !get('photoshopPair').disabled || !get('photoshopRevoke').disabled)
              throw Error('pending selection did not lock controls');
            child.releaseSelection();await change;await wait(30);
            for(const id of ['editorSelect','photoshopPair','photoshopRevoke'])
              if(Object.hasOwn(get(id),'disabled'))throw Error(id+' still has a panel setter');
            if(child.testState.busy || get('tab-mask').disabled || select.disabled ||
               get('photoshopPair').disabled || get('photoshopRevoke').disabled ||
               get('photoshopPairing').hidden ||
               frame.contentDocument.getElementById('pcGet').disabled ||
               !frame.contentDocument.getElementById('pcSend').disabled ||
               !frame.contentDocument.getElementById('apply').disabled)
              throw Error('successful selection left wrong availability');
            await get('photoshopPair').onclick();
            if(!get('photoshopPairCode').textContent.includes('fixture'))throw Error('pairing control unavailable');
            get('tab-frequency').click();
            if(get('tab-frequency').getAttribute('aria-selected')!=='true')throw Error('navigation failed');
            get('tab-settings').click();
            child.rejectNext=true;select.value='photocraft';await select.onchange();await wait(30);
            if(select.value!=='photoshop'||child.testState.busy||get('tab-mask').disabled||
               !child.testStatus.error||!child.testStatus.message.includes('fixture error'))
              throw Error('failed selection did not restore state');
            select.value='photocraft';await select.onchange();await wait(30);
            if(get('photoshopPairing').hidden!==true||get('tab-mask').disabled)
              throw Error('PhotoCraft selection failed');
            get('result').textContent='PASS';
          })().catch(error=>document.getElementById('result').textContent='FAIL: '+error.message);
        """
        page = ('<!doctype html><meta charset="utf-8"><style>' + common_css + shell_css + '</style>' + body + '<pre id="result">PENDING</pre><script>'
                + fixture.replace("</script>", "<\\/script>") + shell_js + checks + "</script>")
        # Headless Edge can outlive --dump-dom briefly and keep its profile locked.
        with tempfile.TemporaryDirectory(prefix="shp-editor-ui-", ignore_cleanup_errors=True) as directory:
            path = Path(directory)
            (path / "index.html").write_text(page, encoding="utf-8")
            server = ThreadingHTTPServer(("127.0.0.1", 0), partial(SimpleHTTPRequestHandler, directory=directory))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                result = subprocess.run(
                    [str(EDGE), "--headless", "--disable-gpu", "--disable-extensions",
                     "--window-size=640,700",
                     "--disable-background-networking", "--no-first-run", "--no-default-browser-check",
                     "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1",
                     f"--user-data-dir={path / 'profile'}", "--virtual-time-budget=5000", "--dump-dom",
                     f"http://127.0.0.1:{server.server_port}/"],
                    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
                )
                self.assertEqual(result.returncode, 0, result.stderr[-2000:])
                if not result.stdout.strip():
                    self.skipTest("headless Edge produced no --dump-dom output on this host")
                match = re.search(r'<pre id="result">([^<]+)</pre>', result.stdout)
                self.assertIsNotNone(match, result.stderr[-2000:])
                self.assertEqual(html.unescape(match.group(1)), "PASS")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
