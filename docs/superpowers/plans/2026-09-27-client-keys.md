# Per-Device Client Keys Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give every client device its own revocable `fk-` key, enforced in the existing front-door ASGI middleware, with optional per-key lane lists, RPM limits and monthly token budgets. Bootstrap enrolls new devices automatically, and a host-only `ferry keys` CLI manages the keys. Ship it as v1.39.0.

**Architecture:** The key store lives in a new stdlib module, `front/ferry_keys.py`. It holds `keys.json` (mode 0600, written atomically, cached by mtime) and is shared by the front door, the CLI and the dash. The usage counters live in a second stdlib module, `front/ferry_keys_usage.py`, backed by a WAL SQLite file that all 4 uvicorn workers share. A new auth step at the top of `LaneCatalogueFilter.__call__` (`front/ferry_front.py`) does the following:
- It recognises `fk-` bearers and refuses unknown, revoked or expired keys with a 401.
- It rewrites the credential to the master key and pins the caller identity to the key's name.
- On inference paths, it enforces lanes (403), RPM (429) and budget (429), with error bodies shaped for each API family.
- It meters input and output tokens off the same passive parser that the event tap uses, whether or not the tap is on.

`POST /v1/ferry/keys/enroll`, gated by the master key, mints a key. `client-bootstrap.sh` calls it and stores `api_key` in place of `master_key`.

**Tech Stack:** Python 3 stdlib (`sqlite3`, `hashlib`, `secrets`, `fcntl`, `unittest`), pure ASGI middleware in front of litellm 1.99 under uvicorn, zsh modules assembled by `build.zsh` into the single `ferry` script, and vanilla JS in `ferry-dash`.

**Spec:** `docs/superpowers/specs/2026-09-27-client-keys-design.md`. The approved answers to its open questions are: (1) v1 budgets are **tokens only** (`budget_tokens`, per calendar month, UTC), with no USD. (2) Only re-running client bootstrap moves a client off the master key; `ferry update` never touches credentials.

## Global Constraints

- The target release is **v1.39.0**. `VERSION` is `1.38.0` at base `43acdda`.
- Keys look like `fk-<slug>-<32 lowercase base32 chars from secrets.token_bytes(20)>`. `<slug>` is the key name, max 24 chars, from `[a-z0-9-]`. The prefix `fk-` is the only thing the front door matches on before it does a lookup.
- `~/.config/ferry/keys.json` has mode 0600, is written atomically (tempfile + `os.replace`) and has the shape `{"version": 1, "keys": [{"name","sha256","created","expires","revoked","lanes","rpm","budget_tokens"}]}`. The plaintext key is never stored and never logged. Revoked entries are kept.
- `~/.config/ferry/keys-usage.sqlite` uses WAL with two tables: `minute(name, minute_epoch, n)` and `month(name, yyyymm, tokens)`. Minute rows older than the previous minute are pruned, and month rows older than 13 months are pruned.
- Tests override the paths with `FERRY_KEYS_FILE` (store) and `FERRY_KEYS_DB` (usage DB). Both are read **at call time**, never at import. `rg FERRY_KEYS` over the repo at `43acdda` finds no collision. Every test that can reach either path sets both variables, so the real `~/.config/ferry/keys.json` is never touched.
- A master-key request keeps today's behaviour exactly: no limits, identity as today, and it works in every failure mode. With the tap off and the header strip off, a master-key or no-bearer inference request must still hand litellm the caller's **original `send` by identity** (`lib/ferry-front.test.py` `test_the_inference_path_is_handed_the_original_send`).
- `fk-` keys fail closed. An unreadable or corrupt `keys.json` gives a 401. A usage DB that cannot be opened gives a 503. The master key is unaffected by either.
- A budget counts **input_tokens + output_tokens only**. Reasoning is a subset of output (`lib/ferry_metrics.py` module docstring, line 4), so adding it again would double-count. The spec's "input + output + reasoning" is corrected here. One request may overshoot the budget, and the docs say so.
- Error bodies depend on the API family. OpenAI paths get `{"error": {"message", "type", "code"}}`. `/v1/messages` and `/messages` get `{"type": "error", "error": {"type", "message"}}`. Every 429 carries `Retry-After`.
- litellm's config and the master-key mechanism are unchanged. No Prometheus `key` label is added.
- `ferry` is generated. After any `lib/ferry-*.zsh` edit, run `zsh ./build.zsh` (never `./build.zsh`, which gives rc=126) and stage `ferry` with the edit. `zsh ./build.zsh --check` must pass before every commit that touches `lib/*.zsh`. Never hand-edit `ferry`.
- **When a merge conflicts in `ferry`, take either side and re-run `zsh ./build.zsh`.** Tasks 7 and 8 both regenerate it.
- Test runs never execute `ferry up/down/reload/install` and never bind the real ports (8090, 8091, 8095, …). Every server in a test binds `127.0.0.1:0`, and every `$HOME` is a `tempfile.mkdtemp()`.
- Commits stage files by explicit pathspec (never `git add -A`). Every commit message ends with the line `Claude-Session-Id: b358f2c8-b360-463c-a431-5b7dd8808316`. Write the message to a file under `/tmp` with the Write tool and commit with `git commit -F <file>`.
- Line numbers in this plan are **valid at `43acdda`; HEAD may have moved**. Find code by the symbol named next to each number (`rg -n 'def caller_identity' front/ferry_front.py`).
- `Expected:` test counts are advisory. The gate is `OK` versus `FAILED`, plus the change from the count you measured yourself before editing.

## Seat environment (read before running anything)

A worktree isolation gate sits in front of every Bash call, and it **refuses** any command that contains:
- `$VAR` or `$(...)`,
- `for`/`while` loops,
- heredocs,
- any inline `HOME=...`, `env HOME=...` or `VAR=value cmd` prefix.

Commands that are allowed:
- `cd /abs/path && <one command>`
- `zsh /abs/script.zsh <literal args>`
- `python3 /abs/file.py`
- `node /abs/file.mjs`
- plain `git` subcommands run from your own worktree.

Put logic and any `$HOME`-sandboxed run into a file, write it with the Write tool (not a heredoc), and run it by absolute path. Scratch files go in `/tmp`, never in the worktree. Test suites sandbox `$HOME` themselves, inside Python. `./build.zsh` gives rc=126; always use `zsh ./build.zsh`. Commands longer than about 2 minutes (the full suite takes about 317 s at base) need the Bash tool's `timeout: 600000` or `run_in_background: true`.

Each task's paths are relative to the repo root of **your** worktree. `/abs/worktree` in a `Run:` line is a placeholder: type your worktree's literal absolute path, because the gate refuses a variable. Before you edit anything, derive your base with `git merge-base HEAD origin/main`, and do not trust a SHA a brief hands you.

## Spec corrections adopted by this plan

1. **Token accounting would never run with the default config.** `_inference` (`front/ferry_front.py:1339`) builds a `RequestMetrics` collector only under `tap_enabled()` (`:1344`). `_fleet_rewrite` injects `stream_options.include_usage` only under `tap_enabled()` (`:1462`). `FERRY_EVENTS` is off by default. So, for an `fk-` request only, the plan builds the collector and injects `include_usage` whether or not the tap is on (Task 4).
2. **Budget = input + output.** See Global Constraints.
3. **Enroll cannot use `_bearer_ok` (`:1070`).** After the auth step rewrites a device key to the master key, `_bearer_ok` would pass, and a device could mint keys. The auth step records the *original* credential class in `scope["ferry.key"]`, and enroll checks that instead (Task 5).
4. **`"replace": true` conflicts with "names are unique".** The plan defines replace as **rotation in place**: the same entry gets a new `sha256` and `created`, `revoked` is cleared, and expiry and limits are kept. The old key dies on the next request.
5. **More readers than `ferry-core.zsh:294` consume `master_key`:**
   - `lib/ferry-claude.zsh:242`, where the wrappers bake the key.
   - `client-reset.sh:99`, which threads `--key`.
   - `client-to-host.sh:179`, which copies the value into the new host's `LITELLM_MASTER_KEY`.

   The first two now prefer `api_key` (Task 7). The third deliberately stays `master_key`-only, so a device key is never promoted to a host master key; a comment and a test pin this.
6. **The two-process RPM test** belongs in the usage module's own suite (Task 2), not in the front-door tests.
7. **The front tests go in new files**, `lib/ferry-front-keys.test.py` and `lib/ferry-front-enroll.test.py`, rather than being appended to `lib/ferry-front.test.py`. Otherwise parallel seats collide at the end of that file.
8. `ferry dash` shows the per-key rows in a new **Device keys** card fed by `/status`, and the live feed labels each request with its key. "Requests" means requests in the current minute (from the usage DB), because the dash has no per-key request history.

## Review Focus

The spec is silent on these conditions. Each is the most likely to bite a user, and each gets a test in the task named.

1. **An `fk-` key sent as `x-api-key` on `/v1/messages`.** litellm accepts either header. Expected: it is recognised, refused or rewritten exactly like a Bearer. → Task 3, `test_x_api_key_carrying_a_device_key_is_rewritten`.
2. **A WebSocket scope carrying a revoked `fk-` key.** The middleware passes non-HTTP scopes through untouched today. With auth off, litellm would admit the key. Expected: `websocket.close` code 1008 and the app is never called. → Task 3, `test_websocket_with_a_revoked_key_is_closed`.
3. **A lanes-restricted key whose body is not JSON, or has no `model`.** Expected: 403, not a fail-open pass. → Task 4, `test_unparseable_body_on_a_lane_restricted_key_is_403`.
4. **The usage DB becomes unwritable after admission,** when tokens are added on completion. Expected: the response is delivered untouched, one rate-limited stderr line is printed, and nothing raises into the ASGI stack. → Task 4, `test_accounting_failure_never_breaks_the_response`.
5. **A master-key or no-bearer request after the feature lands.** Expected: `keys.json` is never read, no usage DB is created, and `send` is passed through by identity. → Task 3, `test_master_and_bare_requests_touch_no_key_state`, and Task 4, `test_master_request_still_gets_the_original_send`.

## Parallelism and dependencies

| Task | Deliverable | Depends on | Wave |
|---|---|---|---|
| 1 | `front/ferry_keys.py` store | — | 1 |
| 2 | `front/ferry_keys_usage.py` usage DB + limits | — | 1 |
| 6 | `ferry_events` `key` field (events half of attribution) | — | 1 |
| 7 | bootstrap enroll + `api_key` readers | — (tests use a stub host; consumes only the enroll wire contract below) | 1 |
| 3 | front-door auth step | 1 | 2 |
| 8 | `ferry keys` CLI (`front/ferry_keys_cli.py` + `lib/ferry-keys.zsh`) | 1, 2 | 2 |
| 9 | dash Device keys card + feed label | 1, 2, 6 | 2 |
| 4 | front-door limits + accounting + event `key` | 1, 2, 3 | 3 |
| 5 | enroll endpoint | 1, 3 | 3 (parallel with 4: different hunks, different test file) |
| 10 | README, release notes, roadmap, VERSION, full suite | all | 4 |

The **enroll wire contract** that Task 5 implements and Task 7 consumes is:
- Request: `POST /v1/ferry/keys/enroll`, `Authorization: Bearer <master>`, body `{"name": "<host>", "replace": true?}`.
- Success: `200 {"name": "<final name>", "key": "fk-..."}`.
- Anything else is a failure, and the bootstrap falls back to storing `master_key`.

The **scope keys** written by Task 3 and read by Tasks 4 and 5 are:
- `"ferry.key"`: `"master"`, a key name, or `""`.
- `"ferry.key_entry"`: the entry dict. Present only for an admitted `fk-` key.
- `"ferry.key_admitted"`: `True`, set by Task 4 once RPM and budget pass.

## File Structure

| File | Responsibility | Task |
|---|---|---|
| `front/ferry_keys.py` (new) | name rules, mint/hash, load/save (atomic 0600, flock), add/revoke/update, status, lane_allowed, KeyCache (mtime) | 1 |
| `lib/ferry-keys-store.test.py` (new) | tests for the above | 1 |
| `front/ferry_keys_usage.py` (new) | Usage: admit (RPM then budget, atomic), add_tokens, month/minute reads, summary, prune | 2 |
| `lib/ferry-keys-usage.test.py` (new) | tests incl. two-process RPM | 2 |
| `front/ferry_front.py` | auth step, identity override, `_reply(headers=)`, key error bodies (T3); limits, metering, event `key` (T4); enroll route (T5) | 3, 4, 5 |
| `lib/ferry-front-keys.test.py` (new) | front auth + limits tests | 3, 4 |
| `lib/ferry-front-enroll.test.py` (new) | enroll tests | 5 |
| `lib/ferry_events.py`, `lib/ferry-events.test.py` | `key` in `_EMPTY` + contract | 6 |
| `client-bootstrap.sh`, `client-reset.sh`, `client-to-host.sh`, `lib/ferry-core.zsh`, `lib/ferry-claude.zsh`, `ferry` | enroll + `api_key` preference | 7 |
| `lib/ferry-clientbootstrap.test.py`, `lib/ferry-fleet.test.py`, `lib/ferry-claude.test.py` | tests for the above | 7 |
| `front/ferry_keys_cli.py` (new), `lib/ferry-keys.zsh` (new), `lib/ferry-main.zsh`, `lib/ferry-usage.zsh`, `build.zsh`, `ferry` | `ferry keys` | 8 |
| `lib/ferry-keys.test.py` (new), `lib/ferry-main.test.py` | CLI tests, help-guard registration | 8 |
| `ferry-dash`, `lib/ferry-dashserver.test.py`, `lib/ferry-dashui.test.mjs` | Device keys card, `/status.keys`, feed label | 9 |
| `README.md`, `docs/releases/v1.39.0.md` (new), `docs/roadmap.md`, `docs/superpowers/specs/2026-09-27-client-keys-design.md`, `client-config-example.json`, `VERSION` | docs + release | 10 |

---

### Task 1: Key store — `front/ferry_keys.py`

**Files:**
- Create: `front/ferry_keys.py`
- Test: `lib/ferry-keys-store.test.py` (new)

**Interfaces:**
- Consumes: nothing.
- Produces (used by Tasks 3, 4, 5, 8, 9):
  - `KEY_PREFIX = "fk-"`
  - `class KeyStoreError(Exception)`: the store is unreadable or corrupt.
  - `class KeyNameError(ValueError)`: a bad, duplicate or unknown name, or a bad limit value.
  - `keys_path(env=None) -> str`: `$FERRY_KEYS_FILE`, else `~/.config/ferry/keys.json`, resolved per call.
  - `utcnow() -> datetime`, `iso(dt) -> str`, `parse_iso(str) -> datetime`, `expires_from_date("YYYY-MM-DD") -> str`
  - `normalize_name(raw) -> str`, `mint_token(name, rand=None) -> str`, `hash_token(token) -> str`
  - `load(path=None) -> dict`, `save(doc, path=None) -> None`, `find(doc, name) -> dict | None`
  - `add(name, *, path=None, expires=None, lanes=None, rpm=None, budget_tokens=None, unique=False, replace=False, now=None, rand=None) -> (str name, str token)`
  - `revoke(name, *, path=None, now=None) -> dict`
  - `update(name, *, path=None, **changes) -> dict`. Allowed changes: `expires`, `lanes`, `rpm`, `budget_tokens`. A value of `None` clears the field.
  - `status(entry, now=None) -> "active" | "revoked" | "expired"`
  - `lane_allowed(lanes, requested, resolved) -> bool`
  - `class KeyCache(path_fn=None)` with `.lookup(token, now=None) -> (entry | None, reason)`. `reason` is `"ok"`, `"unknown"`, `"revoked"` or `"expired"`. It raises `KeyStoreError` on a corrupt or unreadable file, and a missing file means `(None, "unknown")`.

- [ ] **Step 1: Write the failing test**

Create `lib/ferry-keys-store.test.py`:

```python
#!/usr/bin/env python3
"""Stdlib unittest for front/ferry_keys.py — the per-device key store.

Run:  python3 lib/ferry-keys-store.test.py

Every test points FERRY_KEYS_FILE at a temp dir, so the real
~/.config/ferry/keys.json is never read or written.
"""
import datetime
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import unittest
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "front"))

import ferry_keys as K  # noqa: E402

UTC = datetime.timezone.utc


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ferry-keys-store-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "keys.json")
        patcher = mock.patch.dict(os.environ, {"FERRY_KEYS_FILE": self.path})
        patcher.start()
        self.addCleanup(patcher.stop)

    def bump_mtime(self):
        st = os.stat(self.path)
        os.utime(self.path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))


class TestNamesAndTokens(StoreCase):
    def test_token_shape(self):
        token = K.mint_token("mbp-work")
        self.assertRegex(token, r"^fk-mbp-work-[a-z2-7]{32}$")

    def test_tokens_are_random(self):
        self.assertNotEqual(K.mint_token("a"), K.mint_token("a"))

    def test_slug_is_capped_at_24(self):
        token = K.mint_token("a" * 40, rand=b"\0" * 20)
        self.assertEqual(token, "fk-" + "a" * 24 + "-" + "a" * 32)

    def test_hash_is_sha256_hex_of_the_whole_token(self):
        import hashlib
        token = K.mint_token("x")
        self.assertEqual(K.hash_token(token), hashlib.sha256(token.encode()).hexdigest())

    def test_normalize(self):
        self.assertEqual(K.normalize_name("MBP Work!"), "mbp-work")
        self.assertEqual(K.normalize_name("  Stephen's--MacBook  "), "stephen-s-macbook")
        for bad in ("", "---", "!!!", None, 5):
            with self.subTest(bad=bad):
                with self.assertRaises(K.KeyNameError):
                    K.normalize_name(bad)

    def test_expires_from_date(self):
        self.assertEqual(K.expires_from_date("2026-10-01"), "2026-10-01T00:00:00Z")
        with self.assertRaises(K.KeyNameError):
            K.expires_from_date("10/01/2026")


class TestAddAndPersist(StoreCase):
    def test_add_writes_0600_without_the_plaintext(self):
        name, token = K.add("laptop")
        self.assertEqual(name, "laptop")
        mode = stat.S_IMODE(os.stat(self.path).st_mode)
        self.assertEqual(mode, 0o600)
        with open(self.path) as fh:
            text = fh.read()
        self.assertNotIn(token, text)
        doc = json.loads(text)
        self.assertEqual(doc["version"], 1)
        entry = doc["keys"][0]
        self.assertEqual(set(entry), {"name", "sha256", "created", "expires",
                                      "revoked", "lanes", "rpm", "budget_tokens"})
        self.assertEqual(entry["sha256"], K.hash_token(token))

    def test_no_temp_file_is_left_behind(self):
        K.add("laptop")
        leftovers = [f for f in os.listdir(self.dir) if f.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_duplicate_name_is_refused_by_default(self):
        K.add("laptop")
        with self.assertRaises(K.KeyNameError):
            K.add("laptop")

    def test_unique_suffixes_on_collision(self):
        self.assertEqual(K.add("laptop", unique=True)[0], "laptop")
        self.assertEqual(K.add("laptop", unique=True)[0], "laptop-2")
        self.assertEqual(K.add("laptop", unique=True)[0], "laptop-3")

    def test_replace_rotates_in_place_and_keeps_limits(self):
        _, old = K.add("laptop", rpm=5, budget_tokens=100, lanes=["flash"])
        K.revoke("laptop")
        name, new = K.add("laptop", replace=True)
        self.assertEqual(name, "laptop")
        doc = K.load()
        self.assertEqual(len(doc["keys"]), 1)
        entry = doc["keys"][0]
        self.assertIsNone(entry["revoked"])
        self.assertEqual((entry["rpm"], entry["budget_tokens"], entry["lanes"]),
                         (5, 100, ["flash"]))
        cache = K.KeyCache()
        self.assertEqual(cache.lookup(old)[1], "unknown")
        self.assertEqual(cache.lookup(new)[1], "ok")

    def test_replace_of_a_missing_name_creates_it(self):
        self.assertEqual(K.add("fresh", replace=True)[0], "fresh")

    def test_limits_are_validated(self):
        for kwargs in ({"rpm": 0}, {"rpm": -1}, {"rpm": True}, {"budget_tokens": 1.5},
                       {"lanes": []}, {"lanes": "flash"}, {"lanes": [3]}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(K.KeyNameError):
                    K.add("x", **kwargs)
        self.assertFalse(os.path.exists(self.path))


class TestLoad(StoreCase):
    def test_missing_file_is_an_empty_store(self):
        self.assertEqual(K.load(), {"version": 1, "keys": []})

    def test_corrupt_json_raises(self):
        with open(self.path, "w") as fh:
            fh.write("{not json")
        with self.assertRaises(K.KeyStoreError):
            K.load()

    def test_wrong_shape_raises(self):
        for doc in ([], {"version": 2, "keys": []}, {"version": 1, "keys": [{"name": "a"}]},
                    {"version": 1, "keys": [{"name": "a", "sha256": "zz"}]}):
            with self.subTest(doc=doc):
                with open(self.path, "w") as fh:
                    json.dump(doc, fh)
                with self.assertRaises(K.KeyStoreError):
                    K.load()


class TestLifecycle(StoreCase):
    def test_revoke_keeps_the_entry_for_audit(self):
        _, token = K.add("laptop")
        K.revoke("laptop")
        doc = K.load()
        self.assertEqual(len(doc["keys"]), 1)
        self.assertIsNotNone(doc["keys"][0]["revoked"])
        self.assertEqual(K.status(doc["keys"][0]), "revoked")
        self.assertEqual(K.KeyCache().lookup(token), (None, "revoked"))

    def test_revoke_unknown_raises(self):
        with self.assertRaises(K.KeyNameError):
            K.revoke("ghost")

    def test_expiry(self):
        past = K.iso(datetime.datetime.now(UTC) - datetime.timedelta(days=1))
        future = K.iso(datetime.datetime.now(UTC) + datetime.timedelta(days=1))
        _, t_past = K.add("old", expires=past)
        _, t_future = K.add("new", expires=future)
        cache = K.KeyCache()
        self.assertEqual(cache.lookup(t_past), (None, "expired"))
        self.assertEqual(cache.lookup(t_future)[1], "ok")

    def test_unparseable_expiry_counts_as_expired(self):
        self.assertEqual(K.status({"name": "a", "revoked": None, "expires": "soon"}), "expired")

    def test_update_sets_and_clears(self):
        K.add("laptop", rpm=5)
        K.update("laptop", rpm=None, budget_tokens=1000)
        entry = K.find(K.load(), "laptop")
        self.assertIsNone(entry["rpm"])
        self.assertEqual(entry["budget_tokens"], 1000)
        with self.assertRaises(K.KeyNameError):
            K.update("laptop", sha256="00" * 32)


class TestKeyCache(StoreCase):
    def test_missing_file_means_unknown(self):
        self.assertEqual(K.KeyCache().lookup("fk-x-" + "a" * 32), (None, "unknown"))

    def test_revoke_is_seen_on_the_next_lookup_without_a_new_cache(self):
        _, token = K.add("laptop")
        cache = K.KeyCache()
        self.assertEqual(cache.lookup(token)[0]["name"], "laptop")
        K.revoke("laptop")
        self.bump_mtime()
        self.assertEqual(cache.lookup(token), (None, "revoked"))

    def test_corrupt_file_raises_through_the_cache(self):
        K.add("laptop")
        cache = K.KeyCache()
        with open(self.path, "w") as fh:
            fh.write("garbage")
        self.bump_mtime()
        with self.assertRaises(K.KeyStoreError):
            cache.lookup("fk-laptop-" + "a" * 32)

    def test_env_path_is_read_per_lookup(self):
        _, token = K.add("laptop")
        cache = K.KeyCache()
        self.assertEqual(cache.lookup(token)[1], "ok")
        other = os.path.join(self.dir, "other.json")
        with mock.patch.dict(os.environ, {"FERRY_KEYS_FILE": other}):
            self.assertEqual(cache.lookup(token), (None, "unknown"))


class TestLaneAllowed(unittest.TestCase):
    def test_rules(self):
        self.assertTrue(K.lane_allowed(None, "heavy", "domestic.heavy"))
        self.assertTrue(K.lane_allowed(["flash"], "flash", "domestic.flash"))
        self.assertTrue(K.lane_allowed(["flash"], "international.flash", "international.flash"))
        self.assertTrue(K.lane_allowed(["domestic.flash"], "flash", "domestic.flash"))
        self.assertFalse(K.lane_allowed(["domestic.flash"], "flash", "international.flash"))
        self.assertFalse(K.lane_allowed(["flash"], "heavy", "domestic.heavy"))
        self.assertTrue(K.lane_allowed(["heavy"], "orch", "orch"))
        self.assertFalse(K.lane_allowed(["flash"], None, None))


if __name__ == "__main__":
    unittest.main(verbosity=2)
```

