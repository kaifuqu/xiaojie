# -*- coding: utf-8 -*-
"""tools/_test_go_live_fixes.py — 上线审计修复回归（2026-11-14）

覆盖：
  A. P0-3 日志脱敏：scrub_secrets + Handler Filter（key/token/password 打码，普通文本不误伤）
  B. P0-2 网络：net_health 端口解析 / _no_proxy_env 强制绕过注册表代理 / 告警本地账本落盘
  C. P1-7/8 模拟盘：缺口净值回填（内存库，交易日仅取 daily_data 真实日期 + 幂等 + 链式 PnL）
  D. P1-6 交易日历：daemon 交易判断链路包含本地日历兜底（源码级断言）

红线：全程离线、不触碰生产库/生产日志（env 重定向到临时文件）。
"""
from __future__ import annotations

import io
import logging
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="xj_test_golive_")
os.environ["XJ_SNIPER_LOG_PATH"] = os.path.join(_TMP, "sniper.log")
os.environ["XJ_NO_PUSH"] = "1"

PASS = 0


def ok(name):
    global PASS
    PASS += 1
    print(f"[PASS] {name}")


# ---------------- A. 日志脱敏 ----------------
from core.log_config import _SecretScrubFilter, scrub_secrets

s = scrub_secrets("send?key=abc123def-456-789 token=SECRET999 password: pw4444等 普通词 token 正常")
assert "abc123def" not in s and "SECRET999" not in s and "pw4444" not in s, s
assert "普通词 token 正常" in s, s
ok("A1 scrub_secrets 打码 key/token/password 且不误伤普通文本")

buf = io.StringIO()
h = logging.StreamHandler(buf)
h.addFilter(_SecretScrubFilter())
lg = logging.getLogger("xj_test_mask")
lg.addHandler(h)
lg.setLevel(logging.INFO)
lg.info("webhook url=...send?key=0123456789abcdefXYZ boom")
assert "0123456789abcdefXYZ" not in buf.getvalue(), buf.getvalue()
ok("A2 Handler Filter 集成打码")

# ---------------- B. 网络 ----------------
from core.net_health import _parse_loopback_port, check_once

assert _parse_loopback_port("127.0.0.1:7897") == ("127.0.0.1", 7897)
assert _parse_loopback_port("http=127.0.0.1:7897;https=127.0.0.1:7897") == ("127.0.0.1", 7897)
assert _parse_loopback_port("10.0.0.8:3128") is None
ok("B1 代理地址解析")

r = check_once(write_state=False)
assert r.get("verdict") in {"ok", "direct_only", "degraded", "down", "unknown"}, r
ok(f"B2 网络体检可执行（verdict={r.get('verdict')}）")

from data.fetcher._client import _no_proxy_env

os.environ.pop("NO_PROXY", None)
with _no_proxy_env():
    assert os.environ.get("NO_PROXY") == "*" and os.environ.get("no_proxy") == "*"
assert "NO_PROXY" not in os.environ and "no_proxy" not in os.environ
ok("B3 _no_proxy_env 强制 NO_PROXY=* 且退出恢复")

ledger = os.path.join(_TMP, "alert_undelivered.json")
os.environ["XJ_ALERT_LEDGER_PATH"] = ledger
from core.notification.gateway import _append_undelivered_alert, _mask_webhook_url

assert "key=***" in _mask_webhook_url("http://x/send?key=abc123456789")
_append_undelivered_alert("测试告警内容", RuntimeError("proxy dead"))
import json as _json

with open(ledger, "r", encoding="utf-8") as f:
    _d = _json.load(f)
assert _d["items"] and _d["items"][-1]["content"].startswith("测试告警"), _d
ok("B4 告警本地账本落盘（断网不再静默）")

# ---------------- C. 净值缺口回填（内存库） ----------------
import duckdb

from core.paper_trading import schema as p_schema
from core.paper_trading.engine import _backfill_equity_gap_impl, _close_on_or_before
from core.paper_trading.config import load_config

