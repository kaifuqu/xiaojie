# -*- coding: utf-8 -*-
"""tools/_ro_conn.py — 只读连接助手（分析类工具共用）

背景：生产库被 daemon 独占（DuckDB 单写者模型）时，另一个进程的原生
read_only 连接会被文件锁挡住（"另一个程序正在使用此文件"）。
daemon 在每个任务结束时会释放/关闭连接，因此短重试通常即可拿到。

用法：
    from tools._ro_conn import open_ro
    con = open_ro(retries=6, delay=2.0)          # 默认生产库
    con = open_ro(db_path=r"...\\backup.duckdb") # 指定库（分析快照/备份）

红线：永远只读（read_only=True），绝不写库。
"""
from __future__ import annotations

import os
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, "data", "quant_data.duckdb")


def open_ro(db_path: str = "", retries: int = 6, delay: float = 2.0):
    """打开只读 DuckDB 连接。被 daemon 占锁时自动重试。

    失败时抛 RuntimeError，并提示可指定备份库（--db）。
    """
    import duckdb

    path = db_path or DB_PATH
    if not os.path.exists(path):
        raise RuntimeError(f"找不到数据库文件: {path}")
    last_exc = None
    for i in range(max(1, retries)):
        try:
            return duckdb.connect(path, read_only=True)
        except Exception as e:  # 文件锁 / 权限 / 其他
            last_exc = e
            if i < retries - 1:
                time.sleep(delay)
    raise RuntimeError(
        f"无法以只读方式打开 {path}（已重试 {retries} 次）：{last_exc}\n"
        "提示：daemon 正在独占该库时属正常现象；可稍后重跑，"
        "或用 --db 指向 _snapshots/ 下的备份库做离线分析。"
    )
