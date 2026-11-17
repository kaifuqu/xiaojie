# -*- coding: utf-8 -*-
r"""
tools/_test_paper_trading.py — 模拟盘端到端回归（隔离 DuckDB，不碰主库）

【运行】
  D:\xiaojie\_pyenv\venv\Scripts\python.exe tools\_test_paper_trading.py

【隔离方式】
  把 data.db_core._connection.db_path 指向临时目录的 quant_data.duckdb，
  本测试全程不读写 D:\xiaojie\data\quant_data.duckdb。

【覆盖】
  T1  建表 + 账户初始化（幂等二次执行）
  T2  买入：整手股数 / 三项费用 / 现金扣减 / 阈值落库
  T3  重复信号去重（已持仓不重复买，只写 SKIP 流水）
  T4  信号过滤：观察池 / C 档 / 0% 仓位 / 无价 / 非法池
  T5  阈值边界：-3.00% 卖 / -2.99% 不卖；+15.00% 卖 / +14.99% 不卖
  T6  T+1：当日买入当日不卖
  T7  停牌（volume=0）/ 跌停封板 → 不卖
  T8  现金不足 → 跳过
  T9  最大持仓数上限
  T10 日净值结算幂等 + 资金恒等式（初始资金 + Σcash_delta == cash）
  T11 战法排行榜聚合（重叠样本 + 样本不足标记）
  T12 开关关闭不下单 + 重放同一批信号幂等
  T13 板块涨跌停幅度（主板10% / 创业板科创板20% / ST 5%）
  T11b 排行榜边界：仅当前持仓 / 空输入不报错
  T14 build_snapshot 字段完整（UI 只读面板依赖）
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import traceback
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 【QA 旁路】测试套件默认禁止真实推送（2026-11-14 审计发现缺此保护：
# 本机代理死亡时，测试触发的企微推送经「直连兜底」真实发出）。
os.environ.setdefault("XJ_NO_PUSH", "1")

# ---- pyarrow 兼容 shim：必须在 import pandas 之前 ----
from core._pyarrow_compat import disable_pyarrow  # noqa: E402

disable_pyarrow()

BJ_TZ = timezone(timedelta(hours=8))
RESULTS = []


def _case(name):
    def deco(fn):
        fn._case_name = name
        return fn
    return deco


def _run(name, fn):
    try:
        fn()
        RESULTS.append((name, "PASS", ""))
        print(f"  [PASS] {name}")
    except AssertionError as e:
        RESULTS.append((name, "FAIL", str(e)))
        print(f"  [FAIL] {name}: {e}")
    except Exception as e:  # noqa: BLE001
        RESULTS.append((name, "ERROR", f"{type(e).__name__}: {e}"))
        print(f"  [ERROR] {name}: {type(e).__name__}: {e}")
        traceback.print_exc()


def _assert_close(a, b, tol=1e-6, msg=""):
    if abs(float(a) - float(b)) > tol:
        raise AssertionError(f"{msg} expected≈{b} got {a} (tol={tol})")


def _sig(ids, code, price, pool="p3", name="测试股", score=80.0):
    return {
        "代码": code,
        "名称": name,
        "现价": f"{price:.2f}",
        "综合分": score,
        "战法": ids,
        "pool_tier": "main",
        "执行层级": "A",
        "建议仓位": "主仓候选: 20%-30%（分批）",
    }


def _setup(tmp_dir):
    import data.db_core._connection as conn

    conn.db_path = os.path.join(tmp_dir, "quant_data.duckdb")
    conn._write_con = None
    conn._read_con = None
    os.makedirs(tmp_dir, exist_ok=True)


def _cfg(enabled=True, **kw):
    from core.paper_trading.config import PaperTradingConfig

    c = PaperTradingConfig()
    c.enabled = enabled
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def _yesterday():
    return (datetime.now(BJ_TZ) - timedelta(days=1)).strftime("%Y%m%d")


def _today():
    return datetime.now(BJ_TZ).strftime("%Y%m%d")


def main():
    tmp_dir = tempfile.mkdtemp(prefix="paper_test_")
    print(f"[setup] 隔离 DuckDB: {tmp_dir}")
    _setup(tmp_dir)

    from core.paper_trading import store
    from core.paper_trading.engine import (
        EXIT_STOP_LOSS,
        EXIT_TAKE_PROFIT,
        board_of,
        compute_shares,
        daily_settle,
        is_st_stock,
        on_signals,
        on_tick,
        parse_signal_row,
    )
    from core.paper_trading.reporting import _build_leaderboard, build_snapshot
    from core.paper_trading.schema import ALL_TABLES, TABLE_POSITION
    from core.paper_trading.store import ensure_account, ensure_tables

    cfg = _cfg(enabled=True, initial_capital=1000000.0)

    def _w(fn):
        """写事务：执行 fn(con)。"""
        return store.write_transaction(fn)()

    def _r(fn):
        """只读查询：执行 fn(con)。"""
        return store.read_query(fn)()

    def _clear_positions():
        def _fn(con):
            for p in store.load_positions(con):
                store.delete_position(con, str(p["code"]))
            return True
        return _w(_fn)

    def _set_cash(amount: float) -> None:
        _w(lambda con: store.update_account(con, cash=float(amount)))

    def _get_cash() -> float:
        return float((_r(lambda con: store.load_account(con)) or {}).get("cash") or 0.0)

    def _reset_account(initial: float = 1000000.0) -> None:
        # 还原到「未交易」状态：清空持仓 + 复位现金 + 清实验进度。
        # 避免单个用例污染后续用例（T8 临时改现金失败后导致 T9/T10/T12 连锁失败）。
        def _fn(con):
            for p in store.load_positions(con):
                store.delete_position(con, str(p["code"]))
            # 订单 / 净值是 append-only 审计流水，不允许物理删除；
            # T10 资金恒等式改用「期初资金 + 全部历史 cash_delta」口径，
            # 因此这里只复位现金即可保持恒等式成立。
            store.update_account(
                con,
                cash=float(initial),
                status="RUNNING",
                trading_day_index=0,
                last_equity_date=None,
            )
            con.execute("DELETE FROM paper_equity_daily")
            return True

        _w(_fn)

    # ------------------------------------------------ T1
    def t1():
        _w(lambda con: ensure_tables(con))
        _w(lambda con: ensure_account(con, initial_capital=1000000.0))
        _w(lambda con: ensure_tables(con))       # 幂等二次
        _w(lambda con: ensure_account(con, initial_capital=1000000.0))  # 幂等

        import data.db_core._connection as conn

        with conn.get_read_conn(read_only=True) as con:
            names = {r[0] for r in con.execute("SELECT table_name FROM duckdb_tables()").fetchall()}
        missing = set(ALL_TABLES) - names
        assert not missing, f"缺表: {missing}"
        assert TABLE_POSITION in names
        a = _r(lambda con: store.load_account(con))
        _assert_close(a["cash"], 1000000.0)
        assert a["status"] == "RUNNING"
        assert int(a["trading_day_index"]) == 0

    # ------------------------------------------------ T2
    def t2():
        out = on_signals("p3", [_sig("P3-01·测试战法", "600000", 10.0)], cfg=cfg)
        assert int(out["bought"]) == 1, out
        p = _r(lambda con: store.load_position(con, "600000"))
        assert p is not None, "持仓未落库"
        _assert_close(p["buy_price_raw"], 10.0, msg="买入价应为信号现价")
        # 【2026-11-04 修正】仓位口径已从「按总资产比例」改为「每笔固定金额」
        #   （PaperTradingConfig 默认 fixed_amount_per_position=50000，与 config.yaml 一致）。
        #   原断言按 total_asset×position_pct 推算 20000 股，早就不适用当前实现。
        #   现按「每笔 5 万」推算；注意买入价含滑点（exec_price = price×(1+slippage)），
        #   所以 50000 ÷ 10.01 = 4995 → 主板整手取 4900 股。费用按实际成交额重算。
        _fixed_amt = float(getattr(cfg, "fixed_amount_per_position", 50000.0))
        _slip = float(getattr(cfg, "slippage_pct", 0.0) or 0.0) / 100.0
        _exec_px = 10.0 * (1.0 + _slip)
        _expect_shares = int(_fixed_amt // _exec_px // 100 * 100)
        assert int(p["shares"]) == _expect_shares, (
            f"每笔{_fixed_amt:.0f}/滑点后{_exec_px:.4f}元={_expect_shares}股，实际 {p['shares']}"
        )
        assert int(p["shares"]) % 100 == 0, "必须整手"
        _exp_amount = round(_expect_shares * _exec_px, 4)
        _exp_comm = max(_exp_amount * 0.0003, 5.0)
        _exp_tf = _exp_amount * 0.00001
        _exp_net = _exp_amount + _exp_comm + _exp_tf
        _assert_close(p["buy_amount"], _exp_amount, 0.05)
        _assert_close(p["buy_commission"], _exp_comm, 0.05)      # 万3，最低 5 元
        _assert_close(p["buy_transfer_fee"], _exp_tf, 0.05)      # 十万分之一
        _assert_close(p["buy_stamp_tax"], 0.0, 0.01, "买入无印花税")
        _assert_close(p["net_cost"], _exp_net, 0.05)
        # 阈值：止损 -3% / 止盈 +8%（V30.11 起统一为 +8%/-3%，与模拟盘执行口径一致）
        #   注意：止损/止盈价按「实际成交价(含滑点)」再按 0.01 元取整（A 股可挂单价）
        _assert_close(p["stop_loss_price"], round(_exec_px * 0.97, 2), 0.011)
        _assert_close(p["take_profit_price"], round(_exec_px * 1.08, 2), 0.011)
        a = _r(lambda con: store.load_account(con))
        _assert_close(a["cash"], 1000000.0 - _exp_net, 0.05)
        o = _r(lambda con: store.load_orders(con, side="BUY"))
        assert len(o) == 1, o
        assert "P3-01" in str(o[0]["strategy_ids"]), o[0]
        _assert_close(o[0]["cash_delta"], -_exp_net, 0.05)

    # ------------------------------------------------ T3
    def t3():
        out = on_signals("p3", [_sig("P3-01·测试战法", "600000", 10.0)], cfg=cfg)
        assert int(out["bought"]) == 0, out
        assert int(out["skipped"]) >= 1, out
        assert len(_r(lambda con: store.load_positions(con))) == 1
        assert len(_r(lambda con: store.load_orders(con, side="BUY"))) == 1
        skips = _r(lambda con: store.load_orders(con, side="SKIP"))
        assert any(str(s["skip_reason"]) == "ALREADY_HELD" for s in skips), skips

    # ------------------------------------------------ T4
    def t4():
        assert parse_signal_row(dict(_sig("P3-01", "600101", 10.0), pool_tier="observation")) is None
        assert parse_signal_row(dict(_sig("P3-01", "600102", 10.0), **{"执行层级": "C"})) is None
        assert parse_signal_row(dict(_sig("P3-01", "600103", 10.0), **{"建议仓位": "0%（仅观察）"})) is None
        assert parse_signal_row(dict(_sig("P3-01", "600104", 0.0), **{"现价": "--"})) is None
        assert parse_signal_row({"代码": ""}) is None
        assert parse_signal_row(None) is None
        assert parse_signal_row(_sig("P3-01", "600105", 10.0)) is not None
        out = on_signals(
            "p3",
            [
                dict(_sig("P3-01", "600101", 10.0), pool_tier="observation"),
                dict(_sig("P3-01", "600102", 10.0), **{"执行层级": "C"}),
            ],
            cfg=cfg,
        )
        assert int(out["bought"]) == 0, out
        assert out.get("reason") == "NO_SIGNAL", out
        assert on_signals("p9", [_sig("P3-01", "600106", 10.0)], cfg=cfg)["reason"] == "INVALID_POOL"

    # ------------------------------------------------ T5
    def t5():
        _clear_positions()
        out = on_signals("p3", [_sig("P3-02·边界", "600201", 10.00)], cfg=cfg)
        assert int(out["bought"]) == 1, out

        def _set_old(code):
            def _fn(con):
                con.execute("UPDATE paper_position SET buy_date = ? WHERE code = ?", [_yesterday(), code])
                return True
            return _w(_fn)

        _set_old("600201")
        r1 = on_tick(quotes={"600201": {"price": 9.71, "pre_close": 10.0, "volume": 100000}}, cfg=cfg)
        assert int(r1["sold"]) == 0, f"-2.9% 不应卖: {r1}"
        r2 = on_tick(quotes={"600201": {"price": 9.70, "pre_close": 10.0, "volume": 100000}}, cfg=cfg)
        assert int(r2["sold"]) == 1, f"-3.00% 应卖: {r2}"
        t = [x for x in _r(lambda con: store.load_closed_positions(con)) if x["code"] == "600201"]
        assert len(t) == 1, t
        assert t[0]["exit_reason"] == EXIT_STOP_LOSS, t[0]
        assert float(t[0]["pnl"]) < 0, t[0]

        out = on_signals("p3", [_sig("P3-03·边界", "600202", 10.00)], cfg=cfg)
        assert int(out["bought"]) == 1, out
        _set_old("600202")
        # 【2026-11-04 修正】止盈口径已统一为 +8%（V30.11 起，模拟盘/回测/文案同源）。
        #   原断言用 +15% 边界（11.49/11.50），那是更早期的口径，早已与实现不符。
        #   现按 +8% → 买入 10.00 → 止盈价 10.80；7.9%（10.79）不应卖，8.0%（10.80）应卖。
        r3 = on_tick(quotes={"600202": {"price": 10.79, "pre_close": 10.0, "volume": 100000}}, cfg=cfg)
        assert int(r3["sold"]) == 0, f"+7.9% 不应卖: {r3}"
        r4 = on_tick(quotes={"600202": {"price": 10.80, "pre_close": 10.0, "volume": 100000}}, cfg=cfg)
        assert int(r4["sold"]) == 1, f"+8.00% 应卖: {r4}"
        t2 = [x for x in _r(lambda con: store.load_closed_positions(con)) if x["code"] == "600202"]
        assert len(t2) == 1, t2
        assert t2[0]["exit_reason"] == EXIT_TAKE_PROFIT, t2[0]
        assert float(t2[0]["pnl"]) > 0, t2[0]

    # ------------------------------------------------ T5b 当日跌幅离场
    # 【2026-11-05】用户口径：当天（相对昨收）跌 3% 就无条件跑，
    #   不管这笔持仓是浮盈还是浮亏 —— 与按买入价算的 -3% 止损是两条线。
    def t5b():
        _clear_positions()
        out = on_signals("p3", [_sig("P3-08·当日跌幅", "600501", 10.00)], cfg=cfg)
        assert int(out["bought"]) == 1, out

        def _fn(con):
            con.execute("UPDATE paper_position SET buy_date = ? WHERE code = ?", [_yesterday(), "600501"])
            return True
        _w(_fn)

        # 昨收 10.80：跌 2.87%（10.49）不卖（同时也没到 +8% 止盈 / -3% 止损）
        r1 = on_tick(quotes={"600501": {"price": 10.49, "pre_close": 10.80, "volume": 100000}}, cfg=cfg)
        assert int(r1["sold"]) == 0, f"当日跌 2.87% 不应卖: {r1}"
        # 跌 3.06%（10.47）→ 当日跌幅线 10.80×0.97=10.48 命中，且此刻相对买入价是浮盈
        r2 = on_tick(quotes={"600501": {"price": 10.47, "pre_close": 10.80, "volume": 100000}}, cfg=cfg)
        assert int(r2["sold"]) == 1, f"当日跌 3% 应无条件卖: {r2}"
        t = [x for x in _r(lambda con: store.load_closed_positions(con)) if x["code"] == "600501"]
        assert len(t) == 1, t
        from core.paper_trading.engine import EXIT_DAY_DROP

        assert t[0]["exit_reason"] == EXIT_DAY_DROP, t[0]
        assert float(t[0]["pnl"]) > 0, f"该笔是浮盈离场（证明不看持仓盈亏），实际 {t[0]['pnl']}"
        assert "dchg=" in str(t[0].get("note") or ""), t[0]

        # 关闭该规则（day_drop_exit_pct=0）→ 同一报价不应离场
        out2 = on_signals("p3", [_sig("P3-09·当日跌幅关闭", "600502", 10.00)], cfg=cfg)
        assert int(out2["bought"]) == 1, out2

        def _make_old(con):
            con.execute("UPDATE paper_position SET buy_date = ? WHERE code = ?", [_yesterday(), "600502"])
            return True

        _w(_make_old)
        cfg_off = _cfg(enabled=True, day_drop_exit_pct=0.0)
        r3 = on_tick(quotes={"600502": {"price": 10.47, "pre_close": 10.80, "volume": 100000}}, cfg=cfg_off)
        assert int(r3["sold"]) == 0, f"规则关闭后不应卖: {r3}"
        _clear_positions()

    # ------------------------------------------------ T6
    def t6():
        out = on_signals("p3", [_sig("P3-04·T1", "600301", 10.00)], cfg=cfg)
        assert int(out["bought"]) == 1, out
        # 用 9.60（-4%，非跌停）验证 T+1：当日买入不卖
        r1 = on_tick(quotes={"600301": {"price": 9.60, "pre_close": 10.0, "volume": 100000}}, cfg=cfg)
        assert int(r1["sold"]) == 0, f"当日买入不应卖: {r1}"
        assert any(p["code"] == "600301" for p in _r(lambda con: store.load_positions(con)))

        def _fn(con):
            con.execute("UPDATE paper_position SET buy_date = ? WHERE code = ?", [_yesterday(), "600301"])
            return True
        _w(_fn)
        r2 = on_tick(quotes={"600301": {"price": 9.60, "pre_close": 10.0, "volume": 100000}}, cfg=cfg)
        assert int(r2["sold"]) == 1, f"T+1 解禁后应卖: {r2}"

    # ------------------------------------------------ T7
    def t7():
        out = on_signals("p3", [_sig("P3-05·异常", "600401", 10.00)], cfg=cfg)
        assert int(out["bought"]) == 1, out

        def _fn(con):
            con.execute("UPDATE paper_position SET buy_date = ? WHERE code = ?", [_yesterday(), "600401"])
            return True
        _w(_fn)
        r1 = on_tick(quotes={"600401": {"price": 9.00, "pre_close": 10.0, "volume": 0}}, cfg=cfg)
        assert int(r1["sold"]) == 0, f"停牌不应卖: {r1}"
        assert any(s["code"] == "600401" for s in r1.get("skipped", [])), r1
        r2 = on_tick(quotes={"600401": {"price": 9.00, "pre_close": 10.0, "volume": 1000}}, cfg=cfg)
        assert int(r2["sold"]) == 0, f"跌停封板不应卖: {r2}"
        r3 = on_tick(quotes={"600401": {"price": 9.05, "pre_close": 10.0, "volume": 1000}}, cfg=cfg)
        assert int(r3["sold"]) == 1, f"非跌停价应卖: {r3}"

    # ------------------------------------------------ T7b
    # 【V30.5 2026-10-18】涨停买入过滤的生产链集成验证。
    # 历史缺陷：买入链只读 signal_raw['昨收']，而 pipeline/P3/P4 的输出行里
    # 从来没有中文 '昨收' 键（只有 pre_close / 涨幅 等），于是 prev_close 恒为 0，
    # 「涨停封板买不到」这条规则在生产中从未生效。
    # 这里用 pipeline 的真实字段形态（无 '昨收'）验证两级回退 + 涨停跳过。
    def t7b():
        _reset_account()
        cfg_l = _cfg(enabled=True, initial_capital=1000000.0, position_pct=20.0)

        # 场景 A：信号行 *没有* 中文 '昨收'，但带 pre_close → 应判定涨停并写 SKIP_LIMIT_UP
        sig_lu = _sig("P3-07·涨停", "600601", 11.00, name="涨停测试")
        sig_lu["pre_close"] = 10.00
        out_a = on_signals("p3", [sig_lu], cfg=cfg_l)
        assert int(out_a["bought"]) == 0, f"涨停不应买入: {out_a}"
        sk_a = _r(lambda con: store.load_orders(con, side="SKIP"))
        ra = [str(s["skip_reason"]) for s in sk_a if str(s["code"]) == "600601"]
        assert any("LIMIT_UP" in r for r in ra), f"应写 LIMIT_UP skip，实际 {ra}"

        # 场景 B：中文 '昨收' 别名仍生效（回退链第 1 级）
        _reset_account()
        sig_lu2 = _sig("P3-07b·涨停别名", "600602", 11.00, name="涨停测试2")
        sig_lu2["昨收"] = 10.00
        out_b = on_signals("p3", [sig_lu2], cfg=cfg_l)
        assert int(out_b["bought"]) == 0, f"涨停(昨收别名)不应买入: {out_b}"
        sk_b = _r(lambda con: store.load_orders(con, side="SKIP"))
        rb = [str(s["skip_reason"]) for s in sk_b if str(s["code"]) == "600602"]
        assert any("LIMIT_UP" in r for r in rb), f"应写 LIMIT_UP skip(昨收别名)，实际 {rb}"

        # 场景 C：未到涨停价 → 正常买入（确认过滤没有误杀）
        _reset_account()
        sig_ok = _sig("P3-07c·非涨停", "600603", 10.50, name="正常测试")
        sig_ok["pre_close"] = 10.00
        out_c = on_signals("p3", [sig_ok], cfg=cfg_l)
        assert int(out_c["bought"]) == 1, f"非涨停应可买入: {out_c}"

        # 场景 D：既无字段也无 daily_data 兜底 → 不阻塞买入（降级口径）
        _reset_account()
        out_d = on_signals("p3", [_sig("P3-07d·无昨收", "600699", 10.00, name="无昨收测试")], cfg=cfg_l)
        assert int(out_d["bought"]) == 1, f"无昨收不应阻塞买入: {out_d}"

        # 场景 E：新股名称 N/C 开头 + 同等涨幅 → 不套涨停（无涨跌幅窗口）
        _reset_account()
        sig_new = _sig("P3-07e·新股", "301569.SZ", 20.00, name="N联亚药业")
        sig_new["pre_close"] = 10.00
        out_e = on_signals("p3", [sig_new], cfg=cfg_l)
        assert int(out_e["bought"]) == 1, f"新股无涨跌幅窗口不应被判涨停: {out_e}"

        # 场景 F：同一价格但非新股名称 → 应判涨停拦截（证明 E 生效于名称而非价格）
        _reset_account()
        sig_old = _sig("P3-07f·老股对照", "301568.SZ", 20.00, name="联亚药业")
        sig_old["pre_close"] = 10.00
        out_f = on_signals("p3", [sig_old], cfg=cfg_l)
        assert int(out_f["bought"]) == 0, f"老股+80%应判涨停: {out_f}"
        sk_f = _r(lambda con: store.load_orders(con, side="SKIP"))
        rf = [str(s["skip_reason"]) for s in sk_f if str(s["code"]) == "301568.SZ"]
        assert any("LIMIT_UP" in r for r in rf), f"老股应写 LIMIT_UP skip，实际 {rf}"

        _reset_account()

    # ------------------------------------------------ T8
    def t8():
        cash_before = _get_cash()
        try:
            _clear_positions()
            # 【2026-11-04 修正】仓位口径已分两套：
            #   · fixed_amount（默认）：每笔固定金额（5 万），与总资产脱钩 → position_pct 不生效
            #   · pct：总资产 × position_pct
            #   本测试要构造「仓位够但钱不够」，必须显式用 pct 模式。
            #   position_pct 由 2.0 调为 10.0：total_asset = 现金500(+持仓市值)，
            #   目标 = 500×10 = 5000 元（够买 400 股），
            #   但买入价含滑点(10.01 元)且现金仅 500 → 连一手都买不起 → 现金受限。
            #   （原 pct=2.0 时目标仅 1000 元，扣滑点后不足一手，会误判成 LOT_TOO_SMALL）
            _set_cash(500.0)
            big_pos = _cfg(enabled=True, initial_capital=1000000.0,
                           position_mode="pct", position_pct=10.0)
            out = on_signals("p3", [_sig("P3-06·现金不足", "600501", 10.00)], cfg=big_pos)
            assert int(out["bought"]) == 0, out
            skips = _r(lambda con: store.load_orders(con, side="SKIP"))
            reasons = [str(s["skip_reason"]) for s in skips if str(s["code"]) == "600501"]
            assert "INSUFFICIENT_CASH" in reasons, reasons

            # 场景 B：现金足够买一手，但目标仓位不足一手 → LOT_TOO_SMALL
            _set_cash(1000000.0)
            small = _cfg(enabled=True, initial_capital=1000000.0,
                         position_mode="pct", position_pct=0.00001)
            out2 = on_signals("p3", [_sig("P3-06b·不足一手", "600502", 10.00)], cfg=small)
            assert int(out2["bought"]) == 0, out2
            skips2 = _r(lambda con: store.load_orders(con, side="SKIP"))
            reasons2 = [str(s["skip_reason"]) for s in skips2 if str(s["code"]) == "600502"]
            assert "LOT_TOO_SMALL" in reasons2, reasons2

            # 场景 C：仓位目标 5000 元 > 现金 2500 元 → 按现金收敛买 200 股（不误判现金不足）
            #   主板整手 100 股：现金 2500 ÷ 10.01 元 = 249 股 → 整手取 200 股。
            _clear_positions()
            _set_cash(2500.0)
            out3 = on_signals("p3", [_sig("P3-06c·现金收敛", "600503", 10.00)], cfg=big_pos)
            assert int(out3["bought"]) == 1, out3
            p503 = _r(lambda con: store.load_position(con, "600503"))
            assert int(p503["shares"]) == 200, p503
            _w(lambda con: store.delete_position(con, "600503"))
        finally:
            _clear_positions()
            _set_cash(cash_before)

    # ------------------------------------------------ T9
    def t9():
        _reset_account()
        small = _cfg(enabled=True, max_positions=2)
        out1 = on_signals(
            "p3",
            [_sig("P3-07", "600601", 10.0), _sig("P3-08", "600602", 10.0)],
            cfg=small,
        )
        assert int(out1["bought"]) == 2, out1
        out2 = on_signals("p3", [_sig("P3-09", "600603", 10.0)], cfg=small)
        assert int(out2["bought"]) == 0, out2
        skips = _r(lambda con: store.load_orders(con, side="SKIP"))
        assert any(
            str(s["skip_reason"]) == "MAX_POSITIONS" and str(s["code"]) == "600603" for s in skips
        ), skips
        _reset_account()

    # ------------------------------------------------ T10
    def t10():
        td = _today()
        # 资金恒等式用「相对基线的增量」口径，避免被前置用例的 _reset_account 复位污染：
        #   cash_now - cash_base == (Σcash_delta_now - Σcash_delta_base)
        orders_base = _r(lambda con: store.load_orders(con, limit=100000))
        cd_base = sum(float(o["cash_delta"]) for o in orders_base if str(o["side"]) in ("BUY", "SELL"))
        cash_base = _get_cash()
        r1 = daily_settle(trade_date=td, cfg=cfg)
        assert r1.get("row") is not None, r1
        n1 = len(_r(lambda con: store.load_equity_series(con, limit=1000)))
        daily_settle(trade_date=td, cfg=cfg)
        n2 = len(_r(lambda con: store.load_equity_series(con, limit=1000)))
        assert n1 == n2, f"重复结算应幂等: {n1} != {n2}"
        orders = _r(lambda con: store.load_orders(con, limit=100000))
        cd = sum(float(o["cash_delta"]) for o in orders if str(o["side"]) in ("BUY", "SELL"))
        a = _r(lambda con: store.load_account(con))
        _assert_close(float(a["cash"]), cash_base + (cd - cd_base), 0.05, "资金恒等式(增量口径)")
        assert int(a["trading_day_index"]) == 1, a

    # ------------------------------------------------ T11
    def t11():
        trades = [
            {"strategy_ids": "P3-01,P3-02", "strategy_names": "A,B", "pool": "p3",
             "pnl": 100.0, "pnl_pct": 0.10, "exit_reason": "TAKE_PROFIT", "holding_days": 3},
            {"strategy_ids": "P3-01", "strategy_names": "A", "pool": "p3",
             "pnl": -80.0, "pnl_pct": -0.08, "exit_reason": "STOP_LOSS", "holding_days": 1},
        ]
        positions = [{"strategy_ids": "P3-02", "pool": "p3"}]
        lb = _build_leaderboard(trades, positions)
        assert len(lb) == 2, lb
        a = [r for r in lb if r["strategy_id"] == "P3-01"][0]
        b = [r for r in lb if r["strategy_id"] == "P3-02"][0]
        assert a["closed_count"] == 2
        # 信号数 = 已平仓笔数 + 当前持仓数
        assert a["signal_count"] == 2, a
        _assert_close(a["win_rate"], 50.0)
        _assert_close(a["total_pnl"], 20.0)
        _assert_close(a["stop_rate"], 50.0)
        _assert_close(a["take_rate"], 50.0)
        _assert_close(a["avg_pnl_pct"], 0.01, 1e-9)
        # P3-02：1 笔平仓 + 1 笔当前持仓 = 2 个信号
        assert b["closed_count"] == 1, b
        assert b["signal_count"] == 2, b
        # 仅持仓无平仓样本时，胜率/盈亏类指标应为 0 而非报错
        _assert_close(b["win_rate"], 100.0)
        _assert_close(b["total_pnl"], 100.0)
        assert a["sample_ok"] is False and a["sample_label"] == "样本不足"

    # ------------------------------------------------ T11b 排行榜边界
    def t11b():
        # 仅当前持仓（未平仓）也应进入排行榜，signal_count=1，统计指标为 0
        only_pos = [{"strategy_ids": "P3-09", "pool": "p4"}]
        lb = _build_leaderboard([], only_pos)
        assert len(lb) == 1, lb
        r = lb[0]
        assert r["strategy_id"] == "P3-09" and r["pool"] == "p4", r
        assert r["signal_count"] == 1 and r["closed_count"] == 0, r
        _assert_close(r["win_rate"], 0.0)
        _assert_close(r["total_pnl"], 0.0)
        _assert_close(r["avg_holding_days"], 0.0)
        assert r["sample_ok"] is False and r["sample_label"] == "样本不足"

        # 空输入不报错
        assert _build_leaderboard([], []) == []

    # ------------------------------------------------ T12
    def t12():
        off = _cfg(enabled=False)
        out = on_signals("p3", [_sig("P3-10", "600701", 10.0)], cfg=off)
        assert int(out["bought"]) == 0 and out.get("reason") == "DISABLED", out
        assert not any(p["code"] == "600701" for p in _r(lambda con: store.load_positions(con)))
        r1 = on_signals("p3", [_sig("P3-10", "600701", 10.0)], cfg=cfg)
        assert int(r1["bought"]) == 1, r1
        r2 = on_signals("p3", [_sig("P3-10", "600701", 10.0)], cfg=cfg)
        assert int(r2["bought"]) == 0, r2
        codes = [p["code"] for p in _r(lambda con: store.load_positions(con))]
        assert codes.count("600701") == 1, codes

    # ------------------------------------------------ T13
    def t13():
        assert board_of("600000") == "main"
        assert board_of("688001") == "star"
        assert board_of("300750") == "gem"
        assert board_of("301001") == "gem"
        assert is_st_stock("ST康美") and is_st_stock("*ST海投")
        assert not is_st_stock("浦发银行")
        from core.paper_trading.engine import _is_limit_down_locked

        assert _is_limit_down_locked(9.00, 10.00, "600000", "浦发银行")     # 主板 -10%
        assert not _is_limit_down_locked(9.05, 10.00, "600000", "浦发银行")
        assert _is_limit_down_locked(8.00, 10.00, "300750", "宁德时代")     # 创业板 -20%
        assert not _is_limit_down_locked(9.00, 10.00, "300750", "宁德时代")
        assert _is_limit_down_locked(7.50, 10.00, "688001", "中芯国际")     # 科创板 -20%
        assert _is_limit_down_locked(9.50, 10.00, "600002", "ST某某")       # ST -5%
        assert not _is_limit_down_locked(9.60, 10.00, "600002", "ST某某")

        # ---- T13b 2026-10-18 A股合规补强（Decimal 0.01 取整 / 板块最小数量 / 新股 / 退市）
        from core.paper_trading.engine import (
            MIN_BUY_SHARES_BY_BOARD,
            _is_limit_up_locked,
            _shrink_to_affordable,
            has_no_limit_window,
            is_excluded_name,
            is_no_limit_stock,
        )
        from core.paper_trading.config import PaperTradingConfig
        from core.paper_trading.fees import apply_slippage, limit_price, round_price

        # 最小价位 0.01 ROUND_HALF_UP
        assert round_price(10.005) == 10.01
        assert round_price(10.004) == 10.00
        assert round_price(9.7158) == 9.72
        # 浮点陷阱：10.45 * 0.9 == 9.4049999... 必须按 9.41 判定
        assert limit_price(10.45, -10.0) == 9.41
        assert limit_price(10.55, -10.0) == 9.50
        assert _is_limit_down_locked(9.41, 10.45, "600000", "浦发银行")
        assert not _is_limit_down_locked(9.42, 10.45, "600000", "浦发银行")
        # ST 主板 -5% = 9.98
        assert limit_price(10.50, -5.0) == 9.98
        assert _is_limit_down_locked(9.98, 10.50, "600002", "ST某某")
        assert not _is_limit_down_locked(9.99, 10.50, "600002", "ST某某")
        # 创业板/科创板 ST 仍按注册制 ±20%（不是 5%）
        assert limit_price(10.50, -20.0) == 8.40
        assert _is_limit_down_locked(8.40, 10.50, "300750", "ST某某")
        assert not _is_limit_down_locked(8.41, 10.50, "300750", "ST某某")
        assert _is_limit_down_locked(29.75, 37.19, "688001", "*ST海投")
        # 北交所 -30%
        assert limit_price(10.00, -30.0) == 7.00
        # 涨停买入封板
        assert _is_limit_up_locked(11.55, 10.50, "600000", "浦发银行")
        assert not _is_limit_up_locked(11.54, 10.50, "600000", "浦发银行")
        assert _is_limit_up_locked(12.60, 10.50, "300750", "宁德时代")
        # 新股无涨跌幅窗口
        # 【V30.5】真实新股代码是纯 6 位数字，N/C/U/D 标记在 *名称* 开头
        assert is_no_limit_stock("301569.SZ", "N联亚药业")
        assert is_no_limit_stock("301660.SZ", "C粤芯半导体")
        assert is_no_limit_stock("688981.SH", "U某某") and is_no_limit_stock("920196.BJ", "D南方乳业")
        assert is_no_limit_stock("N300750") and is_no_limit_stock("C688981")   # 旧数据兼容
        assert not is_no_limit_stock("600000.SH", "贵州茅台")
        assert not _is_limit_up_locked(20.00, 10.50, "301569.SZ", "N联亚药业")
        assert _is_limit_up_locked(20.00, 10.50, "301569.SZ", "联亚药业")    # 对照：老股同价封板
        assert has_no_limit_window("20261016", "20261016", "main")
        assert has_no_limit_window("20261016", "20261019", "main")
        assert has_no_limit_window("20261016", "20261022", "main")   # 第 5 个交易日仍无涨跌幅
        assert not has_no_limit_window("20261016", "20261023", "main")
        assert has_no_limit_window("20261016", "20261016", "bse")
        assert not has_no_limit_window("20261016", "20261019", "bse")
        # 退市整理期 / PT 排除
        assert is_excluded_name("退市海润")
        assert is_excluded_name("PT水务")
        assert not is_excluded_name("贵州茅台")
        # 买入最小数量
        assert MIN_BUY_SHARES_BY_BOARD == {"main": 100, "star": 200, "gem": 200, "bse": 100}
        c10 = PaperTradingConfig()
        assert compute_shares(cash=10**6, total_asset=10**6, price=10.0, cfg=c10, code="600000")[0] % 100 == 0
        assert compute_shares(cash=10**6, total_asset=10**6, price=30.0, cfg=c10, code="300750")[0] >= 200
        assert compute_shares(cash=10**6, total_asset=10**6, price=30.0, cfg=c10, code="688001")[0] >= 200
        assert compute_shares(cash=10**6, total_asset=10**6, price=15.0, cfg=c10, code="920196")[0] >= 100
        assert compute_shares(cash=10**6, total_asset=10**6, price=1100.0, cfg=c10, code="300750")[0] == 0
        # 费用回缩按板块步长
        n_main = _shrink_to_affordable(1000, 10.0, c10, code="600000", cash=3500.0)
        assert n_main > 0 and n_main % 100 == 0, n_main
        n_gem = _shrink_to_affordable(1000, 10.0, c10, code="300750", cash=3500.0)
        assert n_gem >= 200, n_gem
        assert _shrink_to_affordable(210, 10.0, c10, code="300750", cash=100.0) == 0
        # 滑点后按 0.01 取整
        assert apply_slippage(10.005, "BUY", 0.1) == 10.02
        assert apply_slippage(10.005, "SELL", 0.1) == 9.99
        assert apply_slippage(10.004, "BUY", 0) == 10.00

    # ------------------------------------------------ T14 快照可用性
    def t14():
        snap = build_snapshot()
        assert snap.get("available") is True, snap
        for k in ("account", "positions", "trades", "equity", "leaderboard", "fee_summary", "progress"):
            assert k in snap, f"缺字段 {k}"
        assert snap["progress"]["cohort_days"] == 100, snap["progress"]
        assert snap["positions"] or not snap["positions"]
        # 每行必备字段
        for p in snap["positions"]:
            for k in ("code", "buy_price", "price", "shares", "net_cost", "stop_loss_price",
                      "take_profit_price", "dist_to_stop_pct", "dist_to_take_pct", "weight_pct"):
                assert k in p, f"持仓缺字段 {k}"

    for name, fn in [
        ("T1 建表+账户初始化幂等", t1),
        ("T2 买入整手/费用/现金扣减", t2),
        ("T3 重复信号去重", t3),
        ("T4 信号过滤(观察池/C档/无价/非法池)", t4),
        ("T5 阈值边界 -3%/+15%", t5),
        ("T5b 当日跌幅 -3% 无条件离场", t5b),
        ("T6 T+1 当日不卖", t6),
        ("T7 停牌/跌停不卖", t7),
        ("T7b 涨停买入过滤生产链集成", t7b),
        ("T8 现金不足跳过", t8),
        ("T9 最大持仓上限", t9),
        ("T10 日净值幂等+资金恒等式", t10),
        ("T11 战法排行榜聚合", t11),
        ("T11b 排行榜边界(仅持仓/空输入)", t11b),
        ("T12 开关关闭+重放幂等", t12),
        ("T13 板块涨跌停幅度", t13),
        ("T14 UI 快照字段完整", t14),
    ]:
        _run(name, fn)

    print("\n" + "=" * 72)
    npass = sum(1 for _, s, _ in RESULTS if s == "PASS")
    for name, status, detail in RESULTS:
        print(f"  [{status}] {name}  {detail[:130]}")
    print(f"\n合计: {npass} PASS / {len(RESULTS) - npass} FAIL  (共 {len(RESULTS)} 项)")
    print("=" * 72)

    import data.db_core._connection as conn

    try:
        conn.close_db()
    except Exception:  # noqa: BLE001
        pass
    shutil.rmtree(tmp_dir, ignore_errors=True)
    return 0 if npass == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
