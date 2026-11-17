# -*- coding: utf-8 -*-
"""
小杰AI选股系统 Pro V26.88 - P3 盘中战法池（物理胸甲十大策略 + 防镰刀增强版 + 2026慢牛优化）
【架构】
1. 硬阈值筛选集中在 p3_intraday_screener.py（全局否决 + 八大客观策略），单次 O(1) 尾部访问。
2. 本类负责：盘口组装、日内风控惩罚（VWAP/上影/早盘量比）、爆发分合成。
3. 黄金起爆点门禁交由 scan_engine：命中 p3_core_screener_pass 时放宽，避免与低吸/托底区间冲突。

【V26.88 防镰刀优化】
① 强化跟风/独自点火惩罚权重：crowd_follower 12→18, crowd_phantom 18→28
② 新增早盘冲高回落检测：识别A股最常见亏损模式（上午涨、下午跌）
③ 强化分时均价下方运行惩罚：VWAP下方阈值收紧至0.99，深度跌破单独惩罚
④ 新增尾盘异动识别：识别尾盘突然拉升（次日易低开）
⑤ 新增板块涨幅偏差检测：个股涨幅超过板块超过阈值时惩罚
⑥ 强化昨日涨停T+1风险：已有_check_limit_up_burst，进一步强化
"""
import os
import logging
from typing import Any, Dict, Optional

import pandas as pd
import numpy as np
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

try:
    from core.strategies.fund_mv_utils import effective_turnover_rate_f, mean_effective_turnover_f_last_n
except ImportError:
    from strategies.fund_mv_utils import (  # type: ignore
        effective_turnover_rate_f,
        mean_effective_turnover_f_last_n,
    )

from core.config_manager import get_p3_intraday_screener_config, get_risk_control_config, get_p1_weekly_config
from core.strategies.p3_intraday_screener import evaluate_p3_intraday_screener
from core.strategies.strat_base import _p1_min_circ_mv_yi_strat
from core.strategies.risk_control_engine import (
    DEFAULT_RISK_CONFIG,
    RiskControlConfig,
    evaluate_3layer_risk,
    evaluate_layer2_right_side_only,
    hits_indicate_right_side_attack,
    merge_risk_into_engine_result,
)
from core.strategies.score_calibration import _compute_weekly_buff

logger = logging.getLogger(__name__)


# ==================== 【V26.88+9-23 新增】财务质量注入器 ====================
_FIN_RT_KEYS = (
    "qoq_revenue_yoy", "qoq_profit_yoy",
    "roe_pct", "gross_margin", "net_margin",
    "q_roe", "q_gross_margin",
    "bps_per_share", "debt_to_eqt", "ocf_to_revenue",
    "revenue_yoy", "net_profit_yoy", "asset_liab_rate",
)


def _inject_financial_quality_into_rt(rt: Dict[str, Any], ts_code: str) -> None:
    """
    【V26.88+9-23 新增】从 fact_financial_reports 取最近一季（ann_date 最新）的关键财务指标注入到 rt。

    设计要点：
      - 单条 SQL，O(1) 查找；命中后写回 13 个键，未命中则跳过（不影响主流程）。
      - rt 里 pre_close/pe_ttm 等已有字段会被保留（本次只注入"财务质量"维度）。
      - 异常被调用方 try/except 吞掉，确保不影响打分主流程。

    【V26.88+9-28 P0 修复 ST-FIN-02】改用进程内读连接单例 get_read_conn_singleton()，
    不再每票 duckdb.connect(...) + close() —— P3/P4/P5 全市场扫描时（2000+ 票 / 次）
    频繁 connect/close 会把 DuckDB 文件 mmap 抖动 + Windows file lock 抖动放大。
    """
    if rt is None or not isinstance(rt, dict):
        return
    # 避免重复注入
    if rt.get("_fin_quality_injected") is True:
        return
    # 【V34】已全市场预热 → 直接命中缓存；未命中说明该票无财报，同样标记，不回退逐票 SQL
    if _FIN_PRIMED_KEY is not None:
        _hit = (_FIN_BATCH_CACHE.get(_FIN_PRIMED_KEY) or {}).get(str(ts_code).strip())
        if _hit:
            for _k, _v in _hit.items():
                rt[_k] = _v
        rt["_fin_quality_injected"] = True
        return
    try:
        # 【V26.88+9-28】复用 db_core 读连接单例；缺包/写库异常时静默退化，不影响主流程
        try:
            from data.db_core import get_read_conn_singleton

            con = get_read_conn_singleton()
        except Exception:
            return
        try:
            row = con.execute(
                """
                SELECT qoq_revenue_yoy, qoq_profit_yoy, roe_pct, gross_margin, net_margin,
                       q_roe, q_gross_margin, bps_per_share, debt_to_eqt, ocf_to_revenue,
                       revenue_yoy, net_profit_yoy, asset_liab_rate
                FROM fact_financial_reports
                WHERE ts_code = ?
                ORDER BY ann_date DESC
                LIMIT 1
                """,
                [ts_code],
            ).fetchone()
        except Exception:
            return
        if row is None:
            return
        for k, v in zip(_FIN_RT_KEYS, row):
            try:
                if v is not None and v == v:  # not NaN
                    rt[k] = float(v)
            except (TypeError, ValueError):
                continue
        rt["_fin_quality_injected"] = True
    except Exception:
        # 静默失败：财务表尚未初始化/网络异常等情形下，主流程仍能继续
        return


# 进程级共享缓存：{trade_date_str: {ts_code: {key: float}}}
_FIN_BATCH_CACHE: Dict[str, Dict[str, Dict[str, float]]] = {}
# 最近一次成功预热的 trade_date；非 None 表示逐票注入可直接走缓存、不再发 SQL
_FIN_PRIMED_KEY: Optional[str] = None

_FIN_COLS = (
    "qoq_revenue_yoy", "qoq_profit_yoy", "roe_pct", "gross_margin", "net_margin",
    "q_roe", "q_gross_margin", "bps_per_share", "debt_to_eqt", "ocf_to_revenue",
    "revenue_yoy", "net_profit_yoy", "asset_liab_rate",
)


def _load_fin_batch_cache(ts_codes: List[str], cache_key: str) -> int:
    """1 次 SQL 把整池「每票最新财季」指标灌进 _FIN_BATCH_CACHE[cache_key]，返回命中票数。

    失败/空表一律返回 0 且不写缓存，调用方据此退回逐票注入（不会污染已预热的缓存）。
    """
    unique_codes = list({str(c).strip() for c in ts_codes if c})
    if not unique_codes:
        return 0
    try:
        from data.db_core import get_read_conn_singleton

        con = get_read_conn_singleton()
    except Exception:
        return 0
    cache: Dict[str, Dict[str, float]] = {}
    try:
        placeholders = ",".join(["?"] * len(unique_codes))
        sql = f"""
            WITH ranked AS (
                SELECT
                    ts_code,
                    qoq_revenue_yoy, qoq_profit_yoy, roe_pct, gross_margin, net_margin,
                    q_roe, q_gross_margin, bps_per_share, debt_to_eqt, ocf_to_revenue,
                    revenue_yoy, net_profit_yoy, asset_liab_rate,
                    ROW_NUMBER() OVER (
                        PARTITION BY ts_code ORDER BY ann_date DESC
                    ) AS rn
                FROM fact_financial_reports
                WHERE ts_code IN ({placeholders})
            )
            SELECT ts_code,
                   qoq_revenue_yoy, qoq_profit_yoy, roe_pct, gross_margin, net_margin,
                   q_roe, q_gross_margin, bps_per_share, debt_to_eqt, ocf_to_revenue,
                   revenue_yoy, net_profit_yoy, asset_liab_rate
            FROM ranked
            WHERE rn = 1
        """
        rows = con.execute(sql, unique_codes).fetchall()
        for row in rows:
            if not row:
                continue
            code = str(row[0]).strip()
            d: Dict[str, float] = {}
            for k, v in zip(_FIN_COLS, row[1:]):
                try:
                    if v is not None and v == v:
                        d[k] = float(v)
                except (TypeError, ValueError):
                    continue
            cache[code] = d
    except Exception:
        return 0
    _FIN_BATCH_CACHE[cache_key] = cache
    return len(cache)


def prime_financial_quality_cache(ts_codes: List[str], trade_date: Optional[str] = None) -> int:
    """【V34 新增】全市场财务质量预热：扫描入口调用 1 次，之后逐票注入零查询命中。

    同一 trade_date 内重复调用直接复用缓存（P3 → P4 → P5 共用一份）。
    返回缓存可用票数；返回 0 表示未预热成功（调用方应期望逐票注入退化为原 SQL 路径）。
    """
    global _FIN_PRIMED_KEY
    cache_key = str(trade_date or "default")
    if cache_key not in _FIN_BATCH_CACHE:
        if _load_fin_batch_cache(ts_codes, cache_key) <= 0:
            return 0
    _FIN_PRIMED_KEY = cache_key
    return len(_FIN_BATCH_CACHE.get(cache_key) or {})


def _batch_inject_financial_quality_into_rt(
    rt_list: List[Dict[str, Any]],
    ts_codes: List[str],
    trade_date: Optional[str] = None,
) -> int:
    """
    【V26.88+10-1 P1-1】批量财务质量注入：5300 票 → 1 次 SQL（等价于预热 + 逐票命中）。

    同一 trade_date 内多次调用复用 cache（典型场景：P3 → P4 → P5 同一日扫描）。
    返回实际填充的 ts_code 数（用于监控/日志）。
    """
    global _FIN_PRIMED_KEY
    if not rt_list or not ts_codes:
        return 0
    cache_key = str(trade_date or "default")
    if cache_key not in _FIN_BATCH_CACHE:
        if _load_fin_batch_cache(ts_codes, cache_key) <= 0:
            return 0
    _FIN_PRIMED_KEY = cache_key
    cache = _FIN_BATCH_CACHE.get(cache_key) or {}

    n_filled = 0
    rt_by_code: Dict[str, Dict[str, Any]] = {}
    for rt in rt_list:
        if isinstance(rt, dict):
            code = str(rt.get("ts_code") or "").strip()
            if code:
                rt_by_code[code] = rt

    for code, rt in rt_by_code.items():
        d = cache.get(code)
        if not d:
            continue
        for k, v in d.items():
            rt[k] = v
        rt["_fin_quality_injected"] = True
        n_filled += 1
    return n_filled


