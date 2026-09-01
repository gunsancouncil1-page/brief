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

# 실행이 왜 안 되는지 먼저 짚어 준다. 그냥 죽어 버리면 원인을 알기 어렵다.
$inUse = Get-NetTCPConnection -LocalPort $resolvedPort -State Listen -ErrorAction SilentlyContinue
if ($inUse) {
    $owner = Get-Process -Id $inUse[0].OwningProcess -ErrorAction SilentlyContinue
    Write-Host "$resolvedPort 번 포트를 이미 다른 프로그램이 쓰고 있습니다." -ForegroundColor Red
    Write-Host "  PID $($inUse[0].OwningProcess) $(if ($owner) { $owner.ProcessName })" -ForegroundColor Red
    Write-Host '  그 프로그램을 끄거나, 다른 포트로 실행하세요:  .\scripts\run-server.ps1 -Port 8001' -ForegroundColor Red
    exit 1
}

$anyAddress = @('0.0.0.0', '127.0.0.1', 'localhost')
if ($resolvedHost -notin $anyAddress) {
    $mine = (Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue).IPAddress
    if ($resolvedHost -notin $mine) {
        Write-Host "APP_HOST에 적힌 $resolvedHost 는 지금 이 PC의 주소가 아닙니다." -ForegroundColor Red
        Write-Host '  Tailscale이 연결돼 있는지 확인하세요:  tailscale ip -4' -ForegroundColor Red
        Write-Host "  이 PC의 주소: $($mine -join ', ')" -ForegroundColor Red
        exit 1
    }
}

if ($resolvedHost -notin @('127.0.0.1', 'localhost')) {
    Write-Host "이 PC 밖에서도 접속할 수 있는 주소로 엽니다: $resolvedHost`:$resolvedPort" -ForegroundColor Yellow
    Write-Host '  관리자 키(.env의 ADMIN_API_KEY)가 충분히 긴지 확인하세요.' -ForegroundColor Yellow
    $tailscaleIp = (Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object { $_.InterfaceAlias -like '*Tailscale*' }).IPAddress
    if ($tailscaleIp) {
        Write-Host "  휴대폰에서:  http://$tailscaleIp`:$resolvedPort/admin" -ForegroundColor Yellow
    }
}

Set-Location -LiteralPath $ProjectRoot

# uvicorn은 평범한 기록도 stderr로 내보낸다. 출력을 파일로 넘겨 실행할 때
# 'Stop'인 채로 두면 첫 기록 줄에서 서버가 그대로 죽는다.
$ErrorActionPreference = 'Continue'
& $Python -m uvicorn app.main:app --host $resolvedHost --port $resolvedPort
