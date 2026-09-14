# llm-ferry launch pack — social posts + runway checklist

Repo: https://github.com/sblattj/llm-ferry · v1.37.0 (Claude subscription lanes)
Assets available: `docs/demo.gif`, `docs/images/signal-studio-{desktop,ipad,mobile,metrics}.png`

---

## 1. Twitter/X (3 variants, all ≤280 chars)

### A — Technical hook (269 chars)
> llm-ferry: turn one Mac into an OpenAI-compatible gateway for your whole LAN. Local MLX on Apple silicon + your cloud keys behind named fallback routes, in a single-file zsh + python-stdlib CLI. MIT, keys stay on the host. https://github.com/sblattj/llm-ferry #LocalLLM
>
> **Attach:** `docs/images/signal-studio-metrics.png` (per-request latency/tokens + fallback hops — proof of the routing claim)

### B — Pain hook: keys on every laptop (276 chars)
> Your laptop drawer has API keys copied onto every machine. llm-ferry collapses that: one Mac hosts your cloud keys + local MLX models behind one OpenAI-compatible endpoint. Keys never leave the host; clients join with one curl. https://github.com/sblattj/llm-ferry #selfhosted
>
> **Attach:** `docs/demo.gif` (host up → client onboarding in one command)

### C — Subscription-OAuth hook (256 chars)
> New in llm-ferry v1.37.0: serve claude-* traffic from your Claude Pro/Max subscription via OAuth — one browser login, tokens stored 0600, auto-refresh, metered fallback when the sub runs dry. Same for ChatGPT. https://github.com/sblattj/llm-ferry #LocalLLM
>
> **Attach:** `docs/images/signal-studio-desktop.png` (route editor showing the claude-oauth lanes + fallback hops)

---

## 2. Mastodon / Bluesky (~500 chars, FOSS/selfhost crowd)

> Self-hosting your AI stack shouldn't mean pasting API keys onto every device. llm-ferry turns one Mac into a private AI gateway: a single OpenAI-compatible endpoint for the whole LAN, serving local MLX models on Apple silicon and your own cloud keys behind named fallback routes. New in v1.37.0: run Claude Pro/Max and ChatGPT *subscriptions* through it — one OAuth login on the host, tokens never leave the machine, and a metered hop catches you when the subscription runs dry. Single-file zsh + python-stdlib CLI, MIT. Built on LiteLLM + MLX. https://github.com/sblattj/llm-ferry

**Attach:** `docs/images/signal-studio-desktop.png` (and thread `signal-studio-mobile.png` for the "reroute from the couch" angle — selfhost folks love that).

---

## 3. LinkedIn (~150 words, side-project launch frame)

> Side project launch: llm-ferry 🛥️
>
> Like most developers, I had AI API keys scattered across every laptop and a powerful Mac that mostly idled. llm-ferry fixes both: it turns one Mac into a private AI gateway for the whole LAN — a single OpenAI-compatible endpoint that serves local models (MLX on Apple silicon) alongside your cloud API keys, with named fallback routes so a failed or rate-limited backend degrades to the next hop instead of erroring your tools.
>
> The newest release adds OAuth lanes for Claude Pro/Max and ChatGPT subscriptions: log in once on the host, and your subscription serves OpenAI-compatible traffic — with keys and tokens never leaving that machine. A visual route editor (Signal Studio) works from desktop, tablet, or phone.
>
> It's a single-file zsh + Python-stdlib CLI, MIT-licensed, and open source:
> https://github.com/sblattj/llm-ferry
>
> Feedback and stars welcome.

**Attach:** `docs/images/signal-studio-desktop.png`

---

## 4. Launch runway checklist (ordered)

### Pre-launch (T-minus)
1. [ ] **Pin the social-preview image** — Repo → Settings → General → Social preview: upload `docs/images/signal-studio-desktop.png` (crop target ~1280×640).
2. [ ] **Enable GitHub Discussions** (Suggestions + Q&A categories); pin an intro thread so HN/Reddit traffic has a landing spot.
3. [ ] **Double-check README renders** — verify the demo.gif autoplays on github.com, badge row renders, anchor links resolve (`#the-stack--eight-lanes-on-one-endpoint` etc.), release tag points at v1.37.0.
4. [ ] **Star-your-own-repo note** — don't star/fork from `sblattj` publicly; let stars accrue organically. Sanity-check the repo description + topics (`llm`, `self-hosted`, `mlx`, `ollama`-adjacent, `macos`, `litellm`) for search discovery.
5. [ ] Dry-run the quickstart on a clean machine/VM; confirm the one `curl | zsh` client onboarding works end-to-end.
6. [ ] Prep the HN title (no clickbait, name the thing): e.g. "Show HN: Llm-ferry – One Mac as a private AI gateway for your LAN (MLX + cloud keys, subscription OAuth)".

### Launch day (order of operations)
1. [ ] **Tue–Thu, ~8–10am ET: submit to Hacker News** (`Show HN`). Watch `newest` for the first ~30 min; respond immediately. Do not cross-post until the HN post has been live and has traction (~1–2h).
2. [ ] **Then Reddit** — r/selfhosted, r/LocalLLaMA, r/macOS (check each sub's self-promo rules; lead with the technical write-up, not the link dump).
3. [ ] **Then social** — post Twitter/X variant C (freshest news) first, then A and B spaced hours apart; Mastodon/Bluesky variant; LinkedIn last (end of day).
4. [ ] Pin the launch tweet to the `sblattj` profile; link the GitHub release in the HN/Reddit threads for changelog detail.

### Post-launch (first 48h)
1. [ ] **Reply to every comment within the hour** — HN, Reddit threads, GitHub issues/Discussions, social replies. Thank + answer + log feature requests as issues on the spot.
2. [ ] **Cross-post to Discords** (follow each server's showcase rules): Ollama, LM Studio, Open WebUI, LiteLLM, Latent Space — one short blurb + screenshot, tailored per server (LiteLLM one leads with "built on LiteLLM").
3. [ ] **Submit awesome-list PRs**: `awesome-ollama`, `awesome-llm`, `awesome-selfhosted`, `awesome-macOS`, `awesome-local-llm` style lists — one-line entry + repo link.
4. [ ] **dev.to write-up** — publish a technical post ("How llm-ferry routes OpenAI-compatible traffic across a LAN, with subscription OAuth") tagged `#llm #selfhosted #macos`; link back to the repo and HN thread.
5. [ ] After 48h: retro — capture what drove stars vs. noise, note follow-up feature requests, schedule the v1.38.0 changelog announcement.
