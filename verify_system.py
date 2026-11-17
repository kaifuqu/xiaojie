#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
小杰AI选股系统 Pro V26.88 - 系统启动前体检脚本（verify_system.py）
================================================================================
【用途】start_daemon_24x7.bat 看门狗启动前的强校验脚本。
   - 任一 P0 检查失败：拒绝启动，返回 exit code 1
   - 仅 P2 检查失败：warn 打印但不阻塞启动

【检查项】
   P0 (阻塞):
     1. Python 版本 >= 3.10
     2. 关键依赖（streamlit/pandas/numpy/duckdb/pyyaml/tushare/schedule/requests/aiohttp）
     3. constants.APP_VERSION == "V32.0"
     4. config.yaml 可被 YAML 解析 + 关键字段非空
     5. 数据下载闸 ≥120 亿 / 选股闸 ≥120 亿
     6. 核心模块可导入（scan_engine, pool_manager, score_calibration, p4/p5 strategies）
     7. DuckDB 主库文件可访问 + 关键表存在
     8. Tushare/HiThink 限流器单例可初始化
     9. 企微 Webhook 至少一条配置
     10. 时区已锚定到 Asia/Shanghai
     11. 【V29】daily_data 数据新鲜度（落后 >= 2 天 = FAIL）
     12. 【V29】sniper_pipeline_state 各阶段成功日期（同步/P1/P5）
     13. 【V29】dim_index_valuation 非空（环境阀门不得静默退化为中性）
     14. 【V29】企微 push_markdown_async 调用契约与网关签名一致
     15. 【V30】市值入库底线：库内不得存在 total_mv/circ_mv 低于 120 亿的行
         （用户硬约束；daemon 占用数据库时降级为 WARN）

   P1 (warn):
     - HiThink API Key 已配置
     - DeepSeek API Key 已配置
     - 9 大核心数据表存在

【用法】python verify_system.py
   返回 exit code 0 = 通过；exit code 1 = 阻塞失败
