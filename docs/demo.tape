# docs/demo.tape — VHS script for the llm-ferry README demo.
# Records a SAFE, secret-free, localhost-only file-ferry round-trip.
#
# The hidden setup block runs everything in a throwaway sandbox under
# /tmp/ferry-demo: a copy of the real ./ferry, fake scutil/ifconfig shims so no
# real hostname or LAN IP is shown, a fake $HOME, a neutralized shell prompt,
# and every cloud API key unset. It never touches the machine's real servers
# (no `ferry down`, no global pkill) — cleanup kills only this demo's tagged
# share server. Render with:  vhs docs/demo.tape
Output "docs/demo.gif"

Set Shell "zsh"
Set FontSize 16
Set Width 1120
Set Height 760
Set Padding 24
Set TypingSpeed 35ms

Hide
Type `rm -rf /tmp/ferry-demo; mkdir -p /tmp/ferry-demo/bin /tmp/ferry-demo/home /tmp/ferry-demo/out` Enter
Type `cp "$PWD/ferry" /tmp/ferry-demo/ferry` Enter
Type `sed -i '' 's/SHARE_PORT="8095"/SHARE_PORT="8096"/; s/ferry-share-marker/ferry-demo-share-marker/g; /ls -la/d' /tmp/ferry-demo/ferry` Enter
Type `printf '#!/bin/sh\nif [ "$1 $2" = "--get LocalHostName" ]; then echo demo-host; fi\n' > /tmp/ferry-demo/bin/scutil` Enter
Type `printf '#!/bin/sh\necho "        inet 192.168.7.20 netmask 0xffffff00 broadcast 192.168.7.255"\n' > /tmp/ferry-demo/bin/ifconfig` Enter
Type `chmod +x /tmp/ferry-demo/bin/scutil /tmp/ferry-demo/bin/ifconfig /tmp/ferry-demo/ferry` Enter
Type `export PATH="/tmp/ferry-demo/bin:$PATH"; export HOME="/tmp/ferry-demo/home"` Enter
Type `unset GEMINI_API_KEY GOOGLE_API_KEY ANTHROPIC_API_KEY OPENAI_API_KEY` Enter
Type `printf 'hi from the LAN\n' > /tmp/ferry-demo/d.txt` Enter
Type `alias ferry='/tmp/ferry-demo/ferry'` Enter
Type `cd /tmp/ferry-demo` Enter
Type `precmd_functions=(); preexec_functions=(); PROMPT='ferry $ '; RPROMPT=''` Enter
Type `clear` Enter
Show

Sleep 500ms
Type "ferry --help" Enter
Sleep 3.5s
Type "clear" Enter
Sleep 400ms

Type "printf 'hi from the LAN\n' > d.txt" Enter
Sleep 700ms
Type "ferry offer d.txt" Enter
Sleep 2s

Type "ferry share" Enter
Sleep 2.5s

Type "ferry get d.txt --host 127.0.0.1 --to out" Enter
Sleep 2.5s

Type "cat out/d.txt" Enter
Sleep 2.5s

Hide
Type `pkill -f ferry-demo-share-marker` Enter
Sleep 300ms
