"""Loopback-only HTTP adapter for the existing DesktopService action registry."""
from __future__ import annotations

import hmac
import json
import secrets
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from src.common.cancellation import CancellationController, OperationCancelled
from src.desktop.service import DesktopService, dispatch

STATIC = Path(__file__).with_name("static")
FILES = {"/": ("index.html", "text/html; charset=utf-8"),
         "/app.js": ("app.js", "text/javascript; charset=utf-8"),
         "/style.css": ("style.css", "text/css; charset=utf-8")}
MAX_BODY = 16 * 1024 * 1024


class WebServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, port: int = 8765,
                 service_factory: Callable[[], DesktopService] = DesktopService):
        super().__init__(("127.0.0.1", port), RequestHandler)
        self.token = secrets.token_urlsafe(32)
        self.service_factory = service_factory
        self.process_lock = threading.Lock()
        self.processes: dict[str, dict] = {}

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_port}/?token={self.token}"

    def start_process(self, request: dict) -> str:
        # Validate the folder before starting the worker, including its root boundary.
        service = self.service_factory()
        service.folder(request.get("folder", ""))
        with self.process_lock:
            if any(not item["done"] for item in self.processes.values()):
                raise ValueError("已有轉錄工作正在執行，請等待或取消。")
            job_id = secrets.token_urlsafe(18)
            state = {"done": False, "events": [], "result": None,
                     "controller": CancellationController()}
            self.processes = {job_id: state}

        def worker():
            def progress(event):
                with self.process_lock:
                    state["events"].append(event)
            try:
                result = dispatch(service, request, state["controller"].token, progress)
                outcome = {"type": "result", "ok": True, **result}
            except Exception as exc:
                outcome = {"type": "result", "ok": False,
                           "cancelled": isinstance(exc, OperationCancelled), "error": str(exc)}
            with self.process_lock:
                state["result"] = outcome
                state["done"] = True

        threading.Thread(target=worker, daemon=True).start()
        return job_id


class RequestHandler(BaseHTTPRequestHandler):
    server: WebServer

    def log_message(self, format, *args):
        # Paths and query strings can contain the session token or local filenames.
        pass

    def _json(self, status: int, value: dict):
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _trusted(self) -> bool:
        port = self.server.server_port
        hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        host = self.headers.get("Host", "")
        origin = self.headers.get("Origin")
        if host not in hosts or (origin is not None and origin != f"http://{host}"):
            self._json(403, {"ok": False, "error": "請從本機網頁介面開啟。"})
            return False
        return True

    def _authorized(self) -> bool:
        supplied = self.headers.get("X-Meeting-Desk-Token", "")
        if not hmac.compare_digest(supplied, self.server.token):
            self._json(401, {"ok": False, "error": "連線驗證失敗，請使用啟動時提供的網址。"})
            return False
        return True

    def _read_json(self):
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip() != "application/json":
            raise ValueError("請提交 JSON 資料。")
        length = int(self.headers.get("Content-Length", "0"))
        if length < 1 or length > MAX_BODY:
            raise ValueError("請求內容大小不合法。")
        value = json.loads(self.rfile.read(length))
        if not isinstance(value, dict):
            raise ValueError("請求內容必須是物件。")
        return value

    def do_GET(self):
        if not self._trusted():
            return
        path = urlsplit(self.path).path
        if path.startswith("/api/"):
            if not self._authorized():
                return
            parts = path.split("/")
            if len(parts) == 4 and parts[2] == "process":
                with self.server.process_lock:
                    state = self.server.processes.get(parts[3])
                    if state:
                        value = {"ok": True, "done": state["done"],
                                 "events": list(state["events"]), "result": state["result"]}
                    else:
                        value = None
                self._json(200 if value else 404, value or {"ok": False, "error": "找不到工作。"})
            else:
                self._json(404, {"ok": False, "error": "找不到頁面。"})
            return
        item = FILES.get(path)
        if not item:
            self._json(404, {"ok": False, "error": "找不到頁面。"})
            return
        body = (STATIC / item[0]).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", item[1])
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'none'")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if not self._trusted() or not self._authorized():
            return
        path = urlsplit(self.path).path
        try:
            if path == "/api/action":
                request = self._read_json()
                if request.get("action") == "process":
                    raise ValueError("轉錄請使用可取消的工作介面。")
                result = dispatch(self.server.service_factory(), request)
                self._json(200, {"type": "result", "ok": True, **result})
            elif path == "/api/process":
                request = self._read_json()
                if request.get("action") != "process":
                    raise ValueError("此介面只接受轉錄工作。")
                self._json(202, {"ok": True, "id": self.server.start_process(request)})
            elif path.startswith("/api/process/") and path.endswith("/cancel"):
                job_id = path.split("/")[3]
                with self.server.process_lock:
                    state = self.server.processes.get(job_id)
                    if state is None:
                        self._json(404, {"ok": False, "error": "找不到工作。"})
                        return
                    controller = state["controller"]
                controller.cancel()
                self._json(200, {"ok": True})
            else:
                self._json(404, {"ok": False, "error": "找不到頁面。"})
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            self._json(400, {"type": "result", "ok": False, "error": str(exc)})
        except Exception as exc:
            self._json(500, {"type": "result", "ok": False,
                             "cancelled": isinstance(exc, OperationCancelled), "error": str(exc)})
