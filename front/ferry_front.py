"""litellm's proxy app with the lane catalogue filtered.

WHY THIS EXISTS

`/v1/models` advertises every real `model_name` in the config, which on the
ferry stack means the fallback deployments as well as the four lanes. That is
not cosmetic. `router_settings.fallbacks` is keyed by model group: `orch` has an
entry, `orch-deepseek` does not. A client that picks a fallback hop out of a
model list gets a single provider with nothing behind it — the exact opposite of
what the `orch` lane exists to provide, and it fails only when that hop is down,
which is the case the chain was built for.

litellm has no config-only fix. `hidden` is honoured for `model_group_alias`
entries ONLY (litellm/router.py, both the model-group-info and model-list
paths); a deployment's `model_info` is never consulted for it, and
`litellm.public_model_groups` sets a flag on `/model_group/info` without
filtering `/v1/models`. Per-key model access would work but needs the DB-backed
virtual-key layer, and ferry serves one shared static key.

WHAT THIS DOES

Wraps litellm's own FastAPI app in pure ASGI middleware. A lane declares itself
with `model_info: {public: true}` in the route config (`model_info` is
`extra: allow`, so litellm accepts and ignores the key). Anything not so marked
is dropped from the catalogue.

The inference path is NOT proxied. For every request whose path is not the model
listing, `__call__` hands scope/receive/send straight to the wrapped app and
returns — no buffering, no response rewriting, nothing between the client and a
streaming token. This is deliberately NOT Starlette's BaseHTTPMiddleware, which
buffers and is a known way to break SSE.

FAIL-OPEN, ALWAYS. A hop visible in the catalogue is a routing wart. A front
door that refuses to answer is an outage. Every failure mode here — an
unreadable config, no lane marked public, a body that is not the JSON we expect
— returns the upstream response untouched.
"""
from __future__ import annotations

import json
import os

MODEL_LIST_PATHS = frozenset({"/v1/models", "/models"})


def _public_lane_names(config_path: str) -> frozenset[str]:
    """Read the lane names a config marks `model_info: {public: true}`.

    Returns an empty set on any problem, which the middleware treats as
    "filter nothing" — see the fail-open note in the module docstring.
    """
    if not config_path or not os.path.exists(config_path):
        return frozenset()
    try:
        import yaml

        with open(config_path) as handle:
            cfg = yaml.safe_load(handle) or {}
        names = set()
        for entry in cfg.get("model_list") or []:
            if not isinstance(entry, dict):
                continue
            info = entry.get("model_info") or {}
            if isinstance(info, dict) and info.get("public") is True:
                name = entry.get("model_name")
                if isinstance(name, str) and name:
                    names.add(name)
        return frozenset(names)
    except Exception:
        return frozenset()


def filter_catalogue(payload: bytes, public: frozenset[str]) -> bytes | None:
    """Drop non-public entries from an OpenAI model-list body.

    Returns None when the body should be passed through unchanged: not JSON,
    not the shape we expect, nothing marked public, or filtering would empty a
    non-empty catalogue (which would look like a dead proxy to every client).
    """
    if not public:
        return None
    try:
        doc = json.loads(payload)
    except Exception:
        return None
    if not isinstance(doc, dict) or not isinstance(doc.get("data"), list):
        return None

    kept = [
        item for item in doc["data"]
        if isinstance(item, dict) and item.get("id") in public
    ]
    if doc["data"] and not kept:
        # Every advertised name is unmarked: almost certainly a config that
        # predates the marker, not a deliberately empty catalogue.
        return None
    if len(kept) == len(doc["data"]):
        return None

    doc["data"] = kept
    return json.dumps(doc).encode()


class LaneCatalogueFilter:
    """ASGI middleware that filters the model listing and nothing else."""

    def __init__(self, app, public: frozenset[str]) -> None:
        self.app = app
        self.public = public

    async def __call__(self, scope, receive, send):
        # The hot path: anything that is not the model listing is handed over
        # untouched. No wrapper around `send`, so a streamed completion is
        # byte-for-byte what litellm produced.
        if (
            not self.public
            or scope.get("type") != "http"
            or scope.get("path") not in MODEL_LIST_PATHS
        ):
            return await self.app(scope, receive, send)

        chunks: list[bytes] = []
        start_message: dict | None = None

        async def capture(message):
            nonlocal start_message
            if message["type"] == "http.response.start":
                start_message = message
                return
            if message["type"] == "http.response.body":
                chunks.append(message.get("body", b""))
                if message.get("more_body"):
                    return
                await self._flush(send, start_message, b"".join(chunks))
                return
            await send(message)

        await self.app(scope, receive, capture)

    async def _flush(self, send, start_message, body: bytes) -> None:
        filtered = filter_catalogue(body, self.public)
        out = body if filtered is None else filtered

        headers = []
        for key, value in (start_message or {}).get("headers", []):
            if key.lower() == b"content-length":
                continue
            headers.append((key, value))
        headers.append((b"content-length", str(len(out)).encode()))

        await send({
            "type": "http.response.start",
            "status": (start_message or {}).get("status", 200),
            "headers": headers,
        })
        await send({"type": "http.response.body", "body": out, "more_body": False})


def build_app():
    """Import litellm's proxy app and wrap it. Used as the uvicorn app factory.

    litellm resolves its own config from CONFIG_FILE_PATH, so importing its app
    here is the same startup the `litellm` CLI performs — this module adds the
    wrapper and nothing else.
    """
    from litellm.proxy.proxy_server import app as litellm_app

    public = _public_lane_names(os.environ.get("CONFIG_FILE_PATH", ""))
    if not public:
        # Nothing declared itself public — behave exactly like plain litellm.
        return litellm_app
    return LaneCatalogueFilter(litellm_app, public)


def main(argv: list[str] | None = None) -> int:
    """Serve litellm's proxy with the catalogue filtered.

    Deliberately accepts the same three flags ferry passes to the `litellm` CLI,
    in the same shape, so `pgrep -f '--port N'` still finds this process and
    `ferry down` keeps working.
    """
    import argparse

    parser = argparse.ArgumentParser(prog="ferry_front.py")
    parser.add_argument("--config", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args(argv)

    os.environ["CONFIG_FILE_PATH"] = args.config

    import uvicorn

    uvicorn.run(
        "ferry_front:build_app",
        factory=True,
        host=args.host,
        port=args.port,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
