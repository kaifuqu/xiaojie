# -*- coding: utf-8 -*-
"""上线前永久回归套件·第六批（2026-10-18 V30.7 生产扫描未定义名清扫）。

背景：隔离生产参数扫描（P1->P5 真实参数跑全市场）暴露了一类「静态可编译、
运行才炸」的缺陷——模块内引用了不存在的名字，被外层 try/except 吞成 logger.debug，
表现为「结果为空 / 命中为空」而不是报错，造成假成功。已确认 6 处：

  U1  core/strategies/strat_p5_postmarket.py  _safe_float -> _p5_safe_float
      根因：该文件只定义了 _p5_safe_float；涨停龙头/封单分支 NameError，
            被「次日开盘溢价计算异常（已降级）」吞掉 -> 龙头加分整段不生效。
  U2  core/p2_p5_signal_relay.py              _today_bj_yyyymmdd 未定义
      根因：文件从未定义该函数；读文件异常路径 / record_pool_hit 直接 NameError。
  U3  core/p1_balance.py                     typing 缺 Tuple
  U4  core/strategies/score_calibration.py   os 未导入（P1 周线维度）
  U5  core/strategy_weight_optimizer.py      typing 缺 Optional
  U6  data/fund_memory_score.py               typing 缺 Optional

本批断言：
  U1  strat_p5_postmarket 的 HiThink 涨停龙头分支真实执行（用真实日线窗口 +
      打桩风控/筛子，精确命中被 NameError 吞掉的那一段代码）
  U2  p2_p5_signal_relay 全链路（写盘 → 读盘 → 加分 → 拒绝回查 → 注入）
  U3-U6 缺失导入补齐
  U7  P1 周线维度读取不因缺 os 静默退化
  U8  全仓 AST 扫描零真命中（__file__ / logger 属已知误报白名单）
  U9  core/scan/pipeline.py 扫描异常可观测化（_scan_errors / _scan_error_total）

运行：
    _pyenv/venv/Scripts/python.exe tools/_test_release_regressions6.py
"""
from __future__ import annotations

import ast
import builtins
import os
import sys
import tempfile

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core._pyarrow_compat import disable_pyarrow

disable_pyarrow()

PASSED = []
FAILED = []


def check(name, cond, detail=""):
    if cond:
        PASSED.append(name)
        print("  [PASS] %s%s" % (name, ("  -> " + str(detail)) if detail else ""))
    else:
        FAILED.append((name, str(detail)))
        print("  [FAIL] %s  -> %s" % (name, detail))


def section(t):
    print()
    print("=" * 78)
    print(t)
    print("=" * 78)


# __file__/logger/__name__ 为模块级隐式名；path_p1_gene_json 由
# rom core.pool_manager.paths import * 星号导入提供（运行时已验证存在），
# AST 静态分析无法识别星号导入，属于已知误报。
AST_WHITELIST = {"__file__", "logger", "__name__", "__doc__", "path_p1_gene_json"}


