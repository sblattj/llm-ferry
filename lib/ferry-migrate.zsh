# ----------------- MIGRATE (client -> host) -----------------
# `ferry migrate` — turn THIS machine from a ferry CLIENT into a ferry HOST.
#
# The transition itself lives in client-to-host.sh, which must run from inside a
# checkout (it needs host-bootstrap.sh, host-reset.sh, the route template and
# lib/). But a machine bootstrapped as a client has only the single-file `ferry`
# CLI in ~/.local/bin and no checkout at all — so this command's ONE job is to
# make sure a checkout exists (use the one this CLI already lives in, otherwise
# clone one) and then hand off to the engine. Every flag is forwarded verbatim.
#
# It owns no migration logic of its own: keeping client-to-host.sh the single
# source of truth is deliberate, the same way `ferry update` delegates to
# host-reset.sh / client-reset.sh rather than re-implementing either.
cmd_migrate() {
  local dry_run=0 assume_yes=0 full=0 do_pull=0
  local dir="" repo="https://github.com/sblattj/llm-ferry.git"

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --dry-run) dry_run=1; shift ;;
      -y|--yes)  assume_yes=1; shift ;;
      --full)    full=1; shift ;;
      --pull)    do_pull=1; shift ;;
      --no-pull) do_pull=0; shift ;;
      --dir)
        [[ $# -ge 2 && -n "$2" ]] || { echo "Error: --dir needs a path" >&2; return 1; }
        dir="$2"; shift 2 ;;
      --dir=*)   dir="${1#--dir=}"; shift ;;
      --repo)
        [[ $# -ge 2 && -n "$2" ]] || { echo "Error: --repo needs a URL" >&2; return 1; }
        repo="$2"; shift 2 ;;
      --repo=*)  repo="${1#--repo=}"; shift ;;
      -h|--help)
        echo "Usage: ferry migrate [--dir PATH] [--repo URL] [--full] [--pull] [--dry-run] [--yes]"
        echo
        echo "  Turn THIS machine from a ferry client into a host. Ensures a repo"
        echo "  checkout exists (this CLI's own, or one cloned to --dir), then runs"
        echo "  client-to-host.sh from it."
        echo "  --dir PATH   where to clone the repo if a checkout is needed"
        echo "                 (default: ~/gdev/llm-ferry if ~/gdev exists, else ~/llm-ferry)"
        echo "  --repo URL   clone source (default: $repo)"
        echo "  --full       also reload the GPU lanes at the end (minutes)"
        echo "  --pull       let host-reset git-pull the checkout first"
        echo "  --dry-run    print every step and change nothing"
        echo "  --yes, -y    skip the confirmation prompt"
        return 0
        ;;
      *)
        echo "Error: unknown option for 'ferry migrate': $1" >&2
        echo "       ferry migrate [--dir PATH] [--repo URL] [--full] [--pull] [--dry-run] [--yes]" >&2
        return 1
        ;;
    esac
  done

  # Default checkout location: honour a ~/gdev dev tree if it exists, else a
  # neutral ~/llm-ferry.
  if [[ -z "$dir" ]]; then
    if [[ -d "$HOME/gdev" ]]; then dir="$HOME/gdev/llm-ferry"; else dir="$HOME/llm-ferry"; fi
  fi

  # A checkout is anything carrying the engine plus the host scripts it needs.
  _is_checkout() { [[ -f "$1/client-to-host.sh" && -f "$1/host-bootstrap.sh" && -d "$1/lib" ]]; }

  local checkout=""
  if _is_checkout "$APP_DIR"; then
    # ferry is already running from a checkout (a host box, or a re-run).
    checkout="$APP_DIR"
    echo ">>> ferry migrate — using this checkout: $checkout"
  elif _is_checkout "$dir"; then
    checkout="$dir"
    echo ">>> ferry migrate — using existing checkout: $checkout"
  elif [[ -e "$dir" ]]; then
    echo "Error: $dir exists but is not an llm-ferry checkout." >&2
    echo "       Point --dir at a fresh path, or update that checkout (git pull)." >&2
    return 1
  else
    # No checkout anywhere — clone one. This is the normal client case: the CLI
    # is a lone file in ~/.local/bin with no repo behind it.
    if ! command -v git >/dev/null 2>&1; then
      echo "Error: git is needed to clone the repo, and it is not on PATH." >&2
      echo "       Install git, or clone $repo yourself and re-run:" >&2
      echo "         ferry migrate --dir <that checkout>" >&2
      return 1
    fi
    if (( dry_run )); then
      echo ">>> ferry migrate — would clone $repo -> $dir"
      echo "    [dry-run] git clone $repo $dir"
      echo "    [dry-run] then: zsh $dir/client-to-host.sh --dry-run"
      echo "    (the engine is not on disk yet, so its own dry-run cannot be previewed here)"
      return 0
    fi
    echo ">>> Cloning $repo -> $dir ..."
    git clone "$repo" "$dir" || { echo "Error: clone failed." >&2; return 1; }
    if ! _is_checkout "$dir"; then
      echo "Error: cloned $dir but it has no client-to-host.sh — is the repo up to date?" >&2
      return 1
    fi
    checkout="$dir"
  fi

  # An older checkout may predate this feature.
  if [[ ! -f "$checkout/client-to-host.sh" ]]; then
    echo "Error: $checkout has no client-to-host.sh — update it (git pull) or clone fresh with --dir." >&2
    return 1
  fi

  # Hand off. Flags forwarded verbatim; the engine does the confirming, backups,
  # and the actual work.
  local -a fwd
  (( dry_run ))    && fwd+=(--dry-run)
  (( assume_yes )) && fwd+=(--yes)
  (( full ))       && fwd+=(--full)
  (( do_pull ))    && fwd+=(--pull)

  echo ">>> Running the migration engine: $checkout/client-to-host.sh ${fwd[*]}"
  zsh "$checkout/client-to-host.sh" "${fwd[@]}"
}
