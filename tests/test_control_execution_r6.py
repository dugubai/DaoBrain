"""Pure stdlib checks. No Docker, SSH, socket connections, or downloaded fixtures."""
import ast
import contextlib
import copy
import hashlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from collections import namedtuple
from email.message import Message
from unittest import mock

from control_plane import controller, protocol, runner
from control_plane.registry import ControlError, canonical, digest

KEY = b"k" * 32
TOKEN = "a" * 43
OTHER_TOKEN = "b" * 43
HASH = "c" * 64
IMAGE = "sha256:" + "d" * 64
CONTAINER = "e" * 64
JOB_CONTAINER = "f" * 64
Principal = namedtuple("Principal", "id name permissions")


def write_file(path, data, mode=0o600):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as stream:
        stream.write(data)
    os.chmod(path, mode)


def command_result(output=b"", code=0, timed_out=False, truncated=False, seen=None):
    return runner.CommandResult(code, output, len(output) if seen is None else seen,
                                truncated, timed_out)


class FakeSecurity:
    def __init__(self):
        self.calls = []
        self.revoked = False
        self.expire_after = None

    def authenticate(self, token, permission=None):
        self.calls.append((token, permission))
        if self.revoked or (self.expire_after is not None and len(self.calls) > self.expire_after):
            raise ControlError("expired", 401)
        if token not in (TOKEN, OTHER_TOKEN):
            raise ControlError("invalid", 401)
        return Principal("alice" if token == TOKEN else "bob", "user", ("view", "operate", "develop"))


class FakeStore:
    def __init__(self):
        self.policy = {"config": {"enabled": True, "request_timeout_s": 30}, "hash": HASH}

    @contextlib.contextmanager
    def transaction(self):
        yield None

    def active_policy(self, component):
        return self.policy


class FakeWorkspace:
    def __init__(self):
        self.owner = "alice"
        self.hash = HASH
        self.frozen = []

    def describe(self, workspace_id, actor_id):
        return {"id": workspace_id, "owner": self.owner, "source_hash": self.hash,
                "root": "/never-mount-live", "files": ["gateway/main.py"]}

    def snapshot(self, workspace_id, actor_id, expected_hash):
        self.frozen.append((workspace_id, actor_id, expected_hash))
        return {"owner": self.owner, "source_hash": self.hash, "root": "/immutable-fake",
                "files": ["gateway/main.py"]}


