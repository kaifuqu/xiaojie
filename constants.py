# -*- coding: utf-8 -*-
"""
小杰AI选股系统 Pro V26.78 - 全局常量与阈值控制中心
所有硬编码参数集中于此；版本号与全项目审查基线保持一致。

【V26 数据底座】capital_resonance_score 算法见 data/capital_resonance_features.py（80 底座 +20 两融加分，向量化）；
P1 共振阶梯阈值 P1_CRS_SUPER_MIN / P1_CRS_MID_MIN；P3–P5 动态分共振系数 CAPITAL_RESONANCE_WEIGHT_P345。
【V26 资金记忆体系】fund_memory_score：21 交易日半衰期指数衰减、双重过滤（≥100 亿流通 + 60 日放量异动痕迹），
由日线管道维护；P1 最终平滑分由 score_calibration 按 FUND_MEMORY_WEIGHT_P1 / config fund_memory_weight_p1 可选凸组合。FUND_MEMORY_HALF_LIFE_DAYS 等见第 7 节。
日线宽表列集与业务字段以本仓库 data_fetcher / db_core 契约为准。
"""
import os

# 全项目统一版本标识（审计 / UI / 日志）
# 【V26.88+ 9-27 修正】版本号与实际系统版本对齐
APP_VERSION = "V32.0"

# ==================== 0. 档位命名统一 ====================
POOL_NAME_CN = {
    "p1": "底仓池",
    "p2": "竞价池",
    "p3": "盘中池",
    "p4": "盘尾池",
    "p5": "盘后池",
}

# ==================== 1. 全局路径管理 ====================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
P0_FILE_PATH = os.path.join(BASE_DIR, "p0_custom.txt")

# ==================== 2. 市值过滤门槛 (单位: 万元) ====================
# 【V26.88+ 9-26 双闸重构】数据下载闸 vs 选股闸：
#   - DATA_FETCH_MIN_TOTAL_MV_WAN：日线生肉落库最低总市值（数据下载闸）；P0 自选股在 data_fetcher 中单独豁免。
#   - STRATEGY_MIN_CIRC_MV_WAN：P1~P5 选股 / 打分 / 巡逻共用最低流通市值（选股闸）。
# 两个常量均遵循「代码即文档」，任何调用方都应通过 core.config_manager 读取（带 config.yaml / 实验室覆写），
# 而不是直接 import 这两个常量（避免魔法数散落）。
DATA_FETCH_MIN_TOTAL_MV_WAN = 1_200_000  # 120 亿（2026-09-26 由 100 亿→120 亿，对齐用户双闸要求）
STRATEGY_MIN_CIRC_MV_WAN = 1_200_000     # 120 亿（2026-09-26 由 140 亿→120 亿，对齐用户双闸要求）
# P1 前置防线：现价低于该值(元)视为机构审美拦截（pool_manager）
P1_INSTITUTION_MIN_PRICE_YUAN = 7.0

# 【V26.88+ 9-26】保留旧名作为兼容别名（已映射到 120 亿），新代码禁止直接引用。
# 引用方应改为 constants.STRATEGY_MIN_CIRC_MV_WAN / constants.DATA_FETCH_MIN_TOTAL_MV_WAN。
DAILY_BASIC_MIN_MV_WAN = DATA_FETCH_MIN_TOTAL_MV_WAN  # 旧名（= 120 亿）
P1_SELECT_MIN_CIRC_MV_WAN = STRATEGY_MIN_CIRC_MV_WAN  # 旧名（= 120 亿）
MIN_FETCH_MV = DATA_FETCH_MIN_TOTAL_MV_WAN
MIN_STRAT_MV = STRATEGY_MIN_CIRC_MV_WAN

