# ferry codewhale: point Codewhale (Hmbown/Codewhale, formerly deepseek-tui;
# Rust) at the ferry endpoint through a `codewhale-ferry` wrapper and ONE
# ferry-owned config file. The user's ~/.codewhale/config.toml, keyring and
# login are never read, edited or replaced, so plain `codewhale` is untouched.
#
# Research (Hmbown/Codewhale @ 555322f = tag v0.10.2 + 0, cloned --depth 1
# into a scratch dir; every fact below read from that tree and, where noted,
# confirmed by running the v0.10.2 darwin-arm64 release binary against a stub):
#
# (0) Binary name. The release asset is `codewhale-macos-arm64` (plus
#     `codewhale-tui-macos-arm64`, the TUI it dispatches to, which must sit next
#     to it); installed on PATH it is `codewhale` (`codewhale --help` prints
#     "Usage: codewhale ..."; a `deepseek` alias is not shipped in v0.10.2).
#     The registry's `codewhale:codewhale` is therefore right.
#
# (1) Provider config. TOML, default ~/.codewhale/config.toml (docs/
#     CONFIGURATION.md:341-343). The path is overridable: `--config FILE` or
#     env CODEWHALE_CONFIG_PATH, which must be ABSOLUTE
#     (docs/CONFIGURATION.md:349, crates/paths/src/lib.rs:160); `--config`
#     wins over the env. A custom OpenAI-compatible provider is a user-named
#     table: `provider = "<name>"` plus `[providers.<name>] kind =
#     "openai-compatible"`, `base_url`, `api_key_env`, `model`
#     (docs/CONFIGURATION.md:681). The generic built-in `openai` provider
#     also takes OPENAI_BASE_URL (crates/config/src/env_overrides.rs:198) but its
#     key env is only a FALLBACK after saved config and keyring credentials
#     (docs/CONFIGURATION.md:1469-1470), so a user's saved openai key would
#     beat the ferry key; a custom table with api_key_env has no such rival.
#     Anthropic: the `wire = "anthropic"` custom dialect exists
#     (docs/CONFIGURATION.md:691-692), but ferry uses OpenAI chat below.
#
# (2) Key. `api_key_env = "FERRY_KEY"` names an env var the key is read from
#     (docs/CONFIGURATION.md:699; crates/tui/src/lib.rs:5031 reads
#     entry.api_key_env). The wrapper exports FERRY_KEY from the 0600
#     ~/.config/ferry/agents.key at run time: the key is in no config file and
#     never in argv. Verified live: the stub saw `Authorization: Bearer <key>`.
#
# (3) Custom headers. Yes: `http_headers = { "Name" = "value" }` at top level or
#     in a provider table (docs/CONFIGURATION.md:778; field
#     crates/config/src/provider_toml.rs:150; merge crates/config/src/
#     config_resolve.rs:1049). Authorization and Content-Type stay
#     client-managed. ferry sends X-Ferry-Client (and X-Ferry-Fleet when set)
#     there. Verified live: the stub saw x-ferry-client.
#
# (4) API. A custom table sends Chat Completions by default (docs/
#     CONFIGURATION.md:691), POST {base_url}/chat/completions, streaming.
#     Verified live: POST /v1/chat/completions with "stream":true.
#
# (5) Least invasive wiring (chosen). Ferry-owned config
#     ~/.config/ferry/codewhale/config.toml (provider "ferry", model = the
#     lane) + wrapper ~/.local/bin/codewhale-ferry exporting
#     CODEWHALE_CONFIG_PATH and FERRY_KEY. The ferry table sets
#     `allow_insecure_http = true` (crates/config/src/provider_toml.rs:148;
#     consumed crates/engine/src/client.rs:1145) because a LAN ferry is
#     plain http and Codewhale refuses non-loopback http otherwise; consent is
#     scoped to this one table, not the shell. No TOML surgery on the user's
#     file, so there is no _codewhale_unwire hook: --unwire just removes the
#     two files ferry created.
#     Limits: (a) the wrapper runs on ferry's config ALONE, so the user's own
#     approval/sandbox/skills settings in ~/.codewhale/config.toml do not apply
#     under `codewhale-ferry` (state such as sessions still lives under
#     CODEWHALE_HOME); (b) the table carries one default model, the other
#     lanes are `codewhale-ferry --model <lane>`; (c) the wrapper unsets
#     CODEWHALE_/DEEPSEEK_ PROVIDER, MODEL, BASE_URL and HTTP_HEADERS
#     (crates/config/src/env_overrides.rs:141, :186, :195, :278) so a shell export cannot divert the route.
#     Codewhale's own usage telemetry (on by default, see its first-run
#     notice) is its business and is left as the user configured it.

cmd_codewhale() { _agent_main codewhale "$@"; }

_codewhale_help() {
  cat <<'EOF'
ferry codewhale - point Codewhale (Hmbown/Codewhale, binary `codewhale`) at ferry.

Usage:
  ferry codewhale [--host H] [--port P] [--key K] [--model LANE]
  ferry codewhale --unwire [--dry-run]

Writes a ferry-owned config (~/.config/ferry/codewhale/config.toml, custom
OpenAI-compatible provider "ferry", chat completions) and a wrapper
~/.local/bin/codewhale-ferry that points CODEWHALE_CONFIG_PATH at it and reads
the key from ~/.config/ferry/agents.key at run time. Your ~/.codewhale config,
keys and plain `codewhale` are left alone. Run `codewhale-ferry`; other lanes:
`codewhale-ferry --model <lane>`. Limit: the wrapper uses ferry's config only,
so your own ~/.codewhale/config.toml settings do not apply to it.
Headers: X-Ferry-Client (and X-Ferry-Fleet) are sent via http_headers.
EOF
}

_codewhale_wire() {
  _agent_python <<'PYEOF'
import os, json, shlex
cfg = os.path.join(HOME, ".config", "ferry", "codewhale", "config.toml")
q = json.dumps      # a JSON string is a valid TOML basic string
hdr = ", ".join(f"{q(k)} = {q(v)}" for k, v in HEADERS.items())
toml = "\n".join([
    f"# {MARKER}",
    "# Written by `ferry codewhale`; `ferry codewhale --unwire` removes it.",
    "# The key is NOT here: api_key_env is read from the environment at run time.",
    'provider = "ferry"',
    f"default_text_model = {q(LANE)}",
    "",
    "[providers.ferry]",
    'kind = "openai-compatible"',
    f"base_url = {q(BASE)}",
    'api_key_env = "FERRY_KEY"',
    f"model = {q(LANE)}",
    "allow_insecure_http = true",
    f"http_headers = {{ {hdr} }}",
    "",
])
write_owned(cfg, toml, 0o600)

pre = "unset CODEWHALE_PROVIDER DEEPSEEK_PROVIDER CODEWHALE_MODEL DEEPSEEK_MODEL DEEPSEEK_DEFAULT_TEXT_MODEL " \
      "CODEWHALE_BASE_URL DEEPSEEK_BASE_URL CODEWHALE_HTTP_HEADERS DEEPSEEK_HTTP_HEADERS"
wp = os.path.join(HOME, ".local", "bin", "codewhale-ferry")
wrapper(wp, {"CODEWHALE_CONFIG_PATH": shlex.quote(cfg), "FERRY_KEY": '"$FERRY_KEY"'},
        "codewhale", pre=pre)
print(f"    Wrapper:        {wp}  (lane {LANE})")
save_record()
PYEOF
}
