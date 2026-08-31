
# ----------------- ENCRYPTED OFF-LAN DROP -----------------
#
# `ferry drop` / `ferry pickup` — the only ferry transport that survives an
# UNTRUSTED channel.
#
# Every other transport in this CLI assumes the private LAN and says so
# (README "Client<->host traffic is plain HTTP on your private network").
# That posture is deliberate and unchanged. This pair covers the case the
# posture cannot: getting a file to a machine that is not on the LAN at all.
#
# The model is client-side encryption plus a DUMB carrier: `drop` writes a
# self-contained blob, you move it by whatever channel already exists (email,
# chat, a gist, object storage, a USB stick), and `pickup` decrypts it. Ferry
# supplies confidentiality, never delivery — which is why there is no account,
# no credential file, and no network code here.
#
# WHY openssl, in a CLI whose badge says "zsh + python3 stdlib": python's
# standard library has PBKDF2 (hashlib) but NO AES, in any module. The choice
# was openssl or hand-rolling a cipher. ferry already shells out to `curl` and
# `nc` for exactly this class of OS-provided binary. Verified 2026-08-30 that
# stock macOS LibreSSL 3.3.6 and Homebrew OpenSSL 3.6.3 produce mutually
# decryptable blobs, and that LibreSSL genuinely honours `-iter` rather than
# accepting and ignoring it (decrypting a 600k blob with `-iter 1` exits 1).
# That second check is the one that matters: LibreSSL historically had no
# `-pbkdf2` at all, and a silently-ignored `-iter` would have produced blobs
# that fail across machines for no visible reason.

FERRYDROP_MAGIC="FERRYDROP/1"
FERRYDROP_ITER="600000"
FERRYDROP_EXT=".ferrydrop"

# Exit codes are distinct so a script can tell "wrong passphrase" from
# "someone modified the blob" — those mean very different things.
FERRYDROP_RC_USAGE=2
FERRYDROP_RC_TAMPER=3
FERRYDROP_RC_BADPASS=4
FERRYDROP_RC_NOOPENSSL=5

_ferry_drop_need_openssl() {
  if ! command -v openssl >/dev/null 2>&1; then
    echo "Error: 'openssl' not found on PATH." >&2
    echo "  ferry drop/pickup need it for AES-256-CBC; python3's standard library has no cipher." >&2
    echo "  macOS ships one at /usr/bin/openssl; on Debian/Ubuntu: apt install openssl" >&2
    exit $FERRYDROP_RC_NOOPENSSL
  fi
}

# A fresh passphrase per drop, ~100+ bits from openssl's CSPRNG.
#
# Hyphen-grouped for legibility, and the hyphens are PART OF THE SECRET — the
# string printed is exactly the string to type back, with nothing to reassemble.
# A five-word diceware form was considered and rejected: it needs a wordlist
# embedded in a single-file CLI that clients fetch over the wire, and buys ~50
# bits where this buys twice that.
_ferry_drop_genpass() {
  openssl rand -base64 24 | tr -d '/+=\n' | cut -c1-24 | sed 's/..../&-/g; s/-$//'
}

