"""Disposable headless layout checks; no Tools service or application requests."""

import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import tempfile
import time
import unittest

from shp_photocraft_tools.shared.ui import stylesheet


@unittest.skipUnless(shutil.which("firefox"), "Firefox is not installed")
class StatusLayoutTests(unittest.TestCase):
    def test_screen_centering_responsive_controls_and_progress_background(self):
        root = Path(__file__).resolve().parents[1] / "src/shp_photocraft_tools"
        with tempfile.TemporaryDirectory(prefix="tools-status-layout-") as folder:
            profile = Path(folder)
            with socket.socket() as reserved:
                reserved.bind(("127.0.0.1", 0))
                port = reserved.getsockname()[1]
            (profile / "user.js").write_text(
                f'user_pref("marionette.port", {port});\n'
                'user_pref("browser.startup.page", 0);\n'
                'user_pref("browser.shell.checkDefaultBrowser", false);\n'
                'user_pref("browser.aboutwelcome.enabled", false);\n'
                'user_pref("browser.newtabpage.enabled", false);\n'
                'user_pref("datareporting.policy.dataSubmissionEnabled", false);\n'
                'user_pref("toolkit.telemetry.enabled", false);\n'
                'user_pref("app.update.auto", false);\n'
                'user_pref("network.offline", true);\n'
            )
            process = subprocess.Popen(
                ["firefox", "--headless", "--no-remote", "--profile", folder, "--marionette", "about:blank"],
                env={**os.environ, "MOZ_HEADLESS": "1"},
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            connection = None
            try:
                deadline = time.monotonic() + 20
                while connection is None:
                    try:
                        connection = socket.create_connection(("127.0.0.1", port), timeout=1)
                    except OSError:
                        if process.poll() is not None or time.monotonic() >= deadline:
                            self.fail("Disposable Firefox failed to start its local test connection")
                        time.sleep(.1)
                connection.settimeout(15)
                def read():
                    length = b""
                    while not length.endswith(b":"):
                        byte = connection.recv(1)
                        if not byte:
                            raise RuntimeError("Firefox test connection closed")
                        length += byte
                    remaining = int(length[:-1])
                    chunks = []
                    while remaining:
                        chunk = connection.recv(remaining)
                        if not chunk:
                            raise RuntimeError("Firefox test reply incomplete")
                        chunks.append(chunk)
                        remaining -= len(chunk)
                    return json.loads(b"".join(chunks))
                read()
                sequence = 0
                def command(name, **params):
                    nonlocal sequence
                    sequence += 1
                    payload = json.dumps([0, sequence, name, params]).encode()
                    connection.sendall(str(len(payload)).encode() + b":" + payload)
                    response = read()
                    if response[2] is not None:
                        raise RuntimeError(response[2])
                    result = response[3]
                    return result.get("value", result) if isinstance(result, dict) else result
                command("WebDriver:NewSession", capabilities={"alwaysMatch": {}})
                for module in ("dnb", "crop"):
                    html = (root / f"modules/{module}/ui/index.html").read_text()
                    # Only actual markup/styles: no application script or image/API requests.
                    bar = html.split('<div id="bar">', 1)[1].split("<aside>", 1)[0]
                    styles = "\n".join(re.findall(r"<style>(.*?)</style>", html, re.S))
                    page = profile / f"{module}.html"
                    page.write_text('<!doctype html><meta charset="utf-8"><style>'
                                    + stylesheet().decode() + styles
                                    + '</style><div id="bar">' + bar)
                    command("WebDriver:Navigate", url=page.as_uri())
                    for width in (1800, 1400, 1000, 640, 480):
                        command("WebDriver:SetWindowRect", width=width, height=800)
                        for long_preset in (False, True):
                            result = command("WebDriver:ExecuteScript", script="""
                              const status=document.getElementById('status');
                              status.textContent='Preparing full-resolution layers...';
                              status.className='busy';
                              const preset=document.getElementById('presetSel');
                              if(!document.querySelector('.theme-picker')) {
                                const theme=document.createElement('label');theme.className='theme-picker';
                                theme.innerHTML='Theme <select><option>PhotoCraft</option></select>';
                                document.getElementById('presetBar').prepend(theme);
                              }
                              preset.innerHTML='<option>'+('Preset '.repeat(arguments[0]?40:1))+'</option>';
                              document.getElementById('pcNativeFS')?.removeAttribute('hidden');
                              const rect=id=>document.getElementById(id).getBoundingClientRect();
                              const s=rect('status'),a=rect('barActions'),p=rect('presetBar');
                              const overlap=r=>s.left<r.right && s.right>r.left && s.top<r.bottom && s.bottom>r.top;
                              const progress=getComputedStyle(status,'::before');
                              return {center:s.left+s.width/2,screen:innerWidth,
                                overlaps:overlap(a)||overlap(p),
                                overflow:document.documentElement.scrollWidth>innerWidth,
                                animation:progress.animationName,
                                pointer:progress.pointerEvents,
                                statusPointer:getComputedStyle(document.getElementById('statusWrap')).pointerEvents};
                            """, args=[long_preset], sandbox=None)
                            with self.subTest(module=module, width=width, long_preset=long_preset):
                                self.assertAlmostEqual(result["center"], result["screen"]/2, delta=1)
                                self.assertFalse(result["overlaps"])
                                self.assertFalse(result["overflow"])
                                self.assertEqual(result["animation"], "status-progress")
                                self.assertEqual(result["pointer"], "none")
                                self.assertEqual(result["statusPointer"], "none")
                    idle = command("WebDriver:ExecuteScript", script="""
                      document.getElementById('status').className='';
                      return getComputedStyle(document.getElementById('status'),'::before').animationName;
                    """, args=[], sandbox=None)
                    self.assertEqual(idle, "none")
                    reduced = command("WebDriver:ExecuteScript", script="""
                      for(const sheet of document.styleSheets)for(const rule of sheet.cssRules) {
                        if(rule.media?.mediaText.includes('prefers-reduced-motion'))rule.media.mediaText='all';
                      }
                      document.getElementById('status').className='busy';
                      const style=getComputedStyle(document.getElementById('status'),'::before');
                      return {animation:style.animationName,transform:style.transform};
                    """, args=[], sandbox=None)
                    self.assertEqual(reduced, {"animation":"none", "transform":"none"})
                    if module == "dnb":
                        self.check_combine_controls(command, html, profile)
                        self.check_analysis_controls(command, html, profile)
                        self.check_preview_images(command, html, profile)
                        self.check_panel_availability(command, html, profile)
                        self.check_slider_release(command, html, profile)
                        self.check_directional_controls(command, html, profile)
                self.check_switch_geometry(command, root, profile)
                self.check_preview_tabs(command, root, profile)
                self.check_panel_dimming(command, root, profile)
                self.check_panel_graph_geometry(command, root, profile)
                self.check_stretch_removed(command, root, profile)
                self.check_crop_setting_queue(command, root)
                command("WebDriver:DeleteSession")
            finally:
                if connection is not None:
                    connection.close()
                if process.poll() is None:
                    process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)

    def check_panel_dimming(self, command, root, profile):
        for module in ("dnb", "crop"):
            html=(root/f"modules/{module}/ui/index.html").read_text()
            markup=re.sub(r"<script\b[^>]*>.*?</script>|<link\b[^>]*>","",html,flags=re.S)
            page=profile/f"{module}-panel-disabled.html"
            page.write_text(markup.replace("</head>","<style>"+stylesheet().decode()+"</style></head>"))
            command("WebDriver:Navigate",url=page.as_uri())
            before=command("WebDriver:ExecuteScript",script="""
              const details=arguments[0]==='dnb'?document.getElementById('g-mask'):document.querySelector('details.crop-section');
              const body=details.querySelector('.gbody');
              const label=document.createElement('label');label.id='panelDimLabel';label.textContent='setting label';
              body.append(label);
              const summary=details.querySelector(':scope > summary');
              const normal={header:getComputedStyle(summary).color,label:getComputedStyle(label).color};
              const input=document.createElement('input');input.type='range';input.disabled=true;
              if(arguments[0]==='dnb'){input.classList.add('panel-disabled');document.getElementById('maskSliders').append(input);}
              else {document.getElementById('xPrompt').disabled=true;document.getElementById('xAdv').append(input);}
              return {header:normal.header,label:normal.label,
                hasDisabled:[...details.querySelectorAll('input:disabled')].length>0};
            """,args=[module],sandbox=None)
            for theme in ("photocraft","dark"):
                command("WebDriver:ExecuteScript",script="document.documentElement.dataset.theme=arguments[0];",
                        args=[theme],sandbox=None)
                result=command("WebDriver:ExecuteScript",script="""
                  const details=arguments[0]==='dnb'?document.getElementById('g-mask'):document.querySelector('details.crop-section');
                  const summary=details.querySelector(':scope > summary');
                  const label=document.getElementById('panelDimLabel');
                  const disabled=[...details.querySelectorAll('input:disabled')];
                  return {header:getComputedStyle(summary).color,
                    headerOpacity:getComputedStyle(summary).opacity,
                    label:getComputedStyle(label).color,
                    labelOpacity:getComputedStyle(label).opacity,
                    controls:disabled.map(e=>getComputedStyle(e).opacity),
                    toggle:arguments[0]==='dnb'?{
                      opacity:getComputedStyle(document.getElementById('maskOn')).opacity,
                      cursor:getComputedStyle(document.getElementById('maskOn')).cursor
                    }:null};
                """,args=[module],sandbox=None)
                self.assertNotEqual(result["header"],before["header"])
                self.assertNotEqual(result["label"],before["label"],f"{module} {theme} label color")
                self.assertEqual(result["headerOpacity"],"1")
                self.assertEqual(result["labelOpacity"],"1")
                self.assertTrue(before["hasDisabled"])
                self.assertTrue(all(opacity=="0.5" for opacity in result["controls"]))
                native=command("WebDriver:ExecuteScript",script="""
                  const e=document.createElement('button');
                  e.textContent='native disabled';e.disabled=true;
                  document.querySelector('aside').append(e);
                  return getComputedStyle(e).opacity;
                """,args=[],sandbox=None)
                self.assertEqual(native,"0.5")
                if module=="dnb":
                    self.assertEqual(result["toggle"],{"opacity":"1","cursor":"pointer"})
                    checked=command("WebDriver:ExecuteScript",script="""
                      const toggle=document.getElementById('maskOn');toggle.checked=true;
                      const initial=toggle.checked;toggle.click();return [initial,toggle.checked];
                    """,args=[],sandbox=None)
                    self.assertEqual(checked,[True,False])

    def check_directional_controls(self, command, html, profile):
        markup=re.sub(r"<script\b[^>]*>.*?</script>|<link\b[^>]*>","",html,flags=re.S)
        page=profile/"directional-limb-controls.html"
        page.write_text(markup.replace("</head>","<style>"+stylesheet().decode()+"</style></head>"))
        command("WebDriver:Navigate",url=page.as_uri())
        defaults="const DIRECTIONAL_DEFAULTS ="+html.split("const DIRECTIONAL_DEFAULTS =",1)[1].split("function uiMode(",1)[0]
        helper="const directionalSegments ="+html.split("const directionalSegments =",1)[1].split("function syncTransfer()",1)[0]
        panel="let panelOperationBusy"+html.split("let panelOperationBusy",1)[1].split("function previewFeedback",1)[0]
        slider_helper="function slider("+html.split("function slider(",1)[1].split("let sliderDrag",1)[0]
        result=command("WebDriver:ExecuteScript",script="""
          const $=id=>document.getElementById(id);
        """+defaults+"""
          const state={directional:directionalSettings(),mode:'frequency',loaded:false,
            imageInfo:{directional_pose:{}}};
          const markDirty=()=>{},syncTabs=()=>{},tabSel={B:'color'};
          const statusOk=false,pendingShellMode=null;
        """+slider_helper+helper+panel+"""
          const reference=slider($('blurSliders'),'reference',0,100,1,50,()=>{});
          const parity=[];
          const effective=e=>{
            let opacity=1;for(let node=e;node;node=node.parentElement)opacity*=Number(getComputedStyle(node).opacity);
            const style=getComputedStyle(e);return {opacity,color:style.color,background:style.backgroundColor};
          };
          const checkParity=name=>{
            for(const theme of ['photocraft','dark']){
              document.documentElement.dataset.theme=theme;
              const groups=[...Object.values(directionalPairSliders).flatMap(Object.values),
                ...Object.values(directionalTorsoSliders),...Object.values(directionalSliders)];
              const controls=groups.map(s=>{
                reference.rng.disabled=reference.num.disabled=s.rng.disabled;
                return {rng:effective(s.rng),num:effective(s.num),
                  refRng:effective(reference.rng),refNum:effective(reference.num)};
              });
              reference.num.disabled=$('directionalStart').disabled;
              parity.push({name,theme,controls,output:effective($('directionalRangeValue')),
                refOutput:effective(reference.num),
                track:Number(getComputedStyle(document.querySelector('.directional-range'),'::before').opacity),
                expectedTrack:$('directionalStart').disabled?.5:1});
            }
          };
          state.imageInfo.directional_pose=Object.fromEntries(directionalSegments.map(([,key])=>[key,'Available']));
          state.imageInfo.directional_pose.torso='Available';
          $('g-directional').hidden=false;
          $('directionalSliders').hidden=false;
          $('g-directional').open=true;
          syncDirectionalControls();syncPanelAvailability();
          const controls=directionalSegments.map(([id])=>$(id));
          const groupContainers=[...directionalPairs.map(([pair])=>$('directionalPair-'+pair)),$('directionalTorsoControls')];
          const hiddenOff=groupContainers.every(group=>group.hidden);
          const initiallyOff=controls.every(e=>e.disabled);
          checkParity('panel off');
          const name=document.querySelector('#directionalSegments .limb-name');
          const offColor=getComputedStyle(name).color;
          const defaults=controls.map(e=>e.checked);
          $('directionalOn').checked=true;$('directionalOn').onchange();syncPanelAvailability();
          const enabled=controls.every(e=>!e.disabled);
          const hiddenUnselected=$('directionalPair-upper_arm').hidden&&$('directionalPair-forearm').hidden&&
            $('directionalPair-lower_leg').hidden&&$('directionalTorsoControls').hidden&&!$('directionalPair-upper_leg').hidden;
          checkParity('unselected pairs');
          const onColor=getComputedStyle(name).color;
          $('directionalLeftForearm').checked=true;$('directionalLeftForearm').onchange();
          const selected=state.directional.left_forearm;
          $('directionalTorso').checked=true;$('directionalTorso').onchange();
          const torsoEnabled=Object.values(directionalTorsoSliders).every(s=>!s.rng.disabled);
          const torsoVisible=!$('directionalTorsoControls').hidden;
          checkParity('selected torso');
          state.imageInfo.directional_pose.torso='Unavailable: no hips';
          syncDirectionalControls();
          const torsoMissing=$('directionalTorso').disabled&&$('directionalTorso').checked&&
            $('directionalTorsoControls').hidden&&
            Object.values(directionalTorsoSliders).every(s=>s.rng.disabled)&&
            getComputedStyle($('directionalTorso')).backgroundColor===getComputedStyle($('directionalRightForearm')).backgroundColor&&
            $('directionalTorso').title.includes('Unavailable: no hips');
          checkParity('missing torso');
          state.imageInfo.directional_pose.torso='Available';syncDirectionalControls();
          state.imageInfo.directional_pose.left_forearm='Unavailable: no wrist coordinates';
          syncDirectionalControls();
          checkParity('missing geometry');
          const missing=$('directionalLeftForearm').disabled&&$('directionalLeftForearm').checked;
          const missingHidden=$('directionalPair-forearm').hidden;
          const missingGrey=getComputedStyle($('directionalLeftForearm')).backgroundColor===getComputedStyle($('directionalRightForearm')).backgroundColor;
          const reason=$('directionalLeftForearm').title;
          const partialStatus=$('directionalStatus').textContent.includes('Unavailable selected regions skipped');
          const availability={...state.imageInfo.directional_pose};
          for(const key of Object.keys(availability)) state.imageInfo.directional_pose[key]='Unavailable: no coordinates';
          syncDirectionalControls();setBusy(true);setBusy(false);
          const allMissing=controls.every(e=>e.disabled)&&$('directionalTorso').disabled&&
            $('directionalStatus').textContent.includes('Directional Blur skipped')&&
            Object.keys(directionalPairSliders).every(key=>$('directionalPair-'+key).hidden)&&
            $('directionalTorsoControls').hidden&&state.directional.left_forearm&&state.directional.torso;
          state.imageInfo.directional_pose=availability;
          state.imageInfo.directional_pose.left_forearm='Available';
          syncDirectionalControls();
          setBusy(true);
          syncDirectionalControls();
          const busy=controls.every(e=>e.disabled)&&!$('directionalOn').disabled&&$('directionalTorso').disabled&&
            Object.values(directionalTorsoSliders).every(s=>s.rng.disabled)&&
            Object.values(directionalPairSliders).every(group=>Object.values(group).every(s=>s.rng.disabled));
          const dimmed=getComputedStyle(name).color===offColor&&offColor!==onColor;
          const busyVisible=!$('directionalPair-upper_leg').hidden&&!$('directionalTorsoControls').hidden;
          checkParity('busy');
          setBusy(false);
          const restored=controls.every(e=>!e.disabled)&&$('directionalLeftForearm').checked&&
            Object.values(directionalTorsoSliders).every(s=>!s.rng.disabled)&&
            Object.values(directionalPairSliders.upper_leg).every(s=>!s.rng.disabled);
          checkParity('restored');
          for(const control of controls){control.checked=true;control.onchange();}
          const groupSettings=[...Object.entries(directionalPairSliders).map(([key,sliders])=>[state.directional.pairs[key],sliders]),
            [state.directional.torso_controls,directionalTorsoSliders]].every(([settings,sliders])=>
              ['distance','width','taper','strength','feather','start','end'].every(key=>
                Number(sliders[key].num.value)===settings[key]));
          window.directionalTest={controls};
          return {initiallyOff,defaults,enabled,selected,busy,restored,dimmed,missing,missingGrey,torsoEnabled,torsoMissing,
            hiddenAvailability:!document.querySelector('#directionalSegments .hint'),reason,parity,
            hiddenOff,hiddenUnselected,torsoVisible,missingHidden,busyVisible,groupSettings,partialStatus,allMissing};
        """,args=[],sandbox=None)
        self.assertTrue(all(result[k] for k in ("initiallyOff","enabled","selected","busy","restored","dimmed","missing","missingGrey","torsoEnabled","torsoMissing","hiddenAvailability")),result)
        self.assertEqual(result["defaults"],[True,True,False,False,False,False,False,False])
        self.assertTrue(all(result[k] for k in ("hiddenOff","hiddenUnselected","torsoVisible","missingHidden","busyVisible","groupSettings","partialStatus","allMissing")),result)
        self.assertIn("Unavailable: no wrist coordinates",result["reason"])
        for snapshot in result["parity"]:
            with self.subTest(state=snapshot["name"],theme=snapshot["theme"]):
                for control in snapshot["controls"]:
                    self.assertEqual(control["rng"],control["refRng"])
                    self.assertEqual(control["num"],control["refNum"])
                self.assertEqual(snapshot["output"],snapshot["refOutput"])
                self.assertEqual(snapshot["track"],snapshot["expectedTrack"])
        for theme,width in ((theme,width) for theme in ("photocraft","dark") for width in (1800,980,480)):
            command("WebDriver:SetWindowRect",width=width,height=800)
            geometry=command("WebDriver:ExecuteScript",script="""
              document.documentElement.dataset.theme=arguments[0];
              const aside=document.querySelector('aside').getBoundingClientRect();
              const rows=[...document.querySelectorAll('#directionalSegments > .row:not(#directionalTorsoRow)')];
              if(rows.length!==4||rows.map(r=>r.querySelector('.limb-name').textContent).join('|')!=='upper arm|lower arm|upper leg|lower leg')return false;
              if(rows.some(row=>{
                const inputs=[...row.querySelectorAll('input')];
                return !inputs[0].id.startsWith('directionalRight')||!inputs[1].id.startsWith('directionalLeft')||
                  !inputs[0].getAttribute('aria-label').includes('right')||!inputs[1].getAttribute('aria-label').includes('left');
              }))return false;
              const groups=['upper_arm','forearm','upper_leg','lower_leg'].map(key=>document.getElementById('directionalPair-'+key));
              groups.push(document.getElementById('directionalTorsoControls'));
              const torso=document.getElementById('directionalTorsoRow');
              if(torso.nextElementSibling!==groups[4])return false;
              if(groups.some((group,i)=>{
                const r=group.getBoundingClientRect(),header=(i<4?rows[i]:torso).getBoundingClientRect();
                return group.previousElementSibling!==(i<4?rows[i]:torso)||r.top<header.bottom||
                  [...group.querySelectorAll('.row')].some(row=>getComputedStyle(row).display!=='flex')||
                  r.left<aside.left||r.right>aside.right;
              }))return false;
              const tr=torso.getBoundingClientRect(),tl=torso.querySelector('.limb-name').getBoundingClientRect();
              if(Math.abs((tl.left+tl.right-tr.left-tr.right)/2)>1)return false;
              return directionalTest.controls.map(e=>{
                const row=e.closest('.row'),r=row.getBoundingClientRect();
                const label=row.querySelector('.limb-name').getBoundingClientRect();
                const sides=[...row.querySelectorAll('input')].map(e=>e.getBoundingClientRect());
                const center=b=>(b.left+b.right)/2;
                if(Math.abs(center(label)-center(r))>1||Math.abs(center(r)-(center(sides[0])+center(sides[1]))/2)>1)return ['center',e.id,center(label),center(r),sides.map(center)];
                if(Math.abs(r.width-document.getElementById('directionalSegments').getBoundingClientRect().width)>1)return ['width',e.id,r.width];
                if(!e.getAttribute('aria-label').includes('anatomical')||!e.title.includes('not image side'))return ['aria',e.id,e.title];
                return r.left>=aside.left&&r.right<=aside.right&&[...row.children].every(child=>{
                  const c=child.getBoundingClientRect();return c.left>=r.left-.1&&c.right<=r.right+.1;
                });
              });
            """,args=[theme],sandbox=None)
            self.assertTrue(all(item is True for item in geometry),geometry)
        for selector in ("#directionalLeftUpperArm","#directionalRightForearm","#directionalLeftLowerLeg","#directionalTorso"):
            element=command("WebDriver:FindElement",using="css selector",value=selector)
            element_id=element["element-6066-11e4-a52e-4f735466cecf"]
            before=command("WebDriver:ExecuteScript",script="return document.querySelector(arguments[0]).checked;",
                           args=[selector],sandbox=None)
            command("WebDriver:ElementSendKeys",id=element_id,text=" ")
            after=command("WebDriver:ExecuteScript",script="""
              const e=document.querySelector(arguments[0]);
              return {checked:e.checked,focus:e.matches(':focus-visible'),outline:getComputedStyle(e).outlineStyle};
            """,args=[selector],sandbox=None)
            self.assertNotEqual(after["checked"],before)
            self.assertTrue(after["focus"])
            self.assertEqual(after["outline"],"solid")
            command("WebDriver:ElementClick",id=element_id)
            restored=command("WebDriver:ExecuteScript",script="return document.querySelector(arguments[0]).checked;",
                             args=[selector],sandbox=None)
            self.assertEqual(restored,before)

    def check_panel_graph_geometry(self, command, root, profile):
        html=(root/"modules/dnb/ui/index.html").read_text()
        markup=re.sub(r"<script\b[^>]*>.*?</script>|<link\b[^>]*>","",html,flags=re.S)
        page=profile/"dnb-panel-graphs.html"
        page.write_text(markup.replace("</head>","<style>"+stylesheet().decode()+"</style></head>"))
        command("WebDriver:Navigate",url=page.as_uri())
        helper="async function drawProfile() {"+html.split("async function drawProfile() {",1)[1].split("function bandsPayload()",1)[0]
        output=command("WebDriver:ExecuteAsyncScript",script="""
          const done=arguments[arguments.length-1],$=id=>document.getElementById(id);
          const state={strength:.7,secondBand:{lo:12,hi:80,taper:.8,strength:1.3}};
          const bandsPayload=()=>[[1.5,20,1,.5]],setStatus=()=>{};
          const requests=[],painted=[];
          for(const id of ['profile','profile2']){
            const ctx=$(id).getContext('2d'),clear=ctx.clearRect.bind(ctx);
            ctx.clearRect=(...args)=>{painted.push(id);clear(...args);};
          }
          const api=async(path,p)=>{requests.push(p);return {sizes:[1,2],gains:[.2,.3]};};
        """+helper+"""
          drawProfile();
          window.setTimeout(()=>done({requests,painted,
            order:[...document.querySelectorAll('aside > details')].map(e=>e.id)}),150);
        """,args=[],sandbox=None)
        self.assertEqual(output["requests"],[
            {"bands":[[1.5,20,1,.5]],"strength":.7},
            {"bands":[[12,80,1,.8]],"strength":1.3}])
        self.assertEqual(output["painted"],["profile","profile2"])
        self.assertLess(output["order"].index("g-band2"),output["order"].index("g-dnb-shared"))
        command("WebDriver:ExecuteScript",script="""
          for(const id of ['g-range','g-skintone','g-bands']) {
            const panel=document.getElementById(id);panel.hidden=false;panel.open=true;
          }
        """,args=[],sandbox=None)
        for theme in ("photocraft","dark"):
            for width in (1800,980,480):
                command("WebDriver:SetWindowRect",width=width,height=800)
                result=command("WebDriver:ExecuteScript",script="""
                  document.documentElement.dataset.theme=arguments[0];
                  document.getElementById('g-band2').hidden=false;
                  document.getElementById('g-band2').open=true;
                  return ['hueDistribution','toneplot','profile','profile2'].map(id=>{
                    const c=document.getElementById(id),s=getComputedStyle(c),r=c.getBoundingClientRect();
                    const a=document.querySelector('aside').getBoundingClientRect();
                    return {id,height:r.height,backingHeight:c.height,width:c.width,
                      transform:s.transform,left:r.left,right:r.right,asideLeft:a.left,asideRight:a.right};
                  });
                """,args=[theme],sandbox=None)
                expected={"hueDistribution":(110,198),"toneplot":(70,140),"profile":(70,140),"profile2":(70,140)}
                for graph in result:
                    height,backing=expected[graph["id"]]
                    with self.subTest(theme=theme,width=width,graph=graph["id"]):
                        self.assertEqual(graph["height"],height)
                        self.assertEqual(graph["backingHeight"],backing)
                        self.assertEqual(graph["transform"],"matrix(1, 0, 0, 1, -2, 0)")
                        self.assertGreaterEqual(graph["left"],graph["asideLeft"])
                        self.assertLessEqual(graph["right"],graph["asideRight"])

    def check_stretch_removed(self, command, root, profile):
        html=(root/"modules/dnb/ui/index.html").read_text()
        markup=re.sub(r"<script\b[^>]*>.*?</script>|<link\b[^>]*>","",html,flags=re.S)
        page=profile/"dnb-stretch-removed.html"
        page.write_text(markup.replace("</head>","<style>"+stylesheet().decode()+"</style></head>"))
        source=re.search(r"function paneKinds\(\) \{.*?\n\}",html,re.S).group(0)
        command("WebDriver:Navigate",url=page.as_uri())
        result=command("WebDriver:ExecuteScript",script="""
          const paneKinds=new Function('tabSel','state',arguments[0]+';return paneKinds;');
          localStorage.setItem('dnb-layerX','1');
          const stretchedPreference=paneKinds({A:'orig',B:'layer'},{mode:'dnb'})();
          localStorage.setItem('dnb-layerX','0');
          const normalPreference=paneKinds({A:'orig',B:'layerm'},{mode:'dnb'})();
          const band2=paneKinds({A:'result',B:'layer2m'},{mode:'dnb'})();
          const combined=paneKinds({A:'result',B:'dnb_result'},{mode:'combine'})();
          return {control:!!document.getElementById('layerX'),
            options:!!document.getElementById('optsLayer'),stretchedPreference,normalPreference,band2,combined};
        """,args=[source],sandbox=None)
        self.assertFalse(result["control"])
        self.assertFalse(result["options"])
        self.assertEqual(result["stretchedPreference"],["orig","layerx"])
        self.assertEqual(result["normalPreference"],["orig","layermx"])
        self.assertEqual(result["band2"],["result","layer2mx"])
        self.assertEqual(result["combined"],["result","dnb_result"])

    def check_preview_tabs(self, command, root, profile):
        for module in ("dnb", "crop"):
            html = (root / f"modules/{module}/ui/index.html").read_text()
            markup = re.sub(r"<script\b[^>]*>.*?</script>|<link\b[^>]*>", "", html, flags=re.S)
            page = profile / f"{module}-preview-tabs.html"
            page.write_text(markup.replace("</head>", "<style>" + stylesheet().decode() + "</style></head>"))
            command("WebDriver:Navigate", url=page.as_uri())
            command("WebDriver:ExecuteScript", script="""
              if(arguments[0]==='crop') {
                const frame=document.getElementById('xTabFrame');
                const result=document.createElement('button');result.className='tab';result.textContent='Result';
                document.getElementById('xResTabs').append(result);
              }
              document.querySelectorAll('.pane-header .tab').forEach(button=>{
                button.onclick=()=>{
                  button.closest('.tabs').querySelectorAll('.tab').forEach(tab=>tab.classList.toggle('on',tab===button));
                };
                button.hidden=false;
              });
            """, args=[module], sandbox=None)
            for theme in ("photocraft", "dark"):
                for width in (1800, 980, 480):
                    command("WebDriver:SetWindowRect", width=width, height=800)
                    result = command("WebDriver:ExecuteScript", script="""
                      document.documentElement.dataset.theme=arguments[0];
                      const buttons=[...document.querySelectorAll('.pane-header .tab')];
                      return buttons.map(button=>{
                        const s=getComputedStyle(button),r=button.getBoundingClientRect();
                        const sample=document.createElement('span');sample.style.color='var(--dim)';
                        document.body.append(sample);const dim=getComputedStyle(sample).color;sample.remove();
                        return {text:button.textContent.trim(),active:button.classList.contains('on'),
                          font:s.fontSize,body:getComputedStyle(document.body).fontSize,
                          bottom:s.paddingBottom,left:s.paddingLeft,right:s.paddingRight,
                          height:r.height,color:s.color,dim,background:getComputedStyle(document.body).backgroundColor,
                          leftEdge:r.left,rightEdge:r.right,viewport:innerWidth};
                      });
                    """, args=[theme], sandbox=None)
                    self.assertGreaterEqual(len(result), 3 if module == "dnb" else 2)
                    for tab in result:
                        with self.subTest(module=module, theme=theme, width=width, tab=tab["text"]):
                            self.assertEqual(tab["font"], "13px")
                            self.assertEqual(tab["body"], "13px")
                            self.assertEqual(tab["bottom"], "4px")
                            self.assertEqual(tab["left"], "14px")
                            self.assertEqual(tab["right"], "14px")
                            self.assertGreaterEqual(tab["height"], 26)
                            self.assertGreaterEqual(tab["leftEdge"], 0)
                            self.assertLessEqual(tab["rightEdge"], tab["viewport"])
                            if not tab["active"]:
                                def channels(value):
                                    values = [float(v) for v in re.findall(r"[\d.]+", value)[:3]]
                                    return [v*255 for v in values] if value.startswith("color(srgb ") else values
                                self.assertLess(sum(channels(tab["color"])), sum(channels(tab["dim"])))
                                def luminance(value):
                                    rgb = [c/255 for c in channels(value)]
                                    rgb = [c/12.92 if c <= .04045 else ((c+.055)/1.055)**2.4 for c in rgb]
                                    return sum(c*w for c,w in zip(rgb, (.2126,.7152,.0722)))
                                fg,bg=luminance(tab["color"]),luminance(tab["background"])
                                self.assertGreaterEqual((fg+.05)/(bg+.05),4.5)
                before=command("WebDriver:ExecuteScript",script="""
                  const group=document.querySelector('.pane-header .tabs,.pane-header.tabs');
                  const active=group.querySelector('.tab.on');
                  const inactive=[...group.querySelectorAll('.tab')].find(t=>!t.classList.contains('on'));
                  return {active:active.textContent.trim(),inactive:inactive.textContent.trim()};
                """,args=[],sandbox=None)
                element = command("WebDriver:FindElement", using="css selector",
                              value=".pane-header.tabs .tab:not(.on),.pane-header .tabs .tab:not(.on)")
                element_id=element["element-6066-11e4-a52e-4f735466cecf"]
                command("WebDriver:ElementClick",id=element_id)
                clicked=command("WebDriver:ExecuteScript",script="""
                  return [...document.querySelector('.pane-header .tabs,.pane-header.tabs').querySelectorAll('.tab')]
                .find(t=>t.classList.contains('on')).textContent.trim();
                """,args=[],sandbox=None)
                self.assertEqual(clicked,before["inactive"])
                command("WebDriver:ExecuteScript",script="""
                  document.documentElement.dataset.theme='photocraft';
                  const group=document.querySelector('.pane-header .tabs,.pane-header.tabs');
                  group.querySelectorAll('.tab').forEach(t=>t.classList.toggle('on',t.textContent.trim()===arguments[0]));
                """,args=[before["active"]],sandbox=None)
                command("WebDriver:ExecuteScript",script="""
                  document.querySelector('.pane-header .tabs .tab:not(.on),.pane-header.tabs .tab:not(.on)').focus();
                """,args=[],sandbox=None)
                command("WebDriver:ElementSendKeys",id=element_id,text="\ue007")
                keyboard=command("WebDriver:ExecuteScript",script="""
                  const group=document.querySelector('.pane-header .tabs,.pane-header.tabs');
                  const b=[...group.querySelectorAll('.tab')].find(t=>t.classList.contains('on'));
                  return {active:b.textContent.trim(),focus:document.activeElement.matches('.tab')};
                """,args=[],sandbox=None)
                self.assertEqual(keyboard["active"],before["inactive"])
                self.assertTrue(keyboard["focus"])

    def check_switch_geometry(self, command, root, profile):
        for module in ("dnb", "crop"):
            html = (root / f"modules/{module}/ui/index.html").read_text()
            markup = re.sub(r"<script\b[^>]*>.*?</script>|<link\b[^>]*>", "", html, flags=re.S)
            page = profile / f"{module}-switches.html"
            page.write_text(markup.replace("</head>", "<style>" + stylesheet().decode() + "</style></head>"))
            command("WebDriver:Navigate", url=page.as_uri())
            if module == "crop":
                rows = "\n".join(re.findall(r'<div class="row toggle-row">.*?</div>', html))
                command("WebDriver:ExecuteScript", script="""
                  document.getElementById('xAdv').innerHTML=arguments[0];
                """, args=[rows], sandbox=None)
                helper = "function slider(" + html.split("function slider(", 1)[1].split("(function buildAdvanced()", 1)[0]
                calls = "\n".join(re.findall(r"^\s*slider\(root,.*$", html, re.M))
                command("WebDriver:ExecuteScript", script="const SL={},root=document.getElementById('xAdv');"
                        + helper + calls, args=[], sandbox=None)
            for theme in ("photocraft", "dark"):
                for width in (1800, 980, 480):
                    command("WebDriver:SetWindowRect", width=width, height=800)
                    result = command("WebDriver:ExecuteScript", script="""
                      document.documentElement.dataset.theme=arguments[0];
                      for(const details of document.querySelectorAll('aside details')) {
                        details.hidden=false;details.open=true;
                      }
                      document.getElementById('optsLayer')?.removeAttribute('hidden');
                      return [...document.querySelectorAll('input[type=checkbox]')].map(input=>{
                        const s=getComputedStyle(input),r=input.getBoundingClientRect();
                        return {id:input.id,width:r.width,height:r.height,
                          transform:s.transform,
                          trackWidth:r.width-parseFloat(s.borderLeftWidth)-parseFloat(s.borderRightWidth),
                          trackHeight:r.height-parseFloat(s.borderTopWidth)-parseFloat(s.borderBottomWidth),
                          thumb:s.backgroundSize,clip:s.backgroundClip,origin:s.backgroundOrigin,
                          border:s.borderTopColor,left:r.left,right:r.right,viewport:innerWidth};
                      });
                    """, args=[theme], sandbox=None)
                    self.assertTrue(result)
                    sliders = command("WebDriver:ExecuteScript", script="""
                      return [...document.querySelectorAll('#xAdv input[type=range]')].map(e=>{
                        const s=getComputedStyle(e),t=getComputedStyle(e,'::-moz-range-thumb');
                        const r=e.getBoundingClientRect();
                        return {height:s.height,transform:s.transform,thumb:t.width,
                          border:t.borderLeftWidth,left:r.left,right:r.right,viewport:innerWidth};
                      });
                    """, args=[], sandbox=None)
                    for slider in sliders:
                        self.assertEqual(slider["height"], "18px")
                        self.assertEqual(slider["transform"], "matrix(1, 0, 0, 1, 0, 1)")
                        self.assertEqual(slider["thumb"], "18px")
                        self.assertEqual(slider["border"], "3px")
                        self.assertGreaterEqual(slider["left"], 0)
                        self.assertLessEqual(slider["right"], slider["viewport"])
                    for switch in result:
                        with self.subTest(module=module, theme=theme, width=width, switch=switch["id"]):
                            self.assertEqual((switch["width"], switch["height"]), (32, 24))
                            self.assertEqual((switch["trackWidth"], switch["trackHeight"]), (28, 16))
                            self.assertEqual(switch["thumb"], "12px 12px")
                            self.assertEqual(switch["clip"], "padding-box")
                            self.assertEqual(switch["origin"], "padding-box")
                            self.assertEqual(switch["border"], "rgba(0, 0, 0, 0)")
                            self.assertGreaterEqual(switch["left"], 0)
                            self.assertLessEqual(switch["right"], switch["viewport"])
                    if module=="dnb":
                        moved=command("WebDriver:ExecuteScript",script="""
                          return {row:getComputedStyle(document.getElementById('blurMask')).transform,
                            summary:getComputedStyle(document.getElementById('maskOn')).transform};
                        """,args=[],sandbox=None)
                        self.assertEqual(moved,{"row":"matrix(1, 0, 0, 1, 0, -1)","summary":"none"})
            selector = "#maskOn" if module == "dnb" else "#slora"
            command("WebDriver:ExecuteScript", script="""
              window.switchUnderTest=document.querySelector(arguments[0]);
              switchUnderTest.checked=false;switchUnderTest.disabled=false;
              switchUnderTest.style.transition='none';
              switchUnderTest.scrollIntoView({block:'center'});
            """, args=[selector], sandbox=None)
            element = command("WebDriver:FindElement", using="css selector", value=selector)
            element_id = element["element-6066-11e4-a52e-4f735466cecf"]
            command("WebDriver:ElementSendKeys", id=element_id, text=" ")
            focus = command("WebDriver:ExecuteScript", script="""
              const s=getComputedStyle(switchUnderTest);
              return {checked:switchUnderTest.checked,focus:switchUnderTest.matches(':focus-visible'),
                outline:s.outlineStyle,position:s.backgroundPosition};
            """, args=[], sandbox=None)
            self.assertTrue(focus["checked"])
            self.assertTrue(focus["focus"])
            self.assertEqual(focus["outline"], "solid")
            self.assertIn("100%", focus["position"])
            # Click the transparent edge outside the painted track: hitbox must remain usable.
            command("WebDriver:PerformActions", actions=[{"type": "pointer", "id": "switch-pointer",
                "parameters": {"pointerType": "mouse"}, "actions": [
                    {"type": "pointerMove", "origin": element, "x": 0, "y": -10},
                    {"type": "pointerDown", "button": 0}, {"type": "pointerUp", "button": 0}]}])
            self.assertFalse(command("WebDriver:ExecuteScript", script="return switchUnderTest.checked;",
                                     args=[], sandbox=None))
            command("WebDriver:ExecuteScript", script="switchUnderTest.disabled=true;", args=[], sandbox=None)
            command("WebDriver:ElementClick", id=element_id)
            disabled = command("WebDriver:ExecuteScript", script="""
              const s=getComputedStyle(switchUnderTest);
              return {checked:switchUnderTest.checked,opacity:s.opacity,cursor:s.cursor};
            """, args=[], sandbox=None)
            self.assertEqual(disabled, {"checked": False, "opacity": "0.5", "cursor": "default"})

    def check_crop_setting_queue(self, command, root):
        html=(root / "modules/crop/ui/index.html").read_text()
        helper="let queuedSettings"+html.split("let queuedSettings",1)[1].split("SETKEYS.forEach(k => { const e",1)[0]
        result=command("WebDriver:ExecuteAsyncScript",script="""
          const done=arguments[arguments.length-1];
          const X={running:true,settingsSaving:false},busy=false;
          let cropSliderDrag=null;
          let timer,statusRequest=0;
          const calls=[],setTimeout=fn=>{timer=fn;},clearTimeout=()=>{};
          const xInfo=()=>{},xButtons=()=>{},updateStar=()=>{},setStatus=()=>{};
          let fail=false;
          const api=async(path,body)=>{calls.push(body);if(fail)throw Error('synthetic failure');};
          const loadStatus=async()=>{};
        """+helper+"""
          (async()=>{
            await saveSettings({lora:false});await saveSettings({lora:true,seam_correction:false});
            const queued=calls.length===0;
            X.running=false;timer();await new Promise(resolve=>window.setTimeout(resolve,0));
            const flushed=calls.length===1 && calls[0].values.lora===true && calls[0].values.seam_correction===false;
            cropSliderDrag=5;
            await saveSettings({steps:22});await saveSettings({steps:23});
            const held=calls.length===1;
            cropSliderDrag=null;const dragValues=queuedSettings;queuedSettings={};await saveSettings(dragValues);
            const release=calls.length===2 && calls[1].values.steps===23;
            fail=true;await saveSettings({lora:false});
            done({queued,flushed,held,release,restored:!X.settingsSaving});
          })().catch(e=>done({error:String(e)}));
        """,args=[],sandbox=None)
        self.assertEqual(result,{"queued":True,"flushed":True,"held":True,"release":True,"restored":True})

    def check_combine_controls(self, command, html, profile):
        markup=re.sub(r"<script\b[^>]*>.*?</script>|<link\b[^>]*>","",html,flags=re.S)
        page=profile/"combine-controls.html"
        page.write_text(markup.replace("</head>","<style>"+stylesheet().decode()+"</style></head>"))
        command("WebDriver:Navigate",url=page.as_uri())
        mode="function rightPreviewKinds(mode) {"+html.split("function rightPreviewKinds(mode) {",1)[1].split("function syncMode()",1)[0]
        mode+="function syncMode() {"+html.split("function syncMode() {",1)[1].split("[['A', $('tabsA')]",1)[0]
        command("WebDriver:ExecuteScript",script="""
          const $=id=>document.getElementById(id);
          const state={mode:'combine',sourceKind:'photocraft',loaded:true,busy:false,frequency:{method:'gaussian'}};
          const tabSel={A:'result',B:'color'};
          const uiMode=m=>m,syncFrequencyControls=()=>{},syncDirectionalControls=()=>{},syncTransfer=()=>{},
            renderAnalysisSummary=()=>{},drawSkintone=()=>{};
        """+mode+"""
          window.combineMode=(mode,source,busy=false)=>{
            state.mode=mode;state.sourceKind=source;state.busy=busy;syncMode();
          };
        """,args=[],sandbox=None)
        for mode_name in ("combine","frequency","dnb","mask"):
            for source in ("photocraft","local"):
                command("WebDriver:ExecuteScript",script="combineMode(arguments[0],arguments[1]);",
                        args=[mode_name,source],sandbox=None)
                result=command("WebDriver:ExecuteScript",script="""
                  const visible=id=>!document.getElementById(id).hidden;
                  return {layers:visible('pcSendLayers'),combine:visible('g-combine'),
                    fs:visible('g-frequency'),dnb:visible('g-bands'),source:visible('g-file'),
                    processing:['g-blur','g-directional','g-skintone','g-band2','g-dnb-shared',
                      'g-mask','g-structure','g-range','g-color'].filter(visible),
                    tabs:[...document.querySelectorAll('#tabsB .tab')].filter(t=>!t.hidden).map(t=>t.dataset.k),
                    result:document.querySelector('#tabsA [data-k=result]').textContent,
                    directionalStatus:visible('directionalStatus'),
                    headings:['g-bands','g-band2','g-dnb-shared'].map(id=>
                      document.querySelector('#'+id+' > summary').textContent.trim()),
                    bandLabels:['layer','layerm','layer2','layer2m'].map(key=>
                      document.querySelector('#tabsB [data-k="'+key+'"]').textContent.trim())};
                """,args=[],sandbox=None)
                self.assertEqual(result["layers"],source=="photocraft" and mode_name!="mask")
                self.assertEqual(result["combine"],mode_name=="combine")
                self.assertEqual(result["fs"],mode_name=="frequency")
                self.assertEqual(result["dnb"],mode_name=="dnb")
                self.assertEqual(result["source"],mode_name=="mask")
                self.assertFalse(result["directionalStatus"])
                self.assertEqual(result["headings"],["Band 1","Band 2","shared controls"])
                self.assertEqual(result["bandLabels"],["Band 1","Band 1 + mask","Band 2","Band 2 + mask"])
                if mode_name=="combine":
                    self.assertEqual(result["result"],"Combined")
                    self.assertFalse(result["source"])
                    self.assertEqual(result["processing"],[])
                    self.assertEqual(result["tabs"],["fs_result","dnb_result","mask"])
        command("WebDriver:ExecuteScript",script="""
          combineMode('combine','photocraft');
          document.getElementById('pcGet').disabled=false;
          document.getElementById('pcSend').disabled=false;
          document.getElementById('presetSel').innerHTML='<option>Default</option>';
          window.clickedControls=[];
          for(const id of ['pcGet','pcSend','pcSendLayers','presetSave','presetNew','presetRemove']){
            document.getElementById(id).disabled=false;
            document.getElementById(id).onclick=()=>clickedControls.push(id);
          }
        """,args=[],sandbox=None)
        for theme in ("photocraft","dark"):
            for width in (1800,1400,980,640,480,360):
                command("WebDriver:SetWindowRect",width=width,height=800)
                result=command("WebDriver:ExecuteScript",script="""
                  document.documentElement.dataset.theme=arguments[0];
                  const ids=['pcGet','pcSend','pcSendLayers','presetSel','presetSave','presetNew','presetRemove'];
                  const rects=ids.map(id=>document.getElementById(id).getBoundingClientRect());
                  return {widths:rects.slice(0,3).map(r=>r.width),
                    bounds:rects.every(r=>r.left>=0&&r.right<=innerWidth),
                    overlap:rects.some((a,i)=>rects.slice(i+1).some(b=>
                      a.left<b.right&&a.right>b.left&&a.top<b.bottom&&a.bottom>b.top)),
                    overflow:document.documentElement.scrollWidth>innerWidth};
                """,args=[theme],sandbox=None)
                with self.subTest(theme=theme,width=width):
                    self.assertTrue(result["bounds"])
                    self.assertFalse(result["overlap"])
                    self.assertFalse(result["overflow"])
                    self.assertAlmostEqual(result["widths"][0],result["widths"][2],delta=1)
        for selector in ("#pcGet","#pcSend","#pcSendLayers","#presetSave","#presetNew","#presetRemove"):
            element=command("WebDriver:FindElement",using="css selector",value=selector)
            element_id=element["element-6066-11e4-a52e-4f735466cecf"]
            command("WebDriver:ElementClick",id=element_id)
        self.assertEqual(command("WebDriver:ExecuteScript",script="return clickedControls;",args=[],sandbox=None),
                         ["pcGet","pcSend","pcSendLayers","presetSave","presetNew","presetRemove"])

    def check_slider_release(self, command, html, profile):
        page=profile/"slider-release.html"
        page.write_text('<aside><input id="ordinary" type="range" value="30"><div class="directional-range"><input id="dual" type="range" value="30"></div></aside><button id="apply"></button><select id="presetSel"></select>')
        command("WebDriver:Navigate",url=page.as_uri())
        helper="let sliderDrag"+html.split("let sliderDrag",1)[1].split("const sliders =",1)[0]
        command("WebDriver:ExecuteScript",script="""
          const $=id=>document.getElementById(id),state={loaded:true,busy:false};
          let settingsRevision=0,requests=0;
          const syncPanelAvailability=()=>{},invalidateMaskCacheStatus=()=>{},renderAnalysisSummary=()=>{},drawProfile=()=>{};
          const apply=()=>{requests++;};
        """+helper+"""
          document.querySelectorAll('input').forEach(e=>e.oninput=markDirty);
          window.releaseTest={requests:()=>requests,reset:()=>{requests=0;},dirty:()=>state.dirty};
        """,args=[],sandbox=None)
        slider="function slider("+html.split("function slider(",1)[1].split("let sliderDrag",1)[0]
        mixes="const combineSliders="+html.split("const combineSliders=",1)[1].split("const directionalSegments",1)[0]
        command("WebDriver:ExecuteScript",script="""
          const $=id=>document.getElementById(id),state={combine:{fs_mix:100,dnb_mix:100}};
          const markDirty=()=>releaseTest.markMix();
          const parent=document.createElement('div');parent.id='combineSliders';document.querySelector('aside').append(parent);
          releaseTest.markMix=()=>document.getElementById('ordinary').dispatchEvent(new Event('input'));
        """+slider+mixes+"""
          Object.entries(combineSliders).forEach(([key,s])=>s.rng.id='mix-'+key);
          releaseTest.mixValues=()=>({...state.combine});
        """,args=[],sandbox=None)
        for selector in ("#ordinary","#dual","#mix-fs_mix","#mix-dnb_mix"):
            element=command("WebDriver:FindElement",using="css selector",value=selector)
            command("WebDriver:ExecuteScript",script="releaseTest.reset();",args=[],sandbox=None)
            command("WebDriver:PerformActions",actions=[{"type":"pointer","id":"held-slider",
                "parameters":{"pointerType":"mouse"},"actions":[
                    {"type":"pointerMove","origin":element,"x":0,"y":0},
                    {"type":"pointerDown","button":0},
                    {"type":"pointerMove","origin":element,"x":20,"y":0,"duration":100},
                    {"type":"pause","duration":700}]}])
            if selector.startswith("#mix-"):
                values=command("WebDriver:ExecuteScript",script="return releaseTest.mixValues();",args=[],sandbox=None)
                key=selector.removeprefix("#mix-")
                self.assertLess(values[key],100)
            self.assertEqual(command("WebDriver:ExecuteScript",script="return releaseTest.requests();",args=[],sandbox=None),0)
            command("WebDriver:PerformActions",actions=[{"type":"pointer","id":"held-slider","parameters":{"pointerType":"mouse"},
                "actions":[{"type":"pointerUp","button":0},{"type":"pause","duration":100}]}])
            self.assertEqual(command("WebDriver:ExecuteScript",script="return releaseTest.requests();",args=[],sandbox=None),1)
            command("WebDriver:ElementSendKeys",id=element["element-6066-11e4-a52e-4f735466cecf"],text="\ue014")
            command("WebDriver:ExecuteAsyncScript",script="window.setTimeout(arguments[arguments.length-1],450);",args=[],sandbox=None)
            self.assertEqual(command("WebDriver:ExecuteScript",script="return releaseTest.requests();",args=[],sandbox=None),2)
        for ending in ("pointercancel","lostpointercapture","blur"):
            command("WebDriver:ExecuteScript",script="""
              releaseTest.reset();
              document.getElementById('ordinary').dispatchEvent(new PointerEvent('pointerdown',{bubbles:true,pointerId:9}));
              document.getElementById('ordinary').dispatchEvent(new Event('input',{bubbles:true}));
              if(arguments[0]==='blur')window.dispatchEvent(new Event('blur'));
              else document.dispatchEvent(new PointerEvent(arguments[0],{pointerId:9}));
            """,args=[ending],sandbox=None)
            command("WebDriver:ExecuteAsyncScript",script="window.setTimeout(arguments[arguments.length-1],50);",args=[],sandbox=None)
            self.assertEqual(command("WebDriver:ExecuteScript",script="return releaseTest.requests();",args=[],sandbox=None),1)

    def check_panel_availability(self, command, html, profile):
        page=profile / "panel-availability.html"
        markup=re.sub(r"<script\b[^>]*>.*?</script>|<link\b[^>]*>", "", html, flags=re.S)
        page.write_text(markup.replace("</head>", "<style>"+stylesheet().decode()+"</style></head>"))
        command("WebDriver:Navigate",url=page.as_uri())
        helper="let panelOperationBusy"+html.split("let panelOperationBusy",1)[1].split("function previewFeedback",1)[0]
        command("WebDriver:ExecuteScript",script="""
          const $=id=>document.getElementById(id),state={loaded:true,sourceKind:'local'};
          const statusOk=false,pendingShellMode=null;
          for(const panel of document.querySelectorAll('aside details.grp')){
            panel.hidden=false;panel.open=true;
            const toggle=panel.querySelector(':scope > summary input[type=checkbox]');
            if(toggle){toggle.disabled=false;toggle.checked=true;}
            const slider=document.createElement('input');slider.type='range';slider.className='availability-test';
            const button=document.createElement('button');button.textContent='test';button.className='availability-test';
            panel.querySelector('.gbody').append(slider,button);
          }
        """+helper+"""
          syncPanelAvailability();
          window.panelTest={state,busy:setBusy,mask:value=>{previewMaskBusy=value;syncPanelAvailability();},sync:syncPanelAvailability};
        """,args=[],sandbox=None)
        result=command("WebDriver:ExecuteScript",script="""
          const samples=[...document.querySelectorAll('.availability-test')];
          panelTest.busy(true);
          const busy=samples.every(e=>e.disabled);
          const toggles=[...document.querySelectorAll('aside details.grp > summary input[type=checkbox]')];
          const exempt=toggles.every(e=>!e.disabled);
          panelTest.busy(false);
          const restored=samples.every(e=>!e.disabled);
          panelTest.busy(true,false);
          const fast=samples.every(e=>!e.disabled);
          panelTest.mask(true);
          const mask=samples.every(e=>e.disabled);
          const stillExempt=toggles.every(e=>!e.disabled);
          panelTest.mask(false);panelTest.busy(false);
          document.querySelector('#g-mask .availability-test').disabled=true;
          panelTest.busy(true);panelTest.busy(false);
          const intrinsic=document.querySelector('#g-mask .availability-test').disabled;
          document.getElementById('toneOn').checked=false;panelTest.sync();
          const off=[...document.querySelectorAll('#g-range .availability-test')].every(e=>e.disabled);
          const picker=document.getElementById('toneSwatch').disabled;
          document.getElementById('toneOn').checked=true;panelTest.sync();
          const on=[...document.querySelectorAll('#g-range .availability-test')].every(e=>!e.disabled);
          return {busy,exempt,restored,fast,mask,stillExempt,intrinsic,off,picker,on};
        """,args=[],sandbox=None)
        self.assertTrue(all(result.values()),result)

    def check_preview_images(self, command, html, profile):
        markup = re.sub(r"<script\b[^>]*>.*?</script>|<link\b[^>]*>", "", html, flags=re.S)
        page = profile / "preview-images.html"
        page.write_text(markup.replace("</head>", "<style>" + stylesheet().decode() + "</style></head>"))
        for kind, color in (("result", "red"), ("directional_region", "blue"), ("orig", "green"), ("mask", "white")):
            (profile / f"{kind}.svg").write_text(
                f'<svg xmlns="http://www.w3.org/2000/svg" width="2" height="2"><path fill="{color}" d="M0 0h2v2H0z"/></svg>')
        command("WebDriver:Navigate", url=page.as_uri())
        helper = "function showImages() {" + html.split("function showImages() {", 1)[1].split("function layout()", 1)[0]
        command("WebDriver:ExecuteScript", script="""
          const state={version:1,avers:0},landmarkEdit=null;
          const pans=[document.getElementById('p0'),document.getElementById('p1')];
          let selection=['result','directional_region'];
          const paneKinds=()=>selection,layout=()=>{},setStatus=()=>{};
          const nativeSrc=Object.getOwnPropertyDescriptor(HTMLImageElement.prototype,'src');
          // Map only test requests to disposable local images; no Tools/API requests.
          Object.defineProperty(HTMLImageElement.prototype,'src',{
            get:nativeSrc.get,
            set(value){nativeSrc.set.call(this,value.replace(/^img\\/(\\w+)\\?.*$/,'$1.svg'));}});
        """ + helper + """
          window.previewTest={pans,showImages,select:kinds=>{selection=kinds;}};
        """, args=[], sandbox=None)
        def load():
            return command("WebDriver:ExecuteAsyncScript", script="""
              const done=arguments[arguments.length-1];
              previewTest.showImages().then(done);
            """, args=[], sandbox=None)
        self.assertTrue(load())
        self.assertTrue(command("WebDriver:ExecuteScript", script="""
          return previewTest.pans.every(p=>!p.querySelector('img').hidden && p.querySelector('img').naturalWidth===2);
        """, args=[], sandbox=None))
        pending = command("WebDriver:ExecuteScript", script="""
          previewTest.select(['orig','mask']);
          const previous=previewTest.pans.map(p=>p.querySelector('img').src);
          previewTest.showImages();
          return {previous,hidden:previewTest.pans.map(p=>p.querySelector('img').hidden)};
        """, args=[], sandbox=None)
        self.assertIn("result.svg", pending["previous"][0])
        self.assertIn("directional_region.svg", pending["previous"][1])
        self.assertEqual(pending["hidden"], [True, True])
        self.assertTrue(load())
        result = command("WebDriver:ExecuteScript", script="""
          previewTest.select(['orig','directional_region']);previewTest.showImages();
          const img=previewTest.pans[1].querySelector('img'),oldLoad=img.onload,oldError=img.onerror;
          previewTest.select(['orig','mask']);previewTest.showImages();
          oldLoad();oldError();
          return {hidden:img.hidden,src:img.src};
        """, args=[], sandbox=None)
        self.assertTrue(result["hidden"])
        self.assertIn("mask.svg", result["src"])
        self.assertTrue(load())

    def check_analysis_controls(self, command, html, profile):
        # Run only control initialization and mocked overlay requests, never the application.
        script = re.findall(r"<script>(.*?)</script>", html, re.S)[0]
        markup = re.sub(r"<script\b[^>]*>.*?</script>|<link\b[^>]*>", "", html, flags=re.S)
        page = profile / "analysis-controls.html"
        page.write_text(markup.replace("</head>", "<style>" + stylesheet().decode() + "</style></head>"))
        command("WebDriver:Navigate", url=page.as_uri())
        command("WebDriver:ExecuteScript", script="""
          new Function(arguments[0]);
          localStorage.removeItem('dnb-anDetect');
          localStorage.setItem('dnb-anSkin','0');
          localStorage.removeItem('dnb-anStruct');
        """, args=[script], sandbox=None)
        initialization = script.split("let shellConnected", 1)[0]
        colors = "\n".join(re.findall(r"^\s*sliders\.color\w+ = slider\(.*$", script, re.M))
        overlays = "function analysisFlags()" + script.split("function analysisFlags()", 1)[1].split("function params()", 1)[0]
        parameters = "function params()" + script.split("function params()", 1)[1].split("\nasync function", 1)[0]
        bands = re.search(r"^function bandsPayload\(\).*$", script, re.M)[0]
        test_script = initialization + colors + overlays + bands + parameters + """
          const calls=[];
          api=async (path,body)=>{
            calls.push({path,body});
            if(window.rejectAnalysis)throw new Error('synthetic overlay failure');
            return {avers:42};
          };
          let displayed=0;
          const showImages=()=>{displayed++;};
          window.analysisTest={state,calls,flags:analysisFlags,params,displayed:()=>displayed,
            enableRegion(){markDirty=()=>{};state.directional.on=true;syncDirectionalRange();
              $('g-directional').hidden=false;$('g-directional').open=true;$('directionalSliders').hidden=false;},
            resetRegion(){state.directional.start=20;state.directional.end=100;syncDirectionalRange();},
            setControl(key,value) {
              markDirty=()=>{};
              const input=sliders[key].rng;input.value=value;input.dispatchEvent(new Event('input'));
            }};
          $('optsAn').hidden=false;
        """
        command("WebDriver:ExecuteScript", script=test_script, args=[], sandbox=None)

        def evaluate(script):
            return command("WebDriver:ExecuteScript", script=script, args=[], sandbox=None)

        self.assertEqual(evaluate("return analysisTest.flags();"),
                         {"detect": True, "skin": False, "structure": True})
        evaluate("analysisTest.enableRegion();")
        handles = evaluate("""
          return ['directionalStart','directionalEnd'].map(id=>{
            const e=document.getElementById(id),r=e.getBoundingClientRect();
            return {type:e.type,left:r.left,right:r.right,value:e.value,label:e.getAttribute('aria-label')};
          });
        """)
        self.assertEqual([h["type"] for h in handles], ["range", "range"])
        self.assertEqual([h["value"] for h in handles], ["0", "100"])
        self.assertEqual(handles[0]["left"], handles[1]["left"])
        self.assertEqual(handles[0]["right"], handles[1]["right"])
        element = command("WebDriver:FindElement", using="css selector", value="#directionalStart")
        command("WebDriver:ElementSendKeys", id=element["element-6066-11e4-a52e-4f735466cecf"], text="\ue014")
        self.assertEqual(evaluate("return analysisTest.state.directional.start;"), 1)
        bounds = evaluate("""
          const a=document.getElementById('directionalStart'),b=document.getElementById('directionalEnd');
          a.value=200;a.dispatchEvent(new Event('input'));
          b.value=0;b.dispatchEvent(new Event('input'));
          return [analysisTest.state.directional.start,analysisTest.state.directional.end];
        """)
        self.assertEqual(bounds, [99, 100])
        self.check_slider_handles(command)
        result = evaluate("""
          return {
            labels:[...document.querySelectorAll('.gbody label,#toolModeRow label,.gbody option,#toolModeRow option,#view option')]
              .map(e=>e.textContent.trim()).filter(Boolean),
            color:[...document.querySelectorAll('#colorSliders label')].map(e=>({text:e.textContent,tip:e.title})),
            toggles:[...document.querySelectorAll('#optsAn button')].map(e=>({id:e.id,type:e.type,pressed:e.getAttribute('aria-pressed'),name:e.textContent.trim()})),
            inputs:document.querySelectorAll('#optsAn input').length
          };
        """)
        for label in result["labels"]:
            self.assertEqual(label, label.lower())
        self.assertEqual(result["inputs"], 0)
        self.assertEqual([item["id"] for item in result["toggles"]], ["anDetect", "anSkin", "anStruct"])
        for item in result["toggles"]:
            self.assertEqual(item["type"], "button")
            self.assertTrue(item["name"])
            self.assertIn(item["pressed"], ("true", "false"))
        self.assertIn("sat range", [item["text"] for item in result["color"]])
        for item in result["color"]:
            self.assertNotIn("saturation", item["text"])
            if item["text"] in ("feather", "smoothing"):
                self.assertIn("mm on the face", item["tip"])
        self.assertEqual(evaluate("return analysisTest.params().color;"),
                         {"on": False, "hue": None, "saturation": None, "hue_range": 30,
                          "saturation_range": .25, "blur_mm": 1, "smooth_mm": .8, "strength": 1})
        result = evaluate("""
          for(const [key,value] of [['colorBlur',2.3],['colorSmooth',1.5],['colorSaturation',.42]]) {
            analysisTest.setControl(key,value);
          }
          return analysisTest.params().color;
        """)
        self.assertEqual(result["blur_mm"], 2.3)
        self.assertEqual(result["smooth_mm"], 1.5)
        self.assertEqual(result["saturation_range"], .42)
        for width in (1800, 980, 480):
            command("WebDriver:SetWindowRect", width=width, height=800)
            result = evaluate("""
              const buttons=[...document.querySelectorAll('#optsAn button')];
              const active=getComputedStyle(document.getElementById('anDetect'));
              const off=getComputedStyle(document.getElementById('anSkin'));
              return {font:parseFloat(active.fontSize),regular:parseFloat(getComputedStyle(document.body).fontSize),
                active:active.color,off:off.color,background:active.backgroundColor,border:active.borderTopWidth,
                rects:buttons.map(e=>{const r=e.getBoundingClientRect();return {left:r.left,right:r.right,height:r.height};}),
                viewport:innerWidth};
            """)
            with self.subTest(analysis_width=width):
                self.assertLess(result["font"], result["regular"])
                self.assertNotEqual(result["active"], result["off"])
                self.assertEqual(result["background"], "rgba(0, 0, 0, 0)")
                self.assertEqual(result["border"], "0px")
                for rect in result["rects"]:
                    self.assertGreaterEqual(rect["left"], 0)
                    self.assertLessEqual(rect["right"], result["viewport"])
                    self.assertGreaterEqual(rect["height"], 24)
        for theme in ("photocraft", "dark"):
            evaluate(f"document.documentElement.dataset.theme='{theme}';")
            for selector in ("#anDetect", "#anSkin"):
                evaluate(f"document.querySelector('{selector}').scrollIntoView({{block:'center'}});")
                element = command("WebDriver:FindElement", using="css selector", value=selector)
                command("WebDriver:PerformActions", actions=[{"type": "pointer", "id": "overlay-pointer",
                    "parameters": {"pointerType": "mouse"}, "actions": [
                        {"type": "pointerMove", "origin": element, "x": 0, "y": 0}]}])
                style = command("WebDriver:ExecuteScript", script="""
                  const e=document.querySelector(arguments[0]),s=getComputedStyle(e);
                  const sample=document.createElement('span');sample.style.color='var(--dim)';
                  document.body.append(sample);
                  const dim=getComputedStyle(sample).color;sample.remove();
                  return {hover:e.matches(':hover'),filter:s.filter,decoration:s.textDecorationLine,
                    color:s.color,dim,background:getComputedStyle(document.body).backgroundColor};
                """, args=[selector], sandbox=None)
                self.assertTrue(style["hover"])
                self.assertEqual(style["filter"], "brightness(1.12)")
                self.assertEqual(style["decoration"], "none")
                if selector == "#anSkin":
                    def channels(value):
                        rgb = [float(v) for v in re.findall(r"[\d.]+", value)[:3]]
                        return [c*255 for c in rgb] if value.startswith("color(srgb ") else rgb
                    self.assertLess(sum(channels(style["color"])), sum(channels(style["dim"])))
                    def luminance(value):
                        rgb = [c/255 for c in channels(value)]
                        rgb = [c/12.92 if c <= .04045 else ((c+.055)/1.055)**2.4 for c in rgb]
                        return sum(c*w for c,w in zip(rgb, (.2126,.7152,.0722)))
                    foreground, background = luminance(style["color"]), luminance(style["background"])
                    self.assertGreaterEqual((foreground+.05)/(background+.05), 4.5)
        # Native buttons provide real keyboard activation, not custom key event emulation.
        evaluate("analysisTest.state.version=1;")
        element = command("WebDriver:FindElement", using="css selector", value="#anSkin")
        element_id = element["element-6066-11e4-a52e-4f735466cecf"]
        command("WebDriver:ElementSendKeys", id=element_id, text="\ue007")
        self.assertTrue(evaluate("return analysisTest.flags().skin;"))
        command("WebDriver:ElementSendKeys", id=element_id, text=" ")
        self.assertFalse(evaluate("return analysisTest.flags().skin;"))
        element = command("WebDriver:FindElement", using="css selector", value="#anDetect")
        command("WebDriver:ElementClick", id=element["element-6066-11e4-a52e-4f735466cecf"])
        result = evaluate("""
          return {flags:analysisTest.flags(),calls:analysisTest.calls,avers:analysisTest.state.avers,
            displayed:analysisTest.displayed(),saved:localStorage.getItem('dnb-anDetect')};
        """)
        self.assertEqual(result["flags"], {"detect": False, "skin": False, "structure": True})
        self.assertEqual(result["saved"], "0")
        self.assertEqual(result["avers"], 42)
        self.assertEqual(result["displayed"], 3)
        self.assertEqual(len(result["calls"]), 3)
        self.assertEqual(result["calls"][-1], {"path": "/api/analysis", "body": {"analysis": result["flags"]}})
        evaluate("analysisTest.state.busy=true;document.getElementById('anStruct').click();")
        self.assertEqual(evaluate("return analysisTest.calls.length;"), 3)
        self.assertFalse(evaluate("return analysisTest.flags().structure;"))
        command("WebDriver:ExecuteAsyncScript", script="""
          const done=arguments[arguments.length-1];
          analysisTest.state.busy=false;window.rejectAnalysis=true;
          document.getElementById('anStruct').onclick().then(done,done);
        """, args=[], sandbox=None)
        self.assertEqual(evaluate("return document.getElementById('status').textContent;"),
                         "synthetic overlay failure")
        self.assertEqual(evaluate("return document.getElementById('status').className;"), "err")
        command("WebDriver:Navigate", url=page.as_uri())
        command("WebDriver:ExecuteScript", script=test_script, args=[], sandbox=None)
        self.assertEqual(evaluate("return analysisTest.flags();"),
                         {"detect": False, "skin": False, "structure": True})

    def check_slider_handles(self, command):
        for theme in ("photocraft", "dark"):
            for width in (1800, 980, 480):
                command("WebDriver:SetWindowRect", width=width, height=800)
                geometry = command("WebDriver:ExecuteScript", script="""
                  document.documentElement.dataset.theme=arguments[0];
                  return [...document.querySelectorAll('input[type=range]')].map(e=>{
                    const s=getComputedStyle(e),thumb=getComputedStyle(e,'::-moz-range-thumb');
                    const dual=!!e.closest('.directional-range');
                    return {dual,height:parseFloat(s.height),transform:s.transform,
                      cursor:s.cursor,
                      width:parseFloat(thumb.width),thumbHeight:parseFloat(thumb.height),
                      border:parseFloat(thumb.borderLeftWidth),clip:thumb.backgroundClip};
                  });
                """, args=[theme], sandbox=None)
                for slider in geometry:
                    with self.subTest(slider_theme=theme, slider_width=width, dual=slider["dual"]):
                        self.assertEqual(slider["height"], 18)
                        self.assertEqual(slider["transform"], "matrix(1, 0, 0, 1, 0, 1)")
                        self.assertEqual(slider["cursor"], "pointer")
                        self.assertEqual(slider["clip"], "padding-box")
                        self.assertEqual(slider["width"], 18)
                        self.assertEqual(slider["thumbHeight"], 18)
                        self.assertEqual(slider["border"], 3)
        command("WebDriver:ExecuteScript", script="analysisTest.resetRegion();", args=[], sandbox=None)
        command("WebDriver:ExecuteScript", script="""
          analysisTest.state.directional.end=70;
          document.getElementById('directionalEnd').value=70;
        """, args=[], sandbox=None)
        for selector in ("#directionalStart", "#directionalEnd", "#frequencySliders input[type=range]"):
            element = command("WebDriver:FindElement", using="css selector", value=selector)
            position = command("WebDriver:ExecuteScript", script="""
              const e=document.querySelector(arguments[0]);e.disabled=false;
              const group=e.closest('details');group.hidden=false;group.open=true;
              e.scrollIntoView({block:'center'});
              const r=e.getBoundingClientRect();
              const size=18,ratio=(e.value-e.min)/(e.max-e.min);
              window.draggedSlider=e;
              return {x:r.left+size/2+ratio*(r.width-size),y:r.top+r.height/2,
                before:+e.value,max:+e.max};
            """, args=[selector], sandbox=None)
            self.assertEqual(command("WebDriver:ExecuteScript",script="""
              return getComputedStyle(draggedSlider).cursor;
            """,args=[],sandbox=None),"pointer")
            # Start on the circular thumb edge, rather than at its center.
            command("WebDriver:PerformActions", actions=[{"type":"pointer","id":"slider-drag",
                "parameters":{"pointerType":"mouse"},"actions":[
                    {"type":"pointerMove","origin":"viewport","x":round(position["x"]),"y":round(position["y"]-7)},
                    {"type":"pointerDown","button":0}]}])
            self.assertEqual(command("WebDriver:ExecuteScript",script="""
              return {active:draggedSlider.matches(':active'),
                cursor:getComputedStyle(draggedSlider).cursor};
            """,args=[],sandbox=None),{"active":True,"cursor":"grabbing"})
            command("WebDriver:PerformActions", actions=[{"type":"pointer","id":"slider-drag",
                "parameters":{"pointerType":"mouse"},"actions":[
                    {"type":"pointerMove","origin":"viewport","x":round(position["x"]+25),"y":round(position["y"]-7),"duration":100},
                    {"type":"pointerUp","button":0}]}])
            result = command("WebDriver:ExecuteScript", script="""
              return {value:+draggedSlider.value,focus:draggedSlider.matches(':focus-visible'),
                state:[analysisTest.state.directional.start,analysisTest.state.directional.end],
                cursor:getComputedStyle(draggedSlider).cursor};
            """, args=[], sandbox=None)
            self.assertGreater(result["value"], position["before"])
            self.assertEqual(result["cursor"],"pointer")
            self.assertLess(result["state"][0], result["state"][1])
            command("WebDriver:ElementSendKeys", id=element["element-6066-11e4-a52e-4f735466cecf"],
                    text="\ue004\ue008\ue004\ue000")
            command("WebDriver:ElementSendKeys", id=element["element-6066-11e4-a52e-4f735466cecf"], text="\ue014")
            focus = command("WebDriver:ExecuteScript", script="""
              return {value:+draggedSlider.value,focus:draggedSlider.matches(':focus-visible'),
                outline:getComputedStyle(draggedSlider).outlineStyle};
            """, args=[], sandbox=None)
            self.assertGreater(focus["value"], result["value"])
            self.assertTrue(focus["focus"])
            self.assertEqual(focus["outline"], "solid")
            command("WebDriver:ExecuteScript", script="draggedSlider.disabled=true;", args=[], sandbox=None)
            command("WebDriver:PerformActions", actions=[{"type":"pointer","id":"slider-drag",
                "parameters":{"pointerType":"mouse"},"actions":[
                    {"type":"pointerMove","origin":"viewport","x":round(position["x"]),"y":round(position["y"])},
                    {"type":"pointerDown","button":0},{"type":"pointerUp","button":0}]}])
            self.assertEqual(command("WebDriver:ExecuteScript", script="return +draggedSlider.value;",
                                     args=[], sandbox=None), focus["value"])
            self.assertEqual(command("WebDriver:ExecuteScript", script="return getComputedStyle(draggedSlider).opacity;",
                                     args=[], sandbox=None), "0.5")
            self.assertEqual(command("WebDriver:ExecuteScript",script="return getComputedStyle(draggedSlider).cursor;",
                                     args=[],sandbox=None),"default")
