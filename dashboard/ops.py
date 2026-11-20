"""⑦ 大盘「前后端配置 · 实时流量」聚合。

数据分三块，全部只读、失败降级不抛异常：
- 后端配置：直接 import 的 get_settings()（gateway/local_model/ollama 生效参数）。
- 实时流量：qa_events 近 N 分钟按分钟分桶（命中/推理/拒绝/错误 + 通道），observe 现成聚合。
- 前端 + 主机：读备机 host 侧采集器写好的 /var/lib/daobrain-ops/ops.json
  （内含 nvidia-smi / ollama ps / 关键容器 docker stats，以及经主机→备机隧道 ssh 拉回的 entry.json）。
  该文件缺失或过旧时优雅降级，只影响「前端/主机」块，不影响其余。
"""
import json
import os
import time
from pathlib import Path

from common import observe

OPS_JSON = Path(os.environ.get("DAOBRAIN_OPS_JSON", "/var/lib/daobrain-ops/ops.json"))
STALE_SEC = 60          # ops.json 超过此秒数视为过期
TRAFFIC_MIN = 30        # 流量曲线回看分钟数

# 后端配置只暴露非敏感的运行参数，绝不返回任何密钥 / DSN。
_BACKEND_KEYS = [
    "gateway_green_backend", "local_model_backend", "model_output_protocol",
    "local_model_name", "open_reason_model", "open_reason_enabled",
    "ollama_url", "model_context_tokens", "model_max_output_tokens",
    "model_cpu_threads", "model_timeout_seconds", "max_requests_per_minute",
]


def backend_config():
    from common.config import get_settings
    s = get_settings()
    out = {}
    for k in _BACKEND_KEYS:
        out[k] = getattr(s, k, None)
    return out


def _traffic():
    """qa_events 近 TRAFFIC_MIN 分钟按分钟 + 按处置/通道聚合。"""
    try:
        with observe._LOCK:
            conn = observe._connect()
            series = observe._fetch(conn,
                "SELECT to_char(date_trunc('minute', ts at time zone 'UTC'),'HH24:MI') AS m, "
                "count(*)::int AS total, "
                "count(*) FILTER (WHERE outcome='release')::int AS release, "
                "count(*) FILTER (WHERE outcome='reason')::int AS reason, "
                "count(*) FILTER (WHERE outcome LIKE 'refuse%%')::int AS refuse, "
                "count(*) FILTER (WHERE outcome='dependency_error' OR verify='error')::int AS error "
                "FROM qa_events WHERE ts > now() - (%s || ' minutes')::interval "
                "GROUP BY 1 ORDER BY 1", (str(TRAFFIC_MIN),))
            chan = observe._fetch(conn,
                "SELECT COALESCE(channel,'unrouted') AS c, count(*)::int AS n "
                "FROM qa_events WHERE ts > now() - (%s || ' minutes')::interval "
                "GROUP BY 1 ORDER BY 2 DESC", (str(TRAFFIC_MIN),))
            model = observe._fetch(conn,
                "SELECT COALESCE(model,'') AS m, count(*)::int AS n "
                "FROM qa_events WHERE ts > now() - (%s || ' minutes')::interval "
                "GROUP BY 1 ORDER BY 2 DESC", (str(TRAFFIC_MIN),))
            lat = observe._fetch(conn,
                "SELECT count(*)::int AS n, COALESCE(AVG(latency_ms),0)::int AS avg_ms, "
                "COALESCE((percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms)),0)::int AS p95_ms "
                "FROM qa_events WHERE ts > now() - (%s || ' minutes')::interval AND latency_ms > 0",
                (str(TRAFFIC_MIN),))
        win = observe.metrics(TRAFFIC_MIN)
        return {"window_min": TRAFFIC_MIN,
                "series": series or [],
                "by_channel": chan or [],
                "by_model": model or [],
                "latency": (lat[0] if lat else {"n": 0, "avg_ms": 0, "p95_ms": 0}),
                "totals": win.get("totals", {}),
                "outcome": {x["k"]: x["n"] for x in win.get("by_outcome", [])}}
    except Exception as e:
        return {"window_min": TRAFFIC_MIN, "series": [], "error": type(e).__name__}


def _load_ops_file():
    try:
        raw = json.loads(OPS_JSON.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, "missing"
    except Exception as e:
        return None, type(e).__name__ + ": " + str(e)[:120]
    gen = raw.get("_generated_epoch") or 0
    age = int(time.time() - gen) if gen else None
    if age is not None and age > STALE_SEC:
        raw["_stale"] = True
        raw["_age_sec"] = age
    return raw, None


def build():
    ops, err = _load_ops_file()
    entry = (ops or {}).get("entry")
    entry_age = None
    if isinstance(entry, dict):
        eg = entry.get("generated_at")
        if eg:
            entry_age = int(time.time() - int(eg))
    return {
        "as_of": time.strftime("%Y-%m-%d %H:%M:%S %z"),
        "backend_config": backend_config(),
        "traffic": _traffic(),
        "backup": {
            "ops_file_error": err,
            "stale": bool((ops or {}).get("_stale")),
            "age_sec": (ops or {}).get("_age_sec"),
            "gpu": (ops or {}).get("gpu"),
            "ollama": (ops or {}).get("ollama"),
            "containers": (ops or {}).get("containers"),
            "host": (ops or {}).get("host"),
        } if ops else None,
        "entry": {
            "age_sec": entry_age,
            "data": entry,
        } if isinstance(entry, dict) else None,
    }
