# -*- coding: utf-8 -*-
"""
小杰AI选股系统 Pro V26.88 - 全市场秒级回测验证引擎（接口智能感知版）
【终极修复】
1. 智能接口适配：修复了部分引擎没有 run_all 方法导致的静默崩溃，加入 hasattr 智能感知。
2. 引擎补全：强力注入 GoldenTenStrategies (金·共振引擎)，彻底修复共振战法无法回测的 Bug。
3. 动态路由：将带有“共振”关键字的战法精准路由给 P4/P5 引擎。
4. 换手率防爆：将 circ_mv 和 total_mv 注入 mock_rt 盘口，防止回测过程中触发 3000% 畸形换手率拦截。
"""
import argparse
import sys
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import platform
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
import traceback
from typing import Any, Dict, List, Optional, Set

# 🚀 引入基建
import constants
from data.db_core import get_all_stock_codes, get_stock_data_qfq

from core.backtest_context import reset_backtest_context, set_backtest_legacy_mode
from core.backtest_painpoint_config import (
    CIRC_MV_WAN_MAX,
    CIRC_MV_WAN_MIN,
    PainpointWindow,
    circ_mv_in_horse_elephant_band,
    parse_pain_period,
)
from core.danger_signal_utils import size_emoji_from_circ_mv_wan, would_trigger_danger_sell

# ==================== 动态导入所有战法引擎 ====================
try:
    from core.strategies.fund_mv_utils import infer_turnover_rate_f_pct
except ImportError:
    def infer_turnover_rate_f_pct(vol_hand, close, circ_mv_wan):
        if vol_hand <= 0 or close <= 0 or circ_mv_wan <= 0:
            return 0.0
        return vol_hand * close / circ_mv_wan

try:
    from core.strategies.strat_p2_auction import P2Auction
    from core.strategies.strat_p3_intraday import P3Intraday
    from core.strategies.strat_p4_tail import P4Tail
    from core.strategies.strat_p5_postmarket import P5Postmarket
    # 【V26.88+9-27 修复O3】改用 score_calibration.compute_golden_institutional_bonus
    # GoldenTenStrategies 已废弃，不再 import
except ImportError as e:
    logging.error(f"⚠️ 战法引擎缺失: {e}。请确保 strategies 目录下文件完整。")
    class DummyEngine:
        def run_all(self, df, rt): return []
        def evaluate(self, df, rt, *args, **kwargs): return [], 0.0, 0.0
    P2Auction = P3Intraday = P4Tail = P5Postmarket = DummyEngine

# 跨平台字体适配
system_name = platform.system()
if system_name == "Windows": plt.rcParams['font.sans-serif'] = ['SimHei']
elif system_name == "Darwin": plt.rcParams['font.sans-serif'] = ['Arial Unicode MS']
else: plt.rcParams['font.sans-serif'] = ['DejaVu Sans']
plt.rcParams['axes.unicode_minus'] = False

# 实例化所有引擎
p2_engine = P2Auction()
p3_engine = P3Intraday()
p4_engine = P4Tail()
p5_engine = P5Postmarket()
# 【V26.88+9-27 修复O3】金共振改用 score_calibration.compute_golden_institutional_bonus
try:
    from core.strategies.score_calibration import compute_golden_institutional_bonus
    def golden_engine(sub_df, mock_rt, pool_key='p4', ind_rank=999, net_elg=0.0, cyq=999.0):
        try:
            return compute_golden_institutional_bonus(
                df=sub_df, rt=mock_rt, pool_key=pool_key, ind_rank=ind_rank,
                net_main_amount=net_elg, cyq=cyq,
            )
        except Exception as e:
            logging.debug("回测金共振函数调用失败(降级): %s", e)
            return [], 0.0, 0.0
except ImportError:
    logging.warning("compute_golden_institutional_bonus 不可用,回测金共振降级")
    golden_engine = None

def calculate_mdd(returns_array):
    """【纯 Numpy 极速计算】最大回撤"""
    if len(returns_array) == 0:
        return 0.0
    cum_ret = np.cumprod(1 + returns_array / 100.0)
    peak = np.maximum.accumulate(cum_ret)
    # 极端连亏导致 cum_ret→0 时 peak 可能为 0，须避免除零与 nan 污染整条回测链路
    with np.errstate(divide="ignore", invalid="ignore"):
        drawdown = np.where(peak > 1e-15, (cum_ret - peak) / peak, 0.0)
    return float(abs(np.min(drawdown)) * 100)

# ======================================================================
# Tier 2 · 真实成交口径（core.execution_model 为唯一真值）
# 原则：【只新增指标，绝不覆盖既有 win_rate / pnl_ratio / mdd 等旧字段】。
#       新旧两套数字并列输出，由使用方自行对比。
# ======================================================================
_REAL_NOTIONAL = 100000.0   # 每笔虚拟下单金额（用于把固定佣金摊成百分比）


# 【Tier 2.2】旧口径曾用 buy_delay=0（信号当日开盘成交，前视偏差）的三条战法。
# 它们现在与其他战法一样主口径为次日开盘成交；本常量仅用于在 metrics 里标记
# 「该战法旧口径含前视偏差」，并在 legacy_delay0 块里给出对照数字。
_LOOKAHEAD_LEGACY_KEYS = ("01_一进二", "02_黄金缺口", "29_量比异动")


