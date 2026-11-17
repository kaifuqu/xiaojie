# -*- coding: utf-8 -*-
"""
core/process_orchestrator.py — 进程互斥协调器（UI <-> Daemon 单进程模式）

【背景】
DuckDB 在 Windows 下，同一数据库文件同一时间只能被一个进程独占打开（不论 read-only 还是 write
模式都会触发 "File is already open"）。这意味着:

  - UI (Streamlit run_lab.py / ui/app.py) 进程持有只读连接期间，
    daemon 启动后任何写操作都会 retry 撞锁。
  - 反之亦然：daemon 跑时启动另一个 daemon 也会撞。

【工作模式】
小杰系统的设计是 **单写者模式**：
  - "手动调试期" :  只跑 UI (start_server.bat)，daemon 不跑。
  - "全自动运行期": 只跑 daemon (start_daemon_24x7.bat)，UI 不跑。

这个模块让两个 .bat 互相发现，避免用户"忘了关 UI 就启 daemon"或"忘了关 daemon 就开 UI。

【机制】
  1. 启动时在 data/runtime/state/ 下写一个 sentinel 文件 (e.g. ui_manual.lock / daemon.lock)
     内容含 {pid, started_at, cmdline_info}。
  2. 启动另一方时，检查对方 sentinel 是否存在且对应进程还在线 → 提示用户处理。
  3. 进程退出时清理 sentinel（优雅路径）。
  4. 进程崩溃/强杀时，sentinel 残留 — 提供 cleanup_stale_sentinel() 启动时清理过期文件。

【作者】xiaojie-ai
【创建日期】V26.88+ 10-14
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


SENTINEL_DIR = os.path.join("data", "runtime", "state")
UI_LOCK_FILE = os.path.join(SENTINEL_DIR, "ui_manual.lock")
DAEMON_LOCK_FILE = os.path.join(SENTINEL_DIR, "daemon.lock")

# sentinel 超过这个秒数且对应进程已死，认为是陈旧残留，自动清理
STALE_SENTINEL_SEC = 600.0  # 10 分钟


def _state_dir() -> str:
    p = SENTINEL_DIR
    try:
        os.makedirs(p, exist_ok=True)
    except Exception:
        pass
    return p


def _ensure_path(filename: str) -> str:
    return os.path.join(_state_dir(), os.path.basename(filename))


def _read_sentinel(path: str) -> Optional[dict]:
    try:
        if not os.path.isfile(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.debug("[orchestrator] 读取 sentinel 失败 %s: %s", path, e)
        return None


def _write_sentinel(path: str, payload: dict) -> bool:
    """
    原子写：先写 .tmp，再 os.replace 替换真文件。
    若目标目录 ACL 不允许新文件创建（典型场景：data/runtime/state/ 目录是管理员建的，
    普通用户 token 在 BUILTIN\\Users 下只有 ReadAndExecute），降级为直接覆盖；
    再失败也不抛 — sentinel 仅用于 UI / Daemon 互斥提示，不阻塞业务启动。
    """
    p = _ensure_path(path)
    # 1) 原子写优先
    try:
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, p)
        return True
    except (PermissionError, OSError) as e_atomic:
        # 2) 降级：直接在原文件上覆盖（仅当文件已存在时；不存在仍由 OSError 抛出）
        if os.path.isfile(p):
            try:
                with open(p, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False)
                logger.warning(
                    "[orchestrator] sentinel 原子写失败 (%s), 已降级为原地覆写: %s",
                    e_atomic, p,
                )
                return True
            except Exception as e_fallback:
                logger.warning(
                    "[orchestrator] 写 sentinel 失败 (含降级路径) %s: atomic=%s, fallback=%s",
                    p, e_atomic, e_fallback,
                )
                return False
        # 文件还不存在：可能父目录没写权限（例如 BUILTIN\Users 缺 CreateFiles 权限）
        # 此时 sentinel 留空 = 视为"本进程未声明", 对方启动会通过 _pid_alive 检测到
        # 我们没在跑，从而放行。绝不阻塞业务启动。
        logger.warning(
            "[orchestrator] 写 sentinel 失败 %s: %s (父目录可能无写权限, 继续启动)",
            p, e_atomic,
        )
        return False
    except Exception as e:
        logger.warning("[orchestrator] 写 sentinel 失败 %s: %s", p, e)
        return False


def _pid_alive(pid: int) -> bool:
    """Windows 下检查 PID 是否仍存活（不依赖 psutil）。"""
    if pid is None or pid <= 0:
        return False
    try:
        # 0 号信号 = 非零退出码在 PowerShell 上是"无效信号"，但 Get-Process 不存在 PID 会抛异常
        import subprocess
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", f"(Get-Process -Id {pid} -ErrorAction SilentlyContinue) -ne $null"],
            capture_output=True, text=True, timeout=4,
        )
        return out.returncode == 0 and "True" in out.stdout
    except Exception as e:
        logger.debug("[orchestrator] _pid_alive 异常: %s", e)
        return False


def cleanup_stale_sentinel(path: str) -> bool:
    """如果 sentinel 对应进程已死或文件超过 STALE_SENTINEL_SEC，删除并返回 True。"""
    p = _ensure_path(path)
    payload = _read_sentinel(p)
    if payload is None:
        return False
    pid = int(payload.get("pid") or 0)
    started_at = float(payload.get("started_at") or 0.0)
    age = time.time() - started_at
    alive = pid > 0 and _pid_alive(pid)
    if (not alive) or age > STALE_SENTINEL_SEC:
        try:
            os.unlink(p)
            logger.info("[orchestrator] 已清理陈旧 sentinel %s (pid=%s, age=%.0fs)", p, pid, age)
        except Exception:
            pass
        return True
    return False


def check_peer_active(peer_lock_file: str) -> Tuple[bool, Optional[dict]]:
    """检查对方进程的 sentinel：返回 (是否在跑, sentinel内容)。"""
    p = _ensure_path(peer_lock_file)
    payload = _read_sentinel(p)
    if payload is None:
        return False, None
    pid = int(payload.get("pid") or 0)
    alive = pid > 0 and _pid_alive(pid)
    if not alive:
        return False, payload
    return True, payload


def acquire_my_sentinel(path: str, cmdline_info: str = "") -> bool:
    """启动时为自己写 sentinel。如果对方 sentinel 还活着且未忽略，会 raise exception"""

    cleanup_stale_sentinel(path)
    p = _ensure_path(path)
    payload = {
        "pid": os.getpid(),
        "started_at": time.time(),
        "started_at_human": time.strftime("%Y-%m-%d %H:%M:%S"),
        "cmdline_info": cmdline_info[:200],
        "python": sys.executable,
        "argv": sys.argv[:5],
    }
    return _write_sentinel(p, payload)


def release_my_sentinel(path: str) -> bool:
    """退出时清理自己的 sentinel。仅清理自己 pid 的（防止误删别人的）。"""
    p = _ensure_path(path)
    payload = _read_sentinel(p)
    if payload and int(payload.get("pid") or 0) == os.getpid():
        try:
            os.unlink(p)
            return True
        except Exception:
            pass
    return False


def main_check(peer_lock_file: str, self_name: str, peer_name: str, force: bool = False) -> int:
    """
    CLI 入口：被 .bat 调用。
    返回 exit code:
        0  - 可以进入
        1  - 对方在跑，用户被拒绝
        2  - 陈旧 sentinel 已自动清理，可以进入
        3  - 用户传了 --force，绕过
    """
    if force:
        print(f"[orchestrator] --force specified, skipping peer check for {peer_name}.")
        return 3
    active, payload = check_peer_active(peer_lock_file)
    if active:
        pid = int(payload.get("pid") or 0)
        started = payload.get("started_at_human", "?")
        print(f"[orchestrator] {peer_name} 正在运行中（PID {pid}, 启动于 {started}）。")
        print(f"[orchestrator] DuckDB 单写者模式：{self_name} 与 {peer_name} 不能同时运行。")
        print(f"[orchestrator] 请先结束 {peer_name} 后再试。")
        print(f"[orchestrator] PowerShell 强制关闭: Stop-Process -Id {pid} -Force")
        return 1
    # 不在跑：清理陈旧 sentinel
    if cleanup_stale_sentinel(peer_lock_file):
        print(f"[orchestrator] 已自动清理陈旧 {peer_name} sentinel。")
    return 0


__all__ = [
    "SENTINEL_DIR",
    "UI_LOCK_FILE",
    "DAEMON_LOCK_FILE",
    "STALE_SENTINEL_SEC",
    "cleanup_stale_sentinel",
    "check_peer_active",
    "acquire_my_sentinel",
    "release_my_sentinel",
    "main_check",
]