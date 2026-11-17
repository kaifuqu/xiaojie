# -*- coding: utf-8 -*-
"""
core/notification/notifiers.py
原 core/notification_gateway.py L2038-L2670（外部推送入口）

【职责】
- notify_wechat_system_alert：系统/运维告警（默认走 secondary webhook）
- clear_wechat_push_cache_global：清空主网关防刷字典
- notify_scan_results_top3_p2p4：P2/P3/P4/P5 主池+观察池 Top3/10 推送
- notify_scan_results_top3_p2p3p4：旧名兼容
- P1 高分池推送系列：
  - _safe_float_p1 / _format_net_main_amount_display
  - _build_merged_field_map / _format_plate_and_concept / _extract_net_main_yuan
  - _eastmoney_pc_quote_url / _guess_ts_code_full / _flatten_merged_stock_name_raw
  - _resolve_p1_push_display_name
  - _fill_push_row_from_merged / _p1_base_item_to_push_row / _flat_record_to_push_row
  - normalize_p1_high_score_source / format_p1_high_score_pool_markdown
  - notify_p1_high_score_pool_after_wash（洗盘后高分池推送入口）

【依赖】
- core.notification.core（_bj_now_str / _esc / _finite_float_or_none / _format_system_alert_body / _gateway_lock / _gateway_singleton）
- core.notification.gateway（get_wechat_gateway / get_wechat_secondary_gateway / WechatNotificationGateway）
- core.notification.format（_safe_score_display）
- core.notification.deepseek（_maybe_get_deepseek_advice / _append_deepseek_advice）
- core.stock_name_utils.normalize_stock_display_name
- core.master_control / core.config_manager（容错导入）
"""
import hashlib
import logging
import math
from datetime import datetime
from typing import Any, Dict, List, Optional, Union

from core.notification.core import (
    _MAX_MARKDOWN_CONTENT_LEN,
    _bj_now_str,
    _esc,
    _finite_float_or_none,
    _format_system_alert_body,
    _gateway_lock,
    _gateway_singleton,
    _norm_ts_code,
)
from core.stock_name_utils import normalize_stock_display_name

logger = logging.getLogger(__name__)


def notify_wechat_system_alert(
    *,
    title: str,
    detail: str = "",
    category: str = "system",
    dedup_key: Optional[str] = None,
    dedup_window_sec: Optional[float] = None,
) -> None:
    """数据下载失败、P1～P5 扫描异常等运维温馨提示；默认走 secondary webhook。
    如 secondary 未配置，则回退 primary，避免消息完全丢失。"""
    try:
        from core.master_control import is_wechat_system_alert_enabled

        if not is_wechat_system_alert_enabled():
            return
        from core.config_manager import get_notification_config

        cfg = get_notification_config()
        url = str(cfg.get("wechat_webhook_url_secondary") or "").strip() or str(cfg.get("wechat_webhook_url") or "").strip()
        if not url:
            return
        if dedup_key is None:
            _canon = f"{str(category).strip()}:{str(title).strip()}"[:800]
            dedup_key = hashlib.sha256(_canon.encode("utf-8", errors="ignore")).hexdigest()[:24]
        body = _format_system_alert_body(title, detail or "", category)
        from core.notification.gateway import get_wechat_secondary_gateway
        gw = get_wechat_secondary_gateway(url)
        gw.push_system_alert_async(body, dedup_key, dedup_window_sec=dedup_window_sec)
    except Exception as e:
        logger.debug("notify_wechat_system_alert 跳过: %s", e)


def clear_wechat_push_cache_global() -> None:
    """清空当前进程已创建的网关单例内的防刷字典。"""
    global _gateway_singleton
    with _gateway_lock:
        if _gateway_singleton is not None:
            _gateway_singleton.clear_push_cache()


