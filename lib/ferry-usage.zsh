
# Banner Help
usage() {
  cat <<EOF
LLM-Ferry CLI (ferry) — Decoupled Local AI LAN sharing & Cloud proxying.

Usage:
  ferry <command> [options]

Commands:
  install            Install uv, litellm, and link globally (+ mlx-vlm & models on macOS)
  up                 [Host] Start local GPU server or cloud API proxy (boots Catalog by default)
  down               [Host] Stop all running servers (local, cloud, sharing)
  status             [Dual] Show server listeners, active models, and client test commands
  dash               [Host] Live dashboard for the route proxy
                       ferry dash [--open] [--port P] [--ferry URL]   # lightweight stdlib page (localhost:8091)
                       ferry dash --grafana [--open]                  # full Grafana+VictoriaMetrics stack (localhost:3001)
                       ferry dash --grafana --down                    # stop the Grafana stack
  share              [Host] Expose client-bootstrap.sh over LAN for other laptops to curl
  msg <text>         [Client] Send a direct text message to host's client_logs.txt
  log                [Client] Pipe stdin log stream directly back to host
  env                [Client] Emit shell exports so downloads route via the host proxy
                       eval "\$(ferry env --host H)"  [--proxy-port P] [--hf-port P2] [--write]
  opencode           [Client] Auto-wire opencode to route through the host (detects served models)
                       ferry opencode [--host H] [--port P] [--config PATH] [--model M] [--small-model SM] [--no-default]

Ferrying models & files across the LAN:
  offer <path>...    [Host] Record files/dirs in ~/.config/ferry/offered.json for clients to fetch
  pull <model-id>    [Client] Pull a model from the host's local HF cache
                       [--host H] [--port P] [--transport http|hf|nc] [--to DIR]
                       http (default): stream+untar from the share server
                       hf:  download THROUGH the host's 'ferry serve-hf' proxy (EXPERIMENTAL)
                       nc:  listen for a netcat push (then run 'ferry send' on the host)
  get <name>         [Client] Fetch an offered file/dir  [--host H] [--port P] [--to DIR]
  receive            [Client] Listen for a netcat tar stream  [--port P] [--to DIR]
  send <path> <cli>  [Host] Push a file/dir to a listening client  [--port P]
  serve-hf           [Host] Start EXPERIMENTAL HuggingFace pass-through proxy [--port P] (default $HF_PORT)
  serve-proxy        [Host] Start a general HTTP(S) forward proxy for client downloads [--port P] (default $PROXY_PORT)

Options for 'up':
  -l, --local        Launch local GPU model ($LOCAL_MODEL) [macOS / Apple Silicon only]
  -o, --orch         Launch the local ORCHESTRATOR model ($LOCAL_MODEL_ORCH) — main +
                        concurrent subagents on the host GPU [macOS / Apple Silicon only]
  -c, --cloud        Proxy to default cloud model ($DEFAULT_GEMINI)
  -m, --model <id>   Proxy directly to any specific LiteLLM cloud model string
  -r, --route        Serve MULTIPLE models from a litellm config (orchestrator + key failover)
                       Uses ~/.config/ferry/litellm.yaml (seeded from template on first run)
  -i, --interactive  Force launch the interactive model selection catalog
  -p, --port <port>  Override listening port [default: $PORT]

Examples:
  ferry up             # Starts in interactive mode to query Gemini's active catalog
  ferry up --route     # Serve orchestrator + Gemini key-failover from litellm.yaml
  ferry up --orch      # Local orchestrator lane (Nemotron 3 Nano 30B A3B NVFP4) for opencode-local
  ferry dash --open    # Open the live route-proxy dashboard in your browser
  ferry status         # View connection health diagnostics
  ferry msg "hello"    # Sends telemetry message back to the host Mac
  cat err.log | ferry log  # Stream errors back to host Mac
EOF
  exit 0
}
