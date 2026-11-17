# -*- coding: utf-8 -*-
"""上线前全量回归总跑器（2026-10-18）。

顺序执行全部永久回归套件 + 静态编译 + 系统体检 + 数据库体检，
把 stdout/stderr 落到 _qa_logs/release_full_<ts>.log，并输出汇总表。

用法：
    _pyenv/venv/Scripts/python.exe tools/_release_full_run.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(ROOT, "_pyenv", "venv", "Scripts", "python.exe")
LOGDIR = os.path.join(ROOT, "_qa_logs")
os.makedirs(LOGDIR, exist_ok=True)

# [V30.9 2026-10-18] 总跑器默认为「只读演练」：所有子进程零副作用。
# 生产启动脚本一律不设这三个变量，行为完全不变。
for _k in ("XJ_NO_SIGNAL_PERSIST", "XJ_NO_PUSH", "XJ_NO_DEEPSEEK"):
    os.environ.setdefault(_k, "1")

SUITES = [
    ("静态编译全量", "_qa_logs/_compile_all.py"),
    ("UI逐菜单点检", "_qa_logs/_ui_menu_probe.py"),
    ("回归1", "tools/_test_release_regressions.py"),
    ("回归2", "tools/_test_release_regressions2.py"),
    ("回归3", "tools/_test_release_regressions3.py"),
    ("回归4", "tools/_test_release_regressions4.py"),
    ("回归5", "tools/_test_release_regressions5.py"),
    ("回归6 未定义名", "tools/_test_release_regressions6.py"),
    ("数据覆盖", "tools/_test_data_coverage.py"),
    ("模拟盘", "tools/_test_paper_trading.py"),
    ("A股规则", "_qa_logs/_test_ashare_rules.py"),
    ("A股规则扩展", "_qa_logs/_test_ashare_rules2.py"),
    ("守护闸故障注入", "tools/_test_daemon_gate_fault.py"),
    ("P1量能", "tools/test_p1_vol_sun.py"),
    ("数据健康度广度", "tools/test_data_health_breadth.py"),
    ("数据健康度徽章UI", "tools/test_data_health_badge_ui.py"),
    ("冒烟全量", "tools/_smoke_full.py"),
    ("推送去重端到端", "_qa_logs/_e2e_dedup2.py"),
    ("进程互斥端到端", "_qa_logs/_e2e_mutex3.py"),
    ("列表日链路", "tools/_test_list_days_chain.py"),
    ("不落库开关", "tools/_test_no_signal_persist.py"),
    ("同步dry-run", "tools/_test_sw_sync_dryrun.py"),
    ("无外部副作用", "tools/_test_no_side_effects.py"),
    ("索引完整性", "tools/_test_index_integrity.py"),
]


def main() -> int:
    stamp = time.strftime("%Y%m%d_%H%M%S")
    logpath = os.path.join(LOGDIR, "release_full_%s.log" % stamp)
    results = []
    only = sys.argv[1:] or None
    with open(logpath, "w", encoding="utf-8") as log:
        for name, rel in SUITES:
            if only and name not in only:
                continue
            path = os.path.join(ROOT, rel.replace("/", os.sep))
            if not os.path.isfile(path):
                results.append((name, "SKIP", rel, "文件不存在"))
                continue
            log.write("\n" + "=" * 90 + "\n### %s  (%s)\n" % (name, rel) + "=" * 90 + "\n")
            log.flush()
            t0 = time.time()
            proc = subprocess.run([PY, rel], cwd=ROOT, capture_output=True,
                                  text=True, encoding="utf-8", errors="replace")
            dt = time.time() - t0
            log.write(proc.stdout or "")
            log.write("\n--- stderr ---\n")
            log.write(proc.stderr or "")
            log.flush()
            out = proc.stdout or ""
            verdict = "PASS" if proc.returncode == 0 else "FAIL"
            p = f = None
            for line in out.splitlines():
                if line.strip().startswith("PASS ="):
                    p = line.strip()
                if line.strip().startswith("FAIL ="):
                    f = line.strip()
            results.append((name, verdict, rel, "%s %s  %.1fs rc=%s" % (
                p or "-", f or "-", dt, proc.returncode)))
            print("[%s] %-14s %s  %.1fs  %s %s" % (verdict, name, rel, dt, p or "", f or ""))

    print("\n" + "=" * 90)
    print("汇总（详细日志 %s）" % logpath)
    for name, verdict, rel, detail in results:
        print("  %-14s %-6s %s" % (name, verdict, detail))
    bad = [r for r in results if r[1] == "FAIL"]
    print("\n总计 %d 套，失败 %d 套" % (len(results), len(bad)))
    with open(os.path.join(LOGDIR, "release_full_%s.json" % stamp), "w", encoding="utf-8") as fh:
        json.dump([{"suite": n, "verdict": v, "path": p, "detail": d}
                   for n, v, p, d in results], fh, ensure_ascii=False, indent=2)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
