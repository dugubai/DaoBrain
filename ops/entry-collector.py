"""DaoBrain 主机侧运维采集器：systemd timer 每 5s 触发一次，把主机 nginx/socat/public_demo/流量计数写到 /var/lib/daobrain-ops/entry.json，供备机大盘 SSH 拉取。写盘原子化，异常时保留上一次快照。"""
import json, os, re, subprocess, tempfile, time, pathlib

OUT = pathlib.Path("/var/lib/daobrain-ops/entry.json")
ACCESS = "/var/log/nginx/access.log"
MONTH = {"Jan":1,"Feb":2,"Mar":3,"Apr":4,"May":5,"Jun":6,"Jul":7,"Aug":8,"Sep":9,"Oct":10,"Nov":11,"Dec":12}
LINE = re.compile(r"\[(\d+)/(\w+)/(\d+):(\d+):(\d+):(\d+) [+\-]\d+\] \"[A-Z]+ (\S+)[^\"]*\" (\d{3})")


def sh(cmd, timeout=3):
    # 主机 /usr/bin/python3 是 3.6,不支持 check_output(text=),须用 universal_newlines。
    # nginx -v 等把版本写 stderr 且退出码非 0,命令内已用 2>&1 合流到 stdout,异常时回捞 e.output。
    try:
        return subprocess.check_output(cmd, shell=True, universal_newlines=True,
                                       stderr=subprocess.DEVNULL, timeout=timeout)
    except subprocess.CalledProcessError as e:
        out = getattr(e, "output", None)
        return out if isinstance(out, str) else ""
    except Exception:
        return ""


def uptime_sec():
    try:
        return int(float(open("/proc/uptime").read().split()[0]))
    except Exception:
        return 0


def meminfo():
    d = {}
    try:
        for L in open("/proc/meminfo"):
            k, v = L.split(":", 1)
            d[k] = int(v.strip().split()[0])
    except Exception:
        pass
    return {"mem_total_kb": d.get("MemTotal", 0), "mem_available_kb": d.get("MemAvailable", 0),
            "swap_total_kb": d.get("SwapTotal", 0), "swap_free_kb": d.get("SwapFree", 0)}


def loadavg():
    try:
        a, b, c = os.getloadavg()
        return {"l1": round(a, 2), "l5": round(b, 2), "l15": round(c, 2)}
    except Exception:
        return {}


