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
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from opencode_upstream import OpenCodeClient, OpenCodeError
from opencode_supervisor import OpenCodeSupervisor
from proxy_support import build_opener, parse_proxy

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
    ws_default = os.path.join(DATA_DIR, "opencode-workspace")
    return {
        "bin": str(data.get("opencode_bin") or ""),
        "port": port,
        "workspace": str(data.get("opencode_workspace") or ws_default),
        "workspace_default": ws_default,
        "autostart": bool(data.get("opencode_autostart", True)),
        "key_set": bool(str(load_secrets().get("OPENCODE_API_KEY") or "").strip()),
        "enabled": enabled,
    }


SUPERVISOR = OpenCodeSupervisor(DATA_DIR, opencode_opts)

PROVIDERS: list[dict] = []
SETTINGS: dict = {}
STATE: dict[str, dict] = {}
STATE_LOCK = threading.Lock()
_CURRENT = threading.local()      # 当前请求的令牌（线程本地）
CONFIG_LOCK = threading.Lock()


def _safe_header(v) -> str:
    """HTTP 头只能是 latin-1；中文之类的字符会抛异常，统一降级成 ?。"""
    text = str(v)
    try:
        text.encode("latin-1")
        return text
    except UnicodeEncodeError:
        return text.encode("ascii", "replace").decode("ascii")


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


TOKENS_PATH = os.path.join(DATA_DIR, "tokens.json")


def load_tokens() -> list[dict]:
    try:
        with open(TOKENS_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_tokens(items: list[dict]) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = TOKENS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(items, fh, ensure_ascii=False, indent=2)
    try:
        os.chmod(tmp, 0o600)
    except Exception:
        pass
    os.replace(tmp, TOKENS_PATH)


def new_token_key() -> str:
    import secrets
    return "sk-fnos-" + secrets.token_hex(12)


def find_token(presented: str) -> dict | None:
    """按 key 找启用的令牌。"""
    if not presented:
        return None
    for t in load_tokens():
        if t.get("enabled", True) and str(t.get("key", "")) == presented:
            return t
    return None


_TOKEN_CACHE: dict = {"ts": 0.0, "data": {}}


def token_usage() -> dict:
    """每个令牌累计消耗的 token（用于额度控制）。"""
    now = time.time()
    if now - _TOKEN_CACHE["ts"] < 10:
        return _TOKEN_CACHE["data"]
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        rows = conn.execute(
            "SELECT token_id, SUM(tokens_in + tokens_out) FROM usage "
            "WHERE ok=1 AND token_id != '' GROUP BY token_id").fetchall()
        conn.close()
        data = {str(k): int(v or 0) for k, v in rows}
    except Exception:
        data = {}
    _TOKEN_CACHE["ts"] = now
    _TOKEN_CACHE["data"] = data
    return data


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
                   proxy=str(data.get("proxy") or "").strip())


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
                   "note", "api_key_env", "no_auth", "opencode_provider", "opencode_agent")


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
    names_in_payload: list = []
    for raw in body.get("providers") or []:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "").strip()
        if not name:
            raise ValueError("第 %d 个上游缺少 name（可能是页面上有非上游卡片被收集进来了）"
                             % (len(groups.get("main", [])) + 1))
        item = {k: v for k, v in raw.items() if not str(k).startswith("_")}
        item["name"] = name
        item.setdefault("base_url", "")
        item.setdefault("model", "")
        names_in_payload.append(name)
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

    # 卡片上改了「启用」就以配置为准：清掉这些上游在 overrides 里的残留，
    # 否则状态页点过「停用」的记录会一直压过配置里的勾选。
    try:
        ov = read_overrides()
        if ov and names_in_payload:
            hit = [n for n in names_in_payload if n in ov]
            if hit:
                for n in hit:
                    ov.pop(n, None)
                write_overrides(ov)
    except Exception:
        pass

    _ock = str(body.get("opencode_api_key") or "").strip()
    if _ock:
        save_secret("OPENCODE_API_KEY", _ock)
        os.environ["OPENCODE_API_KEY"] = _ock
        os.environ.setdefault("OPENCODE_ZEN_API_KEY", _ock)

    for env, val in (body.get("secrets") or {}).items():
        save_secret(str(env), str(val or ""))

    if patch:
        save_runtime(patch)
    load_config()
    snap = config_snapshot()
    # 回显磁盘上真正存下来的值，便于确认"到底存没存"
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as fh:
            disk = json.load(fh) or {}
        snap["on_disk"] = {
            "proxy": str(disk.get("proxy") or ""),
            "strategy": str(disk.get("strategy") or ""),
            "cooldown_seconds": disk.get("cooldown_seconds"),
            "opencode_port": disk.get("opencode_port"),
        }
    except Exception as exc:                        # noqa: BLE001
        snap["on_disk"] = {"error": str(exc)}
    return snap


def quick_port_check(url: str, timeout: float = 0.6) -> bool:
    """秒级探测某个地址的端口通不通（不启动任何东西）。"""
    try:
        u = urlparse(url if "://" in url else "http://" + url)
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


def discover_models(p: dict) -> dict:
    """问上游「你有哪些模型」。http 走 GET /models，opencode 走 /config/providers。"""
    try:
        if p["type"] == "opencode":
            client = OpenCodeClient(base_url=p["base_url"], model_id="",
                                    provider_id=p["opencode_provider"], timeout=15)
            ids = client.available_models()
            return {"ok": True, "count": len(ids),
                    "models": [{"id": i, "free": "free" in i.lower()} for i in ids]}

        url = p["base_url"].rstrip("/") + "/models"
        req = urllib.request.Request(url, headers=headers_for(p, False), method="GET")
        opener = build_opener(p.get("proxy") or RUNTIME.get("proxy") or "", p["timeout"])
        resp = opener.open(req, timeout=20) if opener else urllib.request.urlopen(req, timeout=20)
        try:
            raw = resp.read()
        finally:
            resp.close()
        data = json.loads(raw.decode("utf-8", "ignore"))
        items = data.get("data") if isinstance(data, dict) else data
        out = []
        for it in items or []:
            if isinstance(it, str):
                mid, free, price = it, "free" in it.lower(), None
            elif isinstance(it, dict):
                mid = str(it.get("id") or it.get("name") or "")
                price = None
                pr = it.get("pricing") or {}
                try:
                    if pr and str(pr.get("prompt", "")).replace(".", "") == "0"                             and str(pr.get("completion", "")).replace(".", "") == "0":
                        price = 0.0
                except Exception:
                    price = None
                free = ("free" in mid.lower()) or price == 0.0
            else:
                continue
            if not mid:
                continue
            out.append({"id": mid, "free": bool(free)})
        return {"ok": True, "count": len(out), "models": out}
    except Exception as exc:                        # noqa: BLE001
        return {"ok": False, "error": err_text(exc)}


def test_proxy(proxy_url: str, target: str = "https://api.deepseek.com/") -> dict:
    """真连一次：走该代理请求一个地址，返回耗时与状态。"""
    opener = build_opener(proxy_url, 8)
    if opener is None:
        return {"ok": False, "error": "没有配置代理", "target": target}
    t0 = time.time()
    try:
        req = urllib.request.Request(target, method="GET",
                                     headers={"User-Agent": "fnos-gateway-probe"})
        with opener.open(req, timeout=8) as r:
            return {"ok": True, "ms": int((time.time() - t0) * 1000),
                    "status": r.status, "target": target}
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            note = "通道正常（目标需要鉴权，401/403 属预期）"
        elif 400 <= exc.code < 500:
            note = "通道正常（目标返回 4xx，说明已经连上）"
        else:
            note = f"通道能连上，但目标返回 {exc.code}"
        return {"ok": True, "ms": int((time.time() - t0) * 1000),
                "status": exc.code, "target": target, "note": note}
    except Exception as exc:                        # noqa: BLE001
        return {"ok": False, "ms": int((time.time() - t0) * 1000),
                "error": f"{type(exc).__name__}: {exc}", "target": target}


def tokens_payload() -> dict:
    used = token_usage()
    out = []
    for t in load_tokens():
        key = str(t.get("key", ""))
        masked = (key[:9] + "****" + key[-4:]) if len(key) > 14 else "****"
        out.append({
            "id": t.get("id", ""),
            "name": t.get("name", ""),
            "key_masked": masked,
            "enabled": bool(t.get("enabled", True)),
            "quota_tokens": int(t.get("quota_tokens") or 0),
            "used_tokens": used.get(str(t.get("id", "")), 0),
            "created": t.get("created", 0),
        })
    has_master = bool(RUNTIME["gateway_token"])
    enabled_tokens = [x for x in out if x["enabled"]]
    return {
        "tokens": out,
        "master": has_master,
        "open_mode": (not has_master and not enabled_tokens),
    }


def update_provider_enabled(name: str, enabled: bool) -> bool:
    """把 enabled 直接写进该上游所在的配置文件（不再用覆盖文件）。"""
    base_dir = os.path.dirname(os.path.abspath(CONFIG_PATH))
    paths = [CONFIG_PATH]
    d = os.path.join(base_dir, "providers.d")
    if os.path.isdir(d):
        paths += [os.path.join(d, f) for f in sorted(os.listdir(d)) if f.endswith(".json")]
    for path in paths:
        try:
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh) or {}
        except Exception:
            continue
        hit = False
        for p in doc.get("providers") or []:
            if str(p.get("name")) == name:
                p["enabled"] = bool(enabled)
                hit = True
                break
        if hit:
            atomic_write_json(path, doc)
            return True
    return False


def save_one_provider(raw: dict) -> dict:
    """只写回一个上游（其它卡片不动）。"""
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
    try:
        with open(path, encoding="utf-8") as fh:
            existing = json.load(fh) or {}
    except Exception:
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
        ov = read_overrides()
        if ov and name in ov:
            ov.pop(name, None)
            write_overrides(ov)
    except Exception:
        pass
    load_config()
    return config_snapshot()


def provider_key_source(p: dict, sec: dict | None = None) -> str:
    """密钥来源：面板 / 向导 / 配置 / 无 / 本地(opencode) / 免密钥。"""
    if p["type"] == "opencode":
        return "本地"
    if p.get("no_auth"):
        return "免密钥"
    if sec is None:
        sec = load_secrets()
    if p["key_env"] and sec.get(p["key_env"]):
        return "面板"
    if p["key_env"] and os.getenv(p["key_env"]):
        return "向导"
    if p["api_key"]:
        return "配置"
    return "无"


def tail_log(path: str, lines: int = 200) -> str:
    """读文件末尾 N 行（最多回看 256KB，避免大文件拖死）。"""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            block = min(size, 256 * 1024)
            fh.seek(size - block)
            data = fh.read().decode("utf-8", "ignore")
        return chr(10).join(data.splitlines()[-max(1, lines):])
    except FileNotFoundError:
        return "（还没有日志：服务还没写出过内容）"
    except Exception as exc:                        # noqa: BLE001
        return f"（读取失败：{exc}）"


