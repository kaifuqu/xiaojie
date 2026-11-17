# 小杰选股系统 · 第二轮优化复核报告（2026-11-02 下午）

> 复核方式：全仓 `rg` 交叉检索 + 实跑 5 个脚本 + DuckDB 只读直查 + zip 内容审计
> 口径：**只认实际生效的代码与库里的实际数据，不认文档声明。**

---

## 〇、一句话结论

> **这一轮修掉了一个此前完全没被发现的"隐形杀手"——DuckDB 2.0 pending-result 导致的战法账本静默失写。这是本轮最有价值的成果。**
>
> **但我上一轮报的 5 个问题，只修了 1 个（问题 5 的根因被换了个方式找到），4 个未动；**
> **而真正的 P0（数据停在 T-12、覆盖率 26%、daemon 停了 263 小时）依然原封不动。**

---

## 一、本轮实际改了什么（4 个文件）

| 文件 | 时间 | 改了什么 |
|---|---|---|
| `data/signal_logger.py` | 13:14 | **新增 `_drain_result` / `_fetch_one` / `_fetch_scalar`**，把 20+ 处 `execute(...).fetchone()` 全部收口到会排空结果集的写法 |
| `data/db_core/_connection.py` | 13:25 | 写单例的 2 处防御性 ping 从 `fetchone()` 改 `fetchall()` |
| `data/signal_backtest.py` | 13:25 | 基准序列加载路径修正（`benchmark_df` 被置 None 的 bug） |
| `tools/test_signal_ledger_write.py` | 13:19 | **新增回归脚本**，专门验证 signal_log → ledger 链路 |

---

## 二、本轮最大的收获：找到并修掉了一个"隐形杀手"

### 2.1 根因

DuckDB 2.0.0 的同一连接上，若上一次 `execute()` 的结果集没被读完（典型写法 `execute(...).fetchone()` 只取首行），后续对该连接调用 `register()` / `unregister()` 会抛：

```
Resource In Use Error: connection has an unfinished result from execute();
call complete() or abort(), or fetch until a fetch returns no rows...
```

**而 `write_strategy_ledger()` 正是用 `db_conn.register("_df_ledger", df)` 写入账本的。**

**后果链**：
```
任意一处 execute(...).fetchone() 留下 pending result
  → 随后的 register("_df_ledger") 抛 Resource In Use
  → 被 write_strategy_ledger 的 except 吞掉（logger.warning）
  → signal_log 落库了，但 signal_strategy_ledger 一行没有
  → 战法有效性面板全空 / 战法样本统计失真
```

**这是一个"跨模块的、隐式的、由调用顺序决定是否触发"的 Bug** —— 只在特定执行序列下出现，极难定位。**能挖出这个，说明对 DuckDB 2.0 的行为边界理解到位了。**

### 2.2 修复方式（我认可）

- **收口而非 monkey patch**：只在 `data/signal_logger.py` 内部定义 `_fetch_one` / `_fetch_scalar`，逐个替换 20+ 处调用。**没有做全局 patch** —— 这是正确取舍（全局 patch 会影响 DuckDB 的正常语义）。
- **写单例 ping 也修**：`get_read_conn_singleton` / `get_conn` 的防御性 `SELECT 1` 从 `fetchone()` 改 `fetchall()`，避免 ping 本身制造 pending result。
- **配套回归**：`tools/test_signal_ledger_write.py` 验证 5 件事：
  1. signal_log +1 行
  2. ledger +2 行（每个原子战法一行）
  3. `ledger.signal_uid` 与 `signal_log.signal_uid` 一致
  4. `ledger.log_id` 与 `signal_log.id` 一致
  5. **重复写幂等，ledger 不重复**
  6. **全程无 'unfinished result' 警告**

### 2.3 实跑结果：PASS

```
  signal_log 0 -> 1；ledger 0 -> 2
  [OK  ] signal_log +1 行
  [OK  ] ledger +2 行（每个原子战法一行）
  log_id=128 signal_uid=20261102-p3-000099.SZ
  [OK  ] ledger.signal_uid 一致
  [OK  ] ledger.log_id 一致
  [OK  ] 战法集合一致
  重写后 signal_log 1；ledger 2
  [OK  ] 重复写幂等，ledger 未重复
  [OK  ] 无 'unfinished result' 警告
总体： PASS
```

**并且 `tools/test_shadow_filters.py` 也 PASS**（T1~T5 全过，含"Shadow 层未参与任何过滤/打分/推送"的断言）。

