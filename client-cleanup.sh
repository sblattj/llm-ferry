#!/bin/zsh
# client-cleanup.sh — remove every trace of llm-ferry from a CLIENT laptop.
# The inverse of client-bootstrap.sh: uninstall the ferry CLI, delete the
# client profile and ferry-written opencode + claude configs, strip the shell
# wrappers (opencode and claude) and the host-code alias from ~/.zshrc, and
# remove the guardrail files bootstrap installed.
#
#   curl -fsSL http://<host>:<share-port>/client-cleanup.sh | zsh
#   curl -fsSL http://<host>:<share-port>/client-cleanup.sh | zsh -s -- --full
#   curl -fsSL http://<host>:<share-port>/client-cleanup.sh | zsh -s -- --dry-run
#
# Two scopes:
#   default   removes ferry itself. Opencode's own session database
#             (~/.local/share/opencode) is REPORTED but kept — that is your
#             actual chat history, not ferry's.
#   --full    additionally deletes the opencode session DB. Irreversible.
#             The script refuses --full without --yes so a fat-fingered
#             `curl | zsh -s -- --full` cannot wipe history on its own.
#
# --dry-run prints every action without touching anything. Run it first.
#
# Scope-agnostic on purpose: it removes whatever is actually there, so it undoes
# a default bootstrap, a `--profiles-only` one, and a `--no-opencode` one without
# being told which. Anything a narrow bootstrap never created is reported as
# "not present" rather than treated as an error.

set -eu

DRY_RUN=0
FULL=0
YES=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --full)    FULL=1 ;;
    --yes)     YES=1 ;;
    *) echo "Unknown flag: $arg (want: --dry-run, --full, --yes)"; exit 1 ;;
  esac
done

if [[ $FULL -eq 1 && $YES -eq 0 ]]; then
  echo "Refusing --full without --yes."
  echo "  --full deletes ~/.local/share/opencode (your session history) and"
  echo "  cannot be undone. Re-run with:  --full --yes"
  exit 1
fi

# run/do: under --dry-run, print instead of execute. Deliberately NOT a plain
# `eval` wrapper — the arguments are all literals from this script.
run() {
  if [[ $DRY_RUN -eq 1 ]]; then
    echo "    [dry-run] $*"
  else
    "$@"
  fi
}

echo "================================================================="
echo "                  LLM-FERRY CLIENT CLEANUP"
[[ $DRY_RUN -eq 1 ]] && echo "                     (DRY RUN — nothing changes)"
[[ $FULL    -eq 1 ]] && echo "              (FULL — includes opencode session DB)"
echo "================================================================="

# --- 1. Report the opencode session store ----------------------------------
# It exists whether or not ferry was ever installed (a bare opencode makes it),
# so it is inventory, not an action, unless --full was given.
OC_DATA="$HOME/.local/share/opencode"
if [[ -d "$OC_DATA" ]]; then
  oc_size=$(du -sh "$OC_DATA" 2>/dev/null | awk '{print $1}')
  echo ">>> opencode data store: $OC_DATA ($oc_size)"
  if [[ $FULL -eq 1 ]]; then
    echo "    --full given: DELETING session history."
    run rm -rf "$OC_DATA"
  else
    echo "    Keeping it (default scope). Re-run with --full --yes to delete."
  fi
else
  echo ">>> opencode data store: not present"
fi
echo ""

# --- 2. Remove the ferry CLI ------------------------------------------------
FERRY_BIN="$HOME/.local/bin/ferry"
echo ">>> Removing the 'ferry' CLI..."
if [[ -f "$FERRY_BIN" ]]; then
  run rm -f "$FERRY_BIN"
  echo "    Removed $FERRY_BIN"
else
  echo "    Not installed ($FERRY_BIN missing) — skipping."
fi
echo ""