# ==================== 2.1 日线下载窗口（交易日）====================
# 【V26.88+10-5 新增】全项目唯一「日线历史窗口」口径。
#
# 【为什么必须有这个常量】
# auto_sniper_daemon / ui.ui_sidebar / data.fetcher 全部用
#     getattr(constants, "MAX_DAYS", 150) or 150
# 读取下载窗口，但该常量此前**从未被定义过**，因此一律静默回退到 150。
# 加上 check_data_completeness 的日历窗口按 max_days 截尾，
# 导致 daily_data 实际只回溯到 150 个交易日（约 7 个月），
# 库起点更早的日期被整体丢弃 —— 这就是 P5 侧 RPS-120 永远算不出值、
# 以及 p1_precompute 无法做 120 日横截面的根因。
#
# 【V26.88+10-5 改 120 → 130】空库首跑 / 补缺窗口由 120 提到 130 个交易日。
# 动机：120 恰好卡在 RPS 的临界点（period=120 需 121 根），库里少 1 天就整片 NaN；
# 130 留出 10 天冗余，让 period=120 在「日历停牌/新股/半残日」下仍能凑满 121 根。
#
# 【为什么不是 150】
# 150 会把 daily_data 起点往前推 30 个交易日，多出的历史当前没有任何指标消费
# （RPS_PERIOD=60、P1 get_stock_data_qfq(limit=120)），只增 Tushare 配额与落库耗时。
# 需要更长窗口时直接上调此值，全链路自动生效。
#
# 【注意】get_stock_data_qfq(limit=120) / ma250 等仍按各自口径取数，不受此常量影响。
#
# 【影响面】
# 读取方：auto_sniper_daemon.py(_data_sync_job / _barrier_*)
#         ui/ui_sidebar.py(同步按钮 / 缺失日查询)
#         data/fetcher/_post_fetch_verification.py(sync_history 默认值)
#         data/fetcher/_raw_day_data.py(check_data_completeness 默认值)
MAX_DAYS = 130

# ==================== 3. 核心大单资金阈值 ====================
ELG_THRESHOLDS = {
    'tail_min': 800,      
    'pool1_min': -1000,   
    'surge_min': 2000     
}

# ==================== 4. 性能与并发配置 ====================
MAX_WORKERS = 16          
API_RETRY_TIMES = 3       

# ==================== 5. 右侧严格模式与风控打分 ====================
STRICT_MODE = {
    'p2_auction_vr': 1.2, 
    'p3_intraday_vr': 1.5,
    'p4_tail_vr': 1.8,    
    'min_turnover_f_yang': 1.5,  # 阳线最低自由换手率：放量进攻
    'max_turnover_f_yin': 0.8    # 阴线最高自由换手率：极致缩量洗盘
}

PENALTY_RULES = {
    'max_score_cap': 200,
    'min_pass_score': 85,
    'penalty_multipliers': {
        'afternoon_sneak': 0.7,
        'high_amplitude': 0.8
    }
}

# ==================== 6. 左侧战法专属阈值 (L1-L8) ====================
LEFT_SIDE = {
    'max_net_outflow': -800,     
    'emotion_crash_pct': -4.5,   
    'vwap_diverge': 0.965,       
    'big_yin_fake_pct': -6.0,    
    
    'elg_crash': 2500,           
    'elg_vwap': 1500,            
    'elg_probe': 1000,           
    'elg_macd_diverge': 2000,    
    'elg_rsi_diverge': 1500,     
    'elg_boll_touch': 1500       
}

# ==================== 市场状态自适应参数（V26.78 沿用）====================
REGIME_PARAMS = {
    "趋势市": {"filter_mult": 0.85, "base_pos": 40, "hold_days": 8, "allow_direct_chase": True},
    "震荡市": {"filter_mult": 1.0, "base_pos": 30, "hold_days": 5, "allow_direct_chase": False},
    "情绪退潮市": {"filter_mult": 1.3, "base_pos": 20, "hold_days": 3, "allow_direct_chase": False}
}

# 日志表名称
LOG_TABLE = "signal_log"

