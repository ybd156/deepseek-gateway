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

## OpenCode 免费模型

`opencode` 类型的上游走**本地命令行**（`opencode run`），这是唯一能用
免费模型的入口——官方限定免费额度只对客户端本体开放，走 `serve` 的 HTTP API
会被 429。

设置页「OpenCode 运行时」三步：

1. **一键安装** → 自动装 Node + `@opencode/cli`（v2，可执行文件叫 `opencode2`）
2. **试一句** → 真跑一次，立刻知道通不通
3. 通了就把上游卡片勾「启用」

> 关键细节：网关给子进程的**环境是干净的**（只给 HOME/XDG/PATH/TERM/TMPDIR）。
> 把网关自己的环境变量（`PORT`、代理等）传下去会让 opencode 连不上服务器，
> 表现为 `ECONNREFUSED` 而手动跑同样的命令却正常。

## 统计

「统计」页给出：

- 汇总卡：请求总数 / 输入 tokens / 输出 tokens / 花费
- **按天**：日期、请求、成功、tokens、花费
- **按上游**：请求、成功率、tokens、花费、平均耗时

数据来自本地 SQLite（`usage.db`），失败的请求也记，但只统计成功的 tokens。

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
