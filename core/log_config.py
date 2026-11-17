# -*- coding: utf-8 -*-
"""
守护进程 / 工具脚本共用的日志落盘配置。

默认落盘路径：``data/runtime/sniper.log``（与 ``core.runtime_data_paths.RUNTIME_DIR`` 对齐）。
"""
from __future__ import annotations

import logging
import os
import re
import sys
from datetime import datetime
from logging.handlers import RotatingFileHandler
from zoneinfo import ZoneInfo


# ==================== 密钥脱敏（P0-3，2026-11-14）====================
# 事故背景：企微 webhook 发送失败时，requests 异常串里带完整 URL（含 key=...），
# 被原样追加进 sniper.log —— 日志会被分享/留存，等于密钥明文扩散。
# 解决：在 Handler 层对所有日志消息做一次不可逆打码（消息级 Filter），
# 任何 forget-to-mask 的调用点都能被兜住。
_SECRET_RE = (
    re.compile(r"(key=)[0-9A-Za-z\-_]{8,}"),
    re.compile(
        r"((?:access_token|api_?key|api_?secret|secret|password|passwd|token)\s*[=:]\s*)"
        r"[^\s&\"'，。；]{4,}",
        re.IGNORECASE,
    ),
)


def scrub_secrets(text: object) -> str:
    """把文本里的密钥类字段打码（幂等；非字符串安全转换）。"""
    out = str(text)
    for pat in _SECRET_RE:
        out = pat.sub(lambda m: m.group(1) + "***", out)
    return out


class _SecretScrubFilter(logging.Filter):
    """落盘/输出前打码密钥；过滤本身绝不抛异常（日志链路的熔断点）。"""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
            scrubbed = scrub_secrets(msg)
            if scrubbed != msg:
                record.msg = scrubbed
                record.args = ()
        except Exception:
            pass
        return True


class _MissingScriptRunContextFilter(logging.Filter):
    """屏蔽 Streamlit 后台线程里已知无害的 ScriptRunContext 噪音。"""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            return "missing ScriptRunContext" not in record.getMessage()
        except Exception:
            return True

_SNIPER_LOG_SETUP_DONE = False
_BJ_TZ = ZoneInfo("Asia/Shanghai")


class _ShanghaiFormatter(logging.Formatter):
    """强制按北京时间格式化日志时间，避免系统时区显示为 UTC。"""

    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, tz=_BJ_TZ)
        if datefmt:
            return dt.strftime(datefmt)
        return dt.strftime("%Y-%m-%d %H:%M:%S")


def path_sniper_rotating_log() -> str:
    """返回 ``data/runtime/sniper.log`` 绝对路径。

    可用 ``XJ_SNIPER_LOG_PATH`` 覆盖 —— 回归脚本 import daemon 时会连带挂上
    生产日志的 FileHandler，若不重定向，测试里的模拟告警会写进生产日志、
    污染事后复盘证据。
    """
    override = (os.getenv("XJ_SNIPER_LOG_PATH") or "").strip()
    if override:
        return os.path.abspath(override)
    try:
        from core.runtime_data_paths import RUNTIME_DIR, ensure_runtime_data_layout

        ensure_runtime_data_layout()
        return os.path.join(RUNTIME_DIR, "sniper.log")
    except Exception:
        here = os.path.dirname(os.path.abspath(__file__))
        root = os.path.dirname(here)
        d = os.path.join(root, "data", "runtime")
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, "sniper.log")


def _fallback_log_path() -> str:
    """日志主路径不可写时的备用落盘路径（%TEMP%/xiaojie_sniper/sniper.log）。"""
    try:
        import tempfile

        d = os.path.join(tempfile.gettempdir(), "xiaojie_sniper")
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, "sniper.log")
    except Exception:
        return ""


def setup_sniper_rotating_file_logging(
    level: int = logging.INFO,
    *,
    also_stream: bool = True,
) -> None:
    """
    为 root logger 配置 ``sniper.log`` 的 RotatingFileHandler，并可选附加 stderr StreamHandler。

    开启日志轮转，防止 7x24 纯后台模式下日志文件无限膨胀撑爆磁盘。
    单文件上限 50MB、最多 10 个历史备份、UTF-8 编码。
    """
    global _SNIPER_LOG_SETUP_DONE
    if _SNIPER_LOG_SETUP_DONE:
        return

    log_path = path_sniper_rotating_log()
    parent = os.path.dirname(log_path) or "."
    try:
        os.makedirs(parent, exist_ok=True)
    except Exception as e:                                   # pragma: no cover
        logging.warning("日志目录不可创建，改为仅 stderr 输出: %s", e)

    fmt = _ShanghaiFormatter(
        "%(asctime)s [SNIPER] %(levelname)s %(message)s",
        "%Y-%m-%d %H:%M:%S",
    )

    # 开启日志轮转，防止 7x24 纯后台模式下日志文件无限膨胀撑爆磁盘
    #
    # 【2026-11-03 修复】原实现直接构造 RotatingFileHandler，一旦日志文件不可写
    # （目录 ACL 不足 / 被别的账号占用 / 磁盘只读）会抛 PermissionError，
    # 而 auto_sniper_daemon.py 在 import 阶段就调用本函数 → daemon 直接启动失败、
    # 没有任何日志、看起来像「静默不跑」。现改为三级兜底：
    #   ① 首选 data/runtime/sniper.log
    #   ② 不可写 → 退回 %TEMP%/xiaojie_sniper/sniper.log（仍落盘、仍轮转）
    #   ③ 仍不可写 → 只走 stderr，不让日志问题阻断主流程
    rh = None
    for candidate in (log_path, _fallback_log_path()):
        if not candidate:
            continue
        try:
            os.makedirs(os.path.dirname(candidate) or ".", exist_ok=True)
            rh = RotatingFileHandler(
                candidate,
                maxBytes=50 * 1024 * 1024,
                backupCount=10,
                encoding="utf-8",
            )
            log_path = candidate
            break
        except Exception as e:                               # pragma: no cover
            logging.warning("日志文件不可写，尝试下一个候选路径: %s -> %s", candidate, e)
            rh = None
    if rh is None:
        logging.warning("所有日志文件路径均不可写，仅使用 stderr 输出")

    root = logging.getLogger()
    for h in list(root.handlers):
        try:
            root.removeHandler(h)
        except Exception:
            pass
    root.setLevel(level)
    scrub = _SecretScrubFilter()
    if rh is not None:
        rh.setFormatter(fmt)
        rh.addFilter(scrub)
        root.addHandler(rh)
    root.addFilter(_MissingScriptRunContextFilter())
    if also_stream:
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(fmt)
        sh.addFilter(_MissingScriptRunContextFilter())
        sh.addFilter(scrub)
        root.addHandler(sh)

    _SNIPER_LOG_SETUP_DONE = True
