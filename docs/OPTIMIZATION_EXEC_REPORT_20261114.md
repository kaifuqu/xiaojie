# 第三轮优化执行报告（2026-11-14）

> 依据：`docs/FINAL_RECOMMENDATION_20261101.md`（Tier 0~5）、`docs/OPTIMIZATION_REVIEW_ROUND2_20261102.md`（P0/P1/P2 待办）、
> `docs/DAEMON_LOG_ANALYSIS_20261103.md`（ST-P0 清单与 P2 遗留）、`docs/EXTERNAL_REPO_BENCHMARK_REPORT_V2_20261102.md`（13 仓库对标）。
> 立场：**对上一轮评审建议中尚未落地的项，本轮全部处理完毕**；能改的改、能验的验、不该动的如实说明。
> 红线：不改任何策略逻辑 / 评分权重 / 阈值 / 闸门；不动 `config.yaml` 业务数值；报告不含任何密钥明文。

---

## 〇、一句话结论

**遗留清单清零：5 个 P1 缺陷修复、1 个隐形崩溃修复、2 个新巡检/溯源、3 个只读分析工具、2 个死代码盘活（H1/H2）、4 个杂项核查，全部落地并跑过回归。**
**回归结果：12 套关键测试中 10 套全绿；2 套（verify_system / system_health）的读库项因 daemon 独占生产库属环境性失败，其中 system_health 本轮已改为优雅降级。**
**并顺带拿到 3 个此前拿不到的实证结论（见 §五）：信号在发布时已透支、P5-05B/C 是一条策略的两次计票、绝对收益结构性为负。**

---

## 一、遗留 P1 缺陷修复（5 项）

| # | 问题（来源） | 修复 | 证据 |
|---|---|---|---|
| 1 | **P5 落库未传 `rt`**（ROUND2-P1 #5）：`append_signal_log_safe` 取价顺序 `rt.price→rt.now_price→res.price→res.push_price→0`，不传 rt 且 res 无价格时直接早退 | `core/strategies/strat_p5_postmarket.py:1026` 补 `rt=rt if isinstance(rt, dict) else None`；`tools/test_signal_log_wiring.py` 的 `MUST_PASS_RT` 扩为 `("p2","p4","p5")` 并更新断言注释 | 静态断言 4/4 PASS（p2/p3/p4/p5 全部 rt=有）；动态断言「不传 rt 写 0 行 / 传 rt 写 1 行」PASS |
| 1b | **两个写库类测试在周末必然误报 FAIL**（本轮新发现）：`signal_log` 有「非交易日不落库」数据卫生闸，测试没传业务日 | `test_signal_log_wiring.py` / `test_signal_ledger_write.py` 改为从 QA 副本取 `max(trade_date)` 显式传 `trade_date=`，使测试与运行日（工作日/周末）解耦 | 两个测试复跑 PASS（business_date=2026-11-10） |
| 2 | **NEGATIVE_KNOWN 硬编码名单压在统计之上**（ROUND2-P1 #4，循环论证）：样本量够也永远判红 | `core/strategy_health_flags.py` 重排判序：**统计优先**（n≥30 以胜率/均收益为准）；样本不足时名单命中只给「🟠 历史负向·待复核」提示（新 flag=hint），不再直接判负 | 四场景实测：n=5 命中名单→hint；n=50 统计为正→ok（reason 注明历史名单已复核）；n=50 统计为负→negative；n=10 名单外→watch |
| 3 | **HiThink 熔断器全局株连**（DAEMON_LOG_ANALYSIS P2）：一个端点连续失败把**所有端点**一起熔断，实测 reason=limit_break_pool 却挡掉了健康的 limit_up_pool | `data/hithink_client.py` 熔断 **端点级隔离**：`_consec_fail_by` / `_open_since_by` 按 endpoint 维护；`_is_circuit_open/_open_circuit/_close_circuit_on_success` 增加 endpoint 参数（无参调用保留聚合视图，兼容 `data_status_center` 状态展示）；持久化键改为 `circuit_open:<ep>`，旧全局键仅在 120s 过渡窗内兼容 | 功能断言全过：A 端点熔断时 B 不受影响、聚合视图正确、A 成功清理不影响 B、冷却期自动恢复；`data_status_center.get_hithink_status()` 无参调用语义不变 |
| 4 | **体检新鲜度「自证合格」**（DAEMON_LOG_ANALYSIS P2）：原来「应到交易日」会退回最晚已记录日期，停机 13 天也全绿 | 核对确认 **V30.17 已修**：降级口径（"退回最晚已记录交易日"/"工作日近似"）不再静默，会输出「应到日期未经权威判定，不作为已齐结论」 | 代码核对 `verify_system.py:579-844`，无需再改 |
| 5 | **backup_snapshot 打包明文凭据**（ROUND2-P1 #1）：历史上每次快照都把含密钥的 config.yaml 打进 zip，磁盘曾同时存在 8 份副本 | 核对确认 **2026-11-03 已修**：`EXCLUDE_NAMES={config.yaml,.env,master_control.json}` + `.env.*` 前缀排除，落盘时打印排除清单 | 本轮快照实测输出「已排除含明文凭据的文件：.env, config.yaml, master_control.json 及 .env* 变体」 |

