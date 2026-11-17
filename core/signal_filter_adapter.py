# -*- coding: utf-8 -*-
"""
core/signal_filter_adapter.py — 多层信号过滤器的「安全接入层」

【为什么需要这一层】
core/signal_filter.py 写了五层过滤器（基础分/资金真实性/异常形态/流动性/基本面），
但**从未被扫描主链路调用** —— 它是死代码，从没保护过选股。

想直接把它接进 pipeline 会立刻出事，原因有两个：

  1. **缺字段 = 误杀一切**。multi_layer_filter 的默认参数是 net_main_wan=0、
     turnover_f=0、circ_mv_yi=0…… 而 L2 要求"净流入 ≥200 万""换手 ≥0.8%"。
     只要某个字段在实时快照里缺失，取默认 0 就会**判定为不通过**，
     结果是当天几乎没有任何票能出信号。这不是"过滤垃圾"，是"关掉整个系统"。

  2. **单位不统一**。Tushare 的 circ_mv / total_mv 单位是「万元」，
     net_main_amount 也是「万元」，而 turnover_rate_f 是「%」。
     直接喂进去会把 50 亿市值算成 50 万，把 500 万净流入算成 500。

【本层的三条铁律】
  1. **缺失即跳过，绝不拒绝**。某个字段拿不到 → 该子项标记 skipped，视为不拦。
     宁可放过一个可疑票，也不能因为数据缺失把整天的信号全清空。
  2. **单位先归一化再判定**。所有市值统一到「亿」、资金统一到「万元」，
     并用合理区间做 sanity check；落在区间外视为「数据可疑」→ 跳过。
  3. **只在数据齐全时才真的拒绝**。只有拿到可信的坏数据才拦。

【模式】
  · disabled — 完全不动（默认，等验证后再开）
  · shadow   — 只标注不拦截：给每行打上 filter 结论，用于观察"如果开拦截会怎样"
  · enforce  — 真正拦截

【验证路径】
  先用 shadow 跑几天/回放历史，看 reject 率与各层分布；
  确认不会误杀（reject 率合理、被拒的票后续表现确实更差）后，再切 enforce。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

_log = logging.getLogger(__name__)

MODE_DISABLED = "disabled"
MODE_SHADOW = "shadow"
MODE_ENFORCE = "enforce"


@dataclass
class FilterVerdict:
    """过滤器接入层的结论。"""

    passed: bool = True
    enabled: bool = False
    mode: str = MODE_DISABLED
    reject_layer: Optional[int] = None
    reject_reason: str = ""
    evaluated_layers: List[int] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)   # 因缺数据跳过的子项说明

    def to_dict(self) -> Dict[str, Any]:
        return {
            "passed": bool(self.passed),
            "enabled": bool(self.enabled),
            "mode": self.mode,
            "reject_layer": self.reject_layer,
            "reject_reason": self.reject_reason,
            "evaluated_layers": list(self.evaluated_layers),
            "skipped": list(self.skipped),
        }

    def short(self) -> str:
        """一行摘要，用于日志/推送备注。"""
        if not self.enabled:
            return ""
        if self.passed:
            layers = ",".join(map(str, self.evaluated_layers))
            return f"过滤器通过(L{layers})"
        return f"过滤器拦截L{self.reject_layer}:{self.reject_reason}"


def _num(v: Any) -> Optional[float]:
    """安全转 float；无法转换/NaN/无穷 → None（表示"数据缺失"）。"""
    try:
        if v is None or v == "" or v == "--":
            return None
        f = float(v)
        if f != f or f in (float("inf"), float("-inf")):
            return None
        return f
    except (TypeError, ValueError):
        return None


def _first(row: Dict[str, Any], keys: List[str]) -> Optional[float]:
    for k in keys:
        if k in row:
            v = _num(row.get(k))
            if v is not None:
                return v
    return None


# ---------- 单位归一化 ----------
# 【单位口径】按 Tushare 标准约定（不做数量级猜测，猜错比缺数据更危险）：
#   · circ_mv / total_mv : 万元  → /1e4  = 亿元
#   · net_main_amount    : 万元  → 已是万元
#   · amount             : 千元  → /10   = 万元
# 每个结果都做合理性区间校验；落在区间外一律视为「数据可疑」返回 None（→ 跳过该层），
# 绝不硬转 —— 因为错误单位会把 50 亿算成 50 万，进而误杀或误放。
_YI_MIN, _YI_MAX = 0.5, 50000.0        # 0.5 亿 ~ 5 万亿
_WAN_MAX = 2_000_000.0                 # |万元| ≤ 2000 亿


def _to_yi(v: Optional[float], *, assume_unit: str = "wan") -> Optional[float]:
    """市值 → 亿元。assume_unit: 'wan'（Tushare 默认）或 'yuan'。"""
    if v is None or v <= 0:
        return None
    scale = 1e-4 if assume_unit == "wan" else 1e-8
    x = v * scale
    if _YI_MIN <= x <= _YI_MAX:
        return x
    return None


def _to_wan(v: Optional[float], *, assume_unit: str = "wan") -> Optional[float]:
    """资金 → 万元。assume_unit: 'wan'（默认）/ 'yuan' / 'qian'（Tushare amount 用千元）。"""
    if v is None:
        return None
    scale = {"wan": 1.0, "yuan": 1e-4, "qian": 0.1}.get(assume_unit, 1.0)
    #   wan : 已是万元，×1
    #   yuan: 元 → 万元，×1e-4
    #   qian: 千元 → 万元，×0.1（1万元 = 10千元）
    x = v * scale
    if abs(x) <= _WAN_MAX:
        return x
    return None


def _pct_sane(v: Optional[float], lo: float = -100.0, hi: float = 1000.0) -> Optional[float]:
    if v is None or v < lo or v > hi:
        return None
    return v


# ---------- 从 row / rt 提取 filter 输入 ----------
def _extract_inputs(row: Dict[str, Any], rt: Dict[str, Any]) -> Dict[str, Any]:
    """把 pipeline 的 row（中文键）与 rt（实时快照）映射成过滤器入参。

    返回的字典里，值为 None 表示「数据缺失」，由调用方跳过对应子项。
    """
    merged: Dict[str, Any] = {}
    if isinstance(rt, dict):
        merged.update(rt)
    if isinstance(row, dict):
        for k, v in row.items():
            if k not in merged or merged.get(k) in (None, "", "--"):
                merged[k] = v

    score = _first(merged, ["综合分", "score", "burst_score", "pool_score", "p1_score"])

    net_main_wan = _to_wan(_first(merged, ["net_main_amount", "net_main_wan", "net_mf_amount", "主力净流入"]))
    turnover_f = _pct_sane(_first(merged, ["turnover_rate_f", "turnover_f", "换手率", "换手"]))
    vol_ratio = _num(_first(merged, ["vol_ratio", "量比"]))
    circ_mv_yi = _to_yi(_first(merged, ["circ_mv", "circ_mv_yi", "流通市值"]))
    total_mv_yi = _to_yi(_first(merged, ["total_mv", "total_mv_yi", "总市值", "市值"]))
    # Tushare amount 单位是「千元」，故用 qian 口径
    amount_wan = _to_wan(_first(merged, ["amount", "amount_wan", "成交额"]), assume_unit="qian")

    # 上影线占比（%）= (high - max(open, close)) / pre_close * 100
    upper_shadow_pct: Optional[float] = None
    hi = _num(_first(merged, ["high"]))
    op = _num(_first(merged, ["open"]))
    px = _num(_first(merged, ["price", "现价", "close"]))
    pc = _num(_first(merged, ["pre_close", "昨收"]))
    if hi and op and px and pc and hi > 0 and pc > 0:
        body_top = max(op, px)
        if hi >= body_top:
            upper_shadow_pct = (hi - body_top) / pc * 100.0

    bias_ma20 = _pct_sane(_first(merged, ["bias_ma20", "乖离率", "bias20"]), -100.0, 500.0)
    consecutive_yin = _num(_first(merged, ["consecutive_yin", "连阴"]))
    has_long_upper_shadow_3d = bool(_first(merged, ["has_long_upper_shadow_3d"]) or 0)

    pe_ttm = _num(_first(merged, ["pe_ttm", "pe", "市盈率"]))
    pb = _num(_first(merged, ["pb", "市净率"]))
    roe_pct = _pct_sane(_first(merged, ["roe", "roe_pct", "净资产收益率"]))

    return {
        "score": score,
        "net_main_wan": net_main_wan,
        "turnover_f": turnover_f,
        "vol_ratio": vol_ratio,
        "circ_mv_yi": circ_mv_yi,
        "total_mv_yi": total_mv_yi,
        "amount_wan": amount_wan,
        "upper_shadow_pct": upper_shadow_pct,
        "bias_ma20": bias_ma20,
        "consecutive_yin": consecutive_yin,
        "has_long_upper_shadow_3d": has_long_upper_shadow_3d,
        "pe_ttm": pe_ttm,
        "pb": pb,
        "roe_pct": roe_pct,
    }


def _resolve_mode() -> str:
    """读配置里的过滤模式；任何异常都回落 disabled（安全默认）。

    【2026-11-04 修正】原先只读 `mode`，**忽略 `enabled` 字段** ——
    意味着 config 里写 `enabled: false, mode: shadow` 时过滤器依然会启用，
    `enabled` 成了摆设，无法"一键关闭"。
    现改为：`enabled` 显式为 false 时一律 disabled（无论 mode 写的是什么）；
    未写 enabled 时才看 mode（保持向后兼容）。

    另外校验 mode 合法性：非法值一律回落 disabled，绝不意外进入 enforce。
    """
    try:
        from core.config_manager import get_signal_filter_config

        cfg = get_signal_filter_config() or {}
        # enabled 显式关闭 → 直接 disabled（不读 mode）
        if "enabled" in cfg:
            try:
                if not bool(cfg.get("enabled")):
                    return MODE_DISABLED
            except (TypeError, ValueError):
                return MODE_DISABLED
        mode = str(cfg.get("mode") or MODE_DISABLED).strip().lower()
        if mode in (MODE_DISABLED, MODE_SHADOW, MODE_ENFORCE):
            return mode
    except Exception:  # noqa: BLE001
        pass
    return MODE_DISABLED


def _apply_config_thresholds() -> None:
    """把 config.yaml 里的阈值覆盖应用到 signal_filter（失败忽略）。"""
    try:
        from core.config_manager import get_signal_filter_config

        cfg = get_signal_filter_config() or {}
        ov = cfg.get("thresholds")
        if isinstance(ov, dict) and ov:
            from core import signal_filter as SF

            SF.apply_threshold_overrides(ov)
    except Exception:  # noqa: BLE001
        pass


def evaluate_row(
    row: Dict[str, Any],
    rt: Optional[Dict[str, Any]] = None,
    *,
    mode: Optional[str] = None,
) -> FilterVerdict:
    """对一个候选行执行五层过滤（缺失即跳过，不因缺数据拒绝）。

    mode:
      默认取配置；disabled 时立即返回 passed=True 且不做事。
    """
    mode = (mode or _resolve_mode()).strip().lower()
    if mode == MODE_DISABLED:
        return FilterVerdict(passed=True, enabled=False, mode=MODE_DISABLED)

    _apply_config_thresholds()
    v = FilterVerdict(passed=True, enabled=True, mode=mode)
    try:
        from core import signal_filter as SF
    except Exception as e:  # noqa: BLE001
        v.skipped.append(f"过滤器模块不可用:{e}")
        return v

    x = _extract_inputs(row or {}, rt or {})

    try:
        # ---- L1 基础分（几乎总有）----
        if x["score"] is not None:
            ok, msg = SF.filter_layer1_base_score(float(x["score"]))
            v.evaluated_layers.append(1)
            if not ok:
                v.passed, v.reject_layer, v.reject_reason = False, 1, msg
                return v
        else:
            v.skipped.append("L1:无综合分")

        # ---- L2 资金真实性（需 净流入/换手/量比 三者齐全）----
        if x["net_main_wan"] is not None and x["turnover_f"] is not None and x["vol_ratio"] is not None:
            ok, msg = SF.filter_layer2_fund_authenticity(
                x["net_main_wan"], x["turnover_f"], x["vol_ratio"]
            )
            v.evaluated_layers.append(2)
            if not ok:
                v.passed, v.reject_layer, v.reject_reason = False, 2, msg
                return v
        else:
            miss = [k for k in ("net_main_wan", "turnover_f", "vol_ratio") if x[k] is None]
            v.skipped.append(f"L2:缺{','.join(miss)}")

        # ---- L3 异常形态（上影线必需；乖离/连阴可缺）----
        if x["upper_shadow_pct"] is not None:
            ok, msg = SF.filter_layer3_abnormal_pattern(
                x["upper_shadow_pct"],
                x["bias_ma20"] if x["bias_ma20"] is not None else 0.0,
                int(x["consecutive_yin"]) if x["consecutive_yin"] is not None else 0,
                bool(x["has_long_upper_shadow_3d"]),
            )
            v.evaluated_layers.append(3)
            if not ok:
                v.passed, v.reject_layer, v.reject_reason = False, 3, msg
                return v
        else:
            v.skipped.append("L3:缺上影线/OHLC")

        # ---- L4 流动性（需 流通市值/换手/成交额）----
        if x["circ_mv_yi"] is not None and x["turnover_f"] is not None and x["amount_wan"] is not None:
            ok, msg = SF.filter_layer4_liquidity_premium(
                x["circ_mv_yi"], x["turnover_f"], x["amount_wan"]
            )
            v.evaluated_layers.append(4)
            if not ok:
                v.passed, v.reject_layer, v.reject_reason = False, 4, msg
                return v
        else:
            miss = [k for k in ("circ_mv_yi", "turnover_f", "amount_wan") if x[k] is None]
            v.skipped.append(f"L4:缺{','.join(miss)}")

        # ---- L5 基本面（需 PE/PB/ROE/总市值 齐全才判，缺任一则跳过）----
        if all(x[k] is not None for k in ("pe_ttm", "pb", "roe_pct", "total_mv_yi")):
            ok, msg = SF.filter_layer5_fundamental(
                x["pe_ttm"], x["pb"], x["roe_pct"], x["total_mv_yi"],
                is_loss_company=bool(x["pe_ttm"] is not None and x["pe_ttm"] < 0),
            )
            v.evaluated_layers.append(5)
            if not ok:
                v.passed, v.reject_layer, v.reject_reason = False, 5, msg
                return v
        else:
            miss = [k for k in ("pe_ttm", "pb", "roe_pct", "total_mv_yi") if x[k] is None]
            v.skipped.append(f"L5:缺{','.join(miss)}")
    except Exception as e:  # noqa: BLE001
        # 过滤器自身异常绝不阻断选股
        _log.debug("[signal_filter_adapter] 过滤异常(已放行): %s", e)
        v.skipped.append(f"过滤异常:{e}")

    return v


def should_block(verdict: FilterVerdict) -> bool:
    """只有 enforce 模式且明确不通过时才真正拦截。"""
    return bool(verdict.enabled and verdict.mode == MODE_ENFORCE and not verdict.passed)


def _self_test() -> None:
    """自检：缺数据必须放行；数据齐全时才拦截。"""
    # 1) disabled 模式：不做事、直接放行
    v = evaluate_row({"综合分": 10}, {}, mode=MODE_DISABLED)
    assert v.passed and not v.enabled and v.evaluated_layers == []

    # 2) 全缺失数据 → 必须放行（这是最关键的一条）
    v = evaluate_row({"名称": "某票"}, {}, mode=MODE_SHADOW)
    assert v.passed, f"缺数据必须放行，实际被拦: {v.reject_reason}"
    assert v.evaluated_layers == [], v.evaluated_layers
    assert any("L2" in s for s in v.skipped), v.skipped

    # 3) 只有分数且过低 → L1 拦截
    v = evaluate_row({"综合分": 20}, {}, mode=MODE_SHADOW)
    assert not v.passed and v.reject_layer == 1, v.to_dict()

    # 4) 分数够 + 资金/换手/量比齐全且健康 → 通过（L2 不拦）
    #    用真实中小盘口径：circ_mv=600000万元(=60亿) / amount=80000000千元(=8000万元)
    row = {"综合分": 80}
    rt = {"net_main_amount": 500.0, "turnover_rate_f": 3.0, "vol_ratio": 1.5,
          "circ_mv": 600000.0, "amount": 80000.0,
          "price": 10.0, "open": 10.0, "high": 10.2, "pre_close": 10.0}
    v = evaluate_row(row, rt, mode=MODE_SHADOW)
    assert v.passed, v.to_dict()
    assert 1 in v.evaluated_layers and 2 in v.evaluated_layers, v.evaluated_layers
    assert 4 in v.evaluated_layers, f"L4 应被评估: {v.to_dict()}"

    # 5) 资金为负 → L2 拦截（有可信坏数据时才拦）
    rt2 = dict(rt, net_main_amount=-500.0)
    v = evaluate_row(row, rt2, mode=MODE_SHADOW)
    assert not v.passed and v.reject_layer == 2, v.to_dict()

    # 6) 只有 shadow 模式不该拦；enforce 才拦
    v = evaluate_row(row, rt2, mode=MODE_SHADOW)
    assert should_block(v) is False, "shadow 模式绝不能拦截"
    v = evaluate_row(row, rt2, mode=MODE_ENFORCE)
    assert should_block(v) is True, "enforce 模式应拦截"

    # 7) 单位归一化：Tushare 口径 万元 → 亿
    assert abs(_to_yi(600000.0) - 60.0) < 1e-6, _to_yi(600000.0)          # 600000万元 → 60亿
    assert _to_yi(6.0e9) is None, "600亿用万元口径算会超上限，应判数据可疑"
    assert abs(_to_yi(6.0e9, assume_unit="yuan") - 60.0) < 1e-6
    # 金额：Tushare amount 单位千元（80000千元 = 8000万元）
    assert abs(_to_wan(80000.0, assume_unit="qian") - 8000.0) < 1e-6
    assert _to_wan(8.0e7, assume_unit="qian") is None, "800亿元成交额超出合理上限，应判可疑"

    # 8) 阈值覆盖生效（L4 分档系数可配置）
    from core import signal_filter as SF
    old_div = SF.FILTER_THRESHOLDS["L4_mv_score_divisor"]
    SF.apply_threshold_overrides({"L4_mv_score_divisor": 10.0})
    assert SF.FILTER_THRESHOLDS["L4_mv_score_divisor"] == 10.0
    SF.apply_threshold_overrides({"L4_mv_score_divisor": old_div})
    SF.apply_threshold_overrides({"不存在的键": 1})   # 不应抛异常
    print("[signal_filter_adapter] self-test OK | 缺数据放行/有坏数据才拦/shadow不拦/单位归一/阈值可配 已验证")


if __name__ == "__main__":
    _self_test()


__all__ = [
    "MODE_DISABLED",
    "MODE_SHADOW",
    "MODE_ENFORCE",
    "FilterVerdict",
    "evaluate_row",
    "should_block",
]
