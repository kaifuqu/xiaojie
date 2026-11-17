# -*- coding: utf-8 -*-
"""
core/pool_manager - P1 底仓管理与打分枢纽（拆分后子包）

【拆分说明】
原 core/pool_manager.py (138 KB / 2,788 行) 拆分为 7 个子文件 + 兼容 shim：
  - core.pool_manager.paths            路径解析 + 全局底座
  - core.pool_manager.thresholds       P1 阈值常量 + 容错补偿 + 行为判定
  - core.pool_manager.blacklists       Tushare 初始化 + 解禁黑名单 + 惩罚黑名单
  - core.pool_manager.strategic_mapping 战略白名单 + 动态行业贝塔 + 趋势上下文
  - core.pool_manager.single_stock     P1 单票处理主流程（_process_single_stock_for_p1）
  - core.pool_manager.pool_builder     build_p1_pool_and_cache 主流程 + 落盘
  - core.pool_manager.fast_lane        Momentum Fast-Lane 直通车
  - core.pool_manager.cache_sovereignty P1 缓存 JSON 主权（UI vs 守护）

旧 import 路径 `from core.pool_manager import ...` 100% 兼容：
  - 所有公有/私有符号通过 __all__ 重导出
  - core/pool_manager.py 退化为 shim，统一转发到本包

【业务入口】
- 外部调用 build_p1_pool_and_cache(stock_data_list, ...) 时仍走原 import 路径
- 单票处理 _process_single_stock_for_p1 由 pool_builder / 单测直接调用
- 直通车 select_momentum_fast_lane_ts_codes / compute_momentum_fast_lane_probe_codes 由
  core/scan/pipeline.py 拉取并入扫描底仓
"""
from core.pool_manager import paths as paths
from core.pool_manager import thresholds as thresholds
from core.pool_manager import blacklists as blacklists
from core.pool_manager import strategic_mapping as strategic_mapping
from core.pool_manager import single_stock as single_stock
from core.pool_manager import pool_builder as pool_builder
from core.pool_manager import fast_lane as fast_lane
from core.pool_manager import cache_sovereignty as cache_sovereignty

# ============ paths ============
from core.pool_manager.paths import (
    get_project_root,
    PROJECT_ROOT,
    ensure_runtime_data_layout,
    path_blacklist_json,
    path_p1_gene_json,
    path_sector_rank_history_json,
    path_strategic_mapping_debug_json,
    path_wash_metrics_json,
    path_p1_last_wash_input_codes_json,
)

# ============ thresholds ============
from core.pool_manager.thresholds import (
    P1_TREND_MA120_MIN_RATIO, P1_TREND_SLOPE_FASTPASS,
    P1_NEAR_MA20_MIN_RATIO, P1_MACD_BAR_KILL,
    P1_VOL_DIVERGENCE_RATIO, P1_PASS_LINE,
    P1_MV_SUPER_YI, P1_AMNESTY_DYNAMIC_INFLOW_CAP_YUAN,
    p1_capital_resonance_hard_gate, p1_capital_resonance_scan_secondary_gate,
    _p1_final_sort_key, _build_sector_rank_map,
    _get_regime_thresholds, get_p1_threshold_profile_label,
    _p1_regime_profile_key, get_p1_threshold_summary,
    _p1_min_circ_mv_yi_pool, _p1_mv_yi_for_thr,
    _load_p1_min_score, _p1_mv_fail_reason_for,
    infer_turnover_rate_f_pct, compute_market_contraction_context,
    adaptive_turnover_kill_threshold_relaxed,
    get_all_basic_industry, get_latest_sector_ranking,
    get_latest_daily_data_trade_date_yyyymmdd,
    _safe_float, _finite_clip01,
    _compute_p1_compensation_tier, _effective_p1_thresholds,
    _p1_is_high_cooldown, _p1_is_two_day_below_ma20,
    _p1_is_single_peak_dense, _p1_behavior_penalty_hits,
)

# ============ blacklists ============
# 【V26.88+10-1】``pro`` 走 PEP 562 懒解析，不在此 from-import（会把 None 快照到 __init__.py globals）
from core.pool_manager.blacklists import (
    _init_pro,
    GLOBAL_UNLOCK_BLACKLIST,
    _get_unlock_blacklist,
    _get_punish_blacklist,
)

# ============ strategic_mapping ============
from core.pool_manager.strategic_mapping import (
    STRATEGIC_INDUSTRIES,
    _sector_rank_recent_date_keys, _strategic_window_weights,
    _industry_tokens, _industry_theme_seed,
    _build_auto_strategic_mapping, _aggregate_sector_snapshot_for_strategic,
    _calc_sector_rank_trend_context,
    _get_dynamic_strategic_industries,
)

# ============ single_stock ============
from core.pool_manager.single_stock import (
    _evaluate_p1_single_stock,
    _process_single_stock_for_p1,
)

# ============ pool_builder ============
from core.pool_manager.pool_builder import (
    _TIER_META_KEY,
    _wash_metrics_path,
    _merge_tier_meta_p1,
    get_last_p1_observation_pool,
    get_last_p1_wash_adaptive,
    build_p1_pool_and_cache,
)

