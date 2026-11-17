# 小杰系统 · Tier 2 收尾执行报告（EXEC-2）

> 版本：EXEC-2 ｜ 生成时间：2026-11-03（Asia/Shanghai）
> 运行环境：Python 3.11.9（`D:\xiaojie\_pyenv\venv`）｜ DuckDB 2.0.0 ｜ Windows 单机
> 依据：`docs/UPGRADE_FINAL_DECISION_20261102.md` 的 Tier 2（2.1~2.6），外加本轮补齐的 2.7
> 与旧报告关系：本报告**细化并修正** `docs/TIER012_EXECUTION_REPORT_20261102.md` 的 Tier 2 段。
> 旧报告曾写「没有偷偷把 3 条战法改成 delay=1」；后续轮次已把主口径统一为 `buy_delay=1`，
> 旧 delay=0 数字单独放进 `legacy_delay0` 对照块。以本报告为准。
> Tier 0/1/2 的**最终收尾口径**见 `docs/TIER012_CLOSEOUT_AUDIT_20261103.md`（CLOSEOUT-1），冲突时以 CLOSEOUT-1 为准。

---

## 〇、一句话结论

Tier 2 七项全部落地并有可复现证据：**成交口径只剩一个真值**（`core/execution_model.py`）、
**前视偏差被显式标注并保留新旧两套数字**、**不可成交（一字板/停牌）与滑点夹逼进回测**、
**风险指标补齐**、**胜率升级为净收益 + 净超额**、**负向战法亮红灯（只标记不删档）**、
**daemon→UI 的战法面板快照链路修通**。

本轮另修掉 **3 个真 Bug**（其中 1 个会让上线门禁误判 4 条 P0），
最终回归：`compileall` 0 错，5 个专项脚本全 PASS，`verify_system.py` **80 通过 / 0 失败 / 0 警告 → 允许全自动运行**。

---

## 一、改动总表（Tier 2 全量）

| # | 文件 | 改了什么 | 红线自查 |
|---|---|---|---|
| 2.1 | `core/execution_model.py`（新增） | 费用/涨跌停/整手/T+1/成交夹逼的唯一真值 | 新增纯函数模块，不改任何既有策略 |
| 2.1 | `core/backtest_runner.py:142/222` | 新增 `real_exec_returns()` / `realize_one_trade()`，与旧口径**并列输出** | 旧字段一个没删 |
| 2.2 | `core/backtest_runner.py:105-108/414-420/490-541` | 主口径 `buy_delay=1`；3 条旧战法给 `legacy_lookahead_risk` 标记 + `legacy_delay0` 对照 | 不改阈值/权重 |
| 2.3 | `core/backtest_runner.py` + `core/execution_model.py:221/236` | 一字涨停买不进、一字跌停卖不出、`vol<=0` 视停牌、成交价夹在 `[low,high]` | 只加统计，不改选股 |
| 2.4 | `core/backtest_runner.py:111` | `calc_risk_metrics()`：Sharpe/Sortino/Calmar/VaR95/最大回撤 | 纯计算 |
| 2.5 | `data/signal_logger.py:1200`、`data/signal_backtest.py:159/446` | `signal_pnl` 增 7 个净列；`strategy_effectiveness` 增 4 个净聚合列 | 幂等扩列，旧列保留 |
| 2.6 | `core/strategy_health_flags.py`（新增）、`core/strategy_effectiveness_dashboard.py` | 4 条已知负向战法注入 `health_flag/label/reason` | 只展示，不参与打分 |
| 2.5 延伸 | `core/strategy_weight_optimizer.py:99-163/400-487` | `net_weight_recommendation`：**只建议不改权重**，`auto_apply` 默认 False | 权重文件不被动 |
| 2.7 | `core/strategy_effectiveness_snapshot.py:871-998` | `build_ui_snapshot()` 15 字段契约 + 与 one-shot 路径分离 + 原子写 | 只写 1 个 JSON |
| 2.7 | `ui/strategy_effectiveness_panel.py` | 面板透传净字段 + 净负向筛选 + `net_recommendation` | 纯只读 |
| 补丁 | `tools/test_execution_realism.py` | 由"依赖真实触发样本"改为 8 组**代码级契约** + 可选真实样本 | 只改测试脚本 |
| 补丁 | `tools/_smoke_index_bench.py:131/133/136` | `fetchone()[0]` → `fetchall()[0][0]`（见 §十·F1） | 只改测试脚本 |
| 补丁 | `verify_system.py`（新增 `_qc_ro_conn()` + 4 处调用点） | 体检读库多级兜底，消除 4 条虚假 P0（见 §十·F3） | 只改体检脚本 |

