# -*- coding: utf-8 -*-
"""
core/p1_death_cause_registry.py
小杰AI选股系统 Pro — P1 死因分层登记表（批次 A）

【解决什么问题】
P1 有 30 条淘汰死因，但在 UI 上是 30 条**平权**的并列文本。用户看到
「MA20下方」和「业绩爆雷」长得一样，却完全不知道该做什么：

    · 业绩爆雷  → 永久排除，别再看了
    · MA20下方 → 信号已破，等站回均线再评估（可能复活）
    · 直通车超载 → 根本不是票的问题，是名额满了

这批死因的**操作建议完全相反**，但系统把层级信息丢掉了。

【设计约束（严格遵守）】
  1. **零侵入**：不改 core/pool_manager/single_stock.py 里任何一条 return 语句。
     本模块只在**展示层**做「死因文案 → 层级/建议」的反查映射。
  2. **100% 兜底**：任何未登记的死因一律归 L3 + 「未分类」，绝不漏。
     漏了等于把 L0 的票显示成 L3，比不做分层更危险。
  3. **顺序敏感**：匹配规则按 DEATH_CAUSE_RULES 的声明顺序做「首命中即返回」，
     因此更具体的规则必须排在更宽泛的规则之前。
  4. 本模块**不读 DuckDB、不写库、不含任何策略逻辑**，纯静态映射。

【层级语义】
  L0  排除级 · 碰了就亏钱 → 永久拉黑，不因技术面修复
  L1  否决级 · 不适合底仓 → 长期排除（或仅需人工判断）
  L2  拦截级 · 时机不对   → 等信号修复，可能复活 ★ 最需要盯的一层
  L3  降权级 · 只是不好   → 仅扣分，本不该走到淘汰
  D   数据级 · 系统没搞清 → 这不是股票的问题，是系统的问题 ★ 必须视觉隔离
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

# ============================================================================
# 层级元数据
# ============================================================================
LEVEL_META: Dict[str, Dict[str, Any]] = {
    "L0": {
        "level_cn": "排除级",
        "badge": "🔴",
        "action": "永久排除",
        "color": "#dc2626",
        "desc": "碰了就亏钱，不因技术面修复",
    },
    "L1": {
        "level_cn": "否决级",
        "badge": "⚪",
        "action": "长期排除",
        "color": "#6b7280",
        "desc": "不符合 P1 定位，需人工判断是否另作他用",
    },
    "L2": {
        "level_cn": "拦截级",
        "badge": "🟡",
        "action": "等信号修复",
        "color": "#d97706",
        "desc": "时机不对，等条件满足可复活 ★ 最需要盯的一层",
    },
    "L3": {
        "level_cn": "降权级",
        "badge": "🟠",
        "action": "仅扣分",
        "color": "#0891b2",
        "desc": "只是不够好，走到淘汰属于边界个例",
    },
    "D": {
        "level_cn": "数据级",
        "badge": "⚫",
        "action": "系统问题",
        "color": "#4b5563",
        "desc": "这不是股票的问题，是系统还没搞清这只票",
    },
}

UNKNOWN_LEVEL: Dict[str, Any] = {
    "level": "L3",
    "level_cn": "降权级",
    "badge": "🟠",
    "action": "未分类",
    "color": "#0891b2",
    "category": "未分类死因",
    "advice": "该死因未登记入分级表，按最保守的降权级处理，请补登记",
    "desc": "未登记死因，兜底为降权级",
    "matched": None,
    "registered": False,
}

# 空死因（None / "" / 纯空白）专用兜底。
# [V31-AB fix] 文档约定「说不出为什么」= D 数据级（系统问题），
# 但早期实现直接复用 UNKNOWN_LEVEL(L3 降权级)，
# 会把「异常淘汰」误标成「只是不够好」，掩盖真正的数据/执行链路故障。
EMPTY_REASON_LEVEL: Dict[str, Any] = {
    "level": "D",
    "level_cn": "数据级",
    "badge": "⚫",
    "action": "系统问题",
    "color": LEVEL_META["D"]["color"],
    "category": "无死因",
    "advice": "⚫ 系统问题：未记录任何死因，属异常淘汰，请检查 P1 执行链路",
    "desc": LEVEL_META["D"]["desc"],
    "matched": None,
    "registered": False,
}

# ============================================================================
# 分级登记表
# ---------------------------------------------------------------------------
# 元组格式：(关键词, 层级, 分类名, 处置建议)
# 【顺序铁律】首命中即返回 → 具体规则必须在前
# ============================================================================
DEATH_CAUSE_RULES: List[Tuple[str, str, str, str]] = [
    # ---------------- L0 排除级 ----------------
    ("业绩首亏", "L0", "业绩爆雷", "🔴 永久排除：基本面已破坏，不因技术面修复而回池"),
    ("续亏爆雷", "L0", "业绩爆雷", "🔴 永久排除：同上"),
    ("天量解禁", "L0", "解禁风险", "🔴 30 天内不碰：解禁前后资金撤离风险极高"),
    ("解禁黑名单", "L0", "解禁风险", "🔴 30 天内不碰"),
    ("退市风险", "L0", "退市风险", "🔴 永久排除"),
    # [V31-AB fix] 关键词必须用「ST/退市风险」这类语义完整串，不能用裸 "ST"。
    # 根因：匹配是「子串包含」，而 MACD 死因文案里含「容错档strict」，
    # 小写后的 "strict" 包含 "st"，会被裸 "ST" 规则首命中，
    # 把 L2「MACD过弱」错标成 L0「ST永久排除」（详见 tools/_smoke_p1_death_cause.py [2][3]）。
    # 实际文案来源：core/event_filter.py → tags.append("ST/退市风险")，
    # 最终拼进「🚨特殊事件拦截[...; ST/退市风险; ...]」。
    ("ST/退市风险", "L0", "ST 风险", "🔴 永久排除：±5% 涨跌停限制会破坏所有技术指标"),
    ("违规", "L0", "合规风险", "🔴 永久排除"),
    # [V31-AB fix] 顺序铁律落地：「高位派发-15」必须排在「高位派发」之前，
    # 否则 15% 派发会被下方 L0「高位派发」首命中吃掉、误标成永久排除。
    ("高位派发-15", "L2", "高位派发", "🟡 等筹码重新集中：高位放量派发，可回池"),
    ("高位派发", "L0", "高位派发", "🔴 永久排除：筹码结构已坏"),
    # 特殊事件拦截标签来自 core/event_filter.compute_event_penalty，
    # 形如 "🚨特殊事件拦截[次新股; ST/退市风险; 连续跌停]"
    ("特殊事件拦截[次新", "L0", "次新股", "🔴 上市 <30 日，波动无规律，永久排除"),
    ("特殊事件拦截[ST", "L0", "ST 风险", "🔴 永久排除：±5% 涨跌停破坏所有技术指标"),
    ("特殊事件拦截[退市", "L0", "退市风险", "🔴 永久排除"),
    ("特殊事件拦截[连续跌停", "L0", "连续跌停", "🔴 永久排除：跌势未止，接刀风险极高"),
    ("特殊事件拦截[高位派发", "L0", "高位派发", "🔴 永久排除：筹码结构已坏"),

    # ---------------- D 数据级（必须视觉隔离）----------------
    ("日线 df 为空", "D", "数据缺失", "⚫ 系统问题：不是票的问题，检查 DuckDB 该票日线"),
    ("实时快照空", "D", "数据缺失", "⚫ 系统问题：实时快照与合成均失败，检查数据源"),
    ("数据残缺", "D", "数据缺失", "⚫ 系统问题：安检判定数据不完整"),
    ("日线仅", "D", "数据不足", "⚫ 上市初期/停牌：数据不足 100 根，等积累够了自动重评"),
    ("上市不足", "D", "数据不足", "⚫ 上市初期：数据不足"),
    ("复权取错", "D", "数据异常", "⚫ 复权口径异常，需人工核查"),
    # [V31-AB fix] 这两条本质是「日线缺列」= 数据级故障（原 advice 文案就写 ⚫，
    # 却登记成 L3 自相矛盾）。改 D 层，语义更准，不触碰任何判定逻辑。
    ("量价背离检查缺少", "D", "数据缺失", "⚫ 系统问题：日线缺成交量列，无法做量价背离判定"),
    ("回撤检查缺少", "D", "数据缺失", "⚫ 系统问题：日线缺成交量列，无法做回撤判定"),

    # ---------------- L1 否决级 ----------------
    ("直通车超载", "L1", "名额限制", "⚪ 非票的问题：限流已满，明日可重试"),
    ("全球限流", "L1", "名额限制", "⚪ 非票的问题：限流已满，明日可重试"),
    ("黑名单", "L1", "自设黑名单", "⚪ 冷却期内不碰，等冷却结束自动解禁"),
    ("流动性枯竭", "L1", "流动性不足", "⚪ 长期排除：买不进卖不出"),
    ("机构审美", "L1", "单价过高", "⚪ 可人工看：只是不符合机构价格口径，非死症"),
    ("流通市值不足", "L1", "规模不符", "⚪ 长期排除：P1 定位中大市值，小盘不在池内"),
    ("亿以下流通市值全局拦截", "L1", "规模不符", "⚪ 长期排除：P1 定位中大市值，小盘不在池内"),

    # ---------------- L2 拦截级（可复活，最需要盯）----------------
    ("MA20下方", "L2", "信号已破", "🟡 等站回 MA20 并企稳 2 日可重评"),
    ("深海防线", "L2", "趋势走坏", "🟡 等趋势修复：连续 2 日收在 MA20 下方"),
    ("右侧地基", "L2", "趋势未确认", "🟡 等站上 MA60：右侧信号未成立"),
    ("MACD过弱", "L2", "动能不足", "🟡 等红柱翻正并走扩可重评"),
    ("破位洗盘", "L2", "形态破坏", "🟡 需重新筑底：跌势止住、重新站回均线组"),
    ("量价背离", "L2", "量价不配合", "🟡 等放量确认：缩量背离可反转，需成交配合"),
    ("回撤超标", "L2", "深度回撤", "🟡 等修复：需大幅收复跌幅"),
    ("筹码重压", "L2", "上方套牢盘", "🟡 等放量突破成本上沿，或缩量洗盘"),
    ("均线未呈多头", "L2", "趋势结构差", "🟡 等均线修复：粘合后可能金叉"),
    ("偏离主峰", "L2", "筹码位置高", "🟡 等回踩主峰：回踩成本中位可再评估"),
    ("偏离主峰过远", "L2", "筹码位置高", "🟡 等回踩主峰：回踩成本中位可再评估"),
    ("股性呆滞", "L2", "活跃度不足", "🟡 缩量可能是蓄势也可能没资金，看近 5 日资金流"),
    ("乖离率极危", "L2", "偏离过大", "🟡 等回踩均线：BIAS20 超过 22%"),
    ("PE极度超标", "L2", "估值极端", "🟡 等估值消化或业绩兑现"),
    ("特殊事件拦截", "L2", "事件风险", "🟡 看事件类型：确认无风险后可重评"),
    ("财报窗", "L2", "披露窗口", "🟡 等披露落地：财报前后 3 日波动放大，业绩雷未出前不接"),

    # ---------------- L3 降权级 ----------------
    ("PE相对超标", "L3", "估值偏高", "🟠 仅扣分：基本面无问题，看行业估值中枢"),
    ("PE极值异常", "L3", "数据口径", "🟠 PE ≤0 或 >300，估值分失去意义"),
    ("流通市值", "L3", "市值档位", "🟠 市值档位相关扣分"),
]

# ============================================================================
# 匹配与查询
# ============================================================================
# 预编译为小写，避免每次调用都重复 lower()
_RULES_LOWER: List[Tuple[str, str, str, str, str]] = [
    (kw.lower(), level, cat, advice, kw)
    for (kw, level, cat, advice) in DEATH_CAUSE_RULES
]


def classify_death_cause(reason: Any) -> Dict[str, Any]:
    """
    把一条死因文案映射为层级信息。**永不抛出、永不返回 None。**

    返回 dict：
        level        L0 / L1 / L2 / L3 / D
        level_cn     层级中文名
        badge        emoji 徽标
        action       处置动作
        color        UI 着色用十六进制
        category     死因分类
        advice       处置建议（完整一句话）
        desc         层级含义
        matched      命中的登记关键词（未命中为 None）
        registered   是否命中登记表（False 说明需要补登记）

    空 reason / None → 一律按 D（数据级）处理，因为「说不出为什么」
    和「数据没搞清」是同一类问题，绝不能当成策略判断。
    """
    try:
        text = str(reason or "").strip()
        if not text:
            # [V31-AB fix] 根因：空死因曾复用 UNKNOWN_LEVEL(L3)，
            # 与本函数文档「空 reason → D 数据级」的约定不一致，
            # 会把系统级异常淘汰误显示成「只是不够好」。改为专用 D 兜底。
            return dict(EMPTY_REASON_LEVEL)

        low = text.lower()
        for kw_low, level, cat, advice, kw_orig in _RULES_LOWER:
            if kw_low in low:
                meta = LEVEL_META.get(level) or LEVEL_META["L3"]
                return {
                    "level": level,
                    "level_cn": meta["level_cn"],
                    "badge": meta["badge"],
                    "action": meta["action"],
                    "color": meta["color"],
                    "category": cat,
                    "advice": advice,
                    "desc": meta["desc"],
                    "matched": kw_orig,
                    "registered": True,
                }

        # 未登记 → 兜底 L3 + 未分类（绝不静默丢弃）
        out = dict(UNKNOWN_LEVEL)
        out["matched"] = None
        return out
    except Exception:
        # 极端脏输入（如对象 __str__ 抛异常）也必须安全返回
        return dict(UNKNOWN_LEVEL)


def classify_gate_death_cause(reason: Any, keyword: str) -> Dict[str, Any]:
    """按「本次确认触发的硬闸关键词」分类层级，而不是对整条原文首命中。

    根因：classify_death_cause() 对复合死因文案只取第一个命中规则。例如一条文案里同时含「流通市值」与「股性呆滞」时，
    它会把整条原文的层级都取成首个规则的层级。在 p1_explain._infer_hard_gates 里，
    每个闸门都需要知道自己对应那个闸的层级，不能被其他闸的关键词带跑。

    规则：只在「keyword 已确认出现在 reason 中」的前提下调用；找不到对应规则时回退到整文首命中。
    """
    try:
        text = str(reason or "").strip()
        kw = str(keyword or "").strip()
        if not text or not kw:
            return classify_death_cause(reason)
        low = text.lower()
        if kw.lower() not in low:
            return classify_death_cause(reason)
        kw_low = kw.lower()
        for kw_rule_low, level, cat, advice, kw_orig in _RULES_LOWER:
            # 关键词互为子串关系（如「偏离主峰过远」 vs 「偏离主峰」）
            if (kw_rule_low in kw_low or kw_low in kw_rule_low) and kw_rule_low in low:
                meta = LEVEL_META.get(level) or LEVEL_META["L3"]
                return {
                    "level": level,
                    "level_cn": meta["level_cn"],
                    "badge": meta["badge"],
                    "action": meta["action"],
                    "color": meta["color"],
                    "category": cat,
                    "advice": advice,
                    "desc": meta["desc"],
                    "matched": kw_orig,
                    "registered": True,
                }
        return classify_death_cause(reason)
    except Exception:
        return dict(UNKNOWN_LEVEL)


def classify_many(reasons: List[Any]) -> List[Dict[str, Any]]:
    """批量分类（保持与输入一一对应）。"""
    return [classify_death_cause(r) for r in (reasons or [])]


def level_distribution(reasons: List[Any]) -> Dict[str, int]:
    """统计各层级死因数量。用于 UI 概览与「今天卡在哪一层」。"""
    out: Dict[str, int] = {}
    for r in (reasons or []):
        lvl = classify_death_cause(r).get("level") or "L3"
        out[lvl] = out.get(lvl, 0) + 1
    return out


def enrich_rejected_rows(rows: List[Dict[str, Any]],
                        reason_key: str = "淘汰死因") -> List[Dict[str, Any]]:
    """
    给阵亡明细行**就地**补上 `死因层级` / `处置建议` 两个字段。

    只加键不改值：原始 `淘汰死因` 文本一字不动，保证历史可比。
    """
    for row in (rows or []):
        if not isinstance(row, dict):
            continue
        info = classify_death_cause(row.get(reason_key))
        row["死因层级"] = info["level"]
        row["处置建议"] = info["advice"]
        # 便于 UI 直接取色，无需再查表
        row["层级徽标"] = "%s %s" % (info["badge"], info["level_cn"])
    return rows


__all__ = [
    "LEVEL_META",
    "UNKNOWN_LEVEL",
    "EMPTY_REASON_LEVEL",
    "DEATH_CAUSE_RULES",
    "classify_death_cause",
    "classify_gate_death_cause",
    "classify_many",
    "level_distribution",
    "enrich_rejected_rows",
]
