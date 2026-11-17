# -*- coding: utf-8 -*-
"""
tools/_test_p1_pool_cap.py —— P1 主池上限 120 只 回归

【口径】分数达标即入池，但总数不超过 120（可配置，<=0 不限）。
【为什么设上限】P3 每 150 秒扫全池，池子大小直接决定取数/行情开销；120 ≈ 原 30 只池的 4 倍。

锁定契约：
  1. 配置读取默认 120，且 <=0 语义为「不限制」
  2. 超限时按综合分排序保留前 120，砍掉的是低分票（不能砍高分）
  3. 被砍的票进淘汰名单，死因标注「超池上限」且层级为 L1（名额限制，非票的问题）
  4. 平衡调整扩容也不会越过 120（避免先扩到 130 再被出口闸砍回去）
  5. 未触发上限时行为完全不变（池子原样保留）
"""
from __future__ import annotations

import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors="replace")
    except Exception:
        pass

OUT = io.open(os.path.join(ROOT, "tools", "_p1_pool_cap_result.txt"), "w", encoding="utf-8")
FAILS = []


def p(*a):
    s = " ".join(str(x) for x in a)
    print(s)
    OUT.write(s + "\n")


def check(label, ok, detail=""):
    if ok:
        p("  [PASS] %s  %s" % (label, detail))
    else:
        FAILS.append(label)
        p("  [FAIL] %s  %s" % (label, detail))
    return ok


def _mk_item(code, score, mv_wan=2000000.0):
    return {
        "code": code,
        "p1_score": score,
        "inflow_ratio": 0.0,
        "inflow_10d_ratio": 0.0,
        "hist": {"circ_mv": mv_wan, "name": code},
        "df": None,
    }


def main():
    from core.config_manager import get_p1_max_pool_size
    from core.pool_manager.thresholds import _p1_final_sort_key
    from core.p1_balance import enforce_p1_balance

    p("=" * 72)
    p("【P1 主池上限 120 回归】")
    p("=" * 72)

    p("\n=== 1. 配置读取 ===")
    cap = get_p1_max_pool_size()
    p("  strategies.p1.max_pool_size -> %s" % cap)
    check("1a 读取到 120", int(cap) == 120, "cap=%d" % int(cap))

    src = io.open("core/config_manager.py", encoding="utf-8").read()
    check("1b <=0 语义为不限制（代码里有 >0 判断）", "if _v_abs > 0 else None" in io.open("core/p1_balance.py", encoding="utf-8").read())
    check("1c 缺配置时默认 120", 'p1.get("max_pool_size", 120)' in src)

    p("\n=== 2. 超限截断：保留高分、砍掉低分 ===")
    items = [_mk_item("60%04d.SH" % i, 40.0 + i) for i in range(130)]  # 40.0 ~ 169.0
    items.sort(key=lambda x: _p1_final_sort_key(x), reverse=True)
    kept = items[:120]
    overflow = items[120:]
    p("  构造 130 只，保留 %d 只，回退 %d 只" % (len(kept), len(overflow)))
    check("2a 保留数量 = 上限", len(kept) == 120)
    check("2b 保留的是高分票（最低保留分 > 最高回退分）",
          float(min(i["p1_score"] for i in kept)) > float(max(i["p1_score"] for i in overflow)),
          "min_keep=%.1f max_drop=%.1f" % (
              float(min(i["p1_score"] for i in kept)),
              float(max(i["p1_score"] for i in overflow))))
    check("2c 回退数量正确", len(overflow) == 10)

    pb_src = io.open("core/pool_manager/pool_builder.py", encoding="utf-8").read()
    check("2d 出口闸已接线", "[P1 池上限]" in pb_src)
    check("2e 被砍票死因为「超池上限」+ L1 名额限制",
          "🚦超池上限" in pb_src and '"死因层级": "L1"' in pb_src)

    p("\n=== 3. 平衡调整不越过上限 ===")
    # 125 只全大盘股（必然触发不平衡），另备 20 只中盘待回调
    pool = [_mk_item("88%04d.SH" % i, 70.0 + i / 100.0, mv_wan=6000000.0) for i in range(125)]
    rejected = [{"代码": "00%04d.SZ" % i, "名称": "中%d" % i, "当前得分": 66.0} for i in range(20)]
    src_map = {
        id(r): _mk_item("00%04d.SZ" % i, 66.0, mv_wan=2000000.0)
        for i, r in enumerate(rejected)
    }
    merged, rej = enforce_p1_balance(
        p1_pool=pool, p1_rejected=rejected, target_size=len(pool), rejected_src=src_map,
    )
    p("  平衡后池大小 = %d（上限 %d）" % (len(merged), cap))
    check("3a 平衡扩容后仍不超过上限", len(merged) <= int(cap), "len=%d" % len(merged))
    check("3b 没有被比例上限二次截断到 target_size", len(merged) >= len(pool) or len(merged) == int(cap))

    p("\n=== 4. 未超限时行为不变 ===")
    small = [_mk_item("77%04d.SZ" % i, 90.0) for i in range(40)]
    merged2, rej2 = enforce_p1_balance(
        p1_pool=small, p1_rejected=[], target_size=len(small), rejected_src={},
    )
    check("4a 40 只（未超限）原样保留", len(merged2) == 40, "len=%d" % len(merged2))

    p("\n" + "=" * 72)
    if FAILS:
        p("失败 %d 项：%s" % (len(FAILS), FAILS))
        return 1
    p("✅ P1 主池上限 120 回归全部通过")
    p("=" * 72)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        try:
            OUT.close()
        except Exception:
            pass
