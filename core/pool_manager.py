# -*- coding: utf-8 -*-
"""
小杰AI选股系统 Pro V26.88 - P1 底仓管理与打分枢纽

【V26.88 Day 5 拆分版】向后兼容 shim

原 138 KB / 2,788 行单文件已拆分为 core/pool_manager/ 子包：
  - paths / thresholds / blacklists / strategic_mapping
  - single_stock / pool_builder / fast_lane / cache_sovereignty

本文件保留单一历史 import 路径 `from core.pool_manager import ...`：
1. 立即导入 core.pool_manager 子包并把所有符号转发到 globals()，
   既支持 `from core.pool_manager import build_p1_pool_and_cache`，
   也支持 `import core.pool_manager; core.pool_manager.build_p1_pool_and_cache(...)`。
2. 注册到 sys.modules 别名，避免上游引用 `core.pool_manager` 路径被重新解析为 .py。
"""
import logging
import sys

logger = logging.getLogger(__name__)

# ============ Step 1：导入子包 ============
from core.pool_manager import (  # noqa: E402,F401
    paths,
    thresholds,
    blacklists,
    strategic_mapping,
    single_stock,
    pool_builder,
    fast_lane,
    cache_sovereignty,
)

# ============ Step 2：将子包符号注入本模块 globals()（兼容 from import）============
from core.pool_manager import *  # noqa: E402,F401,F403
from core.pool_manager.paths import *  # noqa: E402,F401,F403
from core.pool_manager.thresholds import *  # noqa: E402,F401,F403
from core.pool_manager.blacklists import *  # noqa: E402,F401,F403
from core.pool_manager.strategic_mapping import *  # noqa: E402,F401,F403
from core.pool_manager.single_stock import *  # noqa: E402,F401,F403
from core.pool_manager.pool_builder import *  # noqa: E402,F401,F403
from core.pool_manager.fast_lane import *  # noqa: E402,F401,F403
from core.pool_manager.cache_sovereignty import *  # noqa: E402,F401,F403

# ============ Step 3：登记到 sys.modules，让 `core.pool_manager` 路径别名稳定 ============
_pkg = sys.modules.get("core.pool_manager")
if _pkg is not None:
    # 子包对象已注册；为单文件路径建立别名 import 解析时优先走子包。
    sys.modules.setdefault("core.pool_manager", _pkg)

# ============ Step 4：兼容旧名字（Path 函数旧名）============
# 旧脚本可能 from core.pool_manager import path_p1_candidate_codes（实际不存在，只是兜底）
if not hasattr(__import__(__name__), "path_p1_candidate_codes"):
    path_p1_candidate_codes = path_p1_gene_json  # 兼容 alias

logger.debug("[pool_manager-shim] loaded OK; symbols=%d", len([n for n in dir() if not n.startswith("_")]))


# 【V26.88+10-1】PEP 562 懒模块属性：让单文件 shim 路径下的 ``core.pool_manager.pro``
# 也能走 data.fetcher 懒初始化，避免 import 期 None 快照。
def __getattr__(name):
    if name == "pro":
        from data.fetcher import _client as _fclient
        return _fclient._ensure_pro_initialized()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
