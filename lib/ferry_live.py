"""The live view: lanes resolved into hops, and a tail over the event stream.

Two halves, both consumed by `ferry-dash`:

  * `lanes(topology)` turns the parsed config into what the view draws — every
    lane as its primary plus its ordered fallback hops, with a pooled hop
    (several deployments sharing one model_name) marked so it can fan out.
  * `EventTail` follows the NDJSON the front-door tap writes, `sse_frame`
    frames a record for the browser, and `bps_of` derives the per-request
    bytes/s throughput proxy from a finished record.

Standard library only: ferry-dash runs under any python3 and everything it
reaches inherits that.
"""
from __future__ import annotations

import json
import os
import re
import time


# ── topology → what the view draws ─────────────────────────────────────────
def lanes(topology):
    """Every lane as an ordered list of hops.

    A lane's first hop is the lane itself; the rest come from its `fallbacks`
    entry, in order. Any hop may be a POOL — several deployments sharing that
    one model_name, which litellm splits across rather than trying in order.

    A hop naming a backend the config does not define is kept and flagged
    `missing`, never dropped. A chain hop that resolves to nothing is precisely
    the class of bug this view exists to surface, and silently omitting it would
    hide one.
    """
    groups = topology.get("groups") or {}
    fallbacks = topology.get("fallbacks") or {}
    out = []
    for name in topology.get("order") or []:
        hop_names = [name] + list(fallbacks.get(name) or [])
        out.append({
            "name": name,
            "public": bool((groups.get(name) or {}).get("public")),
            "hops": [_hop(h, groups) for h in hop_names],
        })
    return out


def _hop(name, groups):
    g = groups.get(name)
    if not g:
        return {"name": name, "missing": True, "is_pool": False,
                "pool_size": 0, "deployments": []}
    count = int(g.get("count") or 0)
    ids = g.get("ids") or []
    models = g.get("models") or []
    providers = g.get("providers") or []
    deployments = []
    for i in range(count):
        deployments.append({
            "id": ids[i] if i < len(ids) else "",
            "model": models[i] if i < len(models) else "",
            "provider": providers[i] if i < len(providers) else "",
        })
    return {
        "name": name,
        "missing": False,
        "is_pool": count > 1,
        "pool_size": count,
        "deployments": deployments,
    }


def chains(topology):
    """lane name -> the ordered deployment ids of its hops.

    This is what maps an event's `hop_errors` back to the deployments that
    produced them, and it lives here rather than in either consumer because
    `ferry-dash` draws from it and `observ/ferry-metrics-exporter` counts
    fallback edges from it. Two derivations that drifted apart would attribute
    a failure to a healthy backend, which is worse than attributing nothing.

    A hop with no configured `model_info.id` contributes an EMPTY slot, so
    attribution stops there instead of shifting every later hop onto the wrong
    backend. A pool hop contributes only its first id: litellm splits a pool
    across its members rather than trying them in order, so there is no "next
    member" a hop error could belong to.
    """
    out = {}
    for lane in lanes(topology):
        ids = []
        for hop in lane["hops"]:
            deployments = hop.get("deployments") or []
            ids.append(deployments[0]["id"] if deployments else "")
        out[lane["name"]] = ids
    return out


# ── the event stream ───────────────────────────────────────────────────────
def sse_frame(record):
    """One server-sent-events frame.

    `json.dumps` escapes newlines, which matters more than it looks: a raw
    newline inside the payload would split one event into two frames and
    desynchronise the client for the rest of the stream.
    """
    return b"data: " + json.dumps(record, separators=(",", ":")).encode() + b"\n\n"


def bps_of(record):
    """Bytes per second for one request, or None when it cannot be known.

    resp_bytes over duration_ms — a throughput PROXY expressed in bytes, never
    a claim about tokens. A missing count, a missing or non-numeric duration,
    or a non-positive either yields None, and the view treats None as "no
    data" rather than 0: an old event file or an untapped proxy must not
    render as a relay that moved no bytes.
    """
    try:
        b = record.get("resp_bytes")
        ms = record.get("duration_ms")
        if not isinstance(b, (int, float)) or not isinstance(ms, (int, float)):
            return None
        if b <= 0 or ms <= 0:
            return None
        return b * 1000.0 / ms
    except Exception:
        return None


DEFAULT_RULES_PATH = os.path.expanduser("~/.config/ferry/event-rules.json")

