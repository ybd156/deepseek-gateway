# DeepSeek 网关 · 飞牛 fnOS 原生应用（fpk，无 Docker）

把 **火山方舟 / 硅基流动 / OpenRouter / 腾讯 TokenHub / 百度千帆 / DeepSeek 官方** 等多个上游，
聚合成 **一个 OpenAI 兼容端点**，以飞牛原生应用的形式安装。

**纯 Python 标准库实现**：不用 Docker、不用镜像、不装任何第三方包（`http.server` + `urllib` + `sqlite3`）。
装完就是一个 `python3 gateway.py` 进程，飞牛应用中心可见、桌面有图标、点开就是网页面板。

> 和 Docker 版的取舍：这个版本安装快、占内存小（约 25 MB）、没有镜像拉取和构建环节；
> 代价是没有容器隔离，直接以应用专用用户身份跑在宿主机上。

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
│   ├── providers.json           # 上游池默认模板
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

## 三、安装向导会问你什么

| 分组 | 字段 | 说明 |
|---|---|---|
| 基础设置 | 访问口令 | 留空 = 局域网免口令；填了客户端需带 `Bearer` |
| | 排序策略 | `稳定优先` / `省钱优先`（按价格从低到高） |
| | 失败冷却基数 | 默认 60 秒，连续失败按 1×2×3×… 递增，最多 6× |
| 上游密钥 | OpenRouter / 火山方舟 / 硅基流动 / 腾讯 TokenHub / 百度千帆 / DeepSeek 官方 | **至少填一个**，没填的自动跳过 |
| 网络代理 | HTTP/HTTPS 代理 | 海外上游需要；国内域名已自动进 `NO_PROXY` |

装完想改：应用中心 → DeepSeek 网关 → **设置**（走 `wizard/config`，保存后自动重启服务）。

密钥不会出现在进程列表里：它们被写进 `@appdata/.../run.sh`（权限 700、属主为应用用户），
由 `cmd/main` 以该用户身份启动。

---

## 四、装完怎么用

### 桌面打开面板

飞牛桌面「**DeepSeek 网关**」→ 打开 `http://<nas>:18787/admin`：

- 每个上游：状态、模型名、量化档位、输入/输出单价
- 24h 请求数、tokens、花费、最近错误
- **启用 / 停用**（写 `overrides.json`，不动 `providers.json`）
- **重载配置**（改完 JSON 点一下，不用重启）

### 客户端接入

| 项 | 值 |
|---|---|
| Base URL | `http://<飞牛IP>:18787/v1` |
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

---

## 五、改上游池

编辑 `/vol{n}/@appconf/deepseek-gateway/providers.json`，回面板点「重载配置」：

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
| `price_in/price_out` | USD / 百万 tokens，用于成本估算和 `cheapest` 排序 |

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
