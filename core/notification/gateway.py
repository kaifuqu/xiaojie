# -*- coding: utf-8 -*-
"""
core/notification/gateway.py
原 core/notification_gateway.py L1529-L2018（WechatNotificationGateway 主类 + HTTP 发送）

【职责】
- WechatNotificationGateway：企微 Webhook 推送网关主类
  - __init__ / _reset_spam_if_new_calendar_day_bj / _prune_old_records / _shrink_if_oversized
  - clear_push_cache / _is_signal_pool_slot / _signal_pool_bucket
  - send_heartbeat / push_markdown_async / push_stock_if_allowed
  - _reserve_push_slot / _reserve_signal_daily_once_file / _reserve_system_alert_slot
  - _send_markdown_sync / _send_system_markdown_sync / push_system_alert_async
- get_wechat_gateway / get_wechat_secondary_gateway（单例工厂）
- _WEBHOOK_MAX_ATTEMPTS / _WEBHOOK_RETRY_SLEEP_SEC / _WEBHOOK_CIRCUIT_FAILURE_THRESHOLD
  / _WEBHOOK_CIRCUIT_OPEN_SEC / _SPAM_RECORDS_HARD_CAP 常量

【依赖】
- core.notification.core（_gateway_singleton/_gateway_lock/_bj_* 等）
- core.notification.format（format_wechat_markdown）
- core.notification.deepseek（_maybe_get_deepseek_advice / _append_deepseek_advice）
- core.master_control（is_wechat_push_master_enabled / is_wechat_system_alert_enabled）
- core.file_utils.atomic_json_update（容错导入）
- core.runtime_data_paths.path_wechat_signal_dedup_cache_json
"""
import hashlib
import logging
import os
import re
import threading
import time
from typing import Any, Dict, Optional

from core.notification.core import (
    _MAX_MARKDOWN_CONTENT_LEN,
    _REQUEST_TIMEOUT,
    _SPAM_RECORDS_HARD_CAP,
    _SPAM_WINDOW_SEC,
    _SYSTEM_ALERT_DEDUP_DEFAULT_SEC,
    _WECHAT_WEBHOOK_EXECUTOR,
    _WEBHOOK_CIRCUIT_FAILURE_THRESHOLD,
    _WEBHOOK_CIRCUIT_OPEN_SEC,
    _WEBHOOK_MAX_ATTEMPTS,
    _WEBHOOK_RETRY_SLEEP_SEC,
    _bj_calendar_key,
    _bj_now_str,
    _file_reserve_system_alert_slot,
    _gateway_lock,
    _gateway_secondary_singleton,
    _gateway_singleton,
    _norm_ts_code,
    _path_alert_dedup_cache_file,
    _strategy_dedup_segment,
    _strategy_log_fragment,
    _finite_float_or_none,
)

logger = logging.getLogger(__name__)

# 【V30.13 2026-11-05】内容级重复推送兜底窗口（秒）。
# 事故背景：09:05 的「战法榜早盘推送」因调度器不认完成状态，在 catchup 窗口内
# 每分钟重发一次同一条战法榜，当天实测重推 25+ 次（最坏 61 次）。
# 调度器已单独修复（ST-P0-31），但**推送层不应依赖调用方是否去重** ——
# 这里按内容指纹再兜一层：完全相同的 markdown 在窗口内只发一次。
#   · 只在**发送成功**后登记指纹，失败重试不受影响
#   · 窗口 10 分钟：能挡住「每分钟重复」这类抖动，又不妨碍当天不同时点的正常推送
#     （如战法榜 09:05 早盘版与 18:45 盘后版相隔 13 小时）
_CONTENT_DEDUP_WINDOW_SEC = float(os.getenv("XJ_PUSH_CONTENT_DEDUP_SEC", "600") or 600)


# ==================== P0-3 密钥卫生（2026-11-14）====================
_KEY_IN_URL_RE = re.compile(r"(key=)[0-9A-Za-z\-_]{8,}")


def _mask_webhook_url(url: object) -> str:
    """webhook URL 打码：日志/账本里永不出现明文 key。"""
    try:
        return _KEY_IN_URL_RE.sub(r"\1***", str(url or ""))
    except Exception:
        return "(masked)"


