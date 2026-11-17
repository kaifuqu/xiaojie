# -*- coding: utf-8 -*-
"""core/_pyarrow_compat.py
【V26.88+ SAC 兜底】Windows 11 Smart App Control 可能拒载 pyarrow._compute.pyd（WinError 4551），
导致 pandas 3.x 内部对字符串列做 str.len() / astype(string) 加速时整进程挂掉。

本模块提供 disable_pyarrow() 在 import pandas 之前调用，把 pyarrow.compute 整个子模块
占位为 dummy，pyarrow 主包 + 其它子模块（fs/parquet/csv）照常加载。

用法（必须先于 import pandas / import streamlit）：

    from core import _pyarrow_compat as _pyarrow_compat  # noqa: E402
    _pyarrow_compat.disable_pyarrow()

- 受环境变量 XIAOJIE_DISABLE_PYARROW 控制（V26.88+10-8 改为默认 ON）:
    默认 1（拦截 — 因为 data/fetcher 包的 import 链会触发 pyarrow._compute.pyd 加载，
                Win11 Smart App Control 拒载 (WinError 4551) 会让 data.fetcher 整个包初始化失败，
                连带 26 个公开 API 全部瘫痪 — 故此默认开)
    显式 0（不拦截 — 仅当用户确认本机 SAC 已禁用、pyarrow._compute.pyd 可正常加载时再设 0）
- 副作用：拦截时 pandas 涉及 pyarrow.compute 的加速路径不可用，会回落到 Python 慢路径；
  DataFrame 业务功能完全不受影响。
- 彻底修复仍需关闭 Win11 Smart App Control（设置 → 应用与功能 → Smart App Control → 关）
"""
import logging
import os
import sys

__all__ = ["disable_pyarrow", "enable_pyarrow", "is_disabled"]

logger = logging.getLogger(__name__)


def is_disabled() -> bool:
    """当前是否已屏蔽 pyarrow（只读）"""
    return bool(getattr(sys, "_xiaojie_pyarrow_disabled", False))


def _user_wants_disabled() -> bool:
    """环境变量三态:
       - XIAOJIE_DISABLE_PYARROW=0 → 显式 opt-out（要真的 pyarrow）
       - 其它值（含未设置）       → 默认 opt-in（要 shim，拦截）
    """
    val = os.environ.get("XIAOJIE_DISABLE_PYARROW", "1")
    return val != "0"


class _DummyModule:
    """占位 pyarrow.compute：任何属性访问/调用/取长度/迭代/包含/下标都返回 dummy 或安全默认。

    设计目的: 让 pandas 3.x 内部的鸭子类型探测 (isinstance/len/iter/contains) 全部走 fallback 路径,
    而不是触发 pyarrow._compute.pyd 真实加载 (Win11 SAC 拒载会直接让进程挂掉).
    """

    def __getattr__(self, name):
        return _DummyModule()

    def __call__(self, *args, **kwargs):
        return _DummyModule()

    def __repr__(self):
        return "<Dummy pyarrow.compute - disabled by xiaojie SAC shim>"

    def __bool__(self):
        return True

    # 兼容鸭子类型检查（pandas/duckdb 探测 pyarrow.compute 实例时偶尔会调）:
    #   - len(obj) 用于判断"是不是 array-like / chunked array"，返回 0 让上游走 fallback
    #   - iter(obj) 同理，返回空迭代器避免任何 for 循环炸掉
    def __len__(self):
        return 0

    def __iter__(self):
        return iter(())

    def __contains__(self, item):
        return False

    def __getitem__(self, key):
        return _DummyModule()


def enable_pyarrow() -> bool:
    """显式启用真实 pyarrow: 仅当用户主动 XIAOJIE_DISABLE_PYARROW=0 时调用本函数。"""
    os.environ["XIAOJIE_DISABLE_PYARROW"] = "0"
    sys._xiaojie_pyarrow_disabled = False  # type: ignore[attr-defined]
    logger.info("[pyarrow-compat] 用户已显式启用真实 pyarrow (XIAOJIE_DISABLE_PYARROW=0)")
    return True


