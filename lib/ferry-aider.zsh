# ferry aider: point Aider (Aider-AI/aider, PyPI `aider-chat`) at the ferry
# endpoint through an `aider-ferry` wrapper. Nothing of the user's is edited.
#
# Research (Aider-AI/aider @ 5dc9490, __init__.py:3 says 0.86.3.dev; cloned
# --depth 1 into a scratch dir, every fact below read from that tree):
#
# (1) Provider config / base URL. Aider talks to models through LiteLLM and
#     has no provider table: the model name carries the provider. The OpenAI
#     compatible base URL is `--openai-api-base` (aider/args.py:77), which
#     aider/main.py:620-621 copies into os.environ["OPENAI_API_BASE"] (the
#     variable LiteLLM reads). A model named `openai/<id>` goes to that base
#     with the model id `<id>` (provider = first path piece,
#     aider/models.py:717-719). There is no Anthropic-base path we need: the
#     OpenAI one reaches every lane. Per-model knobs live in a YAML list
#     (`--model-settings-file`, aider/args.py:121; loaded by
#     aider/models.py:1085 register_models into ModelSettings, :135-157) and
#     context/cost metadata in a JSON5 file (`--model-metadata-file`,
#     aider/args.py:127; aider/models.py:1112 register_litellm_models, parsed
#     with json5 so a leading // comment is legal, :1122).
# (2) Key. `--openai-api-key` (aider/args.py:69, main.py:615-616) or the env
#     var OPENAI_API_KEY (aider/models.py:725, fast_validate_environment). A
#     key on argv would show in `ps`, so the wrapper exports OPENAI_API_KEY
#     from $FERRY_KEY (the 0600 agents.key) at run time and passes only the
#     base on argv.
# (3) Custom headers. Yes, and only through model settings: a settings entry
#     named exactly `aider/extra_params` is deep-merged into EVERY model's
#     extra_params (aider/models.py:344-346 finds it, :405-422 merges), and
#     extra_params is splatted into litellm.completion(**kwargs)
#     (aider/models.py:1010-1011, :1036), where `extra_headers` is LiteLLM's
#     own kwarg. So X-Ferry-Client (and X-Ferry-Fleet) ride on every request
#     whatever lane `--model` picks.
# (4) API spoken. OpenAI chat/completions via litellm.completion, streaming by
#     default (`--stream`, aider/args.py:323), so POST {base}/chat/completions.
# (5) Least invasive wiring, and why the wrapper passes flags, not env only:
#     * ferry-owned files under ~/.config/ferry/aider/ (model-settings.yml,
#       model-metadata.json) handed over with --model-settings-file and
#       --model-metadata-file. Aider searches home, git root, cwd and then the
#       command-line file, and the LAST one wins per model name
#       (aider/main.py:305-332 generate_search_path_list, reversed-unique; the
#       list is loaded in order), so ferry's entries override a same-named user
#       entry but the user's ~/.aider.model.settings.yml and repo files are
#       never read-modified or replaced.
#     * the base goes on argv, not only in OPENAI_API_BASE: a user's .env
#       loads with override=True (aider/main.py:361-381, called at :501) and
#       would beat the environment, while `--openai-api-base` is applied after
#       the dotenv load (main.py:620). The user's ~/.aider.conf.yml is never
#       edited; command line beats env beats config in configargparse.
#     * the metadata file is not decoration: without an entry for the model,
#       aider fetches LiteLLM's price table from the network
#       (aider/models.py:203-218 _update_cache, get_model_from_cached_json_db
#       :223-232 short-circuits on local_model_metadata) and warns "unknown
#       context window". A local entry per lane keeps startup offline.
#     The plain `aider` command is untouched. Limitations: a key the user put
#     in .env, ~/.aider.conf.yml (`openai-api-key`) or `--openai-api-key`
#     beats the exported env var (main.py:615-616 runs after dotenv); the
#     wrapper cannot put the key on argv without exposing it. Lane context
#     size is a flat 128000 estimate (ferry's catalogue does not carry one).
#
# Wire: ~/.local/bin/aider-ferry, ~/.config/ferry/aider/model-settings.yml,
# ~/.config/ferry/aider/model-metadata.json, ~/.config/ferry/aider.json.
# `ferry aider --unwire` removes exactly those (generic, record-driven).

_aider_help() {
  cat <<'EOF'
ferry aider - point Aider (aider-chat) at the ferry endpoint.

Usage:
  ferry aider [--host H] [--port P] [--key K] [--model LANE] [--keep N]
  ferry aider --unwire [--dry-run]

  Writes the `aider-ferry` wrapper (~/.local/bin/aider-ferry) plus two
  ferry-owned files, ~/.config/ferry/aider/model-settings.yml and
  model-metadata.json, which the wrapper passes by flag. Your own
  ~/.aider.conf.yml, ~/.aider.model.settings.yml and repo files are not
  touched, and plain `aider` keeps using whatever it used before.

  aider-ferry runs `aider --openai-api-base <ferry>/v1 --model openai/<lane>`
  with OPENAI_API_KEY read from the 0600 key file at run time (never argv).
  X-Ferry-Client / X-Ferry-Fleet go out as extra_headers on every request.
  Pass your own --model to pick another lane (use the openai/<lane> form).

  Limitation: an openai key set in .env or ~/.aider.conf.yml beats the
  exported key; remove it there, or use plain `aider` for that account.
EOF
}

_aider_wire() {
  _agent_python <<'PYEOF'
import os, json
cfg = os.path.join(HOME, ".config", "ferry", "aider")
settings = os.path.join(cfg, "model-settings.yml")
meta = os.path.join(cfg, "model-metadata.json")

# YAML list for --model-settings-file. json.dumps strings are valid YAML
# double-quoted scalars, so a header value can hold any character.
q = json.dumps
y = [f"# {MARKER}", "# Written by `ferry aider`; `ferry aider --unwire` removes it.",
     "- name: aider/extra_params", "  extra_params:", "    extra_headers:"]
for k, v in HEADERS.items():
    y.append(f"      {q(k)}: {q(v)}")
for lane in LANES:
    y += [f"- name: {q('openai/' + lane)}", "  edit_format: diff"]
write_owned(settings, "\n".join(y) + "\n", 0o644)

# JSON5 for --model-metadata-file (json5.loads accepts the // marker line).
m = {}
for lane in LANES:
    m["openai/" + lane] = {
        "max_tokens": 8192, "max_input_tokens": 128000, "max_output_tokens": 8192,
        "input_cost_per_token": 0, "output_cost_per_token": 0,
        "litellm_provider": "openai", "mode": "chat"}
write_owned(meta, f"// {MARKER}\n" + json.dumps(m, indent=2) + "\n", 0o644)

wrapper(os.path.join(HOME, ".local", "bin", "aider-ferry"),
        {"OPENAI_API_KEY": '"$FERRY_KEY"'}, "aider",
        ["--openai-api-base", BASE, "--model", "openai/" + LANE,
         "--model-settings-file", settings, "--model-metadata-file", meta])
print(f"    Wrapper:        {os.path.join(HOME, '.local', 'bin', 'aider-ferry')}  (lane {LANE})")
save_record()
PYEOF
}

cmd_aider() { _agent_main aider "$@"; }
