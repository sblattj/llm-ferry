"""Request-side cloaking + litellm seam for the Claude Pro/Max subscription path.

WHAT THIS IS

Path A of the Claude subscription lane: make an outbound anthropic
/v1/messages request indistinguishable from the official Claude Code CLI, then
carry the OAuth Bearer token from front/ferry_claude_oauth.py (the token
engine, owned by a sibling) on every such request.

Two halves:

  PART 1 - pure cloaking functions (build_headers, build_anthropic_beta,
  build_upstream_url, apply_cloaking). Constants mirror a verified reference
  (AmazingAng/auth2api) for Claude CLI 2.1.88. No I/O, no litellm import, so
  lib/ferry-claude-provider.test.py exercises all of it without network and
  without the sibling present.

  PART 2 - ClaudeOAuthAnthropicConfig, the litellm integration. litellm
  1.99.0 has NO public custom-provider registration hook: its own `chatgpt`
  subscription provider is wired by a hardcoded branch in
  litellm_core_utils/get_llm_provider_logic.py (custom_llm_provider ==
  "chatgpt") plus a slot in constants.py's provider list — edits to which
  require patching the installed package. Mirroring the chatgpt provider's
  SHAPE (config object that resolves a dynamic api_base/api_key per call, see
  litellm/llms/chatgpt/chat/transformation.py) but not subclassing its
  registration, this module instead exposes the stable seam

      get_request_options(model, body, token_path) ->
          (url, headers, cloaked_body)

  which the ferry_front monkeypatch layer (sibling work) calls to rewrite an
  outgoing anthropic request in one shot. Deep litellm subclassing would buy
  nothing here and couple us to unversioned internals.

The token engine is imported LAZILY inside functions (front/ferry_claude_oauth
may not exist yet); tests inject a stub module into sys.modules so this file
is testable standalone. Contract assumed:
  ensure_valid_token(path) -> TokenData; TokenData.access_token: str;
  TokenData.is_expired() -> bool.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import uuid

# --- Cloaking constants (verified against AmazingAng/auth2api, CLI 2.1.88) ---

FINGERPRINT_SALT = "59cf53e54c78"
DEFAULT_CLI_VERSION = "2.1.88"
DEFAULT_ENTRYPOINT = "cli"
ANTHROPIC_API_BASE = "https://api.anthropic.com"
ANTHROPIC_VERSION = "2023-06-01"

STAINLESS_LANG = "js"
STAINLESS_PACKAGE_VERSION = "0.74.0"
STAINLESS_RUNTIME = "node"
STAINLESS_RUNTIME_VERSION = "v22.13.0"

# anthropic-beta feature flags. Non-haiku models get the full list with
# claude-code first; haiku models drop advanced-tool-use/effort and move
# claude-code to the END.
BETA_OAUTH = "oauth-2025-04-20"
BETA_BASE_NON_HAIKU = [
    "claude-code-20250219",
    BETA_OAUTH,
    "interleaved-thinking-2025-05-14",
    "redact-thinking-2026-02-12",
    "context-management-2025-06-27",
    "prompt-caching-scope-2026-01-05",
    "advanced-tool-use-2025-11-20",
    "effort-2025-11-24",
]
BETA_BASE_HAIKU = [b for b in BETA_BASE_NON_HAIKU
                   if b not in ("advanced-tool-use-2025-11-20", "effort-2025-11-24")
                   and b != "claude-code-20250219"] + ["claude-code-20250219"]
BETA_STRUCTURED_OUTPUTS = "structured-outputs-2025-12-15"

# CLI prefix system block. cache_control ephemeral so the prefix (which is
# identical across every request this lane sends) hits the server cache.
CLI_PREFIX_TEXT = "You are Claude Code, Anthropic's official CLI for Claude."

# Token file resolution for the litellm seam: explicit arg > FERRY env
# (ferry_front naming convention) > claude-code-style default.
TOKEN_PATH_ENV = "FERRY_CLAUDE_TOKEN_PATH"
DEFAULT_TOKEN_PATH = os.path.expanduser("~/.config/ferry/claude/auth.json")

# Session-scoped UUID: Anthropic ties telemetry to a session id, so every
# request from this process reuses one value. Reset only in tests.
_session_id_cache: str | None = None


def get_session_id() -> str:
    """Return this process's Claude Code session UUID, generating on first use."""
    global _session_id_cache
    if _session_id_cache is None:
        _session_id_cache = str(uuid.uuid4())
    return _session_id_cache


def reset_session_id() -> str:
    """Drop the cached session UUID and return a fresh one (test hook)."""
    global _session_id_cache
    _session_id_cache = str(uuid.uuid4())
    return _session_id_cache


