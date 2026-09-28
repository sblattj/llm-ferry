#!/usr/bin/env python3
"""Guard: a device key reaches ONLY litellm's client endpoints (front/ferry_front.py).

Run:  python3 lib/ferry-front-routes.test.py

A valid `fk-` key is swapped for the master before litellm sees it, so the
front's route allowlist is the only thing between a device and litellm's admin
surface. A path-only or prefix rule is not enough: litellm has routes whose
FIRST segment is a parameter (`/{provider}/v1/files*`, `/{provider}/v1/batches*`,
`/{mcp_server_name}/{mcp,token,register,authorize}`), so a client-looking prefix
can land on a file, batch or MCP OAuth handler at master power.

This test loads litellm's REAL route table (all LazyFeatureMiddleware features
force-loaded, FastAPI's _IncludedRouter nesting flattened), fills every route's
path params with adversarial values, tries every method plus the websocket
upgrade, drives each through the real LaneCatalogueFilter with a real minted
device key, and resolves every ADMITTED request through litellm's own router:

  (a) every admitted (method, path) resolves to an endpoint in CLIENT_ENDPOINTS;
  (b) every endpoint in CLIENT_ENDPOINTS is still reachable by a device key.

litellm is not importable under the system python, so this file re-execs itself
under ferry's litellm venv (LITELLM_PYTHON below) when that exists, and skips
LOUDLY only when it does not. No port is bound; HOME, FERRY_KEYS_FILE and
FERRY_KEYS_DB are all temp paths.
"""
import os
import sys

LITELLM_PYTHON = os.path.expanduser("~/.local/share/uv/tools/litellm/bin/python")
_REEXEC_FLAG = "FERRY_ROUTES_TEST_REEXEC"

import importlib.util  # noqa: E402

# find_spec, not import: litellm is only imported once HOME and the env below
# are temp/offline (LITELLM_LOCAL_MODEL_COST_MAP stops the import fetching the
# remote cost map).
HAVE_LITELLM = importlib.util.find_spec("litellm") is not None
if not HAVE_LITELLM:
    if os.path.exists(LITELLM_PYTHON) and not os.environ.get(_REEXEC_FLAG):
        env = dict(os.environ)
        env[_REEXEC_FLAG] = "1"
        os.execve(LITELLM_PYTHON, [LITELLM_PYTHON, os.path.abspath(__file__)]
                  + sys.argv[1:], env)

import asyncio  # noqa: E402
import itertools  # noqa: E402
import re  # noqa: E402
import shutil  # noqa: E402
import tempfile  # noqa: E402
import unittest  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "front"))

MASTER = "sk-routes-test-master"
TMP = tempfile.mkdtemp(prefix="ferry-front-routes-")
os.environ.update({
    "HOME": TMP,
    "FERRY_KEYS_FILE": os.path.join(TMP, "keys.json"),
    "FERRY_KEYS_DB": os.path.join(TMP, "keys-usage.sqlite"),
    "LITELLM_MASTER_KEY": MASTER, "FERRY_EVENTS": "0", "FERRY_STRIP_HEADERS": "0",
    "LITELLM_LOG": "ERROR", "LITELLM_LOCAL_MODEL_COST_MAP": "True",
})

import ferry_front as FF  # noqa: E402
import ferry_keys as K  # noqa: E402

# The litellm endpoints a ferry client calls, by FastAPI endpoint name. Adding
# one here is a policy decision: it hands a device key that endpoint at master
# power. Each maps to one canonical client request that must stay admitted.
CLIENT_ENDPOINTS = {
    "model_list": [("GET", "/v1/models"), ("GET", "/models")],
    "model_info": [("GET", "/v1/models/flash")],
    "chat_completion": [("POST", "/v1/chat/completions"), ("POST", "/chat/completions")],
    "completion": [("POST", "/v1/completions"), ("POST", "/completions")],
    "embeddings": [("POST", "/v1/embeddings"), ("POST", "/embeddings")],
    "anthropic_response": [("POST", "/v1/messages")],
    "count_tokens": [("POST", "/v1/messages/count_tokens")],
    "responses_api": [("POST", "/v1/responses"), ("POST", "/responses")],
    "get_response": [("GET", "/v1/responses/resp_1")],
    "delete_response": [("DELETE", "/v1/responses/resp_1")],
    "get_response_input_items": [("GET", "/v1/responses/resp_1/input_items")],
    "compact_response": [("POST", "/v1/responses/compact")],
    "cancel_response": [("POST", "/v1/responses/resp_1/cancel")],
    "google_generate_content": [("POST", "/v1beta/models/flash:generateContent")],
    "google_stream_generate_content": [("POST", "/v1beta/models/flash:streamGenerateContent")],
    "google_count_tokens": [("POST", "/v1beta/models/flash:countTokens")],
    "health_liveliness": [("GET", "/health/liveliness"), ("GET", "/health/liveness")],
    "realtime_websocket_endpoint": [("WEBSOCKET", "/v1/realtime"), ("WEBSOCKET", "/realtime")],
    "responses_websocket_endpoint": [("WEBSOCKET", "/v1/responses"), ("WEBSOCKET", "/responses")],
}
# Answered by the front itself; litellm must have no route there.
FRONT_OWNED = [("GET", FF.FLEET_PATH), ("POST", FF.FLEET_PATH)]

METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "WEBSOCKET")
# Adversarial fills: the first segments of admitted client routes, litellm's
# MCP OAuth verbs, provider-style and path-style values, and Gemini actions.
FILLS = ("x", "flash", "models", "responses", "messages", "v1", "v1beta", "chat",
         "completions", "embeddings", "realtime", "health", "compact", "input_items",
         "cancel", "count_tokens", "mcp", "token", "register", "authorize", "files",
         "batches", "v1/files", "v1/batches", "models/x", "a/b", "x:generateContent",
         "x:streamGenerateContent", "x:countTokens", "v1/files/x:generateContent")
PARAM = re.compile(r"\{[^}]+\}")


class _Recorder:
    def __init__(self):
        self.calls = 0

    async def __call__(self, scope, receive, send):
        self.calls += 1
        if scope["type"] == "websocket":
            return await send({"type": "websocket.accept"})
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": b"{}", "more_body": False})


def _load_litellm():
    """litellm's app with every lazy feature registered; returns (app, flat routes)."""
    from fastapi import routing as fr
    from litellm.proxy import _lazy_features as LZ
    from litellm.proxy.proxy_server import app

    async def load_all():
        failed = []
        for feat in LZ.LAZY_FEATURES:
            try:
                await LZ._force_load(app, feat)
            except Exception as err:  # noqa: BLE001 - reported below
                failed.append((getattr(feat, "module_path", feat), repr(err)))
        return failed

    failed = asyncio.run(load_all())
    flat = []
    for route, ctx in fr._iter_routes_with_context(app.router.routes):
        sr = ctx.starlette_route if ctx is not None and ctx.starlette_route is not None else route
        path = getattr(sr, "path", None) or (ctx.path if ctx is not None else None)
        methods = getattr(sr, "methods", None) or getattr(route, "methods", None) or ()
        kind = type(route).__name__
        flat.append((kind, path, tuple(sorted(methods)), getattr(route, "name", None)))
    return app, flat, len(LZ.LAZY_FEATURES), failed


def _resolve(app, method, path):
    """The endpoint name litellm's router would DISPATCH (method, path) to, or None.

    Mirrors starlette.routing.Router.app: the first Match.FULL wins, descending
    FastAPI 0.141's _IncludedRouter via its own _match. A PARTIAL (method
    mismatch, a 405) or no match dispatches nothing."""
    from fastapi import routing as fr
    from starlette.routing import Match

    typ = "websocket" if method == "WEBSOCKET" else "http"
    scope = {"type": typ, "path": path, "root_path": "", "headers": [],
             "query_string": b"", "app": app}
    if typ == "http":
        scope["method"] = method

    def descend(route, sc):
        while isinstance(route, fr._IncludedRouter):
            _, child, inner, ctx = route._match(sc)
            sc = dict(sc)
            sc.update(child)
            if ctx is not None and not isinstance(inner, fr._IncludedRouter):
                return getattr(getattr(ctx, "original_route", inner), "name", None)
            route = inner
        return getattr(route, "name", None) or type(route).__name__

    for route in app.router.routes:
        match, child = route.matches(scope)
        if match == Match.FULL:
            sc = dict(scope)
            sc.update(child)
            return descend(route, sc)
    return None


@unittest.skipUnless(HAVE_LITELLM, "SKIPPED LOUDLY: litellm is not importable and "
                     "%s does not exist, so the device-key route guard did NOT run"
                     % LITELLM_PYTHON)
