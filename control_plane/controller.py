"""Host-only controller. Run with python -m control_plane.controller.

CONTROL_DB_PATH is the separately provisioned management store; a distinct
owner-only controller.db alongside it holds replay/job/lease state. Host flock prevents two
controllers. A crash leaves an unresolved persistent lease: it is NEVER stolen
on timeout, because an orphaned sandbox may still be alive. Host review must stop
that owned container and explicitly clear the lease before new execution.
"""
import contextlib
import fcntl
import importlib
import os
import re
from http.client import LineTooLong
import sqlite3
import stat
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

from .protocol import (HEX32, HEX64, MAX_BODY, MAX_RESPONSE, TOKEN, load_key,
                       response_headers, route, strict_json, verify_request)
from .registry import ControlError, canonical, component, digest, validate_config
from .runner import Runner


def private_parent(path):
    if (not isinstance(path, str) or not os.path.isabs(path)
            or os.path.realpath(os.path.dirname(path)) != os.path.dirname(path)):
        raise ControlError("absolute fixed database path required", 503)
    try:
        parent = os.stat(os.path.dirname(path))
        if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.geteuid() or parent.st_mode & 0o077:
            raise ControlError("owner-only database parent required", 503)
    except OSError:
        raise ControlError("database parent unavailable", 503)


def owned_fd(path):
    private_parent(path)
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_nlink != 1 or info.st_mode & 0o077):
            os.close(fd)
            raise ControlError("unsafe controller state file", 503)
        return fd
    except OSError:
        raise ControlError("controller state unavailable", 503)


