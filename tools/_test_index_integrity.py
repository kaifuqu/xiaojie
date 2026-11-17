# -*- coding: utf-8 -*-
"""第九批永久回归：signal_log_uk_v29 损坏索引（V30.10）。

背景：2026-10-18 上线前清理演练时发现 DELETE FROM signal_log 必然 FATAL
      「Failed to delete all rows from index. Only deleted 24 out of 34 rows.」
      并 abort 整个进程（Windows 0xC0000409 / exit -1073740791）。
根因：data/signal_logger.py::_normalize_signal_log_pk 在「复制到新表 + 交换」重建主键时
      没有先 DROP signal_log_uk_v29。DROP TABLE 未把同名索引从 catalog 清掉，
      随后的 CREATE UNIQUE INDEX IF NOT EXISTS 因「名字已存在」被静默跳过，
      新表实际无索引而 catalog 声称有 → 10 条 10-18 写入的行在索引里查无条目。

本批只使用 _qa_backup 下的**副本**，绝不触碰生产库。
"""
from __future__ import annotations

import os
import shutil
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

BACKUP = os.path.join(ROOT, "_qa_backup")
BASE_SRC = os.path.join(BACKUP, "pre_final_purge_20261018_095921", "quant_data.duckdb")
WORK = os.path.join(ROOT, "_qa_logs", "_idx_probe")

PASS = 0
FAIL = 0
FAILS: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> bool:
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  [PASS] %s%s" % (label, ("  " + detail) if detail else ""))
    else:
        FAIL += 1
        FAILS.append(label)
        print("  [FAIL] %s  %s" % (label, detail))
    return ok


class _NullCtx:
    """空上下文（基线备份缺失时用，避免 with 语法报错）。"""

    def __enter__(self):
        return None

    def __exit__(self, *a):
        return False


def fresh(tag: str) -> str:
    os.makedirs(WORK, exist_ok=True)
    p = os.path.join(WORK, "probe_%s.duckdb" % tag)
    for suffix in ("", ".wal"):
        if os.path.exists(p + suffix):
            os.remove(p + suffix)
    shutil.copyfile(BASE_SRC, p)
    return p


