#!/bin/zsh
# client-cleanup.sh — remove every trace of llm-ferry from a CLIENT laptop.
# The inverse of client-bootstrap.sh: uninstall the ferry CLI, delete the
# client profile and ferry-written opencode configs, strip the shell wrappers
# and the host-code alias from ~/.zshrc, and remove the guardrail files
# bootstrap installed.
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
echo ">>> Removing ~/.config/ferry (client profile, opencode lane profiles,"
echo "    last-lane marker, takeover snapshots)..."
if [[ -d "$HOME/.config/ferry" ]]; then
  run rm -rf "$HOME/.config/ferry"
  echo "    Removed ~/.config/ferry"
else
  echo "    Not present — skipping."
fi
echo ""

# --- 4. Unwire opencode's own default config --------------------------------
# Bootstrap ran `ferry opencode` against this file, which replaced the
# provider block with a baseURL pointing at the host. Removing the whole file
# would eat any non-ferry keys the user added since, so we strip only the
# ferry-injected provider: identifiable because ferry writes apiKey "local"
# and a baseURL that is NOT api.openai.com. If nothing ferry-shaped is found,
# the file is left exactly as-is.
OC_CFG="$HOME/.config/opencode/opencode.json"
echo ">>> Unwiring $OC_CFG ..."
if [[ -f "$OC_CFG" ]]; then
  if [[ $DRY_RUN -eq 1 ]]; then
    if grep -q '"apiKey": "local"' "$OC_CFG" 2>/dev/null; then
      echo "    [dry-run] would strip the ferry provider block from $OC_CFG"
    else
      echo "    No ferry provider block found — file would be left alone."
    fi
  else
    python3 - "$OC_CFG" <<'PYEOF'
import json, sys, shutil, datetime
path = sys.argv[1]
try:
    with open(path) as f:
        cfg = json.load(f)
except Exception:
    print("    Could not parse as JSON — leaving it untouched.")
    sys.exit(0)
providers = cfg.get("provider")
if not isinstance(providers, dict):
    print("    No provider block — leaving it untouched.")
    sys.exit(0)
ferry_keys = [
    name for name, p in providers.items()
    if isinstance(p, dict)
    and isinstance(p.get("options"), dict)
    and p["options"].get("apiKey") == "local"
]
if not ferry_keys:
    print("    No ferry provider block found — file left unchanged.")
    sys.exit(0)
# Snapshot before writing, same convention as `ferry opencode` (<name>.<UTC>.jsonc).
stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
shutil.copy2(path, path.replace(".json", f".{stamp}.jsonc"))
for name in ferry_keys:
    del providers[name]
    print(f"    Removed ferry provider '{name}'")
# If ferry was the ONLY provider, the model/agent fields it wrote now point at
# nothing — drop them so a fresh `opencode` doesn't error on a dangling model.
if not providers:
    for k in ("model", "small_model"):
        if k in cfg:
            del cfg[k]
            print(f"    Removed dangling '{k}' (no providers left)")
with open(path, "w") as f:
    json.dump(cfg, f, indent=2)
    f.write("\n")
print(f"    Previous file kept beside it as .{stamp}.jsonc")
PYEOF
  fi
else
  echo "    Not present — skipping."
fi
echo ""

# --- 5. Strip the shell wrappers + host-code alias from ~/.zshrc ------------
# Reuses bootstrap's own markers for the wrapper block, plus the alias and the
# comment banner it added. Delimited-block removal is line-based and safe; the
# alias/banner are single lines matched exactly.
ZSHRC="$HOME/.zshrc"
echo ">>> Stripping ferry wrappers from ~/.zshrc..."
if [[ -f "$ZSHRC" ]]; then
  if [[ $DRY_RUN -eq 1 ]]; then
    hits=$(grep -cE 'ferry opencode profiles|alias host-code=|# LLM-Ferry Shortcut' "$ZSHRC" 2>/dev/null || true)
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
    if skip:
        continue
    t = s.strip()
    if (t.startswith("alias host-code=")
        or t == "# LLM-Ferry Shortcut"
        or t.startswith("alias opencode-cloud=")
        or t.startswith("alias opencode-local=")
        or t.startswith("alias opencode=")):
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