def notify_scan_results_top3_p2p4(
    scan_targets: List[str],
    res_dict: Dict[str, Any],
    ui_push_enabled: bool,
) -> None:
    """在扫描成功后由 UI / 异步桥调用：P2/P3/P4/P5 主池 + 观察池均推送"""
    try:
        from core.config_manager import get_notification_config
    except Exception as e:
        logger.debug("notify_scan_results_top3: 无法导入 config: %s", e)
        return

    cfg = get_notification_config()
    if not ui_push_enabled or not cfg.get("enabled"):
        return
    try:
        from core.master_control import is_wechat_push_master_enabled

        if not is_wechat_push_master_enabled():
            logger.debug("notify_scan_results_top3：总控企微推送关闭，跳过")
            return
    except Exception as e:
        logger.debug("notify_scan_results_top3：master_control 读取异常，继续: %s", e)
    url = str(cfg.get("wechat_webhook_url") or "").strip()
    if not url:
        return

    from core.notification.gateway import get_wechat_gateway
    gw = get_wechat_gateway(url)
    targets = {str(x).lower() for x in (scan_targets or [])}
    obs_root = res_dict.get("observation") or {}
    if not isinstance(obs_root, dict):
        obs_root = {}

    def _score(row: Dict[str, Any]) -> float:
        x = _finite_float_or_none(row.get("综合分", 0.0))
        return x if x is not None else 0.0

    for pk in ("p2", "p3", "p4", "p5"):
        if pk not in targets:
            continue
        top_n = 10 if pk == "p5" else 3
        rows = res_dict.get(pk)
        if isinstance(rows, list) and rows:
            sorted_rows = sorted(rows, key=_score, reverse=True)
            for row in sorted_rows[: max(1, top_n)]:
                if not isinstance(row, dict):
                    continue
                try:
                    gw.push_stock_if_allowed(pk, row)
                except Exception as e:
                    logger.debug("push_stock_if_allowed 异常: %s", e, exc_info=True)

        obs_rows = obs_root.get(pk)
        if isinstance(obs_rows, list) and obs_rows:
            sorted_obs = sorted(obs_rows, key=_score, reverse=True)
            for row in sorted_obs[: max(1, top_n)]:
                if not isinstance(row, dict):
                    continue
                try:
                    gw.push_stock_if_allowed(pk, row, pool_key_for_dedup=f"{pk}_obs")
                except Exception as e:
                    logger.debug("push_stock_if_allowed(obs) 异常: %s", e, exc_info=True)


def notify_scan_results_top3_p2p3p4(
    scan_targets: List[str],
    res_dict: Dict[str, Any],
    ui_push_enabled: bool,
) -> None:
    """向后兼容旧调用名，内部转发到 notify_scan_results_top3_p2p4"""
    return notify_scan_results_top3_p2p4(scan_targets, res_dict, ui_push_enabled)


# ============ P1 高分池推送系列 ============
def _safe_float_p1(val: Any, default: float = 0.0) -> float:
    """从 P1 底仓项 hist 中解析浮点；不依赖 pandas，供高分池推送专用。"""
    if val is None:
        return default
    try:
        if isinstance(val, float) and (math.isnan(val) or math.isinf(val)):
            return default
        s = str(val).strip()
        if s in ("", "-", "None", "nan"):
            return default
        return float(val)
    except (TypeError, ValueError):
        return default


def _format_net_main_amount_display(yuan: Any) -> str:
    """底层 net_main_amount 单位为「元」：展示时换算为「万」或「亿」"""
    x = _finite_float_or_none(yuan)
    if x is None:
        return "暂无"
    ax = abs(x)
    if ax < 1e-9:
        return "0.00万"
    neg = x < 0
    if ax >= 1e8:
        s = f"{ax / 1e8:.2f}亿"
    elif ax >= 1e4:
        s = f"{ax / 1e4:.2f}万"
    else:
        s = f"{ax / 1e4:.2f}万"
    return ("-" if neg else "") + s


