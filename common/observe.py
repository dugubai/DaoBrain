"""流量埋点与问答落库（qa_events）。

设计铁律：观测写入是"尽力而为"——任何写库异常都不得阻断或改变业务应答；
只落元数据、路由/核验结果与绿区已释放答案，绝不落被拒绝/敏感请求正文（对齐 common.audit）。
读取端做只读聚合，供大盘/问答列表消费；读取端同样脱敏，使历史泄漏行也不被匿名回读明文。

隐私策略（P0）：
- 非释放行（refuse_* / dependency_error / error）：不落 query、answer 原文，仅留元数据。
- 开放推理行（outcome=reason）：不落模型自由推理正文（非绿区已核验答案），仅留脱敏后的 query。
- 释放的绿区行（outcome=release）：留答案（逐字核验公开正文），但 query/answer/note 一律先脱敏。
- 脱敏覆盖：证件/卡号/手机电话/住址邮箱/账号密码密钥令牌/凭证/URL/邮箱/15–19 位数字串。
"""
import re
import threading
from decimal import Decimal

from common.audit import audit
from common.config import get_settings

_LOCK = threading.Lock()
_CONN = None
_SCHEMA_READY = False

# 敏感/隐私/凭证模式（不含诊疗词，诊疗另有 gateway._MEDICAL 处理）。
_SENSITIVE = re.compile(
    r"(?:身份证|护照|银行卡|储蓄卡|信用卡|社保|公积金|社保号)"
    r"|(?:手机|电话|座机)(?:号码|号)?"
    r"|(?:住址|地址|邮箱|邮件|账号|帐号|密码|口令|密钥|秘钥|私钥|令牌|验证码)"
    r"|(?:secret|password|passwd|token|api[_-]?key|private[_-]?key|credential|authorization)"
    r"|(?:bearer\s+[A-Za-z0-9._-]+)"
    r"|[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
    r"|https?://\S+|www\.\S+"
    r"|(?<![0-9])[0-9]{15,19}(?![0-9])",
    re.IGNORECASE)


def _redact(value, limit):
    text = _clean(value, limit)
    if text is None:
        return None
    return _SENSITIVE.sub("〔已脱敏〕", text)


def sensitive_hit(text) -> bool:
    """供 gateway 复用：命中即视为含隐私/凭证/外链，须据实拒答且不落库原文。"""
    return isinstance(text, str) and bool(_SENSITIVE.search(text))


def _privacy(query, answer, note, released, outcome):
    """按释放状态与处置结果裁剪落库正文，返回 (query, answer, note)。"""
    note = _redact(note, 256)
    if not released:
        return None, None, note          # 被拒/错误：绝不落正文
    if outcome == "reason":
        return _redact(query, 500), None, note   # 开放推理正文不落库
    return _redact(query, 500), _redact(answer, 2000), note  # 绿区已核验答案


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
    """写入一条问答/流量事件。落库前强制隐私裁剪，返回 True/False，绝不把异常抛给调用方。"""
    try:
        query, answer, note = _privacy(f.get("query"), f.get("answer"), f.get("note"),
                                       bool(f.get("released")), f.get("outcome"))
        with _LOCK:
            conn = _connect()
            conn.execute(
                "INSERT INTO qa_events(task_hash,persona,query,zone,channel,route_target,tier,"
                "verify,released,outcome,model,evidence_count,evidence_ids,latency_ms,note,answer)"
                " VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (_clean(f.get("task_hash"), 64), _clean(f.get("persona"), 64),
                 query, _clean(f.get("zone"), 16),
                 _clean(f.get("channel"), 32), _clean(f.get("route_target"), 64),
                 _clean(f.get("tier"), 16), _clean(f.get("verify"), 16),
                 bool(f.get("released")), _clean(f.get("outcome"), 32),
                 _clean(f.get("model"), 64),
                 int(f.get("evidence_count") or 0), _clean(f.get("evidence_ids"), 512),
                 int(f.get("latency_ms") or 0), note, answer))
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
        where = "WHERE ts >= now() - (%s * interval '1 minute')"
        params = [int(window_minutes)]
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
            "CASE WHEN released THEN LEFT(query,120) ELSE NULL END AS query FROM qa_events ORDER BY ts DESC LIMIT %s",
            (limit,))


def qa_detail(event_id):
    with _LOCK:
        conn = _connect()
        rows = _fetch(conn,
            "SELECT id, task_hash, to_char(ts at time zone 'UTC','YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"') AS ts, "
            "persona, query, zone, channel, route_target, tier, verify, released, outcome, model, "
            "evidence_count, evidence_ids, latency_ms, note, answer FROM qa_events WHERE id = %s", (int(event_id),))
        if not rows:
            return None
        row = rows[0]
        # 读取端脱敏：非释放行绝不明文回显正文（含历史泄漏行）；释放行二次脱敏兜底。
        if not row.get("released"):
            row["query"] = None
            row["answer"] = None
        else:
            row["query"] = _redact(row.get("query"), 500)
            row["answer"] = _redact(row.get("answer"), 2000)
            if row.get("outcome") == "reason":
                row["answer"] = None
        return row


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
