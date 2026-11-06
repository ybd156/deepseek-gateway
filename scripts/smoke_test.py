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
 10. 面板填 Key（/admin/api/set-key → secrets.json 落盘、来源标记、清空）
 11. 面板设置（策略 / 冷却 / 口令：保存、生效、非法值拒绝、口令开关）
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import socketserver
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
sys.path.insert(0, os.path.join(ROOT, "app"))

GW_PORT = 18888
UP_PORT = 18999
OC_PORT = 18998            # 假 opencode server
SOCKS_PORT = 18996          # 假 SOCKS5 代理
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

    def do_GET(self):
        if self.path.rstrip("/").endswith("/models"):
            self._json = None
            out = {"object": "list", "data": [
                {"id": "m-one"},
                {"id": "free-model:free"},
                {"id": "m-two", "pricing": {"prompt": "0", "completion": "0"}},
            ]}
            data = json.dumps(out).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        echo = body.get("model") or MODEL
        if body.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for piece in ("你好", "，这是", "假上游"):
                ev = {"id": "chatcmpl-x", "object": "chat.completion.chunk",
                      "model": echo,
                      "choices": [{"index": 0, "delta": {"content": piece}}]}
                self._chunk(f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode())
            tail = {"id": "chatcmpl-x", "object": "chat.completion.chunk", "model": echo,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 12, "completion_tokens": 7}}
            self._chunk(f"data: {json.dumps(tail, ensure_ascii=False)}\n\n".encode())
            self._chunk(b"data: [DONE]\n\n")
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        else:
            out = {"id": "chatcmpl-x", "object": "chat.completion", "model": echo,
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

    def do_GET(self):
        if self.path == "/global/health":
            self._json(200, {"healthy": True, "version": "test-1.0"})
        elif self.path == "/config/providers":
            self._json(200, {"providers": [{
                "id": "opencode",
                "models": {"deepseek-v4.1-flash-free": {}, "other-model": {}},
            }]})
        elif self.path == "/agent":
            self._json(200, [{"name": "build"}, {"name": "plan"}])
        else:
            self._json(404, {"error": "not found"})


class FakeSocks5(socketserver.BaseRequestHandler):
    """极简 SOCKS5：无认证 + CONNECT，把流量转发到假上游。"""

    @staticmethod
    def _read(sock, n):
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                break
            buf += chunk
        return buf

    @staticmethod
    def _pipe(a, b):
        try:
            while True:
                data = a.recv(65536)
                if not data:
                    break
                b.sendall(data)
        except Exception:
            pass
        finally:
            try:
                b.shutdown(socket.SHUT_WR)
            except Exception:
                pass

    def handle(self):
        c = self.request
        try:
            hdr = self._read(c, 2)
            if len(hdr) < 2:
                return
            self._read(c, hdr[1])
            c.sendall(bytes([5, 0]))                      # 无认证
            req = self._read(c, 4)
            if len(req) < 4:
                return
            atyp = req[3]
            if atyp == 1:
                self._read(c, 4)
            elif atyp == 3:
                ln = self._read(c, 1)
                if not ln:
                    return
                self._read(c, ln[0])
            else:
                self._read(c, 16)
            self._read(c, 2)
            up = socket.create_connection(("127.0.0.1", UP_PORT), timeout=5)
            c.sendall(bytes([5, 0, 0, 1]) + socket.inet_aton("127.0.0.1")
                      + UP_PORT.to_bytes(2, "big"))
            threading.Thread(target=self._pipe, args=(c, up), daemon=True).start()
            self._pipe(up, c)
        except Exception:
            pass
        finally:
            try:
                c.close()
            except Exception:
                pass


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

    socks = socketserver.ThreadingTCPServer(("127.0.0.1", SOCKS_PORT), FakeSocks5)
    socks.daemon_threads = True
    threading.Thread(target=socks.serve_forever, daemon=True).start()

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
            json.dump({"providers": [
                {
                    "name": "quota-tiny", "enabled": True, "priority": 0,
                    "base_url": f"http://127.0.0.1:{UP_PORT}", "model": MODEL,
                    "api_key": "k", "free_only": True, "free_quota_tokens": 30,
                    "price_in": 0.0, "price_out": 0.0, "quant": "-",
                    "note": "配额只有 30 tokens",
                },
                {
                    # 用来测「面板填 Key」：有 api_key_env 但没给 key
                    "name": "keytest", "enabled": False, "priority": 99,
                    "base_url": f"http://127.0.0.1:{UP_PORT}", "model": MODEL,
                    "api_key_env": "TEST_KEY_X", "free_only": True,
                    "free_quota_tokens": 0, "price_in": 0.0, "price_out": 0.0,
                    "quant": "-", "note": "测密钥写入",
                },
            ]}, fh, ensure_ascii=False)

        http("POST", base + "/admin/api/reload", {})
        code, _, txt = http("GET", base + "/healthz")
        check(json.loads(txt)["providers"] == 5,
              "providers.d 的文件已并入（3 + 2 = 5 个上游）", txt)

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

        print("== 9b. opencode server 没起来时，报错要能看懂 ==")
        with open(os.path.join(pd, "opencode-dead.json"), "w", encoding="utf-8") as fh:
            json.dump({"providers": [{
                "name": "opencode-dead", "type": "opencode", "enabled": True,
                "priority": 0, "base_url": f"http://127.0.0.1:{DEAD_PORT}",
                "model": "deepseek-v4.1-flash-free", "opencode_provider": "opencode",
                "free_only": True, "free_quota_tokens": 0,
                "price_in": 0, "price_out": 0, "quant": "-", "note": "故意连不上",
            }]}, fh, ensure_ascii=False)
        http("POST", base + "/admin/api/reload", {})
        code, _, txt = http("POST", base + "/admin/api/probe", {"name": "opencode-dead"})
        probe = json.loads(txt) if code == 200 else {}
        err = str(probe.get("error") or "")
        check(probe.get("ok") is False, "自检如实报告失败", err[:80])
        check("没在运行" in err and ("一键安装" in err or "启动" in err),
              "报错里给了可操作提示", err[:100])
        http("POST", base + "/admin/api/toggle", {"name": "opencode-dead", "enabled": False})

        print("== 10. 面板填 Key（/admin/api/set-key → secrets.json）==")
        code, _, txt = http("POST", base + "/admin/api/set-key",
                            {"name": "keytest", "key": "sk-test-abc"})
        d = json.loads(txt) if code == 200 else {}
        check(code == 200 and d.get("ok") and d.get("has_key"), "写入密钥成功", txt[:120])
        check(d.get("source") == "面板", "来源标记为「面板」", str(d.get("source")))

        code, _, txt = http("GET", base + "/admin/api/status")
        st = json.loads(txt)
        kt = next((p for p in st["providers"] if p["name"] == "keytest"), {})
        check(kt.get("has_key") is True and kt.get("key_source") == "面板",
              "状态接口反映密钥来源", f"{kt.get('has_key')} / {kt.get('key_source')}")

        # secrets.json 真的落盘了
        sec_path = os.path.join(data_dir, "secrets.json")
        check(os.path.exists(sec_path), "secrets.json 已生成", sec_path)
        if os.path.exists(sec_path):
            sec = json.load(open(sec_path, encoding="utf-8"))
            check(sec.get("TEST_KEY_X") == "sk-test-abc", "密钥内容正确", str(list(sec)))

        # 清空
        code, _, txt = http("POST", base + "/admin/api/set-key",
                            {"name": "keytest", "key": ""})
        d = json.loads(txt) if code == 200 else {}
        check(code == 200 and d.get("ok") and not d.get("has_key"), "清空密钥成功", txt[:120])

        print("== 11. 面板设置（策略 / 冷却 / 口令）==")
        code, _, txt = http("POST", base + "/admin/api/set-settings",
                            {"strategy": "cheapest", "cooldown_seconds": 5})
        d = json.loads(txt) if code == 200 else {}
        check(code == 200 and d.get("ok") and d.get("strategy") == "cheapest",
              "保存策略/冷却成功", txt[:120])
        check(d.get("cooldown_seconds") == 5, "冷却已生效", str(d.get("cooldown_seconds")))

        code, _, txt = http("GET", base + "/admin/api/status")
        st = json.loads(txt)
        check(st.get("strategy") == "cheapest" and st.get("cooldown_seconds") == 5,
              "状态接口反映新设置", f"{st.get('strategy')}/{st.get('cooldown_seconds')}")

        code, _, _ = http("POST", base + "/admin/api/set-settings", {"strategy": "bogus"})
        check(code == 400, "非法策略被拒绝", str(code))

        code, _, txt = http("POST", base + "/admin/api/set-settings",
                            {"gateway_token": "sk-gw-test"})
        check(code == 200 and json.loads(txt).get("auth_enabled") is True,
              "口令已启用", txt[:120])
        code, _, _ = http("GET", base + "/admin/api/status")
        check(code == 401, "无口令访问被拒", str(code))
        code, _, _ = http("GET", base + "/admin/api/status",
                          headers={"Authorization": "Bearer sk-gw-test"})
        check(code == 200, "带口令访问通过", str(code))

        http("POST", base + "/admin/api/set-settings", {"gateway_token": ""},
             headers={"Authorization": "Bearer sk-gw-test"})
        code, _, _ = http("GET", base + "/admin/api/status")
        check(code == 200, "清除口令后恢复免口令", str(code))

        print("== 12. 设置页整页读写（/admin/api/config）==")
        code, _, txt = http("GET", base + "/admin/api/config")
        cfg = json.loads(txt) if code == 200 else {}
        check(code == 200 and len(cfg.get("providers", [])) >= 3, "读取配置快照", str(code))
        names = [p["name"] for p in cfg.get("providers", [])]
        check("good" in names and "_source" in (cfg["providers"][0] if cfg.get("providers") else {}),
              "快照带 _source 标记", str(names[:5]))

        # 改一个上游的 note/价格，并注入一个「UI 不认识」的字段，验证保存后不丢
        for p in cfg["providers"]:
            if p["name"] == "good":
                p["note"] = "改过的备注"
                p["price_in"] = 0.0123
                p["extra"] = {"provider": {"only": ["TestPin"]}}
        body = {
            "strategy": "priority",
            "cooldown_seconds": 7,
            "proxy": "http://127.0.0.1:7890",
            "providers": cfg["providers"],
            "secrets": {"TEST_KEY_X": "sk-roundtrip"},
        }
        code, _, txt = http("POST", base + "/admin/api/config", body)
        d = json.loads(txt) if code == 200 else {}
        check(code == 200 and d.get("ok"), "整页保存成功", txt[:160])
        check(d.get("cooldown_seconds") == 7 and d.get("proxy") == "http://127.0.0.1:7890",
              "全局设置已生效", f"{d.get('cooldown_seconds')} / {d.get('proxy')}")
        saved = next((p for p in d.get("providers", []) if p["name"] == "good"), {})
        check(saved.get("note") == "改过的备注" and abs(saved.get("price_in", 0) - 0.0123) < 1e-9,
              "上游字段已落盘", str(saved.get("note")))
        kt = next((p for p in d.get("providers", []) if p["name"] == "keytest"), {})
        check(kt.get("_key_source") == "面板", "密钥来源反映为面板",
              str(kt.get("_key_source")))
        check(saved.get("extra", {}).get("provider", {}).get("only") == ["TestPin"],
              "UI 不认识的字段（extra）未被丢弃", str(saved.get("extra")))
        check(all(p.get("name") for p in d.get("providers", [])),
              "每个上游都保住了 name", str([p.get("name") for p in d.get("providers", [])]))

        # 非法策略要被挡住
        code, _, _ = http("POST", base + "/admin/api/config",
                          {"strategy": "nope", "providers": cfg["providers"]})
        check(code == 400, "非法策略被拒绝", str(code))

        print("== 13. OpenCode 运行时托管 ==")
        code, _, txt = http("GET", base + "/admin/api/opencode")
        ocst = json.loads(txt) if code == 200 else {}
        check(code == 200, "读取运行时状态", str(code))
        check("installed" in ocst and "running" in ocst and "port" in ocst,
              "状态字段齐全", str(sorted(ocst))[:100])
        check("bin_source" in ocst and "node_version" in ocst,
              "带环境检测信息（node / bin_source）", str(ocst.get("bin_source")))
        check(ocst.get("running") is False, "当前未运行", str(ocst.get("running")))
        code, _, _ = http("POST", base + "/admin/api/opencode", {"action": "bogus"})
        check(code == 400, "非法 action 被拒绝", str(code))
        code, _, txt = http("POST", base + "/admin/api/opencode", {"action": "start"})
        d = json.loads(txt) if code == 200 else {}
        if not d.get("installed"):
            check(d.get("ok") is False and "opencode" in str(d.get("message", "")),
                  "未安装时启动给出可操作提示", str(d.get("message"))[:80])
        code, _, txt = http("POST", base + "/admin/api/opencode", {"action": "stop"})
        check(code == 200 and json.loads(txt).get("ok") is True, "停止幂等", txt[:80])

        print("== 14. 代理：地址解析 + SOCKS5 端到端 ==")
        from proxy_support import parse_proxy
        check(parse_proxy("192.168.1.10:7890") == ("http", "192.168.1.10", 7890),
              "裸地址按 http 解析", str(parse_proxy("192.168.1.10:7890")))
        check(parse_proxy("socks5://127.0.0.1:1080")[:2] == ("socks5", "127.0.0.1"),
              "识别 socks5 协议", str(parse_proxy("socks5://127.0.0.1:1080")))
        check(parse_proxy("") is None, "空值返回 None")

        code, _, txt = http("POST", base + "/admin/api/set-settings",
                            {"proxy": f"socks5://127.0.0.1:{SOCKS_PORT}"})
        check(code == 200, "设置 socks5 全局代理", txt[:80])
        code, hdrs, txt = http("POST", base + "/v1/chat/completions",
                               {"messages": [{"role": "user", "content": "via socks"}]})
        check(code == 200 and hdrs.get("X-Gateway-Provider") == "good",
              "经 SOCKS5 隧道调用成功", f"{code} {hdrs.get('X-Gateway-Provider')}")

        print("== 15. 免密钥上游（本地 Ollama 这类）==")
        code, _, txt = http("GET", base + "/admin/api/config")
        cfg2 = json.loads(txt)
        cfg2["proxy"] = ""
        cfg2["providers"].append({
            "name": "local-noauth", "type": "http", "enabled": True, "priority": 0,
            "base_url": f"http://127.0.0.1:{UP_PORT}", "model": MODEL,
            "no_auth": True, "free_only": True, "free_quota_tokens": 0,
            "price_in": 0, "price_out": 0, "_source": "main",
        })
        code, _, txt = http("POST", base + "/admin/api/config", cfg2)
        check(code == 200 and json.loads(txt).get("ok"), "保存免密钥上游", txt[:100])
        code, hdrs, txt = http("POST", base + "/v1/chat/completions",
                               {"messages": [{"role": "user", "content": "no key"}]})
        check(code == 200 and hdrs.get("X-Gateway-Provider") == "local-noauth",
              "没有 Key 也能调用", f"{code} {hdrs.get('X-Gateway-Provider')}")
        code, _, txt = http("GET", base + "/admin/api/status")
        st2 = json.loads(txt)
        ln = next((x for x in st2["providers"] if x["name"] == "local-noauth"), {})
        check(ln.get("has_key") is True and ln.get("key_source") == "免密钥",
              "状态显示为免密钥", f"{ln.get('has_key')} / {ln.get('key_source')}")

        print("== 16. 多令牌：别人怎么用这个网关 ==")
        code, _, txt = http("GET", base + "/admin/api/tokens")
        tk = json.loads(txt)
        check(code == 200 and tk.get("open_mode") is True,
              "初始为全开放模式", str(tk.get("open_mode")))

        code, _, txt = http("POST", base + "/admin/api/tokens",
                            {"action": "create", "name": "小明", "quota_tokens": 0})
        d = json.loads(txt)
        key1 = (d.get("token") or {}).get("key", "")
        check(code == 200 and key1.startswith("sk-fnos-"), "创建令牌并返回明文 Key", key1[:14])

        code, hdrs, _ = http("POST", base + "/v1/chat/completions",
                             {"messages": [{"role": "user", "content": "hi"}]},
                             headers={"Authorization": "Bearer " + key1})
        check(code == 200, "用令牌调用成功", str(code))

        code, _, _ = http("POST", base + "/v1/chat/completions",
                          {"messages": [{"role": "user", "content": "hi"}]},
                          headers={"Authorization": "Bearer sk-wrong-key"})
        check(code == 401, "错误 Key 被拒", str(code))

        code, _, _ = http("POST", base + "/v1/chat/completions",
                          {"messages": [{"role": "user", "content": "hi"}]})
        check(code == 401, "没带 Key 被拒（不再是全开放）", str(code))

        # 限额令牌
        code, _, txt = http("POST", base + "/admin/api/tokens",
                            {"action": "create", "name": "限额", "quota_tokens": 10})
        d2 = json.loads(txt)
        key2 = (d2.get("token") or {}).get("key", "")
        tid2 = (d2.get("token") or {}).get("id", "")
        http("POST", base + "/v1/chat/completions",
             {"messages": [{"role": "user", "content": "hi"}]},
             headers={"Authorization": "Bearer " + key2})
        code, _, txt = http("POST", base + "/v1/chat/completions",
                            {"messages": [{"role": "user", "content": "hi"}]},
                            headers={"Authorization": "Bearer " + key2})
        check(code == 429, "超出额度返回 429", f"{code} {txt[:80]}")

        # 停用后立刻失效
        http("POST", base + "/admin/api/tokens", {"action": "toggle", "id": tid2, "enabled": False})
        code, _, _ = http("POST", base + "/v1/chat/completions",
                          {"messages": [{"role": "user", "content": "hi"}]},
                          headers={"Authorization": "Bearer " + key2})
        check(code == 401, "停用后令牌失效", str(code))

        code, _, txt = http("GET", base + "/admin/api/tokens")
        tk2 = json.loads(txt)
        u = next((x for x in tk2["tokens"] if x["id"] == tid2), {})
        check(u.get("used_tokens", 0) > 0, "按令牌统计用量", str(u.get("used_tokens")))

        print("== 17. 代理测试 / Key 显示 / 磁盘回显 ==")
        # 假代理只会把流量转给假上游（明文 HTTP），所以测试目标用 http
        code, _, txt = http("POST", base + "/admin/api/proxy-test",
                            {"proxy": f"socks5://127.0.0.1:{SOCKS_PORT}",
                             "target": f"http://127.0.0.1:{UP_PORT}/"})
        pt = json.loads(txt) if code == 200 else {}
        check(code == 200 and pt.get("ok") is True, "代理测试连通（走 SOCKS5 隧道）",
              f"{pt.get('status')} {pt.get('error','')}")
        check("ms" in pt, "返回耗时", str(pt.get("ms")))

        # HTTPS 目标：假代理不是 TLS 服务，应当失败在握手阶段——
        # 这恰好证明"隧道建立 + TLS 发起"都走到了
        code, _, txt = http("POST", base + "/admin/api/proxy-test",
                            {"proxy": f"socks5://127.0.0.1:{SOCKS_PORT}"})
        ph = json.loads(txt) if code == 200 else {}
        check(ph.get("ok") is False and "SSL" in str(ph.get("error", "")).upper(),
              "HTTPS 经隧道会发起 TLS 握手", str(ph.get("error"))[:70])

        code, _, txt = http("POST", base + "/admin/api/proxy-test",
                            {"proxy": "socks5://127.0.0.1:1"})
        pf = json.loads(txt) if code == 200 else {}
        check(pf.get("ok") is False, "坏代理如实报失败", str(pf.get("error"))[:60])

        # Key 显示
        code, _, txt = http("POST", base + "/admin/api/tokens",
                            {"action": "create", "name": "看看key", "quota_tokens": 0})
        d3 = json.loads(txt)
        tid3 = (d3.get("token") or {}).get("id", "")
        key3 = (d3.get("token") or {}).get("key", "")
        code, _, txt = http("POST", base + "/admin/api/tokens",
                            {"action": "reveal", "id": tid3})
        rv = json.loads(txt) if code == 200 else {}
        check(rv.get("key") == key3, "reveal 返回完整 Key", str(rv.get("key"))[:14])
        code, _, _ = http("POST", base + "/admin/api/tokens",
                          {"action": "reveal", "id": "nope"})
        check(code == 404, "不存在的令牌返回 404", str(code))

        # 保存回显：磁盘上到底存了什么
        code, _, txt = http("GET", base + "/admin/api/config")
        cfg3 = json.loads(txt)
        cfg3["proxy"] = f"socks5://127.0.0.1:{SOCKS_PORT}"
        code, _, txt = http("POST", base + "/admin/api/config", cfg3)
        d4 = json.loads(txt)
        od = d4.get("on_disk") or {}
        check(od.get("proxy") == f"socks5://127.0.0.1:{SOCKS_PORT}",
              "磁盘确认代理已落盘", str(od))

        print("== 18. OpenCode 诊断 ==")
        # 把「运行时端口」指到假 server，诊断才是针对它
        http("POST", base + "/admin/api/set-settings", {"opencode_port": OC_PORT})
        code, _, txt = http("POST", base + "/admin/api/opencode", {"action": "diagnose"})
        dg = json.loads(txt) if code == 200 else {}
        diag = dg.get("diag") or {}
        steps = {x.get("step"): x for x in diag.get("steps", [])}
        check(code == 200 and diag, "诊断返回结果", str(code))
        check(steps.get("health", {}).get("ok") is True, "health 通",
              str(steps.get("health", {}).get("error"))[:60])
        check(steps.get("providers", {}).get("ok") is True, "能列出 providers",
              str(steps.get("providers", {}).get("error"))[:60])
        provs = (steps.get("providers", {}).get("data") or [])
        found = any(p.get("deepseek") for p in provs if isinstance(p, dict))
        check(found, "诊断里能看到 deepseek 模型", str(provs)[:120])
        check(steps.get("agents", {}).get("ok") is True, "能列出 agents",
              str(steps.get("agents", {}).get("data")))

        print("== 19. opencode 模型预检 ==")
        with open(os.path.join(pd, "opencode-badmodel.json"), "w", encoding="utf-8") as fh:
            json.dump({"providers": [{
                "name": "oc-badmodel", "type": "opencode", "enabled": False,
                "priority": 99, "base_url": f"http://127.0.0.1:{OC_PORT}",
                "model": "no-such-model", "opencode_provider": "opencode",
                "free_only": True, "free_quota_tokens": 0,
                "price_in": 0, "price_out": 0, "quant": "-",
            }]}, fh, ensure_ascii=False)
        http("POST", base + "/admin/api/reload", {})
        code, _, txt = http("POST", base + "/admin/api/probe", {"name": "oc-badmodel"})
        d5 = json.loads(txt) if code == 200 else {}
        err = str(d5.get("error") or "")
        check(d5.get("ok") is False and "可用列表" in err,
              "模型不存在时给出可用列表", err[:100])

        print("== 20. CORS 与 /v1/models（浏览器类客户端要用）==")
        code, hdrs, txt = http("GET", base + "/v1/models",
                               headers={"Authorization": "Bearer " + key1})
        check(code == 200, "带令牌能列模型", str(code))
        check(hdrs.get("Access-Control-Allow-Origin") == "*",
              "返回 CORS 头", str(hdrs.get("Access-Control-Allow-Origin")))
        ids = [m["id"] for m in json.loads(txt).get("data", [])]
        check(len(ids) > 0, "模型列表非空", str(ids[:3]))

        code, _, _ = http("OPTIONS", base + "/v1/chat/completions")
        check(code == 204, "OPTIONS 预检返回 204", str(code))

        print("== 21. 多模型与模型探测 ==")
        code, _, txt = http("POST", base + "/admin/api/discover", {"name": "good"})
        d6 = json.loads(txt) if code == 200 else {}
        ids = [m["id"] for m in d6.get("models", [])]
        check(d6.get("ok") is True and len(ids) >= 3, "探测到模型列表", str(ids[:4]))
        freeids = [m["id"] for m in d6.get("models", []) if m.get("free")]
        check("free-model:free" in freeids and "m-two" in freeids,
              "识别出免费模型（含 pricing=0）", str(freeids))

        code, _, txt = http("GET", base + "/admin/api/config")
        cfg4 = json.loads(txt)
        for pp in cfg4["providers"]:
            if pp["name"] == "good":
                pp["models"] = ["alt-model", "third-model"]
        code, _, txt = http("POST", base + "/admin/api/config", cfg4)
        check(code == 200 and json.loads(txt).get("ok"), "保存多模型配置", txt[:80])
        code, _, txt = http("GET", base + "/admin/api/config")
        gg = next((x for x in json.loads(txt).get("providers", [])
                   if x["name"] == "good"), {})
        check(gg.get("models") == ["alt-model", "third-model"],
              "models 已写回配置", str(gg.get("models")))

        code, hdrs, txt = http("POST", base + "/v1/chat/completions",
                            {"model": "alt-model",
                             "messages": [{"role": "user", "content": "hi"}]},
                            headers={"Authorization": "Bearer " + key1})
        try:
            body = json.loads(txt) if code == 200 else {}
        except Exception as exc:
            body = {"_parse_error": str(exc), "_raw": txt[:300]}
        check(code == 200 and body.get("model") == "alt-model",
              "客户端点名的模型被送到上游",
              f"provider={hdrs.get('X-Gateway-Provider')} model={body.get('model')}")

        code, _, txt = http("GET", base + "/v1/models",
                            headers={"Authorization": "Bearer " + key1})
        allids = [m["id"] for m in json.loads(txt).get("data", [])]
        check("alt-model" in allids, "/v1/models 列出多模型", str(allids[:6]))

        print("== 22. 验证 Key（排查 401）==")
        code, _, txt = http("POST", base + "/admin/api/verify-key", {"key": key1})
        v1 = json.loads(txt) if code == 200 else {}
        check(v1.get("ok") is True and v1.get("kind") == "令牌", "有效令牌验证通过", str(v1))
        check(v1.get("name") == "小明", "回显令牌备注", str(v1.get("name")))

        masked = key1[:9] + "****" + key1[-4:]
        code, _, txt = http("POST", base + "/admin/api/verify-key", {"key": masked})
        v2 = json.loads(txt) if code == 200 else {}
        check(v2.get("ok") is False and "掩码" in str(v2.get("error")),
              "粘贴掩码时给出提示", str(v2.get("error"))[:80])

        code, _, txt = http("POST", base + "/admin/api/verify-key", {"key": "sk-nope"})
        v3 = json.loads(txt) if code == 200 else {}
        check(v3.get("ok") is False, "无效 Key 如实报告", str(v3.get("error"))[:60])

        print("== 23. 单张上游保存 ==")
        code, _, txt = http("GET", base + "/admin/api/config")
        cfg5 = json.loads(txt)
        target = next(p for p in cfg5["providers"] if p["name"] == "good")
        other_before = next((p for p in cfg5["providers"] if p["name"] == "local-noauth"), {})
        target["note"] = "单独保存的备注"
        code, _, txt = http("POST", base + "/admin/api/save-provider", {"provider": target})
        d7 = json.loads(txt) if code == 200 else {}
        check(code == 200 and d7.get("ok"), "单张保存成功", txt[:100])
        after = {p["name"]: p for p in d7.get("providers", [])}
        check(after.get("good", {}).get("note") == "单独保存的备注",
              "目标已更新", str(after.get("good", {}).get("note")))
        check(after.get("local-noauth", {}).get("note") == other_before.get("note"),
              "其它上游未受影响", str(after.get("local-noauth", {}).get("note")))

        code, _, txt = http("POST", base + "/admin/api/save-provider",
                            {"provider": {"name": "brand-new",
                                          "base_url": "http://127.0.0.1:1/v1",
                                          "model": "m"}})
        d8 = json.loads(txt) if code == 200 else {}
        check(any(p["name"] == "brand-new" for p in d8.get("providers", [])),
              "新名字会自动追加", str([p["name"] for p in d8.get("providers", [])][-3:]))

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        upstream.shutdown()
        oc.shutdown()
        socks.shutdown()
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
