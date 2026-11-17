# -*- coding: utf-8 -*-
"""
小杰AI选股系统 Pro V26.88 — 物理总控台跨进程状态（Streamlit UI ↔ auto_sniper_daemon）

持久化：data/runtime/state/master_control.json
并发：独立 .lock 文件 + 平台文件锁；写入采用临时文件 + os.replace 原子替换。

缺省（文件不存在或字段缺失）：企微推送、全自动巡航、P1≥75 推送均为 True（全开）；
若需关闭高分底仓摘要可在总控取消勾选。
维护模式 maintenance_mode 缺省为 False；读写合并时始终保留该键，避免被其它字段写入冲掉。
"""
from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import time
from typing import Any, Dict

logger = logging.getLogger(__name__)

_DEFAULTS: Dict[str, bool] = {
    "wechat_push_enabled": True,
    "daemon_auto_cruise_enabled": True,
    # P1 全量洗盘 / 守护进程 19:15 重建后是否自动推送「综合分≥75」底仓摘要至企微
    "push_p1_high_score_enabled": True,
    # 数据下载失败、P1～P5 扫描异常等「运维温馨提示」是否仍发企微（可与上面信号推送分开关）
    "wechat_system_alert_enabled": True,
    # 维护模式：为 True 时由业务侧/守护进程自行解释（如暂停非关键写库）；缺省关
    "maintenance_mode": False,
}

# ---------------------------------------------------------------------------
# 全局缺省配置：维护模式关；与 _DEFAULTS["maintenance_mode"] 语义一致（运行态真值以 JSON 为准）
# ---------------------------------------------------------------------------
maintenance_mode: bool = False


def _state_path() -> str:
    from core.runtime_data_paths import STATE_DIR, ensure_runtime_data_layout

    ensure_runtime_data_layout()
    return os.path.join(STATE_DIR, "master_control.json")


def get_master_control_state_path() -> str:
    """返回 master_control.json 绝对路径（供 UI 监视 mtime、手工改文件后重载）。"""
    return _state_path()


def _lock_file_path() -> str:
    return _state_path() + ".lock"


def _ensure_lock_region(fp: Any) -> None:
    """保证锁文件至少 1 字节，供 msvcrt.locking / flock 使用。"""
    fp.seek(0, os.SEEK_END)
    if fp.tell() == 0:
        fp.write(b"\0")
        fp.flush()
    fp.seek(0)


def _lock_acquire(fp: Any) -> None:
    if sys.platform == "win32":
        import msvcrt

        _ensure_lock_region(fp)
        fp.seek(0)
        # 阻塞直至取得 1 字节排他锁
        msvcrt.locking(fp.fileno(), msvcrt.LK_LOCK, 1)
    else:
        import fcntl

        fcntl.flock(fp.fileno(), fcntl.LOCK_EX)


def _lock_release(fp: Any) -> None:
    try:
        if sys.platform == "win32":
            import msvcrt

            fp.seek(0)
            msvcrt.locking(fp.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fp.fileno(), fcntl.LOCK_UN)
    except OSError as e:
        logger.debug("master_control 释放锁: %s", e)


def read_master_control() -> Dict[str, Any]:
    """
    加共享逻辑由排他文件锁统一串行化读写（简单可靠）。
    返回包含 wechat_push_enabled、daemon_auto_cruise_enabled、maintenance_mode 及可选元数据的 dict；
    磁盘上已有 maintenance_mode 时必定带回，避免丢失。
    """
    path = _state_path()
    lk = _lock_file_path()
    os.makedirs(os.path.dirname(lk), exist_ok=True)
    out: Dict[str, Any] = {**_DEFAULTS}
    try:
        with open(lk, "a+b") as lf:
            _lock_acquire(lf)
            try:
                if os.path.isfile(path):
                    try:
                        with open(path, "r", encoding="utf-8") as jf:
                            raw = json.load(jf)
                        if isinstance(raw, dict):
                            for k in _DEFAULTS:
                                if k in raw:
                                    out[k] = bool(raw[k])
                            ua = raw.get("updated_at")
                            if ua is not None:
                                out["updated_at"] = ua
                    except Exception as e:
                        logger.warning("读取 master_control.json 异常，使用缺省: %s", e)
                return out
            finally:
                _lock_release(lf)
    except Exception as e:
        logger.warning("master_control 读锁失败，返回缺省配置: %s", e)
        return {**_DEFAULTS}


def write_master_control(**kwargs: Any) -> Dict[str, Any]:
    """
    合并写入；仅识别 _DEFAULTS 中的键（含 maintenance_mode）。
    写前完整读回磁盘，再与 kwargs 合并，确保不会把未出现在 kwargs 里的 maintenance_mode 等键抹掉。
    返回合并后的完整状态。
    """
    path = _state_path()
    lk = _lock_file_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    os.makedirs(os.path.dirname(lk), exist_ok=True)
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        ts = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()
    except Exception:
        ts = str(time.time())

    with open(lk, "a+b") as lf:
        _lock_acquire(lf)
        try:
            data = {**_DEFAULTS}
            if os.path.isfile(path):
                try:
                    with open(path, "r", encoding="utf-8") as jf:
                        raw = json.load(jf)
                    if isinstance(raw, dict):
                        for k in _DEFAULTS:
                            if k in raw:
                                data[k] = bool(raw[k])
                except Exception as e:
                    logger.warning("写前读 master_control 失败，将覆盖为缺省+patch: %s", e)
            for k, v in kwargs.items():
                if k in _DEFAULTS and v is not None:
                    data[k] = bool(v)
            payload = dict(data)
            payload["updated_at"] = ts

            dname = os.path.dirname(path) or "."
            fd, tmp = tempfile.mkstemp(prefix=".mc_", suffix=".json", dir=dname, text=True)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as wf:
                    json.dump(payload, wf, ensure_ascii=False, indent=0)
                os.replace(tmp, path)
            except Exception:
                try:
                    if os.path.isfile(tmp):
                        os.unlink(tmp)
                except OSError:
                    pass
                raise
            return payload
        finally:
            _lock_release(lf)


