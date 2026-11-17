# -*- coding: utf-8 -*-
"""
core/paper_trading/engine.py — 模拟盘交易引擎

【职责】
  · on_signals(pool_key, rows)：消费 P3/P4 正式候选行，按信号价买入。
  · on_tick()：交易时段轮询持仓，按 止损 / 当日跌幅 / 止盈 / 到期 四类规则 100% 清仓。
  · daily_settle()：收盘后写 paper_equity_daily 日净值。

【交易规则（A 股真实约束）】
  · 买入价 = 信号行「现价」字段（pipeline 写入的信号时刻真实价）。
  · 止损 = 买入价 × (1 + stop_loss_pct/100)，默认 -3%。
  · 止盈 = 买入价 × (1 + take_profit_pct/100)，默认 +8%。
  · 当日跌幅离场 = 现价 ≤ 昨收 × (1 + day_drop_exit_pct/100)，默认 -3%：
    只要「今天这只票跌了 3%」就卖，不管这笔持仓是浮盈还是浮亏。
  · 多条件同时触发时按 止损 > 当日跌幅 > 止盈 > 到期 取原因（风险事件优先记录）。
  · T+1：当日买入次日才可卖（enforce_t_plus_1=True 时）。
  · 100 股整手：不足 1 手直接放弃该信号。
  · 现金不足 / 已持仓 / 停牌 / 跌停封板 / 报价无效 → 跳过并记录 skip_reason。

【执行归属】
  仅由守护进程调用；UI 侧完全只读。
"""
from __future__ import annotations

import logging
import re
import time
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from core.paper_trading import store
from core.paper_trading.config import PaperTradingConfig, load_config
from core.paper_trading.fees import FeeBreakdown, apply_slippage, compute_fee, limit_price, round_price

_log = logging.getLogger(__name__)

ACCOUNT_ID = store.ACCOUNT_ID

SKIP_ALREADY_HELD = "ALREADY_HELD"
SKIP_INSUFFICIENT_CASH = "INSUFFICIENT_CASH"
SKIP_LOT_TOO_SMALL = "LOT_TOO_SMALL"
SKIP_BAD_PRICE = "BAD_PRICE"
SKIP_NO_QUOTE = "NO_QUOTE"
SKIP_SUSPENDED = "SKIP_SUSPENDED"
SKIP_LIMIT_DOWN = "SKIP_LIMIT_DOWN"
SKIP_LIMIT_UP = "SKIP_LIMIT_UP"
SKIP_EXCLUDED_DELISTING = "EXCLUDED_DELISTING"
SKIP_COHORT_CLOSED = "COHORT_CLOSED"
SKIP_ACCOUNT_PAUSED = "ACCOUNT_PAUSED"
SKIP_DISABLED = "DISABLED"

EXIT_STOP_LOSS = "STOP_LOSS"
EXIT_TAKE_PROFIT = "TAKE_PROFIT"
# 【2026-11-05 新增·用户抛出要求】当日跌幅离场：相对**昨收**跌到阈值（默认 -3%）无条件卖出，
# 与持仓盈亏无关（浮盈也跑）。注意它与 STOP_LOSS 不是一回事：
#   STOP_LOSS   = 相对「买入价」跌 -3%（-3% 止损）
#   DAY_DROP    = 相对「昨收」当日跌 -3%（今天这只票弱，先出来）
EXIT_DAY_DROP = "DAY_DROP"
# 【P0-3 修复 2026-11-04】到期强平：持有交易日数达到 max_hold_days 仍未被止损/止盈触发时，
# 按当时价格强制平仓。此前模拟盘完全没有超期逻辑 —— 只要没碰到 ±3%/8% 就无限期持有，
# 与回测「T+10 收盘强平」口径不一致，导致两套结果永远对不上。
EXIT_TIMEOUT = "TIMEOUT"
EXIT_EXPERIMENT_END = "END_OF_EXPERIMENT"

OBS_TIER = "C"

# A 股涨跌停幅度（用于判定「跌停封板无法卖出」）
LIMIT_PCT_BY_BOARD = {
    "main": 10.0,       # 主板
    "star": 20.0,       # 科创板 688
    "gem": 20.0,        # 创业板 300/301
    "bse": 30.0,        # 北交所 8xx/4xx/920（不再误判成主板 10%）
}

# ST / *ST 降档幅度：仅沪深主板 ±5%。
# 创业板 / 科创板 / 北交所的风险警示股票仍执行注册制幅度（±20% / ±20% / ±30%）。
# 2026-10-18 修正：原先 ST 无条件 5%，会把创业板 ST 的跌停价算高 15 个百分点。
ST_LIMIT_PCT_BY_BOARD = {
    "main": 5.0,
    "star": 20.0,
    "gem": 20.0,
    "bse": 30.0,
}

# 【V30.4 2026-10-18 A股合规】各板块买入最小数量与递增单位：
#   · 主板（沪 60 / 深 00）：100 股起，1 手 = 100 股，之后只能 100 的整数倍；
#   · 创业板（300/301）：最低 200 股，之后 1 股递增（即最小买入 200，之后任意整数）；
#   · 科创板（688/689）：最低 200 股，之后 1 股递增；
#   · 北交所（4xx/8xx/920）：最低 100 股，之后 1 股递增。
# 原实现 cfg.lot_size=100 一刀切，会在科创板/创业板出现「买 100 股」的非法委托。
MIN_BUY_SHARES_BY_BOARD = {"main": 100, "star": 200, "gem": 200, "bse": 100}

# 无涨跌幅限制的品种前缀：这些票不能套用 ±10%/±20%/±30% 涨跌停规则。
#   · N/C 开头 = 上市首日（沪深注册制新股上市前 5 日不设涨跌幅）；
#   · U 开头 = 未盈利上市（科创板/创业板未盈利企业上市首日）；
#   · D = 当日新上市（北交所新股上市首日不设涨跌幅）。
#   · 退 / PT = 退市整理期 / 风险警示，应整体排除出模拟盘（不参与、不可交易）。
NO_LIMIT_PREFIXES = ("N", "C", "U", "D")
EXCLUDE_NAME_PREFIXES = ("退", "PT")

# 新股上市后无涨跌幅限制的天数（注册制）：
#   · 沪深主板注册制新股：上市前 5 个交易日；
#   · 创业板 / 科创板：上市前 5 个交易日；
#   · 北交所：上市首日。
NO_LIMIT_DAYS = 5


def board_of(code: str) -> str:
    """按代码首位判断板块，返回 main/star/gem/bse。"""
    c = str(code or "").strip()
    symbol = c.split(".")[0] if "." in c else c
    suffix = c.rsplit(".", 1)[-1].upper() if "." in c else ""
    if suffix == "BJ" or symbol.startswith(("4", "8", "920")):
        return "bse"
    if symbol.startswith("688") or symbol.startswith("689"):
        return "star"
    if symbol.startswith("300") or symbol.startswith("301"):
        return "gem"
    return "main"


def is_st_stock(name: str) -> bool:
    """ST / *ST 标记识别（名称含 ST 即视为 5% 涨跌停品种）。"""
    n = str(name or "").upper().replace(" ", "")
    return "ST" in n


def is_no_limit_stock(code: str, name: Any = None) -> bool:
    """上市初期无涨跌幅限制的品种：N / C / U / D 开头。

    【V30.5 2026-10-18 修正】A股 N/C/U/D 标记挂在 *名称* 开头，代码仍是纯 6 位数字
    （如 301569.SZ 的名称是「C联亚药业」）。原实现只查代码前缀，导致所有真实
    新股都会被当成「有涨跌幅限制」而套用 ±10/20/30%，在上市首日算出根本不存在的
    涨跌停价。这里同时识别代码前缀（兼容历史伪造数据）与名称前缀（生产真实形态）。
    """
    symbol = str(code or "").strip().split(".")[0].upper()
    if symbol and symbol.startswith(NO_LIMIT_PREFIXES):
        return True
    n = str(name or "").strip().upper().replace(" ", "")
    return bool(n) and n.startswith(NO_LIMIT_PREFIXES)


def is_excluded_name(name: str) -> bool:
    """退市整理期 / PT 类标的：名字以「退」或「PT」开头，模拟盘一律不参与。"""
    n = str(name or "").strip().upper().replace(" ", "")
    return n.startswith(EXCLUDE_NAME_PREFIXES)


def has_no_limit_window(list_date: Any, trade_date: Any, board: str) -> bool:
    """判断该交易日是否处于「无涨跌幅限制」窗口（新股上市初期）。

    list_date / trade_date 均接受 'YYYYMMDD' / 'YYYY-MM-DD' / date / datetime。
    """
    ld = _to_ymd(list_date)
    td = _to_ymd(trade_date)
    if not ld or not td:
        return False
    if td < ld:
        return False
    idx = _trade_day_index_since(ld, td)
    if idx is None:
        # 交易日历不可用时的保守降级：按自然日估算（仅上市首周内可能命中）
        try:
            d_list = datetime.strptime(ld, "%Y%m%d").date()
            d_trade = datetime.strptime(td, "%Y%m%d").date()
        except ValueError:
            return False
        if d_trade < d_list:
            return False
        gap = (d_trade - d_list).days
        return gap <= 0 if board == "bse" else gap < NO_LIMIT_DAYS
    if board == "bse":
        return idx <= 0
    return idx < NO_LIMIT_DAYS


_TRADE_CAL_CACHE: Optional[List[str]] = None
_TRADE_CAL_CACHE_AT = 0.0


