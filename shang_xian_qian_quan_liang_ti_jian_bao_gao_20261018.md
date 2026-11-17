# 小杰选股系统 · 上线前全功能测试报告

**测试日期**：2026-10-18（周日）
**行情基准日**：2026-10-16（周五，最近交易日）
**下一交易日**：2026-10-19（周一）
**测试范围**：D:\xiaojie 全系统，静态 / 数据 / 策略 / UI / 旁路 / 端到端 6 级
**报告版本**：v2.0（V30.9 QA 副作用隔离收尾后重写）

---

## 0. 结论摘要

| 项目 | 结果 |
|---|---|
| 回归套件总数 | **20 套全部 PASS，退出码全部为 0** |
| `verify_system.py` 系统体检 | **80 通过 / 0 失败 / 0 警告** |
| `tools/db_inspector.py` 数据体检 | **9 项全 OK，0 CRIT / 0 WARN，ok=True** |
| A 股规则符合性 | **148 项断言全通过**（基础 78 + 扩展 70） |
| QA 副作用隔离回归 | **127 项全通过**（开关开=0 副作用，开关关=生产行为不变） |
| 不落库开关回归 | **34 项全通过** |
| 回归跑前 / 跑后只读快照比对 | **总变化项 = 0**（8 张表 + 5 个运行态文件全部一致） |
| 本轮修复的 Bug | **6 个**（3 个 P0 + 3 个 P1） |
| **当前上线判定** | **NO-GO —— 唯一阻塞项：生产库测试污染尚未清理（需用户一句明确授权）** |

### 一句话结论

**代码层已经干净：在已验证范围内无已知代码错误，20 套回归 + 2 套体检全绿，且本轮测试对生产数据与外部服务零副作用（前后快照 0 变化）。**
**唯一阻塞在数据层**：前几轮点检曾真实触发 13 条企微推送、13 次 DeepSeek 计费调用，并把 43 行测试信号写进生产库（其中 9 行覆盖改写了 10-16 的真实盘后信号）。清理脚本已干跑验证 3 遍、物理备份已就绪，但生产库 DELETE/UPDATE 必须由你明确授权后才执行。

---

## 1. 自动化测试结果总表（20 套）

证据文件：`_qa_logs/release_full_20261018_095208.log` / `_qa_logs/release_full_20261018_095208.json`

| # | 套件 | 脚本 | 通过 | 失败 | 耗时 | 判定 |
|---:|---|---|---:|---:|---:|---|
| 1 | 静态编译全量 | `_qa_logs/_compile_all.py` | 626 文件 | 0 | 1.1s | PASS |
| 2 | UI 逐菜单点检 | `_qa_logs/_ui_menu_probe.py` | 68 | 0 | 157.4s | PASS |
| 3 | 回归1（核心/配置/闸门/OOM） | `tools/_test_release_regressions.py` | 178 | 0 | 3.1s | PASS |
| 4 | 回归2（选股契约/ST/模拟盘/DB） | `tools/_test_release_regressions2.py` | 123 | 0 | 1.5s | PASS |
| 5 | 回归3（派生列/幂等/覆盖率） | `tools/_test_release_regressions3.py` | 27 | 0 | 23.8s | PASS |
| 6 | 回归4（NULL 语义/新鲜度闸） | `tools/_test_release_regressions4.py` | 95 | 0 | 1.6s | PASS |
| 7 | 回归5（hk_vol 量能评分） | `tools/_test_release_regressions5.py` | 25 | 0 | 1.0s | PASS |
| 8 | 回归6 未定义名（P5/relay/AST） | `tools/_test_release_regressions6.py` | 34 | 0 | 2.8s | PASS |
| 9 | 数据覆盖 | `tools/_test_data_coverage.py` | 24 | 0 | 0.9s | PASS |
| 10 | 模拟盘（买入 -3% 抛 / 涨 +15% 抛） | `tools/_test_paper_trading.py` | 16 | 0 | 2.3s | PASS |
| 11 | A 股规则（基础） | `_qa_logs/_test_ashare_rules.py` | 78 | 0 | 0.9s | PASS |
| 12 | A 股规则（扩展） | `_qa_logs/_test_ashare_rules2.py` | 70 | 0 | 0.9s | PASS |
| 13 | 守护闸故障注入 | `tools/_test_daemon_gate_fault.py` | 13 | 0 | 0.9s | PASS |
| 14 | P1 量能（日线倍量） | `tools/test_p1_vol_sun.py` | 6 | 0 | 0.8s | PASS |
| 15 | 冒烟全量 | `tools/_smoke_full.py` | 全绿 | 0 | 1.2s | PASS |
| 16 | 推送去重端到端 | `_qa_logs/_e2e_dedup2.py` | 9 | 0 | 0.6s | PASS |
| 17 | 进程互斥端到端 | `_qa_logs/_e2e_mutex3.py` | 13 | 0 | 2.5s | PASS |
| 18 | 列表日链路（次新股） | `tools/_test_list_days_chain.py` | 7 | 0 | 1.1s | PASS |
| 19 | 不落库开关 | `tools/_test_no_signal_persist.py` | 34 | 0 | 1.4s | PASS |
| 20 | **无外部副作用（V30.9 新增）** | `tools/_test_no_side_effects.py` | 127 | 0 | 0.8s | PASS |

