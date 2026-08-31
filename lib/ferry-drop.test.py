#!/usr/bin/env python3
"""Stdlib unittest for `ferry drop` / `ferry pickup` — encrypted off-LAN transfer.

Run:  python3 lib/ferry-drop.test.py

These run the REAL built `ferry` against a throwaway $HOME and $TMPDIR, like the
rest of the suite. That matters more here than usual: the thing under test is a
shell pipeline around the openssl BINARY, so anything that imported functions or
reimplemented the crypto in python would be testing a different program than the
one that ships.

Two properties get adversarial treatment, because they are the ones whose failure
is silent rather than loud:

  * A tampered blob must be rejected BEFORE openssl runs. Asserted with a shim
    binary earlier on PATH that logs every invocation — and paired with a control
    asserting a GOOD pickup does invoke it, since a sentinel that never fires
    would "pass" this test no matter what the code did.

  * Path traversal must be contained by the basename reduction, NOT by the MAC.
    The traversal blob in that test is re-signed with a valid MAC, so the crypto
    accepts it and only the path handling can stop it. Testing with an unsigned
    blob would prove nothing: the MAC would reject it first and the basename
    reduction could be entirely absent.
"""
import os
import shutil
import subprocess
import tempfile
import unittest
import hashlib
import hmac

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FERRY = os.path.join(REPO, "ferry")
MODULE = os.path.join(REPO, "lib", "ferry-drop.zsh")

RC_TAMPER = 3
RC_NOOPENSSL = 5


class DropTestBase(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="ferry-drop-home-")
        self.work = tempfile.mkdtemp(prefix="ferry-drop-work-")
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.work, ignore_errors=True)
        self.passfile = self.path("pass")
        self.write("pass", "test-passphrase-not-a-real-one")

    def path(self, *p):
        return os.path.join(self.work, *p)

    def write(self, name, content):
        with open(self.path(name), "w") as f:
            f.write(content)
        return self.path(name)

    def read(self, name):
        with open(self.path(name)) as f:
            return f.read()

    def ferry(self, *args, env_extra=None, cwd=None):
        env = dict(os.environ, HOME=self.home, TMPDIR=self.work)
        if env_extra:
            env.update(env_extra)
        return subprocess.run(
            [FERRY, *args],
            capture_output=True, text=True,
            env=env, cwd=cwd or self.work,
        )


