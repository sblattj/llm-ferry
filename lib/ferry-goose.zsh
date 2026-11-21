# ferry goose: run Goose (aaif-goose/goose, formerly block/goose) on a ferry lane.
#
# Wiring: a wrapper only. `~/.local/bin/goose-ferry` exports Goose's own env
# vars and execs the real `goose`; NO Goose config file is read, written or
# snapshotted, so ~/.config/goose/config.yaml, the user's providers and their
# keyring entries are untouched, and plain `goose` keeps its own provider.
# `ferry goose --unwire` removes the wrapper and the record, nothing else.
#
# Research, all read from source at tag v1.49.0 (commit 71fc4be1ed72, the
# same version as the installed /opt/homebrew/bin/goose, `goose --version`
# prints 1.49.0). Paths are relative to crates/:
#  (1) Provider config: the `openai` provider (goose/src/providers/
#      openai_def.rs:63 from_env) resolves its endpoint from OPENAI_HOST
#      (env, wins, :275 resolve_base_url) else OPENAI_BASE_URL (env or
#      config.yaml) else OPENAI_HOST in config.yaml, default
#      https://api.openai.com (:296). OPENAI_BASE_URL is split into host +
#      path (goose-providers/src/openai.rs:129 parse_openai_base_url): a
#      trailing /v1 is stripped from the host and selects the base path
#      `v1/chat/completions` (openai_def.rs:90-93); OPENAI_BASE_PATH is read
#      from env only in that case (openai_def.rs:99), so a persisted
#      config.yaml OPENAI_BASE_PATH cannot shadow it. The wrapper therefore
#      sets OPENAI_BASE_URL=<ferry>/v1 and UNSETS OPENAI_HOST and
#      OPENAI_BASE_PATH (an exported OPENAI_HOST would outrank it).
#      GOOSE_PROVIDER / GOOSE_MODEL pick provider and model
#      (goose/src/config/base.rs:755 get_param upper-cases the key and checks
#      the environment before config.yaml). Goose also supports declarative
#      custom providers (goose/src/config/declarative_providers.rs, JSON in
#      the config dir) but those are files under ~/.config/goose that ferry
#      would have to own; the env route needs none, so it is not used.
#      Anthropic: goose has an `anthropic` provider with ANTHROPIC_HOST, but
#      ferry uses chat/completions here (rule: prefer OpenAI).
#  (2) Key: OPENAI_API_KEY from the environment. Config::get_secrets
#      (goose/src/config/base.rs:887) takes EVERY secret from the environment
#      when the primary one (OPENAI_API_KEY) is set in env, so with the wrapper
#      the system keyring (base.rs:31 KEYRING_SERVICE "goose") is not read for
#      this provider. The wrapper exports it from $FERRY_KEY, read at run time
#      from the 0600 ~/.config/ferry/agents.key; the key is in no file and no
#      argv. The wrapper does NOT set GOOSE_DISABLE_KEYRING, because that would
#      move the user's OTHER providers' secrets to a file (base.rs:206, :388
#      secret_storage). The e2e test sets GOOSE_DISABLE_KEYRING=1 itself so it
#      never touches a real keychain.
#  (3) Custom headers: yes. OPENAI_CUSTOM_HEADERS, a comma list of
#      `Name=Value` (goose-providers/src/openai.rs:983 parse_custom_headers),
#      read as a secondary secret (openai_def.rs:108-113) and applied to every
#      request (openai_def.rs:153 with_headers). The wrapper sends X-Ferry-Client and,
#      when FERRY_FLEET was set at wire time, X-Ferry-Fleet. A comma or `=`
#      inside a value would break that format, so such characters are
#      replaced by `_`.
#  (4) API: OpenAI chat/completions, streaming (supports_streaming defaults
#      true, openai.rs:201; POST <host>/v1/chat/completions, openai.rs:838
#      and 851 stream_openai_compat). Responses API is used only for known
#      responses model names or a base path containing `responses`
#      (openai.rs:416 should_use_responses_api); lane ids like `heavy` are
#      neither. The model catalogue is GET <host>/v1/models (openai.rs:560
#      fetch_models_from_api), which ferry serves as the lane catalogue.
#  (5) Least invasive: env only, in a wrapper (this file). Goose picks its
#      config dir via GOOSE_PATH_ROOT, else etcetera XDG/HOME
#      (goose/src/config/paths.rs:8,22-36), so HOME= redirects it in tests.
#      Telemetry: GOOSE_TELEMETRY_OFF=1 disables posthog (posthog.rs:23); the
#      e2e sets it, the wrapper leaves the user's choice alone.
#
# Lanes: Goose takes ONE model per run. The wrapper defaults GOOSE_MODEL to
# the wired lane; `FERRY_MODEL=flash goose-ferry ...` (or `--model flash` on
# `goose run`/`session`) picks another lane from the catalogue.

cmd_goose() { _agent_main goose "$@"; }

_goose_help() {
  cat <<'EOF'
ferry goose - run Goose (aaif-goose/goose) on a ferry lane.

Usage:
  ferry goose [--host H] [--port P] [--key K] [--model LANE]

  Writes ~/.local/bin/goose-ferry, a wrapper that exports GOOSE_PROVIDER=openai,
  GOOSE_MODEL=<lane>, OPENAI_BASE_URL=<ferry>/v1, OPENAI_API_KEY (read from the
  0600 ~/.config/ferry/agents.key at run time) and OPENAI_CUSTOM_HEADERS
  (X-Ferry-Client, X-Ferry-Fleet when set), then execs goose. No Goose config
  file is touched: plain `goose` keeps its own provider and keyring.

  goose-ferry run -t "..." --no-session     one headless run on the ferry lane
  goose-ferry                               interactive session on the ferry lane
  FERRY_MODEL=flash goose-ferry ...         another lane for one run

  Limitation: one model per run; `goose configure` inside goose-ferry still
  edits the user's own config.yaml.
EOF
}

_goose_wire() {
  _agent_python <<'PYEOF'
import shlex

def _clean(v):                 # OPENAI_CUSTOM_HEADERS is "A=B,C=D"
    return re.sub(r"[,=\s]", "_", v)

hdr = ",".join(f"{k}={_clean(v)}" for k, v in HEADERS.items())
wrapper(os.path.join(HOME, ".local", "bin", "goose-ferry"),
        {"GOOSE_PROVIDER": "openai",
         "GOOSE_MODEL": '"${FERRY_MODEL:-%s}"' % LANE,
         "OPENAI_BASE_URL": shlex.quote(BASE),
         "OPENAI_API_KEY": '"$FERRY_KEY"',
         "OPENAI_CUSTOM_HEADERS": shlex.quote(hdr)},
        "goose",
        pre="unset OPENAI_HOST OPENAI_BASE_PATH")
save_record({"lanes": LANES})
PYEOF
}
