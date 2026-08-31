# Encrypted off-LAN drop, and the host-side opencode wrappers

Two independent changes, shipped together as v1.17.0.

- **`ferry drop` / `ferry pickup`** — the first ferry transport that survives an
  untrusted channel. New subsystem.
- **The host-side opencode wrapper installer** — closes a gap where a fix shipped
  only to clients. Bounded repair.

They share nothing but a release. Read them separately.

---

## Part 1 — `ferry drop` / `ferry pickup`

### Why

Every existing ferry transport assumes a trusted private LAN, and says so:
`README.md:36` — *"Client↔host traffic is plain HTTP on your private network…
This is not a public gateway, an auth layer, or a hosted service."* That is a
deliberate posture, not an oversight, and this change does not revise it.

What it adds is the one case the posture cannot serve: moving a file to a machine
that is **not** on the LAN. Today there is no ferry answer at all. A grep for
`encrypt|decrypt|gpg|openssl|passphrase|nacl|libsodium|tls` across `lib/`, all
`*.sh` / `*.zsh`, and `README.md` returns exactly one hit, and it is the word
"TLS" inside a sentence about corporate MITM (`README.md:484`). Ferry ships no
cryptography.

The model is the `workspace-drop` skill: encrypt on the sending machine, let a
dumb untrusted carrier hold the ciphertext, decrypt with a passphrase that travels
out-of-band. The transport never sees plaintext, so it needs no trust.

**Scope decision: ferry supplies confidentiality, not delivery.** `drop` writes a
blob; `pickup` reads one. Moving it is the operator's problem — email, Slack, a
gist, S3, a USB stick. This keeps ferry free of an auth dependency and a
credential file (`~/.config/ferry/` holds no credential today), and it keeps the
"not a hosted service" line honest. A carrier plugin system was considered and
dropped as speculative.

### Verified substrate

Measured on this host 2026-08-30, before any code was written. Each row is a
claim, its evidence, and a verdict; the control that must fail is included
because evidence that cannot fail proves nothing.

| Claim | Evidence | Verdict |
|---|---|---|
| A stock Mac has a usable openssl | `/usr/bin/openssl` = LibreSSL 3.3.6; `enc -aes-256-cbc -pbkdf2 -iter 600000` exits 0 | pass |
| LibreSSL ↔ OpenSSL 3 blobs interoperate | LibreSSL-encrypted blob decrypts under OpenSSL 3.6.3 (rc=0, canary recovered); reverse also rc=0 | pass |
| `-iter` is honored, not silently ignored | decrypting a 600k-iteration blob with `-iter 1` → **rc=1** | pass (control fails) |
| Wrong passphrase fails closed | rc=1, `bad decrypt` | pass |
| Python stdlib cannot do this alone | `hashlib.pbkdf2_hmac` present; no AES anywhere in stdlib; `Crypto` absent | pass |

The interop row is the one that decided the design. LibreSSL historically lacked
`-pbkdf2` entirely, and had 3.3.6 accepted the flag while ignoring `-iter`, blobs
would have failed silently across machines — the `-iter 1` control is what rules
that out.

**Consequence: openssl is a hard runtime dependency of `drop`/`pickup` only.**
That is a real addition to a project whose badge reads "zsh + python3 stdlib".
It is defensible because ferry already shells out to `curl` and `nc` for exactly
this class of OS-provided binary, and because the stdlib alternative is
hand-rolling AES. The commands must degrade with a clear error, not a stack
trace, when openssl is absent.

### Blob format

Self-describing, so `pickup` needs nothing out-of-band except the passphrase.
Hand-editing the pickup command — workspace-drop's approach, where the cipher
parameters live in a printed string — breaks the moment a parameter changes.

```
FERRYDROP/1
cipher: aes-256-cbc
kdf: pbkdf2
iter: 600000
kind: file
name: brief.md
mac: <hmac-sha256 hex, over every other header line AND the ciphertext>
--
<base64 ciphertext>
```

- `kind` is `file` or `msg`. On `msg`, `pickup` writes to stdout instead of a file.
- `name` is the original basename, used as the default output path.
- The header is ASCII and greppable so a human can identify a stray blob.
- `FERRYDROP/1` is a version gate: `pickup` refuses an unknown major outright
  rather than guessing.