def clear_financial_batch_cache(trade_date: Optional[str] = None) -> None:
    """清除批量注入缓存（手工调试 / 跨日重扫时调用）。"""
    global _FIN_PRIMED_KEY
    if trade_date is None:
        _FIN_BATCH_CACHE.clear()
        _FIN_PRIMED_KEY = None
    else:
        _FIN_BATCH_CACHE.pop(str(trade_date), None)
        if _FIN_PRIMED_KEY == str(trade_date):
            _FIN_PRIMED_KEY = None


def _safe_float(val, default=0.0):
    if val is None:
        return default
    try:
        if pd.isna(val) or str(val).strip() in ["", "-"]:
            return default
        return float(val)
    except (ValueError, TypeError):
        return default


def _p3_strategy_tier(hit: str) -> str:
    s = str(hit or "")
    if any(k in s for k in ["右侧起爆", "均线回踩低吸", "巨头连贯发力", "倍量启动延续", "质量趋势底仓"]):
        return "core"
    if any(k in s for k in ["单峰跃迁", "平台/二次确认", "资金逆势托底", "均线粘合突破"]):
        return "aux"
    if "缩量分歧低吸" in s or "RSI超跌反转" in s:
        return "obs"
    return "aux"


class P3Intraday:
    def __init__(self, screener_cfg=None, risk_cfg: Optional[RiskControlConfig] = None):
        """
        screener_cfg: 可选 P3IntradayScreenerConfig，用于回测/实盘分区调参。
        risk_cfg: 可选三层风控；不传则从 config.yaml ``risk_control`` 加载（见 get_risk_control_config）。
        """
        self.name = "P3盘中引擎"
        self.version = "V26.88Strategies"
        self.db_cache = {}
        self._cfg_lock_external = screener_cfg is not None
        self._cfg = screener_cfg if screener_cfg is not None else get_p3_intraday_screener_config()
        self._risk_cfg_lock_external = risk_cfg is not None
        if risk_cfg is not None:
            self._risk_cfg = risk_cfg
        else:
            try:
                self._risk_cfg = get_risk_control_config()
            except Exception as ex:
                logger.debug("风控配置从 config.yaml 加载失败，使用 DEFAULT_RISK_CONFIG: %s", ex)
                self._risk_cfg = DEFAULT_RISK_CONFIG
        # 【V26.88+9-22】Z-Score 分钟线量化增强已移除（依赖 Tushare stk_mins，
        # 全量回填需 27 分钟/日，占 10000 积分配额 50%，HiThink 高频动向暂未开放）
        self._vol_slice_zscore_ready = False

    def _load_zscore_db(self):
        """【V26.88+9-22 已移除】Z-Score 量化增强已禁用，始终静默降级。"""
        self._vol_slice_zscore_ready = False
        self._vol_slice_seen_empty = False

    def _map_momentum(self, pct_chg, atr_pct, vr):
        if atr_pct <= 0:
            atr_pct = 2.0
        relative_move = max(0.0, float(pct_chg)) / atr_pct
        momentum_factor = relative_move * vr
        xp = [0.0, 0.5, 1.0, 2.5, 5.0]
        yp = [0.0, 30.0, 60.0, 90.0, 100.0]
        return float(np.interp(momentum_factor, xp, yp))

    def _map_chip_kill(self, now_price, cost_95th):
        if cost_95th <= 0:
            return 50.0
        ratio = now_price / cost_95th
        xp = [0.90, 0.95, 1.00, 1.02, 1.05]
        yp = [0.0, 30.0, 60.0, 90.0, 100.0]
        return float(np.interp(ratio, xp, yp))

    def _map_funds(self, amount, circ_mv_yi):
        if circ_mv_yi <= 0:
            return 0.0
        inflow_ratio = (amount / (circ_mv_yi * 100000000.0)) * 1000.0
        xp = [0.0, 0.5, 2.0, 5.0, 10.0]
        yp = [0.0, 30.0, 65.0, 85.0, 100.0]
        return float(np.interp(inflow_ratio, xp, yp))

    def _check_second_surge(self, df, rt):
        try:
            if df is None or len(df) < 60:
                return 0.0
            curr = df.iloc[-1]
            if _safe_float(curr.get("max_60d_pct", 0.0)) < 15.0:
                return 0.0
            # 【2026-10-15 审计修复 · tail(21) 含当日未收盘实时行导致前视偏差】
            # 根因：core.indicator_calc.merge_daily_with_realtime 会在 df 末行追加/覆写当日盘中行（
            #         core/scan/pipeline.py:566-582 已确认调用前合并并重算指标），
            #         而下行用 rt['price']（当日现价）与「含当日的 21 日高点」比较，
            #         属自参考（自比较），且未收盘的临时高点会虚高基准。
            #   改法：取前 20 个已收盘交易日的高点（排除末行），与已修复的
            #         _high_20_prev() / P5 high_20_prev 保持同一口径。不改阈值 0.96 / bonus 2-5-8。
            _hi_s = df["high"].iloc[:-1] if len(df) >= 2 else df["high"]
            recent_20_high = _hi_s.tail(20).max()
            now_px = _safe_float(rt.get("price", 0.0))
            if now_px < recent_20_high * 0.96:
                return 0.0
            ma20 = _safe_float(curr.get("ma20", 0.0))
            if ma20 > 0 and now_px < ma20:
                return 0.0
            turnover_shrink = mean_effective_turnover_f_last_n(df, 3) < 3.0 if len(df) >= 3 else False
            # 【V30.10 统一 MACD 口径 2026-10-21】原用 macd_diff / macd_dea 判定，
            # 但这两列在生产 daily_data 里【根本不存在】（只有 macd/macd_signal/macd_hist），
            # 取不到 → 两侧恒 0.0 → 0.0 > 0.0 恒 False → 本加分项【永远不成立】。
            # 现改用 core.strategies.macd_unified 统一判定。
            from core.strategies.macd_unified import is_macd_bull
            _bull = is_macd_bull(curr)
            macd_strong = bool(_bull) if _bull is not None else False
            cyq_stable = _safe_float(rt.get("cyq_concentration", 99.0)) < 20.0
            matches = sum([turnover_shrink, macd_strong, cyq_stable])
            if matches == 3:
                return 8.0
            if matches == 2:
                return 5.0
            if matches == 1:
                return 2.0
            return 0.0
        except Exception as e:
            logging.debug(f"P3 _check_second_surge 异常: {e}")
            return 0.0

    def _volume_zscore_signal(self, rt: dict, df, circ_mv_yi: float):
        """
        【V26.88+9-22 已移除】
        Z-Score 分钟线量化增强已从系统中完全移除。

        原因：依赖 Tushare pro.stk_mins，全量回填需 27 分钟/日，占 10000 积分配额约 50%，
        HiThink 高频动向模块暂未开放。

        此方法始终返回 (0, "zscore_removed", None)，确保：
        - P3 爆发分 raw_burst 不受任何 Z-Score 加减分影响
        - 调用方 isinstance(z_value, (int, float)) 判断为 False，不误入任何分支
        """
        return 0.0, "zscore_removed", None

    def _check_limit_up_burst(self, rt: dict, pre_close: float) -> dict:
        """
        【V26.88 P3 优化】涨停板炸板分析。

        V26.88+9-22 优化5：优先读取 HiThink 实时涨停池（rt["_hithink_limit_up"]），
        字段优先级：
          HiThink 实时涨停池 > Tushare 历史 rt 字段（降级兜底）

        HiThink 实时字段映射：
          _hithink_limit_up["continue_day_cnt"]  → 连板数
          _hithink_limit_up["seal_money"]        → 封单金额（元）
          _hithink_limit_up["max_seal_money"]    → 最大封单金额（元）
          _hithink_limit_up["is_st"]             → ST 标识（已有全局拦截，此处防误判）
          _hithink_limit_burst["open_times"]   → 开板次数（炸板池）

        返回: {status, bonus, penalty, reason}
        """
        now_price = _safe_float(rt.get("price", 0.0))
        if now_price <= 0 or pre_close <= 0:
            return {"status": "unknown", "bonus": 0.0, "penalty": 0.0, "reason": "价格缺失"}

        pct_today = (now_price - pre_close) / pre_close * 100.0
        # 判断是否接近涨停（沪市9.95%，深市9.99%）
        is_near_limit_up = pct_today >= 9.4

        # V26.88+9-22 优化5：优先 HiThink 实时涨停池
        ht_lu = rt.get("_hithink_limit_up") or {}
        ht_lb = rt.get("_hithink_limit_burst") or {}

        # 开板次数：优先 HiThink 炸板池，其次 rt 字段
        open_times_raw = ht_lb.get("open_times")
        if open_times_raw is None:
            open_times_raw = rt.get("limit_up_burst_count")
        try:
            burst_count = int(float(open_times_raw) if open_times_raw is not None else 0)
        except (TypeError, ValueError):
            burst_count = 0

        # 封单金额：优先 HiThink 实时封单
        seal_vol = ht_lu.get("seal_money") if ht_lu else None
        if seal_vol is None:
            seal_vol = _safe_float(rt.get("limit_up_seal_volume"), 0.0)

        # 最大封单（HiThink 独有）
        max_seal = ht_lu.get("max_seal_money") if ht_lu else None
        if max_seal is None:
            max_seal = seal_vol

        # 回封次数（HiThink 无此字段，降级使用 rt）
        retry_raw = rt.get("limit_up_retry_count")
        try:
            retry_count = int(float(retry_raw) if retry_raw is not None else 0)
        except (TypeError, ValueError):
            retry_count = 0

        vol_rt = _safe_float(rt.get("volume", 1.0))

        # HiThink 实时涨停池判定：个股在涨停池 → 封板中
        in_limit_pool = bool(ht_lu)

        if not is_near_limit_up and not in_limit_pool:
            return {"status": "normal", "bonus": 0.0, "penalty": 0.0, "reason": ""}

        # HiThink 实时涨停池 → 一字板（最强）
        if in_limit_pool:
            cnt = ht_lu.get("continue_day_cnt", 1) or 1
            seal_yi = (seal_vol or 0.0) / 1e8
            if cnt >= 3 and seal_yi >= 1.0:
                return {
                    "status": "one_word_seal",
                    "bonus": 15.0,
                    "penalty": 0.0,
                    "reason": f"实时封板({cnt}板,封单{seal_yi:.1f}亿)",
                }
            elif cnt >= 2:
                return {
                    "status": "one_word_seal",
                    "bonus": 10.0,
                    "penalty": 0.0,
                    "reason": f"实时封板({cnt}板)",
                }
            else:
                return {
                    "status": "one_word_seal",
                    "bonus": 8.0,
                    "penalty": 0.0,
                    "reason": "实时封板(首板)",
                }

        # 无 HiThink 信号 → 降级使用 Tushare 字段判断
        # 一字板（未炸板）
        if burst_count == 0:
            return {"status": "one_word_seal", "bonus": 15.0, "penalty": 0.0, "reason": "一字板强势封停"}

        # 频繁炸板：主力无力封板
        if burst_count > 3:
            return {"status": "frequent_burst", "bonus": 0.0, "penalty": 30.0, "reason": f"频繁炸板{burst_count}次"}

        # 封单不足：封板力度弱
        if seal_vol is not None and vol_rt > 0 and seal_vol > 0:
            seal_ratio = seal_vol / max(vol_rt, 1.0)
            if seal_ratio < 0.3:
                return {"status": "weak_seal", "bonus": 0.0, "penalty": 25.0, "reason": "封单不足"}

        # 有回封但次数过多
        if retry_count > 2 and burst_count >= 2:
            return {"status": "unstable_seal", "bonus": 0.0, "penalty": 20.0, "reason": f"反复开板{burst_count}次回封{retry_count}次"}

        # 正常炸板1-2次，有一定封单力度
        if burst_count <= 2 and (seal_vol is None or seal_vol <= 0 or (vol_rt > 0 and seal_vol / max(vol_rt, 1.0) >= 0.3)):
            return {"status": "normal_burst", "bonus": 5.0, "penalty": 0.0, "reason": f"炸板{burst_count}次但封单稳健"}

        return {"status": "normal", "bonus": 0.0, "penalty": 0.0, "reason": ""}

    def _build_time_hint(self, curr_min: int, pct_chg: float, vr: float, core_hits: list) -> str:
        """
        【V26.88 P3 优化】小白友好：根据当前时间给出买入时机建议。
        """
        if curr_min < 600:  # 10:00前
            return "开盘初期，观察为主，等10点后趋势明确再操作"
        elif 600 <= curr_min < 660:  # 10:00-11:00
            if pct_chg > 3.0:
                return "⚠️已涨>3%，追高风险大，建议等回踩确认"
            return "可轻仓试探，止损设开盘价-1.5%"
        elif 660 <= curr_min < 720:  # 11:00-12:00
            return "午前时段，观察午后方向，不建议新开仓"
        elif 720 <= curr_min < 780:  # 12:00-13:00
            return "⚠️午盘开盘，情绪不稳定，建议观望5分钟再判断"
        elif 780 <= curr_min < 840:  # 13:00-14:00
            if pct_chg > 2.0 and vr > 1.5:
                return "午后放量上攻，可考虑轻仓(≤15%)，止损设VWAP-1.5%"
            return "午后弱势，不建议追高"
        else:  # 14:00后
            if core_hits and pct_chg > 2.0:
                return "尾盘强势，可博弈但仓位≤10%，止损昨收-2%"
            return "⚠️尾盘时段，当日方向已定，不建议新开仓"

    def _build_stop_loss_hint(self, pct_chg: float, vr: float, core_hits: list, mainline_score: float) -> dict:
        """
        【V26.88 P3 优化】小白友好：根据策略类型给出止损建议。
        【V26.88 新增】增加移动止损提示逻辑，盈利后自动上调止损位。
        """
        # 激进策略（右侧起爆、倍量启动）
        if any("P3-01" in h or "P3-07" in h for h in core_hits):
            return {
                "aggressive": "回踩开盘价-2%严格止损",
                "moderate": "回踩VWAP-1.5%止损",
                "conservative": "昨收-3%止损",
                # 【V26.88 新增】移动止损提示
                "trailing_stop": "浮盈>5%时止损上调至成本价；浮盈>8%时止损上调至今日最低价*0.97",
            }
        # 稳健策略（均线回踩低吸、缩量分歧低吸）
        if any("P3-02" in h or "P3-09" in h for h in core_hits):
            return {
                "aggressive": "回踩MA20-1%止损",
                "moderate": "回踩VWAP-0.8%止损",
                "conservative": "昨收-2%止损",
                # 【V26.88 新增】移动止损提示
                "trailing_stop": "浮盈>4%时止损上调至成本价；浮盈>6%时止损上调至今日最低价*0.98",
            }
        # 主线龙头
        if mainline_score >= 3.0:
            return {
                "aggressive": "回踩开盘价-1.5%止损",
                "moderate": "回踩VWAP-1%止损",
                "conservative": "昨收-2.5%止损",
                # 【V26.88 新增】移动止损提示
                "trailing_stop": "浮盈>5%时止损上调至成本价；浮盈>8%时止损上调至今日最低价*0.97",
            }
        # 连板股策略
        if any("P4-07" in h or "连板" in h for h in core_hits):
            return {
                "aggressive": "回踩MA5-1%止损",
                "moderate": "回踩VWAP-0.5%止损",
                "conservative": "昨收-1.5%止损",
                # 【V26.88 新增】移动止损提示
                "trailing_stop": "浮盈>6%时止损上调至成本价；浮盈>10%时止损上调至今日最低价*0.96",
            }
        # 默认
        return {
            "aggressive": "回踩开盘价-2%止损",
            "moderate": "回踩VWAP-1.5%止损",
            "conservative": "昨收-3%止损",
            # 【V26.88 新增】移动止损提示
            "trailing_stop": "浮盈>5%时止损上调至成本价；浮盈>8%时止损上调至今日最低价*0.97",
        }

    def _p3_time_bucket(self, curr_min: int) -> str:
        """盘中时段桶（用于仓位折价与企微短码，全部以扫描时点为准）。"""
        m = int(curr_min or 0)
        if m < 600:
            return "开盘初期"
        if m <= 660:
            return "早盘"
        if m < 690:
            return "午前"
        if m <= 705:
            return "午盘开盘"
        if m <= 780:
            return "午后开盘"
        if m < 840:
            return "午后"
        return "尾盘"

    def _p3_position_ladder(
        self, *, score: float, penalty: float, core_cnt: int, aux_cnt: int,
        curr_min: int, layer_overheat: int, trade_grade: str, suggested_min: float,
    ) -> Tuple[float, Dict[str, Any]]:
        """【V34 新增】P3 盘中仓位阶梯：把「信号强度 / 分档共振 / 惩罚 / 时段 / 温度」折成仓位。

        刻度提醒：burst_score 是盘口加权分（动量/绞杀/资金 + regime + 板块 + 周线 + 温度 + 财务），
        与 pipeline 展示的综合分不是同一刻度，所以用 45–85 线性归一（0.30→1.00），
        而不是硬套 60/80 分档——后者在 P4 上曾把「分53」一刀切成「仓0%」。
        端点与上下限均可配（p3_score_scale_floor/ceiling、p3_base/max/min_position_ratio）。
        """
        cfg = self._cfg
        base = _safe_float(getattr(cfg, "p3_base_position_ratio", 0.10), 0.10)
        cap = _safe_float(getattr(cfg, "p3_max_position_ratio", 0.18), 0.18)
        low = _safe_float(getattr(cfg, "p3_min_position_ratio", 0.03), 0.03)

        ladder: Dict[str, Any] = {
            "base": round(base, 3), "score_mult": 1.0, "tier_mult": 1.0,
            "penalty_mult": 1.0, "ev_mult": 1.0, "heat_mult": 1.0, "time_mult": 1.0,
        }
        # 只有观察项（无主战法、辅助未共振）→ 不给仓位，只留信息
        if core_cnt <= 0 and aux_cnt < 2:
            ladder.update({"ratio": 0.0, "reason": "仅观察项（无主战法且辅助未共振）"})
            return 0.0, ladder

        s_floor = _safe_float(getattr(cfg, "p3_score_scale_floor", 45.0), 45.0)
        s_ceil = _safe_float(getattr(cfg, "p3_score_scale_ceiling", 85.0), 85.0)
        if s_ceil <= s_floor:
            s_ceil = s_floor + 1.0
        _frac = (float(score) - s_floor) / (s_ceil - s_floor)
        ladder["score_mult"] = round(min(1.0, max(0.30, 0.30 + 0.70 * _frac)), 3)
        ladder["score_input"] = round(float(score), 2)

        if core_cnt >= 1 and aux_cnt >= 1:
            ladder["tier_mult"] = 1.0
        elif core_cnt >= 1:
            ladder["tier_mult"] = 0.92
        else:
            ladder["tier_mult"] = 0.75
        ladder["tier"] = f"core{core_cnt}/aux{aux_cnt}"

        p = _safe_float(penalty, 0.0)
        if p <= 10.0:
            ladder["penalty_mult"] = 1.0
        elif p <= 30.0:
            ladder["penalty_mult"] = 0.85
        elif p <= 50.0:
            ladder["penalty_mult"] = 0.60
        else:
            ladder["penalty_mult"] = 0.30

        ladder["ev_mult"] = {"A": 1.0, "B": 0.90, "B+": 0.95, "C": 0.75, "D": 0.45}.get(
            str(trade_grade or "").strip().upper(), 0.85
        )

        oh = int(layer_overheat or 0)
        if oh <= -3:
            ladder["heat_mult"] = 0.70
        elif oh <= -2:
            ladder["heat_mult"] = 0.85

        bucket = self._p3_time_bucket(curr_min)
        ladder["time_bucket"] = bucket
        ladder["time_mult"] = {
            "开盘初期": 0.0, "早盘": 1.0, "午前": 0.90,
            "午盘开盘": 0.75, "午后开盘": 0.90, "午后": 1.0, "尾盘": 0.70,
        }.get(bucket, 0.85)

        if ladder["time_mult"] <= 0.0:
            ladder.update({"ratio": 0.0, "reason": "开盘初期不建仓，等 10:00 后趋势明确"})
            return 0.0, ladder

        _smin = _safe_float(suggested_min, 0.0)
        if _smin > 0 and float(score) < _smin:
            ladder.update({"ratio": 0.0, "reason": f"综合分{float(score):.0f}<建议最低{_smin:.0f}"})
            return 0.0, ladder

        ratio = base
        for k in ("score_mult", "tier_mult", "penalty_mult", "ev_mult", "heat_mult", "time_mult"):
            ratio *= float(ladder[k])
        if ratio <= 0:
            ladder["ratio"] = 0.0
            return 0.0, ladder
        ratio = min(cap, max(low, ratio))
        ladder["ratio"] = round(ratio, 3)
        return float(ratio), ladder

    def _p3_wechat_hint(self, rt: Dict[str, Any], res: Dict[str, Any], curr_min: int) -> str:
        """企微推送要短：长文案留在 buy_hint，这里给「池|名称|分|仓|时段|止损」短码（与 P4 同口径）。"""
        name = str(rt.get("name", "") or "").strip() or "--"
        score = float(res.get("burst_score", 0.0) or 0.0)
        pos = float(res.get("max_position_ratio", 0.0) or 0.0)
        stop = ""
        _sl = res.get("auto_stop_loss")
        try:
            if isinstance(_sl, dict):
                _px = float(_sl.get("stop_loss_price", 0.0) or 0.0)
                stop = f"{_px:.2f}" if _px > 0 else ""
            elif _sl:
                stop = str(_sl)
        except Exception:
            stop = ""
        parts = [f"P3盘中|{name}", f"分{score:.0f}", f"仓{pos * 100:.0f}%", self._p3_time_bucket(curr_min)]
        if stop:
            parts.append(f"止损{stop}")
        return "|".join(parts)

    def _build_p3_explain(self, hits: list, pct_chg: float, vr: float, mainline_score: float, sector_rank: int, limit_up_result: dict) -> dict:
        """
        【V26.88 P3 优化】小白友好：生成为什么入选、风险提示、建议。
        """
        why_selected = []
        for h in hits:
            if "右侧起爆" in h:
                why_selected.append("价格放量突破关键位")
            elif "均线回踩低吸" in h:
                why_selected.append("回踩均线获得支撑")
            elif "单峰跃迁" in h:
                why_selected.append("突破筹码密集区")
            elif "巨头连贯" in h:
                why_selected.append("大资金持续买入")
            elif "平台" in h:
                why_selected.append("二次确认企稳")
            elif "资金逆势" in h:
                why_selected.append("资金托底护盘")
            elif "倍量启动" in h:
                why_selected.append("昨日放量启动延续")
            elif "缩量分歧" in h:
                why_selected.append("缩量调整后低吸机会")

        risk_note = []
        if pct_chg > 5.0:
            risk_note.append("涨幅过大，追高风险高")
        if vr > 4.0:
            risk_note.append("量比异常放大，小心对倒")
        if limit_up_result.get("penalty", 0) > 0:
            risk_note.append(f"涨停炸板风险: {limit_up_result.get('reason', '')}")
        if mainline_score <= -2.0:
            risk_note.append("非主线板块，持续性存疑")

        return {
            "why_selected": "、".join(why_selected) if why_selected else "满足盘中选股条件",
            "risk_note": "；".join(risk_note) if risk_note else "暂无明显风险提示",
            "suggestion": self._build_time_hint(999, pct_chg, vr, hits)  # 默认时间
        }

    def run_all(self, df, rt):
        """
        增量入口：df 为截至昨收的日线；rt 为当前快照（price/volume/amount/...）。
        """
        if not self._cfg_lock_external:
            self._cfg = get_p3_intraday_screener_config()
        if not self._risk_cfg_lock_external:
            try:
                self._risk_cfg = get_risk_control_config()
            except Exception:
                pass
        res = {
            "burst_score": 0.0,
            "surge_bonus": 0.0,
            "penalty": 0.0,
            "detail": {},
            "strategies": [],
            "p3_core_screener_pass": False,
            "p3_veto_reason": "",
            "p3_strategy_checks": {},
            "risk_tags": [],
            "suggested_min_entry_score": 0.0,
            "risk_control": {},
        }
        if df is None or df.empty or len(df) < 21:
            return res

        try:
            now_price = _safe_float(rt.get("price", 0.0))
            curr = df.iloc[-1]
            pre_close = _safe_float(rt.get("pre_close"), 0.0)
            if pre_close <= 0:
                pre_close = _safe_float(curr.get("pre_close"), 0.0)
            if pre_close <= 0:
                pre_close = _safe_float(curr.get("close"), 0.0)
            if pre_close <= 0 or now_price <= 0:
                return res

            open_price = _safe_float(rt.get("open", now_price))
            high_price = _safe_float(rt.get("high", now_price))
            low_price = _safe_float(rt.get("low", now_price))

            pct_chg = (now_price - pre_close) / pre_close * 100.0
            vr = _safe_float(rt.get("vol_ratio", 0.0))
            turnover_f = float(effective_turnover_rate_f(rt, curr, now_price))
            regime_state = str(rt.get("_regime_state", rt.get("regime", "")) or "")
            regime_score = _safe_float(rt.get("_regime_score", rt.get("market_contraction_score", 0.0)), 0.0)
            # 【V34 修复 · P1】T+1 记忆：pipeline 只注入 _t1_memory(dict)，从不写 _t1_memory_boost，
            # 原式只读后者 → 生产恒 0；而 dict 兜底又放在打分之后才回填 → 加分永远进不了排序。
            _t1_src = rt.get("_t1_memory") if isinstance(rt.get("_t1_memory"), dict) else {}
            t1_boost = _safe_float(rt.get("_t1_memory_boost", 0.0), 0.0) or _safe_float(_t1_src.get("boost", 0.0), 0.0)
            t1_win = _safe_float(rt.get("_t1_win_rate_pct", 0.0), 0.0) or _safe_float(_t1_src.get("win_rate_t1_pct", 0.0), 0.0)
            t1_avg = _safe_float(rt.get("_t1_avg_ret_pct", 0.0), 0.0) or _safe_float(_t1_src.get("avg_ret_t1_pct", 0.0), 0.0)

            amount = _safe_float(rt.get("amount", 0.0))
            vol = _safe_float(rt.get("volume", 0.0))

            if amount > 0 and vol > 0:
                tentative = amount / max(vol, 1e-9)
                vwap = amount / max(vol * 100.0, 1e-9) if tentative > now_price * 20 else tentative
            else:
                vwap = _safe_float(curr.get("vwap", now_price))

            cost_95th = _safe_float(rt.get("cost_95th", 9999.0))
            cyq_conc = _safe_float(rt.get("cyq_concentration", 999.0))

            df_atr = df.tail(15).copy()
            # 【V29 修复】原直接 df_atr["pre_close"]：若历史表未带 pre_close 列（回退到未含列的
            # 历史分弧来源）会直接 KeyError → run_all 抛异常 → 该票 burst_score=0 且为阵，
            # 低点零、高点零。改为从 close 利用 shift(1) 自行推导，丢失列时退回 ATR 估值。
            if "pre_close" in df_atr.columns:
                _pc_atr = df_atr["pre_close"].astype(float)
            elif "close" in df_atr.columns:
                _pc_atr = df_atr["close"].astype(float).shift(1)
            else:
                _pc_atr = pd.Series(index=df_atr.index, dtype=float)
            _pc_atr = _pc_atr.ffill().fillna(float(df_atr["close"].astype(float).iloc[0]) if len(df_atr) else 0.0)
            df_atr["tr1"] = df_atr["high"] - df_atr["low"]
            df_atr["tr2"] = (df_atr["high"] - _pc_atr).abs()
            df_atr["tr3"] = (df_atr["low"] - _pc_atr).abs()
            _tr = df_atr[["tr1", "tr2", "tr3"]].max(axis=1)
            _tr = pd.to_numeric(_tr, errors="coerce").replace(0.0, np.nan).dropna()
            atr = float(_tr.mean()) if len(_tr) else 0.0
            atr_pct = (atr / pre_close) * 100.0 if pre_close > 0 else 0.0
            if atr_pct <= 0.0:
                atr_pct = _safe_float(curr.get("atr_pct"), 2.0) or 2.0

            circ_mv_raw = rt.get("circ_mv")
            if pd.isna(circ_mv_raw) or circ_mv_raw is None:
                circ_mv_raw = _safe_float(df["circ_mv"].iloc[-1]) if "circ_mv" in df.columns else _safe_float(rt.get("total_mv", 10000000)) * 0.6
            circ_mv_yi = _safe_float(circ_mv_raw) / 10000.0

            if circ_mv_yi >= 2000.0:
                size_emoji = "🦍"
            elif circ_mv_yi >= 1000.0:
                size_emoji = "🐘+"
            elif circ_mv_yi >= 500.0:
                size_emoji = "🐘"
            else:
                size_emoji = "🐎"

            if circ_mv_yi < _p1_min_circ_mv_yi_strat():
                return res

            # 【V26.88+9-24 P3 重构】盈利质量注入 + 大盘温度注入 → 挪到风险否决前
            # 这样即使被 3 层风控否决，detail 里也能记录财务质量分数，便于审计/排查（与 P4/P5 对齐）
            # 【V34 审计修复 · P0】原式在首次赋值之前就引用 ts_code，每次都抛 NameError 并被
            # 下方 except 吞掉 → 全市场财务注入恒失败、_fin_quality_score 恒为「中性」。
            ts_code = str(rt.get("ts_code", rt.get("code", "")) or "").strip()
            try:
                if ts_code:
                    _inject_financial_quality_into_rt(rt, ts_code)
            except Exception as _e_fin:
                logger.debug("P3 财务注入失败（不影响主流程）: %s", _e_fin)

            # 【V26.88+9-24 P3 新增】大盘分层温度注入（按流通市值对应 large/mid/growth 层）
            try:
                from core.p1_precompute import compute_market_overheat_score
                _oh = compute_market_overheat_score()
                rt["_pre_market_overheat"] = _oh
                _layer = "growth"
                _cm_yi = float(circ_mv_yi)
                if _cm_yi >= 500.0:
                    _layer = "large"
                elif _cm_yi >= 100.0:
                    _layer = "mid"
                rt["_market_layer"] = _layer
                rt["_layer_overheat_score"] = int(
                    (_oh.get("layer_scores") or {}).get(_layer, _oh.get("market_overheat_score", 0))
                )
                rt["_market_overheat_score"] = int(_oh.get("market_overheat_score", 0))
                rt["_market_regime"] = str(_oh.get("regime", "neutral"))
            except Exception as _e_oh_p3:
                logger.debug("P3 大盘温度注入失败: %s", _e_oh_p3)

            # ================= 三层风控（最高优先级）：第一层死亡红线 =================
            rt_risk = dict(rt)
            rt_risk["_pool_key"] = str(rt.get("_pool_key", "p3"))
            _bj = timezone(timedelta(hours=8))
            _now = datetime.now(_bj)
            rt_risk.setdefault("curr_min", _now.hour * 60 + _now.minute)
            risk_pre = evaluate_3layer_risk(df, rt_risk, self._risk_cfg, is_right_side_strategy=False, pool_key="p3")
            # 【V26.88+9-24 P3】确保 detail 字典存在，把财务/温度/分数结果写入
            # 【2026-11-14 全链路演练修复 · P0】原此处 if 分支内调用 _build_p3_explain，
            # 引用尚未定义的 _explain_mainline/_explain_sector_rank（一旦执行必 NameError），
            # 且只在 "detail" not in res 时赋值 _explain_once → 下方 detail["explain"]
            # 无条件引用时抛 UnboundLocalError → P3 正式行全灭（信号产出为零）。
            # 现统一移动到 _explain_mainline/_explain_sector_rank 定义之后构建。
            res["detail"] = {}
            try:
                # 财务质量分由 evaluate_p3_intraday_screener 的 _fin_quality_score 算
                # 此处只先填温度，fin_score 在 screener 之后再覆盖
                res["detail"]["market_overheat_delta"] = float(
                    rt.get("_layer_overheat_score", rt.get("_market_overheat_score", 0))
                ) * 0.5  # P3 权重 0.5
                res["detail"]["market_overheat_score"] = int(rt.get("_market_overheat_score", 0))
                res["detail"]["market_layer"] = str(rt.get("_market_layer", "unknown"))
                res["detail"]["market_regime"] = str(rt.get("_market_regime", "neutral"))
                res["detail"]["fin_quality_score"] = 0.0  # 占位，等 screener 后覆盖
            except Exception:
                res["detail"]["market_overheat_delta"] = 0.0
                res["detail"]["fin_quality_score"] = 0.0
            if not risk_pre.get("pass_layer1", False):
                res["p3_veto_reason"] = str(risk_pre.get("veto_reason", "") or "")
                res["risk_control"] = {
                    "pass_layer1": False,
                    "pass_layer2": True,
                    "veto_reason": res["p3_veto_reason"],
                    "penalty": float(risk_pre.get("penalty", 0.0) or 0.0),
                    "risk_tags": list(risk_pre.get("risk_tags", []) or []),
                    "suggested_min_entry_score": float(risk_pre.get("suggested_min_entry_score", 0.0) or 0.0),
                    "ui_warnings": list(risk_pre.get("ui_warnings", []) or []),
                }
                res["risk_tags"] = list(risk_pre.get("risk_tags", []) or [])
                res["suggested_min_entry_score"] = float(risk_pre.get("suggested_min_entry_score", 0.0) or 0.0)
                return res

            # ================= 物理胸甲：硬阈值筛选（流式友好，仅尾部 + rt）=================
            ev = evaluate_p3_intraday_screener(df, rt, self._cfg)
            res["p3_core_screener_pass"] = bool(ev.get("p3_core_screener_pass"))
            res["p3_veto_reason"] = str(ev.get("veto_reason", "") or "")
            res["p3_strategy_checks"] = dict(ev.get("strategy_checks") or {})

            # 【V26.80 统一 A 股特色评分】P3 盘中场景：涨停板状态 + 炸板修复 + 次新/ST
            try:
                from core.p2_p5_a_share_enhance import unified_a_share_score

                _a_share = unified_a_share_score(df, rt)
                if _a_share.get("is_p2_p5_veto"):
                    res["p3_veto_reason"] = _a_share.get("veto_reason", "A 股特色高风险")
                    res["risk_tags"].append(f"🚨[统一风控]{_a_share.get('veto_reason')}")
                    return res

                # 把 A 股特色标签注入 risk_tags
                _a_share_tags = _a_share.get("all_tags", [])[:5]
                for tag in _a_share_tags:
                    if tag and tag not in res["risk_tags"]:
                        res["risk_tags"].append(tag)

                # A 股特色加权（盘中权重 0.15）
                _a_boost = float(_a_share.get("total_boost", 0.0)) * 0.15
                res["a_share_p3_boost"] = round(_a_boost, 2)
                res["a_share_p3_tags"] = _a_share_tags
            except Exception as _e_a3:
                logging.debug("V26.80 P3 统一 A 股特色评分跳过: %s", _e_a3)

            # 【V26.88 P3 优化①/③/④】量化横行预警透传到 risk_tags
            ev_detail = ev.get("detail") or {}
            quant_warns = list(ev_detail.get("quant_warnings") or [])
            for w in quant_warns:
                res["risk_tags"].append(w)
            # 滞涨/量化速度预警暂存（penalty 在后文定义后再加权）
            _q_penalty_extra = 0.0
            if ev_detail.get("is_stagnation"):
                _q_penalty_extra += 15.0
            if ev_detail.get("is_quant_speed"):
                _q_penalty_extra += 10.0

            if not ev.get("veto_pass"):
                return res

            hits = list(ev.get("strategies") or [])
            if not hits:
                return res

            # 【V26.80 跨池信号接力】P1→P2→P3 接力加分
            try:
                from core.p2_p5_signal_relay import apply_relay_bonus_to_pool_res

                if ts_code:
                    res = apply_relay_bonus_to_pool_res(res, ts_code, "p3", apply_to_burst=True)
            except Exception as _e_relay3:
                logger.debug("V26.80 P3 跨池接力跳过: %s", _e_relay3)

            tiered = {"core": [], "aux": [], "obs": []}
            for h in hits:
                tiered[_p3_strategy_tier(h)].append(h)
            core_hits = tiered["core"]
            aux_hits = tiered["aux"]
            obs_hits = tiered["obs"]

            # ================= 三层风控：第二层（右侧攻击专属） =================
            if hits_indicate_right_side_attack(core_hits or hits):
                l2 = evaluate_layer2_right_side_only(df, rt_risk, self._risk_cfg)
                for uw in l2.get("ui_warnings") or []:
                    res["risk_tags"].append("⚠️[二层预警]" + str(uw))
                if not l2.get("pass_layer2", True):
                    res["p3_veto_reason"] = str(l2.get("veto_reason", "") or "")
                    res["risk_control"] = {
                        "pass_layer1": True,
                        "pass_layer2": False,
                        "veto_reason": res["p3_veto_reason"],
                        "penalty": float(risk_pre.get("penalty", 0.0) or 0.0),
                        "risk_tags": list(risk_pre.get("risk_tags", []) or []),
                        "suggested_min_entry_score": float(risk_pre.get("suggested_min_entry_score", 0.0) or 0.0),
                        "ui_warnings": list(risk_pre.get("ui_warnings", []) or []),
                    }
                    res["risk_tags"] = list(risk_pre.get("risk_tags", []) or [])
                    res["suggested_min_entry_score"] = float(risk_pre.get("suggested_min_entry_score", 0.0) or 0.0)
                    return res

            penalty = 0.0
            if vr < 1.1:
                penalty += float(np.interp(vr, [0.0, 0.5, 1.1], [40.0, 20.0, 0.0]))
            if turnover_f < 0.8:
                penalty += float(np.interp(turnover_f, [0.0, 0.4, 0.8], [50.0, 20.0, 0.0]))
            # V26.88+9-22 优化22：HiThink 涨停真伪鉴别（A股 90% 假涨停都是大额封板后反复开板）
            try:
                _ht_lu = rt.get("_hithink_limit_up") or {}
                _ht_burst = rt.get("_hithink_limit_burst") or {}
                if _ht_lu:
                    # 涨停板封板状态细分（封死 > 半开 > 高开低走）
                    _seal_money = _safe_float(_ht_lu.get("seal_money"), 0.0)
                    _seal_yi = _seal_money / 1e8  # 转亿
                    _cnt = int(_safe_float(_ht_lu.get("continue_day_cnt"), 1) or 1)
                    _opened = bool(_ht_lu.get("is_open", False))
                    if _opened:
                        # 已开板（涨停当日回封失败）：真龙降为弱信号
                        penalty += 40.0
                        res["risk_tags"].append(f"🚨[已开板涨停]封板失败，主力诱多嫌疑")
                    elif _seal_yi < 0.3:
                        # 封单 < 3000 万 → 板不牢
                        penalty += 18.0
                        res["risk_tags"].append(f"⚠️[封单弱]封板仅{_seal_yi:.2f}亿，板不牢")
                    elif _seal_yi >= 1.0:
                        # 封单≥1亿 + 3板以上 → 真龙加分
                        if _cnt >= 3:
                            res["risk_tags"].append(f"💪[真龙]封单{_seal_yi:.2f}亿{_cnt}板")
                # 炸板池命中：直接 penalty
                if _ht_burst:
                    _open_times = int(_safe_float(_ht_burst.get("open_times"), 0) or 0)
                    penalty += float(_open_times * 12.0)
                    res["risk_tags"].append(f"💣[炸板{_open_times}次]次日开盘大概率低开")
            except Exception as _e_lup3:
                logger.debug("[P3] HiThink 涨停真伪鉴别跳过: %s", _e_lup3)
            # 不再因低量比直接 return：★均线低吸 本身要求缩量，与旧版「vr<0.5 一刀切」冲突；极弱流动性的惩罚已计入 penalty

            upper_shadow = high_price - max(now_price, open_price)
            if upper_shadow > (now_price * 0.03):
                penalty += 45.0

            # 原VWAP下方惩罚已整合到上方【V26.88 P3 优化③】中，此处删除避免重复

            if regime_state:
                if any(k in regime_state for k in ["退潮", "单边下行", "主跌", "空头"]):
                    if any("P3-01" in h or "P3-03" in h or "P3-04" in h or "P3-05" in h or "P3-07" in h for h in hits):
                        penalty += 35.0
                        res["risk_tags"].append("🧯[退潮降权]进攻策略降权")
                elif any(k in regime_state for k in ["主升", "趋势", "修复"]):
                    penalty -= 8.0

            if regime_score < 0.35 and any("P3-01" in h or "P3-03" in h or "P3-04" in h or "P3-05" in h or "P3-07" in h for h in hits):
                penalty += 18.0
                res["risk_tags"].append("🧊[环境偏弱]进攻信号折价")

            # 冲高回落强化：A股盘中最怕追在分时高点，必须把“远离VWAP + 回落 + 放量”单独拎出来
            vwap_gap_pct = ((now_price - vwap) / vwap * 100.0) if vwap > 0 else 0.0
            high_gap_pct = ((high_price - now_price) / now_price * 100.0) if now_price > 0 else 0.0
            if vwap > 0 and pct_chg > 3.0 and now_price < vwap * 0.99:
                penalty += 100.0
                res["risk_tags"].append("💀[致命]冲高回落破均价")
            if pct_chg >= 2.5 and vwap_gap_pct > 1.4 and vr >= 1.6:
                penalty += 35.0
                res["risk_tags"].append("🧨[假突破]远离VWAP追高")
            if high_price > 0 and high_gap_pct > 1.8 and now_price < high_price * 0.985 and vr >= 1.5:
                penalty += 30.0
                res["risk_tags"].append("🧨[冲高回落]高位承接不足")

            # 贴近 95% 成本峰、获利盘极低、无量 → 上方套牢抛压未解放
            winner_rt = _safe_float(
                rt.get("winner_rate"), _safe_float(curr.get("winner_rate"), float("nan"))
            )
            if (
                cost_95th > 0
                and 0.95 * cost_95th <= now_price <= 0.99 * cost_95th
                and np.isfinite(winner_rt)
                and winner_rt < 60.0
                and vr < 2.0
            ):
                penalty += 80.0
                res["risk_tags"].append("🧱[重压]头顶套牢盘且未爆量")

            # 【V26.88 P3 优化】涨停板炸板检测（早盘死水之后）
            limit_up_result = self._check_limit_up_burst(rt, pre_close)
            if limit_up_result.get("penalty", 0.0) > 0:
                penalty += limit_up_result["penalty"]
                reason = limit_up_result.get("reason", "")
                if reason:
                    res["risk_tags"].append(f"🚨[涨停炸板]{reason}")
            elif limit_up_result.get("bonus", 0.0) > 0:
                # 一字板或稳健封板：加分标记到 strategies
                reason = limit_up_result.get("reason", "")
                if reason:
                    res["strategies"] = list(res.get("strategies", [])) + [f"💎[涨停]{reason}"]

            # 早盘「死水→午后偷袭」：用 9:35/10:30/11:25 三档量比地板（有则 min，无则退回 vr_1030）
            vmf = _safe_float(rt.get("vr_morning_floor", -1.0))
            if vmf < 0.0:
                vmf = _safe_float(rt.get("vr_1030", -1.0))
            if vmf >= 0.0 and vmf < 0.8 and vr > 1.5:
                penalty += 25.0

            # 【V26.88 P3 优化】午盘开盘时段处理：11:30-11:45 数据可能失真
            # 【V34 修复 · P1】必须用扫描时点（rt["curr_min"]），墙钟只在缺失时兜底：
            # 原来回测/补跑/延迟扫描时所有时段规则都按「现在几点」判，早盘/午盘/尾盘惩罚全部错位。
            curr_min = int(_safe_float(rt.get("curr_min", -1.0), -1.0))
            if curr_min < 0:
                curr_min = _now.hour * 60 + _now.minute
            if 690 <= curr_min <= 705:  # 11:30-11:45 午盘开盘
                penalty += 8.0
                res["risk_tags"].append("⏰[午盘开盘]情绪不稳定，建议等待稳定")

            # 【V26.88 P3 优化②】早盘冲高回落检测（A股最常见亏损模式）
            # 识别上午涨2%以上、但高点已回落超过1.5%的股票——典型的"追高被套"
            retreat_ratio = 0.0
            if 600 <= curr_min <= 780:  # 10:00-13:00 早盘时段
                if pct_chg > 2.0 and high_price > 0 and now_price > 0:
                    # 计算从高点回落的比例
                    retreat_ratio = (high_price - now_price) / high_price * 100.0
                    # 【V34 修复 · P1】先判深度回落再判普通回落：原式 >1.5 在前，>3.0 的 elif 永远不可达，
                    # 「深度回落 +40」这条从未生效过。
                    if retreat_ratio > 3.0:
                        penalty += 40.0
                        res["risk_tags"].append(f"🚨[早盘深度回落]从高点回落{retreat_ratio:.1f}%，极度危险")
                    elif retreat_ratio > 1.5:
                        penalty += 25.0
                        res["risk_tags"].append(f"⚠️[早盘冲高回落]从高点回落{retreat_ratio:.1f}%，追高风险大")

            # 【V26.88 P3 优化③】强化分时均价下方运行惩罚（防镰刀核心）
            # 价格长期在VWAP下方运行，说明资金在出货
            if now_price < vwap * 0.99:
                penalty += 20.0  # 原来只检测0.985，现在0.99就开始惩罚
                if now_price < vwap * 0.97:
                    penalty += 30.0  # 深度跌破VWAP
                    res["risk_tags"].append("⚠️[深度跌破均价]资金可能在出货")
                elif now_price < vwap * 0.985:
                    penalty += 15.0
                    res["risk_tags"].append("⚠️[跌破均价]价格运行在分时成本下方")

            # 【V26.88 P3 优化④】尾盘异动识别（尾盘拉升次日易低开）
            # 14:00后突然放量拉升，可能是第二天出货的前兆
            if curr_min >= 840:  # 14:00后
                if pct_chg > 3.0 and vr > 2.5:
                    penalty += 20.0
                    res["risk_tags"].append(f"⚠️[尾盘异动]尾盘放量拉升({pct_chg:.1f}%, vr={vr:.1f})，次日低开风险大")
                elif pct_chg > 2.0 and vr > 3.0:
                    # 异常放量更要警惕
                    penalty += 30.0
                    res["risk_tags"].append(f"🚨[尾盘异常放量]vr={vr:.1f}极度异常，次日低开概率高")

            # 【V26.88 P3 优化⑤】板块涨幅偏差检测
            # 个股涨幅与板块涨幅偏差过大 → 跟风或独自点火风险
            sector_strength = float(ev.get("detail", {}).get("sector_strength", 1.0) or 1.0)
            excess_pct = 0.0
            if sector_strength > 0:
                # 估算板块预期涨幅（sector_strength=1.0时板块平均涨幅约1%）
                sector_expected_pct = (sector_strength - 1.0) * 20.0
                excess_pct = pct_chg - sector_expected_pct
                # 个股涨幅超过板块超过3%但量比不足2.0 → 跟风嫌疑
                if excess_pct > 3.0 and vr < 2.0 and pct_chg > 2.0:
                    penalty += 18.0
                    res["risk_tags"].append(f"⚠️[涨幅偏离板块]个股涨幅超过板块{excess_pct:.1f}%，跟风拉升嫌疑")
                # 个股涨幅超过板块超过5% → 独自点火风险
                if excess_pct > 5.0 and pct_chg > 3.0:
                    penalty += 25.0
                    res["risk_tags"].append(f"🚨[独自点火]个股涨幅远超板块{excess_pct:.1f}%，次日低开风险大")

            is_vwap_support = False
            if 1.0 <= pct_chg <= 6.0 and vr >= 1.5:
                if vwap * 1.001 <= now_price <= vwap * 1.025:
                    is_vwap_support = True
            if regime_state and any(k in regime_state for k in ["退潮", "主跌", "空头"]):
                is_vwap_support = False
                penalty += 12.0
            if regime_score >= 0.65:
                penalty *= 0.92
            elif regime_score <= 0.35:
                penalty *= 1.08

            s_momentum = self._map_momentum(pct_chg, atr_pct, vr)
            s_kill = self._map_chip_kill(now_price, cost_95th)
            net_main_amt = float(rt.get("net_main_amount", curr.get("net_main_amount", 0.0) or 0.0))
            s_support = self._map_funds(net_main_amt, circ_mv_yi)

            regime_boost = 1.0
            if regime_state:
                if any(k in regime_state for k in ["主升", "趋势", "修复"]):
                    regime_boost = 1.08
                elif any(k in regime_state for k in ["退潮", "主跌", "空头"]):
                    regime_boost = 0.82
                else:
                    regime_boost = 0.96
            if regime_score >= 0.7:
                regime_boost = max(regime_boost, 1.05)
            elif regime_score < 0.35:
                regime_boost = min(regime_boost, 0.88)

            # 热门板块加成：尽量不改主框架，仅对主线/前排/强板块做轻量加权
            hot_sector_bonus = 0.0
            if float(ev.get("detail", {}).get("mainline_score", 0.0) or 0.0) >= 4.0:
                hot_sector_bonus = 10.0
            elif float(ev.get("detail", {}).get("mainline_score", 0.0) or 0.0) >= 3.0:
                hot_sector_bonus = 7.0
            elif float(ev.get("detail", {}).get("mainline_score", 0.0) or 0.0) >= 1.5:
                hot_sector_bonus = 4.0
            elif float(ev.get("detail", {}).get("mainline_score", 0.0) or 0.0) <= -2.0:
                hot_sector_bonus = -8.0
            elif regime_score < 0.35:
                hot_sector_bonus = -3.0
            hot_sector_bonus = float(max(-10.0, min(12.0, hot_sector_bonus)))

            # 【V34 修复 · P0】财务质量分此前只写进 detail、不参与打分（P3 盈利质量维度形同虚设）。
            # 与 P4 同口径：screener 输出 -3..+4 封到 ±2，避免财务面压过盘中量价。
            _fin_p3_score = float(ev_detail.get("fin_quality_score", 0.0) or 0.0)
            _fin_p3_score = max(-2.0, min(2.0, _fin_p3_score))

            raw_burst = ((s_momentum * 0.4) + (s_kill * 0.3) + (s_support * 0.3)) * regime_boost
            raw_burst += hot_sector_bonus
            if _fin_p3_score:
                raw_burst += _fin_p3_score
                res["risk_tags"].append(
                    f"💎[盈利质量]{ev_detail.get('fin_quality_reason', '')} {_fin_p3_score:+.1f}"
                )
            # 【V26.88+9-22 Z-Score 已移除】vol_z_bonus 恒为 0
            # 旧代码调用了 _volume_zscore_signal()，现已改为一常量：
            vol_z_bonus, vol_z_tag, vol_z_value = 0.0, "zscore_removed", None
            # Z-Score 永远不参与 core_hits；移除相关 detail 写入逻辑，保留结构字段为 null
            res["detail"]["vol_z_value"] = None
            res["detail"]["vol_z_reason"] = "zscore_removed_20260922"
            # T+1 记忆只进 surge_bonus（与 P2/P4 同口径），避免同一笔加分在 burst 与 surge 各算一次
            if is_vwap_support:
                raw_burst += 12.0
            if any("P3-01·★右侧起爆" in h or "★右侧起爆" in h for h in core_hits):
                raw_burst += 10.0
            if any("P3-02·★均线回踩低吸" in h or "★均线回踩低吸" in h for h in core_hits):
                raw_burst += 6.0
            if any("P3-04·★巨头连贯发力" in h or "★巨头连贯发力" in h for h in core_hits):
                raw_burst += 6.0
            if any("P3-07·★倍量启动延续" in h or "★倍量启动延续" in h for h in core_hits):
                raw_burst += 7.0

            # 【V26.88+9-24 P3 新增】龙头连板加权（HiThink 实时连板深度）
            # 3板+封单>1亿 → +8；3板 → +5；2板 → +3
            try:
                _ht_lu = rt.get("_hithink_limit_up") or {}
                if _ht_lu:
                    _ht_cnt = int(_ht_lu.get("continue_day_cnt", 0) or 0)
                    _ht_seal_yi = (_ht_lu.get("seal_vol") or 0.0) / 1e8
                    if _ht_cnt >= 3 and _ht_seal_yi >= 1.0:
                        raw_burst += 8.0
                        res["risk_tags"].append(f"💎[龙头连板]{_ht_cnt}板封单{_ht_seal_yi:.1f}亿 +8")
                    elif _ht_cnt >= 3:
                        raw_burst += 5.0
                        res["risk_tags"].append(f"💎[龙头连板]{_ht_cnt}板 +5")
                    elif _ht_cnt >= 2:
                        raw_burst += 3.0
                        res["risk_tags"].append(f"💎[2板]{_ht_lu.get('limit_up_reason', '')} +3")
            except Exception as _e_lianban:
                logger.debug("P3 连板加权异常: %s", _e_lianban)

            if is_vwap_support:
                raw_burst = max(raw_burst, 85.0)

            # 【V26.88+9-22 优化25】开盘 30 分钟分型确认（A 股必做的实战确认）
            # 【V26.88+10-1 P2-2 优化】权重 ±12/±15 → ±15/±20（这是 A 股最高赔率的失败模式）
            # 5 种分型:
            #   1) 高开高走站稳 VWAP → +15（真突破，多加仓）
            #   2) 高开低走破 VWAP → -20（假突破，警示）
            #   3) 低开高走返正 → +10（主力护盘）
            #   4) 低开低走跌幅扩大 → -30（主力出货嫌疑）
            #   5) 平开平走 → +0（中性）
            try:
                _open_px = _safe_float(rt.get("open"), 0.0)
                _now_px_3 = _safe_float(rt.get("price"), 0.0)
                _pre_close = _safe_float(rt.get("pre_close"), 0.0)
                _vwap_30 = _safe_float(rt.get("vwap_30min"), 0.0)
                if _open_px > 0 and _now_px_3 > 0 and _pre_close > 0 and _vwap_30 > 0:
                    _high_pct = (_open_px - _pre_close) / _pre_close * 100.0
                    _now_pct = (_now_px_3 - _pre_close) / _pre_close * 100.0
                    if _high_pct > 1.0 and _now_px_3 > vwap:  # 高开高走站稳 VWAP
                        raw_burst += 15.0
                        res["risk_tags"].append("✅[真突破]高开高走站稳VWAP")
                    elif _high_pct > 1.0 and _now_px_3 < vwap * 0.995:  # 高开低走
                        raw_burst -= 20.0
                        res["risk_tags"].append("⚠️[假突破]高开低走远离VWAP")
                    elif _high_pct < -1.0 and _now_pct > 0:  # 低开高走
                        raw_burst += 10.0
                        res["risk_tags"].append("💪[主力护盘]低开高走翻正")
                    elif _high_pct < -1.0 and _now_pct < _high_pct + 0.5:  # 低开低走扩大
                        raw_burst -= 30.0
                        res["risk_tags"].append("🚨[主力出货]低开低走跌幅扩大")
            except Exception as _e_p3fa:
                logger.debug("[P3] 开盘分型确认跳过: %s", _e_p3fa)

            # 【V26.88+ P3 优化】周线趋势/放量 buff（互联网实战共识）：
            # 周线多头 + 周线放量 → +6 双强；任一满足 → +3；空头+缩量 → 不动。
            # 复用 P1 weekly 配置（trend_min=4.5 / volume_min=2.5），不引入新阈值。
            try:
                _p3_ts_code = str(
                    (rt.get("ts_code") if isinstance(rt, dict) else "")
                    or (df.get("ts_code").iloc[-1] if df is not None and not df.empty and "ts_code" in df.columns else "")
                    or ""
                ).strip()
                _p3_weekly_root = get_p1_weekly_config()
                _p3_wb = _compute_weekly_buff(_p3_ts_code, _p3_weekly_root)
                raw_burst += _p3_wb["pts"]
            except Exception as _p3_wbe:
                logger.debug("P3 周线 buff 计算异常: %s", _p3_wbe)
                _p3_wb = {"pts": 0.0, "trend": 0.0, "vol": 0.0,
                          "is_double_strong": 0.0, "is_trend_hit": 0.0, "is_vol_hit": 0.0}

            # 【V26.88+9-24 P3 新增】分层大盘温度加权（按股票市值分层取对应温度）
            # 过冷 +3 → +1.5 分；过热 -3 → -1.5 分；最大幅度 1.5 分，避免压制个股选择
            try:
                _layer_oh = int(rt.get("_layer_overheat_score", 0))
                if _layer_oh != 0:
                    _oh_delta = float(_layer_oh) * 0.5
                    raw_burst += _oh_delta
                    _layer_name = str(rt.get("_market_layer", "?"))
                    _oh_label = {-3: "过热", -2: "偏热", -1: "微热",
                                 1: "微冷", 2: "偏冷", 3: "超冷"}.get(_layer_oh, "")
                    res["risk_tags"].append(
                        f"🌡️[大盘温度][{_layer_name}层]{_oh_label}({_layer_oh:+d})  {_oh_delta:+.1f}分"
                    )
            except Exception as _e_oh_b:
                logger.debug("P3 大盘温度加权异常: %s", _e_oh_b)

            res["burst_score"] = round(min(raw_burst, 100.0), 2)
            res["surge_bonus"] = self._check_second_surge(df, rt) if len(df) >= 60 else 0.0
            res["surge_bonus"] = round(
                _safe_float(res["surge_bonus"], 0.0) + min(10.0, max(0.0, t1_boost)), 2
            )
            # 【V26.88 P3 优化①/④】量化预警 → penalty 加权折价
            penalty += _q_penalty_extra
            # 【V26.88 P3 优化①】强化跟风/独自点火惩罚权重（防镰刀核心）
            # 跟风拉升：板块强但个股涨幅远超板块 → 量化被动溢价买入
            # 独自点火：板块弱但个股独自拉升 → 主力控盘但次日易低开
            if ev_detail.get("is_crowd_follower"):
                penalty += 18.0  # 原12→18，强化惩罚
                res["risk_tags"].append("⚠️[跟风]个股涨幅远超板块，量化溢价买入风险")
            elif ev_detail.get("is_crowd_phantom"):
                penalty += 28.0  # 原18→28，独自点火风险更高
                res["risk_tags"].append("⚠️[独自点火]板块弱势但个股独自拉升，次日低开风险大")
            elif ev_detail.get("is_crowd_leader"):
                # 龙头有额外奖励，在后文 hot_sector_bonus 中体现
                pass
            res["penalty"] = round(penalty, 2)
            entry_reason = "承接好，能继续走"
            if float(ev.get("detail", {}).get("mainline_score", 0.0) or 0.0) >= 3.0:
                entry_reason = "主线前排，容易走强"
            elif any("P3-02" in h or "P3-06" in h or "P3-09" in h for h in core_hits):
                entry_reason = "回踩确认，不怕假拉"
            elif float(ev.get("detail", {}).get("mainline_score", 0.0) or 0.0) <= -2.0:
                entry_reason = "不像主线，防假强"

            # 【V26.88+ Bugfix】mainline_score / sector_rank 原为引用未定义变量
            # (P3 文件内仅以 ev.get("detail", {}).get(...) 形式消费)，这里就地
            # 从 ev.detail 取，与 P2 (strat_p2_auction.py:478) 保持一致契约。
            _explain_mainline = float(
                ev.get("detail", {}).get("mainline_score", 0.0) or 0.0
            )
            _explain_sector_rank = int(
                float(ev.get("detail", {}).get("sector_rank", 999.0) or 999.0)
            )
            # 【2026-11-14 全链路演练修复】explain 三参（hits/pct_chg/vr）与
            # limit_up_result 此刻均已在作用域内，统一在此构建；失败降级为空 dict，
            # 保证下方 detail["explain"] 永远有定义（P3 正式行不再抛 UnboundLocalError）。
            try:
                _explain_once = self._build_p3_explain(
                    hits, pct_chg, vr, _explain_mainline, _explain_sector_rank, limit_up_result,
                )
            except Exception as _e_ex:
                logger.debug("P3 explain 构建失败（降级为空）: %s", _e_ex)
                _explain_once = {}

            res["detail"] = {
                "性格动量": round(s_momentum, 2),
                "绞杀分": round(s_kill, 2),
                "时权抢筹": round(s_support, 2),
                "入池理由": entry_reason,
                # 【V34 修复 · P1】原式此处整体重建 dict，把前面写入的温度/财务诊断全部冲掉；
                # 这里补齐，保证「算了 — 用了 — 看得见」三段闭合。
                "market_overheat_score": int(_safe_float(rt.get("_market_overheat_score", 0), 0.0)),
                "market_layer": str(rt.get("_market_layer", "unknown")),
                "market_regime": str(rt.get("_market_regime", "neutral")),
                "market_overheat_delta": round(
                    _safe_float(rt.get("_layer_overheat_score", 0), 0.0) * 0.5, 2
                ),
                "fin_quality_score": round(float(ev_detail.get("fin_quality_score", 0.0) or 0.0), 2),
                "fin_quality_reason": str(ev_detail.get("fin_quality_reason", "") or ""),
                "市值档位": size_emoji,
                "regime_state": regime_state or "--",
                "regime_score": round(regime_score, 3),
                "sector_strength": round(float(ev.get("detail", {}).get("sector_strength", 1.0) or 1.0), 3),
                "mainline_score": round(float(ev.get("detail", {}).get("mainline_score", 0.0) or 0.0), 2),
                "mainline_reason": str(ev.get("detail", {}).get("mainline_reason", "") or ""),
                "hot_sector_bonus": round(float(hot_sector_bonus), 2),
                "t1_memory_boost": round(t1_boost, 2),
                "t1_win_rate_pct": round(t1_win, 1),
                "t1_avg_ret_pct": round(t1_avg, 3),
                "is_vwap_support": bool(is_vwap_support),
                # 【V26.88 P3 优化②】板块超额强度因子
                "excess_strength": round(float(ev_detail.get("excess_strength", 0.0) or 0.0), 2),
                "crowd_type": str(ev_detail.get("crowd_type", "normal")),
                # 【V26.88 P3 优化①/③/④】量化横行预警
                "quant_warnings": quant_warns,
                # 【V26.88+】60日成交量切片 Z-Score（vol_bonus 已在主打分累加；这里只暴露诊断值）
                # 【V26.88+ 契约】vol_z_value 必为 float/None；失败原因已在 detail.vol_z_reason
                # 【V26.88+9-22 Z-Score 已移除】vol_z_bonus 恒为 0，z_value 恒为 None
                "volume_zscore": {
                    "z_value": None,
                    "bonus": 0.0,
                    "tag": "zscore_removed_20260922",
                },
                "is_stagnation": bool(ev_detail.get("is_stagnation", False)),
                "is_t1_risk": bool(ev_detail.get("is_t1_risk", False)),
                "is_quant_speed": bool(ev_detail.get("is_quant_speed", False)),
                "p3_screener": ev_detail,
                # 【V26.88 P3 优化】防镰刀新增检测
                "早盘冲高回落": round(float(max(0.0, retreat_ratio)), 2),
                "尾盘异动检测": bool(curr_min >= 840 and pct_chg > 3.0 and vr > 2.5),
                "板块涨幅偏差": round(float(excess_pct), 2),
                "VWAP偏离度": round(float(vwap_gap_pct), 2),
                # 【V26.88 P3 优化】小白友好详细解释
                "explain": {
                    "why_selected": _explain_once.get("why_selected", ""),
                    "risk_note": _explain_once.get("risk_note", ""),
                    "time_hint": self._build_time_hint(curr_min, pct_chg, vr, core_hits),
                },
                # 【V26.88 P3 优化】止损提示
                "stop_loss_hint": self._build_stop_loss_hint(pct_chg, vr, core_hits, _explain_mainline),
            }
            # 分层输出：主战法 / 辅助标签 / 观察项
            res["detail"]["core_hits"] = core_hits
            res["detail"]["aux_hits"] = aux_hits
            res["detail"]["obs_hits"] = obs_hits
            # 【V26.88+ P3 优化】周线 buff 明细（前端透传 / 阵列仓诊断可见）
            res["detail"]["weekly_buff_pts"] = round(_p3_wb.get("pts", 0.0), 2)
            res["detail"]["weekly_buff_trend"] = round(_p3_wb.get("trend", 0.0), 2)
            res["detail"]["weekly_buff_vol"] = round(_p3_wb.get("vol", 0.0), 2)
            res["detail"]["weekly_buff_double"] = bool(_p3_wb.get("is_double_strong", 0.0) >= 1.0)
            if _p3_wb.get("is_double_strong", 0.0) >= 1.0:
                res["detail"]["aux_hits"] = list(res["detail"]["aux_hits"]) + ["🪐[周线双强] 多头+放量共振"]
            # 【V34 修复 · P2】整体覆盖会冲掉前面追加的 💎[涨停] 标签，先摘出来再拼回
            _limit_up_tags = [
                s for s in (res.get("strategies") or [])
                if isinstance(s, str) and s.startswith("💎[涨停]")
            ]
            res["strategies"] = _limit_up_tags + core_hits + aux_hits + obs_hits
            buy_hint = "盘中信号偏强，可轻仓分批，站稳VWAP再加。"
            if core_hits:
                if float(ev.get("detail", {}).get("mainline_score", 0.0) or 0.0) >= 3.0:
                    buy_hint = f"主线板块龙头可先轻仓跟随，回踩VWAP或分时均线确认后再加。{self._build_time_hint(curr_min, pct_chg, vr, core_hits)}"
                elif any("P3-02" in h or "P3-06" in h or "P3-09" in h for h in core_hits):
                    buy_hint = f"偏低吸类信号，先等分时企稳不破VWAP再试仓。{self._build_time_hint(curr_min, pct_chg, vr, core_hits)}"
                else:
                    buy_hint = f"右侧强势信号，建议分批跟随，避免一次性满仓。{self._build_time_hint(curr_min, pct_chg, vr, core_hits)}"
            elif aux_hits:
                buy_hint = f"辅助信号为主，先观察承接，等主线确认再介入。{self._build_time_hint(curr_min, pct_chg, vr, core_hits)}"
            else:
                buy_hint = f"信号偏弱，先观察，不追高。{self._build_time_hint(curr_min, pct_chg, vr, core_hits)}"
            if float(ev.get("detail", {}).get("mainline_score", 0.0) or 0.0) <= -2.0:
                buy_hint = "疑似假强板块，先观望，等板块回到主线再考虑。"
            res["buy_hint"] = buy_hint
            res["wechat_hint"] = buy_hint
            res["fake_breakout_guard"] = bool(not ev.get("veto_pass", True) and "假突破" in str(ev.get("veto_reason", "")))

            # 【V26.88+9-22 优化28+21+27】P3 入场质量评估 + 自动止损 + EV 评分
            try:
                from core.stop_loss_utils import (
                    evaluate_entry_quality, compute_expected_value,
                )
                _mainline = float(ev.get("detail", {}).get("mainline_score", 0.0) or 0.0)
                strat_type = "leader" if _mainline >= 3.0 else ("main_wave" if core_hits else "default")
                _eq = evaluate_entry_quality(df, rt, risk_reward_min=2.0, strategy_type=strat_type)
                _ev_score = compute_expected_value(
                    risk_reward=_eq["risk_reward_ratio"],
                    signal_strength=float(res.get("burst_score", 60.0) or 60.0),
                    penalty=float(res.get("penalty", 0.0) or 0.0),
                )
                res["entry_quality"] = _eq
                res["expected_value"] = _ev_score
                res["auto_stop_loss"] = _eq["stop_loss"]
                res["auto_take_profit"] = _eq["take_profit"]
                res["trailing_stop_rules"] = _eq["trailing_stop_rules"]
                res["trade_grade"] = _ev_score["trade_grade"]
                # 质量低时附加标签
                if not _eq["pass_2_1"] and _eq["entry_quality_score"] < 50:
                    res["risk_tags"].append(
                        f"📉[赔率低]RR={_eq['risk_reward_ratio']:.1f} 评级{_ev_score['trade_grade']}"
                    )
            except Exception as _e_aq3:
                logging.debug("[P3] 入场质量评估跳过: %s", _e_aq3)


            core_cnt = len(core_hits)
            aux_cnt = len(aux_hits)
            obs_cnt = len(obs_hits)
            if core_cnt == 0:
                raw_burst *= 0.85
            elif core_cnt == 1:
                raw_burst *= 1.03
            elif core_cnt >= 2:
                raw_burst *= 1.08
            if aux_cnt:
                raw_burst += min(6.0, aux_cnt * 1.5)
            if obs_cnt:
                raw_burst += min(2.0, obs_cnt * 0.3)

            # 【V29 Bugfix】多策略共振结果必须回写 burst_score：
            # 旧代码在 res[burst_score] 已定型之后才改 raw_burst，导致 core/aux/obs 数量加成全部丢失。
            res["burst_score"] = round(min(100.0, max(0.0, raw_burst)), 2)

            # 观察项只给信息，不抢主分
            if any("缩量分歧低吸" in h for h in obs_hits):
                res["risk_tags"].append("👀【观察】缩量分歧低吸")

            # 并入第三层扣分、建议最低买入分、标签展示
            # 【V34 修复 · P0】merge_risk_into_engine_result 内部 base = dict(base) 后返回新字典，
            # 原式不接收返回值 → 风控 penalty / risk_tags / risk_control / 建议最低分全部落空，
            # 三层风控只剩 pass_layer1 的直接否决还在生效。
            # deduct_burst_score=False：pipeline 已按 res["penalty"] 单独扣一次，避免双重扣分（与 P4 对齐）。
            res = merge_risk_into_engine_result(
                res, risk_pre, penalty_key="penalty", deduct_burst_score=False,
            )

            # 【V34 新增 · P2】P3 此前没有任何仓位口径，下游只能用统一默认仓位买。
            # 放在 merge 之后：阶梯必须看到合并后的 penalty 与风控建议最低分。
            try:
                _pos, _pos_ladder = self._p3_position_ladder(
                    score=float(res.get("burst_score", 0.0) or 0.0),
                    penalty=float(res.get("penalty", 0.0) or 0.0),
                    core_cnt=core_cnt,
                    aux_cnt=aux_cnt,
                    curr_min=int(curr_min),
                    layer_overheat=int(_safe_float(rt.get("_layer_overheat_score", 0), 0.0)),
                    trade_grade=str(res.get("trade_grade", "") or ""),
                    suggested_min=_safe_float(res.get("suggested_min_entry_score", 0.0), 0.0),
                )
                res["max_position_ratio"] = round(float(_pos), 3)
                res["position_ladder"] = _pos_ladder
                res["detail"]["max_position_ratio"] = round(float(_pos), 3)
                res["detail"]["position_reason"] = str(_pos_ladder.get("reason", "") or _pos_ladder.get("tier", ""))
            except Exception as _e_pos:
                logger.debug("[P3] 仓位阶梯跳过: %s", _e_pos)
            try:
                res["wechat_hint"] = self._p3_wechat_hint(rt, res, int(curr_min))
            except Exception:
                res["wechat_hint"] = f"P3盘中|分{float(res.get('burst_score', 0.0) or 0.0):.0f}"

            res["detail"]["strategy_tier_counts"] = {"core": core_cnt, "aux": aux_cnt, "obs": obs_cnt}

        except Exception as e:
            # 【V26.88 P3 安全】不再 silent skip；写入 p3_veto_reason 让上层明确"该票 P3 评分崩溃"
            logging.exception("P3 run_all 异常: %s", e)
            res["burst_score"] = 0.0
            res["p3_veto_reason"] = f"run_all异常:{type(e).__name__}:{str(e)[:120]}"
            return res

        # === V28 真实接入 signal_log ===
        try:
            from core.pool_signal_adapter_v28 import append_signal_log_safe
            # 【V30.10 修复 2026-10-21】_GLOBAL_DB_CONN 是全项目从未赋值的死变量，
            # 恒为 None → P3 信号从不落库。改为 None 传入，append_signal_log_safe
            # 内部会用 resolve_write_conn() 自动解析可用连接。
            db_conn = None
            try:
                from core.push_template_momentum_reversal import apply_push_template_to_res
                apply_push_template_to_res(res)
            except Exception:
                pass
            try:
                from core.push_dedup import pool_apply_push_dedup
                pool_apply_push_dedup(res, "p3")
            except Exception:
                pass
            # 【V30.10 阈值统一 2026-10-21】push_min_score 改为从 config.yaml 动态解析
            # （core/push_threshold.resolve_push_min_score），不再硬编码。
            from core.push_threshold import resolve_push_min_score
            append_signal_log_safe(res, "p3", db_conn,
                                    push_min_score=resolve_push_min_score("p3", for_log=True),
                                    rt=rt)
        except Exception:
            pass

        return res
