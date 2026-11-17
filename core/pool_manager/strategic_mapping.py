# -*- coding: utf-8 -*-
"""
core/pool_manager/strategic_mapping.py
原 core/pool_manager.py L579-L1031

【职责】
- STRATEGIC_INDUSTRIES：12 个 T0/T1/T3 战略白名单基准权重（电子/计算机/通信 T0=15 等）
- _STRATEGIC_RANK_*：动态贝塔窗口参数
- _sector_rank_recent_date_keys / _strategic_window_weights：日期/权重工具
- _industry_tokens / _industry_theme_seed：自动学习名相似度
- _build_auto_strategic_mapping：自动学习 细分行业 -> 战略一级行业 映射
- _aggregate_sector_snapshot_for_strategic：聚合快照代表排名
- _calc_sector_rank_trend_context：板块趋势上下文（申万 K 线优先，回退排名历史）
- _get_dynamic_strategic_industries：游资嗅觉记忆中枢（全自动升降级 + 落盘）

【依赖】
- core.pool_manager.paths（path_sector_rank_history_json / path_strategic_mapping_debug_json / ensure_runtime_data_layout）
- data.db_core.get_sw_sector_trend_context（容错导入）
"""
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Set, Tuple

import numpy as np

from core.pool_manager.paths import (
    ensure_runtime_data_layout,
    path_sector_rank_history_json,
    path_strategic_mapping_debug_json,
)

logger = logging.getLogger(__name__)

# ==================== 👑 时代贝塔：战略白名单 (基准配置) ====================
# 说明：
# - 行业名须与 Tushare stock_basic.industry（多为申万一级口径）完全一致，否则无法命中。
# - 12 分档：科技主链、高端装备、国家战略与近年机构重仓的成长制造方向（AI/算力、新能源车链、电力设备等）。
# - 8 分档：消费、周期、大金融弹性、农业等，偏轮动与防御/复苏，可由盘面热度「晋级」至 12。
STRATEGIC_INDUSTRIES = {
    # —— T0 基准 15 分（动态可降至 12）：2026 慢牛核心赛道 ——
    # 硬科技（AI/算力/半导体）：科创50核心，全年强势领涨，机构主导
    "电子": 15.0,
    "计算机": 15.0,
    "通信": 15.0,
    # 半导体/光模块：科创板主力，国产替代+AI算力双轮驱动
    # —— T1 基准 12 分（动态可升至 15）：成长制造主战场 ——
    "国防军工": 12.0,
    "机械设备": 12.0,
    "电力设备": 12.0,
    "汽车": 12.0,
    "医药生物": 12.0,        # 2026年创新药/器械景气修复，长线资金配置增加
    # —— 基准 8 分（动态可升至 12）：消费、周期、金融弹性等 ——
    "有色金属": 8.0,
    "基础化工": 8.0,
    "传媒": 8.0,
    "家用电器": 8.0,
    "食品饮料": 8.0,
    "社会服务": 8.0,
    "农林牧渔": 8.0,
    "非银金融": 8.0,
}

# 动态贝塔：仅看「最近若干条」板块排名快照，贴近当前资金风格；过旧样本不参与判定。
_STRATEGIC_RANK_RECENT_WINDOW = 5
# 至少积累这么多天的快照再启用升降级，避免冷启动两三天就误伤。
_STRATEGIC_RANK_MIN_SNAPSHOTS = 3
# 近端窗口权重：越近越重要，避免 5 日前旧热度与当下资金抢主导。
_STRATEGIC_RANK_WINDOW_WEIGHTS = [0.10, 0.15, 0.20, 0.25, 0.30]
# 12 分主线若加权热度长期缺席，则降到 10；8 分行业若强势聚集，则晋级到 12。
_STRATEGIC_DOWNGRADE_SCORE_MAX = 0.85
_STRATEGIC_UPGRADE_SCORE_MIN = 1.75

