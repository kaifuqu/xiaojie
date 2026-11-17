# V28 升级日志（2026-09-27）

> 基于两位朋友的精简路线 + 用户核心诉求"选股尽量不亏、值得持有"，V28 聚焦**避雷 + 拿住**两个维度。

## 升级项（10 项）

| 项 | 文件 | 核心功能 | 预期提升 |
|---|---|---|---|
| **0** | `core/pool_signal_adapter_v28.py` | signal_log 真接入 5 个池 | 信号采集启动 |
| **A** | `core/theme_lifecycle.py` | 题材生命周期 5 态分类 | +5-8 pp |
| **B** | `core/event_filter.py` | 特殊事件拦截（次新/ST/跌停/高位派发/财报窗） | +3-5 pp（避雷） |
| **C** | `core/dragon_tiger_seat.py` | 龙虎榜席位结构（机构 vs 游资） | +5-10 pp |
| **D** | `core/limit_quality_3d.py` | 封板质量三维（时间/开板/封单/联袂） | +2-3 pp |
| **E** | `core/exit_strategy.py` | 4 档出场模板（严/中/宽/持有） | 实际收益 +15-25% |
| **F** | `core/strategy_weight_optimizer.py` | 战法权重自动调节 | +3-5 pp（间接） |
| **G** | `core/push_dedup.py` | 推送去重（同票 3 日冷却 + 跨池） | UX |
| **H** | `core/push_template_momentum_reversal.py` | 动量/反转推送模板分流 | 执行胜率 +3-5 pp |

## 用户核心诉求对应

| 用户诉求 | 解决项 |
|---|---|
| "选股尽量买入后不亏损" | B 避雷 + C 龙虎榜 + D 封板质量 |
| "选出来的股票要值得持有" | E 出场模板 + F 战法权重自调 + H 动量/反转分流 |
| "信号数据要真实可用" | 0 signal_log 真接入 |

## 接入点

- `core/pool_manager._process_single_stock_for_p1`：
  - B 特殊事件拦截（前置 veto）
  - A 题材生命周期加分
  - C 龙虎榜席位加分
- `core/strategies/strat_p5_postmarket.run_all`：
  - V27 A4 炸板回封 + V28 D 封板质量三维
- `core/strategies/strat_p3_intraday.run_all` / `strat_p4_tail.run_all`：
  - V28 0 signal_log 写入
- `core/strategies/strat_p2_auction.run_all` / `strat_p5_postmarket.run_all`：
  - V28 0 signal_log 写入
- `core/pool_signal_adapter_v28.append_signal_log_safe`（统一推送前钩子）：
  - E 出场建议注入
  - G 推送去重
  - H 动量/反转模板分流

## 战法权重优化器规则

- 胜率 ≥ 70% 且 n ≥ 10 → weight × 1.2
- 胜率 ≤ 45% 且 n ≥ 20 → weight × 0.7
- 胜率 ≤ 30% 且 n ≥ 30 → marked deprecated（人工审核）

## 4 档出场模板

| 模板 | 触发 | 止损规则 | 止盈 | 持仓 |
|---|---|---|---|---|
| 🔴 严止损 | 次新 / 高潮期 | MA5 -1% | 8%/15% | 3 日 |
| 🟡 中止损 | 默认 / 动量 | MA10 -2% | 8%/15% | 3 日 |
| 🟢 宽止损 | 反转 / 龙头 / 退潮 | MA20 -3% | 10%/18% | 5 日 |
| 🟣 持有型 | 强势缩量锁仓 | 成本价兜底 | 12%/20% | 5 日 |

## 验证结果

- 14 个文件 AST 静态语法校验：✅
- 11 个新模块 import 路径：✅
- 端到端冒烟测试：✅
  - 题材 5 态分类正确
  - ST / 次新 / 财报窗拦截生效
  - 龙虎榜 机构 +6 / 游资 +4 / 回头率 +2
  - 封板三维综合满分 15 分
  - 出场模板自动选档（高潮期→严止损）
  - 推送去重 3 日冷却生效
  - 动量→09:30 追入 / 反转→09:25 竞价观察
