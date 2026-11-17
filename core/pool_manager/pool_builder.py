# -*- coding: utf-8 -*-
"""
core/pool_manager/pool_builder.py
原 core/pool_manager.py L1861-L2452

【职责】
- _TIER_META_KEY / _LAST_P1_OBSERVATION_POOL / _LAST_P1_WASH_ADAPTIVE（多层级池子元数据）
- _wash_metrics_path / _merge_tier_meta_p1（streak 持久化）
- get_last_p1_observation_pool / get_last_p1_wash_adaptive（对外读取观察池）
- build_p1_pool_and_cache（主流程 + 并发 + 落盘 + 退出模板注入 + signal_log）

【依赖】
- core.pool_manager.paths（path_p1_gene_json / path_wash_metrics_json / ensure_runtime_data_layout）
- core.pool_manager.thresholds（_p1_min_circ_mv_yi_pool / _p1_regime_profile_key / 等）
- core.pool_manager.single_stock（_process_single_stock_for_p1）
- core.pool_manager.blacklists（_get_unlock_blacklist / _get_punish_blacklist / GLOBAL_UNLOCK_BLACKLIST）
- core.pool_manager.strategic_mapping（_get_dynamic_strategic_industries）
- core.pool_manager.cache_sovereignty（暂未使用）
"""
import json
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

import pandas as pd

from core.pool_manager.blacklists import (
    GLOBAL_UNLOCK_BLACKLIST,
    _get_punish_blacklist,
    _get_unlock_blacklist,
)
from core.pool_manager.paths import (
    ensure_runtime_data_layout,
    path_p1_gene_json,
    path_wash_metrics_json,
)
from core.pool_manager.single_stock import _process_single_stock_for_p1
from core.pool_manager.thresholds import (
    P1_PASS_LINE,
    _build_sector_rank_map,
    _get_regime_thresholds,
    _p1_final_sort_key,
    _p1_min_circ_mv_yi_pool,
    _p1_mv_fail_reason_for,
    _p1_mv_yi_for_thr,
    _p1_regime_profile_key,
    _safe_float,
    get_all_basic_industry,
    get_latest_daily_data_trade_date_yyyymmdd,
    get_latest_sector_ranking,
    infer_turnover_rate_f_pct,
)
from core.stock_name_utils import normalize_stock_display_name

import constants

logger = logging.getLogger(__name__)

# ================= 并发调度与行业基准库 =================
# 【多层级池子】说明：与 wash_metrics_history.json 共用元数据键，避免新增独立文件；仅存 streak 与日期，供「连续无票」判定
_TIER_META_KEY = "__tier_pool_meta__"
_LAST_P1_OBSERVATION_POOL: List[Dict[str, Any]] = []
_LAST_P1_WASH_ADAPTIVE = {
    "adaptive_reason": "",
    "adaptive_sample_count": 0,
    "market_contraction_score": 0.0,
}


def _wash_metrics_path():
    return path_wash_metrics_json()


def _merge_tier_meta_p1(is_main_empty, _obs_active_today=False):
    """【多层级池子】说明：主 P1 池为空则累加连续无票日（同日多次洗盘不重复加一）；有票则清零。返回更新后的 streak。"""
    from core.file_utils import atomic_json_update

    # 【P1-修复】原写法 timezone(timedelta(hours=8)) if False else ... 依赖未导入的
    # timedelta，仅靠 "if False" 短路才不抛 NameError，属于会崩的定时炸弹，已改为显式导入。
    bj_tz = timezone(timedelta(hours=8))
    today_str = datetime.now(bj_tz).strftime("%Y%m%d")
    ret_box: Dict[str, Any] = {"streak": 0, "meta": {}, "obs_streak": 0}

    def _upd(root: Dict[str, Any]) -> None:
        meta = root.get(_TIER_META_KEY)
        if not isinstance(meta, dict):
            meta = {}
        streak = int(meta.get("p1_empty_streak", 0) or 0)
        last_d = str(meta.get("p1_last_empty_date", "") or "")
        if is_main_empty:
            if last_d != today_str:
                streak += 1
                meta["p1_last_empty_date"] = today_str
        else:
            streak = 0
            meta["p1_last_empty_date"] = ""
        meta["p1_empty_streak"] = streak

        # 【V26.88+10-3 P3-⑪】观察池激活 streak：通过参数 _obs_active_today 传入
        obs_streak = int(meta.get("p1_obs_streak", 0) or 0)
        last_obs_d = str(meta.get("p1_last_obs_date", "") or "")
        if _obs_active_today:
            if last_obs_d != today_str:
                obs_streak += 1
                meta["p1_last_obs_date"] = today_str
        else:
            obs_streak = 0
            meta["p1_last_obs_date"] = ""
        meta["p1_obs_streak"] = obs_streak

        root[_TIER_META_KEY] = meta
        ret_box["streak"] = streak
        ret_box["obs_streak"] = obs_streak
        ret_box["meta"] = meta

    try:
        ensure_runtime_data_layout()
        atomic_json_update(_wash_metrics_path(), _upd, timeout=5)
    except Exception as e:
        logger.error(f"[tier-meta-atomic] {e}")
    return int(ret_box["streak"]), dict(ret_box["meta"] or {})


def get_last_p1_observation_pool():
    """【多层级池子】说明：供 app 读取本轮洗盘产出的震荡观察池（不改变原 build 的二元组返回，避免破坏其它模块解包）。"""
    return list(_LAST_P1_OBSERVATION_POOL or [])


def get_last_p1_wash_adaptive():
    """【自适应优化】最近一次 P1 洗盘计算的市场收缩度上下文（供 UI 观察池行「缩量说明」与侧边栏追溯）。"""
    return dict(_LAST_P1_WASH_ADAPTIVE)


