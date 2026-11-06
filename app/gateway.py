#!/usr/bin/env python3
"""DeepSeek-V4.1-Flash 聚合网关 · 纯标准库实现

不依赖任何第三方包，也不需要 Docker：
  只用 Python 3.8+ 自带的 http.server / urllib / sqlite3 / json

一个 OpenAI 兼容端点，背后挂多家上游：
  - 自动故障切换：429 / 5xx / 超时 → 冷却 → 换下一家
  - 排序策略：priority（默认）或 cheapest（按价格）
  - 响应 model 字段校验，防上游静默换模型
  - token 用量与成本记账（SQLite）
  - /admin 面板：上游健康、报价、24h 花费、一键启停

环境变量：
  CONFIG_PATH  上游配置 JSON 路径（默认 ./providers.json）
  DATA_DIR     数据目录（默认 ./data）
  PORT         监听端口（默认 18787）
  HOST         监听地址（默认 0.0.0.0）
  GATEWAY_TOKEN 访问口令，留空则不校验
  COOLDOWN_SECONDS 失败冷却基数，默认 60
  OPENROUTER_API_KEY / VOLC_ARK_API_KEY / ... 各上游密钥
"""
from __future__ import annotations

import json
import os
import signal
import socket
import socketserver
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from opencode_upstream import OpenCodeClient, OpenCodeError
from opencode_supervisor import OpenCodeSupervisor

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
APPNAME = "deepseek-gateway"
# 统一网关路径前缀（飞牛会把 /app/deepseek-gateway/* 转发到 Unix Socket）
GATEWAY_PREFIX = os.getenv("GATEWAY_PREFIX", "/app/" + APPNAME).rstrip("/")
SOCKET_PATH = os.getenv("GATEWAY_SOCKET", "").strip()
CONFIG_PATH = os.getenv("CONFIG_PATH") or os.path.join(BASE_DIR, "providers.json")
DATA_DIR = os.getenv("DATA_DIR") or os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "usage.db")
OVERRIDES_PATH = os.path.join(DATA_DIR, "overrides.json")
GATEWAY_TOKEN = os.getenv("GATEWAY_TOKEN", "").strip()
DEFAULT_COOLDOWN = int(os.getenv("COOLDOWN_SECONDS", "60"))
PORT = int(os.getenv("PORT", "18787"))
HOST = os.getenv("HOST", "0.0.0.0")

# 运行时设置（面板里改的，落盘到 DATA_DIR/settings.json）
SETTINGS_PATH = os.path.join(DATA_DIR, "settings.json")
RUNTIME: dict = {"strategy": "priority", "cooldown": DEFAULT_COOLDOWN,
                 "gateway_token": GATEWAY_TOKEN, "proxy": ""}


def opencode_opts() -> dict:
    """托管器读的运行时选项：来自 settings.json + 当前是否有启用的 opencode 上游。"""
    data: dict = {}
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as fh:
            data = json.load(fh) or {}
    except Exception:
        data = {}
    try:
        port = int(data.get("opencode_port") or 4096)
    except Exception:
        port = 4096
    enabled = any(p["type"] == "opencode" and p["enabled"] for p in PROVIDERS)
    return {
        "bin": str(data.get("opencode_bin") or ""),
        "port": port,
        "workspace": str(data.get("opencode_workspace")
                         or os.path.join(DATA_DIR, "opencode-workspace")),
        "autostart": bool(data.get("opencode_autostart", True)),
        "enabled": enabled,
    }


SUPERVISOR = OpenCodeSupervisor(DATA_DIR, opencode_opts)

PROVIDERS: list[dict] = []
SETTINGS: dict = {}
STATE: dict[str, dict] = {}
STATE_LOCK = threading.Lock()
CONFIG_LOCK = threading.Lock()


class ProviderError(Exception):
    """上游失败（可切换到下一家）。"""


# ─────────────────────────── 配置 ───────────────────────────
def read_overrides() -> dict:
    try:
        with open(OVERRIDES_PATH, encoding="utf-8") as fh:
            return {str(k): bool(v) for k, v in json.load(fh).items()}
    except Exception:
        return {}


def write_overrides(data: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = OVERRIDES_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, OVERRIDES_PATH)


def load_secrets() -> dict:
    """额外的密钥文件（DATA_DIR/secrets.json），用于自己新增上游时不必改 run.sh。"""
    path = os.path.join(DATA_DIR, "secrets.json")
    try:
        with open(path, encoding="utf-8") as fh:
            return {str(k): str(v) for k, v in json.load(fh).items()}
    except Exception:
        return {}


def save_secret(name: str, value: str) -> None:
    """写/删一个密钥到 secrets.json（面板「填 Key」用）。"""
    path = os.path.join(DATA_DIR, "secrets.json")
    data = load_secrets()
    if value:
        data[name] = value
    else:
        data.pop(name, None)
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    try:
        os.chmod(tmp, 0o600)
    except Exception:
        pass
    os.replace(tmp, path)


def key_source(key_env: str) -> str:
    """这个上游的 key 是从哪来的：面板 / 环境变量 / 配置内联 / 没有。"""
    if not key_env:
        return "config"
    if load_secrets().get(key_env):
        return "面板"
    if os.getenv(key_env):
        return "向导"
    return "无"


# ── 运行时设置：面板里改策略/冷却/口令，落盘 settings.json ──
def load_runtime() -> None:
    data: dict = {}
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as fh:
            data = json.load(fh) or {}
    except Exception:
        data = {}

    strategy = str(data.get("strategy") or os.getenv("STRATEGY") or "").strip().lower()
    if strategy not in ("priority", "cheapest"):
        strategy = "priority"

    try:
        cooldown = int(data.get("cooldown_seconds") or os.getenv("COOLDOWN_SECONDS") or 60)
    except Exception:
        cooldown = 60

    # 文件里显式给了 gateway_token（哪怕是空串）就以文件为准
    if "gateway_token" in data:
        token = str(data.get("gateway_token") or "").strip()
    else:
        token = GATEWAY_TOKEN

    RUNTIME.update(strategy=strategy, cooldown=max(1, cooldown), gateway_token=token,
                   proxy=str(data.get("proxy") or os.getenv("HTTP_PROXY") or "").strip())


def save_runtime(patch: dict) -> None:
    data: dict = {}
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as fh:
            data = json.load(fh) or {}
    except Exception:
        data = {}
    data.update(patch)
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = SETTINGS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    try:
        os.chmod(tmp, 0o600)
    except Exception:
        pass
    os.replace(tmp, SETTINGS_PATH)


def atomic_write_json(path: str, data) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# 设置页里可编辑的字段（其余字段（extra 等）留在 JSON 里手工改）
EDITABLE_FIELDS = ("name", "type", "enabled", "priority", "base_url", "model",
                   "model_contains", "strict_model", "free_only", "free_quota_tokens",
                   "price_in", "price_out", "quant", "proxy", "console_url",
                   "note", "api_key_env", "opencode_provider", "opencode_agent")


def config_snapshot() -> dict:
    """给设置页的完整快照：全局设置 + 所有上游（按来源文件标记）。"""
    resolved = {p["name"]: p for p in PROVIDERS}
    sec = load_secrets()
    provs: list[dict] = []
    for path in provider_files():
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh) or {}
        except Exception:
            continue
        src = ("main" if os.path.abspath(path) == os.path.abspath(CONFIG_PATH)
               else os.path.basename(path))
        for it in data.get("providers") or []:
            item = dict(it)                 # 保留全部原始字段（extra / opencode_* 等）
            item["_source"] = src
            r = resolved.get(str(it.get("name", "")))
            item["_has_key"] = bool(r and r["api_key"])
            item["_key_source"] = provider_key_source(r, sec) if r else "无"
            provs.append(item)
    return {
        "strategy": RUNTIME["strategy"],
        "cooldown_seconds": RUNTIME["cooldown"],
        "proxy": RUNTIME.get("proxy", ""),
        "auth_enabled": bool(RUNTIME["gateway_token"]),
        "prefix": GATEWAY_PREFIX,
        "socket": SOCKET_PATH,
        "opencode": opencode_opts(),
        "providers": provs,
    }


