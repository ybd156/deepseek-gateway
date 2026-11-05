#!/bin/bash
# 服务控制与运行配置生成（被 cmd/main 与 cmd/config_callback 复用）

# ── 生成运行包装脚本：把密钥写进 700 权限的 run.sh，避免出现在进程列表里 ──
write_run() {
    local run="$TRIM_PKGVAR/run.sh"
    local port
    port="$(printf '%s' "${TRIM_SERVICE_PORT:-18787}" | tr -cd '0-9')"
    [ -n "$port" ] || port=18787

    mkdir -p "$TRIM_PKGVAR" || return 1

    (
        umask 077
        cat > "$run" <<EOF
#!/bin/bash
# 本文件由飞牛应用在安装/配置时自动生成，请勿手工编辑
# 要改密钥或策略，请到「应用中心 → DeepSeek 网关 → 设置」
export CONFIG_PATH='$TRIM_PKGETC/providers.json'
export DATA_DIR='$TRIM_PKGVAR'
export PORT='$port'
export GATEWAY_TOKEN='${wizard_gateway_token:-}'
export COOLDOWN_SECONDS='${wizard_cooldown:-60}'
export STRATEGY='${wizard_strategy:-priority}'
export OPENROUTER_API_KEY='${wizard_openrouter_key:-}'
export VOLC_ARK_API_KEY='${wizard_volc_key:-}'
export SILICONFLOW_API_KEY='${wizard_siliconflow_key:-}'
export TENCENT_TOKENHUB_KEY='${wizard_tencent_key:-}'
export BAIDU_QIANFAN_KEY='${wizard_baidu_key:-}'
export DEEPSEEK_API_KEY='${wizard_deepseek_key:-}'
if [ -n '${wizard_http_proxy:-}' ]; then
    export HTTP_PROXY='${wizard_http_proxy}'
    export HTTPS_PROXY='${wizard_http_proxy}'
fi
export NO_PROXY='localhost,127.0.0.1,ark.cn-beijing.volces.com,api.siliconflow.cn,api.deepseek.com,tokenhub.tencentmaas.com,qianfan.baidubce.com'
exec python3 '$TRIM_APPDEST/gateway.py'
EOF
        chmod 700 "$run"
    ) || return 1

    if [ -n "${TRIM_USERNAME:-}" ] && id "$TRIM_USERNAME" >/dev/null 2>&1; then
        chown "$TRIM_USERNAME:${TRIM_GROUPNAME:-$TRIM_USERNAME}" "$run" 2>/dev/null || true
    fi
    return 0
}

# ── 进程控制 ──
gw_running() {
    pgrep -f "$TRIM_APPDEST/gateway.py" >/dev/null 2>&1
}

gw_start() {
    local run="$TRIM_PKGVAR/run.sh"
    local log="$TRIM_PKGVAR/app.log"

    if gw_running; then
        return 0
    fi
    if [ ! -x "$run" ]; then
        echo "缺少运行脚本 $run，请在应用设置里重新保存一次。" > "$TRIM_TEMP_LOGFILE"
        return 1
    fi

    mkdir -p "$TRIM_PKGVAR"
    if [ "$(id -u)" = "0" ] && command -v runuser >/dev/null 2>&1 && [ -n "${TRIM_USERNAME:-}" ]; then
        nohup runuser -u "$TRIM_USERNAME" -- "$run" >>"$log" 2>&1 &
    else
        nohup "$run" >>"$log" 2>&1 &
    fi

    local i
    for i in $(seq 1 30); do
        sleep 0.2
        if gw_running; then
            return 0
        fi
    done

    echo "服务启动失败，详见日志：$log" > "$TRIM_TEMP_LOGFILE"
    return 1
}

gw_stop() {
    gw_running || return 0

    pkill -f "$TRIM_APPDEST/gateway.py" 2>/dev/null
    local i
    for i in $(seq 1 30); do
        sleep 0.2
        if ! gw_running; then
            return 0
        fi
    done

    pkill -9 -f "$TRIM_APPDEST/gateway.py" 2>/dev/null
    sleep 0.3
    return 0
}
