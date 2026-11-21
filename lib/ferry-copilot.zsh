# ferry copilot: point GitHub Copilot CLI (`copilot`, npm @github/copilot)
# at the ferry endpoint through a `copilot-ferry` wrapper. No config file of
# the user's is edited and no GitHub login is read or changed.
#
# Research. The issue asks "check whether it accepts a custom endpoint at all":
# YES, through its BYOK ("bring your own key") mode. github/copilot-cli holds
# only docs and a changelog; the shipped code is the npm package, whose
# platform package (@github/copilot-darwin-arm64) is one 163 MB Mach-O
# executable. Its JS is not greppable (no COPILOT_PROVIDER_* string is found by
# `grep -a -b`, so there are no byte offsets to cite), so every fact below is
# read from the client's own shipped help text and the upstream changelog, and
# was then confirmed by running the real client against a stub. Pinned:
# copilot 1.0.94 (buildMetadata.gitCommit fdeb5949944; `copilot --version`),
# `copilot help environment` and `copilot help providers` of that build, and
# github/copilot-cli changelog.md (fetched 2026-11-20, top entry 1.0.114).
#
# (1) Provider config / base URL. Env-driven: COPILOT_PROVIDER_BASE_URL
#     "activates BYOK mode ... GitHub authentication is not required" (help
#     providers; help environment). COPILOT_PROVIDER_TYPE is "openai"
#     (default), "azure" or "anthropic", so an Anthropic base is possible too,
#     but ferry's OpenAI chat/completions door reaches every lane, so ferry
#     uses type openai. A saved providers.json is also read (changelog.md:462,
#     1.0.109: COPILOT_PROVIDER_* "take precedence over a saved providers.json,
#     unless COPILOT_PROVIDERS_CONFIG names the file"; :399 1.0.110), which is
#     why the env route never has to touch it.
# (2) Key. COPILOT_PROVIDER_API_KEY (or ..._API_KEY_COMMAND, or
#     COPILOT_PROVIDER_BEARER_TOKEN; help environment). The wrapper exports
#     COPILOT_PROVIDER_API_KEY from $FERRY_KEY (the 0600 agents.key) at run
#     time: it lives in the process environment, never argv or a file. The
#     stub saw `authorization: Bearer <key>`. Any bearer / key-command the user
#     exported is unset first (changelog.md:461: precedence differs per type).
# (3) Custom headers. Yes: COPILOT_PROVIDER_HEADERS, newline-separated
#     "Name: Value" pairs, "sent only to the BYOK provider endpoint (not to
#     GitHub Copilot services)" (help environment). X-Ferry-Client and
#     X-Ferry-Fleet go there; the stub saw `x-ferry-client`.
# (4) API spoken. COPILOT_PROVIDER_WIRE_API "completions" (default) or
#     "responses"; ferry sets completions explicitly. Observed:
#     POST {base}/chat/completions, streamed (OpenAI JS SDK 5.20.1).
# (5) Least invasive wiring: wrapper only, ~/.local/bin/copilot-ferry. The
#     plain `copilot` command, ~/.copilot (config.json, providers.json,
#     sessions) and the GitHub login are untouched. The model comes from
#     COPILOT_MODEL (a model is required for BYOK, help providers); `--model`
#     overrides it (changelog.md:459, 1.0.109), so any lane id works.
#     Limitations: copilot's built-in model picker is GitHub's list, so lane
#     switching is `copilot-ferry --model <lane>`; ferry's lane id is also
#     the "well-known model id", which copilot does not recognise, so it falls
#     back to safe defaults for tool support and token limits (help providers).
#
# Wire: ~/.local/bin/copilot-ferry and ~/.config/ferry/copilot.json.
# `ferry copilot --unwire` removes exactly those (generic, record-driven).

cmd_copilot() { _agent_main copilot "$@"; }

_copilot_help() {
  cat <<'EOF2'
ferry copilot - point GitHub Copilot CLI at the ferry endpoint (BYOK).

Usage:
  ferry copilot [--host H] [--port P] [--key K] [--model LANE] [--keep N]
  ferry copilot --unwire [--dry-run]

  Writes the `copilot-ferry` wrapper (~/.local/bin/copilot-ferry). It sets
  COPILOT_PROVIDER_BASE_URL=<ferry>/v1 (type openai, wire API completions),
  COPILOT_PROVIDER_API_KEY from the 0600 key file at run time (never argv),
  COPILOT_PROVIDER_HEADERS (X-Ferry-Client / X-Ferry-Fleet) and
  COPILOT_MODEL=<lane>, then execs `copilot`. No GitHub login is needed in
  that mode. Your ~/.copilot config and plain `copilot` are not touched.

  Pick another lane with `copilot-ferry --model <lane>`.
  Limitation: copilot's own /model list is GitHub's; lanes are chosen by
  --model. Needs a copilot new enough to have BYOK (see `copilot help providers`).
EOF2
}

_copilot_wire() {
  _agent_python <<'PYEOF'
import os
def dq(s):   # bash/zsh $'...' quoting
    return "$'" + s.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n") + "'"
headers = "\n".join(f"{k}: {v}" for k, v in HEADERS.items())
pre = ("unset COPILOT_PROVIDER_BEARER_TOKEN COPILOT_PROVIDER_API_KEY_COMMAND "
       "COPILOT_PROVIDER_TRANSPORT COPILOT_PROVIDER_MODEL_ID "
       "COPILOT_PROVIDER_WIRE_MODEL COPILOT_PROVIDERS_CONFIG COPILOT_OFFLINE")
w = os.path.join(HOME, ".local", "bin", "copilot-ferry")
import shlex
wrapper(w, {
    "COPILOT_PROVIDER_TYPE": "openai",
    "COPILOT_PROVIDER_BASE_URL": shlex.quote(BASE),
    "COPILOT_PROVIDER_WIRE_API": "completions",
    "COPILOT_PROVIDER_API_KEY": '"$FERRY_KEY"',
    "COPILOT_PROVIDER_HEADERS": dq(headers),
    "COPILOT_MODEL": shlex.quote(LANE),
}, "copilot", pre=pre)
print(f"    Wrapper:        {w}  (lane {LANE}; lanes: {' '.join(LANES)})")
save_record()
PYEOF
}
