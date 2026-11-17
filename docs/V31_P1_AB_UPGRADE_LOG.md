# V31 升级日志 · P1 选股三层优化（批次 A + 批次 B）

- 升级日期：2026-10-13
- 范围：`core/p1_death_cause_registry.py`、`core/p1_explain.py`、`core/pool_manager/{single_stock,thresholds}.py`、`core/strategies/fund_mv_utils.py`、`core/config_manager.py`、`ui/app.py`、`config.yaml`、`tools/`
- 铁律遵守情况：`auto_apply=False` 未变；评分权重、阈值档位、过滤规则、止损模板零改动；`core/pool_manager.legacy.py` 未触碰；未做任何 git 操作

---

## 一句话

这一版只做两件事：**让 P1 的淘汰死因可分层归因（A）**，**把两条量纲有缺陷的硬闸门改成正确口径（B）**。策略逻辑、权重、阈值全部保持原样。

---

## 批次 A：P1 死因五层分级（可归因）

### 问题

P1 有 30 条淘汰死因，在 UI 上是 30 条**平权**并列文本。用户看到「MA20下方」和「业绩爆雷」长得一样，却完全不知道该做什么——这两者的处置动作是**完全相反**的。层级信息在原始死因文案里被丢掉了。

### 做法

新增 `core/p1_death_cause_registry.py`，纯静态映射表，不读库、不参与判定：

| 层级 | 中文名 | 徽标 | 处置动作 | 语义 |
|---|---|---|---|---|
| L0 | 排除级 | 🔴 | 永久排除 | 碰了就亏钱，不因技术面修复 |
| L1 | 否决级 | ⚪ | 长期排除 | 不符合 P1 定位，需人工判断 |
| L2 | 拦截级 | 🟡 | 等信号修复 | 时机不对，可复活 ★ 最需要盯 |
| L3 | 降权级 | 🟠 | 仅扣分 | 只是不够好，走到淘汰属边界个例 |
| D | 数据级 | ⚫ | 系统问题 | 不是股票的问题，是系统没搞清 |

### 本批次修掉的 3 个真实 Bug

| # | 文件:行 | 原始症状 | 根因 | 修复 |
|---|---|---|---|---|
| A-1 | `core/p1_death_cause_registry.py` | MACD过弱 被错标成 L0「ST永久排除」 | 匹配是子串包含，MACD 文案里的「容错档**strict**」小写后含 `st`，被裸 `ST` 规则首命中 | 关键词改为真实文案串 `ST/退市风险`（来源：`core/event_filter.py` 的 `tags.append("ST/退市风险")`） |
| A-2 | 同上 | `高位派发-15`（可复活）被标成 L0「高位派发」（永久排除） | 规则表是「首命中即返回」，`高位派发` 声明在前，先匹配 | 把 `高位派发-15` 上移到 `高位派发` 之前 |
| A-3 | 同上 | 空死因（None/""/空白）被标成 L3「只是不够好」 | 空 reason 复用了 `UNKNOWN_LEVEL`(L3) | 新增 `EMPTY_REASON_LEVEL` 专用 D 兜底：说不出为什么 = 系统问题 |
| A-4 | `core/p1_explain.py:238` | 复合死因文案（如同时含「股性呆滞」和「MA20下方」）的层级会被整条文案的首个命中规则带偏，导致每个闸门挂同一层级 | 对**完整 reason** 做首命中分类，而实际需要的是**本闸门**的层级 | 新增 `classify_gate_death_cause(reason, keyword)`，按已确认触发的那个闸门关键词分类；未触发的闸门仍保持中性（不挂层级） |

### 附带修正

- `量价背离检查缺少` / `回撤检查缺少` 两条规则原登记为 L3，但 advice 文案本身写的是 ⚫，自相矛盾 → 改 D 层。
- 旧缓存 / 旧 JSON / pickle 行在 UI 展示前自动补全层级字段（`ui/app.py` 的 `enrich_rejected_rows` 兜底）。
- 阵亡表新增死因层级概览一行（`L0 x | L1 x | L2 x | L3 x | D x`）+ 处置优先级提示。

---

## 批次 B-1：MACD 柱量纲修正（绝对值 → 相对股价百分比）

### 问题

`core/pool_manager/single_stock.py` 的 MACD 闸门用**绝对值**比较：

