"""⑦ 大盘：把备机容器实时指标 + 主机节点健康 + 分级链路流量,汇聚成一张“架构拓扑·实时运行”图。

数据来源：
- 备机节点：本机 topology 采集器(经 Docker Engine API 读容器 CPU/内存/网络/重启/状态),仅供内网访问。
- 主机节点：中台(/manage) 与 主页门户(/daobrain) 经主机 80 端口 HTTP 探活(尽力而为)。
- 数据流：读 qa_events 近窗口埋点,按区域/通道换算每条边的实时 req/min。
"""
import json
import time
import urllib.request

from common import observe

COLLECTOR_URL = "http://topology:8021/json"
FLOW_WINDOW_MIN = 15

# service(=compose 服务名) -> (中文标签, 层次列, 端口, 分组)
LAYERS = {
    0: "入口 / 分发",
    1: "检索 (分级 RAG)",
    2: "模型 / 推理",
    3: "核验 / 加工",
    4: "基础设施",
    5: "对外服务",
}
TOPO = {
    "gateway":            ("① 网关 gateway", 0, 8000, "core"),
    "hybrid_iface":       ("混合分发 hybrid_iface", 0, 8004, "core"),
    "agent_orchestrator": ("公有云编排 orchestrator", 0, 8002, "cloud"),
    "redline_gate":       ("红线闸门 redline_gate", 0, 8009, "core"),
    "rag_public":         ("公开检索 rag_public", 1, 8003, "cloud"),
    "rag_green":          ("绿区检索 rag_green", 1, 8007, "green"),
    "rag_red":            ("红区检索 rag_red", 1, 8008, "red"),
    "corpus_kb":          ("典籍库 corpus_kb", 1, 8012, "green"),
    "local_model":        ("本地小模型 local_model", 2, 8006, "green"),
    "daobrain_tuned":     ("调教模型 daobrain_tuned", 2, 8011, "red"),
    "external_model":     ("外部模型 external_model", 2, 8005, "cloud"),
    "ollama":             ("Ollama 推理 · GPU", 2, 11434, "green"),
    "output_verify":      ("输出核验 output_verify", 3, 8010, "core"),
    "card_annotator":     ("卡片标注 card_annotator", 3, 8013, "aux"),
    "ocr":                ("OCR ocr", 3, 8014, "aux"),
    "postgres":           ("PostgreSQL · 落库", 4, 5432, "infra"),
    "redis":              ("Redis · 限流/缓存", 4, 6379, "infra"),
    "qdrant":             ("Qdrant · 向量库", 4, 6333, "infra"),
    "dashboard":          ("⑦ 大盘 dashboard", 5, 8020, "edge"),
    "public_demo":        ("公网样例 public_demo", 5, 18100, "edge"),
}

# 主机(admin@39.102.49.1:80)承载的节点——采集器在备机看不到其容器指标,改经公网探活
PRIMARY_NODES = [
    {"service": "control_plane", "label": "中台 control_plane", "layer": 0,
     "url": "http://39.102.49.1/manage/", "note": "/manage · 主机容器 :8791"},
    {"service": "portal", "label": "主页门户 /daobrain", "layer": 0,
     "url": "http://39.102.49.1/daobrain/", "note": "主机容器 :18100 + nginx:80"},
]

# 边：from_service -> to_service（label 用于展示；bucket 决定取哪一档流量做 req/min）
EDGES = [
    ("gateway", "hybrid_iface", "总入口", "dispatched"),
    ("hybrid_iface", "rag_green", "绿区分流", "green"),
    ("rag_green", "local_model", "绿区证据", "green"),
    ("local_model", "output_verify", "绿区待验", "green"),
    ("local_model", "ollama", "GPU 调用", "green"),
    ("hybrid_iface", "rag_red", "红区分流", "red"),
    ("rag_red", "daobrain_tuned", "红区推理", "red"),
    ("gateway", "agent_orchestrator", "公有云分流", "cloud"),
    ("agent_orchestrator", "rag_public", "公开检索", "cloud"),
    ("rag_public", "external_model", "外部模型", "cloud"),
    ("gateway", "postgres", "问答落库", "total"),
    ("gateway", "redis", "限流", "total"),
    ("rag_green", "qdrant", "向量检索", "green"),
    ("rag_red", "qdrant", "向量检索", "red"),
]


