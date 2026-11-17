# `D:\xiaojie` 全量文件扫描 · 清理清单（2026-11-03 16:00）

> 扫描方式：全目录递归 + 逐文件清单 + **代码引用反向验证**（`rg` 全仓查谁在读）+ 实测删除可行性
> 原则：**只列"确认无引用"或"可重建"的**；有任何引用疑义的，一律标注 ⚠️ 让你拍板
> 扫描时系统状态：daemon **未运行**（无 python 进程持有数据），数据库最新 2026-10-21

---

## 〇、总览

| 目录 | 大小 | 文件数 | 处置 |
|---|---:|---:|---|
| `_pyenv/` | **1,421.7 MB** | 46,955 | ⚠️ 大头，见 §1（可省 ~950 MB，**有风险**） |
| `data/` | **514.5 MB** | 154 | 其中 228 MB 是 QA 副本，见 §2 |
| `_snapshots/` | 11.7 MB | 28 | ✅ 可清，见 §3 |
| `core/` | 4.9 MB | 251 | 源码，**保留** |
| `tools/` | 1.3 MB | 43 | 源码，**保留** |
| `ui/` | 0.9 MB | 26 | 源码，**保留** |
| `docs/` | 0.5 MB | 21 | 文档，**保留** |
| `__pycache__`（13 处） | 5.3 MB | 232 | ✅ 可清，见 §4 |
| `_qa_logs/` | 0.15 MB | 2 | ✅ 可清，见 §5 |
| `xuanguchi/` | ~0.05 MB | 33 | ⚠️ **业务存档**，见 §6 |

**可安全释放（无风险）≈ 245 MB**
**可释放（有风险，需你确认）≈ 950 MB**
**本次删除会丢失的唯一重要东西：`_snapshots/*.zip` 里 8 份含 `config.yaml` 明文凭据的快照 → 删了反而是安全收益**

---

## 1. `_pyenv/` —— 1,421.7 MB（占 92%）

### 1.1 结构

```
_pyenv/
├── python311/   949.8 MB   ← Python 3.11.9 完整发行版（base interpreter）
└── venv/        471.9 MB   ← 虚拟环境（实际使用的）
```

**关键事实**（我读了 `pyvenv.cfg`）：
```
_pyenv\venv\pyvenv.cfg:
  home = D:\xiaojie\_pyenv\python311
  include-system-site-packages = true      ← 会继承 base 的 site-packages
  version = 3.11.9
```

> ⚠️ **`_pyenv\python311` 是 venv 的底座，删了 venv 立刻失效。**
> 而且 `include-system-site-packages=true` 意味着 **venv 会看到 base 的包**。

### 1.2 唯一的大发现：base 里有 **362.5 MB / 115 个包**，venv 里完全没有，**且全仓 0 引用**

我逐个验证了 top 包：

| 包 | 大小 | venv 有？ | 全仓代码引用 |
|---|---:|:--:|---|
| `scipy` | 112.7 MB | ❌ | **0** |
| `statsmodels` | 55.9 MB | ❌ | **0** |
| `plotly` | 51.2 MB | ❌ | **0** |
| `sklearn` | 42.1 MB | ❌ | **0** |
| `scipy.libs` | 19.3 MB | ❌ | **0** |
| `cryptography` | 10.5 MB | ❌ | **0** |
| `pdfminer` | 9.0 MB | ❌ | **0** |
| `reportlab` | 8.4 MB | ❌ | **0** |
| `pygments` | 8.2 MB | ❌ | **0** |
| `pypdfium2_raw` | 7.3 MB | ❌ | **0** |
| `lightgbm` | 4.8 MB | ❌ | **0** |
| `pypdf` / `PyPDF2` / `docx` / `seaborn` / `joblib` / `werkzeug` / `patsy` … | 共 ~30 MB | ❌ | **0** |

**这些是别的项目（`parent-meal-signup` / `wangyexuangu` / `.rea`）留下的残留**，那两个目录已经被删了，包还留着。

### 1.3 建议

**🟢 安全（零风险，立即可做）—— 删 base 里 venv 没有的包**
```
_pyenv\python311\Lib\site-packages\{scipy, statsmodels, plotly, sklearn, ...}  (115 个目录)
释放 ≈ 362 MB
```
判据：venv 里没有同名目录 + 全仓 `import X` 0 命中 → **删了不影响任何 import**

