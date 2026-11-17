#!/usr/bin/env python3
"""Offline application deployment: default plan, --check, or explicit --apply.

JSON keys: role, source, data, APP_ORIGIN; optional host (IPv4 loopback only),
host_port (18121), and model ({enabled: false} by default). Frontend requires
APP_BACKEND_URL: pre-provisioned HTTPS on a private literal IP. Default HTTPS
443 is normalized to explicit :443 for the unchanged runtime gateway policy.
Local-node requires NODE_ID, public_capability (exactly "classic-search"),
node_key_file, node_endpoint and node_transport=tls. Container loopback is not
host loopback: host-tunnel and frontend-only Docker-service URLs are refused.
Enabled model requires name="qwen2.5:0.5b" and an existing absolute cache path
containing models/manifests and models/blobs. Its pre-existing external volume
must be daobrain_ollama and demonstrably backed by that same local cache.

No container is started by plan/check. No dependencies, images, models, tunnel,
TLS terminator, credentials, directories or host settings are installed/created.
Generated Compose JSON is retained, exclusively created, never overwritten or
cleaned up. The 900 MiB ceiling includes RAM+swap: standalone's app allocation
is 128 MiB total (64 frontend + 64 backend); backend-only is capped at 128 MiB.
Backend may configure nodes (at most two), each using the local-node identity,
public capability, secret-file and private transport fields. Standalone cannot
borrow nodes. Linux-role simulation is NOT Windows real-machine acceptance.
"""

import argparse
import ctypes
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import platform
import re
import shutil
import socket
import stat
import subprocess
import sys
from urllib.parse import urlsplit
import uuid

APP_IMAGE = "daobrain-app:dev"
MODEL_IMAGE = "ollama/ollama:0.5.4"
MODEL_NAME = "qwen2.5:0.5b"
MODEL_VOLUME = "daobrain_ollama"
MIB = 1024 * 1024
ROLES = {"standalone", "frontend", "backend", "local-node"}
ENTRYPOINTS = {
    "frontend": "portable_frontend.main:app",
    "backend": "application_hub.main:app",
    "local-node": "local_node.main:app",
}
FIELDS = {"role", "source", "data", "APP_ORIGIN", "APP_BACKEND_URL", "host",
          "host_port", "model", "nodes", "NODE_ID", "public_capability", "node_key_file",
          "node_endpoint", "node_transport"}
NODE_FIELDS = {"NODE_ID", "public_capability", "node_key_file", "node_endpoint",
               "node_transport"}


class DeployError(ValueError):
    """Safe, value-free diagnostics; never include captured CLI output."""


