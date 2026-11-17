# -*- coding: utf-8 -*-
"""tools/system_health.py — 全系统体检（只读）· Tier1-P0-4

用途：一条命令回答「今天这台机器能不能信」——
  · 网络健康：代理是否存活、关键域名直连是否可达（P0-2 新增）
  · 数据到哪天、缺哪些天
  · 覆盖率：daily_data 有多少票 vs 在全市多少票
  · 哪些表长期空表（有声明无供给）
  · 交易日历是否过期
  · signal_log 五档是否有样本（P2/P4 是否仍为 0）
  · 模拟盘是否真的成交过
  · daemon 日志最后一次写入是何时

红线：**只读**。不写库、不改任何文件（唯一例外：网络体检状态
`data/runtime/state/net_health.json`）、不触发任何同步。
用法：
    python tools/system_health.py            # 人读
    python tools/system_health.py --json     # 机读
退出码：0=全部 OK/仅 WARN；1=存在 FAIL
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, "data", "quant_data.duckdb")
LOG_PATH = os.path.join(ROOT, "data", "runtime", "sniper.log")

KEY_TABLES = [
    "daily_data", "raw_daily_quotes", "bars_daily", "stock_basic",
    "dim_trade_calendar", "dim_index_valuation", "index_daily",
    "signal_log", "signal_strategy_ledger", "signal_pnl", "strategy_effectiveness",
    "paper_account", "paper_position", "paper_order", "paper_equity_daily",
    "fact_limit_theme", "fact_financial_reports", "sw_sector_daily",
]

STATUS = {"OK": 0, "WARN": 0, "FAIL": 0}


def emit(level, section, msg, bucket):
    STATUS[level] = STATUS.get(level, 0) + 1
    tag = {"OK": "[OK]  ", "WARN": "[WARN]", "FAIL": "[FAIL]"}.get(level, "[????]")
    line = f"{tag} {section} | {msg}"
    bucket.append({"level": level, "section": section, "msg": msg})
    print(line)


# ======================================================================
# Tier 1 · 1.5 数据健康度快照（给 UI 顶部徽章用）
# 与 main() 同口径，但返回 dict、走项目统一连接入口，可在 Streamlit 进程内调用。
# ======================================================================
def data_health_snapshot() -> dict:
    """返回一次性数据健康度快照（只读）。

    status: "OK" / "WARN" / "FAIL"
    """
    out = {
        "status": "FAIL", "reasons": [],
        "db_mb": 0.0, "wal_mb": 0.0, "n_tables": 0,
        "latest_trade_date": "", "lag_days": None, "n_trade_days": 0,
        "covered_n": 0, "in_market_n": 0, "coverage_pct": 0.0,
        "tracked_prev_n": 0, "breadth_pct": 0.0, "mv_gate_yi": 0.0,
        "signal_pool_counts": {}, "calendar_max": "", "calendar_future_days": 0,
        "daemon_last_log": "", "daemon_lag_hours": None,
        "empty_key_tables": [],
    }
    try:
        sys.path.insert(0, ROOT)
        from core._pyarrow_compat import disable_pyarrow

        disable_pyarrow()
    except Exception:
        pass

    if not os.path.exists(DB_PATH):
        out["reasons"].append("找不到数据库")
        return out

    try:
        out["db_mb"] = round(os.path.getsize(DB_PATH) / 1024 / 1024, 1)
        wal = DB_PATH + ".wal"
        if os.path.exists(wal):
            out["wal_mb"] = round(os.path.getsize(wal) / 1024 / 1024, 2)
    except Exception:
        pass

    con = None
    stack = None
    try:
        from contextlib import ExitStack

        stack = ExitStack()
        # 【2026-11-03 修复】体检是纯只读用途：
        #   1) 优先用项目统一只读单例（进程内已存在写连接时也能复用，配置一致，不会冲突）；
        #   2) 单例在「库文件不可写 / 被占 / 无 ACL」时会失败（走的是读写配置），
        #      补一层原生 read_only=True 兜底，避免「读不到」被误报成「数据不可信」。
        try:
            from data.db_core import get_read_conn_singleton

            _s = get_read_conn_singleton(max_wait_sec=3.0)
            if _s is not None:
                con = _s
        except Exception:
            con = None
        if con is None:
            try:
                import duckdb

                con = stack.enter_context(duckdb.connect(DB_PATH, read_only=True))
            except Exception:
                con = None
        if con is None:
            raise RuntimeError("no readable connection")
        try:
            out["n_tables"] = int(con.execute(
                "select count(*) from information_schema.tables").fetchone()[0])

            r = con.execute(
                "select max(trade_date), min(trade_date), count(distinct trade_date) from daily_data"
            ).fetchone()
            latest, earliest, ndays = r[0], r[1], int(r[2] or 0)
            out["latest_trade_date"] = str(latest or "")
            out["n_trade_days"] = ndays
            if latest:
                try:
                    out["lag_days"] = (datetime.now() - datetime.strptime(
                        str(latest), "%Y-%m-%d")).days
                except Exception:
                    out["lag_days"] = None

            # 覆盖率：最新日有行情的票 / 在市票
            # 【2026-11-03 修复】分母口径纠正（原实现把「全市场在市票」当分母，
            # 而日线库按设计只跟踪「总市值或流通市值 >= constants.DATA_FETCH_MIN_TOTAL_MV_WAN(120 亿)」
            # 的票（data/fetcher/_indicators.py:114-120 的市值过滤），
            # 于是覆盖率恒为 ~23%，徽章每天误报「覆盖率仅 23%」。
            # 健康度真正的判据是「最新日相对上一交易日的代码数有没有骤降」，
            # 故新增 breadth_pct = 最新日代码数 / 上一交易日代码数；coverage_pct 仅作信息展示。
            try:
                cov = con.execute(
                    "select count(*) from daily_data where trade_date = "
                    "(select max(trade_date) from daily_data)").fetchone()[0]
                out["covered_n"] = int(cov or 0)
                inm = con.execute(
                    "select count(*) from stock_basic where list_status = 'L'").fetchone()[0]
                out["in_market_n"] = int(inm or 0)
                if out["in_market_n"]:
                    out["coverage_pct"] = round(
                        out["covered_n"] / out["in_market_n"] * 100.0, 1)
                try:
                    _prev = con.execute(
                        "select count(*) from daily_data where trade_date = "
                        "(select max(trade_date) from (select distinct trade_date "
                        " from daily_data) t where trade_date < "
                        "(select max(trade_date) from daily_data))").fetchone()[0]
                    out["tracked_prev_n"] = int(_prev or 0)
                    if out["tracked_prev_n"] > 0:
                        out["breadth_pct"] = round(
                            out["covered_n"] / out["tracked_prev_n"] * 100.0, 1)
                except Exception:
                    pass
                try:
                    import constants as _c
                    out["mv_gate_yi"] = round(
                        float(getattr(_c, "DATA_FETCH_MIN_TOTAL_MV_WAN", 0)) / 10000.0, 0)
                except Exception:
                    pass
            except Exception:
                pass

            try:
                rows = con.execute("select pool, count(*) from signal_log group by 1").fetchall()
                out["signal_pool_counts"] = {str(a): int(b) for a, b in rows}
            except Exception:
                pass

            try:
                cmax = con.execute("select max(trade_date) from dim_trade_calendar").fetchone()[0]
                out["calendar_max"] = str(cmax or "")
                fut = con.execute(
                    "select count(*) from dim_trade_calendar where trade_date > current_date"
                ).fetchone()[0]
                out["calendar_future_days"] = int(fut or 0)
            except Exception:
                pass

            for t in ("paper_position", "paper_order", "signal_pnl", "strategy_effectiveness"):
                try:
                    if t in KEY_TABLES:
                        n = con.execute(f"select count(*) from {t}").fetchone()[0]
                        if int(n or 0) == 0:
                            out["empty_key_tables"].append(t)
                except Exception:
                    pass
        finally:
            if stack is not None:
                try:
                    stack.close()
                except Exception:
                    pass
    except Exception as e:
        out["reasons"].append(f"读库失败: {type(e).__name__}")
        con = None

    # daemon 日志新鲜度
    try:
        if os.path.exists(LOG_PATH):
            mt = datetime.fromtimestamp(os.path.getmtime(LOG_PATH))
            out["daemon_last_log"] = mt.strftime("%Y-%m-%d %H:%M")
            out["daemon_lag_hours"] = round(
                (datetime.now() - mt).total_seconds() / 3600.0, 1)
    except Exception:
        pass

    # ---- 结论 ----
    lag = out["lag_days"]
    if lag is None:
        out["reasons"].append("daily_data 无有效交易日")
    elif lag > 10:
        out["reasons"].append(f"数据落后 {lag} 天")
    elif lag > 3:
        out["reasons"].append(f"数据落后 {lag} 天（需补）")

    # 【2026-11-03 修复】原规则 coverage_pct<60 会在「按设计只跟踪市值>=120亿的票」时天天误报。
    # 改为看广度：最新日代码数相对上一交易日骤降 >15% 才告警。
    if out["breadth_pct"] and out["breadth_pct"] < 85:
        out["reasons"].append(
            f"最新日只取到 {out['covered_n']} 只，较上一交易日 {out['tracked_prev_n']} 只"
            f" 骤降 {100 - out['breadth_pct']:.0f}%（疑似同步中断/半残日）")

    if out["wal_mb"] and out["wal_mb"] > 1.0:
        out["reasons"].append(f"WAL 未合并 {out['wal_mb']}MB")

    if out["daemon_lag_hours"] is not None and out["daemon_lag_hours"] > 24 * 3:
        out["reasons"].append(f"daemon 已 {out['daemon_lag_hours']:.0f} 小时无日志")

    if any("落后" in x or "读库失败" in x or "找不到" in x for x in out["reasons"]):
        out["status"] = "FAIL"
    elif out["reasons"]:
        out["status"] = "WARN"
    else:
        out["status"] = "OK"
    return out


def ledger_consistency_snapshot(con) -> dict:
    """账本一致性巡检（只读）· 2026-11 新增。

    检查 signal_strategy_ledger ↔ signal_log 的对应关系：
      · orphan_n        —— ledger.log_id 指向的 signal_log 行已不存在（历史重写/清理残留）
      · uid_mismatch_n  —— join 上的行里 signal_uid 两边不一致（错配）
      · null_log_id_n   —— ledger 行的 log_id 为空（未关联）
    ok=True 表示无孤儿行、无 uid 错配。任何异常降级为 ok=None + error 文本。
    """
    out = {"ledger_n": 0, "orphan_n": 0, "uid_mismatch_n": 0,
           "null_log_id_n": 0, "ok": True, "error": ""}
    try:
        out["ledger_n"] = int(con.execute(
            "select count(*) from signal_strategy_ledger").fetchone()[0] or 0)
        out["null_log_id_n"] = int(con.execute(
            "select count(*) from signal_strategy_ledger where log_id is null").fetchone()[0] or 0)
        out["orphan_n"] = int(con.execute(
            "select count(*) from signal_strategy_ledger l "
            "where l.log_id is not null and not exists "
            "(select 1 from signal_log s where s.id = l.log_id)").fetchone()[0] or 0)
        out["uid_mismatch_n"] = int(con.execute(
            "select count(*) from signal_strategy_ledger l "
            "join signal_log s on s.id = l.log_id "
            "where l.signal_uid is distinct from s.signal_uid").fetchone()[0] or 0)
        out["ok"] = (out["orphan_n"] == 0 and out["uid_mismatch_n"] == 0)
    except Exception as e:
        out["ok"] = None
        out["error"] = f"{type(e).__name__} {e}"
    return out


def main() -> int:
    rows = []
    if not os.path.exists(DB_PATH):
        emit("FAIL", "数据库", f"找不到 {DB_PATH}", rows)
        return 1

    # pyarrow 兼容必须在任何 pandas 之前（本脚本不用 pandas，但保持一致）
    try:
        sys.path.insert(0, ROOT)
        from core._pyarrow_compat import disable_pyarrow
        disable_pyarrow()
    except Exception:
        pass

    # ---- 0. 网络健康（代理单点）· P0-2 2026-11-14 新增 ----
    # 放在读库之前：网络断/代理死与 DB 状态无关，且它是「数据/推送/告警是否失聪」
    # 的唯一观测点（11-14 实测：代理死亡时企微告警自己也发不出去）。
    try:
        from core.net_health import check_once as _net_check

        _net = _net_check(write_state=True)
        _v = str(_net.get("verdict") or "unknown")
        _proxy = _net.get("proxy") or {}
        _detail = (
            f"verdict={_v} ｜ 代理配置={_proxy.get('registry_server') or '(无)'}"
            f"（存活={_proxy.get('alive')}）"
        )
        _hosts_ok = [str(h.get("host")) for h in (_net.get("hosts") or []) if h.get("direct_ok")]
        _detail += f" ｜ 直连可达={','.join(_hosts_ok) or '无'}"
        _lvl = {"ok": "OK", "direct_only": "WARN", "degraded": "WARN", "down": "FAIL"}.get(_v, "WARN")
        if _v == "direct_only":
            _detail += " → 代理端口已死亡，当前依赖直连兜底，请尽快恢复代理"
        elif _v == "down":
            _detail += " → 代理与直连全断：数据/推送/告警将三停"
        emit(_lvl, "网络健康", _detail, rows)
    except Exception as e:
        emit("WARN", "网络健康", f"检查失败：{type(e).__name__} {e}", rows)

    import duckdb

    size_mb = os.path.getsize(DB_PATH) / 1024 / 1024
    # 【2026-11 修复】daemon 运行时生产库被其独占（DuckDB 单写者模型）——
    # 这属于正常态，不是故障。旧实现直接 connect 会抛 IOException 崩栈；
    # 现在短重试后优雅降级：跳过读库类检查、保留 daemon 日志新鲜度检查。
    con = None
    _last_err = None
    for _attempt in range(3):
        try:
            con = duckdb.connect(DB_PATH, read_only=True)
            break
        except Exception as _e_conn:
            _last_err = _e_conn
            try:
                import time as _time

                _time.sleep(2)
            except Exception:
                pass
    if con is None:
        emit("WARN", "数据库",
             f"被 daemon 独占（{type(_last_err).__name__}），跳过读库类检查 —— 停机后可完整体检", rows)
        if os.path.exists(LOG_PATH):
            mt = datetime.fromtimestamp(os.path.getmtime(LOG_PATH))
            age_h = (datetime.now() - mt).total_seconds() / 3600.0
            lvl = "OK" if age_h <= 2 else ("WARN" if age_h <= 24 else "FAIL")
            emit(lvl, "daemon", f"sniper.log 最后写入 {mt:%Y-%m-%d %H:%M}（{age_h:.1f} 小时前）", rows)
        print("\n—— 汇总 ——")
        print(f"  OK={STATUS['OK']}  WARN={STATUS['WARN']}  FAIL={STATUS['FAIL']}")
        print("  结论：无阻断项（数据库被 daemon 独占属正常态，读库类检查已跳过）。")
        return 0
    try:
        # ---- 1. 库规模 ----
        n_tab = con.execute("select count(*) from information_schema.tables").fetchone()[0]
        wal = DB_PATH + ".wal"
        wal_txt = f"，WAL {os.path.getsize(wal)/1024/1024:.1f}MB" if os.path.exists(wal) else "，无 WAL"
        emit("OK", "数据库", f"{size_mb:.0f}MB，{n_tab} 张表{wal_txt}", rows)

        # ---- 2. 数据新鲜度 ----
        latest_trade, earliest_trade, n_days = con.execute(
            "select max(trade_date), min(trade_date), count(distinct trade_date) from daily_data"
        ).fetchone()
        today = datetime.now().strftime("%Y-%m-%d")
        lag_days = None
        try:
            lag_days = (datetime.strptime(today, "%Y-%m-%d")
                        - datetime.strptime(str(latest_trade), "%Y-%m-%d")).days
        except Exception:
            pass
        if lag_days is None:
            emit("FAIL", "数据新鲜度", "daily_data 无有效交易日", rows)
        elif lag_days <= 3:
            emit("OK", "数据新鲜度", f"最新 {latest_trade}（距今 {lag_days} 天），共 {n_days} 个交易日 {earliest_trade}~{latest_trade}", rows)
        elif lag_days <= 10:
            emit("WARN", "数据新鲜度", f"最新 {latest_trade}，已落后 {lag_days} 天，建议补数据", rows)
        else:
            emit("FAIL", "数据新鲜度", f"最新 {latest_trade}，落后 {lag_days} 天 —— 系统未在跑或同步断了", rows)

        # ---- 3. 覆盖率 ----
        try:
            n_latest = con.execute(
                "select count(distinct ts_code) from daily_data where trade_date=(select max(trade_date) from daily_data)"
            ).fetchone()[0]
            cols = [r[0] for r in con.execute("describe stock_basic").fetchall()]
            if "list_status" in cols:
                n_listed = con.execute(
                    "select count(*) from stock_basic where list_status='L'"
                ).fetchone()[0]
            else:
                n_listed = con.execute("select count(*) from stock_basic").fetchone()[0]
            pct = (n_latest / n_listed * 100.0) if n_listed else 0.0
            # 【口径修复 2026-11-16】日线库按设计只跟踪「总市值或流通市值 >= 120 亿」的票
            # （constants.DATA_FETCH_MIN_TOTAL_MV_WAN，用户硬约束），以全市场在市票为分母时
            # 覆盖率恒约 25% —— 这不是故障。与 data_health_snapshot()（2026-11-03 已修复）
            # 保持一致，改用「广度」口径：最新日相对上一交易日代码数骤降 >15% 才告警；
            # 覆盖率百分比仅作信息展示，不判 FAIL。
            prev_n = con.execute(
                "select count(distinct ts_code) from daily_data where trade_date = "
                "(select max(trade_date) from (select distinct trade_date "
                " from daily_data) t where trade_date < "
                "(select max(trade_date) from daily_data))"
            ).fetchone()[0]
            breadth = (n_latest / prev_n * 100.0) if prev_n else 0.0
            if breadth and breadth < 85:
                emit("FAIL", "覆盖率",
                     f"最新日 {n_latest} 只较上一交易日 {prev_n} 只骤降 "
                     f"{100 - breadth:.0f}%（疑似同步中断/半残日）", rows)
            else:
                emit("OK", "覆盖率",
                     f"最新日 {n_latest} 只 ｜ 上一交易日 {prev_n} 只（广度 {breadth:.0f}%）"
                     f" ｜ 在市 {n_listed} 只——日线库按≥120亿市值口径跟踪，占全市场 {pct:.0f}% 属设计", rows)
        except Exception as e:
            emit("WARN", "覆盖率", f"无法计算：{type(e).__name__} {e}", rows)

        # ---- 4. 关键表行数 ----
        empty = []
        for t in KEY_TABLES:
            try:
                c = con.execute(f"select count(*) from {t}").fetchone()[0]
                if c == 0:
                    empty.append(t)
            except Exception:
                empty.append(t + "(不存在)")
        if empty:
            emit("WARN", "空表", f"{len(empty)} 张关键表为 0 行：{', '.join(empty)}", rows)
        else:
            emit("OK", "空表", "关键表均有数据", rows)

        # ---- 5. 交易日历 ----
        try:
            cal_max, cal_cnt = con.execute(
                "select max(trade_date), count(*) from dim_trade_calendar where is_open=1"
            ).fetchone()
            future = con.execute(
                f"select count(*) from dim_trade_calendar where is_open=1 and trade_date > '{today}'"
            ).fetchone()[0]
            if future == 0:
                emit("WARN", "交易日历", f"最远只到 {cal_max}（开市 {cal_cnt} 天），未来 0 天 —— 日历已过期，需补", rows)
            else:
                emit("OK", "交易日历", f"最远 {cal_max}，未来 {future} 天开市（累计 {cal_cnt} 天）", rows)
        except Exception as e:
            emit("WARN", "交易日历", f"检查失败：{type(e).__name__} {e}", rows)

        # ---- 6. 信号样本分池 ----
        try:
            got = dict(con.execute("select pool, count(*) from signal_log group by 1").fetchall())
            total = sum(got.values())
            miss = [p for p in ("p2", "p3", "p4", "p5") if got.get(p, 0) == 0]
            msg = " ".join(f"{p}={got.get(p, 0)}" for p in ("p2", "p3", "p4", "p5")) + f" 合计={total}"
            if miss:
                emit("FAIL", "信号样本", msg + f" → {'/'.join(miss)} 从未落库，对应战法无法被评估", rows)
            else:
                emit("OK", "信号样本", msg, rows)
        except Exception as e:
            emit("WARN", "信号样本", f"检查失败：{type(e).__name__} {e}", rows)

        # ---- 7. 模拟盘 ----
        try:
            pos = con.execute("select count(*) from paper_position").fetchone()[0]
            od = con.execute("select count(*) from paper_order").fetchone()[0]
            eq = con.execute("select count(*) from paper_equity_daily").fetchone()[0]
            lvl = "OK" if od > 0 else "WARN"
            emit(lvl, "模拟盘", f"持仓={pos} 订单={od} 净值天数={eq}" + ("" if od else " → 引擎完整但从未成交"), rows)
        except Exception as e:
            emit("WARN", "模拟盘", f"检查失败：{type(e).__name__} {e}", rows)

        # ---- 8. daemon 心跳 ----
        if os.path.exists(LOG_PATH):
            mt = datetime.fromtimestamp(os.path.getmtime(LOG_PATH))
            age_h = (datetime.now() - mt).total_seconds() / 3600.0
            lvl = "OK" if age_h <= 2 else ("WARN" if age_h <= 24 else "FAIL")
            emit(lvl, "daemon", f"sniper.log 最后写入 {mt:%Y-%m-%d %H:%M}（{age_h:.1f} 小时前）", rows)
        else:
            emit("WARN", "daemon", "找不到 sniper.log（守护进程可能从未在本机运行）", rows)

        # ---- 9. 账本一致性（signal_strategy_ledger ↔ signal_log）· 2026-11 新增 ----
        try:
            lc = ledger_consistency_snapshot(con)
            if lc.get("error"):
                emit("WARN", "账本一致性", f"检查失败：{lc['error']}", rows)
            else:
                msg = (f"ledger {lc['ledger_n']} 行 ｜ 孤儿行(log_id 不存在)={lc['orphan_n']}"
                       f" ｜ uid 不一致={lc['uid_mismatch_n']} ｜ log_id 为空={lc['null_log_id_n']}")
                if lc["ok"]:
                    emit("OK", "账本一致性", msg, rows)
                else:
                    emit("WARN", "账本一致性",
                         msg + " → 存在账本错配，战法统计可能失真（不主动删除，先人工核对）", rows)
        except Exception as e:
            emit("WARN", "账本一致性", f"检查失败：{type(e).__name__} {e}", rows)
    finally:
        con.close()

    print("\n—— 汇总 ——")
    print(f"  OK={STATUS['OK']}  WARN={STATUS['WARN']}  FAIL={STATUS['FAIL']}")
    if STATUS["FAIL"]:
        print("  结论：存在阻断项，先用上面的 [FAIL] 逐条处理。")
    else:
        print("  结论：无阻断项。")

    if "--json" in sys.argv:
        out = os.path.join(ROOT, "data", "runtime", "system_health_last.json")
        try:
            os.makedirs(os.path.dirname(out), exist_ok=True)
            with open(out, "w", encoding="utf-8") as f:
                json.dump({"ts": datetime.now().isoformat(), "status": STATUS, "items": rows},
                          f, ensure_ascii=False, indent=2)
            print(f"  JSON 已写入 {out}")
        except Exception as e:
            print(f"  JSON 写入失败：{e}")

    return 1 if STATUS["FAIL"] else 0


if __name__ == "__main__":
    sys.exit(main())
