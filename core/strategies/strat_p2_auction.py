# -*- coding: utf-8 -*-
"""
小杰AI选股系统 Pro V26.88 - P2 竞价战法池（物理胸甲四策略版）
【架构说明】
1. 核心筛选逻辑已拆至 p2_auction_screener.py：全局一票否决 + 四大独立竞价策略（标签化）。
2. 本类负责：调用筛选器、将命中结果映射为 burst_score / surge_bonus / penalty，供 scan_engine 排序与展示。
3. 不再在引擎内重复实现 strict_golden_burst_ok；黄金门禁由 scan_engine 统一处理，
   对「四大主策略命中」的股票可通过 hit_res['p2_core_screener_pass'] 放宽（避免主线命中被误杀）。
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import pandas as pd

try:
    from core.strategies.fund_mv_utils import mean_effective_turnover_f_last_n
except ImportError:
    from strategies.fund_mv_utils import mean_effective_turnover_f_last_n  # type: ignore

from core.config_manager import get_p2_screener_config, get_risk_control_config
from core.strategies.p2_auction_screener import (
    evaluate_p2_screener,
    _board_beta,
    _p2_collect_t1_memory,
    _p2_t1_memory_score,
)
from core.strategies.risk_control_engine import (
    DEFAULT_RISK_CONFIG,
    RiskControlConfig,
    evaluate_3layer_risk,
    evaluate_layer2_right_side_only,
    hits_indicate_right_side_attack,
    merge_risk_into_engine_result,
)

logger = logging.getLogger(__name__)


class P2Auction:
    def __init__(self, screener_cfg=None, risk_cfg: Optional[RiskControlConfig] = None):
        """
        screener_cfg:
            可选 P2ScreenerConfig 实例；不传则从 config.yaml strategies.p2（及策略实验室覆写）加载。
            便于在回测脚本中注入不同阈值而不改全局常量。
        risk_cfg: 三层全局风控；不传则从 config.yaml 的 ``risk_control`` 节点加载（见 get_risk_control_config）。
        """
        self.name = "P2竞价引擎"
        self.version = "V26.88_PhysicalArmor_4Strategies"
        self._cfg_lock_external = screener_cfg is not None
        self._cfg = screener_cfg if screener_cfg is not None else get_p2_screener_config()
        # 外部显式传入 risk_cfg 时不再被 YAML 覆盖（便于回测注入）
        self._risk_cfg_lock_external = risk_cfg is not None
        if risk_cfg is not None:
            self._risk_cfg = risk_cfg
        else:
            try:
                self._risk_cfg = get_risk_control_config()
            except Exception as ex:
                logger.debug("风控配置从 config.yaml 加载失败，使用 DEFAULT_RISK_CONFIG: %s", ex)
                self._risk_cfg = DEFAULT_RISK_CONFIG

    def _map_vr_np(self, vr):
        import numpy as np
        xp = [0.0, 0.8, 1.2, 2.0, 4.0, 8.0]
        yp = [0.0, 20.0, 50.0, 80.0, 95.0, 100.0]
        return float(np.interp(float(vr), xp, yp))

    def _map_funds(self, amount, circ_mv_yi):
        """绝对金额 → 相对流通市值（万分之 X）再映射到 0~100 分。"""
        import numpy as np
        if circ_mv_yi <= 0:
            return 0.0
        inflow_ratio = (amount / (circ_mv_yi * 100000000.0)) * 10000.0
        xp = [0.0, 1.0, 2.5, 5.0, 8.0, 15.0]
        yp = [0.0, 30.0, 60.0, 85.0, 100.0, 100.0]
        return float(np.interp(inflow_ratio, xp, yp))

    def _map_cyq(self, cyq):
        """筹码集中度：越低越好。"""
        import numpy as np
        xp = [5.0, 10.0, 15.0, 20.0, 25.0, 30.0]
        yp = [100.0, 100.0, 70.0, 40.0, 20.0, 0.0]
        return float(np.interp(cyq, xp, yp))

    def _check_second_surge(self, df, rt):
        """二波/再启动加分项（与四策略正交，仅作小幅 surge_bonus）。"""
        try:
            import numpy as np
            if df is None or len(df) < 60:
                return 0.0
            curr = df.iloc[-1]
            if float(curr.get("max_60d_pct", 0.0)) < 15.0:
                return 0.0
            recent_20_high = df["high"].tail(21).max()
            now_px = float(rt.get("price", 0.0))
            if now_px < recent_20_high * 0.96:
                return 0.0
            ma20 = float(curr.get("ma20", 0.0) or 0.0)
            if ma20 > 0 and now_px < ma20:
                return 0.0
            turnover_shrink = mean_effective_turnover_f_last_n(df, 3) < 3.0 if len(df) >= 3 else False
            # 【V30.10 统一 MACD 口径 2026-10-21】原用 macd_diff / macd_dea 判定，
            # 但这两列在生产 daily_data 里【根本不存在】（只有 macd/macd_signal/macd_hist），
            # 取不到 → 两侧恒 0.0 → 0.0 > 0.0 恒 False → 本加分项【永远不成立】。
            # 现改用 core.strategies.macd_unified 统一判定（自动回退到 macd_hist/macd_signal）。
            from core.strategies.macd_unified import is_macd_bull
            _bull = is_macd_bull(curr)
            macd_strong = bool(_bull) if _bull is not None else False
            cyq_stable = float(rt.get("cyq_concentration", 99.0)) < 18.0
            matches = sum([turnover_shrink, macd_strong, cyq_stable])
            if matches == 3:
                return 8.0
            if matches == 2:
                return 5.0
            if matches == 1:
                return 2.0
            return 0.0
        except Exception as e:
            logging.debug(f"P2 _check_second_surge 异常: {e}")
            return 0.0

    def _build_why_selected(self, hits: list, open_pct: float, vr: float, sector_rank: int, mainline_score: float, limit_times: float, board_beta: float, limit_up_signal: dict = None) -> str:
        """
        【V26.88 P2 优化】生成小白友好的「为什么入选」解释。

        Args:
            limit_up_signal: rt.get('_pre_limit_up')，涨停池信号 dict，含 continue_day_cnt/seal_money
        """
        reasons = []
        if hits:
            for h in hits:
                # 涨停龙头标签（优先级最高）
                if "龙头" in h or "连板" in h:
                    try:
                        cnt = int(limit_up_signal.get("continue_day_cnt", 1) or 1) if limit_up_signal else 1
                    except (TypeError, ValueError):
                        cnt = 1
                    try:
                        seal_yi = float(limit_up_signal.get("seal_money", 0) or 0) / 1e8 if limit_up_signal else 0.0
                    except (TypeError, ValueError):
                        seal_yi = 0.0
                    seal_label = f"封单{seal_yi:.1f}亿" if seal_yi > 0 else "封单待定"
                    reasons.append(f"龙头连板{cnt}板，竞价高开{open_pct:.2f}%，{seal_label}")
                elif "主升浪确认" in h:
                    reasons.append("昨日放量涨停后今日竞价继续强势")
                elif "机构连贯发力" in h:
                    reasons.append("机构资金连续多日净买入")
                elif "无尽苍穹" in h:
                    reasons.append("筹码高度集中，主力已控盘")
                elif "底仓重金点火" in h:
                    reasons.append("大资金昨日大笔买入")
        if not reasons:
            return "满足竞价选股条件"
        return "、".join(reasons)

    def _build_risk_note(self, open_pct: float, vr: float, limit_times: float, list_days: int, net_main_amount: float, req_main: float) -> str:
        """
        【V26.88 P2 优化】生成小白友好的「风险提示」。
        """
        risks = []
        if open_pct > 5.0:
            risks.append("高开过大可能面临获利盘兑现")
        if limit_times >= 2:
            risks.append("连板股高开容易炸板")
        if list_days < 20:
            risks.append("次新股波动剧烈")
        if net_main_amount < -req_main * 0.5:
            risks.append("主力资金呈流出状态")
        if vr > 5.0:
            risks.append("量比异常放大，需确认是否为对倒")
        if not risks:
            return "暂无明显风险提示"
        return "；".join(risks)

    def _build_suggestion(self, open_pct: float, vr: float, mainline_score: float, sector_rank: int) -> str:
        """
        【V26.88 P2 优化】生成小白友好的「操作建议」。
        """
        if open_pct > 5.0:
            return "建议观望或≤5%仓位，等回踩确认"
        if 2.0 <= open_pct <= 5.0:
            return "等9:40-10:00观察15分钟，确认承接再介入"
        if 0.5 <= open_pct < 2.0:
            if mainline_score >= 3.0 and sector_rank <= 2:
                return "主线龙头可15%-20%仓位，回踩不破开盘价+0.5%加仓"
            return "可10%-15%仓位试仓，止损设昨收-2.5%"
        return "等盘中放量确认方向后再操作"

    def run_all(self, df, rt):
        """
        入口：与 scan_engine 约定一致，返回 dict。
        增量约定：df 为截至 T-1 的历史；rt 为当日竞价快照（open、pre_close、vol_ratio 等）。
        """
        if not self._cfg_lock_external:
            self._cfg = get_p2_screener_config()
        # 每轮扫描刷新风控开关（mtime 变化时 get_risk_control_config 内 _load_yaml_raw 会重载）
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
            "p2_core_screener_pass": False,
            "p2_veto_reason": "",
            "p2_strategy_checks": {},
            "risk_tags": [],
            "suggested_min_entry_score": 0.0,
            "buy_hint": "",
            "wechat_hint": "",
            "risk_control": {},
        }
        # V26.88+9-22 优化4：rt["_pre_limit_up"] 在 scan_engine 预加载（避免逐股调用 HiThink）。
        # run_all 内不做兜底加载，涨停信号已在 _build_why_selected 中安全读取。
        # 兼容旧调用（rt 中无此字段时 limit_up_signal=None，不影响逻辑）。

        if df is None or df.empty or len(df) < 20:
            return res

        try:
            open_price = float(rt.get("open", 0.0))
            y_last = df.iloc[-1]
            # 竞价口径：昨收优先 rt，缺失则用 T-1 日线 pre_close/close，杜绝默认 1.0 造成虚假高开幅度
            pre_close = float(rt.get("pre_close", 0.0) or 0.0)
            if pre_close <= 0:
                pre_close = float(y_last.get("pre_close", 0.0) or 0.0)
            if pre_close <= 0:
                pre_close = float(y_last.get("close", 0.0) or 0.0)
            if pre_close <= 0 or open_price <= 0:
                return res

            open_pct = (open_price - pre_close) / pre_close * 100.0
            vr = float(rt.get("vol_ratio", 0.0))

            circ_mv_raw = rt.get("circ_mv")
            if pd.isna(circ_mv_raw) or circ_mv_raw is None:
                circ_mv_raw = float(df["circ_mv"].iloc[-1]) if "circ_mv" in df.columns else float(rt.get("total_mv", 10000000)) * 0.6
            circ_mv_yi = float(circ_mv_raw) / 10000.0

            rt_risk = dict(rt)
            rt_risk["price"] = float(open_price)
            rt_risk.setdefault("high", float(open_price))
            rt_risk.setdefault("low", float(open_price))
            rt_risk["_pool_key"] = "p2"
            _bj = timezone(timedelta(hours=8))
            _now = datetime.now(_bj)
            rt_risk.setdefault("curr_min", _now.hour * 60 + _now.minute)
            risk_pre = evaluate_3layer_risk(df, rt_risk, self._risk_cfg, is_right_side_strategy=False, pool_key="p2")
            # 一层硬否决仅保留「K 线为空 / 价格无效」；其余在 ui_alert_only 下改为标签预警（见 risk_control_engine）
            if not risk_pre.get("pass_layer1", False):
                res["p2_veto_reason"] = str(risk_pre.get("veto_reason", "") or "")
                res["risk_control"] = {
                    "pass_layer1": False,
                    "pass_layer2": True,
                    "veto_reason": res["p2_veto_reason"],
                    "penalty": float(risk_pre.get("penalty", 0.0) or 0.0),
                    "risk_tags": list(risk_pre.get("risk_tags", []) or []),
                    "suggested_min_entry_score": float(risk_pre.get("suggested_min_entry_score", 0.0) or 0.0),
                    "ui_warnings": list(risk_pre.get("ui_warnings", []) or []),
                }
                res["risk_tags"] = list(risk_pre.get("risk_tags", []) or [])
                res["suggested_min_entry_score"] = float(risk_pre.get("suggested_min_entry_score", 0.0) or 0.0)
                return res

            penalty = 0.0
            # === 涨停股处理：连板≥3且高开过大容易炸板 ===
            limit_times = float(y_last.get("limit_times", 0.0) or 0.0)
            if limit_times >= 3 and open_pct > 5.0:
                penalty += 30.0
                res["risk_tags"].append("⚠️[高开过大]连板≥3且高开谨慎")
            elif limit_times >= 2 and open_pct > 6.0:
                penalty += 20.0
                res["risk_tags"].append("⚠️[高开过大]连板≥2且高开>6%")

            # 【V26.88 新增】昨日涨停股今日竞价细分处理
            # 昨日涨幅>=9.8%视为涨停股
            prev_pct = 0.0
            if "pct_chg" in y_last.index or "pct" in y_last.index:
                prev_pct = float(y_last.get("pct_chg", y_last.get("pct", 0.0) or 0.0))
            is_prev_limit_up = prev_pct >= 9.8

            if is_prev_limit_up:
                # 昨日涨停 → 今日竞价分析
                if open_pct > 5.0 and vr >= 3.0:
                    # 昨日涨停 → 今日高开爆量（最强，连板预期）
                    penalty -= 15.0  # 奖励
                    res["risk_tags"].append("🔥[连板预期]昨涨停今高开爆量，可适当提高仓位")
                elif open_pct > 3.0 and vr >= 2.0:
                    # 昨日涨停 → 今日高开放量（强势）
                    penalty -= 8.0
                    res["risk_tags"].append("✅[昨日涨停今强势]可积极参与")
                elif open_pct > 0 and vr >= 1.5:
                    # 昨日涨停 → 今日平高开温和放量（正常）
                    pass  # 不惩罚
                elif open_pct < 0 and open_pct >= -2.0:
                    # 昨日涨停 → 今日低开（谨慎，可能是T+1收割）
                    penalty += 10.0
                    res["risk_tags"].append("⚠️[T+1风险]昨涨停今低开，小心二次收割")
                elif open_pct < -2.0:
                    # 昨日涨停 → 今日大幅低开（危险，应否决或大幅降分）
                    penalty += 25.0
                    res["risk_tags"].append("🚨[危险]昨涨停今大幅低开，建议观望")

            # === 次新股过滤：上市10日内波动大 ===
            list_days = int(float(rt.get("list_days", y_last.get("list_days", 999)) or 999))
            if list_days < 10:
                penalty += 25.0
                res["risk_tags"].append("🐣[次新股]上市<10日波动大")
            elif list_days < 20:
                penalty += 15.0
                res["risk_tags"].append("🐥[次新股]上市<20日谨慎")

            # === 主力对倒检测：主力流入但高开过大可能出货 ===
            if circ_mv_yi >= 1000.0:
                req_main_for_check = 80000000.0
            elif circ_mv_yi >= 500.0:
                req_main_for_check = 40000000.0
            elif circ_mv_yi >= 100.0:
                req_main_for_check = 15000000.0
            else:
                req_main_for_check = 8000000.0
            net_main_chk = float(rt.get("net_main_amount", y_last.get("net_main_amount", 0.0) or 0.0))
            if net_main_chk > req_main_for_check * 2 and open_pct > 4.0:
                penalty += 20.0
                res["risk_tags"].append("🧨[对倒嫌疑]主力大流入但高开>4%")
            # 主力流出但却高开（异常）
            if net_main_chk < -req_main_for_check * 0.5 and open_pct > 1.5:
                penalty += 15.0
                res["risk_tags"].append("🧨[异常高开]主力净流出但股价高开")

            if circ_mv_yi >= 1000.0:
                req_main = 80000000.0
            elif circ_mv_yi >= 500.0:
                req_main = 40000000.0
            elif circ_mv_yi >= 100.0:
                req_main = 15000000.0
            else:
                req_main = 8000000.0

            # 【V26.88 P2 优化】注：penalty 在上文的"涨停/次新/对倒"分支前已初始化为 0.0，
            # 此处保留下方段落的"regime / t1"等变量初始化。
            regime_bucket = "neutral"
            board_beta = 1.0
            t1_avg = 0.0
            t1_win = 0.0
            t1_n = 0.0
            if vr < 1.0:
                import numpy as np
                xp_pen = [0.0, 0.5, 0.8, 1.0]
                yp_pen = [40.0, 20.0, 8.0, 0.0]
                penalty += float(np.interp(vr, xp_pen, yp_pen))

            if open_pct < -1.0:
                import numpy as np
                xp_pct = [-10.0, -5.0, -2.0, -1.0]
                yp_pct = [50.0, 30.0, 10.0, 0.0]
                penalty += float(np.interp(open_pct, xp_pct, yp_pct))

            curr = df.iloc[-1]
            ma20 = float(curr.get("ma20", open_price))
            bias20_open = (open_price - ma20) / ma20 * 100.0 if ma20 > 0 else 0.0
            if bias20_open > 10.0:
                penalty += 15.0

            regime_bucket = str(rt.get("_regime_state", rt.get("regime", "")) or "")
            board_beta = _board_beta(rt)
            t1_avg, t1_win, t1_n = _p2_collect_t1_memory(rt)
            t1_memory_score = _p2_t1_memory_score(t1_avg, t1_win, t1_n, self._cfg)
            if any(k in regime_bucket for k in ["主升", "趋势"]):
                penalty *= self._cfg.regime_strict_boost
            elif any(k in regime_bucket for k in ["退潮", "空头", "主跌"]):
                penalty *= self._cfg.regime_relaxed_boost
            if board_beta >= self._cfg.board_beta_core_min:
                penalty *= 0.90
            elif board_beta >= self._cfg.board_beta_hot_min:
                penalty *= 0.95
            elif board_beta <= self._cfg.board_beta_cold_max:
                penalty += 12.0

            if vr < 0.4 or open_pct < -6.0:
                return res

            vwap_px = 0.0
            if float(rt.get("amount", 0.0) or 0.0) > 0 and float(rt.get("volume", 0.0) or 0.0) > 0:
                tentative = float(rt.get("amount", 0.0) or 0.0) / max(float(rt.get("volume", 0.0) or 0.0), 1e-9)
                vwap_px = tentative / 100.0 if tentative > open_price * 20 else tentative
            else:
                vwap_px = float(curr.get("vwap", open_price))
            vwap_gap_pct = ((open_price - vwap_px) / vwap_px * 100.0) if vwap_px > 0 and open_price > 0 else 0.0
            # 【V26.88 P2 优化】竞价确认窗口：从9:30集合竞价结束到9:45之间，给小白更多观察时间
            # 9:30 = 570分钟, 9:45 = 585分钟（原9:33-9:35太严格）
            # 【V26.88+10-1 P5 新增】9:30 / 9:40 实测锚点（price_930/price_940）：优先用 HiThink 9:30 集合竞价结束快照判断开盘是否符合预期
            price_930 = float(rt.get("price_930", 0.0) or 0.0)
            price_940 = float(rt.get("price_940", 0.0) or 0.0)
            auction_confirmed = False
            if price_930 > 0 and pre_close > 0:
                # 9:30 价格高于昨收 → 开盘强势；与 vwap 无关更鲁棒
                auction_930_pct = (price_930 - pre_close) / pre_close * 100.0
                if auction_930_pct > 0.5 and auction_930_pct < 5.0:
                    auction_confirmed = True
                # 9:40 价格相对 9:30 走稳（不破 9:30 最低 + 站稳 5 分钟均线）
                if price_940 > 0 and price_930 > 0 and price_940 >= price_930 * 0.998:
                    auction_confirmed = True
            open_confirm_hit = 570 <= int(_now.hour * 60 + _now.minute) <= 585 and (
                auction_confirmed  # 【V26.88+10-1】HiThink 实测锚点优先
                or (vwap_px <= 0 or open_price >= vwap_px * (1.0 - self._cfg.open_confirm_vwap_eps))
            )
            res["auction_confirmed_by_anchor"] = bool(auction_confirmed)
            if vwap_gap_pct < -self._cfg.vwap_death_hard_pct:
                penalty += self._cfg.vwap_death_penalty
            elif vwap_gap_pct < self._cfg.vwap_death_gap_min_pct:
                penalty += 10.0
            if open_confirm_hit:
                penalty = max(0.0, penalty - self._cfg.open_confirm_bonus_hit)
            else:
                penalty += self._cfg.open_confirm_penalty_miss
            if board_beta <= self._cfg.board_beta_cold_max:
                penalty += 6.0
            if t1_n >= self._cfg.t1_memory_min_samples and t1_memory_score < 0:
                penalty += min(self._cfg.t1_memory_penalty_max, abs(t1_memory_score))

            cyq_conc = float(rt.get("cyq_concentration", 999.0))
            net_main_amount = float(rt.get("net_main_amount", y_last.get("net_main_amount", 0.0) or 0.0))

        except Exception as e:
            logging.debug(f"P2 特征提取异常: {e}")
            return res

        # ================= 物理胸甲：全局否决 + 四大策略 =================
        ev = evaluate_p2_screener(df, rt, self._cfg)
        res["p2_core_screener_pass"] = bool(ev.get("p2_core_screener_pass"))
        res["p2_veto_reason"] = str(ev.get("veto_reason", "") or "")
        res["p2_strategy_checks"] = dict(ev.get("strategy_checks") or {})

        # 【V26.80 统一 A 股特色评分】P2 竞价场景：集合竞价定级 + 涨停板状态 + 开盘异常
        try:
            from core.p2_p5_a_share_enhance import unified_a_share_score

            _a_share = unified_a_share_score(df, rt)
            if _a_share.get("is_p2_p5_veto"):
                res["p2_veto_reason"] = _a_share.get("veto_reason", "A 股特色高风险")
                res["risk_tags"].append(f"🚨[统一风控]{_a_share.get('veto_reason')}")
                return res

            # 把 A 股特色标签注入 risk_tags
            _a_share_tags = _a_share.get("all_tags", [])[:5]  # 最多 5 个标签
            for tag in _a_share_tags:
                if tag and tag not in res["risk_tags"]:
                    res["risk_tags"].append(tag)

            # A 股特色加权（默认 0.2 权重，避免喧宾夺主）
            _a_boost = float(_a_share.get("total_boost", 0.0)) * 0.2
            res["a_share_p2_boost"] = round(_a_boost, 2)
            res["a_share_p2_tags"] = _a_share_tags
        except Exception as _e_a2:
            logging.debug("V26.80 P2 统一 A 股特色评分跳过: %s", _e_a2)

            if not ev.get("veto_pass"):
                return res

        hits = list(ev.get("strategies") or [])
        if not hits:
            return res

        # 【V26.80 跨池信号接力】P1→P2 接力加分（同一票连续多池命中）
        try:
            from core.p2_p5_signal_relay import apply_relay_bonus_to_pool_res

            ts_code = str(rt.get("ts_code", rt.get("code", "")) or "")
            if ts_code:
                res = apply_relay_bonus_to_pool_res(res, ts_code, "p2", apply_to_burst=True)
        except Exception as _e_relay:
            logging.debug("V26.80 P2 跨池接力跳过: %s", _e_relay)

        if risk_pre is not None and hits_indicate_right_side_attack(hits):
            l2 = evaluate_layer2_right_side_only(df, rt_risk, self._risk_cfg)
            for uw in l2.get("ui_warnings") or []:
                res["risk_tags"].append("⚠️[二层预警]" + str(uw))
            if not l2.get("pass_layer2", True):
                res["p2_veto_reason"] = str(l2.get("veto_reason", "") or "")
                res["risk_control"] = {
                    "pass_layer1": True,
                    "pass_layer2": False,
                    "veto_reason": res["p2_veto_reason"],
                    "penalty": float(risk_pre.get("penalty", 0.0) or 0.0),
                    "risk_tags": list(risk_pre.get("risk_tags", []) or []),
                    "suggested_min_entry_score": float(risk_pre.get("suggested_min_entry_score", 0.0) or 0.0),
                    "ui_warnings": list(risk_pre.get("ui_warnings", []) or []),
                }
                res["risk_tags"] = list(risk_pre.get("risk_tags", []) or [])
                res["suggested_min_entry_score"] = float(risk_pre.get("suggested_min_entry_score", 0.0) or 0.0)
                return res

        try:
            s_vol = self._map_vr_np(vr)
            s_fund = self._map_funds(net_main_amount, circ_mv_yi)
            s_chip = self._map_cyq(cyq_conc)
            raw_burst = (s_vol * 0.3) + (s_fund * 0.3) + (s_chip * 0.2) + 10.0
            bonus = float(ev.get("sort_weight_bonus", 1.0) or 1.0)
            if bonus > 1.0:
                raw_burst = min(raw_burst + (bonus - 1.0) * 25.0, 100.0)
            if t1_memory_score != 0.0:
                raw_burst = max(0.0, min(100.0, raw_burst + t1_memory_score))
            res["burst_score"] = round(min(raw_burst, 100.0), 2)
            res["surge_bonus"] = self._check_second_surge(df, rt)
            res["penalty"] = round(max(0.0, penalty), 2)
            sector_strength = float(ev.get("detail", {}).get("sector_strength", board_beta) or board_beta)
            sector_rank = int(ev.get("detail", {}).get("sector_rank", 999) or 999)
            mainline_score = float(ev.get("detail", {}).get("mainline_score", 0.0) or 0.0)
            entry_reason = "主线前排，承接稳"
            if sector_rank <= 2 and sector_strength >= 1.10:
                entry_reason = "主线前排，承接稳"
            elif sector_strength <= 0.95:
                entry_reason = "板弱观察，先别追"
            elif mainline_score <= -2.0:
                entry_reason = "不像主线，防假强"
            res["detail"] = {
                "爆量分": round(s_vol, 2),
                "抢筹分": round(s_fund, 2),
                "筹码分": round(s_chip, 2),
                "入池理由": entry_reason,
                "regime_bucket": regime_bucket,
                "board_beta": round(board_beta, 3),
                "sector_strength": round(sector_strength, 3),
                "sector_rank": sector_rank,
                "sector_total": int(ev.get("detail", {}).get("sector_total", 0) or 0),
                "mainline_score": round(float(ev.get("detail", {}).get("mainline_score", 0.0) or 0.0), 2),
                "mainline_reason": str(ev.get("detail", {}).get("mainline_reason", "") or ""),
                "t1_avg_ret_pct": round(t1_avg, 3),
                "t1_win_rate_pct": round(t1_win, 1),
                "t1_sample_n": int(t1_n),
                "t1_memory_score": round(t1_memory_score, 2),
                "open_confirm_hit": bool(open_confirm_hit),
                "vwap_gap_pct": round(vwap_gap_pct, 3),
                "sort_weight_bonus": bonus,
                "p2_screener": ev.get("detail", {}),
                # 【V26.88 P2 优化】小白友好详细解释
                "explain": {
                    "why_selected": self._build_why_selected(hits, open_pct, vr, sector_rank, mainline_score, limit_times, board_beta, limit_up_signal=rt.get("_pre_limit_up") or {}),
                    "risk_note": self._build_risk_note(open_pct, vr, limit_times, list_days, net_main_amount, req_main_for_check),
                    "suggestion": self._build_suggestion(open_pct, vr, mainline_score, sector_rank),
                },
            }
            # 【V26.88 P2 优化】小白友好版买入提示：根据高开幅度细分指导
            # 【V26.88+9-22 优化28+21+27】A 股实战止损+赔率+EV 自动评估
            try:
                from core.stop_loss_utils import (
                    evaluate_entry_quality, compute_expected_value,
                )
                # 根据主流策略推断 strategy_type
                strat_type = "leader" if mainline_score >= 3.0 else "right_side"
                _eq = evaluate_entry_quality(df, rt, risk_reward_min=2.0, strategy_type=strat_type)
                _ev = compute_expected_value(
                    risk_reward=_eq["risk_reward_ratio"],
                    signal_strength=float(res.get("burst_score", 60.0) or 60.0),
                    penalty=float(res.get("penalty", 0.0) or 0.0),
                )
                res["entry_quality"] = _eq
                res["expected_value"] = _ev
                res["auto_stop_loss"] = _eq["stop_loss"]
                res["auto_take_profit"] = _eq["take_profit"]
                res["trailing_stop_rules"] = _eq["trailing_stop_rules"]
                res["trade_grade"] = _ev["trade_grade"]
                # P2 赔率硬卡：达不到 2:1 → 严重降权
                if not _eq["pass_2_1"] and _eq["entry_quality_score"] < 50:
                    res["risk_tags"].append(
                        f"📉[入场质量低]盈亏比{_eq['risk_reward_ratio']:.1f}, 评级{_ev['trade_grade']}"
                    )
            except Exception as _e_aq2:
                logger.debug("[P2] 入场质量评估跳过: %s", _e_aq2)

            buy_hint = ""
            if hits:
                # 高开过大预警（最重要！）
                if open_pct > 5.0:
                    buy_hint = "⚠️高开过大(>5%)，建议放弃或极轻仓(≤5%)观察，等回踩确认再操作"
                elif 3.0 <= open_pct <= 5.0:
                    if vr >= 2.5:
                        buy_hint = "🔥高开爆量强势，可轻仓(10%-15%)跟进，止损设开盘价-2%"
                    else:
                        buy_hint = "⚠️高开3-5%但量比不足，谨慎观望，等9:40后回踩不破再考虑"
                elif 1.5 <= open_pct < 3.0:
                    if open_confirm_hit and vr >= 1.5:
                        buy_hint = "✅平高开+竞价确认，可分批介入(15%-20%)，回踩开盘价+0.5%不破可加仓"
                    else:
                        buy_hint = "平高开1.5-3%，等9:45前确认分时站稳VWAP后再轻仓(10%-15%)"
                elif 0.5 <= open_pct < 1.5:
                    if vr >= 1.8 and board_beta >= 1.1:
                        buy_hint = "✅温和高开+板块强势，可15%-20%仓位，回踩不破开盘价加仓"
                    else:
                        buy_hint = "平高开，竞价量比一般，建议10%仓位试仓，止损设昨收-2%"
                else:
                    buy_hint = "接近平开，等盘中放量确认方向后再操作"
            else:
                buy_hint = "信号偏弱，建议等盘中放量站稳VWAP再考虑介入"
            
            # 主线/板块叠加提示
            if mainline_score >= 3.0 and hits:
                if sector_rank <= 2:
                    buy_hint = "🐉主线龙头+板块前排，最强组合！可20%-25%仓位，回踩VWAP确认后加仓，止损昨收-3%"
                else:
                    buy_hint = "🔥主线板块偏强，可15%-20%仓位，回踩分时均线确认再加"
            elif mainline_score <= -2.0:
                buy_hint = "⚠️疑似假强板块，建议观望不追高，等板块回到主线再考虑"
            elif sector_strength <= 0.95:
                buy_hint = "⚠️板块偏弱，先观察不追高，等分时站稳VWAP和板块转强再考虑"
            
            # 涨停连板特殊提示
            if limit_times >= 2 and open_pct > 2.0:
                buy_hint = "⚠️连板股高开！连板≥2日高开易炸板，建议≤10%仓位或观望"
            if list_days < 20:
                buy_hint = "⚠️次新股波动大，建议≤10%仓位操作"
            res["buy_hint"] = buy_hint
            res["wechat_hint"] = buy_hint
            res["strategies"] = hits
            if risk_pre is not None:
                # 【V34 修复 · P0】merge 内部返回新字典，原式丢弃返回值 → 风控扣分/标签全部落空。
                # deduct_burst_score=False：pipeline 已按 res["penalty"] 扣一次，避免双重扣分。
                res = merge_risk_into_engine_result(
                    res, risk_pre, penalty_key="penalty", deduct_burst_score=False,
                )
        except Exception as e:
            # 【V26.88 P2 安全】结算异常时不再伪装 burst_score=72.0（之前会让"策略崩溃的票"
            #  仍然以"中等分"漏过风控否决），而是把 burst_score 设为 0.0、记录
            #  p2_veto_reason，让上层明确知道"该票未通过 P2 评分"。
            logging.exception("P2 打分结算异常: %s", e)
            res["strategies"] = hits
            res["burst_score"] = 0.0
            res["p2_veto_reason"] = (
                f"打分结算异常:{type(e).__name__}:{str(e)[:120]}"
            )
            if risk_pre is not None:
                merge_risk_into_engine_result(res, risk_pre, penalty_key="penalty")

        # === V28 真实接入 signal_log ===
        try:
            from core.pool_signal_adapter_v28 import append_signal_log_safe
            # 【V30.10 修复 2026-10-21】_GLOBAL_DB_CONN 是全项目从未赋值的死变量，
            # 恒为 None → P2 信号从不落库。改为 None 传入，append_signal_log_safe
            # 内部会用 resolve_write_conn() 自动解析可用连接。
            db_conn = None
            try:
                from core.push_template_momentum_reversal import apply_push_template_to_res
                apply_push_template_to_res(res)
            except Exception:
                pass
            try:
                from core.push_dedup import pool_apply_push_dedup
                pool_apply_push_dedup(res, "p2")
            except Exception:
                pass
            # 【V30.10 阈值统一 2026-10-21】push_min_score 改为从 config.yaml 动态解析
            # （core/push_threshold.resolve_push_min_score），不再硬编码。
            # 落库用 for_log=True（可配 <pool>_log 单独调低以积累统计样本）。
            from core.push_threshold import resolve_push_min_score
            # 【Tier1 · 1.1 修复 2026-11-02】原调用漏传 rt → append_signal_log_safe 取不到
            # 价格（price<=0 直接 return），P2 信号事实上从不落库（signal_log p2=0）。
            # 与 P3 的既有正确写法对齐：把 rt 显式传入。不改任何打分/阈值。
            append_signal_log_safe(res, "p2", db_conn,
                                    push_min_score=resolve_push_min_score("p2", for_log=True),
                                    rt=rt)
        except Exception:
            pass

        return res
