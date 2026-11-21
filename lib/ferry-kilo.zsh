# ferry kilo: run Kilo Code (Kilo-Org/kilocode) on a ferry lane.
#
# Wiring: a wrapper plus ONE ferry-owned config file. `~/.local/bin/kilo-ferry`
# exports KILO_CONFIG=~/.config/ferry/kilo/kilo.json and FERRY_KEY, then execs
# the real `kilo`. Kilo layers KILO_CONFIG over the user's own config, so the
# user's ~/.config/kilo/*, their logins (auth.json) and their default provider
# are not touched, and plain `kilo` never sees the ferry provider.
# `ferry kilo --unwire` removes the wrapper, the owned config and the record.
#
# Optional `--global` (extension users, see the VS Code note below): ALSO adds
# one named `ferry` provider member to ~/.config/kilo/kilo.json (snapshotted,
# plain-JSON member edit, `--unwire` removes only that member). It adds a
# provider only; it never sets `model`, so the user's default does not move.
#
# Research, read from source at Kilo-Org/kilocode@b2947d7e56c5 (main, the CLI
# package is @kilocode/cli 7.8.28, binaries `kilo` and `kilocode`, both the
# same file: npm package.json "bin"; the registry's kilo:kilo is correct).
# Kilo's CLI is an opencode fork, so the provider shape is opencode's. Paths
# are relative to packages/:
#  (1) Provider config: opencode-style `provider.<id>` in kilo.json / kilo.jsonc
#      (also config.json, opencode.json[c], read from Global.Path.config =
#      $XDG_CONFIG_HOME or ~/.config, + "kilo": core/src/global.ts:12,24 and
#      opencode/src/config/config.ts:462-480). Entries merge into the provider
#      table at opencode/src/provider/provider.ts:1548-1556 and :1744-1755.
#      Custom OpenAI base URL: `npm: "@ai-sdk/openai-compatible"` (loaded by
#      provider.ts:145) with `options.baseURL`; the SDK appends
#      /chat/completions. Anthropic base URL: `npm: "@ai-sdk/anthropic"` with
#      options.baseURL works the same way; ferry uses chat/completions (rule:
#      prefer OpenAI). Config path override: KILO_CONFIG (an explicit file,
#      core/src/flag/flag.ts:45, loaded after the global files at
#      opencode/src/config/config.ts:766-781, trusted for {env:}/{file:}),
#      KILO_CONFIG_DIR (flag.ts:140, a whole extra dir, config.ts:818), and
#      KILO_CONFIG_CONTENT (inline JSON, flag.ts:46, config.ts:901). The
#      wrapper uses KILO_CONFIG. The file is JSONC (opencode/src/config/
#      parse.ts:8 allows comments + trailing commas), so it carries the
#      `// ferry-managed: kilo` marker the helper needs.
#  (2) Key: `options.apiKey` in the provider entry (provider.ts:1886 passes the
#      options through to the SDK). Config text supports `{env:NAME}` and
#      `{file:path}` substitution (opencode/src/config/variable.ts:50-100, the
#      secret-name blocklist at kilocode/config/variable.ts:21-30 does not list
#      FERRY_KEY). The owned config says `{env:FERRY_KEY}`, which the wrapper
#      fills from the 0600 agents.key at run time: the key is in no file and no
#      argv. The --global entry uses `{file:~/.config/ferry/agents.key}` (the
#      extension has no wrapper to export an env var).
#  (3) Custom headers: yes, `options.headers` (config schema
#      opencode/src/provider/provider.ts:1138 Record<string,string>, carried
#      into the SDK options by :1886-1890). Sent: X-Ferry-Client and, when
#      FERRY_FLEET was set at wire time, X-Ferry-Fleet.
#  (4) API: OpenAI chat/completions (streaming) through @ai-sdk/openai-compatible.
#      Kilo's own default provider is the Kilo Gateway (`kilo` provider,
#      provider.ts:535); it is simply not selected: the owned config sets
#      model "ferry/<lane>" and small_model likewise.
#  (5) Least invasive: wrapper + KILO_CONFIG (this file). Data (auth.json,
#      sessions, db) lives under $XDG_DATA_HOME/kilo (core/src/global.ts:22),
#      so HOME/XDG redirects it in tests.
#
# VS Code extension (packages/kilo-vscode): NOT file-less. It has no
# SecretStorage use (grep of `secrets` over src/ finds nothing outside tests and
# the vendored autocomplete code) and it spawns the very same CLI backend,
# `kilo serve --port 0` (src/services/cli-backend/server-manager.ts:129) with
# process.env, so it reads the same ~/.config/kilo config and auth.json as the
# CLI. A VS Code launched from a shell with KILO_CONFIG exported would pick the
# owned file up, but a dock/Spotlight launch will not, so for the extension use
# `ferry kilo --global`: it adds the `ferry` provider to the user's global
# config, where the extension finds it and offers ferry/<lane> in its model
# picker. Selecting a model in the extension stays the user's choice.

