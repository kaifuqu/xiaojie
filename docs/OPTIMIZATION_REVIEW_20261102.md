# 小杰选股系统 · 优化执行情况复核报告（2026-11-02）

> 复核方式：全仓 `rg` 交叉检索 + 实跑 `verify_system.py` / `system_health.py` / `test_signal_log_wiring.py` / `test_execution_realism.py` / `shadow_report.py` + DuckDB 只读直查
> 结论口径：**只认实际生效的代码与实际库里的数据，不认文档声明。**

---

## 〇、一句话结论

> **执行质量很高——尤其"B1/B2 两个真 Bug"的定位，是本轮最有价值的收获。**
> **但"完成度"和"生效度"是两回事：已落地 18 项里有 3 项是"半成品"，另有 2 项被环境阻塞，1 项的口径判断需要我修正。**
>
> **最关键的一条**：你的系统现在**仍然停在 T-12（10-21）**，覆盖率 26%，daemon 已停 261 小时。
> **在数据补齐之前，Tier 2 那些"让数字变真"的改造，无法被验证。**

---

## 一、逐项复核（对照 `docs/TIER012_EXECUTION_REPORT_20261102.md`）

### 1.1 Tier 0 · 止血与恢复 —— 3/6 完成，2 项被环境阻塞

| # | 事项 | 声明 | 复核结果 | 判定 |
|---|---|---|---|:--:|
| 0.1 | 恢复 daemon + 补 8 个交易日 | 环境阻塞 | **实测：`daily_data` 最新仍 2026-10-21；`sniper.log` 最后写入 2026-10-22 15:06（261.3 小时前）；`daemon.lock` 是 PID 17608 陈旧 sentinel** | ⛔ 阻塞 |
| 0.2 | 开机自启 + 崩溃自启 | ✅ 脚本已就位 | **`tools/install_windows_task.ps1` 存在且设计正确**：`-AtStartup` 触发 + `-Once`+`-RepetitionInterval 5 分钟` 看门狗 + `-RestartInterval 1 分钟` + `AllowStartIfOnBatteries` + `-Hidden` 隐藏窗口。**但未注册**（需管理员执行） | ✅ 代码就绪 ⏳ 待执行 |
| 0.3 | 交易日历补到未来 | 环境阻塞 | **实测：`dim_trade_calendar` max = 2026-10-21，未来 0 行**（开市 736 天） | ⛔ 阻塞 |
| 0.4 | git 本地仓 | 改用 `backup_snapshot.py` | **实测：`git` 确实不可用（`fatal: not a git repository`）；`tools/backup_snapshot.py` 存在且功能完整（sha256 清单 / `--list` / `--diff` / `--keep N`）；`_snapshots/src_20261102_113141_tier012.manifest.json` 已生成** | ⚠️ **替代方案成立，但有隐患，见 2.1** |
| 0.5 | 凭据外置 `.env` | ⏸ 按你决定不做 | **实测：`config.yaml` 5 个凭据仍全明文**（Tushare token / HiThink key / DeepSeek key / 2 个企微 webhook）。`.env` 不存在，`.gitignore` 已预留 `.env` 条目 | ⏸ **你有否决权，但风险仍在** |
| 0.6 | 日志轮转 | ✅ 已具备 | `core/log_config.py` 是 `RotatingFileHandler(50MB, 10)` | ✅ 属实 |

### 1.2 Tier 1 · 把"已经写好的"接上 —— 3/6 完成

