# -*- coding: utf-8 -*-
"""
小杰AI选股系统 Pro V26.88 - 侧边栏控制中枢（宽域八极雷达版）
【UI 修复与升级】：
1. 📊 雷达扩容：将领跌板块从 Bottom 5 扩容至 Bottom 8，与进攻区 Top 8 形成绝对对称，全景监控资金退潮方向。
2. 🧹 视觉精简：彻底拔除冗余的手动“市场环境预判(Regime)”下拉框，100% 交由顶部双轨雷达自动驾驶。
3. ✨ 排版优化：精简 P1/P0 底仓模式的标题文案，彻底解决侧边栏文字换行导致的视觉臃肿。
4. 强制目录收纳：P0 自选股上传文件强制写入 data/ 目录。
"""
import json
import os
import locale
import logging
import subprocess
import sys
import threading
import time
import streamlit as st
from datetime import datetime
import constants


def _project_root_ui() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _is_maintenance_mode_ui() -> bool:
    """与守护进程共用 master_control.json；维护中为 True 时应避免 UI 侧大事务写库或与 VACUUM 争用。"""
    try:
        from core.master_control import is_maintenance_mode_enabled

        return bool(is_maintenance_mode_enabled())
    except Exception:
        return False


def _load_sniper_pipeline_state_for_ui() -> dict:
    """与 auto_sniper_daemon 共用落盘文件，用于网页端观测定时增量是否曾成功。"""
    p = os.path.join(_project_root_ui(), "data", "runtime", "state", "sniper_pipeline_state.json")
    if not os.path.isfile(p):
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            o = json.load(f)
        return o if isinstance(o, dict) else {}
    except Exception:
        return {}


def _is_duckdb_windows_file_lock_error(exc: Exception) -> bool:
    """识别 Windows 下 DuckDB 主库被其它进程占用的典型报错。"""
    msg = str(exc or "").lower()
    if not msg:
        return False
    return (
        ("quant_data.duckdb" in msg and "cannot open file" in msg)
        or ("file is already open in" in msg)
        or ("另一个程序正在使用此文件" in msg)
    )


# ============== UI → daemon 让路协同 ==============
# 同一台机器上同时只允许一个 Python 进程持有 quant_data.duckdb 的「写连接」。
# daemon 启动后全程持有写连接（即使是空闲），UI 在主线程同步时必定撞锁。
# 解决：UI 写库前，先在 data/runtime/state/ui_yield_signal.json 落「让路信号」，
# daemon 在每个扫描拍之间探测到此信号后，主动关闭空闲写连接并暂停非只读作业，
# UI 同步完成后清除信号，daemon 立即恢复。

_UI_YIELD_WAIT_SEC = 25           # UI 等待 daemon 让路的总时长上限
_UI_YIELD_PROBE_INTERVAL_SEC = 1.5  # 轮询间隔


def _do_sync_with_yield_cooperation(
    sync_fn,
    update_log,
    *,
    sync_label: str = "数据同步",
    max_attempts: int = 3,
) -> bool:
    """
    包装 sync_fn(): 若执行过程中撞 DuckDB 文件锁，主动写「让路信号」并等待 daemon 释放，
    然后重试。最多重试 max_attempts 次。始终确保退出时清除信号。

    探测让路是否生效的方式 = 直接重试同步本身（避免 UI 端另外开写连接而二次撞锁）。
    """
    from core.master_control import (
        clear_ui_yield_signal,
        request_ui_yield_signal,
    )
    from data.data_fetcher import DataFetchCriticalError

    cleared = {"v": False}

    def _clear_once(tag: str) -> None:
        if cleared["v"]:
            return
        cleared["v"] = True
        try:
            clear_ui_yield_signal(reason=f"ui_{tag}")
            update_log(f"🟢 UI 让路信号已清除（{tag}）。")
        except Exception as e:
            logging.debug("清除 UI 让路信号失败（忽略）: %s", e)

    attempt = 0
    sent_signal_this_call = False
    last_send_t = 0.0
    try:
        while True:
            attempt += 1
            try:
                return bool(sync_fn())
            except DataFetchCriticalError:
                raise
            except Exception as e:
                if not _is_duckdb_windows_file_lock_error(e):
                    raise
                if attempt >= max_attempts:
                    update_log(
                        f"❌ DuckDB 主库已被占用 {max_attempts} 次重试仍失败，请先停 daemon 后再同步。"
                    )
                    raise
                # ====== 撞锁：主动让路 + 等待 =====
                # 限流：同一进程 6s 内最多重新写一次信号（避免连续写 IO）
                now = time.time()
                if not sent_signal_this_call or (now - last_send_t) > 6.0:
                    try:
                        request_ui_yield_signal(
                            reason=f"ui_{sync_label}",
                            ttl_sec=max(60, _UI_YIELD_WAIT_SEC + 30),
                        )
                        last_send_t = now
                        sent_signal_this_call = True
                        update_log(
                            f"⚠️ DuckDB 主库被 daemon 占用（第 {attempt}/{max_attempts} 次撞锁）。"
                            "已发送「让路」信号，等待 daemon 释放 quant_data.duckdb…"
                        )
                    except Exception as ee:
                        update_log(f"❌ 写让路信号失败: {ee}")

                # 等待让路生效：分两段，先快轮询（1.5s × 4），再慢轮询
                deadline = now + _UI_YIELD_WAIT_SEC
                wait_first_seg = now + 6.0  # 前 6s：每 1.5s 探一次
                last_hint_t = 0.0
                while time.time() < deadline:
                    sleep_for = _UI_YIELD_PROBE_INTERVAL_SEC
                    if time.time() > wait_first_seg:
                        sleep_for = 3.0  # 后段每 3s 探一次
                    time.sleep(sleep_for)
                    if time.time() - last_hint_t > 5.0:
                        remain = max(0, int(deadline - time.time()))
                        update_log(f"⏳ 仍在等 daemon 让路…（剩余 {remain}s）")
                        last_hint_t = time.time()
                    # 不在 UI 端探写连接（会二次撞锁）；靠下一次 attempt 自然验证
                # 跳出 → 下一轮 attempt 重试
                update_log("🔁 让路等待结束，重试同步…")
    finally:
        if sent_signal_this_call:
            _clear_once("sync_done")


