# Show HN: llm-ferry — launch drafts

## Candidate titles

1. **Show HN: Llm-Ferry – Turn a Mac into an OpenAI-compatible gateway for your LAN** (recommended)
2. Show HN: Llm-Ferry – One Mac serves local MLX and your cloud keys to the whole LAN
3. Show HN: Llm-Ferry – A LAN AI gateway that also runs your Claude/ChatGPT subscriptions

---

## Post body

I built llm-ferry: a macOS (and Linux) gateway that turns one machine into a single OpenAI-compatible endpoint (`/v1/chat/completions`, `/v1/models`, plus `/v1/messages` for Claude Code) for your LAN. It serves local models with MLX on Apple silicon and proxies to cloud providers using the host's own API keys — keys live on one machine, and every laptop or agent points at the gateway with one shared key. I built it because I had provider keys copied across four machines and a fast Mac none of them could use.

New in v1.37.0: you can serve `claude-*` traffic from a Claude Pro/Max subscription instead of pay-per-token billing (ChatGPT subscriptions worked already). `ferry auth-claude login` runs a PKCE OAuth flow through a localhost callback, stores the token at 0600, and refreshes proactively and on 401s, persisting the rotated refresh token. The client headers mimic Claude Code's. To be direct: this lane is reverse-engineered, undocumented, personal-use, and arguably against the provider's ToS — it's for experimenting on your own account, not a product. Each subscription lane carries a metered fallback hop so an exhausted subscription degrades to pay-per-token instead of erroring.

Other bits: a single-file CLI in zsh + python3 stdlib (litellm and MLX load via uv only when you serve inference), named routes with strict fallback chains, a visual route editor (Signal Studio) that previews the YAML diff, and a KV-cache governor that cut peak GPU memory roughly 40% on a 121k-token session. Repo: https://github.com/sblattj/llm-ferry — feedback welcome, especially on the ToS question and fallback-routing semantics.

---

## First comment (author seeds discussion)

The pain that started this: I kept a `.env` of provider keys in sync across four machines and still couldn't point a laptop at the big Mac's GPU. The first version was just a litellm config wrapped in zsh; the subscription-OAuth lane came later, after I noticed my Claude Pro quota sat idle while my Anthropic API bill grew. Reverse-engineering the OAuth flow was the most fun part — PKCE with a one-shot localhost callback, and the refresh trap where a stock litellm provider never refreshes on a server-side 401, so a session dies mid-task.

What's next, in rough order: wiring the request-body cloak (currently headers only), a cleaner story for multi-user keys on one host, and probably a headless Linux install that doesn't assume macOS paths. If you run a home lab, I'd genuinely like to hear what's missing from the client-onboarding flow.