- [ ] **Step 2: Control run. The test must fail against the pre-change tree.**

Run: `cd /abs/worktree && python3 lib/ferry-keys-store.test.py`
Expected: an `ImportError` / `ModuleNotFoundError: No module named 'ferry_keys'`. The module does not exist yet, so this is the control.

- [ ] **Step 3: Write the implementation**

Create `front/ferry_keys.py`:

```python
"""Per-device client keys: the store shared by the front door, `ferry keys`
and ferry-dash.

A key is `fk-<slug>-<32 base32 chars>`. Only its sha256 is stored, in
~/.config/ferry/keys.json (0600, atomic replace). The front door holds a
KeyCache per worker that re-reads the file when its (mtime, size, inode)
changes, so a revoke from the CLI takes effect on the next request with no
restart. Writers take an flock on keys.json.lock, because an enroll (a front
worker) and the CLI can write at the same moment.

Fail-closed: an unreadable or corrupt store raises KeyStoreError and the front
door refuses every fk- key. The master key never reaches this module.

Stdlib only. Paths resolve from the environment on every call
(FERRY_KEYS_FILE), never at import, so tests cannot touch the real file.
"""
from __future__ import annotations

import base64
import contextlib
import datetime
import hashlib
import json
import os
import re
import secrets
import tempfile

KEY_PREFIX = "fk-"
SLUG_MAX = 24
NAME_MAX = 63
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_FIELDS = ("name", "sha256", "created", "expires", "revoked",
           "lanes", "rpm", "budget_tokens")
_MUTABLE = ("expires", "lanes", "rpm", "budget_tokens")


class KeyStoreError(Exception):
    """keys.json is unreadable or corrupt; fk- keys are refused."""


class KeyNameError(ValueError):
    """A bad, duplicate or unknown key name, or a bad limit value."""


def keys_path(env=None) -> str:
    env = os.environ if env is None else env
    return env.get("FERRY_KEYS_FILE") or os.path.join(
        os.path.expanduser("~"), ".config", "ferry", "keys.json")


def utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def iso(dt: datetime.datetime) -> str:
    return dt.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text: str) -> datetime.datetime:
    return datetime.datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=datetime.timezone.utc)


def expires_from_date(day: str) -> str:
    """'YYYY-MM-DD' -> the instant the key stops working: 00:00Z that day."""
    try:
        dt = datetime.datetime.strptime(day, "%Y-%m-%d")
    except (TypeError, ValueError):
        raise KeyNameError("expiry must be YYYY-MM-DD, got %r" % (day,))
    return iso(dt.replace(tzinfo=datetime.timezone.utc))


def normalize_name(raw) -> str:
    if not isinstance(raw, str):
        raise KeyNameError("key name must be a string, got %r" % (raw,))
    name = re.sub(r"[^a-z0-9-]+", "-", raw.strip().lower())
    name = re.sub(r"-{2,}", "-", name).strip("-")[:NAME_MAX].rstrip("-")
    if not name:
        raise KeyNameError("key name %r has no usable characters" % (raw,))
    return name


def mint_token(name, rand=None) -> str:
    slug = normalize_name(name)[:SLUG_MAX].rstrip("-")
    raw = secrets.token_bytes(20) if rand is None else rand
    body = base64.b32encode(raw).decode("ascii").lower().rstrip("=")
    return "%s%s-%s" % (KEY_PREFIX, slug, body)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _empty() -> dict:
    return {"version": 1, "keys": []}


def _check_limits(lanes, rpm, budget_tokens, err=KeyNameError, where="") -> None:
    if lanes is not None and not (
            isinstance(lanes, list) and lanes
            and all(isinstance(x, str) and x for x in lanes)):
        raise err("%slanes must be null or a non-empty list of lane names" % where)
    for label, value in (("rpm", rpm), ("budget_tokens", budget_tokens)):
        if value is not None and (not isinstance(value, int)
                                  or isinstance(value, bool) or value < 1):
            raise err("%s%s must be null or a positive integer" % (where, label))


def _validate(doc, path) -> None:
    if (not isinstance(doc, dict) or doc.get("version") != 1
            or not isinstance(doc.get("keys"), list)):
        raise KeyStoreError('%s: expected {"version": 1, "keys": [...]}' % path)
    seen = set()
    for entry in doc["keys"]:
        if (not isinstance(entry, dict) or not isinstance(entry.get("name"), str)
                or not isinstance(entry.get("sha256"), str)
                or not _SHA_RE.match(entry["sha256"])):
            raise KeyStoreError("%s: malformed key entry %r" % (path, entry))
        if entry["name"] in seen:
            raise KeyStoreError("%s: duplicate key name %r" % (path, entry["name"]))
        seen.add(entry["name"])
        _check_limits(entry.get("lanes"), entry.get("rpm"),
                      entry.get("budget_tokens"), KeyStoreError,
                      "%s: key %r: " % (path, entry["name"]))


def load(path=None) -> dict:
    path = path or keys_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except FileNotFoundError:
        return _empty()
    except OSError as err:
        raise KeyStoreError("cannot read %s: %s" % (path, err)) from err
    try:
        doc = json.loads(text)
    except ValueError as err:
        raise KeyStoreError("%s is not valid JSON: %s" % (path, err)) from err
    _validate(doc, path)
    return doc


def save(doc, path=None) -> None:
    path = path or keys_path()
    _validate(doc, path)
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".keys-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


@contextlib.contextmanager
def _locked(path):
    import fcntl
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, mode=0o700, exist_ok=True)
    fd = os.open(path + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def find(doc, name):
    for entry in doc["keys"]:
        if entry["name"] == name:
            return entry
    return None


def add(name, *, path=None, expires=None, lanes=None, rpm=None,
        budget_tokens=None, unique=False, replace=False, now=None, rand=None):
    """Mint a key. Returns (final name, plaintext token): the ONLY time the
    token exists outside the client.

    Collision: `replace` rotates the existing entry in place (new hash, new
    created, revoked cleared, limits kept) so the old key dies on the next
    request; `unique` takes the first free `<name>-N`; neither raises."""
    path = path or keys_path()
    base = normalize_name(name)
    _check_limits(lanes, rpm, budget_tokens)
    now = now or utcnow()
    with _locked(path):
        doc = load(path)
        entry = find(doc, base)
        final = base
        if entry is not None and replace:
            token = mint_token(base, rand)
            entry["sha256"] = hash_token(token)
            entry["created"] = iso(now)
            entry["revoked"] = None
            save(doc, path)
            return base, token
        if entry is not None:
            if not unique:
                raise KeyNameError("a key named %r already exists" % base)
            final = None
            for n in range(2, 1000):
                suffix = "-%d" % n
                candidate = base[:NAME_MAX - len(suffix)].rstrip("-") + suffix
                if find(doc, candidate) is None:
                    final = candidate
                    break
            if final is None:
                raise KeyNameError("no free name left for %r" % base)
        token = mint_token(final, rand)
        doc["keys"].append({
            "name": final, "sha256": hash_token(token), "created": iso(now),
            "expires": expires, "revoked": None, "lanes": lanes,
            "rpm": rpm, "budget_tokens": budget_tokens,
        })
        save(doc, path)
        return final, token


def revoke(name, *, path=None, now=None) -> dict:
    path = path or keys_path()
    with _locked(path):
        doc = load(path)
        entry = find(doc, name)
        if entry is None:
            raise KeyNameError("no key named %r" % name)
        if entry.get("revoked") is None:
            entry["revoked"] = iso(now or utcnow())
            save(doc, path)
        return dict(entry)


def update(name, *, path=None, **changes) -> dict:
    bad = sorted(set(changes) - set(_MUTABLE))
    if bad:
        raise KeyNameError("cannot change %s (allowed: %s)"
                           % (", ".join(bad), ", ".join(_MUTABLE)))
    path = path or keys_path()
    with _locked(path):
        doc = load(path)
        entry = find(doc, name)
        if entry is None:
            raise KeyNameError("no key named %r" % name)
        merged = dict(entry)
        merged.update(changes)
        _check_limits(merged.get("lanes"), merged.get("rpm"),
                      merged.get("budget_tokens"))
        entry.update(changes)
        save(doc, path)
        return dict(entry)


def status(entry, now=None) -> str:
    if entry.get("revoked"):
        return "revoked"
    expires = entry.get("expires")
    if expires:
        try:
            if (now or utcnow()) >= parse_iso(expires):
                return "expired"
        except (TypeError, ValueError):
            return "expired"  # fail closed on an unreadable expiry
    return "active"


def lane_allowed(lanes, requested, resolved) -> bool:
    """Whether a key restricted to `lanes` may use this request's model.

    `lanes` may name bare lanes ("flash": any fleet's flash) or fleet lanes
    ("domestic.flash": only that one). Both the model the client sent and
    the one fleet resolution produced are checked, and orch/orchestrator
    count as heavy, mirroring ferry_front.LEGACY_HEAVY."""
    if lanes is None:
        return True
    candidates = set()
    for model in (requested, resolved):
        if isinstance(model, str) and model:
            candidates.add(model)
            if "." in model:
                candidates.add(model.split(".", 1)[1])
            if model in ("orch", "orchestrator"):
                candidates.add("heavy")
    return bool(candidates & set(lanes))


class KeyCache:
    """Per-process token lookup, re-read when keys.json changes on disk."""

    def __init__(self, path_fn=None) -> None:
        self._path_fn = path_fn or keys_path
        self._path = None
        self._sig = None
        self._by_hash = {}

    def lookup(self, token, now=None):
        path = self._path_fn()
        try:
            st = os.stat(path)
        except FileNotFoundError:
            self._path, self._sig, self._by_hash = path, None, {}
            return None, "unknown"
        except OSError as err:
            raise KeyStoreError("cannot stat %s: %s" % (path, err)) from err
        sig = (st.st_mtime_ns, st.st_size, st.st_ino)
        if path != self._path or sig != self._sig:
            doc = load(path)
            self._by_hash = {e["sha256"]: e for e in doc["keys"]}
            self._path, self._sig = path, sig
        entry = self._by_hash.get(hash_token(token))
        if entry is None:
            return None, "unknown"
        state = status(entry, now)
        return (dict(entry), "ok") if state == "active" else (None, state)
```

- [ ] **Step 4: Run the tests and confirm they pass**

Run: `cd /abs/worktree && python3 lib/ferry-keys-store.test.py`
Expected: `OK`, 26 tests (advisory).

- [ ] **Step 5: Commit**

Write `/tmp/ferry-keys-t1-msg.txt` with the Write tool:

```
feat(keys): per-device key store (front/ferry_keys.py)

Hash-only keys.json (0600, atomic, flock), mint/revoke/update, expiry,
lane rules, and an mtime-reloaded KeyCache for the front door.

Claude-Session-Id: b358f2c8-b360-463c-a431-5b7dd8808316
```

Run: `cd /abs/worktree && git add front/ferry_keys.py lib/ferry-keys-store.test.py && git commit -F /tmp/ferry-keys-t1-msg.txt`

---

### Task 2: Usage DB and limits — `front/ferry_keys_usage.py`

**Files:**
- Create: `front/ferry_keys_usage.py`
- Test: `lib/ferry-keys-usage.test.py` (new)

**Interfaces:**
- Consumes: nothing. The module is independent of Task 1.
- Produces (used by Tasks 4, 8, 9):
  - `usage_path(env=None) -> str`: `$FERRY_KEYS_DB`, else `~/.config/ferry/keys-usage.sqlite`, resolved per call.
  - `class UsageError(Exception)`
  - `Verdict = namedtuple("Verdict", "ok reason retry_after")`. `reason` is `""`, `"rpm"` or `"budget"`, and `retry_after` is an int number of seconds (0 when `ok`).
  - `class Usage(path=None, clock=time.time)` with these methods:
    - `.admit(name, rpm=None, budget_tokens=None, now=None) -> Verdict`. It runs atomically across processes, checks RPM first and then the budget, and increments the minute counter only when the request is admitted.
    - `.add_tokens(name, tokens, now=None) -> None`
    - `.month_tokens(name, now=None) -> int`
    - `.minute_requests(name, now=None) -> int`
    - `.summary(now=None) -> {name: {"month_tokens": int, "minute_requests": int}}`. It returns `{}` and does **not** create the file when the file does not exist.
  - Every sqlite or OS failure surfaces as `UsageError`.
  - `month_of(now) -> int` (yyyymm, UTC), `seconds_to_next_month(now) -> int`

- [ ] **Step 1: Write the failing test**

Create `lib/ferry-keys-usage.test.py`:

```python
#!/usr/bin/env python3
"""Stdlib unittest for front/ferry_keys_usage.py — RPM and monthly token counters.

Run:  python3 lib/ferry-keys-usage.test.py

The front door runs FERRY_FRONT_WORKERS (default 4) processes, so the counters
live in one shared SQLite file. The two-process test below is the one that
proves an RPM limit is a host-wide limit and not a per-worker one.
"""
import calendar
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRONT = os.path.join(REPO, "front")
sys.path.insert(0, FRONT)

import ferry_keys_usage as U  # noqa: E402

# 2026-09-27T12:00:30Z — mid-minute, mid-month.
NOW = calendar.timegm((2026, 9, 27, 12, 0, 30))


class UsageCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ferry-keys-usage-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "keys-usage.sqlite")
        self.u = U.Usage(self.path)


class TestAdmit(UsageCase):
    def test_unlimited_key_is_admitted_and_counted(self):
        for _ in range(3):
            self.assertEqual(self.u.admit("a", now=NOW), U.Verdict(True, "", 0))
        self.assertEqual(self.u.minute_requests("a", now=NOW), 3)

    def test_rpm_limit_refuses_and_does_not_count_the_refusal(self):
        self.assertTrue(self.u.admit("a", rpm=2, now=NOW).ok)
        self.assertTrue(self.u.admit("a", rpm=2, now=NOW).ok)
        v = self.u.admit("a", rpm=2, now=NOW)
        self.assertEqual((v.ok, v.reason, v.retry_after), (False, "rpm", 30))
        self.assertEqual(self.u.minute_requests("a", now=NOW), 2)

    def test_rpm_window_rolls_over(self):
        self.u.admit("a", rpm=1, now=NOW)
        self.assertFalse(self.u.admit("a", rpm=1, now=NOW).ok)
        self.assertTrue(self.u.admit("a", rpm=1, now=NOW + 60).ok)

    def test_budget_refuses_at_or_over_and_retries_next_month(self):
        self.u.add_tokens("a", 100, now=NOW)
        v = self.u.admit("a", budget_tokens=100, now=NOW)
        self.assertEqual((v.ok, v.reason), (False, "budget"))
        self.assertEqual(v.retry_after,
                         calendar.timegm((2026, 10, 1, 0, 0, 0)) - NOW)
        self.assertTrue(self.u.admit("a", budget_tokens=101, now=NOW).ok)

    def test_rpm_is_checked_before_budget(self):
        self.u.add_tokens("a", 100, now=NOW)
        self.u.admit("a", now=NOW)
        v = self.u.admit("a", rpm=1, budget_tokens=10, now=NOW)
        self.assertEqual(v.reason, "rpm")

    def test_keys_are_independent(self):
        self.u.admit("a", rpm=1, now=NOW)
        self.assertTrue(self.u.admit("b", rpm=1, now=NOW).ok)


class TestTokens(UsageCase):
    def test_tokens_accumulate_per_month(self):
        self.u.add_tokens("a", 10, now=NOW)
        self.u.add_tokens("a", 5, now=NOW)
        self.assertEqual(self.u.month_tokens("a", now=NOW), 15)
        october = calendar.timegm((2026, 10, 2, 0, 0, 0))
        self.assertEqual(self.u.month_tokens("a", now=october), 0)

    def test_december_rolls_to_january(self):
        dec = calendar.timegm((2026, 12, 31, 23, 59, 0))
        self.assertEqual(U.month_of(dec), 202612)
        self.assertEqual(U.seconds_to_next_month(dec), 60)

    def test_prune_drops_old_rows(self):
        self.u.admit("a", now=NOW - 600)
        self.u.add_tokens("a", 7, now=calendar.timegm((2025, 1, 5, 0, 0, 0)))
        self.u.admit("a", now=NOW)
        self.u.add_tokens("a", 1, now=NOW)
        import sqlite3
        conn = sqlite3.connect(self.path)
        try:
            minutes = conn.execute("SELECT minute_epoch FROM minute").fetchall()
            months = conn.execute("SELECT yyyymm FROM month").fetchall()
        finally:
            conn.close()
        self.assertEqual(minutes, [(int(NOW // 60),)])
        self.assertEqual(months, [(202609,)])

    def test_summary(self):
        self.u.admit("a", now=NOW)
        self.u.add_tokens("b", 9, now=NOW)
        self.assertEqual(self.u.summary(now=NOW), {
            "a": {"month_tokens": 0, "minute_requests": 1},
            "b": {"month_tokens": 9, "minute_requests": 0}})

    def test_summary_of_a_missing_file_does_not_create_it(self):
        self.assertEqual(self.u.summary(now=NOW), {})
        self.assertFalse(os.path.exists(self.path))


class TestFiles(UsageCase):
    def test_db_is_0600_and_wal(self):
        self.u.admit("a", now=NOW)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        import sqlite3
        conn = sqlite3.connect(self.path)
        try:
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        finally:
            conn.close()

    def test_unopenable_db_raises_usage_error(self):
        bad = U.Usage(self.dir)  # a directory is not a database
        with self.assertRaises(U.UsageError):
            bad.admit("a", now=NOW)

    def test_env_path(self):
        self.assertEqual(U.usage_path({"FERRY_KEYS_DB": "/x/y.sqlite"}), "/x/y.sqlite")
        self.assertTrue(U.usage_path({}).endswith(
            os.path.join(".config", "ferry", "keys-usage.sqlite")))


WORKER = r"""
import sys
sys.path.insert(0, sys.argv[1])
import ferry_keys_usage as U
u = U.Usage(sys.argv[2])
now = float(sys.argv[3])
print(sum(1 for _ in range(30) if u.admit("shared", rpm=25, now=now).ok))
"""


class TestTwoProcesses(UsageCase):
    def test_rpm_is_shared_across_processes(self):
        U.Usage(self.path).admit("warmup", now=NOW)  # create schema first
        procs = [subprocess.Popen([sys.executable, "-c", WORKER, FRONT, self.path, str(NOW)],
                                  stdout=subprocess.PIPE, text=True) for _ in range(2)]
        admitted = sum(int(p.communicate(timeout=60)[0].strip()) for p in procs)
        self.assertEqual(admitted, 25)
        self.assertEqual(self.u.minute_requests("shared", now=NOW), 25)


if __name__ == "__main__":
    unittest.main(verbosity=2)
```

