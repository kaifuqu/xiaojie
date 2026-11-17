# -*- coding: utf-8 -*-
"""
core/scan/intraday_snapshots.py
原 core/scan_engine.py L1182-L1664（盘中快照 + P3 首次触发落库 + 信号日志 + 交易语言）
- _INTRADAY_SLOT_IDS（六时点槽位：930/935/940/1030/1125/1325/1425/1440）
- _normalize_intraday_slot_id
- _intraday_write_slot_to_stock_row
- _tail_volume_anchor
- _apply_intraday_snapshots_to_rt（槽位字段并入 rt + 早盘量比地板）
- get_intraday_snapshot_status（JSON 文件读取）
- capture_intraday_snapshots（force 路径 + auto/1030/1300/1435 路径）
- save_p3_first_trigger_snapshot（P3 当日首次触发落库）
- save_signal_log（信号日志落库）
- _format_p4_trade_language（P4 短标签）
- _p3_hit_tier / _format_p3_trade_layers（P3 战法分层）

【公开 API】
- capture_intraday_snapshots
- get_intraday_snapshot_status
- save_p3_first_trigger_snapshot
- save_signal_log
- _INTRADAY_SLOT_IDS
"""
import json
import logging
import os
from datetime import datetime

import numpy as np
import pandas as pd

import constants
from core.scan.utils import (
    BJ_TZ,
    _safe_float,
    ensure_runtime_data_layout,
    path_intraday_snapshots_json,
    save_df_to_sql,
    normalize_stock_display_name,
)

logging.basicConfig(level=logging.INFO, format='%(asctime)s [扫描引擎-intraday] %(message)s')


def _norm_signal_ts_code(raw) -> str:
    """
    signal_log 落库前统一 ts_code 口径（带交易所后缀）。

    【V30.10 2026-10-21】signal_log 历史行的 ts_code 是不带后缀的 6 位裸代码
    （如 '000001'），而 daily_data / bars_daily 统一带后缀（'000001.SZ'），
    导致下游 join 必须靠补丁函数兜底。落库时直接写规范口径，
    让 signal_log 与行情表天然对齐。
    """
    try:
        from core.signal_ledger_bridge import normalize_ts_code

        return normalize_ts_code(str(raw or "").strip())
    except Exception:
        return str(raw or "").strip()


# 【V30.1 2026-10-16】signal_log 落库失败累计计数（进程内）。
# 用途：把「扫描成功但信号未落库」从不可见的 debug 日志变成可统计的 WARNING 计数，
# 供 verify_system 的上线门禁与日志巡检直接判断。
_SIGNAL_LOG_FAIL_COUNT = 0


def get_signal_log_fail_count() -> int:
    """返回本进程 signal_log 落库失败累计次数（供体检/巡检读取）。"""
    return int(_SIGNAL_LOG_FAIL_COUNT)


