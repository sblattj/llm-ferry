#!/usr/bin/env python3
"""Stdlib unittest for the Claude Pro/Max subscription OAuth zsh module.

Run:  python3 lib/ferry-auth-claude.test.py

`ferry auth-claude login|status|refresh|logout` (lib/ferry-auth-claude.zsh)
wraps the Python OAuth engine front/ferry_claude_oauth.py — a sibling seat —
behind the zsh CLI, storing the token JSON at
~/.config/ferry/claude/auth.json (0600), the Claude twin of the ChatGPT lane's
~/.config/litellm/chatgpt/auth.json but under ferry's own config dir.

The engine is NEVER invoked for real here. The litellm venv is faked with a
stub bindir — a `litellm` shim plus a `python` twin sitting next to its
realpath, exactly the pair _ferry_litellm_python resolves — and that python
stub implements the documented engine CLI (login --output / ensure-valid
--auth --force), writing throwaway token JSON. Every behavioral assertion also
proves the module never leaks the (fake) access/refresh tokens to stdout or
stderr on any path.

Runs the REAL cmd_auth_claude against a throwaway $HOME — through the built
`ferry` monolith when it answers `ferry auth-claude --help`, otherwise by
sourcing lib/ferry-core.zsh + lib/ferry-auth-claude.zsh in a zsh subprocess
(same fallback as lib/ferry-claude.test.py).
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FERRY = os.path.join(REPO, "ferry")
MODULE = os.path.join(REPO, "lib", "ferry-auth-claude.zsh")

# Throwaway stand-ins for the real credentials. If one of these ever shows up
# in a subprocess's stdout/stderr, the module leaked a token.
ACCESS_1 = "SENTINEL-ACCESS-1"
REFRESH_1 = "SENTINEL-REFRESH-1"
ACCESS_2 = "SENTINEL-ACCESS-2"
REFRESH_2 = "SENTINEL-REFRESH-2"
SENTINELS = (ACCESS_1, REFRESH_1, ACCESS_2, REFRESH_2)
ALL_SENTINELS = "".join(SENTINELS)

ACCOUNT = "operator@example.com"
# Fixed epochs so the formatted expiry is deterministic: 2100-01-01T00:00:00Z
# (valid) and 2001-09-09T01:46:40Z (long expired).
VALID_EXPIRES_AT = 4102444800
VALID_EXPIRES_ISO = "2100-01-01 00:00:00Z"
EXPIRED_EXPIRES_AT = 1000000000
EXPIRED_EXPIRES_ISO = "2001-09-09 01:46:40Z"


def _token_json(access, refresh, expires_at, email=ACCOUNT):
    return {"access_token": access, "refresh_token": refresh,
            "id_token": "", "expires_at": expires_at, "email": email}


# The stub of the litellm-venv python. _ferry_litellm_python resolves
# `command -v litellm`, realpaths it, and runs the `python` twin in the same
# dir — so this script lands at exactly that spot. It logs its invocation
# (argv + the FERRY_CLAUDE_AUTH_JSON env hook) to $STUB_LOG, then fakes the
# engine side per the module's documented CLI contract.
STUB_PYTHON = r"""#!/bin/sh
log() {
  printf 'CALL %s\n' "$*"
  printf 'ENVPATH %s\n' "${FERRY_CLAUDE_AUTH_JSON:-}"
} >> "$STUB_LOG"
verb="$2"
path=""
prev=""
for a in "$@"; do
  case "$prev" in
    --output|--auth) path="$a" ;;
  esac
  prev="$a"