- [ ] **Step 2: Control run.** Run `cd /abs/worktree && python3 lib/ferry-keys-usage.test.py`. Expected: `ModuleNotFoundError: No module named 'ferry_keys_usage'`.

- [ ] **Step 3: Write the implementation**

Create `front/ferry_keys_usage.py`:

```python
"""Per-key request and token counters for client keys, shared by every
front-door worker through one SQLite file (WAL).

Why a file and not memory: the front door runs FERRY_FRONT_WORKERS (default
4) processes, and a per-process counter would let a key through at 4x its
RPM. admit() does its read-check-increment inside BEGIN IMMEDIATE, which
takes SQLite's write lock, so two workers cannot both see "one left".

Windows: RPM is a fixed calendar minute (epoch // 60); budgets are calendar
months in UTC. Tokens are added on completion, so a request admitted just
under budget can overshoot by one request (documented, accepted).

Nothing secret lives here (names and counts), but the file is created 0600
anyway. The WAL sidecars (-wal, -shm) are created by SQLite under the
process umask.
"""
from __future__ import annotations

import calendar
import math
import os
import sqlite3
import time
from collections import namedtuple

Verdict = namedtuple("Verdict", "ok reason retry_after")

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS minute (name TEXT NOT NULL, "
    "minute_epoch INTEGER NOT NULL, n INTEGER NOT NULL, "
    "PRIMARY KEY (name, minute_epoch))",
    "CREATE TABLE IF NOT EXISTS month (name TEXT NOT NULL, "
    "yyyymm INTEGER NOT NULL, tokens INTEGER NOT NULL, "
    "PRIMARY KEY (name, yyyymm))",
)


class UsageError(Exception):
    """The usage DB cannot be opened or written; fk- keys are refused (503)."""


def usage_path(env=None) -> str:
    env = os.environ if env is None else env
    return env.get("FERRY_KEYS_DB") or os.path.join(
        os.path.expanduser("~"), ".config", "ferry", "keys-usage.sqlite")


def month_of(now: float) -> int:
    t = time.gmtime(now)
    return t.tm_year * 100 + t.tm_mon


def _months_back(yyyymm: int, n: int) -> int:
    year, month = divmod(yyyymm, 100)
    index = year * 12 + (month - 1) - n
    return (index // 12) * 100 + index % 12 + 1


def seconds_to_next_month(now: float) -> int:
    t = time.gmtime(now)
    year, month = (t.tm_year + 1, 1) if t.tm_mon == 12 else (t.tm_year, t.tm_mon + 1)
    return max(1, int(math.ceil(calendar.timegm((year, month, 1, 0, 0, 0)) - now)))


class Usage:
    def __init__(self, path=None, clock=time.time) -> None:
        self.path = path or usage_path()
        self.clock = clock
        self._ready = False

    def _connect(self) -> sqlite3.Connection:
        try:
            if not self._ready:
                directory = os.path.dirname(self.path) or "."
                os.makedirs(directory, mode=0o700, exist_ok=True)
                fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
                os.close(fd)
            conn = sqlite3.connect(self.path, timeout=5, isolation_level=None)
            if not self._ready:
                conn.execute("PRAGMA journal_mode=WAL")
                for statement in _SCHEMA:
                    conn.execute(statement)
                self._ready = True
            return conn
        except (OSError, sqlite3.Error) as err:
            raise UsageError("key usage db %s: %s" % (self.path, err)) from err

    def _now(self, now):
        return self.clock() if now is None else now

    def admit(self, name, rpm=None, budget_tokens=None, now=None) -> Verdict:
        now = self._now(now)
        minute = int(now // 60)
        month = month_of(now)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute("DELETE FROM minute WHERE minute_epoch < ?", (minute - 1,))
                row = conn.execute(
                    "SELECT n FROM minute WHERE name = ? AND minute_epoch = ?",
                    (name, minute)).fetchone()
                if rpm is not None and (row[0] if row else 0) >= rpm:
                    conn.execute("COMMIT")
                    return Verdict(False, "rpm", max(1, int(math.ceil(60 - now % 60))))
                row = conn.execute(
                    "SELECT tokens FROM month WHERE name = ? AND yyyymm = ?",
                    (name, month)).fetchone()
                if budget_tokens is not None and (row[0] if row else 0) >= budget_tokens:
                    conn.execute("COMMIT")
                    return Verdict(False, "budget", seconds_to_next_month(now))
                conn.execute(
                    "INSERT INTO minute (name, minute_epoch, n) VALUES (?, ?, 1) "
                    "ON CONFLICT (name, minute_epoch) DO UPDATE SET n = n + 1",
                    (name, minute))
                conn.execute("COMMIT")
                return Verdict(True, "", 0)
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        except sqlite3.Error as err:
            raise UsageError("key usage db %s: %s" % (self.path, err)) from err
        finally:
            conn.close()

    def add_tokens(self, name, tokens, now=None) -> None:
        now = self._now(now)
        month = month_of(now)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM month WHERE yyyymm < ?", (_months_back(month, 12),))
            conn.execute(
                "INSERT INTO month (name, yyyymm, tokens) VALUES (?, ?, ?) "
                "ON CONFLICT (name, yyyymm) DO UPDATE SET tokens = tokens + excluded.tokens",
                (name, month, int(tokens)))
            conn.execute("COMMIT")
        except sqlite3.Error as err:
            raise UsageError("key usage db %s: %s" % (self.path, err)) from err
        finally:
            conn.close()

    def _scalar(self, sql, args) -> int:
        conn = self._connect()
        try:
            row = conn.execute(sql, args).fetchone()
            return int(row[0]) if row else 0
        except sqlite3.Error as err:
            raise UsageError("key usage db %s: %s" % (self.path, err)) from err
        finally:
            conn.close()

    def month_tokens(self, name, now=None) -> int:
        return self._scalar("SELECT tokens FROM month WHERE name = ? AND yyyymm = ?",
                            (name, month_of(self._now(now))))

    def minute_requests(self, name, now=None) -> int:
        return self._scalar("SELECT n FROM minute WHERE name = ? AND minute_epoch = ?",
                            (name, int(self._now(now) // 60)))

    def summary(self, now=None) -> dict:
        if not os.path.exists(self.path):
            return {}
        now = self._now(now)
        out = {}
        conn = self._connect()
        try:
            for name, tokens in conn.execute(
                    "SELECT name, tokens FROM month WHERE yyyymm = ?", (month_of(now),)):
                out.setdefault(name, {"month_tokens": 0, "minute_requests": 0})
                out[name]["month_tokens"] = int(tokens)
            for name, n in conn.execute(
                    "SELECT name, n FROM minute WHERE minute_epoch = ?", (int(now // 60),)):
                out.setdefault(name, {"month_tokens": 0, "minute_requests": 0})
                out[name]["minute_requests"] = int(n)
        except sqlite3.Error as err:
            raise UsageError("key usage db %s: %s" % (self.path, err)) from err
        finally:
            conn.close()
        return out
```

- [ ] **Step 4: Run** `cd /abs/worktree && python3 lib/ferry-keys-usage.test.py`. Expected: `OK`, 16 tests (advisory). If `test_prune_drops_old_rows` fails, recheck the minute prune bound (`< minute - 1` keeps the current and previous minute). The test admits at `NOW` after `NOW-600`, so only `NOW`'s minute survives.

- [ ] **Step 5: Commit.** Write `/tmp/ferry-keys-t2-msg.txt`:

```
feat(keys): shared per-key RPM and monthly token counters

One WAL SQLite file for every front worker; admit() checks RPM then the
token budget under BEGIN IMMEDIATE and counts only admitted requests.

Claude-Session-Id: b358f2c8-b360-463c-a431-5b7dd8808316
```

Run: `cd /abs/worktree && git add front/ferry_keys_usage.py lib/ferry-keys-usage.test.py && git commit -F /tmp/ferry-keys-t2-msg.txt`

---

### Task 3: Front-door authentication — `front/ferry_front.py`

**Files:**
- Modify: `front/ferry_front.py`. Citations are valid at `43acdda`, and HEAD may have moved, so resolve each one by its symbol:
  - the import block, `:42 (import importlib.machinery)`;
  - after `_bearer_ok`, `:1070 (_bearer_ok)`;
  - `caller_identity`, `:952 (caller_identity)`;
  - the top of `LaneCatalogueFilter.__call__`, `:1156 (__call__)`;
  - `LaneCatalogueFilter._reply`, `:1531 (_reply)`.
- Test: `lib/ferry-front-keys.test.py` (new). This is deliberately **not** `lib/ferry-front.test.py`. That file is about 2700 lines of shared fixtures, and Tasks 4 and 5 run in parallel seats.

**Interfaces:**
- Consumes the Task 1 names `KeyCache`, `KeyStoreError` and `add`. Tests only: `add`, `revoke`, `hash_token`.
- Produces these names in `ferry_front`, which Tasks 4 and 5 rely on:
  - `KEY_PREFIX = "fk-"`, `KEY_SCOPE = "ferry.key"`, `KEY_ENTRY_SCOPE = "ferry.key_entry"`
  - `_front_sibling(name) -> module`: loads `front/<name>.py` whether or not `front/` is on `sys.path`, and shares the `sys.modules` entry.
  - `_keys_module()`, `_key_cache() -> KeyCache`, `_key_warn(err, stream=None, clock=None) -> bool`
  - `_master_key() -> str`
  - `presented_credential(headers: dict) -> (bytes header | None, str | None)`
  - `authenticate(scope) -> None | (int status, str message)`. It always sets `scope["ferry.key"]` to `"master"`, `""` or the key name. On a valid key it also sets `scope["ferry.key_entry"]` (a dict copy of the entry) and rewrites the credential.
  - `is_anthropic_path(path) -> bool`
  - `key_error_body(path, status, message, code=None, openai_type=None) -> dict`
  - `LaneCatalogueFilter._reply(send, status, doc, headers=None)`. The `headers` argument is a list of extra `(bytes, bytes)` pairs.
  - `caller_identity(scope, headers)` returns the key name whenever `scope["ferry.key_entry"]` is set, **ahead of** `X-Ferry-Client`, so a device cannot claim another identity's sticky fleet.

**Behaviour contract:**

| Credential presented | Master configured | Result |
|---|---|---|
| none | any | pass through untouched, `ferry.key = ""` |
| equals `LITELLM_MASTER_KEY` (`hmac.compare_digest`) | yes | pass through untouched, `ferry.key = "master"` |
| non-`fk-` string | any | pass through untouched (litellm judges it), `ferry.key = ""` |
| `fk-…`, active | yes | the header is rewritten to the master (Authorization becomes `Bearer <master>`, x-api-key becomes `<master>`), `ferry.key = <name>` |
| `fk-…`, active | no | the credential header is removed (litellm has no auth), `ferry.key = <name>` |
| `fk-…`, unknown, revoked or expired | any | 401 in the path's error shape; litellm never sees it |
| `fk-…`, store corrupt or unreadable | any | 401 "…store is unreadable…", one rate-limited stderr line; the master key still works |
| any refusal on a `websocket` scope | any | `{"type": "websocket.close", "code": 1008}`, sent before accept |

- [ ] **Step 1: Write the failing test**

Create `lib/ferry-front-keys.test.py`:

```python
#!/usr/bin/env python3
"""Per-device client keys at the front door (front/ferry_front.py).

Run:  python3 lib/ferry-front-keys.test.py

Every test runs with FERRY_KEYS_FILE / FERRY_KEYS_DB in a temp dir and a fake
LITELLM_MASTER_KEY, drives LaneCatalogueFilter directly with a recording ASGI
app, and never imports litellm or binds a port.

Task 3 owns the authentication tests; Task 4 appends limit and accounting
tests to this same file.
"""
import asyncio
import datetime
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "front"))

import ferry_front as FF  # noqa: E402
import ferry_keys as K  # noqa: E402

MASTER = "sk-test-master"

# Copied from lib/ferry-front.test.py FLEETS (valid at 43acdda) — a test file
# must not import another test file.
FLEETS = {"domestic": {"heavy": "chatgpt/responses/gpt-5.6-sol",
                       "medium": "chatgpt/responses/gpt-5.6-terra",
                       "flash": "openrouter/~google/gemini-flash-latest",
                       "super-flash": "openrouter/~google/gemini-flash-latest"},
          "international": {"heavy": "anthropic/k3",
                            "medium": "zai/glm-5.3",
                            "flash": "zai/glm-5.3-flash",
                            "super-flash": "zai/glm-5.3-flash"}}

USAGE_BODY = json.dumps({"id": "x", "choices": [],
                         "usage": {"prompt_tokens": 7, "completion_tokens": 5}}).encode()


class Upstream:
    """Stands in for litellm: records what it was handed, answers 200 JSON."""

    def __init__(self, payload=USAGE_BODY):
        self.payload = payload
        self.calls = 0
        self.scope = None
        self.send = None
        self.body = None

    async def __call__(self, scope, receive, send):
        self.calls += 1
        self.scope = scope
        self.send = send
        if scope.get("type") == "websocket":
            await send({"type": "websocket.accept"})
            return
        buf = b""
        while True:
            msg = await receive()
            buf += msg.get("body", b"")
            if not msg.get("more_body"):
                break
        self.body = buf
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": self.payload,
                    "more_body": False})

    def headers(self):
        return {bytes(k).lower(): bytes(v) for k, v in self.scope["headers"]}


def drive(mw, path, headers=(), body=b"{}", method="POST",
          client=("100.64.0.9", 50000), scope_type="http"):
    scope = {"type": scope_type, "path": path, "method": method, "client": client,
             "headers": [(k.encode(), v.encode()) for k, v in headers]}
    sent = []

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(mw(scope, receive, send))
    return scope, sent, send


def reply(sent):
    """(status, {header: value}, parsed body) of the first response in `sent`."""
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    headers = {bytes(k).lower(): bytes(v) for k, v in start.get("headers", [])}
    try:
        doc = json.loads(body)
    except ValueError:
        doc = None
    return start["status"], headers, doc


def bearer(token):
    return [("authorization", "Bearer " + token)]


class KeyFrontCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ferry-front-keys-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.keys = os.path.join(self.dir, "keys.json")
        self.db = os.path.join(self.dir, "keys-usage.sqlite")
        env = {"FERRY_KEYS_FILE": self.keys, "FERRY_KEYS_DB": self.db,
               "LITELLM_MASTER_KEY": MASTER, "FERRY_EVENTS": "0",
               "FERRY_STRIP_HEADERS": "0"}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        FF._KEY_CACHE = None
        FF._KEY_WARNED.clear()
        self.app = Upstream()

    def mw(self, state=None, fleets=None):
        return FF.LaneCatalogueFilter(self.app, frozenset(), fleets=fleets, state=state)

    def mint(self, name="laptop", **kwargs):
        return K.add(name, **kwargs)[1]

    def bump(self):
        st = os.stat(self.keys)
        os.utime(self.keys, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))


class TestPassThrough(KeyFrontCase):
    def test_master_and_bare_requests_touch_no_key_state(self):
        for headers, label in ((bearer(MASTER), "master"), ((), "")):
            with self.subTest(label=label):
                scope, _, send = drive(self.mw(), "/v1/chat/completions", headers)
                self.assertEqual(self.app.headers().get(b"authorization"),
                                 dict(headers).get("authorization", "").encode() or None)
                self.assertEqual(scope["ferry.key"], label)
                self.assertNotIn("ferry.key_entry", scope)
                self.assertIs(self.app.send, send)
        self.assertFalse(os.path.exists(self.keys))
        self.assertFalse(os.path.exists(self.db))

    def test_a_non_ferry_credential_is_litellms_business(self):
        scope, _, _ = drive(self.mw(), "/v1/chat/completions", bearer("sk-something-else"))
        self.assertEqual(self.app.headers()[b"authorization"], b"Bearer sk-something-else")
        self.assertEqual(scope["ferry.key"], "")


class TestDeviceKeys(KeyFrontCase):
    def test_device_key_is_rewritten_to_the_master(self):
        token = self.mint()
        scope, sent, _ = drive(self.mw(), "/v1/chat/completions", bearer(token))
        self.assertEqual(reply(sent)[0], 200)
        self.assertEqual(self.app.headers()[b"authorization"], b"Bearer " + MASTER.encode())
        self.assertEqual(scope["ferry.key"], "laptop")
        self.assertEqual(scope["ferry.key_entry"]["name"], "laptop")
        self.assertNotIn(token.encode(), b"".join(v for _, v in self.app.scope["headers"]))

    def test_device_key_is_stripped_when_no_master_is_configured(self):
        token = self.mint()
        with mock.patch.dict(os.environ, {"LITELLM_MASTER_KEY": ""}):
            scope, _, _ = drive(self.mw(), "/v1/models", bearer(token), method="GET")
        self.assertNotIn(b"authorization", self.app.headers())
        self.assertEqual(scope["ferry.key"], "laptop")

    def test_x_api_key_carrying_a_device_key_is_rewritten(self):
        token = self.mint()
        drive(self.mw(), "/v1/messages", [("x-api-key", token),
                                          ("anthropic-version", "2023-06-01")])
        headers = self.app.headers()
        self.assertEqual(headers[b"x-api-key"], MASTER.encode())
        self.assertEqual(headers[b"anthropic-version"], b"2023-06-01")

    def test_refused_keys_are_401_and_never_reach_litellm(self):
        _, revoked = K.add("gone")
        K.revoke("gone")
        past = K.iso(datetime.datetime.now(datetime.timezone.utc)
                     - datetime.timedelta(days=1))
        _, expired = K.add("old", expires=past)
        cases = {"unknown": "fk-nobody-" + "a" * 32, "revoked": revoked,
                 "expired": expired}
        for reason, token in cases.items():
            for path, method in (("/v1/chat/completions", "POST"),
                                 ("/v1/models", "GET")):
                with self.subTest(reason=reason, path=path):
                    _, sent, _ = drive(self.mw(), path, bearer(token), method=method)
                    status, _, doc = reply(sent)
                    self.assertEqual(status, 401)
                    self.assertEqual(doc["error"]["code"], "invalid_api_key")
                    self.assertEqual(doc["error"]["type"], "invalid_request_error")
                    self.assertIn(reason if reason != "unknown" else "unknown",
                                  doc["error"]["message"])
        self.assertEqual(self.app.calls, 0)

    def test_anthropic_path_gets_the_anthropic_error_shape(self):
        _, sent, _ = drive(self.mw(), "/v1/messages",
                           [("x-api-key", "fk-nobody-" + "a" * 32)])
        status, _, doc = reply(sent)
        self.assertEqual(status, 401)
        self.assertEqual(doc["type"], "error")
        self.assertEqual(doc["error"]["type"], "authentication_error")

    def test_revoke_takes_effect_on_the_next_request(self):
        token = self.mint()
        mw = self.mw()
        self.assertEqual(reply(drive(mw, "/v1/models", bearer(token), method="GET")[1])[0], 200)
        K.revoke("laptop")
        self.bump()
        self.assertEqual(reply(drive(mw, "/v1/models", bearer(token), method="GET")[1])[0], 401)

    def test_corrupt_store_refuses_device_keys_but_not_the_master(self):
        token = self.mint()
        with open(self.keys, "w") as fh:
            fh.write("{corrupt")
        self.bump()
        with mock.patch("sys.stderr") as err:
            _, sent, _ = drive(self.mw(), "/v1/chat/completions", bearer(token))
            status, _, doc = reply(sent)
            self.assertEqual(status, 401)
            self.assertIn("unreadable", doc["error"]["message"])
            drive(self.mw(), "/v1/chat/completions", bearer(token))
        warned = "".join(str(c.args[0]) for c in err.write.call_args_list if c.args)
        self.assertEqual(warned.count("ferry-front: client keys:"), 1)
        _, sent, _ = drive(self.mw(), "/v1/chat/completions", bearer(MASTER))
        self.assertEqual(reply(sent)[0], 200)

    def test_websocket_with_a_revoked_key_is_closed(self):
        _, token = K.add("ws")
        K.revoke("ws")
        _, sent, _ = drive(self.mw(), "/v1/realtime", bearer(token),
                           method="GET", scope_type="websocket")
        self.assertEqual(sent, [{"type": "websocket.close", "code": 1008}])
        self.assertEqual(self.app.calls, 0)

    def test_websocket_with_a_valid_key_is_rewritten(self):
        token = self.mint()
        drive(self.mw(), "/v1/realtime", bearer(token), method="GET",
              scope_type="websocket")
        self.assertEqual(self.app.headers()[b"authorization"], b"Bearer " + MASTER.encode())


class TestIdentity(KeyFrontCase):
    def setUp(self):
        super().setUp()
        self.state_path = os.path.join(self.dir, "fleets.json")
        with open(self.state_path, "w") as fh:
            json.dump({"default": "domestic", "clients": {}}, fh)
        self.state = FF.FleetState(self.state_path, FLEETS)

    def test_fleet_get_names_the_key_not_the_claimed_client(self):
        token = self.mint("mbp")
        headers = bearer(token) + [("x-ferry-client", "someone-else")]
        _, sent, _ = drive(self.mw(self.state, FLEETS), FF.FLEET_PATH, headers, method="GET")
        status, _, doc = reply(sent)
        self.assertEqual(status, 200)
        self.assertEqual(doc["you"], "mbp")

    def test_sticky_selection_is_stored_under_the_key_name(self):
        token = self.mint("mbp")
        _, sent, _ = drive(self.mw(self.state, FLEETS), FF.FLEET_PATH, bearer(token),
                           body=json.dumps({"fleet": "international"}).encode())
        self.assertEqual(reply(sent)[0], 200)
        with open(self.state_path) as fh:
            self.assertEqual(json.load(fh)["clients"], {"mbp": "international"})

    def test_caller_identity_without_a_key_is_unchanged(self):
        scope = {"client": ("100.64.0.9", 1)}
        self.assertEqual(FF.caller_identity(scope, {b"x-ferry-client": b"named"}), "named")
        self.assertEqual(FF.caller_identity({"client": ("127.0.0.1", 1)}, {}), "host")


class TestErrorBodies(unittest.TestCase):
    def test_shapes(self):
        self.assertEqual(FF.key_error_body("/v1/chat/completions", 403, "no"),
                         {"error": {"message": "no", "type": "invalid_request_error",
                                    "param": None, "code": "model_not_allowed"}})
        self.assertEqual(FF.key_error_body("/v1/messages/count_tokens", 429, "slow"),
                         {"type": "error", "error": {"type": "rate_limit_error",
                                                     "message": "slow"}})
        self.assertEqual(FF.key_error_body("/v1/responses", 429, "q", code="insufficient_quota",
                                           openai_type="insufficient_quota")["error"]["type"],
                         "insufficient_quota")


if __name__ == "__main__":
    unittest.main(verbosity=2)
```