def _run_compact_db_offline() -> tuple[bool, str]:
    """
    调用 tools/compact_db_offline.py 执行离线压缩。
    返回 (ok, output_text)。
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    script = os.path.join(root, "tools", "compact_db_offline.py")
    if not os.path.isfile(script):
        # 【V29 修复】tools/compact_db_offline.py 在本仓并不存在，历史脚本已下线；
        # 现役压缩入口是 db_core.duckdb_vacuum_silent()（CHECKPOINT + VACUUM，
        # daemon 晚间链与 tools/weekly_db_maintenance_orchestrated.py 都在用）。
        # 原实现直接 return False，导致侧栏「仅压缩」与「停Daemon→压缩→重启」
        # 两个按钮永久失败（st.error: 压缩失败）。
        try:
            from data.db_core import duckdb_vacuum_silent

            try:
                from data.db_core import close_db

                close_db()
            except Exception:
                pass
            ok_vac = duckdb_vacuum_silent()
            try:
                from data.db_core import duckdb_disk_bytes_total

                mb = int(duckdb_disk_bytes_total() or 0) / 1024.0 / 1024.0
            except Exception:
                mb = 0.0
            if ok_vac:
                return True, f"CHECKPOINT+VACUUM 完成，当前占用 {mb:.2f}MB"
            return False, "CHECKPOINT+VACUUM 执行失败（详见 data/runtime 日志）"
        except Exception as e:
            return False, f"压缩入口不可用: {e}"
    py = sys.executable or "python"
    try:
        cp = subprocess.run(
            [py, script],
            cwd=root,
            capture_output=True,
            timeout=1800,
        )
    except subprocess.TimeoutExpired:
        return False, "压缩超时（>30 分钟），请检查是否有进程占用 quant_data.duckdb。"
    except Exception as e:
        return False, f"启动压缩脚本失败: {e}"
    def _decode_out(b: bytes) -> str:
        if not b:
            return ""
        for enc in (
            locale.getpreferredencoding(False),
            "gbk",
            "cp936",
            "utf-8",
        ):
            try:
                return b.decode(enc)
            except Exception:
                continue
        return b.decode("utf-8", errors="replace")

    stdout_txt = _decode_out(cp.stdout or b"")
    stderr_txt = _decode_out(cp.stderr or b"")
    out = (stdout_txt + ("\n" + stderr_txt if stderr_txt else "")).strip()
    ok = (cp.returncode == 0)
    if not out:
        out = f"压缩脚本已结束，exit_code={cp.returncode}"
    return ok, out


def _precheck_db_file_lock_for_compact() -> tuple[bool, str]:
    """
    压缩前轻量探测主库是否被占用。
    返回 (ok_to_continue, message)。
    """
    try:
        from data.db_core import get_duckdb_path, get_read_conn

        dbp = get_duckdb_path()
    except Exception:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        dbp = os.path.join(root, "data", "quant_data.duckdb")

    if not os.path.isfile(dbp):
        return False, f"未检测到主库文件: {dbp}"

    try:
        # 短时只读连接：with 结束即 close，避免与守护进程长期争用同一读句柄。
        with get_read_conn(read_only=True) as con:
            con.execute("SELECT 1").fetchone()
        return True, f"预检测通过：主库可访问（{dbp}）"
    except Exception as e:
        if _is_duckdb_windows_file_lock_error(e):
            return (
                False,
                "预检测失败：quant_data.duckdb 正被其它 Python 进程占用。"
                "请先停止 daemon/其它写库进程后再压缩。",
            )
        return False, f"预检测失败：{e}"


def _list_daemon_pids_windows() -> list[int]:
    """枚举命令行包含 auto_sniper_daemon.py 的进程 PID（Windows）。"""
    ps_cmd = (
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.CommandLine -and $_.CommandLine -match 'auto_sniper_daemon.py' } | "
        "Select-Object -ExpandProperty ProcessId"
    )
    cp = subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps_cmd],
        capture_output=True,
        timeout=8,
    )
    out = (cp.stdout or b"").decode(errors="ignore")
    pids: list[int] = []
    for line in out.splitlines():
        s = line.strip()
        if s.isdigit():
            try:
                pids.append(int(s))
            except Exception:
                pass
    return sorted(set(pids))


def _stop_daemon_pids(pids: list[int]) -> tuple[bool, str]:
    if not pids:
        return True, "未检测到 daemon 进程。"
    logs: list[str] = []
    ok = True
    for pid in pids:
        killed = False
        # 通道1：taskkill（兼容性最好）
        cp = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            timeout=10,
        )
        one = ((cp.stdout or b"") + b"\n" + (cp.stderr or b"")).decode(errors="ignore").strip()
        logs.append(f"[taskkill PID {pid}] rc={cp.returncode} {one}")
        txt = one.lower()
        if cp.returncode == 0 or ("not found" in txt) or ("没有运行的任务" in one):
            killed = True
        # 通道2：PowerShell Stop-Process（taskkill 失败时兜底）
        if not killed:
            ps = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    f"Stop-Process -Id {pid} -Force -ErrorAction SilentlyContinue; "
                    f"if (Get-Process -Id {pid} -ErrorAction SilentlyContinue) {{ exit 2 }} else {{ exit 0 }}",
                ],
                capture_output=True,
                timeout=10,
            )
            two = ((ps.stdout or b"") + b"\n" + (ps.stderr or b"")).decode(errors="ignore").strip()
            logs.append(f"[Stop-Process PID {pid}] rc={ps.returncode} {two}")
            if ps.returncode == 0:
                killed = True
        if not killed:
            ok = False
    return ok, "\n".join(logs)


def _start_daemon_background() -> tuple[bool, str]:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    daemon_py = os.path.join(root, "auto_sniper_daemon.py")
    py = sys.executable or "python"
    try:
        flags = 0
        if os.name == "nt":
            flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0
            )
        # 守护进程自身已 RotatingFileHandler 写 data/runtime/sniper.log；勿再 shell 重定向以免双写撑盘
        subprocess.Popen(
            [py, daemon_py],
            cwd=root,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
            close_fds=False,
        )
        return True, f"已启动 daemon: {daemon_py}（日志见 data/runtime/sniper.log）"
    except Exception as e:
        return False, f"重启 daemon 失败: {e}"


def _run_tushare_api_probe(data_fetcher_mod) -> tuple[bool, str]:
    """
    运行高阶接口可用性探针，返回 (ok, log_text)。
    ok=True 表示至少核心接口有可用返回（不要求每个接口都有数据）。
    """
    lines: list[str] = []
    try:
        pro = getattr(data_fetcher_mod, "pro", None)
        DataFetchSkippableError = getattr(data_fetcher_mod, "DataFetchSkippableError", None)
        if pro is None:
            return False, "pro 未初始化：请先检查 Tushare Token。"
        retry_api = getattr(data_fetcher_mod, "retry_api")
        norm_date = getattr(data_fetcher_mod, "_norm_cal_date_8")
        fetch_chunked = getattr(data_fetcher_mod, "fetch_chunked")
        hithink_trade_cal = getattr(data_fetcher_mod, "_hithink_trade_cal_fallback", None)
        hithink_limit_up = getattr(data_fetcher_mod, "_hithink_limit_up_pool_fallback", None)
    except Exception as e:
        return False, f"探针初始化失败: {e}"

    try:
        from data.db_core import get_latest_daily_data_trade_date_yyyymmdd

        td = norm_date(get_latest_daily_data_trade_date_yyyymmdd() or "")
    except Exception:
        td = ""
    if not td:
        td = datetime.now().strftime("%Y%m%d")

    lines.append(f"[INFO] probe trade_date={td}")

    # 1) 交易日历基本探活
    def _mark_skippable(name: str, e: Exception) -> None:
        lines.append(f"[SKIP] {name:<13} 业务级错误（接口名/权限）：{e}")

    try:
        cal = retry_api(pro.trade_cal)(exchange="SSE", start_date=td, end_date=td)
        n = 0 if cal is None else len(cal)
        lines.append(f"[OK] trade_cal rows={n}")
    except Exception as e:
        if DataFetchSkippableError and isinstance(e, DataFetchSkippableError) and hithink_trade_cal:
            cal = hithink_trade_cal(td, td)
            n = 0 if cal is None else len(cal)
            if n > 0:
                lines.append(f"[OK] trade_cal rows={n} (HiThink fallback)")
            else:
                _mark_skippable("trade_cal", e)
        else:
            lines.append(f"[FAIL] trade_cal: {e}")

    # 2) 高阶接口（整日回退语义）
    apis = [
        ("cyq_perf", pro.cyq_perf),
        ("margin_detail", pro.margin_detail),
        ("top_inst", pro.top_inst),
    ]
    any_ok = False
    for name, fn in apis:
        try:
            df_all = retry_api(fn)(trade_date=td)
            n_all = 0 if df_all is None else len(df_all)
            lines.append(f"[OK] {name:<13} trade_date rows={n_all}")
            if n_all > 0:
                any_ok = True
        except Exception as e:
            if DataFetchSkippableError and isinstance(e, DataFetchSkippableError):
                _mark_skippable(name, e)
            else:
                lines.append(f"[FAIL] {name:<13} trade_date: {e}")

    # 涨跌停新接口（科室6主数据源）
    if hasattr(pro, "limit_list_d"):
        try:
            df_ld = retry_api(pro.limit_list_d)(trade_date=td, limit_type="U")
            n_ld = 0 if df_ld is None else len(df_ld)
            lines.append(f"[OK] {'limit_list_d(U)':<13} trade_date rows={n_ld}")
            if n_ld > 0:
                any_ok = True
        except Exception as e:
            if DataFetchSkippableError and isinstance(e, DataFetchSkippableError) and hithink_limit_up:
                df_ld, _ = hithink_limit_up(td)
                n_ld = 0 if df_ld is None else len(df_ld)
                if n_ld > 0:
                    lines.append(f"[OK] {'limit_list_d(U)':<13} trade_date rows={n_ld} (HiThink fallback)")
                    any_ok = True
                else:
                    _mark_skippable("limit_list_d(U)", e)
            else:
                lines.append(f"[FAIL] {'limit_list_d(U)':<13} trade_date: {e}")
    # 兼容老接口 limit_list 探针：tushare 该接口在 >=2.0 已弃用，limit_list_d(U) 才是
    # 生产数据源。这里仅记录提示，不让它的 SKIP/FAIL 出现在正式报告里误导用户。
    if not hasattr(pro, "limit_list"):
        lines.append("[INFO] limit_list         (已弃用, 使用 limit_list_d 替代)")
    else:
        try:
            df_ll = retry_api(pro.limit_list)(trade_date=td)
            n_ll = 0 if df_ll is None else len(df_ll)
            if n_ll > 0:
                lines.append(f"[OK] {'limit_list':<13} trade_date rows={n_ll}")
                any_ok = True
            else:
                lines.append("[INFO] limit_list         (返回空, 使用 limit_list_d 替代)")
        except Exception as e:
            # 老接口若抛业务级错误（接口名/权限），仅记录 INFO，不计入失败
            if DataFetchSkippableError and isinstance(e, DataFetchSkippableError):
                lines.append(f"[INFO] limit_list         (已弃用: {e})")
            else:
                lines.append(f"[INFO] limit_list         (网络错误, 不影响: {e})")

    # 3) ts_code 分块语义探针（模拟生产链路）
    try:
        from data.db_core import get_all_stock_codes

        codes = list(get_all_stock_codes() or [])[:800]
    except Exception:
        codes = []
    if codes:
        for name, fn in [("cyq_perf", pro.cyq_perf), ("margin_detail", pro.margin_detail)]:
            try:
                df_chunk = fetch_chunked(fn, codes, td, chunk_size=400)
                n_chunk = 0 if df_chunk is None else len(df_chunk)
                lines.append(f"[OK] {name:<13} ts_code_chunk rows={n_chunk} (codes={len(codes)})")
                if n_chunk > 0:
                    any_ok = True
            except Exception as e:
                if DataFetchSkippableError and isinstance(e, DataFetchSkippableError):
                    _mark_skippable(name + "_ts_chunk", e)
                else:
                    lines.append(f"[FAIL] {name:<13} ts_code_chunk: {e}")
    else:
        lines.append("[WARN] ts_code_chunk probe skipped: no local codes")

    return any_ok, "\n".join(lines)


def render_master_control_sidebar() -> None:
    """
    🎛️ 物理总控台：企微推送总闸 + 全自动巡航总闸。
    状态持久化至 data/runtime/state/master_control.json，与 auto_sniper_daemon 共享。

    【多标签/手工改 JSON】每次 Streamlit 重跑比较磁盘 mtime，有变化则从文件重载开关；
    另提供「从磁盘刷新」按钮强制对齐。

    【V26.88+10-2 F2-14 修复】"磁盘 mtime 对齐写入"必须发生在 widget 实例化**之前**，
    否则会触发 Streamlit 的 `st.session_state[X] cannot be modified after the widget with key X
    is instantiated` 异常。原实现里 line 470-479 的写入在 widget 创建（line 519+）之前是
    安全的，但 line 580「磁盘刷新」按钮里的 `st.session_state["mc_wechat"] = ...` 在
    widget 已经实例化后再写——必报「刷新失败: cannot be modified」。改造：
      1) 把 mtime 对齐逻辑上移到函数最顶部（在 widget 之前）作为幂等保护层；
      2) 「磁盘刷新」按钮只清 mtime 缓存 + st.rerun()，让顶部逻辑自然接管；
      3) 保留「master_control.json 缺失时按全开缺省」的回退。
    """
    from core.master_control import read_master_control, write_master_control, get_master_control_state_path

    mc_path = get_master_control_state_path()
    try:
        disk_mtime = os.path.getmtime(mc_path) if os.path.isfile(mc_path) else 0.0
    except OSError:
        disk_mtime = 0.0

    # 【F2-14 上移】磁盘 mtime 对齐必须在 widget 实例化前完成，避免
    # `cannot be modified after the widget with key mc_wechat is instantiated`。
    prev_mtime = st.session_state.get("mc_disk_mtime")
    if prev_mtime is None or float(prev_mtime) != float(disk_mtime):
        try:
            mc0 = read_master_control()
            st.session_state["mc_wechat"] = bool(mc0.get("wechat_push_enabled", True))
            st.session_state["mc_daemon"] = bool(mc0.get("daemon_auto_cruise_enabled", True))
            # 与 master_control.json 对齐；缺省 False，避免误推送
            st.session_state["push_p1_high_score_enabled"] = bool(mc0.get("push_p1_high_score_enabled", False))
            st.session_state["mc_disk_mtime"] = disk_mtime
        except Exception as _e_mc_top:
            # 读 master_control.json 失败时，按缺省全开兜底（不写入，避免触雷 widget）
            st.session_state.setdefault("mc_wechat", True)
            st.session_state.setdefault("mc_daemon", True)
            st.session_state.setdefault("push_p1_high_score_enabled", False)

    # 升级后旧会话可能缺少该键：补一次缺省，避免 toggle 报错
    if "push_p1_high_score_enabled" not in st.session_state:
        try:
            _mc_fill = read_master_control()
            st.session_state["push_p1_high_score_enabled"] = bool(_mc_fill.get("push_p1_high_score_enabled", False))
        except Exception:
            st.session_state["push_p1_high_score_enabled"] = False

    # 【F2-14】从顶部移除原本 470-479 的 mtime 对齐块；现在它已经在函数入口执行。
    # 【F2-14】保留旧块作为兜底：如果某些代码路径绕过了顶部（如 rerun 中断），再补一次。

    st.sidebar.markdown(
        """
