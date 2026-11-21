# ferry qwen - point Qwen Code at the ferry endpoint, through a wrapper.
#
# Research, answered from the client's source (QwenLM/qwen-code, tag v0.27.3 =
# ddeba2fd3436, the version the e2e installs; checked against the stub too):
#   (1) Provider config / base URL. Qwen Code (a Gemini CLI fork) keeps
#       providers in settings.json under `modelProviders.<authType>[]`, each
#       entry {id, name, envKey, baseUrl, generationConfig}
#       (packages/cli/src/config/settingsSchema.ts:404, docs/users/configuration/
#       model-providers.md:7). OpenAI-compatible is auth type `openai`; env-only
#       setups use OPENAI_BASE_URL / OPENAI_API_KEY / OPENAI_MODEL
#       (packages/core/src/models/constants.ts:90-91,
#       packages/cli/src/config/auth.ts:25, availableModels.ts:48). No Anthropic
#       wiring is needed: the openai type is the least-friction route to ferry.
#   (2) Key. Never stored in settings: the runtime reads process.env[envKey]
#       (docs model-providers.md:7); for `openai` the default envKey is
#       OPENAI_API_KEY (auth.ts:25). The wrapper exports it from the 0600 key
#       file at run time.
#   (3) Custom headers. YES, but only per model entry: `generationConfig.
#       customHeaders` (docs model-providers.md:291-293; applied at
#       packages/core/src/core/openaiContentGenerator/provider/default.ts:109-115).
#       The env-only route has no header knob, which is why ferry writes a
#       settings file instead of relying on OPENAI_BASE_URL alone.
#   (4) API. OpenAI chat/completions by default (pipeline.ts:566,610 call
#       client.chat.completions.create); `wireApi: "responses"` is optional and
#       not used. Observed POST /v1/chat/completions, Authorization: Bearer,
#       X-Ferry-Client from customHeaders.
#   (5) Least invasive wiring: the `qwen-ferry` wrapper plus ONE ferry-owned
#       settings file ~/.config/ferry/qwen-settings.json that the wrapper points
#       QWEN_CODE_SYSTEM_SETTINGS_PATH at (packages/cli/src/config/
#       storage-paths-lite.ts:176). System settings outrank the user's
#       ~/.qwen/settings.json, so the file forces the `openai` auth type and the
#       ferry lane for THIS wrapper only; ~/.qwen and plain `qwen` are never
#       touched. (QWEN_HOME, packages/core/src/config/storage.ts:194, would
#       relocate the whole login/history dir: more invasive, not used.)
#
# "May Qwen share Gemini CLI's wiring?" No, only the shape (wrapper + owned
# system-settings file). Gemini CLI speaks the Gemini-native API through
# GOOGLE_GEMINI_BASE_URL and takes headers from GEMINI_CLI_CUSTOM_HEADERS
# (ferry-gemini.zsh); Qwen Code is the OpenAI-compatible chat/completions path
# with headers in modelProviders[].generationConfig.customHeaders, uses
# QWEN_CODE_SYSTEM_SETTINGS_PATH, and so works with device keys (fk-), which
# the Gemini-native route does not.

_qwen_help() {
  cat <<'EOF'
ferry qwen - point Qwen Code at the ferry endpoint via a `qwen-ferry` wrapper.

Usage:
  ferry qwen [--host H] [--port P] [--key K] [--model LANE] [--unwire]

  Writes ~/.local/bin/qwen-ferry and ~/.config/ferry/qwen-settings.json
  (ferry's own; ~/.qwen is never touched, plain `qwen` is unchanged).
  qwen-ferry reads the key at run time from ~/.config/ferry/agents.key, sets
  OPENAI_API_KEY / OPENAI_BASE_URL / OPENAI_MODEL and
  QWEN_CODE_SYSTEM_SETTINGS_PATH, then runs `qwen`. Every lane the host serves
  is listed for /model; the default is --model (heavy). Requests go to
  <endpoint>/v1/chat/completions with X-Ferry-Client / X-Ferry-Fleet headers.
EOF
}

_qwen_wire() {
  _agent_python <<'PYEOF'
import json, os, shlex
settings = os.path.join(HOME, ".config", "ferry", "qwen-settings.json")
models = [{"id": l, "name": f"ferry {l}", "envKey": "OPENAI_API_KEY",
           "baseUrl": BASE, "generationConfig": {"customHeaders": dict(HEADERS)}}
          for l in LANES]
doc = {"security": {"auth": {"selectedType": "openai"}},
       "model": {"name": LANE},
       "modelProviders": {"openai": models}}
write_owned(settings, json.dumps(doc, indent=2) + "\n", 0o644)
exports = {
    "OPENAI_API_KEY": '"$FERRY_KEY"',
    "OPENAI_BASE_URL": shlex.quote(BASE),
    "OPENAI_MODEL": shlex.quote(LANE),
    "QWEN_CODE_SYSTEM_SETTINGS_PATH": shlex.quote(settings),
}
w = wrapper(os.path.join(HOME, ".local", "bin", "qwen-ferry"), exports, "qwen")
print(f"    Wrapper:        {w}")
print(f"    Settings:       {settings}")
print(f"    Lane:           {LANE}  (endpoint {BASE}, chat/completions)")
save_record({"settings": settings})
PYEOF
}

cmd_qwen() { _agent_main qwen "$@"; }
