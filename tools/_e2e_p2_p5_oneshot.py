# -*- coding: utf-8 -*-
"""tools/_e2e_p2_p5_oneshot.py — P2→P3→P4→P5 全链路演练（一键，2026-11-14）

【是什么】
把守护进程里四个策略池的真实作业函数（_run_scan_push_p2/p3/p4/p5）按序跑一遍：
底仓加载 → 实时行情 → 扫描引擎 → 各战法打分 → signal_filter → 入池门槛 →
落库点 → 推送点 → 模拟盘挂点。整条链路真跑，但**三处副作用全部旁路**：

  · XJ_NO_PUSH=1            → 企微/系统告警零发送（gateway 内部短路，不碰 webhook）
  · XJ_NO_SIGNAL_PERSIST=1  → signal_log / p1_cache / 快照零写入
  · XJ_NO_DEEPSEEK=1        → 不触发计费 AI 调用
  · 双保险：save_signal_log / _push_top_scores / _paper_on_signals_safe
    被本脚本替换为「计数桩」，即使环境变量失效也不会写生产库、不会下单模拟盘

【为什么要停 daemon】
DuckDB 单写者模型：daemon 在跑时演练进程的写连接会抢锁失败。
--manage-daemon（默认）会自动：禁用两个计划任务 → 结束 daemon 进程树 →
等库释放 → 演练 → finally 恢复任务并重拉 daemon（与 controlled_verify 同一套机制）。

【日期口径】
默认锚定最近交易日（从 daily_data 取 MAX(trade_date)）。每个池锚定自己的历史
时间点（P2@09:26 / P3@10:00 / P4@14:36 / P5@20:05），让交易日闸与时段窗通过。
注意：实时行情在休市后返回的是最近交易日收盘快照，P2/P3/P4 的「盘中语义」
（竞价量、分时形态）会退化——本演练验证的是**管线与风控链路**，不是选股质量。

用法：
    python tools/_e2e_p2_p5_oneshot.py             # 一键全链路（自动停/恢复 daemon）
    python tools/_e2e_p2_p5_oneshot.py --dry-run   # 只看计划，不动任何东西
    python tools/_e2e_p2_p5_oneshot.py --no-manage-daemon  # 自己保证 daemon 已停
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors="replace")
    except Exception:
        pass

OUT = open(os.path.join(ROOT, "tools", "_e2e_p2_p5_result.txt"), "w", encoding="utf-8")
FAILS = []

BJ = ZoneInfo("Asia/Shanghai")


def p(*a):
    s = " ".join(str(x) for x in a)
    print(s, flush=True)
    OUT.write(s + "\n")
    OUT.flush()


def check(label, ok, detail=""):
    if not ok:
        FAILS.append(label)
    p(("  [PASS] " if ok else "  [FAIL] ") + label + ("  " + str(detail) if detail != "" else ""))
    return ok


def _last_trade_date() -> str:
    import duckdb

    try:
        con = duckdb.connect(os.path.join(ROOT, "data", "quant_data.duckdb"), read_only=True)
        try:
            r = con.execute("SELECT MAX(trade_date) FROM daily_data").fetchone()[0]
        finally:
            con.close()
        return str(r).replace("-", "")[:8]
    except Exception:
        # 库被 daemon 占用（探测发生在停 daemon 之前）→ 从 P1 池缓存文件名兜底
        import re

        cache_dir = os.path.join(ROOT, "data", "runtime", "pool_cache")
        days = []
        try:
            for fn in os.listdir(cache_dir):
                m = re.match(r"p1_cache_(\d{8})\.json$", fn)
                if m:
                    days.append(m.group(1))
        except Exception:
            pass
        if days:
            return max(days)
        raise


def main() -> int:
    dry = "--dry-run" in sys.argv
    manage = "--no-manage-daemon" not in sys.argv

    target_d8 = ""
    for i, a in enumerate(sys.argv):
        if a == "--date" and i + 1 < len(sys.argv):
            target_d8 = str(sys.argv[i + 1]).replace("-", "").strip()
    if len(target_d8) != 8:
        target_d8 = _last_trade_date()
    # 注意：日分量必须叫 dd —— 下面 `import auto_sniper_daemon as d` 会占用 d
    y, mo, dd = int(target_d8[:4]), int(target_d8[4:6]), int(target_d8[6:8])

    p("=" * 70)
    p("P2 → P3 → P4 → P5 全链路演练 | 目标交易日 %s | dry-run=%s" % (target_d8, dry))
    p("=" * 70)

    import controlled_verify as cv

    if cv._is_trading_window():
        p("[拒绝] 当前处于交易时段，停 daemon 演练会中断生产扫描。请在盘后执行。")
        return 2

    targets = cv._find_target_pids()
    p("计划：")
    p("  1) 停 daemon（进程 %d 个：%s）" % (len(targets), [t[0] for t in targets]))
    p("  2) 演练四池：P2@09:26 → P3@10:00 → P4@14:36 → P5@20:05（锚定 %s）" % target_d8)
    p("  3) 副作用旁路：XJ_NO_PUSH / XJ_NO_SIGNAL_PERSIST / XJ_NO_DEEPSEEK + 函数级计数桩")
    p("  4) finally 恢复计划任务并重拉 daemon")
    if dry:
        p("dry-run：未做任何变更。")
        return 0

    # ---------- 1. 停 daemon ----------
    if manage:
        for tn in (cv.TASK_WATCHDOG, cv.TASK_AUTOSTART):
            r = cv._schtasks(["/Change", "/TN", tn, "/DISABLE"])
            if r.returncode != 0:
                p("[ERR] 禁用计划任务失败: %s" % tn)
                return 2
        for _round in range(3):
            tg = cv._find_target_pids()
            if not tg:
                break
            for pid, cmd in tg:
                import subprocess

                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                               capture_output=True, text=True, encoding="gbk", errors="ignore")
                p("  taskkill pid=%s (%s)" % (pid, cmd[:50]))
            time.sleep(2)
        if not cv._db_free(90):
            p("[ERR] 数据库仍被占用，演练中止（写点已旁路，但仍需读一致性）")
            return 1
        p("daemon 已停，库已空闲")

    since_ts = time.time()
    rc = 1
    try:
        # ---------- 2. 演练环境 ----------
        os.environ["XJ_NO_PUSH"] = "1"
        os.environ["XJ_NO_SIGNAL_PERSIST"] = "1"
        os.environ["XJ_NO_DEEPSEEK"] = "1"

        import auto_sniper_daemon as d

        rec = {
            k: {"selected": 0, "observed": 0, "would_persist": 0,
                "paper_rows": 0, "top": [], "elapsed_s": 0.0, "error": ""}
            for k in ("p2", "p3", "p4", "p5")
        }

        # 函数级计数桩：双保险（不写库、不推送、不下模拟盘单）
        def _push_stub(pool_key, rows, top_n, res=None):
            slot = rec.setdefault(pool_key, {})
            rows_l = rows if isinstance(rows, list) else []
            slot["selected"] = len(rows_l)
            obs = (res or {}).get("observation") or {}
            o = obs.get(pool_key) if isinstance(obs, dict) else None
            slot["observed"] = len(o) if isinstance(o, list) else 0
            top = []
            for r in rows_l:
                if not isinstance(r, dict):
                    continue
                try:
                    sc = float(r.get("综合分", 0.0) or 0.0)
                except (TypeError, ValueError):
                    sc = 0.0
                try:
                    top.append((str(r.get("代码", "")), str(r.get("名称", "")), round(sc, 1)))
                except Exception:
                    continue
            slot["top"] = sorted(top, key=lambda x: x[2], reverse=True)[:5]

        def _paper_stub(pool_key, rows):
            rec.setdefault(pool_key, {})["paper_rows"] = len(rows or [])

        import core.scan.pipeline as pipe

        def _save_stub(*args, **kwargs):
            pool_key = args[1] if len(args) > 1 else kwargs.get("pool_key", "?")
            rec.setdefault(pool_key, {})
            rec[pool_key]["would_persist"] = int(rec[pool_key].get("would_persist") or 0) + 1
            return None

        d._push_top_scores = _push_stub
        d._paper_on_signals_safe = _paper_stub
        pipe.save_signal_log = _save_stub
        p("演练桩已挂载（push/save/paper 全部计数，不产生任何副作用）")

        base = d.load_base_items_latest()
        p("底仓（P1 最新缓存）: %d 只" % len(base or []))
        check("底仓非空", bool(base), "n=%d" % len(base or []))
        if not base:
            return 1

        regime = "_"
        try:
            regime = str(d._resolve_regime_name())
        except Exception as e:
            p("  regime 解析异常（继续）: %s" % e)
        p("市场环境（regime）= %s" % regime)

        def _anchor(hh, mm):
            fixed = datetime(y, mo, dd, hh, mm, 0, tzinfo=BJ)
            d._now_bj = lambda: fixed
            return fixed

        stages = [
            ("p2", "P2 竞价突袭", 9, 26, lambda: d._run_scan_push_p2()),
            ("p3", "P3 盘中巡逻", 10, 0, lambda: d._run_scan_push_p3()),
            ("p4", "P4 尾盘狙击", 14, 36, lambda: d._run_scan_push_p4()),
            ("p5", "P5 盘后战法", 20, 5, lambda: d._run_scan_push_p5()),
        ]
        for key, label, hh, mm, fn in stages:
            fx = _anchor(hh, mm)
            p("\n--- %s | 锚定 %s ---" % (label, fx.strftime("%Y-%m-%d %H:%M")))
            t0 = time.time()
            try:
                ret = fn()
                rec[key]["elapsed_s"] = round(time.time() - t0, 1)
                if isinstance(ret, tuple) and len(ret) == 2:
                    p("  返回: ok=%s n=%s | 用时 %.1fs" % (ret[0], ret[1], rec[key]["elapsed_s"]))
                else:
                    p("  完成 | 用时 %.1fs" % rec[key]["elapsed_s"])
                check("%s 无异常" % label, True)
            except Exception as e:
                rec[key]["error"] = "%s: %s" % (type(e).__name__, str(e)[:300])
                rec[key]["elapsed_s"] = round(time.time() - t0, 1)
                check("%s 无异常" % label, False, rec[key]["error"])

        # ---------- 3. 汇总 ----------
        p("\n" + "=" * 70)
        p("四池演练汇总（%s，全部为『本应产生』计数，均未落库/未推送/未下单）" % target_d8)
        p("=" * 70)
        p("%-4s %-8s %-8s %-10s %-8s %s" % ("池", "正式行", "观察行", "应落库", "模拟盘", "用时(s)"))
        for key, label, _h, _m, _f in stages:
            r = rec[key]
            p("%-4s %-9d %-9d %-11d %-9d %.1f%s"
              % (key.upper(), r.get("selected", 0), r.get("observed", 0),
                 r.get("would_persist", 0), r.get("paper_rows", 0),
                 r.get("elapsed_s", 0.0),
                 ("  ERR: " + r["error"]) if r.get("error") else ""))
        for key, label, _h, _m, _f in stages:
            tops = rec[key].get("top") or []
            if tops:
                p("\n%s 前 5（综合分）:" % key.upper())
                for code, name, sc in tops:
                    p("  %-10s %-10s %6.1f" % (code, name, sc))

        ok_all = not FAILS
        p("\n" + ("✅ 全链路演练完成：四池全部跑通，生产零写入/零推送/零下单"
                  if ok_all else "❌ 存在失败项: %s" % FAILS))
        rc = 0 if ok_all else 1
    except Exception as e:
        p("[FATAL] %s: %s" % (type(e).__name__, str(e)[:500]))
        rc = 1
    finally:
        if manage:
            # 【关键顺序 2026-11-14】先释放本进程的 DuckDB 句柄，再触发 daemon 重启。
            # 否则 bat 前置 verify / 新 daemon 建表都会撞上我们握着读句柄 →
            # verify 失败 → bat 秒退（实测 Last Result=1 的根因）。
            try:
                from data.db_core._connection import close_db

                close_db()
            except Exception:
                pass
            import gc as _gc

            _gc.collect()
            fails = cv._restore_tasks()
            if fails:
                p("[ERR] 计划任务恢复失败: %s" % fails)
                p('  手工: schtasks /Change /TN "\\QuantDaemon_Watchdog" /ENABLE && schtasks /Run /TN "\\QuantDaemon_AutoStart"')
                rc = 1
            else:
                p("计划任务已恢复并已触发 daemon 重启（bat 前置检查约十几秒）")
                p("本进程立即退出以彻底释放库句柄 —— 请随后确认 daemon 心跳。")
            p("退出码=%d | 报告: tools/_e2e_p2_p5_result.txt" % rc)
            OUT.flush()
            try:
                sys.stdout.flush()
            except Exception:
                pass
            # 硬退出：确保所有 DuckDB 文件句柄立刻归还，daemon 才能拿到写锁
            os._exit(rc)
        p("退出码=%d | 报告: tools/_e2e_p2_p5_result.txt" % rc)
    return rc


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        try:
            OUT.close()
        except Exception:
            pass
