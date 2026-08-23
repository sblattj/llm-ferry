# docs/demo.tape — VHS script for the llm-ferry README demo.
# Shows the HERO story: one Mac serves an OpenAI-compatible AI gateway to the LAN.
#
# 100% SAFE + honest + offline:
#  - Runs a REAL litellm proxy, but via docs/demo-litellm.yaml whose models use
#    `mock_response` — so ZERO API keys and ZERO network calls; the endpoint and the
#    /v1/chat/completions round-trip are genuine, the replies are canned.
#  - Everything runs in a throwaway /tmp/ferry-demo sandbox: a copy of ./ferry, fake
#    scutil/ifconfig shims (no real hostname/LAN IP), a fake $HOME, a neutral prompt,
#    and EVERY real API key unset from the recording env.
#  - Binds a FREE port (8190), never 8090 — it never touches the machine's real proxy.
#    Cleanup pkills only this demo (matched on the unique `ferry-demo` path).
# Render with:  vhs docs/demo.tape
Output "docs/demo.gif"

Set Shell "zsh"
Set FontSize 15
Set Width 1240
Set Height 720
Set Padding 20
Set TypingSpeed 28ms

Hide
Type `rm -rf /tmp/ferry-demo; mkdir -p /tmp/ferry-demo/bin /tmp/ferry-demo/home/.config/ferry` Enter
Type `cp "$PWD/ferry" /tmp/ferry-demo/ferry` Enter
Type `cp "$PWD/docs/demo-litellm.yaml" /tmp/ferry-demo/home/.config/ferry/litellm.yaml` Enter
Type `printf '#!/bin/sh\nif [ "$1 $2" = "--get LocalHostName" ]; then echo demo-host; fi\n' > /tmp/ferry-demo/bin/scutil` Enter
Type `printf '#!/bin/sh\necho "        inet 192.168.7.20 netmask 0xffffff00 broadcast 192.168.7.255"\n' > /tmp/ferry-demo/bin/ifconfig` Enter
Type `chmod +x /tmp/ferry-demo/bin/scutil /tmp/ferry-demo/bin/ifconfig /tmp/ferry-demo/ferry` Enter
Type `export PATH="/tmp/ferry-demo/bin:$PATH"; export HOME=/tmp/ferry-demo/home` Enter
Type `for v in $(env | grep -iE "API_KEY|_TOKEN|SECRET" | cut -d= -f1); do unset "$v"; done` Enter
Type `export GEMINI_API_KEY=demo GEMINI_API_KEY_2=demo KIMI_API_KEY=demo` Enter
Type `alias ferry=/tmp/ferry-demo/ferry` Enter
Type `cd /tmp/ferry-demo` Enter
Type `precmd_functions=(); preexec_functions=(); PROMPT='ferry $ '; RPROMPT=''` Enter
Type `setopt interactive_comments` Enter
Type `clear` Enter
Show

Sleep 800ms
Type `# One Mac -> an OpenAI-compatible AI gateway for your whole LAN.` Enter
Sleep 1500ms

Type "ferry up --route --port 8190" Enter
Sleep 3500ms

Type `curl -s --retry 30 --retry-connrefused http://127.0.0.1:8190/v1/models | python3 -c "import sys,json;print('serving:', ', '.join(m['id'] for m in json.load(sys.stdin)['data']))"` Enter
Sleep 6500ms

Type `curl -s http://127.0.0.1:8190/v1/chat/completions -H "Content-Type: application/json" -d '{"model":"orchestrator","messages":[{"role":"user","content":"who are you?"}]}' | python3 -c "import sys,json;print(json.load(sys.stdin)['choices'][0]['message']['content'])"` Enter
Sleep 4500ms

Type `# Any laptop, editor, or device on the LAN points here. The keys stay on the host.` Enter
Sleep 3000ms

Hide
Type `pkill -f ferry-demo` Enter
Sleep 500ms
