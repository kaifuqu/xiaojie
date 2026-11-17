# V29 字段命名契约（module field contract）

> 目的：避免 V28 上线时"is_subnew vs strategies 含'次新'"这种字段名歧义再次发生。
>
> 规则：每个 V28+ 新模块的入口函数 docstring 第一行，必须列出它**消费**（读取）的字段名 + **产出**（写入 res）的字段名。

---

## `core.exit_strategy`

### `pick_exit_template(res) -> str`

**消费字段**（从 res 读）：
- `pool` (str, "p1"/"p2"/"p3"/"p4"/"p5") — 触发宽止损（p4 永远 wide）
- `burst_score` 或 `score` 或 `p1_score` (float) — 触发 hold（≥88）
- `strategies` (list[str] | str with "|") — 触发关键词匹配
- `risk_tags` (list[str]) — 触发关键词匹配（冗余来源）
- `score_details` (dict) — 读 `题材生命周期` 和 `P1动量/反转分类`

**产出字段**（写入 res）：`exit_template` (str: tight/medium/wide/hold)

### `attach_exit_plan_to_res(res, push_price)`

**消费**：`pool` / `burst_score` / `strategies` / `risk_tags` / `score_details` / `price`

**产出**：
- `exit_plan` (dict) — 完整出场计划（template / stop_loss_price / take_profit_*_price / max_hold_days / rule_text）
- `exit_template` (str) — 选档结果
- `auto_stop_loss` (float) — 与 P4 已有字段对齐
- `auto_take_profit` (float)
- `trailing_stop_rules` (str)

---

## `core.push_template_momentum_reversal`

### `apply_push_template_to_res(res)`

**消费**：
- `score_details["P1动量/反转分类"]` (str: "momentum"/"reversal"/"neutral")
- `strategies` (list/str) — 备用识别

**产出**：
- `push_template` (dict) — 含 track / push_time_hint / hint_emoji / hint_title / hint_body
- `push_track` (str: "momentum"/"reversal"/"neutral")
- `should_push_now` (bool) — 时间窗口兜底判断
- 若 res 没 `exit_template`：根据 track 自动写 medium（动量）/ wide（反转）

### `should_push_now(track, now=None) -> bool`

**消费**：`track` / `now`（datetime, 默认当前北京时间）

**产出**：(bool)

---

## `core.push_dedup`

### `check_dedup(ts_code, pool, score) -> dict`

**消费**：（参数）

**产出 dict**：
- `should_dedup` (bool)
- `reason` (str) — 给用户看的提示
- `last_push` (dict or None) — 上次推送记录
- `score_delta` (float)

### `record_push(ts_code, pool, score)`

**消费**：（参数）

**产出**：写入 `data/push_history.json`

> **【2026-11-03】QA 旁路**：当处于 QA / 演练环境时**不写文件**（直接 return）。
> 判定条件（任一命中即视为 QA，见 `core/push_dedup._qa_mode()`）：
> ① `XJ_NO_SIGNAL_PERSIST` / `XJ_NO_PUSH` / `XJ_NO_DEEPSEEK` 任一为真
> ② DuckDB 主库路径不在 `<root>/data/` 下（即指向 `data/_qa` 副本）
> ③ 显式 `XJ_QA_PUSH_HISTORY=1`
>
> 原因：回归脚本会走 `append_signal_log_safe → pool_apply_push_dedup → record_push`，
> 而本文件路径是硬编码的 `data/push_history.json`（生产账本），
> 历史实测曾把 12 条**测试票**写进生产账本，
> `COOLDOWN_DAYS=3` 的票级冷却会把**真实票**静默拦掉最多 3 天。
>
> 生产环境行为完全不变；判定失败一律 fail-open（当作生产）。

---

## `core.dragon_tiger_seat`

### `compute_dragon_tiger_bonus(lhb_df, ts_code) -> tuple`

**消费**（从 lhb_df 读）：
- 列：`ts_code` 必选
- 列：`side`/`side_type`/`buy_or_sell` — 区分买/卖
- 列：`exalter`/`营业部`/`branch_name`/`name` — 识别席位类型
- 列：`net_buy`/`buy_amount`/`net_amount` — 净额

**产出**：`(bonus: float, label: str, detail: dict)`

### `compute_dragon_tiger_with_fallback(rt, db_conn, trade_date) -> tuple`