---

## 二、2.1 统一成交口径（唯一费用真值）

**根因**：此前「模拟盘 `paper_trading/fees.py`」「回测旧路径」「回测真实路径」三套费率各写一遍，
往返成本口径能差 1~2 个万分点，等于同一战法在不同页面显示不同胜率。

**修复**：抽 `core/execution_model.py` 作为唯一真值，三个调用点全部引用同一批常量与函数。

| 常量/函数 | 位置 | 口径 |
|---|---|---|
| `COMMISSION_RATE = 0.0003` | `core/execution_model.py:35` | 佣金万 3（双向） |
| `COMMISSION_MIN = 5.0` | `:36` | 单笔最低 5 元 |
| `STAMP_TAX_RATE = 0.0005` | `:37` | 印花税万 5（**仅卖出**） |
| `TRANSFER_FEE_RATE = 0.00001` | `:38` | 过户费十万分之 1（双向） |
| `LOT_SIZE = 100` | `:40` | 一手 100 股整手 |
| `limit_pct()/limit_prices()` | `:45/:57` | 主板 ±10%、ST ±5%、创业板/科创板 ±20%、北交所 ±30% |
| `calc_cost()/round_lot()/fill_price()` | `:88/:105/:115` | 费用计算 / 整手 / 成交价夹逼 |
| `is_sellable()` | `:153` | T+1 可卖判定 |
| `net_return_pct()` | `:163` | 扣费后的净收益率 |

**证据**：`tools/test_execution_realism.py` ①段实测单笔往返成本 `0.1119%`
（万 3×2 + 万 5 + 过户费十万分之 1×2），与逐笔手工计算完全一致；并断言
**净收益必须小于毛收益**（扣费方向不可反）。

---

## 三、2.2 前视偏差（`buy_delay=0`）

**根因**：旧实现给 `01_一进二` / `02_黄金缺口` / `29_量比异动` 三条战法用 `buy_delay=0`
（= 信号当天开盘价买入），但信号本身是**收盘后/盘中后段**才产生的 → 用当天开盘价成交属前视，
这 3 条战法的历史胜率被系统性高估。

**修复**（`core/backtest_runner.py`）：
- `:419` 主口径统一 `buy_delay = 1`（信号次日开盘成交），**所有战法一致**；
- `:108` `_LOOKAHEAD_LEGACY_KEYS = ("01_一进二","02_黄金缺口","29_量比异动")`；
- `:493/495` `metrics["lookahead_risk"] = False`、`metrics["legacy_lookahead_risk"] = bool(...)`；
- `:528` `metrics["legacy_delay0"]` 保留旧口径数字供对照（历史结论仍可追溯）；
- `:540` `real_exec_delay1` 兼容字段指向主口径，旧依赖不报错。

**证据**（③段合成样本，150 行日线）：同一批 15 笔信号
`legacy_delay0.n_trades = 15`、旧口径 `win_rate = 33.33%`，净口径 `win_rate_net = 53.33%`；
`buy_delay == 1`、`lookahead_risk is False`、`legacy_lookahead_risk is True` 三条断言全部通过。

> 说明：这不是"策略变差"，是把**虚假的当天开盘价**换成**真能成交的次日开盘价**。

---

