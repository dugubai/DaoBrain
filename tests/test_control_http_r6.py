"""Real r6 loopback HTTP integration; NOT deployment or image acceptance.

Execute only after approval in the existing cloud Python 3.11 image, with
network=none, no runtime socket and an explicit writable TMPDIR outside /app:
PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s tests \
    -p test_control_http_r6.py -v
No local execution, installation, xdist, auth/HTTP/ControllerClient doubles,
attestation, sandbox execution, business database, or schema/data restoration.
Imports requiring installed FastAPI/Uvicorn are deliberately deferred until
setUpClass; missing cloud dependencies are errors, never skips.
"""
import contextlib
import errno
import hashlib
import http.client
import importlib
import io
import json
import os
from pathlib import Path
import secrets
import socket
import sqlite3
import stat
import sys
import tempfile
import threading
import time
import unittest
from urllib.parse import urlencode


PROJECT = Path(__file__).resolve().parents[1]
CONFIG = {"enabled": True, "request_timeout_s": 12}
LIMIT = 262144


class ControlHTTPR6Tests(unittest.TestCase):
    """One class-scoped fixture, one real browser server and one controller."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.saved_environment = cls.saved_bytecode = cls.saved_path = None
        cls.saved_attempts = cls.saved_threads = None
        cls.temporary = cls.browser_socket = cls.browser = cls.browser_thread = None
        cls.controller = cls.controller_server = cls.controller_thread = None
        cls.security = cls.source_before = None
        cls.directories = {}
        cls.tokens, cls.passwords, cls.users = {}, {}, {}
        cls.key = b""
        cls.sequence = 0
        cls.addClassCleanup(cls._cleanup)
        if sys.platform != "linux" or sys.version_info[:2] != (3, 11):
            raise AssertionError("HTTP execution requires the approved cloud Python 3.11 image")
        if os.environ.get("PYTEST_XDIST_WORKER"):
            raise AssertionError("This shared-server suite must run once, without xdist")
        if set(os.listdir("/sys/class/net")) != {"lo"}:
            raise AssertionError("HTTP execution requires network=none; only loopback is allowed")
        for path in ("/var/run/docker.sock", "/run/docker.sock", "/run/podman/podman.sock"):
            if os.path.lexists(path):
                raise AssertionError("Runtime sockets must not be mounted")
        tmpdir = os.environ.get("TMPDIR", "")
        if (not os.path.isabs(tmpdir) or os.path.realpath(tmpdir) != tmpdir
                or not os.path.isdir(tmpdir)):
            raise AssertionError("An explicit existing absolute nonsymlink TMPDIR is required")
        if os.path.commonpath((tmpdir, str(PROJECT))) in (tmpdir, str(PROJECT)):
            raise AssertionError("TMPDIR must be separate from the repository")

        cls.saved_environment = dict(os.environ)
        cls.saved_bytecode = sys.dont_write_bytecode
        cls.saved_path = list(sys.path)
        # Include AnyIO/FastAPI workers, not only the named server threads.
        # Capture before imports or any fixture thread can be started.
        cls.saved_threads = set(threading.enumerate())
        sys.dont_write_bytecode = True
        if str(PROJECT) not in sys.path:
            sys.path.insert(0, str(PROJECT))
        cls.temporary = tempfile.TemporaryDirectory(prefix="control-http-r6-", dir=tmpdir)
        cls.base = Path(cls.temporary.name)
        cls.directories = {name: cls.base / name for name in
                           ("management", "source", "workspace", "release", "trust")}
        for path in cls.directories.values():
            path.mkdir(mode=0o700)
        cls.management_db = cls.directories["management"] / "management.sqlite"
        cls.release_db = cls.directories["release"] / "releases.sqlite"
        cls.key_path = cls.directories["trust"] / "controller.key"
        cls.key = secrets.token_bytes(32)
        with cls.key_path.open("xb") as stream:
            stream.write(cls.key)
        cls.key_path.chmod(0o600)

        # Only inert, synthetic UTF-8 import-baseline text. Never copy live source,
        # credentials, tests, business records, images or sockets into candidates.
        cls.source_text = {
            "control_plane/registry.py": "# synthetic trusted source marker; never imported\n",
            "common/__init__.py": "# synthetic import baseline; never imported\n",
            "application_hub/main.py": "# synthetic protected baseline; never imported\n",
            "gateway/main.py": "# synthetic component text; never executed\n",
            "gateway/page.html": "<p>synthetic baseline</p>\n",
        }
        for relative, content in cls.source_text.items():
            target = cls.directories["source"] / relative
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with target.open("x", encoding="utf-8") as stream:
                stream.write(content)
            target.chmod(0o400)
        cls.source_before = cls._source_identity()
        # Do not inherit application credentials, proxy targets, business paths,
        # runtime enrollment or Docker contexts into these synthetic services.
        environment = {key: value for key, value in cls.saved_environment.items()
                       if key in ("PATH", "LANG", "LC_ALL", "TZ")}
        environment.update({
            "TMPDIR": tmpdir, "PYTHONDONTWRITEBYTECODE": "1",
            "CONTROL_DB_PATH": str(cls.management_db),
            "CONTROL_RELEASE_DB_PATH": str(cls.release_db),
            "CONTROL_SOURCE_ROOT": str(cls.directories["source"]),
            "CONTROL_WORKSPACE_ROOT": str(cls.directories["workspace"]),
            "CONTROL_KEY_FILE": str(cls.key_path),
            # Do not enroll, substitute a runner, or provision sandbox trust.
            "CONTROL_RUNTIME_FILE": "",
            "CONTROL_RELEASE_STAGING_ROOT": "",
            "CONTROL_RELEASE_ATTESTATION_KEY_PATH": "",
        })
        os.environ.clear()
        os.environ.update(environment)
        cls.security = importlib.import_module("control_plane.security")
        with cls.security._lock:
            # Preserve mappings as a dict; the current real module uses a deque.
            cls.saved_attempts = (dict(cls.security._attempts)
                                  if isinstance(cls.security._attempts, dict)
                                  else cls.security._attempts.copy())
            cls.security._attempts.clear()
        cls.store = importlib.import_module("control_plane.store")
        cls.releases = importlib.import_module("control_plane.releases")
        cls.registry = importlib.import_module("control_plane.registry")
        cls.protocol = importlib.import_module("control_plane.protocol")
        cls.controller_module = importlib.import_module("control_plane.controller")
        cls._cli("init")
        cls.releases.initialize()  # Explicit local provisioning, never HTTP.
        grants = {
            "author": ("view", "develop", "configure"),
            "reviewer": ("view", "review"),
            "operator": ("view", "operate"),
            "viewer": ("view",),
            "logoutuser": ("view",),
            "revocable": ("view", "develop"),
        }
        for name, permissions in grants.items():
            cls.passwords[name] = secrets.token_urlsafe(24)
            args = ["bootstrap" if name == "author" else "create", "--name", name]
            for permission in permissions:
                args.extend(("--permission", permission))
            os.environ["CONTROL_PROVISION_PASSWORD"] = cls.passwords[name]
            try:
                cls._cli(*args)
            finally:
                os.environ.pop("CONTROL_PROVISION_PASSWORD", None)
        with cls.store.transaction() as db:
            if db.execute("SELECT count(*) FROM cp_sessions").fetchone()[0] != 0:
                raise AssertionError("Provisioning must not fabricate sessions")

        cls.controller = cls.controller_module.create_controller()  # Real default Runner.
        cls.controller_server = cls._bind_controller(cls.controller)
        cls._start_controller_thread()
        os.environ["CONTROL_CONTROLLER_URL"] = "http://127.0.0.1:%d" % cls.controller_server.server_port
        cls.browser_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        cls.browser_socket.bind(("127.0.0.1", 0))
        cls.browser_socket.listen(16)
        cls.browser_port = cls.browser_socket.getsockname()[1]
        cls.host = "127.0.0.1:%d" % cls.browser_port
        cls.origin = "http://" + cls.host
        os.environ["APP_ORIGIN"] = cls.origin
        # These imports must error, not skip, if dependencies are missing.
        uvicorn = importlib.import_module("uvicorn")
        main = importlib.import_module("control_plane.main")
        cls.browser = uvicorn.Server(uvicorn.Config(
            main.app, host="127.0.0.1", port=cls.browser_port, http="h11",
            loop="asyncio", lifespan="on", access_log=False, log_config=None,
            log_level="critical", timeout_keep_alive=1, timeout_graceful_shutdown=2))
        cls.browser_thread = threading.Thread(
            target=cls.browser.run, kwargs={"sockets": [cls.browser_socket]},
            name="r6-browser-http", daemon=True)
        cls.browser_thread.start()
        deadline = time.monotonic() + 10
        waiter = threading.Event()
        while not cls.browser.started:
            if not cls.browser_thread.is_alive() or time.monotonic() >= deadline:
                raise AssertionError("Real Uvicorn failed to become ready")
            waiter.wait(0.01)
        status, _headers, health = cls._request("GET", "/health")
        if status != 200 or health.get("status") != "ok":
            raise AssertionError("Real browser API health failed")
        for name, permissions in grants.items():
            status, headers, result = cls._request("POST", "/api/auth/login", payload={
                "name": name, "password": cls.passwords[name]})
            if status != 200 or set(result) != {"token", "expires", "user"}:
                raise AssertionError("Synthetic account HTTP login failed")
            if set(result["user"]["permissions"]) != set(permissions):
                raise AssertionError("Synthetic account grants differ from explicit CLI provisioning")
            if headers.get("cache-control") != "no-store":
                raise AssertionError("Login must never be cacheable")
            token = result["token"]
            if not isinstance(token, str) or len(token) != 43:
                raise AssertionError("Invalid real HTTP session token")
            cls.tokens[name], cls.users[name] = token, result["user"]

    @classmethod
    def _cli(cls, *args):
        # The CLI emits only a fixed completion notice, not credentials.
        with contextlib.redirect_stdout(io.StringIO()):
            if cls.security.cli(list(args)) != 0:
                raise AssertionError("Local synthetic account provisioning failed")

    @classmethod
    def _source_identity(cls):
        result = {}
        for relative in cls.source_text:
            path = cls.directories["source"] / relative
            info = path.stat()
            result[relative] = (hashlib.sha256(path.read_bytes()).hexdigest(), info.st_ino,
                                info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_mode)
        return result

    @classmethod
    def _bind_controller(cls, controller):
        # This server rejects port=0. Reserve/discover then bind, with bounded
        # EADDRINUSE retries for the small unavoidable reservation race.
        for _attempt in range(8):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reservation:
                reservation.bind(("127.0.0.1", 0))
                port = reservation.getsockname()[1]
            try:
                return cls.controller_module.ControllerHTTPServer(("127.0.0.1", port), controller)
            except OSError as error:
                if error.errno != errno.EADDRINUSE:
                    raise
        raise AssertionError("Could not bind a dynamic loopback controller port")

    @classmethod
    def _start_controller_thread(cls):
        cls.controller_thread = threading.Thread(
            target=cls.controller_server.serve_forever, kwargs={"poll_interval": 0.02},
            name="r6-controller-http", daemon=True)
        cls.controller_thread.start()

    @classmethod
    def _restart_controller_state(cls):
        # Retain the listening socket/Host so the exact signed HTTP headers can
        # be replayed after releasing/reopening the durable SQLite state/flock.
        cls.controller_server.shutdown()
        cls.controller_thread.join(timeout=6)
        if cls.controller_thread.is_alive():
            raise AssertionError("Controller HTTP thread did not stop")
        cls.controller.close()
        cls.controller = None
        cls.controller = cls.controller_module.create_controller()
        cls.controller_server.controller = cls.controller
        cls._start_controller_thread()

    @classmethod
    def _cleanup(cls):
        errors = []
        current = threading.current_thread()

        def step(message, action):
            try:
                action()
                return True
            except Exception:
                # Never include exception text, credentials or fixture paths.
                errors.append(message)
                return False

        def live(thread):
            try:
                return (thread is not None and thread is not current
                        and thread.ident is not None and thread.is_alive())
            except Exception:
                errors.append("Fixture thread state could not be checked")
                return True  # Unknown state must not permit shared-state removal.

        def join(thread, timeout, message):
            def started_join():
                if thread is not None and thread is not current and thread.ident is not None:
                    thread.join(timeout=timeout)
            step(message, started_join)

        browser = getattr(cls, "browser", None)
        browser_thread = getattr(cls, "browser_thread", None)
        controller_thread = getattr(cls, "controller_thread", None)
        controller = getattr(cls, "controller", None)
        if browser is not None:
            step("Browser exit request failed", lambda: setattr(browser, "should_exit", True))
        join(browser_thread, 8, "Browser HTTP thread join failed")
        if live(browser_thread):
            if browser is not None:
                step("Browser forced exit request failed", lambda: setattr(browser, "force_exit", True))
            join(browser_thread, 3, "Browser HTTP thread join failed")
        if live(browser_thread):
            errors.append("Browser HTTP thread did not stop")
        browser_socket = getattr(cls, "browser_socket", None)
        if browser_socket is not None:
            step("Browser socket did not close cleanly", browser_socket.close)
        controller_server = getattr(cls, "controller_server", None)
        if controller_server is not None:
            # shutdown() also waits forever if serve_forever() never started.
            if live(controller_thread):
                step("Controller HTTP shutdown failed", controller_server.shutdown)
                join(controller_thread, 6, "Controller HTTP thread join failed")
            if live(controller_thread):
                errors.append("Controller HTTP thread did not stop")
            step("Controller HTTP server did not close cleanly", controller_server.server_close)
        workers = []
        worker_checked = True
        if controller is not None:
            def remember_worker():
                # Retain the reference even if close() clears controller._worker.
                worker = controller._worker
                if worker is not None:
                    workers.append(worker)
                    errors.append("Unenrolled fixture unexpectedly created an execution worker")
            worker_checked = step("Controller worker state could not be checked", remember_worker)
            step("Controller did not close cleanly", controller.close)

        saved_threads = getattr(cls, "saved_threads", None)
        new_threads = set()

        def find_new_threads():
            if saved_threads is not None:
                new_threads.update(threading.enumerate())
                new_threads.difference_update(saved_threads)
                new_threads.discard(current)

        threads_checked = step("Fixture threads could not be enumerated", find_new_threads)
        # Server shutdown can leave AnyIO/FastAPI pool threads behind. Give all
        # new live threads ONE shared budget, not three seconds per thread.
        deadline = time.monotonic() + 3
        for thread in new_threads:
            if live(thread):
                join(thread, max(0, deadline - time.monotonic()), "Fixture thread join failed")
        threads_checked = step("Fixture threads could not be enumerated", find_new_threads) and threads_checked
        live_threads = not threads_checked or not worker_checked or any(
            live(thread) for thread in list(new_threads) + [browser_thread, controller_thread] + workers)
        # No restoration/deletion while ANY fixture-created thread remains.
        if live_threads:
            errors.append("Fixture threads did not stop; shared state and storage retained")
        else:
            temporary = getattr(cls, "temporary", None)
            if temporary is not None:
                if getattr(cls, "source_before", None) is not None:
                    def check_source():
                        if cls._source_identity() != cls.source_before:
                            errors.append("HTTP integration modified synthetic source")
                    step("Synthetic source identity check failed", check_source)

                def unseal_workspace():
                    # Source/setup may be only partly constructed. Only unseal
                    # fixture workspace snapshots, never the source baseline.
                    workspace_root = getattr(cls, "directories", {}).get("workspace")
                    if workspace_root is not None:
                        for root, directories, _files in os.walk(workspace_root):
                            step("Fixture workspace permissions cleanup failed",
                                 lambda: os.chmod(root, 0o700))
                            for directory in directories:
                                step("Fixture workspace permissions cleanup failed",
                                     lambda: os.chmod(os.path.join(root, directory), 0o700))
                step("Fixture workspace permissions cleanup failed", unseal_workspace)
                step("Fixture temporary storage cleanup failed", temporary.cleanup)

            if getattr(cls, "saved_attempts", None) is not None:
                def restore_attempts():
                    with cls.security._lock:
                        cls.security._attempts.clear()
                        if isinstance(cls.saved_attempts, dict):
                            cls.security._attempts.update(cls.saved_attempts)
                        else:
                            cls.security._attempts.extend(cls.saved_attempts)
                step("Authentication attempts restoration failed", restore_attempts)
            for name in ("tokens", "passwords"):
                values = getattr(cls, name, None)
                if values is not None:
                    step("Fixture credentials cleanup failed", values.clear)
            step("Fixture key cleanup failed", lambda: setattr(cls, "key", b""))
            if getattr(cls, "saved_environment", None) is not None:
                step("Environment restoration failed", os.environ.clear)
                step("Environment restoration failed", lambda: os.environ.update(cls.saved_environment))
            if getattr(cls, "saved_bytecode", None) is not None:
                step("Bytecode setting restoration failed",
                     lambda: setattr(sys, "dont_write_bytecode", cls.saved_bytecode))
            if getattr(cls, "saved_path", None) is not None:
                step("Import path restoration failed", lambda: sys.path.__setitem__(slice(None), cls.saved_path))
        if errors:
            raise AssertionError("; ".join(errors)) from None

    @classmethod
    def _request(cls, method, path, role=None, payload=None, headers=None, body=None):
        if not path.startswith("/") or path.startswith("//"):
            raise AssertionError("Only fixed local request targets are permitted")
        request_headers = {"Host": cls.host, "Connection": "close"}
        if method != "GET":
            request_headers.update({"Origin": cls.origin, "Content-Type": "application/json"})
        if role is not None:
            request_headers["Authorization"] = "Bearer " + cls.tokens[role]
        if headers:
            request_headers.update(headers)
        if payload is not None:
            if body is not None:
                raise AssertionError("Use either a JSON payload or raw body")
            body = json.dumps(payload, separators=(",", ":"), allow_nan=False).encode("utf-8")
        connection = http.client.HTTPConnection("127.0.0.1", cls.browser_port, timeout=6)
        try:
            connection.request(method, path, body=body, headers=request_headers)
            response = connection.getresponse()
            data = response.read(LIMIT + 1)
            if len(data) > LIMIT:
                raise AssertionError("Browser response exceeded integration bound")
            result = json.loads(data)
            return response.status, dict((k.lower(), v) for k, v in response.getheaders()), result
        finally:
            connection.close()

    def http(self, method, path, expected=200, **kwargs):
        status, headers, value = self._request(method, path, **kwargs)
        # Never include a response/body/header in a failure: login tokens and
        # Authorization values must not appear in test reports or traces.
        self.assertEqual(status, expected, "%s %s returned unexpected status" % (method, path))
        self.assertEqual(headers.get("cache-control"), "no-store")
        self.assertEqual(headers.get("x-content-type-options"), "nosniff")
        self.assertEqual(headers.get("referrer-policy"), "no-referrer")
        self.assertEqual(headers.get("x-frame-options"), "DENY")
        return value

    @classmethod
    def _wire(cls, port, wire):
        # Raw TCP is required to exercise duplicate/ambiguous HTTP framing;
        # neither the ASGI boundary nor a transport response is simulated.
        with socket.create_connection(("127.0.0.1", port), timeout=6) as connection:
            connection.sendall(wire)
            # Keep the request side open while Uvicorn schedules its ASGI task;
            # early EOF may otherwise turn a complete request into disconnect.
            response = http.client.HTTPResponse(connection)
            try:
                response.begin()
                body = response.read(LIMIT + 1)
                if len(body) > LIMIT:
                    raise AssertionError("Raw HTTP response exceeded integration bound")
                return response.status, dict(response.getheaders()), body
            finally:
                response.close()

    @classmethod
    def _signed_request(cls, body, headers):
        connection = http.client.HTTPConnection(
            "127.0.0.1", cls.controller_server.server_port, timeout=6)
        try:
            connection.request("GET", "/v1/status", body=body, headers=headers)
            response = connection.getresponse()
            data = response.read(LIMIT + 1)
            cls.protocol.verify_response(cls.key, headers["X-Control-Request-ID"],
                                         headers["X-Control-Nonce"], response.status,
                                         data, response.headers)
            return response.status, cls.protocol.strict_json(data, LIMIT)
        finally:
            connection.close()

    def approved_config(self, activate=False):
        revision = self.http("POST", "/api/manage/config/revisions", 201, role="author",
                             payload={"component": "gateway", "config": CONFIG})
        self.assertEqual(revision["owner"], self.users["author"]["id"])
        for action, role, extra in (("submit", "author", {}),
                                    ("review", "reviewer", {"decision": "approve"})):
            revision = self.http("POST", "/api/manage/config/revisions/%s/%s" % (revision["id"], action),
                                 role=role, payload=dict(expected_version=revision["version"], **extra))
        self.assertEqual(revision["reviewer"], self.users["reviewer"]["id"])
        self.assertNotEqual(revision["owner"], revision["reviewer"])
        self.assertTrue(revision["policy_only"])
        self.assertFalse(revision["business_config_implemented"])
        if activate:
            revision = self.http("POST", "/api/manage/config/revisions/%s/activate" % revision["id"],
                                 role="author", payload={"expected_version": revision["version"]})
        return revision

    def new_workspace(self):
        return self.http("POST", "/api/dev/workspaces", 201, role="author", payload={"component": "gateway"})

    def version(self, previous=None, schema=1, image=None):
        revision = self.approved_config()
        type(self).sequence += 1
        manifest = {
            "component": "gateway", "runtime": "gateway", "label": "r6.0.%d" % self.sequence,
            "source_sha256": self.registry.digest([
                {"path": path, "sha256": hashlib.sha256(text.encode()).hexdigest()}
                for path, text in sorted(self.source_text.items()) if not path.startswith("control_plane/")]),
            # These identities are explicitly UNVERIFIED synthetic declarations,
            # not fabricated successful test/build reports or real image proof.
            "artifact_sha256": hashlib.sha256(b"unverified synthetic artifact").hexdigest(),
            "config_revision": revision["id"], "config_sha256": revision["hash"],
            "test_evidence_sha256": hashlib.sha256(b"unverified test declaration").hexdigest(),
            "build_evidence_sha256": hashlib.sha256(b"unverified build declaration").hexdigest(),
            "image_digest": image, "schema_version": schema, "previous_version": previous,
        }
        result = self.http("POST", "/api/releases/versions", 201, role="author", payload={"manifest": manifest})
        self.assertEqual(result["manifest"], manifest)
        self.assertEqual(result["status"], "registered-unverified")
        self.assertEqual(result["verification_state"], "pending")
        self.assertFalse(result["image_verified"])
        self.assertFalse(result["execution_ready"])
        self.assertFalse(result["deployed"])
        self.assertFalse(result["business_config_verified"])
        return result

    def plan_input(self, version_id):
        type(self).sequence += 1
        now = int(time.time())
        return {"version_id": version_id, "planned_at": now - 5,
                "expires_at": now + 900 + self.sequence * 10, "dependencies": [],
                "prechecks": {key: None for key in
                              ("health_sha256", "capacity_sha256", "baseline_sha256", "backup_sha256")},
                "strategy": "serial-app-slot"}

    def approved_plan(self, version):
        plan = self.http("POST", "/api/releases/plans", 201, role="author",
                         payload={"plan": self.plan_input(version["id"])})
        plan = self.http("POST", "/api/releases/plans/%s/submit" % plan["id"], role="author",
                         payload={"expected_version": plan["version"]})
        plan = self.http("POST", "/api/releases/plans/%s/review" % plan["id"], role="reviewer",
                         payload={"expected_version": plan["version"], "decision": "approve"})
        self.assertEqual(plan["owner"], self.users["author"]["id"])
        self.assertEqual(plan["reviewer"], self.users["reviewer"]["id"])
        self.assertFalse(plan["execution_ready"])
        self.assertFalse(plan["deployed"])
        self.assertFalse(plan["image_verified"])
        self.assertFalse(plan["data_rollback_supported"])
        return plan

    def test_accounts_sessions_are_real_and_private(self):
        self.assertEqual(len({user["id"] for user in self.users.values()}), 6)
        for role in ("author", "reviewer", "operator", "viewer"):
            me = self.http("GET", "/api/auth/me", role=role)
            self.assertEqual(me, self.users[role])
            with self.store.transaction() as db:
                row = db.execute("SELECT digest,user_id,expires FROM cp_sessions WHERE digest=?",
                                 (self.security.digest_token(self.tokens[role]),)).fetchone()
                self.assertIsNotNone(row)
                self.assertEqual(row["user_id"], me["id"])
                self.assertTrue(row["expires"] > int(time.time()))
                self.assertTrue(row["digest"] != self.tokens[role], "Raw tokens must not be persisted")
        self.http("GET", "/api/auth/me", 401)
        self.http("POST", "/api/auth/login", 401,
                  payload={"name": "viewer", "password": "incorrect-synthetic-password"})
        self.http("POST", "/api/auth/login", 422,
                  payload={"name": "viewer", "password": "synthetic-password", "role": "admin"})
        for path in self.directories.values():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
            self.assertEqual(path.stat().st_uid, os.geteuid())
        for path in (self.management_db, self.release_db, self.key_path):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(self.key_path.stat().st_size, 32)
        self.assertEqual(self.key_path.parent, self.directories["trust"])
        with self.store.transaction() as db:
            self.assertEqual({row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")},
                             {"cp_meta", "cp_users", "cp_grants", "cp_sessions", "cp_revisions", "cp_active", "cp_audit"})
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)

    def test_logout_and_live_permission_disable_revocation(self):
        self.assertTrue(self.http("POST", "/api/auth/logout", role="logoutuser", payload={})["logged_out"])
        self.http("GET", "/api/auth/me", 401, role="logoutuser")
        self.http("POST", "/api/auth/logout", 401, role="logoutuser", payload={})
        with self.store.transaction() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM cp_sessions WHERE digest=?",
                                        (self.security.digest_token(self.tokens["logoutuser"]),)).fetchone()[0], 0)
        self._cli("revoke", "--name", "revocable", "--permission", "develop")
        me = self.http("GET", "/api/auth/me", role="revocable")
        self.assertEqual(me["permissions"], ["view"])
        self.http("POST", "/api/dev/workspaces", 403, role="revocable", payload={"component": "gateway"})
        self._cli("revoke", "--name", "revocable", "--permission", "view")
        self.http("GET", "/api/releases/versions", 403, role="revocable")
        client = self.protocol.ControllerClient(timeout=6)
        with self.assertRaises(self.registry.ControlError) as caught:
            client.request("GET", "/v1/status", {}, self.tokens["revocable"])
        self.assertEqual(caught.exception.status, 403)
        self._cli("disable", "--name", "revocable")
        self.http("GET", "/api/auth/me", 401, role="revocable")
        with self.assertRaises(self.registry.ControlError) as caught:
            client.request("GET", "/v1/status", {}, self.tokens["revocable"])
        self.assertEqual(caught.exception.status, 401)

    def test_browser_host_origin_and_strict_json_rejections(self):
        self.http("GET", "/health", 400, headers={"Host": "public.invalid"})
        self.http("GET", "/health", 403, headers={"Origin": "https://public.invalid"})
        self.http("POST", "/api/auth/logout", 403, role="viewer", payload={},
                  headers={"Origin": "https://public.invalid"})
        self.http("POST", "/api/auth/logout", 403, role="viewer", payload={}, headers={"Origin": ""})
        self.http("GET", "/health", 403, headers={"Sec-Fetch-Site": "cross-site"})
        self.http("POST", "/api/auth/logout", 415, role="viewer", body=b"{}",
                  headers={"Content-Type": "text/plain"})
        self.http("POST", "/api/auth/logout", 415, role="viewer", body=b"{}",
                  headers={"Content-Encoding": "gzip"})
        for body in (b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":1e999}',
                     b'{"x":"\\ud800"}', b'[]', b'{"x":', b'\xff'):
            # No credentials in malformed JSON, so failing subtests cannot leak them.
            with self.subTest(case=body):
                self.http("POST", "/api/auth/logout", 422, role="viewer", body=body)
        self.http("GET", "/health", 413, body=b"x")
        self.http("POST", "/api/auth/logout", 413, role="viewer", body=b"x" * 65537)

    def test_browser_real_duplicate_and_chunked_framing(self):
        base = ("POST /api/auth/logout HTTP/1.1\r\nHost: %s\r\nOrigin: %s\r\n"
                "Content-Type: application/json\r\nConnection: close\r\n" % (self.host, self.origin)).encode()
        for name, extra, body, expected in (
                ("duplicate origin", ("Origin: %s\r\n" % self.origin).encode() + b"Content-Length: 2\r\n", b"{}", 400),
                ("duplicate host", ("Host: %s\r\n" % self.host).encode() + b"Content-Length: 2\r\n", b"{}", 400),
                ("ambiguous framing", b"Content-Length: 2\r\nTransfer-Encoding: chunked\r\n", b"2\r\n{}\r\n0\r\n\r\n", 400),
                ("invalid length", b"Content-Length: nope\r\n", b"{}", 400),
                ("unsupported transfer", b"Transfer-Encoding: gzip\r\n", b"{}", 400),
                ("chunked strict JSON", b"Transfer-Encoding: chunked\r\n", b"d\r\n{\"x\":1,\"x\":2}\r\n0\r\n\r\n", 422),
                ("chunked bound", b"Transfer-Encoding: chunked\r\n",
                 b"10001\r\n" + b"x" * 65537 + b"\r\n0\r\n\r\n", 413)):
            with self.subTest(case=name):
                status, _headers, _body = self._wire(self.browser_port, base + extra + b"\r\n" + body)
                self.assertEqual(status, expected)
        # A valid chunked body traverses the real server/boundary and reaches
        # real authentication (401), rather than accepting a fake response.
        status, _headers, _body = self._wire(
            self.browser_port, base + b"Transfer-Encoding: chunked\r\n\r\n2\r\n{}\r\n0\r\n\r\n")
        self.assertEqual(status, 401)

    def test_unobserved_components_reports_and_no_public_deployment(self):
        health = self.http("GET", "/health")
        self.assertEqual(health["controller_runtime"], "unobserved")
        self.assertFalse(health["business_config_implemented"])
        self.assertFalse(health["publish_enabled"])
        snapshot = self.http("GET", "/api/manage/components", role="viewer")
        self.assertTrue(snapshot["controller_observed"])  # Signed controller, not runtime observation.
        self.assertEqual({item["id"] for item in snapshot["components"]}, set(self.registry.REGISTRY))
        for item in snapshot["components"]:
            self.assertEqual(item["runtime_state"], {"state": "unobserved"})
            self.assertFalse(item["runtime_observed"])
            self.assertFalse(item["publish_enabled"])
            self.assertTrue(item["policy_only"])
            self.assertFalse(item["business_config_implemented"])
        self.assertFalse(snapshot["test"]["ready"])
        self.assertFalse(snapshot["build"]["ready"])
        report = self.http("GET", "/api/manage/report", role="viewer")
        self.assertFalse(report["historical_reports_available"])
        self.assertFalse(report["alerts_available"])
        self.assertFalse(report["snapshot"]["publish_enabled"])
        self.assertEqual(set(os.listdir("/sys/class/net")), {"lo"})
        self.assertEqual(self.browser_socket.getsockname()[0], "127.0.0.1")
        self.assertEqual(self.controller_server.server_address[0], "127.0.0.1")
        self.assertTrue(self.origin.startswith("http://127.0.0.1:"))

    def test_config_author_edit_submit_independent_review_activate(self):
        path = "/api/manage/config/revisions"
        self.http("POST", path, 403, role="viewer", payload={"component": "gateway", "config": CONFIG})
        self.http("POST", path, 400, role="author",
                  payload={"component": "gateway", "config": dict(CONFIG, enabled=1)})
        revision = self.http("POST", path, 201, role="author", payload={"component": "gateway", "config": CONFIG})
        target = path + "/" + revision["id"]
        self.http("PATCH", target, 409, role="author", payload={"expected_version": 2, "config": CONFIG})
        changed = dict(CONFIG, request_timeout_s=24)
        revision = self.http("PATCH", target, role="author", payload={"expected_version": 1, "config": changed})
        revision = self.http("POST", target + "/submit", role="author", payload={"expected_version": revision["version"]})
        self._cli("grant", "--name", "author", "--permission", "review")
        try:
            self.http("POST", target + "/review", 409, role="author",
                      payload={"expected_version": revision["version"], "decision": "approve"})
        finally:
            self._cli("revoke", "--name", "author", "--permission", "review")
        revision = self.http("POST", target + "/review", role="reviewer",
                             payload={"expected_version": revision["version"], "decision": "approve"})
        self.assertNotEqual(revision["owner"], revision["reviewer"])
        self.http("PATCH", target, 409, role="author", payload={"expected_version": revision["version"], "config": CONFIG})
        revision = self.http("POST", target + "/activate", role="author", payload={"expected_version": revision["version"]})
        self.assertEqual(revision["state"], "activated")
        self.assertEqual(revision["config"], changed)
        listed = self.http("GET", path + "?component=gateway", role="viewer")
        self.assertTrue(any(item["id"] == revision["id"] and item["state"] == "activated"
                            for item in listed["revisions"]))
        with self.store.transaction() as db:
            pointer = db.execute("SELECT revision FROM cp_active WHERE component='gateway'").fetchone()
            self.assertEqual(pointer[0], revision["id"])
        snapshot = self.http("GET", "/api/manage/components", role="viewer")
        gateway = next(item for item in snapshot["components"] if item["id"] == "gateway")
        self.assertEqual(gateway["active_policy"], dict(changed, hash=revision["hash"]))
        self.assertFalse(gateway["runtime_observed"])
        self.assertFalse(gateway["business_config_implemented"])

    def test_workspace_read_save_stale_hash_and_no_source_writes(self):
        info = self.new_workspace()
        target = "/api/dev/workspaces/" + info["id"]
        self.assertFalse(info["publish_enabled"])
        self.assertFalse(info["authoritative_tests_from_workspace"])
        self.assertEqual({item["path"] for item in info["files"]},
                         set(self.source_text) - {"control_plane/registry.py"})
        self.assertTrue(Path(info["root"]).is_relative_to(self.directories["workspace"]))
        file = self.http("GET", target + "/file?" + urlencode({"path": "gateway/page.html"}), role="author")
        self.assertTrue(file["editable"])
        text = "<p>synthetic edited workspace only</p>\n"
        payload = {"path": file["path"], "expected_sha256": file["sha256"], "content": text}
        result = self.http("PATCH", target + "/file", role="author", payload=payload)
        self.assertTrue(result["backup_retained"])
        self.assertFalse(result["publish_enabled"])
        self.assertNotEqual(result["source_hash"], info["source_hash"])
        self.http("PATCH", target + "/file", 409, role="author", payload=payload)
        saved = self.http("GET", target + "/file?" + urlencode({"path": file["path"]}), role="author")
        self.assertEqual(saved["content"], text)
        self.assertEqual(saved["sha256"], hashlib.sha256(text.encode()).hexdigest())
        protected = self.http("GET", target + "/file?" + urlencode({"path": "common/__init__.py"}), role="author")
        self.http("PATCH", target + "/file", 403, role="author", payload={
            "path": protected["path"], "expected_sha256": protected["sha256"], "content": "# denied\n"})
        self.http("GET", target, 403, role="viewer")
        diff = self.http("GET", target + "/diff", role="author")
        self.assertEqual(diff["format"], "plain_text")
        self.assertEqual(len(diff["changes"]), 1)
        self.assertFalse(diff["publish_enabled"])
        self.assertEqual(self._source_identity(), self.source_before)
        self.assertFalse(any(path.endswith((".key", ".db", ".sqlite")) for path in
                             (item["path"] for item in info["files"])))

    def test_signed_status_exact_replay_persists_after_controller_reopen(self):
        client = self.protocol.ControllerClient(timeout=6)
        value = client.request("GET", "/v1/status", {}, self.tokens["viewer"])
        self.assertFalse(value["enrolled"])
        self.assertEqual(value["readiness"], "not_enrolled")
        self.assertEqual(value["runtimes"], {})
        body = self.registry.canonical({"token": self.tokens["viewer"]})
        headers = self.protocol.request_headers(self.key, "GET", "/v1/status", body)
        headers.update({"Host": "127.0.0.1:%d" % self.controller_server.server_port,
                        "Content-Length": str(len(body)), "Connection": "close"})
        status, value = self._signed_request(body, headers)
        self.assertEqual(status, 200)
        self.assertFalse(value["enrolled"])
        status, value = self._signed_request(body, headers)
        self.assertEqual(status, 409)
        self.assertEqual(value["error"], "request replay rejected")
        old_controller = self.controller
        self._restart_controller_state()
        self.assertIsNot(old_controller, self.controller)
        status, value = self._signed_request(body, headers)  # Exact same body AND all headers.
        self.assertEqual(status, 409)
        self.assertEqual(value["error"], "request replay rejected")
        with self.controller.state.transaction() as db:
            count = db.execute("SELECT count(*) FROM nonces WHERE nonce=? AND request_id=?",
                               (headers["X-Control-Nonce"], headers["X-Control-Request-ID"])).fetchone()[0]
            self.assertEqual(count, 1)
        damaged = self.protocol.request_headers(self.key, "GET", "/v1/status", body)
        damaged["X-Control-Signature"] = "0" * 64
        status, value = self._signed_request(body, damaged)
        self.assertEqual(status, 401)
        self.assertEqual(value["error"], "invalid request authentication")

    def test_controller_signed_host_rejections_preserve_nonce_until_valid_retry(self):
        body = self.registry.canonical({"token": self.tokens["viewer"]})
        headers = self.protocol.request_headers(self.key, "GET", "/v1/status", body)
        port = self.controller_server.server_port
        headers.update({"Host": "127.0.0.1:%d" % port,
                        "Content-Length": str(len(body)), "Connection": "close"})
        nonce, request_id = headers["X-Control-Nonce"], headers["X-Control-Request-ID"]

        def nonce_counts():
            # Independent read-only connections observe durable dedup, not a
            # controller accept() double or merely the HTTP response status.
            db = sqlite3.connect("file:%s?mode=ro" % self.controller.state.path, uri=True)
            try:
                return db.execute(
                    "SELECT count(*), count(CASE WHEN nonce=? AND request_id=? THEN 1 END) "
                    "FROM nonces WHERE nonce=? OR request_id=?",
                    (nonce, request_id, nonce, request_id)).fetchone()
            finally:
                db.close()

        self.assertEqual(nonce_counts(), (0, 0), "Fresh signed request must not be in dedup")
        for case, host in (("localhost alias", "localhost:%d" % port),
                           ("wrong port", "127.0.0.1:%d" % (port % 65535 + 1)),
                           ("external host", "external.invalid:%d" % port),
                           ("missing host", None)):
            # Subtest labels and assertions never contain bodies, headers or tokens.
            with self.subTest(case=case):
                wrong_headers = dict(headers)
                if host is None:
                    del wrong_headers["Host"]
                else:
                    wrong_headers["Host"] = host
                # Raw TCP ensures missing Host is not filled in by http.client.
                # Every case retains the same valid body, HMAC, nonce and ID.
                wire = b"GET /v1/status HTTP/1.1\r\n"
                wire += b"".join(("%s: %s\r\n" % item).encode("ascii")
                                 for item in wrong_headers.items()) + b"\r\n" + body
                status, response_headers, response_body = self._wire(port, wire)
                self.assertEqual(status, 400, "Signed wrong-Host request must return 400")
                self.protocol.verify_response(self.key, request_id, nonce, status,
                                              response_body, response_headers)
                value = self.protocol.strict_json(response_body, LIMIT)
                self.assertTrue(value.get("error") == "Host does not match controller endpoint",
                                "Expected the signed endpoint Host rejection")
                self.assertEqual(nonce_counts(), (0, 0), "Rejected Host must not consume nonce or ID")

        # Change only Host back: authentication and the real authorized route
        # must succeed, proving the rejection cases were not unsigned 401s.
        status, value = self._signed_request(body, headers)
        self.assertEqual(status, 200, "Correct Host retry must succeed with the original signature")
        self.assertTrue(value.get("enrolled") is False, "Expected the real unenrolled status route")
        self.assertEqual(nonce_counts(), (1, 1), "Only the successful retry may enter durable dedup")
        status, value = self._signed_request(body, headers)  # Exact same body and all headers.
        self.assertEqual(status, 409, "Exact accepted-request replay must return 409")
        self.assertTrue(value.get("error") == "request replay rejected", "Expected durable replay rejection")
        self.assertEqual(nonce_counts(), (1, 1), "Replay must not create another dedup entry")

    def test_controller_real_framing_rejections_are_signed(self):
        body = b"{}"  # No raw credential appears in malformed wire cases.
        headers = self.protocol.request_headers(self.key, "GET", "/v1/status", body)
        base = ("GET /v1/status HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n" % self.controller_server.server_port).encode()
        for name, value in headers.items():
            base += ("%s: %s\r\n" % (name, value)).encode()
        for extra in (b"Content-Length: 2\r\nContent-Length: 2\r\n",
                      b"Content-Length: 2\r\nTransfer-Encoding: chunked\r\n",
                      b"Content-Length: nope\r\n", b"Content-Length: 2\r\nExpect: 100-continue\r\n"):
            status, response_headers, response_body = self._wire(
                self.controller_server.server_port, base + extra + b"\r\n" + body)
            self.assertEqual(status, 400)
            self.protocol.verify_response(self.key, headers["X-Control-Request-ID"],
                                          headers["X-Control-Nonce"], status, response_body, response_headers)
        status, response_headers, response_body = self._wire(
            self.controller_server.server_port, base + b"Content-Length: 32769\r\n\r\n")
        self.assertEqual(status, 413)
        self.protocol.verify_response(self.key, headers["X-Control-Request-ID"],
                                      headers["X-Control-Nonce"], status, response_body, response_headers)

    def test_dev_build_and_test_refuse_without_fake_jobs(self):
        info = self.new_workspace()
        self.http("POST", "/api/dev/jobs", 409, role="author", payload={
            "workspace_id": info["id"], "expected_hash": "0" * 64, "kind": "build"})
        # Real prepare checks enrollment before build provisioning; status/legacy build are summaries.
        for kind, reason in (("build", "not_enrolled"), ("test", "not_enrolled")):
            result = self.http("POST", "/api/dev/jobs", 503, role="author", payload={
                "workspace_id": info["id"], "expected_hash": info["source_hash"], "kind": kind})
            self.assertEqual(result["detail"], reason)
            self.assertNotIn("id", result)
        self.http("POST", "/api/dev/jobs", 422, role="author", payload={
            "workspace_id": info["id"], "expected_hash": info["source_hash"], "kind": "build", "command": "forbidden"})
        self.assertEqual(self.http("GET", "/api/dev/jobs", role="author")["jobs"], [])
        with self.controller.state.transaction() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM jobs").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM lease").fetchone()[0], 0)
        self.assertIsNone(self.controller._worker)
        self.assertTrue(not self.controller._tokens, "Refused jobs must not retain credentials")
        self.assertEqual(self.controller.runner.runtime_file, "")
        self.assertEqual(self._source_identity(), self.source_before)

    def test_operator_runtime_actions_only_default_unenrolled_refusal(self):
        revision = self.approved_config(activate=True)
        for action in ("start", "stop", "restart"):
            value = self.http("POST", "/api/manage/actions", 503, role="operator", payload={
                "component": "gateway", "action": action, "expected_config_hash": revision["hash"]})
            self.assertEqual(value["detail"], "not_enrolled")
            self.assertNotIn("observed", value)
        self.http("POST", "/api/manage/actions", 404, role="operator", payload={
            "component": "unknown-runtime", "action": "start", "expected_config_hash": revision["hash"]})
        self.http("POST", "/api/manage/actions", 403, role="viewer", payload={
            "component": "gateway", "action": "start", "expected_config_hash": revision["hash"]})
        with self.controller.state.transaction() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM lease").fetchone()[0], 0)
        self.assertEqual(self.controller.runner.status()["runtimes"], {})

    def test_release_register_list_get_and_strict_unverified_manifest(self):
        version = self.version(image="sha256:" + hashlib.sha256(b"unverified synthetic image").hexdigest())
        target = "/api/releases/versions/" + version["id"]
        read = self.http("GET", target, role="viewer")
        self.assertEqual(read["hash"], version["hash"])
        self.assertEqual(read["manifest"], version["manifest"])
        self.assertFalse(read["image_verified"])
        self.assertFalse(read["deployed"])
        listed = self.http("GET", "/api/releases/versions", role="viewer")
        self.assertTrue(any(item["id"] == version["id"] for item in listed["versions"]))
        self.http("POST", "/api/releases/versions", 409, role="author", payload={"manifest": version["manifest"]})
        self.http("POST", "/api/releases/versions", 403, role="viewer", payload={"manifest": version["manifest"]})
        self.http("POST", "/api/releases/versions", 422, role="author", payload=version["manifest"])
        self.http("POST", "/api/releases/versions", 400, role="author", payload={
            "manifest": dict(version["manifest"], image_verified=True)})
        self.http("POST", "/api/releases/versions", 400, role="author", payload={
            "manifest": dict(version["manifest"], schema_version=True)})
        self.http("GET", "/api/releases/versions/not-an-id", 404, role="viewer")

    def test_release_plan_create_edit_submit_review_cancel(self):
        version = self.version()
        payload = self.plan_input(version["id"])
        plan = self.http("POST", "/api/releases/plans", 201, role="author", payload={"plan": payload})
        target = "/api/releases/plans/" + plan["id"]
        self.assertEqual(plan["version"], 1)
        self.http("PATCH", target, 409, role="author", payload={"expected_version": 2, "plan": payload})
        payload["expires_at"] += 60
        plan = self.http("PATCH", target, role="author", payload={"expected_version": 1, "plan": payload})
        self.assertEqual(plan["version"], 2)
        self.assertFalse(plan["prechecks_verified"])
        plan = self.http("POST", target + "/submit", role="author", payload={"expected_version": plan["version"]})
        self._cli("grant", "--name", "author", "--permission", "review")
        try:
            self.http("POST", target + "/review", 403, role="author", payload={
                "expected_version": plan["version"], "decision": "approve"})
        finally:
            self._cli("revoke", "--name", "author", "--permission", "review")
        self.http("POST", target + "/review", 422, role="reviewer", payload={
            "expected_version": plan["version"], "decision": "approved"})
        plan = self.http("POST", target + "/review", role="reviewer", payload={
            "expected_version": plan["version"], "decision": "approve"})
        self.assertEqual(plan["state"], "approved")
        self.assertEqual(plan["approved_hash"], plan["hash"])
        self.http("PATCH", target, 409, role="author", payload={"expected_version": plan["version"], "plan": payload})
        self.http("POST", target + "/cancel", 403, role="viewer", payload={"expected_version": plan["version"]})
        plan = self.http("POST", target + "/cancel", role="author", payload={"expected_version": plan["version"]})
        self.assertEqual(plan["state"], "cancelled")
        self.assertFalse(plan["deployed"])
        self.assertFalse(plan["image_verified"])
        self.assertEqual(self.http("GET", target, role="viewer")["state"], "cancelled")
        self.assertTrue(any(item["id"] == plan["id"] for item in
                            self.http("GET", "/api/releases/plans", role="viewer")["plans"]))

    def test_release_reject_is_cancelled_not_deployed(self):
        version = self.version()
        plan = self.http("POST", "/api/releases/plans", 201, role="author",
                         payload={"plan": self.plan_input(version["id"])})
        target = "/api/releases/plans/" + plan["id"]
        plan = self.http("POST", target + "/submit", role="author", payload={"expected_version": plan["version"]})
        plan = self.http("POST", target + "/review", role="reviewer",
                         payload={"expected_version": plan["version"], "decision": "reject"})
        self.assertEqual(plan["state"], "cancelled")
        self.assertIsNone(plan["approved_hash"])
        self.assertFalse(plan["deployed"])
        self.http("POST", target + "/execute", 403, role="operator", payload={"expected_version": plan["version"]})

    def test_release_execute_503_persists_blocked_request_and_audit(self):
        version = self.version()
        plan = self.approved_plan(version)
        target = "/api/releases/plans/" + plan["id"]
        self.http("POST", target + "/execute", 403, role="viewer", payload={"expected_version": plan["version"]})
        self.http("POST", target + "/execute", 422, role="operator", payload={
            "expected_version": plan["version"], "executor": "forbidden"})
        result = self.http("POST", target + "/execute", 503, role="operator", payload={"expected_version": plan["version"]})
        self.assertEqual(set(result), {"detail", "request_id"})
        self.assertRegex(result["request_id"], r"\A[0-9a-f]{32}\Z")
        self.assertEqual(result["detail"], "Execution withheld: trusted-local-attestation-required")
        request_id = result["request_id"]
        # Independent connection, after the 503, proves the transaction committed.
        db = sqlite3.connect("file:%s?mode=ro" % self.release_db, uri=True)
        try:
            row = db.execute("SELECT id,plan_id,actor,plan_hash,version_hash,state,blocker FROM release_requests WHERE id=?",
                             (request_id,)).fetchone()
            self.assertEqual(row, (request_id, plan["id"], self.users["operator"]["id"], plan["hash"],
                                   version["hash"], "blocked", "trusted-local-attestation-required"))
            self.assertEqual(db.execute("SELECT request_id,expires FROM release_lease WHERE slot=1").fetchone(), (None, 0))
            self.assertEqual(db.execute("SELECT count(*) FROM release_attestations").fetchone()[0], 0)
        finally:
            db.close()
        requests = self.http("GET", "/api/releases/requests", role="viewer")["requests"]
        request = next(item for item in requests if item["id"] == request_id)
        self.assertEqual(request["state"], "blocked")
        self.assertFalse(request["execution_ready"])
        self.assertFalse(request["deployed"])
        blocked = self.http("GET", target, role="viewer")
        self.assertEqual(blocked["state"], "blocked")
        self.assertEqual(blocked["version"], plan["version"] + 1)
        self.assertFalse(blocked["image_verified"])
        self.assertFalse(blocked["data_rollback_supported"])
        self.http("POST", target + "/execute", 409, role="operator", payload={"expected_version": blocked["version"]})
        requests = self.http("GET", "/api/releases/requests", role="viewer")["requests"]
        self.assertEqual(sum(item["plan_id"] == plan["id"] for item in requests), 1)
        audit = self.http("GET", "/api/releases/audit", role="viewer")["audit"]
        self.assertTrue(any(item["event"] == "release.execution-blocked" and item["target"] == plan["id"] for item in audit))
        self.assertIsNone(self.controller._worker)

    def test_rollback_unverified_lineage_or_schema_cannot_restore_database(self):
        target = self.version(image="sha256:" + "a" * 64)
        current = self.version(previous=target["id"], image="sha256:" + "b" * 64)
        now = int(time.time())
        window = {"planned_at": now - 5, "expires_at": now + 900}
        self.http("POST", "/api/releases/rollback-plans", 409, role="author", payload={
            "version_id": target["id"], "current_version_id": current["id"], "window": window})
        incompatible = self.version(previous=current["id"], schema=2, image="sha256:" + "c" * 64)
        self.http("POST", "/api/releases/rollback-plans", 409, role="author", payload={
            "version_id": current["id"], "current_version_id": incompatible["id"], "window": window})
        with self.store.transaction() as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], self.store.SCHEMA_VERSION)
        self.assertEqual(self._source_identity(), self.source_before)
        plans = self.http("GET", "/api/releases/plans", role="viewer")["plans"]
        self.assertFalse(any(item["payload"]["kind"] == "rollback" for item in plans))
        self.assertFalse(any(item["deployed"] or item["data_rollback_supported"] for item in plans))

    def test_no_http_attest_initialize_success_or_business_routes(self):
        version = self.version()
        plan = self.approved_plan(version)
        paths = ("/attest", "/initialize", "/mark_success", "/api/releases/initialize",
                 "/api/releases/attest", "/api/releases/mark_success",
                 "/api/releases/versions/%s/attest" % version["id"],
                 "/api/releases/plans/%s/mark_success" % plan["id"],
                 "/api/auth/signup", "/api/auth/users", "/api/dev/publish", "/api/cards")
        for path in paths:
            for method in ("GET", "POST"):
                self.http(method, path, 404, role="operator", payload={} if method == "POST" else None)
        self.http("GET", "/docs", 404)
        self.http("GET", "/openapi.json", 404)
        self.assertFalse(self.http("GET", "/api/releases/versions/" + version["id"], role="viewer")["deployed"])

    def test_manage_and_release_audit_http_reads_never_export_credentials(self):
        revision = self.approved_config()
        management = self.http("GET", "/api/manage/audit", role="viewer")
        self.assertTrue(any(item["event"] == "policy.review" and item["target"] == revision["id"]
                            for item in management["audit"]))
        release = self.http("GET", "/api/releases/audit", role="viewer")
        self.assertTrue(any(item["event"] == "release.initialize" for item in release["audit"]))
        encoded = json.dumps({"management": management, "release": release})
        report = json.dumps(self.http("GET", "/api/manage/report", role="viewer"))
        for secret in list(self.tokens.values()) + list(self.passwords.values()) + [self.key.hex()]:
            self.assertTrue(secret not in encoded and secret not in report,
                            "Audit/report exports must not contain raw credentials")
        self.assertNotIn("Authorization", encoded)
        self.assertNotIn("request_timeout_s", encoded)
        self.assertNotIn(str(self.directories["source"]), encoded)
        self.http("GET", "/api/manage/audit", 401)
        self.http("GET", "/api/manage/report", 401)
        self.http("GET", "/api/releases/audit", 401)
        self.http("GET", "/api/releases/requests", 401)


if __name__ == "__main__":
    unittest.main(verbosity=2)
