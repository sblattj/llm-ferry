
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
  msg <text>         [Client] Send a direct text message to host's ~/.config/ferry/client_logs.txt
  log                [Client] Pipe stdin log stream directly back to host
  inbox              [Host] Read what clients sent — dated and attributed where the
                       share log still has the receipt
                       ferry inbox [-n N] [-f] [--all] [--path]
  relay              [Host] Accept reverse-expose registrations so a client can
                       publish one of ITS local ports through this host
                       ferry relay [--port P] [--bind ADDR] [--token] [--foreground]
  expose <port>      [Client] Publish 127.0.0.1:<port> from the host, dialling only
                       outbound — for a laptop that cannot accept inbound at all
                       ferry expose <port> [--as PUBLIC] [--host H] [--token T]
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
  (no flags)         THE STACK — all four lanes on one endpoint (:$PORT):
                       orch        cloud  GLM 5.3 + strict fallback chain
                       flash       cloud  Gemini 3.7 Flash key pool
                       local-orch  GPU    $LOCAL_MODEL_ORCH
                       local-sub   GPU    $LOCAL_MODEL_SUB
                     The GPU lanes run on internal ports $LOCAL_ORCH_PORT/$LOCAL_SUB_PORT;
                     clients only ever address :$PORT and pick a lane by name.
  -a, --all, --stack Same as no flags (explicit form)
  -l, --local, --local-orch
                     Launch ONLY the local orchestrator lane ($LOCAL_MODEL_ORCH)
                       [macOS / Apple Silicon only]
  -s, --sub, --local-sub
                     Launch ONLY the local subagent lane ($LOCAL_MODEL_SUB)
                       [macOS / Apple Silicon only]
  -o, --orch         Alias of --local-orch. NOTE: the orchestrator lane is now Qwen;
                       Nemotron moved to --local-sub.
  -c, --cloud        Proxy to default cloud model ($DEFAULT_CLOUD_MODEL)
  -m, --model <id>   Proxy directly to any specific LiteLLM cloud model string
  -r, --route        Serve only the CLOUD lanes (orch + flash) from the litellm config
                       Uses ~/.config/ferry/litellm.yaml (seeded from template on first run)
  -i, --interactive  Force launch the interactive lane/model selection catalog
  -p, --port <port>  Override listening port [default: $PORT]

Examples:
  ferry up             # The full stack: orch + flash + local-orch + local-sub on :$PORT
  ferry up --route     # Cloud lanes only (no GPU weights resident)
  ferry up --local-sub # Just the Nemotron subagent lane, alone on :$PORT
  ferry up -i          # Interactive catalog (query Gemini's live model list)
  ferry dash --open    # Open the live route-proxy dashboard in your browser
  ferry status         # Per-lane health, memory, and served lane names
  ferry msg "hello"    # Sends telemetry message back to the host Mac
  cat err.log | ferry log  # Stream errors back to host Mac
EOF
  exit 0
}
