"""DaoBrain 拓扑采集器（备机内部服务）。

经 /var/run/docker.sock 用 Docker Engine API(stdlib,无第三方依赖)枚举本机 daobrain-* 容器，
读取每个容器的实时 CPU/内存/网络/进程/重启次数,连同引擎信息缓存于内存,由后台线程周期性刷新,
对外只暴露 0.0.0.0:8021/json(仅供同 docker 网络内的 dashboard 服务访问,不发布到主机/公网)。
只读容器元数据,不执行任何容器操作。
"""
import json
import os
import socket
import threading
import time

try:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
except ImportError:  # py3.6
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from socketserver import ThreadingMixIn

    class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
        daemon_threads = True

try:
    from concurrent.futures import ThreadPoolExecutor
except ImportError:
    ThreadPoolExecutor = None

SOCK = os.environ.get("DOCKER_SOCKET", "/var/run/docker.sock")
PORT = int(os.environ.get("TOPO_PORT", "8021"))
INTERVAL = float(os.environ.get("TOPO_INTERVAL", "6"))
PREFIX = "daobrain-"

_lock = threading.Lock()
_state = {"updated": 0, "nodes": [], "engine": {}, "error": "", "cycle_seconds": None}
_prev_net = {}  # short_id -> (rx_bytes, tx_bytes, ts)


def _api_get(path, timeout=6):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.connect(SOCK)
    s.sendall(("GET " + path + " HTTP/1.1\r\nHost: docker\r\nAccept: */*\r\nConnection: close\r\n\r\n").encode())
    chunks = []
    while True:
        b = s.recv(65536)
        if not b:
            break
        chunks.append(b)
    s.close()
    raw = b"".join(chunks)
    head, _, body = raw.partition(b"\r\n\r\n")
    # 处理 chunked 传输编码
    if b"transfer-encoding: chunked" in head.lower():
        body = _dechunk(body)
    return body


def _dechunk(data):
    out = bytearray()
    i = 0
    while i < len(data):
        j = data.find(b"\r\n", i)
        if j < 0:
            break
        try:
            size = int(data[i:j], 16)
        except ValueError:
            break
        if size == 0:
            break
        start = j + 2
        out += data[start:start + size]
        i = start + size + 2
    return bytes(out)


def _jget(path):
    try:
        return json.loads(_api_get(path).decode("utf-8", "replace"))
    except Exception:
        return None


def _stat_parse(cid, st):
    out = {}
    try:
        mem = st["memory_stats"]
        out["mem_used_mb"] = round(mem.get("usage", 0) / 1e6, 1)
        out["mem_limit_mb"] = round(mem.get("limit", 0) / 1e6, 1)
        out["mem_percent"] = round(mem.get("percent", 0) or 0, 1)
    except Exception:
        pass
    try:
        cpu = st["cpu_stats"]
        pre = st.get("precpu_stats", {}) or {}
        cu = cpu["cpu_usage"]["total_usage"]
        pcu = pre.get("cpu_usage", {}).get("total_usage", 0)
        sysu = cpu.get("system_cpu_usage", 0)
        psys = pre.get("system_cpu_usage", 0)
        cpus = len(cpu.get("cpu_usage", {}).get("percpu_usage") or [1])
        dcu = cu - pcu
        dsys = sysu - psys
        out["cpu_percent"] = round((dcu / dsys) * cpus * 100, 1) if (dsys > 0 and dcu >= 0) else 0.0
    except Exception:
        out["cpu_percent"] = 0.0
    try:
        nets = st.get("networks", {}) or {}
        rx = sum(v.get("rx_bytes", 0) for v in nets.values())
        tx = sum(v.get("tx_bytes", 0) for v in nets.values())
        out["net_rx_mb"] = round(rx / 1e6, 1)
        out["net_tx_mb"] = round(tx / 1e6, 1)
        now = time.time()
        prev = _prev_net.get(cid)
        if prev:
            dt = max(now - prev[2], 0.001)
            drx = rx - prev[0]
            dtx = tx - prev[1]
            out["net_mbps"] = round(max(drx, 0) / 1e6 / dt + max(dtx, 0) / 1e6 / dt, 3)
        else:
            out["net_mbps"] = 0.0
        _prev_net[cid] = (rx, tx, now)
    except Exception:
        pass
    try:
        out["pids"] = (st.get("pids_stats") or {}).get("current", 0)
    except Exception:
        pass
    return out


def _node(cid, c):
    name = (c.get("Names") or ["?"])[0].lstrip("/")
    node = {
        "id": cid,
        "name": name,
        "service": name[len(PREFIX):-2] if name.endswith("-1") else name[len(PREFIX):],
        "state": c.get("State"),
        "status": c.get("Status"),
        "health": (c.get("Health") or {}).get("Status", "none"),
        "restarts": c.get("RestartCount") or 0,
        "image": (c.get("Image") or "").split("/")[-1],
    }
    st = _jget("/containers/" + cid + "/stats?stream=false")
    if st:
        node.update(_stat_parse(cid, st))
    return node


def _collect():
    conts = _jget("/containers/json?all=1&limit=0") or []
    mine = [c for c in conts if ((c.get("Names") or [""])[0].lstrip("/")).startswith(PREFIX)]
    ids = [(c.get("Id", "")[:12], c) for c in mine]
    if ThreadPoolExecutor:
        with ThreadPoolExecutor(max_workers=8) as ex:
            nodes = list(ex.map(lambda t: _node(t[0], t[1]), ids))
    else:
        nodes = [_node(i, c) for i, c in ids]
    eng = _jget("/info") or {}
    engine = {
        "nodename": eng.get("Name"),
        "containers_running": eng.get("ContainersRunning"),
        "containers_total": eng.get("Containers"),
        "images": eng.get("Images"),
        "cpus": eng.get("NCPU"),
        "mem_total_gb": round((eng.get("MemTotal") or 0) / 1e9, 1),
        "kernel": eng.get("KernelVersion"),
        "os": str(eng.get("OS")) + "/" + str(eng.get("Architecture")),
        "server_version": eng.get("ServerVersion"),
    }
    nodes.sort(key=lambda n: n["service"])
    with _lock:
        _state["nodes"] = nodes
        _state["engine"] = engine
        _state["updated"] = time.time()
        _state["error"] = ""


def _loop():
    while True:
        t0 = time.time()
        try:
            _collect()
            _state["cycle_seconds"] = round(time.time() - t0, 2)
        except Exception as e:
            with _lock:
                _state["error"] = (type(e).__name__ + ": " + str(e))[:200]
        time.sleep(max(INTERVAL - (time.time() - t0), 1.0))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.split("?")[0] in ("/json", "/"):
            with _lock:
                body = json.dumps(_state).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()


def main():
    threading.Thread(target=_loop, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
