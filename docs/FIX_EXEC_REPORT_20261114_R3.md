# 修复执行报告 R3 · 上线审计阻断项（2026-11-14）

> 起因：`docs/GO_LIVE_AUDIT_20261114.md`（同日 Go-Live 审计）的 P0/P1 阻断项修复。
> 红线遵守：**未改任何策略逻辑 / 评分权重 / 阈值 / 闸门 / `config.yaml` / `master_control.json`**；
> 未写生产库；测试全部离线/内存库/临时目录。

---

## 一、改动清单（按审计编号）

### P0-3 密钥明文进 `sniper.log`
| 文件 | 改动 |
|---|---|
| `core/log_config.py` | 新增 `scrub_secrets()` + `_SecretScrubFilter`：所有落盘/控制台日志在 Handler 层对 `key=…` / `token=…` / `password=…` 打码（幂等、失败不阻断日志）。挂到文件与 stderr 两个 Handler。 |
| `core/notification/gateway.py` | 新增 `_mask_webhook_url()`；两处发送路径的异常日志全部经打码输出。 |
| 历史日志 | `data/runtime/sniper.log` 中 82 处明文 key 已就地清理为 `key=***`（轮转旧档一并处理）。 |

### P0-2 网络单点（代理死亡 → 数据/推送/告警三停）
| 文件 | 改动 |
|---|---|
| `data/fetcher/_client.py` | 根因修复：`_no_proxy_env()` 原来只清 `HTTP(S)_PROXY` 环境变量，而 requests 会回退读 **Windows 注册表代理**（ProxyEnable=1 / 127.0.0.1:7897）→ 所谓"直连兜底"实际仍走死代理（11-14 日志的 ProxyError 即此）。现在同时设 `NO_PROXY='*'`/`no_proxy='*'` 并在退出时恢复。实测：注册表代理存在时 `get_environ_proxies()` 返回 `{}`。 |
| `core/notification/gateway.py` | 代理路径重试耗尽后，追加一次 `Session(trust_env=False)` **真直连**兜底（`_send_markdown_sync` / `_send_system_markdown_sync` 两路）。实测（本机代理仍死）：直连企微 http 200 成功。 |
| `core/notification/gateway.py` | 系统告警「代理+直连全挂」时落本地账本 `data/runtime/state/alert_undelivered.json`（上限 100 条，原子写）——**告警永不静默丢失**；`XJ_ALERT_LEDGER_PATH` 可覆盖（测试用）。 |
| `core/net_health.py`（新增） | 网络体检：读注册表代理配置 → 探测本地代理端口存活 → `trust_env=False` 探测 `api.waditu.com` / `qyapi.weixin.qq.com` 直连可达；结论 `ok / direct_only / degraded / down` 落 `state/net_health.json`，down 时 ERROR 日志。三档语义：直连通+代理死=direct_only（业务可跑但需修代理）。 |
| `tools/system_health.py` | 巡检新增「网络健康」段（读库前执行，daemon 占库分支同样覆盖）。本次实跑：`verdict=direct_only` WARN 如期出现。 |

### P1-4 双 daemon（审计误读 → 已复验关闭）
- 复验：`ParentProcessId(15640)=8316` —— venv 启动器 stub（8316）等待其子进程（15640，基础解释器）的正常父子形态；
- 互斥契约实测：并发启动第二实例 → **`EXIT_CODE=2`** 立即退出（"检测到已有 auto_sniper_daemon 进程在运行"）。
- 无代码改动；审计文档已更正。

### P1-7/8 模拟盘净值断链 + 停机结算丢失
| 文件 | 改动 |
|---|---|
| `core/paper_trading/engine.py` | 新增 `backfill_equity_gap()` / `_backfill_equity_gap_impl()`：上次净值日 → 今天之间的**真实交易日**（取 `daily_data`，不编造日期），按持仓 × 当日收盘（回溯 `<=`，缺失退买入价并标注）补写 `paper_equity_daily`，链式 PnL/回撤连续；**缺口内存在任何 BUY/SELL 成交则拒绝回填**（防编造现金口径）；幂等（无缺口零写入）；同步推进 `trading_day_index / last_equity_date`。 |
| `core/paper_trading/daemon_hooks.py` | `paper_settle_job`（15:05 结算前）与 `paper_init_job`（08:40 初始化后）两个入口挂接回填；回填异常绝不影响当日结算主流程。 |
- 根因确认：daemon 停机错过 15:05±90min catchup 窗口（11-10 直到 20:16 才有机作业记录）→ 当日净值行永久丢失。修复后在停机恢复的首个结算/初始化点自动补齐。