# ==================== P3 触发快照 + signal_log ====================
def save_p3_first_trigger_snapshot(item, rt, hit_res, regime, final_score, penalty, crowd_score):
    """保存 P3 当日首次正式触发快照；同一股票同一天只写入一次。"""
    # 【V30.11 2026-10-18】与 save_signal_log 对齐的不落库开关。
    # 背景：2026-10-18 上线前回归跑完后，只读快照发现 p3_first_trigger_snapshot
    # 新增 2 行 emoji 回显名测试行（601919 / 600036，trade_date=2026-10-18），
    # 说明 V30.8 的三道闸没有覆盖 P3 首次触发快照落库路径。
    # 短路点位于 try 之外、取写连接之前；默认不设该变量 = 生产行为完全不变。
    if os.environ.get("XJ_NO_SIGNAL_PERSIST", "").strip().lower() in ("1", "true", "yes", "on"):
        logging.warning(
            "[p3_snapshot] XJ_NO_SIGNAL_PERSIST=1 已设置，本次 P3 首次触发快照不落库 code=%s（点检模式）",
            str((item or {}).get("代码", "") or ""),
        )
        return False
    try:
        if not isinstance(item, dict) or not isinstance(hit_res, dict):
            return False
        now = datetime.now(BJ_TZ)
        detail = hit_res.get("detail") or {}
        penalty_detail = hit_res.get("penalty_breakdown") or {}
        try:
            from core.scan.detectors_pattern import _danger_buy_estimate_vwap_from_rt
            vwap = _danger_buy_estimate_vwap_from_rt(rt, _safe_float(rt.get("price"), 0.0)) if _danger_buy_estimate_vwap_from_rt else 0.0
        except Exception:
            vwap = 0.0
        row = pd.DataFrame([{
            "ts_code": str(item.get("代码", "") or "").strip(),
            "trade_date": now.strftime("%Y%m%d"),
            "trigger_time": now.strftime("%H:%M:%S"),
            "curr_min": int(_safe_float(rt.get("curr_min"), now.hour * 60 + now.minute)),
            "name": normalize_stock_display_name(item.get("名称", "")),
            "strategies": json.dumps(hit_res.get("strategies") or [], ensure_ascii=False),
            "price": _safe_float(rt.get("price"), 0.0),
            "vwap": _safe_float(vwap, 0.0),
            "vol_ratio": _safe_float(rt.get("vol_ratio"), 0.0),
            "turnover_rate_f": _safe_float(rt.get("turnover_rate_f"), 0.0),
            "net_main_amount": _safe_float(rt.get("net_main_amount"), 0.0),
            "sector_strength": _safe_float(detail.get("sector_strength"), 1.0),
            "sector_beta": _safe_float(detail.get("sector_beta"), 1.0),
            "mainline_score": _safe_float(detail.get("mainline_score"), 0.0),
            "regime": str(regime or ""),
            "burst_score": _safe_float(hit_res.get("burst_score"), 0.0),
            "engine_penalty": _safe_float(penalty_detail.get("engine"), penalty),
            "risk_penalty": _safe_float(penalty_detail.get("risk"), 0.0),
            "total_penalty": _safe_float(penalty_detail.get("total"), penalty),
            "crowding_score": _safe_float(crowd_score, 0.0),
            "final_score": _safe_float(final_score, 0.0),
            "risk_tags": json.dumps(hit_res.get("risk_tags") or [], ensure_ascii=False),
            "created_at": now,
        }])
        if not row.at[0, "ts_code"]:
            return False
        return bool(save_df_to_sql(row, "p3_first_trigger_snapshot", if_exists="ignore"))
    except Exception as e:
        logging.warning("P3 首次触发快照落库失败: %s", e)
        return False


