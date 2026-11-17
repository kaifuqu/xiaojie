# -*- coding: utf-8 -*-
"""core/net_health.py — 网络健康自检（代理单点故障的唯一观测点）

【为什么需要这个模块】
2026-11-14 实测事故：本机 Windows 系统代理（注册表 ProxyServer=127.0.0.1:7897）
死亡后，Tushare 数据接口与企微告警一起失联——**告警系统自己也发不出告警**，
运维完全失明。事后复盘只看到 sniper.log 里一串 ProxyError。

【本模块做什么】
只做三件事（全部无副作用，只读探测 + 写一个状态文件）：
  1. 读注册表/环境确认「当前生效的代理配置」；
  2. 探测代理端口是否存活（本地 TCP 连接）；
  3. 用 trust_env=False 的独立会话探测关键域名的**直连**可达性
     （api.waditu.com / qyapi.weixin.qq.com）；
结论分三档写入 data/runtime/state/net_health.json：
  · ok          代理配置无 / 代理存活且直连也通 —— 一切正常
  · direct_only 代理死亡但直连通 —— 业务已能靠直连兜底跑，但需修代理
  · down        代理与直连全断 —— 数据/推送/告警三停，必须在本地可见

【纪律】
只读、不下单、不改任何配置；由 system_health 巡检 / 手动 CLI 调用。
"""
from __future__ import annotations

import json
import logging
import os
import socket
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

_log = logging.getLogger(__name__)

BJ_TZ = timezone(timedelta(hours=8))

# 关键依赖域名（直连可达性探测目标）
_CRITICAL_HOSTS = (
    ("tushare", "api.waditu.com"),
    ("wechat", "qyapi.weixin.qq.com"),
)
_ENV_PROXY_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")


def _now_bj_str() -> str:
    return datetime.now(BJ_TZ).strftime("%Y-%m-%d %H:%M:%S")


def _state_path() -> str:
    try:
        from core.runtime_data_paths import RUNTIME_DIR

        return os.path.join(RUNTIME_DIR, "state", "net_health.json")
    except Exception:
        root = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
        return os.path.join(root, "data", "runtime", "state", "net_health.json")


def read_windows_proxy() -> Dict[str, Any]:
    """读取 Windows 系统代理配置（注册表 Internet Settings）；非 Windows / 失败返回空。"""
    out: Dict[str, Any] = {"enabled": False, "server": ""}
    try:
        import winreg  # type: ignore

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        ) as key:
            try:
                enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
                out["enabled"] = bool(int(enabled))
            except OSError:
                pass
            try:
                server, _ = winreg.QueryValueEx(key, "ProxyServer")
                out["server"] = str(server or "")
            except OSError:
                pass
    except Exception:
        pass
    return out


def _parse_loopback_port(server: str) -> Optional[tuple]:
    """从 ProxyServer（可能形如 http=127.0.0.1:7897;https=...）里解析出本机端口。"""
    try:
        s = str(server or "")
        for token in s.replace(";", " ").replace("=", " ").split():
            parts = token.split(":")
            if len(parts) == 2 and parts[1].isdigit():
                host, port = parts[0], int(parts[1])
                if host in ("127.0.0.1", "localhost"):
                    return host, port
    except Exception:
        pass
    return None


def _tcp_alive(host: str, port: int, timeout: float = 0.6) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def _direct_reachable(host: str, timeout: float = 4.0) -> bool:
    """真直连探测：trust_env=False 的会话（绕过注册表代理），HEAD 关键域名。"""
    try:
        import requests

        s = requests.Session()
        s.trust_env = False
        # 任意 HTTP 响应码（含 401/404/405）都说明网络路径通；只有连接层失败才算不通
        s.head(f"https://{host}/", timeout=timeout, allow_redirects=False)
        return True
    except Exception:
        # HEAD 被拒时退回 GET（少数网关对 HEAD 直接断连）
        try:
            import requests

            s = requests.Session()
            s.trust_env = False
            s.get(f"https://{host}/", timeout=timeout, allow_redirects=False)
            return True
        except Exception:
            return False


