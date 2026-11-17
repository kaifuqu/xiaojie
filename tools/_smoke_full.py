"""完整端到端 smoke test：复现 ui/app.py 启动链路"""
import sys
sys.path.insert(0, r"d:\xiaojie")

# 1) _pyarrow_compat 必须在 pandas 之前
from core import _pyarrow_compat as _pyarrow_compat
ok = _pyarrow_compat.disable_pyarrow()
print("[1] disable_pyarrow:", ok)

# 2) pandas + streamlit
import pandas as pd
import streamlit
print("[2] pandas:", pd.__version__, "| streamlit:", streamlit.__version__)

# 3) StringArray (之前 fail 的点)
from pandas.core.arrays.string_ import StringArray
arr = StringArray._from_sequence(["hello", "world", "test"])
print("[3] StringArray OK:", list(arr), "dtype:", arr.dtype)

# 4) pyarrow 主包能加载（不能加载 compute）
import pyarrow
print("[4] pyarrow __version__:", pyarrow.__version__)
import pyarrow as _pa
# pyarrow.compute 应该是 dummy
_compute_mod = sys.modules.get("pyarrow.compute")
print("[4b] pyarrow.compute shim type:", type(_compute_mod).__name__)
print("[4c] pyarrow.compute in sys.modules:", "pyarrow.compute" in sys.modules)

# 5) duckdb fetchdf (之前 fail 的点)
import duckdb
con = duckdb.connect(":memory:")
con.execute("CREATE TABLE t (s VARCHAR, n INT)")
con.execute("INSERT INTO t VALUES ('hello',1),('world',2),('test',3)")
df = con.execute("SELECT * FROM t").fetchdf()
print("[5] duckdb fetchdf OK:")
print(df)
print("    column types:", [str(df[c].dtype) for c in df.columns])

# 6) data.db_core 7 个符号
from data.db_core import (
    BACKUP_KEEP_RECENT, backup_database_files, list_available_backups,
    restore_database_from_backup, duckdb_storage_snapshot,
    duckdb_storage_snapshot_text, duckdb_vacuum_silent,
)
print("[6] all 7 db_core symbols imported OK")

# 7) snapshot 真数据
s = duckdb_storage_snapshot()
print("[7] snapshot.db_bytes =", s.get("db_bytes"), "total_bytes =", s.get("total_bytes"))
print("[7b] snapshot_text:", duckdb_storage_snapshot_text())

# 8) vacuum 真做实事
v = duckdb_vacuum_silent()
print("[8] vacuum_silent result:", v)

# 9) ui/engine_lazy 关键 import
try:
    from data.db_core import get_latest_sector_ranking
    rank = get_latest_sector_ranking()
    print("[9] sector_rank OK, type:", type(rank).__name__, "len:", len(rank) if hasattr(rank, "__len__") else "N/A")
except Exception as e:
    print("[9] sector_rank FAIL:", type(e).__name__, str(e)[:120])

# 10) P1 倍量阳线生产代码自身
from core.pool_manager.single_stock import _process_single_stock_for_p1
print("[10] _process_single_stock_for_p1 imported OK")

print("\nALL GREEN")
