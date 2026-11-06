# DeepSeek 网关 · 飞牛 fnOS 原生应用（fpk，无 Docker）

把 **火山方舟 / 硅基流动 / OpenRouter / 腾讯 TokenHub / 百度千帆 / DeepSeek 官方** 等多个上游，
聚合成 **一个 OpenAI 兼容端点**，以飞牛原生应用的形式安装。

**纯 Python 标准库实现**：不用 Docker、不用镜像、不装任何第三方包（`http.server` + `urllib` + `sqlite3`）。
装完就是一个 `python3 gateway.py` 进程，飞牛应用中心可见、桌面有图标、点开就是网页面板。

> 和 Docker 版的取舍：这个版本安装快、占内存小（约 25 MB）、没有镜像拉取和构建环节；
> 代价是没有容器隔离，直接以应用专用用户身份跑在宿主机上。

---

## 默认配置：只跑免费，不花钱

| 层 | 花费 |
|---|---|
| 网关程序本身（跑在你的飞牛上） | **免费**。零第三方包，一个 Python 进程 |
| GitHub Actions 编译 | **免费**。私有仓库 2000 分钟/月，实测一次 15 秒 |
| 调用的上游模型 | **默认只启用免费上游，付费全部关闭** |

默认 `providers.json` 里只有 `volc-ark`（火山方舟，每模型 50 万 tokens 免费额度）是 `enabled: true`，
其余 9 个上游全部 `enabled: false`。想用付费的，自己改开关。

**关键：免费额度用尽不会静默转付费。** 带 `free_only: true` 的上游会由网关自己累计 token 用量，
达到 `free_quota_tokens` 就自动停用、切下一家，面板上标红「额度用尽」。钱不会悄悄流走。

---

## 最快路径：从 GitHub Actions 直接下载 .fpk

**不需要在本机装 fnpack。** 仓库自带 workflow，每次 push 自动跑测试并打包：

| 想拿什么 | 怎么做 |
|---|---|
| 最新构建 | **Actions** → 点最新一次 `build-fpk` → 页面底部 **Artifacts** → 下载 `fpk` |
| 正式版本 | `git tag v1.0.0 && git push origin v1.0.0` → **Releases** 里已挂好 `.fpk` |
| 手动触发 | Actions → `build-fpk` → **Run workflow** |

拿到 `.fpk` 后：飞牛「应用中心」→ 右上角「手动安装」→ 选文件即可。

> CI 会先跑两道关，任一失败就不出包：
> 1. `scripts/check_structure.py` — 结构自检（manifest 字段、JSON、图标尺寸、入口与端口一致性、脚本可编译）
> 2. `scripts/smoke_test.py` — **端到端冒烟测试**：起一个假上游，实测非流式、流式 SSE、
>    故障切换、严格 model 校验跳过、用量记账、启停、热重载
>
> 若 runner 访问 `static2.fnnas.com` 失败，可在 **Settings → Variables** 加 `FNPACK_URL` 指向镜像地址。

---

## 一、目录结构

```
<repo root>/
├── .github/workflows/build-fpk.yml  # CI：结构自检 → 冒烟测试 → 打包 → 发版
├── scripts/
│   ├── check_structure.py       # 打包前结构自检
│   └── smoke_test.py            # 端到端冒烟测试（纯标准库，本地也能跑）
├── manifest                     # 应用元数据（appname/版本/端口/桌面入口）
├── ICON.PNG  ICON_256.PNG       # 包图标 64 / 256
├── app/
│   ├── gateway.py               # 网关本体（单文件，零依赖）
│   ├── opencode_upstream.py     # type=opencode 上游：驱动本地 opencode serve
│   ├── opencode-serve.sh        # 起 OpenCode server 的辅助脚本
│   ├── providers.json           # 上游池默认模板（免费优先，付费默认关闭）
│   ├── providers.d/README.md    # 新增上游的模板与字段说明
│   ├── service.sh               # 生成 run.sh + 启停控制
│   └── ui/
│       ├── config               # 桌面入口：iframe 打开 http://<nas>:18787/admin
│       └── images/icon_64.png, icon_256.png
├── cmd/                         # 生命周期脚本（9 个）
├── config/
│   ├── privilege                # 以专用包用户运行
│   └── resource                 # 原生应用，无需 docker-project
├── wizard/
│   ├── install                  # 安装向导：密钥 / 策略 / 代理
│   └── config                   # 应用设置页：随时改
├── build.sh / build.ps1         # 本机打包（可选）
├── LICENSE
└── README.md
```

