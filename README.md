# AI 网关（飞牛 fnOS 应用）

把多个模型服务商聚合成一个 **OpenAI 兼容**接口，跑在自己的 NAS 上。
纯 Python 标准库，零第三方依赖，零 Docker。

```
你的客户端（Cherry Studio / OpenCode / 脚本）
        │  OpenAI 协议
        ▼
   ┌──────────────────┐
   │  AI 网关 :18787  │
   └────────┬─────────┘
            │ 按协议适配 + 失败自动切换
   ┌────────┼────────┬─────────┬──────────┐
   ▼        ▼        ▼         ▼          ▼
 火山方舟  NVIDIA  OpenRouter  Anthropic  opencode(本地)
```

## 特性

| 能力 | 说明 |
|---|---|
| **多上游聚合** | 一个接口后面挂任意多个上游，失败自动换下一家 |
| **四种协议** | OpenAI Chat / OpenAI Responses / Anthropic / Gemini，对客户端永远只说 OpenAI |
| **多模型** | 每个上游可挂多个模型，客户端点哪个名就用哪个 |
| **免费额度硬卡** | 给上游设额度，用尽自动停用，**绝不静默转付费** |
| **访问令牌** | 一人一把 `sk-fnos-*`，可单独停用、限额、查看用量 |
| **用量统计** | 按天 / 按上游汇总请求数、tokens、花费、成功率、平均耗时 |
| **调用日志** | 每次请求一行，成功失败都记，含错误原因 |
| **出站脱敏** | 可把消息里的品牌词替换掉再发给上游 |
| **模型能力** | 上下文长度、是否支持图片、支持哪些思考档位，可逐条覆盖 |
| **策略控制** | 黑白名单、模型映射、提示词模式、历史轮数、超时、出站 UA |
| **OpenCode 托管** | 自动安装/启停本地 opencode，走命令行模式用免费模型 |
| **代理** | 全局或按上游单独设 HTTP/SOCKS5（含 SOCKS5 自实现隧道） |

## 安装

1. 从 [Releases](../../releases) 下载 `.fpk`
2. 飞牛 → 应用中心 → 手动安装 → 选文件
3. 打开桌面图标，或访问 `http://<飞牛IP>:18787/admin`

端口固定 **18787**（避开 8080/3000/8000/8090/11434 等常见端口）。

## 接入客户端

| 项 | 值 |
|---|---|
| Base URL | `http://<飞牛IP>:18787/v1` |
| API Key | 主口令，或「访问令牌」里建的 `sk-fnos-*` |
| 模型 | 面板「概览 → 接入地址」里列出的任意一个 |

### 客户端会看到哪些模型

`GET /v1/models` **只返回现在真能调用的**：上游已启用、有密钥、免费额度没用尽。
停用或缺密钥的上游，它的模型不会出现在客户端里，免得你点了一堆都 502。

每条都带能力信息，客户端可以直接用：

```json
{
  "id": "opencode/glm-5.3-flash-free",
  "object": "model",
  "owned_by": "opencode-free",

  "context_length": 200000,
  "context_window": 200000,
  "max_context_length": 200000,

  "max_output_tokens": 65536,
  "max_tokens": 65536,

  "supports_vision": true,
  "vision": true,
  "supports_reasoning": true,
  "reasoning_efforts": ["off", "low", "medium", "high"],

  "capabilities": {
    "vision": true,
    "reasoning": ["off", "low", "medium", "high"],
    "context": 200000,
    "max_output": 65536
  },
  "model_info": {
    "context_size": 200000,
    "max_output": 65536,
    "support_vision": true,
    "support_reasoning": true,
    "support_function_call": true
  }
}
```

同一份能力用多套字段名一起发（`context_length` / `context_window`、
`max_output_tokens` / `max_tokens`、`supports_vision` / `capabilities.vision`、
以及 NewAPI 风格的 `model_info`），客户端认哪个都能读到。

数值来自三层，越靠前越优先：

| 来源 | 怎么来的 | 标签 |
|---|---|---|
| **手动** | 你在「模型」页填的 | `手动` |
| **上游** | 网关问上游 `/models` 拿到的真实值，缓存 12 小时 | `上游` |
| **内置** | 内置能力表按模型名推断 | `内置` |
| **未知** | 都匹配不上，按 32K / 4K 保守处理 | `未知` |

网关启动时会自动探测一次（缓存过期才探），也可以在「模型」页点「探测上游能力」手动触发。
能识别的上游字段：`context_length` / `context_window` / `top_provider.max_completion_tokens` /
`architecture.input_modalities`（OpenRouter 风格）、`model_info.context_size`（NewAPI 风格）等。

## 网关自述（让下游知道网关）

`GET /v1/gateway` 返回网关自己的能力和限制，客户端不用猜：