- [ ] **Step 2: Control run against the pre-change `ferry_front.py`**

Run: `cd /abs/worktree && python3 lib/ferry-front-keys.test.py`

Expected: errors, not passes.
- The first failure is `AttributeError: module 'ferry_front' has no attribute '_KEY_CACHE'`, raised in `setUp`. That error is itself the control.
- For a stronger control, temporarily comment out the two `FF._KEY_CACHE` / `FF._KEY_WARNED` lines in `setUp` and re-run. You should then see:
  - `KeyError: 'ferry.key'` in `test_master_and_bare_requests_touch_no_key_state`;
  - `AssertionError: b'Bearer fk-laptop-…' != b'Bearer sk-test-master'` in `test_device_key_is_rewritten_to_the_master`.
  This proves the assertions can fail on the old code. Restore the lines afterwards.

- [ ] **Step 3: Implement**

3a. In the import block (anchor `import importlib.machinery`), add `import hmac` directly above it:

```python
import hmac
import importlib.machinery
```

3b. Directly after the `_bearer_ok` function, insert:

```python
# ── per-device client keys ───────────────────────────────────────────────────
# A client may present `fk-<name>-<random>` instead of the master key. The
# front door checks it against ~/.config/ferry/keys.json (front/ferry_keys.py),
# then REWRITES the credential to the master before litellm sees the request,
# so litellm's own auth is unchanged. The master key keeps working untouched.
# Only an `fk-` credential ever reads the key store: master and bare requests
# stay one header compare. Fail-closed: a corrupt store refuses fk- keys (and
# says so on stderr, rate-limited); it never lets them through.
KEY_PREFIX = "fk-"
KEY_SCOPE = "ferry.key"
KEY_ENTRY_SCOPE = "ferry.key_entry"
KEY_WARN_INTERVAL = 60.0
_KEY_CACHE = None
_KEY_WARNED: dict = {}
_KEY_REASONS = {
    "unknown": "unknown ferry device key",
    "revoked": "this ferry device key has been revoked",
    "expired": "this ferry device key has expired",
}
ANTHROPIC_PATHS = ("/v1/messages", "/messages")
_OPENAI_ERRORS = {401: ("invalid_request_error", "invalid_api_key"),
                  403: ("invalid_request_error", "model_not_allowed"),
                  429: ("requests", "rate_limit_exceeded"),
                  503: ("server_error", "key_store_unavailable")}
_ANTHROPIC_ERRORS = {401: "authentication_error", 403: "permission_error",
                     429: "rate_limit_error", 503: "api_error"}


def _front_sibling(name: str):
    """Import front/<name>.py whether or not front/ is on sys.path.

    Shares the sys.modules entry, so a test that did `import ferry_keys`
    and this loader hand out the same module (and the same exception
    classes)."""
    if name in sys.modules:
        return sys.modules[name]
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), name + ".py")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(name, None)
        raise
    return module


def _keys_module():
    return _front_sibling("ferry_keys")


def _key_cache():
    global _KEY_CACHE
    if _KEY_CACHE is None:
        _KEY_CACHE = _keys_module().KeyCache()
    return _KEY_CACHE


def _key_warn(err, stream=None, clock=None) -> bool:
    """One stderr line per distinct key-store error per KEY_WARN_INTERVAL."""
    try:
        key = (type(err).__name__, str(err))
        now = (clock or time.monotonic)()
        last = _KEY_WARNED.get(key)
        if last is not None and now - last < KEY_WARN_INTERVAL:
            return False
        _KEY_WARNED[key] = now
        print("ferry-front: client keys: %s: %s" % key, file=stream or sys.stderr)
        return True
    except Exception:
        return False


def _master_key() -> str:
    return (os.environ.get("LITELLM_MASTER_KEY") or "").strip()


def presented_credential(headers: dict):
    """(header name, credential) — Authorization: Bearer first, then x-api-key."""
    parts = _header_text(headers, b"authorization").split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer" and parts[1].strip():
        return b"authorization", parts[1].strip()
    api = _header_text(headers, b"x-api-key")
    if api:
        return b"x-api-key", api
    return None, None


def _replace_credential(scope, token: str, master: str) -> None:
    """Swap every credential header carrying `token` for the master (or drop it)."""
    needle = token.encode()
    out = []
    for key, value in scope.get("headers") or []:
        name = bytes(key).lower()
        if name in (b"authorization", b"x-api-key") and needle in bytes(value):
            if master:
                out.append((name, b"Bearer " + master.encode()
                            if name == b"authorization" else master.encode()))
            continue
        out.append((key, value))
    scope["headers"] = out


def authenticate(scope):
    """Admit or refuse this request's credential. None = proceed.

    Sets scope["ferry.key"] to "master", "" (no or foreign credential) or the
    device key's name; a valid device key also gets scope["ferry.key_entry"]
    and has its credential rewritten. A refusal is (status, message)."""
    headers = _header_map(scope)
    _, value = presented_credential(headers)
    scope[KEY_SCOPE] = ""
    if value is None:
        return None
    master = _master_key()
    if master and hmac.compare_digest(value.encode(), master.encode()):
        scope[KEY_SCOPE] = "master"
        return None
    if not value.startswith(KEY_PREFIX):
        return None
    try:
        entry, reason = _key_cache().lookup(value)
    except Exception as err:
        _key_warn(err)
        return 401, ("the ferry key store is unreadable, so device keys are "
                     "refused until it is fixed (see the host's front log)")
    if entry is None:
        return 401, _KEY_REASONS.get(reason, _KEY_REASONS["unknown"])
    scope[KEY_SCOPE] = entry["name"]
    scope[KEY_ENTRY_SCOPE] = entry
    _replace_credential(scope, value, master)
    return None


def is_anthropic_path(path: str) -> bool:
    return any(path == p or path.startswith(p + "/") for p in ANTHROPIC_PATHS)


def key_error_body(path, status, message, code=None, openai_type=None) -> dict:
    """An error body the calling SDK can parse: Anthropic shape on /v1/messages,
    OpenAI shape everywhere else."""
    if is_anthropic_path(path or ""):
        return {"type": "error", "error": {
            "type": _ANTHROPIC_ERRORS.get(status, "api_error"), "message": message}}
    otype, ocode = _OPENAI_ERRORS.get(status, ("server_error", None))
    return {"error": {"message": message, "type": openai_type or otype,
                      "param": None, "code": code or ocode}}
```

3c. In `caller_identity`, replace the first two body lines:

```python
    named = _header_text(headers, CLIENT_HEADER)
    if named:
        return named
```

with:

```python
    # A device key IS the identity: it outranks X-Ferry-Client, so a device
    # cannot read or move another client's sticky fleet by claiming its name.
    if (scope or {}).get(KEY_ENTRY_SCOPE):
        return str(scope.get(KEY_SCOPE) or "")
    named = _header_text(headers, CLIENT_HEADER)
    if named:
        return named
```

Also append this sentence to its docstring: `A valid device key outranks all three: the key's name is the identity.`

3d. At the top of `LaneCatalogueFilter.__call__`, before the comment `# The control plane first:`, insert:

```python
        # Credentials before anything else, control plane included: a revoked
        # device key must not reach /v1/ferry/fleet either.
        if scope.get("type") in ("http", "websocket"):
            refused = authenticate(scope)
            if refused is not None:
                status, message = refused
                if scope.get("type") == "websocket":
                    # Close before accept: the server answers the upgrade 403.
                    return await send({"type": "websocket.close", "code": 1008})
                return await self._reply(send, status, key_error_body(
                    scope.get("path", ""), status, message))
```

3e. Replace `_reply` with a version that takes extra headers, so Task 4 can send `Retry-After`:

```python
    async def _reply(self, send, status, doc, headers=None):
        body = json.dumps(doc).encode()
        await send({
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode())]
                       + list(headers or []),
        })
        await send({"type": "http.response.body", "body": body,
                    "more_body": False})
```

- [ ] **Step 4: Run the new file and the existing front suite**

- `cd /abs/worktree && python3 lib/ferry-front-keys.test.py` — expected `OK`.
- `cd /abs/worktree && python3 lib/ferry-front.test.py` — expected `OK`, with the same test count as before this task. Record the count from a pre-change run in Step 2.
- `cd /abs/worktree && python3 lib/ferry-fleet.test.py` — expected `OK`.

- [ ] **Step 5: Commit.** Write `/tmp/ferry-keys-t3-msg.txt`:

```
feat(front): accept per-device fk- keys and rewrite them to the master

Master and bare requests are untouched; fk- keys are looked up in
keys.json (fail-closed), refused with an SDK-shaped 401, and a key's name
becomes the caller identity for sticky fleets.

Claude-Session-Id: b358f2c8-b360-463c-a431-5b7dd8808316
```

Run: `cd /abs/worktree && git add front/ferry_front.py lib/ferry-front-keys.test.py && git commit -F /tmp/ferry-keys-t3-msg.txt`

---

### Task 4: Lane limits, RPM and budgets, token accounting — `front/ferry_front.py`

**Files:**
- Modify: `front/ferry_front.py`. Resolve each site by its symbol:
  - after the Task 3 `key_error_body`;
  - `LaneCatalogueFilter._inference`, `LaneCatalogueFilter._fleet_rewrite`, `LaneCatalogueFilter._tapped`;
  - a new method, `LaneCatalogueFilter._key_admit`.
- Test: append to `lib/ferry-front-keys.test.py`, which Task 3 created.

**Interfaces:**
- Consumes:
  - Task 1: `ferry_keys.lane_allowed(lanes, requested, resolved)`.
  - Task 2: `ferry_keys_usage.usage_path()`, `Usage(path)`, `.admit(name, rpm, budget_tokens) -> Verdict(ok, reason, retry_after)`, `.add_tokens(name, tokens)`, `.month_tokens(name)`, `.minute_requests(name)`, `UsageError`.
  - Task 3: `KEY_SCOPE`, `KEY_ENTRY_SCOPE`, `_front_sibling`, `_keys_module`, `_key_warn`, `key_error_body(..., code=, openai_type=)`, `_reply(..., headers=)`.
- Produces:
  - `KEY_ADMITTED_SCOPE = "ferry.key_admitted"`
  - `_usage_module()`, `_usage() -> Usage` (one per DB path per process)
  - `_account_tokens(scope, observed: dict) -> None`, which never raises
  - `_tapped(scope, send, strip=False, collector=None, emit=True)`
  - Every tap record gains `rec["key"]`, set to the key name, `"master"` or `""`. Task 6 adds the field to the contract, and Task 9 displays it.

**Rules pinned here (spec corrections 1 and 2):**
- Token accounting must NOT depend on `FERRY_EVENTS`. A metered request, meaning one that carries a valid device key, always gets the passive `RequestMetrics` collector. Only the event *record* stays gated on `tap_enabled()`.
- Tokens charged = `input_tokens + output_tokens`. `reasoning_tokens` is a subset of output (`lib/ferry_metrics.py` module docstring) and is never added again.
- Admission runs after fleet resolution, because a lane limit has to see both the requested name and the resolved `<fleet>.<lane>`. It runs for every device-key inference request, even when `self.state is None` or the body does not parse. An unparseable body on a lane-restricted key is 403: fail closed.
- A refused request (403/429/503) is never charged. Only `scope["ferry.key_admitted"]` requests are.
- Master and bare requests never touch the usage DB, and they keep the caller's original `send` by identity when the tap and strip are off.
- The SQLite write in `finish()` is synchronous on the event loop: one small UPSERT per completed metered request. That is accepted, and noted in the release notes.

- [ ] **Step 1: Append the failing tests** to `lib/ferry-front-keys.test.py`, above the `if __name__ == "__main__":` line:

```python
import ferry_keys_usage as U  # noqa: E402


class TestLimits(KeyFrontCase):
    def usage(self):
        return U.Usage(self.db)

    def chat(self, token, model="flash", mw=None, **extra):
        body = json.dumps(dict({"model": model, "messages": []}, **extra)).encode()
        return drive(mw or self.mw(), "/v1/chat/completions", bearer(token), body=body)

    def test_lane_restricted_key_gets_403_for_another_lane(self):
        token = self.mint(lanes=["flash"])
        status, _, doc = reply(self.chat(token, "heavy")[1])
        self.assertEqual(status, 403)
        self.assertEqual(doc["error"]["code"], "model_not_allowed")
        self.assertEqual(self.app.calls, 0)
        self.assertEqual(reply(self.chat(token, "flash")[1])[0], 200)

    def test_fleet_qualified_lane_limit_sees_the_resolved_lane(self):
        state_path = os.path.join(self.dir, "fleets.json")
        with open(state_path, "w") as fh:
            json.dump({"default": "domestic", "clients": {}}, fh)
        mw = self.mw(FF.FleetState(state_path, FLEETS), FLEETS)
        token = self.mint(lanes=["international.flash"])
        self.assertEqual(reply(self.chat(token, "flash", mw=mw)[1])[0], 403)
        self.assertEqual(reply(self.chat(token, "international.flash", mw=mw)[1])[0], 200)

    def test_unparseable_body_on_a_lane_restricted_key_is_403(self):
        token = self.mint(lanes=["flash"])
        _, sent, _ = drive(self.mw(), "/v1/chat/completions", bearer(token), body=b"{not json")
        self.assertEqual(reply(sent)[0], 403)
        self.assertEqual(self.app.calls, 0)

    def test_rpm_limit_is_429_with_retry_after_in_both_shapes(self):
        token = self.mint(rpm=1)
        self.assertEqual(reply(self.chat(token)[1])[0], 200)
        status, headers, doc = reply(self.chat(token)[1])
        self.assertEqual(status, 429)
        self.assertTrue(1 <= int(headers[b"retry-after"]) <= 60)
        self.assertEqual(doc["error"]["code"], "rate_limit_exceeded")
        _, sent, _ = drive(self.mw(), "/v1/messages", [("x-api-key", token)],
                           body=b'{"model":"flash","messages":[]}')
        status, headers, doc = reply(sent)
        self.assertEqual((status, doc["error"]["type"]), (429, "rate_limit_error"))
        self.assertIn(b"retry-after", headers)
        self.assertEqual(self.app.calls, 1)

    def test_budget_is_429_insufficient_quota(self):
        token = self.mint(budget_tokens=10)
        self.assertEqual(reply(self.chat(token)[1])[0], 200)  # charges 12
        status, headers, doc = reply(self.chat(token)[1])
        self.assertEqual(status, 429)
        self.assertEqual(doc["error"]["code"], "insufficient_quota")
        self.assertEqual(doc["error"]["type"], "insufficient_quota")
        self.assertGreater(int(headers[b"retry-after"]), 0)

    def test_accounting_works_with_the_tap_off(self):
        self.assertEqual(os.environ["FERRY_EVENTS"], "0")
        token = self.mint()
        self.chat(token)
        self.assertEqual(self.usage().month_tokens("laptop"), 12)  # 7 in + 5 out
        self.assertEqual(self.usage().minute_requests("laptop"), 1)

    def test_reasoning_tokens_are_not_double_counted(self):
        self.app.payload = json.dumps({"usage": {
            "prompt_tokens": 7, "completion_tokens": 5,
            "completion_tokens_details": {"reasoning_tokens": 4}}}).encode()
        self.chat(self.mint())
        self.assertEqual(self.usage().month_tokens("laptop"), 12)

    def test_a_refused_request_is_not_counted(self):
        token = self.mint(rpm=1)
        self.chat(token)
        self.chat(token)
        self.assertEqual(self.usage().month_tokens("laptop"), 12)
        self.assertEqual(self.usage().minute_requests("laptop"), 1)

    def test_include_usage_is_injected_for_device_keys_only(self):
        self.chat(self.mint(), stream=True)
        self.assertEqual(json.loads(self.app.body)["stream_options"], {"include_usage": True})
        body = json.dumps({"model": "flash", "messages": [], "stream": True}).encode()
        drive(self.mw(), "/v1/chat/completions", bearer(MASTER), body=body)
        self.assertEqual(self.app.body, body)

    def test_master_request_still_gets_the_original_send(self):
        _, _, send = drive(self.mw(), "/v1/chat/completions", bearer(MASTER))
        self.assertIs(self.app.send, send)
        # CONTROL: a device key does get the metering wrapper.
        _, _, send = self.chat(self.mint())
        self.assertIsNot(self.app.send, send)

    def test_unusable_usage_db_is_503_for_keys_and_invisible_to_the_master(self):
        token = self.mint()
        with mock.patch.dict(os.environ, {"FERRY_KEYS_DB": self.dir}), \
                mock.patch("sys.stderr"):
            status, _, doc = reply(self.chat(token)[1])
            self.assertEqual(status, 503)
            self.assertEqual(doc["error"]["code"], "key_store_unavailable")
            _, sent, _ = drive(self.mw(), "/v1/chat/completions", bearer(MASTER))
            self.assertEqual(reply(sent)[0], 200)

    def test_accounting_failure_never_breaks_the_response(self):
        token = self.mint()
        with mock.patch.object(U.Usage, "add_tokens",
                               side_effect=U.UsageError("disk full")), \
                mock.patch("sys.stderr") as err:
            _, sent, _ = self.chat(token)
        status, _, _ = reply(sent)
        self.assertEqual(status, 200)
        body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
        self.assertEqual(body, USAGE_BODY)
        self.assertTrue(any("disk full" in str(c.args[0]) for c in err.write.call_args_list if c.args))


class TestEventKeyField(KeyFrontCase):
    def setUp(self):
        super().setUp()
        self.events = os.path.join(self.dir, "events.jsonl")
        os.environ["FERRY_EVENTS"] = "1"   # restored by the patch.dict in the base
        FF.reset_tap(self.events)
        self.addCleanup(FF.reset_tap, None)

    def test_record_names_the_key(self):
        token = self.mint()
        for headers in (bearer(token), bearer(MASTER), ()):
            drive(self.mw(), "/v1/chat/completions", headers)
        FF.tap_flush()
        with open(self.events) as fh:
            keys = [json.loads(line)["key"] for line in fh if line.strip()]
        self.assertEqual(keys, ["laptop", "master", ""])
```