运行时目录（安装后由飞牛创建）：

| 用途 | 路径 |
|---|---|
| 上游配置 | `/vol{n}/@appconf/deepseek-gateway/providers.json` |
| 数据（用量库） | `/vol{n}/@appdata/deepseek-gateway/usage.db` |
| 运行脚本（含密钥，700） | `/vol{n}/@appdata/deepseek-gateway/run.sh` |
| 运行日志 | `/vol{n}/@appdata/deepseek-gateway/app.log` |

---

## 二、本机打包（可选）

> 走 Actions 的话跳过本节。

`.fpk` 必须用飞牛官方工具 `fnpack` 生成：

| 平台 | 下载 |
|---|---|
| Windows x86 | https://static2.fnnas.com/fnpack/fnpack-1.2.3-windows-amd64 |
| Linux x86 | https://static2.fnnas.com/fnpack/fnpack-1.2.3-linux-amd64 |
| Linux ARM | https://static2.fnnas.com/fnpack/fnpack-1.2.3-linux-arm64 |
| macOS ARM | https://static2.fnnas.com/fnpack/fnpack-1.2.3-darwin-arm64 |

Windows 下载下来没有后缀，重命名为 `fnpack.exe` 放进项目根目录，然后：

```powershell
.\build.ps1          # 或 .\fnpack.exe build
```

Linux / macOS：

```bash
chmod +x fnpack-1.2.3-linux-amd64
sudo mv fnpack-1.2.3-linux-amd64 /usr/local/bin/fnpack
./build.sh           # 或 fnpack build
```

安装：

- 应用中心 → 右上角「手动安装」→ 选 `.fpk`
- 或 SSH：`appcenter-cli install-fpk deepseek-gateway.fpk`

---

## 三、安装时不问任何东西

安装向导只有一页说明文字，**没有任何输入框**。所有配置都在装完后的**设置页**里做。

装完桌面会出现**两个图标**：

| 图标 | 走哪条路 | 登录 |
|---|---|---|
| **DeepSeek 网关** | 飞牛统一网关（`/app/deepseek-gateway`） | **直接用 NAS 账号，免口令** |
| DeepSeek 网关（直连端口） | `http://<nas>:18787/admin` | 需要口令（若已设置） |

第一个是主入口：飞牛会先校验你的 NAS 登录态，再把请求连同
`X-Trim-Userid / X-Trim-Username / X-Trim-Isadmin` 转发给网关的 Unix Socket，
所以**打开就是已登录状态**，不用再输密码。第二个是兜底——万一统一网关没生效，
从端口也能进。

> 写操作（保存配置）在网关模式下要求 **NAS 管理员**身份；普通用户只能看状态。

---

## 四、装完怎么用

### 界面

两个页面，左侧导航切换：

| 页面 | 内容 |
|---|---|
| **概览** `/admin` | 四个统计卡（请求/花费/可用上游/策略）、**接入信息**（直连 / 本机 / 网关三行，各带复制按钮）、上游状态表 |
| **设置** `/settings` | 全局（口令/策略/冷却/代理）、OpenCode 运行时、访问令牌、上游卡片（每张带字段说明） |

浅色主题，字段下方都有中文提示，不需要懂 JSON 也能配。

### 日志

侧栏「**日志**」页分两块：

| 块 | 内容 |
|---|---|
| **调用日志** | 每次请求一行：时间 / 上游 / 模型 / 令牌 / 成功失败 / 输入输出 tokens / 花费 / 耗时 / 错误。可切 100/300/1000 条，可开 5 秒自动刷新 |
| **网关日志** | `@appdata/deepseek-gateway/app.log` 的尾部，页面会显示实际路径 |