class ControllerState:
    """Durable nonce dedup and concurrency lease; no raw credentials or stdout."""
    def __init__(self, path, now=None):
        self.path = path
        self.now = now or time.time
        self._closed = False
        self._lock_fd = owned_fd(path + ".lock")
        try:
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self._lock_fd)
            raise ControlError("controller already active", 409)
        try:
            fd = owned_fd(path)
            os.close(fd)
            with self.transaction() as db:
                db.executescript("""
                    CREATE TABLE IF NOT EXISTS nonces (
                        nonce TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL,
                        accepted_at INTEGER NOT NULL);
                    CREATE TABLE IF NOT EXISTS jobs (
                        id TEXT PRIMARY KEY, actor_id TEXT NOT NULL,
                        workspace_id TEXT NOT NULL, source_hash TEXT NOT NULL,
                        kind TEXT NOT NULL, status TEXT NOT NULL,
                        created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
                        container_id TEXT, result BLOB);
                    CREATE TABLE IF NOT EXISTS lease (
                        singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                        owner TEXT NOT NULL, deadline INTEGER NOT NULL,
                        container_id TEXT);
                """)
                db.execute("UPDATE jobs SET status='interrupted', updated_at=? "
                           "WHERE status IN ('queued','running')", (int(self.now()),))
        except Exception:
            self.close()
            raise

    @contextlib.contextmanager
    def transaction(self):
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        try:
            db.execute("PRAGMA busy_timeout=5000")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            yield db
            if db.in_transaction:
                db.commit()
        except Exception:
            if db.in_transaction:
                db.rollback()
            raise
        finally:
            db.close()

    def accept(self, nonce, request_id):
        try:
            with self.transaction() as db:
                # Permanent dedup: no restart/replay window caused by pruning.
                db.execute("INSERT INTO nonces VALUES(?,?,?)", (nonce, request_id, int(self.now())))
        except sqlite3.IntegrityError:
            raise ControlError("request replay rejected", 409)

    def acquire(self, owner, duration=210):
        with self.transaction() as db:
            if db.execute("SELECT owner FROM lease WHERE singleton=1").fetchone():
                raise ControlError("execution busy or host recovery required", 409)
            db.execute("INSERT INTO lease VALUES(1,?,?,NULL)", (owner, int(self.now()) + duration))

    def renew(self, owner, duration=210):
        with self.transaction() as db:
            result = db.execute("UPDATE lease SET deadline=? WHERE singleton=1 AND owner=?",
                                (int(self.now()) + duration, owner))
            if result.rowcount != 1:
                raise ControlError("execution lease lost", 503)

    def release(self, owner):
        with self.transaction() as db:
            db.execute("DELETE FROM lease WHERE singleton=1 AND owner=?", (owner,))

    def busy(self):
        with self.transaction() as db:
            row = db.execute("SELECT owner, deadline FROM lease WHERE singleton=1").fetchone()
            return {"active": bool(row), "recovery_required": bool(row and row[1] < self.now())}

    def create_job(self, actor, workspace_id, source_hash, kind):
        job_id = uuid.uuid4().hex
        with self.transaction() as db:
            db.execute("INSERT INTO jobs VALUES(?,?,?,?,?,'queued',?,?,NULL,NULL)",
                       (job_id, str(actor), workspace_id, source_hash, kind,
                        int(self.now()), int(self.now())))
        return job_id

    def running(self, job_id):
        with self.transaction() as db:
            result = db.execute("UPDATE jobs SET status='running', updated_at=? WHERE id=? AND status='queued'",
                                (int(self.now()), job_id))
            if result.rowcount != 1:
                raise ControlError("job is not executable", 409)

    def container(self, job_id, container_id):
        if not HEX64.fullmatch(container_id):
            raise ControlError("invalid internal container id", 503)
        with self.transaction() as db:
            db.execute("UPDATE jobs SET container_id=?, updated_at=? WHERE id=?",
                       (container_id, int(self.now()), job_id))
            db.execute("UPDATE lease SET container_id=? WHERE owner=?", (container_id, job_id))

    def finish(self, job_id, status, metadata):
        if status not in ("succeeded", "failed", "timed_out", "interrupted"):
            raise ControlError("invalid internal terminal state", 503)
        if type(metadata) is not dict:
            raise ControlError("invalid internal job metadata", 503)
        # Whitelist metadata; no arbitrary errors, tokens, source paths or stdout.
        allowed = {"exit_code", "oom_killed", "output_bytes", "output_truncated",
                   "output_retained", "metadata_limit", "reason"}
        artifacts = {"artifact_sha256", "source_hash", "artifact_bytes", "artifact_files",
                     "kind", "image_built", "release_approved"}
        clean = {key: value for key, value in metadata.items() if key in allowed | artifacts}
        for key in ("oom_killed", "output_truncated", "output_retained"):
            if key in clean and type(clean[key]) is not bool:
                raise ControlError("invalid internal job metadata", 503)
        for key, minimum, maximum in (("exit_code", -(2 ** 31), 2 ** 31 - 1),
                                      ("output_bytes", 0, 2 ** 63 - 1),
                                      ("metadata_limit", 0, 16384)):
            if key not in clean or (key == "exit_code" and clean[key] is None):
                continue
            if type(clean[key]) is not int or not minimum <= clean[key] <= maximum:
                raise ControlError("invalid internal job metadata", 503)
        if "reason" in clean and (clean["reason"] is not None and (
                type(clean["reason"]) is not str or clean["reason"] not in (
                    "execution rejected", "host recovery required"))):
            raise ControlError("invalid internal job metadata", 503)
        with self.transaction() as db:
            job = db.execute("SELECT source_hash,kind FROM jobs WHERE id=?", (job_id,)).fetchone()
            if job is None:
                raise ControlError("invalid internal job", 503)
            if job[1] == "build" and status == "succeeded":
                # Artifact evidence cannot override an incomplete or failed execution.
                if not (allowed - {"reason"}).issubset(clean):
                    raise ControlError("incomplete build execution metadata", 503)
                if (type(clean["exit_code"]) is not int or clean["exit_code"] != 0
                        or clean["oom_killed"] is not False or clean["metadata_limit"] != 16384
                        or clean.get("reason") is not None):
                    raise ControlError("invalid build execution metadata", 503)
                # A ZIP digest is source-artifact evidence, NEVER image/release approval.
                if not artifacts.issubset(clean) or "output_retained" not in clean:
                    raise ControlError("incomplete build artifact metadata", 503)
                for key in ("artifact_sha256", "source_hash"):
                    if type(clean[key]) is not str or not HEX64.fullmatch(clean[key]):
                        raise ControlError("invalid build artifact metadata", 503)
                for key, maximum in (("artifact_bytes", 12 * 1024 * 1024), ("artifact_files", 512)):
                    if type(clean[key]) is not int or not 1 <= clean[key] <= maximum:
                        raise ControlError("invalid build artifact metadata", 503)
                if (type(clean["kind"]) is not str or clean["kind"] != "offline-source-artifact"
                        or clean["image_built"] is not False or clean["release_approved"] is not False
                        or clean["output_retained"] is not False or clean["source_hash"] != job[0]):
                    raise ControlError("invalid build artifact binding", 503)
            elif artifacts.intersection(clean):
                raise ControlError("artifact metadata requires succeeded build", 503)
            result = canonical(clean)
            if len(result) > 16384:
                raise ControlError("job metadata limit", 503)
            db.execute("UPDATE jobs SET status=?, updated_at=?, result=? WHERE id=? AND status IN ('queued','running')",
                       (status, int(self.now()), result, job_id))

    def jobs(self, actor_id, job_id=None):
        with self.transaction() as db:
            columns = "id,workspace_id,source_hash,kind,status,created_at,updated_at,result"
            if job_id is None:
                rows = db.execute("SELECT " + columns + " FROM jobs WHERE actor_id=? ORDER BY created_at DESC,id LIMIT 100",
                                  (str(actor_id),)).fetchall()
            else:
                rows = db.execute("SELECT " + columns + " FROM jobs WHERE actor_id=? AND id=?",
                                  (str(actor_id), job_id)).fetchall()
        output = []
        for row in rows:
            result = dict(zip(("id", "workspace_id", "source_hash", "kind", "status", "created_at", "updated_at"), row[:7]))
            result["result"] = strict_json(bytes(row[7]), 16384) if row[7] else None
            output.append(result)
        if job_id is not None:
            if not output:
                raise ControlError("job not found", 404)
            return output[0]
        return output

    def close(self):
        if not self._closed:
            self._closed = True
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            os.close(self._lock_fd)


