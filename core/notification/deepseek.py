# -*- coding: utf-8 -*-
"""
core/notification/deepseek.py
原 core/notification_gateway.py L62-L1307（DeepSeek 集成 + 战法知识库 + 字段别名）

【职责】
- 常量：_DEEPSEEK_FALLBACK_ADVICE / _DEEPSEEK_SYSTEM_PROMPT
  / _DEEPSEEK_POOL_FOCUS / _DEEPSEEK_POOL_SEMANTICS / _DEEPSEEK_TACTIC_KNOWLEDGE
  / _DEEPSEEK_FIELD_ALIASES / _DEEPSEEK_FINANCIAL_FIELDS
- 字段安全：_trim_deepseek_advice / _is_deepseek_empty_value / _deepseek_float
- 字段提取：_get_first_deepseek_value / _extract_tactic_keywords / _normalize_deepseek_financial
- 战法/语义上下文：_build_deepseek_tactic_context / _build_deepseek_pool_semantics / _deepseek_pick_float
- 派生字段：_enrich_deepseek_derived_fields
- 财报读取：_load_financial_snapshot_for_deepseek
- 主入口：_build_deepseek_stock_payload / _deepseek_ts_code_full / _append_deepseek_advice
- 错误分类：_classify_deepseek_error / _log_deepseek_analysis
- AI 入口：_maybe_get_deepseek_advice（带 fallback / 重试 / 思考模式）

【依赖】
- core.notification.core（_bj_now_str / _finite_float_or_none / _esc / _norm_ts_code）
- core.notification.format（_safe_score_display）
- core.config_manager.get_deepseek_analysis_config
- data.db_core（get_read_conn_singleton / table_exists）
"""
import hashlib
import json
import logging
import math
import time
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from core.notification.core import (
    _bj_now_str,
    _esc,
    _finite_float_or_none,
    _norm_ts_code,
)
from core.stock_name_utils import normalize_stock_display_name

logger = logging.getLogger(__name__)


# [V26.88+10-9 优化] DeepSeek 调用指标聚合（线程安全）—— 用于生产可观测性。
# 计数维度：model × outcome(success/truncate/fallback/error)
# 每 _METRICS_REPORT_INTERVAL 次调用聚合输出一次, 不污染正常日志。
import threading
_METRICS_LOCK = threading.Lock()
_METRICS: Dict[str, Dict[str, int]] = {}  # {model: {outcome: count}}
_METRICS_TOTAL_CALLS = 0
_METRICS_REPORT_INTERVAL = 200  # 每 200 次调用汇总一次
_METRICS_LATENCY_SUM_MS = 0.0   # 累计延迟, 用于算 p50 替代均值


def _record_deepseek_metric(model: str, outcome: str, latency_ms: float = 0.0) -> None:
    """记录一次 DeepSeek 调用结果。outcome ∈ {success, truncate, fallback, error, skip}"""
    global _METRICS_TOTAL_CALLS, _METRICS_LATENCY_SUM_MS
    with _METRICS_LOCK:
        m = _METRICS.setdefault(str(model or "unknown"), {})
        m[outcome] = m.get(outcome, 0) + 1
        _METRICS_TOTAL_CALLS += 1
        _METRICS_LATENCY_SUM_MS += max(0.0, float(latency_ms or 0.0))
        if _METRICS_TOTAL_CALLS % _METRICS_REPORT_INTERVAL == 0:
            avg_ms = _METRICS_LATENCY_SUM_MS / max(_METRICS_TOTAL_CALLS, 1)
            summary = " | ".join(
                f"{m_name}:{dict(sorted(counts.items()))}"
                for m_name, counts in sorted(_METRICS.items())
            )
            logger.info(
                "【DeepSeek指标】累计 %d 次调用 | 平均延迟 %.0fms | %s",
                _METRICS_TOTAL_CALLS, avg_ms, summary,
            )


def get_deepseek_metrics_snapshot() -> Dict[str, Any]:
    """对外暴露指标快照, 便于调试/告警。"""
    with _METRICS_LOCK:
        return {
            "total_calls": _METRICS_TOTAL_CALLS,
            "avg_latency_ms": round(_METRICS_LATENCY_SUM_MS / max(_METRICS_TOTAL_CALLS, 1), 1),
            "by_model": {k: dict(v) for k, v in _METRICS.items()},
        }


# DeepSeek 分析建议兜底与提示词
_DEEPSEEK_FALLBACK_ADVICE = "暂无法分析，按原策略观察。"
# [V26.88+10-9 修复] 模型描述从硬编码改为动态生成, 避免切到非官方模型时系统提示词自我描述错误。
# [V26.88+10-9 按官方文档修正] 仅保留官方当前支持的两个主力模型：
# - deepseek-flash: V4.1-Flash，非思考模式，默认推荐
# - deepseek-v4-pro: V4-Pro，思考模式可用，主力生产模型
_DEEPSEEK_MODEL_DESC = {
    "deepseek-v4-pro": "DeepSeek-V4-Pro（deepseek-v4-pro），主力生产模型，支持思考模式与多模态，适合复杂金融推理。",
    "deepseek-flash": "DeepSeek-V4.1-Flash（deepseek-flash），非思考模式，吞吐快、价格低。",
}
_DEEPSEEK_SYSTEM_PROMPT = (
    "你是一位顶级交易大师，请从上帝视角分析以下股票信息，判断是否值得买入。"
    "【模型版本】你正在使用 {{MODEL_DESC}}"
    "【输入结构说明】你会收到：池子类型(p2=竞价/p3=盘中/p4=尾盘/p5=盘后)、池子语义与核心问题、战法知识库、股票结构化数据（含综合分/涨幅/量比/主力净流入率/主线分等）。"
    "【分析要求】1. 只返回一句话，50个字以内；2. 口吻像交易大师；3. 明确表达买入、观望或放弃；4. 不要解释过程。"
    "【判断权重】趋势>量能>资金>位置>信号质量>财报。宁可错过，不可乱买。涨幅>7%且量比>5需警惕；主力净流入为负需谨慎；主线分为负表示不在主线上。"
)


def _build_system_prompt(model_name: str) -> str:
    """根据实际模型名生成对应的 system prompt, 避免模型自我认知错误。"""
    desc = _DEEPSEEK_MODEL_DESC.get(model_name, f"DeepSeek 模型（{model_name}）")
    return _DEEPSEEK_SYSTEM_PROMPT.replace("{{MODEL_DESC}}", desc)

_DEEPSEEK_POOL_FOCUS = {
    "p2": "P2竞价池：重点看竞价/开盘强度、实时涨幅、实时量比、实时换手、主力净流入、是否站上VWAP与入池理由。",
    "p3": "P3盘中池：重点看盘中承接、趋势延续、实时量比、主力净流入、VWAP、MA20、MACD柱、支撑压力。",
    "p4": "P4尾盘池：重点看尾盘是否站稳VWAP、收盘承接、实时量比、主力净流入、压力位、市场情绪与次日溢价。",
    "p5": "P5盘后池：重点看盘后复盘、次日预案、财报摘要/风险、营收净利趋势、行业概念主线、战法与入池理由。",
}

# [V26.88+10-9 优化] 分池字段白名单 —— 业务语义对齐:
# P2 竞价池 (9:15-9:25 早盘) → 重实时/竞价量价, 砍长周期
# P3 盘中池 (10:00-14:00 持股决策) → 重 VWAP/趋势/资金持续性
# P4 尾盘池 (14:30+ 次日溢价) → 重尾盘承接/情绪/T1胜率
# P5 盘后池 (盘后 复盘+次日预案) → 重战法知识+财报+主线
# 解决: (1) P5 兜底池 2294 字符超 max_tokens=600 截断
#       (2) pct_chg/volume_ratio 实时静态双写模型读不出差异
#       (3) 所有池子字段一刀切, 浪费 token
# 通用字段: market_pulse / market_state / peer_memory 4 池共享, 不在白名单
_DEEPSEEK_POOL_FIELDS = {
    # P2 早盘竞价: 实时优先, 砍长周期, 不带财报(来不及看)
    "p2": {
        "core": {"code", "name", "price", "tactic", "entry_reason", "score",
                 "pct_chg", "volume_ratio", "realtime_above_vwap", "price_vs_vwap_pct",
                 "net_main_amount", "realtime_main_inflow_rate", "k_shape",
                 "mainline_score", "sector_strength", "t1_win_rate_pct", "t1_avg_ret_pct"},
        "allow": {"ma5", "ma20", "vwap", "macd_dif", "macd_bar", "t1_memory_boost",
                  "market_emotion", "top_industry", "top_concept"},
        "financial": False,  # [V26.88+10-9 订正] P2 竞价来不及读财报, 改 False
        "tactic_ctx": True,
    },
    # P3 盘中: 趋势要素 + 板块强度, 主力净流入持续性
    "p3": {
        "core": {"code", "name", "price", "tactic", "entry_reason", "score",
                 "pct_chg", "volume_ratio", "realtime_above_vwap", "price_vs_vwap_pct",
                 "net_main_amount", "realtime_main_inflow_rate",
                 "ma5", "ma20", "vwap", "macd_dif", "macd_bar",
                 "sector_strength", "mainline_score", "t1_win_rate_pct"},
        "allow": {"market_emotion", "top_industry", "t1_avg_ret_pct", "raw_tactics",
                  "ma10", "ma60", "amount", "p3_screener", "VWAP偏离度"},
        "financial": False,  # 盘中无需财报
        "tactic_ctx": True,
    },
    # P4 尾盘: 收敛到"今天发生什么 + 明天溢价空间"
    "p4": {
        "core": {"code", "name", "price", "tactic", "entry_reason", "score",
                 "pct_chg", "volume_ratio", "realtime_above_vwap", "price_vs_vwap_pct",
                 "net_main_amount", "market_emotion", "mainline_reason",
                 "t1_win_rate_pct", "t1_avg_ret_pct"},
        "allow": {"ma5", "ma20", "vwap", "t1_sample_n"},
        "financial": False,
        "tactic_ctx": True,
    },
    # P5 盘后: 战法知识 + 财报 + 主线, 需完整信息但需压缩
    "p5": {
        "core": {"code", "name", "price", "pct_chg", "tactic", "entry_reason", "score",
                 "t1_win_rate_pct", "t1_avg_ret_pct", "t1_sample_n",
                 "mainline_reason", "top_industry", "top_concept",
                 "market_emotion", "sector_strength"},
        "allow": {"volume_ratio", "ma5", "ma20", "t1_memory_score", "t1_memory_boost"},
        "financial": True,  # [V26.88+10-9 订正] P5 盘后, 财报重要
        "tactic_ctx": True,  # [V26.88+10-9 订正] P5 盘后, 战法知识库重要
    },
}

