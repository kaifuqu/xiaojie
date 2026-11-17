# -*- coding: utf-8 -*-
"""网络健康探测 + 降级建议（2026-11-14 上线审计落地）

【背景】
2026-11-14 审计发现：本机代理（127.0.0.1:7897）长期死亡，所有 API 调用先被
requests 送到死代理失败，再走直连兜底。数据能恢复，但：
  · 每次接口调用要空烧一轮重试（日志噪音大、延迟高）
  · 老代码的直连兜底本身有 bug（只清环境变量，仍走注册表代理）→ 已修复
本模块提供：
  1. probe_network_health()  —— 探测「代理/学术直连/企微直连」三路状态
  2. save_network_health_snapshot() —— 写入 data/runtime/state/network_health.json
  3. format_network_health_line() —— 单行摘要（供 system_health / UI 展示）

【调用点】
  · auto_sniper_daemon.py 开盘前 09:20 探测并落盘（若可用）
  · tools/system_health.py 展示当前结论
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_PATH = os.path.join(ROOT, "data", "runtime", "state", "network_health.json")

BJ_TZ = timezone(timedelta(hours=8))


def _tcp_probe(host: str, port: int, timeout: float = 2.0) -> bool:
    import socket

    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def _http_probe(url: str, timeout: float = 6.0) -> bool:
    """HTTP 探活。强制直连：trust_env=False + 显式关闭代理（绕过任何代理配置）。

    注意（2026-11-14 踩坑）：trust_env 是 requests.Session 的属性，
    不是 requests.get() 的关键字参数——传进 get() 会 TypeError。
    """
    import requests

    s = requests.Session()
    s.trust_env = False
    s.proxies = {"http": None, "https": None}
    try:
        r = s.get(url, timeout=timeout)
        return r.status_code < 500
    except Exception:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


def probe_network_health(timeout: float = 6.0) -> dict:
    proxy_alive = _tcp_probe("127.0.0.1", 7897, timeout=1.0)
    waditu_ok = _http_probe("http://api.waditu.com/", timeout=timeout)
    wecom_ok = _http_probe("https://qyapi.weixin.qq.com/cgi-bin/gettoken?corpid=x&corpsecret=x", timeout=timeout)

    if waditu_ok and wecom_ok:
        if proxy_alive:
            conn = "proxy_ok(直连亦可达)"
        else:
            conn = "direct_only（本机代理端口 7897 已死，当前靠直连）"
        status = "OK"
    elif waditu_ok or wecom_ok:
        conn = "partial"
        status = "WARN"
    else:
        conn = "offline"
        status = "CRIT"

    return {
        "probed_at": datetime.now(BJ_TZ).isoformat(timespec="seconds"),
        "status": status,
        "connection": conn,
        "proxy_7897_alive": proxy_alive,
        "waditu_direct_ok": waditu_ok,
        "wecom_direct_ok": wecom_ok,
        "advice": (
            "直连可达，无需处理（代理设了但没开，建议关闭系统代理设置以消除内置噪音）"
            if not proxy_alive and waditu_ok
            else ("网络正常" if proxy_alive and waditu_ok else ("检查网络/防火墙" if not waditu_ok else ""))
        ),
    }


def save_network_health_snapshot() -> str:
    data = probe_network_health()
    try:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
    except Exception:
        pass
    return data.get("status", "?")


def load_network_health_snapshot() -> dict:
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def format_network_health_line(data: dict | None = None, max_age_hours: float = 24.0) -> str:
    if data is None:
        data = load_network_health_snapshot()
    if not data:
        return "网络健康：未探测"
    try:
        ts = datetime.fromisoformat(str(data.get("probed_at", "")))
        age_h = (datetime.now(BJ_TZ) - ts).total_seconds() / 3600.0
    except Exception:
        age_h = 0.0
    head = f"网络健康：{data.get('status', '?')} / {data.get('connection', '?')}"
    tail = f"（{age_h:.1f}h前探测）" if age_h > 0 else ""
    advice = str(data.get("advice") or "").strip()
    return head + tail + (f" — {advice}" if advice else "")
