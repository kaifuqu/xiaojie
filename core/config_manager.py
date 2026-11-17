# -*- coding: utf-8 -*-
"""
小杰AI选股系统 Pro V26.88 — 策略参数统一入口：从项目根 config.yaml 的 strategies: 读取，
并合并 Streamlit 会话中的「策略实验室」覆写（无需重启）。

说明：
- 非 UI 环境（CLI/定时任务）下实验室覆写为空 dict，行为与仅 YAML 一致。
- 每次 get_* 会检查 config.yaml 的 mtime，文件被外部修改后自动重载。
- 根节点 ``risk_control:`` 供三层风控 ``RiskControlConfig`` 使用（见 ``get_risk_control_config``）。
"""
from __future__ import annotations

import copy
import logging
import math
import os
import threading
from dataclasses import MISSING, fields, is_dataclass, replace
from typing import Any, Dict, List, Optional, Type, TypeVar

import yaml

logger = logging.getLogger(__name__)

_LOCK = threading.RLock()
_RAW_CACHE: Dict[str, Any] = {}  # path -> {"mtime": float, "data": dict}

T = TypeVar("T")


def _project_root() -> str:
    d = os.path.dirname(os.path.abspath(__file__))
    for _ in range(5):
        if os.path.exists(os.path.join(d, "config.yaml")):
            return d
        d = os.path.dirname(d)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _config_path() -> str:
    return os.path.join(_project_root(), "config.yaml")


def _load_yaml_raw(force: bool = False) -> dict:
    """仅当 config.yaml 的 mtime 变化（或 force）时读盘并解析；否则返回缓存深拷贝。"""
    path = _config_path()
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return {}
    with _LOCK:
        if (
            not force
            and _RAW_CACHE.get("path") == path
            and _RAW_CACHE.get("mtime") == mtime
            and isinstance(_RAW_CACHE.get("data"), dict)
        ):
            return copy.deepcopy(_RAW_CACHE["data"])
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        except Exception as e:
            logger.warning("读取 config.yaml 失败: %s", e)
            data = {}
        _RAW_CACHE["path"] = path
        _RAW_CACHE["mtime"] = mtime
        _RAW_CACHE["data"] = data
        return copy.deepcopy(data)


def get_strategies_dict(force_reload: bool = False) -> dict:
    raw = _load_yaml_raw(force=force_reload)
    s = raw.get("strategies")
    return s if isinstance(s, dict) else {}


def invalidate_config_cache() -> None:
    """实验室写入 YAML 或需强制刷新时调用。"""
    with _LOCK:
        _RAW_CACHE.clear()


def _lab_root() -> dict:
    if os.environ.get("XIAOJIE_DAEMON_MODE", "").strip().lower() in ("1", "true", "yes", "on"):
        return {}
    try:
        import streamlit as st

        root = st.session_state.get("strategy_lab_overrides")
        return dict(root) if isinstance(root, dict) else {}
    except Exception:
        return {}


def _lab_for(pool_key: str) -> dict:
    d = _lab_root().get(pool_key)
    return dict(d) if isinstance(d, dict) else {}


def deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in (override or {}).items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _field_default(f) -> Any:
    if getattr(f, "default_factory", MISSING) is not MISSING:
        return f.default_factory()
    if getattr(f, "default", MISSING) is not MISSING:
        return f.default
    return None


def _coerce_for_field(name: str, val: Any, annotation: Any) -> Any:
    if val is None:
        return None
    ann = annotation
    if ann is int or getattr(ann, "__name__", "") == "int":
        try:
            return int(round(float(val)))
        except (TypeError, ValueError):
            return int(val)
    if ann is float or getattr(ann, "__name__", "") == "float":
        return float(val)
    if ann is bool or getattr(ann, "__name__", "") == "bool":
        return bool(val)
    return val


def dataclass_from_merged(cls: Type[T], yaml_flat: dict, lab_flat: dict) -> T:
    if not is_dataclass(cls):
        raise TypeError(f"{cls} is not a dataclass")
    y = {k: v for k, v in (yaml_flat or {}).items() if v is not None}
    lab = lab_flat or {}
    kwargs: Dict[str, Any] = {}
    for f in fields(cls):
        if f.name in lab:
            raw = lab[f.name]
        elif f.name in y:
            raw = y[f.name]
        else:
            kwargs[f.name] = _field_default(f)
            continue
        kwargs[f.name] = _coerce_for_field(f.name, raw, f.type)
    return cls(**kwargs)


# 策略 YAML / 策略实验室会话中旧键 → 当前 dataclass 字段（新键缺省时拷贝数值，不删旧键以免干扰排查）
_STRATEGY_YAML_ALIASES: Dict[str, Dict[str, str]] = {
    "p2": {"s4_net_elg_ratio_of_float_mv": "s4_net_main_ratio_of_float_mv"},
    "p3": {
        "s4_net_elg_ratio_of_float_mv": "s4_net_main_ratio_of_float_mv",
        "s2_ma10_touch_ratio": "s2_ma20_touch_ratio",
        "s5_avg_cost_mult": "s5_cost50_mult",
    },
}


def _apply_strategy_flat_aliases(flat: Any, pool_key: str) -> dict:
    if not isinstance(flat, dict):
        return {}
    aliases = _STRATEGY_YAML_ALIASES.get(pool_key, {})
    out = dict(flat)
    for old_k, new_k in aliases.items():
        if old_k in out and new_k not in out:
            out[new_k] = out[old_k]
    return out


def _profile_key_for_regime(regime_name: Optional[str]) -> str:
    name = str(regime_name or "").strip()
    if any(k in name for k in ["主升", "趋势"]):
        return "strict"
    if any(k in name for k in ["退潮", "空头", "主跌"]):
        return "relaxed"
    return "neutral"