_DEEPSEEK_POOL_SEMANTICS = {
    "p2": {
        "name": "竞价突袭",
        "time_window": "集合竞价至开盘初段",
        "core_question": "竞价强度是否真实、开盘是否有第一波参与价值",
        "buy_bias": "只认可温和高开、放量但不过度透支、资金主动且开盘能站稳VWAP的票",
        "avoid": "高开过猛、量比虚胖、开盘跌破VWAP、昨日已高潮、财报硬伤",
    },
    "p3": {
        "name": "盘中防守",
        "time_window": "盘中承接与趋势延续阶段",
        "core_question": "盘中承接是否有效，是否适合低吸或跟随",
        "buy_bias": "偏好回踩不破MA20/VWAP、资金回流、MACD柱改善、支撑清晰的趋势延续票",
        "avoid": "冲高回落、放量滞涨、跌破关键支撑、主力净流出、市场情绪退潮",
    },
    "p4": {
        "name": "尾盘盲狙",
        "time_window": "尾盘确认与次日溢价预判阶段",
        "core_question": "尾盘是否站稳并具备次日溢价",
        "buy_bias": "偏好尾盘站稳VWAP/关键均线、资金未撤、压力位不远、市场情绪配合的票",
        "avoid": "尾盘偷袭无量、临近压力却无资金、全天弱于VWAP、情绪转弱",
    },
    "p5": {
        "name": "盘后真龙",
        "time_window": "收盘后复盘与次日预案阶段",
        "core_question": "今日结构是否值得加入次日观察/预案",
        "buy_bias": "偏好趋势健康、量价资金共振、财报无硬伤、行业概念有主线属性的票",
        "avoid": "单日情绪高潮、财报风险明显、业绩下滑且无主线、位置过高无承接",
    },
}

_DEEPSEEK_TACTIC_KNOWLEDGE = {
    "竞价": {
        "meaning": "开盘前后资金抢筹或试盘信号，核心看高开质量与量能真实性。",
        "confirm": "实时涨幅温和、量比放大、换手有效、开盘不破VWAP、主力净流入为正。",
        "risk": "高开过猛或量比虚胖容易冲高回落，若财报风险叠加应降级观望。",
    },
    "主升": {
        "meaning": "趋势进入加速或延续阶段，核心看均线多头、MA20斜率和资金持续性。",
        "confirm": "价格强于MA20/MA60，MACD柱不弱，量能温和放大，回踩支撑不破。",
        "risk": "涨幅过高、远离MA20或爆量滞涨时，追买性价比下降。",
    },
    "机构": {
        "meaning": "机构/大资金连续介入迹象，核心看资金净额与承接稳定性。",
        "confirm": "主力净额或机构净买入为正，成交额放大但不失控，尾盘不明显撤单。",
        "risk": "龙虎榜或大单资金若只是一日脉冲，次日承接不足时容易兑现。",
    },
    "苍穹": {
        "meaning": "高强度突破或强趋势结构，通常要求筹码/趋势/量能共振。",
        "confirm": "价格站稳关键均线或平台，获利盘承接健康，量比有效但不过热。",
        "risk": "位置过高且获利盘过重时，需防高位派发。",
    },
    "底仓": {
        "meaning": "适合观察或小仓试错的中低位资金点火，重视安全边际。",
        "confirm": "低位放量、MA20改善、主力资金转正、风险标签较少。",
        "risk": "若没有持续资金或行业主线，容易一日游。",
    },
    "尾盘": {
        "meaning": "收盘前资金选择方向，核心判断次日溢价概率。",
        "confirm": "尾盘站稳VWAP/均线，成交额不萎缩，主力资金未明显流出。",
        "risk": "尾盘偷袭无全天承接，次日低开概率较高。",
    },
    "真龙": {
        "meaning": "盘后综合强势候选，要求趋势、资金、题材和基本面至少三项共振。",
        "confirm": "趋势健康、量价配合、资金强、行业/概念靠前、财报无明显硬伤。",
        "risk": "若只是单日涨幅或情绪驱动，缺乏财报和资金确认，应降低评级。",
    },
    "缩量": {
        "meaning": "缩量期备选通常不是立即追买，而是观察承接和二次放量。",
        "confirm": "缩量不破关键支撑，次日重新放量且站回VWAP/MA20。",
        "risk": "无量反弹持续性弱，宜观望等待确认。",
    },
    "直通车": {
        "meaning": "系统强信号快速入池，通常已有硬过滤通过，但仍需判断当下性价比。",
        "confirm": "高分、资金、趋势、入池理由一致，且不存在明显财报/位置风险。",
        "risk": "强信号不等于无脑买，若涨幅和量比透支仍应观望。",
    },
    "低吸": {
        "meaning": "在趋势未破坏时利用回踩买入，核心不是追强，而是买承接和安全边际。",
        "confirm": "回踩MA20/VWAP/平台支撑不破，缩量回落后资金回流，分时重新站回均价线。",
        "risk": "弱势下跌中的低吸容易变接刀，若主力净流出或支撑失守应放弃。",
    },
    "弱转强": {
        "meaning": "前一阶段分歧或弱势后，次日/盘中主动转强，核心看资金是否重新定价。",
        "confirm": "高开不炸、放量过前高、站稳VWAP，主力资金由负转正或明显回流。",
        "risk": "只高开不放量、开盘快速回落或板块不配合，容易是假弱转强。",
    },
    "反包": {
        "meaning": "用强阳线修复上一交易日阴线或分歧，核心看是否完成情绪修复。",
        "confirm": "放量收复前阴实体/关键位，收盘接近高位，主力净流入配合。",
        "risk": "反包不过关键压力或尾盘回落，说明修复失败，次日兑现压力大。",
    },
    "N字": {
        "meaning": "上涨—回踩—再上攻的趋势延续结构，核心看第二段上攻质量。",
        "confirm": "回踩不破前平台/MA20，再次放量突破前高，量价资金同步改善。",
        "risk": "第二段上攻缩量或未过前高，容易形成双头。",
    },
    "首阴": {
        "meaning": "强势上涨后的第一根阴线，关注是否是健康分歧而非趋势结束。",
        "confirm": "阴线缩量、未跌破关键均线/平台，次日快速修复并有资金承接。",
        "risk": "首阴放量长阴、跌破MA20或主力大幅流出，多为退潮信号。",
    },
    "二波": {
        "meaning": "第一轮上涨休整后再启动，核心看筹码消化和新资金接力。",
        "confirm": "调整期间不破中枢，再次放量突破平台，行业/概念仍有主线热度。",
        "risk": "二波失败通常杀伤大，若突破无量或财报/位置不支持应谨慎。",
    },
    "趋势加速": {
        "meaning": "原有趋势进入斜率提升阶段，适合强趋势跟随但不适合无脑追高。",
        "confirm": "MA20斜率抬升，价格沿MA5/MA10上行，量能温和放大且资金持续流入。",
        "risk": "加速末端若远离MA20、量比极高或尾盘松动，容易次日大分歧。",
    },
    "平台突破": {
        "meaning": "横盘整理后突破箱体，核心看突破是否有效和是否带量。",
        "confirm": "放量突破平台上沿，收盘站稳，回踩不破突破位，资金同步放大。",
        "risk": "假突破常见于缩量上穿或冲高回落，若未站稳平台上沿应观望。",
    },
    "回封": {
        "meaning": "炸板或分歧后重新封回，核心看分歧后的资金承接强度。",
        "confirm": "开板后快速回封，封单/成交额健康，板块未明显退潮。",
        "risk": "反复炸板、回封无力或尾盘漏单，次日低开风险高。",
    },
    "卡位": {
        "meaning": "同题材或同梯队中替代弱者成为资金前排，核心看相对强度。",
        "confirm": "强于板块和同梯队，率先回封/突破，量价资金明显优于被卡对象。",
        "risk": "若只是短暂抢跑而板块整体走弱，卡位容易失败。",
    },
    "分歧转一致": {
        "meaning": "市场或个股经历分歧后资金重新统一方向，是强势股二次确认信号。",
        "confirm": "分歧阶段承接不破位，随后放量拉回高位或回封，资金和板块同步回暖。",
        "risk": "一致过快且量能过度，容易一致后再分歧；高位需防兑现。",
    },
    "突破": {
        "meaning": "突破关键价格、均线或平台，核心看是否有成交量与资金确认。",
        "confirm": "突破后站稳关键位，成交额放大，主力净流入为正，回踩不破。",
        "risk": "无量突破、假突破或突破临近强压力位，追买风险大。",
    },
    "回踩": {
        "meaning": "上涨途中的技术性回落，核心看支撑是否有效。",
        "confirm": "回踩MA20/VWAP/平台缩量企稳，随后资金回流或重新放量。",
        "risk": "放量跌破支撑说明趋势转弱，不应按回踩处理。",
    },
    "承接": {
        "meaning": "卖压出现时仍有资金接住，核心判断筹码和资金是否稳定。",
        "confirm": "分时回落不破VWAP，低点抬高，主力净流入或成交额维持。",
        "risk": "承接失败表现为跌破均价线后无法收回，或尾盘资金撤退。",
    },
    "放量": {
        "meaning": "资金参与度提升，必须结合位置判断是攻击还是派发。",
        "confirm": "放量同时价格上行、站稳关键位、主力净流入同步为正。",
        "risk": "高位放量滞涨或长上影，多为派发风险。",
    },
    "缩量回踩": {
        "meaning": "趋势内健康整理形态，核心看缩量是否守住关键位。",
        "confirm": "成交量低于近期均量，价格不破MA20/平台，次日或盘中重新放量。",
        "risk": "缩量只是无人接力时，若后续不放量确认，仍应观望。",
    },
    "首板": {
        "meaning": "第一根涨停或首次强确认，核心看题材新鲜度和封板质量。",
        "confirm": "首板放量适中、封单稳定、板块有助攻、开板次数少。",
        "risk": "无题材孤立首板、尾盘勉强封板或封单弱，次日溢价不稳。",
    },
    "连板": {
        "meaning": "连续涨停的情绪强势结构，核心看高度与分歧承接。",
        "confirm": "连板高度提升但换手健康，分歧后能快速回封，板块情绪不退潮。",
        "risk": "高位连板一旦炸板杀伤大，量比过高或监管风险需降级。",
    },
    "龙头": {
        "meaning": "题材或市场最强核心标的，核心看带动性与辨识度。",
        "confirm": "涨幅、成交额、封板/趋势强度领先同板块，并能带动后排。",
        "risk": "龙头高位退潮时回撤剧烈，若后排掉队且自身放量滞涨要谨慎。",
    },
}

