# ferry omp - point oh-my-pi (can1357/oh-my-pi, binary `omp`, a Pi fork) at the
# ferry endpoint.
#
# Research (source read from a fresh clone, can1357/oh-my-pi @ 2843591; the
# installed omp is 17.2.15). Paths are packages/coding-agent/... unless noted.
#
# Does omp share Pi's models.json wiring? NO. Same idea, different file and
# format, so ferry does not reuse the prime/pi text-surgery:
#   - omp reads <agent dir>/models.yml, else models.yaml, and YAML is the only
#     format it parses there (src/config/config-file.ts:156-176 ConfigFile ctor
#     picks the .yaml fallback and the .json migration path; :233-240
#     #parseContent runs YAML.parse for .yml/.yaml). A models.json is only a
#     ONE-TIME migration source, converted when no YAML file exists
#     (src/config/config-file.ts:30-57 migrateJsonToYml: `if
#     (fs.existsSync(ymlPath)) return`), so a `ferry` entry in models.json is
#     ignored once a models.yml exists. Pi's "$NAME" apiKey form also differs:
#     omp treats a bare env-var NAME as the variable and anything else, "$NAME"
#     included, as a literal (src/config/resolve-config-value.ts:104-108,
#     `$envExact(v) || v`). Default location: path.join(getAgentDir(),
#     "models.yml") (src/config/model-registry.ts:783), getAgentDir() =
#     $PI_CODING_AGENT_DIR else ~/.omp/agent (../utils/src/dirs.ts:361-367; omp
#     reads the same PI_CODING_AGENT_DIR variable Pi does).
#
# (1) Provider config + base URLs. Two places: models.yml
#     `providers.<id>.baseUrl` + `api: openai-completions` (docs/models.md
#     "Provider-level fields"), or an extension's pi.registerProvider(name,
#     {baseUrl, apiKey, api, headers, authHeader, models})
#     (src/extensibility/extensions/types.ts:2284-2328 ProviderConfig,
#     :2331-2362 ProviderModelConfig; docs/extensions.md "Provider
#     registration"). An Anthropic base URL would be `api:
#     "anthropic-messages"`; the OpenAI chat dialect is preferred here.
# (2) Key. registerProvider's apiKey is "API key or environment variable name"
#     (types.ts:2288-2294); it is installed as the provider's runtime key
#     (src/config/model-registry.ts:4440-4449, whose comment says it is
#     typically an env-var name). ferry sets apiKey: "FERRY_KEY" and the
#     omp-ferry wrapper exports FERRY_KEY from ~/.config/ferry/agents.key
#     (0600) at run time, so the key is never in argv nor in any config file.
# (3) Headers. ProviderConfig.headers (types.ts:2299-2300) and per-model
#     headers; ferry sends X-Ferry-Client and X-Ferry-Fleet (when set).
# (4) API. openai-completions, POST {baseUrl}/chat/completions; every served
#     lane is registered as a model. Per-model `compat` pins the plain dialect
#     (supportsStore false, supportsDeveloperRole false, maxTokensField
#     max_tokens; types.ts:2360-2361) because a LiteLLM-fronted local lane may
#     reject OpenAI's newer defaults.
# (5) Least invasive wiring (design rule 1a, NO edit of any user file). omp
#     has no flag to point at another models.yml (PI_CODING_AGENT_DIR and
#     --profile relocate the whole agent dir: credentials DB, settings,
#     sessions), but it loads `-e <file>` extensions on top of normal
#     discovery (docs/cli-reference.md:160 `--extension <path>, -e <path>`;
#     :164 "--no-extensions ... explicit -e paths still work"). So ferry
#     writes ONE ferry-owned extension, ~/.config/ferry/omp/ferry-provider.ts,
#     that registers a `ferry` provider, plus the wrapper ~/.local/bin/omp-ferry
#     = `omp -e <that file> --model ferry/<lane>`. The user's models.yml,
#     settings and logins are never read or written, plain `omp` is unchanged,
#     and `ferry omp --unwire` removes the two files ferry created.
#     Limitation: the provider exists only under omp-ferry (or `omp -e <file>`),
#     not in a plain `omp` /model list.
#
# Files: ~/.local/bin/omp-ferry (wrapper), ~/.config/ferry/omp/ferry-provider.ts
# (extension), ~/.config/ferry/omp.json (record), ~/.config/ferry/agents.key
# (0600, shared).

cmd_omp() { _agent_main omp "$@"; }

_omp_help() {
  cat <<'EOF'
ferry omp - point oh-my-pi (omp, a Pi fork) at the ferry endpoint.

Usage:
  ferry omp [common flags]

  Writes ~/.config/ferry/omp/ferry-provider.ts (an omp extension that
  registers a 'ferry' provider: openai-completions, every served lane as a
  model, X-Ferry-Client / X-Ferry-Fleet headers) and the wrapper
  ~/.local/bin/omp-ferry, which runs `omp -e <that file> --model
  ferry/<lane>` with FERRY_KEY read from ~/.config/ferry/agents.key (0600) at
  run time. Nothing of yours is edited: models.yml, settings and logins are
  untouched and plain `omp` behaves as before. (omp uses models.yml, not Pi's
  models.json, and has no flag to point at another one; -e is the clean door.)
  Limitation: the ferry provider exists under omp-ferry only.

Then:  omp-ferry     (or: omp -e ~/.config/ferry/omp/ferry-provider.ts --model ferry/heavy)
EOF
}

_omp_wire() {
  if (( ${#AG_EXTRA[@]} )); then
    echo "Unknown option for 'ferry omp': ${AG_EXTRA[1]}"
    return 1
  fi
  _agent_python <<'PYEOF'
lanes = [LANE] + [l for l in LANES if l != LANE]
ext_path = os.path.join(HOME, ".config", "ferry", "omp", "ferry-provider.ts")
compat = {"supportsStore": False, "supportsDeveloperRole": False,
          "maxTokensField": "max_tokens"}
models = [{"id": l, "name": f"ferry {l}", "reasoning": False, "input": ["text"],
           "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
           "contextWindow": 128000, "maxTokens": 16384, "compat": compat}
          for l in lanes]
cfg = {"baseUrl": BASE, "apiKey": "FERRY_KEY", "api": "openai-completions",
       "authHeader": True, "headers": HEADERS, "models": models}
body = json.dumps(cfg, indent=2).replace("\n", "\n  ")   # JSON literals are valid TypeScript
ts = (f"// {MARKER}\n"
      "// Generated by `ferry omp`; `ferry omp --unwire` removes it. It holds no key:\n"
      "// apiKey names the FERRY_KEY variable the omp-ferry wrapper exports from\n"
      "// ~/.config/ferry/agents.key at run time.\n"
      "export default function ferryProvider(pi: any) {\n"
      f"  pi.registerProvider(\"ferry\", {body});\n"
      "}\n")
write_owned(ext_path, ts, 0o644)
wrapper(os.path.join(HOME, ".local", "bin", "omp-ferry"),
        {"FERRY_KEY": '"$FERRY_KEY"'}, "omp",
        ["-e", ext_path, "--model", f"ferry/{LANE}"])
save_record({"extension": ext_path, "provider": "ferry", "lanes": lanes})
print(f"    Wired omp       -> {BASE} (openai-completions; lanes: {', '.join(lanes)})")
print(f"    Extension:      {ext_path}")
print(f"    Run:            omp-ferry   (omp -e {ext_path} --model ferry/{LANE})")
PYEOF
}
