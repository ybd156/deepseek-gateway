"""请求策略：模型能力、黑白名单、映射、历史裁剪、提示词、思考档位。

放在独立模块里，网关只负责调用，不掺业务规则。
所有函数都是纯函数（除 load/save 之外），方便单测。
"""
from __future__ import annotations

import json
import os
import re

# ─────────────────────────── 模型能力表 ───────────────────────────
# 键是"模型名里出现的片段"，从上到下第一个命中生效（更具体的放前面）。
# context 单位 token；vision = 能否吃图片；reasoning = 支持的思考档位列表。
# OpenCode Zen 自营 / 免费模型。
# 上下文与图片能力来自公开模型目录（llmdir / models.dev 的 OpenCode Zen 条目）；
# max_output 目录里不给，按同类模型估算（标注 est），上游探测到真值会覆盖。
OPENCODE_CAPS: list = [
    # —— 1M 上下文档 ——
    ("beam-free", {"context": 1048576, "max_output": 32768, "vision": False,
                   "reasoning": ["off", "low", "medium", "high"]}),
    ("cat-jam-free", {"context": 1048576, "max_output": 32768, "vision": False,
                      "reasoning": ["off", "low", "medium", "high"]}),
    ("crouton-alpha-free", {"context": 1048576, "max_output": 32768, "vision": True,
                            "reasoning": ["off", "low", "medium", "high"]}),
    ("exo-free", {"context": 1048576, "max_output": 32768, "vision": False,
                  "reasoning": ["off", "low", "medium", "high"]}),
    ("fledge-alpha-free", {"context": 1048576, "max_output": 32768, "vision": False,
                           "reasoning": ["off", "low", "medium", "high"]}),
    ("pulse-alpha-free", {"context": 1048576, "max_output": 32768, "vision": True,
                          "reasoning": ["off", "low", "medium", "high"]}),
    ("x-preview-f-free", {"context": 1000000, "max_output": 32768, "vision": True,
                          "reasoning": ["off", "low", "medium", "high"]}),
    ("mimo-v2-pro-free", {"context": 1048576, "max_output": 32768, "vision": False,
                          "reasoning": ["off", "low", "medium", "high"]}),
    ("mimo-v2.6", {"context": 1048576, "max_output": 32768, "vision": False,
                   "reasoning": ["off", "low", "medium", "high"]}),
    ("mimo-v2", {"context": 1048576, "max_output": 32768, "vision": False,
                 "reasoning": ["off", "low", "medium", "high"]}),
    ("nemotron-3.5-super-vl-free", {"context": 1048576, "max_output": 32768,
                                    "vision": True,
                                    "reasoning": ["off", "low", "medium", "high"]}),
    ("nemotron-3-ultra-free", {"context": 1000000, "max_output": 32768,
                               "vision": False,
                               "reasoning": ["off", "low", "medium", "high"]}),
    ("minimax-m3.1-flash-free", {"context": 1000000, "max_output": 32768,
                                 "vision": True,
                                 "reasoning": ["off", "low", "medium", "high"]}),
    ("muse-spark-1.2", {"context": 1048576, "max_output": 32768, "vision": True,
                        "reasoning": ["off", "low", "medium", "high"]}),
    ("muse-spark-1.3", {"context": 1048576, "max_output": 32768, "vision": True,
                        "reasoning": ["off", "low", "medium", "high"]}),
    ("muse-wonder", {"context": 1048576, "max_output": 32768, "vision": True,
                     "reasoning": ["off", "low", "medium", "high"]}),
    ("iceman-contributor-free", {"context": 1048576, "max_output": 32768,
                                 "vision": False,
                                 "reasoning": ["off", "low", "medium", "high"]}),

    # —— 256K / 262K 档 ——
    ("flint-40b-free", {"context": 262144, "max_output": 32768, "vision": False,
                        "reasoning": ["off", "low", "medium", "high"]}),
    ("nemotron-3.5-lightning-free", {"context": 262144, "max_output": 32768,
                                     "vision": False,
                                     "reasoning": ["off", "low", "medium", "high"]}),
    ("north-mini-code-free", {"context": 256000, "max_output": 32768,
                              "vision": False,
                              "reasoning": ["off", "low", "medium", "high"]}),
    ("kimi-k2.5-free", {"context": 256000, "max_output": 32768, "vision": False,
                        "reasoning": ["off", "low", "medium", "high"]}),

    # —— 200K / 204K 档 ——
    ("deepseek-v4.1-flash-free", {"context": 200000, "max_output": 32768,
                                  "vision": True,
                                  "reasoning": ["off", "low", "medium", "high"]}),
    ("deepseek-v4-flash-free", {"context": 200000, "max_output": 32768,
                                "vision": False,
                                "reasoning": ["off", "low", "medium", "high"]}),
    ("big-pickle", {"context": 200000, "max_output": 32768, "vision": False,
                    "reasoning": ["off", "low", "medium", "high"]}),
    ("minimax-m2.1-free", {"context": 204800, "max_output": 32768, "vision": False,
                           "reasoning": ["off", "low", "medium", "high"]}),
    ("minimax-m2.5-free", {"context": 204800, "max_output": 32768, "vision": False,
                           "reasoning": ["off", "low", "medium", "high"]}),
    ("nemotron-3-super-free", {"context": 204800, "max_output": 32768,
                               "vision": False,
                               "reasoning": ["off", "low", "medium", "high"]}),
    ("minimax-m3-free", {"context": 200000, "max_output": 32768, "vision": True,
                         "reasoning": ["off", "low", "medium", "high"]}),

    # —— GLM 免费档（OpenCode 侧同名，能力与官方一致）——
    ("glm-5.3-flash-free", {"context": 200000, "max_output": 65536, "vision": True,
                            "reasoning": ["off", "low", "medium", "high"]}),
    ("glm-4.7-free", {"context": 200000, "max_output": 32768, "vision": False,
                      "reasoning": ["off", "low", "medium", "high"]}),
    ("glm-4.7-flash-free", {"context": 200000, "max_output": 32768, "vision": False,
                            "reasoning": ["off", "low", "medium", "high"]}),
    ("glm-4.6v-flash-free", {"context": 128000, "max_output": 16384, "vision": True,
                             "reasoning": ["off"]}),
    ("glm-5-free", {"context": 200000, "max_output": 65536, "vision": True,
                    "reasoning": ["off", "low", "medium", "high"]}),

    # —— 其它免费档 ——
    ("kat-coder-pro-v1-free", {"context": 262144, "max_output": 32768,
                               "vision": False, "reasoning": ["off"]}),
    ("ling-flash-free", {"context": 131072, "max_output": 16384, "vision": False,
                         "reasoning": ["off"]}),
    ("ring-1t-free", {"context": 131072, "max_output": 16384, "vision": False,
                      "reasoning": ["off", "low", "medium", "high"]}),
    ("ll-alpha-free", {"context": 131072, "max_output": 16384, "vision": False,
                       "reasoning": ["off"]}),
    ("space-bunny-free", {"context": 131072, "max_output": 16384, "vision": False,
                          "reasoning": ["off"]}),
    ("step-3.5-flash-free", {"context": 262144, "max_output": 32768,
                             "vision": False, "reasoning": ["off"]}),
    ("step-5-preview-free", {"context": 262144, "max_output": 32768,
                             "vision": False, "reasoning": ["off"]}),
    ("trinity-large-preview-free", {"context": 131072, "max_output": 16384,
                                    "vision": False, "reasoning": ["off"]}),
    ("coding-glm-4.7-free", {"context": 200000, "max_output": 32768,
                             "vision": False,
                             "reasoning": ["off", "low", "medium", "high"]}),
    ("coding-glm-5-free", {"context": 200000, "max_output": 65536, "vision": True,
                           "reasoning": ["off", "low", "medium", "high"]}),
    ("coding-minimax-m2.1-free", {"context": 204800, "max_output": 32768,
                                  "vision": False, "reasoning": ["off"]}),
]

