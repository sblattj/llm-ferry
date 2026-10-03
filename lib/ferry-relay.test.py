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
import re
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


def relay_status_source():
    """The python `cmd_status`'s reverse-relay listing heredocs into python3 —
    the FIRST `<<'PYEOF'` block after `cmd_status() {` in the built ferry (the
    VNC-viewer block's heredoc, which prints `Screen ...` lines instead of
    `Published ...` lines, is the second one and is not what this reads).
    That opening line reads `<<'PYEOF' || true`, not a bare `<<'PYEOF'`
    immediately followed by a newline, so the marker is split off first and
    its own line (` || true`) is dropped before taking the heredoc body."""
    with open(FERRY) as f:
        src = f.read()
    body = src[src.index("cmd_status() {"):]
    after_marker = body.split("<<'PYEOF'", 1)[1]
    after_open_line = after_marker.split("\n", 1)[1]
    return after_open_line.split("\nPYEOF", 1)[0]


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


class RfbService(EchoService):
    """A fake VNC server: greets with the RFB ProtocolVersion, then echoes."""

    def serve(self, conn):
        try:
            conn.sendall(b"RFB 003.008\n")
        except OSError:
            conn.close()
            return
        super().serve(conn)


class SshService(EchoService):
    """A fake sshd: greets with an SSH identification string, then echoes."""

    def serve(self, conn):
        try:
            conn.sendall(b"SSH-2.0-OpenSSH_9.6\r\n")
        except OSError:
            conn.close()
            return
        super().serve(conn)


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

    def state_path(self):
        return os.path.join(self.home, ".config", "ferry", "relay-published.json")

    def state(self):
        path = self.state_path()
        if not os.path.exists(path):
            return {}
        with open(path) as f:
            return json.load(f)

    def status_kind_tags(self):
        """Run the REAL `cmd_status` relay-listing python (extracted verbatim from
        the built ferry) against this test's real state file, and return its
        stdout. `ferry status` itself can't be used here: its relay block gates
        on `lsof -nP -iTCP:"$RELAY_PORT"`, and $RELAY_PORT is the fixed global
        8098 while this suite's relay listens on a free_port() to allow parallel
        runs — faking a listener on 8098 to make that gate pass is exactly what
        the review told this suite not to do, so this runs the actual print
        logic directly instead of reimplementing it."""
        return subprocess.run(["python3", "-", self.state_path()],
                              input=relay_status_source(), capture_output=True,
                              text=True, timeout=10)

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

    def test_plain_expose_is_recorded_as_kind_tcp(self):
        self.start_relay()
        self.start_expose()
        self.wait_for_port(self.public_port, "the published port")
        entry = self.state()[str(self.public_port)]
        self.assertEqual(entry["kind"], "tcp")
        r = self.status_kind_tags()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("[tcp]", r.stdout)

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


class ExposeVncTest(RelayTest):
    def start_expose_vnc(self, local):
        return self.ferry("expose-vnc", "--local", str(local), "--as", str(self.public_port),
                          "--host", "127.0.0.1", "--port", str(self.relay_port),
                          "--token", self.token())

    def test_local_port_must_be_a_number(self):
        r = self.run_ferry("expose-vnc", "--local", "abc")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("must be a port number", r.stdout + r.stderr)
        self.assertNotIn("Traceback", r.stdout + r.stderr)

    def test_refuses_a_local_port_that_is_not_rfb(self):
        self.start_relay()
        r = self.run_ferry("expose-vnc", "--local", str(self.echo.port), "--as", str(self.public_port),
                           "--host", "127.0.0.1", "--port", str(self.relay_port), "--token", self.token())
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("not a VNC server", r.stdout)
        self.assertNotIn(str(self.public_port), self.state())

    def test_refuses_a_closed_local_port(self):
        self.start_relay()
        r = self.run_ferry("expose-vnc", "--local", str(free_port()), "--as", str(self.public_port),
                           "--host", "127.0.0.1", "--port", str(self.relay_port), "--token", self.token())
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("Screen Sharing", r.stdout)

    def test_publishes_an_rfb_service_as_kind_vnc(self):
        rfb = RfbService()
        rfb.start()
        self.addCleanup(rfb.shutdown)
        self.start_relay()
        self.start_expose_vnc(rfb.port)
        self.wait_for_port(self.public_port, "the published VNC port")
        self.assertEqual(self.state()[str(self.public_port)]["kind"], "vnc")
        with socket.create_connection(("127.0.0.1", self.public_port), timeout=10) as s:
            self.assertEqual(s.recv(12), b"RFB 003.008\n")
        r = self.status_kind_tags()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("[vnc]", r.stdout)


