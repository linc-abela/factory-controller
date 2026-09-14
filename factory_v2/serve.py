"""Local event-driven PCP intake over an explicit HTTP POST."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from factory_v2.canonical import ContractError
from factory_v2.machine import Controller, GateError, InvariantError

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8790
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class IntakeHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address, controller: Controller):
        self.controller = controller
        self.intake_lock = threading.Lock()
        super().__init__(server_address, IntakeHandler)


class IntakeHandler(BaseHTTPRequestHandler):
    server: IntakeHTTPServer

    def log_message(self, format: str, *args: Any) -> None:
        del format, args

    def do_GET(self) -> None:
        if self.path.rstrip("/") == "/health":
            self._json(200, {"ok": True})
            return
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path.rstrip("/") != "/v1/pcp":
            self._json(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            self._json(400, {"error": "invalid Content-Length", "code": "PCP_MALFORMED"})
            return
        raw = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(raw.decode("utf-8") or "null")
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._json(400, {"error": "request body must be JSON", "code": "PCP_MALFORMED"})
            return
        if not isinstance(payload, dict):
            self._json(400, {"error": "PCP document must be an object", "code": "PCP_MALFORMED"})
            return
        try:
            with self.server.intake_lock:
                snap = self.server.controller.submit_pcp(payload)
        except (GateError, ContractError) as exc:
            self._json(400, {"error": str(exc), "code": getattr(exc, "code", "PCP_MALFORMED")})
            return
        except InvariantError as exc:
            self._json(409, {"error": str(exc), "code": getattr(exc, "code", "INVARIANT")})
            return
        self._json(200, snap.as_status_dict())

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def bind_host(host: str) -> str:
    if host not in LOOPBACK_HOSTS:
        raise ValueError(
            f"intake binds locally ({', '.join(sorted(LOOPBACK_HOSTS))}); refused {host!r}"
        )
    return host


def make_server(
    controller: Controller,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> IntakeHTTPServer:
    return IntakeHTTPServer((bind_host(host), port), controller)


def serve_forever(
    controller: Controller,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> None:
    httpd = make_server(controller, host, port)
    controller.resume_incomplete()
    print(
        json.dumps(
            {
                "intake": "POST /v1/pcp",
                "host": httpd.server_address[0],
                "port": httpd.server_address[1],
            }
        ),
        flush=True,
    )
    httpd.serve_forever()