- [ ] **Step 2: Control run on the Task 3 tree**

Run: `cd /abs/worktree && python3 lib/ferry-front-keys.test.py`

Expected: the Task 3 tests pass, and every new test fails:
- `test_lane_restricted_key_gets_403_for_another_lane`: `200 != 403`.
- `test_accounting_works_with_the_tap_off`: `0 != 12`.
- `test_record_names_the_key`: `KeyError: 'key'`.
- `test_master_request_still_gets_the_original_send`: this one PASSES on the old code. It is a regression guard, and its second assertion is the control.

- [ ] **Step 3: Implement**

4a. After `key_error_body`, add:

```python
KEY_ADMITTED_SCOPE = "ferry.key_admitted"
_USAGES: dict = {}


def _usage_module():
    return _front_sibling("ferry_keys_usage")


def _usage():
    """One Usage per DB path per process; the path is read per call."""
    mod = _usage_module()
    path = mod.usage_path()
    usage = _USAGES.get(path)
    if usage is None:
        usage = _USAGES[path] = mod.Usage(path)
    return usage


def _account_tokens(scope, observed) -> None:
    """Charge an admitted device-key request its tokens. Never raises.

    input + output only: reasoning tokens are already inside output
    (lib/ferry_metrics.py). A refused request was never admitted, so its
    error body costs nothing."""
    try:
        if not scope.get(KEY_ADMITTED_SCOPE):
            return
        tokens = 0
        for field in ("input_tokens", "output_tokens"):
            value = (observed or {}).get(field)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
                tokens += int(value)
        if tokens:
            _usage().add_tokens(scope[KEY_SCOPE], tokens)
    except Exception as err:
        _key_warn(err)
```

4b. In `_inference`, replace:

```python
        collector = module = token = None
        tapped = None
        if tap_enabled():
```

with:

```python
        collector = module = token = None
        tapped = None
        emit = tap_enabled()
        # A device-key request is metered whether or not the event tap is on:
        # its token budget must not depend on FERRY_EVENTS.
        metered = scope.get(KEY_ENTRY_SCOPE) is not None
        if emit or metered:
```

In the same block, replace `tapped = self._tapped(scope, send, strip, collector)` with `tapped = self._tapped(scope, send, strip, collector, emit=emit)`.

4c. In `_fleet_rewrite`:
- Directly after the comply `try/except` block, and before `if isinstance(doc, dict) and tap_enabled():`, add:

```python
        requested = doc.get("model") if isinstance(doc, dict) else None
```

- Then replace the line:

```python
        if (tap_enabled() and isinstance(doc, dict) and doc.get("stream") is True
```

with:

```python
        entry = scope.get(KEY_ENTRY_SCOPE)
        if entry is not None:
            resolved = doc.get("model") if isinstance(doc, dict) else None
            if not await self._key_admit(scope, send, entry, requested, resolved):
                return None
        if ((tap_enabled() or entry is not None) and isinstance(doc, dict)
                and doc.get("stream") is True
```

The continuation line `and scope.get("path") in ("/v1/chat/completions", "/chat/completions")):` stays as it is.

4d. Add the method `_key_admit` directly after `_fleet_rewrite`:

```python
    async def _key_admit(self, scope, send, entry, requested, resolved) -> bool:
        """Lane, RPM and budget gate for a device key. False = already replied."""
        path = scope.get("path", "")
        name = scope.get(KEY_SCOPE, "")
        lanes = entry.get("lanes")
        if not _keys_module().lane_allowed(lanes, requested, resolved):
            await self._reply(send, 403, key_error_body(path, 403,
                "ferry device key %r may not use model %r (allowed lanes: %s)"
                % (name, requested, ", ".join(lanes or []))))
            return False
        try:
            verdict = _usage().admit(name, entry.get("rpm"), entry.get("budget_tokens"))
        except Exception as err:
            _key_warn(err)
            await self._reply(send, 503, key_error_body(path, 503,
                "the ferry key usage store is unavailable, so device keys are "
                "refused until it is fixed (see the host's front log)"))
            return False
        if not verdict.ok:
            retry = [(b"retry-after", str(int(verdict.retry_after)).encode())]
            if verdict.reason == "budget":
                doc = key_error_body(path, 429,
                    "ferry device key %r has used its monthly budget of %s tokens"
                    % (name, entry.get("budget_tokens")),
                    code="insufficient_quota", openai_type="insufficient_quota")
            else:
                doc = key_error_body(path, 429,
                    "ferry device key %r is over its limit of %s requests per minute"
                    % (name, entry.get("rpm")))
            await self._reply(send, 429, doc, retry)
            return False
        scope[KEY_ADMITTED_SCOPE] = True
        return True
```

4e. In `_tapped`:
- Change the signature to `def _tapped(self, scope, send, strip=False, collector=None, emit=True):`.
- Inside `finish`, replace the block from `            try:\n                tap = _tap()\n                if tap is None:\n                    return` onward with:

```python
            _account_tokens(scope, observed)
            if not emit:
                return
            try:
                tap = _tap()
                if tap is None:
                    return
                if rec is None:
                    client = scope.get("client") or ("", 0)
                    rec = tap.record_from_headers([], client[0], scope.get("path", ""), 0)
                rec["schema_warnings"] = scope.get(SCHEMA_WARNINGS_KEY, [])
                rec["resp_bytes"] = nbytes
                rec["response_complete"] = bool(complete)
                rec.update(observed)
                rec["key"] = scope.get(KEY_SCOPE, "")
                tap.offer(rec)
            except Exception:
                pass
```

- In `tapped`, guard the `http.response.start` record build:

```python
            if mtype == "http.response.start":
                if emit:
                    try:
                        tap = _tap()
                        if tap is not None:
                            client = scope.get("client") or ("", 0)
                            rec = tap.record_from_headers(message.get("headers") or [],
                                client[0], scope.get("path", ""), message.get("status", 0))
                    except Exception:
                        pass
```

The collector and strip lines after it are unchanged.

- [ ] **Step 4: Run**
- `cd /abs/worktree && python3 lib/ferry-front-keys.test.py` — expected `OK`.
- `cd /abs/worktree && python3 lib/ferry-front.test.py` — expected `OK`, with the count unchanged. In particular, the tap byte-identity tests still pass, because `emit=True` is the default.
- `cd /abs/worktree && python3 lib/ferry-events.test.py` — expected `OK`.

- [ ] **Step 5: Commit.** Write `/tmp/ferry-keys-t4-msg.txt`:

```
feat(front): lane limits, RPM and token budgets for device keys

Admission runs after fleet resolution; refusals are SDK-shaped 403/429
(Retry-After) or 503 when the usage DB is unusable. Tokens (input +
output) are charged whether or not FERRY_EVENTS is on, and every event
record names its key.

Claude-Session-Id: b358f2c8-b360-463c-a431-5b7dd8808316
```

Run: `cd /abs/worktree && git add front/ferry_front.py lib/ferry-front-keys.test.py && git commit -F /tmp/ferry-keys-t4-msg.txt`

---

### Task 5: Enroll endpoint — `POST /v1/ferry/keys/enroll`

**Files:**
- Modify: `front/ferry_front.py`, in two places:
  - add a constant beside `FLEET_PATH`;
  - add a route inside `LaneCatalogueFilter.__call__`, directly after the Task 3 auth block and before `path = scope.get("path", "")…`. Add the method `_enroll`.
- Test: `lib/ferry-front-enroll.test.py` (new). It is a separate file so that this seat can run in parallel with Task 4.

**Interfaces:**
- Consumes: Task 1's `ferry_keys.add(name, unique=, replace=) -> (name, token)` and `KeyNameError` (a `ValueError`). From Task 3: `KEY_SCOPE`, `_keys_module`, `_key_warn`, and `_reply`.
- Produces the wire contract that Task 7's bootstrap relies on:
  - Request: `POST /v1/ferry/keys/enroll` with `Authorization: Bearer <master>` and the body `{"name": str, "replace": bool?}`.
  - `200 {"name": <final name>, "key": "fk-…"}`.
  - `400 {"error": {...}}` for a bad body or name.
  - `401` when the caller is not the master (this includes a device key).
  - `403` when no master is configured and the caller is not on loopback.
  - `405` for anything other than POST.
  - `503` when the store is unwritable.
  - `replace: true` rotates the key of that name in place. Without it, a taken name gets `-2`, `-3`, and so on (`unique=True`). The two modes never combine (spec correction 4).

**Why a master check here, not `_bearer_ok`:** Task 3 has already rewritten a device key's credential to the master, so `_bearer_ok` would pass a device key. That is spec correction 3. The gate is `scope["ferry.key"] == "master"`, which `authenticate` sets only for the real master.

- [ ] **Step 1: Write the failing test**

Create `lib/ferry-front-enroll.test.py`:

```python
#!/usr/bin/env python3
"""POST /v1/ferry/keys/enroll — how client-bootstrap.sh trades the master key
for a device key.

Run:  python3 lib/ferry-front-enroll.test.py

Drives LaneCatalogueFilter directly; FERRY_KEYS_FILE points at a temp dir.
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "front"))

import ferry_front as FF  # noqa: E402
import ferry_keys as K  # noqa: E402

MASTER = "sk-test-master"
PATH = "/v1/ferry/keys/enroll"


class NeverApp:
    calls = 0

    async def __call__(self, scope, receive, send):
        NeverApp.calls += 1
        raise AssertionError("enroll must never reach litellm")


def post(body, headers=(), method="POST", client=("100.64.0.9", 50000)):
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    scope = {"type": "http", "path": PATH, "method": method, "client": client,
             "headers": [(k.encode(), v.encode()) for k, v in headers]}
    sent = []

    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(FF.LaneCatalogueFilter(NeverApp(), frozenset())(scope, receive, send))
    start = next(m for m in sent if m["type"] == "http.response.start")
    payload = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return start["status"], json.loads(payload)


def master():
    return [("authorization", "Bearer " + MASTER)]


class EnrollCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ferry-front-enroll-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.keys = os.path.join(self.dir, "keys.json")
        patcher = mock.patch.dict(os.environ, {
            "FERRY_KEYS_FILE": self.keys,
            "FERRY_KEYS_DB": os.path.join(self.dir, "u.sqlite"),
            "LITELLM_MASTER_KEY": MASTER, "FERRY_EVENTS": "0"})
        patcher.start()
        self.addCleanup(patcher.stop)
        FF._KEY_CACHE = None
        NeverApp.calls = 0

    def test_master_mints_a_key_that_then_authenticates(self):
        status, doc = post({"name": "MBP Work"}, master())
        self.assertEqual(status, 200)
        self.assertEqual(doc["name"], "mbp-work")
        self.assertRegex(doc["key"], r"^fk-mbp-work-[a-z2-7]{32}$")
        self.assertEqual(K.KeyCache().lookup(doc["key"])[0]["name"], "mbp-work")
        self.assertEqual(NeverApp.calls, 0)

    def test_taken_name_gets_a_suffix_without_replace(self):
        post({"name": "laptop"}, master())
        status, doc = post({"name": "laptop"}, master())
        self.assertEqual((status, doc["name"]), (200, "laptop-2"))

    def test_replace_rotates_in_place(self):
        _, first = post({"name": "laptop"}, master())
        status, second = post({"name": "laptop", "replace": True}, master())
        self.assertEqual((status, second["name"]), (200, "laptop"))
        self.assertEqual(len(K.load()["keys"]), 1)
        self.assertEqual(K.KeyCache().lookup(first["key"])[1], "unknown")

    def test_replace_must_be_a_real_boolean(self):
        post({"name": "laptop"}, master())
        _, doc = post({"name": "laptop", "replace": "yes"}, master())
        self.assertEqual(doc["name"], "laptop-2")

    def test_a_device_key_cannot_enroll(self):
        _, token = K.add("laptop")
        status, _ = post({"name": "evil"}, [("authorization", "Bearer " + token)])
        self.assertEqual(status, 401)
        self.assertEqual([e["name"] for e in K.load()["keys"]], ["laptop"])

    def test_no_or_wrong_credential_is_401(self):
        self.assertEqual(post({"name": "x"})[0], 401)
        self.assertEqual(post({"name": "x"}, [("authorization", "Bearer nope")])[0], 401)
        self.assertFalse(os.path.exists(self.keys))

    def test_without_a_master_only_loopback_may_enroll(self):
        with mock.patch.dict(os.environ, {"LITELLM_MASTER_KEY": ""}):
            self.assertEqual(post({"name": "x"})[0], 403)
            status, doc = post({"name": "x"}, client=("127.0.0.1", 1))
            self.assertEqual((status, doc["name"]), (200, "x"))

    def test_bad_bodies_are_400(self):
        for body in (b"{nope", b"[]", {"name": 5}, {"name": "!!!"}, {}):
            with self.subTest(body=body):
                self.assertEqual(post(body, master())[0], 400)

    def test_get_is_405(self):
        self.assertEqual(post(b"", master(), method="GET")[0], 405)

    def test_unwritable_store_is_503(self):
        with open(self.keys, "w") as fh:
            fh.write("{corrupt")
        with mock.patch("sys.stderr"):
            status, _ = post({"name": "x"}, master())
        self.assertEqual(status, 503)


if __name__ == "__main__":
    unittest.main(verbosity=2)
```

- [ ] **Step 2: Control run on the Task 3 tree.** Run `cd /abs/worktree && python3 lib/ferry-front-enroll.test.py`.

  Expected: most tests fail with `AssertionError: enroll must never reach litellm`, because the route does not exist and the request falls through to the app. The exceptions are `test_a_device_key_cannot_enroll` and `test_no_or_wrong_credential_is_401`. They fail on `StopIteration`, or reach the app, since nothing answers 401 yet.

- [ ] **Step 3: Implement**

5a. Beside `FLEET_PATH = "/v1/ferry/fleet"`, add:

```python
KEYS_ENROLL_PATH = "/v1/ferry/keys/enroll"
```

5b. In `__call__`, directly after the Task 3 auth block, and before `path = scope.get("path", "") if scope.get("type") == "http" else ""`, add:

```python
        if scope.get("type") == "http" and scope.get("path") == KEYS_ENROLL_PATH:
            return await self._enroll(scope, receive, send)
```

5c. Add this method after `_key_admit` (or after `_fleet_rewrite` if Task 4 has not landed yet):

```python
    async def _enroll(self, scope, receive, send):
        """Mint a device key for client-bootstrap.sh. Master key only.

        `_bearer_ok` is deliberately NOT the gate: authenticate() has already
        rewritten a device key's credential to the master, so only
        scope["ferry.key"] tells the real master apart."""
        if scope.get("method", "GET").upper() != "POST":
            return await self._reply(send, 405, {"error": {
                "message": "use POST on %s" % KEYS_ENROLL_PATH, "type": "ferry_keys"}})
        if _master_key():
            if scope.get(KEY_SCOPE) != "master":
                return await self._reply(send, 401, {"error": {
                    "message": "enrolling a device key needs the master key",
                    "type": "ferry_keys"}})
        elif not _is_loopback_client(scope) or scope.get(KEY_SCOPE):
            return await self._reply(send, 403, {"error": {
                "message": "no master key is configured; enroll from the host itself",
                "type": "ferry_keys"}})
        raw = await self._read_body(receive, send)
        if raw is None:
            return
        try:
            doc = json.loads(raw)
        except Exception:
            doc = None
        if not isinstance(doc, dict) or not isinstance(doc.get("name"), str):
            return await self._reply(send, 400, {"error": {
                "message": 'body needs {"name": "<device>"}', "type": "ferry_keys"}})
        replace = doc.get("replace") is True
        try:
            name, token = _keys_module().add(doc["name"], unique=not replace,
                                             replace=replace)
        except ValueError as err:
            return await self._reply(send, 400, {"error": {
                "message": str(err), "type": "ferry_keys"}})
        except Exception as err:
            _key_warn(err)
            return await self._reply(send, 503, {"error": {
                "message": "the ferry key store is unwritable: %s" % err,
                "type": "ferry_keys"}})
        return await self._reply(send, 200, {"name": name, "key": token})
```

- [ ] **Step 4: Run** these and expect `OK` from each:
  - `cd /abs/worktree && python3 lib/ferry-front-enroll.test.py`
  - `python3 lib/ferry-front-keys.test.py`
  - `python3 lib/ferry-front.test.py`

- [ ] **Step 5: Commit.** Write `/tmp/ferry-keys-t5-msg.txt`:

```
feat(front): POST /v1/ferry/keys/enroll mints device keys for bootstrap

Master-only (checked on scope["ferry.key"], since a device key has
already been rewritten to the master by then); replace rotates in place,
otherwise a taken name gets a numeric suffix.

Claude-Session-Id: b358f2c8-b360-463c-a431-5b7dd8808316
```

Run: `cd /abs/worktree && git add front/ferry_front.py lib/ferry-front-enroll.test.py && git commit -F /tmp/ferry-keys-t5-msg.txt`

**Merge note (T4 ∥ T5):** both tasks edit `front/ferry_front.py` in disjoint regions, so the merge is textual only. If `_key_admit` and `_enroll` collide at the same insertion point, keep both methods.

---

### Task 6: Event record `key` field — `lib/ferry_events.py`

**Files:**
- Modify: `lib/ferry_events.py`, the `_EMPTY` dict (`:54 (_EMPTY)`, valid at `43acdda`).
- Modify: `lib/ferry-events.test.py`, `test_key_set_is_exactly_the_contract`.
- Modify: `observ/CONTRACT.md`, the table under "## Request timing and token fields".

**Interfaces:**
- Produces: every record from `record_from_headers` has `"key": ""`. The front door (Task 4) overwrites it with the key name, `"master"`, or `""`. The dash (Task 9) reads `e.key`.
- The field holds a key's NAME only, never a token or a hash.

- [ ] **Step 1: Write the failing test.**

  In `lib/ferry-events.test.py`, change the set literal in `test_key_set_is_exactly_the_contract` so that its last line reads:

```python
            "reasoning_tokens", "cached_input_tokens", "response_complete", "key"})
```

  Then add this test directly after `test_key_set_is_exactly_the_contract`:

```python
    def test_key_defaults_to_empty_until_the_front_door_names_it(self):
        # The front door writes the device key's NAME (or "master"); the
        # record builder never sees a credential, so it can only say "".
        r = E.record_from_headers([(b"authorization", b"Bearer fk-x-" + b"a" * 32)],
                                  "", "/v1/chat/completions", 200)
        self.assertEqual(r["key"], "")
        self.assertNotIn("fk-x-", json.dumps(r))
```

  If `json` is not already imported at the top of the file, add `import json`.

- [ ] **Step 2: Control.**

  Run: `cd /abs/worktree && python3 lib/ferry-events.test.py`

  Expected: both tests FAIL. The contract test fails with the set-difference message (`Items in the second set but not the first: 'key'`). The new test fails with `KeyError: 'key'`.

- [ ] **Step 3: Implement.**

  In `_EMPTY`, change the last line from:

```python
    "resp_bytes": 0, "client_ip": "", "path": "", "schema_warnings": [],
```

  to:

```python
    "resp_bytes": 0, "client_ip": "", "path": "", "schema_warnings": [],
    "key": "",
```

  In `observ/CONTRACT.md`, append this row to the field table (after `cached_input_tokens`):

```markdown
| `key` | Which credential made the call: a device key's name, `master`, or empty (no or foreign credential). Never the key itself. Absent on records before v1.39.0. |
```

- [ ] **Step 4: Run.**

  Run each of these and expect `OK` from all three:
  - `cd /abs/worktree && python3 lib/ferry-events.test.py`
  - `python3 lib/ferry-front.test.py`
  - `node lib/ferry-dashui.test.mjs`

- [ ] **Step 5: Commit.**

  Write `/tmp/ferry-keys-t6-msg.txt`:

```
feat(events): records carry the calling key's name

Claude-Session-Id: b358f2c8-b360-463c-a431-5b7dd8808316
```

  Then run: `cd /abs/worktree && git add lib/ferry_events.py lib/ferry-events.test.py observ/CONTRACT.md && git commit -F /tmp/ferry-keys-t6-msg.txt`

---

### Task 7: Bootstrap enrollment and every `master_key` reader

**Files:** (line numbers valid at `43acdda`; resolve by the quoted text)
- Modify `client-bootstrap.sh`:
  - the header comment block (`# THE MASTER KEY (v1.22.0)`);
  - the insertion point right before the `# Write local client JSON config profile` anchor;
  - the `MASTER_KEY_JSON` block.
- Modify `lib/ferry-core.zsh`: the `CLIENT_MASTER_KEY=$(python3 -c …get('master_key')…)` line (`:294`).
- Modify `lib/ferry-claude.zsh`:
  - the `cl_key=$(python3 -c …get('master_key')…)` read (`:242`);
  - the mirror write `cfg["master_key"] = key` (`:284`).