**The MAC covers the header, not just the ciphertext.** Authenticating the
ciphertext alone would leave `name:` attacker-controlled, and `name` is used to
choose an output path — `name: ../../../.ssh/authorized_keys` on a blob the
recipient decrypts is a write-anywhere primitive. The MAC input is therefore the
canonical header (every line except `mac:`, in fixed order, `\n`-joined) followed
by `\n--\n` and the base64 ciphertext.

Defence in depth, because a MAC only helps once the passphrase is right:
`pickup` reduces `name` to its basename unconditionally, rejects it if it is
empty, `.`, or `..`, and refuses to follow a symlink at the destination. Path
safety does not depend on the crypto being correct.

### Encrypt-then-MAC

Raw AES-CBC is malleable. PKCS#7 padding validation catches many edits but is not
an integrity check, and a bit-flip at a known plaintext offset is a real attack.
Shipping unauthenticated CBC in a feature whose entire purpose is safe transit
over a hostile channel would be a knowing defect.

So:

1. openssl performs AES-256-CBC with its own salt and PBKDF2 at 600k iterations.
2. A **separate** MAC key is derived in Python stdlib:
   `hashlib.pbkdf2_hmac("sha256", passphrase, b"ferrydrop-mac-v1", 600000, dklen=32)`.
   The distinct salt keeps it independent of the encryption key.
3. `hmac.new(mac_key, b64_ciphertext, sha256)` is written to the header.
4. **`pickup` verifies the MAC before invoking openssl at all.** A tampered blob
   fails closed without entering the decrypt path. The comparison uses
   `hmac.compare_digest`.

Cost is two PBKDF2 runs, ~1-2s total. Acceptable for a one-shot command.

### Passphrase handling

- Generated with `openssl rand -base64 18`, stripped of `/+=` and hyphen-grouped
  for legibility: `k7Fm-2Qxz-9Lpw-Ttre-8Vbn-Ax`. Roughly 100+ bits.
  A five-word list form was considered and rejected: it needs a wordlist embedded
  in a single-file CLI that clients fetch over the wire, for ~50 bits.
- **Never passed in argv.** `-pass pass:<secret>` is world-readable through `ps`.
  Both directions use `-pass file:` against a 0600 file inside a `mktemp -d`
  directory removed by a shell `trap`.
- Printed to the operator's terminal only, exactly once, at the end of `drop`.
  Never written into the blob, a log, or `client_logs.txt`.
- On `pickup`, read from a no-echo prompt; `--pass-file` accepts a path for
  scripted use.

### Surface

```
ferry drop <file> [--to PATH] [--print-pass]
ferry drop --msg "text" [--to PATH]
ferry drop - [--to PATH]                  # stdin
ferry pickup <blob> [--to PATH] [--pass-file F]
```

Namespace was inventoried before allocation (`claiming-names-and-ports`):
22 dispatch arms in `lib/ferry-main.zsh:12-33`, cross-checked against 21 `cmd_*`
definitions across `lib/*.zsh`. `drop` and `pickup` are both free, and are
deliberately distinct from the LAN-transfer family (`offer`/`get`/`pull`/`send`/
`receive`) which all assume the host. No port is required — the carrier is
external — so the `README.md:395-406` ports table is unchanged. No new `DROP_*`
constant collides with the 47 top-level assignments in `lib/ferry-core.zsh`.

### Files

| File | Change |
|---|---|
| `lib/ferry-drop.zsh` | new module: `cmd_drop`, `cmd_pickup`, helpers |
| `build.zsh` | add `drop` to `MODULES`, between `transfer` and `proxy` |
| `lib/ferry-main.zsh` | two dispatch arms |
| `lib/ferry-usage.zsh` | help text under a new "Encrypted off-LAN transfer" heading |
| `lib/ferry-drop.test.py` | new suite |
| `README.md` | feature bullet + a section |
| `VERSION` | 1.17.0 |
| `ferry` | regenerated by `./build.zsh` |

`build.zsh:22` requires the dispatch parser last, so `drop` must precede `main`
in `MODULES`.

### Tests

Following `lib/ferry-inbox.test.py`, which runs the **real built `ferry`** against
a throwaway `$HOME` and `$TMPDIR` rather than sourcing functions — the same
production-loader discipline, and the reason that suite caught a date-parsing bug
that rendered perfectly while being wrong.

