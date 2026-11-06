#!/bin/bash
# 服务控制与运行配置生成（被 cmd/main 与 cmd/config_callback 复用）

# ── 生成运行包装脚本：把密钥写进 700 权限的 run.sh，避免出现在进程列表里 ──
# 注意：向导里留空的项会沿用上一次 run.sh 的值，避免升级后点一下设置就把旧 key 冲掉
write_run() {
    local run="$TRIM_PKGVAR/run.sh"
    local old="$TRIM_PKGVAR/run.sh"
    local port
    port="$(printf '%s' "${TRIM_SERVICE_PORT:-18787}" | tr -cd '0-9')"
    [ -n "$port" ] || port=18787

    # 从旧 run.sh 里取某个 export 的现有值
    oldval() {
        [ -f "$old" ] || return 0
        sed -n "s/^export $1='\(.*\)'\$/\1/p" "$old" 2>/dev/null | head -n1
    }

    local v_token v_cd v_strat v_or v_volc v_sf v_tc v_bd v_nv v_hw v_ds v_proxy
    v_token="$( [ -n "${wizard_gateway_token:-}" ] && printf '%s' "$wizard_gateway_token" || oldval GATEWAY_TOKEN )"
    v_cd="$( [ -n "${wizard_cooldown:-}" ] && printf '%s' "$wizard_cooldown" || oldval COOLDOWN_SECONDS )"
    v_strat="$( [ -n "${wizard_strategy:-}" ] && printf '%s' "$wizard_strategy" || oldval STRATEGY )"
    v_or="$( [ -n "${wizard_openrouter_key:-}" ] && printf '%s' "$wizard_openrouter_key" || oldval OPENROUTER_API_KEY )"
    v_volc="$( [ -n "${wizard_volc_key:-}" ] && printf '%s' "$wizard_volc_key" || oldval VOLC_ARK_API_KEY )"
    v_sf="$( [ -n "${wizard_siliconflow_key:-}" ] && printf '%s' "$wizard_siliconflow_key" || oldval SILICONFLOW_API_KEY )"
    v_tc="$( [ -n "${wizard_tencent_key:-}" ] && printf '%s' "$wizard_tencent_key" || oldval TENCENT_TOKENHUB_KEY )"
    v_bd="$( [ -n "${wizard_baidu_key:-}" ] && printf '%s' "$wizard_baidu_key" || oldval BAIDU_QIANFAN_KEY )"
    v_nv="$( [ -n "${wizard_nvidia_key:-}" ] && printf '%s' "$wizard_nvidia_key" || oldval NVIDIA_NIM_API_KEY )"
    v_hw="$( [ -n "${wizard_huawei_key:-}" ] && printf '%s' "$wizard_huawei_key" || oldval HUAWEI_MAAS_KEY )"
    v_ds="$( [ -n "${wizard_deepseek_key:-}" ] && printf '%s' "$wizard_deepseek_key" || oldval DEEPSEEK_API_KEY )"
    v_proxy="$( [ -n "${wizard_http_proxy:-}" ] && printf '%s' "$wizard_http_proxy" || oldval HTTP_PROXY )"

    mkdir -p "$TRIM_PKGVAR" || return 1

    (
        umask 077
        cat > "$run" <<EOF
#!/bin/bash
# 本文件由飞牛应用在安装/配置时自动生成，请勿手工编辑
# 要改密钥或策略，请打开网关面板（http://<nas>:18787/admin）直接改
export CONFIG_PATH='$TRIM_PKGETC/providers.json'
export DATA_DIR='$TRIM_PKGVAR'
export PORT='$port'
export GATEWAY_SOCKET='$TRIM_APPDEST/app.sock'
export GATEWAY_PREFIX='/app/deepseek-gateway'
export GATEWAY_TOKEN='$v_token'
export COOLDOWN_SECONDS='${v_cd:-60}'
export STRATEGY='${v_strat:-priority}'
export OPENROUTER_API_KEY='$v_or'
export VOLC_ARK_API_KEY='$v_volc'
export SILICONFLOW_API_KEY='$v_sf'
export TENCENT_TOKENHUB_KEY='$v_tc'
export BAIDU_QIANFAN_KEY='$v_bd'
export NVIDIA_NIM_API_KEY='$v_nv'
export HUAWEI_MAAS_KEY='$v_hw'
export DEEPSEEK_API_KEY='$v_ds'
if [ -n '$v_proxy' ]; then
    export HTTP_PROXY='$v_proxy'
    export HTTPS_PROXY='$v_proxy'
fi
export NO_PROXY='localhost,127.0.0.1,ark.cn-beijing.volces.com,api.siliconflow.cn,api.deepseek.com,tokenhub.tencentmaas.com,qianfan.baidubce.com,api.modelarts-maas.com'
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