# ============ fast_lane ============
from core.pool_manager.fast_lane import (
    MOMENTUM_FAST_LANE_MAX_PROBE, MOMENTUM_FAST_LANE_MAX_PICK, MOMENTUM_FAST_LANE_TOP_SECTORS,
    MFL_PCT_LIMIT_TOUCH, MFL_PCT_STRONG, MFL_PCT_AMOUNT_TIER,
    MFL_CLOSE_IN_RANGE_MIN, MFL_OPEN_SURGE_PCT, MFL_OPEN_SURGE_NEED_DAY_PCT,
    MFL_LIMIT_TIMES_MIN, MFL_LIMIT_TIMES_MIN_PCT,
    _ts_to_s_code, compute_momentum_fast_lane_probe_codes,
    _mfl_rt_metrics, select_momentum_fast_lane_ts_codes,
    build_momentum_fast_lane_base_items,
    extend_fast_lane_with_limit_streak_hits,
    merge_scan_base_with_fast_lane_items,
)

# ============ cache_sovereignty ============
from core.pool_manager.cache_sovereignty import (
    P1_CACHE_JSON_SOURCE_UI_MANUAL,
    P1_CACHE_JSON_SOURCE_DAEMON_AUTO,
    p1_cache_json_should_skip_daemon_overwrite,
)


__all__ = [
    # 模块
    "paths", "thresholds", "blacklists", "strategic_mapping",
    "single_stock", "pool_builder", "fast_lane", "cache_sovereignty",
    # paths
    "get_project_root", "PROJECT_ROOT", "ensure_runtime_data_layout",
    "path_blacklist_json", "path_p1_gene_json",
    "path_sector_rank_history_json", "path_strategic_mapping_debug_json",
    "path_wash_metrics_json",
    # thresholds
    "P1_TREND_MA120_MIN_RATIO", "P1_TREND_SLOPE_FASTPASS",
    "P1_NEAR_MA20_MIN_RATIO", "P1_MACD_BAR_KILL",
    "P1_VOL_DIVERGENCE_RATIO", "P1_PASS_LINE",
    "P1_MV_SUPER_YI", "P1_AMNESTY_DYNAMIC_INFLOW_CAP_YUAN",
    "p1_capital_resonance_hard_gate", "p1_capital_resonance_scan_secondary_gate",
    "_p1_final_sort_key", "_build_sector_rank_map",
    "_get_regime_thresholds", "get_p1_threshold_profile_label",
    "_p1_regime_profile_key", "get_p1_threshold_summary",
    "_p1_min_circ_mv_yi_pool", "_p1_mv_yi_for_thr",
    "_load_p1_min_score", "_p1_mv_fail_reason_for",
    "infer_turnover_rate_f_pct", "compute_market_contraction_context",
    "adaptive_turnover_kill_threshold_relaxed",
    "get_all_basic_industry", "get_latest_sector_ranking",
    "get_latest_daily_data_trade_date_yyyymmdd",
    "_safe_float", "_finite_clip01",
    "_compute_p1_compensation_tier", "_effective_p1_thresholds",
    "_p1_is_high_cooldown", "_p1_is_two_day_below_ma20",
    "_p1_is_single_peak_dense", "_p1_behavior_penalty_hits",
    # blacklists
    "_init_pro", "pro", "GLOBAL_UNLOCK_BLACKLIST",
    "_get_unlock_blacklist", "_get_punish_blacklist",
    # strategic_mapping
    "STRATEGIC_INDUSTRIES",
    "_sector_rank_recent_date_keys", "_strategic_window_weights",
    "_industry_tokens", "_industry_theme_seed",
    "_build_auto_strategic_mapping", "_aggregate_sector_snapshot_for_strategic",
    "_calc_sector_rank_trend_context",
    "_get_dynamic_strategic_industries",
    # single_stock
    "_evaluate_p1_single_stock",
    "_process_single_stock_for_p1",
    # pool_builder
    "_TIER_META_KEY", "_wash_metrics_path", "_merge_tier_meta_p1",
    "get_last_p1_observation_pool", "get_last_p1_wash_adaptive",
    "build_p1_pool_and_cache",
    # fast_lane
    "MOMENTUM_FAST_LANE_MAX_PROBE", "MOMENTUM_FAST_LANE_MAX_PICK", "MOMENTUM_FAST_LANE_TOP_SECTORS",
    "MFL_PCT_LIMIT_TOUCH", "MFL_PCT_STRONG", "MFL_PCT_AMOUNT_TIER",
    "MFL_CLOSE_IN_RANGE_MIN", "MFL_OPEN_SURGE_PCT", "MFL_OPEN_SURGE_NEED_DAY_PCT",
    "MFL_LIMIT_TIMES_MIN", "MFL_LIMIT_TIMES_MIN_PCT",
    "_ts_to_s_code", "compute_momentum_fast_lane_probe_codes",
    "_mfl_rt_metrics", "select_momentum_fast_lane_ts_codes",
    "build_momentum_fast_lane_base_items",
    "extend_fast_lane_with_limit_streak_hits",
    "merge_scan_base_with_fast_lane_items",
    # cache_sovereignty
    "P1_CACHE_JSON_SOURCE_UI_MANUAL", "P1_CACHE_JSON_SOURCE_DAEMON_AUTO",
    "p1_cache_json_should_skip_daemon_overwrite",
]


# 【V26.88+10-1】PEP 562 懒模块属性：让 ``core.pool_manager.pro`` / ``pool_manager.pro``
# 走 data.fetcher 懒初始化，避免 import 期 None 快照锁死。
# 注意：仍然禁止 ``from core.pool_manager import pro``，那会在 import 期把值快照到调用方。
def __getattr__(name):
    if name == "pro":
        from data.fetcher import _client as _fclient
        return _fclient._ensure_pro_initialized()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")