_DEEPSEEK_FIELD_ALIASES = {
    "code": ("代码", "code", "ts_code", "symbol"),
    "name": ("名称", "name", "stock_name", "证券简称", "股票简称"),
    "price": ("现价", "price", "close", "latest_price"),
    "pct_chg": ("涨幅", "pct_chg", "pct", "change_pct"),
    "realtime_pct_chg": ("realtime_pct_chg", "实时涨幅", "rt_pct_chg"),
    "amount": ("amount", "成交额", "成交金额"),
    "volume_ratio": ("volume_ratio", "量比", "量比_昨"),
    "realtime_volume_ratio": ("realtime_volume_ratio", "实时量比", "rt_volume_ratio"),
    "ma5": ("ma5", "MA5"),
    "ma10": ("ma10", "MA10"),
    "ma20": ("ma20", "MA20"),
    "ma60": ("ma60", "MA60"),
    "vwap": ("vwap", "VWAP"),
    "macd_dif": ("macd_dif", "DIF", "dif"),
    "macd_dea": ("macd_dea", "DEA", "dea"),
    "macd_bar": ("macd_bar", "MACD柱", "macd"),
    "k_shape": ("k_shape", "K线形态", "kline_shape"),
    "tactic": ("战法", "tactic", "strategy"),
    "tactic_code": ("tactic_code", "战法代码", "strategy_code"),
    "tactic_name": ("tactic_name", "战法名称", "strategy_name"),
    "entry_reason": ("入池理由", "买入原因", "why_selected", "selection_reason", "entry_reason"),
    "buy_hint": ("买入提示", "wechat_hint", "buy_hint"),
    "risk": ("风险标签", "risk", "risk_tags"),
    "score": ("综合分", "score", "burst_score", "p1_score"),
    "pool_tier": ("pool_tier",),
    "pool_source": ("pool_source",),
    "sector_strength": ("sector_strength", "板块强度"),
    "sector_rank": ("sector_rank", "板块排名"),
    "sector_total": ("sector_total", "板块总数"),
    "sector_beta": ("sector_beta", "板块贝塔"),
    "hot_sector_bonus": ("hot_sector_bonus", "热点板块加分"),
    "board_bonus": ("board_bonus", "板块加成"),
    "board_beta": ("board_beta", "板块beta", "board_beta"),
    "mainline_score": ("mainline_score", "主线分"),
    "mainline_reason": ("mainline_reason", "主线理由"),
    "net_main_amount": ("net_main_amount", "主力净额", "main_net_amount", "main_net"),
    "realtime_main_inflow_rate": ("realtime_main_inflow_rate", "实时主力净流入占比", "main_inflow_rate"),
    "t1_memory_score": ("t1_memory_score", "t1记忆分"),
    "t1_memory_boost": ("t1_memory_boost", "t1加成"),
    "t1_win_rate_pct": ("t1_win_rate_pct", "T1胜率"),
    "t1_avg_ret_pct": ("t1_avg_ret_pct", "T1平均收益"),
    "t1_sample_n": ("t1_sample_n", "T1样本数"),
    "market_emotion": ("market_emotion", "市场情绪"),
    "top_industry": ("top_industry", "主线行业", "industry"),
    "top_concept": ("top_concept", "主线概念", "concept"),
    "risk_level": ("风险等级", "risk_level"),
    "market_status": ("市场状态", "market_status"),
    "primary_action": ("主动作", "primary_action"),
    "尾盘无量拉升": ("尾盘无量拉升",),
    "T1收割风险": ("T1收割风险",),
    "跟风拉升嫌疑": ("跟风拉升嫌疑",),
    "deepseek_fin_text": ("deepseek_fin_text", "财报摘要"),
    "annual_report": ("annual_report", "年报"),
    "quarter_report": ("quarter_report", "季报"),
    "financial": ("financial", "财报", "finance"),
}

_DEEPSEEK_FINANCIAL_FIELDS = (
    "summary_text",
    "risk_flags",
    "report_type",
    "report_period",
    "revenue_yoy",
    "net_profit_yoy",
    "deduct_net_profit_yoy",
    "op_cash_flow",
    "asset_liab_rate",
    "goodwill",
    "accounts_receivable",
    "inventories",
)


def _trim_deepseek_advice(raw: Any) -> str:
    """清洗 DeepSeek 返回，只保留一句简短建议。
    【V26.88+ 9-19】从 30 字放宽到 50 字，对齐 V4.1-Flash 的新版 system prompt 限长。
    """
    try:
        text = str(raw or "").strip()
    except Exception:
        return _DEEPSEEK_FALLBACK_ADVICE
    if not text:
        return _DEEPSEEK_FALLBACK_ADVICE
    text = text.replace("\r", " ").replace("\n", " ")
    for prefix in ("【DeepSeek分析建议】", "DeepSeek分析建议", "建议：", "分析建议："):
        if text.startswith(prefix):
            text = text[len(prefix):].strip(" ：:　")
    text = " ".join(text.split())
    if not text:
        return _DEEPSEEK_FALLBACK_ADVICE
    if len(text) > 50:
        text = text[:50].rstrip("，。；、,.!！?？ ")
    return text or _DEEPSEEK_FALLBACK_ADVICE


def _build_deepseek_compact_hint(pool_key: str, stock_dict: Dict[str, Any]) -> str:
    """[V26.88+10-8 修复] 2nd 重试的"极简票面": 只挑 6 个最影响判断的字段, 拼成短字符串.
    设计原则:
    - 单行, 短 (<= 200 字符), 模型一眼能消化
    - 数字类字段直接转 str (避免 JSON 转义符占用 token)
    - 文本类字段截断到 20 字内
    """
    if not isinstance(stock_dict, dict):
        stock_dict = {}
    parts: List[str] = []
    code = _deepseek_ts_code_full(stock_dict) or stock_dict.get("代码") or stock_dict.get("code") or "?"
    parts.append(f"代码={code}")
    name = stock_dict.get("名称") or stock_dict.get("name") or stock_dict.get("stock_name")
    if name:
        parts.append(f"名称={str(name)[:8]}")
    score = _finite_float_or_none(stock_dict.get("综合分") or stock_dict.get("score") or stock_dict.get("burst_score"))
    if score is not None:
        parts.append(f"综合分={score:.1f}")
    pct = _finite_float_or_none(
        stock_dict.get("realtime_pct_chg") or stock_dict.get("实时涨幅")
        or stock_dict.get("pct_chg") or stock_dict.get("涨幅")
    )
    if pct is not None:
        parts.append(f"涨幅={pct:+.2f}%")
    vr = _finite_float_or_none(
        stock_dict.get("realtime_volume_ratio") or stock_dict.get("实时量比")
        or stock_dict.get("volume_ratio") or stock_dict.get("量比")
    )
    if vr is not None:
        parts.append(f"量比={vr:.2f}")
    tactic = stock_dict.get("tactic") or stock_dict.get("战法") or stock_dict.get("strategy")
    if tactic:
        parts.append(f"战法={str(tactic)[:12]}")
    pool_focus = _DEEPSEEK_POOL_FOCUS.get(str(pool_key or "").lower(), "")
    if pool_focus:
        parts.append(f"侧重={pool_focus[:18]}")
    return " | ".join(parts) if parts else f"代码={code}"


def _is_deepseek_empty_value(val: Any) -> bool:
    if val is None:
        return True
    if isinstance(val, float) and (math.isnan(val) or math.isinf(val)):
        return True
    if isinstance(val, str) and val.strip().lower() in ("", "--", "none", "null", "nan", "暂无"):
        return True
    return False


def _get_first_deepseek_value(stock_dict: Dict[str, Any], aliases: Any) -> Any:
    """[V26.88+10-9 修复] 从顶层、detail、_hit_res_full、_hit_res_full.detail 找字段。
    之前只查顶层 + detail + detail.*, 漏掉 P3 push 链路里 _hit_res_full 嵌套层
    (pipeline.py:1356 把 hit_res 整体塞进 b["_hit_res_full"], 里面 detail.入池理由
    / detail.mainline_score / detail.sector_strength 等 P3 关键字段全部看不到)。
    """
    for key in aliases:
        if key in stock_dict:
            val = stock_dict.get(key)
            if not _is_deepseek_empty_value(val):
                return val
    detail = stock_dict.get("detail")
    if isinstance(detail, dict):
        for key in aliases:
            if key in detail:
                val = detail.get(key)
                if not _is_deepseek_empty_value(val):
                    return val
        for sub in detail.values():
            if not isinstance(sub, dict):
                continue
            for key in aliases:
                if key in sub:
                    val = sub.get(key)
                    if not _is_deepseek_empty_value(val):
                        return val
    # [V26.88+10-9 修复] 新增: 查 _hit_res_full (P3 链路) + _hit_res_full.detail
    hit_res = stock_dict.get("_hit_res_full")
    if isinstance(hit_res, dict):
        for key in aliases:
            if key in hit_res:
                val = hit_res.get(key)
                if not _is_deepseek_empty_value(val):
                    return val
        hit_detail = hit_res.get("detail")
        if isinstance(hit_detail, dict):
            for key in aliases:
                if key in hit_detail:
                    val = hit_detail.get(key)
                    if not _is_deepseek_empty_value(val):
                        return val
            for sub in hit_detail.values():
                if not isinstance(sub, dict):
                    continue
                for key in aliases:
                    if key in sub:
                        val = sub.get(key)
                        if not _is_deepseek_empty_value(val):
                            return val
    return None


