# -*- coding: utf-8 -*-
"""上线前永久回归套件·第二批（2026-10-18）。

第一批见 tools/_test_release_regressions.py（R01–R13）。
本批覆盖第一批未覆盖的上线前证据：
  R14 非交易日信号业务日错位（补跑信号被打到墙钟日）
  R15 可选能力空表 / 缺失表（增强能力未闭环的显式登记）
  R16 龙虎榜 lhb_top_list 缺表兜底不抛异常
  R17 A 股 ST / 退市名称识别口径
  R18 策略层与模拟盘层涨跌停常量口径一致性
  R19 DuckDB 连接释放 / 单写者 / vacuum 调用契约
  R20 A 股模拟盘 T+1 / 整手 / 费用在真实配置下的自洽
  R21 交易日历离线可用性（体检/复盘不依赖公网）

运行：
    _pyenv\\venv\\Scripts\\python.exe tools\\_test_release_regressions2.py
"""
from __future__ import annotations

import inspect
import os
import sys
import traceback
from typing import Any, Callable, List, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core._pyarrow_compat import disable_pyarrow

disable_pyarrow()

PASSED: List[str] = []
FAILED: List[Tuple[str, str]] = []


def check(name: str, cond: bool, detail: str = "") -> bool:
    if cond:
        PASSED.append(name)
        print(f"  [PASS] {name}" + (f"  ({detail})" if detail else ""))
    else:
        FAILED.append((name, detail))
        print(f"  [FAIL] {name}  {detail}")
    return bool(cond)


