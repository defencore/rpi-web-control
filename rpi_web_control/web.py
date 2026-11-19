"""Authenticated HTTP API and bounded multipart JPEG transport."""

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import socket
import ssl
import threading
import time
from http import HTTPStatus
from http.cookies import SimpleCookie, CookieError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .application import Conflict
from .downloads import RecordingSourceChanged
from .firmware import FirmwareConflict
from .live import endpoint
from .mode_test import CameraBusy
from .network_transaction import NetworkConflict

LOG = logging.getLogger(__name__)
STATIC = Path(__file__).with_name("static")
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/static/style.css": ("style.css", "text/css; charset=utf-8"),
    "/static/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/static/ui.js": ("ui.js", "text/javascript; charset=utf-8"),
    "/static/device.js": ("device.js", "text/javascript; charset=utf-8"),
    "/static/network.js": ("network.js", "text/javascript; charset=utf-8"),
    "/static/wifi_station.js": ("wifi_station.js", "text/javascript; charset=utf-8"),
    "/static/storage.js": ("storage.js", "text/javascript; charset=utf-8"),
}
SESSION_SECONDS = 12 * 60 * 60


class RequestError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def byte_range(value, size):
    """Resolve a single HTTP byte range to inclusive offsets."""
    match = re.fullmatch(r"bytes=([0-9]{0,20})-([0-9]{0,20})", value)
    if not match or not size:
        return None
    first, last = match.groups()
    if not first:
        length = int(last or 0)
        return (max(0, size - length), size - 1) if length else None
    start = int(first)
    end = min(int(last), size - 1) if last else size - 1
    return (start, end) if start <= end else None


def recording_query(query, *, listing):
    allowed = {"source", "target_id"}
    if listing:
        allowed.add("cursor")
    params = parse_qs(query, strict_parsing=True, keep_blank_values=True, max_num_fields=len(allowed))
    if set(params) - allowed or any(len(values) != 1 for values in params.values()):
        raise ValueError("Invalid recording query parameters.")
    fields = {key: values[0] for key, values in params.items()}
    if "target_id" in fields and not re.fullmatch(r"[0-9a-f]{64}", fields["target_id"]):
        raise ValueError("Invalid recording target identifier.")
    return fields


class Sessions:
    def __init__(self, token):
        self.digest = hashlib.sha256(token.encode()).digest()
        self.lock = threading.Lock()
        self.sessions = {}
        self.failures = {}

    def matches(self, token):
        return hmac.compare_digest(self.digest, hashlib.sha256(token.encode()).digest())

    def login(self, address, token):
        now = time.monotonic()
        with self.lock:
            self.failures = {k: v for k, v in self.failures.items() if v[1] > now}
            attempts, until = self.failures.get(address, (0, now + 60))
            if attempts >= 10:
                raise RequestError(429, "Too many attempts. Try again in one minute.")
            if not self.matches(token):
                if len(self.failures) >= 1024 and address not in self.failures:
                    raise RequestError(429, "Too many attempts. Try again in one minute.")
                self.failures[address] = (attempts + 1, until)
                raise RequestError(401, "Invalid access token.")
            self.failures.pop(address, None)
            self.sessions = {k: v for k, v in self.sessions.items() if v > now}
            if len(self.sessions) >= 64:
                self.sessions.pop(next(iter(self.sessions)))
            session = secrets.token_urlsafe(32)
            self.sessions[session] = now + SESSION_SECONDS
            return session

    def valid(self, session):
        with self.lock:
            return self.sessions.get(session, 0) > time.monotonic()

    def logout(self, session):
        with self.lock:
            self.sessions.pop(session, None)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 32

    def __init__(self, address, application, token, tls=None, bind_and_activate=True):
        self.application = application
        self.auth = Sessions(token) if token is not None else None
        self.tls = tls
        self.stream_slots = threading.BoundedSemaphore(6)
        self.download_slots = threading.BoundedSemaphore(4)
        self.client_slots = threading.BoundedSemaphore(32)
        self.stopping = threading.Event()
        if ":" in address[0]:
            self.address_family = socket.AF_INET6
        super().__init__(address, Handler, bind_and_activate=bind_and_activate)

    def process_request(self, request, client_address):
        if not self.client_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.client_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            request.settimeout(10)
            if self.tls:
                with self.tls.wrap_socket(request, server_side=True) as connection:
                    super().process_request_thread(connection, client_address)
            else:
                super().process_request_thread(request, client_address)
        except (ssl.SSLError, ConnectionError, TimeoutError):
            request.close()
        finally:
            self.client_slots.release()