| # | 事项 | 声明 | 复核结果 | 判定 |
|---|---|---|---|:--:|
| 1.1 | **P2/P4 信号落库** | ✅ 已修并验证 | **我逐行确认了两处修复，真实生效**：<br>① `strat_p2_auction.py:662` 与 `strat_p4_tail.py:1163` 均已补 `rt=rt`<br>② `pool_signal_adapter_v28.py:60-70` 已区分「进程单例（不关）」与「独占短连接（自己关）」，`finally` 里 `_opened_here` 才关<br>③ 回归 `tools/test_signal_log_wiring.py` 实跑 **PASS**（静态 AST 断言 p2/p3/p4 都有 rt；动态在 DuckDB 副本上验证「不传 rt → 0 行 / 传 rt → 1 行」） | ✅ **属实且扎实** |
| 1.2 | P5 死函数 + 10 个 `anti_fly_*` 键 | ✅ 已显式标注"未启用" | `p5_postmarket_screener.py:492-497` 有 `P5_DISABLED_FEATURES` 常量，三个死函数 docstring 已标注 | ⚠️ **半成品，见 2.2** |
| 1.3 | "剔除最大阴量" | ⏸ 暂缓 | **实际已上 Shadow**：`core/shadow_filters.py` 实现了 `bear_volume_verdict()`，并在 `pool_signal_adapter_v28.py:205` 被 `annotate_shadow()` 调用 | ✅ **比声明更好** |
| 1.4 | RPS 灰度到 P2/P3 | ⏸ 暂缓 | **实际已上 Shadow**：`shadow_filters.rps_verdict()`（`RPS_GATE_MIN=50`），走 `core/p1_rps_calculator.RPS_PERIOD=60` | ✅ **比声明更好** |
| 1.5 | 数据覆盖率 + 新鲜度徽章 | ✅ 已接进 UI | `tools/system_health.py:53 data_health_snapshot()` + `ui/data_health_badge.py` + `ui/app.py:1807-1809` 已挂载 | ✅ 属实 |
| 1.6 | 推送降噪 | ⏸ 暂缓 | **实际已上 Shadow**：`shadow_filters.classify_push_tier()` 三档 | ✅ **比声明更好** |

### 1.3 Tier 2 · 让数字变真 —— 5/6 完成

| # | 事项 | 声明 | 复核结果 | 判定 |
|---|---|---|---|:--:|
| 2.1 | 统一成交口径 | ✅ 新旧并列 | **`core/execution_model.py`（227 行）是真·唯一真值**：佣金万3最低5元 / 印花税万5仅卖 / 过户费十万分之一 / 涨跌停四档（主板10%、ST5%、创业板300·301 20%、科创板688·689 20%）/ 整手100股 / T+1 / 一字板不可成交 / 滑点夹逼在 `[low,high]`。`backtest_runner.py:154` 确实 import 并复用 | ✅ 属实 |
| 2.2 | `buy_delay=0` 前视偏差 | ✅ 已量化未擅自改 | `metrics["lookahead_risk"]` + `metrics["real_exec_delay1"]` 并列输出 | ✅ **处理得很克制，正确** |
| 2.3 | 一字板/停牌 + 滑点夹逼 | ✅ 已生效 | 实测：滑点被夹在 10.2（当日 high）、一字涨停买不进、一字跌停卖不出；`n_buy_blocked`/`n_sell_blocked`/`fill_rate_pct` 都有 | ✅ 属实 |
| 2.4 | 补 Sharpe / Sortino / Calmar / VaR | ✅ 已加 | `calc_risk_metrics()`，实测 `sharpe_annual=1.0366, sortino_annual=2.4214, calmar=3.809, var95=-1.885`；`risk_real` 与 `risk_legacy` 双口径并存 | ✅ 属实 |
| 2.5 | 胜率口径升级 | ✅ 已加净收益 | `win_rate_net` / `avg_net_ret` / `cost_pct_avg`（实测 0.1119% 往返）；painpoint 路径加 `approx_mean_forward5d_net_return_pct` 等 4 个字段 | ✅ 属实 |
| 2.6 | 负向战法亮红灯 | ✅ 已加 | `core/strategy_health_flags.py` + `dashboard.normalize_stats()` 注入 + UI 两列 | ✅ 属实（但见 2.3） |

---

## 二、我发现的 5 个问题（按严重度）