def is_wechat_push_master_enabled() -> bool:
    return bool(read_master_control().get("wechat_push_enabled", True))


def is_daemon_auto_cruise_enabled() -> bool:
    return bool(read_master_control().get("daemon_auto_cruise_enabled", True))


def is_push_p1_high_score_enabled() -> bool:
    """是否在全量洗盘完成后自动推送 P1 高分底池（≥75 分）至企业微信。"""
    return bool(read_master_control().get("push_p1_high_score_enabled", False))


def is_wechat_system_alert_enabled() -> bool:
    """是否推送数据/扫描类异常温馨提示至企微（与交易信号推送独立，默认开）。"""
    return bool(read_master_control().get("wechat_system_alert_enabled", True))


def is_maintenance_mode_enabled() -> bool:
    """
    是否处于维护模式（如暂停非关键写库、由守护进程避让长事务）。
    缺省或解析失败返回 False。
    """
    try:
        return bool(read_master_control().get("maintenance_mode", False))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# UI → daemon 「让路信号」（运行态信号，独立于 master_control.json 主配置）
# ---------------------------------------------------------------------------
# 用法：
#   1) UI 端在需要写 quant_data.duckdb 前调 request_ui_yield_signal(...)；
#   2) daemon 端每个扫描拍前/空闲时调 consume_ui_yield_signal()，若为 True 让出写连接；
#   3) UI 端写完库后调 clear_ui_yield_signal(...)，daemon 立即恢复扫描。

_UI_YIELD_DEFAULT_TTL_SEC = 90


def _ui_yield_signal_path() -> str:
    from core.runtime_data_paths import path_ui_yield_signal_json

    return path_ui_yield_signal_json()


def _atomic_write_json(path: str, payload: Dict[str, Any]) -> None:
    """跨进程原子写 JSON：临时文件 + os.replace，避免读到半截。"""
    dname = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(prefix=".ui_yield_", suffix=".json", dir=dname, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as wf:
            json.dump(payload, wf, ensure_ascii=False, indent=0)
        os.replace(tmp, path)
    except Exception:
        try:
            if os.path.isfile(tmp):
                os.unlink(tmp)
        except OSError:
            pass
        raise


def request_ui_yield_signal(
    reason: str = "ui_data_sync",
    ttl_sec: int = _UI_YIELD_DEFAULT_TTL_SEC,
    requester_pid: int | None = None,
) -> Dict[str, Any]:
    """
    UI 端发起请求：写一个「让路信号」JSON，供 daemon 探测。
    - 若已有未过期信号，直接覆盖并续期（避免并发场景丢失）；
    - ttl_sec 默认 90s：超时后 daemon 自动忽略，避免长期卡死。
    返回写入的 payload。
    """
    path = _ui_yield_signal_path()
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        ts = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()
    except Exception:
        ts = str(time.time())
    pid = requester_pid if requester_pid is not None else os.getpid()
    payload = {
        "active": True,
        "reason": str(reason or "ui_data_sync"),
        "requested_at": ts,
        "expires_at": time.time() + max(5, int(ttl_sec)),
        "requested_pid": int(pid),
        "cleared_by_pid": None,
    }
    try:
        _atomic_write_json(path, payload)
        logger.info(
            "UI 让路信号已写入: %s reason=%s ttl=%ss pid=%s",
            path, payload["reason"], ttl_sec, pid,
        )
    except Exception as e:
        logger.warning("UI 让路信号写入失败（将降级为提示用户停 daemon）: %s", e)
    return payload


def read_ui_yield_signal() -> Dict[str, Any] | None:
    """
    daemon 端调用：读取 UI 让路信号；过期 / 解析失败 / 未激活均返回 None。
    """
    path = _ui_yield_signal_path()
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, dict) or not raw.get("active"):
            return None
        exp = raw.get("expires_at")
        try:
            if exp is not None and float(exp) < time.time():
                logger.info("UI 让路信号已过期，视为已解除: %s", path)
                return None
        except Exception:
            pass
        return raw
    except Exception as e:
        logger.debug("读 UI 让路信号失败: %s", e)
        return None


def clear_ui_yield_signal(
    clearer_pid: int | None = None,
    reason: str = "ui_sync_done",
) -> None:
    """
    UI 端写完库后调用：清除信号，daemon 立即恢复扫描。
    """
    path = _ui_yield_signal_path()
    try:
        if not os.path.isfile(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                raw["active"] = False
                raw["cleared_at"] = time.time()
                raw["cleared_by_pid"] = int(clearer_pid or os.getpid())
                raw["clear_reason"] = str(reason or "ui_sync_done")
                _atomic_write_json(path, raw)
                logger.info("UI 让路信号已清除: %s reason=%s", path, raw.get("clear_reason"))
                return
        except Exception:
            pass
        # 信号文件存在但解析失败：直接删除兜底
        try:
            os.unlink(path)
            logger.info("UI 让路信号文件已强制删除: %s", path)
        except OSError as e:
            logger.debug("删除 UI 让路信号文件失败: %s", e)
    except Exception as e:
        logger.debug("clear_ui_yield_signal 异常（忽略）: %s", e)