con = duckdb.connect(":memory:")
p_schema.ensure_paper_tables(con, migrate=True)
con.execute(
    "CREATE TABLE daily_data (ts_code VARCHAR, trade_date VARCHAR, close DOUBLE)"
)
# 真实存在的两个缺口交易日（11-09 / 11-10）；11-11 也插入但账户 last_equity 之后
con.execute("INSERT INTO daily_data VALUES "
            "('600001.SH','2026-11-09',10.0),('600001.SH','2026-11-10',11.0)")
con.execute(
    "INSERT INTO paper_account (account_id, initial_capital, cash, status, start_date, "
    "start_cash, trading_day_index, last_equity_date, created_at, updated_at) "
    "VALUES ('PAPER_MAIN', 1000000, 500000, 'RUNNING', '20261104', 1000000, 3, '20261106', "
    "current_timestamp, current_timestamp)"
)
con.execute(
    "INSERT INTO paper_position (account_id, code, name, pool, shares, buy_price_exec, "
    "created_at, updated_at) VALUES ('PAPER_MAIN', '600001.SH', '测试股', 'p3', 50000, 9.0, "
    "current_timestamp, current_timestamp)"
)
con.execute(
    "INSERT INTO paper_equity_daily (account_id, trade_date, cash, market_value, total_asset, "
    "daily_pnl, daily_pnl_pct, cum_pnl, cum_return, drawdown_pct, position_count, trade_count, "
    "total_fee) VALUES ('PAPER_MAIN','20261106',500000,450000,950000,0,0,0,0,0,1,0,0)"
)
# 缺口成交流水为空 → 允许回填
cfg = load_config()
out = _backfill_equity_gap_impl(con, cfg=cfg)
assert out["ok"] is True, out
assert out["dates"] == ["20261109", "20261110"], out
# 11-09: mv = 10*50000 = 50万, total = 100万
# 11-10: mv = 11*50000 = 55万, total = 105万
rows = con.execute(
    "SELECT trade_date, total_asset, daily_pnl, position_count "
    "FROM paper_equity_daily WHERE trade_date IN ('20261109','20261110') ORDER BY 1"
).fetchall()
assert rows == [("20261109", 1000000.0, 50000.0, 1),
                ("20261110", 1050000.0, 50000.0, 1)], rows
ok("C1 缺口回填 2 天，按当日收盘计价、链式 PnL 正确")

out2 = _backfill_equity_gap_impl(con, cfg=cfg)
assert out2["ok"] is True and out2["filled"] == 0, out2
ok("C2 回填幂等（二跑零写入）")

# 缺口内有成交 → 必须拒绝回填
con.execute(
    "INSERT INTO paper_order (order_id, client_order_id, account_id, side, order_ts, "
    "trade_date, code, shares) VALUES "
    "('o_gap','c_gap','PAPER_MAIN','BUY','2026-11-10 10:00:00','20261110','600001.SH',100)"
)
con.execute("DELETE FROM paper_equity_daily WHERE trade_date IN ('20261109','20261110')")
out3 = _backfill_equity_gap_impl(con, cfg=cfg)
assert out3["ok"] is False and out3["filled"] == 0, out3
ok("C3 缺口内存在成交时拒绝回填（防编造现金口径）")

assert _close_on_or_before(con, "600001.SH", "20261110") == 11.0
assert _close_on_or_before(con, "600001.SH", "20261109") == 10.0
ok("C4 收盘价查询（<= 日回溯）")
con.close()

# ---------------- D. 交易日历本地兜底接线 ----------------
import inspect

import auto_sniper_daemon as d

src = inspect.getsource(d.is_today_trading_day_sse)
assert "is_trade_date_local" in src and "本地日历" in src, src[:400]
ok("D1 daemon 交易判断包含 dim_trade_calendar 本地兜底")

print(f"\n[go_live_fixes] ALL {PASS} PASSED")
