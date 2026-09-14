# Reddit launch drafts — llm-ferry

Repo: https://github.com/sblattj/llm-ferry · MIT · macOS/Linux · single-file zsh+python3-stdlib CLI, litellm/MLX pulled via uv on demand.

**Universal rules for all three posts**
- Post as a **text post** with the repo link in the body (not a bare link post) — most subs auto-remove naked self-links from low-karma accounts.
- Disclose authorship in the first or last line ("I built this…") — r/LocalLLaMA and r/selfhosted tolerate self-promo when it's transparent and the post carries real technical content; r/macapps is explicitly a promo-friendly sub (flair `App Release` / dev-flair where available).
- Check each sub's current rules on the day you post; many require you to have prior participation in the sub (comment a few times the week before), and r/selfhosted especially dislikes marketing tone.
- Stay in comments for ~24h; early replies are what keeps a post alive.

---

## 1 · r/LocalLLaMA

**Self-promo posture:** Community is technically tolerant of dev posts if you're the author, transparent, and deliver numbers/details. No hard "no promo" rule, but low-effort link drops get removed; text post + engagement expected. Flair "Resources" or "Discussion" as appropriate.

**Title:** I turned one Mac into an OpenAI-compatible LAN gateway for local MLX models *and* my Claude/ChatGPT subscriptions — with a KV-cache governor that cut peak GPU memory 97 GB → 56 GB

**Body:**

I got tired of my Apple-silicon Mac being the only machine that could run local models, and of my API keys living in dotfiles on every laptop — so I built **llm-ferry**: one host runs MLX models locally *and* proxies your own cloud keys, exposing a single OpenAI-compatible endpoint (`/v1/chat/completions`, `/v1/models`) for everything on the LAN. It also speaks Anthropic `/v1/messages`, so Claude Code runs on it directly.

