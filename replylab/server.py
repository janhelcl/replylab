import json
import logging
import queue
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .models import Invalid
from .service import Conflict, Lab, Missing


WEB = Path(__file__).with_name("web")
ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}
IDENTIFIER = r"([a-f0-9]{32})"
SESSION = rf"/api/sessions/{IDENTIFIER}"
BRANCH = rf"{SESSION}/branches/{IDENTIFIER}"
MAX_BODY = 1024 * 1024


class Server(ThreadingHTTPServer):
    def __init__(self, address, lab):
        self.lab = lab
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(30)

    def log_message(self, format, *args):
        logging.getLogger(__name__).debug(format, *args)

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def _handle(self, method):
        port = self.server.server_port
        allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
        host = self.headers.get("Host")
        origin = self.headers.get("Origin")
        if host not in allowed or (origin is not None and origin != f"http://{host}"):
            self._json(403, {"error": "Only the local ReplyLab origin is allowed"})
            return
        try:
            path = urlsplit(self.path).path
            if method == "GET" and path in ASSETS:
                name, mime = ASSETS[path]
                self._response(200, (WEB / name).read_bytes(), mime)
                return
            if method == "GET" and (match := re.fullmatch(rf"{SESSION}/events", path)):
                self._events(*match.groups())
                return
            lab = self.server.lab
            if method == "GET":
                if path == "/api/sessions":
                    payload = lab.sessions()
                elif match := re.fullmatch(SESSION, path):
                    payload = lab.session(*match.groups())
                elif match := re.fullmatch(BRANCH, path):
                    payload = lab.branch(*match.groups())
                elif match := re.fullmatch(rf"{SESSION}/attempts/{IDENTIFIER}", path):
                    payload = lab.attempt(*match.groups())
                else:
                    raise Missing("Route not found")
                self._json(200, payload)
                return
            body = self._body()
            if path == "/api/sessions":
                self._json(201, lab.create(body))
                return
            if match := re.fullmatch(rf"{BRANCH}/(send|next|play|fork)", path):
                session_id, branch_id, action = match.groups()
                if action == "send":
                    payload = lab.send(session_id, branch_id, body)
                elif action == "fork":
                    payload = lab.fork(session_id, branch_id, body)
                else:
                    payload = lab.start(session_id, branch_id, body, action)
                self._json(201 if action == "fork" else 200, payload)
                return
            if match := re.fullmatch(rf"{SESSION}/runs/{IDENTIFIER}/stop", path):
                self._json(200, lab.stop(*match.groups()))
                return
            raise Missing("Route not found")
        except Invalid as error:
            self._json(400, {"error": str(error)})
        except Missing as error:
            self._json(404, {"error": str(error)})
        except Conflict as error:
            self._json(409, {"error": str(error), "state": error.state})
        except OSError:
            return

    def _body(self):
        if self.headers.get_content_type() != "application/json":
            raise Invalid("Send a JSON object")
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_BODY or self.headers.get("Transfer-Encoding"):
                raise ValueError
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, RecursionError):
            raise Invalid("Send a valid UTF-8 JSON object up to 1 MiB") from None
        if not isinstance(body, dict):
            raise Invalid("Send a JSON object")
        return body

    def _headers(self, status, mime):
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        self.send_header("Referrer-Policy", "no-referrer")

    def _response(self, status, data, mime):
        self._headers(status, mime)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json(self, status, data):
        self._response(status, json.dumps(data).encode("utf-8"), "application/json; charset=utf-8")

    def _events(self, session_id):
        lab = self.server.lab
        snapshot, mailbox = lab.subscribe(session_id)
        try:
            self._headers(200, "text/event-stream; charset=utf-8")
            self.end_headers()
            self._event(snapshot)
            while True:
                try:
                    event = mailbox.get(timeout=10)
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    continue
                if event is None:
                    return
                self._event(event)
        finally:
            lab.unsubscribe(session_id, mailbox)

    def _event(self, event):
        data = json.dumps(event).encode("utf-8")
        self.wfile.write(b"data: " + data + b"\n\n")
        self.wfile.flush()


def serve(port, database):
    lab = Lab(database)
    try:
        with Server(("127.0.0.1", port), lab) as server:
            print(f"ReplyLab is listening at http://127.0.0.1:{server.server_port}", flush=True)
            server.serve_forever()
    finally:
        lab.close()
