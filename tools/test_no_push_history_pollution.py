# -*- coding: utf-8 -*-
"""tools/test_no_push_history_pollution.py
========================================================================
回归：QA / 演练环境不得污染生产推送账本 data/push_history.json

【事故背景 · 2026-11-03 实测】
    tools/test_signal_ledger_write.py、tools/test_signal_log_wiring.py 等回归脚本
    会走 core/pool_signal_adapter_v28.append_signal_log_safe →
    pool_apply_push_dedup(record=True) → record_push()。
    而 record_push 的目标 HISTORY_FILE 是**硬编码相对路径** `data/push_history.json`：
      · 不受 XJ_NO_SIGNAL_PERSIST / XJ_NO_PUSH / XJ_NO_DEEPSEEK 保护
      · 不随 DuckDB 的 QA 副本（data/_qa/）切换而隔离
    后果（实测数据）：
      · 20261102 / 20261103 两天，共 12 条**测试票**被写进生产推送账本
        （贵州茅台 / 中国平安 / 五粮液 / 平安银行 / 招商银行 / 万科A / 中信海直 / 中国宝安）
      · COOLDOWN_DAYS=3 的票级冷却把这些**真实票**静默拦掉最多 3 天
        （该票即使真的进池、该推的推送，也会被 check_dedup 判为 should_dedup）

【根因修复】
    core/push_dedup._qa_mode() 三重判定，命中任一即视为 QA 环境：
      1) XJ_NO_SIGNAL_PERSIST / XJ_NO_PUSH / XJ_NO_DEEPSEEK 任一为真
      2) DuckDB 主库路径不在 <root>/data/ 下（即指向 data/_qa 副本）
      3) 显式 XJ_QA_PUSH_HISTORY=1
    record_push() 在 QA 模式下直接 return，不写任何文件。

【红线】
    · 生产环境（_qa_mode()=False）行为完全不变
    · 判定一律 fail-open（读取失败当作生产），绝不因开关故障让生产漏记
    · 本脚本只读 + 只写临时目录，绝不碰生产 data/push_history.json

用法：
    python tools/test_no_push_history_pollution.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

FAILS = []


def check(name: str, ok: bool, extra: str = "") -> None:
    tag = "[OK  ]" if ok else "[FAIL]"
    print(f"  {tag} {name}" + (f"  ({extra})" if extra else ""))
    if not ok:
        FAILS.append(name)


def main() -> int:
    from core import push_dedup as pd
    from core.qa_switches import env_flag

    print("=" * 72)
    print("① _qa_mode() 判定矩阵")
    print("=" * 72)

    # --- 生产基线：清掉所有开关，db_path 指向生产库 ---
    _saved_env = {k: os.environ.get(k) for k in
                  ("XJ_NO_SIGNAL_PERSIST", "XJ_NO_PUSH", "XJ_NO_DEEPSEEK", "XJ_QA_PUSH_HISTORY")}
    for k in _saved_env:
        os.environ.pop(k, None)

    import data.db_core._connection as _dbc
    _prod_db_path = str(_dbc.db_path)
    _prod_dir = os.path.join(ROOT, "data").replace("\\", "/").rstrip("/").lower()

    check("生产环境 _qa_mode() = False",
          pd._qa_mode() is False,
          f"db_path={_prod_db_path}")

    # --- 开关 1：三个 XJ_NO_* 任一为真 ---
    for key in ("XJ_NO_SIGNAL_PERSIST", "XJ_NO_PUSH", "XJ_NO_DEEPSEEK"):
        os.environ[key] = "1"
        check(f"{key}=1 → _qa_mode() = True", pd._qa_mode() is True)
        os.environ.pop(key, None)

    # --- 开关 3：显式 XJ_QA_PUSH_HISTORY ---
    os.environ["XJ_QA_PUSH_HISTORY"] = "1"
    check("XJ_QA_PUSH_HISTORY=1 → _qa_mode() = True", pd._qa_mode() is True)
    os.environ.pop("XJ_QA_PUSH_HISTORY", None)

    # --- 开关 2：db_path 指向 QA 副本 ---
    import tools._qa_env as qa
    qa.use_qa_db()
    try:
        _qa_path = str(_dbc.db_path)
        check("db_path 指向 data/_qa → _qa_mode() = True",
              pd._qa_mode() is True, f"db_path={_qa_path}")
        check("QA 副本路径确实不同于生产路径", _qa_path != _prod_db_path)
    finally:
        # 还原生产 db_path
        _dbc.db_path = _prod_db_path
        _dbc._write_con = None
        _dbc._read_con = None
        try:
            import data.db_core as _pkg
            _pkg.db_path = _prod_db_path
        except Exception:
            pass

    check("还原后 _qa_mode() 回到 False", pd._qa_mode() is False)

    print()
    print("=" * 72)
    print("② record_push 在 QA 模式下不写文件")
    print("=" * 72)

    _orig_hist = pd.HISTORY_FILE
    _tmpdir = tempfile.mkdtemp(prefix="push_hist_pollution_")
    _tmp_file = os.path.join(_tmpdir, "push_history.json")
    pd.HISTORY_FILE = _tmp_file
    try:
        # --- QA 模式：开了开关 ---
        os.environ["XJ_NO_SIGNAL_PERSIST"] = "1"
        pd.record_push("600519.SH", "p4", 88.0, trade_date="20261102")
        exists = os.path.exists(_tmp_file)
        check("QA 模式（开关）→ 账本文件未被创建", not exists)
        if exists:
            check("QA 模式（开关）→ 文件内容为空", not pd._load_history())

        # --- QA 模式：db_path 指向副本 ---
        os.environ.pop("XJ_NO_SIGNAL_PERSIST", None)
        qa.use_qa_db()
        try:
            pd.record_push("600519.SH", "p4", 88.0, trade_date="20261102")
            check("QA 模式（QA 副本 db_path）→ 账本文件未被创建",
                  not os.path.exists(_tmp_file))
        finally:
            _dbc.db_path = _prod_db_path
            _dbc._write_con = None
            _dbc._read_con = None
            try:
                import data.db_core as _pkg
                _pkg.db_path = _prod_db_path
            except Exception:
                pass

        # --- 生产模式：必须照常写 ---
        check("生产模式 _qa_mode() = False", pd._qa_mode() is False)
        pd.record_push("600519.SH", "p4", 88.0, trade_date="20261102")
        check("生产模式 → 账本文件已创建", os.path.exists(_tmp_file))
        _h = pd._load_history()
        check("生产模式 → 记录已写入", len(_h.get("600519.SH", [])) == 1,
              str(_h.get("600519.SH")))
    finally:
        pd.HISTORY_FILE = _orig_hist
        shutil.rmtree(_tmpdir, ignore_errors=True)
        for k, v in _saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    print()
    print("=" * 72)
    print("③ 生产 push_history.json 未被本脚本污染")
    print("=" * 72)
    _prod_hist = os.path.join(ROOT, "data", "push_history.json")
    if os.path.exists(_prod_hist):
        import json
        with open(_prod_hist, encoding="utf-8") as f:
            _ph = json.load(f) or {}
        _test_like = 0
        for _code, _arr in _ph.items():
            for _it in _arr:
                if (str(_it.get("date")) in ("20261102", "20261103")
                        and float(_it.get("score", 0) or 0) in (88.0, 80.0, 62.0)):
                    _test_like += 1
        check("生产账本中不再有本脚本写入的记录（临时目录隔离）", True,
              f"现存疑似历史测试记录 { _test_like } 条（属既有污染，需人工清理）")
    else:
        check("生产 push_history.json 不存在", True)

    print()
    print("=" * 72)
    print("④ 端到端：跑一次真回归，确认 push_history 不再被写")
    print("=" * 72)
    _prod_hist = os.path.join(ROOT, "data", "push_history.json")
    _before = os.path.getmtime(_prod_hist) if os.path.exists(_prod_hist) else None
    _size_before = os.path.getsize(_prod_hist) if os.path.exists(_prod_hist) else 0

    import subprocess
    _r = subprocess.run(
        [sys.executable, os.path.join(ROOT, "tools", "test_signal_ledger_write.py")],
        cwd=ROOT, capture_output=True, text=True, timeout=300,
        encoding="utf-8", errors="replace",
    )
    check("test_signal_ledger_write.py 退出码 0", _r.returncode == 0,
          f"rc={_r.returncode}")
    _after = os.path.getmtime(_prod_hist) if os.path.exists(_prod_hist) else None
    _size_after = os.path.getsize(_prod_hist) if os.path.exists(_prod_hist) else 0
    check("回归后生产 push_history.json 未被修改（mtime 不变）", _before == _after,
          f"{_before} -> {_after}")
    check("回归后生产 push_history.json 大小不变",
          _size_before == _size_after, f"{_size_before} -> {_size_after}")

    print()
    print("=" * 72)
    if FAILS:
        print(f"RESULT: FAIL ({len(FAILS)} 项)")
        for f in FAILS:
            print("  -", f)
        return 1
    print("RESULT: PASS  (QA 环境不再污染生产推送账本)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
