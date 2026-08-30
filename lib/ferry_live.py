"""The live view: lanes resolved into hops, and a tail over the event stream.

Two halves, both consumed by `ferry-dash`:

  * `lanes(topology)` turns the parsed config into what the view draws — every
    lane as its primary plus its ordered fallback hops, with a pooled hop
    (several deployments sharing one model_name) marked so it can fan out.
  * `EventTail` follows the NDJSON the front-door tap writes, and `sse_frame`
    frames a record for the browser.

Standard library only: ferry-dash runs under any python3 and everything it
reaches inherits that.
"""
from __future__ import annotations

import json
import os


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


# ── the event stream ───────────────────────────────────────────────────────
def sse_frame(record):
    """One server-sent-events frame.

    `json.dumps` escapes newlines, which matters more than it looks: a raw
    newline inside the payload would split one event into two frames and
    desynchronise the client for the rest of the stream.
    """
    return b"data: " + json.dumps(record, separators=(",", ":")).encode() + b"\n\n"


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