def _as_list(v) -> list:
    """把逗号/换行分隔的字符串或数组，统一成字符串列表。"""
    if not v:
        return []
    if isinstance(v, (list, tuple)):
        return [str(x).strip() for x in v if str(x).strip()]
    parts = str(v).replace(chr(10), ",").split(",")
    return [x.strip() for x in parts if x.strip()]


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
        # opencode 认环境变量里的 Console API Key；子进程（serve / run）会继承
        _sec0 = load_secrets()
        _ock = str(_sec0.get("OPENCODE_API_KEY") or "").strip()
        if _ock:
            os.environ["OPENCODE_API_KEY"] = _ock
            os.environ.setdefault("OPENCODE_ZEN_API_KEY", _ock)
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
                "models": _as_list(it.get("models")),
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
                "no_auth": bool(it.get("no_auth", False)),
                "free_only": bool(it.get("free_only", False)),
                "free_quota_tokens": int(it.get("free_quota_tokens", 0) or 0),
                "extra": it.get("extra") or {},
                # type=http：OpenAI 兼容 HTTP 上游（默认）
                # type=opencode：驱动本地 opencode serve（见 opencode_upstream.py）
                "type": str(it.get("type", "http") or "http").lower(),
                "protocol": str(it.get("protocol", "") or "").lower(),
                "opencode_mode": str(it.get("opencode_mode", "") or "server").lower(),
                "opencode_provider": str(it.get("opencode_provider", "opencode") or "opencode"),
                "opencode_agent": str(it.get("opencode_agent", "") or ""),
                "opencode_tools": it.get("opencode_tools"),
                "opencode_extra": it.get("opencode_extra") or {},
                "proxy": str(it.get("proxy", "") or ""),
                "console_url": str(it.get("console_url", "") or ""),
                "note": str(it.get("note", "")),
            })
        # opencode 上游的兜底：老配置没有这两个字段时也要拿到合理默认
        for p in items:
            if p["type"] == "opencode":
                if not p.get("opencode_agent"):
                    p["opencode_agent"] = "plan"     # build 会自己循环到超时
                if not p.get("timeout"):        # 只在没显式配过时兜底
                    p["timeout"] = 150

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
               cost REAL, ok INTEGER, ms INTEGER, err TEXT, token_id TEXT)"""
    )
    # 老库补列
    cols = [r[1] for r in conn.execute("PRAGMA table_info(usage)").fetchall()]
    if "token_id" not in cols:
        try:
            conn.execute("ALTER TABLE usage ADD COLUMN token_id TEXT DEFAULT ''")
        except Exception:
            pass
    conn.commit()
    conn.close()


def record(provider: str, model: str, tin: int, tout: int,
           cost: float, ok: bool, ms: int, err: str = "",
           token_id: str = "") -> None:
    if not token_id:
        token_id = getattr(_CURRENT, "token_id", "")
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        conn.execute(
            "INSERT INTO usage(ts, provider, model, tokens_in, tokens_out,"
            " cost, ok, ms, err, token_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (time.time(), provider, model, int(tin), int(tout),
             float(cost), 1 if ok else 0, int(ms), (err or "")[:300], token_id),
        )
        _TOKEN_CACHE["ts"] = 0.0
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
def skip_report() -> list:
    """列出每个上游为什么没被用上，用于 502 的错误信息。"""
    now = time.time()
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
        if cd > now:
            out.append(f"{p['name']}：冷却中，还剩 {int(cd - now)} 秒")
            continue
        if is_exhausted(p, used):
            out.append(f"{p['name']}：免费额度已用尽")
            continue
        out.append(f"{p['name']}：可用")
    return out


def candidates(strategy: str, want_model: str = "") -> list[dict]:
    now = time.time()
    used = used_tokens()
    with STATE_LOCK:
        live = [
            p for p in PROVIDERS
            if p["enabled"]
            and (p["api_key"] or p["type"] == "opencode" or p["no_auth"])
            and STATE.get(p["name"], {}).get("cooldown_until", 0) <= now
            and not is_exhausted(p, used)
        ]
    if strategy == "cheapest":
        live.sort(key=lambda p: (p["price_in"] + p["price_out"], p["priority"]))
    else:
        live.sort(key=lambda p: p["priority"])
    if want_model:
        # 能提供这个模型的上游排到前面（稳定排序，组内仍按策略）
        live.sort(key=lambda p: 0 if (want_model == p["model"] or want_model in (p.get("models") or [])) else 1)
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


def pick_model(p: dict, payload: dict) -> str:
    """客户端点名了该上游的某个模型就用它，否则用默认模型。"""
    want = str(payload.get("model") or "").strip()
    if want and want in (p.get("models") or []):
        return want
    return p["model"] or (p.get("models") or [""])[0]


def build_body(p: dict, payload: dict, stream: bool) -> bytes:
    body = dict(payload)
    body["model"] = pick_model(p, payload)
    if os.getenv("DSGW_DEBUG"):
        print(f"[dbg] {p['name']} want={payload.get('model')!r} "
              f"models={p.get('models')!r} -> {body['model']!r}", flush=True)
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


def strip_ansi(text: str) -> str:
    """去掉终端颜色转义，日志里别带 [91m 这种东西。"""
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
        out.append(ch)
        i += 1
    return "".join(out)


def extract_err_ref(text: str) -> str:
    """从报错里抠出 opencode 的 ref（err_xxxxx），用来回查日志。"""
    i = text.find("err_")
    if i < 0:
        return ""
    j = i
    while j < len(text) and (text[j].isalnum() or text[j] in "_-"):
        j += 1
    return text[i:j]


def opencode_log_for_ref(ref: str, lines: int = 400) -> str:
    if not ref:
        return ""
    try:
        tail = SUPERVISOR.own_logs(lines)
    except Exception:
        return ""
    hits = [ln.strip() for ln in tail.splitlines() if ref in ln]
    if not hits:
        return ""
    return " ｜日志：" + " / ".join(h[-220:] for h in hits[-3:])


def opencode_log_error_hint(lines: int = 60) -> str:
    """从 opencode 自己的日志里摘出最后一条错误原因。"""
    try:
        tail = SUPERVISOR.own_logs(lines)
    except Exception:
        return ""
    hits = [ln for ln in tail.splitlines()
            if "ERROR" in ln or "error.error=" in ln]
    if not hits:
        return ""
    line = hits[-1]
    key = 'error.error="'
    i = line.find(key)
    if i >= 0:
        rest = line[i + len(key):]
        j = rest.find('"')
        return (rest[:j] if j > 0 else rest[:300]).strip()
    return line.strip()[-300:]


def _split_messages(payload: dict) -> tuple:
    """OpenAI messages → (system 文本, [{role, content}])"""
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


def _oa_wrap(model: str, text: str, tin, tout, cid: str = "") -> dict:
    """把别家协议的结果包成 OpenAI Chat Completions 形状。"""
    tin = int(tin or 0)
    tout = int(tout or 0)
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


def _post_json(p: dict, url: str, headers: dict, body: dict) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers=headers, method="POST")
    proxy = p.get("proxy") or RUNTIME.get("proxy") or ""
    opener = build_opener(proxy, p["timeout"])
    try:
        resp = opener.open(req, timeout=p["timeout"]) if opener else             urllib.request.urlopen(req, timeout=p["timeout"])
        try:
            raw = resp.read()
        finally:
            resp.close()
    except Exception as exc:                        # noqa: BLE001
        raise ProviderError(err_text(exc)) from None
    try:
        return json.loads(raw.decode("utf-8", "ignore"))
    except Exception:
        raise ProviderError("上游返回的不是 JSON：" + raw[:150].decode("utf-8", "ignore")) from None


def _call_responses(p: dict, payload: dict) -> dict:
    """OpenAI Responses API（/responses）"""
    model = pick_model(p, payload)
    system, msgs = _split_messages(payload)
    items = ([{"role": "system", "content": system}] if system else []) + msgs
    body: dict = {"model": model, "input": items}
    mt = payload.get("max_tokens") or payload.get("max_output_tokens")
    if mt:
        body["max_output_tokens"] = int(mt)
    body.update(p.get("extra") or {})
    headers = {"Content-Type": "application/json"}
    if p["api_key"]:
        headers["Authorization"] = "Bearer " + p["api_key"]
    data = _post_json(p, p["base_url"].rstrip("/") + "/responses", headers, body)
    text = ""
    for item in data.get("output") or []:
        for c in (item.get("content") or []) if isinstance(item, dict) else []:
            if isinstance(c, dict):
                text += str(c.get("text") or "")
    u = data.get("usage") or {}
    return _oa_wrap(model, text, u.get("input_tokens"), u.get("output_tokens"),
                    str(data.get("id") or ""))


def _call_anthropic(p: dict, payload: dict) -> dict:
    """Anthropic Messages API（/v1/messages）"""
    model = pick_model(p, payload)
    system, msgs = _split_messages(payload)
    body: dict = {"model": model,
                  "max_tokens": int(payload.get("max_tokens") or 4096),
                  "messages": msgs or [{"role": "user", "content": ""}]}
    if system:
        body["system"] = system
    body.update(p.get("extra") or {})
    base = p["base_url"].rstrip("/")
    url = base + ("/messages" if base.endswith("/v1") else "/v1/messages")
    headers = {"Content-Type": "application/json",
               "anthropic-version": "2023-06-01"}
    if p["api_key"]:
        headers["x-api-key"] = p["api_key"]
    data = _post_json(p, url, headers, body)
    text = "".join(str(c.get("text") or "") for c in (data.get("content") or [])
                   if isinstance(c, dict))
    u = data.get("usage") or {}
    return _oa_wrap(model, text, u.get("input_tokens"), u.get("output_tokens"),
                    str(data.get("id") or ""))


def _call_gemini(p: dict, payload: dict) -> dict:
    """Google Gemini generateContent"""
    model = pick_model(p, payload)
    system, msgs = _split_messages(payload)
    contents = [{"role": "user" if m["role"] == "user" else "model",
                 "parts": [{"text": m["content"]}]} for m in msgs]
    body: dict = {"contents": contents}
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    body.update(p.get("extra") or {})
    url = p["base_url"].rstrip("/") + "/models/" + model + ":generateContent"
    headers = {"Content-Type": "application/json"}
    if p["api_key"]:
        headers["x-goog-api-key"] = p["api_key"]
    data = _post_json(p, url, headers, body)
    text = ""
    for cand in data.get("candidates") or []:
        for part in ((cand.get("content") or {}).get("parts") or []):
            if isinstance(part, dict):
                text += str(part.get("text") or "")
    u = data.get("usageMetadata") or {}
    return _oa_wrap(model, text, u.get("promptTokenCount"),
                    u.get("candidatesTokenCount"), str(data.get("responseId") or ""))


ALT_PROTOCOLS = {
    "responses": _call_responses,
    "openai-responses": _call_responses,
    "anthropic": _call_anthropic,
    "claude": _call_anthropic,
    "gemini": _call_gemini,
    "google": _call_gemini,
}


def call_opencode_cli(p: dict, payload: dict) -> tuple[dict, str]:
    """走 `opencode run`（客户端本体），而不是 serve 的 HTTP API。"""
    bin_path, ws, env = SUPERVISOR.cli_context()
    if not bin_path:
        raise ProviderError("没找到 opencode 可执行文件：先在设置页点「一键安装」")

    prompt = render_prompt(payload)
    model = pick_model(p, payload)
    # opencode CLI 的 --model 要 provider/model 形式
    model_arg = model
    if model and "/" not in model:
        model_arg = (p.get("opencode_provider") or "opencode") + "/" + model
    args = [bin_path, "run"]
    if model_arg:
        args += ["--model", model_arg]
    agent = p.get("opencode_agent") or "plan"
    if agent:
        args += ["--agent", agent]
    args.append(prompt)

    print("[opencode-cli] " + " ".join(args[:-1]) + " <prompt>", flush=True)
    t0 = time.time()
    timed_out = False
    with tempfile.TemporaryFile() as fo, tempfile.TemporaryFile() as fe:
        try:
            rc = subprocess.call(args, stdout=fo, stderr=fe, env=env, cwd=ws,
                                 timeout=p["timeout"])
        except subprocess.TimeoutExpired:
            timed_out = True
            rc = -1
        except Exception as exc:                    # noqa: BLE001
            raise ProviderError(f"启动 opencode run 失败：{exc}") from None
        fo.seek(0)
        fe.seek(0)
        out = strip_ansi(fo.read().decode("utf-8", "ignore")).strip()
        err = strip_ansi(fe.read().decode("utf-8", "ignore")).strip()

    blob = (err + " " + out)
    if "usage limit" in blob.lower() or "Free usage limit" in blob:
        with STATE_LOCK:
            STATE.setdefault(p["name"], {})["cooldown_until"] = time.time() + 3600
        raise ProviderError(
            "OpenCode 免费额度已用尽（服务端原话：Free usage limit reached）。"
            "免费模型只对订阅 OpenCode Go 的账号开放 API/CLI 调用；"
            "已把该上游冷却 1 小时，请改用有 Key 的上游。")

    if timed_out:
        seen = (err or out)[-400:]
        raise ProviderError(
            f"opencode run 超时（{p['timeout']}s）。"
            + ("它已经输出的内容：" + seen if seen else "（没有任何输出，可能卡在鉴权或重试）")
            + " 可在 providers.json 里调大该上游的 timeout")

    sec = time.time() - t0
    print(f"[opencode-cli] rc={rc} {sec:.1f}s out={out[:120]!r} err={err[:160]!r}",
          flush=True)
    if rc != 0:
        detail = (err or out)[:300]
        ref = extract_err_ref(err + " " + out)
        raise ProviderError(f"opencode run 退出码 {rc}：{detail}"
                            + opencode_log_for_ref(ref))
    if not out:
        raise ProviderError("opencode run 没有输出：" + (err[:200] or "（空）"))
    tin = max(1, len(prompt) // 3)
    tout = max(1, len(out) // 3)
    return _oa_wrap(model, out, tin, tout), ""


def call_provider(p: dict, payload: dict) -> tuple[dict, str]:
    """统一调用入口，返回 (OpenAI 形状的 dict, 警告信息)。

    type=http     → 打上游的 OpenAI 兼容 /chat/completions
    type=opencode → 驱动本地 opencode serve（建会话→发消息→删会话）
    """
    if p["type"] == "opencode":
        if (p.get("opencode_mode") or "server").lower() == "cli":
            return call_opencode_cli(p, payload)
        SUPERVISOR.ensure()             # 没在跑就自动拉起（失败不影响下面报错）
        client = OpenCodeClient(
            base_url=p["base_url"], model_id=p["model"],
            provider_id=p["opencode_provider"], agent=p["opencode_agent"],
            tools=p["opencode_tools"], timeout=p["timeout"],
            extra=p["opencode_extra"])
        try:
            data = client.chat(render_prompt(payload))
        except OpenCodeError as exc:
            msg = str(exc)
            if "超时" in msg or "timed out" in msg.lower() or "timeout" in msg.lower():
                hint = opencode_log_error_hint()
                if hint:
                    if "usage limit" in hint.lower() or "free" in hint.lower():
                        # 免费池被限流：别让它再拖住每一次请求，直接冷却一小时
                        with STATE_LOCK:
                            STATE.setdefault(p["name"], {})["cooldown_until"] = time.time() + 3600
                        raise ProviderError(
                            "OpenCode 免费额度已用尽（服务端原话：%s）。"
                            "这个免费池只在 OpenCode 自己的界面里有效，无头调用会被限流；"
                            "已把该上游冷却 1 小时。" % hint[:200]) from None
                    msg = msg + "｜服务端报错：" + hint
            raise ProviderError(msg) from None
        upstream = str(data.pop("_upstream_model", "") or data.get("model") or "")
        return data, check_model(p, {"model": upstream})

    # 非 OpenAI Chat Completions 的上游协议：统一转成 OpenAI 形状再返回
    proto = (p.get("protocol") or "").lower()
    if proto in ALT_PROTOCOLS:
        data = ALT_PROTOCOLS[proto](p, payload)
        return data, check_model(p, data)

    req = urllib.request.Request(
        p["base_url"] + "/chat/completions",
        data=build_body(p, payload, bool(payload.get("stream"))),
        headers=headers_for(p, bool(payload.get("stream"))),
        method="POST")
    try:
        # 每个上游可以单独指定代理；没指定就用全局代理
        proxy = p.get("proxy") or RUNTIME.get("proxy") or ""
        opener = build_opener(proxy, p["timeout"])     # socks5 也支持
        if opener is not None:
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


def auth_v1(handler: BaseHTTPRequestHandler):
    """OpenAI 兼容接口的鉴权：主口令 / 用户令牌 / 全开放模式。"""
    auth = handler.headers.get("Authorization", "")
    presented = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    if not presented:
        presented = handler.headers.get("X-Api-Key", "").strip()

    master = RUNTIME["gateway_token"]
    tokens = [t for t in load_tokens() if t.get("enabled", True)]

    if master and presented and presented == master:
        return True, None, ""

    tok = find_token(presented) if presented else None
    if tok:
        quota = int(tok.get("quota_tokens") or 0)
        if quota > 0:
            used = token_usage().get(str(tok.get("id", "")), 0)
            if used >= quota:
                return False, tok, (f"令牌「{tok.get('name', '')}」额度已用尽"
                                    f"（{used}/{quota} tokens）")
        return True, tok, ""

    if not master and not tokens:
        return True, None, ""          # 全开放模式（自己局域网用）
    if not presented:
        return False, None, "缺少 API Key：请求头要带 Authorization: Bearer <key>"
    return False, None, "API Key 无效或已停用"


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
        # 浏览器类客户端（Open WebUI / LobeChat 等）需要 CORS
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, X-Api-Key")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        for k, v in (extra or {}).items():
            self.send_header(k, _safe_header(v))
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
        elif path == "/logs" or path == "/settings" or path.startswith("/settings/"):
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
        elif path == "/admin/api/tokens":
            if not authorized(self):
                self.send_json(401, {"error": "unauthorized"})
                return
            self.send_json(200, tokens_payload())
        elif path == "/v1/models":
            # 和 /v1/chat/completions 用同一套鉴权：主口令 或 任意启用的访问令牌
            ok_m, _tok, reason = auth_v1(self)
            if not ok_m:
                self.send_json(401, {"error": {"message": reason,
                                               "type": "unauthorized"}})
                return
            seen, data = set(), []
            for p in PROVIDERS:
                ids = [p["model"]] + list(p.get("models") or [])
                for mid in ids:
                    mid = str(mid or "").strip()
                    if not mid or mid in seen:
                        continue
                    seen.add(mid)
                    data.append({"id": mid, "object": "model", "owned_by": p["name"]})
            self.send_json(200, {"object": "list", "data": data})
        elif path == "/admin/api/status":
            if not authorized(self):
                self.send_json(401, {"error": "unauthorized"})
                return
            self.send_json(200, self.status_payload())
        elif path == "/admin/api/logs":
            self.handle_logs()
        else:
            self.send_json(404, {"error": "not found"})

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, X-Api-Key")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

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
        elif path == "/admin/api/tokens":
            self.handle_tokens()
        elif path == "/admin/api/proxy-test":
            self.handle_proxy_test()
        elif path == "/admin/api/discover":
            self.handle_discover()
        elif path == "/admin/api/verify-key":
            self.handle_verify_key()
        elif path == "/admin/api/save-provider":
            self.handle_save_provider()
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
        if not p["api_key"] and p["type"] != "opencode" and not p["no_auth"]:
            self.send_json(200, {"ok": False, "provider": name,
                                 "error": "没有密钥：在设置页这张卡片的「Key」里填一个"})
            return
        if p["type"] == "opencode" and not quick_port_check(p["base_url"]):
            self.send_json(200, {
                "ok": False, "provider": name, "ms": 0,
                "error": ("连不上 opencode server（%s）：它没在运行。"
                          "去设置页「OpenCode 运行时」点「启动」；没装过就先点「一键安装」。"
                          "不想用就把这张卡片停用。" % p["base_url"])})
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

    def handle_logs(self) -> None:
        """调用日志（SQLite）与网关日志（app.log）。"""
        if not authorized(self):
            self.send_json(401, {"error": "unauthorized"})
            return
        qs = parse_qs(urlparse(self.path).query)
        kind = (qs.get("kind") or ["usage"])[0]
        try:
            limit = max(1, min(int((qs.get("limit") or ["200"])[0]), 1000))
        except Exception:
            limit = 200

        if kind == "gateway":
            path = os.path.join(DATA_DIR, "app.log")
            self.send_json(200, {"ok": True, "kind": "gateway", "path": path,
                                 "text": tail_log(path, limit)})
            return

        names = {str(t.get("id", "")): str(t.get("name", "")) for t in load_tokens()}
        rows = []
        try:
            conn = sqlite3.connect(DB_PATH, timeout=5)
            cur = conn.execute(
                "SELECT ts, provider, model, tokens_in, tokens_out, cost, ok, ms,"
                " err, token_id FROM usage ORDER BY ts DESC LIMIT ?", (limit,))
            for r in cur.fetchall():
                rows.append({
                    "ts": float(r[0] or 0),
                    "time": time.strftime("%m-%d %H:%M:%S", time.localtime(r[0] or 0)),
                    "provider": r[1] or "",
                    "model": r[2] or "",
                    "tokens_in": int(r[3] or 0),
                    "tokens_out": int(r[4] or 0),
                    "cost": float(r[5] or 0),
                    "ok": bool(r[6]),
                    "ms": int(r[7] or 0),
                    "err": r[8] or "",
                    "token": names.get(str(r[9] or ""), "（主口令）" if not r[9] else "已删除"),
                })
            conn.close()
        except Exception as exc:                    # noqa: BLE001
            self.send_json(200, {"ok": False, "error": str(exc)})
            return
        self.send_json(200, {"ok": True, "kind": "usage", "rows": rows})

    def handle_save_provider(self) -> None:
        """保存单张上游卡片。"""
        if not authorized(self):
            self.send_json(401, {"error": "unauthorized"})
            return
        if not is_admin(self):
            self.send_json(403, {"ok": False, "error": "只有管理员能改配置"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            self.send_json(400, {"ok": False, "error": "bad request"})
            return
        raw = body.get("provider") or {}
        if not isinstance(raw, dict):
            self.send_json(400, {"ok": False, "error": "provider 必须是对象"})
            return
        try:
            for env, val in (body.get("secrets") or {}).items():
                save_secret(str(env), str(val or ""))
            snap = save_one_provider(raw)
        except Exception as exc:                    # noqa: BLE001
            self.send_json(400, {"ok": False, "error": str(exc)})
            return
        self.send_json(200, {"ok": True, **snap})

    def handle_verify_key(self) -> None:
        """拿一把 Key 来验：是主口令？是哪个令牌？有没有被停用/超额？"""
        if not authorized(self):
            self.send_json(401, {"error": "unauthorized"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            key = str(json.loads(self.rfile.read(length) or b"{}").get("key", "")).strip()
        except Exception:
            self.send_json(400, {"ok": False, "error": "bad request"})
            return

        if not key:
            self.send_json(200, {"ok": False, "error": "没有输入 Key"})
            return

        master = RUNTIME["gateway_token"]
        if master and key == master:
            self.send_json(200, {"ok": True, "kind": "主口令",
                                 "note": "这是你自己设的访问口令，可以直接用"})
            return

        tok = find_token(key)
        if tok:
            quota = int(tok.get("quota_tokens") or 0)
            used = token_usage().get(str(tok.get("id", "")), 0)
            if quota > 0 and used >= quota:
                self.send_json(200, {"ok": False, "kind": "令牌",
                                     "error": f"令牌「{tok.get('name','')}」额度已用尽（{used}/{quota}）"})
                return
            self.send_json(200, {"ok": True, "kind": "令牌",
                                 "name": tok.get("name", ""),
                                 "quota_tokens": quota, "used_tokens": used})
            return

        # 没匹配上：给出可能原因
        tokens = load_tokens()
        if not master and not [t for t in tokens if t.get("enabled", True)]:
            self.send_json(200, {"ok": True, "kind": "全开放模式",
                                 "note": "当前没有设口令、也没有启用的令牌，任何 Key 都能用"})
            return
        hint = "这把 Key 不认识。"
        if "*" in key:
            hint += "看起来像列表里的掩码（带 ****），点「复制」拿完整值。"
        else:
            disabled = [t.get("name") for t in tokens if not t.get("enabled", True)]
            if disabled:
                hint += f" 已停用的令牌：{', '.join(disabled)}。"
        self.send_json(200, {"ok": False, "error": hint})

    def handle_discover(self) -> None:
        """探测某个上游有哪些模型。"""
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
        self.send_json(200, discover_models(p))

    def handle_proxy_test(self) -> None:
        if not authorized(self):
            self.send_json(401, {"error": "unauthorized"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            body = {}
        proxy = str(body.get("proxy") or RUNTIME.get("proxy") or "")
        target = str(body.get("target") or "https://api.deepseek.com/")
        self.send_json(200, test_proxy(proxy, target))

    def handle_tokens(self) -> None:
        """访问令牌管理：create / toggle / delete / rename。"""
        if not authorized(self):
            self.send_json(401, {"error": "unauthorized"})
            return
        if not is_admin(self):
            self.send_json(403, {"ok": False, "error": "只有管理员能管理令牌"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            self.send_json(400, {"ok": False, "error": "bad request"})
            return

        action = str(body.get("action", ""))
        items = load_tokens()

        if action == "create":
            import uuid
            rec = {
                "id": uuid.uuid4().hex[:12],
                "name": str(body.get("name") or "未命名")[:60],
                "key": new_token_key(),
                "enabled": True,
                "quota_tokens": max(0, int(body.get("quota_tokens") or 0)),
                "created": int(time.time()),
            }
            items.append(rec)
            save_tokens(items)
            payload = tokens_payload()
            payload.update({"ok": True, "token": {"id": rec["id"], "name": rec["name"],
                                                  "key": rec["key"]}})
            self.send_json(200, payload)
            return

        tid = str(body.get("id", ""))
        if action in ("toggle", "delete", "rename") and not tid:
            self.send_json(400, {"ok": False, "error": "缺少 id"})
            return

        if action == "reveal":
            if not is_admin(self):
                self.send_json(403, {"ok": False, "error": "只有管理员能查看完整 Key"})
                return
            for t in items:
                if t.get("id") == tid:
                    self.send_json(200, {"ok": True, "id": tid, "key": t.get("key", "")})
                    return
            self.send_json(404, {"ok": False, "error": "令牌不存在"})
            return

        if action == "toggle":
            for t in items:
                if t.get("id") == tid:
                    t["enabled"] = bool(body.get("enabled", not t.get("enabled", True)))
            save_tokens(items)
        elif action == "delete":
            items = [t for t in items if t.get("id") != tid]
            save_tokens(items)
        elif action == "rename":
            for t in items:
                if t.get("id") == tid:
                    t["name"] = str(body.get("name") or t.get("name", ""))[:60]
            save_tokens(items)
        else:
            self.send_json(400, {"ok": False,
                                 "error": "action 只能是 create/toggle/delete/rename"})
            return

        payload = tokens_payload()
        payload["ok"] = True
        self.send_json(200, payload)

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
            _body = json.loads(self.rfile.read(length) or b"{}")
            action = str(_body.get("action", ""))
        except Exception:
            self.send_json(400, {"ok": False, "error": "bad request"})
            return

        if action == "login":
            ok_l, msg_l = SUPERVISOR.start_login("")
            self.send_json(200, {**SUPERVISOR.status(), "ok": ok_l, "message": msg_l})
            return
        if action == "login-keys":
            keys = str(_body.get("keys") or "")
            if keys == "enter":
                keys = chr(13)
            elif keys in ("pick-console", "pick-go"):
                # 先输入关键词过滤，再回车选中
                word = "console" if keys == "pick-console" else "go"
                ok1, m1 = SUPERVISOR.send_login(word)
                time.sleep(0.6)
                ok2, m2 = SUPERVISOR.send_login(chr(13))
                self.send_json(200, {**SUPERVISOR.status(), "ok": ok1 and ok2,
                                     "message": ("已选择：" + word) if (ok1 and ok2) else (m1 + " / " + m2)})
                return
            ok_k, msg_k = SUPERVISOR.send_login(keys)
            self.send_json(200, {**SUPERVISOR.status(), "ok": ok_k, "message": msg_k})
            return
        if action == "login-log":
            self.send_json(200, {**SUPERVISOR.status(), "ok": True,
                                 "message": "登录输出已刷新"})
            return
        if action == "ping":
            ops = [x for x in PROVIDERS if x["type"] == "opencode"]
            on = [x for x in ops if x["enabled"]] or ops
            op = min(on, key=lambda x: x["priority"]) if on else None
            port = int(opencode_opts().get("port") or 4096)
            base = "http://127.0.0.1:%d" % port     # 「试一句」测的是受管运行时
            model = (op or {}).get("model") or ""
            agent = (op or {}).get("opencode_agent") or "plan"
            prov = (op or {}).get("opencode_provider") or "opencode"
            t0 = time.time()
            print(f"[gateway] opencode 试一句：base={base} model={model} agent={agent}",
                  flush=True)
            mode = (op or {}).get("opencode_mode") or "server"
            bin_path = SUPERVISOR.resolve_bin()
            bin_tag = (os.path.basename(bin_path) + " " +
                       (SUPERVISOR._version(bin_path) or "?")) if bin_path else "未找到"
            if mode == "cli":
                try:
                    data, _w = call_opencode_cli(
                        {**(op or {}), "timeout": 45},
                        {"messages": [{"role": "user", "content": "回复两个字：OK"}],
                         "model": model})
                    text = str(data["choices"][0]["message"]["content"] or "")
                    print(f"[gateway] opencode cli 试一句成功：{text[:60]}", flush=True)
                    self.send_json(200, {**SUPERVISOR.status(), "ok": True,
                                         "message": f"试通了（cli / {bin_tag}）：{text[:80]}",
                                         "probe": {"mode": "cli", "bin": bin_tag,
                                                   "text": text[:400]}})
                except Exception as exc:            # noqa: BLE001
                    print(f"[gateway] opencode cli 试一句失败：{exc}", flush=True)
                    self.send_json(200, {**SUPERVISOR.status(), "ok": False,
                                         "message": f"试失败（cli / {bin_tag}）：{str(exc)[:300]}",
                                         "probe": {"mode": "cli", "bin": bin_tag,
                                                   "error": str(exc)[:400]}})
                return

            client = OpenCodeClient(base_url=base, model_id=model,
                                    provider_id=prov, agent=agent, timeout=25)
            try:
                probe = client.probe_message("回复两个字：OK", timeout=25)
            except Exception as exc:            # noqa: BLE001
                probe = {"steps": [{"step": "fatal", "ok": False, "error": str(exc)[:300]}]}
            sec = time.time() - t0
            steps = probe.get("steps") or []
            sent = next((x for x in steps if x.get("step") == "send_message"), {})
            if sent.get("ok"):
                text = ""
                try:
                    text = str(json.loads(sent.get("preview") or "{}"))[:200]
                except Exception:
                    text = str(sent.get("preview"))[:200]
                print(f"[gateway] opencode 试一句成功：{sec:.1f}s", flush=True)
                self.send_json(200, {**SUPERVISOR.status(), "ok": True,
                                     "message": f"试通了（{sec:.1f}s）",
                                     "probe": probe, "sample": text})
            else:
                err = str(sent.get("error") or (steps[-1].get("error") if steps else ""))[:300]
                print(f"[gateway] opencode 试一句失败：{sec:.1f}s {err}", flush=True)
                self.send_json(200, {**SUPERVISOR.status(), "ok": False,
                                     "message": f"试失败（{sec:.1f}s）：{err}",
                                     "probe": probe})
            return
        if action == "start":
            ok, msg = SUPERVISOR.start()
        elif action == "stop":
            ok, msg = SUPERVISOR.stop()
        elif action == "install":
            ok, msg = SUPERVISOR.install()
        elif action == "diagnose":
            port = int(opencode_opts().get("port") or 4096)
            base = "http://127.0.0.1:%d" % port
            client = OpenCodeClient(base_url=base, model_id="", timeout=12)
            payload = SUPERVISOR.status()
            payload.update({"ok": True, "message": "诊断完成",
                            "diag": client.diagnose()})
            self.send_json(200, payload)
            return
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
        if "proxy" in body:
            patch["proxy"] = str(body["proxy"] or "").strip()
        if "opencode_port" in body:
            try:
                patch["opencode_port"] = max(1, min(int(body["opencode_port"]), 65535))
            except Exception:
                self.send_json(400, {"ok": False, "error": "opencode 端口必须是整数"})
                return
        if "opencode_autostart" in body:
            patch["opencode_autostart"] = bool(body["opencode_autostart"])
        if "opencode_bin" in body:
            patch["opencode_bin"] = str(body["opencode_bin"] or "").strip()

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
            "port": PORT,
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
                "has_key": bool(p["api_key"]) or p["type"] == "opencode" or p["no_auth"],
                "type": p["type"],
                "protocol": p.get("protocol") or "",
                "key_env": p["key_env"],
                "key_source": source,
                "priority": p["priority"],
                "model": p["model"],
                "models": list(p.get("models") or []),
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
        ok_upd = update_provider_enabled(name, enabled)
        # 顺手清掉老的覆盖记录，避免两份状态打架
        try:
            ov = read_overrides()
            if name in ov:
                ov.pop(name, None)
                write_overrides(ov)
        except Exception:
            pass
        load_config()
        self.send_json(200, {"ok": ok_upd, "name": name, "enabled": enabled})

    # ---- 主接口 ----
    def handle_chat(self) -> None:
        ok, tok, reason = auth_v1(self)
        if not ok:
            code = 429 if "额度已用尽" in reason else 401
            self.send_json(code, {"error": {"message": reason, "type": "unauthorized"}})
            return
        _CURRENT.token_id = str((tok or {}).get("id", ""))
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
        want = str(payload.get("model") or "").strip()
        self.want_model = want

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
            self.send_header("X-Gateway-Warning", _safe_header(warn))
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
        for p in candidates(strategy, getattr(self, "want_model", "")):
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
                    # 头里只放 ASCII 短码；中文详情已经写进 usage 账本
                    extra["X-Gateway-Warning"] = "model-mismatch"
                self.send_json(200, data, extra)
                return
            except Exception as exc:               # noqa: BLE001
                msg = err_text(exc)
                if os.getenv("DSGW_DEBUG"):
                    print(f"[dbg] FAIL {p['name']}: {msg[:200]}", flush=True)
                mark_fail(p, ProviderError(msg))
                record(p["name"], p["model"], 0, 0, 0.0, False,
                       int((time.time() - t0) * 1000), msg)
                last = msg
        reasons = skip_report()
        if not tried:
            head = "没有可用上游。" + "；".join(reasons[:8]) + ("…" if len(reasons) > 8 else "")
        else:
            head = f"所有上游均失败：{last}（已尝试：{', '.join(tried)}）"
        self.send_json(502, {"error": {
            "message": head,
            "type": "gateway_error"}})

    def handle_stream(self, payload: dict, strategy: str) -> None:
        tried, last = [], None
        for p in candidates(strategy, getattr(self, "want_model", "")):
            tried.append(p["name"])
            t0 = time.time()

            # opencode 和非 OpenAI 协议没有流式：先阻塞拿到结果，再分块吐给客户端
            if p["type"] == "opencode" or (p.get("protocol") or "").lower() in ALT_PROTOCOLS:
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
  :root{--bg:#f5f7fa;--card:#fff;--line:#e5e9f0;--fg:#1f2430;--dim:#6b7280;
        --ok:#16a34a;--warn:#d97706;--err:#dc2626;--acc:#2563eb;--soft:#eff4ff}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--fg);
       font:14px/1.6 -apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}
  .layout{display:flex;min-height:100vh}
  .side{width:200px;flex:none;background:#fff;border-right:1px solid var(--line);
        padding:18px 12px;position:sticky;top:0;height:100vh}
  .brand{font-weight:700;font-size:15px;margin:0 6px}
  .brand small{display:block;font-weight:400;color:var(--dim);font-size:11px;margin-top:2px}
  .nav{margin-top:18px;display:flex;flex-direction:column;gap:2px}
  .nav a{display:block;padding:8px 10px;border-radius:8px;color:#374151;
         text-decoration:none;font-size:13px}
  .nav a:hover{background:#f3f6fb}
  .nav a.on{background:var(--soft);color:var(--acc);font-weight:600}
  .nav .sep{margin:10px 10px 6px;font-size:11px;color:#9aa4b2}
  .main{flex:1;min-width:0;padding:22px 26px 70px}
  h1{font-size:19px;margin:0 0 3px}
  h2{font-size:14px;margin:26px 0 10px;color:#111827}
  .sub{color:var(--dim);font-size:12px}
  .card{background:var(--card);border:1px solid var(--line);border-radius:12px;
        padding:16px;box-shadow:0 1px 2px rgba(16,24,40,.04)}
  .stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));
         gap:12px;margin:16px 0}
  .stat .k{color:var(--dim);font-size:12px}
  .stat .v{font-size:22px;font-weight:700;margin-top:2px}
  .row{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:12px 0}
  .field{margin:0 0 14px}
  .field>label{display:block;font-size:12px;color:#374151;font-weight:600;margin-bottom:5px}
  .hint{color:var(--dim);font-size:11px;margin-top:4px;line-height:1.5}
  .inline{display:flex;gap:8px;align-items:center}
  .inline>input,.inline>select{flex:1;min-width:0}
  input,select{background:#fff;color:var(--fg);border:1px solid #d5dbe5;border-radius:8px;
        padding:7px 10px;font-size:13px;font-family:inherit;width:100%}
  input[readonly]{background:#fafbfd}
  input:focus,select:focus{outline:none;border-color:var(--acc);
        box-shadow:0 0 0 3px rgba(37,99,235,.12)}
  input[type=checkbox]{width:auto;margin:0}
  button{background:#fff;color:#1f2430;border:1px solid #d5dbe5;border-radius:8px;
         padding:6px 12px;cursor:pointer;font-size:12px;white-space:nowrap}
  button:hover{border-color:var(--acc);color:var(--acc)}
  button.pri{background:var(--acc);border-color:var(--acc);color:#fff}
  button.pri:hover{background:#1d4ed8;color:#fff}
  button.danger{color:var(--err);border-color:#f3c7c7}
  button.danger:hover{border-color:var(--err)}
  table{width:100%;border-collapse:collapse;background:#fff;border:1px solid var(--line);
        border-radius:12px;overflow:hidden}
  th,td{padding:10px 12px;text-align:left;border-bottom:1px solid var(--line);font-size:13px}
  th{background:#fafbfd;color:var(--dim);font-weight:600;font-size:12px;white-space:nowrap}
  tr:last-child td{border-bottom:none}
  .tag{display:inline-block;padding:1px 8px;border-radius:999px;font-size:11px;white-space:nowrap}
  .g{background:#e8f7ee;color:var(--ok)}
  .y{background:#fef3e2;color:var(--warn)}
  .r{background:#fdecec;color:var(--err)}
  .d{background:#eef1f5;color:var(--dim)}
  .mono{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px}
  .nowrap{white-space:nowrap}
  .clip{max-width:250px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .msg{color:var(--acc);font-size:12px}
  .err{color:var(--err);font-size:11px;max-width:240px;overflow:hidden;
       text-overflow:ellipsis;white-space:nowrap;display:inline-block}
  .sect{display:none}
  .sect.on{display:block}
  .pgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(380px,1fr));gap:14px}
  .provcard{background:#fff;border:1px solid var(--line);border-radius:12px;padding:14px;
        box-shadow:0 1px 2px rgba(16,24,40,.04)}
  .phd{display:flex;justify-content:space-between;align-items:center;gap:8px;
       margin-bottom:12px;flex-wrap:wrap}
  .phd>input{flex:1 1 180px;min-width:0}
  .phd>span{flex:none;white-space:nowrap;display:flex;gap:6px;align-items:center}
  .phd>span a{font-size:12px}
  .phd .nm{font-weight:700;flex:1}
  .f2{display:grid;grid-template-columns:1fr 1fr;gap:10px}
  .chk{display:flex;gap:6px;align-items:center;font-size:12px;color:#374151;
       white-space:nowrap;flex:none}
  pre{background:#0f172a;color:#e2e8f0;border-radius:10px;padding:12px;font-size:11px;
      line-height:1.55;max-height:220px;overflow:auto;margin:10px 0 0}
  @media (max-width:760px){
    .side{display:none}
    .main{padding:16px 14px 60px}
    .f2{grid-template-columns:1fr}
  }
"""

_COMMON_JS = """
const BASE = (function(){
  const seg = location.pathname.split('/').filter(Boolean);
  let cut = seg.length;
  for(let i = seg.length - 1; i >= 0; i--){
    const s = seg[i];
    if(s === 'admin' || s === 'settings' || s === 'logs'){ cut = i; }
  }
  return cut > 0 ? ('/' + seg.slice(0, cut).join('/')) : '';
})();
const tok = () => localStorage.getItem('gw_token') || '';
const H = () => ({'Authorization':'Bearer '+tok(),'Content-Type':'application/json'});
function msg(t){ const e=document.getElementById('msg'); if(e) e.textContent = t; }
async function api(path, opts){
  const o = Object.assign({headers:H()}, opts||{});
  const r = await fetch(BASE + path, o);
  if(r.status===401){ msg('未登录或口令不对：在「设置」里填访问口令'); throw new Error('unauthorized'); }
  return r.json();
}
function esc(s){ return String(s==null?'':s).replace(/&/g,'&amp;').replace(/"/g,'&quot;')
  .replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
function parseProxy(s){
  s = String(s||'').trim();
  if(!s) return {scheme:'', addr:''};
  const i = s.indexOf('://');
  if(i > 0) return {scheme: s.slice(0, i).toLowerCase(), addr: s.slice(i + 3)};
  if(m) return {scheme: m[1].toLowerCase(), addr: m[2]};
  return {scheme:'http', addr:s};
}
function copyText(txt, label){
  const done = function(){ msg('已复制' + (label ? '：' + label : '')); };
  const fail = function(){ msg('复制失败，请手动选中输入框复制'); };
  const fallback = function(){
    try{
      const ta = document.createElement('textarea');
      ta.value = txt;
      ta.style.position = 'fixed'; ta.style.top = '-1000px'; ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.focus(); ta.select();
      const ok = document.execCommand('copy');
      document.body.removeChild(ta);
      ok ? done() : fail();
    }catch(e){ fail(); }
  };
  if(navigator.clipboard && window.isSecureContext){
    navigator.clipboard.writeText(txt).then(done).catch(fallback);
  } else { fallback(); }
}
function copyEp(id, label){
  const el = document.getElementById(id);
  const txt = el ? (el.value || '') : '';
  if(!txt){ msg('这一行还没有内容'); return; }
  copyText(txt, label);
}
document.querySelectorAll('[data-nav]').forEach(a => { a.href = BASE + a.dataset.nav; });

function currentSect(){
  let pth = location.pathname;
  while(pth.length > 1 && pth.endsWith('/')){ pth = pth.slice(0, -1); }
  if(pth.endsWith('/settings/providers')) return 'providers';
  if(pth.endsWith('/settings/tokens')) return 'tokens';
  if(pth.endsWith('/settings/opencode')) return 'opencode';
  if(pth.endsWith('/settings')) return 'global';
  if(pth.endsWith('/logs')) return 'logs';
  return 'admin';
}

function applySect(){
  const cur = currentSect();
  document.querySelectorAll('.nav a[data-sect]').forEach(function(a){
    a.classList.toggle('on', a.dataset.sect === cur);
  });
  document.querySelectorAll('.sect').forEach(function(el){
    el.classList.toggle('on', el.dataset.sect === cur);
  });
  const sr = document.getElementById('saveRow');
  if(sr) sr.style.display = (cur === 'logs') ? 'none' : 'flex';
  if(cur === 'logs'){ loadUsage(); loadGwLog(); }
  const titles = {global:'全局设置', providers:'上游', tokens:'访问令牌',
                  opencode:'OpenCode 运行时', logs:'日志'};
  const subs = {global:'口令、路由策略、冷却与代理',
                providers:'每个上游一张卡片，可挂多个模型',
                tokens:'给别人用的独立 Key，可单独停用/限额',
                opencode:'本地 opencode serve 的安装与启停',
                logs:'每次调用的记录，以及服务本身的输出'};
  const t = document.getElementById('secTitle');
  const sb = document.getElementById('secSub');
  if(t && titles[cur]) t.textContent = titles[cur];
  if(sb && subs[cur]) sb.textContent = subs[cur];
}
applySect();
"""

_SIDE = """
  <aside class="side">
    <div class="brand">DeepSeek 网关<small>多上游聚合 · OpenAI 兼容</small></div>
    <nav class="nav">
      <a data-nav="/admin" data-sect="admin">概览</a>
      <a data-nav="/logs" data-sect="logs">日志</a>
      <div class="sep">设置</div>
      <a data-nav="/settings" data-sect="global">全局</a>
      <a data-nav="/settings/providers" data-sect="providers">上游</a>
      <a data-nav="/settings/tokens" data-sect="tokens">访问令牌</a>
      <a data-nav="/settings/opencode" data-sect="opencode">OpenCode</a>
    </nav>
  </aside>
"""



_STATUS_BODY = """
<div class="layout">
NAV
  <main class="main">
    <h1>概览</h1>
    <div class="sub">一个接口，后面挂多个上游；某个上游失败会自动换下一家。</div>

    <div class="stats">
      <div class="card stat"><div class="k">24 小时请求</div><div class="v" id="reqs">-</div></div>
      <div class="card stat"><div class="k">24 小时花费（估算）</div><div class="v" id="cost">-</div></div>
      <div class="card stat"><div class="k">可用上游</div><div class="v" id="live">-</div></div>
      <div class="card stat"><div class="k">当前策略</div><div class="v" id="strat" style="font-size:16px">-</div></div>
    </div>

    <h2>接入信息</h2>
    <div class="card">
      <div class="field"><label>直连地址 —— 外部客户端用这个</label>
        <div class="inline"><input id="epDirect" readonly>
          <button onclick="copyEp('epDirect','直连地址')">复制</button></div>
        <div class="hint">Cherry Studio、OpenCode、脚本、手机 App 都用这一行。</div></div>

      <div class="field"><label>本机地址 —— 跑在飞牛上的程序用这个</label>
        <div class="inline"><input id="epLocal" readonly>
          <button onclick="copyEp('epLocal','本机地址')">复制</button></div>
        <div class="hint">容器或定时脚本走 127.0.0.1，不绕外网、不受防火墙影响。</div></div>

      <div class="field"><label>网关地址 —— 仅飞牛浏览器内可用</label>
        <div class="inline"><input id="epGateway" readonly>
          <button onclick="copyEp('epGateway','网关地址')">复制</button></div>
        <div class="hint">这条路要 NAS 登录态。外部客户端用它会被挡在门外（报 did not answer with JSON）。</div></div>

      <div class="field"><label>可用模型名</label>
        <div class="inline"><input id="epModels" readonly placeholder="加载中…">
          <button onclick="copyEp('epModels','模型名')">复制</button></div></div>

      <div class="field" style="margin-bottom:0"><label>调用示例</label>
        <div class="inline"><input id="epCurl" readonly>
          <button onclick="copyEp('epCurl','调用示例')">复制</button></div></div>
    </div>

    <h2>上游状态</h2>
    <div class="row">
      <button onclick="load()">刷新</button>
      <button onclick="reloadCfg()">重载配置</button>
      <span class="msg" id="msg"></span>
    </div>

    <div style="overflow-x:auto">
      <table style="min-width:1080px">
        <thead><tr>
          <th>上游</th><th>状态</th><th>模型</th><th>量化</th><th>免费额度</th>
          <th>价格 $/M</th><th>24h 请求</th><th>24h tokens</th><th>花费</th>
          <th>操作</th><th>最近错误</th>
        </tr></thead>
        <tbody id="tbody"></tbody>
      </table>
    </div>
  </main>
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
    if(!epInited){ epInited = true; loadEndpoint(d.port); }

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
        quota = '<span style="color:' + col + '">' + p.used_tokens.toLocaleString() + '</span>'
              + '<span class="sub"> / ' + p.free_quota_tokens.toLocaleString() + '</span>';
      } else { quota = '<span class="sub">免费不限</span>'; }
      const typeTag = p.type === 'opencode'
        ? (' <span class="tag d">opencode' +
           ((p.opencode_mode === 'cli') ? '·cli' : '') + '</span>')
        : (p.protocol ? ' <span class="tag d">' + esc(p.protocol) + '</span>' : '');
      const btn = p.enabled
        ? '<button onclick="tog(\\'' + p.name + '\\',false)">停用</button>'
        : '<button onclick="tog(\\'' + p.name + '\\',true)">启用</button>';
      return '<tr>'
        + '<td><b>' + p.name + '</b>' + typeTag
        + '<div class="sub">' + esc(p.note||'') + '</div></td>'
        + '<td><span class="tag ' + cls + '">' + st + '</span></td>'
        + '<td class="mono clip" title="' + esc(p.model) + '">' + esc(p.model) + '</td>'
        + '<td>' + p.quant + '</td>'
        + '<td class="nowrap">' + quota + '</td>'
        + '<td class="mono nowrap">' + p.price_in.toFixed(3) + ' / ' + p.price_out.toFixed(3) + '</td>'
        + '<td class="nowrap">' + p.requests_24h + '</td>'
        + '<td class="nowrap">' + (p.tokens_in_24h+p.tokens_out_24h).toLocaleString() + '</td>'
        + '<td class="mono nowrap">$' + p.cost_24h.toFixed(4) + '</td>'
        + '<td class="nowrap">' + btn + ' <button onclick="probe(\\'' + p.name + '\\')">自检</button></td>'
        + '<td><span class="err" title="' + esc(p.last_error) + '">'
        + (esc(p.last_error) || '-') + '</span></td>'
        + '</tr>';
    }).join('');
  }catch(e){ if(e.message !== 'unauthorized') msg('加载失败：' + e.message); }
}

async function loadEndpoint(port){
  try{
    const host = location.hostname;
    const pt = port || 18787;
    const direct = 'http://' + host + ':' + pt + '/v1';
    document.getElementById('epDirect').value = direct;
    document.getElementById('epLocal').value = 'http://127.0.0.1:' + pt + '/v1';
    document.getElementById('epGateway').value = BASE
      ? (location.origin + BASE + '/v1')
      : '（当前就是直连访问，无需网关）';
    const r = await fetch(BASE + '/v1/models', {headers:H()});
    const d = await r.json();
    const ids = (d.data || []).map(m => m.id);
    document.getElementById('epModels').value = ids.length ? ids.join('  ,  ') : '（还没配置上游）';
    const first = ids[0] || 'deepseek-v4.1-flash';
    document.getElementById('epCurl').value =
      'curl ' + direct + '/chat/completions -H "Content-Type: application/json" '
      + '-H "Authorization: Bearer <你的Key>" -d '
      + String.fromCharCode(39) + '{"model":"' + first
      + '","messages":[{"role":"user","content":"hi"}]}' + String.fromCharCode(39);
  }catch(e){}
}

async function tog(name, enabled){
  try{ await api('/admin/api/toggle', {method:'POST', body: JSON.stringify({name, enabled})}); }
  catch(e){}
  load();
}

async function probe(name){
  msg('正在自检 ' + name + ' …（最多等 60 秒）');
  const t0 = Date.now();
  try{
    const d = await Promise.race([
      api('/admin/api/probe', {method:'POST', body: JSON.stringify({name})}),
      new Promise(function(_, rej){
        setTimeout(function(){ rej(new Error('超时（60 秒没返回）')); }, 60000);
      })
    ]);
    const sec = ((Date.now() - t0) / 1000).toFixed(1);
    if(d.ok){ msg('[通] ' + name + '  ' + sec + 's  ' + (d.sample || '').slice(0, 70)); }
    else { msg('[失败] ' + name + '  ' + sec + 's  ' + (d.error || '')); }
  }catch(e){ msg('[失败] ' + name + '  ' + e.message); }
  load();
}

async function reloadCfg(){
  try{ const d = await api('/admin/api/reload', {method:'POST'});
       msg(d.ok ? '配置已重载' : ('失败：' + d.error)); }
  catch(e){}
  load();
}

let epInited = false;
load(); setInterval(load, 5000);
"""

_SETTINGS_BODY = """
<div class="layout">
NAV
  <main class="main">
    <h1 id="secTitle">设置</h1>
    <div class="sub" id="secSub">改完点「保存全部」立即生效，不用重启。</div>

    <div class="row" id="saveRow">
      <button class="pri" onclick="save()">保存全部</button>
      <button onclick="loadCfg()">重新载入</button>
      <span class="msg" id="msg"></span>
    </div>

    <!-- ===== 全局 ===== -->
    <div class="sect" data-sect="global">
      <h2>访问与路由</h2>
      <div class="card">
        <div class="f2">
          <div class="field"><label>访问口令</label>
            <div class="inline"><input id="setTok" type="password" placeholder="留空 = 免口令">
              <button onclick="clearTok()">清除</button></div>
            <div class="hint">给自己用的主口令。给别人用请到「访问令牌」建独立 Key。</div></div>
          <div class="field"><label>失败冷却（秒）</label>
            <input id="setCd" type="number" min="1" max="3600">
            <div class="hint">某个上游失败后，等多久再试它。</div></div>
        </div>
        <div class="field"><label>排序策略</label>
          <select id="setStrat">
            <option value="priority">稳定优先（按优先级从上到下）</option>
            <option value="cheapest">省钱优先（按价格从低到高）</option>
          </select></div>
        <div class="field" style="margin-bottom:0"><label>全局代理</label>
          <div class="inline">
            <select id="setProxyScheme" style="max-width:190px">
              <option value="">不用代理</option>
              <option value="http">HTTP</option>
              <option value="https">HTTPS</option>
              <option value="socks5">SOCKS5</option>
              <option value="socks5h">SOCKS5（代理解析域名）</option>
            </select>
            <input id="setProxyAddr" placeholder="主机:端口，如 192.168.1.10:7890">
            <button onclick="testProxy()">测试</button>
          </div>
          <div class="hint" id="proxyMsg">只有需要代理的上游才走它；国内直连的平台建议在卡片里单独留空。</div></div>
      </div>
    </div>

    <!-- ===== 上游 ===== -->
    <div class="sect" data-sect="providers">
      <h2>上游（提供模型的服务商）</h2>
      <div class="row" style="margin-top:0">
        <button onclick="addPreset('openai')">+ OpenAI 兼容接口</button>
        <button onclick="addPreset('openrouter')">+ OpenRouter</button>
        <button onclick="addPreset('ollama')">+ 本地 Ollama</button>
        <button onclick="addProv()">+ 空白上游</button>
      </div>
      <div class="grid pgrid" id="grid"></div>
    </div>

    <!-- ===== 访问令牌 ===== -->
    <div class="sect" data-sect="tokens">
      <h2>访问令牌（给别人用这个网关时）</h2>
      <div class="card">
        <div class="hint" id="tokHint" style="margin-bottom:10px">…</div>
        <div class="inline" style="margin-bottom:8px">
          <input id="verifyKey" placeholder="粘贴一把 Key 来验证（排查 401 用）">
          <button onclick="verifyKey()">验证 Key</button>
          <span class="msg" id="verifyMsg"></span>
        </div>
        <div class="inline" style="margin-bottom:12px">
          <input id="newTokName" placeholder="备注名，如 小明的笔记本">
          <input id="newTokQuota" type="number" placeholder="额度 tokens（0 = 不限）" style="max-width:210px">
          <button class="pri" onclick="createTok()">新建令牌</button>
          <button onclick="loadTokens()">刷新</button>
        </div>
        <div style="overflow-x:auto">
          <table style="min-width:640px">
            <thead><tr><th>备注</th><th>Key</th><th>已用 / 额度</th><th>状态</th><th>操作</th></tr></thead>
            <tbody id="tokBody"></tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- ===== OpenCode ===== -->
    <div class="sect" data-sect="opencode">
      <h2>OpenCode 运行时（给 type=opencode 的上游用）</h2>
      <div class="hint" style="margin:-2px 0 10px;line-height:1.8">
        <b>免费模型只能在他的客户端里用。</b>官方文档原文：
        <span class="mono">Free-tier chat models require OpenCode; use paid models or Go from other clients.</span>
        所以无论走 serve 还是 run，都会被限流并返回
        <span class="mono">Free usage limit reached</span>（issue #59387）。<br>
        <b>要用 API 得用付费模型</b>：Console 充值后拿 API Key，配上「<b>opencode-zen</b>」那张卡片
        （<span class="mono">https://opencode.ai/inference/openai/v1</span>）即可，价格便宜
        （DeepSeek V4.1 Flash $0.30/$1.20 每百万 tokens）。<br>
        另外 <b>v1（0.x/1.x）已不支持免费模型</b>，要用必须装 v2（<span class="mono">opencode2</span>）。
      </div>
      <div class="card">
        <div class="phd"><span class="nm">opencode serve</span>
          <span><span id="ocState" class="tag d">…</span></span></div>
        <div class="hint" id="ocEnv" style="margin-bottom:12px">检测中…</div>
        <div class="f2">
          <div class="field"><label>二进制路径</label>
            <input id="ocBin" placeholder="留空 = 自动查找"></div>
          <div class="field"><label>端口</label>
            <input id="ocPort" type="number" placeholder="4096"></div>
        </div>
        <div class="field"><label>工作目录</label>
          <input id="ocWs" placeholder="留空 = 应用数据目录下 opencode-workspace">
          <div class="hint">建议留空。它是个编码 Agent，空目录能避免误动你的文件。</div></div>
        <div class="field"><label>Console API Key（可选）</label>
          <input id="ocKey" type="password" placeholder="留空 = 未设置">
          <div class="hint">会以 <span class="mono">OPENCODE_API_KEY</span> 注入 opencode 进程
            （serve 和 run 都继承）。免费模型认不认它取决于官方策略；付费模型一定认。</div></div>
        <div class="row" style="margin-bottom:0">
          <label class="chk"><input id="ocAuto" type="checkbox"> 崩溃后自动重启</label>
          <span style="flex:1"></span>
          <button onclick="ocAction('install')">一键安装</button>
          <button onclick="ocAction('start')">启动</button>
          <button onclick="ocAction('stop')">停止</button>
          <button onclick="ocAction('login')">登录 OpenCode</button>
          <button onclick="ocAction('login-log')">登录输出</button>
          <button onclick="ocAction('ping')">试一句</button>
          <button onclick="ocAction('diagnose')">诊断</button>
          <button onclick="ocRefresh()">刷新</button>
        </div>
        <div class="sub" style="margin:10px 0 4px">启动输出</div>
        <pre id="ocLog">（暂无日志）</pre>
        <div class="sub" style="margin:10px 0 4px">登录输出（auth login 的链接/验证码在这）</div>
        <div class="inline" style="margin:0 0 6px">
          <input id="ocKeys" placeholder="看到菜单时：直接点「发送回车」选第一项；也可填数字">
          <button onclick="ocSendKeys()">发送</button>
          <button class="pri" onclick="ocSendKeys('enter')">发送回车</button>
          <button onclick="ocSendKeys('pick-console')">选 OpenCode Console</button>
          <button onclick="ocSendKeys('pick-go')">选 OpenCode Go</button>
        </div>
        <pre id="ocLogin">（还没有登录输出）</pre>
        <div class="sub" style="margin:10px 0 4px">opencode 自己的日志（provider 报错在这）</div>
        <pre id="ocOwnLog">（暂无）</pre>
      </div>
    </div>

    <!-- ===== 日志 ===== -->
    <div class="sect" data-sect="logs">
      <h2>调用日志</h2>
      <div class="row" style="margin-top:0">
        <button onclick="loadUsage()">刷新</button>
        <label class="chk"><input type="checkbox" id="autoLog" onchange="toggleAutoLog()">
          自动刷新（5 秒）</label>
        <select id="logLimit" onchange="loadUsage()" style="max-width:150px">
          <option value="100">最近 100 条</option>
          <option value="300" selected>最近 300 条</option>
          <option value="1000">最近 1000 条</option>
        </select>
        <span class="msg" id="logMsg"></span>
      </div>
      <div style="overflow-x:auto">
        <table style="min-width:1000px">
          <thead><tr>
            <th>时间</th><th>上游</th><th>模型</th><th>令牌</th><th>结果</th>
            <th>输入/输出</th><th>花费</th><th>耗时</th><th>错误</th>
          </tr></thead>
          <tbody id="logBody"></tbody>
        </table>
      </div>

      <h2>网关日志</h2>
      <div class="row" style="margin-top:0">
        <button onclick="loadGwLog()">刷新</button>
        <span class="sub" id="gwLogPath"></span>
      </div>
      <pre id="gwLog">（暂无）</pre>
    </div>
  </main>
</div>
"""



_SETTINGS_JS = """
let CFG = null;
let inited = false;

const CONSOLE_MAP = [
  ['openrouter.ai', 'https://openrouter.ai/keys'],
  ['api.deepseek.com', 'https://platform.deepseek.com/api_keys'],
  ['ark.cn-beijing.volces.com', 'https://console.volcengine.com/ark'],
  ['api.siliconflow.cn', 'https://cloud.siliconflow.cn/account/ak'],
  ['tokenhub.tencentmaas.com', 'https://console.cloud.tencent.com/tokenhub'],
  ['qianfan.baidubce.com', 'https://console.bce.baidu.com/qianfan/'],
  ['nvidia.com', 'https://build.nvidia.com/settings/api-keys'],
  ['modelarts-maas.com', 'https://console.huaweicloud.com/modelarts/'],
  ['11434', 'https://ollama.com/library'],
  ['4096', 'https://opencode.ai/docs/zen']
];

function guessConsole(p){
  if(p.console_url) return p.console_url;
  const u = String(p.base_url || '');
  for(let k = 0; k < CONSOLE_MAP.length; k++){
    if(u.indexOf(CONSOLE_MAP[k][0]) >= 0) return CONSOLE_MAP[k][1];
  }
  return '';
}

function openConsole(el){
  const u = el.dataset.url || '';
  if(!u){ msg('这个上游没有可识别的控制台地址，手动填一个再点打开'); return; }
  window.open(u, '_blank', 'noopener');
}

function provCard(p, i){
  const cUrl = guessConsole(p);
  const link = cUrl
    ? '<a href="' + esc(cUrl) + '" target="_blank" rel="noopener">去获取 Key ↗</a>' : '';
  const isOc = (p.type === 'opencode');
  const badge = '<span class="tag d">'
    + (isOc ? ('opencode' + ((p.opencode_mode === 'cli') ? '·cli' : '')) : (p.protocol || 'openai'))
    + '</span>';

  let h = '<div class="provcard" data-i="' + i + '">';

  h += '<div class="phd"><input data-f="name" value="' + esc(p.name)
     + '" style="font-weight:700;flex:1;min-width:0">'
     + '<span class="sub">' + badge + ' ' + link + '</span></div>';

  h += '<div class="field"><label>接口地址</label>'
     + '<input data-f="base_url" value="' + esc(p.base_url) + '">'
     + '<div class="hint">'
     + (isOc ? '本地 opencode server 地址，一般不用改'
             : 'OpenAI 兼容根地址，结尾不要带斜杠')
     + '</div></div>';

  h += '<div class="f2">'
     + '<div class="field"><label>默认模型</label>'
     + '<input data-f="model" value="' + esc(p.model) + '">'
     + '<div class="hint">客户端不点名时用这个</div></div>'
     + '<div class="field"><label>优先级</label>'
     + '<input type="number" data-f="priority" value="' + (p.priority || 100) + '">'
     + '<div class="hint">越小越先试</div></div>'
     + '</div>';

  h += '<div class="field"><label>更多模型（可选）</label>'
     + '<input data-f="models" value="' + esc((p.models || []).join(', ')) + '"'
     + ' placeholder="逗号分隔，客户端可以直接点名这些模型">'
     + '<div class="hint"><button type="button" onclick="discover(' + i + ')">'
     + '探测这个上游有哪些模型</button> <span id="dsc_' + i + '"></span></div></div>';

  h += '<div class="f2">'
     + '<div class="field"><label>类型</label>'
     + '<select data-f="type" onchange="switchType(this)">'
     + '<option value="http"' + (!isOc ? ' selected' : '') + '>http（HTTP 上游）</option>'
     + '<option value="opencode"' + (isOc ? ' selected' : '') + '>opencode（本地 server）</option>'
     + '</select></div>';

  if(isOc){
    h += '<div class="field"><label>调用方式</label>'
       + '<select data-f="opencode_mode">'
       + '<option value="server"' + ((p.opencode_mode || 'server') !== 'cli' ? ' selected' : '') + '>服务端 API（serve）</option>'
       + '<option value="cli"' + (p.opencode_mode === 'cli' ? ' selected' : '') + '>命令行（run）</option>'
       + '</select><div class="hint">cli 走客户端本体。注意：opencode 免费模型'
       + '官方限定「只在 OpenCode 内使用」，API/CLI 会被限流（issue #59387）</div></div>';
  } else {
    h += '<div class="field"><label>协议</label>'
       + '<select data-f="protocol">'
       + '<option value=""' + (!p.protocol ? ' selected' : '') + '>OpenAI Chat Completions</option>'
       + '<option value="responses"' + (p.protocol === 'responses' ? ' selected' : '') + '>OpenAI Responses</option>'
       + '<option value="anthropic"' + (p.protocol === 'anthropic' ? ' selected' : '') + '>Anthropic Messages</option>'
       + '<option value="gemini"' + (p.protocol === 'gemini' ? ' selected' : '') + '>Google Gemini</option>'
       + '</select><div class="hint">网关按它转换请求与响应</div></div>';
  }
  h += '</div>';

  if(isOc){
    h += '<div class="f2">'
       + '<div class="field"><label>providerID</label>'
       + '<input data-f="opencode_provider" value="' + esc(p.opencode_provider || 'opencode') + '"></div>'
       + '<div class="field"><label>agent</label>'
       + '<input data-f="opencode_agent" value="' + esc(p.opencode_agent || 'plan') + '">'
       + '<div class="hint">建议 plan（只读，不会自己循环）</div></div>'
       + '</div>';
  } else {
    h += '<div class="f2">'
       + '<div class="field"><label>密钥</label>'
       + '<input type="password" class="kval" data-env="' + esc(p.api_key_env) + '"'
       + ' placeholder="' + esc(p._key_source || '无') + '">'
       + '<div class="hint">变量名 <span class="mono">'
       + esc(p.api_key_env || '（未设置）') + '</span>　留空保存 = 不改动</div></div>'
       + '<div class="field"><label>量化</label>'
       + '<input data-f="quant" value="' + esc(p.quant) + '" placeholder="fp8 / fp4"></div>'
       + '</div>';
  }

  h += '<div class="field"><label>控制台地址</label><div class="inline">'
     + '<input data-f="console_url" value="' + esc(p.console_url) + '"'
     + ' placeholder="' + esc(cUrl || '留空则按接口自动识别') + '">'
     + '<button type="button" data-url="' + esc(cUrl) + '" onclick="openConsole(this)">打开</button>'
     + '</div></div>';

  h += '<div class="field"><label>免费额度（tokens）</label><div class="inline">'
     + '<input type="number" data-f="free_quota_tokens" value="' + (p.free_quota_tokens || 0) + '" style="max-width:190px">'
     + '<label class="chk"><input type="checkbox" data-f="free_only"'
     + (p.free_only ? ' checked' : '') + '> 免费上游</label>'
     + '</div><div class="hint">0 = 不限。填了额度，用尽后自动停用，不会悄悄转成付费。</div></div>';

  h += '<div class="f2">'
     + '<div class="field"><label>输入价 $/百万</label>'
     + '<input type="number" step="0.0001" data-f="price_in" value="' + (p.price_in || 0) + '"></div>'
     + '<div class="field"><label>输出价 $/百万</label>'
     + '<input type="number" step="0.0001" data-f="price_out" value="' + (p.price_out || 0) + '"></div>'
     + '</div>';

  h += '<div class="f2">'
     + '<div class="field"><label>模型名校验</label>'
     + '<input data-f="model_contains" value="' + esc(p.model_contains) + '" placeholder="留空 = 不校验">'
     + '<div class="hint">响应里的模型名必须包含它，防上游偷换</div></div>'
     + '<div class="field"><label>单独代理</label>'
     + '<input data-f="proxy" value="' + esc(p.proxy) + '" placeholder="留空 = 用全局">'
     + '<div class="hint">可填 socks5://host:port</div></div>'
     + '</div>';

  h += '<div class="field"><label>备注</label>'
     + '<input data-f="note" value="' + esc(p.note) + '"></div>';

  h += '<div class="field" style="margin-bottom:0;display:flex;gap:8px;align-items:center;flex-wrap:wrap">'
     + '<label class="chk"><input type="checkbox" data-f="enabled"'
     + (p.enabled ? ' checked' : '') + '> 启用</label>'
     + '<label class="chk"><input type="checkbox" data-f="no_auth"'
     + (p.no_auth ? ' checked' : '') + '> 免密钥</label>'
     + '<label class="chk"><input type="checkbox" data-f="strict_model"'
     + (p.strict_model ? ' checked' : '') + '> 严格校验</label>'
     + '<span style="flex:1"></span>'
     + '<button class="pri" onclick="saveOne(' + i + ')">保存这张</button>'
     + '<button data-name="' + esc(p.name) + '" onclick="probeOne(this.dataset.name)">自检</button>'
     + '<button class="danger" onclick="delProv(' + i + ')">删除</button>'
     + '</div>';

  h += '</div>';
  return h;
}

function switchType(el){
  const card = el.closest ? el.closest('.provcard') : null;
  if(!card) return;
  const idx = Number(card.dataset.i);
  CFG.providers[idx] = readCard(card);
  card.outerHTML = provCard(CFG.providers[idx], idx);
  msg('已切换类型，记得点「保存这张」');
}

function render(){
  document.getElementById('grid').innerHTML = CFG.providers.map(provCard).join('');
  document.getElementById('setStrat').value = CFG.strategy;
  document.getElementById('setCd').value = CFG.cooldown_seconds;
  const pr = parseProxy(CFG.proxy || '');
  document.getElementById('setProxyScheme').value = pr.scheme;
  document.getElementById('setProxyAddr').value = pr.addr;
  document.getElementById('setTok').placeholder =
    CFG.auth_enabled ? '已设置（留空不改）' : '留空 = 免口令';
  const oc = CFG.opencode || {};
  document.getElementById('ocPort').value = oc.port || 4096;
  document.getElementById('ocBin').value = oc.bin || '';
  const wsEl = document.getElementById('ocWs');
  if(oc.workspace && oc.workspace !== oc.workspace_default){
    wsEl.value = oc.workspace;
  } else {
    wsEl.value = '';
    wsEl.placeholder = '默认：' + (oc.workspace_default || '应用数据目录下 opencode-workspace');
  }
  document.getElementById('ocAuto').checked = oc.autostart !== false;
  const kEl = document.getElementById('ocKey');
  if(kEl) kEl.placeholder = oc.key_set ? '已设置（留空不改）' : '留空 = 未设置';
  ocRefresh();
}

async function loadCfg(){
  try{
    CFG = await api('/admin/api/config');
    if(!CFG.providers) CFG.providers = [];
    render();
  }catch(e){ if(e.message !== 'unauthorized') msg('载入失败：' + e.message); }
}

function readCard(el){
  // 以原始配置为底，只覆盖界面上改过的字段——避免把 extra / timeout / opencode_* 丢掉
  const idx = Number(el.dataset.i);
  const src = CFG.providers[idx] || {};
  const o = Object.assign({}, src);
  delete o._has_key;
  delete o._key_source;
  el.querySelectorAll('[data-f]').forEach(inp => {
    const f = inp.dataset.f;
    if(inp.type === 'checkbox') o[f] = inp.checked;
    else if(f === 'models'){
      o[f] = String(inp.value || '').split(',').map(function(x){ return x.trim(); })
               .filter(function(x){ return x; });
    }
    else if(inp.type === 'number') o[f] = inp.value === '' ? 0 : Number(inp.value);
    else o[f] = inp.value;
  });
  o._source = src._source || 'main';
  return o;
}

function cardSecrets(el){
  const out = {};
  el.querySelectorAll('.kval').forEach(inp => {
    const v = (inp.value || '').trim();
    if(v && inp.dataset.env) out[inp.dataset.env] = v;
  });
  return out;
}

async function saveOne(idx){
  const el = document.querySelector('.provcard[data-i="' + idx + '"]');
  if(!el){ msg('找不到这张卡片'); return; }
  const o = readCard(el);
  if(!String(o.name || '').trim()){ msg('名称不能为空'); return; }
  msg('保存中：' + o.name + ' …');
  try{
    const d = await api('/admin/api/save-provider', {method:'POST',
      body: JSON.stringify({provider: o, secrets: cardSecrets(el)})});
    if(d.ok){ CFG = d; render(); msg('已保存上游：' + o.name + '（其它卡片未改动）'); }
    else { msg('保存失败：' + (d.error || '')); }
  }catch(e){ msg('保存失败：' + e.message); }
}

function collect(){
  const providers = [...document.querySelectorAll('.provcard')].map(readCard);
  const secrets = {};
  document.querySelectorAll('.provcard').forEach(el => {
    Object.assign(secrets, cardSecrets(el));
  });
  const sc = document.getElementById('setProxyScheme').value;
  const ad = (document.getElementById('setProxyAddr').value || '').trim();
  const body = {
    strategy: document.getElementById('setStrat').value,
    cooldown_seconds: Number(document.getElementById('setCd').value || 60),
    proxy: (sc && ad) ? (sc + '://' + ad) : '',
    providers: providers,
    secrets: secrets,
    opencode_bin: document.getElementById('ocBin').value,
    opencode_port: Number(document.getElementById('ocPort').value || 4096),
    opencode_workspace: document.getElementById('ocWs').value,
    opencode_autostart: document.getElementById('ocAuto').checked,
    opencode_api_key: (document.getElementById('ocKey').value || '').trim()
  };
  const t = (document.getElementById('setTok').value || '').trim();
  if(t) body.gateway_token = t;
  return body;
}

async function save(){
  const cards = [...document.querySelectorAll('.provcard')];
  const names = cards.map(el => {
    const inp = el.querySelector('[data-f="name"]');
    return inp ? (inp.value || '').trim() : '';
  });
  if(names.some(n => !n)){
    msg('有上游的「名称」是空的，先填上再保存（第 ' + (names.indexOf('') + 1) + ' 张卡片）');
    return;
  }
  const dup = names.filter((n, i) => names.indexOf(n) !== i);
  if(dup.length){ msg('上游名称重复：' + dup.join(', ') + ' —— 名称必须唯一'); return; }

  msg('保存中…');
  try{
    const d = await api('/admin/api/config', {method:'POST', body: JSON.stringify(collect())});
    if(d.ok){
      CFG = d; render();
      document.getElementById('setTok').value = '';
      const od = d.on_disk || {};
      msg('已保存' + (od.proxy !== undefined
        ? ('（磁盘：代理=' + (od.proxy || '空') + '，策略=' + (od.strategy || '-') + '）') : ''));
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

const PRESETS = {
  openai: {name: 'openai-compatible', type: 'http', enabled: false, priority: 50,
    base_url: 'https://api.example.com/v1', model: '', model_contains: '',
    no_auth: false, free_only: false, free_quota_tokens: 0,
    price_in: 0, price_out: 0, proxy: '', console_url: '',
    api_key_env: 'OPENAI_API_KEY',
    note: '任意 OpenAI 兼容接口：填接口地址 / 模型 / Key 即可'},
  openrouter: {name: 'openrouter-custom', type: 'http', enabled: false, priority: 55,
    base_url: 'https://openrouter.ai/api/v1', model: '', model_contains: '',
    no_auth: false, free_only: false, free_quota_tokens: 0,
    price_in: 0, price_out: 0, proxy: '', console_url: 'https://openrouter.ai/keys',
    api_key_env: 'OPENROUTER_API_KEY',
    note: 'OpenRouter：一个 Key 打通 40+ 家 provider（需代理）'},
  ollama: {name: 'ollama-local', type: 'http', enabled: false, priority: 20,
    base_url: 'http://127.0.0.1:11434/v1', model: 'deepseek-r1:8b',
    model_contains: '', no_auth: true, free_only: true, free_quota_tokens: 0,
    price_in: 0, price_out: 0, proxy: '', console_url: 'https://ollama.com/library',
    api_key_env: '',
    note: '本地 Ollama / vLLM / LM Studio：不需要 Key'}
};

function addPreset(kind){
  const t = PRESETS[kind];
  if(!t) return;
  const p = JSON.parse(JSON.stringify(t));
  p._source = 'main';
  p._key_source = p.no_auth ? '免密钥' : '无';
  CFG.providers.push(p);
  render();
  msg('已添加模板：' + p.name + ' —— 改完点「保存全部」');
}

function delProv(i){
  CFG.providers.splice(i, 1);
  render();
  msg('已从列表移除，点「保存全部」才会真正写盘');
}

const MODEL_CACHE = {};

async function discover(idx){
  const p = CFG.providers[idx];
  if(!p) return;
  const el = document.getElementById('dsc_' + idx);
  if(!el) return;
  el.textContent = '探测中…';
  try{
    const d = await api('/admin/api/discover', {method:'POST',
      body: JSON.stringify({name: p.name})});
    if(!d.ok){ el.textContent = '失败：' + (d.error || ''); return; }
    const free = (d.models || []).filter(function(m){ return m.free; })
                                .map(function(m){ return m.id; });
    MODEL_CACHE[idx] = free;
    if(free.length){
      el.innerHTML = '共 ' + d.count + ' 个，免费 ' + free.length + ' 个 '
        + '<button type="button" onclick="fillFree(' + idx + ')">填入这些免费模型</button>';
    } else {
      el.textContent = '共 ' + d.count + ' 个模型，没识别出免费的';
    }
  }catch(e){ el.textContent = '失败：' + e.message; }
}

function fillFree(idx){
  const list = MODEL_CACHE[idx] || [];
  if(!list.length){ msg('没有探测到免费模型'); return; }
  const card = document.querySelector('.provcard[data-i="' + idx + '"]');
  const inp = card ? card.querySelector('[data-f="models"]') : null;
  if(!inp){ msg('找不到模型输入框'); return; }
  inp.value = list.join(', ');
  msg('已填入 ' + list.length + ' 个免费模型，记得点「保存全部」');
}

async function probeOne(name){
  msg('正在自检 ' + name + ' …');
  try{
    const d = await api('/admin/api/probe', {method:'POST', body: JSON.stringify({name})});
    msg(d.ok ? ('[通] ' + name + '  ' + d.ms + 'ms  ' + (d.sample || '').slice(0, 60))
             : ('[失败] ' + name + '  ' + (d.error || '')));
  }catch(e){ msg('自检失败：' + e.message); }
}

async function testProxy(){
  const el = document.getElementById('proxyMsg');
  const scheme = document.getElementById('setProxyScheme').value;
  const addr = (document.getElementById('setProxyAddr').value || '').trim();
  const proxy = (scheme && addr) ? (scheme + '://' + addr) : '';
  if(!proxy){ el.textContent = '先填代理地址'; return; }
  el.textContent = '测试中…';
  try{
    const d = await api('/admin/api/proxy-test', {method:'POST',
      body: JSON.stringify({proxy: proxy})});
    el.textContent = d.ok
      ? ('通：HTTP ' + d.status + ' · ' + d.ms + 'ms · ' + (d.note || '通道正常')
         + '  ← 别忘了点「保存全部」')
      : ('失败：' + (d.error || '') + '（' + (d.ms || 0) + 'ms）');
  }catch(e){ el.textContent = '测试失败：' + e.message; }
}

async function clearTok(){
  try{ await api('/admin/api/set-settings', {method:'POST',
        body: JSON.stringify({gateway_token:''})});
       localStorage.removeItem('gw_token'); msg('已清除访问口令'); }
  catch(e){ msg('失败：' + e.message); }
  loadCfg();
}

/* ---------- OpenCode 运行时 ---------- */
function ocPaint(d){
  const el = document.getElementById('ocState');
  el.textContent = d.running ? ('运行中 pid ' + d.pid)
                             : (d.installed ? '未运行' : '未安装');
  el.className = 'tag ' + (d.running ? 'g' : (d.installed ? 'y' : 'd'));
  document.getElementById('ocLog').textContent =
    (d.last_error ? ('[错误] ' + d.last_error + ' | ') : '') + (d.log || '（暂无日志）');
  const ol = document.getElementById('ocOwnLog');
  if(ol) ol.textContent = d.oc_logs || '（暂无）';
  const lg = document.getElementById('ocLogin');
  if(lg){
    const urls = d.login_urls || [];
    lg.textContent = (urls.length
        ? ('授权链接（复制到浏览器打开）：' + String.fromCharCode(10)
           + urls.join(String.fromCharCode(10)) + String.fromCharCode(10)
           + '----------' + String.fromCharCode(10))
        : '') + (d.login_log || '（还没有登录输出）');
  }
  document.getElementById('ocEnv').textContent =
    'Node ' + (d.node_version || (d.node ? '?' : '未找到'))
    + (d.node ? ('（' + d.node + '）') : '')
    + ' · npm ' + (d.npm_version || '—')
    + ' · opencode ' + (d.bin ? ((d.bin_version || '?') + '　' + d.bin) : '未安装');
}

async function ocSendKeys(fixed){
  const el = document.getElementById('ocKeys');
  const keys = (fixed !== undefined) ? fixed : (el ? el.value : '');
  try{
    const d = await api('/admin/api/opencode', {method:'POST',
      body: JSON.stringify({action: 'login-keys', keys: keys})});
    msg((d.ok ? '[成功] ' : '[失败] ') + (d.message || ''));
    ocPaint(d);
  }catch(e){ msg('发送失败：' + e.message); }
}

async function ocRefresh(){
  try{
    const d = await api('/admin/api/opencode');
    ocPaint(d);
    if(d.bin && !document.getElementById('ocBin').value){
      document.getElementById('ocBin').placeholder = '自动：' + d.bin;
    }
  }catch(e){ if(e.message !== 'unauthorized') msg('读取运行时状态失败：' + e.message); }
}

async function ocAction(action){
  msg('执行中：' + action + ' …');
  try{
    const d = await api('/admin/api/opencode', {method:'POST', body: JSON.stringify({action})});
    msg((d.ok ? '[成功] ' : '[失败] ') + (d.message || ''));
    ocPaint(d);
    if(action === 'diagnose' && d.diag){
      document.getElementById('ocOwnLog').textContent = JSON.stringify(d.diag, null, 2);
    }
    if(action === 'ping' && d.probe){
      document.getElementById('ocOwnLog').textContent = JSON.stringify(d.probe, null, 2);
    }
  }catch(e){ msg('操作失败：' + e.message); }
}

/* ---------- 日志 ---------- */
async function loadUsage(){
  const sel = document.getElementById('logLimit');
  const n = sel ? sel.value : 300;
  try{
    const d = await api('/admin/api/logs?kind=usage&limit=' + n);
    if(!d.ok){ msg('读取失败：' + (d.error || '')); return; }
    const rows = (d.rows || []).map(function(r){
      return '<tr>'
        + '<td class="nowrap mono">' + r.time + '</td>'
        + '<td>' + esc(r.provider) + '</td>'
        + '<td class="mono clip" title="' + esc(r.model) + '">' + esc(r.model) + '</td>'
        + '<td>' + esc(r.token) + '</td>'
        + '<td><span class="tag ' + (r.ok ? 'g' : 'r') + '">'
        + (r.ok ? '成功' : '失败') + '</span></td>'
        + '<td class="nowrap">' + r.tokens_in + ' / ' + r.tokens_out + '</td>'
        + '<td class="mono nowrap">$' + r.cost.toFixed(4) + '</td>'
        + '<td class="nowrap">' + r.ms + 'ms</td>'
        + '<td><span class="err" title="' + esc(r.err) + '">'
        + (esc(r.err) || '-') + '</span></td>'
        + '</tr>';
    }).join('');
    document.getElementById('logBody').innerHTML =
      rows || '<tr><td colspan="9" class="sub">还没有调用记录</td></tr>';
    const lm = document.getElementById('logMsg');
    if(lm) lm.textContent = '共 ' + (d.rows || []).length + ' 条';
  }catch(e){ msg('读取失败：' + e.message); }
}

async function loadGwLog(){
  try{
    const d = await api('/admin/api/logs?kind=gateway&limit=300');
    document.getElementById('gwLogPath').textContent = d.path || '';
    document.getElementById('gwLog').textContent = d.text || '（暂无）';
  }catch(e){ msg('读取失败：' + e.message); }
}

let autoLogTimer = null;
function toggleAutoLog(){
  const on = document.getElementById('autoLog').checked;
  if(autoLogTimer){ clearInterval(autoLogTimer); autoLogTimer = null; }
  if(on){
    autoLogTimer = setInterval(function(){ loadUsage(); loadGwLog(); }, 5000);
  }
}

/* ---------- 访问令牌 ---------- */
async function loadTokens(){
  try{
    const d = await api('/admin/api/tokens');
    document.getElementById('tokHint').textContent = d.open_mode
      ? '⚠ 当前是全开放模式：任何人能访问就能直接调 /v1。建议先设访问口令，或至少建一个令牌。'
      : (d.master ? '主口令可用；下面是给其他人用的独立令牌（每人一把，可单独停用/限额）'
                  : '当前只接受下面这些令牌');
    const rows = (d.tokens || []).map(function(t){
      const q = t.quota_tokens > 0 ? t.quota_tokens.toLocaleString() : '不限';
      return '<tr>'
        + '<td>' + esc(t.name) + '</td>'
        + '<td class="mono"><span class="kv" data-id="' + esc(t.id) + '">'
        + esc(t.key_masked) + '</span> '
        + '<button data-id="' + esc(t.id) + '" onclick="tokReveal(this)">显示</button> '
        + '<button data-id="' + esc(t.id) + '" onclick="tokCopy(this)">复制</button></td>'
        + '<td class="mono">' + t.used_tokens.toLocaleString() + ' / ' + q + '</td>'
        + '<td><span class="tag ' + (t.enabled ? 'g' : 'd') + '">'
        + (t.enabled ? '启用' : '停用') + '</span></td>'
        + '<td><button data-id="' + esc(t.id) + '" data-on="' + (t.enabled ? '0' : '1')
        + '" onclick="tokToggle(this)">' + (t.enabled ? '停用' : '启用') + '</button> '
        + '<button class="danger" data-id="' + esc(t.id) + '" onclick="tokDelete(this)">删除</button></td>'
        + '</tr>';
    }).join('');
    document.getElementById('tokBody').innerHTML =
      rows || '<tr><td colspan="5" class="sub">还没有令牌</td></tr>';
  }catch(e){ if(e.message !== 'unauthorized') msg('读取令牌失败：' + e.message); }
}

async function verifyKey(){
  const el = document.getElementById('verifyMsg');
  const key = (document.getElementById('verifyKey').value || '').trim();
  if(!key){ el.textContent = '先粘贴一把 Key'; return; }
  try{
    const d = await api('/admin/api/verify-key', {method:'POST',
      body: JSON.stringify({key: key})});
    if(d.ok){
      el.textContent = '可用：' + d.kind
        + (d.name ? ('「' + d.name + '」') : '')
        + (d.quota_tokens ? ('（已用 ' + d.used_tokens + '/' + d.quota_tokens + '）') : '')
        + (d.note ? ('　' + d.note) : '');
    } else {
      el.textContent = '不可用：' + (d.error || '');
    }
  }catch(e){ el.textContent = '验证失败：' + e.message; }
}

async function tokCopy(el){
  const id = el.dataset.id;
  try{
    const d = await api('/admin/api/tokens', {method:'POST',
      body: JSON.stringify({action:'reveal', id: id})});
    if(d.ok){ copyText(d.key, '完整 Key'); }
    else { msg('无法复制：' + (d.error || '')); }
  }catch(e){ msg('复制失败：' + e.message); }
}

async function createTok(){
  const name = (document.getElementById('newTokName').value || '').trim() || '未命名';
  const quota = Number(document.getElementById('newTokQuota').value || 0);
  try{
    const d = await api('/admin/api/tokens', {method:'POST',
      body: JSON.stringify({action: 'create', name: name, quota_tokens: quota})});
    if(d.ok && d.token){
      msg('已创建（只显示这一次，请复制保存）：' + d.token.key);
      document.getElementById('newTokName').value = '';
      document.getElementById('newTokQuota').value = '';
    } else { msg('创建失败：' + (d.error || '')); }
  }catch(e){ msg('创建失败：' + e.message); }
  loadTokens();
}

async function tokToggle(el){
  try{
    await api('/admin/api/tokens', {method:'POST',
      body: JSON.stringify({action:'toggle', id: el.dataset.id, enabled: el.dataset.on === '1'})});
  }catch(e){ msg('操作失败：' + e.message); }
  loadTokens();
}

async function tokDelete(el){
  try{
    await api('/admin/api/tokens', {method:'POST',
      body: JSON.stringify({action:'delete', id: el.dataset.id})});
    msg('已删除令牌');
  }catch(e){ msg('删除失败：' + e.message); }
  loadTokens();
}

async function tokReveal(el){
  const id = el.dataset.id;
  const span = document.querySelector('.kv[data-id="' + id + '"]');
  if(!span) return;
  if(span.dataset.shown === '1'){
    span.textContent = span.dataset.masked || '';
    span.dataset.shown = '0';
    el.textContent = '显示';
    return;
  }
  try{
    const d = await api('/admin/api/tokens', {method:'POST',
      body: JSON.stringify({action:'reveal', id: id})});
    if(d.ok){
      span.dataset.masked = span.textContent;
      span.textContent = d.key;
      span.dataset.shown = '1';
      el.textContent = '隐藏';
    } else { msg('无法显示：' + (d.error || '')); }
  }catch(e){ msg('失败：' + e.message); }
}

loadCfg();
loadTokens();
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


def _nav(active: str) -> str:
    """把侧栏里的占位符换掉：当前页高亮，另一个留空。"""
    side = _SIDE
    side = side.replace("NAV_ADMIN", "on" if active == "admin" else "")
    side = side.replace("NAV_SET", "on" if active == "settings" else "")
    return side


STATUS_HTML = _page("DeepSeek 网关 · 概览",
                    _STATUS_BODY.replace("NAV", _nav("admin")),
                    _STATUS_JS)
SETTINGS_HTML = _page("DeepSeek 网关 · 设置",
                      _SETTINGS_BODY.replace("NAV", _nav("settings")),
                      _SETTINGS_JS)


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
