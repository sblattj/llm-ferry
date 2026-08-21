cmd_serve_hf() {
  if (( CLIENT_MODE )); then
    echo "Error: Command 'ferry serve-hf' is only available on the LLM-Ferry Host Mac."
    exit 1
  fi
  local port=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --port) port="$2"; shift 2 ;;
      *)      echo "Unknown option: $1"; exit 1 ;;
    esac
  done
  local hf_port="${port:-$HF_PORT}"
  local hf_log="$LOG_DIR/ferry-hf-$hf_port.log"

  # Free the port if something is already bound there.
  if lsof -nP -iTCP:"$hf_port" -sTCP:LISTEN >/dev/null 2>&1; then
    echo ">>> Port $hf_port already in use. Stopping conflicting listener..."
    lsof -ti tcp:"$hf_port" | xargs kill -9 2>/dev/null || true
    sleep 1
  fi

  echo "================================================================="
  echo "     STARTING HUGGINGFACE PASS-THROUGH PROXY (EXPERIMENTAL)"
  echo "================================================================="
  echo "Proxy port:  $hf_port"
  echo "Clients set: HF_ENDPOINT=http://$MDNS_NAME:$hf_port  (or http://$LAN_IP:$hf_port)"
  echo "Log:         $hf_log"
  echo "================================================================="

  # The trailing "ferry-hf-marker" arg is a stable kill sentinel for `ferry down`;
  # the heredoc only reads argv[1] (the port), so the extra arg is ignored at runtime.
  nohup python3 - "$hf_port" "ferry-hf-marker" <<'PYEOF' > "$hf_log" 2>&1 & disown
import sys, urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

port = int(sys.argv[1])
UPSTREAM = "https://huggingface.co"

class HFProxyHandler(BaseHTTPRequestHandler):
    def _proxy(self, method):
        url = UPSTREAM + self.path
        try:
            req = urllib.request.Request(url, method=method)
            # Forward the headers that matter for auth and ranged/LFS downloads.
            for h in ("Authorization", "User-Agent", "Range", "Accept"):
                v = self.headers.get(h)
                if v:
                    req.add_header(h, v)
            # urllib follows redirects by default — HF redirects LFS blobs to a CDN.
            with urllib.request.urlopen(req, timeout=60) as resp:
                self.send_response(resp.status)
                for k, v in resp.headers.items():
                    if k.lower() in ("transfer-encoding", "connection"):
                        continue
                    self.send_header(k, v)
                self.end_headers()
                if method != "HEAD":
                    while True:
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
        except urllib.error.HTTPError as e:
            self.send_response(e.code)
            self.end_headers()
            try:
                if method != "HEAD":
                    self.wfile.write(e.read())
            except Exception:
                pass
        except Exception as e:
            self.send_response(502)
            self.end_headers()
            try:
                self.wfile.write(("proxy error: %s\n" % e).encode())
            except Exception:
                pass

    def do_GET(self):
        self._proxy("GET")

    def do_HEAD(self):
        self._proxy("HEAD")

    def log_message(self, fmt, *args):
        sys.stderr.write("[ferry-hf] " + (fmt % args) + "\n")

ThreadingHTTPServer(('0.0.0.0', port), HFProxyHandler).serve_forever()
PYEOF

  sleep 1
  if lsof -nP -iTCP:"$hf_port" -sTCP:LISTEN >/dev/null 2>&1; then
    echo ">>> HF proxy is listening on port $hf_port. Stop it with: ferry down"
  else
    echo "WARNING: HF proxy did not come up; check the log: $hf_log"
  fi
}

cmd_serve_proxy() {
  if (( CLIENT_MODE )); then
    echo "Error: Command 'ferry serve-proxy' is only available on the LLM-Ferry Host Mac."
    exit 1
  fi
  local port=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --port) port="$2"; shift 2 ;;
      *)      echo "Unknown option: $1"; exit 1 ;;
    esac
  done
  local proxy_port="${port:-$PROXY_PORT}"
  local proxy_log="$LOG_DIR/ferry-proxy-$proxy_port.log"

  # Free the port if something is already bound there.
  if lsof -nP -iTCP:"$proxy_port" -sTCP:LISTEN >/dev/null 2>&1; then
    echo ">>> Port $proxy_port already in use. Stopping conflicting listener..."
    lsof -ti tcp:"$proxy_port" | xargs kill -9 2>/dev/null || true
    sleep 1
  fi

  echo "================================================================="
  echo "           STARTING HTTP(S) DOWNLOAD FORWARD PROXY"
  echo "================================================================="
  echo "Proxy port:  $proxy_port"
  echo "Routes:      uv/PyPI, huggingface_hub, sherpa-onnx/GitHub, git, curl — anything honoring proxy env vars"
  echo "Log:         $proxy_log"
  echo "================================================================="

  # A general forward proxy: CONNECT tunneling for HTTPS, plain-HTTP forwarding for GET.
  # The trailing "ferry-proxy-marker" arg is a stable kill sentinel for `ferry down`;
  # the heredoc only reads argv[1] (the port), so the extra arg is ignored at runtime.
  nohup python3 - "$proxy_port" "ferry-proxy-marker" <<'PYEOF' > "$proxy_log" 2>&1 & disown
import sys, select, socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(sys.argv[1])   # sys.argv[2] == "ferry-proxy-marker" (kill sentinel, unused)


class Proxy(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_CONNECT(self):
        # Establish a raw TCP tunnel to the requested host:port, then pump bytes
        # both ways until either side closes. This is what HTTPS clients use.
        try:
            host, _, port = self.path.partition(":")
            upstream = socket.create_connection((host, int(port or 443)), timeout=30)
        except Exception:
            self.send_error(502)
            return
        self.send_response(200, "Connection established")
        self.end_headers()
        client = self.connection
        client.setblocking(0)
        upstream.setblocking(0)
        try:
            while True:
                r, _, _ = select.select([client, upstream], [], [], 60)
                if not r:
                    break
                for s in r:
                    data = s.recv(65536)
                    if not data:
                        return
                    (upstream if s is client else client).sendall(data)
        except Exception:
            pass
        finally:
            try:
                upstream.close()
            except Exception:
                pass

    def do_GET(self):
        # Plain-HTTP forwarding: fetch the absolute URL and relay the response.
        import urllib.request
        try:
            req = urllib.request.Request(self.path, headers=dict(self.headers))
            with urllib.request.urlopen(req, timeout=30) as resp:
                self.send_response(resp.status)
                for k, v in resp.headers.items():
                    if k.lower() not in ("transfer-encoding", "connection", "content-length"):
                        self.send_header(k, v)
                body = resp.read()
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        except Exception:
            self.send_error(502)

    def log_message(self, *a):
        pass


ThreadingHTTPServer(("0.0.0.0", PORT), Proxy).serve_forever()
PYEOF

  sleep 1
  if lsof -nP -iTCP:"$proxy_port" -sTCP:LISTEN >/dev/null 2>&1; then
    echo ">>> HTTP download proxy is listening on port $proxy_port. Stop it with: ferry down"
    echo ">>> On a client run:  eval \"\$(ferry env --host $MDNS_NAME)\""
  else
    echo "WARNING: HTTP proxy did not come up; check the log: $proxy_log"
  fi
}

