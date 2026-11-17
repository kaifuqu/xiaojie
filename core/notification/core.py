# -*- coding: utf-8 -*-
"""
core/notification/core.py
原 core/notification_gateway.py L14-L413（全局常量 + Webhook 线程池 + 工具函数）

【职责】
- _WECHAT_WEBHOOK_EXECUTOR：max_workers=3 全局有界线程池
- _shutdown_wechat_executor / atexit 注册
- _SPAM_WINDOW_SEC / _SYSTEM_ALERT_DEDUP_DEFAULT_SEC / _MAX_MARKDOWN_CONTENT_LEN / _REQUEST_TIMEOUT
- _WEBHOOK_MAX_ATTEMPTS（声明在 gateway.py 中）
- _PATH_ALERT_DEDUP / _file_reserve_system_alert_slot：跨进程文件去重账本
- _POOL_ALERT_TITLES：战区展示名
- _gateway_singleton / _gateway_secondary_singleton / _gateway_lock：单例管理
- _bj_calendar_key / _bj_now_str：北京时间工具
- _norm_ts_code / _strategy_dedup_segment / _strategy_log_fragment：归一化
- _finite_float_or_none / _parse_pct_to_float：数值防爆
- _format_system_alert_body / _esc：模板渲染

【依赖】
- core.runtime_data_paths（容错导入 path_alert_dedup_cache_json）
- core.file_utils.atomic_json_update（容错导入）
"""
import atexit
import html
import logging
import math
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# 企微 HTTP 发送统一走有界线程池
_WECHAT_WEBHOOK_EXECUTOR = ThreadPoolExecutor(
    max_workers=3,
    thread_name_prefix="wechat-webhook",
)


def _shutdown_wechat_executor() -> None:
    try:
        _WECHAT_WEBHOOK_EXECUTOR.shutdown(wait=False, cancel_futures=False)
    except TypeError:
        _WECHAT_WEBHOOK_EXECUTOR.shutdown(wait=False)
    except Exception:
        pass


atexit.register(_shutdown_wechat_executor)

_SPAM_WINDOW_SEC = 30 * 60
# 运维/数据类告警独立去重；同一 dedup 键默认 12h 仅推一条
_SYSTEM_ALERT_DEDUP_DEFAULT_SEC = 12 * 3600

# 企微 markdown 单条长度上限（官方约 4096）；留出余量截断
_MAX_MARKDOWN_CONTENT_LEN = 3800

# HTTP：连接超时 + 读取超时（秒），避免套接字永久挂死
_REQUEST_TIMEOUT = (5.0, 15.0)

# 发送失败后的重试次数（含首次共 attempts 次）；间隔秒（简易退避）
_WEBHOOK_MAX_ATTEMPTS = 3
_WEBHOOK_RETRY_SLEEP_SEC = 3.0
_WEBHOOK_CIRCUIT_FAILURE_THRESHOLD = 3
_WEBHOOK_CIRCUIT_OPEN_SEC = 15 * 60

# 防刷字典极端保护：超过该条数时强制收缩（正常 30 分钟滑动窗口远小于此）
_SPAM_RECORDS_HARD_CAP = 8000


# ============ 路径解析 ============
def _path_alert_dedup_cache_file() -> str:
    """系统告警去重账本路径：``data/runtime/alert_dedup_cache.json``。"""
    try:
        from core.runtime_data_paths import path_alert_dedup_cache_json
        return path_alert_dedup_cache_json()
    except Exception:
        here = os.path.dirname(os.path.abspath(__file__))
        root = os.path.dirname(here)
        d = os.path.join(root, "data", "runtime")
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, "alert_dedup_cache.json")


def _file_reserve_system_alert_slot(dedup_key: str, window_sec: float) -> bool:
    """跨进程系统告警去重：atomic_json_update 读写账本；True=允许发送并已写入时间戳，False=冷却期内跳过。"""
    from core.file_utils import atomic_json_update

    win = max(60.0, min(float(window_sec), float(7 * 24 * 3600)))
    k = f"sys:{str(dedup_key)[:200]}"
    now = time.time()
    box: Dict[str, bool] = {"allow": False}

    def _upd(data: Dict[str, Any]) -> None:
        cutoff_age = max(14.0 * 24 * 3600, 2.0 * win)
        cutoff_ts = now - cutoff_age
        for sk in list(data.keys()):
            if not isinstance(sk, str):
                continue
            sv = data.get(sk)
            try:
                fv = float(sv)
            except (TypeError, ValueError):
                continue
            if fv < cutoff_ts:
                try:
                    del data[sk]
                except KeyError:
                    pass

        last = 0.0
        try:
            last = float(data.get(k, 0) or 0)
        except (TypeError, ValueError):
            last = 0.0

        if now - last < win:
            box["allow"] = False
            return

        data[k] = now
        box["allow"] = True

    try:
        atomic_json_update(_path_alert_dedup_cache_file(), _upd, timeout=8)
    except Exception as e:
        logger.warning("企微系统告警文件去重账本异常，本次放行发送以避免漏告: %s", e)
        return True

    return box["allow"]