调用日志来自本地 SQLite（`usage` 表），失败的请求也会记一行，错误原因截断显示、鼠标悬停看全文。
查「某个上游到底有没有被调用」「为什么这次失败」直接看这里，不用猜。

### 桌面打开面板

飞牛桌面「**DeepSeek 网关**」→ 打开 `http://<nas>:18787/admin`：

- 每个上游：状态、模型名、量化档位、输入/输出单价
- 24h 请求数、tokens、花费、最近错误
- **启用 / 停用**（写 `overrides.json`，不动 `providers.json`）
- **重载配置**（改完 JSON 点一下，不用重启）
- **自检**（真发一次极小请求，直接显示返回样例或报错）

### ⚙ 设置页（`/settings`）

状态页右上角点「⚙ 设置」，或直接开 `http://<nas>:18787/settings`。

**全局**：访问口令（留空=免口令，可「清除」）、排序策略、失败冷却、**全局代理**
（协议下拉可选 **HTTP / HTTPS / SOCKS5 / SOCKS5-代理解析域名**，地址填 `主机:端口`）。
SOCKS5 由标准库自实现隧道，不依赖 PySocks。填完点旁边的「**测试**」——
它会真的走这条代理请求一次（默认 `https://api.deepseek.com/`），回报 HTTP 状态和耗时，
失败时直接给错误原因，不用靠猜。

保存后提示会带上「**磁盘：代理=… 策略=…**」，那是从 settings.json 重新读出来的真实值，
用来确认"到底存没存"。

**每个上游一张卡片**，全部可视化编辑：

| 字段 | 说明 |
|---|---|
| 启用 | 开关 |
| 类型 | `http`（OpenAI 兼容）/ `opencode`（驱动本地 server） |
| 接口 | base_url |
| 模型 | 模型 ID |
| 校验串 | 响应 `model` 必须包含的字符串，留空=不校验 |
| 优先级 | 越小越先试 |
| 免费额度 | 勾选 + tokens 数（0=不限）；用尽自动停用，绝不转付费 |
| 价格/M | 输入 / 输出单价，用于成本估算和「省钱优先」排序 |
| 代理 | **单个上游可单独指定代理**，留空则用全局 |
| Key 变量 | 密钥对应的环境变量名 |
| Key | 直接粘贴；占位文字显示当前来源（面板/向导/配置/无/本地） |
| 控制台 | 「**去获取 Key ↗**」按钮跳转的地址 |
| 备注 | 面板上显示的一行说明 |

底部按钮：

| 按钮 | 作用 |
|---|---|
| **保存全部** | 一次写完 providers.json + secrets.json + settings.json 并热重载 |
| **+ OpenAI 兼容接口** | 插入任意 OpenAI 兼容服务的模板（填 base_url / 模型 / Key 即可） |
| **+ OpenRouter** | 插入 OpenRouter 模板（一个 Key 打通 40+ provider） |
| **+ 本地 Ollama** | 插入本地 Ollama/vLLM/LM Studio 模板（**免密钥**，`no_auth: true`） |
| **+ 空白上游** | 从零填 |
| 每张卡片 | **自检** / **删除** |

**免密钥上游**：勾选卡片里的「免密钥」后，即使没有 Key 也会参与路由——本地 Ollama、vLLM、
LM Studio 这类不需要鉴权的服务用这个。面板上密钥来源会显示为「免密钥」。

> **OpenCode 免费模型的限制**：`opencode-free` 这类免费池只在 OpenCode 自己的界面里有效，
> 无头调用（`opencode serve` + API）会被限流，服务端返回
> `Free usage limit reached`。网关会识别这种情况并把该上游自动冷却 1 小时，
> 不再拖住后续请求。想长期用，请配一个有 Key 的上游。

### 上游协议：不只 OpenAI 兼容

卡片里的「协议」决定网关用哪种接口跟这个上游说话。**客户端那一侧永远是 OpenAI 形状**，转换在网关内部做。

