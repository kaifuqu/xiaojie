# -*- coding: utf-8 -*-
"""
core/scan/pipeline.py
原 core/scan_engine.py L2162-L3545（主中枢调度与打分引擎 run_scan_engine）

【职责】
- 数据新鲜度自检 + P2/P3/P4/P5 停用判断（line 2162-2309）
- 熔断器读入（sop_v11） + 持仓日数映射（signal_log 聚合） + 板块排名（line 2310-2358）
- P3/P4 右侧直通车（Momentum Fast-Lane）合并（line 2360-2405）
- 主循环：P1 多维分项 + P2/P3/P4/P5 战法匹配 + 金共振 + 拥挤度降权 + P3 右侧防追高
  + 双层门禁（strict_gate + 辅助观察门禁）+ 缩量放宽 + danger_buy/sell 预警
  + 入表行契约 + 跨池加权 + EV 排序（line 2408-3524）
- 收尾：跨池加权 + EV 排序 + 主池排序 + 多层级池子过滤 + T+1 归因落盘（line 3470-3545）

【公开 API】
- run_scan_engine（V26.88 核心入口；被主流程 / 调度 / 单元测试直接调用）

【跨包依赖】
- core.scan.utils（_p1_scan_min_circ_mv_yi, BJ_TZ, _safe_float, _a_share_elapsed_minutes,
  _sanitize_rt_vol_ratio, _intraday_ma5_vol_baseline_hand, _extract_scan_surface_metrics,
  _route_scored_pool_row, _try_auto_sync_recent_days_for_scan, _expected_latest_daily_anchor_for_intraday,
  golden_engine, p2_engine, p3_engine, p4_engine, p5_engine,
  fetch_realtime_batch, get_realtime_sector_ranking, get_latest_daily_data_trade_date_yyyymmdd,
  get_stock_industry, get_all_basic_industry,
  path_intraday_snapshots_json, would_trigger_danger_sell, normalize_stock_display_name,
  compute_p1_multi_dim_smooth_score, percentile_ranks, build_rank_lookup, neutral_ranks,
  precompute_indicators, merge_daily_with_realtime, infer_turnover_rate_f_pct, strict_golden_burst_ok,
  adaptive_relaxed_golden_gate_ok, compute_market_contraction_context,
  compress_surge_bonus, burst_soft_cap, dampen_burst_by_extremes)
- core.scan.detectors_market（_evaluate_personality, _calc_safety_factor, _calc_decay_factor_atr,
  _calc_position, _calc_defense_line, _calc_crowding_penalty, _p3_right_side_guard,
  _execution_tier_for_pool, _infer_golden_gate_reason, _build_priority_tags）
- core.scan.detectors_pattern（_load_breakout_vwap_eps_for_danger_buy, _danger_buy_vwap_fish_line_trigger,
  _danger_buy_estimate_vwap_from_rt, DANGER_BUY_VWAP_MIN_DAY_PCT）
- core.scan.intraday_snapshots（_apply_intraday_snapshots_to_rt, _tail_volume_anchor,
  _format_p4_trade_language, _format_p3_trade_layers, save_p3_first_trigger_snapshot, save_signal_log）
- core.scan.t1_memory（_load_t1_memory_for_scan, _normalize_t1_pool_payload,
  _load_prev_scan_empty_streak, _merge_tier_meta_scan, _persist_scan_attribution_snapshot,
  _ensure_pool_table_row_contract, _rollback_scan_funnel_for_skipped_stock, _scan_build_industry_pe_stats_for_p1）
- data.db_core（get_read_conn, table_exists）
- data.api_fetcher（fetch_realtime_batch - 已在 utils）
- core.stop_loss_utils（compute_expected_value）
- core.sop_v11（evaluate_market_circuit_breaker）
- core.regime_analyzer（get_latest_sector_ranking）
- core.master_control（is_maintenance_mode_enabled）
- core.config_manager（get_p1_regime_thresholds, get_strategy_min_circ_mv_wan, get_golden_config, get_p3_intraday_screener_config）
- core.pool_manager（Momentum Fast-Lane 系列 + _get_dynamic_strategic_industries）
- data.data_fetcher（check_data_completeness, sync_recent_days - lazy）
"""
import json
import logging
import os
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

import constants
from core.scan.utils import (
    BJ_TZ,
    _a_share_elapsed_minutes,
    _expected_latest_daily_anchor_for_intraday,
    _extract_scan_surface_metrics,
    _intraday_ma5_vol_baseline_hand,
    _per_stock_daily_stale_reason,
    _p1_scan_min_circ_mv_yi,
    _route_scored_pool_row,
    _safe_float,
    _sanitize_rt_vol_ratio,
    _try_auto_sync_recent_days_for_scan,
    adaptive_relaxed_golden_gate_ok,
    burst_soft_cap,
    build_rank_lookup,
    compress_surge_bonus,
    compute_market_contraction_context,
    compute_p1_multi_dim_smooth_score,
    dampen_burst_by_extremes,
    fetch_realtime_batch,
    get_all_basic_industry,
    get_latest_daily_data_trade_date_yyyymmdd,
    get_stock_industry,
    golden_engine,
    infer_turnover_rate_f_pct,
    merge_daily_with_realtime,
    neutral_ranks,
    normalize_stock_display_name,
    path_intraday_snapshots_json,
    p2_engine,
    p3_engine,
    p4_engine,
    p5_engine,
    percentile_ranks,
    precompute_indicators,
    strict_golden_burst_ok,
    would_trigger_danger_sell,
)
from core.scan.detectors_market import (
    _build_priority_tags,
    _calc_crowding_penalty,
    _calc_decay_factor_atr,
    _calc_defense_line,
    _calc_position,
    _calc_safety_factor,
    _evaluate_personality,
    _execution_tier_for_pool,
    _infer_golden_gate_reason,
    _p3_right_side_guard,
    # [V26.88+10-8 P5 扫描修复] 文档里写的"re-export"从未真正落地, 之前 utils.py 文档承诺
    # 但代码没 import, 导致 pipeline:322 调用 get_realtime_sector_ranking() 时 NameError.
    # P2/P3/P4 也走这条路径, 只是今天第一次点 P5 才暴露. 改为直接从此模块显式导入.
    get_realtime_sector_ranking,
)
from core.scan.detectors_pattern import (
    DANGER_BUY_VWAP_MIN_DAY_PCT,
    _danger_buy_estimate_vwap_from_rt,
    _danger_buy_vwap_fish_line_trigger,
    _load_breakout_vwap_eps_for_danger_buy,
)
from core.scan.intraday_snapshots import (
    _apply_intraday_snapshots_to_rt,
    _format_p3_trade_layers,
    _format_p4_trade_language,
    _tail_volume_anchor,
    save_p3_first_trigger_snapshot,
    save_signal_log,
)
from core.scan.t1_memory import (
    _ensure_pool_table_row_contract,
    _load_prev_scan_empty_streak,
    _load_t1_memory_for_scan,
    _merge_tier_meta_scan,
    _normalize_t1_pool_payload,
    _persist_scan_attribution_snapshot,
    _rollback_scan_funnel_for_skipped_stock,
    _scan_build_industry_pe_stats_for_p1,
)

logging.basicConfig(level=logging.INFO, format='%(asctime)s [扫描引擎-pipeline] %(message)s')


