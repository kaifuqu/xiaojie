# -*- coding: utf-8 -*-
"""
tools/_rerun_p1_p5_today.py —— 重跑某个交易日的 P1 重建与 P5 盘后（运维脚本）

原始背景（2026-11-05）：晚间增量同步因 Tushare 分片接口失效而假成功，daily_data 缺 11-05，
19:55 P1 / 20:05 P5 均基于 11-04 陈旧数据产出。数据补齐后需要清洗并重跑。

【2026-11-13 改造】本脚本此前把 ANCHOR_D8 写死成 "20261105"，且会删除
signal_log 里 2026-11-05 的 P5 记录。任何人为了补"今天"的漏跑去复用它，都会把
结果写进 11-05 并删掉 11-05 的信号——静默的数据损坏。现在改为：
  · 目标日期默认 = 北京时间今天，可显式 `--date 20261113` 回补历史某天；
  · 跑之前强制校验 daily_data 该日确有数据，避免"又基于陈旧数据重跑"这个原始事故；
  · 时间锚定到目标日 23:50（避免跨零点后 _now_bj() 把产物写成次日）。

要点：
  1. 清洗前先把目标日 P5 信号备份到 state 目录，再按 (trade_date, pool) 精确删除
  2. P1 走 _rebuild_p1_cache_job（纯重建，不含推送）
  3. P5 走 _run_scan_push_p5（推送侧由 push_dedup 拦截已推项）
  4. 需先停守护进程：本脚本要开 DuckDB 写连接（单写者），daemon 在跑会直接抢锁

用法：
  python tools/_rerun_p1_p5_today.py                 # 重跑今天
  python tools/_rerun_p1_p5_today.py --date 20261113 # 重跑指定交易日
"""
import json
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors="replace")
    except Exception:
        pass

OUT = open(os.path.join(ROOT, "tools", "_rerun_result.txt"), "w", encoding="utf-8")
FAILS = []


def p(*a):
    s = " ".join(str(x) for x in a)
    print(s)
    OUT.write(s + "\n")
    OUT.flush()


def check(label, ok, detail=""):
    if not ok:
        FAILS.append(label)
        p(("  [PASS] " if ok else "  [FAIL] ") + label + "  " + str(detail))
    else:
        p("  [PASS] %s  %s" % (label, detail))
    return ok


_DB = os.path.join(ROOT, "data", "quant_data.duckdb")


def _parse_target():
    """返回 (目标交易日 YYYYMMDD, 是否允许该日无行情)。默认 = 北京时间今天。"""
    import argparse

    today_d8 = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d")
    ap = argparse.ArgumentParser(description="重跑指定交易日的 P1 重建与 P5 盘后")
    ap.add_argument("--date", default=today_d8,
                    help=f"目标交易日 YYYYMMDD（默认北京时间今天={today_d8}）")
    ap.add_argument("--allow-empty-day", action="store_true",
                    help="即使 daily_data 缺该日行情也强行重跑（默认拒绝）")
    args = ap.parse_args()

    d8 = str(args.date).strip().replace("-", "")
    if len(d8) != 8 or not d8.isdigit():
        raise SystemExit(f"[ERR] --date 需为 YYYYMMDD（可带横线），收到 {args.date!r}")
    return d8, bool(args.allow_empty_day)


