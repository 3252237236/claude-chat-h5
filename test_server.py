import os, json
from http.server import HTTPServer, BaseHTTPRequestHandler

PORT = int(os.environ.get("PORT", 8765))

class H(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
    def do_POST(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

print(f"Starting on port {PORT}", flush=True)
HTTPServer(("0.0.0.0", PORT), H).serve_forever()
