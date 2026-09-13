"""Native Claude Pro/Max subscription OAuth for the ferry front door.

WHY THIS EXISTS

ferry fronts model traffic through API keys, but a Claude Pro/Max
subscription is authenticated by an interactive browser login, not a key.
Anthropic's OAuth client (the one the Claude web app itself uses) issues
short-lived access tokens backed by a rotating refresh token, which litellm
can present as a bearer credential. This module is the credential engine:
PKCE authorize-URL construction, a one-shot localhost callback server, code
exchange, refresh with rotation, and JSON persistence at mode 0600.

The protocol constants come from the verified reference implementation of the
Claude OAuth client (AmazingAng/auth2api) and must not be "fixed": the
client_id is a public identifier hard-coded in Claude's own bundles, the
scopes must be joined with literal '+' characters with the colons left
UN-encoded, and the token endpoint expects JSON bodies with NO Authorization
header.

Token flow: build_authorize_url() -> user opens the URL in a browser ->
run_callback_server() catches the redirect on 127.0.0.1:54545/callback ->
exchange_code() turns the authorization code into a TokenData -> save_token()
persists it. Refresh tokens ROTATE on every exchange and refresh, so any code
that refreshes must persist the new refresh_token or the credential dies with
the next rotation; ensure_valid_token() is the one call that handles load,
refresh and re-save correctly.

Runs on the stdlib plus httpx (both present in the litellm venv). httpx is
imported defensively so the module also imports on a bare interpreter, which
keeps offline unit tests and tooling cheap; the network calls simply raise if
httpx is truly missing at call time.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, quote, urlencode, urlparse

try:  # httpx lives in the litellm venv; keep the bare import usable anyway.
    import httpx
except ImportError:  # pragma: no cover - exercised only outside the venv
    httpx = None  # type: ignore[assignment]

__all__ = [
    "AUTH_URL",
    "TOKEN_URL",
    "CLIENT_ID",
    "REDIRECT_URI",
    "SCOPE",
    "SCOPES",
    "CALLBACK_HOST",
    "CALLBACK_PORT",
    "CALLBACK_PATH",
    "CALLBACK_TIMEOUT",
    "TokenData",
    "OAuthError",
    "CallbackTimeout",
    "CallbackStateError",
    "TokenExchangeError",
    "PermanentRefreshError",
    "build_authorize_url",
    "run_callback_server",
    "exchange_code",
    "refresh_tokens",
    "load_token",
    "save_token",
    "ensure_valid_token",
]

# --- protocol constants (verified against AmazingAng/auth2api) -------------
AUTH_URL = "https://claude.ai/oauth/authorize"
TOKEN_URL = "https://api.anthropic.com/v1/oauth/token"
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
REDIRECT_URI = "http://localhost:54545/callback"
SCOPE = "org:create_api_key user:profile user:inference"
SCOPES = SCOPE.split()  # joined with literal '+' in the authorize query

CALLBACK_HOST = "127.0.0.1"
CALLBACK_PORT = 54545
CALLBACK_PATH = "/callback"
CALLBACK_TIMEOUT = 300

# Where ferry persists the subscription credential. Mirrors the chatgpt lane's
# ~/.config/litellm/chatgpt/auth.json but under ferry's own config dir. The zsh
# wrapper (lib/ferry-auth-claude.zsh) and the provider seam both resolve here by
# default; FERRY_CLAUDE_AUTH_JSON / FERRY_CLAUDE_TOKEN_PATH override.
DEFAULT_TOKEN_PATH = os.path.expanduser("~/.config/ferry/claude/auth.json")

HTTP_TIMEOUT = 30.0        # seconds per token HTTP request
MAX_REFRESH_ATTEMPTS = 3   # 1 try + 2 retries, linear 1s/2s backoff


class OAuthError(Exception):
    """Base class for every failure in the Claude OAuth lifecycle."""


class CallbackTimeout(OAuthError):
    """The localhost callback server never received a valid code in time."""


class CallbackStateError(OAuthError):
    """The callback carried a state that did not match ours (CSRF mismatch)."""


class TokenExchangeError(OAuthError):
    """The authorization-code exchange was rejected by the token endpoint."""


class PermanentRefreshError(OAuthError):
    """The refresh token was revoked, reused or otherwise permanently dead.

    Raised instead of retrying: a rotated-then-lost refresh token cannot be
    recovered by trying again, and hammering the endpoint just risks locking
    the account. The user must re-run the browser flow.
    """


# --- PKCE --------------------------------------------------------------------

def _b64url(raw: bytes) -> str:
    """base64url without padding, the only encoding PKCE accepts."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def generate_pkce_pair() -> Tuple[str, str]:
    """Return a fresh PKCE ``(verifier, challenge)`` pair.

    The verifier is 96 random bytes base64url-encoded (128 chars); the
    challenge is the unpadded base64url SHA-256 of the verifier's ASCII bytes
    (43 chars), per RFC 7636 S256.
    """
    verifier = _b64url(secrets.token_bytes(96))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


