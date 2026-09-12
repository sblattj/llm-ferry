#!/usr/bin/env python3
"""Stdlib unittest for `ferry serve-vnc` — the browser VNC viewer + WebSocket bridge.

Run:  python3 lib/ferry-vnc.test.py

Spawns the real built `ferry` with a throwaway $HOME holding a hand-written relay
state file and a stub noVNC directory, then talks HTTP and WebSocket to it.
"""
import base64, hashlib, http.client, io, json, os, re, shutil, socket, struct, subprocess, tarfile, tempfile, threading, time, unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FERRY = os.path.join(REPO, "ferry")
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def novnc_version():
    with open(FERRY) as f:
        m = re.search(r'^NOVNC_VERSION="([^"]+)"', f.read(), re.M)
    return m.group(1)


def vnc_port():
    with open(FERRY) as f:
        m = re.search(r'^VNC_PORT="(\d+)"', f.read(), re.M)
    return int(m.group(1))


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
        """Terminate the daemon and return everything it printed. Idempotent, because it
        is both an addCleanup and something a test calls directly to read the log."""
        if getattr(self, "_stopped", False):
            return self._daemon_log
        self._stopped = True
        if self.proc.poll() is None:
            self.proc.terminate()
            try: self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired: self.proc.kill()
        self._daemon_log = self.proc.stdout.read() if self.proc.stdout else ""
        if self.proc.stdout: self.proc.stdout.close()
        return self._daemon_log

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


