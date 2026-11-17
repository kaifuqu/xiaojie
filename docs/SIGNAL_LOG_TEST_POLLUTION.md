# signal_log / AI 日志 / 企微推送「测试污染」清单与处置（2026-10-18 上线前全功能测试）

> 本文档已按 V30.9 QA 副作用隔离修复结果重写。旧版本「只污染 signal_log、且建议不还原历史行」的结论**已作废**，因为后续只读复核发现真实副作用范围更大，且当时的判断有误。

---

## 0. 一句话结论

- 生产 `signal_log` 被前几轮 UI / 实验室点检写入 **43 行**（`created_at` 均为 2026-10-18），其中 **9 行按 `signal_uid` 主键 UPSERT 覆盖改写了 10-16 的真实盘后信号**。
- 污染**不止 signal_log**：同批点检还真实发出了 **13 条企微推送**、触发了 **13 次 DeepSeek 计费调用**，并在 4 张派生表写了非交易日衍生物。
- **根因已修复**（V30.8 + V30.9 三道只读旁路开关），并有 34 + 127 项永久回归断言防复发；修复后最新一轮 20 套回归的前后只读快照比对 **总变化项 = 0**。
- **存量污染尚待清理**：需要用户明确授权后执行单事务 DELETE/UPDATE，可完整回滚。

---

## 1. 根因

| # | 根因 | 说明 | 修复 |
|---:|---|---|---|
| 1 | **点检直接跑生产扫描，且无测试模式** | `_qa_logs/_ui_menu_probe.py`、`_qa_logs/_lab_tab_probe.py` 会真实触发 P1-P5 扫描；`save_signal_log` 无任何开关 | `core/scan/intraday_snapshots.py:132` 新增 `XJ_NO_SIGNAL_PERSIST`（短路点在 try 之外、取写连接之前） |
| 2 | **推送 / AI 链路无任何旁路** | `master_control.wechat_push_enabled=True` 是生产实盘配置；点检走到 `notify_scan_results_top3 → push_stock_if_allowed → _maybe_get_deepseek_advice`，等于真发企微 + 真调 DeepSeek | 新建 `core/qa_switches.py`；`gateway.py` 4 处短路（股票推送入口 / **系统告警入口且早于账本写入** / markdown 发送 / 系统告警发送）、`deepseek.py` 2 处短路（调用入口 / AI 日志落库入口）；全部 fail-open |

---

## 2. 污染范围（只读复核，2026-10-18 09:57 复核）

### 2.1 signal_log：43 行污染

| 分类 | 行数 | 特征 |
|---|---:|---|
| 纯新增测试行 | 34 | `name` 是 `🐘 600011` 这类「emoji + 6 位代码」回显；`created_at` 全为 2026-10-18 07:27 / 08:16 |
| 覆盖改写真实信号 | 9 | 按 `signal_uid` 主键 UPSERT，`score` / `name` / `strategies` / `score_breakdown` / 止损止盈价等 18 个字段全部被改写 |
| 真实信号（未被污染） | 1 | 600301 华锡有色（10-16 10:58 原始写入） |

被覆盖的 9 条真实信号（uid 保留，值可从 PRE 备份逐列还原）：

| uid | 股票 | 真实分数 | 被改成 |
|---|---|---:|---:|
| 20261016-p5-000001 | 平安银行 | 83.56 | 79.82 |
| 20261016-p5-001872 | 招商港口 | 92.17 | 97.95 |
| 20261016-p5-600011 | 华能国际 | 71.95 | 81.15 |
| 20261016-p5-600577 | 精达股份（600577） | 58.62 | 59.63 |
| 20261016-p5-600621 | 华鑫股份 | 38.31 | 55.11 |
| 20261016-p5-600938 | 中国海运（PRE 名为「XD中国海」） | 77.17 | 76.47 |
| 20261016-p5-601988 | 中国银行 | 95.37 | 100.07 |
| 20261016-p5-603355 | 莱克电气 | 45.40 | 35.53 |
| 20261016-p5-688617 | 惠泰医疗 | 46.71 | 30.99 |

> 10-16 真实 P5 共 10 条（含未被污染的 600301）。清理后期望：`signal_log` 137 → **103**，10-16 P5 恢复为 10 条中文名。

### 2.2 连带污染：signal_strategy_ledger

9 个 uid 的战法明细也被改写，直接影响「战法胜率面板」的历史基线：

