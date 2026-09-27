#!/usr/bin/env python3
"""Stdlib unittest for the share server's client-script injection.

Run:  python3 lib/ferry-share.test.py

`ferry share` does not serve the client scripts statically. It rewrites three
placeholders on the way out — HOST_MDNS_PLACEHOLDER, SHARE_PORT_PLACEHOLDER, and
the literal `your-host.local` fallback — so the copy a client pipes into zsh
already knows which machine to talk to.

The list of scripts that get this treatment was hardcoded to one name. Adding
client-reset.sh without adding it to that list produces the worst possible
failure: HTTP 200, a script that looks fine, and placeholders reaching the
client verbatim, where HOST_MDNS_PLACEHOLDER resolves to nothing and the first
curl fails against a host that does not exist. Nothing errors on the host side.

These tests run the REAL embedded server — extracted from the built `ferry`, not
a reimplementation — against a temp directory, so a change to the handler that
breaks injection fails here rather than on someone's laptop.
"""
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
import urllib.error
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FERRY = os.path.join(REPO, "ferry")

PLACEHOLDERS = ("HOST_MDNS_PLACEHOLDER", "SHARE_PORT_PLACEHOLDER", "your-host.local")

# Every client-facing script the repo ships. Each must be injected, and each
# must actually carry the placeholders for injection to have anything to do.
CLIENT_SCRIPTS = ("client-bootstrap.sh", "client-reset.sh")

STUB = """#!/bin/zsh
HOST_NAME="${HOST_NAME:-HOST_MDNS_PLACEHOLDER}"
SHARE_PORT="${SHARE_PORT:-SHARE_PORT_PLACEHOLDER}"
if [[ "$HOST_NAME" == "HOST_MDNS_PLACEHOLDER" ]]; then
  HOST_NAME="your-host.local"
fi
echo "em-dash payload — multi-byte, guards the Content-Length byte count"
"""


def extract_embedded_server():
    """Pull the python HTTP server out of the built `ferry`'s cmd_share heredoc."""
    with open(FERRY) as f:
        text = f.read()
    m = re.search(r"nohup python3 - .*?<<'PYEOF'.*?\n(.*?)\nPYEOF\n", text, re.S)
    if not m:
        raise AssertionError("could not find the share server heredoc in ./ferry")
    return m.group(1)


