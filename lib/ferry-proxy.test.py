#!/usr/bin/env python3
"""Stdlib unittest for the `ferry serve-proxy` CONNECT tunnel.

Run:  python3 lib/ferry-proxy.test.py

The proxy is a Python heredoc inside lib/ferry-proxy.zsh. The tests lift that
heredoc out of the shell file and run the real handler class, so the bytes
under test are the bytes a client sees. The regression that motivated this
suite: a fast upstream (HF CDN, 1.6 GB weights file) outran a LAN client, the
non-blocking sendall() raised EAGAIN, the tunnel swallowed it and closed, and
huggingface_hub reported "peer closed connection without sending complete
message body (received 1372170 bytes, expected 1613977612)".
"""
import os
import socket
import sys
import textwrap
import threading
import time
import unittest
from http.server import ThreadingHTTPServer

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.environ.get("FERRY_PROXY_SRC") or os.path.join(REPO, "lib", "ferry-proxy.zsh")


def load_proxy_class():
    src = open(SRC).read()
    start = src.index("import sys, select, socket")
    end = src.rindex("ThreadingHTTPServer((")
    code = textwrap.dedent(src[start:end]).replace("PORT = int(sys.argv[1])", "PORT = 0")
    ns = {"__name__": "ferry_proxy_under_test"}
    exec(compile(code, SRC, "exec"), ns)
    return ns["Proxy"]


class Upstream(threading.Thread):
    """A TCP service that sends `payload` to the first connection, records what
    it receives, then closes."""

    def __init__(self, payload=b"", read_until_close=False):
        super().__init__(daemon=True)
        self.payload = payload
        self.read_until_close = read_until_close
        self.received = b""
        self.error = None
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]

    def run(self):
        conn, _ = self.sock.accept()
        try:
            if self.read_until_close:
                while True:
                    d = conn.recv(65536)
                    if not d:
                        break
                    self.received += d
            if self.payload:
                conn.sendall(self.payload)
        except Exception as e:  # noqa: BLE001 - recorded for the assertion
            self.error = e
        finally:
            conn.close()


def connect_via(proxy_addr, port):
    cl = socket.create_connection(proxy_addr)
    cl.sendall(f"CONNECT 127.0.0.1:{port} HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n".encode())
    hdr = b""
    while b"\r\n\r\n" not in hdr:
        b = cl.recv(1)
        if not b:
            raise AssertionError("proxy closed before CONNECT reply")
        hdr += b
    assert b" 200 " in hdr.split(b"\r\n", 1)[0], hdr
    return cl


class ConnectTunnelTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), load_proxy_class())
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def test_fast_upstream_slow_client_receives_every_byte(self):
        # 20 MB is far past any socket buffer; the client deliberately does not
        # read for a second so the upstream fills every buffer between them.
        payload = b"x" * 20_000_000
        up = Upstream(payload=payload)
        up.start()
        cl = connect_via(self.srv.server_address, up.port)
        time.sleep(1.0)
        got = 0
        cl.settimeout(30)
        while True:
            d = cl.recv(1 << 20)
            if not d:
                break
            got += len(d)
        cl.close()
        up.join(10)
        self.assertIsNone(up.error, f"upstream saw {up.error!r}: proxy hung up on it")
        self.assertEqual(got, len(payload))

    def test_client_bytes_reach_upstream_and_half_close_propagates(self):
        # A client that uploads then half-closes must still get the reply the
        # upstream sends after seeing EOF (the shape of an HTTP request body).
        up = Upstream(payload=b"REPLY", read_until_close=True)
        up.start()
        cl = connect_via(self.srv.server_address, up.port)
        cl.sendall(b"hello " * 100_000)
        cl.shutdown(socket.SHUT_WR)
        cl.settimeout(30)
        got = b""
        while True:
            d = cl.recv(65536)
            if not d:
                break
            got += d
        cl.close()
        up.join(10)
        self.assertEqual(up.received, b"hello " * 100_000)
        self.assertEqual(got, b"REPLY")

    def test_unreachable_upstream_is_a_502(self):
        dead = socket.socket()
        dead.bind(("127.0.0.1", 0))
        port = dead.getsockname()[1]
        dead.close()
        cl = socket.create_connection(self.srv.server_address)
        cl.sendall(f"CONNECT 127.0.0.1:{port} HTTP/1.1\r\n\r\n".encode())
        cl.settimeout(10)
        hdr = cl.recv(4096)
        cl.close()
        self.assertIn(b" 502 ", hdr.split(b"\r\n", 1)[0])


if __name__ == "__main__":
    sys.exit(unittest.main())
