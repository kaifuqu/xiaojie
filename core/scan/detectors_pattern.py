# -*- coding: utf-8 -*-
"""
core/scan/detectors_pattern.py
原 core/scan_engine.py L530-L619（V26.88 拆分）

【职责】
- 黄金门禁：_golden_evaluate / strict_golden_burst_ok（已在 utils）
- 黑名单机制：_add_to_blacklist（V26.88+ 9-26 隔离风控）
- 钓鱼线熔断：danger_buy 系列（DANGER_BUY_VWAP_MIN_DAY_PCT + _load_breakout_vwap_eps_for_danger_buy + _danger_buy_vwap_fish_line_trigger）
- 注：_evaluate_personality（个股性格评估）已迁移到 _detectors_market.py

【公开 API】
- DANGER_BUY_VWAP_MIN_DAY_PCT
- _add_to_blacklist
- _load_breakout_vwap_eps_for_danger_buy
- _danger_buy_vwap_fish_line_trigger
"""
import logging
import json
import os
from datetime import datetime, timedelta

from core.scan.utils import BJ_TZ, ensure_runtime_data_layout, path_blacklist_json

logging.basicConfig(level=logging.INFO, format='%(asctime)s [扫描引擎-pattern] %(message)s')


# ==================== ⛓️ 小黑屋机制 (黑名单隔离) ====================
def _add_to_blacklist(ts_code, stock_name, reason):
    """写入 7 天有效期的临时黑名单（用于 danger_buy/VWAP 钓鱼线熔断等场景）。"""
    blacklist_file = path_blacklist_json()
    today_str = datetime.now(BJ_TZ).strftime("%Y%m%d")
    blacklist = {}
    if os.path.exists(blacklist_file):
        try:
            with open(blacklist_file, 'r', encoding='utf-8') as f:
                blacklist = json.load(f)
        except json.JSONDecodeError as e:
            logging.warning("黑名单 JSON 解析失败，将覆盖写入新内容 [%s]: %s", blacklist_file, e)
        except OSError as e:
            logging.warning("读取黑名单文件 IO 失败 [%s]: %s", blacklist_file, e)
        except Exception as e:
            logging.exception("读取黑名单文件失败（未分类）[%s]: %s", blacklist_file, e)

    blacklist[ts_code] = {
        "name": stock_name,
        "reason": reason,
        "kill_date": today_str
    }

    cutoff_date = (datetime.now(BJ_TZ) - timedelta(days=7)).strftime("%Y%m%d")
    keys_to_remove = [k for k, v in blacklist.items() if v.get("kill_date", "") < cutoff_date]
    for k in keys_to_remove:
        del blacklist[k]

    ensure_runtime_data_layout()
    try:
        with open(blacklist_file, 'w', encoding='utf-8') as f:
            json.dump(blacklist, f, ensure_ascii=False)
    except OSError as e:
        logging.error("写入黑名单失败(IO): %s | %s", blacklist_file, e)
    except Exception as e:
        logging.exception("写入黑名单失败(未分类): %s | %s", blacklist_file, e)


# ==================== 🛑 danger_buy：分时 VWAP 钓鱼线熔断 ====================
# 与 P3 物理胸甲共用配置项 breakout_vwap_eps（config.yaml strategies.p3_intraday_screener）；
# 右侧冲高但现价显著低于当日成交量加权均价时，叠加禁买并拉黑，防主力分时出货诱多。
try:
    from core.strategies.p3_intraday_screener import _estimate_vwap_from_rt as _danger_buy_estimate_vwap_from_rt
except Exception:
    _danger_buy_estimate_vwap_from_rt = None

# 「现价很高」下限（%）：避免低开弱票因略低于 VWAP 误触；与 VWAP 缺省时不硬杀策略一致
DANGER_BUY_VWAP_MIN_DAY_PCT = 4.0


def _load_breakout_vwap_eps_for_danger_buy() -> float:
    """读取 P3 配置中的 breakout_vwap_eps，供禁买 VWAP 熔断与策略层对齐。"""
    try:
        from core.config_manager import get_p3_intraday_screener_config
        cfg = get_p3_intraday_screener_config()
        return float(getattr(cfg, "breakout_vwap_eps", 0.004))
    except Exception:
        return 0.004


def _danger_buy_vwap_fish_line_trigger(
    now_px: float,
    day_pct: float,
    rt: dict,
    curr_min: int,
    breakout_vwap_eps: float,
) -> bool:
    """
    硬规则：涨幅已处高位，但现价低于当日可估计的分时 VWAP 超过 breakout_vwap_eps 比例 → 真。
    盘前（无可靠分时）或额量不足以估计 VWAP 时不触发（与 p3_intraday_screener 一致）。
    """
    if breakout_vwap_eps <= 0 or not isinstance(rt, dict):
        return False
    if _danger_buy_estimate_vwap_from_rt is None:
        return False
    # 集合竞价前：分时均价无意义
    if curr_min < 570:
        return False
    if float(day_pct) < float(DANGER_BUY_VWAP_MIN_DAY_PCT):
        return False
    px = float(now_px)
    if px <= 0:
        return False
    vw = float(_danger_buy_estimate_vwap_from_rt(rt, px))
    if vw <= 0:
        return False
    return px < vw * (1.0 - float(breakout_vwap_eps))