# Every state except `healthy` and `unknown` is a claim about a specific
# provider's behaviour. `unknown` exists so an unrecognised failure is never
# silently reported as healthy, which is how a real outage hides behind a green
# dashboard.
STATES = ("healthy", "rate_limited", "quota_exhausted", "auth_dead",
          "unreachable", "unknown")

# A quota does not clear on the decay that suits a rate limit — a weekly limit
# outlives any timer worth setting — so quota_exhausted and auth_dead are
# STICKY: only a success on that same deployment clears them.
STICKY = ("quota_exhausted", "auth_dead")


def load_rules(path=None):
    """Read the classifier table from host config.

    The table is DATA, not code, for two reasons. Adding a provider should not
    mean editing this repo; and this repo is public, so a vendor's name, its
    error wording and its plan terms belong in the operator's own gitignored
    config. A missing or corrupt file yields no rules — every failure then
    classifies as `unknown`, which is visible, rather than as healthy.
    """
    path = path or DEFAULT_RULES_PATH
    empty = {"version": 0, "rules": [], "ttl": {}}
    try:
        with open(path) as handle:
            loaded = json.load(handle)
        if not isinstance(loaded, dict):
            return empty
        rules = [r for r in (loaded.get("rules") or []) if isinstance(r, dict)]
        ttl = loaded.get("ttl") if isinstance(loaded.get("ttl"), dict) else {}
        return {"version": loaded.get("version", 0), "rules": rules, "ttl": ttl}
    except Exception:
        return empty


def classify(rules, code, err_type, message):
    """First matching rule wins; no match is `unknown`, never `healthy`."""
    code_s = str(code or "").strip()
    text = (message or "").lower()
    typ = (err_type or "")
    for rule in rules.get("rules") or []:
        wanted = [str(s) for s in (rule.get("status") or [])]
        if wanted and code_s not in wanted:
            continue
        needles = rule.get("message_contains") or []
        if needles and not any(str(n).lower() in text for n in needles):
            continue
        types = rule.get("type_contains") or []
        if types and not any(str(t) in typ for t in types):
            continue
        if not wanted and not needles and not types:
            continue                    # a rule matching everything is a bug
        state = rule.get("state")
        if state in STATES:
            return state
    return "unknown"


# ── the proxy-log tap: one raw log line -> a backend event kind ────────────
# `ferry-dash`'s Activity tailer and observ/ferry-metrics-exporter both read
# the proxy's text log and surface "the last backend event". Until 2026-09-04
# each carried its own copy of a vendor-specific test (Kimi's
# `permission_error` + `usage limit` -> "kimi_quota"), which missed every
# other provider's way of saying the same thing. The kinds are now two of the
# classifier STATES above, the test lives here once, and the vendor wording
# comes from the operator's event-rules.json exactly as it does for the
# per-deployment view. A raw line is not a structured event, so the status
# and exception class are recovered from litellm's own log shapes
# (`'status_code': '401'`, `Error code: 429`, `litellm.RateLimitError`) and
# a rule keyed on either still applies. The built-in floor below is
# vendor-neutral: litellm's exception class name and two error CODES
# providers emit as identifiers rather than prose.
TAP_KINDS = ("quota_exhausted", "rate_limited")

_TAP_STATUS = re.compile(
    r"(?:status_code\W{1,4}|error code:\s*|HTTP/1\.[01]\"\s)(\d{3})\b", re.I)
_TAP_TYPE = re.compile(r"litellm\.([A-Za-z]+Error)")


def classify_log_line(rules, line):
    """Return "quota_exhausted", "rate_limited" or None for one log line."""
    if not line:
        return None
    low = line.lower()
    m = _TAP_STATUS.search(line)
    code = m.group(1) if m else None
    t = _TAP_TYPE.search(line)
    err_type = t.group(1) if t else ""
    state = classify(rules or {"rules": []}, code, err_type, line)
    if state in TAP_KINDS:
        return state
    if state != "unknown":
        return None                 # a rule spoke (healthy/auth_dead/...): trust it
    if "insufficient_quota" in low or "insufficient credits" in low:
        return "quota_exhausted"
    if "ratelimiterror" in low or "rate_limit" in low:
        return "rate_limited"
    return None