def _stainless_arch(machine: str | None = None) -> str:
    """Map platform.machine() to the arch token the Stainless SDK emits."""
    machine = (machine or platform.machine() or "").lower()
    return {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64",
            "arm64": "arm64", "i386": "x86", "i686": "x86"}.get(machine, machine or "unknown")


def _stainless_os(system: str | None = None) -> str:
    """Map platform.system() to the OS token the Stainless SDK emits."""
    system = system or platform.system() or ""
    return {"Darwin": "MacOS", "Linux": "Linux", "Windows": "Windows"}.get(system, system or "Unknown")


def build_anthropic_beta(model: str, *, client_beta: str | None = None,
                         structured: bool = False) -> str:
    """Compute the anthropic-beta header value for a model.

    Haiku models (no advanced-tool-use/effort, claude-code flag last) vs the
    rest. `structured` appends structured-outputs. `client_beta` (a header
    value arriving from a downstream client) is merged in — new entries keep
    client order — and oauth-2025-04-20 is FORCE-PREPENDED so the subscription
    grant is always the first flag Anthropic evaluates.
    """
    flags = list(BETA_BASE_HAIKU if "haiku" in (model or "").lower() else BETA_BASE_NON_HAIKU)
    if structured:
        flags.append(BETA_STRUCTURED_OUTPUTS)
    if client_beta:
        for entry in client_beta.split(","):
            entry = entry.strip()
            if entry and entry not in flags:
                flags.append(entry)
        flags = [f for f in flags if f != BETA_OAUTH]
        flags.insert(0, BETA_OAUTH)
    return ",".join(flags)


def build_headers(access_token: str, *, streaming: bool,
                  cli_version: str = DEFAULT_CLI_VERSION,
                  entrypoint: str = DEFAULT_ENTRYPOINT,
                  timeout_s: float = 600,
                  model: str | None = None) -> dict:
    """Return the literal Claude Code CLI header set.

    Casing is deliberate — the Stainless/x-stainless and Anthropic headers
    ship in exactly these spellings, and the lowercase group is lowercase by
    design. X-Claude-Code-Session-Id reuses the session-scoped UUID;
    x-client-request-id is fresh per call. `model` (optional; None means
    non-haiku) only picks the anthropic-beta family.
    """
    return {
        "Authorization": f"Bearer {access_token}",
        "User-Agent": f"claude-cli/{cli_version} (external, {entrypoint})",
        "X-Claude-Code-Session-Id": get_session_id(),
        "X-Stainless-Lang": STAINLESS_LANG,
        "X-Stainless-Package-Version": STAINLESS_PACKAGE_VERSION,
        "X-Stainless-Runtime": STAINLESS_RUNTIME,
        "X-Stainless-Runtime-Version": STAINLESS_RUNTIME_VERSION,
        "X-Stainless-Arch": _stainless_arch(),
        "X-Stainless-Os": _stainless_os(),
        "X-Stainless-Timeout": str(math.ceil(timeout_s)),
        "X-Stainless-Retry-Count": "0",
        "Accept": "text/event-stream" if streaming else "application/json",
        "anthropic-dangerous-direct-browser-access": "true",
        "anthropic-version": ANTHROPIC_VERSION,
        "x-app": "cli",
        "x-client-request-id": str(uuid.uuid4()),
        "anthropic-beta": build_anthropic_beta(model or ""),
    }


def build_upstream_url(*, count_tokens: bool = False) -> str:
    """The upstream endpoint. ?beta=true is REQUIRED for OAuth traffic."""
    path = "/v1/messages/count_tokens" if count_tokens else "/v1/messages"
    return f"{ANTHROPIC_API_BASE}{path}?beta=true"


def _first_user_text(body: dict) -> str:
    """Text of the first user message; content may be a str or block list."""
    for message in body.get("messages") or []:
        if isinstance(message, dict) and message.get("role") == "user":
            content = message.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                return "".join(
                    block.get("text", "") for block in content
                    if isinstance(block, dict))
            return ""
    return ""


def billing_fingerprint(message_text: str, cli_version: str) -> str:
    """First 3 hex chars of SHA256(salt + msg[4]+msg[7]+msg[20] + version).

    Out-of-range indices contribute "0" each, so any message shorter than 21
    chars still yields a stable fingerprint.
    """
    chars = "".join(
        message_text[i] if 0 <= i < len(message_text) else "0"
        for i in (4, 7, 20))
    digest = hashlib.sha256(
        (FINGERPRINT_SALT + chars + cli_version).encode()).hexdigest()
    return digest[:3]


def _billing_block_text(body: dict, cli_version: str, entrypoint: str) -> str:
    fp = billing_fingerprint(_first_user_text(body), cli_version)
    return (f"x-anthropic-billing-header: "
            f"cc_version={cli_version}.{fp}; cc_entrypoint={entrypoint};")