def _build_merged_field_map(item: Dict[str, Any]) -> Dict[str, Any]:
    """合并 hist 与 df 最后一行字段，便于统一用 .get 提取"""
    merged: Dict[str, Any] = {}
    hist = item.get("hist")
    if isinstance(hist, dict):
        merged.update(hist)
    df_obj = item.get("df")
    try:
        import pandas as pd

        if isinstance(df_obj, pd.DataFrame) and not df_obj.empty:
            last = df_obj.iloc[-1]
            ld = last.to_dict()
            for k, v in ld.items():
                old = merged.get(k)
                if old is None or (isinstance(old, str) and str(old).strip() in ("", "nan", "--")):
                    try:
                        if pd.isna(v):
                            continue
                    except Exception:
                        pass
                    merged[k] = v
    except Exception:
        pass
    return merged


def _format_plate_and_concept(merged: Dict[str, Any], ts_code_full: Optional[str]) -> str:
    """安全拼接申万行业（库表）与概念/板块类字段"""
    pieces: List[str] = []
    seen = set()

    def _add_one(val: Any) -> None:
        if val is None:
            return
        try:
            import pandas as pd

            if pd.isna(val):
                return
        except Exception:
            pass
        s = str(val).strip()
        if not s or s.lower() in ("nan", "none", "--", "未知"):
            return
        for part in s.replace("，", ",").split(","):
            p = part.strip()
            if p and p not in seen:
                seen.add(p)
                pieces.append(p)

    if ts_code_full:
        try:
            from data.db_core import get_stock_industry

            ind = get_stock_industry(str(ts_code_full).strip())
            _add_one(ind)
        except Exception:
            pass

    for key in (
        "industry", "concept", "concepts", "概念",
        "概念板块", "leading_concept", "sector_concept",
        "industry_name", "bk_name", "板块",
    ):
        raw = merged.get(key, None)
        if raw is not None:
            _add_one(raw)

    if not pieces:
        return "暂无"
    return ",".join(pieces[:16])


def _extract_net_main_yuan(merged: Dict[str, Any]) -> Any:
    """从合并字段中安全取主力净额（元）"""
    if not isinstance(merged, dict):
        return None
    for key in (
        "net_main_amount",
        "主力净额",
        "net_main",
        "main_net_amount",
        "main_net",
    ):
        if key in merged:
            return merged.get(key)
    return None


def _eastmoney_pc_quote_url(ts_code_raw: Any) -> str:
    """将 ts_code（如 600000.SH）转为东方财富 PC 行情页链接"""
    s = str(ts_code_raw or "").strip().upper()
    if not s:
        return "https://www.eastmoney.com/"
    if "." in s:
        num, suf = s.split(".", 1)
        num = num[:6].zfill(6)
        suf = suf.strip()
    else:
        num = s[:6].zfill(6)
        if num.startswith("6"):
            suf = "SH"
        elif num.startswith(("8", "4")):
            suf = "BJ"
        else:
            suf = "SZ"
    if suf in ("SH", "SSE"):
        return f"https://quote.eastmoney.com/sh{num}.html"
    if suf in ("SZ", "SHE"):
        return f"https://quote.eastmoney.com/sz{num}.html"
    if suf in ("BJ", "BSE"):
        return f"https://quote.eastmoney.com/bj{num}.html"
    return f"https://quote.eastmoney.com/sz{num}.html"


def _guess_ts_code_full(code_str: str, s_code: str) -> str:
    """6 位代码补全为 Tushare 风格 ts_code，供 stock_basic 查简称。"""
    t = str(code_str or "").strip()
    if "." in t:
        return t.upper()
    sc = (s_code or "")[:6].zfill(6)
    if sc.startswith("6"):
        return f"{sc}.SH"
    if sc.startswith(("0", "3")):
        return f"{sc}.SZ"
    if sc.startswith(("8", "4")):
        return f"{sc}.BJ"
    return f"{sc}.SZ"


def _flatten_merged_stock_name_raw(merged: Dict[str, Any]) -> str:
    """从 hist/df 合并字段取证券简称；纯 6 位数字视为无效"""
    for key in ("name", "名称", "stock_name", "证券简称", "股票简称"):
        v = merged.get(key)
        if v is None:
            continue
        try:
            import pandas as pd

            if pd.isna(v):
                continue
        except Exception:
            pass
        t = str(v).strip()
        if not t or t.lower() in ("nan", "none", "--", "null"):
            continue
        if len(t) == 6 and t.isdigit():
            continue
        return t
    return ""


