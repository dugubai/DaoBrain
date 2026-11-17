"""结构化审计只保存事件元数据，不记录正文、密钥、外部错误URL。"""
import hashlib
import json
import logging
import time

_log = logging.getLogger("daobrain.audit")
_log.setLevel(logging.INFO)
if not _log.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    _log.addHandler(handler)
_log.propagate = False
_ALLOWED = {"status", "tokens", "iterations", "cost_cny", "model", "count"}


def audit(module, event, *, task_id="", zone="", external=False, **fields):
    rec = {"ts": round(time.time(), 3), "module": module, "event": event,
           "task_hash": hashlib.sha256(task_id.encode()).hexdigest()[:16],
           "zone": zone, "external": bool(external)}
    rec.update({key: str(value)[:128] for key, value in fields.items() if key in _ALLOWED})
    _log.info(json.dumps(rec, ensure_ascii=False))