# ==================== 7. P1 资金共振 / 股性记忆（V26.78）====================
# P1 主池/观察池「排序键」中与日线 capital_resonance_score 的融合权重（与 pool_manager._p1_final_sort_key 一致）
CAPITAL_RESONANCE_WEIGHT_P1 = 0.18
# P3–P5：calculate_dynamic_score 中对 crs_ui（0~100）的线性加分系数（与 P1 的 18% 排序权重区分）
CAPITAL_RESONANCE_WEIGHT_P345 = 0.08
# P1 初筛·资金共振阶梯阈值（亿元分层，与 pool_manager 硬闸/扫描闸一致）
P1_CRS_SUPER_MIN = 78.0   # 流通市值 ≥300 亿
P1_CRS_MID_MIN = 60.0     # 100 亿 ~ 300 亿
# 股性记忆池半衰期（交易日）；与 fund_memory_score 状态机、data/fund_memory_score.py 一致
FUND_MEMORY_HALF_LIFE_DAYS = 21
# P1 平滑百分制主分与 fund_memory_score（0~200 映射到 0~100）的凸组合权重，建议 0.15~0.20；由 score_calibration 应用；config 可覆写；不进入 P4/P5
FUND_MEMORY_WEIGHT_P1 = 0.10

# ==================== 8. A 股板块动态涨跌停差异化（V26.88+ 9-30）====================
# 朋友2指出 one_word_board_pct_min=9.5 不能区分板块；P2/P3/P4 一字板/炸板/涨停检测使用板块差异化阈值
# 板块识别走 ts_code 后缀（30=创业板/科创板/北交所）：
#   - 创业板（30）：±20%（注册制）
#   - 科创板（68/69）：±20%（注册制）
#   - 北交所（8）：±30%（注册制）
#   - 主板（00/60）：±10%
#   - ST / *ST：仅沪/深主板 ±5%；创业板/科创板/北交所风险警示股仍执行
#     注册制 ±20% / ±20% / ±30%（沪深交易所风险警示股票价格涨跌幅限制规则）。
#     2026-10-18 修正：原先「ST 优先于板块」会把创业板/科创板 ST 误判成 5%，
#     导致一字板/T+1 涨停/跌停拦截阈值错误。

BOARD_LIMIT_UP_PCT = {
    "main": 9.5,          # 主板 / 中小盘：默认 9.5% 阈值（容忍一点波动）
    "chinext": 19.5,      # 创业板：±20% 阈值
    "star": 19.5,         # 科创板：±20% 阈值
    "bj": 29.5,           # 北交所：±30% 阈值
    "st": 4.5,            # ST / *ST：±5% 阈值
}


def get_limit_up_pct(ts_code: str, name: str = "") -> float:
    """
    根据 ts_code 与名称返回该股票适用的涨停阈值百分比。
    优先级：主板 ST（5%）> 创业板/科创板/北交所/主板（按 ts_code 后缀）。
    非主板 ST 不降档，仍按注册制幅度。
    """
    if not ts_code:
        return BOARD_LIMIT_UP_PCT["main"]
    code = str(ts_code).upper()
    name_upper = str(name or "").upper()
    # ts_code 形如 600000.SH / 000001.SZ / 300750.SZ / 688123.SH / 830xxx.BJ
    if code.endswith(".BJ"):
        return BOARD_LIMIT_UP_PCT["bj"]
    if code.endswith(".SZ"):
        # 30xxxx 是创业板
        if code[:6].startswith("30"):
            return BOARD_LIMIT_UP_PCT["chinext"]
        # 深市主板（000/001/002/003）：ST / *ST 降为 ±5%
        if "ST" in name_upper:
            return BOARD_LIMIT_UP_PCT["st"]
        return BOARD_LIMIT_UP_PCT["main"]
    if code.endswith(".SH"):
        # 688xxx 是科创板
        if code[:6].startswith("688"):
            return BOARD_LIMIT_UP_PCT["star"]
        # 沪市主板：ST / *ST 降为 ±5%
        if "ST" in name_upper:
            return BOARD_LIMIT_UP_PCT["st"]
        return BOARD_LIMIT_UP_PCT["main"]
    return BOARD_LIMIT_UP_PCT["main"]


def is_chinext_or_star(ts_code: str) -> bool:
    """是否为创业板 / 科创板（注册制±20% 票）"""
    if not ts_code:
        return False
    code = str(ts_code).upper()
    if code.endswith(".SZ") and code[:6].startswith("30"):
        return True
    if code.endswith(".SH") and code[:6].startswith("688"):
        return True
    return False


