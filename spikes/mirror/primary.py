"""Fake primary backend: answers every request 200 with a small JSON body.

This stands in for Yattee Server. It bytes-counts and hashes the body it received so the
run script can prove the primary and the mirror received the same bytes, and it sleeps
PRIMARY_LATENCY_MS before responding so the run script can measure whether the mirror
delays the client.

Latency is re-read from PRIMARY_LATENCY_FILE on every request so the run script can
change it without recreating the container.
"""

import hashlib
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PRIMARY_PORT", "8080"))
DEFAULT_LATENCY_MS = int(os.environ.get("PRIMARY_LATENCY_MS", "0"))
LATENCY_FILE = os.environ.get("PRIMARY_LATENCY_FILE", "/app/out/primary_latency_ms.txt")


def current_latency_ms() -> int:
    try:
        with open(LATENCY_FILE) as handle:
            return int(handle.read().strip() or DEFAULT_LATENCY_MS)
    except (OSError, ValueError):
        return DEFAULT_LATENCY_MS


def read_chunked(stream) -> bytes:
    chunks = bytearray()
    while True:
        size_line = stream.readline(64).split(b";", 1)[0].strip()
        size = int(size_line or b"0", 16)
        if size == 0:
            stream.readline(64)
            return bytes(chunks)
        chunks += stream.read(size)
        stream.readline(2)


def read_body(handler) -> bytes:
    if "chunked" in handler.headers.get("Transfer-Encoding", "").lower():
        return read_chunked(handler.rfile)
    length = int(handler.headers.get("Content-Length") or 0)
    return handler.rfile.read(length) if length else b""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "serendip-spike-primary"

    def log_message(self, fmt, *args):  # keep the container log quiet
        pass

    def _respond(self):
        body = read_body(self)
        latency_ms = current_latency_ms()
        if latency_ms:
            time.sleep(latency_ms / 1000)
        payload = json.dumps(
            {
                "ok": True,
                "method": self.command,
                "request_target": self.path,
                "host": self.headers.get("Host"),
                "body_len": len(body),
                "body_sha256": hashlib.sha256(body).hexdigest(),
                "t_response": time.time(),
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    do_GET = _respond
    do_POST = _respond
    do_PUT = _respond
    do_DELETE = _respond


def main() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.daemon_threads = True
    print(
        f"primary listening on :{PORT} latency_ms={current_latency_ms()}",
        file=sys.stderr,
        flush=True,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