### 🔴 问题 1：整个 Tier 2 无法验证——因为数据停在 T-12

**实测**（`tools/system_health.py` 实跑）：
```
[FAIL] 数据新鲜度 | 最新 2026-10-21，落后 12 天 —— 系统未在跑或同步断了
[FAIL] 覆盖率     | 最新日 1439 只 / 在市 5576 只 = 26%
[FAIL] 信号样本   | p2=0 p3=11 p4=0 p5=109 合计=120
[FAIL] daemon     | sniper.log 最后写入 2026-10-22 15:06（261.3 小时前）
```

**这条最要紧**：Tier 2 的所有成果（真实成交口径、风险指标、净收益胜率）**都建立在历史日线上，而历史日线只到 10-21**。
更要命的是：**T+1/T+5/T+10 的前瞻收益本身需要"未来数据"**——现在连 10-21 之后的数据都没有，
**`signal_pnl` 的 308 行里，新口径的样本几乎全是"旧数据 + 新算法"**，无法回答"新口径下这些战法还成立吗"。

**并且**：1.1 修好了 P2/P4 落库，但 **`signal_log` 至今 p2=0 / p4=0**——因为**这两池从没在新代码下跑过一个交易日**。
**B1 修好了，但还没被证实修好了。**

> **这是当前唯一的 P0。** 别的都可以等。

### 🟠 问题 2：`backup_snapshot.py` 替代 git —— 有一个致命缺口

**代码事实**：
- `tools/backup_snapshot.py` 的 `make_snapshot()` 生成 zip + sha256 manifest
- `_iter_files()` 会遍历项目文件
- **但 `.gitignore` 里排除了 `config.yaml`、`.env`、`data/`、`_pyenv/`** —— 快照工具**不读 `.gitignore`**（它是自己一套 `_iter_files()` 规则）

**风险点**：
1. **`config.yaml` 里现在有 5 个明文凭据**。如果 `_iter_files()` 没排除它，**每次快照都把凭据复制一份进 zip**——这些 zip 会在磁盘上留 N 份明文密钥。
2. 快照是**全量 zip**，不是增量 diff。文件多了以后每次快照都慢，占盘。

> **建议（不改代码也能做）**：立刻检查 `_snapshots/` 下已有的 zip 里有没有 `config.yaml`；若有，**删掉那些 zip 并立即轮换凭据**。

### 🟠 问题 3：H3 只做了"标注"，但 14 个配置键仍然"能调不生效"

**代码事实**：`P5_DISABLED_FEATURES` 常量定义了「未启用」清单，**但它是个纯元数据**——没有任何代码读它、没有 UI 显示它、没有告警。
而那 10 个 `anti_fly_*` 配置键**仍然躺在 `config.yaml` 里可调**（我实测 `rg "anti_fly"` 在 config.yaml 中有命中）。

**这就还是"看起来能调其实没跑"**——只是加了个注释。
**真正的"消除"是二选一**：
- **A. 接线**（P5 主链调一次 `anti_fly`）→ 但这会改选股结果，需要 Shadow 差集后再定
- **B. 删除**（把这 14 个键从 `config.yaml` 删掉、在配置注释里写明"未实现"）→ **零风险，推荐**

**我的判断**：选 B。理由是——**如果一个规则连 Shadow 验证的价值都没有（`fact_limit_theme` 极稀、炸板池 0 行），接线只是把一个未验证的规则塞进主链**。

### 🟡 问题 4：健康度红绿灯用"硬编码负向名单"打头，数据源是空的

**代码事实**：`core/strategy_health_flags.py` 的 `NEGATIVE_KNOWN` 硬编码了 4 条：
```
P5-05A: 2026-10 快照：胜率 28.6% / 平均 -5.6%
P5-05B: 2026-10 快照：胜率 21.7%
P5-05C: 2026-10 快照：胜率 22.7%
P5-03A: 2026-10 快照：胜率 16.7% / 平均 -7.1%
```
而 `_match_known()` **优先返回硬编码值**，只有不在名单里的才走统计判定。