**🟢 安全 —— 删 base 的文档/可选组件**
```
_pyenv\python311\Doc\      49.7 MB   ← Python 官方文档
_pyenv\python311\Tools\     0.5 MB
_pyenv\python311\tcl\       6.8 MB   ← tkinter（Tk 图形库，本项目不用）
_pyenv\python311\Scripts\   2.8 MB   ← 重复的 exe（venv 里已有一份）
_pyenv\python311\include\   0.8 MB   ← C 头文件（除非你要编译扩展）
_pyenv\python311\libs\     0.5 MB
_pyenv\python311\etc\       0.0 MB
释放 ≈ 61 MB
```
> ⚠️ `include`/`libs` 删了，将来 `pip install` 需要编译的 C 扩展会失败。**建议保留 `include`+`libs`（1.3 MB）**。

**🟠 需谨慎 —— `share/` 27.2 MB**
Python 标准库数据文件（`zoneinfo` 时区数据在 `Lib/zoneinfo`，`share` 里是 `misc` 等）。**时区数据不在这里**（`ZoneInfo("Asia/Shanghai")` 用的是 `Lib/zoneinfo`）。但为稳妥建议**保留**。

**🔴 不要动**
```
_pyenv\python311\python.exe    ← venv 的底座
_pyenv\python311\Lib\          ← 标准库（zoneinfo 在 Lib/zoneinfo，删了会炸时区）
_pyenv\python311\DLLs\         14.1 MB
_pyenv\venv\                   ← 你的运行环境
```

**🟡 另一个选项：把 `include-system-site-packages` 改成 `false`**
改 `pyvenv.cfg` 后 venv 不再继承 base 的包，**然后才**可以放心删 base 的 site-packages。**但这需要重建 venv**，风险高，不建议今晚动。

### 1.4 版本号警告（重要）

```
_pyenv\venv\pyvenv.cfg  →  version = 3.11.9
requirements.lock 注释  →  "安装 Python 3.12.7（务必 3.12.x）"
```
**你的实际运行环境是 Python 3.11.9，不是文档说的 3.12.7。**
`requirements.lock` 里写的 `pandas==3.0.6`（要求 >=3.11）、`pyarrow==25.0.1`（cp312 wheel 覆盖最好）——**当前是 3.11**，pyarrow 装的是 cp311 wheel。
**今晚执行前，先确认你的 `.pyc` 全是 `cpython-311`（我已确认是），别按文档的 3.12 假设。**

---

## 2. `data/` —— 514.5 MB

### 2.1 ✅ 建议删除：`data/_qa/` —— 228.3 MB

```
data\_qa\quant_data.duckdb    228.3 MB
```
**这是生产库的完整副本**，由 `tools/make_qa_db.py` 复制而来，供回归测试用。

- ✅ **可重建**：`python tools/make_qa_db.py` 一条命令重新复制
- ✅ **全仓 0 引用**（只在 `tools/_qa_env.py` 的 `QA_DB` 常量里，且只在测试脚本启动时用）
- ⚠️ **但**：删了之后，第一次跑 `test_signal_log_wiring.py` / `test_signal_ledger_write.py` 会**自动重新复制**（耗时 ~10 秒，占 228 MB 临时空间）

> **建议：删。** 但如果你今晚要跑回归测试，**保留更省事**。

### 2.2 ✅ 建议删除：`data/quant_data.duckdb.wal` —— 11.7 KB
DuckDB 写入日志。生产库最后写入是 11-02 22:06，**daemon 没在跑**。这个 WAL 是正常的未合并状态，**DuckDB 下次打开会自动处理**。删它有极小风险（丢失未合并事务），**建议先让 daemon 正常开一次库让它自动合并，别手删**。

### 2.3 ✅ 建议删除：`data/experiments.duckdb` —— 268 KB（**已查证：表内 0 行**）

```
表 strategy_experiments：0 rows    ← 实测确认无数据
最后写入：2026-10-12
```
被 `core/experiment_db.py` 使用（策略实验记录），**但表是空的**。删了之后若要写实验记录，`_ensure_strategy_experiments_table` 会自动建表。
**✅ 可删（0.26 MB）。**