# --- 3. Remove the client profile + ferry-written opencode profiles ---------
# The whole directory goes, which also takes the claude profile
# (~/.config/ferry/claude.json) and the client.json "claude_mode" key with it —
# there is no scenario where client.json survives this script.
echo ">>> Removing ~/.config/ferry (client profile, opencode lane profiles,"
echo "    claude profile, last-lane marker, takeover snapshots)..."
if [[ -d "$HOME/.config/ferry" ]]; then
  run rm -rf "$HOME/.config/ferry"
  echo "    Removed ~/.config/ferry"
else
  echo "    Not present — skipping."
fi
echo ""

# --- The surgical unwiring both opencode config files share -----------------
# `ferry opencode` writes THREE things a cleanup has to take back out:
#   1. provider.ferry in opencode.json (the takeover proper);
#   2. the goal-plugin spec in the `plugin` array of opencode.json AND of
#      tui.json (v1.30.1 mirrors it: opencode reads TUI-half plugins from
#      tui.json only);
#   3. a top-level `command.goal` in opencode.json, the plugin's /goal command.
# One function does one file, so sections 4 and 4b cannot drift apart. Anything
# else in either file is left exactly as it was, and the original is snapshotted
# to <name>.<UTC>.jsonc before the first write — the same convention `ferry
# opencode` uses, one snapshot per file per run.
#
# The plugin matching rules MIRROR is_goal_spec()/is_path_entry() in
# lib/ferry-integrate.zsh: every REMOTE spelling ferry ever wrote is ours to
# remove (bare npm name, the scoped upstream name, github:owner/repo with or
# without a #ref, git+https, an archive/refs/tags tarball URL, and the
# ["spec", {opts}] tuple form), while a LOCAL FILESYSTEM PATH never is —
# opencode accepts a path and Bun cannot resolve a private repo over `github:`,
# so a hard fork of this plugin can only be named that way, and it is the
# user's entry, not ferry's.
unwire_goal_plugin() {   # <config path> <opencode|tui>
  python3 - "$1" "$2" <<'PYEOF'
import json, sys, shutil, datetime, os

path, mode = sys.argv[1], sys.argv[2]

# --- Mirrored from lib/ferry-integrate.zsh (GOAL_* constants, is_path_entry,
# is_goal_spec, GOAL_COMMAND). Duplicated on purpose: this script is fetched
# and piped straight into zsh, so it cannot import anything.
GOAL_NAME_MATCHES = {
    "opencode-goal-plugin",
    "@prevalentware/opencode-goal-plugin",
    "willytop8/opencode-goal-plugin",
    "github:willytop8/opencode-goal-plugin",
    "sblattj/opencode-goal-plugin",
    "github:sblattj/opencode-goal-plugin",
}
GOAL_REPO_MARKERS = ("sblattj/opencode-goal-plugin", "willytop8/opencode-goal-plugin")
GOAL_COMMAND = {
    "description": "Set a session-scoped goal and auto-continue until complete.",
    "template": "$ARGUMENTS",
    "agent": "build",
}


def raw_spec(entry):
    """The spec string of a plugin entry ("spec" or ["spec", {opts}])."""
    return entry[0] if isinstance(entry, list) and entry else entry


def pkg_name(entry):
    """The npm NAME half of a plugin entry (the raw string when it has none)."""
    entry = raw_spec(entry)
    if not isinstance(entry, str):
        return None
    if "#" in entry:
        entry = entry.rsplit("#", 1)[0]
    # Split at the FIRST separating "@": the canonical spec is
    # `opencode-goal-plugin@https://...`, and a URL may carry an "@" of its own.
    # A leading "@" is an npm SCOPE, not a separator.
    if entry.startswith("@"):
        rest = entry[1:]
        return "@" + rest.split("@", 1)[0] if "@" in rest else entry
    if "@" in entry:
        return entry.split("@", 1)[0]
    return entry


def is_path_entry(entry):
    """A local filesystem path (or file:// URL): somebody's fork, never ours."""
    raw = raw_spec(entry)
    if not isinstance(raw, str):
        return False
    return raw.startswith(("/", ".", "~")) or raw.lower().startswith("file:")


def is_goal_spec(entry):
    """Any REMOTE spelling of ferry's goal plugin. A local path is excluded."""
    raw = raw_spec(entry)
    if not isinstance(raw, str):
        return False
    if is_path_entry(raw):
        return False
    name = pkg_name(raw)
    if isinstance(name, str) and name.lower() in GOAL_NAME_MATCHES:
        return True
    low = raw.lower()
    return any(m in low for m in GOAL_REPO_MARKERS)


try:
    with open(path) as f:
        cfg = json.load(f)
except Exception:
    print("    Could not parse as JSON — leaving it untouched.")
    sys.exit(0)
if not isinstance(cfg, dict):
    print("    Could not parse as JSON — leaving it untouched.")
    sys.exit(0)

# --- Decide everything BEFORE touching the file, so "nothing of ours is in
# here" stays a byte-for-byte no-op (a second cleanup run, a --profiles-only
# machine, somebody else's opencode setup).
# The provider fingerprint is the OBJECT KEY, never the apiKey: 'ferry opencode'
# writes provider.ferry and only provider.ferry, while the apiKey varies between
# 'local' and the front door's real master key (v1.22.0). Matching apiKey ==
# 'local' (the old fingerprint) missed every authed setup — and would have eaten
# a user's own unrelated provider that happens to use a 'local' token.
providers = cfg.get("provider") if mode == "opencode" else None
ferry_keys = ([name for name, p in providers.items()
               if isinstance(p, dict) and name == "ferry"]
              if isinstance(providers, dict) else [])

plugins = cfg.get("plugin")
kept_plugins, dead_plugins = [], []
if isinstance(plugins, list):
    for p in plugins:
        (dead_plugins if is_goal_spec(p) else kept_plugins).append(p)

# `command.goal` goes ONLY when it is still ferry's verbatim. `ferry opencode`
# merges it in and never overwrites an existing one, so anything else in there
# is the user's own command wearing our key.
commands = cfg.get("command") if mode == "opencode" else None
drop_goal_command = isinstance(commands, dict) and commands.get("goal") == GOAL_COMMAND

if not (ferry_keys or dead_plugins or drop_goal_command):
    print("    Nothing ferry-shaped found — file left unchanged.")
    sys.exit(0)

# tui.json exists ONLY because ferry mirrored the plugin into it. If the entry
# we are pulling out was all it held (its $schema is ours too — `ferry opencode`
# writes it when it creates the file), the file itself is the trace: delete it
# rather than leave an empty shell behind. No snapshot: there is nothing in it
# that was not ours.
if mode == "tui" and not kept_plugins and not (set(cfg) - {"$schema", "plugin"}):
    for e in dead_plugins:
        print(f"    Removed goal plugin entry {raw_spec(e)!r}")
    os.remove(path)
    print(f"    Removed {path} (it held nothing but ferry's plugin entry)")
    sys.exit(0)

# Snapshot before the FIRST write, same convention as `ferry opencode`
# (<name>.<UTC>.jsonc) — one per file per run, not one per change.
stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
snap = (path[: -len(".json")] if path.endswith(".json") else path) + f".{stamp}.jsonc"
shutil.copy2(path, snap)

for name in ferry_keys:
    del providers[name]
    print(f"    Removed ferry provider '{name}'")
# If ferry was the ONLY provider, the model/agent fields it wrote now point at
# nothing — drop them so a fresh `opencode` doesn't error on a dangling model.
if ferry_keys and not providers:
    for k in ("model", "small_model"):
        if k in cfg:
            del cfg[k]
            print(f"    Removed dangling '{k}' (no providers left)")

if dead_plugins:
    for e in dead_plugins:
        print(f"    Removed goal plugin entry {raw_spec(e)!r}")
    if kept_plugins:
        cfg["plugin"] = kept_plugins
    else:
        # An empty `plugin: []` is a leftover of ours, not a user setting.
        del cfg["plugin"]
        print("    Removed the now-empty 'plugin' list")

if drop_goal_command:
    del commands["goal"]
    print("    Removed the ferry /goal command")
    if not commands:
        del cfg["command"]
        print("    Removed the now-empty 'command' block")

with open(path, "w") as f:
    json.dump(cfg, f, indent=2)
    f.write("\n")
print(f"    Previous file kept beside it as .{stamp}.jsonc")
PYEOF
}