## 四、2.3 一字板 / 停牌不可成交 + 滑点夹逼

**修复**：`real_exec_returns()` 逐笔走 `execution_model.buy_fill()` / `sell_fill()`：

| 场景 | 判定 | 结果 |
|---|---|---|
| 一字涨停（`open==high==low==涨停价`） | `buy_fill` | 买不进，计入 `n_buy_blocked` |
| 一字跌停 | `sell_fill` | 卖不出，计入 `n_sell_blocked` |
| `vol <= 0` | 停牌 | 不计入分母 |
| 滑点把价格推出当日区间 | `fill_price` | 夹逼在 `[low, high]` 内 |

**证据**：实测 10% 滑点被夹到当日 `high = 10.2`；输出 `fill_rate_pct`（可成交率）与
`n_buy_blocked` / `n_sell_blocked` 分类计数，回测里能直接看到"有信号但无法成交"的比例。

---

## 五、2.4 风险指标

**修复**：`core/backtest_runner.py:111 calc_risk_metrics(returns, hold_days=5)`
输出 `sharpe_annual / sortino_annual / calmar / var95 / max_drawdown`，
并同时挂两套：`risk_real`（真实口径）与 `risk_legacy`（旧口径）。

**证据**：合成样本实测 `sharpe_annual = 1.0366`、`sortino_annual = 2.4214`、
`calmar = 3.809`、`var95 = -1.885`（④段断言数值有限且方向合理）。

---

## 六、2.5 胜率口径升级：净收益 + 净超额

**根因**：旧胜率是"裸 T+N 涨跌"，完全不含费用与指数基准 → 牛市里任何战法都"看起来能赚"。

**修复 1（落库层）**：`data/signal_logger.py:1200 _ensure_tier2_net_columns()` 幂等给 `signal_pnl` 补 7 列：

```
net_return_pct, net_win, net_excess_pct,
net_excess_sh_pct, net_excess_sz_pct, net_excess_cyb_pct, net_excess_blend_pct
```

`data/signal_backtest.py:159 _net_metrics()`：按 `push_price` 买入、T+N 收盘卖出、
10 万本金整手、费用全走 `execution_model`。
`data/signal_backtest.py:446 backfill_pnl()` 负责回填；`_net_exc()` 的**硬规则**：
**基准缺失 → `net_excess_pct` 必须为 NULL，绝不用 0 冒充。**

**修复 2（聚合层）**：`strategy_effectiveness` 增 4 个净聚合列
（`avg_net_return / net_win_rate / avg_net_excess / net_n`），由
`compute_strategy_effectiveness()` 写入。

**证据（⑤段，内存库）**：
- 7 个净列齐全；
- 无基准时 `benchmark_pct IS NULL AND net_excess_pct IS NOT NULL` 的违规行数 = **0**；
- 有基准时 `net_excess = net_return - benchmark` 精确成立：`1.3868 - 0.0800 = 1.3068`（误差 < 1e-6）；
- 净收益恒小于毛收益（费用方向正确）。

**证据（QA 副本实测）**：`signal_pnl` 308 行，`net_return_pct` 非空 308/308，
违规行 0；`strategy_effectiveness` 87 行，其中 18 行有净聚合值。

---

## 七、2.6 负向战法亮红灯 + 优化器"只建议"

**修复 A（红灯）**：`core/strategy_health_flags.py` 内置 `NEGATIVE_KNOWN`
= P5-05A（28.6% / -5.6%）、P5-05B（21.7%）、P5-05C（22.7%）、P5-03A（16.7% / -7.1%）；
`strategy_effectiveness_dashboard.normalize_stats()` 为每行注入 `health_flag / label / reason`，
UI 明细表新增「健康度」「健康度说明」两列。**不等 200 条样本，先标红。**
**只标记，不删档，不参与打分。**

