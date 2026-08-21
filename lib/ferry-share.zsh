
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
  echo ">>> COMMAND TO RUN ON ANY CLIENT LAPTOP ON THE SAME LAN:"
  echo "    \033[1;32mcurl -fsSL http://$MDNS_NAME:$target_port/client-bootstrap.sh | zsh\033[0m"
  echo "    (or: curl -fsSL http://$LAN_IP:$target_port/client-bootstrap.sh | zsh)"
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
        if self.path == "/client-bootstrap.sh" or self.path.endswith("/client-bootstrap.sh"):
            file_path = os.path.join(self.directory, "client-bootstrap.sh")
            if os.path.exists(file_path):
                try:
                    with open(file_path, "r", encoding="utf-8") as f:
                        content = f.read()
                    content = content.replace("HOST_MDNS_PLACEHOLDER", mdns_name)
                    content = content.replace("SHARE_PORT_PLACEHOLDER", str(port))
                    self.send_response(200)
                    self.send_header("Content-Type", "application/x-sh")
                    self.send_header("Content-Length", str(len(content)))
                    self.end_headers()
                    self.wfile.write(content.encode("utf-8"))
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
                
                # Append client logs to host workspace
                log_file_path = os.path.join(self.directory, "client_logs.txt")
                with open(log_file_path, "a", encoding="utf-8") as lf:
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

