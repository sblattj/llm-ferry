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

    def test_reserved_names_are_refused(self):
        # "master" would erase the credential class in scope["ferry.key"];
        # "host" would impersonate the host's sticky fleet identity.
        for bad in ("master", "Master", " HOST ", "host"):
            with self.subTest(bad=bad):
                with self.assertRaises(K.KeyNameError):
                    K.normalize_name(bad)
                with self.assertRaises(K.KeyNameError):
                    K.add(bad)
                with self.assertRaises(K.KeyNameError):
                    K.add(bad, unique=True)
        self.assertFalse(os.path.exists(self.path))
        self.assertEqual(K.normalize_name("master-laptop"), "master-laptop")
        self.assertEqual(K.normalize_name("hosts"), "hosts")

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

    def test_replace_applies_new_limits(self):
        K.add("laptop", rpm=5, budget_tokens=100)
        K.add("laptop", replace=True, rpm=9)
        entry = K.find(K.load(), "laptop")
        self.assertEqual((entry["rpm"], entry["budget_tokens"]), (9, 100))

    def test_replace_without_limit_args_keeps_old_limits(self):
        K.add("laptop", rpm=5, budget_tokens=100, lanes=["flash"])
        K.add("laptop", replace=True)
        entry = K.find(K.load(), "laptop")
        self.assertEqual((entry["rpm"], entry["budget_tokens"], entry["lanes"]),
                         (5, 100, ["flash"]))

    def test_replace_of_an_expired_entry_is_admitted(self):
        past = K.iso(datetime.datetime.now(UTC) - datetime.timedelta(days=1))
        K.add("laptop", expires=past)
        _, token = K.add("laptop", replace=True)
        self.assertIsNone(K.find(K.load(), "laptop")["expires"])
        self.assertEqual(K.KeyCache().lookup(token)[1], "ok")

    def test_replace_with_future_expires_sets_it(self):
        K.add("laptop")
        future = K.iso(datetime.datetime.now(UTC) + datetime.timedelta(days=3))
        _, token = K.add("laptop", replace=True, expires=future)
        self.assertEqual(K.find(K.load(), "laptop")["expires"], future)
        self.assertEqual(K.KeyCache().lookup(token)[1], "ok")

    def test_replace_validates_limits_before_writing(self):
        K.add("laptop", rpm=5)
        with open(self.path) as fh:
            before = fh.read()
        with self.assertRaises(K.KeyNameError):
            K.add("laptop", replace=True, rpm=0)
        with open(self.path) as fh:
            self.assertEqual(fh.read(), before)

    def test_bad_expires_is_refused_before_any_write(self):
        with self.assertRaises(K.KeyNameError):
            K.add("x", expires="soon")
        self.assertFalse(os.path.exists(self.path))
        self.assertFalse(os.path.exists(self.path + ".lock"))
        K.add("laptop")
        with open(self.path) as fh:
            before = fh.read()
        for bad in ("soon", "2026-10-01", 5):
            with self.subTest(bad=bad):
                with self.assertRaises(K.KeyNameError):
                    K.update("laptop", expires=bad)
        with open(self.path) as fh:
            self.assertEqual(fh.read(), before)
        K.update("laptop", expires="2030-01-01T00:00:00Z")
        self.assertEqual(K.find(K.load(), "laptop")["expires"], "2030-01-01T00:00:00Z")

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

    def test_a_reserved_name_in_the_store_fails_closed(self):
        _, token = K.add("laptop")
        for reserved in ("master", "host"):
            with self.subTest(reserved=reserved):
                doc = {"version": 1, "keys": [
                    {"name": "laptop", "sha256": K.hash_token(token)},
                    {"name": reserved, "sha256": "a" * 64}]}
                with open(self.path, "w") as fh:
                    json.dump(doc, fh)
                self.bump_mtime()
                with self.assertRaises(K.KeyStoreError):
                    K.load()
                with self.assertRaises(K.KeyStoreError):
                    K.KeyCache().lookup(token)


class TestLifecycle(StoreCase):
    def test_revoke_keeps_the_entry_for_audit(self):
        _, token = K.add("laptop")
        K.revoke("laptop")
        doc = K.load()
        self.assertEqual(len(doc["keys"]), 1)
        self.assertIsNotNone(doc["keys"][0]["revoked"])
        self.assertEqual(K.status(doc["keys"][0]), "revoked")
        self.assertEqual(K.KeyCache().lookup(token), (None, "revoked"))

    def test_revoke_and_update_normalize_the_name(self):
        _, token = K.add("laptop")
        K.update("  LAPTOP ", rpm=7)
        self.assertEqual(K.find(K.load(), "laptop")["rpm"], 7)
        K.revoke("Laptop")
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