**修复 B（优化器只建议）**：`core/strategy_weight_optimizer.py`
- `:66` `auto_apply: bool = False`（默认只统计）；
- `:99/412/432` 新增 `net_weight_recommendation[strategy] = {weight, avg_net_return, net_win_rate, avg_net_excess_blend, basis, reason}`；
- `:400` 只有 `auto_apply=True` **且** `n_signals >= 200` 才写 `strategy_weights.json`；
- 净口径推荐**永不**参与自动改权重。

**证据（⑥段）**：`mode == "statistics_only"`、`weights == {}`（空）、
`net_weight_recommendation` 非空、报表落盘到隔离临时路径、
`avg_net_return = 9.78685`、`net_win_rate = 1.0`。

---

## 八、2.7 daemon → UI 战法面板快照链路（P0 级修复）

**根因（两个叠加）**：
1. `auto_sniper_daemon.py::_signal_backfill_pipeline()` 里 `from core.strategy_effectiveness_snapshot import build_ui_snapshot`，
   而该函数在旧版本**不存在** → 每晚 18:30 回填流水线每次都 `ImportError`，
   T+N 回填 / 有效性聚合 / 权重推荐**三件事从未真正执行**；
2. 即使函数存在，旧实现把 9 字段结果写到 `data/strategy_effectiveness_one_shot.json`（一次性快照路径），
   而 UI 面板读的是 `data/strategy_effectiveness_ui_snapshot.json` →
   daemon 每晚白跑，面板永远停在最后一次手工生成的那版。

**修复**（`core/strategy_effectiveness_snapshot.py:871-998`）：
- `UI_SNAPSHOT_JSON` 与 `RECOMMENDATION_JSON` 两条路径彻底分离；
- `build_ui_snapshot()` 输出 **15 个顶层字段**：
  `schema_version / generated_at / stat_date / mode / maturity / strategy_maturity /
   recommendation / net_recommendation / deprecated / stats / history /
   index_quotes / index_coverage / source / notice`；
- 组装失败也不抛：降级为空骨架 + `error` 字段（面板可见"数据未就绪"而不是白屏）；
- 落盘 `.tmp` → `os.replace()` **原子写**，不会出现半截 JSON。

**证据（⑦段）**：15 个字段齐全；`stats = 18`、`history = 87`、`index_quotes = 4`、
`index_coverage.primary_ready = True`、`net_recommendation = 18`、
`mode = statistics_only`、`stat_date = 2026-11-02`、`notice` 存在（只读声明）。

---

## 九、测试脚本扩展（为什么这轮才算"能自动回归"）

旧 `tools/test_execution_realism.py`（163 行）的③段依赖"行情自然触发事件"，QA 库日线覆盖不足时长期 `[SKIP]`
——等于最关键的前视/成交口径**从来没有被自动验证过**。本轮重写为 **8 组契约**：

| 段 | 内容 | 是否依赖真实行情 |
|---|---|---|
| ① | 费率 / 涨跌停 / T+1 / 整手 / 滑点夹逼 + 净收益<毛收益 | 否 |
| ② | `real_exec_returns` 与逐笔手工计算一致 | 是（不足则 SKIP） |
| ③ | **`run_strategy_backtest` 指标契约**：合成 150 行日线 + 桩引擎强制命中 | **否（代码级断言）** |
| ③b | 真实样本，仅 `--real` 才跑 | 是（不足则明确 `[SKIP]`） |
| ④ | 风险指标向量 | 否 |
| ⑤ | 净口径落库（含"基准缺失→NULL"隔离验证） | 否（内存库） |
| ⑥ | 净聚合 + 优化器净推荐 | 否（内存库） |
| ⑦ | Tier2.7 UI 快照 15 字段契约 | 否（隔离临时路径） |

③段核心断言：`buy_delay == 1`、`lookahead_risk is False`、旧 3 战法 `legacy_lookahead_risk is True`、
`legacy_delay0` 有 note、非旧战法不产 `legacy_delay0`、`real_exec` 9 字段齐、
`real_exec_delay1` 兼容、成本落在 `0.05%~0.30%` 合理区间。