class Controller:
    def __init__(self, key, state, security, store, workspace, runner=None):
        self.key, self.state = key, state
        self.security, self.store, self.workspace = security, store, workspace
        self.runner = runner or Runner()
        self._tokens = {}  # Live raw credentials ONLY here; never state/store/log.
        self._worker = None
        self._worker_lock = threading.Lock()
        self._closing = threading.Event()

    def authenticate(self, token, permission):
        if not isinstance(token, str) or not TOKEN.fullmatch(token):
            raise ControlError("authentication required", 401)
        try:
            principal = self.security.authenticate(token, permission)
        except ControlError as exc:
            raise ControlError("authentication or permission rejected", 403 if exc.status == 403 else 401)
        if principal is None or not getattr(principal, "id", None):
            raise ControlError("authentication required", 401)
        return principal

    def _authorize_action(self, key, expected, action, token, actor_id):
        # Short fresh transaction: identity/grants and active policy are checked
        # together, without holding the management writer lock during Docker I/O.
        with self.store.transaction() as db:
            if callable(getattr(self.security, "_authenticate", None)) and isinstance(db, sqlite3.Connection):
                try:
                    actor = self.security._authenticate(db, token, "operate")
                except ControlError as exc:
                    raise ControlError("authentication or permission rejected", 403 if exc.status == 403 else 401)
                row = db.execute("SELECT r.* FROM cp_active a JOIN cp_revisions r ON r.id=a.revision "
                                 "WHERE a.component=?", (key,)).fetchone()
                policy = None
                if row:
                    config = validate_config(key, strict_json(row["config"].encode("utf-8")))
                    if (row["state"] != "activated" or not row["reviewer"] or row["reviewer"] == row["owner"]
                            or digest(config) != row["hash"]):
                        raise ControlError("active policy integrity failure", 503)
                    policy = {"config": config, "hash": row["hash"]}
            else:
                policy = self.store.active_policy(key)
                if isinstance(policy, dict) and "config" not in policy:
                    policy = {"hash": policy.get("hash"),
                              "config": {"enabled": policy.get("enabled"),
                                         "request_timeout_s": policy.get("request_timeout_s")}}
                actor = self.authenticate(token, "operate")
            self._check_policy(key, policy, expected, action)
            if str(actor.id) != str(actor_id):
                raise ControlError("principal changed", 401)

    @contextlib.contextmanager
    def action_policy(self, key, expected, action, token, actor_id):
        self._authorize_action(key, expected, action, token, actor_id)

        def authorize():
            if self._closing.is_set():
                raise ControlError("controller shutting down", 503)
            self._authorize_action(key, expected, action, token, actor_id)

        # Final admission observes revocations/policy changes committed during
        # preflight. Once admitted, an issued Docker action is NOT cancellable by
        # later revocation; no promise of atomic cross-database/daemon cancellation.
        yield authorize

    def handle(self, method, path, body, headers):
        request_id = headers.get("X-Control-Request-ID", "")
        nonce = headers.get("X-Control-Nonce", "")
        request_id = request_id if HEX32.fullmatch(request_id) else "0" * 32
        nonce = nonce if HEX32.fullmatch(nonce) else "0" * 32
        try:
            verified_id, verified_nonce = verify_request(self.key, method, path, body, headers)
            self.state.accept(verified_nonce, verified_id)
            route_name = route(method, path)
            payload = strict_json(body)
            status, value = self.dispatch(method, path, route_name, payload)
        except ControlError as exc:
            status = exc.status
            value = {"error": exc.detail}
        except Exception:
            status, value = 503, {"error": "controller operation unavailable"}
        try:
            response = canonical(value)
            if len(response) > MAX_RESPONSE:
                raise ValueError("response too large")
        except (ValueError, TypeError, UnicodeError):
            status, response = 503, b'{"error":"controller response unavailable"}'
        return status, response, response_headers(self.key, request_id, nonce, status, response)

    @staticmethod
    def fields(payload, expected):
        if set(payload) != set(expected):
            raise ControlError("invalid route fields")

    def dispatch(self, method, path, route_name, payload):
        token = payload.get("token")
        permission = "operate" if path == "/v1/actions" else (
            "develop" if method == "POST" and path == "/v1/jobs" else "view")
        actor = self.authenticate(token, permission)  # Live enabled/expiry/grants on accept.
        if path == "/v1/status":
            self.fields(payload, ("token",))
            value = dict(self.runner.status())
            value["controller"] = {"execution": self.state.busy()}
            return 200, value
        if method == "GET":
            self.fields(payload, ("token",))
            if route_name == "/v1/jobs/{id}":
                return 200, self.state.jobs(actor.id, path.rsplit("/", 1)[1])
            return 200, {"jobs": self.state.jobs(actor.id)}
        if path == "/v1/actions":
            self.fields(payload, ("token", "component", "action", "expected_config_hash"))
            item = component(payload["component"])
            action = payload["action"]
            expected = payload["expected_config_hash"]
            if (not item["runtime"] or action not in ("start", "stop", "restart")
                    or not isinstance(expected, str) or not HEX64.fullmatch(expected)):
                raise ControlError("invalid enrolled action")
            owner = uuid.uuid4().hex
            self.state.acquire(owner)
            safe_to_release = True
            try:
                with self.heartbeat(owner):
                    # Initial check, unlocked preflight, then fresh final admission.
                    # This checks control policy, not application configuration.
                    with self.action_policy(item["id"], expected, action, token, actor.id) as authorize:
                        safe_to_release = False
                        observed = self.runner.action(item["runtime"], action, expected, authorize=authorize)
                        safe_to_release = True
                return 200, {"component": item["id"], "policy_hash": expected, "observed": observed}
            except ControlError as exc:
                if not getattr(exc, "execution_uncertain", False):
                    safe_to_release = True
                raise
            finally:
                if safe_to_release:
                    self.state.release(owner)
        self.fields(payload, ("token", "workspace_id", "expected_hash", "kind"))
        workspace_id, expected, kind = payload["workspace_id"], payload["expected_hash"], payload["kind"]
        if (not isinstance(workspace_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", workspace_id)
                or not isinstance(expected, str) or not HEX64.fullmatch(expected)
                or kind not in ("test", "build")):
            raise ControlError("invalid developer job")
        if kind == "build" and not all(callable(getattr(self.runner, name, None))
                                       for name in ("prepare_build", "run_build")):
            # Compatibility only: old runners reject before allocating any job.
            self.runner.build()
            raise ControlError("isolated builder not provisioned", 503)
        if self._closing.is_set():
            raise ControlError("controller shutting down", 503)
        with self._worker_lock:
            if self._worker and self._worker.is_alive():
                raise ControlError("developer execution busy", 409)
            # Temporary owner during freeze; transferred atomically to the job id.
            owner = uuid.uuid4().hex
            self.state.acquire(owner)
            job_id = None
            try:
                description = self.workspace.describe(workspace_id, actor.id)
                if (str(description.get("owner")) != str(actor.id)
                        or description.get("source_hash") != expected):
                    raise ControlError("workspace hash or ownership changed", 409)
                snapshot = self.workspace.snapshot(workspace_id, actor.id, expected)
                if (not isinstance(snapshot, dict) or snapshot.get("source_hash") != expected
                        or str(snapshot.get("owner")) != str(actor.id)):
                    raise ControlError("immutable snapshot hash or ownership unavailable", 409)
                snapshot = dict(snapshot)
                # Immutable snapshot supplies root; describe can supply manifest only.
                if "files" not in snapshot:
                    snapshot["files"] = description.get("files")
                fresh_actor = self.authenticate(token, "develop")
                if str(fresh_actor.id) != str(actor.id):
                    raise ControlError("principal changed", 401)
                with self.heartbeat(owner):
                    prepare = self.runner.prepare_build if kind == "build" else self.runner.prepare_test
                    prepared = prepare(snapshot)
                job_id = self.state.create_job(actor.id, workspace_id, expected, kind)
                with self.state.transaction() as db:
                    job = db.execute("SELECT actor_id,workspace_id,source_hash,kind,status FROM jobs WHERE id=?",
                                     (job_id,)).fetchone()
                    if (job is None or job[0] != str(actor.id) or job[1] != workspace_id
                            or job[2] != expected or job[3] not in ("test", "build")
                            or job[3] != kind or job[4] != "queued"):
                        raise ControlError("invalid internal job binding", 503)
                    kind = job[3]  # Registered state, not a subsequently assigned request field.
                    changed = db.execute("UPDATE lease SET owner=? WHERE singleton=1 AND owner=?", (job_id, owner))
                    if changed.rowcount != 1:
                        raise ControlError("execution lease lost", 503)
                self._tokens[job_id] = token
                self._worker = threading.Thread(target=self._execute, args=(job_id, actor.id, prepared, kind))
                self._worker.daemon = True
                self._worker.start()
            except Exception:
                self.state.release(owner)
                if job_id is not None:
                    self._tokens.pop(job_id, None)
                    try:
                        self.state.finish(job_id, "failed", {"reason": "execution rejected"})
                    finally:
                        self.state.release(job_id)
                        self._worker = None
                raise
        return 202, self.state.jobs(actor.id, job_id)

    @staticmethod
    def _check_policy(key, policy, expected, action):
        if not isinstance(policy, dict) or policy.get("hash") != expected:
            raise ControlError("active policy changed or unavailable", 409)
        config = validate_config(key, policy.get("config"))
        if action != "stop" and not config["enabled"]:
            raise ControlError("controller policy disabled", 409)

    @contextlib.contextmanager
    def heartbeat(self, owner):
        stop = threading.Event()
        lost = threading.Event()

        def renew():
            while not stop.wait(5):
                try:
                    self.state.renew(owner)
                except Exception:
                    lost.set()
                    return

        self.state.renew(owner)
        worker = threading.Thread(target=renew)
        worker.daemon = True
        worker.start()
        try:
            yield
            if lost.is_set():
                raise ControlError("execution lease lost", 503)
        finally:
            stop.set()
            worker.join(timeout=6)

    def _execute(self, job_id, actor_id, prepared, kind="test"):
        safe_to_release = True
        try:
            job = self.state.jobs(actor_id, job_id)
            if kind not in ("test", "build") or job["kind"] != kind or job["status"] != "queued":
                raise ControlError("invalid internal job binding", 503)
            kind = job["kind"]
            with self.heartbeat(job_id):
                token = self._tokens[job_id]

                def authorize():
                    if self._closing.is_set():
                        raise ControlError("controller shutting down", 503)
                    actor = self.authenticate(token, "develop")
                    if str(actor.id) != str(actor_id):
                        raise ControlError("principal changed", 401)

                authorize()  # Queued expiry/revocation cannot execute.
                self.state.running(job_id)
                run = self.runner.run_build if kind == "build" else self.runner.run_test
                safe_to_release = False
                result = run(prepared, job_id, lambda cid: self.state.container(job_id, cid), authorize)
                safe_to_release = True  # Runner proved container stopped and ExitCode.
                self.state.finish(job_id, result["status"], result)
        except Exception as exc:
            if getattr(exc, "execution_uncertain", False):
                safe_to_release = False
            elif isinstance(exc, ControlError):
                safe_to_release = True
            self.state.finish(job_id, "failed", {"reason": "execution rejected" if safe_to_release else "host recovery required"})
        finally:
            self._tokens.pop(job_id, None)
            if safe_to_release:
                self.state.release(job_id)

    def close(self):
        self._closing.set()
        if self._worker:
            self._worker.join(timeout=210)
            if self._worker.is_alive():
                # Never release the process flock while an executor is still alive.
                raise ControlError("executor still active", 503)
        self._tokens.clear()
        self.state.close()


class ControllerHTTPServer(HTTPServer):
    """No ThreadingMixIn: one bounded active HTTP connection worker."""
    request_queue_size = 1
    allow_reuse_address = False

    def __init__(self, address, controller):
        if (not isinstance(address, tuple) or len(address) != 2 or address[0] != "127.0.0.1"
                or type(address[1]) is not int or not 1 <= address[1] <= 65535):
            raise ControlError("explicit IPv4 loopback controller bind required", 503)
        self.controller = controller
        self.expected_host = "%s:%d" % address
        super().__init__(address, ControllerHandler)

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(5)
        return connection, address


class HeaderBudget:
    def __init__(self, stream):
        self.stream = stream
        self.remaining = 8192

    def readline(self, limit=-1):
        line = self.stream.readline(min(limit, self.remaining + 1) if limit >= 0 else self.remaining + 1)
        self.remaining -= len(line)
        if self.remaining < 0:
            raise LineTooLong("controller header budget")
        return line


class ControllerHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    server_version = "ControlledWorkbench"
    sys_version = ""

    def parse_request(self):
        raw_requestline = getattr(self, "raw_requestline",
                                  getattr(self, "rawrequestline", None))
        status = None
        if not isinstance(raw_requestline, bytes):
            status = 400
        elif len(raw_requestline) > 1024:
            status = 414
        elif not raw_requestline.strip():
            status = 400
        if status is not None:
            self.requestline, self.request_version, self.command = "", "HTTP/1.0", None
            self.send_error(status)
            return False
        self.raw_requestline = raw_requestline
        original = self.rfile
        try:
            self.rfile = HeaderBudget(original)
            return super().parse_request()
        finally:
            self.rfile = original

    def log_message(self, format, *args):
        pass  # No path, token, HMAC, request body or source output logging.

    def handle_expect_100(self):
        self.send_error(417)
        return False

    def send_error(self, code, message=None, explain=None):
        # Base class parse failures (unknown methods/overlong headers) are signed too.
        self._respond(code, b'{"error":"invalid internal HTTP request"}')

    def _respond(self, status, body, headers=None):
        if headers is None:
            original = getattr(self, "headers", {})
            request_id = original.get("X-Control-Request-ID", "")
            nonce = original.get("X-Control-Nonce", "")
            request_id = request_id if HEX32.fullmatch(request_id) else "0" * 32
            nonce = nonce if HEX32.fullmatch(nonce) else "0" * 32
            headers = response_headers(self.server.controller.key, request_id, nonce, status, body)
        self.close_connection = True
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        if getattr(self, "command", None) != "HEAD":
            self.wfile.write(body)

    def _request(self):
        sensitive = ("Content-Length", "Content-Type", "Transfer-Encoding", "Expect", "Host",
                     "X-Control-Timestamp", "X-Control-Nonce", "X-Control-Request-ID", "X-Control-Signature")
        try:
            if any(len(self.headers.get_all(name, [])) > 1 for name in sensitive):
                raise ControlError("duplicate HTTP headers")
            length = self.headers.get("Content-Length", "")
            if (self.headers.get("Transfer-Encoding") is not None or self.headers.get("Expect") is not None
                    or self.headers.get("Content-Type") != "application/json"
                    or not re.fullmatch(r"[0-9]{1,6}", length)):
                raise ControlError("strict JSON body framing required")
            size = int(length)
            if not 0 < size <= MAX_BODY:
                raise ControlError("request too large or empty", 413)
            # Transport endpoint binding is independent of the request HMAC.
            # Missing, alias, external or different-port Hosts never reach auth/dedup.
            if self.headers.get("Host") != self.server.expected_host:
                raise ControlError("Host does not match controller endpoint")
            body = self.rfile.read(size)
            if len(body) != size:
                raise ControlError("incomplete request body")
            status, response, headers = self.server.controller.handle(self.command, self.path, body, self.headers)
            self._respond(status, response, headers)
        except ControlError as exc:
            self._respond(exc.status, canonical({"error": exc.detail}))
        except (OSError, ValueError):
            self.close_connection = True

    do_GET = _request
    do_POST = _request
    do_PUT = _request
    do_DELETE = _request
    do_PATCH = _request
    do_OPTIONS = _request


def create_controller():
    """Lazy integration with the independently owned store/security/workspace."""
    key = load_key(os.environ.get("CONTROL_KEY_FILE", ""))
    management_path = os.environ.get("CONTROL_DB_PATH", "")
    private_parent(management_path)
    state_path = os.path.join(os.path.dirname(management_path), "controller.db")
    if state_path == management_path:
        raise ControlError("distinct business and controller databases required", 503)
    state = ControllerState(state_path)
    try:
        store_module = importlib.import_module(".store", __package__)
        security_module = importlib.import_module(".security", __package__)
        workspace_module = importlib.import_module(".workspace", __package__)
        # Current peers expose module-level functions; optional object adapters
        # remain lazy for integration. Never initialize or migrate their database.
        store = store_module.Store(management_path) if hasattr(store_module, "Store") else store_module
        if callable(getattr(store, "database_path", None)) and store.database_path() != management_path:
            raise ControlError("management database binding changed", 503)
        security = security_module.Security(store) if hasattr(security_module, "Security") else security_module
        workspace = workspace_module.Workspace(store) if hasattr(workspace_module, "Workspace") else workspace_module
        return Controller(key, state, security, store, workspace)
    except Exception:
        state.close()
        raise ControlError("controller integration not provisioned", 503)


def main():
    port = os.environ.get("CONTROL_CONTROLLER_PORT", "")
    if not re.fullmatch(r"[0-9]{1,5}", port) or not 1 <= int(port) <= 65535:
        raise ControlError("explicit CONTROL_CONTROLLER_PORT required", 503)
    controller = create_controller()
    server = None
    try:
        server = ControllerHTTPServer(("127.0.0.1", int(port)), controller)
        server.serve_forever(poll_interval=0.2)
    finally:
        if server:
            server.server_close()
        controller.close()


if __name__ == "__main__":
    main()
