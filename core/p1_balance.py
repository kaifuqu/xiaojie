# -*- coding: utf-8 -*-
"""
小杰AI选股系统 Pro V26.80 — P1 选股策略平衡器

【现状问题】
P1 的及格线 + 排序逻辑存在"机构白马偏好"风险：

  1. ❌ 流通市值下限（默认 100 亿）——自动剔除小市值
  2. ❌ PE 满分偏向低 PE（白}马股天然占优）
  3. ❌ 北向资金高的优先（外资偏好 = 大白马）
  4. ❌ 板块排名权重（rank_bonus 5分）——头部行业垄断

导致的问题：
  - 资金抱团票年年霸榜
  - 题材股被低估
  - 风格漂移（大盘股 → 中盘股 → 小盘股 切换慢）

【本模块策略平衡器】
  1. 风格动态平衡：监管 P1 池中"大/中/小"市值比例
  2. 行业平衡：避免单一行业占比 > 30%
  3. 题材股保护：连板/龙头可突破市值下限
  4. 反向平衡：当资金抱团极度严重（白马估值高），强制纳入题材

【使用方式】
  在 build_p1_pool_and_cache 中调用 enforce_p1_balance(p1_pool, ...)
  若不满足平衡条件，从 p1_rejected 中回调高分标的补位
"""
from __future__ import annotations

import logging
import math
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

import numpy as np

try:  # pandas 只是 pool item 的载体类型，仅用于类型判定，不参与任何选股计算
    import pandas as _pd_for_json
except Exception:  # pragma: no cover - pandas 缺失时降级为不可判定
    _pd_for_json = None

logger = logging.getLogger(__name__)


def _json_default(o: Any) -> Any:
    """p1_rejected 落盘用的 JSON 兜底编码器。

    【P1-修复】UI 的 _save_rejected_json 会直接 json.dump(rejected_items)，而
    p1_balance 的「出口净化 / 劣币驱逐 / 超限截断」三条分支会把完整的 P1 item
    （内含 df: DataFrame 与 hist 里的 numpy 标量）追加回 rejected 列表，于是
    json.dump 抛 TypeError: Object of type DataFrame is not JSON serializable，
    整轮洗盘被 UI 判成失败。

    这里不改变任何选股/平衡判定，只把不可序列化的对象降级成可读摘要。
    """
    if _pd_for_json is not None:
        if isinstance(o, _pd_for_json.DataFrame):
            return {
                "__truncated_dataframe__": [str(c) for c in o.columns],
                "rows": int(len(o)),
            }
        if isinstance(o, _pd_for_json.Series):
            return {"__truncated_series__": str(o.name), "len": int(len(o))}
        if isinstance(o, _pd_for_json.Timestamp):
            return o.isoformat()
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, np.floating):
        fv = float(o)
        return fv if math.isfinite(fv) else None
    if isinstance(o, np.bool_):
        return bool(o)
    if isinstance(o, np.ndarray):
        return o.tolist()
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    if isinstance(o, (set, frozenset)):
        return list(o)
    if isinstance(o, Path):
        return str(o)
    if isinstance(o, (bytes, bytearray)):
        try:
            return bytes(o).decode("utf-8")
        except UnicodeDecodeError:
            return bytes(o).hex()
    return repr(o)


# ==================== 平衡参数 ====================
BALANCE_PARAMS = {
    "max_industry_pct": 0.30,            # 单一行业占比 < 30%
    "max_large_cap_pct": 0.50,            # 大盘股（>500亿）占比 < 50%
    "min_mid_cap_pct": 0.20,              # 中盘股（100-500亿）占比 >= 20%
    "max_top3_concentration": 0.45,       # 前 3 名累计占比 < 45%
    "enable_balance": True,                # 全局开关
    # 【P1-修复】劣币驱逐：替换阈值用 HHI(行业集中度)+ 大盘集中度环比改善度。
    "swap_min_hhi_improvement": 0.005,   # HHI 至少下降 0.5% 才允许替换
    "swap_min_large_improvement": 0.02,  # 大盘占比至少下降 2% 才允许替换
    # 【V30.14 2026-11-05】池大小不再设 1.5x 上限。
    # 旧值会让「平衡调整回调进来的票」被二次截断：分数达标的票只因池子已扩到 1.5 倍
    # 就被丢掉，与「分数够就入池、不限制股票数」的口径冲突。
    # 0 或负数 = 不截断；仍保留 target_size 作为 UI 展示口径。
    "max_pool_size_ratio": 0.0,          # 0 = 不限制池大小（分数达标即保留）
}


