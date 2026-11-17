# -*- coding: utf-8 -*-
"""
core/paper_trading/config.py — 模拟盘配置读取

【职责】
从项目根 config.yaml 的 paper_trading 段读取模拟盘参数，并提供安全默认值。

【设计原则】
  · 配置缺失 / 解析失败 → 全部回落 DEFAULT，绝不让模拟盘因配置问题崩掉守护进程。
  · 费率口径（A 股市场通行值，落地前建议再核一次官方公告）：
      - 印花税 0.05%：仅卖出单边征收。
      - 券商佣金 0.03%：买卖双边，按券商实际费率配置，此处为默认值。
      - 过户费 0.001%：买卖双边按成交额收取。
      - 证管费 / 经手费：已包含在券商佣金内，不重复计。
  · 时区固定 Asia/Shanghai；交易日历由主库 dim_trade_calendar 提供。
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

_log = logging.getLogger(__name__)


DEFAULT_CONFIG: Dict[str, Any] = {
    "enabled": False,
    "initial_capital": 1000000.0,
    # 【V30.11 策略优选实验 2026-11-04】仓位模式改造
    #   旧：position_pct（占总资产比例），单票 = 总资产 × position_pct
    #   新：fixed_amount_per_position（固定金额），单票 = 固定金额，与总资产脱钩
    #      → 20 只 × 5 万 = 100 万，每只风险敞口一致，便于按战法横向比较收益
    "position_mode": "fixed_amount",   # fixed_amount | pct
    "fixed_amount_per_position": 50000.0,
    "position_pct": 0.20,
    "max_positions": 20,
    # 【V30.11】止盈止损统一为 +8% / -3%（不再按板差异化）：
    #   实验目的是「同口径比较各战法胜率与盈亏比」，按板差异化止损会引入
    #   额外变量（创业板/科创板止损更宽 → 天然盈亏更好），污染战法归因。
    "stop_loss_pct": -3.0,
    "take_profit_pct": 8.0,
    # 【2026-11-05 新增】当日跌幅离场：相对**昨收**的当日跌幅达到该阈值即无条件卖出，
    #   与你持仓成本盈亏无关（浮盈状态也跑）。
    #   与 stop_loss_pct 的区别：止损看的是「买入价」，这条看的是「今天的跌幅」。
    #   负数 = 启用（-3.0 = 当日跌 3% 就跑）；0 = 关闭。
    "day_drop_exit_pct": -3.0,
    "stop_loss_by_board": {
        "main": -3.0,
        "chi_next": -3.0,
        "star": -3.0,
    },
    "enforce_t_plus_1": True,
    "lot_size": 100,
    # 【V30.12 2026-11-04 执行成本 realism】
    # 旧值 0.0 = 假设「信号价就是成交价」，比现实乐观，会系统性高估策略收益。
    # A 股小盘股实盘买卖价差约 0.1%（买一/卖一 + 冲击成本），取 0.1% 作为保守值。
    # 后果：模拟盘收益会比之前低一点 —— 这是真实，不是变差。
    # 详见 core/stop_rule_backtest.py（同一滑点口径用于回测与模拟盘）。
    "slippage_pct": 0.1,
    # 【V30.12】最长持有交易日数：超过则按收盘价强平。
    # 这是回测与模拟盘必须共用的参数（否则测的和跑的不是同一套规则）。
    "max_hold_days": 10,
    "cohort_days": 100,
    "monitor_interval_sec": 30,
    "quote_stale_deviation_pct": 20.0,
    # 【买入企微推送】模拟盘每成交一笔买入，异步推送到企业微信（名称/买入价/股数/金额/止损止盈）。
    # 受全局通知总闸(notification.enabled)与企微总控(is_wechat_push_master_enabled)双重约束；
    # 置 False 可单独关闭"买入推送"而保留其它推送。
    "notify_buys": True,
    "fee": {
        "commission_rate": 0.0003,
        "commission_min": 5.0,
        "stamp_tax_rate": 0.0005,
        "transfer_fee_rate": 0.00001,
    },
}


def _load_yaml_section() -> Dict[str, Any]:
    """读取 config.yaml 顶层 dict 中的 paper_trading 段。"""
    try:
        import yaml  # type: ignore
    except ImportError:
        _log.debug("PyYAML 缺失，模拟盘配置回落默认值")
        return {}
    here = os.path.abspath(os.path.dirname(__file__))
    project_root = os.path.abspath(os.path.join(here, "..", ".."))
    path = os.path.join(project_root, "config.yaml")
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        if not isinstance(data, dict):
            return {}
        section = data.get("paper_trading") or {}
        return section if isinstance(section, dict) else {}
    except Exception as e:  # noqa: BLE001
        _log.warning("config.yaml paper_trading 段解析失败，回落默认值: %s", e)
        return {}


_RAW_SECTION: Optional[Dict[str, Any]] = None


def _raw_section() -> Dict[str, Any]:
    global _RAW_SECTION
    if _RAW_SECTION is None:
        _RAW_SECTION = _load_yaml_section()
    return _RAW_SECTION


def _merge(base: Dict[str, Any], over: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


@dataclass
class PaperTradingConfig:
    """模拟盘运行参数（全部字段带安全默认值）。"""

    enabled: bool = False
    initial_capital: float = 1000000.0
    # 【V30.11】仓位模式：fixed_amount（固定金额/只）| pct（占总资产比例）
    position_mode: str = "fixed_amount"
    fixed_amount_per_position: float = 50000.0
    position_pct: float = 0.20
    max_positions: int = 20
    stop_loss_pct: float = -3.0
    take_profit_pct: float = 8.0
    # 【2026-11-05】当日跌幅离场阈值（负数；0=关闭）。相对昨收，与持仓盈亏无关。
    day_drop_exit_pct: float = -3.0
    # 【P0 修复 2026-10-19】按板止损 dict（key=main/chi_next/star，value=负百分比）
    stop_loss_by_board: Dict[str, float] = field(default_factory=lambda: {"main": -3.0, "chi_next": -5.0, "star": -5.0})
    enforce_t_plus_1: bool = True
    lot_size: int = 100
    # 【V30.12】滑点：买入上浮、卖出下调；0 = 理想化（不真实）
    slippage_pct: float = 0.1
    # 【V30.12】最长持有交易日数（超期按收盘强平）；回测与模拟盘共用
    max_hold_days: int = 10
    cohort_days: int = 100
    monitor_interval_sec: int = 30
    quote_stale_deviation_pct: float = 20.0
    notify_buys: bool = True
    notify_attribution: bool = True
    commission_rate: float = 0.0003
    commission_min: float = 5.0
    stamp_tax_rate: float = 0.0005
    transfer_fee_rate: float = 0.00001

    stop_loss_ratio: float = field(default=0.97, init=False)
    take_profit_ratio: float = field(default=1.15, init=False)
    # 当日跌幅离场价比（= 1 + day_drop_exit_pct/100）；0 表示该规则未启用
    day_drop_exit_ratio: float = field(default=0.97, init=False)
    # 板级止损比（运行时按 ts_code 查表）
    stop_loss_ratio_by_board: Dict[str, float] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        self.stop_loss_ratio = 1.0 + float(self.stop_loss_pct) / 100.0
        self.take_profit_ratio = 1.0 + float(self.take_profit_pct) / 100.0
        _drop_pct = float(self.day_drop_exit_pct or 0.0)
        # 只有负数才是「跌幅阈值」；0 / 正数一律视为关闭（正数按涨 3% 卖出显然不是本意）
        self.day_drop_exit_ratio = (1.0 + _drop_pct / 100.0) if _drop_pct < 0 else 0.0
        # 计算板级比值
        self.stop_loss_ratio_by_board = {
            board: 1.0 + float(pct) / 100.0
            for board, pct in (self.stop_loss_by_board or {}).items()
        }

    def resolve_stop_loss_pct(self, ts_code: str) -> float:
        """返回止损百分比（负数）。

        【V30.11 2026-11-04】实验口径统一为 -3%（所有板一致）：
        原按板差异化（创业板/科创板 -5%）会让这两板的止损更宽、
        天然盈亏更好，污染「哪个战法更优」的横向比较，故此处一律取全局值。
        保留 stop_loss_by_board 字段仅为向后兼容，不再参与计算。
        """
        return float(self.stop_loss_pct)

    def resolve_stop_loss_ratio(self, ts_code: str) -> float:
        """按 ts_code 返回止损价比（= 1 + pct/100）"""
        pct = self.resolve_stop_loss_pct(ts_code)
        return 1.0 + float(pct) / 100.0

    def position_amount(self, total_asset: float) -> float:
        """单票目标投入金额。

        fixed_amount 模式：固定金额（默认 5 万/只），与总资产脱钩，
            保证 20 只票的风险敞口一致 → 战法收益可横向比较。
        pct 模式：总资产 × position_pct（向后兼容）。
        """
        mode = str(self.position_mode or "fixed_amount").strip().lower()
        if mode == "pct":
            return max(0.0, float(total_asset) * float(self.position_pct))
        return max(0.0, float(self.fixed_amount_per_position))

    @property
    def fee_section(self) -> Dict[str, float]:
        return {
            "commission_rate": self.commission_rate,
            "commission_min": self.commission_min,
            "stamp_tax_rate": self.stamp_tax_rate,
            "transfer_fee_rate": self.transfer_fee_rate,
        }


def load_config(*, reload: bool = False) -> PaperTradingConfig:
    """读取模拟盘配置；任何异常都回落默认值，绝不抛出。"""
    if reload:
        global _RAW_SECTION
        _RAW_SECTION = None
    merged = _merge(DEFAULT_CONFIG, _raw_section())
    fee = merged.get("fee") or {}

    # 【P0 修复 2026-10-19】按板止损 dict 解析
    raw_board = merged.get("stop_loss_by_board") or {}
    if not isinstance(raw_board, dict):
        raw_board = {}
    # 合并默认值（用户少写某个 key 就用默认；用户写错 key 直接丢弃，不传）
    safe_board: Dict[str, float] = {}
    for k in ("main", "chi_next", "star"):
        v = raw_board.get(k)
        if v is None:
            safe_board[k] = float(DEFAULT_CONFIG["stop_loss_by_board"][k])
            continue
        try:
            fv = float(v)
            # 防御：止损百分比必须是负数且在 -20% ~ 0 之间（>0 是 bug）
            if fv >= 0 or fv < -20.0:
                _log.warning("stop_loss_by_board[%s]=%s 越界，回落默认", k, v)
                fv = float(DEFAULT_CONFIG["stop_loss_by_board"][k])
            safe_board[k] = fv
        except (TypeError, ValueError):
            _log.warning("stop_loss_by_board[%s]=%s 解析失败，回落默认", k, v)
            safe_board[k] = float(DEFAULT_CONFIG["stop_loss_by_board"][k])

    # 【2026-11-05】当日跌幅离场阈值：0 = 关闭；负数启用；正数/越界一律回落默认。
    _raw_drop = merged.get("day_drop_exit_pct", DEFAULT_CONFIG["day_drop_exit_pct"])
    try:
        safe_day_drop_pct = float(_raw_drop)
    except (TypeError, ValueError):
        _log.warning("day_drop_exit_pct=%s 解析失败，回落默认", _raw_drop)
        safe_day_drop_pct = float(DEFAULT_CONFIG["day_drop_exit_pct"])
    if safe_day_drop_pct > 0 or safe_day_drop_pct < -20.0:
        _log.warning(
            "day_drop_exit_pct=%s 越界（应在 -20 ~ 0，正数等于「当日涨3%%就跑」不是本意），回落默认",
            _raw_drop,
        )
        safe_day_drop_pct = float(DEFAULT_CONFIG["day_drop_exit_pct"])

    try:
        return PaperTradingConfig(
            enabled=bool(merged.get("enabled")),
            initial_capital=float(merged.get("initial_capital", 1000000.0)),
            position_mode=str(merged.get("position_mode", "fixed_amount") or "fixed_amount"),
            fixed_amount_per_position=float(merged.get("fixed_amount_per_position", 50000.0)),
            position_pct=float(merged.get("position_pct", 0.20)),
            max_positions=int(merged.get("max_positions", 20)),
            stop_loss_pct=float(merged.get("stop_loss_pct", -3.0)),
            take_profit_pct=float(merged.get("take_profit_pct", 8.0)),
            day_drop_exit_pct=safe_day_drop_pct,
            stop_loss_by_board=safe_board,
            enforce_t_plus_1=bool(merged.get("enforce_t_plus_1")),
            lot_size=int(merged.get("lot_size", 100)),
            slippage_pct=float(merged.get("slippage_pct", 0.1)),
            max_hold_days=int(merged.get("max_hold_days", 10)),
            cohort_days=int(merged.get("cohort_days", 100)),
            monitor_interval_sec=int(merged.get("monitor_interval_sec", 30)),
            quote_stale_deviation_pct=float(merged.get("quote_stale_deviation_pct", 20.0)),
            notify_buys=bool(merged.get("notify_buys", True)),
            notify_attribution=bool(merged.get("notify_attribution", True)),
            commission_rate=float(fee.get("commission_rate", 0.0003)),
            commission_min=float(fee.get("commission_min", 5.0)),
            stamp_tax_rate=float(fee.get("stamp_tax_rate", 0.0005)),
            transfer_fee_rate=float(fee.get("transfer_fee_rate", 0.00001)),
        )
    except Exception as e:  # noqa: BLE001
        _log.warning("模拟盘配置转换失败，回落默认值: %s", e)
        return PaperTradingConfig()


def is_enabled(cfg: Optional[PaperTradingConfig] = None) -> bool:
    """模拟盘是否启用（配置缺省关闭）。"""
    c = cfg or load_config()
    return bool(c.enabled)


__all__ = [
    "DEFAULT_CONFIG",
    "PaperTradingConfig",
    "is_enabled",
    "load_config",
]
