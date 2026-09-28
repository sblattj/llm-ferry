#!/usr/bin/env python3
"""`ferry keys` — mint, list, revoke and limit per-device client keys.

Invoked by lib/ferry-keys.zsh on the HOST as
`python3 $APP_DIR/front/ferry_keys_cli.py <verb> ...`. A new key is printed
ONCE, alone on stdout; only its sha256 is stored. Store paths come from
FERRY_KEYS_FILE / FERRY_KEYS_DB (defaults under ~/.config/ferry/).
Exit: 0 ok, 1 store/name error ("ferry keys: ..." on stderr), 2 usage error.
"""
import argparse
import sys

import ferry_keys as K
import ferry_keys_usage as U


def _positive(text):
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError("expected a positive integer, got %r" % text)
    if value < 1:
        raise argparse.ArgumentTypeError("expected a positive integer, got %r" % text)
    return value


def _limit(text):
    return None if text == "none" else _positive(text)


def _lanes(text):
    if text == "none":
        return None
    lanes = [part.strip() for part in text.split(",") if part.strip()]
    if not lanes:
        raise argparse.ArgumentTypeError("expected lane names like flash,heavy or 'none'")
    return lanes


def _expires(text):
    if text == "none":
        return None
    try:
        return K.expires_from_date(text)
    except ValueError as err:
        raise argparse.ArgumentTypeError(str(err))


def _parser():
    parser = argparse.ArgumentParser(prog="ferry keys",
                                     description="Per-device client keys for this host.")
    verbs = parser.add_subparsers(dest="verb", required=True)

    add = verbs.add_parser("add", help="mint a key (printed once, alone on stdout)")
    add.add_argument("name")
    add.add_argument("--expires", type=_expires, default=None, metavar="YYYY-MM-DD")
    add.add_argument("--lanes", type=_lanes, default=None, metavar="LANE[,LANE]")
    add.add_argument("--rpm", type=_positive, default=None, metavar="N")
    add.add_argument("--budget-tokens", type=_positive, default=None, metavar="N")
    add.add_argument("--replace", action="store_true",
                     help="rotate an existing key of this name in place "
                          "(a revoked name is reactivated)")

    verbs.add_parser("list", help="every key with its status, limits and usage")

    revoke = verbs.add_parser("revoke", help="refuse a key from the next request on")
    revoke.add_argument("name")

    setp = verbs.add_parser("set", help="change a key's limits ('none' clears one)")
    setp.add_argument("name")
    setp.add_argument("--expires", type=_expires, default=argparse.SUPPRESS)
    setp.add_argument("--lanes", type=_lanes, default=argparse.SUPPRESS)
    setp.add_argument("--rpm", type=_limit, default=argparse.SUPPRESS)
    setp.add_argument("--budget-tokens", type=_limit, default=argparse.SUPPRESS)
    return parser


def _cell(value):
    return "-" if value is None else str(value)


def _list():
    doc = K.load()
    if not doc["keys"]:
        print("No device keys yet. Create one with: ferry keys add <name>")
        return 0
    try:
        usage = U.Usage().summary()
    except U.UsageError as err:
        print("ferry keys: usage unavailable: %s" % err, file=sys.stderr)
        usage = None
    rows = [("NAME", "STATUS", "CREATED", "EXPIRES", "LANES", "RPM", "BUDGET",
             "TOKENS(MONTH)", "REQ(LAST MIN)")]
    for entry in doc["keys"]:
        used = (usage or {}).get(entry["name"], {"month_tokens": 0, "minute_requests": 0})
        rows.append((
            entry["name"], K.status(entry), (entry.get("created") or "-")[:10],
            (entry.get("expires") or "-")[:10],
            ",".join(entry["lanes"]) if entry.get("lanes") else "all",
            _cell(entry.get("rpm")), _cell(entry.get("budget_tokens")),
            "?" if usage is None else str(used["month_tokens"]),
            "?" if usage is None else str(used["minute_requests"]),
        ))
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    for row in rows:
        print("  ".join(cell.ljust(width) for cell, width in zip(row, widths)).rstrip())
    return 0


def main(argv=None):
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.verb == "add":
            # Only for the wording: --replace on an existing name rotates it
            # (and reactivates it if it was revoked); otherwise it creates.
            rotated = (args.replace
                       and K.find(K.load(), K.normalize_name(args.name)) is not None)
            name, token = K.add(args.name, expires=args.expires, lanes=args.lanes,
                                rpm=args.rpm, budget_tokens=args.budget_tokens,
                                replace=args.replace)
            print(token)
            print("ferry keys: %s %r. The key above is shown ONCE; put it in "
                  "the device's client.json as api_key (or re-run client-bootstrap.sh "
                  "there with the master key to enroll automatically)."
                  % ("rotated" if rotated else "created", name), file=sys.stderr)
            return 0
        if args.verb == "list":
            return _list()
        if args.verb == "revoke":
            entry = K.revoke(args.name)
            print("ferry keys: revoked %r; it is refused from the next request on."
                  % entry["name"], file=sys.stderr)
            return 0
        changes = {field: getattr(args, field)
                   for field in ("expires", "lanes", "rpm", "budget_tokens")
                   if hasattr(args, field)}
        if not changes:
            parser.error("set needs at least one of --expires --lanes --rpm --budget-tokens")
        entry = K.update(args.name, **changes)
        print("ferry keys: updated %r." % entry["name"], file=sys.stderr)
        return 0
    except (K.KeyNameError, K.KeyStoreError, OSError) as err:
        print("ferry keys: %s" % err, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