def _preflight(d8: str, dsh: str, allow_empty: bool) -> None:
    """跑之前的两道闸：库没有被守护进程占住 + 目标日确实有行情数据。

    第 2 道是这次改造的核心：本脚本的原始事故就是「基于陈旧数据重跑」，
    不校验的话换一个日期复用，等于把同一个坑再挖一遍。
    """
    import duckdb

    lock_path = os.path.join(ROOT, "data", "runtime", "state", "daemon.lock")
    try:
        with open(lock_path, "r", encoding="utf-8") as f:
            pid = int(json.load(f).get("pid") or 0)
        alive = False
        try:
            import psutil

            alive = bool(psutil.pid_exists(pid))
        except Exception:
            alive = True  # 判断不了时按「在跑」处理，宁可让人确认
        if alive:
            raise SystemExit(
                f"[ERR] 守护进程正在运行 (pid={pid})，DuckDB 单写者会抢锁。\n"
                f"      请先停止守护进程（关掉它的控制台窗口），再执行本脚本。"
            )
    except FileNotFoundError:
        pass
    except SystemExit:
        raise
    except Exception:
        pass

    con = duckdb.connect(_DB, read_only=True)
    try:
        n = con.execute(
            "SELECT count(*) FROM daily_data WHERE trade_date = ?", [dsh]
        ).fetchone()[0]
    finally:
        con.close()

    if int(n) <= 0:
        msg = (f"[ERR] daily_data 缺少 {dsh} 的行情（0 行）。\n"
               f"      请先补齐该交易日数据（sync_recent_days），否则 P1/P5 仍会基于陈旧数据产出。\n"
               f"      确需强行重跑请加 --allow-empty-day。")
        if not allow_empty:
            raise SystemExit(msg)
        print("[WARN] " + msg.replace("\n", " ").strip())
    else:
        print(f"  预检 OK：daily_data[{dsh}] = {n} 行")


