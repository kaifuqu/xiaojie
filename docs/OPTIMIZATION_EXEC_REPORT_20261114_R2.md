# 第四轮优化执行报告 R2（2026-11-14 下午）

> 依据：`docs/OPTIMIZATION_EXEC_REPORT_20261114.md` §十「遗留与建议（非阻塞）」中可编码落地的部分。
> 立场：**只做「让后续评估/体检不再依赖手工」的只读配套**；策略决策项一律未动。
> 红线：不改策略逻辑 / 权重 / 阈值 / 闸门；不动 `config.yaml`；不写生产库与生产 JSONL。

---

## 〇、一句话结论

**H1 Shadow 的「评估闭环」提前建好（评估器 + 只读看板 + 冒烟测试），停机窗口有了「一键完整体检」脚本；过程中发现的本机 bat 编码与 `py` 启动器两个坑已定位根因，并把存量 bat 统一修整完毕（§六）。**

## 一、本轮新增（4 个文件，均新增，零业务代码修改；存量 bat 工程修整见 §六）

| # | 文件 | 作用 |
|---|---|---|
| 1 | `tools/sentiment_shadow_eval.py` | **H1 Shadow 评估器（只读）**：情绪侧读 `sentiment_shadow.jsonl`（同日多条取最新、自动读 `.1` 轮转档）；信号侧 `signal_log ⋈ signal_pnl`（T+1, n_days=1）按日×池聚合（win / return_pct / COALESCE(excess_blend_pct, excess_pct)）；输出分状态表 + **吻合度三口径**（逐日 Pearson r、看多日 vs 看空日质量差、命中率）+ 分池检查；样本 <20 个交易日打「仅预览，不作为接线依据」；daemon 占锁时情绪侧照常、`db_error` 标注降级。CLI 支持 `--db / --shadow-file / --min-days / --json` |
| 2 | `ui/pages/3_情绪温度计.py` | **只读看板**（Streamlit 原生 multipage 自动收录）：情绪分数时间线、P2/P3 系数走势、分状态质量表、吻合度指标卡；数据与 CLI 同一口径（复用 `shadow_quality_snapshot()`）；daemon 在跑时软提示而非阻断 |
| 3 | `full_health_check.bat` | **停机窗口一键体检**：直接探测「只读连接能否打开」→ 占库时先告警；串跑 `verify_system.py` + `tools/system_health.py`，汇总退出码。纯 ASCII（见 §三.1） |
| 4 | `tools/_smoke_ui_sentiment.py` | **看板 AppTest 冒烟**（自包含）：用 QA 副本库日期现场生成临时 JSONL、monkeypatch `SHADOW_JSONL`/`open_ro`，跑「有记录完整渲染」+「无记录降级」两场景，均断言 exception=0；不碰生产库与生产 JSONL |

## 二、验证结果

| # | 项目 | 结果 |
|---|---|---|
| 1 | `compileall`（core/ui/tools/data/service + 根文件） | **exit=0** |
| 2 | 评估器实跑（QA 库 + 构造 8 交易日 JSONL + `--min-days 5`） | **正常**：分状态表 / 三口径 / 分池全部输出；同日重复记录正确去重；`--json` 输出正常 |
| 3 | 评估器「无记录」路径 | 正确提示尚未生成，不崩 |
| 4 | 看板 AppTest（`tools/_smoke_ui_sentiment.py`） | **2/2 场景 PASS**（有记录 metrics=7/tables=2；无记录 warning=1；exception 均为 0） |
| 5 | `full_health_check.bat` 端到端（daemon 占库态） | **符合预期**：锁探测正确告警 → verify_system 跑完（4 条 FAIL 全为占库 `IO Error` 环境性失败，与 11-14 主报告记录同型）→ system_health 优雅降级 `OK=1 WARN=1 FAIL=0` |

## 三、过程中发现并规避的两个环境坑（存量脚本请留意）

1. **bat 中文编码坑**：本机 cmd 代码页为 936（GBK），UTF-8 中文（**即使在 REM 里**）会造成解析错位、吃掉后续 ASCII（实测出现 `'ify_system' is not recognized` / `'cho' is not recognized` 等残缺命令）。`full_health_check.bat` 已改为**纯 ASCII**。
   ⚠ 存量 bat 的 REM 行含 UTF-8 中文，在本机 cmd 下有同样风险——**已在 §六 统一修整**（约定：`chcp 65001` 置于任何中文行之前，可执行行尽量 ASCII）。