### P1-6 交易日历断网盲区
| 文件 | 改动 |
|---|---|
| `auto_sniper_daemon.py` | `is_today_trading_day_sse()` 内存缓存未命中后，先查 `dim_trade_calendar` 本地表（`data/db_core/_trade_dates.is_trade_date_local`，离线单行查询），命中即用；未收录再走在线 trade_cal。断网不再退化成"整天按非交易日跳过"（历史发生 34 次）。表本身已有同步链路（`sync_trade_calendar(years=3)`，随每日同步更新；当前 1111 行）。 |

### P1-5 daily 主接口退化（口径归一）
| 文件 | 改动 |
|---|---|
| `data/fetcher/_health_record.py` | 健康账本对「daily 失败但 `daily_by_date` 回退成功」归一为 `ok=true + fallback="daily_by_date"`，消除每日假告警疲劳；回退也失败时保持 `ok=false` 不洗白。Tushare 主接口恢复后此分支自然消失。 |

### 附加修复（审计过程中发现）
- `tools/_test_paper_trading.py`：补 `XJ_NO_PUSH=1` 默认旁路。本次审计实跑该套件时，因本机代理死亡触发新直连兜底，**真实发出了 3 条企微消息**（内容为测试文本）。加护后套件不再触网（运行时长 67s → 3.8s）。

---

## 二、验证结果

| 验证 | 结果 |
|---|---|
| `tools/_test_go_live_fixes.py`（新增，11 项） | **11/11 PASS**（脱敏 2 + 网络 4 + 回填 4 + 日历接线 1） |
| `tools/_test_paper_trading.py` | **17/17 PASS**（含 T10 资金恒等式） |
| `core/paper_trading/daemon_hooks.py` 自检 | OK |
| `core/go_live_gate.py` 自检 | OK（门槛/红牌/fail-closed 未受影响） |
| `core/net_health.py` 实跑 | `verdict=direct_only`：代理端口死、直连双域名可达（本机现状的真实刻画） |
| `system_health.py` 实跑 | 新增「网络健康 WARN」段如期输出 |
| `py_compile` 全部改动文件 | 通过；IDE lint 0 错误 |

## 三、生效与遗留

1. **生效条件**：daemon 当前运行旧代码（pid 15640），全部改动**下一次 daemon 重启后生效**；
   计划任务看门狗会在停止后 ~30s 自动拉起新代码。当前为周六非交易时段，重启零风险。
2. 本机代理（127.0.0.1:7897）仍处死亡状态：现在业务已能经直连自愈，但请恢复 Clash 类代理工具，
   让 `net_health` 回到 `ok`；否则每天都会收到一条 `direct_only` WARN 提示。
3. 策略层（真钱上线）判据不变：门禁 6 条未转绿（期望 -2.43%/笔、7 红牌），本次修复
   **不改变、也不应改变** 这一结论——工程修复让"观察模式"可靠，不构成真钱放行。

*报告结束 · 20261114 · Fix Exec R3*

---

# R5 补记（2026-11-14 18:40）：P2→P3→P4→P5 全链路演练 + P3 生产级 bug

### 1. 新工具 `tools/_e2e_p2_p5_oneshot.py`（一键四池全链路演练）
- 四池真实作业函数按序跑：`_run_scan_push_p2/p3/p4/p5`，各自锚定目标日历史时点（09:26/10:00/14:36/20:05）。
- 副作用三保险：`XJ_NO_PUSH / XJ_NO_SIGNAL_PERSIST / XJ_NO_DEEPSEEK` 环境闸 + `save_signal_log / _push_top_scores / _paper_on_signals_safe` 函数级计数桩（即使环境变量失效也不写库/不推送/不下模拟盘单）。
- 自动停/恢复 daemon（复用 controlled_verify 机制）。**恢复关键序**：先 `close_db()` 释放句柄再触发 bat——否则 bat 前置 verify 撞读句柄失败、秒退（实测根因，首次演练踩过）。
- 演练结果（20261113 回放，生产零写入）：P2 0/0（周末无实时行情，按预期）；**P3 2 只正式行（修复后）**；P4 0 正式/17 观察；P5 19 只（≥60 分），应落库 33 条（周五实盘为 28 条，差异来自周末回放拿不到盘中 rt 快照，属预期）。

