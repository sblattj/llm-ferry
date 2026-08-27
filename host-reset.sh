#!/bin/zsh
# host-reset.sh — bring the HOST back in sync with its own checkout: rebuild the
# CLI, re-link it, validate the route config, bounce the proxy, and re-apply the
# opencode takeover to the host's own configs.
#
#   ./host-reset.sh            # rebuild + re-link + proxy/share bounce  (seconds)
#   ./host-reset.sh --full     # ...and reload the GPU lanes             (minutes)
#
# The host counterpart of client-reset.sh, and deliberately NOT its mirror. A
# client resets by DOWNLOADING a newer CLI; the host has nothing to download —
# ~/.local/bin/ferry is a symlink into this checkout, so the host's staleness
# comes from `ferry` being out of sync with lib/, from a link that decayed into a
# stale COPY, or from a litellm.yaml edit the running proxy never picked up.
#
# Deliberately NOT a bootstrap: no `uv tool install`, no model downloads, no
# ~/.zshrc. Run host-bootstrap.sh (or `ferry install`) if dependencies are what
# is missing.

set -eu

APP_DIR="${0:A:h}"
cd "$APP_DIR"

FULL=0
DO_PULL=1
PORT="${FERRY_PORT:-8090}"
SHARE_PORT="${FERRY_SHARE_PORT:-8095}"
LOCAL_ORCH_PORT=8092
LOCAL_SUB_PORT=8093
ROUTE_CONFIG="$HOME/.config/ferry/litellm.yaml"
SECRETS="$HOME/.config/ferry/secrets.env"
FERRY_BIN="$APP_DIR/ferry"

usage() {
  cat <<EOF
Usage: ./host-reset.sh [options]

  --full        Also bounce the local GPU lanes (ferry down && ferry up).
                Reloads ~33GB of weights — minutes, and drops in-flight work.
                Without it the MLX lanes are left running untouched and only
                litellm + the share server restart.
  --no-pull     Skip the git fast-forward. Use offline, or to reset onto the
                working tree exactly as it stands.
  --port <n>    Endpoint port (default $PORT).
  --help        This message.
EOF
  exit 0
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --full)     FULL=1; shift ;;
    --no-pull)  DO_PULL=0; shift ;;
    --port)     PORT="$2"; shift 2 ;;
    --help|-h)  usage ;;
    *) echo "Unknown option: $1"; usage ;;
  esac
done

say()  { echo "$@"; }
ok()   { echo "    \033[1;32m$*\033[0m"; }
warn() { echo "    \033[1;33m$*\033[0m"; }
die()  { echo "    \033[1;31mError: $*\033[0m" >&2; exit 1; }

echo "================================================================="
echo "                    LLM-FERRY HOST RESET"
echo "================================================================="

# --- 0. Refuse to run on a client -------------------------------------------
# ferry decides host-vs-client purely on the presence of this file (see
# CLIENT_MODE in lib/ferry-core.zsh), so match it exactly rather than inventing a
# second, disagreeing definition. Running the host reset on a client would
# rebuild a checkout the client does not have and bounce a proxy it does not run.
if [[ -f "$HOME/.config/ferry/client.json" ]]; then
  echo "This machine has a ferry CLIENT profile (~/.config/ferry/client.json)."
  echo "host-reset.sh is for the host. To catch a client up, run:"
  echo "    curl -fsSL http://<host>:$SHARE_PORT/client-reset.sh | zsh"
  exit 1
fi
echo "Checkout:  $APP_DIR"
echo "Endpoint:  http://127.0.0.1:$PORT   Share: :$SHARE_PORT"
echo "Mode:      $( ((FULL)) && echo 'FULL — GPU lanes reload' || echo 'proxy + share only — GPU lanes untouched')"
echo "================================================================="

