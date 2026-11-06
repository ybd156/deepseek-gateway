#!/usr/bin/env python3
"""飞牛 AI 网关 —— OpenAI 兼容聚合网关。

设计原则
--------
1. 只依赖标准库（http.server / urllib / sqlite3 / json），不装第三方包。
2. 配置与代码分离：settings.json / providers.json / secrets.json / tokens.json
   都在 DATA_DIR 下。
3. 上游按「协议」适配：OpenAI Chat / Responses / Anthropic / Gemini /
   opencode（本地 CLI 或 server）。对客户端永远只说 OpenAI 一种话。
4. 界面是独立静态文件（web/），不用 Python 拼 HTML。
"""
from __future__ import annotations

import base64
import json
import os
import signal
import socket
import socketserver
import sqlite3
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from opencode_supervisor import OpenCodeSupervisor
from opencode_upstream import OpenCodeClient, OpenCodeError
from proxy_support import build_opener
import policy

APP_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(APP_DIR, "web")

CONFIG_PATH = os.getenv("CONFIG_PATH") or os.path.join(APP_DIR, "providers.json")
DATA_DIR = os.getenv("DATA_DIR") or APP_DIR
SETTINGS_PATH = os.path.join(DATA_DIR, "settings.json")
SECRETS_PATH = os.path.join(DATA_DIR, "secrets.json")
TOKENS_PATH = os.path.join(DATA_DIR, "tokens.json")
OVERRIDES_PATH = os.path.join(DATA_DIR, "toggles.json")
DB_PATH = os.path.join(DATA_DIR, "usage.db")

HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "18787"))
GATEWAY_SOCKET = os.getenv("GATEWAY_SOCKET", "")
GATEWAY_PREFIX = os.getenv("GATEWAY_PREFIX", "").rstrip("/")

BOOT_STRATEGY = (os.getenv("STRATEGY") or "priority").strip().lower()
BOOT_COOLDOWN = int(os.getenv("COOLDOWN_SECONDS", "60") or 60)
DEFAULT_TIMEOUT = 180


# ─────────────────────────── 小工具 ───────────────────────────
def now() -> float:
    return time.time()


def _num(value, default, low, high):
    """把配置里的数字夹到合法范围。"""
    try:
        n = int(float(value))
    except Exception:
        return default
    return max(low, min(n, high))


def as_list(value) -> list:
    if not value:
        return []
    if isinstance(value, (list, tuple)):
        return [str(x).strip() for x in value if str(x).strip()]
    parts = str(value).replace(chr(10), ",").split(",")
    return [x.strip() for x in parts if x.strip()]


def atomic_write_json(path: str, data) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def read_json(path: str, default):
    try:
        with open(path, encoding="utf-8") as fh:
            raw = fh.read().strip()
        return json.loads(raw) if raw else default
    except Exception:
        return default


def safe_header(value) -> str:
    text = str(value)
    try:
        text.encode("latin-1")
        return text
    except UnicodeEncodeError:
        return text.encode("ascii", "replace").decode("ascii")


def err_text(exc: BaseException) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        try:
            body = exc.read().decode("utf-8", "ignore")
        except Exception:
            body = ""
        return f"HTTP {exc.code} {body[:300]}"
    return f"{type(exc).__name__}: {exc}"


def strip_ansi(text: str) -> str:
    out, i = [], 0
    while i < len(text):
        ch = text[i]
        if ch == chr(27):
            j = i + 1
            if j < len(text) and text[j] == "[":
                j += 1
                while j < len(text) and not ("@" <= text[j] <= "~"):
                    j += 1
                i = j + 1
                continue
            i += 2
            continue
        if ch == "[":
            j = i + 1
            while j < len(text) and (text[j].isdigit() or text[j] == ";"):
                j += 1
            if j > i + 1 and j < len(text) and text[j].isalpha():
                i = j + 1
                continue
        out.append(ch)
        i += 1
    return "".join(out)


# ─────────────────────────── 运行时状态 ───────────────────────────
RUNTIME = {
    "strategy": BOOT_STRATEGY if BOOT_STRATEGY in ("priority", "cheapest") else "priority",
    "cooldown": max(1, BOOT_COOLDOWN),
    "gateway_token": os.getenv("GATEWAY_TOKEN", "") or "",
    "proxy": "",
    # 出站指纹脱敏：把消息里的品牌词替换掉，避免上游按内容识别来源
    "sanitize": False,
    "sanitize_scope": "all",
    "sanitize_words": [],
    "sanitize_replacement": "该服务",
    # 超时（秒）
    "timeout_short": 120,        # 探测 / 列模型这类短请求
    "timeout_first_byte": 120,   # 聊天等首字节
    "timeout_stream_idle": 300,  # 流式两段之间的最长间隔
    # 出站身份
    "user_agent": "",
    # 模型准入
    "model_blacklist": "",
    "model_whitelist": "",
    "model_mapping": "",
    "model_caps": {},
    # 提示词
    "system_mode": "passthrough",
    "system_text": "",
    "system_file": "",
    "keep_rounds": 0,
    # 思考档位
    "effort_enabled": True,
    "effort_default": "max",
    "effort_mode": "client",
    # 日志
    "log_source": True,
}
STATE: dict = {}
STATE_LOCK = threading.Lock()
PROVIDERS: list = []
SETTINGS: dict = {}
_CURRENT = threading.local()


def load_settings() -> dict:
    data = read_json(SETTINGS_PATH, {})
    return data if isinstance(data, dict) else {}


def save_settings(patch: dict) -> None:
    data = load_settings()
    data.update(patch)
    atomic_write_json(SETTINGS_PATH, data)


def load_secrets() -> dict:
    data = read_json(SECRETS_PATH, {})
    return data if isinstance(data, dict) else {}


def save_secret(env_name: str, value: str) -> None:
    data = load_secrets()
    if value:
        data[env_name] = value
    else:
        data.pop(env_name, None)
    atomic_write_json(SECRETS_PATH, data)
    try:
        os.chmod(SECRETS_PATH, 0o600)
    except Exception:
        pass


def load_overrides() -> dict:
    data = read_json(OVERRIDES_PATH, {})
    return data if isinstance(data, dict) else {}


def save_overrides(data: dict) -> None:
    atomic_write_json(OVERRIDES_PATH, data)


def load_runtime() -> None:
    data = load_settings()
    strategy = str(data.get("strategy") or "").strip().lower()
    if strategy not in ("priority", "cheapest"):
        strategy = BOOT_STRATEGY if BOOT_STRATEGY in ("priority", "cheapest") else "priority"
    try:
        cooldown = max(1, min(int(data.get("cooldown_seconds") or BOOT_COOLDOWN), 3600))
    except Exception:
        cooldown = max(1, BOOT_COOLDOWN)
    RUNTIME.update(
        strategy=strategy,
        cooldown=cooldown,
        gateway_token=str(data.get("gateway_token") or "").strip(),
        proxy=str(data.get("proxy") or "").strip(),
        sanitize=bool(data.get("sanitize")),
        sanitize_scope=str(data.get("sanitize_scope") or "all").lower(),
        sanitize_words=as_list(data.get("sanitize_words")),
        sanitize_replacement=str(data.get("sanitize_replacement")
                                 or "该服务").strip() or "该服务",
        timeout_short=_num(data.get("timeout_short"), 120, 5, 3600),
        timeout_first_byte=_num(data.get("timeout_first_byte"), 120, 5, 3600),
        timeout_stream_idle=_num(data.get("timeout_stream_idle"), 300, 5, 3600),
        user_agent=str(data.get("user_agent") or "").strip(),
        model_blacklist=str(data.get("model_blacklist") or ""),
        model_whitelist=str(data.get("model_whitelist") or ""),
        model_mapping=str(data.get("model_mapping") or ""),
        model_caps=(data.get("model_caps") if isinstance(data.get("model_caps"), dict)
                    else {}),
        system_mode=str(data.get("system_mode") or "passthrough").lower(),
        system_text=str(data.get("system_text") or ""),
        system_file=str(data.get("system_file") or "").strip(),
        keep_rounds=_num(data.get("keep_rounds"), 0, 0, 500),
        effort_enabled=bool(data.get("effort_enabled", True)),
        effort_default=str(data.get("effort_default") or "max").lower(),
        effort_mode=str(data.get("effort_mode") or "client").lower(),
        log_source=bool(data.get("log_source", True)),
    )


def opencode_opts() -> dict:
    data = load_settings()
    try:
        port = max(1, min(int(data.get("opencode_port") or 4096), 65535))
    except Exception:
        port = 4096
    return {
        "bin": str(data.get("opencode_bin") or ""),
        "port": port,
        "workspace": str(data.get("opencode_workspace") or ""),
        "autostart": bool(data.get("opencode_autostart", True)),
    }


SUPERVISOR = OpenCodeSupervisor(DATA_DIR, opencode_opts)


# ─────────────────────────── 上游加载 ───────────────────────────
def provider_files() -> list:
    files = [CONFIG_PATH]
    extra = os.path.join(os.path.dirname(os.path.abspath(CONFIG_PATH)), "providers.d")
    if os.path.isdir(extra):
        for name in sorted(os.listdir(extra)):
            if name.endswith(".json"):
                files.append(os.path.join(extra, name))
    return files


def provider_key_source(p: dict | None, sec: dict | None = None) -> str:
    if not p:
        return "无"
    sec = load_secrets() if sec is None else sec
    env_name = p.get("api_key_env") or ""
    if env_name and sec.get(env_name):
        return "面板填写"
    if env_name and os.getenv(env_name):
        return "环境变量"
    if p.get("api_key"):
        return "配置内联"
    if p.get("no_auth") or p.get("type") == "opencode":
        return "免密钥"
    return "无"


def load_config() -> None:
    global PROVIDERS, SETTINGS
    settings = load_settings()
    SETTINGS = settings if isinstance(settings, dict) else {}
    load_runtime()

    _ock = str(load_secrets().get("OPENCODE_API_KEY") or "").strip()
    if _ock:
        os.environ["OPENCODE_API_KEY"] = _ock

    raw: list = []
    for path in provider_files():
        data = read_json(path, {})
        for it in (data.get("providers") or []):
            if isinstance(it, dict) and str(it.get("name") or "").strip():
                raw.append(it)

    overrides = load_overrides()
    secrets = load_secrets()
    default_timeout = int(SETTINGS.get("request_timeout") or DEFAULT_TIMEOUT)

    items: list = []
    for it in raw:
        env_name = it.get("api_key_env") or ""
        if env_name:
            key = secrets.get(env_name, "") or os.getenv(env_name, "")
        else:
            key = it.get("api_key", "")

        name = str(it["name"]).strip()
        enabled = bool(it.get("enabled", True))
        if name in overrides:
            enabled = overrides[name]

        ptype = str(it.get("type", "http") or "http").lower()
        timeout = int(it.get("timeout") or default_timeout)
        if ptype == "opencode" and timeout >= DEFAULT_TIMEOUT:
            timeout = 150

        items.append({
            "name": name,
            "type": ptype,
            "protocol": str(it.get("protocol", "") or "").lower(),
            "enabled": enabled,
            "priority": int(it.get("priority", 100) or 100),
            "base_url": str(it.get("base_url", "") or "").rstrip("/"),
            "model": str(it.get("model", "") or ""),
            "models": as_list(it.get("models")),
            "model_contains": str(it.get("model_contains", "") or ""),
            "model_passthrough": bool(it.get("model_passthrough", False)),
            "strict_model": bool(it.get("strict_model", False)),
            "api_key": str(key or ""),
            "key_env": env_name,
            "no_auth": bool(it.get("no_auth", False)),
            "timeout": max(5, timeout),
            "quant": str(it.get("quant", "") or "-"),
            "free_only": bool(it.get("free_only", False)),
            "free_quota_tokens": int(it.get("free_quota_tokens", 0) or 0),
            "price_in": float(it.get("price_in", 0) or 0),
            "price_out": float(it.get("price_out", 0) or 0),
            "extra": it.get("extra") or {},
            "proxy": str(it.get("proxy", "") or ""),
            "console_url": str(it.get("console_url", "") or ""),
            "note": str(it.get("note", "")),
            "opencode_mode": str(it.get("opencode_mode", "") or "server").lower(),
            "opencode_provider": str(it.get("opencode_provider", "opencode") or "opencode"),
            "opencode_agent": str(it.get("opencode_agent", "") or ""),
            "opencode_tools": it.get("opencode_tools"),
            "opencode_extra": it.get("opencode_extra") or {},
        })

    PROVIDERS = items
    with STATE_LOCK:
        for p in PROVIDERS:
            STATE.setdefault(p["name"], {})


