#!/bin/bash
# 安装 opencode 到应用私有目录（不碰系统包，删目录即卸载）
# 飞牛自带 Node，所以默认直接用系统的；只有系统里真没有才去下 Node 包
# 由网关的「一键安装」按钮调用：install-opencode.sh <DATA_DIR>
set -uo pipefail

DATA_DIR="${1:-}"
if [ -z "$DATA_DIR" ]; then
    echo "用法：install-opencode.sh <DATA_DIR>"
    exit 1
fi

# 应用以专用用户运行，PATH 可能很干净，显式兜底常见目录
EXTRA="/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin:/opt/bin:/snap/bin"
export PATH="$DATA_DIR/node/bin:$DATA_DIR/npm/bin:$EXTRA:${PATH:-}"

# ── 关键：把 npm 的 HOME / 缓存 / 日志 / 全局目录全部关进应用数据目录 ──
# 否则 npm 会往 /home/<appuser>/.npm 写日志，而那个目录通常不可写，
# 报 "The operation was rejected by your operating system"。
export HOME="$DATA_DIR/home"
export npm_config_cache="$DATA_DIR/npm-cache"
export npm_config_logs_dir="$DATA_DIR/npm-cache/_logs"
export npm_config_prefix="$DATA_DIR/npm"
export npm_config_fund=false
export npm_config_audit=false
export XDG_CACHE_HOME="$DATA_DIR/npm-cache"
export XDG_CONFIG_HOME="$DATA_DIR/home/.config"
export XDG_DATA_HOME="$DATA_DIR/home/.local/share"
mkdir -p "$HOME" "$npm_config_cache" "$npm_config_logs_dir" \
         "$DATA_DIR/npm" "$XDG_CONFIG_HOME" "$XDG_DATA_HOME" 2>/dev/null || {
    echo "无法创建目录：$DATA_DIR（检查应用数据目录权限）"
    exit 1
}

echo "HOME        = $HOME"
echo "npm 缓存     = $npm_config_cache"
echo "npm 全局前缀  = $npm_config_prefix"

ARCH="$(uname -m)"
case "$ARCH" in
    x86_64|amd64)   NARCH=x64 ;;
    aarch64|arm64)  NARCH=arm64 ;;
    *) echo "不支持的架构：$ARCH"; exit 1 ;;
esac

# ── 1) 找 Node：系统有就绝不下载 ──
NODE_BIN="$(command -v node 2>/dev/null || true)"
NPM_BIN="$(command -v npm 2>/dev/null || true)"

if [ -n "$NODE_BIN" ] && [ -n "$NPM_BIN" ]; then
    echo "检测到系统 Node：$("$NODE_BIN" -v 2>&1)  路径：$NODE_BIN"
    echo "检测到系统 npm ：$("$NPM_BIN" -v 2>&1)  路径：$NPM_BIN"
    echo "→ 直接用它安装 opencode，不下载 Node"
else
    echo "系统里没找到 node/npm，改为下载 Node 到应用私有目录"
    [ -n "$NODE_BIN" ] && echo "（只找到 node：$NODE_BIN，缺 npm）"
    NODE_VER="${NODE_VER:-v22.14.0}"
    URL="https://nodejs.org/dist/${NODE_VER}/node-${NODE_VER}-linux-${NARCH}.tar.xz"
    echo "下载：$URL"
    TMP="$(mktemp -d)" || exit 1
    if ! curl -fL --retry 3 --retry-delay 3 --max-time 900 -o "$TMP/node.tar.xz" "$URL"; then
        echo "下载失败。可以："
        echo "  1) 指定版本重试：NODE_VER=v20.18.0"
        echo "  2) 手动下载 node-*-linux-${NARCH}.tar.xz 解压到 $DATA_DIR/node（--strip-components=1）"
        rm -rf "$TMP"
        exit 1
    fi
    if ! tar -xJf "$TMP/node.tar.xz" -C "$DATA_DIR/node" --strip-components=1; then
        echo "解压失败（可能缺 xz-utils）"
        rm -rf "$TMP"
        exit 1
    fi
    rm -rf "$TMP"
    export PATH="$DATA_DIR/node/bin:$PATH"
    NODE_BIN="$DATA_DIR/node/bin/node"
    NPM_BIN="$DATA_DIR/node/bin/npm"
    echo "私有 Node：$("$NODE_BIN" -v 2>&1)"
fi

# ── 2) opencode ──
echo "安装 opencode-ai 到 $npm_config_prefix …"
INSTALLED=""
# 优先装 v2（@opencode/cli）：v1（opencode-ai，0.x/1.x）已不再支持免费模型
for PKG in "@opencode/cli" "opencode-ai"; do
    echo "安装 $PKG 到 $npm_config_prefix …"
    if "$NPM_BIN" install -g --prefix "$npm_config_prefix" --no-fund --no-audit --loglevel=error "$PKG"; then
        INSTALLED="$PKG"
        echo "已安装：$PKG"
        break
    fi
    echo "$PKG 安装失败，换下一个…"
done
if [ -z "$INSTALLED" ]; then
    echo "npm 安装失败。常见原因："
    echo "  1) 网络 / npm 源：npm config get registry（可设成 https://registry.npmmirror.com）"
    echo "  2) 权限：确认 $DATA_DIR 对当前用户可写"
    exit 1
fi

OC_BIN="$DATA_DIR/npm/bin/opencode"
if [ ! -x "$OC_BIN" ]; then
    ALT=""
    for cand in "$DATA_DIR/npm/lib/node_modules/@opencode/cli/bin/opencode"                 "$DATA_DIR/npm/lib/node_modules/opencode-ai/bin/opencode"; do
        if [ -f "$cand" ]; then ALT="$cand"; break; fi
    done
    if [ -n "$ALT" ] && [ -f "$ALT" ]; then
        mkdir -p "$DATA_DIR/npm/bin"
        ln -sf "$ALT" "$OC_BIN"
        chmod +x "$OC_BIN" 2>/dev/null || true
    fi
fi

if [ ! -e "$OC_BIN" ]; then
    echo "安装完成但没找到 $OC_BIN"
    ls -la "$DATA_DIR/npm/bin" 2>/dev/null
    exit 1
fi

echo "版本：$("$OC_BIN" --version 2>&1 | head -n1)"
echo "完成：$OC_BIN"