cmd_drop() {
  local src="" msg="" out="" have_msg=0 passfile_in=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --msg)   msg="$2"; have_msg=1; shift 2 ;;
      --to)    out="$2"; shift 2 ;;
      # Supply your own passphrase instead of a generated one. For scripted
      # drops and for the test suite; a human should prefer the generated one,
      # which is drawn from openssl's CSPRNG rather than from a human's idea of
      # what looks random.
      --pass-file) passfile_in="$2"; shift 2 ;;
      -h|--help)
        echo "Usage: ferry drop <file>|-  [--to PATH] [--pass-file FILE]"
        echo "       ferry drop --msg \"text\" [--to PATH]"
        return 0 ;;
      -)       src="-"; shift ;;
      -*)      echo "Unknown option for 'ferry drop': $1" >&2; exit $FERRYDROP_RC_USAGE ;;
      *)       src="$1"; shift ;;
    esac
  done

  if (( have_msg )) && [[ -n "$src" ]]; then
    echo "Error: give either a file or --msg, not both." >&2; exit $FERRYDROP_RC_USAGE
  fi
  if (( ! have_msg )) && [[ -z "$src" ]]; then
    echo "Usage: ferry drop <file>|-  |  ferry drop --msg \"text\"   (see --help)" >&2
    exit $FERRYDROP_RC_USAGE
  fi
  if [[ -n "$src" && "$src" != "-" && ! -f "$src" ]]; then
    echo "Error: no such file: $src" >&2; exit $FERRYDROP_RC_USAGE
  fi

  _ferry_drop_need_openssl

  local work; work="$(mktemp -d "${TMPDIR:-/tmp}/ferry-drop.XXXXXX")"
  # The passphrase lives in this directory. Remove it on ANY exit path.
  trap "rm -rf '$work'" EXIT INT TERM

  # --- materialise the plaintext, and decide what to call it ---
  local kind name
  if (( have_msg )); then
    kind="msg"; name="message.txt"
    printf '%s' "$msg" > "$work/pt"
  elif [[ "$src" == "-" ]]; then
    kind="msg"; name="stdin.txt"
    cat > "$work/pt"
  else
    kind="file"; name="${src:t}"
    cp "$src" "$work/pt"
  fi

  # --- passphrase to a 0600 file, never to argv ---
  #
  # `-pass pass:<secret>` puts the secret in the process table, where any user
  # on the box can read it with ps. `file:` is the only safe form here.
  local passfile="$work/pass" generated=1
  if [[ -n "$passfile_in" ]]; then
    [[ -f "$passfile_in" ]] || { echo "Error: no such pass file: $passfile_in" >&2; exit $FERRYDROP_RC_USAGE; }
    generated=0
    ( umask 077; head -1 "$passfile_in" | tr -d '\n' > "$passfile" )
    [[ -s "$passfile" ]] || { echo "Error: pass file is empty: $passfile_in" >&2; exit $FERRYDROP_RC_USAGE; }
  else
    ( umask 077; _ferry_drop_genpass > "$passfile" )
  fi

  if ! openssl enc -aes-256-cbc -pbkdf2 -iter "$FERRYDROP_ITER" -salt -a \
        -in "$work/pt" -out "$work/ct.b64" -pass "file:$passfile" 2>"$work/err"; then
    echo "Error: encryption failed." >&2; sed 's/^/    /' "$work/err" >&2
    exit 1
  fi

  [[ -z "$out" ]] && out="${name}${FERRYDROP_EXT}"

  python3 - "$work/ct.b64" "$passfile" "$out" "$kind" "$name" "$FERRYDROP_MAGIC" "$FERRYDROP_ITER" <<'PYEOF'
import sys
sys.path.insert(0, "")
ct_path, pass_path, out_path, kind, name, magic, iters = sys.argv[1:8]

import hashlib, hmac

with open(ct_path) as f:
    ct = f.read()
with open(pass_path, "rb") as f:
    passphrase = f.read().strip()

# The MAC key is derived with a DIFFERENT salt from the one openssl used for the
# encryption key, so the two keys are independent even though one passphrase
# produced both.
mac_key = hashlib.pbkdf2_hmac("sha256", passphrase, b"ferrydrop-mac-v1", int(iters), dklen=32)

# The MAC covers the HEADER as well as the ciphertext. Authenticating the
# ciphertext alone would leave `name:` attacker-controlled, and `name` picks the
# output path on the receiving side — `name: ../../../.ssh/authorized_keys`
# would be a write-anywhere primitive on a blob the recipient can decrypt.
header = [
    magic,
    "cipher: aes-256-cbc",
    "kdf: pbkdf2",
    f"iter: {iters}",
    f"kind: {kind}",
    f"name: {name}",
]
signed = ("\n".join(header) + "\n--\n" + ct).encode()
mac = hmac.new(mac_key, signed, hashlib.sha256).hexdigest()

with open(out_path, "w") as f:
    f.write("\n".join(header) + f"\nmac: {mac}\n--\n" + ct)