def section(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def _con() -> Any:
    from data import db_core as rd

    return rd.get_read_conn_singleton()


# ---------------------------------------------------------------------------
# R14 非交易日信号业务日错位
# ---------------------------------------------------------------------------
def r14() -> None:
    try:
        from core.scan import pipeline as pl

        src = inspect.getsource(pl)
        check(
            "R14 P5/P1 业务日取自日线最后一根 trade_date",
            '_biz_dt = str(_last_row.get("trade_date")' in src,
        )
        check("R14 落库透传 business_date", "business_date=_biz_dt" in src)

        import data.signal_logger as sl

        s2 = inspect.getsource(sl.log_signal)
        check("R14 log_signal 显式接收 trade_date", "trade_date: Optional[str] = None" in s2)
        check(
            "R14 业务日非法时才回落墙钟（正常路径不回落）",
            "if not (len(_biz) == 8 and _biz.isdigit())" in s2,
        )
        check("R14 biz_iso 由业务日而非墙钟生成", 'biz_iso = f"{_biz[:4]}' in s2)
    except Exception as exc:  # noqa: BLE001
        check("R14 业务日口径源码校验", False, f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# R15 可选能力空表 / 缺失表登记
# ---------------------------------------------------------------------------
def r15() -> None:
    try:
        con = _con()
        if not check("R15 主库只读连接可用", con is not None):
            return
        tabs = {
            r[0]
            for r in con.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
            ).fetchall()
        }
        for t in (
            "margin_data",
            "longhubang_data",
            "block_trade_data",
            "unlock_schedule",
            "northbound_holding",
            "northbound_flow",
        ):
            n = con.execute('SELECT count(*) FROM "%s"' % t).fetchone()[0] if t in tabs else -1
            check("R15 %s 存在且为空（增强能力未启用）" % t, n == 0, f"rows={n}")
        for t in (
            "hithink_special_anomaly_analysis",
            "hithink_special_auction_snapshot",
            "hithink_special_dragon_tiger_list",
            "hithink_special_hot_stock_history",
            "hithink_special_limit_break_pool",
            "hithink_special_limit_up_ladder",
        ):
            n = con.execute('SELECT count(*) FROM "%s"' % t).fetchone()[0] if t in tabs else -1
            check("R15 %s 存在且为空（HiThink 特色未启用）" % t, n == 0, f"rows={n}")
        check("R15 lhb_top_list 确实缺失（必须由代码兜底）", "lhb_top_list" not in tabs)
        pt = sorted(t for t in tabs if t.startswith("paper_"))
        check(
            "R15 模拟盘表齐备且已初始化账户（daemon 启动即幂等建表）",
            pt == ["paper_account", "paper_equity_daily", "paper_order", "paper_position"],
            "found=%s" % pt,
        )
        pa = (
            con.execute("SELECT count(*) FROM paper_account WHERE status='RUNNING'").fetchone()[0]
            if "paper_account" in tabs
            else -1
        )
        check("R15 paper_account 已有 RUNNING 账户", int(pa) == 1, f"running={pa}")
        dup = con.execute(
            "SELECT count(*) FROM (SELECT exchange, trade_date FROM dim_trade_calendar"
            " GROUP BY 1,2 HAVING count(*) > 1)"
        ).fetchone()[0]
        check("R15 交易日历 (exchange, trade_date) 无重复", int(dup) == 0, f"dup={dup}")
    except Exception as exc:  # noqa: BLE001
        check("R15 空表/缺表核查", False, f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# R16 龙虎榜缺表兜底
# ---------------------------------------------------------------------------
def r16() -> None:
    try:
        from core.dragon_tiger_seat import compute_dragon_tiger_with_fallback

        bonus, label, detail = compute_dragon_tiger_with_fallback(
            {"ts_code": "600519.SH", "trade_date": "2026-10-16"},
            db_conn=None,
            trade_date="2026-10-16",
        )
        check("R16 无 lhb 数据时返回 0 加成", float(bonus) == 0.0, f"bonus={bonus}")
        check("R16 无 lhb 数据时 label 为空", not label, f"label={label!r}")
        check("R16 返回 detail.reason 可观测", isinstance(detail, dict) and bool(detail.get("reason")))

        con = _con()
        if con is not None:
            b2, _l2, d2 = compute_dragon_tiger_with_fallback(
                {"ts_code": "600519.SH", "trade_date": "2026-10-16"},
                db_conn=con,
                trade_date="2026-10-16",
            )
            check(
                "R16 真实主库（表缺失）也不抛异常",
                float(b2) == 0.0 and isinstance(d2, dict),
                f"bonus={b2} reason={str(d2.get('reason'))[:70]}",
            )
    except Exception as exc:  # noqa: BLE001
        check("R16 龙虎榜兜底", False, f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# R17 A 股 ST / 退市名称识别
# ---------------------------------------------------------------------------
def r17() -> None:
    try:
        from core.paper_trading.engine import is_st_stock

        for name, want in [
            ("ST新河", True),
            ("*ST康乐", True),
            ("ST 生物", True),
            ("XD中国海", False),
            ("浦发银行", False),
            ("C联亚药业", False),
            ("南大光电", False),
        ]:
            got = bool(is_st_stock(name))
            check("R17 is_st_stock(%r)=%s" % (name, want), got == want, f"got={got}")

        import constants as C

        for key, want in [("st", 4.5), ("main", 9.5), ("chinext", 19.5), ("star", 19.5), ("bj", 29.5)]:
            got = float(C.BOARD_LIMIT_UP_PCT[key])
            check("R17 策略层 %s 涨跌幅阈值=%.1f%%" % (key, want), abs(got - want) < 1e-9, f"got={got}")

        # 策略层阈值是「容差判定」，交易所制度值由文档与模拟盘常量表达
        check(
            "R17 策略层阈值严格小于交易所制度（容差判定，非涨跌停制度）",
            all(float(C.BOARD_LIMIT_UP_PCT[k]) < v for k, v in
                [("main", 10.0), ("chinext", 20.0), ("star", 20.0), ("bj", 30.0), ("st", 5.0)]),
        )

        # ST × 板块交叉（2026-10-18 修正：ST 只对主板降档）
        from core.strategies.board_limit_utils import get_limit_up_pct_fast

        for code, name, want in [
            ("600001.SH", "ST主板", 4.5),
            ("000001.SZ", "ST深主", 4.5),
            ("002001.SZ", "*ST深主", 4.5),
            ("300750.SZ", "*ST创板", 19.5),
            ("301269.SZ", "ST创板", 19.5),
            ("688981.SH", "ST科创", 19.5),
            ("830799.BJ", "ST北交", 29.5),
            ("920002.BJ", "*ST北交", 29.5),
            ("300750.SZ", "宁德时代", 19.5),
            ("600000.SH", "浦发银行", 9.5),
            ("000001.SZ", "平安银行", 9.5),
        ]:
            got = float(get_limit_up_pct_fast(code, name))
            check("R17 get_limit_up_pct_fast(%s,%r)=%.1f%%" % (code, name, want), abs(got - want) < 1e-9, f"got={got}")

        for code, name, want in [
            ("600001.SH", "ST主板", 4.5),
            ("000001.SZ", "*ST深主", 4.5),
            ("300750.SZ", "*ST创板", 19.5),
            ("688981.SH", "ST科创", 19.5),
            ("830799.BJ", "ST北交", 29.5),
        ]:
            got = float(C.get_limit_up_pct(code, name))
            check("R17 constants.get_limit_up_pct(%s,%r)=%.1f%%" % (code, name, want), abs(got - want) < 1e-9, f"got={got}")

        from data import data_quality_flags as dqf

        for code, name, want in [
            ("600001.SH", "ST主板", 5.0),
            ("000001.SZ", "*ST深主", 5.0),
            ("300750.SZ", "*ST创板", 20.0),
            ("688981.SH", "ST科创", 20.0),
            ("830799.BJ", "ST北交", 30.0),
            ("920002.BJ", "ST北交", 30.0),
            ("300750.SZ", "宁德时代", 20.0),
            ("600000.SH", "浦发银行", 10.0),
        ]:
            got = float(dqf.limit_pct_for(code, name))
            check("R17 data_quality_flags.limit_pct_for(%s,%r)=%.1f%%" % (code, name, want), abs(got - want) < 1e-9, f"got={got}")
    except Exception as exc:  # noqa: BLE001
        check("R17 ST 识别与阈值", False, f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# R18 策略层 / 模拟盘层 涨跌停口径一致性（含 ST × 板块交叉）
# ---------------------------------------------------------------------------
def r18() -> None:
    try:
        from core.paper_trading import engine as pe

        board = pe.LIMIT_PCT_BY_BOARD
        check("R18 模拟盘主板 10%", board["main"] == 10.0)
        check("R18 模拟盘科创板 20%", board["star"] == 20.0)
        check("R18 模拟盘创业板 20%", board["gem"] == 20.0)
        check("R18 模拟盘北交所 30%", board["bse"] == 30.0)
        stb = pe.ST_LIMIT_PCT_BY_BOARD
        check("R18 模拟盘 ST 主板 5%", stb["main"] == 5.0, f"{stb['main']}")
        check("R18 模拟盘 ST 创业板 20%（不降档）", stb["gem"] == 20.0, f"{stb['gem']}")
        check("R18 模拟盘 ST 科创板 20%（不降档）", stb["star"] == 20.0, f"{stb['star']}")
        check("R18 模拟盘 ST 北交所 30%（不降档）", stb["bse"] == 30.0, f"{stb['bse']}")
        for code, want in [
            ("600000.SH", "main"),
            ("601398.SH", "main"),
            ("000001.SZ", "main"),
            ("002001.SZ", "main"),
            ("688981.SH", "star"),
            ("689009.SH", "star"),
            ("300750.SZ", "gem"),
            ("301269.SZ", "gem"),
            ("830799.BJ", "bse"),
            ("920002.BJ", "bse"),
            ("400001.BJ", "bse"),
        ]:
            got = pe.board_of(code)
            check("R18 board_of(%s)=%s" % (code, want), got == want, f"got={got}")

        # ST × 板块交叉：仅主板 ST 降为 5%，注册制板块 ST 不降档（2026-10-18 修正）
        lock_cases = [
            # (price, pre_close, code, name, want_locked, note)
            (9.50, 10.0, "600001.SH", "ST主板", True, "沪主板 ST 跌停 9.50"),
            (9.51, 10.0, "600001.SH", "ST主板", False, "沪主板 ST 9.51 未跌停"),
            (9.50, 10.0, "000001.SZ", "*ST深主", True, "深主板 ST 跌停 9.50"),
            (8.00, 10.0, "300750.SZ", "*ST创板", True, "创业板 ST 跌停 8.00（非 9.50）"),
            (8.01, 10.0, "300750.SZ", "*ST创板", False, "创业板 ST 8.01 未跌停"),
            (9.00, 10.0, "300750.SZ", "*ST创板", False, "创业板 ST 9.00 不是跌停"),
            (8.00, 10.0, "688981.SH", "ST科创", True, "科创板 ST 跌停 8.00"),
            (8.01, 10.0, "688981.SH", "ST科创", False, "科创板 ST 8.01 未跌停"),
            (7.00, 10.0, "830799.BJ", "ST北交", True, "北交所 ST 跌停 7.00"),
            (7.01, 10.0, "830799.BJ", "ST北交", False, "北交所 ST 7.01 未跌停"),
            (8.00, 10.0, "300750.SZ", "宁德时代", True, "创业板普通 -20%"),
        ]
        for price, pre, code, name, want, note in lock_cases:
            try:
                got = bool(pe._is_limit_down_locked(price, pre, code, name))
                check("R18 %s" % note, got == want, f"got={got}")
            except Exception as exc:  # noqa: BLE001
                check("R18 %s" % note, False, f"{type(exc).__name__}: {exc}")
    except Exception as exc:  # noqa: BLE001
        check("R18 涨跌停口径一致性", False, f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# R19 DuckDB 连接管理
# ---------------------------------------------------------------------------
def r19() -> None:
    try:
        from data import db_core as db

        bad = 0
        for _ in range(30):
            try:
                with db.get_read_conn(read_only=True) as c:
                    if c.execute("SELECT 1").fetchone()[0] != 1:
                        bad += 1
            except Exception:  # noqa: BLE001
                bad += 1
        check("R19 只读连接开关 30 次零失败（无句柄泄漏）", bad == 0, f"fail={bad}")

        with db.get_write_conn() as w:
            check("R19 写连接可用", w.execute("SELECT 1").fetchone()[0] == 1)
            w.execute("CHECKPOINT")
            check("R19 CHECKPOINT 可执行（WAL 可收敛）", True)

        sig = str(inspect.signature(db.duckdb_vacuum_silent))
        check(
            "R19 duckdb_vacuum_silent 接受 log 关键字（daemon 20:30 调用点契约）",
            "log" in sig,
            sig,
        )
    except Exception as exc:  # noqa: BLE001
        check("R19 连接管理", False, f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# R20 模拟盘 T+1 / 整手 / 费用（真实配置口径）
# ---------------------------------------------------------------------------
def r20() -> None:
    try:
        import core.paper_trading.config as pc
        from core.paper_trading import engine as pe
        from core.paper_trading import fees as pf

        cfg = pc.load_config()
        check("R20 模拟盘开关已开启", pc.is_enabled(cfg), f"enabled={pc.is_enabled(cfg)}")
        check("R20 整手 100 股", cfg.lot_size == 100, f"lot={cfg.lot_size}")
        check("R20 T+1 生效", bool(cfg.enforce_t_plus_1))
        # 【2026-11-04 修正】以下断言对齐**当前**配置口径（原值停留在更早期版本）：
        #   · max_positions: 5 → 20（V30.11 起放宽到 20 只）
        #   · position_pct: 仍为 0.20，但默认模式已是 fixed_amount（每笔 5 万），
        #     position_pct 仅在 position_mode="pct" 时才生效
        #   · slippage_pct: 0 → 0.1（V30.x 起加入滑点，回测/实盘统一计入）
        #   · take_profit_pct: 15 → 8（V30.11 起统一为 +8%/-3%，与回测、推送文案同源）
        check("R20 单票仓位比例参数 20%（pct 模式生效）", abs(cfg.position_pct - 0.20) < 1e-9)
        check("R20 默认仓位模式为 fixed_amount（每笔固定金额，与总资产脱钩）",
              str(cfg.position_mode) == "fixed_amount", f"mode={cfg.position_mode}")
        check("R20 每笔固定金额 5 万", abs(float(cfg.fixed_amount_per_position) - 50000.0) < 1e-9)
        check("R20 最大持仓 20 只", cfg.max_positions == 20, f"max={cfg.max_positions}")
        check("R20 最长持有 10 交易日（P0-3 已实装到期强平）", int(cfg.max_hold_days) == 10,
              f"max_hold_days={cfg.max_hold_days}")

        f = pf.compute_fee(
            price=10.0,
            shares=1000,
            side="BUY",
            commission_rate=cfg.commission_rate,
            commission_min=cfg.commission_min,
            stamp_tax_rate=cfg.stamp_tax_rate,
            transfer_fee_rate=cfg.transfer_fee_rate,
        )
        check("R20 买入 gross=10000", abs(f.gross_amount - 10000.0) < 1e-6, f"{f.gross_amount}")
        check("R20 买入佣金取最低 5 元", abs(f.commission - 5.0) < 1e-6, f"{f.commission}")
        check("R20 买入无印花税", f.stamp_tax == 0.0)
        check("R20 买入含双边过户费 0.1", abs(f.transfer_fee - 0.1) < 1e-6, f"{f.transfer_fee}")
        check("R20 买入 cash_delta 负向", f.cash_delta < 0, f"{f.cash_delta}")

        s = pf.compute_fee(
            price=10.0,
            shares=1000,
            side="SELL",
            commission_rate=cfg.commission_rate,
            commission_min=cfg.commission_min,
            stamp_tax_rate=cfg.stamp_tax_rate,
            transfer_fee_rate=cfg.transfer_fee_rate,
        )
        check("R20 卖出印花税 0.05% 单边=5", abs(s.stamp_tax - 5.0) < 1e-6, f"{s.stamp_tax}")
        check(
            "R20 卖出 cash_delta 正向（现金流入 = 成交额 - 佣金 - 印花税 - 过户费）",
            abs(s.cash_delta - 9989.9) < 1e-6,
            f"{s.cash_delta}",
        )
        check(
            "R20 卖出总费用 = 佣金+印花税+过户费",
            abs(s.total_fee - (s.commission + s.stamp_tax + s.transfer_fee)) < 1e-9,
        )

        # 【2026-11-04 修正】滑点 0 → 0.1%（V30.x 起加入滑点，回测与实盘统一计入）
        check("R20 滑点 0.1%", abs(float(cfg.slippage_pct) - 0.1) < 1e-12, f"{cfg.slippage_pct}")
        check(
            "R20 报价偏离熔断 20%",
            abs(float(cfg.quote_stale_deviation_pct) - 20.0) < 1e-9,
        )
        check("R20 止损 -3%", abs(cfg.stop_loss_pct - (-3.0)) < 1e-9)
        # 【2026-11-04 修正】止盈 15% → 8%（V30.11 起统一为 +8%/-3%，与回测、推送文案同源）
        check("R20 止盈 +8%", abs(cfg.take_profit_pct - 8.0) < 1e-9)
        check("R20 跌停封板判定含 0.1% 容差", "1.001" in inspect.getsource(pe._is_limit_down_locked))
    except Exception as exc:  # noqa: BLE001
        check("R20 模拟盘规则", False, f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# R21 交易日历离线可用性
# ---------------------------------------------------------------------------
def r21() -> None:
    try:
        con = _con()
        if not check("R21 主库只读连接可用", con is not None):
            return
        n, o, d, lo, hi = con.execute(
            "SELECT count(*), sum(CASE WHEN is_open THEN 1 ELSE 0 END),"
            " count(DISTINCT trade_date), min(trade_date), max(trade_date)"
            " FROM dim_trade_calendar"
        ).fetchone()
        check("R21 日历有数据且无重复日", int(n) > 0 and int(n) == int(d), f"n={n} distinct={d}")
        check("R21 含休市日（可离线判定休市）", 0 < int(o) < int(n), f"open={o}/{n}")
        check("R21 覆盖到今天", str(hi) >= "2026-10-18", f"{lo}~{hi}")
        fut = con.execute(
            "SELECT count(*) FROM dim_trade_calendar"
            " WHERE replace(trade_date,'-','') > strftime(current_date,'%Y%m%d')"
        ).fetchone()[0]
        check("R21 无未来自然日入库", int(fut) == 0, f"future={fut}")
        empty_next = con.execute(
            "SELECT count(*) FROM dim_trade_calendar WHERE is_open"
            " AND trade_date < (SELECT max(trade_date) FROM dim_trade_calendar WHERE is_open)"
            " AND next_trade_date = ''"
        ).fetchone()[0]
        check("R21 开市日 next_trade_date 齐全", int(empty_next) == 0, f"empty={empty_next}")
        rest_bad = con.execute(
            "SELECT count(*) FROM dim_trade_calendar WHERE (NOT is_open)"
            " AND (pretrade_date <> '' OR next_trade_date <> '')"
        ).fetchone()[0]
        check("R21 休市日前后交易日留空", int(rest_bad) == 0, f"bad={rest_bad}")
        row = con.execute(
            "SELECT is_open, pretrade_date, next_trade_date FROM dim_trade_calendar"
            " WHERE trade_date = '2026-10-16'"
        ).fetchone()
        check(
            "R21 10-16 开市且 next=10-19（跨周末推导正确）",
            bool(row and row[0] and row[1] == "2026-10-15" and row[2] == "2026-10-19"),
            str(row),
        )
    except Exception as exc:  # noqa: BLE001
        check("R21 交易日历离线校验", False, f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# runner
# ---------------------------------------------------------------------------
SUITES: List[Tuple[str, Callable[[], None]]] = [
    ("R14 非交易日业务日口径", r14),
    ("R15 可选能力空表 / 缺失表登记", r15),
    ("R16 龙虎榜缺表兜底", r16),
    ("R17 A 股 ST 识别与涨跌停阈值", r17),
    ("R18 策略层 / 模拟盘层口径一致", r18),
    ("R19 DuckDB 连接管理", r19),
    ("R20 模拟盘 T+1 / 整手 / 费用", r20),
    ("R21 交易日历离线可用性", r21),
]


def main() -> int:
    print("=" * 78)
    print("上线前永久回归套件·第二批 —— xiaojie 2026-10-18")
    print("=" * 78)
    for title, fn in SUITES:
        section(title)
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            check(f"{title}（整体异常）", False, f"{type(exc).__name__}: {exc}")

    section("汇总")
    print(f"  PASS = {len(PASSED)}")
    print(f"  FAIL = {len(FAILED)}")
    for name, detail in FAILED:
        print(f"  [FAIL] {name}  {detail}")
    print()
    print("ALL GREEN" if not FAILED else "HAS FAILURES")
    return 0 if not FAILED else 1


if __name__ == "__main__":
    raise SystemExit(main())
