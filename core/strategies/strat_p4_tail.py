# -*- coding: utf-8 -*-
"""
小杰AI选股系统 Pro V33 - P4 尾盘战法引擎（尾盘执行时钟 + 防镰刀增强 + 2026慢牛优化）
【架构】
1. 硬阈值筛选在 p4_tail_screener.py；本类负责打分、风险标签、仓位与「14:30 后怎么下单」。
2. P5 盘后已独立为 strat_p5_postmarket.P5Postmarket；scan_engine 仅对 p4 调用本引擎。
3. 黄金门禁由 scan_engine 在 p4_core_screener_pass 为真时放宽。

【V33 全局优化 · 以「14:30 之后真能买到并赚到」为中心】
① 尾盘执行时钟（core/strategies/tail_clock.py）：
   14:30–14:35 观察期半仓 / 14:35–14:45 黄金窗主建仓 / 14:45–14:55 尾窗减半 /
   14:55–15:00 冻结期禁止新开仓 / 收盘后仅复盘。
② 惩罚改「分组封顶」：诱多派发 / 资金背离 / 板块联动 / 量能流动性 / 涨幅位置，
   组内累加、组间封顶，杜绝「四五个相关标签叠到 80 分把好票整票清空」的悬崖误杀。
③ 尾盘正向因子真正计入分数：收盘位置、尾盘阶梯上行、站稳尾盘均价、温和放量、
   尾盘 30 分钟缩量控盘、长下影承接（原先这些只留一个标签，从未进分）。
④ 仓位阶梯取代写死的 15%：基准 × 阶段 × 分数 × 数据等级 × 惩罚 × EV × 大盘温度。
⑤ 操作指南变成可执行指令：买入时间窗、下单方式（限价/分笔/放弃）、本次仓位、
   T+1 纪律；企微推送改为短码，长文只留在界面。
⑥ 事实性修复：ts_code 未定义导致财务注入恒失败；财务分在注入前就被算（恒中性）；
   tail_30min 良性 bonus 从未入分；sector_beta 缺省 1.0 触发「板块偏弱」误罚；
   生产 rt 无 vwap 时四条防镰刀检测静默失效；冻结期仍输出「可轻仓试仓」。

【V26.88 防镰刀优化（保留）】
① 尾盘无量拉升检测 ② 昨日涨停尾盘谨慎 ③ 全天回落尾盘拉升
④ 尾盘跟风检测 ⑤ 尾盘板块同步性验证 ⑥ 全天弱于VWAP尾盘拉升惩罚
"""
import logging
from typing import Optional

import pandas as pd
import numpy as np
from datetime import datetime, timezone, timedelta

from core.config_manager import get_p4_tail_screener_config, get_risk_control_config, get_p1_weekly_config
from core.strategies.p4_tail_screener import evaluate_p4_tail_screener
from core.strategies.score_calibration import _compute_weekly_buff
from core.strategies.risk_control_engine import (
    DEFAULT_RISK_CONFIG,
    RiskControlConfig,
    evaluate_3layer_risk,
    evaluate_layer2_right_side_only,
    hits_indicate_right_side_attack,
    merge_risk_into_engine_result,
)
from core.strategies.strat_base import _p1_min_circ_mv_yi_strat
from core.strategies.tail_clock import (
    PHASE_CLOSED,
    PHASE_FREEZE,
    PHASE_GOLDEN,
    PHASE_LATE,
    PHASE_OBSERVE,
    PHASE_PRE_TAIL,
    phase_to_dict,
    resolve_tail_phase,
)
from core.strategies.fund_mv_utils import (
    effective_turnover_rate_f,
    golden_tier_net_elg_min_yuan,
    golden_tier_punish_net_elg_yuan,
    infer_turnover_rate_f_pct,
)

# 【V26.88+9-23 新增】复用 P3 的财务注入器（fact_financial_reports → rt）
try:
    from core.strategies.strat_p3_intraday import _inject_financial_quality_into_rt
except ImportError:
    _inject_financial_quality_into_rt = None  # 退化时不影响主流程

logger = logging.getLogger(__name__)


def _safe_float(val, default=0.0):
    """P4 内部使用：把任意值转 float，失败/Nan 返回 default。"""
    if val is None:
        return default
    try:
        if isinstance(val, float) and pd.isna(val):
            return default
        if pd.isna(val):
            return default
        return float(val)
    except (TypeError, ValueError):
        return default


def _norm_dash_date(value) -> str:
    """将 trade_date 统一为库内存储格式 'YYYY-MM-DD'（不含分隔符）。

    【审计修复】原实现用 .replace('-','') 抹掉分隔符后与 daily_data.trade_date
    的 'YYYY-MM-DD' 字符串做字典序比较，比如 '20261013' >= '2026-10-08' 为 False，
    导致该分支永远查不到行。本函数不改变历史口径，只保证参数格式与库内一致。
    """
    try:
        raw = str(value or "").strip()
    except Exception:
        return ""
    if not raw:
        return ""
    s = raw.replace("/", "-").replace(".", "-")
    if len(s) == 8 and s.isdigit():
        return f"{s[0:4]}-{s[4:6]}-{s[6:8]}"
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        return s[:10]
    if len(s) == 10 and s.isdigit():
        return f"{s[0:4]}-{s[4:6]}-{s[6:8]}"
    return ""


# ==================== 尾盘惩罚分组（V33 P4 重构）====================
# 原实现是「每命中一个坏形态就 penalty += X」的线性累加，且这些形态高度相关：
#   尾盘无量拉升(20) + 跟风拉升(20) + 板块偏弱(15) + 全天弱于VWAP(25) = 80
#   → 恰好越过 fatal_penalty_threshold，整票战法被清空。
# 结果不是「更安全」，而是「同一只票既被判有罪又被判无罪」的随机性。
# 新口径按经济含义分 5 组、组内累加后封顶，让惩罚保持「可解释、可排序」，
# 真正致命的资金背离（放量净流出）仍保留一票否决力度。
PEN_GROUP_TRAP = "trap"           # 诱多/派发形态
PEN_GROUP_FLOW = "flow"           # 资金背离
PEN_GROUP_BOARD = "board"         # 板块联动
PEN_GROUP_LIQUIDITY = "liquidity" # 量能/流动性
PEN_GROUP_STRETCH = "stretch"     # 极端涨幅/位置

_PEN_GROUP_CN = {
    PEN_GROUP_TRAP: "诱多派发",
    PEN_GROUP_FLOW: "资金背离",
    PEN_GROUP_BOARD: "板块联动",
    PEN_GROUP_LIQUIDITY: "量能流动性",
    PEN_GROUP_STRETCH: "涨幅位置",
}


class _PenaltyBuckets:
    """分组封顶式惩罚累加器（组内累加 → 封顶 → 求和）。"""

    __slots__ = ("_caps", "_acc")

    def __init__(self, cfg=None):
        self._caps = {
            PEN_GROUP_TRAP: _safe_float(getattr(cfg, "tail_penalty_cap_trap", 35.0), 35.0),
            PEN_GROUP_FLOW: _safe_float(getattr(cfg, "tail_penalty_cap_flow", 100.0), 100.0),
            PEN_GROUP_BOARD: _safe_float(getattr(cfg, "tail_penalty_cap_board", 20.0), 20.0),
            PEN_GROUP_LIQUIDITY: _safe_float(getattr(cfg, "tail_penalty_cap_liquidity", 30.0), 30.0),
            PEN_GROUP_STRETCH: _safe_float(getattr(cfg, "tail_penalty_cap_stretch", 25.0), 25.0),
        }
        self._acc = {k: 0.0 for k in self._caps}

    def add(self, group: str, amount: float) -> None:
        try:
            amt = float(amount)
        except (TypeError, ValueError):
            return
        if amt <= 0:
            return
        self._acc[group] = self._acc.get(group, 0.0) + amt

    def subtotal(self, group: str) -> float:
        return min(self._acc.get(group, 0.0), self._caps.get(group, 0.0))

    def total(self) -> float:
        return float(sum(self.subtotal(g) for g in self._caps))

    def breakdown(self) -> dict:
        return {
            _PEN_GROUP_CN[g]: {
                "raw": round(self._acc.get(g, 0.0), 2),
                "capped": round(self.subtotal(g), 2),
                "cap": round(self._caps.get(g, 0.0), 2),
            }
            for g in self._caps
            if self._acc.get(g, 0.0) > 0
        }


# 【V33 P4 性能优化】股性记忆缓存：
# 尾盘扫描每 2–5 分钟一轮 × 2000+ 只候选 × 2 条 SQL（signal_log 历史分 + daily_data 次日/两日记忆），
# 是全链路最重的 DB 热点，且同一交易日内同一只票的「股性记忆」结果不会变。
# 用进程内缓存按 (ts_code, 交易日) 命中，跨轮扫描直接复用。
_P4_MEMORY_CACHE: dict = {}
_P4_MEMORY_CACHE_DAY: str = ""

