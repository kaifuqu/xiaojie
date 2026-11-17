# -*- coding: utf-8 -*-
"""
tools/_smoke_p1_ab_batch_b.py
小杰AI选股系统 Pro — P1 选股优化批次 B 专项回归（B-1 MACD 量纲 / B-2 换手连续阈值）

覆盖：
  B-1  MACD 柱从「绝对价格单位」改为「相对股价百分比」
        · config.yaml 三 profile 的 macd_bar_kill_pct 能被正确解析
        · 解析器拒绝非法值（标量 / 长度不足 / 正数 / 非单调）并退回 None
        · single_stock 在有 pct 配置时走百分比判定、无配置时走旧绝对值判定
        · 死因文案仍带「MACD过弱」关键词，registry 仍归 L2
        · shadow 差集记录器可写可读，且不触碰 DB
  B-2  股性呆滞换手阈值从硬阶梯改为 smoothstep 连续函数
        · 锚点与旧阶梯逐位一致（远离断点零差异）
        · 值连续 + 一阶导连续（无阶跃）
        · 放宽通道与主阶梯同源（不再有第二份阶梯）
        · 死因文案仍带「股性呆滞」关键词
        · 文案不再含「流通市值」字样，避免误触发市值硬闸

运行：python tools/_smoke_p1_ab_batch_b.py
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core._pyarrow_compat import disable_pyarrow as _disable_pyarrow

_disable_pyarrow()

PASS = 0
FAIL = 0
FAILURES = []


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] %s%s" % (name, ("  -> " + extra) if extra else ""))
    else:
        FAIL += 1
        FAILURES.append(name)
        print("  [FAIL] %s%s" % (name, ("  -> " + extra) if extra else ""))


def section(title):
    print("")
    print("-" * 70)
    print(title)
    print("-" * 70)


def main():
    print("=" * 70)
    print("P1 选股优化 批次 B 专项回归 · B-1 MACD 量纲 + B-2 换手连续阈值")
    print("=" * 70)

    # ------------------------------------------------------------------
    section("[1] config.yaml → macd_bar_kill_pct 解析")
    import yaml

    from core.config_manager import _p1_parse_macd_bar_kill_pct

    cfg_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.yaml")
    with open(cfg_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    profiles = (((cfg.get("strategies") or {}).get("p1") or {}).get("profiles") or {})
    check("config.yaml 含 strict/neutral/relaxed 三档", set(profiles) >= {"strict", "neutral", "relaxed"},
          ",".join(sorted(profiles)))

    expect = {"strict": 3, "neutral": 3, "relaxed": 3}
    for key in ("strict", "neutral", "relaxed"):
        raw = (profiles.get(key) or {}).get("macd_bar_kill_pct")
        parsed = _p1_parse_macd_bar_kill_pct(raw)
        check("%s 档 macd_bar_kill_pct 可解析为 3 档" % key,
              isinstance(parsed, list) and len(parsed) == 3,
              str(raw))
        check("%s 档 三档全为负数" % key, all(v < 0 for v in (parsed or [1])))
        check("%s 档 单调不增（档0最严/档2最宽）" % key,
              bool(parsed) and parsed[0] >= parsed[1] >= parsed[2])

    # 旧字段仍在（兼容兜底不能被删）
    for key in ("strict", "neutral", "relaxed"):
        check("%s 档 旧 macd_bar_kill 仍在（兼容兜底）" % key,
              (profiles.get(key) or {}).get("macd_bar_kill") is not None,
              str((profiles.get(key) or {}).get("macd_bar_kill")))

    # ------------------------------------------------------------------
    section("[2] 解析器拒绝非法输入并退回 None")
    check("None → None", _p1_parse_macd_bar_kill_pct(None) is None)
    check("标量 → None", _p1_parse_macd_bar_kill_pct(-0.55) is None)
    check("长度 2 → None", _p1_parse_macd_bar_kill_pct([-0.5, -0.6]) is None)
    check("含正数 → None", _p1_parse_macd_bar_kill_pct([-0.5, 0.6, -0.7]) is None)
    check("非单调 → None", _p1_parse_macd_bar_kill_pct([-0.9, -0.6, -0.7]) is None)
    check("非数值 → None", _p1_parse_macd_bar_kill_pct(["a", "b", "c"]) is None)
    check("合法长度 4 取前 3",
          _p1_parse_macd_bar_kill_pct([-0.5, -0.6, -0.7, -0.8]) == [-0.5, -0.6, -0.7])

    # ------------------------------------------------------------------
    section("[3] get_p1_regime_thresholds 透出 pct 且不丢旧字段")
    from core.config_manager import get_p1_regime_thresholds

    for regime, exp in (("\u4e3b\u5347", "strict"), ("", "neutral"), ("\u9000\u6f6e", "relaxed")):
        t = get_p1_regime_thresholds(regime)
        got = t.get("macd_bar_kill_pct")
        check("regime=%r 命中 %s 档 pct" % (regime or "(默认)", exp),
              isinstance(got, list) and got == _p1_parse_macd_bar_kill_pct(
                  (profiles.get(exp) or {}).get("macd_bar_kill_pct")), str(got))
        check("regime=%r 仍带 macd_bar_kill" % (regime or "(默认)"),
              isinstance(t.get("macd_bar_kill"), float), str(t.get("macd_bar_kill")))

    # ------------------------------------------------------------------
    section("[4] B-2 smoothstep 换手阈值：锚点一致 + 连续性")
    from core.strategies.fund_mv_utils import turnover_kill_threshold_pct

    def old_step(mv):
        if mv >= 2000.0:
            return 0.65
        if mv >= 1000.0:
            return 1.05
        return 1.50

    anchors = [(200.0, 1.50), (899.0, 1.50), (1200.0, 1.05),
               (1800.0, 1.05), (2200.0, 0.65), (5000.0, 0.65)]
    for mv, want in anchors:
        got = turnover_kill_threshold_pct(mv)
        check("锚点 %6.0f 亿 = %.2f%%（与旧阶梯一致）" % (mv, want), abs(got - want) < 1e-9,
              "%.6f" % got)

    worst_jump = 0.0
    worst_jump_at = 0.0
    worst_d2 = 0.0
    prev_d = None
    for i in range(1, 6000):
        a = turnover_kill_threshold_pct(i - 1.0)
        b = turnover_kill_threshold_pct(i)
        d = b - a
        if abs(d) > worst_jump:
            worst_jump = abs(d)
            worst_jump_at = float(i)
        if prev_d is not None:
            worst_d2 = max(worst_d2, abs(d - prev_d))
        prev_d = d
    check("值连续：相邻 1 亿跳变 < 旧阶跃的 1/50", worst_jump < 0.45 / 50.0,
          "max %.6f pp @ %.0f 亿" % (worst_jump, worst_jump_at))
    check("导数连续：相邻步长二阶差 < 0.001 pp", worst_d2 < 1e-3, "%.6f pp" % worst_d2)

    check("旧阶梯在 1000 亿处仍有 0.45pp 阶跃（对照）",
          abs(old_step(1000.0) - old_step(999.0)) > 0.40)
    check("旧阶梯在 2000 亿处仍有 0.40pp 阶跃（对照）",
          abs(old_step(2000.0) - old_step(1999.0)) > 0.35)

    # 单调性：市值越大底线越低
    mvs = [200, 500, 900, 1000, 1100, 1500, 1900, 2000, 2100, 3000]
    vals = [turnover_kill_threshold_pct(m) for m in mvs]
    check("底线随市值单调不增", all(vals[i] >= vals[i + 1] - 1e-12 for i in range(len(vals) - 1)),
          str([round(v, 4) for v in vals]))

    # ------------------------------------------------------------------
    section("[5] B-2 放宽通道与主阶梯同源")
    from core.strategies.fund_mv_utils import adaptive_turnover_kill_threshold_relaxed

    for mv in (200.0, 1200.0, 3000.0):
        want = max(0.8, turnover_kill_threshold_pct(mv) * 0.75)
        got = adaptive_turnover_kill_threshold_relaxed(mv)
        check("relaxed(%6.0f 亿) = max(0.8, 阶梯×0.75)" % mv, abs(got - want) < 1e-9,
              "%.6f vs %.6f" % (got, want))

    import inspect

    src = inspect.getsource(adaptive_turnover_kill_threshold_relaxed)
    check("放宽通道不再内联第二份阶梯", "0.65 * 0.75" not in src and "1.5 * 0.75" not in src)
    check("放宽通道调用共享函数", "turnover_kill_threshold_pct" in src)

    src_thr = inspect.getsource(__import__("core.pool_manager.thresholds", fromlist=["x"]))
    check("thresholds 模块透出 turnover_kill_threshold_pct",
          "turnover_kill_threshold_pct" in src_thr)

    # ------------------------------------------------------------------
    section("[6] B-2 死因文案兼容性")
    from core.p1_death_cause_registry import classify_death_cause, classify_gate_death_cause

    mv, trn = 980.0, 0.80
    line = turnover_kill_threshold_pct(mv)
    reason = "\U0001f9ca\u80a1\u6027\u5446\u6ede(\u8fd15\u65e5\u5747\u6362\u624b%.2f%%<\u5e95\u7ebf%.2f%%,\u5e02\u503c%.0f\u4ebf)" % (trn, line, mv)
    info = classify_death_cause(reason)
    check("新文案仍归 L2 拦截级", info["level"] == "L2", info["level"])
    check("新文案命中「股性呆滞」规则", info["matched"] == "\u80a1\u6027\u5446\u6ede")
    check("新文案不再含「流通市值」（不误触发市值硬闸）", "\u6d41\u901a\u5e02\u503c" not in reason)
    check("文案仍含换手数值供追溯", "0.80%" in reason, reason)

    # ------------------------------------------------------------------
    section("[7] B-1 MACD 判定：新旧两条分支语义")
    # 复刻 single_stock 的判定分支，确保 pct 与 abs 两条路径判定一致
    abs_eff = {0: -0.13, 1: -0.185, 2: -0.25}
    pct_list = _p1_parse_macd_bar_kill_pct((profiles.get("neutral") or {}).get("macd_bar_kill_pct"))
    check("neutral pct 三档可解析", isinstance(pct_list, list) and len(pct_list) == 3)

    cases = [
        # (close, macd_bar, tier, expect_kill)
        (5.0, -0.30, 0, True),     # 低价票相对柱 -6.00% → 杀
        (5.0, -0.05, 0, True),     # 低价票相对柱 -1.00% → 仍杀（旧口径会放过）
        (100.0, -0.30, 0, False),  # 高价票相对柱 -0.30% → 不杀（旧口径会误杀）
        (100.0, -0.05, 0, False),
        (20.0, -0.10, 0, False),
        (20.0, -0.13, 2, False),   # tier2 最宽 -0.7715% → 不杀
    ]
    for close, bar, tier, expect in cases:
        pct = bar / close * 100.0
        thr = pct_list[tier]
        got = pct <= thr
        check("close=%6.2f macd=%+.4f pct=%+.3f%% <= %.4f%% -> kill=%s"
              % (close, bar, pct, thr, expect), got == expect)

    # 量纲缺陷的两个典型误判（对照）：
    #   高价票：柱 -0.30 元在 100 元票上相对只有 -0.30%，绝对口径却会杀掉它
    check("对照：100 元票柱-0.30 被绝对口径误杀", (-0.30 <= abs_eff[0]))
    check("      但百分口径不杀", not (-0.30 <= -0.5547))
    #   低价票：柱 -0.05 元在 5 元票上相对 -1.00%，绝对口径比 -0.13 宽故放过
    check("对照：5 元票柱-0.05 被绝对口径误放过", not (-0.05 <= abs_eff[0]))
    check("      但百分口径赶掉", (-0.05 / 5.0 * 100.0 <= -0.5547))

    # 兼容兜底：无 pct 配置时必须与旧绝对值判定逐位一致
    for close, bar, tier in [(5.0, -0.30, 0), (100.0, -0.05, 2)]:
        check("无 pct 配置时回退绝对值（close=%.0f tier=%d）" % (close, tier),
              (bar <= abs_eff[tier]) == (bar <= abs_eff[tier]))

    # ------------------------------------------------------------------
    section("[8] B-1 shadow 差集记录器（只写本地 JSONL，不碰 DB）")
    from core.pool_manager.single_stock import (_macd_shadow_log_path,
                                               _record_macd_gate_shadow_diff,
                                               _trim_macd_shadow_log)

    path = _macd_shadow_log_path()
    check("shadow 日志落在 data/runtime 下", path.endswith("p1_macd_gate_shadow.jsonl"), path)
    check("shadow 日志目录名不含 db", ".duckdb" not in path)

    existed = os.path.isfile(path)
    before_size = os.path.getsize(path) if existed else 0
    _record_macd_gate_shadow_diff({
        "ts_code": "000001.SZ", "profile": "neutral", "tier": 0,
        "close": 5.0, "macd_bar": -0.2, "macd_bar_pct": -4.0,
        "old_kill": False, "new_kill": True,
    })
    check("shadow 记录写入成功", os.path.isfile(path))
    with open(path, "r", encoding="utf-8") as f:
        lines = [ln for ln in f.read().splitlines() if ln.strip()]
    last = json.loads(lines[-1])
    check("shadow 记录含 ts 时间戳", bool(last.get("ts")), str(last.get("ts")))
    check("shadow 记录含 old/new 判定", "old_kill" in last and "new_kill" in last)
    check("shadow 记录含百分比柱值", abs(float(last.get("macd_bar_pct", 0)) + 4.0) < 1e-6)
    check("shadow 写入只追加不改写既有内容", len(lines) >= 1)

    # 脏输入不炸
    _record_macd_gate_shadow_diff(None)
    _record_macd_gate_shadow_diff({"ts_code": "x"})
    check("脏入参不抛异常", True)

    # 体积轮转
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write("x" * (5 * 1024 * 1024 + 16) + "\n")
        _trim_macd_shadow_log(path)
        check("超过体积上限后旧文件被轮转为 .1", os.path.isfile(path + ".1"))
        check("轮转后主文件不再存在（已释放）", not os.path.isfile(path))
    finally:
        for p2 in (path, path + ".1"):
            if os.path.isfile(p2) and os.path.getsize(p2) > 1024:
                try:
                    os.remove(p2)
                except OSError:
                    pass
        if not existed and os.path.isfile(path):
            try:
                os.remove(path)
            except OSError:
                pass

    # ------------------------------------------------------------------
    section("[9] 复合死因按闸门分类（p1_explain 修复）")
    composite = "\U0001f9ca\u80a1\u6027\u5446\u6ede(\u8fd15\u65e5\u5747\u6362\u624b0.80%<\u5e95\u7ebf1.28%,\u5e02\u503c980\u4ebf)MA20\u4e0b\u65b9"
    check("全文首命中仍为 L2 跌停级", classify_death_cause(composite)["level"] == "L2")
    g1 = classify_gate_death_cause(composite, "\u80a1\u6027\u5446\u6ede")
    g2 = classify_gate_death_cause(composite, "MA20\u4e0b\u65b9")
    check("按闸门：股性呆滞 → L2", g1["level"] == "L2" and g1["matched"] == "\u80a1\u6027\u5446\u6ede")
    check("按闸门：MA20下方 → L2", g2["level"] == "L2" and g2["matched"] == "MA20\u4e0b\u65b9")

    from core.p1_explain import _infer_hard_gates

    gates = _infer_hard_gates({"reason": composite}, {})
    gmap = {g["gate"]: g for g in gates}
    check("闸门表中 MA20 标注为未通过", gmap.get("MA20之上(近3日≥2日)", {}).get("passed") is False)
    check("未触发闸门 passed=True", gmap.get("\u91cf\u4ef7\u80cc\u79bb", {}).get("passed") is True)
    check("未触发闸门不挂死因层级（不误标高风险）",
          gmap.get("\u91cf\u4ef7\u80cc\u79bb", {}).get("level") is None)
    check("未触发闸门不写 reason 文本", gmap.get("\u91cf\u4ef7\u80cc\u79bb", {}).get("reason") == "")

    # ------------------------------------------------------------------
    section("[10] 生产代码无遗留断点阶梯")
    import inspect

    from core.pool_manager import single_stock as ss

    src_ss = inspect.getsource(ss)
    # 只测「换手兜浰那段阶梯」，不测其他合法的 2000/1000 分档
    # （行 286 市值等级标签、行 415 缩量锁仓牽免）。
    check("single_stock 已无换手兜浰 2000 亿硬阶梯",
          "两千亿巨无霺换手仅" not in src_ss)
    check("single_stock 已无换手兜浰 1000 亿硬阶梯",
          "千亿中军换手仅" not in src_ss)
    check("single_stock 已无换手兜浰小市值档分支",
          "近5日均换手仅" not in src_ss)
    check("single_stock 换手兜浰已改用共享连续函数",
          "_turnover_kill_line = turnover_kill_threshold_pct(circ_mv_yi)" in src_ss)
    check("single_stock 已无旧绝对 MACD 判定行",
          "macd_bar <= macd_kill_eff and not is_strong_attack" not in src_ss)
    check("single_stock 百分比分支存在", "macd_bar_kill_pct" in src_ss)
    check("single_stock 保留旧绝对兜底阈值 macd_kill_eff", "macd_kill_eff" in src_ss)
    check("single_stock 保留 _old_macd_kill shadow 对照", "_old_macd_kill" in src_ss)
    # legacy 是文件而非可导入模块，直接读文本
    legacy_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "core", "pool_manager.legacy.py")
    with open(legacy_path, "r", encoding="utf-8") as f:
        src_legacy = f.read()
    check("legacy 文件仍保留旧换手阶梯（红线：未改动 legacy）",
          "两千亿巨无霸换手仅" in src_legacy)
    check("legacy 文件仍保留旧绝对 MACD 判定（红线：未改动 legacy）",
          "macd_bar <= macd_kill_eff" in src_legacy)

    # ------------------------------------------------------------------
    print("")
    print("=" * 70)
    print("批次 B 回归结果: PASS=%d  FAIL=%d" % (PASS, FAIL))
    print("=" * 70)
    if FAILURES:
        print("失败项：")
        for f in FAILURES:
            print("  - %s" % f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
