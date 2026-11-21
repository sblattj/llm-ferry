# ferry interpreter - point Open Interpreter at the ferry endpoint by lane name.
#
# WHICH "Open Interpreter": openinterpreter/openinterpreter was rewritten. The
# old Python/LiteLLM package (PyPI `open-interpreter` 0.4.x, flags --api_base /
# --api_key) is no longer what the project ships. Current main is a Rust FORK
# OF OPENAI CODEX ("Open Interpreter is a fork of OpenAI's Codex",
# README.md "Harness Emulation") installed by `curl -fsSL
# https://www.openinterpreter.com/install | sh` (README.md "Installation") as
# the binaries `interpreter` and `i` (codex-package.json in the release asset
# open-interpreter-package-<target>.tar.gz: variant open-interpreter,
# entrypoint bin/interpreter). This module wires THAT. The legacy 0.4.x Python
# CLI also installs a binary called `interpreter`; it has no `-c` override and
# is not supported here (run `interpreter --version`: 0.0.x is the new one).
#
# Research, pinned to openinterpreter/openinterpreter@bb3f575 (release
# rust-v0.0.61, 2026-11-11), codex-rs/ paths:
#  (1) Provider config + base URL. Config lives in $INTERPRETER_HOME (default
#      ~/.openinterpreter; CODEX_HOME is deliberately ignored, so it never
#      shares ~/.codex): utils/home-dir/src/lib.rs:6 and :12. Providers are
#      the `model_providers` map of config.toml (config/src/config_toml.rs:323)
#      selected by `model_provider` (config_toml.rs:163); an entry's `base_url`
#      is model-provider-info/src/lib.rs:158. OpenAI-compatible base URL: yes,
#      that field. Anthropic base URL: yes too, `wire_api = "messages"`
#      (lib.rs:111 enum WireApi: responses | chat | messages, deserialised
#      at lib.rs:140), which ferry serves at /v1/messages.
#  (2) Key: `env_key` names an env var holding the bearer (lib.rs:163). Other
#      options are a literal `experimental_bearer_token` (discouraged, lib.rs
#      :169) and a command (`auth`). ferry uses env_key, so the key is never
#      in argv or a config file.
#  (3) Custom headers: `http_headers` (static, lib.rs:185) and
#      `env_http_headers` (header -> env var, lib.rs:190). Both used below.
#  (4) API: all three wire_api values exist. Chat Completions is POSTed to
#      <base_url>/chat/completions (chat-wire-compat/src/client.rs:135,
#      streaming at :177). ferry uses wire_api="chat" (lib.rs:115) because the
#      project targets low-cost non-OpenAI models, the path ferry's lanes
#      serve natively; Responses (the default, lib.rs:111) would also work.
#      The CLI flag form is `--chat-completions` (exec/src/lib.rs:578).
#  (5) Least invasive wiring: every setting rides on `-c key=value` config
#      overrides (cli/src/main.rs:137, the root flag is forwarded to the exec
#      subcommand too), exactly how `ferry codex` does it. NO file in
#      ~/.openinterpreter is read or written, the user's plain `interpreter`
#      and its login are untouched, and the only file ferry creates is the
#      wrapper ~/.local/bin/interpreter-ferry. The key travels in the
#      FERRY_INTERPRETER_KEY env var, read at run time from the 0600
#      ~/.config/ferry/agents.key. Limitation: lane names are not in the
#      client's model catalogue, so the client may print a non-fatal "model
#      metadata not found" notice; the request is still sent.
#
# User args come LAST, so `interpreter-ferry -c model=\"medium\"` or `exec ...`
# overrides ferry's default lane.

_interpreter_help() {
  cat <<'EOF'
ferry interpreter - wire Open Interpreter (Rust, Codex-based `interpreter`) to ferry.

Usage:
  ferry interpreter [--host H] [--port P] [--key K] [--model LANE]
  ferry interpreter --unwire [--dry-run]

Writes one wrapper, ~/.local/bin/interpreter-ferry. It runs `interpreter`
with `-c` overrides: a `ferry` model provider (Chat Completions at
<base>/v1/chat/completions), the lane as the model, the bearer in the
FERRY_INTERPRETER_KEY env var read from ~/.config/ferry/agents.key (0600),
and X-Ferry-Client (and X-Ferry-Fleet when set) as request headers.
~/.openinterpreter and your plain `interpreter` are never touched.

  interpreter-ferry                       interactive, default lane
  interpreter-ferry exec "do a thing"     one-shot, non-interactive
  interpreter-ferry -c model='"flash"'    another lane

Only the current Rust release (`interpreter --version` 0.0.x) is supported,
not the legacy Python package open-interpreter 0.4.x.
EOF
}

_interpreter_wire() {
  _agent_python <<'PYEOF'
import json, os

def toml_str(s):
    return json.dumps(s)          # a JSON string is a valid TOML basic string

hdrs = "{" + ",".join("%s=%s" % (toml_str(k), toml_str(v))
                      for k, v in HEADERS.items()) + "}"
cfg = [
    ("model_provider", toml_str("ferry")),
    ("model_providers.ferry.name", toml_str("Ferry")),
    ("model_providers.ferry.base_url", toml_str(BASE)),
    ("model_providers.ferry.env_key", toml_str("FERRY_INTERPRETER_KEY")),
    ("model_providers.ferry.wire_api", toml_str("chat")),
    ("model_providers.ferry.http_headers", hdrs),
]
if "X-Ferry-Fleet" not in HEADERS:
    # Fleet set at run time still reaches ferry (the header is dropped when
    # FERRY_FLEET is unset or empty, model-provider-info/src/lib.rs:190).
    cfg.append(("model_providers.ferry.env_http_headers",
                "{" + toml_str("X-Ferry-Fleet") + "=" + toml_str("FERRY_FLEET") + "}"))
cfg.append(("model", toml_str(LANE)))

argv_prefix = []
for k, v in cfg:
    argv_prefix += ["-c", "%s=%s" % (k, v)]

wrapper(os.path.join(HOME, ".local", "bin", "interpreter-ferry"),
        {"FERRY_INTERPRETER_KEY": '"$FERRY_KEY"'},
        "interpreter", argv_prefix)
print(f"    Wrapper:        {HOME}/.local/bin/interpreter-ferry  (lane {LANE})")
print("    Served lanes:   " + " ".join(LANES))
print("    ~/.openinterpreter and plain `interpreter` are untouched.")
save_record({"wire_api": "chat", "lanes": LANES})
PYEOF
}

cmd_interpreter() { _agent_main interpreter "$@"; }
