# -*- coding: utf-8 -*-
"""
小杰AI选股系统 Pro V26.88 - 技术指标预处理模块
【核心原则】
1. 全量计算日线技术指标；与 data_fetcher 55 维字段可并存（本模块侧重 scan 侧单股 DataFrame）。
2. df + rt 对齐：merge_daily_with_realtime 将盘口并入最后一根或追加当日行，再 precompute 可得到与现价一致的 close 与量能；
   盘中合并时 **high/low 不采用 rt 全日最高最低**，以免 KDJ/布林/ATR 等 rolling 指标在收盘前「偷看」当日振幅（维度1 数据口径）。
3. Pandas 规范：ffill() 替代已弃用的 fillna(method='ffill')。
"""
from datetime import datetime, timezone, timedelta

import pandas as pd
import numpy as np
import logging


def _sf(val, default=0.0):
    """指标模块内部安全浮点，避免 None/NaN 参与 rolling。"""
    if val is None:
        return default
    try:
        if isinstance(val, (float, np.floating)) and pd.isna(val):
            return default
        s = str(val).strip()
        if s in ("", "-", "nan", "None"):
            return default
        return float(val)
    except (TypeError, ValueError):
        return default


def _intraday_ohlc_high_low(price: float, pre_close: float, open_px: float) -> tuple:
    """
    仅用现价、昨收、开盘构造当日 high/low（不含行情源 rt 的 high/low）。
    保证 precompute 中依赖 high/low 的指标在盘中不引入「已知全日振幅」的泄漏口径。
    """
    o = float(open_px) if open_px > 0 else float(price)
    pc = float(pre_close) if pre_close > 0 else float(price)
    p = float(price)
    hi = max(p, o, pc)
    lo = min(p, o, pc)
    return hi, lo