def _resolve_p1_push_display_name(merged: Dict[str, Any], code_str: str, s_code: str) -> str:
    """P1 企微推送用简称：daily_data 末行常无 name"""
    _nm_raw = _flatten_merged_stock_name_raw(merged)
    name = normalize_stock_display_name(_nm_raw or s_code)
    looks_like_code_only = not _nm_raw or _nm_raw == s_code or name == s_code
    if not looks_like_code_only:
        return name
    tc = str(merged.get("ts_code") or "").strip()
    if not tc:
        tc = _guess_ts_code_full(code_str, s_code)
    try:
        from data.db_core import map_ts_codes_to_names_local

        m = map_ts_codes_to_names_local([tc])
        got = m.get(tc.upper().strip())
        if got and str(got).strip() and str(got).strip() != s_code:
            return normalize_stock_display_name(got)
    except Exception as e:
        logger.debug("P1 推送简称补全(map_ts_codes_to_names_local): %s", e)
    return name


def _fill_push_row_from_merged(
    merged: Dict[str, Any],
    code_str: str,
    p1_score_override: Optional[float],
) -> Optional[Dict[str, Any]]:
    """由合并字段生成推送行：现价/涨幅/综合分/板块概念/主力展示。"""
    if not code_str:
        return None
    s_code = str(code_str).split(".")[0][:6]
    if len(s_code) < 6:
        s_code = s_code.zfill(6)[:6]
    name = _resolve_p1_push_display_name(merged, code_str, s_code)
    db_close_p = _safe_float_p1(merged.get("close", 0.0), 0.0)
    pre_c = _safe_float_p1(merged.get("pre_close"), 0.0)
    if pre_c <= 0:
        pre_c = _safe_float_p1(merged.get("close"), 0.0)
    pct = (db_close_p - pre_c) / pre_c * 100.0 if pre_c > 0 else 0.0

    if p1_score_override is not None:
        score = float(p1_score_override)
    else:
        sc = _finite_float_or_none(merged.get("p1_score", merged.get("综合分", 0.0)))
        score = float(sc) if sc is not None else 0.0

    nm_raw = _extract_net_main_yuan(merged)
    main_force_display = _format_net_main_amount_display(nm_raw)
    ts_full = str(merged.get("ts_code") or code_str or "").strip()
    if not ts_full or "." not in ts_full:
        ts_full = _guess_ts_code_full(code_str, s_code)
    plate_display = _format_plate_and_concept(merged, ts_full)

    return {
        "代码": s_code,
        "名称": name,
        "ts_code": ts_full,
        "综合分": score,
        "现价展示": f"{db_close_p:.2f}",
        "涨幅展示": f"{pct:.2f}%",
        "板块概念展示": plate_display,
        "主力展示": main_force_display,
    }


