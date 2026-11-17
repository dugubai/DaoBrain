"""Authenticated, bounded internal HTTP protocol (stdlib, Python 3.6+).

Every method has a JSON body, including GET. Tokens exist only in that signed
body. This transport is host-internal, not a browser-facing HTTP interface.
"""
import hashlib
import hmac
import ipaddress
import json
import math
import os
import re
import stat
import time
import uuid
from urllib import error, parse, request

from .registry import ControlError, canonical

MAX_BODY = 32768
MAX_RESPONSE = 262144
WINDOW = 30
HEX32 = re.compile(r"^[0-9a-f]{32}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
TOKEN = re.compile(r"^[A-Za-z0-9_-]{43}$")


def strict_json(data, limit=MAX_BODY):
    if not isinstance(data, bytes) or not data or len(data) > limit:
        raise ControlError("invalid JSON size", 413)

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    def constant(value):
        raise ValueError("non-finite number")

    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=pairs,
                           parse_constant=constant)
        pending = [(value, 0)]
        while pending:
            item, depth = pending.pop()
            if depth > 32:
                raise ValueError("JSON depth")
            if isinstance(item, float) and not math.isfinite(item):
                raise ValueError("number overflow")
            if isinstance(item, str):
                item.encode("utf-8")  # Reject lone surrogate escapes.
            if isinstance(item, dict):
                pending.extend((child, depth + 1) for child in item.values())
                pending.extend((key, depth + 1) for key in item)
            elif isinstance(item, list):
                pending.extend((child, depth + 1) for child in item)
        if not isinstance(value, dict):
            raise ValueError("object required")
        return value
    except (ValueError, UnicodeError, RecursionError, OverflowError):
        raise ControlError("invalid strict JSON", 400)


def private_file(path, limit, minimum=0):
    """Open a trusted owner-only regular file, without following symlinks."""
    if not isinstance(path, str) or not os.path.isabs(path):
        raise ControlError("absolute private file required", 503)
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | os.O_NONBLOCK
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or info.st_mode & 0o077 or info.st_nlink != 1
                    or info.st_size < minimum or info.st_size > limit):
                raise ControlError("invalid private file permissions or size", 503)
            data = stream.read(limit + 1)
        if not minimum <= len(data) <= limit:
            raise ControlError("invalid private file size", 503)
        return data
    except OSError:
        raise ControlError("private file unavailable", 503)


def load_key(path):
    return private_file(path, 4096, 32)


def route(method, path):
    if method == "GET" and path in ("/v1/status", "/v1/jobs"):
        return path
    if method == "POST" and path in ("/v1/actions", "/v1/jobs"):
        return path
    if method == "GET" and re.fullmatch(r"/v1/jobs/[0-9a-f]{32}", path):
        return "/v1/jobs/{id}"
    raise ControlError("unknown route", 404)


def signature(key, fields):
    message = "\n".join(str(field) for field in fields).encode("ascii")
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def body_hash(body):
    return hashlib.sha256(body).hexdigest()


def request_headers(key, method, path, body, timestamp=None, nonce=None,
                    request_id=None):
    route(method, path)
    stamp = str(int(time.time()) if timestamp is None else timestamp)
    nonce = nonce or uuid.uuid4().hex
    request_id = request_id or uuid.uuid4().hex
    return {"Content-Type": "application/json", "X-Control-Timestamp": stamp,
            "X-Control-Nonce": nonce, "X-Control-Request-ID": request_id,
            "X-Control-Signature": signature(key, (
                "request-v1", method, path, body_hash(body), stamp, nonce, request_id))}


def verify_request(key, method, path, body, headers, now=None):
    stamp = headers.get("X-Control-Timestamp", "")
    nonce = headers.get("X-Control-Nonce", "")
    request_id = headers.get("X-Control-Request-ID", "")
    supplied = headers.get("X-Control-Signature", "")
    if (not re.fullmatch(r"[0-9]{1,12}", stamp) or not HEX32.fullmatch(nonce)
            or not HEX32.fullmatch(request_id) or not HEX64.fullmatch(supplied)):
        raise ControlError("invalid request authentication", 401)
    now = time.time() if now is None else now
    if abs(now - int(stamp)) > WINDOW:
        raise ControlError("expired request", 401)
    # Avoid hashing attacker-controlled unbounded bodies even outside HTTPServer.
    if len(body) > MAX_BODY:
        raise ControlError("request too large", 413)
    try:
        expected = signature(key, ("request-v1", method, path, body_hash(body),
                                   stamp, nonce, request_id))
    except (UnicodeError, ValueError):
        raise ControlError("invalid request authentication", 401)
    if not hmac.compare_digest(supplied, expected):
        raise ControlError("invalid request authentication", 401)
    return request_id, nonce


