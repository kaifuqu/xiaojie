# -*- coding: utf-8 -*-
"""
core/scan/__init__.py
V26.88 拆分后的统一入口。

【对外 API】（保持与原 core/scan_engine.py 完全一致的下划线访问）
- run_scan_engine              主扫描入口
- capture_intraday_snapshots   日内快照捕获
- get_intraday_snapshot_status 日内快照状态
- get_realtime_sector_ranking  实时板块排名
- save_p3_first_trigger_snapshot P3 首次触发落库
- save_signal_log              信号日志落库
- evaluate_3layer_risk         三层风控（来自 utils）
- DEFAULT_RISK_CONFIG          三层风控配置（来自 utils）
- DANGER_BUY_VWAP_MIN_DAY_PCT  钓鱼线熔断阈值

【私有 API】（仍 re-export 以兼容可能存在的测试 / 探针引用）
- _p1_scan_min_circ_mv_yi, _safe_float, _a_share_elapsed_minutes
- _golden_evaluate, golden_engine
- _evaluate_personality, _calc_safety_factor, _calc_decay_factor_atr, _calc_position
- _calc_defense_line, _calc_crowding_penalty, _p3_right_side_guard
- _execution_tier_for_pool, _infer_golden_gate_reason, _build_priority_tags
- _add_to_blacklist, _load_breakout_vwap_eps_for_danger_buy, _danger_buy_vwap_fish_line_trigger
- _format_p4_trade_language, _p3_hit_tier, _format_p3_trade_layers
- _INTRADAY_SLOT_IDS, _normalize_intraday_slot_id, _intraday_write_slot_to_stock_row
- _tail_volume_anchor, _apply_intraday_snapshots_to_rt
- _tier_wash_path, _load_prev_scan_empty_streak, _merge_tier_meta_scan
- _parse_crowding_score, _default_t1_memory_payload, _normalize_t1_pool_payload
- _collect_t1_memory_for_pools, _load_t1_memory_for_scan, _encode_t1_memory_protocol
- _persist_scan_attribution_snapshot, _ensure_pool_table_row_contract
- _rollback_scan_funnel_for_skipped_stock, _scan_build_industry_pe_stats_for_p1
- _sanitize_rt_vol_ratio, _intraday_ma5_vol_baseline_hand, _extract_scan_surface_metrics
- _safe_notify_system_alert
- _prev_calendar_bizday_yyyymmdd, _expected_latest_daily_anchor_for_intraday
- _try_auto_sync_recent_days_for_scan

【向后兼容】
- 仍允许 from core.scan_engine import run_scan_engine（通过下方的兼容 shim）
"""
# === 公开 API ===
from core.scan.pipeline import run_scan_engine
from core.scan.intraday_snapshots import (
    capture_intraday_snapshots,
    get_intraday_snapshot_status,
    save_p3_first_trigger_snapshot,
    save_signal_log,
    _INTRADAY_SLOT_IDS,
    _normalize_intraday_slot_id,
    _intraday_write_slot_to_stock_row,
    _tail_volume_anchor,
    _apply_intraday_snapshots_to_rt,
    _format_p4_trade_language,
    _p3_hit_tier,
    _format_p3_trade_layers,
)
from core.scan.detectors_market import (
    get_realtime_sector_ranking,
    _evaluate_personality,
    _calc_safety_factor,
    _calc_decay_factor_atr,
    _calc_position,
    _calc_defense_line,
    _calc_crowding_penalty,
    _p3_right_side_guard,
    _execution_tier_for_pool,
    _infer_golden_gate_reason,
    _build_priority_tags,
)
from core.scan.detectors_pattern import (
    DANGER_BUY_VWAP_MIN_DAY_PCT,
    _add_to_blacklist,
    _load_breakout_vwap_eps_for_danger_buy,
    _danger_buy_vwap_fish_line_trigger,
    _danger_buy_estimate_vwap_from_rt,
)
from core.scan.t1_memory import (
    _tier_wash_path,
    _load_prev_scan_empty_streak,
    _merge_tier_meta_scan,
    _parse_crowding_score,
    _default_t1_memory_payload,
    _normalize_t1_pool_payload,
    _collect_t1_memory_for_pools,
    _load_t1_memory_for_scan,
    _encode_t1_memory_protocol,
    _persist_scan_attribution_snapshot,
    _ensure_pool_table_row_contract,
    _rollback_scan_funnel_for_skipped_stock,
    _scan_build_industry_pe_stats_for_p1,
    T1_MEMORY_SCHEMA_VERSION,
)
from core.scan.utils import (
    BJ_TZ,
    PROJECT_ROOT,
    _p1_scan_min_circ_mv_yi,
    _safe_float,
    _a_share_elapsed_minutes,
    _golden_evaluate,
    golden_engine,
    p2_engine,
    p3_engine,
    p4_engine,
    p5_engine,
    _sanitize_rt_vol_ratio,
    _intraday_ma5_vol_baseline_hand,
    _extract_scan_surface_metrics,
    _safe_notify_system_alert,
    _prev_calendar_bizday_yyyymmdd,
    _expected_latest_daily_anchor_for_intraday,
    _try_auto_sync_recent_days_for_scan,
    _route_scored_pool_row,
    evaluate_3layer_risk,
    DEFAULT_RISK_CONFIG,
    get_project_root,
    ensure_runtime_data_layout,
    path_blacklist_json,
    path_intraday_snapshots_json,
    path_wash_metrics_json,
    compute_p1_multi_dim_smooth_score,
    percentile_ranks,
    compress_surge_bonus,
    dampen_burst_by_extremes,
    burst_soft_cap,
    build_rank_lookup,
    neutral_ranks,
    personality_liquidity_blend,
    infer_turnover_rate_f_pct,
    effective_turnover_rate_f,
    series_effective_turnover_f_daily,
    compute_market_contraction_context,
    adaptive_relaxed_golden_gate_ok,
    precompute_indicators,
    merge_daily_with_realtime,
    strict_golden_burst_ok,
    would_trigger_danger_sell,
    normalize_stock_display_name,
)

