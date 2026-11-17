# -*- coding: utf-8 -*-
"""
core/p1_rejected_persist.py
P1 淘汰原因落盘（守护进程 / UI 共用）。

【为什么需要这个模块 2026-11-05】
用户问「阳光电源连涨 3 天，为什么系统没提示」，排查时发现：
  · 它确实不在当日 P1 主池的 30 只里，所以 P2/P3/P5 整链不会给它打分；
  · 但**无法知道它当天具体被哪一道闸拦下** —— `p1_rejected_cache.json`
    只有 UI 手动重建时才写，守护进程每日 08:58 自动重建 P1 时把 1011 条淘汰原因
    直接丢弃，只打一行「入池 30 拒绝 1011」。
  结果就是「事后无法回答任何一只票为什么没进池」，只能靠前瞻性猜测。

【本模块的职责】
  · 把淘汰展示行原子落盘到 state/p1_rejected_cache.json（保持 UI 兼容的纯 list 格式）
  · 同目录另写 sidecar 元信息（交易日 / 入池数 / 拒绝数 / 死因分布 TopN / 来源），
    便于直接在界面或命令行看清「今天为什么收了这么少」
  · 返回一份 summary 供调用方打日志，不抛异常（落盘失败绝不阻断 P1 主流程）

【格式契约】
  · 主文件：JSON list，每行是 UI 展示字典（代码/名称/淘汰死因/被裁阶段/当前得分/…）
    —— 与 ui/app.py `_save_rejected_json` 完全一致，UI 侧零改动即可读取
  · sidecar：state/p1_rejected_cache.meta.json（本模块新增，UI 不读，仅供追溯）
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from collections import Counter
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

_META_SUFFIX = ".meta.json"

# UI 展示列：缺失时补默认值，避免 UI 侧 KeyError
_DEFAULT_FIELDS: Dict[str, Any] = {
    "代码": "",
    "名称": "",
    "淘汰死因": "未记录",
    "被裁阶段": "未知",
    "当前得分": 0.0,
    "满分项1": "--",
    "满分项2": "--",
    "最低项1": "--",
    "最低项2": "--",
    "死因层级": "",
    "处置建议": "",
    "层级徽标": "",
}


def _decimal_default(o: Any) -> Any:
    """JSON 兜底编码器：numpy/pandas 标量降级为原生类型。"""
    try:
        from core.p1_balance import _json_default as _p1_json_default

        return _p1_json_default(o)
    except Exception:
        pass
    for attr in ("item", "tolist"):
        try:
            return getattr(o, attr)()
        except Exception:
            continue
    return str(o)


def _normalize_row(row: Any) -> Dict[str, Any]:
    """补齐 UI 展示必需的键，保留原行里的其它信息。"""
    if not isinstance(row, dict):
        return dict(_DEFAULT_FIELDS, **{"淘汰死因": str(row) or "未记录"})
    out: Dict[str, Any] = {}
    for k, v in row.items():
        out[str(k)] = v
    for k, v in _DEFAULT_FIELDS.items():
        if k not in out or out[k] is None:
            out[k] = v
    return out


def _atomic_write_json(path: str, payload: Any) -> None:
    """先写同目录 tmp → flush+fsync → os.replace，避免半截 JSON 把旧文件写坏。"""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".p1_rejected_", suffix=".json.tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=0, default=_decimal_default)
            f.flush()
            try:
                os.fsync(f.fileno())
            except Exception:
                pass
        os.replace(tmp_path, path)
    except Exception:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
        raise


def _top_counts(values: Iterable[Any], limit: int = 8) -> List[Tuple[str, int]]:
    try:
        c = Counter(str(v).strip() or "未记录" for v in values)
        return [[k, int(n)] for k, n in c.most_common(int(limit))]
    except Exception:
        return []


def _trim_cause(text: str) -> str:
    """死因文案去掉开头的表情符号，便于聚合统计（原始文案仍完整保存在主文件）。"""
    s = str(text or "").strip()
    while s and not (s[0].isalnum() or s[0] in "([{" or "\u4e00" <= s[0] <= "\u9fff"):
        s = s[1:].lstrip()
    return s or "未记录"


def persist_p1_rejected(
    rejected: Optional[List[Dict[str, Any]]],
    *,
    pool_size: int = 0,
    trade_date: str = "",
    source: str = "DAEMON_AUTO",
    extra_meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """把 P1 淘汰名单落盘（主文件 + sidecar 元信息）。

    Args:
        rejected: build_p1_pool_and_cache 返回的淘汰展示行列表
        pool_size: 本轮入池数量（写进 sidecar，便于和主池对齐）
        trade_date: 交易日 YYYYMMDD（P1 锚定日）
        source: DAEMON_AUTO / UI_MANUAL，用于事后区分是谁写的
        extra_meta: 调用方附加的元信息（如 anchor、profile、regime）

    Returns:
        summary dict（同时写入 sidecar）。任何失败都返回空 summary，绝不抛异常。
    """
    summary: Dict[str, Any] = {"ok": False, "path": "", "count": 0}
    try:
        rows: List[Dict[str, Any]] = [
            _normalize_row(r) for r in (rejected or []) if isinstance(r, (dict, str))
        ]

        from core.runtime_data_paths import path_p1_rejected_json

        path = path_p1_rejected_json()
        _atomic_write_json(path, rows)

        # sidecar：主文件必须保持纯 list（UI 契约），统计信息单独存放
        try:
            causes = [_trim_cause(r.get("淘汰死因")) for r in rows]
            meta: Dict[str, Any] = {
                "trade_date": str(trade_date or ""),
                "generated_at": datetime.now().astimezone().isoformat(),
                "source": str(source or ""),
                "pool_size": int(pool_size or 0),
                "rejected_count": len(rows),
                "stage_top": _top_counts([r.get("被裁阶段") for r in rows]),
                "level_top": _top_counts([r.get("死因层级") for r in rows]),
                "death_cause_top": _top_counts(causes),
            }
            if isinstance(extra_meta, dict):
                for k, v in extra_meta.items():
                    meta.setdefault(str(k), v)
            _atomic_write_json(path + _META_SUFFIX, meta)
        except Exception as e_meta:
            logger.debug("[p1-rejected] sidecar 写入失败(主文件已落盘): %s", e_meta)
            meta = {}

        summary = {
            "ok": True,
            "path": path,
            "count": len(rows),
            "pool_size": int(pool_size or 0),
            "stage_top": meta.get("stage_top", []),
            "level_top": meta.get("level_top", []),
            "death_cause_top": meta.get("death_cause_top", []),
        }
        return summary
    except Exception as e:
        logger.warning("[p1-rejected] 淘汰原因落盘失败(不影响主池): %s", e, exc_info=True)
        return summary


def format_summary_line(summary: Dict[str, Any], top_n: int = 3) -> str:
    """把 summary 压成一行日志，便于直接在 sniper.log 里看死因构成。"""
    try:
        if not summary or not summary.get("ok"):
            return "淘汰原因落盘未成功"
        parts: List[str] = []
        for label, key in (("阶段", "stage_top"), ("死因", "death_cause_top")):
            items = summary.get(key) or []
            if items:
                seg = "、".join(f"{k}×{v}" for k, v in items[:top_n])
                parts.append(f"{label}: {seg}")
        head = f"淘汰原因已落盘 拒绝{summary.get('count', 0)}只(入池{summary.get('pool_size', 0)}只)"
        return head + (" | " + " | ".join(parts) if parts else "")
    except Exception:
        return "淘汰原因落盘完成"


__all__ = ["persist_p1_rejected", "format_summary_line"]
