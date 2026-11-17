# -*- coding: utf-8 -*-
"""
tools/_test_pipeline_block.py —— pipeline 过滤器接入块的集成验证

【为什么需要这个测试】
真实扫描在当前环境（盘后 / 底仓仅数十只）产生不了候选，
过滤器块「每只候选票执行一次」，没候选就走不到 —— 这掩盖了接入逻辑的正确性。
但接入逻辑一旦写错（变量作用域错、res 非 dict、拦截分支漏 continue…），
会在**下次真有候选的交易日**才爆出来，那时已经影响生产。

所以这里把 pipeline 里那段接入代码**原样复刻**到一个受控上下文，
用 signal_log 里的真实历史行喂进去，逐条验证：
  1. disabled → 票数完全不变，且不写风险标签（行为与改造前一致）
  2. shadow   → 票数完全不变（绝不拦票），但不合格票带上风险标签 + 计入统计
  3. enforce  → 不合格票被降级为 observation_only（真正拦截）
  4. 过滤器抛异常 → 票照常进入结果（绝不阻断选股）

【安全】纯内存，不写库不推送。
"""
from __future__ import annotations

import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

_OUT = io.open(os.path.join(ROOT, "tools", "_pipeline_block_result.txt"), "w", encoding="utf-8")
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


# ---------------------------------------------------------------------------
# 与 core/scan/pipeline.py 里那段接入代码**逐行等价**的复刻
# （改动 pipeline 时必须同步这里，否则测试失去意义）
# ---------------------------------------------------------------------------
def run_filter_block(rows, rt_map, mode, stats_res, route_calls, raise_eval=False):
    """返回实际进入「正式池」的行数。

    rows: [row dict]（会被就地修改风险标签，与 pipeline 一致）
    route_calls: list，记录每次 _route_scored_pool_row 的 (row, observation_only)
    """
    import logging

    if raise_eval:
        class _Boom:
            def __call__(self, *a, **k):
                raise RuntimeError("模拟过滤器崩溃")

        _sf_eval = _Boom()
        _sf_block = lambda v: False  # noqa: E731
    else:
        from core.signal_filter_adapter import evaluate_row as _sf_eval
        from core.signal_filter_adapter import should_block as _sf_block

    main_count = 0
    for b in rows:
        rt = rt_map.get(b.get("代码")) or {}
        try:
            _sf_verdict = _sf_eval(b, rt, mode=mode)
            if _sf_verdict.enabled:
                _sf_note = _sf_verdict.short()
                if _sf_note:
                    _tags = str(b.get("风险标签", "") or "").strip()
                    b["风险标签"] = f"{_tags} | {_sf_note}".strip(" |") if _tags else _sf_note
                _sf_stats = stats_res.setdefault("signal_filter_stats", {})
                _key = f"L{_sf_verdict.reject_layer}" if _sf_verdict.reject_layer else "pass"
                _sf_stats[_key] = int(_sf_stats.get(_key, 0) or 0) + 1
                _sf_stats["total"] = int(_sf_stats.get("total", 0) or 0) + 1
                if _sf_block(_sf_verdict):
                    _sf_stats["blocked"] = int(_sf_stats.get("blocked", 0) or 0) + 1
                    logging.info("[signal_filter] 拦截 %s", b.get("名称", ""))
                    route_calls.append((b, True))   # observation_only=True
                    continue
        except Exception as _sf_err:
            logging.warning("[signal_filter] 过滤异常（已放行）: %s", _sf_err)
        route_calls.append((b, False))
        main_count += 1
    return main_count


def load_real_rows(limit=60):
    """从 signal_log 取真实历史行，构造成 pipeline 候选行结构。"""
    from data.db_core import get_read_conn

    with get_read_conn(read_only=True) as con:
        rows = con.execute(
            """
            SELECT ts_code, name, score, burst_score, push_price, strategy
            FROM signal_log
            WHERE pushed = TRUE AND push_price IS NOT NULL AND push_price > 0
            ORDER BY log_date DESC LIMIT ?
            """,
            [limit],
        ).fetchall()

    out_rows, rt_map = [], {}
    for ts_code, name, score, burst, price, strategy in rows:
        out_rows.append({
            "代码": ts_code,
            "名称": name or "",
            "综合分": float(score if score is not None else (burst or 0)),
            "战法": strategy or "",
        })
        rt_map[ts_code] = {"price": float(price)}
    return out_rows, rt_map


