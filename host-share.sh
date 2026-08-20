#!/bin/zsh
# host-share.sh — Starts a simple LAN file server to share client-bootstrap.sh and configurations.
# Uses Python's built-in http.server with dynamic file-substitution to eliminate hardcoded hostnames.
# Automatically detects if port 8792 is in use and rolls over to the next free port.

set -eu

START_PORT="${1:-8095}"
PORT="$START_PORT"

# Scan upwards to find a free TCP port if the target is in use
while lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; do
  echo ">>> Port $PORT is already in use. Checking next port..."
  PORT=$((PORT + 1))
done

DIR_PATH="$(dirname "${0:A}")"
MDNS_NAME="$(scutil --get LocalHostName 2>/dev/null | tr 'A-Z' 'a-z').local"
LAN_IP=$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo "Unknown-IP")

echo "================================================================="
echo "                SHARING LAN INFERENCE BOOTSTRAPPER"
echo "================================================================="
echo "Serving directory: $DIR_PATH"
echo "Port bound:        $PORT"
echo "================================================================="
echo ">>> COMMAND TO RUN ON ANY CLIENT LAPTOP ON THE SAME LAN:"
echo "    \033[1;32mcurl -fsSL http://$MDNS_NAME:$PORT/client-bootstrap.sh | zsh\033[0m"
echo "    (or: curl -fsSL http://$LAN_IP:$PORT/client-bootstrap.sh | zsh)"
echo "================================================================="
echo ">>> Starting Dynamic Python HTTP share server..."

# Inline Python server that dynamically rewrites client-bootstrap.sh's HOST_NAME with the host's actual active mDNS name before streaming.
python3 - "$PORT" "$DIR_PATH" <<'PYEOF'
import sys, os, socket, subprocess
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

port = int(sys.argv[1])
directory = sys.argv[2]

# Resolve actual host local mDNS name
mdns_name = subprocess.getoutput("scutil --get LocalHostName 2>/dev/null").strip().lower() + ".local"
if not mdns_name or mdns_name == ".local":
    mdns_name = socket.gethostname().lower()

class DynamicHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        # Serve client-bootstrap.sh dynamically with host's specific mDNS hostname
        if self.path == "/client-bootstrap.sh" or self.path.endswith("/client-bootstrap.sh"):
            file_path = os.path.join(self.directory, "client-bootstrap.sh")
            if os.path.exists(file_path):
                try:
                    with open(file_path, "r", encoding="utf-8") as f:
                        content = f.read()
                    
                    # Dynamically swap out the hardcoded placeholder for the active host's actual mDNS name
                    content = content.replace("HOST_MDNS_PLACEHOLDER", mdns_name)
                    
                    self.send_response(200)
                    self.send_header("Content-Type", "application/x-sh")
                    self.send_header("Content-Length", str(len(content)))
                    self.end_headers()
                    self.wfile.write(content.encode("utf-8"))
                    return
                except Exception as e:
                    print(f"Error performing dynamic replacement: {e}")
        super().do_GET()

handler = lambda *a, **kw: DynamicHandler(*a, directory=directory, **kw)
ThreadingHTTPServer(('0.0.0.0', port), handler).serve_forever()
PYEOF