_kilo_help() {
  cat <<'EOF'
ferry kilo - run Kilo Code (the `kilo` CLI, Kilo-Org/kilocode) on a ferry lane.

Writes ~/.local/bin/kilo-ferry and ~/.config/ferry/kilo/kilo.json. The wrapper
sets KILO_CONFIG to that file and FERRY_KEY from ~/.config/ferry/agents.key
(0600) and runs `kilo`, so your own Kilo config, logins and plain `kilo` are
not touched. Every lane the host serves is listed as ferry/<lane>; the default
is ferry/<--model lane> (heavy). X-Ferry-Client (and X-Ferry-Fleet when
FERRY_FLEET is set at wire time) are sent as provider headers.

  kilo-ferry run "hello"        non-interactive
  kilo-ferry                    interactive

Kilo flags:
  --global      ALSO add one `ferry` provider to ~/.config/kilo/kilo.json so the
                VS Code extension (which has no wrapper) can use it. Adds the
                provider only; your default model does not change. Needs plain
                JSON (kilo.json); a kilo.jsonc is left alone and reported.
EOF
}

_kilo_wire() {
  local globalcfg=0 a
  for a in "${AG_EXTRA[@]}"; do
    case "$a" in
      --global) globalcfg=1 ;;
      *) echo "Unknown option for 'ferry kilo': $a"; return 1 ;;
    esac
  done
  _agent_python "$globalcfg" <<'PYEOF'
import json, os, sys
want_global = sys.argv[1] == "1"
cfgdir = os.path.join(HOME, ".config", "ferry", "kilo")
owned = os.path.join(cfgdir, "kilo.json")
wrap = os.path.join(HOME, ".local", "bin", "kilo-ferry")

def provider(key):
    return {
        "npm": "@ai-sdk/openai-compatible",
        "name": f"Ferry ({HOST})",
        "options": {"baseURL": BASE, "apiKey": key, "headers": dict(HEADERS)},
        "models": {l: {"name": l, "limit": {"context": 128000, "output": 16000}}
                   for l in LANES},
    }

cfg = {
    "$schema": "https://app.kilo.ai/config.json",
    "provider": {"ferry": provider("{env:FERRY_KEY}")},
    "model": f"ferry/{LANE}",
    "small_model": f"ferry/{LANE}",
}
write_owned(owned, f"// {MARKER}\n// Generated by `ferry kilo`; `ferry kilo --unwire` removes it.\n"
            + json.dumps(cfg, indent=2) + "\n", 0o644)
wrapper(wrap, {"KILO_CONFIG": json.dumps(owned), "FERRY_KEY": '"$FERRY_KEY"'},
        "kilo")
print(f"    Wrapper:        {wrap}")
print(f"    Kilo config:    {owned} (via KILO_CONFIG)")
print(f"    Default model:  ferry/{LANE}  (lanes: {', '.join(LANES)})")

if want_global:
    g = os.path.join(HOME, ".config", "kilo", "kilo.json")
    if os.path.exists(os.path.join(HOME, ".config", "kilo", "kilo.jsonc")):
        print("    NOTE: ~/.config/kilo/kilo.jsonc exists; ferry will not edit a JSONC")
        print("    file. Add the provider by hand or use kilo-ferry.")
    else:
        json_member_set(g, ["provider", "ferry"],
                        provider("{file:" + KEYFILE + "}"))
        print(f"    Global config:  provider.ferry added to {g}")
save_record({"wrapper": wrap, "config": owned, "global": want_global})
PYEOF
}
cmd_kilo() { _agent_main kilo "$@"; }