| 协议 | 请求 | 鉴权头 | 响应字段 |
|---|---|---|---|
| OpenAI Chat Completions（默认） | `POST {base}/chat/completions` | `Authorization: Bearer` | `choices[0].message.content` |
| OpenAI Responses | `POST {base}/responses` | `Authorization: Bearer` | `output[].content[].text` |
| Anthropic Messages | `POST {base}/v1/messages` | `x-api-key` + `anthropic-version` | `content[].text` |
| Google Gemini | `POST {base}/models/{model}:generateContent` | `x-goog-api-key` | `candidates[].content.parts[].text` |

后三种没有真流式：网关先拿到完整结果，再按 SSE 分块吐给客户端（`X-Gateway-Stream: buffered`），
客户端无感。状态表里每个上游后面会标出它用的协议。

### 不只 DeepSeek：一个上游挂多个模型

每个上游除了「默认模型」，还有「**更多模型**」一行（逗号分隔）。网关按客户端请求里的 `model` 路由：

- 客户端点名了某个上游的模型 → 优先用那个上游，并把该模型名发过去
- 没点名 / 没人认领 → 用各上游的默认模型，按优先级或价格排序

**「探测这个上游有哪些模型」**按钮会真的去问一遍上游（`GET /models`，OpenRouter 这类还会读
`pricing` 判断是否免费），然后告诉你「共 N 个，免费 M 个」，点一下就能把免费模型批量填进去。

所以 OpenRouter 一个 Key 可以挂上它全部免费模型，硅基、NVIDIA 同理——不限于 DeepSeek。

### 给别人用：访问令牌（多把 Key）

设置页有「**访问令牌**」区块。别把主口令发给别人，给每人建一把独立令牌：

| 能力 | 说明 |
|---|---|
| 独立 Key | 每把形如 `sk-fnos-xxxxxxxx`，**创建时只显示一次** |
| 单独停用 | 停掉某个人，其他人不受影响 |
| 单独限额 | 填 `quota_tokens`，超出返回 **429**；0 = 不限 |
| 用量统计 | 按令牌累计 token，面板直接看 |
| 查看完整 Key | 列表默认掩码，点「**显示**」可看完整值（仅管理员） |
| 复制完整 Key | 点「**复制**」直接复制真值——**别手选掩码**，那是 `****` 打码的 |
| 验证 Key | 令牌区有「验证 Key」输入框：粘一把 Key 立刻告诉你是主口令/哪个令牌/是否停用/额度够不够 |

鉴权优先级：**主口令** > **任意启用的令牌**。当「主口令为空」且「没有启用的令牌」时进入
**全开放模式**（局域网自用，谁都能调），面板会用黄字警告你。

给别人的接入信息就是状态页「接入信息」里那几行：**直连地址** + 模型名 + curl 示例，
把 Key 换成他们自己那把即可。

> ⚠️ **外部客户端一律用直连地址** `http://<飞牛IP>:18787/v1`。
> 桌面图标走的是飞牛统一网关 `/app/deepseek-gateway/v1`，那条路要 **NAS 登录 Cookie**——
> 只有在本机浏览器里打开时才有效，Cherry Studio / OpenCode / 脚本带 Bearer Key 是进不去的
> （会返回登录页 HTML，表现为 `did not answer with JSON`）。
>
> 接口已带 CORS 头与 OPTIONS 预检，Open WebUI / LobeChat 这类浏览器端也能直连。

密钥落盘在 `@appdata/deepseek-gateway/secrets.json`（600），不会出现在 `ps` 里。

### 客户端接入

| 项 | 值 |
|---|---|
| Base URL | `http://<飞牛IP>:18787/v1`（**直连端口**） |
| API Key | 向导里设的口令（留空则随便填） |
| Model | `deepseek/deepseek-v4.1-flash-1017` 或 `deepseek-flash` |

```bash
curl http://192.168.1.10:18787/v1/chat/completions \
  -H "Authorization: Bearer 你的口令" \
  -H "Content-Type: application/json" \
  -d '{"model":"deepseek/deepseek-v4.1-flash-1017",
       "messages":[{"role":"user","content":"用一句话解释 MoE"}]}'
```

