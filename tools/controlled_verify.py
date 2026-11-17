# -*- coding: utf-8 -*-
"""tools/controlled_verify.py — 受控窗口完整体检（一键）

【要解决的问题】
daemon 运行时独占 DuckDB（单写者模型），verify_system.py 的读库类 P0 检查
（P0-6 全表检查等）会被环境性 FAIL 淹没，看不到真正的体检结论。
本工具制造一个「干净的停机窗口」跑完整 verify，然后**自动恢复** daemon。

【步骤（按序）】
  1. 禁用计划任务 QuantDaemon_Watchdog / QuantDaemon_AutoStart（防止验证期间被拉起）
  2. 结束 start_daemon_24x7.bat 的 cmd 循环 + auto_sniper_daemon.py 进程树
     （只精确匹配这两类 cmdline，永不泛杀 python）
  3. 等待 DuckDB 文件锁释放（最多 90s）
  4. 跑 verify_system.py：完整输出落盘 data/runtime/verify_reports/，
     终端只筛选 [FAIL]/[WARN]/汇总，避免被 trade_cal 日志刷屏
  5. finally（无论成败）：恢复两个计划任务并触发 QuantDaemon_AutoStart，
     等待 daemon 心跳恢复；daemon 会以最新代码启动（兼作修复生效重启）

【安全】
  · --dry-run 只打印计划，不动任何进程/任务
  · 交易时段（工作日 09:10-15:10）默认拒绝；确需体检加 --force
  · finally 一定会恢复任务；恢复失败会打印手工恢复的三条命令
  · 退出码：0=verify 无 FAIL；1=verify 有 FAIL 或步骤异常；2=拒绝执行

用法：
    D:\\xiaojie\\_pyenv\\venv\\Scripts\\python.exe tools\\controlled_verify.py
    ... --dry-run      # 只看计划
    ... --force        # 交易时段强制
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, "data", "quant_data.duckdb")
LOG_PATH = os.path.join(ROOT, "data", "runtime", "sniper.log")
REPORT_DIR = os.path.join(ROOT, "data", "runtime", "verify_reports")

TASK_WATCHDOG = r"\QuantDaemon_Watchdog"
TASK_AUTOSTART = r"\QuantDaemon_AutoStart"

BJ_TZ = timezone(timedelta(hours=8))

# 只杀这两类 cmdline 的进程（互斥精确匹配，防误伤）
_KILL_PATTERNS = ("auto_sniper_daemon.py", "start_daemon_24x7")


def _say(msg: str) -> None:
    print(f"[controlled_verify] {msg}", flush=True)


def _ps(cmd: str) -> subprocess.CompletedProcess:
    """跑一个 PowerShell 命令（捕获输出）。"""
    return subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="ignore",
        timeout=120,
    )


def _schtasks(args: list) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["schtasks"] + args, capture_output=True, text=True,
        encoding="gbk", errors="ignore", timeout=60,
    )


def _is_trading_window() -> bool:
    now = datetime.now(BJ_TZ)
    if now.weekday() >= 5:
        return False
    hm = now.hour * 100 + now.minute
    return 910 <= hm <= 1510


def _find_target_pids() -> list:
    """找出 daemon / 24x7 循环的进程（pid, cmdline）。"""
    cond = " -or ".join(f"$_.CommandLine -like '*{p}*'" for p in _KILL_PATTERNS)
    out = _ps(
        "Get-CimInstance Win32_Process | Where-Object { $_.ProcessId -ne $PID -and ("
        + cond
        + ") } | Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"
    )
    raw = (out.stdout or "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except Exception:
        return []
    if isinstance(data, dict):
        data = [data]
    me = os.getpid()
    items = []
    for it in data:
        try:
            pid = int(it.get("ProcessId") or 0)
        except Exception:
            continue
        if pid and pid != me:
            items.append((pid, str(it.get("CommandLine") or "")[:120]))
    return items


def _db_free(timeout: float = 90.0) -> bool:
    """等待 DuckDB 文件锁释放（能开 read_only 即空闲）。"""
    try:
        import duckdb
    except Exception:
        return True
    t0 = time.time()
    while time.time() - t0 < timeout:
        con = None
        try:
            con = duckdb.connect(DB_PATH, read_only=True)
            return True
        except Exception:
            time.sleep(2)
        finally:
            if con is not None:
                try:
                    con.close()
                except Exception:
                    pass
    return False


def _daemon_alive(max_wait: float = 420.0, since_ts: float = 0.0) -> bool:
    """恢复后确认 daemon 真的起来了。

    判定标准（2026-11-14 修正）：
      · 必须有 cmdline 含 auto_sniper_daemon.py 的进程（start_daemon_24x7.bat
        在进循环前会先跑一遍 verify_system 前置检查，期间只有 cmd 在，python 未起）
      · 且 sniper.log 的 mtime 晚于 since_ts（恢复触发时刻），防止旧日志假阳性
    """
    t0 = time.time()
    while time.time() - t0 < max_wait:
        try:
            procs = _find_target_pids()
            if any("auto_sniper_daemon.py" in c for _p, c in procs):
                try:
                    if os.path.exists(LOG_PATH) and os.path.getmtime(LOG_PATH) >= since_ts - 5:
                        return True
                except Exception:
                    return True  # 进程在但日志读不了 → 以进程为准
        except Exception:
            pass
        time.sleep(5)
    return False


def _restore_tasks() -> list:
    """恢复计划任务 + 触发自启。返回失败步骤列表（空=全成功）。"""
    fails = []
    for tn in (TASK_WATCHDOG, TASK_AUTOSTART):
        r = _schtasks(["/Change", "/TN", tn, "/ENABLE"])
        if r.returncode != 0:
            fails.append(f"enable {tn}: {(r.stdout or r.stderr or '').strip()[:120]}")
    r = _schtasks(["/Run", "/TN", TASK_AUTOSTART])
    if r.returncode != 0:
        fails.append(f"run {TASK_AUTOSTART}: {(r.stdout or r.stderr or '').strip()[:120]}")
    return fails


def main() -> int:
    dry = "--dry-run" in sys.argv
    force = "--force" in sys.argv

    if _is_trading_window() and not force:
        _say("拒绝：当前处于交易时段（工作日 09:10-15:10），停机体检会中断扫描。确需执行请加 --force")
        return 2

    _say(f"开始受控体检 | dry-run={dry} | 时间={datetime.now(BJ_TZ).strftime('%Y-%m-%d %H:%M:%S')}")
    targets = _find_target_pids()
    plan = [
        f"1) 禁用计划任务 {TASK_WATCHDOG} / {TASK_AUTOSTART}",
        f"2) 结束进程 {len(targets)} 个：" + "; ".join(f"pid={p} {c}" for p, c in targets),
        "3) 等待 DuckDB 解除占用（≤90s）",
        "4) 运行 verify_system.py（输出落盘 verify_reports/）",
        "5) 恢复计划任务并重新拉起 daemon（新代码生效）",
    ]
    for line in plan:
        _say("   " + line)
    if dry:
        _say("dry-run：未做任何变更。")
        return 0

    rc = 1
    restored = False
    try:
        # 1) 禁用任务
        for tn in (TASK_WATCHDOG, TASK_AUTOSTART):
            r = _schtasks(["/Change", "/TN", tn, "/DISABLE"])
            if r.returncode != 0:
                _say(f"禁用任务失败: {tn} -> {(r.stdout or r.stderr or '').strip()[:200]}")
                return 2
        _say("计划任务已禁用")

        # 2) 结束进程树（最多 3 轮重扫，防看门狗竞态在间隙里重拉）
        for _round in range(3):
            tg = _find_target_pids()
            if not tg:
                break
            for pid, cmd in tg:
                r = subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True, text=True, encoding="gbk", errors="ignore", timeout=60,
                )
                _say(f"taskkill pid={pid} rc={r.returncode} ({cmd[:60]})")
            time.sleep(2)
        left = _find_target_pids()
        if left:
            _say(f"警告：仍有目标进程存活 {left}")

        # 3) 等库释放
        if not _db_free(90):
            _say("警告：90s 内数据库仍被占用，verify 的读库检查将降级（不阻断）")
        else:
            _say("数据库已空闲")

        # 4) verify
        os.makedirs(REPORT_DIR, exist_ok=True)
        log_path = os.path.join(
            REPORT_DIR, f"verify_{datetime.now(BJ_TZ).strftime('%Y%m%d_%H%M%S')}.log"
        )
        _say(f"运行 verify_system.py ...（完整输出 → {log_path}）")
        with open(log_path, "w", encoding="utf-8") as fh:
            proc = subprocess.run(
                [sys.executable, "verify_system.py"],
                cwd=ROOT, capture_output=True, text=True,
                encoding="utf-8", errors="ignore", timeout=1800,
            )
            fh.write("===== STDOUT =====\n")
            fh.write(proc.stdout or "")
            fh.write("\n===== STDERR =====\n")
            fh.write(proc.stderr or "")
        text = (proc.stdout or "") + "\n" + (proc.stderr or "")
        fails = [ln for ln in text.splitlines() if "[FAIL]" in ln]
        warns = [ln for ln in text.splitlines() if "[WARN]" in ln]
        summary = [ln for ln in text.splitlines() if ("汇总" in ln or "结论" in ln or "OK=" in ln)]
        _say(f"verify 结束 rc={proc.returncode} | FAIL={len(fails)} WARN={len(warns)}")
        for ln in fails[:20]:
            _say("  " + ln.strip())
        for ln in warns[:10]:
            _say("  " + ln.strip())
        for ln in summary[-4:]:
            _say("  " + ln.strip())
        rc = 0 if (proc.returncode == 0 and not fails) else 1
    except Exception as e:  # noqa: BLE001
        _say(f"执行异常: {e}")
        rc = 1
    finally:
        # 5) 恢复（无论如何）
        since_ts = time.time()
        fails = _restore_tasks()
        restored = not fails
        if restored:
            _say("计划任务已恢复，正在等待 daemon 心跳（bat 前置检查约需 3-4 分钟）...")
            alive = _daemon_alive(420, since_ts)
            _say("daemon 已恢复（新代码生效）" if alive else "警告：420s 内未检测到 daemon 进程，请手动检查 start_daemon_24x7.bat")
            if not alive:
                rc = rc or 1
        else:
            _say("恢复失败！请手工执行：")
            for tn in (TASK_WATCHDOG, TASK_AUTOSTART):
                _say(f'    schtasks /Change /TN "{tn}" /ENABLE')
            _say(f'    schtasks /Run /TN "{TASK_AUTOSTART}"')
            for f in fails:
                _say(f"    原因: {f}")
            rc = 1

    _say(f"完成 | 退出码={rc}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