# 自动聚合学习参数：不再维护手写别名表，而是从历史快照中的名称共现、词缀相似度和市场常用语义锚点
# 动态推断「细分行业 -> 战略一级行业」。该映射只用于动态贝塔热度聚合，不改股票自身行业归属。
_STRATEGIC_AUTO_LINK_MIN_SCORE = 0.62
_STRATEGIC_AUTO_LINK_PREFIX_BONUS = 0.42
_STRATEGIC_AUTO_LINK_TOKEN_BONUS = 0.36
_STRATEGIC_AUTO_LINK_THEME_BONUS = 0.30
_STRATEGIC_AUTO_LINK_COOCCUR_BONUS = 0.22
_STRATEGIC_AUTO_LINK_ROOT_SELF_SCORE = 1.0
_SECTOR_TREND_LOOKBACK_BREAKOUT = 20
_SECTOR_TREND_RECENT_SHORT = 3
_SECTOR_TREND_RECENT_MID = 5


def _sector_rank_recent_date_keys(history_data: dict, window: int) -> list:
    """取已排序日期键的最近 window 条，用于近端资金热度判定。"""
    if not isinstance(history_data, dict) or not history_data:
        return []
    sd = sorted(history_data.keys())
    return sd[-window:] if len(sd) > window else sd


def _strategic_window_weights(n: int) -> List[float]:
    if n <= 0:
        return []
    base = list(_STRATEGIC_RANK_WINDOW_WEIGHTS)
    if n == len(base):
        return base
    if n < len(base):
        trimmed = base[-n:]
        s = sum(trimmed) or 1.0
        return [float(x / s) for x in trimmed]
    extra = [1.0] * (n - len(base)) + base
    s = sum(extra) or 1.0
    return [float(x / s) for x in extra]


def _industry_tokens(ind: str) -> Set[str]:
    text = str(ind or "").strip()
    if not text:
        return set()
    # 兼容中文短词语义：按常见双字词切片，再补整体词，供自动聚类评分使用。
    tokens: Set[str] = {text}
    if len(text) >= 2:
        for i in range(len(text) - 1):
            tk = text[i:i + 2].strip()
            if tk:
                tokens.add(tk)
    return tokens


def _industry_theme_seed(root_ind: str) -> Set[str]:
    text = str(root_ind or "").strip()
    tokens = _industry_tokens(text)
    if text == "电子":
        tokens |= {"半导", "元器", "芯片", "光学"}
    elif text == "计算机":
        tokens |= {"软件", "互联", "it", "算力", "数据"}
    elif text == "通信":
        tokens |= {"通信", "电信", "光通", "运营"}
    elif text == "国防军工":
        tokens |= {"军工", "航空", "船舶", "航天"}
    elif text == "机械设备":
        tokens |= {"机械", "机床", "设备", "基件", "工程"}
    elif text == "电力设备":
        tokens |= {"电力", "电气", "储能", "光伏", "电网"}
    elif text == "汽车":
        tokens |= {"汽车", "整车", "配件", "摩托"}
    elif text == "医药生物":
        tokens |= {"医药", "制药", "生物", "医疗"}
    elif text == "有色金属":
        tokens |= {"金属", "黄金", "铜", "铝", "铅锌"}
    elif text == "基础化工":
        tokens |= {"化工", "化纤", "塑料", "染料", "农药"}
    elif text == "传媒":
        tokens |= {"传媒", "影视", "出版", "广告", "文教"}
    elif text == "家用电器":
        tokens |= {"家电", "电器"}
    elif text == "食品饮料":
        tokens |= {"食品", "饮料", "白酒", "啤酒", "乳制"}
    elif text == "社会服务":
        tokens |= {"旅游", "酒店", "餐饮", "服务"}
    elif text == "农林牧渔":
        tokens |= {"农业", "种植", "渔业", "饲料", "牧渔"}
    elif text == "非银金融":
        tokens |= {"证券", "保险", "金融"}
    return {str(t).lower() for t in tokens if str(t).strip()}


