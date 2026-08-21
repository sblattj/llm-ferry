# llm-ferry

**Turn one Apple Silicon Mac into the AI server your whole LAN shares** — local MLX models or cloud APIs, behind one OpenAI-compatible endpoint, driven by one small `zsh` CLI.

![License](https://img.shields.io/badge/license-MIT-blue.svg)
![Platform](https://img.shields.io/badge/platform-macOS%20·%20Apple%20Silicon-lightgrey.svg)
![Shell](https://img.shields.io/badge/shell-zsh-green.svg)
![API](https://img.shields.io/badge/API-OpenAI--compatible-412991.svg)

![demo](docs/demo.gif)

`llm-ferry` runs models on your Mac's GPU (via [MLX](https://github.com/ml-explore/mlx)) **or** proxies to cloud providers behind the host's own API keys, and exposes a standard **OpenAI-compatible** API (`/v1/chat/completions`, `/v1/models`) that any laptop, editor, or device on the same network can point at. One command on the host, one `curl | zsh` on each client, and everyone's tools "just work." Beyond serving inference, it can also **ferry whole models and files** from the host to clients and **route a client's downloads through the host** — all over your private LAN.

## Is this for you?

- You have a strong Apple Silicon Mac and **other laptops on the same LAN** that you'd like to point at it.
- You want to **share that Mac's GPU** (local MLX inference) and/or **centralize cloud API keys** on one host so client devices never hold them.
- You want editor/CLI tools on the client machines (opencode, Continue, Cursor) to **just work** against a single endpoint after a one-line setup.
- **Mac-only, LAN-only.** The host is macOS on Apple Silicon; traffic is plain HTTP on your private network. This is not a public gateway, an auth layer, or a hosted service.

## How it compares

| | Local MLX serving | Cloud API gateway | One-command LAN client setup | Ferries models + files to clients | Routes client downloads through host |
|---|:---:|:---:|:---:|:---:|:---:|
| **Ollama** | — (GGUF, not MLX) | — | — | — | — |
| **LM Studio** | ✓ | — | — | — | — |
| **raw LiteLLM proxy** | — | ✓ | — | — | — |
| **llm-ferry** | ✓ | ✓ | ✓ | ✓ | ✓ |

This table is about **focus**, not "better." Ollama and LM Studio are excellent local runtimes; a raw LiteLLM proxy is a great cloud gateway. `llm-ferry` is the glue for a specific job: **sharing one Mac's local + cloud models across a LAN**, plus the client-onboarding and file/model ferrying that job needs.

## Quickstart

### Host (your Apple Silicon Mac)

A zero-config bootstrapper installs `uv`, MLX inference (`mlx-vlm`), the cloud proxy (`litellm`), downloads the default models, and links the `ferry` CLI globally:

```bash
cd llm-ferry
./host-bootstrap.sh
```

For cloud mode, set your provider key (never commit it):

```bash
export GEMINI_API_KEY="..."          # or drop it in ~/.config/ferry/secrets.env
```

Then run the host:

```bash
ferry up             # interactive: pick from the host's live model catalog
ferry up -c          # cloud proxy to the default Gemini model, on port 8090
ferry up -l          # local MLX GPU model, on port 8090
ferry up -m <id>     # cloud proxy for a specific LiteLLM model id
ferry share          # expose the client bootstrap over the LAN (port 8095)
ferry status         # show active listeners + the loaded model
ferry down           # stop all servers, proxies, and share servers
```

### Client (any other laptop on the same LAN)

Run `ferry share` on the host, then on the client run the command it prints:

```bash
curl -fsSL http://your-mac.local:8095/client-bootstrap.sh | zsh
```

(`ferry share` prints the exact command with your host's live mDNS name **and** LAN IP — use the IP form if `.local` doesn't resolve on your network.) The bootstrapper installs the `ferry` CLI to `~/.local/bin`, writes `~/.config/ferry/client.json`, configures your chosen editor (opencode / Continue / Cursor) to the host endpoint, and adds a `host-code` shell shortcut. On the client:

```bash
ferry status                     # connection health + the host's active model
ferry msg "note"                 # send a quick note to the host's log
some-command 2>&1 | ferry log    # stream logs/errors back to the host
```

### Route mode — one orchestrator + worker failover

`ferry up -c/-l/-m` serves **one** model. `ferry up --route` serves **multiple** models from one endpoint, driven by a [LiteLLM config](https://docs.litellm.ai/docs/proxy/configs): a big **orchestrator** model plus cheaper **worker** models, with automatic **key failover** on the worker — all API keys staying on the host.

```bash
ferry up --route     # serve orchestrator + gemini-3.7-flash from ~/.config/ferry/litellm.yaml
```

The first run seeds `~/.config/ferry/litellm.yaml` from [`litellm-route-example.yaml`](litellm-route-example.yaml) and stops so you can edit it — set your model ids and export the keys it references (`KIMI_API_KEY`, `GEMINI_API_KEY`, `GEMINI_API_KEY_2`, in your shell or `~/.config/ferry/secrets.env`) — then re-run. The two-key failover is simply **two identical `gemini-3.7-flash` deployments** in the yaml: LiteLLM's router load-balances them and, on a `429`, cools the dead key out and rolls traffic to the second.

Note that LiteLLM only **routes and fails over** — the "orchestrator delegates to workers" agent logic lives in **your client** (opencode / Claude Code / etc.). Point it at `http://<host>.local:8090/v1` with the main model set to `orchestrator` and the subagent model to `gemini-3.7-flash`.

On a client, `ferry opencode` auto-wires opencode to the host — it detects the served models (setting up the `orchestrator` + `gemini-3.7-flash` split when both are present), merges non-destructively into your existing config, and backs up the old one first. `ferry opencode` also pins opencode's built-in agents — `build`/`plan` to the orchestrator, and the `general`/`explore`/`scout` subagents to the worker model — so the fan-out actually uses the cheap lane.

## Ports

| Port | Purpose | Started by |
|---|---|---|
| **8090** | Inference / completions (local model **or** cloud proxy) | `ferry up` |
| **8095** | LAN share server — serves the client bootstrap, model/file ferry routes, and receives client telemetry | `ferry share` |
| **8096** | HuggingFace pass-through proxy (experimental) | `ferry serve-hf` |
| **8097** | General HTTP(S) download forward proxy | `ferry serve-proxy` |
| **9099** | Default netcat port for direct `ferry send` / `ferry receive` | `ferry send` / `ferry receive` |

## Ferrying models & files across the LAN

`ferry` can move whole models (from the host's local HuggingFace cache) and arbitrary files/dirs from the **host** to a **client**, over three transports.

### Models — `ferry pull`

```bash
# http (default): stream + untar from the host's local HF cache via the share server (8095)
ferry pull mlx-community/Qwen3.8-27B-nvfp4 --host your-mac.local
ferry pull org/model --host your-mac.local --to ~/models     # choose the destination

# hf (EXPERIMENTAL): download THROUGH the host's proxy (host must run `ferry serve-hf`)
ferry pull org/model --host your-mac.local --transport hf

# nc: this laptop listens; then on the host run `ferry send` (see below)
ferry pull org/model --transport nc
```

`--host` / `--port` default to the client's saved profile (`~/.config/ferry/client.json`); pass them explicitly from an unconfigured machine. The `http` transport reads the **host's local HuggingFace cache**, so the host must already have the model — or use `ferry serve-hf` + `--transport hf` to fetch it *through* the host.

Plain `curl` works too — the share server exposes the transfer routes directly:

```bash
curl -fsS http://your-mac.local:8095/pull/mlx-community/Qwen3.8-27B-nvfp4 | tar -x
curl -fsS http://your-mac.local:8095/manifest       # list cached models + offered files
```

### Files — `ferry offer` / `ferry get`

```bash
# host — record files/dirs in ~/.config/ferry/offered.json as {basename: absolute-path}
ferry offer ~/datasets/eval.jsonl ~/configs/prompt.txt

# client — fetch by basename
ferry get eval.jsonl --host your-mac.local --to ./data
curl -fsS http://your-mac.local:8095/file/eval.jsonl | tar -x   # plain-curl equivalent
```

### Direct push — `ferry send` / `ferry receive`

For a one-off push with no share server, use netcat (default port 9099):

```bash
# client (start first — it listens)
ferry receive --port 9099 --to ./incoming

# host (then push)
ferry send ~/some/dir client-laptop.local --port 9099
```

### `ferry serve-hf` (experimental)

Starts a pass-through HTTP proxy to `https://huggingface.co` on port 8096, following HF's LFS→CDN redirects, so a client with `HF_ENDPOINT=http://<host>:8096` downloads *through* the host. Intentionally minimal; stop it with `ferry down`.

```bash
ferry serve-hf                                                   # host
HF_ENDPOINT=http://your-mac.local:8096 hf download org/model     # client
# ...or simply: ferry pull org/model --host your-mac.local --transport hf
```

## Route a client's downloads through the host

A client with no (or limited) internet can pull its own dependencies and models *through* the host. Start a general HTTP(S) forward proxy on the host, then have the client emit the proxy env vars into its shell:

```bash
# host
ferry serve-proxy
# client
eval "$(ferry env)"        # or: eval "$(ferry env --host your-mac.local)"
uvx whosaid ...            # uv/PyPI, huggingface_hub, git, curl — now download via the host
```

`ferry env` prints the `HTTP(S)_PROXY` / `HF_ENDPOINT` / `NO_PROXY` exports on stdout so it stays `eval`-able (add `--write` to persist them into `~/.zshrc`). The proxy handles HTTPS via `CONNECT` tunneling and plain HTTP by forwarding, and covers **anything that honors the standard proxy env vars**. It routes each request straight through **the host's own connection with no caching**, so it needs no pre-populated cache — the host just needs internet. Stop it with `ferry down`.

## Privacy

Everything runs on your own hardware and network. Client↔host traffic stays on your **private LAN as plain HTTP**; cloud calls go host→provider over HTTPS using the host's keys, so **client devices never see the keys**. Inbound client telemetry (`ferry msg` / `ferry log`) is appended to `client_logs.txt` on the host, which is **gitignored**.

## Local models

Local mode serves an MLX model via `mlx-vlm`. The default (`mlx-community/Qwen3.8-27B-nvfp4`, with `mlx-community/Qwen3.8-27B-MTP-8bit` as a speculative draft) is just an **example** — swap it for any MLX-compatible model your Mac's unified memory can hold by editing `LOCAL_MODEL` in `ferry`. Speculative decoding is optional.

## Command reference

| Command | Mode | What it does |
|---|---|---|
| `install` | host | Install `uv`, `mlx-vlm`, `litellm`, download default models, link `ferry` globally |
| `up [-l\|-c\|-m <id>\|-r\|-i] [-p <port>]` | host | Start the local GPU server, cloud proxy, or multi-model route config on `8090` (no args → interactive catalog; `-r`/`--route` → route mode) |
| `down` | host | Stop all servers, cloud proxies, and share/proxy servers |
| `status` | both | Host: listeners + active model. Client: connection health + host's active model |
| `share` | host | Serve the client bootstrap + ferry transfer routes over the LAN (`8095`) |
| `msg <text>` | client | Send a text note to the host's `client_logs.txt` |
| `log` | client | Pipe stdin straight to the host's `client_logs.txt` |
| `offer <path>...` | host | Record files/dirs in `offered.json` for clients to fetch |
| `pull <model-id> [--host H] [--port P] [--transport http\|hf\|nc] [--to DIR]` | client | Pull a model from the host cache (three transports) |
| `get <name> [--host H] [--port P] [--to DIR]` | client | Fetch an offered file/dir by basename |
| `receive [--port P] [--to DIR]` | client | Listen for a netcat tar stream (default port `9099`) |
| `send <path> <client-host> [--port P]` | host | Push a file/dir to a listening client via netcat (default `9099`) |
| `serve-hf [--port P]` | host | Start the experimental HuggingFace pass-through proxy (default `8096`) |
| `serve-proxy [--port P]` | host | Start the general HTTP(S) download forward proxy (default `8097`) |
| `env [--host H] [--proxy-port P] [--hf-port P2] [--write]` | client | Emit shell exports so this laptop routes downloads via the host proxy |

Run `ferry --help` for the built-in usage banner.

## License

MIT — see [LICENSE](LICENSE).
