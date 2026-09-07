# _ferry_install_opencode_guardrails — put the local-lane guardrails where
# opencode will actually read them: the /fan-out command and the
# spawning-subagents skill.
#
# These shipped ONLY in client-bootstrap.sh, so every CLIENT got them and the
# HOST never did — even though `ferry opencode` deliberately wires the host to
# its own endpoint, so the host drives local lanes exactly like a client does.
# On this host that meant the documented mitigation for malformed/looping `task`
# calls had never been installed on the machine reporting the problem.
#
# opencode documents these as `~/.config/opencode/command(s)/<name>.md` and
# `~/.config/opencode/skill(s)/<name>/SKILL.md` — both spellings are accepted,
# and both are GLOBAL paths, independent of $OPENCODE_CONFIG. So this installs
# to the stock location even on a host whose config lives elsewhere.
#
# Source of truth is the repo (opencode/command, opencode/skills). On a client
# there is no checkout, so this no-ops and client-bootstrap.sh's embedded copies
# remain the client path.
_ferry_install_opencode_guardrails() {
  local src_cmd="$APP_DIR/opencode/command/fan-out.md"
  local src_skill="$APP_DIR/opencode/skills/spawning-subagents/SKILL.md"
  if [[ ! -f "$src_cmd" || ! -f "$src_skill" ]]; then
    return 0   # no checkout here (a client) — nothing to install from
  fi
  local dst_cmd="$HOME/.config/opencode/command"
  local dst_skill="$HOME/.config/opencode/skill/spawning-subagents"
  mkdir -p "$dst_cmd" "$dst_skill"
  cp "$src_cmd"   "$dst_cmd/fan-out.md"
  cp "$src_skill" "$dst_skill/SKILL.md"
  echo ">>> opencode guardrails installed:"
  echo "    $dst_cmd/fan-out.md"
  echo "    $dst_skill/SKILL.md"
  echo "    (the recipe must ride in the USER message — that is what /fan-out does;"
  echo "     putting it in system instructions measured WORSE.)"
}

# _ferry_install_host_wrappers — put the `opencode-cloud` / `opencode-local` /
# `opencode-super` shell functions in the HOST's ~/.zshrc.
#
# The same gap as the guardrails above, one layer out: the wrappers were written
# ONLY by client-bootstrap.sh, so every client got them and the host never did.
# Confirmed by absence rather than assumed — host-bootstrap.sh contains no
# occurrence of "opencode" at all, and host-reset.sh writes the profile JSONs
# but never touches ~/.zshrc. So the host ended up with the FILES the wrappers
# select between and no way to select between them.
#
# Marker discipline is the whole point. client-bootstrap.sh strips a previous
# block by EXACT string compare on "# >>> ferry opencode profiles >>>", and
# client-cleanup.sh compares the same way. Writing a host block under any other
# marker means neither tool can see it, and the next client bootstrap appends a
# SECOND block defining the same functions (the later definition wins, so the
# duplicate is invisible until the two disagree). This writes the canonical
# marker for exactly that reason, and additionally absorbs the "(host)" variant
# that hand-wiring produced before this function existed.
#
# Named wrappers only, no bare `opencode()`. This matches the client's
# --profiles-only scope: a host that exports OPENCODE_CONFIG has chosen its
# default deliberately, and wrapping bare `opencode` would fight that choice.
FERRY_OC_MARK_START="# >>> ferry opencode profiles >>>"
FERRY_OC_MARK_END="# <<< ferry opencode profiles <<<"

_ferry_install_host_wrappers() {
  local rc="$HOME/.zshrc"
  touch "$rc"

  # Strip the canonical block AND the legacy "(host)" variant, then re-add. Both
  # spellings go, or absorbing the legacy one would just leave two again.
  python3 - "$rc" "$FERRY_OC_MARK_START" "$FERRY_OC_MARK_END" <<'PYEOF'
import sys
rc, start, end = sys.argv[1], sys.argv[2], sys.argv[3]

# The hand-wired spelling this function exists to absorb. Neither
# client-bootstrap.sh nor client-cleanup.sh can match it, because both compare
# marker lines for exact equality.
legacy_start = "# >>> ferry opencode profiles (host) >>>"
legacy_end = "# <<< ferry opencode profiles (host) <<<"

with open(rc) as f:
    lines = f.readlines()

out, skip = [], False
for ln in lines:
    s = ln.rstrip("\n")
    if s in (start, legacy_start):
        skip = True
        continue
    if s in (end, legacy_end):
        skip = False
        continue
    if not skip:
        out.append(ln)

# A stray `alias opencode-cloud=` / `alias opencode-local=` / `alias
# opencode-super=` ABOVE a function of the same name makes zsh expand the alias
# inside `name() {`, which is a parse error on every subsequent
# `source ~/.zshrc`. client-bootstrap.sh strips these for the same reason.
def is_legacy_alias(l):
    t = l.lstrip()
    return (t.startswith("alias opencode-cloud=")
            or t.startswith("alias opencode-local=")
            or t.startswith("alias opencode-super="))

out = [l for l in out if not is_legacy_alias(l)]

while out and out[-1].strip() == "":
    out.pop()
with open(rc, "w") as f:
    f.writelines(out)
    if out:
        f.write("\n")
PYEOF
  if (( $? != 0 )); then
    echo "    WARNING: could not rewrite $rc; leaving the shell wrappers alone." >&2
    return 1
  fi

  # QUOTED heredoc: written verbatim, so the $HOME and $@ inside the functions
  # survive into the file instead of being expanded now.
  cat <<'EOF' >> "$rc"
# >>> ferry opencode profiles >>>
# Installed by `ferry update` / host-reset.sh on the HOST. The profile FILES
# these select between are written by `ferry opencode` in the same pass.
#
# There is deliberately no bare `opencode` function: an explicit OPENCODE_CONFIG
# is the host's own choice and ferry does not override it.
unalias opencode-cloud opencode-local opencode-super 2>/dev/null

# opencode-cloud: heavy drives (build/plan); flash handles `light` (tasks rated
# 0-50) and explore; medium handles `standard` (51-100) when advertised;
# super-flash handles compaction and title/summary. The built-in `general`
# subagent is DISABLED. Older or unreachable hosts put standard on flash too.
opencode-cloud() {
  OPENCODE_CONFIG="$HOME/.config/ferry/opencode-cloud.json" command opencode "$@"
}

# opencode-local: the GPU pair — local-orch drives, local-sub runs the fan-out.
# Nothing leaves this machine.
opencode-local() {
  OPENCODE_CONFIG="$HOME/.config/ferry/opencode-local.json" command opencode "$@"
}

# opencode-super: heavy drives; super-flash runs the fan-out AND the
# housekeeping. The cheapest cloud profile.
opencode-super() {
  OPENCODE_CONFIG="$HOME/.config/ferry/opencode-super.json" command opencode "$@"
}
# <<< ferry opencode profiles <<<
EOF

  echo ">>> opencode shell wrappers installed in $rc:"
  echo "    opencode-cloud   -> cloud lanes: heavy drives; flash light+explore; medium standard; super-flash compaction/title/summary; general disabled"
  echo "    opencode-super   -> cloud pair: heavy drives, super-flash fans out"
  echo "    opencode-local   -> GPU pair:   local-orch drives, local-sub fans out"
  echo "    (bare 'opencode' is untouched — run: source $rc)"
}