"""
from __future__ import annotations

import os
import sys
import time

# 时区锚定（平台安全）
# Windows 无 time.tzset；写 TZ=Asia/Shanghai 会让 MSVCRT 回退 UTC，
# 裸 datetime.now() 整体偏移 -8h，故 Windows 不设置进程级 TZ。
if os.name != "nt":
    os.environ["TZ"] = "Asia/Shanghai"
    if hasattr(time, "tzset"):
        try:
            time.tzset()
        except Exception:
            pass
else:
    os.environ.pop("TZ", None)

_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
os.chdir(_PROJECT_ROOT)


# ==================== [V26.88+10-8] SAC pyarrow shim ====================
# Win11 Smart App Control may block pyarrow._compute.pyd (WinError 4551),
# which kills pandas import and breaks core.* module loading.
# Trigger the shim before any core module import.
try:
    from core import _pyarrow_compat as _pyarrow_compat  # noqa: E402
    _pyarrow_compat.disable_pyarrow()
except Exception as _pyarrow_e:  # pragma: no cover
    print(f"[WARN] _pyarrow_compat shim load failed: {_pyarrow_e}")


# ==================== 输出工具 ====================
class Colors:
    OK = "\033[92m"
    FAIL = "\033[91m"
    WARN = "\033[93m"
    INFO = "\033[96m"
    RESET = "\033[0m"
    BOLD = "\033[1m"


def _color(s: str, c: str) -> str:
    if sys.platform == "win32":
        # Windows cmd 终端对 ANSI 不友好，使用 [OK]/[FAIL] 文本前缀
        return s
    return f"{c}{s}{Colors.RESET}"


_PASS = 0
_FAIL = 0
_WARN = 0


def p_ok(msg: str) -> None:
    global _PASS
    _PASS += 1
    print(_color(f"[OK]   {msg}", Colors.OK))


def p_fail(msg: str) -> None:
    global _FAIL
    _FAIL += 1
    print(_color(f"[FAIL] {msg}", Colors.FAIL))


def p_warn(msg: str) -> None:
    global _WARN
    _WARN += 1
    print(_color(f"[WARN] {msg}", Colors.WARN))


def p_info(msg: str) -> None:
    print(_color(f"[INFO] {msg}", Colors.INFO))


def p_section(title: str) -> None:
    print()
    print(_color(f"========== {title} ==========", Colors.BOLD))


# ==================== 体检专用只读连接（2026-11-03 修复） ====================
# 背景（真实事故）：体检在本机以「无主库文件写权限」的会话运行时，
#   data/db_core/_connection.py::_duckdb_connect_readonly() 实际以「读写模式」
#   打开主库（V30.1 为规避 DuckDB「同库不同配置」错误而统一连接配置），
#   于是 4 条纯查询类检查（DuckDB 检查 / 数据新鲜度 / 估值温度 / 信号闭环）
#   全部连带报 IO Error: Cannot open file ...: 拒绝访问，
#   进而列出 4 个虚假 P0 阻塞 —— 而数据本身完好（只读直连可正常读出 238328 行）。
# 修法（仅体检脚本，不碰业务逻辑）：优先走 db_core 单例；不可用时退化为
#   原生 duckdb.connect(read_only=True)（只执行 SELECT，绝不写库）。
def _qc_ro_conn():
    """体检用只读连接 contextmanager：多级兜底，保证「只读能读」就不再误报 P0。"""
    from contextlib import contextmanager as _cm

    @_cm
    def _ctx():
        con = None
        owned = False
        try:
            try:
                from data.db_core import get_read_conn_singleton as _grs
                con = _grs(max_wait_sec=8.0)
            except Exception:
                con = None
            if con is None:
                import duckdb
                from data.db_core import get_duckdb_path as _gdp
                con = duckdb.connect(_gdp(), read_only=True)
                owned = True
            yield con
        finally:
            if owned and con is not None:
                try:
                    con.close()
                except Exception:
                    pass

    return _ctx()


# ==================== P0 检查 ====================
def check_python_version() -> None:
    p_section("P0-1 Python 版本")
    major, minor = sys.version_info[:2]
    if major >= 3 and minor >= 10:
        p_ok(f"Python {major}.{minor}.{sys.version_info.micro} (>=3.10)")
    else:
        p_fail(f"Python {major}.{minor} < 3.10，请升级到 Python 3.10+")


def check_required_packages() -> None:
    p_section("P0-2 关键依赖包")
    REQUIRED = [
        ("streamlit", "1.28.0"),
        ("pandas", "2.0.0"),
        ("numpy", "1.24.0"),
        ("duckdb", "0.9.0"),
        ("yaml", "6.0"),  # PyYAML
        ("tushare", "1.2.89"),
        ("schedule", "1.2.0"),
        ("requests", "2.28.0"),
        ("aiohttp", "3.9.0"),
    ]
    for pkg, min_ver in REQUIRED:
        try:
            mod = __import__(pkg)
            ver = getattr(mod, "__version__", "?")
            p_ok(f"{pkg}=={ver} (要求 >={min_ver})")
        except ImportError as e:
            p_fail(f"{pkg} 未安装: {e}")


def check_app_version() -> None:
    p_section("P0-3 APP_VERSION")
    try:
        import constants
        if constants.APP_VERSION == "V32.0":
            p_ok(f"APP_VERSION = {constants.APP_VERSION}")
        else:
            p_fail(f"APP_VERSION = {constants.APP_VERSION}（应为 V32.0）")
    except Exception as e:
        p_fail(f"无法导入 constants: {e}")


def check_config_yaml() -> None:
    p_section("P0-4 config.yaml 配置")
    cfg_path = os.path.join(_PROJECT_ROOT, "config.yaml")
    if not os.path.isfile(cfg_path):
        p_fail(f"config.yaml 不存在: {cfg_path}")
        return
    # 【V29 上线修复 2026-10-15】原实现把 import yaml 放在 try 内部，却用 except yaml.YAMLError
    # 兜底 —— 当解释器缺 PyYAML 时，import 抛 ModuleNotFoundError，此时 except 子句再去解析
    # 同名局部变量 yaml，直接 UnboundLocalError: cannot access local variable 'yaml'，
    # 于是体检脚本自己崩溃，看门狗只看到非 0 退出码却不知道真实原因（实测生产 py 解释器复现）。
    # 根因：except 子句引用了只在 try 内部才绑定的名字。
    # 修复：把 import 提到 try 之前，缺依赖时直接给出可读 FAIL 并返回。
    try:
        import yaml
    except ImportError as _e:
        p_fail(
            f"当前解释器缺少 PyYAML（{_PROJECT_ROOT}/_pyenv/venv 或系统 Python 需 pip install pyyaml）: {_e}"
        )
        return
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        if not isinstance(cfg, dict):
            p_fail("config.yaml 解析结果不是 dict")
            return
        p_ok(f"config.yaml 解析成功（{len(cfg)} 个顶层 key）")

        # 关键字段
        if cfg.get("tushare", {}).get("token"):
            # 【V29 2026-10-15 安全】体检日志与报告禁止输出密钥片段，只报“已配置”。
            p_ok("Tushare token 已配置")
        else:
            p_fail("Tushare token 未配置")

        # 闸门
        p1_min = cfg.get("strategies", {}).get("p1", {}).get("select_min_circ_mv_wan", 0)
        if p1_min >= 1_200_000:
            p_ok(f"P1 选股闸 = {p1_min/10000:.0f}亿（>= 120 亿）")
        else:
            p_fail(f"P1 选股闸 = {p1_min/10000:.0f}亿（应 >= 120 亿）")

        # 策略评分表（修复后非空）
        ss = cfg.get("strategy_scores", {})
        if len(ss) >= 20:
            p_ok(f"strategy_scores: {len(ss)} 条")
        else:
            p_fail(f"strategy_scores 仅 {len(ss)} 条（应 >= 20）")

        # forbidden_industries
        ng = cfg.get("novice_guard", {})
        fi = ng.get("forbidden_industries", [])
        if len(fi) >= 10:
            p_ok(f"novice_guard.forbidden_industries: {len(fi)} 条")
        else:
            p_warn(f"novice_guard.forbidden_industries 仅 {len(fi)} 条（建议 >= 10）")
    except yaml.YAMLError as e:
        p_fail(f"config.yaml 解析错误: {e}")
    except Exception as e:
        p_fail(f"config.yaml 读取异常: {e}")


def check_core_modules() -> None:
    p_section("P0-5 核心模块可导入")
    modules = [
        ("core.config_manager", "get_strategy_min_circ_mv_wan"),
        ("constants", "APP_VERSION"),
        ("core.scan_engine", "run_scan_engine"),
        ("core.pool_manager", "build_p1_pool_and_cache"),
        ("core.strategies.score_calibration", "compute_p1_multi_dim_smooth_score"),
        ("core.strategies.strat_p2_auction", "P2Auction"),
        ("core.strategies.strat_p3_intraday", "P3Intraday"),
        ("core.strategies.strat_p4_tail", "P4Tail"),
        ("core.strategies.strat_p5_postmarket", "P5Postmarket"),
        ("core.strategies.strat_base", "strict_golden_burst_ok"),
        ("core.signal_filter", "multi_layer_filter"),
        ("core.regime_analyzer", "get_market_regime"),
        ("core.master_control", "is_maintenance_mode_enabled"),
        ("core.intraday_snapshot_scheduler", "_cleanup_old_snapshots"),
        ("core.notification_gateway", "notify_wechat_system_alert"),
        ("data.data_fetcher", "sync_recent_days"),
        ("data.api_fetcher", "fetch_realtime_batch"),
        ("data.db_core", "get_read_conn"),
        ("data.hithink_client", "HithinkClient"),
        ("data.tushare_rate_limiter", "TushareRateLimiter"),
        ("data.capital_resonance_features", "compute_capital_resonance_score"),
        ("data.fund_memory_score", "compute_fund_memory_score"),
        # V30 混合基准（上证 / 深证 / 创业板 / 沪深300）
        ("data.index_daily_store", "sync_index_quietly"),
        ("core.strategy_effectiveness_snapshot", "build_ui_snapshot"),
        ("core.strategy_effectiveness_dashboard", "load_snapshot"),
        ("ui.strategy_effectiveness_panel", "render_strategy_effectiveness_panel"),
    ]
    for mod_path, fn in modules:
        try:
            mod = __import__(mod_path, fromlist=["*"])
            if fn and not hasattr(mod, fn):
                p_fail(f"{mod_path}.{fn} 缺失")
            else:
                p_ok(f"{mod_path}" + (f".{fn}" if fn else ""))
        except ImportError as e:
            p_fail(f"{mod_path} 导入失败: {e}")


def check_duckdb() -> None:
    p_section("P0-6 DuckDB 主库")
    try:
        # 【V29 上线体检修复 2026-10-15】原先这里拼的是 <项目根>/quant_data.duckdb，
        # 而 db_core._connection 实际把主库解析到 <项目根>/data/quant_data.duckdb。
        # 因此体检永远命中“首次运行正常”分支，属于假绿。改为复用 db_core 的唯一解析口径。
        db_file = "quant_data.duckdb"
        db_path = None
        try:
            from data.db_core import get_duckdb_path
            db_path = get_duckdb_path()
        except Exception as _e:
            p_warn(f"无法通过 data.db_core 解析主库路径，回退到 data/ 目录: {_e}")
            db_path = os.path.join(_PROJECT_ROOT, "data", db_file)
        db_rel = os.path.relpath(db_path, _PROJECT_ROOT)
        if not os.path.isfile(db_path):
            # 【V26.88+9-27 优化】首次运行没有数据库是正常现象，
            # data_fetcher.sync_recent_days() 会在首次同步时自动初始化表结构。
            p_warn(f"数据库文件 {db_rel} 尚未存在（首次运行正常，下次同步时自动创建）")
            return
        size_mb = os.path.getsize(db_path) / (1024 * 1024)
        p_ok(f"数据库文件 {db_rel}（{size_mb:.1f} MB）")

        # 【V30.1 2026-10-16】统一走 db_core 读单例：DuckDB 1.5.x 不允许同进程内
        # read_only=True 连接与读写连接共存，直连只读会让体检自身成为阻塞源。
        with _qc_ro_conn() as con:
            tables = con.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
            ).fetchall()
            table_names = {t[0] for t in tables}
            p_info(f"已建表: {len(table_names)} 张")

            # 【V29 2026-10-15】原先只查 4 张老表，漏掉 V26/V28 实际依赖的关键表，
            # 会让 dim_security / signal_log 等缺失仍显示 PASS。
            # 表名取自 data/db_core/_schema_v26.py 与各模块真实 SQL，不含本项目不存在的表。
            expected = [
                "daily_data",
                "weekly_data",
                "stock_basic",
                "dim_security",
                "dim_trade_calendar",
                "index_daily",
                "signal_log",
                "p1_cache",
                "dim_sw_sector",
            ]
            for t in expected:
                if t in table_names:
                    p_ok(f"表 {t} 存在")
                else:
                    p_fail(f"表 {t} 不存在（生产库已存在但缺关键表，属阻塞项）")

            # 【V29 2026-10-15】关键表行数体检：存在但为空 = 数据未落库，属上线阻塞。
            for t, min_rows in (("daily_data", 10000), ("index_daily", 100), ("signal_log", 1)):
                if t not in table_names:
                    continue
                try:
                    n_rows = con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
                    if n_rows < min_rows:
                        p_fail(f"表 {t} 存在但仅 {n_rows} 行（阈值 {min_rows}），数据未落库或已被清空")
                    else:
                        p_ok(f"表 {t} 行数 {n_rows}（>= {min_rows}）")
                except Exception as _e2:
                    p_fail(f"表 {t} 行数统计失败: {_e2}")

            # 【V30.5 2026-10-18 P0】关键源字段「全 0 / 退化」检测。
            # 历史事故：一次性回填脚本把 total_mv / adj_factor / pe_ttm / pb /
            # ps_ttm / dv_ratio 整体写成 0，而旧体检把 0 当有效值，全程报绿，
            # 直到策略层才发现市值闸与前复权全废。
            # 现在对 daily_data 的关键源字段做「全 0 / 非 NULL 且方差为 0」体检。
            _critical_source_cols = [
                ("total_mv", True),    # True = 全 0 视为 P0
                ("adj_factor", True),  # adj_factor=0 直接破坏前复权口径
                ("circ_mv", True),    # P1 市值闸的唯一输入
                ("pe_ttm", False),
                ("pb", False),
                ("ps_ttm", False),
                ("dv_ratio", False),
                ("close", True),
                ("vol", True),
            ]
            if "daily_data" in table_names:
                for _col, _fatal_if_all_zero in _critical_source_cols:
                    try:
                        _r = con.execute(
                            f'SELECT COUNT(*) AS n, '
                            f'  SUM(CASE WHEN "{_col}" IS NULL THEN 1 ELSE 0 END) AS nulls, '
                            f'  SUM(CASE WHEN "{_col}" IS NOT NULL AND "{_col}" = 0 THEN 1 ELSE 0 END) AS zeros, '
                            f'  MIN("{_col}") AS mn, MAX("{_col}") AS mx, '
                            f'  STDDEV_POP("{_col}") AS sd '
                            f'FROM daily_data'
                        ).fetchone()
                        _n, _nulls, _zeros, _mn, _mx, _sd = _r
                        _n = int(_n or 0)
                        if _n <= 0:
                            continue
                        _nonnull = _n - int(_nulls or 0)
                        if _nonnull <= 0:
                            if _fatal_if_all_zero:
                                p_fail(
                                    f"daily_data.{_col} 全表无有效值（全 NULL，{_n} 行）——"
                                    f"数据底座该字段已失效，属于上线阻塞"
                                )
                            else:
                                p_warn(f"daily_data.{_col} 全表 NULL（{_n} 行），该字段无可用数据")
                            continue
                        # 全 0：数值上「看起来有值」，语义上等于没有
                        if int(_zeros or 0) >= _n:
                            if _fatal_if_all_zero:
                                p_fail(
                                    f"daily_data.{_col} 全表为 0（{_n} 行）——"
                                    f"疑似一次性脚本把源字段整体写成 0，属于上线阻塞"
                                )
                            else:
                                p_fail(
                                    f"daily_data.{_col} 全表为 0（{_n} 行）——估值/资金字段已失效"
                                )
                            continue
                        # 非 NULL 且方差为 0：所有值相同，等同于常量伪字段
                        if _sd is not None and float(_sd) == 0.0 and _nonnull >= 1000:
                            if _fatal_if_all_zero:
                                p_fail(
                                    f"daily_data.{_col} 非 NULL 但方差为 0（常量 "
                                    f"{_mn}，{_nonnull} 行）——字段已退化为常量，属于上线阻塞"
                                )
                            else:
                                p_warn(
                                    f"daily_data.{_col} 非 NULL 但方差为 0（常量 {_mn}，"
                                    f"{_nonnull} 行），请确认是否为接口未开通"
                                )
                            continue
                        if _fatal_if_all_zero:
                            p_ok(
                                f"daily_data.{_col} 有效值分布正常"
                                f"（非 NULL {_nonnull}/{_n}，0 值 {int(_zeros or 0)}，"
                                f"范围 {_mn}~{_mx}）"
                            )
                        else:
                            p_info(
                                f"daily_data.{_col} 非 NULL {_nonnull}/{_n}，"
                                f"0 值 {int(_zeros or 0)}，范围 {_mn}~{_mx}"
                            )
                    except Exception as _e3:
                        p_warn(f"daily_data.{_col} 退化检测执行失败（跳过）: {_e3}")
    except Exception as e:
        p_fail(f"DuckDB 检查失败: {e}")


def check_rate_limiter() -> None:
    p_section("P0-7 Tushare/HiThink 限流器")
    try:
        from data.tushare_rate_limiter import TushareRateLimiter, WINDOW_MAX_CALLS, SPECIAL_FEATURE_WINDOW_MAX_CALLS
        limiter = TushareRateLimiter()
        p_ok(f"TushareRateLimiter 初始化成功（常规={WINDOW_MAX_CALLS}/分钟，特色={SPECIAL_FEATURE_WINDOW_MAX_CALLS}/分钟）")
    except Exception as e:
        p_fail(f"TushareRateLimiter 初始化失败: {e}")
    try:
        from data.hithink_client import HithinkClient
        cli = HithinkClient()
        if cli.is_enabled:
            p_ok("HiThink 已启用")
        else:
            p_warn("HiThink 未启用（api_key 为空或 hithink.enabled=false）")
    except Exception as e:
        p_fail(f"HithinkClient 初始化失败: {e}")


def check_wechat_webhook() -> None:
    p_section("P0-8 企微 Webhook")
    try:
        import yaml
        with open(os.path.join(_PROJECT_ROOT, "config.yaml"), "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        url_main = cfg.get("notification", {}).get("wechat_webhook_url", "")
        url_sec = cfg.get("notification", {}).get("wechat_webhook_url_secondary", "")
        if url_main and "key=" in url_main:
            # 【V29 2026-10-15 安全】不再回显 webhook key 尾段。
            p_ok("主 Webhook 已配置")
        else:
            p_fail("主 Webhook 未配置")
        if url_sec and "key=" in url_sec:
            p_ok("备 Webhook 已配置")
        else:
            p_warn("备 Webhook 未配置")
    except Exception as e:
        p_fail(f"企微配置检查失败: {e}")


def check_timezone() -> None:
    """时区锚定硬门禁。

    【V29.1 加固】旧实现只判 now.hour 是否在 0-23，而 datetime.now(ZoneInfo(...))
    永远带正确时区，该判据恒为真（假绿）。真正要防的是 Windows 上 TZ=Asia/Shanghai
    让裸 datetime.now() 回退 UTC，因此这里必须比较"裸本地时间"与"显式北京时间"，
    偏差超过 60 秒即 FAIL。
    """
    p_section("P0-9 时区锚定")
    try:
        import time as _time
        from datetime import datetime
        from zoneinfo import ZoneInfo

        bj = ZoneInfo("Asia/Shanghai")
        now_bj = datetime.now(bj)
        now_naive_local = datetime.now()
        # 裸本地时间按 Asia/Shanghai 解释后的 epoch 与真实北京时间 epoch 之差
        drift_sec = abs(
            now_naive_local.replace(tzinfo=bj).timestamp() - now_bj.timestamp()
        )
        drift_desc = f"{drift_sec:.0f}s"
        if drift_sec <= 60:
            p_ok(
                f"时区已锚定到 Asia/Shanghai（北京时间 {now_bj.strftime('%Y-%m-%d %H:%M:%S')}"
                f" / 本地裸时间 {now_naive_local.strftime('%H:%M:%S')} / 偏差 {drift_desc}）"
            )
        else:
            p_fail(
                f"时区异常：裸 datetime.now()={now_naive_local.strftime('%Y-%m-%d %H:%M:%S')} 与北京时间 "
                f"{now_bj.strftime('%Y-%m-%d %H:%M:%S')} 偏差 {drift_desc}"
                f"（Windows 上请勿设置 TZ 环境变量；C 运行时回退 UTC 会偏移 -8h）"
            )
        if os.name == "nt" and "TZ" in os.environ:
            p_warn(
                f"Windows 进程仍存在 TZ={os.environ.get('TZ')} 环境变量，可能让 C 运行时回退 UTC"
            )
        del _time
    except Exception as e:
        p_fail(f"时区检查失败: {e}")


def _is_after_daily_data_publish_window() -> bool:
    """当前是否已过当日日线数据应该落库的窗口。

    口径与 daemon 时序铁律对齐：日线增量 19:45 跑，看门狗在 20:30 之后仍在同一天
    看到旧数据就一定是漏更，必须判 FAIL。

    【V30.1 修正 2026-10-16】旧实现用 `hour >= 16`。16:00 只是收盘，真正决定
    "当天日线该不该有了" 的是 19:45 的增量窗口。实测 16:58 跑体检时，20261016
    尚未开市收盘，但 hour>=16 已成立，于是 daily_data / 流水线 / 估值三处门禁
    同时误判 FAIL，一天里 16:00-19:45 这 3.75 小时 daemon 完全起不来。
    改为按真实时序：19:45 之前一律按"盘中容忍"，19:45 之后（含补跑窗口）才严格。
    """
    # 【V29.1 修正】必须用 ZoneInfo 显式取北京时间，不能用裸 datetime.now()。
    # 根因：本文件第 42 行 os.environ["TZ"]="Asia/Shanghai" 在 Windows 上会让
    # CPython 的本地时区解析成 UTC（实测真实 21:37 -> datetime.now() 得到 13:37），
    # 裸 now().hour 判出来是 13 点，盘后门禁被悄悄架空成"未过窗口"。
    from datetime import datetime
    from zoneinfo import ZoneInfo

    now_bj = datetime.now(ZoneInfo("Asia/Shanghai"))
    return (now_bj.hour, now_bj.minute) >= (19, 45)


def _now_bj_compact() -> str:
    """当前北京日期的 YYYYMMDD 口径（供 signal_log 当日落库校验使用）。

    【V30.1 2026-10-16】data/signal_logger.log_signal 用 datetime.now(BJ_TZ) 写
    trade_date，所以「今天落库的信号」在库里一定是今天；pipeline_state 里的
    last_p5_ok_bj_date 记的可能是上一交易日（盘中补跑场景）。两者必须分开算。
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d")


