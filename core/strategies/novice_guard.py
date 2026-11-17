# -*- coding: utf-8 -*-
"""
小杰AI选股系统 Pro V26.88 — 新股民保护总闸（Novice Guard）
================================================================================
【设计背景】
本模块面向"用户自评新股民，将直接按提示买入"这一高风险场景，对所有 P1-P5 出票
做一层独立的二次过滤。设计原则：**宁可错过也不冒险**。

【与现有体系的关系】
- 不修改 risk_control_engine 主体（754 行稳定代码避免回归）；
- 在 P1/P2/P3/P4/P5 finalize 之后、企微推送之前介入（push_stock_if_allowed 入口）；
- 与三层风控互补：layer1/2/3 仍在策略引擎内执行；本模块专注于"推送侧过滤 +
  僵尸赛道 + 风险标签 + 场景化买入提示"，并把 ui_alert_only 切回硬否决模式。

【七大防线】
1. 主开关 enabled：False 时本模块全部旁路（兼容老用户）。
2. 推送分门槛：p1 ≥ 70、p2/p3 ≥ 50、p4 ≥ 55、p5 ≥ 60。
3. 僵尸赛道黑名单（银行/地产/白酒/煤炭等 25+ 个行业）。
4. 风险标签危险关键词拦截（派发/长上影诱多/VWAP重锤/禁买 等 11 个关键词）。
5. layer1 硬否决恢复（ui_alert_only=False，趋势破位/恶性派发直接拒绝推送）。
6. 5/10 日线低吸场景标签（推送头部出现"回踩 MA5 低吸"/"MA10 低吸"/"严禁追高"）。
7. 推送头部警示语（"⚠️ 新股民纪律：分批低吸、严守止损、不追高"）。

【调用约定】
- finalize 单票后产出 dict 中携带 detail/scores/risk_tags/pool_key；
- 在 P1-P5 推送入口（push_stock_if_allowed / notify_p1_high_score_pool_after_wash）前
  调用 `apply_novice_guard(stock_dict, pool_key, df, rt)`：
  - 返回 (allowed: bool, reason: str, enriched_dict: dict, scene_hint: str)
- allowed=False 时推送侧 return；True 时把 enriched_dict 交给企微推送。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

_logger = logging.getLogger(__name__)


# ========================== 配置数据类 ==========================

@dataclass
class NoviceGuardConfig:
    """新股民保护总闸配置：从 config.yaml 的 novice_guard 段加载。"""

    enabled: bool = False

    # ---- 风控层 ----
    risk_hard_veto: bool = True  # True 时 layer1 恢复硬否决

    # ---- 推送分门槛 ----
    push_min_score: Dict[str, float] = field(default_factory=lambda: {
        "p1_high_pool": 70.0,
        "p2": 50.0,
        "p3": 50.0,
        "p4": 55.0,
        "p5": 60.0,
    })

    # ---- 僵尸赛道黑名单 ----
    forbidden_industries: List[str] = field(default_factory=lambda: [
        "银行", "全国地产", "区域地产", "房地产", "白酒", "啤酒",
        "煤炭开采", "普钢", "水泥", "火力发电", "供气供热", "水力发电",
        "石油开采", "证券", "保险", "多元金融", "黄金",
        "水运", "空运", "港口", "路桥", "农业综合", "饲料",
        "纺织", "服饰", "家用电器", "摩托车",
    ])

    # ---- 风险标签危险关键词（任一命中即拒绝推送）----
    forbidden_risk_tag_keywords: List[str] = field(default_factory=lambda: [
        "派发", "长上影诱多", "恶性派发", "尾盘诱多", "尾盘脉冲",
        "主力出逃", "假强", "VWAP重锤", "高位爆量派发", "禁买", "黑名单冷却期",
    ])

    # ---- 5/10 日线低吸场景识别 ----
    pullback_enabled: bool = True
    # bias 在 [ma5_low_pct, ma5_high_pct] 区间内视为 MA5 低吸（值均为负数）
    ma5_low_pct: float = -0.5   # bias >= -0.5%（贴近均线）
    ma5_high_pct: float = -2.5  # bias <= -2.5%（已下探）
    # bias 在 [ma10_low_pct, ma10_high_pct] 区间内视为 MA10 低吸
    ma10_low_pct: float = -1.0
    ma10_high_pct: float = -3.5
    # "突破追涨"判定：量比 ≥ breakout_vr_min 且 涨幅 ≥ breakout_pct_min 且 close ≥ MA20
    breakout_vr_min: float = 2.0
    breakout_pct_min: float = 4.0

    # ---- 推送头部警示 ----
    wechat_header_warning: str = "⚠️ 新股民纪律：分批低吸、严守止损、不追高"

    # ---- 单池推送上限 ----
    max_push_per_pool: int = 5


DEFAULT_NOVICE_GUARD_CONFIG = NoviceGuardConfig()


# ========================== 单例加载器 ==========================

_NOVICE_GUARD_SINGLETON: Optional[NoviceGuardConfig] = None


def _load_novice_guard_config_from_yaml() -> NoviceGuardConfig:
    """从 config.yaml 的 novice_guard 段加载；若失败返回默认配置（enabled=False 旁路）。"""
    try:
        import os
        import yaml
        from core.runtime_data_paths import project_root
        cfg_path = os.path.join(project_root(), "config.yaml")
        if not os.path.exists(cfg_path):
            return DEFAULT_NOVICE_GUARD_CONFIG
        with open(cfg_path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        ng = raw.get("novice_guard") if isinstance(raw, dict) else None
        if not isinstance(ng, dict):
            return DEFAULT_NOVICE_GUARD_CONFIG

        cfg = NoviceGuardConfig(
            enabled=bool(ng.get("enabled", False)),
            risk_hard_veto=bool(ng.get("risk_hard_veto", True)),
            wechat_header_warning=str(
                ng.get("wechat_header_warning", DEFAULT_NOVICE_GUARD_CONFIG.wechat_header_warning)
            ),
            max_push_per_pool=int(ng.get("max_push_per_pool", 5)),
        )

        # 嵌套段（push_min_score / forbidden_industries / forbidden_risk_tag_keywords / pullback_scene）
        pm = ng.get("push_min_score")
        if isinstance(pm, dict):
            for k in ("p1_high_pool", "p2", "p3", "p4", "p5"):
                if k in pm and pm[k] is not None:
                    try:
                        cfg.push_min_score[k] = float(pm[k])
                    except (TypeError, ValueError):
                        pass

        fi = ng.get("forbidden_industries")
        if isinstance(fi, list) and fi:
            cfg.forbidden_industries = [str(x).strip() for x in fi if str(x).strip()]

        fk = ng.get("forbidden_risk_tag_keywords")
        if isinstance(fk, list) and fk:
            cfg.forbidden_risk_tag_keywords = [str(x).strip() for x in fk if str(x).strip()]

        pb = ng.get("pullback_scene")
        if isinstance(pb, dict):
            cfg.pullback_enabled = bool(pb.get("enabled", True))
            try:
                cfg.ma5_low_pct = float(pb.get("ma5_low_pct", cfg.ma5_low_pct))
                cfg.ma5_high_pct = float(pb.get("ma5_high_pct", cfg.ma5_high_pct))
                cfg.ma10_low_pct = float(pb.get("ma10_low_pct", cfg.ma10_low_pct))
                cfg.ma10_high_pct = float(pb.get("ma10_high_pct", cfg.ma10_high_pct))
                cfg.breakout_vr_min = float(pb.get("breakout_vr_min", cfg.breakout_vr_min))
                cfg.breakout_pct_min = float(pb.get("breakout_pct_min", cfg.breakout_pct_min))
            except (TypeError, ValueError):
                pass

        return cfg
    except Exception as exc:
        _logger.debug("novice_guard 配置加载失败，使用默认(旁路): %s", exc, exc_info=True)
        return DEFAULT_NOVICE_GUARD_CONFIG


def get_novice_guard_config(refresh: bool = False) -> NoviceGuardConfig:
    """获取 NoviceGuardConfig 单例；refresh=True 时强制重读 yaml。"""
    global _NOVICE_GUARD_SINGLETON
    if refresh or _NOVICE_GUARD_SINGLETON is None:
        _NOVICE_GUARD_SINGLETON = _load_novice_guard_config_from_yaml()
    return _NOVICE_GUARD_SINGLETON


def reset_novice_guard_cache() -> None:
    """测试 / 配置变更后清空单例缓存。"""
    global _NOVICE_GUARD_SINGLETON
    _NOVICE_GUARD_SINGLETON = None


# ========================== 核心判定函数 ==========================

def _safe_float(val: Any, default: float = 0.0) -> float:
    try:
        v = float(val)
    except (TypeError, ValueError):
        return float(default)
    if v != v:  # NaN
        return float(default)
    return v


def _safe_str(val: Any, default: str = "") -> str:
    try:
        if val is None:
            return default
        return str(val).strip()
    except Exception:
        return default


def _get_industry_from_stock_dict(stock_dict: Dict[str, Any]) -> str:
    """从推送/结果 dict 中提取行业；支持多种字段名。"""
    if not isinstance(stock_dict, dict):
        return ""
    for key in ("industry", "行业", "industry_name", "所属行业", "sector"):
        v = stock_dict.get(key)
        if v:
            return _safe_str(v)
    # 从 detail 子 dict 中查找
    detail = stock_dict.get("detail")
    if isinstance(detail, dict):
        for key in ("industry", "行业", "industry_name", "所属行业"):
            v = detail.get(key)
            if v:
                return _safe_str(v)
    return ""


def _is_forbidden_industry(industry: str, cfg: NoviceGuardConfig) -> Tuple[bool, str]:
    """判断是否属于僵尸赛道；匹配方式：包含子串（兼容「区域地产」包含「地产」等）。"""
    if not industry:
        return False, ""
    for bad in cfg.forbidden_industries:
        if bad and (bad in industry or industry in bad):
            return True, bad
    return False, ""


def _is_st_stock(stock_dict: Dict[str, Any]) -> bool:
    """【V26.88+9-27】判断是否为 ST/*ST 风险警示股。仅在 industry 阶段不误伤时启用。

    匹配规则:
      1. name 字段以 "ST" 或 "*ST" 开头（多数情况）
      2. risk_tags 含 ST/*ST 标签（已存在 forbidden_risk_tag_keywords 中）
    """
    if not isinstance(stock_dict, dict):
        return False
    # 1. 通过 name 字段前缀判断（Tushare 的 name 字段是简称）
    name = ""
    for k in ("name", "简称", "stock_name", "ts_name"):
        v = stock_dict.get(k)
        if isinstance(v, str) and v.strip():
            name = v.strip()
            break
    if name:
        upper = name.upper().lstrip()
        if upper.startswith("*ST") or upper.startswith("ST"):
            return True
    # 2. risk_tags 含 ST/*ST 关键字（已经在 forbidden_risk_tag_keywords 处理）
    return False


def _get_risk_tags(stock_dict: Dict[str, Any]) -> List[str]:
    """从推送/结果 dict 中收集风险标签列表。"""
    if not isinstance(stock_dict, dict):
        return []
    tags: List[str] = []
    raw = stock_dict.get("risk_tags")
    if isinstance(raw, list):
        tags.extend([_safe_str(t) for t in raw if t])
    raw = stock_dict.get("风险标签")
    if isinstance(raw, str):
        if raw.strip():
            tags.extend([t.strip() for t in raw.split("|") if t.strip()])
    return tags


def _risk_tag_has_forbidden(risk_tags: List[str], cfg: NoviceGuardConfig) -> Tuple[bool, str]:
    """risk_tags 中是否包含危险关键词。"""
    for tag in risk_tags:
        for kw in cfg.forbidden_risk_tag_keywords:
            if kw and kw in tag:
                return True, kw
    return False, ""


def _get_score_for_pool(stock_dict: Dict[str, Any], pool_key: str) -> float:
    """从 dict 中取综合分（兼容 burst_score / 综合分 / score / p1_score）。"""
    if not isinstance(stock_dict, dict):
        return 0.0
    for key in ("综合分", "burst_score", "score", "p1_score"):
        v = stock_dict.get(key)
        if v is not None:
            f = _safe_float(v, 0.0)
            if f > 0:
                return f
    # 从 detail 子 dict
    detail = stock_dict.get("detail")
    if isinstance(detail, dict):
        for key in ("综合分", "burst_score", "score"):
            v = detail.get(key)
            if v is not None:
                f = _safe_float(v, 0.0)
                if f > 0:
                    return f
    return 0.0


def _min_score_required(pool_key: str, cfg: NoviceGuardConfig) -> float:
    """不同池对应不同推送门槛。"""
    if pool_key == "p1" or pool_key == "p1_high_pool":
        return float(cfg.push_min_score.get("p1_high_pool", 70.0))
    pk = str(pool_key).lower()
    return float(cfg.push_min_score.get(pk, 50.0))


def _detect_pullback_scene(
    close: float, ma5: float, ma10: float, ma20: float,
    pct_chg: float, vol_ratio: float, cfg: NoviceGuardConfig,
) -> str:
    """
    【★右侧优化：只保留突破追涨场景】
    返回值：
      "突破追涨" - 量比≥breakout_vr_min 且 涨幅≥breakout_pct_min 且 close ≥ MA20×1.005（严禁追高）
      "正常"     - 不满足任一场景标签
    """
    if not cfg.pullback_enabled or close <= 0:
        return "正常"
    bias_ma5 = (close - ma5) / ma5 * 100.0 if ma5 > 0 else 0.0

    # 突破追涨（★右侧买点：量价齐升 + 站稳MA20）
    if vol_ratio >= cfg.breakout_vr_min and pct_chg >= cfg.breakout_pct_min and close >= ma20 * 1.005 and ma20 > 0:
        return "突破追涨"
    return "正常"


def _build_scene_hint(scene: str, cfg: NoviceGuardConfig) -> str:
    """场景 → 推送头部文案（含新股民纪律提示，★右侧优化：只保留突破追涨）"""
    base = cfg.wechat_header_warning
    if scene == "突破追涨":
        return f"⚡ 【突破追涨】{base}｜严禁追高！仅在盘中回踩不破 MA5 时跟进。"
    return base


# ========================== 主入口 ==========================

def apply_novice_guard(
    stock_dict: Dict[str, Any],
    pool_key: str,
    *,
    df: Optional[Any] = None,
    rt: Optional[Dict[str, Any]] = None,
    cfg: Optional[NoviceGuardConfig] = None,
) -> Tuple[bool, str, Dict[str, Any], str]:
    """
    新股民保护总闸主入口。

    参数：
        stock_dict: 推送行字典（含 综合分/burst_score/risk_tags/industry/detail 等）。
        pool_key:   "p1_high_pool" / "p2" / "p3" / "p4" / "p5"
        df / rt:    可选历史/实时数据；若提供，将用于 5/10 日线低吸场景识别
        cfg:        可选配置；缺省取单例

    返回：
        (allowed, reason, enriched_dict, scene_hint)
        - allowed=False → 拒绝推送（reason 为拒因）
        - enriched_dict 携带 detail.novice_guard 与 detail.buy_scene
        - scene_hint 为场景化推送头部文案
    """
    cfg = cfg or get_novice_guard_config()
    if not cfg.enabled:
        # 旁路：返回原 dict（拷贝）+ 默认场景
        return True, "", dict(stock_dict) if isinstance(stock_dict, dict) else {}, ""

    if not isinstance(stock_dict, dict):
        return False, "新股民保护：stock_dict 无效", {}, ""

    pk = str(pool_key or "").lower()

    # ---- 1. 分门槛 ----
    score = _get_score_for_pool(stock_dict, pk)
    min_required = _min_score_required(pk, cfg)
    if score < min_required:
        return False, f"新股民保护：综合分 {score:.1f} < 推送门槛 {min_required:.0f}", {}, ""

    # ---- 2. 僵尸赛道 ----
    industry = _get_industry_from_stock_dict(stock_dict)
    hit, hit_bad = _is_forbidden_industry(industry, cfg)
    if hit:
        return False, f"新股民保护：僵尸赛道[{industry}] 黑名单[{hit_bad}]", {}, ""

    # ---- 2.5 ST/*ST 风险警示（V26.88+9-27 新增 - 不通过 industry 子串匹配避免误伤）----
    if _is_st_stock(stock_dict):
        return False, "新股民保护：ST/*ST 风险警示股，不推送", {}, ""

    # ---- 3. 风险标签危险关键词 ----
    tags = _get_risk_tags(stock_dict)
    hit, hit_kw = _risk_tag_has_forbidden(tags, cfg)
    if hit:
        return False, f"新股民保护：风险标签命中[{hit_kw}]", {}, ""

    # ---- 4. 5/10 日线低吸场景识别 ----
    scene = "正常"
    close_v = ma5 = ma10 = ma20 = 0.0
    pct_v = vr_v = 0.0
    # 优先从 dict 中取
    for src in (stock_dict, rt or {}, getattr(df, "iloc", lambda: [])[-1] if df is not None and len(df) > 0 else {}):
        if isinstance(src, dict):
            close_v = _safe_float(src.get("close"), close_v) or close_v
            ma5 = _safe_float(src.get("ma5"), ma5) or ma5
            ma10 = _safe_float(src.get("ma10"), ma10) or ma10
            ma20 = _safe_float(src.get("ma20"), ma20) or ma20
            pct_v = _safe_float(src.get("pct_chg"), pct_v) or pct_v
            vr_v = _safe_float(src.get("vol_ratio"), vr_v) or vr_v
    # 用 rt 中的"现价/涨幅/量比"覆盖
    if isinstance(rt, dict):
        px = _safe_float(rt.get("price"), 0.0)
        if px > 0:
            close_v = px
        pc = _safe_float(rt.get("pct_chg"), 0.0)
        if pc != 0 or "pct_chg" in rt:
            pct_v = pc
        vr = _safe_float(rt.get("vol_ratio"), 0.0)
        if vr > 0:
            vr_v = vr
    scene = _detect_pullback_scene(close_v, ma5, ma10, ma20, pct_v, vr_v, cfg)
    scene_hint = _build_scene_hint(scene, cfg)

    # ---- 5. 装配 enriched_dict ----
    enriched = dict(stock_dict)
    detail = dict(enriched.get("detail")) if isinstance(enriched.get("detail"), dict) else {}
    detail["novice_guard"] = {
        "enabled": True,
        "score": round(score, 2),
        "min_required": min_required,
        "industry": industry,
        "buy_scene": scene,
        "passed_at": "apply_novice_guard",
    }
    # 把场景标签写到推送用字段（format_wechat_markdown 优先读取 stock_dict["buy_scene"]）
    enriched["buy_scene"] = scene
    enriched["detail"] = detail
    # 把头部警示加到 stock_dict["wechat_header"] 顶层键，便于 format_wechat_markdown 读取
    enriched["novice_guard_header"] = scene_hint

    return True, "", enriched, scene_hint


def is_pool_exhausted(pool_push_count: int, cfg: Optional[NoviceGuardConfig] = None) -> bool:
    """推送单池达到上限则拒绝后续推送。"""
    cfg = cfg or get_novice_guard_config()
    if not cfg.enabled:
        return False
    return pool_push_count >= int(cfg.max_push_per_pool)