### 2. 【P0】P3 正式行全灭 bug（演练首次发现，已修复）
- `core/strategies/strat_p3_intraday.py`：`_explain_once` 仅在 `if "detail" not in res:` 分支内赋值，且其调用引用了尚未定义的 `_explain_mainline/_explain_sector_rank` → 越过门槛的股票在 `detail["explain"]` 处必抛 `UnboundLocalError` → **P3 至今不能产出任何正式信号**（历史上判定「P3 无信号＝市场没机会」是误读）。
- 修复：删除错位调用；在 `_explain_mainline/_explain_sector_rank` 定义之后统一构建（try/except 降级空 dict）。修复后演练 P3 恢复产出（000001/000333），`[p3_snapshot]` 落库闸按点检模式正常短路。

### 3. HiThink 客户端补死代理快断（演练性能项）
- `data/hithink_client.py`：限流器重试循环加 `_error_suggests_dead_loopback_proxy` 早退——代理死亡时不再空烧 3 轮退避（~7-10s/调用），直接快速失败由上层默认值降级。
- 附注：HiThinkLimiter 熔断记录现带 900s 陈旧判定（既有机制），演练中正确"忽略陈旧熔断放行"。

### 4. 【2026-11-14 晚】HiThink 直连兜底 + 快速直连窗口（补齐最后一个 P0-2 缺口）
- 实测 `fuyao.aicubes.cn` 本机直连可达（HTTP 200）：系统代理死亡时 HiThink 不应全程降级
  （大盘锚恒 1.0 / 涨停池恒 0 会静默侵蚀 P1 龙头溢价与 P2/P3/P4 注入信号）。
- `_http_get` 增加 `_ht_get_with_fallback`：代理失联 → 直连兜底一次 → 成功后进入 600s
  快速直连窗口（与 data/fetcher/_client.py 同款语义，lazy import 防环依赖）。
- 注意：演练期间（兜底上线前）烧出的跨进程熔断记录（DuckDB `hithink_rate_state`，
  900s 陈旧阈值）会自行过期；兜底上线后首个请求即直连成功 → 失败计数不再累加，
  熔断不会再被死代理误触发。

### 5. 顺手的两个口径修正
- `verify_system.py` 横幅改为动态读取 `constants.APP_VERSION`（原写死 "Pro V26.88"，与实际 V32.0 脱节）。
- `_hithink_market_context.py` 默认降级文案改为「HiThink 快照不可用，使用默认中性值」
  （原「HiThink 未启用」在网络失败路径上误导排查）。

*报告结束 · 20261114 · Fix Exec R5 (E2E drill)*

---

# R6 补记（2026-11-15）：Ctrl+C 人工停止 → 永久免自动重启

### 背景
用户要求：窗口内按 Ctrl+C 关闭系统后**不许任何机制自动重启**（当天曾被看门狗连拉 3 次）。

### 实现（三处协同，标记文件 `data/runtime/state/user_manual_stop.json`）
1. `auto_sniper_daemon.py`：`_sniper_global_excepthook` 的 KeyboardInterrupt 分支新增 `_write_user_stop_flag()`（原子写 epoch/bj/pid/reason）。**只有 Ctrl+C 写标记**；taskkill/强杀/崩溃不写 → 崩溃自愈原行为不变。
2. `start_daemon_24x7.bat`：`--unattended`（看门狗/登录自启）启动时见标记 → 立即 `[SKIP]` 退出；循环内 daemon 退出后见标记 → `exit /b 0` 不再 30 秒重启；**人工双击**（无 --unattended）见标记 → 清除并继续（=明确的上线意愿）。
3. `tools/daemon_watchdog.py`：assess 增加 `user_stopped`；标记存在 → `skipped_user_stopped`，不告警、不拉起（计划任务保持启用但持续静默）。

### 验证（全部通过）
- 单元：`excepthook(KeyboardInterrupt)` → 标记写入（pid/reason 正确）——与真实 Ctrl+C 同一条路径；
- 集成1：daemon 运行中被中断杀停 → 循环见标记不重启（bat 与 daemon 均 0 残留，75s 观察窗双查）；
- 集成2：watchdog 见标记 → `skipped_user_stopped`、退出码 0、不推送不拉起；
- 集成3：`bat --unattended` 见标记 → `[SKIP]`、退出码 0、标记保留、不启动 daemon。
- 恢复：清标记 → AutoStart 拉起生产 daemon（pid 稳定、心跳正常、watchdog 判 healthy/starting）。

