#!/usr/bin/env python3
"""bench-models.py — A/B two model surfaces for a ferry lane, honestly.

Implements the rules in .claude/skills/a-b-testing-models/SKILL.md:
  - generous token budgets (reasoning eats max_tokens — trap 1)
  - stream_options.include_usage (traps 2)
  - per-arm thinking knobs, thoughts VERIFIED from the response (traps 3-4)
  - text-TTFT vs reasoning-TTFT recorded separately (trap 5)
  - decode tok/s only claimed when deltas look per-token; e2e tok/s always (trap 6)
  - temperature 0 + seed 0, interleaved rounds, medians (trap 8)

Stdlib only. Keys come from the environment (source ~/.config/ferry/secrets.env).
Edit ARMS below to the two configurations being compared.
"""
import argparse
import json
import os
import statistics
import sys
import time
import urllib.request

PROMPTS = {
    "chat": "Write a paragraph of about 150 words explaining what a DNS resolver does. Plain text.",
    "code": "Write a complete Python function binary_insert(sorted_list, item) with a docstring and inline comments. Plain code only.",
    "tool": None,  # skipped in prose bench; run a get_weather probe separately (trap 9)
}
MAX_TOKENS = 1024   # thinking models burn budget on thoughts first (trap 1)

# ---- Arms: edit these to the surfaces under comparison ----------------------
# Every entry below hits a NAMED FERRY LANE through the ferry front (default
# :8090), not a raw provider — each lane's actual backend model is noted in its
# comment so a stale line here is easy to catch against ~/.config/ferry/litellm.yaml.
# Active by default: `flash` against its own fallback hop `flash-luna` — the
# pair most worth A/B'ing, since the hop only carries load when `flash` errors.
#
# `heavy`/`orch`/`orchestrator` ride the ChatGPT SUBSCRIPTION and are
# STREAMING-ONLY (litellm's chat->responses bridge 500s a non-streamed call:
# "Unknown items in responses API response: []"). This harness already sends
# `"stream": True` on every arm (see run_openai below), so it benches those
# lanes cleanly without a separate non-streaming probe.
ARMS = {
    "flash": {
        "kind": "openai",
        "url": "http://127.0.0.1:8090/v1/chat/completions",
        "key_env": None,   # set to "LITELLM_MASTER_KEY" if the front is keyed
        "model": "flash",          # openrouter/google/gemini-3.8-flash
        "extra": {},
    },
    "flash-luna": {
        "kind": "openai",
        "url": "http://127.0.0.1:8090/v1/chat/completions",
        "key_env": None,
        "model": "flash-luna",     # openrouter/openai/gpt-5.6-luna, effort high
        "extra": {},
    },
    # Every other current lane, same shape — uncomment to swap into the A/B:
    # "heavy": {          # chatgpt/responses/gpt-6-astra, ChatGPT subscription
    #     "kind": "openai", "url": "http://127.0.0.1:8090/v1/chat/completions",
    #     "key_env": None, "model": "heavy", "extra": {},
    # },
    # "super-flash": {    # same Gemini shape as `flash`, minimal reasoning
    #     "kind": "openai", "url": "http://127.0.0.1:8090/v1/chat/completions",
    #     "key_env": None, "model": "super-flash", "extra": {},
    # },
    # "super-flash-luna": {  # same Luna shape as `flash-luna`, reasoning off
    #     "kind": "openai", "url": "http://127.0.0.1:8090/v1/chat/completions",
    #     "key_env": None, "model": "super-flash-luna", "extra": {},
    # },
    # "local-orch": {     # Qwen 3.8-27B nvfp4 on the host GPU (mlx_vlm :8092)
    #     "kind": "openai", "url": "http://127.0.0.1:8090/v1/chat/completions",
    #     "key_env": None, "model": "local-orch", "extra": {},
    # },
    # "local-sub": {      # Nemotron 3 Nano 30B A3B nvfp4 on the host GPU (:8093)
    #     "kind": "openai", "url": "http://127.0.0.1:8090/v1/chat/completions",
    #     "key_env": None, "model": "local-sub", "extra": {},
    # },
    #
    # Direct-provider arms (bypass ferry, hit the CURRENT upstream providers —
    # the only two ferry itself rides today):
    # "gemini-flash-or": {
    #     "kind": "openai", "url": "https://openrouter.ai/api/v1/chat/completions",
    #     "key_env": "OPENROUTER_API_KEY", "model": "google/gemini-3.8-flash",
    #     "extra": {},
    # },
    # "luna-or": {
    #     "kind": "openai", "url": "https://openrouter.ai/api/v1/chat/completions",
    #     "key_env": "OPENROUTER_API_KEY", "model": "openai/gpt-5.6-luna",
    #     "extra": {},
    # },
}
# ------------------------------------------------------------------------------


