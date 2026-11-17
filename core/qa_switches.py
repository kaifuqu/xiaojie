# -*- coding: utf-8 -*-
"""
core/qa_switches.py  —— 【V30.9 2026-10-18】测试/演练「只读旁路」统一开关中枢

【为什么需要这个模块】
2026-10-18 上线前全功能测试暴露了两类「QA 脚本污染生产」事故：
  事故 A（已修）：_qa_logs/_ui_menu_probe.py、_qa_logs/_lab_tab_probe.py 会真实触发
                P1-P5 扫描，save_signal_log 按 (trade_date, pool, ts_code) 主键 UPSERT，
                把 43 行测试信号写进生产 signal_log，其中 9 行覆盖改写了 10-16 的
                9 条真实盘后信号。已在 save_signal_log 加 XJ_NO_SIGNAL_PERSIST 短路。
  事故 B（本模块修复）：同一个点检在 notify_scan_results_top3 -> push_stock_if_allowed
                链路里，会真实向企微 webhook 发出 markdown、并真实调用 DeepSeek
                （07:22 那轮 3 条、09:32 这轮 10 条，累计 13 条真实推送 + 13 次计费调用）。
                原因：master_control.wechat_push_enabled=True（生产实盘配置），
                且推送/AI 链路里没有任何「测试模式」旁路。

【设计原则】
1. 三个开关全部只读环境变量，**生产启动脚本一律不设置**（回归 tools/_test_no_signal_persist.py V6
   与 tools/_test_no_side_effects.py V5 会断言这一点）。
2. 开关读取放在「副作用动作的最外层」，而不是散落在业务逻辑里，保证短路点早于
   取写连接、早于 HTTP 请求、早于 DeepSeek 计费调用。
3. 任一读取异常都按「不短路」处理（fail-open），绝不允许因为开关读取失败而阻断生产。
4. 一个开关管住一类副作用：XJ_NO_PUSH 同时管住企微 HTTP 发送与系统告警，
   XJ_NO_DEEPSEEK 单独管 DeepSeek 计费调用（两者可以单独关，便于分场景演练）。

【用法】
  QA / 演练 / 实验室点检进程入口统一写这三行：
      os.environ["XJ_NO_SIGNAL_PERSIST"] = "1"
      os.environ["XJ_NO_PUSH"]           = "1"
      os.environ["XJ_NO_DEEPSEEK"]       = "1"

【真值表】（大小写不敏感，允许首尾空白）
  "1" / "true" / "yes" / "on"  -> 生效（跳过副作用）
  "0" / "false" / "no" / "off" / "" / 未设置 -> 不生效（照常执行）
"""
from __future__ import annotations

import os
from typing import Iterable

__all__ = [
    "TRUTHY",
    "env_flag",
    "no_signal_persist",
    "no_push",
    "no_deepseek",
    "read_only_mode",
    "describe",
]

#: 视为「生效」的真值（小写比较前已 strip + lower）
TRUTHY: tuple[str, ...] = ("1", "true", "yes", "on")


def env_flag(name: str, default: str = "") -> bool:
    """读取布尔型环境变量开关。

    任何异常（非法名、非字符串等）都 fail-open 返回 False，绝不因开关读取失败阻断生产。
    """
    try:
        raw = os.environ.get(str(name), str(default))
        return str(raw).strip().lower() in TRUTHY
    except Exception:  # pragma: no cover - 防御性分支
        return False


def no_signal_persist() -> bool:
    """True = 不写 signal_log / signal_strategy_ledger（QA、演练、实验室点检）。"""
    return env_flag("XJ_NO_SIGNAL_PERSIST")


def no_push() -> bool:
    """True = 不向企微 webhook 发送任何 markdown（企微股票信号 + 系统告警）。"""
    return env_flag("XJ_NO_PUSH")


def no_deepseek() -> bool:
    """True = 不调用 DeepSeek（避免测试产生真实计费），也不写 fact_ai_analysis_log。"""
    return env_flag("XJ_NO_DEEPSEEK")


def read_only_mode() -> bool:
    """True = 三类副作用全部关闭（纯只读演练模式）。"""
    return no_signal_persist() and no_push() and no_deepseek()


def describe() -> str:
    """给 UI / 日志用的单行状态摘要（不含任何敏感配置）。"""
    flags = (
        ("信号落库", no_signal_persist()),
        ("企微推送", no_push()),
        ("DeepSeek", no_deepseek()),
    )
    on = [name for name, v in flags if v]
    if not on:
        return "生产模式（全部副作用开启）"
    off = [name for name, v in flags if not v]
    txt = "只读演练模式：已关闭 " + "、".join(on)
    if off:
        txt += "；仍开启 " + "、".join(off)
    return txt


def truthy_table(values: Iterable[str]) -> list[str]:
    """给回归测试用：列出所有会被判定为「生效」的值，便于防回归清单同步。"""
    return [v for v in values if str(v).strip().lower() in TRUTHY]
