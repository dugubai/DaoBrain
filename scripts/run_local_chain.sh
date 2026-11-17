#!/usr/bin/env bash
# 由Python监管启动/健康/退出，仅管理本次启动的子进程；端口自动分配。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="${PYTHON:-$ROOT/.venv/bin/python}"
exec "$PY" "$ROOT/scripts/run_local_chain.py"