**消费**：rt["lhb_df"] or rt["dragon_tiger_df"]，或从 db_conn 拉

**产出**：**(bonus, label, detail) 三元组**（V28.5 修复后保持一致）

---

## `core.theme_lifecycle`

### `classify_lifecycle(pct_5d_sum, pct_20d_sum, limit_up_count=0) -> str`

**消费**：（参数）

**产出**：状态字符串 `import`/`rising`/`climax`/`ebbing`/`chaos`

### `get_industry_lifecycle(db_conn, industry, lookback_days=30) -> dict`

**消费**：db_conn 中的 `daily_data` + `dim_security`

**产出 dict**：
- `state` (str)
- `score_bonus` (float)
- `label` (str)
- `n_limit_up` (int)
- `pct_5d_mean` (float)
- `pct_20d_mean` (float)
- `n_stocks` (int)

---

## `core.event_filter`

### `compute_event_penalty(df, rt, list_date, ts_code, name) -> tuple`

**消费**：df（pct_chg / bias_20 / vol_ratio 列） + rt.name

**产出**：`(penalty_score, label, should_veto)` — V28.5 触发 veto 会被 P1 前置踢出

---

## `core.limit_quality_3d`

### `compute_limit_quality_3d(rt) -> tuple`

**消费**：
- rt["limit_seal_time"] (str, HHMMSS or HH:MM:SS)
- rt["limit_open_count"] (int)
- rt["limit_seal_amount"] (float, 元)
- rt["sector_limit_count"] (int)

**产出**：`(bonus, label)`

---

## `core.strategy_weight_optimizer`

### `optimize_strategy_weights(db_conn, ..., auto_apply=False) -> dict`

**消费**：db_conn 中 `strategy_effectiveness` 表
(pool, strategy, strategy_label, stat_n_days, stat_date, n_signals, n_evaluated,
 n_win, win_rate, avg_return, avg_excess, avg_max_drawdown, avg_max_profit, available_n_days)

> V29.1 起不再依赖 `t5_win_rate`（该字段在 T+5 未成熟时为 NULL，会把全部聚合行过滤掉）。
> 口径：对每个 `(pool, strategy, stat_n_days)` 只取 `stat_date` 最新的一行；
> 对每个战法取「最新已成熟 N 档」（优先级 10 > 5 > 2 > 1）作为 `primary_n_days`。
>
> 【V30.10 上线前修复】跨档重复计数：同一批信号在 T+2 / T+5 两档各出现一次，
> 旧逻辑把两档 `n_signals` 相加后算加权胜率，导致小样本战法胜率虚高
> （P5-02B 面板 83.9%，真实 T+5 为 50%）。现改为：先按
> `own_primary = max(available_n_days ∪ {stat_n_days})` 为每个战法定出唯一采用档位，
> 聚合时只接受 `stat_n_days == own_primary` 的行，跨池只合并同一 N 档。
> 影响面仅为报表与面板展示数字，不改评分权重、不改策略逻辑。

**产出 dict**：
- `mode` (str: "statistics_only" / "auto_applied") — V28.5 默认灰度
- `weights` (dict[str, float]) — 仅 auto_apply=True 且单战法样本 ≥200 时非空
- `weight_recommendation` (dict[str, dict]) — 始终产出，落盘到 `data/strategy_weight_recommendation.json`
- `deprecated` (list[str])
- `stats` (dict[str, dict]) — 每战法：n_signals / win_rate / primary_n_days /
  available_n_days / missing_n_days / n_evaluated / avg_return / avg_max_drawdown /
  avg_max_profit / pools / strategy_label
- `maturity` (dict) — V29.1 新增：available_n_days / primary_n_days / missing_n_days /
  total_evaluated / signals_total / signals_ready / signals_pending /
  min_n_for_auto_apply / next_auto_apply_remaining / stat_basis / updated_at
- `strategy_maturity` (dict[str, dict]) — V29.1 新增：每战法
  primary_n_days / available_n_days / missing_n_days / n_signals / n_evaluated /
  remaining_to_auto_apply

**调用方**：`auto_sniper_daemon._signal_backfill_pipeline()`（每日 18:30，第三步，
强制 `auto_apply=False`）。

---

## `data.index_daily_store` （V30 新增）