def config_save(body: dict) -> dict:
    """整页保存：全局设置 + 上游（按 _source 写回各自文件）+ 密钥。"""
    patch: dict = {}
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
    if "opencode_bin" in body:
        patch["opencode_bin"] = str(body["opencode_bin"] or "").strip()
    if "opencode_workspace" in body:
        patch["opencode_workspace"] = str(body["opencode_workspace"] or "").strip()
    if "opencode_port" in body:
        try:
            patch["opencode_port"] = max(1, min(int(body["opencode_port"]), 65535))
        except Exception:
            raise ValueError("opencode 端口必须是整数") from None
    if "opencode_autostart" in body:
        patch["opencode_autostart"] = bool(body["opencode_autostart"])

    groups: dict[str, list] = {}
    for raw in body.get("providers") or []:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "").strip()
        if not name:
            raise ValueError("每个上游都必须有 name")
        item = {k: v for k, v in raw.items() if not str(k).startswith("_")}
        item["name"] = name
        item.setdefault("base_url", "")
        item.setdefault("model", "")
        groups.setdefault(str(raw.get("_source") or "main"), []).append(item)

    base_dir = os.path.dirname(os.path.abspath(CONFIG_PATH))
    for src, items in groups.items():
        path = CONFIG_PATH if src == "main" else os.path.join(base_dir, "providers.d", src)
        try:
            with open(path, encoding="utf-8") as fh:
                existing = json.load(fh) or {}
        except Exception:
            existing = {}
        existing["providers"] = items
        atomic_write_json(path, existing)

    for env, val in (body.get("secrets") or {}).items():
        save_secret(str(env), str(val or ""))

    if patch:
        save_runtime(patch)
    load_config()
    return config_snapshot()


def provider_key_source(p: dict, sec: dict | None = None) -> str:
    """密钥来源：面板 / 向导 / 配置 / 无 / 本地(opencode)。"""
    if p["type"] == "opencode":
        return "本地"
    if sec is None:
        sec = load_secrets()
    if p["key_env"] and sec.get(p["key_env"]):
        return "面板"
    if p["key_env"] and os.getenv(p["key_env"]):
        return "向导"
    if p["api_key"]:
        return "配置"
    return "无"


def provider_files() -> list[str]:
    """主配置 + providers.d/ 下所有 *.json（便于单独新增上游，不必改主文件）。"""
    files = [CONFIG_PATH]
    extra = os.path.join(os.path.dirname(os.path.abspath(CONFIG_PATH)), "providers.d")
    if os.path.isdir(extra):
        files += [os.path.join(extra, n) for n in sorted(os.listdir(extra))
                  if n.endswith(".json")]
    return files