PYEOF
  local rc=$?
  if (( rc != 0 )); then echo "Error: could not write the blob." >&2; exit 1; fi

  local size; size="$(wc -c < "$out" | tr -d ' ')"

  # Colour only for a human. The passphrase is a VALUE the operator has to carry
  # to another machine, so when stdout is redirected it must come out as plain
  # text a script can read — wrapping a secret in escape codes makes
  # `ferry drop f | grep passphrase` return something subtly wrong rather than
  # something that obviously failed.
  local g="" y="" z=""
  if [[ -t 1 ]]; then g=$'\033[1;32m'; y=$'\033[1;33m'; z=$'\033[0m'; fi

  echo ""
  echo "  ${g}${out}${z}  (${size} bytes, aes-256-cbc + pbkdf2@${FERRYDROP_ITER}, hmac-sha256)"
  echo ""
  if (( generated )); then
    echo "  passphrase: ${y}$(cat "$passfile")${z}"
  else
    echo "  passphrase: (the one you supplied — not echoed)"
  fi
  echo ""
  echo "  Send the BLOB and the PASSPHRASE by different channels — the blob is safe"
  echo "  on an untrusted one, the passphrase is the entire security boundary."
  echo "  Pick it up with:  ferry pickup $out"
  echo ""
}

cmd_pickup() {
  local blob="" out="" passfile_in=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --to)        out="$2"; shift 2 ;;
      --pass-file) passfile_in="$2"; shift 2 ;;
      -h|--help)
        echo "Usage: ferry pickup <blob> [--to PATH] [--pass-file FILE]"
        return 0 ;;
      -*)          echo "Unknown option for 'ferry pickup': $1" >&2; exit $FERRYDROP_RC_USAGE ;;
      *)           blob="$1"; shift ;;
    esac
  done

  if [[ -z "$blob" ]]; then
    echo "Usage: ferry pickup <blob> [--to PATH] [--pass-file FILE]" >&2
    exit $FERRYDROP_RC_USAGE
  fi
  if [[ ! -f "$blob" ]]; then
    echo "Error: no such blob: $blob" >&2; exit $FERRYDROP_RC_USAGE
  fi

  _ferry_drop_need_openssl

  local work; work="$(mktemp -d "${TMPDIR:-/tmp}/ferry-pickup.XXXXXX")"
  trap "rm -rf '$work'" EXIT INT TERM

  local passfile="$work/pass"
  if [[ -n "$passfile_in" ]]; then
    [[ -f "$passfile_in" ]] || { echo "Error: no such pass file: $passfile_in" >&2; exit $FERRYDROP_RC_USAGE; }
    ( umask 077; head -1 "$passfile_in" | tr -d '\n' > "$passfile" )
  else
    local typed
    printf "  passphrase: " >&2
    read -rs typed
    printf "\n" >&2
    ( umask 077; printf '%s' "$typed" > "$passfile" )
    unset typed
  fi

  # --- verify BEFORE decrypting ---
  #
  # A tampered blob must fail closed without ever reaching openssl. The MAC is
  # checked here, in python, and the ciphertext is only written out for the
  # decrypt step once that check has passed.
  python3 - "$blob" "$passfile" "$work" "$FERRYDROP_MAGIC" <<'PYEOF'
import sys
blob_path, pass_path, work, magic = sys.argv[1:5]

import hashlib, hmac, os

RC_TAMPER = 3

with open(blob_path) as f:
    raw = f.read()

if "\n--\n" not in raw:
    sys.stderr.write("Error: not a ferry drop blob (no header separator).\n")
    sys.exit(RC_TAMPER)

head_text, ct = raw.split("\n--\n", 1)
lines = head_text.split("\n")

if not lines or lines[0].strip() != magic:
    got = lines[0].strip() if lines else "<empty>"
    sys.stderr.write(f"Error: unsupported blob format {got!r}; this ferry understands {magic}.\n")
    sys.stderr.write("  A newer ferry wrote it — update this machine with `ferry update`.\n")
    sys.exit(RC_TAMPER)