### 为什么需要它

V29 之前，生产库**从来没有 `index_daily` 表**，`daily_data` 里也没有任何指数行。
于是 `signal_backtest._load_benchmark_series()` 恒 `return None`：

```
benchmark_pct / excess_pct  →  永远 NULL
strategy_effectiveness.avg_excess  →  永远 NULL
UI 面板「平均超额%」  →  整列「—」
```

也就是说系统从上线至今，**从没回答过**「这只票 T+2 涨 3%，是大盘也涨了 3% 还是真跑赢了？」

### 常量

| 常量 | 值 | 说明 |
|---|---|---|
| `INDEX_CODES` | `('000001.SH','399001.SZ','399006.SZ','000300.SH')` | 上证 / 深证 / 创业板 / **沪深300** |
| `BLEND_CODES` | `('000001.SH','399001.SZ','399006.SZ')` | 混合基准等权三指数（不含沪深300） |
| `INDEX_NAMES` | dict | 代码 → 中文名 |
| `DEFAULT_LOOKBACK_DAYS` | 130 | 足够覆盖 `MAX_DAYS` 窗口 |
| `DDL_INDEX_DAILY` | — | `PRIMARY KEY (ts_code, trade_date)` |

> 沪深300 **必须保留**：既有 `benchmark_pct` / `excess_pct` / `avg_excess_t5` 都是这个口径，
> 去掉会破坏历史数字可比性。

### 函数契约

| 函数 | 签名 → 返回 |
|---|---|
| `ensure_index_table` | `(db_conn) -> bool`，`CREATE TABLE IF NOT EXISTS`，幂等 |
| `sync_index_daily` | `(db_conn, lookback_days=130, index_codes=None) -> dict`，返回 `{ok, fetched, written, per_index, skipped}`；按主键覆盖，幂等 |
| `sync_index_quietly` | `(db_conn=None, lookback_days=130) -> dict`，**永不抛出**（daemon 调用点） |
| `load_index_panel` | `(db_conn, index_codes=None, start_date=None) -> Dict[ts_code, DataFrame[td, close]]`；表不存在 → `{}` |
| `single_index_return_pct` | `(panel, code, log_date, eval_date) -> float｜None` |
| `blend_return_pct` | `(panel, log_date, eval_date) -> float｜None`，等权平均三指数 |
| `latest_index_quotes` | `(db_conn) -> list[dict]`，含 `close/pct_chg/chg_5d/chg_20d` |

**缺天处理**：目标日不在序列中时取**该日之前最近的可用交易日**（二分查找 `_last_index_at_or_before`）；
早于序列首日才返回 `None`。理由：指数理论上不停市，缺口多为接口抖动。

**NULL 铁律**：算不出 → `None`，**绝不写 0.0**（0.0 会让「超额=绝对收益」冒充超额）。

### 表结构 `index_daily`

`ts_code`(VARCHAR) / `trade_date`(DATE) / `open` / `high` / `low` / `close` / `pre_close` /
`pct_chg` / `vol` / `amount`，主键 `(ts_code, trade_date)`。

写入注意：`trade_date` 源数据是 `YYYYMMDD` 字符串，INSERT 时必须
`STRPTIME(?, '%Y%m%d')::DATE`，否则 DuckDB 抛 `Conversion Error`（V30 踩过）。

---

## V30 新增字段（signal_pnl / strategy_effectiveness）

### `signal_pnl` 新增 11 列

| 列 | 类型 | 含义 |
|---|---|---|
| `bench_sh_pct` / `bench_sz_pct` / `bench_cyb_pct` | DOUBLE | 上证 / 深证 / 创业板 T+N 区间收益 |
| `bench_blend_pct` | DOUBLE | 三指数等权混合基准收益 |
| `excess_sh_pct` / `excess_sz_pct` / `excess_cyb_pct` | DOUBLE | `return_pct - bench_*_pct` |
| `excess_blend_pct` | DOUBLE | `return_pct - bench_blend_pct`（**面板主口径**） |
| `beat_blend` | BOOLEAN | `excess_blend_pct > 0` |
| `board_attributed` | VARCHAR | 归因命中的指数中文名 |
| `board_excess_pct` | DOUBLE | `return_pct - 归属指数收益` |

### `strategy_effectiveness` 新增 5 列

