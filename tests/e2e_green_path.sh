#!/usr/bin/env bash
# 调用真实HTTP契约检查；密钥从运行环境读取，禁止写进源码或打印。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="${PYTHON:-python3}"
exec "$PY" "$ROOT/tests/e2e_chain.py"
