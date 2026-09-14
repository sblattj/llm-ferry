# llm-ferry — awesome-list submission drafts (READ-ONLY; no PRs opened)

Facts source: `~/code/llm-ferry/README.md` + `LICENSE` (MIT, © 2026 Stephen Blatt).
Repo: https://github.com/sblattj/llm-ferry — macOS/Linux, single-file CLI (zsh + python3 stdlib),
fronts 3 local MLX (`mlx_vlm.server`) lanes + cloud providers behind a LiteLLM proxy on one
OpenAI-compatible endpoint (`:8090`), with named fallback chains, subscription-OAuth lanes
(`ferry auth-claude login`), LAN client onboarding (`curl | zsh`), Signal Studio dashboards.

## Canonical 1-line description

> Turn a Mac into a private AI gateway — one OpenAI-compatible LAN endpoint for local MLX models and the host's own cloud API keys, with fallback routing and one-command client onboarding.

Per-list variants below are trimmed/retuned to each list's format rules; all end with a period
except rafska's, whose siblings omit trailing periods.

---

## 1. awesome-selfhosted/awesome-selfhosted

**Verdict: STRONG FIT — submit.** Free (MIT) license ✓, self-hostable network service ✓ (it *is* a
server on your own machine), and there is a direct precedent sibling: **GoModel**, an AI gateway
with a unified OpenAI-compatible API. LocalAI, Ollama, LLM Harbor also live here.

**(b) Section:** `### Generative Artificial Intelligence (GenAI)`

**(a) Exact markdown line** — rendered form; insert alphabetically between **LLM Harbor** and
**LLMKube** (`llm-ferry` sorts after `LLM Harbor` and before `LLMKube`):

Sibling template (GoModel):
```
- [GoModel](https://gomodel.enterpilot.io/) - AI gateway written in Go with a unified OpenAI-compatible API for multiple LLM providers, USD cost tracking, budgets, usage analytics, guardrails, caching, and an admin dashboard. ([Source Code](https://github.com/ENTERPILOT/GoModel)) `MIT` `Go/Docker`
```

Proposed entry:
```
- [llm-ferry](https://github.com/sblattj/llm-ferry) - Turn a Mac into an AI gateway for the LAN: local MLX models and the host's own cloud API keys behind one OpenAI-compatible endpoint, with fallback routing. `MIT` `Shell/Python`
```

**(c) Contribution requirements (verified):** The README is *generated* from
**awesome-selfhosted/awesome-selfhosted-data** — you do NOT edit the README. Add a new
`software/llm-ferry.yml` (kebab-case) in that repo, based on
`.github/ISSUE_TEMPLATE/addition.md`; commit message `add llm-ferry`; select "Create a new
branch for this commit and start a pull request". The repo's CONTRIBUTING adds:
- Avoid redundant terms in descriptions: *open-source, free, self-hosted…* (already implied).
- Prefer short forms — not "A tool that…" or "$PROJECT is a…".
- Licenses must be FOSS, SPDX identifier (MIT ✓, already in `licenses.yml`).
- In single-page mode only the first `tags` entry shows — put `Generative Artificial Intelligence (GenAI)` first.
- **"Machine/LLM-generated contributions, that do not respect project guidelines are not allowed and will result in a ban"** — review/finalize the wording in your own voice; keep it within the rules above.

YAML skeleton (fields after `archived` look maintainer/bot-maintained — fill best-effort or let
their CI set them; modeled on `software/gomodel.yml`):
```yaml
name: llm-ferry
website_url: https://github.com/sblattj/llm-ferry
source_code_url: https://github.com/sblattj/llm-ferry
description: Turn a Mac into an AI gateway for the LAN: local MLX models and the host's own cloud API keys behind one OpenAI-compatible endpoint, with fallback routing.
licenses:
  - MIT
platforms:
  - Shell
  - Python
tags:
  - Generative Artificial Intelligence (GenAI)
```
(`Shell` and `Python` are existing platform tags; LLM Harbor is listed as `Docker/Shell`, so a
shell-run tool without Docker is accepted.)

---

## 2. rafska/awesome-local-llm

**Verdict: STRONG FIT — submit.** The list is exactly this domain; nearest siblings
LocalAI / jan / lemonade are the same shape (serve local models behind one endpoint).

**(b) Section:** `## Inference platforms` (top-level, before "Inference engines").

**(a) Exact markdown line** — siblings carry a stars badge; descriptions start lowercase and omit
the trailing period; the list is not strictly alphabetized (LM Studio, unsloth, LocalAI, jan,
ChatBox, lemonade).

Sibling template:
```
- <img src="https://img.shields.io/github/stars/mudler/LocalAI?style=social" height="17" align="texttop"/> [LocalAI](https://github.com/mudler/LocalAI) -  the free, open-source alternative to OpenAI, Claude and others
```

Proposed entry:
```
- <img src="https://img.shields.io/github/stars/sblattj/llm-ferry?style=social" height="17" align="texttop"/> [llm-ferry](https://github.com/sblattj/llm-ferry) - turn a Mac into a private AI gateway — local MLX models and the host's own cloud API keys behind one OpenAI-compatible LAN endpoint, with fallback routing
```

