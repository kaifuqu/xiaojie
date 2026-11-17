# -*- coding: utf-8 -*-
"""
小杰AI选股系统 Pro V26.88 - 策略名称别名映射表
================================================================================
【用途】
  1. 代码内部使用拼音键名（如 L1_ri_xian_duo_tou）避免编码乱码
  2. 用户界面（Streamlit/企微推送）显示中文别名（如 "日线多头突破"）
  3. 策略实验室 run_lab.py 中参数编辑与 YAML 序列化的双向转换

【使用方式】
  from core.strategies.strategy_name_alias import (
      PINGYIN_TO_CN,   # 拼音键 -> 中文显示名
      CN_TO_PINGYIN,   # 中文显示名 -> 拼音键（精确匹配）
      SCORE_KEYS,      # strategy_scores 中所有拼音键
      WINRATE_KEYS,    # strategy_winrate 中所有拼音键
  )

【规则】
  - 拼音键名：纯 ASCII（字母/数字/下划线），避免中文编码问题
  - 中文别名：UTF-8，供 UI 和日志展示
  - 龙头系列：独立键名 longtou_ 前缀（避免与普通版 100_/101_ 混淆）

【维护说明】
  - 新增策略：同时添加到 PINGYIN_TO_CN 和 CN_TO_PINGYIN
  - 删除策略：从两表中同时移除
  - 重命名策略：旧拼音键保留在 DEPRECATED_KEYS 做兼容
================================================================================
"""

# ============================================================
# 拼音键 -> 中文显示名
# ============================================================
PINGYIN_TO_CN = {
    # L系列：左侧战法（已废弃保留向后兼容）
    "L1_ri_xian_duo_tou": "日线多头突破",
    "L2_VWAP_dong_liang": "VWAP动量",
    "L3_wu_liang_po_di": "无量破底",
    "L5_jin_chou_po_hui": "金筹破汇",
    "L8_fu_ji_dan_chou": "复基单筹",

    # 80-91系列：右侧战法
    "82_lian_ban_long_tou": "连板龙头",
    "84_shuo_liang_jia_su": "缩量加速",
    "80_you_shang_po_di": "佑上破底",
    "81_ce_lve_fu_cha": "策略复差",
    "90_wu_liang_chong_guan": "无量重关",
    "83_shou_yin_ci_xin": "守音次新",
    "02_zhong_wu_fan": "中吴反",
    "91_shu_liang_xiu_li": "数量修历",
    "60_po_di_cheng_gong": "破地成功",
    "58_maichou_di_xiao": "买筹低效",
    "59_maichou_gao_xiao": "买筹高效",
    "30_ce_lve_ci_xin": "策略次新",
    "01_ci_xin_pian_ru": "次新偏入",
    "29_ci_maichou": "次买筹",

    # 99系列：黄金战法
    "99_ri_xian_po_di": "日线破地",
    "96_you_shang_cheng_gong": "佑上成功",
    "95_ci_xin_chou_dai": "次新筹代",
    "100_ci_xin_fu_cha": "次新复差（普通）",
    "101_ci_xin_dan_chou": "次新单筹（普通）",
    "94_you_shang_ji_ji": "佑上积极（普通）",

    # 龙头系列（独立键名，避免与普通版混淆）
    "longtou_ci_xin_fu_cha": "次新复差龙头",
    "longtou_ci_xin_dan_chou": "次新单筹龙头",
    "longtou_you_shang_ji_ji": "佑上积极龙头",
}

# ============================================================
# 中文显示名 -> 拼音键（反向映射）
# ============================================================
CN_TO_PINGYIN = {v: k for k, v in PINGYIN_TO_CN.items()}

# ============================================================
# 便捷函数
# ============================================================
def get_display_name(pinyin_key: str) -> str:
    """拼音键 -> 中文显示名，未知键返回原始拼音"""
    return PINGYIN_TO_CN.get(pinyin_key, pinyin_key)


def get_pinyin_key(cn_name: str) -> str:
    """中文显示名 -> 拼音键，未知名返回原始中文"""
    return CN_TO_PINGYIN.get(cn_name, cn_name)


def format_strategy_tag(pinyin_key: str, score: float = None) -> str:
    """格式化策略标签：中文名 + 可选分数"""
    name = get_display_name(pinyin_key)
    if score is not None:
        return f"{name}({score:.0f})"
    return name


def is_long_tou(pinyin_key: str) -> bool:
    """判断是否为龙头系列策略"""
    return pinyin_key.startswith("longtou_")


# ============================================================
# 集合定义（用于快速判断）
# ============================================================
SCORE_KEYS = frozenset(PINGYIN_TO_CN.keys())
WINRATE_KEYS = frozenset(CN_TO_PINGYIN.keys())  # 与 PINGYIN_TO_CN 相同
LEFT_SERIES_KEYS = frozenset(k for k in PINGYIN_TO_CN if k.startswith("L"))
RIGHT_SERIES_KEYS = frozenset(k for k in PINGYIN_TO_CN if k.startswith(("8", "9", "0", "1", "2", "3", "4", "5", "6")))
LONGTou_KEYS = frozenset(k for k in PINGYIN_TO_CN if k.startswith("longtou_"))

# ============================================================
# 已废弃别名（用于兼容旧数据）
# ============================================================
DEPRECATED_KEYS = {
    # V26.88 前的旧拼音键名（如有）
}


def normalize_key(key: str) -> str:
    """
    标准化策略键名：
    - 优先从 DEPRECATED_KEYS 映射到新版
    - 再从 PINGYIN_TO_CN 中文别名查找
    - 最后原样返回
    """
    if key in DEPRECATED_KEYS:
        return DEPRECATED_KEYS[key]
    if key in CN_TO_PINGYIN:
        return CN_TO_PINGYIN[key]
    return key
