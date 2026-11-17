# -*- coding: utf-8 -*-
"""
小杰AI选股系统 Pro V26.88 实盘指挥舱（单页大屏；策略实验室已迁移至项目根 `run_lab.py` 独立进程）。
本页不启动任何后台调度器或 Streamlit 异步轮询；P2–P5 扫描为点击后同步执行 `run_scan_engine`，定时任务请用外部守护进程。
【UI版面功能】：
1. 🐉 P5 真龙池：全面扩充出第 5 个盘后验证专池，在视觉和物理内存上与 P4 盘中彻底分离。
2. 📊 上下分层布局：上半区专注“盘尾/盘后”决战 (P4/P5)，下半区统管“底仓/早盘/盘中”常规战 (P1/P2/P3)。
3. 🚀 缓存加固：刷新/跨日态由 session 与落盘缓存兜底；**渲染层禁止** `@st.cache_resource` 持有 `duckdb.connect` 句柄，查库一律短时 `with get_read_conn(read_only=True)`；仅允许 `@st.cache_data(ttl=60)` 缓存 DataFrame/元组等可序列化结果。
4. ✨ 列序优化：将全局 5 大股票池的“现价”列提前至“综合分”前方，提升复盘视觉动线。
"""
import sys
import os

# 必须优先于 PYTHONPATH / 其它路径上的同名包，否则可能解析到错误的 `core`（无 scan_engine）。
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# 【SAC 兜底】Win11 Smart App Control 会拒载 pyarrow._compute.pyd，导致 pandas/streamlit
# 全部挂掉。在 import pandas / import streamlit 之前先把 pyarrow 屏蔽掉，pandas 会 fallback
# 到 NumPy backend，业务侧 DataFrame 功能不受影响。彻底修复仍需关闭 SAC（见模块文档）。
from core import _pyarrow_compat as _pyarrow_compat  # noqa: E402
_pyarrow_compat.disable_pyarrow()  # 受 XIAOJIE_DISABLE_PYARROW=1 控制；start_server.bat 默认已开启

# 【V26.88+ 10-02 修复】UI 启动时一次性调用 a_share_extra_sync.init_or_ensure_tables()，
# 确保 margin_data / longhubang_data / block_trade_data / unlock_schedule /
# northbound_holding / northbound_flow 这 6 张表在 P1 洗盘前就存在（即使从未触发过
# 同步任务）。_TABLES_READY 标志在模块内只生效一次，重启 Streamlit 不会重复建表。
# F2-12 的 _LAST_P1_WASH_ADAPTIVE.data_freshness_xxx 提示「HiThink 同步 6 张表不存在」时，
# 至少表 schema 会先落地，再触发 sync_all_a_share_extras(days=10) 才有目标。
try:
    from data.a_share_extra_sync import init_or_ensure_tables
    init_or_ensure_tables()
except Exception as _e_a_share_init:
    import logging as _logging
    _logging.getLogger(__name__).debug("[a_share_extra_sync] init_or_ensure_tables 失败: %s", _e_a_share_init)

# 【V26.88+ 10-14】进程互斥 sentinel：在 ui/app.py 真正 import 时由 Streamlit 工作进程
# 写文件锁，让 start_daemon_24x7.bat 启动 daemon 前能拒绝（避免 DuckDB 跨进程锁冲突）。
# bat 调用方写 sentinel 会被误识为陈旧（bat 子进程 pid 短命），必须由真 UI 进程写。
try:
    from core.process_orchestrator import acquire_my_sentinel, UI_LOCK_FILE
    acquire_my_sentinel(UI_LOCK_FILE, 'Streamlit ui/app.py')
except Exception as _e_ui_sentinel:
    import logging as _logging
    _logging.getLogger(__name__).debug("[orchestrator] 写 UI sentinel 失败: %s", _e_ui_sentinel)

try:
    from core.runtime_data_paths import (
        path_p0_cache_json,
        path_p0_cache_pkl,
        path_p0_rejected_json,
        path_p0_rejected_pkl,
        path_p1_cache_json,
        path_p1_cache_pkl,
        path_p1_last_wash_input_codes_json,
        path_p1_rejected_json,
        path_p1_rejected_pkl,
        path_wash_metrics_json,
        ensure_runtime_data_layout,
    )
except ImportError:
    def ensure_runtime_data_layout():
        os.makedirs("data", exist_ok=True)

    def path_wash_metrics_json():
        return os.path.join("data", "wash_metrics_history.json")

    def path_p1_cache_json(d):
        return os.path.join("data", f"p1_cache_{d}.json")

    def path_p0_cache_json(d):
        return os.path.join("data", f"p0_cache_{d}.json")

    def path_p1_cache_pkl(d):
        return os.path.join("data", f"p1_cache_{d}.pkl")

    def path_p0_cache_pkl(d):
        return os.path.join("data", f"p0_cache_{d}.pkl")

    def path_p1_rejected_json():
        return os.path.join("data", "p1_rejected_cache.json")

    def path_p0_rejected_json():
        return os.path.join("data", "p0_rejected_cache.json")

    def path_p1_rejected_pkl():
        return os.path.join("data", "p1_rejected_cache.pkl")

    def path_p0_rejected_pkl():
        return os.path.join("data", "p0_rejected_cache.pkl")

    def path_p1_last_wash_input_codes_json():
        return os.path.join("data", "p1_last_wash_input_codes.json")

try:
    import constants
except ImportError:

    class _ConstShim:
        APP_VERSION = "V32.0"

    constants = _ConstShim()

import time
import gc
import copy
import pickle
import json
import io
import re
import html as html_escape
from collections import Counter
from typing import Any, Dict, List, Optional
import pandas as pd
import numpy as np
import streamlit as st
import altair as alt  
import logging
from datetime import datetime, timedelta, timezone

from core.file_utils import atomic_json_update


