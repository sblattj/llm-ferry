# ferry openhands: wire the OpenHands CLI (`openhands`) to ferry (issue #3).
#
# Research, answered from the source (OpenHands/OpenHands-CLI @ 283c6ac, CLI
# 1.16.0; the PyPI `openhands` 1.16.0 I ran the e2e on pins openhands-sdk
# 1.21.0, so SDK lines below cite that wheel, openhands/sdk/llm/):
#
# (1) Provider config. There is no provider list. The CLI holds ONE agent in
#     <persistence dir>/agent_settings.json (openhands_cli/locations.py:51,
#     stores/agent_store.py:263-285), the persistence dir being ~/.openhands
#     unless OPENHANDS_PERSISTENCE_DIR is set (locations.py:5-12). Its LLM
#     goes through LiteLLM: model "openai/<id>" plus llm.base_url is a custom
#     OpenAI-compatible endpoint (sdk llm.py:180 base_url, llm.py:973
#     api_base=self.base_url). The Anthropic base URL is the same field with a
#     "anthropic/<id>" model; ferry uses the OpenAI side.
# (2) Key. Stored as a literal in agent_settings.json by default
#     (agent_store.py:474-476 dumps with expose_secrets). The CLI can instead
#     take it from the environment, but ONLY with the flag
#     --override-with-envs (argparsers/util.py:34-42): LLM_API_KEY, LLM_BASE_URL
#     and LLM_MODEL (agent_store.py:133-135) then override the stored agent
#     (agent_store.py:197-225, 241-254, 353-358) and are never persisted
#     (agent_store.py:184-186). Without the flag the variables are ignored with
#     a warning (agent_store.py:156-178).
# (3) Custom headers. Yes, but only as a settings field: LLM.extra_headers
#     (sdk llm.py:287-290) is forwarded on every chat and responses call
#     (sdk options/chat_options.py:31-33, responses_options.py:30-32). There
#     is no env var or flag for it, so X-Ferry-Client / X-Ferry-Fleet need an
#     agent_settings.json.
# (4) API spoken: OpenAI chat/completions for an "openai/<lane>" model. The SDK
#     switches to /v1/responses only for model names on its Responses list
#     (sdk llm.py:1366-1370, utils/model_features.py:201); lane ids are not on
#     it. Observed e2e: POST /v1/chat/completions, non-streaming. The request
#     body also carries prompt_cache_key and reasoning_effort, which the front
#     door must tolerate (it did against the stub; not checked against the
#     live gateway, by rule).
# (5) Least invasive wiring: a ferry-OWNED persistence dir,
#     ~/.config/ferry/openhands/, holding one agent_settings.json that has the
#     lane model, the base URL and the X-Ferry headers but NO api_key. The
#     `openhands-ferry` wrapper exports OPENHANDS_PERSISTENCE_DIR at that dir,
#     LLM_API_KEY="$FERRY_KEY" (read from the 0600 key file at run time),
#     LLM_BASE_URL, LLM_MODEL=openai/<lane>, and runs
#     `openhands --override-with-envs "$@"`. The user's ~/.openhands and the
#     plain `openhands` command are never touched. Trade-off, stated plainly:
#     because the persistence dir moves, `openhands-ferry` has its own
#     conversation history, mcp.json and CLI settings (the CLI keeps all of
#     them under that one directory, locations.py:15-47, 54). Pick another
#     lane for one run with FERRY_LANE=flash openhands-ferry.
#     Headless one-shot (used by the e2e): `openhands-ferry --headless -t
#     "<task>"` (entrypoint.py:104-108).
#
# The ferry-owned settings file is JSON (no comment room for the managed
# marker), so write_owned() trusts the record instead: a file at that path
# that ferry did not record is never replaced.

_openhands_help() {
  cat <<'EOF2'
ferry openhands - wire the OpenHands CLI to ferry (issue #3)

Writes (nothing of yours is edited):
  ~/.local/bin/openhands-ferry         wrapper: sets OPENHANDS_PERSISTENCE_DIR,
                                       LLM_API_KEY (read from the 0600 key file
                                       at run time), LLM_BASE_URL, LLM_MODEL and
                                       runs `openhands --override-with-envs`
  ~/.config/ferry/openhands/agent_settings.json
                                       ferry's own agent: lane model, base URL,
                                       X-Ferry-Client / X-Ferry-Fleet headers,
                                       no key

Use:  openhands-ferry                  # TUI on the ferry lane
      openhands-ferry --headless -t "fix the failing test"
      FERRY_LANE=flash openhands-ferry # another lane for one run
Your own `openhands` and ~/.openhands are untouched. Limitation: openhands-ferry
keeps its own history, MCP list and settings (OpenHands stores them all under
one directory). Custom headers can only come from the settings file, so the
settings file is what carries X-Ferry-Client.
EOF2
}

_openhands_wire() {
  local pdir="$HOME/.config/ferry/openhands"
  _agent_python "$pdir" <<'PYEOF'
import json, os, shlex, sys
pdir = sys.argv[1]
settings = os.path.join(pdir, "agent_settings.json")
agent = {"llm": {"model": f"openai/{LANE}", "base_url": BASE,
                 "extra_headers": HEADERS, "usage_id": "agent"}}
write_owned(settings, json.dumps(agent, indent=2) + "\n", 0o600)
print(f"    Agent settings: {settings}")
wrapper(os.path.join(HOME, ".local", "bin", "openhands-ferry"),
        {"OPENHANDS_PERSISTENCE_DIR": shlex.quote(pdir),
         "LLM_API_KEY": '"$FERRY_KEY"',
         "LLM_BASE_URL": shlex.quote(BASE),
         "LLM_MODEL": "\"openai/${FERRY_LANE:-" + LANE + "}\""},
        "openhands", ["--override-with-envs"])
print(f"    Wrapper:        {os.path.join(HOME, '.local', 'bin', 'openhands-ferry')}")
print(f"    Lane: {LANE}  Endpoint: {BASE}")
print("    Run `openhands-ferry`; your own `openhands` and ~/.openhands are untouched.")
save_record()
PYEOF
}

cmd_openhands() { _agent_main openhands "$@"; }