def save_signal_log(item, pool_key, regime, breakdown_str, business_date: str = ""):
    """
    【V29 重构】旧结构化写入器 → 转发到统一 signal_log 写入入口。

    原实现（问题）：
      - 写死旧列集（trade_date/strategy/score/regime/score_breakdown/limit_times/forecast_type）
      - 主键 (trade_date, ts_code) 会在同一天多池命中时互相覆盖
      - 用 save_df_to_sql(INSERT *) 写入，与 data/signal_logger 的 DDL 完全不兼容
      - 结果：P2~P5 信号实际从未落库，core/p1_pool_relay 与 tools/db_inspector 长期读到空/残缺数据
    现在：构造 items → core.pool_signal_adapter.safe_log_pool_signal
    → data/signal_logger.log_signal（统一 schema + 原子战法 ID + 数据质量标记 + 风控点位）。
    本函数签名与返回值保持不变，不改任何调用方。

    【V30.1 2026-10-16】原实现把取写连接异常只记 ``logging.debug``，
    于是「DuckDB 连接配置冲突导致落库失败」在生产日志里完全不可见，
    扫描仍被判成功（P5 选出 8 只但 signal_log 当天 0 条）。
    现改为 WARNING + 累计失败计数，便于事后从日志直接发现信号未落库。

    【V30.8 2026-10-18】新增不落库开关 XJ_NO_SIGNAL_PERSIST=1。
    背景：UI 逐菜单点检 / 策略实验室点检直接对生产库跑 P4/P5 扫描，
    save_signal_log 无条件 UPSERT，2026-10-18 上线前测试因此把 43 行测试信号
    写进生产库，其中 9 行按 (trade_date, pool, ts_code) 主键覆盖了 10-16 的
    真实盘后信号（详见 docs/SIGNAL_LOG_TEST_POLLUTION.md）。
    现在：设置 XJ_NO_SIGNAL_PERSIST=1 时本函数直接返回，不写 signal_log /
    signal_strategy_ledger，从根上避免点检污染生产库。默认不设 = 行为不变。
    """
    global _SIGNAL_LOG_FAIL_COUNT
    # 【V30.8】不落库开关：点检/实验室/回归脚本设 XJ_NO_SIGNAL_PERSIST=1 即可
    # 完全跳过 signal_log 与 signal_strategy_ledger 写入，避免污染生产库。
    if os.environ.get("XJ_NO_SIGNAL_PERSIST", "").strip().lower() in ("1", "true", "yes", "on"):
        logging.warning(
            "[signal_log] XJ_NO_SIGNAL_PERSIST=1 已设置，本次 %s 池扫描不落库 code=%s（点检模式）",
            pool_key, str((item or {}).get("代码", "") or ""),
        )
        return
    try:
        if not isinstance(item, dict): return
        from core.pool_signal_adapter import safe_log_pool_signal
        _score = item.get('综合分', 0.0) or 0.0
        try:
            _score = float(_score)
        except (TypeError, ValueError):
            _score = 0.0
        _price = item.get('现价', 0.0) or 0.0
        try:
            _price = float(str(_price).replace('%', ''))
        except (TypeError, ValueError):
            _price = 0.0
        _strategies = item.get('战法', '')
        if isinstance(_strategies, (list, tuple, set)):
            _strategies = [str(s) for s in _strategies if str(s).strip()]
        elif not isinstance(_strategies, str):
            _strategies = str(_strategies or '')
        _position = str(item.get('建议仓位', '') or '')
        _exit_plan = item.get('exit_plan') if isinstance(item.get('exit_plan'), dict) else None
        _items = [{
            'ts_code': _norm_signal_ts_code(item.get('代码', '')),
            'name': normalize_stock_display_name(item.get('名称', '')),
            'score': _score,
            'burst_score': _score,
            'strategies': _strategies,
            'price': _price,
            'rank': int(item.get('排名', 0) or 0),
            # 【V30.10 语义修正 2026-10-21】原先读 item.get('_pushed', False)，
            # 而 '_pushed' 这个 key 在全项目【只有读取点、没有写入点】（死 key），
            # 导致 signal_log.pushed 100% 为 False —— 无法区分
            # 「入池候选」与「用户真的收到了企微」。
            # 现在这里统一写 False（= 候选）；
            # 真实推送由 core/notification/gateway.py 在推送占位成功后回写 TRUE。
            'pushed': False,
            'observation': bool(item.get('_explicit_observation', False)),
            'regime': str(regime or ''),
            'score_breakdown': str(breakdown_str or ''),
            'limit_times': str(item.get('连板高度', '') or ''),
            'forecast_type': str(item.get('机构预测', '') or ''),
            'suggested_position': _position,
            'exit_plan': _exit_plan,
            'remark': str(breakdown_str or '')[:200],
        }]
        _pool = str(pool_key or '').lower()
        if _price <= 0 or not _items[0]['ts_code']:
            # 无有效价格/代码的条目不落库（与旧写入器行为一致：脏行不入库）
            return
        try:
            from data.db_core import get_write_conn
            with get_write_conn() as _con:
                _n = int(
                    safe_log_pool_signal(
                        _pool, _items, db_conn=_con,
                        trade_date=(str(business_date or "").strip() or None),
                    ) or 0
                )
            if _n <= 0:
                _SIGNAL_LOG_FAIL_COUNT += 1
                logging.warning(
                    "[signal_log] %s 池信号未写入（写入条数=0）code=%s — 扫描成功但信号未落库",
                    _pool, _items[0]['ts_code'],
                )
        except Exception as _e_db:
            _SIGNAL_LOG_FAIL_COUNT += 1
            logging.warning(
                "[signal_log] %s 池信号落库失败（累计 %s 次）code=%s: %s",
                _pool, _SIGNAL_LOG_FAIL_COUNT, _items[0]['ts_code'], _e_db,
            )
    except Exception as e:
        _SIGNAL_LOG_FAIL_COUNT += 1
        logging.warning(f"save_signal_log 落库失败: {e}")