def main() -> int:
    import auto_sniper_daemon as d
    from datetime import datetime as _dt

    target_d8, allow_empty = _parse_target()
    target_dsh = f"{target_d8[:4]}-{target_d8[4:6]}-{target_d8[6:8]}"
    p("目标交易日: %s（%s）" % (target_d8, target_dsh))
    _preflight(target_d8, target_dsh, allow_empty)

    _fixed = _dt(int(target_d8[:4]), int(target_d8[4:6]), int(target_d8[6:8]),
                 23, 50, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    d._now_bj = lambda: _fixed
    p("时间已锚定: %s" % _fixed.isoformat())

    # ---------- 1. 备份 + 清洗当日 P5 信号 ----------
    p("\n=== 1. 备份并清洗 signal_log(%s, pool=p5) ===" % target_dsh)
    import duckdb

    con = duckdb.connect(_DB)
    try:
        rows = con.execute(
            "SELECT * FROM signal_log WHERE trade_date = ? AND pool = 'p5'", [target_dsh]
        ).fetchdf()
        p("  待清洗 P5 记录 = %d 条" % len(rows))
        if len(rows):
            bak = os.path.join(ROOT, "data", "runtime", "state",
                               "backup_signal_log_p5_%s.json" % target_d8)
            with open(bak, "w", encoding="utf-8") as f:
                f.write(rows.to_json(orient="records", force_ascii=False))
            p("  已备份 → %s" % bak)
            con.execute("DELETE FROM signal_log WHERE trade_date = ? AND pool = 'p5'", [target_dsh])
            left = con.execute(
                "SELECT count(*) FROM signal_log WHERE trade_date = ? AND pool = 'p5'", [target_dsh]
            ).fetchone()[0]
            check("1a 旧 P5 信号已清除", int(left) == 0, "剩余=%s" % left)
        else:
            p("  （无历史记录，跳过删除）")
    finally:
        con.close()

    # ---------- 2. 重跑 P1 重建 ----------
    p("\n=== 2. 用 %s 数据重建 P1 ===" % target_d8)
    p1_ok = False
    try:
        p1_ok = bool(d._rebuild_p1_cache_job(require_trading_day_barrier=False))
    except Exception as e:
        p("  P1 重建异常: %s: %s" % (type(e).__name__, str(e)[:400]))
    check("2a P1 重建返回成功", p1_ok)

    json_path = os.path.join(ROOT, "data", "runtime", "cache", "p1_cache_%s.json" % target_d8)
    if not os.path.isfile(json_path):
        # 兜底：按 runtime_data_paths 真实路径找
        from core.runtime_data_paths import path_p1_cache_json

        json_path = path_p1_cache_json(target_d8)
    exists = os.path.isfile(json_path)
    check("2b 当日 P1 文件存在", exists, json_path)

    pool_n = 0
    if exists:
        with open(json_path, "r", encoding="utf-8") as f:
            obj = json.load(f)
        rows2 = obj.get("items") if isinstance(obj, dict) else obj
        rows2 = rows2 if isinstance(rows2, list) else []
        pool_n = len(rows2)
        p("  池大小 = %d 只" % pool_n)
        from core.config_manager import get_p1_max_pool_size

        check("2c 池大小不超过上限", pool_n <= max(1, int(get_p1_max_pool_size())),
              "n=%d cap=%d" % (pool_n, int(get_p1_max_pool_size())))
        # 300274 是否入池（本次排查的起点标的）
        codes = {str((r or {}).get("code") or "").split(".")[0][:6] for r in rows2 if isinstance(r, dict)}
        p("  300274 是否在池内 = %s" % ("是" if "300274" in codes else "否"))

    # 淘汰溯源是否落盘
    try:
        from core.runtime_data_paths import path_p1_rejected_json

        rej_path = path_p1_rejected_json()
        with open(rej_path, "r", encoding="utf-8") as f:
            rej = json.load(f)
        check("2d 淘汰原因已落盘", isinstance(rej, list) and len(rej) > 0, "条数=%d" % len(rej))
        # 300274 的淘汰原因
        for r in rej:
            if str(r.get("代码") or "").startswith("300274"):
                p("  300274 淘汰死因: %s | 阶段: %s | 得分: %s"
                  % (r.get("淘汰死因"), r.get("被裁阶段"), r.get("当前得分")))
                break
    except Exception as e:
        p("  淘汰溯源读取失败: %s" % e)

    # ---------- 3. 重跑 P5 ----------
    p("\n=== 3. 用 %s 数据重跑 P5 盘后 ===" % target_d8)
    try:
        d._write_pipeline_state_patch(last_p5_ok_bj_date="")
        p("  已清空 last_p5_ok_bj_date")
    except Exception as e:
        p("  清状态位失败: %s" % e)

    p5_ok, p5_n = False, 0
    try:
        ret = d._run_scan_push_p5()
        if isinstance(ret, tuple) and len(ret) == 2:
            p5_ok, p5_n = bool(ret[0]), int(ret[1] or 0)
        p("  P5 返回: ok=%s selected=%s" % (p5_ok, p5_n))
    except Exception as e:
        p("  P5 重跑异常: %s: %s" % (type(e).__name__, str(e)[:400]))
    check("3a P5 重跑成功", p5_ok)

    if p5_ok:
        try:
            d._write_pipeline_state_patch(
                last_p5_ok_bj_date=target_d8, last_p5_selected_count=int(p5_n)
            )
            p("  状态位已更新 last_p5_ok_bj_date=%s count=%s" % (target_d8, p5_n))
        except Exception as e:
            p("  更新状态位失败: %s" % e)

    # ---------- 4. 结果核对 ----------
    p("\n=== 4. 结果核对 ===")
    con = duckdb.connect(_DB, read_only=True)
    try:
        n_p5 = con.execute(
            "SELECT count(*) FROM signal_log WHERE trade_date = ? AND pool = 'p5'", [target_dsh]
        ).fetchone()[0]
        p("  signal_log 当日 P5 = %d 条" % n_p5)
        check("4a 新 P5 信号已落库", n_p5 > 0, "条数=%d" % n_p5)
        for r in con.execute(
            "SELECT ts_code, name, score FROM signal_log WHERE trade_date = ? AND pool='p5' ORDER BY score DESC LIMIT 10",
            [target_dsh],
        ).fetchall():
            p("    %s %s %s" % r)
        n_cache = con.execute(
            "SELECT count(*) FROM p1_cache WHERE trade_date = ?", [target_d8]
        ).fetchone()[0]
        p("  p1_cache 表今日 = %d 条" % n_cache)
    finally:
        con.close()

    p("\n" + "=" * 70)
    if FAILS:
        p("失败 %d 项: %s" % (len(FAILS), FAILS))
        return 1
    p("✅ P1 + P5 重跑完成")
    p("=" * 70)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        try:
            OUT.close()
        except Exception:
            pass
