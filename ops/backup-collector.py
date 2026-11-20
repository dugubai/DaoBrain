"""备机 host 侧运维采集器：systemd 循环每 5s 运行，汇聚 GPU / ollama / 关键容器负载 + 经主机→备机隧道 ssh 拉回的主机 entry.json，
原子写 /var/lib/daobrain-ops/ops.json，供 dashboard 容器（只读挂载该目录）的 /api/ops 读取。

只在备机本机 root 下运行；对主机仅执行一条只读的 `cat entry.json`。任何子项失败都降级为 null，不影响其它块。
"""
import json
import os
import subprocess
import tempfile
import time
import pathlib
import urllib.request

OUT = pathlib.Path("/var/lib/daobrain-ops/ops.json")
SSH_KEY = os.environ.get("DAOBRAIN_ENTRY_KEY", "/root/.ssh/daobrain_tunnel")
ENTRY_HOST = os.environ.get("DAOBRAIN_ENTRY_HOST", "admin@39.102.49.1")
ENTRY_JSON = "/var/lib/daobrain-ops/entry.json"
KEY_CONTAINERS = ["daobrain-gateway-1", "daobrain-hybrid_iface-1",
                  "daobrain-local_model-1", "daobrain-ollama-1",
                  "daobrain-rag_green-1", "daobrain-postgres-1",
                  "daobrain-output_verify-1", "daobrain-gw-front"]
OLLAMA_URL = os.environ.get("OLLAMA_SCRAPE_URL", "http://127.0.0.1:11434/api/ps")


def run(cmd, timeout=6):
    try:
        p = subprocess.run(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except Exception as e:
        return 1, "", str(e)[:150]


def fetch_entry():
    cmd = (("ssh -i %s -o BatchMode=yes -o StrictHostKeyChecking=accept-new "
            "-o ControlMaster=auto -o ControlPersist=120 "
            "-o ControlPath=/tmp/.daobrain-entry-cm-%%r@%%h:%%p "
            "-o ConnectTimeout=5 %s 'sudo cat %s 2>/dev/null'")
           % (SSH_KEY, ENTRY_HOST, ENTRY_JSON))
    rc, out, err = run(cmd, timeout=9)
    if rc != 0 or not out.strip():
        return None, ("rc=%s %s" % (rc, err.strip()[:120])) if err else "empty"
    try:
        return json.loads(out), None
    except Exception as e:
        return None, "parse: " + str(e)[:120]


def gpu():
    rc, out, _ = run(
        "nvidia-smi --query-gpu=name,utilization.gpu,memory.used,memory.total "
        "--format=csv,noheader,nounits 2>/dev/null", timeout=6)
    if rc != 0 or not out.strip():
        return None
    line = out.strip().splitlines()[0]
    parts = [x.strip() for x in line.split(",")]
    try:
        return {"name": parts[0], "util_gpu_pct": int(parts[1]),
                "mem_used_mib": int(parts[2]), "mem_total_mib": int(parts[3])}
    except Exception:
        return {"raw": line}


def ollama_ps():
    try:
        with urllib.request.urlopen(OLLAMA_URL, timeout=5) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
        models = [{"name": m.get("name"), "size": m.get("size"),
                   "processor": m.get("processor"), "until": m.get("expires_at")}
                  for m in d.get("models", [])]
        return {"reachable": True, "models": models}
    except Exception as e:
        return {"reachable": False, "error": type(e).__name__}


def containers():
    names = " ".join(KEY_CONTAINERS)
    rc, out, _ = run("docker stats --no-stream --format '{{json .}}' %s 2>/dev/null" % names,
                     timeout=8)
    res = []
    for L in out.splitlines():
        try:
            s = json.loads(L)
        except Exception:
            continue
        res.append({"name": s.get("Name"), "cpu": s.get("CPUPerc"),
                    "mem": s.get("MemUsage"), "mem_pct": s.get("MemPerc"),
                    "net": s.get("NetIO"), "pids": s.get("Pids")})
    return res or None


def host():
    rc, out, _ = run("cat /proc/loadavg; free -m | awk 'NR==2{print $2, $7}'; "
                     "uptime -p 2>/dev/null", timeout=4)
    lines = out.splitlines()
    h = {}
    if lines:
        p = lines[0].split()
        if len(p) >= 3:
            h["load"] = {"l1": p[0], "l5": p[1], "l15": p[2]}
    if len(lines) > 1:
        m = lines[1].split()
        if len(m) >= 2:
            h["mem_total_mb"] = int(m[0]) if m[0].isdigit() else m[0]
            h["mem_available_mb"] = int(m[1]) if m[1].isdigit() else m[1]
    if len(lines) > 2:
        h["uptime"] = lines[2].strip()
    return h or None


def main():
    entry, entry_err = fetch_entry()
    data = {
        "_generated_epoch": int(time.time()),
        "_generated_at_str": time.strftime("%Y-%m-%d %H:%M:%S %z"),
        "gpu": gpu(),
        "ollama": ollama_ps(),
        "containers": containers(),
        "host": host(),
        "entry": entry,
        "entry_fetch_error": entry_err,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(OUT.parent), prefix=".ops.", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, OUT)
        os.chmod(OUT, 0o644)
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        raise


if __name__ == "__main__":
    main()
