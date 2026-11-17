# -*- coding: utf-8 -*-
"""
小杰AI选股系统 Pro V26.88 - UI 组件库（终极宽屏实战与路径贯通版）
【细节修复】：
1. 指数 DuckDB 兜底：`data.db_core.get_index_latest_from_daily_data`（腾讯失败时用）。
2. 去除涨幅列多余的正号 (+)，保持视觉极致清爽，跌幅 (-) 完美保留。
3. 全面兼容 Streamlit 新版 width 参数（width="stretch" 替代 use_container_width=True）。
4. 植入全局搜索过滤逻辑，支持通过股票代码或名称秒级定位。
5. 植入 A股实战级 Pandas Styler（涨红跌绿，烈马高亮）。
"""
import logging
import math
import time
import urllib.request

import pandas as pd
import streamlit as st

from core.stock_name_utils import normalize_stock_display_name

try:
    from data.db_core import get_index_latest_from_daily_data
except Exception as e:
    logging.warning("指数 DuckDB 兜底导入失败，顶部看板将依赖腾讯或空白: %s", e)

    def get_index_latest_from_daily_data(_ts_code):  # type: ignore
        return None

def inject_custom_css():
    st.markdown("""
    <style>
        .stApp {background-color: #f8fafc; color: #1e293b;} 
        .metric-card {border-radius: 8px; padding: 12px; color: white; text-align: center; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.1); min-height: 85px; display: flex; flex-direction: column; justify-content: center;} 
        .metric-title { font-size: 0.9em; opacity: 0.9; margin-bottom: 6px; font-weight: 600; text-transform: uppercase;}
        .metric-value { font-size: 1.5em; font-weight: 700; margin-bottom: 2px; }
        
        .stButton > button { padding: 0.4rem 0.2rem; }
        .stButton > button p { 
            font-size: 14px !important; 
            font-weight: 600 !important; 
            white-space: nowrap !important; 
            overflow: hidden !important; 
            text-overflow: ellipsis !important;
        }
    </style>
    """, unsafe_allow_html=True)

def _fetch_tencent_indices_sync():
    """
    腾讯行情三大指数（与常见看盘软件接近的即时价/昨收涨跌幅）。
    解析失败返回 None。
    """
    url = "http://qt.gtimg.cn/q=sh000001,sz399006,sh000300"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            text = resp.read().decode("gbk", errors="replace")
    except Exception as e:
        logging.warning("腾讯指数行情请求失败: %s", e)
        return None
    lines = [ln for ln in text.strip().split("\n") if ln.strip() and '="' in ln]
    keys = ("000001.SH", "399006.SZ", "000300.SH")
    out = {}
    for i, line in enumerate(lines[:3]):
        try:
            body = line.split('="', 1)[1].strip().strip('";')
            parts = body.split("~")
            if len(parts) < 5:
                continue
            price = float(parts[3])
            pre = float(parts[4])
            if price <= 0 or pre <= 0 or not math.isfinite(price) or not math.isfinite(pre):
                continue
            pct = (price - pre) / pre * 100.0
            out[keys[i]] = {"close": price, "pct_chg": pct}
        except Exception:
            continue
    return out if len(out) == 3 else None


def _series_or_dict_to_idx_dict(row):
    """统一为 format_idx 可用的 dict。"""
    if row is None:
        return None
    if isinstance(row, dict):
        return row
    try:
        d = row.to_dict() if hasattr(row, "to_dict") else dict(row)
        return d
    except Exception:
        return None


def fetch_dashboard_indices_safe():
    now = time.time()
    if "dash_idx_cache" in st.session_state:
        cache_time, data = st.session_state["dash_idx_cache"]
        if now - cache_time < 45:
            return data

    data = _fetch_tencent_indices_sync()
    if data is not None:
        data = (data.get("000001.SH"), data.get("399006.SZ"), data.get("000300.SH"))
    else:
        try:
            sz = _series_or_dict_to_idx_dict(get_index_latest_from_daily_data("000001.SH"))
            cy = _series_or_dict_to_idx_dict(get_index_latest_from_daily_data("399006.SZ"))
            hs300 = _series_or_dict_to_idx_dict(get_index_latest_from_daily_data("000300.SH"))
            data = (sz, cy, hs300)
        except Exception as e:
            logging.warning("fetch_dashboard_indices_safe DuckDB 指数兜底失败: %s", e)
            data = (None, None, None)

    st.session_state["dash_idx_cache"] = (now, data)
    return data