`avg_excess_sh` / `avg_excess_sz` / `avg_excess_cyb` / `avg_excess_blend` / `beat_blend_rate`

### 板块归因映射（`signal_backtest._board_index_for_code`）

| 条件 | 归因指数 |
|---|---|
| `.SH` + `600/601/603/605` | 上证指数 |
| `.SH` + `688/689` | 创业板指（无科创50，暂借用） |
| `.SZ` + `300/301` | 创业板指 |
| `.SZ` 其余（`000/001/002/003`） | 深证成指 |
| `.BJ` / `4xx` / `8xx` / `92x` | `None`（**北交所不产出归因**） |

判定必须看交易所后缀，不能只看 6 位裸代码（`000001` 既是上证指数、也是深市平安银行）。

### 旧列口径**未改动**

`signal_pnl.benchmark_pct` / `excess_pct` / `beat_benchmark` 仍为**沪深300 单指数**口径；
`strategy_effectiveness.avg_excess` / `avg_excess_t5` 保持原样。V30 只加列不动列。

### 扩列机制

`signal_logger._ensure_v30_index_columns(db_conn)` —— 逐列 `PRAGMA table_info` 检查后
`ALTER TABLE ADD COLUMN`，失败即跳过，幂等无副作用。

⚠️ **关键**：`signal_backtest._ensure_backtest_tables()` 也必须调用它
（该函数走 `ensure_schema` 而非 `signal_logger._ensure_tables`），
否则 18:30 回填先于任何推送时新列不存在 → `INSERT` 报 Binder Error，整轮回填失败。

同时该函数补齐了 V29 结构列（`stat_n_days`/`win_rate`/`avg_return`/`avg_excess`/
`avg_max_drawdown`/`avg_max_profit`/`primary_n_days` 等）——推送链路与回填链路
两套 DDL 长期不同步，首次建表会缺列。

---

## V30 快照扩展

`core/strategy_effectiveness_snapshot.build_ui_snapshot()` 新增两个顶层字段：

| 字段 | 说明 |
|---|---|
| `index_quotes` | `[{ts_code, name, trade_date, close, pct_chg, chg_5d, chg_20d}]` |
| `index_coverage` | `{available_n_days, primary_ready, per_index: {ts_code: {n, start, end}}}` |

读取端 `core/strategy_effectiveness_dashboard.load_snapshot()` 归一化为
`snap["index_quotes"]` / `snap["index_coverage"]` / `snap["blend_ready"]`，
老快照无这两个字段时降级为 `[]` / `{}` / `False`，不影响主面板。

---

## V30 面板新增区块

| 函数 | 位置 | 说明 |
|---|---|---|
| `render_market_quotes` | 成熟度条**之前** | 市场水位条：4 指数卡（涨红跌绿）+ 5日/20日；未就绪时给补齐指引 |
| `render_blend_table` | 主表之后 | 混合基准归因表：4 列超额 + `跑赢混合基准%` |
| `render_scatter_chart` | 已改写 | X 轴下拉可切「混合超额%（主口径）/平均收益/上证/深证/创业板」；超额全 NULL 时自动回退平均收益口径并说明 |

---

## V30 生产迁移与核验工具

`tools/v30_migrate_and_verify.py` —— 幂等，可重复运行：

1. `ensure_index_table()` 建 `index_daily`
2. `sync_index_quietly(lookback_days=260)` 同步 4 指数（覆盖 MAX_DAYS=130 窗口 + 缓冲）
3. `signal_logger._ensure_v30_index_columns()` 幂等扩列
4. `run_daily_backfill()` → `optimize_strategy_weights(auto_apply=False)` → `build_ui_snapshot()`
5. 打印 `signal_pnl` / `strategy_effectiveness` 填充率、指数覆盖度、区间收益、快照样本行

**铁律**：
- 只 `CREATE TABLE IF NOT EXISTS` + `ALTER TABLE ADD COLUMN`，绝不 `DROP`、绝不改既有列口径
- 全程 `auto_apply=False`，绝不改任何实盘评分权重
- 需要 DuckDB 写锁：**运行前必须确认 `auto_sniper_daemon` 未运行**（单写者铁律）
- 生产库迁移前先整库物理备份 `data/quant_data.duckdb.bak_v30_<yyyymmdd_hhmmss>`

