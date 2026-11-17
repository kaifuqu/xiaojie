# V30 升级日志（2026-10-13）—— 上证 / 深证 / 创业板 混合基准研究

> 需求来源（用户原话）：**「几个池选出来的股票在计算每日涨跌的时候，一定要把上证、深证和创业板的涨跌也放入一起研究。」**
> 用户明确：**不考虑北交所**；指数**只用于统计研究，不进入评分或选股决策**。

---

## 一、为什么要做 V30

V29 把「信号 T+N 收益回填」打通了，`signal_pnl` 表也预留了
`benchmark_pct / excess_pct / beat_benchmark` 三列。但生产库**从来没有
`index_daily` 表**，`daily_data` 里也没有任何指数行，于是：

```
_load_benchmark_series()  ->  SELECT ... FROM index_daily  ->  抛异常
                         ->  return None
                         ->  benchmark_pct / excess_pct 永远 NULL
                         ->  strategy_effectiveness.avg_excess 永远 NULL
                         ->  Streamlit 面板「平均超额%」整列是「—」
```

也就是说：**系统从上线至今，从来没能回答过一句最基本的话**——

> 「这只票 T+2 涨了 3%，是大盘也在涨 3%，还是它真的跑赢了？」

更严重的是：如果只拿沪深300 当唯一基准，一只在创业板涨了 5% 的票，
在沪深300 下跌 2% 的日子里会被判「超额 +7%」——**赢了大盘不等于赢了市场**。
只看单一宽基，会系统性高估中小盘/成长风格战法的表现、低估它们的真实风险。

V30 就是补上这最后一公里。

---

## 二、口径冻结（六条硬规则）

| 项目 | 口径 |
|---|---|
| 混合基准 | **上证指数 + 深证成指 + 创业板指 等权** |
| 沪深300 | **保留**为旧 `benchmark_pct / excess_pct` 口径，历史数字继续可比 |
| 沪市主板 600/601/603/605 | 上证指数 |
| 科创板 688/689 | 创业板指（系统无科创50，暂借用，仅作归因参考） |
| 深市主板 000/001/002/003 | 深证成指 |
| 创业板 300/301 | 创业板指 |
| 北交所 4xx/8xx/92x | **不考虑** —— 归因列保持 `NULL` |
| 指数缺数据 | 一律 `NULL`，**绝不写 0.0** |
| 指数缺某一天 | 取该日**之前最近的一个可用交易日**（前向取最近） |
| 对评分的影响 | **零**。指数只作统计参照，不进任何评分/过滤/闸门 |

> 「绝不写 0.0」是 V29 已确立的铁律：写 0.0 会让「超额 = 绝对收益」冒充超额，
> 让没有基准的样本看起来「刚好打平大盘」，是最隐蔽的一种数据造假。

---

## 三、升级项清单

| 项 | 文件 | 说明 |
|---|---|---|
| A | `data/index_daily_store.py`（**新增**） | 建 `index_daily` 表 + 同步 4 指数 + 混合基准计算 + 缺日前向取值 |
| B | `data/signal_backtest.py` | 回填链路读取 4 指数面板；`signal_pnl` 新增 11 列 |
| C | `data/signal_logger.py` | V30 纯加列迁移（幂等），并补齐两套历史 DDL 遗漏的 V29 列 |
| D | `auto_sniper_daemon.py` | 19:45 日线增量成功后同步 4 个基准指数 |
| E | `core/strategy_effectiveness_snapshot.py` | 快照新增 `index_quotes` / `index_coverage` |
| F | `core/strategy_effectiveness_dashboard.py` | 读取新增超额字段与指数行情，脏字段容错 |
| G | `ui/strategy_effectiveness_panel.py` | 市场水位条 + 混合归因表 + 散点图 X 轴口径切换 |
| H | `core/FIELD_CONTRACT.md` | 追加 V30 契约 |
| I | `core/strategy_weight_optimizer.py` | 推荐报表带出 V30 混合基准字段（纯加列） |
| J | `verify_system.py` | 体检清单加入 4 个 V30 模块 |
| K | `tools/v30_migrate_and_verify.py`（**新增**） | 幂等迁移 + 核验一体脚本 |