class FakeRunner:
    def __init__(self):
        self.actions = []
        self.tests = []
        self.snapshots = []
        self.block = None

    def status(self):
        return {"enrolled": False, "readiness": "not_enrolled"}

    def action(self, runtime, action, expected, authorize=None):
        if authorize:
            authorize()
        self.actions.append((runtime, action, expected))
        return {"running": action != "stop", "readiness": "unknown"}

    def prepare_test(self, snapshot):
        self.snapshots.append(snapshot)
        return {"prepared": True}

    def run_test(self, prepared, job_id, record_container, authorize=None):
        if self.block:
            self.block.wait(3)
        if authorize:
            authorize()
        self.tests.append(job_id)
        record_container(JOB_CONTAINER)
        return {"status": "succeeded", "exit_code": 0, "output_bytes": 0,
                "output_truncated": False, "output_retained": False}

    def build(self, snapshot=None):
        raise ControlError("isolated builder not provisioned", 503)


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self.temp.name)
        self.path = os.path.join(self.root, "controller.db")
        self.state = controller.ControllerState(self.path)
        self.security = FakeSecurity()
        self.store = FakeStore()
        self.workspace = FakeWorkspace()
        self.runner = FakeRunner()
        self.control = controller.Controller(KEY, self.state, self.security, self.store, self.workspace, self.runner)

    def tearDown(self):
        if self.runner.block:
            self.runner.block.set()
        self.control.close()
        self.temp.cleanup()

    def call(self, method="GET", path="/v1/status", payload=None, headers=None, body=None):
        body = canonical(payload if payload is not None else {"token": TOKEN}) if body is None else body
        headers = headers or protocol.request_headers(KEY, method, path, body)
        status, response, response_auth = self.control.handle(method, path, body, headers)
        protocol.verify_response(KEY, headers["X-Control-Request-ID"], headers["X-Control-Nonce"],
                                 status, response, response_auth)
        return status, protocol.strict_json(response, protocol.MAX_RESPONSE), headers

    def action(self):
        return {"token": TOKEN, "component": "gateway", "action": "start", "expected_config_hash": HASH}

    def job(self):
        return {"token": TOKEN, "workspace_id": "workspace", "expected_hash": HASH, "kind": "test"}

    def test_status_requires_live_view_and_reports_not_enrolled(self):
        status, value, unused = self.call()
        self.assertEqual(status, 200)
        self.assertEqual(self.security.calls[-1][1], "view")
        self.assertEqual(value["readiness"], "not_enrolled")
        self.security.revoked = True
        self.assertEqual(self.call()[0], 401)

    def test_hmac_tamper_never_authenticates(self):
        body = canonical({"token": TOKEN})
        headers = protocol.request_headers(KEY, "GET", "/v1/status", body)
        self.assertEqual(self.call(headers=headers, body=body + b" ")[0], 401)
        self.assertEqual(self.security.calls, [])

    def test_replay_and_request_id_dedup_survive_restart(self):
        body = canonical({"token": TOKEN})
        headers = protocol.request_headers(KEY, "GET", "/v1/status", body)
        self.assertEqual(self.call(body=body, headers=headers)[0], 200)
        self.assertEqual(self.call(body=body, headers=headers)[0], 409)
        self.control.close()
        self.state = controller.ControllerState(self.path)
        self.control = controller.Controller(KEY, self.state, self.security, self.store, self.workspace, self.runner)
        self.assertEqual(self.call(body=body, headers=headers)[0], 409)
        alternate = protocol.request_headers(KEY, "GET", "/v1/status", body,
                                             request_id=headers["X-Control-Request-ID"])
        self.assertEqual(self.call(body=body, headers=alternate)[0], 409)

    def test_atomic_nonce_race_has_one_winner(self):
        outcomes = []
        def accept():
            try:
                self.state.accept("1" * 32, "2" * 32)
                outcomes.append("accepted")
            except ControlError:
                outcomes.append("rejected")
        workers = [threading.Thread(target=accept) for unused in range(8)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        self.assertEqual(outcomes.count("accepted"), 1)

    def test_method_and_path_are_bound(self):
        body = canonical({"token": TOKEN})
        headers = protocol.request_headers(KEY, "GET", "/v1/status", body)
        self.assertEqual(self.call("POST", "/v1/jobs", headers=headers, body=body)[0], 401)
        self.assertEqual(self.call("GET", "/v1/jobs", headers=headers, body=body)[0], 401)

    def test_unknown_exact_route_is_signed_error(self):
        body = canonical({"token": TOKEN})
        path = "/v1/status?x=1"
        headers = protocol.request_headers(KEY, "GET", "/v1/status", body)
        headers["X-Control-Signature"] = protocol.signature(KEY, (
            "request-v1", "GET", path, protocol.body_hash(body), headers["X-Control-Timestamp"],
            headers["X-Control-Nonce"], headers["X-Control-Request-ID"]))
        self.assertEqual(self.call(path=path, headers=headers, body=body)[0], 404)
        self.assertEqual(self.security.calls, [])

    def test_strict_json_and_body_size_rejected_before_dispatch(self):
        for body in (b'{"token":"a","token":"b"}', b'{"x":NaN}', b'{"x":Infinity}',
                     b'{"x":1e999}', b'{"x":"\\ud800"}', b'[]', b'{}junk'):
            self.assertEqual(self.call(body=body)[0], 400)
        self.assertEqual(self.call(body=b" " * (protocol.MAX_BODY + 1))[0], 413)
        self.assertEqual(self.security.calls, [])

    def test_old_and_future_timestamp_rejected(self):
        body = canonical({"token": TOKEN})
        for offset in (-31, 31):
            headers = protocol.request_headers(KEY, "GET", "/v1/status", body,
                                                timestamp=int(time.time()) + offset)
            self.assertEqual(self.call(body=body, headers=headers)[0], 401)

    def test_actions_reauthorize_and_policy_conflicts(self):
        self.assertEqual(self.call("POST", "/v1/actions", self.action())[0], 200)
        self.assertEqual(len(self.runner.actions), 1)
        self.assertGreaterEqual(len(self.security.calls), 3)
        self.store.policy["hash"] = "0" * 64
        self.assertEqual(self.call("POST", "/v1/actions", self.action())[0], 409)
        self.assertEqual(len(self.runner.actions), 1)
        self.assertFalse(self.state.busy()["active"])

    def test_expired_action_does_not_execute(self):
        self.security.expire_after = 1
        self.assertEqual(self.call("POST", "/v1/actions", self.action())[0], 401)
        self.assertEqual(self.runner.actions, [])
        self.assertFalse(self.state.busy()["active"])

    def test_disabled_policy_allows_stop_only(self):
        self.store.policy["config"]["enabled"] = False
        self.assertEqual(self.call("POST", "/v1/actions", self.action())[0], 409)
        action = self.action()
        action["action"] = "stop"
        self.assertEqual(self.call("POST", "/v1/actions", action)[0], 200)

    def test_jobs_frozen_owned_and_no_credentials_on_disk(self):
        status, job, unused = self.call("POST", "/v1/jobs", self.job())
        self.assertEqual(status, 202)
        self.control._worker.join(3)
        job = self.state.jobs("alice", job["id"])
        self.assertEqual(job["status"], "succeeded")
        self.assertEqual(self.workspace.frozen, [("workspace", "alice", HASH)])
        self.assertEqual(self.runner.snapshots[0]["root"], "/immutable-fake")
        self.assertEqual(self.control._tokens, {})
        with open(self.path, "rb") as stream:
            stored = stream.read()
        self.assertNotIn(TOKEN.encode("ascii"), stored)
        self.assertNotIn(b"/immutable-fake", stored)
        self.assertEqual(self.call("GET", "/v1/jobs/" + job["id"], {"token": OTHER_TOKEN})[0], 404)
        self.assertEqual(self.call("GET", "/v1/jobs", {"token": OTHER_TOKEN})[1]["jobs"], [])

    def test_workspace_ownership_hash_and_fields_fail_closed(self):
        self.workspace.owner = "bob"
        self.assertEqual(self.call("POST", "/v1/jobs", self.job())[0], 409)
        self.workspace.owner = "alice"
        self.workspace.hash = "0" * 64
        self.assertEqual(self.call("POST", "/v1/jobs", self.job())[0], 409)
        self.workspace.hash = HASH
        payload = self.job()
        payload["command"] = "touch /tmp/unsafe"
        self.assertEqual(self.call("POST", "/v1/jobs", payload)[0], 400)
        self.assertEqual(self.workspace.frozen, [])

    def test_build_honestly_refused_without_job_or_execution(self):
        payload = self.job()
        payload["kind"] = "build"
        status, value, unused = self.call("POST", "/v1/jobs", payload)
        self.assertEqual(status, 503)
        self.assertIn("isolated builder not provisioned", value["error"])
        self.assertEqual(self.state.jobs("alice"), [])
        self.assertEqual(self.runner.tests, [])

    def test_global_execution_blocks_actions_during_test(self):
        self.runner.block = threading.Event()
        self.assertEqual(self.call("POST", "/v1/jobs", self.job())[0], 202)
        self.assertEqual(self.call("POST", "/v1/actions", self.action())[0], 409)
        self.assertEqual(self.call("POST", "/v1/jobs", self.job())[0], 409)
        self.assertEqual(self.runner.actions, [])
        self.runner.block.set()
        self.control._worker.join(3)
        self.assertFalse(self.state.busy()["active"])

    def test_expired_queued_job_not_executed(self):
        job_id = self.state.create_job("alice", "workspace", HASH, "test")
        self.state.acquire(job_id)
        self.control._tokens[job_id] = TOKEN
        self.security.revoked = True
        self.control._execute(job_id, "alice", {})
        self.assertEqual(self.runner.tests, [])
        self.assertEqual(self.state.jobs("alice", job_id)["status"], "failed")
        self.assertFalse(self.state.busy()["active"])

    def test_restart_interrupts_jobs_preserves_lease_no_auto_execution(self):
        job_id = self.state.create_job("alice", "workspace", HASH, "test")
        self.state.running(job_id)
        self.state.acquire(job_id)
        self.state.container(job_id, JOB_CONTAINER)
        self.control.close()
        self.state = controller.ControllerState(self.path, now=lambda: time.time() + 500)
        self.control = controller.Controller(KEY, self.state, self.security, self.store, self.workspace, self.runner)
        self.assertEqual(self.state.jobs("alice", job_id)["status"], "interrupted")
        self.assertTrue(self.state.busy()["recovery_required"])
        self.assertEqual(self.call("POST", "/v1/actions", self.action())[0], 409)
        self.assertEqual(self.runner.tests, [])
        self.assertEqual(self.control._tokens, {})

    def test_process_lock_prevents_second_controller(self):
        with self.assertRaises(ControlError):
            controller.ControllerState(self.path)

    def test_nonce_transaction_rolls_back(self):
        with self.assertRaises(RuntimeError):
            with self.state.transaction() as db:
                db.execute("INSERT INTO nonces VALUES('rollback','id',0)")
                raise RuntimeError("rollback")
        self.state.accept("rollback", "id")

    def test_expiry_at_final_action_authorization_releases_unused_lease(self):
        self.security.expire_after = 2
        self.assertEqual(self.call("POST", "/v1/actions", self.action())[0], 401)
        self.assertEqual(self.runner.actions, [])
        self.assertFalse(self.state.busy()["active"])

    def test_uncertain_action_keeps_persistent_lease(self):
        def uncertain(*args, **kwargs):
            exc = ControlError("uncertain", 503)
            exc.execution_uncertain = True
            raise exc
        self.runner.action = uncertain
        self.assertEqual(self.call("POST", "/v1/actions", self.action())[0], 503)
        self.assertTrue(self.state.busy()["active"])

    def test_thread_start_failure_discards_token_and_queue(self):
        real_start = threading.Thread.start
        def start(worker):
            if worker.name.startswith("Thread") and getattr(worker, "_target", None) == self.control._execute:
                raise RuntimeError("cannot start executor")
            return real_start(worker)
        with mock.patch.object(threading.Thread, "start", start):
            self.assertEqual(self.call("POST", "/v1/jobs", self.job())[0], 503)
        self.assertEqual(self.state.jobs("alice")[0]["status"], "failed")
        self.assertEqual(self.control._tokens, {})
        self.assertFalse(self.state.busy()["active"])

    def test_actual_security_primitive_and_policy_transaction_no_nested_connection(self):
        from control_plane import security as peer_security
        path = os.path.join(self.root, "adapter.db")
        db = sqlite3.connect(path)
        db.executescript("CREATE TABLE cp_users(id TEXT,name TEXT,enabled INTEGER);"
                         "CREATE TABLE cp_sessions(digest TEXT,user_id TEXT,expires INTEGER);"
                         "CREATE TABLE cp_grants(user_id TEXT,permission TEXT);"
                         "CREATE TABLE cp_active(component TEXT,revision TEXT);"
                         "CREATE TABLE cp_revisions(id TEXT,config TEXT,hash TEXT,state TEXT,reviewer TEXT,owner TEXT);")
        config = {"enabled": True, "request_timeout_s": 30}
        policy_hash = digest(config)
        db.execute("INSERT INTO cp_users VALUES('alice','Alice',1)")
        db.execute("INSERT INTO cp_sessions VALUES(?,?,?)", (peer_security.digest_token(TOKEN), "alice", int(time.time()) + 60))
        db.execute("INSERT INTO cp_grants VALUES('alice','operate')")
        db.execute("INSERT INTO cp_active VALUES('gateway','revision')")
        db.execute("INSERT INTO cp_revisions VALUES(?,?,?,'activated','bob','alice')",
                   ("revision", canonical(config).decode("utf-8"), policy_hash))
        db.commit()
        db.close()
        class AdapterStore:
            @contextlib.contextmanager
            def transaction(inner):
                connection = sqlite3.connect(path, isolation_level=None)
                connection.row_factory = sqlite3.Row
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    yield connection
                    connection.commit()
                finally:
                    connection.close()
            def active_policy(inner, key):
                raise AssertionError("must not open a nested transaction")
        class AdapterSecurity:
            authenticate = self.security.authenticate
            _authenticate = staticmethod(peer_security._authenticate)
        self.control.store = AdapterStore()
        self.control.security = AdapterSecurity()
        payload = self.action()
        payload["expected_config_hash"] = policy_hash
        self.assertEqual(self.call("POST", "/v1/actions", payload)[0], 200)
        self.assertEqual(self.runner.actions[0][2], policy_hash)
        db = sqlite3.connect(path)
        db.execute("UPDATE cp_sessions SET expires=0")
        db.commit()
        db.close()
        self.assertEqual(self.call("POST", "/v1/actions", payload)[0], 401)
        self.assertEqual(len(self.runner.actions), 1)

    def concurrent_action_change(self, change, expected_status):
        """Real SQLite commit during blocked preflight; Docker is an explicit fake."""
        from control_plane import security as peer_security
        path = os.path.join(self.root, "concurrent-policy.db")
        db = sqlite3.connect(path)
        db.executescript("CREATE TABLE cp_users(id TEXT,name TEXT,enabled INTEGER);"
                         "CREATE TABLE cp_sessions(digest TEXT,user_id TEXT,expires INTEGER);"
                         "CREATE TABLE cp_grants(user_id TEXT,permission TEXT);"
                         "CREATE TABLE cp_active(component TEXT,revision TEXT);"
                         "CREATE TABLE cp_revisions(id TEXT,config TEXT,hash TEXT,state TEXT,reviewer TEXT,owner TEXT);")
        config = {"enabled": True, "request_timeout_s": 30}
        policy_hash = digest(config)
        db.execute("INSERT INTO cp_users VALUES('alice','Alice',1)")
        db.execute("INSERT INTO cp_sessions VALUES(?,?,?)", (peer_security.digest_token(TOKEN), "alice", int(time.time()) + 60))
        db.execute("INSERT INTO cp_grants VALUES('alice','operate')")
        db.execute("INSERT INTO cp_active VALUES('gateway','revision')")
        db.execute("INSERT INTO cp_revisions VALUES(?,?,?,'activated','bob','alice')",
                   ("revision", canonical(config).decode("utf-8"), policy_hash))
        db.commit()
        db.close()
        transactions = []
        class AdapterStore:
            @contextlib.contextmanager
            def transaction(inner):
                connection = sqlite3.connect(path, timeout=1, isolation_level=None)
                connection.row_factory = sqlite3.Row
                transactions.append(connection)
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    yield connection
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
                finally:
                    connection.close()
            def active_policy(inner, key):
                raise AssertionError("nested policy transaction")
        adapter = AdapterStore()
        class AdapterSecurity:
            _authenticate = staticmethod(peer_security._authenticate)
            @staticmethod
            def authenticate(token, permission):
                with adapter.transaction() as connection:
                    return peer_security._authenticate(connection, token, permission)
        self.control.store = adapter
        self.control.security = AdapterSecurity()
        preflight = threading.Event()
        resume = threading.Event()
        admitted = []
        original_action = self.runner.action
        def blocked(runtime, action, expected, authorize=None):
            preflight.set()
            if not resume.wait(3):
                raise ControlError("test preflight timeout", 503)
            authorize()
            admitted.append(True)
            return original_action(runtime, action, expected)
        self.runner.action = blocked
        payload = self.action()
        payload["expected_config_hash"] = policy_hash
        outcome = []
        errors = []
        def request():
            try:
                outcome.append(self.call("POST", "/v1/actions", payload))
            except Exception as exc:
                errors.append(exc)
        worker = threading.Thread(target=request)
        worker.start()
        try:
            self.assertTrue(preflight.wait(2))
            # A genuine independent writer must commit BEFORE preflight resumes.
            with contextlib.closing(sqlite3.connect(path, timeout=0.5)) as writer:
                change(writer)
                writer.commit()
            self.assertEqual(admitted, [])
        finally:
            resume.set()
            worker.join(4)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(outcome[0][0], expected_status)
        self.assertEqual(admitted, [])
        self.assertEqual(self.runner.actions, [])
        self.assertFalse(self.state.busy()["active"])
        self.assertGreaterEqual(len(transactions), 3)
        self.assertIsNot(transactions[-1], transactions[-2])

    def test_concurrent_grant_revocation_commits_and_blocks_final_admission(self):
        self.concurrent_action_change(lambda db: db.execute("DELETE FROM cp_grants WHERE user_id='alice'"), 403)

    def test_concurrent_session_expiry_blocks_final_admission(self):
        self.concurrent_action_change(lambda db: db.execute("UPDATE cp_sessions SET expires=0"), 401)

    def test_concurrent_user_disable_blocks_final_admission(self):
        self.concurrent_action_change(lambda db: db.execute("UPDATE cp_users SET enabled=0"), 401)

    def test_concurrent_policy_disable_blocks_final_admission(self):
        def change(db):
            config = {"enabled": False, "request_timeout_s": 30}
            db.execute("UPDATE cp_revisions SET config=?,hash=?", (canonical(config).decode("utf-8"), digest(config)))
        self.concurrent_action_change(change, 409)

    def test_http_header_budget_without_socket(self):
        reader = controller.HeaderBudget(io.BytesIO(b"X: " + b"x" * 8192 + b"\r\n"))
        with self.assertRaises(controller.LineTooLong):
            reader.readline(65537)
        handler = controller.ControllerHandler.__new__(controller.ControllerHandler)
        handler.rawrequestline = b"GET " + b"x" * 1100 + b" HTTP/1.0\r\n"
        handler.send_error = mock.Mock()
        self.assertFalse(handler.parse_request())
        handler.send_error.assert_called_once_with(414)

    def http_parser_unit(self, fields, data=b"\r\nbody"):
        """In-memory unit setup; real parent parser/writer, not actual HTTP evidence."""
        handler = controller.ControllerHandler.__new__(controller.ControllerHandler)
        for name, value in fields.items():
            setattr(handler, name, value)
        handler.server = mock.Mock(controller=self.control)
        handler.rfile, handler.wfile = io.BytesIO(data), io.BytesIO()
        self.addCleanup(handler.rfile.close)
        self.addCleanup(handler.wfile.close)
        return handler

    def assert_http_parser_unit_error(self, handler, status):
        original = handler.rfile
        self.assertFalse(handler.parse_request())
        self.assertIs(handler.rfile, original)
        head, body = handler.wfile.getvalue().split(b"\r\n\r\n", 1)
        lines = head.decode("ascii").split("\r\n")
        self.assertEqual(lines[0], "HTTP/1.0 %d %s" % (status, handler.responses[status][0]))
        headers = dict(line.split(": ", 1) for line in lines[1:])
        self.assertEqual(body, b'{"error":"invalid internal HTTP request"}')
        self.assertEqual(int(headers["Content-Length"]), len(body))
        self.assertEqual(headers["Connection"], "close")
        self.assertTrue(handler.close_connection)
        protocol.verify_response(KEY, "0" * 32, "0" * 32, status, body, headers)

    def test_http_parser_requestline_boundaries_without_socket(self):
        for attribute in ("raw_requestline", "rawrequestline"):
            for size in (1023, 1024, 1025):
                with self.subTest(attribute=attribute, size=size):
                    line = b"GET /" + b"x" * (size - len(b"GET / HTTP/1.0\r\n")) + b" HTTP/1.0\r\n"
                    self.assertEqual(len(line), size)
                    handler = self.http_parser_unit(
                        {attribute: line}, b"Host: 127.0.0.1:18127\r\n\r\nbody")
                    original = handler.rfile
                    if size > 1024:
                        self.assert_http_parser_unit_error(handler, 414)
                        self.assertEqual(original.tell(), 0)
                    else:
                        self.assertTrue(handler.parse_request())
                        self.assertIs(handler.rfile, original)
                        self.assertEqual(handler.raw_requestline, line)
                        self.assertEqual(handler.command, "GET")
                        self.assertEqual(handler.path, line.split()[1].decode("ascii"))
                        self.assertEqual(handler.request_version, "HTTP/1.0")
                        self.assertEqual(handler.headers["Host"], "127.0.0.1:18127")
                        self.assertEqual(original.read(), b"body")
                        self.assertEqual(handler.wfile.getvalue(), b"")

    def test_http_parser_modern_attribute_precedes_legacy_without_socket(self):
        valid = b"GET /modern HTTP/1.0\r\n"
        long = b"GET /" + b"x" * 1024 + b" HTTP/1.0\r\n"
        for modern, legacy, status in ((valid, long, None), (valid, None, None),
                                       (long, valid, 414), (None, valid, 400),
                                       (b"", valid, 400)):
            with self.subTest(modern=modern, legacy=legacy):
                handler = self.http_parser_unit({"raw_requestline": modern, "rawrequestline": legacy})
                original = handler.rfile
                if status is not None:
                    self.assert_http_parser_unit_error(handler, status)
                    self.assertEqual(original.tell(), 0)
                else:
                    self.assertTrue(handler.parse_request())
                    self.assertEqual(handler.path, "/modern")
                    self.assertEqual(handler.rawrequestline, legacy)
                    self.assertEqual(handler.wfile.getvalue(), b"")
                self.assertIs(handler.rfile, original)

    def test_http_parser_missing_or_malformed_fields_signed_400_without_socket(self):
        fields = [{}]
        for attribute in ("raw_requestline", "rawrequestline"):
            fields.extend({attribute: value} for value in (
                None, "GET / HTTP/1.0\r\n", 1025, bytearray(b"GET / HTTP/1.0\r\n"),
                memoryview(b"GET / HTTP/1.0\r\n"), b"", b"\r\n", b" \t\r\n"))
        for values in fields:
            with self.subTest(fields=values):
                handler = self.http_parser_unit(values)
                self.assert_http_parser_unit_error(handler, 400)
                self.assertEqual(handler.rfile.tell(), 0)

    def test_http_parser_parent_syntax_failure_restores_stream_without_socket(self):
        handler = self.http_parser_unit({"raw_requestline": b"GET / extra HTTP/1.0\r\n"})
        self.assert_http_parser_unit_error(handler, 400)
        self.assertEqual(handler.rfile.tell(), 0)

    def test_http_parser_header_budget_boundaries_without_socket(self):
        for attribute in ("raw_requestline", "rawrequestline"):
            for aggregate in (False, True):
                for size in (8191, 8192, 8193):
                    with self.subTest(attribute=attribute, aggregate=aggregate, size=size):
                        prefix = b"X-A: " + b"x" * 4000 + b"\r\nX-B: " if aggregate else b"X: "
                        headers = prefix + b"x" * (size - len(prefix) - 4) + b"\r\n\r\n"
                        self.assertEqual(len(headers), size)
                        handler = self.http_parser_unit(
                            {attribute: b"GET / HTTP/1.0\r\n"}, headers + b"body")
                        original = handler.rfile
                        if size > 8192:
                            self.assert_http_parser_unit_error(handler, 431)
                        else:
                            self.assertTrue(handler.parse_request())
                            self.assertEqual(len(handler.headers), 2 if aggregate else 1)
                            self.assertEqual(handler.wfile.getvalue(), b"")
                        self.assertIs(handler.rfile, original)
                        self.assertEqual(original.read(), b"body")

    def test_http_parser_stream_exception_restores_original_without_socket(self):
        handler = self.http_parser_unit({"raw_requestline": b"GET / HTTP/1.0\r\n"})
        original = mock.Mock(spec=io.BytesIO)
        original.readline.side_effect = OSError("unit stream unavailable")
        handler.rfile = original
        with self.assertRaisesRegex(OSError, "unit stream unavailable"):
            handler.parse_request()
        self.assertIs(handler.rfile, original)
        self.assertEqual(handler.wfile.getvalue(), b"")

    def test_http_bind_does_not_allow_wildcard_ipv6_or_implicit_port(self):
        for address in (("0.0.0.0", 1234), ("::1", 1234), ("127.0.0.1", 0), ("127.0.0.1", True)):
            with self.assertRaises(ControlError):
                controller.ControllerHTTPServer(address, self.control)

    def test_http_fixed_host_rejects_alias_missing_external_and_wrong_port_before_body(self):
        body = canonical({'token': TOKEN})
        for host in (None, 'localhost:18127', 'public.invalid:18127', '127.0.0.1:18128',
                     '127.0.0.1', '127.0.0.1:018127'):
            with self.subTest(host=host):
                handler = controller.ControllerHandler.__new__(controller.ControllerHandler)
                handler.server = mock.Mock(expected_host='127.0.0.1:18127', controller=self.control)
                handler.headers = Message()
                handler.headers['Content-Type'] = 'application/json'
                handler.headers['Content-Length'] = str(len(body))
                if host is not None:
                    handler.headers['Host'] = host
                handler.rfile = io.BytesIO(body)
                handler._respond = mock.Mock()
                handler._request()
                handler._respond.assert_called_once_with(
                    400, canonical({'error': 'Host does not match controller endpoint'}))
                self.assertEqual(handler.rfile.tell(), 0)
                self.assertEqual(self.security.calls, [])
        with self.state.transaction() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM nonces').fetchone()[0], 0)

    def test_http_fixed_host_allows_real_signed_dispatch_without_socket(self):
        body = canonical({'token': TOKEN})
        signed = protocol.request_headers(KEY, 'GET', '/v1/status', body)
        handler = controller.ControllerHandler.__new__(controller.ControllerHandler)
        handler.server = mock.Mock(expected_host='127.0.0.1:18127', controller=self.control)
        handler.headers = Message()
        for name, value in dict(signed, Host='127.0.0.1:18127',
                                **{'Content-Length': str(len(body)), 'Content-Type': 'application/json'}).items():
            handler.headers[name] = value
        handler.command, handler.path = 'GET', '/v1/status'
        handler.rfile = io.BytesIO(body)
        handler._respond = mock.Mock()
        handler._request()
        status, response, authentication = handler._respond.call_args[0]
        self.assertEqual(status, 200)
        protocol.verify_response(KEY, signed['X-Control-Request-ID'], signed['X-Control-Nonce'],
                                 status, response, authentication)
        self.assertEqual(self.security.calls[-1], (TOKEN, 'view'))
        self.assertEqual(handler.rfile.tell(), len(body))

    def test_http_framing_rejects_duplicate_transfer_encoding_and_size_without_network(self):
        for pairs in ((('Content-Length', '1'), ('Content-Length', '1')),
                      (('Content-Length', '1'), ('Transfer-Encoding', 'chunked')),
                      (('Content-Length', str(protocol.MAX_BODY + 1)),),
                      (('Content-Length', '1'), ('Expect', '100-continue'))):
            handler = controller.ControllerHandler.__new__(controller.ControllerHandler)
            handler.headers = Message()
            handler.headers['Content-Type'] = 'application/json'
            for key, value in pairs:
                handler.headers[key] = value
            handler.rfile = io.BytesIO(b'{}')
            handler._respond = mock.Mock()
            handler._request()
            self.assertIn(handler._respond.call_args[0][0], (400, 413))
            self.assertEqual(handler.rfile.tell(), 0)


class ProtocolTests(unittest.TestCase):
    def test_private_key_symlink_hardlink_permissions_size(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "key")
            write_file(path, KEY)
            self.assertEqual(protocol.load_key(path), KEY)
            os.chmod(path, 0o644)
            with self.assertRaises(ControlError):
                protocol.load_key(path)
            os.chmod(path, 0o600)
            os.link(path, path + "-hard")
            with self.assertRaises(ControlError):
                protocol.load_key(path)
            os.symlink(path, path + "-sym")
            with self.assertRaises(ControlError):
                protocol.load_key(path + "-sym")
            write_file(os.path.join(root, "short"), b"x" * 31)
            with self.assertRaises(ControlError):
                protocol.load_key(os.path.join(root, "short"))
        with self.assertRaises(ControlError):
            protocol.load_key("relative")

    def test_only_explicit_canonical_ipv4_loopback_http(self):
        self.assertEqual(protocol.controller_url("http://127.0.0.1:18092"), "http://127.0.0.1:18092")
        for value in ("http://localhost:1", "https://127.0.0.1:1", "http://127.0.0.1",
                      "http://[::1]:1", "http://192.0.2.1:1", "http://127.0.0.1:1/",
                      "http://u@127.0.0.1:1", "http://127.0.0.1:01", "http://127.0.0.1:1#x"):
            with self.assertRaises(ControlError):
                protocol.controller_url(value)

    def test_response_hmac_binds_status_body_request_nonce(self):
        request_id, nonce, body = "1" * 32, "2" * 32, b'{"error":"denied"}'
        headers = protocol.response_headers(KEY, request_id, nonce, 403, body)
        protocol.verify_response(KEY, request_id, nonce, 403, body, headers)
        for rid, n, status, data in (("3" * 32, nonce, 403, body), (request_id, "3" * 32, 403, body),
                                     (request_id, nonce, 200, body), (request_id, nonce, 403, body + b" ")):
            with self.assertRaises(ControlError):
                protocol.verify_response(KEY, rid, n, status, data, headers)

    def test_client_forged_errors_caps_no_redirect_no_proxy(self):
        class Response(io.BytesIO):
            def __init__(self, body, headers, code):
                super().__init__(body)
                self.headers, self.code = headers, code
        with tempfile.TemporaryDirectory() as root:
            key = os.path.join(root, "key")
            write_file(key, KEY)
            opener = mock.Mock()
            client = protocol.ControllerClient("http://127.0.0.1:18092", key, opener=opener)
            for status in (200, 401, 302, 500):
                opener.open.return_value = Response(b'{"error":"fake"}', {}, status)
                with self.assertRaises(ControlError) as caught:
                    client.request("GET", "/v1/status", {}, TOKEN)
                self.assertEqual(caught.exception.status, 502)
            def signed(req, timeout):
                headers = {k.lower(): v for k, v in req.header_items()}
                body = b'{}'
                response_auth = protocol.response_headers(KEY, headers["x-control-request-id"],
                                                          headers["x-control-nonce"], 403, body)
                return Response(body, response_auth, 403)
            opener.open.side_effect = signed
            with self.assertRaises(ControlError) as caught:
                client.request("GET", "/v1/status", {}, TOKEN)
            self.assertEqual(caught.exception.status, 403)
            opener.open.side_effect = None
            opener.open.return_value = Response(b'x' * (protocol.MAX_RESPONSE + 10), {}, 200)
            with self.assertRaises(ControlError):
                client.request("GET", "/v1/status", {}, TOKEN)
            self.assertIsNone(protocol.NoRedirect().redirect_request(None, None, 302, None, None, "http://example.invalid"))
            with mock.patch.object(protocol.request, "build_opener", return_value=opener) as build:
                protocol.ControllerClient("http://127.0.0.1:18092", key)
                self.assertEqual(build.call_args[0][0].proxies, {})


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self.temp.name)
        self.trusted = os.path.join(self.root, "accepted")
        self.snapshot_root = os.path.join(self.root, "snapshot")
        os.makedirs(self.trusted)
        os.makedirs(self.snapshot_root)
        self.runtime_file = os.path.join(self.root, "runtime.json")
        self.value = {"Id": CONTAINER, "Image": IMAGE,
                      "Config": {"User": "10001:10001", "Entrypoint": ["python"], "Cmd": ["main.py"],
                                 "Labels": {"com.docker.compose.project": "daobrain-r6-unit",
                                            "com.docker.compose.service": "gateway", runner.OWNER_LABEL: "daobrain-r6",
                                            runner.SOURCE_LABEL: HASH, runner.CONFIG_LABEL: "1" * 64}},
                      "HostConfig": {"Memory": 64 * runner.MIB, "MemorySwap": 64 * runner.MIB,
                                     "NanoCpus": 500000000, "PidsLimit": 64, "Privileged": False,
                                     "ReadonlyRootfs": True, "CapDrop": ["ALL"], "CapAdd": None,
                                     "SecurityOpt": ["no-new-privileges"], "NetworkMode": "none",
                                     "PidMode": "", "IpcMode": "private", "RestartPolicy": {"Name": "no"}},
                      "Mounts": [], "NetworkSettings": {"Networks": {}},
                      "State": {"Running": False, "Status": "exited", "ExitCode": 0, "OOMKilled": False}}
        self.manifest = {"project": "daobrain-r6-unit", "source_root": self.trusted,
                         "app_image": IMAGE, "budget_mib": 900,
                         "runtimes": {"gateway": {"container_id": CONTAINER, "source_hash": HASH,
                                                    "config_hash": "1" * 64,
                                                    "inspect_hash": digest(runner.inspection_binding(self.value))}}}
        self.commands = []
        self.test_value = None
        self.test_result = command_result()
        self.image_config = {"Volumes": None, "Env": ["PATH=/usr/bin:/bin"],
                             "Labels": {"com.daobrain.control.sandbox": "r6-source-only"}}
        self.run = runner.Runner(self.runtime_file, self.trusted, self.fake_command, lambda: 2048)
        self.save_manifest()

    def tearDown(self):
        self.temp.cleanup()

    def save_manifest(self, repin=False):
        if repin:
            self.manifest["runtimes"]["gateway"]["inspect_hash"] = digest(runner.inspection_binding(self.value))
        write_file(self.runtime_file, canonical(self.manifest))

    def fake_command(self, argv, timeout, output_limit):
        self.assertEqual(argv[1], "--host=unix:///var/run/docker.sock")
        argv = [argv[0]] + argv[2:]
        self.commands.append((list(argv), timeout, output_limit))
        if argv[1] == "inspect":
            value = self.value if argv[-1] == CONTAINER else self.test_value
            return command_result(json.dumps([value]).encode("utf-8"))
        if argv[1:3] == ["image", "inspect"]:
            return command_result(json.dumps([{"Id": IMAGE, "Config": self.image_config}]).encode("utf-8"))
        if argv[1] == "create":
            return command_result((JOB_CONTAINER + "\n").encode("ascii"))
        if argv[1:3] == ["start", "--attach"]:
            if not self.test_result.timed_out:
                self.test_value["State"]["Running"] = False
                self.test_value["State"]["Status"] = "exited"
            return self.test_result
        if argv[1] in ("start", "stop", "restart"):
            value = self.value if argv[-1] == CONTAINER else self.test_value
            value["State"]["Running"] = argv[1] != "stop"
            value["State"]["Status"] = "running" if value["State"]["Running"] else "exited"
            return command_result()
        raise AssertionError("unexpected Docker operation " + repr(argv))

    def frozen(self, content=None):
        content = content or {"gateway/main.py": b"VALUE=1\n"}
        metadata = []
        for path, data in sorted(content.items()):
            write_file(os.path.join(self.snapshot_root, path), data)
            metadata.append({"path": path, "sha256": hashlib.sha256(data).hexdigest(),
                             "size": len(data), "editable": path.startswith("gateway/")})
        return {"root": self.snapshot_root, "component": "gateway", "files": metadata,
                "source_hash": digest([{"path": m["path"], "sha256": m["sha256"]} for m in metadata])}

    def fixture(self):
        # Explicit fake suites for envelope tests, not business execution evidence.
        from control_plane.sandbox_test_entry import SUITES
        entry = b"raise SystemExit(0)\n"
        test = b"def test_component(): pass\n"
        write_file(os.path.join(self.trusted, "control_plane/sandbox_test_entry.py"), entry)
        selected = {}
        for name in SUITES:
            path = "tests/" + name
            write_file(os.path.join(self.trusted, path), test)
            selected[path] = hashlib.sha256(test).hexdigest()
        self.manifest["test_entry_hash"] = hashlib.sha256(entry).hexdigest()
        self.manifest["test_fixtures"] = {"gateway": selected}
        self.save_manifest()

    def prepared(self):
        self.fixture()
        prepared = self.run.prepare_test(self.frozen())
        self.test_value = copy.deepcopy(self.value)
        self.test_value["Id"] = JOB_CONTAINER
        self.test_value["Config"].update(User="10001:10001", Entrypoint=["python"],
                                         Cmd=["-I", "/trusted/control_plane/sandbox_test_entry.py"],
                                         Env=prepared["image_env"], WorkingDir="/sandbox")
        self.test_value["Config"]["Labels"] = {runner.OWNER_LABEL: "daobrain-r6", "com.daobrain.control.job": "3" * 32,
                                                runner.SOURCE_LABEL: prepared["source_hash"]}
        self.test_value["HostConfig"].update(Memory=192 * runner.MIB, MemorySwap=192 * runner.MIB,
                                             Tmpfs={"/tmp": "rw,noexec,nosuid,size=32m"}, LogConfig={"Type": "none"})
        self.test_value["Mounts"] = [{"Source": prepared["source"], "Destination": "/sandbox", "RW": False, "Type": "bind"},
                                     {"Source": prepared["trusted"], "Destination": "/trusted", "RW": False, "Type": "bind"}]
        return prepared

    def test_not_enrolled_has_no_docker_call(self):
        self.run.runtime_file = os.path.join(self.root, "missing")
        self.assertEqual(self.run.status()["readiness"], "not_enrolled")
        with self.assertRaises(ControlError):
            self.run.action("gateway", "start", HASH)
        self.assertEqual(self.commands, [])

    def test_labels_protect_legacy_openclaw_other_services_even_if_rehashed(self):
        for key, value in ((runner.OWNER_LABEL, None), ("com.docker.compose.project", "legacy"),
                           ("com.docker.compose.service", "openclaw"), (runner.SOURCE_LABEL, "0" * 64)):
            original = self.value["Config"]["Labels"][key]
            self.value["Config"]["Labels"][key] = value
            self.save_manifest(repin=True)
            with self.assertRaises(ControlError):
                self.run.action("gateway", "stop", HASH)
            self.assertFalse(any(c[0][1] in ("start", "stop", "restart") for c in self.commands))
            self.value["Config"]["Labels"][key] = original
        self.manifest["runtimes"]["openclaw"] = self.manifest["runtimes"]["gateway"]
        self.save_manifest()
        with self.assertRaises(ControlError):
            self.run.enrollment()

    def test_runtime_digest_binds_image_mounts_network_entrypoint_caps(self):
        for mutate in (lambda v: v.update(Image="sha256:" + "0" * 64),
                       lambda v: v["Config"].update(Entrypoint=["sh"]),
                       lambda v: v["Mounts"].append({"Source": "/var/run/docker.sock", "Destination": "/socket"}),
                       lambda v: v["NetworkSettings"].update(Networks={"legacy": {"NetworkID": "x", "Aliases": []}}),
                       lambda v: v["HostConfig"].update(Memory=65 * runner.MIB)):
            original = copy.deepcopy(self.value)
            mutate(self.value)
            with self.assertRaises(ControlError):
                self.run.verify_runtime(self.manifest, "gateway")
            self.value = original

    def test_resources_reserve_aggregate_and_no_unlimited_caps(self):
        for available in (383, 400, None):
            self.run.available = lambda: available
            with self.assertRaises(ControlError):
                self.run.action("gateway", "start", HASH)
        self.run.available = lambda: 2048
        self.value["HostConfig"].update(Memory=800 * runner.MIB, MemorySwap=800 * runner.MIB)
        self.save_manifest(repin=True)
        with self.assertRaises(ControlError):
            self.run.preflight(self.run.enrollment(), extra_mib=192)
        self.value["HostConfig"]["MemorySwap"] = -1
        self.save_manifest(repin=True)
        with self.assertRaises(ControlError):
            self.run.action("gateway", "start", HASH)
        self.assertFalse(any(c[0][1] == "start" for c in self.commands))

    def test_actions_only_full_fixed_id_policy_not_app_configuration(self):
        result = self.run.action("gateway", "restart", HASH)
        operations = [c[0] for c in self.commands if c[0][1] == "restart"]
        self.assertEqual(operations, [["/usr/bin/docker", "restart", "--time", "10", CONTAINER]])
        self.assertEqual(result["running_config_hash"], "1" * 64)
        self.assertNotEqual(result["running_config_hash"], HASH)
        self.assertEqual(result["readiness"], "unknown")
        self.assertEqual(result["health"], "unconfigured")
        self.assertIn("captured_at", result)
        self.value["State"]["Health"] = {"Status": "healthy"}
        self.assertEqual(self.run.status()["runtimes"]["gateway"]["readiness"], "ready")
        with self.assertRaises(ControlError):
            self.run.action("gateway;touch /tmp/x", "start", HASH)

    def test_expiry_between_inspection_and_action(self):
        def denied():
            raise ControlError("expired", 401)
        with self.assertRaises(ControlError):
            self.run.action("gateway", "start", HASH, authorize=denied)
        self.assertFalse(any(c[0][1] == "start" for c in self.commands))

    def test_missing_or_tampered_trusted_fixture_refused(self):
        snapshot = self.frozen()
        with self.assertRaises(ControlError):
            self.run.prepare_test(snapshot)
        self.fixture()
        write_file(os.path.join(self.trusted, "tests/test_card_workflow_r5.py"), b"tampered")
        with self.assertRaises(ControlError):
            self.run.prepare_test(snapshot)
        self.assertFalse(any(c[0][1] == "create" for c in self.commands))

    def test_no_paths_secrets_or_control_modules_in_source_and_hash_rechecked(self):
        for path in ("gateway/../../outside.py", "gateway/a;touch.py", "gateway/.env", "gateway/secret.py",
                     "control_plane/controller.py", "tests/test_fake.py"):
            with self.assertRaises(ControlError):
                self.run._stage({"root": self.snapshot_root, "files": [path], "source_hash": HASH}, self.manifest)
        snapshot = self.frozen()
        write_file(os.path.join(self.snapshot_root, "gateway/main.py"), b"changed")
        with self.assertRaises(ControlError):
            self.run._stage(snapshot, self.manifest)

    def test_no_symlink_or_hardlink_source_and_volume_cap(self):
        path = os.path.join(self.snapshot_root, "gateway/main.py")
        snapshot = self.frozen()
        os.link(path, path + "-link")
        with self.assertRaises(ControlError):
            self.run._stage(snapshot, self.manifest)
        os.symlink(self.trusted, os.path.join(self.snapshot_root, "alias"))
        with self.assertRaises(ControlError):
            runner.source_file(self.snapshot_root, "alias/x.py", 100)
        with self.assertRaises(ControlError):
            runner.source_file(self.snapshot_root, "gateway/main.py", 1)
        with self.assertRaises(ControlError):
            self.run._stage({"root": self.snapshot_root, "files": ["gateway/x.py"] * 513}, self.manifest)

    def test_stages_only_sanitized_source_and_allowlisted_tests_not_full_trusted_root(self):
        self.fixture()
        snapshot = self.frozen({"gateway/main.py": b"VALUE=1\n", "common/__init__.py": b"",
                                "application_hub/__init__.py": b""})
        write_file(os.path.join(self.trusted, "control_plane/controller.py"), b"DO_NOT_MOUNT")
        source, trusted = self.run._stage(snapshot, self.manifest)
        self.assertFalse(os.path.exists(os.path.join(trusted, "control_plane/controller.py")))
        self.assertTrue(os.path.isfile(os.path.join(source, "common/__init__.py")))
        self.assertTrue(os.path.isfile(os.path.join(trusted, "tests/test_card_workflow_r5.py")))
        self.assertEqual(os.stat(os.path.join(source, "gateway/main.py")).st_mode & 0o777, 0o444)

    def test_image_with_whole_repo_or_secret_env_refused(self):
        self.fixture()
        snapshot = self.frozen()
        for config in ({"Volumes": {"/data": {}}}, {"Labels": {}},
                       dict(self.image_config, Env=["CONTROL_KEY_FILE=/private/key"]),
                       dict(self.image_config, OnBuild=["RUN unsafe"])):
            self.image_config = config
            with self.assertRaises(ControlError):
                self.run.prepare_test(snapshot)
        self.assertFalse(any(c[0][1] == "create" for c in self.commands))

    def test_test_container_flags_actual_exit_oom_and_output_metadata_only(self):
        prepared = self.prepared()
        self.test_result = command_result(b"SECRET_UNTRUSTED_OUTPUT", truncated=True, seen=99999)
        ids = []
        result = self.run.run_test(prepared, "3" * 32, ids.append)
        self.assertEqual(result["status"], "succeeded")
        self.assertTrue(result["output_truncated"])
        self.assertFalse(result["output_retained"])
        self.assertNotIn("SECRET_UNTRUSTED_OUTPUT", json.dumps(result))
        self.assertEqual(ids, [JOB_CONTAINER])
        create = next(c[0] for c in self.commands if c[0][1] == "create")
        for flag in ("--network", "--read-only", "--cap-drop", "--security-opt", "--memory", "--memory-swap", "--pids-limit", "--cpus"):
            self.assertIn(flag, create)
        self.assertNotIn("--rm", create)
        self.assertNotIn(self.trusted, create)
        self.test_value["State"].update(ExitCode=137, OOMKilled=True)
        self.assertEqual(self.run.run_test(prepared, "3" * 32, lambda cid: None)["status"], "failed")
        self.assertFalse(any(c[0][1] in ("rm", "exec", "pull", "build", "down") for c in self.commands))

    def test_test_timeout_stops_own_verified_container_and_reports_timed_out(self):
        prepared = self.prepared()
        self.test_value["State"].update(Running=True, Status="running", ExitCode=137)
        self.test_result = command_result(timed_out=True, code=-9)
        result = self.run.run_test(prepared, "3" * 32, lambda cid: None)
        self.assertEqual(result["status"], "timed_out")
        self.assertIn((["/usr/bin/docker", "stop", "--time", "5", JOB_CONTAINER], 10, runner.OUTPUT_LIMIT), self.commands)
        self.assertFalse(self.test_value["State"]["Running"])

    def test_inspect_overflow_or_timeout_refuses_without_actions(self):
        for result in (command_result(truncated=True), command_result(timed_out=True), command_result(code=1)):
            self.run.command = mock.Mock(return_value=result)
            with self.assertRaises(ControlError):
                self.run.action("gateway", "start", HASH)
            self.assertEqual(self.run.command.call_count, 1)

    def test_bounded_streaming_python_output_and_timeout_no_shell(self):
        result = runner.bounded_command([sys.executable, "-I", "-c", "import sys;sys.stdout.write('x'*200000)"],
                                        timeout=5, output_limit=16384)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(len(result.output), 16384)
        self.assertEqual(result.bytes_seen, 200000)
        self.assertTrue(result.truncated)
        result = runner.bounded_command([sys.executable, "-I", "-c", "while True: pass"], timeout=0.05)
        self.assertTrue(result.timed_out)
        self.assertNotEqual(result.returncode, 0)
        with mock.patch.object(runner.subprocess, "Popen", side_effect=OSError("unavailable")) as popen:
            with self.assertRaises(ControlError):
                runner.bounded_command(["/fixed/command", "a;touch /tmp/injected"], timeout=1)
            self.assertFalse(popen.call_args[1]["shell"])
            self.assertEqual(popen.call_args[1]["env"], {"PATH": "/usr/bin:/bin", "LANG": "C"})

    def test_assigned_modules_parse_python36(self):
        if sys.version_info < (3, 8):
            self.skipTest("host AST has no feature_version option")
        for module in (protocol, runner, controller):
            with open(module.__file__, "r") as stream:
                ast.parse(stream.read(), filename=module.__file__, feature_version=(3, 6))
        with open(__file__, "r") as stream:
            ast.parse(stream.read(), filename=__file__, feature_version=(3, 6))


if __name__ == "__main__":
    unittest.main()