# grep_goal_plugin: does this file name ferry's goal plugin REMOTELY? Used by
# --dry-run only, where there is no parsed config to inspect. The second grep is
# what keeps a local fork out of the report: a path entry's opening quote is
# followed by /, ., ~ or file:, and ferry writes these files with indent=2, so
# every array element sits on its own line.
grep_goal_plugin() {
  grep -iE '"[^"]*opencode-goal-plugin' "$1" 2>/dev/null \
    | grep -qvE '"(/|\.|~|file:)'
}

# --- 4. Unwire opencode's own default config --------------------------------
# Bootstrap ran `ferry opencode` against this file, which replaced the
# provider block with a baseURL pointing at the host, appended the goal plugin
# to `plugin`, and merged in `command.goal`. Removing the whole file would eat
# any non-ferry keys the user added since, so we strip only those three. If
# nothing ferry-shaped is found, the file is left exactly as-is.
OC_CFG="$HOME/.config/opencode/opencode.json"
echo ">>> Unwiring $OC_CFG ..."
if [[ -f "$OC_CFG" ]]; then
  if [[ $DRY_RUN -eq 1 ]]; then
    found=0
    if grep -q '"ferry": {' "$OC_CFG" 2>/dev/null; then
      echo "    [dry-run] would strip the ferry provider block from $OC_CFG"
      found=1
    fi
    if grep_goal_plugin "$OC_CFG"; then
      echo "    [dry-run] would remove the goal plugin entry from $OC_CFG"
      found=1
    fi
    if grep -q '"goal": {' "$OC_CFG" 2>/dev/null; then
      echo "    [dry-run] would remove the /goal command from $OC_CFG (only if still verbatim)"
      found=1
    fi
    [[ $found -eq 0 ]] && echo "    Nothing ferry-shaped found — file would be left alone."
  else
    unwire_goal_plugin "$OC_CFG" opencode
  fi