def _deepseek_float(val: Any) -> Optional[float]:
    if val is None:
        return None
    try:
        if isinstance(val, str):
            val = val.strip().replace("%", "").replace(",", "")
            if val in ("", "--"):
                return None
        x = float(val)
        if math.isnan(x) or math.isinf(x):
            return None
        return x
    except (TypeError, ValueError):
        return None


def _extract_tactic_keywords(tactic_text: Any, entry_reason: Any = None) -> List[str]:
    text = f"{str(tactic_text or '')} {str(entry_reason or '')}".strip().replace("_", " ")
    if not text:
        return []
    alias_map = {
        "低位吸筹": "低吸", "低吸": "低吸",
        "弱转强": "弱转强", "转强": "弱转强",
        "反包": "反包", "N字": "N字", "N型": "N字",
        "首阴": "首阴", "二波": "二波", "二次上攻": "二波",
        "趋势加速": "趋势加速", "加速": "趋势加速",
        "平台突破": "平台突破", "突破平台": "平台突破",
        "回封": "回封", "卡位": "卡位",
        "分歧转一致": "分歧转一致", "分歧一致": "分歧转一致", "一致转强": "分歧转一致",
        "竞价": "竞价", "主升": "主升", "机构": "机构", "苍穹": "苍穹", "底仓": "底仓",
        "尾盘": "尾盘", "真龙": "真龙", "缩量": "缩量", "缩量回踩": "缩量回踩",
        "直通车": "直通车", "突破": "突破", "回踩": "回踩", "承接": "承接",
        "放量": "放量", "首板": "首板", "连板": "连板", "龙头": "龙头",
    }
    keys: List[str] = []
    seen = set()
    for alias, canon in sorted(alias_map.items(), key=lambda x: len(x[0]), reverse=True):
        if alias in text and canon not in seen:
            seen.add(canon)
            keys.append(canon)
    return keys[:6]


def _build_deepseek_tactic_context(clean: Dict[str, Any], stock_dict: Dict[str, Any]) -> Dict[str, Any]:
    """[V26.88+10-9 优化] 战法上下文压缩: 3 字段→1 行, 节省 ~60% 字符"""
    tactic = clean.get("tactic") or stock_dict.get("战法") or stock_dict.get("tactic")
    entry_reason = clean.get("entry_reason") or stock_dict.get("入池理由") or stock_dict.get("entry_reason")
    kws = _extract_tactic_keywords(tactic, entry_reason)
    if not kws:
        return {}
    out: Dict[str, Any] = {"matched_keywords": kws}
    # 压缩: meaning/confirm/risk 拼接成单行, 避免 dict 嵌套带来的 token 开销
    tactic_lines: List[str] = []
    for kw in kws:
        meta = _DEEPSEEK_TACTIC_KNOWLEDGE.get(kw)
        if meta:
            # 单行格式: "战法名 | 要点 | 风险"
            line = f"{kw} | {meta.get('meaning', '')} | 风险:{meta.get('risk', '')}"
            tactic_lines.append(line[:120])  # 截断, 单条最长 120 字符
    if tactic_lines:
        out["knowledge"] = " || ".join(tactic_lines)
    return out


def _build_deepseek_pool_semantics(pool_key: str) -> Dict[str, Any]:
    pk = str(pool_key or "").strip().lower()
    meta = _DEEPSEEK_POOL_SEMANTICS.get(pk, {})
    return dict(meta) if isinstance(meta, dict) else {}


def _deepseek_pick_float(clean: Dict[str, Any], *keys: str) -> Optional[float]:
    for key in keys:
        x = _deepseek_float(clean.get(key))
        if x is not None:
            return x
    return None


def _enrich_deepseek_derived_fields(pool_key: str, clean: Dict[str, Any], stock_dict: Dict[str, Any]) -> None:
    """为 DeepSeek 补充高价值派生字段"""
    score = _deepseek_pick_float(clean, "score", "综合分")
    if score is None:
        score = _finite_float_or_none(
            stock_dict.get("综合分") or stock_dict.get("score") or stock_dict.get("burst_score")
        )
    if score is not None:
        clean.setdefault("score", round(float(score), 2))

    tactic = str(clean.get("tactic") or "").strip()
    if tactic and "tactic_name" not in clean:
        try:
            from core.strategies.strategy_name_alias import get_display_name
            _name = get_display_name(tactic)
            clean["tactic_name"] = _name if _name != tactic else tactic.split("_")[-1].replace("[", "").replace("]", "")[:40]
        except Exception:
            clean["tactic_name"] = tactic.split("_")[-1].replace("[", "").replace("]", "")[:40]
    if tactic and "tactic_code" not in clean:
        prefix = tactic.split("_", 1)[0].strip()
        if prefix and any(ch.isdigit() for ch in prefix):
            clean["tactic_code"] = prefix[:20]

    # 价格 vs VWAP
    price = _deepseek_pick_float(clean, "price")
    vwap = _deepseek_pick_float(clean, "vwap")
    if price is not None and vwap not in (None, 0.0):
        if "realtime_above_vwap" not in clean:
            clean["realtime_above_vwap"] = bool(price >= float(vwap))
        if "price_vs_vwap_pct" not in clean:
            clean["price_vs_vwap_pct"] = round((price / float(vwap) - 1.0) * 100.0, 2)

    # 价格 vs MA20
    ma20 = _deepseek_pick_float(clean, "ma20")
    if price is not None and ma20 not in (None, 0.0):
        if "price_vs_ma20_pct" not in clean:
            clean["price_vs_ma20_pct"] = round((price / float(ma20) - 1.0) * 100.0, 2)

    # 主力净流入率
    amount = _deepseek_pick_float(clean, "amount")
    main_net = _deepseek_float(
        stock_dict.get("net_main_amount")
        or stock_dict.get("主力净额")
        or stock_dict.get("main_net_amount")
        or stock_dict.get("main_net")
    )
    if main_net is not None and amount not in (None, 0.0):
        if "realtime_main_inflow_rate" not in clean:
            clean["realtime_main_inflow_rate"] = round(float(main_net) / float(amount) / 1e8 * 100.0, 2)
        if "net_main_amount" not in clean:
            clean["net_main_amount"] = round(float(main_net), 2)

    # 风险兜底
    if "risk" not in clean:
        pct = _deepseek_pick_float(clean, "pct_chg", "realtime_pct_chg")
        vr = _deepseek_pick_float(clean, "realtime_volume_ratio", "volume_ratio")
        risks = []
        if pct is not None and pct >= 7.0:
            risks.append("涨幅较大(>7%)，防冲高回落")
        if vr is not None and vr >= 5.0:
            risks.append("量比过高(>5)，防情绪透支")
        clean["risk"] = "；".join(risks) if risks else "未见明显硬风险"

    # 市场情绪兜底
    pk = str(pool_key or "").lower()
    if "market_emotion" not in clean:
        if pk == "p5":
            clean["market_emotion"] = "盘后复盘环境待人工确认"
        else:
            clean["market_emotion"] = "实时市场环境未入库，按个股信号优先"


def _normalize_deepseek_financial(raw: Any) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    if isinstance(raw, dict):
        for key in _DEEPSEEK_FINANCIAL_FIELDS:
            val = raw.get(key)
            if not _is_deepseek_empty_value(val):
                out[key] = val
    elif not _is_deepseek_empty_value(raw):
        out["summary_text"] = raw
    return out


def _load_financial_snapshot_for_deepseek(code_like: Any) -> Dict[str, Any]:
    """从 fact_financial_reports 读取最近财报快照"""
    ts_code = str(code_like or "").strip().upper()
    if not ts_code:
        return {}
    if "." not in ts_code:
        s6 = _norm_ts_code(ts_code)
        if s6.startswith("6"):
            ts_code = f"{s6}.SH"
        elif s6.startswith(("8", "4")):
            ts_code = f"{s6}.BJ"
        else:
            ts_code = f"{s6}.SZ"
    try:
        from data.db_core import get_read_conn_singleton, table_exists

        if not table_exists("fact_financial_reports"):
            return {}
        con = get_read_conn_singleton(max_wait_sec=5.0)
        if con is None:
            return {}
        q = """
            SELECT *
            FROM fact_financial_reports
            WHERE ts_code = ?
            ORDER BY COALESCE(ann_date, '') DESC, COALESCE(end_date, '') DESC
            LIMIT 2
        """
        df = con.execute(q, [ts_code]).fetchdf()
    except Exception as e:
        logger.debug("读取财报增强表失败 ts_code=%s: %s", ts_code, e)
        return {}
    if df is None or df.empty:
        return {}
    rows = df.to_dict(orient="records")
    latest = rows[0] if rows else {}
    annual = next((r for r in rows if str(r.get("report_type") or "") == "annual"), latest)
    quarter = latest
    fin = {k: latest.get(k) for k in _DEEPSEEK_FINANCIAL_FIELDS if not _is_deepseek_empty_value(latest.get(k))}
    return {
        "deepseek_fin_text": latest.get("summary_text") or "",
        "annual_report": annual.get("summary_text") or latest.get("summary_text") or "",
        "quarter_report": quarter.get("summary_text") or latest.get("summary_text") or "",
        "financial": fin,
    }


