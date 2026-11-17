# -*- coding: utf-8 -*-
"""
小杰AI选股系统 Pro V26.88 — 家用服务器 24h 无人值守守护进程（调度 + 交易日屏障 + 企微推送）
【V26.88 交易时间优化】非交易时段自动进入低频休眠模式，减少资源消耗和日志噪音。
【V26.88】每日 20:30 在 P1/P5 完成后执行 DuckDB 安全压缩（CHECKPOINT+VACUUM，不中断守护）。
【V26.88 新增资金记忆体系 · 第三阶段】：日线 fund_memory_score 由 data_fetcher 夜间/早盘增量管道 `_sync_daily_features()` 维护；本进程仅调度同步/P1，不直接计算该列。

职责概览：
- 仅交易日执行：数据同步、P1 重建、分时六槽快照、2档·竞价池、3档·盘中池、4档·盘尾池扫描与企微 Markdown 推送（与 Streamlit 解耦）。
- 分时快照与 **3档·盘中池/4档·盘尾池异步扫描队列**（`process_one_pending_scan_job`）均在本进程内调度；勿再并行启动其它日线/快照/队列守护。
- 【时序铁律】**19:45** 仅日线增量（已最新则跳过）；**19:55** P1 全量重建 + ≥65 推送；**20:05** P5 盘后扫描（独立定时，依赖当日同步成功且 P1 已落盘）。
- 【资源档】`config.yaml` → `server.daemon_profile`：`standard`（默认）| `low`（老至强/小内存：3档·盘中池降频、异步扫描队列轮询拉长，见 `_apply_daemon_resource_profile`）。
- 【维护避让】`master_control.json` → `maintenance_mode: true` 时：主循环休眠轮询；增量同步/P1/扫描/快照/异步队列/stock_basic/每周 VACUUM 等核心写路径由 `_maintenance_mode_skip` 主动跳过（见 `core.master_control.is_maintenance_mode_enabled`）。
- 【早盘编排】交易日 08:50 采用单一编排入口：先清空企微防刷字典，再执行早盘补位链（同步 / 条件性补 P1）。若当日 p1_cache_YYYYMMDD.json 尚不存在，则同步成功后立即补建 P1，避免周一早盘仍只挂载周五 JSON 却无「当日文件名」的认知与数据断层（K 线仍以 DB 最新交易日为准）。
- 【早盘合并简报】交易日 **09:18** 仅由 `daily_open_heartbeat_routine()` 统一发送一条 Markdown：原早安问候（推送/巡航开关）+ 数据库探活 + ``risk_control.ui_alert_only`` 风控说明 + 巡航待命；启动补发与定时任务共用同一入口，并通过进程内锁 + pipeline_state 日标记双重去重，保证同日仅一条。预检失败则跳过企微简报并尝试一条运维告警（按日去重）。
- 【时区锚定】模块首部设置 TZ=Asia/Shanghai 并在 Unix 上 tzset，与 ZoneInfo(BJ) 双保险。

【调度哲学：尾盘重仓、盘中降频 · V26.88 第二阶段】
- P4 尾盘五拍（14:31 / 14:36 / 14:41 / 14:46 / 14:51）一律 **wait_for_lock_sec=900**（15 分钟）：工作线程内等锁，
  主循环 `schedule.run_pending` **永不阻塞**。其中 **14:40–14:55** 为「重仓核心窗」——与盘中降频的 P3 绝对互斥。
- P3 **大幅降频**（默认每 5 分钟整分对齐一拍，可由 ``server.daemon_profile`` / ``daemon_p3_poll_interval_minutes`` 调整）；仅非阻塞抢锁。
  自 **14:31** 起（含）进入「P4 优先走廊」：**禁止**再派生 P3 线程（O(1) return），杜绝与 P4 争 _SCAN_BUSY。
- 19:45 仅日线增量（可跳过）→ 19:55 P1+推送 → 20:05 P5 → 20:30 安全压缩；错过整分会在窗口内补跑

【分时快照】仅调度六槽（935/1030/1125/1325/1425/1440）量能与 VR 锚点写入 JSON，供 P3/P4 等链路读取。

【不死性说明·锁与 finally】
- 子线程内对 _SCAN_BUSY 采用「acquired 标志 + finally 内安全 release」。
- P4 尾盘使用 **最长 900s** 等锁；P5/晚间/早盘仍用较长超时；P3 **仅非阻塞抢锁**，且尾盘重叠窗内不调度。

依赖：pip install schedule requests
运行前请将工作目录设为项目根目录，或从项目根执行：python auto_sniper_daemon.py
"""
from __future__ import annotations

# ---------- 时区强锚定：须在依赖本地时间语义的逻辑之前（Linux/Unix 上 tzset 刷新 libc 时区表）----------
import os

os.environ["XIAOJIE_DAEMON_MODE"] = "1"
import time

# 【时区锚定·平台安全】仅在 Unix 上设置进程级 TZ 并调用 tzset()。
# Windows 下 time.tzset 不存在；设置 TZ=Asia/Shanghai 反而会让 MSVCRT 用 POSIX 规则
# 解析 "Asia/Shanghai" 失败并回退 UTC，导致裸 datetime.now() 整体偏移 -8 小时。
# Windows 直接依赖系统时区（中国标准时间）+ 显式 ZoneInfo("Asia/Shanghai") 双保险。
if os.name != "nt":
    os.environ["TZ"] = "Asia/Shanghai"
    if hasattr(time, "tzset"):
        try:
            time.tzset()  # type: ignore[attr-defined]
        except Exception:
            pass

import gc
import html
import io
import json
import logging
import re
import subprocess
import sys
import tempfile
import shutil
import threading
import traceback
import atexit
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

# ---------- 项目根路径 ----------
_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))

# 【2026-11-15】「用户 Ctrl+C 主动停止」标记文件。
# 语义：Ctrl+C = 用户明确要求下线，任何机制都不许自动重启：
#   - start_daemon_24x7.bat 循环见标记 → 不再 30 秒重启，直接退出；
#   - 外置看门狗 daemon_watchdog.py 见标记 → 不告警、不拉起；
#   - 人工双击 start_daemon_24x7.bat（非 --unattended）时清除标记，恢复自愈。
# 只在 KeyboardInterrupt 钩子写：taskkill/强杀/崩溃不写，
# 保证「崩溃自愈」原行为不变，仅「人工停止」获得持久免重启。
_USER_STOP_FLAG = os.path.join(_PROJECT_ROOT, "data", "runtime", "state", "user_manual_stop.json")


def _write_user_stop_flag(reason: str = "console_ctrl_c") -> None:
    try:
        os.makedirs(os.path.dirname(_USER_STOP_FLAG), exist_ok=True)
        tmp = _USER_STOP_FLAG + ".tmp"
        payload = {
            "epoch": time.time(),
            "bj": datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S"),
            "pid": os.getpid(),
            "reason": reason,
        }
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _USER_STOP_FLAG)
    except Exception:
        pass
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
os.chdir(_PROJECT_ROOT)


def _force_bj_system_timezone() -> None:
    """尽量把进程级系统时区锚定到北京时间，避免日志/本地时间显示混乱。

    Windows：无 tzset，且设置 TZ 环境变量会让 MSVCRT 回退 UTC（偏移 -8h），
    因此这里只在 Unix 上写 TZ + tzset()；Windows 依赖系统时区。
    """
    if os.name == "nt":
        os.environ.pop("TZ", None)
        return
    os.environ["TZ"] = "Asia/Shanghai"
    if hasattr(time, "tzset"):
        try:
            time.tzset()  # type: ignore[attr-defined]
        except Exception:
            pass


_force_bj_system_timezone()

# 【SAC 兜底】Win11 Smart App Control 会拒载 pyarrow._compute.pyd（WinError 4551），导致 pandas
# 启动时自动 try-import pyarrow.compute 时炸掉，daemon 整个挂掉。在 import pandas 之前先把
# pyarrow 屏蔽掉，pandas 自动 fallback 到 NumPy backend，DataFrame 操作不受影响。
# 彻底修复仍需关闭 SAC：见 core/_pyarrow_compat.py 模块文档。
from core import _pyarrow_compat as _pyarrow_compat  # noqa: E402
_pyarrow_compat.disable_pyarrow()

import pandas as pd  # noqa: E402
import duckdb  # noqa: E402  # 晚间VACUUM的EXPORT/IMPORT需要duckdb原生连接

# 分时快照统一入口（实现位于 scan_engine，此处 re-export 避免循环 import）
from core.intraday_snapshot_scheduler import capture_intraday_snapshots  # noqa: E402

try:
    import schedule
except ImportError as e:
    print("请先安装: pip install schedule", file=sys.stderr)
    raise e

from core.log_config import setup_sniper_rotating_file_logging  # noqa: E402

# 开启日志轮转（sniper.log 50MB×10），防止 7x24 纯后台模式下日志无限膨胀；详见 core/log_config.py
setup_sniper_rotating_file_logging(level=logging.INFO)
logger = logging.getLogger("auto_sniper_daemon")


class _SuppressStreamlitContextWarnings(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            msg = str(getattr(record, "msg", "") or "")
        if "missing ScriptRunContext" in msg:
            return False
        if "Session state does not function when running a script without `streamlit run`" in msg:
            return False
        return True


for _name in (
    "streamlit.runtime.scriptrunner_utils.script_run_context",
    "streamlit.runtime.state.session_state_proxy",
):
    logging.getLogger(_name).addFilter(_SuppressStreamlitContextWarnings())

BJ_TZ = ZoneInfo("Asia/Shanghai")
_BJ_SCHEDULE_LOCK = threading.Lock()
_BJ_DAILY_JOBS: List[Tuple[str, Callable[[], None], str, int]] = []
_BJ_LAST_TICK_MINUTE_KEY = ""
_BJ_DISPATCHED_JOB_KEYS: set[str] = set()
# 早盘合并简报去重锁：防止 09:18 定时任务与启动补发并发重入导致重复发送。
_OPEN_HEARTBEAT_LOCK = threading.Lock()
_DUCKDB_LOCK_ALERT_TS: Dict[str, float] = {}
_ACTIVE_SPAWN_LOCK = threading.Lock()
_ACTIVE_SPAWN_NAMES: set[str] = set()
# 进程级单实例锁：防止 Windows 任务计划 / 手工启动 / 看门狗重复拉起多个 daemon 实例。
_DAEMON_INSTANCE_LOCK_PATH = os.path.join(_PROJECT_ROOT, "data", "runtime", "state", "auto_sniper_daemon.lock")
_DAEMON_INSTANCE_LOCK_FD: Optional[int] = None
# 维护锁开启时主循环每 10s 休眠一轮；日志降为「进入时 + 至多每 5 分钟一条」，避免 7x24 误开维护把磁盘刷满
_MAINT_LOOP_LOG_LAST_MONO: Optional[float] = None
# 运行时 JSON / 缓存清理节拍
_RUNTIME_CLEANUP_LAST_HOUR_KEY = ""


# ========================== 【V26.88 交易时间智能休眠】 ==========================
# A股交易时间窗口：
#   早盘：09:15-09:25（集合竞价）、09:30-11:30（连续竞价）
#   午盘：13:00-15:00（连续竞价，其中14:57-15:00为尾盘集合竞价）
# 非交易时段（盘间休息、周末、节假日）采用低频轮询，减少资源消耗和日志噪音
#
# 【重要】交易日判断复用系统已有的 is_today_trading_day_sse()，确保与定时任务屏障一致

_OFF_MARKET_LOG_LAST_MONO: Optional[float] = None
_IS_IN_TRADING_WINDOW_CACHE: Optional[Tuple[bool, str]] = None
_TRADING_WINDOW_CACHE_TS: float = 0.0
_TRADING_WINDOW_CACHE_TTL_SEC = 5.0  # 5秒缓存


def _is_in_trading_window(bj_now: Optional[datetime] = None) -> Tuple[bool, str]:
    """
    判断当前是否在交易时段内（结合真实交易日 + 时段）。

    返回: (is_trading, status)
    - is_trading: True 表示正在交易或集合竞价窗口
    - status: 当前状态描述（用于日志）

    【重要】复用 is_today_trading_day_sse() 确保与定时任务屏障一致
    """
    global _IS_IN_TRADING_WINDOW_CACHE, _TRADING_WINDOW_CACHE_TS

    now = bj_now if bj_now is not None else _now_bj()
    now_ts = time.mktime(now.timetuple())

    # 缓存检查（5秒内）
    if (_IS_IN_TRADING_WINDOW_CACHE is not None and
        (now_ts - _TRADING_WINDOW_CACHE_TS) < _TRADING_WINDOW_CACHE_TTL_SEC):
        return _IS_IN_TRADING_WINDOW_CACHE

    # ===== 第一步：判断是否为交易日 =====
    # 复用系统已有的交易日判断，确保与定时任务屏障一致
    is_trading_day = False
    try:
        # 避免循环导入：延迟到函数内部导入
        is_trading_day = is_today_trading_day_sse()
    except NameError:
        # 启动早期（模块加载阶段）可能尚未定义，使用本地简单判断兜底
        if now.weekday() < 5:  # 周一到周五
            is_trading_day = True
    except Exception:
        # 异常时保守返回非交易日
        _IS_IN_TRADING_WINDOW_CACHE = (False, f"交易日判断异常({now.strftime('%H:%M:%S')})")
        _TRADING_WINDOW_CACHE_TS = now_ts
        return _IS_IN_TRADING_WINDOW_CACHE

    # 非交易日直接返回
    if not is_trading_day:
        weekday_cn = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][now.weekday()]
        _IS_IN_TRADING_WINDOW_CACHE = (False, f"非交易日({weekday_cn})")
        _TRADING_WINDOW_CACHE_TS = now_ts
        return _IS_IN_TRADING_WINDOW_CACHE

    # ===== 第二步：判断交易时段 =====
    hour = now.hour
    minute = now.minute
    time_val = hour * 60 + minute  # 转换为分钟数便于比较

    # 交易时段判断（北京时间）
    # 集合竞价 09:15-09:25（已含在 is_trading=True）
    PREOPEN_START = 9 * 60 + 15   # 555
    PREOPEN_END   = 9 * 60 + 25   # 565
    # 早盘连续竞价
    MORNING_START = 9 * 60 + 30   # 570
    MORNING_END   = 11 * 60 + 30  # 690
    # 午盘连续竞价（含尾盘集合竞价 14:57-15:00）
    AFTERNOON_START = 13 * 60      # 780
    AFTERNOON_END   = 15 * 60     # 900
    # 尾盘竞价（14:57-15:00，is_trading=True 的延伸）
    CLOSE_AUCTION_START = 14 * 60 + 57  # 897
    CLOSE_AUCTION_END   = 15 * 60       # 900

    is_trading = False
    status = "未知"

    if PREOPEN_START <= time_val <= PREOPEN_END:
        is_trading = True
        status = "集合竞价(09:15-09:25)"
    elif MORNING_START <= time_val <= MORNING_END:
        is_trading = True
        status = "早盘连续竞价"
    elif CLOSE_AUCTION_START <= time_val <= CLOSE_AUCTION_END:
        is_trading = True
        status = "尾盘竞价(14:57-15:00)"
    elif AFTERNOON_START <= time_val < CLOSE_AUCTION_START:
        is_trading = True
        status = "午盘连续竞价"
    elif time_val < PREOPEN_START:
        status = f"盘前等待({hour}:{minute:02d})"
    elif PREOPEN_END < time_val < MORNING_START:
        status = f"早间休市({hour}:{minute:02d})"
    elif MORNING_END < time_val < AFTERNOON_START:
        status = f"午间休市({hour}:{minute:02d})"
    elif time_val > AFTERNOON_END:
        status = f"盘后结束({hour}:{minute:02d})"

    _IS_IN_TRADING_WINDOW_CACHE = (is_trading, status)
    _TRADING_WINDOW_CACHE_TS = now_ts

    return is_trading, status


def _get_smart_sleep_duration(bj_now: Optional[datetime] = None) -> float:
    """
    根据当前交易状态返回智能休眠时长。

    交易时段：1秒（高频轮询定时任务）
    盘后 19:00-24:00 / 盘前 00:00-09:00：300秒（5分钟低频轮询；隔夜增量同步只需小时级）
    09:00-09:15 / 11:30-13:00 / 15:00-15:30：30秒（盘后数据处理期；中等频轮询）
    09:15-15:00 盘中：1秒（高频）
    """
    is_trading, status = _is_in_trading_window(bj_now)
    if is_trading:
        return 1.0
    bj = bj_now or datetime.now(BJ_TZ)
    hhmm = bj.hour * 60 + bj.minute
    # 盘前 00:00-09:00 与盘后 19:00-24:00 → 长休眠 5 分钟
    if hhmm < 9 * 60 or hhmm >= 19 * 60:
        return 300.0
    # 其他时段（09:00-09:15, 11:30-13:00, 15:00-15:30）→ 中等休眠
    return 30.0


def _should_log_off_market() -> bool:
    """
    控制非交易时段日志频率：每300秒最多打印一条。
    """
    global _OFF_MARKET_LOG_LAST_MONO
    now_mono = time.monotonic()

    if _OFF_MARKET_LOG_LAST_MONO is None:
        _OFF_MARKET_LOG_LAST_MONO = now_mono
        return True

    if (now_mono - _OFF_MARKET_LOG_LAST_MONO) >= 300.0:
        _OFF_MARKET_LOG_LAST_MONO = now_mono
        return True

    return False


# ========================== 【V26.88 每日主动退出】 ==========================
# 目标：在长周期 7×24 运行时，定期以干净状态重启进程，释放 Python 进程级内存（含 DuckDB 连接、
# Pandas DataFrame 句柄、numpy 缓存等）；由 start_daemon_24x7.bat 看门狗自动重新拉起。
#
# 配置路径：config.yaml → server.daily_restart_hours（小时数，None=关闭，等效 0）
# 默认：None（关闭，保持向后兼容）；设为 0 表示每次运行都退出（调试用）；正常值如 18（交易日午夜前退出）

_DAILY_RESTART_HOURS: Optional[float] = None  # 运行时读取自 yaml
_DAEMON_START_MONO: float = 0.0            # 进程启动时单调时钟（秒）


def _load_daily_restart_config() -> Optional[float]:
    """
    从 config.yaml → server.daily_restart_hours 读取每日重启阈值。
    返回 None 表示关闭；返回 float 表示运行 N 小时后应退出。
    默认 None（关闭，不改变旧行为）。
    """
    try:
        import yaml
        cfg_path = os.path.join(_PROJECT_ROOT, "config.yaml")
        if not os.path.isfile(cfg_path):
            return None
        with open(cfg_path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        srv = raw.get("server") if isinstance(raw, dict) else None
        if not isinstance(srv, dict):
            return None
        raw_val = srv.get("daily_restart_hours")
        if raw_val is None:
            return None
        hours = float(raw_val)
        # 显式 0=关闭（兼容 None）
        if hours <= 0:
            return None
        # 合理上限 72h，防止误配置导致频繁重启
        return max(0.5, min(72.0, hours))
    except Exception as e:
        logger.debug("读取 server.daily_restart_hours 失败，使用关闭状态: %s", e)
        return None


def _check_daily_restart() -> Tuple[bool, str, Optional[float]]:
    """
    检查是否应执行每日主动退出。

    返回 (should_exit, reason, seconds_until_exit)：
    - should_exit=True 表示主循环应在下一次迭代前退出
    - reason 为可读退出原因，供日志和企微告警使用
    - seconds_until_exit 为距退出还剩多少秒（供企微提示用）；None 表示已触发立即退出
    """
    if _DAILY_RESTART_HOURS is None or _DAILY_RESTART_HOURS <= 0:
        return False, "", None
    if _DAEMON_START_MONO <= 0:
        return False, "", None
    elapsed = time.monotonic() - _DAEMON_START_MONO
    threshold = _DAILY_RESTART_HOURS * 3600.0
    remaining = threshold - elapsed
    if remaining <= 0:
        try:
            bj_now = datetime.now(BJ_TZ)
            exit_time_str = bj_now.strftime("%Y-%m-%d %H:%M:%S")
            reason = (
                f"【每日主动退出】已运行 {elapsed / 3600:.1f}h（阈值 {_DAILY_RESTART_HOURS}h），"
                f"进程 {os.getpid()} 于 {exit_time_str} 主动退出以释放内存，"
                f"看门狗将在约 60s 后自动重新拉起。"
            )
        except Exception:
            reason = (
                f"【每日主动退出】已运行 {elapsed / 3600:.1f}h（阈值 {_DAILY_RESTART_HOURS}h），"
                f"进程 {os.getpid()} 主动退出，看门狗将在约 60s 后自动重新拉起。"
            )
        return True, reason, None
    return False, "", remaining



def _daemon_cruise_off_skip(task_label: str) -> bool:
    """
    物理总控「全自动巡航」关闭时返回 True，调用方应立即 return，不得下载/扫描/重建。
    读取失败时视为开启（与 master_control 缺省 True 一致），避免误伤生产。
    """
    try:
        from core.master_control import is_daemon_auto_cruise_enabled

        if not is_daemon_auto_cruise_enabled():
            logger.info(
                "[Daemon 休眠中] 总控开关已关闭 (master_control.json)，跳过: %s",
                task_label,
            )
            return True
    except Exception as e:
        logger.debug("master_control 读取失败，Daemon 继续运行: %s", e)
    return False


def _maintenance_mode_skip(task_label: str) -> bool:
    """
    维护模式「主动避让」刹车：总控 maintenance_mode 为 True 时，跳过本任务对应的核心写/扫路径。
    返回 True 表示应跳过；False 表示可继续（再经巡航/交易日等闸门）。
    """
    try:
        from core.master_control import is_maintenance_mode_enabled

        if is_maintenance_mode_enabled():
            logger.warning(
                f"⚠️ 发现全局维护锁，守护进程主动让路暂停 | task={task_label}"
            )
            return True
    except Exception as e:
        logger.debug("维护模式检测失败（视为非维护）: %s", e)
    return False


def _notify_daemon_alert(
    title: str,
    detail: str,
    *,
    category: str = "daemon",
    dedup_key: Optional[str] = None,
) -> None:
    """守护进程内数据/扫描异常 → 企微运维提示（与 UI 共用网关）。"""
    try:
        from core.config_manager import get_daemon_alert_silence_config

        cfg = get_daemon_alert_silence_config()
        title_s = str(title or "")
        detail_s = str(detail or "")
        if bool(cfg.get("enabled", True)):
            whitelist = cfg.get("whitelist_keywords") or []
            if any(k and (k in title_s or k in detail_s) for k in whitelist):
                logger.info("已按白名单放行守护告警 | category=%s title=%s", category, title_s)
            else:
                if category in set(cfg.get("blacklist_categories") or []):
                    logger.info("已按类别静默守护告警 | category=%s title=%s detail=%s", category, title_s, detail_s)
                    return
                if any(k and (k in title_s or k in detail_s) for k in (cfg.get("silence_keywords") or [])):
                    logger.info("已按关键字静默守护告警 | category=%s title=%s detail=%s", category, title_s, detail_s)
                    return
        from core.notification_gateway import notify_wechat_system_alert

        notify_wechat_system_alert(title=title, detail=detail, category=category, dedup_key=dedup_key)
    except Exception as e:
        logger.debug("企微守护进程告警发送失败（已忽略）: %s", e)


def _acquire_single_instance_lock() -> bool:
    """获取进程级单实例锁，失败则说明已有 daemon 在运行。"""
    global _DAEMON_INSTANCE_LOCK_FD
    try:
        os.makedirs(os.path.dirname(_DAEMON_INSTANCE_LOCK_PATH), exist_ok=True)
        fd = os.open(_DAEMON_INSTANCE_LOCK_PATH, os.O_RDWR | os.O_CREAT, 0o666)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except Exception:
            os.close(fd)
            return False
        _DAEMON_INSTANCE_LOCK_FD = fd
        try:
            os.ftruncate(fd, 0)
            os.write(fd, str(os.getpid()).encode("utf-8"))
            os.fsync(fd)
        except Exception:
            pass
        # 【V26.88+ 10-14】写 orchestrator sentinel，让 start_server.bat 启动 UI 前
        # 能正确探测到 daemon 真在跑（PID 是 daemon 自身，不是 watchdog 子进程）。
        try:
            from core.process_orchestrator import acquire_my_sentinel, DAEMON_LOCK_FILE
            acquire_my_sentinel(DAEMON_LOCK_FILE, 'auto_sniper_daemon.py')
        except Exception as _e_sentinel:
            logger.debug("[orchestrator] 写 daemon sentinel 失败: %s", _e_sentinel)
        return True
    except Exception as e:
        logger.warning("单实例锁初始化失败，仍继续启动以免误伤: %s", e)
        return True


def _release_single_instance_lock() -> None:
    global _DAEMON_INSTANCE_LOCK_FD
    fd = _DAEMON_INSTANCE_LOCK_FD
    _DAEMON_INSTANCE_LOCK_FD = None
    if fd is None:
        return
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)
    except Exception:
        pass
    try:
        os.close(fd)
    except Exception:
        pass
    # 【V26.88+ 10-14】释放 orchestrator sentinel（正常退出路径）
    try:
        from core.process_orchestrator import release_my_sentinel, DAEMON_LOCK_FILE
        release_my_sentinel(DAEMON_LOCK_FILE)
    except Exception:
        pass


def _cleanup_runtime_caches() -> None:
    """定期清理运行时 JSON / 账本 / 临时快照，避免 24/7 长跑堆积。"""
    global _RUNTIME_CLEANUP_LAST_HOUR_KEY
    now = _now_bj()
    hour_key = now.strftime("%Y%m%d%H")
    if hour_key == _RUNTIME_CLEANUP_LAST_HOUR_KEY:
        return
    _RUNTIME_CLEANUP_LAST_HOUR_KEY = hour_key
    try:
        from core.runtime_data_paths import (
            path_alert_dedup_cache_json,
            path_intraday_snapshots_json,
            path_scan_async_pending_json,
            path_scan_async_running_json,
            path_scan_async_status_json,
            path_scan_async_latest_result_json,
            path_wechat_signal_dedup_cache_json,
            path_wash_metrics_json,
        )
    except Exception:
        return
    try:
        import glob

        from core.runtime_data_paths import POOL_CACHE_DIR

        keep_days = 14
        cutoff = int((now - pd.Timedelta(days=keep_days)).strftime("%Y%m%d"))
        for fp in glob.glob(os.path.join(POOL_CACHE_DIR, "p[01]_cache_*.json")):
            base = os.path.basename(fp)
            m = re.search(r"_(\d{8})\.json$", base)
            if not m:
                continue
            try:
                if int(m.group(1)) < cutoff:
                    os.remove(fp)
            except Exception:
                pass
        for fp in glob.glob(os.path.join(POOL_CACHE_DIR, "p[01]_cache_*.pkl")):
            base = os.path.basename(fp)
            m = re.search(r"_(\d{8})\.pkl$", base)
            if not m:
                continue
            try:
                if int(m.group(1)) < cutoff:
                    os.remove(fp)
            except Exception:
                pass

        for p in (path_alert_dedup_cache_json(), path_wechat_signal_dedup_cache_json()):
            if os.path.isfile(p) and os.path.getsize(p) > 5 * 1024 * 1024:
                bak = f"{p}.bak_{now.strftime('%Y%m%d%H%M')}"
                try:
                    os.replace(p, bak)
                except Exception:
                    pass

        for p in (path_scan_async_pending_json(), path_scan_async_running_json(), path_scan_async_status_json(), path_scan_async_latest_result_json()):
            if os.path.isfile(p) and os.path.getsize(p) > 2 * 1024 * 1024:
                try:
                    os.remove(p)
                except Exception:
                    pass

        wm = path_wash_metrics_json()
        if os.path.isfile(wm) and os.path.getsize(wm) > 10 * 1024 * 1024:
            bak = f"{wm}.bak_{now.strftime('%Y%m%d%H%M')}"
            try:
                os.replace(wm, bak)
            except Exception:
                pass
    except Exception as e:
        logger.debug("运行时缓存清理失败(忽略): %s", e)


# 全任务互斥（扫描 / 晚间链 / 早盘链串行，避免 DuckDB 与底仓逻辑并发踩踏）
_SCAN_BUSY = threading.Lock()
_CAL_MEM_LOCK = threading.Lock()

# ---------- 尾盘优先走廊：14:35 起 P3 彻底让路；P4 在 14:40~14:50 为业务意义上的重仓核心窗 ----------
# 单位：自当日 00:00 起的分钟数（北京时间）。该半开区间内 _job_p3_tick_clock_aligned 直接 return，不创建线程。
P4_TAIL_PRIORITY_START_MIN = 14 * 60 + 31  # 14:31（P4 从此时开始接管股票池扫描）
P4_TAIL_PRIORITY_END_MIN = 14 * 60 + 55  # 14:55（含末枪 P4）
# 文档锚点：14:40~14:50 三枪为「核心重仓」调度，仍统一使用下方 900s 等锁，不在代码里拆更短超时。
P4_CORE_TAIL_START_MIN = 14 * 60 + 40  # 14:40（仅用于日志/注释对齐产品话术）
# P4 每一枪均最多等锁 15 分钟：快照或长尾任务占锁时，尾盘线程仍能挤入执行，满足「必定执行」容错。
P4_TAIL_WAIT_LOCK_SEC = 900.0

