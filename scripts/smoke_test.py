#!/usr/bin/env python3
"""网关端到端冒烟测试（按重写后的接口编写）。

覆盖：健康检查 / 模型列表 / 非流式 / 流式 / 失败切换 / 严格校验 /
配置读写 / 单卡保存 / 启停 / 令牌 / 统计 / 日志 / 脱敏 / 鉴权 / 探测。

用法：python3 scripts/smoke_test.py
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

GW_PORT = 18901
UP_PORT = 18902
DEAD_PORT = 18903

MODEL = "deepseek-v4.1-flash"

FAILS: list = []
PASSED = 0


def check(cond, name, detail=""):
    global PASSED
    if cond:
        PASSED += 1
        print(f"  OK   {name}")
    else:
        FAILS.append(name)
        print(f"  FAIL {name}  {detail}")


def port_free(port: int) -> bool:
    s = socket.socket()
    try:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) != 0
    finally:
        s.close()


class Upstream(BaseHTTPRequestHandler):
    """假上游：OpenAI 兼容，回显收到的最后一条用户消息。"""
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.rstrip("/").endswith("/models"):
            self._json(200, {"data": [
                {"id": "m-free:free"},
                {"id": "m-paid", "pricing": {"prompt": "0.1", "completion": "0.2"}},
                # 带完整能力元数据的模型（OpenRouter 风格字段）
                {"id": "meta-model",
                 "context_length": 111111,
                 "top_provider": {"max_completion_tokens": 2222},
                 "architecture": {"input_modalities": ["text", "image"]}},
            ]})
        else:
            self._json(404, {"error": "nf"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        echo = ""
        for m in body.get("messages") or []:
            if m.get("role") == "user":
                echo = str(m.get("content") or "")
        if body.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()

            def w(obj):
                raw = ("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode()
                self.wfile.write(("%x" % len(raw)).encode() + b"\r\n" + raw + b"\r\n")
                self.wfile.flush()

            for piece in ("你", "好"):
                w({"id": "c1", "object": "chat.completion.chunk", "model": MODEL,
                   "choices": [{"index": 0, "delta": {"content": piece}}]})
            w({"id": "c1", "object": "chat.completion.chunk", "model": MODEL,
               "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
               "usage": {"prompt_tokens": 7, "completion_tokens": 3}})
            raw = ("data: [DONE]\n\n").encode()
            self.wfile.write(("%x" % len(raw)).encode() + b"\r\n" + raw + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        else:
            self._json(200, {
                "id": "c1", "object": "chat.completion", "model": MODEL,
                "choices": [{"index": 0,
                             "message": {"role": "assistant", "content": "echo:" + echo},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 9, "completion_tokens": 4},
            })


def http(method, url, body=None, headers=None, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    h = {"Content-Type": "application/json"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()
    except Exception as exc:                              # noqa: BLE001
        return -1, {}, str(exc).encode()


def main() -> int:
    for p in (GW_PORT, UP_PORT, DEAD_PORT):
        if not port_free(p):
            print(f"端口 {p} 被占用，先关掉再跑")
            return 1

    tmp = tempfile.mkdtemp(prefix="gwsmoke_")
    cfg_path = os.path.join(tmp, "providers.json")
    data_dir = os.path.join(tmp, "data")
    os.makedirs(data_dir, exist_ok=True)

    base_providers = [
        {"name": "dead", "enabled": True, "priority": 1,
         "base_url": f"http://127.0.0.1:{DEAD_PORT}", "model": MODEL,
         "api_key": "k", "price_in": 1, "price_out": 1},
        {"name": "strict", "enabled": True, "priority": 2,
         "base_url": f"http://127.0.0.1:{UP_PORT}", "model": MODEL,
         "api_key": "k", "model_contains": "绝对不存在的模型",
         "strict_model": True, "price_in": 0.5, "price_out": 0.5},
        {"name": "good", "enabled": True, "priority": 3,
         "base_url": f"http://127.0.0.1:{UP_PORT}", "model": MODEL,
         "api_key": "k", "model_contains": "v4.1-flash",
         "free_only": True, "free_quota_tokens": 0,
         "price_in": 0.02, "price_out": 0.4},
    ]
    with open(cfg_path, "w", encoding="utf-8") as fh:
        json.dump({"settings": {}, "providers": base_providers}, fh,
                  ensure_ascii=False, indent=2)

    up = ThreadingHTTPServer(("127.0.0.1", UP_PORT), Upstream)
    up.daemon_threads = True
    threading.Thread(target=up.serve_forever, daemon=True).start()

    env = dict(os.environ, CONFIG_PATH=cfg_path, DATA_DIR=data_dir,
               PORT=str(GW_PORT), HOST="127.0.0.1", COOLDOWN_SECONDS="5")
    env.pop("GATEWAY_TOKEN", None)
    log_path = os.path.join(tmp, "gateway.log")
    log_fh = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, GATEWAY], env=env,
                            stdout=log_fh, stderr=subprocess.STDOUT, text=True)
    base = f"http://127.0.0.1:{GW_PORT}"

    try:
        for _ in range(60):
            time.sleep(0.3)
            if http("GET", base + "/healthz")[0] == 200:
                break
        else:
            print("网关没起来：")
            log_fh.flush()
            print(open(log_path, encoding="utf-8").read()[-1500:])
            return 1

        print("== 1. 健康检查与模型列表 ==")
        code, _, txt = http("GET", base + "/healthz")
        check(code == 200 and json.loads(txt)["ok"] is True, "healthz 正常", str(code))
        code, _, txt = http("GET", base + "/v1/models")
        ids = [m["id"] for m in json.loads(txt).get("data", [])]
        check(code == 200 and MODEL in ids, "/v1/models 列出模型", str(ids[:4]))

        print("== 2. 非流式（跳过 dead + strict，落到 good）==")
        code, hdrs, txt = http("POST", base + "/v1/chat/completions",
                               {"messages": [{"role": "user", "content": "ping"}]})
        check(code == 200, "状态码 200", str(code))
        check(hdrs.get("X-Gateway-Provider") == "good", "命中 good",
              str(hdrs.get("X-Gateway-Provider")))
        body = json.loads(txt) if code == 200 else {}
        content = ""
        try:
            content = str(body["choices"][0]["message"]["content"])
        except Exception:
            pass
        check("echo:ping" in content, "内容正确", content[:80])

        print("== 3. 流式（SSE 透传）==")
        req = urllib.request.Request(
            base + "/v1/chat/completions",
            data=json.dumps({"stream": True,
                             "messages": [{"role": "user", "content": "hi"}]}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        got, code = b"", -1
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                code = r.status
                while True:
                    chunk = r.read(256)
                    if not chunk:
                        break
                    got += chunk
        except Exception as exc:                          # noqa: BLE001
            got = str(exc).encode()
        check(code == 200, "流式状态码 200", str(code))
        check(b"[DONE]" in got, "收到 [DONE]")
        check("你".encode() in got, "收到流式内容")

        print("== 4. 记账与状态 ==")
        time.sleep(0.4)
        code, _, txt = http("GET", base + "/admin/api/status")
        st = json.loads(txt)
        good = next((p for p in st["providers"] if p["name"] == "good"), {})
        check(st.get("total_requests_24h", 0) >= 2, "统计到请求",
              str(st.get("total_requests_24h")))
        check(good.get("requests_24h", 0) >= 2, "good 有请求",
              str(good.get("requests_24h")))
        check(good.get("cost_24h", 0) > 0, "花费已估算", str(good.get("cost_24h")))

        print("== 5. 统计接口 ==")
        code, _, txt = http("GET", base + "/admin/api/stats?days=7")
        sp = json.loads(txt)
        check(code == 200 and sp.get("ok") is True, "统计可读", str(code))
        check(sp.get("totals", {}).get("requests", 0) >= 2, "统计总数正确",
              str(sp.get("totals")))
        check(any(p["provider"] == "good" for p in sp.get("providers", [])),
              "统计里有 good", str([p["provider"] for p in sp.get("providers", [])]))

        print("== 6. 日志 ==")
        code, _, txt = http("GET", base + "/admin/api/logs?kind=usage&limit=50")
        lg = json.loads(txt)
        check(code == 200 and lg.get("ok") is True, "调用日志可读", str(code))
        check(len(lg.get("rows") or []) > 0, "有记录", str(len(lg.get("rows") or [])))
        code, _, _ = http("GET", base + "/admin/api/logs?kind=gateway&limit=30")
        check(code == 200, "网关日志可读", str(code))

        row = (lg.get("rows") or [{}])[0]
        for f in ("first_byte_ms", "tps", "cache_hit", "cache_rate",
                  "client_ip", "request_id", "status", "stream"):
            check(f in row, "日志含字段 " + f, str(list(row))[:80])
        check(any(r.get("stream") for r in (lg.get("rows") or [])),
              "流式请求有标记", str([r.get("stream") for r in (lg.get("rows") or [])][:5]))

        code, _, txt = http("GET", base + "/admin/api/errors")
        er = json.loads(txt)
        check(code == 200 and er.get("ok") is True, "错误汇总可读", str(code))
        check("rows" in er and "kinds" in er, "错误汇总结构完整")

        print("== 7. 配置读写与单卡保存 ==")
        code, _, txt = http("GET", base + "/admin/api/config")
        cfg = json.loads(txt)
        check(code == 200 and len(cfg.get("providers", [])) == 3, "读到 3 个上游",
              str(len(cfg.get("providers", []))))
        target = next(p for p in cfg["providers"] if p["name"] == "good")
        target["note"] = "改过的备注"
        code, _, txt = http("POST", base + "/admin/api/save-provider",
                            {"provider": target})
        d = json.loads(txt) if code == 200 else {}
        check(code == 200 and d.get("ok") is True, "单卡保存成功", txt[:100])
        after = {p["name"]: p for p in d.get("providers", [])}
        check(after.get("good", {}).get("note") == "改过的备注", "备注已更新")
        check(len(after) == 3, "其它上游没丢", str(list(after)))

        print("== 8. 启停 ==")
        code, _, txt = http("POST", base + "/admin/api/toggle",
                            {"name": "dead", "enabled": False})
        check(code == 200 and json.loads(txt).get("ok") is True, "停用成功", txt[:80])
        code, _, txt = http("GET", base + "/admin/api/config")
        dead = next(p for p in json.loads(txt)["providers"] if p["name"] == "dead")
        check(dead.get("enabled") is False, "配置里已停用", str(dead.get("enabled")))

        print("== 9. 全部失败时给出原因 ==")
        http("POST", base + "/admin/api/config", {
            "strategy": "priority", "cooldown_seconds": 5,
            "providers": [{"name": "only", "enabled": False, "priority": 1,
                           "base_url": "http://127.0.0.1:1/v1", "model": "x",
                           "api_key": "k"}]})
        code, _, txt = http("POST", base + "/v1/chat/completions",
                            {"messages": [{"role": "user", "content": "hi"}]})
        msg = ""
        if code == 502:
            try:
                msg = str(json.loads(txt)["error"]["message"])
            except Exception:
                msg = txt.decode("utf-8", "ignore")[:200]
        check(code == 502 and "没有可用上游" in msg, "502 说明原因", msg[:120])
        check("未启用" in msg, "列出未启用", msg[:140])

        http("POST", base + "/admin/api/config", {
            "strategy": "priority", "cooldown_seconds": 5,
            "providers": base_providers})

        print("== 10. 访问令牌 ==")
        code, _, txt = http("POST", base + "/admin/api/tokens",
                            {"action": "create", "name": "小明的笔记本",
                             "quota_tokens": 0})
        tok = json.loads(txt).get("token") or {}
        key = tok.get("key", "")
        check(code == 200 and key.startswith("sk-fnos-"), "创建令牌", key[:12])
        code, _, txt = http("GET", base + "/admin/api/tokens")
        tp = json.loads(txt)
        check(any(t["name"] == "小明的笔记本" for t in tp.get("tokens", [])),
              "令牌列表里有它")
        check(all("key" not in t for t in tp.get("tokens", [])), "列表不回传明文")
        code, _, txt = http("POST", base + "/admin/api/verify-key", {"key": key})
        check(json.loads(txt).get("ok") is True, "验证 Key 通过", txt[:80])

        print("== 11. 出站品牌词脱敏 ==")
        http("POST", base + "/admin/api/config", {
            "sanitize": True, "sanitize_scope": "all",
            "sanitize_words": "OpenCode,Claude Code",
            "sanitize_replacement": "该服务"})
        code, _, txt = http("POST", base + "/v1/chat/completions",
                            {"messages": [{"role": "user",
                                           "content": "我是 OpenCode 用户"}]},
                            headers={"Authorization": "Bearer " + key})
        shown = ""
        if code == 200:
            try:
                shown = str(json.loads(txt)["choices"][0]["message"]["content"])
            except Exception:
                shown = ""
        check(code == 200 and "该服务" in shown and "OpenCode" not in shown,
              "品牌词已被替换", "code=%s shown=%r" % (code, shown[:80]))
        http("POST", base + "/admin/api/config", {"sanitize": False})

        print("== 12. 鉴权（设主口令后）==")
        http("POST", base + "/admin/api/set-settings", {"gateway_token": "master-x"})
        code, _, _ = http("GET", base + "/v1/models",
                          headers={"Authorization": "Bearer " + key})
        check(code == 200, "令牌可以列模型", str(code))
        code, _, _ = http("GET", base + "/v1/models",
                          headers={"Authorization": "Bearer master-x"})
        check(code == 200, "主口令可以列模型", str(code))
        code, _, _ = http("GET", base + "/v1/models",
                          headers={"Authorization": "Bearer nope"})
        check(code == 401, "错误 Key 被拒", str(code))
        http("POST", base + "/admin/api/set-settings", {"gateway_token": ""},
             headers={"Authorization": "Bearer master-x"})

        print("== 13. 探测模型 ==")
        code, _, txt = http("POST", base + "/admin/api/discover", {"name": "good"})
        d = json.loads(txt)
        check(code == 200 and d.get("ok") is True, "探测可用", txt[:80])
        free = [m["id"] for m in d.get("models", []) if m.get("free")]
        check("m-free:free" in free, "识别出免费模型", str(free))


        print("== 14. 模型能力表 ==")
        code, _, txt = http("GET", base + "/admin/api/caps")
        cp = json.loads(txt)
        check(code == 200 and cp.get("ok") is True, "能力接口可读", str(code))
        row = next((m for m in cp.get("models", []) if m["model"] == MODEL), {})
        check(row.get("context", 0) > 0, "识别出上下文长度", str(row.get("context")))
        check(row.get("known") is True, "内置表命中", str(row.get("known")))

        print("== 15. 模型黑名单 ==")
        http("POST", base + "/admin/api/config", {"model_blacklist": MODEL})
        code, _, txt = http("POST", base + "/v1/chat/completions",
                            {"model": MODEL,
                             "messages": [{"role": "user", "content": "hi"}]},
                            headers={"Authorization": "Bearer " + key})
        msg1 = ""
        try:
            msg1 = str(json.loads(txt)["error"]["message"])
        except Exception:
            pass
        check(code == 400 and "黑名单" in msg1, "黑名单拦住了", str(code) + " " + msg1[:60])
        http("POST", base + "/admin/api/config", {"model_blacklist": ""})

        print("== 16. 模型白名单 ==")
        http("POST", base + "/admin/api/config", {"model_whitelist": "别的模型"})
        code, _, txt = http("POST", base + "/v1/chat/completions",
                            {"model": MODEL,
                             "messages": [{"role": "user", "content": "hi"}]},
                            headers={"Authorization": "Bearer " + key})
        check(code == 400, "白名单外被拒", str(code))
        http("POST", base + "/admin/api/config", {"model_whitelist": ""})

        print("== 17. 模型映射（回显原名）==")
        http("POST", base + "/admin/api/config",
             {"model_mapping": MODEL + "=upstream-real-name"})
        code, hdrs, txt = http("POST", base + "/v1/chat/completions",
                               {"model": MODEL,
                                "messages": [{"role": "user", "content": "m"}]},
                               headers={"Authorization": "Bearer " + key})
        body = json.loads(txt) if code == 200 else {}
        check(code == 200, "映射后仍成功", str(code))
        check(body.get("model") == MODEL, "响应回显客户端原名",
              str(body.get("model")))
        check(hdrs.get("X-Gateway-Upstream-Model") == "upstream-real-name",
              "响应头给出上游实际模型",
              str(hdrs.get("X-Gateway-Upstream-Model")))

        print("== 17b. 日志区分「点名」与「上游实际」==")
        time.sleep(0.3)
        code, _, txt = http("GET", base + "/admin/api/logs?kind=usage&limit=20")
        rows = json.loads(txt).get("rows") or []
        row = next((r for r in rows if r.get("upstream_model") == "upstream-real-name"), {})
        check(bool(row), "日志里有这一条", str([r.get("upstream_model") for r in rows][:4]))
        check(row.get("model") == MODEL, "点名列是客户端原名",
              str(row.get("model")))
        check(row.get("upstream_model") == "upstream-real-name", "上游列是真实模型",
              str(row.get("upstream_model")))
        http("POST", base + "/admin/api/config", {"model_mapping": ""})

        print("== 18. 提示词模式 default ==")
        http("POST", base + "/admin/api/config",
             {"system_mode": "default", "system_text": "网关注入的系统提示"})
        code, _, txt = http("POST", base + "/v1/chat/completions",
                            {"messages": [{"role": "system", "content": "客户端的"},
                                          {"role": "user", "content": "s"}]},
                            headers={"Authorization": "Bearer " + key})
        check(code == 200, "注入后成功", str(code))
        http("POST", base + "/admin/api/config",
             {"system_mode": "passthrough", "system_text": ""})

        print("== 19. 只保留最近 N 轮 ==")
        http("POST", base + "/admin/api/config", {"keep_rounds": 1})
        long_msgs = []
        for i in range(6):
            long_msgs.append({"role": "user", "content": "第%d轮" % i})
            long_msgs.append({"role": "assistant", "content": "答%d" % i})
        long_msgs.append({"role": "user", "content": "最后一问"})
        code, _, txt = http("POST", base + "/v1/chat/completions",
                            {"messages": long_msgs},
                            headers={"Authorization": "Bearer " + key})
        body = json.loads(txt) if code == 200 else {}
        content = ""
        try:
            content = str(body["choices"][0]["message"]["content"])
        except Exception:
            pass
        check(code == 200 and "最后一问" in content, "裁剪后仍成功", content[:60])
        check("第0轮" not in content, "老对话已被丢掉", content[:60])
        http("POST", base + "/admin/api/config", {"keep_rounds": 0})

        print("== 20. 图片拒绝（模型不支持视觉）==")
        code, _, txt = http("POST", base + "/v1/chat/completions",
                            {"model": MODEL, "messages": [
                                {"role": "user", "content": [
                                    {"type": "text", "text": "这是什么"},
                                    {"type": "image_url",
                                     "image_url": {"url": "data:image/png;base64,AA"}}]}]},
                            headers={"Authorization": "Bearer " + key})
        msg2 = ""
        try:
            msg2 = str(json.loads(txt)["error"]["message"])
        except Exception:
            pass
        check(code == 400 and "图片" in msg2, "不支持视觉时明确拒绝",
              str(code) + " " + msg2[:60])


        print("== 21. 点名不存在的模型：明确报错，不换默认 ==")
        code, _, txt = http("POST", base + "/v1/chat/completions",
                            {"model": "根本不存在的模型",
                             "messages": [{"role": "user", "content": "x"}]},
                            headers={"Authorization": "Bearer " + key})
        m3 = ""
        try:
            m3 = str(json.loads(txt)["error"]["message"])
        except Exception:
            pass
        check(code == 400, "返回 400 而不是偷偷用默认", str(code))
        check("没有上游支持模型" in m3, "说清楚是模型问题", m3[:70])
        check(MODEL in m3, "列出可用模型", m3[:110])

        print("== 22. 点名列表里有的模型：必须发这个 ==")
        http("POST", base + "/admin/api/config", {
            "strategy": "priority", "cooldown_seconds": 5,
            "providers": [
                {"name": "two-model", "enabled": True, "priority": 1,
                 "base_url": f"http://127.0.0.1:{UP_PORT}",
                 "model": MODEL, "models": ["another-model-b"],
                 "api_key": "k"}]})
        code, hdrs, txt = http("POST", base + "/v1/chat/completions",
                               {"model": "another-model-b",
                                "messages": [{"role": "user", "content": "选我"}]},
                               headers={"Authorization": "Bearer " + key})
        body = json.loads(txt) if code == 200 else {}
        got_model = str(body.get("model") or "")
        check(code == 200, "请求成功", str(code))
        check(got_model == "another-model-b", "响应回显点名的模型", got_model)
        check(hdrs.get("X-Gateway-Upstream-Model") == "another-model-b",
              "上游真的收到这个模型名",
              str(hdrs.get("X-Gateway-Upstream-Model")))

        print("== 23. 勾了透传就照发 ==")
        http("POST", base + "/admin/api/config", {
            "strategy": "priority", "cooldown_seconds": 5,
            "providers": [
                {"name": "passer", "enabled": True, "priority": 1,
                 "base_url": f"http://127.0.0.1:{UP_PORT}",
                 "model": MODEL, "model_passthrough": True,
                 "api_key": "k"}]})
        code, hdrs, txt = http("POST", base + "/v1/chat/completions",
                               {"model": "any-name-ok",
                                "messages": [{"role": "user", "content": "p"}]},
                               headers={"Authorization": "Bearer " + key})
        check(code == 200, "透传成功", str(code))
        check(hdrs.get("X-Gateway-Upstream-Model") == "any-name-ok",
              "原样发给上游", str(hdrs.get("X-Gateway-Upstream-Model")))

        http("POST", base + "/admin/api/config", {
            "strategy": "priority", "cooldown_seconds": 5,
            "providers": base_providers})


        print("== 24. OpenCode 状态视图 ==")
        code, _, txt = http("GET", base + "/admin/api/opencode")
        oc = json.loads(txt)
        check(code == 200, "状态可读", str(code))
        for f in ("cli_mode", "needs_server", "needs_login", "ready", "installed"):
            check(f in oc, "状态含字段 " + f, str(list(oc))[:80])


        print("== 25. /v1/models 只列能用的 ==")
        http("POST", base + "/admin/api/config", {
            "strategy": "priority", "cooldown_seconds": 5,
            "providers": [
                {"name": "on-prov", "enabled": True, "priority": 1,
                 "base_url": f"http://127.0.0.1:{UP_PORT}",
                 "model": "model-on", "api_key": "k"},
                {"name": "off-prov", "enabled": False, "priority": 2,
                 "base_url": f"http://127.0.0.1:{UP_PORT}",
                 "model": "model-off", "api_key": "k"},
                {"name": "nokey-prov", "enabled": True, "priority": 3,
                 "base_url": f"http://127.0.0.1:{UP_PORT}",
                 "model": "model-nokey", "api_key_env": "NOT_SET_KEY"}]})
        code, _, txt = http("GET", base + "/v1/models",
                            headers={"Authorization": "Bearer " + key})
        ids = [m["id"] for m in json.loads(txt).get("data", [])]
        check(code == 200, "列表可读", str(code))
        check("model-on" in ids, "启用的模型在列表里", str(ids))
        check("model-off" not in ids, "停用的上游不出现在列表", str(ids))
        check("model-nokey" not in ids, "缺密钥的不出现在列表", str(ids))

        print("== 26. 模型带上下文 / 最大输出 / 图片 ==")
        item = next((m for m in json.loads(txt)["data"] if m["id"] == "model-on"), {})
        check(item.get("context_length", 0) > 0, "带 context_length",
              str(item.get("context_length")))
        check(item.get("context_window") == item.get("context_length"),
              "带 context_window 别名", str(item.get("context_window")))
        check(item.get("max_output_tokens", 0) > 0, "带 max_output_tokens",
              str(item.get("max_output_tokens")))
        check(item.get("max_tokens") == item.get("max_output_tokens"),
              "带 max_tokens 别名", str(item.get("max_tokens")))
        check(item.get("supports_vision") is False, "带 supports_vision",
              str(item.get("supports_vision")))
        check("capabilities" in item and "vision" in item["capabilities"],
              "带 capabilities")
        mi = item.get("model_info") or {}
        check(mi.get("context_size", 0) > 0 and mi.get("max_output", 0) > 0,
              "带 model_info 聚合字段", str(mi))

        print("== 26b. 视觉模型的能力值 ==")
        http("POST", base + "/admin/api/config", {
            "strategy": "priority", "cooldown_seconds": 5,
            "providers": [
                {"name": "vis", "enabled": True, "priority": 1,
                 "base_url": f"http://127.0.0.1:{UP_PORT}",
                 "model": "glm-5.3-flash", "api_key": "k"}]})
        code, _, txt = http("GET", base + "/v1/models",
                            headers={"Authorization": "Bearer " + key})
        v = next((m for m in json.loads(txt)["data"] if m["id"] == "glm-5.3-flash"), {})
        check(v.get("supports_vision") is True, "GLM 5.3 标为支持图片",
              str(v.get("supports_vision")))
        check(v.get("context_length") == 200000, "上下文 200K",
              str(v.get("context_length")))
        check(v.get("max_output_tokens") == 65536, "最大输出 64K",
              str(v.get("max_output_tokens")))
        # 复原成 25 用的三上游配置，后面的用例还要用
        http("POST", base + "/admin/api/config", {
            "strategy": "priority", "cooldown_seconds": 5,
            "providers": [
                {"name": "on-prov", "enabled": True, "priority": 1,
                 "base_url": f"http://127.0.0.1:{UP_PORT}",
                 "model": "model-on", "api_key": "k"},
                {"name": "off-prov", "enabled": False, "priority": 2,
                 "base_url": f"http://127.0.0.1:{UP_PORT}",
                 "model": "model-off", "api_key": "k"},
                {"name": "nokey-prov", "enabled": True, "priority": 3,
                 "base_url": f"http://127.0.0.1:{UP_PORT}",
                 "model": "model-nokey", "api_key_env": "NOT_SET_KEY"}]})

        print("== 27. 管理员能看全量（含不可用原因）==")
        code, _, txt = http("GET", base + "/v1/models?all=1",
                            headers={"Authorization": "Bearer " + key})
        allrows = json.loads(txt).get("data", [])
        ids_all = [m["id"] for m in allrows]
        check("model-off" in ids_all, "全量含停用的", str(ids_all))
        off = next((m for m in allrows if m["id"] == "model-off"), {})
        check(off.get("available") is False, "标出不可用", str(off.get("available")))
        check("未启用" in str(off.get("unavailable_reason")),
              "说明原因", str(off.get("unavailable_reason")))

        print("== 28. 单个模型查询 ==")
        code, _, txt = http("GET", base + "/v1/models/model-on",
                            headers={"Authorization": "Bearer " + key})
        check(code == 200 and json.loads(txt).get("id") == "model-on", "能查单个", str(code))
        code, _, _ = http("GET", base + "/v1/models/no-such-model",
                          headers={"Authorization": "Bearer " + key})
        check(code == 404, "不存在的返回 404", str(code))

        print("== 29. 排查接口 ==")
        code, _, txt = http("GET", base + "/admin/api/models-check")
        mc = json.loads(txt)
        check(code == 200 and mc.get("ok") is True, "排查可读", str(code))
        check(mc.get("total", 0) >= 3, "列出全部模型", str(mc.get("total")))
        check(mc.get("usable", 0) < mc.get("total", 0), "区分可用与不可用",
              str(mc.get("usable")) + "/" + str(mc.get("total")))

        http("POST", base + "/admin/api/config", {
            "strategy": "priority", "cooldown_seconds": 5,
            "providers": base_providers})


        print("== 30. 网关自己会问上游要能力 ==")
        http("POST", base + "/admin/api/config", {
            "strategy": "priority", "cooldown_seconds": 5,
            "providers": [
                {"name": "meta", "enabled": True, "priority": 1,
                 "base_url": f"http://127.0.0.1:{UP_PORT}",
                 "model": "meta-model", "api_key": "k"}]})
        code, _, txt = http("POST", base + "/admin/api/refresh-caps", {"name": "meta"})
        rc = json.loads(txt)
        check(code == 200 and rc.get("ok") is True, "探测接口可用", str(code))
        check(rc.get("updated", 0) >= 1, "探测到能力", str(rc))

        code, _, txt = http("GET", base + "/v1/models",
                            headers={"Authorization": "Bearer " + key})
        mm = next((m for m in json.loads(txt)["data"] if m["id"] == "meta-model"), {})
        check(mm.get("context_length") == 111111, "上下文来自上游",
              str(mm.get("context_length")))
        check(mm.get("max_output_tokens") == 2222, "最大输出也来自上游",
              str(mm.get("max_output_tokens")))
        check(mm.get("supports_vision") is True, "图片支持来自上游",
              str(mm.get("supports_vision")))
        check(mm.get("capability_source") == "upstream", "标出来源是上游",
              str(mm.get("capability_source")))

        print("== 31. 手填的值优先于上游 ==")
        http("POST", base + "/admin/api/config",
             {"model_caps": {"meta-model": {"context": 8000}}})
        code, _, txt = http("GET", base + "/v1/models",
                            headers={"Authorization": "Bearer " + key})
        mm2 = next((m for m in json.loads(txt)["data"] if m["id"] == "meta-model"), {})
        check(mm2.get("context_length") == 8000, "手动覆盖生效",
              str(mm2.get("context_length")))
        check(mm2.get("capability_source") == "manual", "来源变手动",
              str(mm2.get("capability_source")))
        check(mm2.get("max_output_tokens") == 2222, "没覆盖的仍用上游值",
              str(mm2.get("max_output_tokens")))
        http("POST", base + "/admin/api/config", {"model_caps": {}})

        print("== 32. 网关自述（下游据此知道网关能力）==")
        code, hdrs, txt = http("GET", base + "/v1/gateway",
                               headers={"Authorization": "Bearer " + key})
        gi = json.loads(txt)
        check(code == 200 and gi.get("object") == "gateway", "有 /v1/gateway", str(code))
        for f in ("features", "limits", "policy", "auth", "counts",
                  "capability_sources", "endpoints"):
            check(f in gi, "自述含 " + f, str(list(gi))[:80])
        check(gi["limits"].get("max_context", 0) > 0, "自述带最大上下文",
              str(gi["limits"].get("max_context")))
        check(gi["features"].get("vision") is True, "自述声明支持视觉",
              str(gi["features"].get("vision")))

        http("POST", base + "/admin/api/config", {
            "strategy": "priority", "cooldown_seconds": 5,
            "providers": base_providers})


        print("== 33. 日志能区分「对话」和「自检」==")
        http("POST", base + "/admin/api/probe", {"name": "good"})
        time.sleep(0.3)
        code, _, txt = http("GET", base + "/admin/api/logs?kind=usage&limit=30")
        rows2 = json.loads(txt).get("rows") or []
        srcs = {r.get("source") for r in rows2}
        check("probe" in srcs, "自检有单独标记", str(srcs))
        check("chat" in srcs, "对话有单独标记", str(srcs))
        probe_row = next((r for r in rows2 if r.get("source") == "probe"), {})
        check(bool(probe_row.get("upstream_model")), "自检也记了上游模型",
              str(probe_row.get("upstream_model")))
        check("first_byte_ms" in probe_row, "自检也记了首字耗时",
              str(probe_row.get("first_byte_ms")))

    finally:
        try:
            proc.terminate()
            proc.wait(timeout=6)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        log_fh.close()
        up.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if FAILS:
        print(f"失败 {len(FAILS)} 项：")
        for f in FAILS:
            print("  -", f)
        return 1
    print(f"全部通过（{PASSED} 项）— 网关端到端行为正常")
    return 0


if __name__ == "__main__":
    sys.exit(main())
