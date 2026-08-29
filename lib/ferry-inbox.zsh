
# cmd_inbox — read the telemetry clients POSTed to this host.
#
# `ferry msg` and `ferry log` POST a raw body to the share server's /hq endpoint,
# which appends it to $HQ_LOG. Nothing on the host read it back until this command:
# the answer lives in TWO files and neither one holds all of it.
#
#   $HQ_LOG                       every entry, verbatim, append-only. NO timestamp
#                                 and NO client IP — the handler writes a delimiter
#                                 and the body, nothing else.
#   $LOG_DIR/share-<port>.log     the share server's access log: timestamp, client
#                                 IP and status per POST — but TRUNCATED on every
#                                 `ferry share` relaunch.
#
# So a date is recovered by aligning the two from the END: the k receipts still in
# the access log belong to the k most recent entries. Older entries are real and
# undated, and this prints them as such rather than guessing.
#
# Only status-200 receipts are aligned. A 500 means the handler raised and NO entry
# was written, so counting it would shift every date by one.
cmd_inbox() {
  if (( CLIENT_MODE )); then
    echo "Error: Command 'ferry inbox' is only available on the LLM-Ferry Host Mac."
    echo "       A client SENDS telemetry ('ferry msg' / 'ferry log'); the host is"
    echo "       where it lands, so the inbox only exists there."
    exit 1
  fi

  local last=0 follow=0 show_all=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      -n|--last)   last="$2"; shift 2 ;;
      -f|--follow) follow=1; shift ;;
      -a|--all)    show_all=1; shift ;;
      --path)
        echo "$HQ_LOG"
        echo "$LOG_DIR/share-$SHARE_PORT.log"
        return 0 ;;
      -h|--help)
        echo "Usage: ferry inbox [-n N] [-f] [--all] [--path]"
        echo "  (no flags)   index the 20 most recent entries, dated where possible"
        echo "  -n N         print the last N entries IN FULL"
        echo "  -f           follow new entries as they land (tail -f)"
        echo "  --all        index every entry, not just the last 20"
        echo "  --path       print the content and receipt log paths"
        return 0 ;;
      *) echo "Unknown option for 'ferry inbox': $1"; exit 1 ;;
    esac
  done

  if (( follow )); then
    if [[ ! -f "$HQ_LOG" ]]; then
      echo ">>> No telemetry yet ($HQ_LOG does not exist). Waiting for the first entry..."
      mkdir -p "$(dirname "$HQ_LOG")"
      : >> "$HQ_LOG"
    fi
    echo ">>> Following $HQ_LOG (Ctrl-C to stop)"
    tail -f "$HQ_LOG"
    return 0
  fi

  python3 - "$HQ_LOG" "$LOG_DIR" "$last" "$show_all" "$APP_DIR/client_logs.txt" <<'PYEOF'
import glob, os, re, sys, time

hq_log, log_dir, last, show_all, legacy = sys.argv[1:6]
last, show_all = int(last), show_all == "1"
DELIM = "=== CLIENT LOG ENTRY ==="

if not os.path.exists(hq_log):
    print(f">>> No client telemetry yet: {hq_log} does not exist.")
    print("    Clients POST to the share server, so nothing lands while `ferry share` is down.")
    sys.exit(0)

with open(hq_log, encoding="utf-8", errors="replace") as f:
    raw = f.read()
# The first chunk is whatever preceded the first delimiter — normally empty.
entries = [e.strip("\n") for e in raw.split(DELIM)[1:]]

# --- Receipts: timestamp + client, newest last, across every share-<port>.log ---
# The port can differ from $SHARE_PORT: `ferry share` scans upward when the port is
# taken, so the log for a live server is not always the one named by the default.
LINE = re.compile(r'^(\S+) - - \[([^\]]+)\] "POST [^"]*/hq[^"]*" (\d{3})')
receipts, failed = [], 0
for path in glob.glob(os.path.join(log_dir, "share-*.log")):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                m = LINE.match(line)
                if not m:
                    continue
                ip, stamp, code = m.groups()
                # BaseHTTPRequestHandler stamps "28/Aug/2026 17:50:40" — a SPACE, not the
                # Apache colon. Parsing the Apache form first silently dated NOTHING (every
                # line raised ValueError and was skipped), and the listing still rendered
                # fine, just with an empty date column. Accept both, in that order.
                for fmt in ("%d/%b/%Y %H:%M:%S", "%d/%b/%Y:%H:%M:%S"):
                    try:
                        t = time.strptime(stamp, fmt)
                        break
                    except ValueError:
                        t = None
                if t is None:
                    continue
                if code == "200":
                    receipts.append((t, ip))
                else:
                    failed += 1
    except OSError:
        continue
receipts.sort(key=lambda r: r[0])

# Align from the END: receipt[-1] is entry[-1]. Anything older than the current
# access log is undated, which is a fact about the log, not a failure.
dated = {}
for i, r in enumerate(receipts[-len(entries):] if entries else []):
    dated[len(entries) - min(len(receipts), len(entries)) + i] = r

mtime = time.strftime("%d %b %H:%M", time.localtime(os.path.getmtime(hq_log)))
print(f">>> {hq_log}")
print(f"    {len(entries)} entr{'y' if len(entries) == 1 else 'ies'}, last arrival {mtime}"
      f"  ({len(dated)} dated by the current share log)")
if failed:
    print(f"    WARNING: {failed} POST(s) to /hq returned an error — those bodies were never written.")
if os.path.exists(legacy):
    print(f"    NOTE: a legacy {os.path.basename(legacy)} also exists beside the checkout")
    print(f"          ({legacy}). It is frozen pre-1.8.10 history, not live telemetry.")
print("")

def first_line(text):
    for line in text.split("\n"):
        if line.strip():
            return line.strip()
    return "(empty body)"

if last > 0:
    for i in range(max(0, len(entries) - last), len(entries)):
        t, ip = dated.get(i, (None, None))
        when = time.strftime("%d/%b %H:%M", t) if t else "undated"
        print(f"===== entry {i + 1}  [{when}  {ip or 'unknown client'}] =====")
        print(entries[i])
        print("")
else:
    shown = entries if show_all else entries[-20:]
    offset = len(entries) - len(shown)
    for i, e in enumerate(shown):
        t, ip = dated.get(offset + i, (None, None))
        when = time.strftime("%d/%b %H:%M", t) if t else "     —     "
        print(f"{offset + i + 1:3d}  {when}  {(ip or ''):15.15s}  {first_line(e):.60s}")
    if offset:
        print(f"\n    ({offset} older entr{'y' if offset == 1 else 'ies'} not shown — pass --all)")
    if entries:
        print(f"\n    Full text of the newest:  ferry inbox -n 1")
PYEOF
}
