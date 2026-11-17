# -*- coding: utf-8 -*-
<#
.SYNOPSIS
    安装 / 卸载「守护进程自启」与「外置看门狗」两个 Windows 计划任务（P0 修复）。

.DESCRIPTION
    背景（2026-11-13 事故复盘）：
      2026-11-10 12:49 → 11-13 21:02，守护进程停了整整 3 天。
      原因是「没有任何机制保证开机后 daemon 会被拉起」：
        · start_daemon_24x7.bat 只会在自己活着的时候重启 python 子进程；
          机器重启 / 窗口被关 / 用户没双击 → 什么都没跑，也没人知道；
        · daemon 的死亡只能靠人肉发现，没有任何外部告警。
      本脚本注册两个任务解决这两个洞：
        1) QuantDaemon_AutoStart   登录时启动 start_daemon_24x7.bat（幂等，已在跑则不会双开）
        2) QuantDaemon_Watchdog    每 10 分钟跑 tools/daemon_watchdog.py（发现死亡→企微告警+拉起）

    为什么不再用 schtasks：
      旧版用 `schtasks /create /sc onlogon`，在非管理员会话下会直接报
      「ERROR: Access is denied.」——登录触发器属于系统级事件，注册需要提权。
      改为 ScheduledTasks 模块（Register-ScheduledTask + New-ScheduledTaskTrigger -AtLogOn），
      以「当前用户 / 交互式登录 / 受限权限」注册，实测无需管理员即可成功。
      同时能设置旧版设不了的项：电池供电不停止、错过计划后补跑、无限执行时长。

    为什么用「登录时」而不是「开机时」：
      · 开机任务（/sc onstart）需要 SYSTEM 权限与管理员提权，注册时就要 UAC；
      · 本机是笔记本（Redmi Book Pro 14），7×24 靠「插电不休眠 + 用户常驻登录」维持，
        登录触发已足够，且不需要管理员权限即可注册。
      如需真正开机即起（无人登录），用管理员 PowerShell 追加：
        schtasks /create /tn QuantDaemon_AutoStartBoot /tr "D:\xiaojie\start_daemon_24x7.bat" /sc onstart /ru SYSTEM /rl HIGHEST /f

    安全性：
      · 只注册计划任务，不改注册表、不装服务、不动用户环境变量；
      · -Uninstall 完全移除（可反复执行）；
      · -DryRun 只打印将执行的操作，方便先看再跑；
      · 默认只做只读诊断（daemon_watchdog.py --status），不会拉起进程、不会发企微告警；
        要当场跑完整看门狗（可能告警/拉起）请显式加 -RunNow。

.PARAMETER Uninstall
    删除两个计划任务。

.PARAMETER DryRun
    只打印操作，不实际注册/删除。

.PARAMETER WatchdogMinutes
    看门狗间隔（默认 10 分钟）。

.PARAMETER RunNow
    注册完成后立即执行一次完整看门狗（会告警 + 可能拉起 daemon）。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File D:\xiaojie\tools\install_daemon_autostart.ps1
    powershell -ExecutionPolicy Bypass -File D:\xiaojie\tools\install_daemon_autostart.ps1 -Uninstall
    powershell -ExecutionPolicy Bypass -File D:\xiaojie\tools\install_daemon_autostart.ps1 -RunNow
#>
param(
    [switch]$Uninstall,
    [switch]$DryRun,
    [int]$WatchdogMinutes = 10,
    [switch]$RunNow
)

$ErrorActionPreference = 'Stop'

$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$Python = Join-Path $Root '_pyenv\venv\Scripts\python.exe'
if (-not (Test-Path $Python)) { $Python = 'python' }
$PythonW = Join-Path $Root '_pyenv\venv\Scripts\pythonw.exe'
if (-not (Test-Path $PythonW)) { $PythonW = $Python }
$StartBat = Join-Path $Root 'start_daemon_24x7.bat'
$Watchdog = Join-Path $Root 'tools\daemon_watchdog.py'

$TaskAuto = 'QuantDaemon_AutoStart'
$TaskWatch = 'QuantDaemon_Watchdog'
$Who = "$env:USERDOMAIN\$env:USERNAME"

