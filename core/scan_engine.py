# -*- coding: utf-8 -*-
"""
core/scan_engine.py
V26.88 拆分后的兼容性 shim。原 3545 行单文件已迁移到 core/scan/ 下的 6 个子模块：

  - core/scan/utils.py                  L1-L530   全局物理底座 + 容错 import + 工具
  - core/scan/detectors_pattern.py      L531-L619 黑名单 + danger_buy 钓鱼线熔断
  - core/scan/detectors_market.py       L620-L1154 性格评估 + 核心探测器 + 板块排名
  - core/scan/intraday_snapshots.py     L1182-L1664 日内快照 + P3 落库 + 信号日志
  - core/scan/t1_memory.py              L1665-L2159 多层级池子 + T+1 记忆 + 归因
  - core/scan/pipeline.py               L2162-L3545 主中枢调度 + run_scan_engine

本文件仅做 import 转发，保持原 from core.scan_engine import ... 的所有调用点不动。

【对外 API 完全保留】
- run_scan_engine, capture_intraday_snapshots, get_intraday_snapshot_status
- get_realtime_sector_ranking, save_p3_first_trigger_snapshot, save_signal_log
- fetch_realtime_batch, load_p1_cache（兼容 re-export：原 scan_engine 上层习惯直接拿这两个）
- 所有 _ 开头的私有工具与常量
"""
from core.scan import (  # noqa: F401,F403
    # 公开 API
    run_scan_engine,
    capture_intraday_snapshots,
    get_intraday_snapshot_status,
    save_p3_first_trigger_snapshot,
    save_signal_log,
    get_realtime_sector_ranking,
    # 常量
    BJ_TZ, PROJECT_ROOT, DANGER_BUY_VWAP_MIN_DAY_PCT, T1_MEMORY_SCHEMA_VERSION,
    DEFAULT_RISK_CONFIG,
    # 引擎实例
    p2_engine, p3_engine, p4_engine, p5_engine,
    golden_engine, evaluate_3layer_risk,
    # 路径
    get_project_root, ensure_runtime_data_layout,
    path_blacklist_json, path_intraday_snapshots_json, path_wash_metrics_json,
    # 算法占位 / fallback
    compute_p1_multi_dim_smooth_score, percentile_ranks,
    compress_surge_bonus, dampen_burst_by_extremes, burst_soft_cap,
    build_rank_lookup, neutral_ranks, personality_liquidity_blend,
    infer_turnover_rate_f_pct, effective_turnover_rate_f,
    series_effective_turnover_f_daily, compute_market_contraction_context,
    adaptive_relaxed_golden_gate_ok,
    precompute_indicators, merge_daily_with_realtime, strict_golden_burst_ok,
    would_trigger_danger_sell, normalize_stock_display_name,
    # 工具
    _p1_scan_min_circ_mv_yi, _safe_float, _a_share_elapsed_minutes,
    _golden_evaluate, _sanitize_rt_vol_ratio,
    _intraday_ma5_vol_baseline_hand, _extract_scan_surface_metrics,
    _safe_notify_system_alert, _prev_calendar_bizday_yyyymmdd,
    _expected_latest_daily_anchor_for_intraday, _try_auto_sync_recent_days_for_scan,
    _route_scored_pool_row,
    # 探测器 / 市场
    _evaluate_personality, _calc_safety_factor, _calc_decay_factor_atr,
    _calc_position, _calc_defense_line, _calc_crowding_penalty,
    _p3_right_side_guard, _execution_tier_for_pool,
    _infer_golden_gate_reason, _build_priority_tags,
    # 钓鱼线 / 黑名单
    _add_to_blacklist, _load_breakout_vwap_eps_for_danger_buy,
    _danger_buy_vwap_fish_line_trigger, _danger_buy_estimate_vwap_from_rt,
    # 日内
    _INTRADAY_SLOT_IDS, _normalize_intraday_slot_id,
    _intraday_write_slot_to_stock_row, _tail_volume_anchor,
    _apply_intraday_snapshots_to_rt,
    _format_p4_trade_language, _p3_hit_tier, _format_p3_trade_layers,
    # T+1 记忆
    _tier_wash_path, _load_prev_scan_empty_streak, _merge_tier_meta_scan,
    _parse_crowding_score, _default_t1_memory_payload,
    _normalize_t1_pool_payload, _collect_t1_memory_for_pools,
    _load_t1_memory_for_scan, _encode_t1_memory_protocol,
    _persist_scan_attribution_snapshot, _ensure_pool_table_row_contract,
    _rollback_scan_funnel_for_skipped_stock,
    _scan_build_industry_pe_stats_for_p1,
)