# --- 1. Fast-forward the checkout -------------------------------------------
# --ff-only, and a dirty-tree check ahead of it. A reset that rebased or merged
# would be rewriting the tree you develop in. Divergence and local edits are
# decisions for a human; being offline is not, so a failed fetch only warns.
if (( DO_PULL )); then
  say ">>> Fast-forwarding the checkout..."
  if ! git rev-parse --git-dir >/dev/null 2>&1; then
    warn "not a git checkout — skipping the pull."
  elif ! git diff --quiet || ! git diff --cached --quiet; then
    # Untracked files are fine and deliberately not counted: they cannot block a
    # fast-forward, and this repo normally carries a few.
    die "tracked files have uncommitted changes. Commit/stash them, or use --no-pull.
       $(git diff --name-only HEAD | sed 's/^/         /' | head -10)"
  elif ! git fetch --quiet 2>/dev/null; then
    warn "could not fetch (offline? no remote?) — rebuilding from the tree as it stands."
  else
    branch="$(git branch --show-current 2>/dev/null || echo '')"
    upstream="$(git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null || echo '')"
    if [[ -z "$upstream" ]]; then
      warn "branch '$branch' has no upstream — nothing to fast-forward."
    elif ! git merge-base --is-ancestor HEAD "$upstream" 2>/dev/null; then
      # HEAD is not an ancestor of upstream: either ahead (fine, nothing to pull)
      # or genuinely diverged (a human decision — never resolve it from a reset).
      if git merge-base --is-ancestor "$upstream" HEAD 2>/dev/null; then
        ok "ahead of $upstream — nothing to pull."
      else
        die "$branch has DIVERGED from $upstream. Resolve it by hand, or use --no-pull."
      fi
    else
      git pull --ff-only --quiet
      ok "at $(git rev-parse --short HEAD) ($upstream)"
    fi
  fi
else
  say ">>> Skipping the git pull (--no-pull)."
fi

# --- 2. Rebuild the CLI from lib/ -------------------------------------------
# `ferry` is a GENERATED artifact (build.zsh assembles it from lib/ferry-*.zsh).
# Editing a module without rebuilding leaves the host running the OLD code while
# the source says otherwise — and because ~/.local/bin/ferry is a symlink into
# this checkout, that stale build is what every host command runs.
echo ""
say ">>> Rebuilding ./ferry from lib/..."
[[ -f "$APP_DIR/build.zsh" ]] || die "no build.zsh in $APP_DIR."
# Invoked through the interpreter, not as ./build.zsh: a checkout that lost its
# exec bit (a fresh clone from an archive, a copied tree) exits 126 here, and the
# reset would abort on something that is not actually broken.
zsh "$APP_DIR/build.zsh" >/dev/null || die "build.zsh failed — the modules in lib/ do not assemble."
if zsh "$APP_DIR/build.zsh" --check >/dev/null 2>&1; then
  ok "ferry is in sync with lib/"
else
  die "ferry is still out of sync with lib/ after a rebuild — that should be impossible."
fi
chmod +x "$FERRY_BIN"

# --- 3. Re-link the global commands -----------------------------------------
# ln -sfn every time, on purpose. The failure this catches is not a MISSING link
# but a link that was replaced by a plain COPY at some point: the copy keeps
# working, keeps its old behaviour forever, and nothing ever reports it as stale.
# (Found exactly this on the host — a ferry-dash copy still probing lane names
# that were renamed three releases earlier.)
echo ""
say ">>> Re-linking ~/.local/bin..."
# A git worktree has its own $APP_DIR, so re-linking from one silently repoints
# every global ferry command at a branch checkout — and at a path that stops
# existing the moment the worktree is removed. Legitimate when you are testing a
# branch on the host, surprising otherwise, so warn rather than refuse.
if [[ "$(git rev-parse --git-dir 2>/dev/null)" != "$(git rev-parse --git-common-dir 2>/dev/null)" ]]; then
  warn "this is a git WORKTREE — ~/.local/bin will point at $APP_DIR"
  warn "re-run from the main checkout to point it back."
fi
mkdir -p "$HOME/.local/bin"
for tool in ferry ferry-dash; do
  src="$APP_DIR/$tool"
  dst="$HOME/.local/bin/$tool"
  [[ -f "$src" ]] || continue
  if [[ -L "$dst" && "$(readlink "$dst")" == "$src" ]]; then
    ok "$tool -> $src"
  else
    [[ -e "$dst" && ! -L "$dst" ]] && warn "$tool was a stale COPY, not a link — replacing it."
    ln -sfn "$src" "$dst"
    ok "$tool -> $src (relinked)"
  fi
done
case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *) warn "~/.local/bin is not on your PATH — the linked commands will not resolve." ;;
esac

# --- 4. Validate the route config BEFORE anything live is touched -----------
# The host's answer to client-reset's validate-before-overwrite. litellm does not
# check its config beyond parsing it: a duplicate key, a dangling alias, or a
# missing env var all start cleanly and fail later, at request time, on one lane,
# in a way that looks like a provider outage. Everything here runs while the old
# proxy is still up and serving, so a bad edit costs a failed reset, not an
# endpoint.
echo ""
say ">>> Validating $ROUTE_CONFIG..."
[[ -f "$ROUTE_CONFIG" ]] || die "no route config at $ROUTE_CONFIG. Run 'ferry up' once to seed it from the template."

