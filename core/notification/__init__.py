# -*- coding: utf-8 -*-
"""
core/notification/__init__.py
小杰AI选股系统 Pro V26.88 — 企微 Webhook 异步推送网关 包入口

【包结构】
- core.py：常量（_SPAM_WINDOW_SEC / _SYSTEM_ALERT_DEDUP_DEFAULT_SEC / _MAX_MARKDOWN_CONTENT_LEN /
  _REQUEST_TIMEOUT / _WEBHOOK_MAX_ATTEMPTS / _WEBHOOK_RETRY_SLEEP_SEC /
  _WEBHOOK_CIRCUIT_FAILURE_THRESHOLD / _WEBHOOK_CIRCUIT_OPEN_SEC / _SPAM_RECORDS_HARD_CAP）、
  工具函数（_file_reserve_system_alert_slot / _format_system_alert_body / _esc / _norm_ts_code /
  _bj_now_str / _finite_float_or_none）、线程池（_WECHAT_WEBHOOK_EXECUTOR）、
  去重字典（_pushed_records / _pushed_records_lock / _system_alert_dedup_lock）
- gateway.py：WechatNotificationGateway 类、get_wechat_gateway、get_wechat_secondary_gateway
- format.py：format_wechat_markdown（拼装股票推送 markdown）、_safe_score_display
- deepseek.py：DeepSeek V4.1-Flash 提示词与字段别名、_maybe_get_deepseek_advice、
  _append_deepseek_advice、_log_deepseek_analysis
- notifiers.py：外部入口（notify_wechat_system_alert / notify_scan_results_top3_p2p4 /
  notify_p1_high_score_pool_after_wash 等）

【向后兼容】
- 老代码 `from core.notification_gateway import ...` 走 core/notification_gateway.py（薄 shim）
- 新代码 `from core.notification import ...` 直接从本包导入
"""
from core.notification.core import (
    # 常量
    _SPAM_WINDOW_SEC,
    _SYSTEM_ALERT_DEDUP_DEFAULT_SEC,
    _MAX_MARKDOWN_CONTENT_LEN,
    _REQUEST_TIMEOUT,
    _WEBHOOK_MAX_ATTEMPTS,
    _WEBHOOK_RETRY_SLEEP_SEC,
    _WEBHOOK_CIRCUIT_FAILURE_THRESHOLD,
    _WEBHOOK_CIRCUIT_OPEN_SEC,
    _SPAM_RECORDS_HARD_CAP,
    # 线程池
    _WECHAT_WEBHOOK_EXECUTOR,
    _shutdown_wechat_executor,
    # 单例/锁
    _gateway_lock,
    _gateway_singleton,
    _gateway_secondary_singleton,
    _pushed_records,
    _pushed_records_lock,
    _system_alert_dedup_lock,
    # 工具函数
    _finite_float_or_none,
    _esc,
    _norm_ts_code,
    _bj_now_str,
    _format_system_alert_body,
    _file_reserve_system_alert_slot,
)
from core.notification.format import (
    format_wechat_markdown,
    _safe_score_display,
)
from core.notification.deepseek import (
    _DEEPSEEK_FALLBACK_ADVICE,
    _DEEPSEEK_SYSTEM_PROMPT,
    _DEEPSEEK_POOL_FOCUS,
    _DEEPSEEK_POOL_SEMANTICS,
    _DEEPSEEK_TACTIC_KNOWLEDGE,
    _DEEPSEEK_FIELD_ALIASES,
    _maybe_get_deepseek_advice,
    _append_deepseek_advice,
    _log_deepseek_analysis,
)
from core.notification.gateway import (
    WechatNotificationGateway,
    get_wechat_gateway,
    get_wechat_secondary_gateway,
)
from core.notification.notifiers import (
    notify_wechat_system_alert,
    clear_wechat_push_cache_global,
    notify_scan_results_top3_p2p4,
    notify_scan_results_top3_p2p3p4,
    _safe_float_p1,
    _format_net_main_amount_display,
    _build_merged_field_map,
    _format_plate_and_concept,
    _extract_net_main_yuan,
    _eastmoney_pc_quote_url,
    _guess_ts_code_full,
    _flatten_merged_stock_name_raw,
    _resolve_p1_push_display_name,
    _fill_push_row_from_merged,
    _p1_base_item_to_push_row,
    _flat_record_to_push_row,
    normalize_p1_high_score_source,
    format_p1_high_score_pool_markdown,
    notify_p1_high_score_pool_after_wash,
)

__all__ = [
    # 常量
    "_SPAM_WINDOW_SEC", "_SYSTEM_ALERT_DEDUP_DEFAULT_SEC", "_MAX_MARKDOWN_CONTENT_LEN",
    "_REQUEST_TIMEOUT", "_WEBHOOK_MAX_ATTEMPTS", "_WEBHOOK_RETRY_SLEEP_SEC",
    "_WEBHOOK_CIRCUIT_FAILURE_THRESHOLD", "_WEBHOOK_CIRCUIT_OPEN_SEC",
    "_SPAM_RECORDS_HARD_CAP",
    "_DEEPSEEK_FALLBACK_ADVICE", "_DEEPSEEK_SYSTEM_PROMPT",
    "_DEEPSEEK_POOL_FOCUS", "_DEEPSEEK_POOL_SEMANTICS", "_DEEPSEEK_TACTIC_KNOWLEDGE",
    "_DEEPSEEK_FIELD_ALIASES",
    # 线程池
    "_WECHAT_WEBHOOK_EXECUTOR", "_shutdown_wechat_executor",
    # 单例/锁
    "_gateway_lock", "_gateway_singleton", "_gateway_secondary_singleton",
    "_pushed_records", "_pushed_records_lock", "_system_alert_dedup_lock",
    # 工具函数
    "_finite_float_or_none", "_esc", "_norm_ts_code", "_bj_now_str",
    "_format_system_alert_body", "_file_reserve_system_alert_slot",
    "_safe_score_display",
    # DeepSeek
    "_maybe_get_deepseek_advice", "_append_deepseek_advice", "_log_deepseek_analysis",
    # 类与单例入口
    "WechatNotificationGateway", "get_wechat_gateway", "get_wechat_secondary_gateway",
    # 格式化
    "format_wechat_markdown",
    # 外部入口
    "notify_wechat_system_alert",
    "clear_wechat_push_cache_global",
    "notify_scan_results_top3_p2p4",
    "notify_scan_results_top3_p2p3p4",
    "notify_p1_high_score_pool_after_wash",
    # P1 推送处理
    "_safe_float_p1", "_format_net_main_amount_display",
    "_build_merged_field_map", "_format_plate_and_concept",
    "_extract_net_main_yuan",
    "_eastmoney_pc_quote_url", "_guess_ts_code_full",
    "_flatten_merged_stock_name_raw", "_resolve_p1_push_display_name",
    "_fill_push_row_from_merged", "_p1_base_item_to_push_row",
    "_flat_record_to_push_row",
    "normalize_p1_high_score_source", "format_p1_high_score_pool_markdown",
]