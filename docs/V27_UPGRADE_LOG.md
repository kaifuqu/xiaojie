# V27 升级清单（2026-09-27）

> 把这次"按 ROI 排序的全量升级"汇总成一份文档，便于后续 audit / 回看。

## 升级阶段

| 阶段 | 项目 | 文件 | 预期提升 |
|------|------|------|----------|
| 0 | X-A 回测系统 | `data/signal_logger.py`、`data/signal_backtest.py` | 后续优化的基础 |
| 1 | P1-A 动量/反转双轨 | `core/p1_momentum_reversal_classifier.py`、`core/pool_manager.py` | +8-12 pp |
| 1 | P1-B 涨停炸板拦截 | `core/p1_limit_break_filter.py` | +5-8 pp |
| 1 | P1-C 机构抱团解构 | `core/p1_inst_velocity.py`、`core/pool_manager.py` | +5-8 pp |
| 1 | P1-D 业绩预告-预期剪刀差 | `core/p1_forecast_consensus.py`、`core/pool_manager.py` | +6-10 pp |
| 2 | A2/P3-AB 9:30-9:45 黄金早盘 | `core/strategies/p3_golden_open_strategies.py`、`core/strategies/p3_intraday_screener.py` | +8-12 pp |
| 2 | A4 炸板回封强度 | `core/strategies/limit_break_quality.py`、`core/strategies/strat_p5_postmarket.py` | +3-5 pp |
| 2 | P5 burst_adj 上限 99→100 | `core/strategies/strat_p5_postmarket.py`（11 处） | 顶级龙头不被卡 |
| 3 | A1 跨池接力指数 | `core/p1_pool_relay.py`、`core/pool_manager.py` | +5-8 pp |
| 3 | B3 情绪温度计（8 类） | `core/market_sentiment.py` | +5-8 pp |
| 3 | P1-B/C/D/A1 集成 | `core/pool_manager.py` | 上述整合 |
| 3 | B1/B2/B4 趋势/粘合/轮动 | `core/strategies/p3_aux_strategies.py`、`core/strategies/p3_intraday_screener.py` | +3-5 pp |
| 4 | 推送 signal_log 适配器 | `core/pool_signal_adapter.py` | 回测数据接入 |
| 4 | C1/C2 战法胜率 UI 辅助 | `core/strategy_effectiveness_ui.py` | 用户体验 |

## 数据契约

- `signal_log`（推送记录表）
- `signal_pnl`（T+N 收益表）
- `strategy_effectiveness`（战法胜率矩阵表）

三张表均通过 `data/signal_logger._ensure_tables` 自动创建（幂等）。

## 集成点

| 模块 | 调用 |
|------|------|
| `core/pool_manager._process_single_stock_for_p1` | 注入 P1-A/B/C/D/A1 五个新维度 |
| `core/strategies/p3_intraday_screener.evaluate_p3_intraday_screener` | 注入 P3-12/13/14 + P3-15/16/17 |
| `core/strategies/strat_p5_postmarket.run_all` | 注入 P5 炸板回封分级 + burst 上限 100 |

## 验收

- 各池推送记录入 signal_log（5 日后开始积累 T+1 数据）
- T+5 数据可生成第一份战法胜率报告
- P1 通过票包含"动量/反转"分类标签
- P3 命中标签可包含 17 个子战法
- P5 burst_score 上限为 100（不再是 99）
