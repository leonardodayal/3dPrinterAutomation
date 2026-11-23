#!/usr/bin/env python3
"""Tiny JPEG snapshot server for the Creality K1C stock camera.

The stock `cam_app` keeps its latest MJPEG frame in a memfd and hands that fd out to any
client of /var/run/mjpg_main_sock (SCM_RIGHTS). The buffer starts with a 36-byte header of
nine little-endian uint32s; the last one is the JPEG length, and the JPEG follows.

Serves:
  GET /snapshot.jpg  -> latest frame (image/jpeg)
  GET /health        -> "ok"

Runs on the printer itself (stdlib only). Installed by printloop as
/usr/data/printloop/snapshot_server.py with /etc/init.d/S99printloop_snapshot.
"""
import array
import os
import socket
import struct
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SOCK_PATH = "/var/run/mjpg_main_sock"
PORT = int(os.environ.get("SNAPSHOT_PORT", "8090"))
HEADER = struct.Struct("<9I")
SOI, EOI = b"\xff\xd8\xff", b"\xff\xd9"
MAX_STALE = 5.0  # seconds a cached frame may be served while another grab is in progress

_grab_lock = threading.Lock()
_last = {"jpg": None, "at": 0.0}


def _grab_frame(retries=3):
    last_err = "no frame"
    for _ in range(retries):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(1.5)
        fd = None
        try:
            s.connect(SOCK_PATH)
            fds = array.array("i")
            _, anc, _, _ = s.recvmsg(16, socket.CMSG_LEN(4 * fds.itemsize))
            for _lvl, typ, data in anc:
                if typ == socket.SCM_RIGHTS:
                    fds.frombytes(data[: len(data) - len(data) % fds.itemsize])
            if not fds:
                raise RuntimeError("camera socket passed no buffer")
            fd = fds[0]
            for extra in fds[1:]:
                os.close(extra)
            os.lseek(fd, 0, 0)
            buf = os.read(fd, os.fstat(fd).st_size)
            length = HEADER.unpack_from(buf)[8]
            start = buf.find(SOI, 0, 4096)
            jpg = buf[start:start + length] if start >= 0 else b""
            # The camera writes the buffer continuously; reject torn frames and retry.
            if len(jpg) == length and jpg.endswith(EOI):
                return jpg
            last_err = "torn frame"
        except (OSError, RuntimeError, struct.error) as e:
            last_err = str(e)
        finally:
            if fd is not None:
                os.close(fd)
            s.close()
        time.sleep(0.05)
    raise RuntimeError(last_err)


def grab_frame():
    """One camera read at a time (cam_app's socket may serve a single client).

    Concurrent callers wait briefly for the in-flight grab, then fall back to the last good frame.
    """
    if _grab_lock.acquire(timeout=2.0):
        try:
            jpg = _grab_frame()
            _last.update(jpg=jpg, at=time.time())
            return jpg
        except RuntimeError:
            if _last["jpg"] and time.time() - _last["at"] < MAX_STALE:
                return _last["jpg"]
            raise
        finally:
            _grab_lock.release()
    if _last["jpg"] and time.time() - _last["at"] < MAX_STALE:
        return _last["jpg"]
    raise RuntimeError("camera busy")


class Handler(BaseHTTPRequestHandler):
    timeout = 10  # drop clients that stall mid-request so they can't pin a thread forever
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/snapshot.jpg", "/snapshot", "/"):
            try:
                body, ctype, code = grab_frame(), "image/jpeg", 200
            except RuntimeError as e:
                body, ctype, code = ("camera error: %s" % e).encode(), "text/plain", 503
        elif path == "/health":
            body, ctype, code = b"ok", "text/plain", 200
        else:
            body, ctype, code = b"not found", "text/plain", 404
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # keep the printer's flash quiet
        pass


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--once":
        sys.stdout.buffer.write(grab_frame())
        sys.exit(0)
    ThreadingHTTPServer.daemon_threads = True
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