### 新增字段（全部为**纯加列**，旧列口径一律不动）

**`signal_pnl`（11 列）**

| 字段 | 含义 |
|---|---|
| `bench_sh_pct` | 上证指数区间收益 |
| `bench_sz_pct` | 深证成指区间收益 |
| `bench_cyb_pct` | 创业板指区间收益 |
| `bench_blend_pct` | 三者等权混合基准收益（**主口径**） |
| `excess_sh_pct` / `excess_sz_pct` / `excess_cyb_pct` | 相对三大指数的超额 |
| `excess_blend_pct` | 相对混合基准的超额（**主口径**） |
| `beat_blend` | 是否跑赢混合基准（布尔，无数据为 `NULL`） |
| `board_attributed` | 该票归属指数的中文名；北交所为 `NULL` |
| `board_excess_pct` | 相对**归属板块指数**的超额 |

**`strategy_effectiveness`（5 列）**

`avg_excess_sh` / `avg_excess_sz` / `avg_excess_cyb` / `avg_excess_blend` / `beat_blend_rate`

---

## 四、本轮修复的八个真实运行期 Bug

设计完成后做了一次真实链路的走查，发现三处**代码能编译、测试能过，但线上必然不出数**的问题。
这类问题正是「测试全绿 ≠ 功能可用」的典型样本，已全部最小修复并补回归：

### Bug 1：日期类型不匹配 → 全部超额列会变 NULL

- **现象**：`backfill_pnl()` 里的 `log_date / eval_date` 是 `_normalize_date()` 归一后的
  **`pd.Timestamp`**，而 `single_index_return_pct()` 内部直接 `str(log_date)`，
  得到的是 `'2026-01-05 00:00:00'`。指数序列的日期是 `'20260105'`，
  字符串比较永远不相等 → `_last_index_at_or_before()` 全部 miss → 所有超额列 NULL。
- **根因**：日期格式契约只写在调用方，没在数据层兜底。
- **修复**：`index_daily_store` 新增 `_norm_td()`，在指数层把任意日期形态
  （Timestamp / `YYYY-MM-DD` / `YYYYMMDD` / 脏值）统一归一成 8 位 `YYYYMMDD`。
- **回归**：T13 组 8 项断言（字符串入参、Timestamp 入参、blend、脏值、早于首日）。

### Bug 2：daemon 指数同步钩子从未真正执行

- **现象**：`_sync_benchmark_indices_after_daily()` 调用的是
  `sync_index_daily(db_conn=None)`，而该函数对 `db_conn=None` **直接 return**，
  于是这个钩子从上线起就一直在静默跳过，从未同步过一次指数。
- **根因**：调用方以为「None 会自动取连接」，实际函数契约是「None 即放弃」。
- **修复**：改调 `sync_index_quietly(lookback_days=130)`，由它负责取连接。
- **回归**：T15 组 5 项断言。

### Bug 3：`get_write_conn()` 被当普通函数调用

- **真实异常**：`'\'_GeneratorContextManager\'' object has no attribute 'execute'`
- **根因**：`get_write_conn` 是 `@contextmanager`，返回的是上下文管理器对象，
  原代码却写成 `db_conn = get_write_conn()` 然后当连接直接用。
- **修复**：`sync_index_quietly()` 改为两级取连接——
  1. 优先 `get_conn()`：拿**进程内全局写连接单例**。daemon 晚间链本来就在这个单例里
     （单写者铁律），复用它既不额外抢锁，也不会新开连接；
  2. 单例不可用时才退到 `with get_write_conn() as conn:` 短连接；
  3. 仍失败就老实记日志跳过，绝不让指数同步带崩 19:45 日线主链。
- **回归**：T9 组含 `sync_quietly 不抛`，T15 组含连接获取方式断言。

### Bug 5：`load_index_panel` 的 start_date 过滤恒定返回 0 行

- **现象**：生产迁移后跑回填，日志明明打印「指数面板已就绪」，但
  `bench_blend_pct` / `excess_blend_pct` **全部是 NULL**，而沪深300 有值。