2. **`py` 启动器失效**：本机 `C:\Windows\py.exe` 指向不存在的 `_pyenv\py312\python.exe`，`py` 一律失败、`python` 正常。新脚本已加「探活回退（py → python）」；存量 bat 的 `py` 优先模式在本机会直接报 `[ERROR] Python not found`——**已在 §六 全部补上探活回退**。
3. **sentinel 锁 ≠ 真实占库**：脚本首版用 `DAEMON_LOCK_FILE` 判 daemon，出现「sentinel 说没跑、库实际被锁」的不一致；已改为**直接探测只读连接**（体检结果真正受影响的条线）。

## 四、红线自查

| 红线 | 是否守住 |
|---|---|
| 不改策略逻辑 / 评分权重 / 阈值 / 闸门 | ✅（4 个新文件全部只读，零存量修改） |
| 不动 `config.yaml` / `master_control.json` | ✅ |
| DuckDB 单写者 | ✅（评估器 `read_only=True`；冒烟走 `data/_qa` 副本） |
| 不污染生产数据 | ✅（冒烟用临时 JSONL，跑完即删；未写入生产库/生产 JSONL） |
| 报告/日志无密钥明文 | ✅ |

## 五、遗留与下一步

1. **周一开盘后** daemon 首次扫描即会写入 `data/runtime/observability/sentiment_shadow.jsonl`（本周末无交易、文件为空属正常）；届时看板/评估器即可看到首批数据。
2. 攒满 **≥20 个交易日**后运行 `python tools/sentiment_shadow_eval.py`（或看 UI 页）看三口径是否方向一致，再决定 H1 是否接线——**接线与否仍由你拍板**。
3. 停机窗口跑一次 `full_health_check.bat`（预期 verify `80/0/0` + system_health 完整读库版）。
4. 仍待拍板的策略决策（本轮一律未动）：P1 市值门槛 120 亿 vs 涨停龙头；push_history P1 口径；`fact_limit_theme` 是否作为选股输入。
5. ~~存量 bat 的编码 / `py` 兼容问题统一修一遍~~ → **本轮已完成（见 §六）**。

## 六、存量 bat 统一修整（本轮补充执行）

**根因结论**：本机 cmd 以 CP936（GBK）读取 bat 文件；UTF-8 编码的中文字节会被错位配对，
吞掉相邻 ASCII 字符（实测 `'ify_system' is not recognized` 之类的残缺命令）。
项目其实已有正确约定（`start_server.bat` 头注释 + `start_daemon_24x7.bat` / `deploy_new_machine.bat` 的
`chcp 65001`）：**① `chcp 65001` 置于任何中文行之前；② 可执行行尽量纯 ASCII；③ 解释器探活失败要回退。**

| 文件 | 修整内容 |
|---|---|
| `weekly_maintenance.bat` | 补 `chcp 65001`（原先中文 REM 在 CP936 下会错位）；补 py→python 探活回退 |
| `install_all_deps.bat` | 补 py→python 探活回退（文件本身已纯 ASCII，无需 chcp） |
| `start_server.bat` | 解释器段改探活式（py 探活失败回退 python，再进原有报错门） |
| `start_daemon_24x7.bat` | 同上（原先 `where py` 成功但 py 本体损坏时会直接走 NO_PY，现先回退 python） |
| `deploy_new_machine.bat` | 已符合约定（chcp + 探活式四级回退），未动 |
| `full_health_check.bat` | 已按纯 ASCII 约定编写，未动 |

**验证**：
- 四份 bat 的「去破坏化副本」实跑（python 调用 / pause / 循环替换为 echo）：**4/4 解析干净，0 条 not recognized 碎片**；
- py→python 回退链真机验证：`where py` 命中（坏的）→ 探活失败 → 回退 `python` → 成功输出 runtime 路径（Python311）；
- `compileall` 全仓仍 exit=0（bat 改动不涉及 Python 侧代码）。

**红线**：仅动批处理启动工程、未触及任何业务逻辑 / 配置数值 / 数据文件。

*报告结束 · 20261114 R2 · 全文不含任何 token / api_key / webhook 明文。*