def _compress_deepseek_financial_to_text(clean: Dict[str, Any], stock_dict: Dict[str, Any]) -> Dict[str, Any]:
    """[V26.88+10-9 优化] 财报以文为主: 把 yoy/roe/gross_margin 等数字拼成一句话, 节省 ~50% 字符
    返回: {"text": "营收+15%, 净利润+18%, 扣非+17%, 毛利率91.5%, ROE22%, 资产负载35%",
           "summary": "Q3 营收净利润双增, 财务质量优秀"}
    """
    # 合并来源: clean["financial"] 优先, 否则从 stock_dict 顶层 _DEEPSEEK_FINANCIAL_FIELDS 取
    financial = clean.get("financial") if isinstance(clean.get("financial"), dict) else {}
    if not financial:
        for fk in _DEEPSEEK_FINANCIAL_FIELDS:
            v = stock_dict.get(fk)
            if not _is_deepseek_empty_value(v):
                financial[fk] = v
    if not financial:
        return {}
    text_parts: List[str] = []
    # 优先复用 summary_text
    base_summary = str(financial.get("summary_text") or stock_dict.get("summary_text") or "").strip()
    # 关键财务数字
    rev = financial.get("revenue_yoy")
    np_ = financial.get("net_profit_yoy")
    dedup = financial.get("deduct_net_profit_yoy")
    roe = financial.get("roe")
    gross = financial.get("gross_margin")
    debt = financial.get("asset_liab_rate")
    op_cf = financial.get("op_cash_flow")
    goodwill = financial.get("goodwill")
    def _fmt_pct(v):
        if v is None:
            return None
        try:
            x = float(v)
            return f"{x:+.1f}%" if -200 < x < 500 else None
        except (TypeError, ValueError):
            return None
    rev_s = _fmt_pct(rev)
    np_s = _fmt_pct(np_)
    dedup_s = _fmt_pct(dedup)
    if rev_s:
        text_parts.append(f"营收{rev_s}")
    if np_s:
        text_parts.append(f"净利{np_s}")
    if dedup_s and dedup_s != np_s:
        text_parts.append(f"扣非{dedup_s}")
    if gross is not None:
        try:
            text_parts.append(f"毛利率{float(gross):.1f}%")
        except (TypeError, ValueError):
            pass
    if roe is not None:
        try:
            text_parts.append(f"ROE{float(roe):.1f}%")
        except (TypeError, ValueError):
            pass
    if debt is not None:
        try:
            text_parts.append(f"负债率{float(debt):.1f}%")
        except (TypeError, ValueError):
            pass
    if op_cf is not None:
        try:
            text_parts.append(f"经营现金流{float(op_cf):.1f}亿")
        except (TypeError, ValueError):
            pass
    if goodwill is not None:
        try:
            gw = float(goodwill)
            if gw > 5:  # 商誉超 5 亿要提示
                text_parts.append(f"商誉{gw:.1f}亿⚠")
        except (TypeError, ValueError):
            pass
    if not text_parts and not base_summary:
        return {}
    # 拼接: 人话摘要 + 数字要点
    text_line = " | ".join(text_parts) if text_parts else ""
    full = (base_summary[:80] + " | " + text_line).strip(" |") if base_summary and text_line else (base_summary or text_line)
    return {"text": full[:200], "summary": base_summary[:80] if base_summary else text_line[:80]}


# [V26.88+10-9 优化] 市场情绪量价快照缓存
# 数据源: data/runtime/state/hithink_poller_latest.json (由 hithink_poller 5 分钟落盘)
_HIT_THINK_SENTIMENT_CACHE: Dict[str, Any] = {"ts": 0.0, "data": {}}
_HIT_THINK_SENTIMENT_TTL_SEC = 60.0  # 1 分钟缓存, 避免每次推送都读盘


def _load_hithink_sentiment_snapshot() -> Dict[str, Any]:
    """[V26.88+10-9 优化项 3] 加载 HiThink 涨停/跌停/热点股/龙虎榜/飙升 5 个量价数据
    返回: {
        "limit_up_count": 66, "limit_down_count": 7,
        "hot_stock_n": 30, "dragon_tiger_n": 0, "skyrocket_n": 30,
        "ts": "2026-10-09T14:30:00",
    }
    """
    import time as _t
    now = _t.time()
    if now - _HIT_THINK_SENTIMENT_CACHE.get("ts", 0.0) < _HIT_THINK_SENTIMENT_TTL_SEC:
        return _HIT_THINK_SENTIMENT_CACHE.get("data") or {}
    out: Dict[str, Any] = {}
    try:
        import json as _json
        from pathlib import Path
        snap_path = Path("data/runtime/state/hithink_poller_latest.json")
        if not snap_path.exists():
            _HIT_THINK_SENTIMENT_CACHE["ts"] = now
            _HIT_THINK_SENTIMENT_CACHE["data"] = out
            return out
        with snap_path.open("r", encoding="utf-8") as f:
            snap = _json.load(f)
        counts = snap.get("counts") or {}
        if isinstance(counts, dict):
            out["limit_up_count"] = int(counts.get("limit_up_pool") or 0)
            out["limit_down_count"] = int(counts.get("limit_down_pool") or 0)
            out["hot_stock_n"] = int(counts.get("hot_stock_list") or 0)
            out["dragon_tiger_n"] = int(counts.get("dragon_tiger_list") or 0)
            out["skyrocket_n"] = int(counts.get("skyrocket_list") or 0)
        meta = snap.get("_meta") or {}
        out["ts"] = str(meta.get("finished_at") or "")
    except Exception as e:
        logger.debug("读 HiThink 情绪快照失败: %s", e)
    _HIT_THINK_SENTIMENT_CACHE["ts"] = now
    _HIT_THINK_SENTIMENT_CACHE["data"] = out
    return out


# [V26.88+10-9 优化] 顶流股票记忆缓存
# 同板块/同战法的近 5 个最强 T+1 表现, 让模型在判断该股时知道"同类最近胜率"
_TOP_PEERS_CACHE: Dict[str, Any] = {"ts": 0.0, "data": {}}
_TOP_PEERS_TTL_SEC = 120.0  # 2 分钟缓存


def _load_top_peer_memory(stock_dict: Dict[str, Any]) -> Dict[str, Any]:
    """[V26.88+10-9 优化项 3] 顶流股票记忆: 同战法/同板块近期最强 5 只的 T+1 表现
    返回: {
        "same_tactic": "近5只同战法: 茅台+2.3%, 五粮液+1.8%, 海康+3.1% (平均+2.1%, 胜率3/5)",
        "same_sector": "白酒板块近5强: 茅台+2.3%, 五粮液+1.8% (板块强度8.5)",
    }
    """
    import time as _t
    now = _t.time()
    cache_key = str(stock_dict.get("tactic") or stock_dict.get("战法") or "") + "|" + str(stock_dict.get("top_industry") or stock_dict.get("主线行业") or "")
    if now - _TOP_PEERS_CACHE.get("ts", 0.0) < _TOP_PEERS_TTL_SEC:
        cached = _TOP_PEERS_CACHE.get("data") or {}
        if cache_key in cached:
            return cached[cache_key]
    out: Dict[str, Any] = {}
    try:
        from data.db_core import get_read_conn_singleton, table_exists
        con = None
        try:
            con = get_read_conn_singleton(max_wait_sec=2.0)
        except Exception:
            con = None
        if con is None:
            # [V26.88+10-9 优化] DB 拿不到时不报错, 静默降级
            logger.debug("顶流股票记忆: DB 不可用, 跳过")
            return out
        tactic = str(stock_dict.get("tactic") or stock_dict.get("战法") or "").strip()
        sector = str(stock_dict.get("top_industry") or stock_dict.get("主线行业") or "").strip()
        # 降级策略: 查询任意一张"选股历史/推送记录"表, 找到同战法/同板块的近期推送
        sniper_tbl = None
        for t in ("fact_sniper_history", "fact_pool_history", "fact_sniper_alerts"):
            if table_exists(t):
                sniper_tbl = t
                break
        if not sniper_tbl:
            return out
        # 同战法: 取最近 5 条同 tactic 的 t1 收益
        if tactic:
            try:
                df1 = con.execute(
                    f"SELECT ts_code, name, t1_return_pct FROM {sniper_tbl} "
                    "WHERE tactic_code = ? OR tactic = ? OR strategy = ? "
                    "ORDER BY COALESCE(buy_date, trade_date, push_time, '') DESC LIMIT 5",
                    [tactic[:20], tactic[:30], tactic[:30]],
                ).fetchdf()
            except Exception:
                df1 = None
            if df1 is not None and not df1.empty:
                rows = df1.to_dict(orient="records")
                names = []
                wins = 0
                rets = []
                for r in rows:
                    nm = str(r.get("name") or r.get("ts_code") or "?")[:6]
                    ret = r.get("t1_return_pct") or r.get("next_day_pct") or r.get("pct_chg")
                    if ret is not None:
                        try:
                            rxf = float(ret)
                            rets.append(rxf)
                            if rxf > 0:
                                wins += 1
                            names.append(f"{nm}{rxf:+.1f}%")
                        except (TypeError, ValueError):
                            pass
                if names:
                    avg = sum(rets) / len(rets) if rets else 0
                    out["same_tactic"] = (
                        f"近{len(names)}只同战法: " + ", ".join(names)
                        + f" (均{avg:+.1f}%, 胜率{wins}/{len(rets)})"
                    )
        # 同板块: 简化版, 用 top_industry 关联
        if sector:
            try:
                df2 = con.execute(
                    f"SELECT ts_code, name, pct_chg FROM {sniper_tbl} "
                    "WHERE industry = ? OR top_industry = ? "
                    "ORDER BY COALESCE(buy_date, trade_date, push_time, '') DESC LIMIT 5",
                    [sector[:20], sector[:20]],
                ).fetchdf()
            except Exception:
                df2 = None
            if df2 is not None and not df2.empty:
                rows = df2.to_dict(orient="records")
                names = []
                for r in rows:
                    nm = str(r.get("name") or r.get("ts_code") or "?")[:6]
                    pct = r.get("pct_chg")
                    if pct is not None:
                        try:
                            pcf = float(pct)
                            names.append(f"{nm}{pcf:+.1f}%")
                        except (TypeError, ValueError):
                            pass
                if names:
                    out["same_sector"] = f"{sector}近5强: " + ", ".join(names)
    except Exception as e:
        logger.debug("读顶流股票记忆失败: %s", e)
    # 写缓存
    if out:
        _TOP_PEERS_CACHE.setdefault("data", {})[cache_key] = out
        _TOP_PEERS_CACHE["ts"] = now
    return out