done
log "$@"
case "$verb" in
  login)
    mkdir -p "$(dirname "$path")"
    printf '{"access_token": "SENTINEL-ACCESS-1", "refresh_token": "SENTINEL-REFRESH-1", "id_token": "", "expires_at": 4102444800, "email": "operator@example.com"}\n' > "$path"
    echo "stub: browser PKCE flow simulated; token JSON written"
    ;;
  ensure-valid)
    printf '{"access_token": "SENTINEL-ACCESS-2", "refresh_token": "SENTINEL-REFRESH-2", "id_token": "", "expires_at": 4102444800, "email": "operator@example.com"}\n' > "$path"
    echo "stub: token refreshed and persisted"
    ;;
  *)
    echo "stub: unknown verb $verb" >&2
    exit 2
    ;;
esac
exit 0
"""

# Exists only so `command -v litellm` resolves into the stub bindir; if it is
# ever actually EXECUTED something has gone wrong (it exits 69 on purpose).
STUB_LITELLM = "#!/bin/sh\nexit 69\n"


def _monolith_supports_auth_claude():
    """True when the built `ferry` monolith answers `ferry auth-claude --help`.

    The monolith is GENERATED (build.zsh concatenates lib/ferry-*.zsh), so it
    lags the module until the auth-claude seat's integration lands. When it is
    stale or absent, the functional tests fall back to sourcing the modules
    directly — the contract under test is the module, not the packaging step.
    """
    if not os.path.exists(FERRY):
        return False
    try:
        p = subprocess.run(
            ["zsh", FERRY, "auth-claude", "--help"],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return p.returncode == 0 and "Unknown command" not in (p.stdout + p.stderr)


MONOLITH = _monolith_supports_auth_claude()


class AuthClaudeHarness(unittest.TestCase):
    """Shared fixture: throwaway $HOME + a stubbed litellm venv on PATH."""

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="ferry-auth-claude-home-")
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.bindir = os.path.join(self.home, "bin")
        os.makedirs(self.bindir)
        self.stub_log = os.path.join(self.home, "stub-invocations.log")
        self.default_auth = os.path.join(
            self.home, ".config", "ferry", "claude", "auth.json")
        # The engine-script override target. The stub python receives it as
        # argv[1] and ignores its content, so a one-line file is enough.
        self.engine_stub = os.path.join(self.home, "engine-stub.py")
        with open(self.engine_stub, "w") as f:
            f.write("# stand-in for front/ferry_claude_oauth.py\n")
        self._write_stub_venv()

    def _write_stub_venv(self):
        py = os.path.join(self.bindir, "python")
        with open(py, "w") as f:
            f.write(STUB_PYTHON)
        os.chmod(py, 0o755)
        litellm = os.path.join(self.bindir, "litellm")
        with open(litellm, "w") as f:
            f.write(STUB_LITELLM)
        os.chmod(litellm, 0o755)

    # --- helpers ------------------------------------------------------------
    def env(self, with_venv=True, extra=None):
        e = os.environ.copy()
        e["HOME"] = self.home
        e["TMPDIR"] = self.home
        e["STUB_LOG"] = self.stub_log
        if with_venv:
            e["PATH"] = self.bindir + os.pathsep + e.get("PATH", "")
        else:
            # System dirs only: python3 must still resolve (the module shells
            # out to it for the JSON summary), litellm must NOT.
            e["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"
        e.pop("FERRY_CLAUDE_AUTH_JSON", None)
        e.pop("FERRY_CLAUDE_OAUTH_PY", None)
        if extra:
            e.update(extra)
        return e

    def run_cli(self, *args, stdin=None, with_venv=True, extra_env=None):
        """Drive the REAL cmd_auth_claude — via the built monolith when it is
        in sync, otherwise by sourcing the lib/ modules in a zsh subprocess."""
        if MONOLITH:
            cmd = [FERRY, "auth-claude", *args]
        else:
            if not os.path.exists(MODULE):
                self.fail("lib/ferry-auth-claude.zsh does not exist")
            script = (
                f"source {REPO}/lib/ferry-core.zsh\n"
                f"source {MODULE}\n"
                'cmd_auth_claude "$@"\n'
            )
            cmd = ["zsh", "-c", script, "ferry-auth-claude", *args]
        # Engine override defaults ON so login/refresh never depend on the
        # sibling seat's front/ tree having landed in this checkout.
        extra_env = dict(extra_env or {})
        extra_env.setdefault("FERRY_CLAUDE_OAUTH_PY", self.engine_stub)
        return subprocess.run(
            cmd, capture_output=True, text=True, timeout=60,
            env=self.env(with_venv=with_venv, extra=extra_env),
            cwd=self.home, input=stdin,
        )

    def assertNoTokens(self, *streams):
        for s in streams:
            for sentinel in SENTINELS:
                self.assertNotIn(sentinel, s,
                                 "a token sentinel leaked to a subprocess stream")

    def seed_auth(self, path=None, **token_kw):
        path = path or self.default_auth
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tok = _token_json(ACCESS_1, REFRESH_1, VALID_EXPIRES_AT)
        tok.update(token_kw)
        with open(path, "w") as f:
            json.dump(tok, f)
        return path

    def stub_log_text(self):
        if not os.path.exists(self.stub_log):
            return ""
        with open(self.stub_log) as f:
            return f.read()

    def read_tokens(self, path=None):
        with open(path or self.default_auth) as f:
            return json.load(f)


class ScriptContractTest(unittest.TestCase):
    """Cheap static checks pinning the module's cross-seat wiring contracts."""

    def read(self, rel):
        with open(os.path.join(REPO, rel)) as f:
            return f.read()

    def test_module_syntax_is_clean_zsh(self):
        r = subprocess.run(["zsh", "-n", MODULE], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_module_defines_the_command_and_the_default_token_path(self):
        text = self.read("lib/ferry-auth-claude.zsh")
        self.assertIn("cmd_auth_claude()", text)
        self.assertIn("$HOME/.config/ferry/claude/auth.json", text,
                      "default token path must be ~/.config/ferry/claude/auth.json")
        self.assertIn('${FERRY_CLAUDE_AUTH_JSON:-', text,
                      "token path must be env-overridable")

    def test_module_resolves_the_litellm_venv_python(self):
        # The reuse-first + realpath-sibling pattern from ferry-serve.zsh's
        # _ferry_front_python is the contract; pin both halves.
        text = self.read("lib/ferry-auth-claude.zsh")
        self.assertIn("_ferry_litellm_python", text)
        self.assertIn("$+functions[_ferry_front_python]", text,
                      "must reuse the serve module's resolver when loaded")
        self.assertIn("realpath", text)


class UsageAndDispatchTest(AuthClaudeHarness):
    """Arg parsing and subcommand dispatch."""

    def test_help_exits_zero_and_lists_every_subcommand(self):
        r = self.run_cli("--help")
        self.assertEqual(r.returncode, 0, r.stderr)
        for sub in ("login", "status", "refresh", "logout"):
            self.assertIn(sub, r.stdout, f"usage must list '{sub}'")
        self.assertIn("FERRY_CLAUDE_AUTH_JSON", r.stdout)
        self.assertNotIn("Traceback", r.stdout + r.stderr)
        self.assertEqual(self.stub_log_text(), "",
                         "--help must not touch the OAuth engine")

    def test_no_subcommand_prints_usage_and_fails(self):
        r = self.run_cli()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Usage", r.stdout)
        self.assertNotIn("Traceback", r.stdout + r.stderr)

    def test_unknown_subcommand_fails_cleanly(self):
        r = self.run_cli("bogus")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Unknown subcommand", r.stderr)
        self.assertNotIn("Traceback", r.stdout + r.stderr)
        self.assertEqual(self.stub_log_text(), "")

    def test_unknown_option_fails_cleanly(self):
        r = self.run_cli("login", "--bogus")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Unknown option", r.stderr)
        self.assertNotIn("Traceback", r.stdout + r.stderr)


class LoginTest(AuthClaudeHarness):
    """login — the stubbed browser PKCE flow."""

    def test_writes_0600_token_json_at_the_default_path_and_prints_only_the_email(self):
        r = self.run_cli("login")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.exists(self.default_auth),
                        "no token file at the default path")
        self.assertEqual(os.stat(self.default_auth).st_mode & 0o777, 0o600,
                         "token file must be mode 0600")
        self.assertIn(ACCOUNT, r.stdout, "login must print the account email")
        self.assertIn(self.default_auth, r.stdout)
        self.assertNoTokens(r.stdout, r.stderr)
        self.assertEqual(self.read_tokens()["email"], ACCOUNT)

    def test_token_path_env_override_redirects_the_file_and_both_path_hooks(self):
        alt = os.path.join(self.home, "alt", "auth.json")
        r = self.run_cli("login", extra_env={"FERRY_CLAUDE_AUTH_JSON": alt})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.exists(alt))
        self.assertFalse(os.path.exists(self.default_auth))
        log = self.stub_log_text()
        self.assertIn(f"login --output {alt}", log,
                      "engine must receive the override via --output")
        self.assertIn(f"ENVPATH {alt}", log,
                      "engine must receive the override via the env var too")

    def test_engine_script_override_reaches_the_engine_invocation(self):
        marker = os.path.join(self.home, "custom-engine.py")
        with open(marker, "w") as f:
            f.write("# custom engine\n")
        r = self.run_cli("login", extra_env={"FERRY_CLAUDE_OAUTH_PY": marker})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(marker, self.stub_log_text(),
                      "FERRY_CLAUDE_OAUTH_PY must be the script the venv python runs")

    def test_refuses_cleanly_without_the_litellm_venv(self):
        r = self.run_cli("login", with_venv=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("litellm", r.stderr)
        self.assertNotIn("Traceback", r.stdout + r.stderr)
        self.assertFalse(os.path.exists(self.default_auth))
        self.assertEqual(self.stub_log_text(), "")

    def test_refuses_cleanly_when_the_engine_script_is_missing(self):
        r = self.run_cli("login", extra_env={
            "FERRY_CLAUDE_OAUTH_PY": os.path.join(self.home, "nope.py")})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("missing", r.stderr)
        self.assertNotIn("Traceback", r.stdout + r.stderr)
        self.assertFalse(os.path.exists(self.default_auth))


class StatusTest(AuthClaudeHarness):
    """status — derived fields only, never tokens."""

    def test_refuses_cleanly_when_not_logged_in(self):
        r = self.run_cli("status")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Not logged in", r.stdout)
        self.assertNotIn("Traceback", r.stdout + r.stderr)
        self.assertEqual(self.stub_log_text(), "",
                         "status must not invoke the OAuth engine")

    def test_reports_email_expiry_and_valid(self):
        self.seed_auth()
        r = self.run_cli("status")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(ACCOUNT, r.stdout)
        self.assertIn(VALID_EXPIRES_ISO, r.stdout,
                      "the UTC expiry of the seeded token must be shown")
        self.assertIn("VALID", r.stdout)
        self.assertNoTokens(r.stdout, r.stderr)

    def test_flags_an_expired_token_and_hints_the_fix(self):
        self.seed_auth(expires_at=EXPIRED_EXPIRES_AT)
        r = self.run_cli("status")
        self.assertNotEqual(r.returncode, 0, "expired must exit nonzero")
        self.assertIn(EXPIRED_EXPIRES_ISO, r.stdout)
        self.assertIn("EXPIRED", r.stdout)
        self.assertIn("refresh", r.stdout)
        self.assertNoTokens(r.stdout, r.stderr)

    def test_honors_the_token_path_env_override(self):
        alt = os.path.join(self.home, "alt", "auth.json")
        self.seed_auth(path=alt)
        r = self.run_cli("status", extra_env={"FERRY_CLAUDE_AUTH_JSON": alt})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(ACCOUNT, r.stdout)
        self.assertIn(alt, r.stdout)

    def test_recovers_the_email_from_an_id_token_when_no_email_field(self):
        # The ChatGPT lane's auth.json has no email field; the Claude engine
        # may store it only inside the id_token JWT. Seed that shape.
        import base64
        payload = base64.urlsafe_b64encode(
            json.dumps({"email": ACCOUNT}).encode()).decode().rstrip("=")
        self.seed_auth(email=None, id_token=f"hdr.{payload}.sig")
        tok = self.read_tokens()
        del tok["email"]
        with open(self.default_auth, "w") as f:
            json.dump(tok, f)
        r = self.run_cli("status")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(ACCOUNT, r.stdout,
                      "email must fall back to the id_token payload")
        self.assertNoTokens(r.stdout, r.stderr)


class RefreshTest(AuthClaudeHarness):
    """refresh — forced ensure-valid with in-place rotation."""

    def test_refuses_cleanly_when_not_logged_in(self):
        r = self.run_cli("refresh")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Not logged in", r.stdout)
        self.assertNotIn("Traceback", r.stdout + r.stderr)
        self.assertEqual(self.stub_log_text(), "")

    def test_forces_ensure_valid_and_persists_the_rotated_refresh_token(self):
        self.seed_auth()
        r = self.run_cli("refresh")
        self.assertEqual(r.returncode, 0, r.stderr)
        log = self.stub_log_text()
        self.assertIn(f"ensure-valid --auth {self.default_auth} --force", log,
                      "refresh must call ensure-valid --auth <path> --force")
        self.assertIn(f"ENVPATH {self.default_auth}", log)
        # The stub ROTATED the throwaway tokens; the file on disk must now
        # hold the new pair — proof the rotation is persisted in place.
        tok = self.read_tokens()
        self.assertEqual(tok["access_token"], ACCESS_2)
        self.assertEqual(tok["refresh_token"], REFRESH_2)
        self.assertEqual(os.stat(self.default_auth).st_mode & 0o777, 0o600)
        self.assertIn(ACCOUNT, r.stdout)
        self.assertNoTokens(r.stdout, r.stderr)

    def test_refuses_cleanly_without_the_litellm_venv(self):
        self.seed_auth()
        r = self.run_cli("refresh", with_venv=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("litellm", r.stderr)
        self.assertNotIn("Traceback", r.stdout + r.stderr)
        self.assertEqual(self.read_tokens()["refresh_token"], REFRESH_1,
                         "a failed refresh must not touch the stored tokens")


class LogoutTest(AuthClaudeHarness):
    """logout — confirmation gate and --force."""

    def test_without_a_token_file_is_a_clean_noop(self):
        r = self.run_cli("logout")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Not logged in", r.stdout)
        self.assertNotIn("Traceback", r.stdout + r.stderr)

    def test_a_declined_confirmation_keeps_the_file(self):
        self.seed_auth()
        r = self.run_cli("logout", stdin="n\n")
        self.assertNotEqual(r.returncode, 0)
        self.assertTrue(os.path.exists(self.default_auth))
        self.assertIn("Aborted", r.stdout)
        self.assertNoTokens(r.stdout, r.stderr)

    def test_a_typed_yes_removes_the_file(self):
        self.seed_auth()
        r = self.run_cli("logout", stdin="yes\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(os.path.exists(self.default_auth))
        self.assertNoTokens(r.stdout, r.stderr)

    def test_force_removes_without_asking_even_on_closed_stdin(self):
        # stdin=None (EOF): --force must not block on a confirmation read.
        self.seed_auth()
        r = self.run_cli("logout", "--force")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(os.path.exists(self.default_auth))

    def test_eof_without_force_refuses_instead_of_hanging_or_defaulting(self):
        self.seed_auth()
        r = self.run_cli("logout", stdin="")
        self.assertNotEqual(r.returncode, 0)
        self.assertTrue(os.path.exists(self.default_auth))


if __name__ == "__main__":
    unittest.main(verbosity=2)