class TestDeviceKeyRoutesAgainstLitellm(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.stderr.write("route guard: running under %s (litellm %s)\n"
                         % (sys.executable, _litellm_version()))
        cls.app, cls.flat, cls.n_lazy, cls.failed = _load_litellm()
        FF._KEY_CACHE = None
        cls.token = K.add("guard")[1]

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(TMP, True)

    def admitted(self, requests):
        """The subset of (method, path) the real front hands to litellm."""
        rec = _Recorder()
        mw = FF.LaneCatalogueFilter(rec, frozenset())
        out = []

        async def one(method, path):
            typ = "websocket" if method == "WEBSOCKET" else "http"
            scope = {"type": typ, "path": path, "raw_path": path.encode(),
                     "root_path": "", "method": "GET" if typ == "websocket" else method,
                     "client": ("100.64.0.9", 1), "query_string": b"",
                     "headers": [(b"authorization", ("Bearer " + self.token).encode())]}

            async def receive():
                return {"type": "http.request", "body": b"{}", "more_body": False}

            async def send(message):
                pass

            before = rec.calls
            await mw(scope, receive, send)
            return rec.calls > before

        async def sweep():
            for method, path in requests:
                if await one(method, path):
                    out.append((method, path))

        asyncio.run(sweep())
        return out

    def samples(self):
        seen = set()
        for _, template, _, _ in self.flat:
            if not template:
                continue
            params = PARAM.findall(template)
            fills = itertools.product(FILLS, repeat=len(params)) if params else [()]
            for combo in fills:
                path = template
                for prm, val in zip(params, combo):
                    path = path.replace(prm, val, 1)
                seen.add(path)
        # The client shapes themselves, with the same adversarial ids.
        for reqs in CLIENT_ENDPOINTS.values():
            for _, path in reqs:
                seen.add(path)
                for val in FILLS:
                    seen.add(re.sub(r"(flash|resp_1)", lambda _m: val, path))
        return sorted(seen)

    def test_litellm_table_is_complete(self):
        self.assertEqual(self.failed, [], "lazy features failed to load")
        self.assertGreaterEqual(self.n_lazy, 1)
        names = {name for _, _, _, name in self.flat}
        for name in ("anthropic_response", "realtime_websocket_endpoint",
                     "create_batch", "token_endpoint", "dynamic_mcp_route"):
            self.assertIn(name, names, "lazy route %s missing from the table" % name)

    def test_every_admitted_request_resolves_to_a_client_endpoint(self):
        requests = [(m, p) for p in self.samples() for m in METHODS]
        admitted = self.admitted(requests)
        front_owned = set(FRONT_OWNED)
        bad = []
        for method, path in admitted:
            got = _resolve(self.app, method, path)
            if (method, path) in front_owned:
                if got is not None:
                    bad.append((method, path, got))
                continue
            if got not in CLIENT_ENDPOINTS:
                bad.append((method, path, got))
        sys.stderr.write("route guard: %d/%d lazy features loaded, %d routes, "
                         "%d requests tried, %d admitted\n"
                         % (self.n_lazy - len(self.failed), self.n_lazy, len(self.flat),
                            len(requests), len(admitted)))
        # Dispatched to a real non-client handler first; 404/405s after.
        bad.sort(key=lambda b: (b[2] is None, b))
        self.assertEqual(len(bad), 0, "device key admitted to %d non-client requests "
                         "(%d reach a litellm handler), first 40:\n%s" % (
                             len(bad), sum(b[2] is not None for b in bad),
                             "\n".join("  %s %s -> %s" % b for b in bad[:40])))

    def test_every_client_endpoint_stays_reachable(self):
        for name, reqs in CLIENT_ENDPOINTS.items():
            for method, path in reqs:
                with self.subTest(endpoint=name, method=method, path=path):
                    self.assertEqual(_resolve(self.app, method, path), name)
                    self.assertEqual(self.admitted([(method, path)]), [(method, path)])

    def test_front_owned_routes_are_admitted_and_unknown_to_litellm(self):
        for method, path in FRONT_OWNED:
            with self.subTest(method=method):
                self.assertIsNone(_resolve(self.app, method, path))
                self.assertTrue(FF.device_key_route_allowed(method, path))


def _litellm_version():
    try:
        from importlib.metadata import version
        return version("litellm")
    except Exception:  # noqa: BLE001
        return "unknown"


if __name__ == "__main__":
    if not HAVE_LITELLM:
        sys.stderr.write("\n*** route guard SKIPPED: litellm is not importable under %s "
                         "(%s %s) ***\n\n" % (
                             sys.executable, LITELLM_PYTHON,
                             "exists but was already tried" if os.environ.get(_REEXEC_FLAG)
                             else "is absent"))
    unittest.main()
