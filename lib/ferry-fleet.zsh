# ferry fleet — read or switch which FLEET (routing set) a caller resolves
# bare lane names against. Talks to the front door's control plane at
# /v1/ferry/fleet (front/ferry_front.py): GET returns the fleet document,
# POST mutates the caller's own sticky selection, or (host only, --default)
# the host-wide default. Identity and auth follow the same rules as every
# other ferry client command: CLIENT_MODE=1 talks to
# $CLIENT_HOST:$CLIENT_PORT with $CLIENT_MASTER_KEY; CLIENT_MODE=0 (the host)
# talks to its own loopback front door with $LITELLM_MASTER_KEY.

cmd_fleet() {
  local verb="${1:-}"
  [[ $# -gt 0 ]] && shift
  local fleet="" flag=""

  case "$verb" in
    --help|-h|"")
      cat <<'EOF'
ferry fleet — read or switch which routing fleet bare lane names resolve to.

Usage:
  ferry fleet ls                    List every fleet with its primaries; '*'
                                     marks the default, 'you' marks your own
                                     resolved fleet.
  ferry fleet show                  Show who you are, your resolved fleet, the
                                     host-wide default, and every client's pick.
  ferry fleet use <fleet>           Select a fleet for yourself (sticky).
  ferry fleet use <fleet> --default [Host only] Set the host-wide default fleet.
  ferry fleet use --clear           Clear your own selection (follow the default).
  ferry fleet --help                This message.
EOF
      [[ "$verb" == "" ]] && exit 1
      return 0
      ;;
    ls|show) ;;
    use)
      fleet="${1:-}"
      if [[ "$fleet" == "--clear" ]]; then
        fleet=""
        flag="clear"
        shift
      else
        [[ $# -gt 0 ]] && shift
        if [[ "${1:-}" == "--default" ]]; then
          flag="default"
          shift
        fi
      fi
      if [[ "$flag" != "clear" && -z "$fleet" ]]; then
        echo "Usage: ferry fleet use <fleet> [--default] | ferry fleet use --clear" >&2
        exit 1
      fi
      if [[ $# -gt 0 ]]; then
        echo "Unknown option for 'ferry fleet use': $1" >&2
        exit 1
      fi
      ;;
    *)
      echo "Unknown 'ferry fleet' subcommand: $verb" >&2
      echo "Usage: ferry fleet ls | show | use <fleet> [--default] | use --clear" >&2
      exit 1
      ;;
  esac

  # --default on a client is refused before any HTTP call: a client has no
  # authority to move the host-wide default (the front door would 403 it
  # anyway, loopback-only), so this is a courtesy short-circuit, not the
  # security boundary.
  if [[ "$flag" == "default" && "$CLIENT_MODE" == "1" ]]; then
    echo "the default is the host's to set" >&2
    exit 1
  fi

  local base key route_config=""
  if [[ "$CLIENT_MODE" == "1" ]]; then
    base="http://$CLIENT_HOST:$CLIENT_PORT"
    key="$CLIENT_MASTER_KEY"
  else
    base="http://127.0.0.1:$PORT"
    key="${LITELLM_MASTER_KEY:-}"
    route_config="$DEFAULT_ROUTE_CONFIG"
  fi

  python3 - "$base" "$key" "$CLIENT_NAME" "$verb" "$fleet" "$flag" "$route_config" <<'PYEOF'
import json
import os
import re
import sys
import urllib.error
import urllib.request

base, key, name, verb, fleet, flag, route_config = sys.argv[1:8]

FLEET_PATH = "/v1/ferry/fleet"


def _headers():
    h = {"X-Ferry-Client": name}
    if key:
        h["Authorization"] = "Bearer " + key
    return h


def _fail(msg):
    print("ferry fleet: " + msg, file=sys.stderr)
    sys.exit(1)


def _request(method, payload=None):
    data = None
    headers = _headers()
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base + FLEET_PATH, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            err = json.loads(body)
            # the fleet route replies {"error": {"message": ...}}; the front
            # door's body reader replies {"errors": [...]} -- accept both.
            if isinstance(err.get("error"), dict):
                msg = err["error"].get("message") or body
            elif isinstance(err.get("errors"), list):
                msg = "; ".join(str(x) for x in err["errors"]) or body
            else:
                msg = body
        except Exception:
            msg = body
        _fail(msg)


def _get():
    return _request("GET")


def _keys_column(fleets):
    if not route_config:
        return None
    try:
        with open(route_config) as f:
            text = f.read()
    except OSError:
        return {fname: "ok" for fname in fleets}
    out = {}
    for fname in fleets:
        pat = re.compile(
            r"model_name:\s*" + re.escape(fname) + r"\.[^\n]*\n(.*?)(?=\n\s*-\s*model_name:|\Z)",
            re.S,
        )
        missing = []
        for m in pat.finditer(text):
            for env_name in re.findall(r"os\.environ/([A-Za-z0-9_]+)", m.group(1)):
                if not os.environ.get(env_name) and env_name not in missing:
                    missing.append(env_name)
        out[fname] = "ok" if not missing else "missing: " + ", ".join(missing)
    return out


def _fmt(v):
    return v if v else "-"


def cmd_ls():
    doc = _get()
    fleets = doc["fleets"]
    names = list(fleets.keys())
    keys = _keys_column(fleets)
    header = ["FLEET", "HEAVY", "FLASH", "SUPER-FLASH"]
    if keys is not None:
        header.append("KEYS")
    rows = []
    for fname in names:
        lanes = fleets[fname]
        marks = []
        if fname == doc.get("default"):
            marks.append("*")
        if fname == doc.get("fleet"):
            marks.append("you")
        label = fname + ("  " + " ".join(marks) if marks else "")
        row = [label, _fmt(lanes.get("heavy")), _fmt(lanes.get("flash")), _fmt(lanes.get("super-flash"))]
        if keys is not None:
            row.append(keys.get(fname, "ok"))
        rows.append(row)
    widths = [len(h) for h in header]
    for row in rows:
        for i, c in enumerate(row):
            widths[i] = max(widths[i], len(c))

    def line(cols):
        return "  ".join(c.ljust(widths[i]) for i, c in enumerate(cols))

    print(line(header))
    for row in rows:
        print(line(row))


def cmd_show():
    doc = _get()
    print("you: " + str(doc.get("you", "")))
    print("fleet: " + str(doc.get("fleet", "")))
    print("default: " + str(doc.get("default", "")))
    print("clients:")
    for identity, fname in doc.get("clients", {}).items():
        print("  " + identity + " -> " + str(fname))


def cmd_use():
    doc = _get()
    fleets = doc["fleets"]
    if flag != "clear" and fleet not in fleets:
        names = ", ".join(fleets.keys())
        _fail("unknown fleet '" + fleet + "'; fleets: " + names)
    if flag == "clear":
        payload = {"fleet": None}
    elif flag == "default":
        payload = {"fleet": fleet, "default": True}
    else:
        payload = {"fleet": fleet}
    resp = _request("POST", payload)
    print("fleet: " + str(resp.get("fleet")))


if verb == "ls":
    cmd_ls()
elif verb == "show":
    cmd_show()
elif verb == "use":
    cmd_use()
else:
    _fail("unknown verb '" + verb + "'")
PYEOF
}