def run_openai(arm, prompt):
    body = {
        "model": arm["model"],
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": MAX_TOKENS, "temperature": 0, "seed": 0,
        "stream": True, "stream_options": {"include_usage": True},  # trap 2
    }
    body.update(arm["extra"])
    headers = {"Content-Type": "application/json"}
    if arm.get("key_env"):
        key = os.environ.get(arm["key_env"])
        if not key:
            raise SystemExit(f"export {arm['key_env']} first")
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(arm["url"], data=json.dumps(body).encode(), headers=headers)
    return _consume_sse(req)


def run_gemini(arm, prompt):
    key = os.environ.get(arm["key_env"])
    if not key:
        raise SystemExit(f"export {arm['key_env']} first")
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{arm['model']}:streamGenerateContent?alt=sse&key={key}")
    gc = {"maxOutputTokens": MAX_TOKENS, "temperature": 0, "seed": 0}
    gc.update(arm["extra"])
    body = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": gc}

    # Gemini SSE: parts carry .text (text) or .thought=True (thinking)
    t0 = time.perf_counter(); t_text = t_thought = t_last = None
    deltas = 0; text_tok = reason_tok = None
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    # Gemini-native has no litellm in the path; keep the key present so the
    # summary code can treat every arm uniformly (None = not measured).
    overhead = None
    with urllib.request.urlopen(req, timeout=300) as r:
        for line in r:
            if not line.startswith(b"data: "):
                continue
            d = json.loads(line[6:])
            now = time.perf_counter()
            for part in ((d.get("candidates") or [{}])[0].get("content") or {}).get("parts") or []:
                if part.get("text"):
                    if part.get("thought"):
                        t_thought = t_thought or now
                    else:
                        t_text = t_text or now
                        deltas += 1
                    t_last = now
            um = d.get("usageMetadata") or {}
            if um.get("candidatesTokenCount") is not None:
                text_tok = um.get("candidatesTokenCount")
                reason_tok = um.get("thoughtsTokenCount", 0)  # trap 4: verify, don't trust the knob
    result = _finish(t0, t_text, t_thought, t_last, deltas, text_tok, reason_tok)
    result["proxy_overhead_ms"] = None
    return result


def _consume_sse(req):
    t0 = time.perf_counter(); t_text = t_thought = t_last = None
    deltas = 0; text_tok = reason_tok = None
    with urllib.request.urlopen(req, timeout=300) as r:
        # litellm proxies stamp this on every response: the proxy's own added
        # latency. Present only on ferry-lane arms (direct APIs have no such
        # header) — None means "not measured", never "zero".
        overhead = r.headers.get("x-litellm-overhead-duration-ms")
        for line in r:
            if not line.startswith(b"data: "):
                continue
            p = line[6:].strip()
            if p == b"[DONE]":
                break
            try:
                d = json.loads(p)
            except json.JSONDecodeError:
                continue
            now = time.perf_counter()
            if d.get("usage") and d["usage"].get("completion_tokens") is not None:
                text_tok = d["usage"]["completion_tokens"]
                reason_tok = (d["usage"].get("completion_tokens_details") or {}).get("reasoning_tokens", 0)
                continue
            delta = ((d.get("choices") or [{}])[0].get("delta") or {})
            if delta.get("content"):
                t_text = t_text or now; deltas += 1; t_last = now
            elif delta.get("reasoning_content") or delta.get("reasoning"):
                t_thought = t_thought or now
    result = _finish(t0, t_text, t_thought, t_last, deltas, text_tok, reason_tok)
    result["proxy_overhead_ms"] = float(overhead) if overhead else None
    return result