cmd_env() {
  # Emit shell 'export' lines so a client routes its downloads through the host's
  # forward proxy (see 'ferry serve-proxy'). Designed for:  eval "$(ferry env ...)".
  # stdout stays PURELY eval-able; any human hint goes to stderr.
  local host="" proxy_port="" hf_port="" do_write=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --host)       host="$2"; shift 2 ;;
      --proxy-port) proxy_port="$2"; shift 2 ;;
      --hf-port)    hf_port="$2"; shift 2 ;;
      --write)      do_write=1; shift ;;
      *)            echo "Unknown option: $1" >&2; exit 1 ;;
    esac
  done

  # Resolve the host: --host wins, else the client profile's CLIENT_HOST.
  local H="${host:-$CLIENT_HOST}"
  if [[ -z "$H" ]]; then
    echo "Error: no host given. Pass --host H, or configure ~/.config/ferry/client.json first." >&2
    exit 1
  fi
  local PP="${proxy_port:-$PROXY_PORT}"
  local HFP="${hf_port:-$HF_PORT}"

  # The export block. Built with an expanding heredoc so $H/$PP/$HFP interpolate.
  local block
  block=$(cat <<EOF
export HTTP_PROXY="http://$H:$PP"
export HTTPS_PROXY="http://$H:$PP"
export http_proxy="http://$H:$PP"
export https_proxy="http://$H:$PP"
export ALL_PROXY="http://$H:$PP"
export HF_ENDPOINT="http://$H:$HFP"
export NO_PROXY="localhost,127.0.0.1,::1,$H"
export no_proxy="localhost,127.0.0.1,::1,$H"
EOF
)

  if (( do_write )); then
    local rc="$HOME/.zshrc"
    local start="# >>> ferry env >>>"
    local end="# <<< ferry env <<<"
    touch "$rc"
    # Strip any existing ferry env block (inclusive of its markers) before re-adding.
    python3 - "$rc" "$start" "$end" <<'PYEOF'
import sys
rc, start, end = sys.argv[1], sys.argv[2], sys.argv[3]
with open(rc) as f:
    lines = f.readlines()
out, skip = [], False
for ln in lines:
    s = ln.rstrip("\n")
    if s == start:
        skip = True
        continue
    if s == end:
        skip = False
        continue
    if not skip:
        out.append(ln)
while out and out[-1].strip() == "":
    out.pop()
with open(rc, "w") as f:
    f.writelines(out)
    if out:
        f.write("\n")
PYEOF
    {
      echo "$start"
      echo "$block"
      echo "$end"
    } >> "$rc"
    echo ">>> Appended ferry env block to $rc (host $H). Run: source $rc"
  else
    print -r -- "$block"
    echo "# eval \"\$(ferry env)\" then run your uv/hf tool (downloads route via $H)" >&2
  fi
}

# _ferry_preinstall_goal_plugin — install the goal plugin NOW, through the
# production loader, so a broken spec surfaces at `ferry opencode` time instead
# of never.
#
# `opencode plugin '<spec>' --global` is the ONLY entry point that prints the
# real install error. Inside a normal opencode start a plugin failure is
# published as a Session event and never logged (packages/opencode/src/plugin/
# index.ts:198-201 -> :139-141, with no-op start/missing reporters at :191-192),
# the entry is dropped (loader.ts:234) and npm-source plugins are never retried
# (loader.ts:178) — which is exactly how v1.29.4's unloadable spec survived a
# release. "The cache directory has files in it" is not evidence of anything:
# the whole failure mode is a complete package on disk that opencode discards.
#
# The command also PATCHES a config to add the plugin, which we do not want —
# ferry has already written the config — so it runs against a throwaway
# XDG_CONFIG_HOME/XDG_DATA_HOME/XDG_STATE_HOME with OPENCODE_CONFIG unset, and
# its config patch lands in the sandbox. XDG_CACHE_HOME is deliberately NOT
# overridden: the package cache is the shared real one, and populating it is the
# entire point of doing this early.
#
# 120 s wall clock, without `timeout` (macOS ships none) and without `kill -0`
# (a finished background child stays a zombie until `wait`, so kill -0 reports
# it alive for the whole budget). A completion sentinel file is the observable.
#
# NEVER fails the caller: the config was written correctly either way, and an
# offline laptop must not turn a good bootstrap into a red one.
_ferry_preinstall_goal_plugin() {
  local spec="$1" ref="$2" pkg="$3"
  local sandbox log rcfile pid rc=124 waited=0
  sandbox="$(mktemp -d -t ferry-goal-oc)" || return 0
  log="$sandbox/install.log"
  rcfile="$sandbox/rc"
  echo "    Plugin:  pre-installing $spec"
  # ferry runs under `set -eu`. A FAILING install is the whole point of this
  # pass, so every step that can legitimately return non-zero — the install
  # itself, the kill after a timeout, and the wait on a child that exited 1 —
  # has to be shielded, or errexit tears the command down before it can report.
  (
    local child_rc=0
    unset OPENCODE_CONFIG
    export XDG_CONFIG_HOME="$sandbox/config"
    export XDG_DATA_HOME="$sandbox/data"
    export XDG_STATE_HOME="$sandbox/state"
    mkdir -p "$XDG_CONFIG_HOME" "$XDG_DATA_HOME" "$XDG_STATE_HOME"
    opencode plugin "$spec" --global >"$log" 2>&1 || child_rc=$?
    print -r -- "$child_rc" >"$rcfile"
  ) &
  pid=$!
  while (( waited < 120 )); do
    if [[ -f "$rcfile" ]]; then
      break
    fi
    sleep 1
    waited=$(( waited + 1 ))
  done
  if [[ -f "$rcfile" ]]; then
    rc="$(<"$rcfile")"
  else
    pkill -P "$pid" >/dev/null 2>&1 || true
    kill -TERM "$pid" >/dev/null 2>&1 || true
  fi
  wait "$pid" >/dev/null 2>&1 || true

  # Verify against the CACHE, not against the exit code: a zero exit with an
  # empty node_modules is the interrupted-install state opencode never heals.
  python3 - "$spec" "$ref" "$pkg" "$rc" "$log" <<'PYEOF'
import json, os, sys

spec, ref, pkg, rc_raw, log = sys.argv[1:6]
try:
    rc = int(rc_raw)
except ValueError:
    rc = 1

base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
# Same formula opencode uses: path.join(cache, "packages", <raw spec>), with the
# spec's slashes acting as path separators and "//" collapsed by the join.
directory = os.path.normpath(os.path.join(base, "opencode", "packages", spec))
root = os.path.join(directory, "node_modules", pkg)
manifest = os.path.join(root, "package.json")
want = ref[1:] if ref.startswith("v") else ref

problems, version = [], None
if rc == 124:
    problems.append("`opencode plugin` did not finish within 120s")
elif rc != 0:
    problems.append(f"`opencode plugin` exited {rc}")
if os.path.exists(manifest):
    try:
        version = json.load(open(manifest)).get("version")
    except Exception as e:
        problems.append(f"{manifest} is unreadable ({e})")
else:
    problems.append(f"missing {manifest}")
if version and version != want:
    problems.append(f"cache holds version {version}, expected {want}")
# Both halves: the server plugin AND the tui sidebar module. A package missing
# dist/goal-tui.js loads the /goal command and no sidebar, which is exactly the
# kind of half-success that reads as working.
for half in ("dist/goal-plugin.js", "dist/goal-tui.js"):
    if not os.path.exists(os.path.join(root, half)):
        problems.append(f"missing {half}")

if not problems:
    print(f"    Plugin:  installed {pkg} {version} (ready for next opencode start)")
else:
    print(f"    WARNING: the goal plugin is NOT installed: {'; '.join(problems)}")
    try:
        tail = [l.rstrip() for l in open(log).read().splitlines() if l.strip()][-6:]
    except OSError:
        tail = []
    for line in tail:
        print(f"             {line}")
    print("             opencode will retry on next start.")
PYEOF

  rm -rf "$sandbox"
  return 0
}

