#!/usr/bin/env python3
"""Stdlib unittest for `ferry relay` + `ferry expose` — the reverse tunnel.

Run:  python3 lib/ferry-relay.test.py

The claim under test is a byte path, so the tests move real bytes: a dummy
service on a random loopback port, a real relay process, a real expose process,
and a socket connecting to the published port from the outside. Nothing here
reimplements the protocol — a regression in the handshake, the parking of an
accepted connection, or the teardown shows up as bytes that do not arrive.

The property that matters most is the teardown one: a port published on the host
on someone else's behalf must close when that someone goes away. A tunnel that
outlives its client is an open port nobody remembers opening.
"""
import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FERRY = os.path.join(REPO, "ferry")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class EchoService(threading.Thread):
    """The 'local service' on the client: echoes whatever it is sent."""

    def __init__(self):
        super().__init__(daemon=True)
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self.stop = False

    def run(self):
        while not self.stop:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self.serve, args=(conn,), daemon=True).start()

    def serve(self, conn):
        try:
            while True:
                data = conn.recv(65536)
                if not data:
                    break
                conn.sendall(data)
        except OSError:
            pass
        finally:
            conn.close()

    def shutdown(self):
        self.stop = True
        try:
            self.sock.close()
        except OSError:
            pass


class RelayTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="ferry-relay-home-")
        self.tmp = tempfile.mkdtemp(prefix="ferry-relay-tmp-")
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        os.makedirs(os.path.join(self.tmp, "ferry-logs"), exist_ok=True)
        self.echo = EchoService()
        self.echo.start()
        self.addCleanup(self.echo.shutdown)
        self.relay_port = free_port()
        self.public_port = free_port()
        self.procs = []

    def env(self):
        e = os.environ.copy()
        e["HOME"] = self.home
        e["TMPDIR"] = self.tmp
        e.pop("FERRY_RELAY_TOKEN", None)
        return e

    def ferry(self, *args, capture=True):
        p = subprocess.Popen(["zsh", FERRY, *args], env=self.env(),
                             stdout=subprocess.PIPE if capture else None,
                             stderr=subprocess.STDOUT if capture else None, text=True)
        self.procs.append(p)
        self.addCleanup(self.kill, p)
        return p

    def kill(self, p):
        if p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()

    def run_ferry(self, *args, timeout=60):
        return subprocess.run(["zsh", FERRY, *args], env=self.env(),
                              capture_output=True, text=True, timeout=timeout)

    # --- fixtures -----------------------------------------------------------
    def start_relay(self, bind="127.0.0.1"):
        p = self.ferry("relay", "--foreground", "--port", str(self.relay_port), "--bind", bind)
        self.wait_for_port(self.relay_port, "the relay control port")
        return p

    def token(self):
        with open(os.path.join(self.home, ".config", "ferry", "relay-token")) as f:
            return f.read().strip()

    def start_expose(self, local=None, public=None, token=None):
        p = self.ferry("expose", str(local if local is not None else self.echo.port),
                       "--as", str(public if public is not None else self.public_port),
                       "--host", "127.0.0.1", "--port", str(self.relay_port),
                       "--token", token if token is not None else self.token())
        return p

    def wait_for_port(self, port, what, deadline=15.0, want_open=True):
        end = time.time() + deadline
        while time.time() < end:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                    if want_open:
                        return True
            except OSError:
                if not want_open:
                    return True
            time.sleep(0.15)
        self.fail(f"{what} was never {'open' if want_open else 'closed'} on port {port}")

    def round_trip(self, payload=b"hello over the tunnel\n", port=None):
        with socket.create_connection(("127.0.0.1", port or self.public_port), timeout=10) as s:
            s.sendall(payload)
            got = bytearray()
            s.settimeout(10)
            while len(got) < len(payload):
                chunk = s.recv(65536)
                if not chunk:
                    break
                got += chunk
        return bytes(got)

    def state(self):
        path = os.path.join(self.home, ".config", "ferry", "relay-published.json")
        if not os.path.exists(path):
            return {}
        with open(path) as f:
            return json.load(f)

    # --- the byte path ------------------------------------------------------
    def test_bytes_round_trip_through_the_tunnel(self):
        self.start_relay()
        self.start_expose()
        self.wait_for_port(self.public_port, "the published port")
        self.assertEqual(self.round_trip(), b"hello over the tunnel\n")

    def test_a_large_payload_survives_the_pump(self):
        self.start_relay()
        self.start_expose()
        self.wait_for_port(self.public_port, "the published port")
        payload = os.urandom(512 * 1024)
        self.assertEqual(self.round_trip(payload), payload)

    def test_several_visitors_at_once(self):
        self.start_relay()
        self.start_expose()
        self.wait_for_port(self.public_port, "the published port")
        results = {}

        def visit(i):
            results[i] = self.round_trip(f"visitor {i}\n".encode())

        threads = [threading.Thread(target=visit, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        for i in range(5):
            self.assertEqual(results.get(i), f"visitor {i}\n".encode(), f"visitor {i}")

    # --- lifetime -----------------------------------------------------------
    def test_the_published_port_closes_when_the_client_goes_away(self):
        """Kill the pid you started — nothing may keep publishing behind it.

        This failed the first time it ran: `ferry expose` ran its tunnel as a
        CHILD of the zsh wrapper, so terminating the process the supervisor knows
        about killed the wrapper and left the tunnel — and the host's published
        port — very much alive. The fix was to exec the tunnel in place.
        """
        self.start_relay()
        expose = self.start_expose()
        self.wait_for_port(self.public_port, "the published port")
        self.assertIn(str(self.public_port), self.state())

        self.kill(expose)

        self.wait_for_port(self.public_port, "the published port", want_open=False)
        end = time.time() + 10
        while time.time() < end and str(self.public_port) in self.state():
            time.sleep(0.2)
        self.assertNotIn(str(self.public_port), self.state(),
                         "the relay still advertises a port whose client is gone")

    def test_status_state_names_the_client_and_the_bind(self):
        self.start_relay()
        self.start_expose()
        self.wait_for_port(self.public_port, "the published port")
        entry = self.state()[str(self.public_port)]
        self.assertEqual(entry["bind"], "127.0.0.1")
        self.assertEqual(entry["client"], "127.0.0.1")
        self.assertTrue(entry["since"])

    def test_a_visitor_is_dropped_cleanly_when_the_local_service_is_down(self):
        """The tunnel must not hang or die when the thing behind it isn't there."""
        self.start_relay()
        dead_port = free_port()          # nothing listening
        self.start_expose(local=dead_port)
        self.wait_for_port(self.public_port, "the published port")

        with socket.create_connection(("127.0.0.1", self.public_port), timeout=10) as s:
            s.sendall(b"anyone home?\n")
            s.settimeout(10)
            self.assertEqual(s.recv(100), b"")   # closed, not hung

        # ...and the tunnel still works for a service that IS up.
        self.kill(self.procs[-1])
        self.public_port = free_port()
        self.start_expose()
        self.wait_for_port(self.public_port, "the second published port")
        self.assertEqual(self.round_trip(b"still alive\n"), b"still alive\n")

    # --- refusals -----------------------------------------------------------
    def test_a_bad_token_is_refused(self):
        self.start_relay()
        p = self.run_ferry("expose", str(self.echo.port), "--as", str(self.public_port),
                           "--host", "127.0.0.1", "--port", str(self.relay_port),
                           "--token", "not-the-token")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("bad token", p.stdout + p.stderr)
        # Nothing was published on the strength of a wrong token.
        self.assertEqual(self.state(), {})

    def test_ferrys_own_ports_cannot_be_published(self):
        self.start_relay()
        p = self.run_ferry("expose", str(self.echo.port), "--as", "8090",
                           "--host", "127.0.0.1", "--port", str(self.relay_port),
                           "--token", self.token())
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("belongs to ferry itself", p.stdout + p.stderr)

    def test_expose_without_a_token_says_where_to_get_one(self):
        p = self.run_ferry("expose", "4290", "--host", "127.0.0.1")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("ferry relay --token", p.stdout + p.stderr)

    def test_expose_without_a_host_or_profile_is_refused(self):
        p = self.run_ferry("expose", "4290", "--token", "whatever")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("no host", (p.stdout + p.stderr).lower())

    def test_relay_is_host_only(self):
        os.makedirs(os.path.join(self.home, ".config", "ferry"), exist_ok=True)
        with open(os.path.join(self.home, ".config", "ferry", "client.json"), "w") as f:
            json.dump({"host": "somewhere.local", "port": "8090"}, f)
        p = self.run_ferry("relay", "--port", str(self.relay_port))
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("only available on the LLM-Ferry Host", p.stdout + p.stderr)
        self.assertIn("ferry expose", p.stdout + p.stderr)

    # --- the background launch ----------------------------------------------
    def test_the_background_launch_listens_and_is_killable_by_ferry_down(self):
        """Everything else here uses --foreground; this covers the real entry point.

        `ferry relay` with no flags re-invokes the script in --foreground under
        nohup, which depends on $FERRY_BIN_PATH being the script's own resolved
        path (inside a function `$0` is the function name, so it cannot be
        computed there) and on the sentinel arg reaching argv, which is the only
        thing `ferry down` can match on. Both are invisible to the foreground path.

        It kills the process it started by pid rather than running `ferry down`:
        that command pkills by pattern across the whole machine and would take
        down a relay — or a whole stack — the person running these tests is using.
        """
        out = self.run_ferry("relay", "--port", str(self.relay_port), "--bind", "127.0.0.1")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("Relay running in the background", out.stdout)

        pid = subprocess.run(["lsof", "-t", f"-iTCP:{self.relay_port}", "-sTCP:LISTEN"],
                             capture_output=True, text=True).stdout.strip().split("\n")[0]
        self.assertTrue(pid, "nothing is listening on the relay port")
        self.addCleanup(subprocess.run, ["kill", pid])

        argv = subprocess.run(["ps", "-p", pid, "-o", "args="],
                              capture_output=True, text=True).stdout
        self.assertIn("ferry-relay-marker", argv,
                      "the sentinel `ferry down` matches on never reached argv")

        # And it is a working relay, not just a process holding a port.
        self.start_expose()
        self.wait_for_port(self.public_port, "the published port")
        self.assertEqual(self.round_trip(b"backgrounded\n"), b"backgrounded\n")

    # --- the token ----------------------------------------------------------
    def test_the_token_file_is_created_private_and_reused(self):
        self.run_ferry("relay", "--token")
        path = os.path.join(self.home, ".config", "ferry", "relay-token")
        self.assertTrue(os.path.exists(path))
        self.assertEqual(oct(os.stat(path).st_mode)[-3:], "600")
        first = self.token()
        self.run_ferry("relay", "--token")
        self.assertEqual(first, self.token(), "the token must be stable across runs")


if __name__ == "__main__":
    unittest.main(verbosity=2)
