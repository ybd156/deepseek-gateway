"""把 OpenCode 的本地 server 当成一个上游（type: "opencode"）。

驱动的是官方文档化的接口，不伪造任何客户端特征：
    POST   /session                 建会话
    POST   /session/{id}/message    发消息并等回复
    DELETE /session/{id}            清理

只用标准库。请先自行安装并启动：
    npm install -g opencode-ai
    opencode serve --port 4096 --hostname 127.0.0.1

已知限制（不是 bug，是这条路的本质）：
  - 每次请求都要跑一遍 Agent 循环，比真正的 chat API 慢一个数量级
  - 没有流式，只能等整段返回（网关会假装分块发给客户端）
  - 上下文 200K / 输出 32K（免费档），付费档才是 1M / 384K
  - 依赖 OpenCode 的限时免费档，随时可能失效
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request


class OpenCodeError(Exception):
    """OpenCode server 调用失败。"""


class OpenCodeClient:
    def __init__(self, base_url: str, model_id: str = "",
                 provider_id: str = "opencode", agent: str = "",
                 tools=None, timeout: int = 180,
                 extra: dict | None = None) -> None:
        self.base = base_url.rstrip("/")
        self.model_id = model_id
        self.provider_id = provider_id or "opencode"
        self.agent = agent
        self.tools = tools
        self.timeout = timeout
        self.extra = extra or {}

    # ---- 底层请求 ----
    def _req(self, method: str, path: str, body: dict | None = None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers={"Content-Type": "application/json", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "ignore")[:300]
            except Exception:
                pass
            raise OpenCodeError(f"HTTP {exc.code} {path}: {detail}") from None
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            text = str(reason).lower()
            if isinstance(reason, ConnectionRefusedError) or "refused" in text:
                raise OpenCodeError(
                    f"连不上 opencode server（{self.base}）——它没在运行。"
                    f"先在本机执行 `opencode serve --port 4096 --hostname 127.0.0.1`"
                    f"（或 app/opencode-serve.sh），再点自检。"
                    f"不想用就把这一行停用。") from None
            if "timed out" in text or "timeout" in text:
                raise OpenCodeError(
                    f"连接 {self.base} 超时：server 可能还在启动，或卡住了") from None
            raise OpenCodeError(f"请求 {self.base}{path} 失败：{reason}") from None
        except Exception as exc:                      # noqa: BLE001
            raise OpenCodeError(f"{type(exc).__name__} {path}: {exc}") from None

    # ---- 对外：一次对话 ----
    def chat(self, prompt: str) -> dict:
        """建会话 → 发消息 → 删会话，返回 OpenAI 形状的 dict。"""
        sid = ""
        try:
            session = self._req("POST", "/session", {"title": "gateway"})
            sid = str(session.get("id") or "")
            if not sid:
                raise OpenCodeError("建会话失败：响应里没有 id")

            body: dict = {"parts": [{"type": "text", "text": prompt}]}
            if self.model_id:
                body["model"] = {"providerID": self.provider_id,
                                 "modelID": self.model_id}
            if self.agent:
                body["agent"] = self.agent
            if self.tools is not None:
                body["tools"] = self.tools
            body.update(self.extra)

            try:
                resp = self._req("POST", f"/session/{sid}/message", body)
            except OpenCodeError as exc:
                if "timed out" in str(exc).lower() or "timeout" in str(exc).lower():
                    raise OpenCodeError(
                        f"OpenCode 处理超时（{self.timeout}s）：它多半卡在等工具调用权限确认，"
                        f"或模型响应很慢。看「OpenCode 运行时」的日志，或点「诊断」；"
                        f"也可以在 providers.json 里把该上游的 timeout 调大。") from None
                raise
            return self._to_openai(resp, prompt)
        finally:
            if sid:
                try:
                    self._req("DELETE", f"/session/{sid}")
                except Exception:
                    pass

    # ---- 诊断：把 server 的真实情况拉回来 ----
    def diagnose(self) -> dict:
        out: dict = {"base": self.base, "steps": []}

        def step(name, fn):
            try:
                out["steps"].append({"step": name, "ok": True, "data": fn()})
            except Exception as exc:                # noqa: BLE001
                out["steps"].append({"step": name, "ok": False,
                                     "error": f"{type(exc).__name__}: {exc}"[:300]})

        step("health", lambda: self._req("GET", "/global/health"))
        step("providers", lambda: _summarize_providers(self._req("GET", "/config/providers")))
        step("agents", lambda: _agent_names(self._req("GET", "/agent")))
        return out

    # ---- 响应转换 ----
    @staticmethod
    def _text_of(resp: dict) -> str:
        parts = resp.get("parts") or []
        texts = [str(p.get("text") or "") for p in parts
                 if isinstance(p, dict) and p.get("type") == "text"]
        return "\n".join(t for t in texts if t).strip()

    @staticmethod
    def _tokens_of(info: dict) -> tuple[int, int]:
        """尽力从 info 里挖 usage；不同版本字段名不一样。"""
        for key in ("tokens", "usage"):
            block = info.get(key)
            if not isinstance(block, dict):
                continue
            tin = (block.get("input") or block.get("prompt_tokens")
                   or block.get("input_tokens"))
            tout = (block.get("output") or block.get("completion_tokens")
                    or block.get("output_tokens"))
            if tin is not None or tout is not None:
                return int(tin or 0), int(tout or 0)
        return 0, 0

    def _to_openai(self, resp: dict, prompt: str) -> dict:
        info = resp.get("info") or {}
        text = self._text_of(resp)
        tin, tout = self._tokens_of(info)

        # 拿不到真实 usage 就按字符估一个，至少让面板/额度有数
        if tin == 0 and tout == 0:
            tin = max(1, len(prompt) // 3)
            tout = max(1, len(text) // 3)

        model = self.model_id or "opencode"
        return {
            "id": f"chatcmpl-oc-{int(time.time() * 1000)}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }],
            "usage": {
                "prompt_tokens": tin,
                "completion_tokens": tout,
                "total_tokens": tin + tout,
            },
            # 给网关自己做 model 校验用
            "_upstream_model": str(info.get("modelID") or info.get("model") or model),
        }


def _summarize_providers(data: dict) -> list:
    """把 /config/providers 压成小摘要，重点看有没有 deepseek 的模型。"""
    provs = []
    if isinstance(data, dict):
        provs = data.get("providers") or data.get("all") or []
    out = []
    for p in provs:
        if not isinstance(p, dict):
            continue
        pid = p.get("id") or p.get("providerID") or "?"
        models = p.get("models") or {}
        ids = list(models.keys()) if isinstance(models, dict) else list(models or [])
        out.append({
            "id": pid,
            "model_count": len(ids),
            "deepseek": [x for x in ids if "deepseek" in str(x).lower()][:8],
            "sample": ids[:6],
        })
    return out


def _agent_names(data) -> list:
    names = []
    items = data if isinstance(data, list) else (data or {}).get("agents") or []
    for a in items:
        if isinstance(a, dict):
            names.append(a.get("name") or a.get("id") or "?")
        else:
            names.append(str(a))
    return names[:20]
