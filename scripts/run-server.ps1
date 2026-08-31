param(
    [string]$AppHost,
    [int]$Port
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $Python)) {
    throw '가상 환경이 없습니다. README의 빠른 시작 절차를 먼저 완료하세요.'
}

# 주소와 포트는 .env를 따른다. 인자를 주면 그것이 우선한다.
$resolvedHost = '127.0.0.1'
$resolvedPort = 8000
$envFile = Join-Path $ProjectRoot '.env'
if (Test-Path -LiteralPath $envFile) {
    foreach ($line in Get-Content -LiteralPath $envFile) {
        if ($line -match '^\s*APP_HOST\s*=\s*(\S+)\s*$') { $resolvedHost = $Matches[1] }
        if ($line -match '^\s*APP_PORT\s*=\s*(\d+)\s*$') { $resolvedPort = [int]$Matches[1] }
    }
}
if ($AppHost) { $resolvedHost = $AppHost }
if ($Port) { $resolvedPort = $Port }

if ($resolvedHost -ne '127.0.0.1' -and $resolvedHost -ne 'localhost') {
    Write-Host "이 PC 밖에서도 접속할 수 있는 주소로 엽니다: $resolvedHost`:$resolvedPort" -ForegroundColor Yellow
    Write-Host '관리자 키(.env의 ADMIN_API_KEY)가 충분히 긴지 확인하세요.' -ForegroundColor Yellow
}

Set-Location -LiteralPath $ProjectRoot
& $Python -m uvicorn app.main:app --host $resolvedHost --port $resolvedPort