# --- TokenData ----------------------------------------------------------------

@dataclass
class TokenData:
    """A persisted Claude OAuth credential.

    ``expires_at`` is an absolute epoch float (issue time + ``expires_in``),
    so a token loaded from disk hours later still answers ``is_expired()``
    correctly. ``email``/``account_uuid`` come from the token response's
    ``account`` object and are informational; ``refresh_token`` is the crown
    jewel — it rotates on every use and losing it means re-login.
    """

    access_token: str
    refresh_token: str
    email: Optional[str] = None
    account_uuid: Optional[str] = None
    expires_at: float = 0.0

    def is_expired(self, skew: float = 60) -> bool:
        """True when the access token is (or soon will be) unusable.

        ``skew`` seconds of clock leeway are treated as already expired so a
        token never outlives its validity on the wire.
        """
        return time.time() >= self.expires_at - skew

    def to_json(self) -> str:
        """Serialize to a pretty JSON string (the on-disk format)."""
        return json.dumps(
            {
                "access_token": self.access_token,
                "refresh_token": self.refresh_token,
                "email": self.email,
                "account_uuid": self.account_uuid,
                "expires_at": self.expires_at,
            },
            indent=2,
        )

    @classmethod
    def from_json(cls, payload: "str | Dict[str, Any]") -> "TokenData":
        """Rebuild from the JSON string (or already-parsed dict) ``to_json``
        produced."""
        if isinstance(payload, str):
            payload = json.loads(payload)
        return cls(
            access_token=payload["access_token"],
            refresh_token=payload["refresh_token"],
            email=payload.get("email"),
            account_uuid=payload.get("account_uuid"),
            expires_at=float(payload.get("expires_at", 0.0)),
        )

    @classmethod
    def from_token_response(cls, doc: Dict[str, Any], now: Optional[float] = None) -> "TokenData":
        """Build from a raw token-endpoint response body."""
        account = doc.get("account") or {}
        issued_at = time.time() if now is None else now
        return cls(
            access_token=doc["access_token"],
            refresh_token=doc.get("refresh_token", ""),
            email=account.get("email_address"),
            account_uuid=account.get("uuid"),
            expires_at=issued_at + float(doc.get("expires_in", 0)),
        )


# --- persistence ---------------------------------------------------------------

def save_token(path: str, tok: TokenData) -> None:
    """Atomically write ``tok`` as JSON to ``path`` with mode 0600.

    Written to a temp sibling then renamed, so a crash mid-write never leaves
    a half-token that ``load_token`` would trust, and chmod'ed after the fact
    so a pre-existing wider mode does not survive.
    """
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(tok.to_json())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_token(path: str) -> TokenData:
    """Read a token written by :func:`save_token`. Raises the usual OSError /
    JSON errors for missing or corrupt files."""
    with open(path, "r") as fh:
        return TokenData.from_json(fh.read())


