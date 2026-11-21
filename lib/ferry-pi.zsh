# ferry pi - point Pi (earendil-works/pi, binary `pi`) at the ferry endpoint.
#
# Research (source read from a fresh clone, earendil-works/pi @ 780bf31 which
# is coding-agent 1.6.0; the INSTALLED pi is 0.85.1 and its bundled
# docs/models.md says the same for every fact below, so the file format is
# stable across both). Paths are packages/coding-agent/... unless noted.
#
# (1) Where custom providers live. <agent dir>/models.json, agent dir =
#     $PI_CODING_AGENT_DIR else ~/.pi/agent (src/config.ts:610 ENV_AGENT_DIR,
#     src/config.ts:630-636 getAgentDir, src/config.ts:644-645 getModelsPath;
#     loaded by src/core/model-runtime.ts:220-222). The file is
#     {"providers": {"<id>": {...}}} (src/core/model-config.ts:131 schema).
#     Custom OpenAI base URL: provider `baseUrl` with `api: "openai-completions"`
#     (src/core/model-config.ts:90-93 baseUrl, :101-107 api); the OpenAI SDK is
#     built with baseURL = model.baseUrl (../ai/src/api/openai-completions.ts:
#     778-784) so the .../v1 root is the right value and the SDK appends
#     /chat/completions. Anthropic base URL: the same provider entry with
#     `api: "anthropic-messages"`; ferry serves /v1/messages too, but chat/
#     completions is the preferred dialect, so that is what is wired.
# (2) Key. Provider `apiKey` is a literal, "$NAME" / "${NAME}" env
#     interpolation, or "!command" whose trimmed stdout is the key
#     (src/core/model-config.ts:95-100; src/core/resolve-config-value.ts, the
#     `!` branch of parseConfigValueReference, executeCommandUncached
#     "stdout.trim()", and the "$$" / "$!" escapes in parseConfigValueTemplate).
#     ferry uses "!cat '<~/.config/ferry/agents.key>'": the key never sits in
#     models.json, and it works for a plain `pi --provider ferry` as well as
#     through the pi-ferry wrapper. (A "$FERRY_KEY" form would send an unset
#     variable as no key at all in a plain pi.)
# (3) Custom headers. Provider `headers` map, values resolve like apiKey
#     (src/core/model-config.ts:37-39 HeadersSchema, :108 headers;
#     src/core/resolve-config-value.ts:271-282 resolveHeadersOrThrow). ferry
#     sends X-Ferry-Client (and X-Ferry-Fleet when set), with "$" escaped as
#     "$$" and a leading "!" as "$!" so a host name can never be executed.
# (4) API spoken. openai-completions (POST {baseUrl}/chat/completions). The
#     provider also pins the plain dialect through `compat`
#     (supportsStore false, supportsDeveloperRole false, maxTokensField
#     max_tokens; src/core/pi-ai-schemas.ts ProviderCompatSchema, documented in
#     docs/models.md "OpenAI Compatibility") because a LiteLLM-fronted local
#     lane may reject the generic OpenAI defaults. `authHeader: true` adds
#     Authorization: Bearer <key> (src/core/provider-composer.ts:379-391).
# (5) Least invasive wiring. Pi has NO flag or env var that points at a
#     different models.json alone: PI_CODING_AGENT_DIR relocates the whole
#     agent dir (auth.json, settings.json, sessions, skills), which would
#     orphan the user's own login and settings; there is no extension or
#     --models-file flag either (src/cli/args.ts only has --provider/--model/
#     --api-key, :115-119). So design rule 1b applies: ONE named `ferry`
#     provider is added next to the user's own providers, and nothing that
#     selects the default provider is touched (settings.json is never read).
#     Plain `pi` keeps its provider; `pi-ferry` is `pi --provider ferry
#     --model <lane>` and a `/model` pick of ferry/<lane> works in plain pi.
#
# The file is JSONC for pi, but only `//` line comments and trailing commas
# (src/utils/json.ts:2-5 stripJsonComments; a /* */ block comment is NOT
# tolerated by pi, so it is not tolerated here either). The edit is
# TEXT-surgical: the ferry member is inserted as the FIRST member of
# "providers" (with its own trailing comma), so not one byte of the user's file
# moves, and the literal inserted text is recorded so `ferry pi --unwire`
# removes exactly that text and gives the seeded file back byte for byte. A file
# that cannot be parsed or edited safely, or a user-owned `ferry` provider
# ferry did not write, is left untouched (snippet printed, exit 1).
#
# Files: ~/.local/bin/pi-ferry (wrapper), the edit of <agent dir>/models.json
# (created when absent), ~/.config/ferry/pi.json (record) and
# ~/.config/ferry/agents.key (0600, shared).