- Modify `client-reset.sh`:
  - the tab-print line (`:99`);
  - the `echo "Auth: using the master_key …"` line (`:165`).
- Modify `client-to-host.sh`: add a comment only, above `CLIENT_KEY="$(python3 …get('master_key')…)"`.
- Rebuild the monolith with `ferry` (`zsh ./build.zsh`). The tests below run the built `ferry`.
- Tests: `lib/ferry-clientbootstrap.test.py`, `lib/ferry-fleet.test.py`, `lib/ferry-claude.test.py`.

**Interfaces:**
- Consumes: the Task 5 wire contract (enroll). The shell side is independent of the Python code, so this task runs in wave 1 against stub hosts.
- Produces the `client.json` contract for v1.39.0:
  - an enrolled client has `"api_key": "fk-…"` and `"key_name": "<name>"`, and NO `master_key`;
  - an old host, or a failed enroll, keeps `"master_key"` exactly as before;
  - a keyless host has none of the three keys.
- Every reader prefers `api_key` and falls back to `master_key`.
- `client-to-host.sh` deliberately reads `master_key` only. A device key must never become a host's `LITELLM_MASTER_KEY`.
- The claude mirror (`~/.config/ferry/claude.json`) records `api_key` for an `fk-` key and `master_key` otherwise.

- [ ] **Step 1: Write the failing tests**

1a. In `lib/ferry-clientbootstrap.test.py`, add a class after `MasterKeyTest`:

```python
class EnrollingStubHost(KeyedStubHost):
    """A v1.39.0 host: POST /v1/ferry/keys/enroll mints a device key for the
    master, and /v1/models accepts either credential afterwards."""

    DEVICE_KEY = "fk-laptop-" + "a" * 32
    ENROLLS = []

    def do_GET(self):  # noqa: N802
        if (self.path.startswith("/v1/models")
                and self.headers.get("Authorization", "") == f"Bearer {self.DEVICE_KEY}"):
            return StubHost.do_GET(self)
        KeyedStubHost.do_GET(self)

    def do_POST(self):  # noqa: N802
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if (self.path != "/v1/ferry/keys/enroll"
                or self.headers.get("Authorization", "") != f"Bearer {self.REQUIRED_KEY}"):
            self.send_response(401)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        doc = json.loads(raw)
        EnrollingStubHost.ENROLLS.append(doc)
        body = json.dumps({"name": doc["name"], "key": self.DEVICE_KEY}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class DeviceKeyTest(ClientHarness):
    """v1.39.0 — a bootstrap given the master key trades it for a per-device
    key and stores only that; an older host keeps the master key path."""

    @classmethod
    def setUpClass(cls):
        ClientHarness.setUpClass()
        cls.enrolling = ThreadingHTTPServer(("127.0.0.1", 0), EnrollingStubHost)
        cls.enrolling_port = cls.enrolling.server_address[1]
        threading.Thread(target=cls.enrolling.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.enrolling.shutdown()
        cls.enrolling.server_close()
        ClientHarness.tearDownClass()

    def setUp(self):
        super().setUp()
        EnrollingStubHost.ENROLLS.clear()

    def keyed_env(self):
        return self.env(master_key=KeyedStubHost.REQUIRED_KEY, port=self.enrolling_port)

    def expected_name(self):
        p = subprocess.run(["hostname", "-s"], capture_output=True, text=True)
        return p.stdout.strip().lower()

    def test_bootstrap_trades_the_master_for_a_device_key(self):
        p = self.run_script(BOOTSTRAP, "--no-opencode", env=self.keyed_env())
        prof = self.read_json(".config", "ferry", "client.json")
        self.assertEqual(prof["api_key"], EnrollingStubHost.DEVICE_KEY)
        self.assertEqual(prof["key_name"], self.expected_name())
        self.assertNotIn("master_key", prof)
        self.assertEqual(EnrollingStubHost.ENROLLS, [{"name": self.expected_name()}])
        for secret in (KeyedStubHost.REQUIRED_KEY, EnrollingStubHost.DEVICE_KEY):
            self.assertNotIn(secret, p.stdout + p.stderr)
        self.assertIn("device key", p.stdout)

    def test_a_rerun_rotates_the_same_name(self):
        self.run_script(BOOTSTRAP, "--no-opencode", env=self.keyed_env())
        self.run_script(BOOTSTRAP, "--no-opencode", env=self.keyed_env())
        self.assertEqual(EnrollingStubHost.ENROLLS[-1],
                         {"name": self.expected_name(), "replace": True})

    def test_the_generated_configs_carry_the_device_key(self):
        self.run_script(BOOTSTRAP, "--profiles-only", env=self.keyed_env())
        with open(self.path(".config", "ferry", "opencode-cloud.json")) as f:
            text = f.read()
        self.assertIn(EnrollingStubHost.DEVICE_KEY, text)
        self.assertNotIn(KeyedStubHost.REQUIRED_KEY, text)

    def test_reset_threads_the_device_key_through(self):
        self.run_script(BOOTSTRAP, "--profiles-only", env=self.keyed_env())
        os.remove(self.path(".config", "ferry", "opencode-cloud.json"))
        out = self.run_script(RESET, env=self.env(port=self.enrolling_port)).stdout
        with open(self.path(".config", "ferry", "opencode-cloud.json")) as f:
            self.assertIn(EnrollingStubHost.DEVICE_KEY, f.read())
        self.assertNotIn(EnrollingStubHost.DEVICE_KEY, out)
```

Add this method to the existing `MasterKeyTest`. `KeyedStubHost` has no `do_POST`, so the enroll gets `501`, which is the v1.38-host fallback:

```python
    def test_an_old_host_without_enroll_keeps_the_master_key(self):
        self.run_script(BOOTSTRAP, "--no-opencode",
                        env=self.env(master_key=KeyedStubHost.REQUIRED_KEY,
                                     port=self.keyed_port))
        prof = self.read_json(".config", "ferry", "client.json")
        self.assertEqual(prof["master_key"], KeyedStubHost.REQUIRED_KEY)
        self.assertNotIn("api_key", prof)
        self.assertNotIn("key_name", prof)
```

In `test_reset_threads_a_stored_master_key_through_to_the_cli`, add one line:

```python
        self.assertIn("c.get('api_key') or c.get('master_key'", reset)
```

In the same class, add a method next to it. `self.read` is the helper that method already uses:

```python
    def test_client_to_host_never_promotes_a_device_key_to_master(self):
        """A device key authenticates one laptop to one host; client-to-host.sh
        must carry only a real master_key into the new host's secrets."""
        line = next(l for l in self.read(os.path.join(REPO, "client-to-host.sh")).splitlines()
                    if l.startswith('CLIENT_KEY="$(python3'))
        self.assertIn("get('master_key')", line)
        self.assertNotIn("api_key", line)
```

This test PASSES before and after the change. It is a regression pin, and its control is running it with `'api_key'` swapped for `'master_key'` in the `assertNotIn`, which must fail.

1b. In `lib/ferry-fleet.test.py`, add this to `TestUse`:

```python
    def test_a_device_key_in_the_profile_is_the_bearer(self):
        cfg = os.path.join(self.home, ".config", "ferry", "client.json")
        with open(cfg) as f:
            prof = json.load(f)
        prof.pop("master_key")
        prof["api_key"] = "fk-laptop-" + "b" * 32
        with open(cfg, "w") as f:
            json.dump(prof, f)
        proc = self.run_fleet("use", "international")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(_FleetHandler.LAST_HEADERS.get("Authorization"),
                         "Bearer fk-laptop-" + "b" * 32)
```

1c. In `lib/ferry-claude.test.py` `MasterKeyTest`, add:

```python
    DEVICE_KEY = "fk-mbp-" + "c" * 32

    def test_a_device_key_is_baked_and_mirrored_as_api_key(self):
        fdir = os.path.join(self.home, ".config", "ferry")
        os.makedirs(fdir, exist_ok=True)
        with open(os.path.join(fdir, "client.json"), "w") as f:
            json.dump({"host": INSTALL_HOST, "port": INSTALL_PORT,
                       "api_key": self.DEVICE_KEY, "key_name": "mbp"}, f)
        self.assertEqual(self.run_install().returncode, 0)
        self.assertEqual(self.rc_text().count(f"ANTHROPIC_AUTH_TOKEN={self.DEVICE_KEY}"), 3)
        mirror = self.claude_json()
        self.assertEqual(mirror["api_key"], self.DEVICE_KEY)
        self.assertNotIn("master_key", mirror)
```

- [ ] **Step 2: Control.**

  Run each of these:
  - `cd /abs/worktree && python3 lib/ferry-clientbootstrap.test.py DeviceKeyTest MasterKeyTest`
  - `python3 lib/ferry-fleet.test.py`
  - `python3 lib/ferry-claude.test.py MasterKeyTest`

  Expected:
  - The `DeviceKeyTest` tests fail with `KeyError: 'api_key'`, or on the `ENROLLS == []` assertion.
  - The fleet test fails with `'Bearer ' != 'Bearer fk-laptop-bbbb…'`.
  - The claude test fails on count `0 != 3`.
  - The new `MasterKeyTest` test PASSES. It is the regression guard for old hosts.
  - The added static assertion FAILS.

- [ ] **Step 3: Implement**

3a. `client-bootstrap.sh`: directly above the `# Write local client JSON config profile` line, insert the block below.

The master key travels to Python in the environment, never on argv, so it cannot be seen in `ps`. The Python heredoc is quoted (`<<'PYEOF'`), so zsh expands nothing inside it.

```zsh
# Per-device keys (v1.39.0). A host from v1.39.0 on mints a device key for the
# master key (POST /v1/ferry/keys/enroll); only that device key is stored, so
# the master never has to live on this laptop and the host can revoke this one
# machine alone. A re-run rotates the key under the same name (the name is kept
# in client.json as key_name). Any failure — an older host answers 404/501 —
# falls back to storing the master key exactly as before. Keys never printed.
DEVICE_KEY=""
DEVICE_KEY_NAME=""
if [[ -n "$MASTER_KEY" ]]; then
  echo ">>> Enrolling this machine for its own device key..."
  enroll_out=$(FERRY_ENROLL_MASTER="$MASTER_KEY" python3 - \
      "http://$HOST_NAME:$HOST_PORT" "$CLIENT_NAME" "$HOME/.config/ferry/client.json" \
      2>/dev/null <<'PYEOF'
import json, os, sys, urllib.request
base, name, profile = sys.argv[1], sys.argv[2], sys.argv[3]
replace = False
try:
    prior = json.load(open(profile))
    if isinstance(prior.get("key_name"), str) and prior["key_name"]:
        name, replace = prior["key_name"], True
except Exception:
    pass
body = {"name": name or "device"}
if replace:
    body["replace"] = True
req = urllib.request.Request(
    base + "/v1/ferry/keys/enroll", data=json.dumps(body).encode(), method="POST",
    headers={"Authorization": "Bearer " + os.environ["FERRY_ENROLL_MASTER"],
             "Content-Type": "application/json"})
try:
    with urllib.request.urlopen(req, timeout=10) as resp:
        doc = json.load(resp)
except Exception:
    sys.exit(0)
key, got = doc.get("key"), doc.get("name")
safe = set("abcdefghijklmnopqrstuvwxyz0123456789-")
if (isinstance(key, str) and key.startswith("fk-") and set(key) <= safe
        and isinstance(got, str) and got and set(got) <= safe):
    print(got + "\t" + key)
PYEOF
  ) || enroll_out=""
  if [[ "$enroll_out" == *$'\t'fk-* ]]; then
    DEVICE_KEY_NAME="${enroll_out%%$'\t'*}"
    DEVICE_KEY="${enroll_out#*$'\t'}"
    echo "    Enrolled device key '$DEVICE_KEY_NAME' (the master key is NOT stored on this machine)."
    APIKEY_HINT="your ferry device key (stored as api_key in ~/.config/ferry/client.json)"
    BEARER_HINT="your ferry device key"
  else
    echo "    This host does not issue device keys (older than v1.39.0) — storing the master key."
  fi
fi

```

3b. Replace the `MASTER_KEY_JSON` block. Keep the comment above it, and add one sentence to that comment: `api_key/key_name replace master_key when the host issued a device key.`

```zsh
MASTER_KEY_JSON=""
if [[ -n "$DEVICE_KEY" ]]; then
  MASTER_KEY_JSON=$(printf ',\n  "api_key": "%s",\n  "key_name": "%s"' "$DEVICE_KEY" "$DEVICE_KEY_NAME")
elif [[ -n "$MASTER_KEY" ]]; then
  MASTER_KEY_JSON=$(printf ',\n  "master_key": "%s"' "$MASTER_KEY")
fi
```

3c. In the header comment `# THE MASTER KEY (v1.22.0): …`, append:

```zsh
# From v1.39.0 a host that issues per-device keys trades the master key for one
# at bootstrap: client.json then holds "api_key" + "key_name" and NO master_key.
# Only re-running this script migrates an existing client; `ferry update` never
# touches credentials.
```

3d. `lib/ferry-core.zsh`: replace the `CLIENT_MASTER_KEY=$(python3 -c …)` line with:

```zsh
  # v1.39.0: a per-device api_key (fk-…) wins over a legacy master_key.
  CLIENT_MASTER_KEY=$(python3 -c "import json, os; c = json.load(open(os.path.expanduser('$CLIENT_CONF'))); print(c.get('api_key') or c.get('master_key') or '')" 2>/dev/null || echo "")
```

3e. `lib/ferry-claude.zsh`: in the `cl_key=$(python3 -c "…")` read, change

```python
    print(json.load(open(os.path.expanduser(sys.argv[1]))).get('master_key') or '')
```

to

```python
    c = json.load(open(os.path.expanduser(sys.argv[1])))
    print(c.get('api_key') or c.get('master_key') or '')
```

Then, in the mirror heredoc, change

```python
if key and key != "local":
    cfg["master_key"] = key
```

to

```python
if key and key != "local":
    # A per-device key (v1.39.0) is not the master and must not be named as one.
    cfg["api_key" if key.startswith("fk-") else "master_key"] = key
```

3f. `client-reset.sh`: in the tab-print line, replace `{c.get('master_key','')}` with `{c.get('api_key') or c.get('master_key','')}`. Then replace the message line with:

```zsh
  echo "Auth: using the key stored in $CLIENT_JSON (value never printed)"
```

3g. `client-to-host.sh`: directly above the `CLIENT_KEY="$(python3 …get('master_key')…)"` line, add:

```zsh
# master_key ONLY, never api_key: a per-device key (v1.39.0) authenticates one
# laptop to one host and must never become a new host's LITELLM_MASTER_KEY. A
# client that only holds a device key gets a freshly generated master instead.
```

3h. Rebuild with `cd /abs/worktree && zsh ./build.zsh`, then run `zsh ./build.zsh --check`. Expected: the check reports the monolith is fresh.

- [ ] **Step 4: Run.**

  Run each of these and expect `OK` from all three:
  - `cd /abs/worktree && python3 lib/ferry-clientbootstrap.test.py` (full file; it takes minutes, so use Bash `timeout: 600000`)
  - `python3 lib/ferry-fleet.test.py`
  - `python3 lib/ferry-claude.test.py`

- [ ] **Step 5: Commit.**

  Write `/tmp/ferry-keys-t7-msg.txt`:

```
feat(client): bootstrap enrolls a per-device key; readers prefer api_key

A master key given to client-bootstrap.sh is traded for a device key when
the host supports it (falls back to master_key on older hosts). ferry,
client-reset and the claude wrappers read api_key first;
client-to-host stays master_key-only on purpose.

Claude-Session-Id: b358f2c8-b360-463c-a431-5b7dd8808316
```

  Then run: `cd /abs/worktree && git add client-bootstrap.sh client-reset.sh client-to-host.sh lib/ferry-core.zsh lib/ferry-claude.zsh ferry lib/ferry-clientbootstrap.test.py lib/ferry-fleet.test.py lib/ferry-claude.test.py && git commit -F /tmp/ferry-keys-t7-msg.txt`

---

### Task 8: `ferry keys` CLI

**Files:**
- Create: `front/ferry_keys_cli.py`
- Create: `lib/ferry-keys.zsh`
- Modify: `build.zsh`, the `MODULES=(…)` line. Insert `keys` after `fleet`.
- Modify: `lib/ferry-main.zsh`. Add a case arm after `fleet)`, and add `keys` to the comment `(_ferry_<cmd>_usage: auth-claude, claude, fleet)`.
- Modify: `lib/ferry-usage.zsh`. Add a banner entry after the `fleet` entry.
- Modify: `lib/ferry-main.test.py`. Add `"ferry-keys.zsh"` to `USAGE_OWNERS`, plus one assertion.
- Regenerate: `ferry`, with `zsh ./build.zsh`.
- Test: `lib/ferry-keys.test.py` (new).

**Interfaces:**
- Consumes: Task 1 (`add`, `revoke`, `update`, `load`, `status`, `expires_from_date`, `KeyNameError`, `KeyStoreError`) and Task 2 (`Usage(path).summary()`, `usage_path()`, `UsageError`).
- Produces:
  - `ferry keys add <name> [--expires YYYY-MM-DD] [--lanes a,b] [--rpm N] [--budget-tokens N] [--replace]`. It prints the token ALONE on stdout (so `ferry keys add x | pbcopy` works) and the notice on stderr.
  - `ferry keys list`, printing columns `NAME STATUS CREATED EXPIRES LANES RPM BUDGET TOKENS(MONTH) REQ(LAST MIN)`.
  - `ferry keys revoke <name>`.
  - `ferry keys set <name> [--expires D|none] [--lanes a,b|none] [--rpm N|none] [--budget-tokens N|none]`.
  - Exit codes: 0 ok; 1 for a store or name error, with the message `ferry keys: …` on stderr and never a traceback; 2 for a usage error.
- Host only: on a client (`CLIENT_MODE=1`) it refuses with exit 1 before touching anything.

- [ ] **Step 1: Write the failing tests**

Create `lib/ferry-keys.test.py`:

