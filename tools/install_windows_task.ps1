<#
.SYNOPSIS
  小杰AI选股系统 · Windows 计划任务安装脚本（Tier 0 · 0.2）

.DESCRIPTION
  把 24x7 守护进程注册成 Windows 计划任务，解决两件事：
    1) 开机自启           —— 断电/重启后无需人工登录手动双击 bat
    2) 崩溃自启（看门狗） —— 每 5 分钟检查一次；进程不在就拉起

  设计要点（与家里 Windows 单机场景对齐）：
    · 复用现有 start_daemon_24x7.bat 作为看门狗体，不重写启动逻辑
    · 以【隐藏窗口】运行，不弹黑框干扰桌面
    · 网络就绪后再启动（避开开机瞬间 DNS/网络未就绪）
    · 有电池的机器：默认允许电池供电时运行（服务器场景）
    · 绝不开放任何端口；只在本机运行

  用法（必须用【管理员】PowerShell）：
      powershell -ExecutionPolicy Bypass -File tools\install_windows_task.ps1
      powershell -ExecutionPolicy Bypass -File tools\install_windows_task.ps1 -Uninstall
      powershell -ExecutionPolicy Bypass -File tools\install_windows_task.ps1 -Status

.NOTES
  红线：不写 DuckDB、不改业务代码、不开放端口。
#>
param(
    [string]$TaskName = "XiaoJie_SniperDaemon_24x7",
    [switch]$Uninstall,
    [switch]$Status
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Bat  = Join-Path $Root "start_daemon_24x7.bat"

function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}

Write-Host "项目根目录: $Root" -ForegroundColor Cyan
Write-Host "看门狗脚本: $Bat"  -ForegroundColor Cyan

if ($Status) {
    try {
        $t = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
        $i = Get-ScheduledTaskInfo -TaskName $TaskName
        Write-Host "`n任务状态: $($t.State)" -ForegroundColor Green
        Write-Host "上次运行: $($i.LastRunTime)"
        Write-Host "上次结果: $($i.LastTaskResult)  (0 = 成功)"
        Write-Host "下次运行: $($i.NextRunTime)"
    } catch {
        Write-Host "任务 $TaskName 未注册。" -ForegroundColor Yellow
    }
    exit 0
}

if (-not (Test-Admin)) {
    Write-Host "`n[错误] 需要管理员权限。请右键 PowerShell → 以管理员身份运行后重试。" -ForegroundColor Red
    exit 1
}

if ($Uninstall) {
    try {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "[OK] 已删除计划任务 $TaskName" -ForegroundColor Green
    } catch {
        Write-Host "任务 $TaskName 不存在或删除失败: $($_.Exception.Message)" -ForegroundColor Yellow
    }
    exit 0
}

if (-not (Test-Path $Bat)) {
    Write-Host "[错误] 找不到 $Bat" -ForegroundColor Red
    exit 1
}

# ---- 1) 开机触发 + 每 5 分钟重复（同时覆盖"断电重启"与"进程崩溃"两种场景） ----
$action = New-ScheduledTaskAction -Execute "cmd.exe" `
    -Argument "/c `"$Bat`"" -WorkingDirectory $Root

$triggerAtStart = New-ScheduledTaskTrigger -AtStartup
$triggerRepeat   = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 5) `
    -RepetitionDuration (New-TimeSpan -Days 3650)

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Hours 0) `
    -MultipleInstances IgnoreNew `
    -Hidden

$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType S4U -RunLevel Highest

try {
    Register-ScheduledTask -TaskName $TaskName -Action $action `
        -Trigger @($triggerAtStart, $triggerRepeat) -Settings $settings `
        -Principal $principal -Description "小杰AI选股系统 24x7 守护进程（开机+每5分钟看门狗）" `
        -Force | Out-Null
    Write-Host "`n[OK] 计划任务已注册：$TaskName" -ForegroundColor Green
    Write-Host "     触发：开机 + 每 5 分钟检查"
    Write-Host "     动作：cmd /c `"$Bat`"（隐藏窗口）"
    Write-Host "`n常用命令："
    Write-Host "  查看状态：powershell -ExecutionPolicy Bypass -File `"$PSCommandPath`" -Status"
    Write-Host "  立即启动：Start-ScheduledTask -TaskName $TaskName"
    Write-Host "  停止任务：Stop-ScheduledTask  -TaskName $TaskName"
    Write-Host "  删除任务：powershell -ExecutionPolicy Bypass -File `"$PSCommandPath`" -Uninstall"
} catch {
    Write-Host "`n[失败] 注册计划任务出错: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "提示：若提示 LogonType S4U 不支持，可把 -LogonType S4U 改为 -LogonType Interactive" -ForegroundColor Yellow
    exit 1
}