**关于「断言总数」的诚实说明**：不要把上表 PASS 列相加当成总断言数。`数据覆盖` 在回归3 内被重复调用，`模拟盘` 在同一日志中前后各打印一次统计行。任何「约 868 项」之类的加总都是重复计数，本报告不再给出该数字。

---

## 2. 本轮 6 级测试逐级结论

| 级别 | 覆盖内容 | 证据 | 判定 |
|---|---|---|---|
| 1 静态 | 全量 `.py` 编译 + 全模块导入探测 | 626 文件 0 失败；回归6 AST 未定义名 34 项 | PASS |
| 2 数据层 | `db_core` / `fetcher` / `hithink_*` / `tushare_rate_limiter`；DuckDB 连接泄漏、fetchdf 空表/缺列、WAL 合并 | `tools/_test_data_coverage.py` 24 项；`_smoke_full.py` 第 7 项 snapshot.db_bytes == total_bytes；回归2 DuckDB 连接契约 | PASS |
| 3 策略层 | `core/strategies` 全策略类实例化 + 小样本跑通；`scan` / `pool_manager` / `signal_filter` / `regime_analyzer` / `event_filter` / `limit_quality_3d` / `dragon_tiger_seat` / `exit_strategy` | 回归1–5 共 448 项；隔离生产参数扫描 P1-P5 全有结果 | PASS |
| 4 UI 逐菜单 | 五档按钮 P1-P5（含回稳）、♻️刷新大盘、刷新指数熔断、刷新战法面板、磁盘刷新、刷新实时板块、radio/selectbox 全部选项穷举、侧栏可点条目 | `_ui_menu_probe.py` 68 项 0 exception 0 error | PASS |
| 5 旁路 | `run_lab.py` 全 Tab；`auto_sniper_daemon.py` 启动链路与闸门故障注入；`backtest_runner.py` 全市场秒级回测 | `_lab_tab_probe.py`；`tools/_test_daemon_gate_fault.py` 13 项；回测实测 eligible=180 hit_rate=17.22% | PASS |
| 6 端到端 | `tools/_e2e_judge.py` 全链路；daemon 与 UI 进程互斥；企微推送去重 | `_e2e_mutex3.py` 13 项；`_e2e_dedup2.py` 9 项 | PASS |

---

## 3. A 股规则符合性核查（148 项断言，只核查不改逻辑）

### 3.1 涨跌停幅度体系（全部符合）

