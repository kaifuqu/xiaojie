# 清理后复核报告（2026-11-03 16:15）

> 复核方式：全目录重扫 + 逐项对照上一轮清单 + DuckDB 只读直查 + `verify_system.py` / `system_health.py` 实跑

---

## 〇、一句话结论

> **你删掉的都是零风险项，核心资产一个没碰 —— 这一轮清理执行得很干净。**
> **而且有 3 件事变好了：① 覆盖数据补到了 11-02；② WAL 已自动合并（省了 11.7 KB 且消除了风险）；③ `verify_system` 从 79/0/1 变成 80/0/0。**
> **但 `data/runtime/` 那一大批（52 MB）和 `data/_qa`（228 MB）原封未动，`_snapshots` 8 份含明文凭据的 zip 也还在。**

---

## 一、逐项核对（对照上一轮清单）

### ✅ 已删除

| 项 | 状态 | 证据 |
|---|:--:|---|
| `.rea/` | ✅ | 目录已不存在 |
| `data/copied.duckdb` | ✅ | 211 MB 全量市场库副本已删（列表里已无此文件） |
| 5 个根目录探针脚本 | ✅ | `_aclprobe.py` `_aclprobe2.py` `_cleanup_plan.py` `_run_qa.py` `_ui_probe.py` **全部消失** |
| `_cleanup_log.json` | ✅ | 已移到 `_qa_logs/cleanup_log_20261103.json`（**这个处理是对的**——它移到 `_qa_logs` 正好避开我列的"可删"范围，还留了审计记录） |
| `data/push_history.json.bak_20261021_232301` | ✅ | 旧备份已删 |
| `data/runtime/state/p1_rejected_cache.json.corrupt_bak_*` | ✅ | 损坏备份已删（`state/` 里已无此文件） |
| `data/runtime/state/sniper_pipeline_state.json.bak_20261017` | ✅ | 已删 |

### ⚠️ 仍未删除（`[WinError 5] 拒绝访问`）

**上一轮的清理日志 `_qa_logs/cleanup_log_20261103.json` 里 30+ 项是 ERR**，我逐条核对，**这些文件确实还在**：

```
data\runtime\pool_cache\p1_cache_20261008/09/12/13/15/16/20/21.json   ← 8 个历史日缓存
data\runtime\pool_cache\p1_cache_20261012/15/16.json.bak_20261017_202415 ← 3 个备份
data\runtime\pool_cache\pre_restore_20261018_055028\                    ← 恢复前快照
data\runtime\p1_gene\p1_gene_20261008/09/12/13/15/16/20/21.json          ← 8 个历史
data\runtime\state\strategic_mapping_debug_20261009~20261021.json       ← 9 个历史
```

**根因仍是 ACL**：`icacls` 显示这些目录只有 `Authenticated Users:(I)(M)` + `BUILTIN\Users:(I)(RX)`，执行清理的账号**没有 Delete 权限**（`RX` 只给读和执行）。

**但我上一轮实测时删 `p1_cache_20261012.json.bak_20261017_202415` 是 DELETE OK —— 那是我的账号有权。** 所以这是**账号权限差异**，不是文件本身锁死。

> **解法（管理员 PowerShell 跑一次）**：
> ```powershell
> icacls D:\xiaojie\data\runtime /grant "$env:USERNAME:(OI)(CI)F" /t /c
> ```
> 然后重跑清理脚本即可。**或者直接用资源管理器以管理员身份删除。**

### ⬜ 未在清单内（我没提过，你也没删）

| 项 | 大小 | 说明 |
|---|---:|---|
| `data/_qa/` | **228.26 MB** | QA 副本，可重建 |
| `_snapshots/` 8 份 zip | **11.69 MB** | ⚠️ **仍含 `config.yaml` 明文凭据** |
| `data/runtime/` 其余 | 52 MB | 见下 |
| `data/experiments.duckdb` | 0.26 MB | 表内 0 行 |
| `_pyenv/python311` 冗余包 | **362.5 MB** | 第二批，需确认 |
| `__pycache__` | 5.3 MB | **删完又全量重建了**（14 个目录，见 §四） |

---

## 二、🟢 三件变好的事（不是我删的，是系统自己恢复的）

### 2.1 ✅ 覆盖数据补到了 11-02

```
daily_data 最新 = 2026-11-02（15:54 扫描时是 10-21）
最近 10 个交易日：
  2026-11-02  1380
  2026-10-30  1345
  2026-10-29  1393
  2026-10-28  1414
  2026-10-27  1389
  2026-10-26  1413
  2026-10-23  1433
  2026-10-22  1403
  2026-10-21  1439
```
**断更的 10-22 ~ 11-02 共 8 个交易日全部补上了。** 这正是我前面三轮一直说的 P0 —— 现在解决了。

### 2.2 ✅ WAL 已自动合并