def render_top_dashboard(p1_count, p1_fallback_date=None):
    safe_p1_count = str(p1_count) if p1_count is not None else "0"
    # [V26.88+9-29 ST-P0-10] 当日 P1 缓存空、走 fallback 时显示旧日标记，避免误读为「今日 0 只」
    _fb_note = ""
    if p1_fallback_date:
        _fb_date_human = f"{str(p1_fallback_date)[:4]}-{str(p1_fallback_date)[4:6]}-{str(p1_fallback_date)[6:]}"
        _fb_note = f" <span style='font-size:0.55em; color:#f59e0b; font-weight:600;'>↩︎ {_fb_date_human} 缓存</span>"
    sz, cy, hs300 = fetch_dashboard_indices_safe()
    
    def format_idx(idx_data):
        if idx_data is None: return "---", ""
        try:
            pct = float(idx_data.get('pct_chg', 0))
            close_val = float(idx_data.get('close', 0))
            color = "color: #ef4444;" if pct > 0 else "color: #10b981;"
            return f"{close_val:.2f}", f"<span style='{color}'>{pct:+.2f}%</span>"
        except Exception as e:
            logging.debug(f"format_idx 解析指数数据异常: {e}")
            return "---", ""
            
    sz_val, sz_pct = format_idx(sz)
    cy_val, cy_pct = format_idx(cy)
    hs_val, hs_pct = format_idx(hs300)

    col1, col2, col3, col4 = st.columns(4)
    with col1: st.markdown(f"<div class='metric-card' style='background:white; border-left:4px solid #e2e8f0; color:#1e293b;'><div class='metric-title' style='color:#64748b;'>上证指数</div><div class='metric-value'>{sz_val} <span style='font-size:0.6em'>{sz_pct}</span></div></div>", unsafe_allow_html=True)
    with col2: st.markdown(f"<div class='metric-card' style='background:white; border-left:4px solid #e2e8f0; color:#1e293b;'><div class='metric-title' style='color:#64748b;'>创业板指</div><div class='metric-value'>{cy_val} <span style='font-size:0.6em'>{cy_pct}</span></div></div>", unsafe_allow_html=True)
    with col3: st.markdown(f"<div class='metric-card' style='background:white; border-left:4px solid #e2e8f0; color:#1e293b;'><div class='metric-title' style='color:#64748b;'>沪深 300</div><div class='metric-value'>{hs_val} <span style='font-size:0.6em'>{hs_pct}</span></div></div>", unsafe_allow_html=True)
    with col4: st.markdown(f"<div class='metric-card' style='background:white; border-left:4px solid #3b82f6; color:#1e293b;'><div class='metric-title' style='color:#64748b;'>底仓锁定数量 (P1)</div><div class='metric-value' style='color:#3b82f6;'>{safe_p1_count} 只{_fb_note}</div></div>", unsafe_allow_html=True)

def _normalize_name_for_display(name):
    return normalize_stock_display_name(name)

# 🎨 修复1：抛弃依赖 '+' 号的字符串判断，采用直接纯数字大小比较
def _color_pct(val):
    try:
        v = float(val)
        if v > 0: return 'color: #ef4444; font-weight: bold;'
        if v < 0: return 'color: #10b981; font-weight: bold;'
    except (TypeError, ValueError) as e:
        logging.debug(f"_color_pct 数值解析失败: {e}")
    return ''

def _color_nature(val):
    val_str = str(val)
    if '烈马' in val_str: return 'color: #ef4444; font-weight: bold;'
    if '慢牛' in val_str or '基石' in val_str: return 'color: #3b82f6; font-weight: bold;'
    return ''

