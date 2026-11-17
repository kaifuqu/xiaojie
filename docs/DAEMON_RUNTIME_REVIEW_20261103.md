# Daemon 启动实测复核（2026-11-03 19:12 起跑）

> 复核方式：读 `sniper.log` 实时日志 + 读代码路径 + 实测网络 + DuckDB 锁行为验证
> 起始：`verify_system.py` 80/0/0 → daemon 19:12:40 启动

---

## 〇、一句话结论

> **daemon 起来了，体检 80/0/0 全绿，模拟盘建表成功，行情已补到 11-02，PnL 回填从 308 → 459 条。**
> **但我发现了 1 个必须今晚修的 Bug：`信号T+N回填` 任务陷入无限重跑循环（每 ~2 分钟一次，已经跑了 9 次），到 20:30 会自动停下。**
> **另外 p2/p4 依然 0 行 —— B1/B2 修复仍未被任何真实交易日验证。**

---

## 一、✅ 已经变好的（实跑确认）

| 项 | 启动前（16:15） | **现在（19:32）** |
|---|---|---|
| `verify_system.py` | 80 / 0 / 0 | ✅ **80 / 0 / 0** |
| daemon 进程 | 无 | ✅ **PID 23104（217 MB）** |
| 模拟盘 | 0 持仓 0 订单 | ✅ **账户就绪 cash=1000000.00 status=RUNNING day=1** |
| `signal_pnl` | 308 行 | ✅ **459 行**（+151） |
| `strategy_effectiveness` | 69 行 | ✅ **18 条战法胜率记录**（最新批次） |
| `signal_strategy_ledger` | 367 行 | ✅ **回填 pushed=TRUE 95 行** |
| UI 快照 | 21:06 | ✅ **19:23 已重新生成**（战法 18 / 历史 87 / 指数 4） |

**启动日志全绿**：
```
[DuckDB 主库绝对路径] D:\xiaojie\data\quant_data.duckdb
[时钟自检] 北京时间=2026-11-03 19:12:40 CST | |Δepoch|=0.000s
[模拟盘] 账户就绪 cash=1000000.00 status=RUNNING day=1
[scan_async] 已占用 pending 队列互斥锁（与 UI 内嵌 worker 互斥）
[DB 维护调度器] 已启动（凌晨 03:00 全量 VACUUM）
[HiThink 盘中轮询器] 已启动（09:30,10:30,13:30,14:30）
```

**DuckDB 单写者锁工作正常**（我实测撞了一次）：
```
_duckdb.IOException: IO Error: Cannot open file "...quant_data.duckdb":
另一个程序正在使用此文件。
File is already open in ... python.exe (PID 23104)
```
> **这是正确行为** —— daemon 独占写库，外部只读连接被拒。说明写连接单例是健康的。

---

## 二、🔴 必须今晚修：信号 T+N 回填陷入无限重跑

### 2.1 实测现象

```
backfill 运行时间点：
19:14:37 → 19:16:50 → 19:18:23 → 19:20:42 → 19:23:07
        → 19:25:53 → 19:27:58 → 19:29:55 → 19:31:58
（共 9 次，每 ~2 分钟一次）
```
每次都写 **459 条收益记录**（内容完全相同），并触发一整条下游链：
```
[backfill_pnl] 写入 459 条
[strategy_effectiveness] 写入 18 条
[optimize] 战法权重推荐 18 条
[snapshot] UI 快照已生成
[ledger_bridge] 回填 pushed=TRUE 95 行
```

### 2.2 根因（代码级确认）

**两个缺陷叠加**：

**缺陷 A —— 缺"已完成"判定**
`auto_sniper_daemon.py:3568-3579`
```python
def _bj_daily_job_completed(label: str, today: str) -> bool:
    state_key_by_label = {
        "晚间增量同步": "last_sync_ok_bj_date",
        "晚间P1":     "last_p1_ok_bj_date",
        "晚间P5":     "last_p5_ok_bj_date",
        "晚间安全压缩": "last_daily_vacuum_bj_date",
    }
    state_key = state_key_by_label.get(label)
    if not state_key:
        return False          # ← "信号T+N回填" 落这里，永远 False
    return str(_read_pipeline_state().get(state_key, "") or "") == today
```
而回填任务**确实写了状态**（`:3877`）：
```python
_write_pipeline_state_patch(
    last_signal_backfill_bj=_now_bj().isoformat(),     # ← 键名对不上
    last_signal_backfill_result=str(result)[:200],
)
```
> **写的是 `last_signal_backfill_bj`（ISO 时间戳），判定查的是 `last_signal_backfill_bj_date`（日期）。**
> **键名不匹配 → 状态写了也读不到 → 永远判定为"未完成" → 每分钟重新派发。**

**缺陷 B —— 进程内去重键含分钟戳**
`auto_sniper_daemon.py:3601`
```python
dispatch_key = f"{minute_key}:{hhmm}:{label}"   # minute_key = "%Y%m%d%H%M"
```
**每分钟都不同** → `_BJ_DISPATCHED_JOB_KEYS` 也拦不住。