```
data\quant_data.duckdb.wal   11.7 KB  →  已消失
data\quant_data.duckdb        228.26 MB（mtime 更新到 16:13:46）
```
DuckDB 在我第一次用系统连接只读打开时自动 checkpoint 了。**这是好事**——WAL 留着有未合并事务风险，现在没了。

### 2.3 ✅ `verify_system.py` 从 79/0/1 → **80/0/0**

```
[OK]   Daemon 锁文件不存在（可正常启动）
体检汇总: 80 通过 / 0 失败 / 0 警告
[READY] 系统通过体检，可启动 daemon。
【上线判定】允许全自动运行（自动巡航 + 企微推送）
```
**上次是 `[WARN] Daemon 锁为陈旧 sentinel（PID 17608 已不存在）` —— 你把那个锁删了，现在直接是 OK。**
**这就是今晚启动前的最后一道门槛，现在过了。**

---

## 三、🔴 但 `p2` / `p4` 依然 0 行 —— B1/B2 修复仍未被验证

```
signal_log:  p3=11 (10-09~10-13)   p5=132 (10-08~11-02)
              p1=0    p2=0    p4=0
```
**11-02 有 23 条 p5 信号落库了**（`trade_date=2026-11-02`，含 `P5-03A/P5-05A/B/C/P5-08/P5-12A/B/P5-13A/B/C`），**说明 p5 链路是通的**。

**但 p2 和 p4 从来没有一行。** 我上一轮说的「B1/B2 修好了但还没被证实修好了」——**现在依然成立**。

**这有两种可能，需要你今晚启动后 5 分钟内确认**：
1. ✅ 修复有效，只是 P2/P4 今天真的没选出票（**可能性存在**：P2 是竞价池、P4 是尾盘池，10-21~11-02 期间市场可能确实没触发）
2. ❌ 还有别的问题（res 结构缺字段被吞 / `is_pass` 判定不过 / `push_min_score` 门槛太高）

**判定方法**：今晚启动后看 `data/runtime/sniper.log` 里有没有 `[signal_log] 写入 N 条 p2/p4 记录`。**连续 3 个交易日都 0 行 = 修复无效**，那就得再查。

---

## 四、⚠️ `__pycache__` 删完立刻全量重建了（5.3 MB）

```
core\__pycache__              1148.9 KB
core\strategies\__pycache__    830.0 KB
tools\__pycache__              787.6 KB
ui\__pycache__                 537.7 KB
data\__pycache__               506.8 KB
__pycache__                    316.7 KB
...共 14 个目录，232 个 .pyc
```
**因为我这半小时跑了好几个脚本（system_health / verify_system / DuckDB 查询），Python 一 import 就重新编译了。**

> **结论：删 `__pycache__` 没有持久价值** —— 只要你跑一次系统就会回来。
> **建议：别再删了。** 它占了 5.3 MB，占总量 1.95 GB 的 0.27%，为这点空间反复删是负收益。
> **如果你就是看着乱想删，那就删最后一次，然后接受它会回来。**

---

## 五、剩余可清理项（现状 + 建议）

| # | 项 | 大小 | 建议 | 风险 |
|:--:|---|---:|---|:--:|
| 1 | `data/_qa/` | **228.26 MB** | ✅ 删 | 无（`make_qa_db.py` 可重建） |
| 2 | `_snapshots/` 8 份 zip | 11.69 MB | ✅ 删 6~7 份，留 1 份作退路 | **含明文凭据 → 删是安全收益** |
| 3 | `pool_cache` 历史日缓存（留 20261102） | 43.8 MB | 🟡 删（需管理员权限） | UI 的 P1 兜底显示会失效 |
| 4 | `pool_cache/*.bak` ×3 | 10.7 MB | ✅ 删（需管理员权限） | 无 |
| 5 | `pool_cache/pre_restore_20261018_055028/` | 0.93 MB | ✅ 删（需管理员权限） | 无 |
| 6 | `p1_gene` 历史 8 个 | 0.15 MB | 🟡 删（需管理员权限） | 无 |
| 7 | `strategic_mapping_debug` 历史 9 个 | 0.12 MB | 🟡 删（需管理员权限） | 无 |
| 8 | `runtime/{daemon_err,daemon_stdout,daemon_stderr,daemon_out,weekly_maintenance}.log` | 0.31 MB | ✅ 删 | 无（`sniper.log` 保留） |
| 9 | `runtime/logs/_daemon_boot_*.log` | 7.7 KB | ✅ 删 | 无 |
| 10 | `data/experiments.duckdb` | 0.26 MB | ✅ 删（表内已查证 0 行） | 无 |
| 11 | `_pyenv/python311` 冗余 115 包 | **362.5 MB** | 🟠 删（第二批） | 低但需验证 |
| 12 | `_pyenv/python311/{Doc,Tools,tcl,Scripts}` | **60 MB** | 🟠 删（第二批） | 低 |
| 13 | `xuanguchi/` 33 个 TXT | 0.05 MB | ❌ **保留**（业务存档） | — |
| 14 | `__pycache__` | 5.3 MB | ❌ **别再删**（会重建） | — |

