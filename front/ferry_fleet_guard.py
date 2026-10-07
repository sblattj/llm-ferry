#!/usr/bin/env python3
"""Refuse a route config in which a restricted fleet can reach a Chinese model.

The `domestic` fleet is US-only by policy. A fleet is a `<fleet>.<lane>` model_name
prefix (split at the FIRST "."), so the policy is checked on the config text
itself, before litellm or the front is ever launched.

A restricted fleet "reaches" a deployment when its model_name is

  * in the fleet itself, or
  * a fallback target (fallbacks / context_window_fallbacks /
    content_policy_fallbacks, in router_settings or litellm_settings) of
    anything already reached, or
  * a default_fallbacks target (those apply to EVERY group), or
  * a model_group_alias target of anything already reached.

Every deployment in that closure is checked against a denylist of Chinese
model/provider tokens, matched case-insensitively with a LEFT boundary only
(so `glm-5.3`, `glm5` and `kimi-k3` all match, `chatgpt` and `openai` do not).

Usage:  ferry_fleet_guard.py CONFIG.yaml
  exit 0  clean (silent)
  exit 1  violations (message on stdout)
  exit 2  unreadable / invalid YAML
"""
import re
import sys
from collections import deque

RESTRICTED_FLEETS = {"domestic"}

DENY_TOKENS = [
    "kimi", "moonshot", "moonshotai", "zai", r"z\.ai", "z-ai", "zhipu", "bigmodel",
    "glm", "chatglm", "deepseek", "qwen", "qwq", "dashscope", "aliyun", "alibaba",
    "minimax", "abab", "baichuan", "01-ai", r"01\.ai", "lingyiwanwu", "yi-large",
    "ernie", "baidu", "qianfan", "doubao", "volcengine", "volces", "bytedance",
    "hunyuan", "tencent", "stepfun", "iflytek", "xfyun", "sensenova", "sensetime",
    "internlm", "meituan", "longcat",
    # Xiaomi. `mimo` must not run into a letter, so `mimosa` stays clean.
    "xiaomi", r"mimo(?![a-z])",
    # inclusionAI / Ant Group. Ling and Ring are English words, so a bare `ling`
    # or `ring` is NOT a token: only a version digit or a size suffix after the
    # hyphen counts, and the left boundary rejects `spring-2`, `string-1`,
    # `bowling-3`, `sterling-pro` and `ceiling-lite`.
    "inclusionai", "antgroup", "ant-group", "antling", "bailing",
    r"(?:ling|ring)-(?:\d|lite|plus|flash|mini|max|pro|1t)",
    # Other Chinese labs and China-hosted providers found by sweeping the
    # OpenRouter catalogue (see lib/ferry-fleetguard.test.py).
    "kuaishou", "kwaipilot", "kwai", r"kat-(?:coder|dev|v\d)",
    "skywork", "kunlun", "tiangong",
    "openbmb", "minicpm", "modelbest",
    "thudm", "cogvlm", "cogview", "xverse", "huawei", "pangu", "baai", "infly",
    "internvl", "360zhinao", "telechat", "xiaohongshu", "rednote",
    r"dots-studio", r"dots-\d", r"dots\.llm",
    "nex-agi", r"nex-n\d",
    r"hy-mt", r"hy\d", "ui-tars",
]
DENY_RE = re.compile(r"(?<![a-z0-9])(?:%s)" % "|".join(DENY_TOKENS), re.I)

FALLBACK_KEYS = ("fallbacks", "context_window_fallbacks", "content_policy_fallbacks")
SECTIONS = ("router_settings", "litellm_settings")


def fleet_of(name):
    return name.split(".", 1)[0] if "." in name else None


def _fallback_edges(config):
    """(edges, default_targets): edges maps source group -> [targets]."""
    edges, defaults = {}, []
    for section in SECTIONS:
        sec = config.get(section)
        if not isinstance(sec, dict):
            continue
        for key in FALLBACK_KEYS:
            for entry in sec.get(key) or []:
                if not isinstance(entry, dict):
                    continue
                for src, targets in entry.items():
                    for t in _names(targets):
                        edges.setdefault(str(src), []).append((t, "fallback"))
        for t in _names(sec.get("default_fallbacks")):
            defaults.append(t)
    return edges, defaults