def _suppress_streamlit_missing_script_run_ctx_warning() -> None:
    """PyArrow 并行转 Arrow 等会在子线程触发 get_script_run_ctx，属已知无害噪音。"""
    class _F(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            try:
                if "missing ScriptRunContext" in record.getMessage():
                    return False
            except Exception:
                pass
            return True

    logging.getLogger("streamlit.runtime.scriptrunner_utils.script_run_context").addFilter(_F())


_suppress_streamlit_missing_script_run_ctx_warning()

# 启动时 P1 回溯补水最多拉取 K 线只数（避免历史分数缓存过大时拖死首屏）
_P1_STARTUP_HYDRATE_MAX = 25


@st.cache_data(ttl=60)
def _ui_cached_distinct_ts_codes_latest_trade_date() -> tuple[str, ...]:
    """
    P1 洗盘「终极兜底」用：最新交易日 distinct ts_code。
    仅缓存不可变元组；数据库连接在 with 内短时打开并立即释放，绝不缓存连接句柄。
    """
    try:
        from data.db_core import get_read_conn

        _q = """
            SELECT DISTINCT ts_code
            FROM daily_data
            WHERE trade_date = (SELECT MAX(trade_date) FROM daily_data)
        """
        with get_read_conn(read_only=True) as con:
            rel = con.execute(_q)
            try:
                df = rel.df()
                if df is not None and not df.empty and "ts_code" in df.columns:
                    return tuple(str(x) for x in df["ts_code"].tolist())
            except Exception:
                pass
            rows = con.execute(_q).fetchall()
            return tuple(str(r[0]) for r in rows if r and r[0])
    except Exception:
        return tuple()


st.set_page_config(
    page_title=f"小杰AI选股系统 Pro {constants.APP_VERSION}",
    page_icon="🛡️",
    layout="wide",
    initial_sidebar_state="expanded"
)

try:
    from core.master_control import is_maintenance_mode_enabled

    if is_maintenance_mode_enabled():
        st.error(
            "🚨 **系统处于数据库维护模式**（`maintenance_mode=true`）：守护进程已暂停写库与定时链。"
            "请等待 `force_maintenance_vacuum` 等运维控制台流程结束；期间请减少刷新与本页的洗盘 / 扫描操作，"
            "以降低与 CHECKPOINT/VACUUM 的文件锁冲突。"
        )
except Exception:
    pass

try:
    import ui.ui_sidebar as ui_sidebar
    import ui.ui_components as ui_components
    from ui.display_labels import POOL_KEY_CN, style_dataframe_center
    from core.regime_analyzer import get_market_regime
    import constants
    from core.p1_score_display import normalize_p1_score_details_for_display, p1_score_details_to_rows
    from core.stock_name_utils import normalize_stock_display_name
    from ui.engine_lazy import (
        run_scan_engine,
        get_realtime_sector_ranking,
        fetch_realtime_batch,
        run_batch_backtest,
        build_p1_pool_and_cache,
        get_last_p1_observation_pool,
        get_last_p1_wash_adaptive,
        get_all_stock_codes,
        get_stock_data_qfq,
        get_p1_candidate_codes,
        save_p1_cache,
        load_p1_cache,
        get_stock_industry,
        get_latest_sector_ranking,
        get_all_basic_industry,
        precompute_indicators,
        dehydrate_base_items_list,
        dehydrate_scan_nested_fragment,
        dehydrate_scan_results_list,
        rehydrate_base_items_for_scan_engine,
    )
except ImportError as e:
    st.error(f"🚨 核心引擎导入失败: {e}")
    st.stop()

ui_components.inject_custom_css()


def _normalize_score_details(raw: Any) -> Optional[Dict[str, Any]]:
    """session / 缓存中的 score_details 可能是 dict，或历史遗留的 JSON 字符串。"""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            obj = json.loads(raw)
            return obj if isinstance(obj, dict) else None
        except Exception:
            return None
    return None


def _rejected_rows_dataframe_for_display(rows: Any) -> "pd.DataFrame":
    """阵亡/追溯表：列名「score_details」对用户显示为「评分详情」。

    【V26.88+9-24 新增】字段精简：
    - 合并 满分项1/满分项2 → 满分项(前3)
    - 合并 最低项1/最低项2 → 最低项(前3)
    - 删除 被裁阶段（全员同一阶段，无信息量）
    - 新增 距及格线差值（直观看复活所需分数）
    """
    # 【批次 A】死因分级补全：防久多处调用方提供的 rows 未含新字段
    # （如旧 pickle 缓存、手工构造的 rows）。只加列，不改原文。
    try:
        from core.p1_death_cause_registry import enrich_rejected_rows as _enr

        rows = _enr(list(rows) if rows else [])
    except Exception:
        pass

    df = pd.DataFrame(rows)
    if "score_details" in df.columns:
        df = df.rename(columns={"score_details": "评分详情"})

    # 【V26.88+9-24】合并 满分项1+2 → 满分项(前3)
    if "满分项1" in df.columns or "满分项2" in df.columns:
        tops = []
        for i, row in df.iterrows():
            top_parts = []
            for col in ("满分项1", "满分项2"):
                v = str(row.get(col, "") or "").strip()
                if v and v != "--":
                    top_parts.append(v)
            tops.append("、".join(top_parts) if top_parts else "--")
        df["满分项(前3)"] = tops
        df = df.drop(columns=[c for c in ("满分项1", "满分项2") if c in df.columns], errors="ignore")

    # 【V26.88+9-24】合并 最低项1+2 → 最低项(前3)
    if "最低项1" in df.columns or "最低项2" in df.columns:
        worsts = []
        for i, row in df.iterrows():
            worst_parts = []
            for col in ("最低项1", "最低项2"):
                v = str(row.get(col, "") or "").strip()
                if v and v != "--":
                    worst_parts.append(v)
            worsts.append("、".join(worst_parts) if worst_parts else "--")
        df["最低项(前3)"] = worsts
        df = df.drop(columns=[c for c in ("最低项1", "最低项2") if c in df.columns], errors="ignore")

    # 【V26.88+9-24】删除 被裁阶段（全员同一阶段）
    if "被裁阶段" in df.columns:
        df = df.drop(columns=["被裁阶段"])

    # 【批次 A】层级列插在「淘汰死因」之后，不做嵌套展开
    # 原因：30 行 × 30 闸的嵌套会把页面压滞；分层 + 一句话建议已能解决 90% 的认知负拉。
    _prefer = ["代码", "名称", "死因层级", "层级徽标", "淘汰死因",
               "处置建议", "当前得分", "距及格线差值",
               "满分项(前3)", "最低项(前3)"]
    if len(df.columns):
        _rest = [c for c in df.columns if c not in _prefer]
        df = df[[c for c in _prefer if c in df.columns] + _rest]

    # 【V26.88+9-24】新增 距及格线差值（= pass_line - 当前得分）
    if "当前得分" in df.columns:
        gaps = []
        for _, row in df.iterrows():
            try:
                sc = float(row.get("当前得分", 0) or 0)
            except (TypeError, ValueError):
                sc = 0.0
            try:
                pl = float(row.get("有效及格线", 0) or 0)
            except (TypeError, ValueError):
                try:
                    pl = float(row.get("基础及格线", 0) or 0)
                except (TypeError, ValueError):
                    pl = 50.0
            if pl > 0 and sc > 0:
                gaps.append(f"{pl - sc:+.2f}")
            else:
                gaps.append("--")
        df["距及格线差值"] = gaps

    return df


def _p1_sector_trend_display(score_details_raw: Any) -> str:
    sd = _normalize_score_details(score_details_raw)
    if not isinstance(sd, dict):
        return "--"
    try:
        bonus = float(sd.get("板块趋势加分", 0.0) or 0.0)
    except (TypeError, ValueError):
        bonus = 0.0
    label = str(sd.get("板块趋势标签", "") or "").strip()
    if bonus <= 0:
        return "--"
    if label and label != "--":
        return f"+{bonus:.1f}｜{label}"
    return f"+{bonus:.1f}"


def _render_p1_score_breakdown_dataframe(score_details_raw: Any) -> None:
    """P1 多维分项表：固定四列顺序；缺数据或渲染失败时给出明确提示，避免静默 st.json 看起来像「没有列」。"""
    sd = _normalize_score_details(score_details_raw)
    if sd is None:
        st.info(
            "暂无「分项拆解」明细（评分详情缺失或无法解析）。请重新执行【启动洗盘】以写入完整分项。"
        )
        return
    sd = normalize_p1_score_details_for_display(sd)
    if not sd:
        st.info("当前条目的评分详情为空。请重新执行【启动洗盘】。")
        return
    rows = p1_score_details_to_rows(sd)
    if not rows:
        st.warning("分项行列表为空，请重试或检查引擎版本。")
        return
    df = pd.DataFrame(rows)
    _col_order = ["维度", "项满分", "得分或内容", "说明"]
    for c in _col_order:
        if c not in df.columns:
            df[c] = ""
    df = df[_col_order]
    try:
        st.dataframe(df, width="stretch", hide_index=True)
    except Exception as e:
        st.error(f"表格渲染失败（以下为原始评分详情供排查）：{e}")
        st.json(sd)


def _load_wash_metrics_history():
    ensure_runtime_data_layout()
    p = path_wash_metrics_json()
    if not os.path.exists(p):
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception as e:
        logging.warning(f"读取洗盘日报历史失败: {e}")
        return {}


def _load_tier_streaks_ui():
    """# 【多层级池子】从 wash_metrics_history 读取连续无主池日数，供侧边栏展示。"""
    root = _load_wash_metrics_history()
    meta = root.get("__tier_pool_meta__") if isinstance(root, dict) else None
    if not isinstance(meta, dict):
        return 0, 0
    return (
        int(meta.get("p1_empty_streak", 0) or 0),
        int(meta.get("scan_empty_streak", 0) or 0),
    )


def _calc_wash_metrics(base_items, rejected_items):
    pool_count = len(base_items) if base_items else 0
    score_vals = [float(x.get("p1_score", 0.0)) for x in (base_items or [])]
    score_max = max(score_vals) if score_vals else 0.0
    reasons = [str(x.get("淘汰死因", "")) for x in (rejected_items or []) if x.get("淘汰死因")]
    reason_counter = Counter(reasons)
    top_reason, top_reason_count = ("--", 0)
    if reason_counter:
        top_reason, top_reason_count = reason_counter.most_common(1)[0]
    rejected_count = len(rejected_items) if rejected_items else 0
    top_reason_ratio = (top_reason_count / rejected_count * 100.0) if rejected_count > 0 else 0.0
    return {
        "pool_count": int(pool_count),
        "max_score": round(float(score_max), 2),
        "top_reason": top_reason,
        "top_reason_ratio": round(float(top_reason_ratio), 1),
    }


def _p1_data_anchor_yyyymmdd():
    """与 P1 引擎一致：以日线库最新交易日为键，休市日重复洗盘不另开日历列、缓存路径与数据日对齐。"""
    try:
        from data.db_core import get_latest_daily_data_trade_date_yyyymmdd

        a = (get_latest_daily_data_trade_date_yyyymmdd() or "").strip()
        if len(a) == 8 and a.isdigit():
            return a
    except Exception as e:
        logging.debug("_p1_data_anchor_yyyymmdd: %s", e)
    return datetime.now().strftime("%Y%m%d")


def _wash_metrics_calendar_yyyymmdd():
    """
    洗盘日报 JSON 归档键：北京时间日历日（YYYYMMDD）。

    与 _p1_data_anchor_yyyymmdd（数据锚定日）解耦，避免「今日操作」因库内锚定仍停在旧交易日
    而写入/覆盖历史键，导致微趋势表列日期与真实操作日错位。
    """
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d")


def _record_wash_metrics(mode, base_items, rejected_items):
    today = _wash_metrics_calendar_yyyymmdd()
    try:
        ensure_runtime_data_layout()
        p = path_wash_metrics_json()

        def _upd(data: Dict[str, Any]) -> None:
            if today not in data:
                data[today] = {}
            data[today][mode] = _calc_wash_metrics(base_items, rejected_items)

        atomic_json_update(p, _upd, timeout=5)
    except Exception as e:
        logging.warning(f"写入洗盘日报历史失败: {e}")


def render_wash_daily_card(mode):
    hist = _load_wash_metrics_history()
    if not hist:
        st.info("📘 洗盘结果日报：暂无历史记录（完成一次洗盘后自动生成）。")
        return

    today = _wash_metrics_calendar_yyyymmdd()
    today_metrics = (hist.get(today) or {}).get(mode)
    if not today_metrics:
        st.info("📘 洗盘结果日报：今日暂无记录（请先执行一次洗盘）。")
        return

    # 【多层级池子】说明：wash_metrics 文件内可能含 __tier_pool_meta__，日期键须为 8 位数字才参与日报
    all_dates = sorted(k for k in hist.keys() if str(k).isdigit() and len(str(k)) == 8)
    prev_metrics = None
    for d in reversed(all_dates):
        if d < today and mode in (hist.get(d) or {}):
            prev_metrics = hist[d][mode]
            break

    def _delta(curr, prev):
        if prev is None:
            return "N/A"
        try:
            dv = float(curr) - float(prev)
            if abs(dv) < 1e-9:
                return "0"
            arrow = "↑" if dv > 0 else "↓"
            return f"{arrow}{abs(dv):.1f}"
        except Exception as e:
            # 【审计修复】维度6-洗盘日报环比计算异常可观测
            logging.debug("render_wash_daily_card _delta: %s", e)
            return "N/A"

    pool_delta = _delta(today_metrics.get("pool_count", 0), None if not prev_metrics else prev_metrics.get("pool_count", 0))
    score_delta = _delta(today_metrics.get("max_score", 0.0), None if not prev_metrics else prev_metrics.get("max_score", 0.0))
    ratio_delta = _delta(today_metrics.get("top_reason_ratio", 0.0), None if not prev_metrics else prev_metrics.get("top_reason_ratio", 0.0))

    st.markdown(
        f"""
<div style='background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;padding:12px 14px;margin:6px 0 12px 0;line-height:1.65;'>
  <div style='font-weight:700;color:#1e293b;'>📘 洗盘结果日报（{mode}）</div>
  <div style='color:#475569;'>对比基准：{("昨日/最近一次" if prev_metrics else "无历史可比")}</div>
  <div>1) 入池数量：<b>{today_metrics.get("pool_count", 0)}</b> <span style='color:#64748b;'>({pool_delta})</span></div>
  <div>2) 最高分：<b>{today_metrics.get("max_score", 0.0):.2f}</b> <span style='color:#64748b;'>({score_delta})</span></div>
  <div>3) 头号拦截占比：<b>{today_metrics.get("top_reason_ratio", 0.0):.1f}%</b> <span style='color:#64748b;'>({ratio_delta})</span></div>
  <div style='color:#64748b;'>头号拦截：{today_metrics.get("top_reason", "--")}</div>
</div>
""",
        unsafe_allow_html=True
    )

    # 最近 5 个有记录交易日的微趋势（只读展示，不影响策略）
    trend_rows = []
    for d in sorted(k for k in hist.keys() if str(k).isdigit() and len(str(k)) == 8):
        m = (hist.get(d) or {}).get(mode)
        if not m:
            continue
        trend_rows.append({
            "date": d,
            "pool_count": float(m.get("pool_count", 0)),
            "max_score": float(m.get("max_score", 0.0)),
            "top_reason_ratio": float(m.get("top_reason_ratio", 0.0)),
        })
    trend_rows = trend_rows[-5:]
    if trend_rows:
        df_trend = pd.DataFrame(trend_rows)
        df_trend["date_label"] = df_trend["date"].astype(str).str.slice(4, 8)
        st.markdown(
            "<div style='text-align:center;margin-top:0.5rem;'><span style='color:#64748b;font-size:0.9rem;'>"
            "最近5天微趋势（仅观察，不改策略）</span></div>",
            unsafe_allow_html=True,
        )
        tab_data: Dict[str, List[Any]] = {
            "指标": ["入池数", "最高分", "头号拦截占比(%)"],
        }
        for _, r in df_trend.iterrows():
            lbl = str(r["date_label"])
            tab_data[lbl] = [
                str(int(r["pool_count"])),
                f"{float(r['max_score']):.2f}",
                f"{float(r['top_reason_ratio']):.1f}",
            ]
        _st_markdown_centered_table(pd.DataFrame(tab_data))


def _st_markdown_centered_table(df: pd.DataFrame) -> None:
    """Streamlit 的 st.dataframe 常无法稳定呈现单元格居中，漏斗区改用 HTML 表格。"""
    if df is None or df.empty:
        return
    cols = list(df.columns)
    th = "<tr>" + "".join(
        f"<th style='text-align:center;padding:8px 12px;border-bottom:1px solid #e5e7eb;font-weight:600'>{html_escape.escape(str(c))}</th>"
        for c in cols
    ) + "</tr>"
    trs = []
    for _, row in df.iterrows():
        tds = "".join(
            f"<td style='text-align:center;padding:8px 12px'>{html_escape.escape(str(v))}</td>"
            for v in row
        )
        trs.append(f"<tr>{tds}</tr>")
    tbl = (
        "<div style='display:flex;justify-content:center;width:100%;margin:0.25rem 0 0.75rem 0;'>"
        "<table style='border-collapse:collapse;font-size:14px;color:#1e293b'>"
        f"<thead>{th}</thead><tbody>{''.join(trs)}</tbody></table></div>"
    )
    st.markdown(tbl, unsafe_allow_html=True)


_FUNNEL_METRIC_COLS = ["战区", "候选", "战法核对", "门禁通过", "命中战法", "入池", "入池率"]
_FUNNEL_PREV_SNAPSHOT_KEY = "_funnel_prev_snapshot"


def _funnel_pool_row_dict(
    funnel: Dict[str, Any],
    pool_key: str,
    pool_name_map: Dict[str, str],
) -> Optional[Dict[str, Any]]:
    """从单次扫描的 funnel 子树取出某一池的一行指标；无有效候选则 None。"""
    f = funnel.get(pool_key)
    if not isinstance(f, dict):
        return None
    total = int(f.get("total_candidates", 0))
    if total <= 0:
        return None
    p1_ok = int(f.get("enter_strategy_check", f.get("pass_p1_gene", 0)))
    gate_ok = int(f.get("pass_golden_gate", 0))
    hit_ok = int(f.get("hit_strategy", 0))
    score_ok = int(f.get("pass_score", 0))
    return {
        "战区": pool_name_map.get(pool_key, pool_key.upper()),
        "候选": total,
        "战法核对": p1_ok,
        "门禁通过": gate_ok,
        "命中战法": hit_ok,
        "入池": score_ok,
        "入池率": f"{(score_ok / total * 100.0):.1f}%" if total > 0 else "0.0%",
    }


def _funnel_dash_row(zone_label: str) -> Dict[str, Any]:
    """本轮或上轮缺该池数据时的占位行。"""
    return {
        "战区": zone_label,
        "候选": "—",
        "战法核对": "—",
        "门禁通过": "—",
        "命中战法": "—",
        "入池": "—",
        "入池率": "—",
    }


def _build_funnel_scan_compare_rows(
    funnel_curr: Dict[str, Any],
    funnel_prev: Dict[str, Any],
    pool_name_map: Dict[str, str],
) -> List[tuple]:
    """
    左=本轮扫描、右=上一轮点击扫描；按 P2~P5 对齐成行。仅当至少一侧该池有候选时输出一行。
    """
    out: List[tuple] = []
    for pk in ("p2", "p3", "p4", "p5"):
        label = pool_name_map.get(pk, pk.upper())
        left = _funnel_pool_row_dict(funnel_curr, pk, pool_name_map)
        right = _funnel_pool_row_dict(funnel_prev, pk, pool_name_map)
        if left is None and right is None:
            continue
        if left is None:
            left = _funnel_dash_row(f"{label}（未扫）")
        if right is None:
            right = _funnel_dash_row(f"{label}（无）")
        out.append((left, right))
    return out


def _render_scan_funnel_wide_14col_scan_compare(row_pairs: List[tuple]) -> None:
    """
    14 列：左 7 列=最近一次按钮扫描，右 7 列=再上一次扫描（session 内记忆，刷新页面后清空）。
    """
    if not row_pairs:
        return
    esc = html_escape.escape
    group_row = (
        "<tr>"
        "<th colspan='7' style='text-align:center;padding:4px 6px;border:1px solid #e5e7eb;"
        "background:#eff6ff;font-weight:700;font-size:11px;color:#1d4ed8'>本轮（最近）</th>"
        "<th colspan='7' style='text-align:center;padding:4px 6px;border:1px solid #e5e7eb;"
        "background:#fefce8;font-weight:700;font-size:11px;color:#a16207'>上轮</th>"
        "</tr>"
    )
    _th_base = (
        "text-align:center;padding:4px 6px;border:1px solid #e5e7eb;"
        "background:#f8fafc;font-weight:600;font-size:11px;"
        "overflow-wrap:anywhere;word-break:break-word;vertical-align:middle"
    )
    th_cells = []
    for _ in range(2):
        for c in _FUNNEL_METRIC_COLS:
            th_cells.append(
                f"<th style='{_th_base}'>{esc(str(c))}</th>"
            )
    thead = group_row + "<tr>" + "".join(th_cells) + "</tr>"

    # 左右各 7 列均分整表宽度（50% / 50%），避免「战区」文案长短导致两侧列宽错位
    _col_w = f"{100.0 / 14.0:.5f}%"
    colgroup = "<colgroup>" + "".join(f'<col style="width:{_col_w}" />' for _ in range(14)) + "</colgroup>"
    _cell = (
        "text-align:center;padding:4px 6px;border:1px solid #e5e7eb;"
        "overflow-wrap:anywhere;word-break:break-word;vertical-align:middle"
    )

    body_trs = []
    for left, right in row_pairs:
        tds = []
        for key in _FUNNEL_METRIC_COLS:
            v = left.get(key, "")
            tds.append(
                f"<td style='{_cell};font-size:12px'>"
                f"{esc(str(v))}</td>"
            )
        for key in _FUNNEL_METRIC_COLS:
            v = right.get(key, "")
            tds.append(
                f"<td style='{_cell};font-size:12px'>"
                f"{esc(str(v))}</td>"
            )
        body_trs.append("<tr>" + "".join(tds) + "</tr>")

    html = (
        "<div style='width:100%;overflow-x:auto;margin:0.25rem 0 0.75rem 0;'>"
        "<table style='border-collapse:collapse;table-layout:fixed;width:100%;max-width:100%;"
        "color:#1e293b'>"
        f"{colgroup}<thead>{thead}</thead><tbody>{''.join(body_trs)}</tbody></table></div>"
    )
    st.markdown(html, unsafe_allow_html=True)


def render_scan_funnel_card(scan_results):
    funnel = (scan_results or {}).get("funnel", {})
    _sop = (scan_results or {}).get("sop_market_breaker") or {}
    if isinstance(_sop, dict) and _sop.get("active"):
        st.warning(
            "SOP 大盘防空洞（指数）："
            + html_escape.escape(str(_sop.get("message", "") or "已触发"))
        )
    pool_name_map = {k: POOL_KEY_CN[k] for k in ("p2", "p3", "p4", "p5")}
    _fp = st.session_state.get(_FUNNEL_PREV_SNAPSHOT_KEY)
    funnel_prev: Dict[str, Any] = _fp if isinstance(_fp, dict) else {}
    row_pairs = _build_funnel_scan_compare_rows(funnel, funnel_prev, pool_name_map)
    if not row_pairs:
        return
    st.markdown(
        "<h4 style='text-align:center;margin:0 0 0.5rem 0;width:100%;font-weight:700;"
        "color:#1e293b;letter-spacing:0.02em;line-height:1.35'>"
        "🧪 <span style='font-size:1.35rem;font-weight:800'>扫描漏斗</span>"
        "<span style='font-size:1.05rem;font-weight:600'>（只读）</span>"
        "</h4>",
        unsafe_allow_html=True,
    )
    st.markdown(
        "<div style='text-align:center;margin:0 auto 0.65rem;max-width:52rem;padding:0 0.75rem;"
        "color:#475569;font-size:14px;line-height:1.65;'>"
        "<div style='margin:0 auto 0.45rem;text-align:left;max-width:48rem;color:#334155'>"
        "<span style='font-weight:600;color:#1e293b'>入池</span>："
        "<span style='font-weight:600'>命中战法且通过黄金门禁</span>后即出现在上方各池表格；"
        "<span style='font-weight:600'>综合分</span>用于排序，不作为是否入池的硬门槛。"
        "</div>"
        "<div style='margin:0 auto 0.45rem;text-align:left;max-width:48rem;color:#334155'>"
        "<span style='font-weight:600'>综合分 60</span>：参考线，用于"
        "<span style='font-weight:600'>写入实盘 signal_log</span>与质量复盘；低于 60 仍可入池。"
        "</div>"
        "<div style='margin:0 auto 0.55rem;text-align:left;max-width:48rem;color:#334155'>"
        "<span style='font-weight:600'>实盘下单</span>建议优先综合分"
        "<span style='font-weight:600;color:#b45309'>≥85 分</span>；"
        "未达 85 时「操盘提示」列会标注，仅为纪律提醒。"
        "</div>"
        "<div style='margin:0 auto;text-align:left;max-width:48rem;color:#334155'>"
        "<span style='font-weight:600'>门禁通过</span>：满足战法后还须过量比/形态等硬门槛；未过门禁不计入「命中战法」。"
        "</div>"
        "</div>",
        unsafe_allow_html=True,
    )
    _render_scan_funnel_wide_14col_scan_compare(row_pairs)
    # 门禁拦截 Top 原因（每个战区 Top3）
    reason_rows = []
    for k in ["p2", "p3", "p4", "p5"]:
        f = funnel.get(k)
        if not isinstance(f, dict):
            continue
        reason_map = f.get("gate_block_reasons", {}) or {}
        if not isinstance(reason_map, dict) or not reason_map:
            continue
        top3 = sorted(reason_map.items(), key=lambda x: x[1], reverse=True)[:3]
        for reason, cnt in top3:
            reason_rows.append({
                "战区": pool_name_map.get(k, k.upper()),
                "门禁拦截原因": str(reason),
                "次数": int(cnt),
            })
    if reason_rows:
        st.markdown(
            "<div style='text-align:center; color:#64748b; font-size:12px; margin-top:0.25rem; margin-bottom:0.25rem;'>"
            "本轮被门禁拦截 Top 原因"
            "</div>",
            unsafe_allow_html=True
        )
        df_reason = pd.DataFrame(reason_rows)
        _st_markdown_centered_table(df_reason)


def _render_circuit_breaker_panel():
    """
    大盘指数熔断（防空洞）人工刷新区：原独立 Tab「大盘熔断」收拢至此，放在漏斗与底仓表之间，
    不改动下方各池表格列结构与样式。依赖 core.sop_v11.evaluate_market_circuit_breaker。
    """
    try:
        from core.sop_v11 import evaluate_market_circuit_breaker, load_sop_v11_config
    except ImportError as e:
        st.error(f"熔断模块加载失败: {e}")
        return

    cfg = load_sop_v11_config()
    if not cfg.get("enabled", True):
        st.caption("sop_v11.enabled=false，指数熔断逻辑已跳过。")
        return

    with st.expander("📡 大盘指数熔断（防空洞）· 手动刷新", expanded=False):
        st.caption(
            "检测沪深300/创业板指等是否跌破配置阈值；enforce_block_p4=true 时将在 P4 扫描前拦截。"
            "与上方「扫描漏斗」里来自 scan_engine 的熔断提示可并存（前者为实时拉数，后者为最近一次扫描快照）。"
        )
        if st.button("🔭 刷新指数熔断状态", key="circuit_breaker_refresh_btn"):
            brk = evaluate_market_circuit_breaker(use_cache=False)
            st.session_state["_sop_last_breaker"] = brk
        brk = st.session_state.get("_sop_last_breaker")
        if isinstance(brk, dict):
            if brk.get("active"):
                st.warning(brk.get("message", "指数熔断触发"))
            elif brk.get("skipped"):
                st.caption(f"未触发（{brk.get('skipped')}）")
            else:
                st.success("指数熔断条件：当前未触发（在时间窗内已检测）。")
            det = brk.get("details") or {}
            if det:
                st.json(det)
            st.caption(
                f"enforce_block_p4={brk.get('enforce_block_p4')} — 为 true 时指挥舱将在 P4 扫描前拦截。"
            )
        else:
            st.info("点击「刷新指数熔断状态」拉取当前指数涨跌幅。")


# ==================== 🎨 颜色嗅探与视觉渲染 ====================
def color_pct(val):
    if not isinstance(val, str): return ''
    if '%' in val:
        try:
            num = float(val.replace('%', '').strip())
            if num > 0: return 'color: #ef4444; font-weight: bold;' 
            elif num < 0: return 'color: #10b981; font-weight: bold;'
        except (TypeError, ValueError) as e:
            logging.debug(f"color_pct 解析涨幅字符串失败: {e}")
    return ''

def color_position(val):
    if not isinstance(val, str): return ''
    if '🔥' in val or '主攻' in val: return 'color: #ef4444; font-weight: bold;' 
    if '🛑' in val or '极危' in val: return 'color: #f97316; font-weight: bold;' 
    if '避险' in val or '重装' in val or '压舱' in val: return 'color: #3b82f6; font-weight: bold;' 
    if '均衡' in val or '游击' in val or '常规' in val: return 'color: #8b5cf6; font-weight: bold;' 
    return ''


def _safe_float(val, default=0.0):
    try:
        if val is None:
            return default
        if pd.isna(val):
            return default
        s = str(val).strip()
        if s in ("", "-", "None", "nan"):
            return default
        return float(val)
    except (TypeError, ValueError):
        return default


def _hist_display_turnover_f(hist: dict, close_fallback: float) -> float:
    """展示用真实换手：与引擎一致，缺失 turnover_rate_f 时用 vol×close/circ_mv 反算。"""
    try:
        from core.strategies.fund_mv_utils import effective_turnover_rate_f

        y = pd.Series(hist or {})
        cl = _safe_float((hist or {}).get("close"), close_fallback)
        return float(effective_turnover_rate_f({}, y, cl if cl > 0 else close_fallback))
    except Exception as e:
        # 【审计修复】维度6-展示换手推算失败时记录原因
        logging.debug("_hist_display_turnover_f: %s", e)
        return _safe_float((hist or {}).get("turnover_rate_f", 0.0), 0.0)


def _p1_now_bj() -> datetime:
    """量比进度用「北京时间」锚点，与 A 股交易时段一致。"""
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo("Asia/Shanghai"))
    except Exception:
        return datetime.now(timezone(timedelta(hours=8)))


