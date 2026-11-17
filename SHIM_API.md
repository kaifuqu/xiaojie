# SHIM_API.md — 拆分前后公开符号契约清单

> **目的**：Day 1 锁住 3 个被拆分文件的全部公开符号。Day 5 拆完后 grep 对比，零遗漏 = 合格。
>
> **生成时间**：2026-10-01 18:40
>
> **生成命令**：
> ```python
> python -c "
> import ast
> for f in ['data/db_core.py', 'data/data_fetcher.py', 'core/scan_engine.py']:
>     tree = ast.parse(open(f, encoding='utf-8').read())
>     for node in tree.body:
>         if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
>             if not node.name.startswith('_'):
>                 print(f'{f}::{node.name}')
>         elif isinstance(node, ast.Assign):
>             for t in node.targets:
>                 if isinstance(t, ast.Name) and not t.id.startswith('_') and t.id.isupper():
>                     print(f'{f}::{t.id}')
> "
> ```

---

## 一、`data/db_core.py`（56 个公开符号）

### `_connection.py`（连接池 / 单例 / 锁重试）
- `get_duckdb_path`
- `duckdb_checkpoint`
- `duckdb_disk_bytes_total`
- `probe_duckdb_lock`
- `init_tushare_pro`
- `get_conn`
- `get_read_conn_singleton`
- `get_write_conn`
- `get_read_conn`
- `table_exists`
- `duckdb_resolve_table_sql_id`
- `duckdb_drop_table_if_exists_resolved`
- `close_db`
- `release_write_conn_for_yield`

### `_backup.py`（备份 / VACUUM / 快照）
- `BACKUP_KEEP_RECENT`
- `backup_database_files`
- `list_available_backups`
- `restore_database_from_backup`
- `duckdb_storage_snapshot`
- `duckdb_storage_snapshot_text`
- `duckdb_vacuum_silent`

### `_save.py`（UPSERT / 主键推断 / 未来日 bug）
- `save_df_to_sql`
- `is_max_trade_date_daily_rows_sparse`

### `_trade_dates.py`（交易日 / QFQ / 股票基础信息）
- `get_project_root`
- `get_existing_trade_dates`
- `get_all_stock_codes`
- `get_latest_daily_data_trade_date_yyyymmdd`
- `get_p1_candidate_codes`
- `get_stock_data_qfq`
- `get_name_map`
- `get_stock_names`
- `sync_stock_basic`
- `map_ts_codes_to_names_local`
- `get_index_latest_from_daily_data`

### `_schema_v26.py`（V26 表 / P1 缓存 / 验证 / 行业查询）
- `save_p1_cache`
- `ensure_v26_tables`
- `ensure_v26_compat_view`
- `verify_v26_coverage`
- `verify_dim_security_consistency`
- `get_ai_analysis_debug_rows`
- `get_latest_ai_analysis_payload_pretty`
- `verify_v26_consistency`
- `load_p1_cache`
- `list_p1_cache_trade_dates_desc`
- `get_realtime_sector_ranking`
- `get_stock_industry`
- `get_all_basic_industry`

### `_sw_industry.py`（申万行业同步 / 诊断）
- `sync_sw_sector_basic`
- `sync_sw_sector_daily`
- `get_sw_sector_daily`
- `suggest_sw_industry_name_mapping`
- `save_sw_industry_name_mapping`
- `get_saved_sw_industry_name_mapping`
- `get_sw_sector_trend_context`
- `diagnose_sw_sector_coverage`
- `sync_and_diagnose_sw_sector`

**db_core 小计：56 个** ✅

---

## 二、`data/data_fetcher.py`（26 个公开符号）