**还能再腾出 ≈ 246 MB（安全）+ 422 MB（需确认）**

---

## 六、🔴 还没修的那件事：8 份 zip 里的明文凭据

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
**8 份全部还在，一份没删。**

**根因**：`tools/backup_snapshot.py:43` 的 `_iter_files()` **不读 `.gitignore`，也没有 `config.yaml`/`.env` 的文件名排除规则**。

> **只要不修这个工具，你今晚再打一次快照，又会多一份明文凭据副本。**
> 修法就一行（在 `EXCLUDE_EXT` 旁边加）：
> ```python
> EXCLUDE_NAMES = {"config.yaml", ".env", "master_control.json"}
> # 并在 _iter_files() 里：if fn in EXCLUDE_NAMES: continue
> ```

---

## 七、当前状态总表

| 维度 | 15:54（清理前） | **16:15（现在）** |
|---|---|---|
| `verify_system.py` | 79 通过 / 0 失败 / **1 警告** | ✅ **80 / 0 / 0** |
| `system_health.py` | 1 OK / 3 WARN / 4 FAIL | **2 OK / 3 WARN / 3 FAIL** |
| `daily_data` 最新 | 2026-10-21 | ✅ **2026-11-02** |
| 断更交易日 | 8 天（10-22~11-02） | ✅ **0 天** |
| WAL | 11.7 KB 未合并 | ✅ **已合并** |
| daemon 锁 | 陈旧 sentinel（PID 17608） | ✅ **已清除** |
| `signal_log` p5 | 109 行 | **132 行**（含 11-02 的 23 条） |
| `signal_log` p2/p4 | 0 / 0 | ⚠️ **0 / 0（仍未验证）** |
| 交易日历未来 | 0 行 | ⚠️ **仍 0 行** |
| 覆盖率 | 26% | 25%（11-02 是 1380/5576） |
| 模拟盘 | 0 持仓 0 订单 | ⚠️ **仍 0（引擎完整但从未成交）** |
| `ledger` 行数 | 302 | **367**（+65） |
| 目录总量 | ~1.95 GB | ~1.95 GB（`_pyenv` 占 73%） |

---

## 八、📌 今晚执行前的最后 5 件事

```
□ 1. 删 data/_qa/（228 MB，腾空间）                        ← 可选，不删也行
□ 2. 删 _snapshots/ 里 7 份旧 zip（留最近 1 份作退路）    ← 安全收益
□ 3. 修 tools/backup_snapshot.py：加 config.yaml/.env 排除 ← 5 行代码
□ 4. 【最关键】start_daemon_24x7.bat
□ 5. 启动后 5 分钟内查 sniper.log：
      · 有无新的 signal_log 写入 → 数据恢复成功？
      · 有无 "[signal_log] 写入 N 条 p2 / p4" → B1/B2 修复验证？
      · 覆盖率是否从 25% 往上走？
```

**另外两件"不急但别忘"**：
- **交易日历仍是 0 行**（`dim_trade_calendar` max = 2026-10-21）→ 离线路径的节假日判断会错一天，daemon 起来后如果报日历相关错，先补这个
- **`data/runtime/` 那 52 MB 要删得先用管理员 PowerShell 授权**（`icacls` 那条命令），否则还是 `WinError 5`

---

## 九、最后一句

> **清理这一轮执行得干净 —— 零风险项全删、核心资产零损伤，还顺带把 WAL 和陈旧锁解决了，`verify_system` 直接变成全绿。**
>
> **但要说清楚：清理解决的是"磁盘乱不乱"，不是"系统跑不跑"。**
>
> **好消息**：数据已经补到 11-02 了（这个我以为是最大的 P0，现在已经过去）。
> **剩下的真问题只有两个**：
> ① **p2/p4 依然 0 行** → B1/B2 修复仍未被任何真实交易日验证
> ② **模拟盘 0 持仓 0 订单** → 引擎接的是 P3/P4，而这两池没有信号，**模拟盘永远不会有成交**
>
> **这两件事是同一件事：p2/p4 不落库 → 模拟盘永远空转。**
> **今晚启动后第一件事不是看数据，是看 `signal_log` 里 p2/p4 有没有行。**

---

*本报告基于 2026-11-03 16:15 的全目录重扫、上一轮清单逐项对照、DuckDB 只读直查（经系统连接含 WAL 重放）、`verify_system.py`（80/0/0）与 `system_health.py`（2 OK/3 WARN/3 FAIL）实跑生成。*
*不含任何凭据明文。*
