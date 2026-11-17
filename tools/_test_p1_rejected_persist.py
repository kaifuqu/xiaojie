# -*- coding: utf-8 -*-
"""
tools/_test_p1_rejected_persist.py —— P1 淘汰原因落盘 回归

锁定契约：
  1. 主文件是**纯 list**（UI 零改动可读）
  2. 展示键齐全（代码/名称/淘汰死因/被裁阶段/当前得分/死因层级…）
  3. sidecar 元信息包含交易日、入池数、拒绝数、死因分布
  4. 写入是原子的：并发/异常不会留下半截 JSON
  5. 脏数据（None / 字符串 / numpy 标量 / DataFrame 字段）不崩、不丢行
  6. 落盘失败返回 ok=False，不抛异常（绝不阻断 P1 主链）
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors="replace")
    except Exception:
        pass

OUT = io.open(os.path.join(ROOT, "tools", "_p1_rejected_result.txt"), "w", encoding="utf-8")
FAILS = []


def p(*a):
    s = " ".join(str(x) for x in a)
    print(s)
    OUT.write(s + "\n")


def check(label, ok, detail=""):
    if ok:
        p("  [PASS] %s  %s" % (label, detail))
    else:
        FAILS.append(label)
        p("  [FAIL] %s  %s" % (label, detail))
    return ok


def main():
    import core.runtime_data_paths as rdp
    from core.p1_rejected_persist import format_summary_line, persist_p1_rejected

    tmpdir = tempfile.mkdtemp(prefix="p1rej_")
    fake_path = os.path.join(tmpdir, "p1_rejected_cache.json")
    real_fn = rdp.path_p1_rejected_json
    rdp.path_p1_rejected_json = lambda: fake_path  # 隔离：不碰生产缓存

    p("=" * 70)
    p("【P1 淘汰原因落盘 回归】")
    p("=" * 70)

    try:
        rows = [
            {"代码": "300274", "名称": "阳光电源", "淘汰死因": "📉 MA20下方(近5日仅0日站上MA20)",
             "被裁阶段": "前置防线", "当前得分": 46.5, "死因层级": "L2"},
            {"代码": "600519", "名称": "贵州茅台", "淘汰死因": "🧊股性呆滞(近5日均换手0.9%<底线)",
             "被裁阶段": "股性闸", "当前得分": 41.0, "死因层级": "L2"},
            {"代码": "002594", "名称": "比亚迪", "淘汰死因": "🎢一日游嫌疑(冲高回落)",
             "被裁阶段": "防一日游", "当前得分": 39.2, "死因层级": "L1"},
            "脏数据：字符串行",
            None,
        ]

        s1 = persist_p1_rejected(rows, pool_size=30, trade_date="20261105", source="DAEMON_AUTO")
        p("\n=== 1. 主文件（UI 兼容纯 list）===")
        check("1a 落盘成功", bool(s1.get("ok")), "path=%s" % s1.get("path"))

        with open(fake_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        check("1b 顶层是 list", isinstance(data, list), "type=%s" % type(data).__name__)
        check("1c 脏数据未丢行（None 被跳过，字符串保留）",
              len(data) == 4, "len=%d" % len(data))

        first = next((r for r in data if r.get("代码") == "300274"), {})
        required = ["代码", "名称", "淘汰死因", "被裁阶段", "当前得分",
                    "满分项1", "满分项2", "最低项1", "最低项2", "死因层级", "处置建议", "层级徽标"]
        check("1d 展示键齐全", all(k in first for k in required),
              "缺=%s" % [k for k in required if k not in first])

        p("\n=== 2. sidecar 元信息 ===")
        meta_path = fake_path + ".meta.json"
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
        check("2a 交易日/入池数/拒绝数正确",
              meta.get("trade_date") == "20261105" and meta.get("pool_size") == 30
              and meta.get("rejected_count") == 4,
              "date=%s pool=%s rej=%s" % (meta.get("trade_date"), meta.get("pool_size"), meta.get("rejected_count")))
        check("2b 死因分布已聚合", bool(meta.get("death_cause_top")),
              "top=%s" % meta.get("death_cause_top"))
        check("2c 阶段分布已聚合", bool(meta.get("stage_top")),
              "top=%s" % meta.get("stage_top"))
        p("  日志摘要: %s" % format_summary_line(s1))

        p("\n=== 3. numpy / DataFrame 脏字段不崩 ===")
        np_rows = [{
            "代码": "688001", "名称": "华兴源创",
            "淘汰死因": "PE相对超标", "被裁阶段": "估值闸",
            "当前得分": np.float32(37.5),          # numpy 标量
            "debug_df": pd.DataFrame([{"a": 1}]),   # 不可序列化对象
        }]
        s2 = persist_p1_rejected(np_rows, pool_size=0, trade_date="20261105", source="DAEMON_AUTO")
        check("3a 含 numpy/DataFrame 仍落盘成功", bool(s2.get("ok")))
        with open(fake_path, "r", encoding="utf-8") as f:
            d2 = json.load(f)
        check("3b 得分被降级为可读值", isinstance(d2[0].get("当前得分"), (int, float)),
              "val=%r" % (d2[0].get("当前得分"),))

        p("\n=== 4. 空名单 / 异常路径 fail-open ===")
        s3 = persist_p1_rejected([], pool_size=5, trade_date="20261105", source="DAEMON_AUTO")
        check("4a 空名单也落盘（写空 list 便于 UI 显示）", bool(s3.get("ok")) and s3.get("count") == 0)

        rdp.path_p1_rejected_json = lambda: os.path.join("/", "不存在的目录", "x.json") if os.name != "nt" else "Z:\\nope\\x.json"
        s4 = persist_p1_rejected([{"代码": "x"}], pool_size=1)
        check("4b 路径不可写时返回 ok=False 且不抛异常", not s4.get("ok"))
    finally:
        rdp.path_p1_rejected_json = real_fn

    p("\n" + "=" * 70)
    if FAILS:
        p("失败 %d 项：%s" % (len(FAILS), FAILS))
        return 1
    p("✅ P1 淘汰原因落盘 回归全部通过")
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