**(c) Contribution requirements (verified):** Minimal — CONTRIBUTING.md just says fork, branch,
edit README.md, clear commit message, PR "and describe your addition". No license tag, no PR
template, no ordering rule documented. One link per PR is the norm here in practice; keep it to a
single line.

---

## 3. EndoTheDev/Awesome-Ollama

**Verdict: NOT A FIT — do not submit.**

Evidence: llm-ferry's local lanes run `mlx_vlm.server` processes fronted by LiteLLM; Ollama
appears in llm-ferry's README only inside the *comparison table* ("Per-device API keys |
Ollama/LM Studio | Raw LiteLLM proxy | OpenRouter | llm-ferry") as a different tool, and in no
integration, config, or command. There is no `ollama` provider lane. CONTRIBUTING.md requires
"Entries should be actively maintained and **useful to Ollama users**" and bans mentioning Ollama
in the description — with no interop, there is no user story, and the list's sections (Coding
Agents, Assistants, IDEs, Chat & RAG, Mobile, Automation, Notebooks, Terminal, Eval, Package
Manager, Libraries) have no serving/gateway category that llm-ferry would slot into anyway.

If a genuine Ollama provider lane ever ships, the format would be a table row:
`| [llm-ferry](https://github.com/sblattj/llm-ferry) | description | local-server |`
(one link per PR; description without the word "Ollama"). Until then: skip.

---

## 4. jaywcjlove/awesome-mac

**Verdict: MODERATE FIT — optional submit, with caveats.** The main `README.md` lists GUI apps
only; **CLI apps have a dedicated `command-line-apps.md`** (Databases / Media / Developer /
Other), so a CLI *is* acceptable — but it skews consumer/app-store flavored, and llm-ferry would
be near-alone as an AI-gateway entry. Target `command-line-apps.md`, **Developer** section,
alphabetically between "JSON Schema CLI" and "mdctl".

**(a) Exact markdown line** — sibling template:
```
* [httpie](https://httpie.org) - Modern command line HTTP client. [![Open-Source Software][OSS Icon]](https://github.com/jakubroztocil/httpie) ![Freeware][Freeware Icon]
```

Proposed entry:
```
* [llm-ferry](https://github.com/sblattj/llm-ferry) - Turn a Mac into a private AI gateway serving local MLX models and the host's own cloud API keys through one OpenAI-compatible LAN endpoint. [![Open-Source Software][OSS Icon]](https://github.com/sblattj/llm-ferry) ![Freeware][Freeware Icon]
```

**(c) Contribution requirements (verified, docs/CONTRIBUTING.md):** one PR per suggestion;
`[Name](link)` title-cased (AP style) — existing brand entries keep native casing (mycli,
httpie), so `llm-ferry` stays as-is; alphabetical order within the category; one-sentence
descriptions; **entries must be synced across all four language files**
(`command-line-apps.md`, `-zh`, `-ja`, `-ko`) — the repo ships a `$awesome-mac-maintainer` Codex
skill for exactly this, and a manual PR is expected to update all four; search for duplicates
first.

---

## 5. Hannibal046/Awesome-LLM

**Verdict: MODERATE FIT — submit, into the collapsed sub-block.** "LLM Inference" is the
serving/deployment section, but its *top-level* entries are inference engines (SGLang, vLLM,
llama.cpp, ollama, TGI, TensorRT-LLM). Gateways/deployment systems (FastChat, OpenLLM, SkyPilot)
live in the `<details><summary>other deployment tools</summary>` block — that is llm-ferry's
honest slot. (Precedent for gateways under "LLM Applications" too: Portkey "AI Gateway"; the
details block is the closer match.)

**(b) Section:** `## LLM Inference` → inside `<details><summary>other deployment tools</summary>`.

**(a) Exact markdown line** — sibling template:
```
- [FastChat](https://github.com/lm-sys/FastChat) - A distributed multi-model LLM serving system with web UI and OpenAI-compatible RESTful APIs.
```

Proposed entry:
```
- [llm-ferry](https://github.com/sblattj/llm-ferry) - A single-file gateway that serves a Mac's local MLX models and the host's own cloud API keys through one OpenAI-compatible LAN endpoint, with named fallback routes.
```

**(c) Contribution requirements (verified, contributing.md):** "Please follow the existing format
to add new terms" — that is the only rule touching tools (the rest covers paper chronology and
leaderboard sorting). No PR template found in the repo; not alphabetized — place near FastChat /
OpenLLM.

---

## Submit order (by expected friction → payoff)

1. **awesome-selfhosted** (yml in awesome-selfhosted-data) — highest reach, strictest rules; hand-finish the description yourself, mind the LLM-content ban.
2. **rafska/awesome-local-llm** — one line, trivial PR.
3. **Hannibal046/Awesome-LLM** — one line, details block.
4. **jaywcjlove/awesome-mac** — optional; requires syncing 4 language files.
5. **EndoTheDev/Awesome-Ollama** — skip (no Ollama interop).