class StatusReportsVncTest(unittest.TestCase):
    """`ferry status` (cmd_status in lib/ferry-serve.zsh) reports the browser VNC
    viewer. VNC_PORT is a fixed global (unlike serve-vnc's own --port), so the
    check below reuses whatever already listens there rather than claiming it —
    a stray unrelated process on that port satisfies cmd_status's lsof check
    just as well, and this way the test never fights another listener for it."""

    @classmethod
    def setUpClass(cls):
        cls.port = vnc_port()

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="ferry-status-home-")
        self.tmp = tempfile.mkdtemp(prefix="ferry-status-tmp-")
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        os.makedirs(os.path.join(self.tmp, "ferry-logs"), exist_ok=True)
        cfg = os.path.join(self.home, ".config", "ferry")
        os.makedirs(cfg, exist_ok=True)
        self.state_path = os.path.join(cfg, "relay-published.json")
        self._listener = None
        try:
            socket.create_connection(("127.0.0.1", self.port), timeout=0.5).close()
        except OSError:
            self._listener = socket.socket()
            self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._listener.bind(("127.0.0.1", self.port))
            self._listener.listen(1)
            self.addCleanup(self._listener.close)

    def write_state(self, obj):
        with open(self.state_path, "w") as f:
            json.dump(obj, f)

    def status(self):
        e = os.environ.copy(); e["HOME"] = self.home; e["TMPDIR"] = self.tmp
        return subprocess.run(["zsh", FERRY, "status"], env=e, capture_output=True,
                              text=True, timeout=60)

    def test_viewer_online_lists_each_vnc_screen(self):
        self.write_state({"5901": {"client": "10.0.0.7", "label": "laptop", "kind": "vnc",
                                    "since": "2026-09-12 10:00:00", "bind": "0.0.0.0"}})
        out = self.status().stdout
        self.assertIn("VNC viewer is", out)
        self.assertRegex(out, r"Screen laptop: http://\S+:%d/vnc/5901" % self.port)

    def test_tcp_only_state_says_no_screens_published(self):
        self.write_state({"4290": {"client": "10.0.0.8", "label": "dev", "kind": "tcp",
                                    "since": "2026-09-12 10:00:00", "bind": "0.0.0.0"}})
        out = self.status().stdout
        self.assertIn("VNC viewer is", out)
        self.assertIn("No screens published", out)

    # --- fix round 1: a malformed state file must not print a traceback -----
    def test_a_json_list_state_file_does_not_crash_status(self):
        with open(self.state_path, "w") as f:
            json.dump([], f)
        r = self.status()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("Traceback", r.stdout + r.stderr)

    def test_a_non_numeric_key_is_skipped_but_a_valid_screen_still_prints(self):
        self.write_state({"abc": {"kind": "vnc"},
                          "5901": {"kind": "vnc", "client": "x", "bind": "0.0.0.0", "since": "now"}})
        r = self.status()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("Traceback", r.stdout + r.stderr)
        self.assertIn("Screen x: http://", r.stdout)
        self.assertIn("/vnc/5901", r.stdout)


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

    # --- hardening (fix round 1) ---------------------------------------------
    @staticmethod
    def drain_to_eof(ws, timeout=10):
        """Read until the server closes; returns the bytes it sent first."""
        ws.sock.settimeout(timeout)
        out = b""
        while True:
            chunk = ws.sock.recv(65536)
            if not chunk:
                return out
            out += chunk

    def test_disconnect_mid_header_is_not_a_traceback(self):
        self.start()
        ws = WsClient(self.port, f"/ws/{self.echo.port}")
        self.assertEqual(ws.status, 101)
        ws.sock.sendall(b"\x82\xfe")   # masked, 16-bit length announced, then nothing
        ws.sock.close()
        time.sleep(0.5)
        self.assertEqual(self.get("/")[0], 200, "daemon stopped serving after a torn frame")
        log = self.stop()
        self.assertNotIn("Traceback", log, log)

    def test_ping_does_not_splice_into_a_large_frame(self):
        self.start()
        ws = WsClient(self.port, f"/ws/{self.echo.port}")
        self.addCleanup(ws.close)
        size = 70000
        payload = bytes((i * 7) % 251 for i in range(size))
        ws.send(payload)
        ws.send(b"hi", opcode=0x9)
        got, pongs = b"", []
        while len(got) < size or not pongs:
            op, data = ws.recv()
            if op == 0xA:
                pongs.append(data)
            elif op == 0x2:
                got += data
            else:
                self.fail(f"unexpected opcode 0x{op:x}")
        self.assertEqual(pongs, [b"hi"])
        self.assertEqual(got, payload)

    def test_oversized_declared_length_closes_without_hanging(self):
        self.start()
        ws = WsClient(self.port, f"/ws/{self.echo.port}")
        self.addCleanup(ws.close)
        self.assertEqual(ws.status, 101)
        too_big = (1 << 20) + 1
        ws.sock.sendall(bytes([0x82, 0x80 | 127]) + struct.pack("!Q", too_big) + b"\x12\x34\x56\x78")
        self.assertEqual(self.drain_to_eof(ws), b"")

    def test_unmasked_client_frame_is_rejected_with_1002(self):
        self.start()
        ws = WsClient(self.port, f"/ws/{self.echo.port}")
        self.addCleanup(ws.close)
        self.assertEqual(ws.status, 101)
        ws.sock.sendall(bytes([0x82, 2]) + b"hi")   # no mask bit, no masking key
        op, data = ws.recv()
        self.assertEqual(op, 0x8)
        self.assertEqual(struct.unpack("!H", data[:2])[0], 1002)


