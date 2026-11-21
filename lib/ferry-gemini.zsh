# ferry gemini - point Gemini CLI at the ferry endpoint, through a wrapper.
#
# Research, answered from the client's source (google-gemini/gemini-cli, tag
# v0.56.0 = b6e23a7dc29e; the same facts were observed against the installed
# 0.56.0 binary with a stub server):
#   (1) Provider config / base URL. Gemini CLI has no provider list: it speaks
#       ONE API, Gemini-native. The endpoint comes from the env var
#       GOOGLE_GEMINI_BASE_URL (packages/core/src/core/contentGenerator.ts:352,
#       validated at :354); there is no OpenAI or Anthropic base URL at all.
#   (2) Key. GEMINI_API_KEY (contentGenerator.ts:175, :201). The Google SDK
#       sends it as `x-goog-api-key` (header name from :277). The front door
#       reads that header (front/ferry_front.py CREDENTIAL_HEADERS, ~:1473).
#       GEMINI_API_KEY_AUTH_MECHANISM=bearer would send Authorization instead
#       (:277-:288) but is not needed.
#   (3) Custom headers. YES: GEMINI_CLI_CUSTOM_HEADERS, "Name: value" pairs
#       separated by commas (contentGenerator.ts:241, parsed by
#       packages/core/src/utils/customHeaderUtils.ts:10-38, merged at :272-:274).
#       So X-Ferry-Client / X-Ferry-Fleet are sent.
#   (4) API. Gemini-native: POST {base}/v1beta/models/<model>:streamGenerateContent
#       (observed: /v1beta/models/heavy:streamGenerateContent?alt=sse).
#   (5) Least invasive wiring: the `gemini-ferry` wrapper, env only, plus ONE
#       ferry-owned settings file ~/.config/ferry/gemini-settings.json that the
#       wrapper points GEMINI_CLI_SYSTEM_SETTINGS_PATH at
#       (packages/core/src/config/storage.ts:185, settings.ts:105). The user's
#       ~/.gemini is never read-written by ferry and plain `gemini` is untouched.
#
# Three source facts shape the wiring:
#   - GOOGLE_GEMINI_BASE_URL alone selects auth type "gateway"
#     (contentGenerator.ts:87-88), but the CLI's own validateAuthMethod
#     (packages/cli/src/config/auth.ts:73) rejects it ("Invalid auth method
#     selected."; observed on 0.56.0). So the settings file selects
#     security.auth.selectedType = "gemini-api-key" (contentGenerator.ts:65),
#     which still honours GOOGLE_GEMINI_BASE_URL (:352) and, being a SYSTEM
#     settings file, beats whatever login the user's own settings.json holds.
#   - The model name goes to the wire verbatim (observed /models/heavy:...)
#     EXCEPT four aliases the CLI rewrites to Google model ids before sending:
#     auto, pro, flash, flash-lite (packages/core/src/config/models.ts:110-113,
#     resolveModel switch ~:262-:300). So lane `heavy` / `medium` reach litellm,
#     but a ferry lane literally named `flash` is turned into a Google id and
#     cannot be selected from this client. Reported as a gap, not worked around.
#   - Device keys: since v1.52.0 the front door admits a device key on
#     POST /v1beta/models/<lane>:generateContent and :streamGenerateContent
#     with the lane limit, RPM, budget and metering of chat/completions
#     (front/ferry_front.py GEMINI_GENERATE_RE, DEVICE_KEY_PATTERN_ROUTES).
#     A host older than v1.52.0 still refuses them, so with an `fk-` key the
#     wire step prints a one-line note saying so.
# Headless runs in an untrusted directory need GEMINI_CLI_TRUST_WORKSPACE=true
# or --skip-trust (a Gemini CLI rule, not ferry's); the wrapper does not set it.

_gemini_help() {
  cat <<'EOF'
ferry gemini - point Gemini CLI at the ferry endpoint via a `gemini-ferry` wrapper.

Usage:
  ferry gemini [--host H] [--port P] [--key K] [--model LANE] [--unwire]

  Writes ~/.local/bin/gemini-ferry and ~/.config/ferry/gemini-settings.json
  (ferry's own; ~/.gemini is never touched, plain `gemini` is unchanged).
  gemini-ferry sets GOOGLE_GEMINI_BASE_URL, GEMINI_API_KEY (read at run time
  from ~/.config/ferry/agents.key), GEMINI_MODEL=<lane> and
  GEMINI_CLI_CUSTOM_HEADERS (X-Ferry-Client, X-Ferry-Fleet), then runs `gemini`.

Limits:
  - Gemini CLI only speaks the Gemini-native API (/v1beta/...). A device key
    (fk-...) works there only on a host running ferry v1.52.0 or later.
  - The CLI rewrites the model names auto, pro, flash and flash-lite to Google
    ids, so those lane names cannot be reached; heavy / medium pass through.
EOF
}

_gemini_wire() {
  local key
  key=$(<"$AG_KEYFILE") 2>/dev/null || key=""
  if [[ "$key" == fk-* ]]; then
    echo "    Note: the key is a device key (fk-...). The host must run ferry v1.52.0 or"
    echo "    later, which admits device keys on the Gemini-native routes (/v1beta)."
  fi
  _agent_python <<'PYEOF'
import json, os, shlex
settings = os.path.join(HOME, ".config", "ferry", "gemini-settings.json")
# gemini-api-key, not "gateway": gateway fails the CLI's own auth validation.
write_owned(settings, json.dumps(
    {"security": {"auth": {"selectedType": "gemini-api-key"}}}, indent=2) + "\n", 0o644)
hdr =",".join(f"{k}: {v}" for k, v in HEADERS.items())
pre = (
    "unset GOOGLE_GENAI_USE_GCA GOOGLE_GENAI_USE_VERTEXAI"
)
exports = {
    "GOOGLE_GEMINI_BASE_URL": shlex.quote(ROOT),
    "GEMINI_API_KEY": '"$FERRY_KEY"',
    "GEMINI_MODEL": shlex.quote(LANE),
    "GEMINI_CLI_SYSTEM_SETTINGS_PATH": shlex.quote(settings),
    "GEMINI_CLI_CUSTOM_HEADERS":
        '"${GEMINI_CLI_CUSTOM_HEADERS:+$GEMINI_CLI_CUSTOM_HEADERS,}"' + shlex.quote(hdr),
}
w = wrapper(os.path.join(HOME, ".local", "bin", "gemini-ferry"), exports, "gemini", pre=pre)
print(f"    Wrapper:        {w}")
print(f"    Settings:       {settings}")
print(f"    Lane:           {LANE}  (endpoint {ROOT}, Gemini-native /v1beta)")
save_record({"settings": settings})
PYEOF
}

cmd_gemini() { _agent_main gemini "$@"; }