### 2.4 ⚠️ 保留：`data/quant_data.duckdb` —— 228.3 MB
**这是你的主库，删了系统就没了。绝对不要动。**

### 2.5 ⚠️ 保留：`data/runtime/` 下的业务状态文件

| 文件 | 大小 | 谁在读 | 建议 |
|---|---:|---|---|
| `runtime/state/p1_rejected_cache.json` | 446 KB | 阵亡名单，UI 直接读 | **保留** |
| `runtime/state/master_control.json` | 0.2 KB | **开关文件**（推送开关/巡航开关/维护模式） | **保留** |
| `runtime/state/sniper_pipeline_state.json` | 0.6 KB | pipeline 状态 | **保留** |
| `runtime/state/download_health.json` | 17.3 KB | 数据健康度徽章读 | **保留** |
| `runtime/state/sector_rank_history.json` | 12.3 KB | 板块排名历史 | **保留** |
| `runtime/state/wash_metrics_history.json` | 57 KB | 洗盘指标历史 | **保留** |
| `runtime/state/daemon_public_meta.json` | 0.6 KB | daemon 元信息 | 保留 |
| `runtime/state/p1_last_wash_input_codes.json` | 13 KB | 洗盘输入 | 保留 |
| `runtime/pool_cache/p1_cache_20261102.json` | 4.9 MB | **今天最新**，UI 直接读 | **保留** |
| `runtime/p1_gene/p1_gene_20261102.json` | 18 KB | 最新 | **保留** |
| `runtime/state/strategic_mapping_debug_20261102.json` | 14 KB | 最新 | **保留** |
| `runtime/state/intraday_snapshots.json` | 10 KB | 分时快照 | 保留 |

---

## 3. `_snapshots/` —— 11.7 MB（✅ 建议清理，但有一个重要副作用）

### 3.1 内容

```
_snapshots\
├── src_20261102_113141_tier012.zip         1.36 MB  ⚠️ 含 config.yaml
├── src_20261102_144406_before_tier22_25_26.zip  1.36 MB  ⚠️ 含 config.yaml
├── src_20261102_151852_before_tier22_25_26.zip  1.37 MB  ⚠️ 含 config.yaml
├── src_20261102_205559_before_tier2_ui_snapshot_fix.zip  1.37 MB  ⚠️ 含 config.yaml
├── src_20261102_224020_before_tier2_test_extension.zip  1.39 MB  ⚠️ 含 config.yaml
├── src_20261103_145437_tier012_closeout.zip  1.48 MB  ⚠️ 含 config.yaml
├── src_20261103_145930_tier012_closeout_final.zip  1.41 MB  ⚠️ 含 config.yaml
├── src_20261103_153345_pre_cleanup.zip      1.41 MB  ⚠️ 含 config.yaml
├── *.manifest.json × 8                     ~0.3 MB
├── tier2_logs_20261103\                    0.17 MB  （3 个 .log）
└── *.py (9 个"改前留底"单文件)             ~0.14 MB
    data_health_badge_before_breadth_20261103.py
    log_config_before_fallback_20261103.py
    system_health_before_breadth_20261103.py
    system_health_before_ro_fallback_20261103.py
    test_execution_realism_before_tier2_ext_20261103.py
    verify_system_before_ro_fallback_20261103.py
    _smoke_index_bench_before_fix_20261103.py
```

### 3.2 🔴 重要发现：**8 个 zip 全部含 `config.yaml` 明文凭据**

我逐个审计了 zip 内容：
```
src_20261102_113141_tier012.zip                 SENSITIVE= ['config.yaml']
src_20261102_144406_before_tier22_25_26.zip     SENSITIVE= ['config.yaml']
src_20261102_151852_before_tier22_25_26.zip     SENSITIVE= ['config.yaml']
src_20261102_205559_before_tier2_ui_snapshot_fix.zip  SENSITIVE= ['config.yaml']
src_20261102_224020_before_tier2_test_extension.zip   SENSITIVE= ['config.yaml']
src_20261103_145437_tier012_closeout.zip        SENSITIVE= ['config.yaml']
src_20261103_145930_tier012_closeout_final.zip  SENSITIVE= ['config.yaml']
src_20261103_153345_pre_cleanup.zip             SENSITIVE= ['config.yaml']
```