def disk_free(path="/"):
    try:
        s = os.statvfs(path)
        return {"avail_mb": s.f_bavail * s.f_frsize // 1024 // 1024,
                "total_mb": s.f_blocks * s.f_frsize // 1024 // 1024}
    except Exception:
        return {}


def tcp_count(expr):
    # expr 含括号,须整体单引号交给 ss,否则 shell 把 ( ) 当子 shell。
    out = sh("ss -tan state established '" + expr + "' 2>/dev/null | tail -n +2 | wc -l")
    try:
        return int(out.strip())
    except Exception:
        return None


def access_recent(window=60, lines=2000):
    now = time.time()
    cutoff = now - window
    text = sh(f"sudo tail -n {lines} {ACCESS}", timeout=4)
    b = {"total": 0, "2xx": 0, "3xx": 0, "4xx": 0, "5xx": 0,
         "429": 0, "403": 0, "404": 0, "503": 0, "500": 0}
    paths = {}
    daobrain_qa = 0
    dash = 0
    manage = 0
    other = 0
    for L in text.splitlines():
        m = LINE.search(L)
        if not m:
            continue
        try:
            d = int(m.group(1)); mo = MONTH[m.group(2)]; y = int(m.group(3))
            h = int(m.group(4)); mi = int(m.group(5)); s = int(m.group(6))
            ts = time.mktime((y, mo, d, h, mi, s, 0, 0, -1))
            if ts < cutoff:
                continue
        except Exception:
            continue
        st = m.group(8); path = m.group(7)
        b["total"] += 1
        cls = st[0] + "xx"
        if cls in b:
            b[cls] += 1
        if st in ("429", "403", "404", "500", "503"):
            b[st] = b.get(st, 0) + 1
        top = "/".join(path.split("?")[0].split("/")[:3]) or "/"
        paths[top] = paths.get(top, 0) + 1
        if path.startswith("/daobrain/api/"):
            daobrain_qa += 1
        if path.startswith("/daobrain/dash"):
            dash += 1
        if path.startswith("/manage"):
            manage += 1
        if not (path.startswith("/daobrain") or path.startswith("/manage")):
            other += 1
    top_paths = sorted(paths.items(), key=lambda x: -x[1])[:10]
    return {"window_sec": window, "counts": b,
            "top_paths": [{"path": k, "n": v} for k, v in top_paths],
            "buckets": {"daobrain_qa": daobrain_qa, "dash": dash,
                        "manage": manage, "other_sites": other}}


def env_filter(name, keys):
    raw = sh(f"sudo docker exec {name} printenv 2>/dev/null", timeout=3)
    d = {}
    for L in raw.splitlines():
        if "=" in L:
            k, v = L.split("=", 1)
            if k in keys:
                d[k] = v
    return d


def container_state(name):
    out = sh(f"sudo docker inspect -f '{{{{.State.Status}}}}|{{{{.State.StartedAt}}}}|{{{{.RestartCount}}}}' {name} 2>/dev/null",
             timeout=3).strip()
    parts = out.split("|")
    if len(parts) < 2:
        return {"state": "missing"}
    return {"state": parts[0], "started_at": parts[1],
            "restarts": parts[2] if len(parts) > 2 else ""}


def container_ip(name):
    # 用普通字符串拼接避免 f-string 对 {} 的转义,单引号内 $ 直接交给 docker Go 模板。
    tmpl = "'{{range $k,$v := .NetworkSettings.Networks}}{{$v.IPAddress}}{{end}}'"
    return sh("sudo docker inspect -f " + tmpl + " " + name + " 2>/dev/null",
              timeout=3).strip()


def nginx_meta():
    ver = sh("nginx -v 2>&1", timeout=2).strip() or sh("sudo nginx -v 2>&1", timeout=2).strip()
    upm = sh("sudo pgrep -a nginx 2>/dev/null | grep 'nginx: master' | head -1", timeout=2).strip()
    active = sh("sudo grep -hE 'proxy_pass|limit_req' /root/DaoBrain/public_demo/nginx.locations.active.conf 2>/dev/null | head -6",
                timeout=2)
    rate = sh("sudo grep -hE 'limit_req' /etc/nginx/conf.d/daobrain-public-rate.conf 2>/dev/null | head -3",
              timeout=2)
    return {"version": ver, "master_line": upm,
            "active_proxy": [l.strip() for l in active.splitlines() if l.strip()],
            "rate_limit": [l.strip() for l in rate.splitlines() if l.strip()]}


def tunnel_meta():
    socat = sh("pgrep -a socat 2>/dev/null | grep 18083 | head -1", timeout=2).strip()
    ssh_fwd = sh("pgrep -af 'ssh.*18082' 2>/dev/null | head -1", timeout=2).strip()
    return {"socat_line": socat, "ssh_tunnel_line": ssh_fwd,
            "tcp_18083_established": tcp_count("( sport = :18083 or dport = :18083 )"),
            "tcp_18082_established": tcp_count("( sport = :18082 or dport = :18082 )")}


def collect():
    return {
        "generated_at": int(time.time()),
        "generated_at_str": time.strftime("%Y-%m-%d %H:%M:%S %z"),
        "host": sh("hostname").strip(),
        "uptime_sec": uptime_sec(),
        "load": loadavg(),
        "mem": meminfo(),
        "disk": disk_free("/"),
        "tcp_80_established": tcp_count("( dst :80 )"),
        "nginx": nginx_meta(),
        "tunnel": tunnel_meta(),
        "public_demo": {
            "env": env_filter("daobrain-public_demo-1",
                              {"PUBLIC_DEMO_BACKEND", "PUBLIC_DEMO_MODEL_NAME", "GATEWAY_URL"}),
            "state": container_state("daobrain-public_demo-1"),
            "ip": container_ip("daobrain-public_demo-1"),
        },
        "manage": {
            "state": container_state("daobrain-manage"),
            "ip": container_ip("daobrain-manage"),
        },
        "access": access_recent(60),
    }


def main():
    try:
        data = collect()
    except Exception as e:
        data = {"error": f"{type(e).__name__}: {str(e)[:200]}", "generated_at": int(time.time())}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(OUT.parent), prefix=".entry.", suffix=".json")
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