cmd_opencode() {
  # [Client] Take this machine's opencode config over so EVERY agent routes
  # through the host's ferry endpoint, addressed by LANE NAME only.
  #
  # A ferry client knows agent lanes, never a real model:
  #
  #   driver       build / plan                      heavy       local-orch
  #   light        light   (complexity 0-50)         flash       local-sub
  #   standard     standard (complexity 51-100)      medium*     local-sub
  #   explore      explore                           flash       local-sub
  #   compaction   compaction                        super-flash local-sub
  #   housekeeper  title / summary                   super-flash local-sub
  #
  # opencode's built-in `general` subagent is DISABLED — the fan-out worker is
  # split into two custom subagents banded by complexity, because opencode's
  # task tool has no model parameter: the driver picks an AGENT NAME, and each
  # agent is pinned to exactly one lane here.
  #
  # * `medium` is used only when the host catalogue advertises it. Older or
  # unreachable hosts retain flash for standard rather than receiving a broken
  # new lane reference.
  #
  # Compaction fires on its own schedule and carries the ENTIRE transcript, so
  # cloud uses the super-flash housekeeping lane. On the GPU pair there is no
  # third lane, so all non-driver agents share local-sub.
  #
  # A real model id must NEVER reach a client config. The host re-points a lane
  # whenever the economics change; a client that named the model would keep
  # asking for something the catalogue no longer advertises. Which model sits
  # behind a lane is the host's business and is not discoverable from here.
  #
  # TAKEOVER, not merge. Four keys are ferry's and get replaced outright:
  #   permission  -> "allow"
  #   model       -> ferry/<driver>
  #   small_model -> ferry/<housekeeper>
  #   agent       -> six built-ins pinned, `general` disabled, and the two
  #                  custom `light`/`standard` subagents declared (see the
  #                  AGENTS lists below)
  # provider.ferry.options.headers is ours too, rewritten every run alongside
  # baseURL/apiKey (see the prov["ferry"] block below) - it carries this
  # machine's identity and a one-shot fleet override, never a real model id.
  # `plugin` gets the goal plugin appended only when no entry already IS that
  # plugin - which includes a LOCAL PATH to a fork of it, since opencode accepts
  # a filesystem path and a private fork can only be named that way. The spec is
  # the NAME-PREFIXED TARBALL form
  # `opencode-goal-plugin@https://github.com/.../vX.Y.Z.tar.gz`, because on
  # opencode 1.18.29 a bare `github:` spec installs to disk and is then silently
  # discarded, and any git spec dies in pacote's prepare step - see the
  # GOAL_PLUGIN comment block for the file:line trace. Every earlier spelling
  # ferry ever wrote is rewritten to it on every run, and the ref is pinned
  # because the spec string IS opencode's cache key.
  # The same entry is MIRRORED into ~/.config/opencode/tui.json, which is the
  # only place opencode reads TUI-half plugins from - opencode.json's `plugin`
  # array feeds the server loader alone, so the plugin's sidebar panel never
  # appears without it. Only the full-takeover target gets a tui.json:
  # --config paths outside ~/.config/opencode (the ferry lane profiles) never do.
  # `command.goal` is MERGED in (never taken over) so the plugin's /goal slash
  # command exists; a user's own `goal` and every other command are left alone.
  # Every OTHER key in the
  # file is left exactly as it was, and the whole original is snapshotted to
  # <name>.<UTC>.jsonc first, so a takeover is always reversible.
  # Afterwards the orphaned per-spec cache directories are removed and the plugin
  # is pre-installed through `opencode plugin`, so a broken spec is visible here
  # rather than silently absent at runtime (--keep-cache / --no-install opt out).
  local oc_host="${CLIENT_HOST:-}" oc_port="${CLIENT_PORT:-8090}"
  # v1.22.0: the front door can run litellm behind a master_key. The bearer
  # baked into the generated configs (and sent on the catalogue check) resolves
  # --key > client.json's master_key (boot-loaded as CLIENT_MASTER_KEY) > unset
  # (the legacy 'local' token, so keyless LAN setups are unchanged). The key is
  # only written into files / request headers, never printed.
  local oc_key=""
  # opencode resolves its config from $OPENCODE_CONFIG when that is set, so
  # honour it here too. Writing the hardcoded default on a machine that sets
  # OPENCODE_CONFIG edits a file opencode never reads: the command reports
  # success, and nothing changes.
  local oc_config="${OPENCODE_CONFIG:-$HOME/.config/opencode/opencode.json}"
  local force_model="" force_small="" force_house="" set_default=1 prefer_local=0 force_write=0 keep_snaps=10
  # tui.json: the TUI half of the goal plugin is loaded from tui.json ONLY -
  # opencode.json's `plugin` array is never read by the TUI plugin loader
  # (packages/opencode/src/config/tui.ts:157-210). "auto" means "mirror the
  # entry when the config we are writing IS the global takeover target".
  local tui_mode="auto" tui_path="${OPENCODE_TUI_CONFIG:-}"
  local keep_cache=0 do_install=1

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --host)         oc_host="$2"; shift 2 ;;
      --port)         oc_port="$2"; shift 2 ;;
      --config)       oc_config="$2"; shift 2 ;;
      --key)          oc_key="$2"; shift 2 ;;
      --model)        force_model="$2"; shift 2 ;;
      --small-model)  force_small="$2"; shift 2 ;;
      --housekeeper)  force_house="$2"; shift 2 ;;
      # --super: the cheapest cloud profile — heavy still drives, super-flash
      # takes every non-driver agent. Set here at parse time so a later
      # --small-model / --housekeeper overrides its respective agent group.
      --super)        force_small="super-flash"; force_house="super-flash"; shift ;;
      --keep)         keep_snaps="$2"; shift 2 ;;
      # Where the TUI half of the goal plugin gets listed. An explicit path is
      # always honoured; --no-tui-config suppresses the mirror entirely.
      --tui-config)   tui_mode="explicit"; tui_path="$2"; shift 2 ;;
      --no-tui-config) tui_mode="off"; tui_path=""; shift ;;
      # Leave ~/.cache/opencode/packages/ alone (see the purge block below).
      --keep-cache)   keep_cache=1; shift ;;
      # Skip the `opencode plugin` pre-install/verify pass after the write.
      --no-install)   do_install=0; shift ;;
      --no-default)   set_default=0; shift ;;
      --local)        prefer_local=1; shift ;;
      # Retained for compatibility: --force used to bypass a refusal to rewrite
      # a commented (JSONC) config. That refusal is gone — the snapshot keeps the
      # original verbatim, comments included — so the flag is now a no-op.
      --force)        force_write=1; shift ;;
      --cloud)        prefer_local=0; shift ;;
      # Install the ~/.zshrc wrappers and nothing else. host-reset.sh calls this
      # once, after writing the profile files the wrappers select between —
      # doing it inside the normal path would re-run it once per config.
      --wrappers)     _ferry_install_host_wrappers; return $? ;;
      *) echo "Unknown option for 'ferry opencode': $1"; exit 1 ;;
    esac
  done

  if [[ -z "$oc_host" ]]; then
    if (( CLIENT_MODE )); then
      # A bootstrapped client whose profile exists but carries no host.
      echo "Error: ~/.config/ferry/client.json has no 'host'. Re-run the client"
      echo "bootstrap, or pass --host <mdns-or-ip> explicitly."
      exit 1
    fi
    # HOST: the proxy is on this very machine, so loopback is the right default.
    # Wiring the host to its own endpoint is the point of running one — every
    # local tool then shares the lanes, the fallback chain, and the observability,
    # and no tool on this box needs its own copy of a provider key.
    oc_host="127.0.0.1"
    echo ">>> No --host and no client profile: this is the HOST, so wiring it to its"
    echo "    own proxy at http://127.0.0.1:$oc_port/v1."
  fi

  # Flag wins over the boot-loaded profile key; both unset keeps the legacy token.
  [[ -z "$oc_key" ]] && oc_key="${CLIENT_MASTER_KEY:-}"

  # The canonical plugin spec lives in ONE place (the Python block below). It is
  # handed back through this scratch file so the pre-install pass cannot drift
  # from the string that was actually written into the config.
  local oc_specfile; oc_specfile="$(mktemp -t ferry-goal-spec)"

  python3 - "$oc_host" "$oc_port" "$oc_config" "$force_model" "$force_small" "$set_default" "$prefer_local" "$force_write" "$keep_snaps" "$force_house" "$oc_key" "$CLIENT_NAME" "$keep_cache" "$tui_mode" "$tui_path" "$oc_specfile" <<'PYEOF'