def load_config() -> None:
    """读取 providers.json + providers.d/*.json + overrides.json（支持热重载）。"""
    global PROVIDERS, SETTINGS
    with CONFIG_LOCK:
        settings: dict = {}
        raw_providers: list[dict] = []
        index: dict[str, int] = {}

        for path in provider_files():
            try:
                with open(path, encoding="utf-8") as fh:
                    data = json.load(fh)
            except Exception as exc:                    # noqa: BLE001
                print(f"[gateway] 跳过无法解析的配置 {path}: {exc}", flush=True)
                continue
            if not settings and data.get("settings"):
                settings = data["settings"]
            for it in data.get("providers") or []:
                name = it.get("name")
                if not name:
                    continue
                if name in index:                       # 同名则后加载的覆盖
                    raw_providers[index[name]] = it
                else:
                    index[name] = len(raw_providers)
                    raw_providers.append(it)

        SETTINGS = settings
        load_runtime()
        overrides = read_overrides()
        secrets = load_secrets()
        default_timeout = int(SETTINGS.get("request_timeout", 180))

        items: list[dict] = []
        for it in raw_providers:
            key_env = it.get("api_key_env")
            # 优先级：secrets.json（面板里填的） > 环境变量（向导/run.sh） > 配置内联
            if key_env:
                key = secrets.get(key_env, "") or os.getenv(key_env, "")
            else:
                key = it.get("api_key", "")
            name = it["name"]
            enabled = bool(it.get("enabled", True))
            if name in overrides:
                enabled = overrides[name]
            items.append({
                "name": name,
                "base_url": str(it["base_url"]).rstrip("/"),
                "model": it["model"],
                "api_key": (key or "").strip(),
                "key_env": key_env or "",
                "priority": int(it.get("priority", 100)),
                "price_in": float(it.get("price_in", 0) or 0),
                "price_out": float(it.get("price_out", 0) or 0),
                "quant": it.get("quant", "-"),
                "enabled": enabled,
                "timeout": int(it.get("timeout", default_timeout)),
                "model_contains": str(it.get("model_contains", "") or ""),
                "strict_model": bool(it.get("strict_model", False)),
                "free_only": bool(it.get("free_only", False)),
                "free_quota_tokens": int(it.get("free_quota_tokens", 0) or 0),
                "extra": it.get("extra") or {},
                # type=http：OpenAI 兼容 HTTP 上游（默认）
                # type=opencode：驱动本地 opencode serve（见 opencode_upstream.py）
                "type": str(it.get("type", "http") or "http").lower(),
                "opencode_provider": str(it.get("opencode_provider", "opencode") or "opencode"),
                "opencode_agent": str(it.get("opencode_agent", "") or ""),
                "opencode_tools": it.get("opencode_tools"),
                "opencode_extra": it.get("opencode_extra") or {},
                "proxy": str(it.get("proxy", "") or ""),
                "console_url": str(it.get("console_url", "") or ""),
                "note": str(it.get("note", "")),
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
               cost REAL, ok INTEGER, ms INTEGER, err TEXT)"""
    )
    conn.commit()
    conn.close()


def record(provider: str, model: str, tin: int, tout: int,
           cost: float, ok: bool, ms: int, err: str = "") -> None:
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        conn.execute(
            "INSERT INTO usage VALUES (?,?,?,?,?,?,?,?,?)",
            (time.time(), provider, model, int(tin), int(tout),
             float(cost), 1 if ok else 0, int(ms), (err or "")[:300]),
        )
        conn.commit()
        conn.close()
    except Exception:                 # 记账失败不影响主链路
        pass
    _QUOTA_CACHE["ts"] = 0.0          # 让额度统计立即刷新


# ── 免费额度硬卡：累计 token 超配额就自动停用，绝不静默转付费 ──
_QUOTA_CACHE: dict = {"ts": 0.0, "data": {}}
QUOTA_TTL = 10.0


def used_tokens() -> dict:
    """各上游累计成功消耗的 token（全时段，用于比对免费额度）。"""
    now = time.time()
    if now - _QUOTA_CACHE["ts"] < QUOTA_TTL:
        return _QUOTA_CACHE["data"]
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        rows = conn.execute(
            "SELECT provider, SUM(tokens_in + tokens_out) FROM usage "
            "WHERE ok=1 GROUP BY provider"
        ).fetchall()
        conn.close()
        data = {name: int(total or 0) for name, total in rows}
    except Exception:
        data = {}
    _QUOTA_CACHE["ts"] = now
    _QUOTA_CACHE["data"] = data
    return data


def is_exhausted(p: dict, used: dict) -> bool:
    """free_only 且累计用量已达配额 → 视为额度用尽。"""
    if not p["free_only"]:
        return False
    quota = p["free_quota_tokens"]
    if quota <= 0:
        return False
    return used.get(p["name"], 0) >= quota


def usage_summary() -> dict:
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        rows = conn.execute(
            """SELECT provider, COUNT(*), SUM(tokens_in), SUM(tokens_out),
                      SUM(cost), SUM(CASE WHEN ok=0 THEN 1 ELSE 0 END)
               FROM usage WHERE ts > ? GROUP BY provider""",
            (time.time() - 86400,),
        ).fetchall()
        conn.close()
    except Exception:
        rows = []
    out = {}
    for name, reqs, tin, tout, cost, errs in rows:
        out[name] = {
            "requests_24h": reqs or 0,
            "tokens_in_24h": tin or 0,
            "tokens_out_24h": tout or 0,
            "cost_24h": round(cost or 0.0, 6),
            "errors_24h": errs or 0,
        }
    return out


# ─────────────────────────── 路由 ───────────────────────────
def candidates(strategy: str) -> list[dict]:
    now = time.time()
    used = used_tokens()
    with STATE_LOCK:
        live = [
            p for p in PROVIDERS
            if p["enabled"]
            and (p["api_key"] or p["type"] == "opencode")   # opencode 走本地，不需要 key
            and STATE.get(p["name"], {}).get("cooldown_until", 0) <= now
            and not is_exhausted(p, used)
        ]
    if strategy == "cheapest":
        live.sort(key=lambda p: (p["price_in"] + p["price_out"], p["priority"]))
    else:
        live.sort(key=lambda p: p["priority"])
    return live


def mark_fail(p: dict, err: Exception) -> None:
    with STATE_LOCK:
        st = STATE.setdefault(p["name"], {})
        st["fails"] = st.get("fails", 0) + 1
        st["cooldown_until"] = time.time() + RUNTIME["cooldown"] * min(st["fails"], 6)
        st["last_error"] = str(err)[:300]
        st["last_error_at"] = time.time()


def mark_ok(p: dict) -> None:
    with STATE_LOCK:
        st = STATE.setdefault(p["name"], {})
        st["fails"] = 0
        st["cooldown_until"] = 0
        st["ok"] = st.get("ok", 0) + 1
        st["last_ok_at"] = time.time()


def build_body(p: dict, payload: dict, stream: bool) -> bytes:
    body = dict(payload)
    body["model"] = p["model"]
    if p["extra"]:
        body.update(p["extra"])
    if stream:
        body.setdefault("stream_options", {"include_usage": True})
    return json.dumps(body, ensure_ascii=False).encode("utf-8")


def headers_for(p: dict, stream: bool) -> dict:
    h = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream" if stream else "application/json",
    }
    if p["api_key"]:
        h["Authorization"] = "Bearer " + p["api_key"]
    if "openrouter.ai" in p["base_url"]:
        h["HTTP-Referer"] = SETTINGS.get("site_url", "http://fnos.local")
        h["X-Title"] = SETTINGS.get("site_name", "fnos-deepseek-gateway")
    return h


def open_upstream(p: dict, payload: dict, stream: bool):
    req = urllib.request.Request(
        p["base_url"] + "/chat/completions",
        data=build_body(p, payload, stream),
        headers=headers_for(p, stream),
        method="POST",
    )
    return urllib.request.urlopen(req, timeout=p["timeout"])


def err_text(exc: BaseException) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        try:
            body = exc.read().decode("utf-8", "ignore")
        except Exception:
            body = ""
        return f"HTTP {exc.code}: {body[:300]}"
    return f"{type(exc).__name__}: {exc}"


def check_model(p: dict, data: dict) -> str:
    got = str(data.get("model") or "")
    want = p["model_contains"]
    if want and want.lower() not in got.lower():
        msg = f"model 校验失败：期望含 '{want}'，实际 '{got}'"
        if p["strict_model"]:
            raise ProviderError(msg)
        return msg
    return ""


def cost_of(p: dict, tin: int, tout: int) -> float:
    return tin / 1_000_000 * p["price_in"] + tout / 1_000_000 * p["price_out"]


def render_prompt(payload: dict) -> str:
    """把 OpenAI 的 messages 摊平成一段文本（OpenCode 收的是单条 prompt）。"""
    msgs = payload.get("messages") or []
    if len(msgs) == 1 and str(msgs[0].get("role")) == "user":
        content = msgs[0].get("content")
        if isinstance(content, str):
            return content
    out = []
    for m in msgs:
        role = str(m.get("role") or "user")
        content = m.get("content")
        if isinstance(content, list):      # 多模态分片，只取文本部分
            content = " ".join(str(x.get("text") or "")
                               for x in content if isinstance(x, dict))
        label = {"system": "System", "user": "User",
                 "assistant": "Assistant"}.get(role, role)
        out.append(f"{label}: {content}")
    return "\n\n".join(out)


def call_provider(p: dict, payload: dict) -> tuple[dict, str]:
    """统一调用入口，返回 (OpenAI 形状的 dict, 警告信息)。

    type=http     → 打上游的 OpenAI 兼容 /chat/completions
    type=opencode → 驱动本地 opencode serve（建会话→发消息→删会话）
    """
    if p["type"] == "opencode":
        SUPERVISOR.ensure()             # 没在跑就自动拉起（失败不影响下面报错）
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
        return data, check_model(p, {"model": upstream})

    req = urllib.request.Request(
        p["base_url"] + "/chat/completions",
        data=build_body(p, payload, bool(payload.get("stream"))),
        headers=headers_for(p, bool(payload.get("stream"))),
        method="POST")
    try:
        # 每个上游可以单独指定代理；没指定就用全局代理
        proxy = p.get("proxy") or RUNTIME.get("proxy") or ""
        if proxy:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
            resp = opener.open(req, timeout=p["timeout"])
        else:
            resp = urllib.request.urlopen(req, timeout=p["timeout"])
        try:
            raw = resp.read()
        finally:
            resp.close()
    except Exception as exc:                  # noqa: BLE001
        raise ProviderError(err_text(exc)) from None

    try:
        data = json.loads(raw.decode("utf-8", "ignore"))
    except Exception as exc:                  # noqa: BLE001
        raise ProviderError(f"响应不是合法 JSON：{exc}") from None
    return data, check_model(p, data)


def authorized(handler: BaseHTTPRequestHandler) -> bool:
    # 走飞牛统一网关：fnOS 已经校验过 NAS 登录态，直接放行
    if getattr(handler.server, "is_gateway", False):
        return bool(handler.headers.get("X-Trim-Userid"))
    token = RUNTIME["gateway_token"]
    if not token:
        return True
    auth = handler.headers.get("Authorization", "")
    got = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    return got == token


def is_admin(handler: BaseHTTPRequestHandler) -> bool:
    """写操作权限：走网关时要求 NAS 管理员；端口直连 + 口令通过视为管理员。"""
    if getattr(handler.server, "is_gateway", False):
        return str(handler.headers.get("X-Trim-Isadmin", "")).lower() == "true"
    return True


# ─────────────────────────── HTTP 处理 ───────────────────────────
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "DeepSeekGateway/1.0"

    # ---- 输出工具 ----
    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def send_json(self, code: int, obj, extra: dict | None = None) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8", extra)

    def _chunk(self, data: bytes) -> None:
        self.wfile.write(b"%x\r\n" % len(data) + data + b"\r\n")
        self.wfile.flush()

    def log_message(self, fmt, *args):        # 安静一点
        pass

    # ---- 路由 ----
    def _path(self) -> str:
        """把统一网关前缀剥掉，让同一套路由在两种入口下都能用。"""
        path = urlparse(self.path).path or "/"
        if GATEWAY_PREFIX and path.startswith(GATEWAY_PREFIX):
            path = path[len(GATEWAY_PREFIX):] or "/"
        return path

    def _safe(self, fn) -> None:
        """任何未捕获异常都变成 500 JSON，而不是直接掐断连接。"""
        try:
            fn()
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:                    # noqa: BLE001
            try:
                self.send_json(500, {"ok": False,
                                     "error": f"{type(exc).__name__}: {exc}"})
            except Exception:
                pass

    def do_GET(self) -> None:
        self._safe(self._do_get)

    def _do_get(self) -> None:
        path = self._path()
        if path == "/healthz":
            self.send_json(200, {"ok": True, "providers": len(PROVIDERS)})
        elif path in ("/", "/admin", "/admin/"):
            self._send(200, STATUS_HTML.encode("utf-8"), "text/html; charset=utf-8")
        elif path in ("/settings", "/settings/"):
            self._send(200, SETTINGS_HTML.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/admin/api/config":
            if not authorized(self):
                self.send_json(401, {"error": "unauthorized"})
                return
            self.send_json(200, config_snapshot())
        elif path == "/admin/api/opencode":
            if not authorized(self):
                self.send_json(401, {"error": "unauthorized"})
                return
            self.send_json(200, SUPERVISOR.status())
        elif path == "/v1/models":
            if not authorized(self):
                self.send_json(401, {"error": "unauthorized"})
                return
            seen, data = set(), []
            for p in PROVIDERS:
                if p["model"] in seen:
                    continue
                seen.add(p["model"])
                data.append({"id": p["model"], "object": "model", "owned_by": p["name"]})
            self.send_json(200, {"object": "list", "data": data})
        elif path == "/admin/api/status":
            if not authorized(self):
                self.send_json(401, {"error": "unauthorized"})
                return
            self.send_json(200, self.status_payload())
        else:
            self.send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        self._safe(self._do_post)

    def _do_post(self) -> None:
        path = self._path()
        if path == "/v1/chat/completions":
            self.handle_chat()
        elif path == "/admin/api/config":
            self.handle_save_config()
        elif path == "/admin/api/opencode":
            self.handle_opencode()
        elif path == "/admin/api/reload":
            if not authorized(self):
                self.send_json(401, {"error": "unauthorized"})
                return
            try:
                load_config()
                self.send_json(200, {"ok": True, "providers": len(PROVIDERS)})
            except Exception as exc:               # noqa: BLE001
                self.send_json(500, {"ok": False, "error": str(exc)})
        elif path == "/admin/api/toggle":
            self.handle_toggle()
        elif path == "/admin/api/probe":
            self.handle_probe()
        elif path == "/admin/api/set-key":
            self.handle_set_key()
        elif path == "/admin/api/set-settings":
            self.handle_set_settings()
        else:
            self.send_json(404, {"error": "not found"})

    # ---- 管理接口 ----
    def handle_probe(self) -> None:
        """单上游连通性自检：真发一次极小请求，把结果（含报错）返回给面板。"""
        if not authorized(self):
            self.send_json(401, {"error": "unauthorized"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            name = str(json.loads(self.rfile.read(length) or b"{}").get("name", ""))
        except Exception:
            self.send_json(400, {"ok": False, "error": "bad request"})
            return

        p = next((x for x in PROVIDERS if x["name"] == name), None)
        if p is None:
            self.send_json(404, {"ok": False, "error": "unknown provider"})
            return
        if not p["api_key"] and p["type"] != "opencode":
            self.send_json(200, {"ok": False, "provider": name,
                                 "error": "没有密钥，先在上游配置里填 api_key_env"})
            return

        payload = {"messages": [{"role": "user", "content": "回复 OK 两个字，不要做其他操作"}]}
        t0 = time.time()
        try:
            data, warn = call_provider(p, payload)
            try:
                sample = str(data["choices"][0]["message"]["content"] or "")
            except Exception:                       # noqa: BLE001
                sample = ""
            self.send_json(200, {
                "ok": True, "provider": name,
                "ms": int((time.time() - t0) * 1000),
                "sample": sample[:200],
                "warning": warn,
                "usage": data.get("usage") or {},
            })
        except Exception as exc:                    # noqa: BLE001
            self.send_json(200, {
                "ok": False, "provider": name,
                "ms": int((time.time() - t0) * 1000),
                "error": err_text(exc)[:300],
            })

    def handle_opencode(self) -> None:
        """OpenCode 运行时控制：start / stop / install / log。"""
        if not authorized(self):
            self.send_json(401, {"error": "unauthorized"})
            return
        if not is_admin(self):
            self.send_json(403, {"ok": False, "error": "只有 NAS 管理员能操作运行时"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            action = str(json.loads(self.rfile.read(length) or b"{}").get("action", ""))
        except Exception:
            self.send_json(400, {"ok": False, "error": "bad request"})
            return

        if action == "start":
            ok, msg = SUPERVISOR.start()
        elif action == "stop":
            ok, msg = SUPERVISOR.stop()
        elif action == "install":
            ok, msg = SUPERVISOR.install()
        elif action == "log":
            ok, msg = True, "ok"
        else:
            self.send_json(400, {"ok": False, "error": "action 只能是 start/stop/install/log"})
            return

        payload = SUPERVISOR.status()
        payload.update({"ok": ok, "message": msg})
        self.send_json(200, payload)

    def handle_save_config(self) -> None:
        """设置页整页保存。"""
        if not authorized(self):
            self.send_json(401, {"error": "unauthorized"})
            return
        if not is_admin(self):
            self.send_json(403, {"ok": False, "error": "只有 NAS 管理员能改配置"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            self.send_json(400, {"ok": False, "error": "bad request"})
            return
        try:
            snap = config_save(body)
        except Exception as exc:                    # noqa: BLE001
            self.send_json(400, {"ok": False, "error": str(exc)})
            return
        self.send_json(200, {"ok": True, **snap})

    def handle_set_settings(self) -> None:
        """面板里改排序策略 / 冷却 / 访问口令（写 data/settings.json）。"""
        if not authorized(self):
            self.send_json(401, {"error": "unauthorized"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            self.send_json(400, {"ok": False, "error": "bad request"})
            return

        patch: dict = {}
        if "strategy" in body:
            s = str(body["strategy"]).strip().lower()
            if s not in ("priority", "cheapest"):
                self.send_json(400, {"ok": False, "error": "strategy 只能是 priority / cheapest"})
                return
            patch["strategy"] = s
        if "cooldown_seconds" in body:
            try:
                cd = int(body["cooldown_seconds"])
            except Exception:
                self.send_json(400, {"ok": False, "error": "冷却必须是整数秒"})
                return
            patch["cooldown_seconds"] = max(1, min(cd, 3600))
        if "gateway_token" in body:
            patch["gateway_token"] = str(body["gateway_token"] or "").strip()

        if not patch:
            self.send_json(400, {"ok": False, "error": "没有要更新的字段"})
            return

        try:
            save_runtime(patch)
            load_runtime()
        except Exception as exc:                    # noqa: BLE001
            self.send_json(500, {"ok": False, "error": str(exc)})
            return

        self.send_json(200, {
            "ok": True,
            "strategy": RUNTIME["strategy"],
            "cooldown_seconds": RUNTIME["cooldown"],
            "auth_enabled": bool(RUNTIME["gateway_token"]),
        })

    def handle_set_key(self) -> None:
        """面板里直接填/清某个上游的 Key（写进 data/secrets.json，优先于向导）。"""
        if not authorized(self):
            self.send_json(401, {"error": "unauthorized"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            name = str(body["name"])
            key = str(body.get("key", "")).strip()
        except Exception:
            self.send_json(400, {"ok": False, "error": "bad request"})
            return

        p = next((x for x in PROVIDERS if x["name"] == name), None)
        if p is None:
            self.send_json(404, {"ok": False, "error": "unknown provider"})
            return
        env = p["key_env"]
        if not env:
            self.send_json(400, {"ok": False,
                                 "error": "该上游没有 api_key_env，请直接在 providers.json 里填 api_key"})
            return

        try:
            save_secret(env, key)
            load_config()
        except Exception as exc:                    # noqa: BLE001
            self.send_json(500, {"ok": False, "error": str(exc)})
            return

        now = next((x for x in PROVIDERS if x["name"] == name), None)
        self.send_json(200, {"ok": True, "name": name,
                             "has_key": bool(now and now["api_key"]),
                             "source": key_source(env)})

    def status_payload(self) -> dict:
        now = time.time()
        usage = usage_summary()
        used = used_tokens()
        sec = load_secrets()
        rows = []
        for p in PROVIDERS:
            with STATE_LOCK:
                st = dict(STATE.get(p["name"], {}))
            u = usage.get(p["name"], {})
            source = provider_key_source(p, sec)
            rows.append({
                "name": p["name"],
                "enabled": p["enabled"],
                "has_key": bool(p["api_key"]) or p["type"] == "opencode",
                "type": p["type"],
                "key_env": p["key_env"],
                "key_source": source,
                "priority": p["priority"],
                "model": p["model"],
                "quant": p["quant"],
                "price_in": p["price_in"],
                "price_out": p["price_out"],
                "cooldown": max(0, int(st.get("cooldown_until", 0) - now)),
                "fails": st.get("fails", 0),
                "ok": st.get("ok", 0),
                "last_error": st.get("last_error", ""),
                "requests_24h": u.get("requests_24h", 0),
                "tokens_in_24h": u.get("tokens_in_24h", 0),
                "tokens_out_24h": u.get("tokens_out_24h", 0),
                "cost_24h": u.get("cost_24h", 0.0),
                "errors_24h": u.get("errors_24h", 0),
                "note": p["note"],
                "free_only": p["free_only"],
                "free_quota_tokens": p["free_quota_tokens"],
                "used_tokens": used.get(p["name"], 0),
                "exhausted": is_exhausted(p, used),
            })
        return {
            "strategy": RUNTIME["strategy"],
            "cooldown_seconds": RUNTIME["cooldown"],
            "auth_enabled": bool(RUNTIME["gateway_token"]),
            "total_cost_24h": round(sum(r["cost_24h"] for r in rows), 6),
            "total_requests_24h": sum(r["requests_24h"] for r in rows),
            "providers": rows,
        }

    def handle_toggle(self) -> None:
        if not authorized(self):
            self.send_json(401, {"error": "unauthorized"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            name = str(body["name"])
            enabled = bool(body["enabled"])
        except Exception:
            self.send_json(400, {"ok": False, "error": "bad request"})
            return
        if name not in {p["name"] for p in PROVIDERS}:
            self.send_json(404, {"ok": False, "error": "unknown provider"})
            return
        ov = read_overrides()
        ov[name] = enabled
        write_overrides(ov)
        load_config()
        self.send_json(200, {"ok": True, "name": name, "enabled": enabled})

    # ---- 主接口 ----
    def handle_chat(self) -> None:
        if not authorized(self):
            self.send_json(401, {"error": {"message": "invalid gateway token",
                                           "type": "unauthorized"}})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            self.send_json(400, {"error": {"message": "invalid JSON body",
                                           "type": "bad_request"}})
            return

        strategy = (self.headers.get("X-Route-Strategy")
                    or RUNTIME["strategy"] or "priority").lower()
        if strategy not in ("priority", "cheapest"):
            strategy = "priority"

        if payload.get("stream"):
            self.handle_stream(payload, strategy)
        else:
            self.handle_nonstream(payload, strategy)

    def _emit_buffered_stream(self, p: dict, data: dict, warn: str) -> None:
        """把一次性结果伪装成 SSE 流（opencode 上游没有真流式）。"""
        try:
            text = str(data["choices"][0]["message"]["content"] or "")
        except Exception:                          # noqa: BLE001
            text = ""
        model = data.get("model") or p["model"]
        cid = data.get("id") or f"chatcmpl-{int(time.time())}"

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("X-Gateway-Provider", p["name"])
        self.send_header("X-Gateway-Stream", "buffered")
        if warn:
            self.send_header("X-Gateway-Warning", warn)
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        try:
            step = max(1, len(text) // 4) if text else 1
            for i in range(0, len(text), step):
                ev = {"id": cid, "object": "chat.completion.chunk", "model": model,
                      "choices": [{"index": 0,
                                   "delta": {"content": text[i:i + step]}}]}
                self._chunk(f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode())
            tail = {"id": cid, "object": "chat.completion.chunk", "model": model,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    "usage": data.get("usage")}
            self._chunk(f"data: {json.dumps(tail, ensure_ascii=False)}\n\n".encode())
            self._chunk(b"data: [DONE]\n\n")
        except (BrokenPipeError, ConnectionResetError):
            return
        finally:
            try:
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except Exception:                      # noqa: BLE001
                pass

    def handle_nonstream(self, payload: dict, strategy: str) -> None:
        tried, last = [], None
        for p in candidates(strategy):
            tried.append(p["name"])
            t0 = time.time()
            try:
                data, warn = call_provider(p, payload)
                u = data.get("usage") or {}
                tin = int(u.get("prompt_tokens") or 0)
                tout = int(u.get("completion_tokens") or 0)
                record(p["name"], p["model"], tin, tout,
                       cost_of(p, tin, tout), True,
                       int((time.time() - t0) * 1000), warn)
                mark_ok(p)
                extra = {"X-Gateway-Provider": p["name"]}
                if warn:
                    extra["X-Gateway-Warning"] = warn
                self.send_json(200, data, extra)
                return
            except Exception as exc:               # noqa: BLE001
                msg = err_text(exc)
                mark_fail(p, ProviderError(msg))
                record(p["name"], p["model"], 0, 0, 0.0, False,
                       int((time.time() - t0) * 1000), msg)
                last = msg
        self.send_json(502, {"error": {
            "message": f"所有上游均失败：{last}（已尝试：{', '.join(tried) or '无可用上游'}）",
            "type": "gateway_error"}})

    def handle_stream(self, payload: dict, strategy: str) -> None:
        tried, last = [], None
        for p in candidates(strategy):
            tried.append(p["name"])
            t0 = time.time()

            # opencode 上游没有流式：先阻塞拿到结果，再分块吐给客户端
            if p["type"] == "opencode":
                try:
                    data, warn = call_provider(p, payload)
                except Exception as exc:           # noqa: BLE001
                    msg = err_text(exc)
                    mark_fail(p, ProviderError(msg))
                    record(p["name"], p["model"], 0, 0, 0.0, False,
                           int((time.time() - t0) * 1000), msg)
                    last = msg
                    continue
                self._emit_buffered_stream(p, data, warn)
                u = data.get("usage") or {}
                tin = int(u.get("prompt_tokens") or 0)
                tout = int(u.get("completion_tokens") or 0)
                record(p["name"], p["model"], tin, tout,
                       cost_of(p, tin, tout), True,
                       int((time.time() - t0) * 1000), warn)
                mark_ok(p)
                return

            try:
                resp = open_upstream(p, payload, True)
            except Exception as exc:               # noqa: BLE001
                msg = err_text(exc)
                mark_fail(p, ProviderError(msg))
                record(p["name"], p["model"], 0, 0, 0.0, False,
                       int((time.time() - t0) * 1000), msg)
                last = msg
                continue

            # 连接已建立，开始向客户端输出
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("X-Gateway-Provider", p["name"])
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()

            tin = tout = 0
            committed = False
            try:
                for raw_line in resp:
                    line = raw_line.decode("utf-8", "ignore").strip()
                    if not line:
                        continue
                    if line.startswith("data:"):
                        chunk = line[5:].strip()
                        if chunk == "[DONE]":
                            self._chunk(b"data: [DONE]\n\n")
                            break
                        try:
                            obj = json.loads(chunk)
                        except json.JSONDecodeError:
                            self._chunk((line + "\n\n").encode())
                            committed = True
                            continue
                        u = obj.get("usage") or {}
                        if u:
                            tin = int(u.get("prompt_tokens") or tin)
                            tout = int(u.get("completion_tokens") or tout)
                    self._chunk((line + "\n\n").encode())
                    committed = True
                mark_ok(p)
                record(p["name"], p["model"], tin, tout,
                       cost_of(p, tin, tout), True,
                       int((time.time() - t0) * 1000), "")
            except (BrokenPipeError, ConnectionResetError):
                return                                  # 客户端断开，不记错误
            except Exception as exc:                    # noqa: BLE001
                msg = err_text(exc)
                mark_fail(p, ProviderError(msg))
                record(p["name"], p["model"], 0, 0, 0.0, False,
                       int((time.time() - t0) * 1000), msg)
                if committed:
                    # 已经吐过内容，再切换会让客户端收到重复文本
                    err = json.dumps({"error": {
                        "message": f"上游 {p['name']} 中途断开：{msg}",
                        "type": "gateway_stream_error"}}, ensure_ascii=False)
                    try:
                        self._chunk(f"data: {err}\n\n".encode())
                        self._chunk(b"data: [DONE]\n\n")
                    except Exception:
                        pass
            finally:
                try:
                    resp.close()
                except Exception:
                    pass
                try:
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                except Exception:
                    pass
            return

        self.send_json(502, {"error": {
            "message": f"所有上游均失败：{last}（已尝试：{', '.join(tried) or '无可用上游'}）",
            "type": "gateway_error"}})


_CSS = """
  :root{--bg:#0e1116;--card:#161b22;--line:#232a33;--fg:#e6edf3;--dim:#8b949e;
        --ok:#3fb950;--warn:#d29922;--err:#f85149;--acc:#58a6ff}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--fg);
       font:14px/1.5 -apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}
  .wrap{max-width:1280px;margin:0 auto;padding:20px 14px 56px}
  h1{font-size:17px;margin:0 0 4px}
  h2{font-size:13px;margin:22px 0 10px;color:var(--dim);font-weight:600}
  .sub{color:var(--dim);font-size:12px}
  a{color:var(--acc);text-decoration:none}
  .cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px;margin:16px 0}
  .card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:13px}
  .card .k{color:var(--dim);font-size:12px}
  .card .v{font-size:21px;font-weight:600;margin-top:3px}
  table{width:100%;border-collapse:collapse;background:var(--card);
        border:1px solid var(--line);border-radius:10px;overflow:hidden}
  th,td{padding:9px 11px;text-align:left;border-bottom:1px solid var(--line);font-size:13px}
  th{color:var(--dim);font-weight:500;font-size:12px;background:#11161d;white-space:nowrap}
  tr:last-child td{border-bottom:none}
  .tag{display:inline-block;padding:1px 7px;border-radius:99px;font-size:11px;white-space:nowrap}
  .g{background:rgba(63,185,80,.15);color:var(--ok)}
  .y{background:rgba(210,153,34,.15);color:var(--warn)}
  .r{background:rgba(248,81,73,.15);color:var(--err)}
  .d{background:#21262d;color:var(--dim)}
  .mono{font-family:ui-monospace,Menlo,Consolas,monospace}
  button{background:#21262d;color:var(--fg);border:1px solid var(--line);
         border-radius:7px;padding:5px 10px;cursor:pointer;font-size:12px}
  button:hover{border-color:var(--acc)}
  button.pri{background:#1f6feb;border-color:#1f6feb}
  button.danger{color:var(--err)}
  input,select{background:#0d1117;color:var(--fg);border:1px solid var(--line);
       border-radius:7px;padding:5px 8px;font-size:12px;font-family:inherit}
  input:focus,select:focus{outline:none;border-color:var(--acc)}
  .row{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:12px 0}
  .err{color:var(--err);font-size:11px;max-width:260px;overflow:hidden;
       text-overflow:ellipsis;white-space:nowrap;display:inline-block}
  .nm{font-weight:600}
  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:12px}
  .prov{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px}
  .prov .hd{display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:8px}
  .fld{display:flex;gap:6px;align-items:center;margin:5px 0}
  .fld>label{color:var(--dim);font-size:12px;min-width:74px;flex:none}
  .fld>input,.fld>select{flex:1;min-width:0}
  .keycell{display:flex;gap:6px;align-items:center}
"""

_COMMON_JS = """
const BASE = (location.pathname.replace(/\\/(admin|settings)\\/?$/, '') || '').replace(/\\/$/, '');
const tok = () => localStorage.getItem('gw_token') || '';
const H = () => ({'Authorization':'Bearer '+tok(),'Content-Type':'application/json'});
function msg(t){ const e=document.getElementById('msg'); if(e) e.textContent = t; }
async function api(path, opts){
  const o = Object.assign({headers:H()}, opts||{});
  const r = await fetch(BASE + path, o);
  if(r.status===401){ msg('未登录或口令不对'); throw new Error('unauthorized'); }
  return r.json();
}
function esc(s){ return String(s==null?'':s).replace(/&/g,'&amp;').replace(/"/g,'&quot;')
  .replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
document.querySelectorAll('[data-nav]').forEach(a => { a.href = BASE + a.dataset.nav; });
"""

_STATUS_BODY = """
<div class="wrap">
  <h1>DeepSeek 网关 · 运行状态</h1>
  <div class="sub">一个端点 · 多上游自动切换 · 每 5 秒刷新 ·
    <a href="#" data-nav="/settings">⚙ 设置</a></div>

  <div class="cards">
    <div class="card"><div class="k">24h 请求数</div><div class="v" id="reqs">-</div></div>
    <div class="card"><div class="k">24h 花费 (USD)</div><div class="v" id="cost">-</div></div>
    <div class="card"><div class="k">可用上游</div><div class="v" id="live">-</div></div>
    <div class="card"><div class="k">排序策略</div><div class="v" id="strat" style="font-size:15px">-</div></div>
  </div>

  <div class="row">
    <button onclick="load()">刷新</button>
    <button onclick="reloadCfg()">重载配置</button>
    <span class="sub" id="msg"></span>
  </div>

  <table>
    <thead><tr>
      <th>上游</th><th>状态</th><th>模型</th><th>量化</th><th>免费额度</th>
      <th>输入 $/M</th><th>输出 $/M</th><th>24h 请求</th>
      <th>24h tokens</th><th>24h 花费</th><th>操作</th><th>最近错误</th>
    </tr></thead>
    <tbody id="tbody"></tbody>
  </table>
</div>
"""

_STATUS_JS = """
async function load(){
  try{
    const d = await api('/admin/api/status');
    document.getElementById('reqs').textContent = d.total_requests_24h;
    document.getElementById('cost').textContent = '$' + d.total_cost_24h.toFixed(4);
    document.getElementById('live').textContent =
      d.providers.filter(p=>p.enabled&&p.has_key&&p.cooldown===0&&!p.exhausted).length
      + ' / ' + d.providers.length;
    document.getElementById('strat').textContent = d.strategy;
    document.getElementById('tbody').innerHTML = d.providers.map(p=>{
      let st, cls;
      if(!p.enabled){ st='已停用'; cls='d'; }
      else if(!p.has_key){ st='缺密钥'; cls='d'; }
      else if(p.exhausted){ st='额度用尽'; cls='r'; }
      else if(p.cooldown>0){ st='冷却 '+p.cooldown+'s'; cls='r'; }
      else if(p.fails>0){ st='重试中'; cls='y'; }
      else { st='正常'; cls='g'; }
      let quota;
      if(!p.free_only){ quota = '<span class="sub">付费</span>'; }
      else if(p.free_quota_tokens>0){
        const pct = Math.min(100, p.used_tokens / p.free_quota_tokens * 100);
        const col = p.exhausted ? 'var(--err)' : (pct > 80 ? 'var(--warn)' : 'var(--ok)');
        quota = `<span style="color:${col}">${p.used_tokens.toLocaleString()}</span>`
              + `<span class="sub"> / ${p.free_quota_tokens.toLocaleString()}</span>`;
      } else { quota = '<span class="sub">免费(不限)</span>'; }
      const typeTag = p.type === 'opencode' ? ' <span class="tag d">opencode</span>' : '';
      const btn = p.enabled
        ? `<button onclick="tog('${p.name}',false)">停用</button>`
        : `<button onclick="tog('${p.name}',true)">启用</button>`;
      return `<tr>
        <td><span class="nm">${p.name}</span>${typeTag}<div class="sub">${esc(p.note||'')}</div></td>
        <td><span class="tag ${cls}">${st}</span></td>
        <td class="mono">${esc(p.model)}</td>
        <td>${p.quant}</td>
        <td>${quota}</td>
        <td class="mono">${p.price_in.toFixed(4)}</td>
        <td class="mono">${p.price_out.toFixed(4)}</td>
        <td>${p.requests_24h}</td>
        <td>${(p.tokens_in_24h+p.tokens_out_24h).toLocaleString()}</td>
        <td class="mono">$${p.cost_24h.toFixed(4)}</td>
        <td>${btn} <button onclick="probe('${p.name}')">自检</button></td>
        <td><span class="err" title="${esc(p.last_error)}">${esc(p.last_error)||'-'}</span></td>
      </tr>`;
    }).join('');
  }catch(e){ if(e.message!=='unauthorized') msg('加载失败：'+e.message); }
}
async function tog(name, enabled){
  try{ await api('/admin/api/toggle', {method:'POST', body: JSON.stringify({name, enabled})}); }
  catch(e){}
  load();
}
async function probe(name){
  msg('正在自检 ' + name + ' …');
  try{
    const d = await api('/admin/api/probe', {method:'POST', body: JSON.stringify({name})});
    if(d.ok){ msg('[通] ' + name + '  ' + d.ms + 'ms  ' + (d.sample || '').slice(0, 80)); }
    else { msg('[失败] ' + name + '  ' + (d.ms || 0) + 'ms  ' + (d.error || '')); }
  }catch(e){ msg('自检失败：' + e.message); }
  load();
}
async function reloadCfg(){
  try{ const d = await api('/admin/api/reload', {method:'POST'}); msg(d.ok ? '配置已重载' : ('失败：'+d.error)); }
  catch(e){}
  load();
}
load(); setInterval(load, 5000);
"""

_SETTINGS_BODY = """
<div class="wrap">
  <h1>DeepSeek 网关 · 设置</h1>
  <div class="sub">改完点「保存全部」立即生效 · <a href="#" data-nav="/admin">← 返回运行状态</a></div>

  <h2>全局</h2>
  <div class="row">
    <span class="sub" style="margin:0">访问口令</span>
    <input id="setTok" type="password" placeholder="留空 = 免口令" style="max-width:170px">
    <button onclick="clearTok()">清除</button>
    <span class="sub" style="margin:0">策略</span>
    <select id="setStrat" style="max-width:130px">
      <option value="priority">稳定优先</option>
      <option value="cheapest">省钱优先</option>
    </select>
    <span class="sub" style="margin:0">冷却(秒)</span>
    <input id="setCd" type="number" min="1" max="3600" style="max-width:80px">
  </div>
  <div class="row">
    <span class="sub" style="margin:0">全局代理</span>
    <input id="setProxy" placeholder="http://192.168.1.10:7890（留空 = 不用代理）" style="flex:1;min-width:280px">
  </div>

  <h2>OpenCode 运行时（给 type=opencode 的上游用）</h2>
  <div class="prov" style="max-width:840px">
    <div class="hd"><span class="nm">opencode serve</span>
      <span><span id="ocState" class="tag d">…</span></span></div>
    <div class="sub" id="ocEnv" style="margin-bottom:6px">检测中…</div>
    <div class="fld"><label>二进制</label><input id="ocBin" placeholder="留空 = 自动查找（应用私有安装 / PATH）"></div>
    <div class="fld"><label>端口</label><input id="ocPort" type="number" placeholder="4096"></div>
    <div class="fld"><label>工作目录</label><input id="ocWs" placeholder="留空 = 应用数据目录下 opencode-workspace"></div>
    <div class="fld"><label>自动重启</label><input id="ocAuto" type="checkbox"></div>
    <div class="fld"><label></label>
      <button onclick="ocAction('install')">一键安装</button>
      <button onclick="ocAction('start')">启动</button>
      <button onclick="ocAction('stop')">停止</button>
      <button onclick="ocRefresh()">刷新状态</button>
    </div>
    <pre id="ocLog" style="max-height:170px;overflow:auto;background:#0d1117;border:1px solid var(--line);border-radius:8px;padding:8px;font-size:11px;margin:8px 0 0"></pre>
  </div>

  <div class="row">
    <button class="pri" onclick="save()">保存全部</button>
    <button onclick="addProv()">+ 新增上游</button>
    <button onclick="loadCfg()">重新载入</button>
    <span class="sub" id="msg"></span>
  </div>

  <h2>上游（每张卡片可单独配接口 / 模型 / 代理 / 密钥）</h2>
  <div class="grid" id="grid"></div>
</div>
"""

_SETTINGS_JS = """
let CFG = null;

function fld(label, inner){ return `<div class="fld"><label>${label}</label>${inner}</div>`; }

function provCard(p, i){
  const link = p.console_url
    ? `<a href="${esc(p.console_url)}" target="_blank" rel="noopener">去获取 Key ↗</a>` : '';
  return `<div class="prov" data-i="${i}">
    <div class="hd"><input data-f="name" value="${esc(p.name)}" style="font-weight:600;flex:1">
      <span>${link}</span></div>
    ${fld('启用', `<input type="checkbox" data-f="enabled" ${p.enabled?'checked':''}>`)}
    ${fld('类型', `<select data-f="type">
        <option value="http" ${p.type!=='opencode'?'selected':''}>http（OpenAI 兼容）</option>
        <option value="opencode" ${p.type==='opencode'?'selected':''}>opencode（本地 server）</option>
      </select>`)}
    ${fld('接口', `<input data-f="base_url" value="${esc(p.base_url)}">`)}
    ${fld('模型', `<input data-f="model" value="${esc(p.model)}">`)}
    ${fld('校验串', `<input data-f="model_contains" value="${esc(p.model_contains)}" placeholder="留空 = 不校验">`)}
    ${fld('严格校验', `<input type="checkbox" data-f="strict_model" ${p.strict_model?'checked':''}>`)}
    ${fld('优先级', `<input type="number" data-f="priority" value="${p.priority||100}">`)}
    ${fld('免费额度', `<input type="checkbox" data-f="free_only" ${p.free_only?'checked':''}>
        <input type="number" data-f="free_quota_tokens" value="${p.free_quota_tokens||0}" placeholder="tokens，0=不限">`)}
    ${fld('价格/M', `<input type="number" step="0.0001" data-f="price_in" value="${p.price_in||0}" placeholder="输入">
        <input type="number" step="0.0001" data-f="price_out" value="${p.price_out||0}" placeholder="输出">`)}
    ${fld('代理', `<input data-f="proxy" value="${esc(p.proxy)}" placeholder="留空 = 用全局">`)}
    ${fld('Key 变量', `<input data-f="api_key_env" value="${esc(p.api_key_env)}" placeholder="环境变量名">`)}
    ${fld('Key', `<input type="password" class="kval" data-env="${esc(p.api_key_env)}"
        placeholder="${esc(p._key_source||'无')}">`)}
    ${fld('控制台', `<input data-f="console_url" value="${esc(p.console_url)}" placeholder="获取 Key 的页面">`)}
    ${fld('备注', `<input data-f="note" value="${esc(p.note)}">`)}
    <div class="fld"><label></label>
      <button onclick="probeOne('${esc(p.name)}')">自检</button>
      <button class="danger" onclick="delProv(${i})">删除</button></div>
  </div>`;
}

function render(){
  document.getElementById('grid').innerHTML = CFG.providers.map(provCard).join('');
  document.getElementById('setStrat').value = CFG.strategy;
  document.getElementById('setCd').value = CFG.cooldown_seconds;
  document.getElementById('setProxy').value = CFG.proxy || '';
  document.getElementById('setTok').placeholder =
    CFG.auth_enabled ? '已设置（留空不改）' : '留空 = 免口令';
  const oc = CFG.opencode || {};
  document.getElementById('ocPort').value = oc.port || 4096;
  document.getElementById('ocBin').value = oc.bin || '';
  document.getElementById('ocWs').value = oc.workspace || '';
  document.getElementById('ocAuto').checked = oc.autostart !== false;
  ocRefresh();
}

async function loadCfg(){
  try{
    CFG = await api('/admin/api/config');
    if(!CFG.providers) CFG.providers = [];
    render();
  }catch(e){ if(e.message!=='unauthorized') msg('载入失败：'+e.message); }
}

function collect(){
  const providers = [...document.querySelectorAll('.prov')].map(el=>{
    const o = {};
    el.querySelectorAll('[data-f]').forEach(inp=>{
      const f = inp.dataset.f;
      if(inp.type==='checkbox') o[f] = inp.checked;
      else if(inp.type==='number') o[f] = inp.value==='' ? 0 : Number(inp.value);
      else o[f] = inp.value;
    });
    const src = CFG.providers[Number(el.dataset.i)];
    o._source = (src && src._source) || 'main';
    return o;
  });
  const secrets = {};
  document.querySelectorAll('.kval').forEach(inp=>{
    const v = (inp.value||'').trim(), env = inp.dataset.env;
    if(v && env) secrets[env] = v;
  });
  const body = {
    strategy: document.getElementById('setStrat').value,
    cooldown_seconds: Number(document.getElementById('setCd').value || 60),
    proxy: document.getElementById('setProxy').value,
    providers: providers,
    secrets: secrets
  };
  const t = (document.getElementById('setTok').value || '').trim();
  if(t) body.gateway_token = t;
  body.opencode_bin = document.getElementById('ocBin').value;
  body.opencode_port = Number(document.getElementById('ocPort').value || 4096);
  body.opencode_workspace = document.getElementById('ocWs').value;
  body.opencode_autostart = document.getElementById('ocAuto').checked;
  return body;
}

async function save(){
  msg('保存中…');
  try{
    const d = await api('/admin/api/config', {method:'POST', body: JSON.stringify(collect())});
    if(d.ok){
      CFG = d; render();
      document.getElementById('setTok').value = '';
      msg('已保存并生效');
    } else { msg('保存失败：' + (d.error || '未知错误')); }
  }catch(e){ msg('保存失败：' + e.message); }
}

function addProv(){
  CFG.providers.push({
    name: 'new-provider-' + (CFG.providers.length + 1), type: 'http', enabled: false,
    priority: 100, base_url: 'https://api.example.com/v1', model: '',
    model_contains: '', free_only: true, free_quota_tokens: 0,
    price_in: 0, price_out: 0, proxy: '', console_url: '', note: '',
    api_key_env: 'NEW_PROVIDER_KEY', _source: 'main', _key_source: '无'
  });
  render();
}

function delProv(i){
  CFG.providers.splice(i, 1);
  render();
  msg('已从列表移除，点「保存全部」才会真正写盘');
}

async function probeOne(name){
  msg('正在自检 ' + name + ' …');
  try{
    const d = await api('/admin/api/probe', {method:'POST', body: JSON.stringify({name})});
    msg(d.ok ? ('[通] ' + name + '  ' + d.ms + 'ms  ' + (d.sample||'').slice(0,60))
             : ('[失败] ' + (d.error||'')));
  }catch(e){ msg('自检失败：' + e.message); }
}

function ocPaint(d){
  const el = document.getElementById('ocState');
  el.textContent = d.running ? ('运行中 pid ' + d.pid)
                             : (d.installed ? '未运行' : '未安装');
  el.className = 'tag ' + (d.running ? 'g' : (d.installed ? 'y' : 'd'));
  document.getElementById('ocLog').textContent =
    (d.last_error ? ('[错误] ' + d.last_error + '
') : '') + (d.log || '(无日志)');
  const env = document.getElementById('ocEnv');
  env.textContent = 'Node ' + (d.node_version || (d.node ? '?' : '未找到'))
    + (d.node ? ('（' + d.node + '）') : '')
    + ' · npm ' + (d.npm_version || '—')
    + ' · opencode ' + (d.bin ? (d.bin_source + '：' + d.bin) : '未安装');
}

async function ocRefresh(){
  try{
    const d = await api('/admin/api/opencode');
    ocPaint(d);
    if(d.bin && !document.getElementById('ocBin').value){
      document.getElementById('ocBin').placeholder = '自动：' + d.bin;
    }
  }catch(e){ if(e.message!=='unauthorized') msg('读取运行时状态失败：'+e.message); }
}

async function ocAction(action){
  msg('执行中：' + action + ' …');
  try{
    const d = await api('/admin/api/opencode', {method:'POST', body: JSON.stringify({action})});
    msg((d.ok ? '[成功] ' : '[失败] ') + (d.message || ''));
    ocPaint(d);
  }catch(e){ msg('操作失败：' + e.message); }
}

async function clearTok(){
  try{ await api('/admin/api/set-settings', {method:'POST', body: JSON.stringify({gateway_token:''})});
       localStorage.removeItem('gw_token'); msg('已清除访问口令'); }
  catch(e){ msg('失败：' + e.message); }
  loadCfg();
}

loadCfg();
"""


def _page(title: str, body: str, script: str) -> str:
    """拼一个页面。注意：不能用 f-string，页面里的 ${...} 是 JS 模板串。"""
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>' + title + '</title><style>' + _CSS + '</style></head><body>'
        + body + '<script>' + _COMMON_JS + script + '</script>'
        '</body></html>'
    )


STATUS_HTML = _page("DeepSeek 网关 · 状态", _STATUS_BODY, _STATUS_JS)
SETTINGS_HTML = _page("DeepSeek 网关 · 设置", _SETTINGS_BODY, _SETTINGS_JS)


def main() -> int:
    os.makedirs(DATA_DIR, exist_ok=True)
    init_db()
    load_config()

    try:
        srv = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError as exc:
        print(f"[gateway] 监听 {HOST}:{PORT} 失败：{exc}", flush=True)
        return 1
    srv.daemon_threads = True
    srv.is_gateway = False

    # 统一网关：监听 Unix Socket，飞牛校验 NAS 登录态后转发（免口令单点登录）
    sock_srv = None
    if SOCKET_PATH and hasattr(socket, "AF_UNIX"):
        try:
            if os.path.exists(SOCKET_PATH):
                os.unlink(SOCKET_PATH)

            class _UnixServer(socketserver.ThreadingUnixStreamServer):
                daemon_threads = True
                is_gateway = True

            sock_srv = _UnixServer(SOCKET_PATH, Handler)
            try:
                os.chmod(SOCKET_PATH, 0o660)
            except Exception:
                pass
            threading.Thread(target=sock_srv.serve_forever, daemon=True).start()
            print(f"[gateway] 统一网关 socket：{SOCKET_PATH}", flush=True)
        except Exception as exc:                    # noqa: BLE001
            print(f"[gateway] 统一网关 socket 启动失败（不影响端口访问）：{exc}", flush=True)
            sock_srv = None

    def _shutdown(signum, frame):        # noqa: ARG001
        for _s in (srv, sock_srv):
            if _s is not None:
                threading.Thread(target=_s.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    threading.Thread(target=SUPERVISOR.watch, daemon=True).start()

    live = sum(1 for p in PROVIDERS if p["enabled"] and p["api_key"])
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
