# -*- coding: utf-8 -*-
"""signal_filter 端到端验证：模拟 pipeline 的真实调用路径"""
import io
import os
import sys

os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
out = io.open("tools/_e2e_out.txt", "w", encoding="utf-8")


def p(*a):
    s = " ".join(str(x) for x in a)
    print(s)
    out.write(s + "\n")


from core.signal_filter_adapter import evaluate_row, should_block, MODE_DISABLED, MODE_SHADOW, MODE_ENFORCE

row_ok = {"综合分": 78, "代码": "300750.SZ", "名称": "测试票", "战法": "P3-01"}
row_low = {"综合分": 35, "代码": "600000.SH", "名称": "低分票", "战法": "P3-01"}
rt_ok = {
    "net_main_amount": 800.0, "turnover_rate_f": 3.2, "vol_ratio": 1.8,
    "circ_mv": 600000.0, "amount": 80000.0,
    "pe_ttm": 25.0, "pb": 3.0, "roe_pct": 12.0, "total_mv_yi": 90.0,
    "price": 10.0, "open": 10.0, "high": 10.2, "low": 9.95, "pre_close": 10.0,
    "close": 10.1, "bias_ma20": 3.0,
}
rt_empty = {}

p("=" * 70)
p("【端到端验证】signal_filter 三种模式")
p("=" * 70)

p("\n① mode=disabled（默认上线状态）")
v = evaluate_row(row_ok, rt_ok, mode=MODE_DISABLED)
p(f"   passed={v.passed} enabled={v.enabled} mode={v.mode}")
assert v.passed and not v.enabled, "disabled 必须完全放行"
assert should_block(v) is False, "disabled 绝不可拦截"

p("\n② mode=shadow（观察期）")
v1 = evaluate_row(row_ok, rt_ok, mode=MODE_SHADOW)
p(f"   合格票: passed={v1.passed} layers={v1.evaluated_layers} short={v1.short()!r}")
v2 = evaluate_row(row_low, rt_ok, mode=MODE_SHADOW)
p(f"   低分票: passed={v2.passed} reject_layer={v2.reject_layer} short={v2.short()!r}")
assert should_block(v1) is False, "shadow 下合格票不拦"
assert should_block(v2) is False, "shadow 下不合格票也不拦（只标注）"
assert v2.reject_layer == 1, "shadow 下仍应识别出 L1 不合格"

p("\n③ mode=enforce（拦截期）")
v3 = evaluate_row(row_low, rt_ok, mode=MODE_ENFORCE)
p(f"   低分票: passed={v3.passed} reject_layer={v3.reject_layer} reason={v3.reject_reason!r}")
assert v3.passed is False and should_block(v3) is True, "enforce 必须拦截"

p("\n④ 缺数据安全（关键：绝不因数据缺失清空信号）")
v4 = evaluate_row(row_ok, rt_empty, mode=MODE_ENFORCE)
p(f"   全缺数据: passed={v4.passed} skipped={v4.skipped}")
assert v4.passed is True, "缺数据必须放行"
assert len(v4.skipped) > 0, "应记录跳过的层"

p("\n⑤ 过滤器异常不阻断（模拟 pipeline 的 try/except）")
try:
    bad = evaluate_row(row_ok, {"net_main_amount": "非数字"}, mode=MODE_ENFORCE)
    p(f"   脏数据: passed={bad.passed} skipped={bad.skipped}")
    assert bad.passed is True, "脏数据应放行而非抛错"
except Exception as e:
    p(f"   抛异常（pipeline 会捕获并放行）: {type(e).__name__}")

p("\n⑥ 阈值可配置生效")
from core import signal_filter as SF
old = dict(SF.FILTER_THRESHOLDS)
SF.apply_threshold_overrides({"L1_base_score": 20.0})
v5 = evaluate_row(row_low, rt_ok, mode=MODE_ENFORCE)
p(f"   阈值降到 20 后，低分票 passed={v5.passed}（原为 False）")
assert v5.passed is True, "阈值覆盖应生效"
SF.FILTER_THRESHOLDS.clear()
SF.FILTER_THRESHOLDS.update(old)
p("   已还原默认阈值")

p("\n" + "=" * 70)
p("✅ 端到端验证全部通过")
p("=" * 70)
out.close()
print("\nE2E_OK")