def _p1_normalize_trade_date_yyyymmdd(raw: Any) -> Optional[str]:
    """
    将日线行上的 trade_date 规范为 8 位 YYYYMMDD；无法识别时返回 None。
    兼容 int、YYYYMMDD 字符串、带分隔符日期、Timestamp。
    """
    if raw is None:
        return None
    try:
        if isinstance(raw, float) and pd.isna(raw):
            return None
    except Exception:
        pass
    try:
        if isinstance(raw, pd.Timestamp):
            return raw.strftime("%Y%m%d")
    except Exception:
        pass
    if isinstance(raw, datetime):
        return raw.strftime("%Y%m%d")
    s = str(raw).strip().replace("-", "").replace("/", "")
    digits = "".join(c for c in s if c.isdigit())
    if len(digits) >= 8:
        return digits[:8]
    return None


def _p1_minutes_elapsed_a_share_session_bj(now_bj: datetime) -> float:
    """
    计算当前时刻在「09:30-11:30 + 13:00-15:00」合计 240 分钟内的已交易分钟数。
    口径：从各段起点到「当前整分」的连续经过分钟数（09:30 记 0，10:30 记 60，11:30 记 120；
    午休固定 120；13:00 记 120，15:00 记 240）。秒级未进位到下一分钟，与常见行情终端一致。
    - 早于 09:30：0
    - 11:30（含）至 13:00（不含）：上午已结束，返回 120
    - 15:00（含）之后：返回 240（调用方若在 progress 里已提前按 1.0 处理，此值仅作兜底）
    """
    h = int(now_bj.hour)
    mi = int(now_bj.minute)
    tmin = h * 60 + mi
    am_open = 9 * 60 + 30
    am_close = 11 * 60 + 30
    pm_open = 13 * 60
    pm_close = 15 * 60
    if tmin < am_open:
        return 0.0
    if tmin <= am_close:
        return float(tmin - am_open)
    if tmin < pm_open:
        return 120.0
    if tmin < pm_close:
        return 120.0 + float(tmin - pm_open)
    return 240.0


def _p1_trading_progress_ratio_for_volume(now_bj: datetime, trade_date_yyyymmdd: Optional[str]) -> Optional[float]:
    """
    盘中时间进度比例 Progress Ratio ∈ (0, 1]，用于将「当前累计成交量」放缩为「预估全日量」的分母。

    规则：
    - 无有效 trade_date：不缩放（返回 None，调用方按 vol 原值参与量比）。
    - 数据日 ≠ 今日（历史 K 线）：全日已结束，返回 1.0。
    - 今日且北京时间 ≥ 15:00：返回 1.0。
    - 今日且早于 09:30：返回 None（集合竞价/盘前，不做进度放缩，避免除零或极端放大）。
    - 今日交易时段内：Progress = max(已交易分钟数 / 240, 1/240)，避免首分钟除零。

    返回 None 表示「不对 vol 除以 progress」，即沿用原始累计量与 vol_ma5 的比值。
    """
    if not trade_date_yyyymmdd or len(trade_date_yyyymmdd) != 8:
        return None
    today = now_bj.strftime("%Y%m%d")
    if trade_date_yyyymmdd != today:
        return 1.0
    h = int(now_bj.hour)
    mi = int(now_bj.minute)
    tmin = h * 60 + mi
    am_open = 9 * 60 + 30
    pm_close = 15 * 60
    if tmin >= pm_close:
        return 1.0
    if tmin < am_open:
        return None
    elapsed = _p1_minutes_elapsed_a_share_session_bj(now_bj)
    if elapsed <= 0:
        return None
    ratio = elapsed / 240.0
    return max(ratio, 1.0 / 240.0)


def _p1_display_vol_ratio(hist: dict, item: dict) -> tuple:
    """
    P1 底仓表「量比」列用：优先直读 vol_ratio；否则用「成交量 / 近 5 日均量」推算。

    【盘中放缩】当直读无效且数据日为「今日」、处于 09:30-15:00 时：
    - 按 A 股 240 分钟交易时长计算 Progress Ratio（已交易分钟 / 240）。
    - 预估全日成交量 ≈ 当前累计成交量 / Progress Ratio，再除以 vol_ma5 得到动态量比，
      缓解 10:30 等时点「半日量 ÷ 全日均量」导致的量比严重低估。
    - 15:00（含）后或历史 K 线：Progress 视为 1.0，与收盘口径一致。
    - 09:30 前或无法识别 trade_date：不做放缩（与旧逻辑一致，避免异常放大）。

    返回 (数值, 尾标)，与历史完全一致：H=列直读，F=直读无键，C=hist 行推算，D=df 末行推算，~=兜底。
    """
    h = hist if isinstance(hist, dict) else {}
    raw = h.get("vol_ratio", h.get("vr"))
    vr_col = _safe_float(raw, float("nan"))
    has_vr_key = "vol_ratio" in h or "vr" in h

    now_bj = _p1_now_bj()

    def _row_vol_and_ma5(row_like) -> tuple:
        if row_like is None:
            return 0.0, 0.0
        try:
            if isinstance(row_like, pd.Series):
                vol = _safe_float(row_like.get("vol", row_like.get("volume", 0)), 0.0)
                vma5 = _safe_float(row_like.get("vol_ma5"), 0.0)
                if vma5 <= 0:
                    vma5 = _safe_float(row_like.get("vol_ma10"), 0.0)
            else:
                vol = _safe_float(row_like.get("vol", row_like.get("volume", 0)), 0.0)
                vma5 = _safe_float(row_like.get("vol_ma5"), 0.0)
                if vma5 <= 0:
                    vma5 = _safe_float(row_like.get("vol_ma10"), 0.0)
            return float(vol), float(vma5)
        except Exception as e:
            logging.debug("_p1_display_vol_ratio _row_vol_and_ma5: %s", e)
            return 0.0, 0.0

    def _trade_date_from_row(row_like) -> Optional[str]:
        if row_like is None:
            return None
        try:
            if isinstance(row_like, pd.Series):
                td = row_like.get("trade_date")
            else:
                td = row_like.get("trade_date")
            return _p1_normalize_trade_date_yyyymmdd(td)
        except Exception as e:
            logging.debug("_p1_display_vol_ratio _trade_date_from_row: %s", e)
            return None

    def _ratio_from_row_scaled(row_like) -> float:
        if row_like is None:
            return float("nan")
        try:
            vol, vma5 = _row_vol_and_ma5(row_like)
            if vma5 <= 0 or vol <= 0:
                return float("nan")
            td = _trade_date_from_row(row_like)
            prog = _p1_trading_progress_ratio_for_volume(now_bj, td)
            if prog is None:
                eff_vol = vol
            else:
                eff_vol = vol / max(float(prog), 1e-12)
            return eff_vol / vma5
        except Exception as e:
            logging.debug("_p1_display_vol_ratio _ratio_from_row_scaled: %s", e)
            return float("nan")

    if vr_col > 0 and np.isfinite(vr_col):
        return round(vr_col, 2), ("H" if has_vr_key else "F")

    vr_est = _ratio_from_row_scaled(h)
    if vr_est > 0 and np.isfinite(vr_est):
        return round(vr_est, 2), "C"

    df = item.get("df") if isinstance(item, dict) else None
    if isinstance(df, pd.DataFrame) and not df.empty:
        try:
            last = df.iloc[-1]
            vr_df = _ratio_from_row_scaled(last)
            if vr_df > 0 and np.isfinite(vr_df):
                return round(vr_df, 2), "D"
        except Exception as e:
            logging.debug("_p1_display_vol_ratio df tail: %s", e)

    return 1.0, "~"


def render_styled_pool(col, title, pool_id, data_list, cols, empty_msg="未扫到任何标的。", min_visible_rows: int = 10):
    """
    池子表格渲染器 (P1-P5 通用).

    参数:
        col: st.column 上下文
        title: 卡片标题
        pool_id: 'p1' / 'p2' / 'p3' / 'p4' / 'p5'
        data_list: 列表[dict]
        cols: 显示列顺序
        empty_msg: 空数据提示
        min_visible_rows: 至少可视化的行数 (Streamlit dataframe height 计算依据).
            - 默认 10 (保持旧行为, 其它池子不动)
            - P1 底仓池传入 20, 让底仓卡永远展示 ≥ 20 行, 即使当前只 15 只也不显空白
    """
    with col:
        count = len(data_list)
        if title:
            st.markdown(f"#### {title} ({count})")
        
        if not data_list:
            st.info(empty_msg)
            return
            
        df = pd.DataFrame(data_list)
        
        if df.empty:
            st.info("全局狙击未发现匹配标的。")
            return
            
        existing_cols = [c for c in cols if c in df.columns]
        df = df[existing_cols]
        
        if '综合分' in df.columns:
            df['综合分'] = pd.to_numeric(df['综合分'], errors='coerce').round(2)
        if '现价' in df.columns:
            df['现价'] = pd.to_numeric(df['现价'], errors='coerce').round(2)
            
        try:
            format_dict = {}
            if '综合分' in df.columns: format_dict['综合分'] = '{:.2f}'
            if '现价' in df.columns: format_dict['现价'] = '{:.2f}'
            
            _tbl_center = [
                {"selector": "th", "props": [("text-align", "center")]},
                {"selector": "td", "props": [("text-align", "center")]},
            ]
            styled_df = df.style.set_table_styles(_tbl_center).format(format_dict)
            
            def highlight_golden_rows(row):
                strategy = str(row.get('战法', ''))
                name_cell = str(row.get('名称', ''))
                styles = [''] * len(row)
                color_str = ''
                # 【多层级池子】说明：观察池行（战法或名称含缩量期备选 / 🔭）使用琥珀色底，与主池紫/蓝区分
                if (
                    '【缩量期备选】' in strategy
                    or '【缩量期备选】' in name_cell
                    or name_cell.strip().startswith('🔭')
                    or '高风险备选' in strategy
                    or '仅供观察' in strategy
                ):
                    color_str = 'background-color: #fff7ed; color: #c2410c; font-weight: 600; border-left: 4px solid #fb923c;'
                elif '短共振' in strategy or '短狙击' in strategy:
                    color_str = 'background-color: #8E24AA; color: #FFA726; font-weight: bold;'
                elif '月共振' in strategy:
                    color_str = 'background-color: #01579B; color: #FFFFFF; font-weight: bold;'
                
                if color_str:
                    for i, c in enumerate(row.index):
                        if c in ['代码', '名称']:
                            styles[i] = color_str
                return styles

            styled_df = styled_df.apply(highlight_golden_rows, axis=1)

            if '涨幅' in df.columns:
                if hasattr(styled_df, 'map'):
                    styled_df = styled_df.map(color_pct, subset=['涨幅'])
                else:
                    styled_df = styled_df.applymap(color_pct, subset=['涨幅'])
                    
            if '建议仓位' in df.columns:
                if hasattr(styled_df, 'map'):
                    styled_df = styled_df.map(color_position, subset=['建议仓位'])
                else:
                    styled_df = styled_df.applymap(color_position, subset=['建议仓位'])

            if '板块趋势' in df.columns:
                def color_sector_trend(v):
                    s = str(v or "")
                    if s.startswith("+"):
                        return 'background-color: #dcfce7; color: #166534; font-weight: 800;'
                    return 'color: #94a3b8;'
                if hasattr(styled_df, 'map'):
                    styled_df = styled_df.map(color_sector_trend, subset=['板块趋势'])
                else:
                    styled_df = styled_df.applymap(color_sector_trend, subset=['板块趋势'])

            if '量能爆能' in df.columns:
                def color_volume_zscore(v):
                    s = str(v or "")
                    if not s or s.strip() in ("--", "nan", "None"):
                        return 'color: #cbd5e1;'
                    if "🌋🌋" in s:
                        return 'background-color: #7f1d1d; color: #fde047; font-weight: 800; border-radius: 4px;'
                    if "🌋" in s:
                        return 'background-color: #fed7aa; color: #9a3412; font-weight: 700; border-radius: 4px;'
                    if "❄️" in s:
                        return 'background-color: #dbeafe; color: #1e3a8a; font-weight: 700; border-radius: 4px;'
                    return 'background-color: #f1f5f9; color: #334155;'
                if hasattr(styled_df, 'map'):
                    styled_df = styled_df.map(color_volume_zscore, subset=['量能爆能'])
                else:
                    styled_df = styled_df.applymap(color_volume_zscore, subset=['量能爆能'])

            # [V26.88+10-8 底仓可视扩容] 底仓池 (P1) 至少显示 min_visible_rows 行, 避免池子有
            # 15-20 只但 dataframe 默认只展示 ~10 行的截断感. 高度按 max(count, min_visible_rows)
            # 推算, 行高 35 + 头部 38; count 大于上限则不再撑高, 走原滚动行为.
            try:
                if pool_id == "p1" and min_visible_rows > 0:
                    _row_h = 35
                    _header_h = 38
                    _visible_rows = max(count, min_visible_rows)
                    _df_height = _header_h + _row_h * _visible_rows
                    st.dataframe(
                        styled_df,
                        width='stretch',
                        hide_index=True,
                        height=int(_df_height),
                    )
                else:
                    st.dataframe(styled_df, width='stretch', hide_index=True)
            except TypeError:
                # 兼容老版本 streamlit 不支持 height
                st.dataframe(styled_df, width='stretch', hide_index=True)

        except Exception as e:
            st.error(f"UI 渲染阻断: {e}")
            st.dataframe(df, width='stretch', hide_index=True)

    # 【V26.88+ 周线 buff 可视化】仅在 P3/P4/P5 池展示；P1/P2 不参与（避免噪音）
    if pool_id in ("p3", "p4", "p5"):
        _render_weekly_buff_panel(data_list, pool_id)