# 战区展示名（与 UI 心智对齐）
_POOL_ALERT_TITLES = {
    "p2": ("🚨", "P2竞价突袭"),
    "p3": ("🚨", "P3盘中防守"),
    "p4": ("🚨", "P4尾盘盲狙"),
    "p5": ("🌙", "P5盘后真龙"),
}

_gateway_singleton: Optional["WechatNotificationGateway"] = None  # noqa: F821
_gateway_secondary_singleton: Optional["WechatNotificationGateway"] = None  # noqa: F821
_gateway_lock = threading.RLock()

# 模块级共享去重字典与锁（与 gateway 实例属性 _pushed_records 互为冗余保险）
_pushed_records: Dict[str, float] = {}
_pushed_records_lock = threading.RLock()
_system_alert_dedup_lock = threading.RLock()


# ============ 时间工具 ============
def _bj_calendar_key() -> str:
    """北京时间日历键 YYYYMMDD"""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d")
    except Exception:
        return datetime.now().strftime("%Y%m%d")


def _bj_now_str(fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """北京时间格式化时间字符串。"""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Asia/Shanghai")).strftime(fmt)
    except Exception:
        return datetime.now().strftime(fmt)


# ============ 归一化工具 ============
def _norm_ts_code(code: Any) -> str:
    s = str(code or "").strip()
    if not s:
        return ""
    s = s.split(".")[0]
    return s[:6] if len(s) >= 6 else s.zfill(6)[:6]


def _strategy_dedup_segment(raw: Any) -> str:
    """从 stock_dict「战法」字段构造去重键片段：去首尾空白、压缩连续空白。"""
    s = str(raw or "").strip()
    if not s:
        return "__NA__"
    return " ".join(s.split())


def _strategy_log_fragment(raw: Any) -> str:
    """日志用短文案"""
    sk = _strategy_dedup_segment(raw)
    if sk == "__NA__":
        return "(未填战法)"
    return sk if len(sk) <= 80 else sk[:77] + "..."


def _finite_float_or_none(val: Any) -> Optional[float]:
    """转 float；NaN/inf/异常一律 None"""
    if val is None:
        return None
    try:
        if isinstance(val, float) and (math.isnan(val) or math.isinf(val)):
            return None
        x = float(val)
        if math.isnan(x) or math.isinf(x):
            return None
        return x
    except (TypeError, ValueError):
        return None


def _parse_pct_to_float(pct: Any) -> float:
    """解析「12.34%」或数字为 float"""
    if pct is None:
        return 0.0
    if isinstance(pct, (int, float)):
        try:
            x = float(pct)
            if math.isnan(x) or math.isinf(x):
                return 0.0
            return x
        except (TypeError, ValueError):
            return 0.0
    s = str(pct).strip().replace("%", "")
    if not s or s.lower() in ("nan", "none", "--"):
        return 0.0
    try:
        x = float(s)
        if math.isnan(x) or math.isinf(x):
            return 0.0
        return x
    except (TypeError, ValueError):
        return 0.0


# ============ 系统告警模板 ============
def _format_system_alert_body(title: str, detail: str, category: str) -> str:
    """企微 markdown：数据/扫描异常温馨提示（与交易信号格式区分）。"""
    try:
        from zoneinfo import ZoneInfo
        ts = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    d = (detail or "").strip()
    if len(d) > 1500:
        d = d[:1500] + "\n…(已截断)"
    lines = [
        "⚠️【小杰系统提示】",
        _esc(title),
        f"时间：{_esc(ts)}",
        f"类别：{_esc(category)}",
        "---",
    ]
    for ln in d.split("\n"):
        lines.append(_esc(ln))
    body = "\n".join(lines)
    if len(body) > _MAX_MARKDOWN_CONTENT_LEN:
        body = body[: _MAX_MARKDOWN_CONTENT_LEN - 20] + "\n…(已截断)"
    return body


def _esc(s: Any) -> str:
    """HTML 转义"""
    try:
        if s is None:
            return "--"
        if isinstance(s, float) and (math.isnan(s) or math.isinf(s)):
            return "--"
        return html.escape(str(s), quote=False)
    except Exception:
        return "--"


__all__ = [
    "_WECHAT_WEBHOOK_EXECUTOR", "_shutdown_wechat_executor",
    "_SPAM_WINDOW_SEC", "_SYSTEM_ALERT_DEDUP_DEFAULT_SEC",
    "_MAX_MARKDOWN_CONTENT_LEN", "_REQUEST_TIMEOUT",
    "_path_alert_dedup_cache_file", "_file_reserve_system_alert_slot",
    "_POOL_ALERT_TITLES",
    "_gateway_singleton", "_gateway_secondary_singleton", "_gateway_lock",
    "_pushed_records", "_pushed_records_lock", "_system_alert_dedup_lock",
    "_bj_calendar_key", "_bj_now_str",
    "_norm_ts_code", "_strategy_dedup_segment", "_strategy_log_fragment",
    "_finite_float_or_none", "_parse_pct_to_float",
    "_format_system_alert_body", "_esc",
]