class Handler(BaseHTTPRequestHandler):
    server_version = "RpiWebControl"
    sys_version = ""

    def log_message(self, fmt, *args):
        # Operational events are logged by the application. Omit client input and polling.
        pass

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; object-src 'none'; frame-ancestors 'none'; "
                         "base-uri 'none'; form-action 'self'")
        super().end_headers()

    def reply(self, status, data, mime="application/json; charset=utf-8", cookie=None, filename=None):
        if not isinstance(data, bytes):
            data = json.dumps(data, separators=(",", ":"), allow_nan=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        if cookie is not None:
            flags = "; Secure" if self.server.tls else ""
            self.send_header("Set-Cookie", f"rpi_session={cookie}; HttpOnly; Path=/; "
                             f"SameSite=Strict; Max-Age={SESSION_SECONDS if cookie else 0}{flags}")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def cookie(self):
        try:
            cookie = SimpleCookie(self.headers.get("Cookie", "")).get("rpi_session")
            return cookie.value if cookie else ""
        except CookieError:
            return ""

    def authenticated(self):
        if self.server.auth is None:
            return True
        authorization = self.headers.get("Authorization", "")
        if authorization.startswith("Bearer "):
            return self.server.auth.matches(authorization[7:])
        return self.server.auth.valid(self.cookie())

    def read_object(self):
        if self.headers.get_content_type() != "application/json":
            raise RequestError(415, "Send an application/json object.")
        if self.headers.get("Transfer-Encoding"):
            raise RequestError(400, "Chunked requests are unsupported.")
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError as exc:
            raise RequestError(411, "Supply Content-Length.") from exc
        if not 0 < length <= 65536:
            raise RequestError(413, "Request size must be between 1 and 65536 bytes.")
        try:
            obj = json.loads(self.rfile.read(length))
        except (ValueError, RecursionError) as exc:
            raise RequestError(400, "Invalid JSON.") from exc
        if not isinstance(obj, dict):
            raise RequestError(400, "Send a JSON object.")
        return obj

    def check_origin(self):
        origin = self.headers.get("Origin")
        scheme = "https" if self.server.tls else "http"
        if origin is not None and origin != scheme + "://" + self.headers.get("Host", ""):
            raise RequestError(403, "Cross-origin requests are forbidden.")
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            raise RequestError(403, "Cross-site requests are forbidden.")

    def stream(self):
        cam = self.server.application.camera
        if not cam.status()["preview"]:
            raise Conflict("Enable preview first.")
        if not self.server.stream_slots.acquire(blocking=False):
            raise RequestError(503, "All preview connections are in use.")
        try:
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.end_headers()
            last = -1
            while not self.server.stopping.is_set() and self.authenticated():
                item = cam.preview_frame(last, 1.0)
                if item is None:
                    if not cam.status()["preview"]:
                        break
                    continue
                last, data = item
                self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                                 + str(len(data)).encode("ascii") + b"\r\n\r\n")
                self.wfile.write(data)
                self.wfile.write(b"\r\n")
            self.wfile.write(b"--frame--\r\n")
        except (ConnectionError, TimeoutError, ssl.SSLError):
            pass
        finally:
            self.server.stream_slots.release()

    def peer_address(self):
        host = self.client_address[0]
        # An IPv6 link-local UDP reply needs the ingress interface's scope ID.
        if len(self.client_address) == 4 and self.client_address[3] and "%" not in host:
            return f"{host}%{self.client_address[3]}"
        return host

    def download(self, session, filename, options):
        if not self.server.download_slots.acquire(blocking=False):
            raise RequestError(503, "All download connections are in use.")
        try:
            if filename == "archive.zip":
                with self.server.application.recording_archive(session, **options) as chunks:
                    return self.send_archive(session, chunks)
            with self.server.application.recording_file(session, filename, **options) as file:
                info = os.fstat(file.fileno())
                size = info.st_size
                etag = f'"{info.st_dev:x}-{info.st_ino:x}-{size:x}-{info.st_mtime_ns:x}-{info.st_ctime_ns:x}"'
                requested = self.headers.get("Range")
                start, length, status = 0, size, 200
                if requested is not None and self.headers.get("If-Range", etag) == etag:
                    offsets = byte_range(requested, size)
                    if offsets is None:
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{size}")
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    start, end = offsets
                    length, status = end - start + 1, 206
                file.seek(start)
                self.send_response(status)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("ETag", etag)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
                self.send_header("Content-Length", str(length))
                if status == 206:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.end_headers()
                if self.command == "HEAD":
                    return
                try:
                    while length and not self.server.stopping.is_set():
                        block = file.read(min(length, 128 * 1024))
                        if not block:
                            break
                        self.wfile.write(block)
                        length -= len(block)
                except OSError:
                    # Disconnect or media failure after headers. A short body allows a retry.
                    self.close_connection = True
        except FileNotFoundError as exc:
            raise RequestError(404, "The completed recording or file is unavailable.") from exc
        finally:
            self.server.download_slots.release()

    def send_archive(self, session, chunks):
        # The ZIP size is unknown until the central directory is written. HTTP/1.1
        # chunking lets a client distinguish completion from an interrupted transfer.
        chunked = self.request_version == "HTTP/1.1"
        if chunked:
            self.protocol_version = "HTTP/1.1"
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Disposition", f'attachment; filename="{session}.zip"')
        self.send_header("Accept-Ranges", "none")
        self.send_header("Connection", "close")
        if chunked:
            self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        if self.command == "HEAD":
            return
        try:
            for block in chunks:
                if self.server.stopping.is_set():
                    return
                if chunked:
                    self.wfile.write(f"{len(block):x}\r\n".encode("ascii"))
                self.wfile.write(block)
                if chunked:
                    self.wfile.write(b"\r\n")
            if chunked:
                self.wfile.write(b"0\r\n\r\n")
        except OSError:
            # Headers are already on the wire. End the connection without a final chunk.
            return

    def dispatch(self, method):
        app = self.server.application
        url = urlsplit(self.path)
        path = url.path
        if method in ("GET", "HEAD") and path in STATIC_FILES:
            name, mime = STATIC_FILES[path]
            return self.reply(200, (STATIC / name).read_bytes(), mime)
        if method == "POST":
            self.check_origin()
            if path == "/api/login":
                if self.server.auth is None:
                    raise RequestError(409, "Token sign-in is disabled.")
                token = self.read_object().get("token")
                if not isinstance(token, str):
                    raise ValueError("Enter an access token.")
                cookie = self.server.auth.login(self.client_address[0], token)
                return self.reply(200, {"ok": True}, cookie=cookie)
        if not self.authenticated():
            raise RequestError(401, "Sign in with the device access token.")
        if method in ("GET", "HEAD") and path == "/api/tools/render_recording.py":
            script = Path(__file__).resolve().parents[1] / "tools" / "render_recording.py"
            return self.reply(200, script.read_bytes(), "application/octet-stream", filename=script.name)
        if method in ("GET", "HEAD") and path.startswith("/api/recordings/"):
            parts = path.split("/")
            if len(parts) != 5:
                raise RequestError(404, "Unknown recording file.")
            return self.download(parts[3], parts[4], recording_query(url.query, listing=False))
        if method == "HEAD":
            raise RequestError(404, "Unknown endpoint.")
        if method == "GET":
            if path == "/api/status":
                return self.reply(200, {**app.status(), "client_address": self.peer_address(),
                                        "token_required": self.server.auth is not None})
            if path == "/api/storage":
                return self.reply(200, {"devices": app.storage.inventory()})
            if path in ("/api/nvme", "/api/usb"):
                return self.reply(200, app.nvme_status(path.rsplit("/", 1)[-1]))
            if path == "/api/recordings":
                return self.reply(200, app.recordings(**recording_query(url.query, listing=True)))
            if path == "/api/firmware":
                return self.reply(200, app.firmware.status())
            if path == "/api/capabilities":
                return self.reply(200, app.camera.capabilities())
            if path == "/api/stream.mjpg":
                return self.stream()
        else:
            body = self.read_object()
            if path == "/api/logout":
                if self.server.auth is not None:
                    self.server.auth.logout(self.cookie())
                return self.reply(200, {"ok": True}, cookie="")
            if path in ("/api/preview", "/api/recording"):
                if set(body) != {"enabled"} or type(body["enabled"]) is not bool:
                    raise ValueError("Set enabled to true or false.")
                if path == "/api/preview":
                    app.preview(body["enabled"])
                else:
                    app.record("manual", body["enabled"])
            elif path == "/api/live":
                if set(body) - {"enabled", "host", "port", "max_fps"} or type(body.get("enabled")) is not bool:
                    raise ValueError("Specify enabled and optionally the receiver IP, UDP port and stream FPS limit.")
                host = body.get("host", "")
                if isinstance(host, str):
                    host = host.strip() or self.peer_address()
                host, port = endpoint(host, body.get("port", 5004))
                app.set_live(body["enabled"], host, port, body.get("max_fps"))
            elif path == "/api/camera/mode-test":
                result = app.camera_mode_test(body)
                return self.reply(202 if body["action"] == "start" else 200, result)
            elif path == "/api/firmware":
                return self.reply(200, app.set_firmware(body))
            elif path == "/api/clock":
                return self.reply(200, app.set_clock(body))
            elif path in ("/api/nvme", "/api/usb"):
                return self.reply(200, app.apply_nvme(body, path.rsplit("/", 1)[-1]))
            elif path == "/api/recordings/cleanup":
                return self.reply(200, app.cleanup_recordings(body))
            elif path == "/api/reboot":
                return self.reply(202, app.reboot(body))
            elif path == "/api/config":
                app.configure(body)
            elif path == "/api/network":
                return self.reply(200, app.switch_network(body))
            else:
                raise RequestError(404, "Unknown endpoint.")
            return self.reply(200, {"ok": True})
        raise RequestError(404, "Unknown endpoint.")

    def run_request(self, method):
        try:
            self.dispatch(method)
        except RequestError as exc:
            self.reply(exc.status, {"error": str(exc)})
        except (ValueError, TypeError) as exc:
            self.reply(400, {"error": str(exc)})
        except (CameraBusy, Conflict, FirmwareConflict, NetworkConflict, RecordingSourceChanged) as exc:
            self.reply(409, {"error": str(exc)})
        except (ConnectionError, TimeoutError, ssl.SSLError):
            pass
        except (OSError, RuntimeError) as exc:
            self.reply(503, {"error": str(exc)})
        except Exception:
            LOG.exception("Request failed")
            self.reply(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "Internal service error."})

    def do_GET(self):
        self.run_request("GET")

    def do_HEAD(self):
        self.run_request("HEAD")

    def do_POST(self):
        self.run_request("POST")