def run_scan_engine(target_pools, base_items, regime="震荡市", progress_callback=None):
    """
    主扫描入口（V26.88+ 拆分后）：
    1. 数据新鲜度自检（同步 + 停用判断）
    2. 板块排名 + 持仓日数映射 + 直通车合并
    3. P1 多维分项初筛 → P2/P3/P4/P5 战法循环
    4. 金共振 + 双层门禁 + 拥挤度 + P3 右侧防追高 + danger 预警
    5. 入表行契约 + 跨池加权 + EV 排序
    6. 多层级池子过滤 + T+1 归因落盘
    """
    # 策略阈值：P2–P5 引擎 run_all 内每轮从 config_manager 读取（见 strat_p*_*.py）
    if not base_items: return {}
    p1_min_circ_mv_yi = _p1_scan_min_circ_mv_yi()
    res = {k: [] for k in target_pools}

    res['danger_buy'] = []
    res['danger_sell'] = []
    # 诊断漏斗：按池子统计每一层剩余数量（只读诊断，不参与策略计算）
    res['funnel'] = {
        k: {
            "total_candidates": 0,
            "enter_strategy_check": 0,
            "pass_golden_gate": 0,
            "hit_strategy": 0,
            "pass_score": 0,
            "gate_block_reasons": {},
        } for k in target_pools
    }
    # P1 初筛统计：市值回滚次数、平滑分拦截次数（与 pool_manager 淘汰文案对齐）
    res["p1_prescreen"] = {
        "pass_line": 50.0,
        "smooth_blocked": 0,
        "mv_skipped_rollbacks": 0,
        "gate_reasons": {},
    }
    # 【多层级池子】说明：P2–P5 震荡观察池（缩量期备选），与主池分列；最终是否保留由文末 real_fill 再校验
    res["observation"] = {k: [] for k in target_pools}
    active_target_pools = list(target_pools)

    # 【V30.7 上线加固】异常可观测：过去单票/单池异常只写 logging.warning 后 continue，
    # 扫描函数仍返回空池，外部看起来像「正常但今天没票」，属于假成功。
    # 现在把每次异常结构化记入 res["_scan_errors"]，由调用方（守护/UI/隔离扫描）判定。
    res["_scan_errors"] = []
    res["_skipped_stocks"] = {}
    _scan_error_detail = {}

    def _record_scan_error(pool_key, code, stage, exc):
        """记录一次扫描期异常（限流：同一 pool+阶段 只留 20 条明细，防止日志膨胀）。"""
        pk = str(pool_key or "unknown")
        cs = str(code or "?")
        slot = res["_skipped_stocks"].setdefault(pk, {})
        slot[cs] = slot.get(cs, 0) + 1
        key = "%s|%s|%s" % (pk, stage, type(exc).__name__)
        agg = _scan_error_detail.get(key)
        if agg is None:
            agg = {"pool": pk, "stage": stage, "exc_type": type(exc).__name__,
                   "count": 0, "codes": [], "message": str(exc)[:200]}
            _scan_error_detail[key] = agg
        agg["count"] += 1
        if len(agg["codes"]) < 20 and cs not in agg["codes"]:
            agg["codes"].append(cs)
        if len(res["_scan_errors"]) < 200:
            res["_scan_errors"].append({
                "pool": pk, "stage": stage, "code": cs,
                "exc_type": type(exc).__name__, "message": str(exc)[:200],
            })

    def _finalize_scan_errors():
        res["_scan_error_summary"] = list(_scan_error_detail.values())
        res["_scan_error_total"] = sum(v["count"] for v in _scan_error_detail.values())

    # 扫描前数据新鲜度规则
    today_cal = datetime.now(BJ_TZ).strftime("%Y%m%d")
    is_trading_day = True
    try:
        from core.master_control import is_maintenance_mode_enabled
        import data.fetcher as _scan_data_fetcher  # V26.88 拆分
        if not is_maintenance_mode_enabled():
            is_trading_day = bool(_scan_data_fetcher.check_data_completeness(days=1, static_mode=True)[0])
    except Exception:
        is_trading_day = True
    expected_prev_td = _expected_latest_daily_anchor_for_intraday(today_cal)
    latest_trade_date = str(get_latest_daily_data_trade_date_yyyymmdd() or "").replace("-", "")[:8]

    p234_requested = [k for k in ("p2", "p3", "p4") if k in target_pools]
    p5_requested = "p5" in target_pools

    p234_need_sync = (
        bool(p234_requested)
        and len(expected_prev_td) == 8
        and expected_prev_td.isdigit()
        and (
            len(latest_trade_date) != 8
            or (not latest_trade_date.isdigit())
            or latest_trade_date < expected_prev_td
        )
    )
    p5_required_trade_date = expected_prev_td or today_cal
    p5_need_sync = bool(p5_requested) and (
        (len(latest_trade_date) != 8)
        or (not latest_trade_date.isdigit())
        or (len(p5_required_trade_date) == 8 and p5_required_trade_date.isdigit() and latest_trade_date < p5_required_trade_date)
    )

    if p234_need_sync or p5_need_sync:
        _ = _try_auto_sync_recent_days_for_scan(progress_callback=progress_callback)
        latest_trade_date = str(get_latest_daily_data_trade_date_yyyymmdd() or "").replace("-", "")[:8]
        p234_stale = (
            bool(p234_requested)
            and len(expected_prev_td) == 8
            and expected_prev_td.isdigit()
            and (
                len(latest_trade_date) != 8
                or (not latest_trade_date.isdigit())
                or latest_trade_date < expected_prev_td
            )
        )
        p5_need_sync_after = bool(p5_requested) and (
            (len(latest_trade_date) != 8)
            or (not latest_trade_date.isdigit())
            or (
                len(p5_required_trade_date) == 8
                and p5_required_trade_date.isdigit()
                and latest_trade_date < p5_required_trade_date
            )
        )
    else:
        p234_stale = (
            bool(p234_requested)
            and len(expected_prev_td) == 8
            and expected_prev_td.isdigit()
            and (
                len(latest_trade_date) != 8
                or (not latest_trade_date.isdigit())
                or latest_trade_date < expected_prev_td
            )
        )
        p5_need_sync_after = False

    if p234_requested:
        if p234_stale:
            active_target_pools = [k for k in active_target_pools if k not in p234_requested]
            msg = (
                "P2~P4停用：日线未对齐到扫描基准日"
                f"（latest={latest_trade_date or 'N/A'}，required>={expected_prev_td or 'N/A'}）"
            )
            logging.warning(msg)
            if progress_callback:
                try:
                    progress_callback(f"⏸️ {msg}")
                except Exception:
                    pass
            for pk in p234_requested:
                if pk in res.get("funnel", {}):
                    gr = res["funnel"][pk]["gate_block_reasons"]
                    gr[msg] = int(gr.get(msg, 0)) + 1

    if p5_requested and p5_need_sync_after:
        active_target_pools = [k for k in active_target_pools if k != "p5"]
        now_bj = datetime.now()
        curr_min = now_bj.hour * 60 + now_bj.minute
        if curr_min < 15 * 60:
            hint = "盘前/盘中启动：等待收盘日落库"
        else:
            hint = "盘后启动：可直接扫描，无需再判日线是否落后"
        msg = (
            f"P5停用：{hint}"
            f"（latest={latest_trade_date or 'N/A'}，required>={p5_required_trade_date or 'N/A'}；收盘日={p5_required_trade_date or 'N/A'}）"
        )
        logging.warning(msg)
        if progress_callback:
            try:
                progress_callback(f"⏸️ {msg}")
            except Exception:
                pass
        if "p5" in res.get("funnel", {}):
            gr = res["funnel"]["p5"]["gate_block_reasons"]
            gr[msg] = int(gr.get(msg, 0)) + 1

    try:
        from core.sop_v11 import evaluate_market_circuit_breaker
        res["sop_market_breaker"] = evaluate_market_circuit_breaker()
    except Exception:
        res["sop_market_breaker"] = {}

    # 综合分参考线 60：仅用于排序、漏斗「质量档」与 signal_log 写入
    # P2–P5 统一底线：综合分 >= 30 才允许企微推送
    min_pass_map = {'p2': 30.0, 'p3': 30.0, 'p4': 30.0, 'p5': 30.0}
    # 🐉 核心修改区：P5 与 P4 共用 0.95 极高优选权重
    pool_weight_map = {'p2': 0.92, 'p3': 1.0, 'p4': 0.95, 'p5': 0.95}

    holding_days_map = {}
    try:
        from data.db_core import get_read_conn, table_exists
        if table_exists("signal_log"):
            recent_date = (datetime.now(BJ_TZ) - timedelta(days=15)).strftime("%Y-%m-%d")
            query = """
                SELECT ts_code, COUNT(DISTINCT trade_date) AS sig_days
                FROM signal_log
                WHERE trade_date >= ?
                GROUP BY ts_code
            """
            df_sig = None
            with get_read_conn(read_only=True) as con:
                df_sig = con.execute(query, [recent_date]).df()
            if df_sig is not None and not df_sig.empty and "ts_code" in df_sig.columns and "sig_days" in df_sig.columns:
                holding_days_map = {
                    str(row.ts_code): row.sig_days
                    for row in df_sig.itertuples(index=False)
                    if getattr(row, "ts_code", None)
                }
            elif df_sig is not None and not df_sig.empty:
                logging.warning("signal_log 聚合结果缺 ts_code/sig_days 列，跳过持仓日数映射")
    except Exception as e:
        logging.error(f"批量获取信号日志失败: {e}")

    if progress_callback: progress_callback("📡 正在探测全市场板块共振信号...")
    if is_trading_day:
        sector_ranking_dict = get_realtime_sector_ranking()
    else:
        try:
            from core.regime_analyzer import get_latest_sector_ranking
            sector_ranking_dict = get_latest_sector_ranking() or {}
        except Exception:
            sector_ranking_dict = {}
    sorted_sectors = list(sector_ranking_dict.keys())
    sector_rank_map = {ind: idx + 1 for idx, ind in enumerate(sorted_sectors)}
    total_sectors = len(sorted_sectors)

    # ---------- P3/P4 右侧直通车（Momentum Fast-Lane）----------
    base_items_for_scan = list(base_items)
    rt_map_mfl = None
    if is_trading_day and ("p3" in active_target_pools or "p4" in active_target_pools):
        try:
            from core.pool_manager import (
                build_momentum_fast_lane_base_items,
                compute_momentum_fast_lane_probe_codes,
                extend_fast_lane_with_limit_streak_hits,
                merge_scan_base_with_fast_lane_items,
                select_momentum_fast_lane_ts_codes,
            )

            _bc = [
                str(x["code"]).strip()
                for x in base_items
                if isinstance(x, dict) and x.get("code")
            ]
            _probe = compute_momentum_fast_lane_probe_codes(_bc, sorted_sectors)
            if progress_callback and _probe:
                progress_callback(
                    f"🚀 P3/P4 右侧直通车：增量探测 {len(_probe)} 只非底仓标的（并行快照，无 daily 全表扫描）…"
                )
            _fetch_union = list(dict.fromkeys(_bc + _probe))
            rt_map_mfl = fetch_realtime_batch(_fetch_union) or {}
            _base_frozen = frozenset(_bc)
            _picked = select_momentum_fast_lane_ts_codes(_probe, rt_map_mfl, _base_frozen)
            _extra_streak = extend_fast_lane_with_limit_streak_hits(
                _probe, rt_map_mfl, _base_frozen, frozenset(_picked), max_add=12,
            )
            _picked_all = list(dict.fromkeys(list(_picked) + list(_extra_streak)))
            _fl_items = build_momentum_fast_lane_base_items(_picked_all)
            for _it in _fl_items:
                if isinstance(_it, dict):
                    _it.setdefault("pool_tier", "fastlane")
                    _it.setdefault("pool_source", "直通车")
            base_items_for_scan = merge_scan_base_with_fast_lane_items(base_items, _fl_items)
        except Exception as _e_mfl:
            logging.warning("P3/P4 右侧直通车合并失败，回退为纯底仓扫描: %s", _e_mfl)
            base_items_for_scan = list(base_items)
            rt_map_mfl = None

    try:
        from core.config_manager import get_p1_regime_thresholds
        p1_pass_line_scan = float(get_p1_regime_thresholds(regime).get("pass_line", 50.0))
    except Exception:
        p1_pass_line_scan = 50.0
    res["p1_prescreen"]["pass_line"] = float(p1_pass_line_scan)
    industry_map_scan, industry_pe_stats_scan, global_stats_scan = _scan_build_industry_pe_stats_for_p1(
        base_items_for_scan
    )
    try:
        from core.pool_manager import _get_dynamic_strategic_industries
        dynamic_industries_scan = _get_dynamic_strategic_industries(sector_ranking_dict)
    except Exception as _e_dyn:
        logging.debug("扫描引擎动态行业贝塔失败: %s", _e_dyn)
        dynamic_industries_scan = {}

    codes = [
        x["code"]
        for x in base_items_for_scan
        if isinstance(x, dict) and x.get("code")
    ]

    if progress_callback: progress_callback(f"⚡ 定向并发拉取盘口中 (调用异步高并发引擎)...")
    if rt_map_mfl is not None:
        rt_map = rt_map_mfl
    else:
        rt_map = fetch_realtime_batch(codes) or {}
    # 【自适应优化】主扫描入口：全样本市场情绪收缩度
    _mctx = compute_market_contraction_context(base_items, rt_map)
    market_contraction_score = float(_mctx.get("score", 0.0) or 0.0)
    if not is_trading_day:
        market_contraction_score = 0.0
    res["market_contraction_score"] = market_contraction_score
    res["adaptive_reason"] = str(_mctx.get("adaptive_reason", "") or "")
    res["adaptive_sample_count"] = int(_mctx.get("sample_count", 0) or 0)
    prev_scan_streak = _load_prev_scan_empty_streak()
    if not is_trading_day:
        prev_scan_streak = 0
    if not rt_map:
        rt_map = {}

    snapshot_file = path_intraday_snapshots_json()
    now_time = datetime.now(BJ_TZ)
    today_str = now_time.strftime("%Y%m%d")
    curr_min = now_time.hour * 60 + now_time.minute

    # 【V34】全市场财务质量预热：1 次 SQL 覆盖整池（P3→P4→P5 共用同一份缓存），
    # 逐票注入从此零查询；预热失败时 _inject_financial_quality_into_rt 自动退回逐票 SQL。
    try:
        from core.strategies.strat_p3_intraday import prime_financial_quality_cache
        _n_fin_primed = prime_financial_quality_cache(codes, today_str)
        logging.debug("全市场财务质量预热完成: %s 票", _n_fin_primed)
    except Exception as _e_fin_prime:
        logging.debug("全市场财务质量预热跳过: %s", _e_fin_prime)

    try:
        _breakout_vwap_eps_danger = _load_breakout_vwap_eps_for_danger_buy()
    except Exception:
        _breakout_vwap_eps_danger = 0.004

    snapshots = {}
    if os.path.exists(snapshot_file):
        try:
            with open(snapshot_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                saved_date = data.get("date", "")
                if saved_date == today_str or curr_min < 565:
                    snapshots = data.get("stocks", {})
        except json.JSONDecodeError as e:
            logging.warning(f"扫描引擎加载快照 JSON 失败 [{snapshot_file}]: {e}")
        except OSError as e:
            logging.warning(f"扫描引擎加载快照 IO 失败 [{snapshot_file}]: {e}")
        except Exception as e:
            logging.warning(f"扫描引擎加载快照失败（未分类）[{snapshot_file}]: {e}")

    # T+1 记忆必须在主循环前显式初始化
    t1_memory = _load_t1_memory_for_scan(active_target_pools)
    if not is_trading_day:
        t1_memory = {pk: _normalize_t1_pool_payload(None) for pk in active_target_pools}

    # ---------- 综合分·截面 Rank ----------
    rank_lookup = {}
    try:
        _iah_cs = (curr_min > 900 or curr_min < 565)
        _surf_rows = []
        for _sit in base_items_for_scan:
            if not isinstance(_sit, dict):
                continue
            _sx = _extract_scan_surface_metrics(_sit, rt_map, curr_min, _iah_cs, today_str, snapshots)
            if _sx:
                _surf_rows.append(_sx)
        if _surf_rows:
            _csdf = pd.DataFrame(_surf_rows)
            for _cc in ("pct", "vol_ratio", "turnover_f", "main_ratio"):
                if _cc in _csdf.columns:
                    _csdf[_cc] = pd.to_numeric(_csdf[_cc], errors="coerce").replace([np.inf, -np.inf], np.nan)
                    if _csdf[_cc].notna().any():
                        _csdf[_cc] = _csdf[_cc].fillna(_csdf[_cc].median())
                    else:
                        _csdf[_cc] = _csdf[_cc].fillna(0.0)
            _csdf = percentile_ranks(_csdf, ["vol_ratio", "turnover_f", "pct", "main_ratio"])
            rank_lookup = build_rank_lookup(_csdf)
            if not is_trading_day:
                rank_lookup = {k: {"pct_r": 0.5, "vol_ratio_r": 0.5, "turnover_f_r": 0.5, "main_ratio_r": 0.5} for k in rank_lookup.keys()}
    except Exception as _rke:
        logging.debug("综合分·截面 Rank 构建失败(退化为中性分位): %s", _rke)
        rank_lookup = {}

    total_items = len(base_items_for_scan)
    funnel = res.get("funnel", {})
    active_funnel_keys = [k for k in active_target_pools if k in funnel]
    funnel_total_candidates = {k: funnel[k]["total_candidates"] for k in active_funnel_keys}
    funnel_enter_strategy_check = {k: funnel[k]["enter_strategy_check"] for k in active_funnel_keys}

    # ==================== 主循环：每只票遍历 P2/P3/P4/P5 ====================
    for i, item in enumerate(base_items_for_scan):
        if not isinstance(item, dict): continue
        for k in active_funnel_keys:
            funnel_total_candidates[k] += 1
            funnel_enter_strategy_check[k] += 1
        if progress_callback and i % max(1, total_items // 10) == 0:
            progress_callback(f"🚀 运算与战法匹配: [{i}/{total_items}]...")

        try:
            full_code = item.get('code', '')
            s_code = str(full_code).split('.')[0][:6]

            if s_code not in rt_map:
                fallback_hist = item.get('hist', {})
                if isinstance(fallback_hist, dict) and fallback_hist:
                    _fb_pc = _safe_float(fallback_hist.get("pre_close"), 0.0)
                    if _fb_pc <= 0:
                        _fb_pc = _safe_float(fallback_hist.get("close"), 0.0)
                    rt_map[s_code] = {
                        'price': fallback_hist.get('close', 0),
                        'pre_close': _fb_pc,
                        'open': fallback_hist.get('open', 0),
                        'volume': 0,
                        'high': fallback_hist.get('high', 0),
                        'low': fallback_hist.get('low', 0),
                        'name': normalize_stock_display_name(fallback_hist.get('name', s_code)),
                    }
                else:
                    continue

            rt = rt_map.get(s_code, {})
            if not isinstance(rt, dict): rt = {}

            # 【V30-修复】P2 次新拦截 / P2-P5 unified_a_share_score 读 rt["list_days"]。
            # fetch_realtime_batch 已注入，但 rt-fallback 与直通车 base_items 仍可能缺失，
            # 这里从底仓 item 的 hist / 日线末行补齐，保证次新股过滤链真的拿得到上市天数。
            try:
                if "list_days" not in rt or rt.get("list_days") in (None, 0):
                    _hist_lb = item.get("hist") if isinstance(item.get("hist"), dict) else {}
                    _lb = None
                    if _hist_lb.get("list_days") not in (None, ""):
                        _lb = _hist_lb.get("list_days")
                    else:
                        _it_df = item.get("df")
                        if isinstance(_it_df, pd.DataFrame) and not _it_df.empty and "list_days" in _it_df.columns:
                            _lb = _it_df["list_days"].iloc[-1]
                    if _lb is not None and pd.notna(_lb):
                        rt["list_days"] = int(float(_lb))
                if not str(rt.get("list_date") or "").strip():
                    _hist_ld = item.get("hist") if isinstance(item.get("hist"), dict) else {}
                    _ld = str(_hist_ld.get("list_date") or "").strip()
                    if not _ld:
                        _it_df2 = item.get("df")
                        if isinstance(_it_df2, pd.DataFrame) and not _it_df2.empty and "list_date" in _it_df2.columns:
                            _ld = str(_it_df2["list_date"].iloc[-1] or "").strip()
                    if _ld:
                        rt["list_date"] = _ld
            except Exception as _e_ldrt:
                logging.debug("[list-days-inject][%s] %s", s_code, _e_ldrt)

            sn_row = snapshots.get(s_code, {})
            if isinstance(sn_row, dict):
                _apply_intraday_snapshots_to_rt(rt, sn_row)

            anchor_vol, anchor_t = _tail_volume_anchor(sn_row, curr_min)
            if (
                anchor_vol is not None
                and anchor_t is not None
                and curr_min > anchor_t
            ):
                delta_vol = _safe_float(rt.get("volume", 0)) - _safe_float(anchor_vol, 0)
                delta_time = curr_min - anchor_t
                if delta_time > 0 and delta_vol > 0:
                    tail_vol_per_min = delta_vol / delta_time
                    if 570 <= curr_min <= 690:
                        elapsed = curr_min - 570
                    elif 780 <= curr_min <= 900:
                        elapsed = 120 + (curr_min - 780)
                    elif curr_min > 900:
                        elapsed = 240
                    else:
                        elapsed = 1
                    _elapsed_safe = max(float(elapsed), 1e-9)
                    avg_vol_per_min = _safe_float(rt.get("volume", 0)) / _elapsed_safe
                    if avg_vol_per_min > 1e-9:
                        rt["tail_vol_ratio"] = tail_vol_per_min / max(avg_vol_per_min, 1e-9)
                        rt["tail_30min_vol"] = delta_vol
                        rt["tail_window_min"] = delta_time
                        rt["tail_anchor_min"] = anchor_t

            hist = item.get('hist', {})
            if not isinstance(hist, dict):
                hist = {}

            df_target = item.get('df')
            if df_target is None or df_target.empty:
                continue

            # 【P0-20261018 V30.5 逐票数据新鲜度闸】
            # 全局闸只检查全库 MAX(trade_date)，发现不了「个别票滞后」。
            # 实测 20261016 全库最新，但 2134 只票中 703 只最后一行更早；
            # 这批票若继续往下走：策略用旧 K 线当今天、P5 business_date 取
            # _last_row.trade_date 会把信号写到历史日期、T+N 回看整体错位。
            # 处理口径：落后票不进任何策略、不写 signal_log，
            #           但要在漏斗里留下「日线截止日落后」的记账，方便 UI 追溯。
            _stale_req = ""
            if p5_requested and "p5" in active_target_pools:
                _stale_req = p5_required_trade_date
            elif p234_requested:
                _stale_req = expected_prev_td
            if _stale_req:
                _stale_reason = _per_stock_daily_stale_reason(df_target, _stale_req)
                if _stale_reason:
                    _rollback_scan_funnel_for_skipped_stock(res, active_target_pools)
                    for _pk in active_funnel_keys:
                        _gr = res["funnel"][_pk]["gate_block_reasons"]
                        _gr[_stale_reason] = int(_gr.get(_stale_reason, 0)) + 1
                    if len(res.get("_per_stock_daily_stale", {})) < 50:
                        res.setdefault("_per_stock_daily_stale", {})[full_code] = {
                            "reason": _stale_reason,
                            "required": str(_stale_req),
                            "last_trade_date": (
                                str(df_target["trade_date"].iloc[-1]) if "trade_date" in df_target.columns else ""
                            ),
                        }
                    logging.debug("[逐票新鲜度闸] %s %s", full_code, _stale_reason)
                    continue

            merged_flag = False
            try:
                df_m, merged_flag = merge_daily_with_realtime(df_target, rt)
                if merged_flag:
                    df_target = df_m
                    item["df"] = df_target
            except Exception as e:
                logging.debug("merge_daily_with_realtime 跳过 %s: %s", s_code, e)

            _need_pre = (
                merged_flag
                or ("ma20" not in df_target.columns)
                or ("max_60d_pct" not in df_target.columns)
            )
            if _need_pre:
                try:
                    df_target = precompute_indicators(df_target)
                    item["df"] = df_target
                except Exception as e:
                    logging.warning("指标计算失败 %s: %s", s_code, e)
                    continue

            df_tail_60 = df_target.tail(60).copy()
            df_tail_5 = df_target.tail(5).copy()

            vol_col = 'vol' if 'vol' in df_target.columns else ('volume' if 'volume' in df_target.columns else None)
            if vol_col is None:
                logging.debug(f"扫描跳过 {full_code}: 无成交量列")
                continue

            circ_mv_wan = 0.0
            if 'circ_mv' in df_target.columns:
                circ_mv_wan = _safe_float(df_target['circ_mv'].iloc[-1])
            else:
                circ_mv_raw = rt.get('circ_mv')
                if circ_mv_raw is None or pd.isna(circ_mv_raw): circ_mv_raw = rt.get('total_mv', 10000000) * 0.6
                circ_mv_wan = _safe_float(circ_mv_raw, default=10000000)

            circ_mv_yi = circ_mv_wan / 10000.0

            # 市值档位
            if circ_mv_yi >= 2000.0:
                size_emoji, size_label = "🦍", "巨无霸"
                size_pool_boost, size_safety_mult, size_penalty_factor = 1.08, 1.06, 0.82
            elif circ_mv_yi >= 1000.0:
                size_emoji, size_label = "🐘", "千亿中军"
                size_pool_boost, size_safety_mult, size_penalty_factor = 1.05, 1.04, 0.86
            elif circ_mv_yi >= 500.0:
                size_emoji, size_label = "🐘", "超级中军"
                size_pool_boost, size_safety_mult, size_penalty_factor = 1.03, 1.02, 0.90
            elif circ_mv_yi >= _p1_scan_min_circ_mv_yi():
                size_emoji, size_label = "🐎", "核心中盘"
                size_pool_boost, size_safety_mult, size_penalty_factor = 1.00, 1.00, 1.00
            else:
                size_emoji, size_label = "🐥", "袖珍盘"
            if circ_mv_yi < p1_min_circ_mv_yi:
                _rollback_scan_funnel_for_skipped_stock(res, active_target_pools)
                res["p1_prescreen"]["mv_skipped_rollbacks"] = int(
                    res["p1_prescreen"].get("mv_skipped_rollbacks", 0) or 0
                ) + 1
                continue

            # 市场温度 × 市值档位 的权重
            if regime in ["情绪退潮市", "主跌浪", "退潮防守"]:
                w_gene, w_burst = 0.60, 0.40
                if size_emoji == "🐎": regime_mult = 0.70
                elif size_emoji in ["🦍", "🐘"]: regime_mult = 1.20
                else: regime_mult = 0.85
            elif regime in ["趋势市", "主升浪", "趋势主升"]:
                w_gene, w_burst = 0.30, 0.70
                if size_emoji == "🐎": regime_mult = 1.20
                elif size_emoji in ["🦍", "🐘"]: regime_mult = 0.90
                else: regime_mult = 1.15
            else:
                w_gene, w_burst = 0.40, 0.60
                regime_mult = 1.0

            industry = get_stock_industry(full_code)
            sector_mult = 1.0
            is_core_sector = False
            ind_rank_str = "未上榜"
            ind_rank = 999
            is_bottom_3 = False

            rank = sector_rank_map.get(industry)
            if rank is not None:
                ind_rank = rank
                if rank <= 3:
                    sector_mult = 1.15
                    is_core_sector = True
                    ind_rank_str = f"实时第{rank}名"
                elif rank <= 8:
                    sector_mult = 1.05
                    ind_rank_str = f"实时第{rank}名"
                elif total_sectors >= 3 and rank > total_sectors - 3:
                    sector_mult = 0.60
                    is_bottom_3 = True
                    ind_rank_str = f"领跌倒数{total_sectors - rank + 1}"
                elif total_sectors >= 8 and rank > total_sectors - 8:
                    sector_mult = 0.85
                    ind_rank_str = f"弱势倒数{total_sectors - rank + 1}"
                else:
                    ind_rank_str = f"实时第{rank}名"

            stock_name = normalize_stock_display_name(
                rt.get("name") or hist.get("name") or str(s_code)
            )

            now_price = _safe_float(rt.get('price'))
            if now_price <= 0 and curr_min < 565:
                now_price = _safe_float(hist.get('close', 0.0))
            elif now_price <= 0:
                now_price = _safe_float(hist.get('close', 0.0))

            pre_price = _safe_float(rt.get("pre_close"), 0.0)
            if pre_price <= 0:
                pre_price = _safe_float(hist.get("pre_close"), 0.0)
            if pre_price <= 0:
                pre_price = _safe_float(hist.get("close"), 0.0)
            if pre_price <= 0:
                continue
            pct = (now_price - pre_price) / pre_price * 100.0

            vol_shares = _safe_float(rt.get('volume', 0.0))
            is_after_hours = (curr_min > 900 or curr_min < 565)

            ma5_vol_hand = _intraday_ma5_vol_baseline_hand(df_target, vol_col)

            vol_ratio_tag = "F"
            if vol_shares <= 0 or curr_min < 565:
                rt['vol_ratio'] = _safe_float(hist.get('vol_ratio', hist.get('vr', 1.0)), default=1.0)
                _trf = _safe_float(hist.get('turnover_rate_f'), 0.0)
                if _trf <= 0:
                    _vh = _safe_float(hist.get('vol', df_target[vol_col].iloc[-1] if vol_col else 0), 0.0)
                    _cl = _safe_float(hist.get('close', now_price), 0.0)
                    _trf = infer_turnover_rate_f_pct(_vh, _cl, circ_mv_wan)
                rt['turnover_rate_f'] = _trf
                vol_ratio_tag = "H" if ('vol_ratio' in hist or 'vr' in hist) else "F"
            else:
                vol_hand = vol_shares / 100.0
                vol_wan_shares = vol_shares / 10000.0
                if is_after_hours:
                    rt['vol_ratio'] = vol_hand / ma5_vol_hand
                else:
                    elapsed_mins = _a_share_elapsed_minutes(curr_min)
                    rt['vol_ratio'] = min((vol_hand / elapsed_mins) / (ma5_vol_hand / 240), 50.0)
                vol_ratio_tag = "R"
                calc_price = now_price if now_price > 0 else _safe_float(hist.get('close', 10), default=10.0)
                _cp = max(calc_price, 1e-9)
                float_shares_wan = circ_mv_wan / _cp
                if float_shares_wan <= 0:
                    float_shares_wan = 1.0
                rt["turnover_rate_f"] = min(infer_turnover_rate_f_pct(vol_hand, calc_price, circ_mv_wan), 100.0)

            _vr_fix, vol_ratio_tag = _sanitize_rt_vol_ratio(rt, df_target, hist, vol_col, vol_ratio_tag)
            rt["vol_ratio"] = _vr_fix

            rt['price'] = now_price
            rt['open'] = _safe_float(rt.get('open', now_price))
            y_bar = df_target.iloc[-1]
            if "capital_resonance_score" in df_target.columns:
                rt["capital_resonance_score"] = _safe_float(y_bar.get("capital_resonance_score", 0.0), 0.0)
            else:
                rt["capital_resonance_score"] = _safe_float(hist.get("capital_resonance_score", 0.0), 0.0)
            for key in ['net_elg_amount', 'cost_50th', 'cost_95th']:
                rt[key] = _safe_float(hist.get(key, 0.0))
            if 'winner_rate' in df_target.columns:
                rt['winner_rate'] = _safe_float(y_bar.get('winner_rate'), rt.get('winner_rate', 0.0))
            if 'avg_cost' in df_target.columns and _safe_float(y_bar.get('avg_cost'), 0.0) > 0:
                rt['avg_cost'] = _safe_float(y_bar.get('avg_cost'), 0.0)
            rz_net_buy_hist = _safe_float(hist.get('rz_net_buy', 0.0))

            cyq_raw = hist.get('cyq_concentration')
            rt['cyq_concentration'] = _safe_float(cyq_raw, default=999.0) if _safe_float(cyq_raw) > 0 else 999.0

            limit_times_val = int(_safe_float(hist.get('limit_times', 0)))
            rt['limit_times'] = limit_times_val
            rt['forecast_type'] = int(_safe_float(hist.get('forecast_type', 0)))

            cyq_str = f"{rt['cyq_concentration']:.1f}" if rt['cyq_concentration'] < 900 else "--"

            # ---------- P1 多维分项初筛 ----------
            df_tail5_p1 = df_target.tail(5)
            _tr5_p1 = []
            for _, rr in df_tail5_p1.iterrows():
                v = _safe_float(rr.get("vol"), 0.0)
                c = _safe_float(rr.get("close"), 0.0)
                cm = _safe_float(rr.get("circ_mv"), 0.0)
                t0 = _safe_float(rr.get("turnover_rate_f"), 0.0)
                _tr5_p1.append(t0 if t0 > 0 else infer_turnover_rate_f_pct(v, c, cm))
            avg_trn_scan_row = float(np.mean(_tr5_p1)) if _tr5_p1 else 0.0

            pe_raw_p1 = hist.get("pe_ttm")
            if pe_raw_p1 is None or pd.isna(pe_raw_p1) or str(pe_raw_p1).strip() in ("", "-"):
                pe_raw_p1 = hist.get("pe", 0)
            pe_p1 = _safe_float(pe_raw_p1)
            ind_stats_p1 = industry_pe_stats_scan.get(industry, global_stats_scan)

            p1_strategies_allowed = True
            try:
                p1_smooth, p1_ok, p1_reason, _p1_det_scan = compute_p1_multi_dim_smooth_score(
                    df_target, rt, circ_mv_yi, ind_rank, pe_p1, ind_stats_p1,
                    industry, dynamic_industries_scan, avg_trn_scan_row, float(p1_pass_line_scan),
                )
            except Exception as _ex_p1:
                p1_smooth, p1_ok = 0.0, False
                p1_reason = "平滑得分不达标"
                logging.debug("P1多维分项初筛异常 %s: %s", s_code, _ex_p1)
            if not p1_ok:
                p1_strategies_allowed = False
                _rollback_scan_funnel_for_skipped_stock(res, active_target_pools)
                res["p1_prescreen"]["smooth_blocked"] = int(res["p1_prescreen"].get("smooth_blocked", 0) or 0) + 1
                _gpre = res["p1_prescreen"]["gate_reasons"]
                _gpre["平滑得分不达标"] = int(_gpre.get("平滑得分不达标", 0) or 0) + 1
                logging.debug("P1初筛拦截 %s: %s score=%.2f", s_code, p1_reason, p1_smooth)
            p1_gene = float(p1_smooth)
            item["p1_score"] = float(p1_smooth)
            # 【2026-10-15 P4-06（旧P4-07）重写修复】把 P1 多维平滑分注入实时上下文 rt。
            #   根因：p1_score 只写进 item（UI 展示用），从未进入 rt；
            #   而 P4 战法链只拿得到 rt（core/scan/pipeline.py 之后 engine.run_all(df, rt)），
            #   ⇒ P4-06 读 rt.get("p1_score") 永远拿到默认 0.0 ⇒ 第一道门恒不成立。
            rt["p1_score"] = float(p1_smooth)

            empty_board_count = 0
            is_empty_board = False
            for _, row in df_tail_5.iterrows():
                row_pct = _safe_float(row.get('pct_chg', 0))
                row_high = _safe_float(row.get('high', 0))
                row_low = _safe_float(row.get('low', -1))
                if row_high == row_low and row_pct >= 9.0:
                    empty_board_count += 1
                    if empty_board_count >= 2:
                        is_empty_board = True
                else:
                    empty_board_count = 0

            decay_factor = _calc_decay_factor_atr(df_target, rt)

            vol_mean_pre = df_tail_60[vol_col].mean()
            vol_std_pre = df_tail_60[vol_col].std()
            curr_vol_pre = _safe_float(rt.get('vol', df_target[vol_col].iloc[-1] if not df_target.empty else 0))
            vol_z = (curr_vol_pre - vol_mean_pre) / vol_std_pre if pd.notna(vol_std_pre) and vol_std_pre > 0 else 0.0
            holding_days = holding_days_map.get(full_code, 0)

            trigger_ds, final_reason = would_trigger_danger_sell(
                df_target, rt, size_emoji, holding_days, vol_z, ind_rank
            )
            is_stop_loss = bool(trigger_ds)
            if trigger_ds:
                res['danger_sell'].append({
                    "代码": s_code, "名称": f"{size_emoji} {stock_name}", "现价": f"{now_price:.2f}",
                    "涨幅": f"{pct:.2f}%", "斩仓指令": final_reason
                })

            safety_factor, safety_tags = _calc_safety_factor(df_target, rt, regime, industry, sorted_sectors, limit_times_val, is_empty_board)
            safety_factor = max(0.5, min(1.2, safety_factor * size_safety_mult))

            _trf_disp = _safe_float(rt.get("turnover_rate_f", 0.0), 0.0)
            if not np.isfinite(_trf_disp) or _trf_disp < 0:
                _trf_disp = 0.0
            _vr_show = _safe_float(rt.get("vol_ratio", 1.0), 1.0)
            if not np.isfinite(_vr_show) or _vr_show <= 0:
                _vr_show = 1.0
            cs_ranks = rank_lookup.get(s_code)
            if not isinstance(cs_ranks, dict):
                cs_ranks = neutral_ranks()

            _rt_vwap = 0.0
            if _danger_buy_estimate_vwap_from_rt is not None:
                try:
                    _rt_vwap = float(_danger_buy_estimate_vwap_from_rt(rt, now_price))
                except Exception:
                    _rt_vwap = 0.0
            if _rt_vwap <= 0:
                _rt_vwap = _safe_float(rt.get("vwap", 0.0), 0.0)

            _last_row = df_target.iloc[-1] if df_target is not None and len(df_target) > 0 else {}

            base_info = {
                "代码": s_code, "名称": f"{size_emoji} {stock_name}", "行业": f"{industry}({ind_rank_str})",
                "股性": f"{size_emoji}{size_label}", "综合分": 0.0, "涨幅": f"{pct:.2f}%",
                "量比": f"{min(_vr_show, 50.0):.1f}({vol_ratio_tag})", "建议仓位": "", "纪律防线": "",
                "真换手": f"{_trf_disp:.1f}%", "集中度": cyq_str,
                "现价": f"{now_price:.2f}", "战法": "", "连板高度": f"{limit_times_val:.0f}",
                "机构预测": f"{int(_safe_float(rt.get('forecast_type', 0), 0))}",
                "vwap": round(_rt_vwap, 3) if _rt_vwap > 0 else None,
                "ma5": _safe_float(rt.get("ma5", _last_row.get("ma5", 0.0)), 0.0) or None,
                "ma10": _safe_float(rt.get("ma10", _last_row.get("ma10", 0.0)), 0.0) or None,
                "ma20": _safe_float(rt.get("ma20", _last_row.get("ma20", 0.0)), 0.0) or None,
                "ma60": _safe_float(rt.get("ma60", _last_row.get("ma60", 0.0)), 0.0) or None,
                "主力净额": _safe_float(rt.get("net_main_amount", 0.0), 0.0) or None,
            }

            scan_vwap_fish = False
            if _danger_buy_estimate_vwap_from_rt is not None and _breakout_vwap_eps_danger > 0:
                scan_vwap_fish = _danger_buy_vwap_fish_line_trigger(
                    now_price, pct, rt, curr_min, _breakout_vwap_eps_danger,
                )
            if scan_vwap_fish:
                pass  # 已禁用：仅依赖下方 danger_buy 表

            is_danger_buy_added = False

            # ==================== P2/P3/P4/P5 战法循环 ====================
            for pool_key, engine_obj in [('p2', p2_engine), ('p3', p3_engine), ('p4', p4_engine), ('p5', p5_engine)]:
                if not p1_strategies_allowed:
                    continue
                if pool_key in active_target_pools:
                    if pool_key == 'p3':
                        rt['rz_net_buy'] = 0.0
                    elif pool_key in ('p4', 'p5'):
                        rt['rz_net_buy'] = rz_net_buy_hist
                    else:
                        rt['rz_net_buy'] = 0.0
                    rt['_pool_key'] = pool_key
                    rt["curr_min"] = curr_min
                    rt["_market_contraction_score"] = market_contraction_score
                    rt["_regime_state"] = regime
                    rt["regime"] = regime
                    if pool_key in ("p2", "p3"):
                        rt["_t1_memory"] = (t1_memory.get(pool_key) if isinstance(t1_memory, dict) else {}) or {}
                    else:
                        rt.pop("_t1_memory", None)
                    try:
                        if hasattr(engine_obj, "run_all"):
                            hit_res = engine_obj.run_all(df_target, rt)
                        elif hasattr(engine_obj, "evaluate"):
                            hit_res = engine_obj.evaluate(df_target, rt)
                        else:
                            continue
                    except Exception as e:
                        logging.warning("战法单池跳过 %s - %s: %s", s_code, pool_key, e)
                        _record_scan_error(pool_key, s_code, "strategy_singleton", e)
                        continue

                    if hit_res is None: hit_res = {}

                    if isinstance(hit_res, dict): orig_hits = hit_res.get("strategies", [])
                    elif isinstance(hit_res, list): orig_hits = hit_res
                    else: orig_hits = []

                    if not isinstance(orig_hits, list): orig_hits = []

                    if orig_hits:
                        if isinstance(hit_res, dict):
                            burst_score = _safe_float(hit_res.get("burst_score", 90.0), default=90.0)
                            surge_bonus = _safe_float(hit_res.get("surge_bonus", 0.0))
                            penalty = _safe_float(hit_res.get("penalty", 0.0))
                        else:
                            burst_score = 90.0
                            surge_bonus = 0.0
                            penalty = 0.0
                    else:
                        burst_score = 0.0
                        surge_bonus = 0.0
                        penalty = 0.0

                    hits = orig_hits.copy()
                    p4_observation_only = bool(
                        pool_key == "p4"
                        and isinstance(hit_res, dict)
                        and (hit_res.get("data_quality") or {}).get("observation_only", False)
                    )
                    if p4_observation_only:
                        hits.append("👁️[尾盘数据不足]仅观察，不作正式买入信号")

                    # 金共振
                    if golden_engine is not None:
                        net_main_for_gold = _safe_float(rt.get("net_main_amount", hist.get("net_main_amount", 0.0)))
                        cyq = _safe_float(rt.get('cyq_concentration', hist.get('cyq_concentration', 999.0)))
                        try:
                            gold_result = golden_engine(df_target, rt, pool_key, ind_rank, 0.0, net_main_for_gold, cyq)
                            if isinstance(gold_result, tuple) and len(gold_result) >= 3:
                                gold_hits = gold_result[0]
                                gold_burst = gold_result[1]
                                gold_bonus = gold_result[2]
                            else:
                                gold_hits = gold_result if isinstance(gold_result, list) else []
                                gold_burst, gold_bonus = 0.0, 0.0
                            if gold_hits:
                                hits.extend(gold_hits)
                                burst_score = max(burst_score, gold_burst)
                                surge_bonus += gold_bonus
                        except Exception as e:
                            logging.debug("金共振函数调用失败(降级): %s", e)

                    # 分发层二次门禁
                    strict_gate = strict_golden_burst_ok(df_target, rt, pool_key)
                    gate_ok = strict_gate
                    core_screener_bypass = False
                    if pool_key == "p2" and isinstance(hit_res, dict) and hit_res.get("p2_core_screener_pass"):
                        gate_ok = True
                        core_screener_bypass = True
                    if pool_key == "p3" and isinstance(hit_res, dict) and hit_res.get("p3_core_screener_pass"):
                        gate_ok = True
                        core_screener_bypass = True
                    if pool_key == "p4" and isinstance(hit_res, dict) and hit_res.get("p4_core_screener_pass"):
                        gate_ok = True
                        core_screener_bypass = True
                    if pool_key == "p5" and isinstance(hit_res, dict) and hit_res.get("p5_core_screener_pass"):
                        gate_ok = True
                        core_screener_bypass = True
                    relaxed_gate_applied = False
                    if (not gate_ok) and market_contraction_score >= 0.7:
                        if adaptive_relaxed_golden_gate_ok(pool_key, df_target, rt, market_contraction_score):
                            gate_ok = True
                            relaxed_gate_applied = True
                    if (
                        hits
                        and (not gate_ok)
                        and item.get("_momentum_fast_lane")
                        and pool_key in ("p3", "p4")
                    ):
                        gate_ok = True
                        core_screener_bypass = True
                    if gate_ok and pool_key in res['funnel']:
                        res['funnel'][pool_key]["pass_golden_gate"] += 1
                    if hits and not gate_ok:
                        if pool_key in res['funnel']:
                            r = _infer_golden_gate_reason(pool_key, df_target, rt)
                            gr = res['funnel'][pool_key]["gate_block_reasons"]
                            gr[r] = int(gr.get(r, 0)) + 1
                        hits = []
                        burst_score = 0.0
                        surge_bonus = 0.0
                        penalty = 0.0

                    if hits and burst_score > 0:
                        if pool_key in res['funnel']:
                            res['funnel'][pool_key]["hit_strategy"] += 1
                        if circ_mv_yi < _p1_scan_min_circ_mv_yi():
                            continue
                        if pool_key in ("p4", "p5"):
                            uniq_hits = []
                            seen = set()
                            for h in hits:
                                if h not in seen:
                                    uniq_hits.append(h)
                                    seen.add(h)
                            # 【V35】聚合别名（旧02/03/05/12/13）已随编号重整从代码层删除，
                            # 命中清单不再含同义总标签，无需再做展示去重。
                            prioritized = [h for h in uniq_hits if (
                                "金·" in h or "实锤" in h or "九转序列" in h or "龙虎榜机构" in h
                                or "布林CCI" in h or "融资杠杆主力" in h or "主力真金" in h or "机构实锤" in h
                                or "★" in h
                            )]
                            hits = (prioritized[:3] if prioritized else uniq_hits[:3]) or hits

                        pw = pool_weight_map.get(pool_key, 1.0) * size_pool_boost
                        actual_penalty = 0.0 if is_core_sector else (penalty * size_penalty_factor)
                        has_golden = any("金·" in h for h in hits)

                        surge_eff = compress_surge_bonus(surge_bonus)
                        burst_adj = burst_soft_cap(float(burst_score))
                        burst_adj = dampen_burst_by_extremes(
                            burst_adj, p1_gene,
                            float(cs_ranks.get("vol_ratio_r", 0.5)),
                            float(cs_ranks.get("pct_r", 0.5)),
                            float(cs_ranks.get("turnover_f_r", 0.5)),
                            float(cs_ranks.get("main_ratio_r", 0.5)),
                        )
                        personality_multiplier = _evaluate_personality(
                            df_target, rt, pool_key,
                            vr_rank=float(cs_ranks.get("vol_ratio_r", 0.5)),
                        )
                        adjusted_burst = burst_adj * personality_multiplier

                        if not hits:
                            continue

                        # danger_buy 致死原因
                        if not is_danger_buy_added:
                            fatal_reasons = []
                            if scan_vwap_fish:
                                fatal_reasons.append(
                                    f"📉VWAP钓鱼线(现价低于分时均价>breakout_vwap_eps="
                                    f"{_breakout_vwap_eps_danger:.4f})"
                                )
                            if is_bottom_3: fatal_reasons.append("💀身处领跌板块")
                            if safety_factor <= 0.65: fatal_reasons.append("⚠️高位乖离严重")
                            if is_empty_board and has_golden: fatal_reasons.append("💣中空假龙诱多")
                            if personality_multiplier == 0.5: fatal_reasons.append("🩸暴跌熔断(破ATR极限)")
                            if fatal_reasons:
                                res['danger_buy'].append({
                                    "代码": s_code, "名称": f"{size_emoji} {stock_name}", "现价": f"{now_price:.2f}",
                                    "涨幅": f"{pct:.2f}%", "致死原因": " | ".join(fatal_reasons)
                                })
                                is_danger_buy_added = True

                        # 综合分权重：是否含金共振？
                        if is_empty_board and has_golden:
                            current_w_gene = w_gene
                            current_w_burst = w_burst
                            adjusted_burst -= 20.0
                            hits.append("💣[假龙重罚]")
                        elif has_golden:
                            current_w_gene = 0.35
                            current_w_burst = 0.65
                        else:
                            current_w_gene = w_gene
                            current_w_burst = w_burst

                        # 资金共振复合分
                        _crs_live = _safe_float(
                            rt.get("capital_resonance_score", y_bar.get("capital_resonance_score", 0.0)),
                            0.0,
                        )
                        if pool_key == "p2":
                            _crs_w = 0.05
                        elif pool_key == "p3":
                            _crs_w = 0.04
                        elif pool_key == "p4":
                            _crs_w = 0.035
                        else:
                            _crs_w = 0.03
                        _crs_add = _crs_live * _crs_w * float(pw)
                        _sector_beta = _safe_float((hit_res or {}).get("detail", {}).get("sector_beta", 1.0), 1.0)
                        if pool_key == "p4":
                            if _sector_beta >= 1.2:
                                sector_mult *= min(1.5, _sector_beta)
                            elif _sector_beta <= 0.9:
                                sector_mult *= max(0.7, _sector_beta)
                        raw_score = (
                            (p1_gene * current_w_gene)
                            + (adjusted_burst * pw * current_w_burst)
                            + surge_eff
                            - actual_penalty
                            + _crs_add
                        )
                        final_score = raw_score * regime_mult * sector_mult * decay_factor * safety_factor

                        # 拥挤度
                        hi_shadow_live = _safe_float(rt.get("high", now_price), now_price)
                        upper_shadow_pct_live = 0.0
                        pre_close_live = _safe_float(rt.get("pre_close", 0.0), 0.0)
                        if pre_close_live > 0:
                            upper_shadow_pct_live = (
                                (max(hi_shadow_live, now_price) - max(now_price, _safe_float(rt.get("open", now_price), now_price)))
                                / pre_close_live
                                * 100.0
                            )
                        crowd_score, crowd_mult, crowd_label = _calc_crowding_penalty(
                            pool_key=pool_key,
                            pct=pct,
                            vol_ratio=_safe_float(rt.get("vol_ratio", 1.0), 1.0),
                            turnover_f=_safe_float(rt.get("turnover_rate_f", 0.0), 0.0),
                            upper_shadow_pct=upper_shadow_pct_live,
                            circ_mv_yi=circ_mv_yi,
                        )
                        # P3 右侧防追高
                        p3_hard_filter = False
                        p3_guard_mult = 1.0
                        p3_guard_label = ""
                        p3_guard_tags: list = []
                        if pool_key == "p3":
                            p3_hard_filter, p3_guard_mult, p3_guard_label, p3_guard_tags = _p3_right_side_guard(
                                df_target, rt, hit_res=hit_res,
                                market_contraction_score=market_contraction_score,
                                crowd_score=crowd_score,
                            )
                            if p3_hard_filter:
                                if pool_key in res['funnel']:
                                    res['funnel'][pool_key]["gate_block_reasons"][p3_guard_label or "P3硬过滤"] = int(
                                        res['funnel'][pool_key]["gate_block_reasons"].get(p3_guard_label or "P3硬过滤", 0)
                                    ) + 1
                                hits = []
                                burst_score = 0.0
                                surge_bonus = 0.0
                                penalty = 0.0
                                continue
                        final_score = final_score * crowd_mult * p3_guard_mult

                        min_pass = float(min_pass_map.get(pool_key, 60.0))
                        if relaxed_gate_applied and (not strict_gate) and (not core_screener_bypass) and market_contraction_score >= 0.7:
                            min_pass = 55.0
                        if pool_key in res['funnel']:
                            res['funnel'][pool_key]["pass_score"] += 1
                        b = base_info.copy()
                        if pool_key == "p3":
                            b["主战法"] = "--"
                            b["辅助标签"] = "--"
                            b["观察项"] = "--"
                        b["综合分"] = round(final_score, 2)
                        if final_score < 85.0:
                            b["操盘提示"] = "综合分未达85，建议谨慎操作（仅供参考）"
                        else:
                            b["操盘提示"] = "--"

                        # 赔率 / EV
                        if isinstance(hit_res, dict):
                            _entry_q = hit_res.get("entry_quality") or {}
                            _ev_info = hit_res.get("expected_value") or {}
                            if _entry_q or _ev_info:
                                if "risk_reward_ratio" in _entry_q:
                                    b["赔率"] = round(float(_entry_q["risk_reward_ratio"]), 2)
                                if "entry_quality_score" in _entry_q:
                                    b["入场质量分"] = round(float(_entry_q["entry_quality_score"]), 1)
                                if "expected_value" in _ev_info:
                                    b["期望收益EV"] = round(float(_ev_info["expected_value"]), 3)
                                if "trade_grade" in _ev_info:
                                    b["交易评级"] = str(_ev_info["trade_grade"])
                                if _entry_q.get("stop_loss"):
                                    b["自动止损位"] = _entry_q["stop_loss"]
                                if _entry_q.get("take_profit"):
                                    b["自动止盈位"] = _entry_q["take_profit"]
                                b["_hit_res_full"] = hit_res

                        # 三层风控展示
                        if isinstance(hit_res, dict):
                            _rtags = hit_res.get("risk_tags") or []
                            if isinstance(_rtags, list):
                                b["风险标签"] = " ".join(str(t) for t in _rtags) if _rtags else "--"
                            else:
                                b["风险标签"] = str(_rtags) if _rtags else "--"
                            b["建议最低分"] = f"{_safe_float(hit_res.get('suggested_min_entry_score', 0.0), 0.0):.1f}"
                            if pool_key == "p4":
                                b["最大仓位比例"] = round(_safe_float(hit_res.get("max_position_ratio", 0.0), 0.0), 3)
                                b["尾盘数据质量"] = str((hit_res.get("data_quality") or {}).get("grade", "--"))
                            if pool_key == "p3":
                                _vz = hit_res.get("detail", {}).get("volume_zscore") or {}
                                _tag = (_vz.get("tag") or "") if isinstance(_vz, dict) else ""
                                _zval = (_vz.get("z_value")) if isinstance(_vz, dict) else None
                                _bonus = (_vz.get("bonus")) if isinstance(_vz, dict) else 0.0
                                if "removed" in str(_tag).lower() or _tag in ("zscore_removed", "zscore_removed_20260922"):
                                    b["量能爆能"] = "Z-Score已移除"
                                    b["量能爆能_z"] = 0.0
                                    b["量能爆能_bonus"] = 0.0
                                elif _zval is not None:
                                    b["量能爆能"] = f"Z:{float(_zval):.1f}"
                                    b["量能爆能_z"] = _safe_float(_zval, 0.0)
                                    b["量能爆能_bonus"] = _safe_float(_bonus, 0.0)
                                else:
                                    b["量能爆能"] = str(_tag) if _tag else "--"
                                    b["量能爆能_z"] = 0.0
                                    b["量能爆能_bonus"] = 0.0
                            p4_md = _format_p4_trade_language(
                                hits,
                                stock_memory_score=_safe_float(hit_res.get("detail", {}).get("stock_memory_score", 0.0), 0.0),
                                sector_beta=_safe_float(hit_res.get("detail", {}).get("sector_beta", 1.0), 1.0),
                                close_vwap_dev=hit_res.get("detail", {}).get("close_vwap_dev_pct"),
                            )
                            p3_layers = _format_p3_trade_layers(hits) if pool_key == "p3" else None
                            if pool_key == "p4" and p4_md and p4_md != "--":
                                b["战法"] = p4_md
                            if pool_key == "p3" and isinstance(p3_layers, dict):
                                b["主战法"] = p3_layers.get("主战法", "--")
                                b["辅助标签"] = p3_layers.get("辅助标签", "--")
                                b["观察项"] = p3_layers.get("观察项", "--")
                            if pool_key == "p2":
                                b["主战法"] = " / ".join([h for h in hits if h and "✈️" not in h][:3]) or "--"
                                b["辅助标签"] = "--"
                                b["观察项"] = "--"
                            if pool_key in ("p3", "p4", "p5"):
                                _wb_detail = hit_res.get("detail") or {}
                                _wb_pts = round(_safe_float(_wb_detail.get("weekly_buff_pts", 0.0), 0.0), 2)
                                _wb_trend = round(_safe_float(_wb_detail.get("weekly_buff_trend", 0.0), 0.0), 2)
                                _wb_vol = round(_safe_float(_wb_detail.get("weekly_buff_vol", 0.0), 0.0), 2)
                                _wb_double = bool(_wb_detail.get("weekly_buff_double", False))
                                b["weekly_buff_pts"] = _wb_pts
                                b["weekly_buff_trend"] = _wb_trend
                                b["weekly_buff_vol"] = _wb_vol
                                b["weekly_buff_double"] = _wb_double
                                b["detail"] = _wb_detail
                                if _wb_double:
                                    _pt_now = str(b.get("操盘提示", "") or "").strip()
                                    _add = "🪐周线双强"
                                    if _pt_now and _pt_now != "--":
                                        b["操盘提示"] = f"{_pt_now} | {_add}"
                                    else:
                                        b["操盘提示"] = _add
                        else:
                            b["风险标签"] = "--"
                            b["建议最低分"] = "--"
                        if pool_key == "p3":
                            extra_tags = []
                            if p3_guard_label:
                                extra_tags.append(f"P3:{p3_guard_label}")
                            if p3_guard_tags:
                                extra_tags.extend([str(x) for x in p3_guard_tags if str(x).strip()])
                            if extra_tags:
                                _risk = str(b.get("风险标签", "") or "").strip()
                                _extra = " ".join(dict.fromkeys(extra_tags))
                                b["风险标签"] = f"{_risk} {_extra}".strip() if _risk and _risk != "--" else _extra
                                if p3_guard_label in ("强确认", "适度加分"):
                                    _pt = str(b.get("操盘提示", "") or "").strip()
                                    _add = "P3右侧确认"
                                    b["操盘提示"] = f"{_pt} | {_add}" if _pt and _pt != "--" else _add
                        b["拥挤度"] = f"{crowd_score:.0f}({crowd_label})"
                        if crowd_mult < 1.0:
                            _tag_c = str(b.get("风险标签", "") or "").strip()
                            _crowd_suffix = f"【拥挤降权x{crowd_mult:.2f}】"
                            b["风险标签"] = (
                                f"{_tag_c} {_crowd_suffix}".strip()
                                if _tag_c and _tag_c != "--"
                                else _crowd_suffix
                            )
                        exec_tier, exec_hint = _execution_tier_for_pool(
                            pool_key=pool_key, final_score=final_score, crowding_score=crowd_score,
                            relaxed_gate_applied=bool(relaxed_gate_applied and (not strict_gate) and (not core_screener_bypass)),
                        )
                        if p4_observation_only:
                            exec_tier = "C"
                            exec_hint = "尾盘快照不完整，仅观察，不执行买入"
                        b["执行层级"] = exec_tier
                        if exec_hint:
                            b["操盘提示"] = exec_hint if b.get("操盘提示", "--") == "--" else f"{b.get('操盘提示')} | {exec_hint}"
                            if pool_key in ("p4", "p5"):
                                if exec_tier == "A":
                                    b["建议仓位"] = "主仓候选: 20%-30%（分批）"
                                elif exec_tier == "B":
                                    b["建议仓位"] = "试错仓位: 8%-15%"
                                elif exec_tier == "C":
                                    b["建议仓位"] = "观察仓位: 0%-8%"
                        if relaxed_gate_applied and (not strict_gate) and (not core_screener_bypass):
                            _tag0 = str(b.get("风险标签", "") or "").strip()
                            _suffix = "【缩量辅助观察】"
                            b["风险标签"] = f"{_tag0} {_suffix}".strip() if _tag0 and _tag0 != "--" else _suffix

                        is_precise_pullback = "精准回踩" in " ".join(hits)
                        b["建议仓位"] = _calc_position(size_emoji, safety_factor, regime, is_precise_pullback)
                        if p4_observation_only:
                            b["建议仓位"] = "0%（仅观察）"

                        defense_line = "跌破20日线就先观察，不急着动"
                        b["纪律防线"] = defense_line

                        # === V29：注入出场计划（止损价 / 止盈价 / 最长持有），
                        #     使推送卡片与 signal_log 都能带上风控点位。
                        #     只附加字段，不改变任何分数/闸门/过滤结果。
                        try:
                            from core.exit_strategy import attach_exit_plan_to_res

                            _exit_res = {
                                "pool": pool_key,
                                "burst_score": float(b.get("综合分") or 0.0),
                                "strategies": list(hits or []),
                                "ts_code": s_code,
                                "price": now_price,
                            }
                            attach_exit_plan_to_res(_exit_res, push_price=now_price)
                            b["exit_plan"] = _exit_res.get("exit_plan")
                            b["exit_template"] = _exit_res.get("exit_template", "medium")
                        except Exception as _e_ep:
                            logging.debug("exit_plan 注入失败（忽略）: %s", _e_ep)

                        tags_str = _build_priority_tags(p1_gene, burst_score, surge_bonus, regime_mult, decay_factor, is_stop_loss, actual_penalty, is_core_sector, pool_key, safety_tags, personality_multiplier)

                        golden_hits = [h for h in hits if "金·短共振" in h or "金·月共振" in h]
                        other_hits = [h for h in hits if h not in golden_hits]

                        parts = []
                        if golden_hits:
                            parts.append(" + ".join(golden_hits))
                        if tags_str:
                            parts.append(tags_str)
                        if other_hits:
                            parts.append(" + ".join(other_hits))

                        b["战法"] = " | ".join(parts)
                        if pool_key == "p5" and isinstance(hit_res, dict):
                            p5_action = str(hit_res.get("primary_action", "") or hit_res.get("detail", {}).get("主动作", "") or "").strip()
                            p5_status = str(hit_res.get("market_status", "") or hit_res.get("detail", {}).get("市场状态", "") or "").strip()
                            p5_reason = str(hit_res.get("detail", {}).get("入池理由", "") or hit_res.get("detail", {}).get("买入原因", "") or "").strip()
                            p5_hint = str(hit_res.get("buy_hint", "") or hit_res.get("wechat_hint", "") or "").strip()
                            p5_prefix = " / ".join([x for x in (p5_action, p5_status, p5_reason) if x and x != "--"])
                            if p5_prefix:
                                b["战法"] = f"{p5_prefix} | {b['战法']}" if b.get("战法") else p5_prefix
                            if p5_hint:
                                old_tip = str(b.get("操盘提示", "") or "").strip()
                                b["操盘提示"] = f"{old_tip} | {p5_hint}" if old_tip and old_tip != "--" else p5_hint
                        b["pool_tier"] = "observation" if p4_observation_only else "main"
                        if p4_observation_only:
                            b["建议仓位"] = "0%（仅观察）"
                            b["_explicit_observation"] = True
                            b["风险标签"] = f"{str(b.get('风险标签', '') or '--').strip()} 【仅观察】".strip()
                        b["名称"] = normalize_stock_display_name(b.get("名称", ""))
                        b["股性"] = normalize_stock_display_name(b.get("股性", ""))
                        b["战法"] = normalize_stock_display_name(b.get("战法", ""))
                        if pool_key == "p3" and isinstance(hit_res, dict):
                            save_p3_first_trigger_snapshot(
                                b, rt, hit_res, regime, final_score, actual_penalty, crowd_score,
                            )
                        # 【P0-1 2026-11-04】多层信号过滤器接入（默认 disabled / shadow，不拦截）。
                        # core/signal_filter.py 的五层过滤此前从未被调用（死代码），
                        # 现在通过 core/signal_filter_adapter 接入：
                        #   · disabled → 完全不做，行为与改造前完全一致（默认）
                        #   · shadow   → 只标注结论并统计 reject 率，不拦任何票
                        #   · enforce  → 真正拦截（需先验证不会误杀）
                        # 关键安全设计：缺数据时跳过该层而非拒绝，绝不因数据缺失清空信号。
                        # 注意：必须在本行之前完成，以便把过滤标签写进 b 再做行契约规范化。
                        _sf_verdict = None
                        try:
                            from core.signal_filter_adapter import evaluate_row as _sf_eval, should_block as _sf_block

                            _sf_verdict = _sf_eval(b, rt)
                            if _sf_verdict.enabled:
                                _sf_note = _sf_verdict.short()
                                if _sf_note:
                                    _tags = str(b.get("风险标签", "") or "").strip()
                                    b["风险标签"] = f"{_tags} | {_sf_note}".strip(" |") if _tags else _sf_note
                                # 统计 reject 分布（供校准阈值用）
                                _sf_stats = res.setdefault("signal_filter_stats", {})
                                _key = f"L{_sf_verdict.reject_layer}" if _sf_verdict.reject_layer else "pass"
                                _sf_stats[_key] = int(_sf_stats.get(_key, 0) or 0) + 1
                                _sf_stats["total"] = int(_sf_stats.get("total", 0) or 0) + 1
                                if _sf_block(_sf_verdict):
                                    _sf_stats["blocked"] = int(_sf_stats.get("blocked", 0) or 0) + 1
                                    logging.info(
                                        "[signal_filter] 拦截 %s %s L%s %s",
                                        pool_key, b.get("名称", ""), _sf_verdict.reject_layer,
                                        _sf_verdict.reject_reason,
                                    )
                                    _route_scored_pool_row(
                                        res, pool_key, _ensure_pool_table_row_contract(b),
                                        observation_only=True,
                                    )
                                    continue
                        except Exception as _sf_err:
                            # 过滤器异常绝不阻断选股
                            logging.warning("[signal_filter] 过滤异常（已放行）: %s", _sf_err)
                        normalized_row = _ensure_pool_table_row_contract(b)
                        _route_scored_pool_row(
                            res, pool_key, normalized_row, observation_only=p4_observation_only,
                        )
                        # 【V33.1 审计修复 · P1】P4 尾盘阶段门槛真正生效。
                        # tail_clock 给每个阶段定了 score_bar_delta（观察期 +2.5、尾窗 +2.0），
                        # 引擎把它写进 detail["phase_score_bar_delta"]，
                        # 但原来这个值只被塞进展示字段「建议最低分」，整条链路没有任何地方据此过滤
                        # → 「14:30–14:35 要求更高分」「14:45 之后要求更高分」等于空转。
                        # 这里把它叠进本池的入池门槛（只叠加增量，不改风控给出的绝对值，避免过冲）。
                        _p4_phase_bar = 0.0
                        if pool_key == "p4" and isinstance(hit_res, dict):
                            try:
                                _p4_phase_bar = float(
                                    (hit_res.get("detail") or {}).get("phase_score_bar_delta", 0.0) or 0.0
                                )
                            except (TypeError, ValueError):
                                _p4_phase_bar = 0.0
                        if final_score >= (min_pass + _p4_phase_bar) and not p4_observation_only:
                            # 【V30.3 2026-10-17】P5 盘后/补跑的业务日 = 日线数据所属交易日。
                            # 不能沿用写入时刻的墙钟：夜间 20:05 与次日补跑会打在两个不同日期上，
                            # 导致 T+N 回看、战法有效性与模拟盘建仓日整体错位。
                            _biz_dt = ""
                            try:
                                if pool_key in ("p5", "p1"):
                                    _biz_dt = str(_last_row.get("trade_date") or "").replace("-", "")[:8]
                            except Exception:
                                _biz_dt = ""
                            save_signal_log(
                                b, pool_key, regime,
                                f"S({safety_factor:.2f})*P({actual_penalty:.0f})*M({personality_multiplier:.2f})",
                                business_date=_biz_dt,
                            )

            # 未命中任何战法时仍要进入禁买表
            if scan_vwap_fish and (not is_danger_buy_added) and _danger_buy_estimate_vwap_from_rt is not None:
                _vw_only = float(_danger_buy_estimate_vwap_from_rt(rt, now_price))
                _reason_vwap_only = (
                    f"📉VWAP钓鱼线熔断：涨幅≥{DANGER_BUY_VWAP_MIN_DAY_PCT:.1f}%"
                    f"且现价低于当日VWAP超过 breakout_vwap_eps={_breakout_vwap_eps_danger:.4f}"
                    f"（VWAP≈{_vw_only:.2f}）"
                )
                res["danger_buy"].append({
                    "代码": s_code,
                    "名称": f"{size_emoji} {stock_name}",
                    "现价": f"{now_price:.2f}",
                    "涨幅": f"{pct:.2f}%",
                    "致死原因": _reason_vwap_only,
                })

        except Exception as scan_item_err:
            logging.warning("扫描引擎单票处理跳过 code=%s: %s", item.get('code', '?'), scan_item_err)
            _record_scan_error(",".join(active_target_pools) or "unknown",
                               item.get('code', '?'), "stock_item", scan_item_err)
            continue

    # ==================== 收尾：跨池加权 + EV 排序 + 主池排序 ====================
    _cross_pool_multiplier_map = {1: 1.00, 2: 1.12, 3: 1.26, 4: 1.45}
    try:
        _ts_to_pools = {}
        for _pk in active_target_pools:
            for _row in (res.get(_pk) or []):
                _tc = str(_row.get("代码", "") or "").strip()
                if _tc:
                    _ts_to_pools.setdefault(_tc, set()).add(_pk)
        for _pk in active_target_pools:
            for _row in (res.get(_pk) or []):
                _tc = str(_row.get("代码", "") or "").strip()
                _n_hits = len(_ts_to_pools.get(_tc, set()))
                if _n_hits >= 2:
                    _multi = _cross_pool_multiplier_map.get(min(_n_hits, 4), 1.0)
                    _row["综合分"] = round(float(_row.get("综合分", 0.0) or 0.0) * _multi, 2)
                    _row["_cross_pool_count"] = _n_hits
                    _row["_cross_pool_multiplier"] = _multi
                    if _n_hits >= 3:
                        _row["风险标签"] = (
                            str(_row.get("风险标签", "") or "") +
                            f" 🎯[多池命中×{_n_hits}]"
                        ).strip()

        from core.stop_loss_utils import compute_expected_value as _cev
        for _pk in active_target_pools:
            for _row in (res.get(_pk) or []):
                _hit_res = _row.get("_hit_res_full") or {}
                if _hit_res:
                    _entry_q = _hit_res.get("entry_quality") or {}
                    _ev_info = _hit_res.get("expected_value") or {}
                    _rr = _entry_q.get("risk_reward_ratio", 1.5) if _entry_q else 1.5
                    _sig = float(_hit_res.get("burst_score", 60.0) or 60.0)
                    _pen = float(_hit_res.get("penalty", 0.0) or 0.0)
                    _ev_calc = _cev(risk_reward=_rr, signal_strength=_sig, penalty=_pen)
                    if _ev_calc["expected_value_norm"] >= 60:
                        _row["综合分"] = round(float(_row.get("综合分", 0.0) or 0.0) * 1.15, 2)
                        _row["风险标签"] = (
                            str(_row.get("风险标签", "") or "") +
                            f" [EV{_ev_calc['trade_grade']}]"
                        ).strip()
                    elif _ev_calc["expected_value_norm"] < 30 and _ev_calc["expected_value"] < 0:
                        _row["综合分"] = round(float(_row.get("综合分", 0.0) or 0.0) * 0.85, 2)
    except Exception as _e_cp:
        logging.debug("cross_pool 加权异常（已降级）: %s", _e_cp)

    for k in active_target_pools:
        res[k] = sorted(res[k], key=lambda x: float(x.get("综合分", 0) or 0), reverse=True)
    for k in active_funnel_keys:
        res["funnel"][k]["total_candidates"] = int(funnel_total_candidates.get(k, 0) or 0)
        res["funnel"][k]["enter_strategy_check"] = int(funnel_enter_strategy_check.get(k, 0) or 0)
        if res["funnel"][k]["total_candidates"] == 0 and len(base_items_for_scan) > 0:
            res["funnel"][k]["total_candidates"] = len(base_items_for_scan)

    # 【多层级池子】观察池过滤
    total_main = sum(len(res.get(k) or []) for k in active_target_pools)
    real_fill_obs = (market_contraction_score >= 0.7) or (prev_scan_streak >= 1 and total_main == 0)
    if not real_fill_obs:
        res["observation"] = {
            k: [row for row in (res.get("observation", {}).get(k) or []) if row.get("_explicit_observation")]
            for k in active_target_pools
        }
    else:
        for k in active_target_pools:
            if k in res.get("observation", {}):
                res["observation"][k] = sorted(res["observation"][k], key=lambda x: x.get("综合分", 0), reverse=True)
    _merge_tier_meta_scan(total_main == 0)
    _persist_scan_attribution_snapshot(res, active_target_pools, regime)
    _finalize_scan_errors()

    # 【P0-1 2026-11-04】多层信号过滤器 shadow 期统计输出。
    #   shadow 模式只标注不拦截，所以「reject 分布」只能从日志里看 ——
    #   如果不打印，观察期就无从谈起（改了配置也不知道效果）。
    #   输出格式：各层拒绝数 + 拒绝率，一眼能看出哪层阈值过严。
    _sf_stats = res.get("signal_filter_stats") or {}
    if _sf_stats.get("total"):
        _sf_total = int(_sf_stats.get("total") or 0)
        _sf_pass = int(_sf_stats.get("pass") or 0)
        _sf_parts = [f"总{_sf_total} 通过{_sf_pass}"]
        for _k in sorted(k for k in _sf_stats if k.startswith("L")):
            _n = int(_sf_stats.get(_k) or 0)
            if _n:
                _sf_parts.append(f"{_k}:{_n}({_n / _sf_total * 100:.0f}%)")
        logging.info("[signal_filter][shadow] %s", " ".join(_sf_parts))
    elif res.get("signal_filter_stats"):
        logging.info("[signal_filter][shadow] 未产生统计（过滤器未启用或无候选）")

    # 【H1 Shadow 2026-11】情绪温度计影子记录（只记录不拦截）
    #   背景：SENTIMENT_POOL_MULTIPLIER 8×5 系数表此前零消费者。先按 Shadow 纪律
    #   记录「若接线情绪系数，今天的状态与各池系数是什么」，攒够样本后再评估接线。
    #   本调用绝不拦截/改分——任何异常静默降级。
    try:
        from core.sentiment_shadow import record_sentiment_shadow

        record_sentiment_shadow(res, active_target_pools, rt_map=rt_map)
    except Exception as _e_h1_shadow:
        logging.debug("sentiment shadow 跳过: %s", _e_h1_shadow)

    return res
