# VNC through ferry — design

Date: 2026-09-12. Status: approved in chat, implementation to follow via a written plan.

## Goal

A locked-down laptop (a ferry **client**) runs a VNC server. Another device on
the LAN — a phone, another laptop — sees that screen by connecting to the ferry
**host**, either with a native VNC client or with nothing but a browser. The
laptop only ever dials out, exactly as with `ferry expose`.

Out of scope: reaching the host's own screen from a client, enabling Screen
Sharing on the laptop (needs `sudo` on macOS), TLS, and any auth beyond what the
VNC server already has.

## What exists

`ferry relay` (host) + `ferry expose <port>` (client) already move raw TCP in
this direction; `ferry expose 5900` publishes a VNC server today. The relay
records each published port in `$RELAY_STATE_FILE` (`{port: {client, label,
bind, since}}`), `ferry status` prints that file, and `ferry down` kills the
relay by its `ferry-relay-marker` sentinel. All of that is reused unchanged.

## Components

### 1. `ferry expose-vnc` (client, `lib/ferry-relay.zsh`)

```
ferry expose-vnc [--local PORT] [--as PUBLIC] [--host H] [--port P] [--token T]
```

- Defaults: local port `5900`, public port = local port. Every other flag is
  the same as `ferry expose` and is passed straight through.
- **Preflight**: connect to `127.0.0.1:<local>` and read up to 12 bytes; they
  must start with `RFB ` (the RFB ProtocolVersion greeting, e.g.
  `RFB 003.008\n`). A refused connection or a non-RFB greeting exits 1 with a
  message naming the fix (turn on Screen Sharing / start the VNC server), so a
  dead port is never published.
- Registers with `"kind": "vnc"` in the existing register message. `ferry
  expose` sends `"kind": "tcp"`; the relay stores `kind` in the state file
  (default `tcp` when absent, so old clients keep working).