def _build_auto_strategic_mapping(history_data: dict, current_ranking_dict: dict) -> Tuple[Dict[str, str], List[str]]:
    """根据历史快照自动学习 细分行业 -> 战略一级行业 映射。"""
    root_inds = [str(k).strip() for k in STRATEGIC_INDUSTRIES.keys() if str(k).strip()]
    all_names: Set[str] = set(root_inds)
    if isinstance(current_ranking_dict, dict):
        all_names |= {str(k).strip() for k in current_ranking_dict.keys() if str(k).strip()}
    if isinstance(history_data, dict):
        for snap in history_data.values():
            if not isinstance(snap, dict):
                continue
            all_names |= {str(k).strip() for k in snap.keys() if str(k).strip()}

    root_theme_tokens = {root: _industry_theme_seed(root) for root in root_inds}
    occurrence_days: Dict[str, Set[str]] = {name: set() for name in all_names}
    for dt, snap in (history_data or {}).items():
        if not isinstance(snap, dict):
            continue
        for raw_name in snap.keys():
            name = str(raw_name).strip()
            if name:
                occurrence_days.setdefault(name, set()).add(str(dt))

    learned: Dict[str, str] = {root: root for root in root_inds}
    explain: List[str] = []
    for name in sorted(all_names):
        if not name:
            continue
        if name in STRATEGIC_INDUSTRIES:
            learned[name] = name
            continue
        best_root = ""
        best_score = -1.0
        name_tokens = {str(t).lower() for t in _industry_tokens(name)}
        name_days = occurrence_days.get(name, set())
        for root in root_inds:
            score = 0.0
            root_tokens = {str(t).lower() for t in _industry_tokens(root)}
            if name == root:
                score = _STRATEGIC_AUTO_LINK_ROOT_SELF_SCORE
            else:
                if name.startswith(root) or root.startswith(name):
                    score += _STRATEGIC_AUTO_LINK_PREFIX_BONUS
                token_inter = name_tokens & root_tokens
                if token_inter:
                    score += _STRATEGIC_AUTO_LINK_TOKEN_BONUS
                if name_tokens & root_theme_tokens.get(root, set()):
                    score += _STRATEGIC_AUTO_LINK_THEME_BONUS
                root_days = occurrence_days.get(root, set())
                if name_days and root_days:
                    overlap = name_days & root_days
                    if overlap:
                        score += _STRATEGIC_AUTO_LINK_COOCCUR_BONUS
            if score > best_score:
                best_score = score
                best_root = root
        if best_root and best_score >= _STRATEGIC_AUTO_LINK_MIN_SCORE:
            learned[name] = best_root
            explain.append(f"{name}->{best_root}@{best_score:.2f}")

    return learned, explain


def _aggregate_sector_snapshot_for_strategic(snapshot: dict, learned_mapping: Dict[str, str]) -> Dict[str, int]:
    """将快照中的细分行业按自动学习映射聚合为战略一级行业代表排名（取最强名次）。"""
    agg: Dict[str, int] = {}
    if not isinstance(snapshot, dict):
        return agg
    for raw_ind, rank_val in snapshot.items():
        root_ind = learned_mapping.get(str(raw_ind).strip())
        if not root_ind:
            continue
        try:
            rk = int(rank_val)
        except (TypeError, ValueError):
            continue
        if rk <= 0:
            continue
        old = agg.get(root_ind)
        if old is None or rk < old:
            agg[root_ind] = rk
    return agg