fields, order = {}, []
for ln in lines[1:]:
    if not ln.strip():
        continue
    if ": " not in ln:
        sys.stderr.write("Error: malformed header line in blob.\n")
        sys.exit(RC_TAMPER)
    k, v = ln.split(": ", 1)
    fields[k] = v
    order.append(k)

for required in ("cipher", "kdf", "iter", "kind", "name", "mac"):
    if required not in fields:
        sys.stderr.write(f"Error: blob header is missing '{required}'.\n")
        sys.exit(RC_TAMPER)

try:
    iters = int(fields["iter"])
except ValueError:
    sys.stderr.write("Error: blob header has a non-numeric 'iter'.\n")
    sys.exit(RC_TAMPER)
# A hostile blob could name an absurd iteration count purely to hang the
# recipient's machine. The MAC cannot help here: it is verified with this very
# number, so the cost is paid before the check can reject anything.
if not (1000 <= iters <= 5_000_000):
    sys.stderr.write(f"Error: refusing an implausible iteration count ({iters}).\n")
    sys.exit(RC_TAMPER)

with open(pass_path, "rb") as f:
    passphrase = f.read().strip()

mac_key = hashlib.pbkdf2_hmac("sha256", passphrase, b"ferrydrop-mac-v1", iters, dklen=32)
signed = ("\n".join([magic] + [f"{k}: {fields[k]}" for k in order if k != "mac"])
          + "\n--\n" + ct).encode()
expect = hmac.new(mac_key, signed, hashlib.sha256).hexdigest()

if not hmac.compare_digest(expect, fields["mac"]):
    sys.stderr.write("Error: authentication failed.\n")
    sys.stderr.write("  Either the passphrase is wrong, or this blob was modified in transit.\n")
    sys.stderr.write("  Nothing was decrypted.\n")
    sys.exit(RC_TAMPER)

# Path safety does NOT ride on the crypto being right. Reduce to a basename
# unconditionally, so even a correctly-signed blob from a sender who has turned
# hostile cannot escape the destination directory.
name = os.path.basename(fields["name"].strip().replace("\\", "/").rstrip("/"))
if name in ("", ".", ".."):
    name = "ferrydrop.out"

with open(os.path.join(work, "ct.b64"), "w") as f:
    f.write(ct)
with open(os.path.join(work, "meta"), "w") as f:
    f.write(f"{fields['kind']}\n{name}\n{iters}\n")
PYEOF
  local rc=$?
  if (( rc != 0 )); then exit $rc; fi

  local kind name iters
  kind="$(sed -n 1p "$work/meta")"
  name="$(sed -n 2p "$work/meta")"
  iters="$(sed -n 3p "$work/meta")"

  if ! openssl enc -d -aes-256-cbc -pbkdf2 -iter "$iters" -a \
        -in "$work/ct.b64" -out "$work/pt" -pass "file:$passfile" 2>"$work/err"; then
    # The MAC already passed, so the passphrase was right and the bytes are
    # intact. Reaching here means something stranger — a cipher mismatch, or a
    # blob written by a build whose parameters differ.
    echo "Error: decryption failed after the MAC verified." >&2
    sed 's/^/    /' "$work/err" >&2
    exit $FERRYDROP_RC_BADPASS
  fi

  if [[ "$kind" == "msg" && -z "$out" ]]; then
    cat "$work/pt"
    return 0
  fi

  local dest="${out:-$name}"
  # Never write THROUGH a symlink planted at the destination.
  if [[ -L "$dest" ]]; then
    echo "Error: $dest is a symlink; refusing to write through it." >&2
    exit $FERRYDROP_RC_USAGE
  fi
  [[ -d "$dest" ]] && dest="$dest/$name"
  cp "$work/pt" "$dest"
  local g="" z=""
  if [[ -t 1 ]]; then g=$'\033[1;32m'; z=$'\033[0m'; fi
  echo "  ${g}${dest}${z}  ($(wc -c < "$dest" | tr -d ' ') bytes, verified)"
}