省钱模式（单次请求）：加请求头 `X-Route-Strategy: cheapest`。

### OpenCode / Cline 这类 Agent 怎么接

OpenCode 支持自定义 OpenAI 兼容 provider，把网关当成一个 provider 加进去就行：

`~/.config/opencode/opencode.json`（或项目根的 `opencode.json`）

```json
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "fnos-gateway": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "飞牛网关",
      "options": {
        "baseURL": "http://192.168.1.10:18787/v1",
        "apiKey": "sk-fnos-change-me"
      },
      "models": {
        "deepseek-v4.1-flash": { "name": "DeepSeek V4.1 Flash（网关）" }
      }
    }
  },
  "model": "fnos-gateway/deepseek-v4.1-flash"
}
```

`apiKey` 填安装时设的访问口令（没设就随便填一个非空字符串）。
模型名可以随便写——**网关会忽略客户端传的 `model`，统一映射到各上游自己的模型 ID**。

> ⚠️ 反过来不行：OpenCode 自带的免费模型 `opencode/deepseek-v4.1-flash-free` **只能在其客户端内使用**。
> 从外部调用 `https://opencode.ai/inference/openai/v1/chat/completions` 会返回
> `403 FreeTierError: OpenCode's free tier can only be used from within OpenCode`。
> 所以它没法作为本网关的上游；想用它的免费额度，就在 OpenCode 里直接用那个模型。

---

## 五、上游池：改已有的 / 加新的

主配置在 `/vol{n}/@appconf/deepseek-gateway/providers.json`，改完回面板点「重载配置」：

```json
{
  "settings": {
    "strategy": "priority",
    "request_timeout": 180
  },
  "providers": [
    {
      "name": "or-morph",
      "enabled": true,
      "priority": 30,
      "base_url": "https://openrouter.ai/api/v1",
      "api_key_env": "OPENROUTER_API_KEY",
      "model": "deepseek/deepseek-v4.1-flash-1017",
      "model_contains": "v4.1-flash",
      "strict_model": true,
      "extra": { "provider": { "only": ["Morph"], "allow_fallbacks": false } },
      "price_in": 0.022,
      "price_out": 0.40,
      "quant": "fp8",
      "note": "性价比最高：便宜且 fp8"
    }
  ]
}
```

字段说明：

| 字段 | 作用 |
|---|---|
| `priority` | 越小越先试（`strategy=priority` 时生效） |
| `api_key_env` | 从环境变量取密钥，**不要把 key 写进这个文件** |
| `model_contains` | 校验响应里的 `model` 字段，防上游静默换模型 |
| `strict_model` | `true` = 校验不过就换下一家；`false` = 只加个警告头 |
| `extra` | 原样并入请求体（OpenRouter 用来钉死某个 provider） |
| `protocol` | 用哪种接口跟上游说话：空 = OpenAI Chat Completions；`responses` / `anthropic` / `gemini` 见下 |
| `models` | 该上游额外支持的模型列表。客户端请求里点哪个名就用哪个 |
| `price_in/price_out` | USD / 百万 tokens，用于成本估算和 `cheapest` 排序 |
| `free_only` | `true` = 计入免费额度硬卡；额度用尽自动停用，**绝不转付费** |
| `free_quota_tokens` | 免费额度总量（tokens）；`free_only=true` 且 >0 时生效 |

### 怎么加一个新的免费上游（不用改主配置）

在 `providers.d/` 里丢一个 json 就行：

```
/vol{n}/@appconf/deepseek-gateway/providers.d/my-provider.json
```

```json
{
  "providers": [
    {
      "name": "my-free-provider",
      "enabled": true,
      "priority": 15,
      "base_url": "https://api.example.com/v1",
      "api_key_env": "MY_PROVIDER_KEY",
      "model": "deepseek-v4.1-flash",
      "model_contains": "deepseek",
      "free_only": true,
      "free_quota_tokens": 1000000,
      "price_in": 0.15,
      "price_out": 0.6,
      "quant": "fp8",
      "note": "某某云：新用户 100 万 tokens"
    }
  ]
}
```

