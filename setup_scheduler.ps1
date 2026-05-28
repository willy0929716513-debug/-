# 執行前請先修改 run_bot.bat 填入你的 API Keys
# 用系統管理員身分執行 PowerShell，然後輸入：
#   Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
#   .\setup_scheduler.ps1

$batPath = Join-Path $PSScriptRoot "run_bot.bat"
$logDir  = Join-Path $PSScriptRoot "logs"

if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }

$action  = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$batPath`""
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopOnIdleEnd

# 台灣時間各區賽事開跑前約 1.5 小時觸發
$times = @(
    @{ Name="TenniBot-EU-Early";  Time="14:00"; Desc="歐洲早場 (法網/溫網)" },
    @{ Name="TenniBot-EU-Mid";    Time="17:00"; Desc="歐洲午場" },
    @{ Name="TenniBot-US-Early";  Time="21:00"; Desc="北美早場 (美網)" },
    @{ Name="TenniBot-US-Mid";    Time="01:00"; Desc="北美午場" },
    @{ Name="TenniBot-AUS-Early"; Time="06:00"; Desc="澳洲早場 (澳網)" }
)

foreach ($t in $times) {
    $trigger = New-ScheduledTaskTrigger -Daily -At $t.Time
    Register-ScheduledTask `
        -TaskName   $t.Name `
        -Action     $action `
        -Trigger    $trigger `
        -Settings   $settings `
        -Description $t.Desc `
        -Force | Out-Null
    Write-Host "已建立: $($t.Name) — 每天 $($t.Time) TW ($($t.Desc))"
}

Write-Host "`n完成！共建立 $($times.Count) 個排程。"
Write-Host "可在「工作排程器」→「工作排程器程式庫」查看。"