else
  echo "    Not present — skipping."
fi
echo ""

# --- 4b. Unwire opencode's tui.json (the plugin's TUI half) -----------------
# Since v1.30.1 `ferry opencode` MIRRORS the goal-plugin spec into
# ~/.config/opencode/tui.json, because opencode reads TUI-half plugins from
# tui.json/tui.jsonc alone — opencode.json's `plugin` array feeds the server
# loader only. Cleaning opencode.json and stopping there left the sidebar half
# still wired. Path convention: $HOME/.config, spelled literally, exactly as
# every other path in this script; a machine that relocates its config with
# XDG_CONFIG_HOME is out of scope for the piped one-liner and should point the
# script at the real HOME instead.
TUI_CFG="$HOME/.config/opencode/tui.json"
echo ">>> Unwiring $TUI_CFG ..."
if [[ -f "$TUI_CFG" ]]; then
  if [[ $DRY_RUN -eq 1 ]]; then
    if grep_goal_plugin "$TUI_CFG"; then
      echo "    [dry-run] would remove the goal plugin entry from $TUI_CFG"
      echo "    [dry-run] would delete $TUI_CFG if that entry was all it held"
    else
      echo "    Nothing ferry-shaped found — file would be left alone."
    fi
  else
    unwire_goal_plugin "$TUI_CFG" tui
  fi
else
  echo "    Not present — skipping."
fi
echo ""