The new v1.37.0 bit: **subscription OAuth lanes.** `ferry auth-claude login` does a browser PKCE flow and serves `claude-*` traffic from a Claude Pro/Max subscription (there's a matching ChatGPT-subscription lane). Each subscription lane carries a metered OpenRouter fallback hop, so an exhausted subscription degrades to pay-per-token instead of erroring your client.

**Honesty corner:** the subscription lane works by making requests that look like they come from the first-party coding client. That is squarely **ToS-gray** — you're using compute you paid for through an interface the provider didn't sanction. If that bothers you, the cloud-key lanes and local lanes are the boring, compliant parts. The OAuth body-cloak isn't finished yet either; headers are cloaked, the request body isn't.

Numbers from the local side (128 GB M5 Max, 121k-token agentic session): stock mlx-vlm launch kept full fp16 KV for every request in the APC prefix cache and peaked at **97 GB phys_footprint** (wired ceiling ~90–100 GB → OOM territory). The KV governor (4-bit KV + bounded APC pool + concurrent-seq cap) brings the same session to **56 GB peak / 35 GB idle**, decoding **~60% faster** (32 vs 20 tok/s at 64k context).

Stack: single-file zsh + python3-stdlib CLI; litellm/MLX arrive via uv only when you serve. Routes are named lanes with explicit fallback chains, editable in a visual editor. Repo: https://github.com/sblattj/llm-ferry — happy to answer architecture questions.

**Comment-engagement plan:**
- *"How is the OAuth lane not a ban?"* → Agree it's ToS-gray up front, describe the header-cloak mechanism at a high level, note no account has been flagged yet but present it as a personal-use risk the user accepts, not a recommendation. Don't help anyone scale it.
- *"Why not just LiteLLM proxy / Ollama?"* → It's built on litellm; the delta is the LAN job: one-curl client onboarding, MLX lane launching, named fallback chains preconfigured, key isolation. Link the honest comparison table.
- *"Benchmarks?"* → Give the KV numbers with the exact measurement conditions (model, context length, tok/s, memory) and offer the bench script in `scripts/bench-models.py`.
- *"Does it work on Linux?"* → Yes, cloud-proxy mode; MLX is Apple-silicon only.
- *"KV quantization hurts quality?"* → Explain 4-bit KV + unquantized-weights nvfp4, and that the orchestrator lane deliberately runs full KV @64k because MTP draft + quantized KV crashes (real measured trade-off, not hand-waving).

---

## 2 · r/selfhosted

**Self-promo posture:** Strictly anti-marketing; requires genuine technical content and transparent authorship. Text post strongly preferred; bare links to your own project are commonly removed unless accompanied by a write-up. Being an active commenter there first helps a lot. Frame as "here's how I self-host this," not "check out my app."

**Title:** I self-host my whole household's AI access on one Mac — one OpenAI-compatible endpoint on the LAN, API keys on exactly one box, zero SaaS

**Body:**

My pre-selfhosted setup: a drawer of API keys copied onto every laptop, a different endpoint configured in every editor, and local models trapped on the one machine with a GPU. If you run a home lab, you've probably lived some version of this.

**llm-ferry** (self-hosted, MIT, no SaaS component) collapses it into one appliance: the host Mac serves local MLX models on its own GPU *and* proxies to cloud providers behind keys that exist only on the host. Every other device — laptops, a phone, whatever — points at one OpenAI-compatible endpoint (`/v1/chat/completions`) on the LAN. Clients hold a single shared master key; the provider keys never leave the host. Client onboarding is one `curl | zsh` that wires the editor for you.

Details that matter for the selfhosted crowd:

- **Plain HTTP on your private LAN** behind the master key; cloud calls go host→provider over HTTPS with the host's keys. Nothing is exposed publicly. Remote access is via Tailscale if you want it outside the house.
- **Named lanes with explicit fallback chains** — clients ask for `heavy` or `flash`; you swap the backing models server-side without touching a client.
- **A web route editor (Signal Studio)** with live per-request metrics, desktop/tablet/phone layouts — edit fallback order from the couch, preview the YAML diff, snapshot before apply.
- It also ferries models/files between machines over the LAN and can proxy a client's downloads through the host — handy for locked-down machines.
- Single-file zsh + python3-stdlib CLI on the host; no containers, no databases, no accounts, no phone-home.

The house rule now: one box holds the secrets, everything else is a thin client. Repo + full docs: https://github.com/sblattj/llm-ferry

**Comment-engagement plan:**
- *"Isn't plain HTTP on the LAN insecure?"* → Own it: it's the documented trade-off (private-network trust boundary + shared master key), point to Tailscale and the `ferry drop`/`pickup` encrypted path for off-LAN, and say TLS on the LAN hop is a known gap, not a hidden one.
- *"Why not a container/K8s?"* → It targets one Mac with a GPU; the value is the preconfigured litellm + MLX wiring and zero-dependency client onboarding, not orchestration. Say plainly that if you want K8s, this isn't that.
- *"What about family members' devices?"* → Client bootstrap writes a scoped profile; `ferry status` for health; logs stream back to the host.
- *"Backups/upgrades?"* → Config is a YAML file; Signal Studio snapshots before writes; explain the host reset script.
- Keep replies practical and non-defensive; this sub rewards admitted limitations.

---

## 3 · r/macapps

**Self-promo posture:** Most promo-tolerant of the three — it exists for app releases — but wants: transparent dev participation, a clear "what it does / why Mac" story, screenshots or a demo, and a price/availability note (free/OSS is fine). No bare App Store-style ad spam; text post with link, and flair as a developer release if the sub's flair system offers it. (Alt target r/homelab: same body works, but lead with the "one appliance box" framing and expect more infra questions.)

**Title:** Turn your Mac mini/studio into the house AI box — one app makes it an OpenAI-compatible server for every device, running local MLX models or your cloud subscriptions

**Body:**

If you have an Apple-silicon Mac with unified memory sitting mostly idle, **llm-ferry** makes it the inference box for the whole house: it runs local models on the Mac's GPU via MLX and/or fronts your Claude Pro/Max and ChatGPT subscriptions plus API keys, then exposes one OpenAI-compatible endpoint on your LAN. Your laptop's editor, another family member's machine, a phone — they all just point at `your-mac.local` and work.

What the Mac gets to do:

- **Local MLX serving with real memory discipline** — the built-in KV-cache governor took a 121k-token session from **97 GB → 56 GB peak GPU** (35 GB idle) and made decoding **~60% faster** on a 128 GB M5 Max. It ships tuned lanes: a 27B orchestrator with speculative decoding, a cheap MoE worker lane, an HTML→JSON extraction model.
- **Subscriptions as backends (new in v1.37.0)** — one browser login per provider and your existing Claude Pro/Max or ChatGPT plan serves the house, with automatic metered fallback when a plan runs dry.
- **Signal Studio** — a built-in visual editor for fallback routes with live request metrics, designed for desktop, iPad, and phone, so you can reroute from the couch.
- **Zero-install clients** — other Macs onboard with one `curl | zsh`; keys stay on the host only.

It's a single-file zsh + Python-stdlib CLI, MIT, no accounts, no telemetry. Install is one bootstrap script that pulls MLX + litellm via uv and downloads default local models (~16.6 GB).

Repo, screenshots, and docs: https://github.com/sblattj/llm-ferry — I'm the developer, happy to answer anything.

**Comment-engagement plan:**
- *"Will it run on my 16 GB MacBook Air?"* → Be honest: it's designed for a *host* with big unified memory (48 GB+ realistically); a small Mac can still be a cloud-proxy-only host or a client.
- *"GUI?"* → Yes — Signal Studio is the browser dashboard served by the host; the CLI is for setup. Point to the desktop/tablet/phone screenshots.
- *"Subscription OAuth — will my account get banned?"* → Same honesty as LocalLLaMA: it's a gray area, personal use, your call; compliant API-key mode is the default path.
- *"Why not just Ollama/LM Studio on the Mac?"* → They're runtimes for one machine; ferry is the sharing layer across the house plus cloud lanes in the same endpoint.
- Reply fast to install issues; offer the FAQ and deep-dive docs links rather than re-explaining inline.
