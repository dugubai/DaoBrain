"""验收脚本不能在优化掉断言后虚报成功；不连接HTTP或真实模型。"""
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("script,marker", [
    ("scripts/verify_cpu_model.py", "CPU-MODEL-E2E-OK"),
    ("tests/e2e_chain.py", "E2E-CHAIN-OK"),
])
def test_optimized_acceptance_is_rejected_before_network(script, marker):
    result = subprocess.run([sys.executable, "-O", str(ROOT / script)],
                            cwd=ROOT, capture_output=True, text=True, timeout=20)
    assert result.returncode != 0
    assert "禁止" in result.stderr and "PYTHONOPTIMIZE" in result.stderr
    assert marker not in result.stdout
