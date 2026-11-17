# 模拟盘设计说明（P3 / P4 信号自动买卖）

> 版本：V30.12 · 落地日期 2026-10-17 · 最后校准 2026-11-04 · 模块目录 `core/paper_trading/`
>
> **【2026-11-04 口径校准说明】** 本文档此前停留在 V29.3 口径，与实际执行口径已脱节。
> 现按 `config.yaml → paper_trading` 与 `core/paper_trading/` 实际实现校准，
> 涉及：仓位口径（总资产×20% → 固定 5 万/只）、最大持仓（5 → 20 只）、
> 止盈（+15% → +8%）、止损（按板差异化 → 统一 -3%）、滑点（0 → 0.1%）、
> 最长持有（无 → 10 个交易日到期强平）。
> **口径唯一事实源是 `config.yaml`，本文档只作说明；若两者冲突，以 config.yaml 为准。**

## 0. 一句话

模拟盘把 **P3 盘中 / P4 尾盘的正式信号**当成买入指令，按信号现价自动买入；
之后每 30 秒盯一次现价，**跌 3% 全部清仓、涨 8% 全部清仓**，强制 T+1，
持有满 10 个交易日未触发阈值则按收盘价强制平仓；
跑满 100 个交易日封板，用战法排行榜回答「哪条战法真的准」。

它不接券商、不下单、不改任何实盘逻辑，只在旁路记一笔账。

## 1. 业务规则（现行口径）

| 项目 | 取值 | 说明 |
| --- | --- | --- |
| 买入来源 | 仅 P3 / P4 正式信号 | P1 / P2 / P5 不参与 |
| 排除对象 | 观察池、执行层级 C、建议仓位 0%、现价 ≤ 0、非法 pool | 与五池扫描的过滤口径一致 |
| 买入价 | 信号行「现价」+ 滑点上浮（0.1%） | 严格按信号价成交，不做二次过滤 |
| 单票仓位 | **固定 5 万/只**（`position_mode: fixed_amount`） | 与总资产脱钩，向下取整到 100 股整手 |
| 最大持仓 | **20 只** | 超出记 MAX_POSITIONS 跳过 |
| 止损 | 现价 ≤ 买入价 × 0.97 → 100% 清仓 | **所有板统一 -3%**（不按板差异化，见 §1.1） |
| 止盈 | 现价 ≥ 买入价 × **1.08** → 100% 清仓 | 同上 |
| 最长持有 | **10 个交易日** | 到期未触发阈值则按收盘价强平（`max_hold_days`） |
| 优先级 | 止损优先于止盈 | 同一次 tick 里先判止损 |
| T+1 | 强制 | 当日买入当日不可卖 |
| 不卖情形 | 停牌（volume=0）、跌停封板、报价无效 | 记跳过流水，下一拍再试 |
| 监控间隔 | 30 秒 | 仅交易时段 09:15–11:30 / 13:00–15:00 |
| 实验期 | 100 个交易日 | 满 100 日后停止新开仓，已有持仓继续按规则退出 |
| 买入限制 | 不受企微开关、不受 Top3 限制 | 模拟盘要吃全量正式信号才有统计意义 |

### 1.1 为什么是固定金额 / 20 只 / 统一 ±3%/+8%

- **固定金额而非总资产比例**：20 只票若各占总资产 20%，总敞口 400%，
  收益率会被放大数倍且不可横向比较。改为每只固定 5 万、同时最多 20 只，
  总敞口恰好 100%，**每只票的风险敞口一致** → 1–2 个月后按战法横向比较收益才有意义。
- **统一 ±3%/+8% 而非按板差异化**：创业板/科创板若止损更宽（如 -5%），
  天然显得更优，会**污染战法归因**。统一阈值让不同板块的胜率可比。
- **`position_pct` 仍保留**：仅当 `position_mode: pct` 时生效（向后兼容），默认不用。

### 1.2 最长持有 10 个交易日（P0-3）

`max_hold_days: 10` 的语义是「**最多持有 10 个交易日**」，按**交易日**而非自然日计数
（用交易日历取实际交易日，避免周末/长假把持仓拖成 14 天）。
到期未触发止损/止盈的持仓，在收盘结算时按当日收盘价强制平仓，
出场原因记 `MAX_HOLD_DAYS`。

> 这条规则与回测侧 `core/stop_rule_backtest` **共用同一份配置**，
> 保证「测的和跑的」是同一套规则 —— 否则回测结论对模拟盘没有参考价值。
> `max_hold_days <= 0` 表示不启用该规则。

