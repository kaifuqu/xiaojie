# V29 待办清单（仅文档，不实现）

> 来自两位朋友的客观评价 + V28.5 复盘。每一项标注前置 / 阻塞 / 建议排期。

---

## 🟡 A. 数据 / 接口相关（需 Tushare 积分或新接口确认）

### A1. Tushare 三个特殊事件接口接入
**目的**：把 `special event_filter` 从"纯 df + 规则"升级为"接口驱动"，覆盖：
- 大股东减持公告 5 日内 → -8
- 限售解禁后 60 日 → -4
- 财报披露前 3 日 → -3（V28 已用规则版，接口版更准）
- 业绩预增 / 回购 / 中标公告 → +3~+4

**接口**：
- `pro.anns_d` — 公告日数据（积分门槛 5000/日级别）
- `pro.share_float` — 解禁数据（积分门槛 8000/日级别）
- `pro.pledge_stat` — 质押（可选）

**前置 / 阻塞**：
- 需要确认 Tushare token 当前积分余额（高级版预计够用，基础版可能不够）
- 需要新增 `data/announcement_fetcher.py`
- 需要新增 dim_security.list_date 字段校验

**预计工作量**：1.5 周（含接口测试）

**建议排期**：V29.5（V29 跑一周真实数据再决定）

---

### A2. HiThink 北向通道端点
**目的**：用北向资金做大盘 Beta 调整（北向连续 3 日流出 → P2/P3 -5）

**前置 / 阻塞**：
- **HiThink 没有"沪深港通"端点**（已确认）
- 替代方案：
  - 方案 1：用东财 Web API（不稳定，每天 IP 限流）
  - 方案 2：用 Tushare `pro.hsgt_top10` 和 `pro.moneyflow_hsgt_capital`（积分门槛 5000/日级别）
  - 方案 3：用同花顺北向数据抓包（合规风险高）

**建议排期**：V30 前置评估，方案 2 优先

---

## 🟡 B. 守护进程 / 实时任务相关

### B1. auto_sniper_daemon 09:30 / 10:30 / 13:30 cron 任务
**目的**：盘中自动刷新涨停池，分别用于：
- 09:30 — 竞价后第一步过滤
- 10:30 — 早盘封板池
- 13:30 — 尾盘接力池

**前置 / 阻塞**：
- 需要确认现有 `auto_sniper_daemon.py` 是否已存在并能 cron 调度
- 建议部署方式：Windows 任务计划程序 / Linux crontab
- **风险**：crontab 与主程序 race condition（推送过程中被新数据覆盖）

**建议排期**：与 V29 数据清洗一起做（约 1 周）

---

### B2. regime_analyzer 接入情绪温度计（B3 模块）
**目的**：把"情绪温度计"输出与 P1 pass_line 联动，不同温度用不同阈值

**前置 / 阻塞**：
- 确认 `core.market_sentiment.classify_market_sentiment` 接口是否已实现
- 如已实现，P1 入口加 `regime = classify_market_sentiment()` → 读 `pass_line` 从常量改为从 regime 表读
- 如果阈值切换规则不明确，先做 2 周回测看哪种 regime 阈值最优

**建议排期**：V29 后半段

---

### B3. P2-A sector_realtime 字段推手
**目的**：P2 竞价时段需要"板块涨幅 Top5"实时推送，由谁负责刷新是 P2 打分的前置

**前置 / 阻塞**：
- 确认 `data/sector_realtime_fetcher` 是否已存在
- 如不存在，需 1 周开发

**建议排期**：与 B1（auto_sniper_daemon）一起

---

## 🟡 C. 长时间优化（高 ROI，但需稳定数据）

### C1. 真实战法胜率盘点（C1 — 跑 5-7 天后做）
**触发条件**：signal_log 累积 ≥ 50 条后

**步骤**：
1. 跑 `python -m data.signal_backtest.run_daily_backfill` 7 天
2. 用 V28 的 `data/signal_backtest.stat_strategy_effectiveness` 看报告
3. 胜率 < 50% 的战法列入候选淘汰名单
4. 胜率 ≥ 70% 且样本 ≥ 20 的战法列入 V29 升权候选

**建议排期**：V29 第 2 周执行

---

### C2. 战法权重 auto_apply=True 开启
**触发条件**：合计样本 ≥ 200 且 7 日胜率趋势稳定

**前置**：
- 已跑完 C1
- 信号采集无显著异常
- 用户对 V28.5 推送质量确认满意

**建议排期**：V29 末 → V30 初

---

## 🟢 D. 用户体验改进（低优先级）

### D1. weight_recommendation 自动 review 入口（已部分完成）
- ✅ 已加 `core/cli_view_recommendation.py`（人工跑命令查看）
- 未来可加：邮件 / 微信日报推送

### D2. hold 档触发条件文档化（部分完成）
- ✅ V29-A 改用宽松关键词 + 字段契约文档
- 未来可加：把 hold 档实际跑 N 天，统计误触发率

---

## 🔴 E. 已完成的 V28 → V28.5 → V29 工作回顾

| 项目 | 状态 | 时间 |
|---|---|---|
| V28 9 项新模块 | ✅ | 22:51 |
| V28.5 4 个真实 bug 修复 | ✅ | 23:02 |
| V28.5 5 池 signal_log 真接入 | ✅ | 23:02 |
| V28.5 V27 prefilter 前置拦截 | ✅ | 23:08 |
| V29-A hold 档宽容化 + 字段契约 | ✅ | 23:18 |
| V29-B CLI 查看入口 | ✅ | 23:18 |
| V29-C 待办清单（本文档） | ✅ | 23:18 |

---

## 🎯 我对下一周的建议

> "A 股系统的有效性是慢变量，跑得太快反而错。"
> — 朋友 1

**V28 → V28.5 已上手 1.5 小时，写了 12 个新模块、修了 5 个真实 bug**。

**接下来 1 周不写新代码**，只做：
1. 每天盘后跑一次端到端，确认 5 池信号都入 signal_log
2. 用 `python -m core.cli_view_recommendation` 人工 review 战法推荐
3. 用 V28 的 `data/signal_backtest.stat_strategy_effectiveness` 出每日战法胜率
4. 等 7 天数据稳定后（V29 第 2 周）再决定下一步

**永远不要再 V28 → V28.5 那种节奏**（1.5 小时写 12 模块）。