def _append_undelivered_alert(content: str, err: object) -> None:
    """系统告警全链路失败（代理 + 直连都挂）时落本地账本，「告警永不静默丢失」。

    背景（P0-2）：11-14 实测本机代理死亡后，企微告警与数据 API 一起失联，
    告警系统自己哑火 —— 等于断网期间运维完全失明。此账本是最后的本地观测通道，
    由 UI / system_health 巡检读出展示。
    """
    try:
        from core.file_utils import atomic_json_update

        try:
            from core.runtime_data_paths import RUNTIME_DIR

            path = os.environ.get("XJ_ALERT_LEDGER_PATH") or os.path.join(
                RUNTIME_DIR, "state", "alert_undelivered.json"
            )
        except Exception:
            path = os.environ.get("XJ_ALERT_LEDGER_PATH") or os.path.join(
                "data", "runtime", "state", "alert_undelivered.json"
            )

        def _upd(data: Dict[str, Any]) -> None:
            items = data.get("items")
            if not isinstance(items, list):
                items = []
            items.append({
                "ts": _bj_now_str("%Y-%m-%d %H:%M:%S"),
                "error": str(err)[:200],
                "content": str(content)[:2000],
            })
            data["items"] = items[-100:]

        atomic_json_update(path, _upd, timeout=8)
    except Exception as e:  # noqa: BLE001 — 账本失败绝不能影响推送主流程
        logger.warning("本地告警账本写入失败(忽略): %s", e)