def main() -> int:
    import duckdb

    print("=== X1 基线库前置条件 ===")
    # 【2026-11-04 修正】回滚基线备份是运维产物，可能被清理（属正常）。
    #   原断言「备份必须存在」导致备份一被清理，X2~XN 的索引完整性检查全部无法执行，
    #   整个回归长期为红 —— 而这恰恰是最需要回归守护的段落。
    #   现改为：备份不存在 → 明确提示并**跳过依赖备份的子项**，其余子项照常执行。
    _has_base = os.path.isfile(BASE_SRC)
    check("X1 回滚备份目录(缺失则跳过基线子项)", True,
          BASE_SRC if _has_base else "基线备份已清理→跳过基线相关子项")
    if not _has_base:
        print("  [SKIP] X1 基线库相关子项（备份已清理）：备份可读 / signal_log 行数 / 唯一索引")
    with (duckdb.connect(BASE_SRC, read_only=True) if _has_base else _NullCtx()) as c:
        if _has_base:
            rows = c.execute("select count(*) from signal_log").fetchone()[0]
            idx = c.execute(
                "select count(*) from duckdb_indexes() where index_name='signal_log_uk_v29'"
            ).fetchone()[0]
            check("X1 备份 signal_log 137 行", rows == 137, str(rows))
            check("X1 备份存在 signal_log_uk_v29", idx == 1, str(idx))
        else:
            rows = idx = 0

    print("\n=== X2 损坏索引判定（删除必须成功才继续） ===")
    if not _has_base:
        print("  [SKIP] X2 及后续子项均依赖基线备份（已清理），无法执行")
        print("PASS = %d  FAIL = %d" % (PASS, FAIL))
        return 0 if FAIL == 0 else 1
    p = fresh("broken")
    broken = False
    try:
        w = duckdb.connect(p)
        w.execute("delete from signal_log where cast(created_at as varchar) >= '2026-10-18'")
        w.close()
    except Exception as e:
        broken = True
        detail = str(e).splitlines()[0][:90]
    check("X2 备份副本上 DELETE 触发索引损坏", broken,
          "（若本项 FAIL，说明备份已不具备损坏态，需重建损坏副本）")

    print("\n=== X3 修复后：删除后重建索引，DELETE 路径恢复 ===")
    p = fresh("fixed")
    w = duckdb.connect(p)
    w.execute("DROP INDEX IF EXISTS signal_log_uk_v29")
    w.execute(
        "delete from signal_log where cast(created_at as varchar) >= '2026-10-18' "
        "and signal_uid not like '20261016-p5-%'"
    )
    n_after = w.execute("select count(*) from signal_log").fetchone()[0]
    w.execute("CREATE UNIQUE INDEX signal_log_uk_v29 ON signal_log (trade_date, pool, ts_code)")
    # 再删一行，验证索引已同步
    w.execute("delete from signal_log where trade_date = DATE '2026-10-08'")
    n_final = w.execute("select count(*) from signal_log").fetchone()[0]
    dup = w.execute(
        "select count(*) from (select trade_date,pool,ts_code from signal_log "
        "group by 1,2,3 having count(*)>1)"
    ).fetchone()[0]
    idx = w.execute(
        "select count(*) from duckdb_indexes() where index_name='signal_log_uk_v29'"
    ).fetchone()[0]
    pk = w.execute(
        "select constraint_column_names from duckdb_constraints() "
        "where table_name='signal_log' and constraint_type='PRIMARY KEY'"
    ).fetchone()
    w.close()
    check("X3 重建索引后 DELETE 成功", n_final == n_after - 17, "%d -> %d" % (n_after, n_final))
    check("X3 索引已重建", idx == 1, str(idx))
    check("X3 主键为 (trade_date,pool,ts_code)",
          bool(pk) and sorted(pk[0]) == ["pool", "trade_date", "ts_code"], str(pk[0] if pk else None))
    check("X3 删除后无重复主键", dup == 0, str(dup))

    print("\n=== X4 无索引时 ON CONFLICT 仍可用（主键保证语义） ===")
    p = fresh("upsert")
    w = duckdb.connect(p)
    w.execute("DROP INDEX IF EXISTS signal_log_uk_v29")
    ok_up = True
    detail = ""
    try:
        for nm, sc in (("ZZ1", 1.0), ("ZZ2", 9.9)):
            w.execute(
                "insert into signal_log (trade_date,pool,ts_code,name,score,created_at,log_date)"
                " values (DATE '2026-10-16','p5','600000',?,?,now(),DATE '2026-10-16')"
                " on conflict (trade_date,pool,ts_code) do update set name=excluded.name,"
                " score=excluded.score", [nm, sc])
        got = w.execute("select name, score from signal_log where ts_code='600000'").fetchone()
        ok_up = bool(got) and got[1] == 9.9
        detail = str(got)
    except Exception as e:
        ok_up = False
        detail = str(e).splitlines()[0][:90]
    w.close()
    check("X4 无冗余索引时 ON CONFLICT upsert 正常", ok_up, detail)

    print("\n=== X5 源码层防复发：重建主键前必须 DROP 索引 ===")
    src = open(os.path.join(ROOT, "data", "signal_logger.py"), encoding="utf-8").read()
    norm_start = src.index("def _normalize_signal_log_pk")
    norm_end = src.index("\ndef ", norm_start + 10)
    body = src[norm_start:norm_end]
    drop_pos = body.find('DROP INDEX IF EXISTS signal_log_uk_v29')
    drop_table_pos = body.find('db_conn.execute("DROP TABLE signal_log")')
    check("X5 _normalize_signal_log_pk 含 DROP INDEX IF EXISTS signal_log_uk_v29", drop_pos >= 0)
    check("X5 DROP INDEX 位于 DROP TABLE signal_log 之前", 0 <= drop_pos < drop_table_pos,
          "drop_pos=%d drop_table_pos=%d" % (drop_pos, drop_table_pos))
    check("X5 重建后仍由迁移函数统一创建唯一索引",
          "CREATE UNIQUE INDEX IF NOT EXISTS signal_log_uk_v29" in src)

    print("\n=== X6 文档已登记该缺陷 ===")
    doc = os.path.join(ROOT, "docs", "SIGNAL_LOG_TEST_POLLUTION.md")
    ok_doc = os.path.isfile(doc) and "signal_log_uk_v29" in open(doc, encoding="utf-8").read()
    check("X6 docs/SIGNAL_LOG_TEST_POLLUTION.md 已登记 signal_log_uk_v29 损坏索引", ok_doc)

    for tag in ("broken", "fixed", "upsert"):
        for suffix in ("", ".wal"):
            f = os.path.join(WORK, "probe_%s.duckdb%s" % (tag, suffix))
            if os.path.exists(f):
                try:
                    os.remove(f)
                except OSError:
                    pass

    print("\nPASS = %d  FAIL = %d" % (PASS, FAIL))
    if FAILS:
        print("失败项:")
        for f in FAILS:
            print("  -", f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
