"""复现 pandas 内部 try-import pyarrow.compute 的真实路径"""
import sys
sys.path.insert(0, r"d:\xiaojie")
from core import _pyarrow_compat as p
p.disable_pyarrow()
print("[1] dummy inserted for pyarrow.compute:", "pyarrow.compute" in sys.modules)

# pandas 内部 pandas.compat.pyarrow 的真实 import 方式
print("[2] now import pandas (内部会 try-import pyarrow.compute)")
import pandas as pd
print("[3] pandas OK:", pd.__version__)

# 试 duckdb 的 fetchdf
import duckdb
con = duckdb.connect(":memory:")
con.execute("CREATE TABLE t (s VARCHAR, n INT)")
con.execute("INSERT INTO t VALUES ('hello',1),('world',2),('test',3)")
df = con.execute("SELECT * FROM t").fetchdf()
print("[4] duckdb.fetchdf OK:")
print(df)
print("    dtypes:", dict(df.dtypes))