function Write-Step($msg) { Write-Host "[autostart] $msg" -ForegroundColor Cyan }
function Write-Warn($msg) { Write-Host "[autostart] $msg" -ForegroundColor Yellow }
function Write-Ok($msg) { Write-Host "[autostart] $msg" -ForegroundColor Green }
function Write-Dim($msg) { Write-Host "  $msg" -ForegroundColor DarkGray }

function Test-TaskExists([string]$Name) {
    return [bool](Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue)
}

if (-not (Get-Command Register-ScheduledTask -ErrorAction SilentlyContinue)) {
    throw '本机 PowerShell 缺少 ScheduledTasks 模块（需 Win8+/Server2012+）。请改用管理员 PowerShell 手动 schtasks 注册。'
}

# ---------------- 卸载 ----------------
if ($Uninstall) {
    Write-Step '卸载计划任务…'
    foreach ($t in @($TaskAuto, $TaskWatch)) {
        if (-not (Test-TaskExists $t)) { Write-Warn "不存在，跳过：$t"; continue }
        if ($DryRun) { Write-Dim "Unregister-ScheduledTask -TaskName $t -Confirm:`$false"; continue }
        Unregister-ScheduledTask -TaskName $t -Confirm:$false
        Write-Ok "已删除 $t"
    }
    Write-Step '卸载完成。守护进程本身不会被停止；如需停止请关掉它的控制台窗口。'
    exit 0
}

# ---------------- 前置检查 ----------------
Write-Step "项目根目录：$Root"
Write-Step "Python     ：$Python"
Write-Step "注册身份   ：$Who（交互式登录 / 受限权限，无需管理员）"
if (-not (Test-Path $StartBat)) { throw "找不到 $StartBat" }
if (-not (Test-Path $Watchdog)) { throw "找不到 $Watchdog" }

$principal = New-ScheduledTaskPrincipal -UserId $Who -LogonType Interactive -RunLevel Limited

# 公共设置：笔记本必须允许电池供电运行，否则拔掉电源任务直接不跑。
# ExecutionTimeLimit=Zero 表示不限时长（自启任务要常驻，不能被 3 天默认上限掐掉）。
function New-BaseSettings([TimeSpan]$Limit) {
    return New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit $Limit
}

# ---------------- 1) 登录自启 ----------------
Write-Step "注册登录自启任务 $TaskAuto（登录后延迟 1 分钟）"
# [P0 修复 2026-11-13] 必须带 --unattended。
# start_daemon_24x7.bat 在 verify_system.py 失败 / 找不到 python / UI 互斥冲突时会 pause。
# 自启任务的动作如果是裸启动 bat，一旦某天登录时 verify 不通过，那个窗口会永久停在
# pause，任务状态卡在 Running；配合 MultipleInstances=IgnoreNew + ExecutionTimeLimit=无限，
# 之后每次登录都会被直接忽略 —— 自启等于彻底失效。
# （旧版靠看门狗 10 分钟后带 --unattended 兜底拉起，但那是兜底，不是主路径。）
$actAuto = New-ScheduledTaskAction -Execute 'cmd.exe' `
    -Argument "/c `"$StartBat`" --unattended" `
    -WorkingDirectory $Root
$trgAuto = New-ScheduledTaskTrigger -AtLogOn -User $Who
$trgAuto.Delay = 'PT1M'

if ($DryRun) {
    Write-Dim "Action   : cmd.exe /c `"$StartBat`"   (cwd=$Root)"
    Write-Dim "Trigger  : AtLogOn User=$Who Delay=PT1M"
    Write-Dim "Settings : AllowStartIfOnBatteries, DontStopIfGoingOnBatteries, StartWhenAvailable, MultipleInstances=IgnoreNew, ExecutionTimeLimit=Unlimited"
} else {
    Register-ScheduledTask -TaskName $TaskAuto `
        -Action $actAuto -Trigger $trgAuto `
        -Settings (New-BaseSettings ([TimeSpan]::Zero)) `
        -Principal $principal `
        -Description '守护进程登录自启（Quant 项目）。幂等：daemon 有单实例锁，已在运行则不会双开。' `
        -Force | Out-Null
    Write-Ok "已注册 $TaskAuto"
}