### 附注
- 本机 venv 的 `python.exe` 是 stub（`Popen` 外层 pid ≠ daemon 真实内层 pid，锁文件记录内层 pid）；
  任何外部自动化"按 Ctrl+C"必须按锁文件 pid 操作——外部发 CTRL_BREAK 只会硬杀（无 ATEXIT），
  无法模拟 KeyboardInterrupt，本次联调已踩实。
- 恢复上线方式：**双击 `start_daemon_24x7.bat`**（会清标记并恢复"崩溃自愈"）；看门狗/自启不恢复人工停止。

*报告结束 · 20261115 · Fix Exec R6 (Ctrl+C stop policy)*

---

# R4 补记（2026-11-14 17:00-17:30）：受控窗口复检 + 尾迹收口

### 1. 新增 `tools/controlled_verify.py`（受控窗口一键复检）
- 流程：禁用两个计划任务 → 结束 `start_daemon_24x7.bat` 循环与 daemon 进程树（精确 cmdline 匹配，绝不泛杀 python）→ 等 DuckDB 释放 → 跑 `verify_system.py`（输出落盘 `data/runtime/verify_reports/`）→ **finally 无条件恢复任务并重拉 daemon**。
- 安全：`--dry-run` 零变更；交易时段默认拒绝（`--force` 放行）；恢复失败打印手工三条命令。
- **实跑结果（17:06:22）**：全程 57s，**FAIL=0 / WARN=1**（"sentinel 陈旧"是停机窗口的预期噪音），任务恢复、daemon 心跳确认。
- 修正：`_daemon_alive` 曾因"120 秒内的旧日志"假阳性（bat 前置检查期间只有 cmd、python 未起）；现改为「必须存在 auto_sniper_daemon.py 进程 + 日志 mtime 晚于恢复时刻」，等待上限 420s。

### 2. 死代理「快速直连窗口」（17:1x 追加）
| 文件 | 改动 |
|---|---|
| `data/fetcher/_client.py` | 模块级 `_DIRECT_ONLY_SEC=600 / _DIRECT_ONLY_UNTIL`：直连兜底成功一次 → 开窗 600s，窗口内所有 `retry_api` 调用**直接走直连**（保留限流器记账），成功仅 DEBUG 级日志；直连也失败则关窗回常规流程。企微告警只在"开窗瞬间"发一次（防每接口重复）。 |
| `data/fetcher/_client.py` | `_dead_loopback_proxy_now()`：端口级探测（0.35s TCP），首次网络失败即确认代理死亡、跳过剩余重试直跳兜底（省 ~50s/调用）。 |
| `data/tushare_rate_limiter.py` | 同一快断逻辑挂到限流器退避前（lazy import，避免环依赖）——限流器不再空烧 3 次重试。 |
- **实测**：同进程连续 2 次 trade_cal——第 1 次 3.2s（含开窗）、第 2 次 **0.45s、零告警**；daemon 重启后日志 17:19:18 起 trade_cal 噪音完全消失（窗口内 DEBUG 不落 INFO）。
- 效果：休市手环每 ~2 分钟的 trade_cal 探测从「每次 4-5 行 WARNING + ~0.5-10s」变为「每 10 分钟一次静默重探」。

### 3. 受控复检中发现的回归并修复
- `core/network_health.py` 文件丢失 → 已重建；重建时发现探测器 bug：`trust_env` 是 `requests.Session` 属性而非 `get()` 关键字 → 之前恒返回 offline 假阴性。改为 `Session(trust_env=False)` 后实测：`OK / direct_only，waditu=true 企微=true`，快照已落盘。

### 4. 最终状态（17:2x）
- daemon 单链：`start_daemon_24x7.bat` → venv 启动器 → daemon（lock pid=11852, 17:19:13），全部修复已在运行时生效。
- 17:12:21 bat 前置体检（daemon 停机窗口）：**80 通过 / 0 失败 / 1 警告**（唯一 WARN = 旧 sentinel，随后 orchestrator 自动清理 age=262s）——上线以来首次 0 失败完整体检。
- 遗留提示：① 系统代理仍指向已死的 127.0.0.1:7897，建议关闭以免每 10 分钟一次"重探"告警；② verify 横幅仍显示 "Pro V26.88" 而 APP_VERSION=V32.0（纯展示口径差异，未动）；③ 周一开盘观察点不变，另加：08:40 init 后的模拟盘净值缺口回填日志、网络健康探测（09:20）结论。