密钥写进 `@appdata/deepseek-gateway/secrets.json`（没有就新建，**不会被应用设置覆盖**）：

```json
{ "MY_PROVIDER_KEY": "sk-xxxx" }
```

然后面板点「重载配置」即可。规则：同名 `name` 覆盖主配置里的那一条，其余按 `priority` 参与排序；
某个文件解析失败只跳过它自己，不影响其他。

### 把 OpenCode 的免费档接进来（type: "opencode"）

OpenCode 的免费 DeepSeek **不能直接当 API 调**（外部请求返回 `403 FreeTierError`），
但它自带一个**官方文档化**的无头 server，网关可以驱动它——请求真的从 OpenCode 内部发出，不伪造任何东西。

**不需要自己装 Node 和 OpenCode**——设置页里有「**OpenCode 运行时**」卡片：

1. 点「**一键安装**」→ 把 Node + opencode 装进 `@appdata/deepseek-gateway/node|npm`（**不碰系统包**，删目录即卸载）
2. 点「**启动**」→ 在私有工作目录里跑 `opencode serve`
3. 卡片上直接看状态（运行中 / 未运行 / 未安装）、pid、运行时长、重启次数、实时日志

**看门狗**：进程意外退出会自动重启（最多连续 5 次，带退避）。
**按需启动**：请求打到 `type=opencode` 的上游时，如果服务没在跑会先尝试拉起。

也可以在卡片里指定：
- **二进制**：留空 = 自动查找（私有安装 → PATH）；也可以指向你自己装的 opencode
- **端口**：默认 4096
- **工作目录**：留空 = 应用数据目录下的 `opencode-workspace`（空目录，防止 Agent 误动你的文件）
- **自动重启**：开关

> 一键安装会把 npm 的 **HOME / 缓存 / 日志 / 全局前缀**全部重定向到应用数据目录
> （`@appdata/deepseek-gateway/home`、`npm-cache`、`npm`），所以不会去写 `/home/<appuser>/.npm`
> ——那是应用专用用户通常无权限的地方，也是"权限被拒"报错的根因。
>
> 一键安装依赖网络（npm 源）。装不上时日志会写清楚是下载失败还是 npm 失败，
> 也可以 `NODE_VER=v20.18.0` 换个 Node 版本重试，或手动把 opencode 装好后在「二进制」里填路径。

**然后启用上游**（`providers.json` 里已经有这条，默认 `enabled: false`）：

```json
{
  "name": "opencode-free",
  "type": "opencode",
  "enabled": true,
  "priority": 5,
  "base_url": "http://127.0.0.1:4096",
  "model": "deepseek-v4.1-flash-free",
  "opencode_provider": "opencode"
}
```

回面板点「重载配置」，再点这一行的「**自检**」——通了会显示返回的样例文本。

| 字段 | 说明 |
|---|---|
| `type` | `"opencode"` 才会走这条路；默认 `"http"` |
| `base_url` | `opencode serve` 的地址 |
| `model` | `deepseek-v4.1-flash-free`（免费档） |
| `opencode_provider` | 模型所属 provider，默认 `opencode` |
| `opencode_agent` | agent 名。**默认 `plan`（只读）**——`build` 带全套写工具，容易自己循环到超时 |
| `timeout` | 默认 300 秒（agent 循环本来就慢） |
| `opencode_tools` | 可选，传给 OpenCode 的 `tools` 字段，用来收紧工具权限 |
| `opencode_extra` | 可选，原样并入 `/session/{id}/message` 请求体 |

**必须知道的代价**（这不是 bug，是这条路的本质）：