```python
#!/usr/bin/env python3
"""`ferry keys` — the host CLI over the device-key store.

Run:  python3 lib/ferry-keys.test.py

The Python CLI (front/ferry_keys_cli.py) is exercised directly with
FERRY_KEYS_FILE / FERRY_KEYS_DB in a temp dir. The zsh wrapper
(lib/ferry-keys.zsh) is exercised through a tiny harness that sources ONLY that
module, so the built monolith's host-mode load path is never run on this
machine; the built `ferry` is run only in CLIENT mode (a temp HOME holding a
client.json), where it must refuse.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLI = os.path.join(REPO, "front", "ferry_keys_cli.py")
MODULE = os.path.join(REPO, "lib", "ferry-keys.zsh")
FERRY = os.path.join(REPO, "ferry")
sys.path.insert(0, os.path.join(REPO, "front"))

import ferry_keys_usage as U  # noqa: E402

TOKEN_RE = re.compile(r"^fk-[a-z0-9-]+-[a-z2-7]{32}$")


class CliCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ferry-keys-cli-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.keys = os.path.join(self.dir, "keys.json")
        self.db = os.path.join(self.dir, "keys-usage.sqlite")
        self.env = dict(os.environ, FERRY_KEYS_FILE=self.keys, FERRY_KEYS_DB=self.db,
                        HOME=self.dir)

    def cli(self, *args):
        return subprocess.run([sys.executable, CLI, *args], capture_output=True,
                              text=True, env=self.env, timeout=30)

    def doc(self):
        with open(self.keys) as fh:
            return json.load(fh)


class TestAdd(CliCase):
    def test_token_alone_on_stdout_and_only_its_hash_stored(self):
        p = self.cli("add", "MBP Work", "--rpm", "30", "--lanes", "flash,heavy",
                     "--budget-tokens", "100000", "--expires", "2027-01-01")
        self.assertEqual(p.returncode, 0, p.stderr)
        token = p.stdout.strip()
        self.assertRegex(token, TOKEN_RE)
        self.assertEqual(p.stdout, token + "\n")
        self.assertNotIn(token, p.stderr)
        self.assertIn("mbp-work", p.stderr)
        entry = self.doc()["keys"][0]
        self.assertEqual((entry["name"], entry["rpm"], entry["lanes"], entry["budget_tokens"],
                          entry["expires"]),
                         ("mbp-work", 30, ["flash", "heavy"], 100000, "2027-01-01T00:00:00Z"))
        self.assertNotIn(token, open(self.keys).read())

    def test_duplicate_is_exit_1_and_replace_rotates(self):
        first = self.cli("add", "laptop").stdout.strip()
        p = self.cli("add", "laptop")
        self.assertEqual(p.returncode, 1)
        self.assertIn("ferry keys:", p.stderr)
        self.assertIn("already exists", p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        second = self.cli("add", "laptop", "--replace").stdout.strip()
        self.assertNotEqual(first, second)
        self.assertEqual(len(self.doc()["keys"]), 1)

    def test_bad_limits_are_usage_errors(self):
        for args in (("--rpm", "0"), ("--rpm", "x"), ("--budget-tokens", "-5"),
                     ("--expires", "tomorrow"), ("--lanes", ",")):
            with self.subTest(args=args):
                p = self.cli("add", "x", *args)
                self.assertEqual(p.returncode, 2)
        self.assertFalse(os.path.exists(self.keys))


class TestListRevokeSet(CliCase):
    def test_list_shows_status_limits_and_usage_but_no_secrets(self):
        token = self.cli("add", "laptop", "--rpm", "5").stdout.strip()
        self.cli("add", "old")
        self.cli("revoke", "old")
        U.Usage(self.db).add_tokens("laptop", 1234)
        p = self.cli("list")
        self.assertEqual(p.returncode, 0, p.stderr)
        header, *rows = p.stdout.splitlines()
        self.assertEqual(header.split(), ["NAME", "STATUS", "CREATED", "EXPIRES", "LANES",
                                          "RPM", "BUDGET", "TOKENS(MONTH)", "REQ(LAST", "MIN)"])
        laptop = next(r for r in rows if r.startswith("laptop"))
        self.assertEqual(laptop.split()[1], "active")
        self.assertEqual(laptop.split()[5], "5")
        self.assertEqual(laptop.split()[7], "1234")
        self.assertEqual(next(r for r in rows if r.startswith("old")).split()[1], "revoked")
        self.assertNotIn(token, p.stdout)
        self.assertNotRegex(p.stdout, r"[0-9a-f]{64}")

    def test_empty_store_says_how_to_start(self):
        p = self.cli("list")
        self.assertEqual(p.returncode, 0)
        self.assertIn("ferry keys add", p.stdout)
        self.assertFalse(os.path.exists(self.db))

    def test_corrupt_store_is_a_one_line_error(self):
        with open(self.keys, "w") as fh:
            fh.write("{nope")
        p = self.cli("list")
        self.assertEqual(p.returncode, 1)
        self.assertTrue(p.stderr.startswith("ferry keys:"), p.stderr)
        self.assertNotIn("Traceback", p.stderr)

    def test_revoke_unknown_is_exit_1(self):
        p = self.cli("revoke", "ghost")
        self.assertEqual(p.returncode, 1)
        self.assertIn("no key named", p.stderr)

    def test_set_changes_and_clears_limits(self):
        self.cli("add", "laptop", "--rpm", "5")
        self.assertEqual(self.cli("set", "laptop", "--rpm", "none",
                                  "--budget-tokens", "900").returncode, 0)
        entry = self.doc()["keys"][0]
        self.assertIsNone(entry["rpm"])
        self.assertEqual(entry["budget_tokens"], 900)
        self.assertEqual(self.cli("set", "laptop").returncode, 2)


class TestZshWrapper(CliCase):
    def harness(self, client_mode):
        path = os.path.join(self.dir, "harness.zsh")
        with open(path, "w") as fh:
            fh.write('APP_DIR="%s"\nCLIENT_MODE=%d\nsource "%s"\ncmd_keys "$@"\n'
                     % (REPO, client_mode, MODULE))
        return path

    def run_zsh(self, client_mode, *args):
        return subprocess.run(["zsh", self.harness(client_mode), *args],
                              capture_output=True, text=True, env=self.env, timeout=30)

    def test_host_mode_runs_the_cli(self):
        p = self.run_zsh(0, "add", "laptop")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertRegex(p.stdout.strip(), TOKEN_RE)
        self.assertEqual(self.run_zsh(0, "revoke", "ghost").returncode, 1)

    def test_client_mode_refuses_before_touching_anything(self):
        p = self.run_zsh(1, "add", "laptop")
        self.assertEqual(p.returncode, 1)
        self.assertIn("runs on the host", p.stderr)
        self.assertFalse(os.path.exists(self.keys))

    def test_no_arguments_prints_usage_and_exits_1(self):
        p = self.run_zsh(0)
        self.assertEqual(p.returncode, 1)
        self.assertIn("ferry keys add", p.stdout)

    def test_built_ferry_on_a_client_refuses_and_help_writes_nothing(self):
        cfg = os.path.join(self.dir, ".config", "ferry")
        os.makedirs(cfg)
        with open(os.path.join(cfg, "client.json"), "w") as fh:
            json.dump({"host": "127.0.0.1", "port": "1", "share_port": "1",
                       "name": "laptop"}, fh)
        p = subprocess.run(["zsh", FERRY, "keys", "list"], capture_output=True, text=True,
                           env=self.env, cwd=REPO, timeout=30)
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("runs on the host", p.stderr)
        p = subprocess.run(["zsh", FERRY, "keys", "--help"], capture_output=True, text=True,
                           env=self.env, cwd=REPO, timeout=30)
        self.assertEqual(p.returncode, 0)
        self.assertIn("ferry keys add", p.stdout)
        self.assertFalse(os.path.exists(self.keys))


if __name__ == "__main__":
    unittest.main(verbosity=2)
```

In `lib/ferry-main.test.py`:
- Change `USAGE_OWNERS` to `("ferry-claude.zsh", "ferry-fleet.zsh", "ferry-auth-claude.zsh", "ferry-keys.zsh")`.
- Add to `test_help_output_is_the_commands_own_section`:

```python
        proc, _ = self.run_ferry("keys", "--help")
        self.assertIn("ferry keys revoke <name>", proc.stdout)
```

- [ ] **Step 2: Control.**

  Run: `cd /abs/worktree && python3 lib/ferry-keys.test.py`

  Expected failures:
  - every `CliCase` test fails with `can't open file '…/front/ferry_keys_cli.py'` (exit 2 ≠ 0);
  - the zsh tests fail on `source` of a missing module.

  Before editing `ferry-main.test.py`, check the harness: with the `USAGE_OWNERS` change alone, `python3 lib/ferry-main.test.py` errors on sourcing a missing `ferry-keys.zsh`. With the assertion alone, it fails with "Unknown command: keys".

- [ ] **Step 3: Implement**

3a. Create `front/ferry_keys_cli.py`:

```python
#!/usr/bin/env python3
"""`ferry keys` — mint, list, revoke and limit per-device client keys.

Invoked by lib/ferry-keys.zsh on the HOST as
`python3 $APP_DIR/front/ferry_keys_cli.py <verb> ...`. A new key is printed
ONCE, alone on stdout; only its sha256 is stored. Store paths come from
FERRY_KEYS_FILE / FERRY_KEYS_DB (defaults under ~/.config/ferry/).
Exit: 0 ok, 1 store/name error ("ferry keys: ..." on stderr), 2 usage error.
"""
import argparse
import sys

import ferry_keys as K
import ferry_keys_usage as U


def _positive(text):
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError("expected a positive integer, got %r" % text)
    if value < 1:
        raise argparse.ArgumentTypeError("expected a positive integer, got %r" % text)
    return value


def _limit(text):
    return None if text == "none" else _positive(text)


def _lanes(text):
    if text == "none":
        return None
    lanes = [part.strip() for part in text.split(",") if part.strip()]
    if not lanes:
        raise argparse.ArgumentTypeError("expected lane names like flash,heavy or 'none'")
    return lanes


def _expires(text):
    if text == "none":
        return None
    try:
        return K.expires_from_date(text)
    except ValueError as err:
        raise argparse.ArgumentTypeError(str(err))


def _parser():
    parser = argparse.ArgumentParser(prog="ferry keys",
                                     description="Per-device client keys for this host.")
    verbs = parser.add_subparsers(dest="verb", required=True)

    add = verbs.add_parser("add", help="mint a key (printed once, alone on stdout)")
    add.add_argument("name")
    add.add_argument("--expires", type=_expires, default=None, metavar="YYYY-MM-DD")
    add.add_argument("--lanes", type=_lanes, default=None, metavar="LANE[,LANE]")
    add.add_argument("--rpm", type=_positive, default=None, metavar="N")
    add.add_argument("--budget-tokens", type=_positive, default=None, metavar="N")
    add.add_argument("--replace", action="store_true",
                     help="rotate an existing key of this name in place")

    verbs.add_parser("list", help="every key with its status, limits and usage")

    revoke = verbs.add_parser("revoke", help="refuse a key from the next request on")
    revoke.add_argument("name")

    setp = verbs.add_parser("set", help="change a key's limits ('none' clears one)")
    setp.add_argument("name")
    setp.add_argument("--expires", type=_expires, default=argparse.SUPPRESS)
    setp.add_argument("--lanes", type=_lanes, default=argparse.SUPPRESS)
    setp.add_argument("--rpm", type=_limit, default=argparse.SUPPRESS)
    setp.add_argument("--budget-tokens", type=_limit, default=argparse.SUPPRESS)
    return parser


def _cell(value):
    return "-" if value is None else str(value)


def _list():
    doc = K.load()
    if not doc["keys"]:
        print("No device keys yet. Create one with: ferry keys add <name>")
        return 0
    try:
        usage = U.Usage().summary()
    except U.UsageError as err:
        print("ferry keys: usage unavailable: %s" % err, file=sys.stderr)
        usage = None
    rows = [("NAME", "STATUS", "CREATED", "EXPIRES", "LANES", "RPM", "BUDGET",
             "TOKENS(MONTH)", "REQ(LAST MIN)")]
    for entry in doc["keys"]:
        used = (usage or {}).get(entry["name"], {"month_tokens": 0, "minute_requests": 0})
        rows.append((
            entry["name"], K.status(entry), (entry.get("created") or "-")[:10],
            (entry.get("expires") or "-")[:10],
            ",".join(entry["lanes"]) if entry.get("lanes") else "all",
            _cell(entry.get("rpm")), _cell(entry.get("budget_tokens")),
            "?" if usage is None else str(used["month_tokens"]),
            "?" if usage is None else str(used["minute_requests"]),
        ))
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    for row in rows:
        print("  ".join(cell.ljust(width) for cell, width in zip(row, widths)).rstrip())
    return 0


def main(argv=None):
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.verb == "add":
            name, token = K.add(args.name, expires=args.expires, lanes=args.lanes,
                                rpm=args.rpm, budget_tokens=args.budget_tokens,
                                replace=args.replace)
            print(token)
            print("ferry keys: created %r. The key above is shown ONCE; put it in "
                  "the device's client.json as api_key (or re-run client-bootstrap.sh "
                  "there with the master key to enroll automatically)." % name,
                  file=sys.stderr)
            return 0
        if args.verb == "list":
            return _list()
        if args.verb == "revoke":
            K.revoke(args.name)
            print("ferry keys: revoked %r; it is refused from the next request on."
                  % args.name, file=sys.stderr)
            return 0
        changes = {field: getattr(args, field)
                   for field in ("expires", "lanes", "rpm", "budget_tokens")
                   if hasattr(args, field)}
        if not changes:
            parser.error("set needs at least one of --expires --lanes --rpm --budget-tokens")
        K.update(args.name, **changes)
        print("ferry keys: updated %r." % args.name, file=sys.stderr)
        return 0
    except (K.KeyNameError, K.KeyStoreError, OSError) as err:
        print("ferry keys: %s" % err, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
```

3b. Create `lib/ferry-keys.zsh`:

```zsh
# ferry keys — per-device client keys on the HOST. Thin wrapper over
# front/ferry_keys_cli.py, which edits ~/.config/ferry/keys.json (hashes only,
# 0600) and reads the usage counters the front door keeps in
# ~/.config/ferry/keys-usage.sqlite. A client has no key store, so it refuses.

# `ferry keys --help` text. A function of its own so the dispatcher in
# lib/ferry-main.zsh can print it WITHOUT entering cmd_keys.
_ferry_keys_usage() {
  cat <<'EOF'
ferry keys — per-device client keys (host only).

Usage:
  ferry keys add <name> [--expires YYYY-MM-DD] [--lanes L1,L2] [--rpm N]
                        [--budget-tokens N] [--replace]
                                    Mint a key. It is printed ONCE, alone on
                                    stdout; only its hash is stored.
  ferry keys list                   Every key: status, limits, tokens this
                                    month, requests in the last minute.
  ferry keys revoke <name>          Refuse the key from the next request on.
  ferry keys set <name> [--expires D|none] [--lanes L1,L2|none]
                        [--rpm N|none] [--budget-tokens N|none]
                                    Change a key's limits; 'none' clears one.
  ferry keys --help                 This message.

Budgets are TOKENS (input + output) per calendar month (UTC). A client gets
its key automatically by re-running client-bootstrap.sh with the master key.
EOF
}

cmd_keys() {
  case "${1:-}" in
    ""|-h|--help)
      _ferry_keys_usage
      [[ -z "${1:-}" ]] && exit 1
      return 0
      ;;
  esac
  if [[ "$CLIENT_MODE" == "1" ]]; then
    echo "ferry keys runs on the host (it edits the host's ~/.config/ferry/keys.json); this machine is a client." >&2
    exit 1
  fi
  python3 "$APP_DIR/front/ferry_keys_cli.py" "$@"
}
```

3c. In `build.zsh`, set:

```zsh
MODULES=(core usage install serve share inbox relay vnc transfer drop proxy integrate claude auth-claude fleet keys dash update migrate main)
```

3d. In `lib/ferry-main.zsh`, add this line after `  fleet)         cmd_fleet "$@" ;;`:

```zsh
  keys)          cmd_keys "$@" ;;
```

Then change the comment `(_ferry_<cmd>_usage: auth-claude, claude, fleet)` to `(_ferry_<cmd>_usage: auth-claude, claude, fleet, keys)`.

3e. In `lib/ferry-usage.zsh`, directly after the fleet entry's `ferry fleet ls | show | use <fleet> [--default] | use --clear` line, insert:

```
  keys               [Host] Per-device client keys: mint, list, revoke, and
                       limit (lanes, requests/min, monthly token budget)
                        ferry keys add <name> | list | revoke <name> | set <name> ...
```

3f. Rebuild and check:
- `cd /abs/worktree && zsh ./build.zsh`. Expected: `Built ferry from 20 modules: …`.
- `zsh ./build.zsh --check`. Expected: `ferry is in sync with lib/ ✓`.

- [ ] **Step 4: Run.** Expect `OK` from each:
  - `cd /abs/worktree && python3 lib/ferry-keys.test.py`
  - `python3 lib/ferry-main.test.py`
  - `python3 lib/ferry-fleet.test.py`

- [ ] **Step 5: Commit.** Write `/tmp/ferry-keys-t8-msg.txt`:

```
feat(cli): ferry keys add | list | revoke | set

Host-only wrapper over front/ferry_keys_cli.py; a new key prints once,
alone on stdout. Registered in build.zsh, the dispatcher and the banner.

Claude-Session-Id: b358f2c8-b360-463c-a431-5b7dd8808316
```

Run: `cd /abs/worktree && git add front/ferry_keys_cli.py lib/ferry-keys.zsh build.zsh lib/ferry-main.zsh lib/ferry-usage.zsh lib/ferry-main.test.py lib/ferry-keys.test.py ferry && git commit -F /tmp/ferry-keys-t8-msg.txt`

**Merge note (T7 ∥ T8):** both tasks regenerate `ferry`. Never hand-merge `ferry`. Resolve a conflict by merging the `lib/` sources, then running `zsh ./build.zsh`, then `git add ferry`. See Global Constraints.

---

### Task 9: Dashboard — keys card and per-key feed label (`ferry-dash`)

**Files:**
- Modify `ferry-dash`. Resolve each place by its symbol:
  - after `def _catalog` (`:161 (_catalog)`), add `_keys_mods` and `keys_summary`;
  - in `get_status` (`:976 (get_status)`), add the `out["keys"]` entry;
  - after the `<section class="card full" id="live">` line (`:1489`), add the card HTML;
  - after `function renderFleets` (`:1593`), add `renderKeys`;
  - in `renderFeed`, the `w.textContent=` line (`:2102`);
  - in `tick`, the `renderFleets(st);` call (`:2162`).
- Test: `lib/ferry-dashserver.test.py` (new class plus one method), `lib/ferry-dashui.test.mjs` (a new block before the summary).

**Interfaces:**
- Consumes:
  - Task 1: `load`, `status`.
  - Task 2: `Usage().summary()`, which returns `{}` and creates nothing when the DB is absent.
  - Task 6: the event field `key`.
- Produces:
  - `GET /status` JSON gains `"keys": {"error": str | None, "keys": [{"name", "status", "created", "expires", "lanes", "rpm", "budget_tokens", "month_tokens", "minute_requests"}]}`. It never carries a token or a hash.
  - JS `renderKeys(ks)`.

- [ ] **Step 1: Write the failing tests**

1a. Append to `lib/ferry-dashserver.test.py`, before `if __name__ == "__main__":`:

```python
class KeysSummaryTests(unittest.TestCase):
    """The dash's device-key card: names, status, limits and usage — never a
    key or its hash. FERRY_KEYS_FILE / FERRY_KEYS_DB point into a temp dir."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ferry-dash-keys-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.keys = os.path.join(self.tmp, "keys.json")
        self.db = os.path.join(self.tmp, "keys-usage.sqlite")
        patcher = unittest.mock.patch.dict(os.environ, {
            "FERRY_KEYS_FILE": self.keys, "FERRY_KEYS_DB": self.db})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_summary_has_status_limits_usage_and_no_secrets(self):
        K, U = dash._keys_mods()
        _, token = K.add("mbp", rpm=30, budget_tokens=1000)
        K.add("old")
        K.revoke("old")
        U.Usage().add_tokens("mbp", 12)
        s = dash.keys_summary()
        self.assertIsNone(s["error"])
        by = {k["name"]: k for k in s["keys"]}
        self.assertEqual(by["mbp"]["status"], "active")
        self.assertEqual((by["mbp"]["rpm"], by["mbp"]["budget_tokens"],
                          by["mbp"]["month_tokens"]), (30, 1000, 12))
        self.assertEqual(by["old"]["status"], "revoked")
        text = json.dumps(s)
        self.assertNotIn(token, text)
        self.assertNotRegex(text, r"[0-9a-f]{64}")

    def test_missing_store_is_empty_and_creates_nothing(self):
        self.assertEqual(dash.keys_summary(), {"error": None, "keys": []})
        self.assertFalse(os.path.exists(self.keys))
        self.assertFalse(os.path.exists(self.db))

    def test_corrupt_store_is_reported_not_raised(self):
        with open(self.keys, "w") as fh:
            fh.write("{nope")
        s = dash.keys_summary()
        self.assertEqual(s["keys"], [])
        self.assertTrue(s["error"].startswith("keys.json unreadable"), s["error"])
```

If they are missing from the file's imports, add `import shutil` and `import tempfile`. `tempfile` is already used by `HttpServerTests`.

Add this method to `HttpServerTests`:

```python
    def test_status_carries_the_keys_card(self):
        with tempfile.TemporaryDirectory() as tmp, unittest.mock.patch.dict(os.environ, {
                "FERRY_KEYS_FILE": os.path.join(tmp, "keys.json"),
                "FERRY_KEYS_DB": os.path.join(tmp, "u.sqlite")}):
            status, _, body = self._get("/status")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["keys"], {"error": None, "keys": []})
```

1b. In `lib/ferry-dashui.test.mjs`, insert this before the final `console.log('');` / `if (fails)` lines:

```js
console.log('device keys card:');
check('the keys card is in the page', page.includes('<section class="card full" id="keys">') && page.includes('id="keyrows"') && page.includes('id="keysmeta"'));
check('tick renders the keys card every poll', /renderFleets\(st\);\s*renderKeys\(st\.keys\)/.test(script));
check('the feed names the calling key', script.includes("(e.key?'['+e.key+'] ':'')"));
const rkSrc = script.match(/function renderKeys\(ks\)\{[\s\S]*?\n\}/)?.[0];
check('renderKeys exists', !!rkSrc);
if (rkSrc) {
  const mk = (t, c, x) => ({ tag: t, className: c || '', textContent: x == null ? '' : String(x), children: [],
    appendChild(n) { this.children.push(n); return n; }, set innerHTML(v) { this.children = []; } });
  const nodes = { '#keyrows': mk('div'), '#keysmeta': mk('span') };
  const kctx = vm.createContext({ $: s => nodes[s], el: mk });
  vm.runInContext(rkSrc + ';renderKeys({error:null,keys:[' +
    '{name:"mbp",status:"active",lanes:null,rpm:30,budget_tokens:1000,month_tokens:12,minute_requests:1,expires:null},' +
    '{name:"old",status:"revoked",lanes:["flash"],rpm:null,budget_tokens:null,month_tokens:0,minute_requests:0,expires:null}]})', kctx);
  const text = JSON.stringify(nodes['#keyrows']);
  check('renderKeys lists every key with its status and usage', nodes['#keyrows'].children.length === 2 && text.includes('mbp') && text.includes('12 / 1000') && text.includes('revoked'));
  check('renderKeys counts the active keys', nodes['#keysmeta'].textContent === '1 active of 2');
  vm.runInContext('renderKeys({error:null,keys:[]})', kctx);
  check('an empty store says how to mint a key', nodes['#keyrows'].textContent.includes('ferry keys add'));
  vm.runInContext('renderKeys(undefined)', kctx);
  check('a status without keys (older front) renders, not throws', nodes['#keyrows'].textContent.includes('ferry keys add'));
}
```