import datetime, json, os, re, sys, shutil, urllib.request

host, port, cfg_path, force_model, force_small = sys.argv[1:6]
set_default  = sys.argv[6] == "1"
prefer_local = sys.argv[7] == "1"
force_write  = sys.argv[8] == "1"   # no-op; see the --force note above
keep_snaps   = int(sys.argv[9])
force_house  = sys.argv[10]
oc_key       = sys.argv[11]
client_name  = sys.argv[12]
keep_cache   = sys.argv[13] == "1"
tui_mode     = sys.argv[14]         # auto | explicit | off
tui_path     = sys.argv[15]
spec_out     = sys.argv[16]
cfg_path = os.path.expanduser(cfg_path)
base = f"http://{host}:{port}/v1"

SCHEMA = "https://opencode.ai/config.json"
TUI_SCHEMA = "https://opencode.ai/tui.json"

# --- The goal plugin spec, and why it is spelled EXACTLY like this. ---
#
# opencode 1.18.29 has TWO independent defects that make the obvious spellings
# install-to-disk-but-never-load, with nothing in any log:
#
#   1. NO NPM NAME. For a bare `github:owner/repo[#tag]` or a bare tarball URL,
#      npm-package-arg returns no name, so `npa(pkg).name ?? pkg`
#      (packages/core/src/npm.ts:119) yields the WHOLE RAW SPEC as the "name".
#      After a perfectly successful reify, `tree.edgesOut` is empty, so
#      npm.ts:130-134 falls back to resolveEntryPoint(<raw spec>), which cannot
#      resolve, and throws NpmInstallFailedError - with the package fully
#      written to disk. The failure is published as a Session event and never
#      logged (plugin/index.ts:198-201 -> :139-141; the start/missing reporters
#      at :191-192 are no-ops), the entry is dropped (loader.ts:234) and npm
#      plugins are never retried (loader.ts:178). v1.29.4's pin
#      `github:sblattj/OpenCode-goal-plugin#v0.9.1` was dead on arrival, and so
#      was the unpinned spelling before it: the cache directory filled with a
#      complete package that opencode then threw away on every single start.
#   2. GIT PREPARE. Any GIT spec whose package.json declares any of
#      postinstall/build/preinstall/install/prepack/prepare makes pacote's
#      GitFetcher run a "prepare" step (pacote/lib/git.js:160-197) - arborist's
#      `ignoreScripts: true` does NOT suppress it. opencode derives npmBin from
#      `fileURLToPath(new URL("..", import.meta.url))`
#      (packages/core/src/npm-config.ts:10), which inside the bun single-file
#      binary is `/$bunfs/bin/npm-cli.js`; because that ends in `.js`, pacote
#      spawns process.execPath - i.e. opencode ITSELF - with it as argv[1]
#      (pacote/lib/util/npm.js:5-7). The child prints opencode's own help, exits
#      1, and the install dies with "git dep preparation failed". The plugin has
#      declared `build` + `prepack` since v0.9.1, so every git form is poisoned.
#
# The one form that installs AND loads is NAME-PREFIXED + REMOTE TARBALL. The
# `name@` prefix gives npa a real name, which fixes both the entrypoint
# resolution and the cache short-circuit (defect 1); a remote tarball is fetched
# by pacote's RemoteFetcher, which never enters the git prepare path at all, so
# it is IMMUNE to defect 2 rather than merely dodging its trigger. Verified end
# to end against opencode 1.18.29.
GOAL_PLUGIN_PKG = "opencode-goal-plugin"
GOAL_PLUGIN_REPO = "sblattj/OpenCode-goal-plugin"
# PIN THE REF, and bump it on every release that ships a new plugin version.
# opencode installs a plugin into ~/.cache/opencode/packages/<the spec string>/
# and, if node_modules/<pkg> already exists in that directory, returns
# IMMEDIATELY without refetching - no TTL, no version compare, no eviction
# (packages/core/src/npm.ts:79,125-127). An UNPINNED spec therefore freezes a
# machine at whatever it fetched first: hosts sat on plugin 0.9.0 for weeks
# while the fork's HEAD was 0.9.1. The spec string IS the cache key, so a NEW
# ref means a NEW directory and a guaranteed fresh install. That is why the ref
# is pinned here and why cutting a plugin release means bumping GOAL_PLUGIN_REF.
GOAL_PLUGIN_REF = "v0.10.1"
GOAL_PLUGIN_URL = (f"https://github.com/{GOAL_PLUGIN_REPO}"
                   f"/archive/refs/tags/{GOAL_PLUGIN_REF}.tar.gz")
# Current spec, spelled out for grep:
# opencode-goal-plugin@https://github.com/sblattj/OpenCode-goal-plugin/archive/refs/tags/v0.10.1.tar.gz
GOAL_PLUGIN = f"{GOAL_PLUGIN_PKG}@{GOAL_PLUGIN_URL}"
# Kept for MATCHING configs written by older ferries; never written any more.
GOAL_PLUGIN_BASE = f"github:{GOAL_PLUGIN_REPO}"
LEGACY_GOAL_PLUGINS = {
    "@prevalentware/opencode-goal-plugin",
    "opencode-goal-plugin",
    "willytop8/opencode-goal-plugin",
    "github:willytop8/opencode-goal-plugin",
    "sblattj/opencode-goal-plugin",
}
# Substrings that identify OUR plugin inside any spec shape a previous ferry (or
# a hand edit) could have written: `github:owner/repo`, `owner/repo#ref`,
# `git+https://github.com/owner/repo.git`, an `archive/refs/tags/*.tar.gz` URL.
# Matched case-insensitively because the repo is CamelCase and half the historic
# spellings are not. Never applied to a local filesystem path - see is_goal_plugin.
GOAL_REPO_MARKERS = ("sblattj/opencode-goal-plugin", "willytop8/opencode-goal-plugin")
# The package's own directory name, used to recognise a LOCAL PATH pointing at
# the same plugin. opencode accepts a filesystem path as a plugin entry, and Bun
# cannot resolve a PRIVATE repo over `github:` - so a hard fork of this plugin
# can only be named by path. A path never equals the npm name, so a presence
# check on the name alone re-appended upstream on EVERY run, leaving opencode
# loading both the fork and the very package the fork exists to replace.
GOAL_PLUGIN_DIR = GOAL_PLUGIN_REPO.rsplit("/", 1)[-1].lower()

# opencode's own `command` config key (top-level), NOT the
# ~/.config/opencode/command/*.md files ferry installs for /fan-out. The goal
# plugin's README requires this entry or its /goal slash command never appears.
GOAL_COMMAND = {
    "description": "Set a session-scoped goal and auto-continue until complete.",
    "template": "$ARGUMENTS",
    "agent": "build",
}

# Every remote spelling of our plugin whose NAME half identifies it outright.
GOAL_NAME_MATCHES = {x.lower() for x in LEGACY_GOAL_PLUGINS}
GOAL_NAME_MATCHES.add(GOAL_PLUGIN_BASE.lower())
GOAL_NAME_MATCHES.add(GOAL_PLUGIN_PKG.lower())