def _calc_sector_rank_trend_context(history_data: dict, current_ranking_dict: dict) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, float], List[str]]:
    """
    板块趋势上下文：优先读取申万行业指数 K 线；若本地未同步或无匹配数据，回退到
    sector_rank_history 的"板块强度新高/趋势改善"近似识别。
    """
    learned_mapping, learned_explain = _build_auto_strategic_mapping(history_data, current_ranking_dict)
    try:
        from data.db_core import get_sw_sector_trend_context

        sw_context = get_sw_sector_trend_context(list(STRATEGIC_INDUSTRIES.keys()), limit=120)
        if isinstance(sw_context, dict) and sw_context:
            sw_bonus = {
                str(k): float(v.get("trend_bonus", 0.0) or 0.0)
                for k, v in sw_context.items()
                if isinstance(v, dict) and float(v.get("trend_bonus", 0.0) or 0.0) > 0
            }
            if sw_bonus:
                learned_explain = ["趋势源=申万行业K线"] + learned_explain
                return sw_context, sw_bonus, learned_explain
    except Exception as e:
        logger.debug("申万行业K线趋势上下文不可用，回退排名历史: %s", e)
    all_dates = _sector_rank_recent_date_keys(history_data, _SECTOR_TREND_LOOKBACK_BREAKOUT)
    if not all_dates:
        return {}, {}, learned_explain

    agg_by_date: Dict[str, Dict[str, int]] = {
        dt: _aggregate_sector_snapshot_for_strategic(history_data.get(dt, {}), learned_mapping)
        for dt in all_dates
    }
    context: Dict[str, Dict[str, Any]] = {}
    bonus_map: Dict[str, float] = {}
    roots = [str(k).strip() for k in STRATEGIC_INDUSTRIES.keys() if str(k).strip()]
    for root in roots:
        ranks = [int(agg_by_date.get(dt, {}).get(root, 999) or 999) for dt in all_dates]
        valid_ranks = [r for r in ranks if r < 999]
        if not valid_ranks:
            continue
        today_rank = valid_ranks[-1]
        best_rank = min(valid_ranks)
        prev_valid = valid_ranks[:-1]
        prev_best = min(prev_valid) if prev_valid else 999
        short_ranks = valid_ranks[-_SECTOR_TREND_RECENT_SHORT:]
        mid_ranks = valid_ranks[-_SECTOR_TREND_RECENT_MID:]
        short_mean = sum(short_ranks) / float(len(short_ranks))
        mid_mean = sum(mid_ranks) / float(len(mid_ranks))
        rank_breakout_20 = bool(today_rank <= prev_best and today_rank <= 12)
        top8_persist = bool(short_mean <= 8.0)
        top12_persist = bool(mid_mean <= 12.0)
        trend_improving = bool(len(mid_ranks) >= 3 and mid_ranks[-1] <= mid_ranks[0] - 3)

        trend_bonus = 0.0
        if rank_breakout_20:
            trend_bonus += 1.5
        if rank_breakout_20 and top12_persist:
            trend_bonus += 0.8
        if top8_persist:
            trend_bonus += 0.7
        elif trend_improving:
            trend_bonus += 0.5
        # 排名前 3 已有 rank_bonus，趋势分只轻度确认，避免同一热度重复放大。
        if today_rank <= 3:
            trend_bonus = min(trend_bonus, 2.4)
        trend_bonus = round(float(np.clip(trend_bonus, 0.0, 3.0)), 2)

        # 【V26.88+10-3 P1-⑦】动态贝塔双窗口：30 日长端保护慢热行业
        # 长端热度：基于更长窗口（30 日）的 rank 数据，慢热型行业（如医药生物）不会被短窗降权
        _long_window = min(30, len(valid_ranks))
        if _long_window >= 10:
            long_ranks = valid_ranks[-_long_window:]
            long_mean = sum(long_ranks) / float(len(long_ranks))
            long_top12_persist = bool(long_mean <= 12.0)
            # 当短窗弱势（short_mean > 12）但长窗强势（long_mean ≤ 12）→ 标记为"慢热型"
            if short_mean > 12.0 and long_top12_persist:
                context[root]["slow_heat_protected"] = True
                # 把 trend_bonus 至少拉到 +0.5，避免被降权
                trend_bonus = max(trend_bonus, 0.5)

        context[root] = {
            "today_rank": int(today_rank),
            "best_rank_lookback": int(best_rank),
            "prev_best_rank": int(prev_best if prev_best < 999 else today_rank),
            "recent_rank_mean_3": round(float(short_mean), 2),
            "recent_rank_mean_5": round(float(mid_mean), 2),
            "rank_breakout_20": rank_breakout_20,
            "top8_persist": top8_persist,
            "top12_persist": top12_persist,
            "trend_improving": trend_improving,
            "trend_bonus": trend_bonus,
        }
        if trend_bonus > 0:
            bonus_map[root] = trend_bonus

    return context, bonus_map, learned_explain