# 原 scan_engine 期间作为"统一入口"的兼容 re-export：
# core/intraday_snapshot_scheduler.py 等老调用方习惯从 core.scan_engine 直接拿这两个
# 工具代理到 data.api_fetcher / data.db_core._schema_v26 的真实来源
from data.api_fetcher import fetch_realtime_batch as fetch_realtime_batch  # noqa: E402,F401
from data.db_core._schema_v26 import load_p1_cache as load_p1_cache  # noqa: E402,F401

__all__ = [
    "run_scan_engine",
    "capture_intraday_snapshots",
    "get_intraday_snapshot_status",
    "save_p3_first_trigger_snapshot",
    "save_signal_log",
    "get_realtime_sector_ranking",
    "fetch_realtime_batch",
    "load_p1_cache",
    "BJ_TZ", "PROJECT_ROOT", "DANGER_BUY_VWAP_MIN_DAY_PCT",
    "T1_MEMORY_SCHEMA_VERSION", "DEFAULT_RISK_CONFIG",
    "p2_engine", "p3_engine", "p4_engine", "p5_engine",
    "golden_engine", "evaluate_3layer_risk",
    "get_project_root", "ensure_runtime_data_layout",
    "path_blacklist_json", "path_intraday_snapshots_json", "path_wash_metrics_json",
    "compute_p1_multi_dim_smooth_score", "percentile_ranks",
    "compress_surge_bonus", "dampen_burst_by_extremes", "burst_soft_cap",
    "build_rank_lookup", "neutral_ranks", "personality_liquidity_blend",
    "infer_turnover_rate_f_pct", "effective_turnover_rate_f",
    "series_effective_turnover_f_daily", "compute_market_contraction_context",
    "adaptive_relaxed_golden_gate_ok",
    "precompute_indicators", "merge_daily_with_realtime", "strict_golden_burst_ok",
    "would_trigger_danger_sell", "normalize_stock_display_name",
    "_p1_scan_min_circ_mv_yi", "_safe_float", "_a_share_elapsed_minutes",
    "_golden_evaluate", "_sanitize_rt_vol_ratio",
    "_intraday_ma5_vol_baseline_hand", "_extract_scan_surface_metrics",
    "_safe_notify_system_alert", "_prev_calendar_bizday_yyyymmdd",
    "_expected_latest_daily_anchor_for_intraday", "_try_auto_sync_recent_days_for_scan",
    "_route_scored_pool_row",
    "_evaluate_personality", "_calc_safety_factor", "_calc_decay_factor_atr",
    "_calc_position", "_calc_defense_line", "_calc_crowding_penalty",
    "_p3_right_side_guard", "_execution_tier_for_pool",
    "_infer_golden_gate_reason", "_build_priority_tags",
    "_add_to_blacklist", "_load_breakout_vwap_eps_for_danger_buy",
    "_danger_buy_vwap_fish_line_trigger", "_danger_buy_estimate_vwap_from_rt",
    "_INTRADAY_SLOT_IDS", "_normalize_intraday_slot_id",
    "_intraday_write_slot_to_stock_row", "_tail_volume_anchor",
    "_apply_intraday_snapshots_to_rt",
    "_format_p4_trade_language", "_p3_hit_tier", "_format_p3_trade_layers",
    "_tier_wash_path", "_load_prev_scan_empty_streak", "_merge_tier_meta_scan",
    "_parse_crowding_score", "_default_t1_memory_payload",
    "_normalize_t1_pool_payload", "_collect_t1_memory_for_pools",
    "_load_t1_memory_for_scan", "_encode_t1_memory_protocol",
    "_persist_scan_attribution_snapshot", "_ensure_pool_table_row_contract",
    "_rollback_scan_funnel_for_skipped_stock",
    "_scan_build_industry_pe_stats_for_p1",
]