def merge_daily_with_realtime(df, rt):
    """
    将实时/快照 rt 并入历史 df，消除「最后一根仍是昨收、指标与现价脱节」的断层。

    规则（增量、非全量重下历史）：
    - 若最后一根 trade_date 与北京时间「当日」为同一自然日：更新 close/open、量额；high/low 按现价/昨收/开盘链式构造（不写入 rt 高低）。
    - 若最后一根早于当日、且为工作日、且当前已过 09:25（分钟>=565）：在表末追加一行当日 OHLCV，pre_close 取前一日收盘。
    - 周末不追加新行（避免伪造非交易日 K 线）。

    返回:
        (df_merged, did_change: bool) — did_change 为 True 时建议调用方执行 precompute_indicators 重算尾部指标。
    """
    if df is None or df.empty or not isinstance(rt, dict):
        return df, False

    price = _sf(rt.get("price"), 0.0)
    if price <= 0:
        price = _sf(rt.get("close"), 0.0)
    if price <= 0:
        return df, False

    if "trade_date" not in df.columns:
        logging.debug("merge_daily_with_realtime: 缺少 trade_date 列，跳过合并")
        return df, False

    bj = timezone(timedelta(hours=8))
    now = datetime.now(bj)
    today_d = now.date()
    curr_min = now.hour * 60 + now.minute
    weekday_ok = today_d.weekday() < 5

    out = df.copy()
    out["trade_date"] = pd.to_datetime(out["trade_date"], errors="coerce")
    if out["trade_date"].isna().all():
        return df, False

    last_td = out["trade_date"].iloc[-1]
    if pd.isna(last_td):
        return df, False
    last_d = last_td.normalize().date()

    vol_col = "vol" if "vol" in out.columns else ("volume" if "volume" in out.columns else None)
    vol_shares = _sf(rt.get("volume"), 0.0)
    # 【审计修复】维度2：手数换算分母兜底，避免异常 volume 导致除零
    vol_hand = (vol_shares / max(100.0, 1e-9)) if vol_shares > 0 else 0.0

    rt_open = _sf(rt.get("open"), price)
    last_close = _sf(out.iloc[-1].get("close"), price)
    pre_close_rt = _sf(rt.get("pre_close"), 0.0)

    def _patch_last_row():
        nonlocal out
        idx = len(out) - 1
        ix = out.index[idx]
        out.loc[ix, "close"] = price
        pc_row = pre_close_rt if pre_close_rt > 0 else _sf(out.iloc[idx].get("pre_close"), last_close)
        open_eff = rt_open if rt_open > 0 else price
        # 【审计修复】维度1：盘中合并禁止把 rt 的 high/low 写入 K 线，防止 rolling 指标泄漏当日振幅
        hi_bar, lo_bar = _intraday_ohlc_high_low(price, pc_row, open_eff)
        if "high" in out.columns:
            out.loc[ix, "high"] = hi_bar
        if "low" in out.columns:
            out.loc[ix, "low"] = lo_bar
        if "open" in out.columns and rt_open > 0:
            out.loc[ix, "open"] = rt_open
        if vol_col is not None and vol_hand > 0:
            out.loc[ix, vol_col] = vol_hand
        if "amount" in out.columns:
            amt = _sf(rt.get("amount"), 0.0)
            if amt > 0:
                out.loc[ix, "amount"] = amt / 10000.0
        pc = pre_close_rt if pre_close_rt > 0 else _sf(out.iloc[idx].get("pre_close"), last_close)
        if "pre_close" in out.columns and pc > 0:
            out.loc[ix, "pre_close"] = pc
        if "pct_chg" in out.columns and pc > 0:
            out.loc[ix, "pct_chg"] = (price - pc) / max(pc, 1e-9) * 100.0

    changed = False

    if last_d == today_d:
        _patch_last_row()
        changed = True
    elif weekday_ok and last_d < today_d and curr_min >= 565:
        prev_close = _sf(out.iloc[-1].get("close"), last_close)
        new_row = out.iloc[-1].copy()
        new_row["trade_date"] = pd.Timestamp(today_d)
        new_row["pre_close"] = pre_close_rt if pre_close_rt > 0 else prev_close
        new_row["open"] = rt_open if rt_open > 0 else price
        # 【审计修复】维度1：追加当日行时同样禁止 rt 全日 high/low，仅 close/pre_open/pre 链
        hi_new, lo_new = _intraday_ohlc_high_low(price, float(new_row.get("pre_close") or prev_close), float(new_row.get("open") or price))
        new_row["high"] = hi_new
        new_row["low"] = lo_new
        new_row["close"] = price
        if vol_col is not None:
            new_row[vol_col] = vol_hand if vol_hand > 0 else _sf(new_row.get(vol_col), 0.0)
        if "amount" in new_row.index and _sf(rt.get("amount"), 0) > 0:
            new_row["amount"] = _sf(rt.get("amount")) / 10000.0
        pc2 = float(new_row.get("pre_close") or prev_close)
        if "pct_chg" in new_row.index and pc2 > 0:
            new_row["pct_chg"] = (price - pc2) / max(pc2, 1e-9) * 100.0
        out = pd.concat([out, new_row.to_frame().T], ignore_index=True)
        changed = True

    if changed:
        out.reset_index(drop=True, inplace=True)
    return out, changed