### 2.3 影响

| 维度 | 影响 |
|---|---|
| **持续时间** | catchup 窗口 = 120 分钟，18:30+120 = **20:30 自动停** |
| **还会跑** | 现在 19:32 → **还会跑约 58 次**（到 20:30） |
| **数据正确性** | ✅ **无害** —— `signal_pnl` 用 `DELETE WHERE (log_id,n_days) IN (...)` + INSERT，**幂等**；`signal_strategy_ledger` 用 `NOT EXISTS` 去重，**幂等** |
| **磁盘/DB** | ⚠️ 每次全量重写 459 行 pnl + 18 行 se，**DuckDB 写放大**（约 60 次 × 全量） |
| **性能** | 🔴 **每次占用 `_SCAN_BUSY` 全局串行锁 ~40-90 秒** → **P3 扫描被完全饿死** |
| **日志污染** | 每次 15 行 INFO，19:12→20:30 预计刷 **~900 行** |

### 2.4 🔴 最严重的连带影响：P3 扫描被饿死

```
19:12:50 [P3] 未获取全局串行锁 _SCAN_BUSY（None），跳过本次
19:15:00 [P3] 未获取全局串行锁 _SCAN_BUSY（None），跳过本次
19:17:30 [P3] 未获取全局串行锁 _SCAN_BUSY（None），跳过本次
19:22:30 [P3] 未获取全局串行锁 _SCAN_BUSY（None），跳过本次
19:25:02 [P3] 未获取全局串行锁 _SCAN_BUSY（None），跳过本次
19:27:30 [P3] 未获取全局串行锁 _SCAN_BUSY（None），跳过本次
```
> **`wait_for_lock_sec=None` → 非阻塞获取 → 拿不到就立刻跳过。**
> **回填每 2 分钟霸占锁 40-90 秒，P3 每 150 秒拍一次 —— 几乎必然撞上。**
> **这解释了为什么 p3 依然只有 11 条（最后一条是 10-13）。**

### 2.5 修复方案（两处，最小改动）

**方案：给 `信号T+N回填` 加持久完成标记 + 修键名**
```python
# auto_sniper_daemon.py:3570  state_key_by_label 增加一行
state_key_by_label = {
    "晚间增量同步":   "last_sync_ok_bj_date",
    "晚间P1":        "last_p1_ok_bj_date",
    "晚间P5":        "last_p5_ok_bj_date",
    "晚间安全压缩":   "last_daily_vacuum_bj_date",
    "信号T+N回填":    "last_signal_backfill_bj_date",   # ← 新增
}

# auto_sniper_daemon.py:3877  写盘时同时写日期键
_write_pipeline_state_patch(
    last_signal_backfill_bj=_now_bj().isoformat(),
    last_signal_backfill_bj_date=today,          # ← 新增（today = YYYYMMDD）
    last_signal_backfill_result=str(result)[:200],
)
```
> **注意日期格式**：`last_sync_ok_bj_date` 等用的是 `today`（`YYYYMMDD` 8 位无横杠），不是 ISO。**必须一致**，否则 `== today` 永远 False。
> **这一处改完，重跑循环立刻消失。**

**可选加固（防同类问题复发）**：把 `dispatch_key` 改成不含分钟戳，或给"每次只需跑一次"的任务加独立的进程内 `_BJ_ONCE_PER_DAY` 集合。

---

## 三、🟠 Tushare 网络间歇性失败（12 次）

```
[TushareLimiter] NoneType 调用失败 (attempt 1/3): ConnectionResetError(10054, 远程主机强迫关闭)
[TushareLimiter] NoneType 调用失败 (attempt 2/3): NameResolutionError(api.waditu.com)
[TushareLimiter] NoneType 调用失败 (attempt 3/3): NameResolutionError(api.waditu.com)
```
**我实测网络**：
```
api.waditu.com  → HTTP 200 ✅（DNS 解析正常，IP 8.140.225.26）
push.hithink.com → 未能解析此远程名称 ❌
deepseek.com     → HTTP 401（说明通，只是没带 key）✅
```
**判定**：
- ✅ **Tushare 本身是通的**（我刚测 200）—— 那是**间歇性 DNS/连接抖动**，非配置错误
- 🔴 **但 `push.hithink.com` DNS 解析失败** —— 这正是 Tushare 报 `NameResolutionError` 的原因
- **Tushare 的 `trade_cal` 请求走的正是这个域名** → 日历查询失败 → 触发 `_daemon_is_trading_day_safe()` 的熔断分支

**好消息**：熔断路径有保守兜底（`:1088`）
```python
# 日历 API N 次重试后全部失败且无当日本地缓存 → 按交易日处理
return True   # 【V26.88 安全兜底】保守按交易日，任务仍执行
```
> **所以不会因为日历挂掉而静默假死。** 这个设计是对的。
> **但**：`[LOOP_GAP] 主循环中断 40.8s 后恢复` 说明网络重试确实阻塞了主循环 40 秒。