# Env vars are resolved the way ferry itself resolves them: the shell first, then
# secrets.env. Sourced in a subshell so the reset never carries keys further.
python3 - "$ROUTE_CONFIG" "$SECRETS" <<'PYEOF' || die "route config is not safe to serve — nothing was restarted."
import os, re, sys

cfg_path, secrets_path = sys.argv[1], sys.argv[2]
problems, notes = [], []

# --- env vars the config references ---
text = open(cfg_path).read()
referenced = sorted(set(re.findall(r"os\.environ/([A-Za-z_][A-Za-z0-9_]*)", text)))

env = dict(os.environ)
if os.path.exists(secrets_path):
    # A deliberately dumb KEY=VALUE reader: enough for the export lines ferry's
    # own secrets.env carries, and it never executes the file.
    for line in open(secrets_path):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        line = line[7:].strip() if line.startswith("export ") else line
        if "=" in line:
            k, v = line.split("=", 1)
            env.setdefault(k.strip(), v.strip().strip("'\""))
else:
    notes.append(f"no {secrets_path} — env vars must come from the shell")

missing = [k for k in referenced if not env.get(k)]
if missing:
    problems.append("env vars referenced by the config but unset (those lanes will 401): "
                    + ", ".join(missing))

# --- parse, with duplicate keys treated as the error they are ---
try:
    import yaml
except ImportError:
    notes.append("pyyaml not installed — skipped the parse, alias, and fallback checks")
    yaml = None

if yaml is not None:
    class StrictLoader(yaml.SafeLoader):
        pass

    def no_duplicates(loader, node, deep=False):
        # PyYAML (and litellm) silently keep the LAST of a duplicated key. That is
        # how a `flash` deployment ended up carrying the model_info id of its own
        # fallback: two model_info blocks, one config, no error anywhere.
        mapping = {}
        for key_node, value_node in node.value:
            key = loader.construct_object(key_node, deep=deep)
            if key in mapping:
                raise yaml.constructor.ConstructorError(
                    None, None, f"duplicate key {key!r} at line {key_node.start_mark.line + 1}",
                    key_node.start_mark)
            mapping[key] = loader.construct_object(value_node, deep=deep)
        return mapping

    StrictLoader.add_constructor(
        yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, no_duplicates)

    try:
        cfg = yaml.load(open(cfg_path), Loader=StrictLoader) or {}
    except yaml.YAMLError as e:
        print(f"    YAML error: {e}", file=sys.stderr)
        sys.exit(1)

    deployments = cfg.get("model_list") or []
    if not deployments:
        problems.append("model_list is empty or missing — the proxy would serve no lanes")

    names = set()
    for i, d in enumerate(deployments):
        if not isinstance(d, dict):
            problems.append(f"model_list[{i}] is not a mapping")
            continue
        n = d.get("model_name")
        if not n:
            problems.append(f"model_list[{i}] has no model_name")
            continue
        names.add(n)
        if not (d.get("litellm_params") or {}).get("model"):
            problems.append(f"deployment '{n}' has no litellm_params.model")

    # An alias pointing at a name that does not exist resolves to nothing: the
    # request 400s at call time, and because a hidden alias never appears in
    # /v1/models there is no listing that would have shown the typo.
    #
    # litellm reads model_group_alias from router_settings. Looking only at the
    # top level finds nothing and reports a clean "0 aliases" on a config that is
    # full of them — which then makes the verify step below call every hidden
    # lane missing. Checked in both places for that reason.
    router = cfg.get("router_settings") or {}
    aliases = router.get("model_group_alias") or cfg.get("model_group_alias") or {}
    for alias, target in aliases.items():
        t = target.get("model") if isinstance(target, dict) else target
        if t not in names:
            problems.append(f"model_group_alias '{alias}' -> '{t}', which is not in model_list")

    # A fallback chain naming a lane that does not exist fails silently on the
    # hop that matters — under load, exactly when the fallback was the point.
    for entry in router.get("fallbacks") or []:
        for src, targets in entry.items():
            if src not in names and src not in aliases:
                problems.append(f"fallback source '{src}' is not a lane")
            for t in targets or []:
                if t not in names and t not in aliases:
                    problems.append(f"fallback '{src}' -> '{t}', which is not a lane")

    print(f"    {len(deployments)} deployments, {len(aliases)} aliases, "
          f"{len(referenced)} env refs")

for n in notes:
    print(f"    note: {n}")
for p in problems:
    print(f"    PROBLEM: {p}", file=sys.stderr)
sys.exit(1 if problems else 0)
PYEOF
ok "route config is valid"