def show_pool(col, title, key, data, display_cols=None, search_query="", empty_msg="等待雷达扫描..."):
    with col:
        if not data:
            st.markdown(f"#### {title} (0)")
            st.info(empty_msg)
            return

        filtered_list = []
        for row in data:
            c_code = str(row.get("代码", ""))
            c_name = str(row.get("名称", ""))
            if search_query and (search_query not in c_code and search_query not in c_name):
                continue
            filtered_list.append(row)
        
        st.markdown(f"#### {title} ({len(filtered_list)})")
        if not filtered_list:
            st.warning("⚠️ 未找到匹配标的")
            return

        if display_cols:
            clean_list = [{k: row.get(k, "") for k in display_cols if k in row} for row in filtered_list]
            df = pd.DataFrame(clean_list)
            cols_order = [c for c in display_cols if c in df.columns]
        else:
            df = pd.DataFrame(filtered_list)
            base_cols = ['代码', '名称', '现价', '综合分', '战法', '涨幅', '连板', '机构', '量比', '真换手', '集中度']
            cols_order = [c for c in base_cols if c in df.columns]
            for c in df.columns:
                if c not in cols_order and c != '建议仓位': cols_order.append(c)

        num_cols = ['涨幅', '现价', '量比', '真换手', '集中度', '综合分', '连板', '机构']
        for c in df.columns:
            if c in num_cols:
                df[c] = pd.to_numeric(df[c].astype(str).replace(['%', '\\+', '＋', ','], '', regex=True), errors='coerce')

        def highlight_fn(df_to_style):
            styles = pd.DataFrame('', index=df_to_style.index, columns=df_to_style.columns)
            for i in range(len(df_to_style)):
                row = df_to_style.iloc[i]
                try:
                    score = float(row.get('综合分', 0))
                except (TypeError, ValueError):
                    score = 0
                try:
                    pct = float(row.get('涨幅', 0))
                except (TypeError, ValueError):
                    pct = 0
                nature_str = str(row.get('股性', ''))
                
                if score >= 135: row_style = 'color: #7e22ce; font-weight: 900; font-size: 1.05em;' 
                elif score >= 120: row_style = 'color: #dc2626; font-weight: bold;' 
                elif score >= 110: row_style = 'color: #2563eb; font-weight: bold;' 
                else: row_style = 'color: #d97706; font-weight: 500;' 
                styles.iloc[i, :] = row_style
                
                if pct < 0:
                    green_style = 'color: #10b981; font-weight: 600;' 
                    for col_name in ['代码', '名称', '涨幅', '现价']:
                        if col_name in styles.columns: styles.loc[df_to_style.index[i], col_name] = green_style

                if '股性' in styles.columns:
                    if '烈马' in nature_str: styles.loc[df_to_style.index[i], '股性'] = 'color: #ef4444; font-weight: bold;'
                    elif '慢牛' in nature_str or '基石' in nature_str: styles.loc[df_to_style.index[i], '股性'] = 'color: #3b82f6; font-weight: bold;'
            return styles

        col_cfg = {
            "综合分": st.column_config.NumberColumn(format="%.2f"), 
            "涨幅": st.column_config.NumberColumn(format="%.2f%%"), 
            "真换手": st.column_config.NumberColumn(format="%.1f%%"), 
            "集中度": st.column_config.NumberColumn(format="%.1f"), 
            "现价": st.column_config.NumberColumn(format="%.2f"), 
            "量比": st.column_config.NumberColumn(format="%.1f"),
            "连板": st.column_config.NumberColumn(format="%d"),
            "机构": st.column_config.NumberColumn(format="%d")
        }
        
        _tc = [
            {"selector": "th", "props": [("text-align", "center")]},
            {"selector": "td", "props": [("text-align", "center")]},
        ]
        st.dataframe(
            df[cols_order]
            .style.set_table_styles(_tc)
            .apply(highlight_fn, axis=None)
            .map(_color_pct, subset=["涨幅"] if "涨幅" in cols_order else []),
            width="stretch",
            hide_index=True,
            column_config=col_cfg,
        )

def push_snapshot_to_mobile(scan_results, limit_up_count=0): 
    pass