**建议**：
1. **确认 `push.hithink.com` 是否是正确的 HiThink 域名**（可能已变更）——查 `data/hithink_client.py` 里的 base_url
2. 给 Tushare 调换用 **HTTPS**（当前日志显示 `port=80` 明文 HTTP）

---

## 四、🔴 p2 / p4 依然 0 行 —— B1/B2 修复仍未被验证

```
signal_log:  p3=11 (10-09 ~ 10-13)   p5=132 (10-08 ~ 11-02)
              p1=0    p2=0    p4=0
```
**daemon 19:12 启动时已过 09:26（P2 竞价）和 14:31-14:51（P4 尾盘）**，所以**今晚不会有 P2/P4 的补跑机会**。

**p3 只有 11 条（10-09~10-13）的原因现在很清楚了**：
```
19:12~19:32 这 20 分钟里，P3 每 150 秒拍一次，
每次都撞上回填任务霸占的 _SCAN_BUSY，全被跳过。
```
> **这是历史遗留（P3 停摆 3 天）+ 今晚新 Bug（锁被霸占）双重叠加。**

### 判定方法（明天 09:30 后看）
```
① 09:26 P2 竞价跑完后 → 查 signal_log 有无 pool='p2' 行
② 14:31~14:51 P4 尾盘跑完后 → 查 signal_log 有无 pool='p4' 行
③ p3 是否开始新增（现在完全没有新行）
```
**连续 3 个交易日 p2/p4 都 0 行 → B1/B2 修复无效，要再查。**

---

## 五、⚠️ 其它观察

### 5.1 `账本对账: 严格匹配 0 / 跨池兜底 95 / 未匹配 48`
**这是已知的、有注释解释的设计**（`core/signal_ledger_bridge.py:283-291`）：
> `push_history` 里记的 pool 主要是 `"p1"`（P1 底仓重建写入），
> 而 `signal_log` 里记的是 `"p3"/"p5"`（信号所属池）。
> 按 (date, pool, code) 严格匹配 → 全部匹配不上 → 跨池兜底才生效。

**不是新 bug**，但**说明 `push_history.json` 的 pool 口径和 `signal_log` 的 pool 口径是错位的** —— 这个口径错位早晚会让"战法有效性"统计失真。**建议后续统一。**

### 5.2 `未匹配 48` 条
48 条 push_history 记录既没严格匹配上、也没跨池兜底上 —— **这些是"推过但账本里查无此人"的孤儿**。
结合上一轮我报的 `ledger(302) > signal_log(120)` 口径错位，**这两件事很可能是同一个根因**。

### 5.3 `距自动应用还差 80 条`
`[optimize] mode=statistics_only, 推荐=18 条, 距自动应用还差 80 条`
**战法权重自动应用需要 98 条样本**，现在 18 条。**这是正常的安全闸，不是故障。**

---

## 六、📌 今晚的 To-Do（按优先级）

```
【P0 · 必做，影响明天的数据】
□ 修 2.5 的两处（键名不匹配）→ 停止回填无限重跑
  → 同时解除 P3 扫描被饿死
□ 修完重启 daemon（或等 20:30 自动停，但那样 P3 今晚也是废的）

【P1 · 明早开盘前必须确认】
□ 查 push.hithink.com 域名是否正确（DNS 不通）
□ 明天 09:26 后查 signal_log 有无 pool='p2'
□ 明天 14:31 后查 signal_log 有无 pool='p4'
□ 明天盘中看 p3 是否开始新增

【P2 · 有空再修】
□ push_history.json 的 pool 口径统一到 signal_log 口径
□ Tushare 改用 HTTPS（当前 port=80 明文）
□ dispatch_key 去重加固（防同类问题复发）
```

---

## 七、最后一句

> **daemon 是真的起来了，而且起得很干净 —— 体检全绿、模拟盘建表、行情补到 11-02、PnL 从 308 涨到 459、UI 快照重新生成。这比 16:15 那会儿强太多了。**
>
> **但有一个 Bug 必须说清楚：`信号T+N回填` 的"完成标记"键名写错了（写 `last_signal_backfill_bj`，查 `last_signal_backfill_bj_date`），导致它每 2 分钟重跑一次，每次霸占全局锁 40-90 秒，把 P3 扫描完全饿死。**
>
> **这是今晚唯一值得动手改的代码 —— 两行，改完 P3 就能正常跑。**
>
> **至于 p2/p4：今晚没有机会验证了（09:26 和 14:31 都过了），明天才是真正的验货日。**

---

*本报告基于 2026-11-03 19:12~19:32 的 daemon 实时日志（sniper.log）、代码路径确认（auto_sniper_daemon.py:3568/3594/3601/3877/3958、data/signal_backtest.py:696、core/signal_ledger_bridge.py:283）、实测网络连通性、DuckDB 锁冲突实测生成。*
*不含任何凭据明文。*
