# -*- coding: utf-8 -*-
"""
core/pool_manager/blacklists.py
原 core/pool_manager.py L295-L418（Tushare 初始化 + 解禁黑名单 + 惩罚黑名单）

【职责】
- _init_pro()：薄壳委托 ``data.fetcher._client._ensure_pro_initialized()``。
- pro：**PEP 562 lazy module attr**，访问时才解析为 ``DataApi``（避免 import 时 None 快照）。
- _get_unlock_blacklist(anchor_yyyymmdd)：30 天解禁黑名单（默认关闭，受 config 控制）
- _get_punish_blacklist(max_age_days, anchor_yyyymmdd)：从 blacklist.json 读取近 N 日的 ts_code
  （用于防 P1 在报复性交易期被动纳入）

【依赖】
- os / json / logging / yaml
- core.pool_manager.paths（path_blacklist_json）
- core.config_manager（get_p1_use_tushare_unlock_blacklist）
- data.fetcher._client（懒初始化单例）
"""
import json
import logging
import os
from datetime import datetime, timedelta, timezone

import yaml

from core.pool_manager.paths import PROJECT_ROOT, path_blacklist_json

logger = logging.getLogger(__name__)


def _init_pro():
    """【V26.88+10-1】薄壳：复用 ``data.fetcher._client._ensure_pro_initialized()``。

    保留函数名以兼容 ``core.pool_manager.__init__`` 的 ``re-export``，但不再
    自己 ``ts.pro_api(token)`` —— 全交给懒初始化单例，避免多份独立 DataApi 实例
    各撞一套限流器 / http 缓存。
    """
    try:
        from data.fetcher import _client as _fclient
        return _fclient._ensure_pro_initialized()
    except Exception as e:
        logger.warning(f"[init_pro-thin-wrapper-fail] {e}")
        return None


def __getattr__(name):
    """PEP 562 懒模块属性：访问 ``blacklists.pro`` / ``pool_manager.pro`` 时按需解析。

    仅 ``pro`` 走懒初始化；其它属性仍按常规 AttributeError 抛出，避免误捕获拼写错误。
    """
    if name == "pro":
        from data.fetcher import _client as _fclient
        return _fclient._ensure_pro_initialized()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# 历史模块级 ``from core.pool_manager.blacklists import pro`` 兼容：
# 这类 ``from x import y`` 在 import 期执行 ``x.__getattr__('y')`` 并把结果快照到目标模块，
# 会把 ``None`` 锁定。因此本文件仅暴露 ``pro`` 作为属性（PEP 562），禁止外部 ``from ... import pro``。
# 如果检测到错误用法，运行时（再枚举一次已重新查询）会自然修正；如果在 import 期立即调用 pro.xxx，
# 仍可能拿 None——但本仓库已 grep 确认没有此类用法。

GLOBAL_UNLOCK_BLACKLIST = None


def _get_pro_for_internal():
    """模块内部使用：函数体内安全获取 pro，永不返回缓存值。"""
    from data.fetcher import _client as _fclient
    return _fclient._ensure_pro_initialized()


def _get_unlock_blacklist(anchor_yyyymmdd=None):
    """解禁窗口以 anchor 日为起点向后 30 天；与 DB 最新交易日对齐，避免自然日漂移导致入池集合变化。
    anchor 无效时回退当前日历日。
    默认不启用（见 config use_tushare_unlock_blacklist_for_p1），不调 Tushare。"""
    try:
        from core.config_manager import get_p1_use_tushare_unlock_blacklist

        if not get_p1_use_tushare_unlock_blacklist():
            return set()
    except Exception:
        return set()
    pro = _get_pro_for_internal()
    if pro is None:
        logger.warning("[tushare-init-skip] 解禁黑名单检查")
        return set()
    try:
        cand = str(anchor_yyyymmdd or "").strip()
        if len(cand) == 8 and cand.isdigit():
            start_date = cand
        else:
            start_date = datetime.now().strftime("%Y%m%d")
        try:
            dt_start = datetime.strptime(start_date, "%Y%m%d")
        except ValueError:
            start_date = datetime.now().strftime("%Y%m%d")
            dt_start = datetime.strptime(start_date, "%Y%m%d")
        end_date = (dt_start + timedelta(days=30)).strftime("%Y%m%d")
        df_unlock = pro.share_float(start_date=start_date, end_date=end_date)
        if not df_unlock.empty:
            return set(df_unlock["ts_code"].tolist())
    except Exception as e:
        logger.warning(f"[unlock-fail] {e}")
    return set()


def _get_punish_blacklist(max_age_days: int = 7, anchor_yyyymmdd: str = None) -> set:
    """读取 scan_engine 写入的 blacklist.json。
    仅返回 kill_date 在 [anchor−max_age_days, anchor] 内的 ts_code（与 _add_to_blacklist 侧 7 日清理语义一致）。
    无 kill_date 或格式异常的历史条目不参与拦截，避免「只读不写」时永久残留。

    【修复】anchor_yyyymmdd：若传入数据锚定日而非日历日，可保证同日多次运行 cutoff 固定，
    避免跨午夜时 cutoff 边界从「含今日」漂移到「不含今日」。"""
    blacklist_file = path_blacklist_json()
    if not os.path.exists(blacklist_file):
        return set()
    try:
        with open(blacklist_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logger.warning(f"[punish-read] {e}")
        return set()
    if not isinstance(data, dict):
        return set()
    bj_tz = timezone(timedelta(hours=8))
    if anchor_yyyymmdd and len(str(anchor_yyyymmdd)) == 8 and str(anchor_yyyymmdd).isdigit():
        ref_date = datetime.strptime(str(anchor_yyyymmdd), "%Y%m%d").replace(tzinfo=bj_tz)
    else:
        ref_date = datetime.now(bj_tz)
    cutoff = (ref_date - timedelta(days=max(1, int(max_age_days)))).strftime("%Y%m%d")
    out: set = set()
    for k, v in data.items():
        if not isinstance(v, dict):
            continue
        kd = str(v.get("kill_date") or "").strip()
        if len(kd) == 8 and kd.isdigit() and kd >= cutoff:
            out.add(str(k))
    return out


__all__ = [
    "_init_pro", "pro", "GLOBAL_UNLOCK_BLACKLIST",
    "_get_unlock_blacklist", "_get_punish_blacklist",
    "_get_pro_for_internal",
]