# ==================== P4 / P3 交易语言 ====================
def _format_p4_trade_language(hits, stock_memory_score=0.0, sector_beta=1.0, close_vwap_dev=None):
    """把 P4 命中翻成更像交易员看盘的短标签。"""
    hits = [str(h) for h in (hits or []) if str(h).strip()]
    tags = []
    hit_txt = " ".join(hits)
    if any("P4-01" in h or "光头阳线抢筹" in h for h in hits):
        tags.append("🔥抢筹")
    if any("P4-02" in h or "筹码锁死" in h for h in hits):
        tags.append("🧲锁筹")
    if any("P4-03" in h or "均线缩量低吸" in h for h in hits):
        tags.append("↘️回踩")
    if any("P4-04" in h or "强势洗盘承接" in h for h in hits):
        tags.append("🧱承接")
    if any("P4-05" in h or "动能突破共振" in h for h in hits):
        tags.append("🚀突破")
    # 【V35】P4-06 底仓趋势延续（旧编号 P4-07，旧名「底仓不破均线」），两个名字都要匹配
    if any("P4-06" in h or "底仓趋势延续" in h or "底仓不破均线" in h for h in hits):
        tags.append("🛡️底仓")
    if stock_memory_score >= 12.0:
        tags.append("👑股性强")
    elif stock_memory_score >= 6.0:
        tags.append("⭐股性热")
    elif stock_memory_score < -3.0:
        tags.append("🧊股性弱")
    if close_vwap_dev is not None:
        if close_vwap_dev > 2.5:
            tags.append("⚠️尾盘偷袭")
        elif close_vwap_dev < -1.5:
            tags.append("弱收")
    if sector_beta >= 1.25:
        tags.append("🌋板块热")
    elif sector_beta <= 0.9:
        tags.append("🧊板块冷")
    if not tags:
        tags.append("--")
    if hit_txt:
        return " / ".join(tags[:4])
    return "--"


def _p3_hit_tier(hit: str) -> str:
    s = str(hit or "")
    if any(k in s for k in ["右侧起爆", "均线回踩低吸", "巨头连贯发力", "倍量启动延续"]):
        return "主战法"
    if any(k in s for k in ["单峰跃迁", "平台/二次确认", "资金逆势托底", "均线粘合突破"]):
        return "辅助标签"
    if "缩量分歧低吸" in s:
        return "观察项"
    return "辅助标签"


def _format_p3_trade_layers(hits):
    hits = [str(h) for h in (hits or []) if str(h).strip()]
    tiers = {"主战法": [], "辅助标签": [], "观察项": []}
    for h in hits:
        tiers[_p3_hit_tier(h)].append(h)
    return {
        "主战法": " / ".join(tiers["主战法"]) if tiers["主战法"] else "--",
        "辅助标签": " / ".join(tiers["辅助标签"]) if tiers["辅助标签"] else "--",
        "观察项": " / ".join(tiers["观察项"]) if tiers["观察项"] else "--",
    }


# ==================== 日内槽位 ====================
_INTRADAY_SLOT_IDS = frozenset({"930", "935", "940", "1030", "1125", "1325", "1425", "1440"})


def _normalize_intraday_slot_id(raw):
    """后台六时点槽位：935/1030/1125/1325/1425/1440。"""
    if raw is None:
        return None
    s = str(raw).strip().lower()
    if s in _INTRADAY_SLOT_IDS:
        return s
    if s.isdigit():
        s2 = str(int(s))
        if s2 in _INTRADAY_SLOT_IDS:
            return s2
    return None


