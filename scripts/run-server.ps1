param(
    [int]$Port = 8000
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $Python)) {
    throw '가상 환경이 없습니다. README의 빠른 시작 절차를 먼저 완료하세요.'
}

Set-Location -LiteralPath $ProjectRoot
& $Python -m uvicorn app.main:app --host 127.0.0.1 --port $Port