def calc_risk_metrics(returns_array, hold_days: float = 5.0) -> Dict[str, float]:
    """在「单笔百分比收益」序列上补算 Sharpe / Sortino / Calmar / VaR95。

    - sharpe_per_trade : 单笔均值 / 单笔标准差
    - sharpe_annual    : 按 250 个交易日 ÷ hold_days 折算年化
    - sortino_annual   : 只用下行波动
    - calmar           : 年化收益 / 最大回撤
    - var95            : 单笔收益 5% 分位（负数代表单笔亏损风险）
    """
    arr = np.asarray(returns_array, dtype=float)
    arr = arr[np.isfinite(arr)]
    out = {
        "sharpe_per_trade": 0.0, "sharpe_annual": 0.0,
        "sortino_annual": 0.0, "calmar": 0.0, "var95": 0.0, "n": int(arr.size),
    }
    if arr.size < 3:
        return out
    mu = float(np.mean(arr))
    sd = float(np.std(arr, ddof=1))
    per_year = 250.0 / max(1.0, float(hold_days or 5.0))
    out["sharpe_per_trade"] = round(mu / sd, 4) if sd > 1e-12 else 0.0
    out["sharpe_annual"] = round(out["sharpe_per_trade"] * float(np.sqrt(per_year)), 4)
    downside = arr[arr < 0]
    dsd = float(np.std(downside, ddof=1)) if downside.size >= 2 else 0.0
    out["sortino_annual"] = round(mu / dsd * float(np.sqrt(per_year)), 4) if dsd > 1e-12 else 0.0
    mdd = float(calculate_mdd(arr))
    out["calmar"] = round(mu * per_year / mdd, 4) if mdd > 1e-9 else 0.0
    out["var95"] = round(float(np.percentile(arr, 5)), 4)
    return out


def real_exec_returns(ts_code, df, buy_idx, sell_idx, notional: float = _REAL_NOTIONAL,
                      slippage_bps: float = 0.0) -> Dict[str, Any]:
    """用 core.execution_model 计算「真实可成交」净收益序列（不替换旧口径）。

    规则：
      · 一字涨停买不进 / 一字跌停卖不出 / 成交量<=0 视作停牌不可成交
      · 滑点夹逼在当日 [low, high]
      · 真实费率：佣金万3(最低5元) + 印花税万5(仅卖出) + 过户费十万分之一
      · 股数按 100 股整手向下取整
    """
    res: Dict[str, Any] = {
        "n_signal": int(len(buy_idx)), "n_realized": 0,
        "n_buy_blocked": 0, "n_sell_blocked": 0,
        "net_returns": np.array([], dtype=float),
        "win_rate_net": 0.0, "avg_net_ret": 0.0, "cost_pct_avg": 0.0,
        "fill_rate": 0.0, "reason_top": {},
    }
    try:
        from core import execution_model as em
        o = pd.to_numeric(df["open"], errors="coerce").to_numpy(dtype=float)
        h = pd.to_numeric(df["high"], errors="coerce").to_numpy(dtype=float)
        l = pd.to_numeric(df["low"], errors="coerce").to_numpy(dtype=float)
        pc = pd.to_numeric(df["pre_close"], errors="coerce").to_numpy(dtype=float)
        vol_arr = None
        for c in ("vol", "volume"):
            if c in df.columns:
                vol_arr = pd.to_numeric(df[c], errors="coerce").to_numpy(dtype=float)
                break
        tds = df["trade_date"].astype(str).to_numpy()
    except Exception as e:                                    # pragma: no cover
        res["error"] = f"{type(e).__name__}: {e}"
        return res

    net: List[float] = []
    costs: List[float] = []
    reasons: Dict[str, int] = {}
    for b, s in zip(buy_idx, sell_idx):
        b, s = int(b), int(s)
        if b < 0 or s >= len(o) or b >= s:
            continue
        if not (np.isfinite(o[b]) and np.isfinite(h[b]) and np.isfinite(l[b])):
            reasons["无有效报价"] = reasons.get("无有效报价", 0) + 1
            res["n_buy_blocked"] += 1
            continue
        suspended_b = bool(vol_arr is not None and np.isfinite(vol_arr[b]) and vol_arr[b] <= 0)
        suspended_s = bool(vol_arr is not None and np.isfinite(vol_arr[s]) and vol_arr[s] <= 0)
        shares = em.round_lot(notional / o[b]) if o[b] > 0 else 0
        if shares <= 0 and o[b] > 0:
            shares = em.LOT_SIZE          # A 股最小 100 股：整笔金额不够一手时按一手计
        bf = em.buy_fill(o[b], h[b], l[b], pc[b], ts_code, shares,
                         slippage_bps=slippage_bps, suspended=suspended_b)
        if not bf.get("filled"):
            res["n_buy_blocked"] += 1
            k = str(bf.get("reason") or "买不进")
            reasons[k] = reasons.get(k, 0) + 1
            continue
        sf = em.sell_fill(o[s], h[s], l[s], pc[s], ts_code, bf["shares"],
                          buy_date=str(tds[b]), sell_date=str(tds[s]),
                          slippage_bps=slippage_bps, suspended=suspended_s)
        if not sf.get("filled"):
            res["n_sell_blocked"] += 1
            k = str(sf.get("reason") or "卖不出")
            reasons[k] = reasons.get(k, 0) + 1
            continue
        r = em.net_return_pct(bf["price"], sf["price"], bf["shares"], slippage_bps)
        net.append(float(r["net_pct"]))
        costs.append(float(r["cost_pct"]))

    arr = np.asarray(net, dtype=float)
    res["net_returns"] = arr
    res["n_realized"] = int(arr.size)
    if arr.size:
        res["win_rate_net"] = round(float(np.mean(arr > 0) * 100.0), 2)
        res["avg_net_ret"] = round(float(np.mean(arr)), 4)
        res["cost_pct_avg"] = round(float(np.mean(costs)), 4) if costs else 0.0
    res["fill_rate"] = round(res["n_realized"] / max(1, res["n_signal"]) * 100.0, 2)
    res["reason_top"] = dict(sorted(reasons.items(), key=lambda kv: -kv[1])[:5])
    return res


def realize_one_trade(ts_code, df, buy_pos: int, sell_pos: int,
                      notional: float = _REAL_NOTIONAL,
                      slippage_bps: float = 0.0) -> Optional[float]:
    """单笔真实净收益（%）。买在 buy_pos 开盘，卖在 sell_pos 收盘；不可成交返回 None。"""
    try:
        from core import execution_model as em
        o = float(df["open"].iloc[buy_pos])
        h = float(df["high"].iloc[buy_pos])
        l = float(df["low"].iloc[buy_pos])
        pc = float(df["pre_close"].iloc[buy_pos])
        so = float(df["open"].iloc[sell_pos])
        sh = float(df["high"].iloc[sell_pos])
        sl = float(df["low"].iloc[sell_pos])
        spc = float(df["pre_close"].iloc[sell_pos])
        bd = str(df["trade_date"].iloc[buy_pos])
        sd = str(df["trade_date"].iloc[sell_pos])
        shares = em.round_lot(notional / o) if o > 0 else 0
        if shares <= 0 and o > 0:
            shares = em.LOT_SIZE          # A 股最小 100 股
        bf = em.buy_fill(o, h, l, pc, ts_code, shares, slippage_bps=slippage_bps)
        if not bf.get("filled"):
            return None
        sf = em.sell_fill(so, sh, sl, spc, ts_code, bf["shares"],
                          buy_date=bd, sell_date=sd, slippage_bps=slippage_bps)
        if not sf.get("filled"):
            return None
        return float(em.net_return_pct(bf["price"], sf["price"], bf["shares"], slippage_bps)["net_pct"])
    except Exception:
        return None