1. File round-trip: `drop` then `pickup` recovers byte-identical content.
2. `--msg` round-trip: recovered on stdout, `kind: msg` in the header.
3. Stdin (`-`) round-trip.
4. Wrong passphrase → non-zero exit, no output file written.
5. **Tampered ciphertext → rejected at the MAC, and openssl is never invoked.**
6. Tampered *header* (`iter` altered) → rejected at the MAC.
7. **Path traversal:** a blob whose `name` is `../../evil` — MAC recomputed so it
   is cryptographically valid — writes to `./evil` in the destination directory
   and nowhere else. This is the test that proves the basename reduction, not the
   MAC, is what contains it.
8. Unknown version (`FERRYDROP/2`) → refused with a clear message.
9. Passphrase never appears in argv (assert no `-pass pass:` in the module).
10. openssl absent → clean error, not a stack trace.

---

## Part 2 — host-side opencode wrappers

### The defect

`opencode-cloud` and `opencode-local` are installed **only** by
`client-bootstrap.sh`. The host never gets them from any ferry code path, even
though `ferry opencode` deliberately wires the host to its own endpoint
(`lib/ferry-integrate.zsh:186-190`), so the host drives local lanes exactly like
a client does.

Traced, one citation per hop:

- `host-bootstrap.sh` contains **zero** occurrences of `opencode`.
- `host-reset.sh` writes the two profile JSONs (`:424`) but never touches
  `~/.zshrc`; its single `zshrc` occurrence is a comment at `:16`.
- `client-bootstrap.sh:358` appends the wrapper block under the exact marker
  `# >>> ferry opencode profiles >>>`.
- `client-bootstrap.sh:322-329` strips a previous block by **exact string
  compare** on that marker.
- `client-cleanup.sh:186-188` uses the same exact compare; `:194-198` removes only
  `alias …` lines, never function definitions.

This is not a hypothesis about why the host lacks them — it is the absence of any
write path, confirmed by reading every candidate writer.

**Observed consequence on the maintainer's host:** the block present in
`~/.zshrc` is hand-written and marked
`# >>> ferry opencode profiles (host) >>>`. That string is not equal to the
marker both strippers compare against, so neither can see it. A future
`client-bootstrap.sh` run would append a *second* block defining the same two
functions, and `client-cleanup.sh` would leave the first behind.

Note this mirrors an already-fixed instance of the same bug:
`_ferry_install_opencode_guardrails` (`lib/ferry-integrate.zsh:1-35`) exists
because the `/fan-out` command and `spawning-subagents` skill "shipped ONLY in
client-bootstrap.sh, so every CLIENT got them and the HOST never did". The fix
here follows that precedent deliberately.

### The fix

`_ferry_install_host_wrappers()` in `lib/ferry-integrate.zsh`, beside the
guardrails installer:

- Writes the **canonical** marker `# >>> ferry opencode profiles >>>`, so the host
  and client converge on one block instead of two.
- Strips any existing canonical block first, by the same exact compare
  `client-bootstrap.sh:322` uses.
- Additionally strips the legacy `(host)` marker, so a hand-written block is
  absorbed rather than duplicated. This is the only behavioral difference from
  the client path, and it exists because that variant is known to be in the wild.
- Installs the two **named** wrappers only. It does **not** write a bare
  `opencode()` function: a host that exports `$OPENCODE_CONFIG` has chosen its
  default deliberately, and wrapping bare `opencode` would fight that choice.
  This matches the client's
  `--profiles-only` scope (`client-bootstrap.sh:358-378`).
- Idempotent: running twice leaves one block.

Called from `host-reset.sh` after the profile-JSON loop at `:424`, so the wrappers
land in the same pass that writes the files they point at.

### Files

| File | Change |
|---|---|
| `lib/ferry-integrate.zsh` | new `_ferry_install_host_wrappers()` |
| `host-reset.sh` | call it after the profile loop |
| `lib/ferry-hostreset.test.py` | assertions for block creation, idempotency, legacy absorption |
| `README.md` | note that the host gets the wrappers too |

### Tests

1. Fresh `~/.zshrc` → one canonical block, both functions present.
2. Run twice → still exactly one block.
3. A pre-existing `(host)`-marked block → absorbed; exactly one block remains,
   and it carries the canonical marker.
4. A pre-existing canonical block → replaced, not duplicated.
5. Unrelated `~/.zshrc` content is preserved across the rewrite.
6. No bare `opencode()` function is written.

---

## Out of scope

- Retrofitting encryption onto `ferry msg` / `ferry log` / the `/hq` endpoint.
  Those are LAN telemetry and the posture at `README.md:546` is deliberate.
- Any carrier integration (GitHub, S3, gist). Explicitly deferred.
- TLS on the ferry endpoint itself.
