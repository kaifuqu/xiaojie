# -*- coding: utf-8 -*-
"""
tools/_test_shadow_mode.py —— signal_filter shadow 模式端到端验证

【验什么】
  1. 配置读取：enabled=true + mode=shadow 能被正确解析
  2. 阈值透传：config 的 thresholds 真的作用到 signal_filter.FILTER_THRESHOLDS
  3. shadow 语义：不合格票 **不被拦截**（passed 仍为 False，但 should_block 为 False）
  4. 标注语义：能生成"过滤器拦截L1:..."的短标签，用于写进信号的风险标签
  5. 统计语义：reject 分布可被正确累计（pipeline 用它出观察报表）
  6. enabled=false 一键关闭：即使 mode 写成 enforce 也必须完全禁用
  7. 非法 mode 回落 disabled：绝不意外进入 enforce

【不碰生产库】：纯内存验证，只读 config.yaml。
"""
from __future__ import annotations

import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

_OUT = io.open(os.path.join(ROOT, "tools", "_shadow_result.txt"), "w", encoding="utf-8")
FAILS = []


def p(*a):
    s = " ".join(str(x) for x in a)
    print(s)
    _OUT.write(s + "\n")


def check(label, ok, detail=""):
    if ok:
        p("  [PASS] %s  %s" % (label, detail))
    else:
        FAILS.append(label)
        p("  [FAIL] %s  %s" % (label, detail))
    return ok


