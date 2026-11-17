# -*- coding: utf-8 -*-
"""
core/pool_manager/cache_sovereignty.py
原 core/pool_manager.py L2758-L2788

【职责】
- P1 缓存 JSON 主权：UI 人工洗盘 vs 守护自动重建
- P1_CACHE_JSON_SOURCE_UI_MANUAL / P1_CACHE_JSON_SOURCE_DAEMON_AUTO
- p1_cache_json_should_skip_daemon_overwrite：守护写入前检测 UI 已干预
"""
import json
import logging
import os

logger = logging.getLogger(__name__)


P1_CACHE_JSON_SOURCE_UI_MANUAL = "UI_MANUAL"
P1_CACHE_JSON_SOURCE_DAEMON_AUTO = "DAEMON_AUTO"


def p1_cache_json_should_skip_daemon_overwrite(json_path: str) -> bool:
    """
    守护进程写入前的主权防御：若当日 p1_cache_YYYYMMDD.json 已存在且声明为 UI 人工洗盘，
    则不得覆盖 JSON 及同源 DuckDB p1_cache，以人工数据为准。

    Returns:
        True  — 应跳过本次守护覆写；
        False — 可继续写入（文件不存在、旧版纯数组格式、或 _source 非 UI_MANUAL）。

    【V26.88+10-14】空 items 的 UI_MANUAL 视为「无效 UI 干预」（可能是历史上 UI 在数据未就绪
    时触发了空洗盘，导致 daemon 此后永远无法补救）。若 items 为空数组，自动忽略
    ``_source=UI_MANUAL`` 标记并允许 daemon 重建，同时打印告警，方便用户在 UI 中后续
    重新洗盘覆盖。
    """
    p = str(json_path or "").strip()
    if not p or not os.path.isfile(p):
        return False
    try:
        with open(p, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except Exception as e:
        logger.warning(f"[p1-cache-sovereignty] {p}: {e}")
        return False
    if isinstance(payload, dict) and payload.get("_source") == P1_CACHE_JSON_SOURCE_UI_MANUAL:
        items = payload.get("items")
        if not isinstance(items, list) or len(items) == 0:
            # 空 UI 干预不视为有效主权：保留文件以便排查，但允许 daemon 覆写
            logger.warning(
                "[p1-cache-sovereignty] %s 标记为 UI_MANUAL 但 items 为空（%d 个），"
                "视为历史无效洗盘，守护将生成有效 P1 缓存覆盖。若需重新人工干预，请在 UI 中重新洗盘。",
                p, 0 if not isinstance(items, list) else len(items),
            )
            return False
        logger.warning(
            "[p1-cache-sovereignty] UI 人工洗盘已干预，守护放弃自动覆盖，以人工数据为准: %s",
            p,
        )
        return True
    return False


__all__ = [
    "P1_CACHE_JSON_SOURCE_UI_MANUAL",
    "P1_CACHE_JSON_SOURCE_DAEMON_AUTO",
    "p1_cache_json_should_skip_daemon_overwrite",
]