- **真实根因**：`index_daily.trade_date` 是 `DATE` 列，
  `CAST(trade_date AS VARCHAR)` 得到的是 **ISO 带横线**形式（`'2026-06-05'`）。
  旧代码用 8 位串（`'20260605'`）去比：
  `'2026-06-05' >= '20260605'` 字符串比较恒为 **False**（第 5 位 `'-'`(45) < `'0'`(48)），
  于是带 start_date 的查询永远返回 0 行。
- **修复**：改为按 DATE 直接比较 `trade_date >= STRPTIME('YYYYMMDD','%Y%m%d')::DATE`；
  同时在 `_load_index_panel` 侧再加 30 天回看缓冲，保证区间起点可用。
- **回归**：T16 组 5 项断言 + 生产库实测 `混合基准=55/55` 全部落数。

### Bug 6：`latest_index_quotes` / 快照把日期截成 `'2026-10-'`

- **现象**：面板「市场水位条」的指数日期、以及快照 `index_coverage.start/end`
  显示成 `2026-10-`、`2026-01-` 这种**缺年份的残串**。
- **根因**：同样是 ISO 带横线串被 `str[:8]` 硬截。
- **修复**：先 `replace("-","")` 再取 8 位，统一成 `YYYYMMDD`。
- **回归**：T17 组断言 + 快照实测 `start=20260126 / end=20261013`。

### Bug 7：V30 面板就绪反而把旧沪深300 三列打成 NULL（口径回归）

- **现象**：修完 Bug 5 后，混合基准列有值了，但
  `benchmark_pct` / `excess_pct` / `beat_benchmark` **反而全变 NULL**。
- **根因**：加载逻辑写成
  ```python
  if not index_panel:
      benchmark_df = _load_benchmark_series(...)
  else:
      benchmark_df = None      # ← V30 面板一就绪，旧基准被主动置空
  ```
  新面板越健康，旧口径越没人填。
- **修复**：两套基准**彼此独立**加载，任一方缺失只影响自己那几列。
- **回归**：T18 组断言 + 生产库实测两套口径 `55/55` 并列非空。

### Bug 8：优化器与快照根本没读 V30 新列（面板全空）

- **现象**：库里有 `avg_excess_blend`，但面板每行仍是 `None`。
- **根因**：`strategy_weight_optimizer` 的 SELECT 列表里没有这 5 个新列，
  推荐报表 `stats` 里就没有它们；面板是快照的下游，自然全空。
- **修复**：SELECT 纯加列 + 每个基准**各自独立的加权累加器与分母**
  （共用一个分母会让某列缺数时把 0 混进别的基准均值）。缺列/缺数 → `None`，绝不 `0.0`。
- **回归**：生产库实测快照样本行
  `avg_excess=1.5126 / avg_excess_blend=2.5464 / beat_blend_rate=100.0`。

### Bug 4（旧口径会被悄悄换掉，一并加固）

- **现象**：`_load_benchmark_series()` 的 SQL **没有限定 `ts_code`**。
  V30 建表后同一交易日有 4 行指数，`fetchdf` 只返回**任意第一行**——
  很可能取到上证指数，**「沪深300 超额」会被悄悄换成别的口径**，且无任何报错。
- **修复**：SQL 加 `WHERE ts_code = '000300.SH'`，并加注释说明为什么不能删。
- **回归**：T14 组用 4 指数同日数据验证「只取沪深300」。

---

## 五、验证结果

| 项 | 结果 |
|---|---|
| `python tools/_smoke_index_bench.py` | **112 通过 / 0 失败**（18 组：T1–T12 原有 + T13–T18 本轮新增） |
| `python tools/_smoke_strategy_panel.py` | **27 通过 / 0 失败** |
| `python tools/_smoke_full.py` | **ALL GREEN** |
| `python tools/_smoke_real.py` | **4/4 通过** |
| `python tools/test_p1_vol_sun.py` | **T1–T6 ALL PASSED** |
| `python -m compileall`（全量） | **0 错误** |
| `python verify_system.py` | **50 通过 / 0 失败 / 1 警告**（警告为旧 daemon 锁 PID 已失效，启动时自动清理） |
| Streamlit 无头渲染（AppTest） | **0 异常**；指数水位条 4 个指数全渲染；归因表列 `上证超额% / 深证超额% / 创业板超额% / 混合超额% / 跑赢混合基准%` 齐全 |