def check_once(*, write_state: bool = True, timeout: float = 4.0) -> Dict[str, Any]:
    """执行一次网络体检并（默认）落盘；任何异常都不会抛出。"""
    t0 = time.time()
    try:
        reg = read_windows_proxy()
        env_proxy = {k: os.environ.get(k) for k in _ENV_PROXY_KEYS if os.environ.get(k)}

        proxy_configured = bool(reg.get("enabled") and reg.get("server")) or bool(env_proxy)
        proxy_alive: Optional[bool] = None
        proxy_target = ""
        loop = _parse_loopback_port(str(reg.get("server") or ""))
        if loop:
            proxy_target = f"{loop[0]}:{loop[1]}"
            proxy_alive = _tcp_alive(loop[0], loop[1])
        elif env_proxy:
            # 环境变量形态的代理：尽力解析第一个 loopback 地址
            for v in env_proxy.values():
                stripped = str(v).split("://")[-1]
                parts = stripped.split(":")
                if len(parts) == 2 and parts[1].isdigit() and parts[0] in ("127.0.0.1", "localhost"):
                    proxy_target = stripped
                    proxy_alive = _tcp_alive(parts[0], int(parts[1]))
                    break

        hosts: List[Dict[str, Any]] = []
        for name, host in _CRITICAL_HOSTS:
            hosts.append({"name": name, "host": host, "direct_ok": _direct_reachable(host, timeout=timeout)})

        any_direct = any(h["direct_ok"] for h in hosts)
        all_direct = all(h["direct_ok"] for h in hosts)

        if not proxy_configured:
            verdict = "ok" if all_direct else ("down" if not any_direct else "degraded")
        elif proxy_alive:
            verdict = "ok" if all_direct else "degraded"
        else:
            # 代理配置了但端口死亡
            verdict = "direct_only" if any_direct else "down"

        result: Dict[str, Any] = {
            "checked_at": _now_bj_str(),
            "elapsed_sec": round(time.time() - t0, 2),
            "verdict": verdict,
            "proxy": {
                "configured": proxy_configured,
                "registry_enabled": bool(reg.get("enabled")),
                "registry_server": str(reg.get("server") or ""),
                "env": env_proxy,
                "loopback_target": proxy_target,
                "alive": proxy_alive,
            },
            "hosts": hosts,
        }

        level_ok = verdict == "ok"
        if verdict == "down":
            _log.error(
                "[net_health] 严重：代理与直连全部不可达（%s）| 数据/推送/告警将三停",
                ", ".join(h["host"] for h in hosts),
            )
        elif verdict == "direct_only":
            _log.warning(
                "[net_health] 代理端口 %s 已死亡，直连可达；业务将依赖直连兜底，请尽快恢复代理",
                proxy_target or "(unknown)",
            )
        elif verdict == "degraded":
            _log.warning(
                "[net_health] 部分关键域名直连不可达: %s",
                ", ".join(h["host"] for h in hosts if not h["direct_ok"]),
            )
        elif level_ok:
            _log.debug("[net_health] 网络正常 | 代理=%s 直连=全部可达", proxy_target or "(无)")

        if write_state:
            try:
                path = _state_path()
                os.makedirs(os.path.dirname(path), exist_ok=True)
                tmp = path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(result, f, ensure_ascii=False, indent=2)
                os.replace(tmp, path)
            except Exception as e:
                _log.debug("[net_health] 状态落盘失败(忽略): %s", e)
        return result
    except Exception as e:  # noqa: BLE001 — 体检本体绝不抛出
        _log.warning("[net_health] 体检异常(按未知处理): %s", e)
        return {"checked_at": _now_bj_str(), "verdict": "unknown", "error": str(e)[:300]}


def load_state() -> Dict[str, Any]:
    """读取最近一次体检结果；不存在/损坏返回 {}。"""
    try:
        with open(_state_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _self_test() -> None:
    """纯逻辑自检：端口解析 + 无异常执行 + 落盘可读。"""
    assert _parse_loopback_port("127.0.0.1:7897") == ("127.0.0.1", 7897)
    assert _parse_loopback_port("http=127.0.0.1:7897;https=127.0.0.1:7897") == ("127.0.0.1", 7897)
    assert _parse_loopback_port("10.0.0.8:3128") is None
    assert _parse_loopback_port("") is None
    r = check_once(write_state=False)
    assert r.get("verdict") in {"ok", "degraded", "direct_only", "down", "unknown"}, r
    print(f"[net_health] self-test OK | verdict={r.get('verdict')}")


if __name__ == "__main__":
    _self_test()
    print(json.dumps(check_once(), ensure_ascii=False, indent=2))


__all__ = ["check_once", "load_state", "read_windows_proxy"]