def response_headers(key, request_id, nonce, status, body):
    return {"Content-Type": "application/json", "X-Control-Request-ID": request_id,
            "X-Control-Nonce": nonce,
            "X-Control-Signature": signature(key, (
                "response-v1", request_id, nonce, status, body_hash(body)))}


def verify_response(key, request_id, nonce, status, body, headers):
    supplied = headers.get("X-Control-Signature", "")
    if (len(body) > MAX_RESPONSE or headers.get("X-Control-Request-ID") != request_id
            or headers.get("X-Control-Nonce") != nonce or not HEX64.fullmatch(supplied)):
        raise ControlError("invalid controller response", 502)
    expected = signature(key, ("response-v1", request_id, nonce, status, body_hash(body)))
    if not hmac.compare_digest(supplied, expected):
        raise ControlError("invalid controller response", 502)


def controller_url(url):
    try:
        parsed = parse.urlsplit(url)
        host = parsed.hostname
        if (parsed.scheme != "http" or not host
                or not isinstance(ipaddress.ip_address(host), ipaddress.IPv4Address)
                or not ipaddress.ip_address(host).is_loopback
                or parsed.username is not None or parsed.password is not None
                or parsed.port is None or not 1 <= parsed.port <= 65535
                or parsed.path or parsed.query or parsed.fragment
                or parsed.netloc != "{}:{}".format(host, parsed.port)
                or url != "http://" + parsed.netloc):
            raise ValueError("invalid endpoint")
        return url
    except (TypeError, ValueError):
        raise ControlError("explicit IPv4 loopback http controller URL required", 503)


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class ControllerClient:
    def __init__(self, url=None, key_file=None, timeout=10, opener=None):
        self.url = controller_url(url if url is not None else
                                  os.environ.get("CONTROL_CONTROLLER_URL", ""))
        self.key = load_key(key_file if key_file is not None else
                            os.environ.get("CONTROL_KEY_FILE", ""))
        if not isinstance(timeout, (int, float)) or not 0 < timeout <= 180:
            raise ControlError("invalid controller timeout")
        self.timeout = timeout
        # Disable ambient HTTP_PROXY/ALL_PROXY and never follow redirects.
        self.opener = opener or request.build_opener(request.ProxyHandler({}), NoRedirect())

    def request(self, method, path, payload, token):
        route(method, path)
        if (not isinstance(payload, dict) or "token" in payload
                or not isinstance(token, str) or not TOKEN.fullmatch(token)):
            raise ControlError("invalid internal request")
        data = dict(payload)
        data["token"] = token
        try:
            body = canonical(data)
        except (ValueError, TypeError, UnicodeError):
            raise ControlError("invalid internal payload")
        strict_json(body)
        headers = request_headers(self.key, method, path, body)
        req = request.Request(self.url + path, data=body, headers=headers, method=method)
        try:
            try:
                response = self.opener.open(req, timeout=self.timeout)
            except error.HTTPError as exc:
                response = exc  # Error responses require authentication too.
            with response:
                result = response.read(MAX_RESPONSE + 1)
                status = response.code
                verify_response(self.key, headers["X-Control-Request-ID"],
                                headers["X-Control-Nonce"], status, result, response.headers)
                value = strict_json(result, MAX_RESPONSE)
            if not 200 <= status < 300:
                # Only fixed readiness reasons may cross into the browser API.
                safe_errors = {"isolated builder not provisioned", "not_enrolled",
                               "trusted test fixture not provisioned",
                               "trusted focused test fixture not provisioned"}
                detail = value.get("error")
                if not isinstance(detail, str) or detail not in safe_errors:
                    detail = "controller request rejected"
                raise ControlError(detail, status)
            return value
        except ControlError:
            raise
        except (OSError, error.URLError, ValueError):
            raise ControlError("controller unavailable", 503)