# P3 / P4 轮询：按秒级节拍对齐；默认 150 秒（2分30秒）一拍。
P3_POLL_INTERVAL_SECONDS = 150
P4_POLL_INTERVAL_SECONDS = 300
_P3_LAST_DISPATCH_BUCKET = ""
_P4_DISPATCHED_MINUTE_KEYS: set[str] = set()
P4_TAIL_SHOT_MINUTES = (871, 876, 881, 886, 891)  # 14:31 / 14:36 / 14:41 / 14:46 / 14:51

# 主循环 sleep(1s)；累计满 N 次 tick 后轮询异步扫描队列（默认 10s）
_ASYNC_QUEUE_POLL_TICKS = 10

# 异步扫描队列（原独立 scheduler 进程）：本进程启动时独占 filelock，主循环按 _ASYNC_QUEUE_POLL_TICKS 轮询
_async_scan_queue_lock_ok = False


def _apply_daemon_resource_profile() -> None:
    """
    从 config.yaml server.* 读取资源档，减轻老 CPU / 小内存主机上盘中扫描与轮询压力。
    - daemon_profile: standard | low（low：P3 约 5 分钟一拍、异步队列约 15s 轮询；如无显式覆盖，P3 仍保持 5 分钟）
    - 可选覆盖：daemon_p3_poll_interval_minutes、daemon_async_queue_poll_seconds
    """
    global P3_POLL_INTERVAL_SECONDS, P4_POLL_INTERVAL_SECONDS, _ASYNC_QUEUE_POLL_TICKS
    std = {"p3_sec": 150, "p4_sec": 150, "async_sec": 10}
    low = {"p3_sec": 150, "p4_sec": 150, "async_sec": 15}
    prof = "standard"
    p3_override: Optional[int] = None
    p4_override: Optional[int] = None
    async_override: Optional[int] = None
    try:
        import yaml

        cfg_path = os.path.join(_PROJECT_ROOT, "config.yaml")
        if os.path.isfile(cfg_path):
            with open(cfg_path, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            if isinstance(cfg, dict):
                srv = cfg.get("server") or {}
                if isinstance(srv, dict):
                    raw = str(srv.get("daemon_profile") or srv.get("daemon_resource_profile") or "").strip().lower()
                    if raw:
                        prof = raw
                    if srv.get("daemon_p3_poll_interval_seconds") is not None:
                        p3_override = int(srv["daemon_p3_poll_interval_seconds"])
                    elif srv.get("daemon_p3_poll_interval_minutes") is not None:
                        p3_override = int(srv["daemon_p3_poll_interval_minutes"]) * 60
                    if srv.get("daemon_p4_poll_interval_seconds") is not None:
                        p4_override = int(srv["daemon_p4_poll_interval_seconds"])
                    elif srv.get("daemon_p4_poll_interval_minutes") is not None:
                        p4_override = int(srv["daemon_p4_poll_interval_minutes"]) * 60
                    if srv.get("daemon_async_queue_poll_seconds") is not None:
                        async_override = int(srv["daemon_async_queue_poll_seconds"])
    except Exception as e:
        logger.debug("读取 server.daemon_profile 失败，使用内置默认: %s", e)

    # 保险：避免任何分支下 p4_override 未绑定
    if "p4_override" not in locals():
        p4_override = None
    base = low if prof in ("low", "weak", "minimal", "e5", "light") else std
    p3 = p3_override if p3_override is not None else base["p3_sec"]
    p4 = p4_override if p4_override is not None else base["p4_sec"]
    asec = async_override if async_override is not None else base["async_sec"]
    try:
        p3 = max(60, min(600, int(p3)))
    except (TypeError, ValueError):
        p3 = std["p3_sec"]
    try:
        p4 = max(60, min(600, int(p4)))
    except (TypeError, ValueError):
        p4 = std["p4_sec"]
    try:
        asec = max(5, min(120, int(asec)))
    except (TypeError, ValueError):
        asec = std["async_sec"]
    P3_POLL_INTERVAL_SECONDS = p3
    P4_POLL_INTERVAL_SECONDS = 300
    _ASYNC_QUEUE_POLL_TICKS = max(1, asec)
    if p4_override is not None:
        logger.info("server.daemon_p4_poll_interval_* 已忽略：P4 固定使用尾盘五拍")
    logger.info(
        "守护资源档 profile=%s | P3 每 %ss 一拍 | P4 固定14:31/36/41/46/51 | 异步队列每 %ss 轮询",
        prof or "standard",
        P3_POLL_INTERVAL_SECONDS,
        _ASYNC_QUEUE_POLL_TICKS,
    )


# ---------- 晚间链状态落盘（可观测 + 重启后可读）----------
_PIPELINE_STATE_REL = os.path.join("data", "runtime", "state", "sniper_pipeline_state.json")
_DAEMON_PUBLIC_META_REL = os.path.join("data", "runtime", "state", "daemon_public_meta.json")


# ------------------- 【V26.88+ 9-26 复盘修复】第三方服务启动期健康检查 -------------------
# 目标：在 daemon 启动时主动校验 DeepSeek 模型名 + Tushare/HiThink 连接可用性，
# 防止"配置错误→服务静默失败"事故（如将 deepseek-flash 错填为 deepseek-v99、API Key 失效）。
# 同时检测 fact_financial_reports 数据新鲜度，超 30 自然日未更新则告警（AI-03）。

_DEEPSEEK_MODEL_WHITELIST = frozenset({
    "deepseek-flash",      # V4.1-Flash（非思考模式，默认推荐）
    "deepseek-v4-pro",     # V4-Pro 主力模型（思考模式可用）
    # [V26.88+10-9 按官方文档修正] 旧模型 deepseek-chat / deepseek-reasoner / deepseek-coder 已从白名单移除
})
_HEALTHCHECK_COOLDOWN_SEC = 6 * 3600  # 6 小时去重
_LAST_HEALTHCHECK_TS: float = 0.0


def _init_third_party_health_checks() -> None:
    """
    【V26.88+ 9-26 复盘修复】第三方服务启动期健康检查。

    1. DeepSeek 配置校验：模型名必须在白名单内，API Key 必填且非空；
       异常时调用企微告警，避免运行时静默 fallback。
    2. 财务表新鲜度：fact_financial_reports 最近 ann_date 距今超过 30 个自然日 → 告警（AI-03）。
    3. HiThink 健康：若 hithink.enabled=true，发送一次轻量请求确认 API Key 有效。
    """
    try:
        _init_deepseek_healthcheck()
        _init_financial_freshness_check()
        _init_hithink_healthcheck()
    except Exception as e:
        logger.debug("[healthcheck] 启动期健康检查异常（不影响启动）: %s", e)


def _init_deepseek_healthcheck() -> None:
    """检查 DeepSeek 模型名白名单 + API Key 非空。"""
    try:
        from core.config_manager import get_deepseek_analysis_config
        cfg = get_deepseek_analysis_config()
    except Exception as e:
        logger.debug("[healthcheck] DeepSeek 配置读取失败: %s", e)
        return

    if not cfg.get("enabled"):
        return

    api_key = str(cfg.get("api_key") or "").strip()
    if not api_key:
        msg = "DeepSeek enabled=True 但 api_key 为空；推送前会兜底文案。请补全。"
        logger.warning("⚠️ %s", msg)
        _notify_deepseek_healthcheck("config_invalid", msg)
        return

    model = str(cfg.get("model") or "deepseek-flash").strip()
    if model not in _DEEPSEEK_MODEL_WHITELIST:
        msg = (
            f"DeepSeek model={model} 不在白名单 {sorted(_DEEPSEEK_MODEL_WHITELIST)}；"
            f"可能不存在。系统将自动 fallback 到 deepseek-chat，但建议修正配置。"
        )
        logger.warning("⚠️ %s", msg)
        _notify_deepseek_healthcheck("model_not_whitelisted", msg)

    # [V26.88+10-9 优化] fallback_model 也必须白名单校验, 防止"主模型合法 + fallback
    # 拼写错误"导致降级路径静默失败。原逻辑只查 model, 漏掉 fallback, 历史已踩过坑。
    if cfg.get("fallback_enabled"):
        fb_model = str(cfg.get("fallback_model") or "").strip()
        if fb_model and fb_model not in _DEEPSEEK_MODEL_WHITELIST:
            msg = (
                f"DeepSeek fallback_model={fb_model} 不在白名单 {sorted(_DEEPSEEK_MODEL_WHITELIST)}；"
                f"主模型失败时降级将不可用，请修正配置或关闭 fallback_enabled。"
            )
            logger.warning("⚠️ %s", msg)
            _notify_deepseek_healthcheck("fallback_model_not_whitelisted", msg)


def _init_financial_freshness_check() -> None:
    """检查 fact_financial_reports 新鲜度，缺失或陈旧时告警。"""
    try:
        from data.db_core import get_read_conn_singleton, table_exists
        con = get_read_conn_singleton(max_wait_sec=10.0)
        if con is None or not table_exists("fact_financial_reports"):
            logger.info("fact_financial_reports 表不存在，跳过新鲜度检查")
            return
        row = con.execute("SELECT MAX(ann_date) FROM fact_financial_reports").fetchone()
        latest = (row[0] if row else None) or ""
        if not latest:
            logger.info("fact_financial_reports 表为空，跳过新鲜度检查")
            return
        try:
            latest_dt = datetime.strptime(str(latest), "%Y%m%d").date()
            today_dt = _now_bj().date()
            age_days = (today_dt - latest_dt).days
            if age_days > 30:
                msg = (
                    f"财报同步滞后 {age_days} 天（最新 ann_date={latest}）。"
                    "AI 分析时若用到财报会拿到过期数据，可能误判财务健康度。"
                    "建议人工触发财报增量同步。"
                )
                logger.warning("⚠️ %s", msg)
                _notify_deepseek_healthcheck("financial_stale", msg)
            else:
                logger.info("✅ fact_financial_reports 新鲜度 OK（最新 %s, 距今 %d 天）", latest, age_days)
        except Exception as e:
            logger.debug("[healthcheck] 财报新鲜度解析失败: %s", e)
    except Exception as e:
        logger.debug("[healthcheck] 财报新鲜度检查异常: %s", e)


def _init_hithink_healthcheck() -> None:
    """HiThink 健康检查（仅 enabled 时）。失败写日志 + 告警，不阻塞启动。"""
    try:
        from data.hithink_client import HithinkClient
        client = HithinkClient()
        if not client.is_enabled:
            logger.info("HiThink 未启用，跳过健康检查")
            return
        if hasattr(client, "health_check"):
            ok = bool(client.health_check())
            if not ok:
                _notify_deepseek_healthcheck(
                    "hithink_unhealthy",
                    "HiThink health_check 返回 False，可能 API Key 过期或网络异常",
                )
        else:
            logger.debug("HithinkClient 无 health_check 方法，跳过主动健康检查")
    except Exception as e:
        logger.debug("[healthcheck] HiThink 健康检查异常: %s", e)


def _notify_deepseek_healthcheck(category: str, msg: str) -> None:
    """通过 daemon 告警通道推送第三方健康问题。6 小时去重。"""
    global _LAST_HEALTHCHECK_TS
    try:
        now_ts = time.time()
        if now_ts - float(_LAST_HEALTHCHECK_TS or 0.0) < _HEALTHCHECK_COOLDOWN_SEC:
            return
        _LAST_HEALTHCHECK_TS = now_ts
        _notify_daemon_alert(
            title=f"⚠️ 启动期健康检查 ({category})",
            detail=msg,
            category="data_sync",
            dedup_key=f"daemon_healthcheck_{category}_{_now_bj().strftime('%Y%m%d%H')}",
        )
    except Exception as e:
        logger.debug("[healthcheck] 告警推送失败: %s", e)


def _write_daemon_public_meta() -> None:
    """
    启动时落盘简明运维说明（晚间链时间表、关键路径），避免在网页堆长文。
    见 data/runtime/state/daemon_public_meta.json
    """
    try:
        from core.runtime_data_paths import ensure_runtime_data_layout

        ensure_runtime_data_layout()
        p = os.path.join(_PROJECT_ROOT, _DAEMON_PUBLIC_META_REL.replace("/", os.sep))
        payload = {
            "timezone": "Asia/Shanghai",
            "evening_chain": "19:45 incremental | 19:55 P1+push | 20:05 P5 | 20:30 compact",
            "morning": "08:50 clear push cache + catchup; 09:18 merged wechat brief (preflight); 09:26 P2; 09:35 P5 validation snapshot",
            "files": {
                "pipeline_state": "data/runtime/state/sniper_pipeline_state.json",
                "daemon_log": "data/runtime/sniper.log",
                "daemon_log_note": "RotatingFileHandler 50MB x10 UTF-8; see core/log_config.py",
            },
            "note": "Scheduled jobs run only in this process; Streamlit does not run the scheduler.",
            "started_at_bj": _now_bj().isoformat(),
        }
        os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        logger.info("运维说明已写入 %s", _DAEMON_PUBLIC_META_REL)
    except Exception as e:
        logger.debug("daemon_public_meta 写入失败: %s", e)


def _pipeline_state_path() -> str:
    from core.runtime_data_paths import ensure_runtime_data_layout

    ensure_runtime_data_layout()
    return os.path.join(_PROJECT_ROOT, _PIPELINE_STATE_REL.replace("/", os.sep))


def _read_pipeline_state() -> Dict[str, Any]:
    p = _pipeline_state_path()
    if not os.path.isfile(p):
        logger.debug("读取 pipeline 状态：文件不存在 | path=%s", p)
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            o = json.load(f)
        if isinstance(o, dict):
            return o
        logger.warning("读取 pipeline 状态：内容不是 dict，已忽略 | path=%s type=%s", p, type(o).__name__)
        return {}
    except json.JSONDecodeError as e:
        logger.warning("读取 pipeline 状态：JSON 解析失败 | path=%s err=%s", p, e)
        return {}
    except Exception as e:
        logger.warning("读取 pipeline 状态失败 | path=%s err=%s", p, e)
        return {}


def _read_today_data_quality(day: str) -> Dict[str, Any]:
    """读当日三态数据质量（ok / degraded / failed）。

    读不到时返回 status="unknown" —— 调用方必须把 unknown 视为「不可标 ok」。
    若在读取失败时默认按正常处理，监控本身一坏，整个防线就静默变成「一切正常」，
    这恰好是 2026-11-13 那次静默降级的翻版。
    """
    try:
        from data.fetcher._health_record import read_day_quality

        q = read_day_quality(day) or {}
    except Exception as e:
        logger.debug("读取数据质量失败（按 unknown 处理）: %s", e)
        return {"status": "unknown", "reasons": [f"读取失败: {str(e)[:200]}"]}
    if not str(q.get("status") or "").strip():
        return {"status": "unknown", "reasons": ["当日无数据质量记录"]}
    return q


def _quality_reason_text(q: Dict[str, Any]) -> str:
    return "；".join(q.get("reasons") or [])[:500] or "（无明细）"


def _mark_pool_quality(pool: str, day: str, selected_count: int = None) -> str:
    """按当日数据质量落盘状态位，返回最终判定（ok/degraded/failed/unknown）。

    设计取舍（为什么降级时**仍然**让 P5 跑完）：
      降级不等于不可用——缺涨停池时其余十一组信号依然有效，直接停产等于把
      「部分失效」放大成「完全失效」。所以这里只做两件事：
        1. 不写 last_<pool>_ok_bj_date（别再让残缺结果冒充正常）；
        2. 写独立的 degraded 状态位 + 原因，供体检/UI/告警读取。
      幂等性由调用方的「ok 或 degraded 都算今日已完成」保证，不会反复重跑。
    """
    q = _read_today_data_quality(day)
    status = str(q.get("status") or "unknown")
    ok_key = f"last_{pool}_ok_bj_date"
    deg_key = f"last_{pool}_degraded_bj_date"

    patch: Dict[str, Any] = {f"last_{pool}_data_quality": status}
    if selected_count is not None:
        patch[f"last_{pool}_selected_count"] = int(selected_count)

    if status == "ok":
        patch[ok_key] = day
        patch[f"last_{pool}_degraded_reasons"] = ""
    else:
        patch[deg_key] = day
        patch[f"last_{pool}_degraded_reasons"] = _quality_reason_text(q)

    if not _write_pipeline_state_patch(**patch):
        logger.warning("写 pipeline 状态失败 | pool=%s status=%s", pool, status)
        _notify_daemon_alert(
            "晚间链：状态位写入失败",
            f"日期 {day}：{pool} 已完成（数据质量={status}），但 pipeline_state 写盘失败，下一轮可能重复执行。",
            category="data_sync",
            dedup_key=f"daemon_pipeline_state_write_fail_{pool}_{day}",
        )

    if status != "ok":
        logger.warning(
            "数据质量不足，%s 不标记为 ok | status=%s | %s", pool, status, _quality_reason_text(q)
        )
        _notify_daemon_alert(
            f"数据降级：{pool} 在数据不全的情况下完成",
            (
                f"日期 {day}（北京时间）：{pool} 已执行完毕，但当日在取数阶段就被判定为 "
                f"**{status}**，结果仅供参考，不要当作完整信号使用。\n\n"
                f"**原因**：{_quality_reason_text(q)}\n\n"
                f"> 该结果未写入 {ok_key}，不会被当作正常完成品复用。"
            ),
            category="data_quality",
            dedup_key=f"daemon_{pool}_degraded_{day}",
        )
    return status


def _write_pipeline_state_patch(**kwargs: Any) -> bool:
    """合并写入 last_sync_ok_bj_date / last_p1_ok_bj_date 等，成功返回 True。"""
    p = _pipeline_state_path()
    try:
        cur = _read_pipeline_state()
        cur.update({k: v for k, v in kwargs.items() if v is not None})
        cur["updated_at"] = _now_bj().isoformat()
        dir_name = os.path.dirname(p) or "."
        os.makedirs(dir_name, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(prefix="pipeline_state_", suffix=".json", dir=dir_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(cur, f, ensure_ascii=False, indent=0)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, p)
            return True
        finally:
            try:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
            except Exception:
                pass
    except Exception as e:
        logger.warning("写入 pipeline 状态失败: %s", e)
        return False


# ---------- 交易日历：内存 TTL + 本地 JSON 雪崩兜底 ----------
_CAL_TTL_SEC = 120.0
_CAL_API_MAX_RETRIES = 3  # 【V26.88】日历 API 失败重试次数
_cal_mem: Dict[str, Any] = {
    "date": "",
    "is_open": False,
    "mono": 0.0,
    "source": "",
}


def _now_bj() -> datetime:
    return datetime.now(BJ_TZ)


def _trade_cal_cache_path() -> str:
    from core.runtime_data_paths import STATE_DIR, ensure_runtime_data_layout

    ensure_runtime_data_layout()
    return os.path.join(STATE_DIR, "sniper_trade_cal_cache.json")


def _load_cal_file_for_today(today_yyyymmdd: str) -> Optional[bool]:
    p = _trade_cal_cache_path()
    if not os.path.isfile(p):
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            return None
        if str(raw.get("date") or "") != today_yyyymmdd:
            return None
        return bool(raw.get("is_open"))
    except Exception as e:
        logger.debug("读取交易日历本地缓存失败: %s", e)
        return None


def _save_cal_file(today_yyyymmdd: str, is_open: bool) -> None:
    try:
        p = _trade_cal_cache_path()
        os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
        payload = {
            "date": today_yyyymmdd,
            "is_open": is_open,
            "updated_at": _now_bj().isoformat(),
        }
        with open(p, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=0)
    except Exception as e:
        logger.debug("写入交易日历本地缓存失败: %s", e)


class CalendarCache:
    """
    交易日历两层缓存（内存 + 磁盘）。
    get() 先查内存（按 date 键），命中且 TTL 内则返回；
    未命中时 source="miss"，调用方从 API/文件查询后调用 store() + persist() 写回两层缓存。
    """

    _cal_mem: dict = {}  # {"date": str, "is_open": bool, "mono": float, "source": str}

    @staticmethod
    def get(today: str, now_m: float) -> "CalendarCache":
        mem = CalendarCache._cal_mem
        if mem.get("date") == today and (now_m - float(mem.get("mono") or 0.0)) < _CAL_TTL_SEC:
            return CalendarCache(
                date=today,
                is_open=bool(mem.get("is_open")),
                source=str(mem.get("source") or "mem"),
                mono=float(mem.get("mono") or 0.0),
            )
        return CalendarCache(date=today, is_open=False, source="miss", mono=now_m)

    def __init__(self, date: str, is_open: bool, source: str, mono: float) -> None:
        self.date = date
        self.is_open = is_open
        self.source = source
        self.mono = mono

    def store(self) -> None:
        with _CAL_MEM_LOCK:
            CalendarCache._cal_mem = {
                "date": self.date,
                "is_open": self.is_open,
                "mono": self.mono,
                "source": self.source,
            }

    def persist(self, today: str) -> None:
        if self.source == "api":
            _save_cal_file(today, self.is_open)


# _daemon_is_trading_day_safe 用了 DataFetchCriticalError（外层 except 子句可见性要求）；
# 把它从 is_today_trading_day_sse 函数体内上移到模块级，避免 NameError。
from data.fetcher import DataFetchCriticalError  # noqa: E402

# === 注册向后兼容 shim：把 data.fetcher 包对象同时放到 sys.modules['data.data_fetcher']
# 旧业务代码 `from data.data_fetcher import xxx` 仍能工作。
import data.fetcher  # noqa: E402,F401  # 注册 sys.modules['data.data_fetcher'] = data.fetcher


def is_today_trading_day_sse() -> bool:
    today = _now_bj().strftime("%Y%m%d")
    now_m = time.monotonic()

    cache = CalendarCache.get(today, now_m)
    if cache.source != "miss":
        return cache.is_open

    # 【P1-6 2026-11-14】本地交易日历兜底（dim_trade_calendar，离线单行查询）：
    # 断网/代理死亡时不再「按非交易日跳过」整天（历史上发生过 34 次），
    # 也不依赖在线 trade_cal 才能判断今天是交易日。
    try:
        from data.db_core._trade_dates import is_trade_date_local

        _local = is_trade_date_local(today)
        if _local is not None:
            result = CalendarCache(date=today, is_open=bool(_local), source="local", mono=now_m)
            result.store()
            logger.debug("交易日判断命中本地日历（dim_trade_calendar）: is_open=%s", _local)
            return bool(_local)
    except Exception as _e_local:
        logger.debug("本地交易日历查询失败（继续走在线链路）: %s", _e_local)

    for attempt in range(_CAL_API_MAX_RETRIES):
        try:
            import data.fetcher as data_fetcher  # noqa: F401  # V26.88 拆分后用新包；shim 已注册 sys.modules['data.data_fetcher']
            # DataFetchCriticalError 已在模块级 import；这里不要再 from-import（会让函数体内出现
            # 同名局部变量，导致 except 子句出现 UnboundLocalError）。

            if getattr(data_fetcher, "pro", None) is None:
                cached = _load_cal_file_for_today(today)
                if cached is not None:
                    result = CalendarCache(date=today, is_open=bool(cached), source="file", mono=now_m)
                    result.store()
                    logger.warning("Tushare 未初始化，已用缓存 is_open=%s", cached)
                    return bool(cached)
                if attempt < _CAL_API_MAX_RETRIES - 1:
                    logger.info("Tushare 未初始化且无缓存，%.0fs 后重试 (%s/%s)", 3.0, attempt + 2, _CAL_API_MAX_RETRIES)
                    time.sleep(3.0)
                    continue
                data_fetcher.raise_data_fetch_critical(
                    f"is_today_trading_day_sse：pro 未初始化且无当日本地缓存，{_CAL_API_MAX_RETRIES} 次重试后放弃"
                )

            cal = data_fetcher.retry_api(data_fetcher.pro.trade_cal)(
                exchange="SSE", is_open="1", start_date=today, end_date=today,
            )
            is_open = cal is not None and not cal.empty
            result = CalendarCache(date=today, is_open=is_open, source="api", mono=now_m)
            result.store()
            result.persist(today)
            return is_open

        except DataFetchCriticalError:
            raise
        except Exception as e:
            logger.warning("trade_cal 查询异常(attempt %s/%s)，%.0fs 后重试: %s", attempt + 1, _CAL_API_MAX_RETRIES, 3.0, e)
            cached = _load_cal_file_for_today(today)
            if cached is not None:
                result = CalendarCache(date=today, is_open=bool(cached), source="file", mono=now_m)
                result.store()
                logger.warning("日历 API 不可用，已用缓存 is_open=%s", cached)
                return bool(cached)
            if attempt < _CAL_API_MAX_RETRIES - 1:
                time.sleep(3.0)
            else:
                result = CalendarCache(date=today, is_open=False, source="fail", mono=now_m)
                result.store()
                logger.warning(
                    "日历 API %s 次重试后全部失败且无当日本地缓存 → 按交易日处理（避免守护静默假死）",
                    _CAL_API_MAX_RETRIES,
                )
                return True  # 【V26.88 安全兜底】保守按交易日，任务仍执行

    return True  # 保底


def _daemon_is_trading_day_safe() -> bool:
    """
    交易日 True / 非交易日 False。
    DataFetchCriticalError（企微已告警）时记 ERROR 并返回 False，避免守护线程因日历熔断而整体退出。
    【V26.88+ 9-26 复盘修复】同时把熔断事件主动推企微，避免守护线程把异常"内部消化"导致无告警。
    【V26.88+10-2 F2-10 双保险】再叠一层 NameError 兜底，防止 namespace 包内 from data import data_fetcher 等历史 import
    失败导致 DataFetchCriticalError 名字未绑定而触发 UnboundLocalError。
    """
    try:
        return is_today_trading_day_sse()
    except DataFetchCriticalError as e:
        _notify_data_fetch_critical_once("trade_cal_check", e)
        logger.error("🚨 交易日历不可用（行情底座熔断），本轮按非交易日跳过: %s", e)
        return False
    except NameError as _e_ne:
        # 【F2-10 双保险】DataFetchCriticalError 等符号在 namespace 包下偶尔会丢失。保底返回 False 跳过本轮。
        logger.error("🚨 交易日历判断 NameError（符号可能未绑定），本轮按非交易日跳过: %s", _e_ne)
        return False


def _barrier_trading_day_or_skip(task_name: str) -> bool:
    if not _daemon_is_trading_day_safe():
        logger.info("非交易日，跳过: %s", task_name)
        return False
    return True


# ==================== 模拟盘（Paper Trading）挂钩 ====================
# 【设计约束】
#  · 模拟盘是旁路能力：任何异常都不得影响 P3/P4 扫描与企微推送主链路。
#  · 买入在扫描出正式行之后立即挂载（不依赖企微开关、不受 Top3 限制）。
#  · 卖出由独立「持仓监控任务」按 monitor_interval_sec 一拍（P4 只扫五次，无法连续盯盘）。
#  · 全部逻辑收敛在 core/paper_trading/daemon_hooks.py，本文件只保留薄调用点。


def _paper_on_signals_safe(pool_key: str, rows: Any) -> None:
    """把扫描正式行交给模拟盘引擎；异常全部吞掉，仅记日志。"""
    try:
        from core.paper_trading.daemon_hooks import paper_on_signals_safe

        paper_on_signals_safe(pool_key, rows)
    except Exception as e:
        logger.warning("[模拟盘][%s] 挂钩异常（已吞）: %s", pool_key, e)


def _job_paper_positions_tick() -> None:
    """模拟盘持仓监控一帧（-3% 止损 / +8% 止盈 / 当日跌3% / 到期，均 100% 清仓）。

    【P0 修复 2026-11-13】本函数只保留兼容入口：真正的监控跑在独立线程
    `_paper_monitor_loop`（由 `_start_paper_monitor_thread` 启动）。
    原先它挂在 schedule.every(1).seconds 上，与 P3 扫描 / DeepSeek 调用共享
    同一个主循环线程 —— 盘中实测出现过 125.5 秒的 LOOP_GAP，意味着
    **止损在这 2 分钟里根本没被检查**，于是 -3% 的仓位实际以 -3.9% 成交。
    """
    if _maintenance_mode_skip("模拟盘持仓监控"):
        return
    if not _barrier_trading_day_or_skip("模拟盘持仓监控"):
        return
    try:
        from core.paper_trading.daemon_hooks import paper_tick_job

        paper_tick_job()
    except Exception as e:
        logger.warning("[模拟盘] 持仓监控异常（已吞）: %s", e)


# ==================== 模拟盘持仓监控 · 独立线程（P0-4）====================
_PAPER_MONITOR_THREAD: Optional[threading.Thread] = None
_PAPER_MONITOR_STOP = threading.Event()
_PAPER_WATCHDOG_THREAD: Optional[threading.Thread] = None
_PAPER_MONITOR_STATS: Dict[str, Any] = {
    "ticks": 0,
    "sold": 0,
    "errors": 0,
    "skipped_trading_day": 0,
    "skipped_maintenance": 0,
    "last_tick_bj": "",
    "last_sold_bj": "",
    "max_interval_sec": 0.0,
}


def _maintenance_mode_on_quiet() -> bool:
    """维护模式判定（静默版）：供高频轮询使用，命中时不写 WARNING 日志。"""
    try:
        from core.master_control import is_maintenance_mode_enabled

        return bool(is_maintenance_mode_enabled())
    except Exception:  # noqa: BLE001
        return False


def _paper_monitor_interval() -> int:
    """监控间隔（秒）。配置非法/异常时回落 10s，且强制夹在 [3, 60]。"""
    try:
        from core.paper_trading.config import load_config

        raw = int(load_config().monitor_interval_sec or 10)
    except Exception as e:  # noqa: BLE001
        logger.debug("[模拟盘] 读取 monitor_interval_sec 失败，回落 10s: %s", e)
        raw = 10
    return max(3, min(60, raw))


def _paper_monitor_loop() -> None:
    """独立线程主体：每 interval 秒检查一次持仓阈值，不受主循环阻塞影响。

    设计约束（与 daemon 主链路的关系）：
      · **只读判定 + 写库走 store.write_transaction 的 _write_lock**，
        与主线程的写操作串行，不会制造 DuckDB 双写；
      · 不抢 _SCAN_BUSY（否则又会变成「抢不到就跳过」，与本次修复目标相反）；
      · 任何异常都吞掉并计数，绝不让本线程死掉；
      · 维护模式 / 非交易日直接跳过（不写库）。
    """
    interval = _paper_monitor_interval()
    logger.info(
        "[模拟盘] 持仓监控线程已启动 | interval=%ss | 独立于主循环（出场不再被扫描阻塞）",
        interval,
    )
    last_mono = time.monotonic()
    while not _PAPER_MONITOR_STOP.is_set():
        try:
            now_m = time.monotonic()
            gap = now_m - last_mono
            last_mono = now_m
            if gap > float(_PAPER_MONITOR_STATS.get("max_interval_sec") or 0.0):
                _PAPER_MONITOR_STATS["max_interval_sec"] = round(gap, 1)

            # 维护模式 / 非交易日：跳过（不写库、不告警刷屏）
            try:
                if _maintenance_mode_on_quiet():
                    _PAPER_MONITOR_STATS["skipped_maintenance"] += 1
                elif not _daemon_is_trading_day_safe():
                    _PAPER_MONITOR_STATS["skipped_trading_day"] += 1
                else:
                    from core.paper_trading.daemon_hooks import paper_tick_job

                    out = paper_tick_job()
                    _PAPER_MONITOR_STATS["ticks"] += 1
                    _PAPER_MONITOR_STATS["last_tick_bj"] = _now_bj().strftime("%H:%M:%S")
                    sold = int((out or {}).get("sold") or 0) if isinstance(out, dict) else 0
                    if sold:
                        _PAPER_MONITOR_STATS["sold"] += sold
                        _PAPER_MONITOR_STATS["last_sold_bj"] = _now_bj().strftime("%H:%M:%S")
                # 间隔可能被运行时改配置 → 每轮重读一次（成本可忽略）
                interval = _paper_monitor_interval()
            except Exception as e:  # noqa: BLE001
                _PAPER_MONITOR_STATS["errors"] += 1
                logger.warning("[模拟盘] 监控 tick 异常（已吞，线程继续）: %s", e)
                if _PAPER_MONITOR_STATS["errors"] % 50 == 1:
                    _notify_daemon_alert(
                        "【模拟盘】持仓监控连续异常",
                        f"时间：{_now_bj().strftime('%Y-%m-%d %H:%M:%S')}\n"
                        f"累计异常：{_PAPER_MONITOR_STATS['errors']} 次\n异常：{str(e)[:400]}",
                        category="paper_trading",
                        dedup_key="paper_monitor_errors",
                    )
        except BaseException as e:  # noqa: BLE001  线程绝不允许退出
            _PAPER_MONITOR_STATS["errors"] += 1
            logger.warning("[模拟盘] 监控循环外层异常（已吞）: %s", e)

        # 用 Event.wait 代替 sleep：进程退出时能被立刻唤醒，收尾更快
        _PAPER_MONITOR_STOP.wait(timeout=interval)


def _start_paper_monitor_thread() -> bool:
    """启动（幂等）模拟盘持仓监控线程。返回是否新启动。"""
    global _PAPER_MONITOR_THREAD
    try:
        from core.paper_trading.config import is_enabled

        if not is_enabled():
            logger.info("[模拟盘] 未启用（paper_trading.enabled=False），不启动监控线程")
            return False
    except Exception as e:  # noqa: BLE001
        logger.warning("[模拟盘] 启用状态检测失败，按未启用处理: %s", e)
        return False
    if _PAPER_MONITOR_THREAD is not None and _PAPER_MONITOR_THREAD.is_alive():
        return False
    _PAPER_MONITOR_STOP.clear()
    t = threading.Thread(target=_paper_monitor_loop, name="paper-monitor", daemon=True)
    t.start()
    _PAPER_MONITOR_THREAD = t
    return True


def _paper_monitor_status() -> Dict[str, Any]:
    """供心跳/诊断展示的监控线程状态。"""
    alive = bool(_PAPER_MONITOR_THREAD is not None and _PAPER_MONITOR_THREAD.is_alive())
    return {
        "alive": alive,
        "interval_sec": _paper_monitor_interval(),
        **_PAPER_MONITOR_STATS,
    }


def _job_paper_monitor_watchdog() -> None:
    """08:41 看护：监控线程若因任何原因死掉（或被配置临时关闭后重开），当天自动拉起。

    与 08:40 的「模拟盘初始化」错峰一分钟：初始化要先建表/建账户，
    监控线程在表就位后启动才不会在首拍踩到「表不存在」。
    始终返回 None（视为当日完成），避免 catchup 窗口内每分钟重复派发。
    """
    try:
        if _start_paper_monitor_thread():
            logger.info("[模拟盘] 监控线程已（重新）拉起")
        else:
            logger.debug("[模拟盘] 监控线程无需拉起（未启用或已在运行）")
    except Exception as e:  # noqa: BLE001
        logger.warning("[模拟盘] 监控线程看护异常（已吞）: %s", e)


def _job_paper_daily_settle() -> None:
    """收盘结算：写当日净值行并推进实验进度。

    返回 False 只在真异常时，避免「结算失败被标记完成 → 当天净值缺失」。
    """
    if _maintenance_mode_skip("模拟盘收盘结算"):
        return
    if not _barrier_trading_day_or_skip("模拟盘收盘结算"):
        return
    try:
        from core.paper_trading.daemon_hooks import paper_settle_job

        paper_settle_job()
    except Exception as e:
        logger.warning("[模拟盘] 收盘结算异常（已吞）: %s", e)
        return False


def _job_paper_init() -> None:
    """08:40 建表 / 初始化模拟账户（保证当日首笔信号前表已就位）。"""
    if _maintenance_mode_skip("模拟盘初始化"):
        return
    try:
        from core.paper_trading.daemon_hooks import paper_init_job

        paper_init_job()
    except Exception as e:
        logger.warning("[模拟盘] 初始化异常（已吞）: %s", e)


# ---- 历史买入补推（一次性，幂等）----
# 背景：买入企微推送功能上线当天，若当日已有成交发生在上线之前，
# 这些笔不会自动推送。此任务在 daemon 启动后跑一次，把当天已成交的
# BUY 订单补推一条汇总；用状态文件记已补推日期，保证跨重启不重复补推。
_PAPER_BACKFILL_MARKER = "data/runtime/state/paper_buy_backfill.json"
_paper_backfill_done = False


def _job_paper_buy_backfill_once() -> None:
    """把当日已成交但未推送过的模拟盘买入补推一条企微汇总（幂等，一天一次）。"""
    global _paper_backfill_done
    if _paper_backfill_done:
        return
    try:
        from core.paper_trading.store import now_bj, today_yyyymmdd

        d = today_yyyymmdd()
        marker_path = os.path.join(os.getcwd(), _PAPER_BACKFILL_MARKER)
        try:
            if os.path.isfile(marker_path):
                with open(marker_path, "r", encoding="utf-8") as f:
                    if json.load(f).get("done_date") == d:
                        _paper_backfill_done = True
                        return
        except Exception as e:
            logger.debug("[模拟盘补推] 读取标记失败（继续）: %s", e)
        from core.paper_trading.engine import notify_historical_buys

        n = notify_historical_buys(d)
        _paper_backfill_done = True
        logger.info("[模拟盘补推] 当日买入补推 %d 笔", n)
        try:
            os.makedirs(os.path.dirname(marker_path), exist_ok=True)
            with open(marker_path, "w", encoding="utf-8") as f:
                json.dump(
                    {"done_date": d, "done_at": now_bj().strftime("%Y-%m-%d %H:%M:%S"), "count": n},
                    f,
                    ensure_ascii=False,
                )
        except Exception as e:
            logger.warning("[模拟盘补推] 写标记失败: %s", e)
    except Exception as e:
        logger.warning("[模拟盘补推] 异常（已吞）: %s", e)


# ---- 战法归因报告推送（启动后一次，幂等按日期）----
# 目的：1-2 个月的策略优选实验需要持续看到「哪个战法最赚」。
# 每天 15:05 收盘结算后会自动推一次；这里额外在 daemon 启动后推一次，
# 保证「重启 / 补跑」也能立刻看到当前归因结论。
_ATTRIBUTION_MARKER = "data/runtime/state/paper_attribution_push.json"
_paper_attribution_done = False


def _job_paper_attribution_push_once() -> None:
    """推送一次「按战法收益归因」报告（幂等，一天一次）。"""
    global _paper_attribution_done
    if _paper_attribution_done:
        return
    try:
        from core.paper_trading.store import now_bj, today_yyyymmdd

        d = today_yyyymmdd()
        marker_path = os.path.join(os.getcwd(), _ATTRIBUTION_MARKER)
        try:
            if os.path.isfile(marker_path):
                with open(marker_path, "r", encoding="utf-8") as f:
                    if json.load(f).get("done_date") == d:
                        _paper_attribution_done = True
                        return
        except Exception as e:
            logger.debug("[模拟盘归因] 读取标记失败（继续）: %s", e)
        from core.paper_trading.attribution import notify_attribution_report

        ok = notify_attribution_report()
        _paper_attribution_done = True
        logger.info("[模拟盘归因] 归因报告推送 %s", "成功" if ok else "跳过")
        try:
            os.makedirs(os.path.dirname(marker_path), exist_ok=True)
            with open(marker_path, "w", encoding="utf-8") as f:
                json.dump(
                    {"done_date": d, "done_at": now_bj().strftime("%Y-%m-%d %H:%M:%S"), "pushed": bool(ok)},
                    f,
                    ensure_ascii=False,
                )
        except Exception as e:
            logger.warning("[模拟盘归因] 写标记失败: %s", e)
    except Exception as e:
        logger.warning("[模拟盘归因] 异常（已吞）: %s", e)


def _count_limit_up_hits(base: Any) -> int:
    """【ST-P0-15】统计 base 中真正命中涨停池的条数。

    背景：P3/P4/P5 的「涨停池命中: %d/%d」曾把 len(base) 传两遍 → 永远 100%，
    即使涨停池整体失效（命中 0）也照常显示满分，监控告警完全看不见问题。
    这里直接读 item.rt 里的 _hithink_limit_up 标记来统计真实命中数。
    """
    if not isinstance(base, (list, tuple)):
        return 0
    hits = 0
    for _item in base:
        try:
            if isinstance(_item, dict):
                _rt = _item.get("rt") or {}
                if isinstance(_rt, dict) and _rt.get("_hithink_limit_up"):
                    hits += 1
        except Exception:
            continue
    return hits


def _resolve_regime_name() -> str:
    try:
        from core.regime_analyzer import get_market_regime

        raw = str((get_market_regime() or {}).get("primary", {}).get("status", "") or "")
        if "主升" in raw:
            return "主升浪"
        if "退潮" in raw or "防守" in raw:
            return "情绪退潮市"
        return "震荡市"
    except Exception as e:
        logger.debug("regime 回退震荡市: %s", e)
        return "震荡市"


# ---------- JSON 缓存读写 ----------


def _json_sanitize_scalar(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, float) and pd.isna(v):
        return None
    if isinstance(v, pd.Timestamp):
        return v.isoformat()
    if isinstance(v, (bytes, bytearray)):
        return bytes(v).decode("utf-8", errors="replace")
    try:
        json.dumps(v)
    except (TypeError, ValueError):
        return str(v)
    return v


def _json_safe_dict(d: Any) -> Dict[str, Any]:
    if not isinstance(d, dict):
        return {}
    out: Dict[str, Any] = {}
    for k, v in d.items():
        ks = str(k)
        try:
            if isinstance(v, dict):
                out[ks] = _json_safe_dict(v)
            elif isinstance(v, (list, tuple)):
                out[ks] = [
                    _json_safe_dict(x) if isinstance(x, dict) else _json_sanitize_scalar(x)
                    for x in v
                ]
            else:
                out[ks] = _json_sanitize_scalar(v)
        except Exception:
            out[ks] = str(v)
    return out


def _save_base_items_json(
    path: str,
    items: List[Dict[str, Any]],
    *,
    p1_envelope_source: Optional[str] = None,
) -> None:
    rows: List[Dict[str, Any]] = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        row: Dict[str, Any] = {
            "code": it.get("code"),
            "p1_score": float(it.get("p1_score", 0) or 0),
            "hist": _json_safe_dict(it.get("hist") or {}),
        }
        df = it.get("df")
        if isinstance(df, pd.DataFrame) and not df.empty:
            row["df_split"] = json.loads(df.to_json(orient="split", date_format="iso"))
        else:
            row["df_split"] = None
        rows.append(row)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    # 原子写：先写临时文件 + flush + fsync，再 os.replace。避免 daemon 在 json.dump 中途崩溃时
    # 留下半截 JSON 导致 _load_base_items_json 拒载、底仓消失。
    import tempfile
    fd, tmp_path = tempfile.mkstemp(prefix=".p1_cache_", suffix=".json.tmp", dir=os.path.dirname(path) or ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            if p1_envelope_source in ("UI_MANUAL", "DAEMON_AUTO"):
                # P1 底仓 JSON：顶层主权元数据，与 core.pool_manager.p1_cache_json_should_skip_daemon_overwrite 对齐
                _ts = _now_bj().isoformat()
                json.dump(
                    {
                        "_source": str(p1_envelope_source),
                        "_timestamp": _ts,
                        "items": rows,
                    },
                    f,
                    ensure_ascii=False,
                    indent=0,
                )
            else:
                json.dump(rows, f, ensure_ascii=False, indent=0)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
        except Exception:
            pass
        raise


def _load_base_items_json(path: str) -> List[Dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    # 兼容新版带主权封套 {"_source","_timestamp","items"} 与旧版顶层数组
    if isinstance(raw, dict) and isinstance(raw.get("items"), list):
        rows = raw["items"]
    elif isinstance(raw, list):
        rows = raw
    else:
        rows = []
    out: List[Dict[str, Any]] = []
    if not isinstance(rows, list):
        return out
    for row in rows:
        if not isinstance(row, dict):
            continue
        hist = row.get("hist") or {}
        if not isinstance(hist, dict):
            hist = {}
        sp = row.get("df_split")
        if sp:
            df = pd.read_json(io.StringIO(json.dumps(sp)), orient="split")
        else:
            df = pd.DataFrame()
        out.append(
            {
                "code": row.get("code"),
                "p1_score": float(row.get("p1_score", 0) or 0),
                "df": df,
                "hist": hist,
            }
        )
    return out


def load_base_items_latest() -> List[Dict[str, Any]]:
    """优先当日 p1_cache JSON；否则取 runtime 目录下最新一份（周末后周一早盘常见）。"""
    try:
        from core.runtime_data_paths import POOL_CACHE_DIR, ensure_runtime_data_layout
        import glob

        ensure_runtime_data_layout()
        today = _now_bj().strftime("%Y%m%d")
        from core.runtime_data_paths import path_p1_cache_json

        p_today = path_p1_cache_json(today)
        if os.path.isfile(p_today):
            try:
                items = _load_base_items_json(p_today)
                if items:
                    logger.info("底仓载入: 当日缓存 %s (%s只)", p_today, len(items))
                    return items
            except Exception as e:
                logger.warning("读取当日底仓 JSON 失败: %s", e)

        files = sorted(
            glob.glob(os.path.join(POOL_CACHE_DIR, "p1_cache_*.json")),
            reverse=True,
        )
        for fp in files:
            try:
                items = _load_base_items_json(fp)
                if items:
                    logger.info("底仓载入: 最近缓存 %s (%s只)", fp, len(items))
                    return items
            except Exception as e:
                logger.warning("读取底仓 JSON 失败 %s: %s", fp, e)
    except Exception as e:
        logger.exception("load_base_items_latest: %s", e)
    return []


def _p1_cache_path_today() -> str:
    from core.runtime_data_paths import path_p1_cache_json, ensure_runtime_data_layout

    ensure_runtime_data_layout()
    return path_p1_cache_json(_now_bj().strftime("%Y%m%d"))


def _notification_url_and_enabled() -> Tuple[bool, str]:
    try:
        from core.config_manager import get_notification_config

        cfg = get_notification_config()
        en = bool(cfg.get("enabled"))
        url = str(cfg.get("wechat_webhook_url") or "").strip()
        return en, url
    except Exception as e:
        logger.debug("get_notification_config: %s", e)
        return False, ""


def daily_morning_routine() -> None:
    """
    每日 08:50 由独立守护线程执行（见 _job_daily_morning_routine，不占用 schedule 主循环）：
    1) 调用 clear_wechat_push_cache_global()，丢弃昨日可能残留的防刷键，防止字典长尾；
    2) 企微文案已与 09:18「早盘合并简报」合并为一条发送，此处不再单独推送（避免同日两条早间消息）。
    """
    from core.notification_gateway import clear_wechat_push_cache_global

    clear_wechat_push_cache_global()
    logger.info("早安例行：企微防刷缓存已清空；交易日完整早间企微简报于 09:18 预检通过后合并发送")


def _probe_duckdb_for_open_heartbeat() -> Tuple[bool, str]:
    """
    为早盘报备探活 DuckDB：能执行 SELECT 1 即视为正常。
    异常时返回 (False, 简短原因)，不向外抛，避免干扰守护进程主循环。
    """
    try:
        from data.db_core import get_read_conn_singleton

        con = get_read_conn_singleton()
        con.execute("SELECT 1").fetchone()
        return True, "正常"
    except Exception as e:
        return False, f"异常：{str(e)[:120]}"


def _morning_briefing_readiness() -> Tuple[bool, str, bool, str, Dict[str, Any]]:
    """
    合并早报发送前自检：DuckDB、master_control、风控配置可读、当日 P1 底仓已就绪（pipeline 或当日 JSON）。
    返回 (ok, fail_reason, db_ok, db_msg, mc)；失败时 fail_reason 供日志与运维告警，mc 可能为 {}。
    """
    db_ok, db_msg = _probe_duckdb_for_open_heartbeat()
    if not db_ok:
        return False, f"DuckDB不可连接: {str(db_msg)[:220]}", db_ok, db_msg, {}
    try:
        from core.master_control import read_master_control

        mc = read_master_control()
        if not isinstance(mc, dict):
            mc = {}
    except Exception as e:
        return False, f"master_control不可读: {e}", db_ok, db_msg, {}
    try:
        from core.config_manager import get_ui_alert_only

        get_ui_alert_only(force_reload=True)
    except Exception as e:
        return False, f"config/ui_alert_only不可读: {e}", db_ok, db_msg, mc
    today = _now_bj().strftime("%Y%m%d")
    st = _read_pipeline_state()
    if str(st.get("last_p1_ok_bj_date", "") or "").strip() != today:
        if not os.path.isfile(_p1_cache_path_today()):
            return (
                False,
                "P1底仓未就绪：当日 last_p1_ok_bj_date 非今日且无 p1_cache 当日 JSON（请查 08:50 早盘补位或晚间 P1）",
                db_ok,
                db_msg,
                mc,
            )
    return True, "", db_ok, db_msg, mc


def daily_open_heartbeat_routine() -> None:
    """
    交易日早盘 **合并简报**（原 08:50 早安 + 原核心引擎就绪）企微一条 Markdown：
    - 预检通过后才发送（见 ``_morning_briefing_readiness``）；
    - 早安问候、侧栏企微推送/自动巡航开关展示；
    - 数据库连接探活；
    - 当前风控模式：``get_ui_alert_only()`` → UI 预警 / 硬否决；
    - 盘中巡航待命提示。

    与 09:18 定时任务及启动补发共用同一入口：由本函数内的进程级锁 + pipeline_state 去重保证同日只发送一次。
    全函数外层由调用方线程再包 try-except；本函数内部对企微发送亦单独兜底，网络超时不得顶翻进程。
    """
    if not _OPEN_HEARTBEAT_LOCK.acquire(blocking=False):
        logger.debug("早盘就绪报备：已有线程在执行，跳过并发重入")
        return
    try:
        if not _daemon_is_trading_day_safe():
            logger.info("早盘就绪报备：非交易日，跳过")
            return

        today = _now_bj().strftime("%Y%m%d")
        st = _read_pipeline_state()
        if str(st.get("last_daemon_open_heartbeat_bj_date", "") or "").strip() == today:
            logger.debug("早盘就绪报备：本日已发送过，跳过")
            return

        en, url = _notification_url_and_enabled()
        if not en or not url:
            logger.info("早盘就绪报备：notification 未启用或未配置 webhook，跳过")
            return

        try:
            from core.master_control import is_wechat_push_master_enabled

            if not is_wechat_push_master_enabled():
                logger.info("早盘就绪报备：总控「企微推送」关闭，跳过")
                return
        except Exception as e:
            logger.debug("早盘就绪报备：读取 master_control 失败，继续尝试: %s", e)

        ok_r, why_r, db_ok, db_msg, mc = _morning_briefing_readiness()
        if not ok_r:
            logger.warning("早盘合并简报：预检未通过，跳过企微 | %s", why_r)
            _notify_daemon_alert(
                title="早盘合并简报已跳过（预检未过）",
                detail=f"{why_r}\n日期：{today}",
                category="daemon",
                dedup_key=f"daemon_morning_brief_preflight_skip_{today}",
            )
            return

        wechat_label = "开启" if mc.get("wechat_push_enabled") else "关闭"
        cruise_label = "开启" if mc.get("daemon_auto_cruise_enabled") else "关闭"

        try:
            from core.config_manager import get_ui_alert_only

            ui_alert_only = bool(get_ui_alert_only(force_reload=True))
            if ui_alert_only:
                risk_line = "纯界面预警：红线仅标签提示，不拦截出票"
            else:
                risk_line = "硬否决：触碰风控红线即拦截出票"
        except Exception as e:
            logger.debug("早盘就绪报备：读取 ui_alert_only 失败: %s", e)
            risk_line = "读取失败（已兜底）"

        ts = _now_bj().strftime("%Y-%m-%d %H:%M:%S")
        ts_short = _now_bj().strftime("%H:%M:%S")
        db_line = "正常" if db_ok else html.escape(str(db_msg), quote=False)
        risk_safe = html.escape(str(risk_line), quote=False)
        lines = [
            f"🟢 **【小杰AI选股系统 Pro V26.88】早安 · 交易日简报** <font color=\"comment\">[{ts_short}]</font>",
            "",
            "今日为交易日，各项数据已就绪。",
            "",
            f"- 🎛️ 企微推送：{html.escape(str(wechat_label), quote=False)}",
            f"- 🤖 自动巡航：{html.escape(str(cruise_label), quote=False)}",
            "",
            "---",
            "",
            "✅ **小杰AI选股系统 Pro V26.88 核心引擎已就绪**",
            "",
            f"- 数据库连接：{db_line}",
            f"- 当前风控模式：{risk_safe}",
            "- 守护进程已进入盘中巡航待命",
            "",
            f'<font color="comment">时间：{ts}</font>',
        ]
        body = "\n".join(lines)
        try:
            from core.notification_gateway import get_wechat_gateway

            gw = get_wechat_gateway(url)
            # 【V29 上线修复 2026-10-15】push_markdown_async 的签名是 (content: str) -> None，
            # 原代码多传了 dedup_key= 并把 None 当布尔判断，会在每天 08:50 早盘简报时
            # 必然抛 TypeError: push_markdown_async() got an unexpected keyword argument 'dedup_key'，
            # 导致早盘简报 100% 静默失败（异常被外层 try 吞掉）。
            # 根因：WechatNotificationGateway 在 V26.88 拆分后已改签名，但调用点没同步。
            # 修复：按现有签名调用；同日去重仍由本函数开头
            # pipeline_state['last_daemon_open_heartbeat_bj_date'] 与 _OPEN_HEARTBEAT_LOCK 保证，
            # 不依赖网关侧的 dedup_key（网关只支持 (content,) 单参）。
            gw.push_markdown_async(body)
        except Exception as e:
            logger.warning("早盘就绪报备：企微投递失败（已吞，不中断守护进程）: %s", e, exc_info=True)
            return

        _write_pipeline_state_patch(
            last_daemon_open_heartbeat_bj_date=today,
            last_daemon_open_heartbeat_at=_now_bj().isoformat(),
        )
        logger.info("早盘合并简报：已提交企微异步发送 | date=%s", today)
    except Exception as e:
        logger.exception("早盘就绪报备：未预期异常（已吞，不中断守护进程）: %s", e)
    finally:
        _OPEN_HEARTBEAT_LOCK.release()


def _maybe_open_heartbeat_on_startup() -> None:
    """
    09:18 启动补发入口：
    - 仅在交易日且北京时间落在 09:00~09:18（含）时启用；
    - 作用只是补发早盘合并简报，不改变 08:50 早盘编排链；
    - 最终仍统一落到 ``daily_open_heartbeat_routine()``，由其内部锁 + pipeline_state 去重。
    """
    try:
        now_bj = _now_bj()
        curr_min = now_bj.hour * 60 + now_bj.minute
        if curr_min < (9 * 60 + 0) or curr_min > (9 * 60 + 18):
            return
        if not _daemon_is_trading_day_safe():
            return

        def _runner() -> None:
            try:
                # 启动补发时，先尽量补齐 08:50 早盘补位链，避免 09:18 还处于 "--" 空状态。
                _morning_trading_catchup()
            except Exception as e:
                logger.exception("早盘补位(启动补发) 未捕获异常: %s", e)
            try:
                daily_open_heartbeat_routine()
            except Exception as e:
                logger.exception("早盘就绪报备(启动补发) 未捕获异常: %s", e)

        threading.Thread(target=_runner, name="daemon-open-hb-startup", daemon=True).start()
    except Exception as e:
        logger.exception("早盘就绪报备启动补发线程派生失败（已吞）: %s", e)


def _job_daily_open_heartbeat_routine() -> None:
    """09:18：调度入口只负责起线程；真正发送逻辑统一由 `daily_open_heartbeat_routine()` 执行。"""

    def _runner() -> None:
        try:
            daily_open_heartbeat_routine()
        except Exception as e:
            logger.exception("daily_open_heartbeat_routine 未捕获异常: %s", e)

    try:
        threading.Thread(target=_runner, name="daemon-open-hb-0918", daemon=True).start()
    except Exception as e:
        logger.exception("早盘就绪报备线程派生失败: %s", e)


def _push_top_scores(
    pool_key: str,
    rows: List[Dict[str, Any]],
    top_n: int,
    res: Optional[Dict[str, Any]] = None,
) -> None:
    en, url = _notification_url_and_enabled()
    if not en or not url:
        return
    obs_rows: List[Dict[str, Any]] = []
    if res is not None and isinstance(res, dict):
        obs = res.get("observation") or {}
        if isinstance(obs, dict):
            o = obs.get(pool_key)
            if isinstance(o, list):
                obs_rows = o
    if not rows and not obs_rows:
        return
    try:
        from core.notification_gateway import get_wechat_gateway

        gw = get_wechat_gateway(url)

        def _sc(r: Dict[str, Any]) -> float:
            try:
                return float(r.get("综合分", 0.0) or 0.0)
            except (TypeError, ValueError):
                return 0.0

        for row in sorted(rows, key=_sc, reverse=True)[: max(1, top_n)]:
            if isinstance(row, dict):
                gw.push_stock_if_allowed(pool_key, row)
        for row in sorted(obs_rows, key=_sc, reverse=True)[: max(1, top_n)]:
            if isinstance(row, dict):
                gw.push_stock_if_allowed(pool_key, row, pool_key_for_dedup=f"{pool_key}_obs")
    except Exception as e:
        logger.debug("企微推送失败(已吞): %s", e, exc_info=True)


def _scrub_base_items_inplace(items: List[Dict[str, Any]]) -> None:
    for it in items or []:
        if isinstance(it, dict):
            it.pop("df", None)
    try:
        items.clear()
    except Exception:
        pass


def _gc_after_scan(*, base: Optional[List[Dict[str, Any]]], res: Any) -> None:
    try:
        if isinstance(res, dict):
            res.clear()
    except Exception:
        pass
    if base is not None:
        _scrub_base_items_inplace(base)
    try:
        del res
    except Exception:
        pass
    try:
        del base
    except Exception:
        pass
    gc.collect()


def _bj_minutes_of_day() -> int:
    """北京时间当前时刻在当日 00:00 起的分钟数，用于时间窗比较。"""
    n = _now_bj()
    return int(n.hour) * 60 + int(n.minute)


def _in_p4_tail_priority_window() -> bool:
    """
    P4 优先走廊：14:31–14:55（含边界）。14:40–14:55 内 P4 五拍中的后四拍为「重仓核心」时段。
    此间 **严禁** 派发 P3（见 _job_p3_tick_clock_aligned）：不抢锁、不排队、不占主循环。
    """
    m = _bj_minutes_of_day()
    return P4_TAIL_PRIORITY_START_MIN <= m <= P4_TAIL_PRIORITY_END_MIN


def _run_scan_push_p2() -> None:
    """2 档竞价池：与 UI「竞价突袭」同源；守护进程固定在 09:26 触发，与 09:18 早盘合并简报错峰。"""
    if _maintenance_mode_skip("P2竞价"):
        return
    if _daemon_cruise_off_skip("P2竞价"):
        return
    if not _barrier_trading_day_or_skip("P2竞价"):
        return
    base = load_base_items_latest()
    if not base:
        logger.warning("P2：底仓为空，跳过扫描")
        return
    res: Any = None
    try:
        # V26.88+9-22 优化3：HiThink 大盘指数实时锚注入
        try:
            from core.strategies._hithink_market_context import hithink_index_snapshot as _get_ht_index
            _ht_ctx = _get_ht_index()
        except Exception:
            _ht_ctx = {}
        _ht_strength = float(_ht_ctx.get("market_strength", 1.0) or 1.0)
        _ht_label   = str(_ht_ctx.get("market_label", "正常") or "正常")
        _ht_intp    = str(_ht_ctx.get("best_interpretation", "") or "")
        logger.info("[P2] HiThink 大盘锚: strength=%.1f (%s) | %s", _ht_strength, _ht_label, _ht_intp)
        for _item in base:
            _rt = _item.setdefault("rt", {})
            _rt["_hithink_index_strength"] = _ht_strength
            _rt["_hithink_market_label"]   = _ht_label
            _rt["_hithink_market_intp"]    = _ht_intp

        # V26.88+9-22 优化4：一次性加载 HiThink 涨停池信号，注入每只股票的 rt["_pre_limit_up"]
        try:
            from core.p1_precompute import batch_precompute_limit_up_signals
            _lu_map = batch_precompute_limit_up_signals(base)
            _lu_hit = 0
            for _item in base:
                _code = str(_item.get("code", "") or "").strip()
                if _code and _code in _lu_map:
                    _rt2 = _item.setdefault("rt", {})
                    _rt2["_pre_limit_up"] = _lu_map[_code]
                    _lu_hit += 1
            logger.info("[P2] HiThink 涨停池注入: %d/%d 只命中", _lu_hit, len(base))
        except Exception as _e_lu:
            logger.debug("[P2] HiThink 涨停池加载失败（已降级忽略）: %s", _e_lu)

        from core.scan_engine import run_scan_engine
        regime = _resolve_regime_name()
        res = run_scan_engine(["p2"], base, regime=regime, progress_callback=None)
        rows = res.get("p2") or []
        # V29 可观测：把「P2 今日命中/观察数」写入心跳，回答「09:25 有几条命中」
        try:
            from data.daemon_observability import write_heartbeat

            write_heartbeat(
                {
                    "last_pool_result": {
                        "pool": "p2",
                        "regime": regime,
                        "hits": len(rows) if isinstance(rows, list) else 0,
                        "observed": len(
                            (res.get("observation") or {}).get("p2") or []
                        ),
                        "scanned": len(base or []),
                    }
                }
            )
        except Exception:
            pass
        _push_top_scores("p2", rows if isinstance(rows, list) else [], 3, res if isinstance(res, dict) else None)
    except Exception as e:
        logger.exception("P2 扫描异常: %s", e)
        _notify_daemon_alert(
            "P2 竞价扫描异常",
            str(e)[:900],
            category="scan_p2",
            dedup_key=f"daemon_p2_scan_{_now_bj().strftime('%Y%m%d')}",
        )
    finally:
        _gc_after_scan(base=base, res=res)


def _job_p2_tick() -> None:
    """09:26 竞价扫描：子线程内最多等锁 10 分钟（早盘链或快照占锁时可排队）。"""
    if not _daemon_is_trading_day_safe():
        logger.info("非交易日，跳过: P2竞价")
        return
    _spawn("P2", _run_scan_push_p2, wait_for_lock_sec=600.0)


def _in_p3_patrol_window() -> bool:
    """
    P3 允许跑扫描的盘中时段（与策略一致）：早盘 09:30–11:30、午盘 13:00–14:30。
    注意：14:30 之后即使调度器仍 tick，此处也会挡掉；与尾盘窗互斥策略叠加后，14:35 起仅 P4 活跃。
    """
    m = _bj_minutes_of_day()
    if 570 <= m <= 690:
        return True
    if 780 <= m <= 870:
        return True
    return False


def _run_scan_push_p3() -> None:
    if _maintenance_mode_skip("P3巡逻"):
        return
    if _daemon_cruise_off_skip("P3巡逻"):
        return
    if not _barrier_trading_day_or_skip("P3巡逻"):
        return
    if not _in_p3_patrol_window():
        return
    base = load_base_items_latest()
    if not base:
        logger.warning("P3：底仓为空，跳过扫描")
        return
    res: Any = None
    try:
        # V26.88+9-22 优化3+5：HiThink 大盘指数实时锚 + 涨停板实时上下文注入
        try:
            from core.strategies._hithink_market_context import hithink_index_snapshot as _get_ht_index
            _ht_ctx = _get_ht_index()
        except Exception:
            _ht_ctx = {}
        _ht_strength = float(_ht_ctx.get("market_strength", 1.0) or 1.0)
        _ht_label = str(_ht_ctx.get("market_label", "正常") or "正常")
        _ht_intp = str(_ht_ctx.get("best_interpretation", "") or "")

        # V26.88+9-22 优化5：HiThink 涨停/炸板池实时注入
        try:
            from core.strategies._hithink_limit_up_context import (
                load_hithink_limit_context as _get_ht_limit,
                inject_limit_context_to_base_items as _inject_limit,
            )
            _ht_limit = _get_ht_limit()
            _inject_limit(base, _ht_limit)
        except Exception as _e_lim:
            logger.debug("[P3] HiThink 涨停上下文加载失败（已降级）: %s", _e_lim)
            _ht_limit = {}

        for _item in base:
            _rt = _item.setdefault("rt", {})
            _rt["_hithink_index_strength"] = _ht_strength
            _rt["_hithink_market_label"] = _ht_label
            _rt["_hithink_market_intp"] = _ht_intp

        logger.info("[P3] HiThink 大盘锚: strength=%.1f(%s) | 涨停池命中: %d/%d",
                    _ht_strength, _ht_label,
                    _count_limit_up_hits(base), len(base))

        from core.scan_engine import run_scan_engine
        regime = _resolve_regime_name()
        res = run_scan_engine(["p3"], base, regime=regime, progress_callback=None)
        rows = res.get("p3") or []
        # 【模拟盘】P3 正式候选 → 自动模拟买入（只读主库之外独立账户；异常已内部吞掉）
        _paper_on_signals_safe("p3", rows)
        _push_top_scores("p3", rows if isinstance(rows, list) else [], 3, res if isinstance(res, dict) else None)
    except Exception as e:
        logger.exception("P3 扫描异常: %s", e)
        _notify_daemon_alert(
            "P3 盘中扫描异常",
            str(e)[:900],
            category="scan_p3",
            dedup_key=f"daemon_p3_scan_{_now_bj().strftime('%Y%m%d%H')}",
        )
    finally:
        _gc_after_scan(base=base, res=res)


def _run_scan_push_p4() -> None:
    if _maintenance_mode_skip("P4尾盘"):
        return
    if _daemon_cruise_off_skip("P4尾盘"):
        return
    if not _barrier_trading_day_or_skip("P4尾盘"):
        return
    base = load_base_items_latest()
    if not base:
        logger.warning("P4：底仓为空，跳过扫描")
        return
    res: Any = None
    try:
        # V26.88+9-22 优化10+11：HiThink 大盘指数实时锚 + 涨停/炸板池实时注入
        try:
            from core.strategies._hithink_market_context import hithink_index_snapshot as _get_ht_index
            _ht_ctx = _get_ht_index()
        except Exception:
            _ht_ctx = {}
        _ht_strength = float(_ht_ctx.get("market_strength", 1.0) or 1.0)
        _ht_label = str(_ht_ctx.get("market_label", "正常") or "正常")
        _ht_intp = str(_ht_ctx.get("best_interpretation", "") or "")

        try:
            from core.strategies._hithink_limit_up_context import (
                load_hithink_limit_context as _get_ht_limit,
                inject_limit_context_to_base_items as _inject_limit,
            )
            _ht_limit = _get_ht_limit()
            _inject_limit(base, _ht_limit)
        except Exception as _e_lim4:
            logger.debug("[P4] HiThink 涨停上下文加载失败（已降级）: %s", _e_lim4)
            _ht_limit = {}

        for _item in base:
            _rt = _item.setdefault("rt", {})
            _rt["_hithink_index_strength"] = _ht_strength
            _rt["_hithink_market_label"] = _ht_label
            _rt["_hithink_market_intp"] = _ht_intp

        logger.info("[P4] HiThink 大盘锚: strength=%.1f(%s) | 涨停池命中: %d/%d",
                    _ht_strength, _ht_label,
                    _count_limit_up_hits(base), len(base))

        from core.scan_engine import run_scan_engine
        regime = _resolve_regime_name()
        res = run_scan_engine(["p4"], base, regime=regime, progress_callback=None)
        rows = res.get("p4") or []
        # 【模拟盘】P4 正式候选 → 自动模拟买入（只读主库之外独立账户；异常已内部吞掉）
        _paper_on_signals_safe("p4", rows)
        _push_top_scores("p4", rows if isinstance(rows, list) else [], 3, res if isinstance(res, dict) else None)
    except Exception as e:
        logger.exception("P4 扫描异常: %s", e)
        _notify_daemon_alert(
            "P4 尾盘扫描异常",
            str(e)[:900],
            category="scan_p4",
            dedup_key=f"daemon_p4_scan_{_now_bj().strftime('%Y%m%d%H')}",
        )
    finally:
        _gc_after_scan(base=base, res=res)


def _run_scan_push_p5() -> Tuple[bool, int]:
    if _maintenance_mode_skip("P5盘后"):
        return False, 0
    if _daemon_cruise_off_skip("P5盘后"):
        return False, 0
    if not _barrier_trading_day_or_skip("P5盘后"):
        return False, 0
    base = load_base_items_latest()
    if not base:
        logger.warning("P5：底仓为空，跳过扫描")
        return False, 0
    res: Any = None
    # P5 盘后仅做扫描与推送；不在这里再写入会话表/维护表，避免与晚间同步链抢写 DuckDB。
    try:
        # V26.88+9-22 优化16+17：HiThink 大盘指数实时锚 + 涨停/炸板池实时注入
        try:
            from core.strategies._hithink_market_context import hithink_index_snapshot as _get_ht_index
            _ht_ctx = _get_ht_index()
        except Exception:
            _ht_ctx = {}
        _ht_strength = float(_ht_ctx.get("market_strength", 1.0) or 1.0)
        _ht_label = str(_ht_ctx.get("market_label", "正常") or "正常")
        _ht_intp = str(_ht_ctx.get("best_interpretation", "") or "")

        try:
            from core.strategies._hithink_limit_up_context import (
                load_hithink_limit_context as _get_ht_limit,
                inject_limit_context_to_base_items as _inject_limit,
            )
            _ht_limit = _get_ht_limit()
            _inject_limit(base, _ht_limit)
        except Exception as _e_lim5:
            logger.debug("[P5] HiThink 涨停上下文加载失败（已降级）: %s", _e_lim5)
            _ht_limit = {}

        for _item in base:
            _rt = _item.setdefault("rt", {})
            _rt["_hithink_index_strength"] = _ht_strength
            _rt["_hithink_market_label"] = _ht_label
            _rt["_hithink_market_intp"] = _ht_intp

        logger.info("[P5] HiThink 大盘锚: strength=%.1f(%s) | 涨停池命中: %d/%d",
                    _ht_strength, _ht_label,
                    _count_limit_up_hits(base), len(base))

        from core.scan_engine import run_scan_engine
        regime = _resolve_regime_name()
        res = run_scan_engine(["p5"], base, regime=regime, progress_callback=None)
        rows = res.get("p5") or []
        filtered_rows = []
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                try:
                    score = float(row.get("综合分", 0.0) or 0.0)
                except (TypeError, ValueError):
                    score = 0.0
                if score >= 60.0:
                    filtered_rows.append(row)
        _push_top_scores("p5", filtered_rows, 10, res if isinstance(res, dict) else None)
        return True, len(filtered_rows)
    except Exception as e:
        logger.exception("P5 扫描异常: %s", e)
        _notify_daemon_alert(
            "P5 盘后扫描异常",
            str(e)[:900],
            category="scan_p5",
            dedup_key=f"daemon_p5_scan_{_now_bj().strftime('%Y%m%d%H')}",
        )
        return False, 0
    finally:
        _gc_after_scan(base=base, res=res)


# ---------- 同步核心：返回 bool，供晚间链/早盘链强依赖 ----------

# 最新交易日（MAX(trade_date)）相对次新日行数明显偏少时，对当日再执行 sync_single_day 的总次数
_LATEST_TRADE_DAY_RESYNC_ATTEMPTS = 5


def _is_duckdb_lock_error(err: BaseException) -> bool:
    """
    识别 DuckDB 文件被其他进程占用导致的锁冲突异常。
    """
    raw = str(err or "")
    msg = raw.lower()
    if "database is locked" in msg or "could not set lock" in msg:
        return True
    if "quant_data.duckdb" not in msg:
        return False
    if "cannot open file" in msg or "io error" in msg:
        return True
    if "进程无法访问" in raw or "being used by another" in msg:
        return True
    return False


def _extract_lock_holder_pid(err: BaseException) -> Optional[int]:
    """从 DuckDB 锁异常文本中提取占锁 PID（如：PID 22072）。"""
    m = re.search(r"PID\s*(\d+)", str(err or ""), flags=re.IGNORECASE)
    if not m:
        return None
    try:
        return int(m.group(1))
    except Exception:
        return None


def _resolve_pid_brief(pid: int) -> str:
    """查询 PID 对应进程名，失败时回退为 unknown。"""
    try:
        out = subprocess.check_output(
            ["tasklist", "/FI", f"PID eq {int(pid)}", "/FO", "CSV", "/NH"],
            text=True,
            encoding="utf-8",
            errors="ignore",
        ).strip()
        if not out:
            return "unknown"
        first = out.splitlines()[0].strip()
        if first.upper().startswith("INFO:"):
            return "unknown"
        if first.startswith('"') and "," in first:
            parts = [x.strip().strip('"') for x in first.split('","')]
            if parts and parts[0]:
                return parts[0]
    except Exception:
        pass
    return "unknown"


def _notify_duckdb_lock_holder_once(err: BaseException, *, phase: str, attempt: int) -> None:
    """
    DuckDB 占锁告警（同一 PID + phase + 10 分钟窗口内去重），避免告警风暴。
    """
    pid = _extract_lock_holder_pid(err)
    pid_key = str(pid) if pid is not None else "unknown"
    dedup = f"duckdb_lock_{phase}_{pid_key}"
    now_ts = time.time()
    last_ts = float(_DUCKDB_LOCK_ALERT_TS.get(dedup, 0.0))
    if now_ts - last_ts < 600:
        return
    _DUCKDB_LOCK_ALERT_TS[dedup] = now_ts
    proc_name = _resolve_pid_brief(pid) if pid is not None else "unknown"
    _notify_daemon_alert(
        "DuckDB 文件占锁告警",
        (
            f"阶段={phase} attempt={attempt} 检测到 quant_data.duckdb 被占用；"
            f"holder_pid={pid_key} holder_proc={proc_name}。"
            "守护进程已进入指数退避等待并自动重试。"
        ),
        category="data_sync",
        dedup_key=f"daemon_{dedup}_{_now_bj().strftime('%Y%m%d%H')}",
    )


# 行情数据熔断告警（DL-03 修复）：DataFetchCriticalError 触发企微告警，避免静默吞掉
_DATA_FETCH_CRITICAL_ALERT_TS: Dict[str, float] = {}


def _notify_data_fetch_critical_once(stage: str, err: BaseException) -> None:
    """
    【V26.88+ 9-26 复盘修复】DataFetchCriticalError 是 data_fetcher 设计的实盘熔断异常。
    原设计文档：「抛出前已通过企微发送「实盘熔断」强告警」，但实测仅 raise_data_fetch_critical()
    内部调用企微网关；守护进程捕获后仅 logger.error 后 return=False，没有把异常重新上报。
    本函数补丁：守护进程捕获点也调一次 dedup 过的告警，category=critical，确保值班人收到。
    """
    dedup = f"data_fetch_critical_{stage}"
    now_ts = time.time()
    last_ts = float(_DATA_FETCH_CRITICAL_ALERT_TS.get(dedup, 0.0))
    if now_ts - last_ts < 1800:  # 30 分钟去重
        return
    _DATA_FETCH_CRITICAL_ALERT_TS[dedup] = now_ts
    err_str = str(err)[:480] if err is not None else "未知异常"
    _notify_daemon_alert(
        title=f"🚨 行情数据源熔断 (stage={stage})",
        detail=(
            f"阶段 {stage} 触发 DataFetchCriticalError：\n"
            f"原因：{err_str}\n\n"
            "提示：可能是 Tushare Token 过期、HiThink 配额耗尽、网络代理异常或接口业务级失败。"
            "请尽快检查 token/代理/网络，必要时人工接管。"
        ),
        category="critical",
        dedup_key=f"daemon_data_fetch_critical_{stage}_{_now_bj().strftime('%Y%m%d%H')}",
    )


def _ensure_stock_basic_table_ready() -> bool:
    """
    同步链成功后刷新 stock_basic（行业/简称维表）：
    - 每次成功跑完日线增量后执行全量 sync_stock_basic（L+D+P + daily 缺码补行），
      避免「表已存在但从不刷新」导致新股/映射过期、P1 推送简称仍缺。
    - 失败不阻断主链，但会告警提示后续行业/简称相关能力可能降级。
    """
    if _maintenance_mode_skip("stock_basic维表刷新"):
        return False
    try:
        from data.db_core import table_exists, sync_stock_basic
    except Exception as e:
        logger.warning("stock_basic 兜底检查导入失败（降级忽略）: %s", e)
        return False

    last_err: Optional[Exception] = None
    for attempt in range(3):
        try:
            ok = bool(sync_stock_basic())
            if ok and bool(table_exists("stock_basic")):
                logger.info("stock_basic 维表已刷新 (attempt %s/3)", attempt + 1)
                return True
            raise RuntimeError("sync_stock_basic 返回失败或表仍不存在")
        except Exception as e:
            last_err = e
            if _is_duckdb_lock_error(e):
                _notify_duckdb_lock_holder_once(e, phase="stock_basic", attempt=attempt + 1)
                backoff = min(120.0, 20.0 * (2 ** attempt))
                logger.warning(
                    "stock_basic 自动补齐失败 attempt %s/3（DuckDB占锁）: %s | 退避 %.0fs",
                    attempt + 1,
                    e,
                    backoff,
                )
                time.sleep(backoff)
            else:
                logger.warning("stock_basic 自动补齐失败 attempt %s/3: %s", attempt + 1, e)
                time.sleep(8.0 * (attempt + 1))

    detail = str(last_err)[:900] if last_err is not None else "unknown"
    _notify_daemon_alert(
        "行业维表刷新失败（stock_basic）",
        "自动重试后仍未写入 stock_basic；P1 推送简称、P2~P5 板块/行业相关能力可能降级。"
        f"最后错误：{detail}",
        category="data_sync",
        dedup_key=f"daemon_stock_basic_missing_{_now_bj().strftime('%Y%m%d')}",
    )
    return False


def _resync_max_trade_day_if_sparse() -> bool:
    """
    在 sync_recent_days + 缺失补洞完成后调用：若库内「最新交易日」行数相对「次新日」半残
    （与 P1 日线锚定同一口径：n < max(500, 85%×次新)），则对该日 sync_single_day，
    最多 _LATEST_TRADE_DAY_RESYNC_ATTEMPTS 次；任一次成功后若不再半残即返回 True。
    仍半残则返回 False（晚间/早盘链将不继续 P1，避免在错数据上全量洗盘）。
    """
    if _maintenance_mode_skip("最新交易日半残救场"):
        # 维护模式下不执行 sync_single_day 写库救场；返回 True 以免误将整轮增量同步判失败。
        return True
    import data.fetcher as data_fetcher
    from data.db_core import is_max_trade_date_daily_rows_sparse

    sparse, ymd, n0, n1 = is_max_trade_date_daily_rows_sparse()
    if not sparse or not ymd:
        return True

    logger.warning(
        "【最新日救场】疑似半残 trade_date=%s 行数=%s vs 次新=%s，将对当日重拉最多 %s 次",
        ymd,
        n0,
        n1,
        _LATEST_TRADE_DAY_RESYNC_ATTEMPTS,
    )

    def _cb(msg: str) -> None:
        logger.info("[sync-rescue] %s", msg)

    # 数据源常在收盘后 15:30～20:30 间分批落库；锚定日为当日时先等待，减少「半残→救场仍失败」误报
    if ymd == _now_bj().strftime("%Y%m%d"):
        logger.info("【最新日救场】锚定日为当日，先等待 90s 以利日线接口收全")
        time.sleep(90.0)

    for attempt in range(_LATEST_TRADE_DAY_RESYNC_ATTEMPTS):
        ok_pull = bool(data_fetcher.sync_single_day(ymd, status_callback=_cb))
        sparse2, _, n2, n3 = is_max_trade_date_daily_rows_sparse()
        if not sparse2:
            logger.info(
                "【最新日救场】成功 attempt=%s/%s sync_single_day_ok=%s 最新日 n=%s",
                attempt + 1,
                _LATEST_TRADE_DAY_RESYNC_ATTEMPTS,
                ok_pull,
                n2,
            )
            return True
        logger.warning(
            "【最新日救场】仍半残 attempt=%s/%s sync_single_day_ok=%s 最新日 n=%s vs 次新 n=%s",
            attempt + 1,
            _LATEST_TRADE_DAY_RESYNC_ATTEMPTS,
            ok_pull,
            n2,
            n3,
        )
        if attempt < _LATEST_TRADE_DAY_RESYNC_ATTEMPTS - 1:
            time.sleep(45.0 * (attempt + 1))
    logger.error(
        "【最新日救场】已达最大次数仍半残 trade_date=%s，本轮同步判失败（不继续 P1）",
        ymd,
    )
    return False


def _refresh_weekly_data_after_daily_sync() -> bool:
    """日线确认完成后刷新 60 周聚合；失败仅告警，不阻断实盘主链。"""
    try:
        from data.db_core import close_db
        from tools.weekly_aggregate import run_incremental

        # weekly_aggregate 使用独立 DuckDB 连接；先释放本进程单例，避免 Windows 配置/文件锁冲突。
        close_db()
        run_incremental()
        logger.info("[WEEKLY_AGG] 日线同步后周线刷新完成")
        return True
    except Exception as e:
        logger.exception("[WEEKLY_AGG] 日线同步后周线刷新失败（不阻断 P1/P5）: %s", e)
        _notify_daemon_alert(
            "周线聚合刷新失败",
            f"日线同步已完成，但 weekly_data 刷新失败：{e}\nP1/P3/P4 将暂时使用旧周线或安全降级。",
            category="data_sync",
            dedup_key=f"daemon_weekly_aggregate_fail_{_now_bj().strftime('%Y%m%d')}",
        )
        return False


def _sync_daily_incremental_core() -> bool:
    """
    先执行近端交易日同步，再做缺失交易日补洞（基于交易日历，不依赖自然日）。
    返回 True 表示本轮同步与补洞均成功；False 表示失败，禁止继续 P1。

    【V26.88+9-22】补洞超时或限速时，降级为「只补最近 7 天」，避免 Tushare rate-limit 把
    整晚都耗在历史补洞上，导致当日同步永远无法完成。
    """
    if _maintenance_mode_skip("增量同步 sync_recent_days"):
        return False
    if _daemon_cruise_off_skip("增量同步 sync_recent_days"):
        return False
    import constants
    import data.fetcher as data_fetcher

    last_err: Optional[Exception] = None
    for attempt in range(5):
        try:

            def _cb(msg: str) -> None:
                logger.info("[sync] %s", msg)

            data_fetcher.sync_recent_days(
                days=5,
                status_callback=_cb,
                progress_callback=None,
                raise_on_all_days_failed=True,
            )
            # 第二阶段：补洞模式
            # 【V26.88+9-22】智能补洞：缺 ≤7 天全补；>7 天只补最近 7 天 + 警告（避免全窗口补缺被 Tushare 限速卡死）
            # 【V26.88+10-5】MAX_DAYS 常量已显式定义(=130)，getattr 兜底不再静默回退到 150
            lookback_days = int(getattr(constants, "MAX_DAYS", 150) or 150)
            ok_comp, missing_dates = data_fetcher.check_data_completeness(days=lookback_days)
            if not ok_comp:
                raise RuntimeError("check_data_completeness 未成功，无法确认缺失交易日")
            if missing_dates:
                if len(missing_dates) > 7:
                    logger.warning(
                        "检测到缺失交易日 %s 天（>7），仅补最近 7 天，剩余 %s 天延后补齐",
                        len(missing_dates), len(missing_dates) - 7,
                    )
                    sorted_missing = sorted(missing_dates)
                    missing_dates = sorted_missing[-7:]
                else:
                    logger.info(
                        "检测到缺失交易日 %s 天，开始补洞: %s",
                        len(missing_dates), missing_dates,
                    )
                batch_result = data_fetcher.sync_missing_days_batch(
                    missing_dates,
                    status_callback=_cb,
                    progress_callback=None,
                )
                failed_dates = list(batch_result.get("failed") or [])
                if failed_dates:
                    # 【V26.88+9-22】补洞失败不再 raise（避免 145 天补洞一直卡住导致当晚 P1/P5 永远不跑）
                    logger.warning(
                        "缺失交易日补洞部分失败（%s 只），继续同步链路：%s",
                        len(failed_dates), failed_dates[:5],
                    )
            logger.info("增量同步+缺失补洞完成 (attempt %s)", attempt + 1)
            if not _resync_max_trade_day_if_sparse():
                logger.error("最新交易日救场失败（已对当日重拉多轮），本轮同步判失败")
                return False
            _refresh_weekly_data_after_daily_sync()
            # 非阻断兜底：补齐行业维表，避免后续 P2~P5 因 stock_basic 缺失长期降级。
            _ensure_stock_basic_table_ready()
            return True
        except data_fetcher.DataFetchCriticalError as e:
            # 【V26.88+ 9-26 复盘修复】实盘熔断不仅记日志，需主动推企微告警避免静默
            _notify_data_fetch_critical_once("sync_recent", e)
            # 企微已告警；网络/Token 级问题重试无意义，直接结束本轮
            logger.error("增量同步熔断（Tushare/网络不可用），不重试: %s", e)
            return False
        except Exception as e:
            last_err = e
            if _is_duckdb_lock_error(e):
                _notify_duckdb_lock_holder_once(e, phase="sync_recent", attempt=attempt + 1)
                backoff = min(300.0, 30.0 * (2 ** attempt))
                logger.warning(
                    "增量同步失败 attempt %s（DuckDB占锁）: %s | 退避 %.0fs 后重试",
                    attempt + 1,
                    e,
                    backoff,
                )
                time.sleep(backoff)
            else:
                logger.warning("增量同步失败 attempt %s: %s", attempt + 1, e)
                time.sleep(20.0 * (attempt + 1))
    if last_err is not None:
        logger.error("增量同步最终失败: %s", last_err)
    gc.collect()
    return False


def _evening_incremental_sync_can_skip() -> bool:
    """
    交易日：lookback 内无缺失、最新交易日非半残时，无需再跑 _sync_daily_incremental_core（省流量）。
    """
    try:
        import constants
        import data.fetcher as data_fetcher
        from data.db_core import is_max_trade_date_daily_rows_sparse

        if getattr(data_fetcher, "pro", None) is None:
            return False
        lookback_days = int(getattr(constants, "MAX_DAYS", 150) or 150)
        ok_comp, missing_dates = data_fetcher.check_data_completeness(days=lookback_days)
        if not ok_comp or missing_dates:
            return False
        sparse, ymd, _, _ = is_max_trade_date_daily_rows_sparse()
        if sparse or not ymd:
            return False
        return True
    except Exception as e:
        logger.debug("晚间增量跳过检查: %s", e)
        return False


def _push_p1_ge75_after_daemon_wash() -> None:
    """P1 JSON 落盘后，按 master_control 推送综合分≥65 底仓，最多 12 只（与 UI 洗盘后推送同源）。"""
    if _maintenance_mode_skip("P1高分池企微推送"):
        return
    try:
        from core.notification_gateway import notify_p1_high_score_pool_after_wash

        p = _p1_cache_path_today()
        if not os.path.isfile(p):
            return
        items = _load_base_items_json(p)
        if items:
            notify_p1_high_score_pool_after_wash(items, min_score=60.0, main_score=75.0, max_items=8)
    except Exception as e:
        logger.exception("P1≥75 分企微推送失败: %s", e)


def _evening_hithink_special_pipeline() -> None:
    """
    交易日 19:46（晚间常规同步尾端）：HiThink 特色数据同步（涨停/跌停/龙虎榜/热股榜）。
    完全独立于 Tushare 增量管道：
      - 当 hithink.enabled=false 或无 API Key 时，立即返回，零侵入。
      - 落库到独立 hithink_special_* 表，不与原表冲突。
      - 失败仅记日志，不影响 19:55/20:05 的 P1/P5 链。

    【ST-P0-22 修复】旧实现无论成功/未启用都不写持久状态 → 调度器永远判「今日未完成」，
    在 catchup 60 分钟窗口内每分钟重派一次（实测 19:46→20:46 共 48 次，反复打 HiThink 接口）。
    现在的完成判定：
      - 未启用（无 Key / enabled=false）→ 标记今日完成，不再重试；
      - 至少一个池同步得到 >0 行 → 标记今日完成（部分池 0 行属正常，如当日无涨停）；
      - 全部接口 0 行（DNS/限流/熔断全失败）→ 保留未完成，交给下一分钟补跑。
    """
    today = _now_bj().strftime("%Y%m%d")
    try:
        from data.hithink_special_sync import (
            sync_special_today,
            ensure_tables_then_silent,
        )

        try:
            from data.hithink_client import HithinkClient

            _enabled = bool(getattr(HithinkClient(), "is_enabled", True))
        except Exception:
            _enabled = True
        if not _enabled:
            logger.info("[HiThink] 特色同步未启用（enabled=false 或无 API Key），今日不再重试")
            _write_pipeline_state_patch(last_hithink_special_ok_bj_date=today)
            return
        # 静默建表（首次运行自动建库）
        ensure_tables_then_silent()
        summary = sync_special_today()
        if any(v > 0 for v in summary.values()):
            logger.info("[HiThink] 19:46 特色数据同步完成 | %s", summary)
            _write_pipeline_state_patch(
                last_hithink_special_ok_bj=_now_bj().isoformat(),
                last_hithink_special_ok_bj_date=today,
                last_hithink_special_result=str(summary)[:200],
            )
        else:
            logger.warning(
                "[HiThink] 19:46 特色数据同步全部 0 行（接口/网络异常），保留待补跑 | %s",
                summary,
            )
    except Exception as e:
        logger.warning("[HiThink] 19:46 特色同步异常（已降级忽略，等待补跑）: %s", e)


def _sync_benchmark_indices_after_daily() -> None:
    """
    【V30 新增】晚间日线增量成功后，同步 4 个基准指数（上证/深证/创业板/沪深300）。

    为什么放在这里：
      - index_daily 是「混合基准超额」的**唯一数据源**，缺它则 signal_pnl 的
        bench_* / excess_* 全为 NULL，等于「超额收益」能力从未存在。
      - 指数一天一根 K 线，数据量极小（4 × ~130 根），同步成本可忽略。
      - 放在日线增量之后、19:55 P1 之前：此时 DuckDB 写锁已在本链内持有，
        不需要额外抢锁；指数同步失败绝不影响 P1 / P5。

    铁律：整段永不抛出。指数只是「参考基准」，缺了只能让超额列为 NULL，
    绝不能让晚间主链带崩。
    """
    try:
        from data.index_daily_store import sync_index_quietly

        # 【V30 修复】原实现调 sync_index_daily(db_conn=None)，而该函数对
        # db_conn=None 会直接 return（result["skipped"]=["db_conn=None"]），
        # 于是这个钩子**永远静默跳过、从未真正同步过指数**。
        # 改用 sync_index_quietly()：内部在需要时自己取写连接，
        # 并且整段 try/except，绝不把晚间主链带崩。
        res = sync_index_quietly(lookback_days=130)
        if not res.get("ok"):
            logger.warning("[V30] 基准指数同步未成功（不影响主链）: %s", res.get("skipped"))
            return
        logger.info(
            "[V30] 基准指数同步完成: %d 行 / 指数 %s",
            res.get("written", 0), res.get("per_index", {}),
        )
    except Exception as e:
        logger.warning("[V30] 基准指数同步异常（已忽略，不影响 P1/P5）: %s", e)


def _sync_index_valuation_after_daily() -> None:
    """【V30 新增】晚间日线增量成功后，补齐 dim_index_valuation 大盘估值温度表。

    为什么必须接在这里：
      - compute_market_overheat_score() 读 dim_index_valuation 做环境阀门
        （P1/P3/P4/P5 四个池都依赖）；该表为空时会**静默退化为中性默认**
        （PE=0 / 分位=0.5 / 温度=0 / regime=neutral），体检能看出、人工看不出来。
      - sync_index_valuation_recent() 此前只有定义、**没有任何生产调用方**，
        所以估值表一直是空的（本轮上线体检第 5 个阻塞项）。
      - 位置与基准指数同步一致：日线增量之后、19:55 P1 之前，DuckDB 写锁在本链内。

    铁律：整段永不抛出。估值只是「环境温度参考」，缺了只能让阀门取中性，
    绝不能让晚间主链带崩 P1 / P5。
    """
    try:
        from data.fetcher import sync_index_valuation_recent

        df = sync_index_valuation_recent(years=3)
        if df is None or df.empty:
            logger.warning(
                "[V30] 大盘估值同步无数据（环境阀门将取中性默认，不影响 P1/P5）"
            )
            return
        logger.info("[V30] 大盘估值同步完成: %d 行", len(df))
    except Exception as e:
        logger.warning("[V30] 大盘估值同步异常（已忽略，不影响 P1/P5）: %s", e)


def _sync_trade_calendar_after_daily() -> None:
    """【V30 新增】晚间日线增量成功后，把 SSE 开市日历落库到 dim_trade_calendar。

    为什么必须接在这里：
      - dim_trade_calendar 此前只有空表骨架，tools/db_inspector.py 检查 6 每次体检都报
        WARN（empty_table），是上线前最后一个未消除的告警。
      - 该表是离线/体检视角的日历底座：daemon 交易日闸仍走在线 trade_cal（在线优先、
        失败回退本地缓存），本同步只是让「历史开市日 / 前一交易日 / 后一交易日」可离线查询，
        供体检与事后复盘使用，**不参与任何选股或下单判定**。
      - 位置与估值同步一致：日线增量之后、19:55 P1 之前，DuckDB 写锁在本链内。

    铁律：整段永不抛出。日历缺失只是体检降级，绝不能让晚间主链带崩 P1 / P5。
    """
    try:
        from data.fetcher import sync_trade_calendar

        df = sync_trade_calendar(years=3)
        if df is None or df.empty:
            logger.warning("[V30] 交易日历同步无数据（体检将保留 WARN，不影响 P1/P5）")
            return
        # 【ST-P0-16 修复】原日志写「%d 个开市日」但传的是 len(df)，
        # 而 df 是【全日历】（含休市日 is_open=False）→ 报 1111 个开市日，
        # 实际只有 736 个开市日。与 fetcher 层的「1111 天（开市 736 / 休市 375）」自相矛盾。
        _is_open_col = df.get("is_open")
        if _is_open_col is not None:
            _n_open = int(_is_open_col.sum())
            _n_total = len(df)
            logger.info(
                "[V30] 交易日历同步完成: %d 天（开市 %d / 休市 %d）",
                _n_total, _n_open, _n_total - _n_open,
            )
        else:
            logger.info("[V30] 交易日历同步完成: %d 天（无 is_open 列）", len(df))
    except Exception as e:
        logger.warning("[V30] 交易日历同步异常（已忽略，不影响 P1/P5）: %s", e)


def _sync_daily_derived_features_after_daily() -> bool:
    """【V30.4 2026-10-18 P0】晚间日线增量成功后，把滚动指标派生列补齐。

    为什么必须接在这里：
      - data.fetcher 的轻量 UPSERT 链路只写生肉（行情/估值/资金），不产滚动指标；
      - 此前只有 UI 侧栏「本地数据完整」时才会调 _sync_daily_features()，daemon 生产路径
        从不调用，于是每日落库后 ma20/macd/rsi/kdj/... 长期为 NULL；
      - 位置与指数/估值/日历同步一致：日线增量之后、19:55 P1 之前，DuckDB 写锁在本链内。

    铁律：整段永不抛出，但返回 True/False 作为 P1/P5 的放行闸。
    派生列（ma20/macd/rsi/kdj/...）缺失时，P1 池与 P5 盘后读到的全是 NULL，
    会退化成 0 分或 KeyError —— 此时宁可不推 P1/P5 也不要推错票。
    """
    try:
        from data.fetcher import _sync_daily_features

        ok = bool(_sync_daily_features(lambda m: logger.info("[V30.4] 派生列同步 | %s", m)))
        if not ok:
            logger.error("[V30.4] 派生列同步未全部成功 -> 19:55 P1 / 20:05 P5 不放行")
        return ok
    except Exception as e:
        logger.error("[V30.4] 派生列同步异常 -> 19:55 P1 / 20:05 P5 不放行: %s", e)
        return False


def _daily_data_covers_bj_date(d8: str) -> bool:
    """校验 daily_data 是否真的落了该交易日数据。

    【V30.16 2026-11-05】用于根除「状态位成功但库内无数据」的假成功：
    当日事故中上游把「交易日历为空」当成「今日无待办」返回成功，
    last_sync_ok_bj_date 被写成 20261105，而 daily_data 实际最新仍是 11-04。
    """
    try:
        _d8 = str(d8 or "").strip().replace("-", "")[:8]
        if len(_d8) != 8 or not _d8.isdigit():
            return False
        _dash = f"{_d8[:4]}-{_d8[4:6]}-{_d8[6:8]}"
        from data.db_core import get_read_conn

        with get_read_conn() as con:
            row = con.execute(
                "SELECT count(*) FROM daily_data WHERE trade_date IN (?, ?)", [_dash, _d8]
            ).fetchone()
        return bool(row and int(row[0] or 0) > 0)
    except Exception as e:
        logger.debug("校验 daily_data 覆盖失败: %s", e)
        return False


def _evening_sync_only_pipeline() -> None:
    """
    交易日 19:45：仅日线增量同步（含补洞、半残救场）；不执行 P1/P5。
    若库内已覆盖最新交易日且无缺失、非半残，则跳过拉取，仅写入 last_sync_ok 供 19:55/20:05 链使用。
    """
    if _maintenance_mode_skip("晚间数据链(仅同步)"):
        return
    if _daemon_cruise_off_skip("晚间数据链(仅同步)"):
        return
    if not _daemon_is_trading_day_safe():
        logger.info("非交易日，跳过晚间增量同步")
        return
    today = _now_bj().strftime("%Y%m%d")
    if _evening_incremental_sync_can_skip():
        logger.info("晚间增量同步：库内已为最新且无半残，跳过拉取 | BJ日期=%s", today)
        _refresh_weekly_data_after_daily_sync()
        if not _sync_daily_derived_features_after_daily():
            _write_pipeline_state_patch(last_sync_ok_bj_date="", last_sync_fail_bj_date=today)
            _notify_daemon_alert(
                "晚间链：派生列完整性闸未通过",
                f"日期 {today}：日线已最新但滚动指标/分层特征未补齐，**19:55** P1 与 **20:05** P5 不放行。",
                category="data_sync",
                dedup_key=f"daemon_evening_derived_gate_fail_syncskip_{today}",
            )
            return
        if not _daily_data_covers_bj_date(today):
            logger.error(
                "[晚间链] %s 判定可跳过拉取，但 daily_data 无该日数据，拒绝写 last_sync_ok（防假成功）", today
            )
            _write_pipeline_state_patch(last_sync_ok_bj_date="", last_sync_fail_bj_date=today)
            return
        if not _write_pipeline_state_patch(last_sync_ok_bj_date=today, last_sync_fail_bj_date=""):
            _notify_daemon_alert(
                "晚间链：状态位写入失败",
                f"日期 {today}：晚间增量同步已跳过拉取，但 last_sync_ok_bj_date 写盘失败。",
                category="data_sync",
                dedup_key=f"daemon_pipeline_state_write_fail_syncskip_{today}",
            )
        return
    logger.info("晚间增量同步开始 | BJ日期=%s", today)
    ok = _sync_daily_incremental_core()
    if not ok:
        logger.error("晚间同步失败（19:55/20:05 将跳过 P1/P5 链）")
        if not _write_pipeline_state_patch(last_sync_ok_bj_date="", last_sync_fail_bj_date=today):
            _notify_daemon_alert(
                "晚间链：状态位写入失败",
                f"日期 {today}：晚间增量同步失败后，last_sync_fail_bj_date 写盘失败。",
                category="data_sync",
                dedup_key=f"daemon_pipeline_state_write_fail_syncfail_{today}",
            )
        _notify_daemon_alert(
            "晚间链：日线增量同步失败",
            (
                f"计划触发：北京时间 **19:45**（仅增量，不含 P1/P5）\n"
                f"告警时间：{_now_bj().strftime('%Y-%m-%d %H:%M:%S')}（常为整轮重试结束时刻，非整点触发）\n"
                f"日期 {today}：增量同步（含 sync_recent_days 与补洞）多轮重试仍失败，或最新交易日半残救场仍失败，已跳过当日同步；**19:55** P1 与 **20:05** P5 将不跑。\n"
                "若失败总出现在旧整点，多半是守护进程未重启、仍在跑旧时间表，请重启 auto_sniper_daemon。\n"
                "详情见 data/runtime/sniper.log 末条异常。"
            ),
            category="data_sync",
            dedup_key=f"daemon_evening_sync_fail_{today}",
        )
        return
    # 【V30.4 P0】派生列闸前置：日线写入成功 ≠ 数据可用。必须先补齐滚动指标与
    # feat_daily_* 分层表，再写 last_sync_ok_bj_date 放行 19:55 P1 / 20:05 P5。
    if not _sync_daily_derived_features_after_daily():
        _write_pipeline_state_patch(last_sync_ok_bj_date="", last_sync_fail_bj_date=today)
        _notify_daemon_alert(
            "晚间链：派生列完整性闸未通过",
            f"日期 {today}：日线增量已落库但滚动指标/分层特征补齐失败，**19:55** P1 与 **20:05** P5 不放行。\n"
            "详情见 data/runtime/sniper.log 中 [派生列同步] 关键字。",
            category="data_sync",
            dedup_key=f"daemon_evening_derived_gate_fail_{today}",
        )
        return
    if not _daily_data_covers_bj_date(today):
        logger.error(
            "[晚间链] %s 增量同步报成功，但 daily_data 查不到该日数据，拒绝写 last_sync_ok（防假成功）", today
        )
        _write_pipeline_state_patch(last_sync_ok_bj_date="", last_sync_fail_bj_date=today)
        _notify_daemon_alert(
            "晚间链：日线未真正落库",
            f"日期 {today}：同步流程报成功，但库内无该交易日数据，已拒绝放行 19:55 P1 / 20:05 P5。",
            category="data_sync",
            dedup_key=f"daemon_sync_no_rows_{today}",
        )
        return
    if not _write_pipeline_state_patch(last_sync_ok_bj_date=today, last_sync_fail_bj_date=""):
        _notify_daemon_alert(
            "晚间链：状态位写入失败",
            f"日期 {today}：晚间增量同步成功，但 last_sync_ok_bj_date 写盘失败。",
            category="data_sync",
            dedup_key=f"daemon_pipeline_state_write_fail_syncok_{today}",
        )
    logger.info("晚间增量同步完成 | BJ日期=%s（19:55 将执行 P1+推送；20:05 将执行 P5；20:30 将安全压缩）", today)
    # 【V30 新增】基准指数同步（上证/深证/创业板/沪深300）。
    # 位置放在日线增量之后、P1 之前：此时 DuckDB 写锁在本链内，
    # 失败只 logger.warning，绝不影响 19:55 P1 / 20:05 P5。
    _sync_benchmark_indices_after_daily()
    _sync_index_valuation_after_daily()
    _sync_trade_calendar_after_daily()
    _notify_daemon_alert(
        "晚间链：日线增量同步已完成",
        f"日期 {today}（北京时间）：自动增量同步成功；**19:55** P1 与高分推送；**20:05** P5 盘后扫描；**20:30** 安全压缩。",
        category="data_sync",
        dedup_key=f"daemon_evening_sync_ok_{today}",
    )


def _evening_p1_rebuild_pipeline() -> None:
    """
    交易日 19:55：P1 全量重建 → 高分池推送（不含 P5；P5 由 20:05 独立任务执行）。
    仅当当日 19:45 已标记同步成功（pipeline_state.last_sync_ok_bj_date==今日，含「已最新跳过」路径）。
    """
    if _maintenance_mode_skip("晚间P1重建"):
        return
    if _daemon_cruise_off_skip("晚间P1重建"):
        return
    if not _daemon_is_trading_day_safe():
        logger.info("非交易日，跳过晚间 P1 重建")
        return
    today = _now_bj().strftime("%Y%m%d")
    st = _read_pipeline_state()
    last_sync_ok = str(st.get("last_sync_ok_bj_date", "") or "")
    if (str(st.get("last_p1_ok_bj_date", "") or "") == today
            or str(st.get("last_p1_degraded_bj_date", "") or "") == today):
        logger.info("晚间P1：今日已完成（含降级口径），跳过重复执行 | BJ日期=%s", today)
        return
    if last_sync_ok != today:
        logger.warning(
            "晚间P1：当日尚未标记增量同步成功(last_sync_ok=%s)，本次不执行；调度器将在补跑窗口内重试",
            last_sync_ok or "(空)",
        )
        return
    logger.info("晚间 P1 重建开始 | BJ日期=%s", today)
    p1_ok = False
    for _p1_try in range(3):
        p1_ok = _rebuild_p1_cache_job(require_trading_day_barrier=False)
        if p1_ok:
            break
        logger.warning("P1 重建未成功，%.0fs 后重试 (%s/3)", 45.0 * (_p1_try + 1), _p1_try + 2)
        time.sleep(45.0 * (_p1_try + 1))
    if p1_ok:
        _p1_quality = _mark_pool_quality("p1", today)
        logger.info("晚间 P1 重建完成 | BJ日期=%s | 数据质量=%s", today, _p1_quality)
        _push_p1_ge75_after_daemon_wash()
        _notify_daemon_alert(
            "晚间链：P1 底仓与高分推送已完成",
            f"日期 {today}（北京时间）：P1 已落盘并完成高分池推送流程；**20:05** 将执行 P5 盘后扫描。",
            category="data_sync",
            dedup_key=f"daemon_evening_p1_chain_ok_{today}",
        )
        logger.info("晚间链：P1 与高分推送完成，P5 由 20:05 定时任务执行")
    else:
        logger.error("晚间链：P1 重建未成功，请检查日志")
        _notify_daemon_alert(
            "晚间链：P1 底仓重建失败",
            f"日期 {today}：当日增量已同步成功，但 P1 重建未落盘，请查守护进程日志。",
            category="scan_p1",
            dedup_key=f"daemon_evening_p1_fail_{today}",
        )


def _evening_p5_only_pipeline() -> None:
    """
    交易日 20:05：仅 P5 盘后扫描与企微推送。
    要求当日 19:45 已标记同步成功，且 19:55 P1 已成功落盘（last_p1_ok_bj_date==今日）。
    """
    if _maintenance_mode_skip("晚间P5盘后"):
        return
    if _daemon_cruise_off_skip("晚间P5盘后"):
        return
    if not _daemon_is_trading_day_safe():
        logger.info("非交易日，跳过晚间 P5 盘后")
        return
    today = _now_bj().strftime("%Y%m%d")
    st = _read_pipeline_state()
    last_sync_ok = str(st.get("last_sync_ok_bj_date", "") or "")
    last_p1_ok = str(st.get("last_p1_ok_bj_date", "") or "")
    if (str(st.get("last_p5_ok_bj_date", "") or "") == today
            or str(st.get("last_p5_degraded_bj_date", "") or "") == today):
        logger.info("晚间P5：今日已完成（含降级口径），跳过重复执行 | BJ日期=%s", today)
        return
    if last_sync_ok != today:
        logger.warning(
            "晚间P5：等待落库失败——未找到当日增量同步成功标记(last_sync_ok_bj_date=%s, today=%s)，跳过 P5",
            last_sync_ok or "(空)",
            today,
        )
        return
    last_p1_deg = str(st.get("last_p1_degraded_bj_date", "") or "")
    if last_p1_ok != today and last_p1_deg != today:
        logger.warning(
            "晚间P5：等待落库失败——未找到当日 P1 落盘标记"
            "(last_p1_ok_bj_date=%s, last_p1_degraded_bj_date=%s, today=%s)，跳过 P5",
            last_p1_ok or "(空)",
            last_p1_deg or "(空)",
            today,
        )
        return
    logger.info("晚间 P5 盘后扫描开始 | BJ日期=%s | last_sync_ok_bj_date=%s | last_p1_ok_bj_date=%s", today, last_sync_ok, last_p1_ok)
    p5_ok, selected_count = _run_scan_push_p5()
    if p5_ok:
        _p5_quality = _mark_pool_quality("p5", today, selected_count)
        if _p5_quality == "ok":
            _notify_daemon_alert(
                "晚间链：P5 盘后扫描已完成",
                f"日期 {today}（北京时间）：P5 盘后扫描和推送流程完成，正式候选 {int(selected_count)} 只。",
                category="scan_p5",
                dedup_key=f"daemon_evening_p5_ok_{today}",
            )
        else:
            # 降级告警已由 _mark_pool_quality 单独发出，这里不再重复推送，避免一天两条。
            logger.warning(
                "晚间 P5 完成但数据降级，未标记为 ok | status=%s | 候选=%s",
                _p5_quality, selected_count,
            )
    else:
        _notify_daemon_alert(
            "晚间链：P5 盘后扫描未完成",
            f"日期 {today}：P5 扫描未成功完成，调度器将在补跑窗口内重试；详情见 data/runtime/sniper.log。",
            category="scan_p5",
            dedup_key=f"daemon_evening_p5_incomplete_{today}",
        )


def _morning_trading_catchup() -> None:
    """
    【周末穿越补丁】交易日早盘 08:50：
    - 以晚间 19:45 同步（或已最新跳过）为准：若 pipeline_state 显示「当日晚间同步已成功」且当日 P1 文件存在，则直接跳过。
    - 仅在晚间同步未成功，或当日 P1 文件缺失时，执行早盘兜底链。
    """
    if _maintenance_mode_skip("早盘补位(同步+条件P1)"):
        return
    if _daemon_cruise_off_skip("早盘补位(同步+条件P1)"):
        return
    if not _daemon_is_trading_day_safe():
        return
    today = _now_bj().strftime("%Y%m%d")
    p_today = _p1_cache_path_today()
    need_p1 = not os.path.isfile(p_today)
    st = _read_pipeline_state()
    last_sync_ok = str(st.get("last_sync_ok_bj_date", "") or "")
    evening_sync_ok_today = last_sync_ok == today
    logger.info("早盘补位 | 当日P1缓存存在=%s | path=%s", not need_p1, p_today)
    if evening_sync_ok_today and not need_p1:
        logger.info("早盘补位跳过：已确认晚间同步成功且当日 P1 缓存已存在（以晚间结果为准）")
        return
    if evening_sync_ok_today and need_p1:
        logger.warning("早盘补位：晚间同步已成功，但当日 P1 缓存缺失，执行仅补建 P1（跳过增量同步）")
        for _p1_try in range(3):
            if _rebuild_p1_cache_job(require_trading_day_barrier=False):
                _p1_quality = _mark_pool_quality("p1", today)
                logger.info("早盘仅补建 P1 完成 | 数据质量=%s", _p1_quality)
                gc.collect()
                return
            logger.warning("早盘 P1 补建失败，%.0fs 后重试 (%s/3)", 45.0 * (_p1_try + 1), _p1_try + 2)
            time.sleep(45.0 * (_p1_try + 1))
        logger.error("早盘仅补建 P1 未成功，请检查日志")
        _notify_daemon_alert(
            "早盘链：P1 补建失败",
            f"日期 {today}：晚间同步已成功，但当日 P1 文件缺失且早盘补建失败。",
            category="scan_p1",
            dedup_key=f"daemon_morning_p1_only_fail_{today}",
        )
        return
    ok = _sync_daily_incremental_core()
    if not ok:
        logger.warning("早盘同步失败；若缺当日P1文件，扫描仍将回退到最近 JSON")
        if need_p1:
            logger.warning("早盘同步失败且当日 P1 缓存缺失，尝试应急补建 P1（最多 3 轮）")
            emergency_p1_ok = False
            for _p1_try in range(3):
                if _rebuild_p1_cache_job(require_trading_day_barrier=False):
                    emergency_p1_ok = True
                    break
                logger.warning(
                    "早盘应急 P1 补建失败，%.0fs 后重试 (%s/3)",
                    45.0 * (_p1_try + 1),
                    _p1_try + 2,
                )
                time.sleep(45.0 * (_p1_try + 1))
            if emergency_p1_ok:
                _p1_quality = _mark_pool_quality("p1", today)
                logger.info(
                    "早盘同步失败后应急 P1 补建成功，继续使用当日底仓文件 | 数据质量=%s",
                    _p1_quality,
                )
                return
        _notify_daemon_alert(
            "早盘链：增量同步失败",
            f"日期 {today}：早盘补位 sync_recent_days 未成功；若缺当日 P1 文件，盘中可能回退到最近 JSON。",
            category="data_sync",
            dedup_key=f"daemon_morning_sync_fail_{today}",
        )
        return
    if not _write_pipeline_state_patch(last_morning_sync_ok_bj_date=today):
        _notify_daemon_alert(
            "早盘链：状态位写入失败",
            f"日期 {today}：早盘增量同步成功，但 last_morning_sync_ok_bj_date 写盘失败。",
            category="data_sync",
            dedup_key=f"daemon_pipeline_state_write_fail_morning_syncok_{today}",
        )
    if need_p1:
        logger.warning("当日 P1 缓存缺失，早盘同步后触发补建")
        for _p1_try in range(3):
            if _rebuild_p1_cache_job(require_trading_day_barrier=False):
                break
            logger.warning("早盘 P1 补建失败，%.0fs 后重试 (%s/3)", 45.0 * (_p1_try + 1), _p1_try + 2)
            time.sleep(45.0 * (_p1_try + 1))
    gc.collect()


def _rebuild_p1_cache_job(*, require_trading_day_barrier: bool = True) -> bool:
    """
    重建 P1 并写入当日 JSON + DuckDB p1_cache。
    require_trading_day_barrier=False：由晚间链/早盘链外层已判定交易日时传入，避免重复 trade_cal。
    返回是否完整跑通未抛致命错误；若当日 JSON 已带 UI_MANUAL 主权则跳过落盘但仍返回 True（以人工底仓为准）。
    """
    if _maintenance_mode_skip("P1重建"):
        return False
    if _daemon_cruise_off_skip("P1重建"):
        return False
    if require_trading_day_barrier:
        if not _barrier_trading_day_or_skip("P1重建"):
            return False
    mock_raw: List[Dict[str, Any]] = []
    rejected: List[Any] = []
    ok_out = False
    try:
        from data.db_core import get_all_stock_codes, get_p1_candidate_codes, get_stock_data_qfq
        from core.pool_manager import build_p1_pool_and_cache
        from core.runtime_data_paths import path_p1_cache_json, ensure_runtime_data_layout

        ensure_runtime_data_layout()
        target_codes: List[str] = []
        for wait_round in range(4):
            target_codes = get_p1_candidate_codes() or []
            if not target_codes:
                target_codes = get_all_stock_codes() or []
            target_codes = list(dict.fromkeys(target_codes))
            if target_codes:
                break
            if wait_round < 3:
                delay = 30.0 * (wait_round + 1)
                logger.warning(
                    "P1 重建：候选代码为空（常见于另一进程占锁 DuckDB），%.0fs 后重试 (%s/4)",
                    delay,
                    wait_round + 2,
                )
                time.sleep(delay)
        if not target_codes:
            logger.error("P1 重建：无候选代码")
            _notify_daemon_alert(
                "P1 重建：无候选代码",
                "get_p1_candidate_codes / get_all_stock_codes 在多次重试后仍为空。"
                "请确认 quant_data.duckdb 未被 Streamlit/其它 python 长期占写锁，且 daily_data 已有数据。",
                category="scan_p1",
                dedup_key=f"daemon_p1_no_codes_{_now_bj().strftime('%Y%m%d')}",
            )
            return False

        # 连续错误计数器：单票级别瞬时错误容忍 3 次；超过则把该票加入"本轮可疑名单"，便于排查
        _transient_keys = (
            "timeout", "timed out", "temporarily", "connection",
            "rate limit", "429", "503", "reset", "aborted",
        )
        p1_skip_logged = 0
        for c in target_codes:
            try:
                df = get_stock_data_qfq(c, limit=120)
                if df is not None and not df.empty:
                    mock_raw.append({"code": c, "df": df, "hist": df.iloc[-1].to_dict()})
            except Exception as e:
                msg = str(e) or ""
                is_transient = any(k in msg.lower() for k in _transient_keys)
                level = logger.debug if is_transient else logger.warning
                level("P1 单票%s %s: %s",
                      "暂跳" if is_transient else "跳过", c, e)
                # 每轮最多 8 条告警刷屏保护：前 8 条全部打印，超过则采样
                if not is_transient and p1_skip_logged < 8:
                    p1_skip_logged += 1

        if not mock_raw:
            logger.error("P1 重建：mock_raw 为空")
            _notify_daemon_alert(
                "P1 重建：K 线数据全空",
                f"候选 {len(target_codes)} 只均未读到有效 K 线，请检查 DuckDB 与占用。",
                category="scan_p1",
                dedup_key=f"daemon_p1_mock_empty_{_now_bj().strftime('%Y%m%d')}",
            )
            return False

        regime_name = _resolve_regime_name()
        base_items, rejected = build_p1_pool_and_cache(
            mock_raw,
            progress_callback=None,
            regime_name=regime_name,
        )
        today = _now_bj().strftime("%Y%m%d")
        out_path = path_p1_cache_json(today)

        # 【V30.14 2026-11-05】守护进程每日自动重建 P1 时，把 1011 条淘汰原因也落盘。
        # 旧行为只打「入池 30 拒绝 1011」一行，淘汰原因直接丢弃 → 事后无法回答
        # 「某只票今天为什么没进池」。这里写 state/p1_rejected_cache.json（UI 兼容纯 list）
        # + sidecar 元信息，并在日志里直接给出死因分布，落盘失败不阻断主池。
        try:
            from core.p1_rejected_persist import format_summary_line, persist_p1_rejected

            _rej_summary = persist_p1_rejected(
                rejected,
                pool_size=len(base_items),
                trade_date=today,
                source="DAEMON_AUTO",
                extra_meta={"regime_name": regime_name},
            )
            if _rej_summary.get("ok"):
                logger.info("[P1 淘汰溯源] %s", format_summary_line(_rej_summary))
            else:
                logger.warning("[P1 淘汰溯源] 淘汰原因落盘失败，本次仅保留计数")
        except Exception as _e_rej:
            logger.warning("[P1 淘汰溯源] 落盘异常(不影响主池): %s", _e_rej)
        try:
            from core.pool_manager import p1_cache_json_should_skip_daemon_overwrite

            if p1_cache_json_should_skip_daemon_overwrite(out_path):
                logger.info(
                    "P1 重建：已跳过 JSON 与 DuckDB p1_cache 覆写（保留 UI_MANUAL 人工底仓）| %s",
                    out_path,
                )
                ok_out = True
            else:
                _save_base_items_json(out_path, base_items, p1_envelope_source="DAEMON_AUTO")
                try:
                    from data.db_core import save_p1_cache

                    save_p1_cache(today, base_items)
                except Exception as e:
                    logger.warning("save_p1_cache DuckDB 侧失败(可忽略): %s", e)
                logger.info("P1 重建完成：入池 %s 拒绝 %s → %s", len(base_items), len(rejected), out_path)
                ok_out = True
        except Exception as e:
            logger.warning("P1 重建主权预检异常，按原路径尝试落盘: %s", e)
            _save_base_items_json(out_path, base_items, p1_envelope_source="DAEMON_AUTO")
            try:
                from data.db_core import save_p1_cache

                save_p1_cache(today, base_items)
            except Exception as e2:
                logger.warning("save_p1_cache DuckDB 侧失败(可忽略): %s", e2)
            logger.info("P1 重建完成：入池 %s 拒绝 %s → %s", len(base_items), len(rejected), out_path)
            ok_out = True
    except Exception as e:
        logger.exception("P1 重建异常: %s", e)
        _notify_daemon_alert(
            "P1 重建过程异常",
            str(e)[:900],
            category="scan_p1",
            dedup_key=f"daemon_p1_exc_{_now_bj().strftime('%Y%m%d')}",
        )
        ok_out = False
    finally:
        for it in mock_raw:
            if isinstance(it, dict):
                it.pop("df", None)
        mock_raw.clear()
        try:
            del rejected
        except Exception:
            pass
        gc.collect()
    return ok_out


def _spawn(
    name: str,
    fn: Callable[[], None],
    *,
    wait_for_lock_sec: Optional[float] = None,
) -> None:
    """
    后台线程执行任务；主循环永不在此阻塞（仅 start 线程，瞬时返回）。

    wait_for_lock_sec=None：非阻塞抢锁。
    wait_for_lock_sec>0：子线程内限时阻塞等锁。
    这是全局串行闸门，避免晚间同步 / P1 / P5 / 快照 / 盘中扫描同时写 DuckDB。
    """

    with _ACTIVE_SPAWN_LOCK:
        if name in _ACTIVE_SPAWN_NAMES:
            logger.debug("[%s] 同名任务仍在等待或执行，本轮不重复派生", name)
            return
        _ACTIVE_SPAWN_NAMES.add(name)

    def _runner() -> None:
        acquired = False
        task_ok = False
        task_note = ""
        try:
            if wait_for_lock_sec is None:
                got = _SCAN_BUSY.acquire(blocking=False)
            else:
                got = _SCAN_BUSY.acquire(timeout=float(wait_for_lock_sec))
            if not got:
                logger.warning("[%s] 未获取全局串行锁 _SCAN_BUSY（%s），跳过本次", name, wait_for_lock_sec)
                if name in ("EVENING_SYNC", "EVENING_P1", "EVENING_P5"):
                    _notify_daemon_alert(
                        "晚间链：未获得任务锁（已跳过）",
                        f"任务={name} 北京时间 {_now_bj().strftime('%Y-%m-%d %H:%M:%S')} 起 {float(wait_for_lock_sec):.0f}s 内未能占用同步锁，"
                        "本分钟晚间定时任务未执行。请确认无其它进程长时间占用 DuckDB（如 Streamlit 大数据操作），或查看 data/runtime/sniper.log。",
                        category="data_sync",
                        dedup_key=f"daemon_evening_lock_skip_{name}_{_now_bj().strftime('%Y%m%d%H%M')}",
                    )
                try:
                    from data.daemon_observability import mark_task_result

                    mark_task_result(name, False, "未获得全局串行锁 _SCAN_BUSY，本轮跳过")
                except Exception:
                    pass
                return
            acquired = True
            fn()
            task_ok = True
        except Exception as e:
            task_note = f"{type(e).__name__}: {str(e)[:180]}"
            logger.exception("[%s] 任务异常(已记录): %s", name, e)
        except BaseException as e:
            task_note = f"{type(e).__name__}: {str(e)[:180]}"
            logger.critical("[%s] 致命基类异常: %s", name, e, exc_info=True)
            raise
        finally:
            # V29：任务执行结果写入可观测矩阵（仅观测，不改变任何业务行为）
            try:
                from data.daemon_observability import mark_task_result

                mark_task_result(name, task_ok, task_note or "ok")
            except Exception:
                pass
            if acquired:
                try:
                    _SCAN_BUSY.release()
                except RuntimeError as re:
                    logger.error("[%s] 释放全局串行锁 _SCAN_BUSY 异常(忽略): %s", name, re)
            with _ACTIVE_SPAWN_LOCK:
                _ACTIVE_SPAWN_NAMES.discard(name)

    try:
        threading.Thread(target=_runner, name=name, daemon=True).start()
    except Exception as e:
        with _ACTIVE_SPAWN_LOCK:
            _ACTIVE_SPAWN_NAMES.discard(name)
        logger.exception("线程派生失败 %s: %s", name, e)


def _job_p3_tick() -> None:
    """P3：仅非阻塞抢锁；与 P4 尾盘窗互斥逻辑在 clock_aligned 层处理。"""
    if not _daemon_is_trading_day_safe():
        logger.info("非交易日，跳过: P3巡逻")
        return
    # 【ST-P0-21 修复】窗口判定必须放在派发之前。
    # 旧实现只在工作线程 _run_scan_push_p3 内判窗口 → 盘后每 150s 仍派生一次线程，
    # 抢不到 _SCAN_BUSY 就打一条 WARNING（实测 19:12–22:45 共 31 条噪声）。
    if not _in_p3_patrol_window():
        logger.debug("非 P3 盘中巡逻窗口，跳过派发: P3巡逻")
        return
    _spawn("P3", _run_scan_push_p3, wait_for_lock_sec=None)


def _job_p3_tick_clock_aligned() -> None:
    """P3 按时间桶每档至多派发一次；主循环错过精确秒也不会整拍丢失。"""
    global _P3_LAST_DISPATCH_BUCKET
    if _in_p4_tail_priority_window():
        return
    n = _now_bj()
    sec_of_day = int(n.hour) * 3600 + int(n.minute) * 60 + int(n.second)
    bucket = f"{n.strftime('%Y%m%d')}:{sec_of_day // max(1, int(P3_POLL_INTERVAL_SECONDS))}"
    if bucket == _P3_LAST_DISPATCH_BUCKET:
        return
    _P3_LAST_DISPATCH_BUCKET = bucket
    _job_p3_tick()


def _job_p4_tick() -> None:
    # 尾盘最高优先级：P4 在尾盘窗内按节拍触发；子线程内最多等锁 900s
    if not _daemon_is_trading_day_safe():
        logger.info("非交易日，跳过: P4尾盘")
        return
    _spawn("P4", _run_scan_push_p4, wait_for_lock_sec=P4_TAIL_WAIT_LOCK_SEC)


def _job_p4_tick_clock_aligned() -> None:
    """P4 固定五拍 14:31/36/41/46/51；同一分钟内只派发一次，不依赖精确秒。"""
    n = _now_bj()
    minute_of_day = int(n.hour) * 60 + int(n.minute)
    if minute_of_day not in P4_TAIL_SHOT_MINUTES:
        return
    minute_key = n.strftime("%Y%m%d%H%M")
    if minute_key in _P4_DISPATCHED_MINUTE_KEYS:
        return
    _P4_DISPATCHED_MINUTE_KEYS.add(minute_key)
    if len(_P4_DISPATCHED_MINUTE_KEYS) > 16:
        today = n.strftime("%Y%m%d")
        _P4_DISPATCHED_MINUTE_KEYS.intersection_update(
            key for key in _P4_DISPATCHED_MINUTE_KEYS if key.startswith(today)
        )
    _job_p4_tick()


def _job_p5_tick() -> None:
    """
    保留：手工/扩展用；正常晚间 P5 由 20:05 _job_evening_p5 调度。
    """
    if not _daemon_is_trading_day_safe():
        logger.info("非交易日，跳过: P5盘后")
        return
    _spawn("P5", _run_scan_push_p5, wait_for_lock_sec=7200.0)


def _job_evening_sync_only() -> None:
    _spawn("EVENING_SYNC", _evening_sync_only_pipeline, wait_for_lock_sec=600.0)


def _job_evening_hithink_special() -> None:
    """
    交易日 19:46：HiThink 特色数据同步（涨停/跌停/龙虎榜/热股榜/飙升榜/异动）。
    完全独立于 Tushare 增量链；不影响 19:55/20:05 的 P1/P5 链。
    当 hithink.enabled=false 时立即返回，零侵入。

    【锁策略】wait_for_lock_sec=120s，仍串行加入全局 _SCAN_BUSY 闸门；
    19:45 EVENING_SYNC 链此时已释放锁，19:55 EVENING_P1 链尚未到达，互不冲突。
    """
    try:
        _spawn("EVENING_HITHINK", _evening_hithink_special_pipeline, wait_for_lock_sec=120.0)
    except Exception as e:
        logger.warning("[HiThink] 19:46 spawn 异常（已降级忽略）: %s", e)


def _job_evening_p1_rebuild() -> None:
    _spawn("EVENING_P1", _evening_p1_rebuild_pipeline, wait_for_lock_sec=600.0)


def _job_evening_p5() -> None:
    _spawn("EVENING_P5", _evening_p5_only_pipeline, wait_for_lock_sec=7200.0)


def _evening_vacuum_pipeline() -> None:
    """
    V26.88 晚间数据压缩任务（固定 19:50 执行）：
    停Daemon + DuckDB EXPORT/IMPORT 彻底压缩（强收敛体积）。

    流程：
    1. 打开维护锁
    2. 等待8秒让守护完成当前事务
    3. 终止所有 auto_sniper_daemon 进程（释放DuckDB锁）
    4. EXPORT DATABASE → PARQUET临时目录
    5. IMPORT DATABASE → 新库文件
    6. 校验 daily_data 行数一致性
    7. 清理旧备份（保留最新2个交易日，在原子替换之前执行避免误删）
    8. 原子替换旧库（保留 .bak 备份）
    9. 清理临时文件 + 解除维护锁
    10. 守护进程约60秒后自动重启
    """
    if _maintenance_mode_skip("晚间VACUUM"):
        return
    if _daemon_cruise_off_skip("晚间VACUUM"):
        return
    if not _daemon_is_trading_day_safe():
        logger.info("非交易日，跳过晚间VACUUM")
        return

    from core.master_control import write_master_control
    from data.db_core import duckdb_disk_bytes_total

    PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
    DB_PATH = os.path.join(PROJECT_ROOT, "data", "quant_data.duckdb")
    DATA_DIR = os.path.join(PROJECT_ROOT, "data")

    ts_str = _now_bj().strftime("%Y%m%d_%H%M%S")
    tmp_dir = os.path.join(PROJECT_ROOT, "data", "runtime", f"db_compact_export_{ts_str}")
    new_db = os.path.join(PROJECT_ROOT, "data", f"quant_data_compact_{ts_str}.duckdb")
    bak_db = os.path.join(PROJECT_ROOT, "data", f"quant_data_{ts_str}.bak.duckdb")

    def _cleanup_old_backups(keep_count: int = 2) -> None:
        """清理旧备份，仅保留最新 N 个（按文件名时间戳降序）"""
        import re as _re
        pattern = _re.compile(r"^quant_data_(\d{8}_\d{6})\.bak\.duckdb$")
        bak_files = []
        for f in os.listdir(DATA_DIR):
            m = pattern.match(f)
            if m:
                bak_files.append((m.group(1), f))
        if not bak_files:
            return
        bak_files.sort(key=lambda x: x[0], reverse=True)
        to_delete = bak_files[keep_count:]
        if not to_delete:
            logger.info("晚间VACUUM：备份清理（无需删除，最新 %d 个已保留）", keep_count)
            return
        deleted_size = 0
        for _, fname in to_delete:
            fpath = os.path.join(DATA_DIR, fname)
            try:
                s = os.path.getsize(fpath)
                os.remove(fpath)
                deleted_size += s
                logger.info("晚间VACUUM：已删除旧备份 %s（%.2fMB）", fname, s / 1024 / 1024)
            except Exception as e:
                logger.warning("晚间VACUUM：删除旧备份 %s 失败: %s", fname, e)
        logger.info("晚间VACUUM：备份清理完成，保留最新 %d 个，删除 %d 个（节省 %.2fMB）",
                    keep_count, len(to_delete), deleted_size / 1024 / 1024)

    logger.info("晚间VACUUM开始：北京时间 %s | DB=%s", _now_bj().strftime("%Y-%m-%d %H:%M:%S"), DB_PATH)
    before_bytes = duckdb_disk_bytes_total() or 0
    logger.info("压缩前大小：%.2fMB", before_bytes / 1024 / 1024)

    try:
        # 步骤1：打开维护锁
        write_master_control(maintenance_mode=True)
        logger.info("晚间VACUUM：维护锁已开启")

        # 步骤2：等待8秒让守护进程完成当前事务
        logger.info("晚间VACUUM：等待 8 秒...")
        time.sleep(8)

        # 步骤3：终止所有守护进程（释放DuckDB锁）
        try:
            import psutil

            current_pid = os.getpid()
            killed = []
            for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
                try:
                    cmdline = proc.info.get('cmdline') or []
                    if any('auto_sniper_daemon' in str(c) for c in cmdline):
                        if proc.info['pid'] != current_pid:
                            killed.append(proc.info['pid'])
                            logger.info("晚间VACUUM：终止进程 %s", proc.info['pid'])
                            proc.terminate()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            if killed:
                logger.info("晚间VACUUM：已终止 %d 个守护进程", len(killed))
            time.sleep(3)
        except ImportError:
            logger.warning("晚间VACUUM：psutil未安装，跳过进程终止")

        # 步骤4：EXPORT DATABASE → PARQUET
        logger.info("晚间VACUUM：EXPORT DATABASE → PARQUET...")
        os.makedirs(os.path.dirname(tmp_dir), exist_ok=True)
        os.makedirs(tmp_dir, exist_ok=True)

        con = duckdb.connect(DB_PATH, read_only=True)
        con.execute(f"EXPORT DATABASE '{tmp_dir}' (FORMAT PARQUET)")
        old_rows = 0
        try:
            old_rows = int(con.execute("SELECT COUNT(*) FROM daily_data").fetchone()[0] or 0)
        except Exception:
            pass
        con.close()
        logger.info("晚间VACUUM：EXPORT 完成，原始行数=%d", old_rows)

        # 步骤5：IMPORT DATABASE → 新库
        logger.info("晚间VACUUM：IMPORT DATABASE → 新库...")
        con2 = duckdb.connect(new_db)
        con2.execute(f"IMPORT DATABASE '{tmp_dir}'")
        con2.execute("CHECKPOINT")
        con2.execute("VACUUM")
        con2.execute("CHECKPOINT")
        new_rows = 0
        try:
            new_rows = int(con2.execute("SELECT COUNT(*) FROM daily_data").fetchone()[0] or 0)
        except Exception:
            pass
        con2.close()
        logger.info("晚间VACUUM：IMPORT 完成，新库行数=%d", new_rows)

        # 步骤6：校验行数
        if old_rows > 0 and new_rows != old_rows:
            raise RuntimeError(f"行数校验失败：原始={old_rows}，新库={new_rows}")
        logger.info("晚间VACUUM：行数校验通过（%d 行）", old_rows)

        # 步骤7：清理旧备份（仅清理本次新备份之前的，保持最新2个）
        _cleanup_old_backups(keep_count=2)

        # 步骤8：原子替换
        logger.info("晚间VACUUM：原子替换旧库...")
        os.replace(DB_PATH, bak_db)
        os.replace(new_db, DB_PATH)

        # 步骤9：清理临时目录
        shutil.rmtree(tmp_dir, ignore_errors=True)

        # 步骤10：记录压缩结果
        after_bytes = duckdb_disk_bytes_total() or 0
        saved_bytes = before_bytes - after_bytes
        logger.info(
            "晚间VACUUM：✅ 彻底压缩完成！%.2fMB → %.2fMB（节省 %.2fMB）",
            before_bytes / 1024 / 1024,
            after_bytes / 1024 / 1024,
            saved_bytes / 1024 / 1024,
        )
        logger.info("晚间VACUUM：新备份：%s", bak_db)

    except Exception as e:
        logger.exception("晚间VACUUM异常: %s", e)
        # 清理失败的临时文件
        try:
            if os.path.isdir(tmp_dir):
                shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass
        try:
            if os.path.isfile(new_db):
                os.remove(new_db)
        except Exception:
            pass
    finally:
        # 解除维护锁（守护进程会自动重启）
        try:
            write_master_control(maintenance_mode=False)
            logger.info("晚间VACUUM：维护锁已解除，守护进程约60秒后自动重启")
        except Exception:
            pass


def _job_evening_vacuum() -> None:
    """晚间链结束后执行进程内 CHECKPOINT+VACUUM，不再中断守护或替换数据库文件。"""

    def _safe_compact() -> None:
        today = _now_bj().strftime("%Y%m%d")
        st = _read_pipeline_state()
        if str(st.get("last_daily_vacuum_bj_date", "") or "") == today:
            logger.info("晚间安全压缩：今日已完成，跳过重复执行")
            return
        try:
            from data.db_core import duckdb_storage_snapshot_text, duckdb_vacuum_silent

            before = duckdb_storage_snapshot_text()
            duckdb_vacuum_silent()  # 去掉 log=logger（V26.88 shim 后 log 参数签名不一致，忽略细粒度日志）
            after = duckdb_storage_snapshot_text()
            _write_pipeline_state_patch(last_daily_vacuum_bj_date=today)
            logger.info("晚间安全压缩完成 | before=%s | after=%s", before, after)
        except Exception as e:
            logger.exception("晚间安全压缩失败: %s", e)
            _notify_daemon_alert(
                "晚间链：数据库安全压缩失败",
                f"日期 {today}：CHECKPOINT+VACUUM 未完成，不影响 P1/P5 已完成结果。异常：{str(e)[:600]}",
                category="data_sync",
                dedup_key=f"daemon_evening_safe_vacuum_fail_{today}",
            )

    _spawn("EVENING_VACUUM", _safe_compact, wait_for_lock_sec=1800.0)


def _run_morning_0850_orchestration() -> None:
    """
    08:50 早盘编排入口：
    - 先清空企微防刷缓存，避免旧日状态影响当天早盘消息；
    - 再执行早盘补位链（同步 / 条件性补 P1）。

    这是唯一的 08:50 调度入口，便于后续维护时一眼看清两步动作的顺序与职责。
    """
    try:
        daily_morning_routine()
    except Exception as e:
        logger.exception("daily_morning_routine 未捕获异常: %s", e)
    try:
        _morning_trading_catchup()
    except Exception as e:
        logger.exception("_morning_trading_catchup 未捕获异常: %s", e)


def _run_intraday_snapshot_body(slot_id: str, label: str) -> None:
    """
    持 _SCAN_BUSY 时在子线程内执行；与扫描任务串行，避免 DuckDB 并发踩踏。
    说明：此为「分时多时点 VR/量能锚点」快照（slot_id 如 935…1440）。
    """
    if _maintenance_mode_skip(f"分时快照 capture_intraday_snapshots {label}"):
        return
    if _daemon_cruise_off_skip(f"分时快照{label}"):
        return
    if not _barrier_trading_day_or_skip(f"分时快照{label}"):
        return
    if _now_bj().weekday() >= 5:
        logger.info("分时快照跳过(周末): %s", label)
        return
    try:
        msg = capture_intraday_snapshots(slot_id=slot_id, force=True)
        logger.info("【分时快照】%s slot_id=%s — %s", label, slot_id, msg)
        if slot_id == "935":
            try:
                from core.p5_morning_validation import early_morning_p5_validation

                early_morning_p5_validation(log=logger)
            except Exception as e:
                logger.exception("P5 次日早盘验证异常: %s", e)
    except Exception as e:
        logger.exception("【分时快照】%s slot_id=%s 异常: %s", label, slot_id, e)


def _job_intraday_snapshot(slot_id: str, label: str) -> None:
    """分时快照：与 P3/P4/P5 同锁序列化；等待至多 30min 以免被长尾扫描误跳过。"""
    _spawn(
        f"SNAP_{slot_id}",
        lambda: _run_intraday_snapshot_body(slot_id, label),
        wait_for_lock_sec=1800.0,
    )


def _job_daily_morning_routine() -> None:
    """08:50：立即返回；`_run_morning_0850_orchestration` 在独立线程执行，保证 `schedule.run_pending` 毫秒级。"""

    def _runner() -> None:
        try:
            _run_morning_0850_orchestration()
        except Exception as e:
            logger.exception("08:50 早盘编排未捕获异常: %s", e)

    try:
        threading.Thread(target=_runner, name="daemon-morning-routine", daemon=True).start()
    except Exception as e:
        logger.exception("早安例行线程派生失败: %s", e)


def _weekly_alive_heartbeat() -> None:
    """
    每周一次「系统运行正常」心跳：
    - 仅交易日发送（防止周末/节假日误触）；
    - 通过 pipeline_state 持久化去重，跨重启仍保证每 ISO 周仅 1 条。
    """
    if _daemon_cruise_off_skip("每周运行心跳"):
        return
    if not _daemon_is_trading_day_safe():
        logger.info("每周心跳：非交易日，跳过")
        return

    now = _now_bj()
    week_key = f"{now.isocalendar().year}-W{now.isocalendar().week:02d}"
    st = _read_pipeline_state()
    if str(st.get("last_weekly_alive_week", "")).strip() == week_key:
        logger.info("每周心跳：本周已发送 (%s)，跳过", week_key)
        return

    last_sync = str(st.get("last_sync_ok_bj_date", "") or "--")
    last_p1 = str(st.get("last_p1_ok_bj_date", "") or "--")
    detail = (
        f"时间：{now.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"周标识：{week_key}\n"
        f"状态：守护进程运行正常\n"
        f"最近同步成功日：{last_sync}\n"
        f"最近P1成功日：{last_p1}"
    )
    _notify_daemon_alert(
        title="【每周心跳】系统运行正常",
        detail=detail,
        category="daemon",
        dedup_key=f"daemon_weekly_alive_{week_key}",
    )
    _write_pipeline_state_patch(last_weekly_alive_week=week_key, last_weekly_alive_at=now.isoformat())
    logger.info("每周心跳：已发送 (%s)", week_key)


def _job_weekly_alive_heartbeat() -> None:
    _spawn("WEEKLY_HEARTBEAT", _weekly_alive_heartbeat, wait_for_lock_sec=5.0)


def _weekly_db_maintenance_vacuum() -> None:
    """
    【V26.88 已取消】原用于每周 CHECKPOINT+VACUUM。
    日常链已改为 20:30 进程内安全压缩；离线 EXPORT/IMPORT 仅保留供手工紧急维护，故注销每周重复任务。
    保留本函数与 tools/weekly_db_maintenance_orchestrated.py，供手动紧急维护使用：
        python tools/weekly_db_maintenance_orchestrated.py --no-pause --force
    说明：避免进程内直接 VACUUM 与 UI 只读连接在 Windows 上争用同一 .duckdb 文件。

    - ISO 周去重、交易时段跳过仍在本函数判定；真正维护与企微通知在子进程内完成。
    """
    if _maintenance_mode_skip("每周数据库维护 CHECKPOINT+VACUUM"):
        return
    if _daemon_cruise_off_skip("每周数据库维护"):
        return

    now = _now_bj()
    week_key = f"{now.isocalendar().year}-W{now.isocalendar().week:02d}"
    st = _read_pipeline_state()
    if str(st.get("last_weekly_vacuum_week", "")).strip() == week_key:
        logger.info("每周数据库维护：本周已执行 (%s)，跳过", week_key)
        return

    curr_min = now.hour * 60 + now.minute
    # 交易窗口保护（09:00~15:30），即便调度误触发也不在盘中维护。
    if (9 * 60) <= curr_min <= (15 * 60 + 30):
        logger.info("每周数据库维护：当前处于交易时段，跳过")
        return

    script = os.path.join(_PROJECT_ROOT, "tools", "weekly_db_maintenance_orchestrated.py")
    if not os.path.isfile(script):
        logger.error("每周数据库维护：未找到编排脚本 %s", script)
        return

    try:
        popen_kw: Dict[str, Any] = {
            "cwd": _PROJECT_ROOT,
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        if sys.platform == "win32":
            # 独立进程组，避免维护子进程随守护退出而被误杀
            popen_kw["creationflags"] = int(subprocess.DETACHED_PROCESS) | int(
                subprocess.CREATE_NEW_PROCESS_GROUP
            )
        else:
            popen_kw["start_new_session"] = True
        subprocess.Popen(
            [sys.executable, script, "--no-pause"],
            **popen_kw,
        )
        logger.info(
            "每周数据库维护：已投递独立编排进程（方案二：释放守护/UI 后 VACUUM；守护重启依赖 7x24 外壳看门狗）| week=%s | 详见 data/runtime/weekly_maintenance.log",
            week_key,
        )
    except Exception as e:
        logger.exception("每周数据库维护：投递编排进程失败: %s", e)
        _notify_daemon_alert(
            title="【每周数据库维护】投递失败",
            detail=(
                f"时间：{now.strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"周标识：{week_key}\n"
                f"说明：无法启动 weekly_db_maintenance_orchestrated.py\n"
                f"异常：{str(e)[:900]}"
            ),
            category="data_sync",
            dedup_key=f"daemon_weekly_vacuum_spawn_fail_{week_key}",
        )


def _job_weekly_db_maintenance_vacuum() -> None:
    _spawn("WEEKLY_DB_MAINT", _weekly_db_maintenance_vacuum, wait_for_lock_sec=60.0)


def _init_async_scan_queue_consumer() -> None:
    """
    独占 pending_queue_consumer.filelock，由本进程消费 data/runtime/scan_async/pending.json。
    替代已删除的 service/scheduler.py 中的队列循环；与 Streamlit 内嵌 ScanAsyncWorker 仍互斥（二选一）。
    """
    global _async_scan_queue_lock_ok
    try:
        from service.async_scan_bridge import try_acquire_scheduler_queue_consumer_filelock

        _async_scan_queue_lock_ok = bool(try_acquire_scheduler_queue_consumer_filelock())
        if _async_scan_queue_lock_ok:
            logger.info("scan_async: 本进程已占用 pending 队列消费锁")
        else:
            logger.warning(
                "scan_async: 未获得 pending 队列锁（他进程/UI 已占用），本进程不消费异步扫描队列"
            )
    except Exception as e:
        logger.warning("scan_async 队列锁初始化失败: %s", e)
        _async_scan_queue_lock_ok = False


def _tick_async_scan_queue() -> None:
    """
    主循环轮询异步 pending 队列（约每 _ASYNC_QUEUE_POLL_TICKS 秒一次）。
    纯 Headless 7x24 守护：无 pending 时 ``process_one_pending_scan_job`` 静默 return，
    不在此处打 info，避免与 UI 已剥离后仍产生海量空轮询日志。
    """
    if _maintenance_mode_skip("异步扫描队列 process_one_pending_scan_job"):
        return
    if not _async_scan_queue_lock_ok:
        return
    try:
        from service.async_scan_bridge import process_one_pending_scan_job

        process_one_pending_scan_job()
    except Exception as e:
        logger.debug("async_scan 队列处理: %s", e)


def _register_bj_daily(
    hhmm: str,
    fn: Callable[[], None],
    label: str,
    *,
    catchup_minutes: int = 0,
) -> None:
    """注册北京时间日任务；允许在指定窗口内补跑，避免维护锁或系统挂起吞掉整点任务。"""
    t = str(hhmm or "").strip()
    if len(t) != 5 or t[2] != ":":
        raise ValueError(f"非法 HH:MM: {hhmm!r}")
    try:
        h = int(t[:2])
        m = int(t[3:5])
    except Exception as e:
        raise ValueError(f"非法 HH:MM: {hhmm!r}") from e
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError(f"非法 HH:MM: {hhmm!r}")
    with _BJ_SCHEDULE_LOCK:
        _BJ_DAILY_JOBS.append((t, fn, label, max(0, int(catchup_minutes))))


_BJ_STATE_KEY_BY_LABEL = {
    "晚间增量同步": "last_sync_ok_bj_date",
    "晚间P1": "last_p1_ok_bj_date",
    "晚间P5": "last_p5_ok_bj_date",
    "晚间安全压缩": "last_daily_vacuum_bj_date",
    # 【ST-P0-14 修复】信号T+N回填 原先不在此表 → 走进程内分钟去重，
    # catchup_minutes=120 窗口内每分钟重跑一次（实测 19:12→20:30 共 79 次），
    # 既空转消耗 DuckDB 写锁，又把 EVENING_HITHINK 挤掉（4 次抢锁失败）。
    "信号T+N回填": "last_signal_backfill_bj_date",
    # 【ST-P0-22 修复】晚间HiThink特色同步 原先也不在此表 → catchup 60 分钟窗口内
    # 每分钟重派一次（实测 19:46→20:46 共 48 次，反复请求 HiThink 接口）。
    "晚间HiThink特色同步": "last_hithink_special_ok_bj_date",
}

# 【2026-11-13 三态降级配套】降级也算「当日已完成」的替代完成键。
# 为什么必须加：数据降级时我们**故意不写** last_<pool>_ok_bj_date，避免残缺结果
# 冒充正常成品；但调度器靠 _bj_daily_job_completed 判断是否已派发过，若它不认
# degraded 键，就会认为任务没做成 → 在 catchup 窗口内每分钟重新派发一次。
# 那正是此前修过三次的「79 次 / 48 次重复推送」事故（ST-P0-14 / ST-P0-22 / ST-P0-31）。
# 语义区分：ok = 数据完整；degraded = 已执行但数据不全。两者都表示「今天这事做过了」。
_BJ_STATE_ALT_KEYS = {
    "晚间P1": ("last_p1_degraded_bj_date",),
    "晚间P5": ("last_p5_degraded_bj_date",),
}

# 【ST-P0-31 修复 2026-11-05】通用「当日已完成」账本。
# 背景：_bj_daily_job_completed 原先只有上面这张**硬编码白名单**能判定完成，
#      凡是不在名单里的任务一律 return False → 调度器认为「没做成」，
#      在 catchup 窗口内**每分钟重新派发一次**，成功的推送就这样被反复重发。
#      同一类事故已修过三次（79 次 / 48 次 / 本次），说明白名单模式本身是坑：
#      新增任务只要忘了登记，就会静默重复推送，而且没人会立刻发现。
#
# 实测影响（本次）：
#   · 09:05 战法榜早盘推送（catchup=60）→ 每天重推最多 61 条完全相同的战法榜
#   · 18:45 战法效果榜推送（catchup=120）→ 最多 121 次
#   · 15:05 模拟盘收盘结算（catchup=90）→ 实测 2026-11-04 重跑 90 次
#
# 修法：白名单改为「pipeline 状态位优先，查不到则用本账本兜底」。
#      任务成功返回后由调度器统一落盘，白名单从此只用于晚间链的既有语义，
#      新增任务**无需登记**也不会重复派发。
# 【ST-P0-31】账本路径必须与 cwd 无关：原先用 os.getcwd()，一旦守护进程被
# 从别的工作目录拉起（计划任务 / 快捷方式），账本会写到另一个位置，
# 判定永远「未完成」→ 又回到每分钟重发。改为锚定项目内的 runtime/state。
try:
    from core.runtime_data_paths import STATE_DIR as _XJ_STATE_DIR

    _BJ_DAILY_DONE_LEDGER = os.path.join(_XJ_STATE_DIR, "bj_daily_jobs_done.json")
except Exception:
    _BJ_DAILY_DONE_LEDGER = os.path.join(
        os.getcwd(), "data", "runtime", "state", "bj_daily_jobs_done.json"
    )


def _bj_daily_ledger_read() -> Dict[str, Any]:
    try:
        with open(_BJ_DAILY_DONE_LEDGER, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _bj_daily_ledger_has(label: str, today: str) -> bool:
    return bool(_bj_daily_ledger_read().get(f"{today}:{label}"))


def _mark_bj_daily_job_completed(label: str, today: str) -> None:
    """把「今天这个任务已成功跑过」落盘，供下个 tick 判定，避免 catchup 重复派发。

    对已有 pipeline 状态位的任务直接跳过 —— 那些任务以状态位为准，
    两套口径同时写会打架（例如晚间 P1 重试成功前不应被账本抢先标记完成）。
    """
    if label in _BJ_STATE_KEY_BY_LABEL:
        return
    try:
        from core.file_utils import atomic_json_update

        def _upd(data: Dict[str, Any]) -> None:
            data[f"{today}:{label}"] = {
                "at": _now_bj().strftime("%Y-%m-%d %H:%M:%S"),
            }
            # 只保留最近 7 天，避免账本无限增长
            try:
                from datetime import timedelta

                cutoff = (_now_bj() - timedelta(days=7)).strftime("%Y%m%d")
                for k in list(data.keys()):
                    if isinstance(k, str) and len(k) >= 8 and k[:8] < cutoff:
                        del data[k]
            except Exception:
                pass

        atomic_json_update(_BJ_DAILY_DONE_LEDGER, _upd, timeout=5)
    except Exception as e:
        logger.warning("BJ定时任务完成标记写入失败 [%s]: %s", label, e)


def _bj_daily_job_completed(label: str, today: str) -> bool:
    """判定当日任务是否已完成：pipeline 状态位优先，其余走通用账本。

    返回 False 才会被调度器派发，所以这里**绝不能**对未知 label 无脑返回 False。
    """
    state_key = _BJ_STATE_KEY_BY_LABEL.get(label)
    if state_key:
        st = _read_pipeline_state()
        if str(st.get(state_key, "") or "") == today:
            return True
        # 降级口径同样算「今天已完成」，否则 catchup 窗口内会每分钟重派一次。
        for alt_key in _BJ_STATE_ALT_KEYS.get(label, ()):
            if str(st.get(alt_key, "") or "") == today:
                return True
        return False
    return _bj_daily_ledger_has(label, today)


def _tick_bj_daily_scheduler() -> None:
    """每秒轮询；精确触发或窗口补跑，关键晚间任务直到持久状态完成才停止派发。"""
    global _BJ_LAST_TICK_MINUTE_KEY
    now = _now_bj()
    minute_key = now.strftime("%Y%m%d%H%M")
    if minute_key == _BJ_LAST_TICK_MINUTE_KEY:
        return
    _BJ_LAST_TICK_MINUTE_KEY = minute_key
    now_hhmm = now.strftime("%H:%M")
    today = now.strftime("%Y%m%d")
    current_minute = now.hour * 60 + now.minute
    with _BJ_SCHEDULE_LOCK:
        registered = list(_BJ_DAILY_JOBS)
    for hhmm, fn, label, catchup_minutes in registered:
        scheduled_minute = int(hhmm[:2]) * 60 + int(hhmm[3:5])
        delay_minutes = current_minute - scheduled_minute
        if delay_minutes < 0 or delay_minutes > catchup_minutes:
            continue
        if _bj_daily_job_completed(label, today):
            continue
        dispatch_key = f"{minute_key}:{hhmm}:{label}"
        with _BJ_SCHEDULE_LOCK:
            if dispatch_key in _BJ_DISPATCHED_JOB_KEYS:
                continue
            _BJ_DISPATCHED_JOB_KEYS.add(dispatch_key)
            if len(_BJ_DISPATCHED_JOB_KEYS) > 128:
                _BJ_DISPATCHED_JOB_KEYS.intersection_update(
                    key for key in _BJ_DISPATCHED_JOB_KEYS if key.startswith(f"{today}")
                )
        if delay_minutes > 0:
            logger.warning(
                "BJ定时任务补跑 [%s] | 计划=%s | 当前=%s | 延迟=%s分钟",
                label,
                hhmm,
                now_hhmm,
                delay_minutes,
            )
        try:
            _ret = fn()
            # 【ST-P0-31】成功返回（未抛异常、且未显式返回 False）即标记当日完成，
            # 否则 catchup 窗口内每分钟都会重新派发 → 推送类任务会重复发同一内容。
            # 返回 False 表示「业务上没做成」，保留 catchup 重试机会。
            if _ret is not False:
                _mark_bj_daily_job_completed(label, today)
        except Exception as e:
            logger.exception("BJ定时任务触发异常 [%s @ %s]: %s", label, now_hhmm, e)


def _job_wal_checkpoint() -> None:
    """
    每 30 分钟 WAL checkpoint：强制将 WAL 落盘合并，防止 7×24 长期运行后 .wal 膨胀。
    在 maintenance_mode=true 时主动跳过（避免与 VACUUM 争写锁）。
    独立线程执行，不阻塞主循环。
    """
    def _runner() -> None:
        _t0 = time.perf_counter()
        _status = "ok"
        _extra = {"trigger": "30min_scheduler"}
        try:
            from core.master_control import is_maintenance_mode_enabled
            if is_maintenance_mode_enabled():
                _extra["skipped_reason"] = "maintenance_mode"
                logger.debug("[WAL] 维护锁开启，跳过本轮 checkpoint")
                return
        except Exception:
            pass
        try:
            from data.db_core import duckdb_checkpoint
            duckdb_checkpoint(force=True)
            logger.debug("[WAL] 30min checkpoint 完成")
        except Exception as e:
            _status = "failed"
            _extra["error"] = f"{type(e).__name__}: {str(e)[:240]}"
            logger.warning("[WAL] checkpoint 失败: %s", e)
        finally:
            try:
                from data.sync_health import append_sync_health
                append_sync_health({
                    "kind": "wal_checkpoint",
                    "status": _status,
                    "duration_sec": round(time.perf_counter() - _t0, 3),
                    **_extra,
                })
            except Exception:
                pass

    threading.Thread(target=_runner, name="daemon-wal-checkpoint", daemon=True).start()


def _job_daily_db_inspect() -> None:
    """
    每日 08:00 跑一次 V26.88 数据库全量体检，落到 data/runtime/db_inspect_latest.{json,md}。

    设计原因：
    - duckdb 是单进程排他锁，外部 inspector.py 无法与 daemon 共存于同一文件；
    - 在 daemon 进程内运行 `tools.db_inspector.run_full_inspection()` 可以复用读连接；
    - 产物供早盘 08:50 编排器 / UI 体检按钮 / 手动排查读取。

    独立线程执行，不阻塞主循环 schedule。
    """
    def _runner() -> None:
        _t0 = time.perf_counter()
        _status = "ok"
        _extra: Dict[str, Any] = {"trigger": "bj_08:00"}
        try:
            from tools.db_inspector import run_full_inspection
            result = run_full_inspection(sample_rows=500, wait_sec=0.0)
            # 复用 inspector 内部已写的 json/md；这里仅汇总给 sync_health 与日志
            crits = result.get("critical_items") or []
            fresh = result.get("runtime_freshness") or {}
            _extra.update({
                "ok": bool(result.get("ok")),
                "critical_count": len(crits),
                "critical_items": crits,
                "freshness_severity": fresh.get("severity"),
                "view_compat_severity": (result.get("view_compat") or {}).get("severity"),
                "coverage_ok": bool((result.get("coverage") or {}).get("ok")),
            })
            if not result.get("ok"):
                _status = "crit"
                logger.warning("[DB_INSPECT] 体检未通过: critical=%s | freshness=%s",
                               crits, fresh.get("severity"))
            else:
                logger.info("[DB_INSPECT] 体检通过 | freshness=%s | view=%s",
                            fresh.get("severity"),
                            (result.get("view_compat") or {}).get("severity"))
        except Exception as e:
            _status = "failed"
            _extra["error"] = f"{type(e).__name__}: {str(e)[:240]}"
            logger.warning("[DB_INSPECT] 体检失败: %s", e)
        finally:
            _extra["duration_sec"] = round(time.perf_counter() - _t0, 3)
            try:
                from data.sync_health import append_sync_health
                append_sync_health({
                    "kind": "db_inspect",
                    "status": _status,
                    **_extra,
                })
            except Exception:
                pass

    threading.Thread(target=_runner, name="daemon-db-inspect", daemon=True).start()


def _job_daemon_heartbeat() -> None:
    """
    V29：细粒度心跳（每 5 分钟一行 JSONL）。
    只观测，不改动任何业务行为；写失败也不影响扫描。
    """
    try:
        from data.daemon_observability import write_heartbeat

        _mem_mb = None
        try:
            import psutil

            _mem_mb = round(psutil.Process(os.getpid()).memory_info().rss / (1024 * 1024), 1)
        except Exception:
            _mem_mb = None

        write_heartbeat(
            {
                "active_spawns": sorted(_ACTIVE_SPAWN_NAMES) if _ACTIVE_SPAWN_NAMES else [],
                "scan_busy_locked": bool(_SCAN_BUSY.locked()),
                "memory_mb": _mem_mb,
            }
        )
    except Exception as e:
        logger.debug("[obs] heartbeat 写入失败（忽略）: %s", e)


def _job_daemon_health_check() -> None:
    """V29：只在心跳/任务异常时才告警；正常时完全静默。"""
    try:
        from data.daemon_observability import notify_if_unhealthy

        notify_if_unhealthy(_notify_daemon_alert)
    except Exception as e:
        logger.debug("[obs] health_check 异常（忽略）: %s", e)


# ==================== 死亡告警（dead-man switch）====================
# 【P0 修复 2026-11-13】「进程死了没人知道」是这次停摆 3 天的根因之一。
# 两个视角互补，缺一不可：
#   · 进程内视角（本函数）：启动时发现「上次心跳是很久以前」→ 主动说明中断了多久。
#     优点是不需要外部依赖；缺点：进程根本没起来时它也说不了话。
#   · 进程外视角（tools/daemon_watchdog.py + 计划任务）：每 10 分钟查心跳，
#     死了就企微告警 + 自动拉起。这是真正的兜底。
def _last_heartbeat_epoch() -> Optional[float]:
    """读最近一次心跳的 epoch（只读 JSONL 末行，不碰数据库）。"""
    try:
        from data.daemon_observability import HEARTBEAT_PATH

        if not os.path.isfile(HEARTBEAT_PATH):
            return None
        size = os.path.getsize(HEARTBEAT_PATH)
        with open(HEARTBEAT_PATH, "r", encoding="utf-8", errors="ignore") as f:
            if size > 65536:
                f.seek(size - 65536)
                f.readline()
            lines = f.readlines()
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            ep = rec.get("epoch")
            if ep:
                return float(ep)
        return None
    except Exception as e:  # noqa: BLE001
        logger.debug("[deadman] 读取心跳失败（忽略）: %s", e)
        return None


def _check_interrupted_since_last_run(*, gap_alert_min: int = 120) -> None:
    """启动时自查：上次心跳距今多久？中断足够久就主动告警（不再静默恢复）。

    阈值 120 分钟：心跳是 5 分钟一拍，120 分钟 = 24 拍缺失，
    短于此的多是「每日主动退出 → 看门狗 30s 后拉起」的正常重启，不该告警。
    """
    try:
        ep = _last_heartbeat_epoch()
        if ep is None:
            logger.info("[deadman] 无历史心跳（首次部署），跳过中断自查")
            return
        gap_min = max(0.0, (time.time() - ep) / 60.0)
        if gap_min < float(gap_alert_min):
            logger.info("[deadman] 距上次心跳 %.1f 分钟（<%.0f），按正常重启处理", gap_min, gap_alert_min)
            return
        last_bj = datetime.fromtimestamp(ep, BJ_TZ).strftime("%Y-%m-%d %H:%M:%S")
        gap_h = gap_min / 60.0
        logger.warning(
            "[deadman] 检测到守护进程中断：上次心跳 %s，距今 %.1f 小时 | pid=%s",
            last_bj, gap_h, os.getpid(),
        )
        _notify_daemon_alert(
            "【守护进程】检测到中断后已恢复",
            f"现在：{_now_bj().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"上次心跳：{last_bj}\n"
            f"中断时长：约 {gap_h:.1f} 小时（{gap_min:.0f} 分钟）\n"
            f"含义：这段时间内没扫描、没推送、模拟盘没监控持仓（止损未执行）。\n"
            f"处置：已在本次启动后恢复调度；请检查 sniper.log 与 daemon_watchdog.log 找中断原因。",
            category="daemon",
            dedup_key=f"deadman_recovered_{_now_bj().strftime('%Y%m%d%H')}",
        )
    except Exception as e:  # noqa: BLE001
        logger.debug("[deadman] 中断自查失败（忽略）: %s", e)


def _job_daemon_daily_summary() -> None:
    """V29：每日 07:00 把过去 24h 心跳聚合成摘要推送（独立于普通信号流）。"""
    if _maintenance_mode_skip("每日系统体检摘要"):
        return
    _spawn("DAEMON_DAILY_SUMMARY", lambda: _daemon_daily_summary_body(),
           wait_for_lock_sec=5.0)


def _daemon_daily_summary_body() -> None:
    try:
        from data.daemon_observability import push_daily_summary

        push_daily_summary(_notify_daemon_alert)
    except Exception as e:
        logger.debug("[obs] 每日摘要异常（忽略）: %s", e)


def _signal_backfill_pipeline() -> None:
    """
    V29：T+1/T+2/T+5/T+10 收益回填 + 原子战法有效性聚合 + 战法权重推荐报表。

    【V29.1 修复】旧版只做前两步，优化器实际从未被调度（全仓无调用点），
    于是「回填已接通、权重链路无输出」。现把 optimize_strategy_weights 作为
    第三步接进来，且强制 auto_apply=False：只落 statistics_only 推荐报表，
    绝不改实盘权重。

    读写 DuckDB 走统一入口；全程只做统计，不改任何评分权重或策略逻辑。
    """
    try:
        from data.db_core import get_write_conn
        from data.signal_backtest import run_daily_backfill
        from core.strategy_weight_optimizer import optimize_strategy_weights
        from core.strategy_effectiveness_snapshot import build_ui_snapshot

        with get_write_conn() as con:
            result = run_daily_backfill(con)
            # auto_apply=False：仅生成 data/strategy_weight_recommendation.json
            opt = optimize_strategy_weights(con, auto_apply=False)
            maturity = opt.get("maturity") or {}
            result["opt_mode"] = opt.get("mode")
            result["opt_strategies"] = len(opt.get("weight_recommendation") or {})
            result["opt_available_n_days"] = maturity.get("available_n_days")
            result["opt_missing_n_days"] = maturity.get("missing_n_days")
            result["opt_next_apply_remaining"] = maturity.get("next_auto_apply_remaining")
            # V29.2：生成 UI 只读快照（面板专用，只读查询 + 写 JSON，不碰业务表）。
            # 独立 try/except：快照失败只告警，绝不让回填事务整体判失败。
            try:
                snap_path = build_ui_snapshot(con) if con is not None else build_ui_snapshot()
                result["ui_snapshot"] = snap_path
                logger.info("[signal-backfill] UI 快照已生成: %s", snap_path)
            except Exception as snap_err:
                logger.warning("[signal-backfill] UI 快照生成失败（不影响回填结果）: %s", snap_err)
                _notify_daemon_alert(
                    "【战法面板】UI 快照生成失败",
                    f"时间：{_now_bj().strftime('%Y-%m-%d %H:%M:%S')}\n"
                    f"影响：Streamlit 战法胜率面板将回退显示上一版快照\n"
                    f"异常：{str(snap_err)[:400]}",
                    category="data_sync",
                    dedup_key="daemon_ui_snapshot_err",
                )
            # 【V30.12 诚实体检】按「真实止损止盈规则」重算战法期望值并推送。
            # 为什么不能只看 signal_backtest 的 T+N 榜：那个榜是「死拿到 T+10」的口径，
            # 与模拟盘/实盘真正执行的 +8%/-3% 规则不是一回事，用它评价实盘会系统性失真。
            # 这里用 core/stop_rule_backtest 按真实规则重算，给出期望值/盈利因子/止盈止损率。
            # 纯统计：不下单、不改参数、不改权重。
            try:
                from core.paper_trading.honest_report import (
                    build_honest_report,
                    format_honest_report,
                )
                from core.go_live_gate import (
                    evaluate as _gate_eval,
                    format_verdict as _gate_fmt,
                    load_verdict as _gate_load,
                    save_verdict as _gate_save,
                )

                # 【P0 修复 2026-11-13】先算放行门禁，再把结论嵌进体检报告：
                # 「-2.01%/笔 = 不许上实盘」这句话必须每天由机器说一遍，
                # 而不是躺在某份文档里等人想起来。
                _gate = _gate_eval(con)
                _gate_prev = _gate_load()
                _gate_save(_gate)

                _rep = build_honest_report(con, gate=_gate)
                if _rep.get("available"):
                    _sm = _rep.get("summary") or {}
                    result["honest_summary"] = {
                        "n_trades": int(_sm.get("n_trades") or 0),
                        "win_rate": float(_sm.get("win_rate") or 0.0),
                        "avg_pnl_pct": float(_sm.get("avg_pnl_pct") or 0.0),
                        "profit_factor": float(_sm.get("profit_factor") or 0.0),
                    }
                    _psm = (_rep.get("pessimistic") or {}).get("summary") or {}
                    result["honest_pessimistic"] = {
                        "avg_pnl_pct": float(_psm.get("avg_pnl_pct") or 0.0),
                        "profit_factor": float(_psm.get("profit_factor") or 0.0),
                    }
                    result["go_live_gate"] = {
                        "allowed": bool(_gate.get("allowed")),
                        "reasons": list(_gate.get("reasons") or []),
                        "red_cards": len(_gate.get("red_cards") or []),
                    }
                    logger.info(
                        "[诚实报告] 成交 %d 笔 | 胜率 %.1f%% | 期望 %+.2f%%/笔（悲观 %+.2f%%）| "
                        "盈利因子 %s | 放行=%s",
                        int(_sm.get("n_trades") or 0), float(_sm.get("win_rate") or 0.0),
                        float(_sm.get("avg_pnl_pct") or 0.0), float(_psm.get("avg_pnl_pct") or 0.0),
                        ("%.2f" % _sm["profit_factor"]) if _sm.get("profit_factor") != float("inf") else "∞",
                        "是" if _gate.get("allowed") else "否",
                    )
                    _honest_body = format_honest_report(_rep)
                    try:
                        from core.notification.gateway import get_wechat_gateway
                        from core.config_manager import get_notification_config

                        _ncfg = get_notification_config()
                        _url = str(_ncfg.get("wechat_webhook_url") or "").strip()
                        if _url and _ncfg.get("enabled"):
                            get_wechat_gateway(_url).push_markdown_async(_honest_body)
                            logger.info("[诚实报告] 已推送企微")
                            # 门禁「结论翻转」单独再推一条：放行/收回是必须立刻知道的事
                            _flipped = bool(_gate_prev) and (
                                bool(_gate_prev.get("allowed")) != bool(_gate.get("allowed"))
                            )
                            if _flipped:
                                get_wechat_gateway(_url).push_markdown_async(
                                    _gate_fmt(_gate, previous=_gate_prev)
                                )
                                logger.warning(
                                    "[放行门禁] 结论翻转：%s → %s",
                                    _gate_prev.get("allowed"), _gate.get("allowed"),
                                )
                    except Exception as push_err:
                        logger.warning("[诚实报告] 推送失败（不影响回填）: %s", push_err)
                else:
                    logger.warning("[诚实报告] 不可用: %s", _rep.get("error"))
            except Exception as honest_err:
                logger.warning("[诚实报告] 计算失败（不影响回填）: %s", honest_err)
            # 【V30.10 2026-10-21】信号账本对账：用 push_history.json 回填 signal_log.pushed。
            # 为什么每晚都要做：
            #   · push_history.json 是本地文件，会被进程重启 / 手工清空 / 写入失败影响
            #   · signal_log.pushed 是【唯一】能区分「候选」与「真推送」的字段
            #   · 两者任何一侧丢失都会让战法有效性归因失真
            # 口径：(交易日, 代码) —— 同票同日被推过即算已推送（跨池冷却会把记录
            #       只留在最先推送的池上，所以池维度不可靠）。
            try:
                from core.signal_ledger_bridge import reconcile_push_history

                _rep = reconcile_push_history(con, apply=True)
                result["pushed_reconcile"] = {
                    "matched": _rep.get("matched"),
                    "matched_cross_pool": _rep.get("matched_cross_pool"),
                    "unmatched": _rep.get("unmatched_history"),
                    "updated_rows": _rep.get("updated_rows"),
                }
                logger.info(
                    "[signal-backfill] 账本对账: 严格匹配 %s / 跨池兜底 %s / 未匹配 %s / 回填 %s 行",
                    _rep.get("matched"), _rep.get("matched_cross_pool"),
                    _rep.get("unmatched_history"), _rep.get("updated_rows"),
                )
            except Exception as rec_err:
                logger.warning("[signal-backfill] 账本对账失败（不影响回填结果）: %s", rec_err)

            # 【V30.10 2026-10-21】推送分数阈值自适应标定（只出建议，不改生产阈值）。
            # 与 T+N 回填同一事务窗口：阈值推荐必须建立在【刚回填完】的收益数据上，
            # 否则用的是隔夜旧数据。绝不自动改 config.yaml（dry_run 语义）。
            try:
                from core.push_threshold_calibrator import (
                    calc_recommended_thresholds,
                    save_recommendation,
                )

                calib = calc_recommended_thresholds(days=120, min_samples=8, db_conn=con) if con is not None else calc_recommended_thresholds(days=120, min_samples=8)
                save_recommendation(calib)
                result["push_threshold_recommendations"] = {
                    p: (n or {}).get("threshold")
                    for p, n in (calib.get("recommendations") or {}).items()
                }
                logger.info(
                    "[signal-backfill] 推送阈值标定建议: %s",
                    result["push_threshold_recommendations"],
                )
            except Exception as calib_err:
                logger.warning("[signal-backfill] 推送阈值标定失败（不影响回填）: %s", calib_err)
        logger.info(
            "[signal-backfill] 完成: pnl=%s 条 / 战法聚合=%s 条 / 权重推荐=%s 条"
            "（成熟档位=%s，缺失档位=%s，距自动应用还差 %s 条，mode=%s）",
            result.get("pnl_count"),
            result.get("eff_count"),
            result.get("opt_strategies"),
            result.get("opt_available_n_days"),
            result.get("opt_missing_n_days"),
            result.get("opt_next_apply_remaining"),
            result.get("opt_mode"),
        )
        _write_pipeline_state_patch(
            last_signal_backfill_bj=_now_bj().isoformat(),
            # 【ST-P0-14 修复】必须写 8 位 YYYYMMDD，与 _bj_daily_job_completed 比较的
            # today 格式一致（此前只写 ISO 串 → 判定永远不相等 → 每分钟重复跑）。
            last_signal_backfill_bj_date=_now_bj().strftime("%Y%m%d"),
            last_signal_backfill_result=str(result)[:200],
        )
    except Exception as e:
        logger.exception("信号 T+N 回填异常: %s", e)
        _notify_daemon_alert(
            "【信号回填】T+N 收益回填异常",
            f"时间：{_now_bj().strftime('%Y-%m-%d %H:%M:%S')}\n异常：{str(e)[:600]}",
            category="data_sync",
            dedup_key=f"daemon_signal_backfill_err_{_now_bj().strftime('%Y%m%d')}",
        )


def _job_signal_backfill() -> None:
    """18:30 触发；走 _spawn 以复用全局串行闸门 _SCAN_BUSY，避免与同步/压缩争用 DuckDB。

    V29.1：本次链路已包含「战法权重推荐报表（仅统计）」，与 18:30 的
    T+N 回填同一事务窗口完成，无需新增独立调度点。
    """
    if _maintenance_mode_skip("信号T+N回填"):
        return
    _spawn("SIGNAL_BACKFILL", _signal_backfill_pipeline, wait_for_lock_sec=600.0)


# ---- 战法效果榜推送（盘后 / 早盘）----
# 目的：用户的实验目标是「1-2 个月后选出最好的战法」。
# 信号层面的后验（T+N 收益）比模拟盘样本多得多（每天数十条 vs 4 笔），
# 因此战法优选以 signal_pnl / strategy_effectiveness 为准，模拟盘为辅。
def _job_pool_leaderboard_push() -> None:
    """18:45 推送「盘后战法榜」（P5）+「盘中战法榜」（P3/P4）。

    时点选在 18:30 的 T+N 回填之后 —— 回填完成才有最新胜率。

    返回 False 只在**真异常**时（供调度器保留 catchup 重试）；
    业务性跳过（webhook 关闭 / 无数据）视为已完成，否则会在
    catchup 窗口内每分钟重发一次。
    """
    if _maintenance_mode_skip("战法效果榜推送"):
        return
    try:
        from core.paper_trading.postmarket_attribution import notify_pool_leaderboard

        for pool in ("p5", "p3", "p4"):
            try:
                ok = notify_pool_leaderboard(pool=pool)
                logger.info("[战法榜] %s 推送 %s", pool, "成功" if ok else "跳过")
            except Exception as e:
                logger.warning("[战法榜] %s 推送异常（已吞）: %s", pool, e)
    except Exception as e:
        logger.warning("[战法榜] 推送入口异常（已吞）: %s", e)
        return False


def _job_pool_leaderboard_push_morning() -> None:
    """09:05 推送盘后战法榜（早盘版，便于开盘前决策）。"""
    if _maintenance_mode_skip("战法榜早盘推送"):
        return
    try:
        from core.paper_trading.postmarket_attribution import notify_pool_leaderboard

        ok = notify_pool_leaderboard(pool="p5")
        logger.info("[战法榜] 早盘 p5 推送 %s", "成功" if ok else "跳过")
    except Exception as e:
        logger.warning("[战法榜] 早盘推送异常（已吞）: %s", e)
        return False


def register_schedules() -> None:
    # 北京时间任务分发器（与系统本地时区解耦）：每秒 tick，内部按北京时间 HH:MM 触发。
    schedule.every(1).seconds.do(_tick_bj_daily_scheduler)
    # P2：竞价日各 1 次（09:26，与 09:18 早盘合并简报错峰）；与 UI 按钮「2档·竞价突袭」同源引擎
    _register_bj_daily("09:26", _job_p2_tick, "P2竞价")
    # P3 / P4：每秒仅跑时钟对齐函数（O(1)）；真正扫描各自每 150 秒一拍
    schedule.every(1).seconds.do(_job_p3_tick_clock_aligned)
    schedule.every(1).seconds.do(_job_p4_tick_clock_aligned)
    # 晚间链：任务均允许错过整点后补跑；压缩放到 P1/P5 之后，避免维护锁吞掉推送窗口。
    _register_bj_daily("19:45", _job_evening_sync_only, "晚间增量同步", catchup_minutes=60)
    _register_bj_daily("19:46", _job_evening_hithink_special, "晚间HiThink特色同步", catchup_minutes=60)
    _register_bj_daily("19:55", _job_evening_p1_rebuild, "晚间P1", catchup_minutes=240)
    _register_bj_daily("20:05", _job_evening_p5, "晚间P5", catchup_minutes=230)
    _register_bj_daily("20:30", _job_evening_vacuum, "晚间安全压缩", catchup_minutes=180)
    # 08:50：统一编排入口——先清企微防刷缓存，再执行早盘补位链（同步 / 条件性补 P1）
    _register_bj_daily("08:50", _job_daily_morning_routine, "早盘编排入口")
    # 08:00：每日 V26.88 数据库体检落盘（独立线程，不阻塞主循环；为早盘 08:50 提供数据基础）
    _register_bj_daily("08:00", _job_daily_db_inspect, "每日DB体检")
    # 09:18：交易日早盘合并简报企微一条（预检通过后发送），独立线程
    _register_bj_daily("09:18", _job_daily_open_heartbeat_routine, "早盘合并简报")
    # 每周心跳：固定 09:20 触发；内部按 ISO 周去重，跨重启仍每周仅发一次。
    _register_bj_daily("09:20", _job_weekly_alive_heartbeat, "每周心跳")
    # 【V26.88 取消每周库维护 03:30】20:30 已执行日常安全压缩；离线 EXPORT/IMPORT 仅供手工紧急维护。
    # 收益为零（269MB→269MB），且同样需要停守护，属于多余操作。保留 weekly_db_maintenance_orchestrated.py
    # 供手动紧急维护使用（python tools/weekly_db_maintenance_orchestrated.py --no-pause --force）。
    # _register_bj_daily("03:30", _job_weekly_db_maintenance_vacuum, "每周库维护")
    # 分时六槽快照；slot_id=1440 提前至 14:39，错峰 14:40 密集扫描
    for hhmm, sid, lbl in (
        ("09:35", "935", "09:35"),
        ("10:30", "1030", "10:30"),
        ("11:25", "1125", "11:25"),
        ("13:25", "1325", "13:25"),
        ("14:25", "1425", "14:25"),
        ("14:39", "1440", "14:39→1440错峰"),
    ):
        _register_bj_daily(
            hhmm,
            lambda sid=sid, lbl=lbl: _job_intraday_snapshot(sid, lbl),
            f"分时快照{lbl}",
        )
    # 每 30 分钟 WAL checkpoint：防止 7×24 长期运行后 .wal 膨胀；独立线程不阻塞主循环。
    schedule.every(30).minutes.do(_job_wal_checkpoint)
    # === 模拟盘（P3/P4 自动买卖实验）===
    # 08:40 建表初始化；持仓监控由**独立线程**按 monitor_interval_sec 跑（P0-4）；
    # 15:05 收盘写日净值。全部经 _maintenance_mode / 交易日屏障，不进 _SCAN_BUSY 串行闸门。
    #
    # 【P0 修复 2026-11-13】旧写法 schedule.every(1).seconds.do(_job_paper_positions_tick)
    # 把出场监控绑在主循环线程上：P3 扫描 / DeepSeek 同步调用一阻塞，止损就不被检查
    # （盘中实测 LOOP_GAP 125.5s）。现在改由 _start_paper_monitor_thread() 独立线程驱动，
    # 主循环再怎么卡，出场判定照常按秒级节奏执行。
    _start_paper_monitor_thread()
    _register_bj_daily("08:40", _job_paper_init, "模拟盘初始化")
    _register_bj_daily("08:41", _job_paper_monitor_watchdog, "模拟盘监控线程看护")
    _register_bj_daily("15:05", _job_paper_daily_settle, "模拟盘收盘结算", catchup_minutes=90)
    # 一次性补推：启动后延迟 90 秒，把当日已成交但未推送过的买入补推一条企微汇总。
    # 幂等（状态文件按日期去重），跨重启不会重复补推。
    schedule.every(90).seconds.do(_job_paper_buy_backfill_once)
    # 一次性归因报告：启动后延迟 150 秒，推送一次「按战法收益归因」。
    # 与 15:05 收盘结算后的自动推送互补（重启/补跑也能立刻看到结论）。
    schedule.every(150).seconds.do(_job_paper_attribution_push_once)

    # === V29 可观测性：细粒度心跳（5 分钟一行）+ 异常告警 ===
    # 回答「昨晚 2 点在不在跑 / 今天各档位跑没跑 / DuckDB 多大」三个问题。
    schedule.every(5).minutes.do(_job_daemon_heartbeat)
    # 异常时才推（正常静默），避免每日噪声；每小时检查一次即可。
    schedule.every(60).minutes.do(_job_daemon_health_check)
    # 每日 07:00 把过去 24h 心跳聚合成一份摘要推企微（独立于普通信号流）。
    _register_bj_daily("07:00", _job_daemon_daily_summary, "每日系统体检摘要")
    # === V29 T+N 回填：18:30（晚间同步之后、晚间 P1/P5 之前） ===
    _register_bj_daily("18:30", _job_signal_backfill, "信号T+N回填", catchup_minutes=120)
    # === 战法效果榜推送 ===
    # 18:45：紧跟 18:30 的 T+N 回填（回填完成才有最新胜率），推盘后+盘中三榜。
    # 09:05：早盘版盘后榜，开盘前可据此调整当日关注方向。
    _register_bj_daily("18:45", _job_pool_leaderboard_push, "战法效果榜推送", catchup_minutes=120)
    _register_bj_daily("09:05", _job_pool_leaderboard_push_morning, "战法榜早盘推送", catchup_minutes=60)


def _sniper_global_excepthook(exc_type, exc_value, exc_tb):
    # 【ST-P0-23 修复】Ctrl+C / 看门狗 Ctrl+Break 属正常停止，不再记 CRITICAL
    # （旧日志 22:21:54 人工停止被记成 "[UNCAUGHT] ... CRITICAL"，误导排障）。
    if issubclass(exc_type, KeyboardInterrupt):
        # 【2026-11-15】Ctrl+C = 用户主动停止：先落「人工停止」标记，之后
        # bat 循环 / 外置看门狗 见标记即不再自动重启（用户明确要求）。
        _write_user_stop_flag()
        try:
            logger.info("收到中断信号（KeyboardInterrupt），守护进程正常退出 | pid=%s", os.getpid())
        except Exception:
            pass
        sys.__excepthook__(exc_type, exc_value, exc_tb)
        return
    try:
        logger.critical(
            "[UNCAUGHT] daemon 主线程未捕获异常（1.03h 无心跳期真凶落盘）：\n%s",
            "".join(traceback.format_exception(exc_type, exc_value, exc_tb)),
        )
    except Exception:
        pass
    # 调用原 hook（默认就是 sys.__excepthook__ 走 stderr）以保持一致行为
    sys.__excepthook__(exc_type, exc_value, exc_tb)


def main() -> None:
    # ============== 【V26.88 调试期：全局未捕获异常落盘钩子】==============
    # 此前 1.03h 无心跳日志 + bat 窗口也消失的根因追查：在 daemon 启动时先装一个
    # sys.excepthook 把所有「未捕获异常」和「主线程 sys.exit」都立刻写进 sniper.log。
    # 这样即使后面有 Bug 让主循环体直接挂掉，sniper.log 必有一条 traceback 留下，
    # 我们就有足够证据确认 daemon 是「异常退出」还是「被人/系统强杀」。
    # 【ST-P0-23】全局未捕获异常钩子已提到模块级（便于单测），此处只做安装。
    sys.excepthook = _sniper_global_excepthook
    # atexit：进程退出（无论正常/异常）都打一行，区分「自愿退出」vs「被强杀」（无 atexit 日志）。
    def _sniper_atexit_log():
        try:
            logger.info(
                "[ATEXIT] daemon 进程退出 hook 触发 | pid=%s | 时间=%s（若日志中此行缺失=被强杀/段错误）",
                os.getpid(),
                _now_bj().strftime("%Y-%m-%d %H:%M:%S"),
            )
        except Exception:
            pass
    atexit.register(_sniper_atexit_log)
    if not _acquire_single_instance_lock():
        logger.error("检测到已有 auto_sniper_daemon 进程在运行，当前实例退出以避免双实例抢锁")
        # 【P0 修复 2026-11-13】退出码 2 = 「已有实例在跑」。必须与「正常退出(0)」区分开，
        # 否则 start_daemon_24x7.bat 会把这种情况当作『进程结束后重启』，
        # 每 30 秒空转拉起一个立刻退出的 python —— 自启任务 + 手动启动并存时必然触发。
        sys.exit(2)

    # 【ST-P0-24】持有系统唤醒锁：daemon 运行期间阻止系统空闲睡眠。
    # 背景：笔记本（Redmi Book Pro 14）昨夜空转后自动睡眠，主循环中断 ~5.5h，
    # 03:00 VACUUM 从未执行、盘中 P2/P4 一旦落在睡眠窗口即漏扫。此锁治本。
    # 配合电源方案（插电永不睡眠 + 合盖不休眠）双保险。退出时 atexit 自动释放。
    try:
        from core.wake_lock import acquire_system_awake, release_system_awake
        acquire_system_awake()
        atexit.register(release_system_awake)
    except Exception as e:
        logger.warning(f"⚠️ 持有系统唤醒锁失败（不影响主功能，但机器可能睡眠）: {e}")

    logger.info(
        "auto_sniper_daemon 启动 | 根目录=%s | 时区=Asia/Shanghai | pid=%s | thread=%s",
        _PROJECT_ROOT,
        os.getpid(),
        threading.current_thread().name,
    )

    # 【V26.80】启动 DB 维护调度器（每日凌晨 03:00 全量 VACUUM，守护进程内零侵入）
    try:
        from data.db_maintenance_scheduler import start_db_maintenance
        start_db_maintenance()
        logger.info("✅ V26.80 DB 维护调度器已启动（凌晨 03:00 全量 VACUUM）")
    except Exception as e:
        logger.warning(f"⚠️ V26.80 DB 维护调度器启动失败（不影响主功能）: {e}")

    # 【V26.88+9-28 P0 必升 ST-POLL-03】HiThink 盘中轮询器（09:30 / 10:30 / 13:30 / 14:30 自动拉涨停/炸板/龙虎榜/飙升榜）
    # 落地 data/runtime/state/hithink_poller_latest.json 供 P2/P3/P4/P5 战法共享；未启用时静默跳过。
    try:
        from data.hithink_poller import start_hithink_poller
        if start_hithink_poller():
            logger.info("✅ V26.88+9-28 HiThink 盘中轮询器已启动（4 时点）")
        else:
            logger.info("[hithink_poller] HiThink 未启用，盘中轮询器未启动")
    except Exception as e:
        logger.debug(f"[hithink_poller] 启动失败（不影响主程序）: {e}")
    # 调度触发统一按 _now_bj() 判定；即使系统时区错误也按北京时间执行。
    try:
        # 统一以北京时间口径记录启动时钟，避免宿主机时区为 UTC 时把日志误读成“23:10”之类的本地错觉。
        _bj = datetime.now(BJ_TZ)
        _utc_aw = datetime.now().astimezone()
        skew_sec = abs(_bj.timestamp() - _utc_aw.timestamp())
        logger.info(
            "时钟自检：北京时间=%s | 系统本地(aware)=%s | |Δepoch|=%.3fs",
            _bj.strftime("%Y-%m-%d %H:%M:%S %Z"),
            _utc_aw.strftime("%Y-%m-%d %H:%M:%S %z"),
            skew_sec,
        )
        if skew_sec >= 3600.0:
            logger.warning(
                "检测到系统时间与标准时偏差较大（|BJ_epoch−本地_epoch|≥3600s）；本守护调度仍按 Asia/Shanghai 触发，"
                "请检查 BIOS/系统时间或 NTP；若仅时区显示不同而本行 Δepoch 接近 0，可忽略。"
            )
    except Exception as e:
        logger.debug("时钟自检跳过: %s", e)
    try:
        import data.fetcher  # noqa: F401  # V26.88 拆分后注册 sys.modules['data.data_fetcher'] shim
    except Exception as e:
        logger.warning("data_fetcher 预热失败: %s", e)

    # 【V30.4 2026-10-18 P0】模拟盘建表补偿。
    # 此前只在交易日 08:40 的日任务里建表：① 首次上线当天若 daemon 是下午才启动，
    # 表会一直缺席到次日早盘；② 非交易日启动则当天不建表。
    # 此处在启动链路无条件幂等建表 + 初始化账户（ensure_tables/ensure_account 都是 IF NOT EXISTS 语义），
    # 保证「任一时点启动，第一次 P3/P4 信号到来前 paper_* 表与账户已就位」。
    try:
        from core.paper_trading.daemon_hooks import paper_init_job as _paper_init_at_boot

        _acct = _paper_init_at_boot()
        if _acct:
            logger.info("✅ 模拟盘启动期建表/账户初始化完成")
        else:
            logger.info("[模拟盘] 启动期建表跳过（未启用或异常，见前一行日志）")
    except Exception as e:
        logger.warning("模拟盘启动期建表异常（已忽略，不影响主链路）: %s", e)

    _apply_daemon_resource_profile()
    # ============== 【V26.88 每日主动退出】初始化 ==============
    global _DAILY_RESTART_HOURS, _DAEMON_START_MONO
    _DAILY_RESTART_HOURS = _load_daily_restart_config()
    _DAEMON_START_MONO = time.monotonic()
    if _DAILY_RESTART_HOURS is not None and _DAILY_RESTART_HOURS > 0:
        logger.info(
            "【每日主动退出】已开启：进程将在运行 %.1fh 后主动退出（看门狗约60s后拉起新进程以释放内存）",
            _DAILY_RESTART_HOURS,
        )
    else:
        logger.debug("【每日主动退出】未开启（server.daily_restart_hours 未配置）")
    _init_async_scan_queue_consumer()
    register_schedules()
    # 【P0 修复 2026-11-13】启动即自查「上次心跳距今多久」：
    # 2026-11-10 停摆 3 天后恢复时，日志里没有任何一行说明「中间停了 3 天」，
    # 于是「没信号」被误认为「策略不灵」。现在只要中断超过 2 小时，启动就企微告警。
    _check_interrupted_since_last_run()
    # 【V26.88+ 9-26 复盘修复】启动期主动校验 DeepSeek / HiThink / 财报新鲜度
    _init_third_party_health_checks()
    _write_daemon_public_meta()
    _maybe_open_heartbeat_on_startup()
    _cleanup_runtime_caches()
    logger.info(
        "已注册：08:50清企微防刷+早盘补位 | 09:18早盘合并简报企微(独立线程,预检) | 09:26 P2竞价 | "
        "分时快照六槽(14:39=slot1440 错峰) | 每30min WAL checkpoint | "
        "P3×%ss降频(14:31起停派让路P4) | 每周心跳 09:20(每周1条) | 20:30每日安全压缩 | "
        "P4×%ss降频(14:31起停派让路P3) | P4每拍等锁%ss | 重仓核心窗 %02d:%02d~%02d:%02d | "
        "19:45晚间增量 | 19:55晚间P1 | 20:05晚间P5 | 20:30安全压缩 | 09:35 P5早盘验证 | 非交易日跳过", 
        P3_POLL_INTERVAL_SECONDS,
        P4_POLL_INTERVAL_SECONDS,
        int(P4_TAIL_WAIT_LOCK_SEC),
        P4_CORE_TAIL_START_MIN // 60,
        P4_CORE_TAIL_START_MIN % 60,
        P4_TAIL_PRIORITY_END_MIN // 60,
        P4_TAIL_PRIORITY_END_MIN % 60,
    )
    if _async_scan_queue_lock_ok:
        logger.info(
            "scan_async: 主循环将每 %ss 轮询 process_one_pending_scan_job",
            _ASYNC_QUEUE_POLL_TICKS,
        )
    _async_poll_i = 0
    _YIELD_LOG_LAST_MONO = 0.0
    gc_tick_counter = 0
    global _MAINT_LOOP_LOG_LAST_MONO
    # 交易时段心跳按真实时间节流；同时记录相邻迭代间隔，用于识别系统休眠或主线程阻塞。
    _last_heartbeat_mono = time.monotonic()
    _last_loop_mono = _last_heartbeat_mono
    _loop_iter = 0
    _off_market_iter_counter = 0
    try:
        while True:
            _loop_now_mono = time.monotonic()
            _loop_gap_sec = _loop_now_mono - _last_loop_mono
            _last_loop_mono = _loop_now_mono
            _loop_iter += 1
            if _loop_gap_sec >= 10.0:
                logger.warning(
                    "[LOOP_GAP] 主循环中断 %.1fs 后恢复 | bj=%s | pid=%s | "
                    "可能原因=系统睡眠/进程挂起/schedule 同步任务阻塞",
                    _loop_gap_sec,
                    _now_bj().strftime("%Y-%m-%d %H:%M:%S"),
                    os.getpid(),
                )

            # ============== 【V26.88 交易时间智能休眠】非交易时段优化 ==============
            # 判断当前是否在交易时段
            _is_trading, _trade_status = _is_in_trading_window()
            _sleep_duration = _get_smart_sleep_duration()

            # 非交易时段日志降频：每5分钟最多一条
            if not _is_trading and _should_log_off_market():
                logger.info(
                    "[OFF_MARKET] iter=%s | bj=%s | status=%s | sleep=%ss",
                    _loop_iter,
                    _now_bj().strftime("%Y-%m-%d %H:%M:%S"),
                    _trade_status,
                    int(_sleep_duration),
                )

            # ============== 【V26.88 每日主动退出】检查（所有时段都需要检查）==============
            _should_exit, _exit_reason, _ = _check_daily_restart()
            if _should_exit:
                logger.warning(_exit_reason)
                try:
                    _notify_daemon_alert(
                        "【每日主动退出】守护进程即将重启",
                        _exit_reason + "\n详情见 data/runtime/sniper.log",
                        category="daemon",
                        dedup_key="daemon_daily_restart_{}".format(_now_bj().strftime("%Y%m%d")),
                    )
                except Exception:
                    pass
                break  # 退出主循环

            # ============== 【V26.88 维护锁优先】 ==============
            # 在 schedule.run_pending() 之前短路，避免定时任务与运维 VACUUM/离线压库争用 DuckDB。
            try:
                from core.master_control import is_maintenance_mode_enabled
                _maint_on = bool(is_maintenance_mode_enabled())
            except Exception as e:
                logger.debug("主循环维护模式检测异常(忽略): %s", e)
                _maint_on = False
            if _maint_on:
                _now_mono = time.monotonic()
                if _MAINT_LOOP_LOG_LAST_MONO is None or (_now_mono - _MAINT_LOOP_LOG_LAST_MONO) >= 300.0:
                    _MAINT_LOOP_LOG_LAST_MONO = _now_mono
                    logger.warning(
                        "🚨 [维护锁] maintenance_mode=ON，本轮跳过 schedule.run_pending，休眠 %ss 后重试",
                        int(_sleep_duration),
                    )
                time.sleep(_sleep_duration)
                continue  # 回到循环开头

            # ============== 【V26.88 UI 让路探测】 ==============
            # UI 主动写数据同步前会写 ui_yield_signal.json，检测到后释放写连接让 UI 抢占
            _skip_this_tick = False
            try:
                from core.master_control import read_ui_yield_signal
                from data import db_core as _db_core

                _sig = read_ui_yield_signal()
                if _sig is not None:
                    _yielded = bool(_db_core.release_write_conn_for_yield())
                    _sig_p = _sig.get("requested_pid")
                    _sig_r = _sig.get("reason")
                    _now_mono_y = time.monotonic()
                    if _yielded and (_now_mono_y - _YIELD_LOG_LAST_MONO) >= 5.0:
                        _YIELD_LOG_LAST_MONO = _now_mono_y
                        logger.info(
                            "🟡 [UI 让路] 已释放 DuckDB 写连接：pid=%s reason=%s",
                            _sig_p,
                            _sig_r,
                        )
                    time.sleep(_sleep_duration)
                    continue  # 直接回到 while 开头
            except Exception as _ye:
                logger.debug("UI 让路探测异常(忽略): %s", _ye)

            # ============== 【V26.88 定时任务始终执行】 ==============
            # 重要：定时任务（如19:45/19:55/20:05晚间任务）必须在所有时段执行
            # schedule.run_pending() 本身非常轻量（只是检查时间），不应该跳过
            try:
                schedule.run_pending()
            except Exception as e:
                logger.exception("schedule.run_pending 异常(继续运行): %s", e)

            # 异步队列轮询：交易时段正常频率，非交易时段降频
            _async_poll_i += 1
            if _is_trading:
                # 交易时段：正常轮询
                if _async_poll_i >= _ASYNC_QUEUE_POLL_TICKS:
                    _async_poll_i = 0
                    try:
                        _tick_async_scan_queue()
                    except Exception as e:
                        logger.exception("async_scan 队列轮询外层异常(继续运行): %s", e)
            else:
                # 非交易时段：大幅降低异步队列轮询频率（每60秒才检查一次）
                if _async_poll_i >= 60:  # 原来每10秒检查，现在改为每60秒
                    _async_poll_i = 0
                    try:
                        _tick_async_scan_queue()
                    except Exception as e:
                        logger.debug("非交易时段 async_scan 队列轮询异常(忽略): %s", e)

            gc_tick_counter += 1
            if gc_tick_counter >= 3600:
                gc.collect()
                gc_tick_counter = 0

            # 仅交易时段按真实 60 秒输出心跳；循环迭代数不能代表秒数，因为动态 sleep 可能低至 0.1 秒。
            _heartbeat_now_mono = time.monotonic()
            if _is_trading and (
                _loop_iter == 1 or (_heartbeat_now_mono - _last_heartbeat_mono) >= 60.0
            ):
                logger.info(
                    "[IN_LOOP] iter=%s | bj=%s | pid=%s | alive_threads=%s | trade=%s",
                    _loop_iter,
                    _now_bj().strftime("%Y-%m-%d %H:%M:%S"),
                    os.getpid(),
                    threading.active_count(),
                    _trade_status,
                )
                _last_heartbeat_mono = _heartbeat_now_mono

    # ====== V26.88 交易时间智能休眠 ======
            # 关键修复：不固定 60s，而是计算到下一个 schedule 任务的秒数（避免 60s 固定 sleep 漏触发）。
            # APScheduler schedule.next_run() 返回系统本地 naive datetime（假设环境 TZ=Asia/Shanghai，即北京时间）。
            _next_due = schedule.next_run()
            if _next_due is not None:
                _now_naive = _now_bj().replace(tzinfo=None)
                _secs_until_next = max(0.0, (_next_due - _now_naive).total_seconds())
                _dynamic_sleep = min(_secs_until_next, _sleep_duration)
                if _dynamic_sleep < 0.1:
                    _dynamic_sleep = 0.1  # 至少 0.1s，避免紧循环
            else:
                _dynamic_sleep = _sleep_duration
            time.sleep(_dynamic_sleep)
    finally:
            # 必须放在 schedule.run_pending() 和所有业务逻辑之后、DuckDB 单例释放之前。
            # 退出前强制 gc.collect()，减少下一进程继承的内存碎片。
            logger.info("守护进程退出中，释放单实例锁...")
            _release_single_instance_lock()
            gc.collect()
            logger.info("守护进程已退出 | pid=%s", os.getpid())


if __name__ == "__main__":
    main()

#
# =============================================================================
# 交钥匙：pm2 长期部署 + 日志切割（Linux / 家用小主机）
# =============================================================================
#
# --- 环境与目录 ---
#   sudo timedatectl set-timezone Asia/Shanghai
#   cd /path/to/xiaozhu
#   pip3 install -r requirements.txt -r requirements-daemon.txt   # 系统 Python；至少含 schedule 等
#
# --- pm2 启动（推荐 ecosystem 文件，便于合并日志与轮转参数）---
#   新建 ecosystem.sniper.config.cjs 内容示例：
#   ---------------------------------------------------------------------------
#   module.exports = {
#     apps: [{
#       name: 'xiaozhu-sniper',
#       script: 'auto_sniper_daemon.py',
#       interpreter: '/usr/bin/python3',   # 或 `which python3` 的绝对路径
#       cwd: '/path/to/xiaozhu',
#       instances: 1,
#       autorestart: true,
#       max_restarts: 50,
#       min_uptime: '10s',
#       error_file: '/path/to/xiaozhu/logs/sniper-err.log',
#       out_file: '/path/to/xiaozhu/logs/sniper-out.log',
#       merge_logs: true,
#       time: true,
#     }]
#   };
#   ---------------------------------------------------------------------------
#   mkdir -p logs
#   pm2 start ecosystem.sniper.config.cjs
#   pm2 save
#   pm2 startup systemd -u $USER --hp $HOME   # 按提示执行一条 sudo 命令
#
# --- 日志切割（二选一）---
#   (A) pm2 模块 logrotate（简单）:
#       pm2 install pm2-logrotate
#       pm2 set pm2-logrotate:max_size 50M
#       pm2 set pm2-logrotate:retain 14
#       pm2 set pm2-logrotate:compress true
#
#   (B) 系统 logrotate（示例 /etc/logrotate.d/xiaozhu-sniper）:
#       /path/to/xiaozhu/logs/*.log {
#         daily
#         rotate 14
#         compress
#         missingok
#         notifempty
#         copytruncate
#       }
#
# --- 运维命令 ---
#   pm2 status
#   pm2 logs xiaozhu-sniper --lines 200
#   pm2 restart xiaozhu-sniper
#   pm2 stop xiaozhu-sniper
#
# =============================================================================
