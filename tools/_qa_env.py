# -*- coding: utf-8 -*-
"""QA 环境桩：把 DuckDB 指向 data/_qa 的只写副本 + 断网。

用途：生产库 data/quant_data.duckdb 在当前会话是只读的，凡是需要「写库」的
验证（signal_log 落库、模拟盘、daemon 一轮）都在副本上跑，绝不污染生产库。
用法：
    import tools._qa_env as qa; qa.use_qa_db()
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
QA_DB = os.path.join(_ROOT, "data", "_qa", "quant_data.duckdb")


def use_qa_db():
    """把 db_core 的 db_path 切到 QA 副本（读写皆可）。"""
    import data.db_core._connection as _c
    _c.db_path = QA_DB
    _c._write_con = None
    _c._read_con = None
    try:
        import data.db_core as _pkg
        _pkg.db_path = QA_DB
    except Exception:
        pass
    return QA_DB


def block_network():
    """沙箱离线：任何外网调用立即失败，避免卡死。"""
    import socket
    socket.create_connection = lambda *a, **k: (_ for _ in ()).throw(OSError("sandbox offline"))
    _gai = socket.getaddrinfo

    def _g(host, *a, **k):
        if host in ("127.0.0.1", "localhost", None):
            return _gai(host, *a, **k)
        raise OSError("sandbox offline dns")

    socket.getaddrinfo = _g


def boot(offline: bool = True):
    if offline:
        block_network()
    from core._pyarrow_compat import disable_pyarrow
    disable_pyarrow()
    return use_qa_db()