def read_repo(*parts):
    with open(os.path.join(REPO, *parts)) as f:
        return f.read()


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class ShareServerCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp(prefix="ferry-share-")
        for name in CLIENT_SCRIPTS:
            with open(os.path.join(cls.dir, name), "w") as f:
                f.write(STUB)
        # A client-facing-looking script that is NOT on the injection list, to
        # prove the handler is selective rather than rewriting every .sh.
        with open(os.path.join(cls.dir, "unrelated.sh"), "w") as f:
            f.write(STUB)

        cls.server_py = os.path.join(cls.dir, "_server.py")
        with open(cls.server_py, "w") as f:
            f.write(extract_embedded_server())

        cls.port = free_port()
        cls.proc = subprocess.Popen(
            [sys.executable, cls.server_py, str(cls.port), cls.dir, "ferry-share-marker"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{cls.port}/manifest", timeout=1).read()
                break
            except Exception:
                if cls.proc.poll() is not None:
                    raise AssertionError(f"share server died:\n{cls.proc.stdout.read()}")
                time.sleep(0.15)
        else:
            raise AssertionError("share server never came up")

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        try:
            cls.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            cls.proc.kill()
            cls.proc.wait(timeout=10)
        cls._drain()
        shutil.rmtree(cls.dir, ignore_errors=True)

    def get(self, path):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=10) as r:
            return r.status, r.headers, r.read()

    @classmethod
    def _drain(cls):
        if cls.proc.stdout and not cls.proc.stdout.closed:
            cls.proc.stdout.close()


class TestClientScriptInjection(ShareServerCase):
    def test_every_client_script_is_injected(self):
        for name in CLIENT_SCRIPTS:
            with self.subTest(script=name):
                status, _, body = self.get(f"/{name}")
                self.assertEqual(status, 200)
                text = body.decode()
                for ph in PLACEHOLDERS:
                    self.assertNotIn(ph, text, f"{name} served with {ph} un-substituted")

    def test_injected_host_is_a_real_dotlocal_name(self):
        _, _, body = self.get("/client-reset.sh")
        text = body.decode()
        self.assertRegex(text, r'HOST_NAME:-[\w.-]+\.local')

    def test_injected_port_is_the_live_share_port(self):
        _, _, body = self.get("/client-reset.sh")
        self.assertIn(f"SHARE_PORT:-{self.port}", body.decode())

    def test_content_length_counts_bytes_not_characters(self):
        # The scripts contain multi-byte UTF-8. A char-count header truncates the
        # tail, and the client's zsh reports it as an unmatched quote at EOF —
        # a failure that points nowhere near the header that caused it.
        for name in CLIENT_SCRIPTS:
            with self.subTest(script=name):
                _, headers, body = self.get(f"/{name}")
                self.assertEqual(int(headers["Content-Length"]), len(body))
                self.assertIn("—", body.decode())

    def test_unlisted_scripts_are_served_verbatim(self):
        _, _, body = self.get("/unrelated.sh")
        self.assertIn("HOST_MDNS_PLACEHOLDER", body.decode())


class TestShippedClientScripts(unittest.TestCase):
    """The real scripts must carry what the server expects to rewrite."""

    def test_each_client_script_exists_and_is_executable(self):
        for name in CLIENT_SCRIPTS:
            p = os.path.join(REPO, name)
            self.assertTrue(os.path.exists(p), f"{name} is missing")
            self.assertTrue(os.access(p, os.X_OK), f"{name} is not executable")

    def test_each_client_script_carries_the_placeholders(self):
        # Injection is a no-op on a script that hardcodes a host, and the result
        # is a client silently pointed at whatever the author's machine was.
        for name in CLIENT_SCRIPTS:
            with self.subTest(script=name):
                text = read_repo(name)
                self.assertIn("HOST_MDNS_PLACEHOLDER", text)
                self.assertIn("SHARE_PORT_PLACEHOLDER", text)

    def test_server_injection_list_matches_the_shipped_scripts(self):
        # Guards the actual regression: a new client script added to the repo but
        # never added to the handler's INJECTED tuple.
        module = read_repo("lib", "ferry-share.zsh")
        m = re.search(r"INJECTED\s*=\s*\(([^)]*)\)", module)
        self.assertIsNotNone(m, "no INJECTED tuple in lib/ferry-share.zsh")
        listed = set(re.findall(r'"([^"]+)"', m.group(1)))
        self.assertEqual(listed, set(CLIENT_SCRIPTS))

    def test_the_share_server_never_injects_the_master_key(self):
        # The share server is unauthenticated, so the v1.22.0 master key must
        # never ride the served script — that would publish it to the LAN.
        # Comment lines may DOCUMENT the omission; no code may reference it.
        code = "\n".join(line for line in read_repo("lib", "ferry-share.zsh").splitlines()
                         if not line.lstrip().startswith("#"))
        self.assertNotIn("master_key", code)
        self.assertNotIn("MASTER_KEY", code)

    def test_reset_validates_the_download_before_overwriting(self):
        # A share server that is down, or a proxy serving an error page, must not
        # be able to replace a working CLI with an HTML 404.
        text = read_repo("client-reset.sh")
        self.assertIn("mktemp", text)
        self.assertIn("cmd_opencode()", text)
        self.assertIn("zsh -n", text)
        # ...and the move must happen only after those checks.
        self.assertLess(text.index("zsh -n"), text.index('mv "$tmp_ferry"'))

    def test_reset_neutralises_an_inherited_opencode_config(self):
        text = read_repo("client-reset.sh")
        self.assertIn("env -u OPENCODE_CONFIG", text)

    def test_reset_passes_the_host_explicitly(self):
        # Without --host/--port, a ferry that cannot find a client profile
        # concludes it is running ON the host and wires the config to
        # 127.0.0.1 — which on a client points opencode at itself. Observed
        # against a scratch HOME before this was added.
        text = read_repo("client-reset.sh")
        self.assertIn('--host "$HOST_NAME" --port "$HOST_PORT"', text)

    def test_reset_covers_all_three_targets(self):
        text = read_repo("client-reset.sh")
        for target in ("opencode/opencode.json",
                       "ferry/opencode-cloud.json",
                       "ferry/opencode-local.json"):
            self.assertIn(target, text)
        self.assertIn("--local", text)


class TestClientTelemetryLogPath(unittest.TestCase):
    """`ferry msg` / `ferry log` must outlive the checkout the server started in.

    The handler used to build its path from the serving directory, captured once
    at startup. A share server launched from a git worktree that was later
    removed turned every /hq POST into an unhandled exception and a bare 500:
    the client saw a failed send, the host logged nothing a human reads, and the
    message was gone. Observed 2026-08-26 — two messages lost that way.
    """

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="ferry-hqhome-")
        self.addCleanup(shutil.rmtree, self.home, True)
        # The tree the server is launched from — deliberately separate from HOME,
        # and deliberately deletable.
        self.serve = tempfile.mkdtemp(prefix="ferry-hqserve-")
        self.addCleanup(shutil.rmtree, self.serve, True)

        server_py = os.path.join(self.home, "_server.py")
        with open(server_py, "w") as f:
            f.write(extract_embedded_server())

        self.port = free_port()
        self.proc = subprocess.Popen(
            [sys.executable, server_py, str(self.port), self.serve, "ferry-share-marker"],
            env=dict(os.environ, HOME=self.home),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.addCleanup(self._stop)

        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{self.port}/manifest", timeout=1).read()
                return
            except Exception:
                if self.proc.poll() is not None:
                    raise AssertionError(f"share server died:\n{self.proc.stdout.read()}")
                time.sleep(0.15)
        raise AssertionError("share server never came up")

    def _stop(self):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=10)
        if self.proc.stdout and not self.proc.stdout.closed:
            self.proc.stdout.close()

    def post_hq(self, text):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/hq",
                                     data=text.encode(), method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status

    @property
    def log_path(self):
        return os.path.join(self.home, ".config", "ferry", "client_logs.txt")

    def test_telemetry_lands_outside_the_serving_directory(self):
        self.assertEqual(self.post_hq("hello from a client"), 200)
        self.assertTrue(os.path.exists(self.log_path),
                        "client telemetry did not reach ~/.config/ferry/client_logs.txt")
        with open(self.log_path) as f:
            self.assertIn("hello from a client", f.read())
        self.assertFalse(os.path.exists(os.path.join(self.serve, "client_logs.txt")),
                         "telemetry was written into the serving directory")

    def test_the_directory_is_created_if_absent(self):
        # A fresh host may never have run anything that makes ~/.config/ferry.
        self.assertFalse(os.path.exists(os.path.dirname(self.log_path)))
        self.assertEqual(self.post_hq("first ever message"), 200)
        self.assertTrue(os.path.exists(self.log_path))

    def test_a_deleted_serving_directory_does_not_lose_the_message(self):
        # The actual regression: the checkout the server was launched from goes
        # away (a removed worktree), and every send after that 500s.
        self.assertEqual(self.post_hq("before"), 200)
        shutil.rmtree(self.serve, ignore_errors=True)
        self.assertEqual(self.post_hq("after the checkout vanished"), 200)
        with open(self.log_path) as f:
            body = f.read()
        self.assertIn("before", body)
        self.assertIn("after the checkout vanished", body)


