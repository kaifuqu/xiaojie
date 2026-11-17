# -*- coding: utf-8 -*-
"""
Tier1-1.3 / 1.4 / 1.6 · Shadow 影子层（只记录，不拦截）

设计原则（写死，不要改）：
1. 本模块只回答"假如这条规则启用，今天会否决/降级哪些信号"，**绝不修改 res、绝不参与
   过滤、打分、排序、推送**。上线后对选股结果的影响严格为 0。
2. 任何异常一律降级为空操作，绝不向调用方抛出（宁可不记录，也不能打断主链路）。
3. **不写数据库**：不新增表、不给 signal_log 加列，只追加 JSONL 到
   `data/runtime/shadow/shadow_YYYYMMDD.jsonl`（可用环境变量 XJ_SHADOW_DIR 改目录）。
4. 时区固定 Asia/Shanghai；参数只在本模块内生效，不动 config.yaml / master_control.json。

对应三件事：
- 1.3 剔除最大阴量（放量阴线出货）→ `bear_volume_verdict()`
- 1.4 RPS 灰度到 P2/P3               → `rps_verdict()`
- 1.6 推送三档降噪                   → `classify_push_tier()`

观察期数据积累后用 `tools/shadow_report.py` 出"如果启用会否决多少、被否决的那批
后续 N 日收益如何"的对照表，再由人决定是否真的接线。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import pandas as pd

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------- 规则参数
# 仅 Shadow 计算使用；不覆盖任何线上阈值。
BEAR_VOL_LOOKBACK = 10        # 观察窗口（交易日）
BEAR_VOL_RATIO_MIN = 1.5      # 最大阴量 / 阳线均量 的倍数阈值
BEAR_VOL_RECENT_DAYS = 3      # 该阴线须落在最近 N 日内才算"近期出货"
RPS_GATE_MIN = 50.0           # RPS 百分位下限（低于则记"would_veto"）
RPS_PERIOD = 60               # 与 core/p1_rps_calculator.RPS_PERIOD 保持一致
SHADOW_TZ = ZoneInfo("Asia/Shanghai")

_rps_cache: Dict[str, Dict[str, float]] = {}
_rps_lock = threading.Lock()
# RPS 失败熔断：只读连接不可用（daemon 占库等）时，10 分钟内不再重试，
# 避免在落库/推送热路径上反复退避等待。
_RPS_COOLDOWN_SEC = 600.0
_rps_fail_until: float = 0.0
_last_error: Optional[str] = None


# ---------------------------------------------------------------- 小工具
def _now() -> datetime:
    return datetime.now(SHADOW_TZ)


def _norm_code(code: Any) -> str:
    c = str(code or "").strip().upper()
    if not c:
        return ""
    if "." in c:
        return c
    if c.startswith(("6", "9")):
        return c + ".SH"
    if c.startswith(("0", "3", "2")):
        return c + ".SZ"
    if c.startswith(("4", "8")):
        return c + ".BJ"
    return c


def shadow_dir() -> Path:
    """Shadow 记录目录（默认 data/runtime/shadow，可用 XJ_SHADOW_DIR 覆盖）。"""
    env = str(os.environ.get("XJ_SHADOW_DIR") or "").strip()
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent / "data" / "runtime" / "shadow"


def fmt_date(d: Any = None) -> str:
    """统一成 'YYYY-MM-DD'（与 daily_data / signal_log 的 trade_date 写法一致）。"""
    if d is None:
        return _now().strftime("%Y-%m-%d")
    t = str(d).strip()
    if not t:
        return _now().strftime("%Y-%m-%d")
    t = t.replace("/", "-")
    if len(t) == 8 and t.isdigit():
        return "%s-%s-%s" % (t[0:4], t[4:6], t[6:8])
    return t[:10]


def _as_float(v: Any, default: float = 0.0) -> float:
    try:
        f = float(v)
    except Exception:
        return default
    if f != f or f in (float("inf"), float("-inf")):
        return default
    return f


# ---------------------------------------------------------------- 1.3 最大阴量
def bear_volume_verdict(df: Optional[pd.DataFrame]) -> Dict[str, Any]:
    """
    判断"近 BEAR_VOL_LOOKBACK 日内是否出现放量阴线（出货）"。

    返回 dict：
        would_veto(bool) / reason(str) / max_bear_vol / yang_avg_vol / ratio / days_ago
    数据不足时 would_veto=False，reason 写明不足原因（不误杀）。
    """
    out: Dict[str, Any] = {
        "would_veto": False,
        "reason": "",
        "max_bear_vol": 0.0,
        "yang_avg_vol": 0.0,
        "ratio": 0.0,
        "days_ago": None,
    }
    try:
        if df is None or not isinstance(df, pd.DataFrame) or df.empty:
            out["reason"] = "bars_empty"
            return out
        need = {"open", "close", "vol"}
        if not need.issubset({str(c).lower() for c in df.columns}):
            out["reason"] = "bars_missing_cols"
            return out
        d = df.copy()
        d.columns = [str(c).lower() for c in d.columns]
        d["open"] = pd.to_numeric(d["open"], errors="coerce")
        d["close"] = pd.to_numeric(d["close"], errors="coerce")
        d["vol"] = pd.to_numeric(d["vol"], errors="coerce")
        d = d.dropna(subset=["open", "close", "vol"])
        d = d[d["vol"] > 0]
        if d.empty:
            out["reason"] = "bars_all_zero_vol"
            return out
        d = d.tail(int(BEAR_VOL_LOOKBACK))
        if len(d) < 3:
            out["reason"] = "bars_too_few:%d" % len(d)
            return out
        is_yin = d["close"] < d["open"]
        yang = d[~is_yin]
        yin = d[is_yin]
        yang_avg = float(yang["vol"].mean()) if not yang.empty else 0.0
        if yang_avg <= 0 or yin.empty:
            out["reason"] = "no_yin_or_no_yang"
            return out
        idx_max = yin["vol"].idxmax()
        max_bear = float(yin.loc[idx_max, "vol"])
        ratio = max_bear / yang_avg
        # 该阴线处在窗口内倒数第几根（1=最新）
        order = list(d.index)
        pos = order.index(idx_max)
        days_ago = len(order) - pos
        out.update(
            max_bear_vol=round(max_bear, 2),
            yang_avg_vol=round(yang_avg, 2),
            ratio=round(ratio, 3),
            days_ago=int(days_ago),
        )
        if ratio >= BEAR_VOL_RATIO_MIN and days_ago <= BEAR_VOL_RECENT_DAYS:
            out["would_veto"] = True
            out["reason"] = "放量阴线出货: 阴量/阳量均量=%.2f≥%.2f 且发生在 %d 日前" % (
                ratio,
                BEAR_VOL_RATIO_MIN,
                days_ago,
            )
        else:
            out["reason"] = "ok"
        return out
    except Exception as e:  # noqa: BLE001
        out["reason"] = "error:%s" % type(e).__name__
        return out


# ---------------------------------------------------------------- 1.4 RPS 灰度
def rps_verdict(codes: List[str]) -> Dict[str, float]:
    """返回 {ts_code: rps 百分位}；按"当日"缓存，失败返回空 dict（不误杀）。"""
    global _last_error, _rps_fail_until
    try:
        want = [_norm_code(c) for c in (codes or []) if str(c or "").strip()]
        if not want:
            return {}
        if time.time() < _rps_fail_until:
            return {}
        key = _now().strftime("%Y%m%d")
        with _rps_lock:
            cached = _rps_cache.get(key)
        if cached is None:
            from core.p1_rps_calculator import compute_rps_map

            cached = compute_rps_map(period=RPS_PERIOD) or {}
            if not cached:
                # 连接不可用 / 数据不足：进熔断，热路径上不再反复等 30s
                _rps_fail_until = time.time() + _RPS_COOLDOWN_SEC
                logger.debug("[shadow] RPS 不可用，进入 %.0fs 冷却", _RPS_COOLDOWN_SEC)
                return {}
            with _rps_lock:
                if len(_rps_cache) > 4:
                    _rps_cache.clear()
                _rps_cache[key] = cached
        return {c: float(cached[c]) for c in want if c in cached}
    except Exception as e:  # noqa: BLE001
        _last_error = "%s: %s" % (type(e).__name__, e)
        _rps_fail_until = time.time() + _RPS_COOLDOWN_SEC
        logger.debug("[shadow] RPS 计算降级: %s", _last_error)
        return {}


# ---------------------------------------------------------------- 1.6 推送分档
def classify_push_tier(pool: str, item: Dict[str, Any]) -> Dict[str, Any]:
    """
    三档分流（只记录，不改变投递）：
      alarm     告警级：数据质量存在严重标记（停牌/新股/ST/一字），必须立刻让人知道
      important 重要级：多战法共振 或 分数进入当日头部
      observe   观察级：单战法、低置信 → 建议只写库不推送
    """
    try:
        pool_u = str(pool or "").upper()
        item = item or {}
        flags = item.get("data_quality_flags")
        flag_txt = ""
        if isinstance(flags, str):
            flag_txt = flags
        elif isinstance(flags, (list, tuple)):
            flag_txt = ",".join(str(x) for x in flags)
        elif isinstance(flags, dict):
            flag_txt = ",".join(str(k) for k, v in flags.items() if v)
        severe = [t for t in ("SUSPEND", "SUSP", "NEW", "ST", "LIMIT_UP", "ONE_WORD")
                  if t.upper() in flag_txt.upper()]
        if severe:
            return {"tier": "alarm", "reason": "数据质量严重标记:%s" % ",".join(severe)}

        score = _as_float(item.get("score"), 0.0)
        strats = item.get("strategies") or []
        if isinstance(strats, str):
            strats = [s for s in strats.replace("|", ",").split(",") if s.strip()]
        n_strat = len(strats) if isinstance(strats, (list, tuple, set)) else 0
        rank = int(_as_float(item.get("rank"), 0) or 0)

        if n_strat >= 2:
            return {"tier": "important", "reason": "多战法共振(%d 条)" % n_strat}
        if pool_u in ("P1", "P2") and score >= 75.0:
            return {"tier": "important", "reason": "%s 底仓/竞价高分 %.1f" % (pool_u, score)}
        if rank and rank <= 3:
            return {"tier": "important", "reason": "池内排名 #%d" % rank}
        return {"tier": "observe", "reason": "单战法/低置信(score=%.1f)" % score}
    except Exception as e:  # noqa: BLE001
        return {"tier": "observe", "reason": "error:%s" % type(e).__name__}


# ---------------------------------------------------------------- 行情取数
def load_recent_bars(db_conn, codes: List[str], lookback: int = 20) -> Dict[str, pd.DataFrame]:
    """
    批量取最近 lookback 根日线（含当日），用于 1.3。只读查询，单次 SQL。
    失败返回空 dict（下游按"数据不足"处理，不误杀）。
    """
    try:
        want = sorted({_norm_code(c) for c in (codes or []) if str(c or "").strip()})
        if not want or db_conn is None:
            return {}
        lo = int(max(5, min(60, lookback)))
        marks = ",".join(["?"] * len(want))
        sql = (
            "SELECT ts_code, trade_date, open, high, low, close, vol FROM ("
            "  SELECT ts_code, trade_date, open, high, low, close, vol,"
            "         row_number() OVER (PARTITION BY ts_code ORDER BY trade_date DESC) AS rn"
            "  FROM daily_data WHERE ts_code IN (%s)"
            ") t WHERE rn <= ? ORDER BY ts_code, trade_date" % marks
        )
        df = db_conn.execute(sql, want + [lo]).df()
        if df is None or df.empty:
            return {}
        df["ts_code"] = df["ts_code"].astype(str).str.strip().str.upper()
        return {str(k): g.copy() for k, g in df.groupby("ts_code", sort=False)}
    except Exception as e:  # noqa: BLE001
        logger.debug("[shadow] 取日线失败: %s", e)
        return {}


# ---------------------------------------------------------------- 统一入口
def annotate_shadow(pool: str, items: List[Dict[str, Any]], db_conn=None,
                    write: bool = True) -> List[Dict[str, Any]]:
    """
    为一批 signal item 计算 Shadow 结论并（默认）追加到当日 JSONL。

    返回记录列表；**永不抛异常**；不修改入参 item。
    """
    global _last_error
    records: List[Dict[str, Any]] = []
    try:
        rows = [it for it in (items or []) if isinstance(it, dict)]
        if not rows:
            return records
        codes = [it.get("ts_code") or it.get("code") for it in rows]
        bars = load_recent_bars(db_conn, codes, lookback=max(BEAR_VOL_LOOKBACK + 5, 20))
        # 没有可用连接时不去碰 RPS（避免热路径上的连接退避），rps 记为 None
        rps_map = rps_verdict(codes) if db_conn is not None else {}
        now = _now()
        for it in rows:
            try:
                code = _norm_code(it.get("ts_code") or it.get("code"))
                bv = bear_volume_verdict(bars.get(code))
                rps = rps_map.get(code)
                tier = classify_push_tier(pool, it)
                rec = {
                    "ts": now.isoformat(timespec="seconds"),
                    "trade_date": fmt_date(it.get("trade_date")),
                    "pool": str(pool or ""),
                    "ts_code": code,
                    "name": str(it.get("name") or ""),
                    "score": _as_float(it.get("score"), 0.0),
                    "price": _as_float(it.get("price"), 0.0),
                    "strategies": it.get("strategies") or [],
                    # 1.3
                    "veto_bear_vol": bool(bv.get("would_veto")),
                    "bear_vol_reason": str(bv.get("reason") or ""),
                    "bear_vol_ratio": bv.get("ratio"),
                    "bear_vol_days_ago": bv.get("days_ago"),
                    # 1.4
                    "rps": (round(float(rps), 2) if rps is not None else None),
                    "veto_rps": (bool(rps is not None and float(rps) < RPS_GATE_MIN)),
                    # 1.6
                    "push_tier": tier.get("tier"),
                    "tier_reason": tier.get("reason"),
                }
                records.append(rec)
            except Exception as e:  # noqa: BLE001
                logger.debug("[shadow] 单条标注失败: %s", e)
        if write and records:
            _append_jsonl(records, now)
        return records
    except Exception as e:  # noqa: BLE001
        _last_error = "%s: %s" % (type(e).__name__, e)
        logger.debug("[shadow] annotate_shadow 降级: %s", _last_error)
        return records


def _append_jsonl(records: List[Dict[str, Any]], now: datetime) -> None:
    try:
        d = shadow_dir()
        d.mkdir(parents=True, exist_ok=True)
        path = d / ("shadow_%s.jsonl" % now.strftime("%Y%m%d"))
        with open(path, "a", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
    except Exception as e:  # noqa: BLE001
        # 目录不可写（如只读沙箱）时不报错，只在 debug 留痕
        logger.debug("[shadow] 写 JSONL 降级: %s", e)


def read_shadow_records(day: Optional[str] = None) -> List[Dict[str, Any]]:
    """读回 Shadow 记录；day=None 表示全部日期，'20261103' 表示单日。"""
    out: List[Dict[str, Any]] = []
    try:
        d = shadow_dir()
        if not d.exists():
            return out
        pat = "shadow_%s.jsonl" % day if day else "shadow_*.jsonl"
        for p in sorted(d.glob(pat)):
            try:
                with open(p, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            out.append(json.loads(line))
                        except Exception:
                            continue
            except Exception:
                continue
        return out
    except Exception:
        return out


def last_error() -> Optional[str]:
    return _last_error
