# ferry reasonix - point Reasonix (esengine/DeepSeek-Reasonix, a Go coding agent
# for the terminal) at the ferry endpoint.
#
# Wiring: a ferry-owned REASONIX_HOME plus a `reasonix-ferry` wrapper. Plain
# `reasonix` is never touched: ~/.reasonix (config.toml, .env, sessions, skills,
# hooks) and every project reasonix.toml stay byte for byte as the user left
# them, and ferry never opens them, so no snapshot is needed.
#
# What it is: the repo has two lines (README.md:93-109). 2.x (branch `studio`,
# a Go module, `go.mod`) is what the research below read; the final release is
# v2.61.0 (README.md:7, "no longer maintained as of 2026-11-12"). 1.x is the
# npm package `reasonix` (README.md:125-129: `npm i -g reasonix`, or
# `brew install esengine/reasonix/reasonix`), a thin launcher that pulls a
# prebuilt native binary (npm/reasonix/package.json optionalDependencies
# @reasonix/cli-<os>-<arch>). 2.x ships as release archives
# `reasonix-<os>-<arch>.tar.gz` on github.com/esengine/DeepSeek-Reasonix
# releases (README.md:155-156). Both lines install a binary named `reasonix`
# (npm/reasonix/package.json "bin": {"reasonix": ...}; cmd/reasonix), which
# matches the registry entry reasonix:reasonix in lib/ferry-agents.zsh.
#
# Research (clone: github.com/esengine/DeepSeek-Reasonix @ cd20bb50, branch
# studio, 2026-11-11; the e2e runs the v2.61.0 darwin-arm64 release binary):
#
# (1) Where provider config lives; base URLs.
#   <Reasonix home>/config.toml, where the home is $REASONIX_HOME, else
#   ~/.reasonix on macOS/Linux (docs/CONFIG_PATHS.md:6-19;
#   internal/contract/config/paths.go:33 userConfigPath = Home()/config.toml;
#   storage_roots.go:67 and :127 read REASONIX_HOME). Precedence is flag >
#   ./reasonix.toml > <home>/config.toml (reasonix.example.toml:2). With
#   REASONIX_HOME set "the runtime is fully self-contained" (docs/
#   CONFIG_PATHS.md:21-24), which is exactly the isolation ferry wants.
#   A provider is `[[providers]]` with name, kind, base_url, models, default,
#   api_key_env, headers (internal/contract/config/provider_entry.go:29-46;
#   reasonix.example.toml:94-103). kind = "openai" is the OpenAI-compatible
#   /chat/completions client (internal/model/openai/openai.go:1) and its URL is
#   base_url + "/chat/completions" (internal/model/openai/chaturl.go:25), so
#   ferry's base (http://host:port/v1) is used as is. kind = "anthropic" speaks
#   /v1/messages (internal/model/anthropic/anthropic.go:2) and takes a root
#   base_url without /v1 (anthropic.go:76-85); ferry uses the OpenAI kind.
#   There is NO command-line flag to point at another config file; the home
#   directory is the override, so ferry owns a whole home.
#   `default_model = "ferry"` selects the provider (reasonix.example.toml:8);
#   a project reasonix.toml cannot override default_model
#   (internal/contract/config/config.go:221 IgnoredProjectDefaultModel).
#
# (2) Key. Only through the NAMED env var in the provider's api_key_env, and
#   only as stored in <home>/.env: "loaded provider entries still resolve only
#   from Reasonix's global .env" (config.go:1566-1570; credentials.go:557-581
#   resolveCredentialForRootGlobalFirst reads storedCredentialValue(.env) and
#   nothing else; paths.go:389-402 UserCredentialsPath = Home()/.env). The
#   process environment is NOT consulted at run time. So a literal key has to
#   sit in a file: ferry keeps it in <ferry home>/.env, mode 0600, and the
#   wrapper rewrites it from the 0600 ~/.config/ferry/agents.key on every
#   launch, so a rotated key is picked up and nothing carries it in argv or in
#   a world-readable file. config.toml holds only api_key_env = "FERRY_API_KEY"
#   (reasonix.example.toml:4-5: "Never put API key values here").
#   REASONIX_CREDENTIALS_STORE=file keeps it off the OS keyring
#   (credentials.go:136-138).
#
# (3) Custom headers: yes. `headers = { "X-Ferry-Client" = "..." }` on the
#   provider (provider_entry.go:46) is applied to every streamed chat POST
#   (internal/model/openai/chat_headers.go:19 applyCustomHeaders) and to the
#   model-list GET (internal/model/openai/fetch_models.go:124). Only
#   authorization, content-type, accept and host are refused
#   (openai.go:401-408 reservedCustomHeader). ferry sends X-Ferry-Client (and
#   X-Ferry-Fleet when set).
#
# (4) API: OpenAI chat/completions, streamed (Accept: text/event-stream,
#   chat_headers.go:18). Auth is `Authorization: Bearer <key>` (openai.go:
#   365-375 applyAPIKeyHeader; only a MiMo host uses `api-key`).
#
# (5) Least invasive wiring: the wrapper exports REASONIX_HOME (ferry's own dir
#   ~/.config/ferry/reasonix/home) and REASONIX_CREDENTIALS_STORE=file and
#   execs `reasonix`. The ferry home holds config.toml (one `ferry` provider
#   listing every served lane as a model, default = LANE) and .env.
#   Limits, stated plainly: (a) the ferry home is self-contained, so
#   `reasonix-ferry` has its own sessions, skills, slash commands and hooks
#   (docs/CONFIG_PATHS.md:35-47 lists what lives in the home); plain
#   `reasonix` keeps yours. (b) A project reasonix.toml that declares its own
#   providers still loads. (c) Reasonix is unmaintained since 2026-11-12.