def _intraday_write_slot_to_stock_row(sn: dict, rt_data: dict, curr_min: int, slot: str) -> None:
    """
    单票写入某一瞬态槽位（须 force 路径调用，总是覆盖该槽字段）。
    早盘三档：935/1030/1125；午盘末 1325；尾盘两锚 1425→vol_1435、1440→vol_1440。
    """
    amt = _safe_float(rt_data.get("amount", 0))
    vol = _safe_float(rt_data.get("volume", 0))
    now_p = _safe_float(rt_data.get("price", 0))
    high = _safe_float(rt_data.get("high", 0))
    vr = _safe_float(rt_data.get("vol_ratio", 0))
    curr_vwap = 0.0
    if amt > 0 and vol > 0:
        _vs = max(vol, 1e-9)
        tentative = amt / _vs
        curr_vwap = amt / max(vol * 100.0, 1e-9) if tentative > now_p * 20 else tentative

    sn[f"vr_{slot}"] = vr
    sn[f"vol_{slot}"] = vol
    sn[f"high_{slot}"] = high
    if now_p > 0:
        sn[f"price_{slot}"] = now_p
    if slot == "1030":
        sn["time_1030"] = curr_min
    else:
        sn[f"tmin_{slot}"] = curr_min
    if curr_vwap > 0:
        sn[f"vwap_{slot}"] = curr_vwap

    if slot == "1325":
        sn["pre_tail_high"] = high
        if curr_vwap > 0:
            sn["pre_tail_vwap"] = curr_vwap
        sn["time_1300"] = curr_min

    if slot == "1425":
        sn["vol_1435"] = vol
        sn["time_1435"] = curr_min

    if slot == "1440":
        sn["vol_1440"] = vol
        sn["time_1440"] = curr_min


def _tail_volume_anchor(sn: dict, curr_min: int):
    """尾盘 tail_vol_ratio：过 14:40 后优先用 1440 锚点，否则 1425。"""
    if not isinstance(sn, dict):
        return None, None
    t1440 = sn.get("time_1440")
    if t1440 is not None and curr_min > int(t1440):
        return sn.get("vol_1440"), int(t1440)
    t1435 = sn.get("time_1435")
    if t1435 is not None and curr_min > int(t1435):
        return sn.get("vol_1435"), int(t1435)
    return None, None


def _apply_intraday_snapshots_to_rt(rt: dict, sn: dict) -> None:
    """JSON 瞬态记忆并入 rt：全槽位字段 + P3/P4 用的早盘量比地板。"""
    if not isinstance(rt, dict) or not isinstance(sn, dict):
        return
    for k in (
        # 【V26.88+10-1 P5 新增】9:30 集合竞价结束 + 9:40 早盘确认 两点槽位
        "vr_930", "vol_930", "high_930", "vwap_930", "price_930", "time_930",
        "vr_935", "vol_935", "high_935", "tmin_935", "vwap_935", "price_935",
        "vr_940", "vol_940", "high_940", "vwap_940", "price_940", "time_940",
        "vr_1030", "time_1030", "vol_1030", "high_1030", "vwap_1030", "price_1030",
        "vr_1125", "vol_1125", "high_1125", "tmin_1125", "vwap_1125", "price_1125",
        "vr_1325", "vol_1325", "high_1325", "tmin_1325", "vwap_1325", "price_1325",
        "pre_tail_high", "pre_tail_vwap", "time_1300",
        "vol_1435", "time_1435", "price_1425",
        "vol_1440", "time_1440", "price_1440",
        # 【V33 P4 补口】快照写入端一直在写 vr_/vwap_/tmin_ 的 1425 与 1440 槽，
        # 但这里没把它们投进 rt → P4 尾盘只能拿到 13:25 的 pre_tail_vwap，
        # 「站稳尾盘均价 / 尾盘斜率阶梯」这类真正决定 T+1 的因子拿不到数据。
        # 只新增键、不改任何既有键的语义，对 P3 无影响。
        "vr_1425", "tmin_1425", "vwap_1425",
        "vr_1440", "tmin_1440", "vwap_1440",
    ):
        if k in sn:
            rt[k] = sn[k]
    morning = []
    for _k in ("vr_935", "vr_1030", "vr_1125"):
        if _k in sn:
            v = _safe_float(sn.get(_k), -1.0)
            if v >= 0:
                morning.append(v)
    if morning:
        rt["vr_morning_floor"] = float(min(morning))
        rt["vr_morning_mean"] = float(sum(morning) / len(morning))