```
macd_bar <= macd_kill_eff          # macd_kill_eff ≈ -0.08 / -0.135 / -0.200 等
```

但 MACD 柱的量纲是**元**，随股价线性放大。后果是同一条阈值对不同价位完全是两套标准：

| 股价 | 柱 -0.13 元 | 相对股价 | 现状判定 |
|---|---|---|---|
| 5 元 | -0.13 | **-2.60%** | 轻易被砍（冤枉） |
| 100 元 | -0.13 | **-0.13%** | 几乎砍不到（放过） |

即：**低价股被冤枉、高价股被放过**，而这两类恰好都在 P1 池里。

### 标定方法（无主观参数）

`tools/probe_macd_bar_kill_scale.py`，40 个有效交易日、流通市值 ≥120 亿、全市场样本池：

1. 读 config.yaml 三 profile 真实基值（strict -0.08 / neutral -0.13 / relaxed -0.16）
2. 经 `_effective_p1_thresholds` 得到容错补偿后的 9 个真实生效阈值
3. 在样本池上算现状淘汰分位 q，再取 `macd_bar/close*100` 的 q 分位数作等效百分比阈值
4. 构造上保证总体淘汰率与现状一致，**不引入任何人为权重**

### 标定结果

| profile | tier 0 | tier 1 | tier 2 |
|---|---|---|---|
| strict | -0.4268% | -0.5652% | -0.6930% |
| neutral | -0.5547% | -0.6665% | -0.7715% |
| relaxed | -0.6180% | -0.7182% | -0.8123% |

- 样本池总体淘汰率差：**0.00 pp**（构造等价）
- 逐交易日淘汰率差最大：**4.42 pp**（目标 ≤5pp，PASS）
- 翻转型态按价位段（strict/档0）：

| 价位段 | 样本 | 旧杀率 | 新杀率 | 差异 |
|---|---|---|---|---|
| <10 元 | 13617 | 2.64% | 17.65% | **+15.02pp** |
| 10–20 元 | 11851 | 18.34% | 24.14% | +5.80pp |
| 20–50 元 | 14665 | 32.04% | 24.54% | -7.49pp |
| 50–100 元 | 7522 | 34.39% | 23.30% | -11.09pp |
| ≥100 元 | 6850 | 34.42% | 22.76% | **-11.66pp** |

低价段杀得更狠、高价段杀得更松 —— 这就是量纲缺陷的量化证据。

### 落地方式

- `config.yaml` 三 profile 各新增 `macd_bar_kill_pct: [tier0, tier1, tier2]`，**旧 `macd_bar_kill` 全部保留**
- `core/config_manager.py` 新增 `_p1_parse_macd_bar_kill_pct`：只接受长度 ≥3、全负、单调不增的数值序列，任一步失败返回 `None`
- `single_stock.py` 有 pct 配置走百分比判定，无配置或解析失败**自动退回旧绝对值判定**
- 死因文案改为 `MACD过弱(柱-0.3000≈-0.30%股价≤底线-0.55%,容错档0)`，关键词 `MACD过弱` 不变（registry 仍归 L2）

### Shadow 差集留痕（不自动生效）

新增 `data/runtime/p1_macd_gate_shadow.jsonl`：每当日志记录新旧口径判定不一致的样本（ts_code / profile / tier / close / macd_bar / macd_bar_pct / old_kill / new_kill / ts），超过 5MB 自动轮转为 `.1`。

- 只写本地 JSONL，**不碰 DuckDB**
- **不改权重、不改历史数据、不回写配置**
- 任何写入失败静默吞掉，绝不打断 P1 主链路

---

## 批次 B-2：股性呆滞换手阈值连续化（硬阶梯 → smoothstep）

### 问题

`single_stock.py` 与 `fund_mv_utils.py` 各有一份**硬阶梯**，断点处阈值跳变：

```
999 亿  → 底线 1.50%
1000 亿 → 底线 1.05%    ← 同一只票，市值涨 0.1% 就换了一套标准
1999 亿 → 底线 1.05%
2000 亿 → 底线 0.65%    ← 同上
```

一只票的生死取决于它落在断点的哪一侧，而市值本身是每天波动的连续量。制造两类噪声：边界票反复横跳（今天进池、明天被砍）；断点附近的票天然获得额外豁免（可被「卡市值」规避）。