- After the relay acks, prints the two ways in: `vnc://<host>:<public>` and
  `http://<host>:8099/vnc/<public>` (the second only says "if the host runs
  `ferry serve-vnc`").
- Same lifetime rule as expose: `exec`s the python tunnel, foreground, Ctrl-C
  stops publishing.

Implementation: `cmd_expose` grows an internal `kind` variable (default `tcp`)
and the preflight is a small function called only when `kind=vnc`.
`cmd_expose_vnc` parses `--local`, sets `kind=vnc`, and delegates to the same
python tunnel with `kind` as one more argv.

### 2. `ferry serve-vnc` (host, new module `lib/ferry-vnc.zsh`)

```
ferry serve-vnc [--port 8099] [--bind ADDR] [--foreground]
```

A python-stdlib daemon (`nohup … & disown`, sentinel arg `ferry-vnc-marker`,
log `$LOG_DIR/vnc-8099.log`), host-only like `ferry relay`. Three routes:

| Route | What |
|---|---|
| `GET /` | Index: every entry of the relay state file with `kind == "vnc"`, as links to `/vnc/<port>`, plus client name and since-time. Empty state or missing file renders "nothing published". |
| `GET /vnc/<port>` | The viewer page: noVNC's `vnc.html` served with the WebSocket path pre-set to `/ws/<port>` (via `?path=ws/<port>&autoconnect=1&resize=scale`). |
| `GET /novnc/…` | Static files from `~/.config/ferry/novnc/` (the unpacked release). |
| `GET /ws/<port>` | WebSocket → TCP bridge to `127.0.0.1:<port>`. |

**Port gating.** `/ws/<port>` bridges only if `<port>` is present in the relay
state file **at the moment of the request** with `kind == "vnc"`. Anything else
is `403`. This keeps the bridge from becoming a generic WebSocket-to-TCP hole
into the host, and it means a port that stops being published stops being
bridgeable immediately.

**WebSocket.** RFC 6455 server side, stdlib only: `Sec-WebSocket-Accept`
(SHA-1 + base64 of key + GUID), `Sec-WebSocket-Protocol: binary` when offered,
binary frames both ways, masked client frames unmasked, server frames unmasked,
payload lengths 7/16/64-bit, `ping`→`pong`, `close` handshake. Two pump threads
per connection (ws→tcp, tcp→ws), same shape as the relay's `splice`. Chunk size
64 KiB. No compression extension (declined by not echoing it).

**noVNC fetch.** On first `serve-vnc` (or `--fetch` alone), download
`https://github.com/novnc/noVNC/archive/refs/tags/v1.7.0.tar.gz`, verify a
pinned SHA-256 (computed during implementation and stored as
`NOVNC_SHA256` in `ferry-core.zsh` next to `NOVNC_VERSION`), and extract only
`app/`, `core/`, `vendor/`, `vnc.html` into `~/.config/ferry/novnc/`. A
`VERSION` file inside records the tag; a mismatch re-fetches. The daemon refuses
to start without it and says how to fetch. `ferry down` does not delete it.

**Bind.** `0.0.0.0` by default (the LAN, like relay's published ports);
`--bind 127.0.0.1` keeps it local to the host.

### 3. Glue

- `lib/ferry-core.zsh`: `VNC_PORT="8099"`, `VNC_LOG`, `NOVNC_VERSION`,
  `NOVNC_SHA256`, `NOVNC_DIR`. The relay's reserved-port list gains
  `$VNC_PORT`.
- `lib/ferry-main.zsh`: `expose-vnc` and `serve-vnc` dispatch entries.
- `build.zsh`: `vnc` added to `MODULES` after `relay`.
- `lib/ferry-serve.zsh`: `ferry down` kills `ferry-vnc-marker`; `ferry status`
  reports the viewer as ONLINE and, for each published `kind == vnc` port,
  prints the viewer URL. `ferry usage` gains the two commands.
- README: port table row, command table rows, and a subsection under "Reverse
  expose" describing the flow, the preflight, the gating rule, and the security
  posture (relay token authenticates the publisher, the VNC password gates the
  screen, plain HTTP on the LAN; RFB passwords cross the wire as VNC always
  sends them).
- `VERSION` bump to 1.33.0 and a `docs/releases/` entry, following the existing
  release pattern in the repo.

## Data flow

```
phone browser ──http──▶ host:8099 /vnc/5900  (noVNC page)
phone browser ──ws────▶ host:8099 /ws/5900 ──tcp──▶ host:5900 (relay-published)
                                                        │ relay data conn
                                                        ▼
                                              laptop ──▶ 127.0.0.1:5900 (VNC server)
```

## Error handling

| Condition | Behaviour |
|---|---|
| Local port not RFB | `expose-vnc` exits 1 before touching the relay. |
| noVNC not fetched | `serve-vnc` exits 1 naming `ferry serve-vnc --fetch`. |
| Checksum mismatch | Fetch aborts, partial download removed, exit 1. |
| `/ws/<port>` not published as vnc | HTTP 403, connection closed. |
| Published port unreachable | Bridge sends WebSocket close 1011 and closes. |
| Client leaves (laptop lid shut) | Relay reaps; state file loses the entry; next `/ws` is 403; live bridges close when the TCP side EOFs. |
| Bad WebSocket handshake | HTTP 400. |

## Testing

`lib/ferry-vnc.test.py` (stdlib unittest, same style as `ferry-relay.test.py`):
the bridge's python is exercised as a real process, with real sockets.

- Handshake: correct `Sec-WebSocket-Accept` for a known key; missing key → 400.
- Framing: round-trip of 1-byte, 200-byte, 70 000-byte payloads (all three
  length encodings), masked in / unmasked out; ping answered with pong; close
  handshake completes.
- Gating: `/ws/<port>` for a port absent from the state file → 403; for a port
  present with `kind: tcp` → 403; present with `kind: vnc` → bridged, bytes
  arrive at a dummy TCP service and echo back.
- Index: renders the vnc entries and not the tcp ones; empty and missing state
  file both render the empty message.
- Static: `/novnc/vnc.html` served from a temp directory; path traversal
  (`/novnc/../…`) → 404.
- Fetch: extraction from a locally built tarball with the right and wrong
  checksum (no network in tests).

`lib/ferry-relay.test.py` additions: `expose-vnc` refuses a non-RFB local
service; publishes when a fake service greets `RFB 003.008\n`; the state file
carries `kind: vnc`; plain `expose` writes `kind: tcp`.

Build check: `zsh build.zsh` then the existing test suite, then a live run of
`ferry serve-vnc --foreground` against a fake RFB service with `curl` on the
index and viewer routes.
