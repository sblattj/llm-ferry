#!/bin/zsh
# status.sh — Diagnoses LAN reachability and model server status.
# Prints exact URLs and configurations for other laptops to connect.

set -eu

PORT="${1:-8090}"
MDNS_NAME="$(scutil --get LocalHostName 2>/dev/null | tr 'A-Z' 'a-z').local"
LAN_IP=$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo "Unknown-IP")

echo "================================================================="
echo "               LAN INFERENCE HOST DIAGNOSTICS"
echo "================================================================="
echo "Host Machine mDNS:   http://$MDNS_NAME:$PORT"
echo "Host Machine LAN IP: http://$LAN_IP:$PORT"
echo "================================================================="

# Check if server is running on the target port
if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  PID=$(lsof -t -iTCP:"$PORT" -sTCP:LISTEN)
  echo ">>> STATUS: ONLINE (PID: $PID)"
  
  # Fetch loaded models list from the active server
  echo ">>> Querying active models from server..."
  MODELS=$(curl -fsS -m 3 "http://127.0.0.1:$PORT/v1/models" 2>/dev/null || echo "")
  
  if [[ -n "$MODELS" ]]; then
    ACTIVE_MODEL=$(echo "$MODELS" | python3 -c "import json,sys; d=json.load(sys.stdin).get('data',[]); print(d[0]['id'] if d else 'None')")
    echo "    Active Model loaded: \033[1;32m$ACTIVE_MODEL\033[0m"
    echo "================================================================="
    echo ">>> TEST COMMAND FOR OTHER LAPTOPS (Copy-paste this on any client):"
    echo "    curl -fsS -H \"Content-Type: application/json\" \\"
    echo "      -d '{\"model\":\"$ACTIVE_MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"Say Hello!\"}],\"max_tokens\":10}' \\"
    echo "      http://$MDNS_NAME:$PORT/v1/chat/completions"
  else
    echo "    \033[1;31mWARNING: Server is running but returned no loaded models.\033[0m"
    echo "    Check host server startup logs."
  fi
else
  echo ">>> STATUS: \033[1;31mOFFLINE\033[0m (Nothing is listening on port $PORT)"
  echo "    To start the server: ./host-serve.sh -p $PORT"
fi
echo "================================================================="