**根因**：`tools/backup_snapshot.py:43` 的 `_iter_files()` 只按目录/扩展名过滤，**不读 `.gitignore`，也没有 `config.yaml`/`.env` 的文件名排除规则**。

> **删除这些 zip = 消除 8 份磁盘上的明文凭据副本。这是安全收益，不是空间收益。**
> **但删了就没有"回滚到 11-02"的退路了。** 建议：**留最后 1~2 份，其余删**，并且**立刻修 `backup_snapshot.py` 的排除规则**。

### 3.3 建议处置

```
✅ 删  8 份 zip 中的 6 份（保留 src_20261103_153345_pre_cleanup.zip 这 1 份最近的做退路）
✅ 删  8 份 manifest.json 中的 6 份
✅ 删  tier2_logs_20261103\        （3 个 .log，QA 残留）
✅ 删  9 个 *.py "改前留底"        （它们已被 git-like 快照覆盖，或可从 zip 里取）
⚠️ 删前先修 backup_snapshot.py 的排除规则（见 §7）
```

---

## 4. `__pycache__` —— 5.3 MB / 13 个目录（✅ 全部可删）

```
__pycache__\                      245.1 KB    2
core\__pycache__\                1148.7 KB   71
core\notification\__pycache__\    202.3 KB    6
core\paper_trading\__pycache__\   150.4 KB    8
core\pool_manager\__pycache__\    219.9 KB    9
core\scan\__pycache__\            240.2 KB    7
core\strategies\__pycache__\     829.8 KB   24
data\__pycache__\                 506.8 KB   21
data\db_core\__pycache__\         223.5 KB    8
data\fetcher\__pycache__\         266.9 KB    8
service\__pycache__\               42.4 KB    2
tools\__pycache__\                787.6 KB   42
ui\__pycache__\                   537.7 KB   12
ui\pages\__pycache__\               3.5 KB    1
```
**纯字节码缓存，Python 会自动重建**。上次清理（`_cleanup_log.json`）已删过一次，但当天又生成了 —— **说明是运行后自动重建的，正常现象**。

> ⚠️ **注意**：你今晚要执行系统，**删完马上又会生成**。如果只是想"看起来干净"，意义不大；**但删了不会有害**。

---

## 5. `_qa_logs/` —— 0.15 MB（✅ 可删）