### `core.strategy_weight_optimizer` 的 V30 扩展

SELECT 列表**纯加 5 列**：`avg_excess_sh / avg_excess_sz / avg_excess_cyb /
avg_excess_blend / beat_blend_rate`。

跨池汇总时**每个基准各有独立的加权累加器与分母**
（`v_sh_num/v_sh_den` … `v_beat_num/v_beat_den`），不做共用分母——
共用分母会让某列缺数时把 0 混进别的基准的均值。分母为 0 → `None`，绝不写 `0.0`。

**旧列零改动**：`avg_excess`（沪深300）、`avg_return`、`win_rate` 的口径与算法完全不变。

---

## V30 daemon 接入

`_sync_benchmark_indices_after_daily()` —— 在 `_evening_sync_only_pipeline()`
日线增量成功**之后**、19:55 P1 **之前**调用。选这个位置的三个原因：

1. 此时 DuckDB 写锁已在本链内持有，不需额外抢锁
2. 指数同步失败只 `logger.warning`，绝不影响 P1 / P5
3. 指数一天一根 K 线（4 × ~130 根 ≈ 8 KB），成本可忽略

### 回归测试

| 命令 | 覆盖 | 规模 |
|---|---|---|
| `python tools/_smoke_index_bench.py` | 18 组：幂等建表 / 覆盖度 / upsert 幂等 / 人工验算 / 单指数收益 / 缺天前向填充 / 全缺 None / 板块归因 / NULL 安全 / 扩列幂等 / 旧口径未变 / 快照容错 / **Timestamp 日期归一** / **旧基准 SQL 锁定沪深300** / **daemon 钩子不再静默跳过** / **start_date 按 DATE 比较** / **行情日期不被截断** / **新旧基准独立加载** | 112 项断言 |
| `python tools/_smoke_strategy_panel.py` | 面板 9 场景回归 | 27 项断言 |

全部使用**内存 DuckDB**（`:memory:`），不碰生产库。

## 各池 strat_*.py 的统一推送钩子（5 池共用）
---

## `core.strategy_effectiveness_snapshot` （V29.2 新增）

### `build_ui_snapshot(db_conn=None, lookback_days=30) -> str`

**消费**：
- `data/strategy_weight_recommendation.json`（优化器刚产出的推荐报表，作为快照主数据源）
- `db_conn` 中 `strategy_effectiveness` 表（**可选**，仅用于采集 `history` 趋势行）

**产出**：`data/strategy_effectiveness_ui_snapshot.json`（原子写：先 `.tmp` 再 `os.replace`），返回绝对路径。

**JSON 结构**：

| 顶层字段 | 类型 | 说明 |
|---|---|---|
| `schema_version` | str | 当前 `"1.0"`；读取端只接受 1.x，其它版本忽略 |
| `generated_at` | str | ISO 时间戳，驱动 24h/72h 新鲜度提示 |
| `stat_date` | str | 最新统计日（`MAX(stat_date)`） |
| `mode` | str | 恒为 `"statistics_only"` |
| `maturity` | dict | 原样透传优化器成熟度块 |
| `strategy_maturity` | dict | 原样透传 |
| `recommendation` | dict[str, dict] | 原样透传（strategy → {weight, reason}） |
| `deprecated` | list[str] | 原样透传 |
| `stats` | dict[str, dict] | 原样透传（strategy → 统计行） |
| `history` | list[dict] | 近 30 天每日行；采集失败降级为 `[]`，**不阻断快照** |
| `source` | str | 生成来源标识 |
| `notice` | str | 「只读快照，禁止用于自动改权重」提示 |

**`history` 行结构**：
`stat_date` / `pool` / `strategy` / `stat_n_days` / `n_signals` / `n_evaluated` / `n_win` / `win_rate` / `avg_return`

**调用方**：`auto_sniper_daemon._signal_backfill_pipeline()`（每日 18:30，第四步，
独立 `try/except` + `_notify_daemon_alert`；快照失败**不影响**回填事务结果）。

---

## `core.strategy_effectiveness_dashboard` （V29.2 新增）

**硬约束**：本模块**只读 JSON**，**绝不 import `data.db_core`**、绝不连 DuckDB
（Windows 下 DuckDB 单写锁，daemon 运行时 UI 直连必然失败）。

### `load_snapshot(path=None) -> dict | None`