# ─────────────────────────── 记账 ───────────────────────────
def init_db() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS usage(
               ts REAL, provider TEXT, model TEXT,
               tokens_in INTEGER, tokens_out INTEGER,
               cost REAL, ok INTEGER, ms INTEGER, err TEXT, token_id TEXT,
               first_byte_ms INTEGER DEFAULT 0, stream INTEGER DEFAULT 0,
               cache_hit INTEGER DEFAULT 0, cache_miss INTEGER DEFAULT 0,
               client_ip TEXT DEFAULT '', user_agent TEXT DEFAULT '',
               effort TEXT DEFAULT '', upstream_model TEXT DEFAULT '',
               request_id TEXT DEFAULT '', status INTEGER DEFAULT 200)"""
    )
    cols = [r[1] for r in conn.execute("PRAGMA table_info(usage)").fetchall()]
    for col, ddl in (
            ("token_id", "TEXT DEFAULT ''"),
            ("first_byte_ms", "INTEGER DEFAULT 0"),
            ("stream", "INTEGER DEFAULT 0"),
            ("cache_hit", "INTEGER DEFAULT 0"),
            ("cache_miss", "INTEGER DEFAULT 0"),
            ("client_ip", "TEXT DEFAULT ''"),
            ("user_agent", "TEXT DEFAULT ''"),
            ("effort", "TEXT DEFAULT ''"),
            ("upstream_model", "TEXT DEFAULT ''"),
            ("request_id", "TEXT DEFAULT ''"),
            ("status", "INTEGER DEFAULT 200")):
        if col not in cols:
            try:
                conn.execute(f"ALTER TABLE usage ADD COLUMN {col} {ddl}")
            except Exception:
                pass
    conn.commit()
    conn.close()


_USAGE_CACHE: dict = {"ts": 0.0, "data": {}}
_QUOTA_CACHE: dict = {"ts": 0.0, "data": {}}


def record(provider: str, model: str, tin: int, tout: int, cost: float,
           ok: bool, ms: int, error: str = "", token_id: str = "",
           extra: dict | None = None) -> None:
    """记一笔。extra 可带首字延迟、缓存、来源、请求 ID 等。"""
    if not token_id:
        token_id = getattr(_CURRENT, "token_id", "")
    e = extra or {}
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        conn.execute(
            "INSERT INTO usage(ts, provider, model, tokens_in, tokens_out,"
            " cost, ok, ms, err, token_id, first_byte_ms, stream,"
            " cache_hit, cache_miss, client_ip, user_agent, effort,"
            " upstream_model, request_id, status)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (now(), provider, model, int(tin), int(tout), float(cost),
             1 if ok else 0, int(ms), (error or "")[:300], token_id,
             int(e.get("first_byte_ms") or 0), 1 if e.get("stream") else 0,
             int(e.get("cache_hit") or 0), int(e.get("cache_miss") or 0),
             str(e.get("client_ip") or "")[:64],
             str(e.get("user_agent") or "")[:200],
             str(e.get("effort") or "")[:16],
             str(e.get("upstream_model") or "")[:120],
             str(e.get("request_id") or "")[:40],
             int(e.get("status") or (200 if ok else 502))),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass
    _USAGE_CACHE["ts"] = 0.0
    _QUOTA_CACHE["ts"] = 0.0


def usage_summary() -> dict:
    if now() - _USAGE_CACHE["ts"] < 3:
        return _USAGE_CACHE["data"]
    out: dict = {}
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        cur = conn.execute(
            "SELECT provider, COUNT(*), SUM(ok), SUM(tokens_in),"
            " SUM(tokens_out), SUM(cost) FROM usage"
            " WHERE ts >= ? GROUP BY provider", (now() - 86400,))
        for name, reqs, oks, tin, tout, cost in cur.fetchall():
            out[name] = {
                "requests_24h": int(reqs or 0),
                "ok_24h": int(oks or 0),
                "tokens_in_24h": int(tin or 0),
                "tokens_out_24h": int(tout or 0),
                "cost_24h": float(cost or 0.0),
            }
        conn.close()
    except Exception:
        pass
    _USAGE_CACHE.update(ts=now(), data=out)
    return out


def used_tokens() -> dict:
    if now() - _QUOTA_CACHE["ts"] < 3:
        return _QUOTA_CACHE["data"]
    out: dict = {}
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        cur = conn.execute(
            "SELECT provider, SUM(tokens_in + tokens_out) FROM usage"
            " WHERE ok = 1 GROUP BY provider")
        for name, total in cur.fetchall():
            out[name] = int(total or 0)
        conn.close()
    except Exception:
        pass
    _QUOTA_CACHE.update(ts=now(), data=out)
    return out


def is_exhausted(p: dict, used: dict) -> bool:
    if not p["free_only"] or p["free_quota_tokens"] <= 0:
        return False
    return used.get(p["name"], 0) >= p["free_quota_tokens"]


def usage_of(data: dict) -> dict:
    """从上游响应里抽 tokens / 缓存命中（兼容 OpenAI 与 Anthropic 口径）。"""
    u = data.get("usage") or {}
    tin = int(u.get("prompt_tokens") or u.get("input_tokens") or 0)
    tout = int(u.get("completion_tokens") or u.get("output_tokens") or 0)

    hit = 0
    details = u.get("prompt_tokens_details") or {}
    if isinstance(details, dict):
        hit = int(details.get("cached_tokens") or 0)
    if not hit:
        hit = int(u.get("cache_read_input_tokens") or 0)
    if not hit:
        hit = int(u.get("cached_tokens") or 0)
    miss = max(0, tin - hit)
    return {"in": tin, "out": tout, "cache_hit": hit, "cache_miss": miss}


def client_ip(handler) -> str:
    """取真实来源 IP：优先 X-Forwarded-For 首个，否则用连接对端。"""
    fwd = handler.headers.get("X-Forwarded-For") or ""
    if fwd:
        first = fwd.split(",")[0].strip()
        if first:
            return first
    try:
        return str(handler.client_address[0])
    except Exception:
        return ""


def cost_of(p: dict, tin: int, tout: int) -> float:
    return (tin / 1_000_000.0) * p["price_in"] + (tout / 1_000_000.0) * p["price_out"]


# ─────────────────────────── 访问令牌 ───────────────────────────
def load_tokens() -> list:
    data = read_json(TOKENS_PATH, {"tokens": []})
    tokens = data.get("tokens") if isinstance(data, dict) else []
    return tokens if isinstance(tokens, list) else []


def save_tokens(tokens: list) -> None:
    atomic_write_json(TOKENS_PATH, {"tokens": tokens})
    try:
        os.chmod(TOKENS_PATH, 0o600)
    except Exception:
        pass


def find_token(key: str) -> dict | None:
    for t in load_tokens():
        if str(t.get("key", "")) and str(t.get("key")) == key:
            return t
    return None


def token_usage() -> dict:
    out: dict = {}
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        cur = conn.execute(
            "SELECT token_id, SUM(tokens_in + tokens_out) FROM usage"
            " WHERE ok = 1 AND token_id <> '' GROUP BY token_id")
        for tid, total in cur.fetchall():
            out[str(tid)] = int(total or 0)
        conn.close()
    except Exception:
        pass
    return out


def mask_key(key: str) -> str:
    key = str(key or "")
    if len(key) <= 12:
        return key[:4] + "****"
    return key[:9] + "****" + key[-4:]


def gen_token_key() -> str:
    return "sk-fnos-" + base64.b32encode(os.urandom(16)).decode().rstrip("=").lower()[:24]


# ─────────────────────────── 鉴权 ───────────────────────────
def _bearer(headers) -> str:
    raw = headers.get("Authorization") or ""
    if raw.lower().startswith("bearer "):
        return raw[7:].strip()
    return (headers.get("X-Api-Key") or "").strip()


def is_gateway_admin(handler) -> bool:
    return str(handler.headers.get("X-Trim-Isadmin") or "").lower() in ("1", "true")


def is_admin(handler) -> bool:
    if is_gateway_admin(handler):
        return True
    master = RUNTIME["gateway_token"]
    if not master:
        return True
    return _bearer(handler.headers) == master


def auth_v1(handler) -> tuple:
    key = _bearer(handler.headers)
    master = RUNTIME["gateway_token"]
    if master and key == master:
        return True, None, ""
    if key:
        tok = find_token(key)
        if tok:
            if not tok.get("enabled", True):
                return False, None, "这个令牌已被停用"
            quota = int(tok.get("quota_tokens") or 0)
            if quota > 0 and token_usage().get(str(tok.get("id", "")), 0) >= quota:
                return False, None, "令牌额度已用尽"
            return True, tok, ""
        return False, None, "缺少 API Key，或 Key 不正确"
    if not master and not [t for t in load_tokens() if t.get("enabled", True)]:
        return True, None, ""
    return False, None, "缺少 API Key"


# ─────────────────────────── 上游调用 ───────────────────────────
class ProviderError(Exception):
    """上游调用失败。"""


def apply_request_policy(payload: dict) -> tuple:
    """按面板策略改写请求。返回 (新 payload, 错误或 None)。"""
    msgs = payload.get("messages")
    if not isinstance(msgs, list):
        msgs = []
    model = str(payload.get("model") or "").strip()

    # 1) 黑名单：直接拒
    black = policy.parse_rules(RUNTIME["model_blacklist"])
    if model and policy.rule_hits(black, model):
        return payload, (400, f"模型 {model} 在网关黑名单里，已拒绝。"
                              f"要放行请到「高级」页删掉这条规则。")

    # 2) 白名单：非空时只放行名单内的
    white = policy.parse_rules(RUNTIME["model_whitelist"])
    if white and model and not policy.rule_hits(white, model):
        return payload, (400, f"模型 {model} 不在白名单里，已拒绝。"
                              f"要放行请到「高级」页把它加进白名单，或清空白名单。")

    # 3) 模型映射：客户端名 → 上游真名
    payload["_route_model"] = model
    mapping = policy.parse_map(RUNTIME["model_mapping"])
    if model and mapping:
        mapped, changed = policy.map_model(mapping, model)
        if changed:
            # 路由仍按客户端原名匹配上游，出站才换成映射后的真名
            payload["_mapped_model"] = mapped

    # 4) 系统提示词
    text = RUNTIME["system_text"]
    fp = RUNTIME["system_file"]
    if fp:
        try:
            with open(fp, encoding="utf-8") as fh:
                text = fh.read()
        except Exception:
            pass
    payload["messages"] = policy.apply_system(RUNTIME["system_mode"], text, msgs)

    # 5) 只保留最近 N 轮
    payload["messages"], _ = policy.trim_history(
        payload["messages"], RUNTIME["keep_rounds"])

    # 6) 上下文窗口：超了就丢最老的对话
    caps = policy.caps_for(model, (RUNTIME["model_caps"] or {}).get(model))
    payload["messages"], dropped = policy.fit_context(
        payload["messages"], caps.get("context") or 0)

    # 7) 图片：模型不支持就别发，给个明白话
    if policy.has_image(payload["messages"]) and not caps.get("vision"):
        return payload, (400, f"模型 {model} 不支持图片输入（上下文 "
                              f"{caps.get('context')} tokens）。"
                              f"请换一个支持视觉的模型，或去掉图片。")

    # 8) 思考档位
    if RUNTIME["effort_enabled"]:
        want = payload.get("reasoning_effort") or ""
        eff, _note = policy.resolve_effort(
            model, want, RUNTIME["effort_default"], RUNTIME["effort_mode"], caps)
        if eff and eff != "off":
            payload["reasoning_effort"] = eff
        elif eff == "off":
            payload.pop("reasoning_effort", None)

    payload["_caps"] = caps
    return payload, None


def _tail(name: str) -> str:
    """取模型名的最后一段：opencode/deepseek-v4.1-flash-free → deepseek-v4.1-flash-free"""
    return str(name or "").strip().split("/")[-1].lower()


def declared_models(p: dict) -> list:
    """这个上游声明支持的全部模型名（默认 + 更多模型）。"""
    out = []
    for m in [p.get("model")] + list(p.get("models") or []):
        m = str(m or "").strip()
        if m and m not in out:
            out.append(m)
    return out


def can_serve(p: dict, want: str) -> bool:
    """这个上游能不能处理客户端点名的模型。"""
    if not want:
        return True
    if p.get("model_passthrough"):
        return True
    w = want.strip().lower()
    wt = _tail(w)
    for m in declared_models(p):
        if m.lower() == w:
            return True
        # 允许省略 provider 前缀（deepseek-v4.1-flash-free ≈ opencode/deepseek-v4.1-flash-free）
        if _tail(m) == wt:
            return True
    return False


def pick_model(p: dict, payload: dict) -> str:
    """决定真正发给上游的模型名。

    客户端点名了就必须发它（或它在该上游的完整名），绝不悄悄换成默认模型。
    """
    mapped = str(payload.get("_mapped_model") or "").strip()
    if mapped:
        return mapped                     # 用户明确映射过，照办
    want = str(payload.get("model") or "").strip()
    if not want:
        return p["model"] or ((p.get("models") or [""])[0])

    w = want.lower()
    wt = _tail(w)
    for m in declared_models(p):
        if m.lower() == w or _tail(m) == wt:
            return m                      # 用声明里的完整名
    if p.get("model_passthrough"):
        return want
    raise ProviderError(
        f"上游「{p['name']}」没有模型 {want}。它支持："
        + "、".join(declared_models(p)[:6])
        + ("（可在上游卡片里勾「允许任意模型名」强制透传）"))


def headers_for(p: dict, stream: bool) -> dict:
    head = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream" if stream else "application/json",
    }
    if p["api_key"]:
        head["Authorization"] = "Bearer " + p["api_key"]
    ua = RUNTIME.get("user_agent") or ""
    if ua:
        head["User-Agent"] = ua
    if "openrouter.ai" in p["base_url"]:
        head["HTTP-Referer"] = SETTINGS.get("site_url", "http://fnos.local")
        head["X-Title"] = SETTINGS.get("site_name", "fnos-ai-gateway")
    return head


def chat_timeout(p: dict) -> float:
    """聊天请求用「首字节超时」，但不超过该上游自己的 timeout。"""
    return float(min(p["timeout"], RUNTIME["timeout_first_byte"]))


def _http_json(p: dict, url: str, headers: dict, body: dict) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers=headers, method="POST")
    to = chat_timeout(p)
    opener = build_opener(p.get("proxy") or RUNTIME.get("proxy") or "", to)
    try:
        if opener is not None:
            resp = opener.open(req, timeout=to)
        else:
            resp = urllib.request.urlopen(req, timeout=to)
        try:
            raw = resp.read()
        finally:
            resp.close()
    except Exception as exc:
        raise ProviderError(err_text(exc)) from None
    try:
        return json.loads(raw.decode("utf-8", "ignore"))
    except Exception:
        raise ProviderError("上游返回的不是 JSON：" +
                            raw[:160].decode("utf-8", "ignore")) from None


def _split_messages(payload: dict) -> tuple:
    system, msgs = [], []
    for m in payload.get("messages") or []:
        if not isinstance(m, dict):
            continue
        role = str(m.get("role") or "user")
        content = m.get("content")
        if isinstance(content, list):
            content = "".join(str(c.get("text") or "") if isinstance(c, dict) else str(c)
                              for c in content)
        content = str(content or "")
        if role == "system":
            system.append(content)
        elif role in ("user", "assistant"):
            msgs.append({"role": role, "content": content})
    return chr(10).join(system), msgs


def _wrap_openai(model: str, text: str, tin, tout, cid: str = "") -> dict:
    tin, tout = int(tin or 0), int(tout or 0)
    return {
        "id": cid or ("chatcmpl-" + str(int(time.time()))),
        "object": "chat.completion",
        "model": model,
        "choices": [{"index": 0,
                     "message": {"role": "assistant", "content": text},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": tin, "completion_tokens": tout,
                  "total_tokens": tin + tout},
    }


def call_responses(p: dict, payload: dict) -> dict:
    model = pick_model(p, payload)
    system, msgs = _split_messages(payload)
    items = ([{"role": "system", "content": system}] if system else []) + msgs
    body = {"model": model, "input": items}
    mt = payload.get("max_tokens") or payload.get("max_output_tokens")
    if mt:
        body["max_output_tokens"] = int(mt)
    body.update(p.get("extra") or {})
    head = {"Content-Type": "application/json"}
    if p["api_key"]:
        head["Authorization"] = "Bearer " + p["api_key"]
    data = _http_json(p, p["base_url"] + "/responses", head, body)
    text = ""
    for item in data.get("output") or []:
        if not isinstance(item, dict):
            continue
        for c in item.get("content") or []:
            if isinstance(c, dict):
                text += str(c.get("text") or "")
    u = data.get("usage") or {}
    return _wrap_openai(model, text, u.get("input_tokens"),
                        u.get("output_tokens"), str(data.get("id") or ""))


def call_anthropic(p: dict, payload: dict) -> dict:
    model = pick_model(p, payload)
    system, msgs = _split_messages(payload)
    body = {"model": model,
            "max_tokens": int(payload.get("max_tokens") or 4096),
            "messages": msgs or [{"role": "user", "content": ""}]}
    if system:
        body["system"] = system
    body.update(p.get("extra") or {})
    base = p["base_url"]
    url = base + ("/messages" if base.endswith("/v1") else "/v1/messages")
    head = {"Content-Type": "application/json", "anthropic-version": "2023-06-01"}
    if p["api_key"]:
        head["x-api-key"] = p["api_key"]
    data = _http_json(p, url, head, body)
    text = "".join(str(c.get("text") or "") for c in (data.get("content") or [])
                   if isinstance(c, dict))
    u = data.get("usage") or {}
    return _wrap_openai(model, text, u.get("input_tokens"),
                        u.get("output_tokens"), str(data.get("id") or ""))


def call_gemini(p: dict, payload: dict) -> dict:
    model = pick_model(p, payload)
    system, msgs = _split_messages(payload)
    contents = [{"role": "user" if m["role"] == "user" else "model",
                 "parts": [{"text": m["content"]}]} for m in msgs]
    body = {"contents": contents}
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    body.update(p.get("extra") or {})
    url = p["base_url"] + "/models/" + model + ":generateContent"
    head = {"Content-Type": "application/json"}
    if p["api_key"]:
        head["x-goog-api-key"] = p["api_key"]
    data = _http_json(p, url, head, body)
    text = ""
    for cand in data.get("candidates") or []:
        for part in ((cand.get("content") or {}).get("parts") or []):
            if isinstance(part, dict):
                text += str(part.get("text") or "")
    u = data.get("usageMetadata") or {}
    return _wrap_openai(model, text, u.get("promptTokenCount"),
                        u.get("candidatesTokenCount"),
                        str(data.get("responseId") or ""))


ALT_PROTOCOLS = {
    "responses": call_responses,
    "openai-responses": call_responses,
    "anthropic": call_anthropic,
    "claude": call_anthropic,
    "gemini": call_gemini,
    "google": call_gemini,
}


def sanitize_text(text: str) -> str:
    """把配置里的品牌词换成中性词。"""
    if not RUNTIME["sanitize"] or not RUNTIME["sanitize_words"]:
        return text
    out = text
    rep = RUNTIME["sanitize_replacement"]
    for w in RUNTIME["sanitize_words"]:
        if not w:
            continue
        # 大小写不敏感地替换
        idx = 0
        low, wl = out.lower(), w.lower()
        while True:
            i = low.find(wl, idx)
            if i < 0:
                break
            out = out[:i] + rep + out[i + len(w):]
            low = out.lower()
            idx = i + len(rep)
    return out


def sanitize_payload(payload: dict) -> dict:
    """按作用域改写消息内容。返回新的 payload（不改原对象）。"""
    if not RUNTIME["sanitize"] or not RUNTIME["sanitize_words"]:
        return payload
    scope = RUNTIME["sanitize_scope"] or "all"
    data = dict(payload)
    msgs = []
    for m in payload.get("messages") or []:
        if not isinstance(m, dict):
            continue
        role = str(m.get("role") or "user")
        if scope != "all" and role != scope:
            msgs.append(m)
            continue
        m2 = dict(m)
        content = m.get("content")
        if isinstance(content, list):
            m2["content"] = [
                ({**c, "text": sanitize_text(str(c.get("text") or ""))}
                 if isinstance(c, dict) and "text" in c else c)
                for c in content]
        elif content is not None:
            m2["content"] = sanitize_text(str(content))
        msgs.append(m2)
    data["messages"] = msgs
    return data


def render_prompt(payload: dict) -> str:
    msgs = payload.get("messages") or []
    if len(msgs) == 1 and str(msgs[0].get("role")) == "user":
        content = msgs[0].get("content")
        if isinstance(content, str):
            return content
    out = []
    for m in msgs:
        role = str(m.get("role") or "user")
        content = m.get("content")
        if isinstance(content, list):
            content = " ".join(str(x.get("text") or "") for x in content
                               if isinstance(x, dict))
        prefix = "" if role == "user" else f"[{role}] "
        out.append(prefix + str(content or ""))
    return chr(10).join(out)


def call_opencode_cli(p: dict, payload: dict) -> tuple:
    bin_path, ws, env = SUPERVISOR.cli_context()
    if not bin_path:
        raise ProviderError("没找到 opencode 可执行文件：先在设置页点「一键安装」")

    prompt = render_prompt(payload)
    model = pick_model(p, payload)
    model_arg = model if "/" in model else \
        (p.get("opencode_provider") or "opencode") + "/" + model

    # --standalone：用私有服务；共享服务被网关占着，连不上会 ECONNREFUSED
    args = [bin_path, "run", "--standalone", "--model", model_arg]
    agent = p.get("opencode_agent") or ""
    if agent:
        args += ["--agent", agent]
    args.append(prompt)

    t0 = now()
    try:
        proc = subprocess.run(args, capture_output=True, timeout=p["timeout"],
                              env=env, cwd=ws)
    except subprocess.TimeoutExpired:
        raise ProviderError(f"opencode run 超时（{p['timeout']}s）") from None
    except Exception as exc:
        raise ProviderError(f"启动 opencode run 失败：{exc}") from None

    out = strip_ansi(proc.stdout.decode("utf-8", "ignore")).strip()
    err = strip_ansi(proc.stderr.decode("utf-8", "ignore")).strip()
    ms = int((now() - t0) * 1000)

    blob = (out + " " + err).lower()
    if "usage limit" in blob or "freeusage" in blob:
        raise ProviderError(
            "OpenCode 免费额度已用尽（服务端返回 FreeUsageLimitError）。"
            "免费模型有每日配额，用尽后需等重置或改用有 Key 的上游。")

    if proc.returncode != 0:
        raise ProviderError(f"opencode run 退出码 {proc.returncode}：{(err or out)[:300]}")
    if not out:
        raise ProviderError("opencode run 没有输出")

    lines = list(out.split(chr(10)))
    while lines and (lines[0].startswith(">") or not lines[0].strip()):
        lines.pop(0)
    text = chr(10).join(lines).strip()

    data = _wrap_openai(model, text, max(1, len(prompt) // 3),
                        max(1, len(text) // 3))
    data["_gateway_ms"] = ms
    data["_upstream"] = model_arg
    return data, ""


def call_provider(p: dict, payload: dict) -> tuple:
    payload = sanitize_payload(payload)
    if p["type"] == "opencode":
        if (p.get("opencode_mode") or "server").lower() == "cli":
            return call_opencode_cli(p, payload)
        SUPERVISOR.ensure()
        client = OpenCodeClient(
            base_url=p["base_url"], model_id=p["model"],
            provider_id=p["opencode_provider"], agent=p["opencode_agent"],
            tools=p["opencode_tools"], timeout=p["timeout"],
            extra=p["opencode_extra"])
        try:
            data = client.chat(render_prompt(payload))
        except OpenCodeError as exc:
            raise ProviderError(str(exc)) from None
        upstream = str(data.pop("_upstream_model", "") or data.get("model") or "")
        data["_upstream"] = pick_model(p, payload)
        return data, check_model(p, {"model": upstream})

    proto = (p.get("protocol") or "").lower()
    if proto in ALT_PROTOCOLS:
        data = ALT_PROTOCOLS[proto](p, payload)
        data["_upstream"] = pick_model(p, payload)
        return data, check_model(p, data)

    stream = bool(payload.get("stream"))
    body = dict(payload)
    body["model"] = pick_model(p, payload)
    if p["extra"]:
        body.update(p["extra"])
    if stream:
        body.setdefault("stream_options", {"include_usage": True})

    req = urllib.request.Request(
        p["base_url"] + "/chat/completions",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers=headers_for(p, stream), method="POST")
    to = chat_timeout(p)
    opener = build_opener(p.get("proxy") or RUNTIME.get("proxy") or "", to)
    try:
        if opener is not None:
            resp = opener.open(req, timeout=to)
        else:
            resp = urllib.request.urlopen(req, timeout=to)
        try:
            raw = resp.read()
        finally:
            resp.close()
    except Exception as exc:
        raise ProviderError(err_text(exc)) from None
    try:
        data = json.loads(raw.decode("utf-8", "ignore"))
    except Exception:
        raise ProviderError("上游返回的不是 JSON：" +
                            raw[:160].decode("utf-8", "ignore")) from None
    data["_upstream"] = body["model"]
    return data, check_model(p, data)


def check_model(p: dict, data: dict) -> str:
    want = p.get("model_contains") or ""
    if not want:
        return ""
    got = str(data.get("model") or "")
    if want.lower() in got.lower():
        return ""
    note = f"model 校验失败：期望包含 {want}，实际 {got or '空'}"
    if p.get("strict_model"):
        raise ProviderError(note)
    return note


# ─────────────────────────── 路由 ───────────────────────────
def skip_report() -> list:
    used = used_tokens()
    out = []
    for p in PROVIDERS:
        if not p["enabled"]:
            out.append(f"{p['name']}：未启用")
            continue
        if not (p["api_key"] or p["type"] == "opencode" or p["no_auth"]):
            out.append(f"{p['name']}：缺密钥")
            continue
        cd = STATE.get(p["name"], {}).get("cooldown_until", 0)
        if cd > now():
            out.append(f"{p['name']}：冷却中，还剩 {int(cd - now())} 秒")
            continue
        if is_exhausted(p, used):
            out.append(f"{p['name']}：免费额度已用尽")
            continue
        out.append(f"{p['name']}：可用")
    return out


def all_models() -> list:
    """全站能点的模型名（去重，保持顺序）。"""
    seen, out = set(), []
    for p in PROVIDERS:
        for m in declared_models(p):
            if m.lower() not in seen:
                seen.add(m.lower())
                out.append(m)
    return out


def candidates(strategy: str, want_model: str = "") -> list:
    used = used_tokens()
    with STATE_LOCK:
        live = [
            p for p in PROVIDERS
            if p["enabled"]
            and (p["api_key"] or p["type"] == "opencode" or p["no_auth"])
            and STATE.get(p["name"], {}).get("cooldown_until", 0) <= now()
            and not is_exhausted(p, used)
        ]
    if want_model:
        # 点名了模型：只留真能处理它的上游，不拿默认模型凑数
        live = [p for p in live if can_serve(p, want_model)]
    if strategy == "cheapest":
        live.sort(key=lambda p: (p["price_in"] + p["price_out"], p["priority"]))
    else:
        live.sort(key=lambda p: p["priority"])
    if want_model:
        # 声明里精确有的排前面（比 passthrough 更可信）
        w = want_model.strip().lower()
        live.sort(key=lambda p: 0 if any(m.lower() == w for m in declared_models(p)) else 1)
    return live


def mark_ok(p: dict) -> None:
    with STATE_LOCK:
        STATE[p["name"]] = {"cooldown_until": 0, "fails": 0}


def mark_fail(p: dict) -> None:
    with STATE_LOCK:
        st = STATE.setdefault(p["name"], {})
        st["fails"] = st.get("fails", 0) + 1
        st["cooldown_until"] = now() + RUNTIME["cooldown"]


# ─────────────────────────── HTTP ───────────────────────────
def read_web(name: str) -> bytes:
    try:
        with open(os.path.join(WEB_DIR, os.path.basename(name)), "rb") as fh:
            return fh.read()
    except Exception:
        return b""


class Handler(BaseHTTPRequestHandler):
    server_version = "fnos-ai-gateway"

    def log_message(self, *args):
        pass

    def _path(self) -> str:
        path = urllib.parse.urlparse(self.path).path or "/"
        if GATEWAY_PREFIX and path.startswith(GATEWAY_PREFIX):
            path = path[len(GATEWAY_PREFIX):] or "/"
        return path

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers",
                         "Authorization, Content-Type, X-Api-Key")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        for k, v in (extra or {}).items():
            self.send_header(k, safe_header(v))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def send_json(self, code: int, obj, extra: dict | None = None) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8", extra)

    def _body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
            data = json.loads(self.rfile.read(n) or b"{}")
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _page(self) -> None:
        html = read_web("admin.html")
        if not html:
            html = b"<h1>web/admin.html missing</h1>"
        self._send(200, html, "text/html; charset=utf-8")

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers",
                         "Authorization, Content-Type, X-Api-Key")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        self._guard(self._get)

    def do_POST(self) -> None:
        self._guard(self._post)

    def _guard(self, fn) -> None:
        try:
            fn()
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:                      # noqa: BLE001
            try:
                self.send_json(500, {"error": {"message": str(exc)[:300],
                                               "type": "internal_error"}})
            except Exception:
                pass

    def _get(self) -> None:
        path = self._path()
        if path == "/healthz":
            self.send_json(200, {"ok": True, "providers": len(PROVIDERS)})
            return
        if path in ("/", "/admin", "/admin/"):
            self._page()
            return
        if path.startswith("/static/"):
            name = os.path.basename(path)
            ctype = ("text/css; charset=utf-8" if name.endswith(".css")
                     else "application/javascript; charset=utf-8")
            self._send(200, read_web(name), ctype)
            return
        if path.startswith("/settings") or path == "/logs":
            self._page()
            return
        if path == "/v1/models":
            ok, _tok, reason = auth_v1(self)
            if not ok:
                self.send_json(401, {"error": {"message": reason,
                                               "type": "unauthorized"}})
                return
            seen, data = set(), []
            for p in PROVIDERS:
                for mid in declared_models(p):
                    key = mid.lower()
                    if key in seen:
                        continue
                    seen.add(key)
                    data.append({"id": mid, "object": "model", "owned_by": p["name"]})
            self.send_json(200, {"object": "list", "data": data})
            return
        if path == "/admin/api/config":
            if not is_admin(self):
                self.send_json(401, {"error": "unauthorized"})
                return
            self.send_json(200, config_snapshot())
            return
        if path == "/admin/api/status":
            if not is_admin(self):
                self.send_json(401, {"error": "unauthorized"})
                return
            self.send_json(200, status_payload())
            return
        if path == "/admin/api/logs":
            self._logs()
            return
        if path == "/admin/api/errors":
            if not is_admin(self):
                self.send_json(401, {"error": "unauthorized"})
                return
            self.send_json(200, errors_payload())
            return
        if path == "/admin/api/caps":
            if not is_admin(self):
                self.send_json(401, {"error": "unauthorized"})
                return
            self.send_json(200, caps_payload())
            return
        if path == "/admin/api/stats":
            if not is_admin(self):
                self.send_json(401, {"error": "unauthorized"})
                return
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try:
                days = int((qs.get("days") or ["7"])[0])
            except Exception:
                days = 7
            self.send_json(200, stats_payload(days))
            return
        if path == "/admin/api/tokens":
            if not is_admin(self):
                self.send_json(401, {"error": "unauthorized"})
                return
            self.send_json(200, tokens_payload())
            return
        if path == "/admin/api/opencode":
            if not is_admin(self):
                self.send_json(401, {"error": "unauthorized"})
                return
            self.send_json(200, SUPERVISOR.status())
            return
        self.send_json(404, {"error": "not found"})

    def _post(self) -> None:
        path = self._path()
        if path == "/v1/chat/completions":
            self._chat()
            return
        body = self._body()

        if path == "/admin/api/opencode":
            self._opencode(str(body.get("action") or ""), body)
            return
        if path == "/admin/api/tokens":
            self._tokens(body)
            return

        if not is_admin(self):
            self.send_json(401, {"error": "unauthorized"})
            return

        if path == "/admin/api/config":
            try:
                snap = config_save(body)
            except ValueError as exc:
                self.send_json(400, {"ok": False, "error": str(exc)})
                return
            self.send_json(200, snap)
            return
        if path == "/admin/api/save-provider":
            raw = body.get("provider") or {}
            if not isinstance(raw, dict):
                self.send_json(400, {"ok": False, "error": "provider 必须是对象"})
                return
            try:
                for env, val in (body.get("secrets") or {}).items():
                    save_secret(str(env), str(val or ""))
                snap = save_one_provider(raw)
            except Exception as exc:                   # noqa: BLE001
                self.send_json(400, {"ok": False, "error": str(exc)})
                return
            self.send_json(200, {"ok": True, **snap})
            return
        if path == "/admin/api/toggle":
            name = str(body.get("name") or "")
            enabled = bool(body.get("enabled"))
            if not any(p["name"] == name for p in PROVIDERS):
                self.send_json(404, {"ok": False, "error": "unknown provider"})
                return
            self.send_json(200, {"ok": set_provider_enabled(name, enabled),
                                 "name": name, "enabled": enabled})
            return
        if path == "/admin/api/reload":
            load_config()
            self.send_json(200, {"ok": True})
            return
        if path == "/admin/api/probe":
            self._probe(str(body.get("name") or ""))
            return
        if path == "/admin/api/discover":
            p = next((x for x in PROVIDERS if x["name"] == str(body.get("name") or "")), None)
            if p is None:
                self.send_json(404, {"ok": False, "error": "unknown provider"})
                return
            self.send_json(200, discover_models(p))
            return
        if path == "/admin/api/set-key":
            name = str(body.get("name") or "")
            key = str(body.get("key") or "").strip()
            p = next((x for x in PROVIDERS if x["name"] == name), None)
            if p is None:
                self.send_json(404, {"ok": False, "error": "unknown provider"})
                return
            env = p.get("key_env") or ""
            if not env:
                self.send_json(400, {"ok": False, "error": "该上游没有 api_key_env，无法写入密钥"})
                return
            save_secret(env, key)
            load_config()
            self.send_json(200, {"ok": True, "name": name, "key_env": env})
            return
        if path == "/admin/api/verify-key":
            self._verify_key(str(body.get("key") or "").strip())
            return
        if path == "/admin/api/proxy-test":
            self.send_json(200, test_proxy(str(body.get("proxy") or "")))
            return
        if path == "/admin/api/set-settings":
            self._set_settings(body)
            return
        self.send_json(404, {"error": "not found"})

    # ---- 管理员操作 ----
    def _set_settings(self, body: dict) -> None:
        patch = {}
        for key in ("proxy", "strategy", "cooldown_seconds", "gateway_token"):
            if key in body:
                patch[key] = body[key]
        if "cooldown_seconds" in patch:
            try:
                patch["cooldown_seconds"] = max(1, min(int(patch["cooldown_seconds"]), 3600))
            except Exception:
                self.send_json(400, {"ok": False, "error": "冷却必须是整数秒"})
                return
        if "strategy" in patch:
            s = str(patch["strategy"]).lower()
            if s not in ("priority", "cheapest"):
                self.send_json(400, {"ok": False, "error": "策略只能是 priority / cheapest"})
                return
            patch["strategy"] = s
        for key in ("opencode_port", "opencode_bin", "opencode_workspace"):
            if key in body:
                patch[key] = body[key]
        if "opencode_autostart" in body:
            patch["opencode_autostart"] = bool(body["opencode_autostart"])
        if "opencode_port" in patch:
            try:
                patch["opencode_port"] = max(1, min(int(patch["opencode_port"]), 65535))
            except Exception:
                self.send_json(400, {"ok": False, "error": "端口必须是整数"})
                return
        save_settings(patch)
        load_config()
        self.send_json(200, {"ok": True})

    def _opencode(self, action: str, body: dict) -> None:
        if not is_admin(self):
            self.send_json(401, {"error": "unauthorized"})
            return
        if action == "start":
            ok, msg = SUPERVISOR.start()
        elif action == "stop":
            ok, msg = SUPERVISOR.stop()
        elif action == "install":
            ok, msg = SUPERVISOR.install()
        elif action == "login":
            ok, msg = SUPERVISOR.start_login("")
        elif action == "login-log":
            ok, msg = True, "已刷新"
        elif action == "login-keys":
            ok, msg = SUPERVISOR.send_login(str(body.get("keys") or ""))
        elif action == "diagnose":
            base = "http://127.0.0.1:%d" % int(opencode_opts().get("port") or 4096)
            client = OpenCodeClient(base_url=base, model_id="", timeout=10)
            payload = SUPERVISOR.status()
            payload.update(ok=True, message="诊断完成", diag=client.diagnose())
            self.send_json(200, payload)
            return
        elif action == "ping":
            self._opencode_ping()
            return
        else:
            self.send_json(400, {"ok": False, "error": "未知操作"})
            return
        payload = SUPERVISOR.status()
        payload.update(ok=ok, message=msg)
        self.send_json(200, payload)

    def _opencode_ping(self) -> None:
        ops = [x for x in PROVIDERS if x["type"] == "opencode"]
        pick = None
        if ops:
            on = [x for x in ops if x["enabled"]] or ops
            pick = min(on, key=lambda x: x["priority"])
        bin_path = SUPERVISOR.resolve_bin()
        tag = ""
        if bin_path:
            tag = os.path.basename(bin_path) + " " + (SUPERVISOR._version(bin_path) or "?")

        payload = SUPERVISOR.status()
        if pick and (pick.get("opencode_mode") or "server").lower() == "cli":
            try:
                data, _w = call_opencode_cli(
                    {**pick, "timeout": 120},
                    {"messages": [{"role": "user", "content": "回复两个字：OK"}],
                     "model": pick["model"]})
                text = str(data["choices"][0]["message"]["content"] or "")
                payload.update(ok=True, message=f"试通了（cli / {tag}）：{text[:80]}",
                               probe={"mode": "cli", "bin": tag, "text": text[:400]})
            except Exception as exc:                   # noqa: BLE001
                payload.update(ok=False, message=f"试失败（cli / {tag}）：{str(exc)[:300]}",
                               probe={"mode": "cli", "bin": tag, "error": str(exc)[:400]})
            self.send_json(200, payload)
            return

        base = "http://127.0.0.1:%d" % int(opencode_opts().get("port") or 4096)
        client = OpenCodeClient(base_url=base, model_id=(pick or {}).get("model") or "",
                                provider_id=(pick or {}).get("opencode_provider") or "opencode",
                                agent=(pick or {}).get("opencode_agent") or "", timeout=30)
        try:
            probe = client.probe_message("回复两个字：OK", timeout=25)
        except Exception as exc:                       # noqa: BLE001
            probe = {"steps": [{"step": "fatal", "ok": False, "error": str(exc)[:300]}]}
        sent = next((x for x in (probe.get("steps") or [])
                     if x.get("step") == "send_message"), {})
        ok = bool(sent.get("ok"))
        payload.update(ok=ok, probe=probe,
                       message=("试通了" if ok else "试失败：" + str(sent.get("error") or ""))[:300])
        self.send_json(200, payload)

    def _probe(self, name: str) -> None:
        p = next((x for x in PROVIDERS if x["name"] == name), None)
        if p is None:
            self.send_json(404, {"ok": False, "error": "unknown provider"})
            return
        if not p["api_key"] and p["type"] != "opencode" and not p["no_auth"]:
            self.send_json(200, {"ok": False, "provider": name,
                                 "error": "没有密钥：在卡片里填一个"})
            return
        if p["type"] == "opencode" and \
                (p.get("opencode_mode") or "server").lower() == "server":
            if not quick_port_check(p["base_url"]):
                self.send_json(200, {"ok": False, "provider": name, "ms": 0,
                                     "error": "连不上 opencode server（%s）：它没在运行。"
                                              "设置页「OpenCode」里点启动，"
                                              "或把调用方式改成命令行。" % p["base_url"]})
                return
        t0 = now()
        try:
            data, warn = call_provider(p, {"messages": [{"role": "user", "content": "hi"}],
                                           "max_tokens": 16})
            try:
                text = str(data["choices"][0]["message"]["content"] or "")
            except Exception:
                text = str(data)[:200]
            ms = int((now() - t0) * 1000)
            mark_ok(p)
            record(p["name"], p["model"], 0, 0, 0.0, True, ms, warn)
            self.send_json(200, {"ok": True, "provider": name, "ms": ms,
                                 "sample": text[:400], "warning": warn})
        except Exception as exc:                       # noqa: BLE001
            ms = int((now() - t0) * 1000)
            mark_fail(p)
            record(p["name"], p["model"], 0, 0, 0.0, False, ms, err_text(exc))
            self.send_json(200, {"ok": False, "provider": name, "ms": ms,
                                 "error": err_text(exc)[:400]})

    def _verify_key(self, key: str) -> None:
        if not key:
            self.send_json(200, {"ok": False, "error": "没有输入 Key"})
            return
        master = RUNTIME["gateway_token"]
        if master and key == master:
            self.send_json(200, {"ok": True, "kind": "主口令",
                                 "note": "这是访问口令，可以直接用"})
            return
        tok = find_token(key)
        if tok:
            quota = int(tok.get("quota_tokens") or 0)
            used = token_usage().get(str(tok.get("id", "")), 0)
            if quota > 0 and used >= quota:
                self.send_json(200, {"ok": False, "kind": "令牌",
                                     "error": f"令牌「{tok.get('name','')}」额度已用尽"
                                              f"（{used}/{quota}）"})
                return
            self.send_json(200, {"ok": True, "kind": "令牌", "name": tok.get("name", ""),
                                 "quota_tokens": quota, "used_tokens": used})
            return
        tokens = load_tokens()
        if not master and not [t for t in tokens if t.get("enabled", True)]:
            self.send_json(200, {"ok": True, "kind": "全开放模式",
                                 "note": "没设口令也没有启用的令牌，任何 Key 都能用"})
            return
        hint = "这把 Key 不认识。"
        if "*" in key:
            hint += "看起来像掩码（带 ****），点「复制」拿完整值。"
        self.send_json(200, {"ok": False, "error": hint})

    def _tokens(self, body: dict) -> None:
        if not is_admin(self):
            self.send_json(401, {"error": "unauthorized"})
            return
        action = str(body.get("action") or "")
        tokens = load_tokens()
        if action == "create":
            tok = {
                "id": "tok_" + base64.b32encode(os.urandom(8)).decode().rstrip("=").lower(),
                "name": str(body.get("name") or "未命名")[:60],
                "key": gen_token_key(),
                "enabled": True,
                "quota_tokens": max(0, int(body.get("quota_tokens") or 0)),
                "created": int(now()),
            }
            tokens.append(tok)
            save_tokens(tokens)
            self.send_json(200, {"ok": True, "token": tok})
            return
        if action == "toggle":
            for t in tokens:
                if str(t.get("id")) == str(body.get("id")):
                    t["enabled"] = bool(body.get("enabled"))
            save_tokens(tokens)
            self.send_json(200, {"ok": True})
            return
        if action == "delete":
            save_tokens([t for t in tokens if str(t.get("id")) != str(body.get("id"))])
            self.send_json(200, {"ok": True})
            return
        if action == "reveal":
            for t in tokens:
                if str(t.get("id")) == str(body.get("id")):
                    self.send_json(200, {"ok": True, "key": t.get("key", "")})
                    return
            self.send_json(404, {"ok": False, "error": "令牌不存在"})
            return
        self.send_json(400, {"ok": False, "error": "未知操作"})

    def _logs(self) -> None:
        if not is_admin(self):
            self.send_json(401, {"error": "unauthorized"})
            return
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        kind = (qs.get("kind") or ["usage"])[0]
        try:
            limit = max(1, min(int((qs.get("limit") or ["200"])[0]), 1000))
        except Exception:
            limit = 200
        if kind == "gateway":
            path = os.path.join(DATA_DIR, "app.log")
            self.send_json(200, {"ok": True, "kind": "gateway", "path": path,
                                 "text": tail_file(path, limit)})
            return
        names = {str(t.get("id", "")): str(t.get("name", "")) for t in load_tokens()}
        rows = []
        try:
            conn = sqlite3.connect(DB_PATH, timeout=5)
            cur = conn.execute(
                "SELECT ts, provider, model, tokens_in, tokens_out, cost, ok, ms,"
                " err, token_id, first_byte_ms, stream, cache_hit, cache_miss,"
                " client_ip, user_agent, effort, upstream_model, request_id, status"
                " FROM usage ORDER BY ts DESC LIMIT ?", (limit,))
            for r in cur.fetchall():
                tin, tout = int(r[3] or 0), int(r[4] or 0)
                ms, fb = int(r[7] or 0), int(r[10] or 0)
                gen_ms = max(1, ms - fb) if fb else ms
                rows.append({
                    "ts": float(r[0] or 0),
                    "time": time.strftime("%m-%d %H:%M:%S", time.localtime(r[0] or 0)),
                    "provider": r[1] or "",
                    "model": r[2] or "",
                    "tokens_in": tin,
                    "tokens_out": tout,
                    "cost": float(r[5] or 0),
                    "ok": bool(r[6]),
                    "status": int(r[19] or (200 if r[6] else 502)),
                    "ms": ms,
                    "first_byte_ms": fb,
                    "tps": round(tout / (gen_ms / 1000.0), 1) if tout and gen_ms else 0,
                    "stream": bool(r[11]),
                    "cache_hit": int(r[12] or 0),
                    "cache_miss": int(r[13] or 0),
                    "cache_rate": (round(int(r[12] or 0) * 100.0 / tin, 2)
                                   if tin else 0),
                    "client_ip": r[14] or "",
                    "user_agent": r[15] or "",
                    "effort": r[16] or "",
                    "upstream_model": r[17] or "",
                    "request_id": r[18] or "",
                    "err": r[8] or "",
                    "token": names.get(str(r[9] or ""), "（主口令）" if not r[9] else "已删除"),
                })
            conn.close()
        except Exception as exc:                       # noqa: BLE001
            self.send_json(200, {"ok": False, "error": str(exc)})
            return
        self.send_json(200, {"ok": True, "kind": "usage", "rows": rows})

    def _chat(self) -> None:
        ok, tok, reason = auth_v1(self)
        if not ok:
            code = 429 if "额度已用尽" in reason else 401
            self.send_json(code, {"error": {"message": reason, "type": "unauthorized"}})
            return
        _CURRENT.token_id = str((tok or {}).get("id", ""))
        payload = self._body()
        if not payload:
            self.send_json(400, {"error": {"message": "invalid JSON body",
                                           "type": "bad_request"}})
            return

        strategy = (self.headers.get("X-Route-Strategy")
                    or RUNTIME["strategy"] or "priority").lower()
        if strategy not in ("priority", "cheapest"):
            strategy = "priority"

        # 客户端原始点名的模型，响应里要原样回显
        original_model = str(payload.get("model") or "").strip()
        payload, err = apply_request_policy(payload)
        if err:
            self.send_json(err[0], {"error": {"message": err[1],
                                              "type": "policy_rejected"}})
            return
        want = str(payload.get("_route_model") or payload.get("model") or "").strip()

        payload["_echo_model"] = original_model
        if payload.get("stream"):
            self._stream(payload, strategy, want)
        else:
            self._nonstream(payload, strategy, want)

    def _nonstream(self, payload: dict, strategy: str, want: str) -> None:
        tried, last = [], None
        for p in candidates(strategy, want):
            tried.append(p["name"])
            t0 = now()
            try:
                data, warn = call_provider(p, payload)
                u = usage_of(data)
                ms = int((now() - t0) * 1000)
                record(p["name"], payload.get("_echo_model") or p["model"],
                       u["in"], u["out"], cost_of(p, u["in"], u["out"]),
                       True, ms, warn, extra={
                           "first_byte_ms": ms,
                           "stream": 0,
                           "cache_hit": u["cache_hit"],
                           "cache_miss": u["cache_miss"],
                           "client_ip": client_ip(self),
                           "user_agent": self.headers.get("User-Agent") or "",
                           "effort": str(payload.get("reasoning_effort") or ""),
                           "upstream_model": str(data.get("_upstream")
                                                 or p["model"]),
                           "request_id": str(data.get("id") or ""),
                           "status": 200,
                       })
                mark_ok(p)
                echo = payload.get("_echo_model") or ""
                if echo:
                    data["model"] = echo
                extra = {"X-Gateway-Provider": p["name"],
                         "X-Gateway-Upstream-Model": str(
                             data.pop("_upstream", "") or p["model"])}
                if warn:
                    extra["X-Gateway-Warning"] = "model-mismatch"
                caps = payload.get("_caps") or {}
                if caps:
                    extra["X-Gateway-Context"] = str(caps.get("context") or "")
                self.send_json(200, data, extra)
                return
            except Exception as exc:                   # noqa: BLE001
                msg = err_text(exc)
                mark_fail(p)
                record(p["name"], payload.get("_echo_model") or p["model"],
                       0, 0, 0.0, False, int((now() - t0) * 1000), msg,
                       extra={
                           "client_ip": client_ip(self),
                           "user_agent": self.headers.get("User-Agent") or "",
                           "effort": str(payload.get("reasoning_effort") or ""),
                           "stream": 1 if payload.get("stream") else 0,
                           "status": 502,
                       })
                last = msg

        if not tried:
            if want and not candidates(strategy, want):
                avail = all_models()
                head = (f"没有上游支持模型 {want}。可用模型："
                        + "、".join(avail[:12])
                        + ("…" if len(avail) > 12 else "")
                        + "　（在上游卡片里勾「允许任意模型名」可以强制透传）")
                self.send_json(400, {"error": {"message": head,
                                               "type": "model_not_found"}})
                return
            head = "没有可用上游。" + "；".join(skip_report()[:8])
        else:
            head = f"所有上游均失败：{last}（已尝试：{', '.join(tried)}）"
        self.send_json(502, {"error": {"message": head, "type": "gateway_error"}})

    def _stream(self, payload: dict, strategy: str, want: str) -> None:
        for p in candidates(strategy, want):
            t0 = now()
            proto = (p.get("protocol") or "").lower()
            # 真流式：OpenAI 兼容的上游直接透传 SSE
            if p["type"] == "http" and proto not in ALT_PROTOCOLS:
                info = {}
                if self._passthrough(p, payload, info):
                    ms = int((now() - t0) * 1000)
                    u = info.get("usage") or {}
                    tin = int(u.get("prompt_tokens") or 0)
                    tout = int(u.get("completion_tokens") or 0)
                    record(p["name"], payload.get("_echo_model") or p["model"],
                           tin, tout, cost_of(p, tin, tout), True, ms, "",
                           extra={
                               "first_byte_ms": int(info.get("first_byte_ms") or 0),
                               "stream": 1,
                               "cache_hit": int(info.get("cache_hit") or 0),
                               "cache_miss": int(info.get("cache_miss") or 0),
                               "client_ip": client_ip(self),
                               "user_agent": self.headers.get("User-Agent") or "",
                               "effort": str(payload.get("reasoning_effort") or ""),
                               "upstream_model": str(info.get("model") or p["model"]),
                               "request_id": str(info.get("id") or ""),
                               "status": 200,
                           })
                    mark_ok(p)
                    return
                mark_fail(p)
                record(p["name"], p["model"], 0, 0, 0.0, False,
                       int((now() - t0) * 1000), "流式转发失败")
                continue
            try:
                data, warn = call_provider(p, payload)
            except Exception as exc:                   # noqa: BLE001
                mark_fail(p)
                record(p["name"], p["model"], 0, 0, 0.0, False,
                       int((now() - t0) * 1000), err_text(exc))
                continue
            self._emit(data, p, warn)
            u = data.get("usage") or {}
            record(p["name"], p["model"], int(u.get("prompt_tokens") or 0),
                   int(u.get("completion_tokens") or 0),
                   cost_of(p, int(u.get("prompt_tokens") or 0),
                           int(u.get("completion_tokens") or 0)),
                   True, int((now() - t0) * 1000), warn)
            mark_ok(p)
            return
        if want and not candidates(strategy, want):
            avail = all_models()
            head = (f"没有上游支持模型 {want}。可用模型："
                    + "、".join(avail[:12])
                    + ("…" if len(avail) > 12 else ""))
            self.send_json(400, {"error": {"message": head,
                                           "type": "model_not_found"}})
            return
        head = "没有可用上游。" + "；".join(skip_report()[:8])
        self.send_json(502, {"error": {"message": head, "type": "gateway_error"}})

    def _passthrough(self, p: dict, payload: dict, info: dict | None = None) -> bool:
        """把上游的 SSE 原样转发给客户端。info 收集首字时间、用量、模型名。"""
        info = info if info is not None else {}
        body = dict(payload)
        body["model"] = pick_model(p, payload)
        body["stream"] = True
        if p["extra"]:
            body.update(p["extra"])
        body.setdefault("stream_options", {"include_usage": True})
        req = urllib.request.Request(
            p["base_url"] + "/chat/completions",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers=headers_for(p, True), method="POST")
        to = chat_timeout(p)
        opener = build_opener(p.get("proxy") or RUNTIME.get("proxy") or "", to)
        try:
            if opener is not None:
                resp = opener.open(req, timeout=to)
            else:
                resp = urllib.request.urlopen(req, timeout=to)
        except Exception:
            return False

        CRLF = bytes([13, 10])
        t0_pb = now()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("X-Gateway-Provider", p["name"])
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        idle = float(RUNTIME["timeout_stream_idle"])
        try:
            sk = getattr(getattr(resp, "fp", None), "raw", None)
            if sk is not None and hasattr(sk, "_sock"):
                sk._sock.settimeout(idle)
        except Exception:
            pass
        first = 0
        try:
            while True:
                line = resp.readline()
                if not line:
                    break
                if not first:
                    first = int((now() - t0_pb) * 1000)
                txt = line.decode("utf-8", "ignore")
                if txt.startswith("data:") and '"usage"' in txt:
                    try:
                        obj = json.loads(txt[5:].strip())
                        u = obj.get("usage") or {}
                        if u:
                            info["usage"] = u
                            det = u.get("prompt_tokens_details") or {}
                            info["cache_hit"] = int(det.get("cached_tokens") or 0)
                        if obj.get("model"):
                            info["model"] = obj["model"]
                        if obj.get("id"):
                            info["id"] = obj["id"]
                    except Exception:
                        pass
                head = ("%x" % len(line)).encode()
                self.wfile.write(head + CRLF + line + CRLF)
                self.wfile.flush()
            self.wfile.write(b"0" + CRLF + CRLF)
            self.wfile.flush()
            info["first_byte_ms"] = first
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            pass
        finally:
            try:
                resp.close()
            except Exception:
                pass
        return True

    def _emit(self, data: dict, p: dict, warn: str) -> None:
        try:
            text = str(data["choices"][0]["message"]["content"] or "")
        except Exception:
            text = ""
        model = data.get("model") or p["model"]
        cid = data.get("id") or f"chatcmpl-{int(now())}"
        echo = ""
        try:
            echo = str(data.get("_echo_model") or "")
        except Exception:
            echo = ""
        if echo:
            model = echo

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("X-Gateway-Provider", p["name"])
        self.send_header("X-Gateway-Stream", "buffered")
        self.send_header("Access-Control-Allow-Origin", "*")
        if warn:
            self.send_header("X-Gateway-Warning", "model-mismatch")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        def chunk(obj) -> None:
            raw = ("data: " + json.dumps(obj, ensure_ascii=False) + chr(10) + chr(10)).encode()
            self.wfile.write(("%x" % len(raw)).encode() + b"\r\n" + raw + b"\r\n")
            self.wfile.flush()

        try:
            step = 24
            for i in range(0, max(1, len(text)), step):
                chunk({"id": cid, "object": "chat.completion.chunk", "model": model,
                       "choices": [{"index": 0, "delta": {"content": text[i:i + step]}}]})
            chunk({"id": cid, "object": "chat.completion.chunk", "model": model,
                   "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                   "usage": data.get("usage") or {}})
            raw = ("data: [DONE]" + chr(10) + chr(10)).encode()
            self.wfile.write(("%x" % len(raw)).encode() + b"\r\n" + raw + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass


# ─────────────────────────── 配置读写 ───────────────────────────
def config_snapshot() -> dict:
    secrets = load_secrets()
    resolved = {p["name"]: p for p in PROVIDERS}
    provs = []
    for path in provider_files():
        data = read_json(path, {})
        src = ("main" if os.path.abspath(path) == os.path.abspath(CONFIG_PATH)
               else os.path.basename(path))
        for it in (data.get("providers") or []):
            item = dict(it)
            item["_source"] = src
            r = resolved.get(str(it.get("name", "")))
            item["_has_key"] = bool(r and r["api_key"])
            item["_key_source"] = provider_key_source(r, secrets) if r else "无"
            provs.append(item)
    return {
        "strategy": RUNTIME["strategy"],
        "cooldown_seconds": RUNTIME["cooldown"],
        "port": PORT,
        "proxy": RUNTIME["proxy"],
        "proxy_env": (os.getenv("HTTPS_PROXY") or os.getenv("https_proxy")
                      or os.getenv("HTTP_PROXY") or os.getenv("http_proxy") or ""),
        "opencode": {**opencode_opts(),
                     "key_set": bool(str(secrets.get("OPENCODE_API_KEY") or "").strip())},
        "auth_enabled": bool(RUNTIME["gateway_token"]),
        "sanitize": RUNTIME["sanitize"],
        "sanitize_scope": RUNTIME["sanitize_scope"],
        "sanitize_words": "、".join(RUNTIME["sanitize_words"]),
        "sanitize_replacement": RUNTIME["sanitize_replacement"],
        "timeout_short": RUNTIME["timeout_short"],
        "timeout_first_byte": RUNTIME["timeout_first_byte"],
        "timeout_stream_idle": RUNTIME["timeout_stream_idle"],
        "user_agent": RUNTIME["user_agent"],
        "model_blacklist": RUNTIME["model_blacklist"],
        "model_whitelist": RUNTIME["model_whitelist"],
        "model_mapping": RUNTIME["model_mapping"],
        "model_caps": RUNTIME["model_caps"],
        "system_mode": RUNTIME["system_mode"],
        "system_text": RUNTIME["system_text"],
        "system_file": RUNTIME["system_file"],
        "keep_rounds": RUNTIME["keep_rounds"],
        "effort_enabled": RUNTIME["effort_enabled"],
        "effort_default": RUNTIME["effort_default"],
        "effort_mode": RUNTIME["effort_mode"],
        "log_source": RUNTIME["log_source"],
        "providers": provs,
    }


def config_save(body: dict) -> dict:
    patch = {}
    if "strategy" in body:
        s = str(body["strategy"]).strip().lower()
        if s not in ("priority", "cheapest"):
            raise ValueError("策略只能是 priority / cheapest")
        patch["strategy"] = s
    if "cooldown_seconds" in body:
        try:
            patch["cooldown_seconds"] = max(1, min(int(body["cooldown_seconds"]), 3600))
        except Exception:
            raise ValueError("冷却必须是整数秒") from None
    if "gateway_token" in body:
        patch["gateway_token"] = str(body["gateway_token"] or "").strip()
    if "proxy" in body:
        patch["proxy"] = str(body["proxy"] or "").strip()
    for key in ("opencode_bin", "opencode_workspace"):
        if key in body:
            patch[key] = str(body[key] or "").strip()
    if "opencode_port" in body:
        try:
            patch["opencode_port"] = max(1, min(int(body["opencode_port"]), 65535))
        except Exception:
            raise ValueError("opencode 端口必须是整数") from None
    if "opencode_autostart" in body:
        patch["opencode_autostart"] = bool(body["opencode_autostart"])
    if "sanitize" in body:
        patch["sanitize"] = bool(body["sanitize"])
    if "sanitize_scope" in body:
        sc = str(body["sanitize_scope"] or "all").lower()
        if sc not in ("all", "user", "assistant", "system"):
            raise ValueError("作用域只能是 all / user / assistant / system")
        patch["sanitize_scope"] = sc
    if "sanitize_words" in body:
        patch["sanitize_words"] = as_list(body["sanitize_words"])
    if "sanitize_replacement" in body:
        patch["sanitize_replacement"] = str(body["sanitize_replacement"] or "该服务")
    for key, low, high, dflt in (
            ("timeout_short", 5, 3600, 120),
            ("timeout_first_byte", 5, 3600, 120),
            ("timeout_stream_idle", 5, 3600, 300),
            ("keep_rounds", 0, 500, 0)):
        if key in body:
            patch[key] = _num(body[key], dflt, low, high)
    if "user_agent" in body:
        patch["user_agent"] = str(body["user_agent"] or "").strip()
    for key in ("model_blacklist", "model_whitelist", "model_mapping"):
        if key in body:
            patch[key] = str(body[key] or "")
    if "model_caps" in body and isinstance(body["model_caps"], dict):
        patch["model_caps"] = body["model_caps"]
    if "system_mode" in body:
        sm = str(body["system_mode"] or "passthrough").lower()
        if sm not in ("passthrough", "default", "append", "prepend", "drop"):
            raise ValueError("提示词模式只能是 passthrough / default / "
                             "append / prepend / drop")
        patch["system_mode"] = sm
    if "system_text" in body:
        patch["system_text"] = str(body["system_text"] or "")
    if "system_file" in body:
        patch["system_file"] = str(body["system_file"] or "").strip()
    if "effort_enabled" in body:
        patch["effort_enabled"] = bool(body["effort_enabled"])
    if "effort_default" in body:
        et = str(body["effort_default"] or "max").lower()
        if et not in ("off", "low", "medium", "high", "max"):
            raise ValueError("思考档位只能是 off / low / medium / high / max")
        patch["effort_default"] = et
    if "effort_mode" in body:
        em = str(body["effort_mode"] or "client").lower()
        if em not in ("client", "force", "force+"):
            raise ValueError("思考档位策略只能是 client / force / force+")
        patch["effort_mode"] = em
    if "log_source" in body:
        patch["log_source"] = bool(body["log_source"])

    groups: dict = {}
    names: list = []
    for raw in body.get("providers") or []:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "").strip()
        if not name:
            raise ValueError("有上游缺少 name（名称不能为空）")
        item = {k: v for k, v in raw.items() if not str(k).startswith("_")}
        item["name"] = name
        item.setdefault("base_url", "")
        item.setdefault("model", "")
        names.append(name)
        groups.setdefault(str(raw.get("_source") or "main"), []).append(item)

    base_dir = os.path.dirname(os.path.abspath(CONFIG_PATH))
    for src, items in groups.items():
        path = CONFIG_PATH if src == "main" else os.path.join(base_dir, "providers.d", src)
        existing = read_json(path, {})
        if not isinstance(existing, dict):
            existing = {}
        existing["providers"] = items
        atomic_write_json(path, existing)

    try:
        ov = load_overrides()
        hit = [n for n in names if n in ov]
        if hit:
            for n in hit:
                ov.pop(n, None)
            save_overrides(ov)
    except Exception:
        pass

    _ock = str(body.get("opencode_api_key") or "").strip()
    if _ock:
        save_secret("OPENCODE_API_KEY", _ock)
        os.environ["OPENCODE_API_KEY"] = _ock
    for env, val in (body.get("secrets") or {}).items():
        save_secret(str(env), str(val or ""))

    if patch:
        save_settings(patch)
    load_config()

    snap = config_snapshot()
    disk = load_settings()
    snap["on_disk"] = {
        "proxy": str(disk.get("proxy") or ""),
        "strategy": str(disk.get("strategy") or ""),
        "cooldown_seconds": disk.get("cooldown_seconds"),
        "opencode_port": disk.get("opencode_port"),
    }
    snap["ok"] = True
    return snap


def save_one_provider(raw: dict) -> dict:
    name = str(raw.get("name") or "").strip()
    if not name:
        raise ValueError("名称不能为空")
    item = {k: v for k, v in raw.items() if not str(k).startswith("_")}
    item["name"] = name
    item.setdefault("base_url", "")
    item.setdefault("model", "")

    src = str(raw.get("_source") or "main")
    base_dir = os.path.dirname(os.path.abspath(CONFIG_PATH))
    path = CONFIG_PATH if src == "main" else os.path.join(base_dir, "providers.d", src)
    existing = read_json(path, {})
    if not isinstance(existing, dict):
        existing = {}
    provs = list(existing.get("providers") or [])
    for i, p in enumerate(provs):
        if str(p.get("name")) == name:
            provs[i] = item
            break
    else:
        provs.append(item)
    existing["providers"] = provs
    atomic_write_json(path, existing)

    try:
        ov = load_overrides()
        if name in ov:
            ov.pop(name)
            save_overrides(ov)
    except Exception:
        pass
    load_config()
    return config_snapshot()


def set_provider_enabled(name: str, enabled: bool) -> bool:
    base_dir = os.path.dirname(os.path.abspath(CONFIG_PATH))
    paths = [CONFIG_PATH]
    d = os.path.join(base_dir, "providers.d")
    if os.path.isdir(d):
        paths += [os.path.join(d, f) for f in sorted(os.listdir(d)) if f.endswith(".json")]
    for path in paths:
        data = read_json(path, {})
        if not isinstance(data, dict):
            continue
        hit = False
        for p in (data.get("providers") or []):
            if str(p.get("name")) == name:
                p["enabled"] = bool(enabled)
                hit = True
                break
        if hit:
            atomic_write_json(path, data)
            try:
                ov = load_overrides()
                if name in ov:
                    ov.pop(name)
                    save_overrides(ov)
            except Exception:
                pass
            load_config()
            return True
    return False


# ─────────────────────────── 其它服务 ───────────────────────────
def tail_file(path: str, lines: int = 200) -> str:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 256 * 1024))
            data = fh.read().decode("utf-8", "ignore")
        return chr(10).join(data.splitlines()[-max(1, lines):])
    except FileNotFoundError:
        return "（还没有日志）"
    except Exception as exc:                           # noqa: BLE001
        return f"（读取失败：{exc}）"


def quick_port_check(url: str, timeout: float = 0.6) -> bool:
    try:
        u = urllib.parse.urlparse(url if "://" in url else "http://" + url)
        host = u.hostname or "127.0.0.1"
        port = u.port or (443 if u.scheme == "https" else 80)
        sk = socket.socket()
        sk.settimeout(timeout)
        try:
            return sk.connect_ex((host, port)) == 0
        finally:
            sk.close()
    except Exception:
        return False


def test_proxy(proxy_url: str, target: str = "https://api.deepseek.com/") -> dict:
    proxy_url = (proxy_url or "").strip()
    if not proxy_url:
        return {"ok": False, "error": "没有填代理地址", "ms": 0}
    t0 = now()
    try:
        opener = build_opener(proxy_url, 12)
        req = urllib.request.Request(target, method="GET",
                                     headers={"User-Agent": "fnos-ai-gateway"})
        resp = opener.open(req, timeout=12) if opener else \
            urllib.request.urlopen(req, timeout=12)
        try:
            code = resp.status
        finally:
            resp.close()
        return {"ok": True, "status": code, "ms": int((now() - t0) * 1000),
                "target": target, "note": "通道正常"}
    except urllib.error.HTTPError as exc:
        ms = int((now() - t0) * 1000)
        if exc.code in (401, 403):
            note = "通道正常（目标需要鉴权，401/403 属预期）"
        elif 400 <= exc.code < 500:
            note = "通道正常（目标返回 4xx，说明已经连上）"
        else:
            note = f"通道能连上，但目标返回 {exc.code}"
        return {"ok": True, "status": exc.code, "ms": ms, "target": target, "note": note}
    except Exception as exc:                           # noqa: BLE001
        return {"ok": False, "error": err_text(exc), "ms": int((now() - t0) * 1000),
                "target": target}


def opencode_cli_models() -> list:
    try:
        bin_path, ws, env = SUPERVISOR.cli_context()
    except Exception:
        return []
    if not bin_path:
        return []
    try:
        proc = subprocess.run([bin_path, "models"], capture_output=True,
                              timeout=90, env=env, cwd=ws)
    except Exception:
        return []
    out = proc.stdout.decode("utf-8", "ignore") or proc.stderr.decode("utf-8", "ignore")
    ids = []
    for ln in out.splitlines():
        ln = ln.strip()
        if not ln or " " in ln or ln.startswith(">") or "/" not in ln:
            continue
        ids.append(ln)
    return ids


def discover_models(p: dict) -> dict:
    try:
        if p["type"] == "opencode":
            ids = []
            try:
                client = OpenCodeClient(base_url=p["base_url"], model_id="",
                                        provider_id=p["opencode_provider"], timeout=10)
                ids = client.available_models()
            except Exception:
                ids = []
            if not ids:
                ids = opencode_cli_models()
            want = (p.get("opencode_provider") or "opencode") + "/"
            ids = [i for i in ids if i.startswith(want)]
            return {"ok": True, "count": len(ids),
                    "models": [{"id": i, "free": "free" in i.lower()} for i in ids]}

        url = p["base_url"] + "/models"
        head = headers_for(p, False)
        head.pop("Accept", None)
        req = urllib.request.Request(url, headers=head, method="GET")
        to = float(RUNTIME["timeout_short"])
        opener = build_opener(p.get("proxy") or RUNTIME.get("proxy") or "", to)
        if opener is not None:
            resp = opener.open(req, timeout=to)
        else:
            resp = urllib.request.urlopen(req, timeout=to)
        try:
            raw = resp.read()
        finally:
            resp.close()
        data = json.loads(raw.decode("utf-8", "ignore"))
        items = data.get("data") if isinstance(data, dict) else data
        out = []
        for it in items or []:
            if isinstance(it, str):
                mid, free = it, "free" in it.lower()
            elif isinstance(it, dict):
                mid = str(it.get("id") or it.get("name") or "")
                pr = it.get("pricing") or {}
                zero = (str(pr.get("prompt", "")).replace(".", "") == "0"
                        and str(pr.get("completion", "")).replace(".", "") == "0")
                free = ("free" in mid.lower()) or (bool(pr) and zero)
            else:
                continue
            if mid:
                out.append({"id": mid, "free": bool(free)})
        return {"ok": True, "count": len(out), "models": out}
    except Exception as exc:                           # noqa: BLE001
        return {"ok": False, "error": err_text(exc)}


def caps_payload() -> dict:
    """列出所有模型及其上下文 / 图片 / 思考档能力。"""
    seen, rows = set(), []
    override = RUNTIME["model_caps"] or {}
    for p in PROVIDERS:
        for mid in [p["model"]] + list(p.get("models") or []):
            mid = str(mid or "").strip()
            if not mid or mid in seen:
                continue
            seen.add(mid)
            caps = policy.caps_for(mid, override.get(mid))
            rows.append({
                "model": mid,
                "provider": p["name"],
                "context": caps.get("context"),
                "vision": bool(caps.get("vision")),
                "reasoning": list(caps.get("reasoning") or []),
                "known": bool(caps.get("known")),
                "override": override.get(mid) or {},
            })
    rows.sort(key=lambda r: (r["provider"], r["model"]))
    tiers = [r for r in rows if len(r["reasoning"]) > 1]
    return {"ok": True, "models": rows, "count": len(rows),
            "with_reasoning": len(tiers),
            "vision_count": sum(1 for r in rows if r["vision"]),
            "default_effort": RUNTIME["effort_default"],
            "unsupported_default": [
                {"model": r["model"], "falls_to": policy.resolve_effort(
                    r["model"], "", RUNTIME["effort_default"],
                    "force", policy.caps_for(r["model"], override.get(r["model"])))[0]}
                for r in rows
                if RUNTIME["effort_default"] not in (r["reasoning"] or [])
            ],
            }


def stats_payload(days: int = 7) -> dict:
    """按天汇总 + 按上游汇总。"""
    days = max(1, min(int(days or 7), 90))
    since = now() - days * 86400
    daily, by_prov = [], []
    totals = {"requests": 0, "ok": 0, "tokens_in": 0, "tokens_out": 0, "cost": 0.0}
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        cur = conn.execute(
            "SELECT strftime('%Y-%m-%d', ts, 'unixepoch', 'localtime') AS d,"
            " COUNT(*), SUM(ok), SUM(tokens_in), SUM(tokens_out), SUM(cost)"
            " FROM usage WHERE ts >= ? GROUP BY d ORDER BY d", (since,))
        for d, reqs, oks, tin, tout, cost in cur.fetchall():
            row = {"day": d or "", "requests": int(reqs or 0), "ok": int(oks or 0),
                   "tokens_in": int(tin or 0), "tokens_out": int(tout or 0),
                   "cost": float(cost or 0.0)}
            daily.append(row)
            totals["requests"] += row["requests"]
            totals["ok"] += row["ok"]
            totals["tokens_in"] += row["tokens_in"]
            totals["tokens_out"] += row["tokens_out"]
            totals["cost"] += row["cost"]

        cur = conn.execute(
            "SELECT provider, COUNT(*), SUM(ok), SUM(tokens_in), SUM(tokens_out),"
            " SUM(cost), SUM(ms) FROM usage WHERE ts >= ? GROUP BY provider", (since,))
        for name, reqs, oks, tin, tout, cost, ms in cur.fetchall():
            reqs = int(reqs or 0)
            by_prov.append({
                "provider": name or "", "requests": reqs, "ok": int(oks or 0),
                "tokens_in": int(tin or 0), "tokens_out": int(tout or 0),
                "cost": float(cost or 0.0),
                "avg_ms": int((int(ms or 0)) / reqs) if reqs else 0,
                "success_rate": round(int(oks or 0) / reqs * 100, 1) if reqs else None,
            })
        conn.close()
    except Exception as exc:                           # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    by_prov.sort(key=lambda x: x["tokens_in"] + x["tokens_out"], reverse=True)
    return {"ok": True, "days": days, "daily": daily, "providers": by_prov,
            "totals": totals}


def errors_payload(limit: int = 3000) -> dict:
    """按 上游 × 错误类型 聚合失败，方便一眼看出谁在拖后腿。"""
    out = {}
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        cur = conn.execute(
            "SELECT provider, model, status, err, ts, request_id FROM usage"
            " WHERE ok = 0 ORDER BY ts DESC LIMIT ?", (max(1, limit),))
        for prov, model, status, err, ts, rid in cur.fetchall():
            kind = "其它"
            low = str(err or "").lower()
            if status == 429 or "429" in low or "rate" in low or "限流" in low:
                kind = "模型限流"
            elif "timeout" in low or "超时" in low:
                kind = "超时"
            elif "401" in low or "403" in low or "unauthorized" in low:
                kind = "鉴权失败"
            elif "econnrefused" in low or "connect" in low:
                kind = "连不上"
            elif "额度" in low or "quota" in low or "usage limit" in low:
                kind = "额度用尽"
            elif status == 502:
                kind = "上游 502"
            key = (prov or "", kind)
            item = out.setdefault(key, {
                "provider": prov or "", "kind": kind, "count": 0,
                "status": status, "last": float(ts or 0),
                "last_time": time.strftime("%m-%d %H:%M:%S",
                                           time.localtime(ts or 0)),
                "sample": str(err or "")[:200], "request_id": rid or "",
            })
            item["count"] += 1
            if float(ts or 0) >= item["last"]:
                item["last"] = float(ts or 0)
                item["last_time"] = time.strftime("%m-%d %H:%M:%S",
                                                  time.localtime(ts or 0))
                item["sample"] = str(err or "")[:200]
        conn.close()
    except Exception as exc:                           # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    rows = sorted(out.values(), key=lambda x: (-x["count"], -x["last"]))
    return {"ok": True, "rows": rows, "kinds": len({r["kind"] for r in rows})}


def status_payload() -> dict:
    usage = usage_summary()
    used = used_tokens()
    sec = load_secrets()
    rows = []
    for p in PROVIDERS:
        st = dict(STATE.get(p["name"], {}))
        u = usage.get(p["name"], {})
        rows.append({
            "name": p["name"],
            "enabled": p["enabled"],
            "has_key": bool(p["api_key"]) or p["type"] == "opencode" or p["no_auth"],
            "key_source": provider_key_source(p, sec),
            "type": p["type"],
            "protocol": p.get("protocol") or "",
            "opencode_mode": p.get("opencode_mode") or "",
            "model": p["model"],
            "models": list(p.get("models") or []),
            "quant": p["quant"],
            "free_only": p["free_only"],
            "free_quota_tokens": p["free_quota_tokens"],
            "used_tokens": used.get(p["name"], 0),
            "exhausted": is_exhausted(p, used),
            "cooldown": int(max(0, st.get("cooldown_until", 0) - now())),
            "fails": st.get("fails", 0),
            "requests_24h": u.get("requests_24h", 0),
            "tokens_in_24h": u.get("tokens_in_24h", 0),
            "tokens_out_24h": u.get("tokens_out_24h", 0),
            "cost_24h": u.get("cost_24h", 0.0),
            "price_in": p["price_in"],
            "price_out": p["price_out"],
            "note": p["note"],
            "last_error": "",
        })
    return {
        "port": PORT,
        "strategy": RUNTIME["strategy"],
        "providers": rows,
        "total_requests_24h": sum(r["requests_24h"] for r in rows),
        "total_cost_24h": sum(r["cost_24h"] for r in rows),
    }


def tokens_payload() -> dict:
    used = token_usage()
    out = []
    for t in load_tokens():
        item = dict(t)
        item["key_masked"] = mask_key(str(t.get("key", "")))
        item.pop("key", None)
        item["used_tokens"] = used.get(str(t.get("id", "")), 0)
        out.append(item)
    return {
        "tokens": out,
        "master": bool(RUNTIME["gateway_token"]),
        "open_mode": (not RUNTIME["gateway_token"]
                      and not [t for t in load_tokens() if t.get("enabled", True)]),
    }


# ─────────────────────────── 启动 ───────────────────────────
_UnixBase = getattr(socketserver, "ThreadingUnixStreamServer", None)


if _UnixBase is not None:
    class UnixHTTPServer(_UnixBase):          # noqa: F811
        daemon_threads = True

    class UnixHandler(Handler):
        pass
else:
    UnixHTTPServer = None
    UnixHandler = None


def main() -> int:
    load_config()
    init_db()

    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    srv.daemon_threads = True
    sock_srv = None

    if GATEWAY_SOCKET and UnixHTTPServer is not None:
        try:
            if os.path.exists(GATEWAY_SOCKET):
                os.unlink(GATEWAY_SOCKET)
            sock_srv = UnixHTTPServer(GATEWAY_SOCKET, UnixHandler)
            try:
                os.chmod(GATEWAY_SOCKET, 0o660)
            except Exception:
                pass
            threading.Thread(target=sock_srv.serve_forever, daemon=True).start()
            print(f"[gateway] 统一网关 socket：{GATEWAY_SOCKET}", flush=True)
        except Exception as exc:                       # noqa: BLE001
            print(f"[gateway] socket 建立失败（不影响端口）：{exc}", flush=True)
            sock_srv = None

    def _shutdown(signum, frame):                      # noqa: ARG001
        threading.Thread(target=srv.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    threading.Thread(target=SUPERVISOR.watch, daemon=True).start()

    live = sum(1 for p in PROVIDERS
               if p["enabled"] and (p["api_key"] or p["type"] == "opencode"))
    print(f"[gateway] 监听 http://{HOST}:{PORT}  上游 {len(PROVIDERS)} 个 / 可用 {live} 个",
          flush=True)
    try:
        srv.serve_forever()
    finally:
        srv.server_close()
        if sock_srv is not None:
            sock_srv.server_close()
        try:
            SUPERVISOR.shutdown()
        except Exception:
            pass
        print("[gateway] 已退出", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