def _render_weekly_buff_panel(data_list, pool_id: str):
    """周线 buff 详情面板：从引擎 res["detail"] 透传的 weekly_buff_pts/trend/vol/double 渲染。
    设计为可折叠 expander，避免占用主表视线；任何一只票 buff>0 或双强时显示一行高亮。
    """
    if not data_list:
        return

    try:
        rows = []
        for r in data_list:
            if not isinstance(r, dict):
                continue
            pts = r.get("weekly_buff_pts")
            if pts is None and "detail" in r and isinstance(r["detail"], dict):
                pts = r["detail"].get("weekly_buff_pts")
            try:
                pts_f = float(pts) if pts is not None else 0.0
            except (TypeError, ValueError):
                pts_f = 0.0
            if pts_f == 0.0 and not r.get("weekly_buff_double"):
                continue
            trend = r.get("weekly_buff_trend")
            vol = r.get("weekly_buff_vol")
            dbl = bool(r.get("weekly_buff_double"))
            code = str(r.get("代码", ""))
            name = str(r.get("名称", ""))
            try:
                trend_f = float(trend) if trend is not None else 0.0
                vol_f = float(vol) if vol is not None else 0.0
            except (TypeError, ValueError):
                trend_f = vol_f = 0.0
            rows.append({
                "代码": code,
                "名称": name,
                "趋势分": round(trend_f, 2),
                "量能分": round(vol_f, 2),
                "buff加成": round(pts_f, 2),
                "双强共振": "🪐是" if dbl else "--",
            })

        if not rows:
            return

        import pandas as _pd
        _df = _pd.DataFrame(rows).sort_values(["buff加成", "双强共振"], ascending=[False, False])

        label_pool = {"p3": "🌊 3档·盘中", "p4": "🕒 4档·盘尾池", "p5": "👑 5档·盘后池"}.get(pool_id, pool_id)
        with st.expander(f"🪐 周线 buff 明细（{label_pool}）— {_df.shape[0]} 只票享受加成", expanded=False):
            st.caption("周线维度为引擎自动加分依据：趋势分 / 量能分 来自周线多头 + 周线放量；双强共振=同时满足两项时触发。"
                       "buff加成越大代表对综合分贡献越高，但务必结合主战法综合判断。")
            _tbl_styles = [
                {"selector": "th", "props": [("text-align", "center")]},
                {"selector": "td", "props": [("text-align", "center")]},
            ]

            def _color_pts(v):
                try:
                    f = float(v)
                except (TypeError, ValueError):
                    return ""
                if f >= 3.0:
                    return "background-color: #7f1d1d; color: #fde047; font-weight: 800; border-radius: 4px;"
                if f >= 1.5:
                    return "background-color: #fed7aa; color: #9a3412; font-weight: 700; border-radius: 4px;"
                if f > 0:
                    return "background-color: #dcfce7; color: #166534;"
                return "color: #94a3b8;"

            def _color_double(v):
                return "background-color: #7f1d1d; color: #fde047; font-weight: 800;" if "🪐" in str(v) else ""

            _styled = (
                _df.style.set_table_styles(_tbl_styles)
                .format({"趋势分": "{:.2f}", "量能分": "{:.2f}", "buff加成": "{:+.2f}"})
            )
            if hasattr(_styled, "map"):
                _styled = _styled.map(_color_pts, subset=["buff加成"]).map(_color_double, subset=["双强共振"])
            else:
                _styled = _styled.applymap(_color_pts, subset=["buff加成"]).applymap(_color_double, subset=["双强共振"])

            st.dataframe(_styled, width="stretch", hide_index=True)
    except Exception as _e:
        # buff 面板是锦上添花，绝不能影响主流程
        try:
            st.warning(f"周线 buff 面板渲染失败（不影响主表）：{_e}")
        except Exception:
            pass


# 【审计修复】维度6-池缓存由 pickle 改为 JSON（规避反序列化 RCE），保留 .pkl 只读兼容
def _json_sanitize_scalar(v):
    if isinstance(v, (np.integer, np.int64, np.int32)):
        return int(v)
    if isinstance(v, (np.floating, np.float64, np.float32)):
        fv = float(v)
        if fv != fv:
            return None
        return fv
    if isinstance(v, np.bool_):
        return bool(v)
    if v is None:
        return None
    if isinstance(v, float) and pd.isna(v):
        return None
    if isinstance(v, pd.Timestamp):
        return v.isoformat()
    if isinstance(v, (bytes, bytearray)):
        return bytes(v).decode("utf-8", errors="replace")
    try:
        json.dumps(v)
    except (TypeError, ValueError):
        return str(v)
    return v


def _json_safe_dict(d):
    if not isinstance(d, dict):
        return {}
    out = {}
    for k, v in d.items():
        ks = str(k)
        try:
            if isinstance(v, dict):
                out[ks] = _json_safe_dict(v)
            elif isinstance(v, (list, tuple)):
                out[ks] = [
                    _json_safe_dict(x) if isinstance(x, dict) else _json_sanitize_scalar(x)
                    for x in v
                ]
            else:
                out[ks] = _json_sanitize_scalar(v)
        except Exception as e:
            logging.warning("【审计修复】维度6-hist 字段 JSON 化降级为 str: key=%s err=%s", ks, e)
            out[ks] = str(v)
    return out


def _save_base_items_json(path, items, *, p1_envelope_source=None):
    rows = []
    # 【V30.3 2026-10-17】落盘前按 code 去重：p1_cache JSON 是 P5 扫描的底仓来源，
    # 一只票出现两次会让 P5 结果、signal_log、企微推送全部重复。
    _seen_codes = set()
    for it in items or []:
        if not isinstance(it, dict):
            continue
        _code = str(it.get("code") or it.get("ts_code") or "").strip()
        if _code and _code in _seen_codes:
            continue
        if _code:
            _seen_codes.add(_code)
        row = {
            "code": it.get("code"),
            "p1_score": float(it.get("p1_score", 0) or 0),
            "hist": _json_safe_dict(it.get("hist") or {}),
        }
        df = it.get("df")
        if isinstance(df, pd.DataFrame) and not df.empty:
            row["df_split"] = json.loads(df.to_json(orient="split", date_format="iso"))
        else:
            row["df_split"] = None
        rows.append(row)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        if p1_envelope_source in ("UI_MANUAL", "DAEMON_AUTO"):
            # P1 底仓 JSON：顶层主权元数据，与守护进程 pool_manager.p1_cache_json_should_skip_daemon_overwrite 对齐
            _ts = datetime.now(timezone(timedelta(hours=8))).isoformat()
            json.dump(
                {
                    "_source": str(p1_envelope_source),
                    "_timestamp": _ts,
                    "items": rows,
                },
                f,
                ensure_ascii=False,
                indent=0,
            )
        else:
            json.dump(rows, f, ensure_ascii=False, indent=0)


def _load_base_items_json(path):
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    # 兼容新版带主权封套 {"_source","_timestamp","items"} 与旧版顶层数组
    if isinstance(raw, dict) and isinstance(raw.get("items"), list):
        rows = raw["items"]
    elif isinstance(raw, list):
        rows = raw
    else:
        rows = []
    out = []
    if not isinstance(rows, list):
        return out
    for row in rows:
        if not isinstance(row, dict):
            continue
        hist = row.get("hist") or {}
        if not isinstance(hist, dict):
            hist = {}
        sp = row.get("df_split")
        if sp:
            df = pd.read_json(io.StringIO(json.dumps(sp)), orient="split", convert_dates=False)
        else:
            df = pd.DataFrame()
        out.append({
            "code": row.get("code"),
            "p1_score": float(row.get("p1_score", 0) or 0),
            "df": df,
            "hist": hist,
        })
    return out


def _load_base_items_maybe_legacy(json_path, pkl_path, label):
    if os.path.exists(json_path):
        try:
            return _load_base_items_json(json_path)
        except Exception as e:
            logging.warning("【审计修复】维度6-读取 %s JSON 失败，将尝试 legacy pickle: %s", label, e)
    if os.path.exists(pkl_path):
        try:
            with open(pkl_path, "rb") as f:
                data = pickle.load(f)
            logging.warning(
                "【审计修复】维度6-已用 legacy pickle 加载 %s，完成一次洗盘后将写入 JSON",
                label,
            )
            return data if isinstance(data, list) else []
        except Exception as e:
            logging.warning("【审计修复】维度6-读取 %s pickle 失败: %s", label, e)
    return []


def _find_latest_nonempty_base_items_cache(glob_pattern, *, today_str, exclude_today=True):
    """
    [V26.88+9-29 ST-P0-10] 当日 P1/P0 缓存为空时（典型场景：当日洗盘结果尚未生成，
    或上一次同步/洗盘异常把 items 写空），从 pool_cache 目录里挑出「最近一次 items 非空」
    的 JSON 缓存作为 fallback，确保 UI 立刻能展示可观察的底仓规模，避免大屏显示 0 只误导判断。

    glob_pattern: 例如 ``p1_cache_*.json`` / ``p0_cache_*.json``。
    today_str: 形如 ``20260929``；如果 exclude_today=True，会跳过当日缓存（避免覆盖逻辑分歧）。
    返回: (loaded_items: list, fallback_date_str: Optional[str])
        loaded_items 非空时同时返回对应 YYYYMMDD；找不到则两项都为空。
    """
    try:
        import glob as _glob
        candidates = _glob.glob(glob_pattern)
    except Exception:
        return [], None

    # 文件名形如 ``p1_cache_20260928.json``：按日期段倒序扫，新 → 旧
    def _date_key_from_name(name: str) -> str:
        base = os.path.basename(name)
        # 取末尾 8 位数字（前缀里可能也含数字，比如 pool_cache 子目录）
        digits = "".join(ch for ch in base if ch.isdigit())
        return digits[-8:] if len(digits) >= 8 else ""

    candidates.sort(key=lambda p: _date_key_from_name(p), reverse=True)

    for path in candidates:
        date_key = _date_key_from_name(path)
        if not date_key or not date_key.isdigit() or len(date_key) != 8:
            continue
        if exclude_today and date_key == today_str:
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception as e:
            logging.debug("ST-P0-10 扫描 %s 失败: %s", path, e)
            continue
        rows = []
        if isinstance(raw, dict) and isinstance(raw.get("items"), list):
            rows = raw["items"]
        elif isinstance(raw, list):
            rows = raw
        if rows:
            return list(rows), date_key
    return [], None


def _save_rejected_json(path, items):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    # 【P1-修复】rejected 列表里可能混入上游回填的完整 item（含 df: DataFrame
    # 与 hist 的 numpy 标量）。原实现裸 json.dump 会抛
    # "Object of type DataFrame is not JSON serializable"，导致整轮洗盘被判失败。
    # 这里挂上 core.p1_balance._json_default 兜底：DataFrame 降级为列名摘要，
    # 展示字段（代码/名称/淘汰死因/当前得分…）保持原值不变。
    #
    # 【原子写】旧实现直接 open(path, "w")：一旦序列化中途抛异常，会把原文件
    # 截断成半截 JSON（2026-10-16 的 p1_rejected_cache.json 就是这样损坏的）。
    # 改为先写同目录 .tmp、fsync 后再 os.replace，失败时旧文件保持完整。
    tmp_path = f"{path}.tmp"
    try:
        from core.p1_balance import _json_default as _p1_rej_json_default
    except Exception:
        _p1_rej_json_default = None
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(
                items if isinstance(items, list) else [],
                f,
                ensure_ascii=False,
                indent=0,
                default=_p1_rej_json_default,
            )
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def _load_rejected_maybe_legacy(json_path, pkl_path, label):
    if os.path.exists(json_path):
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except Exception as e:
            logging.warning("【审计修复】维度6-读取 %s 阵亡 JSON 失败，将尝试 pickle: %s", label, e)
    if os.path.exists(pkl_path):
        try:
            with open(pkl_path, "rb") as f:
                data = pickle.load(f)
            logging.warning(
                "【审计修复】维度6-已用 legacy pickle 加载 %s 阵亡名单",
                label,
            )
            return data if isinstance(data, list) else []
        except Exception as e:
            logging.warning("【审计修复】维度6-读取 %s pickle 阵亡失败: %s", label, e)
    return []


# ==================== 全局双轨状态初始化 ====================
if 'last_scan_time' not in st.session_state: st.session_state['last_scan_time'] = "未执行"
if 'scan_results' not in st.session_state:
    st.session_state['scan_results'] = {
        'p1': [], 'p2': [], 'p3': [], 'p4': [], 'p5': [],
        'danger_buy': [], 'danger_sell': [], 'funnel': {}, 'observation': {},
        'adaptive_reason': '', 'adaptive_sample_count': 0, 'market_contraction_score': 0.0,
        'sop_market_breaker': {},
    }
if 'pool_view_mode' not in st.session_state: st.session_state['pool_view_mode'] = 'main_plus_obs'
if 'p1_observation_cache' not in st.session_state: st.session_state['p1_observation_cache'] = []
if 'pool_mode' not in st.session_state: st.session_state['pool_mode'] = 'P1'
if 'sector_rank' not in st.session_state: st.session_state['sector_rank'] = get_latest_sector_ranking()

if 'p1_base_items_cache' not in st.session_state: st.session_state['p1_base_items_cache'] = []
if 'p0_base_items_cache' not in st.session_state: st.session_state['p0_base_items_cache'] = []
if 'p1_rejected_cache' not in st.session_state: st.session_state['p1_rejected_cache'] = []
if 'p0_rejected_cache' not in st.session_state: st.session_state['p0_rejected_cache'] = []

today_str = _p1_data_anchor_yyyymmdd()
ensure_runtime_data_layout()

# ==================== 🚀 极速唤醒双轨缓存 ====================
# 【审计修复】维度6-主缓存扩展名改为 .json（内容见 _save_base_items_json）
# 按日缓存集中在 data/runtime/pool_cache/
p1_cache_file = path_p1_cache_json(today_str)
p0_cache_file = path_p0_cache_json(today_str)
p1_cache_file_legacy = path_p1_cache_pkl(today_str)
p0_cache_file_legacy = path_p0_cache_pkl(today_str)

# 🚀 绝杀修复：阵亡缓存文件剥离日期，永远读取最新状态！
p1_rejected_cache_file = path_p1_rejected_json()
p0_rejected_cache_file = path_p0_rejected_json()
p1_rejected_cache_file_legacy = path_p1_rejected_pkl()
p0_rejected_cache_file_legacy = path_p0_rejected_pkl()

# 1. 唤醒 P1 和阵亡名单
if not st.session_state['p1_base_items_cache']:
    _loaded_p1 = _load_base_items_maybe_legacy(
        p1_cache_file, p1_cache_file_legacy, "P1底仓"
    )
    # [V26.88+9-29 ST-P0-10] 当日 P1 缓存空（如：今日洗盘尚未生成 / 上一轮异常把 items 写空），
    # 立刻回退到「最近一次 items 非空」的旧日缓存，避免大屏显示 0 只误导用户重新触发历史 150 批同步。
    _fallback_date = None
    if not _loaded_p1:
        _loaded_p1, _fallback_date = _find_latest_nonempty_base_items_cache(
            os.path.join("data", "runtime", "pool_cache", "p1_cache_*.json"),
            today_str=today_str,
            exclude_today=True,
        )
        if _loaded_p1 and _fallback_date:
            logging.warning(
                "【ST-P0-10】当日 P1 缓存 %s 为空，已回退至最近非空缓存 %s（%d 只）。"
                "完整刷新请使用侧边栏「启动洗盘」。",
                p1_cache_file, _fallback_date, len(_loaded_p1),
            )
            st.session_state["_p1_fallback_date"] = _fallback_date
    if _loaded_p1:
        st.session_state['p1_base_items_cache'] = dehydrate_base_items_list(_loaded_p1)

if not st.session_state['p1_rejected_cache']:
    st.session_state['p1_rejected_cache'] = _load_rejected_maybe_legacy(
        p1_rejected_cache_file, p1_rejected_cache_file_legacy, "P1"
    )