def _classify_mv(mv_yi: float) -> str:
    """按市值分类。"""
    if mv_yi >= 500.0:
        return "large"     # 大盘股
    elif mv_yi >= 100.0:
        return "mid"       # 中盘股
    elif mv_yi >= 30.0:
        return "small"     # 小盘股
    else:
        return "micro"     # 微盘股


def _get_item_mv_yi(item: Dict[str, Any]) -> float:
    """从 item 中提取流通市值亿元。"""
    if not isinstance(item, dict):
        return 0.0
    # hist/rt 都不存在或为 None 时按 0 处理
    rt = item.get("hist")
    if not isinstance(rt, dict):
        rt = item.get("rt")
    if not isinstance(rt, dict):
        return 0.0
    mv = rt.get("circ_mv", 0) or rt.get("total_mv", 0) * 0.6
    try:
        return float(mv) / 10000.0
    except (TypeError, ValueError):
        return 0.0


def _get_item_industry(item: Dict[str, Any]) -> str:
    """从 item 中提取行业。"""
    if not isinstance(item, dict):
        return "未知"
    pre_ind = item.get("_pre_ind")
    if pre_ind:
        return str(pre_ind)
    hist = item.get("hist")
    if isinstance(hist, dict):
        return str(hist.get("industry", "未知"))
    return "未知"


def _hhi(pool: List[Dict[str, Any]]) -> float:
    """行业赫芬达尔-赫希曼指数: Σ(份额²)。值越小越分散。"""
    if not pool:
        return 0.0
    cnt = Counter(_get_item_industry(it) for it in pool)
    n = len(pool)
    return sum((c / n) ** 2 for c in cnt.values())


def _balance_score(pool: List[Dict[str, Any]]) -> Dict[str, float]:
    """
    计算池的"平衡健康度"。

    Returns:
        {
            "hhi": 行业集中度（越小越好）,
            "large_pct": 大盘股占比（越小越平衡）,
            "mid_pct": 中盘股占比（越大越平衡）,
            "top3_pct": 行业 Top3 累计占比（越小越好）,
            "composite": 综合得分（越大越健康） = 1 - (hhi*0.5 + large_pct*0.3 + top3_pct*0.2) + mid_pct*0.1
        }
    """
    if not pool:
        return {"hhi": 0.0, "large_pct": 0.0, "mid_pct": 0.0, "top3_pct": 0.0, "composite": 0.0}
    n = len(pool)
    hhi = _hhi(pool)
    cnt = Counter(_get_item_industry(it) for it in pool)
    large_pct = sum(_classify_mv(_get_item_mv_yi(it)) == "large" for it in pool) / n
    mid_pct = sum(_classify_mv(_get_item_mv_yi(it)) == "mid" for it in pool) / n
    top3 = sorted(cnt.values(), reverse=True)[:3]
    top3_pct = (sum(top3) / n) if n else 0.0
    # 综合：扣 HHI/大盘/Top3，加中盘。0~1 区间，>0.7 为健康。
    composite = 1.0 - (hhi * 0.5 + large_pct * 0.3 + top3_pct * 0.2) + mid_pct * 0.1
    return {
        "hhi": round(hhi, 4),
        "large_pct": round(large_pct, 4),
        "mid_pct": round(mid_pct, 4),
        "top3_pct": round(top3_pct, 4),
        "composite": round(max(0.0, composite), 4),
    }