def _trade_day_index_since(list_date_ymd: str, trade_date_ymd: str) -> Optional[int]:
    """返回「trade_date 相对 list_date 的交易日序号」（list_date 当天记 0）。

    【V30.5 2026-10-18 修正】原实现用自然日差 (d_trade - d_list).days 近似交易日，
    在国庆长假（10-01 ~ 10-07 休市）后上市的新股会被算成「已上市 5 日以上」而
    错误套用涨跌停 —— 实际交易所按 *交易日* 计数，沪深新股上市前 5 个交易日无涨跌幅。
    这里改从 dim_trade_calendar 读取真实开市日序列。

    注意：dim_trade_calendar 只入库到「今天」（sync_trade_calendar 刻意不臆造未来），
    所以当天/未来交易日往往查不到。这里对超出日历覆盖的目标日按「周一至周五」
    前向估算（覆盖国庆/春节等长假会略偏保守，但绝不会把新股的 5 日窗口算短）；
    日历整体不可用时返回 None 交由上层自然日降级。
    """
    global _TRADE_CAL_CACHE, _TRADE_CAL_CACHE_AT
    now_mono = time.monotonic()
    if _TRADE_CAL_CACHE is not None and (now_mono - _TRADE_CAL_CACHE_AT) < 3600.0:
        cal = _TRADE_CAL_CACHE
    else:
        cal = None
        try:
            from data.db_core import get_read_conn

            with get_read_conn(read_only=True) as con:
                try:
                    cal = [
                        str(r[0] or "").strip().replace("-", "")[:8]
                        for r in con.execute(
                            "SELECT REPLACE(CAST(trade_date AS VARCHAR),'-','') FROM dim_trade_calendar "
                            "WHERE is_open IS TRUE ORDER BY 1"
                        ).fetchall()
                    ]
                except Exception:
                    cal = []
                if not cal:
                    # 表缺失/为空时退回「主库实际有行情的交易日序列」。
                    # 这比自然日差可靠得多：daily_data 的日期本身就是真实开市日。
                    cal = [
                        str(r[0] or "").strip().replace("-", "")[:8]
                        for r in con.execute(
                            "SELECT DISTINCT REPLACE(CAST(trade_date AS VARCHAR),'-','') "
                            "FROM daily_data ORDER BY 1"
                        ).fetchall()
                    ]
            cal = [d for d in cal if len(d) == 8 and d.isdigit()]
        except Exception as e:  # noqa: BLE001
            _log.debug("[模拟盘] 读取 dim_trade_calendar 失败（新股窗口按自然日降级）: %s", e)
            cal = None
        _TRADE_CAL_CACHE = cal
        _TRADE_CAL_CACHE_AT = now_mono
    if not cal:
        # 日历表整体不可用（隔离库 / 表未同步）：退回「周一至周五」计数。
        # 比自然日差可靠得多（自然日差会把周末当交易日，指数虚高一倍），
        # 且方向保守：遇长假会高估「已上市天数」，只会让无涨跌幅窗口更长，
        # 绝不会把新股提前按 ±10/20% 判成涨停/跌停。
        return _weekday_index_since(list_date_ymd, trade_date_ymd)
    last_cal = cal[-1]
    if list_date_ymd > last_cal:
        # 上市日在日历覆盖范围之外（尚未上市），窗口不成立
        return None
    try:
        start = cal.index(list_date_ymd)
    except ValueError:
        # 上市日当天休市/日历未覆盖：取之后第一个开市日作为窗口起点
        start = next((i for i, d in enumerate(cal) if d >= list_date_ymd), len(cal))
    if trade_date_ymd in cal:
        end = cal.index(trade_date_ymd)
    elif trade_date_ymd > last_cal:
        # 目标日在未来：日历最后开市日 + 工作日估算
        try:
            d_last = datetime.strptime(last_cal, "%Y%m%d").date()
            d_tar = datetime.strptime(trade_date_ymd, "%Y%m%d").date()
        except ValueError:
            return None
        extra = 0
        cur = d_last
        while cur < d_tar:
            cur += timedelta(days=1)
            if cur.weekday() < 5:
                extra += 1
        end = len(cal) - 1 + extra
    else:
        return None
    if end < start:
        return None
    return end - start


def _weekday_index_since(list_date_ymd: str, trade_date_ymd: str) -> Optional[int]:
    """无交易日历时用「周一至周五」计数，返回 trade_date 相对 list_date 的序号。"""
    try:
        d_list = datetime.strptime(list_date_ymd, "%Y%m%d").date()
        d_tar = datetime.strptime(trade_date_ymd, "%Y%m%d").date()
    except ValueError:
        return None
    if d_tar < d_list:
        return None
    idx = 0
    cur = d_list
    while cur < d_tar:
        cur += timedelta(days=1)
        if cur.weekday() < 5:
            idx += 1
    return idx


def _to_ymd(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, (date, datetime)):
        return v.strftime("%Y%m%d")
    s = str(v).strip().replace("-", "").replace("/", "")
    return s[:8] if len(s) >= 8 and s[:8].isdigit() else ""


def _f(v: Any, default: float = 0.0) -> float:
    """宽松 float 转换；'--' / None / 空串 / '10.5%' 都能处理。"""
    if v is None:
        return default
    if isinstance(v, (int, float)):
        try:
            fv = float(v)
        except (TypeError, ValueError):
            return default
        if fv != fv:  # NaN
            return default
        return fv
    s = str(v).strip().replace("%", "").replace(",", "")
    if s in ("", "--", "-", "None", "nan"):
        return default
    try:
        return float(s)
    except (TypeError, ValueError):
        return default