# 2. 唤醒 P0 和阵亡名单
if not st.session_state['p0_base_items_cache']:
    _loaded_p0 = _load_base_items_maybe_legacy(
        p0_cache_file, p0_cache_file_legacy, "P0底仓"
    )
    # [V26.88+9-29 ST-P0-10] P0 同 P1：当日空时回退最近非空缓存
    if not _loaded_p0:
        _loaded_p0, _p0_fallback_date = _find_latest_nonempty_base_items_cache(
            os.path.join("data", "runtime", "pool_cache", "p0_cache_*.json"),
            today_str=today_str,
            exclude_today=True,
        )
        if _loaded_p0 and _p0_fallback_date:
            logging.warning(
                "【ST-P0-10】当日 P0 缓存 %s 为空，已回退至最近非空缓存 %s（%d 只）。",
                p0_cache_file, _p0_fallback_date, len(_loaded_p0),
            )
    if _loaded_p0:
        st.session_state['p0_base_items_cache'] = dehydrate_base_items_list(_loaded_p0)

if not st.session_state['p0_rejected_cache']:
    st.session_state['p0_rejected_cache'] = _load_rejected_maybe_legacy(
        p0_rejected_cache_file, p0_rejected_cache_file_legacy, "P0"
    )

# 如果完全没有 P1 数据，尝试回溯修复指标（仅启动时最多补水 _P1_STARTUP_HYDRATE_MAX 只；完整请「启动洗盘」）
if not st.session_state['p1_base_items_cache']:
    cached_records = load_p1_cache(today_str)
    if cached_records:
        st.session_state.pop("_p1_startup_hydrate_capped", None)
        _rows = cached_records[:_P1_STARTUP_HYDRATE_MAX]
        if len(cached_records) > _P1_STARTUP_HYDRATE_MAX:
            st.session_state["_p1_startup_hydrate_capped"] = True
        hydrated = []
        for row in _rows:
            try:
                df = get_stock_data_qfq(row['ts_code'], limit=120)
                if not df.empty:
                    df = precompute_indicators(df)
                    hydrated.append({'code': row['ts_code'], 'p1_score': row['p1_score'], 'df': df, 'hist': df.iloc[-1].to_dict()})
            except Exception as e:
                logging.warning(f"P1 缓存回溯补水失败 ts_code={row.get('ts_code', '')}: {e}")
        hydrated.sort(key=lambda x: x['p1_score'], reverse=True)
        if hydrated:
            os.makedirs('data', exist_ok=True)
            # 【审计修复】维度6-P1 回溯补水落盘改为 JSON
            # 回溯补水来自 DuckDB，非「指挥舱人工洗盘」；标为 DAEMON_AUTO 语义，晚间守护仍可覆盖刷新
            _save_base_items_json(p1_cache_file, hydrated, p1_envelope_source="DAEMON_AUTO")
            st.session_state['p1_base_items_cache'] = dehydrate_base_items_list(hydrated)

curr_mode = st.session_state.get('pool_mode', 'P1')
active_cache_key = 'p0_base_items_cache' if curr_mode == 'P0' else 'p1_base_items_cache'
active_items = st.session_state.get(active_cache_key, [])

# [V26.88+9-29 ST-P0-11] 大屏「底仓锁定数量」按 55 分准入闸过滤后再统计：
#   - 用户偏好 (V26.88+10-8 微调): "底仓锁定"代表"接近一线入场的核心持仓", 55 分闸和
#     推送企微的 p1_high_pool 60 分形成两档语义: 55+ 是底仓可选项, 60+ 是高分池推送项.
#   - 与 wash 阶段 min_score 双重保险: 即便缓存里残留了 < 55 分的票 (例如手工编辑/旧版缓存
#     写脏), 大屏也不会把它计入锁定数量, 避免出现"显示 15 只但实际只有 8 只≥55"的误导.
#   - 不修改 session_state 中的全量数据, 只在展示层筛选, 扫描引擎仍可访问原始 items.
_P1_DISPLAY_MIN_SCORE = 55.0
def _filter_p1_for_display(items, threshold):
    if not items:
        return items
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        try:
            score = float(it.get('p1_score', 0) or 0)
        except (TypeError, ValueError):
            continue
        if score >= threshold:
            out.append(it)
    return out

if active_cache_key == 'p1_base_items_cache' and active_items:
    _filtered_active_items = _filter_p1_for_display(active_items, _P1_DISPLAY_MIN_SCORE)
    _filtered_size = len(_filtered_active_items)
    if _filtered_size != len(active_items):
        logging.info(
            "【ST-P0-11】大屏 P1 显示过滤：%d → %d 只（剔除 %d 只 < %.1f 分）",
            len(active_items), _filtered_size, len(active_items) - _filtered_size, _P1_DISPLAY_MIN_SCORE,
        )
    p1_size = _filtered_size
else:
    p1_size = len(active_items)
latest_trade_date = "未同步"

# [V26.88+9-29 ST-P0-10] 若当日 P1 缓存空、走的是 fallback，latest_trade_date 应使用
# fallback 缓存的日期，避免大屏右上角显示「未同步」与底仓数量（10 只）出现自相矛盾。
_p1_fallback_date = st.session_state.get("_p1_fallback_date") if not active_items else None
_active_first_hist_date = None
if p1_size > 0:
    raw_date = active_items[0].get('hist', {}).get('trade_date', '未知')
    if len(str(raw_date)) == 8:
        _active_first_hist_date = str(raw_date)
        latest_trade_date = f"{str(raw_date)[:4]}-{str(raw_date)[4:6]}-{str(raw_date)[6:]}"
    else:
        latest_trade_date = str(raw_date)

if (not active_items) and _p1_fallback_date:
    latest_trade_date = f"{_p1_fallback_date[:4]}-{_p1_fallback_date[4:6]}-{_p1_fallback_date[6:]}"

# ==================== 📡 最高指挥部：双轨行情雷达 ====================
title_col, title_btn_col = st.columns([5, 2])
with title_col:
    st.markdown(
        f"<h2 style='margin: 0.35rem 0 0.15rem 0;'>🛡️ 小杰选股系统 Pro {constants.APP_VERSION} | 航母指挥舱</h2>",
        unsafe_allow_html=True
    )
with title_btn_col:
    st.markdown("")

# [V26.88+10-8] get_market_regime() 内部已有 try/except 降级, 这里直接调即可
# (早期版本在这里写过 app 层 try/except, 但 get_market_regime 内部已统一处理, 重复冗余)
regime_data = get_market_regime()
prim = regime_data["primary"]
sec = regime_data["secondary"]
# 副线情绪标准化：供 strat_base / 实验室等做轻量风控（与双轨 Regime 主文案解耦）
st.session_state["market_sentiment"] = str(regime_data.get("sentiment_key") or "平稳")

col_r1, col_r2 = st.columns(2)
with col_r1:
    st.markdown(f"""
    <div style="padding: 18px; border-left: 6px solid {prim['color']}; background-color: #f8fafc; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.1);">
        <h3 style="margin:0; color: #1e293b; font-size: 20px;">🎯 主 Regime (战略方向): <span style="color: {prim['color']};">{prim['status']}</span></h3>
        <p style="margin: 8px 0 0 0; color: #475569; font-size: 15px; font-weight: 500;">🔒 纪律防线: {prim['advice']}</p>
    </div>
    """, unsafe_allow_html=True)
with col_r2:
    st.markdown(
        f"""
    <div style="padding: 18px; border-left: 6px solid #3b82f6; background-color: #f8fafc; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.1);">
        <h3 style="margin:0; color: #1e293b; font-size: 20px;">⚡ 副 Regime (情绪前瞻): <span style="color: #3b82f6;">{html_escape.escape(str(sec.get('status', '')))}</span></h3>
        <p style="margin: 8px 0 0 0; color: #475569; font-size: 15px; font-weight: 500;">🔭 前线观察: {html_escape.escape(str(sec.get('desc', '')))}</p>
    </div>
    """,
        unsafe_allow_html=True,
    )

st.caption(
    "💡 策略指南：主状态定仓位与大方向，副状态示警今日短期极端情绪（二者不一致时以主状态定战略，副状态作战术提示）。"
)

st.markdown("<hr style='margin: 1.2em 0; border: 0; border-top: 1px solid #e2e8f0;'>", unsafe_allow_html=True)

raw_status = prim['status']
if "主升" in raw_status:
    st.session_state['market_regime'] = "主升浪"
elif "退潮" in raw_status or "防守" in raw_status:
    st.session_state['market_regime'] = "情绪退潮市"
else:
    st.session_state['market_regime'] = "震荡市"

curr_regime = st.session_state.get('market_regime', '震荡市')

# ==================== 渲染侧边栏（顺序：①总控台 ②池子视图 ③数据底座/下载 ④机构中控等其余）====================
# 【V26.88+10-1 ST-UI-N2】合并为单个 with st.sidebar：避免 6 次 mount 抖动 + 重复 import 子模块。
# 旧实现 6 段独立 with-st.sidebar，每个都触发 st.session_state 与 streamlit 重渲染（~30% rerun 开销）。
# 新实现：单次 sidebar，按顺序渲染各段；分支判断放在主体代码段内。
with st.sidebar:
    ui_sidebar.render_master_control_sidebar()

    if st.session_state.get("_p1_startup_hydrate_capped"):
        st.caption(
            f"ℹ️ 为加快启动，P1 仅自动补水前 {_P1_STARTUP_HYDRATE_MAX} 只 K 线；完整底仓请执行「启动洗盘」。"
        )

    st.markdown("### 📡 系统状态探针")
    st.caption("盘中 P2–P5 扫描为同步执行；定时快照与异步队列由外部守护进程负责，前端不启后台任务。")

    st.markdown("### 🔭 池子视图")
    if "pool_view_mode_radio" not in st.session_state:
        st.session_state["pool_view_mode_radio"] = (
            "仅看主池" if st.session_state.get("pool_view_mode", "main_plus_obs") == "main_only" else "主池+观察池"
        )
    _pv = st.radio(
        "展示范围",
        ["仅看主池", "主池+观察池"],
        key="pool_view_mode_radio",
        help="观察池仅在连续无票/缩量高压时由引擎填充；单票建议≤8%，仅供观察。",
    )
    st.session_state["pool_view_mode"] = "main_only" if str(_pv).startswith("仅看") else "main_plus_obs"
    _st_p1, _st_scan = _load_tier_streaks_ui()
    st.caption(
        f"今日连续无主池信号天数：1档 底仓 {_st_p1} 天 · 扫描主池 {_st_scan} 天"
    )

    st.markdown("---")
    ui_sidebar.render_data_foundation_expander()

need_radar_refresh = ui_sidebar.render_sidebar(
    p1_size=p1_size,
    latest_trade_date=latest_trade_date,
    sector_ranks=st.session_state.get('sector_rank', {}),
    regime_name=curr_regime,
)

if need_radar_refresh:
    with st.spinner("正在并发扫描全市场标的..."):
        st.session_state['sector_rank'] = get_realtime_sector_ranking()
        st.rerun()

_p1_fallback_date_for_dashboard = (
    st.session_state.get("_p1_fallback_date") if (p1_size == 0) else None
)
ui_components.render_top_dashboard(p1_size, p1_fallback_date=_p1_fallback_date_for_dashboard)

# 【Tier1 · 1.5 新增 2026-11-02】顶部数据健康度徽章（纯只读、可降级、不阻塞任何选股流程）
try:
    from ui.data_health_badge import render_data_health_badge

    render_data_health_badge()
except Exception as _dhb_err:
    logging.warning("[ui] 数据健康度徽章挂载异常（已忽略）: %s", _dhb_err)
progress_placeholder = st.empty()
progress_bar = st.empty()

# 🐉 核心重构：五核心五按钮，上2下3排版
st.markdown("""
<style>
    div[data-testid="column"]:nth-of-type(1) button { background-color: #ef4444; color: white; font-weight: bold; border: none; }
    div[data-testid="column"]:nth-of-type(2) button { background-color: white; color: #1e293b; font-weight: bold; border: 1px solid #cbd5e1; }
    div[data-testid="column"]:nth-of-type(3) button { background-color: white; color: #1e293b; font-weight: bold; border: 1px solid #cbd5e1; }
</style>
""", unsafe_allow_html=True)

btn_p1_text = (
    "P1底仓池"
    if curr_mode == "P1"
    else "P1底仓池"
)

col_btn_1, col_btn_refresh, col_btn_2 = st.columns(3)
with col_btn_1: btn_p1 = st.button(btn_p1_text, width="stretch", type="primary")
with col_btn_refresh: btn_refresh_market = st.button("♻️ 刷新大盘", width="stretch")
with col_btn_2: btn_p2 = st.button("2档竞价池", width="stretch")

col_btn_3, col_btn_4, col_btn_5 = st.columns(3)
with col_btn_3: btn_p3 = st.button("3档盘中池", width="stretch")
with col_btn_4: btn_p4 = st.button("4档盘尾池", width="stretch")
with col_btn_5: btn_p5 = st.button("5档·盘后池", width="stretch")

st.markdown("<hr style='margin: 1.0em 0; border: 0; border-top: 1px solid #e2e8f0;'>", unsafe_allow_html=True)

# ==================== 🚀 阵亡基因追溯雷达 ====================
search_query = st.text_input("🔍 阵亡基因追溯 (输入股票代码或简称，追溯未入选底仓的致命原因)", placeholder="例如：中际旭创 或 300308")
st.write("") 

if search_query:
    search_q = str(search_query).strip()
    rej_key = 'p0_rejected_cache' if curr_mode == 'P0' else 'p1_rejected_cache'
    rejected_data = st.session_state.get(rej_key, [])
    
    p1_key = 'p0_base_items_cache' if curr_mode == 'P0' else 'p1_base_items_cache'
    p1_data = st.session_state.get(p1_key, [])
    
    found_in_p1 = False
    for item in p1_data:
        code = str(item.get('code', ''))
        s_code = code.split('.')[0][:6]
        name = normalize_stock_display_name(item.get("hist", {}).get("name", ""))
        if search_q in code or search_q in name:
            st.success(
                f"✅ 喜报：【{name} ({s_code})】已成功跨越重重防线，入选 {curr_mode} 底仓池！"
                f"底层基因得分: {item.get('p1_score', 0):.2f}"
            )
            _sd_ok = item.get("score_details") or item.get("评分详情")
            with st.expander("📊 多维分项平滑分 · 分项拆解（入选股）", expanded=True):
                _render_p1_score_breakdown_dataframe(_sd_ok)
            found_in_p1 = True
            break
            
    if not found_in_p1:
        found_rej = []
        for rej in rejected_data:
            code = str(rej.get('代码', ''))
            name = normalize_stock_display_name(rej.get("名称", ""))
            if search_q in code or search_q in name:
                found_rej.append(rej)
        
        if found_rej:
            st.warning(f"💀 查找到 【{search_q}】 的阵亡诊断报告：")
            try:
                _df_rj = _rejected_rows_dataframe_for_display(found_rej)
                _hide = [c for c in _df_rj.columns if c in ("score_details", "评分详情")]
                _df_show = _df_rj.drop(columns=_hide) if _hide else _df_rj
                st.dataframe(style_dataframe_center(_df_show), width="stretch", hide_index=True)
            except Exception:
                st.dataframe(pd.DataFrame(found_rej), width="stretch", hide_index=True)
            for _rj in found_rej:
                _sd_r = (_rj.get("score_details") or _rj.get("评分详情")) if isinstance(_rj, dict) else None
                _nm = normalize_stock_display_name(_rj.get("名称", ""))
                _cd = str(_rj.get("代码", ""))
                with st.expander(f"📊 多维分项拆解 · {_nm} ({_cd}) — 淘汰仍展示各分项", expanded=False):
                    _render_p1_score_breakdown_dataframe(_sd_r)
        else:
            st.info(f"⚠️ 未找到 【{search_q}】 的扫描记录。可能原因：1. 今日系统未执行洗盘； 2. 该股上市时间过短； 3. 输入有误。")
            
    st.markdown("<hr style='margin: 1.0em 0; border: 0; border-top: 1px dashed #e2e8f0;'>", unsafe_allow_html=True)