**为什么用合成样本**：QA 库 `daily_data` 每股仅约 140 行，而 `run_strategy_backtest` 要求 `len(df) >= 140`
→ 只有最后约 10 个可回测日，扫 60 只 × 3 战法 = 0 命中。这是**数据覆盖不足**，不是代码回归。
所以把"代码契约"与"真实样本"拆成 ③ / ③b 两段：前者永远能跑，后者等数据补齐后跑。

---

## 十、本轮新发现并修复的真 Bug

### F1｜`tools/_smoke_index_bench.py`：DuckDB 2.0 pending result 让 `register()` 崩

- **现象**：脚本跑到 T3 直接 `exit=1`，异常为
  `ResourceInUseException: connection has an unfinished result from execute()`
- **根因**：同一连接上 `con.execute(...).fetchone()` **只取首行**会留下未读完的 pending result，
  紧接着同一连接 `con.register(...)` 时，DuckDB 2.0 判定连接仍在用 → 抛异常。
- **修复**：`:131/:133/:136` 三处 `fetchone()[0]` → `fetchall()[0][0]`（读空 result set）。
- **回归**：`RESULT pass=112 fail=0`，`exit=0`（修复前 exit=1）。
- **备份**：`_snapshots/_smoke_index_bench_before_fix_20261103.py`（20914 B，md5 `8aeade…`）。
- **顺带扫描**：全仓 6 个含 `.register(` 的文件，检查 register 前 12 行内是否有 `.fetchone()`
  → **0 处风险**（生产代码已由 `_fetch_one / _fetch_scalar` 收口）。

### F2｜`tools/test_execution_realism.py` 自身两处缺陷

1. `_run_contract()` 的 `finally` 恢复逻辑引用了未定义变量 `_eng` → 契约段一失败就二次崩；
   改为**逐项恢复**（`generate_plot` / `set_backtest_legacy_mode` 各自 try/finally）。
2. ⑦段打开 QA 库报 `Can't open a connection to same database file with a different configuration`
   → 改为先试 `read_only=True`，失败回退 `read_only=False`（**只执行 SELECT**）。

### F3｜`verify_system.py`：无写权限/被占库时误报 4 条 P0

- **现象**：本轮以「对主库文件无写权限」的会话跑体检，
  输出 `56 通过 / 4 失败`，4 条 FAIL 全是读库类：
  `DuckDB 检查失败` / `daily_data 无任何交易日数据` / `大盘估值表检查失败` / `信号落库闭环检查失败`，
  一律带 `IO Error: Cannot open file "data/quant_data.duckdb": 拒绝访问`。
  但同一时刻用**原生只读连接**可正常读出 `daily_data 238328 行 / 148 个交易日`。
- **根因**：`data/db_core/_connection.py::_duckdb_connect_readonly()` 名字叫只读，
  实际是 `duckdb.connect(db_path, config={})`（**读写模式**）——这是 V30.1 为规避
  "同一库不同配置"错误而做的有意统一。因此当主库文件对当前进程不可写（权限丢失 / 只读挂载 /
  其他进程独占）时，连**纯查询**也一起失败，再被体检解释成「数据为空 / 数据脏」。
- **修复（只动体检脚本，不碰 db_core）**：`verify_system.py` 新增 `_qc_ro_conn()`：
  优先取 `get_read_conn_singleton(max_wait_sec=8)`；取不到则退化为
  `duckdb.connect(get_duckdb_path(), read_only=True)`（**只执行 SELECT**，用完即关）。
  4 个读库检查点（DuckDB 检查 / 交易日历 / 估值温度 / 信号闭环）统一改走它；
  P0-10 再加一层兜底：`latest` 为空或日期序列取不到时，用原生只读直查 `MAX(trade_date)` 与 `DISTINCT trade_date`。
