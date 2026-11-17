# 新机器 24 小时运行部署手册

> 适用版本：小杰AI选股系统 Pro **V26.88 + V30.1**
> 目标机器：另一台 Windows 电脑，长期 7x24 运行选股守护进程 + 企微推送
> 本手册按实际验证过的路径写，不是通用教程。

---

## 零、先看这张表（全部硬约束）

| 项 | 值 | 为什么不能改 |
|---|---|---|
| Python | **3.12.7**（3.12.x 均可，**不要 3.13/3.14**） | pandas 3.0.6 需 >=3.11；3.13+ 部分 C 扩展无预编译 wheel，会触发源码编译 |
| 依赖安装源 | **`requirements.lock`** | `requirements.txt` 是下限版，pip 会拉到未验证的组合 |
| 隔离环境 | `_pyenv\venv` | 全局 Python 会被别的项目污染 |
| 数据库 | `data\quant_data.duckdb` 单文件 | DuckDB 单写者锁，不能两个进程同开 |
| 互斥 | daemon 与 UI **二选一** | 同上，见下文第 4 节 |
| 时区 | **Asia/Shanghai** | 全部时序铁律依赖北京时间 |
| 首次数据 | 必须联网跑一次 19:45 增量 | 空库上不了线 |

---

## 一、迁移前：本机打包

### 1.1 必须带走的东西

```
D:\xiaojie\                     <- 整个项目目录（含 config.yaml）
  |- config.yaml                <- 【含 API Key，物理拷贝，不要贴到聊天里】
  |- requirements.lock          <- 新增：精确版本清单
  |- deploy_new_machine.bat     <- 新增：一键部署脚本
  |- auto_sniper_daemon.py
  |- data\
  |   |- quant_data.duckdb     <- 173 MB，历史行情（可迁可不迁，见 1.3）
  |   |- runtime\               <- 运行态（建议不迁，让新机重建）
  `- ...
```

### 1.2 建议先跑一次体检，确认源机是干净状态

```powershell
cd D:\xiaojie
_pyenv\venv\Scripts\python.exe verify_system.py
```

期望：`0 失败 / 1 警告`（那个 WARN 一定是盘中日线滞后，属正常）。

### 1.3 数据库要不要一起搬？—— 建议**搬**

| 做法 | 优点 | 缺点 |
|---|---|---|
| **搬 `quant_data.duckdb`（推荐）** | 新机立刻有 136 个交易日历史，策略/回测/胜率面板立刻能用 | 173 MB 拷贝 |
| 不搬，从零同步 | 干净 | 首次同步要拉 130+ 交易日，耗时长且消耗 Tushare 积分额度（你的限速是 400 次/分钟、250 次/分钟，实际拉满要几十分钟） |

**搬的时候务必先停 daemon**：

```powershell
# 在跑 daemon 的窗口按 Ctrl+C，然后确认
Get-Content D:\xiaojie\data\runtime\state\daemon.sentinel
# 报错说明文件不存在 = 已正常退出
```

> `data\runtime\` 整个目录**建议不迁**：里面是缓存、锁文件、上一台机器的 PID。新机让脚本重建更干净。

### 1.4 拷贝时的坑（Windows 特有）

- **不要用微信/QQ 直接传文件夹**，大文件容易断、容易丢。
- 用 **7-Zip 打包**：`7z a -tzip xiaojie_migrate.zip D:\xiaojie\*`，最稳。
- 拷贝完**抽查文件数**：`Get-ChildItem D:\xiaojie -Recurse -File | Measure-Object`
- 别忘 `config.yaml`，这是最容易漏、又最关键的。

### 1.5 一份可直接用的打包命令（含必须排除项）

```powershell
# 1) 先停 daemon：在跑 daemon 的窗口按 Ctrl+C

# 2) 打包（-x 排除本地垃圾与可再生目录）
cd D:\
7z a -tzip xiaojie_migrate.zip .\xiaojie\* -xr!xiaojie\_pyenv\logs `
    -xr!xiaojie\data\runtime\scan_async -x!*.pyc -x!*.log

# 3) 解包到新机同一个绝对路径 D:\xiaojie
#    （7z 会自动建目录；保持 _pyenv 在里面，方案 A 就能用）
```

**为什么必须同一绝对路径**：`_pyenv\venv\pyvenv.cfg` 里写死了
`home = D:\xiaojie\_pyenv\py312`。放到别的盘符/目录，venv 会直接失效，
`python.exe` 报 `Permission denied` 或找不到 base interpreter。