# ==================== 引擎调度逻辑 ====================
def _release_large_session_caches_before_heavy_job():
    """
    释放 session_state 中挂载大 DataFrame 的缓存键，降低长时间跨日运行 OOM 风险。
    对大列表类键：先 del 键（显式断开 Streamlit 状态树中的引用），再赋空列表，最后 gc.collect()。
    结构重置与页面初始化一致，不改变表格列定义与渲染样式代码。
    刷新大盘 / 启动洗盘（P1 管线）的第一步调用；P2–P5 单独扫描不调用以免误清空底仓。
    """
    _empty_scan = {
        'p1': [], 'p2': [], 'p3': [], 'p4': [], 'p5': [],
        'danger_buy': [], 'danger_sell': [], 'funnel': {}, 'observation': {},
        'adaptive_reason': '', 'adaptive_sample_count': 0, 'market_contraction_score': 0.0,
        'sop_market_breaker': {},
    }
    _big_list_keys = (
        'p1_base_items_cache',
        'p0_base_items_cache',
        'p1_rejected_cache',
        'p0_rejected_cache',
        'p1_observation_cache',
    )
    for _k in _big_list_keys:
        if _k in st.session_state:
            del st.session_state[_k]
    for _k in _big_list_keys:
        st.session_state[_k] = []
    if 'scan_results' in st.session_state:
        del st.session_state['scan_results']
    st.session_state['scan_results'] = dict(_empty_scan)
    for _k in ('_p1_lab_mock_raw_cache', '_strategy_lab_sweep_cache', 'dash_idx_cache'):
        st.session_state.pop(_k, None)
    gc.collect()


_SCAN_MUTEX_KEY = "_ui_scan_mutex"