# ==================== 9. 统一放量阈值（V26.88+ 9-30）====================
# 朋友2指出 p3/p4/p5 各池 s1_vol_ma5_mult / s1_vol_ratio_min / crowding_leader_vr 等
# 散落在各池 config，定义口径不一致（vol_ratio vs vol_ma5 vs VR）；这里给出"统一换算公式"：
#
#   vol_spike_ratio = today_vol / max(vol_ma5, vol_ma20)
#
# 在 indicators 层向量化计算后，各池策略调用方只需读 vol_spike_ratio 即可。
# 各池 config 中的 vol_ma5_mult/vol_ratio_min 应通过 core.config_manager.get_unified_volume_threshold(pool, strategy)
# 读取，并叠加"绝对成交额下限"防小盘虚高。

# 绝对成交额下限（万元）：小盘票无量放大虚高的硬地板
VOLUME_ABSOLUTE_FLOOR_WAN = {
    "large_cap": 20000.0,    # 流通市值 ≥300 亿：2 亿成交额
    "mid_cap": 8000.0,       # 100-300 亿：8000 万
    "small_cap": 3000.0,     # 50-100 亿：3000 万
    "tiny_cap": 1500.0,      # <50 亿：1500 万（已由 STRATEGY_MIN_CIRC_MV_WAN=120 亿过滤）
}
# 换手率区间（真实换手 turnover_rate_f）：3% 下限防僵尸股，15% 上限防妖股见顶
TURNOVER_F_MIN_PCT = 1.5    # 全局下限（V26.88 由 0.8 → 1.5，过滤绝对死水）
TURNOVER_F_MAX_PCT = 15.0   # 全局上限（防高位放量见顶）
# P3 拥挤度三档：与 config.yaml p3.crowding_ordinary_vr / strong / leader 对齐（统一在此）
CROWDING_VR_ORDINARY = 2.5
CROWDING_VR_STRONG = 3.5
CROWDING_VR_LEADER = 4.2
# P4 vol_ratio 阈值：与 config.yaml p4.s1_vol_ratio_min 对齐
P4_VOL_RATIO_MIN = 1.5
# P3 s1 / s8 vol_ma5_mult 阈值（口径不同但统一收口）
P3_VOL_MA5_MULT_S1 = 1.5
P3_VOL_MA5_MULT_S8 = 1.85


# ==================== 10. HiThink 限流参数（V26.88+ 9-30）====================
# 朋友1指出 window_calls_per_min=60 过紧，09:25 集中调用必熔断；上调到 90 与官方"建议控制频率"对齐。
HITHINK_WINDOW_CALLS_PER_MIN = 90
HITHINK_FAIL_CIRCUIT_BREAKER_SEC = 30  # 连续失败熔断时长（不变）
HITHINK_TOTAL_FAIL_THRESHOLD = 5       # 熔断触发阈值（不变）


# ==================== 11. t1_memory 小样本（V26.88+ 9-30）====================
# 朋友1指出 P2 t1_memory_min_samples=6 在 5300 只股票中大部分新股/小票永远凑不齐 6 次 T+1 历史，
# 导致该功能空转；改 min_samples=2，且小样本时按样本数比例衰减 boost。
T1_MEMORY_MIN_SAMPLES = 2              # 全局最小样本门槛（V26.88 由 6 → 2）
T1_MEMORY_SAMPLE_DECAY = 0.4           # 小样本时 boost 衰减系数（n=2 时约 0.4× boost）
T1_MEMORY_BOOST_MAX = 10.0
T1_MEMORY_PENALTY_MAX = 8.0


# ==================== 12. 推送去重（V26.88+ 9-30）====================
# 朋友1指出 push_dedup 跨池冷却已是当日一次，但 P2→P3→P4→P5 同票在当天 5 个池可能产生 4 次推送。
# 增强：同票在 P2→P3→P4 任意两个池当日都触发时，仅保留最高分池推送，其余标 silent 状态入 push_history。
PUSH_DEDUP_SAME_DAY_CROSS_POOL = True  # 全局开关
PUSH_DEDUP_SCORE_GAP_FOR_DUPLICATE = 5.0  # 同票同日多池触发时，分差 < 此值视为重复