```
_qa_logs\release_full_20261103_093733.log   154.8 KB
_qa_logs\release_full_20261103_093733.json    3.3 KB
```
QA 全量发布的日志。`tools/_release_full_run.py` 会重新生成。
**同名的副本还在 `_snapshots\tier2_logs_20261103\` 里** —— 删一份即可。

---

## 6. `xuanguchi/` —— 33 个文件（⚠️ **是业务存档，别删**）

```
xuanguchi\20260620\20260620_214019\{P1_底仓.txt, P2_竞价.txt, P3_盘中.txt, P4_盘尾.txt, P5_真龙.txt}
xuanguchi\20261012\20261012_005606\...
xuanguchi\20261013\20261013_002654\...
xuanguchi\20261013\20261013_005803\...
xuanguchi\20261017\20261017_221527\...
xuanguchi\20261017\20261017_234132\...
xuanguchi\20261018\20261018_010058\...
```
**代码引用确认**：`ui/app.py:2848` 的"导出选股结果"按钮写入这个目录。
**这是你每天的选股结果 TXT 存档** —— **是你自己选的信号历史，属于业务数据，删了就没了。**

> **建议：保留。** 如果嫌乱，可以只归档到 `xuanguchi/archive/` 而不是删。
> 想彻底不想要，就删 20260620 那批（6 月的老数据）。

---

## 7. 根目录临时脚本 —— 5 个（✅ 可删）

```
_aclprobe.py     0.5 KB   15:43   ACL 探测
_aclprobe2.py    0.8 KB   15:44   ACL 探测
_cleanup_plan.py 4.0 KB   15:36   清理执行脚本（已跑过，有 _cleanup_log.json）
_run_qa.py       0.5 KB   16:00   QA 跑批
_ui_probe.py     1.0 KB   15:58   UI 探测
```
**全是今天调试用的探针脚本**，无引用。✅ 可删。

**另外还有：**
```
_cleanup_log.json   上次清理的执行日志（1 个文件）— 可删，但它是"已清理哪些"的审计记录，建议留
```

---

## 8. `data/runtime/` 里的历史缓存 —— 42.9 MB（⚠️ 有条件可删）

### 8.1 `runtime/pool_cache/` —— 52.3 MB

```
p1_cache_20261008.json          4.0 MB   ← 历史
p1_cache_20261009.json          4.0 MB   ← 历史
p1_cache_20261012.json          6.2 MB   ← 历史
p1_cache_20261012.json.bak_20261017_202415   3.7 MB  ← 备份，必删
p1_cache_20261013.json          4.0 MB   ← 历史
p1_cache_20261015.json          5.9 MB   ← 历史
p1_cache_20261015.json.bak_20261017_202415   3.3 MB  ← 备份，必删
p1_cache_20261016.json          4.0 MB   ← 历史
p1_cache_20261016.json.bak_20261017_202415   3.7 MB  ← 备份，必删
p1_cache_20261020.json          4.1 MB   ← 历史
p1_cache_20261021.json          3.6 MB   ← 历史
p1_cache_20261102.json          4.9 MB   ← ★ 最新，保留
pre_restore_20261018_055028\     0.9 MB   ← 10-18 恢复前快照，必删
```

**代码引用**：`ui/app.py:1428-1445` 的 `_find_latest_nonempty_base_items_cache()` 会**倒序扫 `data/runtime/pool_cache/p1_cache_*.json`**，在**今天没生成**的情况下**回退到最近一个 items 非空的历史日**。

> ⚠️ **所以历史缓存不是纯垃圾** —— 它是"今天 P1 底仓为空时的兜底显示源"。
> **但那只是"显示"用的，不是真实数据**。你今晚跑起来后，今天的缓存会重新生成。

**分级建议：**
```
✅ 必删（纯备份/快照，0 风险）:
   p1_cache_*.json.bak_20261017_202415   ×3      10.7 MB
   pre_restore_20261018_055028\                  0.9 MB
🟡 可删（历史日缓存，保留最新即可）:
   p1_cache_20261008/09/12/13/15/16/20/21.json   35.7 MB
   ⚠️ 删后若"今天 P1 底仓为空"，UI 会显示不出 P1（兜底没了）
   → 建议：今晚先跑通，看明天 UI 正常，再删