def execute_scan(target_pool_list):
    if st.session_state.get(_SCAN_MUTEX_KEY):
        st.warning("⏳ 扫描引擎正忙，请等待当前任务完成后再试。")
        return
    st.session_state[_SCAN_MUTEX_KEY] = True
    try:
        if 'p1' in target_pool_list:
            _release_large_session_caches_before_heavy_job()
        st.session_state['last_scan_time'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        pool_mode = st.session_state.get('pool_mode', 'P1')
        active_key = 'p0_base_items_cache' if pool_mode == 'P0' else 'p1_base_items_cache'
        base_items = st.session_state.get(active_key, [])

        if not base_items and 'p1' not in target_pool_list:
            progress_placeholder.warning("⚠️ 战地雷达尚未建立！请先点击左上角【1. 启动洗盘】！")
            return

        if 'p1' in target_pool_list:
            my_bar = progress_bar.progress(0.0, text="🔄 收到指令！启动极速引擎...")
            try:
                # 启动前 DuckDB 锁检测：避免进入大循环后才报文件占用
                from data.db_core import probe_duckdb_lock
                lock_state = probe_duckdb_lock()
                if not lock_state.get("ok", False):
                    pid = lock_state.get("pid")
                    msg = lock_state.get("msg", "未知锁冲突")
                    if pid:
                        progress_placeholder.error(
                            f"❌ 启动前锁检测失败：DuckDB 正被其他进程占用（PID: {pid}）。"
                            f"请先结束该进程后重试。\n\n原始错误：{msg}"
                        )
                    else:
                        progress_placeholder.error(
                            f"❌ 启动前锁检测失败：无法打开 DuckDB。请先关闭占用进程后重试。\n\n原始错误：{msg}"
                        )
                    my_bar.empty()
                    return

                target_codes = []
                if pool_mode == 'P0':
                    p0_file = getattr(constants, 'P0_FILE_PATH', 'data/p0_custom.txt')
                    if not os.path.exists(p0_file):
                        progress_placeholder.error("❌ 自选股文件不存在！请先在侧边栏上传 TXT 文件！")
                        my_bar.empty()
                        return

                    raw_lines = []
                    try:
                        with open(p0_file, 'r', encoding='utf-8') as f:
                            raw_lines = f.readlines()
                    except UnicodeDecodeError:
                        with open(p0_file, 'r', encoding='gbk', errors='ignore') as f:
                            raw_lines = f.readlines()

                    for line in raw_lines:
                        match = re.search(r'\d{6}', line)
                        if match:
                            code_str = match.group(0)
                            if code_str.startswith('6'): target_codes.append(code_str + '.SH')
                            elif code_str.startswith('8') or code_str.startswith('4'): target_codes.append(code_str + '.BJ')
                            else: target_codes.append(code_str + '.SZ')

                    target_codes = list(dict.fromkeys(target_codes))

                    if not target_codes:
                        progress_placeholder.error("❌ 未识别到有效的 6位 股票代码！请检查文件内容。")
                        my_bar.empty()
                        return

                    my_bar.progress(0.1, text="🌐 正在连接主干网获取自选股名称...")
                    rt_map = fetch_realtime_batch(target_codes)

                    new_base_items = []
                    total = len(target_codes)
                    for i, c in enumerate(target_codes):
                        my_bar.progress(0.1 + 0.9*(i / max(1, total)), text=f"📥 强行注入自选股特征 ({i+1}/{total})...")
                        df = get_stock_data_qfq(c, limit=120)
                        if not df.empty:
                            df = precompute_indicators(df)
                            hist = df.iloc[-1].to_dict()
                            s_code = c.split('.')[0][:6]
                            hist['name'] = normalize_stock_display_name(
                                rt_map.get(s_code, {}).get("name", s_code)
                            )
                            new_base_items.append({'code': c, 'p1_score': 85.0, 'df': df, 'hist': hist})

                    os.makedirs('data', exist_ok=True)
                    _save_base_items_json(p0_cache_file, new_base_items)
                    _save_rejected_json(p0_rejected_cache_file, [])
                    _record_wash_metrics("P0", new_base_items, [])
                    progress_placeholder.success(f"✅ 自选股直通车装载完毕！成功锁定 {len(new_base_items)} 只专属标的！")
                    my_bar.empty()
                    st.session_state['p0_base_items_cache'] = dehydrate_base_items_list(new_base_items)
                    st.session_state['p0_rejected_cache'] = []
                    st.rerun()

                else:
                    target_codes = get_p1_candidate_codes()

                    if not target_codes: target_codes = get_all_stock_codes()
                    if not target_codes:
                        ind_map = get_all_basic_industry()
                        if ind_map:
                            target_codes = list(ind_map.keys())
                    if not target_codes:
                        try:
                            target_codes = list(_ui_cached_distinct_ts_codes_latest_trade_date())
                        except Exception as e:
                            logging.error(f"P1终极兜底查询失败: {e}")
                    target_codes = list(dict.fromkeys(target_codes)) if target_codes else []

                    if not target_codes:
                        progress_placeholder.error("❌ 数据提取失败！未获取到候选股票代码，请先执行数据同步（历史/近期）后再启动全量洗盘。")
                        my_bar.empty()
                        try:
                            from core.notification_gateway import notify_wechat_system_alert
                            notify_wechat_system_alert(
                                title="P1 洗盘：未获取候选代码",
                                detail="候选接口与 DuckDB 兜底均未返回股票代码，请先完成日线同步。",
                                category="scan_p1",
                                dedup_key="ui_p1_no_candidate_codes",
                            )
                        except Exception:
                            pass
                        return

                    my_bar.progress(0.05, text="🌐 正在同步全网标的名称...")
                    rt_map = fetch_realtime_batch(target_codes)

                    mock_raw_data = []
                    empty_df_codes = []
                    total = len(target_codes)
                    for i, c in enumerate(target_codes):
                        if i % max(1, total // 100) == 0:
                            my_bar.progress(0.05 + 0.95*(i / max(1, total)), text=f"📥 提取特征 ({i}/{total})...")
                        df = get_stock_data_qfq(c, limit=120)
                        if not df.empty:
                            hist = df.iloc[-1].to_dict()
                            s_code = c.split('.')[0][:6]
                            hist['name'] = normalize_stock_display_name(
                                rt_map.get(s_code, {}).get("name", s_code)
                            )
                            mock_raw_data.append({'code': c, 'df': df, 'hist': hist})
                        else:
                            empty_df_codes.append(c)

                    if not mock_raw_data:
                        diag_reason = "数据库读取为空（原因待诊断）"
                        try:
                            from data.db_core import get_duckdb_path, get_read_conn, table_exists
                            db_file = get_duckdb_path()
                            if not table_exists("daily_data"):
                                diag_reason = f"未找到 daily_data 表（库文件: {db_file}）"
                            else:
                                with get_read_conn(read_only=True) as con:
                                    total_rows = con.execute("SELECT COUNT(*) FROM daily_data").fetchone()
                                    total_rows = int(total_rows[0]) if total_rows and total_rows[0] is not None else 0
                                    if total_rows <= 0:
                                        diag_reason = f"daily_data 表为空（库文件: {db_file}）"
                                    else:
                                        last_date_row = con.execute("SELECT MAX(trade_date) FROM daily_data").fetchone()
                                        last_date = last_date_row[0] if last_date_row else None
                                        if not last_date:
                                            diag_reason = f"daily_data 无有效 trade_date（库文件: {db_file}）"
                                        else:
                                            latest_rows = con.execute(
                                                "SELECT COUNT(*) FROM daily_data WHERE trade_date = ?",
                                                [last_date],
                                            ).fetchone()
                                            latest_rows = int(latest_rows[0]) if latest_rows and latest_rows[0] is not None else 0
                                            if latest_rows <= 0:
                                                diag_reason = f"最新交易日 {last_date} 无可用数据（库文件: {db_file}）"
                                            else:
                                                diag_reason = (
                                                    f"库可读(total={total_rows}, latest={last_date}/{latest_rows})，"
                                                    f"但候选股票 K 线读取全空（优先检查 DuckDB 占用或字段脏数据）"
                                                )
                        except Exception as e:
                            diag_reason = f"DuckDB 连接/查询异常: {e}"

                        rejected_items = [{
                            "代码": "--", "名称": "系统诊断", "淘汰死因": diag_reason,
                            "被裁阶段": "数据提取阶段", "当前得分": 0.0,
                            "满分项1": "--", "满分项2": "--", "最低项1": "--", "最低项2": "--"
                        }]
                        st.session_state['p1_base_items_cache'] = []
                        st.session_state['p1_rejected_cache'] = rejected_items
                        os.makedirs('data', exist_ok=True)
                        _save_rejected_json(p1_rejected_cache_file, rejected_items)
                        progress_placeholder.error(
                            f"❌ 数据提取失败：{len(target_codes)}只候选全部未取到K线。诊断信息：{diag_reason}"
                        )
                        my_bar.empty()
                        try:
                            from core.notification_gateway import notify_wechat_system_alert
                            notify_wechat_system_alert(
                                title="P1 洗盘：K 线数据提取全空",
                                detail=f"候选 {len(target_codes)} 只。{diag_reason}",
                                category="scan_p1",
                                dedup_key="ui_p1_all_kline_empty",
                            )
                        except Exception:
                            pass
                        return

                    my_bar.progress(1.0, text="🚀 打分引擎启动中...")

                    _cached_p1 = None
                    _try_use_cached = False
                    _candidate_codes_set = frozenset(str(x.get('code', '')) for x in mock_raw_data if x.get('code'))
                    try:
                        from core.pool_manager import path_p1_last_wash_input_codes_json
                        _meta_path = path_p1_last_wash_input_codes_json()
                        if os.path.exists(_meta_path):
                            with open(_meta_path, 'r', encoding='utf-8') as _mf:
                                _meta = json.load(_mf)
                            if _meta.get('saved_at') == today_str:
                                _prev_codes = frozenset(str(c) for c in _meta.get('codes', []) if c)
                                if _prev_codes == _candidate_codes_set:
                                    _try_use_cached = True
                                    logging.info("[P1幂等] 候选集未变，跳过重算 | date=%s | n=%d", today_str, len(_prev_codes))
                    except Exception:
                        pass

                    if _try_use_cached:
                        _cache_path = path_p1_cache_json(today_str)
                        if os.path.exists(_cache_path):
                            try:
                                _cached_p1 = _load_base_items_json(_cache_path)
                            except Exception:
                                _cached_p1 = None

                    if _cached_p1 is not None and len(_cached_p1) > 0:
                        base_items = _cached_p1
                        rejected_items = []
                        logging.info("[P1幂等] 从缓存恢复 | 入池=%d", len(base_items))
                    else:
                        base_items, rejected_items = build_p1_pool_and_cache(
                            mock_raw_data,
                            progress_callback=lambda p: my_bar.progress(p, text=f"⚡ 淬炼中... ({int(p*100)}%)"),
                            regime_name=curr_regime
                        )

                    _codes_lab = [x.get("code") for x in mock_raw_data if isinstance(x, dict) and x.get("code")]
                    st.session_state["p1_last_wash_input_codes"] = _codes_lab
                    st.session_state["p1_last_wash_input_revision"] = (
                        1 + int(st.session_state.get("p1_last_wash_input_revision", 0) or 0)
                    )
                    st.session_state.pop("_p1_lab_mock_raw_cache", None)
                    try:
                        _p1_lab_meta_path = path_p1_last_wash_input_codes_json()
                        os.makedirs("data", exist_ok=True)
                        with open(_p1_lab_meta_path, "w", encoding="utf-8") as _lf:
                            json.dump(
                                {
                                    "codes": _codes_lab,
                                    "revision": int(st.session_state["p1_last_wash_input_revision"]),
                                    "saved_at": today_str,
                                },
                                _lf,
                                ensure_ascii=False,
                            )
                    except Exception as _e_lab_meta:
                        logging.debug("写入 P1 实验室候选存档失败: %s", _e_lab_meta)

                    save_p1_cache(today_str, base_items)

                    os.makedirs('data', exist_ok=True)
                    _save_base_items_json(p1_cache_file, base_items, p1_envelope_source="UI_MANUAL")
                    _save_rejected_json(p1_rejected_cache_file, rejected_items)
                    _record_wash_metrics("P1", base_items, rejected_items)
                    try:
                        from core.notification_gateway import notify_p1_high_score_pool_after_wash
                        notify_p1_high_score_pool_after_wash(base_items, max_items=12)
                    except Exception as e:
                        logging.debug("P1 高分底仓企微推送: %s", e)
                    st.session_state['p1_base_items_cache'] = dehydrate_base_items_list(base_items)
                    st.session_state['p1_rejected_cache'] = rejected_items
                    st.session_state["p1_observation_cache"] = dehydrate_base_items_list(
                        get_last_p1_observation_pool()
                    )
                    progress_placeholder.success(f"✅ 洗盘完成！成功锁定 {len(base_items)} 只核心底仓！")
                    my_bar.empty()
                    st.rerun()

            except Exception as e:
                progress_placeholder.error(f"❌ 洗盘崩溃: {e}")
                my_bar.empty()
                try:
                    from core.notification_gateway import notify_wechat_system_alert
                    notify_wechat_system_alert(
                        title="P1 洗盘过程异常",
                        detail=str(e)[:900],
                        category="scan_p1",
                    )
                except Exception:
                    pass
                return

        scan_targets = [p for p in target_pool_list if p != 'p1']
        if scan_targets and base_items:
            if "p4" in scan_targets:
                try:
                    from core.sop_v11 import evaluate_market_circuit_breaker, load_sop_v11_config

                    _brk_pre = evaluate_market_circuit_breaker()
                    st.session_state["_sop_last_breaker"] = _brk_pre
                    _cb_cfg = load_sop_v11_config().get("circuit_breaker") or {}
                    if (
                        _brk_pre.get("active")
                        and bool(_cb_cfg.get("enforce_block_p4"))
                        and "p4" in scan_targets
                    ):
                        progress_placeholder.error(
                            f"🛑 SOP 指数防空洞已触发，已按配置阻止本次 4档 扫描：{_brk_pre.get('message', '')}"
                        )
                        return
                    if _brk_pre.get("active"):
                        progress_placeholder.warning(
                            f"⚠️ SOP 指数熔断提示（未强制拦截时仍可继续）：{_brk_pre.get('message', '')}"
                        )
                except Exception as _e_sop:
                    logging.debug("SOP 熔断预检: %s", _e_sop)

            with st.spinner("正在执行全池同步扫描，请稍候..."):
                start_time = time.time()
                try:
                    base_for_scan = rehydrate_base_items_for_scan_engine(base_items)
                    if not base_for_scan:
                        progress_placeholder.warning(
                            "⚠️ 战地雷达尚未建立或无法加载 K 线！请先执行洗盘或检查数据库。"
                        )
                        return
                    _eng_res = run_scan_engine(
                        target_pools=scan_targets,
                        base_items=base_for_scan,
                        regime=curr_regime,
                        progress_callback=progress_placeholder.info,
                    )
                    scan_results_update = {k: _eng_res.get(k, []) for k in scan_targets}
                    scan_results_update["danger_buy"] = _eng_res.get("danger_buy", [])
                    scan_results_update["danger_sell"] = _eng_res.get("danger_sell", [])
                    scan_results_update["funnel"] = _eng_res.get("funnel", {})
                    scan_results_update["observation"] = _eng_res.get("observation", {})

                    for k in scan_targets:
                        st.session_state['scan_results'][k] = dehydrate_scan_results_list(
                            scan_results_update.get(k, [])
                        )

                    st.session_state['scan_results']['danger_buy'] = dehydrate_scan_results_list(
                        scan_results_update.get('danger_buy', [])
                    )
                    st.session_state['scan_results']['danger_sell'] = dehydrate_scan_results_list(
                        scan_results_update.get('danger_sell', [])
                    )
                    if set(scan_targets) & {"p2", "p3", "p4", "p5"}:
                        _prev_fu = st.session_state.get("scan_results", {}).get("funnel")
                        if isinstance(_prev_fu, dict) and _prev_fu:
                            st.session_state[_FUNNEL_PREV_SNAPSHOT_KEY] = copy.deepcopy(_prev_fu)
                    st.session_state['scan_results']['funnel'] = dehydrate_scan_nested_fragment(
                        scan_results_update.get('funnel', {})
                    )
                    st.session_state['scan_results']['observation'] = dehydrate_scan_nested_fragment(
                        scan_results_update.get('observation', {})
                    )
                    st.session_state['scan_results']['adaptive_reason'] = str(_eng_res.get('adaptive_reason', '') or '')
                    st.session_state['scan_results']['adaptive_sample_count'] = int(_eng_res.get('adaptive_sample_count', 0) or 0)
                    try:
                        st.session_state['scan_results']['market_contraction_score'] = float(
                            _eng_res.get('market_contraction_score', 0.0) or 0.0
                        )
                    except (TypeError, ValueError):
                        st.session_state['scan_results']['market_contraction_score'] = 0.0

                    st.session_state["scan_results"]["sop_market_breaker"] = dehydrate_scan_nested_fragment(
                        _eng_res.get("sop_market_breaker") or {}
                    )

                    try:
                        st.session_state['sector_rank'] = get_realtime_sector_ranking()
                    except Exception as e:
                        logging.warning("【多层级池子】扫描后 sector_rank 拉取失败: %s", e)
                        st.session_state['sector_rank'] = st.session_state.get('sector_rank', {})
                    progress_placeholder.success(f"✅ {str(scan_targets).upper()} 扫描完成！耗时: {time.time() - start_time:.2f} 秒。")
                    try:
                        from core.notification_gateway import notify_scan_results_top3_p2p4

                        notify_scan_results_top3_p2p4(
                            scan_targets,
                            scan_results_update,
                            bool(st.session_state.get("wechat_notify_enabled", True)),
                        )
                    except Exception as _e_push:
                        logging.debug("企微推送旁路跳过: %s", _e_push)
                except Exception as e:
                    progress_placeholder.error(f"❌ 扫描崩溃: {e}")
                    try:
                        from core.notification_gateway import notify_wechat_system_alert

                        notify_wechat_system_alert(
                            title=f"池扫描异常：{scan_targets}",
                            detail=str(e)[:900],
                            category="scan_engine",
                            dedup_key=f"ui_scan_{','.join(scan_targets)}",
                        )
                    except Exception:
                        pass
    finally:
        st.session_state.pop(_SCAN_MUTEX_KEY, None)

if btn_p1 and not st.session_state.get(_SCAN_MUTEX_KEY): execute_scan(['p1'])
if btn_refresh_market:
    # 仅刷新顶部三大指数缓存，禁止调用全量释放（否则会清空底仓与扫描结果）
    st.session_state.pop("dash_idx_cache", None)
    st.rerun()
if btn_p2 and not st.session_state.get(_SCAN_MUTEX_KEY): execute_scan(['p2'])
if btn_p3 and not st.session_state.get(_SCAN_MUTEX_KEY): execute_scan(['p3'])
if btn_p4 and not st.session_state.get(_SCAN_MUTEX_KEY): execute_scan(['p4'])
if btn_p5 and not st.session_state.get(_SCAN_MUTEX_KEY): execute_scan(['p5'])

# ==================== 结果组装与渲染 ====================
res_dict = st.session_state.get('scan_results', {})
render_scan_funnel_card(res_dict)
_render_circuit_breaker_panel()
active_cache_key = 'p0_base_items_cache' if curr_mode == 'P0' else 'p1_base_items_cache'
active_items = st.session_state.get(active_cache_key, [])

if active_items:
    first_code = str(active_items[0].get('code', '')).split('.')[0][:6]
    first_name = normalize_stock_display_name(
        active_items[0].get("hist", {}).get("name", first_code)
    )
    if str(first_name) == str(first_code) or not first_name:
        with st.spinner("🌐 正在极速抢救股票真实中文名称..."):
            rescue_codes = [x.get('code') for x in active_items]
            rescue_map = fetch_realtime_batch(rescue_codes)
            for x in active_items:
                sc = str(x.get('code', '')).split('.')[0][:6]
                _hist = x.setdefault('hist', {})
                if not isinstance(_hist, dict):
                    _hist = {}
                    x['hist'] = _hist
                _hist['name'] = normalize_stock_display_name(
                    rescue_map.get(sc, {}).get("name", sc)
                )

base_title = "⭐ 直通车·自选底仓" if curr_mode == "P0" else "🔴 1档·底仓池"
base_strategy = "⭐ 自选股免死金牌" if curr_mode == 'P0' else "核心战略底仓"

p1_display_data = []
_wad_ui = get_last_p1_wash_adaptive()
_mcs_ui = float(_wad_ui.get("market_contraction_score") or 0.0)
_sc_ui = int(_wad_ui.get("adaptive_sample_count") or 0)
if _mcs_ui > 0 or _sc_ui > 0:
    _p1_shrink_col_txt = f"收缩度{_mcs_ui:.2f}｜样本{_sc_ui}"
else:
    _p1_shrink_col_txt = "--"
if active_items:
    for item in active_items:
        hist = item.get('hist', {})
        code = item.get('code', '')
        s_code = str(code).split('.')[0][:6]
        name = normalize_stock_display_name(hist.get("name", s_code))
        
        db_close_p = _safe_float(hist.get('close', 0.0), 0.0)
        # 与 scan_engine 一致：日线 pre_close → close，禁止默认 1 元伪造涨跌幅
        pre_c = _safe_float(hist.get('pre_close'), 0.0)
        if pre_c <= 0:
            pre_c = _safe_float(hist.get('close'), 0.0)
        pct = (db_close_p - pre_c) / pre_c * 100.0 if pre_c > 0 else 0.0
        cyq = _safe_float(hist.get('cyq_concentration', 0.0), 0.0)
        vr_val, vr_tag = _p1_display_vol_ratio(hist, item)
        # 推算量比写回 hist：刷新页面即可在会话内持久为有效列值，下次展示走直读(H)
        if isinstance(item.get("hist"), dict) and vr_tag in ("C", "D"):
            item["hist"]["vol_ratio"] = float(vr_val)
        
        circ_mv_raw = hist.get('circ_mv')
        if pd.isna(circ_mv_raw) or circ_mv_raw is None:
            circ_mv_raw = hist.get('total_mv', 0) * 0.6
        circ_mv_yi = float(circ_mv_raw) / 10000.0

        if circ_mv_yi >= 1000.0:
            size_emoji, size_label = "🦍", "巨无霸"
        elif circ_mv_yi >= 500.0:
            size_emoji, size_label = "🐘", "超级中军"
        elif circ_mv_yi >= 100.0:
            size_emoji, size_label = "🐎", "核心中盘"
        else:
            size_emoji, size_label = "🐥", "袖珍盘"

        name_with_emoji = f"{size_emoji} {name}"

        pos_advice = "🛡️常规: 15-20%"
        if curr_regime == "主升浪":
            if size_emoji == "🐎": pos_advice = "🔥主攻: 30-40%"
            elif size_emoji in ["🦍", "🐘"]: pos_advice = "🛡️压舱: 15-20%"
        elif curr_regime == "情绪退潮市":
            if size_emoji == "🐎": pos_advice = "🛑极危: 0-10% (试探)"
            elif size_emoji in ["🦍", "🐘"]: pos_advice = "🛡️重装避险: 30-40%"
        else:
            if size_emoji == "🐎": pos_advice = "⚔️游击: 15-20%"
            elif size_emoji in ["🦍", "🐘"]: pos_advice = "🛡️均衡: 20-30%"
            
        stop_loss = "破20日线" if size_emoji in ["🦍", "🐘"] else "3日未脱离成本"
        
        p1_display_data.append({
            "代码": s_code, 
            "名称": name_with_emoji, 
            "综合分": round(item.get('p1_score', 0.0), 2),
            "板块趋势": _p1_sector_trend_display(item.get("score_details") or item.get("评分详情")),
            "现价": f"{db_close_p:.2f}", 
            "涨幅": f"{pct:.2f}%",  
            "量比": f"{vr_val:.1f}({vr_tag})", 
            "真换手": f"{_hist_display_turnover_f(hist if isinstance(hist, dict) else {}, db_close_p):.1f}%", 
            "行业": get_stock_industry(code), 
            "股性": f"{size_emoji}{size_label}", 
            "建议仓位": pos_advice, 
            "纪律防线": stop_loss,
            "集中度": f"{cyq:.1f}" if cyq > 0 else "--",
            "战法": base_strategy,
            "缩量说明": _p1_shrink_col_txt,
        })

# 【多层级池子】说明：在「主池+观察池」模式下追加 P1 震荡观察底仓行（与主池分列展示、琥珀色由表格高亮）
if st.session_state.get("pool_view_mode") == "main_plus_obs":
    for item in st.session_state.get("p1_observation_cache") or []:
        if not isinstance(item, dict):
            continue
        hist = item.get("hist", {})
        code = item.get("code", "")
        s_code = str(code).split(".")[0][:6]
        name = normalize_stock_display_name(hist.get("name", s_code))
        db_close_p = _safe_float(hist.get("close", 0.0), 0.0)
        pre_c = _safe_float(hist.get("pre_close"), 0.0)
        if pre_c <= 0:
            pre_c = _safe_float(hist.get("close"), 0.0)
        pct = (db_close_p - pre_c) / pre_c * 100.0 if pre_c > 0 else 0.0
        cyq = _safe_float(hist.get("cyq_concentration", 0.0), 0.0)
        vr_val, vr_tag = _p1_display_vol_ratio(hist, item)
        circ_mv_raw = hist.get("circ_mv")
        if pd.isna(circ_mv_raw) or circ_mv_raw is None:
            circ_mv_raw = hist.get("total_mv", 0) * 0.6
        circ_mv_yi = float(circ_mv_raw) / 10000.0
        if circ_mv_yi >= 1000.0:
            size_emoji, size_label = "🦍", "巨无霸"
        elif circ_mv_yi >= 500.0:
            size_emoji, size_label = "🐘", "超级中军"
        elif circ_mv_yi >= 100.0:
            size_emoji, size_label = "🐎", "核心中盘"
        else:
            size_emoji, size_label = "🐥", "袖珍盘"
        name_with_emoji = f"🔭 【高风险备选·仅供观察】 {size_emoji} {name}"
        p1_display_data.append({
            "代码": s_code,
            "名称": name_with_emoji,
            "综合分": round(item.get("p1_score", 0.0), 2),
            "板块趋势": _p1_sector_trend_display(item.get("score_details") or item.get("评分详情")),
            "现价": f"{db_close_p:.2f}",
            "涨幅": f"{pct:.2f}%",
            "量比": f"{vr_val:.1f}({vr_tag})",
            "真换手": f"{_hist_display_turnover_f(hist if isinstance(hist, dict) else {}, db_close_p):.1f}%",
            "行业": get_stock_industry(code),
            "股性": f"{size_emoji}{size_label}",
            "建议仓位": "⚠️ 高风险备选·仅供观察 | 单票≤8%",
            "纪律防线": "观察池：仅供观察，勿重仓",
            "集中度": f"{cyq:.1f}" if cyq > 0 else "--",
            "战法": f"{base_strategy} | 高风险备选·仅供观察",
            "缩量说明": _p1_shrink_col_txt,
        })

_pvm = st.session_state.get("pool_view_mode", "main_plus_obs")
_obs = res_dict.get("observation") or {}
p2m = res_dict.get("p2", [])
p3m = res_dict.get("p3", [])
p4m = res_dict.get("p4", [])
p5m = res_dict.get("p5", [])
if _pvm == "main_plus_obs":
    s2 = {str(x.get("代码")) for x in p2m}
    s3 = {str(x.get("代码")) for x in p3m}
    s4 = {str(x.get("代码")) for x in p4m}
    s5 = {str(x.get("代码")) for x in p5m}
    p2_data = p2m + [r for r in (_obs.get("p2") or []) if str(r.get("代码")) not in s2]
    p3_data = p3m + [r for r in (_obs.get("p3") or []) if str(r.get("代码")) not in s3]
    p4_data = p4m + [r for r in (_obs.get("p4") or []) if str(r.get("代码")) not in s4]
    p5_data = p5m + [r for r in (_obs.get("p5") or []) if str(r.get("代码")) not in s5]
else:
    p2_data, p3_data, p4_data, p5_data = p2m, p3m, p4m, p5m

_scmcs = float(res_dict.get("market_contraction_score") or 0.0)
_scasc = int(res_dict.get("adaptive_sample_count") or 0)
_shrink_txt_scan = (
    f"收缩度{_scmcs:.2f}｜样本{_scasc}" if (_scmcs > 0 or _scasc > 0) else "--"
)

def enhance_pool_data(pool_data, pool_id: str = ""):
    """P2–P5 行：补全新列、用底仓 hist/df 刷新量比与集中度（与会话内未重扫的缓存对齐）。
    pool_id: 'p1'/'p2'/'p3'/'p4'/'p5'，仅 P3 引擎会写量能爆能列；其它池子显式占位 '--'。
    """
    for row in pool_data:
        # 【多层级池子】说明：观察池行已由引擎写好仓位/标签，禁止用主池逻辑覆盖
        if row.get("pool_tier") == "observation" or "【缩量期备选】" in str(row.get("战法", "")):
            row["缩量说明"] = _shrink_txt_scan
            if "量能爆能" not in row or row.get("量能爆能") in (None, ""):
                row["量能爆能"] = "--"
            continue
        if "高风险备选" in str(row.get("战法", "")) or "仅供观察" in str(row.get("战法", "")):
            row["缩量说明"] = _shrink_txt_scan
            if "量能爆能" not in row or row.get("量能爆能") in (None, ""):
                row["量能爆能"] = "--"
            continue
        if "建议最低分" not in row:
            row["建议最低分"] = "--"
        if "风险标签" not in row:
            row["风险标签"] = "--"
        if "操盘提示" not in row:
            row["操盘提示"] = "--"
        if pool_id != "p3":
            row["量能爆能"] = row.get("量能爆能") or "--"
            row["量能爆能_z"] = row.get("量能爆能_z", 0.0)
            row["量能爆能_bonus"] = row.get("量能爆能_bonus", 0.0)
        s_code = str(row.get("代码", ""))
        match = next((x for x in active_items if str(x.get('code','')).startswith(s_code)), None)
        if match:
            hist = match.get('hist', {})
            if not isinstance(hist, dict):
                hist = {}
            vrv, vrt = _p1_display_vol_ratio(hist, match)
            row["量比"] = f"{vrv:.1f}({vrt})"
            cyq_e = _safe_float(hist.get("cyq_concentration", 0.0), 0.0)
            if cyq_e > 0 and str(row.get("集中度", "")).strip() in ("--", "", "nan", "None"):
                row["集中度"] = f"{cyq_e:.1f}"
            circ_mv_raw = hist.get('circ_mv')
            if pd.isna(circ_mv_raw) or circ_mv_raw is None:
                circ_mv_raw = hist.get('total_mv', 0) * 0.6
            circ_mv_yi = float(circ_mv_raw) / 10000.0
            
            if circ_mv_yi >= 1000.0: size_emoji, size_label = "🦍", "巨无霸"
            elif circ_mv_yi >= 500.0: size_emoji, size_label = "🐘", "超级中军"
            elif circ_mv_yi >= 100.0: size_emoji, size_label = "🐎", "核心中盘"
            else: size_emoji, size_label = "🐥", "袖珍盘"

            orig_name = normalize_stock_display_name(str(row.get("名称", "")))
            if orig_name and orig_name[0] not in ["🦍", "🐘", "🐎", "🐥"]:
                row["名称"] = f"{size_emoji} {orig_name}"
                
            if curr_regime == "主升浪":
                if size_emoji == "🐎": row["建议仓位"] = "🔥主攻: 30-40%"
                elif size_emoji in ["🦍", "🐘"]: row["建议仓位"] = "🛡️压舱: 15-20%"
            elif curr_regime == "情绪退潮市":
                if size_emoji == "🐎": row["建议仓位"] = "🛑极危: 0-10% (试探)"
                elif size_emoji in ["🦍", "🐘"]: row["建议仓位"] = "🛡️重装避险: 30-40%"
            else:
                if size_emoji == "🐎": row["建议仓位"] = "⚔️游击: 15-20%"
                elif size_emoji in ["🦍", "🐘"]: row["建议仓位"] = "🛡️均衡: 20-30%"
                
            row["纪律防线"] = "破20日线" if size_emoji in ["🦍", "🐘"] else "3日未脱离成本"
            row["股性"] = f"{size_emoji}{size_label}"
        if "缩量说明" not in row:
            row["缩量说明"] = "--"

enhance_pool_data(p2_data, "p2")
enhance_pool_data(p3_data, "p3")
enhance_pool_data(p4_data, "p4")
enhance_pool_data(p5_data, "p5") 

# ✨ 核心列序优化：将现价放到综合分前面
display_cols = [
    "代码", "名称", "涨幅", "现价", "综合分", "板块趋势",
    "建议最低分", "风险标签", "操盘提示", "缩量说明",
    "战法", "量能爆能", "量比", "真换手", "行业", "股性", "建议仓位", "纪律防线",
]

# ==================== 🚀 终极上下层网格排版 ====================
st.markdown("<br>", unsafe_allow_html=True)

# 【V26.80】P1 增强面板（在 P1 主表前插入快速指标卡）
# 【V26.88+10-3 UI 清理】删除 p1_enhancement_panel 重复 Panel，详情并入主面板
try:
    from ui.p1_enhancement_panel import render_p1_quick_metrics  # noqa
    # if p1_display_data and active_items:
    #     render_p1_quick_metrics(active_items)
except Exception:
    pass

# 上层（常规作战区）：P1 底仓 / P2 竞价 / P3 盘中
col_bot1, col_bot2, col_bot3 = st.columns(3)
# [V26.88+10-8 底仓可视扩容] P1 底仓池至少显示 20 行, 防止 ≥15 只的池子被截断成 ~10 行
render_styled_pool(col_bot1, "⭐ 1档·底仓", "p1", p1_display_data, display_cols, "底仓为空，请点击上方大按钮进行洗盘。", min_visible_rows=20)
render_styled_pool(col_bot2, "🔥 2档·竞价", "p2", p2_data, display_cols)
render_styled_pool(col_bot3, "🌊 3档·盘中", "p3", p3_data, display_cols)

st.markdown("<hr style='margin: 2.0em 0; border: 0; border-top: 1px dashed #cbd5e1;'>", unsafe_allow_html=True)

# 下层（盘尾/盘后决战区）：P4 盘尾池 vs P5 真龙池
col_top1, col_top2 = st.columns(2)
render_styled_pool(col_top1, "🕒 4档·盘尾池", "p4", p4_data, display_cols, "等待 14:31–14:55 扫描...")
render_styled_pool(col_top2, "👑 5档·盘后池", "p5", p5_data, display_cols, "等待盘后数据下载并扫描...")

# 【V26.80】P1 完整增强面板（在所有渲染完毕后展开）
try:
    from ui.p1_enhancement_panel import render_p1_enhancement_panel  # noqa
    # 【V26.88+10-3 UI 清理】删除 P1 增强维度详情 Panel
    # if p1_display_data and active_items:
    #     with st.expander("🎯 V26.80 P1 增强维度详情（风格画像 / A 股特色 / 性能监控 / 拒绝原因）", expanded=False):
    #         render_p1_enhancement_panel(
    #             p1_pool=active_items,
    #             p1_rejected=st.session_state.get('p1_rejected_cache', []),
    #             show_full=True,
    #         )
except Exception as _e_p1_panel:
    logging.debug("P1 增强面板渲染跳过: %s", _e_p1_panel)

# 【V26.80】A 股特色数据看板（独立于 P1 主流程）
# 【V26.88+10-3 UI 清理】删除 A 股特色数据看板（与主面板重复）
try:
    from ui.a_share_dashboard import render_a_share_dashboard  # noqa
    # with st.expander("📊 V26.80 A 股特色数据看板（财报窗/板块轮动/数据同步）", expanded=False):
    #     render_a_share_dashboard()
except Exception as _e_a_share_dash:
    logging.debug("A 股特色看板渲染跳过: %s", _e_a_share_dash)

# ==================== 🚨 高危警报舱 (禁飞与斩仓) 托底显示 ====================
danger_buy_data = res_dict.get('danger_buy', [])
danger_sell_data = res_dict.get('danger_sell', [])

def dedup_list(dict_list, key='代码'):
    seen = set()
    new_list = []
    for d in dict_list:
        if d[key] not in seen:
            seen.add(d[key])
            new_list.append(d)
    return new_list
    
danger_buy_data = dedup_list(danger_buy_data)
danger_sell_data = dedup_list(danger_sell_data)

if danger_buy_data or danger_sell_data:
    st.markdown("<br>", unsafe_allow_html=True)
    st.markdown("""
    <style>
        div[data-testid="stExpander"] details summary {
            background-color: #fef2f2;
            color: #dc2626;
            border: 1px solid #f87171;
            border-radius: 6px;
            font-weight: bold;
        }
    </style>
    """, unsafe_allow_html=True)
    
    with st.expander(f"🚨 高危警报舱：今日禁飞与斩仓名单 (发现 {len(danger_buy_data)} 只禁买，{len(danger_sell_data)} 只破位)", expanded=True):
        col_dbuy, col_dsell = st.columns(2)
        
        with col_dbuy:
            st.markdown("##### 🛑 【绝对禁买区】(防诱多拦截)")
            if danger_buy_data:
                df_buy = pd.DataFrame(danger_buy_data)
                if hasattr(df_buy.style, 'map'):
                    styled_buy = df_buy.style.map(color_pct, subset=['涨幅'])
                else:
                    styled_buy = df_buy.style.applymap(color_pct, subset=['涨幅'])
                _tc = [
                    {"selector": "th", "props": [("text-align", "center")]},
                    {"selector": "td", "props": [("text-align", "center")]},
                ]
                try:
                    styled_buy = styled_buy.set_table_styles(_tc)
                except Exception:
                    pass
                st.dataframe(styled_buy, width="stretch", hide_index=True)
            else:
                st.info("今日扫描暂无致命诱多标的。")
                
        with col_dsell:
            st.markdown("##### 🩸 【无条件斩仓区】(底仓破位风控)")
            if danger_sell_data:
                df_sell = pd.DataFrame(danger_sell_data)
                if hasattr(df_sell.style, 'map'):
                    styled_sell = df_sell.style.map(color_pct, subset=['涨幅'])
                else:
                    styled_sell = df_sell.style.applymap(color_pct, subset=['涨幅'])
                _tc = [
                    {"selector": "th", "props": [("text-align", "center")]},
                    {"selector": "td", "props": [("text-align", "center")]},
                ]
                try:
                    styled_sell = styled_sell.set_table_styles(_tc)
                except Exception:
                    pass
                st.dataframe(styled_sell, width="stretch", hide_index=True)
            else:
                st.success("底仓阵地安全，无触及止损标的。")

st.markdown("<br>", unsafe_allow_html=True)

# ==================== 🔥 交易铁律 (必执行) ====================
st.markdown("""
<div style='background-color: #f8fafc; padding: 18px; border-radius: 8px; border-left: 6px solid #8b5cf6; margin-bottom: 20px;'>
    <h4 style='color: #4c1d95; margin-top: 0;'>🔥 交易铁律与验尸法则（交易纪律基石）</h4>
    <div style='color: #334155; font-size: 13px; line-height: 1.45; margin-bottom: 0;'>
        <div style='margin: 4px 0;'>🎯 <b>【P4 盘尾池】</b>：14:31–14:55 瞎子摸象，只要系统通过，不触及风控即可买入建立先手底仓。</div>
        <div style='margin: 4px 0;'>👑 <b>【P5 真龙池】验真</b>：19:30 盘后扫描若【P4票】平移到了【P5池】且分数暴涨，说明有超级机构真实建仓！次日死拿甚至做T加仓。</div>
        <div style='margin: 4px 0;'>🩸 <b>【P5 真龙池】现原形</b>：19:30 盘后扫描若【P4票】在【P5池】彻底消失，说明尾盘遭砸盘或主力纯诱多，<b>次日开盘5分钟内无条件清仓止损！</b></div>
    </div>
</div>
""", unsafe_allow_html=True)

# ==================== 🗑️ 阵亡将士诊断舱 & 大数据死因统计 ====================
rej_key = 'p0_rejected_cache' if curr_mode == 'P0' else 'p1_rejected_cache'
rejected_data = st.session_state.get(rej_key, [])

if rejected_data:
    with st.expander(f"🗑️ 阵亡将士诊断舱 (共拦截 {len(rejected_data)} 只标的)"):
        try:
            st.dataframe(style_dataframe_center(_rejected_rows_dataframe_for_display(rejected_data)), width="stretch", hide_index=True)
        except Exception:
            st.dataframe(_rejected_rows_dataframe_for_display(rejected_data), width="stretch", hide_index=True)

        # [V31-A] 死因层级概览：先告诉用户“怎么样的错”占多少，再看具体那只。
        # 直接展开 5 个层级的占比：展示写法与优先级一致（层级定义只有一份，在 registry 里）。
        try:
            from core.p1_death_cause_registry import level_distribution as _lvl_dist

            # level_distribution 接收的是死因文案列表，不是行字典列表
            _dist = _lvl_dist([
                r.get("淘汰死因") for r in rejected_data
                if isinstance(r, dict)
            ])
            if _dist:
                _labels = ["L0", "L1", "L2", "L3", "D"]
                _captions = []
                for _lv in _labels:
                    _n = int(_dist.get(_lv, 0) or 0)
                    _captions.append("%s %d" % (_lv, _n))
                st.caption("【死因分层】" + " | ".join(_captions))
                st.caption(
                    "⇒ 处置优先级：L0 永久排除 → L1 长期排除 → L2 等信号修复（最需追）"
                    " → L3 仅扣分 → D 系统问题（可能误杀）"
                )
        except Exception:
            pass
        
        st.markdown("<hr style='margin: 1.5em 0; border: 0; border-top: 1px solid #e2e8f0;'>", unsafe_allow_html=True)
        st.markdown("#### 📊 阵亡原因大数据透视 (自动脱敏归类)")
        
        cleaned_reasons = []
        for item in rejected_data:
            raw_reason = str(item.get("淘汰死因", "未知原因"))
            clean_r = re.sub(r'\s*\([^)]*\)\s*', '', raw_reason)
            clean_r = re.sub(r'（.*?）', '', clean_r).strip()
            cleaned_reasons.append(clean_r)
            
        reason_counts = Counter(cleaned_reasons)
        
        total_rejected = len(rejected_data)
        stat_data = []
        for reason, count in reason_counts.most_common():
            stat_data.append({
                "死因大类": reason,
                "拦截数量": count,
                "全市场淘汰占比": f"{(count / total_rejected) * 100:.1f}%"
            })
            
        stat_df = pd.DataFrame(stat_data)
        
        col_stat_table, col_stat_chart = st.columns([1.2, 1])
        
        with col_stat_table:
            try:
                st.dataframe(style_dataframe_center(stat_df), width="stretch", hide_index=True)
            except Exception:
                st.dataframe(stat_df, width="stretch", hide_index=True)
            
        with col_stat_chart:
            chart_df = pd.DataFrame({
                "拦截数量": [x["拦截数量"] for x in stat_data],
                "死因大类": [x["死因大类"] for x in stat_data],
                "警示色": ["#ef4444" if i < 5 else "#94a3b8" for i in range(len(stat_data))]
            })
            
            chart = alt.Chart(chart_df).mark_bar().encode(
                x=alt.X('死因大类:N', sort=alt.EncodingSortField(field="拦截数量", order="descending"), title=""),
                y=alt.Y('拦截数量:Q', title="拦截数量"),
                color=alt.Color('警示色:N', scale=None),
                tooltip=['死因大类', '拦截数量']
            ).properties(height=350)
            
            st.altair_chart(chart, width="stretch")

# ==================== 📊 战法胜率与权重反馈（V29.2 · 纯只读） ====================
st.markdown("<hr style='margin: 1.0em 0; border: 0; border-top: 1px solid #e2e8f0;'>", unsafe_allow_html=True)
with st.expander("📊 战法胜率与权重反馈（只读面板 · 数据来自 daemon 18:30 汇总）", expanded=False):
    try:
        from ui.strategy_effectiveness_panel import render_strategy_effectiveness_panel
        render_strategy_effectiveness_panel()
    except Exception as _se_panel_err:
        logging.warning("[ui] 战法胜率面板挂载异常（已忽略）: %s", _se_panel_err)

# 【V29.3】模拟盘已迁移为独立一级页面 `ui/pages/1_模拟盘.py`（Streamlit 原生 multipage），
# 侧栏可直接切页，无需在大屏底部 expander 里展开。这里保留一行指引，避免老用户找不到入口。
st.markdown("<hr style='margin: 1.0em 0; border: 0; border-top: 1px solid #e2e8f0;'>", unsafe_allow_html=True)
st.caption("📈 模拟盘持仓已独立成页：请从左侧导航栏切到 **模拟盘** 查看账户 / 持仓 / 平仓 / 战法排行榜。")

# ==================== 🛠️ 后勤工具箱 (沉底显示) ====================
st.markdown("<hr style='margin: 1.0em 0; border: 0; border-top: 1px solid #e2e8f0;'>", unsafe_allow_html=True)
with st.expander("🛠️ 后勤工具箱 (一键导出 & 沙盘推演)", expanded=False):
    tool_col1, tool_col2 = st.columns(2)
    with tool_col1:
        if st.button("💾 一键将五个选股池独立导出为 TXT", width="stretch"):
            try:
                date_dir = datetime.now().strftime("%Y%m%d")
                time_str = datetime.now().strftime("%Y%m%d_%H%M%S")
                export_dir = os.path.join(os.getcwd(), "xuanguchi", date_dir, time_str)
                os.makedirs(export_dir, exist_ok=True)
                
                p1_raw = active_items
                
                def write_txt(fn, codes):
                    with open(os.path.join(export_dir, fn), 'w', encoding='utf-8') as f:
                        for c in codes: f.write(f"{c}\n")
                            
                write_txt(f"{curr_mode}_底仓.txt", [str(x.get('code', '')).split('.')[0][:6] for x in p1_raw if x.get('code')])
                write_txt("P2_竞价.txt", [str(x.get('代码', '')) for x in res_dict.get('p2', []) if x.get('代码')])
                write_txt("P3_盘中.txt", [str(x.get('代码', '')) for x in res_dict.get('p3', []) if x.get('代码')])
                write_txt("P4_盘尾.txt", [str(x.get('代码', '')) for x in res_dict.get('p4', []) if x.get('代码')])
                write_txt("P5_真龙.txt", [str(x.get('代码', '')) for x in res_dict.get('p5', []) if x.get('代码')]) # 🐉 P5 导出支持
                st.toast(f"✅ 导出成功！保存至: xuanguchi/{date_dir}/{time_str}/")
            except Exception as e: 
                st.error(f"导出失败: {e}")

    with tool_col2:
        if st.button("▶️ 立即运行沙盘推演 (仅测试前30只底仓)", width="stretch"):
            if active_items:
                top_30_codes = [x['code'] for x in active_items[:30]]
                with st.spinner("沙盘推演中..."):
                    try:
                        backtest_df = run_batch_backtest(top_30_codes, strategy_key="👑终极共振")
                        # 【BUG 修复 2026-10-17】空结果不再直接渲染空表：
                        # 回测无命中时 DataFrame 可能为 None / 0 行 0 列，
                        # 直接 st.dataframe 会显示空白表格，用户误判为系统报错。
                        _bt_empty = (
                            backtest_df is None
                            or not hasattr(backtest_df, "empty")
                            or bool(getattr(backtest_df, "empty", True))
                        )
                        if _bt_empty:
                            st.info(
                                "ℹ️ 所选区间（👑终极共振 × 前 30 只底仓）无触发信号，"
                                "这是正常的空结果，不是故障。可调整战法参数或换用更长的回测区间再试。"
                            )
                        else:
                            try:
                                st.dataframe(style_dataframe_center(backtest_df), width="stretch", hide_index=True)
                            except Exception:
                                st.dataframe(backtest_df, width="stretch", hide_index=True)
                    except Exception as e: 
                        st.error(f"推演异常: {e}")
            else: 
                st.warning("⚠️ 底仓为空，请先洗盘。")

# ==================== 📘 洗盘结果日报（沉底显示） ====================
st.markdown("<hr style='margin: 1.0em 0; border: 0; border-top: 1px solid #e2e8f0;'>", unsafe_allow_html=True)
render_wash_daily_card(curr_mode)