def get_intraday_snapshot_status():
    """返回当日快照完成情况（供 UI / 诊断）。
    六时点：935 / 1030 / 1125 / 1325（午盘末）/ 1425（尾盘锚1）/ 1440（尾盘锚2）。
    仍附带 done_1030、done_1300、done_1435 与旧 ratio_* 字段名以便兼容展示。
    """
    now_time = datetime.now(BJ_TZ)
    today_str = now_time.strftime("%Y%m%d")
    snapshot_file = path_intraday_snapshots_json()
    snapshots = {}
    if os.path.exists(snapshot_file):
        try:
            with open(snapshot_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if data.get("date") == today_str:
                    snapshots = data.get("stocks", {})
        except Exception:
            snapshots = {}

    total = len(snapshots)
    if total <= 0:
        z = {
            "total": 0,
            "quality_score": 0.0,
            "quality_grade": "C",
            "quality_hint": "未建立快照，建议先捕获关键时点",
        }
        for sid in ("935", "1030", "1125", "1325", "1425", "1440"):
            z[f"done_{sid}"] = 0
            z[f"ratio_{sid}"] = 0.0
        z["done_1030"] = z["done_1300"] = z["done_1435"] = 0
        z["ratio_1030"] = z["ratio_1300"] = z["ratio_1435"] = 0.0
        return z

    def _has_slot(s: dict, sid: str) -> bool:
        if sid == "935":
            return "vr_935" in s
        if sid == "1030":
            return "vr_1030" in s and "time_1030" in s
        if sid == "1125":
            return "vr_1125" in s
        if sid == "1325":
            return ("pre_tail_high" in s) or ("pre_tail_vwap" in s)
        if sid == "1425":
            return ("vol_1435" in s) and ("time_1435" in s)
        if sid == "1440":
            return ("vol_1440" in s) and ("time_1440" in s)
        return False

    done = {sid: 0 for sid in ("935", "1030", "1125", "1325", "1425", "1440")}
    for _, s in snapshots.items():
        if not isinstance(s, dict):
            continue
        for sid in done:
            if _has_slot(s, sid):
                done[sid] += 1

    score = sum((done[sid] / total) * (100.0 / 6.0) for sid in done)
    if score >= 90.0:
        grade = "A"
        hint = "六时点快照齐套，早盘地板与双尾盘锚可用"
    elif score >= 65.0:
        grade = "B"
        hint = "快照部分齐套，P3/P4 早盘与尾盘判定可能偏保守"
    else:
        grade = "C"
        hint = "快照不足，易漏判/误判，请确认后台调度或手动补录"

    out = {
        "total": total,
        "quality_score": round(score, 2),
        "quality_grade": grade,
        "quality_hint": hint,
        "done_1030": done["1030"],
        "done_1300": done["1325"],
        "done_1435": done["1425"],
        "ratio_1030": round(done["1030"] / total * 100.0, 1),
        "ratio_1300": round(done["1325"] / total * 100.0, 1),
        "ratio_1435": round(done["1425"] / total * 100.0, 1),
    }
    for sid in done:
        out[f"done_{sid}"] = done[sid]
        out[f"ratio_{sid}"] = round(done[sid] / total * 100.0, 1)
    return out


def capture_intraday_snapshots(codes=None, capture_mode='auto', force=False, slot_id=None):
    """日内快照主入口：force + slot 路径优先（后台调度），否则按 mode 自动/1030/1300/1435。"""
    now_time = datetime.now(BJ_TZ)
    curr_min = now_time.hour * 60 + now_time.minute
    today_str = now_time.strftime("%Y%m%d")
    slot = _normalize_intraday_slot_id(slot_id)

    snapshot_file = path_intraday_snapshots_json()
    snapshots = {}
    if os.path.exists(snapshot_file):
        try:
            with open(snapshot_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if data.get("date") == today_str:
                    snapshots = data.get("stocks", {})
        except json.JSONDecodeError as e:
            logging.warning(f"日内快照 JSON 损坏 [{snapshot_file}]: {e}，将重建当日快照容器。")
        except OSError as e:
            logging.warning(f"读取日内快照 IO 失败 [{snapshot_file}]: {e}")
        except Exception as e:
            logging.warning(f"读取日内快照失败（未分类）[{snapshot_file}]: {e}")

    if not codes:
        from core.scan.utils import load_p1_cache, fetch_realtime_batch
        p1_cache = load_p1_cache(today_str)
        if p1_cache:
            if isinstance(p1_cache, list):
                codes = [x.get('ts_code', x.get('code', '')) for x in p1_cache if isinstance(x, dict)]
            elif isinstance(p1_cache, dict):
                codes = list(p1_cache.keys())
        if not codes:
            from core.scan.utils import get_all_basic_industry
            codes = list(get_all_basic_industry().keys())

    if not codes:
        return "无股票池"

    rt_map = fetch_realtime_batch(codes)
    if not rt_map:
        return "获取实时行情失败"

    if slot:
        if not force:
            return f"slot={slot} 捕获须 force=True（由后台调度触发）"
        need_save = False
        update_count = 0
        for s_code, rt_data in rt_map.items():
            if not isinstance(rt_data, dict):
                continue
            if s_code not in snapshots:
                snapshots[s_code] = {}
            _intraday_write_slot_to_stock_row(snapshots[s_code], rt_data, curr_min, slot)
            need_save = True
            update_count += 1
        if need_save:
            ensure_runtime_data_layout()
            try:
                with open(snapshot_file, 'w', encoding='utf-8') as f:
                    json.dump({"date": today_str, "stocks": snapshots}, f, ensure_ascii=False)
                return f"OK slot {slot} 快照捕获成功（更新 {update_count} 只，{now_time.strftime('%H:%M')}）"
            except Exception as e:
                return f"保存失败: {e}"
        return f"slot {slot} 无有效标的"

    mode = str(capture_mode or 'auto').strip().lower()
    valid_modes = {'auto', '1030', '1300', '1435'}
    if mode not in valid_modes:
        mode = 'auto'

    if mode == 'auto':
        if curr_min >= 875:
            mode = '1435'
        elif curr_min >= 780:
            mode = '1300'
        elif curr_min >= 630:
            mode = '1030'
        else:
            return "尚未到捕获时段"

    windows = {
        '1030': (615, 660),
        '1300': (770, 820),
        '1435': (860, 895),
    }
    ws, we = windows.get(mode, (0, 24 * 60))
    if (not force) and not (ws <= curr_min <= we):
        return f"当前时间 {now_time.strftime('%H:%M')} 不在 {mode} 捕获窗口内（{ws//60:02d}:{ws%60:02d}-{we//60:02d}:{we%60:02d}）"

    need_save = False
    update_count = 0
    for s_code, rt_data in rt_map.items():
        if not isinstance(rt_data, dict):
            continue
        if s_code not in snapshots:
            snapshots[s_code] = {}

        amt = _safe_float(rt_data.get('amount', 0))
        vol = _safe_float(rt_data.get('volume', 0))
        now_p = _safe_float(rt_data.get('price', 0))

        curr_vwap = 0.0
        if amt > 0 and vol > 0:
            _vs = max(vol, 1e-9)
            tentative = amt / _vs
            curr_vwap = amt / max(vol * 100.0, 1e-9) if tentative > now_p * 20 else tentative

        if mode == '1030':
            if force or ('vr_1030' not in snapshots[s_code]):
                snapshots[s_code]['vr_1030'] = _safe_float(rt_data.get('vol_ratio', 0))
                snapshots[s_code]['time_1030'] = curr_min
                need_save = True
                update_count += 1
        elif mode == '1300':
            if force or ('pre_tail_high' not in snapshots[s_code]):
                snapshots[s_code]['pre_tail_high'] = _safe_float(rt_data.get('high', 0))
                if curr_vwap > 0:
                    snapshots[s_code]['pre_tail_vwap'] = curr_vwap
                snapshots[s_code]['time_1300'] = curr_min
                need_save = True
                update_count += 1
        elif mode == '1435':
            if force or ('vol_1435' not in snapshots[s_code]):
                snapshots[s_code]['vol_1435'] = vol
                snapshots[s_code]['time_1435'] = curr_min
                need_save = True
                update_count += 1

    if need_save:
        ensure_runtime_data_layout()
        try:
            with open(snapshot_file, 'w', encoding='utf-8') as f:
                json.dump({"date": today_str, "stocks": snapshots}, f, ensure_ascii=False)
            return f"OK {mode} 快照捕获成功（更新 {update_count} 只，{now_time.strftime('%H:%M')}）"
        except Exception as e:
            return f"保存失败: {e}"

    return f"{mode} 快照无需更新"
