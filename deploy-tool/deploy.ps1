#!/usr/bin/env pwsh
# CPQS 部署工具的 Windows 启动器：优先使用 py，回退到 python。
$ErrorActionPreference = 'Stop'

$scriptPath = Join-Path $PSScriptRoot 'deploy.py'
$py = Get-Command py -ErrorAction SilentlyContinue
if ($py) {
    & $py.Source $scriptPath @args
    exit $LASTEXITCODE
}

$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    Write-Error '未找到 Python，请安装 Python 3.9+ 并加入 PATH。'
    exit 1
}

& $python.Source $scriptPath @args
exit $LASTEXITCODE
