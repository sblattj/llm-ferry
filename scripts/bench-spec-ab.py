#!/usr/bin/env python3
"""A/B benchmark: current MTP-drafter lane (mlx_vlm :8092) vs mlx-dspark DFlash 2 (:8094).

Same target model (mlx-community/Qwen3.8-27B-nvfp4), same prompts, greedy decoding,
thinking disabled on both arms. Interleaved rounds, medians reported.
"""
import json
import statistics
import sys
import time
import urllib.request

PROMPTS = {
    "chat": "Explain how rainbows form, from physics first principles to the full arc. Cover refraction, dispersion, total internal reflection, and why the primary arc is about 42 degrees. Write about 350 words.",
    "code": "Write a complete Python module implementing an LRU cache class with get, put, delete, and a __repr__ showing eviction order. Include type hints, docstrings, and a demo under __main__. Do not use functools.lru_cache or OrderedDict.move_to_end.",
    "math": "A water tank has two inlet pipes and one outlet pipe. Pipe A fills it in 6 hours, pipe B in 4 hours, and the outlet drains a full tank in 9 hours. If all three are open starting from empty, how long until the tank is full? Show every step and the exact fraction.",
}

ARMS = {
    "A-mtp": {
        "port": 8092,
        "model": "mlx-community/Qwen3.8-27B-nvfp4",
        "extra": {},
        "single_token_chunks": True,
    },
    "B-dflash": {
        "port": 8094,
        "model": "Qwen3.8-27B-nvfp4",
        "extra": {"enable_thinking": False},
        "single_token_chunks": False,
    },
}

MAX_TOKENS = 512
ROUNDS = 3
GAP_S = 2.0


def one_request(arm, prompt):
    body = {
        "model": arm["model"],
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": MAX_TOKENS,
        "temperature": 0,
        "seed": 0,
        "stream": True,
    }
    body.update(arm["extra"])
    req = urllib.request.Request(
        f"http://127.0.0.1:{arm['port']}/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    t_start = time.perf_counter()
    t_first = None
    t_last = None
    tokens = 0
    meta = {}
    with urllib.request.urlopen(req, timeout=600) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            try:
                chunk = json.loads(line[6:])
            except json.JSONDecodeError:
                continue
            now = time.perf_counter()
            choices = chunk.get("choices") or []
            delta = choices[0].get("delta", {}) if choices else {}
            content = delta.get("content")
            if content:
                if t_first is None:
                    t_first = now
                t_last = now
                if arm["single_token_chunks"]:
                    tokens += 1
            if chunk.get("usage") and chunk["usage"].get("completion_tokens"):
                tokens = chunk["usage"]["completion_tokens"]
            if chunk.get("x_mlx_dspark"):
                meta = chunk["x_mlx_dspark"]
                if meta.get("completion_tokens"):
                    tokens = meta["completion_tokens"]
    if t_first is None:
        raise RuntimeError("no content tokens received")
    return {
        "ttft_s": t_first - t_start,
        "decode_s": t_last - t_first,
        "e2e_s": t_last - t_start,
        "tokens": tokens,
        "decode_tok_s": (tokens - 1) / (t_last - t_first) if t_last > t_first else 0.0,
        "e2e_tok_s": tokens / (t_last - t_start),
        "dspark": meta,
    }


def main():
    results = {arm: {p: [] for p in PROMPTS} for arm in ARMS}
    order = list(ARMS)
    for rnd in range(ROUNDS):
        arms_this_round = order if rnd % 2 == 0 else order[::-1]
        for arm_name in arms_this_round:
            for pname, prompt in PROMPTS.items():
                try:
                    r = one_request(ARMS[arm_name], prompt)
                except RuntimeError:
                    time.sleep(3)
                    r = one_request(ARMS[arm_name], prompt)
                r["round"] = rnd + 1
                results[arm_name][pname].append(r)
                ds = r["dspark"]
                extra = f" accept={ds['accept_len']}" if ds.get("accept_len") else ""
                print(
                    f"r{rnd+1} {arm_name:9s} {pname:5s} {r['tokens']:4d} tok "
                    f"decode {r['decode_tok_s']:6.1f} tok/s  e2e {r['e2e_tok_s']:6.1f}  "
                    f"ttft {r['ttft_s']:5.2f}s{extra}",
                    flush=True,
                )
                time.sleep(GAP_S)

    print("\n=== MEDIANS ===")
    summary = {}
    for arm_name in ARMS:
        summary[arm_name] = {}
        for pname in PROMPTS:
            runs = results[arm_name][pname]
            summary[arm_name][pname] = {
                "decode_tok_s": statistics.median(r["decode_tok_s"] for r in runs),
                "e2e_tok_s": statistics.median(r["e2e_tok_s"] for r in runs),
                "ttft_s": statistics.median(r["ttft_s"] for r in runs),
                "tokens": statistics.median(r["tokens"] for r in runs),
                "accept_len": (
                    statistics.median(
                        [r["dspark"]["accept_len"] for r in runs if r["dspark"].get("accept_len")]
                    )
                    if any(r["dspark"].get("accept_len") for r in runs)
                    else None
                ),
            }
        all_decode = [r["decode_tok_s"] for runs in results[arm_name].values() for r in runs]
        all_ttft = [r["ttft_s"] for runs in results[arm_name].values() for r in runs]
        summary[arm_name]["OVERALL"] = {
            "decode_tok_s_med": statistics.median(all_decode),
            "decode_tok_s_mean": statistics.mean(all_decode),
            "ttft_s_med": statistics.median(all_ttft),
        }
        for pname in list(PROMPTS) + ["OVERALL"]:
            s = summary[arm_name][pname]
            if pname == "OVERALL":
                print(
                    f"{arm_name:9s} OVERALL   decode {s['decode_tok_s_med']:6.1f} tok/s (mean {s['decode_tok_s_mean']:6.1f})  ttft {s['ttft_s_med']:5.2f}s"
                )
            else:
                acc = f"  accept {s['accept_len']:.2f}" if s["accept_len"] else ""
                print(
                    f"{arm_name:9s} {pname:9s} decode {s['decode_tok_s']:6.1f} tok/s  e2e {s['e2e_tok_s']:6.1f}  ttft {s['ttft_s']:5.2f}s{acc}"
                )

    with open("/tmp/spec-ab-results.json", "w") as f:
        json.dump({"summary": summary, "raw": results}, f, indent=2)
    print("\nsaved /tmp/spec-ab-results.json")


if __name__ == "__main__":
    sys.exit(main())