def apply_cloaking(body: dict, *, cli_version: str = DEFAULT_CLI_VERSION,
                   entrypoint: str = DEFAULT_ENTRYPOINT,
                   session_id: str | None = None,
                   account_uuid: str | None = None,
                   device_id: str | None = None) -> dict:
    """Mutate `body` in place into a Claude Code CLI request and return it.

    system -> [billing block, CLI prefix block (ephemeral cache_control),
    ...existing user system blocks]. A string system is wrapped as one text
    block; a list is preserved verbatim after the two injected blocks.
    metadata.user_id -> JSON string of device/account/session ids, generating
    UUIDs for any not supplied (session defaults to the shared session UUID).
    """
    existing = body.get("system")
    if existing is None:
        existing_blocks: list = []
    elif isinstance(existing, str):
        existing_blocks = [{"type": "text", "text": existing}]
    else:
        existing_blocks = list(existing)
    body["system"] = [
        {"type": "text",
         "text": _billing_block_text(body, cli_version, entrypoint)},
        {"type": "text", "text": CLI_PREFIX_TEXT,
         "cache_control": {"type": "ephemeral"}},
        *existing_blocks,
    ]
    body.setdefault("metadata", {})["user_id"] = json.dumps({
        "device_id": device_id or str(uuid.uuid4()),
        "account_uuid": account_uuid or str(uuid.uuid4()),
        "session_id": session_id or get_session_id(),
    })
    return body


def _load_oauth_engine():
    """Lazily import the sibling token engine.

    front/ferry_front.py runs with front/ itself on sys.path (script launch),
    so the plain sibling import is tried first; the package-qualified import
    covers repo-root CWDs. Tests pre-seed sys.modules["ferry_claude_oauth"]
    with a stub, which the first branch always finds.
    """
    try:
        import ferry_claude_oauth
        return ferry_claude_oauth
    except ImportError:
        from front import ferry_claude_oauth
        return ferry_claude_oauth


class ClaudeOAuthAnthropicConfig:
    """litellm-side config for the Claude subscription path.

    Shaped after litellm's chatgpt subscription provider
    (litellm/llms/chatgpt/chat/transformation.py): a config object that
    resolves, per call, the dynamic credentials an OAuth subscription needs.
    Because litellm 1.99.0 hardcodes provider registration inside its own
    package, the supported integration point here is NOT litellm routing but
    the get_request_options() seam below, for the ferry_front monkeypatch
    layer to call when rewriting outgoing anthropic requests.
    """

    def __init__(self, token_path: str | None = None) -> None:
        self.token_path = token_path

    def resolve_token_path(self, token_path: str | None = None) -> str:
        """Explicit arg > FERRY_CLAUDE_TOKEN_PATH > default credentials file."""
        return (token_path or self.token_path
                or (os.environ.get(TOKEN_PATH_ENV) or "").strip()
                or DEFAULT_TOKEN_PATH)

    def get_access_token(self, token_path: str | None = None) -> str:
        """ensure_valid_token() from the sibling engine; refreshes as needed."""
        engine = _load_oauth_engine()
        token_data = engine.ensure_valid_token(self.resolve_token_path(token_path))
        return token_data.access_token

    def get_request_options(self, model: str, body: dict,
                            token_path: str | None = None, *,
                            streaming: bool = False,
                            timeout_s: float = 600,
                            client_beta: str | None = None,
                            structured: bool = False,
                            cli_version: str = DEFAULT_CLI_VERSION,
                            entrypoint: str = DEFAULT_ENTRYPOINT,
                            session_id: str | None = None,
                            account_uuid: str | None = None,
                            device_id: str | None = None,
                            count_tokens: bool = False) -> tuple[str, dict, dict]:
        """THE SEAM: (url, headers, cloaked_body) for one upstream request.

        The ferry_front monkeypatch layer calls this with the parsed request
        body and rewrites url/headers/payload with what comes back. No
        network happens here beyond whatever ensure_valid_token itself does
        (the engine's refresh-on-expiry); everything else is pure.
        """
        access_token = self.get_access_token(token_path)
        headers = build_headers(
            access_token, streaming=streaming, cli_version=cli_version,
            entrypoint=entrypoint, timeout_s=timeout_s, model=model)
        if client_beta is not None or structured:
            headers["anthropic-beta"] = build_anthropic_beta(
                model, client_beta=client_beta, structured=structured)
        cloaked_body = apply_cloaking(
            body, cli_version=cli_version, entrypoint=entrypoint,
            session_id=session_id, account_uuid=account_uuid,
            device_id=device_id)
        return build_upstream_url(count_tokens=count_tokens), headers, cloaked_body


def get_request_options(model: str, body: dict,
                        token_path: str | None = None,
                        **kwargs) -> tuple[str, dict, dict]:
    """Module-level convenience wrapper around the config seam."""
    return ClaudeOAuthAnthropicConfig().get_request_options(
        model, body, token_path, **kwargs)