<div style="text-align:center;font-weight:700;font-size:1.06rem;margin:0 0 0.25rem 0;line-height:1.35;">
🎛️ 系统总控中心<br/>
<span style="font-size:0.78rem;font-weight:500;color:#64748b;">(Master Control)</span>
</div>
""",
        unsafe_allow_html=True,
    )
    st.sidebar.divider()
    st.sidebar.caption("跨进程总闸 · 与后台 Daemon 共用同一状态文件")

    def _persist_master_control() -> None:
        write_master_control(
            wechat_push_enabled=bool(st.session_state.get("mc_wechat", True)),
            daemon_auto_cruise_enabled=bool(st.session_state.get("mc_daemon", True)),
            push_p1_high_score_enabled=bool(st.session_state.get("push_p1_high_score_enabled", False)),
        )
        try:
            st.session_state["mc_disk_mtime"] = os.path.getmtime(mc_path)
        except OSError:
            st.session_state["mc_disk_mtime"] = time.time()
        try:
            st.toast("总控状态已写入 master_control.json", icon="🎛️")
        except Exception:
            try:
                st.sidebar.success("已同步总控状态")
            except Exception:
                pass

    _toggle = getattr(st.sidebar, "toggle", None)
    if _toggle is not None:
        _toggle(
            "🔔 企微实盘推送",
            key="mc_wechat",
            on_change=_persist_master_control,
            help="总闸关闭后，本机任何进程均不会向企微 Webhook 发出消息。",
        )
    else:
        st.sidebar.checkbox(
            "🔔 企微实盘推送",
            key="mc_wechat",
            on_change=_persist_master_control,
            help="总闸关闭后，本机任何进程均不会向企微 Webhook 发出消息。",
        )
    st.sidebar.markdown(
        "<div style='font-size:12px;color:#64748b;margin:-6px 0 10px 0;'>开启后允许发送信号至手机</div>",
        unsafe_allow_html=True,
    )

    if _toggle is not None:
        _toggle(
            "🤖 24H 全自动巡航",
            key="mc_daemon",
            on_change=_persist_master_control,
            help="关闭后，独立守护进程将跳过下载、P1 重建与 P3/P4/P5 扫描。开启时：休市日（周末/法定节假日）自动跳过同步与企微推送。",
        )
    else:
        st.sidebar.checkbox(
            "🤖 24H 全自动巡航",
            key="mc_daemon",
            on_change=_persist_master_control,
            help="关闭后，独立守护进程将跳过下载、P1 重建与 P3/P4/P5 扫描。开启时：休市日（周末/法定节假日）自动跳过同步与企微推送。",
        )
    st.sidebar.markdown(
        "<div style='font-size:12px;color:#64748b;margin:-6px 0 8px 0;'>"
        "开启后台 Daemon 的自动下载与扫描。<br/>休市日自动跳过同步与推送"
        "</div>",
        unsafe_allow_html=True,
    )

    if _toggle is not None:
        _toggle(
            "🌟 推送 P1 高分池 (≥75分)",
            key="push_p1_high_score_enabled",
            on_change=_persist_master_control,
            help="开启后，在全量洗盘结束后自动将 75 分以上的底仓股票推送至企业微信。",
        )
    else:
        st.sidebar.checkbox(
            "🌟 推送 P1 高分池 (≥75分)",
            key="push_p1_high_score_enabled",
            on_change=_persist_master_control,
            help="开启后，在全量洗盘结束后自动将 75 分以上的底仓股票推送至企业微信。",
        )
    st.sidebar.markdown(
        "<div style='font-size:12px;color:#64748b;margin:-6px 0 8px 0;'>"
        "开启后，在全量洗盘结束后自动将 75 分以上的底仓股票推送至企业微信。"
        "</div>",
        unsafe_allow_html=True,
    )

    c1, c2 = st.sidebar.columns(2)
    with c1:
        if st.button("🔄 磁盘刷新", key="mc_btn_refresh_disk", help="从 master_control.json 强制重载（多标签/手工改文件）"):
            # 【V26.88+10-2 F2-14 修复】按钮里禁止直接写 mc_* session_state——widget
            # 已经在 line 519+ 实例化，再写会触发 `cannot be modified after the widget
            # with key mc_wechat is instantiated`。改为只清 mtime 缓存 + st.rerun()，
            # 让函数顶部对齐逻辑（line 484+）自然接管、且在 widget 实例化前完成写入。
            st.session_state.pop("mc_disk_mtime", None)
            try:
                # 读 master_control.json 一次以确认文件可达，失败时给出明确错误
                _probe = read_master_control()
                _ = bool(_probe.get("wechat_push_enabled", True))
            except Exception as ex:
                st.sidebar.error(f"刷新失败: {ex}")
                try:
                    st.toast(f"磁盘刷新失败: {ex}", icon="⚠️")
                except Exception:
                    pass
            else:
                try:
                    st.toast("已从磁盘重载总控状态", icon="🔄")
                except Exception:
                    pass
                st.rerun()
    with c2:
        st.caption(f"修改时间：`{disk_mtime:.0f}`" if disk_mtime else "`（无文件·缺省全开）`")

    # 与 app.py 扫描推送开关对齐（notify_scan_results_top3_p2p3p4 仍读此键）
    st.session_state["wechat_notify_enabled"] = bool(st.session_state.get("mc_wechat", True))

    # ---------- 风控模式：config.yaml risk_control.ui_alert_only（与引擎 get_risk_control_config 联动）----------
    try:
        from core.config_manager import (
            get_config_yaml_path,
            get_ui_alert_only,
            write_ui_alert_only_to_config_yaml,
            invalidate_config_cache,
        )

        _cfg_yaml_path = get_config_yaml_path()
        try:
            _mt_risk_cfg = os.path.getmtime(_cfg_yaml_path) if os.path.isfile(_cfg_yaml_path) else 0.0
        except OSError:
            _mt_risk_cfg = 0.0
        _prev_risk_mt = st.session_state.get("cfg_yaml_mtime_ui_alert")
        if _prev_risk_mt is None or float(_prev_risk_mt) != float(_mt_risk_cfg):
            st.session_state["rc_ui_alert_only"] = bool(get_ui_alert_only(force_reload=True))
            st.session_state["cfg_yaml_mtime_ui_alert"] = _mt_risk_cfg
        if "rc_ui_alert_only" not in st.session_state:
            st.session_state["rc_ui_alert_only"] = bool(get_ui_alert_only(force_reload=True))

        st.sidebar.markdown(
            '<div style="text-align:center;font-weight:600;font-size:0.95rem;margin:0.4rem 0 0.15rem 0;">'
            "⚙️ 风控模式</div>",
            unsafe_allow_html=True,
        )
        st.sidebar.caption("写入配置文件中的风控开关，下一轮扫描生效")

        def _persist_risk_ui_alert_only() -> None:
            v = bool(st.session_state.get("rc_ui_alert_only", True))
            if write_ui_alert_only_to_config_yaml(v):
                invalidate_config_cache()
                try:
                    st.session_state["cfg_yaml_mtime_ui_alert"] = os.path.getmtime(_cfg_yaml_path)
                except OSError:
                    st.session_state["cfg_yaml_mtime_ui_alert"] = time.time()
                try:
                    st.toast("风控配置已切换", icon="✅")
                except Exception:
                    try:
                        st.sidebar.success("风控配置已切换")
                    except Exception:
                        pass
                # toggle 的 on_change 本轮已触发重绘，无需再手动 rerun（可减少前端重复卸载节点风险）。
            else:
                try:
                    st.sidebar.error("写入 config.yaml 失败（请检查文件权限与磁盘）")
                except Exception:
                    pass

        _rc_toggle = getattr(st.sidebar, "toggle", None)
        _rc_label = "⚠️ 仅界面预警（不拦出票）"
        _rc_help = "开：红线只打预警标签、不拦出票；关：触线即硬否决。会写入配置文件。"
        if _rc_toggle is not None:
            _rc_toggle(
                _rc_label,
                key="rc_ui_alert_only",
                on_change=_persist_risk_ui_alert_only,
                help=_rc_help,
            )
        else:
            st.sidebar.checkbox(
                _rc_label,
                key="rc_ui_alert_only",
                on_change=_persist_risk_ui_alert_only,
                help=_rc_help,
            )
        st.sidebar.markdown(
            "<div style='font-size:12px;color:#64748b;margin:-6px 0 10px 0;'>"
            "开＝仅预警　关＝触线拦票</div>",
            unsafe_allow_html=True,
        )
    except Exception as _risk_ex:
        st.sidebar.caption(f"风控开关不可用: {_risk_ex}")

    st.sidebar.divider()


def _fmt_size_mb(byte_size):
    try:
        return f"{(float(byte_size) / (1024 * 1024)):.2f} MB"
    except Exception:
        return "--"


@st.cache_data(ttl=60)
def _cached_sidebar_daily_data_rowcounts() -> tuple[int, int]:
    """
    高频大表统计：仅缓存 (总行数, 代码数) 元组，绝不缓存数据库连接。
    返回 (-1, -1) 表示缺表或查询失败。
    """
    try:
        from data.db_core import get_read_conn

        with get_read_conn(read_only=True) as con:
            row = con.execute("SELECT COUNT(*) FROM daily_data").fetchone()
            codes = con.execute("SELECT COUNT(DISTINCT ts_code) FROM daily_data").fetchone()
        tr = int(row[0]) if row and row[0] is not None else 0
        tc = int(codes[0]) if codes and codes[0] is not None else 0
        return (tr, tc)
    except Exception:
        return (-1, -1)


def _get_db_snapshot_cached(ttl_sec=300):
    """
    读取 DuckDB 健康快照（带 TTL 缓存），避免每次 UI 重绘都触发重查询。
    返回：
    {
      ok, db_size_bytes, total_rows, total_codes, quality_tag, quality_hint, error
    }
    """
    now_ts = time.time()
    cache = st.session_state.get("sidebar_db_snapshot_cache")
    if cache and (now_ts - cache.get("ts", 0) < ttl_sec):
        return cache.get("data", {})

    snapshot = {
        "ok": False,
        "db_size_bytes": 0,
        "total_rows": 0,
        "total_codes": 0,
        "quality_tag": "未知",
        "quality_hint": "未完成检测",
        "error": "",
    }
    try:
        from data.db_core import duckdb_disk_bytes_total, get_duckdb_path, table_exists

        db_file = get_duckdb_path()
        if os.path.exists(db_file):
            snapshot["db_size_bytes"] = duckdb_disk_bytes_total()

        tr, tc = _cached_sidebar_daily_data_rowcounts()
        if tr < 0 or tc < 0:
            if not table_exists("daily_data"):
                snapshot["quality_tag"] = "缺表"
                snapshot["quality_hint"] = "未找到 daily_data"
            else:
                snapshot["quality_tag"] = "异常"
                snapshot["quality_hint"] = "读取失败，请检查连接"
                snapshot["error"] = "daily_data 统计失败"
        else:
            snapshot["total_rows"] = tr
            snapshot["total_codes"] = tc
            snapshot["ok"] = True

            if snapshot["total_rows"] >= 200000 and snapshot["total_codes"] >= 1500:
                snapshot["quality_tag"] = "优"
                snapshot["quality_hint"] = "覆盖充足，可直接洗盘"
            elif snapshot["total_rows"] >= 80000 and snapshot["total_codes"] >= 800:
                snapshot["quality_tag"] = "中"
                snapshot["quality_hint"] = "可运行，建议盘后补全"
            else:
                snapshot["quality_tag"] = "低"
                snapshot["quality_hint"] = "样本偏少，建议先补数"
    except Exception as e:
        snapshot["quality_tag"] = "异常"
        snapshot["quality_hint"] = "读取失败，请检查连接"
        snapshot["error"] = str(e)

    st.session_state["sidebar_db_snapshot_cache"] = {"ts": now_ts, "data": snapshot}
    return snapshot


def render_data_foundation_expander():
    """数据底座手动工具区；定时链由 auto_sniper_daemon 执行（见 data/runtime/state/daemon_public_meta.json）。"""
    try:
        import data.fetcher as data_fetcher  # V26.88 拆分：shim 自动注册 sys.modules['data.data_fetcher']
        from data.fetcher import DataFetchCriticalError
    except Exception as e:
        st.sidebar.error(f"❌ 数据模块加载失败: {e}")
        return

    with st.expander("💾 数据底座", expanded=True):
        _stp = _load_sniper_pipeline_state_for_ui()
        _sync_ok = str(_stp.get("last_sync_ok_bj_date") or "").strip()
        _p1_ok = str(_stp.get("last_p1_ok_bj_date") or "").strip()
        _sync_fail = str(_stp.get("last_sync_fail_bj_date") or "").strip()
        if _sync_ok or _p1_ok or _sync_fail:
            st.caption(
                f"链：同步 {_sync_ok or '—'} · P1 {_p1_ok or '—'}"
                + (f" · 同步失败 {_sync_fail}" if _sync_fail else "")
            )
        else:
            st.caption("⚠ 无守护落盘（请运行 auto_sniper_daemon）")

        db_snap = _get_db_snapshot_cached(ttl_sec=300)
        db_ok = bool(db_snap.get("ok"))
        size_text = _fmt_size_mb(db_snap.get("db_size_bytes", 0)) if db_ok else "--"
        rows_text = f"{db_snap.get('total_rows', 0):,}" if db_ok else "--"
        codes_text = f"{db_snap.get('total_codes', 0):,}" if db_ok else "--"
        tag_text = db_snap.get("quality_tag", "暂不可用") if db_ok else "暂不可用"
        hint_text = db_snap.get("quality_hint", "") if db_ok else ""
        st.markdown(
            f"""