**要排除的**：

| 排除项 | 理由 |
|---|---|
| `_pyenv\logs\` | 本机调试日志，新机用不上 |
| `data\runtime\scan_async\` | 异步任务队列，含 PID 和本机状态 |
| `*.pyc` / `__pycache__` | 可再生，还会带上旧绝对路径 |
| `*.log` | 同上 |

**不要排除的**：

| 必须带 | 理由 |
|---|---|
| `config.yaml` | 4 项凭据都在里面 |
| `_pyenv\py312\` | 方案 A 的便携解释器（省掉装 Python） |
| `_pyenv\venv\` | 已装好依赖的 venv（省掉 pip 下载，省几分钟） |
| `data\quant_data.duckdb` | 173 MB 历史行情 |
---

## 二、新机器：前置准备

### 2.1 系统要求

| 项 | 最低 | 建议 |
|---|---|---|
| 操作系统 | Windows 10 21H2 | Windows 11（**务必关闭自动休眠和睡眠**） |
| 内存 | 8 GB | **32 GB**（全市场 5300 只扫描 + DuckDB 173 MB 库很吃内存） |
| 磁盘 | 40 GB 空闲 | 80 GB（DuckDB 会随时间涨，WAL 未合并时翻倍） |
| 网络 | 稳定宽带 | 有线更稳（要连 Tushare + 企微 + DeepSeek + HiThink） |

**关掉睡眠和休眠（必做，否则白天休眠时守护进程直接死）**：

```powershell
powercfg /change standby-timeout-ac 0     # 接通电源不睡眠
powercfg /change hibernate-timeout-ac 0   # 接通电源不休眠
powercfg /change monitor-timeout-ac 60    # 屏幕可以关
```

### 2.2 Python 3.12.7（这台机器有个特殊情况）

参考机（D:\xiaojie）**没有装系统 Python**，用的是随项目携带的便携版解释器
`_pyenv\py312\python.exe`（官方 embeddable 发行版）。它的 `py` 启动器会回答
`No installed Pythons found!`，这是正常现象，不是坏了。

所以有两条路径，**优先选 A**：

| 方案 | 做法 | 适用 |
|---|---|---|
| **A（推荐）** | 拷贝时把 `_pyenv` 目录一起带上，放到新机**同一个绝对路径** `D:\xiaojie` | 最省事，什么都不用装 |
| B | 正常安装 Python 3.12.7 | 新机没带 `_pyenv` 时用 |

**方案 B 的步骤**：

1. 下载 <https://www.python.org/downloads/windows/> -> **Python 3.12.7**
2. 安装时**务必勾选 `Add python.exe to PATH`**
3. 装完开新 cmd 验证：

```powershell
py -3.12 -V
# 应输出 Python 3.12.7
```

### 2.3 关于 Win11 Smart App Control（pyarrow 被拦）

本机实测：`pyarrow._compute.pyd` 被 WDAC 拒绝加载，报 `WinError 4551`，会导致 pandas 整体崩。

**项目已内置解法**：`core/_pyarrow_compat.py` 的 `disable_pyarrow()` 会把 pandas 顶到 NumPy backend。部署脚本里已设：

```
set "XIAOJIE_DISABLE_PYARROW=1"
```

**如果新机同样被拦**，两个办法：

- 保持 shim 生效（本项目默认，不需要你做任何事）
- 或彻底根治：关闭 Smart App Control（`设置 -> 隐私和安全性 -> Windows 安全 -> 设备安全性 -> 设备保护`）——**但不建议**，会降低整机安全性

---

## 三、一键部署（推荐路径）

把整个项目目录（含 `_pyenv`，除非你不打算用方案 A）放到新机，例如 `D:\xiaojie`，然后：

```powershell
cd D:\xiaojie
deploy_new_machine.bat
```

脚本会依次做完：

| 步 | 动作 | 失败时 |
|---|---|---|
| 1/5 | 找 Python 3.12.x | 明确报错告诉你去官网装 |
| 2/5 | 建 `_pyenv\venv` 隔离环境 | 提示删掉 `_pyenv` 重来 |
| 3/5 | 按 `requirements.lock` 装精确版本 | 提示检查网络/代理 |
| 4/5 | 建 `data\runtime\*` 目录骨架 | 降级告警，不阻塞 |
| 5/5 | 跑 `verify_system.py` | **0 失败才算装好** |

### 手动等价步骤（脚本跑不了时用）

```powershell
cd D:\xiaojie
py -3.12 -m venv _pyenv\venv
_pyenv\venv\Scripts\python.exe -m pip install -U pip
_pyenv\venv\Scripts\python.exe -m pip install -r requirements.lock
_pyenv\venv\Scripts\python.exe verify_system.py
```

---

## 四、启动：两种模式二选一（**这是最容易出错的地方**）

DuckDB 是**单文件单写者**。同一台机器上：

| 模式 | 启动命令 | 用途 |
|---|---|---|
| **全自动 7x24** | `start_daemon_24x7.bat` | 实盘常驻，唯一正确选项 |
| 手动调试 | `start_server.bat` | 策略实验室 / 手动同步 / 修库 |

**这两个绝对不能同时开。** 互相检测由 `core/process_orchestrator.py` 的 sentinel 文件锁实现，检测到对方在跑会直接拒绝启动并给出提示。

### 4.1 实盘模式（你要的 24 小时）

```powershell
cd D:\xiaojie
start_daemon_24x7.bat
```

这个脚本做了：

1. 先跑 `verify_system.py`，有 FAIL 直接拦下不启动
2. 检查 UI 是否在跑，在跑就拒绝（除非传 `--force`）
3. 进死循环拉起 `auto_sniper_daemon.py`，进程死了 30 秒后自动重启

### 4.2 真正的「开机自启」（推荐做法）

别用「双击 bat 窗口」，窗口一关守护就停。用**任务计划程序**：

1. `Win + R` -> `taskschd.msc`
2. 「创建任务」-> **触发器**：
   - 触发器 1：登录时
   - 触发器 2：计算机启动时（勾「如果任务失败，每 1 分钟重启」，最大尝试 999 次）
3. **操作**：新建 -> 程序 `D:\xiaojie\start_daemon_24x7.bat`
   - 起始于：`D:\xiaojie`
4. **条件**页签：取消勾选「只有在计算机使用交流电源时才启动」
5. **设置**页签：取消勾选「如果任务运行超过 3 天就停止」

### 4.3 关掉窗口但不中断

如果一定要用窗口：窗口标题栏左下角对着「选择」-> 选「最小化」，别点 X。

---

## 五、验证它在真的跑

### 5.1 部署后第一件事

```powershell
cd Dxiaojie
_pyenv\venv\Scripts\python.exe verify_system.py
```

| 结果 | 含义 |
|---|---|
| `0 失败 / 1 警告` | 可以上线 |
| `BLOCKED` | 按 `[FAIL]` 行逐条修，修完再跑 |

### 5.2 每天看一眼的三件事

```powershell
# 1) 守护进程在不在
Get-Content D:\xiaojie\data\runtime\state\daemon.sentinel