def main():
    from core import signal_filter as SF
    from core.signal_filter_adapter import (
        evaluate_row, should_block, _resolve_mode, _apply_config_thresholds,
    )
    import core.config_manager as cm

    p("=" * 72)
    p("【shadow 模式端到端验证】")
    p("=" * 72)

    # ---- 1. 配置读取 ----
    p("\n=== 1. 配置读取 ===")
    cfg = cm.get_signal_filter_config() or {}
    p("  config: enabled=%r mode=%r thresholds=%s 项"
      % (cfg.get("enabled"), cfg.get("mode"), len(cfg.get("thresholds") or {})))
    check("1a enabled 解析为 bool", isinstance(cfg.get("enabled"), bool))
    check("1b mode 解析正确", cfg.get("mode") in ("disabled", "shadow", "enforce"),
          "mode=%r" % cfg.get("mode"))
    check("1c thresholds 为 dict", isinstance(cfg.get("thresholds"), dict))

    # ---- 2. 阈值透传 ----
    p("\n=== 2. 阈值透传生效 ===")
    _apply_config_thresholds()
    th = cfg.get("thresholds") or {}
    if th:
        mismatch = []
        for k, v in th.items():
            if k in SF.FILTER_THRESHOLDS:
                got = SF.FILTER_THRESHOLDS[k]
                if abs(float(got) - float(v)) > 1e-9:
                    mismatch.append((k, got, v))
        check("2a config 阈值已写入 FILTER_THRESHOLDS", not mismatch,
              "不一致: %s" % (mismatch[:3] if mismatch else "无"))
        check("2b L4 分档系数可配（关键：原写死 1000亿才满分）",
              SF.FILTER_THRESHOLDS.get("L4_mv_score_divisor") != 20.0,
              "divisor=%s" % SF.FILTER_THRESHOLDS.get("L4_mv_score_divisor"))
    else:
        check("2a 跳过（config 未配 thresholds）", True)

    # ---- 3. shadow 语义：不拦票 ----
    p("\n=== 3. shadow 语义（核心：绝不拦票）===")
    row_low = {"综合分": 35, "代码": "600000.SH", "名称": "低分票", "战法": "P3-01"}
    rt = {
        "net_main_amount": 800.0, "turnover_rate_f": 3.2, "vol_ratio": 1.8,
        "circ_mv": 600000.0, "amount": 80000.0,
        "pe_ttm": 25.0, "pb": 3.0, "roe_pct": 12.0, "total_mv_yi": 90.0,
        "price": 10.0, "open": 10.0, "high": 10.2, "low": 9.95,
        "pre_close": 10.0, "close": 10.1, "bias_ma20": 3.0,
    }
    v = evaluate_row(row_low, rt, mode="shadow")
    p("  低分票: passed=%s reject_layer=%s enabled=%s mode=%s"
      % (v.passed, v.reject_layer, v.enabled, v.mode))
    check("3a shadow 下仍能识别出不合格层", v.reject_layer == 1, "reject_layer=%s" % v.reject_layer)
    check("3b shadow 下 should_block 为 False（不拦票）", should_block(v) is False)
    check("3c shadow 下 enabled 为 True（参与评估）", v.enabled is True)

    # enforce 下才拦
    ve = evaluate_row(row_low, rt, mode="enforce")
    check("3d enforce 下 should_block 为 True", should_block(ve) is True)
    check("3e enforce 下 passed 为 False", ve.passed is False)

    # disabled 下完全不参与
    vd = evaluate_row(row_low, rt, mode="disabled")
    check("3f disabled 下 enabled=False 且 passed=True",
          vd.enabled is False and vd.passed is True)
    check("3g disabled 下不评估任何层", vd.evaluated_layers == [])

    # ---- 4. 标注语义 ----
    p("\n=== 4. 标注语义（写进信号「风险标签」）===")
    note = v.short()
    p("  短标签: %r" % note)
    check("4a 能生成短标签", bool(note))
    check("4b 标签含层号", "L1" in note, note)

    # ---- 5. 统计语义 ----
    p("\n=== 5. 统计语义（pipeline 观察期报表）===")
    stats = {}
    for row in (row_low, {"综合分": 78, "代码": "300750.SZ", "名称": "好票"}):
        vv = evaluate_row(row, rt, mode="shadow")
        key = "L%s" % vv.reject_layer if vv.reject_layer else "pass"
        stats[key] = stats.get(key, 0) + 1
        stats["total"] = stats.get("total", 0) + 1
    p("  统计: %s" % stats)
    check("5a 统计含 total", stats.get("total") == 2)
    check("5b 统计含各层拒绝数", stats.get("L1") == 1, "L1=%s" % stats.get("L1"))
    check("5c 统计含 pass 数", stats.get("pass") == 1)

    # ---- 6. enabled=false 一键关闭 ----
    p("\n=== 6. enabled=false 一键关闭（安全阀）===")
    orig = dict(cfg)
    cfg["enabled"] = False
    cfg["mode"] = "enforce"   # 危险：即使 mode 是 enforce，enabled=false 也必须禁用
    # 直接测 _resolve_mode 的语义：enabled=False 应返回 disabled
    import core.config_manager as _cm
    _orig_loader = _cm._load_yaml_raw

    def _fake_loader(force=False):
        return {"signal_filter": {"enabled": False, "mode": "enforce"}}

    _cm._load_yaml_raw = _fake_loader
    try:
        m = _resolve_mode()
        check("6a enabled=false + mode=enforce → 解析为 disabled", m == "disabled", "mode=%s" % m)
    finally:
        _cm._load_yaml_raw = _orig_loader

    # ---- 7. 非法 mode 回落 ----
    p("\n=== 7. 非法 mode 安全回落 ===")
    _cm._load_yaml_raw = lambda force=False: {"signal_filter": {"enabled": True, "mode": "bogus"}}
    try:
        m2 = _resolve_mode()
        check("7a 非法 mode → disabled（绝不意外 enforce）", m2 == "disabled", "mode=%s" % m2)
    finally:
        _cm._load_yaml_raw = _orig_loader

    # ---- 8. 缺数据安全 ----
    p("\n=== 8. 缺数据安全（绝不因数据缺失清空信号）===")
    # 8a 全无任何数据 → 所有层都缺字段 → 必须全跳过并放行
    v8 = evaluate_row({}, {}, mode="enforce")
    p("  全无数据: passed=%s skipped=%d" % (v8.passed, len(v8.skipped)))
    check("8a 完全无数据时放行", v8.passed is True)
    check("8b 记录了跳过的层", len(v8.skipped) >= 5, "skipped=%d" % len(v8.skipped))
    check("8c 未评估任何层", v8.evaluated_layers == [])

    # 8d 只有综合分（无市场数据）：L1 可评估 → 低分票应被 L1 拒绝
    #    这是**应有行为**（L1 只依赖综合分，不属于"缺数据"），
    #    与"缺数据放行"不矛盾：缺的是市场数据，L1 并不缺。
    #    注意：L1 拒绝后**逐层短路直接 return**，后面的层不再评估，
    #    所以 skipped 为 0 是正确的（不是"没跳过"，是"根本没走到"）。
    v8d = evaluate_row({"综合分": 35}, {}, mode="enforce")
    p("  仅综合分35: passed=%s layer=%s evaluated=%s skipped=%d"
      % (v8d.passed, v8d.reject_layer, v8d.evaluated_layers, len(v8d.skipped)))
    check("8d 仅综合分时 L1 正常评估并拒绝低分", v8d.reject_layer == 1)
    check("8e L1 拒绝后逐层短路（后续层未评估）", v8d.evaluated_layers == [1],
          "evaluated=%s" % v8d.evaluated_layers)

    # 8f 只有综合分但合格 → 市场数据缺失的层跳过，仍放行
    v8f = evaluate_row({"综合分": 78}, {}, mode="enforce")
    p("  仅综合分78: passed=%s skipped=%d" % (v8f.passed, len(v8f.skipped)))
    check("8f 合格分+缺市场数据 → 放行", v8f.passed is True)

    p("\n" + "=" * 72)
    if FAILS:
        p("❌ 失败 %d 项：%s" % (len(FAILS), FAILS))
        _OUT.close()
        return 1
    p("✅ shadow 模式端到端验证全部通过")
    p("=" * 72)
    _OUT.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
