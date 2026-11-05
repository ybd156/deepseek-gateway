#!/bin/bash
# 启动 OpenCode 本地 server，供网关的 type=opencode 上游使用。
#
# 用法：
#   ./opencode-serve.sh                 # 工作目录默认 ./opencode-workspace
#   ./opencode-serve.sh /path/to/ws     # 指定工作目录
#
# 前置：npm install -g opencode-ai
#
# 为什么单独给一个空目录：OpenCode 是个编码 Agent，即使你只想问一句话，
# 它也可能尝试读文件、跑命令。把它关在一个空目录里，误伤面积最小。
set -u

WS="${1:-$PWD/opencode-workspace}"
PORT="${OPENCODE_PORT:-4096}"
HOST="${OPENCODE_HOST:-127.0.0.1}"

if ! command -v opencode >/dev/null 2>&1; then
    echo "未找到 opencode，请先安装：npm install -g opencode-ai" >&2
    exit 1
fi

mkdir -p "$WS" || exit 1
cd "$WS" || exit 1

echo "工作目录: $WS"
echo "监听:     http://$HOST:$PORT"
echo "提示:     网关上游里把 base_url 填成 http://$HOST:$PORT"
echo

exec opencode serve --port "$PORT" --hostname "$HOST"