**产出 dict**：
- `schema_version` / `source_file` / `generated_at`
- `freshness_level` (str: fresh/warn/bad/unknown) + `freshness_message` (str)
- `stat_date` / `mode`
- `maturity` / `strategy_maturity` / `recommendation` / `deprecated`
- `stats` (list[dict]) — 规范化后的战法行，见下表
- `history` (list[dict]) — 规范化趋势行
- `summary` (dict) — 顶部指标卡聚合值（全部按 `n_evaluated` 加权）
- `duplicates` (list[dict]) — 疑似重复统计组

**行为约定**（全部经过回归验证）：
- 文件缺失 / JSON 损坏 / 顶层非 dict / schema 不受支持 → **返回 None，绝不抛异常**
- 快照路径优先级：`data/strategy_effectiveness_ui_snapshot.json`
  → 兜底 `data/strategy_weight_recommendation.json`

### `stats[]` 行字段契约

| 字段 | 类型 | 说明 |
|---|---|---|
| `strategy` | str | 战法原子 ID |
| `strategy_label` | str | 中文标签 |
| `n_signals` / `n_evaluated` / `n_win` | int | NULL 安全，非法值 → 0 |
| `win_rate` | float \| None | 主口径胜率；**NULL 不转 0** |
| `primary_n_days` | int \| None | 主口径 N 档 |
| `available_n_days` / `missing_n_days` | list[int] | 支持 `[2,1]` / `"2,1"` / `"[2, 1]"` / `"2,5,bad"` |
| `avg_return` / `avg_excess` / `avg_max_drawdown` / `avg_max_profit` | float \| None | 同上 NULL 安全 |
| `pools` | list[str] | 小写池名；缺省时从 `pool_rows` 回填 |
| `pool_rows` | list[dict] | `{pool, n_days, n_signals, win_rate}`，脏行已剔除 |
| `sample_tier` | str | 零样本 / 样本偏少(1-9) / 样本中等(10-29) / 样本充足(≥30) |

**口径冻结（代码级强制）**：
`normalize_stats()` 会**物理 pop** 掉 `FORBIDDEN_FIELDS`
（`t5_win_rate` / `t10_win_rate` / `avg_t5_return` / `avg_excess_t5`），
确保 UI 层拿不到旧口径字段。

### 其他公开函数

| 函数 | 用途 |
|---|---|
| `resolve_snapshot_path(path=None) -> str \| None` | 返回第一个存在的快照绝对路径 |
| `normalize_stats(raw) -> list[dict]` | stats 规范化 + 脏行丢弃 + 禁字段剔除 |
| `build_daily_history(raw) -> list[dict]` | 趋势行规范化 |
| `classify_sample(n_evaluated) -> str` | 样本厚度四档 |
| `judge_freshness(generated_at, now=None) -> tuple[str, str]` | 新鲜度判定 |
| `weighted_average(rows, field, weight_field="n_evaluated") -> float \| None` | 加权平均（禁止算术平均） |
| `summarize(rows, recommendation) -> dict` | 顶部指标卡聚合 |
| `build_pool_matrix(rows) -> dict[str, list]` | 跨池归集 |
| `history_for_strategy(history, strategy) -> list[dict]` | 单战法趋势切片 |
| `detect_suspicious_duplicates(rows) -> list[dict]` | 多战法统计完全一致 → 疑似重复记账 |

### `ui.strategy_effectiveness_panel`

入口 `render_strategy_effectiveness_panel()`。全程 `try/except` 包裹，
**任何异常都不会冒泡到五池扫描主流程**。

关键约定：
- `_cached_snapshot(_salt)` 使用 `@st.cache_data(ttl=30)`
- `render_refresh_button()` 只清 Streamlit 缓存，**不触发回填**（回填属 daemon 职责）
- 渲染顺序：成熟度条 → 指标卡 → 筛选器 → 明细表 → 散点图 → 趋势 → 单战法详情 → 重复提示
- 挂载点：`ui/app.py` 底部，后勤工具箱 expander 之前，默认折叠

### 回归测试

| 命令 | 覆盖 |
|---|---|
| `python tools/_smoke_strategy_panel.py` | 9 场景无头渲染（真实快照 / 缺失 / 损坏 / schema 不支持 / 极值 / 多池 / 带 history / 筛选交互 / 异常隔离），27 项断言 |
| 数据层边界用例 | 15 组、47 项断言（脏数据、NULL、样本分档、加权平均、新鲜度、重复探测、跨池矩阵、history 防御） |