def _p1_base_item_to_push_row(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """将 build_p1_pool_and_cache 产出的单条底仓项转为推送用行"""
    if not isinstance(item, dict):
        return None
    code = item.get("code")
    if not code:
        return None
    merged = _build_merged_field_map(item)
    sc = _finite_float_or_none(item.get("p1_score", 0.0))
    p1_override = float(sc) if sc is not None else None
    return _fill_push_row_from_merged(merged, str(code).strip(), p1_override)


def _flat_record_to_push_row(rec: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """将扁平 dict（如 DataFrame 行）转为推送行"""
    if not isinstance(rec, dict):
        return None
    code = rec.get("ts_code") or rec.get("code") or rec.get("代码")
    if not code:
        return None
    merged = dict(rec)
    sc = _finite_float_or_none(rec.get("p1_score", rec.get("综合分")))
    p1_override = float(sc) if sc is not None else None
    return _fill_push_row_from_merged(merged, str(code).strip(), p1_override)


def normalize_p1_high_score_source(src: Any) -> List[Dict[str, Any]]:
    """将 list[底仓项] 或 pandas.DataFrame 规范为推送行列表"""
    if src is None:
        return []
    try:
        import pandas as pd

        if isinstance(src, pd.DataFrame):
            if src.empty:
                return []
            out: List[Dict[str, Any]] = []
            for _, row in src.iterrows():
                try:
                    d = row.to_dict()
                except Exception:
                    continue
                pr = _flat_record_to_push_row(d)
                if pr:
                    out.append(pr)
            return out
    except Exception:
        pass

    if isinstance(src, list):
        out2: List[Dict[str, Any]] = []
        for it in src:
            if not isinstance(it, dict):
                continue
            if it.get("hist") is not None or (
                it.get("df") is not None and it.get("code")
            ):
                r = _p1_base_item_to_push_row(it)
            else:
                r = _flat_record_to_push_row(it)
            if r:
                out2.append(r)
        return out2
    return []


def format_p1_high_score_pool_markdown(
    rows: List[Dict[str, Any]],
    total_ge_60: int,
    shown: int,
    *,
    total_main: int = 0,
    total_backup: int = 0,
    main_score: float = 75.0,
) -> str:
    """组装「P1 核心战略底仓 (高分池)」企微 markdown 正文"""
    lines: List[str] = []
    title = f"## V26.88 🌙 P1 底仓池 [{_bj_now_str('%H:%M:%S')}]"
    lines.append(title)
    lines.append("")
    try:
        from zoneinfo import ZoneInfo

        now_str = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines.append(f'<font color="comment">{_esc(now_str)}</font>')
    lines.append("")
    for i, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            continue
        code_plain = _norm_ts_code(row.get("代码")) or str(row.get("代码", "") or "").strip() or "--"
        name_raw = normalize_stock_display_name(row.get("名称", "--"))
        link_label = _esc(f"{name_raw if name_raw is not None else '--'} ({code_plain})")
        url = _eastmoney_pc_quote_url(row.get("ts_code") or row.get("代码"))
        score_v = _finite_float_or_none(row.get("综合分", 0.0))
        score_txt = f"{float(score_v):.2f}" if score_v is not None else "--"
        price_txt = _esc(row.get("现价展示", "--"))
        pct_txt = _esc(row.get("涨幅展示", "--"))
        link_line = f"[{link_label}]({url})"
        if score_v is not None and score_v >= 85.0:
            tag = "主推"
        else:
            tag = "观察"
        lines.append(
            f"{i}. [{tag}] {link_line} - 综合分: {score_txt}分 | 现价: {price_txt} | 涨幅: {pct_txt}"
        )
        plate = _esc(str(row.get("板块概念展示", "暂无")))
        mainf = _esc(str(row.get("主力展示", "暂无")))
        lines.append(f"   └ 🏷️ 板块: {plate} | 💰 主力: {mainf}")
        # V29：风控执行卡（仓位 / 止损价 / 止盈价 / 最长持有 / 纪律底线）
        try:
            from core.notification.format import _render_risk_card

            _risk = _render_risk_card(row)
            if _risk:
                lines.append(_risk)
        except Exception as _e_rc:
            logger.debug("P1 风控卡渲染异常（忽略）: %s", _e_rc)
    lines.append("")
    lines.append(
        f"💡 提示：共发现 {int(total_ge_60)} 只 60分以上标的，其中主推 {int(total_main)} 只、备选 {int(total_backup)} 只；仅展示前 {int(shown)} 只。"
    )
    if int(shown) > 0:
        lines.append(
            "> 温馨提示：炒股有风险，决策需谨慎；本消息仅作策略信号参考，不构成投资建议。"
        )
    body = "\n".join(lines)
    if len(body) > _MAX_MARKDOWN_CONTENT_LEN:
        body = body[: _MAX_MARKDOWN_CONTENT_LEN - 20] + "\n…(已截断)"
    return body


def notify_p1_high_score_pool_after_wash(
    base_items: Union[List[Dict[str, Any]], Any],
    *,
    min_score: float = 60.0,
    main_score: float = 75.0,
    max_items: int = 10,
) -> None:
    """在 UI 完成「全量洗盘」且 P1 底仓列表已落盘之后调用"""
    try:
        from core.master_control import is_push_p1_high_score_enabled, is_wechat_push_master_enabled
    except Exception as e:
        logger.debug("notify_p1_high_score_pool: master_control 导入失败: %s", e)
        return

    if not is_push_p1_high_score_enabled():
        return
    if not is_wechat_push_master_enabled():
        logger.debug("notify_p1_high_score_pool：企微总闸关闭，跳过")
        return
    if base_items is None:
        return

    try:
        from core.config_manager import get_notification_config
    except Exception as e:
        logger.debug("notify_p1_high_score_pool: 无法导入 config: %s", e)
        return

    cfg = get_notification_config()
    if not cfg.get("enabled"):
        return
    url = str(cfg.get("wechat_webhook_url") or "").strip()
    if not url:
        return

    parsed = normalize_p1_high_score_source(base_items)
    if not parsed:
        return

    min_score = float(min_score)
    main_score = float(main_score)
    max_items = max(1, int(max_items))

    filtered = [r for r in parsed if float(r.get("综合分", 0.0)) >= min_score]
    if not filtered:
        return

    filtered.sort(key=lambda x: float(x.get("综合分", 0.0)), reverse=True)
    main_rows = [r for r in filtered if float(r.get("综合分", 0.0)) >= 85.0]
    backup_rows = [r for r in filtered if min_score <= float(r.get("综合分", 0.0)) < 85.0]
    display_rows = filtered[:15]
    shown = len(display_rows)
    total_ge_60 = len(filtered)

    # NoviceGuard 集成
    try:
        from core.strategies.novice_guard import apply_novice_guard
        _guarded = []
        for r in display_rows:
            sd = dict(r) if isinstance(r, dict) else {}
            allowed, reason, enriched, scene_hint = apply_novice_guard(sd, "p1_high_pool")
            if allowed:
                _guarded.append(enriched)
            else:
                logger.debug("P1 高分池 NoviceGuard 拒绝: %s | reason=%s", sd.get("ts_code") or sd.get("code"), reason)
        if not _guarded:
            logger.info("P1 高分池经 NoviceGuard 过滤后为空，跳过推送")
            return
        display_rows = _guarded
        main_rows = [r for r in display_rows if float(r.get("综合分", 0.0)) >= 85.0]
        backup_rows = [r for r in display_rows if min_score <= float(r.get("综合分", 0.0)) < 85.0]
        shown = len(display_rows)
    except Exception as e:
        logger.debug("NoviceGuard 集成失败（不影响推送主流程）: %s", e)

    try:
        body = format_p1_high_score_pool_markdown(
            display_rows,
            total_ge_60,
            shown,
            total_main=len(main_rows),
            total_backup=len(backup_rows),
            main_score=85.0,
        )
    except Exception as e:
        logger.warning("format_p1_high_score_pool_markdown 异常: %s", e, exc_info=True)
        return

    from core.notification.gateway import get_wechat_gateway
    gw = get_wechat_gateway(url)
    try:
        gw.push_markdown_async(body)
    except Exception as e:
        logger.debug("notify_p1_high_score_pool push_markdown_async: %s", e, exc_info=True)


__all__ = [
    "notify_wechat_system_alert",
    "clear_wechat_push_cache_global",
    "notify_scan_results_top3_p2p4",
    "notify_scan_results_top3_p2p3p4",
    "_safe_float_p1", "_format_net_main_amount_display",
    "_build_merged_field_map", "_format_plate_and_concept",
    "_extract_net_main_yuan",
    "_eastmoney_pc_quote_url", "_guess_ts_code_full",
    "_flatten_merged_stock_name_raw", "_resolve_p1_push_display_name",
    "_fill_push_row_from_merged", "_p1_base_item_to_push_row",
    "_flat_record_to_push_row",
    "normalize_p1_high_score_source", "format_p1_high_score_pool_markdown",
    "notify_p1_high_score_pool_after_wash",
]