def run_strategy_backtest(ts_code, strategy_key, generate_plot=True):
    """单只大盘股的战法回测逻辑 (极致性能优化版)"""
    # 动态选择引擎（加入“共振”关键词路由）
    if any(x in strategy_key for x in ["竞价", "拍卖", "P2", "01_一进二", "黄金缺口"]):
        engine = p2_engine
        engine_name = "P2竞价"
    elif any(x in strategy_key for x in ["P5", "真龙", "盘后"]):
        engine = p5_engine
        engine_name = "P5盘后"
    elif any(x in strategy_key for x in ["尾盘", "收盘", "P4", "82_", "83_", "84_", "共振"]):
        engine = p4_engine
        engine_name = "P4尾盘"
    else:
        engine = p3_engine
        engine_name = "P3盘中"

    df = get_stock_data_qfq(ts_code, limit=400) 
    warmup_period = 130 
    
    if df is None or df.empty or len(df) < warmup_period + 10: 
        return None, f"历史数据不足 (至少需 {warmup_period+10} 天)"
        
    signals, dates = [], []
    
    records = df.to_dict('records')
    # 与 scan_engine / 落库口径一致：主列为 vol_ma5，勿仅用 'vma' in name（会漏匹配 vol_ma5）
    if "vol_ma5" in df.columns:
        vma5_col = "vol_ma5"
    elif "vma5" in df.columns:
        vma5_col = "vma5"
    elif "vol" in df.columns:
        vma5_col = "vol"
    elif "volume" in df.columns:
        vma5_col = "volume"
    else:
        return None, "回测数据缺少成交量列 vol/volume/vol_ma5，无法计算量比"

    vol_col = 'vol' if 'vol' in df.columns else 'volume'
    
    df['vol_ma5_shifted'] = df[vma5_col].shift(1).fillna(0)
    df['vr_sim'] = np.where(df['vol_ma5_shifted'] > 0, df[vol_col] / df['vol_ma5_shifted'], 0.0)
    vr_sim_values = df['vr_sim'].values
    
    for i in range(warmup_period, len(df)):
        current_row = records[i]
        
        if float(current_row.get('total_mv', 0)) < constants.MIN_STRAT_MV:
            continue
            
        vol_raw = float(current_row.get(vol_col, current_row.get('vol', 0)) or 0)
        circ_wan = float(
            current_row.get('circ_mv', current_row.get('total_mv', 10000000)) or 0
        )
        close_px = float(current_row['close'] or 0)
        trf = float(current_row.get('turnover_rate_f') or 0)
        if trf <= 0 and vol_raw > 0 and close_px > 0 and circ_wan > 0:
            trf = infer_turnover_rate_f_pct(vol_raw / 100.0, close_px, circ_wan)

        mock_rt = {
            'price': current_row['close'], 'open': current_row['open'], 
            'high': current_row['high'], 'low': current_row['low'], 
            'pre_close': current_row['pre_close'], 'volume': vol_raw, 
            'amount': current_row['amount'], 'vol_ratio': vr_sim_values[i], 
            'turnover_rate_f': trf,
            'up_limit': current_row.get('up_limit', 0),
            # 与实盘 rt 对齐：缺失时默认为 0，避免 P5/共振把「无主力字段」误当成净流入
            'net_main_amount': current_row.get('net_main_amount', 0),
            'net_elg_amount': current_row.get('net_elg_amount', 0),
            'cyq_concentration': current_row.get('cyq_concentration', 999),
            'cost_50th': current_row.get('cost_50th', current_row['close']),
            'rz_net_buy': current_row.get('rz_net_buy', 0),
            'winner_rate': current_row.get('winner_rate', 0),
            'cost_5th': current_row.get('cost_5th', 0),
            'cost_95th': current_row.get('cost_95th', 0),
            'avg_cost': current_row.get('avg_cost', 0),
            'limit_times': current_row.get('limit_times', 0),
            'forecast_type': current_row.get('forecast_type', 0),
            'circ_mv': current_row.get('circ_mv', current_row.get('total_mv', 10000000)),
            'total_mv': current_row.get('total_mv', 10000000),
            'is_backtest': True,
            '_pool_key': 'p4' if engine_name == 'P4尾盘' else ('p5' if engine_name == 'P5盘后' else ''),
            # 日线回测只能模拟收盘时点；无分钟快照时 P4 不计算尾盘量能加减分。
            'curr_min': 900,
        }
        
        sub_df = df.iloc[:i+1]
        
        try:
            hits = []
            # 1. 🚀 智能路由运行常规主引擎，解决 AttributeError 静默崩溃
            res = {}
            if hasattr(engine, "run_all"):
                res = engine.run_all(sub_df, mock_rt)
            elif hasattr(engine, "evaluate"):
                res = engine.evaluate(sub_df, mock_rt)
                
            if isinstance(res, dict): hits.extend(res.get("strategies", []))
            elif isinstance(res, list): hits.extend(res)
            
            # 2. 强力运行 GoldenTen (共振引擎)
            # 【P0 漏洞修复】原代码硬编码 ind_rank = 1（"假设大盘股排名优异"），导致所有
            # 票在 GoldenTen 中都按"行业第 1 名"待遇参与共振计算，严重高估回测命中
            # 收益（特别是小盘票的真实 ind_rank 可能在 10~50 之间，权重会差 1~10 倍）。
            # 修复策略：
            #   (1) 优先从 current_row 读取 ind_rank / 行业排名字段；
            #   (2) 否则从 mock_rt 取 _industry_rank / ind_rank / sector_rank；
            #   (3) 否则从历史 sub_df 中由 net_main_amount 推断（rank by 主力净流入降序）；
            #   (4) 兜底使用 999（保守，避免虚高）。
            ind_rank = None
            # (1) current_row 直接字段
            for k in ("ind_rank", "industry_rank", "sector_rank", "申万排名", "板块排名"):
                v = current_row.get(k)
                if v is not None and v != "" and float(v) > 0:
                    try:
                        ind_rank = int(float(v))
                        break
                    except (TypeError, ValueError):
                        continue
            # (2) mock_rt 间接字段
            if ind_rank is None and isinstance(mock_rt, dict):
                for k in ("ind_rank", "industry_rank", "_industry_rank"):
                    v = mock_rt.get(k)
                    if v is not None and float(v) > 0:
                        try:
                            ind_rank = int(float(v))
                            break
                        except (TypeError, ValueError):
                            continue
            # (3) sub_df 行业内主力净流入降序推断（仅在已知 industry 时）
            if ind_rank is None:
                ind_val = current_row.get("industry") or current_row.get("industry_name") or current_row.get("申万一级")
                if ind_val and "industry" in sub_df.columns:
                    same_ind_mask = sub_df["industry"].astype(str) == str(ind_val)
                    if same_ind_mask.any():
                        # 用 sub_df 中已知主力净额做行业排名（最近一日）
                        sub_nm = pd.to_numeric(sub_df.get("net_main_amount", 0), errors="coerce").fillna(0.0)
                        sub_ind_nm = sub_nm[same_ind_mask]
                        cur_nm = float(sub_nm.iloc[-1]) if len(sub_nm) else 0.0
                        # 名次 = (行业 > 当前主力净额的数量) + 1
                        ind_rank = int((sub_ind_nm > cur_nm).sum() + 1)
            # (4) 兜底：999（保守）而非 1（虚高）
            if ind_rank is None or ind_rank <= 0:
                ind_rank = 999
            net_elg_val = mock_rt.get('net_elg_amount', 0)
            cyq_val = mock_rt.get('cyq_concentration', 999)

            if golden_engine is not None:
                gold_hits, _, _ = golden_engine(sub_df, mock_rt, pool_key='p4', ind_rank=ind_rank, net_elg=net_elg_val, cyq=cyq_val)
                if gold_hits: hits.extend(gold_hits)

            # 3. 匹配信号
            if any(strategy_key in hit for hit in hits):
                signals.append(i)
                dates.append(current_row['trade_date'])
        except Exception as e: 
            logging.error(f"回测推演异常 {ts_code} [{engine_name}]: {traceback.format_exc()}")

    if not signals: return None, "未触发该战法信号"

    hold_days = 5
    # 【Tier 2.2 前视偏差修正 2026-11-02】
    # 主成交口径统一为「信号次日开盘成交」（buy_delay=1）。
    # 旧实现给 01_一进二 / 02_黄金缺口 / 29_量比异动 三条战法用了 buy_delay=0，
    # 即「用当日收盘算出的信号，去买当日开盘价」——开盘早于信号产生，属前视偏差。
    # 现在：主数字一律 delay=1（诚实口径）；旧口径数字不再冒充主指标，
    # 单独放在 metrics["legacy_delay0"] 里供对比，历史结论仍可追溯。
    buy_delay = 1
    legacy_lookahead = any(k in strategy_key for k in _LOOKAHEAD_LEGACY_KEYS)

    sig_arr = np.array(signals)
    buy_idx = sig_arr + buy_delay
    sell_idx = sig_arr + hold_days
    
    valid_mask = (buy_idx < len(df)) & (sell_idx < len(df))
    valid_buy = buy_idx[valid_mask]
    valid_sell = sell_idx[valid_mask]
    total_trades = len(valid_buy)
    
    if total_trades == 0:
        return None, "触发信号后持仓天数不足，无法计算盈亏"
        
    open_arr = df['open'].values
    close_arr = df['close'].values
    
    buy_prices = open_arr[valid_buy] * (1 + 0.002 + 0.00025)
    sell_prices = close_arr[valid_sell] * (1 - 0.002 - 0.00025 - 0.0005)
    
    returns_array = np.where(buy_prices > 0, (sell_prices - buy_prices) / buy_prices * 100, 0)

    next_idx = sig_arr + 1
    next_valid = next_idx < len(df)
    next_pos = next_idx[next_valid]
    next_buy = open_arr[next_pos] * (1 + 0.002 + 0.00025)
    next_close = close_arr[next_pos] * (1 - 0.002 - 0.00025 - 0.0005)
    high_arr = df['high'].values
    low_arr = df['low'].values
    next_close_returns = np.where(next_buy > 0, (next_close - next_buy) / next_buy * 100, 0)
    next_mfe = np.where(next_buy > 0, (high_arr[next_pos] - next_buy) / next_buy * 100, 0)
    next_mae = np.where(next_buy > 0, (low_arr[next_pos] - next_buy) / next_buy * 100, 0)
    
    win_mask = returns_array > 0
    loss_mask = returns_array <= 0
    
    win_rate = (np.sum(win_mask) / total_trades) * 100
    avg_win = np.mean(returns_array[win_mask]) if np.any(win_mask) else 0
    avg_loss = np.mean(returns_array[loss_mask]) if np.any(loss_mask) else 0
    pnl_ratio = abs(avg_win / avg_loss) if avg_loss != 0 else (99.9 if avg_win > 0 else 0)
    mdd = calculate_mdd(returns_array)

    fig = None
    if generate_plot:
        fig, ax = plt.subplots(figsize=(10, 5))
        ax.plot(df['trade_date'], df['close'], label='股价', color='#333333', linewidth=1)
        ax.scatter(df.iloc[signals]['trade_date'], df.iloc[signals]['close'], color='red', marker='^', s=100, label='战法触发', zorder=5)
        title_str = f"{ts_code} - {strategy_key} ({engine_name}) (去水胜率: {win_rate:.1f}%, 盈亏比: {pnl_ratio:.2f})"
        ax.set_title(title_str)
        ax.legend(); plt.xticks(rotation=45); plt.tight_layout()
        
    next_total = int(len(next_close_returns))
    next_win_rate = float(np.mean(next_close_returns > 0) * 100.0) if next_total else 0.0
    metrics = {
        "win_rate": win_rate,
        "pnl_ratio": pnl_ratio,
        "mdd": mdd,
        "total": total_trades,
        "next_day_total": next_total,
        "next_day_close_return": float(np.mean(next_close_returns)) if next_total else 0.0,
        "next_day_close_median": float(np.median(next_close_returns)) if next_total else 0.0,
        "next_day_close_p10": float(np.percentile(next_close_returns, 10)) if next_total else 0.0,
        "next_day_win_rate": next_win_rate,
        "next_day_mfe": float(np.mean(next_mfe)) if next_total else 0.0,
        "next_day_mfe_median": float(np.median(next_mfe)) if next_total else 0.0,
        "next_day_mae": float(np.mean(next_mae)) if next_total else 0.0,
        "next_day_mae_p10": float(np.percentile(next_mae, 10)) if next_total else 0.0,
        "p4_proxy_mode": engine_name == "P4尾盘",
    }
    # ============ Tier 2 · 真实成交口径（与旧口径并列，绝不覆盖旧字段） ============
    metrics["buy_delay"] = int(buy_delay)
    metrics["hold_days"] = int(hold_days)
    # 【Tier 2.2】主口径已统一为次日开盘成交 → 主指标不再含前视偏差。
    metrics["lookahead_risk"] = False
    # 但该战法的「旧口径」曾含前视偏差（仅作历史标记，不影响主数字）
    metrics["legacy_lookahead_risk"] = bool(legacy_lookahead)
    try:
        _real = real_exec_returns(ts_code, df, valid_buy, valid_sell)
        _ra = _real.get("net_returns")
        metrics["real_exec"] = {
            "n_signal": int(_real["n_signal"]),
            "n_realized": int(_real["n_realized"]),
            "fill_rate_pct": float(_real["fill_rate"]),
            "n_buy_blocked": int(_real["n_buy_blocked"]),
            "n_sell_blocked": int(_real["n_sell_blocked"]),
            "win_rate_net": float(_real["win_rate_net"]),
            "avg_net_ret": float(_real["avg_net_ret"]),
            "cost_pct_avg": float(_real["cost_pct_avg"]),
            "reason_top": _real["reason_top"],
        }
        if isinstance(_ra, np.ndarray) and _ra.size >= 3:
            metrics["real_exec"]["mdd_net"] = calculate_mdd(_ra)
            metrics["risk_real"] = calc_risk_metrics(_ra, hold_days=hold_days)
        metrics["risk_legacy"] = calc_risk_metrics(returns_array, hold_days=hold_days)
    except Exception as _e_real:
        logging.debug("真实成交口径计算失败（不影响旧口径）: %s", _e_real)

    # 【Tier 2.2】旧口径（信号当日开盘买入，含前视偏差）仅作对照保留。
    # 主口径已是 delay=1，这里的 legacy 块不再是主数字，只用来量化「修正前后差多少」。
    if legacy_lookahead:
        try:
            _b0 = sig_arr + 0
            _s0 = sig_arr + hold_days
            _m0 = (_b0 < len(df)) & (_s0 < len(df))
            _bp = open_arr[_b0[_m0]] * (1 + 0.002 + 0.00025)
            _sp = close_arr[_s0[_m0]] * (1 - 0.002 - 0.00025 - 0.0005)
            _r0 = np.where(_bp > 0, (_sp - _bp) / _bp * 100, 0)
            _r0_real = real_exec_returns(ts_code, df, _b0[_m0], _s0[_m0])
            metrics["legacy_delay0"] = {
                "n_trades": int(_r0.size),
                "win_rate": float(np.mean(_r0 > 0) * 100.0) if _r0.size else 0.0,
                "avg_return": float(np.mean(_r0)) if _r0.size else 0.0,
                "n_realized": int(_r0_real["n_realized"]),
                "win_rate_net": float(_r0_real["win_rate_net"]),
                "avg_net_ret": float(_r0_real["avg_net_ret"]),
                "cost_pct_avg": float(_r0_real["cost_pct_avg"]),
                "note": "旧口径：信号当日开盘买入（前视偏差），仅供对比，不是主指标",
            }
        except Exception as _e_leg:
            logging.debug("旧口径对照计算失败: %s", _e_leg)
    # 【兼容】旧字段 real_exec_delay1 保留，现指向主口径（delay=1），避免旧依赖报错。
    metrics["real_exec_delay1"] = {
        "n_signal": int(metrics.get("real_exec", {}).get("n_signal", 0)),
        "n_realized": int(metrics.get("real_exec", {}).get("n_realized", 0)),
        "win_rate_net": float(metrics.get("real_exec", {}).get("win_rate_net", 0.0)),
        "avg_net_ret": float(metrics.get("real_exec", {}).get("avg_net_ret", 0.0)),
        "cost_pct_avg": float(metrics.get("real_exec", {}).get("cost_pct_avg", 0.0)),
        "note": "主口径已统一为次日开盘成交，此字段仅作旧命名兼容",
    }

    return {"metrics": metrics, "fig": fig}, "OK"

