# ferry crush - point Crush (charmbracelet/crush, Go) at the ferry endpoint.
#
# Wiring: a ferry-owned config DIRECTORY plus a `crush-ferry` wrapper. Plain
# `crush` is never touched: ~/.config/crush/crush.json, ~/.local/share/crush/
# crush.json and every project crush.json stay byte for byte as the user left
# them, and no snapshot is needed because ferry never opens them.
#
# Research (clone: github.com/charmbracelet/crush @ db7c905f, main on
# 2026-11-20; the e2e runs the v0.102.0 release binary, same loader):
#
# (1) Where provider config lives; base URLs.
#   internal/config/load.go:1064 (lookupConfigs) loads, low to high priority:
#     systemConfigPath, GlobalConfig(), crushrc sibling, GlobalConfigData(),
#     then project .crushrc / crushrc / .crush.json / crush.json found from
#     cwd up to the git root; later wins on conflict (load.go:1068-1096).
#   GlobalConfig() = $CRUSH_GLOBAL_CONFIG/crush.json, else $XDG_CONFIG_HOME
#     (else ~/.config)/crush/crush.json (load.go:1360, internal/home/home.go:50).
#   GlobalConfigData() = $CRUSH_GLOBAL_DATA/crush.json, else
#     $XDG_DATA_HOME/crush/crush.json, else ~/.local/share/crush/crush.json
#     (load.go:1407). It is "machine-owned" state Crush itself rewrites when
#     you pick a model, and it merges AFTER the user's global file.
#   CRUSH_GLOBAL_CONFIG would REPLACE the user's global file (their providers
#     would vanish), so ferry points CRUSH_GLOBAL_DATA instead: the user's
#     global config still loads, ferry's file merges over it.
#   A `providers.<id>` entry has `type`, `base_url`, `api_key`, `extra_headers`,
#     `models` (internal/config/config.go:99-151). `type: openai-compat` is
#     normalised to the chat-completions endpoint (load.go:46, 632-634); an
#     `anthropic` type would use /v1/messages. Both are OpenAI/Anthropic base
#     URL style (base_url includes /v1). ferry uses openai-compat.
#
# (2) Key. `api_key` goes through the shell-style resolver (internal/config/
#   resolve.go:74: $VAR, ${VAR}, $(cmd)), resolved at load (load.go:646). ferry
#   writes api_key "$CRUSH_FERRY_KEY"; the wrapper exports that from the 0600
#   ~/.config/ferry/agents.key at run time, so no key is in argv or in the
#   config file.
#
# (3) Custom headers: `extra_headers` (config.go:120), values resolved the same
#   way and sent on every request (load.go:669-678; internal/agent/
#   coordinator.go:1554). ferry sends X-Ferry-Client (and X-Ferry-Fleet).
#
# (4) API: OpenAI chat/completions for type openai-compat (POST {base_url}/
#   chat/completions, streamed). Crush also GETs {base_url}/models to discover
#   models (config.go:148 discover_models, default on); ferry serves that as
#   the lane catalogue and lists every lane explicitly as well.
#
# (5) Default model. models.large / models.small select {provider, model}
#   (config.go:857, load.go:984-990). ferry's data file sets both to the ferry
#   lane, else Crush falls back to the first known provider with a key in env.
#   `options.disable_provider_auto_update` keeps the wrapper from fetching the
#   catwalk catalogue over the net (config.go:471). Limit: a project
#   .crush.json that sets models.* overrides this (project files merge last),
#   and with CRUSH_CLIENT_SERVER=1 a server already started by plain `crush`
#   keeps its own env (internal/cmd/root.go:280).

_crush_help() {
  cat <<'EOF'
ferry crush - point Crush (charmbracelet/crush) at the ferry endpoint.

Usage:
  ferry crush [--host H] [--port P] [--key K] [--model LANE] [--keep N]

  Writes a ferry-owned Crush config dir (~/.config/ferry/crush/crush.json: a
  'ferry' provider with every lane as a model, X-Ferry-Client header, default
  model = LANE) and the `crush-ferry` wrapper (~/.local/bin/crush-ferry). The
  wrapper points Crush at that dir with CRUSH_GLOBAL_DATA and passes the key as
  $CRUSH_FERRY_KEY at run time. Plain `crush` and your own crush.json files are
  never edited. Run `crush-ferry` (or `crush-ferry run "prompt"`).
  Limit: a project .crush.json that sets models.* overrides the default lane.
EOF
}