def build_p1_pool_and_cache(stock_data_list, progress_callback=None, regime_name=None, p1_threshold_override=None):
    """P1 主洗盘流程：并发处理单票 + 排序 + 落盘 + 多层级观察池"""
    global GLOBAL_UNLOCK_BLACKLIST
    global _LAST_P1_OBSERVATION_POOL
    global _LAST_P1_WASH_ADAPTIVE
    _LAST_P1_OBSERVATION_POOL = []
    _LAST_P1_WASH_ADAPTIVE = {
        "adaptive_reason": "",
        "adaptive_sample_count": 0,
        "market_contraction_score": 0.0,
    }

    bj_tz = timezone(timedelta(hours=8))
    calendar_yyyymmdd = datetime.now(bj_tz).strftime("%Y%m%d")
    p1_anchor = (get_latest_daily_data_trade_date_yyyymmdd() or "").strip()
    if len(p1_anchor) != 8 or not p1_anchor.isdigit():
        p1_anchor = calendar_yyyymmdd
        logger.warning("[p1-anchor-fallback] %s", p1_anchor)

    # 【F0-2-A 修复】非交易日早退：节假日/长假/周末直接跳过 build，避免 fetch_realtime_batch 在非交易日被腾讯/新浪限流、推 1109 只 rt 为空 → 全部死在 single_stock "📡实时快照为空" 闸。
    # 三重判定（按可靠性排序）：
    #  (1) DuckDB anchor 与今天的日差：>4 天必为长假/春节/国庆；<=4 天不可靠（DuckDB 锁时 anchor 为空）。
    #  (2) 直接判断 weekday：>=5 必为周末，与数据库无关。
    #  (3) 三层兜底：以上两者都拿不到（极少见），按交易日走，避免误判。
    _is_holiday_window = False
    try:
        _today_dt = datetime.now(bj_tz).date()
        _weekday = _today_dt.weekday()
        if _weekday >= 5:
            # 【V26.88+10-3 修复-2 P0-⑭】用户硬约束：取消周末/节假日早退。
            # 原逻辑：weekday>=5 直接 return ([], [])
            # 新逻辑：仅记录"周末运行"标记，不早退；走完主流程选出票。
            # 原因：用户希望在国庆 / 周末也能跑 P1 验证主池选股能力（不实际买入）。
            logger.info(
                "[p1-holiday-force-run][V26.88+10-3] today=%s 是周末(weekday=%d), 不早退，继续 P1 build（仅验证用）",
                calendar_yyyymmdd, _weekday,
            )
            _LAST_P1_WASH_ADAPTIVE["adaptive_reason"] = "weekend_force_run"
            _is_holiday_window = True

        if p1_anchor and len(p1_anchor) == 8 and p1_anchor.isdigit():
            _anchor_dt = datetime.strptime(p1_anchor, "%Y%m%d").date()
            _gap_days = (_today_dt - _anchor_dt).days
            if _gap_days >= 3:
                # 【V26.88+10-3 修复-2 P0-⑭】用户硬约束：取消长假期早退。
                # 原逻辑：gap>=3 直接 return ([], [])
                # 新逻辑：仅记录"长假期运行"标记，不早退；启用<60 天假期宽容闸门继续 P1 build。
                logger.info(
                    "[p1-holiday-force-run][V26.88+10-3] today=%s anchor=%s gap=%d 天 (长假), 不早退，继续 P1 build（仅验证用）",
                    calendar_yyyymmdd, p1_anchor, _gap_days,
                )
                _is_holiday_window = True
                _LAST_P1_WASH_ADAPTIVE["adaptive_reason"] = f"long_holiday_force_run: gap={_gap_days}d"
            elif _gap_days >= 1:
                # 【F2-5 数据】anchor 与 today 差 1~2 天（短假期/国庆前两天/中秋）：不早退，但
                # 把 holiday_window 标记透传给 single_stock 闸门，让 100 天硬卡自动降到 60。
                # 这样做的好处是清明/五一/中秋/国庆前两天（gap=1/2）不会全军死在 len(df)<100。
                _is_holiday_window = True
                logger.info(
                    "[p1-holiday-tolerance][F2-5] today=%s anchor=%s gap=%d 天 (短假期) → 启用日线 <60 宽容闸门",
                    calendar_yyyymmdd, p1_anchor, _gap_days,
                )
                _LAST_P1_WASH_ADAPTIVE["adaptive_reason"] = f"holiday_tolerance: gap={_gap_days}d"
    except Exception as _e_hs:
        logger.debug("[p1-holiday-skip] 节假日探测异常，按交易日处理: %s", _e_hs)

    # 【F2-2】数据完整度启动闸：>=5000 候选进来时做一次抽样验证，data 残缺就直接报警退出，避免静默失败。
    try:
        if isinstance(stock_data_list, list) and len(stock_data_list) >= 5000:
            _samp = stock_data_list[:200]
            _ok = 0
            for _it in _samp:
                if not isinstance(_it, dict):
                    continue
                _df = _it.get("df")
                if isinstance(_df, pd.DataFrame) and not _df.empty and len(_df) >= 100:
                    _ok += 1
            _ok_ratio = _ok / max(len(_samp), 1)
            if _ok_ratio < 0.5:
                logger.error(
                    "[p1-data-quality-check][F2-2] 抽样200只日线<100天比例%.1f%%(<50%%)，疑似数据库长时间未同步",
                    (1.0 - _ok_ratio) * 100.0,
                )
                _LAST_P1_WASH_ADAPTIVE["adaptive_reason"] = f"data_quality_alert: ok={_ok_ratio:.0%}"
                try:
                    from core.notification_gateway import notify_wechat_system_alert
                    notify_wechat_system_alert(
                        title="P1 启动闸: 日线数据完整度不足 50%",
                        detail=f"抽样 200 只里仅 {_ok}/200 有 ≥100 天日线 ({_ok_ratio:.0%})。疑似数据库长时间未补齐或 DuckDB 锁住。",
                        dedup_key="",
                    )
                except Exception:
                    pass
                if progress_callback:
                    try:
                        progress_callback(1.0)
                    except Exception:
                        pass
                return ([], [])
            else:
                logger.info(
                    "[p1-data-quality-check][F2-2] 抽样 200 只, %d 只日线>=100天 (%.0f%%)，放行",
                    _ok, _ok_ratio * 100.0,
                )
    except Exception as _e_dq:
        logger.debug("[p1-data-quality-check] 校验异常按原 list 处理: %s", _e_dq)

    # 【F2-12】数据缺口提醒：当 anchor 与 today 差 1~N 天时，把 gap 状态写进 _LAST_P1_WASH_ADAPTIVE，
    # UI 侧栏可读取并在"自适应原因"列展示"⚠️数据缺口 N 天"——这样用户能立刻看到为什么 70% 出局是数据问题。
    try:
        _today_dt2 = datetime.now(bj_tz).date()
        if p1_anchor and len(p1_anchor) == 8 and p1_anchor.isdigit():
            _anchor_dt2 = datetime.strptime(p1_anchor, "%Y%m%d").date()
            _gap_dt2 = (_today_dt2 - _anchor_dt2).days
            if _gap_dt2 >= 1:
                _LAST_P1_WASH_ADAPTIVE["data_freshness_gap_days"] = int(_gap_dt2)
                _LAST_P1_WASH_ADAPTIVE["data_freshness_anchor"] = p1_anchor
                _LAST_P1_WASH_ADAPTIVE["data_freshness_today"] = calendar_yyyymmdd
                logger.warning(
                    "[p1-data-freshness][F2-12] 数据缺口: anchor=%s today=%s gap=%d 天 (建议补全后再跑 P1 主池；当前已启用 <60 假期宽容)",
                    p1_anchor, calendar_yyyymmdd, _gap_dt2,
                )
    except Exception as _e_df:
        logger.debug("[p1-data-freshness] 探测异常: %s", _e_df)

    if progress_callback:
        progress_callback(0.01)
    unlock_blacklist = _get_unlock_blacklist(p1_anchor)
    GLOBAL_UNLOCK_BLACKLIST = unlock_blacklist

    try:
        from core.config_manager import get_p1_respect_scan_blacklist

        _respect_bl = bool(get_p1_respect_scan_blacklist())
    except Exception:
        _respect_bl = False
    punish_blacklist: Set[str] = _get_punish_blacklist(anchor_yyyymmdd=p1_anchor) if _respect_bl else set()
    if not _respect_bl:
        logger.info("[p1-respect-bl] respect_scan_blacklist_for_p1=false")

    thresholds = dict(_get_regime_thresholds(regime_name))
    if isinstance(p1_threshold_override, dict) and p1_threshold_override:
        for k, v in p1_threshold_override.items():
            try:
                thresholds[k] = float(v)
            except (TypeError, ValueError):
                thresholds[k] = v
    thresholds["_p1_profile_key"] = _p1_regime_profile_key(regime_name)
    thresholds["_p1_min_circ_mv_yi"] = float(_p1_min_circ_mv_yi_pool())
    # 【F2-5】长假期日线宽容闸门：当 anchor 与今天差 1~2 天（短假期）时，让 single_stock 的 len(df)<100
    # 硬卡自动降级为 len(df)<60。这是 70.3% 出局的根治方案——日线 < 100 不再是数据问题，而是假期问题。
    # 【P1-修复】原本用 locals().get("_is_holiday_window") 取值，脆弱且可读性差；
    # 该变量在上面的 try 之前已显式初始化，直接引用即可。
    thresholds["_p1_is_holiday_window"] = bool(_is_holiday_window)

    try:
        from core.config_manager import get_p1_fund_memory_weight, get_p1_select_min_circ_mv_wan

        _log_mv_wan = int(get_p1_select_min_circ_mv_wan())
        _log_w_fm = float(get_p1_fund_memory_weight())
    except Exception:
        _log_mv_wan = int(getattr(constants, "STRATEGY_MIN_CIRC_MV_WAN", 1_200_000))
        _log_w_fm = float(getattr(constants, "FUND_MEMORY_WEIGHT_P1", 0.17))
    logger.info(
        "P1 build 参数: pass_line=%.1f profile=%s select_min_circ_mv_wan=%d fund_memory_weight_p1=%.4f 输入候选数=%d",
        float(thresholds.get("pass_line", 50.0)),
        str(thresholds.get("_p1_profile_key", "")),
        _log_mv_wan,
        _log_w_fm,
        len(stock_data_list) if stock_data_list else 0,
    )

    # 【自适应优化】仅当「库内最新交易日 = 当日日历」时拉实时算收缩度
    _mcs_wash = 0.0
    skip_rt_mcs = p1_anchor != calendar_yyyymmdd
    if skip_rt_mcs:
        logger.info(
            "[p1-anchor-mcs-skip] %s ≠ %s",
            p1_anchor,
            calendar_yyyymmdd,
        )
    try:
        from data.api_fetcher import fetch_realtime_batch as _fetch_rt_for_mcs
    except Exception:
        _fetch_rt_for_mcs = None
    if (not skip_rt_mcs) and _fetch_rt_for_mcs and stock_data_list:
        _codes_mcs = [
            it.get("code")
            for it in stock_data_list
            if isinstance(it, dict) and it.get("code")
        ]
        if _codes_mcs:
            try:
                _rtm_wash = _fetch_rt_for_mcs(_codes_mcs) or {}
                from core.pool_manager.thresholds import compute_market_contraction_context
                _ctx_wash = compute_market_contraction_context(stock_data_list, _rtm_wash)
                _mcs_wash = float(_ctx_wash.get("score", 0.0) or 0.0)
                _LAST_P1_WASH_ADAPTIVE.update(
                    {
                        "adaptive_reason": str(_ctx_wash.get("adaptive_reason", "") or ""),
                        "adaptive_sample_count": int(_ctx_wash.get("sample_count", 0) or 0),
                        "market_contraction_score": _mcs_wash,
                    }
                )
            except Exception as _e_mcs:
                logger.debug("[mcs-fail] %s", _e_mcs)
    thresholds["_market_contraction_score"] = _mcs_wash

    p1_pool = []
    p1_rejected = []
    fast_track_pool = []
    p1_gene_dict = {}
    # 【P1-修复】与 p1_rejected 并行的「原始 item」表，key=id(rejected_dict)。
    # 用于 min_score 动态降档回调时还原完整行情 item（code/hist/df）。
    # 之所以不能直接塞进 p1_rejected：UI 会 json.dump 整个 p1_rejected，DataFrame 不可序列化。
    _p1_rejected_src: Dict[int, Dict[str, Any]] = {}

    if not stock_data_list:
        # 【多层级池子】说明：无候选时仍返回二元组，观察池通过 get_last_p1_observation_pool 为空列表
        return p1_pool, p1_rejected

    end_date_str = p1_anchor
    optimal_workers = min(32, (os.cpu_count() or 4) * 2)
    workers = getattr(constants, 'MAX_WORKERS', optimal_workers)

    industry_map = get_all_basic_industry()

    # 【V26.80 性能优化】缓存化的板块排名读取
    try:
        from core.p1_cache import cached_get_sector_ranking
        sector_ranking_dict = cached_get_sector_ranking(get_latest_sector_ranking)
    except Exception:
        sector_ranking_dict = get_latest_sector_ranking()
    sorted_sectors = list(sector_ranking_dict.keys())
    sector_rank_map = _build_sector_rank_map(sorted_sectors)

    # 【V26.80 性能优化】缓存化的动态战略行业计算
    try:
        from core.p1_cache import cached_get_dynamic_industries
        from core.pool_manager.strategic_mapping import _get_dynamic_strategic_industries
        dynamic_industries = cached_get_dynamic_industries(
            _get_dynamic_strategic_industries,
            sector_ranking_dict,
            p1_anchor,
        )
    except Exception:
        from core.pool_manager.strategic_mapping import _get_dynamic_strategic_industries
        dynamic_industries = _get_dynamic_strategic_industries(
            sector_ranking_dict, p1_anchor_yyyymmdd=p1_anchor
        )

    # 【V26.80 性能优化】批量预计算 PE 分位 + 市值档位 + 资金流入
    # 【P1-修复】_limit_up_map 必须在 try 之前初始化：原代码只在 try 成功分支里赋值，
    # 一旦 batch_precompute_limit_up_signals 之前任何一步抛错并进入 except，
    # 后面 fast-track 配额处读 _limit_up_map 会抛 NameError 把整轮 P1 打断。
    _limit_up_map: Dict[str, Any] = {}
    try:
        from core.p1_precompute import (
            batch_compute_pe_quantiles,
            batch_precompute_mv_tier,
            batch_precompute_inflow,
            batch_attach_precomputed,
            batch_precompute_limit_up_signals,
            attach_limit_up_signals,
            compute_market_overheat_score,
        )

        _pe_result = batch_compute_pe_quantiles(stock_data_list, industry_map)
        global_stats = _pe_result["global_pe_stats"]
        industry_pe_stats = _pe_result["industry_pe_stats"]

        _mv_tier_map = batch_precompute_mv_tier(stock_data_list)
        _inflow_map = batch_precompute_inflow(stock_data_list)
        batch_attach_precomputed(stock_data_list, _mv_tier_map, _pe_result, _inflow_map)

        _limit_up_map = batch_precompute_limit_up_signals(stock_data_list)
        attach_limit_up_signals(stock_data_list, _limit_up_map)

        _overheat = compute_market_overheat_score()
        for item in stock_data_list:
            item["_pre_market_overheat"] = _overheat
        logger.info(
            "P1 预计算完成: PE 分位 %d 行业 / 资金流入 %d 票 / 市值档位 %d 票 / 涨停池 %d 票 / 大盘温度 %d",
            len(industry_pe_stats), len(_inflow_map), len(_mv_tier_map), len(_limit_up_map),
            _overheat.get("market_overheat_score", 0),
        )
    except Exception as _e_pre:
        logger.warning("[p1-precompute-fallback] %s", _e_pre)
        # 降级：原始 for 循环
        pe_records = []
        for item in stock_data_list:
            ts_code = item.get('code', '')
            ind = industry_map.get(ts_code, '未知')

            pe_raw = item.get('hist', {}).get('pe_ttm')
            if pe_raw is None or pd.isna(pe_raw) or str(pe_raw).strip() in ['', '-']:
                pe_raw = item.get('hist', {}).get('pe', 0)

            pe = _safe_float(pe_raw)
            if pe <= 0 or pe > 500: continue
            pe_records.append({'ind': ind, 'pe': pe})

        df_pe = pd.DataFrame(pe_records)

        global_stats = {
            'median': df_pe['pe'].median() if not df_pe.empty else 20.0,
            'q30': df_pe['pe'].quantile(0.30) if not df_pe.empty else 15.0,
            'q50': df_pe['pe'].quantile(0.50) if not df_pe.empty else 20.0,
            'q75': df_pe['pe'].quantile(0.75) if not df_pe.empty else 30.0,
            'q80': df_pe['pe'].quantile(0.80) if not df_pe.empty else 35.0
        }

        industry_pe_stats = {}
        if not df_pe.empty:
            for ind, group in df_pe.groupby('ind'):
                if len(group) >= 10:
                    industry_pe_stats[ind] = {
                        'median': group['pe'].median(),
                        'q30': group['pe'].quantile(0.30),
                        'q50': group['pe'].quantile(0.50),
                        'q75': group['pe'].quantile(0.75),
                        'q80': group['pe'].quantile(0.80)
                    }
                else:
                    industry_pe_stats[ind] = global_stats

    # 【V30.3 2026-10-17】输入去重：同一 code 只打分一次。
    # 根因：UI 洗盘/多路合并可能把同一只票重复塞进 stock_data_list；重复行会原样进入
    # p1_pool → p1_cache JSON 出现重复 code → P5 扫描逐行处理 → 结果与 signal_log 重复。
    # 去重只影响重复项，不改变任何评分/闸门。
    _seen_in_codes = set()
    _uniq_stock_list = []
    for _it_src in (stock_data_list or []):
        _c_src = str((_it_src or {}).get("code") or (_it_src or {}).get("ts_code") or "").strip()
        if _c_src and _c_src in _seen_in_codes:
            continue
        if _c_src:
            _seen_in_codes.add(_c_src)
        _uniq_stock_list.append(_it_src)
    if len(_uniq_stock_list) != len(stock_data_list or []):
        logger.warning(
            "[p1-input-dedup] 输入重复 %d 行已合并（%d → %d）",
            len(stock_data_list or []) - len(_uniq_stock_list),
            len(stock_data_list or []), len(_uniq_stock_list),
        )
    stock_data_list = _uniq_stock_list
    total_tasks = len(stock_data_list)
    completed_tasks = 0
    lock = threading.Lock()

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(
                _process_single_stock_for_p1,
                item, industry_pe_stats, global_stats, industry_map, sector_rank_map,
                dynamic_industries, unlock_blacklist, punish_blacklist, thresholds
            ): item
            for item in stock_data_list
        }

        for future in as_completed(futures):
            with lock:
                completed_tasks += 1
                if progress_callback and completed_tasks % max(1, total_tasks // 100) == 0:
                    progress_callback(completed_tasks / total_tasks)

            try:
                res = future.result()
            except Exception as exc:
                logger.warning("[p1-future-fail] %s", exc)
                continue

            with lock:
                if res:
                    ts_code = res["ts_code"]
                    score = res["score"]
                    p1_gene_dict[ts_code] = score

                    if res["is_pass"]:
                        hit_item = res["item"]
                        hit_item['p1_score'] = score
                        hit_item['inflow_ratio'] = res.get("inflow_ratio", 0.0)
                        hit_item['inflow_10d_ratio'] = res.get("inflow_10d_ratio", 0.0)
                        hit_item['df'] = res["df"]
                        hit_item["score_details"] = dict(res.get("score_details") or {})

                        if res.get("used_fast_track", False):
                            # 【V26.88+10-8 新增】倍量阳线 → 100 分插队；其他快车道（涨停等）仍 85
                            if "倍量阳线" in str(res.get("score_details", {})):
                                hit_item['p1_score'] = 100.0
                            else:
                                hit_item['p1_score'] = max(score, 85.0)
                            fast_track_pool.append(hit_item)
                        else:
                            p1_pool.append(hit_item)
                    else:
                        s_code = ts_code.split('.')[0][:6]
                        stock_name = normalize_stock_display_name(
                            res.get("item", {}).get("hist", {}).get("name", s_code)
                        )

                        score_details = res.get("score_details", {})
                        try:
                            from core.p1_score_display import p1_score_details_to_extreme_labels, score_details_json_safe
                            top_1, top_2, worst_1, worst_2 = p1_score_details_to_extreme_labels(score_details, score)
                            _sdj = score_details_json_safe(score_details)
                        except Exception:
                            top_1 = top_2 = worst_1 = worst_2 = ""
                            _sdj = {}

                        _rej_row = {
                            "代码": s_code,
                            "名称": stock_name,
                            "淘汰死因": (
                                f"{int(thresholds.get('_p1_min_circ_mv_yi', _p1_min_circ_mv_yi_pool()))}亿以下流通市值全局拦截"
                                if "流通市值不足" in str(res.get("reason", ""))
                                else res["reason"]
                            ),
                            "被裁阶段": "第五层打分" if score > 0 else "前置防线",
                            "当前得分": round(float(score), 2),
                            "满分项1": top_1,
                            "满分项2": top_2,
                            "最低项1": worst_1,
                            "最低项2": worst_2,
                            "score_details": _sdj,
                        }
                        # 【批次 A】死因分级标注（纯展示层，不影响任何判定）
                        # 原文一字不动，只加三个字段：层级 / 建议 / 徽标。
                        try:
                            from core.p1_death_cause_registry import (
                                classify_death_cause as _cdc_p1,
                            )

                            _ci_p1 = _cdc_p1(_rej_row.get("淘汰死因"))
                            _rej_row["死因层级"] = _ci_p1.get("level")
                            _rej_row["处置建议"] = _ci_p1.get("advice")
                            _rej_row["层级徽标"] = "%s %s" % (
                                _ci_p1.get("badge"), _ci_p1.get("level_cn"))
                        except Exception:
                            _rej_row["死因层级"] = "L3"
                            _rej_row["处置建议"] = "未分类（请补登记）"
                            _rej_row["层级徽标"] = "🟠 降权级"
                        p1_rejected.append(_rej_row)
                        # 【P1-修复】留存原始 item，供 min_score 降档回调时还原 code/hist/df
                        _src_item = res.get("item")
                        if isinstance(_src_item, dict):
                            _p1_rejected_src[id(_rej_row)] = _src_item

    # 🛑 Fast-Lane 动态配额【V26.88+10-3 P2-⑧】
    # 根据全市场涨停池规模动态调整直通车数量（涨停池 ≤10 只时 8 只、≤30 时 5 只、>30 时 3 只）
    # 同时约束同行业直通车最多 2 只（避免被单一题材绑死）
    fast_track_pool.sort(key=lambda x: x.get('inflow_ratio', 0.0), reverse=True)
    try:
        _limit_up_pool_size = len(_limit_up_map or {}) if isinstance(_limit_up_map, dict) else 0
    except Exception:
        _limit_up_pool_size = 0
    if _limit_up_pool_size <= 10:
        _max_fast = 8
    elif _limit_up_pool_size <= 30:
        _max_fast = 5
    else:
        _max_fast = 3

    # 同行业分散：每个行业最多 2 只
    _ind_counter: Dict[str, int] = {}
    accepted_fast_tracks: List[Dict[str, Any]] = []
    rejected_fast_tracks: List[Dict[str, Any]] = []
    for _ft in fast_track_pool:
        _ft_industry = _ft.get("hist", {}).get("industry") or _ft.get("_pre_ind") or "未知"
        if len(accepted_fast_tracks) >= _max_fast:
            rejected_fast_tracks.append(_ft)
            continue
        if _ind_counter.get(_ft_industry, 0) >= 2:
            rejected_fast_tracks.append(_ft)
            continue
        accepted_fast_tracks.append(_ft)
        _ind_counter[_ft_industry] = _ind_counter.get(_ft_industry, 0) + 1

    for ft_item in rejected_fast_tracks:
        s_code = str(ft_item.get('code', '')).split('.')[0][:6]
        stock_name = normalize_stock_display_name(ft_item.get("hist", {}).get("name", s_code))
        _ft_industry = ft_item.get("hist", {}).get("industry") or ft_item.get("_pre_ind") or "未知"
        # 区分"行业超载"和"全局超载"两种拒绝原因
        if _ind_counter.get(_ft_industry, 0) >= 2:
            _reason = f"🛑直通车超载保护 (同行业{_ft_industry}已满2只)"
        else:
            _reason = f"🛑直通车超载保护 (全市场限流{_max_fast}只，名额已满拦截)"
        _rej_row = {
            "代码": s_code,
            "名称": stock_name,
            "淘汰死因": _reason,
            "被裁阶段": "全局限流防线",
            "当前得分": round(float(ft_item.get('p1_score', 0) or 0), 2),
            "满分项1": "--",
            "满分项2": "--",
            "最低项1": "--",
            "最低项2": "--"
        }
        # 【批次 A】直通车超载属于「名额限制」，不是票的问题
        _rej_row["死因层级"] = "L1"
        _rej_row["处置建议"] = "⚪ 非票的问题：限流已满，明日可重试"
        _rej_row["层级徽标"] = "⚪ 否决级"
        p1_rejected.append(_rej_row)
        # 【P1-修复】直通车被限流淘汰时同样留存原始 item（ft_item 本身即完整 hit_item）
        if isinstance(ft_item, dict):
            _p1_rejected_src[id(_rej_row)] = ft_item

    p1_pool.extend(accepted_fast_tracks)
    # 最终安全阀：进入 P1 池前再次物理过滤流通市值
    _p1_mv_final_bar = float(thresholds.get("_p1_min_circ_mv_yi", _p1_min_circ_mv_yi_pool()))
    p1_pool = [
        x
        for x in p1_pool
        if (_safe_float(x.get('hist', {}).get('circ_mv', x.get('hist', {}).get('total_mv', 0) * 0.6)) / max(10000.0, 1e-9))
        >= _p1_mv_final_bar
    ]

    # [V26.88+9-29 ST-P1-N 改造] P1 池准入分数闸
    _p1_min_score = None
    try:
        from core.config_manager import get_config
        _cfg = get_config()
        _v = (((_cfg or {}).get("strategies") or {}).get("p1") or {}).get("min_score")
        if _v is not None:
            _v = float(_v)
            if _v > 0.0:
                _p1_min_score = _v
    except Exception:
        _p1_min_score = None
    _before_count = len(p1_pool)
    if _p1_min_score is not None and _p1_min_score > 0.0:
        p1_pool = [x for x in p1_pool if float(x.get("p1_score", 0) or 0) >= float(_p1_min_score)]
        _after_count = len(p1_pool)
        if _after_count != _before_count:
            logger.info(
                "[P1 准入闸] min_score=%.1f 过滤: %d → %d 只 (踢掉 %d 只分数过低)",
                float(_p1_min_score), _before_count, _after_count, _before_count - _after_count
            )

    # 【V30.14 2026-11-05 P1-池不设上限】
    # 用户口径变更：主池数量**不再封顶**，分数达标就入池；120 亿流通市值下限保持不变。
    #
    # 为什么去掉「≥30 只」这个硬目标：
    #   旧逻辑会在池子不足 30 只时**主动下调 min_score 凑数**，等于为了凑数量而降低质量，
    #   与「宁可少而精」的原则相反；而且它掩盖了真实问题——2026-11-05 实测入池 30 / 拒 1011，
    #   凑数只补进来 1 只，说明瓶颈在打分口径而不是数量。
    #
    # 现在的语义：
    #   · 上限：无（分数够就进，池子多大由当日符合条件的票数决定）
    #   · 下限：仅当池子**完全为空**时兜底补齐，避免空池把下游 P2/P3/P5 整链打死
    _MIN_P1_POOL_TARGET = 0
    _EMPTY_POOL_FALLBACK = 10
    _cur_min_score = _p1_min_score if _p1_min_score is not None else 48.0
    _p1_target = _MIN_P1_POOL_TARGET if _MIN_P1_POOL_TARGET > 0 else _EMPTY_POOL_FALLBACK
    if len(p1_pool) == 0:
        # 收集所有没过 min_score 但有 p1_score 的票（p1_pool 之外）→ 从 rejected 回调
        _shortfall = _p1_target - len(p1_pool)
        # 把 p1_rejected 转成可索引结构，并按当前得分降序
        _rejected_pool: List[Dict[str, Any]] = []
        for _r in p1_rejected:
            try:
                _score = float(_r.get("当前得分", 0) or 0)
            except (TypeError, ValueError):
                _score = 0.0
            # 【P1-修复】rejected 里只存了展示字段，必须从并行的 _src 还原真实 item，
            # 否则回填的 _new_item 没有 code/hist/df → 下游落库 KeyError: 'code'、UI KeyError: 'hist'。
            # 注意：不能用 rejected 自带键携带 item（含 DataFrame），因为 p1_rejected 会被
            # UI json.dump 序列化；因此用与 p1_rejected 同序的并行列表 _p1_rejected_src。
            _rejected_pool.append({"_r": _r, "_score": _score, "_src": _p1_rejected_src.get(id(_r)) or {}})
        _rejected_pool.sort(key=lambda x: x["_score"], reverse=True)

        # 降 min_score 步进：每步 -5 分，最低 _cur_min_score - 15（保证不破坏质量）
        _min_floor = max(_cur_min_score - 15.0, 33.0)
        _promoted: List[Dict[str, Any]] = []
        _step_size = 5.0
        for _drop in [5.0, 5.0, 5.0]:
            if len(p1_pool) + len(_promoted) >= _p1_target:
                break
            _threshold = _cur_min_score - _drop
            if _threshold < _min_floor:
                break
            for _rp in _rejected_pool:
                if len(p1_pool) + len(_promoted) >= _p1_target:
                    break
                if _rp["_score"] >= _threshold and _rp["_score"] < _cur_min_score:
                    _r_dict = _rp["_r"]
                    # 【P1-修复】以 _src_item（真实行情 item）为基底重建，
                    # 再覆盖打分字段；保证 code/hist/df 齐全，与 _process_single_stock_for_p1
                    # 产出的 hit_item 结构一致（旧代码只留 代码/名称，导致下游 KeyError）
                    _src = _rp.get("_src") or {}
                    _new_item = dict(_src) if _src else {}
                    _hist = _new_item.get("hist")
                    if not isinstance(_hist, dict):
                        _hist = {}
                        _new_item["hist"] = _hist
                    if not str(_new_item.get("code") or _new_item.get("ts_code") or "").strip():
                        _new_item["code"] = _r_dict.get("代码")
                    if not str(_hist.get("name") or "").strip():
                        _hist["name"] = _r_dict.get("名称")
                    if _new_item.get("df") is None:
                        _new_item["df"] = _src.get("df")
                    _new_item["p1_score"] = _rp["_score"]
                    _new_item["_promoted_from_rejected"] = True
                    _new_item["promoted_threshold"] = _threshold
                    _new_item["reason"] = f"🔽 空池兜底：min_score降到{_threshold}补入"
                    _promoted.append(_new_item)
                    _rp["_promoted"] = True  # 标记已用
        # 加入主池
        if _promoted:
            p1_pool.extend(_promoted)
            logger.warning(
                "[P1 空池兜底] 今日无票达到 min_score=%.1f，已放宽到 %.1f 补入 %d 只（仅空池时触发）",
                _cur_min_score, _cur_min_score - 5.0, len(_promoted),
            )

    # 【V30.3 2026-10-17】出口兜底去重：任何路径（快车道/降档补位/上游重复输入）
    # 都不允许同一只票在 P1 池出现两次，否则 p1_cache 与 P5 扫描会整链重复。
    _seen_out_codes = set()
    _dedup_pool = []
    for _x in p1_pool:
        _c_out = str(_x.get("code") or _x.get("ts_code") or "").strip()
        if _c_out and _c_out in _seen_out_codes:
            continue
        if _c_out:
            _seen_out_codes.add(_c_out)
        _dedup_pool.append(_x)
    if len(_dedup_pool) != len(p1_pool):
        logger.warning(
            "[p1-output-dedup] P1 池重复 %d 只已合并（%d → %d）",
            len(p1_pool) - len(_dedup_pool), len(p1_pool), len(_dedup_pool),
        )
    p1_pool = _dedup_pool

    p1_pool.sort(key=lambda x: _p1_final_sort_key(x), reverse=True)
    p1_rejected.sort(key=lambda x: x["当前得分"], reverse=True)

    # ---------------------------------------------------------------------
    # 【V26.88+10-5 新增】RPS-60 相对强度注入（供 p1_rank_enhancer 做过热回避）
    #
    # 【为什么必须在这里注入】p1_pool 的元素是「纯 dict」（代码/名称/p1_score），
    # 不带行情列；enhanced_p1_sort_key 只能读 item 里的键。
    # 用「一次批量取一次库 + 一次横截面排名」的方式补 rps_60，避免逐票查库（N+1）。
    #
    # 失败语义：整体 try/except，任何异常都保持 rps_60 缺失 ⇒ calc_rps_penalty 返回 0.0，
    # 即排序退化为改动前的行为（不惩罚、不加分），绝不影响主流程出票。
    # ---------------------------------------------------------------------
    try:
        # 【P1-修复】原代码用 it.get("代码")/it.get("ts_code") 取代码，但主池 item
        # 的真实字段是 code（如 '600519.SH'），中文字段只存在于 p1_rejected 展示行，
        # 导致 _p1_codes 恒为空 → rps_60 从未注入，RPS 维度整体失效。
        _p1_codes = []
        for it in p1_pool:
            if not isinstance(it, dict):
                continue
            _c = str(it.get("code") or it.get("ts_code") or "").strip()
            if _c:
                _p1_codes.append(_c)
        if _p1_codes:
            from core.p1_rps_calculator import attach_rps_to_items

            _rps_map = attach_rps_to_items(_p1_codes)
            _hit = 0
            for _it in p1_pool:
                # attach_rps_to_items 返回的键已归一化为带后缀形态（_norm_code）
                _c = str(_it.get("code") or _it.get("ts_code") or "").strip()
                if not _c:
                    continue
                _rps_val = _rps_map.get(_c)
                if _rps_val is None:
                    # 兼容纯 6 位写法
                    _rps_val = _rps_map.get(_c.split(".")[0])
                if _rps_val is not None:
                    _it["rps_60"] = _rps_val
                    _hit += 1
            logger.info("[P1-RPS] 已注入 rps_60：%d/%d 只（周期 %s 日）",
                        _hit, len(p1_pool), getattr(__import__("core.p1_rps_calculator", fromlist=["RPS_PERIOD"]), "RPS_PERIOD", "?"))
    except Exception as _rps_e:
        logger.warning("[P1-RPS] RPS 注入失败，本次排序退化为无 RPS 维度（不阻断选股）: %s", _rps_e)

    # 【V26.80 排名优化】增强排序键
    try:
        from core.p1_rank_enhancer import rerank_with_diversity
        from datetime import datetime as _dt_now

        if len(p1_pool) > 5:
            _today_str = _dt_now.now().strftime("%Y%m%d")
            p1_pool = rerank_with_diversity(
                pool=p1_pool,
                target_size=len(p1_pool),
                max_same_industry=2,
                today=_today_str,
            )
            logger.debug("[rerank] 池大小 %d", len(p1_pool))
    except Exception as _e_rank:
        logger.debug("[rerank-skip] %s", _e_rank)

    # 【V26.80 性能优化】P1 池平衡检查
    try:
        from core.p1_balance import enforce_p1_balance, build_p1_style_profile

        if len(p1_pool) > 5:
            p1_pool, p1_rejected = enforce_p1_balance(
                p1_pool=p1_pool,
                p1_rejected=p1_rejected,
                target_size=len(p1_pool),
                # 【P1-修复】让平衡回调也能把展示行还原成完整 item，避免缺 code/hist/df
                rejected_src=_p1_rejected_src,
            )

        _P1_STYLE_PROFILE_CACHE = build_p1_style_profile(p1_pool)
        logger.debug("[p1-style] %s", _P1_STYLE_PROFILE_CACHE)
    except Exception as _e_bal:
        logger.debug("[p1-balance-skip] %s", _e_bal)

    # 【V30.15 2026-11-05 P1 池上限 120 只】用户口径：总分达标即入池，但总数不超过 120。
    # 为什么放在平衡调整之后：enforce_p1_balance 会把 rejected 里的高分票回调进主池，
    # 只有在这里再做一次出口闸，才能保证「最终落盘的池」真的不超过上限。
    # 截断口径：按 _p1_final_sort_key（分数 + 资金/题材/北向/时效复合）排序取前 N，
    # 被截掉的票回写 rejected 并标注「超池上限」，便于事后分清是「分不够」还是「名额限制」。
    try:
        from core.config_manager import get_p1_max_pool_size

        _p1_max_size = int(get_p1_max_pool_size())
    except Exception:
        _p1_max_size = 120
    if _p1_max_size > 0 and len(p1_pool) > _p1_max_size:
        p1_pool.sort(key=lambda x: _p1_final_sort_key(x), reverse=True)
        _overflow = p1_pool[_p1_max_size:]
        p1_pool = p1_pool[:_p1_max_size]
        _cap_floor_score = float(p1_pool[-1].get("p1_score", 0) or 0) if p1_pool else 0.0
        for _o in _overflow:
            if not isinstance(_o, dict):
                continue
            _o_code = str(_o.get("code") or _o.get("ts_code") or "").strip()
            _o_name = str((_o.get("hist") or {}).get("name") or "")
            _rej_row = {
                "代码": _o_code,
                "名称": _o_name,
                "淘汰死因": (
                    f"🚦超池上限({_p1_max_size}只)：得分{float(_o.get('p1_score', 0) or 0):.1f}"
                    f" 低于入池底线{_cap_floor_score:.1f}"
                ),
                "被裁阶段": "池容量上限",
                "当前得分": round(float(_o.get("p1_score", 0) or 0), 2),
                "满分项1": "--",
                "满分项2": "--",
                "最低项1": "--",
                "最低项2": "--",
                # 名额限制不是票的问题，与直通车限流同属 L1
                "死因层级": "L1",
                "处置建议": "⚪ 非票的问题：今日高分票超额，明日可重评",
                "层级徽标": "⚪ 否决级",
            }
            p1_rejected.append(_rej_row)
            _p1_rejected_src[id(_rej_row)] = _o
        logger.info(
            "[P1 池上限] 超过 %d 只上限，按综合分保留前 %d 只，回退 %d 只 (底线分 %.1f)",
            _p1_max_size, len(p1_pool), len(_overflow), _cap_floor_score,
        )

    # 【多层级池子】说明：先根据主池是否为空更新 streak，再决定是否跑第二遍降档观察池
    # 【V26.88+10-3 P3-⑪】观察池激活 streak 在后面判断后再二次调用 _merge_tier_meta_p1
    streak_after, _ = _merge_tier_meta_p1(len(p1_pool) == 0)
    pass_line_main = float(thresholds.get("pass_line", P1_PASS_LINE))

    # 【V26.88+10-3 P3-⑪】观察池回收机制：连续激活超过 5 天仍未转主池 → 自动收紧一档
    _obs_streak_after = 0
    try:
        from core.pool_manager.thresholds import _read_observation_streak
        _obs_streak_after = int(_read_observation_streak())
    except Exception:
        pass
    # 收紧系数：streak >=5 → 0.74，streak >=10 → 0.70
    if _obs_streak_after >= 10:
        _obs_pass_line_coef = 0.70
    elif _obs_streak_after >= 5:
        _obs_pass_line_coef = 0.74
    else:
        _obs_pass_line_coef = 0.78
    pass_line_obs = max(35.0, pass_line_main * _obs_pass_line_coef)
    if float(_mcs_wash) >= 0.7:
        pass_line_obs = max(35.0, pass_line_obs * 0.95)
    fill_observation_p1 = (streak_after >= 2) or (float(_mcs_wash) >= 0.7)
    observation_pool = []
    # 【P1-修复】同上：主池 item 用 code/ts_code，中文字段"代码"只存在于淘汰展示行。
    # 原代码只读 "code" 尚可，但为兼容 ts_code 口径一并兜底，避免观察池与主池重复出票。
    main_codes = {
        str(x.get("code") or x.get("ts_code") or "").split(".")[0][:6].strip()
        for x in p1_pool if isinstance(x, dict)
    }
    main_codes.discard("")
    if fill_observation_p1 and stock_data_list:
        obs_thresholds = dict(thresholds)
        obs_thresholds["pass_line"] = float(pass_line_obs)
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures_obs = {
                executor.submit(
                    _process_single_stock_for_p1,
                    item, industry_pe_stats, global_stats, industry_map, sector_rank_map,
                    dynamic_industries, unlock_blacklist, punish_blacklist, obs_thresholds
                ): item
                for item in stock_data_list
                if str(item.get("code") or item.get("ts_code") or "").split(".")[0][:6].strip() not in main_codes
            }
            for fut in as_completed(futures_obs):
                try:
                    res_o = fut.result()
                except Exception as exc:
                    logger.warning("[obs-future-fail] %s", exc)
                    continue
                if not res_o or not res_o.get("is_pass"):
                    continue
                hit_o = res_o.get("item")
                if not isinstance(hit_o, dict):
                    continue
                sc = float(res_o.get("score", 0.0))
                hit_o["p1_score"] = sc
                hit_o["inflow_ratio"] = res_o.get("inflow_ratio", 0.0)
                hit_o["inflow_10d_ratio"] = res_o.get("inflow_10d_ratio", 0.0)
                hit_o["df"] = res_o.get("df")
                hit_o["score_details"] = dict(res_o.get("score_details") or {})
                hit_o["pool_tier"] = "observation"
                if isinstance(hit_o.get("hist"), dict):
                    hit_o["hist"]["_observation_label"] = "【缩量期备选】"
                observation_pool.append(hit_o)
        observation_pool.sort(key=lambda x: _p1_final_sort_key(x), reverse=True)
    _LAST_P1_OBSERVATION_POOL = observation_pool

    # 【V26.88+10-3 P3-⑪】观察池激活 streak 持久化：本次 build 是否启用了观察池
    try:
        _merge_tier_meta_p1(len(p1_pool) == 0, _obs_active_today=bool(fill_observation_p1))
    except Exception:
        pass

    ensure_runtime_data_layout()
    file_path = path_p1_gene_json(end_date_str)

    # 【审计修复】维度2：p1_gene 仅允许可 JSON 序列化的标量
    _gene_out = {}
    for _gk, _gv in (p1_gene_dict or {}).items():
        try:
            if isinstance(_gv, (pd.DataFrame, pd.Series)):
                logger.warning("[p1-gene-skip-df] %s", _gk)
                continue
            _gene_out[str(_gk)] = float(_gv)
        except (TypeError, ValueError) as _je:
            logger.warning("[p1-gene-skip] %s: %s", _gk, _je)

    try:
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(_gene_out, f, ensure_ascii=False)
    except TypeError as e:
        logger.error(f"[p1-gene-json-type] {e}")
    except OSError as e:
        logger.error(f"[p1-gene-io] {e}")
    except Exception as e:
        logger.error(f"[p1-gene-crash] {e}")

    _missing_required_fields = []
    for _it in p1_pool:
        if not isinstance(_it, dict):
            _missing_required_fields.append(("<non-dict>", ["code", "p1_score", "hist", "df"]))
            continue
        _missing = [k for k in ("code", "p1_score", "hist", "df") if k not in _it or _it.get(k) is None]
        if _missing:
            _missing_required_fields.append((str(_it.get("code", "<no-code>")), _missing))
    if _missing_required_fields:
        sample = "; ".join([f"{code}:{'/'.join(fields)}" for code, fields in _missing_required_fields[:10]])
        logger.warning(
            "[p1-missing-fields] count=%s sample=%s",
            len(_missing_required_fields),
            sample,
        )
        # 【P1-修复】缺失必需字段的行无法落库(p1_cache 需 code)也无法在 UI 渲染(需 hist)，
        # 继续往下走只会抛 KeyError 并中断整个扫描，因此就地剔除。
        if p1_pool:
            _good = [
                _it for _it in p1_pool
                if isinstance(_it, dict)
                and str(_it.get("code") or _it.get("ts_code") or "").strip()
                and isinstance(_it.get("hist"), dict)
                and _it.get("df") is not None
                and _it.get("p1_score") is not None
            ]
            if len(_good) != len(p1_pool):
                logger.warning(
                    "[p1-drop-malformed] 剔除残缺行 %d → 剩余 %d 只（避免落库/UI KeyError）",
                    len(p1_pool) - len(_good), len(_good),
                )
                p1_pool = _good
    else:
        logger.info("[p1-fields-ok] count=%s", len(p1_pool))

    # === V28 真实接入 signal_log ===
    try:
        from core.pool_signal_adapter import safe_log_pool_signal, build_items_from_hits
        # 【V30.10 修复 2026-10-21】原先用 globals().get("_GLOBAL_DB_CONN") 取连接，
        # 但该全局变量在全项目从未被赋值（只读不写），恒为 None，
        # 导致 P1 的 safe_log_pool_signal 被 if _db_conn is not None 直接跳过 ——
        # P1 是推送量最大的池（push_history 实测 1036 条），却一条都没落库。
        # 现在改为经 resolve_write_conn() 解析可用连接（复用进程内写连接单例）。
        _db_conn = None
        try:
            from core.signal_ledger_bridge import resolve_write_conn

            _db_conn = resolve_write_conn()
        except Exception as _e_c:
            logger.debug("[p1-signal-log] 连接解析失败: %s", _e_c)
        if _db_conn is None:
            logger.warning(
                "[p1-signal-log] 无可用数据库连接，本次 P1 信号不落库"
            )
        try:
            from core.exit_strategy import attach_exit_plan_to_res
            from core.push_template_momentum_reversal import apply_push_template_to_res
            for _it in p1_pool:
                if isinstance(_it, dict):
                    _push_price = float((_it.get("hist") or _it.get("rt") or {}).get("price", 0.0))
                    if _push_price <= 0:
                        _push_price = float(_it.get("price") or _it.get("push_price") or 0.0)
                    _tmp = {
                        "pool": "p1",
                        "burst_score": float(_it.get("p1_score") or 0.0),
                        "score": float(_it.get("p1_score") or 0.0),
                        "strategies": _it.get("strategies") or [],
                        "score_details": _it.get("score_details") or {},
                        "ts_code": _it.get("code") or _it.get("ts_code"),
                        "price": _push_price,
                    }
                    _tmp = attach_exit_plan_to_res(_tmp, push_price=_push_price)
                    _it["exit_plan"] = _tmp.get("exit_plan")
                    _it["exit_template"] = _tmp.get("exit_template", "medium")
                    _tmp2 = dict(_tmp)
                    _tmp2["strategies"] = _it.get("strategies") or []
                    _tmp2 = apply_push_template_to_res(_tmp2)
                    _it["push_template"] = _tmp2.get("push_template")
                    _it["push_track"] = _tmp2.get("push_track", "neutral")
                    _it["should_push_now"] = _tmp2.get("should_push_now", True)
        except Exception as _e_inj:
            logger.debug("[p1-template-inject] %s", _e_inj)

        # V28-4 修复：推送去重（统一入口 pool_apply_push_dedup）
        try:
            from core.push_dedup import pool_apply_push_dedup, record_push
            _filtered_pool = []
            for _it in p1_pool:
                pool_apply_push_dedup(_it, "p1", record=False)
                if _it.get("is_pass") is False and _it.get("dedup_decision", {}).get("should_dedup"):
                    continue
                # 【V30.10】带上真实交易日：旧实现用"今天"，
                # 跨日补跑 / 隔夜重建会把推送历史记到错误的日期上，
                # 导致冷却期计算偏移 + 回溯时 (date, code) 匹配不上。
                record_push(
                    str(_it.get("code") or _it.get("ts_code") or ""),
                    "p1",
                    float(_it.get("p1_score") or 0.0),
                    trade_date=p1_anchor,
                )
                _filtered_pool.append(_it)
            p1_pool = _filtered_pool
        except Exception as _e_dd:
            logger.debug("[p1-push-dedup] %s", _e_dd)

        if _db_conn is not None and p1_pool:
            _items = build_items_from_hits(p1_pool, pool="p1")
            # 【V30.10 阈值统一 2026-10-21】P1 落库阈值原硬编码 60.0，
            # 现改为从 config.yaml 动态解析（core/push_threshold）。
            try:
                from core.push_threshold import resolve_push_min_score

                _p1_log_min = resolve_push_min_score("p1_high_pool", for_log=True)
            except Exception:
                _p1_log_min = 60.0
            _items = [it for it in _items
                      if float(it.get("score") or 0.0) >= float(_p1_log_min)]
            if _items:
                # === V29：补数据质量标记与风控点位（只采集/打标，不参与过滤） ===
                try:
                    from data.data_quality_flags import build_batch_quality_flags

                    _by_code = {str(it.get("ts_code") or ""): it for it in _items}
                    _flags_map = build_batch_quality_flags(_db_conn, _items, trade_date="")
                    for _code, _flags in (_flags_map or {}).items():
                        if _code in _by_code:
                            _by_code[_code]["data_quality_flags"] = _flags
                except Exception as _e_q:
                    logger.debug("[p1-quality-flags] %s", _e_q)
                try:
                    _p1_by_code = {
                        str(it.get("code") or it.get("ts_code") or ""): it for it in p1_pool
                    }
                    for _it2 in _items:
                        _src = _p1_by_code.get(str(_it2.get("ts_code") or ""))
                        if not _src:
                            continue
                        if isinstance(_src.get("exit_plan"), dict):
                            _it2["exit_plan"] = _src.get("exit_plan")
                        if _src.get("exit_template"):
                            _it2["exit_template"] = _src.get("exit_template")
                        _it2["suggested_position"] = str(
                            _src.get("建议仓位") or _src.get("suggested_position") or ""
                        )
                        _it2["entry_basis"] = "push_price:p1"
                except Exception as _e_r:
                    logger.debug("[p1-risk-fields] %s", _e_r)
                safe_log_pool_signal("p1", _items, _db_conn)
    except Exception as _e_sl:
        logger.debug("[p1-signal-log] %s", _e_sl)

    return p1_pool, p1_rejected


__all__ = [
    "_TIER_META_KEY",
    "_wash_metrics_path",
    "_merge_tier_meta_p1",
    "get_last_p1_observation_pool",
    "get_last_p1_wash_adaptive",
    "build_p1_pool_and_cache",
]