# --- 5. Restart -------------------------------------------------------------
echo ""
if (( FULL )); then
  say ">>> FULL restart: stopping everything, then reloading the GPU lanes..."
  "$FERRY_BIN" down
  "$FERRY_BIN" up
else
  # `ferry up --route` reads the SAME litellm.yaml the stack does, so bouncing it
  # re-reads the config without touching the MLX servers — litellm reaches those
  # over HTTP on 127.0.0.1, and a running lane does not care that its front door
  # restarted. This is the whole reason the default is cheap.
  say ">>> Bouncing the proxy (GPU lanes left running)..."
  "$FERRY_BIN" up --route --port "$PORT"
fi

# Share server: it must be STOPPED before it is started. `ferry share` scans
# UPWARD for a free port, so starting a second one while the first still holds
# :$SHARE_PORT lands it on :$((SHARE_PORT+1)) — where it happily serves, injects
# its own port into the client scripts, and leaves every published URL pointing
# at the old server. `ferry down` already reaps it in the --full path.
echo ""
say ">>> Restarting the share server on :$SHARE_PORT..."
pkill -f "ferry-share-marker" 2>/dev/null || true
waited=0
while lsof -nP -iTCP:"$SHARE_PORT" -sTCP:LISTEN >/dev/null 2>&1; do
  (( waited >= 10 )) && die "port $SHARE_PORT is still held after ${waited}s — 'ferry share' would roll to $((SHARE_PORT+1)) and every published client URL would break."
  sleep 1
  waited=$(( waited + 1 ))
done
"$FERRY_BIN" share >/dev/null
waited=0
while ! lsof -nP -iTCP:"$SHARE_PORT" -sTCP:LISTEN >/dev/null 2>&1; do
  (( waited >= 10 )) && die "share server did not bind :$SHARE_PORT. Check ${TMPDIR:-/tmp}/ferry-logs/share-$SHARE_PORT.log"
  sleep 1
  waited=$(( waited + 1 ))
done
ok "share server is on :$SHARE_PORT"

# --- 5b. Wait for the endpoint before anything reads from it ----------------
# `ferry up --route` returns the moment it forks — unlike stack mode it never
# polls for readiness. Re-applying the takeover against a proxy that is not
# listening yet costs the takeover's OWN validation: cmd_opencode queries
# /v1/models to check the lane pair it is about to write, and on a connection
# refusal it degrades to "wiring the lane pair unchecked" and writes anyway.
# A wrong lane name would then land in all three configs unremarked. Observed on
# the first live run of this script, which is why the wait sits here and not
# after the re-apply.
echo ""
say ">>> Waiting for the endpoint on :$PORT..."
waited=0
while ! curl -fsS -m 3 "http://127.0.0.1:$PORT/v1/models" >/dev/null 2>&1; do
  (( waited >= 60 )) && die "the endpoint never came up on :$PORT. Check ${TMPDIR:-/tmp}/ferry-logs/cloud-proxy-$PORT.log"
  sleep 2
  waited=$(( waited + 2 ))
done
ok "endpoint answering (${waited}s)"

# --- 6. Re-apply the opencode takeover to the HOST's own configs ------------
# Same three targets a client gets. Wiring the host to its own endpoint is the
# point of running one: every tool on this box then shares the lanes, the
# fallback chain, and the observability, and none of them needs its own key.
#
# --host/--port are passed explicitly for the reason client-reset.sh passes them:
# never let the config's baseURL be decided by an inference about which machine
# this is. env -u OPENCODE_CONFIG because `ferry opencode` honours that variable
# as its default target, so a shell exporting one would redirect all three writes
# onto a single file.
echo ""
say ">>> Re-applying the opencode takeover (host -> its own endpoint)..."
RESET_FAILED=0
for oc_target in \
  "$HOME/.config/opencode/opencode.json|" \
  "$HOME/.config/ferry/opencode-cloud.json|" \
  "$HOME/.config/ferry/opencode-local.json|--local"
do
  oc_path="${oc_target%%|*}"
  oc_flag="${oc_target#*|}"
  echo "    -> $oc_path"
  if ! env -u OPENCODE_CONFIG "$FERRY_BIN" opencode \
        --host 127.0.0.1 --port "$PORT" --config "$oc_path" $oc_flag; then
    RESET_FAILED=1
  fi
done