__all__ = [
    # 公开 API
    "run_scan_engine",
    "capture_intraday_snapshots",
    "get_intraday_snapshot_status",
    "save_p3_first_trigger_snapshot",
    "save_signal_log",
    "get_realtime_sector_ranking",
    # 常量
    "BJ_TZ",
    "PROJECT_ROOT",
    "DANGER_BUY_VWAP_MIN_DAY_PCT",
    "T1_MEMORY_SCHEMA_VERSION",
    "DEFAULT_RISK_CONFIG",
    # 引擎实例
    "p2_engine", "p3_engine", "p4_engine", "p5_engine",
    "golden_engine", "evaluate_3layer_risk",
    # 路径
    "get_project_root", "ensure_runtime_data_layout",
    "path_blacklist_json", "path_intraday_snapshots_json", "path_wash_metrics_json",
    # 算法占位 / fallback
    "compute_p1_multi_dim_smooth_score", "percentile_ranks",
    "compress_surge_bonus", "dampen_burst_by_extremes", "burst_soft_cap",
    "build_rank_lookup", "neutral_ranks", "personality_liquidity_blend",
    "infer_turnover_rate_f_pct", "effective_turnover_rate_f",
    "series_effective_turnover_f_daily", "compute_market_contraction_context",
    "adaptive_relaxed_golden_gate_ok",
    "precompute_indicators", "merge_daily_with_realtime", "strict_golden_burst_ok",
    "would_trigger_danger_sell", "normalize_stock_display_name",
    # 工具
    "_p1_scan_min_circ_mv_yi", "_safe_float", "_a_share_elapsed_minutes",
    "_golden_evaluate", "_sanitize_rt_vol_ratio",
    "_intraday_ma5_vol_baseline_hand", "_extract_scan_surface_metrics",
    "_safe_notify_system_alert", "_prev_calendar_bizday_yyyymmdd",
    "_expected_latest_daily_anchor_for_intraday", "_try_auto_sync_recent_days_for_scan",
    "_route_scored_pool_row",
    # 探测器 / 市场
    "_evaluate_personality", "_calc_safety_factor", "_calc_decay_factor_atr",
    "_calc_position", "_calc_defense_line", "_calc_crowding_penalty",
    "_p3_right_side_guard", "_execution_tier_for_pool",
    "_infer_golden_gate_reason", "_build_priority_tags",
    # 钓鱼线 / 黑名单
    "_add_to_blacklist", "_load_breakout_vwap_eps_for_danger_buy",
    "_danger_buy_vwap_fish_line_trigger", "_danger_buy_estimate_vwap_from_rt",
    # 日内
    "_INTRADAY_SLOT_IDS", "_normalize_intraday_slot_id",
    "_intraday_write_slot_to_stock_row", "_tail_volume_anchor",
    "_apply_intraday_snapshots_to_rt",
    "_format_p4_trade_language", "_p3_hit_tier", "_format_p3_trade_layers",
    # T+1 记忆
    "_tier_wash_path", "_load_prev_scan_empty_streak", "_merge_tier_meta_scan",
    "_parse_crowding_score", "_default_t1_memory_payload",
    "_normalize_t1_pool_payload", "_collect_t1_memory_for_pools",
    "_load_t1_memory_for_scan", "_encode_t1_memory_protocol",
    "_persist_scan_attribution_snapshot", "_ensure_pool_table_row_contract",
    "_rollback_scan_funnel_for_skipped_stock",
    "_scan_build_industry_pe_stats_for_p1",
]