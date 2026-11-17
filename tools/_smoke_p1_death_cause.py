# -*- coding: utf-8 -*-
"""
tools/_smoke_p1_death_cause.py
小杰AI选股系统 Pro — 批次 A（P1 死因分层 + 处置建议）专项回归

【为什么需要它】
core/p1_death_cause_registry.py 是「展示层纯静态映射」，一旦规则顺序写错
（典型事故：高位派发-15 被 高位派发 L0 首命中吃掉）不会有任何异常抛出，
只会静默把 L2 显示成 L0 —— 靠人工看不出来，必须有测试兜底。

【覆盖】
  1. 5 个层级各取典型死因，断言 level 正确
  2. 顺序敏感回归：高位派发-15 必须命中 L2（不能被 L0 吃掉）
  3. 空死因 / None / 脏输入 → 必须 D 数据级，且永不抛异常
  4. 未登记死因 → 必须落到 UNKNOWN 兜底，且 registered=False
  5. 100% 兜底：随机/畸形输入全部产出 10 个必需字段
  6. enrich_rejected_rows 只加键不改值
  7. level_distribution 计数自洽

【运行】
  python tools/_smoke_p1_death_cause.py
退出码 0 = 全通过；非 0 = 有 FAIL。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS = 0
FAIL = 0
FAILURES = []


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] %s" % name)
    else:
        FAIL += 1
        FAILURES.append("%s | %s" % (name, detail))
        print("  [FAIL] %s | %s" % (name, detail))


def main():
    from core.p1_death_cause_registry import (
        EMPTY_REASON_LEVEL,
        LEVEL_META,
        UNKNOWN_LEVEL,
        classify_death_cause,
        enrich_rejected_rows,
        level_distribution,
    )

    print("=" * 70)
    print("批次 A 回归 · P1 死因分层")
    print("=" * 70)

    # ---------- 1. 5 层典型映射 ----------
    print("\n[1] 五层典型死因映射")
    cases = [
        # (死因文案, 期望层级)
        ("业绩首亏 (扣分)", "L0"),
        # 真实文案来自 core/event_filter.py tags.append("ST/退市风险")
        ("🚨特殊事件拦截[ST/退市风险; 连续跌停]", "L0"),
        ("🚨特殊事件拦截[次新股]", "L0"),
        ("🚨特殊事件拦截[ST/退市风险]", "L0"),
        ("🚨特殊事件拦截[连续跌停]", "L0"),
        ("🛑直通车超载保护 (同行业半导体已满2只)", "L1"),
        ("🛑直通车超载保护 (全市场限流65只，名额已满拦截)", "L1"),
        ("流通市值不足120亿", "L1"),
        ("MA20下方: 收盘跌破20日线", "L2"),
        ("深海防线: 连续2日收盘低于MA20", "L2"),
        ("右侧地基: 现价未站上MA60", "L2"),
        ("MACD过弱(柱-0.1400≤放宽底线-0.1300,容错档strict)", "L2"),
        ("🧊股性呆滞(千亿中军换手仅0.80%)", "L2"),
        ("PE极度超标 (PE:180.0 > 75分位60.0*2.5)", "L2"),
        ("PE相对超标 (PE:120.0 > 75分位60.0*1.7)", "L3"),
        ("PE极值异常 (≤0或>300)", "L3"),
    ]
    for reason, want in cases:
        info = classify_death_cause(reason)
        check(
            "分类[%s] -> %s" % (want, reason[:24]),
            info.get("level") == want,
            "got=%s" % info.get("level"),
        )

    # ---------- 2. 顺序敏感回归（本次修复点）----------
    print("\n[2] 顺序敏感回归（首命中即返回）")
    info15 = classify_death_cause("🚨特殊事件拦截[高位派发-15]")
    check(
        "高位派发-15 未被 L0「高位派发」吃掉",
        info15.get("level") == "L2",
        "got=%s matched=%s" % (info15.get("level"), info15.get("matched")),
    )
    info_hq = classify_death_cause("高位派发: 筹码结构已坏")
    check(
        "裸「高位派发」仍为 L0",
        info_hq.get("level") == "L0",
        "got=%s" % info_hq.get("level"),
    )
    info15e = classify_death_cause("特殊事件拦截[高位派发]")
    check(
        "特殊事件拦截[高位派发] 仍为 L0（显式标签优先）",
        info15e.get("level") == "L0",
        "got=%s matched=%s" % (info15e.get("level"), info15e.get("matched")),
    )
    # 裸 "ST" 子串误命中回归：MACD 死因里的「容错档strict」小写后含 "st"。
    # 早期用裸 "ST" 关键词登记规则，导致 MACD过弱 被错标成 L0「ST永久排除」。
    for macd_txt in (
        "MACD\u8fc7\u5f31(\u67f1-0.1400\u2264\u653e\u5bbd\u5e95\u7ebf-0.1300,\u5bb9\u9519\u6863strict)",
        "MACD\u8fc7\u5f31(\u67f1-0.2100\u2264\u653e\u5bbd\u5e95\u7ebf-0.1850,\u5bb9\u9519\u6863neutral)",
        "MACD\u8fc7\u5f31(\u67f1-0.2600\u2264\u653e\u5bbd\u5e95\u7ebf-0.2500,\u5bb9\u9519\u6863relaxed)",
    ):
        i_m = classify_death_cause(macd_txt)
        check(
            "MACD\u8fc7\u5f31[\u5bb9\u9519\u6863strict/neutral/relaxed] \u4e0d\u88ab\u88f8 ST \u541e\u6389",
            i_m.get("level") == "L2",
            "got=%s matched=%s" % (i_m.get("level"), i_m.get("matched")),
        )
    # 真实 ST 文案仍必须是 L0
    i_st = classify_death_cause("ST/\u9000\u5e02\u98ce\u9669")
    check(
        "\u771f\u5b9e ST \u6587\u6848 \u4ecd\u4e3a L0",
        i_st.get("level") == "L0",
        "got=%s matched=%s" % (i_st.get("level"), i_st.get("matched")),
    )

    # ---------- 3. 空死因 -> D 数据级（本次修复点）----------
    print("\n[3] 空死因 / 脏输入 -> D 数据级")
    for label, val in [("None", None), ("空串", ""), ("空白", "   "), ("NoneType占位", [])]:
        info = classify_death_cause(val)
        check(
            "空死因[%s] -> D" % label,
            info.get("level") == "D" and info.get("level_cn") == "数据级",
            "got=%s/%s" % (info.get("level"), info.get("level_cn")),
        )
        check(
            "空死因[%s] action=系统问题" % label,
            info.get("action") == EMPTY_REASON_LEVEL["action"],
            "got=%s" % info.get("action"),
        )
    # object() 的 str() 是非空串（如 "<object object at 0x...>"），
    # 因此按「未登记死因」兜底走 L3，而不是空死因分支的 D。
    # 这里断言的是「不抛异常 + 层级合法」，而不是强绑 L3。
    info_obj = classify_death_cause(object())
    check(
        "对象输入不抛异常且层级合法",
        info_obj.get("level") in LEVEL_META,
        "got=%s" % info_obj.get("level"),
    )
    info_num = classify_death_cause(12345)
    check("数字输入归 D（未登记）", info_num.get("level") in ("D", "L3"), "got=%s" % info_num.get("level"))

    # ---------- 4. 未登记死因兜底 ----------
    print("\n[4] 未登记死因兜底")
    info_unk = classify_death_cause("某个从未登记过的死因XYZ")
    check(
        "未登记 -> UNKNOWN 兜底",
        info_unk.get("level") == UNKNOWN_LEVEL["level"]
        and info_unk.get("registered") is False,
        "got=%s reg=%s" % (info_unk.get("level"), info_unk.get("registered")),
    )
    check(
        "未登记 -> 带补登记提示",
        "未登记" in str(info_unk.get("advice", "")) or "未分类" in str(info_unk.get("advice", "")),
        "advice=%s" % info_unk.get("advice"),
    )

    # ---------- 5. 字段完整性 100% 兜底 ----------
    print("\n[5] 返回字段完整性（永不缺字段）")
    required = [
        "level", "level_cn", "badge", "action", "color",
        "category", "advice", "desc", "matched", "registered",
    ]
    dirty_pool = [
        None, "", "  ", 0, 1, -1, 3.14, [], {}, object(),
        "业绩首亏", "MACD过弱", "完全没见过的死因", "🚨特殊事件拦截[次新股]",
        "x" * 500,
    ]
    missing_all = []
    for v in dirty_pool:
        info = classify_death_cause(v)
        miss = [k for k in required if k not in info]
        if miss:
            missing_all.append("%r -> %s" % (v, miss))
    check(
        "%d 个脏输入全部返回 10 个必需字段" % len(dirty_pool),
        not missing_all,
        "; ".join(missing_all[:3]),
    )
    bad_level = [
        str(v)[:20]
        for v in dirty_pool
        if classify_death_cause(v).get("level") not in LEVEL_META
    ]
    check(
        "所有返回 level 都在 LEVEL_META 内",
        not bad_level,
        "; ".join(bad_level[:3]),
    )

    # ---------- 6. enrich_rejected_rows 只加键不改值 ----------
    print("\n[6] enrich_rejected_rows 只加键不改值")
    rows = [
        {"代码": "600519", "名称": "贵州茅台", "淘汰死因": "深海防线: 连续2日收盘低于MA20", "当前得分": 48.2},
        {"代码": "000001", "名称": "平安银行", "淘汰死因": "", "当前得分": 30.0},
        {"代码": "300750", "名称": "宁德时代", "淘汰死因": "未登记XYZ", "当前得分": 22.0},
    ]
    origin = [dict(r) for r in rows]
    out = enrich_rejected_rows(rows)
    check("返回条数一致", len(out) == len(origin), "got=%d" % len(out))
    check(
        "原始字段零改动",
        all(out[i].get("代码") == origin[i].get("代码")
            and out[i].get("淘汰死因") == origin[i].get("淘汰死因")
            for i in range(len(out))),
        "原始文案被改写",
    )
    check("新增 死因层级", all("死因层级" in r for r in out), "缺列")
    check("新增 处置建议", all("处置建议" in r for r in out), "缺列")
    check("新增 层级徽标", all("层级徽标" in r for r in out), "缺列")
    check(
        "行1=L2 / 行2=D / 行3=L3兜底",
        [out[0]["死因层级"], out[1]["死因层级"], out[2]["死因层级"]] == ["L2", "D", "L3"],
        "got=%s" % [r["死因层级"] for r in out],
    )
    check("徽标带 emoji", all(r["层级徽标"].split(" ")[0] in ("🔴", "⚪", "🟡", "🟠", "⚫") for r in out),
          "got=%s" % [r["层级徽标"] for r in out])

    # ---------- 7. level_distribution 自洽 ----------
    print("\n[7] level_distribution 计数自洽")
    reasons = [r.get("淘汰死因") for r in out]
    dist = level_distribution(reasons)
    check(
        "计数总和 == 输入条数",
        sum(dist.values()) == len(reasons),
        "sum=%d n=%d" % (sum(dist.values()), len(reasons)),
    )
    check("L2 命中 1 条", dist.get("L2") == 1, "got=%s" % dist)
    check("D 命中 1 条", dist.get("D") == 1, "got=%s" % dist)
    check("L3 命中 1 条", dist.get("L3") == 1, "got=%s" % dist)
    check("空列表返回空 dict", level_distribution([]) == {}, "got=%s" % level_distribution([]))

    # ---------- 8. p1_explain 集成（不依赖 DuckDB）----------
    print("\n[8] p1_explain 集成（字段已挂载）")
    try:
        import core.p1_explain as _pe

        for field in ("death_cause_level", "death_cause_level_cn", "death_cause_advice"):
            src = open(_pe.__file__, "r", encoding="utf-8").read()
            check("p1_explain 已挂载 %s" % field, field in src, "字段缺失")
    except Exception as exc:  # pragma: no cover
        check("p1_explain 可导入", False, "%s: %s" % (type(exc).__name__, exc))

    # ---------- 汇总 ----------
    print("\n" + "=" * 70)
    print("批次 A 回归结果: PASS=%d  FAIL=%d" % (PASS, FAIL))
    if FAILURES:
        print("-" * 70)
        for f in FAILURES:
            print("  FAIL: %s" % f)
    print("=" * 70)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