**问题**：
1. 这 4 条的胜率**是裸收益、不是净收益、不是样本外**——正是 Tier 2 要纠正的口径。**用旧口径的结论去否决新口径的战法，是循环论证。**
2. `strategy_effectiveness` 表有 69 行、`signal_pnl` 308 行、ledger 302 行——**数据其实有**，硬编码却盖过了它。
3. "P5-03A 胜率 16.7%" 这种 6 样本的胜率，**Wilson 95% 置信区间宽到覆盖 50%**，本身就不该下结论。

> **建议**：`NEGATIVE_KNOWN` 降级为**「待复核提示」**而非**「直接判负」**——UI 显示"⚠️ 2026-10 裸口径曾负向，待净口径复核"，而不是钉死的 🔴。
> 这样既保留了"提醒你看一眼"的价值，又不把噪声变成判决。

### 🟡 问题 5：P5 落库走的是**另一条路径**，B1 的修法没覆盖它

**代码事实（我追了完整链路）**：
- **P2/P3/P4** 走 `core/strategies/strat_pX.py` → `pool_signal_adapter_v28.append_signal_log_safe(res, pool, db_conn, rt=rt)`
- **P5** 走的是**完全不同的一条**：`core/scan/pipeline.py:1447` 的 `save_signal_log(b, pool_key, regime, ...)` → `core/scan/intraday_snapshots.py:131` → 价格取 `item.get('现价')`

**后果**：
- `tools/test_signal_log_wiring.py` 静态检查的 `TARGETS` 里 **p5 标的是 `rt=缺失`**，但 `MUST_PASS_RT = ("p2", "p4")` —— **p5 被显式豁免**
- 实证：P5 有 109 行、`push_price` 全部 > 0（6.94~260.65）—— **说明 P5 的价格链路本来就是通的**，走的是 `现价` 字段
- 但这带来一个**一致性隐患**：P5 的 `push_price` 来自 `b["现价"]`（字符串格式化后的行情），P2/P3/P4 来自 `rt["price"]`（实时快照）。**两个价格源不同**，同一只票同一天可能出现两个微小的 push_price 差异。

**另外**：P5 的 `append_signal_log_safe` 调用（`strat_p5_postmarket.py:901`）**确实没传 rt**，但那条路径 `res` 里没有 `price` 键——**如果哪天 P5 走到这条路径，价格为 0 → 又一次静默不落库**。这是个**潜伏的同类 Bug**。

> **建议**：把 P5 的 `append_signal_log_safe` 调用补上 `rt=rt`（无害，因为 `res` 里本来就没 price，adapter 会优先用 rt），并把测试的 `MUST_PASS_RT` 加上 `p5` —— **让它变成"双保险"而不是"隐患"**。

---

## 三、我认为做得特别好的 4 个地方（值得固化）

| # | 做法 | 为什么好 |
|---|---|---|
| 1 | **新旧口径并列，绝不悄悄替换**（`risk_real`/`risk_legacy`、`real_exec`/旧字段、`real_exec_delay1`/原 `buy_delay`） | 这正是 hikyuu 的核心哲学（语义正确 > 数字好看）。而且它让"改口径会降多少"变成可量化、可讨论的事，而不是一次不可逆的变更 |
| 2 | **Shadow 层纪律**（`core/shadow_filters.py` 三个函数 + `tools/shadow_report.py` 对照报表） | 「假如这条规则启用，今天会否决/降级哪些信号」——这正是我上一份报告里说的"最成熟的做法"，你直接复用了。**而且 1.3/1.4/1.6 三件"暂缓"的事，你其实已经用 Shadow 做了** |
| 3 | **`buy_delay=0` 只量化不擅自改** | 前视偏差是"纠正"，但纠正会改数字口径。**留给人类拍板是对的**——你已经用 `lookahead_risk` 标记 + `real_exec_delay1` 给出了对比 |
| 4 | **B2 那个 Bug 的定位质量** | `resolve_write_conn()` 契约写明"单例不得关闭"，而调用方在 `finally` 里关了它 —— 导致"**同进程只有第一次落库能成功，之后每次静默失败**"。这是极其隐蔽的一类 Bug（异常只进 debug 日志）。**能定位到这一层，说明对 DuckDB 单写者模型理解得很深** |

