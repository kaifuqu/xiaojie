# -*- coding: utf-8 -*-
"""
core/strategy_id_registry.py
统一原子战法 ID（Atomic Strategy ID）规范层

【背景】
历史 signal_log 的 strategy 列存的是「人话战法串」，例如：
    "观察 / 分时偏弱 / 趋势确认较强，延续更顺 | 👑[主线保送] 🚀[黄金十绝] | P5-09·★趋势确认 P5-08·★趋势质量 P5-07·★趋势结构"
其中只有 "P5-09 / P5-08 / P5-07" 是可统计的原子战法，其余是描述性文案。
战法有效性评估（T+N 胜率）必须按原子 ID 聚合，否则同一条信号会被拆成十几个「战法」，
其中大部分永远只有 1 条样本、胜率无意义。

【职责】
- parse_atomic_strategy_ids(raw)          : 从任意文本抽出去重后的原子 ID 列表
- parse_labeled_atomic_strategies(raw)    : 抽出 (ID, 中文标签) 列表，标签优先取 "ID·★标签" 形式
- canonical_label(id) / display_name(id)  : 规范标签与展示名
- pool_of(id)                             : 由 ID 前缀推断所属池（p1~p5），无法判断返回 ""
- is_low_confidence(raw)                  : 判断战法串是否低置信（无原子 ID / 纯描述文案）

【红线】
本模块是纯函数解析层，不新增战法、不改策略、不参与任何过滤或打分；
解析失败一律返回空列表，调用方必须容忍空结果，保证推送主链路不受影响。
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple


# 原子战法 ID 形如 P5-09 / P4-01 / P3-10 / P1-07B
# 说明：[1-5] 池编号；-\d{2} 战法序号；[A-Z] 可选分档后缀
# （V35 编号重整后现行编号无字母后缀，后缀仅用于兼容历史 signal_log 的 05A/05B/05C 等旧写法）
_ATOM_ID_RE = re.compile(r"(?<![A-Za-z0-9])([Pp][1-5]-\d{2}[A-Za-z]?)(?![A-Za-z0-9])")

_ID_NORMALIZE_RE = re.compile(r"^([Pp])([1-5])-(\d{2})([A-Za-z]?)$")

# 「P5-09·★趋势确认」这类带标签写法：
# 使用 |｜ ： 逗号、句号、括号、顷等做「条目级」分隔，中点 · 必须保留（它是 ID 与标签的分隔符）。
# 修正（2026-10-13）：原正则未分割英文逗号与句号，
# 使「P5-05B·★趋势质量」这类下标签把后面的标签一并吞掉
# （实测质量标签被附带到 JSON 尾括号）。
_LABEL_SPLIT_RE = re.compile(r"[｜|：:, 、；（）()\[\]{}，。.\s]+")
_LABEL_PREFIX_RE = re.compile(r"^[★☆⭐🌟]*")

# 明确表示「非可统计战法」的描述性前缀
_NON_ATOM_PREFIXES = ("观察", "分时偏弱", "主线保送", "黄金十绝", "缩量期备选", "仅观察")


def canonical_id(raw) -> Optional[str]:
    """
    把任意写法的原子 ID 规范成唯一大写形式（p5-09 -> P5-09；历史字母后缀 P5-05a -> P5-05A 兼容）。
    非原子 ID 返回 None。
    """
    if raw is None:
        return None
    try:
        s = str(raw).strip()
    except Exception:
        return None
    if not s:
        return None
    m = _ID_NORMALIZE_RE.match(s)
    if not m:
        return None
    return "P%s-%s%s" % (m.group(2), m.group(3), m.group(4).upper())


def parse_atomic_strategy_ids(raw) -> List[str]:
    """
    从任意结构（str / list / tuple / dict 嵌套）中抽取去重后的原子战法 ID。

    返回顺序稳定：按首次出现顺序去重，便于日志与 UI 展示可复现。
    解析失败（None / 空 / 非原子文本）返回空列表，不抛异常。
    """
    out: List[str] = []
    seen = set()
    for token in _iter_text_tokens(raw):
        for m in _ATOM_ID_RE.finditer(token):
            cid = canonical_id(m.group(1))
            if cid and cid not in seen:
                seen.add(cid)
                out.append(cid)
    return out


def parse_labeled_atomic_strategies(raw) -> List[Tuple[str, str]]:
    """
    抽取 (原子ID, 中文标签) 列表。

    识别两种写法：
      1) "P5-09·★趋势确认"  -> ("P5-09", "趋势确认")
      2) "P5-08"（无标签）   -> ("P5-08", "")
    若同一 ID 多次出现，保留第一个非空标签。
    """
    order: List[str] = []
    labels: Dict[str, str] = {}
    for token in _iter_text_tokens(raw):
        parts = [p for p in _LABEL_SPLIT_RE.split(token) if p.strip()]
        for part in parts:
            m = _ATOM_ID_RE.search(part)
            if not m:
                continue
            cid = canonical_id(m.group(1))
            if not cid:
                continue
            tail = part[m.end():].strip()
            tail = _LABEL_PREFIX_RE.sub("", tail).strip()
            # 进一步剥除 JSON 引号：strategies 列存的是
            # json.dumps(["P5-08·★趋势质量"]) = '["P5-08·★趋势质量"]'，
            # 未拆除引号时标签会带着 '"' 完成。
            tail = tail.strip("-—·|｜:： ，,。、；;'\"“”")
            if cid not in labels:
                labels[cid] = ""
                order.append(cid)
            if tail and not labels[cid]:
                labels[cid] = tail
    return [(cid, labels[cid]) for cid in order]


def canonical_label(strategy_id) -> str:
    """
    返回原子 ID 的规范展示标签（无内置标签表时回退为 ID 本身）。
    刻意不硬编码全部战法中文名，避免与策略文件双份维护产生漂移。
    """
    cid = canonical_id(strategy_id)
    if cid:
        return cid
    try:
        return str(strategy_id).strip() if strategy_id else ""
    except Exception:
        return ""


def display_name(strategy_id) -> str:
    """面向推送/UI 的展示名：规范 ID。"""
    return canonical_id(strategy_id) or ""


def pool_of(strategy_id) -> str:
    """由原子 ID 前缀推断池键：P5-07 -> 'p5'；非法返回 ''。"""
    cid = canonical_id(strategy_id)
    if not cid:
        return ""
    return "p" + cid[1]


def is_low_confidence(raw) -> bool:
    """
    低置信判定：战法串里抽不出任何原子 ID。

    含义：这条信号的「命中战法」只是描述性文案，无法归属到可统计战法，
    战法有效性聚合时应排除在分子之外（但不影响信号本身入库）。
    """
    return not parse_atomic_strategy_ids(raw)


def is_strategy_text_descriptive(raw) -> bool:
    """便捷判定：文本以描述性前缀开头（如「观察 / ...」）。"""
    for token in _iter_text_tokens(raw):
        t = token.strip()
        for p in _NON_ATOM_PREFIXES:
            if t.startswith(p):
                return True
    return False


def _iter_text_tokens(raw):
    """把 str / list / tuple / dict / None 递归摊平成文本片段流。"""
    if raw is None:
        return
    if isinstance(raw, str):
        yield raw
        return
    if isinstance(raw, dict):
        for v in raw.values():
            for t in _iter_text_tokens(v):
                yield t
        return
    if isinstance(raw, (list, tuple, set)):
        for v in raw:
            for t in _iter_text_tokens(v):
                yield t
        return
    try:
        yield str(raw)
    except Exception:
        return


__all__ = [
    "canonical_id",
    "parse_atomic_strategy_ids",
    "parse_labeled_atomic_strategies",
    "canonical_label",
    "display_name",
    "pool_of",
    "is_low_confidence",
    "is_strategy_text_descriptive",
]