# ================= 🧠 游资嗅觉记忆中枢 (全自动升降级) =================
def _get_dynamic_strategic_industries(current_ranking_dict, p1_anchor_yyyymmdd=None):
    history_file = path_sector_rank_history_json()
    bj_tz = timezone(timedelta(hours=8))
    anchor = str(p1_anchor_yyyymmdd or "").strip()
    if len(anchor) == 8 and anchor.isdigit():
        history_key = anchor
    else:
        history_key = datetime.now(bj_tz).strftime("%Y%m%d")

    sorted_inds = list(current_ranking_dict.keys())
    today_ranks = {ind: (i + 1) for i, ind in enumerate(sorted_inds)}

    history_data = {}
    if os.path.exists(history_file):
        try:
            with open(history_file, 'r', encoding='utf-8') as f:
                history_data = json.load(f)
        except json.JSONDecodeError as e:
            logger.warning(f"[history-json] {history_file}: {e}")
            history_data = {}
        except OSError as e:
            logger.warning(f"[history-io] {history_file}: {e}")
            history_data = {}
        except Exception as e:
            logger.warning(f"[history-unknown] {history_file}: {e}")
            history_data = {}

    # 【修复】当日快照已存在（同一日历日多次运行）→ 复用上次已聚合的 dynamic_dict，
    # 避免边读边写导致自反馈循环，造成同日多次运行结果不一致。
    debug_json_path_actual = path_strategic_mapping_debug_json(history_key)
    if history_key in history_data and os.path.exists(debug_json_path_actual):
        try:
            with open(debug_json_path_actual, 'r', encoding='utf-8') as f:
                debug_payload = json.load(f)
            cached_scores = debug_payload.get("dynamic_scores", {})
            if cached_scores:
                cached_dict = {k: float(v) for k, v in cached_scores.items() if not str(k).startswith("__")}
                if cached_dict:
                    logger.info(
                        "[动态行业贝塔] 复用已有快照 | date=%s | cached_count=%d",
                        history_key,
                        len(cached_dict),
                    )
                    bonus_map = debug_payload.get("sector_trend_bonus_map", {})
                    ctx_map = debug_payload.get("sector_trend_context", {})
                    if bonus_map:
                        cached_dict["__sector_trend_bonus__"] = bonus_map
                    if ctx_map:
                        cached_dict["__sector_trend_context__"] = ctx_map
                    return cached_dict
        except Exception:
            pass

    # ---------- 正常路径：快照不存在，需计算并落盘 ----------
    if today_ranks:
        history_data[history_key] = today_ranks
        logger.info("[动态行业贝塔] 新增当日快照 | date=%s | n=%d", history_key, len(today_ranks))

    sorted_dates = sorted(history_data.keys())
    if len(sorted_dates) > 10:
        for d in sorted_dates[:-10]:
            del history_data[d]

    ensure_runtime_data_layout()
    try:
        with open(history_file, 'w', encoding='utf-8') as f:
            json.dump(history_data, f, ensure_ascii=False)
    except Exception as e:
        logger.error(f"嗅觉记忆库写入失败: {e}")

    dynamic_dict = STRATEGIC_INDUSTRIES.copy()
    sector_trend_context, sector_trend_bonus_map, _trend_learned_explain = _calc_sector_rank_trend_context(
        history_data, current_ranking_dict
    )
    if sector_trend_bonus_map:
        dynamic_dict["__sector_trend_bonus__"] = sector_trend_bonus_map
        dynamic_dict["__sector_trend_context__"] = sector_trend_context

    recent_dates = _sector_rank_recent_date_keys(history_data, _STRATEGIC_RANK_RECENT_WINDOW)
    if len(history_data) < _STRATEGIC_RANK_MIN_SNAPSHOTS or len(recent_dates) < _STRATEGIC_RANK_MIN_SNAPSHOTS:
        return dynamic_dict

    learned_mapping, learned_explain = _build_auto_strategic_mapping(history_data, current_ranking_dict)
    recent_weights = _strategic_window_weights(len(recent_dates))
    weighted_snapshots = []
    for dt, weight in zip(recent_dates, recent_weights):
        agg_snapshot = _aggregate_sector_snapshot_for_strategic(history_data.get(dt, {}), learned_mapping)
        weighted_snapshots.append((dt, float(weight), agg_snapshot))

    downgraded: List[str] = []
    upgraded: List[str] = []
    evidence_lines: List[str] = []

    for ind, base_score in dynamic_dict.items():
        weighted_top8 = 0.0
        weighted_top3 = 0.0
        hit_days = 0
        evidence_parts: List[str] = []

        for dt, weight, agg_snapshot in weighted_snapshots:
            rank_val = int(agg_snapshot.get(ind, 999) or 999)
            if rank_val <= 8:
                weighted_top8 += weight
                hit_days += 1
            if rank_val <= 3:
                weighted_top3 += weight
            evidence_parts.append(f"{dt[-4:]}#{rank_val if rank_val < 999 else '--'}@{weight:.2f}")

        action = "保持"
        target_score = base_score

        # 【V26.88+10-3 P1-⑦】慢热型行业保护：长端热度在场时跳过降权
        _is_slow_heat = bool(sector_trend_context.get(ind, {}).get("slow_heat_protected", False))

        if base_score == 12.0 and weighted_top8 <= _STRATEGIC_DOWNGRADE_SCORE_MAX and not _is_slow_heat:
            target_score = 10.0
            action = "12→10"
            downgraded.append(ind)
        elif base_score == 8.0 and weighted_top3 >= _STRATEGIC_UPGRADE_SCORE_MIN:
            target_score = 12.0
            action = "8→12"
            upgraded.append(ind)
        elif _is_slow_heat and base_score == 12.0 and weighted_top8 <= _STRATEGIC_DOWNGRADE_SCORE_MAX:
            action = "12→10(skip-长端热度保护)"

        dynamic_dict[ind] = target_score
        evidence_lines.append(
            f"{ind}:{action}(top8={weighted_top8:.2f},top3={weighted_top3:.2f},hits={hit_days}/{len(weighted_snapshots)}|{'/'.join(evidence_parts)})"
        )

    debug_payload = {
        "date": history_key,
        "recent_dates": list(recent_dates),
        "window_weights": [round(float(w), 4) for w in recent_weights],
        "learned_mapping": learned_mapping,
        "learned_mapping_explain": learned_explain,
        "sector_trend_context": sector_trend_context,
        "sector_trend_bonus_map": sector_trend_bonus_map,
        "downgraded": list(downgraded),
        "upgraded": list(upgraded),
        "dynamic_scores": {k: float(v) for k, v in dynamic_dict.items() if not str(k).startswith("__")},
        "evidence": list(evidence_lines),
    }
    try:
        debug_json_path = path_strategic_mapping_debug_json(history_key)
        with open(debug_json_path, 'w', encoding='utf-8') as f:
            json.dump(debug_payload, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning("动态行业贝塔调试缓存写入失败: %s", e)

    logger.info(
        "[动态行业贝塔] 自动聚合窗口=%s | 权重=%s | 学习映射=%s | 板块趋势加分=%s | 调试缓存=%s | 12→10 降权: %s | 8→12 晋级: %s | 证据: %s",
        len(recent_dates),
        "/".join(f"{w:.2f}" for w in recent_weights),
        "、".join(learned_explain[:24]) if learned_explain else "无新增",
        "、".join(f"{k}+{v:.1f}" for k, v in sector_trend_bonus_map.items()) if sector_trend_bonus_map else "无",
        path_strategic_mapping_debug_json(history_key),
        "、".join(downgraded) if downgraded else "无",
        "、".join(upgraded) if upgraded else "无",
        " ; ".join(evidence_lines),
    )

    return dynamic_dict


__all__ = [
    "STRATEGIC_INDUSTRIES",
    "_sector_rank_recent_date_keys", "_strategic_window_weights",
    "_industry_tokens", "_industry_theme_seed",
    "_build_auto_strategic_mapping", "_aggregate_sector_snapshot_for_strategic",
    "_calc_sector_rank_trend_context",
    "_get_dynamic_strategic_industries",
]