### `_client.py`（Tushare 客户端 / 重试 / 限速）
- `DataFetchCriticalError`
- `DataFetchSkippableError`
- `raise_data_fetch_critical`
- `init_pro`
- `retry_api`
- `fetch_chunked`
- `fetch_trade_date_fallback`
- `fetch_prefer_trade_date`
- `ALL_55_COLS`（**注意：原文件名是 ALL_55_COLS，但 V26.88 已 59 列，待评估是否更名**）
- `P1_ELEVEN_DIM_SOURCE_COLUMNS`
- `RECENT_FORCE_RESYNC_TAIL`
- `CRITICAL_FETCH`

### `_sync_entry.py`（4 个 sync 入口）
- `sync_history`
- `sync_recent_days`
- `sync_missing_days_batch`
- `sync_single_day`

### `_raw_day_data.py`（生肉提取器）
- `fetch_raw_day_data`
- `get_p0_codes`
- `check_data_completeness`
- `check_data_completeness_with_db_origin`
- `KEY_INDEX_CODES`

### `_indicators.py`（指标预计算 + 特征重算）
- `calc_indicators_in_memory`
- `run_post_fetch_verification`

### `_hithink_fallback.py`（HiThink 兜底 / 概念 / 指数）
- `sync_index_valuation_recent`
- `sync_concept_tags_recent`
- `sync_financial_reports_recent`

**fetcher 小计：26 个** ✅

---

## 三、`core/scan_engine.py`（12 个公开符号）

### `_pipeline.py`（主入口 + 段落化子函数）
- `run_scan_engine`
- `PROJECT_ROOT`
- `BJ_TZ`
- `DummyEngine`
- `DANGER_BUY_VWAP_MIN_DAY_PCT`
- `get_project_root`

### `_intraday_snapshots.py`（分时快照独立生命周期）
- `save_p3_first_trigger_snapshot`
- `get_intraday_snapshot_status`
- `capture_intraday_snapshots`

### `_t1_memory.py`（T1 记忆系统）
- `T1_MEMORY_SCHEMA_VERSION`
- `save_signal_log`

### `_detectors_market.py`（市场技术检测）
- `get_realtime_sector_ranking`

**scan_engine 小计：12 个** ✅

---

## 四、拆分边界声明

### ⚠️ 不拆的私有符号（保留 `_` 前缀，仅在新子包内可见）

下划线开头的函数（如 `_safe_fetch`、`_core_pipeline`、`_infer_pk_cols_for_df`、`_calc_safety_factor` 等）**不写入 `__init__.py`**：
- 新代码不应引用私有符号
- 旧代码若硬性引用 → shim 跑 2 个版本周期后清理
- 拆完立即 `git grep "from data.data_fetcher import _"` 排查双入口风险

### ⚠️ 拆分时禁止改名

- 公开符号名必须逐字保留（包括大小写）
- 公开常量（如 `ALL_55_COLS`）即使含义过时也暂不改名（V27 再议）

---

## 五、Day 5 校验清单

```bash
# 1. 公开符号总数对比（拆完应仍为 94）
python -c "
import ast
total = 0
for f in ['data/db_core/__init__.py', 'data/data_fetcher/__init__.py', 'core/scan/__init__.py']:
    tree = ast.parse(open(f, encoding='utf-8').read())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name != '*':
                    total += 1
print(f'总 re-export 数: {total}')
"

# 2. 私有符号残留扫描（应为 0 命中）
git grep "from data.data_fetcher import _" -- '*.py'
git grep "from data.db_core import _" -- '*.py'
git grep "from core.scan_engine import _" -- '*.py'

# 3. baseline 对比
python -c "from data.data_fetcher import sync_single_day"
python -c "from data.db_core import save_df_to_sql"
python -c "from core.scan_engine import run_scan_engine"
```

---

## 六、变更日志

| 日期 | 阶段 | 操作 |
|---|---|---|
| 2026-10-01 | Day 1 | 锁住 94 个公开符号清单 |
| TBD | Day 5 | db_core 拆完校验 |
| TBD | Day 6 | fetcher 拆完校验 |
| TBD | Day 7 | scan 拆完校验 |
| TBD | V27 | shim 清理（2 个版本周期后） |