# 2) 今天流水线跑到哪了
Get-Content D:\xiaojie\data\runtime\state\sniper_pipeline_state.json

# 3) 日志尾部（应该看到 8:50 早盘 / 19:45 同步 / 20:05 P5）
Get-Content D:\xiaojie\data\runtime\sniper.log -Tail 40
```

`sniper_pipeline_state.json` 里正常的字段：

```json
{
  "last_sync_ok_bj_date": "20261015",
  "last_p1_ok_bj_date":   "20261015",
  "last_p5_ok_bj_date":   "20261015",
  "last_p5_selected_count": 8
}
```

### 5.3 信号真的落库了吗（V30.1 新增门禁）

```powershell
_pyenv\venv\Scripts\python.exe -c "import sys; sys.path.insert(0,r'D:\xiaojie'); from core import _pyarrow_compat; _pyarrow_compat.disable_pyarrow(); from data.db_core import get_read_conn; c=get_read_conn(read_only=True); print(c.execute('SELECT CAST(trade_date AS VARCHAR) d, LOWER(pool) p, COUNT(*) FROM signal_log GROUP BY 1,2 ORDER BY 1 DESC LIMIT 5').fetchall())"
```

有输出且是今天日期 -> 正常。

**如果扫描说选出票但这里查不到**，就是 V30.1 修的那个老 bug 复现了，立刻查 `sniper.log` 里 `[signal_log]` 开头的 WARNING。

---

## 六、日常运维

### 6.1 时序铁律（daemon 每天自动跑）

| 时间 | 任务 | 失败后果 |
|---|---|---|
| 03:30 | 全量数据库维护（VACUUM） | 磁盘膨胀 |
| 06:30 / 12:00 | 推送防刷字典清空 | 防慢性中毒 |
| 08:50 | 早盘单一编排入口 | 早盘简报不发 |
| 09:18 | 早盘合并简报推送 | 同上 |
| 09:25 | P2 竞价扫描 | 错过竞价机会 |
| 盘中 | P3 盘中扫描（六时点快照） | 错过盘中机会 |
| 14:31-14:51 | P4 盘尾五拍 | 错过尾盘机会 |
| 19:45 | 日线增量同步 | **全系统数据停更** |
| 19:55 | P1 底仓重建 | P2-P5 无底可用 |
| 20:05 | P5 盘后扫描 | 盘后候选丢失 |
| 20:30 | DuckDB 安全压缩 | 磁盘翻倍 |

### 6.2 磁盘监控

```powershell
Get-ChildItem D:\xiaojie\data\*.duckdb* | Select-Object Name,@{n='MB';e={[math]::Round($_.Length/1MB,1)}}
```

正常：`quant_data.duckdb` 173 MB 左右 + 无 `.wal` 或 `.wal` 很小。
异常：出现 `.duckdb.wal` 超过 500 MB -> 说明有进程异常退出留下 WAL。

### 6.3 手动跑一次维护

```powershell
# 先停 daemon（窗口里 Ctrl+C），然后：
weekly_maintenance.bat
# 完事再 start_daemon_24x7.bat
```

### 6.4 配置改动位置

| 想改什么 | 改哪个文件 |
|---|---|
| API Key / Webhook / 阈值 | `config.yaml` |
| 推送开关 / 自动巡航 / 维护模式 | `data\runtime\state\master_control.json` |
| daemon 频率档（老机器降频） | `config.yaml` -> `server.daemon_profile: low` |

**改完配置不需要重启 daemon 也要紧**——多数配置每次扫描时实时读。但推送开关和巡航开关建议改完手动重启一次 daemon 更稳。

---

## 七、故障排查速查表

| 现象 | 根因 | 处理 |
|---|---|---|
| `start_daemon_24x7.bat` 立刻退出 | `verify_system.py` 有 FAIL | 看 `[FAIL]` 行逐条修 |
| 提示 `UI session is still active` | Streamlit 还在跑 | 关掉 UI 窗口；真死锁了用 `--force` |
| 日志 `No module named 'duckdb'` | 用错解释器 | 确认用 `_pyenv\venv\Scripts\python.exe` |
| 日志 `[signal_log] 写入失败` | 磁盘满或 WAL 损坏 | 查磁盘；`weekly_maintenance.bat` |
| 企微没消息 | 总控开关关了 | 查 `master_control.json` 的 `wechat_push_enabled` |
| 企微没消息但开关是开的 | webhook 填错 / 防刷命中 | 看 `data\runtime\state\alerts_dedup.json` |
| DuckDB 报 `different configuration` | 你的自定义脚本直连了 `read_only=True` | 改用 `data.db_core.get_read_conn()` |
| daemon 半夜自己停了 | 机器睡眠了 | 见 2.1 的 `powercfg` 命令 |

---

## 八、上线前最后确认清单

- [ ] Python `py -3.12 -V` -> 3.12.x
- [ ] `deploy_new_machine.bat` 第 5 步 -> `0 失败`
- [ ] 休眠/睡眠已关闭（`powercfg` 三个命令都执行过）
- [ ] `data\quant_data.duckdb` 已拷贝且能读
- [ ] `config.yaml` 里 4 项凭据已配置（token / hithink / wechat 主+副 / deepseek）
- [ ] 任务计划程序已建「开机 + 登录」双触发器
- [ ] 首日盯到 19:45 同步和 20:05 P5 真的跑了
- [ ] 首日验证 `signal_log` 当天有 P5 记录

---

## 附：本次修复摘要（为什么新机必须用这个版本）

本机在 V30.1 修复了一个**静默数据丢失**：P5 扫描选出 8 只票，但 `signal_log` 一条都没落库。

根因：DuckDB 1.5.x 要求同库所有连接配置一致，`read_only=True` 与读写连接被视为两种配置，进程内混用必抛异常；而异常被 `logging.debug` 吞掉，扫描仍被判成功。

修复：`data/db_core/_connection.py` 统一为 `duckdb.connect(path, config={})`，读取路径仍只 SELECT；`save_signal_log` 失败提升为 WARNING；`verify_system.py` 新增 **P0-14 门禁**——「扫描成功但信号未落库」直接判 BLOCKED。

**这就是为什么新机必须装 `requirements.lock` 而不是 `requirements.txt`**：低版本 DuckDB 会重演这个 bug，而且它是静默的，等你发现时可能已经丢了几周的信号。