| uid | 真实明细（PRE） | 污染后库内 |
|---|---|---|
| 600577 | P5-13A / 13B / 13C | P5-13(测试) / 13A / 13B —— **13C 丢失** |
| 001872 | P5-13A / 13B / 13C | P5-12B(测试) / 13B / 13C —— **13A 丢失** |
| 600938 | P5-12B / 13B / 13C | P5-13(测试) / 13A(测试) / 13B —— **12B、13C 丢失** |
| 688617 | P5-13A / 13B / 13C | P5-13(测试) / 13A / 13B —— **13C 丢失** |
| 603355 | P5-13A / 13B / 13C | P5-05A(测试) / 13A —— **13B、13C 丢失** |

清理后期望：340 → **253**（与 PRE 中非 10-17 明细完全一致；10-17 隔离的 25 条明细保持不动）。

### 2.3 四张表的非交易日衍生物

| 表 | 污染行 | 特征 | 清理后期望 |
|---|---:|---|---:|
| `strategy_effectiveness` | 18 | `stat_date='2026-10-18'`（周日，非交易日） | 51 → 33 |
| `p3_first_trigger_snapshot` | 3 | `trade_date='2026-10-18'` | 30 → 27 |
| `fact_ai_analysis_log` | 76 | `trade_date='2026-10-18'`，全部 `success=True`，累计计费耗时约 66 秒 | 160 → 84 |
| `vw_ai_analysis_debug` | 76 | 同上 | 160 → 84 |

### 2.4 企微推送与去重账本

| 副作用 | 证据 | 处置 |
|---|---|---|
| 真实企微推送 13 条 | 07:22 三条 P4 观察推送；09:32 十条 P5 推送 | 无法撤回；已加旁路防复发 |
| 真实 DeepSeek 调用 13 次 | AI 日志 `success=True`，累计 66 秒 | 无法撤回；已加旁路防复发 |
| `wechat_signal_dedup_cache.json` 被写入 3 条测试键 | `20261018:p4_obs:001872 / 601328 / 601398` | **已清理**，现 9 条生产键（10-12 / 10-13 真实 P3 推送） |
| `alert_dedup_cache.json` | 检查后确认 12 条全部为生产告警，无测试键 | 未动 |

---

## 2.5 额外发现的 P0：signal_log_uk_v29 损坏索引导致 DELETE 必然崩溃

在 2026-10-18 清理演练（只对 `_qa_backup` 副本操作）中发现：
`DELETE FROM signal_log` 必然触发 DuckDB FATAL 并 abort 整个进程。

| 项 | 内容 |
|---|---|
| 现象 | `Invalid Input Error: Failed to delete all rows from index. Only deleted 24 out of 34 rows.` 随后 `FatalException: database has been invalidated`，进程以 `0xC0000409`（-1073740791）退出，无 Python traceback |
| 影响面 | 任何 `DELETE FROM signal_log`（含 `DELETE ... WHERE` 多行）与 `UPDATE` 主键列；单行 `DELETE WHERE id=?` 也会在缺少索引条目时报 `Only deleted 0 out of 1 rows` |
| 未影响 | `INSERT` / `INSERT ... ON CONFLICT DO UPDATE`（走主键）全部正常；生产信号写入链路实测无异常 |
| 精确范围 | 137 行中 10 行在索引里查无条目，均为 2026-10-18 点检写入：`600391 / 601155 / 600674 / 002244 / 601398 / 600256 / 601921 / 601919 / 601939 / 600036` |

### 根因

`data/signal_logger.py::_normalize_signal_log_pk` 用「复制到新表 + 交换」重建主键
`(trade_date, ts_code)` → `(trade_date, pool, ts_code)`，但重建前没有 `DROP` 旧表上的
`signal_log_uk_v29`。`DROP TABLE` 未把同名索引从 catalog 一并清除，
随后的 `CREATE UNIQUE INDEX IF NOT EXISTS signal_log_uk_v29` 因「名字已存在」被静默跳过 ——
于是新表实际没有该索引，catalog 却声称有，形成物理条目缺失的损坏索引。

### 修复