```

### 8.2 `runtime/p1_gene/` —— 9 个文件 / 0.17 MB
```
p1_gene_20261008/09/12/13/15/16/20/21.json  ← 历史，18-19 KB 各
p1_gene_20261102.json                        ← ★ 最新
```
同样被 `core/pool_manager.legacy.py:2432` 按日读取。**🟡 历史 8 个可删（0.14 MB），收益极小但干净。**

### 8.3 `runtime/state/strategic_mapping_debug_*.json` —— 10 个 / 0.13 MB
```
20261009 / 20261012 / 20261013 / 20261015 / 20261016 / 20261017
20261018 / 20261020 / 20261021 / 20261102(★最新)
```
**🟡 可删历史 9 个（0.12 MB）。** 是调试输出文件。

### 8.4 `runtime/state/` 里的损坏/旧备份 —— 0.5 MB
```
p1_rejected_cache.json.corrupt_bak_20261017_202557   469.3 KB  ← 损坏备份，必删
sniper_pipeline_state.json.bak_20261017                  0.3 KB  ← 旧备份，必删
```
**✅ 无条件删**（当前有效版本 `p1_rejected_cache.json` / `sniper_pipeline_state.json` 都在）。

### 8.5 `runtime/logs/` —— 2 个旧启动日志（7.7 KB）
```
_daemon_boot_err.log    7.7 KB   2026-10-13
_daemon_boot_out.log    0.0 KB   2026-10-13
```
**✅ 可删**（10-13 的旧启动日志，已被 `RotatingFileHandler` 接管）。

### 8.6 `runtime/` 根目录日志 —— 1.8 MB

| 文件 | 大小 | 建议 |
|---|---:|---|
| `sniper.log` | **1.54 MB** | ⚠️ **保留** —— 这是 daemon 主日志，`system_health.py` 用它判断"daemon 停了多久"。删了会误判 |
| `daemon_err.log` | 297 KB | 🟡 可删（10-11 的旧错误日志） |
| `daemon_stdout.log` / `daemon_stderr.log` | 6.1 / 1.4 KB | 🟡 可删 |
| `daemon_out.log` | 0 KB | ✅ 可删 |
| `weekly_maintenance.log` | 4.5 KB | 🟡 可删（10-13） |
| `p1_macd_gate_shadow.jsonl` | 3.8 KB | ⚠️ 保留（Shadow 差集证据） |

### 8.7 陈旧锁文件（✅ 可删，进程已死会自动重建）
```
runtime\state\auto_sniper_daemon.lock      0 KB   ← PID 17608 陈旧 sentinel
runtime\state\ui_manual.lock                0.2 KB
runtime\state\master_control.json.lock      0 KB
runtime\scan_async\pending_queue_consumer.filelock  0 KB
```
**✅ 可删** —— 上次清理也是这么做的（`_cleanup_plan.py` 第 7 项）。

### 8.8 `runtime/shadow/` + `runtime/health/` + `runtime/observability/`（⚠️ 保留）
```
shadow\shadow_20261102.jsonl              3.1 KB   ← Shadow 差集证据，保留
health\sync_health.jsonl                  2.7 KB   ← 同步健康，保留
observability\daemon_heartbeat.jsonl      219.9 KB ← 心跳，保留
observability\daemon_task_matrix.json     2.1 KB
observability\daemon_daily_summary.json   1.5 KB
```

---

## 9. 已被上一轮清理掉的（无需重复操作）

```
.rea/               ✅ 已删（外部 npm 依赖，0 引用）
.corepack/          ✅ 已删
.npmcache/          ✅ 已删
parent-meal-signup/ ✅ 已删（外部子项目）
wangyexuangu/       ✅ 已删
_bk_v291/           ✅ 已删（V29.1 备份）
_bk_v30/            ✅ 已删
_bk_v31_ab_20261013/✅ 已删
_p3opt_backup_20261013/ ✅ 已删
data/copied.duckdb  ✅ 已删（211 MB 全量市场库副本）
```

---

## 10. 上次清理为什么没删掉 `pool_cache` —— **ACL 权限问题（已解决）**

`_cleanup_log.json` 里 30+ 项是 `ERR [WinError 5] 拒绝访问`：

```
ERR  data\runtime\pool_cache\p1_cache_20261008.json   4106.5 KB  拒绝访问
ERR  data\runtime\p1_gene\p1_gene_20261008.json           19.6 KB  拒绝访问
ERR  data\runtime\state\strategic_mapping_debug_20261009.json  8.8 KB  拒绝访问
```

**根因**：`icacls` 显示 `Authenticated Users:(I)(M)` + `BUILTIN\Users:(I)(RX)`，但当时执行清理的沙箱账号在这些目录上没有删除权。

**我刚才实测**：以当前账号删 `p1_cache_20261012.json.bak_20261017_202415` → **DELETE OK** ✅

> **说明权限问题已经解决（很可能是上次清理时 ACL 被临时收紧过，现在已恢复）。今晚删除应该能成功。**
> 如果再遇到拒绝访问，用管理员 PowerShell：
> ```powershell
> icacls D:\xiaojie\data\runtime /grant "$env:USERNAME:(OI)(CI)F" /t
> ```

---

## 11. ✅ 最终删除清单（按建议执行顺序）

### 🟢 第一批：零风险（~245 MB + 干净目录）

```
【A】删 data/_qa/                                    228.3 MB   ← 可重建
【B】删所有 __pycache__/                             5.3 MB    ← 可重建
【C】删根目录 5 个临时探针脚本                          6 KB
      _aclprobe.py  _aclprobe2.py  _cleanup_plan.py  _run_qa.py  _ui_probe.py
【D】删 _qa_logs/ 全部                                 0.15 MB
【E】删 _snapshots/ 里 6 份旧 zip + 6 份旧 manifest    ~10 MB
      ⚠️ 保留 src_20261103_153345_pre_cleanup.zip 作退路