## 各池 strat_*.py 的统一推送钩子（5 池共用）

每个 strat_*.py 的"末尾"必须按以下顺序调用：

```python
# 1. 推送模板（注入 should_push_now 字段）
from core.push_template_momentum_reversal import apply_push_template_to_res
apply_push_template_to_res(res)

# 2. 出场建议（P5 含 rt 价格；P1/P2/P3/P4 用 res.price）
from core.exit_strategy import attach_exit_plan_to_res
attach_exit_plan_to_res(res, push_price=res.get("price", 0.0))

# 3. 推送去重（先调，再落库）
from core.push_dedup import check_dedup, record_push
_dedup = check_dedup(res["ts_code"], pool, res["burst_score"])
if _dedup["should_dedup"]:
    res["is_pass"] = False
    res.setdefault("risk_tags", []).append(f"🔁推送去重:{_dedup['reason']}")
else:
    record_push(res["ts_code"], pool, res["burst_score"])

# 4. 落库（adapter 内部不再做 dedup，只写库）
from core.pool_signal_adapter_v28 import append_signal_log_safe
db_conn = globals().get("_GLOBAL_DB_CONN")
append_signal_log_safe(res, pool, db_conn, push_min_score=...)
```

---

# Tier 2 字段契约（2026-11-03 补充 · 详见 `docs/TIER2_EXECUTION_REPORT_20261102.md`）

## `core.execution_model`（唯一成交/费用真值）

三处调用（模拟盘 / 回测旧路径 / 回测真实路径）必须引用同一批常量，禁止各写一份费率。

| 常量 | 值 | 语义 |
|---|---|---|
| `COMMISSION_RATE` | `0.0003` | 佣金万 3（双向） |
| `COMMISSION_MIN` | `5.0` | 单笔最低 5 元 |
| `STAMP_TAX_RATE` | `0.0005` | 印花税万 5（**仅卖出**） |
| `TRANSFER_FEE_RATE` | `0.00001` | 过户费十万分之 1（双向） |
| `LOT_SIZE` | `100` | 一手 100 股整手 |

函数：`limit_pct / limit_prices / calc_cost / round_lot / fill_price / is_sellable /
net_return_pct / position_size_by_risk / buy_fill / sell_fill`。

成交硬规则：
- 一字涨停（`open==high==low==涨停价`）**买不进** → `n_buy_blocked`
- 一字跌停 **卖不出** → `n_sell_blocked`
- `vol <= 0` 视作停牌，不计入分母
- 滑点后成交价必须夹逼在当日 `[low, high]` 内
- T+1：`is_sellable(buy_date, today)` 为 False 时不可卖

## `signal_pnl` 新增 7 个净列（Tier 2.5）

```
net_return_pct
net_win
net_excess_pct
net_excess_sh_pct      -- 对上证的超额
net_excess_sz_pct      -- 对深证的超额
net_excess_cyb_pct     -- 对创业板的超额
net_excess_blend_pct   -- 板块混合基准超额（按个股所属板块加权）
```

口径：按 `push_price` 买入、T+N 收盘卖出、10 万本金 100 股整手、费用全走 `execution_model`。

**硬规则（不得违反）**：基准缺失时 `net_excess_*` 必须为 **NULL**，禁止用 0 冒充。
回归判据：`SELECT COUNT(*) FROM signal_pnl WHERE benchmark_pct IS NULL AND net_excess_pct IS NOT NULL` 必须为 **0**。

扩列机制：`data.signal_logger._ensure_tier2_net_columns(db_conn)`（幂等，可反复执行）。

## `strategy_effectiveness` 新增 4 个净聚合列（Tier 2.5）

```
avg_net_return     -- 净收益均值
net_win_rate       -- 净胜率（0~1）
avg_net_excess     -- 净超额均值
net_n              -- 参与净口径计算的样本数
```

写入方：`data.signal_backtest.compute_strategy_effectiveness()`。

## `core.strategy_weight_optimizer` 的净推荐（Tier 2.5 延伸）

`optimize_strategy_weights(auto_apply=False)` 返回体新增：