### 生产库迁移与实测（迁移前已整库物理备份）

`python tools/v30_migrate_and_verify.py`（幂等，可重复运行）：

```
[1/5] 建 index_daily 表: True
[2/5] 同步 4 个基准指数（260 天窗口，幂等）
      ok=True written=680 per_index={'000001.SH':170,'399001.SZ':170,'399006.SZ':170,'000300.SH':170}
[3/5] 幂等扩列  signal_pnl 缺列: 无 | strategy_effectiveness 缺列: 无
[4/5] backfill: {'pnl_count': 55, 'eff_count': 15}
      optimizer mode=statistics_only 战法数=15（绝不改实盘权重）
[5/5] signal_pnl  总数=55 | 沪深300基准=55 超额=55 | 混合基准=55 超额=55 | 板块归因=55 板块超额=55
      strategy_effectiveness 总数=15 | 沪深300超额=15 | 混合超额=15 | 跑赢率=15
      指数覆盖度：4 指数各 170 行，2026-01-26 ~ 2026-10-13
      区间收益：上证 -8.027% | 沪深300 -9.163% | 深证 -13.711% | 创业板 -10.870%
                混合基准 -10.870%（等权混合，主口径）
      快照 index_coverage.primary_ready = True
```

**新旧两套口径并列抽样（同一行）**：

| log_id | 绝对收益% | 沪深300% | 混合% | 超额(300)% | 超额(混合)% | 归属指数 | 板块超额% |
|---|---|---|---|---|---|---|---|
| 23 | +8.353 | -0.678 | -1.693 | +9.031 | +10.046 | 创业板指 | +10.966 |
| 22 | +1.096 | -0.678 | -1.693 | +1.774 | +2.789 | 深证成指 | +3.129 |
| 21 | -2.677 | -0.678 | -1.693 | -1.999 | -0.984 | 深证成指 | -0.644 |
| 32 | -8.473 | -0.678 | -1.693 | -7.795 | -6.780 | 上证指数 | -8.039 |

> 这张表就是 V30 的意义所在：同一天、同一个 T+1，沪深300 只跌 0.68%，
> 但深证跌 1.36%、创业板跌 1.15%，**等权混合基准跌 1.69%**。
> 只看沪深300 会低估「非大盘股」的下跌幅度。
> 板块归因分布：上证指数 40 / 深证成指 7 / 创业板指 8（北交所不产出归因）。

---

## 六、怎么用

1. **每天 19:45** —— daemon 日线增量成功后自动同步 4 个指数（130 日回看窗）。
2. **每天 18:30** —— `backfill_pnl()` 算出 4 指数 + 混合基准的超额，写入 `signal_pnl`。
3. **随后** —— `compute_strategy_effectiveness()` 聚合出各战法的 `avg_excess_blend` / `beat_blend_rate`。
4. **打开 Streamlit** —— 页面底部「战法胜率面板」：
   - 顶部**市场水位条**：上证 / 深证 / 创业板 / 沪深300 当日涨跌 + 5 日 + 20 日
   - **混合归因表**：每条战法的绝对收益、三大指数超额、混合超额、跑赢率
   - **散点图**：X 轴可切换「绝对收益 / 混合超额 / 板块超额」

---

## 七、明确不做的事（口径冻结）

- ❌ 指数不进入评分、不改任何权重、不改任何阈值
- ❌ 不做自动调权（`strategy_weight_optimizer` 仍保持 `auto_apply=False`）
- ❌ 不新增数据源、菜单、策略、指标
- ❌ 不考虑北交所
- ❌ 不删任何代码，不升级依赖

> 一句话：**这次升级只增加「看得见的能力」，不给系统增加任何「自己会动的能力」。**
> 新老数字并存，老口径一列没动，随时可以关掉新列回到 V29。