## 2. 费用口径

按 A 股市场通行参数配置（属于市场通行值，不是官方核验结论，如券商实际费率不同请改 config.yaml）：

| 费用项 | 费率 | 方向 | 备注 |
| --- | --- | --- | --- |
| 佣金 | 成交额 × 0.03%，最低 5 元/笔 | 买卖双边 | 各家券商不同 |
| 印花税 | 成交额 × 0.05% | 仅卖出 | 证管费 / 经手费已含在佣金内，不重复计 |
| 过户费 | 成交额 × 0.001% | 买卖双边 | |
| 滑点 | **0.1%** | 买入上浮 / 卖出下调 | A 股小盘股实盘约 0.1%；调整后按 0.01 元 ROUND_HALF_UP 取整 |

配置位置：`config.yaml → paper_trading.fee`，实现位置 `core/paper_trading/fees.py`。

## 3. 架构

    五池扫描 (P3/P4)
      └─> auto_sniper_daemon._paper_on_signals_safe()      # 薄调用点
            └─> core/paper_trading/daemon_hooks.paper_on_signals_safe()
                  └─> engine.on_signals()                  # 过滤 → 排序 → 买入

    每 30 秒（交易时段）
      └─> auto_sniper_daemon._job_paper_positions_tick()
            └─> daemon_hooks.paper_tick_job()
                  └─> engine.on_tick()                    # 止损 / 止盈 / T+1 / 异常行情

    15:05 收盘
      └─> auto_sniper_daemon._job_paper_daily_settle()
            └─> daemon_hooks.paper_settle_job()
                  └─> engine.daily_settle()               # 写日净值 + 推进实验进度

    08:40 开盘前
      └─> auto_sniper_daemon._job_paper_init()
            └─> daemon_hooks.paper_init_job()             # 建表 + 初始化账户

    UI（只读）
      └─> ui/pages/1_模拟盘.py
            └─> ui/paper_trading_panel.render_paper_trading_panel()
                  └─> reporting.build_snapshot()          # 只读 DuckDB

模块清单：

| 文件 | 职责 |
| --- | --- |
| `config.py` | 读 config.yaml 的 paper_trading 段 + 安全默认值 |
| `fees.py` | 佣金 / 印花税 / 过户费 / 滑点计算 |
| `schema.py` | 四张表 DDL + 唯一索引 + 自检 |
| `store.py` | 读写封装（write_transaction / read_query） |
| `engine.py` | 买卖判定、T+1、涨跌停、结算 |
| `reporting.py` | 只读快照 + 战法排行榜聚合 |
| `daemon_hooks.py` | 守护进程挂钩，异常全吞 |

## 4. 数据表

| 表 | 主键 | 说明 |
| --- | --- | --- |
| `paper_account` | account_id | 单例账户：现金、状态、实验进度 |
| `paper_position` | (account_id, code) | 只存未平仓持仓 |
| `paper_order` | order_id，client_order_id 唯一索引 | append-only 流水账，含 SKIP 记录 |
| `paper_equity_daily` | (account_id, trade_date) | 每日收盘一行净值 |

client_order_id 格式：`PAPER_MAIN|代码|BUY|YYYYMMDD|序号`。
唯一索引兜底幂等 —— 同一批信号重放不会重复买入。

## 5. UI

独立一级页面：`ui/pages/1_模拟盘.py`（Streamlit 原生 multipage，左侧导航栏直接切）。

V29.3 变更：原先挂在 ui/app.py 底部的 expander 已移除。大屏每次 rerun 都很重，
持仓表需要「随时可查 + 独立刷新」，独立成页更合适。主页面保留一行指引。

面板包含 6 块：

1. **账户总览卡**：总资产 / 现金 / 市值 / 持仓盈亏 / 今日盈亏 / 累计费用 / 实验进度
2. **当前持仓表**：买入价、现价、股数、净成本、浮动盈亏、仓位占比、距止损 %、距止盈 %、持有天数
3. **已平仓记录**：净收益、收益率、持有天数、出场原因
4. **战法排行榜**（100 日实验的核心产出）
5. **日净值表 + 净值曲线**
6. **费用汇总**

## 6. 战法排行榜口径