# --- authorize ----------------------------------------------------------------

def build_authorize_url() -> Tuple[str, str, str]:
    """Build the consent-page URL plus the secrets that go with it.

    Returns ``(url, state, verifier)``. Open ``url`` in the user's browser,
    hand ``state`` to :func:`run_callback_server` to validate the redirect,
    and hand ``verifier`` (with ``state``) to :func:`exchange_code`.

    The query keeps the scope colons and '+' separators literal: Anthropic's
    authorize endpoint matches the scope string verbatim, and ``%3A``/``%2B``
    spellings are rejected.
    """
    state = secrets.token_urlsafe(32)
    verifier, challenge = generate_pkce_pair()
    params = [
        ("code", "true"),
        ("client_id", CLIENT_ID),
        ("response_type", "code"),
        ("redirect_uri", REDIRECT_URI),
        ("scope", "+".join(SCOPES)),
        ("code_challenge", challenge),
        ("code_challenge_method", "S256"),
        ("state", state),
    ]
    query = urlencode(params, quote_via=quote, safe=":+/")
    return f"{AUTH_URL}?{query}", state, verifier


# --- localhost callback --------------------------------------------------------

class _CallbackResult:
    """Mutable box the handler thread writes the captured redirect into."""

    def __init__(self) -> None:
        self.code: Optional[str] = None
        self.error: Optional[str] = None
        self.done = threading.Event()


def run_callback_server(state: str, timeout: float = CALLBACK_TIMEOUT) -> str:
    """Wait for the OAuth redirect on 127.0.0.1:54545/callback.

    Serves exactly one browser redirect: validates ``state`` (mismatches get a
    400 and the server keeps waiting — a stray tab reload must not kill the
    login), captures the authorization ``code``, shows a "you may close this
    tab" page and returns the code.

    Raises :class:`CallbackTimeout` if nothing valid arrives within ``timeout``
    seconds, :class:`CallbackStateError` never (mismatches keep waiting), and
    :class:`OAuthError` if the provider redirected an explicit ``error``.
    """
    result = _CallbackResult()

    class Handler(BaseHTTPRequestHandler):
        server_version = "ferry-claude-oauth/1.0"

        def _reply(self, status: int, title: str, body: str) -> None:
            page = (
                "<!doctype html><html><head><title>"
                + title
                + "</title></head><body style=\"font-family:system-ui;"
                  "text-align:center;padding-top:3em\"><h2>"
                + title
                + "</h2><p>"
                + body
                + "</p><script>window.close()</script></body></html>"
            )
            blob = page.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(blob)))
            self.end_headers()
            self.wfile.write(blob)

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            parsed = urlparse(self.path)
            if parsed.path != CALLBACK_PATH:
                self._reply(404, "Not found", f"expected {CALLBACK_PATH}")
                return
            qs = {k: v[0] for k, v in parse_qs(parsed.query).items()}
            if qs.get("state") != state:
                # CSRF mismatch: reject this redirect but keep serving; the
                # user's real browser tab is still on its way.
                self._reply(400, "State mismatch", "This redirect is not for this login.")
                return
            if "error" in qs:
                result.error = qs.get("error_description") or qs["error"]
                self._reply(400, "Authorization failed", str(result.error))
                result.done.set()
                return
            code = qs.get("code")
            if not code:
                self._reply(400, "Missing code", "The redirect carried no authorization code.")
                return
            result.code = code
            self._reply(200, "Claude connected to ferry", "You can close this tab.")
            result.done.set()

        def log_message(self, *args: Any) -> None:  # keep the console clean
            pass

    try:
        server = HTTPServer((CALLBACK_HOST, CALLBACK_PORT), Handler)
    except OSError as exc:
        raise OAuthError(
            f"cannot bind {CALLBACK_HOST}:{CALLBACK_PORT} for the OAuth "
            f"callback ({exc}); is another login already running?"
        ) from exc
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        got_it = result.done.wait(timeout)
    finally:
        server.shutdown()
        server_close = getattr(server, "server_close", None)
        if server_close is not None:
            server_close()
        thread.join(timeout=5)
    if result.error is not None:
        raise OAuthError(f"authorization failed: {result.error}")
    if result.code is None:
        raise CallbackTimeout(
            f"no OAuth redirect arrived on {REDIRECT_URI} within {timeout:.0f}s"
        )
    return result.code