---

## 四、当前的阻塞清单（按顺序解）

### P0（今天就能做，但必须你在本机做）
```
□ 1. start_daemon_24x7.bat                    # 恢复数据（阻塞一切验证）
□ 2. 补完 10-22 ~ 最新交易日
□ 3. 连续跑 3 个交易日，确认 signal_log 出现 p2≥1 且 p4≥1
     （这才是 B1/B2 的真正验收，当前 p2=0 p4=0）
□ 4. 检查 _snapshots/*.zip 里有没有 config.yaml（明文凭据）
□ 5. 轮换 5 个凭据（如果你决定做 0.5）
```

### P1（不需要数据也能做）
```
□ 6. 修问题 5：P5 的 append_signal_log_safe 补 rt=rt；测试 MUST_PASS_RT 加 p5
□ 7. 修问题 3：H3 二选一 —— 推荐「删键 + 配置注释标明未实现」（零风险）
□ 8. 修问题 4：NEGATIVE_KNOWN 从「直接判负」降级为「待复核提示」
□ 9. 修问题 2：让 backup_snapshot.py 读 .gitignore 排除规则（config.yaml / .env）
□ 10. 补 dim_trade_calendar 未来一年（静态化，不依赖在线接口）
```

### P2（数据补齐后）
```
□ 11. 重算 strategy_effectiveness 快照 → 健康度红灯才有一致的数据源
□ 12. run_daily_backfill → signal_pnl 用新净口径重算
□ 13. 1.3/1.4/1.6 的 Shadow 差集积累到 ≥30 条后，看对照表再决定是否接线
```

---

## 五、对整体节奏的评价

### ✅ 做对的
1. **顺序对**：先接已有的 → 再让数字变真 → 再防过拟合。**没有一上来就加新战法**，守住了 `auto_apply=False` 的纪律。
2. **红线守得住**：不改评分/权重/阈值、只改"接线"与"输出字段"、新旧并列、不删旧代码。
3. **发现了真 Bug**：B1/B2 是有价值的收获，不是搬运。

### ⚠️ 需要调整的
1. **"完成度"被高估了**：Tier 0 里 3 项被环境阻塞（其中 2 项是**根本性阻塞**），实际可用度约 55%。
2. **有一条依赖链没画出来**：**B1/B2 的验收依赖"系统跑起来"**。修好代码但系统停着 = 无法验收。
3. **两处"标注代替解决"**：H3（14 个死配置键）、健康度硬编码名单。**标注是第一步，不是终点。**

### 📌 一句话建议

> **代码层面这一轮做得很好；但现在最该做的不是继续改代码，而是「让系统跑起来 3 个交易日」。**
>
> 因为：
> - B1/B2 的修复**还没被任何真实交易日验证过**（p2=0 p4=0）
> - Tier 2 的全部成果**没有新数据可测**
> - `signal_log` 120 条 / 7 个交易日的样本，**不跑就不会长**
>
> **补数据 = 唯一的 P0。** 代码可以再写，但 3 个交易日的真实数据买不到。

---

*本报告基于 2026-11-02 实跑 `verify_system.py`（79 通过 / 0 失败 / 1 警告）、`system_health.py`（1 OK / 3 WARN / 4 FAIL）、`test_signal_log_wiring.py`（PASS）、`test_execution_realism.py`（PASS）、`shadow_report.py`（2 条信号 / 1 交易日）及 DuckDB 只读直查生成。*
*不含任何凭据明文。*