def disable_pyarrow() -> bool:
    """在 import pandas / import streamlit 之前调用。

    实现原理:
      1) 在 sys.modules 提前占位 ``pyarrow.compute`` 为 _DummyModule, 这样后续
         `import pyarrow` / pandas 内部 try-import ``pyarrow.compute`` 不会触盘加载.
      2) 设置环境变量 ``PYARROW_DISABLE_THREADS=1`` (pyarrow 自身也支持此环境变量兜底).
      3) 标记 ``sys._xiaojie_pyarrow_disabled = True`` 便于其它模块检测.

    返回: True=已屏蔽(或用户主动 opt-out 也算 True 表明 shim 系统已就位),
          False=不应该出现, 保留为兼容返回值.
    """
    if not _user_wants_disabled():
        # 用户主动 opt-out: 什么都不做, 让真实 pyarrow 加载
        sys._xiaojie_pyarrow_disabled = False  # type: ignore[attr-defined]
        return True

    if is_disabled():
        # 幂等: 重复调用直接返回
        return True

    # === 真正被禁用的分支: 只把 pyarrow.compute 占位为 dummy, pyarrow 主包保持正常 ===
    # 注意: 不能占位 pyarrow 主包 — pandas.compat.pyarrow 内部会 import pyarrow as pa
    # 然后访问 pa.__version__ / pa.Array / pa.ChunkedArray, 这些属性在我们的 _DummyModule
    # 链下会无限递归产生 _DummyModule, 接着 pa_version_under14p0 = Version(...) 会抛
    # TypeError 而不是 ImportError, pandas 的 except ImportError 抓不住 → 进程挂掉.
    # 真实需求是: 阻止 pyarrow._compute.pyd 被加载, 这只发生在尝试 `import pyarrow.compute` 时.
    # pyarrow 主包本身能正常 import (其它子模块 fs/parquet/csv 都不依赖 _compute.pyd).
    try:
        import pyarrow as _pa  # noqa: F401  # 仅探测主包能否加载, 不占位
    except Exception as _e:
        # 主包都加载不了(极端情况) → 也只能继续, 后面 import 仍会失败但至少 shim 就位
        logger.debug("[pyarrow-compat] pyarrow 主包加载失败(忽略): %s", _e)

    if "pyarrow.compute" not in sys.modules:
        sys.modules["pyarrow.compute"] = _DummyModule()  # type: ignore[assignment]

    os.environ.setdefault("PYARROW_DISABLE_THREADS", "1")
    sys._xiaojie_pyarrow_disabled = True  # type: ignore[attr-defined]

    # [V26.88+10-8 修复] pandas 3.x 默认会启用 Arrow backend (StringArrowArray),
    # 该 backend 内部用 pyarrow._pa_array.take(indices) 等真实 C++ 调用.
    # 我们的 _DummyModule shim 只占位了 pyarrow.compute, 不影响主包 — 但一旦
    # pandas 把字符串列转成 ArrowExtensionArray 后, 任何列操作 (包括最简单的
    # df[mask]) 都会走 _pa_array.take 路径, 触发 ValueError: Unsupported type _DummyModule.
    # 强制让 pandas 用 NumPy backend 即可彻底绕开. mode.string_storage 必须在
    # 第一次 import pandas 之前设置, 但放在 disable_pyarrow() 内仍能影响之后
    # 任何 pd.set_option / pd.options.mode.string_storage 的 get/set 调用.
    # 真正生效的方式: 设置环境变量让 pandas 启动时就读到.
    os.environ.setdefault("PANDAS_STRING_STORAGE", "python")  # pandas >=2.1 支持
    # 兜底: 如果 pandas 已经被 import 了, 立刻改 option
    try:
        import pandas as _pd  # noqa: F401
        _pd.set_option("mode.string_storage", "python")
        _pd.set_option("future.infer_string", False)
    except Exception:
        pass
    return True