def _today_is_open_cached(today_str: str):
    """读 daemon 写的「当日是否开市」缓存，判断今天是否为交易日。

    【V30.17 2026-11-06】背景事故：2026-11-06 确为交易日（交易日历缓存
    is_open=true），但 `dim_trade_calendar` 尚未同步当日记录，于是
    `_expected_latest_trade_date` 退回 latest_local(11-05) → 应到=11-05
    恰好等于库内最新 → P0-10 判「合格」，当日数据缺失被完全掩盖。
    这等于拿「我有什么」当「我该有什么」，漏更永远无法被门禁发现。

    缓存 `state/sniper_trade_cal_cache.json`（date / is_open）由 daemon 每日按
    Tushare 结果写入，是「今天开不开市」最直接的依据，优先级应高于
    「日历表里有没有今天的记录」。

    返回 True / False；无缓存、日期不匹配或读取失败返回 None（交由调用方继续降级）。
    """
    try:
        import json
        import os

        from core.runtime_data_paths import STATE_DIR, ensure_runtime_data_layout

        ensure_runtime_data_layout()
        path = os.path.join(STATE_DIR, "sniper_trade_cal_cache.json")
        with open(path, "r", encoding="utf-8") as f:
            obj = json.load(f)
        if not isinstance(obj, dict):
            return None
        d = str(obj.get("date") or "").replace("-", "")[:8]
        if not d or d != str(today_str or "")[:8]:
            return None
        v = obj.get("is_open")
        if v is None:
            return None
        return bool(v)
    except Exception:
        return None