# --- token endpoint -------------------------------------------------------------

def _token_post(payload: Dict[str, str]) -> Any:
    """POST ``payload`` as JSON to TOKEN_URL with NO auth header.

    Single choke point for the network, so tests can mock ``httpx.post``.
    Returns the raw response; raises nothing on HTTP error statuses (the
    callers classify them).
    """
    if httpx is None:
        raise OAuthError("httpx is required for token requests (install it or run inside the litellm venv)")
    return httpx.post(TOKEN_URL, json=payload, timeout=HTTP_TIMEOUT)


def _permanent_refresh_failure(response: Any) -> bool:
    """True when a refresh rejection is permanent (revoked/reused token).

    OAuth maps an invalid (rotated-away, revoked, reused) refresh token to
    400/401; those never succeed on retry. 429/5xx and network hiccups are
    transient and worth retrying.
    """
    if response is None:
        return False
    status = getattr(response, "status_code", None)
    if status in (400, 401, 403):
        return True
    try:
        err = response.json().get("error")
    except Exception:
        return False
    if isinstance(err, dict):
        err = str(err.get("type", "")) + " " + str(err.get("message", ""))
    err = str(err).lower()
    return "invalid_grant" in err or "revoked" in err or "reused" in err


def _transient_http_errors() -> Tuple[type, ...]:
    """Exception classes worth retrying, resolved at CALL time so a mocked
    httpx (tests substitute a real class for ``httpx.HTTPError``) works."""
    errs = [OSError]
    http_error = getattr(httpx, "HTTPError", None) if httpx is not None else None
    if isinstance(http_error, type) and issubclass(http_error, BaseException):
        errs.append(http_error)
    return tuple(errs)


def _response_body(response: Any) -> Dict[str, Any]:
    try:
        return response.json()
    except Exception as exc:
        raise TokenExchangeError(
            f"token endpoint returned non-JSON body ({getattr(response, 'status_code', '?')})"
        ) from exc


def exchange_code(code: str, verifier: str, state: Optional[str] = None) -> TokenData:
    """Exchange the authorization code for tokens (authorization_code grant).

    ``state`` is threaded through from :func:`build_authorize_url` (the
    reference client sends it in the exchange body too) and is included only
    when provided. The POST is JSON with NO Authorization header.
    """
    payload = {
        "grant_type": "authorization_code",
        "client_id": CLIENT_ID,
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "code_verifier": verifier,
    }
    if state is not None:
        payload["state"] = state
    response = _token_post(payload)
    status = getattr(response, "status_code", 0)
    if status != 200:
        body = _safe_text(response)
        raise TokenExchangeError(f"code exchange failed ({status}): {body}")
    return TokenData.from_token_response(_response_body(response))


def refresh_tokens(refresh_token: str) -> TokenData:
    """Refresh an access token (refresh_token grant), retrying transient
    failures up to 3 attempts with linear backoff.

    Refresh tokens ROTATE: the returned TokenData carries the replacement,
    which callers MUST persist (see :func:`ensure_valid_token`). Raises
    :class:`PermanentRefreshError` without retrying when the endpoint says the
    token was revoked/reused, and :class:`TokenExchangeError` when attempts
    run out.
    """
    payload = {
        "grant_type": "refresh_token",
        "client_id": CLIENT_ID,
        "refresh_token": refresh_token,
    }
    transient = _transient_http_errors()
    last_error: Optional[str] = None
    for attempt in range(1, MAX_REFRESH_ATTEMPTS + 1):
        try:
            response = _token_post(payload)
        except transient as exc:
            last_error = f"network error: {exc!r}"
        else:
            status = getattr(response, "status_code", 0)
            if status == 200:
                return TokenData.from_token_response(_response_body(response))
            if _permanent_refresh_failure(response):
                raise PermanentRefreshError(
                    f"refresh token rejected permanently ({status}): "
                    f"{_safe_text(response)} — re-run the browser login"
                )
            last_error = f"HTTP {status}: {_safe_text(response)}"
        if attempt < MAX_REFRESH_ATTEMPTS:
            time.sleep(attempt)  # linear backoff: 1s, 2s
    raise TokenExchangeError(
        f"refresh failed after {MAX_REFRESH_ATTEMPTS} attempts; last: {last_error}"
    )