def check_p1_balance(p1_pool: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    检测当前 P1 池的平衡性。

    Returns:
        {
            "balanced": bool,
            "industry_concentration": {ind: count},
            "mv_distribution": {"large": n, "mid": n, "small": n, "micro": n},
            "top3_concentration": 0.0~1.0,
            "violations": [违反的规则列表]
        }
    """
    if not p1_pool:
        return {"balanced": True, "violations": []}

    industry_counter = Counter()
    mv_counter = Counter({"large": 0, "mid": 0, "small": 0, "micro": 0})

    for item in p1_pool:
        ind = _get_item_industry(item)
        industry_counter[ind] += 1
        mv_yi = _get_item_mv_yi(item)
        cat = _classify_mv(mv_yi)
        mv_counter[cat] += 1

    n_total = len(p1_pool)
    violations = []

    # 1. 行业集中度
    if industry_counter:
        top_industry, top_n = industry_counter.most_common(1)[0]
        top_pct = top_n / n_total
        if top_pct > BALANCE_PARAMS["max_industry_pct"]:
            violations.append(
                f"行业集中度过高：{top_industry} 占 {top_pct*100:.1f}%"
            )

    # 2. 大盘股占比
    large_pct = mv_counter["large"] / n_total if n_total > 0 else 0
    if large_pct > BALANCE_PARAMS["max_large_cap_pct"]:
        violations.append(
            f"大盘股占比过高：{large_pct*100:.1f}%"
        )

    # 3. 中盘股底线
    mid_pct = mv_counter["mid"] / n_total if n_total > 0 else 0
    if mid_pct < BALANCE_PARAMS["min_mid_cap_pct"]:
        violations.append(
            f"中盘股占比过低：{mid_pct*100:.1f}%"
        )

    # 4. 前 3 集中度
    top3 = sorted(industry_counter.values(), reverse=True)[:3]
    top3_sum = sum(top3)
    top3_pct = top3_sum / n_total if n_total > 0 else 0
    if top3_pct > BALANCE_PARAMS["max_top3_concentration"]:
        violations.append(
            f"行业 Top3 集中度：{top3_pct*100:.1f}%"
        )

    return {
        "balanced": len(violations) == 0,
        "industry_concentration": dict(industry_counter.most_common(10)),
        "mv_distribution": dict(mv_counter),
        "top3_concentration": round(top3_pct, 4),
        "violations": violations,
    }


def enforce_p1_balance(
    p1_pool: List[Dict[str, Any]],
    p1_rejected: List[Dict[str, Any]],
    target_size: int = 5,
    rejected_src: Optional[Dict[int, Dict[str, Any]]] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    强制使 P1 池保持平衡。

    【V26.88+10-6 设计要点】
    - 不再设硬"加入后大盘<60%"这类自相矛盾的守卫（大盘 100% 时永远加不进中盘），
      改用 HHI 改善度 + 大盘占比改善度作为"是否更平衡"的客观判据。
    - 允许扩容到原池 1.5x；但回调务必保证「让池变更好」（不只是替换被淘汰的票）。
    - 当回调能让池更平衡（HHI 下降或大盘占比明显下降）→ 扩容加入；
      否则 → 在池内劣币驱逐，把"对当前不平衡贡献最大的票"换掉。
    - 仍触发时打印 composite 改善幅度，便于 UI 展示"为何扩容/换票"。

    规则（仅作"不平衡检测"，不再作"加入硬拒"）：
      1. 单一行业 ≤ 30%
      2. 大盘股 ≤ 50%
      3. 中盘股 ≥ 20%
      4. 行业 Top3 占比 ≤ 45%

    Args:
        p1_pool: 当前通过票池
        p1_rejected: 被拒绝的票（按 score 降序排列）
        target_size: 目标池大小（仅作展示，池实际大小允许在 1.0x~1.5x 之间浮动）
        rejected_src: 【P1-修复】p1_rejected 各行对应的「原始 item」表。
            p1_rejected 只存展示字段，若直接并入主池会缺失 code/hist/df。
            有此表时用原始 item 还原，仅回填打分字段。

    Returns:
        (调整后的 pool, 调整后的 rejected)
    """
    if not BALANCE_PARAMS.get("enable_balance", True):
        return p1_pool[:target_size], p1_rejected

    balance = check_p1_balance(p1_pool)
    if balance["balanced"]:
        return p1_pool[:target_size], p1_rejected

    logger.info(f"P1 池不平衡，触发平衡调整: {balance['violations']}")

    # === 准备阶段 ===
    pool = list(p1_pool)
    rejected = list(p1_rejected)
    src_map = rejected_src if isinstance(rejected_src, dict) else {}
    # 【V30.14】max_pool_size_ratio <= 0 → 不再按比例截断；
    # 【V30.15】额外受 strategies.p1.max_pool_size 绝对上限约束（默认 120，<=0 不限），
    # 让平衡扩容本身就不会越过池上限，避免「先扩到 130 再被出口闸砍回去」的白跑。
    _cap_ratio = float(BALANCE_PARAMS.get("max_pool_size_ratio", 1.5) or 0.0)
    _abs_cap: Optional[int] = None
    try:
        from core.config_manager import get_p1_max_pool_size

        _v_abs = int(get_p1_max_pool_size())
        _abs_cap = _v_abs if _v_abs > 0 else None
    except Exception:
        _abs_cap = None

    _ratio_cap: Optional[int] = None
    if _cap_ratio > 0:
        _ratio_cap = max(
            int(len(p1_pool) * _cap_ratio),
            target_size,  # 至少满足 UI 期望的 target_size
        )
    _caps = [c for c in (_ratio_cap, _abs_cap) if c is not None]
    pool_size_cap: Optional[int] = min(_caps) if _caps else None

    def _rehydrate(candidate: Dict[str, Any]) -> Dict[str, Any]:
        """把 rejected 展示行还原成完整 item（补齐 code/hist/df/p1_score）。"""
        _src = src_map.get(id(candidate))
        if not isinstance(_src, dict):
            return candidate
        _new = dict(_src)
        _h = _new.get("hist")
        if not isinstance(_h, dict):
            _h = {}
            _new["hist"] = _h
        if not str(_new.get("code") or _new.get("ts_code") or "").strip():
            _new["code"] = candidate.get("代码")
        if not str(_h.get("name") or "").strip():
            _h["name"] = candidate.get("名称")
        try:
            _new["p1_score"] = float(candidate.get("当前得分", 0) or 0)
        except (TypeError, ValueError):
            _new["p1_score"] = 0.0
        _new["_promoted_from_rejected"] = True
        _new["reason"] = candidate.get("淘汰死因", "平衡调整回调")
        return _new

    # === 主循环 ===
    new_added: List[Dict[str, Any]] = []
    swapped_out: List[Dict[str, Any]] = []
    base_score = _balance_score(pool)
    best_composite = base_score["composite"]
    max_iter = min(20, len(rejected))

    for _ in range(max_iter):
        if not rejected:
            break
        # 已经达到池大小上限：只能尝试劣币驱逐（上限为 None = 不限制）
        if pool_size_cap is not None and len(pool) >= pool_size_cap:
            break

        candidate = rejected.pop(0)
        candidate_item = _rehydrate(candidate)
        cand_industry = _get_item_industry(candidate_item)
        cand_mv_cat = _classify_mv(_get_item_mv_yi(candidate_item))

        # 【P1-修复】不再用自相矛盾的"加入后大盘<60%"硬拒，改用 HHI 改善度判据。
        # 如果加入能让 HHI 或大盘占比明显下降 → 接受扩容；
        # 否则放到 rejected 尾部等待下一轮（也许下一只是更好行业/小票）。
        trial = pool + new_added + [candidate_item]
        trial_score = _balance_score(trial)
        baseline_score = _balance_score(pool + new_added)

        hhi_improvement = baseline_score["hhi"] - trial_score["hhi"]  # 越大越好
        large_improvement = baseline_score["large_pct"] - trial_score["large_pct"]
        composite_improvement = trial_score["composite"] - baseline_score["composite"]

        # 两条独立改善路径任一命中即可：
        # 路径 A：行业更分散（HHI 下降明显）
        # 路径 B：大盘占比下降明显（通常是回调进中盘/小盘）
        accept_for_diversify = (
            hhi_improvement >= float(BALANCE_PARAMS.get("swap_min_hhi_improvement", 0.005))
            or large_improvement >= float(BALANCE_PARAMS.get("swap_min_large_improvement", 0.02))
        )
        # 但不能恶化大盘/Top3
        not_worse = (
            trial_score["large_pct"] <= baseline_score["large_pct"] + 0.05
            and trial_score["top3_pct"] <= baseline_score["top3_pct"] + 0.05
        )

        if accept_for_diversify and not_worse:
            new_added.append(candidate_item)
            candidate_item["_balance_decision"] = (
                f"扩容(改善ΔHHI={hhi_improvement:+.4f} Δ大盘={large_improvement:+.2%} "
                f"composite={trial_score['composite']:.3f})"
            )
            if trial_score["composite"] > best_composite:
                best_composite = trial_score["composite"]
        else:
            # 这一只不改善 → 暂放回尾部，等下一轮
            candidate_item["_balance_decision"] = (
                f"未扩容(ΔHHI={hhi_improvement:+.4f} Δ大盘={large_improvement:+.2%} "
                f"不够明显或变差)"
            )
            rejected.append(candidate)

    # === 合并 + 劣币驱逐 ===
    # 先把回调票合并进来，再判断是否仍不均衡；
    # 仍不均衡时用回调的"扩容"票驱逐池内"对不平衡贡献最大"的票。
    merged = pool + new_added
    if new_added and (not _is_balanced_now(merged)):
        # 先把 new_added 的票按 composite 增量排序，优先拿"贡献最大的"
        new_added_sorted = sorted(
            new_added,
            key=lambda it: it.get("_balance_decision", "").startswith("扩容"),
            reverse=True,
        )
        for cand in new_added_sorted:
            if _is_balanced_now(merged):
                break
            # 找"贡献最大"的现有票：同时考虑市值档和行业衡定
            worst_idx = _find_worst_balance_contributor(merged)
            if worst_idx < 0:
                break
            # 试替换
            trial = merged[:worst_idx] + [cand] + merged[worst_idx + 1:]
            trial_score = _balance_score(trial)
            cur_score = _balance_score(merged)
            if (
                trial_score["composite"] > cur_score["composite"] + 0.005
                and trial_score["hhi"] <= cur_score["hhi"]
            ):
                evicted = merged.pop(worst_idx)
                evicted["_swap_reason"] = _diffs_log(cur_score, trial_score, "swap")
                evicted["_promoted_out_by_balance"] = True
                merged.append(cand)
                swapped_out.append(evicted)
                # 驱逐出来的票回 rejected（避免主池里有重复无主票）
                rejected.append(evicted)
                logger.info(
                    "[P1-balance-swap] 替换 %s -> %s | composite %+.3f",
                    (evicted.get("code") or evicted.get("ts_code") or "?"),
                    (cand.get("code") or cand.get("ts_code") or "?"),
                    trial_score["composite"] - cur_score["composite"],
                )

    # === 出口净化 ===
    # 兜底过滤：上游可能漏掉缺 code/hist/df 的票（已知上游会污染 <no-code>）。
    # 这里按"能在主池存活"的最低要求过滤：
    #   - 必须有 code (或 ts_code)
    #   - 必须是 dict
    #   - hist 字段必须是 dict（None/缺失/非 dict 全部补成 {}）
    # 这样可同时防止 save_p1_cache KeyError('code') 和 UI KeyError('hist')。
    def _is_pool_viable(it: Dict[str, Any]) -> bool:
        if not isinstance(it, dict):
            return False
        code = str(it.get("code") or it.get("ts_code") or "").strip()
        return bool(code)

    def _normalize_pool_item(it: Dict[str, Any]) -> Dict[str, Any]:
        # hist 必须是 dict（修复 UI KeyError('hist')）
        if not isinstance(it.get("hist"), dict):
            it["hist"] = {}
        # code 与 ts_code 至少一个非空；尝试从 code 拆出 ts_code
        code = str(it.get("code") or "").strip()
        ts_code = str(it.get("ts_code") or "").strip()
        if not code and ts_code:
            it["code"] = ts_code
        if not ts_code and code:
            it["ts_code"] = code
        return it

    # 池内净化：缺字段的票移回 rejected（带说明），让 pool + new_added 都干净
    merged_clean: List[Dict[str, Any]] = []
    for it in merged:
        if _is_pool_viable(it):
            merged_clean.append(_normalize_pool_item(it))
        else:
            reason = "[P1-balance] 主池出口净化：缺 code/hist/df，已移回 rejected"
            if not isinstance(it, dict):
                continue  # 极端情况：非 dict 直接丢弃
            it.setdefault("_p1_drop_to_rejected", True)
            it.setdefault("_p1_drop_reason", reason)
            rejected.append(it)
    merged = merged_clean

    final_score = _balance_score(merged)

    # 修复：超过 target_size 不再粗暴截断丢弃回调票；
    # 池上限 = pool_size_cap；为 None 时表示不设上限（V30.14 默认口径）。
    # 超过上限则按 _p1_final_sort_key 截断（截掉的票带着 _balance_decision 进 rejected，便于 UI 解释）。
    if pool_size_cap is not None and len(merged) > pool_size_cap:
        try:
            from core.pool_manager.thresholds import _p1_final_sort_key
            merged.sort(key=lambda x: _p1_final_sort_key(x), reverse=True)
        except Exception:
            pass
        kept = merged[:pool_size_cap]
        dropped = merged[pool_size_cap:]
        # 被截掉的票回 rejected
        for d in dropped:
            rejected.append(d)
        merged = kept

    logger.info(
        "P1 平衡调整: 新增 %d 只, 驱逐 %d 只, 最终池大小 %d (cap=%d, target_size=%d), "
        "composite %.3f -> %.3f (Δ %+0.3f)",
        len(new_added), len(swapped_out), len(merged), pool_size_cap, target_size,
        base_score["composite"], final_score["composite"],
        final_score["composite"] - base_score["composite"],
    )
    return merged, rejected


def _diffs_log(before: Dict[str, float], after: Dict[str, float], action: str) -> str:
    """生成日志可读的差异字符串（写入被驱逐票的 _swap_reason 字段）。"""
    return (
        f"{action}: composite {before['composite']:.3f} -> {after['composite']:.3f} "
        f"(HHI {before['hhi']:.3f}->{after['hhi']:.3f}, "
        f"large {before['large_pct']:.0%}->{after['large_pct']:.0%})"
    )


def _is_balanced_now(pool: List[Dict[str, Any]]) -> bool:
    """便捷调用 check_p1_balance，只取 balanced 字段。"""
    return bool(check_p1_balance(pool).get("balanced"))


def _find_worst_balance_contributor(pool: List[Dict[str, Any]]) -> int:
    """
    在池中找"对当前不平衡贡献最大"的票下标。

    贡献度 = 该股所属行业的超额占比 + (若大盘则) 大盘超额占比。
    返回 -1 表示池太小或没有明显可驱逐票。
    """
    if len(pool) < 6:  # 池太小不值得驱逐
        return -1
    n = len(pool)
    ind_cnt = Counter(_get_item_industry(it) for it in pool)
    large_pct_now = sum(_classify_mv(_get_item_mv_yi(it)) == "large" for it in pool) / n
    worst_idx, worst_score = -1, -1.0
    for i, it in enumerate(pool):
        ind = _get_item_industry(it)
        ind_excess = (ind_cnt[ind] / n) - 1.0 / max(len(ind_cnt), 1)
        is_large = _classify_mv(_get_item_mv_yi(it)) == "large"
        large_excess = large_pct_now - 0.4 if is_large else 0.0  # 目标大盘占比 40%
        # 行业贡献 + 大盘贡献（大盘股若行业也集中则双重扣分）
        score = ind_excess + max(0.0, large_excess)
        # 优先找"既是行业大头、又是大盘"的票
        if is_large and ind_excess > 0.05:
            score *= 1.5
        if score > worst_score:
            worst_score = score
            worst_idx = i
    return worst_idx


# ==================== 主池风格画像 ====================

def build_p1_style_profile(p1_pool: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    构建 P1 池的风格画像，供 UI 展示。

    Returns:
        {
            "size_profile": "大盘主导"/"中盘平衡"/"小盘激进"/"混合",
            "industry_top3": [(industry, count)],
            "avg_mv_yi": float,
            "diversity_score": 0~100,
        }
    """
    if not p1_pool:
        return {"size_profile": "无数据", "diversity_score": 0.0}

    mv_counter = Counter()
    industry_counter = Counter()
    total_mv = 0.0

    for it in p1_pool:
        mv_yi = _get_item_mv_yi(it)
        total_mv += mv_yi
        mv_counter[_classify_mv(mv_yi)] += 1
        industry_counter[_get_item_industry(it)] += 1

    n = len(p1_pool)
    avg_mv = total_mv / n

    # 风格判定
    large_pct = mv_counter["large"] / n
    mid_pct = mv_counter["mid"] / n
    small_pct = (mv_counter["small"] + mv_counter["micro"]) / n

    if large_pct > 0.6:
        size_profile = "大盘主导"
    elif large_pct > 0.4 and small_pct < 0.2:
        size_profile = "中盘平衡"
    elif small_pct > 0.3:
        size_profile = "小盘激进"
    else:
        size_profile = "混合"

    # 多样性得分（基于行业分散度）
    n_industries = len(industry_counter)
    if n > 1:
        # 计算赫芬达尔指数（HHI）反向
        hhi = sum((c / n) ** 2 for c in industry_counter.values())
        diversity = round((1 - hhi) * 100, 1)
    else:
        diversity = 0.0

    return {
        "size_profile": size_profile,
        "industry_top3": industry_counter.most_common(3),
        "avg_mv_yi": round(avg_mv, 0),
        "mv_distribution": dict(mv_counter),
        "diversity_score": diversity,
    }