| # | 修复 | 位置 |
|---:|---|---|
| 1 | 重建表前显式 `DROP INDEX IF EXISTS signal_log_uk_v29`（失败仅 debug，不阻断） | `data/signal_logger.py:896`（`_normalize_signal_log_pk`） |
| 2 | 清理脚本在删除前 DROP、删除后原样重建，并在事务内做真实探针验证 DELETE 路径 | `_qa_logs/_purge_pollution_v2.py` |
| 3 | 新增第九批永久回归（13 项断言，全部只在副本上跑） | `tools/_test_index_integrity.py` |

> `signal_log_uk_v29` 与主键 `PRIMARY KEY(trade_date, pool, ts_code)` 列完全相同，属**冗余**索引。
> 删除它不改变 `ON CONFLICT` 语义（仍由主键保证），已在副本上实测验证（X4）。

## 3. 边界校验（防止误删真实数据）

1. 43 行 `name` **全部**匹配 `^emoji + 6 位代码$`，0 例外。
2. 43 行 `pool` 全为 `p5`，`trade_date` 全为 `2026-10-16`。
3. 43 行 `id` 与库内 94 条真实行**零交集**。
4. 34 条纯新增 uid 与 PRE 备份**零交集**（确认为测试新增，非历史信号）。
5. 9 条被覆盖行的真实值可从 `_qa_backup/db_before_calfix_20261017_215750.duckdb` 逐列还原。
6. `signal_pnl`(223)、`signal_log_quarantine`(9)、`dim_trade_calendar`(1111) **不动**。

---

## 4. 清理方案

### 4.1 脚本（单事务 + 19 项逐条断言）

`_qa_logs/_purge_pollution_v2.py`（**当前 `DRY = True` 干跑态**）。

> 副本全流程演练（`DRY=False` 打副本）已跑通：23 项断言全 PASS，退出码 0。

执行序列（删除前后各一次索引处置）：

0. 事务外 `DROP INDEX IF EXISTS signal_log_uk_v29`（绕开损坏索引，DuckDB 的 DDL 在事务内延后生效）
1-5. 单事务：还原 9 条真实 `signal_log` / 删 34 条测试行 / 删回填 `signal_strategy_ledger` / 删 4 表非交易日衍生物
6. `COMMIT` → 事务外 `CREATE UNIQUE INDEX IF NOT EXISTS signal_log_uk_v29` → 23 项断言

1. 按 PRE 备份逐列 `UPDATE` 还原 9 条真实 `signal_log`
2. `DELETE FROM signal_log WHERE id IN (34 个测试 id)`
3. `DELETE FROM signal_strategy_ledger WHERE signal_uid IN (34 个纯测试 uid)`
4. `DELETE` 9 个覆盖 uid 的多余明细 → 从 PRE 重建 7 条真实明细
5. `DELETE` 四张表中 `2026-10-18` 非交易日行
6. `COMMIT` → 19 项断言

### 4.2 回滚点

`_qa_backup/pre_final_purge_20261018_095921/`

- `quant_data.duckdb`：192,950,272 字节，MD5 `77285BA55245307D439674A6134CB2B9`（与清理前主库逐字节一致）
- `runtime/`：全部 55 个运行态文件
- `wechat_signal_dedup_cache.json`：去重账本清理前副本

### 4.3 授权状态

**未获授权，未执行任何生产库写操作。** 需要用户回复「授权执行生产库污染清理」。

---

## 5. 防复发（永久回归）

| 套件 | 断言数 | 覆盖 |
|---|---:|---|
| `tools/_test_no_signal_persist.py` | 34 | 开关真值表、短路位置、早于取写连接、点检脚本内置、生产链路不误设 |
| `tools/_test_no_side_effects.py` | 127 | 开关开=0 HTTP / 0 账本写入 / 0 AI 日志；开关关=生产行为不变；fail-open；点检与总跑器内置 |
| `_ui_menu_probe.py` / `_lab_tab_probe.py` | — | 已内置三道闸 |
| `tools/_release_full_run.py` | — | 总跑器默认只读演练模式 |

**验证证据**：修复后重跑 20 套回归，跑前/跑后只读快照比对 —— 8 张表（`signal_log` / `signal_strategy_ledger` / `strategy_effectiveness` / `p3_first_trigger_snapshot` / `fact_ai_analysis_log` / `vw_ai_analysis_debug` / `signal_pnl` / `dim_trade_calendar`）计数与时间戳、5 个运行态文件（两个去重账本、P1 缓存等）大小与 mtime **全部一致，总变化项 = 0**。