<div style='background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;padding:8px 10px;line-height:1.45;font-size:12px;'>
  <div style='margin:2px 0;'>📁 {size_text} · 📊 {rows_text} · 🎯 {codes_text} · 🧪 {tag_text}{(' · ' + hint_text) if hint_text else ''}</div>
</div>
""",
            unsafe_allow_html=True,
        )
        if db_snap.get("error"):
            st.caption("数据库快照暂不可用，请检查 daemon 或维护状态")

        if _is_maintenance_mode_ui():
            st.error(
                "🚨 **维护模式已开启**：后台守护已暂停写库与同步 / P1–P5 链。"
                "请勿执行下载或压缩，直至运维控制台提示维护完成。"
            )
            if st.button(
                "🔓 强制解除维护锁（仅当维护脚本已退出仍误锁时）",
                key="sidebar_force_clear_maintenance",
                help="写入 maintenance_mode=false；误点可能导致与正在运行的 VACUUM 争锁，请确认维护窗口已结束。",
            ):
                try:
                    from core.master_control import write_master_control

                    write_master_control(maintenance_mode=False)
                    st.session_state.pop("force_maintenance_dispatched", None)
                    st.success("已解除维护锁，请刷新本页后重试。")
                except Exception as _e_clr:
                    st.error(f"解除失败: {_e_clr}")
            st.divider()
            return

        if st.session_state.get("force_maintenance_dispatched"):
            st.warning("⚠️ 维护程序已投递，请等待脚本结束后再操作压缩与同步。")

        def _render_panel_header(title: str, hint: str) -> None:
            st.markdown(
                f"<div style='font-weight:700;font-size:0.98rem;margin:0.35rem 0 0.15rem 0;'>{title}</div>",
                unsafe_allow_html=True,
            )
            st.caption(hint)

        _render_panel_header("🧪 1. 数据健康检查", "只读诊断，不改库；优先判断是否真的需要同步或维护。")
        c1, c2 = st.columns(2)
        with c1:
            if st.button("🔍 缺失查询", width="stretch", key="sidebar_btn_missing_check"):
                status_box = st.empty()
                status_box.markdown(
                    "<div style='background-color:#f8f9fa;padding:12px;border-radius:6px;margin:10px 0;color:#6c757d;font-size:14px;'>🔄 正在比对交易日历与本地库...</div>",
                    unsafe_allow_html=True,
                )
                try:
                    # [V26.88+9-29 ST-DL-N] 改用「库起点」语义查询缺失，避免误报项目上线前的远古老日期。
                    success, missing_dates = data_fetcher.check_data_completeness_with_db_origin(
                        max_days=int(getattr(constants, "MAX_DAYS", 150) or 150),
                        status_callback=lambda m: None,
                    )
                    if success:
                        st.session_state["missing_dates"] = missing_dates
                        st.session_state["query_done"] = True
                    else:
                        st.error("❌ 无法连接 Tushare，请检查网络或 Token。")
                        st.session_state["missing_dates"] = []
                        st.session_state["query_done"] = False
                except DataFetchCriticalError as e:
                    st.error(f"🚨 实盘熔断：无法比对交易日历（企微已告警）。{e}")
                except Exception as e:
                    st.error(f"检查异常: {e}")
                status_box.empty()
        with c2:
            if st.button("🧪 接口探针", width="stretch", key="sidebar_btn_api_probe"):
                with st.spinner("正在执行 Tushare 接口探针..."):
                    ok_probe, probe_log = _run_tushare_api_probe(data_fetcher)
                st.code((probe_log or "")[-7000:], language="log")
                if ok_probe:
                    st.success("✅ 接口探针完成：至少一条高阶接口返回了有效数据。")
                else:
                    st.warning("💡 探针未拿到高阶有效数据，请检查 Token 权限/额度/时间窗。")

        missing_dates = st.session_state.get("missing_dates", [])
        query_done = st.session_state.get("query_done", False)
        if query_done:
            if not missing_dates:
                st.markdown(
                    "<div style='background-color:#e6f4ea;padding:12px;border-radius:6px;margin:10px 0;color:#10b981;font-size:14px;font-weight:bold;'>✅ 150天数据完备，滴水不漏！</div>",
                    unsafe_allow_html=True,
                )
            else:
                st.markdown(
                    f"<div style='background-color:#fdfadd;border:1px solid #ffeeba;padding:12px;border-radius:6px;margin:10px 0;color:#856404;font-size:14px;'>⚠️ 发现缺失 <span style='font-weight:bold;'>{len(missing_dates)}</span> 天：</div>",
                    unsafe_allow_html=True,
                )
                st.markdown(
                    f"<div style='background-color:#e2e8f0;padding:12px;border-radius:6px;margin:10px 0;color:#004085;font-size:14px;'>{', '.join(missing_dates[:5])}{'...' if len(missing_dates) > 5 else ''}</div>",
                    unsafe_allow_html=True,
                )

        st.markdown(
            """
            <style>
            div[data-testid="stExpander"] div[data-testid="stMetricValue"] {
                font-size: 0.98rem !important;
                line-height: 1.05 !important;
            }
            div[data-testid="stExpander"] div[data-testid="stMetricDelta"] {
                font-size: 0.55rem !important;
            }
            div[data-testid="stExpander"] div[data-testid="stMetric"] label {
                font-size: 0.64rem !important;
            }
            </style>
            """,
            unsafe_allow_html=True,
        )
        st.markdown("---")
        st.caption("申万板块趋势诊断：只读诊断不会改库；同步按钮先出预览，确认后才真正拉取申万行业维表和 L1 日线。")
        sw_c1, sw_c2 = st.columns(2)
        with sw_c1:
            sw_do_diag = st.button("🔎 只读诊断", width="stretch", key="sidebar_btn_sw_sector_diag")
        with sw_c2:
            sw_do_sync = st.button("📡 同步诊断", width="stretch", key="sidebar_btn_sw_sector_sync_diag")
        # 【V30.14 2026-10-18】同步诊断 dry-run 预览 + 二次确认。
        # 背景：这是侧栏里唯一会真实调 Tushare API 并改库（dim_sw_sector / sw_sector_daily）
        # 的按钮，误点代价不高但完全没有反馈，容易困惑。
        # 现在：点「📡 同步诊断」只出预览（当前覆盖情况 + 本次会做什么 + 会消耗的接口点数），
        # 必须在预览面板里再点「✅ 确认同步」才会真正同步；「✖ 取消」直接丢弃。
        # 只改交互形态，不改任何同步参数、接口、阈值或落库逻辑；
        # 不设任何环境变量时行为与 V30.13 之前完全一致（只多一次确认点击）。
        sw_sync_confirm_key = "_sw_sync_confirm_pending"
        sw_sync_do_key = "_sw_sync_execute_pending"
        sw_sync_preview = None
        sw_diag = None
        sw_confirm = False
        sw_cancel = False
        # 【V30.14】确认动作先记进 session_state 再 st.rerun()，由下一轮脚本 pop 出来执行。
        # st.rerun() 会抛 RerunException 中断本轮，所以不能在同一轮里「先 rerun 再执行」。
        if st.session_state.pop(sw_sync_do_key, False):
            try:
                from data.db_core import sync_and_diagnose_sw_sector

                with st.spinner("正在同步申万行业维表与 L1 日线，并生成诊断..."):
                    sw_diag = sync_and_diagnose_sw_sector(level="L1")
                st.success("✅ 申万行业维表与 L1 日线同步完成。")
            except Exception as e:
                st.error(f"申万板块同步诊断失败: {e}")
        if sw_do_sync:
            st.session_state[sw_sync_confirm_key] = True
        if st.session_state.get(sw_sync_confirm_key):
            try:
                from data.db_core import diagnose_sw_sector_coverage

                with st.spinner("正在生成同步预览（只读，不调接口、不改库）..."):
                    sw_sync_preview = diagnose_sw_sector_coverage()
            except Exception as e:
                st.error(f"申万板块同步预览生成失败: {e}")
                sw_sync_preview = None
            st.markdown("---")
            st.markdown("**📡 同步预览（尚未执行任何写入）**")
            if isinstance(sw_sync_preview, dict):
                pv_counts = sw_sync_preview.get("counts", {}) if isinstance(sw_sync_preview.get("counts"), dict) else {}
                pv_match = sw_sync_preview.get("match", {}) if isinstance(sw_sync_preview.get("match"), dict) else {}
                st.caption(
                    "点下面「✅ 确认同步」才会真实调用 Tushare 接口并写入 dim_sw_sector / sw_sector_daily。"
                )
                pv_rows = [
                    {"项目": "申万 L1 行业数", "当前值": int(pv_counts.get("sw_l1_count", 0) or 0)},
                    {"项目": "申万日线行数", "当前值": int(pv_counts.get("sw_sector_daily_rows", 0) or 0)},
                    {
                        "项目": "申万日线区间",
                        "当前值": "%s → %s" % (
                            str(pv_counts.get("sw_sector_daily_min_date", "") or "—"),
                            str(pv_counts.get("sw_sector_daily_max_date", "") or "—"),
                        ),
                    },
                    {"项目": "系统行业数", "当前值": int(pv_counts.get("system_industry_count", 0) or 0)},
                    {
                        "项目": "精确匹配率",
                        "当前值": "%.1f%%" % (float(pv_match.get("exact_hit_rate", 0.0) or 0.0) * 100),
                    },
                ]
                st.dataframe(pv_rows, width="stretch", hide_index=True)
                st.markdown(
                    """
                    <div style='background-color:#eef2ff;border:1px solid #c7d2fe;padding:10px;border-radius:6px;margin:8px 0;color:#3730a3;font-size:13px;'>
                    本次同步会做：<br/>
                    ① 重新拉取申万 L1+L2 行业维表 → 覆盖写 <code>dim_sw_sector</code><br/>
                    ② 重新拉取申万 L1 行业日线（近 180 天）→ 覆盖写 <code>sw_sector_daily</code><br/>
                    ③ 重新生成覆盖率诊断（含行业映射建议）<br/>
                    数据内容以 Tushare 返回为准；同一天重复同步只会刷新 <code>updated_at</code>，业务列内容不变。
                    </div>
                    """,
                    unsafe_allow_html=True,
                )
            sw_c3, sw_c4 = st.columns(2)
            with sw_c3:
                sw_confirm = st.button("✅ 确认同步", width="stretch", type="primary", key="sidebar_btn_sw_sector_sync_confirm")
            with sw_c4:
                sw_cancel = st.button("✖ 取消", width="stretch", key="sidebar_btn_sw_sector_sync_cancel")
            if sw_cancel or sw_confirm:
                st.session_state[sw_sync_confirm_key] = False
            if sw_cancel:
                st.rerun()
            if sw_confirm:
                st.session_state[sw_sync_do_key] = True
                st.rerun()
        else:
            sw_diag = None
            if sw_do_diag:
                try:
                    from data.db_core import diagnose_sw_sector_coverage

                    with st.spinner("正在诊断申万行业 K 线覆盖率..."):
                        sw_diag = diagnose_sw_sector_coverage()
                except Exception as e:
                    st.error(f"申万板块诊断失败: {e}")

        if isinstance(sw_diag, dict):
            sync_info = sw_diag.get("sync") if isinstance(sw_diag.get("sync"), dict) else {}
            if sync_info:
                st.caption(
                    f"同步结果：维表={bool(sync_info.get('basic_ok'))} · 日线={bool(sync_info.get('daily_ok'))} · level={sync_info.get('level', 'L1')}"
                )
            counts = sw_diag.get("counts", {}) if isinstance(sw_diag.get("counts"), dict) else {}
            match = sw_diag.get("match", {}) if isinstance(sw_diag.get("match"), dict) else {}
            m1, m2, m3 = st.columns(3)
            m1.metric("申万日线行数", f"{int(counts.get('sw_sector_daily_rows', 0) or 0):,}")
            m2.metric("趋势上下文", int(sw_diag.get("trend_context_count", 0) or 0))
            m3.metric(
                "匹配率",
                f"{float(match.get('effective_hit_rate', match.get('exact_hit_rate', 0.0)) or 0.0) * 100:.1f}%",
                delta=f"精确 {float(match.get('exact_hit_rate', 0.0) or 0.0) * 100:.1f}%",
            )
            date_min = str(counts.get("sw_sector_daily_min_date", "") or "—")
            date_max = str(counts.get("sw_sector_daily_max_date", "") or "—")
            st.caption(f"申万日线区间：{date_min} → {date_max}")
            top_bonus = sw_diag.get("trend_bonus_top", []) or []
            if top_bonus:
                st.markdown("**趋势加分 Top**")
                top_bonus_zh = []
                for row in top_bonus[:10]:
                    if not isinstance(row, dict):
                        continue
                    top_bonus_zh.append({"行业": row.get("industry", ""), "趋势加分": row.get("trend_bonus", 0.0), "来源": row.get("source", "")})
                st.dataframe(top_bonus_zh, width="stretch", hide_index=True)
            suggestions = sw_diag.get("suggestions", []) or []
            if suggestions:
                st.warning("\n".join(f"- {x}" for x in suggestions[:6]))
            mapping_suggestions = sw_diag.get("mapping_suggestions", {}) if isinstance(sw_diag.get("mapping_suggestions"), dict) else {}
            if mapping_suggestions:
                st.markdown("**自动映射建议（系统行业 → 申万行业）**")
                rows = []
                auto_map = {}
                for sys_ind, cands in mapping_suggestions.items():
                    if not cands:
                        continue
                    best = cands[0]
                    rows.append({"系统行业": sys_ind, "建议申万行业": best.get("sw_industry", ""), "置信分": best.get("score", 0.0)})
                    if float(best.get("score", 0.0) or 0.0) >= 0.55:
                        auto_map[str(sys_ind)] = str(best.get("sw_industry", ""))
                if rows:
                    st.dataframe(rows[:30], width="stretch", hide_index=True)
                if auto_map and st.button("💾 保存高置信映射", width="stretch", key="sidebar_btn_save_sw_mapping"):
                    try:
                        from data.db_core import save_sw_industry_name_mapping

                        if save_sw_industry_name_mapping(auto_map):
                            st.success(f"已保存 {len(auto_map)} 条行业映射。重新诊断后匹配率会更新。")
                        else:
                            st.warning("没有可保存的映射。")
                    except Exception as e:
                        st.error(f"保存申万行业映射失败: {e}")
            unmatched = sw_diag.get("unmatched_industries", []) or []
            if unmatched:
                st.caption("未精确命中行业：" + "、".join([str(x) for x in unmatched[:20]]))
            with st.expander("查看完整诊断 JSON", expanded=False):
                st.json(sw_diag)
            if sw_diag.get("ok"):
                st.success("✅ 申万行业 K 线趋势层可用，P1 板块趋势加分将优先使用真实行业指数。")
            else:
                st.info("当前仍可能回退到 sector_rank_history 排名近似逻辑。")

        _render_panel_header("📥 2. 数据同步", "空库走全量历史；已有库优先做增量补缺，不触发重铸。")
        # [V26.88+9-29 ST-DL-N] 删 radio 选项，仅保留「轻量补缺」单一入口。
        # 原因：原「全量重建」走 sync_history(days=150) → 全表 DROP + INSERT 重铸 daily_data，
        # 风险高、耗时长，且 150 天窗口会强制覆盖「项目起点之前」的远古老日期。
        # UI 端的「轻量补缺」会自动调用 check_data_completeness 并修复真缺，不再需要手动选。
        # 真要做「清空库全量重建」请用 tools/compact_db_offline.py 或直接删 data/quant_data.duckdb 后重启 daemon。
        st.caption("数据同步：自动补齐缺失交易日（仅在库起点之后），不重铸 daily_data。若需彻底重建，请使用 tools/compact_db_offline.py 离线压缩或手动备份后删库重启。")

        if st.button("📥 执行同步", width="stretch", key="sidebar_btn_sync_entry", type="primary"):
            if _is_maintenance_mode_ui():
                st.error("维护模式中：已暂停 UI 侧同步，请待维护结束后再试。")
            else:
                log_container = st.empty()
                # [V26.88+9-29 ST-UI-N] 日志持久化：用 st.session_state 而不是闭包 list，
                # 让 Streamlit rerun（用户切 tab 回来）后仍能保留本次同步日志。
                # [V26.88+9-29 ST-UI-F] 防御：update_log 可能在并发线程回调中被调用（如
                # sync_missing_days_batch 的并发下载）。Streamlit 文档明令：不要在异步线程
                # 访问 st.session_state——必须先在主线程初始化并赋 default，callback 内
                # 用 get+setdefault 兜底访问，避免并发 KeyError 把日志「静默失败」。
                if "data_sync_log_history" not in st.session_state:
                    st.session_state["data_sync_log_history"] = []

                def update_log(msg):
                    try:
                        # 并发回调兜底：可能 Streamlit 在 callback 期间尚未初始化或清理 session_state
                        hist = st.session_state.get("data_sync_log_history")
                        if hist is None:
                            st.session_state["data_sync_log_history"] = []
                            hist = st.session_state["data_sync_log_history"]
                        hist.append(str(msg))
                        # log_container 在 rerun 后可能失效（empty() 创建的新对象），try 兜底
                        try:
                            log_container.code(
                                "\n".join(hist[-15:]),
                                language="log",
                            )
                        except Exception:
                            pass
                    except Exception:
                        # 最末层兜底：日志绝不能阻断同步主流程
                        import logging as _lg
                        _lg.debug("update_log 异常被吞: %r", msg)

                # 同步内部动作全部封装到一个回调，让 UI→daemon 让路协同可整体重试
                def _do_sync() -> bool:
                    # [V26.88+9-29 ST-DL-N] 删「全量重建」分支，仅保留「轻量补缺」。
                    # 窗口：lookback_days = MAX_DAYS（默认 150），但通过 check_data_completeness_with_db_origin
                    # 改用「以库起点为基准」查缺失，避免误报「项目上线前」的远古老日期。
                    lookback_days = int(getattr(constants, "MAX_DAYS", 150) or 150)
                    ok, missing = data_fetcher.check_data_completeness_with_db_origin(
                        max_days=lookback_days,
                        status_callback=update_log,
                    )
                    missing = [str(d) for d in (missing or []) if str(d)]
                    if not ok:
                        update_log("⚠️ 交易日历校验未通过，已跳过同步。")
                        return False
                    if not missing:
                        update_log("✨ 本地数据已完整，仅执行特征修补。")
                        data_fetcher._sync_daily_features(update_log)
                        return True
                    update_log(
                        f"检测到缺失 {len(missing)} 个交易日：先下载全部生肉，再统一保存、同步和重算特征。"
                    )
                    # 【V26.88+10-1 ST-UI-N3】文本流替代 progress_bar：原进度条单日补缺 0→0 跳变
                    # 用户怀疑卡死；改为基于比率的百分数 + emoji 动画（⠋ ⠙ ⠸ ⠴ ⠦ ⠇），体感更顺。
                    total_n = len(missing)
                    progress_box = st.empty()
                    def _progress_text(ratio: float) -> None:
                        n_done = int(round(ratio * total_n)) if ratio > 0 else 0
                        anims = ["⠋", "⠙", "⠸", "⠴", "⠦", "⠇"]
                        anim = anims[int(round(ratio * 5)) % 6] if 0 < ratio < 1 else ("✅" if ratio >= 1 else "⏳")
                        progress_box.markdown(
                            f"<div style='background-color:#e2e8f0;padding:10px;border-radius:6px;margin:6px 0;'>"
                            f"{anim} 进度 {n_done}/{total_n}（{ratio*100:5.1f}%）</div>",
                            unsafe_allow_html=True,
                        )
                    batch_result = data_fetcher.sync_missing_days_batch(
                        missing,
                        status_callback=update_log,
                        progress_callback=_progress_text,
                    )
                    failed_days = list(batch_result.get("failed") or [])
                    downloaded_days = list(batch_result.get("downloaded") or [])
                    if failed_days:
                        st.warning(
                            f"⚠️ 自动补缺完成，但仍有 {len(failed_days)} 天失败："
                            + ",".join(failed_days[:8])
                            + ("..." if len(failed_days) > 8 else "")
                        )
                    else:
                        # [V26.88+10-8 修复] sync_missing_days_batch 当前实现不返回 rows 字段
                        # (只返回 downloaded/failed). rows 信息从前面的 [轻量 UPSERT] 实时日志
                        # 即可看到. 这里做兼容: 优先用 rows 字段, 缺失时退化为 0.
                        update_log(
                            f"✅ 自动补缺完成：统一写入 {len(downloaded_days)} 个交易日、"
                            f"{int(batch_result.get('rows') or 0):,} 行。"
                        )
                    return not failed_days

                try:
                    _do_sync_with_yield_cooperation(
                        _do_sync,
                        update_log,
                        sync_label="data_sync",
                        max_attempts=3,
                    )
                    st.success("✅ 数据同步流程已完成。")
                except DataFetchCriticalError as e:
                    st.error(f"🚨 实盘熔断：行情拉取不可用，本轮已终止（企微已告警）。{e}")
                except Exception as e:
                    if _is_duckdb_windows_file_lock_error(e):
                        st.error(
                            "❌ 主库 quant_data.duckdb 正被其它 Python 进程占用（通常是 daemon）。"
                            "已尝试 3 次「让路」等待仍失败，请先停 daemon 后再同步，"
                            "或改用 daemon 定时晚间链自动同步。"
                        )
                    else:
                        st.error(f"同步失败: {e}")
                    try:
                        from core.notification_gateway import notify_wechat_system_alert

                        notify_wechat_system_alert(
                            title="UI 数据同步异常",
                            detail=str(e)[:900],
                            category="data_sync",
                            dedup_key="ui_sidebar_sync_entry_exc",
                        )
                    except Exception:
                        pass

        _render_panel_header("🧹 3. 数据维护", "维护类操作集中收口；建议先停 daemon，再执行压缩。")
        col_a, col_b = st.columns(2)
        with col_a:
            if st.button("🧹 仅压缩", width="stretch", key="sidebar_btn_compact"):
                if _is_maintenance_mode_ui():
                    st.error("维护模式中：请等待 force_maintenance_vacuum 结束后再压缩。")
                else:
                    ok_precheck, precheck_msg = _precheck_db_file_lock_for_compact()
                    if ok_precheck:
                        st.info(precheck_msg)
                    else:
                        st.error(precheck_msg)
                    if ok_precheck:
                        st.caption("建议先停 daemon 再压库。")
                        log_container = st.empty()
                        with st.spinner("正在执行数据库压缩（可能需要数分钟）..."):
                            ok, output_text = _run_compact_db_offline()
                        log_container.code(output_text[-6000:], language="log")
                        if ok:
                            st.success("✅ 数据库压缩完成。")
                            st.session_state.pop("sidebar_db_snapshot_cache", None)
                        else:
                            st.warning("💡 本次压缩未完成，可能仍有进程占用主库。")
        with col_b:
            if st.button("🛠 停Daemon→压缩→重启", width="stretch", key="sidebar_btn_compact_restart"):
                if _is_maintenance_mode_ui():
                    st.error("维护模式中：请等待运维脚本结束后再执行。")
                else:
                    flow_logs: list[str] = []
                    if os.name != "nt":
                        st.error("该一键流程目前仅支持 Windows。")
                    else:
                        try:
                            from data.db_core import close_db

                            close_db()
                            flow_logs.append("[STEP] 已释放当前 UI 进程 DuckDB 连接")
                        except Exception as e:
                            flow_logs.append(f"[WARN] 释放 UI 连接失败（继续执行）: {e}")

                        with st.spinner("正在执行：停 daemon -> 压缩 -> 重启 daemon ..."):
                            pids = _list_daemon_pids_windows()
                            flow_logs.append(f"[STEP] 检测到 daemon PID: {pids or '无'}")
                            ok_stop, stop_msg = _stop_daemon_pids(pids)
                            flow_logs.append(stop_msg)
                            if not ok_stop:
                                st.error("❌ 停止 daemon 失败，请手工关闭后再试。")
                                st.code("\n".join(flow_logs)[-7000:], language="log")
                            else:
                                ok_comp, comp_msg = _run_compact_db_offline()
                                flow_logs.append(comp_msg)
                                if not ok_comp:
                                    st.error("❌ 压缩失败，已跳过 daemon 重启。")
                                    st.code("\n".join(flow_logs)[-7000:], language="log")
                                else:
                                    ok_start, start_msg = _start_daemon_background()
                                    flow_logs.append(start_msg)
                                    st.code("\n".join(flow_logs)[-7000:], language="log")
                                    if ok_start:
                                        st.success("✅ 一键流程完成：daemon 已重启，数据库已压缩。")
                                        st.session_state.pop("sidebar_db_snapshot_cache", None)
                                    else:
                                        st.warning("⚠️ 压缩已完成，但 daemon 重启失败，请手工启动。")

        if st.session_state.get("force_maintenance_dispatched"):
            st.warning("⚠️ 维护程序已投递，请等待脚本结束后再操作。")
        st.warning("🚨 深度维护将切断扫描引擎，执行底层清理后自动恢复。")
        if st.button("🚨 一键强制数据库维护", type="primary", width="stretch", key="sidebar_btn_force_maintenance"):
            _root = _project_root_ui()
            _script = os.path.join(_root, "tools", "force_maintenance_vacuum.py")
            # 兼容两种运维脚本命名：历史 force_maintenance_vacuum.py
            # 与现役 weekly_db_maintenance_orchestrated.py。
            # 原实现只认前者，找不到就 st.error，按钮等于废掉。
            if not os.path.isfile(_script):
                _alt = os.path.join(_root, "tools", "weekly_db_maintenance_orchestrated.py")
                if os.path.isfile(_alt):
                    _script = _alt
                else:
                    st.error(f"未找到运维脚本: {_script}")
            if os.path.isfile(_script):
                _cmd = [sys.executable, _script]
                try:
                    from data.db_core import close_db

                    try:
                        close_db()
                    except Exception:
                        pass
                except Exception as e:
                    st.warning(f"未能释放本进程 DuckDB 连接（仍将投递脚本）: {e}")
                try:
                    if sys.platform == "win32":
                        subprocess.Popen(_cmd, cwd=_root, creationflags=subprocess.CREATE_NEW_CONSOLE)
                    else:
                        subprocess.Popen(
                            _cmd,
                            cwd=_root,
                            start_new_session=True,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                        )
                    st.session_state["force_maintenance_dispatched"] = True
                    st.success("清理任务已投递至独立进程，请留意弹出的控制台！")
                except Exception as e:
                    st.error(f"投递失败: {e}")

        if query_done:
            st.divider()
            st.caption("缺失查询结果")
            if not missing_dates:
                st.markdown(
                    "<div style='background-color:#e6f4ea;padding:12px;border-radius:6px;margin:10px 0;color:#10b981;font-size:14px;font-weight:bold;'>✅ 150天数据完备，滴水不漏！</div>",
                    unsafe_allow_html=True,
                )
            else:
                st.markdown(
                    f"<div style='background-color:#fdfadd;border:1px solid #ffeeba;padding:12px;border-radius:6px;margin:10px 0;color:#856404;font-size:14px;'>⚠️ 发现缺失 <span style='font-weight:bold;'>{len(missing_dates)}</span> 天：</div>",
                    unsafe_allow_html=True,
                )
                st.markdown(
                    f"<div style='background-color:#e2e8f0;padding:12px;border-radius:6px;margin:10px 0;color:#004085;font-size:14px;'>{', '.join(missing_dates[:5])}{'...' if len(missing_dates) > 5 else ''}</div>",
                    unsafe_allow_html=True,
                )


def render_sidebar(p1_size="--", latest_trade_date="未同步", sector_ranks=None, regime_name="震荡市"):
    need_refresh_radar = False

    with st.sidebar:
        # 与上方「池子视图 + 数据底座」区块分隔（二者由 app.py 先行渲染）
        st.markdown("---")
        st.markdown("### 🛡️ 机构实盘中控舱")
        st.markdown(f"**📅 本地数据基准**: `{latest_trade_date}`")
        db_snap = _get_db_snapshot_cached(ttl_sec=300)
        size_text = _fmt_size_mb(db_snap.get("db_size_bytes", 0))
        rows_text = f"{db_snap.get('total_rows', 0):,}"
        codes_text = f"{db_snap.get('total_codes', 0):,}"
        tag = db_snap.get("quality_tag", "未知")
        hint = db_snap.get("quality_hint", "")
        tag_color = "#10b981" if tag == "优" else ("#f59e0b" if tag in ["中", "锁冲突"] else "#ef4444")
        st.markdown(
            f"""
