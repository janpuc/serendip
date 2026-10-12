"""Fake mirror backend: records every request to JSONL and replies 204.

Stands in for serendip's ingest port (8081). Everything the mirror receives is written
as one JSON object per line so the run script can compare it with what was sent:

  seq, mode, arrival_ts, finish_ts, method, request_target, path, raw_query,
  host, headers (list of [name, value] pairs, in order), body_len, body_sha256,
  content_length, transfer_encoding, truncated, source_ip, remote_port

Modes (read from MODE_FILE on every request, so the run script can switch without
recreating the container):
  normal  204 immediately
  slow    sleep SLOW_SECONDS (5 s) before replying 204
  500     reply 500 with a small JSON body
"""

import hashlib
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("MIRROR_PORT", "8080"))
OUT = os.environ.get("MIRROR_OUT", "/app/out/mirror.jsonl")
MODE_FILE = os.environ.get("MIRROR_MODE_FILE", "/app/out/mode.txt")
DEFAULT_MODE = os.environ.get("MIRROR_MODE", "normal")
SLOW_SECONDS = float(os.environ.get("MIRROR_SLOW_SECONDS", "5"))
BODY_READ_TIMEOUT_S = float(os.environ.get("MIRROR_BODY_READ_TIMEOUT_S", "15"))

_write_lock = threading.Lock()
_seq_lock = threading.Lock()
_seq = 0


def current_mode() -> str:
    try:
        with open(MODE_FILE) as handle:
            mode = handle.read().strip()
        return mode or DEFAULT_MODE
    except OSError:
        return DEFAULT_MODE


def next_seq() -> int:
    global _seq
    with _seq_lock:
        _seq += 1
        return _seq


def read_body(handler):
    """Read the whole request body; return (bytes, truncated)."""
    timeout = BODY_READ_TIMEOUT_S
    handler.connection.settimeout(timeout)
    transfer_encoding = handler.headers.get("Transfer-Encoding", "")
    if "chunked" in transfer_encoding.lower():
        chunks = bytearray()
        try:
            while True:
                size_line = handler.rfile.readline(64).split(b";", 1)[0].strip()
                size = int(size_line or b"0", 16)
                if size == 0:
                    handler.rfile.readline(64)
                    return bytes(chunks), False
                chunks += handler.rfile.read(size)
                handler.rfile.readline(2)
        except (OSError, ValueError):
            return bytes(chunks), True
    length = int(handler.headers.get("Content-Length") or 0)
    if not length:
        return b"", False
    received = bytearray()
    try:
        while len(received) < length:
            block = handler.rfile.read(length - len(received))
            if not block:
                break
            received += block
    except OSError:
        pass
    return bytes(received), len(received) < length


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "serendip-spike-mirror"

    def log_message(self, fmt, *args):  # keep the container log quiet
        pass

    def _handle(self):
        arrival_ts = time.time()
        mode = current_mode()
        body, truncated = read_body(self)
        target = self.path
        path, _, raw_query = target.partition("?")
        record = {
            "seq": next_seq(),
            "mode": mode,
            "arrival_ts": arrival_ts,
            "arrival_iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(arrival_ts))
            + f".{int(arrival_ts % 1 * 1000):03d}Z",
            "method": self.command,
            "request_target": target,
            "path": path,
            "raw_query": raw_query,
            "host": self.headers.get("Host"),
            "headers": [[name, value] for name, value in self.headers.items()],
            "body_len": len(body),
            "body_sha256": hashlib.sha256(body).hexdigest(),
            "content_length": self.headers.get("Content-Length"),
            "transfer_encoding": self.headers.get("Transfer-Encoding"),
            "truncated": truncated,
            "source_ip": self.client_address[0],
            "remote_port": self.client_address[1],
            "finish_ts": None,
            "reply_error": None,
        }
        try:
            if mode == "slow":
                time.sleep(SLOW_SECONDS)
            if mode == "500":
                payload = b'{"error":"mirror failure"}'
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            else:
                self.send_response(204)
                self.end_headers()
        except OSError as exc:
            # Envoy reset the shadow stream before we replied; still record the request.
            record["reply_error"] = f"{type(exc).__name__}: {exc}"
        finally:
            record["finish_ts"] = time.time()
            line = json.dumps(record) + "\n"
            with _write_lock, open(OUT, "a") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())

    do_GET = _handle
    do_POST = _handle
    do_PUT = _handle
    do_DELETE = _handle


def main() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.daemon_threads = True
    print(
        f"mirror listening on :{PORT} mode={current_mode()} out={OUT}",
        file=sys.stderr,
        flush=True,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
