# ferry kimi: wire Kimi Code CLI (`kimi`) to the ferry front door (issue #3).
#
# WHICH "kimi": the issue names MoonshotAI/kimi-cli (Python, PyPI `kimi-cli`).
# That project is archived: its last release (1.52.0) short-circuits every entry
# point to a one-line "no longer maintained" notice (and a bare `kimi` downloads
# and runs an installer script from the CDN), so nothing can be wired to it:
#   MoonshotAI/kimi-cli@9ab1286 src/kimi_cli/__main__.py:23 (gate call),
#   :28 (_deprecation_gate), src/kimi_cli/deprecation.py:1 (module docstring),
#   pyproject.toml:4 (description "[Archived] Legacy Python Kimi CLI").
# Its successor, MoonshotAI/kimi-code (TypeScript, npm @moonshot-ai/kimi-code,
# binary `kimi`, also `curl https://code.kimi.com/kimi-code/install.sh | bash`),
# is what a user has on PATH as `kimi` today, so this module wires THAT one.
# Source cites below are MoonshotAI/kimi-code@83e4dde (HEAD, CLI 2.7.2 line);
# the e2e in ferry-kimi.test.py ran on npm @moonshot-ai/kimi-code 2.6.2.
#
# Research checklist (issue #3):
#  (1) Provider config / base URL. TOML at $KIMI_CODE_HOME/config.toml, default
#      ~/.kimi-code (apps/kimi-code/src/utils/paths.ts:26 getDataDir: env
#      KIMI_CODE_HOME, else homedir()/.kimi-code; constant name at
#      apps/kimi-code/src/constant/app.ts:26; docs/en/configuration/
#      config-files.md:5, :9). `[providers.<name>]` has type / base_url /
#      api_key / api_key_env / custom_headers (schema, camelCased in code:
#      packages/agent-core-v2/src/app/kosongConfig/configSection.ts:50;
#      docs/en/configuration/config-files.md:127); `[models."<alias>"]`
#      points at a provider (docs config-files.md:147). Types: kimi,
#      anthropic, openai (Chat Completions), openai_responses, bedrock,
#      google-genai, vertexai (docs/en/configuration/providers.md:9). The
#      `openai` type calls client.chat.completions with baseURL and
#      defaultHeaders from the provider (packages/core/src/llm/builtin/
#      protocol/openai/index.ts:48, :49, :69, :75). ferry uses type "openai":
#      lane ids go out as the `model` of POST <base>/chat/completions.
#  (2) Key. `api_key_env = "<VAR>"` names an environment variable that is
#      re-read on every request and is mutually exclusive with api_key (docs
#      config-files.md:129; read from process.env at
#      packages/agent-core-v2/src/app/auth/authService.ts:729). Plain
#      KIMI_API_KEY / OPENAI_API_KEY in the shell are NOT read (docs
#      env-vars.md:5, :9). So the wrapper exports FERRY_KIMI_API_KEY from the
#      0600 key file and the config only names that variable: no key on disk
#      in the config, none in argv.
#  (3) Custom headers. Yes: `[providers.<name>.custom_headers]` goes on every
#      request (configSection.ts:53, protocol/openai/index.ts:49). ferry puts
#      X-Ferry-Client (and X-Ferry-Fleet when set) there.
#  (4) API. OpenAI Chat Completions, streaming only (the e2e stub saw
#      stream=true, 24 tools, POST /v1/chat/completions).
#  (5) Least invasive wiring: `kimi-ferry`, a wrapper that sets
#      KIMI_CODE_HOME=~/.config/ferry/kimi/home (a ferry-owned data dir whose
#      config.toml lists every served lane) and execs `kimi`. The user's own
#      ~/.kimi-code (login, config, sessions) is never opened or edited and the
#      plain `kimi` command is unchanged. No flag can point at another config
#      file (apps/kimi-code/src/cli/commands.ts has only -m/-p style options),
#      so the data-dir env var is the only relocation hook.
#      Why not the env-only route (KIMI_MODEL_NAME / KIMI_MODEL_API_KEY /
#      KIMI_MODEL_BASE_URL, docs env-vars.md:115): it defines a single
#      model, so no lane list and no /model switching between lanes.
#
# Limitations, all from the data-dir swap: `kimi-ferry` starts with an empty
# profile, so it does not see the user's own MCP servers, skills, plugins,
# sessions or Kimi login; sessions made by `kimi-ferry` live under
# ~/.config/ferry/kimi/home and `--unwire` leaves that data (only ferry's
# config.toml and the wrapper are removed). A Python kimi-cli older than 1.52
# on PATH ignores KIMI_CODE_HOME and would run against the user's own account.
# The wrapper unsets KIMI_MODEL_NAME / _API_KEY / _PROVIDER_TYPE / _BASE_URL
# and KIMI_CODE_BASE_URL first: they outrank default_model (env-vars.md:117) and
# would route around ferry. Telemetry is off in the owned config.

