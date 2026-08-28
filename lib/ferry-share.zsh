
cmd_share() {
  if (( CLIENT_MODE )); then
    echo "Error: Command 'ferry share' is only available on the LLM-Ferry Host Mac."
    exit 1
  fi

  local target_port="$SHARE_PORT"
  
  # Scan upwards to find an available port
  while lsof -nP -iTCP:"$target_port" -sTCP:LISTEN >/dev/null 2>&1; do
    echo ">>> Share port $target_port is already in use. Checking next port..."
    target_port=$((target_port + 1))
  done

  echo "================================================================="
  echo "                SHARING LLM-FERRY CLIENT BOOTSTRAPPER"
  echo "================================================================="
  echo "Serving directory: $APP_DIR"
  echo "Port bound:        $target_port"
  echo "================================================================="
  echo ">>> FIRST-TIME SETUP on any client laptop on the same LAN:"
  echo "    \033[1;32mcurl -fsSL http://$MDNS_NAME:$target_port/client-bootstrap.sh | zsh\033[0m"
  echo "    (or: curl -fsSL http://$LAN_IP:$target_port/client-bootstrap.sh | zsh)"
  echo "    Narrow the opencode scope on a laptop that already has its own setup:"
  echo "      ... /client-bootstrap.sh | zsh -s -- --profiles-only   (ferry's own profiles only)"
  echo "      ... /client-bootstrap.sh | zsh -s -- --no-opencode     (the CLI and nothing else)"
  echo ""
  echo ">>> CATCH UP an already-bootstrapped client (re-pull the CLI, re-apply"
  echo "    the opencode takeover; leaves ~/.zshrc alone):"
  echo "    \033[1;32mcurl -fsSL http://$MDNS_NAME:$target_port/client-reset.sh | zsh\033[0m"
  echo "    (or: curl -fsSL http://$LAN_IP:$target_port/client-reset.sh | zsh)"
  echo ""
  echo ">>> REMOVE ferry from a client (CLI, profile, wrappers, guardrails;"
  echo "    keeps opencode's own session history unless --full --yes):"
  echo "    \033[1;32mcurl -fsSL http://$MDNS_NAME:$target_port/client-cleanup.sh | zsh -s -- --dry-run\033[0m"
  echo "    (drop --dry-run to apply)"
  echo "================================================================="
  echo ">>> Starting Dynamic Python HTTP share server in background..."

  # Launch the dynamic share server in background (log to share log)
  # The trailing "ferry-share-marker" arg is a stable kill sentinel for `ferry down` (see cmd_down);
  # the heredoc script only reads argv[1]/argv[2], so the extra arg is ignored at runtime.
  nohup python3 - "$target_port" "$APP_DIR" "ferry-share-marker" <<'PYEOF' > "$SHARE_LOG" 2>&1 & disown
import sys, os, socket, subprocess, json, tarfile, glob
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

port = int(sys.argv[1])
directory = sys.argv[2]

# macOS advertises scutil's LocalHostName; other OSes (Linux/avahi) advertise the
# short hostname as <hostname>.local. Try scutil first, then fall back to the hostname.
mdns_name = subprocess.getoutput("scutil --get LocalHostName 2>/dev/null").strip().lower()
if mdns_name:
    mdns_name += ".local"
else:
    mdns_name = socket.gethostname().split(".")[0].lower() + ".local"

# Ferry transfer locations: the host's HuggingFace cache and the offered-files manifest.
HF_HUB = os.path.expanduser("~/.cache/huggingface/hub")
OFFERED = os.path.expanduser("~/.config/ferry/offered.json")
# Client telemetry (`ferry msg` / `ferry log`) lands here, NOT under the serving
# directory. `directory` is whatever tree the server was launched from, captured
# once at startup: a checkout that later moves or is deleted — a git worktree
# removed after the share server was started from it — turns every /hq POST into
# an unhandled exception and a bare 500. The client sees a failed send, the host
# sees nothing, and the message is gone. Observed 2026-08-26, two messages lost.
# Same stable-path treatment as OFFERED above, so telemetry outlives any checkout.
CLIENT_LOG = os.path.expanduser("~/.config/ferry/client_logs.txt")

class DynamicHandler(SimpleHTTPRequestHandler):
    def _tar_stream(self, root, arcname):
        # Stream a tar of `root` back to the client. dereference=True resolves the
        # HuggingFace cache's snapshot symlinks (which point into ../../blobs) into
        # real file content, so the pulled model is self-contained on the client.
        self.send_response(200)
        self.send_header("Content-Type", "application/x-tar")
        self.end_headers()
        with tarfile.open(fileobj=self.wfile, mode="w|", dereference=True) as tar:
            tar.add(root, arcname=arcname)

    def do_GET(self):
        # Client-facing scripts get the host's live identity injected. Add a name
        # here and it is served the same way; serve it as a plain static file and
        # its placeholders reach the client verbatim, where they resolve to a
        # bogus `your-host.local` and the script fails at its first request.
        INJECTED = ("client-bootstrap.sh", "client-reset.sh")
        requested = self.path.rsplit("/", 1)[-1]
        if requested in INJECTED:
            file_path = os.path.join(self.directory, requested)
            if os.path.exists(file_path):
                try:
                    with open(file_path, "r", encoding="utf-8") as f:
                        content = f.read()
                    content = content.replace("HOST_MDNS_PLACEHOLDER", mdns_name)
                    content = content.replace("SHARE_PORT_PLACEHOLDER", str(port))
                    # Also rewrite the script's own your-host.local fallback, so
                    # even the no-injection code path lands on the real host.
                    content = content.replace("your-host.local", mdns_name)
                    content = content.replace('"your-host.local"', f'"{mdns_name}"')
                    body = content.encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/x-sh")
                    # Content-Length MUST count BYTES, not characters: the script
                    # contains multi-byte UTF-8 (em-dashes), and a char-count header
                    # silently truncates the tail (an unterminated `echo "` at EOF,
                    # which the client's zsh reports as `unmatched "`).
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                except Exception as e:
                    print(f"Error performing dynamic replacement: {e}")

        # /pull/<model-id> — tar-stream a model from the host's local HuggingFace cache.
        if self.path.startswith("/pull/"):
            model_id = self.path[len("/pull/"):].strip("/")
            base = os.path.join(HF_HUB, "models--" + model_id.replace("/", "--"))
            snaps = sorted(glob.glob(os.path.join(base, "snapshots", "*")))
            if not snaps:
                self.send_response(404); self.end_headers()
                self.wfile.write(b"model not in host cache\n"); return
            self._tar_stream(snaps[-1], model_id.split("/")[-1]); return

        # /file/<name> — tar-stream a previously offered file/dir (see `ferry offer`).
        if self.path.startswith("/file/"):
            name = self.path[len("/file/"):].strip("/")
            offered = json.load(open(OFFERED)) if os.path.exists(OFFERED) else {}
            p = offered.get(name)
            if not p or not os.path.exists(p):
                self.send_response(404); self.end_headers()
                self.wfile.write(b"not offered\n"); return
            self._tar_stream(p, os.path.basename(p.rstrip("/"))); return

        # /manifest — list the models in the host cache and the offered files.
        if self.path == "/manifest" or self.path.endswith("/manifest"):
            models = [os.path.basename(d)[len("models--"):].replace("--", "/")
                      for d in glob.glob(os.path.join(HF_HUB, "models--*"))]
            offered = json.load(open(OFFERED)) if os.path.exists(OFFERED) else {}
            body = json.dumps({"models": sorted(models), "files": sorted(offered.keys())}, indent=2).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body))); self.end_headers()
            self.wfile.write(body); return

        super().do_GET()

    def do_POST(self):
        if self.path == "/hq" or self.path.endswith("/hq"):
            try:
                content_length = int(self.headers.get('Content-Length', 0))
                post_data = self.rfile.read(content_length).decode('utf-8')
                
                # Append client telemetry to the stable host-side log.
                os.makedirs(os.path.dirname(CLIENT_LOG), exist_ok=True)
                with open(CLIENT_LOG, "a", encoding="utf-8") as lf:
                    lf.write(f"=== CLIENT LOG ENTRY ===\n{post_data}\n\n")
                
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"Logged successfully at HQ!\n")
                return
            except Exception as e:
                print(f"Error handling HQ log: {e}")
                self.send_response(500)
                self.end_headers()
                return
        self.send_response(444)
        self.end_headers()

handler = lambda *a, **kw: DynamicHandler(*a, directory=directory, **kw)
ThreadingHTTPServer(('0.0.0.0', port), handler).serve_forever()
PYEOF

  echo ">>> Sharing server running in background. Log: $SHARE_LOG"
}

cmd_msg() {
  if (( ! CLIENT_MODE )); then
    echo "Error: Command 'ferry msg' is only available in Client Mode."
    exit 1
  fi
  if [[ $# -lt 1 ]]; then
    echo "Usage: ferry msg <your text message here>"
    exit 1
  fi
  local text="$*"
  echo ">>> Streaming direct text telemetry to Host HQ..."
  curl -sS -X POST --data-binary "$text" "http://$CLIENT_HOST:$CLIENT_SHARE_PORT/hq"
}

cmd_log() {
  if (( ! CLIENT_MODE )); then
    echo "Error: Command 'ferry log' is only available in Client Mode."
    exit 1
  fi
  echo ">>> Streaming stdin log stream directly back to Host HQ..."
  curl -sS -X POST --data-binary @- "http://$CLIENT_HOST:$CLIENT_SHARE_PORT/hq"
}

