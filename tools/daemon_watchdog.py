# -*- coding: utf-8 -*-
"""
tools/daemon_watchdog.py — 守护进程「死亡告警 + 自动拉起」外置看门狗

【为什么必须有这个文件（P0 修复 2026-11-13）】
2026-11-10 12:49 → 11-13 21:02，守护进程整整停了 3 天没人知道：
  · `start_daemon_24x7.bat` 里的 while-loop 只管「python 子进程退出后重启」，
    它管不了「bat 窗口被关掉 / 机器重启后没人双击 / 整个 python 卡死不退出」；
  · daemon 自己会写心跳，但**进程死了就没人读心跳**，也就没人告警；
  · 于是「系统停了」和「今天没信号」在用户侧完全无法区分。
本脚本就是那个「进程外部、独立调度」的观察者：
  · 由 Windows 计划任务每 10 分钟跑一次（见 tools/install_daemon_autostart.ps1）；
  · 读 data/runtime/daemon_heartbeat.jsonl 最后一行 epoch 判断新鲜度；
  · 读 data/runtime/state/daemon.lock 判断进程是否还活着；
  · 判定「死了 / 卡住了」→ 企微告警（带冷却，避免刷屏）→ 按需自动拉起。

【安全边界（重要）】
  · **绝不打开 DuckDB**。DuckDB 全机单写者，看门狗去连库会在关键时刻抢锁/制造
    .wal 冲突；本脚本只读「心跳文件 + sentinel JSON + 文件 mtime」。
  · 不 kill 任何进程（除显式 --kill-hung）。VACUUM/压缩可能合法阻塞 20 分钟以上，
    误杀比漏杀危险得多。
  · 自动拉起走 `start_daemon_24x7.bat`（它自带 verify_system.py + UI 互斥检查），
    不绕过任何既有安全闸门。

【用法】
  python tools/daemon_watchdog.py                # 检查一次；异常才告警；死透则拉起
  python tools/daemon_watchdog.py --no-restart   # 只告警不拉起
  python tools/daemon_watchdog.py --status       # 只打印状态（供人看/接 UI），永远退出码 0
  python tools/daemon_watchdog.py --kill-hung    # 卡死时先 kill 再拉起（默认关闭）
  python tools/daemon_watchdog.py --self-test    # 纯逻辑自检

退出码：0 = 健康或已处理；1 = 发现故障但处理失败（计划任务会记录）。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

BJ_TZ = timezone(timedelta(hours=8))

RUNTIME_DIR = os.path.join(ROOT, "data", "runtime")
STATE_DIR = os.path.join(RUNTIME_DIR, "state")
# 心跳文件真身由 data/daemon_observability.py 决定（data/runtime/observability/）。
# 这里保留多个候选路径：历史上路径改过一次，看门狗必须能在两种布局下都找到心跳，
# 否则「找不到文件」会被误判成「进程从没心跳」→ 假告警。
_HEARTBEAT_CANDIDATES = (
    os.path.join(RUNTIME_DIR, "observability", "daemon_heartbeat.jsonl"),
    os.path.join(RUNTIME_DIR, "daemon_heartbeat.jsonl"),
)
DAEMON_LOCK_PATH = os.path.join(STATE_DIR, "daemon.lock")
MASTER_CONTROL_PATH = os.path.join(STATE_DIR, "master_control.json")
WATCHDOG_STATE_PATH = os.path.join(STATE_DIR, "daemon_watchdog_state.json")
START_BAT = os.path.join(ROOT, "start_daemon_24x7.bat")
# 【2026-11-15】用户 Ctrl+C 主动停止标记（auto_sniper_daemon.py 在 KeyboardInterrupt 时写入）。
# 存在时看门狗保持「人停我停」：不告警、不自动拉起；人工重新双击 bat 时清除。
USER_STOP_PATH = os.path.join(STATE_DIR, "user_manual_stop.json")

# 心跳每 5 分钟一行（schedule.every(5).minutes）。3 拍缺失才算异常：
# 单拍丢失可能只是写盘抖动，多拍缺失才说明进程真的没了/卡住了。
HEARTBEAT_INTERVAL_MIN = 5
STALE_HEARTBEAT_MIN = 20
# 「卡死」判定阈值比「死亡」宽得多：20:30 的安全压缩实测要跑 20.8 分钟，
# 期间 DuckDB 长事务会让心跳变稀。宁可晚 10 分钟告警，也不要误判。
HUNG_HEARTBEAT_MIN = 40
# 启动宽限（分钟）：sentinel 的 started_at 是进程一启动就写的，但第一拍心跳要等满
# HEARTBEAT_INTERVAL_MIN 才落盘。只看「心跳年龄」的话，任何一次停摆超过 40 分钟后的
# 恢复，都会在启动瞬间被判 hung——2026-11-13 实测：22:32:24 起进程，22:32:54 与
# 22:37:19 两次判 hung，22:37:48 首拍心跳落地才转 healthy。那两次只是「刚起还没写心跳」，
# 却会发出一条「守护进程疑似卡死」的假企微。宽限期内判 starting，既不发告警也不拉起。
STARTUP_GRACE_MIN = 15
# 同一类告警的最小间隔（分钟）：防止每 10 分钟一条刷屏
ALERT_COOLDOWN_MIN = 60
# 自动拉起的冷却（分钟）：拉起失败会连试，避免每 10 分钟弹一个窗口
RESTART_COOLDOWN_MIN = 20
# 这些时间窗内不做任何判定/拉起（压缩/维护窗口）
QUIET_WINDOWS: Tuple[Tuple[str, str], ...] = (("20:25", "21:20"),)


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------
def now_bj() -> datetime:
    return datetime.now(BJ_TZ)


def _read_json(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _write_json_atomic(path: str, payload: Dict[str, Any]) -> bool:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return True
    except Exception:
        return False


def _pid_alive(pid: Any) -> bool:
    """跨平台 PID 存活判断（不引入 psutil 依赖，失败按存活处理更安全？否：按死亡）。"""
    try:
        ipid = int(pid)
    except (TypeError, ValueError):
        return False
    if ipid <= 0:
        return False
    try:
        import psutil  # type: ignore

        return bool(psutil.pid_exists(ipid))
    except Exception:
        pass
    if os.name == "nt":
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {ipid}", "/NH"],
                capture_output=True, text=True, timeout=15,
            )
            return str(ipid) in (out.stdout or "")
        except Exception:
            return False
    try:
        os.kill(ipid, 0)
        return True
    except OSError:
        return False


def heartbeat_path() -> Optional[str]:
    """心跳文件路径：只按候选路径查找，**不 import 任何数据层模块**。

    【安全】data.daemon_observability 会连带 import data.db_core（进而触碰 DuckDB），
    而 DuckDB 是全机单写者：看门狗在夜里 20:30 压缩期间去连库会制造锁冲突。
    这里只做纯文件系统操作，宁可路径写死也不引入数据库依赖。
    """
    best: Optional[str] = None
    best_mtime = -1.0
    for p in _HEARTBEAT_CANDIDATES:
        try:
            if os.path.isfile(p):
                m = os.path.getmtime(p)
                if m > best_mtime:
                    best, best_mtime = p, m
        except Exception:
            continue
    return best


def last_heartbeat() -> Tuple[Optional[float], Optional[int]]:
    """返回 (最后一个心跳 epoch, pid)。文件缺失/损坏返回 (None, None)。"""
    try:
        path = heartbeat_path()
        if not path:
            return None, None
        # jsonl 可能很大（每 5 分钟一行 + 保留 2000 行），从尾部读最后 ~64KB 即可
        size = os.path.getsize(path)
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            if size > 65536:
                f.seek(size - 65536)
                f.readline()  # 丢弃可能被截断的半行
            lines = f.readlines()
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            try:
                return float(rec.get("epoch") or 0.0), int(rec.get("pid") or 0)
            except (TypeError, ValueError):
                continue
        return None, None
    except Exception:
        return None, None


def daemon_lock() -> Optional[Dict[str, Any]]:
    """daemon.lock sentinel（进程启动时写入 pid/started_at）。"""
    return _read_json(DAEMON_LOCK_PATH)


def _user_stop_flag() -> bool:
    """用户 Ctrl+C 主动停止标记是否存在（存在 → 人停我停：不告警、不拉起）。"""
    try:
        return os.path.isfile(USER_STOP_PATH)
    except Exception:
        return False


def _hhmm_in_quiet_window(hhmm: str) -> bool:
    for start, end in QUIET_WINDOWS:
        if start <= hhmm <= end:
            return True
    return False


def _maintenance_on() -> bool:
    mc = _read_json(MASTER_CONTROL_PATH) or {}
    return bool(mc.get("maintenance_mode", False))


# --------------------------------------------------------------------------
# 告警
# --------------------------------------------------------------------------
def _wechat_url() -> str:
    """读 config.yaml 的 notification 段（只读 yaml，不 init 任何数据管道）。"""
    try:
        import yaml  # type: ignore

        with open(os.path.join(ROOT, "config.yaml"), "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        noti = (cfg or {}).get("notification") or {}
        if not bool(noti.get("enabled")):
            return ""
        return str(noti.get("wechat_webhook_url") or "").strip()
    except Exception:
        return ""


def push_wechat(markdown: str) -> bool:
    """直接 POST 企微机器人 webhook（不经 core.notification，避免拉起整套依赖）。"""
    url = _wechat_url()
    if not url:
        _log_line("[watchdog] 未配置企微 webhook，跳过推送")
        return False
    try:
        import urllib.request

        body = json.dumps({"msgtype": "markdown", "markdown": {"content": markdown}}).encode("utf-8")
        req = urllib.request.Request(
            url, data=body, headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8", "ignore")
        ok = '"errcode":0' in raw.replace(" ", "")
        _log_line(f"[watchdog] 企微推送 {'成功' if ok else '失败'}：{raw[:160]}")
        return ok
    except Exception as e:  # noqa: BLE001
        _log_line(f"[watchdog] 企微推送异常：{e}")
        return False


def _state() -> Dict[str, Any]:
    return _read_json(WATCHDOG_STATE_PATH) or {}


def _save_state(st: Dict[str, Any]) -> None:
    _write_json_atomic(WATCHDOG_STATE_PATH, st)


def _cooldown_ok(st: Dict[str, Any], key: str, minutes: float) -> bool:
    last = float(st.get(f"{key}_last_epoch") or 0.0)
    return (time.time() - last) >= minutes * 60.0


def _mark(st: Dict[str, Any], key: str) -> None:
    st[f"{key}_last_epoch"] = time.time()
    st[f"{key}_last_bj"] = now_bj().strftime("%Y-%m-%d %H:%M:%S")


def _log_line(msg: str) -> None:
    try:
        ts = now_bj().strftime("%Y-%m-%d %H:%M:%S")
        os.makedirs(RUNTIME_DIR, exist_ok=True)
        with open(os.path.join(RUNTIME_DIR, "daemon_watchdog.log"), "a", encoding="utf-8") as f:
            f.write(f"{ts} {msg}\n")
    except Exception:
        pass
    try:
        print(msg)
    except Exception:
        pass


# --------------------------------------------------------------------------
# 核心判定
# --------------------------------------------------------------------------
def classify(
    *,
    alive: bool,
    age_min: Optional[float],
    startup_age_min: Optional[float],
    has_heartbeat: bool,
) -> Tuple[str, str]:
    """纯函数状态机（无副作用，便于自检）。

    startup_age_min: 进程启动至今的分钟数（来自 sentinel.started_at）；None 表示未知。
    has_heartbeat : 心跳文件里是否存在可解析记录。
    """
    if not alive and not has_heartbeat:
        return "dead_no_heartbeat", "从未产生心跳（首次部署？）或心跳文件被删"
    if not alive:
        return "dead", "进程不在（sentinel/心跳里的 pid 均不存活）"
    if not has_heartbeat:
        return "stale", "心跳文件里没有可解析记录"

    # 启动宽限：只对「刚起来、还没到第一拍心跳」这种情况生效，且当前心跳确实陈旧
    # （否则进程跑得好好的却被判 starting，会掩盖真实卡死）。
    if (
        startup_age_min is not None
        and startup_age_min < STARTUP_GRACE_MIN
        and age_min is not None
        and age_min > STALE_HEARTBEAT_MIN
    ):
        return (
            "starting",
            f"进程刚启动 {startup_age_min:.1f} 分钟，首拍心跳未落盘（宽限 {STARTUP_GRACE_MIN} 分钟）",
        )

    if age_min is not None and age_min > HUNG_HEARTBEAT_MIN:
        return "hung", f"进程存活但心跳已停 {age_min:.0f} 分钟（>{HUNG_HEARTBEAT_MIN}）"
    if age_min is not None and age_min > STALE_HEARTBEAT_MIN:
        return "stale", f"心跳已停 {age_min:.0f} 分钟（>{STALE_HEARTBEAT_MIN}）"
    return "healthy", ""


def assess() -> Dict[str, Any]:
    """返回诊断字典（不产生副作用）。"""
    hb_epoch, hb_pid = last_heartbeat()
    lock = daemon_lock() or {}
    lock_pid = lock.get("pid")
    alive_by_lock = _pid_alive(lock_pid) if lock_pid else False
    alive_by_hb = _pid_alive(hb_pid) if hb_pid else False
    alive = bool(alive_by_lock or alive_by_hb)

    age_min: Optional[float] = None
    if hb_epoch:
        age_min = max(0.0, (time.time() - hb_epoch) / 60.0)

    # sentinel 的 started_at 是进程一启动就写的，用它算「起多久了」。
    startup_age_min: Optional[float] = None
    lock_started = lock.get("started_at")
    if lock_started is not None:
        try:
            startup_age_min = max(0.0, (time.time() - float(lock_started)) / 60.0)
        except (TypeError, ValueError):
            startup_age_min = None

    status, reason = classify(
        alive=alive,
        age_min=age_min,
        startup_age_min=startup_age_min,
        has_heartbeat=hb_epoch is not None,
    )

    return {
        "status": status,
        "reason": reason,
        "alive": alive,
        "pid": lock_pid or hb_pid,
        "heartbeat_age_min": None if age_min is None else round(age_min, 1),
        "heartbeat_epoch": hb_epoch,
        "heartbeat_pid": hb_pid,
        "lock_pid": lock_pid,
        "lock_started_at": lock.get("started_at"),
        "startup_age_min": None if startup_age_min is None else round(startup_age_min, 1),
        "checked_at": now_bj().strftime("%Y-%m-%d %H:%M:%S"),
        "quiet_window": _hhmm_in_quiet_window(now_bj().strftime("%H:%M")),
        "maintenance_mode": _maintenance_on(),
        "user_stopped": _user_stop_flag(),
    }


def _launch_daemon() -> bool:
    """在新控制台窗口里拉起 start_daemon_24x7.bat（靠它自带的 verify + UI 互斥）。"""
    if not os.path.isfile(START_BAT):
        _log_line(f"[watchdog] 找不到 {START_BAT}，无法自动拉起")
        return False
    try:
        if os.name == "nt":
            # 【P0 修复 2026-11-13】旧写法 ["cmd.exe","/c","start","守护进程(看门狗拉起)",START_BAT]
            # 实际从未启动过 bat：`start` 的第一个参数只有加了引号才会被当成窗口标题，
            # 裸传的中文标题会被当成「要执行的程序名」，而括号在 cmd 里又是特殊字符。
            # 表现极具迷惑性——日志已经打印「已发起拉起」，但 daemon 根本没起来。
            # 改为不经过 `start`：cmd /c + CREATE_NEW_CONSOLE 直接给 bat 一个独立控制台窗口。
            subprocess.Popen(
                ["cmd.exe", "/c", START_BAT, "--unattended"],
                cwd=ROOT,
                creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
            )
        else:
            subprocess.Popen(["/bin/sh", START_BAT], cwd=ROOT, start_new_session=True)
        _log_line("[watchdog] 已发起拉起 start_daemon_24x7.bat")
        return True
    except Exception as e:  # noqa: BLE001
        _log_line(f"[watchdog] 拉起失败：{e}")
        return False


def _kill_pid(pid: Any) -> bool:
    try:
        import psutil  # type: ignore

        p = psutil.Process(int(pid))
        p.terminate()
        try:
            p.wait(timeout=15)
        except Exception:
            p.kill()
        _log_line(f"[watchdog] 已终止卡死进程 pid={pid}")
        return True
    except Exception as e:  # noqa: BLE001
        _log_line(f"[watchdog] 终止进程失败 pid={pid}: {e}")
        return False


def _format_alert(diag: Dict[str, Any]) -> str:
    st = diag["status"]
    emoji = {"dead": "💀", "dead_no_heartbeat": "💀", "hung": "🧊",
             "stale": "⚠️", "starting": "⏳"}.get(st, "⚠️")
    title = {
        "dead": "守护进程已停止",
        "dead_no_heartbeat": "守护进程无心跳记录",
        "hung": "守护进程疑似卡死",
        "stale": "守护进程心跳异常",
        "starting": "守护进程启动中",
    }.get(st, "守护进程异常")
    age = diag.get("heartbeat_age_min")
    lines = [
        f"## {emoji} {title}",
        "",
        f"> 判定：**{st}** — {diag.get('reason')}",
        f"> 检查时间：{diag.get('checked_at')}",
        f"> 心跳距今：{'—' if age is None else f'{age:.0f} 分钟'} | pid：{diag.get('pid')}",
        "",
    ]
    if st in ("dead", "dead_no_heartbeat"):
        lines.append("**影响**：盘中不扫描、不推送、模拟盘不监控持仓（止损不会执行）。")
    elif st == "hung":
        lines.append("**影响**：进程还在但主循环可能已阻塞，扫描/止损都在推迟。")
    else:
        lines.append("**影响**：心跳稀疏，尚未确认停摆；先观察，若持续则按 dead 处理。")
    lines.append("")
    lines.append("**处置**：看门狗已尝试自动拉起（见 daemon_watchdog.log）；"
                 "若反复重启失败，请检查 sniper.log 末尾报错与 DuckDB 锁。")
    lines.append("")
    lines.append("> 本告警由外置看门狗发出（每 10 分钟检查一次，同类告警 60 分钟冷却）。")
    return "\n".join(lines)


def run_once(*, allow_restart: bool = True, kill_hung: bool = False) -> Dict[str, Any]:
    """执行一次检查；返回 (诊断 + 已采取动作)。"""
    diag = assess()
    st = _state()
    actions: List[str] = []

    # 维护窗口 / 维护模式：只记录，不判定也不拉起
    if diag["quiet_window"] or diag["maintenance_mode"]:
        actions.append("skipped_quiet_or_maintenance")
        diag["actions"] = actions
        _save_state({**st, "last_check_bj": diag["checked_at"], "last_status": diag["status"],
                     "last_action": actions[-1]})
        _log_line(f"[watchdog] 静默窗口/维护模式，跳过判定 | status={diag['status']}")
        return diag

    # 【2026-11-15】用户 Ctrl+C 主动停止：保持「人停我停」，不告警也不自动拉起。
    # 标记由 auto_sniper_daemon.py 在 KeyboardInterrupt 时写入；
    # 人工重新双击 start_daemon_24x7.bat（非 --unattended）时清除后恢复自愈。
    if diag.get("user_stopped"):
        actions.append("skipped_user_stopped")
        diag["actions"] = actions
        _save_state({**st, "last_check_bj": diag["checked_at"],
                     "last_status": diag["status"],
                     "last_action": actions[-1], "user_stopped": True})
        _log_line(
            f"[watchdog] 检测到「用户 Ctrl+C 主动停止」标记 → 不告警/不拉起 | status={diag['status']}"
        )
        return diag

    # starting = 刚拉起、首拍心跳还没落盘。既不该告警，也不该再拉起（正在启动中）。
    if diag["status"] == "starting":
        _save_state({**st, "last_check_bj": diag["checked_at"], "last_status": "starting",
                     "last_action": "", "starting_since_epoch": time.time()})
        _log_line(
            f"[watchdog] 启动宽限中 | {diag['reason']} | 心跳 {diag['heartbeat_age_min']} 分钟前 | pid={diag['pid']}"
        )
        diag["actions"] = actions
        return diag

    if diag["status"] == "healthy":
        _save_state({**st, "last_check_bj": diag["checked_at"], "last_status": "healthy",
                     "last_action": "", "healthy_since_epoch": time.time()})
        _log_line(f"[watchdog] 健康 | 心跳 {diag['heartbeat_age_min']} 分钟前 | pid={diag['pid']}")
        diag["actions"] = actions
        return diag

    # ---- 异常：告警（带冷却）----
    alert_key = f"alert_{diag['status']}"
    if _cooldown_ok(st, alert_key, ALERT_COOLDOWN_MIN):
        if push_wechat(_format_alert(diag)):
            actions.append("alerted")
            _mark(st, alert_key)
    else:
        actions.append("alert_suppressed_by_cooldown")

    # ---- 卡死：可选 kill（默认关闭，风险高于收益）----
    if diag["status"] == "hung" and kill_hung and diag.get("pid"):
        if _kill_pid(diag["pid"]):
            actions.append("killed_hung")
            time.sleep(5)
            diag["alive"] = False

    # ---- 死亡：自动拉起（带冷却）----
    need_restart = (not diag["alive"]) or diag["status"] in ("dead", "dead_no_heartbeat")
    if need_restart and allow_restart:
        if _cooldown_ok(st, "restart", RESTART_COOLDOWN_MIN):
            if _launch_daemon():
                actions.append("restarted")
                _mark(st, "restart")
            else:
                actions.append("restart_failed")
        else:
            actions.append("restart_suppressed_by_cooldown")
    elif need_restart:
        actions.append("restart_disabled_by_flag")

    diag["actions"] = actions
    _save_state({
        **st,
        "last_check_bj": diag["checked_at"],
        "last_status": diag["status"],
        "last_action": ",".join(actions),
        "last_reason": diag["reason"],
    })
    return diag


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def _print_status(diag: Dict[str, Any]) -> None:
    print("=== 守护进程看门狗诊断 ===")
    for k in ("status", "reason", "alive", "pid", "heartbeat_age_min", "startup_age_min",
              "lock_started_at", "checked_at", "quiet_window", "maintenance_mode",
              "user_stopped", "actions"):
        if k in diag:
            print(f"  {k:20s}: {diag[k]}")


def _self_test() -> None:
    """纯逻辑自检：状态机 + 冷却 + 静默窗口（不碰网络、不碰数据库）。"""
    # 静默窗口
    assert _hhmm_in_quiet_window("20:30") is True
    assert _hhmm_in_quiet_window("20:26") is True
    assert _hhmm_in_quiet_window("10:30") is False
    assert _hhmm_in_quiet_window("21:20") is True
    assert _hhmm_in_quiet_window("21:21") is False
    # 冷却
    st = {}
    assert _cooldown_ok(st, "restart", 20) is True
    _mark(st, "restart")
    assert _cooldown_ok(st, "restart", 20) is False
    assert _cooldown_ok(st, "restart", 0) is True
    # PID 判定（当前进程必然存活；明显不存在的 pid 必须判死）
    assert _pid_alive(os.getpid()) is True
    assert _pid_alive(999999) is False
    assert _pid_alive(None) is False
    assert _pid_alive("abc") is False
    # 告警文案：必须说清影响与处置
    body = _format_alert({
        "status": "dead", "reason": "进程不在", "checked_at": "2026-11-13 21:00:00",
        "heartbeat_age_min": 4320.0, "pid": 1234,
    })
    assert "守护进程已停止" in body and "止损不会执行" in body and "自动拉起" in body

    # ---- 状态机：启动宽限（2026-11-13 假 hung 事故的回归测试）----
    # 刚起 1 分钟、心跳还是上次停摆时的旧值 → 必须判 starting，不能判 hung
    assert classify(alive=True, age_min=4400.0, startup_age_min=1.0,
                    has_heartbeat=True)[0] == "starting"
    # 宽限期边界：刚好在宽限内算 starting，超出宽限就该回到 hung
    assert classify(alive=True, age_min=4400.0,
                    startup_age_min=STARTUP_GRACE_MIN - 0.1, has_heartbeat=True)[0] == "starting"
    assert classify(alive=True, age_min=4400.0,
                    startup_age_min=STARTUP_GRACE_MIN + 0.1, has_heartbeat=True)[0] == "hung"
    # 宽限不能掩盖真实卡死：心跳是新鲜的，即使刚启动也必须判 healthy
    assert classify(alive=True, age_min=1.0, startup_age_min=1.0,
                    has_heartbeat=True)[0] == "healthy"
    # 启动信息缺失（老 sentinel 没有 started_at）时退回按心跳判定，不能报错
    assert classify(alive=True, age_min=4400.0, startup_age_min=None,
                    has_heartbeat=True)[0] == "hung"
    # 进程已死：无论启动多久都必须判 dead（宽限期不适用于死亡判定）
    assert classify(alive=False, age_min=1.0, startup_age_min=1.0,
                    has_heartbeat=True)[0] == "dead"
    assert classify(alive=False, age_min=None, startup_age_min=None,
                    has_heartbeat=False)[0] == "dead_no_heartbeat"
    print("[daemon_watchdog] self-test OK | 状态机/启动宽限/冷却/静默窗口/告警文案 verified")


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="守护进程外置看门狗（死亡告警 + 自动拉起）")
    ap.add_argument("--no-restart", action="store_true", help="只告警，不自动拉起")
    ap.add_argument("--kill-hung", action="store_true", help="卡死时先 kill 再拉起（默认关闭）")
    ap.add_argument("--status", action="store_true", help="只打印诊断，不告警不拉起")
    ap.add_argument("--self-test", action="store_true", help="运行纯逻辑自检")
    args = ap.parse_args(argv)

    if args.self_test:
        _self_test()
        return 0
    if args.status:
        _print_status(assess())
        return 0

    diag = run_once(allow_restart=not args.no_restart, kill_hung=args.kill_hung)
    _print_status(diag)
    # starting 是正常中间态（刚拉起、首拍心跳未落盘）：不算故障，退出码必须是 0，
    # 否则计划任务会把它记成 last result 非零，和真故障混在一起。
    return 0 if diag["status"] in ("healthy", "starting") or diag.get("actions") else 1


if __name__ == "__main__":
    raise SystemExit(main())