def run_batch_backtest(codes, strategy_key, progress_callback=None):
    """对底仓标的进行批量并行推演"""
    results = []
    
    test_codes = codes 
    total_codes = len(test_codes)
    completed = 0
    
    if progress_callback: progress_callback(0.0)
    
    with ThreadPoolExecutor(max_workers=constants.MAX_WORKERS) as executor:
        future_to_code = {executor.submit(run_strategy_backtest, code, strategy_key, False): code for code in test_codes}
        
        for future in as_completed(future_to_code):
            code = future_to_code[future]
            completed += 1
            if progress_callback: progress_callback(completed / total_codes)
            
            try:
                res, msg = future.result()
                if res: 
                    results.append({
                        "代码": code, 
                        "触发次数": res['metrics']['total'], 
                        "去水胜率%": round(res['metrics']['win_rate'], 1), 
                        "真实盈亏比": round(res['metrics']['pnl_ratio'], 2),
                        "最大回撤%": round(res['metrics']['mdd'], 1),
                        "次日胜率%": round(res['metrics'].get('next_day_win_rate', 0.0), 1),
                        "次日收盘收益%": round(res['metrics'].get('next_day_close_return', 0.0), 2),
                        "次日收盘中位数%": round(res['metrics'].get('next_day_close_median', 0.0), 2),
                        "次日收益P10%": round(res['metrics'].get('next_day_close_p10', 0.0), 2),
                        "次日MFE%": round(res['metrics'].get('next_day_mfe', 0.0), 2),
                        "次日MAE%": round(res['metrics'].get('next_day_mae', 0.0), 2),
                        "次日MAE_P10%": round(res['metrics'].get('next_day_mae_p10', 0.0), 2),
                    })
                elif "🚫" in str(msg):
                    logging.warning(msg)
            except Exception as e:
                logging.error(f"批量回测异常 {code}: {str(e)}")
            
    if progress_callback: progress_callback(1.0)
    
    columns = [
        "代码", "触发次数", "去水胜率%", "真实盈亏比", "最大回撤%",
        "次日胜率%", "次日收盘收益%", "次日收盘中位数%", "次日收益P10%",
        "次日MFE%", "次日MAE%", "次日MAE_P10%",
    ]
    return pd.DataFrame(results) if results else pd.DataFrame(columns=columns)


