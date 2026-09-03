# TODO: internet tunnel integration (phase 2 — deferred from v1.22.0)

v1.22.0 hardened the front door (master-key auth, MLX lanes on loopback) and
documented Tailscale Serve as the remote-access recipe (README →
[Remote access (Tailscale)](../README.md#remote-access-tailscale)). That recipe
covers *your* devices. This doc is the deferred work: making the endpoint
reachable for laptops and people outside the tailnet.

Nothing here is scheduled. Do it when a real need shows up, not before.

## The scheme problem: ferry assumes `http://` everywhere

`~/.config/ferry/client.json` carries `host` + `port` and every consumer
hardcodes the scheme. A tunnel front door is `https://`, so the scheme has to
become data:

- [ ] Add an optional `scheme` field to `client.json` (`http` default; `https`
      for the ts.net / tunnel case). Absent = `http`, so every existing profile
      stays valid untouched.
- [ ] Thread it through the hardcoded construction sites:
  - `lib/ferry-integrate.zsh:327` — `base = f"http://{host}:{port}/v1"` in the
    opencode config writer.
  - `lib/ferry-claude.zsh:90` (and the `-local` / `-super` twins below it) —
    `ANTHROPIC_BASE_URL="http://__FERRY_CL_HOST__:__FERRY_CL_PORT__"`, baked in
    at install time. The placeholder pair becomes a triple
    (`__FERRY_CL_SCHEME__`).
  - `lib/ferry-update.zsh:79` — the client catch-up one-liner
    `curl -fsSL http://$CLIENT_HOST:$CLIENT_SHARE_PORT/client-reset.sh`. Note
    this is the *share* port, not the endpoint port: a tunneled endpoint does
    not automatically mean a tunneled share server (see the last section).
  - `lib/ferry-share.zsh:198` — `ferry msg` / `ferry log` POST to
    `http://$CLIENT_HOST:$CLIENT_SHARE_PORT/hq`.
  - `lib/ferry-serve.zsh:707` — `ferry status` prints and probes
    `http://$CLIENT_HOST:$CLIENT_PORT`.
  - `client-bootstrap.sh` / `client-reset.sh` regenerate all of the above, so
    the scheme has to survive the curl-pipe bootstrap path too.

## Alternatives to Tailscale, and why each was passed over

- **cloudflared named tunnel** — stable hostname, real TLS, no tailnet required
  for visitors. Costs: a Cloudflare account plus a managed domain, and the free
  edge applies a ~100-second no-data timeout that is hostile to LLM streams —
  a quiet mid-generation pause or a slow GPU lane decoding at 30 tok/s can
  outrun it mid-response. It also puts a third party's policy between you and
  your own model.
- **cloudflared quick tunnel (`cloudflared tunnel --url`)** — no account, but
  the `trycloudflare.com` URL is **ephemeral**: new on every launch. A
  bootstrap one-liner whose URL dies nightly is a support-ticket generator, and
  every `client.json` in the fleet needs a rewrite per restart.
- **VPS relay** (WireGuard + caddy/nginx on a cheap box) — full control,
  stable DNS, real TLS. Costs: another machine to keep patched, and **relay
  visitors are unauthenticated by design** unless you build auth at the relay —
  which means re-inventing the master-key gate one hop earlier, with the
  origin leg plaintext unless the relay re-encrypts back to the host. This is
  the "run a hosted service" path the README explicitly declines.
- **Tailscale (shipped as the v1.22 recipe)** — identity + TLS without ferry
  managing any of it, but only for tailnet members. `tailscale share` can
  extend a node to a specific outsider; it is clunky as a permanent arrangement
  for someone else's laptop.

The honest read: every non-tailnet option trades away streaming reliability,
stability, or the not-a-hosted-service posture. That is why this is a TODO and
not a feature.

## Also needed when this lands

- `ferry share` prints the client one-liner; it should print the tunnel-aware
  variant when the host itself is being served over a tunnel.
- TLS must be end-to-end. A VPS relay terminates TLS at the box, so the master
  key and every token cross the origin leg in whatever the relay forwards.
  Origin-pull TLS or the exercise is theater.
- Phase 3 first: opening the door to the internet while the share server and
  relay are in their current unhardened state (see
  [TODO-share-relay-hardening.md](TODO-share-relay-hardening.md)) would be
  publishing the telemetry log and the relay token to whoever finds the URL.