| 问题 | 说明 |
|---|---|
| 慢 | 每次请求跑一遍 Agent 循环，几秒到几十秒；真 API 是毫秒级 |
| 无真流式 | 网关只能「缓冲后分块」发给你，响应头会带 `X-Gateway-Stream: buffered` |
| 会话即弃 | 每个请求建一个会话、用完删掉，所以**没有多轮上下文缓存**（上下文由客户端每次全量传） |
| 200K / 32K | 免费档上下文和输出上限，付费档才是 1M / 384K |
| 会动工具 | 它是编码 Agent，理论上可能尝试读文件/跑命令。所以务必让它在**空目录**里跑 |
| 随时失效 | 限时活动，无公布截止日；上一代 `deepseek-v4-flash-free` 就是这么下架的 |

挂了不会影响其它上游：网关会冷却它、自动切下一家，面板上点「停用」即可。

### 想用付费上游
把对应条目的 `enabled` 改 `true`，或直接在面板上点「启用」。

⚠️ **开之前先核对单价**：`price_in/price_out` 是我写死的估算值，会过期；真实账单以各平台控制台为准。
付费条目没有 `free_only`，所以网关**不会**替你把关——开了就是真花钱。

---

## 六、端口（默认 18787）

刻意避开 NAS 上最容易撞车的 `80/443/3000/5000/8000/8080/8096/8443/8888/9000/9090/11434/32400`
（Plex、Jellyfin、qBittorrent、Ollama、各类 *arr 都在这些区间），也不落在 Linux 临时端口段（32768+）。

要换端口，两处一起改：

1. `manifest` → `service_port=18787`
2. `app/ui/config` → `"port": "18787"`

服务实际监听端口由 `cmd/main` 通过 `TRIM_SERVICE_PORT` 传给 `run.sh`，不用改代码。
改完重新 `fnpack build` 再安装（或升级安装）。

---

## 七、排错

```bash
# 服务在不在跑
pgrep -af "gateway.py"

# 看日志
tail -f /vol1/@appdata/deepseek-gateway/app.log

# 手动重启
/var/apps/deepseek-gateway/cmd/main stop
/var/apps/deepseek-gateway/cmd/main start

# 直接探活
curl -s http://127.0.0.1:18787/healthz
```

| 现象 | 处理 |
|---|---|
| 安装报「未检测到 python3」 | 先装 Python 3.8+；本应用零第三方依赖，只要解释器 |
| 应用状态「未运行」 | 看 `app.log`：端口被占 → 换端口；`run.sh` 缺失 → 到应用设置里保存一次 |
| 桌面图标点开空白 | `manifest.service_port` 与 `app/ui/config` 的 port 不一致 |
| 面板全是「缺密钥」 | 向导没填，或填完没在「设置」里保存 |
| 502 所有上游失败 | 看面板「最近错误」：`HTTP 401`=key 错；`timeout`=要代理；`HTTP 404`=模型名不对 |
| 国内上游也超时 | 代理把国内域名也劫持了，确认 `run.sh` 里 `NO_PROXY` 没被改 |
| 回复质量变差 | 看命中的上游；`quant=fp4` 的（Sail / OpenInference）质量会掉，面板一键停用 |

---

## 八、本地开发

不用装任何依赖，直接跑：

```bash
# 端到端测试（自带假上游，不花一分钱、不联网）
python3 scripts/smoke_test.py

# 结构自检
python3 scripts/check_structure.py

# 手动起一个网关
CONFIG_PATH=app/providers.json DATA_DIR=./data PORT=18787 python3 app/gateway.py
```

## 九、说明与限制

- **依赖 Python 3.8+**（fnOS 自带）。零第三方包，所以没有 pip、没有编译、没有镜像。
- **单进程 + 多线程**（`ThreadingHTTPServer`）。家用/小团队够用；要抗高并发请走 Docker 版或多实例。
- **无容器隔离**：以应用专用用户（`config/privilege` 里的 `deepseek-gateway`）运行，
  端口 >1024 不需要 root；`run.sh` 700 权限，密钥不会出现在 `ps` 里。
- **免费额度会变**：面板的「24h 花费」就是用来发现"怎么突然开始花钱了"的。
- 面板与 API 走 HTTP，默认只建议局域网使用；外网访问请用飞牛远程访问 / Tailscale / 反代 + HTTPS，并设置口令。