def _vol_z_at_index(df: pd.DataFrame, pos: int, vol_col: str) -> float:
    """与 scan_engine 类似的当日量相对近窗 Z 分数，供 danger_sell 止损分支使用。"""
    try:
        lo = max(0, pos - 59)
        tail = df.iloc[lo : pos + 1]
        if vol_col not in tail.columns:
            return 0.0
        s = pd.to_numeric(tail[vol_col], errors="coerce")
        if len(s) < 5:
            return 0.0
        vm = float(s.mean())
        vs = float(s.std())
        if not np.isfinite(vs) or vs <= 0:
            return 0.0
        cur = float(pd.to_numeric(df.iloc[pos][vol_col], errors="coerce") or 0.0)
        return float((cur - vm) / vs)
    except Exception:
        return 0.0


def _build_mock_rt_painpoint_row(row: pd.Series, vr_val: float, vol_col: str) -> Dict[str, Any]:
    vol_raw = float(row.get(vol_col, row.get("vol", 0)) or 0)
    circ_wan = float(row.get("circ_mv", row.get("total_mv", 10000000)) or 0)
    close_px = float(row.get("close") or 0)
    trf = float(row.get("turnover_rate_f") or 0)
    if trf <= 0 and vol_raw > 0 and close_px > 0 and circ_wan > 0:
        trf = infer_turnover_rate_f_pct(vol_raw / 100.0, close_px, circ_wan)
    return {
        "price": row["close"],
        "open": row["open"],
        "high": row["high"],
        "low": row["low"],
        "pre_close": row["pre_close"],
        "volume": vol_raw,
        "amount": row.get("amount", 0),
        "vol_ratio": vr_val,
        "turnover_rate_f": trf,
        "up_limit": row.get("up_limit", 0),
        "net_main_amount": row.get("net_main_amount", 0),
        "net_elg_amount": row.get("net_elg_amount", 0),
        "cyq_concentration": row.get("cyq_concentration", 999),
        "cost_50th": row.get("cost_50th", row["close"]),
        "rz_net_buy": row.get("rz_net_buy", 0),
        "winner_rate": row.get("winner_rate", 0),
        "cost_5th": row.get("cost_5th", 0),
        "cost_95th": row.get("cost_95th", 0),
        "avg_cost": row.get("avg_cost", 0),
        "limit_times": row.get("limit_times", 0),
        "forecast_type": row.get("forecast_type", 0),
        "circ_mv": circ_wan,
        "total_mv": row.get("total_mv", 10000000),
        "is_backtest": True,
    }