def get_p1_profiles_merged(regime_name: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    """返回三档 profile（已合并 YAML + 实验室对 profiles 子树的覆写）。"""
    s = get_strategies_dict()
    p1 = s.get("p1") if isinstance(s.get("p1"), dict) else {}
    profiles = p1.get("profiles") if isinstance(p1.get("profiles"), dict) else {}
    lab_p1 = _lab_for("p1")
    if isinstance(lab_p1.get("profiles"), dict):
        profiles = deep_merge(profiles, lab_p1["profiles"])
    return {
        "strict": dict(profiles.get("strict") or {}),
        "neutral": dict(profiles.get("neutral") or {}),
        "relaxed": dict(profiles.get("relaxed") or {}),
        "_active_key": _profile_key_for_regime(regime_name),
    }


def _p1_prof_to_threshold_dict(prof: dict) -> dict:
    return {
        "trend_ma120_min_ratio": float(prof.get("trend_ma120_min_ratio", 0.98)),
        "trend_slope_fastpass": float(prof.get("trend_slope_fastpass", 0.25)),
        "near_ma20_min_ratio": float(prof.get("near_ma20_min_ratio", 0.985)),
        # 【F3-1】MA20 之上前闸可配置化（原为 3 日窗口中站上 2 日的硬编码）。
        # lookback / above_days 均带默认值，旧配置缺失时行为与 F3-1 之前完全一致。
        "ma20_pregate_lookback": int(prof.get("ma20_pregate_lookback", 3)),
        "ma20_pregate_above_days": int(prof.get("ma20_pregate_above_days", 2)),
        "macd_bar_kill": float(prof.get("macd_bar_kill", -0.13)),
        # 【V31 批次 B-1】MACD 柱相对股价百分比阈值（按容错档 0/1/2 索引）。
        # 缺失或格式非法时置 None，由 single_stock.py 自动退回旧的绝对值口径。
        "macd_bar_kill_pct": _p1_parse_macd_bar_kill_pct(prof.get("macd_bar_kill_pct")),
        "vol_divergence_ratio": float(prof.get("vol_divergence_ratio", 0.85)),
        "pass_line": float(prof.get("pass_line", 50.0)),
    }


def _p1_parse_macd_bar_kill_pct(raw) -> Optional[list]:
    """解析 macd_bar_kill_pct：[tier0, tier1, tier2] 三档百分比阈值。

    只接受长度 ≥3 的数值序列（可多给，取前三项），按「越负越宽松」校验单调性；
    任一步失败返回 None，让调用方退回旧绝对值口径，绝不抛异常打断 P1 主链路。
    """
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return None
    try:
        vals = [float(v) for v in list(raw)]
    except (TypeError, ValueError):
        return None
    if len(vals) < 3:
        return None
    vals = vals[:3]
    if not all(v < 0.0 for v in vals):
        return None
    # 单调性要求：tier0 最严、tier2 最宽 → 数值应单调不增
    if not (vals[0] >= vals[1] >= vals[2]):
        return None
    return vals


def get_p1_regime_thresholds(regime_name: Optional[str] = None) -> dict:
    """与 pool_manager._get_regime_thresholds 输出结构一致。"""
    merged = get_p1_profiles_merged(regime_name=regime_name)
    key = merged.pop("_active_key", "neutral")
    prof = merged.get(key) or merged.get("neutral") or {}
    return _p1_prof_to_threshold_dict(prof)


def get_strategy_min_circ_mv_wan() -> int:
    """
    P1~P5 候选 SQL 与安检共用流通下限（万元；2026-09-26 起为 120 亿）。
    优先级：策略实验室 p1.select_min_circ_mv_wan > config.yaml strategies.p1 > constants.STRATEGY_MIN_CIRC_MV_WAN。
    """
    import constants as c

    default = int(getattr(c, "STRATEGY_MIN_CIRC_MV_WAN", 1_200_000))
    s = get_strategies_dict()
    p1 = s.get("p1") if isinstance(s.get("p1"), dict) else {}
    lab = _lab_for("p1")
    v = lab.get("select_min_circ_mv_wan")
    if v is None:
        v = p1.get("select_min_circ_mv_wan")
    if v is None:
        return default
    try:
        return max(0, int(float(v)))
    except (TypeError, ValueError):
        return default


# 【V26.88+ 9-26】保留旧函数名作为兼容 shim，新代码应使用 get_strategy_min_circ_mv_wan。
get_p1_select_min_circ_mv_wan = get_strategy_min_circ_mv_wan


def get_p1_respect_scan_blacklist() -> bool:
    """
    是否在 P1 洗盘中拦截 scan_engine 写入的 danger 黑名单（blacklist.json）。
    默认 False：黑名单仍用于扫描链路禁买/记录，但不因一次误判 danger 把标的永久踢出 P1 基因池。
    设为 True 可恢复「冷却期内 P1 也拦截」的旧行为。
    """
    s = get_strategies_dict()
    p1 = s.get("p1") if isinstance(s.get("p1"), dict) else {}
    lab = _lab_for("p1")
    v = lab.get("respect_scan_blacklist_for_p1")
    if v is None:
        v = p1.get("respect_scan_blacklist_for_p1")
    if v is None:
        return False
    if isinstance(v, bool):
        return v
    s2 = str(v).strip().lower()
    return s2 in ("1", "true", "yes", "on")


def get_p1_use_tushare_unlock_blacklist() -> bool:
    """
    是否启用 Tushare share_float 拉取「未来约 30 天解禁」并在 P1 安检中拦截。
    默认 False：不调接口、不拦截，避免专线/代理超时或权限问题拖慢洗盘。
    需要该风控时改为 true（并确保 Tushare 可连通）。
    """
    s = get_strategies_dict()
    p1 = s.get("p1") if isinstance(s.get("p1"), dict) else {}
    lab = _lab_for("p1")
    v = lab.get("use_tushare_unlock_blacklist_for_p1")
    if v is None:
        v = p1.get("use_tushare_unlock_blacklist_for_p1")
    if v is None:
        return False
    if isinstance(v, bool):
        return v
    s2 = str(v).strip().lower()
    return s2 in ("1", "true", "yes", "on")


def get_p1_fund_memory_weight() -> float:
    """
    多维分项主分与 fund_memory_score（0~200 映射到 0~100）凸组合权重 w；最终分为 (1-w)*分项主分 + w*记忆分，再 cap 100。
    0 表示纯分项主分；实验室覆写优先生效。w 裁剪到 [0, 0.30]。
    """
    import constants as c

    default = float(getattr(c, "FUND_MEMORY_WEIGHT_P1", 0.10))
    s = get_strategies_dict()
    p1 = s.get("p1") if isinstance(s.get("p1"), dict) else {}
    lab = _lab_for("p1")
    v = lab.get("fund_memory_weight_p1")
    if v is None:
        v = p1.get("fund_memory_weight_p1")
    if v is None:
        w = default
    else:
        try:
            w = float(v)
        except (TypeError, ValueError):
            w = default
    return float(max(0.0, min(0.30, w)))


def get_p1_combo_multiplier_config() -> Dict[str, float]:
    """读取 P1 组合特征乘数参数，便于实盘从 config.yaml 微调。"""
    s = get_strategies_dict()
    p1 = s.get("p1") if isinstance(s.get("p1"), dict) else {}
    lab = _lab_for("p1")
    base = p1.get("combo_multiplier") if isinstance(p1.get("combo_multiplier"), dict) else {}
    override = lab.get("combo_multiplier") if isinstance(lab.get("combo_multiplier"), dict) else {}
    merged = deep_merge(base, override)

    def _f(key: str, default: float) -> float:
        try:
            v = float(merged.get(key, default))
        except (TypeError, ValueError):
            v = default
        return float(v)

    return {
        "enabled": bool(merged.get("enabled", True)),
        "min": _f("min", 1.00),  # 【V26.88+10-1 P2-4 优化】0.90 → 1.00（避免观察池漂移）
        "max": _f("max", 1.15),  # 【V26.88+10-1 P2-4 优化】1.20 → 1.15（实战中 1.05-1.12 占比最高）
        "top_tier": _f("top_tier", 1.15),  # 【V26.88+10-1 P2-4】1.20 → 1.15
        "second_tier": _f("second_tier", 1.12),  # 【V26.88+10-1 P2-4】1.15 → 1.12
        "third_tier": _f("third_tier", 1.10),  # 【V26.88+10-1 P2-4】1.12 → 1.10
        "industry_dark_flow": _f("industry_dark_flow", 1.08),
        "fund_shape_wakeup": _f("fund_shape_wakeup", 1.18),
        "fund_pulse_confirm": _f("fund_pulse_confirm", 1.10),
        "weak_pattern_discount": _f("weak_pattern_discount", 0.94),
        "bubble_discount": _f("bubble_discount", 0.92),
        "hot_low_fund_discount": _f("hot_low_fund_discount", 0.95),
        "chip_core_min": _f("chip_core_min", 8.5),
        "trend_dist_min": _f("trend_dist_min", 6.0),
        "mat_min": _f("mat_min", 4.5),
        "fund_core_min": _f("fund_core_min", 8.0),
        "momentum_core_min": _f("momentum_core_min", 6.5),
        "golden_core_min": _f("golden_core_min", 8.0),
        "healthy_core_min": _f("healthy_core_min", 2.4),
        "slope_core_min": _f("slope_core_min", 4.2),
        "fund_hot_min": _f("fund_hot_min", 11.0),
        "chip_hot_min": _f("chip_hot_min", 6.0),
        "trend_health_min": _f("trend_health_min", 2.0),
        "industry_bonus_min": _f("industry_bonus_min", 8.0),
        "rank_bonus_min": _f("rank_bonus_min", 2.0),
    }


def get_p1_weekly_config() -> Dict[str, Any]:
    """读取 P1 周线维度（V2）配置：周线趋势 + 周线放量 + 量价组合 + 共振乘数。

    通过 base + lab_overlay 合并；调用方无需关心 lab 子段。
    """
    s = get_strategies_dict()
    p1 = s.get("p1") if isinstance(s.get("p1"), dict) else {}
    lab = _lab_for("p1")
    base = p1.get("weekly") if isinstance(p1.get("weekly"), dict) else {}
    override = lab.get("weekly") if isinstance(lab.get("weekly"), dict) else {}
    merged = deep_merge(base, override)

    def _sub(name: str) -> dict:
        node = merged.get(name)
        return node if isinstance(node, dict) else {}

    def _f(d: dict, key: str, default: float) -> float:
        try:
            v = float(d.get(key, default))
        except (TypeError, ValueError):
            return float(default)
        return float(v)

    trend = _sub("trend")
    volume = _sub("volume")
    combo = _sub("combo_bonus")
    resonance = _sub("resonance")
    safety = _sub("safety")

    return {
        "enabled": bool(merged.get("enabled", True)),
        "trend": {
            "standard_full": _f(trend, "standard_full", 6.0),
            "initial_full": _f(trend, "initial_full", 4.0),
            "ma20_above_shake": _f(trend, "ma20_above_shake", 2.0),
            "danger_flat": _f(trend, "danger_flat", 0.0),
            "below_ma60": _f(trend, "below_ma60", -2.0),
            "short_align": _f(trend, "short_align", -4.0),
            "agg_weight": _f(trend, "agg_weight", 0.6),
        },
        "volume": {
            "warm_up_full": _f(volume, "warm_up_full", 4.0),
            "warm_down": _f(volume, "warm_down", 2.0),
            "flat": _f(volume, "flat", 1.0),
            "surge_up_tired": _f(volume, "surge_up_tired", 1.0),
            "shrink_up": _f(volume, "shrink_up", -2.0),
            "surge_down": _f(volume, "surge_down", -4.0),
            "agg_weight": _f(volume, "agg_weight", 0.5),
        },
        "combo_bonus": {
            "pullback_volume_shrink": _f(combo, "pullback_volume_shrink", 1.0),
            "three_week_surge": _f(combo, "three_week_surge", -1.0),
        },
        "resonance": {
            "trend_min": _f(resonance, "trend_min", 4.5),
            "volume_min": _f(resonance, "volume_min", 2.5),
            "multiplier": _f(resonance, "multiplier", 1.05),
        },
        "safety": {
            "min_history_weeks": int(float(safety.get("min_history_weeks", 30))),
        },
    }


def get_p1_stock_behavior_penalty_config() -> Dict[str, Any]:
    """读取 P1 股性惩罚参数（长上影 / 冲高回落黑名单）。"""
    s = get_strategies_dict()
    p1 = s.get("p1") if isinstance(s.get("p1"), dict) else {}
    lab = _lab_for("p1")
    base = p1.get("stock_behavior_penalty") if isinstance(p1.get("stock_behavior_penalty"), dict) else {}
    override = lab.get("stock_behavior_penalty") if isinstance(lab.get("stock_behavior_penalty"), dict) else {}
    merged = deep_merge(base, override)
    return {
        "enabled": bool(merged.get("enabled", True)),
        "lookback_days": int(float(merged.get("lookback_days", 20))),
        "long_upper_shadow_hits_min": int(float(merged.get("long_upper_shadow_hits_min", 3))),
        "intraday_dump_hits_min": int(float(merged.get("intraday_dump_hits_min", 3))),
        "score_penalty": float(merged.get("score_penalty", 8.0)),
    }


def get_p1_max_pool_size() -> int:
    """【V30.15 2026-11-05】P1 主池数量上限（strategies.p1.max_pool_size）。

    返回 <=0 表示不限制。默认 120：
      · 下界：由 min_score + 120 亿流通市值闸决定，分数达标才进，不再降分凑数；
      · 上界：120 只 —— 目标是封住 P3 每 150 秒扫全池的取数/行情开销
        （原来 30 只，四倍以内仍在可控范围），而不是为了砍掉高分票。
    缺配置时返回 120，与显式配置 120 等价。
    """
    s = get_strategies_dict()
    p1 = s.get("p1") if isinstance(s.get("p1"), dict) else {}
    lab = _lab_for("p1")
    raw = p1.get("max_pool_size", 120)
    if isinstance(lab, dict) and lab.get("max_pool_size") is not None:
        raw = lab.get("max_pool_size")
    try:
        v = int(float(raw))
    except (TypeError, ValueError):
        return 120
    return v


def get_p1_large_cap_bonus_config() -> Dict[str, Any]:
    """【V30.14 2026-11-05】千亿蓝筹加分参数（strategies.p1.large_cap_bonus）。

    缺配置时全部走默认值，与「不启用」相比只多出蓝筹加分，不影响中小盘票。
    """
    s = get_strategies_dict()
    p1 = s.get("p1") if isinstance(s.get("p1"), dict) else {}
    lab = _lab_for("p1")
    base = p1.get("large_cap_bonus") if isinstance(p1.get("large_cap_bonus"), dict) else {}
    override = lab.get("large_cap_bonus") if isinstance(lab.get("large_cap_bonus"), dict) else {}
    merged = deep_merge(base, override)
    return {
        "enabled": bool(merged.get("enabled", True)),
        "min_circ_mv_yi": float(merged.get("min_circ_mv_yi", 1000.0)),
        "base_bonus": float(merged.get("base_bonus", 1.5)),
        "trend_bonus": float(merged.get("trend_bonus", 1.5)),
        "inflow_bonus": float(merged.get("inflow_bonus", 1.0)),
        "inflow_days_min": int(float(merged.get("inflow_days_min", 3))),
        "mild_volume_bonus": float(merged.get("mild_volume_bonus", 1.0)),
        "mild_volume_ratio_min": float(merged.get("mild_volume_ratio_min", 1.1)),
        "mild_volume_ratio_max": float(merged.get("mild_volume_ratio_max", 3.0)),
        "mild_pct_min": float(merged.get("mild_pct_min", 1.0)),
        "mild_pct_max": float(merged.get("mild_pct_max", 7.0)),
        "max_bonus": float(merged.get("max_bonus", 5.0)),
        "turnover_relax_factor": float(merged.get("turnover_relax_factor", 0.75)),
    }


def get_p1_one_day_trip_config() -> Dict[str, Any]:
    """【V30.14 2026-11-05】防一日游参数（strategies.p1.one_day_trip_guard）。"""
    s = get_strategies_dict()
    p1 = s.get("p1") if isinstance(s.get("p1"), dict) else {}
    lab = _lab_for("p1")
    base = p1.get("one_day_trip_guard") if isinstance(p1.get("one_day_trip_guard"), dict) else {}
    override = lab.get("one_day_trip_guard") if isinstance(lab.get("one_day_trip_guard"), dict) else {}
    merged = deep_merge(base, override)
    return {
        "enabled": bool(merged.get("enabled", True)),
        "pump_pct_min": float(merged.get("pump_pct_min", 5.0)),
        "close_position_max": float(merged.get("close_position_max", 0.32)),
        "pullback_from_high_min_pct": float(merged.get("pullback_from_high_min_pct", 3.0)),
        "dump_vol_mult_min": float(merged.get("dump_vol_mult_min", 2.5)),
        "dump_pct_max": float(merged.get("dump_pct_max", -3.0)),
        "high_shadow_ratio_min": float(merged.get("high_shadow_ratio_min", 0.50)),
        "high_shadow_3d_gain_min": float(merged.get("high_shadow_3d_gain_min", 15.0)),
        "high_shadow_penalty": float(merged.get("high_shadow_penalty", 4.0)),
        "plunge_5d_max": float(merged.get("plunge_5d_max", -8.0)),
        "spike_pct_min": float(merged.get("spike_pct_min", 7.0)),
        "plunge_spike_penalty": float(merged.get("plunge_spike_penalty", 3.0)),
        "overheat_5d_pct": float(merged.get("overheat_5d_pct", 30.0)),
        "overheat_close_pos_max": float(merged.get("overheat_close_pos_max", 0.45)),
        "overheat_penalty": float(merged.get("overheat_penalty", 3.0)),
    }


def get_p1_thresholds_for_profile(regime_name: Optional[str], profile_key: str) -> dict:
    """指定 strict/neutral/relaxed 档位的六键阈值（已合并 YAML + 实验室该档 pass_line 等）。"""
    merged = get_p1_profiles_merged(regime_name=regime_name)
    pk = str(profile_key or "").strip() or "neutral"
    prof = dict(merged.get(pk) or merged.get("neutral") or {})
    return _p1_prof_to_threshold_dict(prof)


def get_p1_threshold_profile_label(regime_name: Optional[str] = None) -> str:
    name = str(regime_name or "").strip()
    if any(k in name for k in ["主升", "趋势"]):
        return "趋势市·严格精选"
    if any(k in name for k in ["退潮", "空头", "主跌"]):
        return "退潮/空头·适度放宽"
    return "震荡市·稳健中性"


def get_p2_screener_config() -> Any:
    from core.strategies.p2_auction_screener import P2ScreenerConfig

    s = get_strategies_dict()
    flat = s.get("p2") if isinstance(s.get("p2"), dict) else {}
    flat = _apply_strategy_flat_aliases(flat, "p2")
    lab = _apply_strategy_flat_aliases(_lab_for("p2"), "p2")
    return dataclass_from_merged(P2ScreenerConfig, flat, lab)


def get_p3_intraday_screener_config() -> Any:
    from core.strategies.p3_intraday_screener import P3IntradayScreenerConfig

    s = get_strategies_dict()
    flat = s.get("p3") if isinstance(s.get("p3"), dict) else {}
    flat = _apply_strategy_flat_aliases(flat, "p3")
    lab = _apply_strategy_flat_aliases(_lab_for("p3"), "p3")
    cfg = dataclass_from_merged(P3IntradayScreenerConfig, flat, lab)

    # V26.88+9-22 优化8：当 config.yaml 未显式配置 strategic_industries 时，
    # 自动从 pool_manager.STRATEGIC_INDUSTRIES 注入（默认 None → 真实赛道列表），
    # 使 P3 的战略赛道加成（_p3_mainline_score / p3_global_risk_veto）真正生效。
    if getattr(cfg, "strategic_industries", None) is None:
        try:
            from core.pool_manager import STRATEGIC_INDUSTRIES
            cfg.strategic_industries = list(STRATEGIC_INDUSTRIES.keys())
        except Exception:
            pass  # 静默降级
    elif isinstance(cfg.strategic_industries, list) and all(not str(x).strip() for x in cfg.strategic_industries):
        try:
            from core.pool_manager import STRATEGIC_INDUSTRIES
            cfg.strategic_industries = list(STRATEGIC_INDUSTRIES.keys())
        except Exception:
            pass

    return cfg


def get_p4_tail_screener_config() -> Any:
    from core.strategies.p4_tail_screener import P4TailScreenerConfig

    s = get_strategies_dict()
    flat = s.get("p4") if isinstance(s.get("p4"), dict) else {}
    flat = _apply_strategy_flat_aliases(flat, "p4")
    lab = _apply_strategy_flat_aliases(_lab_for("p4"), "p4")
    cfg = dataclass_from_merged(P4TailScreenerConfig, flat, lab)

    # V26.88+9-22 优化13：当 config.yaml 未显式配置 strategic_industries 时，
    # 自动从 pool_manager.STRATEGIC_INDUSTRIES 注入（默认 None → 真实赛道列表），
    # 使 P4 的战略赛道加成（p4_global_risk_veto）真正生效。
    # 【V26.88+9-23】北向资金评分已下线，仅保留 p4_global_risk_veto。
    if getattr(cfg, "strategic_industries", None) is None:
        try:
            from core.pool_manager import STRATEGIC_INDUSTRIES
            cfg.strategic_industries = list(STRATEGIC_INDUSTRIES.keys())
        except Exception:
            pass  # 静默降级
    elif isinstance(cfg.strategic_industries, list) and all(not str(x).strip() for x in cfg.strategic_industries):
        try:
            from core.pool_manager import STRATEGIC_INDUSTRIES
            cfg.strategic_industries = list(STRATEGIC_INDUSTRIES.keys())
        except Exception:
            pass

    return cfg


def get_p5_postmarket_config() -> Any:
    from core.strategies.p5_postmarket_screener import P5PostmarketConfig

    s = get_strategies_dict()
    flat = s.get("p5") if isinstance(s.get("p5"), dict) else {}
    flat = _apply_strategy_flat_aliases(flat, "p5")
    lab = _apply_strategy_flat_aliases(_lab_for("p5"), "p5")
    cfg = dataclass_from_merged(P5PostmarketConfig, flat, lab)

    # V26.88+9-22 优化18：当 config.yaml 未显式配置 strategic_industries 时，
    # 自动从 pool_manager.STRATEGIC_INDUSTRIES 注入（默认 None → 真实赛道列表），
    # 使 P5 的战略赛道加成（strategic_burst_bonus / strategic_cross_bonus）真正生效。
    if getattr(cfg, "strategic_industries", None) is None:
        try:
            from core.pool_manager import STRATEGIC_INDUSTRIES
            cfg.strategic_industries = list(STRATEGIC_INDUSTRIES.keys())
        except Exception:
            pass  # 静默降级
    elif isinstance(cfg.strategic_industries, list) and all(not str(x).strip() for x in cfg.strategic_industries):
        # 空列表/空白列表 → 同样注入默认
        try:
            from core.pool_manager import STRATEGIC_INDUSTRIES
            cfg.strategic_industries = list(STRATEGIC_INDUSTRIES.keys())
        except Exception:
            pass

    return cfg


def get_golden_config() -> Dict[str, float]:
    s = get_strategies_dict()
    g = s.get("golden_burst") if isinstance(s.get("golden_burst"), dict) else {}
    lab_g = _lab_for("golden_burst")
    merged = {**g, **lab_g}
    return {
        "golden_burst_pct_low": float(merged.get("golden_burst_pct_low", 4.0)),
        "golden_burst_pct_high": float(merged.get("golden_burst_pct_high", 6.0)),
        "golden_burst_vr_low": float(merged.get("golden_burst_vr_low", 1.8)),
        "golden_burst_vr_high": float(merged.get("golden_burst_vr_high", 2.5)),
        "p5_golden_vr_min": float(merged.get("p5_golden_vr_min", 1.2)),
        "p5_golden_pct_low": float(merged.get("p5_golden_pct_low", 2.0)),
        "p5_golden_pct_high": float(merged.get("p5_golden_pct_high", 7.0)),
    }


def sync_scan_engine_strategy_configs() -> None:
    """
    兼容旧调用点（可删）。P2–P5 已在各自 run_all 首行从本模块 get_p* 拉最新配置，
    避免 scan_engine 与长生命周期引擎对象并发写 _cfg。
    """
    return


def _lab_opr_session_overrides_safe() -> Dict[str, Any]:
    """
    策略实验室：从 Streamlit session_state 读取 lab_opr_overrides。
    非 Streamlit 环境、未运行 run、或键类型错误时返回空 dict，绝不抛异常。
    """
    try:
        import streamlit as st  # type: ignore

        if not hasattr(st, "session_state"):
            return {}
        ov = st.session_state.get("lab_opr_overrides")
        if not isinstance(ov, dict):
            return {}
        return dict(ov)
    except Exception:
        return {}


def _observation_pool_relax_clamp_to_valid(out: Dict[str, float], defaults: Dict[str, float]) -> None:
    """就地修正越界项为 defaults（三键语义与 get_observation_pool_relax_settings 一致）。"""
    if not (0.5 < out.get("vr_shrink_gate", 0.0) <= 3.0):
        out["vr_shrink_gate"] = float(defaults["vr_shrink_gate"])
    if out.get("large_cap_yi_min", 0.0) < 10.0 or out.get("large_cap_yi_min", 0.0) > 20000.0:
        out["large_cap_yi_min"] = float(defaults["large_cap_yi_min"])
    if out.get("turnover_floor_pct", 0.0) < 0.05 or out.get("turnover_floor_pct", 0.0) > 5.0:
        out["turnover_floor_pct"] = float(defaults["turnover_floor_pct"])


def get_observation_pool_relax_settings(
    force_reload: bool = False,
    ignore_session_overrides: bool = False,
) -> Dict[str, float]:
    """
    读取 sop_v11.observation_pool_relax（极端缩量期观察池相关阈值）。

    若未传 ignore_session_overrides=True，则在 YAML 有效值之上再 deep_merge
    st.session_state['lab_opr_overrides']（策略实验室滑块）；覆写键类型错误或非有限数则跳过该键，
    越界则保留 YAML 合并后的当前值。最终再跑一次范围钳制。

    防御性：读盘失败、节点缺失时回退内置默认，保证 fund_mv_utils 不因配置损坏崩溃。

    返回固定三键（float）：vr_shrink_gate, large_cap_yi_min, turnover_floor_pct。
    """
    defaults: Dict[str, float] = {
        "vr_shrink_gate": 0.95,
        "large_cap_yi_min": 500.0,
        "turnover_floor_pct": 0.56,
    }
    out = dict(defaults)
    try:
        raw = _load_yaml_raw(force=force_reload)
        sop = raw.get("sop_v11")
        if isinstance(sop, dict):
            blk = sop.get("observation_pool_relax")
            if isinstance(blk, dict):
                for key in defaults:
                    if key not in blk:
                        continue
                    try:
                        f = float(blk[key])
                    except (TypeError, ValueError):
                        continue
                    if not math.isfinite(f):
                        continue
                    out[key] = f
        _observation_pool_relax_clamp_to_valid(out, defaults)
    except Exception as e:
        logger.debug("get_observation_pool_relax_settings YAML 段异常，使用默认: %s", e)
        out = dict(defaults)

    if ignore_session_overrides:
        return dict(out)

    yaml_merged = dict(out)
    sess = _lab_opr_session_overrides_safe()

    def _override_in_range(k: str, v: float) -> bool:
        if k == "vr_shrink_gate":
            return 0.5 < v <= 3.0
        if k == "large_cap_yi_min":
            return 10.0 <= v <= 20000.0
        if k == "turnover_floor_pct":
            return 0.05 <= v <= 5.0
        return False

    for key in defaults:
        if key not in sess:
            continue
        try:
            cand = float(sess[key])
        except (TypeError, ValueError):
            logger.debug(
                "lab_opr_overrides[%s] 类型不可转 float，保留 YAML 值 %.4f",
                key,
                float(yaml_merged[key]),
            )
            continue
        if not math.isfinite(cand):
            logger.debug(
                "lab_opr_overrides[%s] 非有限数，保留 YAML 值 %.4f",
                key,
                float(yaml_merged[key]),
            )
            continue
        if not _override_in_range(key, cand):
            logger.debug(
                "lab_opr_overrides[%s]=%s 超出允许范围，保留 YAML 值 %.4f",
                key,
                cand,
                float(yaml_merged[key]),
            )
            continue
        out[key] = cand

    _observation_pool_relax_clamp_to_valid(out, defaults)
    return out


def get_notification_config(force_reload: bool = False) -> Dict[str, Any]:
    """
    读取根节点 notification:
    {
      enabled,
      wechat_webhook_url,
      wechat_webhook_url_secondary
    }

    - primary：仅用于 p1~p5 选股推送 + 每天 9:18 固定消息
    - secondary：用于维护、下载、报错、任务执行结果等其他系统消息

    兼容逻辑：primary 为空时回退 legacy notify.wechat_webhook；secondary 为空则允许留空。
    """
    raw = _load_yaml_raw(force=force_reload)
    n = raw.get("notification")
    if not isinstance(n, dict):
        n = {}
    legacy = raw.get("notify")
    legacy_wh = ""
    if isinstance(legacy, dict):
        legacy_wh = str(legacy.get("wechat_webhook") or "").strip()
    url = str(n.get("wechat_webhook_url") or "").strip()
    if not url:
        url = legacy_wh
    sec = str(n.get("wechat_webhook_url_secondary") or "").strip()
    if not sec and url:
        sec = url
    return {
        "enabled": bool(n.get("enabled", True)),
        "wechat_webhook_url": url,
        "wechat_webhook_url_secondary": sec,
    }


def get_deepseek_analysis_config(force_reload: bool = False) -> Dict[str, Any]:
    """
    读取根节点 deepseek_analysis：用于 P2/P3/P4/P5 企微推送前追加 AI 分析建议。
    API Key 优先从环境变量 DEEPSEEK_API_KEY 读取，其次读取 YAML，避免核心逻辑硬编码。
    【V26.88+ 9-19】默认切到 V4.1-Flash (deepseek-flash)；旧名 deepseek-chat 兼容映射到 deepseek-flash。
    """
    raw = _load_yaml_raw(force=force_reload)
    cfg = raw.get("deepseek_analysis")
    if not isinstance(cfg, dict):
        cfg = {}
    api_key = str(os.environ.get("DEEPSEEK_API_KEY") or cfg.get("api_key") or "").strip()
    base_url = str(cfg.get("base_url") or cfg.get("api_url") or "https://api.deepseek.com/chat/completions").strip()
    model = str(cfg.get("model") or "deepseek-flash").strip()
    # [V26.88+10-9 按官方文档修正] 仅支持 deepseek-flash / deepseek-v4-pro，旧名不再做兼容映射
    # 旧名 deepseek-chat / deepseek-v4-flash / deepseek-reasoner 等已从白名单移除
    thinking_enabled = bool(cfg.get("thinking_enabled", False))
    reasoning_effort = str(cfg.get("reasoning_effort") or "low").strip().lower()
    # [V26.88+10-9 修正] 官方 thinking 模式仅支持 reasoning_effort ∈ {low, high, max}
    # 旧映射 "xhigh/maximum → max" 与官方文档 "xhigh→high" 不一致, 现按官方修正
    # 参考: https://api-docs.deepseek.com/guides/thinking_mode
    _EFFORT_LEGACY_MAP = {"xhigh": "high", "maximum": "max", "ultra": "max", "medium": "high", "minimal": "low"}
    if reasoning_effort in _EFFORT_LEGACY_MAP:
        reasoning_effort = _EFFORT_LEGACY_MAP[reasoning_effort]
    if reasoning_effort not in {"low", "high", "max"}:
        reasoning_effort = "low"
    try:
        timeout_seconds = float(cfg.get("timeout_seconds", 8.0))
    except (TypeError, ValueError):
        timeout_seconds = 8.0
    timeout_seconds = max(1.0, min(timeout_seconds, 30.0))
    try:
        max_tokens = int(cfg.get("max_tokens", 320))
    except (TypeError, ValueError):
        max_tokens = 320
    fallback_enabled = bool(cfg.get("fallback_enabled", True))
    fallback_model = str(cfg.get("fallback_model") or "deepseek-flash").strip()
    if fallback_model == model:
        fallback_model = ""  # 避免 fallback == 主模型时无意义切换
    # 【V26.88+ 9-19】prompt_cache_enabled：V4.1-Flash 协议文档暂未明文列出 prompt_cache_key；
    # 默认 false 保守，等官方确认后再开。YAML 显式 true 也尊重用户选择。
    prompt_cache_enabled = bool(cfg.get("prompt_cache_enabled", False))
    return {
        "enabled": bool(cfg.get("enabled", False)),
        "api_key": api_key,
        "base_url": base_url,
        "model": model,
        "thinking_enabled": thinking_enabled,
        "reasoning_effort": reasoning_effort,
        "timeout_seconds": timeout_seconds,
        "max_tokens": max(80, min(max_tokens, 600)),
        "fallback_enabled": fallback_enabled,
        "fallback_model": fallback_model,
        "prompt_cache_enabled": prompt_cache_enabled,
    }


def get_daemon_alert_silence_config(force_reload: bool = False) -> Dict[str, Any]:
    """
    读取根节点 daemon_alert_silence: { enabled, whitelist_keywords, blacklist_categories, silence_keywords }。
    - whitelist_keywords 命中时优先放行
    - blacklist_categories 精确类别静默
    - silence_keywords 为 title/detail 子串静默
    """
    raw = _load_yaml_raw(force=force_reload)
    blk = raw.get("daemon_alert_silence")
    if not isinstance(blk, dict):
        blk = {}
    def _norm_list(v: Any) -> List[str]:
        if not isinstance(v, list):
            return []
        out: List[str] = []
        for x in v:
            s = str(x or "").strip()
            if s:
                out.append(s)
        return out
    return {
        "enabled": bool(blk.get("enabled", True)),
        "whitelist_keywords": _norm_list(blk.get("whitelist_keywords")),
        "blacklist_categories": _norm_list(blk.get("blacklist_categories")),
        "silence_keywords": _norm_list(blk.get("silence_keywords")),
    }


def get_ui_alert_only(force_reload: bool = False) -> bool:
    """
    读取 config.yaml 根节点 ``risk_control.ui_alert_only``。

    为 True：纯 UI 预警模式（``RiskControlConfig.ui_alert_only=True``），一层/二层红线以标签提示为主，
    不拦截战法命中，便于极端行情下仅改配置即可切换「防守」强度。

    未配置 ``risk_control`` 或未写 ``ui_alert_only`` 时默认为 True，与引擎 dataclass 默认值一致。
    """
    raw = _load_yaml_raw(force=force_reload)
    rc = raw.get("risk_control")
    if not isinstance(rc, dict):
        return True
    v = rc.get("ui_alert_only")
    if v is None:
        return True
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    return s in ("1", "true", "yes", "on", "y")


def get_ui_alert_only_flag(force_reload: bool = False) -> bool:
    """与 :func:`get_ui_alert_only` 同义，便于与外部接口命名对齐。"""
    return get_ui_alert_only(force_reload=force_reload)


def get_config_yaml_path() -> str:
    """项目根目录 ``config.yaml`` 绝对路径（供 UI 比对 mtime）。"""
    return _config_path()


def write_ui_alert_only_to_config_yaml(value: bool) -> bool:
    """
    将 ``risk_control.ui_alert_only`` 写回 config.yaml。

    实现为**仅替换**首个匹配行 ``ui_alert_only: ...``（保留文件其余内容、注释与键顺序），
    不使用整文件 ``yaml.safe_dump``，避免冲掉用户手工注释。

    若文件中不存在 ``ui_alert_only:`` 行但存在根键 ``risk_control:``，则在该行下一行插入一行
    ``  ui_alert_only: true|false``。

    成功后调用 :func:`invalidate_config_cache`。
    """
    path = _config_path()
    val = "true" if bool(value) else "false"
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError as e:
        logger.warning("write_ui_alert_only_to_config_yaml 读盘失败: %s", e)
        return False

    replaced = False
    out: List[str] = []
    for line in lines:
        if (not replaced) and line.lstrip().startswith("ui_alert_only:"):
            indent = line[: len(line) - len(line.lstrip())]
            out.append(f"{indent}ui_alert_only: {val}\n")
            replaced = True
        else:
            out.append(line)

    if not replaced:
        out2: List[str] = []
        inserted = False
        for line in lines:
            out2.append(line)
            if (not inserted) and line.strip().startswith("risk_control:"):
                out2.append(f"  ui_alert_only: {val}\n")
                inserted = True
        if not inserted:
            logger.warning("write_ui_alert_only_to_config_yaml: 未找到 ui_alert_only 行且未找到 risk_control 锚点")
            return False
        out = out2

    try:
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(out)
    except OSError as e:
        logger.warning("write_ui_alert_only_to_config_yaml 写盘失败: %s", e)
        return False

    # 用 yaml 安全解析回读，确认磁盘状态与写入意图一致（不整文件 dump，仅校验）
    try:
        with open(path, "r", encoding="utf-8") as f:
            chk = yaml.safe_load(f) or {}
        rc = chk.get("risk_control") if isinstance(chk, dict) else None
        if isinstance(rc, dict):
            g = rc.get("ui_alert_only")
            g_ok = g if isinstance(g, bool) else str(g).strip().lower() in ("true", "1", "yes", "on")
            if bool(g_ok) != bool(value):
                logger.warning(
                    "write_ui_alert_only_to_config_yaml: YAML 校验与目标不一致 | 文件=%s 期望=%s",
                    g_ok,
                    value,
                )
    except Exception as e:
        logger.debug("write_ui_alert_only_to_config_yaml 校验跳过: %s", e)

    invalidate_config_cache()
    logger.info("config.yaml risk_control.ui_alert_only 已设为 %s", val)
    return True


def get_risk_control_config(force_reload: bool = False):
    """
    返回与 config.yaml 的 ``risk_control`` 节点对齐的 ``RiskControlConfig`` 实例。

    当前仅将 YAML 中的 ``ui_alert_only`` 覆写到引擎配置；其余阈值与
    ``core.strategies.risk_control_engine.DEFAULT_RISK_CONFIG`` 保持一致。
    回测或单测若需完全自定义，可仍向 P2–P5 构造函数传入 ``risk_cfg=...`` 以绕过本函数。

    参数 force_reload 为 True 时强制重新读盘（忽略 mtime 缓存），一般不必使用。
    """
    from core.strategies.risk_control_engine import DEFAULT_RISK_CONFIG, RiskControlConfig

    ui = get_ui_alert_only(force_reload=force_reload)
    out: RiskControlConfig = replace(DEFAULT_RISK_CONFIG, ui_alert_only=ui)
    logger.debug("risk_control 已加载: ui_alert_only=%s", ui)
    return out


def get_signal_filter_config(force_reload: bool = False) -> dict:
    """读取 config.yaml 的 ``signal_filter`` 节点（多层信号过滤器接入配置）。

    【P0-1 2026-11-04】core/signal_filter.py 的五层过滤器此前从未被扫描主链路调用，
    属于「写了但没接线」的死代码。为安全上线，改由本配置控制接入方式：

      enabled: false      # 总开关；false 时完全不参与（默认）
      mode: shadow        # disabled / shadow（只标注不拦）/ enforce（真拦截）
      thresholds: {}      # 可选阈值覆盖（key 见 core/signal_filter.py FILTER_THRESHOLDS）

    默认 enabled=false，即**不改任何现有行为**。等 shadow 模式跑出足够样本、
    确认不会误杀（reject 率合理、被拒标的后续表现确实更差）后，再切 enforce。

    解析失败一律回落 {"enabled": False, "mode": "disabled"} —— 宁可不用，也不误杀。

    【2026-11-04】新增透传 thresholds（可选），非 dict 或非法值一律忽略。
    """
    default = {"enabled": False, "mode": "disabled"}
    try:
        raw = _load_yaml_raw(force=force_reload)
    except Exception:  # noqa: BLE001
        return dict(default)
    node = raw.get("signal_filter")
    if not isinstance(node, dict):
        return dict(default)

    enabled = node.get("enabled", False)
    if not isinstance(enabled, bool):
        enabled = str(enabled).strip().lower() in ("1", "true", "yes", "on", "y")

    mode = str(node.get("mode") or "disabled").strip().lower()
    if mode not in ("disabled", "shadow", "enforce"):
        mode = "disabled"
    if not enabled:
        mode = "disabled"

    # 【2026-11-04 新增】透传 thresholds（可选阈值覆盖）。
    #   解析失败/非 dict 时忽略 → 适配层会沿用 signal_filter.py 里的默认阈值。
    #   注意：即使 enabled=false 也要透传，因为 shadow 期可能想单独调阈值观察，
    #   而 _resolve_mode 会因 enabled=false 保持 disabled（不会真的启用）。
    out = {"enabled": bool(enabled), "mode": mode}
    raw_th = node.get("thresholds")
    if isinstance(raw_th, dict) and raw_th:
        clean = {}
        for k, v in raw_th.items():
            if not isinstance(k, str):
                continue
            try:
                clean[k] = float(v)
            except (TypeError, ValueError):
                continue
        if clean:
            out["thresholds"] = clean
    return out


# 避免 pool_manager 与 config_manager 循环导入：标签函数在 get_p1_threshold_profile_label 内懒加载