class FetchTest(VncServeTest):
    def make_tarball(self):
        ver = novnc_version()
        path = os.path.join(self.tmp, "novnc.tar.gz")
        with tarfile.open(path, "w:gz") as tf:
            for name, data in ((f"noVNC-{ver}/vnc.html", b"<html>real viewer</html>"),
                               (f"noVNC-{ver}/app/ui.js", b"// ui"),
                               (f"noVNC-{ver}/core/rfb.js", b"// rfb"),
                               (f"noVNC-{ver}/vendor/pako/x.js", b"// pako"),
                               (f"noVNC-{ver}/tests/big.js", b"// not wanted"),
                               (f"noVNC-{ver}/README.md", b"nope")):
                info = tarfile.TarInfo(name); info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
        with open(path, "rb") as f:
            return path, hashlib.sha256(f.read()).hexdigest()

    def fetch(self, url, sha):
        e = self.env(); e["FERRY_NOVNC_URL"] = url; e["FERRY_NOVNC_SHA256"] = sha
        return subprocess.run(["zsh", FERRY, "serve-vnc", "--fetch"], env=e,
                              capture_output=True, text=True, timeout=120)

    def test_fetch_extracts_only_the_viewer_tree(self):
        shutil.rmtree(self.novnc)
        path, sha = self.make_tarball()
        r = self.fetch("file://" + path, sha)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        with open(os.path.join(self.novnc, "vnc.html"), "rb") as f:
            self.assertEqual(f.read(), b"<html>real viewer</html>")
        self.assertTrue(os.path.isfile(os.path.join(self.novnc, "core", "rfb.js")))
        self.assertTrue(os.path.isfile(os.path.join(self.novnc, "vendor", "pako", "x.js")))
        self.assertFalse(os.path.exists(os.path.join(self.novnc, "tests")))
        self.assertFalse(os.path.exists(os.path.join(self.novnc, "README.md")))
        with open(os.path.join(self.novnc, "VERSION")) as f:
            self.assertEqual(f.read().strip(), novnc_version())

    def test_fetch_refuses_a_bad_checksum_and_leaves_the_old_tree(self):
        path, _ = self.make_tarball()
        r = self.fetch("file://" + path, "0" * 64)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("checksum", r.stdout.lower())
        with open(os.path.join(self.novnc, "vnc.html")) as f:
            self.assertEqual(f.read(), "<html>stub viewer</html>")

    def test_serve_starts_after_fetch(self):
        shutil.rmtree(self.novnc)
        path, sha = self.make_tarball()
        self.assertEqual(self.fetch("file://" + path, sha).returncode, 0)
        self.start()
        self.assertEqual(self.get("/novnc/core/rfb.js")[2], b"// rfb")

    def test_fetch_with_invalid_url_scheme_fails_cleanly(self):
        r = self.fetch("not-a-valid-url-scheme", "0" * 64)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertNotIn("Traceback", r.stdout + r.stderr)
        self.assertIn("fetching noVNC failed", r.stdout)

    def test_fetch_ignores_path_traversal_members(self):
        shutil.rmtree(self.novnc)
        ver = novnc_version()
        path = os.path.join(self.tmp, "evil.tar.gz")
        with tarfile.open(path, "w:gz") as tf:
            for name, data in ((f"noVNC-{ver}/vnc.html", b"<html>real viewer</html>"),
                               (f"noVNC-{ver}/app/ok.js", b"// ok"),
                               (f"noVNC-{ver}/app/../../evil.txt", b"pwned"),
                               (f"noVNC-{ver}/vnc.htmlx", b"not the viewer")):
                info = tarfile.TarInfo(name); info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
        with open(path, "rb") as f:
            sha = hashlib.sha256(f.read()).hexdigest()
        r = self.fetch("file://" + path, sha)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(os.path.isfile(os.path.join(self.novnc, "app", "ok.js")))
        self.assertFalse(os.path.exists(os.path.join(self.novnc, "vnc.htmlx")))
        for root, _, files in os.walk(self.tmp):
            self.assertNotIn("evil.txt", files, f"traversal member escaped into {root}")


def daemon_source():
    """The python the zsh module heredocs into python3, as text."""
    with open(os.path.join(REPO, "lib", "ferry-vnc.zsh")) as f:
        src = f.read()
    return src.split("<<'PYEOF'\n", 1)[1].split("\nPYEOF", 1)[0]


def fetch_source():
    """The python the zsh module heredocs into python3 for `novnc_fetch` (the SECOND
    `<<'PYEOF'` block in the file — the first is the daemon `daemon_source()` reads),
    as text."""
    with open(os.path.join(REPO, "lib", "ferry-vnc.zsh")) as f:
        src = f.read()
    return src.split("<<'PYEOF'\n")[2].split("\nPYEOF", 1)[0]