def _probe(url, timeout=3.0):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "daobrain-topo"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            code = getattr(r, "status", None) or r.getcode()
            return "up" if 200 <= code < 400 else "down", code
    except Exception as e:
        return "down", type(e).__name__


def _fetch_collector(timeout=4.0):
    try:
        with urllib.request.urlopen(COLLECTOR_URL, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace")), None
    except Exception as e:
        return {}, type(e).__name__ + ": " + str(e)[:120]


def _flow():
    m = observe.metrics(FLOW_WINDOW_MIN)
    t = m.get("totals", {})
    zone = {x["k"]: x["n"] for x in m.get("by_zone", [])}
    chan = {(x["k"] or "unrouted"): x["n"] for x in m.get("by_channel", [])}
    out = {x["k"]: x["n"] for x in m.get("by_outcome", [])}
    total = t.get("total", 0) or 0
    entry_refused = out.get("refuse_entry", 0) or 0
    green = zone.get("green", 0) or 0
    red = zone.get("red", 0) or 0
    cloud = chan.get("public_cloud", 0) or 0
    buckets = {
        "total": total,
        "dispatched": max(total - entry_refused, 0),
        "green": green,
        "red": red,
        "cloud": cloud,
    }
    per_min = {k: round(v * 60.0 / FLOW_WINDOW_MIN, 2) for k, v in buckets.items()}
    return {"window_min": FLOW_WINDOW_MIN, "counts": buckets, "per_min": per_min,
            "totals": t, "outcome": out}


def build():
    raw, cerr = _fetch_collector()
    nodes = []
    seen = set()
    for n in raw.get("nodes", []):
        svc = n.get("service", "")
        label, layer, port, group = TOPO.get(svc, (svc, 0, None, "aux"))
        state = n.get("state")
        up = state == "running"
        nodes.append({
            "service": svc, "label": label, "layer": layer, "host": "备机",
            "port": port, "group": group, "container": n.get("name"),
            "state": state, "status": n.get("status"), "health": n.get("health"),
            "restarts": n.get("restarts", 0), "image": n.get("image"),
            "cpu": n.get("cpu_percent", 0.0), "mem_mb": n.get("mem_used_mb", 0),
            "mem_percent": n.get("mem_percent", 0), "net_rx_mb": n.get("net_rx_mb", 0),
            "net_tx_mb": n.get("net_tx_mb", 0), "net_mbps": n.get("net_mbps", 0),
            "pids": n.get("pids", 0), "up": up,
        })
        seen.add(svc)

    for pn in PRIMARY_NODES:
        health, code = _probe(pn["url"])
        nodes.append({
            "service": pn["service"], "label": pn["label"], "layer": pn["layer"], "host": "主机",
            "port": 80, "group": "edge", "container": pn["note"],
            "state": health, "status": "HTTP " + str(code), "health": health,
            "restarts": None, "image": None, "cpu": None, "mem_mb": None,
            "mem_percent": None, "net_rx_mb": None, "net_tx_mb": None, "net_mbps": None,
            "pids": None, "up": health == "up",
        })

    # 只画两端都存在且有标签的边；signal 用对应档位的 req/min
    svcset = {x["service"] for x in nodes}
    per_min = _flow()["per_min"]
    edges = []
    for a, b, lab, bucket in EDGES:
        if a in svcset and b in svcset:
            edges.append({"from": a, "to": b, "label": lab, "signal": per_min.get(bucket, 0)})

    layers = [{"layer": k, "title": LAYERS[k]} for k in sorted(LAYERS)]
    return {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
        "collector_error": cerr,
        "engine": raw.get("engine", {}),
        "collector_updated": raw.get("updated", 0),
        "cycle_seconds": raw.get("cycle_seconds"),
        "layers": layers,
        "nodes": nodes,
        "edges": edges,
        "flow": _flow(),
    }