```json
{
  "object": "gateway",
  "name": "fnos-ai-gateway",
  "features": { "stream": true, "stream_passthrough": true, "vision": true,
                "reasoning": true, "tool_calls": true, "cors": true },
  "limits": { "max_context": 1000000, "max_output": 128000,
              "timeout_first_byte": 120, "timeout_stream_idle": 300 },
  "policy": { "strategy": "priority", "effort_default": "max",
              "system_mode": "passthrough", "sanitize": false },
  "auth": { "scheme": "bearer", "master": true, "tokens": 2 },
  "counts": { "models": 12, "providers": 5, "providers_enabled": 3 },
  "capability_sources": { "manual": 0, "upstream": 9, "builtin": 3, "guess": 0 }
}
```

- `GET /v1/models/{id}` — 查单个
- `GET /v1/models?all=1` — 管理员用，连不可用的也列，并给出 `unavailable_reason`

> ⚠️ **外部客户端一律用直连地址**。桌面图标走的是飞牛统一网关
> `/app/ai-gateway/v1`，那条路要 NAS 登录 Cookie，只有在本机浏览器里有效。
> 接口已带 CORS 头与 OPTIONS 预检，Open WebUI / LobeChat 这类浏览器端也能直连。

## 配置上游

打开「上游」页，每张卡片是一个服务商。改完点 **保存这张** 只写回这一张。

| 字段 | 说明 |
|---|---|
| 接口地址 | OpenAI 兼容根地址，结尾不带斜杠 |
| 默认模型 | 客户端不点名时用这个 |
| 更多模型 | 逗号分隔，客户端可以直接点名 |
| 优先级 | 越小越先试 |
| 类型 | `http`（HTTP 上游）/ `opencode`（本地） |
| 协议 | OpenAI Chat / Responses / Anthropic / Gemini |
| 密钥 | 填了写进 `secrets.json`（600 权限），留空 = 不改 |
| 控制台地址 | 留空会按接口域名自动识别，「打开」按钮直达 |
| 免费额度 | 0 = 不限；填了用尽自动停用 |
| 单独代理 | 留空用全局，可填 `socks5://host:port` |

### 内置预设

首次安装自带 14 个上游模板（默认只开 `volc-ark`）：

`volc-ark` `huawei-maas` `siliconflow` `tencent-tokenhub` `baidu-qianfan`
`deepseek` `nvidia-nim` `openrouter` 系列 `opencode-free` `opencode-zen` …

点卡片里的「**探测这个上游有哪些模型**」可以列出它真实支持的模型，
并标出哪些免费，一键填入。

### 一个上游挂多个模型

```
默认模型   deepseek-v4.1-flash
更多模型   deepseek-v4.1-pro, glm-5.3-flash, kimi-k2.5
```

客户端点名 `glm-5.3-flash` → 网关优先选这个上游，并把该模型名发过去；
没点名 → 用默认模型，按优先级/价格排序。

### 点名的模型一定生效

客户端点名了模型，网关就**只发给真能处理它的上游**，绝不悄悄换成默认模型：

| 情况 | 行为 |
|---|---|
| 名字在某个上游的模型列表里 | 用它，并把该名字发给上游 |
| 名字是 `deepseek-v4.1-flash-free` 这种省略前缀的 | 自动匹配到 `opencode/deepseek-v4.1-flash-free` |
| 哪个上游都不认 | **400 报错**，列出所有可用模型 |
| 上游卡片勾了「允许任意模型名」 | 原样透传，不校验 |

响应头 `X-Gateway-Upstream-Model` 会告诉你上游真正收到了什么模型名，
日志里也有 `上游模型` 一列，方便核对。

## OpenCode 免费模型

`opencode` 类型的上游走**本地命令行**（`opencode run`），这是唯一能用
免费模型的入口——官方限定免费额度只对客户端本体开放，走 `serve` 的 HTTP API
会被 429。

设置页「OpenCode 运行时」两步：

1. **一键安装** → 装 `@opencode/cli`（v2，可执行文件叫 `opencode2`）
2. **试一句** → 真跑一次，立刻知道通不通

> 命令行模式下**不需要启动服务，也不需要登录**。面板上「运行」「登录」两个标签
> 会直接显示「不需要」。`opencode2` 是自带运行时的单体二进制，没装 Node 也能跑。
> 只有「服务端 API」模式才需要 serve 在跑。

> 关键细节：网关给子进程的**环境是干净的**（只给 HOME/XDG/PATH/TERM/TMPDIR）。
> 把网关自己的环境变量（`PORT`、代理等）传下去会让 opencode 连不上服务器，
> 表现为 `ECONNREFUSED` 而手动跑同样的命令却正常。

## 模型与档位

「模型」页列出所有上游的模型，带三项能力：

| 能力 | 用途 |
|---|---|
| **上下文** | 超出窗口会自动丢最老的对话，保住 system 和最后一问 |
| **图片** | 不支持视觉的模型收到图片会直接 400 说明原因，不会瞎发 |
| **思考档位** | off / low / medium / high / max，不支持时落到不高于它的最高档 |