_reasonix_help() {
  cat <<'EOF'
ferry reasonix - point Reasonix (esengine/DeepSeek-Reasonix) at the ferry endpoint.

Usage:
  ferry reasonix [--host H] [--port P] [--key K] [--model LANE] [--keep N]

  Writes a ferry-owned Reasonix home (~/.config/ferry/reasonix/home: config.toml
  with a 'ferry' OpenAI-compatible provider listing every lane as a model and
  the X-Ferry-Client header, plus a 0600 .env for the key) and the
  `reasonix-ferry` wrapper (~/.local/bin/reasonix-ferry), which sets
  REASONIX_HOME to that dir and refreshes the key from ~/.config/ferry/agents.key
  at run time. Plain `reasonix` and ~/.reasonix are never edited.
  Run `reasonix-ferry` (or `reasonix-ferry run -p "prompt"`).
  Limit: the ferry home is separate, so reasonix-ferry has its own sessions,
  skills and hooks. Reasonix itself is unmaintained since 2026-11-12.
EOF
}

_reasonix_wire() {
  _agent_python <<'PYEOF'
import json, os, shlex

RHOME = os.path.join(HOME, ".config", "ferry", "reasonix", "home")
CFG = os.path.join(RHOME, "config.toml")
ENV = os.path.join(RHOME, ".env")
WRAP = os.path.join(HOME, ".local", "bin", "reasonix-ferry")

def q(s):                       # TOML basic string (JSON escapes are a subset)
    return json.dumps(str(s))

hdrs = ", ".join(f"{q(k)} = {q(v)}" for k, v in HEADERS.items())
toml = "\n".join([
    f"# {MARKER}",
    "# Written by `ferry reasonix`; `ferry reasonix --unwire` removes it.",
    "# The key lives in the 0600 .env next to this file, never here.",
    "",
    'default_model = "ferry"',
    "",
    "[[providers]]",
    'name        = "ferry"',
    'kind        = "openai"',
    f"base_url    = {q(BASE)}",
    "models      = [" + ", ".join(q(l) for l in LANES) + "]",
    f"default     = {q(LANE)}",
    'api_key_env = "FERRY_API_KEY"',
    f"headers     = {{ {hdrs} }}",
    "",
])

# Wrapper first: it refuses (and exits) before anything else is written when
# a user file sits at the wrapper path.
pre = "\n".join([
    f"_ferry_rhome={shlex.quote(RHOME)}",
    'mkdir -p "$_ferry_rhome"',
    '( umask 077; print -r -- "FERRY_API_KEY=$FERRY_KEY" > "$_ferry_rhome/.env" )',
    'chmod 600 "$_ferry_rhome/.env"',
])
wrapper(WRAP, {"REASONIX_HOME": shlex.quote(RHOME),
               "REASONIX_CREDENTIALS_STORE": "file"},
        "reasonix", pre=pre)

write_owned(CFG, toml, 0o644)

# .env: ferry-created, 0600, key only. Tracked as an "edited" kind so the
# unwire hook removes it whatever key it holds by then (the wrapper rewrites
# it), then drops the home dir if nothing else is in it.
key = open(KEYFILE).read().strip() if os.path.exists(KEYFILE) else "local"
if os.path.exists(ENV) and "FERRY_API_KEY" not in open(ENV).read():
    raise SystemExit(f"    ERROR: {ENV} exists and is not ferry's; leaving it alone.")
os.makedirs(RHOME, exist_ok=True)
fd = os.open(ENV + ".ferry-tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as f:
    f.write(f"FERRY_API_KEY={key}\n")
os.chmod(ENV + ".ferry-tmp", 0o600)
os.replace(ENV + ".ferry-tmp", ENV)
note_edited(ENV, "reasonix-env", home=RHOME)

print(f"    Reasonix home:  {RHOME}")
print(f"    Wrapper:        {WRAP}")
print(f"    Provider:       ferry -> {BASE} (lane {LANE})")
print("    Run `reasonix-ferry`; plain `reasonix` is untouched.")
save_record()
PYEOF
}

# --unwire hook for the .env ferry created. Receives the edit as JSON:
# {"path": ".../.env", "kind": "reasonix-env", "home": "<ferry home>"}.
_reasonix_unwire() {
  local edit="$1"
  python3 - "$edit" "$AG_DRY" <<'PYEOF'
import json, os, sys
e = json.loads(sys.argv[1])
dry = sys.argv[2] == "1"
p, home = e["path"], e.get("home", "")
if dry:
    print(f"    [dry-run] would remove {p}", file=sys.stderr)
    sys.exit(0)
try:
    os.remove(p)
    print(f"    Removed {p}", file=sys.stderr)
except FileNotFoundError:
    pass
# The home goes only when nothing else is in it (a session the user made
# under reasonix-ferry keeps it, with a note).
try:
    os.rmdir(home)
    print(f"    Removed {home}", file=sys.stderr)
except OSError:
    if home and os.path.isdir(home):
        print(f"    Kept {home}: reasonix-ferry created sessions or files in it.",
              file=sys.stderr)
PYEOF
}

cmd_reasonix() { _agent_main reasonix "$@"; }