class ExhaustionState:
    """Per-deployment health, derived from the event stream.

    Per DEPLOYMENT is the point. `ferry-dash` kept a single `last_event`, so any
    new backend event overwrote a still-live outage on a different backend; one
    lane's transient 429 could erase another lane's exhausted quota from the
    display entirely.
    """

    def __init__(self, rules, now=None):
        self.rules = rules if isinstance(rules, dict) else {"rules": [], "ttl": {}}
        self._now = now or time.time
        self._state = {}

    def observe(self, record, chain=None):
        """Fold one event in.

        The event's `deployment` is the one that SUCCEEDED. The entries in
        `hop_errors` belong to the hops tried BEFORE it, in order, so `chain`
        maps them back to deployments.

        NOTE, stated as an assumption rather than a fact: that hop_errors[i]
        corresponds to chain[i] is taken from the order litellm appends them and
        has not been traced end to end. Without a `chain` the failures are
        counted but not attributed, which is the safe direction — a wrong
        attribution would blame a healthy backend.
        """
        if not isinstance(record, dict):
            return
        now = self._now()

        for index, hop in enumerate(record.get("hop_errors") or []):
            if not isinstance(hop, dict):
                continue
            if not chain or index >= len(chain):
                continue
            state = classify(self.rules, hop.get("code"), hop.get("type"),
                             hop.get("message"))
            self._set(chain[index], state, now, hop.get("message") or "",
                      str(hop.get("code") or ""))

        served = record.get("deployment")
        if not served:
            return
        status = int(record.get("status") or 0)
        if 200 <= status < 300:
            self._set(served, "healthy", now, "", "")
        else:
            state = classify(self.rules, status, "", "")
            self._set(served, state, now, "", str(status))

    def _set(self, deployment, state, now, detail, code):
        prev = self._state.get(deployment)
        if prev and prev["state"] == state:
            prev["last"] = now          # `since` stays the first moment
            if detail:
                prev["detail"] = detail
            return
        self._state[deployment] = {"state": state, "since": now, "last": now,
                                   "detail": detail, "code": code}

    def snapshot(self):
        """Current state per deployment, with non-sticky states decayed.

        A deployment never seen is ABSENT rather than assumed healthy: this
        knows only what traffic has shown it.
        """
        now = self._now()
        ttl = self.rules.get("ttl") or {}
        out = {}
        for deployment, entry in self._state.items():
            state = entry["state"]
            if state not in STICKY and state != "healthy":
                window = ttl.get(state)
                if window and (now - entry["last"]) > float(window):
                    state = "healthy"
            out[deployment] = {
                "state": state,
                "since": entry["since"] if state == entry["state"] else now,
                "detail": entry["detail"] if state == entry["state"] else "",
                "code": entry["code"] if state == entry["state"] else "",
            }
        return out


class EventTail:
    """Follow the tap's NDJSON from EOF.

    Rotation, truncation and partial lines are handled the way
    observ/ferry-log-shipper's Tailer handles them, for the same reason: the
    writer unlinks and recreates rather than truncating, so identity is
    (st_dev, st_ino) and a changed inode means re-read from zero.
    """

    MAX_PARTIAL = 262144

    def __init__(self, path, from_start=False):
        self.path = path
        self._partial = ""
        self._offset = 0
        self._ident = None
        if not from_start:
            try:
                st = os.stat(self.path)
                self._offset = st.st_size
                self._ident = (st.st_dev, st.st_ino)
            except OSError:
                pass

    def read_new(self):
        """Every complete record appended since the last call. Never raises."""
        try:
            st = os.stat(self.path)
        except OSError:
            return []

        ident = (st.st_dev, st.st_ino)
        if self._ident is not None and ident != self._ident:
            # Rotated: a different file wears the name now.
            self._offset = 0
            self._partial = ""
        elif st.st_size < self._offset:
            # Truncated in place.
            self._offset = 0
            self._partial = ""
        self._ident = ident

        if st.st_size == self._offset:
            return []

        try:
            with open(self.path, "r", errors="replace") as handle:
                handle.seek(self._offset)
                chunk = handle.read()
                self._offset = handle.tell()
        except OSError:
            return []

        text = self._partial + chunk
        self._partial = ""
        lines = text.split("\n")
        if not text.endswith("\n"):
            self._partial = lines.pop()
            if len(self._partial) > self.MAX_PARTIAL:
                self._partial = ""      # a line this long is not a record
        else:
            lines.pop()                 # trailing empty piece after the last \n

        out = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except Exception:
                continue                # a corrupt line is skipped, not fatal
            if isinstance(record, dict):
                out.append(record)
        return out