cmd_pi() { _agent_main pi "$@"; }

_pi_help() {
  cat <<'EOF'
ferry pi - point Pi (earendil-works/pi) at the ferry endpoint.

Usage:
  ferry pi [--agent-dir DIR] [common flags]

  Adds ONE 'ferry' provider (openai-completions, every served lane listed)
  to Pi's models.json and writes the wrapper ~/.local/bin/pi-ferry, which
  runs `pi --provider ferry --model <lane>`. The key is never written to
  models.json: the provider reads ~/.config/ferry/agents.key (0600) through
  an apiKey "!cat ..." command. Your other providers, comments and
  formatting stay byte for byte; the default provider of plain `pi` is not
  changed. X-Ferry-Client / X-Ferry-Fleet are sent as provider headers.
  --agent-dir DIR  Pi agent dir (default $PI_CODING_AGENT_DIR, else ~/.pi/agent).

Then:  pi-ferry            (or: pi --provider ferry --model heavy)
EOF
}

_pi_wire() {
  local pi_dir="" a
  local -a extra=("${AG_EXTRA[@]}")
  while (( ${#extra[@]} )); do
    a="${extra[1]}"
    case "$a" in
      --agent-dir) pi_dir="${extra[2]:-}"; extra=("${extra[@]:2}") ;;
      *) echo "Unknown option for 'ferry pi': $a"; return 1 ;;
    esac
  done
  pi_dir="${pi_dir:-${PI_CODING_AGENT_DIR:-$HOME/.pi/agent}}"
  _agent_python "$pi_dir" <<'PYEOF'
import shlex, sys

agent_dir = os.path.abspath(os.path.expanduser(sys.argv[1]))
models_path = os.path.join(agent_dir, "models.json")


def cfg_escape(v):
    """A literal header value for pi's value resolver: "$" -> "$$", a leading
    "!" -> "$!" (so a host name can never be run as a command)."""
    v = v.replace("$", "$$")
    return "$!" + v[1:] if v.startswith("!") else v


lanes = [LANE] + [l for l in LANES if l != LANE]
provider = {
    "name": "ferry",
    "baseUrl": BASE,
    "apiKey": "!cat " + shlex.quote(KEYFILE),
    "api": "openai-completions",
    "authHeader": True,
    "headers": {k: cfg_escape(v) for k, v in HEADERS.items()},
    "compat": {
        "supportsStore": False,
        "supportsDeveloperRole": False,
        "maxTokensField": "max_tokens",
    },
    "models": [{"id": l, "name": f"ferry {l}"} for l in lanes],
}


# ---------------------------------------------------------------- JSONC ----
class Bad(Exception):
    pass


WS = " \t\r\n﻿"


def skip_ws(t, i):
    n = len(t)
    while i < n:
        if t[i] in WS:
            i += 1
        elif t.startswith("//", i):
            j = t.find("\n", i)
            i = n if j < 0 else j + 1
        elif t.startswith("/*", i):
            raise Bad("a /* */ comment (pi does not accept those either)")
        else:
            break
    return i


def end_string(t, i):
    j = i + 1
    while j < len(t):
        if t[j] == "\\":
            j += 2
        elif t[j] == '"':
            return j + 1
        else:
            j += 1
    raise Bad("unterminated string")


SCALAR = re.compile(r"-?\d[\d.eE+\-]*|true|false|null")


def parse_value(t, i):
    i = skip_ws(t, i)
    if i >= len(t):
        raise Bad("unexpected end of file")
    c = t[i]
    if c == "{":
        return parse_object(t, i)[0]
    if c == "[":
        i += 1
        while True:
            i = skip_ws(t, i)
            if i >= len(t):
                raise Bad("unterminated array")
            if t[i] == "]":
                return i + 1
            i = skip_ws(t, parse_value(t, i))
            if i < len(t) and t[i] == ",":
                i += 1
            elif i < len(t) and t[i] != "]":
                raise Bad("expected , or ] in array")
    if c == '"':
        return end_string(t, i)
    m = SCALAR.match(t, i)
    if not m:
        raise Bad(f"unexpected character {c!r}")
    return m.end()


def parse_object(t, i):
    """(index past the closing brace, [(key, value_start, value_end)])."""
    members = []
    i += 1
    while True:
        i = skip_ws(t, i)
        if i >= len(t):
            raise Bad("unterminated object")
        if t[i] == "}":
            return i + 1, members
        if t[i] != '"':
            raise Bad("expected a string key")
        ke = end_string(t, i)
        k = json.loads(t[i:ke])
        i = skip_ws(t, ke)
        if i >= len(t) or t[i] != ":":
            raise Bad("expected ':' after a key")
        vs = skip_ws(t, i + 1)
        ve = parse_value(t, vs)
        members.append((k, vs, ve))
        i = skip_ws(t, ve)
        if i < len(t) and t[i] == ",":
            i += 1
        elif i < len(t) and t[i] != "}":
            raise Bad("expected , or } in object")


def strip_jsonc(t):
    """Comments and trailing commas removed: a strict-JSON twin of t."""
    out, i, n = [], 0, len(t)
    while i < n:
        c = t[i]
        if c == '"':
            j = end_string(t, i)
            out.append(t[i:j])
            i = j
        elif c == "/" or c in WS:
            j = skip_ws(t, i)
            if j > i:
                out.append(" ")
                i = j
            else:
                out.append(c)
                i += 1
        elif c == ",":
            j = skip_ws(t, i + 1)
            if not (j < n and t[j] in "}]"):
                out.append(c)
            i += 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


def loads(t):
    try:
        return json.loads(strip_jsonc(t))
    except (ValueError, IndexError) as e:
        raise Bad(str(e))


def block(indent, key, val):
    pad = " " * indent
    body = json.dumps(val, indent=2).replace("\n", "\n" + pad)
    return f"{pad}{json.dumps(key)}: {body}"


def first_member_text(t, obj_start, indent, key, val):
    """The exact text to insert right after the `{` of the object at obj_start
    so that `key` becomes its first member. Existing bytes never move."""
    _, members = parse_object(t, obj_start)
    member = block(indent, key, val)
    if members:
        return "\n" + member + ","
    return "\n" + member + "\n" + " " * (indent - 2)


# ----------------------------------------------------------------- state ----
def prior_edit():
    try:
        with open(RECORD) as f:
            for e in json.load(f).get("edited", []):
                if e.get("path") == os.path.abspath(models_path) \
                        and e.get("kind") == "pi-models-jsonc":
                    return e
    except Exception:
        pass
    return None


def fail(msg, snippet_indent=4):
    print(f"    ERROR: {models_path} was NOT modified: {msg}.", file=sys.stderr)
    print('    Add this provider under "providers" by hand:', file=sys.stderr)
    print(block(snippet_indent, "ferry", provider), file=sys.stderr)
    sys.exit(1)


existing = None
if os.path.exists(models_path):
    with open(models_path, encoding="utf-8") as f:
        existing = f.read()

prior = prior_edit()
created = bool(prior and prior.get("created")) or existing is None
blank = prior.get("blank") if prior else None
if existing is not None and not existing.strip() and not (prior and prior.get("inserted")):
    blank = existing                      # a user's empty file: restore it on unwire
if existing is None or (not existing.strip() and blank is not None):
    base = "{}\n"
else:
    base = existing

try:
    old = loads(base)
    if not isinstance(old, dict):
        raise Bad("top level is not a JSON object")
    start = skip_ws(base, 0)
    if base[start] != "{":
        raise Bad("top level is not a JSON object")
    old_inserted = prior.get("inserted") if prior else None
    if old_inserted and old_inserted in base:
        # Re-wire: refresh our own text in place, same shape, same spot.
        tail = "," if old_inserted.endswith(",") else old_inserted[old_inserted.rfind("\n"):]
        top = bool(prior.get("top"))
        member = (block(2, "providers", {"ferry": provider}) if top
                  else block(4, "ferry", provider))
        new_inserted = "\n" + member + tail
        new_text = base.replace(old_inserted, new_inserted, 1)
    else:
        prov = next((m for m in parse_object(base, start)[1] if m[0] == "providers"), None)
        if prov is None:
            new_inserted = first_member_text(base, start, 2, "providers", {"ferry": provider})
            pos = start + 1
            top = True
        else:
            if not isinstance(old.get("providers"), dict):
                raise Bad('"providers" is not an object')
            if "ferry" in old["providers"]:
                raise Bad('a "ferry" provider that ferry did not write is already there')
            new_inserted = first_member_text(base, prov[1], 4, "ferry", provider)
            pos = prov[1] + 1
            top = False
        new_text = base[:pos] + new_inserted + base[pos:]
    want = json.loads(json.dumps(old))
    want.setdefault("providers", {})["ferry"] = provider
    if loads(new_text) != want:
        raise Bad("the edit did not round-trip")
except (Bad, IndexError) as e:
    fail(str(e))

os.makedirs(agent_dir, exist_ok=True)
changed = new_text != existing
if existing is not None and changed and existing.strip():
    snapshot(models_path)
if changed:
    fd = os.open(models_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(new_text)

wrapper(os.path.join(HOME, ".local", "bin", "pi-ferry"), {}, "pi",
        ["--provider", "ferry", "--model", LANE])
note_edited(models_path, "pi-models-jsonc", inserted=new_inserted,
            created=created, blank=blank, top=top)
save_record({"agent_dir": agent_dir, "provider": "ferry", "lanes": lanes})
print(f"    Wired pi        -> {BASE} (openai-completions; lanes: {', '.join(lanes)})")
print(f"    File edited:    {models_path}" + ("" if changed else " (unchanged)"))
print(f"    Run:            pi-ferry   (pi --provider ferry --model {LANE})")
PYEOF
}

# Remove ONLY ferry's inserted text from models.json. Called by the generic
# unwire with the edit record (json) as $1; it runs under --dry-run too.
_pi_unwire() {
  python3 - "$1" "${AG_DRY:-0}" <<'PYEOF'
import json, os, sys
e = json.loads(sys.argv[1]); dry = sys.argv[2] == "1"
path, ins = e["path"], e.get("inserted") or ""
with open(path, encoding="utf-8") as f:
    text = f.read()
if not ins or ins not in text:
    print(f"    Kept {path}: ferry's provider text is no longer there verbatim;"
          " remove the \"ferry\" provider by hand.", file=sys.stderr)
    sys.exit(0)
new = text.replace(ins, "", 1)
gone = new.strip() == "{}" and (e.get("created") or e.get("blank") is not None)
if dry:
    print(f"    [dry-run] would remove the ferry provider from {path}"
          + (" (and the file)" if gone and e.get("created") else ""), file=sys.stderr)
    sys.exit(0)
if gone and e.get("created"):
    os.remove(path)
    print(f"    Removed {path} (ferry created it)", file=sys.stderr)
else:
    out = e["blank"] if gone else new
    mode = os.stat(path).st_mode & 0o777
    with open(path + ".ferry-tmp", "w", encoding="utf-8") as f:
        f.write(out)
    os.chmod(path + ".ferry-tmp", mode)
    os.replace(path + ".ferry-tmp", path)
    print(f"    Removed the ferry provider from {path}", file=sys.stderr)
PYEOF
}