> **这个修复的价值被低估了**：它很可能就是 **`strategy_effectiveness` 里 p2/p4 一直没有战法样本、面板部分列全空** 的隐藏原因之一。
> **但注意** —— 我查了库：`strategy_effectiveness` 现有 69 行（p3=17 / p5=52），`signal_strategy_ledger` 302 行，`signal_pnl` 308 行。
> **说明账本链路历史上不是全断的**（302 行是真的写进去了）。所以这个 Bug 的实际影响面是"**偶发**失写"，不是"从不通"。
> **这恰恰是最危险的形态** —— 静默、偶发、无告警。

---

## 三、我上一轮 5 个问题的复核结果

| # | 问题 | 状态 | 复核依据 |
|:--:|---|:--:|---|
| 1 | **数据停在 T-12、daemon 停 263 小时** | ⛔ **完全未动** | `system_health.py` 实跑仍是 **1 OK / 3 WARN / 4 FAIL**；`sniper.log` 最后写入仍是 **2026-10-22 15:06:04**；`daily_data` max 仍 **2026-10-21**；覆盖率仍 **26%**；`dim_trade_calendar` max 仍 **2026-10-21，未来 0 行**；`signal_log` 仍 **p1=0 p2=0 p3=11 p4=0 p5=109** |
| 2 | **快照工具不读 .gitignore，可能复制明文凭据** | ⛔ **未修，且我已确认受害** | 我直接审计了 zip 内容：`_snapshots/src_20261102_113141_tier012.zip` 含 260 个条目，其中**包含 `config.yaml`**。`backup_snapshot.py:43` 的 `_iter_files()` 只按 `EXCLUDE_DIRS`/`EXCLUDE_EXT` 过滤，**不含 `config.yaml` / `.env` 的文件名排除** |
| 3 | **H3：14 个 `anti_fly_*` 配置键仍"能调不生效"** | ⛔ **未动** | `config.yaml` 里 `rg "anti_fly"` **仍有命中**；`P5_DISABLED_FEATURES` 仍是纯元数据，无代码读取、无 UI 显示 |
| 4 | **健康度红绿灯用裸口径硬编码名单 = 循环论证** | ⛔ **未动** | `core/strategy_health_flags.py` 的 `NEGATIVE_KNOWN` 仍 4 条硬编码，且 `_match_known()` **仍优先于统计判定** |
| 5 | **P5 落库走另一条路径，`append_signal_log_safe` 缺 rt** | ⚠️ **未修，但被"绕过"了** | `strat_p5_postmarket.py:901` 仍**没传 rt**；`test_signal_log_wiring.py:40` 的 `MUST_PASS_RT` 仍是 `("p2", "p4")`（p5 仍被豁免）。**但本轮修了 pending-result 后，P5 那条路径即使 price=0 早退也不会污染账本** —— 风险等级从"高"降为"低"，但**根因仍在** |

---

## 四、本轮我新发现的 2 个问题

### 🔴 新问题 A：`signal_log` 与 `signal_pnl` 的口径已经错位

**实测**：
```
signal_log   : 120 行（p3=11, p5=109），交易日 2026-10-08 ~ 2026-10-21
signal_pnl   : 308 行，distinct log_id = 113，max eval_date = 2026-10-21
signal_strategy_ledger : 302 行
strategy_effectiveness : 69 行（p3=17, p5=52），max stat_date = 2026-10-21
```

**疑点**：`signal_log` 只有 120 行，但 `signal_pnl` 的 `distinct log_id` 是 **113**、`ledger` 是 **302 行**。

**302 > 120**：ledger 行数**大于** signal_log 行数是正常的（一条信号命中多个原子战法 → 多行），但这意味着：
**ledger 里有相当一部分行的 `log_id` 指向的 signal_log 行已经不在了**（或指向重复/历史版本）。
这与 `_resync_persisted_ids` 注释里写的"**生产已出现 41 行错配**"是同一类问题的残留。

> **建议**：本轮既然修了 pending-result，就该顺手加一个**账本一致性巡检**（ledger.log_id 是否都能在 signal_log 里找到对应行、signal_uid 是否一致），把它并进 `system_health.py` 的检查项。**这类问题不主动查就永远发现不了。**

### 🟠 新问题 B：QA 副本会"自动变新"，回归测试可能在测旧代码

**实测**：`data/_qa/quant_data.duckdb` 的 `LastWriteTime` = **14:43:39** —— 正好是 `test_signal_log_wiring.py` 运行的时刻。

**代码事实**：`tools/make_qa_db.py` 每次运行会**从生产库全量复制**（`shutil.copyfile`，206MB）。而 `tools/_qa_env.py` 在 QA 副本不存在时会**自动触发复制**。

**风险**：QA 副本每次回归都被刷新 → **副本里的数据是"当时的生产库"**，而生产库停留在 10-21。
所以：
- ✅ 好处：测试永远在最新生产数据上跑
- ⚠️ 隐患：**生产库一旦补上 10-22 之后的数据，历史行的口径（QFQ 复权因子、指数点位）会变化** → **同一份回归代码在不同时间跑出的结果可能不同** → 回归基线不固定