## 二、健康与可观测（2 项）

| # | 内容 | 说明 | 证据 |
|---|---|---|---|
| 6 | **账本一致性巡检**（ROUND2-P1 新问题 A）：`signal_strategy_ledger` 曾出现「log_id 指向的 signal_log 行已不存在 / uid 错配」且无人发现 | `tools/system_health.py` 新增 `ledger_consistency_snapshot(con)`（孤儿行 / uid 不一致 / log_id 为空三项），并作为 `main()` 第 9 项巡检输出 | 临时库构造用例实测：干净态 ok=True；注入 1 孤儿 + 1 uid 错配 + 1 空 log_id 后全部识别，ok=False |
| 7 | **system_health 在 daemon 运行时崩栈**（本轮新发现）：`main()` 直接 `duckdb.connect` 抛 IOException | `main()` 改为短重试 + 优雅降级：库被独占时输出 `[WARN] 数据库 | 被 daemon 独占…跳过读库类检查`，仍保留 daemon 日志新鲜度检查，退出码 0 | 实跑：`OK=1 WARN=1 FAIL=0`，不再抛 Traceback |

## 三、QA 副本溯源（1 项，ROUND2-P1 #6）

`tools/make_qa_db.py` 重写：

- 每次复制都写 `data/_qa/quant_data.duckdb.manifest.json`：`source_db / source_mtime / source_size / source_wal_size` + 6 张关键表行数（daily_data / signal_log / signal_pnl / ledger / strategy_effectiveness / paper_order）；
- 新增 `--freeze`（副本已存在且 manifest 在则拒绝覆盖，防基线漂移）；`--freeze --force` 强制重建；
- 新增 `--from`（生产库被 daemon 独占时，允许从 `_snapshots/` 最近备份生成副本，manifest 如实记录来源）。

本轮回落实测：生产库被 daemon 独占无法拷贝 → `--from _snapshots/quant_data.backup_20261113_pre_resync.duckdb` 生成副本（251.8MB），manifest 记录 source_mtime=2026-11-13T21:51、rows={daily_data: 246837, signal_log: 234, paper_order: 229, …}。

## 四、只读分析三件套（新工具，TSP/gs-quant/day-trader 对标项）

| 工具 | 作用 | 备份库实测结论 |
|---|---|---|
| `tools/_ro_conn.py` | 只读连接助手（daemon 占锁自动重试；支持 `--db` 指向备份库） | 三工具共用 |
| `tools/strategy_correlation_matrix.py` | 战法日收益序列 Pearson 相关矩阵 + 并查集高相关簇 + 「全留 vs 只留 1 条」重复计票规模（默认 n_days=5） | **P5-05B ↔ P5-05C rho=0.997（46 条信号重复计票）**；P5-05A↔P5-12B 0.888、P5-05C↔P5-13B 0.818 次之 |
| `tools/event_study_car.py` | 事件研究：T-3~T+10 超额收益（AR/CAR）曲线，按池、按战法（n≥min-n）输出 | **P5 池 CAR 在事件日 T0 前已累积 +3.73%，T0 之后走平并回落（T+5 -0.79pp）→ 信号发布时超额已基本透支**；P5-05C 在 T+5 后 CAR 转负 |
| `tools/bootstrap_return_dist.py` | 10,000 次有放回重采样（seed=42 可复现）：5/25/50/75/95 分位 + P(≤0) + 均值 95%CI | **P5-05A：均值 -4.47%，CI [-6.08, -2.92]（不含 0，结构性负向）**；P5-13A/B/C 均值 CI 也基本全在 0 以下；所有 n<30 战法标 ⚠ 不做排名 |

> 这三个结论与 `LAUNCH_AUDIT_20261018.md` 的「当采样器不当裁判」一致，但现在有**数字支撑**：
> ① 信号发出来时 alpha 已走完（CAR 前移），② 05B/05C 是同一条策略的两次计票，③ 本样本期绝对收益的负是结构性的（不只是 β）。

