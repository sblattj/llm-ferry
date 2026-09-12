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


if __name__ == "__main__":
    unittest.main()