def ast_unbound_names(path):
    with open(path, "r", encoding="utf-8", errors="ignore") as fh:
        src = fh.read().lstrip("\ufeff")
    tree = ast.parse(src)
    bound = set(dir(builtins))
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            bound.add(node.id)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                bound.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
    loads = {n.id for n in ast.walk(tree)
             if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    return sorted(loads - bound - AST_WHITELIST)


def _make_p5_window():
    """真实 P5 单票日线窗口：末日放量拉升 + MA 多头 + 主力净流入。"""
    import numpy as np
    import pandas as pd

    rng = np.random.default_rng(20261018)
    n = 120
    base = 20.0 + np.cumsum(rng.normal(0.06, 0.22, n))
    base[-1] = base[-2] * 1.031
    vol = rng.uniform(6e6, 1.6e7, n)
    vol[-1] = vol[-2] * 1.7
    df = pd.DataFrame({
        "ts_code": "600519.SH",
        "trade_date": pd.bdate_range("2026-03-01", periods=n).strftime("%Y%m%d"),
        "open": base * 0.996, "high": base * 1.012, "low": base * 0.991,
        "close": base, "vol": vol, "amount": vol * base,
        "turnover_rate": 6.0, "circ_mv": 1.4e6, "net_main_amount": 1.5e7,
        "winner_rate": 30.0,
    })
    df["pre_close"] = df["close"].shift(1).bfill()
    df["pct_chg"] = (df["close"] / df["pre_close"] - 1.0) * 100.0
    for col, w in (("ma5", 5), ("ma10", 10), ("ma20", 20), ("ma60", 60),
                   ("vol_ma5", 5), ("vol_ma20", 20), ("vol_ma10", 10)):
        src = df["vol"] if col.startswith("vol_ma") else df["close"]
        df[col] = src.rolling(w).mean()
    df["ma20_slope_5"] = (df["ma20"] / df["ma20"].shift(5) - 1.0) * 100.0
    df["bias_20"] = df["close"] / df["ma20"] * 100.0 - 100.0
    df["vol_ratio"] = 1.7
    df["vwap"] = df["close"] * 0.995
    for c in ("inst_net_buy", "cost_50th", "cost_95th", "strth",
              "pe_ttm", "pb", "ps_ttm", "dv_ratio", "limit_times", "atr_pct"):
        df[c] = np.nan
    df["high_20"] = df["high"].rolling(20).max()
    df["low_60"] = df["low"].rolling(60).min()
    df["turnover_rate_f"] = np.nan
    df["macd"] = np.nan
    df["macd_signal"] = np.nan
    return df


def _u1_p5_hithink_branch():
    """用真实日线窗口 + 打桩风控/筛子，精确跑到「次日开盘溢价」分支。"""
    import core.strategies.strat_p5_postmarket as s5

    df = _make_p5_window()
    rt = {
        "ts_code": "600519.SH",
        "trade_date": int(df["trade_date"].iloc[-1]),
        "price": float(df["close"].iloc[-1]),
        "high": float(df["high"].iloc[-1]),
        "low": float(df["low"].iloc[-1]),
        "open": float(df["open"].iloc[-1]),
        "vol": float(df["vol"].iloc[-1]),
        "amount": float(df["amount"].iloc[-1]),
        "circ_mv": 1.4e6,
        "_hithink_limit_up": {"continue_day_cnt": 4, "seal_money": 2.5e8,
                              "limit_up_map": {"600001.SH": 1, "600002.SH": 2}},
        "_hithink_index_strength": 1.6,
        "_market_contraction_score": 0.0,
    }

    saved = {
        "risk": s5.evaluate_3layer_risk,
        "screen": s5.evaluate_p5_single_prepared_row,
        "right": s5.hits_indicate_right_side_attack,
        "inject": s5._inject_financial_quality_into_rt,
    }
    s5.evaluate_3layer_risk = lambda *a, **k: {
        "pass_layer1": True, "pass_layer2": True, "penalty": 0.0,
        "risk_tags": [], "ui_warnings": [], "suggested_min_entry_score": 0.0,
        "veto_reason": "",
    }
    s5.hits_indicate_right_side_attack = lambda hits: False
    s5.evaluate_p5_single_prepared_row = lambda *a, **k: (
        True, ["P5-03·主力回流", "P5-01·趋势加速", "P5-12·箱体回踩"], "", {},
    )
    s5._inject_financial_quality_into_rt = None
    try:
        out = s5.P5Postmarket().run_all(df, rt)
    finally:
        s5.evaluate_3layer_risk = saved["risk"]
        s5.evaluate_p5_single_prepared_row = saved["screen"]
        s5.hits_indicate_right_side_attack = saved["right"]
        s5._inject_financial_quality_into_rt = saved["inject"]

    detail = (out.get("detail") or {}) if isinstance(out, dict) else {}
    tags = (out.get("risk_tags") or []) if isinstance(out, dict) else []
    return out, detail, tags


def main():
    section("U1  strat_p5_postmarket 涨停龙头分支真实执行")
    import core.strategies.strat_p5_postmarket as s5

    p5file = os.path.join(ROOT, "core", "strategies", "strat_p5_postmarket.py")
    with open(p5file, "r", encoding="utf-8") as fh:
        src = fh.read()
    check("U1a 模块内无裸 _safe_float( 调用",
          "_safe_float(" not in src.replace("_p5_safe_float(", ""),
          src.count("_safe_float(") - src.count("_p5_safe_float("))
    check("U1b _p5_safe_float 可用", hasattr(s5, "_p5_safe_float"))

    try:
        out, detail, tags = _u1_p5_hithink_branch()
        check("U1c run_all 返回 dict", isinstance(out, dict), type(out).__name__)
        check("U1c P5 核心筛子被放行", bool(out.get("p5_core_screener_pass")),
              "veto=%s" % out.get("p5_veto_reason"))
        # 4 板 + 2.5 亿封单 => 「次日龙头」标签；大盘极强 1.6 => 「大盘极强」标签；
        # 涨停池仅 2 只 => 「市场冷清」扣分标签。三者同时出现即证明该分支真跑过
        # （未定义 _safe_float 时整段 try 被吞，三个标签一个都不会出现）。
        check("U1c 涨停龙头分支生效（次日龙头标签）",
              any("次日龙头" in str(t) for t in tags), tags[:5])
        check("U1d 涨停池规模分支生效（市场冷清 2 只）",
              any("市场冷清" in str(t) for t in tags), tags[:5])
        check("U1e 大盘极强分支生效（_hithink_index_strength=1.6）",
              any("大盘极强" in str(t) for t in tags), tags[:5])
        check("U1f burst_score 已含次日溢价加成（>55）",
              float(out.get("burst_score", 0.0)) > 55.0, out.get("burst_score"))
        # [BugFix] res["detail"] 曾被整体重写，导致早期诊断字段被静默清空
        check("U1g detail 保留次日开盘溢价", float(detail.get("次日开盘溢价", 0.0)) > 0.0,
              detail.get("次日开盘溢价"))
        check("U1h detail 保留次日开盘标签",
              bool(str(detail.get("次日开盘标签") or "")), detail.get("次日开盘标签"))
        check("U1i detail 保留财务质量字段",
              "fin_quality_score" in detail or "fin_quality_reason" in detail,
              sorted(detail.keys())[:12])
    except Exception as exc:
        check("U1c HiThink 涨停龙头分支生效", False, repr(exc))

    section("U2  p2_p5_signal_relay 北京时间业务日 + 全链路")
    import core.p2_p5_signal_relay as relay

    check("U2a _today_bj_yyyymmdd 已定义", hasattr(relay, "_today_bj_yyyymmdd"))
    d = relay._today_bj_yyyymmdd()
    check("U2b 返回 8 位数字业务日", len(d) == 8 and d.isdigit(), d)
    src_relay = ""
    with open(os.path.join(ROOT, "core", "p2_p5_signal_relay.py"), "r", encoding="utf-8") as fh:
        src_relay = fh.read()
    check("U2b2 正常读取路径已统一北京时间（无 datetime.now() 裸调用）",
          "datetime.now().strftime" not in src_relay,
          "残留=%d" % src_relay.count("datetime.now().strftime"))

    tmpdir = tempfile.mkdtemp(prefix="xj_relay_")
    saved_getter = relay._get_relay_path
    try:
        relay._get_relay_path = lambda: os.path.join(tmpdir, relay._RELAY_FILE)
        relay.record_pool_hit("600000.SH", "p2", 60.0)
        relay.record_pool_hit("600000.SH", "p3", 70.0)
        hist = relay._load_relay_history()
        check("U2c 写盘成功且 date=今日业务日", hist.get("date") == d, hist.get("date"))
        r = relay.calc_relay_bonus("600000.SH", "p3")
        check("U2d 双池接力加分 = 5", float(r.get("relay_bonus", 0.0)) == 5.0,
              "bonus=%s pools=%s" % (r.get("relay_bonus"), r.get("consecutive_pools")))
        relay.record_pool_hit("600001.SH", "p2", 55.0, is_rejected=True, reject_reason="熔断")
        rej = relay.was_rejected_in_pool("600001.SH", "p2")
        check("U2e 拒绝记录可回查",
              rej is not None and "熔断" in str(rej.get("reason", "")), rej)
        res = {"burst_score": 50.0}
        relay.apply_relay_bonus_to_pool_res(res, "600000.SH", "p3")
        check("U2f apply_relay_bonus_to_pool_res 注入成功",
              float(res.get("burst_score", 0.0)) == 55.0
              and float(res.get("detail", {}).get("relay_bonus", 0.0)) == 5.0, res)
        # 跨日切换：历史文件日期不是今天 -> 应整体丢弃，不污染新交易日
        with open(relay._get_relay_path(), "r", encoding="utf-8") as fh:
            stale = json_load(fh)
        stale["date"] = "20260101"
        with open(relay._get_relay_path(), "w", encoding="utf-8") as fh:
            json_dump(stale, fh)
        check("U2g 跨日历史自动失效",
              relay.calc_relay_bonus("600000.SH", "p3").get("relay_bonus") == 0.0,
              relay.calc_relay_bonus("600000.SH", "p3"))
    except Exception as exc:
        check("U2c 接力链路可执行", False, repr(exc))
    finally:
        relay._get_relay_path = saved_getter

    section("U3-U6  缺失导入补齐")
    import core.p1_balance as p1b
    import core.strategies.score_calibration as sc
    import core.strategy_weight_optimizer as swo
    import data.fund_memory_score as fms

    check("U3 core.p1_balance.Tuple", hasattr(p1b, "Tuple"))
    check("U4 score_calibration.os", hasattr(sc, "os"))
    check("U5 strategy_weight_optimizer.Optional", hasattr(swo, "Optional"))
    check("U6 fund_memory_score.Optional", hasattr(fms, "Optional"))

    section("U7  P1 周线维度读取不因缺 os 静默退化")
    try:
        wf = sc._fetch_weekly_row("600519.SH")
        check("U7a _fetch_weekly_row 可执行且不抛 NameError",
              isinstance(wf, dict), "weeks=%d" % (len(wf) if isinstance(wf, dict) else -1))
        check("U7b 生产库存在且只读可开",
              os.path.isfile(os.path.join(ROOT, "data", "quant_data.duckdb")))
    except Exception as exc:
        check("U7a _fetch_weekly_row 可执行", False, repr(exc))

    section("U8  全仓 AST 未绑定名零真命中")
    targets = ["core", "data", "ui", "tools", "service"]
    hits = []
    for t in targets:
        base_dir = os.path.join(ROOT, t)
        if not os.path.isdir(base_dir):
            continue
        for root, dirs, files in os.walk(base_dir):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for f in files:
                if not f.endswith(".py"):
                    continue
                p = os.path.join(root, f)
                rel = os.path.relpath(p, ROOT)
                try:
                    for n in ast_unbound_names(p):
                        hits.append("%s:%s" % (rel, n))
                except SyntaxError as exc:
                    hits.append("%s:SyntaxError %s" % (rel, exc))
    check("U8a core/data/ui/tools/service 无未绑定名", not hits, hits[:20])

    section("U9  scan/pipeline 扫描异常可观测化")
    pipe_path = os.path.join(ROOT, "core", "scan", "pipeline.py")
    with open(pipe_path, "r", encoding="utf-8") as fh:
        pipe_src = fh.read()
    for key in ('res["_scan_errors"]', 'res["_skipped_stocks"]',
                'res["_scan_error_summary"]', 'res["_scan_error_total"]',
                '_record_scan_error(', '_finalize_scan_errors()'):
        check("U9 pipeline 含 %s" % key, key in pipe_src)
    # 单票/单池异常分支必须调用记录器，否则又是「静默 continue」
    check("U9 单票异常已结构化记录",
          'logging.warning("扫描引擎单票处理跳过' in pipe_src
          and '"stock_item"' in pipe_src)
    check("U9 单池异常已结构化记录",
          'logging.warning("战法单池跳过' in pipe_src
          and '"strategy_singleton"' in pipe_src)

    section("汇总")
    print("PASS = %d" % len(PASSED))
    print("FAIL = %d" % len(FAILED))
    for name, detail in FAILED:
        print("  FAILED: %s -> %s" % (name, detail))
    print("ALL PASSED" if not FAILED else "HAS FAILURE")
    return 0 if not FAILED else 1


def json_load(fh):
    import json
    return json.load(fh)


def json_dump(obj, fh):
    import json
    return json.dump(obj, fh, ensure_ascii=False)


if __name__ == "__main__":
    sys.exit(main())