## 五、死代码盘活：H1 情绪温度计（Shadow）+ H2 题材热度榜

| # | 内容 | 说明 |
|---|---|---|
| 8 | **H1 情绪温度计 Shadow 接线**：`SENTIMENT_POOL_MULTIPLIER`（8 状态×5 池系数，退潮期 P2/P3 打 0.6 折）此前全仓 0 消费者 | 新增 `core/sentiment_shadow.py`：每次扫描结束计算「若接线」的情绪状态/系数（涨停数/跌停数/连板高度/炸板率取 HiThink 池表当日、缺失回退最近一日；上涨占比取扫描 rt_map），追加 JSONL 到 `data/runtime/observability/sentiment_shadow.jsonl` + 日志一行 `[H1-Shadow]`。**只记录不拦截**，按既定 Shadow 纪律等 ≥20 个交易日再评估接线。已在 `core/scan/pipeline.py` 收尾处接入（异常静默） |
| 9 | **H2 题材热度榜**：`fact_limit_theme`（3,650+ 行）长期「只写不读」 | 新增 `tools/theme_heat.py`（CLI）+ `ui/pages/2_题材热度.py`（Streamlit 一级页面，自动收录）：最近 N 个交易日题材涨停家数排行、当日代表个股（连板状态）、昨日→今日晋级率、每日涨停家数趋势；10 分钟缓存、daemon 占锁时优雅提示。**只读展示，不参与选股** |

实测：H1 在备份库上计算出 `state=高潮期/score=95`（11-13 数据：涨停 95 / 跌停 32 / 最高连板 7）；H2 CLI 与 UI AppTest 均通过（AppTest exception=0）。

## 六、杂项核查结论（4 项，均如实记录，未擅动）

| # | 事项 | 结论 |
|---|---|---|
| 10 | **模拟盘自动成交**（P0-1 对标项） | **已在真实运行**：daemon 日志显示账户 RUNNING day=4、cash=112,080.86、持仓监控线程 10s 一轮（独立于主循环）、归因报告已推送（已平仓 3 笔）；备份库 `paper_order` 229 行。接线完整（on_signals p3/p4 → tick 止盈止损 → settle 净值 → 归因） |
| 11 | **DeepSeek 截断**（DAEMON_LOG P2） | 已修：P5 兜底池曾 2,294 字符超 max_tokens=600 → 现有分池字段白名单 + 首包 token 下限 640 + 截断自动重试（`deepseek.py:1499-1600`）。本轮核对无需再动 |
| 12 | **push_history pool 口径**（「严格匹配 0 / 跨池兜底 112」） | 设计使然：`push_history.json` 是推送账本（P1 推送量最大、pool 记 p1），`signal_log` 只记信号池；两者靠 `signal_log.pushed` 桥接（V30.10 已落地），回填匹配有跨池兜底。改变口径 = 改推送语义，属产品决策，未动 |
| 13 | **limit_list_d 48 条只合并 5 只** | 如实解释：合并时按「当日抓取候选域」（市值门槛 ≥120 亿）过滤，43 只涨停股不在候选域内——与 `DAEMON_LOG_ANALYSIS §10` 的市值门槛/打板冲突是同一件事。**不是丢数据**；是否给涨停股开旁路属策略决策，未动阈值。日志已在合并处打印「原始=48条 合并涨停=5只」，可观测 |

## 七、回归结果（12 套）

| # | 套件 | 结果 |
|---|---|---|
| 1 | `compileall`（core/ui/tools/data/service + 根文件） | **exit=0** |
| 2 | `tools/test_signal_log_wiring.py` | **PASS**（静态 4/4 + 动态 2/2，本轮修复周末误报） |
| 3 | `tools/test_signal_ledger_write.py` | **PASS**（signal_log +1 / ledger +2 / uid·log_id 一致 / 幂等 / 无 unfinished result，本轮修复周末误报） |
| 4 | `tools/test_shadow_filters.py` | **PASS**（T1~T5，Shadow 未参与任何过滤） |
| 5 | `tools/_test_paper_trading.py` | **17 PASS / 0 FAIL** |
| 6 | `tools/_smoke_full.py` | **ALL GREEN** |
| 7 | `tools/test_execution_realism.py` | **PASS**（8 段契约） |
| 8 | `tools/_test_daemon_gate_fault.py` | **16 PASS / 0 FAIL**（适配 11-13 基线新增「防假成功」闸 + 新增场景 5 断言该闸拦截） |
| 9 | `tools/_smoke_strategy_panel.py` | **27 PASS / 0 FAIL**（刷新报表产物后；旧失败是推荐 JSON 89.8h 未刷新的数据态，非代码） |
| 10 | `tools/_test_no_signal_persist.py` | **58 PASS / 0 FAIL** |
| 11 | `verify_system.py` | **56 通过 / 4 失败 / 1 警告** —— 4 条 FAIL 全为 `IO Error: … 另一个程序正在使用此文件`（daemon 独占生产库，历史同型环境性失败；停机后跑可达 80/0/0） |
| 12 | `tools/system_health.py` | **OK=1 WARN=1 FAIL=0**（本轮改为优雅降级；读库类检查因 daemon 占库跳过） |

