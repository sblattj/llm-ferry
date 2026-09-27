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


def _check_expires(expires) -> None:
    """A non-None expiry must be an ISO instant iso() would write."""
    if expires is None:
        return
    try:
        parse_iso(expires)
    except (TypeError, ValueError):
        raise KeyNameError("expires must be null or YYYY-MM-DDTHH:MM:SSZ, got %r"
                           % (expires,))


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
    created, revoked cleared) so the old key dies on the next request. Every
    non-None limit argument (expires, lanes, rpm, budget_tokens) overrides the
    kept value; a kept expiry already in the past is cleared, so a rotation
    never mints a key that is dead on arrival. `unique` takes the first free
    `<name>-N`; neither raises."""
    path = path or keys_path()
    base = normalize_name(name)
    _check_limits(lanes, rpm, budget_tokens)
    _check_expires(expires)
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
            given = {"expires": expires, "lanes": lanes, "rpm": rpm,
                     "budget_tokens": budget_tokens}
            entry.update({k: v for k, v in given.items() if v is not None})
            if expires is None and status(entry, now) == "expired":
                entry["expires"] = None
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
    name = normalize_name(name)
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
    name = normalize_name(name)
    if "expires" in changes:
        _check_expires(changes["expires"])
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