# ---------------- 2) 外置看门狗 ----------------
Write-Step "注册看门狗任务 $TaskWatch（每 $WatchdogMinutes 分钟）"
# 用 pythonw 启动：避免每 10 分钟弹一个黑色控制台窗口。
# 看门狗的输出会写 data\runtime\daemon_watchdog.log，不依赖控制台。
$actWatch = New-ScheduledTaskAction -Execute $PythonW `
    -Argument "`"$Watchdog`"" `
    -WorkingDirectory $Root
$trgWatch = New-ScheduledTaskTrigger -Once -At ((Get-Date).AddMinutes(1)) `
    -RepetitionInterval (New-TimeSpan -Minutes $WatchdogMinutes) `
    -RepetitionDuration (New-TimeSpan -Days 1)
# Duration 置空 = 无限期重复（PS 5.1 里直接传 Zero 会生成非法 XML，报 0x80041318）
$trgWatch.Repetition.Duration = $null
$trgWatch.Repetition.StopAtDurationEnd = $false

if ($DryRun) {
    Write-Dim "Action   : $PythonW `"$Watchdog`"   (cwd=$Root)"
    Write-Dim "Trigger  : Once at $((Get-Date).AddMinutes(1).ToString('HH:mm')) then every $WatchdogMinutes min, forever"
    Write-Dim "Settings : 同上，ExecutionTimeLimit=5min"
} else {
    Register-ScheduledTask -TaskName $TaskWatch `
        -Action $actWatch -Trigger $trgWatch `
        -Settings (New-BaseSettings (New-TimeSpan -Minutes 5)) `
        -Principal $principal `
        -Description '守护进程外置看门狗：心跳超时→企微告警+自动拉起。20:25~21:20 与 maintenance_mode 静默。' `
        -Force | Out-Null
    Write-Ok "已注册 $TaskWatch"
}

# ---------------- 3) 只读诊断（默认不做任何副作用） ----------------
if (-not $DryRun) {
    Write-Step '运行只读诊断（不告警、不拉起）：'
    & $Python $Watchdog --status
    Write-Dim "退出码：$LASTEXITCODE"
}

# ---------------- 4) 可选：立即跑一次完整看门狗 ----------------
if ($RunNow -and -not $DryRun) {
    Write-Step '立即执行完整看门狗（可能发企微告警并拉起 daemon）…'
    & $Python $Watchdog
    Write-Dim "退出码：$LASTEXITCODE（0=已处理/健康，1=发现故障）"
}

# ---------------- 5) 自检输出 ----------------
if (-not $DryRun) {
    Write-Step '当前计划任务状态：'
    foreach ($t in @($TaskAuto, $TaskWatch)) {
        $task = Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue
        if (-not $task) { Write-Warn "  未注册：$t"; continue }
        $info = Get-ScheduledTaskInfo -TaskName $t -ErrorAction SilentlyContinue
        Write-Host "  $t : State=$($task.State)" -ForegroundColor Gray
        if ($info) {
            Write-Host "    上次运行: $($info.LastRunTime)  结果=0x$('{0:X}' -f $info.LastTaskResult)  下次: $($info.NextRunTime)" -ForegroundColor DarkGray
        }
    }
}

Write-Host ''
Write-Step '完成。要点：'
Write-Host '  · 登录后 1 分钟自动启动守护进程；已在运行时不会双开（单实例锁，退出码 2）。' -ForegroundColor Gray
Write-Host '  · 每 10 分钟检查心跳：死亡→企微告警（60 分钟冷却）+ 自动拉起（20 分钟冷却）。' -ForegroundColor Gray
Write-Host '  · 20:25~21:20 与 maintenance_mode 期间看门狗静默（避让数据库压缩）。' -ForegroundColor Gray
Write-Host '  · 电池供电下也会运行（笔记本必需），不会因拔电源而停摆。' -ForegroundColor Gray
Write-Host '  · 卸载：powershell -ExecutionPolicy Bypass -File D:\xiaojie\tools\install_daemon_autostart.ps1 -Uninstall' -ForegroundColor Gray
Write-Host '  · 手动查看状态：python tools\daemon_watchdog.py --status' -ForegroundColor Gray