class ExposeTmuxTest(RelayTest):
    def tmux_args(self, local):
        return ("expose-tmux", "--local", str(local), "--as", str(self.public_port),
                "--host", "127.0.0.1", "--port", str(self.relay_port), "--token", self.token())

    def test_local_port_must_be_a_number(self):
        r = self.run_ferry("expose-tmux", "--local", "abc")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("must be a port number", r.stdout + r.stderr)
        self.assertNotIn("Traceback", r.stdout + r.stderr)

    def test_refuses_a_local_port_that_is_not_ssh(self):
        if not shutil.which("tmux"):
            self.skipTest("tmux is not installed here")
        self.start_relay()
        r = self.run_ferry(*self.tmux_args(self.echo.port))
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("not an SSH server", r.stdout)
        self.assertNotIn(str(self.public_port), self.state())

    def test_refuses_a_closed_local_port(self):
        if not shutil.which("tmux"):
            self.skipTest("tmux is not installed here")
        self.start_relay()
        r = self.run_ferry(*self.tmux_args(free_port()))
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("Remote Login", r.stdout)

    def test_refuses_when_tmux_is_not_on_path(self):
        bindir = os.path.join(self.tmp, "nobin")
        os.makedirs(bindir)
        for tool in ("python3", "zsh"):
            os.symlink(shutil.which(tool), os.path.join(bindir, tool))
        path = bindir + ":/usr/bin:/bin:/usr/sbin:/sbin"
        if shutil.which("tmux", path=path):
            self.skipTest("a system tmux is on the minimal PATH")
        env = self.env()
        env["PATH"] = path
        r = subprocess.run(["zsh", FERRY, "expose-tmux", "--local", str(self.echo.port)],
                           env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("brew install tmux", r.stdout)

    def test_publishes_an_ssh_service_as_kind_tmux_with_the_user(self):
        if not shutil.which("tmux"):
            self.skipTest("tmux is not installed here")
        ssh = SshService()
        ssh.start()
        self.addCleanup(ssh.shutdown)
        self.start_relay()
        p = self.ferry(*self.tmux_args(ssh.port))
        self.wait_for_port(self.public_port, "the published ssh port")
        entry = self.state()[str(self.public_port)]
        self.assertEqual(entry["kind"], "tmux")
        self.assertEqual(entry.get("user"), os.environ.get("USER", ""))
        with socket.create_connection(("127.0.0.1", self.public_port), timeout=10) as s:
            self.assertTrue(s.recv(8).startswith(b"SSH-"))
        self.kill(p)
        out = p.stdout.read()
        self.assertIn("ferry tmux", out)

    def test_default_public_port_is_the_tmux_port(self):
        # Without --as, expose-tmux asks the relay for TMUX_PORT.
        with open(FERRY) as f:
            m = re.search(r'^TMUX_PORT="(\d+)"', f.read(), re.M)
        self.assertIsNotNone(m, "TMUX_PORT is not defined in ferry")
        self.assertEqual(m.group(1), "8101")


class RelayUserFieldTest(RelayTest):
    def register(self, **extra):
        s = socket.create_connection(("127.0.0.1", self.relay_port), timeout=10)
        self.addCleanup(s.close)
        msg = {"op": "register", "token": self.token(), "public_port": self.public_port,
               "label": "box", "kind": "tmux"}
        msg.update(extra)
        s.sendall((json.dumps(msg) + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            buf += s.recv(1)
        reply = json.loads(buf)
        # The relay answers before it writes the state file; wait for the entry.
        end = time.time() + 10
        while time.time() < end and str(self.public_port) not in self.state():
            time.sleep(0.1)
        return reply

    def test_hostile_user_is_dropped(self):
        self.start_relay()
        self.assertTrue(self.register(user="a b;rm")["ok"])
        entry = self.state()[str(self.public_port)]
        self.assertEqual(entry["kind"], "tmux")
        self.assertNotIn("user", entry)

    def test_valid_user_is_stored_and_old_clients_still_work(self):
        self.start_relay()
        self.assertTrue(self.register(user="stephen.b-1_x")["ok"])
        self.assertEqual(self.state()[str(self.public_port)]["user"], "stephen.b-1_x")

    def test_missing_user_registers_without_a_user_key(self):
        self.start_relay()
        self.assertTrue(self.register()["ok"])
        self.assertNotIn("user", self.state()[str(self.public_port)])


class RelayHostKeyTest(RelayTest):
    def key_path(self):
        return os.path.join(self.home, ".config", "ferry", "tmux_ed25519")

    def ask(self, token):
        with socket.create_connection(("127.0.0.1", self.relay_port), timeout=10) as s:
            s.sendall((json.dumps({"op": "hostkey", "token": token}) + "\n").encode())
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = s.recv(1)
                if not chunk:
                    break
                buf += chunk
        return json.loads(buf)

    def test_starting_the_relay_creates_the_tmux_key(self):
        self.start_relay()
        self.assertTrue(os.path.exists(self.key_path()))
        self.assertTrue(os.path.exists(self.key_path() + ".pub"))
        self.assertEqual(oct(os.stat(self.key_path()).st_mode)[-3:], "600")
        with open(self.key_path() + ".pub") as f:
            self.assertTrue(f.read().startswith("ssh-ed25519 "))

    def test_the_key_is_not_regenerated(self):
        self.start_relay()
        with open(self.key_path() + ".pub") as f:
            first = f.read()
        self.run_ferry("relay", "--port", str(free_port()), "--token")  # --token returns early
        with open(self.key_path() + ".pub") as f:
            self.assertEqual(first, f.read())

    def test_hostkey_op_returns_the_public_key_for_the_right_token(self):
        self.start_relay()
        reply = self.ask(self.token())
        with open(self.key_path() + ".pub") as f:
            self.assertEqual(reply, {"ok": True, "pubkey": f.read().strip()})

    def test_hostkey_op_refuses_a_wrong_token(self):
        self.start_relay()
        reply = self.ask("not-the-token")
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["error"], "bad token")
        self.assertNotIn("pubkey", reply)

    def test_sshd_user_is_recorded_and_other_values_are_ignored(self):
        self.start_relay()
        for value, expect in (("user", "user"), ("system", None)):
            port = free_port()
            s = socket.create_connection(("127.0.0.1", self.relay_port), timeout=10)
            self.addCleanup(s.close)
            s.sendall((json.dumps({"op": "register", "token": self.token(), "public_port": port,
                                   "label": "box", "kind": "tmux", "sshd": value}) + "\n").encode())
            buf = b""
            while not buf.endswith(b"\n"):
                buf += s.recv(1)
            end = time.time() + 10
            while time.time() < end and str(port) not in self.state():
                time.sleep(0.1)
            self.assertEqual(self.state()[str(port)].get("sshd"), expect)


class ExposeTmuxUserSshdTest(RelayTest):
    """The no-sudo path, end to end with a REAL sshd and a REAL ssh client."""

    def setUp(self):
        super().setUp()
        if not (os.path.exists("/usr/sbin/sshd") and shutil.which("ssh") and shutil.which("ssh-keygen")):
            self.skipTest("needs /usr/sbin/sshd, ssh and ssh-keygen")
        self.bin = os.path.join(self.tmp, "stubbin")
        os.makedirs(self.bin)
        stub = os.path.join(self.bin, "tmux")
        with open(stub, "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(stub, 0o755)

    def env(self):
        e = super().env()
        e["PATH"] = self.bin + ":" + e["PATH"]
        return e

    def alive(self, pid):
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False

    def wait_until(self, cond, what, deadline=20.0):
        end = time.time() + deadline
        while time.time() < end:
            if cond():
                return
            time.sleep(0.2)
        self.fail(f"timed out waiting for {what}")

    def ssh(self, key, port, remote_cmd="echo FERRY-USER-SSHD-OK"):
        user = os.environ.get("USER") or subprocess.run(["id", "-un"], capture_output=True, text=True).stdout.strip()
        return subprocess.run(
            ["ssh", "-F", "/dev/null", "-p", str(port), "-i", key, "-o", "IdentitiesOnly=yes",
             "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
             "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", f"{user}@127.0.0.1", remote_cmd],
            capture_output=True, text=True, timeout=60)

    def start_user_sshd_expose(self):
        self.start_relay()
        p = self.ferry("expose-tmux", "--user-sshd", "--host", "127.0.0.1",
                       "--port", str(self.relay_port), "--token", self.token(),
                       "--as", str(self.public_port))
        self.wait_until(lambda: self.state().get(str(self.public_port), {}).get("sshd") == "user",
                        "the relay to list the user-sshd entry")
        pidfile = os.path.join(self.home, ".config", "ferry", "tmux-sshd", "sshd.pid")
        self.wait_until(lambda: os.path.exists(pidfile), "the sshd pid file")
        with open(pidfile) as f:
            sshd_pid = int(f.read().strip())
        self.addCleanup(lambda: self.alive(sshd_pid) and os.kill(sshd_pid, 9))
        return p, sshd_pid

    def host_key(self):
        return os.path.join(self.home, ".config", "ferry", "tmux_ed25519")

    def test_host_key_logs_in_other_key_does_not_and_sigterm_stops_the_sshd(self):
        p, sshd_pid = self.start_user_sshd_expose()
        entry = self.state()[str(self.public_port)]
        self.assertEqual(entry["kind"], "tmux")
        # The sshd listens on 127.0.0.1 only.
        ok = self.ssh(self.host_key(), self.public_port)
        self.assertIn("FERRY-USER-SSHD-OK", ok.stdout, ok.stdout + ok.stderr)
        # Control: a different key is refused (pubkey-only, one authorized key).
        other = os.path.join(self.tmp, "other_key")
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", other], check=True)
        bad = self.ssh(other, self.public_port)
        self.assertNotEqual(bad.returncode, 0)
        self.assertNotIn("FERRY-USER-SSHD-OK", bad.stdout)
        # authorized_keys holds exactly the host's pubkey.
        with open(self.host_key() + ".pub") as f:
            pub = f.read().strip()
        with open(os.path.join(self.home, ".config", "ferry", "tmux-sshd", "authorized_keys")) as f:
            self.assertEqual(f.read().strip(), pub)
        # kill <pid> (SIGTERM): the sshd goes and the port is unpublished.
        p.terminate()
        p.wait(timeout=15)
        self.wait_until(lambda: not self.alive(sshd_pid), "the sshd to exit after SIGTERM")
        self.wait_until(lambda: str(self.public_port) not in self.state(), "the relay to unpublish")

    def test_sigint_also_stops_the_sshd(self):
        import signal
        p, sshd_pid = self.start_user_sshd_expose()
        p.send_signal(signal.SIGINT)
        p.wait(timeout=15)
        self.wait_until(lambda: not self.alive(sshd_pid), "the sshd to exit after SIGINT")

    def test_an_older_relay_that_closes_without_answering_is_explained(self):
        # A fake "relay" that accepts and closes, like a relay that predates the hostkey op.
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(4)
        self.addCleanup(srv.close)

        def closer():
            while True:
                try:
                    c, _ = srv.accept()
                except OSError:
                    return
                c.close()
        threading.Thread(target=closer, daemon=True).start()
        r = self.run_ferry("expose-tmux", "--user-sshd", "--host", "127.0.0.1",
                           "--port", str(srv.getsockname()[1]), "--token", "x")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("predates this feature", r.stdout)
        self.assertIn("ferry update", r.stdout)

    def test_a_wrong_token_prints_the_token_hint(self):
        self.start_relay()
        r = self.run_ferry("expose-tmux", "--user-sshd", "--host", "127.0.0.1",
                           "--port", str(self.relay_port), "--token", "wrong")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("bad token", r.stdout)
        self.assertIn("ferry relay --token", r.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