def pkg_name(entry):
    """The npm NAME half of a plugin entry (the raw string when it has none)."""
    if isinstance(entry, list) and entry:      # the ["pkg", {opts}] form
        entry = entry[0]
    if not isinstance(entry, str):
        return None
    # Strip trailing #ref
    if "#" in entry:
        entry = entry.rsplit("#", 1)[0]
    # Split at the FIRST separating "@", never the last: the canonical spec is
    # `opencode-goal-plugin@https://...`, and rsplit() would hand back the whole
    # `name@https://github.com/...` string the moment a URL carried an "@" of its
    # own. A leading "@" is an npm SCOPE, not a separator.
    if entry.startswith("@"):
        rest = entry[1:]
        return "@" + rest.split("@", 1)[0] if "@" in rest else entry
    if "@" in entry:
        return entry.split("@", 1)[0]
    return entry


def is_path_entry(entry):
    """A local filesystem path (or file:// URL): somebody's fork, never rewritten."""
    raw = entry[0] if isinstance(entry, list) and entry else entry
    if not isinstance(raw, str):
        return False
    return raw.startswith(("/", ".", "~")) or raw.lower().startswith("file:")


def is_goal_spec(entry):
    """Any REMOTE spelling of our plugin, in every shape ferry ever wrote.

    Covers the bare npm name, the scoped upstream name, `github:owner/repo`
    with or without a `#ref`, the same repo behind a `name@` prefix, a
    `git+https://` URL and an `archive/refs/tags/*.tar.gz` URL. A LOCAL PATH is
    deliberately excluded - opencode accepts a filesystem path, Bun cannot
    resolve a private repo over `github:`, and a hard fork can only be named
    that way, so a path keeps its counts-as-present behaviour untouched.
    """
    raw = entry[0] if isinstance(entry, list) and entry else entry
    if not isinstance(raw, str):
        return False
    if is_path_entry(raw):
        return False
    name = pkg_name(raw)
    if isinstance(name, str) and name.lower() in GOAL_NAME_MATCHES:
        return True
    low = raw.lower()
    return any(m in low for m in GOAL_REPO_MARKERS)


def is_goal_plugin(entry):
    """Does this entry already SATISFY the requirement (upstream or a fork)?"""
    name = pkg_name(entry)
    if not isinstance(name, str):
        return False
    # The canonical spec's name half, plus the bare `github:` form an older
    # ferry wrote (pkg_name() has already stripped any trailing #ref).
    if name.lower() in (GOAL_PLUGIN_PKG.lower(), GOAL_PLUGIN_BASE.lower()):
        return True
    # Match the package's directory name as a whole PATH SEGMENT, with or
    # without a file extension, so ".../opencode-goal-plugin/dist/server.js"
    # and ".../opencode-goal-plugin.js" both count as present while a
    # neighbouring ".../opencode-goal-plugin-extras/..." does not.
    if name.startswith(("/", ".", "~")):
        segs = [s.lower() for s in name.split("/") if s]
        return GOAL_PLUGIN_DIR in segs or GOAL_PLUGIN_DIR in (
            os.path.splitext(s)[0].lower() for s in segs)
    return False


def ensure_goal_plugin(plugins):
    """Migrate every earlier spelling to GOAL_PLUGIN, dedupe, guarantee one entry.

    Ferry OWNS this entry: any other remote spelling is drift, and rewriting it
    is the only way a working spec (or a new plugin version) ever reaches a
    machine that already has one. Options on a ["pkg", {opts}] tuple survive.

    Returns (plugins, goal_entry, migrated_from); migrated_from lists the raw
    spec strings this run replaced - i.e. the cache directories nothing
    references any more.
    """
    if not isinstance(plugins, list):
        plugins = []
    migrated_from, out = [], []
    for p in plugins:
        if not is_goal_spec(p):
            out.append(p)
            continue
        old = p[0] if isinstance(p, list) and p else p
        if isinstance(p, list) and len(p) > 1:
            out.append([GOAL_PLUGIN, p[1]])
        else:
            out.append(GOAL_PLUGIN)
        if old != GOAL_PLUGIN:              # an exact match is a no-op
            migrated_from.append(old)

    # Deduplicate by package name while preserving order.
    seen, deduped = set(), []
    for p in out:
        name = pkg_name(p)
        key = name.lower() if isinstance(name, str) else None
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        deduped.append(p)

    goal_entry = next((e for e in deduped if is_goal_plugin(e)), None)
    if goal_entry is None:
        deduped.append(GOAL_PLUGIN)
        goal_entry = GOAL_PLUGIN
    return deduped, goal_entry, migrated_from


def load_jsonc(path, label):
    """Read a JSON/JSONC config, tolerating comments and trailing commas."""
    if not os.path.exists(path):
        return {}
    raw = open(path).read()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        stripped = re.sub(r'("(?:\\.|[^"\\])*")|//[^\n]*|/\*.*?\*/',
                          lambda m: m.group(1) or '', raw, flags=re.S)
        stripped = re.sub(r',\s*([}\]])', r'\1', stripped)
        try:
            return json.loads(stripped)
        except json.JSONDecodeError as e:
            print(f"    WARNING: {label} is unparseable ({e}); starting from a fresh one.")
            print("    The original is preserved verbatim in the snapshot below.")
            return {}


# --- tui.json: the ONLY place the TUI half of a plugin is read from. ---
# opencode.json's `plugin` array feeds the SERVER plugin loader. A module loaded
# with kind:"tui" (the goal plugin's sidebar panel) comes from tui.json /
# tui.jsonc in the global config dir, $OPENCODE_TUI_CONFIG, project tui files,
# or a .opencode directory - packages/opencode/src/config/tui.ts:157-210. Listing
# the spec in opencode.json alone loads the server half and silently nothing else.
def global_opencode_dir():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.abspath(os.path.join(base, "opencode"))


def tui_target(cfg_path, mode, path):
    """Which tui.json to mirror the plugin entry into, or None for "do not".

    "auto" writes ONLY when the config being written is the global takeover
    target itself. The ferry lane profiles (~/.config/ferry/opencode-*.json) and
    the --profiles-only / --no-opencode client scopes must never bring
    ~/.config/opencode into existence - that ABSENCE is what those scopes mean,
    and lib/ferry-clientbootstrap.test.py asserts it.
    """
    if mode == "off":
        return None
    if mode == "explicit":
        return os.path.expanduser(path) if path else None
    if os.path.abspath(os.path.dirname(os.path.expanduser(cfg_path))) != global_opencode_dir():
        return None
    return os.path.expanduser(path) if path else os.path.join(global_opencode_dir(), "tui.json")


# --- Cache hygiene: opencode NEVER invalidates ~/.cache/opencode/packages. ---
# The per-spec directory is path.join(global.cache, "packages", spec) with
# sanitize() a no-op off Windows, so the raw spec string acts as a PATH - the
# slashes in a tarball URL nest several levels deep, and node's path.join
# collapses the "//" exactly as normpath does (packages/core/src/npm.ts:43-47,79).
# With a name-prefixed spec the install SHORT-CIRCUITS on the mere existence of
# <dir>/node_modules/<name> (npm.ts:125-127) - no TTL, no version compare - so an
# interrupted install leaves an empty directory that is treated as installed
# forever, with no self-heal and no retry (loader.ts:178).
DEAD_CACHE_SPECS = (
    "github:sblattj/OpenCode-goal-plugin#v0.9.1",
    "github:sblattj/OpenCode-goal-plugin",
    "github:sblattj/opencode-goal-plugin",
    "opencode-goal-plugin@latest",
)


def cache_packages_root():
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.abspath(os.path.join(base, "opencode", "packages"))