def precompute_indicators(df):
    """
    全量计算单只股票的日线技术指标
    传入的 df 必须包含: open, high, low, close, pre_close, vol(或volume), amount
    """
    # 极度严苛的基础数据拦截，不满足最低计算周期的直接放行原数据
    if df is None or df.empty or len(df) < 5:
        return df

    try:
        # 深拷贝以防止 pandas 的 SettingWithCopyWarning
        df = df.copy()
        
        # 必须确保按时间升序排列，这是所有时间序列指标计算的绝对前提
        df.sort_values('trade_date', ascending=True, inplace=True)
        df.reset_index(drop=True, inplace=True)

        # 提取常用序列，提升后续计算性能
        close = df['close']
        high = df['high']
        low = df['low']
        open_price = df['open']
        
        # 统一成交量字段名称
        vol_col = 'vol' if 'vol' in df.columns else 'volume'
        volume = df[vol_col] if vol_col in df.columns else pd.Series(1, index=df.index)

        # =========================================================
        # 第一战区：基础均线系统 (MA 家族)
        # =========================================================
        df['ma5'] = close.rolling(window=5, min_periods=1).mean()
        df['ma10'] = close.rolling(window=10, min_periods=1).mean()
        df['ma20'] = close.rolling(window=20, min_periods=1).mean()
        df['ma30'] = close.rolling(window=30, min_periods=1).mean()
        df['ma60'] = close.rolling(window=60, min_periods=1).mean()
        df['ma120'] = close.rolling(window=120, min_periods=1).mean()
        df['ma250'] = close.rolling(window=250, min_periods=1).mean()

        # =========================================================
        # 第二战区：量能均线系统 (VMA 家族)
        # =========================================================
        if vol_col in df.columns:
            df['vma5'] = volume.rolling(window=5, min_periods=1).mean()
            df['vma10'] = volume.rolling(window=10, min_periods=1).mean()
            df['vma20'] = volume.rolling(window=20, min_periods=1).mean()
            df['vma60'] = volume.rolling(window=60, min_periods=1).mean()

        # =========================================================
        # 第三战区：MACD 动能指标 (12, 26, 9)
        # =========================================================
        exp1 = close.ewm(span=12, adjust=False).mean()
        exp2 = close.ewm(span=26, adjust=False).mean()
        df['macd_diff'] = exp1 - exp2
        df['macd_dea'] = df['macd_diff'].ewm(span=9, adjust=False).mean()
        df['macd_bar'] = (df['macd_diff'] - df['macd_dea']) * 2

        # =========================================================
        # 第四战区：BOLL 布林带系统 (20, 2)
        # =========================================================
        df['boll_mid'] = df['ma20']
        std_20 = close.rolling(window=20, min_periods=1).std()
        df['boll_upper'] = df['boll_mid'] + 2 * std_20
        df['boll_lower'] = df['boll_mid'] - 2 * std_20

        # =========================================================
        # 第五战区：RSI 相对强弱指标 (6, 12, 24)
        # =========================================================
        delta = close.diff()
        gain = delta.where(delta > 0, 0)
        loss = -delta.where(delta < 0, 0)
        
        for period in [6, 12, 24]:
            avg_gain = gain.rolling(window=period, min_periods=1).mean()
            avg_loss = loss.rolling(window=period, min_periods=1).mean()
            rs = avg_gain / avg_loss.replace(0, np.nan)
            df[f'rsi_{period}'] = 100 - (100 / (1 + rs))
            df[f'rsi_{period}'] = df[f'rsi_{period}'].fillna(50)

        # =========================================================
        # 第六战区：KDJ 随机指标 (9, 3, 3)
        # =========================================================
        low_list_9 = low.rolling(window=9, min_periods=1).min()
        high_list_9 = high.rolling(window=9, min_periods=1).max()
        rsv = (close - low_list_9) / (high_list_9 - low_list_9).replace(0, np.nan) * 100
        rsv = rsv.fillna(50)
        df['k'] = rsv.ewm(com=2, adjust=False).mean()
        df['d'] = df['k'].ewm(com=2, adjust=False).mean()
        df['j'] = 3 * df['k'] - 2 * df['d']

        # =========================================================
        # 第七战区：👑 核心海拔探测器与防山顶极值区 
        # =========================================================
        df['bias_5'] = (close - df['ma5']) / df['ma5'].replace(0, np.nan) * 100
        df['bias_20'] = (close - df['ma20']) / df['ma20'].replace(0, np.nan) * 100
        df['price_ma20_ratio'] = close / df['ma20'].replace(0, np.nan) 
        
        max_close_60 = close.rolling(window=60, min_periods=1).max()
        df['max_60d_pct'] = (max_close_60 / close.shift(1).replace(0, np.nan) - 1.0) * 100.0
        
        min_low_60 = low.rolling(window=60, min_periods=1).min()
        df['pct_from_60d_low'] = (close - min_low_60) / min_low_60.replace(0, np.nan) * 100.0

        max_high_60 = high.rolling(window=60, min_periods=1).max()
        df['dist_high_60'] = close / max_high_60.replace(0, np.nan)  
        
        max_high_120 = high.rolling(window=120, min_periods=1).max()
        df['dist_high_120'] = close / max_high_120.replace(0, np.nan) 

        ma20_shifted_5 = df['ma20'].shift(5)
        df['ma20_slope_5'] = (df['ma20'] - ma20_shifted_5) / ma20_shifted_5.replace(0, np.nan) * 100.0

        ma60_shifted_5 = df['ma60'].shift(5)
        df['ma60_slope_5'] = (df['ma60'] - ma60_shifted_5) / ma60_shifted_5.replace(0, np.nan) * 100.0
        
        df['ma_dispersion_60_120'] = (df['ma60'] - df['ma120']) / df['ma120'].replace(0, np.nan) * 100.0

        # =========================================================
        # 第八战区：辅助共振指标 (CCI, WR, VWAP)
        # =========================================================
        tp = (high + low + close) / 3
        ma_tp = tp.rolling(14, min_periods=1).mean()
        md = tp.rolling(14, min_periods=1).apply(lambda x: np.abs(x - x.mean()).mean(), raw=True)
        df['cci'] = (tp - ma_tp) / (0.015 * md).replace(0, np.nan)
        
        highest_high_14 = high.rolling(14, min_periods=1).max()
        lowest_low_14 = low.rolling(14, min_periods=1).min()
        df['wr'] = (highest_high_14 - close) / (highest_high_14 - lowest_low_14).replace(0, np.nan) * 100
        
        if 'amount' in df.columns and df['amount'].sum() > 0:
            # 【审计修复】维度2：VWAP 分母用 max(vol,1e-9)，避免静默 inf
            vol_pos = volume.clip(lower=1e-9)
            df['vwap'] = (df['amount'] * 10) / vol_pos
        else:
            df['vwap'] = (high + low + close) / 3.0

        # =========================================================
        # 第九战区：ATR 真实波幅 (性格探测器核心)
        # =========================================================
        if 'pre_close' not in df.columns:
            df['pre_close'] = close.shift(1).fillna(open_price)
            
        df['tr1'] = high - low
        df['tr2'] = (high - df['pre_close']).abs()
        df['tr3'] = (low - df['pre_close']).abs()
        
        df['tr'] = df[['tr1', 'tr2', 'tr3']].max(axis=1)
        df['atr20'] = df['tr'].rolling(window=20, min_periods=1).mean()
        df['atr'] = df['atr20']
        # close 为 0 或缺失时避免 inf，后续 replace/fillna 统一收口
        df['atr_pct'] = (df['atr20'] / close.replace(0, np.nan)) * 100.0
        
        df.drop(columns=['tr1', 'tr2', 'tr3', 'tr'], inplace=True, errors='ignore')

        # =========================================================
        # 第十战区B：🧩 历史列名兼容别名（保证 P2–P5 屏幕器/风控可读取）
        #   P5 与 P4 的硬编码字段（macd/macd_signal/macd_hist/high_20/rsi_14/vol_ma5/vol_ma10/vol_ma20）
        #   在历史上由 data_fetcher 单独追加；轻量入口 precompute_indicators 之前只产出
        #   vma*/macd_diff/macd_dea/macd_bar/rsi_6 等新名，会导致 P5 prepare_p5_lags 报 KeyError。
        #   这里统一补齐别名，避免在不同入口（直通车 vs 全量刷新）下行为不一致。
        # =========================================================
        if 'macd' not in df.columns and 'macd_diff' in df.columns:
            df['macd'] = df['macd_diff']
        if 'macd_signal' not in df.columns and 'macd_dea' in df.columns:
            df['macd_signal'] = df['macd_dea']
        if 'macd_hist' not in df.columns and 'macd_bar' in df.columns:
            df['macd_hist'] = df['macd_bar']
        # 【2026-10-15 审计修复 · 数据缺失/陈旧指标统一重算】
        # 根因：daily_data 自 2026-10-08 起 23 个指标列（ma5/ma10/ma20/ma60/ma120/
        #       high_20/low_60/vol_ma5/vol_ma10/vol_ma20/macd*/rsi_14/bias_20/
        #       ma20_slope_5/kdj_*/boll_*/cci/atr_pct）全部为 NULL，
        #       因为增量同步路径只写原始行情列（见 data/fetcher/_sync_entry.py:250-277）。
        #       原写法的 `if 'X' not in df.columns` 判「列存在」即跳过重算，
        #       NULL 随后被本函数末尾的 df.ffill() 填成更早交易日的陈旧常量，
        #       对外表现是「指标有值但其实是几天前的旧值」。
        # 直接后果：
        #   1) P4-02 / P4-05（旧P4-06 动能突破）的 `close > high_20` 恒假（两策略近 4 个交易日零命中）；
        #   2) P4-03（旧P4-04 均线缩量低吸）的 vol_ma5、P4-04/P4-05 的量能门槛、P5-09（旧P5-05 趋势确认）的 MACD 动能全部失真。
        # 证据：daily_data 2026-10-13 全市场 1493 行上述 23 列 NULL 率均为 100%；
        #       600519.SH 改前 precompute 后 high_20 末 5 行被填成同一个 1338.86（陈旧）。
        # 改法：把「列不存在」升级为「列不存在 或 末行为 NULL 或 整列非数值/全空」，
        #       只要最新交易日该指标没有真实落库，就一律按源列重算，
        #       杜绝 ffill 造成的「陈旧指标伪装成新值」。已重算的值不会再被 ffill 覆盖。
        def _needs_recompute(target: str) -> bool:
            if target not in df.columns:
                return True
            col = pd.to_numeric(df[target], errors='coerce')
            if len(col) == 0 or col.notna().sum() == 0:
                return True
            # 末行是 NULL = 最新交易日该指标未落库（增量同步漏写），必须重算
            return bool(pd.isna(col.iloc[-1]))

        if _needs_recompute('macd') and 'macd_diff' in df.columns:
            df['macd'] = df['macd_diff']
        if _needs_recompute('macd_signal') and 'macd_dea' in df.columns:
            df['macd_signal'] = df['macd_dea']
        if _needs_recompute('macd_hist') and 'macd_bar' in df.columns:
            df['macd_hist'] = df['macd_bar']
        if _needs_recompute('high_20'):
            df['high_20'] = high.rolling(window=20, min_periods=1).max()
        if _needs_recompute('low_60'):
            df['low_60'] = low.rolling(window=60, min_periods=1).min()
        if _needs_recompute('rsi_14'):
            delta14 = close.diff()
            gain14 = delta14.where(delta14 > 0, 0)
            loss14 = delta14.where(delta14 < 0, 0)
            avg_gain14 = gain14.rolling(window=14, min_periods=1).mean()
            avg_loss14 = loss14.rolling(window=14, min_periods=1).mean()
            rs14 = avg_gain14 / avg_loss14.replace(0, np.nan)
            df['rsi_14'] = (100 - (100 / (1 + rs14))).fillna(50.0)
        if vol_col in df.columns:
            if _needs_recompute('vol_ma5'):
                df['vol_ma5'] = volume.rolling(window=5, min_periods=1).mean()
            if _needs_recompute('vol_ma10'):
                df['vol_ma10'] = volume.rolling(window=10, min_periods=1).mean()
            if _needs_recompute('vol_ma20'):
                df['vol_ma20'] = volume.rolling(window=20, min_periods=1).mean()

        # =========================================================
        # 第十战区：数据后处理与异常值收尾 (🚀 修复警告)
        # =========================================================
        df.replace([np.inf, -np.inf], np.nan, inplace=True)
        
        # 🚀 痛点修复：彻底解决 FutureWarning，采用原生 ffill()
        df.ffill(inplace=True)
        
        # 最后一行 fillna(0) 是「工程兜底」：停牌前缘、除权缺口等会在 ffill 后仍缺首值，填 0 避免下游比较炸掉。
        # 代价是：个别列在「真缺失」时会被当成 0（例如某根 K 的换手），调用方若做严格风控应再按列做 isna 检查或跳过该票。
        df.fillna(0, inplace=True)

        return df

    except Exception as e:
        logging.error(f"指标计算发生致命异常: {e}")
        return df
