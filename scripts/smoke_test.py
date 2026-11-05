#!/usr/bin/env python3
"""网关端到端冒烟测试（纯标准库，CI 和本地都能跑）

起一个假上游 + 一个真网关，验证：
  1. /healthz
  2. 非流式对话（含 usage 记账、X-Gateway-Provider 头）
  3. 流式对话（SSE，含 [DONE]）
  4. 故障切换（第一个上游连不上 → 自动切到第二个）
  5. 严格 model 校验失败 → 自动跳过该上游
  6. /admin/api/status 与 toggle 启停
  7. 重载配置
  8. providers.d/ 合并 + 免费额度硬卡（free_only/free_quota_tokens）
  9. type=opencode 上游（假 opencode serve：会话创建/发消息/删除、流式缓冲、probe）
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
GATEWAY = os.path.join(ROOT, "app", "gateway.py")

GW_PORT = 18888
UP_PORT = 18999
OC_PORT = 18998            # 假 opencode server
DEAD_PORT = 18997          # 没有任何服务监听，用来测故障切换

MODEL = "deepseek/deepseek-v4.1-flash-1017"
FAILS: list[str] = []


def check(cond: bool, msg: str, detail: str = "") -> None:
    if cond:
        print(f"  OK   {msg}")
    else:
        FAILS.append(msg)
        print(f"  FAIL {msg} {detail}")


# ─────────────────────── 假上游 ───────────────────────
class Upstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        if body.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for piece in ("你好", "，这是", "假上游"):
                ev = {"id": "chatcmpl-x", "object": "chat.completion.chunk",
                      "model": MODEL,
                      "choices": [{"index": 0, "delta": {"content": piece}}]}
                self._chunk(f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode())
            tail = {"id": "chatcmpl-x", "object": "chat.completion.chunk", "model": MODEL,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 12, "completion_tokens": 7}}
            self._chunk(f"data: {json.dumps(tail, ensure_ascii=False)}\n\n".encode())
            self._chunk(b"data: [DONE]\n\n")
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        else:
            out = {"id": "chatcmpl-x", "object": "chat.completion", "model": MODEL,
                   "choices": [{"index": 0, "message": {"role": "assistant",
                                                        "content": "pong"},
                                "finish_reason": "stop"}],
                   "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
            data = json.dumps(out, ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    def _chunk(self, data: bytes):
        self.wfile.write(b"%x\r\n" % len(data) + data + b"\r\n")
        self.wfile.flush()


class FakeOpenCode(BaseHTTPRequestHandler):
    """假 opencode serve：只实现网关用到的那三个接口。"""
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _json(self, code: int, obj) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        if self.path == "/session":
            self._json(200, {"id": "ses_test123"})
        elif self.path.endswith("/message"):
            parts = body.get("parts") or [{}]
            text = str(parts[0].get("text") or "")
            model = (body.get("model") or {}).get("modelID", "unknown")
            self._json(200, {
                "info": {"modelID": model, "tokens": {"input": 12, "output": 7}},
                "parts": [{"type": "text", "text": "OC[" + text[:24] + "]"}],
            })
        else:
            self._json(404, {"error": "not found"})

    def do_DELETE(self):
        self._json(200, {"ok": True})


def free_port(port: int) -> bool:
    s = socket.socket()
    s.settimeout(0.5)
    try:
        return s.connect_ex(("127.0.0.1", port)) != 0
    finally:
        s.close()


def http(method: str, url: str, body=None, headers=None, raw=False, timeout=20):
    data = json.dumps(body).encode() if body is not None else None
    h = {"Content-Type": "application/json"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = resp.read()
            return resp.status, dict(resp.headers), (payload if raw else payload.decode("utf-8", "ignore"))
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), exc.read().decode("utf-8", "ignore")


def main() -> int:
    if not free_port(GW_PORT) or not free_port(UP_PORT) or not free_port(OC_PORT):
        print(f"端口 {GW_PORT}/{UP_PORT}/{OC_PORT} 被占用，换端口后重试")
        return 2

    # 放在仓库内固定路径，避免系统临时目录在受限沙箱下不可写
    tmp = os.path.join(ROOT, ".testtmp", "run")
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp, exist_ok=True)
    cfg_path = os.path.join(tmp, "providers.json")
    data_dir = os.path.join(tmp, "data")
    os.makedirs(data_dir, exist_ok=True)

    config = {
        "settings": {"strategy": "priority", "request_timeout": 15},
        "providers": [
            {"name": "dead-first", "enabled": True, "priority": 1,
             "base_url": f"http://127.0.0.1:{DEAD_PORT}", "model": MODEL,
             "api_key": "k", "price_in": 1, "price_out": 1, "quant": "fp8",
             "note": "故意连不上"},
            {"name": "strict-mismatch", "enabled": True, "priority": 2,
             "base_url": f"http://127.0.0.1:{UP_PORT}", "model": MODEL,
             "api_key": "k", "model_contains": "不存在的模型名",
             "strict_model": True, "price_in": 0.5, "price_out": 0.5,
             "quant": "fp4", "note": "严格校验必然失败"},
            {"name": "good", "enabled": True, "priority": 3,
             "base_url": f"http://127.0.0.1:{UP_PORT}", "model": MODEL,
             "api_key": "k", "model_contains": "v4.1-flash",
             "price_in": 0.022, "price_out": 0.40, "quant": "fp8",
             "note": "真正可用的上游"},
        ],
    }
    with open(cfg_path, "w", encoding="utf-8") as fh:
        json.dump(config, fh, ensure_ascii=False, indent=2)

    upstream = ThreadingHTTPServer(("127.0.0.1", UP_PORT), Upstream)
    upstream.daemon_threads = True
    threading.Thread(target=upstream.serve_forever, daemon=True).start()

    oc = ThreadingHTTPServer(("127.0.0.1", OC_PORT), FakeOpenCode)
    oc.daemon_threads = True
    threading.Thread(target=oc.serve_forever, daemon=True).start()

    env = dict(os.environ,
               CONFIG_PATH=cfg_path, DATA_DIR=data_dir,
               PORT=str(GW_PORT), HOST="127.0.0.1", COOLDOWN_SECONDS="30")
    env.pop("GATEWAY_TOKEN", None)

    log_path = os.path.join(tmp, "gateway.log")
    log_fh = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, GATEWAY], env=env,
                            stdout=log_fh, stderr=subprocess.STDOUT, text=True)
    base = f"http://127.0.0.1:{GW_PORT}"

    try:
        # 等端口就绪
        for _ in range(50):
            if not free_port(GW_PORT):
                break
            time.sleep(0.1)
        else:
            log_fh.flush()
            try:
                out = open(log_path, encoding="utf-8", errors="replace").read()
            except Exception:
                out = ""
            print("网关没起来：\n", out)
            return 1

        print("== 1. /healthz ==")
        code, _, txt = http("GET", base + "/healthz")
        check(code == 200 and json.loads(txt)["providers"] == 3, "/healthz 返回 3 个上游", txt)

        print("== 2. 非流式（应跳过 dead + strict-mismatch，落到 good）==")
        code, hdrs, txt = http("POST", base + "/v1/chat/completions",
                               {"messages": [{"role": "user", "content": "ping"}]})
        check(code == 200, "状态码 200", str(code))
        check(hdrs.get("X-Gateway-Provider") == "good", "命中 good 上游",
              str(hdrs.get("X-Gateway-Provider")))
        if code == 200:
            check(json.loads(txt)["choices"][0]["message"]["content"] == "pong", "内容正确")

        print("== 3. 流式（SSE）==")
        code, _, txt = http("POST", base + "/v1/chat/completions",
                            {"messages": [{"role": "user", "content": "ping"}],
                             "stream": True})
        check(code == 200, "状态码 200", str(code))
        check("data: [DONE]" in txt, "收到 [DONE]")
        check("假上游" in txt, "收到流式内容")

        print("== 4. 面板状态与记账 ==")
        code, _, txt = http("GET", base + "/admin/api/status")
        st = json.loads(txt) if code == 200 else {}
        check(code == 200, "status 200", str(code))
        good = next((p for p in st.get("providers", []) if p["name"] == "good"), {})
        check(good.get("requests_24h", 0) >= 2, "good 已记账 >= 2 次",
              str(good.get("requests_24h")))
        check(good.get("cost_24h", 0) > 0, "成本已计算", str(good.get("cost_24h")))
        dead = next((p for p in st.get("providers", []) if p["name"] == "dead-first"), {})
        check(dead.get("cooldown", 0) > 0 or dead.get("fails", 0) > 0,
              "dead 上游进入冷却/失败计数", str(dead.get("last_error", ""))[:60])

        print("== 5. 一键停用 good 后应全失败 ==")
        code, _, txt = http("POST", base + "/admin/api/toggle",
                            {"name": "good", "enabled": False})
        check(code == 200 and json.loads(txt).get("ok"), "停用成功", txt)
        code, _, txt = http("POST", base + "/v1/chat/completions",
                            {"messages": [{"role": "user", "content": "ping"}]})
        check(code == 502, "全部停用后返回 502", str(code))

        print("== 6. 重新启用后恢复 ==")
        http("POST", base + "/admin/api/toggle", {"name": "good", "enabled": True})
        code, hdrs, _ = http("POST", base + "/v1/chat/completions",
                             {"messages": [{"role": "user", "content": "ping"}]})
        check(code == 200 and hdrs.get("X-Gateway-Provider") == "good",
              "恢复后重新命中 good", f"{code} {hdrs.get('X-Gateway-Provider')}")

        print("== 7. 重载配置 ==")
        code, _, txt = http("POST", base + "/admin/api/reload", {})
        check(code == 200 and json.loads(txt).get("ok"), "重载成功", txt)

        print("== 8. providers.d/ 合并 + 免费额度硬卡 ==")
        # 丢一个 json 进 providers.d，重载后应自动并入；配额设 30 tokens（每次调用 15）
        pd = os.path.join(tmp, "providers.d")
        os.makedirs(pd, exist_ok=True)
        with open(os.path.join(pd, "extra.json"), "w", encoding="utf-8") as fh:
            json.dump({"providers": [{
                "name": "quota-tiny", "enabled": True, "priority": 0,
                "base_url": f"http://127.0.0.1:{UP_PORT}", "model": MODEL,
                "api_key": "k", "free_only": True, "free_quota_tokens": 30,
                "price_in": 0.0, "price_out": 0.0, "quant": "-",
                "note": "配额只有 30 tokens",
            }]}, fh, ensure_ascii=False)

        http("POST", base + "/admin/api/reload", {})
        code, _, txt = http("GET", base + "/healthz")
        check(json.loads(txt)["providers"] == 4, "providers.d 的文件已并入（4 个上游）", txt)

        code, hdrs, _ = http("POST", base + "/v1/chat/completions",
                             {"messages": [{"role": "user", "content": "ping"}]})
        check(hdrs.get("X-Gateway-Provider") == "quota-tiny", "首次命中配额上游",
              str(hdrs.get("X-Gateway-Provider")))

        code, hdrs, _ = http("POST", base + "/v1/chat/completions",
                             {"messages": [{"role": "user", "content": "ping"}]})
        check(hdrs.get("X-Gateway-Provider") == "quota-tiny", "额度未满时继续用它",
              str(hdrs.get("X-Gateway-Provider")))

        code, hdrs, _ = http("POST", base + "/v1/chat/completions",
                             {"messages": [{"role": "user", "content": "ping"}]})
        check(hdrs.get("X-Gateway-Provider") == "good", "额度用尽后自动跳过、切下一家",
              str(hdrs.get("X-Gateway-Provider")))

        code, _, txt = http("GET", base + "/admin/api/status")
        st = json.loads(txt)
        tiny = next((p for p in st["providers"] if p["name"] == "quota-tiny"), {})
        check(tiny.get("exhausted") is True, "面板标记额度用尽", str(tiny.get("exhausted")))
        check(tiny.get("used_tokens", 0) >= 30, "已用 token 计数正确",
              str(tiny.get("used_tokens")))

        print("== 9. type=opencode 上游（驱动 opencode serve）==")
        with open(os.path.join(pd, "opencode.json"), "w", encoding="utf-8") as fh:
            json.dump({"providers": [{
                "name": "opencode-free", "type": "opencode", "enabled": True,
                "priority": 0, "base_url": f"http://127.0.0.1:{OC_PORT}",
                "model": "deepseek-v4.1-flash-free",
                "opencode_provider": "opencode",
                "free_only": True, "free_quota_tokens": 0,
                "price_in": 0, "price_out": 0, "quant": "-",
                "note": "假 opencode",
            }]}, fh, ensure_ascii=False)
        http("POST", base + "/admin/api/reload", {})

        code, hdrs, txt = http("POST", base + "/v1/chat/completions",
                               {"messages": [{"role": "user", "content": "你好"}]})
        check(hdrs.get("X-Gateway-Provider") == "opencode-free",
              "命中 opencode 上游", str(hdrs.get("X-Gateway-Provider")))
        body = json.loads(txt) if code == 200 else {}
        content = body.get("choices", [{}])[0].get("message", {}).get("content", "")
        check(content.startswith("OC["), "内容来自 opencode server", content[:40])
        check(body.get("usage", {}).get("prompt_tokens") == 12, "usage 已透传",
              str(body.get("usage")))

        code, hdrs, txt = http("POST", base + "/v1/chat/completions",
                               {"messages": [{"role": "user", "content": "你好"}],
                                "stream": True})
        check(code == 200 and "data: [DONE]" in txt,
              "opencode 上游的流式（缓冲后分块）可用")
        check(hdrs.get("X-Gateway-Stream") == "buffered", "标记为 buffered 流",
              str(hdrs.get("X-Gateway-Stream")))

        code, _, txt = http("POST", base + "/admin/api/probe", {"name": "opencode-free"})
        probe = json.loads(txt) if code == 200 else {}
        check(probe.get("ok") is True, "probe 自检通过", str(probe.get("error")))

        # 停用后应回落到其它上游，而不是 502
        http("POST", base + "/admin/api/toggle", {"name": "opencode-free", "enabled": False})
        code, hdrs, _ = http("POST", base + "/v1/chat/completions",
                             {"messages": [{"role": "user", "content": "你好"}]})
        check(code == 200 and hdrs.get("X-Gateway-Provider") == "good",
              "停用 opencode 后正常回落", str(hdrs.get("X-Gateway-Provider")))

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        upstream.shutdown()
        oc.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if FAILS:
        print(f"失败 {len(FAILS)} 项：")
        for m in FAILS:
            print("  -", m)
        return 1
    print("全部通过 - 网关端到端行为正常")
    return 0


if __name__ == "__main__":
    sys.exit(main())