- signal_count = 已平仓笔数 + 当前持仓数（同一票只计一次）
- closed_count = 已平仓笔数
- win_rate = 净收益 > 0 的平仓占比（只有平仓样本参与）
- total_pnl / avg_pnl / median_pnl 一律含费用
- 一个信号可能命中多个原子战法 → 各战法样本相互重叠，累计盈亏不可相加
- closed_count < 10 标「样本不足」，不作可信排名

## 7. 开关与运维

```yaml
# config.yaml —— 【2026-11-04 已按现行实现校准】
paper_trading:
  enabled: true    # 改为 true 并重启 daemon 才会自动买卖
  initial_capital: 1000000.0   # 初始模拟资金（100万 = 20只 × 5万）
  # 仓位口径：固定金额/只，与总资产脱钩，保证 20 只票风险敞口一致 → 战法收益可横向比较
  position_mode: fixed_amount  # fixed_amount | pct
  fixed_amount_per_position: 50000.0   # 每只 5 万
  position_pct: 0.20          # 仅 position_mode=pct 时生效（向后兼容）
  max_positions: 20            # 最多同时持有 20 只
  stop_loss_pct: -3.0          # 止损 -3%（所有板统一）
  take_profit_pct: 8.0         # 止盈 +8%（所有板统一）
  max_hold_days: 10            # 最长持有 10 个交易日，到期强平；<=0 不启用
  enforce_t_plus_1: true       # T+1：当日买入次日才可卖
  lot_size: 100                # 整手股数
  slippage_pct: 0.1            # 滑点 %：买入上浮/卖出下调
  cohort_days: 100             # 实验交易日数；满 100 日后停止新开仓
  monitor_interval_sec: 30     # 持仓阈值监控间隔（交易时段）
  fee:
    commission_rate: 0.0003    # 佣金 万3
    commission_min: 5.0        # 最低 5 元/笔
    stamp_tax_rate: 0.0005     # 印花税 万5（卖出单边）
    transfer_fee_rate: 0.00001 # 过户费 十万分之一（双边）
```

注意事项：

- 与主库同库，遵守 DuckDB 单写者约束：UI 与 daemon 不能同时跑。
- 模拟盘全部走 data.db_core 连接层，不另开连接，不破单写者规则。
- 所有 daemon 挂钩点都 try/except 兜底，模拟盘异常不会打断五池扫描和企微推送。
- maintenance_mode=true 时模拟盘任务全部跳过。

## 8. 测试

```powershell
# 端到端回归（隔离临时 DuckDB，不碰主库）
D:\xiaojie\_pyenv\venv\Scripts\python.exe tools\_test_paper_trading.py

# 模块自检（需要 PYTHONPATH 指向项目根）
$env:PYTHONPATH = "D:\xiaojie"
D:\xiaojie\_pyenv\venv\Scripts\python.exe core\paper_trading\fees.py
D:\xiaojie\_pyenv\venv\Scripts\python.exe core\paper_trading\schema.py
D:\xiaojie\_pyenv\venv\Scripts\python.exe core\paper_trading\store.py
D:\xiaojie\_pyenv\venv\Scripts\python.exe core\paper_trading\engine.py
D:\xiaojie\_pyenv\venv\Scripts\python.exe core\paper_trading\reporting.py
D:\xiaojie\_pyenv\venv\Scripts\python.exe core\paper_trading\daemon_hooks.py
```

回归覆盖 16 项：建表幂等 / 买入整手与费用 / 重复信号去重 / 信号过滤 /
止损止盈阈值边界 / T+1 / 停牌与跌停不卖 / 涨停买入过滤生产链集成 /
现金不足与不足一手 / 最大持仓上限 / 日净值幂等与资金恒等式 / 排行榜聚合 /
排行榜边界 / 开关关闭与重放幂等 / 板块涨跌停幅度 / UI 快照字段完整。

> 【2026-11-04】阈值断言已对齐现行口径（止盈 +8%、固定 5 万仓位含 0.1% 滑点）。
> 原断言停留在更早期的 +15% / 总资产×20% / 无滑点，已无法反映真实执行行为。

## 9. 已知限制

- 模拟盘不判断成交容量：涨停封板时买入仍会成交（现实中可能买不到）。
- 止损止盈按 30 秒采样判定，不是逐笔；极端行情下实际成交价可能偏离阈值。
- 卖出不做部分减仓，符合需求中「100% 抛出」的要求。
- 战法排行榜在样本重叠时不可把各战法盈亏相加。
- 费用参数为市场通行值，未从官方公告核验，按实际券商费率调整即可。