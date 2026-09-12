#!/usr/bin/env python3
"""Stdlib unittest for `ferry serve-vnc` — the browser VNC viewer + WebSocket bridge.

Run:  python3 lib/ferry-vnc.test.py

Spawns the real built `ferry` with a throwaway $HOME holding a hand-written relay
state file and a stub noVNC directory, then talks HTTP and WebSocket to it.
"""
import base64, hashlib, http.client, json, os, re, shutil, socket, struct, subprocess, tempfile, threading, time, unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FERRY = os.path.join(REPO, "ferry")
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def novnc_version():
    with open(FERRY) as f:
        m = re.search(r'^NOVNC_VERSION="([^"]+)"', f.read(), re.M)
    return m.group(1)


class EchoService(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.sock = socket.socket(); self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0)); self.sock.listen(16); self.port = self.sock.getsockname()[1]
    def run(self):
        while True:
            try: conn, _ = self.sock.accept()
            except OSError: return
            threading.Thread(target=self.serve, args=(conn,), daemon=True).start()
    def serve(self, conn):
        try:
            while True:
                d = conn.recv(65536)
                if not d: break
                conn.sendall(d)
        except OSError: pass
        finally: conn.close()
    def shutdown(self):
        try: self.sock.close()
        except OSError: pass


class VncServeTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="ferry-vnc-home-")
        self.tmp = tempfile.mkdtemp(prefix="ferry-vnc-tmp-")
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        os.makedirs(os.path.join(self.tmp, "ferry-logs"), exist_ok=True)
        self.cfg = os.path.join(self.home, ".config", "ferry")
        self.novnc = os.path.join(self.cfg, "novnc")
        os.makedirs(self.novnc)
        with open(os.path.join(self.novnc, "VERSION"), "w") as f: f.write(novnc_version() + "\n")
        with open(os.path.join(self.novnc, "vnc.html"), "w") as f: f.write("<html>stub viewer</html>")
        self.state_path = os.path.join(self.cfg, "relay-published.json")
        self.echo = EchoService(); self.echo.start(); self.addCleanup(self.echo.shutdown)
        self.port = free_port()
        self.write_state({str(self.echo.port): {"client": "10.0.0.7", "label": "laptop", "kind": "vnc",
                                                "since": "2026-09-12 10:00:00", "bind": "0.0.0.0"},
                          "4290": {"client": "10.0.0.8", "label": "dev", "kind": "tcp",
                                   "since": "2026-09-12 10:00:00", "bind": "0.0.0.0"}})

    def write_state(self, obj):
        with open(self.state_path, "w") as f: json.dump(obj, f)

    def env(self):
        e = os.environ.copy(); e["HOME"] = self.home; e["TMPDIR"] = self.tmp; return e

    def start(self):
        self.proc = subprocess.Popen(["zsh", FERRY, "serve-vnc", "--foreground", "--port", str(self.port),
                                      "--bind", "127.0.0.1"], env=self.env(),
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.addCleanup(self.stop)
        end = time.time() + 15
        while time.time() < end:
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.5): return
            except OSError: time.sleep(0.15)
        self.fail("serve-vnc never listened")

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try: self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired: self.proc.kill()
        if self.proc.stdout: self.proc.stdout.close()

    def get(self, path):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request("GET", path); r = c.getresponse(); body = r.read(); c.close()
        return r.status, dict(r.getheaders()), body

    # --- routes --------------------------------------------------------------
    def test_index_lists_vnc_exposures_only(self):
        self.start()
        status, _, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn(f"/vnc/{self.echo.port}".encode(), body)
        self.assertIn(b"laptop", body)
        self.assertNotIn(b"/vnc/4290", body)

    def test_index_with_no_state_file_says_nothing_published(self):
        os.remove(self.state_path)
        self.start()
        status, _, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn(b"Nothing published", body)

    def test_index_with_non_object_state_file_says_nothing_published(self):
        with open(self.state_path, "w") as f: json.dump([], f)
        self.start()
        status, _, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn(b"Nothing published", body)
        self.assertEqual(self.get("/ws/4290")[0], 403)

    def test_index_with_non_numeric_port_key_says_nothing_published(self):
        self.write_state({"abc": {"kind": "vnc"}})
        self.start()
        status, _, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn(b"Nothing published", body)
        self.assertEqual(self.get("/ws/4290")[0], 403)

    def test_vnc_route_redirects_into_novnc(self):
        self.start()
        status, headers, _ = self.get(f"/vnc/{self.echo.port}")
        self.assertEqual(status, 302)
        self.assertEqual(headers["Location"],
                         f"/novnc/vnc.html?autoconnect=1&resize=scale&path=ws/{self.echo.port}")

    def test_vnc_route_for_unpublished_port_is_404(self):
        self.start()
        self.assertEqual(self.get("/vnc/4290")[0], 404)
        self.assertEqual(self.get("/vnc/59999")[0], 404)

    def test_static_serves_novnc_files_and_blocks_traversal(self):
        self.start()
        status, headers, body = self.get("/novnc/vnc.html")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"<html>stub viewer</html>")
        self.assertTrue(headers["Content-Type"].startswith("text/html"))
        self.assertEqual(self.get("/novnc/../VERSION")[0], 404)
        self.assertEqual(self.get("/novnc/%2e%2e/VERSION")[0], 404)
        self.assertEqual(self.get("/novnc/missing.js")[0], 404)

    def test_ws_route_is_403_for_unpublished_or_tcp_ports(self):
        self.start()
        for port in (4290, 59999):
            status, _, _ = self.get(f"/ws/{port}")
            self.assertEqual(status, 403, port)

    def test_refuses_to_start_without_novnc(self):
        shutil.rmtree(self.novnc)
        r = subprocess.run(["zsh", FERRY, "serve-vnc", "--foreground", "--port", str(self.port)],
                           env=self.env(), capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("serve-vnc --fetch", r.stdout)


class WsClient:
    """Just enough RFC 6455 to test the server side: masked frames out, unmasked in."""

    def __init__(self, port, path, key="dGhlIHNhbXBsZSBub25jZQ==", send_key=True, protocol="binary"):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        req = f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Version: 13\r\n"
        if send_key:
            req += f"Sec-WebSocket-Key: {key}\r\n"
        if protocol:
            req += f"Sec-WebSocket-Protocol: {protocol}\r\n"
        self.sock.sendall((req + "\r\n").encode())
        raw = b""
        while b"\r\n\r\n" not in raw:
            chunk = self.sock.recv(4096)
            if not chunk:
                break
            raw += chunk
        head, _, self.rest = raw.partition(b"\r\n\r\n")
        lines = head.decode(errors="replace").split("\r\n")
        self.status = int(lines[0].split()[1])
        self.headers = {k.strip().lower(): v.strip() for k, _, v in (l.partition(":") for l in lines[1:])}

    def send(self, payload, opcode=0x2):
        mask = b"\x12\x34\x56\x78"
        n = len(payload)
        head = bytes([0x80 | opcode])
        if n < 126: head += bytes([0x80 | n])
        elif n < 65536: head += bytes([0x80 | 126]) + struct.pack("!H", n)
        else: head += bytes([0x80 | 127]) + struct.pack("!Q", n)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(head + mask + masked)

    def _exact(self, n):
        buf = self.rest[:n]; self.rest = self.rest[n:]
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise EOFError
            buf += chunk
        return buf

    def recv(self):
        b0, b1 = self._exact(2)
        n = b1 & 0x7F
        if n == 126: n = struct.unpack("!H", self._exact(2))[0]
        elif n == 127: n = struct.unpack("!Q", self._exact(8))[0]
        assert not (b1 & 0x80), "server frames must not be masked"
        return b0 & 0x0F, self._exact(n)

    def close(self):
        self.sock.close()


class BridgeTest(VncServeTest):
    def test_handshake_accept_key_is_rfc6455(self):
        self.start()
        ws = WsClient(self.port, f"/ws/{self.echo.port}")
        self.addCleanup(ws.close)
        self.assertEqual(ws.status, 101)
        self.assertEqual(ws.headers["sec-websocket-accept"], "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=")
        self.assertEqual(ws.headers["sec-websocket-protocol"], "binary")

    def test_missing_key_is_400(self):
        self.start()
        ws = WsClient(self.port, f"/ws/{self.echo.port}", send_key=False)
        self.addCleanup(ws.close)
        self.assertEqual(ws.status, 400)

    def test_all_three_payload_lengths_round_trip(self):
        self.start()
        ws = WsClient(self.port, f"/ws/{self.echo.port}")
        self.addCleanup(ws.close)
        for size in (1, 200, 70000):
            payload = bytes((i * 7) % 251 for i in range(size))
            ws.send(payload)
            got = b""
            while len(got) < size:
                op, data = ws.recv()
                self.assertEqual(op, 0x2)
                got += data
            self.assertEqual(got, payload, size)

    def test_ping_gets_pong(self):
        self.start()
        ws = WsClient(self.port, f"/ws/{self.echo.port}")
        self.addCleanup(ws.close)
        ws.send(b"hi", opcode=0x9)
        self.assertEqual(ws.recv(), (0xA, b"hi"))

    def test_close_is_echoed_and_tcp_side_closes(self):
        self.start()
        ws = WsClient(self.port, f"/ws/{self.echo.port}")
        self.addCleanup(ws.close)
        ws.send(struct.pack("!H", 1000), opcode=0x8)
        op, data = ws.recv()
        self.assertEqual(op, 0x8)
        self.assertEqual(ws.sock.recv(1), b"")   # server closed after the close handshake

    def test_unreachable_published_port_is_502(self):
        dead = free_port()
        self.write_state({str(dead): {"client": "x", "label": "gone", "kind": "vnc", "since": "", "bind": "0.0.0.0"}})
        self.start()
        ws = WsClient(self.port, f"/ws/{dead}")
        self.addCleanup(ws.close)
        self.assertEqual(ws.status, 502)

    def test_port_that_stops_being_published_is_403_next_time(self):
        self.start()
        first = WsClient(self.port, f"/ws/{self.echo.port}")
        self.addCleanup(first.close)
        self.assertEqual(first.status, 101)
        self.write_state({})
        second = WsClient(self.port, f"/ws/{self.echo.port}")
        self.addCleanup(second.close)
        self.assertEqual(second.status, 403)


if __name__ == "__main__":
    unittest.main()