```python
net_weight_recommendation[strategy] = {
    "weight": float,               # 建议权重（仅建议）
    "avg_net_return": float,
    "net_win_rate": float,
    "avg_net_excess_blend": float,
    "basis": str,                  # 依据说明
    "reason": str,
}
mode = "statistics_only" | "auto_applied"
```

**约束**：
- `auto_apply` 默认 `False`；只有 `auto_apply=True` **且** `n_signals >= min_n_for_auto_apply(200)` 才写 `strategy_weights.json`。
- `net_weight_recommendation` **永不**参与自动改权重。

## `core.strategy_effectiveness_snapshot.build_ui_snapshot()`（Tier 2.7）

两条路径**必须分离**：

| 用途 | 常量 | 实际文件 |
|---|---|---|
| UI 面板快照（daemon 每晚写） | `UI_SNAPSHOT_JSON` | `data/strategy_effectiveness_ui_snapshot.json` |
| 一次性快照（人工触发） | one-shot 路径 | `data/strategy_effectiveness_one_shot.json` |
| 权重推荐报表 | `RECOMMENDATION_JSON` | `data/strategy_weight_recommendation.json` |

返回 15 个顶层字段（缺一即视为契约破坏）：

```
schema_version, generated_at, stat_date, mode, maturity, strategy_maturity,
recommendation, net_recommendation, deprecated, stats, history,
index_quotes, index_coverage, source, notice
```

约束：
- **只读**：只执行 SELECT；只写这 1 个 JSON；不写 `signal_log / signal_pnl / strategy_effectiveness / 任何权重文件`
- **原子写**：`.tmp` → `os.replace()`
- **失败降级**：组装异常返回空骨架 + `error` 字段，绝不抛给调用方（daemon 不能因为面板而中断）
- 落盘后日志格式：`UI 快照已生成: <path>（战法 N / 历史行 M / 指数 K）`


---

## Tier 1.5 · 数据健康度快照契约（2026-11-03 补充）

> 实现：`tools/system_health.py::data_health_snapshot()` → dict
> 消费：`ui/data_health_badge.py`（10 分钟缓存，异常降级，绝不阻塞主界面）
> 详见 `docs/TIER012_CLOSEOUT_AUDIT_20261103.md` 第二节 C1/C2

### 新增/变更字段

| 字段 | 类型 | 含义 | 判据 |
|---|---|---|---|
| `covered_n` | int | 最新交易日有行情的股票数 | — |
| `in_market_n` | int | `stock_basic where list_status='L'` 全市场在市票数 | — |
| `coverage_pct` | float | `covered_n / in_market_n × 100` | **仅作信息展示，不再用于告警** |
| `tracked_prev_n` | int | 上一交易日有行情的股票数 | — |
| `breadth_pct` | float | `covered_n / tracked_prev_n × 100` | **告警阈值 `< 85`**（骤降 >15% 判疑似半残日/同步中断） |
| `mv_gate_yi` | float | 日线库市值闸（亿元），取自 `constants.DATA_FETCH_MIN_TOTAL_MV_WAN / 10000` | 与 `constants` 强一致，禁止硬编码 |

### 硬约束

1. **分母口径**：日线库按设计只抓 `total_mv` 或 `circ_mv ≥ DATA_FETCH_MIN_TOTAL_MV_WAN`（120 亿）的票，
   因此 `coverage_pct` 天然只有 ~25%，**不得**用它判断"缺数据"。
2. **告警唯一判据**：`breadth_pct < 85`。
3. **只读**：优先 `data.db_core.get_read_conn_singleton(max_wait_sec=3.0)`；
   失败时用原生 `duckdb.connect(DB_PATH, read_only=True)` 兜底（由 `ExitStack` 关闭）；
   **不得**在 `data/db_core` 里改成 `read_only=True`（会与进程内读写连接触发 DuckDB 配置冲突，导致 signal_log 落库失败）。
4. **降级**：任何异常都不得抛给调用方；`reasons` 里写清"读不到"与"数据不可信"的区别。

### UI 文案口径

```
跟踪域 1380 只（较上一交易日 103%）        # 主文案：breadth_pct
· 全市场口径 1380/5576 = 25%：日线库按设计只跟踪总市值/流通市值 ≥ 120 亿的票，非缺数据   # caption
```
