#!/bin/bash
# 一键打包：生成 .fpk（本机打包用，走 GitHub Actions 则不需要）
set -e
cd "$(dirname "$0")"

# 生命周期脚本和 shell 工具必须有执行位，否则装完起不来
chmod +x cmd/* app/service.sh app/install-opencode.sh 2>/dev/null || true

if ! command -v fnpack >/dev/null 2>&1; then
    cat <<'EOF'
未找到 fnpack（飞牛官方打包工具）。

下载地址：
  Linux x86  : https://static2.fnnas.com/fnpack/fnpack-1.2.3-linux-amd64
  Linux ARM  : https://static2.fnnas.com/fnpack/fnpack-1.2.3-linux-arm64
  macOS ARM  : https://static2.fnnas.com/fnpack/fnpack-1.2.3-darwin-arm64
  macOS x86  : https://static2.fnnas.com/fnpack/fnpack-1.2.3-darwin-amd64

安装：
  chmod +x fnpack-1.2.3-linux-amd64
  sudo mv fnpack-1.2.3-linux-amd64 /usr/local/bin/fnpack
  fnpack --help
EOF
    exit 1
fi

echo "==> 结构自检"
python3 scripts/check_structure.py

echo "==> 端到端冒烟测试"
python3 scripts/smoke_test.py

echo "==> 清理 Python 缓存"
find . -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
find . -name '*.pyc' -delete 2>/dev/null || true

echo "==> 开始打包"
fnpack build

echo "==> 产物："
ls -lh ./*.fpk