def _names(value):
    """Group names from a list of str / {"model": str} / {str: ...}, or a str."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    out = []
    if isinstance(value, dict):
        value = [value]
    for item in value if isinstance(value, (list, tuple)) else []:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict):
            if isinstance(item.get("model"), str):
                out.append(item["model"])
            else:
                out.extend(str(k) for k in item)
    return out


def _alias_edges(config):
    edges = {}
    sec = config.get("router_settings")
    aliases = sec.get("model_group_alias") if isinstance(sec, dict) else None
    if isinstance(aliases, dict):
        for name, target in aliases.items():
            tgt = target.get("model") if isinstance(target, dict) else target
            if isinstance(tgt, str):
                edges.setdefault(str(name), []).append((tgt, "alias"))
    return edges


def reachable(config):
    """model_name -> human path string, for every name a restricted fleet reaches."""
    edges, defaults = _fallback_edges(config)
    for name, lst in _alias_edges(config).items():
        edges.setdefault(name, []).extend(lst)

    names = [str(d.get("model_name")) for d in config.get("model_list") or []
             if isinstance(d, dict) and d.get("model_name") is not None]
    paths, queue = {}, deque()
    for n in names:
        if fleet_of(n) in RESTRICTED_FLEETS and n not in paths:
            paths[n] = n
            queue.append(n)
    # A restricted-fleet name may also appear only as an alias key or fallback
    # source with no deployment of its own; it is still a fleet entry point.
    for n in list(edges):
        if fleet_of(n) in RESTRICTED_FLEETS and n not in paths:
            paths[n] = n
            queue.append(n)
    if paths:
        # default_fallbacks apply to every group, so they are reachable from any.
        origin = next(iter(paths))
        for t in defaults:
            if t not in paths:
                paths[t] = "%s -> default_fallbacks -> %s" % (origin, t)
                queue.append(t)
    while queue:
        cur = queue.popleft()
        for tgt, kind in edges.get(cur, []):
            if tgt not in paths:
                paths[tgt] = "%s -> %s -> %s" % (paths[cur], kind, tgt)
                queue.append(tgt)
    return paths


def _fields(dep):
    lp = dep.get("litellm_params") if isinstance(dep.get("litellm_params"), dict) else {}
    mi = dep.get("model_info") if isinstance(dep.get("model_info"), dict) else {}
    pairs = [("litellm_params.model", lp.get("model")),
             ("litellm_params.api_base", lp.get("api_base")),
             ("litellm_params.custom_llm_provider", lp.get("custom_llm_provider")),
             ("model_info.base_model", mi.get("base_model"))]
    return [(k, str(v)) for k, v in pairs if v is not None]


def deployment_violation(params):
    """Why a runtime deployment's litellm params hit the denylist, else None.

    `params` is the litellm_params dict litellm hands a hook (model, api_base,
    custom_llm_provider, plus model_info.base_model when present). Shares
    DENY_RE with check(), so config-time and request-time use one denylist.
    """
    if not isinstance(params, dict):
        return None
    mi = params.get("model_info") if isinstance(params.get("model_info"), dict) else {}
    pairs = [("model", params.get("model")),
             ("api_base", params.get("api_base")),
             ("custom_llm_provider", params.get("custom_llm_provider")),
             ("base_model", params.get("base_model") or mi.get("base_model"))]
    for field, value in pairs:
        if value is None:
            continue
        m = DENY_RE.search(str(value))
        if m:
            return "token %r matches %s: %s" % (m.group(0), field, value)
    return None


def violation_report(path, problems):
    """The refusal text shared by the CLI and the front door's startup check."""
    lines = ["Error: the domestic fleet must use US models only; refusing to load %s:" % path]
    lines.extend(problems)
    lines.append("Fix the lane in %s, then re-run. The domestic fleet is US-only by policy; "
                 "international is the place for these models." % path)
    return "\n".join(lines)


def check(config):
    """Return one message per violation (empty list when the config is clean)."""
    if not isinstance(config, dict):
        return []
    paths = reachable(config)
    out = []
    for dep in config.get("model_list") or []:
        if not isinstance(dep, dict):
            continue
        name = str(dep.get("model_name"))
        if name not in paths:
            continue
        for field, value in _fields(dep):
            m = DENY_RE.search(value)
            if m:
                out.append("  %s (reached as %s): token %r matches %s: %s"
                           % (name, paths[name], m.group(0), field, value))
    return out


def main(argv):
    if len(argv) != 2:
        print("usage: ferry_fleet_guard.py CONFIG.yaml", file=sys.stderr)
        return 2
    path = argv[1]
    try:
        import yaml
        with open(path) as fh:
            config = yaml.safe_load(fh)
    except Exception as exc:  # unreadable file or invalid YAML
        print("Error: cannot read route config %s: %s" % (path, exc))
        return 2
    if not isinstance(config, dict):
        print("Error: route config %s is not a YAML mapping" % path)
        return 2
    problems = check(config)
    if not problems:
        return 0
    print(violation_report(path, problems))
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