BUILTIN_CAPS: list = [
    # DeepSeek
    ("deepseek-v4.1-flash", {"context": 128000, "max_output": 32768, "vision": False,
                             "reasoning": ["off", "low", "medium", "high"]}),
    ("deepseek-v4.1-pro", {"context": 256000, "max_output": 65536, "vision": False,
                           "reasoning": ["off", "low", "medium", "high"]}),
    ("deepseek-v4-pro", {"context": 256000, "max_output": 65536, "vision": False,
                         "reasoning": ["off", "low", "medium", "high"]}),
    ("deepseek-v3", {"context": 128000, "max_output": 8192, "vision": False,
                     "reasoning": ["off"]}),
    ("deepseek-r1", {"context": 128000, "max_output": 32768, "vision": False,
                     "reasoning": ["off", "low", "medium", "high"]}),
    ("deepseek", {"context": 128000, "max_output": 8192, "vision": False,
                  "reasoning": ["off"]}),
    # GLM / 智谱
    ("glm-5.3", {"context": 200000, "max_output": 65536, "vision": True,
                 "reasoning": ["off", "low", "medium", "high"]}),
    ("glm-5.2", {"context": 200000, "max_output": 65536, "vision": True,
                 "reasoning": ["off", "low", "medium", "high"]}),
    ("glm-4.5v", {"context": 128000, "max_output": 16384, "vision": True,
                  "reasoning": ["off"]}),
    ("glm", {"context": 128000, "max_output": 16384, "vision": False,
             "reasoning": ["off"]}),
    # Kimi / Moonshot
    ("kimi-k2.5", {"context": 256000, "max_output": 32768, "vision": True,
                   "reasoning": ["off", "low", "medium", "high"]}),
    ("kimi-k2", {"context": 256000, "max_output": 32768, "vision": False,
                 "reasoning": ["off"]}),
    ("kimi", {"context": 128000, "max_output": 16384, "vision": False,
              "reasoning": ["off"]}),
    # Qwen
    ("qwen3-vl", {"context": 128000, "max_output": 16384, "vision": True,
                  "reasoning": ["off"]}),
    ("qwen3", {"context": 128000, "max_output": 32768, "vision": False,
               "reasoning": ["off", "low", "medium", "high"]}),
    ("qwen2.5-vl", {"context": 128000, "max_output": 8192, "vision": True,
                    "reasoning": ["off"]}),
    ("qwen", {"context": 131072, "max_output": 8192, "vision": False,
              "reasoning": ["off"]}),
    # Claude
    ("claude-opus", {"context": 200000, "max_output": 32000, "vision": True,
                     "reasoning": ["off", "low", "medium", "high", "max"]}),
    ("claude-sonnet", {"context": 200000, "max_output": 64000, "vision": True,
                       "reasoning": ["off", "low", "medium", "high", "max"]}),
    ("claude-haiku", {"context": 200000, "max_output": 32000, "vision": True,
                      "reasoning": ["off", "low", "medium", "high"]}),
    ("claude", {"context": 200000, "max_output": 8192, "vision": True,
                "reasoning": ["off"]}),
    # OpenAI
    ("gpt-5", {"context": 400000, "max_output": 128000, "vision": True,
               "reasoning": ["off", "low", "medium", "high"]}),
    ("gpt-4.1", {"context": 1000000, "max_output": 32768, "vision": True,
                 "reasoning": ["off"]}),
    ("gpt-4o", {"context": 128000, "max_output": 16384, "vision": True,
                "reasoning": ["off"]}),
    ("gpt-4", {"context": 128000, "max_output": 8192, "vision": True,
               "reasoning": ["off"]}),
    ("o3", {"context": 200000, "max_output": 100000, "vision": True,
            "reasoning": ["off", "low", "medium", "high"]}),
    ("o4", {"context": 200000, "max_output": 100000, "vision": True,
            "reasoning": ["off", "low", "medium", "high"]}),
    # Gemini
    ("gemini", {"context": 1000000, "max_output": 65536, "vision": True,
                "reasoning": ["off", "low", "medium", "high"]}),
    # 其它常见开源
    ("llama", {"context": 128000, "max_output": 8192, "vision": False,
               "reasoning": ["off"]}),
    ("mistral", {"context": 128000, "max_output": 8192, "vision": False,
                 "reasoning": ["off"]}),
    ("llava", {"context": 32000, "max_output": 4096, "vision": True,
               "reasoning": ["off"]}),
    ("internvl", {"context": 32000, "max_output": 4096, "vision": True,
                  "reasoning": ["off"]}),
]