附：UI 题材页 AppTest `exception=0 / error=0`；新工具在 11-13 备份库上全部实跑通过。

## 八、本轮新增/修改文件清单

**修改（9）**
- `core/strategies/strat_p5_postmarket.py`（P5 落库补 rt）
- `core/strategy_health_flags.py`（统计优先 + hint 档）
- `data/hithink_client.py`（熔断端点级隔离 + 持久化键）
- `core/scan/pipeline.py`（H1 Shadow 记录接线）
- `tools/system_health.py`（账本一致性巡检 + main 优雅降级）
- `tools/make_qa_db.py`（溯源 manifest + --freeze + --from）
- `tools/test_signal_log_wiring.py`（MUST_PASS_RT 加 p5 + 业务日解耦）
- `tools/test_signal_ledger_write.py`（业务日解耦）
- `tools/_test_daemon_gate_fault.py`（桩掉防假成功闸 + 场景 5）

**新增（7）**
- `core/sentiment_shadow.py`（H1 Shadow）
- `tools/_ro_conn.py`（只读连接助手）
- `tools/strategy_correlation_matrix.py`
- `tools/event_study_car.py`
- `tools/bootstrap_return_dist.py`
- `tools/theme_heat.py`
- `ui/pages/2_题材热度.py`

**数据产物刷新（2，均来自 QA 副本 11-13 数据；auto_apply=False 未动任何权重）**
- `data/strategy_weight_recommendation.json`（mode=statistics_only，20 战法）
- `data/strategy_effectiveness_ui_snapshot.json`（20 战法 / 146 历史行 / 4 指数）

**留档**
- `_snapshots/src_20261114_140932.zip`（298 文件 / 1.67MB，已排除明文凭据）
- `data/_qa/quant_data.duckdb` + `.manifest.json`（来源=11-13 备份，可追溯）

## 九、红线自查

| 红线 | 是否守住 |
|---|---|
| 不改策略逻辑 / 评分权重 / 阈值 / 闸门 | ✅（H1 只记录不拦截；健康度只改展示判序；熔断隔离不改限流窗口本身） |
| 不动 `config.yaml` / `master_control.json` 数值 | ✅ 一个都没动 |
| 不删业务代码 | ✅（旧全局熔断键兼容读取；旧字段全保留） |
| DuckDB 单写者 | ✅（分析工具一律 `read_only=True`；写库测试走 `data/_qa` 副本） |
| 报告/日志无密钥明文 | ✅ |
| 不 commit/push | ✅（快照留档用 backup_snapshot） |

## 十、遗留与建议（非阻塞）

1. **周一 18:30 daemon 会自动重刷**两份报表产物，本轮用 QA 副本刷新的版本届时被自然覆盖——属预期行为。
2. **停机窗口建议跑一次完整体检**：`verify_system.py`（预期 80/0/0）+ `tools/system_health.py`（完整读库版，含新的账本一致性巡检）。
3. **H1 Shadow 攒样本**：`data/runtime/observability/sentiment_shadow.jsonl` 每轮扫描一行，攒 ≥20 个交易日后看「情绪系数方向 vs 后续信号质量」吻合率，再决定是否接线。
4. **真正的新发现请从三件套看起**：`strategy_correlation_matrix.py`（重复计票）、`event_study_car.py`（信号是否透支）、`bootstrap_return_dist.py`（结构性正/负）——它们已经把「哪条战法更好」从猜测变成可复算。
5. **仍待你拍板的策略决策**（本轮一律未动）：P1 市值门槛 120 亿 vs 涨停龙头的关系；push_history P1 口径是否对齐；`fact_limit_theme` 是否作为选股输入（当前只做看板）。

*报告结束 · 20261114 · 全文不含任何 token / api_key / webhook 明文。*