class RoundTripTest(DropTestBase):
    def test_file_round_trip_is_byte_identical(self):
        original = "secret payload\nline two\nunicode: café ✓\n"
        self.write("plain.txt", original)

        r = self.ferry("drop", "plain.txt", "--pass-file", "pass")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.exists(self.path("plain.txt.ferrydrop")))

        r = self.ferry("pickup", "plain.txt.ferrydrop", "--to", "out.txt",
                       "--pass-file", "pass")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.read("out.txt"), original)

    def test_plaintext_does_not_appear_in_the_blob(self):
        self.write("plain.txt", "MAGIC-CANARY-STRING")
        self.ferry("drop", "plain.txt", "--pass-file", "pass")
        with open(self.path("plain.txt.ferrydrop"), "rb") as f:
            blob = f.read()
        self.assertNotIn(b"MAGIC-CANARY-STRING", blob)

    def test_msg_round_trips_to_stdout(self):
        r = self.ferry("drop", "--msg", "hello from the other side",
                       "--to", "m.ferrydrop", "--pass-file", "pass")
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self.ferry("pickup", "m.ferrydrop", "--pass-file", "pass")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, "hello from the other side")

    def test_stdin_round_trips(self):
        p = subprocess.run(
            [FERRY, "drop", "-", "--to", "s.ferrydrop", "--pass-file", "pass"],
            input="piped content\n", capture_output=True, text=True,
            env=dict(os.environ, HOME=self.home, TMPDIR=self.work), cwd=self.work,
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        r = self.ferry("pickup", "s.ferrydrop", "--pass-file", "pass")
        self.assertEqual(r.stdout, "piped content\n")

    def test_header_is_plain_ascii_and_greppable(self):
        self.write("plain.txt", "x")
        self.ferry("drop", "plain.txt", "--pass-file", "pass")
        head = self.read("plain.txt.ferrydrop").split("\n--\n")[0]
        self.assertTrue(head.startswith("FERRYDROP/1"))
        for field in ("cipher: aes-256-cbc", "kdf: pbkdf2", "iter: 600000",
                      "kind: file", "name: plain.txt", "mac: "):
            self.assertIn(field, head)


class RejectionTest(DropTestBase):
    def setUp(self):
        super().setUp()
        self.write("plain.txt", "payload under test\n")
        r = self.ferry("drop", "plain.txt", "--pass-file", "pass")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.blob = "plain.txt.ferrydrop"

    def test_wrong_passphrase_fails_and_writes_nothing(self):
        self.write("bad", "definitely-the-wrong-passphrase")
        r = self.ferry("pickup", self.blob, "--to", "nope.txt", "--pass-file", "bad")
        self.assertEqual(r.returncode, RC_TAMPER)
        self.assertFalse(os.path.exists(self.path("nope.txt")))

    def test_tampered_ciphertext_is_rejected(self):
        raw = self.read(self.blob)
        head, ct = raw.split("\n--\n", 1)
        flipped = ("X" if ct[0] != "X" else "Y") + ct[1:]
        self.write("t.ferrydrop", head + "\n--\n" + flipped)
        r = self.ferry("pickup", "t.ferrydrop", "--to", "nope.txt", "--pass-file", "pass")
        self.assertEqual(r.returncode, RC_TAMPER)
        self.assertFalse(os.path.exists(self.path("nope.txt")))

    def test_tampered_header_iter_is_rejected(self):
        raw = self.read(self.blob).replace("iter: 600000", "iter: 500000")
        self.write("i.ferrydrop", raw)
        r = self.ferry("pickup", "i.ferrydrop", "--to", "nope.txt", "--pass-file", "pass")
        self.assertEqual(r.returncode, RC_TAMPER)

    def test_absurd_iteration_count_is_refused_without_computing_it(self):
        # A hostile blob could name a huge iteration count purely to hang the
        # recipient. The MAC cannot defend against this: it is verified USING
        # that number, so the cost lands before any check can reject it.
        raw = self.read(self.blob).replace("iter: 600000", "iter: 999999999")
        self.write("h.ferrydrop", raw)
        r = self.ferry("pickup", "h.ferrydrop", "--pass-file", "pass")
        self.assertEqual(r.returncode, RC_TAMPER)
        self.assertIn("implausible", r.stderr)

    def test_unknown_version_is_refused(self):
        raw = self.read(self.blob).replace("FERRYDROP/1", "FERRYDROP/2", 1)
        self.write("v2.ferrydrop", raw)
        r = self.ferry("pickup", "v2.ferrydrop", "--pass-file", "pass")
        self.assertEqual(r.returncode, RC_TAMPER)
        self.assertIn("FERRYDROP/2", r.stderr)

    def test_garbage_input_is_refused(self):
        self.write("junk.ferrydrop", "this is not a blob at all")
        r = self.ferry("pickup", "junk.ferrydrop", "--pass-file", "pass")
        self.assertEqual(r.returncode, RC_TAMPER)


class PathSafetyTest(DropTestBase):
    def test_traversal_in_a_validly_signed_blob_is_contained(self):
        """The blob here is re-signed, so the MAC ACCEPTS it.

        Only the basename reduction can contain it — which is the point. A test
        using an unsigned traversal blob would pass with the path handling
        removed entirely, because the MAC would reject it first.
        """
        secret = "test-passphrase-not-a-real-one"
        self.write("plain.txt", "traversal payload\n")
        self.ferry("drop", "plain.txt", "--pass-file", "pass")

        head, ct = self.read("plain.txt.ferrydrop").split("\n--\n", 1)
        kept = [ln for ln in head.split("\n") if not ln.startswith("mac: ")]
        kept = ["name: ../../escaped.txt" if ln.startswith("name: ") else ln
                for ln in kept]
        key = hashlib.pbkdf2_hmac("sha256", secret.encode(),
                                  b"ferrydrop-mac-v1", 600000, dklen=32)
        signed = ("\n".join(kept) + "\n--\n" + ct).encode()
        mac = hmac.new(key, signed, hashlib.sha256).hexdigest()
        self.write("trav.ferrydrop", "\n".join(kept) + f"\nmac: {mac}\n--\n" + ct)

        deep = self.path("a", "b")
        os.makedirs(deep)
        r = self.ferry("pickup", self.path("trav.ferrydrop"),
                       "--pass-file", self.passfile, cwd=deep)

        self.assertEqual(r.returncode, 0, f"the MAC should ACCEPT this blob: {r.stderr}")
        self.assertTrue(os.path.exists(os.path.join(deep, "escaped.txt")),
                        "should land in the cwd under its basename")
        for escaped in (self.path("a", "escaped.txt"), self.path("escaped.txt")):
            self.assertFalse(os.path.exists(escaped),
                             f"escaped the destination directory: {escaped}")

    def test_refuses_to_write_through_a_symlink(self):
        self.write("plain.txt", "payload\n")
        self.ferry("drop", "plain.txt", "--pass-file", "pass")
        self.write("target.txt", "ORIGINAL")
        os.symlink(self.path("target.txt"), self.path("link.txt"))
        r = self.ferry("pickup", "plain.txt.ferrydrop", "--to", "link.txt",
                       "--pass-file", "pass")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.read("target.txt"), "ORIGINAL")


