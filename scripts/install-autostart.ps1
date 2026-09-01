<#
  run-server.ps1을 로그온할 때 자동으로 띄우고, 꺼지면 다시 살린다.
  창은 뜨지 않고, 화면에 찍히던 내용은 storage\server.log에 쌓인다.

    등록:  .\scripts\install-autostart.ps1
    해제:  .\scripts\install-autostart.ps1 -Remove
    상태:  Get-ScheduledTask GunsanBrief | Get-ScheduledTaskInfo
#>
param(
    [switch]$Remove,
    [string]$TaskName = 'GunsanBrief'
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$ServerScript = Join-Path $ProjectRoot 'scripts\run-server.ps1'
$LogFile = Join-Path $ProjectRoot 'storage\server.log'

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue

if ($Remove) {
    if ($existing) {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "자동 실행을 해제했습니다: $TaskName" -ForegroundColor Yellow
        Write-Host '  이미 떠 있는 서버는 그대로입니다. 끄려면 작업 관리자에서 python.exe를 종료하세요.'
    }
    else {
        Write-Host "등록된 자동 실행이 없습니다: $TaskName"
    }
    return
}

if (-not (Test-Path -LiteralPath $ServerScript)) {
    throw "실행 스크립트를 찾지 못했습니다: $ServerScript"
}
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $LogFile) | Out-Null

# 창을 숨긴 채 실행하고, 화면에 찍히던 내용은 기록으로 남긴다.
# Out-File을 거치는 이유는 >> 가 UTF-16으로 써서 다른 도구에서 읽기 나쁘기 때문이다.
$inner = "& '$ServerScript' *>&1 | Out-File -FilePath '$LogFile' -Append -Encoding utf8"
$arguments = '-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -Command "{0}"' -f $inner

$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $arguments -WorkingDirectory $ProjectRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
# 시간 제한 없이 계속 돌리고, 죽으면 1분 뒤 다시 띄운다.
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartInterval (New-TimeSpan -Minutes 1) -RestartCount 99 -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description '군산 보도자료 스크랩 서버' -Force | Out-Null

Write-Host "자동 실행을 등록했습니다: $TaskName" -ForegroundColor Green
Write-Host '  로그온할 때마다 서버가 뜨고, 꺼지면 1분 뒤 다시 뜹니다.'
Write-Host "  기록:  $LogFile"

$inUse = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
if ($inUse) {
    Write-Host '  8000번 포트를 이미 쓰고 있어 지금은 띄우지 않았습니다. 그 서버를 끄고 다시 로그온하세요.' -ForegroundColor Yellow
    return
}

Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 3
$info = Get-ScheduledTask -TaskName $TaskName | Get-ScheduledTaskInfo
Write-Host "  지금 상태: $((Get-ScheduledTask -TaskName $TaskName).State) · 마지막 결과 $($info.LastTaskResult)"
Write-Host '  확인:  http://localhost:8000/admin'