def _normalize_p3_str_fields(stock_dict: Dict[str, Any]) -> Dict[str, Any]:
    """[V26.88+10-9 修复] 把 P3 base_info 里的字符串型数字字段归一化为 float
    pipeline.py:860 把"涨幅/量比/真换手/现价"等写成字符串给微信显示,
    但 DeepSeek 评分模型需要 float. 在 _build_deepseek_stock_payload 入口处做归一化,
    不修改原 stock_dict (避免影响微信推送路径)。
    """
    if not isinstance(stock_dict, dict):
        return stock_dict
    sd = dict(stock_dict)  # 浅拷贝, 不污染原 dict
    # 涨幅: "+4.32%" -> 4.32
    for key in ("涨幅", "pct_chg", "实时涨幅", "realtime_pct_chg"):
        v = sd.get(key)
        if isinstance(v, str):
            try:
                cleaned = v.replace("%", "").replace("+", "").strip()
                f = float(cleaned)
                sd[key] = f
            except (TypeError, ValueError):
                pass
    # 量比: "1.85(F)" -> 1.85
    for key in ("量比", "volume_ratio", "实时量比", "realtime_volume_ratio"):
        v = sd.get(key)
        if isinstance(v, str):
            try:
                import re as _re
                m = _re.search(r"[-+]?\d*\.?\d+", v)
                if m:
                    sd[key] = float(m.group(0))
            except (TypeError, ValueError):
                pass
    # 现价: "1580.50" -> 1580.5
    for key in ("现价", "price", "close", "latest_price"):
        v = sd.get(key)
        if isinstance(v, str):
            try:
                sd[key] = float(v.replace(",", "").strip())
            except (TypeError, ValueError):
                pass
    # 真换手: "1.5%" -> 1.5
    for key in ("真换手", "turnover_rate_f", "turnover_rate"):
        v = sd.get(key)
        if isinstance(v, str):
            try:
                sd[key] = float(v.replace("%", "").replace("+", "").strip())
            except (TypeError, ValueError):
                pass
    # 集中度: "12.5" -> 12.5
    for key in ("集中度", "cyq_concentration"):
        v = sd.get(key)
        if isinstance(v, str):
            try:
                sd[key] = float(v.replace("%", "").strip())
            except (TypeError, ValueError):
                pass
    # 北向资金: "+1.2亿" -> 1.2 (单位亿)
    for key in ("北向资金", "hk_net_buy"):
        v = sd.get(key)
        if isinstance(v, str):
            try:
                import re as _re
                m = _re.search(r"[-+]?\d*\.?\d+", v)
                if m:
                    sd[key] = float(m.group(0))
            except (TypeError, ValueError):
                pass
    # 主力净额: "150000000.0" 或字符串 -> 保持 float
    for key in ("主力净额", "net_main_amount", "main_net_amount"):
        v = sd.get(key)
        if isinstance(v, str):
            try:
                sd[key] = float(v.replace(",", "").strip())
            except (TypeError, ValueError):
                pass
    # 连板高度: "3" -> 3
    for key in ("连板高度", "limit_times", "limit_up_times"):
        v = sd.get(key)
        if isinstance(v, str):
            try:
                sd[key] = int(float(v.strip()))
            except (TypeError, ValueError):
                pass
    # 战法: "P3-01 右侧起爆 | P3-05 平台确认 | 金·短共振" -> 取第一个作为主战法
    # (P3 链路里 战法 字段是 3 个战法 | 拼接, deepseek 关键词匹配需要单个战法)
    for key in ("战法", "tactic", "strategy"):
        v = sd.get(key)
        if isinstance(v, str) and "|" in v:
            first = v.split("|", 1)[0].strip()
            if first:
                sd[key] = first
                # 把全量战法清单保留到 raw_tactics
                sd["raw_tactics"] = v
    return sd


def _build_deepseek_stock_payload(pool_key: str, stock_dict: Dict[str, Any]) -> str:
    """为 DeepSeek 构建精简高价值 payload"""
    if not isinstance(stock_dict, dict):
        stock_dict = {}
    pk = str(pool_key or "").strip().lower()
    clean: Dict[str, Any] = {"pool_key": pk}
    used_keys = set()

    # [V26.88+10-9 修复] P3 链路: base_info 字段是字符串 ("+4.32%", "1.85(F)", "1580.50"),
    # 微信推送要字符串, 但 DeepSeek 必须用 float. 这里在进入 alias 查找前先做归一化副本.
    stock_dict = _normalize_p3_str_fields(stock_dict)

    for canonical, aliases in _DEEPSEEK_FIELD_ALIASES.items():
        val = _get_first_deepseek_value(stock_dict, aliases)
        if not _is_deepseek_empty_value(val):
            clean[canonical] = _normalize_deepseek_financial(val) if canonical == "financial" else val
            for alias in aliases:
                if alias in stock_dict:
                    used_keys.add(alias)

    if "code" in clean:
        clean["code"] = _norm_ts_code(clean.get("code")) or clean.get("code")
    if "name" in clean:
        clean["name"] = normalize_stock_display_name(clean.get("name"))

    # 第二步：财报快照
    fin_snap = _load_financial_snapshot_for_deepseek(stock_dict.get("ts_code") or clean.get("code"))
    for k, v in fin_snap.items():
        if k == "financial":
            cur_fin = clean.get("financial") if isinstance(clean.get("financial"), dict) else {}
            if not isinstance(cur_fin, dict):
                cur_fin = {}
            if isinstance(v, dict):
                for fk, fv in v.items():
                    if _is_deepseek_empty_value(cur_fin.get(fk)) and not _is_deepseek_empty_value(fv):
                        cur_fin[fk] = fv
            if cur_fin:
                clean["financial"] = cur_fin
        elif _is_deepseek_empty_value(clean.get(k)) and not _is_deepseek_empty_value(v):
            clean[k] = v

    # [V26.88+10-9 优化] 第二步半: 财报"以文为主"压缩, 把数字拼成一句话
    # 只在 P5 盘后池启用 (其他池已 financial=False)
    pool_cfg_early = _DEEPSEEK_POOL_FIELDS.get(pk, {})
    if bool(pool_cfg_early.get("financial", True)):
        fin_text = _compress_deepseek_financial_to_text(clean, stock_dict)
        if fin_text:
            clean["financial_text"] = fin_text
            # 不再删除原 financial dict, 留给模型自取, 但 financial_text 是首选

    # 第三步：高价值派生字段
    _enrich_deepseek_derived_fields(pk, clean, stock_dict)

    # [V26.88+10-9 优化项 3] 第三步半: 市场情绪量价 + 顶流股票记忆
    # 所有池子都受益, 但字段体量小 (~150 字符)
    if pk in {"p2", "p3", "p4", "p5"}:
        sent = _load_hithink_sentiment_snapshot()
        if sent:
            up_n = sent.get("limit_up_count", 0)
            down_n = sent.get("limit_down_count", 0)
            hot_n = sent.get("hot_stock_n", 0)
            dragon_n = sent.get("dragon_tiger_n", 0)
            # 拼成一句话节省字符
            clean["market_pulse"] = (
                f"涨停{up_n}/跌停{down_n}/热点{hot_n}/龙虎{dragon_n}"
            )
            # 关键: 把情绪分类结果也带上
            try:
                from core.market_sentiment import classify_market_sentiment
                cls = classify_market_sentiment(
                    limit_up_count=up_n, limit_down_count=down_n,
                    max_consecutive_limit=0, limit_burst_rate=0.0, up_ratio=0.5,
                )
                if isinstance(cls, dict) and cls.get("state"):
                    clean["market_state"] = f"{cls.get('state')}({cls.get('score', '?')})"
            except Exception:
                pass
        # 顶流股票记忆: 同战法/同板块近期最强
        peers = _load_top_peer_memory(stock_dict)
        if peers:
            clean["peer_memory"] = peers

    # 第四步：财报字段防御性兜底
    financial = clean.get("financial")
    pool_cfg = _DEEPSEEK_POOL_FIELDS.get(pk, {})
    pool_core = pool_cfg.get("core", set())
    pool_allow = pool_cfg.get("allow", set())
    pool_keep_financial = bool(pool_cfg.get("financial", True))
    pool_keep_tactic_ctx = bool(pool_cfg.get("tactic_ctx", True))

    if isinstance(financial, dict):
        if not pool_keep_financial:
            # 该池不要财报, 直接丢弃节省 ~400 字符
            clean.pop("financial", None)
        else:
            for key in _DEEPSEEK_FINANCIAL_FIELDS:
                val = stock_dict.get(key)
                if not _is_deepseek_empty_value(val) and key not in financial:
                    financial[key] = val
    elif any(k in stock_dict for k in _DEEPSEEK_FINANCIAL_FIELDS):
        if not pool_keep_financial:
            pass  # 该池不要财报, 不构造
        else:
            fin2: Dict[str, Any] = {}
            for key in _DEEPSEEK_FINANCIAL_FIELDS:
                val = stock_dict.get(key)
                if not _is_deepseek_empty_value(val):
                    fin2[key] = val
            if fin2:
                clean["financial"] = fin2

    # [V26.88+10-9 优化] 第五步: 按池字段白名单裁剪, 解决 P5 兜底池 payload 过大截断问题
    if pool_core or pool_allow:
        allowed = pool_core | pool_allow
        # 必须保留的元字段 + 优化项 3 通用字段
        must_keep = {"pool_key", "code", "name", "tactic", "tactic_name", "tactic_code",
                     "entry_reason", "score", "financial", "financial_text",
                     "market_pulse", "market_state", "peer_memory"}
        allowed = allowed | must_keep
        # 删除超出白名单的字段
        to_drop = [k for k in clean.keys() if k not in allowed]
        for k in to_drop:
            clean.pop(k, None)
        # 在 financial 内部也按池裁剪
        fin = clean.get("financial")
        if isinstance(fin, dict):
            # P2/P3 财报字段全保留, P4/P5 已 financial=False 不会到这里
            pass  # 财报细分裁剪可后续按需扩展

    payload = {
        "pool_key": pk,
        "pool_focus": _DEEPSEEK_POOL_FOCUS.get(pk, "按短线交易信号综合判断买入、观望或放弃。"),
        "pool_semantics": _build_deepseek_pool_semantics(pk),
        "tactic_context": _build_deepseek_tactic_context(clean, stock_dict) if pool_keep_tactic_ctx else {},
        "stock": clean,
    }
    try:
        return json.dumps(payload, ensure_ascii=False, sort_keys=False, default=str)
    except Exception:
        safe_payload = {
            "pool_key": pk,
            "pool_focus": _DEEPSEEK_POOL_FOCUS.get(pk, "按短线交易信号综合判断买入、观望或放弃。"),
            "stock": {str(k): str(v) for k, v in clean.items()},
        }
        return json.dumps(safe_payload, ensure_ascii=False, sort_keys=False)