<div style='background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;padding:8px 10px;line-height:1.45;font-size:12px;'>
  <div style='margin:2px 0;'>📁 物理体积: <b>{size_text}</b> <span style='color:#64748b;'>(防暴涨锁死)</span></div>
  <div style='margin:2px 0;'>📊 载弹总数: <b>{rows_text}</b> 条 K 线记录</div>
  <div style='margin:2px 0;'>🎯 标的数量: <b>{codes_text}</b> 只 <span style='color:#64748b;'>(全市场样本覆盖)</span></div>
  <div style='margin:2px 0;'>🧪 健康评级: <span style='color:{tag_color};font-weight:700;'>{tag}</span> · <span style='color:#475569;'>{hint}</span></div>
</div>
""",
            unsafe_allow_html=True
        )
        if db_snap.get("error"):
            st.caption(f"数据库快照提示：{db_snap.get('error')}")
        # 【V26.88 鲁棒性】同步健康度面板：来自 data/sync_health.py
        try:
            from data.sync_health import read_sync_health_summary, list_available_backups
            from data.db_core import list_available_backups as _list_dbb
            health = read_sync_health_summary(limit=30)
            backups = _list_dbb()
            if (health.get("total") or 0) > 0 or backups:
                parts = []
                if health.get("total"):
                    rate_ok = health.get("ok", 0)
                    rate_total = max(1, health["total"])
                    parts.append(
                        f"同步健康：✅ {rate_ok}/{rate_total}"
                        + (f" · 失败 {health.get('failed', 0)}" if health.get("failed", 0) else "")
                        + (f" · 熔断 {health.get('critical', 0)}" if health.get("critical", 0) else "")
                        + (f" · 平均 {health['avg_duration_sec']:.1f}s" if health.get("avg_duration_sec") else "")
                    )
                if health.get("last_sync_ts"):
                    parts.append(f"最近同步：`{health['last_sync_ts']}`（{health.get('last_kind', '-')}/{health.get('last_status', '-')}）")
                if health.get("last_backup_ts"):
                    parts.append(f"最近备份：`{health['last_backup_ts']}`")
                if backups:
                    latest = backups[0]
                    parts.append(f"DB备份：{len(backups)} 份（最新 `{latest.get('timestamp', '-')}`）")
                if parts:
                    st.markdown(
                        "<div style='background:#f1f5f9;border:1px dashed #cbd5e1;border-radius:8px;padding:6px 10px;line-height:1.5;font-size:11px;'>"
                        + "<br/>".join(parts)
                        + "</div>",
                        unsafe_allow_html=True,
                    )
        except Exception as _health_err:
            logging.debug("同步健康度面板跳过: %s", _health_err)

        st.markdown("---")
        
        st.markdown("### 🎯 P1 战略底仓模式（参与门槛：流通市值）")
        # 与 Daemon 09:35 early_morning_p5_validation 写入的 p5_yesterday_validated.json 对齐（当日口径）
        try:
            from core.p5_morning_validation import read_p5_validation_summary_for_ui

            _pv_c, _pv_r, _ = read_p5_validation_summary_for_ui()
            st.caption(f"P5 次日早盘已验证 {_pv_c} 只（剔除 {_pv_r} 只）")
        except Exception:
            st.caption("P5 次日早盘已验证 0 只（剔除 0 只）")
        st.markdown(
            """