DEFAULT_CAPS = {"context": 32768, "max_output": 4096,
                "vision": False, "reasoning": ["off"]}
TIERS = ["off", "low", "medium", "high", "max"]


CAP_KEYS = ("context", "max_output", "vision", "reasoning")


def caps_for(model: str, override: dict | None = None,
             upstream: dict | None = None) -> dict:
    """查一个模型的能力，三层来源，越靠前越优先。

    1. override  —— 面板里手填的（最可信，人说了算）
    2. upstream  —— 从上游 /models 探测到的（真实，但可能不全）
    3. 内置表     —— 猜的，兜底
    """
    name = str(model or "").lower()
    hit = None
    # OpenCode 自营模型名会撞上通用家族名（如 deepseek-v4.1-flash-free），
    # 所以先查更具体的表
    for frag, caps in OPENCODE_CAPS:
        if frag in name:
            hit = caps
            break
    if hit is None:
        for frag, caps in BUILTIN_CAPS:
            if frag in name:
                hit = caps
                break
    base = dict(hit or DEFAULT_CAPS)
    base["known"] = hit is not None
    base["source"] = "builtin" if hit else "guess"

    if isinstance(upstream, dict) and upstream:
        got = False
        for k in CAP_KEYS:
            v = upstream.get(k)
            if v not in (None, ""):
                base[k] = v
                got = True
        if got:
            base["known"] = True
            base["source"] = "upstream"

    if isinstance(override, dict) and override:
        for k in CAP_KEYS:
            if k in override and override[k] not in (None, ""):
                base[k] = override[k]
        base["known"] = True
        base["source"] = "manual"

    return base


