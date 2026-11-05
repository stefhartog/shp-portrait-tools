"""ComfyUI lifecycle stub. No torch, models or inference."""

import argparse
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--port", type=int)
parser.add_argument("--listen")
parser.add_argument("--disable-auto-launch", action="store_true")
parser.add_argument("--user-directory")
parser.add_argument("--output-directory")
parser.add_argument("--input-directory")
parser.add_argument("--temp-directory")
args = parser.parse_args()
root = Path(__file__).parent
if (root / "fail").exists():
    raise SystemExit(7)
time.sleep(float((root / "delay").read_text()) if (root / "delay").exists() else 0)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        busy = (root / "busy").exists()
        body = json.dumps({"queue_running": [[1]] if busy else [], "queue_pending": []}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


ThreadingHTTPServer((args.listen, args.port), Handler).serve_forever()
