"""无Docker的全模块模拟联调；不代表Compose/数据库/GPU验收。"""
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
MODULES = {
    "gateway": "GATEWAY_URL", "agent_orchestrator": "ORCHESTRATOR_URL",
    "rag_public": "RAG_PUBLIC_URL", "external_model": "EXTERNAL_MODEL_URL",
    "output_verify": "OUTPUT_VERIFY_URL", "hybrid_iface": "HYBRID_IFACE_URL",
    "local_model": "LOCAL_MODEL_URL", "rag_green": "RAG_GREEN_URL",
    "rag_red": "RAG_RED_URL", "redline_gate": "REDLINE_GATE_URL",
    "daobrain_tuned": "DAOBRAIN_TUNED_URL", "corpus_kb": "CORPUS_KB_URL",
    "card_annotator": "CARD_ANNOTATOR_URL", "ocr": "OCR_URL",
}


def run():
    env = os.environ.copy()
    env.update(API_KEY=secrets.token_urlsafe(32), INTERNAL_API_KEY=secrets.token_urlsafe(32),
               APPROVAL_SECRET=secrets.token_urlsafe(32), EXTERNAL_MODEL_MODE="stub",
               ALLOW_EXTERNAL_STUB="true", LOCAL_MODEL_BACKEND="stub", PYTHONUNBUFFERED="1")
    # 先保留所有端口，再逐个释放供相应子进程绑定。
    sockets = {}
    for module, setting in MODULES.items():
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        sockets[module] = sock
        env[setting] = f"http://127.0.0.1:{sock.getsockname()[1]}"
    processes = []
    logpath = Path(tempfile.gettempdir()) / ("daobrain-chain-" + secrets.token_hex(8) + ".log")
    try:
        with logpath.open("x", encoding="utf-8") as log:
            for module, setting in MODULES.items():
                port = sockets[module].getsockname()[1]
                sockets[module].close()
                command = [sys.executable, "-m", "uvicorn", module + ".main:app",
                           "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"]
                proc = subprocess.Popen(command, cwd=ROOT, env=env, stdout=log, stderr=log)
                processes.append((module, proc))
                deadline = time.monotonic() + 15
                ready = False
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                while time.monotonic() < deadline:
                    if proc.poll() is not None:
                        raise RuntimeError(f"{module} 启动退出：{proc.returncode}")
                    try:
                        with opener.open(env[setting] + "/health", timeout=0.5) as response:
                            health = json.loads(response.read())
                        if health.get("module") == module and health.get("status") == "ok":
                            ready = True
                            break
                    except (OSError, ValueError):
                        time.sleep(0.1)
                if not ready:
                    raise RuntimeError(module + " 未在限时内就绪")
                print(module + " 就绪", flush=True)
            env["GW"] = env["GATEWAY_URL"]
            env["PYTHON"] = sys.executable
            subprocess.run([sys.executable, str(ROOT / "tests" / "e2e_chain.py")],
                           cwd=ROOT, env=env, check=True, timeout=90)
        print("全模块本地模拟联调通过（不含Docker/GPU/真实OCR）")
    except Exception:
        if logpath.exists():
            print(logpath.read_text(encoding="utf-8")[-6000:], file=sys.stderr)
        raise
    finally:
        for sock in sockets.values():
            sock.close()
        for _, proc in processes:
            if proc.poll() is None:
                proc.terminate()
        for _, proc in processes:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)


if __name__ == "__main__":
    run()
