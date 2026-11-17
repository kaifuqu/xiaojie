# -*- coding: utf-8 -*-
"""
core/wake_lock.py — 防睡眠唤醒锁（Windows）

【背景 / ST-P0-24】
本系统计划在笔记本（XIAOMI Redmi Book Pro 14）上 7×24 运行。实测发现：
  - 插电睡眠超时 180 分钟、电池 3 分钟；
  - 全系统没有任何 SetThreadExecutionState 调用；
  - 昨夜机器空转后自动睡眠，导致 daemon 主循环中断合计 ~5.5 小时：
      [LOOP_GAP] 主循环中断 2330.9s / 16857.9s
      03:00 VACUUM 从未真正触发（正好落在睡眠窗口）。
  - 盘中任务对「睡眠」毫无容错：P2 竞价（09:26，catchup=0）、
    P4 尾盘五拍（精确分钟匹配）一旦在睡眠中错过就直接漏掉。

【作用】
daemon 启动时调用 acquire_system_awake()，进程存活期间阻止系统因空闲自动睡眠
（屏幕仍可熄灭、可关显示器，不影响正常使用）。进程退出（atexit）自动释放。

【实现】
Windows 用 kernel32.SetThreadExecutionState：
  ES_CONTINUOUS        (0x80000000) 持续生效，直到再次调用重置
  ES_SYSTEM_REQUIRED   (0x00000001) 阻止系统进入睡眠
  ES_AWAYMODE_REQUIRED (0x00000040) 允许系统进入"离开模式"，程序继续运行
非 Windows 平台为安全空操作（no-op），绝不抛错。

【注意】
ES_SYSTEM_REQUIRED 只阻止「系统睡眠」，不阻止用户手动关机/合盖（合盖行为由电源方案决定，
请配合 `powercfg /setacvalueindex ... LIDACTION 0` 设置合盖不睡眠）。
"""
from __future__ import annotations

import logging
import sys
import threading
from typing import Optional

logger = logging.getLogger(__name__)

# --- Windows kernel32 常量 ---
_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001
_ES_AWAYMODE_REQUIRED = 0x00000040

# 兼容 Win7 的旧常量（无 AWAYMODE）；新系统上 AWAYMODE 可用。

_lock_state: Optional[str] = None  # None / "held" / "released" / "noop"
_state_guard = threading.Lock()


def _kernel32():
    import ctypes

    return ctypes.windll.kernel32  # type: ignore[attr-defined]


def acquire_system_awake() -> bool:
    """阻止系统空闲睡眠。返回是否成功持有唤醒锁（非 Windows 返回 False）。"""
    global _lock_state
    if not sys.platform.startswith("win"):
        logger.info("[wake_lock] 非 Windows 平台，跳过唤醒锁（no-op）")
        with _state_guard:
            _lock_state = "noop"
        return False
    with _state_guard:
        if _lock_state == "held":
            return True
        try:
            flags = _ES_CONTINUOUS | _ES_SYSTEM_REQUIRED | _ES_AWAYMODE_REQUIRED
            prev = _kernel32().SetThreadExecutionState(flags)
            if prev == 0:
                # AWAYMODE 不被支持时回退到只阻止系统睡眠
                flags = _ES_CONTINUOUS | _ES_SYSTEM_REQUIRED
                prev = _kernel32().SetThreadExecutionState(flags)
            if prev == 0:
                logger.warning("[wake_lock] SetThreadExecutionState 返回 0（持有失败）")
                _lock_state = "noop"
                return False
            _lock_state = "held"
            logger.info(
                "[wake_lock] ✅ 已持有系统唤醒锁（daemon 运行期间阻止空闲睡眠；"
                "屏幕仍可熄灭，合盖行为另由电源方案决定）"
            )
            return True
        except Exception as e:  # pragma: no cover - 防御性
            logger.warning("[wake_lock] 持有唤醒锁异常（忽略，不影响主功能）: %s", e)
            _lock_state = "noop"
            return False


def release_system_awake() -> None:
    """释放唤醒锁，恢复系统正常休眠行为。幂等、绝不抛错。"""
    global _lock_state
    if not sys.platform.startswith("win"):
        return
    with _state_guard:
        if _lock_state != "held":
            return
        try:
            _kernel32().SetThreadExecutionState(_ES_CONTINUOUS)
            logger.info("[wake_lock] 已释放系统唤醒锁（恢复系统休眠行为）")
        except Exception as e:  # pragma: no cover
            logger.debug("[wake_lock] 释放唤醒锁异常（忽略）: %s", e)
        finally:
            _lock_state = "released"


def is_held() -> bool:
    return _lock_state == "held"


__all__ = ["acquire_system_awake", "release_system_awake", "is_held"]
