import os, sys
from http.server import HTTPServer, BaseHTTPRequestHandler

PORT = int(os.environ.get("PORT", 8765))

class H(BaseHTTPRequestHandler):
    def do_GET(self):
        print(f"RECEIVED: {self.command} {self.path}", flush=True)
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")

    def do_HEAD(self):
        print(f"RECEIVED HEAD: {self.path}", flush=True)
        self.send_response(200)
        self.end_headers()

    def log_message(self, format, *args):
        print(f"LOG: {format % args}", flush=True)

print(f"PORT={PORT}", flush=True)
print(f"Starting on 0.0.0.0:{PORT}", flush=True)
sys.stdout.flush()
HTTPServer(("0.0.0.0", PORT), H).serve_forever()