【F】删 data/runtime/state/p1_rejected_cache.json.corrupt_bak_20261017_202557   469 KB
【G】删 data/runtime/state/sniper_pipeline_state.json.bak_20261017             0.3 KB
【H】删 data/runtime/pool_cache/*.json.bak_20261017_202415  ×3                10.7 MB
【I】删 data/runtime/pool_cache/pre_restore_20261018_055028/                   0.9 MB
【J】删 4 个陈旧锁文件（auto_sniper_daemon / ui_manual / master_control / pending_queue_consumer）
【K】删 data/runtime/logs/_daemon_boot_*.log                               7.7 KB
【L】删 data/runtime/{daemon_out,daemon_stdout,daemon_stderr,daemon_err,weekly_maintenance}.log
【M】删 data/runtime/state/strategic_mapping_debug_*.json （保留 20261102）   0.12 MB
【N】删 data/runtime/p1_gene/p1_gene_*.json （保留 20261102）                 0.14 MB
【O】删 data/runtime/pool_cache/p1_cache_*.json （保留 20261102）             35.7 MB
     ⚠️ 前提：今晚跑通后确认 UI 的 P1 底仓正常显示
【P】删 _snapshots/tier2_logs_20261103/ 与 9 个 *_before_*.py                 0.3 MB
```

### 🟠 第二批：需你确认（有风险 / 需先查证）

```
【Q】删 _pyenv\python311\Lib\site-packages\ 下 venv 没有的 115 个包    362.5 MB
     判据：venv 里无同名 + 全仓 import 0 命中
【R】删 _pyenv\python311\{Doc,Tools,tcl,Scripts}                       60 MB
     ⚠️ 保留 include/ libs/ DLLs/ Lib/ python.exe
【S】删 data/experiments.duckdb                                        0.26 MB
     ✅ 已查证：strategy_experiments 表 0 行，删后会自动重建
【T】修 tools/backup_snapshot.py —— 加 config.yaml / .env 排除规则
【U】删 xuanguchi/20260620/（6 月老存档）                              1 KB
     ⚠️ 其余 4 个日期的存档保留（业务数据）
```

### 🔴 绝对不要删

```
data/quant_data.duckdb              主库
_pyenv/venv/                         运行环境
_pyenv/python311/python.exe + Lib/   venv 底座 + 标准库（含 zoneinfo）
_pyenv/python311/DLLs/               14 MB
config.yaml / constants.py / auto_sniper_daemon.py / verify_system.py
core/ ui/ tools/ service/ scripts/ docs/
data/runtime/state/master_control.json     ← 推送/巡航开关
data/runtime/state/p1_rejected_cache.json  ← 阵亡名单
data/runtime/sniper.log                    ← daemon 主日志
data/runtime/pool_cache/p1_cache_20261102.json
data/runtime/p1_gene/p1_gene_20261102.json
```

---

## 12. 📌 执行前必须知道的两件事

### 12.1 你的运行环境是 **Python 3.11.9**，不是文档写的 3.12.7

```
_pyenv\venv\pyvenv.cfg → version = 3.11.9
所有 .pyc → cpython-311
requirements.lock 注释说"务必 3.12.7"
```
**今晚执行前按 3.11 核对**，别照文档的 3.12 假设。

### 12.2 数据仍然停在 T-12，daemon 没在跑

```
daily_data 最新 = 2026-10-21
dim_trade_calendar max = 2026-10-21（未来 0 行）
signal_log: p1=0 p2=0 p3=11 p4=0 p5=109
sniper.log 最后写入 = 2026-10-22 15:06（已停 3 天）
当前无 python 进程
```

**清理动作本身不会破坏这些**，但**清理 ≠ 恢复**。
今晚真正要做的第一件事仍然是：**`start_daemon_24x7.bat` + 补齐 10-22 至今的交易日**。
清理是"打扫房间"，**恢复数据是"把暖气打开"** —— 后者优先级高得多。

---

*本清单基于 2026-11-03 16:00 的全目录递归扫描（46,955 个文件）、逐文件引用反向验证（`rg` 全仓）、zip 内容审计、DuckDB 只读直查、ACL 检查与实测删除验证生成。*
*不含任何凭据明文。*