### 方案选择

试过「分段线性全局插值」，但它会改掉中段约 70% 样本的口径。最终采用**窄带 smoothstep**：

```
mv <  900 亿  : 1.50%（与旧 <1000 亿档完全一致）
900~1100 亿   : smoothstep 1.50% → 1.05%
1100~1900 亿  : 1.05%（与旧 1000~2000 亿档完全一致）
1900~2100 亿  : smoothstep 1.05% → 0.65%
mv >= 2100 亿 : 0.65%（与旧 >=2000 亿档完全一致）

smooth(t) = t*t*(3-2t)
```

### 标定结果（`tools/probe_turnover_kill_continuous.py`，40 个有效交易日）

- 值连续性：相邻 1 亿最大跳变 **0.003375 pp**，约为旧阶跃 0.45pp 的 **1/133** → PASS
- 导数连续性：相邻步长最大二阶差 **0.000067 pp** → PASS
- 锚点一致性：6/6 与旧阶梯逐位相同（200/899→1.50、1200/1800→1.05、2200/5000→0.65）
- 逐日淘汰率差最大 **0.11 pp**，平均 0.04 pp
- 主阶梯逐票差集 **0.05%**（新增杀 0 / 解除杀 1），目标 <3% → PASS
- 放宽通道（×0.75 同构阶梯）差集 **0.05%** → PASS

### 落地方式

- 新增唯一口径函数 `core/strategies/fund_mv_utils.turnover_kill_threshold_pct(circ_mv_yi)`
- `single_stock.py` 与 `adaptive_turnover_kill_threshold_relaxed` **同时**改为调用它，消除两份阶梯
- `core/pool_manager/thresholds.py` 透出该函数并加入 `__all__`，同时提供**退回旧硬阶梯**的容错 import 兜底
- 死因文案统一为 `🧊股性呆滞(近5日均换手0.80%<底线1.28%,市值980亿)`，关键词 `股性呆滞` 不变；刻意用「市值」而非「流通市值」，避免误触发市值硬闸

---

## 回归结果

| 测试 | 结果 |
|---|---|
| `tools/_smoke_p1_death_cause.py`（批次 A） | PASS=52 FAIL=0 |
| `tools/_smoke_p1_ab_batch_b.py`（批次 B） | PASS=87 FAIL=0 |
| `tools/test_p1_vol_sun.py` | ALL PASSED（6/6） |
| `tools/_smoke_full.py` | ALL GREEN |
| `tools/_smoke_real.py` | EXIT=0 |
| `tools/_smoke_index_bench.py` | pass=112 fail=0 |
| `tools/_smoke_strategy_panel.py` | pass=27 fail=0 |
| `python -m compileall -q data core ui tools ...` | EXIT=0 |
| `python verify_system.py` | 51 通过 / 0 失败 / 0 警告 |

新增测试：
- `tools/_smoke_p1_death_cause.py` —— 批次 A 死因五层 52 项断言
- `tools/_smoke_p1_ab_batch_b.py` —— 批次 B 87 项断言（含「legacy 未被改动」红线校验）
- `tools/probe_macd_bar_kill_scale.py` —— B-1 标定探针（只读）
- `tools/probe_turnover_kill_continuous.py` —— B-2 标定探针（只读）

---

## 遗留与建议

1. **shadow 差集尚未积累**：落地后建议跑满 2 周再决定是否把百分比口径写成默认；在此之前旧 `macd_bar_kill` 一直作为兜底保留，可随时一行回退。
2. **翻转型态需人工抽样核对**：<10 元段新增杀 15pp，建议在 `p1_macd_gate_shadow.jsonl` 里抽 20 只低价股人工看图，确认新判定更符合实际。
3. **B-2 断点附近样本极少**：900–1100 亿带仅约 32 只、1900–2100 亿带仅约 5 只，差集统计意义有限，但函数连续性本身已自证，风险主要来自样本而非公式。
4. **`regime_analyzer` 仍未接控制**：本轮未动（属于策略语义变更，需先观察 3 个月吻合率 >70%）。
5. **`core/pool_manager.legacy.py` 未同步**：按红线保留旧逻辑，它是历史快照，不参与现行链路。