<style>
section[data-testid="stSidebar"] div[data-baseweb="radio"] label {
  font-size: 0.86rem !important;
  line-height: 1.35 !important;
}
</style>
""",
            unsafe_allow_html=True,
        )
        pool_mode = st.radio(
            "模式选定",
            ["🔴 P1底仓池", "⭐ 直通车: 专属自选"],
            index=0 if st.session_state.get('pool_mode', 'P1') == 'P1' else 1,
            label_visibility="collapsed"
        )
        st.session_state["pool_mode"] = "P0" if "直通车" in pool_mode else "P1"

        st.markdown("---")
        if st.session_state['pool_mode'] == 'P0':
            uploaded_file = st.file_uploader("📥 导入自选股 (TXT格式)", type=['txt'])
            if uploaded_file is not None:
                os.makedirs('data', exist_ok=True)
                file_path = getattr(constants, 'P0_FILE_PATH', 'data/p0_custom.txt')
                with open(file_path, "wb") as f:
                    f.write(uploaded_file.getbuffer())
                st.toast("✅ 自选股弹药已上膛入库 (data/p0_custom.txt)！")
                
        st.markdown("---")
        st.markdown("### 📡 实时板块雷达")
        if st.button("🔄 刷新实时板块", width="stretch"):
            need_refresh_radar = True
            
        # ================= 🚀 涨跌双向宽域雷达渲染 =================
        _sr_fs = "14.5px"
        _sr_lh = "1.45"
        if sector_ranks:
            rank_list = list(sector_ranks.items())
            total_sectors = len(rank_list)

            st.markdown(
                f"<div style='font-size:{_sr_fs};line-height:{_sr_lh};'>🔥 <b>领涨板块 (进攻区 Top 8)</b></div>",
                unsafe_allow_html=True,
            )
            # 展示前 8 名
            for i, (sec, pct) in enumerate(rank_list[:8]):
                if i < 3:
                    st.markdown(
                        f"<div style='font-size:{_sr_fs};line-height:{_sr_lh};margin:2px 0;'>"
                        f"<b>Top {i+1}</b>: 👑 <b>{sec}</b> <span style='color:#ef4444;font-weight:bold;'>{pct:.2f}%</span></div>",
                        unsafe_allow_html=True,
                    )
                else:
                    st.markdown(
                        f"<div style='font-size:{_sr_fs};line-height:{_sr_lh};margin:2px 0;'>"
                        f"<b>Top {i+1}</b>: {sec} <span style='color:#ef4444;'>{pct:.2f}%</span></div>",
                        unsafe_allow_html=True,
                    )

            # 🚀 修改点：将领跌板块扩容至 Bottom 8
            if total_sectors > 16:
                st.markdown(
                    f"<div style='font-size:{_sr_fs};line-height:{_sr_lh};margin-top:8px;'>"
                    f"🧊 <b>领跌板块 (绞肉机 Bottom 8)</b></div>",
                    unsafe_allow_html=True,
                )
                # 提取倒数 8 名，并反转顺序让跌得最惨的排在“倒数第1”
                bottom_list = rank_list[-8:]
                bottom_list.reverse()
                for i, (sec, pct) in enumerate(bottom_list):
                    if i < 3:
                        st.markdown(
                            f"<div style='font-size:{_sr_fs};line-height:{_sr_lh};margin:2px 0;'>"
                            f"<b>倒数 {i+1}</b>: 💀 <b>{sec}</b> <span style='color:#10b981;font-weight:bold;'>{pct:.2f}%</span></div>",
                            unsafe_allow_html=True,
                        )
                    else:
                        st.markdown(
                            f"<div style='font-size:{_sr_fs};line-height:{_sr_lh};margin:2px 0;'>"
                            f"<b>倒数 {i+1}</b>: 🩸 {sec} <span style='color:#10b981;'>{pct:.2f}%</span></div>",
                            unsafe_allow_html=True,
                        )
        else:
            st.info("大盘数据不足或尚未收盘。")

        st.markdown("---")
        st.markdown("### ⚠️ 机构实盘铁纪律")
        st.markdown("""
        <div style='background-color: #fce8e8; padding: 18px; border-radius: 8px; color: #a94442; line-height: 1.8; font-size: 14px;'>
            <span style='color:#d9534f; font-weight:bold;'>1. 仓位底线：</span>单票≤8%，总仓≤40%。<br>
            <span style='color:#d9534f; font-weight:bold;'>2. 止损铁律：</span>浮亏5%无条件斩仓。<br>
            <span style='color:#d9534f; font-weight:bold;'>3. 情绪过滤：</span>大盘处于【冰点/退潮】时，P3 全忽略。<br>
            <span style='color:#d9534f; font-weight:bold;'>4. 确认机制：</span>看到 P3 底牌，必须等量比 > 2.0 且站上 MA5。<br>
            <span style='color:#d9534f; font-weight:bold;'>5. 敬畏市场：</span>谋定后动，严禁盘中冲动追高！
        </div>
        """, unsafe_allow_html=True)

        # 1档阈值固定在侧边栏最底部，便于与上方雷达/纪律区分
        # 【V26.88+10-3 UI 清理】删除 P1 阈值显示面板（每天不变，占位大，调试用 explain_p1_decision）
        # 档位信息已写入 _LAST_P1_WASH_ADAPTIVE.adaptive_reason，主面板可见
        st.markdown("---")

    return need_refresh_radar