def cache_dir_for(spec):
    """opencode's per-spec directory for `spec`.

    NORMPATH IS LOAD-BEARING. opencode builds this with node's path.join, which
    COLLAPSES the `//` after `https:`; python's os.path.join does not. So the
    canonical spec lands at
    `<packages>/opencode-goal-plugin@https:/github.com/sblattj/OpenCode-goal-plugin/archive/refs/tags/v0.10.1.tar.gz`
    with a SINGLE slash after `https:`, and a path built without normpath points
    at a directory that does not exist - which reads as "nothing to purge" and
    as "the plugin is not installed".
    """
    return os.path.normpath(os.path.join(cache_packages_root(), spec))


def purgeable(spec, path):
    """Refuse to delete anything that is not plainly one of OUR cache dirs.

    Three independent gates, all of which must hold:
      1. the path is strictly under <cache>/opencode/packages/ and walks no `..`;
      2. the SPEC it came from is one of ours (is_goal_spec / the package name);
      3. the package name appears as a PATH COMPONENT under packages/.

    (3) is deliberately not "the LEAF is the package name". A spec's slashes
    become directories, so the tarball form's leaf is a TAG
    (`v0.10.1.tar.gz`) and its first component is `opencode-goal-plugin@https:`,
    while the pre-v1.30.1 `github:` form's leaf is `OpenCode-goal-plugin#v0.9.1`
    and its first component is `github:sblattj`. A rule anchored to either end
    alone refuses half the directories this exists to clean. `packages/foo`
    matches nothing under any of them.
    """
    root = cache_packages_root()
    p = os.path.normpath(path)
    if not p.startswith(root + os.sep):
        return False
    rel = [s for s in os.path.relpath(p, root).split(os.sep) if s]
    if not rel or os.pardir in rel:
        return False
    marker = GOAL_PLUGIN_PKG.lower()
    if not (is_goal_spec(spec) or marker in str(spec).lower()):
        return False
    return any(marker in seg.lower() for seg in rel)


def purge_cache(specs):
    """Remove the per-spec directory of each spec — and ONLY that directory.

    NOT the whole `opencode-goal-plugin@https:` subtree: every tarball spec of
    this package shares that first component, so wiping it while migrating a
    stale `...v0.10.0.tar.gz` entry would also delete an already-good
    `...v0.10.1.tar.gz` install and leave an offline laptop with nothing. The
    exact per-spec directory is the minimal correct unit; empty scaffolding is
    pruned below.
    """
    removed = []
    root = cache_packages_root()
    for spec in specs:
        d = cache_dir_for(spec)
        if not purgeable(spec, d) or not os.path.isdir(d):
            continue
        shutil.rmtree(d, ignore_errors=True)
        if os.path.exists(d):
            continue
        removed.append(d)
        # Prune the now-empty scaffolding a nested spec left behind
        # (`packages/https:/github.com/...`). rmdir refuses a non-empty
        # directory, so a sibling install stops the walk on its own.
        parent = os.path.dirname(d)
        while parent.startswith(root + os.sep):
            try:
                os.rmdir(parent)
            except OSError:
                break
            parent = os.path.dirname(parent)
    return removed

# opencode 1.18.23 ships SEVEN built-in agents. Verified two ways so a future
# rename gets caught: the published schema's $defs.Config.properties.agent names
# exactly plan/build/general/explore/title/summary/compaction, and the installed
# binary contains each of those strings. `scout` is in NEITHER (0 occurrences in
# the 144MB binary) — ferry pinned it for months and the pin did nothing, because
# an unknown key just lands in `agent`'s additionalProperties and is never read.
DRIVER_AGENTS = ("build", "plan")
EXPLORE_AGENTS = ("explore",)
COMPACTION_AGENTS = ("compaction",)
HOUSE_AGENTS = ("title", "summary")

# --- The fan-out worker, split in two and banded by complexity. ---
# opencode's task tool takes NO model parameter: the driver dispatches by AGENT
# NAME, and each agent is pinned to exactly one lane in this config. The only
# thing the driver sees at dispatch time is the task tool's own description,
# which lists every non-primary agent as "- <name>: <description>" — so the
# complexity band has to live IN the description or the driver has nothing to
# route on. Hence one cheap worker (light) and one capable worker (standard),
# each carrying its band in prose.
#
# `general` is DISABLED rather than deleted. Dropping the key does not remove
# the agent: opencode ships `general` as a built-in, and an unpinned built-in
# reappears inheriting the primary model — i.e. every fan-out task would land
# on the expensive driver lane. `{"disable": true}` is the only way to take it
# off the task tool's menu.
LIGHT_AGENTS = ("light",)
STANDARD_AGENTS = ("standard",)
DISABLED_AGENTS = ("general",)
LIGHT_DESC = "Worker for tasks rated 0-50 of 100 complexity: exploration follow-ups, small fixes, easy implementation, mechanical edits with clear instructions. Full tool access. Default worker; use standard only when the task clearly needs deeper judgment."
STANDARD_DESC = "Worker for tasks rated 51-100 of 100 complexity: multi-file implementation, ambiguous debugging, design judgment. Full tool access. Use light for anything rated 50 or below."

# --- Role lanes plus the selectable medium lane. Never a real model id. ---
# The local lanes cap KV at 131072 (128k) tokens, so a 100k-token prompt plus
# opencode's 32k output reservation tips over into a clean 400 (max_tokens is
# reserved against the KV budget). 8k output keeps prompts up to ~123k
# admissible; a compaction summary never needs 32k anyway.
#
# `modalities` is NOT decoration. opencode gates attachments on it: a custom
# provider's model has capabilities.input.image == false unless its config
# entry says `modalities.input` includes "image" (models.dev knows nothing
# about a ferry lane, so there is no fallback). With the flag false, opencode
# still runs its Read tool on a pasted screenshot — and then REPLACES the image
# with the text `ERROR: Cannot read "x.png" (this model does not support image
# input). Inform the user.` before the request leaves the laptop. The lane
# behind `heavy` reads images fine; the model was told it could not. Measured
# 2026-09-05 (opencode 1.18.29) by capturing the bytes on the wire: without the
# declaration the request carried that ERROR line, with it the request carried
# an image_url part and GPT-6 Astra described the picture. Same for PDF, which
# the front passes as an input_file. The GPU pair stays text-only: the mlx
# servers behind local-orch/local-sub take no image input, and declaring one
# would send bytes they reject instead of the placeholder they now get.
# --- Query the host catalogue; never populate a config FROM it. ---
# The catalogue does NOT advertise the fallback deployments: they route by name
# but are not `public`, so they never appear in /v1/models. Those are reached by
# the ROUTER on overflow, not by a client picking one out of a menu, so they stay
# out of the config.
served = []
try:
    # An authed front door rejects a bare catalogue request, which would read
    # as "host down" and wire the lane pair unchecked — so carry the same
    # bearer the generated configs will use. No key => no header (unchanged).
    req = urllib.request.Request(f"{base}/models")
    if oc_key:
        req.add_header("Authorization", "Bearer %s" % oc_key)
    with urllib.request.urlopen(req, timeout=4) as r:
        served = [m.get("id") for m in json.load(r).get("data", []) if m.get("id")]
except Exception as e:
    print(f"    (Could not query {base}/models: {e}; wiring the lane pair unchecked)")

# A modern cloud host exposes `medium`, which carries the `standard` worker.
# When that capability is absent (or cannot be checked), standard joins light
# and explore on flash; compaction and housekeeping use super-flash. The GPU
# pair deliberately stays exactly as it was — it has only two lanes, so light,
# standard and explore all share local-sub.
if prefer_local:
    driver, light, standard, explore, compaction, house = (
        "local-orch", "local-sub", "local-sub", "local-sub", "local-sub",
        "local-sub")
    limits = {"limit": {"context": 131072, "output": 8192}}