内置能力表覆盖 DeepSeek / GLM / Kimi / Qwen / Claude / GPT / Gemini / Llama 等常见模型；
表里没有的标「未知」，按 32K 保守处理，可以在表格里手填覆盖。

档位策略三选一：

- **客户端优先**（默认）— 客户端没传才补网关档位
- **网关优先** — 一律改写成网关档位，但尊重客户端的 `off`
- **网关优先（强制）** — 连 `off` 也覆盖成开启

## 高级策略

「高级」页集中放这些开关：

| 项 | 说明 |
|---|---|
| 短请求超时 | 探测 / 列模型这类请求 |
| 聊天首字节超时 | 等第一个字节最多多久 |
| 流空闲超时 | 流式两段之间最长间隔 |
| 出站 User-Agent | 有些上游按 UA 识别客户端 |
| 模型黑名单 | 命中直接拒绝，可加 `cn:` / `global:` 前缀 |
| 模型白名单 | 非空时只放行名单内 |
| 模型映射 | `客户端名=真实名`，响应仍回显客户端原名 |
| 系统提示词模式 | passthrough / default / append / prepend / drop |
| 提示词文件 | 填了就以文件内容为准，改文件即生效 |
| 只保留最近 N 轮 | 出站前丢掉更早对话，system 全留 |
| 记录调用来源 | 日志里显示 IP 与 UA |

## 统计

「统计」页给出：

- 汇总卡：请求总数 / 输入 tokens / 输出 tokens / 花费
- **按天**：日期、请求、成功、tokens、花费
- **按上游**：请求、成功率、tokens、花费、平均耗时

数据来自本地 SQLite（`usage.db`），失败的请求也记，但只统计成功的 tokens。

「日志」页是**请求事件表**，一行一次调用：

时间 / 结果（含 HTTP 码）/ 模型 / 令牌 / 思考档位 / 请求类型（SSE 或 JSON）/
**首字延迟** / 总耗时 / **生成速度 t/s** / 上下行 tokens / **缓存命中率** /
来源 IP / 客户端 UA / 请求 ID

下面还有**错误汇总**：按「上游 × 错误类型」聚合，一眼看出谁在限流、谁连不上。

## 出站指纹脱敏

「设置 → 出站指纹脱敏」：把消息里的品牌词替换成中性词再发给上游。

| 项 | 说明 |
|---|---|
| 开关 | 关闭时完全不动内容 |
| 作用域 | `all` 所有角色 / `user` / `assistant` / `system` |
| 替换为 | 默认「该服务」 |
| 品牌词 | 顿号、逗号或换行分隔，如 `OpenCode、Claude Code` |

只影响发往上游的内容，不改你在客户端看到的历史。

## 鉴权模型

三层，从高到低：

1. **主口令**（设置页填）— 管理员操作 + `/v1` 都可用
2. **访问令牌**（`sk-fnos-*`）— 只能调 `/v1`，可单独停用/限额
3. **全开放**（两者都没设）— 任何人能访问就能用，面板会黄字警告

`/admin/api/*` 只认主口令或飞牛管理员身份。

## 密钥落盘

优先级从高到低：

1. `@appdata/ai-gateway/secrets.json`（面板里填的，600 权限）
2. 环境变量（`run.sh` 里，向导/旧版）
3. `providers.json` 内联 `api_key`

面板留空保存 = 不改动已有密钥。

## 目录结构

```
manifest               应用清单（名称/版本/端口）
app/gateway.py         网关主程序（HTTP 服务 + 路由 + 适配）
app/opencode_*.py      opencode 运行时与上游
app/proxy_support.py   HTTP/SOCKS5 代理（含 SOCKS5 自实现隧道）
app/web/               界面（admin.html / app.css / app.js）
app/providers.json     上游配置
app/providers.d/       可选的额外上游（一个平台一个文件）
cmd/                   9 个生命周期脚本
wizard/                安装/配置向导（无输入项，配置全在面板）
config/                privilege / resource
scripts/               结构自检 + 端到端冒烟测试
```

## 开发

```bash
# 结构自检（含前端语法、换行符、图标尺寸）
python3 scripts/check_structure.py

# 端到端测试（自带假上游，13 组 35 项）
python3 scripts/smoke_test.py

# 打包（需要官方 fnpack）
./build.sh
```

CI 每次推送都会跑上面两个脚本，通过后产出 `.fpk`。

## 已知限制

- opencode 免费模型有**每日配额**，用尽后需等重置（服务端会返回 `Retry-After`）
- opencode 走命令行，每次请求起一个进程，冷启动 1~3 秒
- 非 OpenAI 协议没有真流式，网关会先拿完整结果再按 SSE 分块吐出
- 统计只按本地账本算，不校验上游账单

## 许可

MIT