_kimi_help() {
  cat <<'EOF'
ferry kimi - wire Kimi Code CLI (npm @moonshot-ai/kimi-code, binary `kimi`) to ferry.

  Run `kimi-ferry` instead of `kimi`. It uses a ferry-owned data dir
  (~/.config/ferry/kimi/home) with one provider "ferry" (OpenAI Chat
  Completions) and one model alias per served lane (ferry/<lane>); the default
  is the lane you pass with --model (default heavy). The key is read at run
  time from ~/.config/ferry/agents.key through an env var, never put in the
  config. X-Ferry-Client / X-Ferry-Fleet go out as provider custom_headers.

  Your own ~/.kimi-code and the plain `kimi` command are not touched. Limits:
  kimi-ferry starts with its own empty profile (no MCP servers, skills,
  plugins, sessions or Kimi login from your ~/.kimi-code), and --unwire keeps
  the sessions stored under ~/.config/ferry/kimi/home. The old Python kimi-cli
  (PyPI kimi-cli) is archived and cannot be wired.
EOF
}

_kimi_wire() {
  _agent_python <<'PYEOF'
import json, shlex

home_dir = os.path.join(HOME, ".config", "ferry", "kimi", "home")
bin_dir = os.path.join(HOME, ".local", "bin")

def q(s):                       # TOML basic string (JSON escapes are valid)
    return json.dumps(s, ensure_ascii=False)

lines = [
    f"# {MARKER}",
    "# Generated by `ferry kimi`; `ferry kimi --unwire` removes it. Kimi Code reads",
    "# this as $KIMI_CODE_HOME/config.toml when started through `kimi-ferry`.",
    f"default_model = {q('ferry/' + LANE)}",
    "telemetry = false",
    "",
    "[providers.ferry]",
    'type = "openai"',
    f"base_url = {q(BASE)}",
    'api_key_env = "FERRY_KIMI_API_KEY"',
    "",
    "[providers.ferry.custom_headers]",
]
for k, v in HEADERS.items():
    lines.append(f"{q(k)} = {q(v)}")
for lane in LANES:
    lines += ["",
              f"[models.{q('ferry/' + lane)}]",
              'provider = "ferry"',
              f"model = {q(lane)}",
              "max_context_size = 200000",
              'capabilities = ["tool_use"]']
write_owned(os.path.join(home_dir, "config.toml"), "\n".join(lines) + "\n", 0o644)

# Env that outranks default_model / the provider must not leak in from the
# user's shell: the KIMI_MODEL_* family and the managed base URL.
pre = "unset KIMI_MODEL_NAME KIMI_MODEL_API_KEY KIMI_MODEL_PROVIDER_TYPE KIMI_MODEL_BASE_URL KIMI_CODE_BASE_URL"
wrapper(os.path.join(bin_dir, "kimi-ferry"),
        {"KIMI_CODE_HOME": shlex.quote(home_dir),
         "FERRY_KIMI_API_KEY": '"$FERRY_KEY"',
         "KIMI_DISABLE_TELEMETRY": "1"},
        "kimi", pre=pre)
print(f"    Wrapper:        {os.path.join(bin_dir, 'kimi-ferry')}")
print(f"    Default model:  ferry/{LANE} (lanes: {' '.join(LANES)})")
print("    Run `kimi-ferry`; plain `kimi` and ~/.kimi-code are untouched.")
save_record()
PYEOF
}

cmd_kimi() { _agent_main kimi "$@"; }
