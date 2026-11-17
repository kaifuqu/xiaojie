# -*- coding: utf-8 -*-
"""验证真实 pipeline 信号行的字段能否被 signal_filter 正确提取

这是 shadow 上线的关键前提：如果字段提取不上，五层过滤器就会大面积
"缺数据跳过"，观察期看到的 reject 分布就毫无参考价值。
"""
import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

out = io.open("tools/_field_result.txt", "w", encoding="utf-8")


def p(*a):
    s = " ".join(str(x) for x in a)
    print(s)
    out.write(s + "\n")


from core.signal_filter_adapter import _extract_inputs, evaluate_row

# 模拟 pipeline 真实信号行的结构（中文键）+ rt 实时快照
row = {
    "代码": "300750.SZ",
    "名称": "某某股份",
    "综合分": 78.5,
    "现价": "123.45",
    "战法": "P3-01·主力回踩",
    "流通市值": 600000.0,       # 万元
    "总市值": 900000.0,
    "换手率": 3.2,
    "量比": 1.85,
    "市盈率": 25.0,
    "市净率": 3.1,
    "主力净流入": 800.0,
    "成交额": 80000.0,
    "乖离率": 3.5,
    "连阴": 0,
    "昨收": "122.00",
}
rt = {
    "high": 124.00,
    "open": 122.80,
    "low": 122.00,
    "pre_close": 122.00,
    "close": 123.45,
    "price": 123.45,
}

p("=" * 72)
p("【字段提取验证】模拟 pipeline 真实信号行")
p("=" * 72)

x = _extract_inputs(row, rt)
p("\n=== 提取结果 ===")
for k, v in x.items():
    if k.startswith("_"):
        continue
    flag = "  ✅" if v is not None else "  ⚠️ 缺失"
    p("  %-24s = %-14s%s" % (k, v, flag))

p("\n=== 各层可评估性 ===")
v = evaluate_row(row, rt, mode="shadow")
p("  evaluated_layers = %s" % v.evaluated_layers)
p("  skipped          = %s" % v.skipped)
p("  passed           = %s" % v.passed)
p("  reject_layer     = %s" % v.reject_layer)
p("  reject_reason    = %s" % v.reject_reason)

missing = [k for k in ("score", "net_main_wan", "turnover_f", "vol_ratio",
                       "circ_mv_yi", "amount_wan", "pe_ttm", "pb", "total_mv_yi")
           if x.get(k) is None]
if missing:
    p("\n  ⚠️ 缺失字段: %s" % missing)
    p("     → 对应层会被跳过，观察期统计会失真")
else:
    p("\n  ✅ 全部关键字段提取成功（L1/L2/L4/L5 均可评估）")

p("\n=== 上影线计算核对 ===")
p("  上影线 = (high - max(open,close)) / pre_close * 100")
p("         = (124.00 - max(122.80,123.45)) / 122.00 * 100")
p("         = %.2f%%" % x.get("upper_shadow_pct", 0))
p("  → L3 可评估" if x.get("upper_shadow_pct") is not None else "  → L3 会被跳过")

out.close()
print("done")
