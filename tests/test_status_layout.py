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