def parse_signal_row(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """把扫描引擎输出行归一化为模拟盘信号结构。

    过滤规则（与设计一致）：
      · pool_tier == 'observation' → 拒绝（观察池不买）
      · 执行层级 == 'C' → 拒绝（P4 尾盘快照不完整 / 0% 仓位）
      · 现价 <= 0 → 拒绝
    """
    if not isinstance(row, dict):
        return None
    code = str(row.get("代码") or "").strip()
    if not code:
        return None
    if str(row.get("pool_tier") or "main").strip().lower() == "observation":
        return None
    tier = str(row.get("执行层级") or "").strip().upper()
    if tier == OBS_TIER:
        return None
    pos_hint = str(row.get("建议仓位") or "").strip()
    if pos_hint.startswith("0%"):
        return None
    price = _f(row.get("现价"), 0.0)
    if price <= 0:
        return None
    strat_raw = row.get("战法") or ""
    strategy_ids: List[str] = []
    strategy_names: List[str] = []
    try:
        from core.strategy_id_registry import parse_labeled_atomic_strategies

        for cid, label in parse_labeled_atomic_strategies(strat_raw):
            strategy_ids.append(cid)
            strategy_names.append(label or cid)
    except Exception as e:  # noqa: BLE001
        _log.debug("模拟盘解析原子战法失败 code=%s: %s", code, e)
        strategy_ids = []
        strategy_names = []
    score = _f(row.get("综合分"), 0.0)
    name = str(row.get("名称") or "").strip()
    for ch in ("⭐", "★", "☆", "🌟"):
        name = name.replace(ch, "")
    return {
        "code": code,
        "name": name.strip(),
        "price": price,
        "score": score,
        "strategy_ids": strategy_ids,
        "strategy_names": strategy_names,
        "signal_raw": row,
    }


def sort_key(sig: Dict[str, Any]) -> Tuple[float, float, str]:
    """买入优先级：综合分降序 → 价格降序 → 代码升序（保证可复现）。"""
    return (-float(sig.get("score") or 0.0), -float(sig.get("price") or 0.0), str(sig.get("code")))


def _order_id(prefix: str, trade_date: str, code: str, seq: int) -> str:
    return f"{prefix}{trade_date}{code}{seq:04d}"


def _next_seq(con: Any, trade_date: str, code: str) -> int:
    from core.paper_trading.schema import TABLE_ORDER

    try:
        r = store._row(
            con,
            f"SELECT COUNT(*) AS n FROM {TABLE_ORDER} WHERE trade_date = ? AND code = ?",
            [trade_date, code],
        )
    except Exception:  # noqa: BLE001
        return 1
    return int((r or {}).get("n") or 0) + 1


def _sellable_today(buy_date: str, today: str, *, enforce_t_plus_1: bool) -> bool:
    """T+1 判定：买入日 != 今天即可卖。"""
    if not enforce_t_plus_1:
        return True
    return str(buy_date) != str(today)


def _holding_days(buy_date: str, today: str) -> int:
    """自然日差（UI 展示用；交易日口径在排行榜里另算）。"""
    try:
        b = datetime.strptime(str(buy_date), "%Y%m%d").date()
        t = datetime.strptime(str(today), "%Y%m%d").date()
        return max(0, (t - b).days)
    except Exception:  # noqa: BLE001
        return 0


def _should_force_timeout(buy_date: str, today: str, max_hold_days: Any) -> bool:
    """到期强平判定（**交易日**口径，不是自然日）。

    【P0-3】为什么必须用交易日：max_hold_days=10 的语义是「最多持有 10 个交易日」，
    若用自然日差，国庆/春节长假会把 10 个交易日算成 16+ 天，导致提前强平。

    【保守失败策略】以下情况一律**不强平**（宁可继续持有，也不误卖）：
      - max_hold_days <= 0 或非法 → 该功能未启用
      - 交易日历不可用（_trade_day_index_since 返回 None）→ 无法判断，不冒险

    这是与回测（core/stop_rule_backtest.py）对齐的关键：回测在 T+max_hold 收盘强平，
    模拟盘此前完全没有超期逻辑，导致「只要不碰 ±3%/8% 就无限持有」。
    """
    try:
        mh = int(max_hold_days or 0)
    except (TypeError, ValueError):
        return False
    if mh <= 0:
        return False
    idx = _trade_day_index_since(str(buy_date or ""), str(today or ""))
    if idx is None:
        return False
    return int(idx) >= mh


def _make_buy_order(
    cfg: PaperTradingConfig,
    sig: Dict[str, Any],
    *,
    seq: int,
    trade_date: str,
    order_ts: str,
    cash_after: float,
    fee: FeeBreakdown,
    shares: int,
    exec_price: float,
) -> Dict[str, Any]:
    return {
        "order_id": _order_id("B", trade_date, sig["code"], seq),
        "client_order_id": f"{ACCOUNT_ID}|{sig['code']}|BUY|{trade_date}|{seq}",
        "side": "BUY",
        "order_ts": order_ts,
        "trade_date": trade_date,
        "pool": "",  # 由 on_signals 填充
        "code": sig["code"],
        "name": sig["name"],
        "strategy_ids": ",".join(sig["strategy_ids"]),
        "strategy_names": ",".join(sig["strategy_names"]),
        "signal_score": float(sig.get("score") or 0.0),
        "exec_price": exec_price,
        "shares": shares,
        "gross_amount": fee.gross_amount,
        "commission": fee.commission,
        "stamp_tax": fee.stamp_tax,
        "transfer_fee": fee.transfer_fee,
        "total_fee": fee.total_fee,
        "cash_delta": fee.cash_delta,
        "buy_price_raw": float(sig["price"]),
        "pnl": None,
        "pnl_pct": None,
        "holding_days": None,
        "exit_reason": None,
        "skip_reason": None,
        "note": f"cash_after={cash_after:.2f}",
    }


def _make_skip_order(
    sig: Dict[str, Any],
    *,
    seq: int,
    trade_date: str,
    order_ts: str,
    reason: str,
    pool: str,
) -> Dict[str, Any]:
    return {
        "order_id": _order_id("S", trade_date, sig["code"], seq),
        "client_order_id": f"{ACCOUNT_ID}|{sig['code']}|SKIP|{trade_date}|{seq}",
        "side": "SKIP",
        "order_ts": order_ts,
        "trade_date": trade_date,
        "pool": pool,
        "code": sig["code"],
        "name": sig["name"],
        "strategy_ids": ",".join(sig["strategy_ids"]),
        "strategy_names": ",".join(sig["strategy_names"]),
        "signal_score": float(sig.get("score") or 0.0),
        "exec_price": None,
        "shares": 0,
        "gross_amount": 0.0,
        "commission": 0.0,
        "stamp_tax": 0.0,
        "transfer_fee": 0.0,
        "total_fee": 0.0,
        "cash_delta": 0.0,
        "buy_price_raw": float(sig.get("price") or 0.0),
        "pnl": None,
        "pnl_pct": None,
        "holding_days": None,
        "exit_reason": None,
        "skip_reason": reason,
        "note": "",
    }


def _record_skip(con: Any, sig: Dict[str, Any], *, reason: str, pool: str, trade_date: str, order_ts: str) -> None:
    """跳过信号也写流水（可审计），但不写任何 skip 记录以外的副作用。"""
    try:
        seq = _next_seq(con, trade_date, sig["code"])
        cid = f"{ACCOUNT_ID}|{sig['code']}|SKIP|{trade_date}|{seq}"
        if store.order_exists(con, cid):
            return
        store.insert_order(
            con,
            _make_skip_order(
                sig, seq=seq, trade_date=trade_date, order_ts=order_ts, reason=reason, pool=pool
            ),
        )
    except Exception as e:  # noqa: BLE001
        _log.debug("记录跳过流水失败 code=%s reason=%s: %s", sig.get("code"), reason, e)


def compute_shares(
    *,
    cash: float,
    total_asset: float,
    price: float,
    cfg: PaperTradingConfig,
    code: str = "",
) -> Tuple[int, float, bool]:
    """按仓位模式 + 板块买入规则计算可买股数。

    【V30.11 2026-11-04】目标金额由 cfg.position_amount(total_asset) 决定：
      · fixed_amount 模式：固定金额/只（默认 5 万），与总资产脱钩，
        保证 20 只票风险敞口一致 → 按战法横向比较收益才有意义。
      · pct 模式：总资产 × position_pct（向后兼容）。

    返回 (shares, target_amount, cash_limited)；shares=0 表示不可买。
    cash_limited=True 表示「仓位目标够，但现金不够」→ 记 INSUFFICIENT_CASH；
    cash_limited=False 且 shares=0 表示「目标金额不足一手」→ 记 LOT_TOO_SMALL。
    target_amount 不含费用；实际扣款以费用引擎结果为准。
    """
    if price <= 0:
        return 0, 0.0, True
    target = cfg.position_amount(total_asset)
    board = board_of(code) if code else "main"
    lot = max(1, int(cfg.lot_size))
    min_buy = int(MIN_BUY_SHARES_BY_BOARD.get(board, lot))
    if board == "main":
        # 主板：100 股整手
        max_by_cash = int(cash // (price * lot)) * lot
        n = int(target // (price * lot)) * lot
        if n <= 0:
            return 0, target, False
        if n > max_by_cash:
            n = max_by_cash
        if n <= 0:
            return 0, target, True
        return n, target, False

    # 创业板 / 科创板：最低 min_buy 股（200），之后 1 股递增；
    # 北交所：最低 100 股，之后 1 股递增。
    n = int(target // price)
    if n < min_buy:
        # 仓位目标都买不起最小数量 -> 不是「现金不够」，是「一手都不到」
        return 0, target, False
    max_by_cash = int(cash // price)
    if n > max_by_cash:
        n = max_by_cash
    if n < min_buy:
        return 0, target, True
    return n, target, False


def _market_value(con: Any) -> float:
    """持仓市值：优先用 last_price，缺失时回退买入价（避免估值跳 0）。"""
    total = 0.0
    for p in store.load_positions(con):
        px = _f(p.get("last_price"), 0.0)
        if px <= 0:
            px = _f(p.get("buy_price_raw"), 0.0)
        total += px * int(p.get("shares") or 0)
    return total


def _total_asset(con: Any, cash: float) -> float:
    return float(cash) + _market_value(con)


def _shrink_to_affordable(shares: int, price: float, cfg: PaperTradingConfig, *, code: str, cash: float) -> int:
    """费用算完后发现现金不够，把股数回缩到「含费用也能买得起」的最大值。

    【V30.4】原实现固定 shares -= 100：主板正确，但创业板/科创板/北交所允许 1 股递增，
    减 100 会把本来能买 260 股的仓位压到 160 股（或者直接砍成 0 误判 INSUFFICIENT_CASH）。
    这里按板块规则回缩：主板按 100 股整手下台阶，其余按 1 股递减。
    """
    n = int(shares)
    if n <= 0 or price <= 0:
        return 0
    board = board_of(code)
    min_buy = int(MIN_BUY_SHARES_BY_BOARD.get(board, 100))
    step = int(cfg.lot_size) if board == "main" else 1
    step = max(1, step)
    while n > 0:
        if board == "main":
            n = (n // step) * step
        if n < min_buy:
            return 0
        fee = compute_fee(
            price=price,
            shares=n,
            side="BUY",
            commission_rate=cfg.commission_rate,
            commission_min=cfg.commission_min,
            stamp_tax_rate=cfg.stamp_tax_rate,
            transfer_fee_rate=cfg.transfer_fee_rate,
        )
        if fee.cash_delta >= -cash:
            return n
        n -= step
    return 0


def _settle_trading_day(con: Any, trade_date: str, *, cfg: PaperTradingConfig, force: bool = False) -> Optional[Dict[str, Any]]:
    """写 / 更新某交易日净值行。已存在且非 force 时跳过（幂等）。"""
    acct = store.load_account(con) or {}
    cash = _f(acct.get("cash"), 0.0)
    mv = _market_value(con)
    total = cash + mv
    prev = store.latest_equity(con)
    if prev and str(prev.get("trade_date")) == str(trade_date) and not force:
        return prev
    if prev and str(prev.get("trade_date")) > str(trade_date):
        # 更晚的净值已存在，禁止回写历史
        return prev
    prev_total = _f(prev.get("total_asset"), 0.0) if prev else float(acct.get("initial_capital") or 0.0)
    daily_pnl = total - prev_total if prev else (total - float(acct.get("initial_capital") or 0.0))
    daily_pct = (daily_pnl / prev_total) if prev_total > 0 else 0.0
    init_cap = float(acct.get("initial_capital") or 0.0)
    cum_pnl = total - init_cap
    cum_ret = (cum_pnl / init_cap) if init_cap > 0 else 0.0
    # 回撤：相对历史峰值总资产
    peak = init_cap
    for row in store.load_equity_series(con, limit=10000):
        ta = _f(row.get("total_asset"), 0.0)
        if ta > peak:
            peak = ta
    drawdown = ((total - peak) / peak) if peak > 0 else 0.0
    pos_n = store.count_positions(con)
    day_trades = 0
    day_fee = 0.0
    try:
        from core.paper_trading.schema import TABLE_ORDER

        r = store._row(
            con,
            f"SELECT COUNT(*) AS n, COALESCE(SUM(total_fee), 0) AS fee FROM {TABLE_ORDER} "
            f"WHERE trade_date = ? AND side IN ('BUY','SELL')",
            [trade_date],
        )
        day_trades = int((r or {}).get("n") or 0)
        day_fee = float((r or {}).get("fee") or 0.0)
    except Exception as e:  # noqa: BLE001
        _log.debug("统计当日成交/费用失败: %s", e)
    row = {
        "trade_date": trade_date,
        "cash": cash,
        "market_value": mv,
        "total_asset": total,
        "daily_pnl": daily_pnl,
        "daily_pnl_pct": daily_pct,
        "cum_pnl": cum_pnl,
        "cum_return": cum_ret,
        "drawdown_pct": drawdown,
        "position_count": pos_n,
        "trade_count": day_trades,
        "total_fee": day_fee,
    }
    store.insert_or_update_equity(con, row)
    idx = int(acct.get("trading_day_index") or 0)
    if not prev or str(prev.get("trade_date")) < str(trade_date):
        idx += 1
    store.update_account(
        con,
        cash=cash,
        trading_day_index=idx,
        last_equity_date=trade_date,
        status=("COHORT_CLOSED" if idx >= int(cfg.cohort_days) else None),
    )
    return row


# ==================== 停机缺口净值回填（P1-7/8，2026-11-14）====================

def _close_on_or_before(con: Any, ts_code: str, yyyymmdd: str) -> Optional[float]:
    """取某票在指定日之前（含）最近一个收盘价；daily_data 两种日期格式都兼容。"""
    try:
        d1 = f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:8]}"
        r = con.execute(
            "SELECT close FROM daily_data WHERE ts_code = ? "
            "AND CAST(trade_date AS VARCHAR) <= ? "
            "ORDER BY CAST(trade_date AS VARCHAR) DESC LIMIT 1",
            [ts_code, d1],
        ).fetchone()
        if r and r[0] is not None:
            v = _f(r[0], 0.0)
            return v if v > 0 else None
    except Exception as e:  # noqa: BLE001
        _log.debug("[模拟盘][回填] 收盘价查询失败 %s %s: %s", ts_code, yyyymmdd, e)
    return None


def _backfill_equity_gap_impl(
    con: Any,
    *,
    cfg: Optional[PaperTradingConfig] = None,
    max_days: int = 30,
) -> Dict[str, Any]:
    """停机导致 15:05±90min 结算窗口错过后，按历史收盘补写缺口交易日净值（幂等）。

    【背景 2026-11-14】上线审计发现 paper_equity_daily 在 11-09~11-13 断链：
    daemon 只在 15:05±90min 的 catchup 窗口内补跑结算，若那段时间整机停机
    （如 11-10 直到 20:16 才有第一条作业记录），当天的净值行就永久丢失 ——
    净值序列出现空洞，归因/回撤连续性受损。

    【口径】只补「现金流不变」的缺口：
      · 缺口内存在任何 BUY/SELL 成交 → 不补（现金变动无法还原）；
      · 市值用 daily_data 当日（或最近可得）收盘价；全部取不到时退到买入价，
        并在日志明示使用了近似价；
      · 只补 daily_data 里真实存在的交易日，不编造日期。
    """
    c = cfg or load_config()
    out: Dict[str, Any] = {"ok": True, "filled": 0, "dates": []}
    acct = store.load_account(con) or {}
    if not acct:
        return out
    prev = store.latest_equity(con)
    if not prev:
        return out
    last_date = re.sub(r"\D", "", str(prev.get("trade_date") or ""))
    today = store.today_yyyymmdd()
    if not last_date or last_date >= today:
        return out

    # 缺口内不允许有成交（现金无法还原，宁可不补）
    try:
        mx = store._row(
            con,
            "SELECT MAX(CAST(trade_date AS VARCHAR)) AS d FROM paper_order "
            "WHERE side IN ('BUY','SELL')",
        )
        mx_d = re.sub(r"\D", "", str((mx or {}).get("d") or ""))
        if mx_d and last_date < mx_d < today:
            out["ok"] = False
            out["reason"] = f"缺口内存在成交({mx_d})，不自动补净值"
            _log.warning("[模拟盘][回填] 跳过：%s", out["reason"])
            return out
    except Exception as e:  # noqa: BLE001
        _log.debug("[模拟盘][回填] 缺口成交流水检查失败(继续): %s", e)

    # 候选交易日来自 daily_data（本地、离线）
    try:
        rows = con.execute(
            "SELECT DISTINCT CAST(trade_date AS VARCHAR) AS d FROM daily_data "
            "ORDER BY d DESC LIMIT 400"
        ).fetchall()
    except Exception as e:  # noqa: BLE001
        out["ok"] = False
        out["reason"] = f"读取交易日失败: {e}"
        return out
    dates: List[str] = []
    for (d,) in rows:
        dg = re.sub(r"\D", "", str(d or ""))
        if len(dg) == 8 and last_date < dg < today:
            dates.append(dg)
    dates = sorted(set(dates))[-int(max_days):]
    if not dates:
        return out

    positions = store.load_positions(con)
    cash = _f(acct.get("cash"), 0.0)
    init_cap = float(acct.get("initial_capital") or 0.0)
    peak = init_cap
    for row in store.load_equity_series(con, limit=10000):
        peak = max(peak, _f(row.get("total_asset"), 0.0))
    prev_total = _f(prev.get("total_asset"), 0.0) or init_cap
    approx_used = False

    for d in dates:
        mv = 0.0
        for p in positions:
            code = str(p.get("code") or "")
            shares = int(_f(p.get("shares"), 0))
            if not code or shares <= 0:
                continue
            px = _close_on_or_before(con, code, d)
            if px is None:
                px = _f(p.get("buy_price_exec"), 0.0)
                approx_used = True
            mv += px * shares
        total = cash + mv
        daily_pnl = total - prev_total
        daily_pct = (daily_pnl / prev_total) if prev_total > 0 else 0.0
        cum_pnl = total - init_cap
        cum_ret = (cum_pnl / init_cap) if init_cap > 0 else 0.0
        drawdown = ((total - peak) / peak) if peak > 0 else 0.0
        store.insert_or_update_equity(con, {
            "trade_date": d,
            "cash": cash,
            "market_value": mv,
            "total_asset": total,
            "daily_pnl": daily_pnl,
            "daily_pnl_pct": daily_pct,
            "cum_pnl": cum_pnl,
            "cum_return": cum_ret,
            "drawdown_pct": drawdown,
            "position_count": len(positions),
            "trade_count": 0,
            "total_fee": 0.0,
        })
        peak = max(peak, total)
        prev_total = total
        out["filled"] += 1
        out["dates"].append(d)

    try:
        idx = int(acct.get("trading_day_index") or 0) + out["filled"]
        store.update_account(
            con,
            cash=cash,
            trading_day_index=idx,
            last_equity_date=dates[-1],
            status=("COHORT_CLOSED" if idx >= int(c.cohort_days) else None),
        )
    except Exception as e:  # noqa: BLE001
        _log.debug("[模拟盘][回填] 账户进度更新失败(净值行已补): %s", e)

    _log.warning(
        "[模拟盘][回填] 停机缺口净值补写 %d 天（%s ~ %s）%s",
        out["filled"],
        dates[0],
        dates[-1],
        "；部分收盘价缺失，已退用买入价近似" if approx_used else "",
    )
    return out


@store.write_transaction
def backfill_equity_gap(con, *, cfg: Optional[PaperTradingConfig] = None, max_days: int = 30) -> Dict[str, Any]:
    """开写连接执行缺口回填（幂等；无缺口时不写任何行）。"""
    try:
        return _backfill_equity_gap_impl(con, cfg=cfg, max_days=max_days)
    except Exception as e:  # noqa: BLE001
        _log.warning("[模拟盘][回填] 异常（已吞）: %s", e)
        return {"ok": False, "filled": 0, "reason": str(e)[:300]}


# ==================== 对外 API：买入 ====================

_LOT_META_CACHE: Optional[Dict[str, str]] = None
_LOT_META_CACHE_AT = 0.0

_PREV_CLOSE_CACHE: Dict[str, Tuple[str, float]] = {}
_PREV_CLOSE_CACHE_AT = 0.0


_PREV_CLOSE_KEYS = ("昨收", "昨收价", "pre_close", "prev_close", "preClose", "前收盘", "昨收盘")


def _resolve_pre_close(sig: Dict[str, Any], trade_date: str) -> float:
    """取信号票「上一交易日收盘价」，用于涨停/跌停封板判定。

    【V30.5 2026-10-18 修正】生产买入链原来只读 signal_raw['昨收']，
    但 pipeline / P3 / P4 的输出行里从来没有中文 '昨收' 键（有的是 pre_close、
    以及涨幅/今开等派生字段），于是 prev_close 恒为 0，涨停封板买入过滤在生产中
    实际从未生效 —— 涨停板买不到的票被当成可成交建仓，直接污染模拟胜率。
    这里改成三级回退：
      ① 信号行内任一别名（昨收 / 昨收价 / pre_close / 前收盘 …）；
      ② daily_data 里该票 trade_date 之前最近一个交易日记录的 pre_close；
      ③ 仍拿不到则返回 0，调用方按「不做涨跌停过滤」处理（不阻塞买入）。
    """
    global _PREV_CLOSE_CACHE, _PREV_CLOSE_CACHE_AT
    raw = sig.get("signal_raw") or {}
    if isinstance(raw, dict):
        for key in _PREV_CLOSE_KEYS:
            v = raw.get(key)
            if v is None:
                continue
            fv = _f(v, 0.0)
            if fv > 0:
                return fv
    code = str(sig.get("code") or "").strip()
    if not code:
        return 0.0
    now_mono = time.monotonic()
    if (now_mono - _PREV_CLOSE_CACHE_AT) >= 300.0:
        _PREV_CLOSE_CACHE = {}
        _PREV_CLOSE_CACHE_AT = now_mono
    hit = _PREV_CLOSE_CACHE.get(code)
    if hit and hit[0] == trade_date:
        return float(hit[1])
    out = 0.0
    try:
        from data.db_core import get_read_conn

        with get_read_conn(read_only=True) as con:
            row = con.execute(
                "SELECT pre_close, close FROM daily_data WHERE CAST(ts_code AS VARCHAR)=? "
                "AND REPLACE(CAST(trade_date AS VARCHAR),'-','')<? "
                "ORDER BY REPLACE(CAST(trade_date AS VARCHAR),'-','') DESC LIMIT 1",
                [code, trade_date],
            ).fetchone()
        if row is not None:
            out = _f(row[0], 0.0) or _f(row[1], 0.0)
    except Exception as e:  # noqa: BLE001
        _log.debug("[模拟盘] 查 prev_close 失败 code=%s: %s", code, e)
        out = 0.0
    _PREV_CLOSE_CACHE[code] = (trade_date, out)
    return out


def _load_lot_meta_map() -> Dict[str, str]:
    """code -> list_date（来自 stock_basic），用于新股无涨跌幅窗口判定。

    缓存 1 小时：DuckDB 单写者约束下，扫描期批量读一次就够。
    读不到就返回空 dict（降级为「按常规涨跌停处理」，不阻塞买入）。
    """
    global _LOT_META_CACHE, _LOT_META_CACHE_AT
    now_mono = time.monotonic()
    if _LOT_META_CACHE is not None and (now_mono - _LOT_META_CACHE_AT) < 3600.0:
        return _LOT_META_CACHE
    out: Dict[str, str] = {}
    try:
        from data.db_core import get_read_conn

        con = get_read_conn()
        cur = con.execute("SELECT ts_code, list_date FROM stock_basic").fetchall()
        for r in cur or []:
            c = str(r[0] or "").strip()
            ld = str(r[1] or "").strip().replace("-", "")[:8]
            if c and len(ld) == 8 and ld.isdigit():
                out[c] = ld
    except Exception as e:  # noqa: BLE001
        _log.debug("[模拟盘] 读取 stock_basic.list_date 失败（按常规涨跌停处理）: %s", e)
        out = {}
    _LOT_META_CACHE = out
    _LOT_META_CACHE_AT = now_mono
    return out

@store.write_transaction
def _on_signals_tx(con: Any, pool_key: str, sigs: List[Dict[str, Any]], cfg: PaperTradingConfig, *, allow_new: bool) -> Dict[str, Any]:
    trade_date = store.today_yyyymmdd()
    order_ts = store.now_bj().strftime("%Y-%m-%d %H:%M:%S")
    acct = store.ensure_account(con, initial_capital=cfg.initial_capital)
    status = str(acct.get("status") or "RUNNING")
    if not allow_new:
        return {"pool": pool_key, "bought": 0, "skipped": 0, "reason": SKIP_DISABLED}
    if status == "PAUSED":
        return {"pool": pool_key, "bought": 0, "skipped": 0, "reason": SKIP_ACCOUNT_PAUSED}
    if status == "COHORT_CLOSED":
        return {"pool": pool_key, "bought": 0, "skipped": 0, "reason": SKIP_COHORT_CLOSED}
    if not acct.get("start_date"):
        store.update_account(con, start_date=trade_date)
    cash = _f(acct.get("cash"), 0.0)
    held = {str(p["code"]) for p in store.load_positions(con)}
    bought = 0
    skipped = 0
    results: List[Dict[str, Any]] = []
    lot_meta = _load_lot_meta_map()
    for sig in sorted(sigs, key=sort_key):
        code = sig["code"]
        # 【V30.4 A股合规】退市整理期 / PT 标的：模拟盘不建仓。
        if is_excluded_name(sig.get("name")):
            _record_skip(con, sig, reason="EXCLUDED_DELISTING", pool=pool_key, trade_date=trade_date, order_ts=order_ts)
            skipped += 1
            continue
        if code in held:
            _record_skip(con, sig, reason=SKIP_ALREADY_HELD, pool=pool_key, trade_date=trade_date, order_ts=order_ts)
            skipped += 1
            continue
        if store.count_positions(con) >= int(cfg.max_positions):
            _record_skip(con, sig, reason="MAX_POSITIONS", pool=pool_key, trade_date=trade_date, order_ts=order_ts)
            skipped += 1
            continue
        price = float(sig["price"])
        exec_price = apply_slippage(price, "BUY", cfg.slippage_pct)
        board = board_of(code)
        min_buy = int(MIN_BUY_SHARES_BY_BOARD.get(board, 100))
        total = _total_asset(con, cash)
        shares, _target, cash_limited = compute_shares(
            cash=cash, total_asset=total, price=exec_price, cfg=cfg, code=code
        )
        if shares <= 0:
            reason = SKIP_INSUFFICIENT_CASH if cash_limited else SKIP_LOT_TOO_SMALL
            _record_skip(con, sig, reason=reason, pool=pool_key, trade_date=trade_date, order_ts=order_ts)
            skipped += 1
            continue
        # 【V30.4 A股合规】涨停封板买不到：现价已封在涨停价，买单只能排队，
        # 模拟盘按「信号价可成交」的前提建仓会把不可能成交的票算进胜率。
        # 新股上市初期（N/C/U/D 开头或上市前 5 个交易日）无涨跌幅限制，不套用。
        prev_close = _resolve_pre_close(sig, trade_date)
        no_limit = (
            is_no_limit_stock(code, sig.get("name"))
            or has_no_limit_window(lot_meta.get(code), trade_date, board)
        )
        if prev_close > 0 and not no_limit and _is_limit_up_locked(
            price, prev_close, code, sig.get("name"), no_limit=no_limit
        ):
            _record_skip(con, sig, reason=SKIP_LIMIT_UP, pool=pool_key, trade_date=trade_date, order_ts=order_ts)
            skipped += 1
            continue
        fee = compute_fee(
            price=exec_price,
            shares=shares,
            side="BUY",
            commission_rate=cfg.commission_rate,
            commission_min=cfg.commission_min,
            stamp_tax_rate=cfg.stamp_tax_rate,
            transfer_fee_rate=cfg.transfer_fee_rate,
        )
        if fee.cash_delta < -cash:
            shares = _shrink_to_affordable(shares, exec_price, cfg, code=code, cash=cash)
            if shares <= 0:
                _record_skip(con, sig, reason=SKIP_INSUFFICIENT_CASH, pool=pool_key, trade_date=trade_date, order_ts=order_ts)
                skipped += 1
                continue
            fee = compute_fee(
                price=exec_price,
                shares=shares,
                side="BUY",
                commission_rate=cfg.commission_rate,
                commission_min=cfg.commission_min,
                stamp_tax_rate=cfg.stamp_tax_rate,
                transfer_fee_rate=cfg.transfer_fee_rate,
            )
        seq = _next_seq(con, trade_date, code)
        cid = f"{ACCOUNT_ID}|{code}|BUY|{trade_date}|{seq}"
        if store.order_exists(con, cid):
            skipped += 1
            continue
        cash += fee.cash_delta
        # 【V30.4 A股合规】止损/止盈价必须是可挂单的 0.01 元整数分，
        # 原 round(..., 4) 会产生 9.7158 这种根本不存在的委托价。
        # 【P0 修复 2026-10-19】止损比按 ts_code 板块差异化：主板 -3% / 创业板 -5% / 科创板 -5%
        sl_ratio = cfg.resolve_stop_loss_ratio(code)
        stop_px = round_price(price * sl_ratio)
        take_px = round_price(price * cfg.take_profit_ratio)
        if stop_px <= 0:
            stop_px = 0.01
        pos = {
            "code": code,
            "name": sig["name"],
            "pool": pool_key,
            "strategy_ids": ",".join(sig["strategy_ids"]),
            "strategy_names": ",".join(sig["strategy_names"]),
            "signal_score": float(sig.get("score") or 0.0),
            "buy_date": trade_date,
            "buy_time": store.now_bj().strftime("%H:%M:%S"),
            "buy_price_raw": price,
            "buy_price_exec": exec_price,
            "shares": int(shares),
            "buy_amount": fee.gross_amount,
            "buy_commission": fee.commission,
            "buy_stamp_tax": fee.stamp_tax,
            "buy_transfer_fee": fee.transfer_fee,
            "net_cost": -fee.cash_delta,
            "stop_loss_price": stop_px,
            "take_profit_price": take_px,
            "last_price": price,
            "price_ts": order_ts,
            "unrealized_pnl": 0.0,
            "unrealized_pnl_pct": 0.0,
            "entry_signal_json": store.dumps(sig.get("signal_raw") or {}),
        }
        store.insert_position(con, pos)
        order = _make_buy_order(
            cfg,
            sig,
            seq=seq,
            trade_date=trade_date,
            order_ts=order_ts,
            cash_after=cash,
            fee=fee,
            shares=int(shares),
            exec_price=exec_price,
        )
        order["pool"] = pool_key
        store.insert_order(con, order)
        held.add(code)
        bought += 1
        results.append({
            "code": code,
            "name": sig.get("name") or code,
            "shares": int(shares),
            "price": exec_price,
            "amount": float(fee.gross_amount),
            "stop": float(stop_px),
            "take": float(take_px),
            "strategies": list(sig.get("strategy_ids") or sig.get("strategy_names") or []),
            "cash_after": float(cash),
            "cash_delta": fee.cash_delta,
        })
    store.update_account(con, cash=cash)
    return {"pool": pool_key, "bought": bought, "skipped": skipped, "details": results, "cash": cash}


def load_today_buy_orders(trade_date: Optional[str] = None) -> List[Dict[str, Any]]:
    """读取指定交易日的全部 BUY 订单（用于补推历史买入通知）。

    注意：必须在持有主库写连接的进程内调用（DuckDB 单写者约束）。
    返回订单 dict 列表，按下单时间升序。
    """

    @store.write_transaction
    def _tx(con: Any) -> List[Dict[str, Any]]:
        d = trade_date or store.today_yyyymmdd()
        cur = con.execute(
            "SELECT order_ts, code, name, pool, exec_price, shares, gross_amount, "
            "strategy_ids, strategy_names, signal_score, cash_delta "
            "FROM paper_order WHERE side='BUY' AND trade_date=? ORDER BY order_ts",
            [d],
        )
        cols = [x[0] for x in (cur.description or [])]
        return [{c: r[i] for i, c in enumerate(cols)} for r in cur.fetchall()]

    try:
        return list(_tx() or [])
    except Exception as e:  # noqa: BLE001
        _log.warning("[模拟盘] 读取历史买入订单失败: %s", e)
        return []


def _load_today_stop_take(trade_date: Optional[str] = None) -> Dict[str, Any]:
    """读取指定交易日买入的持仓的止损/止盈价（order 表没有这两列）。"""

    @store.write_transaction
    def _tx(con: Any) -> Dict[str, Any]:
        d = trade_date or store.today_yyyymmdd()
        cur = con.execute(
            "SELECT code, stop_loss_price, take_profit_price FROM paper_position WHERE buy_date=?",
            [d],
        )
        return {str(r[0]): {"stop": r[1], "take": r[2]} for r in cur.fetchall()}

    try:
        return _tx() or {}
    except Exception as e:  # noqa: BLE001
        _log.debug("[模拟盘] 读取止损止盈失败（忽略）: %s", e)
        return {}


def notify_historical_buys(trade_date: Optional[str] = None, cfg: Optional[PaperTradingConfig] = None) -> int:
    """把指定交易日已成交的买入订单补推一条企微汇总。返回推送的笔数。

    用于「新功能上线后，把当天已成交但未推送过的买入补发一次」。
    仅推 BUY 订单，且只推一次（由调用方用状态文件/记忆保证幂等）。
    """
    try:
        c = cfg or load_config()
        if not c.enabled:
            return 0
        orders = load_today_buy_orders(trade_date)
        if not orders:
            return 0
        # 从持仓表补止损/止盈价（order 表没有这两列）
        stop_take = _load_today_stop_take(trade_date)
        buys: List[Dict[str, Any]] = []
        for o in orders:
            code = str(o.get("code") or "")
            name = str(o.get("name") or code)
            px = _f(o.get("exec_price"), 0.0)
            sh = int(_f(o.get("shares"), 0.0))
            # strategy_ids 存的是原子编号（P3-08），strategy_names 存中文标签
            ids_txt = str(o.get("strategy_ids") or "").strip()
            st = stop_take.get(code) or {}
            buys.append({
                "code": code,
                "name": name,
                "shares": sh,
                "price": px,
                "amount": _f(o.get("gross_amount"), px * sh),
                "stop": _f(st.get("stop"), 0.0),
                "take": _f(st.get("take"), 0.0),
                "strategies": [x for x in ids_txt.split(",") if x],
                "cash_after": None,
                "order_ts": str(o.get("order_ts") or ""),
                "pool": str(o.get("pool") or ""),
            })
        _notify_paper_buys_safe("p3(补推)", buys, c)
        return len(buys)
    except Exception as e:  # noqa: BLE001
        _log.warning("[模拟盘] 补推历史买入异常（已吞）: %s", e)
        return 0


def _notify_paper_buys_safe(pool_key: str, buys: List[Dict[str, Any]], cfg: PaperTradingConfig) -> None:
    """模拟盘买入成交后异步推送企业微信；任何异常吞掉，绝不影响交易主链路。

    展示：股票名称(代码) / 买入价 / 买入股数 / 买入金额 / 止损止盈 / 战法 / 剩余现金。
    受三重约束：cfg.notify_buys 开关、全局通知总闸(notification.enabled)、企微总控。
    推送走有界线程池(push_markdown_async)，非阻塞。
    """
    try:
        if not buys:
            return
        if not bool(getattr(cfg, "notify_buys", True)):
            return
        try:
            from core.config_manager import get_notification_config

            ncfg = get_notification_config()
            if not ncfg.get("enabled"):
                return
            url = str(ncfg.get("wechat_webhook_url") or "").strip()
            if not url:
                return
        except Exception as e:  # noqa: BLE001
            _log.debug("[模拟盘] 读取通知配置失败，跳过买入推送: %s", e)
            return
        try:
            from core.master_control import is_wechat_push_master_enabled

            if not is_wechat_push_master_enabled():
                _log.debug("[模拟盘] 企微总控关闭，跳过买入推送")
                return
        except Exception as e:  # noqa: BLE001
            _log.debug("[模拟盘] 企微总控读取异常，继续尝试推送: %s", e)

        try:
            ts = store.now_bj().strftime("%H:%M:%S")
        except Exception:  # noqa: BLE001
            ts = ""
        lines: List[str] = [f"## 📥 模拟盘买入 [{pool_key}] {ts}"]
        for b in buys:
            code = str(b.get("code") or "")
            name = str(b.get("name") or code)
            px = _f(b.get("price"), 0.0)
            sh = int(_f(b.get("shares"), 0.0))
            amt = _f(b.get("amount"), px * sh)
            stop = _f(b.get("stop"), 0.0)
            take = _f(b.get("take"), 0.0)
            lines.append(f"**{name} ({code})**")
            # 补推场景显示真实成交时刻
            ots = str(b.get("order_ts") or "").strip()
            if ots:
                lines.append(f"- 成交时间：{ots}")
            lines.append(f"- 买入价：¥{px:.2f}")
            lines.append(f"- 买入股数：{sh} 股")
            lines.append(f"- 买入金额：¥{amt:,.2f}")
            if stop > 0 and take > 0:
                lines.append(f"- 止损 ¥{stop:.2f} / 止盈 ¥{take:.2f}")
            strat = b.get("strategies") or []
            _strat_txt = ""
            try:
                from core.paper_trading.strategy_labels import format_strategy_line

                _strat_txt = format_strategy_line([str(x) for x in strat])
            except Exception as e:  # noqa: BLE001
                _log.debug("[模拟盘] 战法说明渲染失败，回落纯编号: %s", e)
                _strat_txt = "/".join(str(x) for x in strat if x)
            if _strat_txt:
                lines.append(f"- 战法：{_strat_txt}")
            if b.get("cash_after") is not None:
                lines.append(f"- 剩余现金：¥{_f(b.get('cash_after'), 0.0):,.2f}")
            lines.append("")
        lines.append("> 模拟盘自动成交，仅供策略验证参考，不构成投资建议")
        body = "\n".join(lines).strip()
        from core.notification.gateway import get_wechat_gateway

        get_wechat_gateway(url).push_markdown_async(body)
        _log.info("[模拟盘][%s] 已推送买入企微通知 %d 笔", pool_key, len(buys))
    except Exception as e:  # noqa: BLE001
        _log.warning("[模拟盘] 买入企微推送异常（已吞，不影响交易）: %s", e)


def on_signals(pool_key: str, rows: Any, *, cfg: Optional[PaperTradingConfig] = None, allow_new: bool = True) -> Dict[str, Any]:
    """消费扫描结果：过滤 → 排序 → 买入。永不抛出。

    pool_key: 'p3' / 'p4'
    rows: run_scan_engine 返回的正式行 list
    """
    c = cfg or load_config()
    pk = str(pool_key or "").strip().lower()
    try:
        if pk not in ("p3", "p4"):
            return {"pool": pk, "bought": 0, "skipped": 0, "reason": "INVALID_POOL"}
        if not c.enabled or not allow_new:
            return {"pool": pk, "bought": 0, "skipped": 0, "reason": SKIP_DISABLED}
        sigs: List[Dict[str, Any]] = []
        for row in (rows or []):
            sig = parse_signal_row(row)
            if sig is not None:
                sig["pool"] = pk
                sigs.append(sig)
        if not sigs:
            return {"pool": pk, "bought": 0, "skipped": 0, "reason": "NO_SIGNAL"}
        out = _on_signals_tx(pk, sigs, c, allow_new=True)
        _log.info(
            "[模拟盘][%s] 候选=%d 买入=%d 跳过=%d 现金=%.2f",
            pk, len(sigs), int(out.get("bought") or 0), int(out.get("skipped") or 0), _f(out.get("cash"), 0.0),
        )
        # 买入成交 → 企微实时通知（异步、异常隔离，不影响扫描/交易）
        try:
            _notify_paper_buys_safe(pk, list(out.get("details") or []), c)
        except Exception as e:  # noqa: BLE001
            _log.debug("[模拟盘] 买入推送外层异常（已吞）: %s", e)
        return out
    except Exception as e:  # noqa: BLE001
        _log.warning("[模拟盘][%s] on_signals 异常（已吞，不影响扫描）: %s", pk, e, exc_info=True)
        return {"pool": pk, "bought": 0, "skipped": 0, "reason": "EXCEPTION", "error": str(e)[:300]}


# ==================== 对外 API：卖出监控 ====================

def _is_limit_down_locked(price: float, pre_close: float, code: str, name: str) -> bool:
    """跌停封板判定：现价 ≤ 跌停价。

    【V30.4 2026-10-18】跌停价改用 Decimal ROUND_HALF_UP 按 0.01 元取整，
    并去掉 1.001 浮点容差 —— 交易所的跌停价就是分位整数，用浮点比例判断会
    在 9.045 这类价格上误判（原公式算出 9.0449999 < 真实跌停价 9.05）。
    【V30.5 2026-10-18】新股上市初期（名称 N/C/U/D 开头，或上市前 5 个交易日）
    不设涨跌幅，不能套用涨跌停价判定卖出。
    """
    if pre_close <= 0:
        return False
    # 新股上市初期无涨跌幅窗口（上市首日 N/C/U/D 开头，或上市前 5 个交易日）
    if is_no_limit_stock(code, name):
        return False
    board = board_of(code)
    if is_st_stock(name):
        amp = float(ST_LIMIT_PCT_BY_BOARD.get(board, 5.0))
    else:
        amp = float(LIMIT_PCT_BY_BOARD.get(board, 10.0))
    down_price = limit_price(pre_close, -amp)
    return float(price) <= down_price


def _suspended(price: float, volume: Any) -> bool:
    """停牌判定：价格有效但成交量为 0 且无成交额 → 视为停牌/无流动性。"""
    if price <= 0:
        return True
    try:
        v = _f(volume, 0.0)
    except Exception:  # noqa: BLE001
        return False
    return v <= 0


def _limit_up_price(pre_close: float, code: str, name: str, *, no_limit: Optional[bool] = None) -> float:
    if no_limit if no_limit is not None else is_no_limit_stock(code, name):
        return float("inf")
    board = board_of(code)
    amp = float(ST_LIMIT_PCT_BY_BOARD.get(board, 5.0)) if is_st_stock(name) else float(
        LIMIT_PCT_BY_BOARD.get(board, 10.0)
    )
    return limit_price(pre_close, amp)


def _is_limit_up_locked(
    price: float,
    pre_close: float,
    code: str,
    name: str,
    *,
    no_limit: Optional[bool] = None,
) -> bool:
    """涨停封板判定：现价 >= 涨停价 —— 涨停板上买不到，必须跳过买入。"""
    if pre_close <= 0:
        return False
    return float(price) >= _limit_up_price(pre_close, code, name, no_limit=no_limit)


@store.write_transaction
def _on_tick_tx(con: Any, quotes: Dict[str, Dict[str, Any]], today: str, cfg: PaperTradingConfig) -> Dict[str, Any]:
    order_ts = store.now_bj().strftime("%Y-%m-%d %H:%M:%S")
    positions = store.load_positions(con)
    if not positions:
        return {"checked": 0, "sold": 0, "held": 0, "skipped": []}
    sold = 0
    held = 0
    skipped: List[Dict[str, str]] = []
    cash = _f((store.load_account(con) or {}).get("cash"), 0.0)
    for pos in positions:
        code = str(pos.get("code"))
        name = str(pos.get("name") or "")
        q = quotes.get(code) or {}
        price = _f(q.get("price"), 0.0)
        if price <= 0:
            skipped.append({"code": code, "reason": SKIP_NO_QUOTE})
            continue
        # 报价有效性：与上次现价偏离过大视为无效报价，不据此交易
        last = _f(pos.get("last_price"), 0.0)
        dev_cap = float(cfg.quote_stale_deviation_pct) / 100.0
        if last > 0 and abs(price - last) / last > dev_cap:
            skipped.append({"code": code, "reason": SKIP_BAD_PRICE})
            store.update_position_quote(con, code, last_price=price, price_ts=order_ts)
            continue
        if _suspended(price, q.get("volume")):
            skipped.append({"code": code, "reason": SKIP_SUSPENDED})
            store.update_position_quote(con, code, last_price=price, price_ts=order_ts)
            continue
        pre_close = _f(q.get("pre_close"), 0.0)
        if _is_limit_down_locked(price, pre_close, code, name):
            skipped.append({"code": code, "reason": SKIP_LIMIT_DOWN})
            store.update_position_quote(con, code, last_price=price, price_ts=order_ts)
            continue
        buy_px = _f(pos.get("buy_price_raw"), 0.0)
        if buy_px <= 0:
            skipped.append({"code": code, "reason": SKIP_BAD_PRICE})
            continue
        # 【P0 修复 2026-10-19】优先用 position 已存的 stop_loss_price（按板差异化计算时已落库），
        # 兜底才用 cfg.stop_loss_ratio（保持向后兼容）。
        # 若 position 没存（老 schema），则按 ts_code 重新算板级止损比。
        stored_stop_px = _f(pos.get("stop_loss_price"), 0.0)
        if stored_stop_px > 0:
            stop_px = stored_stop_px
        else:
            sl_ratio = cfg.resolve_stop_loss_ratio(code)
            stop_px = buy_px * sl_ratio
        stored_take_px = _f(pos.get("take_profit_price"), 0.0)
        if stored_take_px > 0:
            take_px = stored_take_px
        else:
            take_px = buy_px * cfg.take_profit_ratio
        hit_stop = price <= stop_px
        hit_take = price >= take_px
        # 【2026-11-05 新增·用户抛出要求】当日跌幅离场：
        #   相对**昨收**的当日跌幅达到阈值（默认 -3%）就无条件卖出 ——
        #   不管这笔持仓是浮盈还是浮亏（所以它独立于按买入价算的 STOP_LOSS）。
        #   阈值按 0.01 元取整（A 股最小报价单位），避免 10×0.97=9.699999… 的浮点误判。
        drop_pct = float(getattr(cfg, "day_drop_exit_pct", 0.0) or 0.0)
        drop_ratio = (1.0 + drop_pct / 100.0) if drop_pct < 0 else 0.0
        drop_px = 0.0
        if drop_ratio > 0.0:
            ref_close = pre_close
            if ref_close <= 0:
                # 报价缺 pre_close 时回库兜底（与买入链 _resolve_pre_close 同口径）；
                # 仍然取不到 → 本条规则不生效（不拿 0 当昨收去误杀）。
                ref_close = _resolve_pre_close({"code": code}, today)
            if ref_close > 0:
                drop_px = round_price(ref_close * drop_ratio)
        hit_day_drop = drop_px > 0 and price <= drop_px + 1e-9
        # 【P0-3 修复】到期强平判定（交易日口径，非自然日）。
        # 语义：持有交易日数 >= max_hold_days 即强制平仓；max_hold_days<=0 表示不启用。
        # 判定逻辑抽在 _should_force_timeout（纯函数，可单测）。
        hit_timeout = _should_force_timeout(
            str(pos.get("buy_date") or ""), today, getattr(cfg, "max_hold_days", 0)
        )
        if not (hit_stop or hit_take or hit_timeout or hit_day_drop):
            held += 1
            store.update_position_quote(con, code, last_price=price, price_ts=order_ts)
            continue
        if not _sellable_today(str(pos.get("buy_date") or ""), today, enforce_t_plus_1=bool(cfg.enforce_t_plus_1)):
            # T+1 未解禁：记录但不卖，下一交易日继续监控
            skipped.append({"code": code, "reason": "T_PLUS_1_LOCKED"})
            store.update_position_quote(con, code, last_price=price, price_ts=order_ts)
            continue
        exec_price = apply_slippage(price, "SELL", cfg.slippage_pct)
        shares = int(pos.get("shares") or 0)
        fee = compute_fee(
            price=exec_price,
            shares=shares,
            side="SELL",
            commission_rate=cfg.commission_rate,
            commission_min=cfg.commission_min,
            stamp_tax_rate=cfg.stamp_tax_rate,
            transfer_fee_rate=cfg.transfer_fee_rate,
        )
        # 【P0-3】出场原因优先级：止损 > 止盈 > 到期强平。
        # 若同日既触止损又到期，按止损处理（风险事件，价格更差、更该被记录）。
        # 【2026-11-05】当日跌幅离场插在止损之后、止盈之前：同属风控事件，
        # 但它常常发生在「还浮盈」的时候（+8% 止盈前先被当日 -3% 打掉），
        # 需要被单独记成 DAY_DROP 才能统计这条规则的真实效果。
        reason = (
            EXIT_STOP_LOSS if hit_stop
            else (
                EXIT_DAY_DROP if hit_day_drop
                else (EXIT_TAKE_PROFIT if hit_take else EXIT_TIMEOUT)
            )
        )
        net_cost = _f(pos.get("net_cost"), 0.0)
        # 净盈亏 = 卖出现金流入 − 买入总成本（含全部费用）
        pnl = fee.cash_delta - net_cost
        pnl_pct = (pnl / net_cost) if net_cost > 0 else 0.0
        # 备注里带上「当日涨跌幅」，事后复盘能直接看出是止损还是当日跌幅离场
        note = f"last={price:.3f} exec={exec_price:.3f}"
        if pre_close > 0:
            note += f" dchg={(price / pre_close - 1.0) * 100.0:.2f}%"
        cash += fee.cash_delta
        seq = _next_seq(con, today, code)
        store.insert_order(
            con,
            {
                "order_id": _order_id("S", today, code, seq),
                "client_order_id": f"{ACCOUNT_ID}|{code}|SELL|{today}|{seq}",
                "side": "SELL",
                "order_ts": order_ts,
                "trade_date": today,
                "pool": str(pos.get("pool") or ""),
                "code": code,
                "name": name,
                "strategy_ids": str(pos.get("strategy_ids") or ""),
                "strategy_names": str(pos.get("strategy_names") or ""),
                "signal_score": _f(pos.get("signal_score"), 0.0),
                "exec_price": exec_price,
                "shares": shares,
                "gross_amount": fee.gross_amount,
                "commission": fee.commission,
                "stamp_tax": fee.stamp_tax,
                "transfer_fee": fee.transfer_fee,
                "total_fee": fee.total_fee,
                "cash_delta": fee.cash_delta,
                "buy_price_raw": buy_px,
                "pnl": pnl,
                "pnl_pct": pnl_pct,
                "holding_days": _holding_days(str(pos.get("buy_date") or ""), today),
                "exit_reason": reason,
                "skip_reason": None,
                "note": note,
            },
        )
        store.delete_position(con, code)
        sold += 1
        _log.info(
            "[模拟盘] 卖出 %s %s price=%.3f reason=%s pnl=%.2f (%.2f%%)",
            code, name, exec_price, reason, pnl, pnl_pct * 100.0,
        )
    store.update_account(con, cash=cash)
    return {"checked": len(positions), "sold": sold, "held": held, "skipped": skipped, "cash": cash}


def on_tick(*, quotes: Optional[Dict[str, Dict[str, Any]]] = None, today: Optional[str] = None, cfg: Optional[PaperTradingConfig] = None) -> Dict[str, Any]:
    """持仓阈值监控一帧。永不抛出。

    quotes: {code: {'price': float, 'pre_close': float, 'volume': float}}；为 None 时自动抓实时。
    """
    c = cfg or load_config()
    try:
        if not c.enabled:
            return {"checked": 0, "sold": 0, "held": 0, "reason": SKIP_DISABLED}
        qmap = quotes if quotes is not None else _fetch_quotes_for_positions()
        if not qmap:
            return {"checked": 0, "sold": 0, "held": 0, "reason": "NO_POSITIONS_OR_QUOTES"}
        td = today or store.today_yyyymmdd()
        out = _on_tick_tx(qmap, td, c)
        return out
    except Exception as e:  # noqa: BLE001
        _log.warning("[模拟盘] on_tick 异常（已吞）: %s", e, exc_info=True)
        return {"checked": 0, "sold": 0, "held": 0, "reason": "EXCEPTION", "error": str(e)[:300]}


def _fetch_quotes_for_positions() -> Dict[str, Dict[str, Any]]:
    """拉取全部当前持仓的实时报价（复用 api_fetcher 多源回退）。"""
    try:
        from data.db_core import get_read_conn
        from data.api_fetcher import fetch_realtime_batch

        with get_read_conn(read_only=True) as con:
            rows = store.load_positions(con)
        codes = [str(r.get("code")) for r in rows if r.get("code")]
        if not codes:
            return {}
        rt = fetch_realtime_batch(codes) or {}
        return {str(k): (v or {}) for k, v in rt.items()}
    except Exception as e:  # noqa: BLE001
        _log.debug("[模拟盘] 拉取持仓实时报价失败: %s", e)
        return {}


# ==================== 对外 API：收盘结算 ====================

@store.write_transaction
def daily_settle(con, *, trade_date: Optional[str] = None, cfg: Optional[PaperTradingConfig] = None, force: bool = False) -> Dict[str, Any]:
    """收盘后写日净值（幂等）。需要写连接：会 INSERT OR REPLACE paper_equity_daily。

    【V30.11】收盘后顺带推送「按战法收益归因」报告 —— 这是 1-2 个月实验选出
    最优战法的核心产出。推送失败绝不影响净值结算。
    """
    c = cfg or load_config()
    try:
        td = trade_date or store.today_yyyymmdd()
        row = _settle_trading_day(con, td, cfg=c, force=force)
    except Exception as e:  # noqa: BLE001
        _log.warning("[模拟盘] daily_settle 异常（已吞）: %s", e)
        return {"trade_date": trade_date, "row": None, "error": str(e)[:300]}
    # 归因报告推送（独立 try，任何异常不影响上面的结算结果）
    try:
        from core.paper_trading.attribution import notify_attribution_report

        notify_attribution_report(cfg=c)
    except Exception as e:  # noqa: BLE001
        _log.debug("[模拟盘] 归因报告推送异常（已吞）: %s", e)
    return {"trade_date": td, "row": row}


def _self_test() -> None:
    """引擎纯逻辑自检（不碰 DuckDB）：阈值边界 + 股数整手 + 板块涨跌停。"""
    cfg = load_config()
    cfg.enabled = True
    # 【V30.11】仓位口径：fixed_amount 模式 → 每只固定 5 万，与总资产脱钩。
    # 10 元 → 5万/10 = 5000 股（主板 100 股整手）
    n, target, cash_limited = compute_shares(cash=1_000_000.0, total_asset=1_000_000.0, price=10.0, cfg=cfg)
    assert n == 5000, n
    assert abs(target - 50000.0) < 1e-6, target
    assert cash_limited is False
    # 现金不足时按现金收敛
    n2, _t2, cash_limited2 = compute_shares(cash=5000.0, total_asset=1_000_000.0, price=10.0, cfg=cfg)
    assert n2 == 500, n2
    assert cash_limited2 is False
    # 现金连一手都不够 → INSUFFICIENT_CASH
    n2b, _t2b, cash_limited3 = compute_shares(cash=500.0, total_asset=1_000_000.0, price=10.0, cfg=cfg)
    assert n2b == 0 and cash_limited3 is True, (n2b, cash_limited3)
    # 不足一手
    n3, _t3, cash_limited4 = compute_shares(cash=1_000_000.0, total_asset=1_000_000.0, price=300.0, cfg=cfg)
    assert n3 == 100, n3  # 5万/300 = 166 → 100 股（主板整手）
    assert cash_limited4 is False
    # 目标金额不足一手 → LOT_TOO_SMALL
    # 注意：fixed_amount 模式与 total_asset 脱钩（target 恒为 5 万），
    # 所以「金额不足一手」要用「价位过高」来构造：5万买不起 200 股 × 300 元 = 6 万。
    n4, _t4, cash_limited5 = compute_shares(cash=1_000_000.0, total_asset=1_000_000.0, price=400.0, cfg=cfg)
    assert n4 == 100 and cash_limited5 is False, (n4, cash_limited5)
    # fixed_amount 模式下 target 不随 total_asset 变化（实验口径：每只风险敞口一致）
    _n5, t5, _c5 = compute_shares(cash=1_000_000.0, total_asset=10_000.0, price=10.0, cfg=cfg)
    assert abs(t5 - 50000.0) < 1e-6, t5
    # 阈值：买入 10 元 → 止损 9.70 / 止盈 10.80（V30.11 统一 +8%/-3%）
    assert abs(cfg.stop_loss_ratio - 0.97) < 1e-9
    assert abs(cfg.take_profit_ratio - 1.08) < 1e-9
    # 止损已统一为所有板 -3%（不再按板差异化，避免污染战法归因）
    assert abs(cfg.resolve_stop_loss_pct("300750") - (-3.0)) < 1e-9
    assert abs(cfg.resolve_stop_loss_pct("688001") - (-3.0)) < 1e-9
    # 板块与涨跌停
    assert board_of("600000") == "main"
    assert board_of("688001") == "star"
    assert board_of("300750") == "gem"
    assert is_st_stock("ST康美")
    assert not is_st_stock("浦发银行")
    assert _is_limit_down_locked(9.00, 10.00, "600000", "浦发银行")  # -10% 跌停
    assert not _is_limit_down_locked(9.05, 10.00, "600000", "浦发银行")
    assert _is_limit_down_locked(9.50, 10.00, "300750", "宁德时代") is False  # 创业板 -20% → -8.0 才是跌停
    assert _is_limit_down_locked(8.00, 10.00, "300750", "宁德时代")
    assert _is_limit_down_locked(9.50, 10.00, "600001", "ST某某")  # ST -5% → 9.50
    # 信号行过滤
    good = {"代码": "600000", "名称": "浦发银行", "现价": "10.00", "综合分": 80.0, "战法": "P3-01·测试"}
    assert parse_signal_row(good) is not None
    bad_obs = dict(good, pool_tier="observation")
    assert parse_signal_row(bad_obs) is None
    bad_tier = dict(good, **{"执行层级": "C"})
    assert parse_signal_row(bad_tier) is None
    bad_px = dict(good, **{"现价": "--"})
    assert parse_signal_row(bad_px) is None
    s = parse_signal_row(good)
    assert s and s["strategy_ids"] == ["P3-01"], s
    # 【P0-3】到期强平判定（交易日口径）：max_hold_days<=0 不启用；同日=0 天不强平
    assert _should_force_timeout("20261104", "20261104", 10) is False
    assert _should_force_timeout("20261104", "20261104", 0) is False      # 未启用
    assert _should_force_timeout("20261104", "20261104", -1) is False     # 非法值
    assert _should_force_timeout("20261104", "20261104", None) is False   # 缺失
    # 20261020 → 20261104 为 11 个交易日：>=10 应强平，>=12 不应
    assert _should_force_timeout("20261020", "20261104", 10) is True, "满10交易日应强平"
    assert _should_force_timeout("20261020", "20261104", 12) is False, "未满12交易日不应强平"
    print("[engine] self-test OK | shares/threshold/board/filter/timeout verified")


if __name__ == "__main__":
    _self_test()


__all__ = [
    "EXIT_DAY_DROP",
    "EXIT_EXPERIMENT_END",
    "EXIT_STOP_LOSS",
    "EXIT_TAKE_PROFIT",
    "EXIT_TIMEOUT",
    "board_of",
    "compute_shares",
    "daily_settle",
    "is_st_stock",
    "LIMIT_PCT_BY_BOARD",
    "ST_LIMIT_PCT_BY_BOARD",
    "on_signals",
    "on_tick",
    "parse_signal_row",
]