def strict_json(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise DeployError("duplicate JSON key")
            result[key] = value
        return result

    def constant(_):
        raise DeployError("non-finite JSON number")

    def finite(value):
        if isinstance(value, float) and not math.isfinite(value):
            raise DeployError("non-finite JSON number")
        if isinstance(value, dict):
            for child in value.values():
                finite(child)
        elif isinstance(value, list):
            for child in value:
                finite(child)

    try:
        value = json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
        finite(value)
        return value
    except DeployError:
        raise
    except (UnicodeError, ValueError, TypeError, RecursionError, OverflowError):
        raise DeployError("invalid strict JSON") from None


def text(value, field):
    if (not isinstance(value, str) or not value or len(value) > 4096
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise DeployError(field + " must be a nonempty bounded string")
    return value


def existing_path(value, field, directory=True):
    path = Path(text(value, field))
    if not path.is_absolute():
        raise DeployError(field + " must be an existing absolute path")
    try:
        resolved = path.resolve(strict=True)
        if not (resolved.is_dir() if directory else resolved.is_file()):
            raise DeployError(field + " has the wrong file type")
    except (OSError, RuntimeError):
        raise DeployError(field + " must exist") from None
    return resolved


def origin(value, field):
    value = text(value, field)
    try:
        url = urlsplit(value)
        host = url.hostname
        port = url.port
        if (url.scheme not in {"http", "https"} or not host or url.username is not None
                or url.password is not None or url.path or url.query or url.fragment
                or "?" in value or "#" in value or "\\" in value
                or any(c.isspace() for c in value) or "%" in value
                or port is not None and not 1 <= port <= 65535):
            raise ValueError
        # Require one canonical origin, not parser-normalized alternate spellings.
        try:
            address = ipaddress.ip_address(host)
            host = str(address)
            if address.version == 6:
                host = "[" + host + "]"
        except ValueError:
            if len(host) > 253 or not all(re.fullmatch(
                    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in host.split(".")):
                raise ValueError
        canonical = url.scheme + "://" + host + (":" + str(port) if port else "")
        if canonical != value:
            raise ValueError
    except ValueError:
        raise DeployError(field + " must be an explicit canonical HTTP(S) origin") from None
    return value


def private_endpoint(value, transport, field="node_endpoint"):
    value = origin(value, field)
    url = urlsplit(value)
    try:
        address = ipaddress.ip_address(url.hostname)
    except ValueError:
        raise DeployError(field + " requires a private literal IP (no DNS lookup)") from None
    if address.is_loopback:
        raise DeployError(field + " loopback refers to this container, not a host tunnel; path refused and unaccepted")
    if address.version != 4:
        raise DeployError(field + " IPv6 routing is not provisioned by this bridge; path refused and unaccepted")
    private = any(address in network for network in
                  (ipaddress.ip_network("10.0.0.0/8"), ipaddress.ip_network("172.16.0.0/12"),
                   ipaddress.ip_network("192.168.0.0/16")))
    if not private:
        raise DeployError(field + " must remain private")
    if transport != "tls" or url.scheme != "https":
        raise DeployError(field + " requires pre-provisioned private TLS; host tunnels have no supported container channel")
    # nodes.private_url checks parsed.port, not the HTTPS effective port. Emit
    # explicit :443 so both the frontend proxy and node gateway accept defaults.
    if url.port is None:
        value += ":443"
    if urlsplit(value).port not in {443, 8000, 18131, 18132}:
        raise DeployError(field + " is outside the existing fixed proxy/gateway port policy")
    return value


def validate_config(raw):
    if not isinstance(raw, dict) or set(raw) - FIELDS:
        raise DeployError("configuration must be an object with allowlisted fields")
    role = raw.get("role")
    if not isinstance(role, str) or role not in ROLES:
        raise DeployError("unsupported role")
    source = existing_path(raw.get("source"), "source")
    data = existing_path(raw.get("data"), "data")
    if source == data or source in data.parents or data in source.parents:
        raise DeployError("source and persistent data must not overlap")
    host = raw.get("host", "127.0.0.1")
    try:
        address = ipaddress.ip_address(text(host, "host"))
        if address.version != 4 or not address.is_loopback or str(address) != host:
            raise ValueError
    except ValueError:
        raise DeployError("host must be a canonical IPv4 loopback IP; IPv6 publication is unaccepted") from None
    port = raw.get("host_port", 18121)
    if type(port) is not int or not 1 <= port <= 65535:
        raise DeployError("host_port must be an integer in 1..65535")
    config = {"role": role, "source": str(source), "data": str(data), "host": host,
              "host_port": port, "APP_ORIGIN": origin(raw.get("APP_ORIGIN"), "APP_ORIGIN")}
    if role == "frontend":
        # No backend service exists on a frontend-only project's bridge. A
        # single-label service or host loopback cannot identify a trusted route.
        config["APP_BACKEND_URL"] = private_endpoint(raw.get("APP_BACKEND_URL"), "tls", "APP_BACKEND_URL")
    elif "APP_BACKEND_URL" in raw:
        raise DeployError("APP_BACKEND_URL is permitted only for frontend")
    if role == "local-node":
        node_id = text(raw.get("NODE_ID"), "NODE_ID")
        if not re.fullmatch(r"[a-z][a-z0-9-]{1,31}", node_id):
            raise DeployError("invalid NODE_ID")
        if raw.get("public_capability") != "classic-search":
            raise DeployError("local-node exposes only classic-search; RED is forbidden")
        key = existing_path(raw.get("node_key_file"), "node_key_file", directory=False)
        if source == key.parent or source in key.parents or data == key.parent or data in key.parents:
            raise DeployError("node key must be outside source and data mounts")
        # Only metadata, never key bytes. Host owner controls access; never chmod.
        key_stat = key.stat()
        if key_stat.st_size < 32 or (os.name != "nt" and stat.S_IMODE(key_stat.st_mode) & 0o077):
            raise DeployError("node key must be at least 32 bytes and owner-only (Windows ACL must be provisioned)")
        transport = raw.get("node_transport")
        if transport not in ("tls", "tunnel"):
            raise DeployError("node_transport requires tls or tunnel")
        config.update(NODE_ID=node_id, public_capability="classic-search",
                      node_key_file=str(key), node_transport=transport,
                      node_endpoint=private_endpoint(raw.get("node_endpoint"), transport))
    elif set(raw) & NODE_FIELDS:
        raise DeployError("node identity/capability/secrets are local-node-only")
    model = raw.get("model", {"enabled": False})
    if not isinstance(model, dict) or type(model.get("enabled")) is not bool:
        raise DeployError("model must explicitly contain a boolean enabled")
    if model["enabled"]:
        if role not in ("standalone", "backend"):
            raise DeployError("model is allowed only for standalone or backend")
        if set(model) != {"enabled", "name", "path"} or model.get("name") != MODEL_NAME:
            raise DeployError("model accepts only the fixed local model name and cache path; no URLs")
        cache = existing_path(model.get("path"), "model.path")
        if (cache == source or cache == data or source in cache.parents or data in cache.parents
                or cache in source.parents or cache in data.parents):
            raise DeployError("model cache must not overlap source or data")
        config["model"] = {"enabled": True, "name": MODEL_NAME, "path": str(cache)}
    else:
        if set(model) != {"enabled"}:
            raise DeployError("disabled model accepts no name, URL, or cache settings")
        config["model"] = {"enabled": False}
    if "nodes" in raw:
        if role != "backend" or not isinstance(raw["nodes"], list) or len(raw["nodes"]) > 2:
            raise DeployError("nodes is a backend-only list of at most two explicitly trusted nodes")
        records = []
        for record in raw["nodes"]:
            if not isinstance(record, dict) or set(record) != NODE_FIELDS:
                raise DeployError("node registry requires explicit identity, public capability, key file and private transport")
            validated = validate_config({"role": "local-node", "source": str(source), "data": str(data),
                                         "APP_ORIGIN": config["APP_ORIGIN"], **record})
            if urlsplit(validated["node_endpoint"]).port not in {443, 8000, 18131, 18132}:
                raise DeployError("node endpoint is outside the existing gateway port policy")
            if os.name != "nt" and Path(validated["node_key_file"]).stat().st_uid != data.stat().st_uid:
                raise DeployError("backend node keys and data must have the same pre-provisioned owner")
            records.append({key: validated[key] for key in NODE_FIELDS})
        if len({record["NODE_ID"] for record in records}) != len(records):
            raise DeployError("duplicate node identity")
        config["nodes"] = records
    return config


def compose_literal(value):
    # Compose interpolates even JSON strings; preserve literal dollar signs.
    return value.replace("$", "$$") if isinstance(value, str) else value


def bind(path, target, read_only):
    return {"type": "bind", "source": compose_literal(path), "target": target,
            "read_only": read_only, "bind": {"create_host_path": False}}


def service_identity(config, name):
    if os.name == "nt":
        return "10001:10001"  # Preview only; ACL/VM UID mapping is not verified.
    owner = (config["node_key_file"] if name == "local-node" else
             config["data"] if name == "backend" else
             config["model"]["path"] if name == "ollama" else config["source"])
    metadata = Path(owner).stat()
    if metadata.st_uid == 0:
        raise DeployError("runtime owner must be pre-provisioned non-root; no chown is attempted")
    return str(metadata.st_uid) + ":" + str(metadata.st_gid)


def make_compose(config):
    config = validate_config(config)
    role = config["role"]
    enabled = config["model"]["enabled"]
    services = {}
    names = ["frontend", "backend"] if role == "standalone" else [role]
    for name in names:
        env = {"APP_ORIGIN": config["APP_ORIGIN"], "QA_ORIGIN": config["APP_ORIGIN"],
               "PYTHONDONTWRITEBYTECODE": "1", "HOME": "/tmp",
               "PYTHONUNBUFFERED": "1", "EXTERNAL_MODEL_MODE": "disabled",
               "EXTERNAL_MODEL_BASE_URL": "", "EXTERNAL_MODEL_API_KEY": "",
               "ALLOW_EXTERNAL_STUB": "false", "LOCAL_MODEL_BACKEND": "disabled",
               "QA_OLLAMA_URL": "", "APP_NODE_ENDPOINTS": "{}",
               "APP_PAYMENT_MODE": "disabled"}
        if name == "frontend":
            env["APP_BACKEND_URL"] = ("http://backend:8000" if role == "standalone"
                                      else config["APP_BACKEND_URL"])
        if name == "backend":
            env["APP_DB_PATH"] = "/data/application.sqlite3"
            if enabled:
                # application_hub /api/ask reuses public_qa directly. Legacy
                # LOCAL_MODEL_* and APP_MODEL_ENABLED do not wire this route.
                env["QA_OLLAMA_URL"] = "http://qa_ollama:11434"
        if name == "backend" and config.get("nodes"):
            env["APP_NODE_ENDPOINTS"] = json.dumps({record["NODE_ID"]: {
                "url": record["node_endpoint"], "secret_file": "/run/secrets/node_key_" + record["NODE_ID"]}
                for record in config["nodes"]}, sort_keys=True)
        if name == "local-node":
            env.update(NODE_ID=config["NODE_ID"], NODE_PUBLIC_CAPABILITY="classic-search",
                       NODE_CAPABILITIES="classic-search", NODE_ALLOW_RED="false",
                       NODE_SECRET_FILE="/run/secrets/node_key",
                       NODE_ENDPOINT=config["node_endpoint"], NODE_TRANSPORT=config["node_transport"])
        service = {"image": APP_IMAGE, "pull_policy": "never",
                   "command": ["uvicorn", ENTRYPOINTS[name], "--workers", "1", "--host",
                               "0.0.0.0", "--port", "8000", "--no-proxy-headers"],
                   "working_dir": "/app", "read_only": True, "user": service_identity(config, name),
                   "volumes": [bind(config["source"], "/app", True)] +
                              ([bind(config["data"], "/data", False)] if name == "backend" else []),
                   "environment": {k: compose_literal(v) for k, v in env.items()},
                   # In standalone, 64+64 MiB apps plus 768 MiB model RAM+swap
                   # total 896 MiB. A separate backend has its full 128 MiB cap.
                   "mem_limit": "128m" if name == "backend" and role != "standalone" else "64m",
                   "memswap_limit": "128m" if name == "backend" and role != "standalone" else "64m",
                   "cpus": 0.5 if name == "backend" else 0.25,
                   "cap_drop": ["ALL"], "security_opt": ["no-new-privileges:true"],
                   "tmpfs": ["/tmp:rw,noexec,nosuid,size=16m"], "restart": "no",
                   "networks": ["portable"], "logging": {"driver": "json-file",
                       "options": {"max-size": "5m", "max-file": "2"}}}
        if name == "backend" and config.get("nodes"):
            service["secrets"] = ["node_key_" + record["NODE_ID"] for record in config["nodes"]]
        if name != "backend" or role != "standalone":
            service["ports"] = [{"target": 8000, "published": str(config["host_port"]),
                                 "host_ip": config["host"], "protocol": "tcp"}]
        if name == "local-node":
            service["secrets"] = ["node_key"]
        services[name] = service
    if role == "standalone":
        services["frontend"]["depends_on"] = {"backend": {"condition": "service_started"}}
    if enabled:
        services["ollama"] = {
            "image": MODEL_IMAGE, "pull_policy": "never", "command": ["serve"],
            "user": service_identity(config, "ollama"),
            "read_only": True, "environment": {"HOME": "/tmp", "OLLAMA_HOST": "0.0.0.0:11434",
                "OLLAMA_MODELS": "/models/models", "OLLAMA_NOPRUNE": "true",
                "OLLAMA_NUM_PARALLEL": "1", "OLLAMA_MAX_LOADED_MODELS": "1",
                "OLLAMA_MAX_QUEUE": "1", "OLLAMA_CONTEXT_LENGTH": "1024"},
            "volumes": [{"type": "volume", "source": MODEL_VOLUME, "target": "/models",
                         "read_only": True, "volume": {"nocopy": True}}],
            "mem_limit": "512m", "memswap_limit": "768m", "cpus": 0.75,
            "cap_drop": ["ALL"], "security_opt": ["no-new-privileges:true"],
            "tmpfs": ["/tmp:rw,noexec,nosuid,size=16m"], "restart": "no",
            "networks": {"model_private": {"aliases": ["qa_ollama"]}},
            "logging": {"driver": "json-file", "options": {"max-size": "5m", "max-file": "2"}}}
    fingerprint = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()[:12]
    # Docker 26 internal-only publication is not a usable host listener. Apps
    # need an ordinary bridge; loopback publication is NOT an egress firewall.
    document = {"name": "daobrain-portable-" + role + "-" + fingerprint,
                "services": services, "networks": {"portable": {"driver": "bridge", "internal": False}}}
    if enabled:
        document["networks"]["model_private"] = {"driver": "bridge", "internal": True}
        services["backend"]["networks"].append("model_private")
        document["volumes"] = {MODEL_VOLUME: {"external": True, "name": MODEL_VOLUME}}
    if role == "local-node":
        document["secrets"] = {"node_key": {"file": compose_literal(config["node_key_file"])}}
    elif config.get("nodes"):
        document["secrets"] = {"node_key_" + record["NODE_ID"]: {
            "file": compose_literal(record["node_key_file"])} for record in config["nodes"]}
    return document


def budget(document):
    ram = sum(int(service["mem_limit"][:-1]) for service in document["services"].values())
    total = sum(int(service["memswap_limit"][:-1]) for service in document["services"].values())
    if ram > 900 or total > 900:
        raise DeployError("single-host RAM plus swap budget exceeds 900 MiB")
    return {"ram_mib": ram, "ram_plus_swap_mib": total,
            "required_available_mib": 700 if "ollama" in document["services"] else 350}


def require_access(path, identity, mask, field):
    # Model Linux DAC for exactly the UID and primary GID emitted to Compose.
    # Host os.access alone is insufficient; supplementary groups are NOT passed.
    metadata = path.stat()
    uid, gid = map(int, identity.split(":"))
    shift = 6 if uid == metadata.st_uid else 3 if gid == metadata.st_gid else 0
    if ((stat.S_IMODE(metadata.st_mode) >> shift) & mask) != mask:
        raise DeployError(field + " is not accessible to the configured container UID/GID")


def check_tree_access(root, identity, field):
    def failed(_error):
        raise DeployError(field + " metadata traversal failed")
    for parent, directories, files in os.walk(root, followlinks=False, onerror=failed):
        require_access(Path(parent), identity, 5, field)
        for name in directories + files:
            path = Path(parent) / name
            if path.is_symlink():
                raise DeployError(field + " symlinks are unaccepted; migrate a self-contained tree")
            metadata = path.stat()
            if stat.S_ISDIR(metadata.st_mode):
                require_access(path, identity, 5, field)
            elif stat.S_ISREG(metadata.st_mode):
                require_access(path, identity, 4, field)
            else:
                raise DeployError(field + " contains a non-regular file")


def check_source(config):
    if os.name == "nt":
        raise DeployError("Windows ACL and VM UID mapping are unverified; check/apply refused, preview is not acceptance")
    names = ["frontend", "backend"] if config["role"] == "standalone" else [config["role"]]
    root = Path(config["source"])
    shared = ["application_hub/security.py", "application_hub/store.py", "common/catalog.py",
              "common/qa.py", "common/schemas.py", "common/corpus_fixtures.py"]
    browser = ["application_hub/boundary.py", "application_hub/nodes.py", "public_qa/main.py",
               "public_qa/model.py", "public_qa/index.html"]
    for name in names:
        required = shared + [ENTRYPOINTS[name].split(".")[0] + "/main.py"]
        if name != "local-node":
            required += browser
        if name == "frontend":
            required += ["portable_frontend/index.html"]
        if name == "backend":
            required += [module + "/main.py" for module in
                         ("card_workflow", "knowledge_library", "clinical_records", "media_review", "membership")]
        for relative in required:
            module = root / relative
            if not module.is_file() or module.is_symlink() or root not in module.resolve().parents:
                raise DeployError("required application source or page is not present in source")
        check_tree_access(root, service_identity(config, name), "source")
    # Metadata only: never read an existing database, sidecar, secret or .env.
    if "backend" in names:
        data = Path(config["data"])
        identity = service_identity(config, "backend")
        require_access(data, identity, 7, "persistent data")
        if not os.access(data, os.W_OK | os.X_OK):
            raise DeployError("persistent data is not writable by the host user")
        for suffix in ("", "-wal", "-shm", "-journal"):
            db = data / ("application.sqlite3" + suffix)
            if db.is_symlink() or (db.exists() and not db.is_file()):
                raise DeployError("APP_DB_PATH and sidecars must not be symlinks or non-files")
            if db.exists():
                require_access(db, identity, 6, "persistent database")
        for record in config.get("nodes", []):
            require_access(Path(record["node_key_file"]), identity, 4, "node secret")
    if config["role"] == "local-node":
        require_access(Path(config["node_key_file"]), service_identity(config, "local-node"), 4, "node secret")
    if config["model"]["enabled"]:
        check_tree_access(Path(config["model"]["path"]), service_identity(config, "ollama"), "model cache")


def published_ports(document):
    return [(port["host_ip"], int(port["published"]))
            for service in document["services"].values() for port in service.get("ports", [])]


def check_host_ports(document):
    # Bind only, without reuse/listen/connect or DNS. The reservation must be
    # released before Docker up, so a residual race is explicitly not accepted.
    for host, port in published_ports(document):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                if os.name == "nt":
                    probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                probe.bind((host, port))
        except OSError:
            raise DeployError("loopback host port is occupied or cannot be reserved; no service was stopped") from None


def check_engine_ports(docker, env, document):
    # Docker can reserve a published port even when no host listener exists.
    # Inspect running containers, not merely host sockets; never stop/recreate.
    ids = run_command(docker + ["container", "ls", "--quiet"], env).splitlines()
    if not ids:
        return
    if any(not re.fullmatch(r"[0-9a-f]{12,64}", value) for value in ids):
        raise DeployError("running container inventory is invalid")
    for offset in range(0, len(ids), 64):
        records = run_command(docker + ["container", "inspect"] + ids[offset:offset + 64] +
                              ["--format", "{{json .NetworkSettings.Ports}}"], env).splitlines()
        if len(records) != len(ids[offset:offset + 64]):
            raise DeployError("running container port inventory is incomplete")
        for record in records:
            mapping = strict_json(record)
            if mapping is None:
                continue
            if not isinstance(mapping, dict):
                raise DeployError("running container port mapping is invalid")
            for target, bindings in mapping.items():
                if not re.fullmatch(r"[0-9]+/(tcp|udp|sctp)", target):
                    raise DeployError("running container port mapping is invalid")
                if bindings is None:
                    continue
                if not isinstance(bindings, list):
                    raise DeployError("running container port bindings are invalid")
                for binding in bindings:
                    try:
                        address = ipaddress.ip_address(binding["HostIp"] or "0.0.0.0")
                        port_text = binding["HostPort"]
                        if not isinstance(port_text, str) or not re.fullmatch(r"[0-9]{1,5}", port_text):
                            raise ValueError
                        port = int(port_text)
                        if not 1 <= port <= 65535:
                            raise ValueError
                    except (KeyError, TypeError, ValueError):
                        raise DeployError("running container host binding is invalid") from None
                    if target.endswith("/tcp") and any(port == candidate and
                            (address.is_unspecified or str(address) == host)
                            for host, candidate in published_ports(document)):
                        raise DeployError("Docker has already reserved the host port; existing containers are not replaced")


def verify_model_cache(config):
    if not config["model"]["enabled"]:
        return
    root = Path(config["model"]["path"])
    manifest = root / "models/manifests/registry.ollama.ai/library/qwen2.5/0.5b"
    try:
        if not manifest.is_file() or manifest.is_symlink() or root not in manifest.resolve().parents:
            raise DeployError("preinstalled model manifest is missing or escapes cache")
        if manifest.stat().st_size > MIB:
            raise DeployError("model manifest is oversized")
        data = strict_json(manifest.read_bytes())
        if not isinstance(data, dict) or data.get("schemaVersion") != 2:
            raise DeployError("invalid preinstalled model manifest")
        layers = data.get("layers")
        if not isinstance(layers, list) or not 1 <= len(layers) <= 64:
            raise DeployError("invalid model layer inventory")
        descriptors = [data.get("config")] + layers
        if not any(isinstance(d, dict) and d.get("mediaType") == "application/vnd.ollama.image.model"
                   for d in layers):
            raise DeployError("model weights are absent")
        for item in descriptors:
            if (not isinstance(item, dict) or not isinstance(item.get("digest"), str)
                    or not re.fullmatch(r"sha256:[0-9a-f]{64}", item["digest"])
                    or type(item.get("size")) is not int or not 0 < item["size"] <= 600 * MIB):
                raise DeployError("invalid model blob descriptor")
            blob = root / "models/blobs" / item["digest"].replace(":", "-")
            if (not blob.is_file() or blob.is_symlink() or root not in blob.resolve().parents
                    or blob.stat().st_size != item["size"]):
                raise DeployError("preinstalled model blob is missing or has wrong size")
            digest = hashlib.sha256()
            with blob.open("rb") as stream:
                for chunk in iter(lambda: stream.read(MIB), b""):
                    digest.update(chunk)
            if digest.hexdigest() != item["digest"].split(":")[1]:
                raise DeployError("preinstalled model blob digest mismatch")
    except OSError:
        raise DeployError("preinstalled model cache is not locally verifiable") from None


def run_command(argv, env=None):
    try:
        result = subprocess.run(argv, shell=False, check=False, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, universal_newlines=True, timeout=60, env=env)
    except (OSError, subprocess.SubprocessError):
        raise DeployError("local prerequisite command failed; no software was installed") from None
    if result.returncode:
        raise DeployError("local prerequisite or Compose command failed; output suppressed")
    return result.stdout.strip()


def host_available_mib():
    system = platform.system()
    if system == "Linux":
        try:
            for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
        except (OSError, ValueError, IndexError):
            pass
    elif system == "Darwin":
        output = run_command(["/usr/bin/vm_stat"])
        page = re.search(r"page size of (\d+) bytes", output)
        counts = [re.search(r"Pages " + name + r":\s+(\d+)\.", output)
                  for name in ("free", "inactive", "speculative")]
        if page and all(counts):
            return int(page.group(1)) * sum(int(c.group(1)) for c in counts) // MIB
    elif system == "Windows":
        class MemoryStatus(ctypes.Structure):
            _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong)] + [
                (name, ctypes.c_ulonglong) for name in ("total", "available", "page_total",
                    "page_available", "virtual_total", "virtual_available", "extended")]
        status = MemoryStatus()
        status.length = ctypes.sizeof(status)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return status.available // MIB
    raise DeployError("host available memory cannot be measured; fail closed")


def local_docker(env):
    endpoint = env.get("DOCKER_HOST", "")
    if endpoint or env.get("DOCKER_TLS_VERIFY") or env.get("DOCKER_CERT_PATH"):
        raise DeployError("DOCKER_HOST/TLS overrides are not accepted; use an installed local context")
    executable = shutil.which("docker")
    if not executable:
        raise DeployError("Docker is not installed; install/provision it separately")
    context = env.get("DOCKER_CONTEXT") or run_command([executable, "context", "show"], env)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", context):
        raise DeployError("invalid Docker context")
    endpoint = run_command([executable, "context", "inspect", context, "--format",
                            '{{(index .Endpoints "docker").Host}}'], env)
    if not (re.fullmatch(r"unix:///[^\r\n]+", endpoint)
            or re.fullmatch(r"npipe:////\./pipe/[A-Za-z0-9_-]+", endpoint)):
        raise DeployError("remote Docker contexts are forbidden; require a local socket or named pipe")
    return [executable, "--context", context]


def write_compose(document, work_dir):
    directory = existing_path(str(work_dir), "work_dir")
    path = directory / ("compose.portable." + uuid.uuid4().hex + ".json")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(str(path), flags, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(document, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
    except OSError:
        raise DeployError("exclusive Compose JSON creation failed; no existing file was replaced") from None
    return path


def assert_saved_plan(path, document):
    try:
        if path.is_symlink() or not path.is_file() or strict_json(path.read_bytes()) != document:
            raise DeployError("saved Compose snapshot differs from the allowlisted plan")
    except OSError:
        raise DeployError("saved Compose snapshot cannot be verified") from None


def preflight(config, document, path, available=None):
    config = validate_config(config)
    if document != make_compose(config):
        raise DeployError("Compose document differs from the allowlisted plan")
    assert_saved_plan(path, document)
    check_source(config)
    verify_model_cache(config)
    check_host_ports(document)
    limits = budget(document)
    memory = host_available_mib() if available is None else available()
    if type(memory) is not int or memory < limits["required_available_mib"]:
        raise DeployError("insufficient host available memory (350/700 MiB required)")
    # Child-process-only isolation: never mutate os.environ or host settings.
    env = {k: v for k, v in os.environ.items() if not k.startswith("COMPOSE_")}
    docker = local_docker(env)
    template = ('{"os":{{json .OSType}},"arch":{{json .Architecture}},'
                '"memory":{{json .MemTotal}}}')
    info = strict_json(run_command(docker + ["info", "--format", template], env))
    if not isinstance(info, dict) or info.get("os") != "linux":
        raise DeployError("a local Linux-container Docker engine is mandatory")
    architectures = {"amd64": "amd64", "x86_64": "amd64", "arm64": "arm64", "aarch64": "arm64"}
    architecture = architectures.get(info.get("arch")) if isinstance(info.get("arch"), str) else None
    if architecture is None:
        raise DeployError("Docker engine architecture is unsupported or unverified")
    if type(info.get("memory")) is not int or info["memory"] <= 0:
        raise DeployError("Docker VM memory ceiling cannot be verified")
    if info["memory"] < limits["ram_mib"] * MIB:
        raise DeployError("Docker VM memory ceiling is below the service RAM budget")
    version = run_command(docker + ["compose", "version", "--short"], env)
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)(?:[-+][A-Za-z0-9_.-]+)?", version)
    if not match or tuple(map(int, match.groups())) < (2, 24, 0):
        raise DeployError("installed Docker Compose v2.24+ is required; no installation attempted")
    images = [APP_IMAGE] + ([MODEL_IMAGE] if config["model"]["enabled"] else [])
    for image in images:
        template = ('{"os":{{json .Os}},"arch":{{json .Architecture}},'
                    '"variant":{{json .Variant}},"entrypoint":{{json .Config.Entrypoint}}}')
        metadata = strict_json(run_command(docker + ["image", "inspect", image, "--format", template], env))
        if not isinstance(metadata, dict) or metadata.get("os") != "linux":
            raise DeployError("preinstalled Linux image is required; no build or pull attempted")
        if (metadata.get("arch") != architecture or
                metadata.get("variant") not in (("", "v8") if architecture == "arm64" else ("",))):
            raise DeployError("preinstalled image architecture does not match native Docker engine; emulation is unaccepted")
        entrypoint = metadata.get("entrypoint")
        if (image == APP_IMAGE and entrypoint not in (None, []) or
                image == MODEL_IMAGE and entrypoint != ["/bin/ollama"]):
            raise DeployError("preinstalled image entrypoint differs from the fixed application/runner wiring")
    check_engine_ports(docker, env, document)
    if config["model"]["enabled"]:
        template = ('{"driver":{{json .Driver}},"mountpoint":{{json .Mountpoint}},'
                    '"type":{{json (index .Options "type")}},"options":{{json (index .Options "o")}},'
                    '"device":{{json (index .Options "device")}}}')
        volume = strict_json(run_command(docker + ["volume", "inspect", MODEL_VOLUME,
                                                   "--format", template], env))
        if not isinstance(volume, dict) or volume.get("driver") != "local":
            raise DeployError("model volume must already exist with the local driver")
        backed = volume.get("mountpoint")
        if volume.get("device"):
            if volume.get("type") != "none" or volume.get("options") not in ("bind", "bind,ro", "ro,bind"):
                raise DeployError("model volume must be a pre-existing local bind, not a network mount")
            backed = volume["device"]
        if not isinstance(backed, str) or existing_path(backed, "model volume") != Path(config["model"]["path"]):
            raise DeployError("model volume does not match the verified local cache; VM-only cache is unverified")
    command = docker + ["compose", "--env-file", os.devnull, "--project-name", document["name"],
                        "--project-directory", str(path.parent), "-f", str(path)]
    # Validate the exact saved snapshot, with no .env or profile inheritance.
    run_command(command + ["config", "--quiet"], env)
    assert_saved_plan(path, document)
    return command, env


def execute(config, mode="plan", work_dir=None, available=None):
    if mode not in ("plan", "check", "apply"):
        raise DeployError("unsupported execution mode")
    config = validate_config(config)
    document = make_compose(config)
    if os.name != "nt":
        check_source(config)
    result = {"mode": mode, "role": config["role"], "services": list(document["services"]),
              "APP_ORIGIN": config["APP_ORIGIN"], "QA_ORIGIN": config["APP_ORIGIN"],
              "listen_host": config["host"], "host_port": config["host_port"],
              "budget": budget(document), "containers_started": False,
              "deployment_accepted": False, "host_listener_verified": False,
              "private_tls_verified": False, "qa_verified": False,
              "qa_mode": "preinstalled_runner_unverified" if config["model"]["enabled"] else "disabled_fail_closed",
              "notice": "Linux role simulation is not Windows real-machine acceptance; no TLS/tunnel or host software is provisioned. "
                        "Apps use an ordinary bridge with IPv4 loopback publication, NOT an egress firewall. "
                        "Private TLS routes, certificate trust, origin/Host-preserving ingress, actual listeners and business/QA readiness are unverified. "
                        "Host-loopback tunnels, public/DNS upstreams and IPv6 publication/upstream routes are refused. "
                        "Permissions are host-DAC metadata checks, not proof of Docker Desktop VM ownership mapping. "
                        "Windows ACL/VM UID mapping cannot pass check/apply. Port checks have a residual race before up."}
    if mode == "plan":
        return result
    path = write_compose(document, Path.cwd() if work_dir is None else work_dir)
    result["compose_file"] = str(path)
    command, env = preflight(config, document, path, available)
    result["preflight"] = "passed"
    if mode == "apply":
        # All targets come from validated roles. No dependencies outside this list,
        # no builds/pulls, no old-project shutdown, no orphan or volume cleanup.
        targets = list(document["services"])
        assert_saved_plan(path, document)
        if make_compose(config) != document:
            raise DeployError("runtime ownership changed after preflight; apply refused")
        check_source(config)
        verify_model_cache(config)
        check_host_ports(document)
        # local_docker always emits [executable, --context, context_name]; the
        # context itself may be named 'compose', so do not search by that word.
        check_engine_ports(command[:3], env, document)
        run_command(command + ["up", "-d", "--no-build", "--pull", "never", "--no-deps"] + targets, env)
        result["containers_started"] = True
        result["notice"] += " Compose up succeeded; business readiness and real-machine acceptance are not verified."
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="existing strict JSON configuration file")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--plan", action="store_true", help="default; validate and print plan only")
    actions.add_argument("--check", action="store_true", help="local preflight only; no containers started")
    actions.add_argument("--apply", action="store_true", help="preflight then start only allowlisted services")
    parser.add_argument("--work-dir", help="existing absolute directory for exclusive Compose JSON")
    args = parser.parse_args(argv)
    try:
        config_path = existing_path(args.config, "config", directory=False)
        if config_path.stat().st_size > MIB:
            raise DeployError("configuration file is oversized")
        raw = strict_json(config_path.read_bytes())
        mode = "apply" if args.apply else "check" if args.check else "plan"
        result = execute(raw, mode, args.work_dir)
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    except (DeployError, OSError):
        # Do not reveal filenames from OS errors, subprocess output, or input values.
        error = sys.exc_info()[1]
        print("portable deployment refused: " + (str(error) if isinstance(error, DeployError)
                                                else "local file access failed"), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