def _any_engine_signal_painpoint(sub_df: pd.DataFrame, mock_rt: Dict[str, Any]) -> bool:
    """P2–P5 任一引擎出非空 strategies 即视为「上车」信号。"""
    for eng in (p2_engine, p3_engine, p4_engine, p5_engine):
        try:
            if hasattr(eng, "run_all"):
                res = eng.run_all(sub_df, mock_rt)
                if isinstance(res, dict):
                    st = res.get("strategies")
                    if isinstance(st, list) and len(st) > 0:
                        return True
        except Exception as ex:
            logging.debug("painpoint run_all 跳过 %s: %s", getattr(eng, "name", eng), ex)
    return False


def run_painpoint_backtest(
    period: str,
    legacy_mode: bool = False,
    max_codes: Optional[int] = None,
) -> Dict[str, Any]:
    """
    痛点时段专项回测。

    统计口径：
    - 标的池：痛点窗口「最后一个交易日」流通市值落在 [100,500] 亿元（万元见 backtest_painpoint_config）。
    - 命中率：上述标的中，窗口内任一日触发 P2–P5 任一引擎信号的标的占比。
    - 平均持仓时长：对每个命中日，取计划持仓 min(5, 剩余可交易日至序列末尾) 的样本均值。
    - danger_sell：窗口内逐日调用 would_trigger_danger_sell 的累计次数（全样本）。
    - 主力连红>8 日：窗口内以当日为尾的连续 net_main_amount>0 天数≥8 时，T+5 收盘相对当日收盘涨幅%。
    """
    set_backtest_legacy_mode(bool(legacy_mode))
    try:
        win: PainpointWindow = parse_pain_period(period)
        codes = get_all_stock_codes()
        if not codes:
            return {"error": "daily_data 无股票代码或未建库", "period": period}

        if max_codes is not None:
            codes = codes[: int(max_codes)]

        warmup_period = 130
        eligible: List[str] = []
        hit_stocks: Set[str] = set()
        hold_days_samples: List[float] = []
        danger_sell_count = 0
        nm_streak_returns: List[float] = []
        first_hit_forward5d_returns: List[float] = []
        first_hit_forward5d_net: List[float] = []   # Tier2 · 真实成交口径（含费+不可成交剔除）

        vol_col_guess = "vol"
        processed = 0

        for ts_code in codes:
            df = get_stock_data_qfq(ts_code, limit=800)
            processed += 1
            if df is None or df.empty or "trade_date" not in df.columns:
                continue

            df = df.copy()
            df["trade_date"] = pd.to_datetime(df["trade_date"])
            vol_col = "vol" if "vol" in df.columns else "volume"
            vol_col_guess = vol_col

            win_mask = (df["trade_date"] >= win.start) & (df["trade_date"] <= win.end)
            if not win_mask.any():
                continue

            wdf = df.loc[win_mask]
            last_w = wdf.iloc[-1]
            circ_last = float(
                pd.to_numeric(
                    last_w.get("circ_mv", last_w.get("total_mv", 0)),
                    errors="coerce",
                )
                or 0.0
            )
            if not circ_mv_in_horse_elephant_band(circ_last):
                continue
            eligible.append(ts_code)

            vma5_col = (
                "vol_ma5"
                if "vol_ma5" in df.columns
                else ("vma5" if "vma5" in df.columns else vol_col)
            )
            df["vol_ma5_shifted"] = df[vma5_col].shift(1).fillna(0)
            df["vr_sim"] = np.where(
                df["vol_ma5_shifted"] > 0,
                pd.to_numeric(df[vol_col], errors="coerce") / df["vol_ma5_shifted"],
                0.0,
            )

            stock_any_hit = False
            first_hit_forward_done = False
            for pos in range(len(df)):
                td = df["trade_date"].iloc[pos]
                if td.normalize() < win.start.normalize() or td.normalize() > win.end.normalize():
                    continue
                if pos < warmup_period:
                    continue

                sub_df = df.iloc[: pos + 1]
                row = df.iloc[pos]
                vr_val = float(df["vr_sim"].iloc[pos])
                mock_rt = _build_mock_rt_painpoint_row(row, vr_val, vol_col)
                circ_wan_row = float(row.get("circ_mv", row.get("total_mv", 0)) or 0)
                sz_emoji = size_emoji_from_circ_mv_wan(circ_wan_row)
                vz = _vol_z_at_index(df, pos, vol_col)
                tr_ds, _ = would_trigger_danger_sell(sub_df, mock_rt, sz_emoji, 0, vz, 999)
                if tr_ds:
                    danger_sell_count += 1

                if _any_engine_signal_painpoint(sub_df, mock_rt):
                    stock_any_hit = True
                    rem = len(df) - 1 - pos
                    hold_days_samples.append(float(min(5, max(0, rem))))
                    if not first_hit_forward_done and pos + 5 < len(df):
                        c0 = float(pd.to_numeric(df.iloc[pos]["close"], errors="coerce") or 0.0)
                        c5 = float(pd.to_numeric(df.iloc[pos + 5]["close"], errors="coerce") or 0.0)
                        if c0 > 0 and np.isfinite(c5):
                            first_hit_forward5d_returns.append((c5 - c0) / c0 * 100.0)
                            # Tier2 · 同日信号改用「次日开盘买入 → 第 pos+5 日收盘卖出」的真实净收益
                            _net_r = realize_one_trade(ts_code, df, pos + 1, pos + 5)
                            if _net_r is not None:
                                first_hit_forward5d_net.append(_net_r)
                        first_hit_forward_done = True

            if stock_any_hit:
                hit_stocks.add(ts_code)

            if "net_main_amount" in df.columns:
                # 【P0 漏洞修复】原代码三重嵌套循环：
                #   for pos in range(len(df)):                          # O(n)
                #     for k in range(pos, -1, -1):                     # O(pos) ≈ O(n)
                #       ... + 阈值判定 + df.iloc[pos] / df.iloc[pos+5]   # O(1)
                # 单只股票复杂度 O(n²)，N=4000 时单只 16M 次，400 只 = 6.4G 次，
                # 实际回测单期 30 分钟以上，且 df.iloc[pos] 是低效的 pandas 标量访问。
                # 修复策略：先计算「截至 pos 的连续正流入长度」前缀数组 + 落窗起点掩码，
                # 最后用 numpy 向量化计算 5 日后收益，整体 O(n)。
                nm = pd.to_numeric(df["net_main_amount"], errors="coerce").fillna(0.0).to_numpy()
                n = len(nm)
                if n > 5:
                    # 1. 前缀连续正流入长度：pos 处 streak = nm[pos]>0 ? streak[pos-1]+1 : 0
                    pos_mask = nm > 0
                    streak_at = np.zeros(n, dtype=np.int32)
                    # 顺序扫描构建 streak 数组
                    for j in range(n):
                        if pos_mask[j]:
                            streak_at[j] = streak_at[j - 1] + 1 if j > 0 else 1
                        else:
                            streak_at[j] = 0
                    # 2. 命中条件：streak[pos] >= 8 且 pos+5 < n
                    valid_pos = (streak_at >= 8) & (np.arange(n) + 5 < n)
                    # 3. 在窗口内（按 trade_date）再筛选
                    valid_idx = np.where(valid_pos)[0]
                    if valid_idx.size > 0:
                        td_arr = pd.to_datetime(df["trade_date"]).dt.normalize().to_numpy()
                        win_mask = (td_arr >= np.datetime64(win.start.normalize())) & \
                                   (td_arr <= np.datetime64(win.end.normalize()))
                        inside_idx = valid_idx[win_mask[valid_idx]]
                        if inside_idx.size > 0:
                            close_arr = pd.to_numeric(df["close"], errors="coerce").to_numpy()
                            c0 = close_arr[inside_idx]
                            c5 = close_arr[inside_idx + 5]
                            ok = (c0 > 0) & np.isfinite(c5)
                            if np.any(ok):
                                nm_streak_returns.extend(((c5[ok] - c0[ok]) / c0[ok] * 100.0).tolist())

        ne = len(eligible)
        nh = len(hit_stocks)
        hit_rate_pct = (nh / ne * 100.0) if ne > 0 else 0.0
        avg_hold = float(np.mean(hold_days_samples)) if hold_days_samples else 0.0
        avg_nm_ret = float(np.mean(nm_streak_returns)) if nm_streak_returns else 0.0
        fwd_arr = np.array(first_hit_forward5d_returns, dtype=float)
        fwd_net_arr = np.array(first_hit_forward5d_net, dtype=float)
        mean_fwd5 = float(np.mean(fwd_arr)) if fwd_arr.size > 0 else 0.0
        mdd_simple = float(calculate_mdd(fwd_arr)) if fwd_arr.size > 0 else 0.0

        # 【P1 高优先级】基准对比：避免「牛市赚钱熊市亏大钱」的错觉
        benchmark_summary: Dict[str, Any] = {}
        try:
            from core.benchmark_compare import BenchmarkComparator
            period_label = f"{win.key}_马象监测带_{CIRC_MV_WAN_MIN:.0f}_{CIRC_MV_WAN_MAX:.0f}"
            cmp = BenchmarkComparator(
                start_date=win.start.strftime("%Y%m%d"),
                end_date=win.end.strftime("%Y%m%d"),
            )
            # 拉取失败不阻塞主流程
            fetch_results = cmp.fetch_all()
            benchmark_result = cmp.compare(
                strategy_returns_pct=fwd_arr.tolist(),
                strategy_label=f"痛点专项_{period_label}",
            )
            benchmark_summary = benchmark_result.to_dict()
            benchmark_summary["fetch_ok"] = fetch_results
            logging.info(
                "[backtest_runner] 基准对比完成: 策略 %.2f%% vs 沪深300 %.2f%% (α=%.2f%%)",
                benchmark_result.strategy_total_return_pct,
                benchmark_result.benchmarks.get("000300.SH", {}).get("total_return_pct", 0.0),
                benchmark_result.benchmarks.get("000300.SH", {}).get("alpha_pct", 0.0),
            )
        except Exception as e:
            logging.debug("[backtest_runner] 基准对比失败（不影响主流程）: %s", e)
            benchmark_summary = {"error": str(e)}

        return {
            "period_key": win.key,
            "start": str(win.start.date()),
            "end": str(win.end.date()),
            "legacy_mode": bool(legacy_mode),
            "circ_mv_band_wan_min": CIRC_MV_WAN_MIN,
            "circ_mv_band_wan_max": CIRC_MV_WAN_MAX,
            "eligible_universe_n": ne,
            "hit_stocks_n": nh,
            "hit_rate_pct": round(hit_rate_pct, 4),
            "avg_hold_days_effective": round(avg_hold, 4),
            "danger_sell_triggers_total": int(danger_sell_count),
            "net_main_streak_gt8_sample_n": int(len(nm_streak_returns)),
            "net_main_streak_gt8_avg_return_pct": round(avg_nm_ret, 4),
            "processed_codes": processed,
            "vol_col_used": vol_col_guess,
            "hold_samples_n": int(len(hold_days_samples)),
            "KPI_1_100_500亿命中率_pct": round(hit_rate_pct, 4),
            "KPI_1_平均持仓天数_计划值": round(avg_hold, 4),
            "KPI_2_danger_sell_触发总次数": int(danger_sell_count),
            "KPI_3_主力连红大于8日_T5平均收益_pct": round(avg_nm_ret, 4),
            "approx_mean_forward5d_return_pct": round(mean_fwd5, 4),
            "approx_mdd_pct_on_forward5d_samples": round(mdd_simple, 4),
            # Tier2 · 真实成交口径（旧字段一律保留，这里只并列新增）
            "approx_mean_forward5d_net_return_pct": (
                round(float(np.mean(fwd_net_arr)), 4) if fwd_net_arr.size else 0.0
            ),
            "forward5d_net_win_rate_pct": (
                round(float(np.mean(fwd_net_arr > 0) * 100.0), 2) if fwd_net_arr.size else 0.0
            ),
            "forward5d_net_samples_n": int(fwd_net_arr.size),
            "forward5d_net_fill_rate_pct": (
                round(fwd_net_arr.size / max(1, fwd_arr.size) * 100.0, 2) if fwd_arr.size else 0.0
            ),
            "risk_real_forward5d": calc_risk_metrics(fwd_net_arr, hold_days=5.0),
            "forward5d_samples_n": int(fwd_arr.size),
            # 【P1 高优先级】多基准对比输出
            "benchmark_compare": benchmark_summary,
        }
    finally:
        reset_backtest_context()


