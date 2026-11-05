# 一键打包（Windows）：生成 .fpk
# 注意：Windows 下无法设置 Linux 执行位，正式发布建议用 GitHub Actions 或 WSL
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$fnpack = Join-Path $PSScriptRoot "fnpack.exe"
if (-not (Test-Path $fnpack)) {
    if (Get-Command fnpack -ErrorAction SilentlyContinue) {
        $fnpack = "fnpack"
    } else {
        Write-Host "未找到 fnpack.exe。" -ForegroundColor Yellow
        Write-Host "下载地址（无后缀，下载后重命名为 fnpack.exe 放到本目录）："
        Write-Host "  https://static2.fnnas.com/fnpack/fnpack-1.2.3-windows-amd64"
        exit 1
    }
}

Write-Host "==> 结构自检"
python scripts/check_structure.py
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "==> 端到端冒烟测试"
python scripts/smoke_test.py
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "==> 开始打包"
& $fnpack build

Write-Host "==> 产物："
Get-ChildItem *.fpk | Format-Table Name, Length, LastWriteTime -AutoSize