| 板块 | 涨跌幅 | 前收 10 元时涨停/跌停 | 断言 | 判定 |
|---|---:|---:|---:|---|
| 主板普通股 | ±10% | 11.00 / 9.00 | 4/4 | PASS |
| 主板 ST/*ST | ±5% | 10.50 / 9.50 | 4/4 | PASS |
| 创业板 300/301 | ±20% | 12.00 / 8.00 | 4/4 | PASS |
| 创业板 ST（**仍 ±20%**） | ±20% | 12.00 / 8.00 | 4/4 | PASS |
| 科创板 688 | ±20% | 12.00 / 8.00 | 4/4 | PASS |
| 北交所 920/43/83/87 | ±30% | 13.00 / 7.00 | 4/4 | PASS |

> 创业板/科创板 ST 股不适用主板 ST 的 5% 规则，系统实现正确。

### 3.2 交易单位与最小价位

| 板块 | 最小买入 | 断言 | 判定 |
|---|---:|---:|---|
| 主板 / 北交所 | 100 股 | 4/4 | PASS |
| 创业板 / 科创板 | 200 股起 | — | PASS |

- 最小价位 0.01 元、round-half-up：10.005→10.01、10.004→10.00 —— PASS
- 涨跌停用 `Decimal` 计算避免浮点误差：10.45→9.41、10.55→9.50 —— PASS

### 3.3 交易日历与时段（实测真实数据）

| 核查项 | 实测 | 判定 |
|---|---|---|
| 国庆休市 `is_open` | 10-01 ~ 10-07 全 `False` | PASS |
| 日历覆盖 | 2023-10-04 → 2026-10-18，1,111 天，开市 733 天 | PASS |
| daily_data 日期 ⊆ 开市日 / 开市日 ⊆ daily_data | 双向缺失均为 `[]` | PASS |
| 最新行情日与日历一致 | 2026-10-16 `is_open=True` | PASS |
| `next_trade_date` 推导 | 0 条缺失 | PASS |

### 3.4 交易时段

| 时点 | 判定 | 说明 |
|---|---|---|
| 09:14 / 09:15 | 未开盘 / 集合竞价开盘 | PASS |
| 11:30 / 11:31 | 上午收盘仍在窗内 / 午休已停 | PASS |
| 13:00 / 14:57 / 15:00 / 15:01 | 下午开盘 / 收盘竞价窗内 / 收盘窗内 / 已收市 | PASS |

时段定义：上午 `555-690`、下午 `780-900`，实现于 `core/paper_trading/daemon_hooks.py`。

### 3.5 新股 / 停牌 / ST / 退市整理期

| 场景 | 实测 | 判定 |
|---|---|---|
| N 名称首日无涨跌幅、第 6 日恢复 ±10% | 1009→1015 恢复 | PASS |
| U 名称次日有涨跌幅 / C 名称第 2-5 日 / D 名称 | 均正确 | PASS |
| 科创板新股前 5 日无涨跌幅 | 正确 | PASS |
| 北交所新股次日即有涨跌停 | 正确 | PASS |
| 停牌股不按涨跌停处理（`pre_close<=0` 拦截） | 正确 | PASS |
| ST / *ST / PT / 退市整理期识别与 ±5% | 正确，老股无误判 | PASS |

### 3.6 前复权与量额口径

| 核查项 | 实测 | 判定 |
|---|---|---|
| qfq 关键字段空值 | 0 | PASS |
| 单日 OHLC 合法性（`high >= max(o,c)`） | 000029.SZ h=21.20 o=20.96 c=20.52 l=20.33 | PASS |
| `pre_close > 0` | 20.7 | PASS |
| 量比 / 成交额口径 | 一致 | PASS |

### 3.7 A 股规则侧的非阻塞发现

| 项目 | 现状 | 结论 |
|---|---|---|
| 北交所行情覆盖 3.4% | `daily_data` `.BJ` 仅 12 只 / 894 行，`stock_basic` 有 356 只 | 系统已正确排除（`get_p1_candidate_codes` / `regime_analyzer` 均不含 `.BJ`，P1 候选 1,065 只中 `.BJ`=0）。若将来要纳入需先补齐行情 —— P2 建议 |

---

## 4. 数据层体检结果

### 4.1 主库数据规模（`verify_system.py` + `db_inspector.py` 实测）

| 表 | 行数 | 说明 |
|---|---:|---|
| `daily_data` | 222,834 | 137 个交易日（2026-03-25 → 2026-10-16） |
| `stock_basic` / `dim_security` | 5,916 | 主板 3,488 / 创业板 1,451 / 科创板 621 / 北交所 356 |
| `feat_daily_core` / `capital` / `memory` | 各 44,600 | 29 个交易日，与 daily_data 完全对齐 |
| `vw_daily_data_compat` | 222,834 | legacy 0 漂移 |
| `dim_trade_calendar` | 1,111 | 开市 733（**这是修复后的正确格式，禁止用旧备份整体回滚**） |
| `dim_index_valuation` | 4,398 | 最新 2026-10-16，6 个核心指数 |
| `fact_financial_reports` | 1,454 | ann_date 最新 2026-09-10 |
| `signal_log` | **137** | 含 43 行测试污染，见第 6 节 |
| `signal_pnl` | 223 | 最新 eval_date 2026-10-16 |
| `paper_account` | 1 | PAPER_MAIN，100 万初始资金，RUNNING |

### 4.2 关键字段完整性（0 CRIT / 0 WARN）

| 字段 | 行数 | NULL | 0 值 | 最小 | 最大 | 状态 |
|---|---:|---:|---:|---:|---:|---|
| `total_mv` / `circ_mv` / `adj_factor` | 222,834 | 0 | 0 | — | — | OK |
| `close` / `vol` / `amount` / `pre_close` | 222,834 | 0 | 0 | — | — | OK |
| `pe_ttm` | 222,834 | 1,404 | 27,090 | 0 | 150,890 | OK（NULL 为亏损股） |
| `pb` / `ps_ttm` | 222,834 | 8 | 82 / 298 | 0 | 1,315 / 25,642 | OK |
| `dv_ratio` | 222,834 | 2,426 | 44,331 | 0 | 18.2985 | OK |

### 4.3 需知晓的运维事项（非阻塞）

| 项目 | 说明 | 结论 |
|---|---|---|
| `daily_data` 有 11 个「缺口日」 | 20260406、20260501、20260504、20260505、20260619、20260925、20261001–06 | **全部是法定节假日休市日**，`dim_trade_calendar` 已标 `is_open=False`，非丢数据 |
| `p1_cache` 缺 20261014 | `vs daily_data missing=131 extra=0`，说明当日未产出缓存而非丢数据 | 上线首日实跑会补齐 |

---

## 5. 本轮发现并修复的问题清单

### 5.1 P0 级（严重，必须修复）—— 全部已修复并回归

| # | 问题 | 位置 | 影响 | 修复方式 | 回归证据 |
|---:|---|---|---|---|---|
| 1 | **`res["detail"]` 被整体重写，静默清空 4 个关键字段** | `core/strategies/strat_p5_postmarket.py:641` | P5 结果丢失 `fin_quality_score`（财务质量分）、次日开盘溢价/标签、`market_overheat_delta`；UI 看不到财务质量，下溢价决策失效 | 改为先快照 `_early_detail_p5 = dict(res.get("detail") or {})` 再 `.update()` 合并 | 回归6 `U1g/U1h/U1i` 全 PASS |
| 2 | **信号接力跨日判定用本地时区而非北京时间** | `core/p2_p5_signal_relay.py:70` | 非 CST 环境下业务日算错，双池命中加分失效 | 统一用 `_today_bj_yyyymmdd()` | 回归6 `U2b2/U2c/U2g` 全 PASS |
| 3 | **QA 点检会真实发企微 + 真实调 DeepSeek（13 条推送 / 13 次计费）** | `core/notification/gateway.py`、`core/notification/deepseek.py` | `master_control.wechat_push_enabled=True` 时点检直接走生产推送链路，07:22 发 3 条 P4 观察推送、09:32 发 10 条 P5 推送并触发 10 次 DeepSeek（约 66 秒计费耗时），AI 日志被写脏 | 新建 `core/qa_switches.py` 统一开关中枢；gateway 加 4 处短路（股票推送入口 / **系统告警入口且早于账本写入** / markdown 发送 / 系统告警发送）；deepseek 加 2 处短路（调用入口 / AI 日志落库）。全部 fail-open，生产不设变量时行为完全不变 | `tools/_test_no_side_effects.py` 127 项全 PASS；`_ui_menu_probe.py` / `_lab_tab_probe.py` 已内置三道闸；`tools/_release_full_run.py` 默认只读演练；跑前/跑后快照总变化项 = 0 |

### 5.2 P1 级（重要）—— 全部已修复并回归

| # | 问题 | 位置 | 影响 | 修复方式 | 回归证据 |
|---:|---|---|---|---|---|
| 4 | **扫描异常完全不可观测**：单票/单池异常只 warning 一句，扫描仍判成功 | `core/scan/pipeline.py` | 「选出 0 只」无法区分「真没有」与「崩了」 | 新增 `_scan_errors` / `_skipped_stocks` / `_scan_error_summary` / `_scan_error_total` + `_record_scan_error()` / `_finalize_scan_errors()`，两处 warning 分支接入 | 回归6 `U9` 八项全 PASS；隔离扫描 `_scan_error_total == 0` |
| 5 | **UI / 实验室点检污染生产 `signal_log`** | `core/scan/intraday_snapshots.py:132` | 点检触发真实 P2-P5 扫描，`save_signal_log` 按主键 UPSERT，43 行测试信号进生产库，9 行覆盖真实盘后信号 | 新增 `XJ_NO_SIGNAL_PERSIST` 短路（位置在 try 之外、取写连接之前）；`tools/_test_no_signal_persist.py` 34 项永久回归 | 34/34 PASS；回归跑后 `signal_log` 计数与时间戳未变 |
| 6 | **QA 脚本自身缺陷** | 多个测试脚本 | `pool_key` 列名错误导致体检脚本崩溃；GBK 控制台 emoji 输出 `UnicodeEncodeError` 中断 | 列名 `pool_key`→`pool`；脚本统一 `sys.stdout.reconfigure(encoding="utf-8", errors="replace")` | 20 套跑完 0 中断 |

### 5.3 P2 级（优化建议，非阻塞）

| # | 项目 | 现状 | 建议 | 优先级 |
|---:|---|---|---|---|
| 1 | **回测预设 period 与数据覆盖不匹配** | `core/backtest_runner.py` 预设 `tech_rally` / `bluechip_accel` 定义在 2024-2025，`daily_data` 只覆盖 2026-03-25~2026-10-16 | 要么把预设 period 改到 2026-03~2026-10，要么补历史数据；当前直接跑预设必然 0 样本 | 高 |
| 2 | **北交所行情覆盖 3.4%** | `.BJ` 仅 12 只 / 894 行，`stock_basic` 356 只 | 系统已正确排除，不影响主流程；若要纳入需先补齐行情 | 中 |
| 3 | **`dv_ratio` 2,426 个 NULL** | 占 1.09% | 疑为未分红公司，NULL 语义正确；若要展示可填 0 | 低 |
| 4 | **sniper.log 3 类历史错误**（10-11/10-13/10-17） | `duckdb_vacuum_silent()` 参数不匹配、DuckDB 独占锁、双实例抢锁 | 均已在 10-13~10-17 修复（回归2 有专门断言）；上线后首周观察日志 | 中 |

---

## 6. 上线阻塞项：生产数据清理（唯一阻塞）

### 6.1 阻塞说明

`verify_system.py` P0-14 显示 `[OK] P5 20261016 选中 8 只，signal_log 已落 44 条 P5 正式信号`。
**这个 OK 是污染造成的假阳性**：真实 10-16 盘后 P5 只有 **10 条**中文名信号，另外 **34 条**是测试新增、**9 条**是测试覆盖改写后的「代码回显名」。必须在清理后才能作为上线依据。

### 6.2 污染范围（只读复核，2026-10-18 09:57）

| 表 / 文件 | 现状 | 清理后期望 | 性质 |
|---|---:|---:|---|
| `signal_log` | 137 | 103 | 删 34 条纯测试；按 PRE 备份逐列还原 9 条真实信号 |
| `signal_strategy_ledger` | 340 | 253 | 删 94 条测试明细；重建 7 条真实明细（10-17 隔离的 25 条保持） |
| `strategy_effectiveness` | 51 | 33 | 删 `stat_date=2026-10-18` 非交易日行 18 条 |
| `p3_first_trigger_snapshot` | 30 | 27 | 删 `trade_date=2026-10-18` 3 条 |
| `fact_ai_analysis_log` | 160 | 84 | 删 `trade_date=2026-10-18` 76 条（AI 计费脏行） |
| `vw_ai_analysis_debug` | 160 | 84 | 同上 |
| `signal_pnl` | 223 | **223（不动）** | 事后表现回填数据 |
| `signal_log_quarantine` | 9 | **9（不动）** | 10-17 非交易日隔离数据 |
| `dim_trade_calendar` | 1,111 | **1,111（不动）** | 修复后的正确格式 |
| `wechat_signal_dedup_cache.json` | 12 | **9（已完成）** | 已删除 07:22 三条 P4 观察测试键，5 项断言 PASS |

### 6.3 已就绪的执行条件

| 项 | 状态 |
|---|---|
| 清理脚本 | `_qa_logs/_purge_pollution_v2.py`（**已恢复 `DRY = True` 干跑态**），单事务 + 19 项逐条断言 |
| 干跑验证 | 3 次全部通过，期望值与第 6.2 节完全一致 |
| 物理备份 | `_qa_backup/pre_final_purge_20261018_095921/`（主库 192,950,272 字节，MD5 `77285BA55245307D439674A6134CB2B9`，与当前主库逐字节一致 + 全部 runtime 55 个文件） |
| 参照基准 | `_qa_backup/db_before_calfix_20261017_215750.duckdb`（含 10-16 真实 P5 10 条、ledger 非 10-17 明细 253 条） |

### 6.4 需要用户的一句授权

生产库 DELETE/UPDATE 属不可逆生产数据写操作。**请回复「授权执行生产库污染清理」**，我将：

1. 再次干跑确认 → 切 `DRY = False` → 单事务执行
2. 逐项断言 19 条（10-16 真实 P5 恢复为 10 条中文名、平安银行 83.56 / 华能国际 71.95 / 莱克电气 45.4 等分数还原、ledger 无测试 UID、quarantine 与日历不动）
3. 重跑 `verify_system.py` + `tools/db_inspector.py` + 关键回归
4. 只有全部通过，才把结论从 NO-GO 改为 GO

---

## 7. 遗留风险与配置类问题

| # | 项目 | 说明 | 是否阻塞上线 |
|---:|---|---|---|
| 1 | **`config.yaml` 内明文存放 token / api_key / webhook** | 本报告不复述任何明文；风险是文件泄露即凭证泄露 | 否（但建议改为环境变量或本机 ACL 保护） |
| 2 | **DeepSeek / Tushare / HiThink 凭证均已配置** | 上线后会产生真实计费调用（正常业务行为） | 否 |
| 3 | **北交所行情覆盖不足** | 系统已正确排除 | 否 |
| 4 | **模拟盘费率按 A 股实际规则建模**（佣金万 2.5 双边最低 5 元、印花税千 1 卖出单边、过户费万 0.1） | 16 项断言全 PASS | 否 |
| 5 | **实盘推送无独立降噪** | 5 档 × 多战法，日均可能数十条 | 否（建议按告警/重要/观察三档分流） |

---

## 8. 最终结论

**是否达到「每个菜单都能用」：是。**
UI 逐菜单点检 68 项全部 PASS，0 exception / 0 error；五档按钮、刷新类按钮、全部 radio/selectbox 选项穷举、侧栏条目均能打开、非空或有正确空态、不卡死。策略实验室全部 Tab、守护进程闸门、回测引擎、端到端互斥与去重链路全部 PASS。

**是否无错误：在已验证范围内无已知代码错误。**
20 套回归全绿 + 2 套体检全绿 + 148 项 A 股规则断言全通过 + 本轮测试零外部副作用。数学意义上的「绝对零错误」无法证明，本报告只承诺已验证范围。

**剩余阻塞项：1 个 —— 生产数据清理尚未执行（需一句明确授权）。**
清理并复验通过后即可转 GO。清理过程有物理备份兜底，可完整回滚。