def _finish(t0, t_text, t_thought, t_last, deltas, text_tok, reason_tok):
    if text_tok is None:
        raise RuntimeError("no usage received — stream_options.include_usage missing? (trap 2)")
    per_token = deltas >= max(2, text_tok * 0.5)  # trap 6: deltas must cover most tokens
    decode = (text_tok - 1) / (t_last - t_text) if (t_last and t_text and t_last > t_text and per_token) else None
    return {
        "reason_ttft": (t_thought - t0) if t_thought else None,
        "text_ttft": (t_text - t0) if t_text else None,
        "e2e": time.perf_counter() - t0,
        "text_tok": text_tok, "reason_tok": reason_tok or 0,
        "decode_tok_s": decode,                       # None = chunk-burst, unmeasurable
        "e2e_tok_s": text_tok / max(time.perf_counter() - t0, 1e-9),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--gap", type=float, default=1.0)
    args = ap.parse_args()

    runners = {"openai": run_openai, "gemini": run_gemini}
    prompts = {k: v for k, v in PROMPTS.items() if v}
    results = {a: {p: [] for p in prompts} for a in ARMS}
    order = list(ARMS)
    for rnd in range(args.rounds):
        arms = order if rnd % 2 == 0 else order[::-1]  # interleave (trap 8)
        for name in arms:
            for pname, prompt in prompts.items():
                try:
                    r = runners[ARMS[name]["kind"]](ARMS[name], prompt)
                except RuntimeError as e:
                    print(f"r{rnd+1} {name:14s} {pname:5s} ERROR {e}", flush=True)
                    time.sleep(3)
                    r = runners[ARMS[name]["kind"]](ARMS[name], prompt)
                results[name][pname].append(r)
                dec = f"{r['decode_tok_s']:6.1f}" if r['decode_tok_s'] else "  n/a "
                ov = f" proxy {r['proxy_overhead_ms']:.0f}ms" if r.get("proxy_overhead_ms") is not None else ""
                print(f"r{rnd+1} {name:14s} {pname:5s} textTTFT "
                      f"{(r['text_ttft'] or 0):5.2f}s e2e {r['e2e']:6.2f}s "
                      f"text {r['text_tok']:4d} think {r['reason_tok']:4d} "
                      f"decode {dec} e2etok/s {r['e2e_tok_s']:5.1f}{ov}", flush=True)
                time.sleep(args.gap)

    print("\n=== MEDIANS ===")
    for name in ARMS:
        allr = [r for runs in results[name].values() for r in runs]
        if not allr:
            continue
        ttft = statistics.median(r["text_ttft"] for r in allr if r["text_ttft"])
        e2e = statistics.median(r["e2e"] for r in allr)
        dec = [r["decode_tok_s"] for r in allr if r["decode_tok_s"]]
        dec_s = f"{statistics.median(dec):6.1f}" if dec else "  n/a "
        ovs = [r["proxy_overhead_ms"] for r in allr if r.get("proxy_overhead_ms") is not None]
        ov_s = f"  proxy-median {statistics.median(ovs):5.1f}ms ({len(ovs)}/{len(allr)})" if ovs else ""
        print(f"{name:14s} textTTFT {ttft:5.2f}s  e2e {e2e:6.2f}s  "
              f"decode {dec_s} tok/s ({len(dec)}/{len(allr)} measurable)  "
              f"e2etok/s {statistics.median(r['e2e_tok_s'] for r in allr):5.1f}  "
              f"think-median {statistics.median(r['reason_tok'] for r in allr):.0f}{ov_s}")
    print("\nNOTE: think-median > 0 on an arm configured 'thinking off' = knob ignored (trap 4).")
    print("NOTE: proxy-median = litellm's x-litellm-overhead-duration-ms; present only on arms")
    print("      served through a litellm proxy (ferry lanes). It is the proxy's own added latency")
    print("      — compare arms by it, and watch it drop when workers go up.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