def parse_upstream_models(data) -> dict:
    """从上游 /models 的响应里抠出能力。各家字段名不一样，都试一遍。"""
    items = data.get("data") if isinstance(data, dict) else data
    out = {}
    for it in items or []:
        if isinstance(it, str):
            continue
        if not isinstance(it, dict):
            continue
        mid = str(it.get("id") or it.get("name") or "").strip()
        if not mid:
            continue
        top = it.get("top_provider") or {}
        if not isinstance(top, dict):
            top = {}
        arch = it.get("architecture") or {}
        if not isinstance(arch, dict):
            arch = {}

        ctx = (it.get("context_length") or it.get("context_window")
               or it.get("max_context_length") or top.get("context_length")
               or (it.get("limits") or {}).get("context")
               or (it.get("model_info") or {}).get("context_size"))
        mo = (it.get("max_completion_tokens") or it.get("max_output_tokens")
              or it.get("max_tokens") or top.get("max_completion_tokens")
              or (it.get("limits") or {}).get("max_output")
              or (it.get("model_info") or {}).get("max_output"))

        mods = (arch.get("input_modalities") or it.get("input_modalities")
                or it.get("modalities") or [])
        vision = None
        if isinstance(mods, list) and mods:
            vision = any(str(m).lower() in ("image", "vision", "img") for m in mods)
        for key in ("supports_vision", "support_vision", "vision"):
            if vision is None and isinstance(it.get(key), bool):
                vision = it[key]

        caps = {}
        try:
            if ctx:
                caps["context"] = int(ctx)
        except Exception:
            pass
        try:
            if mo:
                caps["max_output"] = int(mo)
        except Exception:
            pass
        if vision is not None:
            caps["vision"] = bool(vision)
        if caps:
            out[mid] = caps
    return out


