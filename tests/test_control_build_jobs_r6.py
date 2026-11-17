"""Local stdlib controller checks: real SQLite/HMAC, explicitly fake execution.

FakeRunner does not create ZIPs or containers and is NOT Docker/build evidence.
No HTTP servers, sockets, SSH, network, installation, or host configuration.
"""
import ast
import contextlib
import copy
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest import mock

from control_plane import controller, protocol, security
from control_plane.registry import ControlError, canonical, digest

KEY = b"r" * 32
TOKEN = "a" * 43
OTHER_TOKEN = "b" * 43
SOURCE_HASH = "c" * 64
OTHER_HASH = "d" * 64
ARTIFACT_HASH = "e" * 64
CONTAINER_ID = "f" * 64
POLICY = {"enabled": True, "request_timeout_s": 30}
POLICY_HASH = digest(POLICY)
ARTIFACT_FIELDS = ("artifact_sha256", "source_hash", "artifact_bytes", "artifact_files",
                   "kind", "image_built", "release_approved", "output_retained")


def execution_result():
    return {"status": "succeeded", "exit_code": 0, "oom_killed": False,
            "output_bytes": 0, "output_truncated": False, "output_retained": False,
            "metadata_limit": 16384}


def artifact_result(source_hash=SOURCE_HASH):
    result = execution_result()
    result.update(artifact_sha256=ARTIFACT_HASH, source_hash=source_hash,
                  artifact_bytes=1024, artifact_files=2, kind="offline-source-artifact",
                  image_built=False, release_approved=False)
    return result


def invalid_build_execution_results():
    """Complete fake artifacts must never hide missing or contradictory execution."""
    for key in ("exit_code", "oom_killed", "output_bytes", "output_truncated",
                "output_retained", "metadata_limit"):
        metadata = artifact_result()
        del metadata[key]
        yield "missing " + key, metadata
    invalid = {"exit_code": (-1, 1, 137, None, True, False, "0", 0.0),
               "oom_killed": (True, 0, 1, None, "false"),
               "output_bytes": (-1, None, True, "0", 0.0),
               "output_truncated": (None, 0, "false"),
               "output_retained": (True, None, 0),
               "metadata_limit": (0, 1, 16383, 16385, -1, None, True, False, "16384", 16384.0),
               "reason": ("execution rejected", "host recovery required")}
    for key, values in invalid.items():
        for value in values:
            metadata = artifact_result()
            metadata[key] = value
            yield (key, value), metadata