class FrameEncoderTest(unittest.TestCase):
    """Exec just the shipped `frame` helper out of the daemon source, so the encoder
    under test is literally the text that ships — the wire cannot prove the 64-bit
    branch, because the pump's recv(65536) may return short."""

    def setUp(self):
        m = re.search(r"^def frame\(.*?(?=\n\ndef )", daemon_source(), re.S | re.M)
        self.assertIsNotNone(m, "could not find `def frame(` in lib/ferry-vnc.zsh")
        ns = {"struct": struct}
        exec(m.group(0), ns)
        self.frame = ns["frame"]

    def test_seven_bit_length_below_126(self):
        self.assertEqual(self.frame(b"x" * 125)[:2], bytes([0x82, 125]))

    def test_sixteen_bit_branch_from_126_to_65535(self):
        for n in (126, 65535):
            f = self.frame(b"x" * n)
            self.assertEqual(f[1], 126, n)
            self.assertEqual(struct.unpack("!H", f[2:4])[0], n)
            self.assertEqual(len(f), 4 + n)

    def test_sixty_four_bit_branch_at_65536(self):
        f = self.frame(b"x" * 65536)
        self.assertEqual(f[1], 127)
        self.assertEqual(struct.unpack("!Q", f[2:10])[0], 65536)
        self.assertEqual(len(f), 10 + 65536)

    def test_opcode_rides_in_the_first_byte_with_fin_set(self):
        self.assertEqual(self.frame(b"hi", 0xA)[0], 0x8A)
        self.assertEqual(self.frame(b"", 0x8)[:2], bytes([0x88, 0]))


class FakeTarMember:
    def __init__(self, name, isfile=True, isdir=False):
        self.name, self._isfile, self._isdir = name, isfile, isdir
    def isfile(self):
        return self._isfile
    def isdir(self):
        return self._isdir


class SafeMemberTest(unittest.TestCase):
    """Exec just the shipped `safe_member` traversal predicate out of the fetch
    heredoc, so the check under test is literally the text that ships. This proves
    the predicate itself, independent of the tf.extractall(filter="data") 3.12+
    backstop that FetchTest.test_fetch_ignores_path_traversal_members exercises
    end-to-end through the real interpreter on this host."""

    def setUp(self):
        # Captures both the `dirs = (...)` allowlist AND `def safe_member` together —
        # safe_member reads `dirs` out of its enclosing (module) scope, so execing the
        # function alone with a hand-typed `dirs` would test a reimplementation, not
        # the shipped allowlist.
        m = re.search(r"^dirs = .*?(?=\n\n\ntmp = )", fetch_source(), re.S | re.M)
        self.assertIsNotNone(m, "could not find `dirs = ` / `def safe_member(` in lib/ferry-vnc.zsh")
        ns = {"os": os}
        exec(m.group(0), ns)
        self.safe_member = ns["safe_member"]

    def test_rejects_relative_traversal_out_of_the_tree(self):
        self.assertFalse(self.safe_member(FakeTarMember("app/../../evil.txt")))

    def test_rejects_absolute_paths(self):
        self.assertFalse(self.safe_member(FakeTarMember("/etc/passwd")))

    def test_accepts_files_under_kept_directories(self):
        self.assertTrue(self.safe_member(FakeTarMember("app/ok.js")))
        self.assertTrue(self.safe_member(FakeTarMember("core/rfb.js")))
        self.assertTrue(self.safe_member(FakeTarMember("vendor/pako/x.js")))

    def test_vnc_html_matches_only_exactly(self):
        self.assertTrue(self.safe_member(FakeTarMember("vnc.html")))
        self.assertFalse(self.safe_member(FakeTarMember("vnc.htmlx")))

    def test_rejects_paths_outside_the_kept_set(self):
        self.assertFalse(self.safe_member(FakeTarMember("tests/big.js")))
        self.assertFalse(self.safe_member(FakeTarMember("README.md")))

    def test_rejects_non_file_non_dir_members(self):
        self.assertFalse(self.safe_member(FakeTarMember("app/link.js", isfile=False, isdir=False)))


if __name__ == "__main__":
    unittest.main()
