#!/usr/bin/env bash
set -euo pipefail

# 小杰AI选股系统 Pro V26.88 — 生产启动（Linux）
# 1) auto_sniper_daemon.py：晚间/早盘同步、P1、P3~P5、分时快照、scan_async 队列
# 2) streamlit ui/app.py：指挥舱
# 使用前：cd 到项目根，pip3 install -r requirements.txt -r requirements-daemon.txt（系统 Python，无需 venv）

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

mkdir -p data/runtime

# V26.80 Enhancement Switch (default OFF; export XIAOJIE_V2680_ENABLED=1 to enable)
export XIAOJIE_V2680_ENABLED="${XIAOJIE_V2680_ENABLED:-0}"

# V26.80 A 股特色维度开关
export XIAOJIE_A_SHARE_FEATURES_ENABLED="${XIAOJIE_A_SHARE_FEATURES_ENABLED:-0}"

nohup python auto_sniper_daemon.py >> data/runtime/daemon.log 2>&1 &
echo $! > data/runtime/daemon.pid

exec streamlit run ui/app.py