def main():
    p("=" * 74)
    p("【pipeline 过滤器接入块 集成验证】")
    p("=" * 74)

    rows, rt_map = load_real_rows(60)
    p(f"\n真实历史信号样本: {len(rows)} 条（来自 signal_log）")
    if not rows:
        p("⚠️ 无样本，跳过")
        _OUT.close()
        return 0

    def _stats_of(container):
        """pipeline 里统计是**嵌在** res['signal_filter_stats'] 下的，取出来。"""
        return (container or {}).get("signal_filter_stats") or {}

    # ---- 1. disabled：票数不变，无标签 ----
    p("\n=== 1. disabled（默认/回滚状态）===")
    r1 = [dict(x) for x in rows]
    st1, calls1 = {}, []
    n1 = run_filter_block(r1, rt_map, "disabled", st1, calls1)
    tagged1 = sum(1 for x in r1 if str(x.get("风险标签", "")).strip())
    p(f"  进入正式池: {n1}/{len(r1)}   带标签: {tagged1}   统计: {_stats_of(st1)}")
    check("1a 全部进入正式池（票数不变）", n1 == len(r1), f"{n1} vs {len(r1)}")
    check("1b 不写风险标签", tagged1 == 0)
    check("1c 无统计（完全未参与）", not _stats_of(st1))
    check("1d 无 observation_only 降级", all(not o for _, o in calls1))

    # ---- 2. shadow：票数不变，但有标签 + 统计 ----
    p("\n=== 2. shadow（当前上线状态）===")
    r2 = [dict(x) for x in rows]
    st2, calls2 = {}, []
    n2 = run_filter_block(r2, rt_map, "shadow", st2, calls2)
    s2 = _stats_of(st2)
    tagged2 = sum(1 for x in r2 if str(x.get("风险标签", "")).strip())
    p(f"  进入正式池: {n2}/{len(r2)}   带标签: {tagged2}   blocked: {s2.get('blocked', 0)}")
    p(f"  统计分布: { {k: v for k, v in s2.items()} }")
    check("2a **票数完全不变（shadow 绝不拦票）**", n2 == len(r2), f"{n2} vs {len(r2)}")
    check("2b blocked 必须为 0", int(s2.get("blocked", 0)) == 0)
    check("2c 有统计 total", int(s2.get("total", 0)) == len(r2), f"total={s2.get('total')}")
    check("2d 产生了风险标签", tagged2 > 0, f"tagged={tagged2}")
    check("2e 未降级任何票", all(not o for _, o in calls2))

    # 拒绝率（观察期的核心指标）
    tot = int(s2.get("total", 0) or 0) or 1
    rej = sum(int(s2.get(k, 0) or 0) for k in s2 if k.startswith("L"))
    p(f"\n  观察期 reject 率 = {rej}/{tot} = {rej / tot * 100:.1f}%")

    # ---- 3. enforce：真正拦截 ----
    p("\n=== 3. enforce（未来切换目标）===")
    r3 = [dict(x) for x in rows]
    st3, calls3 = {}, []
    n3 = run_filter_block(r3, rt_map, "enforce", st3, calls3)
    s3 = _stats_of(st3)
    blocked3 = int(s3.get("blocked", 0) or 0)
    p(f"  进入正式池: {n3}/{len(r3)}   被拦: {blocked3}")
    check("3a enforce 下被拦数 > 0（确实生效）", blocked3 > 0, f"blocked={blocked3}")
    check("3b 进入正式池数 = 总数 - 被拦数",
          n3 == len(r3) - blocked3, f"{n3} vs {len(r3) - blocked3}")
    check("3c 被拦票走 observation_only",
          sum(1 for _, o in calls3 if o) == blocked3,
          f"{sum(1 for _, o in calls3 if o)} vs {blocked3}")

    # ---- 4. 异常不阻断 ----
    p("\n=== 4. 过滤器崩溃时绝不阻断选股 ===")
    r4 = [dict(x) for x in rows]
    st4, calls4 = {}, []
    n4 = run_filter_block(r4, rt_map, "enforce", st4, calls4, raise_eval=True)
    p(f"  过滤器崩溃 → 进入正式池: {n4}/{len(r4)}")
    check("4a 崩溃时全部照常进入（fail-open）", n4 == len(r4), f"{n4} vs {len(r4)}")

    # ---- 5. 统计容器契约 ----
    p("\n=== 5. 统计容器契约 ===")
    check("5a 统计嵌在 res['signal_filter_stats'] 下（与 pipeline 一致）",
          isinstance(_stats_of(st2), dict) and int(_stats_of(st2).get("total", 0)) > 0)

    p("\n" + "=" * 74)
    if FAILS:
        p("❌ 失败 %d 项：%s" % (len(FAILS), FAILS))
        _OUT.close()
        return 1
    p("✅ pipeline 接入块集成验证全部通过")
    p("=" * 74)
    _OUT.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