class P4Tail:
    @staticmethod
    def _resolve_curr_min(rt: dict) -> int:
        raw = rt.get("curr_min")
        if isinstance(raw, (int, float)) and not pd.isna(raw):
            return int(raw)
        bj_tz = timezone(timedelta(hours=8))
        now = datetime.now(bj_tz)
        return now.hour * 60 + now.minute

    @staticmethod
    def _resolve_sector_beta(rt: dict) -> float:
        for key in ("sector_beta", "industry_beta", "sector_mult"):
            raw = rt.get(key)
            if isinstance(raw, (int, float)) and not pd.isna(raw):
                return max(0.7, min(1.5, float(raw)))
        return 1.0

    @staticmethod
    def _sector_beta_known(rt: dict) -> bool:
        """【V33 P4 修复】区分「板块真的弱(beta<1)」与「压根没算板块数据(默认1.0)」。

        原实现 _resolve_sector_beta 缺省返回 1.0，而惩罚条件写成 `sector_beta < 1.05`，
        于是【所有拿不到板块数据的票】都会被扣 15 分“板块偏弱”。
        板块数据在生产 rt 里并非必给字段，这等于给大多数票凭空记一次过。
        """
        for key in ("sector_beta", "industry_beta", "sector_mult"):
            raw = rt.get(key)
            if isinstance(raw, (int, float)) and not pd.isna(raw):
                return float(raw) > 0
        return False

    def _apply_fatal_penalty_guard(self, res: dict) -> dict:
        cfg = getattr(self, "_cfg", None)
        threshold = float(getattr(cfg, "fatal_penalty_threshold", 80.0) or 80.0)
        if float(res.get("penalty", 0.0) or 0.0) < threshold:
            return res
        res["risk_tags"] = list(res.get("risk_tags", []) or [])
        res["risk_tags"].append("[P4风险否决]综合惩罚达到危险阈值")
        res["p4_veto_reason"] = "综合风险惩罚达到危险阈值"
        res["strategies"] = []
        res["burst_score"] = 0.0
        res["surge_bonus"] = 0.0
        # 【V33 P4 修复】原来只清空战法命中，却留下 15% 仓位与「可轻仓试仓」的操作指南，
        # 一旦上层没有用 strategies 做展示门禁，就会出现「已否决但仍在教你买」的自相矛盾。
        res["max_position_ratio"] = 0.0
        res["buy_hint"] = "【风险否决】尾盘综合风险惩罚达到危险阈值，本信号作废，禁止建仓。"
        res["wechat_hint"] = "P4风险否决|禁止建仓"
        res["entry_window"] = {
            "phase": "",
            "window_text": "",
            "order_style": "",
            "allow_new_entry": False,
            "max_position_ratio": 0.0,
        }
        return res

    def _merge_risk_and_guard(self, res: dict, risk: dict) -> dict:
        """【V33.1 已弃用 · 保留仅为兼容】风控惩罚的合并点已前移到 run_all 打分期之后
        （见那里 merge_risk_into_engine_result 的注释），仓位阶梯必须看到合并后的 penalty
        才不会被「罚了三四十分照样发满仓」放过去。

        因此这里【只做致命否决】。若仍按旧语义在这里再 merge 一次，
        会把同一笔风控惩罚加两遍（40 分变 80 分，直接触发一票否决）。
        """
        return self._apply_fatal_penalty_guard(res)

    def _tail_data_quality(self, rt: dict, curr_min: int) -> dict:
        is_backtest = bool(rt.get("is_backtest"))
        if is_backtest:
            return {
                "grade": "P",
                "reason": "日线收盘代理，无历史分钟快照",
                "observation_only": True,
                "anchor_age_min": None,
                "allow_new_entry": False,
            }
        # 【V33 P4 优化】数据质量与「尾盘阶段」解耦：
        #   原实现只判断 curr_min 与锚点新鲜度，于是 14:55–15:00 收盘竞价期间
        #   仍会给出正式买入信号与 15% 仓位 → 用户实际是在收盘竞价里追单，
        #   次日低开即套。现在阶段不允许新开仓时，一律降级为「仅观察」。
        phase = resolve_tail_phase(curr_min, getattr(self, "_cfg", None), is_backtest=False)
        if phase.key in (PHASE_FREEZE, PHASE_CLOSED, PHASE_PRE_TAIL):
            return {
                "grade": "N",
                "reason": phase.block_reason or phase.label,
                "observation_only": True,
                "anchor_age_min": None,
                "allow_new_entry": False,
                "phase": phase_to_dict(phase),
            }
        anchor_times = []
        actual_anchor = rt.get("tail_anchor_min")
        if isinstance(actual_anchor, (int, float)) and not pd.isna(actual_anchor):
            anchor_times.append(int(actual_anchor))
        else:
            for key in ("time_1440", "time_1435"):
                raw = rt.get(key)
                if isinstance(raw, (int, float)) and not pd.isna(raw):
                    anchor_times.append(int(raw))
        try:
            tail_ratio = float(rt.get("tail_vol_ratio", 0.0) or 0.0)
        except (TypeError, ValueError):
            tail_ratio = 0.0
        if not anchor_times or tail_ratio <= 0:
            return {
                "grade": "C",
                "reason": "缺少当日尾盘量能锚点",
                "observation_only": True,
                "anchor_age_min": None,
                "allow_new_entry": bool(phase.allow_new_entry),
                "phase": phase_to_dict(phase),
            }
        anchor_t = max(t for t in anchor_times if t <= curr_min) if any(t <= curr_min for t in anchor_times) else None
        if anchor_t is None:
            return {
                "grade": "C",
                "reason": "尾盘锚点时间晚于扫描时间",
                "observation_only": True,
                "anchor_age_min": None,
                "allow_new_entry": bool(phase.allow_new_entry),
                "phase": phase_to_dict(phase),
            }
        age = curr_min - anchor_t
        max_age = int(getattr(getattr(self, "_cfg", None), "tail_snapshot_max_age_min", 45) or 45)
        if age > max_age:
            return {
                "grade": "C",
                "reason": f"尾盘量能锚点已过期({age}分钟)",
                "observation_only": True,
                "anchor_age_min": age,
                "allow_new_entry": bool(phase.allow_new_entry),
                "phase": phase_to_dict(phase),
            }
        has_path_price = False
        for key in ("price_1325", "price_1425", "price_1440"):
            try:
                if float(rt.get(key, 0.0) or 0.0) > 0:
                    has_path_price = True
                    break
            except (TypeError, ValueError):
                continue
        return {
            "grade": "A" if has_path_price else "B",
            "reason": "尾盘量价锚点齐全" if has_path_price else "尾盘量能可用，路径价格不完整",
            "observation_only": False,
            "anchor_age_min": age,
            "allow_new_entry": bool(phase.allow_new_entry),
            "phase": phase_to_dict(phase),
        }

    @staticmethod
    def _intraday_reversal_risk(rt: dict, now_price: float) -> dict:
        anchors = []
        for key in ("price_1325", "price_1425", "price_1440"):
            try:
                value = float(rt.get(key, 0.0) or 0.0)
            except (TypeError, ValueError):
                value = 0.0
            if value > 0:
                anchors.append((key, value))
        if not anchors or now_price <= 0:
            return {"score": 0.0, "tail_lift_pct": 0.0, "anchor": "", "available": False}
        key, anchor_price = anchors[-1]
        tail_lift_pct = (now_price - anchor_price) / anchor_price * 100.0
        pre_tail_vwap = float(rt.get("pre_tail_vwap", 0.0) or 0.0)
        score = 0.0
        if tail_lift_pct >= 2.0:
            score += 20.0
        elif tail_lift_pct >= 1.2:
            score += 10.0
        if pre_tail_vwap > 0 and anchor_price < pre_tail_vwap * 0.99 and tail_lift_pct >= 1.2:
            score += 15.0
        return {
            "score": score,
            "tail_lift_pct": tail_lift_pct,
            "anchor": key,
            "available": True,
        }

    def __init__(self, screener_cfg=None, risk_cfg: Optional[RiskControlConfig] = None):
        self.name = "P4尾盘智能引擎"
        self.version = "V33_P4_TailClock_AntiScam"
        self.db_cache = {}
        self._cfg_lock_external = screener_cfg is not None
        self._cfg = screener_cfg if screener_cfg is not None else get_p4_tail_screener_config()
        self._risk_cfg_lock_external = risk_cfg is not None
        if risk_cfg is not None:
            self._risk_cfg = risk_cfg
        else:
            try:
                self._risk_cfg = get_risk_control_config()
            except Exception as ex:
                logging.debug("风控配置从 config.yaml 加载失败，使用 DEFAULT_RISK_CONFIG: %s", ex)
                self._risk_cfg = DEFAULT_RISK_CONFIG

    def _map_vr(self, vr):
        xp = [0.0, 0.8, 1.2, 2.0, 4.0]
        yp = [0.0, 30.0, 60.0, 90.0, 100.0]
        return float(np.interp(vr, xp, yp))

    def _map_funds(self, amount, circ_mv_yi):
        """成交额强度映射（amount 为元）；不是换手率，只用于资金评分。"""
        if circ_mv_yi <= 0:
            return 0.0
        inflow_ratio = (amount / (circ_mv_yi * 100000000.0)) * 1000.0
        xp = [0.0, 0.5, 2.0, 5.0, 10.0]
        yp = [0.0, 30.0, 65.0, 85.0, 100.0]
        return float(np.interp(inflow_ratio, xp, yp))

    def _map_cyq(self, cyq):
        xp = [5.0, 10.0, 15.0, 20.0, 25.0, 30.0]
        yp = [100.0, 100.0, 70.0, 40.0, 20.0, 0.0]
        return float(np.interp(cyq, xp, yp))

    def _map_tail_abnormal(self, tail_vol_ratio):
        xp = [0.5, 1.0, 1.5, 3.0, 5.0]
        yp = [10.0, 50.0, 70.0, 95.0, 100.0]
        return float(np.interp(tail_vol_ratio, xp, yp))

    def _check_second_surge(self, df, rt):
        try:
            if df is None or len(df) < 60:
                return 0.0
            curr = df.iloc[-1]
            if float(curr.get("max_60d_pct", 0.0)) < 15.0:
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
            now_px = float(rt.get("price", 0.0))
            if now_px < recent_20_high * 0.96:
                return 0.0
            ma20 = float(curr.get("ma20", 0.0) or 0.0)
            if ma20 > 0 and now_px < ma20:
                return 0.0
            tr_list = []
            for _, r in df.tail(3).iterrows():
                v = float(r.get("vol", 0) or 0)
                c = float(r.get("close", 0) or 0)
                cm = float(r.get("circ_mv", 0) or 0)
                tr0 = float(r.get("turnover_rate_f", 0) or 0)
                tr_list.append(tr0 if tr0 > 0 else infer_turnover_rate_f_pct(v, c, cm))
            turnover_shrink = float(np.mean(tr_list)) < 3.0 if tr_list else False
            # 【V30.10 统一 MACD 口径 2026-10-21】原用 macd_diff / macd_dea 判定，
            # 但这两列在生产 daily_data 里【根本不存在】（只有 macd/macd_signal/macd_hist），
            # 取不到 → 两侧恒 0.0 → 0.0 > 0.0 恒 False → 本加分项【永远不成立】。
            # 现改用 core.strategies.macd_unified 统一判定。
            from core.strategies.macd_unified import is_macd_bull
            _bull = is_macd_bull(curr)
            macd_strong = bool(_bull) if _bull is not None else False
            cyq_stable = float(rt.get("cyq_concentration", 99.0)) < 20.0
            matches = sum([turnover_shrink, macd_strong, cyq_stable])
            if matches == 3:
                return 8.0
            if matches == 2:
                return 5.0
            if matches == 1:
                return 2.0
            return 0.0
        except Exception as e:
            logging.debug(f"P4 _check_second_surge 异常: {e}")
            return 0.0

    def _check_tail_30min_behavior(self, rt: dict, vr: float, pct_chg: float, vol_rt: float, vma5_vol: float) -> dict:
        """
        【V26.88 P4 优化】尾盘最后30分钟(14:30-15:00)特殊行为检测。

        检测逻辑：
        - 尾盘突放量大涨 → 可能是主力尾盘拉升次日出货(诱多)
        - 尾盘缩量横盘 → 主力控盘观望(良性)
        - 尾盘堆量上涨 → 可能是基金做净值
        - 尾盘堆量下跌 → 主力派发嫌疑

        需要的rt字段：
        - tail_vol_ratio: 尾盘每分钟成交量/全天每分钟均量（1.0表示与全天均速相当）
        - tail_30min_vol: 尾盘窗口成交量（可选，仅用于标记数据是否真实存在）
        - curr_min: 当前分钟数(14:30=870)

        返回: {status, bonus, penalty, reason}
        """
        curr_min = rt.get("curr_min", 0)
        if curr_min < 870:
            return {"status": "pre_tail", "bonus": 0.0, "penalty": 0.0, "reason": ""}

        try:
            tail_vol_ratio = float(rt.get("tail_vol_ratio", 0.0) or 0.0)
        except (TypeError, ValueError):
            tail_vol_ratio = 0.0
        if tail_vol_ratio <= 0:
            return {"status": "unavailable", "bonus": 0.0, "penalty": 0.0, "reason": ""}

        if vr > 2.5 and pct_chg > 2.5 and tail_vol_ratio > 2.5:
            return {
                "status": "tail_surge_suspicious",
                "bonus": 0.0,
                "penalty": 25.0,
                "reason": "尾盘突放量大涨(诱多嫌疑)"
            }

        if tail_vol_ratio > 2.0 and pct_chg > 2.0:
            return {
                "status": "tail_fund_nav",
                "bonus": 0.0,
                "penalty": 15.0,
                "reason": "尾盘堆量做净值(基金嫌疑)"
            }

        if tail_vol_ratio < 0.75 and abs(pct_chg) < 0.5 and vr < 0.8:
            return {
                "status": "tail_control_benign",
                "bonus": 8.0,
                "penalty": 0.0,
                "reason": "尾盘缩量控盘(良性)"
            }

        if 0.8 <= tail_vol_ratio <= 1.8 and 0.5 <= pct_chg <= 3.0 and 0.8 <= vr <= 2.0:
            return {
                "status": "tail_healthy",
                "bonus": 5.0,
                "penalty": 0.0,
                "reason": "尾盘温和放量(正常)"
            }

        return {"status": "normal", "bonus": 0.0, "penalty": 0.0, "reason": ""}

    def _close_vwap_deviation_pct(self, rt, df):
        try:
            close_live = float(rt.get("price", 0.0) or rt.get("close", 0.0) or 0.0)
            if close_live <= 0:
                return None
            vwap = rt.get("vwap")
            if vwap is None or (isinstance(vwap, float) and pd.isna(vwap)):
                vwap = rt.get("vwap_price")
            # 【V33 P4 修复】生产 rt 通常不带 vwap / vwap_price，
            # 原实现直接退化成「用日线 amount/vol 近似」，而盘中 df 末行是实时合成行，
            # 该近似值经常失真甚至缺失 → 返回 None，
            # 于是「尾盘偷袭/弱收盘/假突破追高/全天弱于VWAP」四条主力防镰刀检测全部静默失效。
            # 现按信息新旧顺序回落到快照锚点 VWAP：14:40 → 14:25 → 13:25(pre_tail_vwap) → 10:30。
            if vwap is None or (isinstance(vwap, float) and pd.isna(vwap)) or float(vwap or 0.0) <= 0:
                for key in ("vwap_1440", "vwap_1425", "vwap_1325", "pre_tail_vwap", "vwap_1030", "vwap_940", "vwap_930"):
                    raw = rt.get(key)
                    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
                        continue
                    try:
                        v = float(raw)
                    except (TypeError, ValueError):
                        continue
                    if v > 0:
                        vwap = v
                        break
            if vwap is None or (isinstance(vwap, float) and pd.isna(vwap)):
                # 兼容：用日线近似 VWAP，不强行造假；缺失时返回 None。
                if df is not None and len(df) > 0 and "amount" in df.columns and "vol" in df.columns:
                    last = df.iloc[-1]
                    amt = float(last.get("amount", 0.0) or 0.0)
                    vol = float(last.get("vol", 0.0) or 0.0)
                    if amt > 0 and vol > 0:
                        vwap = amt / max(vol, 1e-9)
            vwap = float(vwap) if vwap is not None and not pd.isna(vwap) else 0.0
            if vwap <= 0:
                return None
            return (close_live - vwap) / vwap * 100.0
        except Exception:
            return None

    def _stock_character_memory(self, ts_code, stock_name, pool_key, df, rt):
        """龙头股性评分：历史高分 + 次日高开 + 次日加速 + 两日延续。"""
        global _P4_MEMORY_CACHE_DAY
        try:
            if df is None or df.empty:
                return 0.0, []
            # 【V33 P4 性能】同日同票结果不变 → 进程内缓存，避免每轮扫描重复两条 SQL
            _day_key = ""
            try:
                if "trade_date" in df.columns:
                    _day_key = _norm_dash_date(df["trade_date"].iloc[-1])
            except Exception:
                _day_key = ""
            if not _day_key:
                _day_key = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
            if _P4_MEMORY_CACHE_DAY != _day_key:
                _P4_MEMORY_CACHE.clear()
                _P4_MEMORY_CACHE_DAY = _day_key
            _mem_key = (str(ts_code or ""), _day_key)
            _hit = _P4_MEMORY_CACHE.get(_mem_key)
            if _hit is not None:
                return float(_hit[0]), list(_hit[1])
            score = 0.0
            tags = []
            y = df.iloc[-1]
            ma5 = float(y.get("ma5", 0.0) or 0.0)
            ma20 = float(y.get("ma20", 0.0) or 0.0)
            ma60 = float(y.get("ma60", 0.0) or 0.0)
            wr = float(rt.get("winner_rate", y.get("winner_rate", 0.0)) or 0.0)
            cyq = float(rt.get("cyq_concentration", 99.0) or 99.0)
            if ma5 > ma20 > ma60:
                score += 6.0
                tags.append("📈[趋势龙头]")
            if wr >= 85.0:
                score += 4.0
            if cyq <= 18.0:
                score += 3.0

            # 1) 近期 P4 表现：高分样本越多，越像龙头股性
            # 【V26.88+10-1 P0-1 修复 SQL 注入】改用参数化查询（? 占位符）
            recent_p4_scores = []
            try:
                from data.db_core import get_read_conn, table_exists

                if table_exists("signal_log"):
                    sql = (
                        "SELECT trade_date, score, strategy "
                        "FROM signal_log "
                        "WHERE ts_code = ? AND pool = 'p4' "
                        "ORDER BY trade_date DESC "
                        "LIMIT 12"
                    )
                    with get_read_conn(read_only=True) as con:
                        rows = con.execute(sql, [str(ts_code)]).fetchall()
                    for r in rows or []:
                        try:
                            recent_p4_scores.append(float(r[1] or 0.0))
                        except Exception:
                            continue
                    if recent_p4_scores:
                        hi = sum(1 for x in recent_p4_scores if x >= 85.0)
                        mid = sum(1 for x in recent_p4_scores if 70.0 <= x < 85.0)
                        score += min(10.0, hi * 2.8 + mid * 1.0)
                        if hi >= 3:
                            tags.append("👑[历史强龙]")
                        elif hi >= 1:
                            tags.append("🚀[高开倾向]")
                        if len(recent_p4_scores) >= 6 and hi == 0:
                            score -= 4.0
                            tags.append("🧊[历史弹性弱]")
            except Exception as ex:
                logging.debug("P4 历史分样本读取跳过: %s", ex)

            # 2) 直接从日线库回看后验表现：次日高开、次日加速、两日延续
            # 【审计修复】原实现有两个错误：
            #   (a) 日期参数被抹掉分隔符后与库内 'YYYY-MM-DD' 字符串比较，
            #       结果永远 0 行 → 本块死条件（次日/两日记忆从未生效）；
            #   (b) 取数从“评分日及之后”开始，等于在盘中用未来收盘数据回填分数（前视偏差）。
            # 修复：参数保持库内原格式，并只取“评分日之前”的已收盘日，倒序取 3 行后翻转。
            try:
                from data.db_core import get_read_conn, table_exists

                if "ts_code" in df.columns:
                    td = _norm_dash_date(y.get("trade_date"))
                    if td:
                        src_table = "vw_daily_data_compat" if table_exists("vw_daily_data_compat") else "daily_data"
                        # 表名不能用 ? 参数化（DuckDB 限制），通过白名单限定为已知表
                        if src_table not in ("vw_daily_data_compat", "daily_data"):
                            src_table = "daily_data"
                        if table_exists(src_table):
                            # 【V26.88+10-1 P0-1 修复 SQL 注入】改用参数化查询
                            sql = (
                                f"SELECT trade_date, open, high, close, pre_close "
                                f"FROM {src_table} "
                                f"WHERE ts_code = ? AND trade_date < ? "
                                f"ORDER BY trade_date DESC "
                                f"LIMIT 3"
                            )
                            with get_read_conn(read_only=True) as con:
                                rows_desc = con.execute(sql, [str(ts_code), td]).fetchall()
                            # 倒序取的结果需翻转回时序：rows[0]=最旧，rows[1]=其后一日，rows[2]=两日后
                            rows = list(reversed(rows_desc or []))
                            if len(rows) >= 2:
                                # rows[0] 是评分日之前最近一根已收盘 K，“次日/两日”从 rows[1]/rows[2] 取
                                base = rows[0]
                                d1 = rows[1]
                                d2 = rows[2] if len(rows) >= 3 else None
                                base_close = float(base[3] or 0.0)
                                if base_close > 0:
                                    o1, h1, c1 = float(d1[1] or 0.0), float(d1[2] or 0.0), float(d1[3] or 0.0)
                                    gap1 = (o1 - base_close) / base_close * 100.0 if o1 > 0 else 0.0
                                    ret1 = (c1 - base_close) / base_close * 100.0 if c1 > 0 else 0.0
                                    accel1 = (h1 - o1) / o1 * 100.0 if o1 > 0 else 0.0
                                    if gap1 >= 1.5:
                                        score += 5.0
                                        tags.append("🌅[次日高开]")
                                    elif gap1 >= 0.6:
                                        score += 2.0
                                    if ret1 >= 4.0 and accel1 >= 2.0:
                                        score += 6.0
                                        tags.append("🚀[次日加速]")
                                    elif ret1 >= 2.0:
                                        score += 2.0
                                    ret2 = 0.0  # 初始化，避免未定义
                                    if d2 is not None:
                                        c2 = float(d2[3] or 0.0)
                                        ret2 = (c2 - base_close) / base_close * 100.0 if c2 > 0 else 0.0
                                    if ret2 >= 6.0:
                                        score += 5.0
                                        tags.append("🧱[两日延续]")
                                    elif ret2 >= 3.0:
                                        score += 2.0
                            
                            # 若次日和两日都弱，给龙头记忆轻罚，避免“假强反复”
                            if len(rows) >= 2:
                                d1_close = float(rows[1][3] or 0.0)
                                if base_close > 0 and d1_close > 0:
                                    if (d1_close - base_close) / base_close * 100.0 < -2.0:
                                        score -= 3.0
                                        tags.append("🩸[次日弱反]")
            except Exception as ex:
                logging.debug("P4 次日/两日记忆跳过: %s", ex)

            winner_floor = float(getattr(self._cfg, "winner_floor", 80.0) or 80.0)
            if cyq >= 25.0 and wr < winner_floor:
                score -= 6.0
                tags.append("🩸[股性松散]")
            try:
                _P4_MEMORY_CACHE[_mem_key] = (float(score), list(tags))
            except Exception:
                pass
            return score, tags
        except Exception as e:
            logging.debug("P4 股性记忆异常: %s", e)
            return 0.0, []

    def _tail_edge_score(self, rt: dict, now_price: float, high_price: float, low_price: float,
                         pct_chg: float, vr: float, curr_min: int) -> dict:
        """【V33 P4 新增】尾盘正向因子：把「健康的尾盘」真正折算成加分。

        旧实现只有「惩罚 + 一个 ✅ 标签」：良性尾盘（缩量控盘 / 温和放量 / 收盘贴近日内高位 /
        尾盘阶梯上行 / 站稳尾盘均价）对最终分数没有任何影响，
        等于把 T+1 胜率最高的一类形态白白丢掉。这里把它们量化成 0~tail_edge_bonus_max 的加分。
        """
        cfg = self._cfg
        cap = _safe_float(getattr(cfg, "tail_edge_bonus_max", 14.0), 14.0)
        strong = _safe_float(getattr(cfg, "tail_edge_close_pos_strong", 0.90), 0.90)
        mid = _safe_float(getattr(cfg, "tail_edge_close_pos_mid", 0.75), 0.75)
        eps = _safe_float(getattr(cfg, "tail_edge_vwap_hold_eps", 0.001), 0.001)

        bonus = 0.0
        tags: list = []
        detail: dict = {}

        # 1) 收盘位于日内振幅的位置
        # 【V33 标定结论 · 2026-11-12】这一项原设计是「收盘越贴日高越好」，给 +3.0/+1.5。
        # tools/p4_tail_calibration.py --proxy 用全市场 246,837 根日线（近 250 天）实测：
        #   P4-like 切片（站上 ma20 + 涨幅1-4% + 量比>=1，n=53,510）：
        #     0-30% +0.03% | 30-50% -0.05% | 50-75% -0.13% | 75-90% -0.24% | 90-100% -0.55%
        #   涨幅 0-2% / 2-3.5% / 3.5-5% 三档、以及全市场口径，全部单调向下。
        # 结论：T+1 维度上「收盘贴日高」是【诱多】特征（次日均值回归），不是优点。
        # 因此该因子默认权重归零（tail_edge_close_pos_bonus=0.0），只保留为诊断字段；
        # 若未来样本推翻（阈值可配置），把权重调回即可，不用改代码。
        rng = high_price - low_price
        cpr = None
        if rng > 0 and now_price > 0:
            cpr = (now_price - low_price) / rng
            detail["close_pos_ratio"] = round(float(cpr), 3)
            _cpr_w = _safe_float(getattr(cfg, "tail_edge_close_pos_bonus", 0.0), 0.0)
            if _cpr_w > 0:
                if cpr >= strong:
                    bonus += _cpr_w
                    tags.append("📌收盘贴日内高位")
                elif cpr >= mid:
                    bonus += _cpr_w / 2.0
            else:
                detail["close_pos_note"] = "标定结论：贴日高对T+1为负向，仅作诊断"

        # 2) 尾盘阶梯：14:25 → 14:40 → 现在，是否「一级一级往上走」而不是单点脉冲
        p1425 = _safe_float(rt.get("price_1425"), 0.0)
        p1440 = _safe_float(rt.get("price_1440"), 0.0)
        p1325 = _safe_float(rt.get("price_1325"), 0.0)
        tail_vol_ratio = _safe_float(rt.get("tail_vol_ratio"), 0.0)
        if p1425 > 0 and p1440 > 0 and now_price > 0:
            seg1 = (p1440 / p1425 - 1.0) * 100.0
            seg2 = (now_price / p1440 - 1.0) * 100.0
            detail["tail_seg_1425_1440_pct"] = round(float(seg1), 3)
            detail["tail_seg_1440_now_pct"] = round(float(seg2), 3)
            if seg1 > 0 and seg2 > 0 and seg2 <= 3.0:
                bonus += 4.0
                tags.append("📶尾盘阶梯上行")
                if 0.9 <= tail_vol_ratio <= 2.2:
                    bonus += 2.0
                    tags.append("量价配合")
            elif seg1 < -0.3 and seg2 < -0.3:
                tags.append("🧊尾盘走弱")
        elif p1425 > 0 and now_price > 0:
            lift = (now_price / p1425 - 1.0) * 100.0
            detail["tail_lift_from_1425_pct"] = round(float(lift), 3)
            if 0.2 <= lift <= 2.0:
                bonus += 2.0
        elif p1325 > 0 and now_price > 0:
            lift = (now_price / p1325 - 1.0) * 100.0
            detail["tail_lift_from_1325_pct"] = round(float(lift), 3)

        # 3) 是否站稳「尾盘均价」：尾盘均价是当日最后一段资金成本，站上去才算真承接
        vwap_ref = 0.0
        for _k in ("vwap_1440", "vwap_1425", "pre_tail_vwap", "vwap_1325"):
            v = _safe_float(rt.get(_k), 0.0)
            if v > 0:
                vwap_ref = v
                break
        if vwap_ref > 0 and now_price > 0:
            hold = (now_price / vwap_ref - 1.0)
            detail["tail_vwap_dev_pct"] = round(float(hold * 100.0), 3)
            if hold >= eps:
                bonus += 3.0
                tags.append("🧱站稳尾盘均价")
            elif hold <= -eps:
                tags.append("⚠️尾盘均价下方")

        # 4) 量价配合：尾盘温和放量 + 涨幅在健康区间（不是暴拉，也不是砸盘）
        if 1.0 <= tail_vol_ratio <= 2.0 and 0.5 <= pct_chg <= 3.0 and vr >= 1.0:
            bonus += 3.0
            tags.append("🌊尾盘温和放量")

        bonus = float(min(cap, max(0.0, bonus)))
        return {"bonus": round(bonus, 2), "tags": tags, "detail": detail}

    def _position_ladder(self, *, score: float, phase, data_quality: dict, penalty: float,
                         ev_grade: str, layer_overheat: int, observation_only: bool) -> tuple:
        """【V33 P4 新增】仓位阶梯：取代写死的 max_position_ratio=0.15。

        尾盘买入的真实风险差异主要在四处：现在几点、数据够不够、信号多强、罚了多少。
        60 分的尾盘票和 95 分的尾盘票拿一样的钱，是最典型的「用胜率换暴雷」。
        """
        cfg = self._cfg
        base = _safe_float(getattr(cfg, "p4_base_position_ratio", 0.10), 0.10)
        cap = _safe_float(getattr(cfg, "p4_max_position_ratio", 0.20), 0.20)
        floor = _safe_float(getattr(cfg, "p4_min_position_ratio", 0.04), 0.04)

        ladder = {
            "base": round(base, 3),
            "score_mult": 1.0,
            "phase_mult": 1.0,
            "grade_mult": 1.0,
            "penalty_mult": 1.0,
            "ev_mult": 1.0,
            "heat_mult": 1.0,
        }
        if observation_only or phase is None or not getattr(phase, "allow_new_entry", False):
            ladder["reason"] = phase.block_reason if phase is not None else "尾盘状态未知"
            ladder["ratio"] = 0.0
            return 0.0, ladder

        s = _safe_float(score, 0.0)
        # 【V33.1 审计修复 · P0】分数刻度对齐
        # 原实现把 88/78/68/60 四档直接套在 burst_score 上，但 burst_score 是
        # 「量比 0.25 / 资金 0.30 / 筹码 0.20 / 尾盘异动 0.25」四项加权分，
        # 与 pipeline 展示的综合分（raw_score × regime × sector × decay × safety，
        # 其中 burst 只占 w_burst≈0.5 权重）根本不是同一刻度。
        # 实测 signal_log 里真实 P4 信号 burst_score = 35.98 / 58.11 / 53.85：
        # 套 88 分档等于几乎所有信号都拿 0.30 倍，再被写死的「s<60 不建仓」一刀切成 0 仓位，
        # 于是推送出去的是「分53 | 仓0% | 不建仓」——信号选了却明确不让买。
        # 改为在 burst_score 自己的刻度上做线性归一（floor→0.30，ceiling→1.00），
        # 既保留「分数越高仓位越大」的单调性，也去掉 60 分那道悬崖；两个端点均可配。
        _s_floor = _safe_float(getattr(cfg, "p4_score_scale_floor", 45.0), 45.0)
        _s_ceil = _safe_float(getattr(cfg, "p4_score_scale_ceiling", 85.0), 85.0)
        if _s_ceil <= _s_floor:
            _s_ceil = _s_floor + 1.0
        _frac = (s - _s_floor) / (_s_ceil - _s_floor)
        ladder["score_mult"] = round(float(min(1.0, max(0.30, 0.30 + 0.70 * _frac))), 3)
        ladder["score_input"] = round(float(s), 2)

        ladder["phase_mult"] = float(getattr(phase, "position_mult", 1.0) or 0.0)

        grade = str((data_quality or {}).get("grade", "C") or "C").upper()
        ladder["grade_mult"] = {"A": 1.0, "B": 0.85, "C": 0.60}.get(grade, 0.60)

        p = _safe_float(penalty, 0.0)
        if p <= 10.0:
            ladder["penalty_mult"] = 1.0
        elif p <= 30.0:
            ladder["penalty_mult"] = 0.85
        elif p <= 50.0:
            ladder["penalty_mult"] = 0.60
        else:
            ladder["penalty_mult"] = 0.35

        ladder["ev_mult"] = {"A": 1.0, "B": 0.90, "B+": 0.95, "C": 0.75, "D": 0.45}.get(
            str(ev_grade or "").strip().upper(), 0.85
        )

        oh = int(layer_overheat or 0)
        if oh <= -3:
            ladder["heat_mult"] = 0.70
        elif oh <= -2:
            ladder["heat_mult"] = 0.85

        ratio = base
        for k in ("score_mult", "phase_mult", "grade_mult", "penalty_mult", "ev_mult", "heat_mult"):
            ratio *= float(ladder[k])

        if ratio <= 0:
            ladder["ratio"] = 0.0
            return 0.0, ladder
        # 【V33.1 审计修复】写死的 60 分门槛改为可配（p4_min_score_for_entry）。
        # 原口径的问题不是「设了门槛」，而是门槛与 burst_score 的真实分布不匹配：
        # 实测 P4 信号集中在 36–58，写死 60 等于默认「一律不建仓」。
        _min_entry_score = _safe_float(getattr(cfg, "p4_min_score_for_entry", 50.0), 50.0)
        if s < _min_entry_score:
            ladder["ratio"] = 0.0
            ladder["reason"] = f"综合分{s:.0f}<{_min_entry_score:.0f}，尾盘不建仓"
            return 0.0, ladder
        ratio = max(floor, min(cap, ratio))
        ladder["ratio"] = round(float(ratio), 3)
        return round(float(ratio), 3), ladder

    def run_all(self, df, rt):
        if not self._cfg_lock_external:
            self._cfg = get_p4_tail_screener_config()
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
            "p4_core_screener_pass": False,
            "p4_veto_reason": "",
            "p4_strategy_checks": {},
            "risk_tags": [],
            "data_quality": {},
            "suggested_min_entry_score": 0.0,
            "risk_control": {},
            "buy_hint": "",
            "wechat_hint": "",
            "max_position_ratio": 0.0,
        }
        if df is None or df.empty or len(df) < 21:
            return res

        try:
            y_last = df.iloc[-1]
            # 【V33 P4 修复 · P0】ts_code 在 run_all 内从未定义，下方财务注入
            # `_inject_financial_quality_into_rt(rt, str(ts_code))` 必然抛 NameError，
            # 被 except 吞成 debug 日志 → 财务字段永远注入不进来，
            # 于是 _fin_quality_score_p4 恒返回「盈利质量(中性)」，
            # 「💎盈利质量」加分/降分自上线起就是死代码。这里显式解析一次。
            ts_code = str(
                rt.get("ts_code")
                or rt.get("code")
                or (df.get("ts_code").iloc[-1] if "ts_code" in df.columns and not df.empty else "")
                or ""
            ).strip()
            now_price = float(rt.get("price", rt.get("close", y_last.get("close", 0.0))) or 0.0)
            pre_close = float(rt.get("pre_close", 0.0) or 0.0)
            if pre_close <= 0:
                pre_close = float(y_last.get("pre_close", 0.0) or 0.0)
            if pre_close <= 0:
                pre_close = float(y_last.get("close", 0.0) or 0.0)
            if pre_close <= 0 or now_price <= 0:
                return res

            open_price = float(rt.get("open", y_last.get("open", now_price)) or now_price)
            low_price = float(rt.get("low", y_last.get("low", now_price)) or now_price)
            today_high = float(rt.get("high", y_last.get("high", now_price)) or now_price)
            pct_chg = (now_price - pre_close) / pre_close * 100.0
            vr = float(rt.get("vol_ratio", y_last.get("vol_ratio", 0.0)) or 0.0)
            turnover_f = effective_turnover_rate_f(rt, y_last, now_price)
            vol = float(rt.get("volume", 0.0) or 0.0)

            circ_mv_raw = rt.get("circ_mv")
            if circ_mv_raw is None or pd.isna(circ_mv_raw):
                circ_mv_raw = float(df["circ_mv"].iloc[-1]) if "circ_mv" in df.columns else float(rt.get("total_mv", 10000000)) * 0.6
            circ_mv_yi = float(circ_mv_raw) / 10000.0

            if circ_mv_yi < _p1_min_circ_mv_yi_strat():
                return res

            curr_min = self._resolve_curr_min(rt)
            rt["curr_min"] = curr_min
            # 【V33 P4 新增】尾盘执行时钟：决定「现在还能不能开新仓 / 买多少 / 怎么下单」
            phase = resolve_tail_phase(curr_min, self._cfg, is_backtest=bool(rt.get("is_backtest")))
            res["detail"]["tail_phase"] = phase_to_dict(phase)
            tail_quality = self._tail_data_quality(rt, curr_min)
            res["data_quality"] = tail_quality
            if tail_quality.get("observation_only"):
                res["risk_tags"].append(f"[P4数据降级]{tail_quality.get('reason', '尾盘数据不足')}")

            # 【V33 P4 修复】盈利质量注入必须先于打分：
            # 原顺序是「先打分（读 rt 里的 roe/qoq_profit_yoy…）→ 之后才注入」，
            # 注入即使成功也来不及影响分数 → 恒为中性 0 分。
            # 现在先注入再打分，让财务质量真正参与 P4 排序（最多 ±2.0 分）。
            if _inject_financial_quality_into_rt is not None and ts_code:
                try:
                    _inject_financial_quality_into_rt(rt, ts_code)
                except Exception as _e_fin:
                    logger.debug("P4 财务注入失败（不影响主流程）: %s", _e_fin)
            # 【V26.88+9-24 P4 新增】盈利质量打分 + 大盘温度注入 → 在风险否决之前完成
            # 这样即使被 3 层风控否决，detail 里也能记录财务质量分数，便于审计/排查
            try:
                from core.strategies.p4_tail_screener import _fin_quality_score_p4
                _fin_score, _fin_reason = _fin_quality_score_p4(rt, self._cfg)
                res["detail"]["fin_quality_score"] = round(float(_fin_score), 2)
                res["detail"]["fin_quality_reason"] = str(_fin_reason)
            except Exception as _e_fin_p4:
                logger.debug("P4 财务打分失败: %s", _e_fin_p4)

            rt_risk = dict(rt)
            rt_risk["_pool_key"] = str(rt.get("_pool_key", "p4"))
            risk_pre = evaluate_3layer_risk(df, rt_risk, self._risk_cfg, is_right_side_strategy=False, pool_key="p4")
            if not risk_pre.get("pass_layer1", False):
                res["p4_veto_reason"] = str(risk_pre.get("veto_reason", "") or "")
                res["risk_control"] = {
                    "pass_layer1": False,
                    "pass_layer2": True,
                    "veto_reason": res["p4_veto_reason"],
                    "penalty": float(risk_pre.get("penalty", 0.0) or 0.0),
                    "risk_tags": list(risk_pre.get("risk_tags", []) or []),
                    "suggested_min_entry_score": float(risk_pre.get("suggested_min_entry_score", 0.0) or 0.0),
                    "ui_warnings": list(risk_pre.get("ui_warnings", []) or []),
                }
                res["risk_tags"] = list(risk_pre.get("risk_tags", []) or [])
                res["suggested_min_entry_score"] = float(risk_pre.get("suggested_min_entry_score", 0.0) or 0.0)
                return res

            if circ_mv_yi >= 2000.0:
                size_emoji = "🦍"
            elif circ_mv_yi >= 1000.0:
                size_emoji = "🐘+"
            elif circ_mv_yi >= 500.0:
                size_emoji = "🐘"
            elif circ_mv_yi >= 100.0:
                size_emoji = "🐎"
            else:
                size_emoji = "🐥"

            req_main = golden_tier_net_elg_min_yuan(circ_mv_yi)
            punish_main = golden_tier_punish_net_elg_yuan(circ_mv_yi)

            cyq_conc = float(rt.get("cyq_concentration", y_last.get("cyq_concentration", 999.0)) or 999.0)
            net_main_amount = float(rt.get("net_main_amount", y_last.get("net_main_amount", 0.0)) or 0.0)
            sector_beta = self._resolve_sector_beta(rt)

            # 【V33.1 审计修复】此处原本还有一次 _inject_financial_quality_into_rt(rt, ts_code)，
            # 与上方「先注入再打分」的那次完全重复（同函数、同参数）。
            # 每次扫描每只票多打一次财务表，2000+ 只 × 每 2–5 分钟一轮，是纯浪费。
            # 注入点只保留「打分之前」那一处，否则顺序又会被写散。

            # 【V26.88+9-24 P4 新增】大盘分层温度注入（按流通市值对应 large/mid/growth 层）
            try:
                from core.p1_precompute import compute_market_overheat_score
                _oh = compute_market_overheat_score()
                rt["_pre_market_overheat"] = _oh
                _circ_mv_p4 = _safe_float(rt.get("circ_mv"), _safe_float(y_last.get("circ_mv"), 0.0))
                _cm_yi_p4 = _circ_mv_p4 / 10000.0
                if _cm_yi_p4 >= 500.0:
                    _layer_p4 = "large"
                elif _cm_yi_p4 >= 100.0:
                    _layer_p4 = "mid"
                else:
                    _layer_p4 = "growth"
                rt["_market_layer"] = _layer_p4
                rt["_layer_overheat_score"] = int(
                    (_oh.get("layer_scores") or {}).get(_layer_p4, _oh.get("market_overheat_score", 0))
                )
                rt["_market_overheat_score"] = int(_oh.get("market_overheat_score", 0))
                rt["_market_regime"] = str(_oh.get("regime", "neutral"))
            except Exception as _e_oh_p4:
                logger.debug("P4 大盘温度注入失败: %s", _e_oh_p4)
            ev = evaluate_p4_tail_screener(df, rt, self._cfg)
            res["p4_core_screener_pass"] = bool(ev.get("p4_core_screener_pass"))
            res["p4_veto_reason"] = str(ev.get("veto_reason", "") or "")
            res["p4_strategy_checks"] = dict(ev.get("strategy_checks") or {})

            # 【V26.80 统一 A 股特色评分】P4 尾盘场景：涨停板状态 + 炸板修复
            try:
                from core.p2_p5_a_share_enhance import unified_a_share_score

                _a_share = unified_a_share_score(df, rt)
                if _a_share.get("is_p2_p5_veto"):
                    res["p4_veto_reason"] = _a_share.get("veto_reason", "A 股特色高风险")
                    res["risk_tags"].append(f"🚨[统一风控]{_a_share.get('veto_reason')}")
                    return res

                _a_share_tags = _a_share.get("all_tags", [])[:5]
                for tag in _a_share_tags:
                    if tag and tag not in res["risk_tags"]:
                        res["risk_tags"].append(tag)

                # A 股特色加权（尾盘权重 0.18）
                _a_boost = float(_a_share.get("total_boost", 0.0)) * 0.18
                res["a_share_p4_boost"] = round(_a_boost, 2)
                res["a_share_p4_tags"] = _a_share_tags
            except Exception as _e_a4:
                logging.debug("V26.80 P4 统一 A 股特色评分跳过: %s", _e_a4)

            if not ev.get("veto_pass"):
                return res

            hits = list(ev.get("strategies") or [])
            if not hits:
                return res

            # 【V26.80 跨池信号接力】P1→P2→P3→P4 接力加分
            try:
                from core.p2_p5_signal_relay import apply_relay_bonus_to_pool_res

                ts_code = str(rt.get("ts_code", rt.get("code", "")) or "")
                if ts_code:
                    res = apply_relay_bonus_to_pool_res(res, ts_code, "p4", apply_to_burst=True)
            except Exception as _e_relay4:
                logger.debug("V26.80 P4 跨池接力跳过: %s", _e_relay4)

            if hits_indicate_right_side_attack(hits):
                l2 = evaluate_layer2_right_side_only(df, rt_risk, self._risk_cfg)
                for uw in l2.get("ui_warnings") or []:
                    res["risk_tags"].append("⚠️[二层预警]" + str(uw))
                if not l2.get("pass_layer2", True):
                    res["p4_veto_reason"] = str(l2.get("veto_reason", "") or "")
                    res["risk_control"] = {
                        "pass_layer1": True,
                        "pass_layer2": False,
                        "veto_reason": res["p4_veto_reason"],
                        "penalty": float(risk_pre.get("penalty", 0.0) or 0.0),
                        "risk_tags": list(risk_pre.get("risk_tags", []) or []),
                        "suggested_min_entry_score": float(risk_pre.get("suggested_min_entry_score", 0.0) or 0.0),
                        "ui_warnings": list(risk_pre.get("ui_warnings", []) or []),
                    }
                    res["risk_tags"] = list(risk_pre.get("risk_tags", []) or [])
                    res["suggested_min_entry_score"] = float(risk_pre.get("suggested_min_entry_score", 0.0) or 0.0)
                    return res

            # ==================== 尾盘风险惩罚（V33 P4 · 分组封顶）====================
            # 旧实现是「每个坏形态各自 penalty += X」的线性累加，而尾盘的坏形态彼此高度相关
            # （无量拉升 + 跟风 + 板块偏弱 + 全天弱于VWAP 经常同时成立），
            # 四五个标签叠起来轻松越过 fatal_penalty_threshold=80 → 战法被整票清空。
            # 结果不是更安全，而是「同一只票随机被判有罪/无罪」。
            # 新口径：按经济含义分 5 组，组内累加后组间封顶求和（见 _PenaltyBuckets）。
            pen = _PenaltyBuckets(self._cfg)
            vwap_penalty = 0.0          # 仅用于「尾盘有虚火」的操作建议分流，不参与分数
            tail_smart_penalty = 0.0

            close_vwap_dev = self._close_vwap_deviation_pct(rt, df)
            if close_vwap_dev is not None:
                res["detail"]["close_vwap_dev_pct"] = round(float(close_vwap_dev), 3)
                if close_vwap_dev > 2.5:
                    res["risk_tags"].append("🧨[尾盘偷袭]收盘显著高于VWAP")
                    vwap_penalty += 18.0
                    pen.add(PEN_GROUP_TRAP, 18.0)
                elif close_vwap_dev < -1.5:
                    res["risk_tags"].append("🧊[弱收盘]收盘低于VWAP")
                    vwap_penalty += 8.0
                    pen.add(PEN_GROUP_TRAP, 8.0)

            vwap = 0.0
            vwap_raw = rt.get("vwap")
            if vwap_raw is None or (isinstance(vwap_raw, float) and pd.isna(vwap_raw)):
                vwap_raw = rt.get("vwap_price")
            try:
                vwap = float(vwap_raw) if vwap_raw is not None and not pd.isna(vwap_raw) else 0.0
            except (TypeError, ValueError):
                vwap = 0.0
            if vwap <= 0:
                for _vk in ("vwap_1440", "vwap_1425", "vwap_1325", "pre_tail_vwap", "vwap_1030"):
                    _vv = rt.get(_vk)
                    try:
                        _vf = float(_vv) if _vv is not None and not pd.isna(_vv) else 0.0
                    except (TypeError, ValueError):
                        _vf = 0.0
                    if _vf > 0:
                        vwap = _vf
                        break
            if vwap <= 0 and close_vwap_dev is not None:
                vwap = now_price / (1.0 + float(close_vwap_dev) / 100.0)

            intraday_high_gap = ((today_high - now_price) / now_price * 100.0) if now_price > 0 else 0.0
            vwap_gap_pct = ((now_price - vwap) / vwap * 100.0) if vwap > 0 else 0.0
            if pct_chg >= 2.5 and vwap_gap_pct > 1.2 and vr >= 1.6:
                res["risk_tags"].append("🧨[假突破]尾盘远离VWAP追高")
                vwap_penalty += 20.0
                pen.add(PEN_GROUP_TRAP, 20.0)
            if intraday_high_gap > 1.8 and now_price < today_high * 0.985 and vr >= 1.5:
                res["risk_tags"].append("🧨[冲高回落]收盘承接不足")
                vwap_penalty += 18.0
                pen.add(PEN_GROUP_TRAP, 18.0)

            # 聪明钱验证：尾盘量比放大但主力净流出 → 资金背离组，保留一票否决力度
            tail_vr_chk = float(rt.get("tail_vol_ratio", y_last.get("tail_vol_ratio", 0.0)) or 0.0)
            _nm_rt = rt.get("net_main_amount")
            if _nm_rt is None or (isinstance(_nm_rt, float) and pd.isna(_nm_rt)):
                net_main_chk = float(y_last.get("net_main_amount", 0.0) or 0.0)
            else:
                try:
                    net_main_chk = float(_nm_rt)
                except (TypeError, ValueError):
                    net_main_chk = float(y_last.get("net_main_amount", 0.0) or 0.0)
            if tail_vr_chk > 2.0 and net_main_chk < 0.0:
                tail_smart_penalty = 100.0
                res["risk_tags"].append("🩸[派发预警]尾盘放量但主力净流出（请人工复核）")
                pen.add(PEN_GROUP_FLOW, 100.0)

            reversal_risk = self._intraday_reversal_risk(rt, now_price)
            res["detail"]["intraday_reversal_score"] = round(float(reversal_risk["score"]), 2)
            res["detail"]["tail_lift_pct"] = round(float(reversal_risk["tail_lift_pct"]), 3)
            res["detail"]["reversal_anchor"] = reversal_risk["anchor"]
            if reversal_risk["score"] >= 20.0 and curr_min >= 870:
                vwap_penalty += 20.0
                res["risk_tags"].append("⚠️[路径反转]尾盘相对尾盘锚点急拉")
                pen.add(PEN_GROUP_TRAP, 20.0)

            # 【V26.88 P4 优化】尾盘30分钟异动检测：罚进「诱多派发」组，奖进尾盘正向因子
            vma5_vol = float(rt.get("vol_ma5", y_last.get("vol_ma5", 0.0)) or 0.0)
            tail_behavior_bonus = 0.0
            tail_30min_result = self._check_tail_30min_behavior(rt, vr, pct_chg, vol, vma5_vol)
            if tail_30min_result.get("penalty", 0.0) > 0:
                pen.add(PEN_GROUP_TRAP, float(tail_30min_result["penalty"]))
                reason = tail_30min_result.get("reason", "")
                if reason:
                    res["risk_tags"].append(f"🚨[尾盘30分]{reason}")
            elif tail_30min_result.get("bonus", 0.0) > 0:
                # 【V33 修复】原实现这里只往 risk_tags 里写了一个 ✅ 标签，
                # bonus 从未进入任何分数 → 「良性尾盘」这条最重要的 T+1 胜率来源被静默丢弃。
                tail_behavior_bonus = float(tail_30min_result["bonus"])
                reason = tail_30min_result.get("reason", "")
                if reason:
                    res["risk_tags"].append(f"✅[尾盘30分]{reason}")

            # ①尾盘无量拉升：尾盘涨但全天量能不足（做收盘价嫌疑）
            if curr_min >= 870 and pct_chg > 2.0 and vr < 1.2:
                pen.add(PEN_GROUP_TRAP, 20.0)
                res["risk_tags"].append("⚠️[尾盘无量拉升]可能是做收盘价")

            # ②昨日涨停 T+1 收割风险
            prev_pct = 0.0
            if len(df) >= 2:
                prev_pct = float(df.iloc[-2].get("pct_chg", 0.0) or 0.0)
            if curr_min >= 870 and prev_pct >= 9.5 and pct_chg > 3.0:
                pen.add(PEN_GROUP_TRAP, 25.0)
                res["risk_tags"].append(f"⚠️[T+1收割]昨涨停({prev_pct:.1f}%)今冲高({pct_chg:.1f}%)")

            # ③全天回落尾盘拉升（典型收割形态）
            day_direction = 0.0
            if open_price > 0 and now_price > 0:
                day_direction = (now_price - open_price) / open_price * 100.0
            if curr_min >= 870 and day_direction < -1.0 and pct_chg > 1.5:
                pen.add(PEN_GROUP_TRAP, 30.0)
                res["risk_tags"].append("⚠️[全天回落尾拉]收割嫌疑")

            # ④⑤板块联动：只有【真的拿到了板块 beta】才判「板块偏弱」。
            # 【V33 修复】原实现缺省 sector_beta=1.0 且条件是 `<1.05`，
            # 等于所有无板块数据的票都被白扣 15 分。
            sector_beta_known = self._sector_beta_known(rt)
            if curr_min >= 870 and sector_beta_known:
                if pct_chg > 3.0 and sector_beta < 1.0:
                    pen.add(PEN_GROUP_BOARD, 20.0)
                    res["risk_tags"].append("⚠️[尾盘跟风]板块未配合")
                if pct_chg > 4.0 and sector_beta < 1.05:
                    pen.add(PEN_GROUP_BOARD, 15.0)
                    res["risk_tags"].append("⚠️[尾盘涨幅异常]板块偏弱")
            elif curr_min >= 870 and not sector_beta_known and pct_chg > 3.0:
                res["detail"]["sector_beta_missing"] = True

            # ⑥全天弱于VWAP 尾盘拉升（诱多嫌疑）
            if close_vwap_dev is not None and close_vwap_dev < -0.5:
                if curr_min >= 870 and pct_chg > 2.0:
                    pen.add(PEN_GROUP_TRAP, 25.0)
                    res["risk_tags"].append("⚠️[全天弱于VWAP]尾盘拉升疑似诱多")

            # ⑦收盘贴着当日最高价 + 涨幅已经不小 = 尾盘拉高诱多
            # 【V33 标定依据】tools/p4_tail_calibration.py --proxy（近 250 天全市场日线）显示：
            #   收盘位于日内振幅 90-100% 的 T+1 均值 -0.55%，相对中枢 50-75% 的 -0.13% 差 -0.42%，
            #   且在涨幅 0-2% / 2-3.5% / 3.5-5% 三档里单调成立。
            #   即「尾盘收在最高价」不是强势确认，而是次日回落的概率来源。
            #   注意排除涨停区（>=7% 由 STRETCH 组与涨停龙头加成单独处理，避免双罚）。
            if curr_min >= 870 and now_price > 0 and today_high > low_price > 0:
                _cpr_chk = (now_price - low_price) / (today_high - low_price)
                _cpr_flag = _safe_float(getattr(self._cfg, "tail_edge_close_pos_flag_high", 0.9), 0.9)
                if _cpr_chk >= _cpr_flag and 3.0 <= pct_chg < 7.0:
                    pen.add(PEN_GROUP_TRAP, 8.0)
                    res["risk_tags"].append("⚠️[贴日高]尾盘拉至当日最高价(次日易回落)")
                    res["detail"]["close_pos_trap"] = round(float(_cpr_chk), 3)

            # 量能/流动性组
            if vr < 0.8:
                pen.add(PEN_GROUP_LIQUIDITY, float(np.interp(vr, [0.0, 0.5, 0.8], [50.0, 20.0, 0.0])))
            if turnover_f < 0.8:
                pen.add(PEN_GROUP_LIQUIDITY, float(np.interp(turnover_f, [0.0, 0.4, 0.8], [60.0, 25.0, 0.0])))

            # 长下影承接（原本用 penalty -= 15 的负惩罚实现，与分组封顶口径冲突，改为正向因子）
            entity_bottom = min(now_price, open_price)
            lower_shadow_pct = (entity_bottom - low_price) / pre_close * 100.0
            is_deep_v = lower_shadow_pct > 2.5 and pct_chg > 0

            vmf = float(rt.get("vr_morning_floor", y_last.get("vr_morning_floor", -1.0)) or -1.0)
            if vmf < 0.0:
                vmf = float(rt.get("vr_1030", y_last.get("vr_1030", -1.0)) or -1.0)
            if vmf >= 0.0 and vmf < 0.8:
                pen.add(PEN_GROUP_LIQUIDITY, 40.0)

            if pct_chg >= 7.0:
                pen.add(PEN_GROUP_STRETCH, 25.0)

            penalty = pen.total()
            if pen.breakdown():
                res["detail"]["penalty_groups"] = pen.breakdown()
                res["detail"]["penalty_pre_cap"] = round(
                    float(sum(v["raw"] for v in pen.breakdown().values())), 2
                )

            # 【V26.88+9-24 P4 新增】HiThink 炸板/封板拦截（尾盘避免买入炸板股）
            try:
                _ht_brk = rt.get("_hithink_limit_burst") or {}
                if _ht_brk:
                    _brk_open = int(_ht_brk.get("open_times", 0) or 0)
                    _brk_turnover = float(_ht_brk.get("turnover_ratio_pct", 0.0) or 0.0)
                    # 炸板即在 HiThink 炸板池 → 直接否决
                    res["p4_veto_reason"] = (
                        f"尾盘拦截：HiThink炸板池(回开{_brk_open}次 换手{_brk_turnover:.1f}%)"
                    )
                    res["risk_control"] = {
                        "pass_layer1": False,
                        "pass_layer2": True,
                        "veto_reason": res["p4_veto_reason"],
                        "penalty": 100.0,
                        "risk_tags": [f"🚨[尾盘炸板]HiThink炸板池(回开{_brk_open}次 换手{_brk_turnover:.1f}%)"],
                        "ui_warnings": ["尾盘买入炸板股高风险"],
                    }
                    res["risk_tags"].append(f"🚨[尾盘炸板]HiThink炸板池(回开{_brk_open}次)")
                    return res
            except Exception as _e_p4_brk:
                logger.debug("P4 炸板拦截异常: %s", _e_p4_brk)

            s_vol = self._map_vr(vr)
            s_fund = self._map_funds(net_main_amount, circ_mv_yi)
            s_chip = self._map_cyq(cyq_conc)
            tail_ratio_val = float(rt.get("tail_vol_ratio", 0.0) or 0.0)
            s_tail_abnormal = self._map_tail_abnormal(tail_ratio_val) if tail_ratio_val > 0 else 0.0
            raw_burst = (s_vol * 0.25) + (s_fund * 0.3) + (s_chip * 0.2) + (s_tail_abnormal * 0.25)

            if vr < 0.6 or cyq_conc > 25.0:
                burst_score = min(raw_burst, 50.0)
            else:
                burst_score = min(raw_burst, 100.0)

            stock_memory_score, stock_memory_tags = self._stock_character_memory(
                rt.get("ts_code", rt.get("code", "")),
                rt.get("name", ""),
                str(rt.get("_pool_key", "p4")),
                df,
                rt,
            )
            next_day_confidence = 0.0
            if stock_memory_score:
                res["detail"]["stock_memory_score"] = round(float(stock_memory_score), 2)
                if stock_memory_tags:
                    res["risk_tags"].extend(stock_memory_tags)
                if any(tag in stock_memory_tags for tag in ("🌅[次日高开]", "🚀[次日加速]", "🧱[两日延续]")):
                    next_day_confidence += 2.0
                if any(tag in stock_memory_tags for tag in ("🧊[历史弹性弱]",)):
                    next_day_confidence -= 1.5

            surge_bonus_val = self._check_second_surge(df, rt) if len(df) >= 60 else 0.0
            if stock_memory_score:
                surge_bonus_val += min(8.0, max(0.0, stock_memory_score) * 0.22)
            if next_day_confidence:
                surge_bonus_val += min(6.0, max(-3.0, next_day_confidence))

            # 【V26.88+9-22 优化14】HiThink 涨停连板尾盘溢价加分
            # 尾盘买入涨停龙头股，次日溢价显著：3板+封单>1亿 → +15，2板 → +10，首板 → +5
            # 已被 P4 优化15「炸板拦截」过滤；这里只处理「未炸板」且已封板的高质量股
            _ht_lu = rt.get("_hithink_limit_up") or {}
            # 【V26.88+9-27 修复P0】原逻辑永远False（not False == True），龙头连板加分永远不执行
            # 修复：改为检查 p4_core_screener_pass 是否为 True
            if _ht_lu and ev.get("p4_core_screener_pass") is True:
                _ht_cnt = int(_safe_float(_ht_lu.get("continue_day_cnt"), 1) or 1)
                _ht_seal_yi = _safe_float(_ht_lu.get("seal_money"), 0.0) / 1e8
                if _ht_cnt >= 3 and _ht_seal_yi >= 1.0:
                    surge_bonus_val += 15.0
                    res["risk_tags"].append(f"💎[涨停龙头]{_ht_cnt}板封板{_ht_seal_yi:.1f}亿")
                elif _ht_cnt >= 3:
                    surge_bonus_val += 10.0
                    res["risk_tags"].append(f"💎[连板]{_ht_cnt}板")
                elif _ht_cnt >= 2:
                    surge_bonus_val += 8.0
                    res["risk_tags"].append(f"💎[2板]{_ht_lu.get('limit_up_reason', '')}")
                else:
                    surge_bonus_val += 4.0
                    res["risk_tags"].append("💎[涨停首板]")

            # 【V33 P4 新增】尾盘正向因子：收盘位置 / 尾盘阶梯 / 站稳尾盘均价 / 温和放量
            # 与「尾盘 30 分钟良性」的分红、长下影承接一起并入 surge_bonus，
            # 让「健康的尾盘」真正折算成分数，而不是只留一个 ✅ 标签。
            _tail_edge = self._tail_edge_score(rt, now_price, today_high, low_price, pct_chg, vr, curr_min)
            _tail_edge_bonus = float(_tail_edge.get("bonus", 0.0) or 0.0) + float(tail_behavior_bonus or 0.0)
            if is_deep_v:
                _tail_edge_bonus += 6.0
                res["risk_tags"].append("🕳️[长下影承接]")
            # 【V33.1 审计修复】原实现这里写死 min(20.0, ...)，而配置里
            # tail_edge_bonus_max=10 只作用于 _tail_edge_score 内部那四项，
            # 加上「尾盘30分良性行为」与「长下影承接」后实际上限是 20 —— 配置名与实际不符。
            # 现在合并封顶交给独立字段 tail_edge_total_bonus_max（默认 20，可调）。
            _tail_edge_total_cap = _safe_float(
                getattr(self._cfg, "tail_edge_total_bonus_max", 20.0), 20.0
            )
            _tail_edge_bonus = min(_tail_edge_total_cap, max(0.0, _tail_edge_bonus))
            if _tail_edge_bonus > 0:
                surge_bonus_val += _tail_edge_bonus
                for _t in (_tail_edge.get("tags") or []):
                    if _t and _t not in res["risk_tags"]:
                        res["risk_tags"].append(f"✅{_t}")
            res["detail"]["tail_edge_bonus"] = round(_tail_edge_bonus, 2)
            if _tail_edge.get("detail"):
                res["detail"]["tail_edge"] = dict(_tail_edge["detail"])

            pool_hint = str(rt.get("_pool_key", "")).lower()
            if pool_hint in ("p4", "p5"):
                is_p5_mode = pool_hint == "p5"
            else:
                is_p5_mode = curr_min >= 900

            if is_p5_mode:
                positive_hits_before_p5 = len([h for h in hits if ("⚠️" not in h and "💀" not in h and "🩸" not in h)])
                if net_main_amount > req_main and positive_hits_before_p5 >= 1:
                    hits.append("👑[实锤] 主力真金盖章")
                    surge_bonus_val += 3.0
                elif net_main_amount < punish_main:
                    hits.append("🩸[绞肉机] 尾盘诱多派发")
                    # 【V33.1 审计修复】原为 `penalty += 42.0`，直接加在【已封顶】的
                    # pen.total() 之上 → 这一条旁路了五组封顶的全部设计
                    # （例如 TRAP 组已顶到 35，再加 42 就是 77，等于封顶形同虚设）。
                    # 归入「资金背离」组后重算，语义与其它派发形态一致。
                    pen.add(PEN_GROUP_FLOW, 42.0)
                    penalty = pen.total()
                    if pen.breakdown():
                        res["detail"]["penalty_groups"] = pen.breakdown()
                # 【上线前修复 2026-10-18】北向资金因子整体退役：
                # hk_hold 数据源下线后 hk_vol 全库恒为 0，hk_vol_3d 无任何生产写入方，
                # 「外资辅助」标签恒不触发，只会输出伪信号，故一并移除。

            board_bonus = 0.0
            if sector_beta >= 1.20:
                board_bonus = 6.0
                res["risk_tags"].append("🌋[热板块加权]")
            elif sector_beta >= 1.10:
                board_bonus = 3.0
                res["risk_tags"].append("🔥[偏热板块]")
            elif sector_beta < 0.9:
                board_bonus = -3.0
                res["risk_tags"].append("🧊[冷板块折价]")

            # 【V26.88+ P4 优化】周线趋势/放量 buff（互联网实战共识）：
            # 周线多头 + 周线放量 → +6 双强；任一满足 → +3；空头+缩量 → 不动。
            # 复用 P1 weekly 配置（trend_min=4.5 / volume_min=2.5），不引入新阈值。
            try:
                _p4_ts_code = str(
                    rt.get("ts_code", rt.get("code", ""))
                    or (df.get("ts_code").iloc[-1] if df is not None and not df.empty and "ts_code" in df.columns else "")
                    or ""
                ).strip()
                _p4_weekly_root = get_p1_weekly_config()
                _p4_wb = _compute_weekly_buff(_p4_ts_code, _p4_weekly_root)
                board_bonus += _p4_wb["pts"]
            except Exception as _p4_wbe:
                logger.debug("P4 周线 buff 计算异常: %s", _p4_wbe)
                _p4_wb = {"pts": 0.0, "trend": 0.0, "vol": 0.0,
                          "is_double_strong": 0.0, "is_trend_hit": 0.0, "is_vol_hit": 0.0}

            # 【V26.88+9-24 P4 新增】合并财务质量打分（最多 ±2.0）
            _fin_p4_score = float((res.get("detail") or {}).get("fin_quality_score", 0.0) or 0.0)
            if _fin_p4_score != 0:
                _fin_label = (res.get("detail") or {}).get("fin_quality_reason", "")
                if _fin_p4_score > 0:
                    res["risk_tags"].append(f"💎[盈利质量]{_fin_label}")
                else:
                    res["risk_tags"].append(f"⚠️[盈利质量]{_fin_label}")

            # 【V26.88+9-24 P4 新增】分层大盘温度加权（尾盘对系统性风险更敏感，幅度比 P3 大）
            try:
                _layer_oh_p4 = int(rt.get("_layer_overheat_score", 0))
                if _layer_oh_p4 != 0:
                    _oh_delta_p4 = float(_layer_oh_p4) * 0.7  # 尾盘更敏感：±2.1 分
                    _fin_p4_score += _oh_delta_p4  # 合并到总分
                    _layer_name_p4 = str(rt.get("_market_layer", "?"))
                    _oh_label_p4 = {-3: "过热", -2: "偏热", -1: "微热",
                                    1: "微冷", 2: "偏冷", 3: "超冷"}.get(_layer_oh_p4, "")
                    res["risk_tags"].append(
                        f"🌡️[大盘温度][{_layer_name_p4}层]{_oh_label_p4}({_layer_oh_p4:+d})  {_oh_delta_p4:+.1f}分"
                    )
                    res["detail"]["market_overheat_delta"] = round(_oh_delta_p4, 2)
            except Exception as _e_oh_p4b:
                logger.debug("P4 大盘温度加权异常: %s", _e_oh_p4b)

            # 【V33.1 审计修复】原式只封上界不封下界：board_bonus 可 -3、财务可 -2、
            # 大盘温度可 -2.1，叠加后 burst_score 会变成负数；而 pipeline 里
            # `if hits and burst_score > 0` 会把负分信号直接丢掉，同时在仓位阶梯里落进最低档。
            # 统一为 [0, 100] 封顶。
            res["burst_score"] = round(min(100.0, max(0.0, burst_score + board_bonus + _fin_p4_score)), 2)
            res["surge_bonus"] = round(surge_bonus_val, 2)
            res["penalty"] = round(penalty, 2)

            # 【V33.1 审计修复 · P0】三层风控惩罚必须在【仓位阶梯之前】并入。
            # 原顺序是：先按尾盘分组惩罚算仓位 → ... → 最后才 merge 风控惩罚，
            # 于是被三层风控扣了三四十分的票，仓位仍按「尾盘惩罚≈0」拿满（penalty_mult=1.0），
            # 只有等到 ≥fatal(80) 才被一次性清零——中间那一整段完全没有反映到仓位上。
            # 这里只并入 penalty / risk_tags / risk_control / suggested_min，
            # 不做致命否决（此时 strategies 还没写完，否决统一放到后面）。
            res = merge_risk_into_engine_result(
                res, risk_pre, penalty_key="penalty", deduct_burst_score=False,
            )
            sector_strength = float(rt.get("sector_strength", ev.get("detail", {}).get("sector_strength", sector_beta)) or sector_beta)
            mainline_score = float(rt.get("mainline_score", ev.get("detail", {}).get("mainline_score", 0.0)) or 0.0)
            mainline_reason = str(rt.get("mainline_reason", ev.get("detail", {}).get("mainline_reason", "")) or "")
            entry_reason = "主线确认，次日看强"
            if any("P4-06" in h for h in hits):
                entry_reason = "底仓趋势延续，尾盘仍稳"
            elif mainline_score >= 3.0:
                if next_day_confidence >= 1.5:
                    entry_reason = "主线前排，延续更稳"
                elif sector_strength >= 1.10:
                    entry_reason = "主线偏强，等次日看"
                else:
                    entry_reason = "主线里偏强，等确认"
            elif mainline_score <= -2.0:
                entry_reason = "不像主线，防假强"
            elif vwap_penalty > 0 or tail_smart_penalty > 0:
                entry_reason = "尾盘有虚火，防兑现"
            res["detail"].update({
                "异动分": round(s_tail_abnormal, 2),
                "抢筹分": round(s_fund, 2),
                "筹码分": round(s_chip, 2),
                "入池理由": entry_reason,
                "市值档位": size_emoji,
                "sector_beta": round(sector_beta, 3),
                "sector_strength": round(sector_strength, 3),
                "mainline_score": round(mainline_score, 2),
                "mainline_reason": mainline_reason,
                "board_bonus": round(board_bonus, 2),
                "next_day_confidence": round(next_day_confidence, 2),
                "p4_screener": ev.get("detail", {}),
                "data_quality": res.get("data_quality", {}),
                "intraday_reversal_score": round(float(reversal_risk.get("score", 0.0)), 2),
                "tail_lift_pct": round(float(reversal_risk.get("tail_lift_pct", 0.0)), 3),
                # 【V26.88+ P4 优化】周线 buff 明细（前端透传 / 阵列仓诊断可见）
                "weekly_buff_pts": round(_p4_wb.get("pts", 0.0), 2),
                "weekly_buff_trend": round(_p4_wb.get("trend", 0.0), 2),
                "weekly_buff_vol": round(_p4_wb.get("vol", 0.0), 2),
                "weekly_buff_double": bool(_p4_wb.get("is_double_strong", 0.0) >= 1.0),
            })
            res["strategies"] = hits

            # ========== 入场质量 / EV / 仓位阶梯 / 可执行尾盘操作指南 ==========
            # 【V33 重构】原顺序是「先写死 15% 仓位 → 再生成买入口径 → 才算赔率」，
            # 于是赔率、尾盘阶段、数据质量、惩罚分对仓位几乎没有影响（最多一刀砍到 5%），
            # 且 14:55 之后依然输出「可轻仓试仓」→ 用户实际是在收盘竞价里追单。
            # 新顺序：先算赔率/EV → 再按（分数·阶段·数据·惩罚·EV·大盘温度）算仓位 → 最后写操作指南。
            _eq = None
            _ev_p4 = None
            try:
                from core.stop_loss_utils import (
                    evaluate_entry_quality, compute_expected_value,
                )
                strat_type = "leader" if mainline_score >= 3.0 else "right_side"
                _eq = evaluate_entry_quality(df, rt, risk_reward_min=2.0, strategy_type=strat_type)
                _ev_p4 = compute_expected_value(
                    risk_reward=_eq["risk_reward_ratio"],
                    signal_strength=float(res.get("burst_score", 60.0) or 60.0),
                    penalty=float(res.get("penalty", 0.0) or 0.0),
                )
                res["entry_quality"] = _eq
                res["expected_value"] = _ev_p4
                res["auto_stop_loss"] = _eq["stop_loss"]
                res["auto_take_profit"] = _eq["take_profit"]
                res["trailing_stop_rules"] = _eq["trailing_stop_rules"]
                res["trade_grade"] = _ev_p4["trade_grade"]
                if not _eq["pass_2_1"] and _eq["entry_quality_score"] < 50:
                    res["risk_tags"].append(
                        f"📉[赔率低]RR={_eq['risk_reward_ratio']:.1f} 评级{_ev_p4['trade_grade']}"
                    )
            except Exception as _e_aq4:
                logger.debug("[P4] 入场质量评估跳过: %s", _e_aq4)

            _obs_only = bool(tail_quality.get("observation_only"))
            _pos, _ladder = self._position_ladder(
                score=float(res.get("burst_score", 0.0) or 0.0),
                phase=phase,
                data_quality=tail_quality,
                penalty=float(res.get("penalty", 0.0) or 0.0),
                ev_grade=str(((_ev_p4 or {}).get("trade_grade", "")) or ""),
                layer_overheat=int(rt.get("_layer_overheat_score", 0) or 0),
                observation_only=_obs_only,
            )
            if _obs_only or not getattr(phase, "allow_new_entry", False):
                _pos = 0.0
                _ladder["ratio"] = 0.0
                _ladder.setdefault("reason", getattr(phase, "block_reason", "") or tail_quality.get("reason", ""))
            # 赔率不达标 → 仓位再砍一刀（保留原有纪律）
            if _eq is not None and (not _eq.get("pass_2_1")) and float(_eq.get("entry_quality_score", 0.0) or 0.0) < 50:
                _pos = min(_pos, 0.05)
                _ladder["ratio"] = round(float(_pos), 3)
                _ladder["odds_cut"] = True
            res["max_position_ratio"] = round(float(_pos), 3)
            res["position_ladder"] = _ladder

            # ---- 策略口径（保留原有小白友好文案）----
            _strat_hint = "尾盘信号偏强，次日开盘观察30分钟再决定，跌破昨日收盘价0.5%止损，站稳VWAP可加仓。"
            if any("P4-06" in h for h in hits):
                _strat_hint = ("【底仓趋势延续】20日线向上且收盘贴近20日线，趋势未破未追高。\n"
                               "次日若低开<1%可在20日线附近观察承接；高开>2%勿追，等回踩。\n"
                               "止损：跌破20日线收盘价0.5%出场。止盈：涨幅>8%可分批卖。")
            elif any("P4-07" in h or "连板" in h for h in hits):
                _strat_hint = ("【连板缩量整理】连板后缩量整理二次买点。\n"
                               "次日站稳MA5可加仓，回踩不破MA5继续持有。\n"
                               "止损：跌破MA5收盘价0.5%出场。止盈：连板预期>10%。")
            elif any("P4-08" in h or "金叉" in h for h in hits):
                _strat_hint = ("【双均线金叉确认】5日线上穿20日线，趋势由弱转多。\n"
                               "次日沿MA5持有；回踩MA5不破可加仓，跌破MA5收盘0.5%离场。\n"
                               "止盈：涨幅>6%分批；若MA5再度下穿MA20则清仓。")
            elif mainline_score >= 3.0:
                if next_day_confidence >= 1.5:
                    _strat_hint = ("【主线龙头】次日高开概率大，优先操作。\n"
                                   "开盘高开<2%可加仓；低开可在昨日收盘价附近补。\n"
                                   "止损：买入价下方1.5%。目标：3天内涨5-8%分批止盈。")
                elif sector_strength >= 1.10:
                    _strat_hint = ("【主线偏强】板块热度支撑，次日可期。\n"
                                   "次日开盘30分钟内站稳VWAP再加仓；跌破VWAP收盘减仓。\n"
                                   "目标：5日线附近减半。")
                else:
                    _strat_hint = ("【主线试探】信号偏中性，快进快出。\n"
                                   "次日站稳5日线再介入；止损：买入价下方2%。止盈：2天内3-5%走。")
            elif mainline_score <= -2.0:
                _strat_hint = ("【⚠️假强嫌疑】尾盘虚火，不宜重仓。\n"
                               "强烈建议观察为主，勿追高；若次日高开回落更要远离。\n"
                               "止损：买入即设1%。")
            elif vwap_penalty > 0 or tail_smart_penalty > 0:
                _strat_hint = ("【⚠️尾盘虚火】资金可能有派发嫌疑。\n"
                               "等次日真实站稳VWAP后再考虑入场，不急于追。\n"
                               "止损：严格控制在买入价下方1%内。")

            _t1_rules = [
                "次日开盘30分钟仍站不上昨收/VWAP → 先减半",
                "次日高开>2% 不追不加，等回踩今日收盘价附近",
                "19:30 P5 扫描若本票未再入池 → 次日开盘5分钟内清仓（尾盘诱多现原形）",
                "浮盈>5% 止损上移至成本价；浮盈>8% 止损上移至今日最低价*0.97",
            ]
            res["t1_exit_rules"] = _t1_rules

            if _obs_only:
                buy_hint = f"【仅观察】{tail_quality.get('reason', '尾盘数据不足')}；本信号不作为正式买入依据。"
            elif _pos <= 0:
                buy_hint = (
                    f"【不建仓】{_ladder.get('reason', '综合分或阶段不满足建仓条件')}；仅登记观察，不占用资金。\n"
                    f"⏱️{getattr(phase, 'label', '')}"
                )
            else:
                _head = (
                    f"⏱️买入窗口：{phase.window_text}\n"
                    f"📌下单方式：{phase.order_style}\n"
                    f"🎯本次仓位：{_pos * 100:.0f}%"
                    f"（基准{_ladder.get('base', 0.0) * 100:.0f}% × 阶段{_ladder.get('phase_mult', 0.0):.2f}"
                    f" × 分数{_ladder.get('score_mult', 0.0):.2f} × 数据{_ladder.get('grade_mult', 0.0):.2f}"
                    f" × 惩罚{_ladder.get('penalty_mult', 0.0):.2f} × EV{_ladder.get('ev_mult', 0.0):.2f}）\n"
                )
                buy_hint = _head + _strat_hint + "\n【T+1 纪律】\n" + "\n".join(f"· {r}" for r in _t1_rules)

            res["buy_hint"] = buy_hint
            res["entry_window"] = {
                "phase": getattr(phase, "key", ""),
                "window_text": getattr(phase, "window_text", ""),
                "order_style": getattr(phase, "order_style", ""),
                "allow_new_entry": bool(getattr(phase, "allow_new_entry", False)),
                "max_position_ratio": round(float(_pos), 3),
            }
            # 企微推送要短：长文案进 buy_hint，短码进 wechat_hint
            _w_name = str(rt.get("name", "") or "").strip()
            _w_stop = ""
            try:
                _sl = (_eq or {}).get("stop_loss")
                if isinstance(_sl, dict):
                    _w_stop = f"{float(_sl.get('stop_loss_price', 0.0) or 0.0):.2f}"
                elif _sl:
                    _w_stop = str(_sl)
            except Exception:
                _w_stop = ""
            res["wechat_hint"] = (
                f"P4尾盘|{_w_name}|分{float(res.get('burst_score', 0.0) or 0.0):.0f}"
                f"|仓{_pos * 100:.0f}%|{getattr(phase, 'window_text', '')}"
                + (f"|止损{_w_stop}" if _w_stop else "")
            )
            # 【V33 P4】把「仅观察」标记同步给 signal_log（is_observation 字段），
            # 让策略有效性统计不会把 14:30 前 / 冻结期的信号算进可执行样本。

            # 【V33.1 审计修复 · P0】风控惩罚已在仓位阶梯之前并入（见上方 merge 注释），
            # 这里只保留「致命否决」这一道：≥fatal_penalty_threshold 时清空战法/仓位/买入指引。
            # 放在最后，保证否决结论覆盖 buy_hint / wechat_hint / entry_window 的所有输出。
            res = self._apply_fatal_penalty_guard(res)

            # 【V33 P4】把「信号时点 / 尾盘阶段 / 惩罚 / 仓位 / 金门禁状态」写进 signal_log 的 entry_basis，
            # 这样将来做 T+1 标定时可以精确分桶（按 14:30–14:35 / 14:35–14:45 / 14:45–14:55 …），
            # 而不是靠 remark 文本猜测。字段已存在（entry_basis VARCHAR），不改表结构。
            # gate 口径：?pipeline 对 p4 的金门禁有「p4_core_screener_pass 直接豁免」规则，
            # 因此「门禁最终是否放行」在引擎内等价于 core_screener_pass 是否为真——
            # 两者一旦不同步（历史上 winner_floor 那次就是），就会出现「落库了但从未推送」的样本。
            # 现在把这一位显式记下来，标定工具据此剔除。
            try:
                _gate_tag = "pass" if bool(res.get("p4_core_screener_pass")) else "block"
                res["entry_basis"] = (
                    f"p4_tail|phase={getattr(phase, 'key', '')}|min={curr_min}"
                    f"|pen={float(res.get('penalty', 0.0) or 0.0):.1f}"
                    f"|pos={float(res.get('max_position_ratio', 0.0) or 0.0):.3f}"
                    f"|grade={str(tail_quality.get('grade', '') or '')}"
                    f"|gate={_gate_tag}"
                )
            except Exception:
                pass
            res["tail_phase_key"] = str(getattr(phase, "key", "") or "")

            # 【V33 P4】阶段门槛：14:30–14:35 与 14:45 之后要求更高的综合分才允许建仓。
            # merge_risk_into_engine_result 会用风控层建议覆盖 suggested_min_entry_score，
            # 所以这条增量必须放在 merge 之后，否则会被静默吃掉。
            _bar_delta = float(getattr(phase, "score_bar_delta", 0.0) or 0.0)
            if _bar_delta > 0:
                res["suggested_min_entry_score"] = round(
                    float(res.get("suggested_min_entry_score", 0.0) or 0.0) + _bar_delta, 2
                )
                res["detail"]["phase_score_bar_delta"] = _bar_delta

            # === V28 真实接入 signal_log ===
            # 【V33.1 审计修复 · P0】signal_log 落库与推送去重必须尊重「金门禁」结论。
            # 原实现是在 run_all 内部无条件落库，而 pipeline 的 golden gate
            # （strict_golden_burst_ok）是在 run_all 返回之后才判的：
            # 一个被门禁拦掉、从未进入推送列表的票，signal_log 里依然留了一行 p4 记录，
            # 而且 pool_apply_push_dedup 还替它写了「已推送」历史（导致后续真信号被冷却期压掉）。
            # 后果：T+N 回看、战法有效性、以及上一轮的尾盘阶段标定，
            # 都在把这些「从未成为信号」的记录当成真实样本统计。
            # 现在由 pipeline 先做门禁预判写入 rt["_golden_gate_pass"]：
            #   True  → 正常落库（entry_basis 记 gate=pass）
            #   False → 不落库、不写去重历史（entry_basis 记 gate=block 供排查）
            #   None  → 上游未预判（如离线脚本/回测），保持原行为（记 gate=unknown）
            _gate_pass_flag = bool(res.get("p4_core_screener_pass"))
            if _gate_pass_flag is False:
                res["_p4_signal_log_skipped"] = "golden_gate_blocked"
            else:
                try:
                    from core.pool_signal_adapter_v28 import append_signal_log_safe
                    # 【V30.10 修复 2026-10-21】_GLOBAL_DB_CONN 是全项目从未赋值的死变量，
                    # 恒为 None → P4 信号从不落库。改为 None 传入，append_signal_log_safe
                    # 内部会用 resolve_write_conn() 自动解析可用连接。
                    db_conn = None
                    try:
                        from core.push_template_momentum_reversal import apply_push_template_to_res
                        apply_push_template_to_res(res)
                    except Exception:
                        pass
                    try:
                        from core.push_dedup import pool_apply_push_dedup
                        pool_apply_push_dedup(res, "p4")
                    except Exception:
                        pass
                    # 【V30.10 阈值统一 2026-10-21】push_min_score 改为从 config.yaml 动态解析
                    # （core/push_threshold.resolve_push_min_score），不再硬编码。
                    from core.push_threshold import resolve_push_min_score
                    # 【Tier1 · 1.1 修复 2026-11-02】原调用漏传 rt → append_signal_log_safe 取不到
                    # 价格（price<=0 直接 return），P4 信号事实上从不落库（signal_log p4=0）。
                    # 与 P3 的既有正确写法对齐：把 rt 显式传入。不改任何打分/阈值。
                    append_signal_log_safe(res, "p4", db_conn,
                                            push_min_score=resolve_push_min_score("p4", for_log=True),
                                            rt=rt)
                except Exception:
                    pass

        except Exception as e:
            logging.debug(f"P4 run_all 异常: {e}")
            return res

        return res