class WechatNotificationGateway:
    """企微 Webhook 推送网关：有界线程池投递 _send_markdown_sync + 股票信号内存防刷去重 + requests 超时与重试。"""

    def __init__(self, webhook_url: str) -> None:
        self._url = (webhook_url or "").strip()
        self._pushed_records: Dict[str, float] = {}
        self._pushed_signal_codes_once: Dict[str, float] = {}
        self._pushed_minute_records: Dict[str, float] = {}
        self._recent_content_hashes: Dict[str, float] = {}
        self._records_lock = threading.RLock()
        self._spam_calendar_key: str = ""
        self._failure_streak = 0
        self._circuit_open_until = 0.0
        self._circuit_lock = threading.RLock()

    def _reset_spam_if_new_calendar_day_bj(self) -> None:
        """【慢性中毒修复】跨自然日（北京时间）静默清空防刷字典。"""
        today_k = _bj_calendar_key()
        with self._records_lock:
            if self._spam_calendar_key != today_k:
                if self._pushed_records or self._pushed_signal_codes_once:
                    logger.info(
                        "企微防刷 _pushed_records 跨日清空 | 旧日键=%s → 新日键=%s | 条数=%s",
                        self._spam_calendar_key or "(init)",
                        today_k,
                        len(self._pushed_records),
                    )
                self._pushed_records.clear()
                self._pushed_signal_codes_once.clear()
                self._pushed_minute_records.clear()
                self._spam_calendar_key = today_k

    def _prune_old_records(self, now: float) -> None:
        """剔除超过 30 分钟窗口的记录。"""
        cutoff = now - _SPAM_WINDOW_SEC
        with self._records_lock:
            self._pushed_records = {k: v for k, v in self._pushed_records.items() if v > cutoff}

    def _shrink_if_oversized(self) -> None:
        """硬顶：字典异常膨胀时只保留时间戳最新的若干条。"""
        with self._records_lock:
            n = len(self._pushed_records)
            if n <= _SPAM_RECORDS_HARD_CAP:
                return
            sorted_items = sorted(self._pushed_records.items(), key=lambda kv: kv[1], reverse=True)
            self._pushed_records = dict(sorted_items[: _SPAM_RECORDS_HARD_CAP // 2])
            logger.warning(
                "企微防刷字典触顶收缩 | 原条数=%s → 保留半帽=%s",
                n,
                len(self._pushed_records),
            )

    def clear_push_cache(self) -> None:
        """硬性清空 _pushed_records；用于每日早安例行或运维排障。"""
        with self._records_lock:
            n = len(self._pushed_records)
            n_codes = len(self._pushed_signal_codes_once)
            n_minutes = len(self._pushed_minute_records)
            n_hash = len(self._recent_content_hashes)
            self._pushed_records.clear()
            self._pushed_signal_codes_once.clear()
            self._pushed_minute_records.clear()
            # 【V30.13】内容指纹也要清：排障时若不清，相同内容 10 分钟内仍发不出去。
            self._recent_content_hashes.clear()
        removed_json = False
        try:
            ap = _path_alert_dedup_cache_file()
            if os.path.isfile(ap):
                os.unlink(ap)
                removed_json = True
        except Exception as e:
            logger.warning("企微网关 clear_push_cache 删除系统告警去重账本失败(忽略): %s", e)
        logger.info(
            "企微网关 clear_push_cache | 战法防刷键=%s | 当日代码防刷键=%s | 当日分钟防刷键=%s | 内容指纹=%s | 系统告警账本已删=%s",
            n,
            n_codes,
            n_minutes,
            n_hash,
            removed_json,
        )

    @staticmethod
    def _is_signal_pool_slot(pool_key: str) -> bool:
        """仅对 P3/P4（含 *_obs）启用「当日同代码仅推一次」硬防重。"""
        pk = str(pool_key or "").strip().lower()
        return pk.startswith("p3") or pk.startswith("p4")

    @staticmethod
    def _signal_pool_bucket(pool_key: str) -> str:
        """将战区归一为池维度桶"""
        pk = str(pool_key or "").strip().lower()
        if pk.startswith("p3"):
            return "p3"
        if pk.startswith("p4"):
            return "p4"
        return pk

    def send_heartbeat(self, status_dict: Dict[str, Any]) -> None:
        """拼装「早安 / 系统唤醒」Markdown"""
        if not isinstance(status_dict, dict):
            status_dict = {}
        wl = status_dict.get("wechat_push_label")
        cl = status_dict.get("daemon_cruise_label")
        if wl is None:
            wl = "开启" if status_dict.get("wechat_push_enabled", True) else "关闭"
        if cl is None:
            cl = "开启" if status_dict.get("daemon_auto_cruise_enabled", True) else "关闭"
        wls = str(wl).strip() or "--"
        cls = str(cl).strip() or "--"
        body = "\n".join(
            [
                f"🟢 【小杰AI选股系统 Pro V26.88】早安！系统已唤醒 [{_bj_now_str('%H:%M:%S')}]",
                "今日为交易日，各项数据已就绪。",
                f"🎛️ 企微推送：{wls}",
                f"🤖 自动巡航：{cls}",
            ]
        )
        extra = status_dict.get("extra_lines")
        if isinstance(extra, list) and extra:
            body = body + "\n" + "\n".join(str(x) for x in extra if str(x).strip())
        if len(body) > _MAX_MARKDOWN_CONTENT_LEN:
            body = body[: _MAX_MARKDOWN_CONTENT_LEN - 20] + "\n…(已截断)"
        self.push_markdown_async(body)

    def _reserve_push_slot(self, pool_key: str, ts_code: str, strategy_raw: Any) -> bool:
        """战法防刷闸门"""
        self._reset_spam_if_new_calendar_day_bj()
        pk = str(pool_key).strip().lower()
        sk = _strategy_dedup_segment(strategy_raw)
        bucket = self._signal_pool_bucket(pk)
        is_signal_pool = self._is_signal_pool_slot(pk)
        ts6 = _norm_ts_code(ts_code)
        minute_key = f"{bucket}:{ts6}:{_bj_now_str('%Y%m%d%H%M')}"
        key = f"{bucket}_{ts6}" if is_signal_pool else f"{pk}_{ts6}_{sk}"
        once_code_key = f"sig:{bucket}:{ts6}"
        now = time.time()
        self._prune_old_records(now)
        self._shrink_if_oversized()

        if is_signal_pool:
            if self._reserve_signal_daily_once_file(pk, ts6, sk):
                with self._records_lock:
                    self._pushed_signal_codes_once[once_code_key] = now
                return True
            logger.debug("企微推送跳过(跨进程当日同池已推送): code=%s pool=%s bucket=%s", ts6, pk, bucket)
            return False

        with self._records_lock:
            if minute_key in self._pushed_minute_records:
                logger.debug(
                    "企微推送跳过(同一分钟重复触发): code=%s pool=%s bucket=%s",
                    ts6,
                    pk,
                    bucket,
                )
                return False
            last = self._pushed_records.get(key, 0.0)
            if now - last < _SPAM_WINDOW_SEC:
                logger.debug(
                    "%s %s 处于冷却期，已静默",
                    ts6,
                    _strategy_log_fragment(strategy_raw),
                )
                return False
            self._pushed_records[key] = now
            self._pushed_minute_records[minute_key] = now
            return True

    def _reserve_signal_daily_once_file(self, pool_key: str, ts6: str, strategy_raw: Any) -> bool:
        """P3/P4 股票信号跨进程去重"""
        from core.file_utils import atomic_json_update

        try:
            from core.runtime_data_paths import path_wechat_signal_dedup_cache_json
            dedup_path = path_wechat_signal_dedup_cache_json()
        except Exception:
            dedup_path = os.path.join("data", "runtime", "wechat_signal_dedup_cache.json")

        today = _bj_calendar_key()
        pk = str(pool_key).strip().lower()
        slot_key = f"{today}:{pk}:{ts6}"
        now = time.time()
        reserved = {"ok": False}

        def _upd(data: Dict[str, Any]) -> None:
            cutoff_ts = now - float(7 * 24 * 3600)
            for k in list(data.keys()):
                if not isinstance(k, str):
                    continue
                try:
                    v = data.get(k) or {}
                    if not isinstance(v, dict):
                        continue
                    ts = float(v.get("ts", 0.0) or 0.0)
                    if ts < cutoff_ts:
                        del data[k]
                except Exception:
                    continue

            old = data.get(slot_key)
            if isinstance(old, dict):
                old_ts = float(old.get("ts", 0.0) or 0.0)
                if old_ts > 0 and str(old.get("date") or "") == today:
                    reserved["ok"] = False
                    return

            data[slot_key] = {
                "ts": now,
                "date": today,
                "pool_key": pk,
                "code": ts6,
                "strategy": _strategy_log_fragment(strategy_raw),
            }
            reserved["ok"] = True

        try:
            atomic_json_update(dedup_path, _upd, timeout=8)
        except Exception as e:
            logger.warning("企微股票信号去重账本异常，退回内存去重放行: %s", e)
            return True
        return reserved["ok"]

    def _send_markdown_sync(self, content: str) -> None:
        """同步发送：强制超时 + 失败重试 + 熔断"""
        # 【V30.9 2026-10-18】QA / 演练旁路：XJ_NO_PUSH=1 时完全不碰 webhook。
        # 事故背景：2026-10-18 上线前点检因 master_control.wechat_push_enabled=True，
        # 真实发出了 13 条企微 markdown（07:22 三条 + 09:32 十条），并触发 13 次 DeepSeek 计费调用。
        # 放在函数最外层，保证 HTTP 请求、熔断计数、失败告警一个都不发生。
        try:
            from core.qa_switches import no_push

            if no_push():
                logger.warning("[QA旁路] XJ_NO_PUSH=1 已设置，本次企微 markdown 发送已跳过（演练模式）")
                return
        except Exception as e:  # fail-open：开关读取失败绝不阻断生产推送
            logger.debug("读取 XJ_NO_PUSH 失败，继续尝试发送: %s", e)

        if not self._url:
            logger.debug("企微 webhook 未配置，跳过发送")
            return
        if not content or not str(content).strip():
            logger.debug("企微 markdown 内容为空，跳过发送")
            return

        now = time.time()
        with self._circuit_lock:
            if now < float(self._circuit_open_until or 0.0):
                logger.warning(
                    "企微 webhook 熔断中，跳过发送（剩余 %.0fs）",
                    float(self._circuit_open_until) - now,
                )
                return

        try:
            from core.master_control import is_wechat_push_master_enabled

            if not is_wechat_push_master_enabled():
                logger.debug("企微推送已跳过：master_control「企微实盘推送」关闭")
                return
        except Exception as e:
            logger.debug("读取 master_control 失败，继续尝试发送: %s", e)

        # 【V30.13】内容指纹兜底：完全相同的 markdown 在窗口内只发一次。
        # 放在 master_control 之后（关闭推送时不消耗指纹）、HTTP 之前（省掉无谓请求）。
        _content_hash = hashlib.sha1(str(content).encode("utf-8", "ignore")).hexdigest()
        with self._records_lock:
            _keep_from = now - _CONTENT_DEDUP_WINDOW_SEC
            if self._recent_content_hashes:
                self._recent_content_hashes = {
                    k: v for k, v in self._recent_content_hashes.items() if v > _keep_from
                }
            _prev = self._recent_content_hashes.get(_content_hash, 0.0)
            if _prev and (now - _prev) < _CONTENT_DEDUP_WINDOW_SEC:
                logger.warning(
                    "[企微防重] 相同内容 %.0fs 内已成功发送，跳过重复推送 | 距上次=%.0fs | 首行=%s",
                    _CONTENT_DEDUP_WINDOW_SEC,
                    now - _prev,
                    str(content).strip().splitlines()[0][:60] if str(content).strip() else "",
                )
                return

        try:
            import requests
        except ImportError:
            logger.error("未安装 requests，无法发送企微 webhook")
            return

        payload = {"msgtype": "markdown", "markdown": {"content": str(content)}}
        last_err: Optional[BaseException] = None

        for attempt in range(1, _WEBHOOK_MAX_ATTEMPTS + 1):
            try:
                resp = requests.post(
                    self._url,
                    json=payload,
                    timeout=_REQUEST_TIMEOUT,
                    headers={"Content-Type": "application/json; charset=utf-8"},
                )
                code = int(resp.status_code)
                if 200 <= code < 300:
                    if attempt > 1:
                        logger.info("企微 webhook 第 %s 次尝试成功", attempt)
                    with self._circuit_lock:
                        self._failure_streak = 0
                        self._circuit_open_until = 0.0
                    # 【V30.13】仅成功才登记内容指纹：失败重试仍可发送。
                    with self._records_lock:
                        self._recent_content_hashes[_content_hash] = time.time()
                    return
                last_err = RuntimeError(f"HTTP {code}: {resp.text[:300]}")
                logger.warning("企微 webhook HTTP 异常 attempt=%s: %s", attempt, last_err)
            except Exception as e:
                last_err = e
                logger.warning(
                    "企微 webhook 网络异常 attempt=%s/%s: %s",
                    attempt,
                    _WEBHOOK_MAX_ATTEMPTS,
                    _mask_webhook_url(e),
                )
            if attempt < _WEBHOOK_MAX_ATTEMPTS:
                try:
                    time.sleep(_WEBHOOK_RETRY_SLEEP_SEC)
                except Exception:
                    pass

        # 【P0-2 网络单点修复 2026-11-14】代理路径全部失败后，强制一次「真直连」。
        # 本机 Windows 系统代理（注册表 ProxyServer=127.0.0.1:7897）死亡时，requests
        # 默认仍无条件走代理；trust_env=False 的会话才是真正的直连（企微域名本可直连）。
        try:
            direct = requests.Session()
            direct.trust_env = False
            resp = direct.post(
                self._url,
                json=payload,
                timeout=_REQUEST_TIMEOUT,
                headers={"Content-Type": "application/json; charset=utf-8"},
            )
            code = int(resp.status_code)
            if 200 <= code < 300:
                with self._circuit_lock:
                    self._failure_streak = 0
                    self._circuit_open_until = 0.0
                with self._records_lock:
                    self._recent_content_hashes[_content_hash] = time.time()
                logger.warning(
                    "企微 webhook 走代理失败、直连兜底成功 | 请检查本机代理(127.0.0.1:7897)是否存活"
                )
                return
            last_err = RuntimeError(f"直连 HTTP {code}: {resp.text[:300]}")
            logger.warning("企微 webhook 直连兜底 HTTP 异常: %s", last_err)
        except Exception as e:
            last_err = e
            logger.warning("企微 webhook 直连兜底失败: %s", _mask_webhook_url(e))

        with self._circuit_lock:
            self._failure_streak += 1
            if self._failure_streak >= _WEBHOOK_CIRCUIT_FAILURE_THRESHOLD:
                self._circuit_open_until = time.time() + float(_WEBHOOK_CIRCUIT_OPEN_SEC)
                logger.error(
                    "企微 webhook 连续失败 %s 次，熔断 %ss",
                    self._failure_streak,
                    _WEBHOOK_CIRCUIT_OPEN_SEC,
                )
        logger.error("企微 webhook 已达最大重试仍失败: %s", _mask_webhook_url(last_err))

    def _reserve_system_alert_slot(
        self,
        dedup_key: str,
        *,
        window_sec: Optional[float] = None,
    ) -> bool:
        """系统告警文件去重"""
        self._reset_spam_if_new_calendar_day_bj()
        win = float(window_sec) if window_sec is not None else float(_SYSTEM_ALERT_DEDUP_DEFAULT_SEC)
        win = max(60.0, min(win, float(7 * 24 * 3600)))
        key = f"sys:{str(dedup_key)[:200]}"
        allow = _file_reserve_system_alert_slot(dedup_key, win)
        if not allow:
            logger.debug("企微系统告警跳过(文件防刷): %s (窗口=%ss)", key, int(win))
        return allow

    def _send_system_markdown_sync(self, content: str) -> None:
        """与 _send_markdown_sync 相同 HTTP 路径，但总闸为「系统告警」而非「信号推送」。"""
        # 【V30.9】与 _send_markdown_sync 同一旁路：演练模式一律不发系统告警。
        try:
            from core.qa_switches import no_push

            if no_push():
                logger.warning("[QA旁路] XJ_NO_PUSH=1 已设置，本次企微系统告警已跳过（演练模式）")
                return
        except Exception as e:  # fail-open
            logger.debug("读取 XJ_NO_PUSH 失败，继续尝试发送系统告警: %s", e)

        if not self._url:
            logger.debug("企微 webhook 未配置，跳过系统告警")
            return
        if not content or not str(content).strip():
            return
        try:
            from core.master_control import is_wechat_system_alert_enabled

            if not is_wechat_system_alert_enabled():
                logger.debug("企微系统告警已跳过：master_control「系统温馨提示」关闭")
                return
        except Exception as e:
            logger.debug("读取 master_control 系统告警开关失败，继续尝试: %s", e)

        try:
            import requests
        except ImportError:
            logger.error("未安装 requests，无法发送企微系统告警")
            return

        payload = {"msgtype": "markdown", "markdown": {"content": str(content)}}
        last_err: Optional[BaseException] = None

        for attempt in range(1, _WEBHOOK_MAX_ATTEMPTS + 1):
            try:
                resp = requests.post(
                    self._url,
                    json=payload,
                    timeout=_REQUEST_TIMEOUT,
                    headers={"Content-Type": "application/json; charset=utf-8"},
                )
                code = int(resp.status_code)
                if 200 <= code < 300:
                    if attempt > 1:
                        logger.info("企微系统告警 webhook 第 %s 次尝试成功", attempt)
                    return
                last_err = RuntimeError(f"HTTP {code}: {resp.text[:300]}")
                logger.warning("企微系统告警 HTTP 异常 attempt=%s: %s", attempt, last_err)
            except Exception as e:
                last_err = e
                logger.warning(
                    "企微系统告警网络异常 attempt=%s/%s: %s",
                    attempt,
                    _WEBHOOK_MAX_ATTEMPTS,
                    _mask_webhook_url(e),
                )
            if attempt < _WEBHOOK_MAX_ATTEMPTS:
                try:
                    time.sleep(_WEBHOOK_RETRY_SLEEP_SEC)
                except Exception:
                    pass

        # 【P0-2 网络单点修复 2026-11-14】同 _send_markdown_sync：代理失败后强制真直连。
        try:
            direct = requests.Session()
            direct.trust_env = False
            resp = direct.post(
                self._url,
                json=payload,
                timeout=_REQUEST_TIMEOUT,
                headers={"Content-Type": "application/json; charset=utf-8"},
            )
            code = int(resp.status_code)
            if 200 <= code < 300:
                logger.warning(
                    "企微系统告警走代理失败、直连兜底成功 | 请检查本机代理(127.0.0.1:7897)是否存活"
                )
                return
            last_err = RuntimeError(f"直连 HTTP {code}: {resp.text[:300]}")
            logger.warning("企微系统告警直连兜底 HTTP 异常: %s", last_err)
        except Exception as e:
            last_err = e
            logger.warning("企微系统告警直连兜底失败: %s", _mask_webhook_url(e))

        logger.error("企微系统告警已达最大重试仍失败: %s", _mask_webhook_url(last_err))
        # 【P0-2】代理 + 直连全挂 = 网络级故障：告警本体落本地账本，做到永不静默丢失。
        _append_undelivered_alert(content, last_err)

    def push_system_alert_async(
        self,
        content: str,
        dedup_key: str,
        *,
        dedup_window_sec: Optional[float] = None,
    ) -> None:
        # [V30.9 2026-10-18] QA bypass must come BEFORE _reserve_system_alert_slot:
        # otherwise with XJ_NO_PUSH=1 no HTTP is sent, but the dedup ledger
        # alert_dedup_cache.json is still written with test timestamps, polluting
        # the production alert ledger (one entry per UI menu probe click).
        try:
            from core.qa_switches import no_push

            if no_push():
                logger.warning(
                    "[QA-BYPASS] XJ_NO_PUSH=1 set, system alert skipped (dry-run, dedup ledger untouched)"
                )
                return
        except Exception as e:  # fail-open: switch read failure must never block production alerts
            logger.debug("Failed to read XJ_NO_PUSH, continuing production alert path: %s", e)

        if not self._reserve_system_alert_slot(dedup_key, window_sec=dedup_window_sec):
            return

        def _run() -> None:
            try:
                self._send_system_markdown_sync(content)
            except Exception as e:
                logger.warning("企微系统告警异步发送兜底异常(已吞): %s", e, exc_info=True)

        try:
            _WECHAT_WEBHOOK_EXECUTOR.submit(_run)
        except Exception as e:
            logger.error("企微系统告警提交线程池失败: %s", e)

    def push_markdown_async(self, content: str) -> None:
        """将 _send_markdown_sync 提交至全局有界线程池"""

        def _run() -> None:
            try:
                self._send_markdown_sync(content)
            except Exception as e:
                logger.warning("企微异步发送兜底异常(已吞): %s", e, exc_info=True)

        try:
            _WECHAT_WEBHOOK_EXECUTOR.submit(_run)
        except Exception as e:
            logger.error("企微推送提交线程池失败: %s", e)

    def push_stock_if_allowed(
        self,
        pool_key: str,
        stock_dict: Dict[str, Any],
        *,
        pool_key_for_dedup: Optional[str] = None,
    ) -> None:
        """去重通过后异步推送单票 markdown"""
        # 【V30.9】演练模式在本函数最外层短路：先于 master_control 读取、先于防刷账本写入、
        # 先于 DeepSeek 计费调用。这样点检既不会发企微，也不会把真实去重账本
        # wechat_signal_dedup_cache.json 写成测试数据（历史事故：07:22 三条 p4_obs 测试推送）。
        try:
            from core.qa_switches import no_push

            if no_push():
                logger.warning(
                    "[QA旁路] XJ_NO_PUSH=1 已设置，本次 %s 池股票推送已跳过（演练模式）", pool_key
                )
                return
        except Exception as e:  # fail-open
            logger.debug("读取 XJ_NO_PUSH 失败，继续按生产链路推送: %s", e)

        try:
            from core.master_control import is_wechat_push_master_enabled

            if not is_wechat_push_master_enabled():
                logger.debug("企微推送跳过(总控关闭): pool=%s", pool_key)
                return
        except Exception as e:
            logger.debug("push_stock_if_allowed master_control: %s", e)
        ts_code = _norm_ts_code(stock_dict.get("代码") if isinstance(stock_dict, dict) else None)
        if not ts_code:
            logger.debug("企微推送跳过：无有效代码")
            return
        # P5 次日早盘闭环
        try:
            from core.p5_morning_validation import is_code_blocked_by_morning_p5_validation

            if is_code_blocked_by_morning_p5_validation(ts_code):
                logger.debug("企微推送跳过(P5次日早盘已剔除): code=%s pool=%s", ts_code, pool_key)
                return
        except Exception as e:
            logger.debug("P5 早盘剔除闸读取失败(不拦截): %s", e)
        score_v = _finite_float_or_none(stock_dict.get("综合分", stock_dict.get("score", 0.0)))
        if str(pool_key).lower() in {"p2", "p3", "p4"} and (score_v is None or score_v < 40.0):
            logger.debug("企微推送跳过：%s 分数过低 score=%s code=%s", pool_key, score_v, ts_code)
            return
        pk_slot = (pool_key_for_dedup or pool_key).strip().lower()
        zhanfa_raw = stock_dict.get("战法") if isinstance(stock_dict, dict) else None
        if not self._reserve_push_slot(pk_slot, ts_code, zhanfa_raw):
            return
        # 【V30.10 信号账本桥接】推送成功占位后，把 signal_log.pushed 回写为 TRUE。
        # 根因：signal_log 只记录「入池候选」，push_history.json 记录「实际推送」，
        #      两者本应靠 signal_log.pushed 打通，但该字段的写入点从未落地，
        #      导致 pushed 100% 为 False → 无法区分「候选」与「真推送」，
        #      战法有效性归因失去参照系、push_min_score 无法调参。
        #
        # 【并发安全】推送发生在 _WECHAT_WEBHOOK_EXECUTOR 线程池里，而主扫描线程
        # 可能正持有 DuckDB 写连接。此处若直接 get_write_conn() 开独占短连接，
        # 会与主扫描抢文件锁 → 报 "another program is using the database"。
        # 故：提交到独立后台线程池异步回写 + 复用进程内写连接单例，
        #      失败静默重试，绝不阻塞推送、绝不抛到推送主链路。
        try:
            from core.signal_ledger_bridge import submit_pushed_mark

            # 【V30.10】带上真实交易日：同一天同票可能同时命中多个池，
            # 不限定交易日会误标到错误的候选行上，破坏归因准确性。
            from core.notification.core import _bj_calendar_key

            submit_pushed_mark(ts_code, pk_slot, _bj_calendar_key())
        except Exception as _e_mp:
            logger.debug("signal_log.pushed 回写提交失败(不影响推送): %s", _e_mp)
        try:
            # 延迟导入避免循环
            from core.notification.format import format_wechat_markdown
            body = format_wechat_markdown(pool_key, stock_dict)
        except Exception as e:
            logger.warning("format_wechat_markdown 外层异常(不应发生): %s", e, exc_info=True)
            from core.notification.format import format_wechat_markdown
            body = format_wechat_markdown(pool_key, {})
        try:
            from core.notification.deepseek import _maybe_get_deepseek_advice, _append_deepseek_advice
            advice = _maybe_get_deepseek_advice(pool_key, stock_dict)
            if advice:
                body = _append_deepseek_advice(body, advice)
        except Exception as e:
            logger.debug("DeepSeek 建议注入失败(不影响推送): %s", e)
        self.push_markdown_async(body)


def get_wechat_gateway(webhook_url: str) -> WechatNotificationGateway:
    """单例：同一进程共享受去重字典；URL 变更时重建实例。"""
    global _gateway_singleton
    u = (webhook_url or "").strip()
    with _gateway_lock:
        if _gateway_singleton is None or _gateway_singleton._url != u:
            _gateway_singleton = WechatNotificationGateway(u)
        return _gateway_singleton


def get_wechat_secondary_gateway(webhook_url: str) -> WechatNotificationGateway:
    """副路由单例：与主路由隔离，避免不同消息类型共享同一去重状态。"""
    global _gateway_secondary_singleton
    u = (webhook_url or "").strip()
    with _gateway_lock:
        if _gateway_secondary_singleton is None or _gateway_secondary_singleton._url != u:
            _gateway_secondary_singleton = WechatNotificationGateway(u)
        return _gateway_secondary_singleton


__all__ = [
    "WechatNotificationGateway",
    "get_wechat_gateway", "get_wechat_secondary_gateway",
    "_WEBHOOK_MAX_ATTEMPTS", "_WEBHOOK_RETRY_SLEEP_SEC",
    "_WEBHOOK_CIRCUIT_FAILURE_THRESHOLD", "_WEBHOOK_CIRCUIT_OPEN_SEC",
    "_SPAM_RECORDS_HARD_CAP",
]
