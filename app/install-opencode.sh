#!/bin/bash
# 把 Node + opencode 装进应用私有目录（不碰系统包，删目录即卸载）
# 由网关的「一键安装」按钮调用：install-opencode.sh <DATA_DIR>
set -uo pipefail

DATA_DIR="${1:-}"
if [ -z "$DATA_DIR" ]; then
    echo "用法：install-opencode.sh <DATA_DIR>"
    exit 1
fi

ARCH="$(uname -m)"
case "$ARCH" in
    x86_64|amd64)   NARCH=x64 ;;
    aarch64|arm64)  NARCH=arm64 ;;
    *) echo "不支持的架构：$ARCH"; exit 1 ;;
esac

mkdir -p "$DATA_DIR/node" "$DATA_DIR/npm" || exit 1

# ── 1) Node：系统有就用系统的 ──
if command -v node >/dev/null 2>&1 && command -v npm >/dev/null 2>&1; then
    echo "使用系统已有的 Node：$(node -v 2>&1)"
else
    NODE_VER="${NODE_VER:-v22.14.0}"
    URL="https://nodejs.org/dist/${NODE_VER}/node-${NODE_VER}-linux-${NARCH}.tar.xz"
    echo "下载 Node ${NODE_VER} (${NARCH})"
    echo "  $URL"
    TMP="$(mktemp -d)" || exit 1
    if ! curl -fL --retry 3 --retry-delay 3 --max-time 900 -o "$TMP/node.tar.xz" "$URL"; then
        echo "下载失败。可以："
        echo "  1) 换版本：NODE_VER=v20.18.0 重试（改应用设置或手动跑本脚本）"
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
fi

export PATH="$DATA_DIR/node/bin:$DATA_DIR/npm/bin:$PATH"

echo "node: $(node -v 2>&1)"
echo "npm : $(npm -v 2>&1)"

# ── 2) opencode ──
echo "安装 opencode-ai …"
if ! npm install -g --prefix "$DATA_DIR/npm" opencode-ai; then
    echo "npm 安装失败（检查网络 / npm 源）"
    exit 1
fi

if [ ! -x "$DATA_DIR/npm/bin/opencode" ]; then
    echo "安装完成但没找到 $DATA_DIR/npm/bin/opencode"
    exit 1
fi

echo "版本：$("$DATA_DIR/npm/bin/opencode" --version 2>&1 | head -n1)"
echo "完成：$DATA_DIR/npm/bin/opencode"