def _safe_text(response: Any) -> str:
    try:
        return str(response.json())
    except Exception:
        return getattr(response, "text", "<no body>")[:500]


def ensure_valid_token(path: str) -> TokenData:
    """Return a usable token from ``path``, refreshing and RE-SAVING if stale.

    This is the only correct read path for a stored credential: Anthropic
    rotates the refresh token on every refresh, so the refreshed TokenData
    (with its replacement refresh_token) must be persisted immediately or the
    next call would present an already-consumed token. If the server ever
    omits a new refresh token, the old one is carried forward rather than
    dropped.
    """
    tok = load_token(path)
    if not tok.is_expired():
        return tok
    if not tok.refresh_token:
        raise OAuthError(f"token at {path} is expired and has no refresh_token; re-run the browser login")
    fresh = refresh_tokens(tok.refresh_token)
    if not fresh.refresh_token:
        fresh.refresh_token = tok.refresh_token
    save_token(path, fresh)
    return fresh


def _resolve_cli_path(path: Optional[str]) -> str:
    return path or os.environ.get("FERRY_CLAUDE_AUTH_JSON") or DEFAULT_TOKEN_PATH


def main(argv: Optional[list] = None) -> int:
    """CLI consumed by lib/ferry-auth-claude.zsh.

    Subcommands:
      login       --output <path>     run the browser PKCE flow, write auth.json
      ensure-valid --auth <path> [--force]   refresh if stale (or forced), re-save
    The zsh wrapper never parses stdout; it re-reads the auth.json we write.
    Tokens are never printed. Exit 0 on success, 1 on any OAuthError.
    """
    import argparse

    parser = argparse.ArgumentParser(prog="ferry_claude_oauth")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_login = sub.add_parser("login")
    p_login.add_argument("--output", default=None)
    p_ev = sub.add_parser("ensure-valid")
    p_ev.add_argument("--auth", default=None)
    p_ev.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)

    try:
        if args.cmd == "login":
            path = _resolve_cli_path(args.output)
            url, state, verifier = build_authorize_url()
            print(f"Open this URL in your browser to authorize ferry:\n\n{url}\n", flush=True)
            code = run_callback_server(state)
            token = exchange_code(code, verifier, state=state)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            save_token(path, token)
            print(f"logged in as {token.email or 'unknown'} -> {path}")
            return 0
        if args.cmd == "ensure-valid":
            path = _resolve_cli_path(args.auth)
            if args.force:
                tok = load_token(path)
                if not tok.refresh_token:
                    raise OAuthError(f"token at {path} has no refresh_token; re-run login")
                fresh = refresh_tokens(tok.refresh_token)
                if not fresh.refresh_token:
                    fresh.refresh_token = tok.refresh_token
                save_token(path, fresh)
                print(f"refreshed {fresh.email or 'unknown'}")
            else:
                token = ensure_valid_token(path)
                print(f"valid {token.email or 'unknown'}")
            return 0
    except OAuthError as exc:
        print(f"error: {exc}", file=os.sys.stderr)
        return 1
    return 1


if __name__ == "__main__":  # pragma: no cover - exercised via ferry auth-claude
    raise SystemExit(main())