def _append_deepseek_advice(content: str, advice: str) -> str:
    block = f"\n\n【DeepSeek分析建议】\n{_trim_deepseek_advice(advice)}"
    merged = f"{str(content or '').rstrip()}{block}"
    if len(merged) > 3800:
        merged = merged[: 3780].rstrip() + "\n…(已截断)"
    return merged


def _deepseek_ts_code_full(stock_dict: Dict[str, Any]) -> str:
    raw = stock_dict.get("ts_code") or stock_dict.get("代码") or stock_dict.get("code")
    s = str(raw or "").strip().upper()
    if not s:
        return ""
    if "." in s:
        return s
    s6 = _norm_ts_code(s)
    if s6.startswith("6"):
        return f"{s6}.SH"
    if s6.startswith(("8", "4")):
        return f"{s6}.BJ"
    return f"{s6}.SZ"


def _classify_deepseek_error(err: Any, status_code: Optional[int] = None, body: str = "") -> str:
    """把 DeepSeek 失败原因压成醒目的中文分类"""
    msg = f"{str(err or '')} {str(body or '')}".lower()
    code = int(status_code or 0)
    if code in (401, 403) or "unauthorized" in msg or "invalid api key" in msg or "authentication" in msg:
        return "鉴权失败/API Key无效"
    if code == 400 or "bad request" in msg or "invalid" in msg or "unknown parameter" in msg:
        return "请求参数错误"
    if code == 404 or "model" in msg and "not" in msg:
        return "模型或接口地址错误"
    if code == 429 or "rate limit" in msg or "quota" in msg or "insufficient" in msg:
        return "限速/额度不足"
    if 500 <= code < 600:
        return "DeepSeek服务端异常"
    if "timeout" in msg or "timed out" in msg or "read timed" in msg:
        return "请求超时"
    if "connection" in msg or "dns" in msg or "proxy" in msg or "ssl" in msg:
        return "网络/代理/证书异常"
    if "choices" in msg:
        return "返回结构异常"
    return "未知异常"


def _log_deepseek_analysis(
    *,
    pool_key: str,
    stock_dict: Dict[str, Any],
    stock_info: str,
    advice: str,
    model: str,
    latency_ms: float,
    success: bool,
    error_msg: str = "",
    error_type: str = "",
    reasoning_content: str = "",
) -> None:
    """AI 分析日志独立落库，失败只记 debug，绝不影响企微推送。"""
    # 【V30.9】演练模式不写 AI 分析日志，避免污染 fact_ai_analysis_log / vw_ai_analysis_debug。
    try:
        from core.qa_switches import no_deepseek

        if no_deepseek():
            return
    except Exception:  # pragma: no cover
        pass

    try:
        from data.db_core import ensure_v26_tables, save_df_to_sql
        import pandas as pd

        ensure_v26_tables()
        ts_code = _deepseek_ts_code_full(stock_dict)
        trade_date = str(stock_dict.get("trade_date") or stock_dict.get("交易日期") or _bj_now_str("%Y-%m-%d"))[:10]
        prompt_hash = hashlib.sha256(str(stock_info or "").encode("utf-8", errors="ignore")).hexdigest()[:24]
        row = {
            "id": uuid.uuid4().hex,
            "ts_code": ts_code,
            "trade_date": trade_date,
            "pool_key": str(pool_key or "").lower(),
            "prompt_hash": prompt_hash,
            "input_json": str(stock_info or "")[:12000],
            "advice": _trim_deepseek_advice(advice),
            "model": str(model or ""),
            "latency_ms": float(latency_ms or 0.0),
            "success": bool(success),
            "error_msg": str((f"[{error_type}] " if error_type else "") + str(error_msg or ""))[:1000],
            "reasoning_content": str(reasoning_content or "")[:4000],
            "created_at": _bj_now_str("%Y-%m-%d %H:%M:%S"),
        }
        save_df_to_sql(pd.DataFrame([row]), "fact_ai_analysis_log")
    except Exception as e:
        logger.debug("DeepSeek 分析日志落库失败(不影响推送): %s", e)


