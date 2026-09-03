# TODO: share server + relay hardening (phase 3 — deferred from v1.22.0)

v1.22.0 hardened the inference front door only (master-key auth, MLX lanes on
loopback). The two other always-on listeners — the LAN share server
(`ferry share`, port 8095) and the reverse relay (`ferry relay`, port 8098) —
still trust the LAN completely. That is a defensible posture on a home network,
and it is phase 2's prerequisite: none of the tunnel options in
[TODO-internet-tunnel-integration.md](TODO-internet-tunnel-integration.md)
should go live while any item below is open.

## Share server (8095)

- [ ] **Token-gate the routes.** `/hq` (telemetry append), `/manifest`, and the
      file/model pull routes answer any device that can reach the port, with no
      credential at all. The front door learned a master key in v1.22; the
      share server should demand the same one (one header check in the embedded
      handler). Every client already holds the key, so there is nothing new to
      distribute — `FERRY_MASTER_KEY` rides the bootstrap one-liner as of
      v1.22.
- [ ] **Stop serving the checkout root.** The server hands out an entire
      directory tree — `lib/ferry-share.zsh:172` starts a `ThreadingHTTPServer`
      with `directory=` pointed at the checkout — so the box that serves the
      client bootstraps also serves `.git/` and everything else in the repo.
      `client_logs.txt` was moved out of the checkout (to
      `~/.config/ferry/`) for exactly this reason; the directory hole is the
      same class of exposure with a bigger blast radius. Serve a purpose-built
      `share-root/` instead: the three client scripts plus the offered-file
      manifest, copied or linked at `ferry share` start. Nothing else exists at
      that URL.
- [ ] **Cap `/hq` and make it JSONL.** The handler (`ferry:1495-1515`; the same
      embedded code lives at `lib/ferry-share.zsh:148-167`) reads whatever
      `Content-Length` claims into memory — an unauthenticated LAN device can
      POST a 10 GB body — and appends it verbatim between
      `=== CLIENT LOG ENTRY ===` delimiters, so a body *containing* that
      delimiter forges entries and breaks `ferry inbox`'s align-from-the-end
      join. Fix both at once: reject bodies over a few MB (read with a cap,
      never trust the header), and store one JSON object per line
      (`{"ts":…,"ip":…,"body":…}`) so the delimiter becomes data instead of
      syntax. Migrating `ferry inbox` to read the new format — while still
      reading the old one, for logs that already exist — is part of the same
      change, not a follow-up.

## Relay (8098)

- [ ] **HMAC challenge-response — the token never crosses the wire.** Today the
      client *sends* the shared token in cleartext: `register` carries it
      (`lib/ferry-relay.zsh:498`), and every data connection carries it again
      (`:477`, `:484`). One LAN sniff and an attacker holds the registration
      credential. The server should send a random challenge per connection; the
      client answers with `HMAC(token, challenge)`; the server compares digests
      with the `hmac.compare_digest` it already uses for the token check
      (`lib/ferry-relay.zsh:294-305`). The token itself stays on the two
      machines that own it.
- [ ] **Per-IP registration caps.** Any LAN host can open control connections
      and register until ports and parked sockets run out. Bound concurrent
      registrations per source IP (a handful), and shed the rest with the same
      quiet close the bad-token path already uses — no log spam, no resource
      wedge.
- [ ] **Flip the published-port bind default to `127.0.0.1`.** `ferry relay`
      publishes exposures on `0.0.0.0` today (`lib/ferry-relay.zsh:38`), so
      anything that can reach the host reaches the client's published port —
      including ports the client only ever meant for itself. Host-local is the
      safe default for a feature whose entire threat model is "the client sits
      on a network it does not control"; exposing to the LAN becomes the
      opt-in (`--bind 0.0.0.0`), not the other way around. Update the README's
      reverse-expose section, which currently documents the LAN default as
      deliberate.