else:
    driver, light, explore, house = "heavy", "flash", "flash", "super-flash"
    standard = "medium" if "medium" in served else "flash"
    compaction = house
    limits = {"modalities": {"input": ["text", "image", "pdf"],
                             "output": ["text"]}}
driver = force_model or driver
# --small-model (and therefore --super) moves the whole fan-out together: the
# band split is about which worker the driver PICKS, not about keeping two
# different lanes alive when the operator asked for one.
light = force_small or light
standard = force_small or standard
explore = force_small or explore
compaction = force_house or compaction
house = force_house or house

# --- Validate the selected lanes against the host catalogue. ---
if served:
    # Public selected lanes are checked. A selected lane that also backs
    # title/summary is exempt: some compatible hosts omit it from their public
    # catalogue, and the config must remain usable for their scheduled agents.
    # The same exemption covers local-sub and --super, where all non-driver
    # agents deliberately share that lane.
    missing = [l for l in dict.fromkeys((driver, light, standard, explore,
                                         compaction))
               if l not in served and l != house]
    if missing:
        print(f"    WARNING: host does not serve {', '.join(missing)}.")
        print(f"    Catalogue: {', '.join(served)}")

# --- Snapshot: the whole original, verbatim, before we touch anything. ---
# .jsonc because opencode's schema sets allowComments/allowTrailingCommas, so a
# hand-maintained config legitimately carries comments that json.dump cannot
# round-trip. The snapshot is where they survive.
SNAP_RE_TPL = r"^{stem}\.\d{{8}}T\d{{6}}Z(-\d+)?\.jsonc$"

def snapshot(path, keep):
    if not os.path.exists(path):
        return None
    d = os.path.dirname(path) or "."
    stem = os.path.splitext(os.path.basename(path))[0]
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    snap = os.path.join(d, f"{stem}.{ts}.jsonc")
    n = 1
    while os.path.exists(snap):     # two runs inside one second must not collide
        snap = os.path.join(d, f"{stem}.{ts}-{n}.jsonc")
        n += 1
    shutil.copy2(path, snap)
    if keep > 0:
        # Match only OUR snapshots: the timestamp shape, anchored to this stem.
        # A plain "{stem}.*.jsonc" glob would happily delete a user's own
        # opencode.notes.jsonc sitting in the same directory.
        pat = re.compile(SNAP_RE_TPL.format(stem=re.escape(stem)))
        olds = sorted(f for f in os.listdir(d) if pat.match(f))
        for old in olds[:-keep]:
            os.remove(os.path.join(d, old))
    return snap

# --- Load whatever is there (JSONC-tolerant). ---
cfg = None
if os.path.exists(cfg_path):
    raw = open(cfg_path).read()
    try:
        cfg = json.loads(raw)
    except json.JSONDecodeError:
        stripped = re.sub(r'("(?:\\.|[^"\\])*")|//[^\n]*|/\*.*?\*/',
                          lambda m: m.group(1) or '', raw, flags=re.S)
        stripped = re.sub(r',\s*([}\]])', r'\1', stripped)
        try:
            cfg = json.loads(stripped)
            print("    (config is JSONC; its comments survive in the snapshot, not in the rewrite)")
        except json.JSONDecodeError as e:
            print(f"    WARNING: existing config is unparseable ({e}); starting from a fresh one.")
            print("    The original is preserved verbatim in the snapshot below.")
            cfg = None

snap = snapshot(cfg_path, keep_snaps)
if cfg is None:
    cfg = {}
cfg.setdefault("$schema", SCHEMA)

# --- provider.ferry: the WIRING is ours; the picker's contents are not. ---
# npm/options/limits are rewritten every run — that is the drift this command
# exists to end. Two things here are NOT ours, and survive a takeover:
#
#   1. Extra lanes. A host config typically declares local-orch/local-sub too, so
#      the GPU pair is selectable from the picker without a hand edit. Rebuilding
#      `models` wholesale deleted them, and the deletion was invisible: the
#      command still reported success, and the lanes still resolved if you typed
#      one — they were just gone from the menu.
#   2. A hand-written `name` on any lane. It is the label a human reads in the
#      status bar, and /v1/models carries only the lane id, so ferry has no
#      better one to offer: never invented, never overwritten.
#
# A label naming the MODEL behind a lane goes stale by design — the whole point
# of the lane-name contract is that the model swaps host-side without touching a
# client config — so a label should name the lane's ROLE.
prov = cfg.setdefault("provider", {})
prev_ferry = prov.get("ferry") if isinstance(prov.get("ferry"), dict) else {}
prev_models = prev_ferry.get("models") if isinstance(prev_ferry.get("models"), dict) else {}
prev_options = prev_ferry.get("options") if isinstance(prev_ferry.get("options"), dict) else {}

# dict.fromkeys: multiple agents can share one lane, and the GPU pair has only
# two lanes, so declaring one model entry per agent would create duplicates.
models = {}
# `medium` is a cloud capability tier. Declare it when the host offers it so it
# appears in opencode's model picker and can serve the `standard` worker. Its
# resolved backend varies by fleet: domestic
# Terra accepts attachments, while international GLM-5.3 is text-only. A single
# opencode provider entry cannot vary modalities with X-Ferry-Fleet, so omit the
# declaration and preserve the safe text-only baseline across every fleet.
declared_lanes = (driver, light, standard, explore, compaction, house)
# Do not advertise a lane a pre-medium host does not serve. Explicit use still
# adds it through the selected agent lanes above, allowing a caller to request the
# new lane deliberately and receive the normal catalogue warning if absent.
if not prefer_local and "medium" in served:
    declared_lanes += ("medium",)
for lane in dict.fromkeys(declared_lanes):
    spec = {} if lane == "medium" and not prefer_local else dict(limits)
    prev_name = (prev_models.get(lane) or {}).get("name")
    if isinstance(prev_name, str) and prev_name:
        spec["name"] = prev_name
    models[lane] = spec
for lane, spec in prev_models.items():
    if lane not in models and isinstance(spec, dict):
        models[lane] = spec
extra_lanes = [l for l in models if l not in declared_lanes]

# Only baseURL/apiKey/headers are ours; every other options key a user
# hand-added (or a previous run wrote) survives untouched.
options = dict(prev_options)
# The bearer the front door expects: the master key when one is configured
# (client.json / --key), else the legacy 'local' placeholder.
options["baseURL"] = base
options["apiKey"] = oc_key or "local"
# Fleet identity, rewritten fresh every run - see front/ferry_front.py's
# resolver (docs/superpowers/specs/2026-09-04-fleets-design.md §4/§6).
# "{env:FERRY_FLEET}" is opencode's OWN env-substitution syntax; ferry must
# never resolve it, so a one-shot `FERRY_FLEET=international opencode-super`
# is read at opencode's load time, not at config-write time.
options["headers"] = {
    "X-Ferry-Client": client_name,
    "X-Ferry-Fleet": "{env:FERRY_FLEET}",
}

prov["ferry"] = {
    "npm": "@ai-sdk/openai-compatible",
    # Regenerated, not preserved: this one is DERIVED from --host, and a name
    # carried over from a previous host would label the picker with a box the
    # baseURL no longer points at.
    "name": f"Ferry ({host})",
    "options": options,
    "models": models,
}

# Raw spec strings this run rewrote, in the config AND in tui.json: those are
# the ~/.cache/opencode/packages directories nothing references any more.
migrated_from = []
goal_entry = None
tui_file = None
tui_snap = None