def _maybe_get_deepseek_advice(pool_key: str, stock_dict: Dict[str, Any]) -> str:
    """仅对 P2/P3/P4/P5 生成 DeepSeek 建议；失败时返回兜底文案。"""
    # 【V30.9 2026-10-18】QA / 演练旁路：XJ_NO_DEEPSEEK=1 时完全不调 DeepSeek。
    # 事故背景：2026-10-18 上线前点检真实调用 DeepSeek 13 次（07:22 三条 + 09:32 十条），
    # 累计计费耗时 66 秒，并往 fact_ai_analysis_log 写了 13 条 trade_date=2026-10-18 的脏行。
    # 短路点放在「配置读取」之前，连 API key 校验、缓存读写、日志落库都不发生。
    try:
        from core.qa_switches import no_deepseek

        if no_deepseek():
            logger.warning("[QA旁路] XJ_NO_DEEPSEEK=1 已设置，本次 DeepSeek 分析已跳过（演练模式）")
            return ""
    except Exception as e:  # fail-open：开关读取失败不阻断生产
        logger.debug("读取 XJ_NO_DEEPSEEK 失败，继续按生产链路调用: %s", e)

    pk = str(pool_key or "").strip().lower()
    if pk not in {"p2", "p3", "p4", "p5"}:
        return ""
    try:
        from core.config_manager import get_deepseek_analysis_config
        cfg = get_deepseek_analysis_config()
    except Exception as e:
        logger.debug("DeepSeek 配置读取失败: %s", e)
        return _DEEPSEEK_FALLBACK_ADVICE
    if not cfg.get("enabled"):
        _record_deepseek_metric("disabled", "skip")
        return ""
    api_key = str(cfg.get("api_key") or "").strip()
    base_url = str(cfg.get("base_url") or "").strip()
    model = str(cfg.get("model") or "deepseek-flash").strip()
    thinking_enabled = bool(cfg.get("thinking_enabled", False))
    reasoning_effort = str(cfg.get("reasoning_effort") or "low").strip().lower()
    timeout_seconds = float(cfg.get("timeout_seconds") or 8.0)
    max_tokens = int(cfg.get("max_tokens") or 320)
    fallback_enabled = bool(cfg.get("fallback_enabled", True))
    fallback_model = str(cfg.get("fallback_model") or "deepseek-flash").strip() or "deepseek-flash"
    prompt_cache_enabled = bool(cfg.get("prompt_cache_enabled", False))
    if not api_key or not base_url or not model:
        _record_deepseek_metric(model or "unknown", "skip")
        logger.debug("DeepSeek 配置不完整，跳过分析 | pool=%s", pk)
        return _DEEPSEEK_FALLBACK_ADVICE
    try:
        import requests
    except ImportError:
        logger.warning("未安装 requests，无法调用 DeepSeek")
        return _DEEPSEEK_FALLBACK_ADVICE

    stock_info = _build_deepseek_stock_payload(pk, stock_dict)
    pool_focus = _DEEPSEEK_POOL_FOCUS.get(pk, "按短线交易信号综合判断买入、观望或放弃。")

    _code_for_cache = _norm_ts_code(stock_dict.get("代码") if isinstance(stock_dict, dict) else None)
    _date_for_cache = str(
        stock_dict.get("trade_date") or stock_dict.get("交易日期") or _bj_now_str("%Y-%m-%d")
    )[:10]
    cache_key = f"{pk}:{_code_for_cache or 'NA'}:{_date_for_cache}"

    def _build_payload(
        *,
        target_model: str,
        enable_thinking: bool,
        token_budget: int,
        concise_retry: bool = False,
    ) -> Dict[str, Any]:
        user_prompt = (
            f"当前池子：{pk.upper()}\n"
            f"分析侧重点：{pool_focus}\n"
            "你会同时看到池子语义说明与战法知识库，请先理解该池子的交易场景，再结合战法含义判断信号质量。"
            "请基于以下结构化股票信息，直接给出买入、观望或放弃判断。"
            "优先考虑：是否符合该池子的买点逻辑、战法是否被当前量价资金验证、是否存在明显风险。"
        )
        if concise_retry:
            user_prompt += "本次是最终结论重试，请基于你已经看到的股票信息，只输出一句明确结论，20字以内。"
        else:
            user_prompt += "只返回一句话，50个字以内，交易大师口吻，不要解释。"
            user_prompt += f"\n股票信息如下：\n{stock_info}"
        payload_local: Dict[str, Any] = {
            "model": target_model,
            "messages": [
                {"role": "system", "content": _build_system_prompt(target_model)},
                {"role": "user", "content": user_prompt},
            ],
            "max_tokens": int(token_budget),
            "stream": False,
        }
        if prompt_cache_enabled:
            payload_local["prompt_cache_key"] = cache_key
        if enable_thinking:
            payload_local["reasoning_effort"] = reasoning_effort
            payload_local["thinking"] = {"type": "enabled"}
            # [V26.88+10-9 修复] 官方要求: 思考模式下 top_p 必须 ∈ [0.95, 1.0]
            # 低于 0.95 会被服务端强制为 0.95, 高于 1.0 视为非法
            # 参考: https://api-docs.deepseek.com/guides/thinking_mode
            existing_top_p = payload_local.get("top_p")
            if existing_top_p is None or float(existing_top_p) < 0.95:
                payload_local["top_p"] = 0.95
        return payload_local

    def _build_payload_with_hint(
        *,
        target_model: str,
        enable_thinking: bool,
        token_budget: int,
        compact_hint: str,
    ) -> Dict[str, Any]:
        """[V26.88+10-8 修复] 2nd 重试专用 payload: 短 prompt + 极简股票要素,
        给 fallback_model 足够决策空间, 又避免重传长 stock_info 触发同样的 flash 截断问题.
        """
        user_prompt_local = (
            f"当前池子：{pk.upper()}\n"
            f"分析侧重点：{pool_focus}\n"
            "1st 请求因 max_tokens 截断失败, 现在重新提问。"
            "请基于以下极简票面要素, 直接给出一句结论 (买入/观望/放弃 + 1句理由, 30字以内)。"
            "不要解释过程, 不要客套话。\n"
            f"极简票面：{compact_hint}"
        )
        payload_local2: Dict[str, Any] = {
            "model": target_model,
            "messages": [
                {"role": "system", "content": _build_system_prompt(target_model)},
                {"role": "user", "content": user_prompt_local},
            ],
            "max_tokens": int(token_budget),
            "stream": False,
        }
        if prompt_cache_enabled:
            payload_local2["prompt_cache_key"] = cache_key
        if enable_thinking:
            payload_local2["reasoning_effort"] = reasoning_effort
            payload_local2["thinking"] = {"type": "enabled"}
        return payload_local2

    def _do_request(payload_local: Dict[str, Any], tag: str) -> Dict[str, Any]:
        started_local = time.perf_counter()
        resp = requests.post(
            base_url,
            json=payload_local,
            timeout=(min(5.0, timeout_seconds), timeout_seconds + 10.0),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json; charset=utf-8",
            },
        )
        elapsed_ms = (time.perf_counter() - started_local) * 1000.0
        if not (200 <= int(resp.status_code) < 300):
            body = resp.text[:800] if getattr(resp, "text", None) else ""
            et = _classify_deepseek_error(f"HTTP {resp.status_code}", resp.status_code, body)
            raise RuntimeError(f"[{tag}] {et} | HTTP {resp.status_code} | {body}")
        data = resp.json() if resp.content else {}
        choices = data.get("choices") if isinstance(data, dict) else None
        if not (isinstance(choices, list) and choices):
            raise RuntimeError(f"[{tag}] DeepSeek 返回 choices 为空")
        first = choices[0] if isinstance(choices[0], dict) else {}
        msg = first.get("message") if isinstance(first, dict) else None
        reasoning = ""
        if isinstance(msg, dict):
            r = msg.get("reasoning_content")
            if isinstance(r, str):
                reasoning = r
        content = msg.get("content") if isinstance(msg, dict) else ""
        finish_reason = str(first.get("finish_reason") or "").strip().lower()
        return {
            "content": str(content or ""),
            "reasoning_content": reasoning,
            "finish_reason": finish_reason,
            "elapsed_ms": elapsed_ms,
            "raw": data,
        }

    code = _norm_ts_code(stock_dict.get("代码") if isinstance(stock_dict, dict) else None)
    # [V26.88+10-8 修复] 1st 截断后需要降级到 fallback_model, 截断也属于降级触发条件之一
    _FALLBACK_TRIGGERS = ("限速", "额度", "服务端", "未知异常", "网络", "返回截断")
    final_model_tag = model
    used_fallback = False
    # [V26.88+10-8 修复] 1st 是否开 thinking 跟随配置 thinking_enabled, 而不是硬编码 False
    # 之前 _first_enable_thinking 写死 False, 但 tag 却写 "thinking-pass", 行为与命名完全不一致
    _first_enable_thinking = bool(thinking_enabled)
    # [V26.88+10-8 修复] 1st max_tokens 下限 640: flash 模型在 320 tokens 内即使开 thinking
    # 也常因 preamble 被截断, 640 给"开头客套话 + 50字结论"留出充足余量
    first_token_budget = max(int(max_tokens), 640)
    try:
        first_try = _do_request(
            _build_payload(
                target_model=model,
                enable_thinking=_first_enable_thinking,
                token_budget=first_token_budget,
                concise_retry=False,
            ),
            "thinking-pass" if _first_enable_thinking else "primary-pass",
        )
        advice = _trim_deepseek_advice(first_try["content"])
        if not (first_try["finish_reason"] == "length" and len(str(first_try["content"] or "").strip()) < 16):
            _log_deepseek_analysis(
                pool_key=pk,
                stock_dict=stock_dict,
                stock_info=stock_info,
                advice=advice,
                model=final_model_tag,
                latency_ms=float(first_try["elapsed_ms"]),
                success=True,
                error_type="",
                reasoning_content=first_try.get("reasoning_content", ""),
            )
            _record_deepseek_metric(model, "success", first_try["elapsed_ms"])
            return advice

        _record_deepseek_metric(model, "truncate", first_try["elapsed_ms"])

        logger.warning(
            "【DeepSeek重试】type=返回截断 pool=%s code=%s model=%s first_elapsed_ms=%.0f finish_reason=%s content=%r",
            pk, code, model,
            float(first_try["elapsed_ms"]),
            first_try["finish_reason"],
            str(first_try["content"] or "")[:120],
        )

        # [V26.88+10-9 按官方文档修正] 2nd 关键改进:
        # 1) 切到 fallback_model (deepseek-flash), 避开主模型同款再炸
        # 2) 在 2nd 短 prompt 中补上 code + 综合分 + 涨幅 + 战法, 给模型最小决策依据
        #    (避免完全无输入下模型输出 "请补充股票信息" 这类废语)
        # 3) 2nd 也加独立 warning 日志, 方便定位 flash 是否一炸再炸
        _retry_model = fallback_model if (fallback_enabled and fallback_model and fallback_model != model) else model
        _compact_hint = _build_deepseek_compact_hint(pk, stock_dict)
        second_try = _do_request(
            _build_payload_with_hint(
                target_model=_retry_model,
                enable_thinking=False,
                token_budget=max(int(max_tokens) * 4, 1600),
                compact_hint=_compact_hint,
            ),
            "fallback-retry" if _retry_model != model else "final-answer-retry",
        )
        advice2 = _trim_deepseek_advice(second_try["content"])
        if advice2 and advice2 != _DEEPSEEK_FALLBACK_ADVICE:
            _log_deepseek_analysis(
                pool_key=pk,
                stock_dict=stock_dict,
                stock_info=stock_info,
                advice=advice2,
                model=f"{_retry_model}#retry_no_thinking",
                latency_ms=float(first_try["elapsed_ms"]) + float(second_try["elapsed_ms"]),
                success=True,
                error_type="",
                reasoning_content=second_try.get("reasoning_content", ""),
            )
            _record_deepseek_metric(
                _retry_model, "fallback",
                float(first_try["elapsed_ms"]) + float(second_try["elapsed_ms"]),
            )
            return advice2

        logger.warning(
            "【DeepSeek重试】type=二次仍截断 pool=%s code=%s retry_model=%s second_elapsed_ms=%.0f "
            "second_finish=%s second_content=%r",
            pk, code, _retry_model,
            float(second_try["elapsed_ms"]),
            second_try["finish_reason"],
            str(second_try["content"] or "")[:120],
        )

        err_msg = (
            f"DeepSeek 二次重试后仍未得到有效结论 | first_finish={first_try['finish_reason']} "
            f"first_content={first_try['content']!r} second_finish={second_try['finish_reason']} second_content={second_try['content']!r}"
        )
        err_type = "返回截断"
    except Exception as e:
        err_msg = str(e)
        err_type = _classify_deepseek_error(e)
        logger.error(
            "【DeepSeek失败】type=%s pool=%s code=%s model=%s base_url=%s err=%s",
            err_type, pk, code, model, base_url, err_msg[:800],
        )
        if fallback_enabled and any(t in err_type for t in _FALLBACK_TRIGGERS) and fallback_model and fallback_model != model:
            try:
                fb_try = _do_request(
                    _build_payload(
                        target_model=fallback_model,
                        enable_thinking=False,
                        token_budget=max(max_tokens, 320),
                        concise_retry=False,
                    ),
                    "fallback-flash",
                )
                fb_advice = _trim_deepseek_advice(fb_try["content"])
                if fb_advice and fb_advice != _DEEPSEEK_FALLBACK_ADVICE:
                    used_fallback = True
                    final_model_tag = fallback_model
                    logger.info(
                        "【DeepSeek降级】primary=%s 失败(%s) → fallback=%s 成功 elapsed=%.0fms",
                        model, err_type, fallback_model, float(fb_try["elapsed_ms"]),
                    )
                    _log_deepseek_analysis(
                        pool_key=pk,
                        stock_dict=stock_dict,
                        stock_info=stock_info,
                        advice=fb_advice,
                        model=f"{fallback_model}#fallback",
                        latency_ms=float(fb_try["elapsed_ms"]),
                        success=True,
                        error_type="",
                        reasoning_content=fb_try.get("reasoning_content", ""),
                    )
                    _record_deepseek_metric(fallback_model, "fallback", fb_try["elapsed_ms"])
                    return fb_advice
            except Exception as e2:
                _record_deepseek_metric(fallback_model or "fallback_unknown", "error")
                logger.warning(
                    "【DeepSeek降级失败】fallback=%s err=%s",
                    fallback_model, str(e2)[:300],
                )

    _log_deepseek_analysis(
        pool_key=pk,
        stock_dict=stock_dict,
        stock_info=stock_info,
        advice=_DEEPSEEK_FALLBACK_ADVICE,
        model=f"{final_model_tag}#fail{'#fb' if used_fallback else ''}",
        latency_ms=0.0,
        success=False,
        error_msg=err_msg,
        error_type=err_type,
        reasoning_content="",
    )
    _record_deepseek_metric(final_model_tag, "error")
    return _DEEPSEEK_FALLBACK_ADVICE


__all__ = [
    "_DEEPSEEK_FALLBACK_ADVICE", "_DEEPSEEK_SYSTEM_PROMPT",
    "_DEEPSEEK_POOL_FOCUS", "_DEEPSEEK_POOL_SEMANTICS",
    "_DEEPSEEK_TACTIC_KNOWLEDGE", "_DEEPSEEK_FIELD_ALIASES",
    "_DEEPSEEK_FINANCIAL_FIELDS",
    "_record_deepseek_metric", "get_deepseek_metrics_snapshot", "_build_system_prompt",
    "_trim_deepseek_advice", "_is_deepseek_empty_value",
    "_get_first_deepseek_value", "_deepseek_float",
    "_extract_tactic_keywords", "_build_deepseek_tactic_context",
    "_build_deepseek_pool_semantics", "_deepseek_pick_float",
    "_enrich_deepseek_derived_fields", "_normalize_deepseek_financial",
    "_load_financial_snapshot_for_deepseek",
    "_build_deepseek_stock_payload", "_append_deepseek_advice",
    "_deepseek_ts_code_full", "_classify_deepseek_error",
    "_log_deepseek_analysis", "_maybe_get_deepseek_advice",
]