> **建议**：QA 副本**冻结**（不再自动复制），或至少给 manifest 记一个 `source_db_mtime + source_row_count`，让回归结果可追溯到"哪一版生产数据"。

---

## 五、当前的完整状态快照

| 维度 | 状态 |
|---|---|
| `verify_system.py` | ✅ **79 通过 / 0 失败 / 1 警告**（陈旧 daemon 锁） |
| `system_health.py` | ❌ **1 OK / 3 WARN / 4 FAIL** |
| `test_signal_log_wiring.py` | ✅ PASS |
| `test_execution_realism.py` | ✅ PASS |
| `test_signal_ledger_write.py` | ✅ PASS（**本轮新增**） |
| `test_shadow_filters.py` | ✅ PASS |
| `signal_log` | p3=11 / p5=109 / **p2=0 / p4=0** |
| `signal_pnl` / `ledger` / `strategy_effectiveness` | 308 / 302 / 69 行 |
| `daily_data` | max **2026-10-21**（T-12） |
| `dim_trade_calendar` | max **2026-10-21**，未来 0 行 |
| `daemon` | 停 **263.6 小时** |
| 覆盖率 | **26%**（1439 / 5576） |
| `config.yaml` 明文凭据 | **5 个仍在**（且已被打进快照 zip） |
| git | **仍不可用**（`backup_snapshot.py` 替代） |

---

## 六、我的建议：这一轮该收手了，去跑数据

### 你已经连续做了两轮"改代码"

两轮下来，代码层面的成果很扎实：
- B1/B2 断链修复
- 统一成交口径（`execution_model.py` 唯一真值）
- 风险指标补齐（Sharpe/Sortino/Calmar/VaR）
- 净收益口径 + 新旧并列
- 健康度红绿灯
- Shadow 层（1.3/1.4/1.6）
- 数据健康度徽章
- 开机自启脚本
- **DuckDB pending-result 隐形杀手（本轮）**

### 但**代码改到这个程度，再改的边际收益已经低于跑数据**

因为：
1. **B1/B2 至今没被一个真实交易日验证过**（p2=0 / p4=0）
2. **Tier 2 的全部成果没有新数据可测**（T+N 前瞻收益需要"未来数据"）
3. **120 条样本 / 7 个交易日** 不跑不会长
4. **每多改一天代码，数据就多断一天**（现在是 T-12）

### 建议的下一步（按优先级）

**P0 · 今天必须做（你本机执行，我做不了）**
```
□ start_daemon_24x7.bat
□ 补齐 10-22 ~ 最新交易日
□ 连续跑 3 个交易日，确认 signal_log 出现 p2≥1 且 p4≥1
    ← 这是 B1/B2 + pending-result 修复的唯一真验收
□ 删除 _snapshots/src_20261102_113141_tier012.zip（含明文凭据）
□ 轮换 5 个凭据（config.yaml 仍有明文）
```

**P1 · 不需要数据也能做（小改动，1~2 小时）**
```
□ backup_snapshot.py 加文件名排除：config.yaml / .env / *.key
□ P5 的 append_signal_log_safe 补 rt=rt；测试 MUST_PASS_RT 加 p5
□ H3 二选一 —— 推荐「删键 + 配置注释标明未实现」
□ NEGATIVE_KNOWN 从「直接判负」降级为「待复核提示」
□ system_health.py 加「账本一致性巡检」项
```

**P2 · 数据补齐后**
```
□ 重算 strategy_effectiveness 快照（让健康度红灯有一致数据源）
□ run_daily_backfill 用新净口径重算 signal_pnl
□ 1.3/1.4/1.6 Shadow 差集攒到 ≥30 条后再决定是否接线
```

---

## 七、最后一句

> **两轮代码改造，工程质量是合格的（红线守得住、新旧并列、Shadow 纪律、连 DuckDB 2.0 的 pending-result 都挖到了）。**
>
> **但"让数字变真"这件事，现在卡在"没有新数据"上，而不是卡在"没有新功能"上。**
>
> **第三轮不要再改代码了。去把 daemon 跑起来，让它跑满 3 个交易日，然后我们再看这些新口径在真实新数据上是什么表现。**
>
> **代码可以再写，但 3 个交易日的真实数据买不到。**

---

*本报告基于 2026-11-02 14:40 实跑 `verify_system.py`（79/0/1）、`system_health.py`（1/3/4）、`test_signal_log_wiring.py`（PASS）、`test_execution_realism.py`（PASS）、`test_signal_ledger_write.py`（PASS）、`test_shadow_filters.py`（PASS）、`shadow_report.py`，DuckDB 只读直查，以及 `_snapshots/*.zip` 内容审计生成。*
*不含任何凭据明文。*