if set_default:
    # --- The takeover. Four keys replaced outright, one appended to. ---
    cfg["permission"] = "allow"          # schema: PermissionConfig accepts the
                                         # bare enum "ask" | "allow" | "deny"
    cfg["model"] = f"ferry/{driver}"
    # small_model follows the title/summary HOUSEKEEPER, not the fan-out workers. opencode's own schema
    # describes it as "small model to use for tasks like title generation", which
    # is the housekeeping role exactly; leaving it on light/standard/explore would send
    # every small task opencode has not got a named agent for to a fan-out lane.
    cfg["small_model"] = f"ferry/{house}"

    # Replaced WHOLESALE, not merged: a stale pin left behind here (a compaction
    # agent still naming a retired model id, say) is exactly the drift this
    # command exists to end. Anything custom is recoverable from the snapshot.
    agent = {a: {"model": f"ferry/{driver}"} for a in DRIVER_AGENTS}
    # Disabled, not deleted: a deleted key lets opencode's built-in `general`
    # come back on the primary (driver) model.
    agent.update({a: {"disable": True} for a in DISABLED_AGENTS})
    agent.update({a: {"description": LIGHT_DESC, "mode": "subagent",
                      "model": f"ferry/{light}"} for a in LIGHT_AGENTS})
    agent.update({a: {"description": STANDARD_DESC, "mode": "subagent",
                      "model": f"ferry/{standard}"} for a in STANDARD_AGENTS})
    agent.update({a: {"model": f"ferry/{explore}"} for a in EXPLORE_AGENTS})
    agent.update({a: {"model": f"ferry/{compaction}"} for a in COMPACTION_AGENTS})
    agent.update({a: {"model": f"ferry/{house}"} for a in HOUSE_AGENTS})
    cfg["agent"] = agent

    # Additive — a plugin list belongs to the user; we only ensure ours is in it.
    # Every earlier spelling of OUR entry is rewritten to the canonical spec (see
    # ensure_goal_plugin and the GOAL_PLUGIN comment block: the forms ferry wrote
    # before v1.30.1 install to disk and are then silently discarded).
    plugins, goal_entry, migrated = ensure_goal_plugin(cfg.get("plugin"))
    cfg["plugin"] = plugins
    migrated_from.extend(migrated)

    # MERGE, never take over: the plugin's /goal slash command needs a
    # top-level `command.goal` entry or it never appears in opencode. Only the
    # `goal` key is ours, and only when it is absent - a user who customised it
    # keeps their version verbatim, and no other command is touched.
    commands = cfg.get("command")
    if not isinstance(commands, dict):
        commands = {}
    if "goal" not in commands:
        commands["goal"] = dict(GOAL_COMMAND)
    cfg["command"] = commands

os.makedirs(os.path.dirname(cfg_path) or ".", exist_ok=True)
with open(cfg_path, "w") as f:
    json.dump(cfg, f, indent=2)
    f.write("\n")

# --- Mirror the plugin entry into tui.json (the sidebar half). ---
# Same migration, same dedupe, same snapshot policy as opencode.json; every
# other key in the file is left exactly as it was.
if set_default:
    tui_file = tui_target(cfg_path, tui_mode, tui_path)
if tui_file:
    tui_cfg = load_jsonc(tui_file, tui_file)
    if not isinstance(tui_cfg, dict):
        tui_cfg = {}
    tui_snap = snapshot(tui_file, keep_snaps)
    tui_plugins, _tui_entry, tui_migrated = ensure_goal_plugin(tui_cfg.get("plugin"))
    migrated_from.extend(tui_migrated)
    tui_cfg.setdefault("$schema", TUI_SCHEMA)
    tui_cfg["plugin"] = tui_plugins
    os.makedirs(os.path.dirname(tui_file) or ".", exist_ok=True)
    with open(tui_file, "w") as f:
        json.dump(tui_cfg, f, indent=2)
        f.write("\n")

# --- Purge the cache directories this run just orphaned. ---
purged = []
if set_default and not keep_cache:
    specs = list(dict.fromkeys(list(migrated_from) + list(DEAD_CACHE_SPECS)))
    purged = purge_cache(specs)
    # The canonical directory is kept when it holds a real install and removed
    # only when it does NOT: an interrupted install leaves an empty
    # node_modules/<name> that opencode's existence-only short-circuit then
    # treats as installed forever.
    canon = cache_dir_for(GOAL_PLUGIN)
    if os.path.isdir(canon) and not os.path.exists(
            os.path.join(canon, "node_modules", GOAL_PLUGIN_PKG, "package.json")):
        purged += purge_cache([GOAL_PLUGIN])

# Hand the canonical spec to the pre-install pass, but only when ferry's own
# entry is the one in play: a local fork must not trigger an upstream install.
if set_default and goal_entry == GOAL_PLUGIN and spec_out:
    with open(spec_out, "w") as f:
        f.write(f"{GOAL_PLUGIN}\n{GOAL_PLUGIN_REF}\n{GOAL_PLUGIN_PKG}\n")

print(f"    Wired opencode -> {base}")
print(f"    Provider: ferry   Lanes: {driver} (driver), {light} (light), {standard} (standard), {explore} (explore), {compaction} (compaction), {house} (title/summary)")
if extra_lanes:
    print(f"    Kept in picker: {', '.join(extra_lanes)} (declared in the config, not pinned by ferry)")
if set_default:
    print(f"    model={cfg['model']}  small_model={cfg['small_model']}  permission=allow")
    print(f"    Agents pinned:  {'/'.join(DRIVER_AGENTS)} -> ferry/{driver}")
    print(f"                    {'/'.join(LIGHT_AGENTS)} -> ferry/{light}; {'/'.join(STANDARD_AGENTS)} -> ferry/{standard}")
    print(f"                    {'/'.join(EXPLORE_AGENTS)} -> ferry/{explore}; {'/'.join(DISABLED_AGENTS)} -> disabled")
    print(f"                    {'/'.join(COMPACTION_AGENTS)} -> ferry/{compaction}; {'/'.join(HOUSE_AGENTS)} -> ferry/{house}")
    # Report the entry that actually SATISFIES the requirement, not the package
    # we would have added. Printing GOAL_PLUGIN unconditionally claimed an
    # install that never happened whenever a local fork was already present.
    label = goal_entry[0] if isinstance(goal_entry, list) and goal_entry else goal_entry
    print(f"    Plugin: {label}  (/goal command wired; tui.json: {'written' if tui_file else 'skipped'})")
    if label != GOAL_PLUGIN:
        print(f"                    (counts as {GOAL_PLUGIN}; upstream not added)")
    for d in purged:
        print(f"    Cache purged:   {d}")
else:
    print("    --no-default: provider wired; permission/model/agent left alone.")
if snap:
    print(f"    Snapshot:       {snap}")
if tui_snap:
    print(f"    Snapshot:       {tui_snap}")
print(f"    Config written: {cfg_path}")
if tui_file:
    print(f"    TUI config:     {tui_file}")
PYEOF

  # --- Pre-install the plugin through the production loader. ---
  # The block above only WROTE a spec. Nothing installs it until opencode next
  # starts, and if the install fails there it fails silently forever (see
  # _ferry_preinstall_goal_plugin). Doing it here is what turns "the config
  # looks right" into "the plugin is on disk and loadable".
  local goal_spec="" goal_ref="" goal_pkg=""
  if [[ -s "$oc_specfile" ]]; then
    goal_spec="$(sed -n 1p "$oc_specfile")"
    goal_ref="$(sed -n 2p "$oc_specfile")"
    goal_pkg="$(sed -n 3p "$oc_specfile")"
  fi
  rm -f "$oc_specfile"

  if (( do_install )) && [[ -n "$goal_spec" ]]; then
    if command -v opencode >/dev/null 2>&1; then
      _ferry_preinstall_goal_plugin "$goal_spec" "$goal_ref" "$goal_pkg"
    else
      echo "    Plugin:  opencode is not on PATH; skipping the pre-install."
      echo "             It will be fetched the first time opencode starts."
    fi
  fi
  return 0
}