def _parse_bool_cli(s: str) -> bool:
    sl = (s or "").strip().lower()
    if sl in ("1", "true", "yes", "y", "on"):
        return True
    if sl in ("0", "false", "no", "n", "off"):
        return False
    return bool(s)


def main(argv: Optional[List[str]] = None) -> int:
    """
    命令行入口。

    交钥匙示例（在项目根目录执行）：

    python -m core.backtest_runner --mode=painpoint --legacy_mode=false --period=tech_rally

    python -m core.backtest_runner --mode=painpoint --legacy_mode=true --period=20241008_20241120 --max_codes=200

    python -m core.backtest_runner --mode=painpoint --legacy_mode=False --period=bluechip_accel
    """
    parser = argparse.ArgumentParser(
        description="小杰AI选股系统 Pro V26.88 回测引擎 CLI（痛点专项已实现）"
    )
    parser.add_argument(
        "--mode",
        type=str,
        default="painpoint",
        help="painpoint=痛点专项；其它模式请用 Python API",
    )
    parser.add_argument(
        "--legacy_mode",
        type=str,
        default="false",
        help="true/false：是否启用 legacy 风控/筛选分支（AB 对比）",
    )
    parser.add_argument(
        "--period",
        type=str,
        default="tech_rally",
        help="tech_rally | bluechip_accel | YYYYMMDD_YYYYMMDD",
    )
    parser.add_argument(
        "--max_codes",
        type=int,
        default=None,
        help="最多遍历股票数（省略则全市场，可能较慢）",
    )
    args = parser.parse_args(argv)

    if args.mode.strip().lower() == "painpoint":
        out = run_painpoint_backtest(
            period=args.period,
            legacy_mode=_parse_bool_cli(args.legacy_mode),
            max_codes=args.max_codes,
        )
        print("--- 痛点专项回测报告 ---")
        for k in sorted(out.keys()):
            print(f"{k}: {out[k]}")
        return 0 if "error" not in out else 1

    print("提示：当前 CLI 仅实现 --mode=painpoint。单股/批量请 from core.backtest_runner import run_strategy_backtest, run_batch_backtest")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