# ─────────────────────────── 名单 / 映射 ───────────────────────────
def _split_list(raw) -> list:
    if not raw:
        return []
    if isinstance(raw, (list, tuple)):
        items = [str(x) for x in raw]
    else:
        items = re.split(r"[,，、\s]+", str(raw))
    return [x.strip() for x in items if x.strip()]


def parse_rules(raw) -> list:
    """把 "deepseek-pro, cn:legacy-model, global:gpt-5.4" 解析成 [(scope, name)]。

    scope: "" 表示不限定（等价 global）。
    """
    out = []
    for item in _split_list(raw):
        scope = ""
        name = item
        if ":" in item:
            head, tail = item.split(":", 1)
            if head.strip().lower() in ("cn", "global", "all", "*"):
                scope = head.strip().lower()
                name = tail.strip()
        if name:
            out.append((scope, name.lower()))
    return out


def rule_hits(rules: list, model: str, region: str = "") -> bool:
    """规则里有没有命中这个模型。region 是当前上游的区域标记（cn/global）。"""
    name = str(model or "").lower()
    region = (region or "").lower()
    for scope, target in rules:
        if not target:
            continue
        matched = (target == name) or (target in name) if len(target) >= 4 else (target == name)
        if not matched:
            continue
        if scope in ("", "all", "*"):
            return True
        if scope == region:
            return True
        # cn 规则对未标注区域的上游也生效（保守起见只对 cn 生效）
        if scope == "cn" and region == "":
            continue
    return False


def parse_map(raw) -> list:
    """把 "claude-opus-4=glm-5.2, gpt-5=global:gpt-5.4" 解析成 [(client, target, scope)]。"""
    out = []
    for item in _split_list(raw):
        if "=" not in item:
            continue
        left, right = item.split("=", 1)
        client = left.strip()
        target = right.strip()
        scope = ""
        if ":" in target:
            head, tail = target.split(":", 1)
            if head.strip().lower() in ("cn", "global", "all", "*"):
                scope = head.strip().lower()
                target = tail.strip()
        if client and target:
            out.append((client.lower(), target, scope))
    return out


def map_model(mappings: list, model: str, region: str = "") -> tuple:
    """返回 (发给上游的模型名, 是否发生了映射)。"""
    name = str(model or "")
    low = name.lower()
    region = (region or "").lower()
    for client, target, scope in mappings:
        if client != low:
            continue
        if scope in ("", "all", "*") or scope == region:
            return target, True
        if scope == "global" and region in ("", "global"):
            return target, True
    return name, False


# ─────────────────────────── 历史裁剪 ───────────────────────────
def trim_history(messages: list, keep_rounds: int) -> tuple:
    """只保留最近 N 轮（一轮 = 一条 user 及其后的内容）。system 永远保留。

    返回 (新 messages, 丢掉的条数)。
    """
    if not messages or keep_rounds is None or keep_rounds <= 0:
        return messages, 0

    systems, rest = [], []
    for m in messages:
        if isinstance(m, dict) and str(m.get("role")) == "system":
            systems.append(m)
        else:
            rest.append(m)

    # 找出 user 的位置，按 user 切轮
    starts = [i for i, m in enumerate(rest)
              if isinstance(m, dict) and str(m.get("role")) == "user"]
    if len(starts) <= keep_rounds:
        return messages, 0
    cut = starts[len(starts) - keep_rounds]
    dropped = len(rest[:cut])
    return systems + rest[cut:], dropped


