# providers.d —— 新增上游放这里

主配置 `providers.json` **不用动**。在本目录放任意 `*.json`，网关启动或点「重载配置」时会自动合并：

- `name` 与已有上游重名 → 覆盖那一条
- 新名字 → 按 `priority` 参与排序
- 某个文件解析失败 → 只跳过它，不影响其他

> 目录位置：`/vol{n}/@appconf/deepseek-gateway/providers.d/`

---

## 复制这段改一改

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

**三个必填**：`name` / `base_url` / `model`。其余可省。

---

## 字段速查

| 字段 | 必填 | 说明 |
|---|---|---|
| `name` | ✅ | 唯一标识，改名会丢历史统计 |
| `type` | | `http`（默认，OpenAI 兼容上游）/ `opencode`（驱动本地 `opencode serve`） |
| `base_url` | ✅ | OpenAI 兼容根地址，**不要**带结尾斜杠，网关自己拼 `/chat/completions` |
| `model` | ✅ | 该平台上的模型 ID，从控制台复制，大小写敏感 |
| `api_key_env` | | 密钥来源的环境变量名（见下） |
| `enabled` | | 默认 `true`；建议先 `false`，确认可用再开 |
| `priority` | | 越小越先试，默认 100 |
| `free_only` | | `true` = 计入免费额度硬卡；额度用尽后**自动停用，绝不转付费** |
| `free_quota_tokens` | | 免费额度总量（tokens）。`free_only=true` 且此值 >0 时才生效 |
| `model_contains` | | 校验响应 `model` 字段，防上游静默换模型 |
| `strict_model` | | `true` = 校验不过就换下一家；`false` = 只加警告头 |
| `price_in/price_out` | | USD / 百万 tokens，用于成本估算和 `cheapest` 排序 |
| `extra` | | 原样并入请求体（如 OpenRouter 钉 provider） |

---

## 密钥怎么给

`api_key_env` 指向一个变量名，网关按这个顺序找值：

1. 环境变量（由安装向导写入 `@appdata/deepseek-gateway/run.sh`）
2. **`@appdata/deepseek-gateway/secrets.json`** ← 自己加新上游时用这个
3. 配置里的 `api_key` 字段（不推荐，会明文躺在配置里）

**推荐做法**：编辑 `secrets.json`（没有就新建），写进去就行，不用碰 `run.sh`：

```json
{
  "MY_PROVIDER_KEY": "sk-xxxxxxxx",
  "ANOTHER_KEY": "xxxx"
}
```

改完点面板的「重载配置」即可生效，`secrets.json` 不会因为你在应用设置里保存而被覆盖。

> `run.sh` 是向导生成的，手工改会在下次保存设置时被冲掉——所以新密钥放 `secrets.json`。

---

## 已验证的免费额度（参考，会变）

| 平台 | 额度 | 备注 |
|---|---|---|
| **华为云 ModelArts Studio** | 每模型 **200 万 tokens** | 国内额度最大；需实名，region 选贵阳一；模型名/base_url 以控制台为准 |
| **NVIDIA NIM** | 免费端点（额度说法不一） | `integrate.api.nvidia.com/v1`，模型 `deepseek-ai/deepseek-v4.1-flash`；需海外网络 |
| 火山方舟 | 每模型 50 万 tokens | 默认已启用 |
| 腾讯云 TokenHub | 新人 100 万 tokens / 90 天 | 模型名以控制台为准 |
| 百度千帆 | 每模型 100 万 tokens / 3 个月 | 模型名以控制台为准 |
| 硅基流动 | Free 标签模型免费 | 必须用带 Free 标签的模型名 |
| 魔搭 ModelScope | 2000 次/天（单模型 500） | 走 `api-inference.modelscope.cn/v1` |

> **加不进来的**：OpenCode 的 `opencode/deepseek-v4.1-flash-free` 虽然是真免费，但官方限制
> 只能在 OpenCode 客户端内使用——外部调用 `opencode.ai/inference/openai/v1` 会返回
> `403 FreeTierError`。别浪费时间往里加。
> 正确用法是反过来：让 OpenCode 把本网关当作 provider（见主 README「客户端接入」）。

额度政策随时会变，**填之前去控制台确认一次**，然后按实际数字写 `free_quota_tokens`。