- **为什么不改 db_core**：`_duckdb_connect_readonly()` 的"读写模式"是**故意的**，
  进程内若出现 read_only=True 与读写两种配置并存，DuckDB 会直接拒绝第二个连接
  （历史事故：扫描成功后 `save_signal_log` 必失败，P2~P5 选了票却从不落库）。所以只允许在
  **体检这一条独立进程**里退化，不能动生产连接模型。
- **回归**：体检从 `56/4/0` → **`80 通过 / 0 失败 / 0 警告` → 「允许全自动运行（自动巡航 + 企微推送）」**。
  同一批检查现在给出真实数据：`daily_data 最新 20261102（148 个交易日）`、
  `dim_index_valuation 4416 行，最新 2026-10-21，覆盖 6 个核心指数`、
  `P5 20261021 选中 2 只，signal_log 落 7 条 P5 正式信号`。
- **备份**：`_snapshots/verify_system_before_ro_fallback_20261103.py`（56223 B）；
  改动共 +2008 字节，diff 见 §附录。

---

## 十一、回归结果（逐条命令 + 实际输出）

执行时间：2026-11-03 09:00~09:35（Asia/Shanghai）｜全部使用 `D:\xiaojie\_pyenv\venv\Scripts\python.exe`

| # | 命令 | 结果 | 关键输出 |
|---|---|---|---|
| 1 | `python -m compileall -q core ui tools data service` | **PASS** | `compileall exit=0` |
| 2 | `python tools\test_execution_realism.py` | **PASS** | `总体： PASS`（8 段全 OK；合成样本 total=15、`n_realized=15/15`、`cost_pct_avg=0.1119%`） |
| 3 | `python tools\_smoke_strategy_panel.py` | **PASS** | `RESULT pass=27 fail=0` |
| 4 | `python tools\_smoke_index_bench.py` | **PASS** | `RESULT pass=112 fail=0`（F1 修复后） |
| 5 | `python tools\test_signal_log_wiring.py` | **PASS** | 静态全过；动态「不传 rt 写 0 行 / 传 rt 写 1 行」 |
| 6 | `python tools\test_signal_ledger_write.py` | **PASS** | 幂等复写、ledger 不重复、无 `unfinished result` 警告 |
| 7 | `python tools\test_shadow_filters.py` | **PASS** | `RESULT: PASS (Shadow 层未参与任何过滤/打分/推送)` |
| 8 | `python verify_system.py` | **PASS** | 修复前 `56 通过 / 4 失败`；修复后 **`80 通过 / 0 失败 / 0 警告`，允许全自动运行** |

QA 副本数据现状（`data/_qa/quant_data.duckdb`，只读实测）：

| 表 | 行数 | 说明 |
|---|---|---|
| `signal_pnl` | 308 | 7 个净列齐全，`net_return_pct` 非空 308/308，基准缺失却写净超额的违规行 **0** |
| `strategy_effectiveness` | 87 | 4 个净聚合列齐全，18 行有净聚合值 |
| `signal_strategy_ledger` | 310 | 战法-信号映射账本 |
| `index_daily` | 704 | 4 个核心指数 |
| `daily_data` | 227158 行 / 2134 只 / 2026-03-25~2026-10-21 | 每股约 140 行（覆盖不足，见 §十二） |

---

## 十二、遗留与风险（不阻塞，但要知道）

