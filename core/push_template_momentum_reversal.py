# -*- coding: utf-8 -*-
"""
小杰AI选股系统 Pro V28 — 动量/反转推送模板与时机分流
================================================================================
【V28 新增·H 项】对动量票 vs 反转票用不同的推送文案和不同的推送时机。

动量票（momentum）：
  - 推送时机：盘后立即（15:30 之前）
  - 文案：⚡动量主线 + "明日 09:30 直接追入 + 严格止损"
  - 出场模板：medium（中止损）

反转票（reversal）：
  - 推送时机：次日 09:25 竞价前（让用户先看竞价表现再决策）
  - 文案：🔄反转低吸 + "次日 09:25 看竞价承接 + MA5 确认后再进"
  - 出场模板：wide（宽止损）

集成：
  - core/pool_signal_adapter_v28.append_signal_log_safe 末尾调用
================================================================================
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

BJ_TZ = timezone(timedelta(hours=8))


def detect_track(res: Dict[str, Any]) -> str:
    """
    检测 res 对应的"动量 / 反转 / 中性"。
    """
    try:
        score_details = res.get("score_details") or {}
        track = str(score_details.get("P1动量/反转分类", "")).strip()
        if track in ("momentum", "reversal", "neutral"):
            return track
        # 备用：从 strategies 字符串里找
        strategies = res.get("strategies") or []
        if isinstance(strategies, str):
            strategies = [s.strip() for s in strategies.replace("|", ",").split(",") if s.strip()]
        joined = "|".join(str(s) for s in strategies)
        if "动量" in joined or "momentum" in joined.lower():
            return "momentum"
        if "反转" in joined or "reversal" in joined.lower():
            return "reversal"
    except Exception:
        pass
    return "neutral"


def build_push_hint(res: Dict[str, Any], track: str = "") -> Dict[str, Any]:
    """
    根据 track 类别生成推送文案和时机。
    返回 {push_time_hint, hint_emoji, hint_title, hint_body}
    """
    if not track:
        track = detect_track(res)

    if track == "momentum":
        return {
            "track": "momentum",
            "push_time_hint": "盘后立即推（≤15:30）",
            "hint_emoji": "⚡",
            "hint_title": "动量主线",
            "hint_body": "次日 09:30 直接追入 + 严守中止损模板",
        }
    if track == "reversal":
        return {
            "track": "reversal",
            "push_time_hint": "次日 09:25 竞价前推",
            "hint_emoji": "🔄",
            "hint_title": "反转低吸",
            "hint_body": "次日 09:25 看竞价承接 + MA5 站稳后再进（宽止损）",
        }
    return {
        "track": "neutral",
        "push_time_hint": "盘后立即推",
        "hint_emoji": "⚪",
        "hint_title": "中性票",
        "hint_body": "按 score_details 中各项提示操作",
    }


def is_push_time_window(track: str, now: Optional[datetime] = None, force: bool = False) -> bool:
    """
    动量票：盘后立即（15:00-15:30）
    反转票：次日 09:00-09:25
    中性票：盘后立即

    参数：
        force=True：忽略时段约束（用于"非实时"测试 / 离线回放 / cron 重试）
    """
    if force:
        return True
    now = now or datetime.now(BJ_TZ)
    hhmm = now.hour * 100 + now.minute

    if track == "reversal":
        return 900 <= hhmm <= 925
    if track == "momentum":
        return 1500 <= hhmm <= 1530
    return 1500 <= hhmm <= 1530


def should_push_now(track: str = "neutral", now: Optional[datetime] = None) -> bool:
    """
    推送主链路入口函数：
      - 在正确时段返回 True
      - 其他时间但距离正确时段 ≤ 8 小时返回 True（cron 重试窗口）
      - 否则 False
    """
    if is_push_time_window(track, now=now):
        return True
    now = now or datetime.now(BJ_TZ)
    hhmm = now.hour * 100 + now.minute
    if track == "reversal":
        # 09:25 之后到 17:00 之前都允许 cron 重试
        return hhmm >= 925 and hhmm <= 1700
    # momentum / neutral
    # 15:30 之后到次日 11:30 之前允许 cron 重试
    return hhmm >= 1530 or hhmm <= 1130


def apply_push_template_to_res(res: Dict[str, Any]) -> Dict[str, Any]:
    """
    给 res 注入推送模板与时机信息（含 should_push_now 兜底字段）。
    """
    try:
        if not isinstance(res, dict):
            return res
        track = detect_track(res)
        hint = build_push_hint(res, track)
        res["push_template"] = hint
        res["push_track"] = track
        # 写入 should_push_now 字段（推送层据此决定是否立即推送）
        try:
            res["should_push_now"] = should_push_now(track)
        except Exception:
            res["should_push_now"] = True
        # 调整出场模板（动量用 medium / 反转用 wide）
        if not res.get("exit_template"):
            from core.exit_strategy import pick_exit_template
            res["exit_template"] = (
                "wide" if track == "reversal"
                else "medium" if track == "momentum"
                else pick_exit_template(res)
            )
    except Exception as e:
        logger.debug("[push_template] 注入失败: %s", e)
    return res


def render_track_hint(res: Dict[str, Any]) -> str:
    """生成推送文案中的"动量/反转"标记段"""
    pt = res.get("push_template") if isinstance(res, dict) else None
    if not pt:
        return ""
    return f"\n{pt.get('hint_emoji','')}{pt.get('hint_title','')}：{pt.get('hint_body','')}\n⏰{pt.get('push_time_hint','')}"