class SQLiteSecurityStore:
    """Local security fixture using the real peer authentication primitive."""
    def __init__(self, path):
        self.path = path
        with self.transaction() as db:
            db.executescript("CREATE TABLE cp_users(id TEXT,name TEXT,enabled INTEGER);"
                             "CREATE TABLE cp_sessions(digest TEXT,user_id TEXT,expires INTEGER);"
                             "CREATE TABLE cp_grants(user_id TEXT,permission TEXT);"
                             "CREATE TABLE cp_active(component TEXT,revision TEXT);"
                             "CREATE TABLE cp_revisions(id TEXT,config TEXT,hash TEXT,state TEXT,reviewer TEXT,owner TEXT);")
            for actor, token in (("alice", TOKEN), ("bob", OTHER_TOKEN)):
                db.execute("INSERT INTO cp_users VALUES(?,?,1)", (actor, actor))
                db.execute("INSERT INTO cp_sessions VALUES(?,?,?)",
                           (security.digest_token(token), actor, int(time.time()) + 3600))
                for permission in ("view", "operate", "develop"):
                    db.execute("INSERT INTO cp_grants VALUES(?,?)", (actor, permission))
            db.execute("INSERT INTO cp_active VALUES('gateway','revision')")
            db.execute("INSERT INTO cp_revisions VALUES(?,?,?,'activated','bob','alice')",
                       ("revision", canonical(POLICY).decode("utf-8"), POLICY_HASH))

    @contextlib.contextmanager
    def transaction(self):
        db = sqlite3.connect(self.path, timeout=1, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
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

    def active_policy(self, component):
        raise AssertionError("real security/policy transaction must not nest")

    def change(self, sql, parameters=()):
        with self.transaction() as db:
            db.execute(sql, parameters)

    def reset_credentials(self):
        with self.transaction() as db:
            db.execute("UPDATE cp_users SET enabled=1")
            db.execute("UPDATE cp_sessions SET user_id='alice',expires=? WHERE digest=?",
                       (int(time.time()) + 3600, security.digest_token(TOKEN)))
            db.execute("DELETE FROM cp_grants WHERE user_id='alice'")
            for permission in ("view", "operate", "develop"):
                db.execute("INSERT INTO cp_grants VALUES('alice',?)", (permission,))


class SQLiteSecurity:
    _authenticate = staticmethod(security._authenticate)

    def __init__(self, store):
        self.store = store
        self.permissions = []

    def authenticate(self, token, permission):
        self.permissions.append(permission)
        with self.store.transaction() as db:
            return security._authenticate(db, token, permission)


class FakeWorkspace:
    """Frozen data contract only; these paths are never opened or mounted."""
    def __init__(self):
        self.owner = "alice"
        self.source_hash = SOURCE_HASH
        self.snapshot_value = None
        self.freeze_hook = None
        self.frozen = []

    def describe(self, workspace_id, actor_id):
        return {"owner": self.owner, "source_hash": self.source_hash,
                "root": "/fake-live-source", "files": ["gateway/main.py"]}

    def snapshot(self, workspace_id, actor_id, expected_hash):
        self.frozen.append((workspace_id, actor_id, expected_hash))
        if self.freeze_hook:
            self.freeze_hook()
        if self.snapshot_value is not None:
            return copy.deepcopy(self.snapshot_value)
        return {"owner": self.owner, "source_hash": self.source_hash, "root": "/fake-frozen-source"}


class FakeRunner:
    """Explicit fake prepare/run contract, no daemon, process, ZIP or source IO."""
    def __init__(self):
        self.prepares = []
        self.runs = []
        self.admitted = []
        self.actions = []
        self.legacy_builds = 0
        self.prepare_error = None
        self.run_error = None
        self.result_override = None
        self.prepared_kind = "untrusted-prepared-kind"
        self.prepare_entered = threading.Event()
        self.prepare_resume = threading.Event()
        self.prepare_resume.set()
        self.run_entered = threading.Event()
        self.run_resume = threading.Event()
        self.run_resume.set()
        self.action_entered = threading.Event()
        self.action_resume = threading.Event()
        self.action_resume.set()

    def _prepare(self, kind, snapshot):
        self.prepares.append((kind, copy.deepcopy(snapshot)))
        self.prepare_entered.set()
        if not self.prepare_resume.wait(5):
            raise ControlError("fake prepare gate timeout", 503)
        if self.prepare_error:
            raise self.prepare_error
        return {"source_hash": snapshot["source_hash"], "kind": self.prepared_kind}

    def prepare_test(self, snapshot):
        return self._prepare("test", snapshot)

    def prepare_build(self, snapshot):
        return self._prepare("build", snapshot)

    def _run(self, kind, prepared, job_id, record_container, authorize):
        self.runs.append((kind, job_id, copy.deepcopy(prepared)))
        self.run_entered.set()
        if not self.run_resume.wait(5):
            raise ControlError("fake execution gate timeout", 503)
        authorize()  # Fake final admission models the fixed runner callback contract.
        self.admitted.append((kind, job_id))
        record_container(CONTAINER_ID)
        if self.run_error:
            raise self.run_error
        result = artifact_result(prepared["source_hash"]) if kind == "build" else execution_result()
        if self.result_override is not None:
            result = copy.deepcopy(self.result_override)
        result.update(stdout="FAKE_PRIVATE_STDOUT", token=TOKEN,
                      artifact_path="/fake-private-artifact/source.zip", path="/fake-frozen-source")
        return result

    def run_test(self, prepared, job_id, record_container, authorize):
        return self._run("test", prepared, job_id, record_container, authorize)

    def run_build(self, prepared, job_id, record_container, authorize):
        return self._run("build", prepared, job_id, record_container, authorize)

    def build(self):
        self.legacy_builds += 1
        raise ControlError("isolated builder not provisioned", 503)

    def action(self, runtime, action, expected, authorize):
        self.action_entered.set()
        if not self.action_resume.wait(5):
            raise ControlError("fake action gate timeout", 503)
        authorize()
        self.actions.append((runtime, action, expected))
        return {"running": True, "readiness": "unknown"}


class BuildJobTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="build-jobs-r6-")
        self.root = os.path.realpath(self.temp.name)
        self.path = os.path.join(self.root, "controller.db")
        self.state = controller.ControllerState(self.path)
        self.store = SQLiteSecurityStore(os.path.join(self.root, "security-fixture.db"))
        self.security = SQLiteSecurity(self.store)
        self.workspace = FakeWorkspace()
        self.runner = FakeRunner()
        self.control = controller.Controller(KEY, self.state, self.security, self.store,
                                             self.workspace, self.runner)
        self.executor_resume = threading.Event()
        self.executor_resume.set()
        self.executor_entered = threading.Event()
        self.spawned = []
        self.request_workers = []

    def tearDown(self):
        self.executor_resume.set()
        self.runner.prepare_resume.set()
        self.runner.run_resume.set()
        self.runner.action_resume.set()
        for worker in self.request_workers:
            worker.join(6)
        self.control.close()
        self.temp.cleanup()

    def call(self, method, path, payload, body=None, headers=None):
        body = canonical(payload) if body is None else body
        headers = protocol.request_headers(KEY, method, path, body) if headers is None else headers
        status, response, authentication = self.control.handle(method, path, body, headers)
        protocol.verify_response(KEY, headers["X-Control-Request-ID"], headers["X-Control-Nonce"],
                                 status, response, authentication)
        return status, protocol.strict_json(response, protocol.MAX_RESPONSE)

    def job_payload(self, kind="build"):
        return {"token": TOKEN, "workspace_id": "workspace", "expected_hash": SOURCE_HASH, "kind": kind}

    def submit(self, kind="build"):
        return self.call("POST", "/v1/jobs", self.job_payload(kind))

    def action(self):
        return self.call("POST", "/v1/actions", {"token": TOKEN, "component": "gateway",
                         "action": "start", "expected_config_hash": POLICY_HASH})

    def wait_worker(self):
        self.control._worker.join(6)
        self.assertFalse(self.control._worker.is_alive())

    @contextlib.contextmanager
    def gated_executor(self):
        self.executor_resume.clear()
        self.executor_entered.clear()
        real_start = threading.Thread.start

        def start(worker):
            if getattr(worker, "_target", None) == self.control._execute:
                target = worker._target
                self.spawned.append(worker._args)

                def gated(*args, **kwargs):
                    self.executor_entered.set()
                    self.executor_resume.wait(5)
                    target(*args, **kwargs)

                worker._target = gated
            return real_start(worker)

        with mock.patch.object(threading.Thread, "start", start):
            try:
                yield
            finally:
                self.executor_resume.set()
                if self.control._worker:
                    self.wait_worker()

    def assert_clean(self, no_jobs=False):
        self.assertEqual(self.control._tokens, {})
        self.assertFalse(self.state.busy()["active"])
        if no_jobs:
            self.assertEqual(self.state.jobs("alice"), [])

    def lease(self):
        with self.state.transaction() as db:
            return db.execute("SELECT owner,container_id FROM lease WHERE singleton=1").fetchone()

    def test_async_202_registered_kind_source_digest_and_private_metadata(self):
        with self.gated_executor():
            status, queued = self.submit()
            self.assertEqual(status, 202)
            self.assertTrue(self.executor_entered.wait(2))
            self.assertEqual(queued["status"], "queued")
            self.assertEqual((queued["kind"], queued["source_hash"]), ("build", SOURCE_HASH))
            self.assertEqual(self.spawned[0][0], queued["id"])
            self.assertEqual(self.spawned[0][3], "build")
            self.assertEqual(self.lease(), (queued["id"], None))
            self.assertEqual(self.control._tokens, {queued["id"]: TOKEN})
            self.assertEqual(self.runner.runs, [])
            self.assertEqual(self.runner.prepares[0][1]["root"], "/fake-frozen-source")
            self.assertEqual(self.runner.prepares[0][1]["files"], ["gateway/main.py"])
        status, job = self.call("GET", "/v1/jobs/" + queued["id"], {"token": TOKEN})
        self.assertEqual(status, 200)
        self.assertEqual(job["status"], "succeeded")
        self.assertEqual(job["result"], {k: v for k, v in artifact_result().items() if k != "status"})
        self.assertEqual(self.runner.admitted, [("build", job["id"])])
        self.assertEqual(self.workspace.frozen, [("workspace", "alice", SOURCE_HASH)])
        with self.state.transaction() as db:
            self.assertEqual(db.execute("SELECT container_id FROM jobs WHERE id=?", (job["id"],)).fetchone()[0],
                             CONTAINER_ID)
        for path in (self.path, self.store.path):
            with open(path, "rb") as stream:
                stored = stream.read()
            for private in (TOKEN, "/fake-frozen-source", "/fake-private-artifact", "FAKE_PRIVATE_STDOUT"):
                self.assertNotIn(private.encode("ascii"), stored)
        self.assertEqual(self.call("GET", "/v1/jobs/" + job["id"], {"token": OTHER_TOKEN})[0], 404)
        self.assert_clean()

    def test_test_and_build_route_independently_of_prepared_kind(self):
        for kind in ("test", "build"):
            self.runner.prepared_kind = "build" if kind == "test" else "test"
            status, job = self.submit(kind)
            self.assertEqual(status, 202)
            self.wait_worker()
            self.assertEqual(self.runner.prepares[-1][0], kind)
            self.assertEqual(self.runner.runs[-1][:2], (kind, job["id"]))
            saved = self.state.jobs("alice", job["id"])
            self.assertEqual(saved["status"], "succeeded")
            self.assertEqual("artifact_sha256" in saved["result"], kind == "build")
            self.assert_clean()
        self.assertEqual(self.runner.legacy_builds, 0)

    def test_missing_or_noncallable_build_interface_keeps_legacy_503(self):
        for name in ("prepare_build", "run_build"):
            for missing in (None, False):
                with self.subTest(name=name, missing=missing):
                    with mock.patch.object(self.runner, name, missing):
                        status, value = self.submit()
                    self.assertEqual(status, 503)
                    self.assertEqual(value["error"], "isolated builder not provisioned")
                    self.assert_clean(no_jobs=True)
        self.assertEqual(self.runner.legacy_builds, 4)
        self.assertEqual(self.workspace.frozen, [])
        self.assertEqual(self.runner.prepares, [])

    def test_prepare_build_not_provisioned_creates_no_job(self):
        self.runner.prepare_error = ControlError("isolated builder not provisioned", 503)
        self.assertEqual(self.submit()[0], 503)
        self.assertEqual(self.runner.legacy_builds, 0)
        self.assertEqual(self.runner.runs, [])
        self.assert_clean(no_jobs=True)

    def test_description_hash_and_owner_rejected_for_both_kinds(self):
        for kind in ("test", "build"):
            for owner, source_hash in (("bob", SOURCE_HASH), ("alice", OTHER_HASH)):
                with self.subTest(kind=kind, owner=owner, source_hash=source_hash):
                    self.workspace.owner, self.workspace.source_hash = owner, source_hash
                    self.assertEqual(self.submit(kind)[0], 409)
                    self.assert_clean(no_jobs=True)
        self.assertEqual(self.workspace.frozen, [])
        self.assertEqual(self.runner.prepares, [])

    def test_snapshot_hash_owner_shape_rejected_for_both_kinds(self):
        for kind in ("test", "build"):
            for snapshot in ({"owner": "bob", "source_hash": SOURCE_HASH},
                             {"owner": "alice", "source_hash": OTHER_HASH},
                             {"source_hash": SOURCE_HASH}, [], "not-a-snapshot"):
                with self.subTest(kind=kind, snapshot=snapshot):
                    self.workspace.snapshot_value = snapshot
                    self.assertEqual(self.submit(kind)[0], 409)
                    self.assert_clean(no_jobs=True)
        self.assertEqual(self.runner.prepares, [])

    def test_freeze_identity_change_is_rejected_before_prepare(self):
        self.workspace.freeze_hook = lambda: self.store.change(
            "UPDATE cp_sessions SET user_id='bob' WHERE digest=?", (security.digest_token(TOKEN),))
        for kind in ("test", "build"):
            self.store.reset_credentials()
            self.assertEqual(self.submit(kind)[0], 401)
            self.assert_clean(no_jobs=True)
        self.assertEqual(self.runner.prepares, [])

    def test_freeze_grant_revocation_is_rejected_before_prepare(self):
        self.workspace.freeze_hook = lambda: self.store.change(
            "DELETE FROM cp_grants WHERE user_id='alice' AND permission='develop'")
        self.assertEqual(self.submit()[0], 403)
        self.assert_clean(no_jobs=True)
        self.assertEqual(self.runner.prepares, [])

    def test_prepare_rejection_and_heartbeat_start_failure_release_temporary_lease(self):
        for kind in ("test", "build"):
            self.runner.prepare_error = RuntimeError("fake staging denied")
            self.assertEqual(self.submit(kind)[0], 503)
            self.assert_clean(no_jobs=True)
        self.runner.prepare_error = None
        with mock.patch.object(threading.Thread, "start", side_effect=RuntimeError("no heartbeat")):
            self.assertEqual(self.submit()[0], 503)
        self.assert_clean(no_jobs=True)
        self.assertEqual(self.runner.runs, [])

    def test_queued_live_revocation_expiry_disable_for_both_kinds(self):
        changes = (("DELETE FROM cp_grants WHERE user_id='alice' AND permission='develop'", ()),
                   ("UPDATE cp_sessions SET expires=0 WHERE digest=?", (security.digest_token(TOKEN),)),
                   ("UPDATE cp_users SET enabled=0 WHERE id='alice'", ()))
        for kind in ("test", "build"):
            for sql, parameters in changes:
                with self.subTest(kind=kind, sql=sql):
                    self.store.reset_credentials()
                    with self.gated_executor():
                        status, job = self.submit(kind)
                        self.assertEqual(status, 202)
                        self.assertEqual(job["status"], "queued")
                        self.store.change(sql, parameters)  # Independent committed real SQLite writer.
                    saved = self.state.jobs("alice", job["id"])
                    self.assertEqual(saved["status"], "failed")
                    self.assertEqual(saved["result"], {"reason": "execution rejected"})
                    self.assert_clean()
        self.assertEqual(self.runner.runs, [])

    def test_final_admission_revocation_expiry_disable_and_identity_change(self):
        changes = (("DELETE FROM cp_grants WHERE user_id='alice' AND permission='develop'", ()),
                   ("UPDATE cp_sessions SET expires=0 WHERE digest=?", (security.digest_token(TOKEN),)),
                   ("UPDATE cp_users SET enabled=0 WHERE id='alice'", ()),
                   ("UPDATE cp_sessions SET user_id='bob' WHERE digest=?", (security.digest_token(TOKEN),)))
        for kind in ("test", "build"):
            for sql, parameters in changes:
                with self.subTest(kind=kind, sql=sql):
                    self.store.reset_credentials()
                    self.runner.run_entered.clear()
                    self.runner.run_resume.clear()
                    status, job = self.submit(kind)
                    self.assertEqual(status, 202)
                    self.assertTrue(self.runner.run_entered.wait(2))
                    self.assertEqual(self.state.jobs("alice", job["id"])["status"], "running")
                    self.store.change(sql, parameters)
                    self.runner.run_resume.set()
                    self.wait_worker()
                    self.assertEqual(self.state.jobs("alice", job["id"])["status"], "failed")
                    self.assert_clean()
        self.assertEqual(self.runner.admitted, [])
        with self.state.transaction() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM jobs WHERE container_id IS NOT NULL").fetchone()[0], 0)

    def test_global_job_lease_blocks_both_job_kinds_and_actions(self):
        for kind in ("test", "build"):
            with self.subTest(kind=kind):
                self.runner.run_entered.clear()
                self.runner.run_resume.clear()
                self.assertEqual(self.submit(kind)[0], 202)
                self.assertTrue(self.runner.run_entered.wait(2))
                self.assertEqual(self.submit("test")[0], 409)
                self.assertEqual(self.submit("build")[0], 409)
                self.assertEqual(self.action()[0], 409)
                self.assertEqual(self.runner.actions, [])
                self.runner.run_resume.set()
                self.wait_worker()
                self.assert_clean()

    def test_action_lease_blocks_both_job_kinds(self):
        self.runner.action_resume.clear()
        outcomes = []
        worker = threading.Thread(target=lambda: outcomes.append(self.action()))
        self.request_workers.append(worker)
        worker.start()
        self.assertTrue(self.runner.action_entered.wait(2))
        self.assertEqual(self.submit("build")[0], 409)
        self.assertEqual(self.submit("test")[0], 409)
        self.assertEqual(self.runner.prepares, [])
        self.runner.action_resume.set()
        worker.join(6)
        self.assertFalse(worker.is_alive())
        self.assertEqual(outcomes[0][0], 200)
        self.assert_clean(no_jobs=True)

    def test_prepare_lease_already_blocks_actions_and_heartbeat_transfers_owner(self):
        self.runner.prepare_resume.clear()
        self.runner.run_resume.clear()
        outcomes = []
        owners = []
        original_renew = self.state.renew

        def renew(owner, duration=210):
            owners.append(owner)
            return original_renew(owner, duration)

        with mock.patch.object(self.state, "renew", renew):
            worker = threading.Thread(target=lambda: outcomes.append(self.submit()))
            self.request_workers.append(worker)
            worker.start()
            self.assertTrue(self.runner.prepare_entered.wait(2))
            temporary = self.lease()[0]
            self.assertEqual(self.action()[0], 409)
            self.runner.prepare_resume.set()
            worker.join(6)
            self.assertFalse(worker.is_alive())
            self.assertEqual(outcomes[0][0], 202)
            job_id = outcomes[0][1]["id"]
            self.assertTrue(self.runner.run_entered.wait(2))
            self.assertEqual(self.lease()[0], job_id)
            self.assertIn(temporary, owners)
            self.assertIn(job_id, owners)
            self.runner.run_resume.set()
            self.wait_worker()
        self.assert_clean()

    def test_worker_lock_serializes_concurrent_test_and_build_submission(self):
        self.runner.run_resume.clear()
        gate = threading.Barrier(3)
        outcomes = []

        def request(kind):
            gate.wait(3)
            outcomes.append(self.submit(kind))

        for kind in ("test", "build"):
            worker = threading.Thread(target=request, args=(kind,))
            self.request_workers.append(worker)
            worker.start()
        gate.wait(3)
        for worker in self.request_workers:
            worker.join(6)
            self.assertFalse(worker.is_alive())
        self.assertEqual(sorted(status for status, value in outcomes), [202, 409])
        self.assertEqual(len(self.runner.prepares), 1)
        self.runner.run_resume.set()
        self.wait_worker()
        self.assert_clean()

    def test_executor_thread_start_failure_discards_tokens_and_marks_failed(self):
        real_start = threading.Thread.start

        def start(worker):
            if getattr(worker, "_target", None) == self.control._execute:
                job_id, actor_id, prepared, kind = worker._args
                self.assertEqual(self.state.jobs(actor_id, job_id)["kind"], kind)
                self.assertEqual(self.lease()[0], job_id)
                raise RuntimeError("fake executor start denied")
            return real_start(worker)

        for kind in ("test", "build"):
            with mock.patch.object(threading.Thread, "start", start):
                self.assertEqual(self.submit(kind)[0], 503)
            self.assertIsNone(self.control._worker)
            self.assert_clean()
        self.assertTrue(all(job["status"] == "failed" for job in self.state.jobs("alice")))
        self.assertEqual(self.runner.runs, [])

    def test_registered_kind_must_match_before_thread_start(self):
        original = self.state.create_job
        for actual_kind in ("test", "invalid"):
            with self.subTest(actual_kind=actual_kind):
                def create(actor, workspace_id, source_hash, requested_kind):
                    return original(actor, workspace_id, source_hash, actual_kind)
                with mock.patch.object(self.state, "create_job", create):
                    self.assertEqual(self.submit("build")[0], 503)
                self.assert_clean()
                self.assertIsNone(self.control._worker)
        self.assertEqual(self.runner.runs, [])
        self.assertTrue(all(job["status"] == "failed" for job in self.state.jobs("alice")))

    def test_registered_kind_changed_after_queue_is_not_executed(self):
        with self.gated_executor():
            status, job = self.submit("build")
            self.assertEqual(status, 202)
            with self.state.transaction() as db:
                db.execute("UPDATE jobs SET kind='test' WHERE id=?", (job["id"],))
        self.assertEqual(self.runner.runs, [])
        self.assertEqual(self.state.jobs("alice", job["id"])["status"], "failed")
        self.assert_clean()

    def test_transfer_failure_cleans_registered_job(self):
        original = self.state.create_job

        def create(*args):
            job_id = original(*args)
            self.state.release(self.lease()[0])
            return job_id

        with mock.patch.object(self.state, "create_job", create):
            self.assertEqual(self.submit()[0], 503)
        self.assertEqual(self.state.jobs("alice")[0]["status"], "failed")
        self.assertEqual(self.runner.runs, [])
        self.assert_clean()

    def test_direct_default_execute_still_means_test(self):
        job_id = self.state.create_job("alice", "workspace", SOURCE_HASH, "test")
        self.state.acquire(job_id)
        self.control._tokens[job_id] = TOKEN
        self.control._execute(job_id, "alice", {"source_hash": SOURCE_HASH})
        self.assertEqual(self.state.jobs("alice", job_id)["status"], "succeeded")
        self.assertEqual(self.runner.admitted, [("test", job_id)])
        self.assert_clean()

    def test_direct_execute_illegal_or_mismatched_kind_never_runs(self):
        for registered, supplied in (("test", "build"), ("build", "test"), ("build", "shell")):
            job_id = self.state.create_job("alice", "workspace", SOURCE_HASH, registered)
            self.state.acquire(job_id)
            self.control._tokens[job_id] = TOKEN
            self.control._execute(job_id, "alice", {"source_hash": SOURCE_HASH}, supplied)
            self.assertEqual(self.state.jobs("alice", job_id)["status"], "failed")
            self.assert_clean()
        self.assertEqual(self.runner.runs, [])

    def test_uncertain_execution_preserves_lease_container_and_restart_does_not_retry(self):
        exc = ControlError("fake uncertain execution", 503)
        exc.execution_uncertain = True
        self.runner.run_error = exc
        status, job = self.submit()
        self.assertEqual(status, 202)
        self.wait_worker()
        saved = self.state.jobs("alice", job["id"])
        self.assertEqual(saved["status"], "failed")
        self.assertEqual(saved["result"], {"reason": "host recovery required"})
        self.assertEqual(self.lease(), (job["id"], CONTAINER_ID))
        self.assertEqual(self.control._tokens, {})
        self.control.close()
        self.state = controller.ControllerState(self.path, now=lambda: time.time() + 500)
        self.control = controller.Controller(KEY, self.state, self.security, self.store, self.workspace, self.runner)
        self.assertTrue(self.state.busy()["recovery_required"])
        self.assertEqual(self.lease(), (job["id"], CONTAINER_ID))
        self.assertEqual(self.submit()[0], 409)
        self.assertEqual(len(self.runner.runs), 1)
        self.assertEqual(self.control._tokens, {})

    def test_unclassified_execution_error_keeps_lease_but_known_safe_rejection_releases(self):
        for kind, error, uncertain in (("build", RuntimeError("unknown stop state"), True),
                                       ("test", ControlError("safe fake rejection", 403), False)):
            self.runner.run_error = error
            status, job = self.submit(kind)
            self.assertEqual(status, 202)
            self.wait_worker()
            self.assertEqual(self.state.busy()["active"], uncertain)
            expected = "host recovery required" if uncertain else "execution rejected"
            self.assertEqual(self.state.jobs("alice", job["id"])["result"], {"reason": expected})
            self.assertEqual(self.control._tokens, {})
            if uncertain:
                self.state.release(job["id"])  # Test fixture cleanup, not automatic recovery.

    def test_restart_marks_queued_and_running_interrupted_without_stealing_lease(self):
        queued = self.state.create_job("alice", "workspace", SOURCE_HASH, "test")
        running = self.state.create_job("alice", "workspace", SOURCE_HASH, "build")
        self.state.running(running)
        self.state.acquire(running)
        self.state.container(running, CONTAINER_ID)
        self.control.close()
        self.state = controller.ControllerState(self.path, now=lambda: time.time() + 500)
        self.control = controller.Controller(KEY, self.state, self.security, self.store, self.workspace, self.runner)
        for job_id in (queued, running):
            self.assertEqual(self.state.jobs("alice", job_id)["status"], "interrupted")
        self.assertEqual(self.lease(), (running, CONTAINER_ID))
        self.assertTrue(self.state.busy()["recovery_required"])
        self.assertEqual(self.submit("build")[0], 409)
        self.assertEqual(self.action()[0], 409)
        self.assertEqual(self.runner.runs, [])

    def test_invalid_runner_success_is_failed_safely_without_artifact_metadata(self):
        for invalid in ({"status": "succeeded"}, artifact_result(OTHER_HASH),
                        dict(artifact_result(), image_built=0), dict(artifact_result(), artifact_bytes=True)):
            with self.subTest(invalid=invalid):
                self.runner.result_override = invalid
                status, job = self.submit()
                self.assertEqual(status, 202)
                self.wait_worker()
                saved = self.state.jobs("alice", job["id"])
                self.assertEqual(saved["status"], "failed")
                self.assertEqual(saved["result"], {"reason": "execution rejected"})
                self.assert_clean()

    def test_inconsistent_build_execution_success_fails_generically_and_releases(self):
        for case, metadata in invalid_build_execution_results():
            with self.subTest(case=case):
                self.runner.result_override = metadata
                with mock.patch.object(self.state, "finish", wraps=self.state.finish) as finish, \
                        mock.patch.object(self.state, "release", wraps=self.state.release) as release:
                    with self.gated_executor():
                        status, job = self.submit()
                        self.assertEqual(status, 202)
                        self.assertEqual(job["status"], "queued")
                        self.assertEqual(self.lease(), (job["id"], None))
                        self.assertEqual(self.control._tokens, {job["id"]: TOKEN})
                    self.assertEqual([call[0][1] for call in finish.call_args_list],
                                     ["succeeded", "failed"])
                    finish.assert_any_call(job["id"], "failed", {"reason": "execution rejected"})
                    release.assert_called_once_with(job["id"])
                saved = self.state.jobs("alice", job["id"])
                self.assertEqual(saved["status"], "failed")
                self.assertEqual(saved["result"], {"reason": "execution rejected"})
                self.assertEqual(self.runner.runs[-1][:2], ("build", job["id"]))
                self.assertEqual(self.runner.admitted[-1], ("build", job["id"]))
                with self.state.transaction() as db:
                    self.assertEqual(db.execute("SELECT container_id FROM jobs WHERE id=?",
                                                (job["id"],)).fetchone()[0], CONTAINER_ID)
                self.assertIsNone(self.lease())
                self.assert_clean()

    def test_truncated_build_output_with_absent_or_none_reason_still_succeeds(self):
        for include_reason in (False, True):
            with self.subTest(include_reason=include_reason):
                metadata = dict(artifact_result(), output_bytes=99999, output_truncated=True)
                if include_reason:
                    metadata["reason"] = None
                self.runner.result_override = metadata
                status, job = self.submit()
                self.assertEqual(status, 202)
                self.wait_worker()
                saved = self.state.jobs("alice", job["id"])
                self.assertEqual(saved["status"], "succeeded")
                self.assertEqual(saved["result"], {k: v for k, v in metadata.items() if k != "status"})
                self.assert_clean()

    def test_failed_and_timed_out_runner_completion_release_lease(self):
        for status in ("failed", "timed_out", "interrupted"):
            self.runner.result_override = dict(execution_result(), status=status)
            code, job = self.submit()
            self.assertEqual(code, 202)
            self.wait_worker()
            self.assertEqual(self.state.jobs("alice", job["id"])["status"], status)
            self.assert_clean()

    def test_hmac_tamper_replay_and_invalid_route_fields_never_build(self):
        body = canonical(self.job_payload())
        headers = protocol.request_headers(KEY, "POST", "/v1/jobs", body)
        self.assertEqual(self.call("POST", "/v1/jobs", {}, body + b" ", headers)[0], 401)
        self.assertEqual(self.security.permissions, [])
        invalid = self.job_payload()
        invalid["artifact_path"] = "/untrusted/upload.zip"
        body = canonical(invalid)
        headers = protocol.request_headers(KEY, "POST", "/v1/jobs", body)
        self.assertEqual(self.call("POST", "/v1/jobs", {}, body, headers)[0], 400)
        self.assertEqual(self.call("POST", "/v1/jobs", {}, body, headers)[0], 409)
        for field, value in (("kind", "image"), ("kind", True), ("expected_hash", SOURCE_HASH.upper()),
                             ("workspace_id", "../escape")):
            payload = self.job_payload()
            payload[field] = value
            self.assertEqual(self.call("POST", "/v1/jobs", payload)[0], 400)
        self.assert_clean(no_jobs=True)
        self.assertEqual(self.runner.prepares, [])

    def test_shutdown_rejects_before_freeze(self):
        self.control._closing.set()
        for kind in ("test", "build"):
            self.assertEqual(self.submit(kind)[0], 503)
        self.assert_clean(no_jobs=True)
        self.assertEqual(self.workspace.frozen, [])


class MetadataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="build-metadata-r6-")
        self.path = os.path.join(os.path.realpath(self.temp.name), "controller.db")
        self.state = controller.ControllerState(self.path)

    def tearDown(self):
        self.state.close()
        self.temp.cleanup()

    def job(self, kind="build", running=True):
        job_id = self.state.create_job("alice", "workspace", SOURCE_HASH, kind)
        if running:
            self.state.running(job_id)
        return job_id

    def reject(self, metadata, kind="build", status="succeeded", running=True):
        job_id = self.job(kind, running)
        before = self.state.jobs("alice", job_id)
        with self.assertRaises(ControlError) as caught:
            self.state.finish(job_id, status, metadata)
        self.assertEqual(caught.exception.status, 503)
        self.assertEqual(self.state.jobs("alice", job_id), before)
        self.state.finish(job_id, "failed", {"reason": "execution rejected"})
        self.assertEqual(self.state.jobs("alice", job_id)["status"], "failed")

    def test_inconsistent_build_execution_rejected_atomically_queued_and_running(self):
        for case, metadata in invalid_build_execution_results():
            for running in (False, True):
                with self.subTest(case=case, running=running):
                    job_id = self.job(running=running)
                    self.state.acquire(job_id)
                    self.state.container(job_id, CONTAINER_ID)
                    before_metadata = copy.deepcopy(metadata)
                    with self.state.transaction() as db:
                        before_job = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
                        before_lease = db.execute("SELECT * FROM lease").fetchall()
                    with mock.patch.object(self.state, "now", return_value=int(time.time()) + 100):
                        with self.assertRaises(ControlError) as caught:
                            self.state.finish(job_id, "succeeded", metadata)
                    self.assertEqual(caught.exception.status, 503)
                    with self.state.transaction() as db:
                        self.assertEqual(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone(),
                                         before_job)
                        self.assertEqual(db.execute("SELECT * FROM lease").fetchall(), before_lease)
                    self.assertEqual(metadata, before_metadata)
                    self.state.finish(job_id, "failed", {"reason": "execution rejected"})
                    self.assertEqual(self.state.jobs("alice", job_id)["result"],
                                     {"reason": "execution rejected"})
                    self.state.release(job_id)
                    self.assertFalse(self.state.busy()["active"])

    def test_build_success_accepts_output_boundaries_truncation_and_none_reason(self):
        for output_bytes in (0, 2 ** 63 - 1):
            for truncated in (False, True):
                for include_reason in (False, True):
                    with self.subTest(output_bytes=output_bytes, truncated=truncated,
                                      include_reason=include_reason):
                        metadata = dict(artifact_result(), output_bytes=output_bytes,
                                        output_truncated=truncated, stdout="PRIVATE_STDOUT")
                        if include_reason:
                            metadata["reason"] = None
                        job_id = self.job()
                        self.state.finish(job_id, "succeeded", metadata)
                        saved = self.state.jobs("alice", job_id)
                        self.assertEqual(saved["status"], "succeeded")
                        self.assertEqual(saved["result"],
                                         {k: v for k, v in metadata.items() if k not in ("status", "stdout")})

    def test_partial_test_success_and_unsuccessful_execution_metadata_remain_compatible(self):
        partial = ({}, {"exit_code": None},
                   {"exit_code": 137, "oom_killed": True, "metadata_limit": 0, "output_retained": True},
                   {"reason": "execution rejected"}, {"reason": "host recovery required"})
        for kind in ("test", "build"):
            statuses = ("succeeded", "failed", "timed_out", "interrupted") if kind == "test" else (
                "failed", "timed_out", "interrupted")
            for status in statuses:
                for metadata in partial:
                    with self.subTest(kind=kind, status=status, metadata=metadata):
                        job_id = self.job(kind)
                        self.state.finish(job_id, status, metadata)
                        saved = self.state.jobs("alice", job_id)
                        self.assertEqual(saved["status"], status)
                        self.assertEqual(saved["result"], metadata)

    def test_all_common_types_and_integer_overflows_rejected_atomically(self):
        invalid = {"exit_code": (True, False, "0", 0.0, [], {}, -(2 ** 31) - 1, 2 ** 31),
                   "oom_killed": (0, 1, None, "false", []),
                   "output_truncated": (0, 1, None, "false", []),
                   "output_retained": (0, 1, None, "false", []),
                   "output_bytes": (True, False, None, "0", 0.0, -1, 2 ** 63),
                   "metadata_limit": (True, False, None, "16384", 16384.0, -1, 16385),
                   "reason": (True, False, 0, [], {}, "arbitrary private reason", "x" * 16385)}
        for key, values in invalid.items():
            for value in values:
                with self.subTest(key=key, value=value):
                    for kind in ("test", "build"):
                        metadata = artifact_result() if kind == "build" else execution_result()
                        metadata[key] = value
                        self.reject(metadata, kind)
        for value in (None, [], "not-a-dict", 0):
            self.reject(value)

    def test_complete_build_metadata_required_for_each_success(self):
        for key in ARTIFACT_FIELDS:
            with self.subTest(missing=key):
                metadata = artifact_result()
                del metadata[key]
                self.reject(metadata, running=False)
        self.reject({})
        self.reject(execution_result())

    def test_artifact_types_bounds_true_and_false_lookalikes_rejected(self):
        invalid = {"artifact_sha256": (None, True, 1, "E" * 64, "e" * 63, "g" * 64, "e" * 64 + "\n"),
                   "source_hash": (None, True, 1, "C" * 64, "c" * 63, OTHER_HASH),
                   "artifact_bytes": (None, True, False, "1", 1.0, 0, -1, 12 * 1024 * 1024 + 1),
                   "artifact_files": (None, True, False, "1", 1.0, 0, -1, 513),
                   "kind": (None, True, 1, "build", "image", "release-approved"),
                   "image_built": (None, True, 0, 1, 0.0, "false"),
                   "release_approved": (None, True, 0, 1, 0.0, "false"),
                   "output_retained": (True,)}
        for key, values in invalid.items():
            for value in values:
                with self.subTest(key=key, value=value):
                    metadata = artifact_result()
                    metadata[key] = value
                    self.reject(metadata)

    def test_test_jobs_and_unsuccessful_build_cannot_store_any_artifact_assertion(self):
        cases = [("test", status) for status in ("succeeded", "failed", "timed_out", "interrupted")]
        cases += [("build", status) for status in ("failed", "timed_out", "interrupted")]
        for kind, status in cases:
            for key in ARTIFACT_FIELDS:
                if key == "output_retained":  # Also a legitimate common output field.
                    continue
                for value in (artifact_result()[key], None):
                    with self.subTest(kind=kind, status=status, key=key, value=value):
                        metadata = execution_result()
                        metadata[key] = value
                        self.reject(metadata, kind, status)
            self.reject(artifact_result(), kind, status)

    def test_common_metadata_boundaries_and_three_safe_reasons_remain_compatible(self):
        for code in (None, -(2 ** 31), 2 ** 31 - 1):
            for reason in (None, "execution rejected", "host recovery required"):
                job_id = self.job("test")
                metadata = dict(execution_result(), exit_code=code, output_bytes=2 ** 63 - 1,
                                metadata_limit=0, output_retained=True, reason=reason)
                self.state.finish(job_id, "succeeded", metadata)
                self.assertEqual(self.state.jobs("alice", job_id)["result"],
                                 {k: v for k, v in metadata.items() if k != "status"})
        job_id = self.job("test", False)
        self.state.finish(job_id, "failed", {})
        self.assertEqual(self.state.jobs("alice", job_id)["result"], {})

    def test_artifact_size_and_file_count_upper_bounds_are_valid(self):
        for size, files in ((1, 1), (12 * 1024 * 1024, 512)):
            job_id = self.job()
            self.state.finish(job_id, "succeeded", dict(artifact_result(), artifact_bytes=size, artifact_files=files))
            saved = self.state.jobs("alice", job_id)
            self.assertEqual(saved["status"], "succeeded")
            self.assertEqual(saved["result"]["artifact_bytes"], size)
            self.assertLessEqual(len(canonical(saved["result"])), 16384)

    def test_unknown_fields_filtered_without_serializing_private_paths_or_objects(self):
        job_id = self.job()
        metadata = artifact_result()
        metadata.update(stdout="PRIVATE_STDOUT", token=TOKEN, artifact_path="/private/source.zip",
                        path="/private/snapshot", arbitrary=object(), image="published-image")
        self.state.finish(job_id, "succeeded", metadata)
        self.assertEqual(self.state.jobs("alice", job_id)["result"],
                         {k: v for k, v in artifact_result().items() if k != "status"})
        with open(self.path, "rb") as stream:
            data = stream.read()
        for value in (TOKEN, "PRIVATE_STDOUT", "/private/source.zip", "/private/snapshot", "published-image"):
            self.assertNotIn(value.encode("ascii"), data)

    def test_result_cap_rejects_before_terminal_update(self):
        with mock.patch.object(controller, "canonical", return_value=b"x" * 16385):
            job_id = self.job()
            with self.assertRaises(ControlError):
                self.state.finish(job_id, "succeeded", artifact_result())
        self.assertEqual(self.state.jobs("alice", job_id)["status"], "running")
        self.assertIsNone(self.state.jobs("alice", job_id)["result"])
        self.state.finish(job_id, "failed", {"reason": "execution rejected"})

    def test_source_binding_and_terminal_write_share_begin_immediate(self):
        job_id = self.job()
        trace = []
        original_transaction = self.state.transaction
        original_canonical = controller.canonical
        blocked_writer = []

        @contextlib.contextmanager
        def transaction():
            with original_transaction() as db:
                db.set_trace_callback(trace.append)
                yield db

        def encode(metadata):
            writer = sqlite3.connect(self.path, timeout=0, isolation_level=None)
            try:
                with self.assertRaises(sqlite3.OperationalError):
                    writer.execute("UPDATE jobs SET source_hash=? WHERE id=?", (OTHER_HASH, job_id))
                blocked_writer.append(True)
            finally:
                writer.close()
            return original_canonical(metadata)

        with mock.patch.object(self.state, "transaction", transaction), mock.patch.object(controller, "canonical", encode):
            self.state.finish(job_id, "succeeded", artifact_result())
        selects = [i for i, sql in enumerate(trace) if sql.startswith("SELECT source_hash,kind")]
        updates = [i for i, sql in enumerate(trace) if sql.startswith("UPDATE jobs SET status=")]
        self.assertEqual(len(selects), 1)
        self.assertEqual(len(updates), 1)
        self.assertLess(selects[0], updates[0])
        self.assertEqual(trace[-1], "COMMIT")
        self.assertEqual(blocked_writer, [True])
        saved = self.state.jobs("alice", job_id)
        self.assertEqual(saved["source_hash"], saved["result"]["source_hash"])

    def test_terminal_whitelist_and_existing_terminal_cannot_be_overwritten(self):
        for status in ("queued", "running", "released", "published", True, None):
            self.reject(artifact_result(), status=status)
        job_id = self.job()
        self.state.finish(job_id, "succeeded", artifact_result())
        before = self.state.jobs("alice", job_id)
        self.state.finish(job_id, "failed", {"reason": "execution rejected"})
        self.assertEqual(self.state.jobs("alice", job_id), before)

    def test_controller_and_new_suite_python36_syntax(self):
        for path in (controller.__file__, os.path.abspath(__file__)):
            with open(path, "r") as stream:
                ast.parse(stream.read(), filename=path, feature_version=(3, 6))


if __name__ == "__main__":
    unittest.main()