# ─────────────────────────── 提示词 ───────────────────────────
def apply_system(mode: str, default_text: str, messages: list) -> list:
    """系统提示词模式。

    passthrough  原样透传客户端的 system（默认）
    default      用面板里的提示词替换客户端 system
    append       客户端 system 后面追加面板提示词
    prepend      面板提示词放前面
    drop         丢掉客户端 system
    """
    mode = (mode or "passthrough").lower()
    text = (default_text or "").strip()
    if mode == "passthrough" or not messages:
        return messages

    systems = [m for m in messages
               if isinstance(m, dict) and str(m.get("role")) == "system"]
    others = [m for m in messages
              if not (isinstance(m, dict) and str(m.get("role")) == "system")]
    client_text = chr(10).join(str(m.get("content") or "") for m in systems).strip()

    if mode == "drop":
        return others
    if not text:
        return messages

    if mode == "default":
        merged = text
    elif mode == "append":
        merged = (client_text + chr(10) + text).strip() if client_text else text
    elif mode == "prepend":
        merged = (text + chr(10) + client_text).strip() if client_text else text
    else:
        return messages

    return [{"role": "system", "content": merged}] + others


# ─────────────────────────── 思考档位 ───────────────────────────
def resolve_effort(model: str, want: str, gateway_tier: str, mode: str,
                   caps: dict | None = None) -> tuple:
    """算出实际要发给上游的思考档位。

    返回 (effort 或 "", 说明)。空字符串表示不要往请求里加这个字段。

    mode:
      client  客户端没传才补（默认）
      force   一律改成网关档位（但尊重客户端 off）
      force+  一律改成网关档位（连 off 也覆盖）
    """
    caps = caps or caps_for(model)
    supported = list(caps.get("reasoning") or ["off"])
    want = (want or "").strip().lower()
    gateway_tier = (gateway_tier or "").strip().lower()
    mode = (mode or "client").lower()

    def fit(tier: str) -> str:
        """落到不高于该档的最高支持档。"""
        if tier in supported:
            return tier
        if not supported:
            return ""
        order = [t for t in TIERS if t in supported]
        if not order:
            return ""
        try:
            idx = TIERS.index(tier)
        except ValueError:
            return order[0]
        best = ""
        for t in order:
            if TIERS.index(t) <= idx:
                best = t
        return best or order[0]

    if want:
        if mode == "force+":
            return fit(gateway_tier), f"覆盖客户端 {want} → {gateway_tier}"
        if mode == "force" and want != "off":
            return fit(gateway_tier), f"覆盖客户端 {want} → {gateway_tier}"
        return fit(want), ""

    if not gateway_tier:
        return "", ""
    return fit(gateway_tier), "客户端未指定，用网关档位"


# ─────────────────────────── 上下文估算 ───────────────────────────
def text_len(messages: list) -> int:
    """粗估字符数（不算 token，够用来做阈值提示）。"""
    total = 0
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        c = m.get("content")
        if isinstance(c, list):
            for part in c:
                if isinstance(part, dict):
                    total += len(str(part.get("text") or ""))
        else:
            total += len(str(c or ""))
    return total


def has_image(messages: list) -> bool:
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        c = m.get("content")
        if isinstance(c, list):
            for part in c:
                if isinstance(part, dict) and str(part.get("type") or "") in (
                        "image_url", "image", "input_image"):
                    return True
    return False


def fit_context(messages: list, context_tokens: int, reserve: int = 1024) -> tuple:
    """按上下文窗口丢最老的对话，保住 system 和最后一条 user。

    返回 (新 messages, 丢掉条数)。字符按 3 字符 ≈ 1 token 粗算。
    """
    if not context_tokens or context_tokens <= 0:
        return messages, 0
    budget_chars = max(2000, (context_tokens - reserve) * 3)
    if text_len(messages) <= budget_chars:
        return messages, 0

    systems = [m for m in messages
               if isinstance(m, dict) and str(m.get("role")) == "system"]
    rest = [m for m in messages
            if not (isinstance(m, dict) and str(m.get("role")) == "system")]
    dropped = 0
    while rest and text_len(systems + rest) > budget_chars:
        # 一次丢一轮：从最老的 user 开始
        idx = 0
        for i, m in enumerate(rest):
            if isinstance(m, dict) and str(m.get("role")) == "user":
                idx = i
                break
        # 保留最后一条 user 不动
        if idx == len(rest) - 1:
            break
        rest.pop(idx)
        dropped += 1
    return systems + rest, dropped