class SecretHandlingTest(DropTestBase):
    def test_module_never_uses_the_argv_passphrase_form(self):
        """`-pass pass:<secret>` is world-readable through ps."""
        with open(MODULE) as f:
            joined = "".join(ln for ln in f if not ln.lstrip().startswith("#"))
        self.assertNotIn("-pass pass:", joined)
        self.assertNotIn('-pass "pass:', joined)
        # Control: the safe form IS present, so this scan can find things at all.
        self.assertIn('-pass "file:', joined)

    def test_passphrase_is_plain_text_when_stdout_is_not_a_tty(self):
        """A secret wrapped in escape codes scrapes to something subtly wrong.

        subprocess gives us a pipe, so this is the redirected case by
        construction — which is exactly the one a script hits.
        """
        self.write("plain.txt", "x")
        r = self.ferry("drop", "plain.txt")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("\033", r.stdout, "ANSI escapes leaked into piped output")

        line = [l for l in r.stdout.splitlines() if "passphrase:" in l][0]
        secret = line.split("passphrase:")[1].strip()
        # Round-trip the scraped value: if it were mangled, this would fail.
        self.write("scraped", secret)
        r = self.ferry("pickup", "plain.txt.ferrydrop", "--to", "back.txt",
                       "--pass-file", "scraped")
        self.assertEqual(r.returncode, 0,
                         f"scraped passphrase {secret!r} did not work: {r.stderr}")
        self.assertEqual(self.read("back.txt"), "x")

    def test_generated_passphrase_differs_every_run(self):
        self.write("plain.txt", "x")
        seen = set()
        for i in range(3):
            r = self.ferry("drop", "plain.txt", "--to", f"b{i}.ferrydrop")
            self.assertEqual(r.returncode, 0, r.stderr)
            line = [l for l in r.stdout.splitlines() if "passphrase:" in l][0]
            seen.add(line.split("passphrase:")[1].strip())
        self.assertEqual(len(seen), 3, "passphrases must not repeat")


class OpensslBoundaryTest(DropTestBase):
    """openssl must not run until the blob has been authenticated."""

    def _shim_dir(self):
        d = self.path("shim")
        os.makedirs(d, exist_ok=True)
        log = self.path("openssl-calls.log")
        shim = os.path.join(d, "openssl")
        with open(shim, "w") as f:
            f.write("#!/bin/sh\n"
                    f'echo "INVOKED $@" >> "{log}"\n'
                    'exec /usr/bin/openssl "$@"\n')
        os.chmod(shim, 0o755)
        return d, log

    def test_mac_failure_never_reaches_openssl(self):
        d, log = self._shim_dir()
        env = {"PATH": d + os.pathsep + os.environ["PATH"]}

        self.write("plain.txt", "payload\n")
        r = self.ferry("drop", "plain.txt", "--pass-file", "pass", env_extra=env)
        self.assertEqual(r.returncode, 0, r.stderr)

        open(log, "w").close()
        self.write("bad", "wrong-passphrase-entirely")
        r = self.ferry("pickup", "plain.txt.ferrydrop", "--to", "nope.txt",
                       "--pass-file", "bad", env_extra=env)
        self.assertEqual(r.returncode, RC_TAMPER)
        with open(log) as f:
            self.assertEqual(f.read(), "", "openssl was invoked on an unauthenticated blob")

        # Control: a GOOD pickup must invoke it. Without this, a sentinel that
        # never fires would pass the assertion above no matter what.
        open(log, "w").close()
        r = self.ferry("pickup", "plain.txt.ferrydrop", "--to", "ok.txt",
                       "--pass-file", "pass", env_extra=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(log) as f:
            self.assertIn("INVOKED", f.read(), "sentinel never fires; the test above proves nothing")

    def test_missing_openssl_gives_a_clean_error(self):
        # A complete PATH with exactly one thing missing, so the only difference
        # from a working run is the absence under test.
        d = self.path("nossl")
        os.makedirs(d, exist_ok=True)
        for src in ("/usr/bin", "/bin", "/usr/sbin", "/sbin", "/opt/homebrew/bin"):
            if not os.path.isdir(src):
                continue
            for b in os.listdir(src):
                if b == "openssl":
                    continue
                dst = os.path.join(d, b)
                if not os.path.exists(dst):
                    try:
                        os.symlink(os.path.join(src, b), dst)
                    except OSError:
                        pass
        self.write("plain.txt", "x")
        r = self.ferry("drop", "plain.txt", env_extra={"PATH": d})
        self.assertEqual(r.returncode, RC_NOOPENSSL)
        self.assertIn("openssl", r.stderr)
        self.assertNotIn("Traceback", r.stderr)

        # Control: the same PATH with openssl restored must succeed.
        os.symlink("/usr/bin/openssl", os.path.join(d, "openssl"))
        r = self.ferry("drop", "plain.txt", "--to", "c.ferrydrop", env_extra={"PATH": d})
        self.assertEqual(r.returncode, 0, f"control failed: {r.stderr}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