# --- 5. Strip the shell wrappers + host-code alias from ~/.zshrc ------------
# Reuses bootstrap's own markers for the wrapper block(s) — the opencode one
# and the claude one `ferry claude` installs — plus the host-code alias, its
# comment banner, and the legacy `alias opencode*=` / `alias claude-ferry*=`
# lines from older hand-wired setups. Delimited-block removal is line-based
# and safe; the alias/banner are single lines matched exactly.
ZSHRC="$HOME/.zshrc"
echo ">>> Stripping ferry wrappers from ~/.zshrc..."
if [[ -f "$ZSHRC" ]]; then
  if [[ $DRY_RUN -eq 1 ]]; then
    hits=$(grep -cE 'ferry opencode profiles|ferry claude profiles|alias host-code=|# LLM-Ferry Shortcut|alias claude-ferry' "$ZSHRC" 2>/dev/null || true)
    echo "    [dry-run] $hits ferry line(s)/marker(s) found in ~/.zshrc"
  else
    python3 - "$ZSHRC" <<'PYEOF'
import sys
rc = sys.argv[1]
with open(rc) as f:
    lines = f.readlines()
out, skip, removed = [], False, 0
for ln in lines:
    s = ln.rstrip("\n")
    if s == "# >>> ferry opencode profiles >>>":
        skip = True; removed += 1; continue
    if s == "# <<< ferry opencode profiles <<<":
        skip = False; removed += 1; continue
    if s == "# >>> ferry claude profiles >>>":
        skip = True; removed += 1; continue
    if s == "# <<< ferry claude profiles <<<":
        skip = False; removed += 1; continue
    if skip:
        continue
    t = s.strip()
    if (t.startswith("alias host-code=")
        or t == "# LLM-Ferry Shortcut"
        or t.startswith("alias opencode-cloud=")
        or t.startswith("alias opencode-local=")
        or t.startswith("alias opencode=")
        or t.startswith("alias claude-ferry=")
        or t.startswith("alias claude-ferry-local=")):
        removed += 1
        continue
    out.append(ln)
# Collapse any trailing blank lines the removals may have left.
while out and out[-1].strip() == "":
    out.pop()
with open(rc, "w") as f:
    f.writelines(out)
    if out:
        f.write("\n")
print(f"    Removed {removed} ferry line(s) from ~/.zshrc")
PYEOF
  fi
else
  echo "    No ~/.zshrc — skipping."
fi
echo ""

# --- 6. Remove the guardrails bootstrap installed into opencode's global dirs
echo ">>> Removing the local-lane guardrails (/fan-out + spawning-subagents)..."
# Both spellings: opencode accepts `skill/` and `skills/`, and the two installers
# disagree — client-bootstrap.sh writes the plural, `ferry opencode`'s host-side
# guardrail install writes the singular. Removing only one leaves the other
# loading on every session, which is exactly the trace this script exists to
# clear on a machine that has been both a host and a client.
for p in \
  "$HOME/.config/opencode/command/fan-out.md" \
  "$HOME/.config/opencode/skills/spawning-subagents/SKILL.md" \
  "$HOME/.config/opencode/skill/spawning-subagents/SKILL.md"
do
  if [[ -f "$p" ]]; then
    run rm -f "$p"
    echo "    Removed $p"
  else
    echo "    Not present: $p"
  fi
done
# Remove the skill dir only if we just emptied it (never rmdir a dir that may
# hold user files).
for d in \
  "$HOME/.config/opencode/skills/spawning-subagents" \
  "$HOME/.config/opencode/skill/spawning-subagents"
do
  if [[ -d "$d" ]]; then
    run rmdir "$d" 2>/dev/null || true
  fi
done
echo ""

echo "================================================================="
if [[ $DRY_RUN -eq 1 ]]; then
  echo "DRY RUN COMPLETE — re-run without --dry-run to apply."
else
  echo "\033[1;32mCLEANUP COMPLETE\033[0m"
fi
echo "Left in place on purpose:"
echo "  - the opencode binary itself (it is not ferry's)"
[[ $FULL -eq 0 ]] && echo "  - $OC_DATA (your session history; --full --yes removes it)"
echo "Open a NEW terminal so the stripped wrappers/aliases unload."
echo "================================================================="