# --- 7. Verify against the running endpoint ---------------------------------
# Nothing above proves the lanes answer. Ask the endpoint, then check the local
# backends separately: litellm lists local-orch and local-sub whether or not an
# MLX server is behind them, so "listed" is not "reachable" and reporting the
# listing as health is how a dead GPU lane stays invisible until a client hits it.
echo ""
say ">>> Verifying..."
python3 - "$PORT" "$ROUTE_CONFIG" "$LOCAL_ORCH_PORT" "$LOCAL_SUB_PORT" <<'PYEOF' || RESET_FAILED=1
import json, os, re, socket, sys, urllib.request

port, cfg_path, orch_port, sub_port = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])

with urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/models", timeout=5) as r:
    served = [m["id"] for m in json.load(r).get("data", [])]
print("    Lanes served: " + " ".join(served))

# A hidden alias resolves on request but is deliberately absent from /v1/models,
# so the listing alone cannot confirm it. Read the declared aliases out of the
# config and count those as resolvable too — otherwise the housekeeper lane looks
# broken on every single run.
aliases, aliases_known = set(), True
try:
    import yaml
    cfg = yaml.safe_load(open(cfg_path)) or {}
    # Under router_settings, where litellm reads it — see the validator above.
    aliases = set(((cfg.get("router_settings") or {}).get("model_group_alias")
                   or cfg.get("model_group_alias") or {}).keys())
except Exception as e:
    # Without the alias list a hidden lane is indistinguishable from a missing
    # one. Downgrade to a warning rather than failing the reset on every hidden
    # alias — a false red here would train you to ignore this whole section.
    aliases_known = False
    print(f"    note: could not read aliases from the config ({e}) — "
          f"hidden lanes are reported below but not enforced.")
resolvable = set(served) | aliases
if aliases:
    print("    Hidden aliases: " + " ".join(sorted(aliases)) + "  (resolve on request, absent from /v1/models by design)")

# Every lane the host's own opencode configs name must be reachable. This is the
# check that would have caught a lane rename landing in ferry but not in the
# config, or the reverse.
CONFIGS = ["~/.config/opencode/opencode.json",
           "~/.config/ferry/opencode-cloud.json",
           "~/.config/ferry/opencode-local.json"]
failed = False
for c in CONFIGS:
    p = os.path.expanduser(c)
    if not os.path.exists(p):
        print(f"    {c}: MISSING", file=sys.stderr); failed = True; continue
    raw = open(p).read()
    raw = re.sub(r"/\*.*?\*/", "", raw, flags=re.S)
    raw = re.sub(r"(?m)^\s*//.*$", "", raw)
    raw = re.sub(r",(\s*[}\]])", r"\1", raw)
    try:
        cfg = json.loads(raw)
    except Exception as e:
        print(f"    {c}: does not parse ({e})", file=sys.stderr); failed = True; continue
    wanted = set()
    for v in (cfg.get("agent") or {}).values():
        m = (v or {}).get("model", "")
        if m.startswith("ferry/"):
            wanted.add(m.split("/", 1)[1])
    for key in ("model", "small_model"):
        m = cfg.get(key, "")
        if isinstance(m, str) and m.startswith("ferry/"):
            wanted.add(m.split("/", 1)[1])
    bad = sorted(w for w in wanted if w not in resolvable)
    if bad and aliases_known:
        print(f"    {os.path.basename(p)}: names {', '.join(bad)} — NOT served", file=sys.stderr)
        failed = True
    elif bad:
        print(f"    {os.path.basename(p)}: {', '.join(bad)} unconfirmed (see the note above)")
    else:
        print(f"    {os.path.basename(p)}: {' '.join(sorted(wanted))} ✓")

# The local backends, probed directly. litellm lists these lanes regardless.
for label, p in (("local-orch", orch_port), ("local-sub", sub_port)):
    s = socket.socket(); s.settimeout(1)
    up = s.connect_ex(("127.0.0.1", p)) == 0
    s.close()
    if up:
        print(f"    {label} backend :{p} ✓")
    else:
        print(f"    {label} backend :{p} DOWN — calls to that lane will fail. "
              f"Reload it with: ./host-reset.sh --full")

sys.exit(1 if failed else 0)
PYEOF

echo ""
echo "================================================================="
if [[ $RESET_FAILED -eq 1 ]]; then
  echo "\033[1;31mDONE WITH ERRORS\033[0m — see the lines above."
  exit 1
fi
echo "\033[1;32mHOST RESET COMPLETE\033[0m"
echo "Previous opencode configs are kept beside each file as <name>.<UTC>.jsonc."
if (( ! FULL )); then
  echo "The GPU lanes were left running. Use --full to reload them."
fi
echo "Catch clients up:  curl -fsSL http://<host>:$SHARE_PORT/client-reset.sh | zsh"
echo "================================================================="