class TestOfferedPayloads(unittest.TestCase):
    """`ferry offer` naming and `/file/<name>` payload integrity.

    Runs the REAL built `ferry offer` and the REAL embedded share server, both
    against a temp HOME, so the live ~/.config/ferry/offered.json is never read
    or written. Two defects are guarded here:

    - the manifest key was always the path's basename: no way to name a
      payload, and a second path with the same basename silently replaced the
      first;
    - `_tar_stream` dereferenced every symlink, which is right for the HF cache
      and voids the code signature of a macOS .app (its seal covers symlinks).
    """

    @classmethod
    def setUpClass(cls):
        cls.home = tempfile.mkdtemp(prefix="ferry-offerhome-")
        cls.work = tempfile.mkdtemp(prefix="ferry-offerwork-")
        server_py = os.path.join(cls.work, "_server.py")
        with open(server_py, "w") as f:
            f.write(extract_embedded_server())
        cls.port = free_port()
        cls.proc = subprocess.Popen(
            [sys.executable, server_py, str(cls.port), cls.work, "ferry-share-marker"],
            env=dict(os.environ, HOME=cls.home),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{cls.port}/manifest", timeout=1).read()
                break
            except Exception:
                if cls.proc.poll() is not None:
                    raise AssertionError(f"share server died:\n{cls.proc.stdout.read()}")
                time.sleep(0.15)
        else:
            raise AssertionError("share server never came up")

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        try:
            cls.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            cls.proc.kill()
            cls.proc.wait(timeout=10)
        if cls.proc.stdout and not cls.proc.stdout.closed:
            cls.proc.stdout.close()
        shutil.rmtree(cls.home, ignore_errors=True)
        shutil.rmtree(cls.work, ignore_errors=True)

    # -- helpers -----------------------------------------------------------
    @property
    def manifest_path(self):
        return os.path.join(self.home, ".config", "ferry", "offered.json")

    def offer(self, *args):
        return subprocess.run(["zsh", FERRY, "offer", *args],
                              env=dict(os.environ, HOME=self.home),
                              capture_output=True, text=True, timeout=120)

    def mkfile(self, rel, text="x\n"):
        p = os.path.join(self.work, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(text)
        return p

    def fetch(self, name):
        """GET /file/<name>; return (status, raw tar bytes)."""
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{self.port}/file/{name}", timeout=30) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            with e:
                return e.code, e.read()

    def members(self, body):
        with tarfile.open(fileobj=io.BytesIO(body), mode="r:") as t:
            return {m.name: m for m in t.getmembers()}

    def extract(self, body):
        """Unpack with the system tar, exactly as `ferry get` does."""
        dest = tempfile.mkdtemp(prefix="ferry-offerdest-", dir=self.work)
        subprocess.run(["tar", "-x", "-C", dest], input=body, check=True)
        return dest

    def set_entry(self, name, value):
        with open(self.manifest_path) as f:
            data = json.load(f)
        data[name] = value
        with open(self.manifest_path, "w") as f:
            json.dump(data, f)

    # -- item 1: naming ----------------------------------------------------
    def test_as_names_the_payload_and_the_basename_is_not_a_key(self):
        src = self.mkfile("naming/toolsdir/readme.txt", "tools\n")
        r = self.offer("--as", "tools", os.path.dirname(src))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("/file/tools", r.stdout, "offer must print the fetch URL")
        status, body = self.fetch("tools")
        self.assertEqual(status, 200)
        self.assertIn("toolsdir/readme.txt", self.members(body))
        self.assertEqual(self.fetch("toolsdir")[0], 404)

    def test_default_name_is_still_the_basename(self):
        src = self.mkfile("legacy/eval.jsonl", "{}\n")
        r = self.offer(src)
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(self.manifest_path) as f:
            # The historical plain-string shape, so older readers keep working.
            self.assertEqual(json.load(f)["eval.jsonl"], src)
        status, body = self.fetch("eval.jsonl")
        self.assertEqual(status, 200)
        self.assertIn("eval.jsonl", self.members(body))

    def test_a_basename_collision_is_refused_and_writes_nothing(self):
        first = self.mkfile("coll/one/payload.bin", "one\n")
        second = self.mkfile("coll/two/payload.bin", "two\n")
        self.assertEqual(self.offer(first).returncode, 0)
        with open(self.manifest_path, "rb") as f:
            before = f.read()
        # A second, valid path in the same call must not be written either.
        other = self.mkfile("coll/other.txt")
        r = self.offer(other, second)
        self.assertNotEqual(r.returncode, 0, "a collision must fail the command")
        self.assertIn("already offered", r.stderr)
        with open(self.manifest_path, "rb") as f:
            self.assertEqual(f.read(), before, "a refused offer changed the manifest")
        # Re-offering the SAME path is not a collision.
        self.assertEqual(self.offer(first).returncode, 0)
        # --replace is the explicit override.
        r = self.offer("--replace", second)
        self.assertEqual(r.returncode, 0, r.stderr)
        _, body = self.fetch("payload.bin")
        dest = self.extract(body)
        with open(os.path.join(dest, "payload.bin")) as f:
            self.assertEqual(f.read(), "two\n")

    def test_a_name_with_a_slash_is_refused(self):
        src = self.mkfile("slash/a.txt")
        r = self.offer("--as", "a/b", src)
        self.assertNotEqual(r.returncode, 0)

    # -- item 2: dereferencing ----------------------------------------------
    def _selftest_app(self):
        r = self.offer("--selftest")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        with open(self.manifest_path) as f:
            data = json.load(f)
        return data["ferry-selftest-app"]

    def test_symlinks_inside_an_app_stay_symlinks(self):
        self._selftest_app()
        status, body = self.fetch("ferry-selftest-app")
        self.assertEqual(status, 200)
        m = self.members(body)
        link = m["FerrySelftest.app/Contents/Resources/current"]
        self.assertTrue(link.issym(), "a symlink inside a .app was dereferenced")
        self.assertEqual(link.linkname, "data.txt")

    def test_symlinks_outside_an_app_are_still_dereferenced(self):
        self._selftest_app()
        status, body = self.fetch("ferry-selftest-tree")
        self.assertEqual(status, 200)
        link = self.members(body)["tree/link.txt"]
        self.assertTrue(link.isfile(), "default dereferencing regressed")

    @unittest.skipUnless(shutil.which("codesign"), "codesign is macOS-only")
    def test_a_ferried_app_keeps_a_valid_signature_and_deref_breaks_it(self):
        app = self._selftest_app()
        src = subprocess.run(["codesign", "--verify", "--deep", "--strict", app],
                             capture_output=True, text=True)
        self.assertEqual(src.returncode, 0, f"fixture is not validly signed: {src.stderr}")

        _, body = self.fetch("ferry-selftest-app")
        ok = subprocess.run(["codesign", "--verify", "--deep", "--strict",
                             os.path.join(self.extract(body), "FerrySelftest.app")],
                            capture_output=True, text=True)
        self.assertEqual(ok.returncode, 0, f"ferried .app fails verification: {ok.stderr}")

        # The control: force the old unconditional dereference for this entry.
        # Without a failing case, the passing one proves nothing.
        self.set_entry("ferry-selftest-app", {"path": app, "deref": True})
        try:
            _, body = self.fetch("ferry-selftest-app")
            bad = subprocess.run(["codesign", "--verify", "--deep", "--strict",
                                  os.path.join(self.extract(body), "FerrySelftest.app")],
                                 capture_output=True, text=True)
            self.assertNotEqual(bad.returncode, 0,
                                "dereferencing did not break the signature; the check cannot fail")
        finally:
            self.set_entry("ferry-selftest-app", app)

    def test_a_symlink_to_an_app_inside_a_tree_ships_the_bundle(self):
        # The provisioning pattern one level down: a plain directory of links,
        # one of which points at an .app. The link must become the bundle (not
        # a dangling absolute symlink), and the bundle's own links must survive.
        app = self._selftest_app()
        links = os.path.join(self.work, "applinks")
        os.makedirs(links)
        os.symlink(app, os.path.join(links, "Linked.app"))
        self.assertEqual(self.offer("--as", "applinks", links).returncode, 0)
        _, body = self.fetch("applinks")
        m = self.members(body)
        self.assertTrue(m["applinks/Linked.app"].isdir(), "the .app link shipped as a symlink")
        self.assertTrue(m["applinks/Linked.app/Contents/Resources/current"].issym())

    def test_a_no_deref_entry_still_resolves_a_symlinked_root(self):
        target = os.path.dirname(self.mkfile("rootlink/real/inner.txt", "inner\n"))
        os.symlink("inner.txt", os.path.join(target, "inner-link"))
        link = os.path.join(self.work, "rootlink", "via-link")
        os.symlink(target, link)
        r = self.offer("--no-deref", "--as", "vialink", link)
        self.assertEqual(r.returncode, 0, r.stderr)
        _, body = self.fetch("vialink")
        m = self.members(body)
        self.assertTrue(m["via-link"].isdir(), "a symlinked root shipped as a dangling link")
        self.assertTrue(m["via-link/inner.txt"].isfile())
        self.assertTrue(m["via-link/inner-link"].issym(), "--no-deref was ignored")

    def test_pull_still_dereferences_the_hf_cache(self):
        base = os.path.join(self.home, ".cache", "huggingface", "hub",
                            "models--org--tiny")
        os.makedirs(os.path.join(base, "blobs"))
        os.makedirs(os.path.join(base, "snapshots", "abc"))
        with open(os.path.join(base, "blobs", "deadbeef"), "w") as f:
            f.write("weights\n")
        os.symlink("../../blobs/deadbeef",
                   os.path.join(base, "snapshots", "abc", "model.safetensors"))
        with urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}/pull/org/tiny", timeout=30) as r:
            body = r.read()
        member = self.members(body)["tiny/model.safetensors"]
        self.assertTrue(member.isfile(), "a pulled model shipped a symlink into blobs/")
        with tarfile.open(fileobj=io.BytesIO(body), mode="r:") as t:
            self.assertEqual(t.extractfile("tiny/model.safetensors").read(), b"weights\n")

    # -- item 3: the fixture -------------------------------------------------
    def test_selftest_offers_a_fetchable_fixture(self):
        self._selftest_app()
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/manifest", timeout=10) as r:
            files = json.load(r)["files"]
        for name in ("ferry-selftest-file", "ferry-selftest-tree", "ferry-selftest-app"):
            self.assertIn(name, files)
        status, body = self.fetch("ferry-selftest-file")
        self.assertEqual(status, 200)
        self.assertIn("hello.txt", self.members(body))


if __name__ == "__main__":
    if not os.path.exists(FERRY):
        sys.exit("built ./ferry not found — run ./build.zsh first")
    unittest.main(verbosity=2)
