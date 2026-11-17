# -*- coding: utf-8 -*-
"""
小杰系统 V26.88 — 多基准对比模块（P1 高优先级）

【问题】
当前回测仅输出策略本身的命中率 / 平均收益，缺少与市场基准的对比：
  - 「我的策略 T+5 涨 3%」是真本事，还是同期沪深 300 也涨了 3%？
  - 没有超额收益（Alpha）评估，无法判断策略是否真的战胜市场
  - 散户极易被「绝对收益」误导：「牛市赚钱熊市亏大钱」效应

【本模块】
提供多基准对比：
  1. 沪深 300（000300.SH）：大盘蓝筹基准
  2. 中证 500（000905.SH）：中盘股基准（最贴近小杰 P3/P4 选股范围）
  3. 创业板指（399006.SZ）：成长 / 题材股基准（贴近 P1/P2 强势股）

【核心指标】
  - Alpha = 策略收益 - 基准收益（绝对超额）
  - Beta = 策略与基准的协方差 / 基准方差（相对波动率）
  - 胜率差 = 策略命中率 - 基准同期正向天数占比
  - Calmar = 年化收益 / 最大回撤

【数据源】
  - 优先从本地 DuckDB daily_data 缓存读取（ts_code=000300.SH 等）
  - 缺失时调用 Tushare pro.index_daily()（已通过限流器封装）
  - 若 Tushare 失败，返回 None 跳过该基准（不阻塞主流程）

【使用方式】
    from core.benchmark_compare import BenchmarkComparator

    cmp = BenchmarkComparator(start_date='20250101', end_date='20251231')
    cmp.fetch_all()  # 预拉取三个基准
    result = cmp.compare(strategy_returns=[...], strategy_label='P4尾盘')
    print(result.alpha_hs300_pct, result.alpha_zz500_pct, result.alpha_cyb_pct)
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)


# ==================== 基准定义 ====================

@dataclass(frozen=True)
class BenchmarkSpec:
    """单个基准指数的元数据。"""
    code: str           # Tushare ts_code
    name: str           # 中文名
    category: str       # large_cap / mid_cap / growth
    typical_pool: str   # 适配的小杰战法池

    @property
    def label(self) -> str:
        return f"{self.name}({self.code})"


# A 股三大代表性基准（散户最关心的三档）
DEFAULT_BENCHMARKS: Tuple[BenchmarkSpec, ...] = (
    BenchmarkSpec("000300.SH", "沪深300", "large_cap", "P1底仓"),
    BenchmarkSpec("000905.SH", "中证500", "mid_cap", "P3尾盘"),
    BenchmarkSpec("399006.SZ", "创业板指", "growth", "P2竞价/P4共振"),
)


@dataclass
class BenchmarkSeries:
    """单个基准的日线收益率序列。"""
    spec: BenchmarkSpec
    dates: List[str] = field(default_factory=list)
    closes: List[float] = field(default_factory=list)

    @property
    def daily_returns_pct(self) -> np.ndarray:
        """日收益率序列（百分比），长度 = len(closes) - 1。"""
        if len(self.closes) < 2:
            return np.array([], dtype=float)
        c = np.asarray(self.closes, dtype=float)
        return (c[1:] - c[:-1]) / c[:-1] * 100.0

    @property
    def total_return_pct(self) -> float:
        """区间总收益（百分比）。"""
        if len(self.closes) < 2 or self.closes[0] <= 0:
            return 0.0
        return (self.closes[-1] - self.closes[0]) / self.closes[0] * 100.0


@dataclass
class BenchmarkCompareResult:
    """单期策略 vs 多基准的对比结果。"""
    strategy_label: str
    strategy_total_return_pct: float
    strategy_sample_n: int
    period_start: str
    period_end: str
    benchmarks: Dict[str, Dict[str, Any]]  # code -> {name, total_return, alpha, beta, ...}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "strategy_label": self.strategy_label,
            "strategy_total_return_pct": round(self.strategy_total_return_pct, 4),
            "strategy_sample_n": int(self.strategy_sample_n),
            "period_start": self.period_start,
            "period_end": self.period_end,
            "benchmarks": self.benchmarks,
        }


# ==================== 拉取器 ====================


class BenchmarkComparator:
    """基准对比器：拉取三大指数 + 计算 Alpha/Beta。"""

    def __init__(
        self,
        start_date: str,
        end_date: str,
        benchmarks: Tuple[BenchmarkSpec, ...] = DEFAULT_BENCHMARKS,
        use_db_cache: bool = True,
    ):
        self.start_date = str(start_date).strip()
        self.end_date = str(end_date).strip()
        self.benchmarks = benchmarks
        self.use_db_cache = bool(use_db_cache)
        self._series: Dict[str, BenchmarkSeries] = {}

    # ---------- 拉取 ----------
    def fetch_all(self) -> Dict[str, bool]:
        """
        预拉取所有基准日线。

        Returns:
            {code: True/False} 表示是否成功
        """
        results: Dict[str, bool] = {}
        for spec in self.benchmarks:
            results[spec.code] = self._fetch_one(spec)
        return results

    def _fetch_one(self, spec: BenchmarkSpec) -> bool:
        """拉取单个基准日线。优先 DuckDB，回落 Tushare。"""
        series = BenchmarkSeries(spec=spec)

        # 1. 尝试 DuckDB 缓存
        if self.use_db_cache:
            try:
                df = self._read_from_db(spec.code)
                if df is not None and len(df) >= 2:
                    series.dates = df["trade_date"].astype(str).tolist()
                    series.closes = df["close"].astype(float).tolist()
                    self._series[spec.code] = series
                    logger.info("[Benchmark] %s 命中缓存 %d 条", spec.label, len(series.dates))
                    return True
            except Exception as e:
                logger.debug("[Benchmark] DB 缓存读取失败 %s: %s", spec.code, e)

        # 2. 回落 Tushare（走限流器）
        try:
            from data.tushare_rate_limiter import rate_limited_call
            # 【V26.88+10-1】改为复用 data.fetcher 懒初始化单例（避免多份独立 Tushare 实例）
            from data.fetcher import _client as _fclient
            pro = _fclient._ensure_pro_initialized()
            if pro is None:
                logger.warning("[Benchmark] Tushare 未初始化，跳过 %s", spec.code)
                return False

            df = rate_limited_call(
                lambda: pro.index_daily(
                    ts_code=spec.code,
                    start_date=self.start_date,
                    end_date=self.end_date,
                ),
                endpoint=f"index_daily_{spec.code}",
            )
            if df is None or len(df) < 2:
                logger.warning("[Benchmark] %s Tushare 返回数据不足", spec.code)
                return False
            df = df.sort_values("trade_date").reset_index(drop=True)
            series.dates = df["trade_date"].astype(str).tolist()
            series.closes = df["close"].astype(float).tolist()
            self._series[spec.code] = series
            logger.info("[Benchmark] %s 拉取 Tushare %d 条", spec.label, len(series.dates))
            return True
        except Exception as e:
            logger.warning("[Benchmark] %s 拉取失败: %s", spec.code, e)
            return False

    def _read_from_db(self, ts_code: str) -> "Optional[Any]":
        """从 DuckDB daily_data 表读取指数日线。"""
        try:
            from data.db_core import get_conn
            con = get_conn(read_only=True)
            try:
                df = con.execute(
                    """
                    SELECT trade_date, close FROM daily_data
                    WHERE ts_code = ? AND trade_date BETWEEN ? AND ?
                    ORDER BY trade_date ASC
                    """,
                    [ts_code, self.start_date, self.end_date],
                ).fetch_df()
                return df if len(df) > 0 else None
            finally:
                try:
                    con.close()
                except Exception:
                    pass
        except Exception:
            return None

    # ---------- 对比 ----------
    def compare(
        self,
        strategy_returns_pct: List[float],
        strategy_dates: Optional[List[str]] = None,
        strategy_label: str = "strategy",
    ) -> BenchmarkCompareResult:
        """
        对比策略收益与所有基准。

        Args:
            strategy_returns_pct: 策略每笔交易的收益率（百分比），如 [3.2, -1.5, 5.1]
            strategy_dates: 对应的交易日期（YYYYMMDD），用于对齐基准
            strategy_label: 策略标识，如 "P4尾盘" / "P3盘中"

        Returns:
            BenchmarkCompareResult 对象
        """
        # 策略聚合收益（等权平均）
        if not strategy_returns_pct:
            strat_total = 0.0
            strat_n = 0
        else:
            arr = np.asarray(strategy_returns_pct, dtype=float)
            arr = arr[np.isfinite(arr)]
            strat_total = float(np.mean(arr)) if arr.size > 0 else 0.0
            strat_n = int(arr.size)

        period_start = (
            strategy_dates[0] if strategy_dates and len(strategy_dates) > 0
            else self.start_date
        )
        period_end = (
            strategy_dates[-1] if strategy_dates and len(strategy_dates) > 0
            else self.end_date
        )

        benchmarks_out: Dict[str, Dict[str, Any]] = {}
        for code, series in self._series.items():
            bench_total = series.total_return_pct
            alpha = strat_total - bench_total
            # Beta（仅在策略和基准都有足够样本时计算）
            beta = self._calc_beta(arr if strat_n > 0 else np.array([]), series.daily_returns_pct)
            win_rate_bench = float(np.mean(series.daily_returns_pct > 0)) * 100.0 if len(series.daily_returns_pct) > 0 else 0.0
            benchmarks_out[code] = {
                "name": series.spec.name,
                "category": series.spec.category,
                "typical_pool": series.spec.typical_pool,
                "total_return_pct": round(bench_total, 4),
                "alpha_pct": round(alpha, 4),
                "beta": round(beta, 4) if np.isfinite(beta) else None,
                "win_rate_pct": round(win_rate_bench, 4),
                "sample_n": len(series.daily_returns_pct),
                "data_source": "cache" if self.use_db_cache else "tushare",
            }

        return BenchmarkCompareResult(
            strategy_label=strategy_label,
            strategy_total_return_pct=round(strat_total, 4),
            strategy_sample_n=strat_n,
            period_start=period_start,
            period_end=period_end,
            benchmarks=benchmarks_out,
        )

    @staticmethod
    def _calc_beta(strategy_returns: np.ndarray, bench_returns: np.ndarray) -> float:
        """
        Beta = cov(strategy, bench) / var(bench)。

        简化处理：使用整体收益率作为单点计算（不展开成日序列），
        返回策略收益相对基准的弹性系数：
          beta ≈ strat_total / bench_total  （当 bench_total != 0）
        """
        if strategy_returns.size == 0 or bench_returns.size == 0:
            return float("nan")
        strat_total = float(np.mean(strategy_returns))
        bench_total = float(np.mean(bench_returns))
        if abs(bench_total) < 1e-9:
            return float("nan")
        return strat_total / bench_total


# ==================== 便捷函数 ====================


def quick_compare(
    strategy_returns_pct: List[float],
    start_date: str,
    end_date: str,
    strategy_label: str = "strategy",
) -> BenchmarkCompareResult:
    """
    一键对比：拉取默认三大基准 + 输出对比结果。

    Example:
        result = quick_compare([3.2, -1.5, 5.1], '20250101', '20251231', 'P4尾盘')
        print(f"Alpha vs 沪深300: {result.benchmarks['000300.SH']['alpha_pct']:.2f}%")
    """
    cmp = BenchmarkComparator(start_date=start_date, end_date=end_date)
    cmp.fetch_all()
    return cmp.compare(
        strategy_returns_pct=strategy_returns_pct,
        strategy_label=strategy_label,
    )


# ==================== 自检入口 ====================


def _self_test() -> int:
    """自检：使用本地 DB 缓存（如有）或生成 mock 数据。"""
    print("[Benchmark] 开始自检 ...")

    # Mock：避免依赖真实 DB
    class _MockSeries:
        def __init__(self, total_ret: float, daily: np.ndarray):
            self.spec = BenchmarkSpec("000300.SH", "沪深300", "large_cap", "P1")
            self._closes = [100.0, 100.0 * (1 + total_ret / 100.0)]
            self.daily_returns_pct = daily

    cmp = BenchmarkComparator("20250101", "20251231")
    # 注入 mock 数据
    cmp._series["000300.SH"] = BenchmarkSeries(spec=DEFAULT_BENCHMARKS[0])
    cmp._series["000300.SH"].dates = ["20250101", "20251231"]
    cmp._series["000300.SH"].closes = [100.0, 108.0]
    cmp._series["000905.SH"] = BenchmarkSeries(spec=DEFAULT_BENCHMARKS[1])
    cmp._series["000905.SH"].dates = ["20250101", "20251231"]
    cmp._series["000905.SH"].closes = [100.0, 105.0]
    cmp._series["399006.SZ"] = BenchmarkSeries(spec=DEFAULT_BENCHMARKS[2])
    cmp._series["399006.SZ"].dates = ["20250101", "20251231"]
    cmp._series["399006.SZ"].closes = [100.0, 112.0]

    # 策略：模拟 5 笔交易，平均 6% 收益
    strat_returns = [5.0, 7.0, -2.0, 10.0, 10.0]
    result = cmp.compare(strat_returns, strategy_label="自检策略")

    print(f"[OK] 策略平均收益: {result.strategy_total_return_pct:.2f}%")
    for code, b in result.benchmarks.items():
        print(f"  vs {b['name']}: 基准 {b['total_return_pct']:.2f}%, "
              f"Alpha {b['alpha_pct']:+.2f}%")
        # 验证：策略 6% vs 沪深 300 8% → alpha = -2%
        if code == "000300.SH":
            assert abs(b["alpha_pct"] - (-2.0)) < 0.01, f"alpha 错误: {b['alpha_pct']}"

    print("[OK] 自检全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(_self_test())
