"""Explicit fixed-endpoint gateway; no query-driven URLs or fallback path."""
import ipaddress
import os
from pathlib import Path
import re
from urllib.parse import urlsplit
import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import Field
from application_hub.security import Input, roles
from common.catalog import trusted
from common.qa import strict_json
from common.schemas import Retrieved, SourceType, Zone

router = APIRouter()


def private_url(value):
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        if (not host or parsed.username is not None or parsed.password is not None or parsed.query
                or parsed.fragment or parsed.path not in {"", "/"} or parsed.port not in {8000, 443, 18131, 18132}
                or value != value.strip() or not value.isascii()):
            raise ValueError
        # HTTP only to loopback IP or a fixed Docker single-label service.
        docker_host = re.fullmatch(r"[a-z][a-z0-9-]{1,40}", host) is not None
        try:
            ip = ipaddress.ip_address(host)
            loopback = ip.is_loopback
        except ValueError:
            loopback = False
        if parsed.scheme != "https" and not (parsed.scheme == "http" and (docker_host or loopback)):
            raise ValueError
        return value.rstrip("/")
    except (TypeError, ValueError):
        raise ValueError("endpoint configuration rejected") from None


class NodeQuery(Input):
    source_id: str = Field(min_length=1, max_length=128, pattern=r"^[a-z0-9_-]+$")


@router.post("/api/nodes/{node_id}/source")
def fetch(node_id: str, req: NodeQuery, _user=Depends(roles("admin", "editor", "reviewer"))):
    try:
        records = strict_json(os.environ.get("APP_NODE_ENDPOINTS", "{}"))
        if len(records) > 2 or node_id not in records:
            raise ValueError
        record = records[node_id]
        if not isinstance(record, dict) or set(record) != {"url", "secret_file"}:
            raise ValueError
        url = private_url(record["url"])
        key = Path(record["secret_file"]).read_text().strip()
        if len(key) < 32:
            raise ValueError
    except (ValueError, TypeError, OSError, KeyError):
        raise HTTPException(503, "受信节点未配置") from None
    try:
        # No credentials from the person/session or private questions are forwarded.
        with httpx.Client(timeout=5, trust_env=False, follow_redirects=False) as client:
            with client.stream("POST", url + "/v1/public/source", headers={"X-Node-Key": key},
                               json={"source_id": req.source_id}) as response:
                if response.status_code != 200:
                    raise ValueError
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > 16384:
                        raise ValueError
        payload = strict_json(bytes(body))
        if set(payload) != {"node_id", "evidence", "scope", "role_simulation"}:
            raise ValueError
        evidence = Retrieved.model_validate(payload["evidence"])
        if (payload["node_id"] != node_id or payload["scope"] != "public_classics_only"
                or payload["role_simulation"] is not True or evidence.zone != Zone.GREEN
                or evidence.provenance.source_type != SourceType.PUBLIC_CLASSIC
                or evidence.provenance.source_id != req.source_id or not trusted(evidence)):
            raise ValueError
        return payload
    except (httpx.HTTPError, ValueError, TypeError, KeyError):
        raise HTTPException(503, "节点调用或来源核验失败；未回退") from None