| # | 事项 | 等级 | 说明与建议 |
|---|---|---|---|
| R1 | QA 库日线覆盖不足（每股约 140 行） | 中 | ③b 真实样本段长期 SKIP；`run_strategy_backtest` 要求 `>=140` 行，只剩最后 ~10 个可回测日。**等生产库补齐历史后**跑 `test_execution_realism.py --real` 即自动转 PASS |
| R2 | 生产库对"非主人会话"只读 | 中 | 本轮已用体检兜底绕开；但**写库仍然只能在主人账号/daemon 里做**。若用计划任务跑，务必用你自己的账号 |
| R3 | `pipeline_state` 落后于 `daily_data` | 中 | 体检实测：`daily_data` 最新 **20261102**，而 `last_sync_ok / last_p1_ok / last_p5_ok` 仍是 **20261021**。说明最近 8 个交易日的行情不是走正常流水线补的（或状态未回写）。**建议**：跑一次完整 daemon 日程（19:45 同步 → 19:55 P1 → 20:05 P5），确认状态推进 |
| R4 | 4 条负向战法仍在榜 | 低 | P5-05A/B/C、P5-03A 已标红但**未删档**（红线：不删策略）。样本到 200 条后由优化器决定 |
| R5 | 净超额依赖基准数据 | 低 | 基准缺失一律 NULL（不用 0 冒充）。若某段时间没同步指数，净超额会大片空值——这是**正确行为**，不是 Bug |
| R6 | `config.yaml` 明文密钥 | 低（你已确认单机自用） | 本报告与所有日志未出现任何 token / api_key / webhook 明文，仅以「已配置 / 未配置」表述 |
| R7 | `legacy_delay0` 会长期存在 | 低 | 属**有意保留**的对照数据，不建议删；否则历史结论不可追溯 |

---

## 十三、红线自查

| 红线 | 是否触碰 | 说明 |
|---|---|---|
| 不新增/删除功能、菜单、策略、指标、参数、数据源 | 否 | 只新增内部函数与净口径列；战法数量不变 |
| 不改评分权重/阈值/闸门/止损模板/策略逻辑 | 否 | 2.2 只把"虚假成交价"改成"可成交价"；优化器 `auto_apply` 仍默认 False |
| 不升级依赖 / 不重构 / 不删代码 | 否 | Python 3.11.9 + DuckDB 2.0.0 未动；`*.legacy.py` 全在 |
| `config.yaml` / `master_control.json` 业务数值不动 | 否 | 一个数字都没改 |
| 不 commit / push / 上传 | 否 | 仓库 `.git` 已按你要求删除；本轮用 `tools/backup_snapshot.py` + `_snapshots/` 单文件备份 |
| 报告/日志无密钥明文 | 是（合规） | 仅"已配置/未配置" |
| 每次改动最小化 + 根因→修复→证据 → 回归 | 是 | 见 §二~§十 |

---

## 十四、下一轮建议（按性价比排序）

1. **补数据 + 跑 `--real`**：把 `daily_data` 补到最新交易日后，`test_execution_realism.py --real` 的 ③b
   会从 SKIP 转 PASS，2.2/2.3 的数字才有真实样本支撑。
2. **让 `pipeline_state` 追上 `daily_data`**（R3）：这是"机器会说话"的前提，否则你会看到
   "数据是新的、但流水线说上周就停了"的自相矛盾。
3. **Tier 3.4 战法相关性矩阵**：用 `signal_strategy_ledger`（现有 310 行）按日聚合成战法日收益序列，
   算 Pearson 矩阵，输出 `rho > 0.9` 的战法簇——这是**最便宜的大发现**（重复计票一目了然）。
4. **Tier 3.1/3.2 样本外 + 稳健分**：等样本量上来（每战法 ≥ 60~100 笔）再做，否则折内样本太薄。

---

## 附录：本轮备份清单

| 文件 | 说明 |
|---|---|
| `_snapshots/src_20261102_224020_before_tier2_test_extension.zip` + `.manifest.json` | 改动前全量源码快照（253 文件 / 1.39 MB） |
| `_snapshots/test_execution_realism_before_tier2_ext_20261103.py` | 测试脚本改写前原版（6811 B） |
| `_snapshots/_smoke_index_bench_before_fix_20261103.py` | F1 修复前原版（20914 B） |
| `_snapshots/verify_system_before_ro_fallback_20261103.py` | F3 修复前原版（56223 B） |
| `_snapshots/tier2_logs_20261103/` | 本轮回归原始日志（`_t_verify*.log` 等） |

回滚方式：把上表文件覆盖回原路径即可（均为单文件，无目录结构变更）。