def _expected_latest_trade_date():
    """算出"此刻应已落库的最新交易日"以及最近交易日序列。

    返回 (expected_latest_yyyymmdd, expected_days_desc, source_label)。
    优先用官方 trade_cal（SSE 开市日），失败则退回工作日近似，调用方据此决定是否阻塞。

    【Bug B Fix 2026-10-20】周末/节假日误判根因：
      - 10-19（周日）Tushare trade_cal 返回 is_open=1（因为 Tushare 认为 10-19 是交易日）。
      - 但库里 dim_trade_calendar 没有 10-19（daemon 停摆未同步日历），daily_data 停在 10-16。
      - verify 用"应到 20261019"与"现有 20261016"比较，误判 FAIL。
      - 修复：优先查本地日历判断今天是否已记录为交易日；
        只有"今天已在本地日历且 is_open=TRUE"时才用今天；
        否则退回本地日历最晚的已记录交易日（库里真实状态）。
    """
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    today_str = today.strftime("%Y%m%d")
    window_start = today - timedelta(days=25)

    # === BUG FIX: 优先查本地日历 ===
    # 只有"今天已在本地日历且 is_open=TRUE"时才要求今天的数据。
    # 否则退回本地日历最晚的已记录交易日（daemon 停摆导致今天的日历数据不存在是正常态）。
    # 特别处理：即使库里没今天的记录，也要用"库里最晚交易日"做应到日期，
    # 避免 Tushare 返回"未来交易日"（如周一）导致误报 FAIL。
    try:
        with _qc_ro_conn() as con_local:
            cal_local = con_local.execute("""
                SELECT trade_date, is_open FROM dim_trade_calendar
                WHERE trade_date BETWEEN ? AND ?
                ORDER BY trade_date DESC
            """, [window_start.strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d")]).fetchall()
        if cal_local:
            # 构造本地已知的交易日列表（只含 is_open=1 的）
            local_days = sorted({str(r[0])[:10].replace("-", "") for r in cal_local if r[1]})
            # 今天是否已在本地日历且为交易日？
            today_in_local = today_str in local_days
            if today_in_local:
                # 今天已记录为交易日 → 用今天作为应到日期
                return today_str, local_days, "本地日历(今日已记录)"
            else:
                # 今天未记录（周末/节假日/daemon 未同步）：
                # 【V30.17】先用 daemon 当日开市缓存裁决「今天到底开不开市」，
                # 避免把「日历表没同步今天」误当成「今天不是交易日」，
                # 从而让「当日数据缺失」被自我满足地判成合格。
                _cached_open = _today_is_open_cached(today_str)
                if _cached_open is True:
                    # 今日确实开市，只是 dim_trade_calendar 还没同步今日 → 应到就是今天
                    days_with_today = sorted(set(local_days) | {today_str})
                    return today_str, days_with_today, "当日开市缓存(今日开市,日历未同步)"
                if _cached_open is False:
                    # 今日确认休市 → 应到为最近已记录交易日（正常态，不该阻断启动）
                    latest_local = local_days[-1] if local_days else today_str
                    return latest_local, local_days, "当日开市缓存(今日休市)"
                # 【关键】用本地日历最晚交易日作为应到日期（而不是 Tushare 的"未来交易日"）
                # 因为"未来交易日是否开市"是 Tushare 的权威，本地日历的权威是"已落库的交易日"
                latest_local = local_days[-1] if local_days else today_str
                return latest_local, local_days, "本地日历(退回最晚已记录交易日)"
    except Exception:
        pass  # 本地日历查不到时继续走 Tushare 路线

    # === 原 Tushare 路线（本地日历不可用时兜底）===
    try:
        import data.fetcher as data_fetcher

        if getattr(data_fetcher, "pro", None) is not None:
            cal = data_fetcher.retry_api(data_fetcher.pro.trade_cal)(
                exchange="SSE", is_open="1",
                start_date=window_start.strftime("%Y%m%d"),
                end_date=today.strftime("%Y%m%d"),
                fields="cal_date",
            )
            if cal is not None and not cal.empty and "cal_date" in cal.columns:
                days = sorted({str(d)[:8] for d in cal["cal_date"].tolist()})
                if days:
                    return days[-1], days, "Tushare trade_cal(SSE)"
    except Exception as e:
        p_info(f"官方交易日历不可用（{type(e).__name__}），退回工作日近似口径")

    # 2) 工作日近似（含法定节假日会被高估一天，故只作兜底）
    days = []
    cur = window_start
    while cur <= today:
        if cur.weekday() < 5:
            days.append(cur.strftime("%Y%m%d"))
        cur += timedelta(days=1)
    return (days[-1] if days else today_str), days, "工作日近似(未取到官方日历)"


def _prev_trade_date_of(day_yyyymmdd: str) -> str:
    """取 day_yyyymmdd 的前一个交易日，用于盘前/盘中容忍 1 个交易日的滞后。"""
    if not day_yyyymmdd or len(day_yyyymmdd) != 8 or not day_yyyymmdd.isdigit():
        return ""
    try:
        from datetime import datetime, timedelta

        target = datetime.strptime(day_yyyymmdd, "%Y%m%d").date()
        import data.fetcher as data_fetcher

        if getattr(data_fetcher, "pro", None) is None:
            return ""
        cal = data_fetcher.retry_api(data_fetcher.pro.trade_cal)(
            exchange="SSE", is_open="1",
            start_date=(target - timedelta(days=15)).strftime("%Y%m%d"),
            end_date=(target - timedelta(days=1)).strftime("%Y%m%d"),
            fields="cal_date",
        )
        if cal is not None and not cal.empty and "cal_date" in cal.columns:
            days = sorted({str(d)[:8] for d in cal["cal_date"].tolist()})
            return days[-1] if days else ""
    except Exception:
        pass

    # 【V29.2 离线兜底】Tushare 不可用（离线/限流）时不能直接返回空串，
    # 否则 pipeline_state 的日期下限会退化成 None，整段日期门禁被静默跳过（假绿）。
    # 复用 _expected_latest_trade_date() 的交易日序列取前一日。
    try:
        _exp_latest, exp_days, _src = _expected_latest_trade_date()
        earlier = [d for d in (exp_days or []) if d < day_yyyymmdd]
        if earlier:
            return earlier[-1]
    except Exception:
        pass
    return ""


def check_data_freshness() -> None:
    """【V29 上线门禁 2026-10-15 / V29.1 交易日门禁修正】数据新鲜度硬门禁。

    背景：旧体检只查"表存在 + 行数达标"，导致 2026-10-15 体检报 63/0/0 [READY]，
    但主库最新行情其实停在 2026-10-13（缺 10-14、10-15 两个交易日），
    daemon 也早在 10-13 13:02 就停了 —— 体检假绿，是最危险的一类问题。

    V29.1 根因修复：原实现用"自然日 gap"当门禁（<=1 OK / ==2 WARN / >=3 FAIL）。
    2026-10-15（周四，盘后体检）时 day_gap==2 只得到一条 WARN，退出码 0 = 假绿。
    自然日 gap 与"缺了几个交易日"根本不是一回事：周五凌晨体检 day_gap=1 但其实
    缺了整个周五；周一凌晨体检 day_gap=3 但可能只缺 1 个交易日。
    改为：按 daily_data 已落库的真实交易日序列 + 官方 trade_cal 算"应到几日"，
    只比"交易日笔数"，并在盘后（>=16:00）把任何落后直接判 FAIL。
    """
    p_section("P0-10 数据新鲜度（上线硬门禁）")
    try:
        from data.db_core import get_existing_trade_dates, get_latest_daily_data_trade_date_yyyymmdd

        latest = str(get_latest_daily_data_trade_date_yyyymmdd() or "").strip()
        raw_dates = get_existing_trade_dates()
        # 【2026-11-03 修复】db_core 单例取不到（无写权限/被占库）时，退化原生只读直查，
        # 避免把「连接方式不可用」误报成「行情底座为空」的双重假 P0。
        if not latest or not isinstance(raw_dates, (list, tuple, set)):
            try:
                with _qc_ro_conn() as _c:
                    _row = _c.execute(
                        "SELECT MAX(trade_date) AS mx FROM daily_data"
                    ).fetchone()
                if _row and _row[0] is not None:
                    latest = str(_row[0])[:10].replace("-", "")
                with _qc_ro_conn() as _c:
                    raw_dates = [
                        r[0] for r in _c.execute(
                            "SELECT DISTINCT trade_date FROM daily_data "
                            "ORDER BY trade_date DESC"
                        ).fetchall()
                    ]
            except Exception as _e_ro:
                p_info(f"原生只读兜底查询失败: {_e_ro}")
        if not latest:
            p_fail("daily_data 无任何交易日数据（行情底座为空，禁止上线）")
            return
        if not isinstance(raw_dates, (list, tuple, set)):
            p_fail("daily_data 读取失败（DuckDB 可能被其它进程独占锁），无法判定新鲜度 — 阻塞项")
            return
        dates = sorted(str(d) for d in raw_dates)
        p_ok(f"daily_data 最新交易日 = {latest}（共 {len(dates)} 个交易日）")
        p_info(f"最近落库交易日序列: {dates[-6:]}")

        expected_latest, expected_days, cal_src = _expected_latest_trade_date()
        p_info(f"应到的最新交易日 = {expected_latest}（口径: {cal_src}）")

        after_close = _is_after_daily_data_publish_window()
        # 【V30.17 2026-11-06】降级口径不再静默：无法权威判定应到交易日时，
        # 不能悄悄判「合格」冒充通过，至少给出显式警告让人工确认。
        _degraded_cal = ("退回最晚已记录交易日" in cal_src) or ("工作日近似" in cal_src)
        if after_close and _degraded_cal:
            p_warn(
                f"应到交易日无法权威判定（口径: {cal_src}）：已过 19:45 增量窗口仍拿不到"
                "「今日是否开市」的依据，本项新鲜度结论仅供参考，请人工确认当日行情是否已落库"
            )
        if expected_latest:
            if latest >= expected_latest:
                # 【V30.18 2026-11-10】降级口径下 latest >= expected 只是「自洽」而非「已齐」：
                # 2026-11-10 daemon 停机 4 天后重启，当日开市缓存停更 → 应到退回 20261106，
                # 恰好等于库内最新，于是打印「合格」，而 11-09/11-10 两个交易日其实都缺。
                # 因此降级口径一律只给中性提示，不再输出「合格」结论。
                if _degraded_cal:
                    p_info(
                        f"数据新鲜度按降级口径自洽（最新 {latest} >= 应到 {expected_latest}），"
                        "但应到日期未经权威判定，不作为「已齐」结论 — 请人工核对当日行情是否落库"
                    )
                else:
                    p_ok(f"数据新鲜度合格（最新 {latest} >= 应到 {expected_latest}）")
            else:
                miss_n = len([d for d in expected_days if d > latest])
                detail = (
                    f"daily_data 最新 {latest} < 应到 {expected_latest}"
                    f"，缺 {miss_n} 个交易日数据（阻塞项）。"
                    "所有策略基于陈旧数据出票，先补齐历史行情再启动 daemon"
                )
                if after_close:
                    p_fail(detail + "；当前已过 19:45 增量窗口，属真实漏更而非盘前正常态")
                else:
                    p_warn(detail + "；当前尚未到盘后增量窗口（若盘后仍缺则转为阻塞）")
        else:
            # 拿不到官方日历（离线/限流）时退回自然日保守口径，只做提示不阻塞，
            # 避免"日历接口抖动 → 体检拒绝启动"的假阻塞。
            from datetime import datetime
            from zoneinfo import ZoneInfo

            latest_dt = datetime.strptime(latest, "%Y%m%d").date()
            today_date = datetime.now(ZoneInfo("Asia/Shanghai")).date()
            day_gap = int((today_date - latest_dt).days)
            if day_gap <= 1:
                p_ok(f"数据新鲜度合格（最新 {latest}，距今 {day_gap} 天；未取到官方日历）")
            elif after_close:
                p_fail(
                    f"数据严重过期：最新 {latest}，距今 {day_gap} 天（阻塞项，且未取到官方交易日历）"
                )
            else:
                p_warn(f"数据新鲜度偏低：最新 {latest}，距今 {day_gap} 天（未取到官方交易日历）")
    except Exception as e:
        p_fail(f"数据新鲜度检查失败: {e}")


def check_pipeline_state() -> None:
    """【V29 上线门禁 2026-10-15 / V29.1 日期门禁】sniper_pipeline_state 时间戳体检。

    这个文件记录 daemon 每天各阶段是否成功（last_sync_ok_bj_date / last_p1_ok_bj_date /
    last_p5_ok_bj_date ...）。旧体检从不看它，导致"daemon 三天没跑但体检全绿"。

    V29.1 根因修复：旧实现只要字段非空就打印 OK，2026-10-15 体检时
    last_sync_ok_bj_date=20261009（落后 4 个交易日）依然显示 [OK]。
    "曾经成功过" != "今天成功过"。改为与"最近已完成交易日"比较：
      - 行情同步/P5：应 >= 最近已完成交易日（盘后口径当天）
      - P1 底仓：同样应 >= 最近已完成交易日
      落后 >= 1 个交易日 = FAIL（daemon 没在跑，是上线阻塞项）。
    """
    p_section("P0-11 流水线执行状态（上线硬门禁）")
    try:
        # pipeline_state 路径口径复用 daemon 自己的解析（data/daemon_observability.py:87），
        # 不要臆造 runtime_data_paths 里不存在的符号（该模块并没有这个导出）。
        sp = os.path.join(_PROJECT_ROOT, "data", "runtime", "state", "sniper_pipeline_state.json")
        if not os.path.isfile(sp):
            p_warn(f"pipeline_state 尚未生成（首次运行正常）: {os.path.relpath(sp, _PROJECT_ROOT)}")
            return
        import json

        with open(sp, "r", encoding="utf-8") as f:
            st = json.load(f)
        rel = os.path.relpath(sp, _PROJECT_ROOT)

        expected_latest, _days, _src = _expected_latest_trade_date()
        # 盘后（>=16:00）应当天全部跑完；盘前/盘中允许落后 1 个交易日（当日尚未出数据）。
        after_close = _is_after_daily_data_publish_window()
        lag_tolerated = expected_latest if after_close else _prev_trade_date_of(expected_latest)
        p_info(f"流水线应达到的日期下限 = {lag_tolerated}（应到 {expected_latest}，盘后口径={after_close}）")

        for key, label in (
            ("last_sync_ok_bj_date", "行情同步"),
            ("last_p1_ok_bj_date", "P1 底仓重建"),
            ("last_p5_ok_bj_date", "P5 盘后"),
        ):
            v = str(st.get(key) or "").strip()
            if not v:
                p_fail(f"{label} 从未成功执行过（{key} 为空）— 阻塞项，{rel}")
            elif len(v) == 8 and v.isdigit() and lag_tolerated and v < lag_tolerated:
                p_fail(
                    f"{label} 最后成功 = {v}，低于日期下限 {lag_tolerated}"
                    f"（{key}）— daemon 已停摆，阻塞项"
                )
            else:
                p_ok(f"{label} 最后成功 = {v} >= 下限 {lag_tolerated}（{key}）")

        fail_date = str(st.get("last_sync_fail_bj_date") or "").strip()
        ok_date = str(st.get("last_sync_ok_bj_date") or "").strip()
        if fail_date and fail_date >= ok_date:
            p_fail(f"最近一次行情同步是失败态（last_sync_fail_bj_date={fail_date}）— 阻塞项")
        elif fail_date:
            p_warn(f"曾发生过同步失败（{fail_date}），最近成功 {ok_date}")
    except Exception as e:
        p_fail(f"流水线状态检查失败: {e}")


def check_index_valuation_gate() -> None:
    """【V29 上线门禁 2026-10-15】大盘估值温度表体检。

    compute_market_overheat_score() 在 dim_index_valuation 为空时会静默返回中性默认
    （PE=0 / 分位=0.5 / 温度=0 / regime=neutral），而 P1/P3/P4/P5 全部依赖该结果。
    空表 = 4 个池的环境阀门全部退化成"中性"，属于隐藏降级，必须体检出来。
    """
    p_section("P0-12 大盘估值温度（环境阀门依赖）")
    try:
        from data.db_core import get_duckdb_path

        db_path = get_duckdb_path()
        if not os.path.isfile(db_path):
            p_warn("数据库不存在，跳过估值表检查（首次运行正常）")
            return
        with _qc_ro_conn() as con:
            tabs = {
                r[0] for r in con.execute(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
                ).fetchall()
            }
            if "dim_index_valuation" not in tabs:
                p_fail("表 dim_index_valuation 不存在（环境阀门 compute_market_overheat_score 无数据源）")
                return
            row = con.execute(
                "SELECT COUNT(*), MAX(trade_date) FROM dim_index_valuation"
            ).fetchone()
            n_rows, max_date = int(row[0] or 0), row[1]
            if n_rows == 0:
                p_fail(
                    "dim_index_valuation 为空表（0 行）— P1/P3/P4/P5 的环境阀门已静默退化为中性默认，"
                    "属于隐藏降级，需先同步估值数据"
                )
                return
            p_ok(f"dim_index_valuation {n_rows} 行，最新 {max_date}")

            # ---- 覆盖率门禁：只有 1~2 个指数时，大部分池的环境阀门照样退化成中性 ----
            key_codes = ("000016.SH", "000300.SH", "000688.SH", "000852.SH", "000905.SH", "399006.SZ")
            try:
                latest_cdf = con.execute(
                    "SELECT ts_code FROM dim_index_valuation WHERE trade_date = ?", [max_date]
                ).fetchall()
                latest_codes = {str(r[0]) for r in latest_cdf}
                missing_codes = [c for c in key_codes if c not in latest_codes]
                if missing_codes:
                    p_fail(
                        f"最新估值日 {max_date} 缺少核心指数 {missing_codes}"
                        f"（{len(missing_codes)}/{len(key_codes)} 缺失）— 环境阀门样本不全，阻塞项"
                    )
                else:
                    p_ok(f"最新估值日 {max_date} 覆盖 6 个核心指数")

                # ---- 最新值非空门禁：分位字段全 NULL 时分位计算同样退化为中性 ----
                null_row = con.execute(
                    "SELECT COUNT(*) FROM dim_index_valuation "
                    "WHERE trade_date = ? AND (pe_ttm IS NULL OR pb IS NULL "
                    "OR pe_ttm_pct_3y IS NULL OR pb_pct_3y IS NULL)",
                    [max_date],
                ).fetchone()
                n_null = int(null_row[0] or 0)
                if n_null:
                    p_fail(
                        f"最新估值日 {max_date} 有 {n_null} 行 pe/pb/分位字段为空"
                        f"— 分位计算会退化为 0.5 中性值，阻塞项"
                    )
                else:
                    p_ok(f"最新估值日 {max_date} pe/pb/3年分位字段无空值")
            except Exception as inner_e:
                p_fail(f"dim_index_valuation 覆盖/非空检查失败: {inner_e}")

            # ---- 新鲜度门禁：估值必须跟到最近已完成交易日，否则阀门用陈旧分位 ----
            # 【V30 修正·类型对齐】dim_index_valuation.trade_date 是 DATE 类型，
            # 读取回来是 datetime.date；Tushare trade_cal 返回的是 '20261015' 字符串。
            # 直接 str() 比较会得到 '2026-10-15' < '20261015'（因为 '-' < '0'），
            # 造成"数据明明是最新的却判 FAIL"的假阻塞。
            expected_latest, _days, cal_src = _expected_latest_trade_date()
            max_date_str = ""
            if max_date is not None:
                max_date_str = str(max_date).replace("-", "").strip()
            # 【V30.1 2026-10-16】修正假阻塞：与 check_data_freshness 同一口径。
            # 当日估值只有在收盘后才发布，盘中（16:00 前）应到日期必然是当天，
            # 此时 dim_index_valuation 停在昨日属正常态；旧实现直接 p_fail，
            # 会让每天 11:00 前后启动 daemon 都被无理由拦下。
            after_close = _is_after_daily_data_publish_window()
            val_floor = expected_latest if after_close else _prev_trade_date_of(expected_latest)
            if expected_latest and max_date_str and max_date_str < val_floor:
                p_fail(
                    f"dim_index_valuation 最新 {max_date_str} < 日期下限 {val_floor}"
                    f"（应到 {expected_latest}，口径 {cal_src}）— 环境阀门用的是过期分位，阻塞项"
                )
            elif expected_latest:
                p_ok(f"dim_index_valuation 新鲜度合格（{max_date_str} >= 应到 {expected_latest}）")
    except Exception as e:
        p_fail(f"大盘估值表检查失败: {e}")


def check_push_contract() -> None:
    """【V29 上线门禁 2026-10-15】推送网关调用契约体检。

    背景：auto_sniper_daemon 的早盘简报曾调用 push_markdown_async(body, dedup_key=...)，
    而真实签名是 (content: str) -> None，导致每天 08:50 早盘简报 100% 抛 TypeError
    且被外层 except 静默吞掉 —— 表现是"早盘简报从来不发"，从无告警。
    这里用 inspect 静态比对所有调用点与真实签名，避免同类回归。
    """
    p_section("P0-13 企微推送调用契约")
    try:
        import inspect
        import re

        from core.notification_gateway import get_wechat_gateway

        gw = get_wechat_gateway("https://example.invalid/key=contract-probe-only")
        pm_sig = inspect.signature(gw.push_markdown_async)
        pnames = list(pm_sig.parameters)
        has_var_kw = any(
            p.kind is inspect.Parameter.VAR_KEYWORD for p in pm_sig.parameters.values()
        )
        if "dedup_key" in pnames:
            p_ok(f"push_markdown_async 支持 dedup_key: {pm_sig}")
        elif not has_var_kw and pnames == ["content"]:
            p_ok(f"push_markdown_async 单参 content（调用点不可再传 dedup_key）: {pm_sig}")
        else:
            p_warn(f"push_markdown_async 签名已变化，需人工确认调用点: {pm_sig}")

        # 静态扫全部生产调用点
        bad_sites = []
        for rel in ("auto_sniper_daemon.py", "ui/app.py", "core/notification/notifiers.py"):
            fp = os.path.join(_PROJECT_ROOT, rel.replace("/", os.sep))
            if not os.path.isfile(fp):
                continue
            with open(fp, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            for m in re.finditer(r"push_markdown_async\(([^\n)]*)\)", content):
                args = m.group(1)
                if "dedup_key" in args or "=" in args:
                    line_no = content[: m.start()].count("\n") + 1
                    bad_sites.append(f"{rel}:{line_no} -> {args.strip()}")
        if bad_sites:
            for s in bad_sites:
                p_fail(f"推送调用点传了网关不接受的参数: {s}")
        else:
            p_ok("全部生产 push_markdown_async 调用点与网关签名一致")
    except Exception as e:
        p_fail(f"推送契约检查失败: {e}")


def _shift_day_compact(day_yyyymmdd: str, offset_days: int) -> str:
    """把 YYYYMMDD 日期平移 offset_days 个自然日（用于按落库窗口回溯）。"""
    if not day_yyyymmdd or len(day_yyyymmdd) != 8 or not day_yyyymmdd.isdigit():
        return ""
    try:
        from datetime import datetime, timedelta

        base = datetime.strptime(day_yyyymmdd, "%Y%m%d").date()
        return (base + timedelta(days=int(offset_days))).strftime("%Y%m%d")
    except Exception:
        return ""


def check_signal_log_landed() -> None:
    """【V30.1 上线门禁 2026-10-16】扫描成功 -> 信号必须落 signal_log。

    背景：2026-10-15 P5 报告选出 8 只正式候选、pipeline_state 记
    last_p5_ok_bj_date=20261015 / last_p5_selected_count=8，但当天 signal_log 里
    一条 P5 正式信号都没有。根因是 DuckDB 只读单例(read_only=True)与写短连接
    配置冲突，取写连接必失败，而 intraday_snapshots.save_signal_log 把异常只记
    logging.debug，扫描仍被判成功。

    这类「扫描成功但信号未落库」是静默数据丢失：战法有效性、事后回看、持仓追踪
    全部依赖 signal_log。旧门禁只看 pipeline_state 的成功日期，看不出有没有真落库。
    这里补上闭环判定：
      - last_p5_selected_count > 0
      - last_p5_ok_bj_date 已达到流水线日期下限（当天口径）
      => 当天 signal_log 的 P5 正式信号数必须 >= 当天 P5 选中数
    """
    p_section("P0-14 扫描信号落库闭环（signal_log）")
    try:
        import json

        from data.db_core import get_duckdb_path, get_read_conn

        sp = os.path.join(_PROJECT_ROOT, "data", "runtime", "state", "sniper_pipeline_state.json")
        if not os.path.isfile(sp):
            p_warn("pipeline_state 尚未生成，跳过信号落库闭环检查（首次运行正常）")
            return
        with open(sp, "r", encoding="utf-8") as f:
            st = json.load(f)

        selected = int(st.get("last_p5_selected_count") or 0)
        p5_date = str(st.get("last_p5_ok_bj_date") or "").strip()
        if selected <= 0:
            p_info(
                f"最近一次 P5 未选中正式候选（last_p5_selected_count={selected}）"
                f" — signal_log 当天无 P5 正式信号属预期，跳过闭环校验"
            )
            return
        if not (len(p5_date) == 8 and p5_date.isdigit()):
            p_fail(f"last_p5_ok_bj_date 非法（{p5_date!r}），无法校验信号落库 — 阻塞项")
            return

        expected_latest, _days, _src = _expected_latest_trade_date()
        after_close = _is_after_daily_data_publish_window()
        lag_tolerated = expected_latest if after_close else _prev_trade_date_of(expected_latest)
        # 【V30.2 2026-10-17】落库窗口修正。
        # data/signal_logger.log_signal 的 trade_date 取「写入时刻的北京日期」，
        # 而 pipeline_state 记的是「业务日」。夜间 P5 失败后由调度器/人工补跑时，
        # 同一批业务日的信号会落到次日甚至更晚。旧实现只查 {业务日, 今天}，
        # 恰好漏掉最常见的「业务日 + 1 落库」，于是把已落库误判成未落库
        # （实测 20261015 的 P5 补跑落在 20261016）。
        # 现改为按业务日起连续 4 个自然日取窗口，并并入今天。
        today_compact = _now_bj_compact()
        check_dates = []
        for _off in range(0, 4):
            _d = _shift_day_compact(p5_date, _off)
            if _d and _d not in check_dates:
                check_dates.append(_d)
        if today_compact and today_compact not in check_dates:
            check_dates.append(today_compact)
        if lag_tolerated and p5_date < lag_tolerated:
            p_info(
                f"P5 成功日期 {p5_date} 低于日期下限 {lag_tolerated}"
                f"（当日口径尚未到），跳过信号落库闭环校验"
            )
            return

        db_path = get_duckdb_path()
        if not os.path.isfile(db_path):
            p_warn("数据库不存在，跳过信号落库闭环检查")
            return

        with _qc_ro_conn() as con:
            tabs = {
                str(r[0])
                for r in con.execute(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
                ).fetchall()
            }
            if "signal_log" not in tabs:
                p_fail("表 signal_log 不存在 — 扫描信号无法落库，阻塞项")
                return
            n_p5 = 0
            n_p5_all = 0
            hit_date = ""
            per_date = {}
            latest_p5 = ""
            for d in check_dates:
                # trade_date 历史上存在 '2026-10-12' 与 '20261012' 两种写法，
                # 统一去横线后按 8 位口径比较，避免格式差异造成假阴性。
                c_p5 = int(
                    con.execute(
                        "SELECT COUNT(*) FROM signal_log "
                        "WHERE REPLACE(CAST(trade_date AS VARCHAR), '-', '') = ? "
                        "AND LOWER(CAST(pool AS VARCHAR)) = 'p5' "
                        "AND COALESCE(is_observation, FALSE) = FALSE",
                        [d],
                    ).fetchone()[0]
                    or 0
                )
                c_all = int(
                    con.execute(
                        "SELECT COUNT(*) FROM signal_log "
                        "WHERE REPLACE(CAST(trade_date AS VARCHAR), '-', '') = ? "
                        "AND LOWER(CAST(pool AS VARCHAR)) = 'p5'",
                        [d],
                    ).fetchone()[0]
                    or 0
                )
                per_date[d] = (c_p5, c_all)
                if c_p5 > n_p5:
                    n_p5, n_p5_all, hit_date = c_p5, c_all, d
            _row = con.execute(
                "SELECT MAX(REPLACE(CAST(trade_date AS VARCHAR), '-', '')) "
                "FROM signal_log WHERE LOWER(CAST(pool AS VARCHAR)) = 'p5' "
                "AND COALESCE(is_observation, FALSE) = FALSE"
            ).fetchone()
            latest_p5 = str((_row[0] if _row else "") or "")

        _detail = "，".join(
            f"{d}: 正式={v[0]}/含观察={v[1]}" for d, v in per_date.items()
        )
        if n_p5 >= selected:
            p_ok(
                f"P5 {p5_date} 选中 {selected} 只，signal_log（{hit_date}）已落 {n_p5} 条 P5 正式信号"
                f"（该日落库 P5 共 {n_p5_all} 条，含观察）"
            )
            return
        if latest_p5 and latest_p5 > p5_date:
            # 历史缺口：该业务日的落库发生在 signal_log 写入修复之前，且其后已有更晚的
            # P5 正常落库 —— 说明写入链路当前健康。历史缺口无法无损回补（重跑会与当日
            # 实际推送名单不一致），因此显式列出并降级为 WARN，而不是永久阻塞启动。
            p_warn(
                f"历史数据缺口（非当前故障）：P5 {p5_date} 选中 {selected} 只，"
                f"该业务日落库窗口内未见足量正式信号（{_detail}）；"
                f"其后 {latest_p5} 的 P5 已正常落库，写入链路当前健康。"
                "该缺口早于 signal_log 写入修复，无法无损回补"
            )
            return
        p_fail(
            f"扫描成功但信号未落库：P5 {p5_date} 选中 {selected} 只，"
            f"signal_log 未见足量 P5 正式信号（{_detail}）"
            " — 战法有效性/事后回看会缺数据，阻塞项"
        )
    except Exception as e:
        p_fail(f"信号落库闭环检查失败: {e}")


# ==================== P0-15 市值入库底线 ====================
def check_mv_floor_gate() -> None:
    """P0-15：库内不得存在低于 120 亿入库底线的行。

    【为什么是硬门禁】用户硬约束「只做 120 亿市值以上的股票进入数据库」。
    代码层已有取数层（_mv_gate.apply_mv_gate）+ 写库层（enforce_mv_floor）双保险，
    但历史数据、回填脚本、人工 SQL 都可能绕过代码路径。此处直接从库里查证，
    把「底线被突破」变成启动即 FAIL，而不是等回测时才发现池子里混进了小票。

    口径（与 constants.DATA_FETCH_MIN_TOTAL_MV_WAN 一致）：
      - total_mv >= 门槛 即达标；
      - total_mv 为 NULL 时看 circ_mv 是否达标（与 _mv_gate.compute_mv_mask 同口径）；
      - 两者都缺失 → 视为违规（宁可不入库，不可错入库）。
    """
    p_section("P0-15 市值入库底线（用户硬约束）")
    try:
        from data.db_core import get_duckdb_path

        floor = 1_200_000.0  # 万元，= 120 亿
        try:
            import constants as _c

            floor = float(
                getattr(_c, "DATA_FETCH_MIN_TOTAL_MV_WAN", None)
                or getattr(_c, "DAILY_BASIC_MIN_MV_WAN", None)
                or 1_200_000
            )
        except Exception:
            pass

        db_path = get_duckdb_path()
        if not os.path.isfile(db_path):
            p_warn("数据库不存在，跳过市值底线检查")
            return

        # DuckDB 全机单写者：daemon 在跑时只读连接也打不开。
        # 那是「暂时读不到」，不是「底线被突破」，必须降级为 WARN 而非 FAIL，
        # 否则 24x7 巡航中人工跑体检会被误判为阻塞。
        def _is_lock_err(msg: str) -> bool:
            m = msg.lower()
            return (
                "another program" in m
                or "being used" in m
                or "cannot open file" in m
                or "lock" in m
            )

        n_all = n_bad = 0
        min_total = None
        sample = []
        try:
            with _qc_ro_conn() as con:
                tabs = {
                    str(r[0])
                    for r in con.execute(
                        "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
                    ).fetchall()
                }
                if "daily_data" not in tabs:
                    p_warn("daily_data 不存在，跳过市值底线检查（首次运行正常）")
                    return

                _row = con.execute(
                    "SELECT "
                    " COUNT(*) AS n_all,"
                    " SUM(CASE WHEN (total_mv IS NOT NULL AND total_mv < ?) "
                    "          OR (total_mv IS NULL AND (circ_mv IS NULL OR circ_mv < ?)) "
                    "     THEN 1 ELSE 0 END) AS n_bad,"
                    " MIN(total_mv) AS min_total "
                    "FROM daily_data",
                    [floor, floor],
                ).fetchone()
                n_all = int((_row[0] if _row else 0) or 0)
                n_bad = int((_row[1] if _row else 0) or 0)
                min_total = _row[2] if _row else None

                if n_bad > 0:
                    for r in con.execute(
                        "SELECT ts_code, CAST(trade_date AS VARCHAR), total_mv, circ_mv "
                        "FROM daily_data "
                        "WHERE (total_mv IS NOT NULL AND total_mv < ?) "
                        "   OR (total_mv IS NULL AND (circ_mv IS NULL OR circ_mv < ?)) "
                        "LIMIT 5",
                        [floor, floor],
                    ).fetchall():
                        sample.append(f"{r[0]}@{r[1]}(total={r[2]},circ={r[3]})")
        except Exception as _ce:
            if _is_lock_err(str(_ce)):
                p_warn("数据库被占用（daemon 运行中），无法校验市值底线 —— 停机后再跑可完整校验")
                return
            raise

        if n_bad == 0:
            min_yi = (float(min_total) / 10000.0) if min_total is not None else float("nan")
            p_ok(
                f"库内 {n_all:,} 行全部 >= {floor/10000.0:.0f} 亿"
                f"（total_mv 最小值 {min_yi:,.1f} 亿）"
            )
            return

        p_fail(
            f"库内有 {n_bad:,} 行低于 {floor/10000.0:.0f} 亿入库底线"
            f"（占 {100.0*n_bad/max(n_all,1):.3f}%）— 违反用户硬约束，阻塞项。"
            + (f" 样例: {', '.join(sample)}" if sample else "")
        )
    except Exception as e:
        p_fail(f"市值底线检查失败: {e}")


# ==================== P1 警告检查 ====================
def check_ai_keys() -> None:
    p_section("P1-AI API Key")
    try:
        import yaml
        with open(os.path.join(_PROJECT_ROOT, "config.yaml"), "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        ds = cfg.get("deepseek_analysis", {})
        if ds.get("api_key"):
            p_ok(f"DeepSeek API Key 已配置（{ds.get('model', '?')}）")
        else:
            p_warn("DeepSeek API Key 未配置（V26.88 AI 解读将跳过）")
        ht = cfg.get("hithink", {})
        if ht.get("api_key"):
            p_ok("HiThink API Key 已配置")
        else:
            p_warn("HiThink API Key 未配置（无法使用 HiThink fallback）")
    except Exception as e:
        p_warn(f"AI Key 检查失败: {e}")


def check_daemon_singleton() -> None:
    p_section("P1-Daemon 单例锁")
    # 【V29 修复】旧实现只看锁文件是否存在，不看里面的 PID 是否还活着。
    # 结果是 daemon 正常 24x7 运行时，每次体检都误报
    # 「锁文件已存在 — 旧 daemon 可能未清理」，真实故障与正常态无法区分。
    # 现改为读取 sentinel 的 PID 并判活：
    #   PID 存活 -> OK（正常在跑）
    #   PID 死亡/文件为空 -> WARN（陈旧锁，交由 process_orchestrator 清理）
    try:
        from core.process_orchestrator import DAEMON_LOCK_FILE, check_peer_active
        active, payload = check_peer_active(DAEMON_LOCK_FILE)
        if active:
            pid = int((payload or {}).get("pid") or 0)
            started = (payload or {}).get("started_at_human", "?")
            p_ok(f"Daemon 正在运行（PID {pid}, 启动于 {started}）— 单例正常")
            return
        if payload is not None:
            pid = int(payload.get("pid") or 0)
            p_warn(
                f"Daemon 锁为陈旧 sentinel（PID {pid} 已不存在）: {DAEMON_LOCK_FILE}"
                " — 启动时会自动清理，或手工删除"
            )
            return
        p_ok("Daemon 锁文件不存在（可正常启动）")
    except Exception as e:
        p_warn(f"Daemon 单例锁检查失败: {e}")


# ==================== 主流程 ====================
def main() -> int:
    try:
        _ver = str(constants.APP_VERSION)
    except Exception:
        _ver = ""
    _ver_tag = f" {_ver}" if _ver else ""
    print(_color("=" * 60, Colors.BOLD))
    print(_color(f" 小杰AI选股系统 Pro{_ver_tag} - 系统体检", Colors.BOLD))
    print(_color(f" 时间: {time.strftime('%Y-%m-%d %H:%M:%S')}", Colors.INFO))
    print(_color("=" * 60, Colors.BOLD))

    check_python_version()
    check_required_packages()
    check_app_version()
    check_config_yaml()
    check_core_modules()
    check_duckdb()
    check_rate_limiter()
    check_wechat_webhook()
    check_timezone()
    check_data_freshness()
    check_pipeline_state()
    check_index_valuation_gate()
    check_push_contract()
    check_signal_log_landed()
    check_mv_floor_gate()
    check_ai_keys()
    check_daemon_singleton()

    print()
    print(_color("=" * 60, Colors.BOLD))
    print(_color(f" 体检汇总: {_PASS} 通过 / {_FAIL} 失败 / {_WARN} 警告", Colors.BOLD))
    print(_color("=" * 60, Colors.BOLD))

    if _FAIL > 0:
        print()
        print(_color(f"[BLOCKED] 系统存在 {_FAIL} 个 P0 问题，禁止启动 daemon。", Colors.FAIL))
        print(_color("          请按上方 [FAIL] 行修复后重新执行体检。", Colors.FAIL))
        print()
        print(_color("【上线判定】不可上线。请先修复全部 [FAIL] 项。", Colors.FAIL))
        return 1
    if _WARN > 0:
        print()
        print(_color(f"[READY-WARN] 无阻塞项，但有 {_WARN} 条警告。", Colors.WARN))
        print(_color("            可启动观察模式；自动巡航/实盘推送请先确认警告项。", Colors.WARN))
        return 0
    print()
    print(_color("[READY] 系统通过体检，可启动 daemon。", Colors.OK))
    print(_color("【上线判定】允许全自动运行（自动巡航 + 企微推送）。", Colors.OK))
    return 0


if __name__ == "__main__":
    sys.exit(main())