- [ ] **Step 2: Control.** Run each of these:
  - `cd /abs/worktree && python3 lib/ferry-dashserver.test.py`. Expected: `AttributeError: module 'ferrydash_server' has no attribute '_keys_mods'` and `… 'keys_summary'`, plus `KeyError: 'keys'` in `test_status_carries_the_keys_card`.
  - `node lib/ferry-dashui.test.mjs`. Expected: the 4 new checks `FAIL`, and the run exits 1.

- [ ] **Step 3: Implement**

3a. After the `_catalog` function, add:

```python
_KEYS_MODS = None


def _keys_mods():
    """(ferry_keys, ferry_keys_usage) from front/, loaded by path, or None.

    Registered in sys.modules under their plain names so a caller that already
    imported them (a test, the front door in the same process) shares them."""
    global _KEYS_MODS
    if _KEYS_MODS is None:
        try:
            import importlib.machinery
            import importlib.util
            import sys
            mods = []
            for name in ("ferry_keys", "ferry_keys_usage"):
                if name not in sys.modules:
                    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                        "front", name + ".py")
                    spec = importlib.util.spec_from_loader(
                        name, importlib.machinery.SourceFileLoader(name, path))
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)
                    sys.modules[name] = module
                mods.append(sys.modules[name])
            _KEYS_MODS = tuple(mods)
        except Exception:
            _KEYS_MODS = False
    return _KEYS_MODS or None


def keys_summary():
    """Device keys for the dash card. Names, status, limits and usage only:
    a key or its hash never leaves the host's key store."""
    mods = _keys_mods()
    if mods is None:
        return {"error": "the key store module (front/ferry_keys.py) is unavailable",
                "keys": []}
    keys_mod, usage_mod = mods
    try:
        doc = keys_mod.load()
    except Exception as err:
        return {"error": "keys.json unreadable: %s" % err, "keys": []}
    error = None
    try:
        usage = usage_mod.Usage().summary()
    except Exception as err:
        usage, error = {}, "usage unavailable: %s" % err
    rows = []
    for entry in doc["keys"]:
        used = usage.get(entry["name"], {})
        rows.append({
            "name": entry["name"], "status": keys_mod.status(entry),
            "created": entry.get("created"), "expires": entry.get("expires"),
            "lanes": entry.get("lanes"), "rpm": entry.get("rpm"),
            "budget_tokens": entry.get("budget_tokens"),
            "month_tokens": used.get("month_tokens", 0),
            "minute_requests": used.get("minute_requests", 0),
        })
    return {"error": error, "keys": rows}
```

3b. In `get_status`, in the `out = {…}` literal, directly after the `"fleet": fleet,` line, add:

```python
        # Device keys (v1.39.0): read straight from the host's key store, so
        # the card works while the front door is down.
        "keys": keys_summary(),
```

3c. Directly after the `<section class="card full" id="live">…</section>` line, add:

```html
    <section class="card full" id="keys"><h2>Device keys <span class="muted" id="keysmeta"></span></h2><div id="keyrows" class="muted">Loading…</div></section>
```

3d. Directly after the whole `function renderFleets(st){…}` block (after its closing `}` at column 0), add:

```js
function renderKeys(ks){
  const box=$('#keyrows'); box.innerHTML=''; box.className='';
  const meta=$('#keysmeta');
  const rows=(ks&&ks.keys)||[];
  meta.textContent=rows.length?rows.filter(k=>k.status==='active').length+' active of '+rows.length:'';
  if(ks&&ks.error&&!rows.length){ box.className='muted'; box.textContent=ks.error; return; }
  if(!rows.length){ box.className='muted'; box.textContent='No device keys. Mint one on the host with: ferry keys add <name>'; return; }
  rows.forEach(k=>{
    const r=el('div','row');
    r.appendChild(el('span',null,k.name));
    r.appendChild(el('span',k.status==='active'?'ok':'bad',k.status));
    const tokens=k.budget_tokens?k.month_tokens+' / '+k.budget_tokens+' tok':k.month_tokens+' tok';
    const reqs=k.minute_requests+(k.rpm?' / '+k.rpm:'')+' req/min';
    r.appendChild(el('span','muted',tokens+' this month · '+reqs+' · lanes '+(k.lanes?k.lanes.join(','):'all')
      +(k.expires?' · expires '+String(k.expires).slice(0,10):'')));
    box.appendChild(r);
  });
  if(ks.error) box.appendChild(el('div','muted',ks.error));
}
```

3e. In `renderFeed`, change

```js
    w.textContent=(e.lane||'?')+' → '+shortModel(e.deployment||e.model)
```

to

```js
    w.textContent=(e.key?'['+e.key+'] ':'')+(e.lane||'?')+' → '+shortModel(e.deployment||e.model)
```

3f. In `tick`, change `renderLive(st); renderFleets(st);` to `renderLive(st); renderFleets(st); renderKeys(st.keys);`.

- [ ] **Step 4: Run.** Expect `OK` and `N checks passed` from:
  - `cd /abs/worktree && python3 lib/ferry-dashserver.test.py`
  - `node lib/ferry-dashui.test.mjs`
  - `python3 lib/ferry-dashroutes.test.py`

- [ ] **Step 5: Commit.** Write `/tmp/ferry-keys-t9-msg.txt`:

```
feat(dash): device keys card and per-key labels in the live feed

Claude-Session-Id: b358f2c8-b360-463c-a431-5b7dd8808316
```

Run: `cd /abs/worktree && git add ferry-dash lib/ferry-dashserver.test.py lib/ferry-dashui.test.mjs && git commit -F /tmp/ferry-keys-t9-msg.txt`

---

### Task 10: Docs, release notes, version, full suite

**Files:**
- Modify `README.md`:
  - Contents list;
  - a new `## Device keys` section directly above `## Privacy`;
  - the Privacy paragraph;
  - the Command reference row after `fleet`;
  - the Recent releases list;
  - the Development "18 per-domain modules" sentence;
  - the FAQ "Do clients need API keys?" answer.
- Create: `docs/releases/v1.39.0.md`.
- Modify: `docs/roadmap.md`, item 06.
- Modify: `docs/superpowers/specs/2026-09-27-client-keys-design.md`, the Status line and "Open questions".
- Modify: `client-config-example.json`, the three `apiKey` placeholders.
- Modify: `VERSION`, changing `1.38.0` to `1.39.0`.

**Interfaces:**
- Consumes the shipped behaviour of Tasks 1–9. It runs only after all of them have merged into one branch and `zsh ./build.zsh --check` passes there.

- [ ] **Step 1: Baseline the full suite on the merged branch, before editing docs.**

Write `/tmp/ferry-keys-fullsuite.zsh` with the Write tool. Replace `/abs/worktree` with your literal path:

```zsh
#!/bin/zsh
# Full ferry suite, one log per suite under /tmp/ferry-keys-suite/.
cd /abs/worktree || exit 1
mkdir -p /tmp/ferry-keys-suite
fail=0
for suite in lib/*.test.py observ/*.test.py scripts/*.test.py; do
  if python3 "$suite" > "/tmp/ferry-keys-suite/${suite:t}.log" 2>&1; then
    print "ok    $suite"
  else
    print "FAIL  $suite"; fail=1
  fi
done
if node lib/ferry-dashui.test.mjs > /tmp/ferry-keys-suite/dashui.log 2>&1; then
  print "ok    lib/ferry-dashui.test.mjs"
else
  print "FAIL  lib/ferry-dashui.test.mjs"; fail=1
fi
zsh ./build.zsh --check || fail=1
print "fail=$fail"
exit $fail
```

Run: `zsh /tmp/ferry-keys-fullsuite.zsh` with the Bash tool's `timeout: 600000`. It takes about 5–6 min.

Expected: every line is `ok`, the last line is `fail=0`, and `ferry is in sync with lib/ ✓` appears.

If a suite fails, read its log in `/tmp/ferry-keys-suite/`. **A FAIL is a claim about the check until the suite has passed on the base.** To run the same suite at the base without `git stash`, open a throwaway worktree at `43acdda` under `/tmp` and run that one suite there. Only then attribute the failure to this feature.

- [ ] **Step 2: README.**

2a. In Contents, add a line directly above `- [Privacy](#privacy)`:

```markdown
- [Device keys](#device-keys)
```

2b. Directly above `## Privacy`, insert:

```markdown
## Device keys

Every client can hold **its own key** instead of the shared master key, so you can see which device spent what, cut off one lost laptop without re-keying the rest, and optionally cap a device.

- **Getting one is automatic.** Run the client bootstrap with the master key (`FERRY_MASTER_KEY=… curl …/client-bootstrap.sh | zsh`). A v1.39.0 host mints a device key named after the machine and the client stores only that (`api_key` in `~/.config/ferry/client.json`), never the master. Re-running the bootstrap rotates the key under the same name. An older host just keeps the master-key setup. Existing clients keep working on the master key until they re-run the bootstrap; `ferry update` never touches credentials.
- **Managing them (host only):**

  ```bash
  ferry keys add ci-box --lanes flash --rpm 30 --budget-tokens 2000000 --expires 2027-01-01   # prints the key once
  ferry keys list                  # status, limits, tokens this month, requests this minute
  ferry keys revoke old-laptop     # refused from the next request on, no restart
  ferry keys set ci-box --rpm none # 'none' clears a limit
  ```

- **Limits are optional.** `--lanes` accepts bare lanes (`flash`, which means any fleet's flash) or fleet lanes (`international.flash`); `orch` counts as `heavy`. A disallowed lane is a 403. `--rpm` is a host-wide requests-per-minute cap (429 with `Retry-After`). `--budget-tokens` caps **input + output tokens per calendar month (UTC)**, answered 429 `insufficient_quota` once spent. Usage is added when a response completes, so the request that crosses the line finishes and the next one is refused. There are no USD budgets in v1.
- **Where it shows.** `ferry dash` has a *Device keys* card, and each live-feed row is prefixed with the calling key's name. Every event record carries a `key` field holding the key's name, `master`, or empty.
- **Failure modes.** A corrupt `keys.json` refuses every device key (401) and a broken usage DB refuses them with a 503. Each problem gets one rate-limited line in the front log. The master key keeps working through both.
- **Files:** `~/.config/ferry/keys.json` (0600; hashes only, and a key is never stored or logged) and `~/.config/ferry/keys-usage.sqlite` (counters).
```

2c. In `## Privacy`, replace the sentence `The front door answers only requests carrying the **master key** — one shared secret you set in \`LITELLM_MASTER_KEY\` and every client holds a copy of (a keyless request gets a 401).` with:

```markdown
The front door answers only requests carrying the **master key** (`LITELLM_MASTER_KEY`) or a **[device key](#device-keys)** minted from it (a keyless request gets a 401); a bootstrapped v1.39.0 client holds only its own revocable device key.
```

Then replace the clause `the master key is the one credential a client holds` with `its ferry key is the one credential a client holds`.

2d. In the Command reference table, directly after the `fleet` row, add:

```markdown
| `keys add\|list\|revoke\|set` | host | Per-device client keys: mint (shown once), list with usage, revoke, and set lane / RPM / monthly token limits |
```

2e. In Recent releases, add this line first and delete the `v1.36.0` line so that three entries remain. Move its `[Full history →](docs/releases)` link onto the end of the `v1.37.0` line:

```markdown
- **[v1.39.0 — per-device keys](docs/releases/v1.39.0.md)** — every client gets its own revocable key at bootstrap, with optional lane, requests-per-minute and monthly token limits; `ferry keys` manages them and the dash shows who spent what.
```

2f. In Development, replace `assembled from 18 per-domain modules` with the count `zsh ./build.zsh` printed in Task 8 (`Built ferry from N modules`); at 43acdda + Task 8 that is 20. The README's 18 was already stale at 19.

2g. In the FAQ, replace the answer to **Do clients need API keys?** with:

```markdown
**Do clients need API keys?** No provider keys. A client holds one ferry key for the front door — its own [device key](#device-keys) from v1.39.0, or the shared master key on older setups; provider keys and OAuth subscription logins exist only on the host.
```

- [ ] **Step 3: Release notes.** Create `docs/releases/v1.39.0.md`:

```markdown
# llm-ferry v1.39.0 — per-device keys

Every client can now hold its own key instead of the shared master key.

[Read the project overview](https://github.com/sblattj/llm-ferry/blob/main/README.md)

## What's new

- **Device keys, automatically.** Run `client-bootstrap.sh` with the master
  key and a v1.39.0 host mints a key for that machine
  (`POST /v1/ferry/keys/enroll`); the client stores only `api_key`, never the
  master. Re-running the bootstrap rotates the key under the same name.
- **`ferry keys add | list | revoke | set`** on the host. A new key is printed
  once, alone on stdout; only its sha256 is stored in
  `~/.config/ferry/keys.json` (0600). A revoke takes effect on the next
  request with no restart.
- **Optional limits per key:** allowed lanes (403 otherwise), requests per
  minute and a monthly token budget (429 with `Retry-After`; the budget
  answers `insufficient_quota`). Budgets count input + output tokens per
  calendar month (UTC); there are no USD budgets in this release.
- **Attribution.** A device key's name is its fleet identity, every event
  record carries `key`, and `ferry dash` gains a Device keys card and
  per-key labels in the live feed.

## Notes

- Existing clients keep working on the master key; only re-running the
  bootstrap moves one to a device key. `ferry update` never touches
  credentials.
- Fail-closed: a corrupt `keys.json` refuses device keys (401) and an unusable
  usage DB refuses them (503); the master key is unaffected by either.
- Tokens are charged when a response completes, so a budget can be overshot
  by the one request that crosses it.
- The front door must be restarted once (`ferry reload`) to load the new
  code; clients must re-pull `ferry` (`ferry update`) to read `api_key`.
- `client-to-host.sh` never promotes a device key to a new host's master key.
```

- [ ] **Step 4: Roadmap, spec, example config, version.**

4a. In `docs/roadmap.md`, replace item 06's four lines with:

```markdown
- [x] **06 Per-client virtual keys with budgets.** Shipped in v1.39.0 as
      per-device keys with token budgets ([spec](superpowers/specs/2026-09-27-client-keys-design.md),
      [release](releases/v1.39.0.md)). USD budgets are deferred (spec open
      question 1). Unblocks 13 (spend rollups), 25 (transcripts) and 08 (key rotation).
```

4b. In the spec, change the Status line to:

```markdown
Status: APPROVED 2026-09-27; implemented in v1.39.0 (plan: `docs/superpowers/plans/2026-09-27-client-keys.md`).
```

Then append under "## Open questions for review":

```markdown
**Resolved 2026-09-27:** (1) v1 is token-only (`budget_tokens`); USD is
deferred. (2) Only re-running bootstrap migrates a client; `ferry update`
never touches credentials. The plan also records eight corrections to this
draft — notably budgets count input + output only, since reasoning is a
subset of output.
```

4c. In `client-config-example.json`, replace each of the three occurrences of

```
<LITELLM_MASTER_KEY or \"local\" on a keyless LAN>
```

with

```
<your device key (api_key in ~/.config/ferry/client.json), the master key, or \"local\" on a keyless LAN>
```

Then run `cd /abs/worktree && python3 -c "import json; json.load(open('client-config-example.json'))"`. The file has no `//` comment lines at 43acdda, so it must still parse as JSON.

4d. Replace the contents of `VERSION` with `1.39.0` and a newline.

- [ ] **Step 5: Final verification.**

- Run `zsh /tmp/ferry-keys-fullsuite.zsh` again (`timeout: 600000`). Expected: `fail=0`.
- Check the release notes heading matches the title format of `docs/releases/v1.38.0.md`: `cd /abs/worktree && head -1 docs/releases/v1.39.0.md docs/releases/v1.38.0.md`. Do not run `scripts/release-notes.py title v1.39.0` here: it derives the title from the git tag, and that tag does not exist until release.
- Check the new README anchor: `rg -n '\(#device-keys\)' README.md`. Expected: at least 3 hits (Contents, Privacy, FAQ).

- [ ] **Step 6: Commit.**

Write `/tmp/ferry-keys-t10-msg.txt`:

```
release: v1.39.0 — per-device keys

README Device keys section, release notes, roadmap 06 ticked, spec
approved with its resolved questions, example config and VERSION.

Claude-Session-Id: b358f2c8-b360-463c-a431-5b7dd8808316
```

Run: `cd /abs/worktree && git add README.md docs/releases/v1.39.0.md docs/roadmap.md docs/superpowers/specs/2026-09-27-client-keys-design.md client-config-example.json VERSION && git commit -F /tmp/ferry-keys-t10-msg.txt`

Tagging and pushing `v1.39.0` is the release owner's call. It is not part of this plan.

---

## Self-Review

**1. Spec coverage**

| Spec section | Where |
|---|---|
| §1 key format, hash-only 0600 store, revoked kept | T1 |
| §1 usage counters shared across workers | T2 (two-process test) |
| §2 fk- detection, rewrite to master, 401 on unknown/revoked/expired, fail-closed | T3 |
| §2 lanes / RPM / budget, 403 / 429 + Retry-After, SDK-shaped bodies | T3 (`key_error_body`), T4 |
| §2 token metering | T4 (tap-independent; spec correction 1) |
| §3 enroll endpoint, bootstrap auto-enroll, `ferry keys` CLI | T5, T7, T8 |
| §4 attribution: identity, events, dash | T3 (`caller_identity`), T6, T4 (`rec["key"]`), T9 |
| §5 testing | every task's Step 1/2 includes its control |
| §6 docs | T10 |
| Open question 1 (token-only) | T2/T4 (`budget_tokens`), T10 docs |
| Open question 2 (only bootstrap migrates) | T7 (only bootstrap writes `api_key`), T10 docs |

No gaps found.

**2. Placeholder scan.**
- Every code step carries the code.
- The one deliberate substitution is `/abs/worktree`, which the Seat environment section defines.
- T10 2f and 4c name the exact text to replace, and derive the module count from `build.zsh` output rather than guessing it.

**3. Type and name consistency.** The names below were checked across tasks:
- `KEY_SCOPE` / `"ferry.key"`, `KEY_ENTRY_SCOPE` / `"ferry.key_entry"` and `KEY_ADMITTED_SCOPE` / `"ferry.key_admitted"` match between T3, T4 and T5, and the tests use the string literals.
- `key_error_body(path, status, message, code=None, openai_type=None)` is used the same way in T3 and T4.
- `_reply(send, status, doc, headers=None)` is defined in T3 and used in T4 with `retry`.
- `add(..., unique=, replace=)` from T1 is used by T5 (`unique=not replace`) and T8 (`replace=`).
- `Usage.admit(name, rpm, budget_tokens)` returns `Verdict(ok, reason, retry_after)` in T2 and is read by T4.
- `Usage.summary()` from T2 is used by T8 and T9. It returns `{}` without creating the file, which T9 asserts.
- `lane_allowed(lanes, requested, resolved)` from T1 is used in T4.
- `_front_sibling` is defined in T3 and used by T4 (`_usage_module`).
- The client.json fields `api_key` and `key_name` match between T7's bootstrap, its readers and T10's docs.

**4. Review Focus.** All five lines have tests in their owning tasks:
- `x-api-key`: T3.
- WebSocket: T3.
- Unparseable body on a restricted key: T4.
- Accounting failure: T4.
- Master and bare requests untouched: T3 and T4.

**UNVERIFIED — for the implementing seats to confirm, not assume:**
- **WebSocket close.** uvicorn is expected to answer an HTTP 403 to the upgrade when the app sends `websocket.close` before `websocket.accept`. That is the ASGI spec; uvicorn's actual behaviour under litellm has not been observed. The T3 test asserts the middleware's message, not the wire.
- **Worker imports.** uvicorn `--workers N` children can load `ferry_keys` via `_front_sibling`, which resolves by file path, not `sys.path`. This has not been observed under the real launcher (`_ferry_launch_front`). Check it on the first `ferry reload` after release.
- **Where the key lands in `opencode-cloud.json`.** The exact nesting of the device key (`provider.ferry.options.apiKey` is expected) is unconfirmed. T7 asserts that the string is present and the master is absent.
- **Banner help.** `_ferry_usage_section` (the awk banner parser) should print the new `keys` banner entry for `ferry help keys`. It is not relied on: `_ferry_keys_usage` answers first, and `lib/ferry-main.test.py` checks the output.
- **Hostnames outside `[a-z0-9-]`.** A client whose `hostname -s` contains characters outside `[a-z0-9-]` gets a normalised name from the host. The bootstrap stores the name the host returned, and the T7 stub echoes the name verbatim, so the test assumes a plain hostname on the test machine.
- **Suite timing.** The full-suite time (about 317 s at base) is an estimate this plan did not re-measure. Keep `timeout: 600000`, or use `run_in_background: true`.