_crush_wire() {
  _agent_python <<'PYEOF'
import json, os, shlex

DDIR = os.path.join(HOME, ".config", "ferry", "crush")
CFG = os.path.join(DDIR, "crush.json")
WRAP = os.path.join(HOME, ".local", "bin", "crush-ferry")

models = [{"id": l, "name": f"ferry {l}", "context_window": 200000,
           "default_max_tokens": 16384} for l in LANES]
provider = {"name": "ferry", "type": "openai-compat", "base_url": BASE,
            "api_key": "$CRUSH_FERRY_KEY", "extra_headers": dict(HEADERS),
            "models": models}

cur = load_json(CFG)
if cur is None:
    raise SystemExit(f"    ERROR: {CFG} is not valid JSON; leaving it alone.")
if cur and cur.get("_ferry") != MARKER:
    raise SystemExit(f"    ERROR: {CFG} exists and is not ferry-managed; "
                     "leaving it alone. Move it aside and re-run.")
cur["_ferry"] = MARKER
cur.setdefault("providers", {})["ferry"] = provider
sel = {"provider": "ferry", "model": LANE}
mods = cur.setdefault("models", {})
for slot in ("large", "small"):
    old = mods.get(slot) or {}
    # keep the tuning Crush saved for a ferry selection, never another provider's
    mods[slot] = {**(old if old.get("provider") == "ferry" else {}), **sel}
cur.setdefault("options", {})["disable_provider_auto_update"] = True
dump_json(CFG, cur, 0o600)
note_edited(CFG, "crush-data", dir=DDIR)

wrapper(WRAP,
        {"CRUSH_GLOBAL_DATA": shlex.quote(DDIR),
         "CRUSH_FERRY_KEY": '"$FERRY_KEY"'},
        "crush")
print(f"    Crush config:   {CFG}")
print(f"    Wrapper:        {WRAP}  (run `crush-ferry`; plain `crush` is untouched)")
save_record()
PYEOF
}

# Hook for the generic unwire: the data file is shared with Crush itself (it
# rewrites it when the user picks a model), so remove only ferry's members and
# delete the file/dir only when nothing else is left in it.
_crush_unwire() {
  python3 - "$1" "${AG_DRY:-0}" <<'PYEOF'
import json, os, sys
e = json.loads(sys.argv[1]); dry = sys.argv[2] == "1"
p = e["path"]
try:
    d = json.load(open(p))
except Exception:
    print(f"    Kept {p}: no longer valid JSON; remove ferry's entry by hand.")
    sys.exit(0)
if dry:
    print(f"    [dry-run] would remove ferry's members from {p}")
    sys.exit(0)
d.pop("_ferry", None)
(d.get("providers") or {}).pop("ferry", None)
for sect in ("models", "recent_models"):
    m = d.get(sect)
    if isinstance(m, dict):
        for k in list(m):
            v = m[k]
            if isinstance(v, dict) and v.get("provider") == "ferry":
                del m[k]
            elif isinstance(v, list):
                m[k] = [x for x in v if not (isinstance(x, dict) and x.get("provider") == "ferry")]
                if not m[k]:
                    del m[k]
opts = d.get("options")
if isinstance(opts, dict):
    opts.pop("disable_provider_auto_update", None)
for k in ("providers", "models", "recent_models", "options"):
    if k in d and d[k] == {}:
        del d[k]
if d == {}:
    os.remove(p)
    try:
        os.rmdir(os.path.dirname(p))
    except OSError:
        pass
    print(f"    Removed {p}")
else:
    with open(p + ".ferry-tmp", "w") as f:
        json.dump(d, f, indent=2); f.write("\n")
    os.chmod(p + ".ferry-tmp", os.stat(p).st_mode & 0o777)
    os.replace(p + ".ferry-tmp", p)
    print(f"    Removed ferry's members from {p} (other keys kept)")
PYEOF
}

cmd_crush() { _agent_main crush "$@"; }
