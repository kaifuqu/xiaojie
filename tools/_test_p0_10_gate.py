# -*- coding: utf-8 -*-
"""
tools/_test_p0_10_gate.py —— P0-10 应到交易日口径 + 战法榜连接回退 回归

锁定两次事故：
  1) 2026-11-06：交易日但日历未同步当日 → 应到退回库内最新 → 判「合格」，当日缺失被掩盖。
  2) 2026-11-10：daemon 停机 4 天后重启，开市缓存停更 → 同样退回 → 打印「合格」，
     实际 11-09/11-10 两个交易日都缺。故降级口径一律不得输出「合格」结论。
  3) 2026-11-10：战法榜 p4 因共享 DuckDB 连接被占用整榜跳过。

本脚本不写死日期，随系统当天日期自适应。
"""
import os
import sys

sys.stdout.reconfigure(errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FAILS = []


def check(label, ok, detail=""):
    tag = "[PASS]" if ok else "[FAIL]"
    if not ok:
        FAILS.append(label)
    print(f"  {tag} {label}  {detail}")


class FakeCon:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, sql, params=None):
        return self

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _shift(yyyymmdd, n):
    from datetime import datetime, timedelta

    d = datetime.strptime(yyyymmdd, "%Y%m%d").date() + timedelta(days=n)
    return d.strftime("%Y%m%d")


def main() -> int:
    import verify_system as V
    from datetime import datetime
    from zoneinfo import ZoneInfo

    today = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d")
    prev = _shift(today, -1)
    prev2 = _shift(today, -2)

    print("=" * 72)
    print(f"【P0-10 应到交易日口径回归】今日={today}")
    print("=" * 72)

    rows_no_today = [(f"{prev[:4]}-{prev[4:6]}-{prev[6:]}", True),
                     (f"{prev2[:4]}-{prev2[4:6]}-{prev2[6:]}", True)]
    rows_with_today = [(f"{today[:4]}-{today[4:6]}-{today[6:]}", True)] + rows_no_today

    print("\n=== 1. 当日开市缓存读取 ===")
    real = V._today_is_open_cached(today)
    print(f"  _today_is_open_cached({today}) = {real}")
    check("1a 当日缓存可读（daemon 每日写入）", real is not None, "value=%s" % real)
    # 1b：探针日期必须「确定不是缓存里记录的日期」。
    # 旧写法直接用 prev，daemon 停机（缓存停在昨天）时 prev 恰好等于缓存日期，
    # 会把「日期键控正确」误判成「误用缓存」。先读缓存日期，再挑一个不同的探针。
    _cache_date = ""
    try:
        import json as _json
        from core.runtime_data_paths import STATE_DIR as _SD
        with open(os.path.join(_SD, "sniper_trade_cal_cache.json"), "r", encoding="utf-8") as _f:
            _cache_date = str((_json.load(_f) or {}).get("date") or "").replace("-", "")[:8]
    except Exception:
        _cache_date = ""
    probe = next(d for d in (prev, prev2, today) if d != _cache_date)
    check("1b 非当日日期不误用缓存", V._today_is_open_cached(probe) is None,
          "probe=%s cache=%s" % (probe, _cache_date or "无"))

    print("\n=== 2. 事故场景：日历未同步今日 + 缓存开市 ===")
    V._qc_ro_conn = lambda: FakeCon(rows_no_today)
    real_fn = V._today_is_open_cached
    V._today_is_open_cached = lambda t: True
    exp, days, src = V._expected_latest_trade_date()
    print(f"  应到={exp} 口径={src}")
    check("2a 应到日期为今天（不退回库内最新）", exp == today, "exp=%s" % exp)
    check("2b 口径标注为开市缓存", "当日开市缓存" in src, src)
    check("2c 交易日序列含今天", today in days)

    print("\n=== 3. 今日休市仍要求最近交易日 ===")
    V._today_is_open_cached = lambda t: False
    exp2, _, src2 = V._expected_latest_trade_date()
    print(f"  应到={exp2} 口径={src2}")
    check("3a 休市退回最近已记录交易日", exp2 == prev, "exp=%s" % exp2)
    check("3b 口径标注为休市", "休市" in src2, src2)

    print("\n=== 4. 无缓存保持原降级行为 ===")
    V._today_is_open_cached = lambda t: None
    exp3, _, src3 = V._expected_latest_trade_date()
    print(f"  应到={exp3} 口径={src3}")
    check("4a 无缓存退回旧口径",
          exp3 == prev and "退回最晚已记录交易日" in src3, src3)

    print("\n=== 5. 今日已在日历（正常路径） ===")
    V._qc_ro_conn = lambda: FakeCon(rows_with_today)
    V._today_is_open_cached = real_fn
    exp4, _, src4 = V._expected_latest_trade_date()
    print(f"  应到={exp4} 口径={src4}")
    check("5a 今日已记录时用今天", exp4 == today and "今日已记录" in src4, src4)

    print("\n=== 6. 降级口径不得输出「合格」 ===")
    src_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "verify_system.py")
    with open(src_path, "r", encoding="utf-8") as f:
        code = f.read()
    has_degraded_flag = "_degraded_cal" in code
    # 降级分支必须是 p_info 中性提示，不能是 p_ok("...合格...")
    check("6a 已定义降级口径标记", has_degraded_flag)
    check("6b 降级分支改用中性提示", "数据新鲜度按降级口径自洽" in code)
    check("6c 旧的无条件合格打印已移除",
          'if latest >= expected_latest:\n                p_ok(f"数据新鲜度合格' not in code)

    print("\n=== 7. 战法榜：共享连接被占用时回退独立连接 ===")
    import core.paper_trading.postmarket_attribution as PA
    import data.db_core as dbc

    class SharedCon:
        pass

    class FreshCon:
        pass

    calls = {"n": 0}

    def fake_build(con, pool):
        calls["n"] += 1
        if isinstance(con, SharedCon):
            raise RuntimeError(
                "Resource In Use Error: connection is busy with another call; "
                "it refuses instead of waiting, so use a separate connection from cursor()"
            )
        return {"available": True, "rows": [{"a": 1}], "pool": pool}

    class Ctx:
        def __enter__(self):
            return FreshCon()

        def __exit__(self, *a):
            return False

    PA._build = fake_build
    dbc.get_read_conn = lambda **kw: Ctx()

    r = PA.build_pool_leaderboard(pool="p4", con=SharedCon())
    print(f"  结果 available={r.get('available')} 调用次数={calls['n']}")
    check("7a busy 时成功回退", r.get("available") is True, str(r)[:80])
    check("7b 确实重试了一次", calls["n"] == 2, "calls=%s" % calls["n"])

    print("\n=== 8. 战法榜：非 busy 错误不掩盖 ===")
    def fake_build_other(con, pool):
        raise RuntimeError("Catalog Error: some other failure")

    PA._build = fake_build_other
    calls["n"] = 0
    r2 = PA.build_pool_leaderboard(pool="p4", con=SharedCon())
    print(f"  结果 available={r2.get('available')} error={r2.get('error')}")
    check("8a 其他错误照常返回不可用", r2.get("available") is False)
    check("8b 不会误判为 busy 重试",
          "Resource In Use" not in str(r2.get("error") or ""), str(r2.get("error"))[:60])

    print("\n" + "=" * 72)
    if FAILS:
        print(f"失败 {len(FAILS)} 项: {FAILS}")
        return 1
    print("✅ P0-10 口径 + 战法榜回退 回归全部通过")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
