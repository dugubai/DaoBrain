"""流量埋点与问答落库（qa_events）。

设计铁律：观测写入是"尽力而为"——任何写库异常都不得阻断或改变业务应答；
只落元数据、路由/核验结果与绿区已释放答案，绝不落被拒绝/敏感请求正文（对齐 common.audit）。
读取端做只读聚合，供大盘/问答列表消费。
"""
import re
import threading
from decimal import Decimal

from common.audit import audit
from common.config import get_settings

_LOCK = threading.Lock()
_CONN = None
_SCHEMA_READY = False

_DDL = [
    """CREATE TABLE IF NOT EXISTS qa_events (
        id             BIGSERIAL PRIMARY KEY,
        ts             TIMESTAMPTZ NOT NULL DEFAULT now(),
        task_hash      TEXT,
        persona        TEXT,
        query          TEXT,
        zone           TEXT,
        channel        TEXT,
        route_target   TEXT,
        tier           TEXT,
        verify         TEXT,
        released       BOOLEAN,
        outcome        TEXT,
        model          TEXT,
        evidence_count INTEGER,
        evidence_ids   TEXT,
        latency_ms     INTEGER,
        note           TEXT,
        answer         TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS qa_events_ts_idx ON qa_events (ts DESC)",
    "CREATE INDEX IF NOT EXISTS qa_events_hash_idx ON qa_events (task_hash)",
]


def _dsn():
    dsn = get_settings().postgres_dsn
    return re.sub(r"postgresql\+psycopg", "postgresql", dsn, count=1)


def _connect():
    global _CONN, _SCHEMA_READY
    import psycopg
    if _CONN is not None:
        try:
            _CONN.execute("SELECT 1")
        except Exception:
            try:
                _CONN.close()
            except Exception:
                pass
            _CONN = None
    if _CONN is None:
        _CONN = psycopg.connect(_dsn(), autocommit=True, connect_timeout=3)
    if not _SCHEMA_READY:
        for stmt in _DDL:
            _CONN.execute(stmt)
        _SCHEMA_READY = True
    return _CONN


def _clean(value, limit):
    if value is None:
        return None
    text = str(value)
    return text[:limit] if text else None


def record(**f):
    """写入一条问答/流量事件。返回 True/False，绝不把异常抛给调用方。"""
    try:
        with _LOCK:
            conn = _connect()
            conn.execute(
                "INSERT INTO qa_events(task_hash,persona,query,zone,channel,route_target,tier,"
                "verify,released,outcome,model,evidence_count,evidence_ids,latency_ms,note,answer)"
                " VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (_clean(f.get("task_hash"), 64), _clean(f.get("persona"), 64),
                 _clean(f.get("query"), 500), _clean(f.get("zone"), 16),
                 _clean(f.get("channel"), 32), _clean(f.get("route_target"), 64),
                 _clean(f.get("tier"), 16), _clean(f.get("verify"), 16),
                 bool(f.get("released")), _clean(f.get("outcome"), 32),
                 _clean(f.get("model"), 64),
                 int(f.get("evidence_count") or 0), _clean(f.get("evidence_ids"), 512),
                 int(f.get("latency_ms") or 0), _clean(f.get("note"), 256),
                 _clean(f.get("answer"), 2000)))
        return True
    except Exception as exc:
        audit("gateway", "observe_write_error", model=type(exc).__name__)
        return False


def _fetch(conn, sql, params=None):
    cur = conn.execute(sql, params or [])
    cols = [d.name for d in cur.description]
    out = []
    for row in cur.fetchall():
        rec = dict(zip(cols, row))
        for key, val in rec.items():
            if isinstance(val, Decimal):
                rec[key] = float(val)
        out.append(rec)
    return out


def metrics(window_minutes=60):
    with _LOCK:
        conn = _connect()
    params = []
    if window_minutes:
        where = "WHERE ts >= (%s || ' minutes')::interval"
        params = [str(int(window_minutes))]
    else:
        where = ""

    def agg(sql):
        return _fetch(conn, sql, params)

    totals = agg("SELECT count(*)::int AS total, "
                 "count(*) FILTER (WHERE released)::int AS released, "
                 "count(*) FILTER (WHERE outcome IN ('refuse_verify','refuse_entry'))::int AS refused, "
                 "count(*) FILTER (WHERE outcome='dependency_error')::int AS errors, "
                 "COALESCE(AVG(latency_ms),0)::int AS avg_latency, "
                 "COALESCE(percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms),0)::int AS p95_latency "
                 "FROM qa_events " + where)[0]

    def group(col):
        return agg("SELECT COALESCE(" + col + ",'') AS k, count(*)::int AS n "
                    "FROM qa_events " + where + " GROUP BY 1 ORDER BY 2 DESC")

    return {"window_minutes": int(window_minutes) if window_minutes else "all",
            "totals": totals,
            "by_zone": group("zone"),
            "by_channel": group("channel"),
            "by_tier": group("tier"),
            "by_outcome": group("outcome"),
            "by_model": group("model")}


def qa_list(limit=200):
    limit = max(1, min(int(limit or 200), 1000))
    with _LOCK:
        conn = _connect()
        return _fetch(conn,
            "SELECT id, task_hash, to_char(ts at time zone 'UTC','YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"') AS ts, "
            "persona, zone, channel, tier, verify, released, outcome, model, evidence_count, latency_ms, "
            "LEFT(query,120) AS query FROM qa_events ORDER BY ts DESC LIMIT %s", (limit,))


def qa_detail(event_id):
    with _LOCK:
        conn = _connect()
        rows = _fetch(conn,
            "SELECT id, task_hash, to_char(ts at time zone 'UTC','YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"') AS ts, "
            "persona, query, zone, channel, route_target, tier, verify, released, outcome, model, "
            "evidence_count, evidence_ids, latency_ms, note, answer FROM qa_events WHERE id = %s", (int(event_id),))
        return rows[0] if rows else None


def tier_stats():
    with _LOCK:
        conn = _connect()
        return _fetch(conn,
            "SELECT COALESCE(tier,'') AS tier, count(*)::int AS total, "
            "count(*) FILTER (WHERE released)::int AS released, "
            "count(*) FILTER (WHERE outcome='refuse_verify')::int AS refuse_verify, "
            "count(*) FILTER (WHERE outcome='refuse_entry')::int AS refuse_entry, "
            "count(*) FILTER (WHERE outcome='dependency_error')::int AS errors, "
            "COALESCE(AVG(latency_ms),0)::int AS avg_latency FROM qa_events GROUP BY 1 ORDER BY 2 DESC")


def channel_stats():
    with _LOCK:
        conn = _connect()
        return _fetch(conn,
            "SELECT COALESCE(channel,'') AS channel, COALESCE(zone,'') AS zone, count(*)::int AS n "
            "FROM qa_events GROUP BY 1,2 ORDER BY 3 DESC")
