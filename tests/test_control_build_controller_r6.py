"""Local controller/Runner/ZIP/SQLite integration, NOT Docker or HTTP acceptance.

Run with an existing workspace TMPDIR and /usr/local/bin/python3 -B. Requests
call the real signed handle() in process; executor/heartbeat threads are real.
Only Docker inspection/execution, chown and rlimit are explicit fakes. The fake
Docker producer runs the real fixed entry on synthetic syntax-only source; its
report job id comes from Runner's actual build-request.json, never a constant.
No sockets, subprocesses, SSH, HTTP, installs, network or Mac configuration.
"""
import ast
import contextlib
import copy
import gc
import os
from pathlib import Path
import resource
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import warnings
import zipfile
from unittest import mock

# Module aliases, not imported TestCase classes: unittest must not rediscover
# the helper suites. Also support an absolute script invocation from TMPDIR.
TEST_DIRECTORY = Path(__file__).resolve().parent
PROJECT_DIRECTORY = TEST_DIRECTORY.parent
sys.path.insert(0, str(PROJECT_DIRECTORY))
sys.path.insert(0, str(TEST_DIRECTORY))
import test_control_build_execution_r6 as execution_fixture
import test_control_build_jobs_r6 as jobs_fixture

from control_plane import build_artifacts as artifacts
from control_plane import controller, protocol, runner, sandbox_build_entry as entry
from control_plane.registry import ControlError, canonical

EXECUTION_FIELDS = {"exit_code", "oom_killed", "output_bytes", "output_truncated",
                    "output_retained", "metadata_limit"}
ARTIFACT_FIELDS = {"artifact_sha256", "source_hash", "artifact_bytes", "artifact_files",
                   "kind", "image_built", "release_approved"}


class FixtureWorkspace:
    """Explicit workspace adapter over real, immutable synthetic fixture files."""
    def __init__(self, snapshot):
        self.value = dict(snapshot, owner="alice")
        self.frozen = []

    def describe(self, workspace_id, actor_id):
        return copy.deepcopy(self.value)

    def snapshot(self, workspace_id, actor_id, expected_hash):
        self.frozen.append((workspace_id, actor_id, expected_hash))
        return copy.deepcopy(self.value)


class BuildControllerTests(unittest.TestCase):
    def setUp(self):
        # Registered first, hence audited AFTER all other cleanup, including the
        # manually owned helper fixture. ResourceWarning in __del__ is recorded.
        self.warning_scope = warnings.catch_warnings()
        self.warning_scope.__enter__()
        warnings.simplefilter("error", ResourceWarning)
        self.unraisable = []
        self.thread_errors = []
        self.old_unraisable = getattr(sys, "unraisablehook", None)
        self.old_thread_hook = getattr(threading, "excepthook", None)
        if self.old_unraisable is not None:
            sys.unraisablehook = self.unraisable.append
        if self.old_thread_hook is not None:
            threading.excepthook = self.thread_errors.append
        self.addCleanup(self.audit_resources)

        tmpdir = os.environ.get("TMPDIR", "")
        if (not os.path.isabs(tmpdir) or not os.path.isdir(tmpdir)
                or os.path.realpath(tmpdir) != tmpdir.rstrip(os.sep)):
            raise AssertionError("an existing absolute nonsymlink workspace TMPDIR is required")
        workspace = str(PROJECT_DIRECTORY.parent.parent)
        if (os.path.commonpath([tmpdir, workspace]) != workspace
                or os.path.commonpath([tmpdir, str(PROJECT_DIRECTORY)]) == str(PROJECT_DIRECTORY)):
            raise AssertionError("TMPDIR must be in the workspace, outside the repository")
        self.patch(tempfile, "tempdir", tmpdir)

        # Fail closed even if the real Runner accidentally falls back to its
        # default command, or somebody introduces an HTTP/SSH/install operation.
        for obj, name in ((runner, "bounded_command"), (subprocess, "Popen"),
                          (os, "system"), (socket, "socket"),
                          (socket, "create_connection"), (socket, "getaddrinfo")):
            forbidden = self.patch(obj, name, side_effect=AssertionError("forbidden real " + name))
            self.addCleanup(forbidden.assert_not_called)
        for name in ("posix_spawn", "posix_spawnp"):
            if hasattr(os, name):
                forbidden = self.patch(os, name, side_effect=AssertionError("forbidden real " + name))
                self.addCleanup(forbidden.assert_not_called)
        self.fake_limit = self.patch(resource, "setrlimit")
        self.fake_getlimit = self.patch(resource, "getrlimit", return_value=(
            resource.RLIM_INFINITY, resource.RLIM_INFINITY))

        self.fixture = execution_fixture.BuildExecutionTests(methodName="runTest")
        # Register BEFORE setUp: partially failed setup still runs doCleanups.
        self.addCleanup(self.cleanup_fixture)
        self.fixture.setUp()
        self.run = self.fixture.run
        self.docker = self.fixture.docker
        self.assertIsInstance(self.run, runner.Runner)
        self.assertIsInstance(self.docker, execution_fixture.FakeDocker)
        self.assertIs(self.run.command, self.docker)
        self.produced = []
        self.reviews = []
        self.fixture.produce_artifact = self.produce_from_fixed_request
        self.workspace = FixtureWorkspace(self.fixture.snapshot())
        self.path = str(self.fixture.private / "controller.db")
        self.state = controller.ControllerState(self.path)
        self.addCleanup(self.state.close)
        self.store = jobs_fixture.SQLiteSecurityStore(str(self.fixture.private / "security-fixture.db"))
        self.security = jobs_fixture.SQLiteSecurity(self.store)
        self.control = controller.Controller(jobs_fixture.KEY, self.state, self.security,
                                             self.store, self.workspace, self.run)
        self.gates = []
        self.addCleanup(self.close_control)
        self.prepare = self.patch(self.run, "prepare_build", wraps=self.run.prepare_build)
        self.execute = self.patch(self.run, "run_build", wraps=self.run.run_build)
        self.finish = self.patch(self.state, "finish", wraps=self.state.finish)
        self.release = self.patch(self.state, "release", wraps=self.state.release)
        self.verify = self.patch(runner, "verify_artifacts", side_effect=self.verify_stopped_artifacts)

    def patch(self, obj, name, *args, **kwargs):
        patcher = mock.patch.object(obj, name, *args, **kwargs)
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def audit_resources(self):
        try:
            gc.collect()
        finally:
            if self.old_unraisable is not None:
                sys.unraisablehook = self.old_unraisable
            if self.old_thread_hook is not None:
                threading.excepthook = self.old_thread_hook
            self.warning_scope.__exit__(None, None, None)
        self.assertEqual(self.unraisable, [], "unraisable exception during execution/cleanup")
        self.assertEqual(self.thread_errors, [], "executor/heartbeat thread exception")

    def cleanup_fixture(self):
        self.assertTrue(self.fixture.doCleanups(), "helper fixture cleanup failed")
        if hasattr(self.fixture, "temp"):
            self.assertFalse(Path(self.fixture.temp.name).exists())

    def close_control(self):
        for gate in self.gates:
            gate.set()
        self.control.close()

    def produce_from_fixed_request(self, prepared, mutate=None):
        # Real main() reads the real fixed request and creates the actual ZIP and
        # report. Only container paths and resource APIs are faked, not evidence.
        with self.fixture.main_remap(prepared), mock.patch.object(sys, "dont_write_bytecode", True):
            self.assertEqual(entry.main(), 0)
        output = Path(prepared["output"])
        report_path = output / artifacts.RESULT_NAME
        report = protocol.strict_json(report_path.read_bytes(), artifacts.RESULT_LIMIT)
        request = protocol.strict_json((Path(prepared["trusted"]) / "build-request.json").read_bytes())
        self.assertEqual(report["job_id"], request["job_id"])
        self.assertEqual(report["source_hash"], request["source_hash"])
        self.produced.append(copy.deepcopy(report))
        if mutate:
            mutate(report)
            report_path.write_bytes(canonical(report))
        return report

    def verify_stopped_artifacts(self, *args):
        self.assertIs(self.docker.value["State"]["Running"], False)
        self.assertIn(self.docker.value["State"]["Status"], ("exited", "dead"))
        self.docker.events.append("review")
        try:
            report = artifacts.verify_artifacts(*args)
        except artifacts.ArtifactError:
            self.reviews.append("rejected")
            raise
        self.reviews.append("verified")
        return report

    def payload(self, token=jobs_fixture.TOKEN):
        return {"token": token, "workspace_id": "workspace",
                "expected_hash": self.workspace.value["source_hash"], "kind": "build"}

    def call(self, method, path, payload, body=None, headers=None):
        body = canonical(payload) if body is None else body
        headers = protocol.request_headers(jobs_fixture.KEY, method, path, body) if headers is None else headers
        status, response, signed = self.control.handle(method, path, body, headers)
        protocol.verify_response(jobs_fixture.KEY, headers["X-Control-Request-ID"],
                                 headers["X-Control-Nonce"], status, response, signed)
        return status, protocol.strict_json(response, protocol.MAX_RESPONSE)

    @contextlib.contextmanager
    def queued_executor(self):
        """Gate a REAL executor thread; this is not a fake thread or HTTP server."""
        resume, entered = threading.Event(), threading.Event()
        self.gates.append(resume)
        real_start = threading.Thread.start
        spawned = []

        def start(worker):
            if getattr(worker, "_target", None) == self.control._execute:
                target = worker._target
                self.fixture.prepared = worker._args[2]
                spawned.append(worker)

                def gated(*args, **kwargs):
                    entered.set()
                    if not resume.wait(10):
                        raise AssertionError("local executor gate timed out")
                    target(*args, **kwargs)

                worker._target = gated
            return real_start(worker)

        with mock.patch.object(threading.Thread, "start", start):
            try:
                yield entered
            finally:
                resume.set()
                for worker in spawned:
                    worker.join(10)
                    self.assertFalse(worker.is_alive(), "executor did not stop before fixture cleanup")

    def complete(self):
        with self.queued_executor() as entered:
            status, queued = self.call("POST", "/v1/jobs", self.payload())
            self.assertEqual(status, 202)
            self.assertTrue(entered.wait(2))
            self.assertEqual(queued["status"], "queued")
            self.assertEqual((queued["kind"], queued["source_hash"]),
                             ("build", self.workspace.value["source_hash"]))
            self.assertEqual(self.lease(), (queued["id"], None))
            self.assertEqual(self.control._tokens, {queued["id"]: jobs_fixture.TOKEN})
            self.assertNotIn("create", self.docker.events)
            self.assertEqual(self.produced, [])
            self.assertEqual(self.execute.call_count, 0)
        status, job = self.call("GET", "/v1/jobs/" + queued["id"], {"token": jobs_fixture.TOKEN})
        self.assertEqual(status, 200)
        self.assertEqual(job["id"], queued["id"])
        self.prepare.assert_called_once()
        self.execute.assert_called_once()
        self.assertEqual(self.execute.call_args[0][:2], (self.fixture.prepared, job["id"]))
        self.assertEqual(self.control._tokens, {})
        self.assertEqual(self.workspace.frozen, [("workspace", "alice", job["source_hash"])])
        self.fixture.fake_chown.assert_called_once_with(self.fixture.prepared["output"], 10001, 10001)
        request_path = Path(self.fixture.prepared["trusted"]) / "build-request.json"
        self.assertEqual(protocol.strict_json(request_path.read_bytes()),
                         {"job_id": job["id"], "source_hash": job["source_hash"]})
        self.assertNotEqual(job["id"], execution_fixture.JOB)
        for report in self.produced:
            self.assertEqual((report["job_id"], report["source_hash"]), (job["id"], job["source_hash"]))
        self.assert_private(job)
        return job

    def lease(self):
        with self.state.transaction() as db:
            return db.execute("SELECT owner,container_id FROM lease WHERE singleton=1").fetchone()

    def assert_private(self, job):
        forbidden = [jobs_fixture.TOKEN, jobs_fixture.OTHER_TOKEN, "UNTRUSTED_PRIVATE_STDOUT",
                     str(self.fixture.host), str(self.fixture.snapshot_root)]
        if self.fixture.prepared:
            forbidden.extend(self.fixture.prepared[key] for key in ("source", "trusted", "output"))
        for pattern in ("controller.db*", "security-fixture.db*"):
            for path in self.fixture.private.glob(pattern):
                if path.is_file():
                    data = path.read_bytes()
                    for value in forbidden:
                        self.assertNotIn(value.encode("utf-8"), data, str(path))
        encoded = canonical(job)
        for value in forbidden:
            self.assertNotIn(value.encode("utf-8"), encoded)
        self.assertFalse({"token", "path", "stdout", "artifact_path", "artifact"}.intersection(job["result"]))

    def assert_terminal(self, job, status, code, oom, review=None):
        self.assertEqual(job["status"], status)
        metadata = job["result"]
        self.assertEqual(set(metadata), EXECUTION_FIELDS | (ARTIFACT_FIELDS if status == "succeeded" else set()))
        self.assertEqual(metadata["exit_code"], code)
        self.assertIs(metadata["oom_killed"], oom)
        self.assertEqual(metadata["output_bytes"], min(self.docker.start_result.bytes_seen, 2147483647))
        self.assertIs(metadata["output_truncated"], self.docker.start_result.truncated is True
                      or self.docker.start_result.bytes_seen > runner.OUTPUT_LIMIT)
        self.assertIs(metadata["output_retained"], False)
        self.assertEqual(metadata["metadata_limit"], 16384)
        # Exactly one real terminal write: failures/OOM/timeouts must NOT fall
        # through finish() rejection to generic {reason: execution rejected}.
        self.finish.assert_called_once()
        args = self.finish.call_args[0]
        self.assertEqual(args[:2], (job["id"], status))
        self.assertEqual({k: v for k, v in args[2].items() if k != "status"}, metadata)
        self.assertNotIn("reason", metadata)
        self.release.assert_called_once_with(job["id"])
        self.assertIsNone(self.lease())
        self.assertFalse(self.state.busy()["active"])
        self.assertEqual(self.reviews, [] if review is None else [review])
        self.assertEqual(self.verify.call_count, 0 if review is None else 1)
        # Independent real SQLite read (closing(), not Connection.__exit__,
        # which does not close a sqlite connection).
        with contextlib.closing(sqlite3.connect(self.path)) as db:
            row = db.execute("SELECT status,result,container_id FROM jobs WHERE id=?", (job["id"],)).fetchone()
        self.assertEqual(row, (status, canonical(metadata), execution_fixture.BUILD_CID))

    def revoke_develop(self):
        self.store.change("DELETE FROM cp_grants WHERE user_id='alice' AND permission='develop'")

    def assert_rejected_safely(self, job, container_id):
        self.assertEqual((job["status"], job["result"]), ("failed", {"reason": "execution rejected"}))
        self.finish.assert_called_once_with(job["id"], "failed", {"reason": "execution rejected"})
        self.release.assert_called_once_with(job["id"])
        self.assertIsNone(self.lease())
        self.assertEqual(self.control._tokens, {})
        self.assertEqual(self.produced, [])
        self.verify.assert_not_called()
        with self.state.transaction() as db:
            self.assertEqual(db.execute("SELECT container_id FROM jobs WHERE id=?", (job["id"],)).fetchone(),
                             (container_id,))

    def reopen(self, expired=False):
        self.control.close()
        self.state = controller.ControllerState(self.path, now=(lambda: time.time() + 500) if expired else None)
        self.addCleanup(self.state.close)
        self.control = controller.Controller(jobs_fixture.KEY, self.state, self.security,
                                             self.store, self.workspace, self.run)

    def assert_uncertain_persists(self, job, container_id):
        self.assertEqual((job["status"], job["result"]), ("failed", {"reason": "host recovery required"}))
        self.finish.assert_called_once_with(job["id"], "failed", {"reason": "host recovery required"})
        self.release.assert_not_called()
        self.assertEqual(self.lease(), (job["id"], container_id))
        self.assertEqual(self.control._tokens, {})
        self.verify.assert_not_called()
        with self.state.transaction() as db:
            self.assertEqual(db.execute("SELECT container_id FROM jobs WHERE id=?", (job["id"],)).fetchone(),
                             (container_id,))
        calls = copy.deepcopy(self.docker.calls)
        frozen = copy.deepcopy(self.workspace.frozen)
        self.reopen(expired=True)
        self.assertEqual(self.state.busy(), {"active": True, "recovery_required": True})
        self.assertEqual(self.lease(), (job["id"], container_id))
        self.assertIsNone(self.control._worker)
        self.assertEqual(self.control._tokens, {})
        self.assertEqual(self.call("GET", "/v1/jobs/" + job["id"], {"token": jobs_fixture.TOKEN}), (200, job))
        self.assertEqual(self.call("POST", "/v1/jobs", self.payload())[0], 409)
        self.assertEqual(self.call("POST", "/v1/actions", {"token": jobs_fixture.TOKEN,
                         "component": "gateway", "action": "start",
                         "expected_config_hash": jobs_fixture.POLICY_HASH})[0], 409)
        self.assertEqual(self.lease(), (job["id"], container_id))
        self.assertEqual(self.docker.calls, calls)
        self.assertEqual(self.workspace.frozen, frozen)
        self.assertEqual(self.prepare.call_count, 1)
        self.assertEqual(self.execute.call_count, 1)
        self.assert_private(job)

    def test_succeeded_real_fixed_entry_zip_review_and_sqlite_terminal(self):
        job = self.complete()
        self.assert_terminal(job, "succeeded", 0, False, "verified")
        report = self.produced[0]
        self.assertEqual({k: job["result"][k] for k in ARTIFACT_FIELDS},
                         {k: report[k] for k in ARTIFACT_FIELDS})
        self.assertIs(job["result"]["image_built"], False)
        self.assertIs(job["result"]["release_approved"], False)
        p = self.fixture.prepared
        blob = (Path(p["output"]) / artifacts.ZIP_NAME).read_bytes()
        self.assertEqual(job["result"]["artifact_sha256"], execution_fixture.sha(blob))
        self.assertEqual(job["result"]["artifact_bytes"], len(blob))
        self.assertEqual(job["result"]["artifact_files"], len(p["source_entries"]))
        with zipfile.ZipFile(str(Path(p["output"]) / artifacts.ZIP_NAME)) as archive:
            self.assertEqual(set(archive.namelist()), set(p["source_entries"]) | {"BUILD-MANIFEST.json"})
            for name, data in p["source_entries"].items():
                self.assertEqual(archive.read(name), data)
        self.fake_limit.assert_called_once_with(resource.RLIMIT_FSIZE, (entry.ARTIFACT_LIMIT, entry.ARTIFACT_LIMIT))
        self.fake_getlimit.assert_called_once_with(resource.RLIMIT_FSIZE)
        self.assertGreater(self.docker.events.index("review"), self.docker.events.index("start"))
        self.assertNotIn("stop", self.docker.events)
        create = next(call[0] for call in self.docker.calls if call[0][0] == "create")
        self.assertEqual(create[create.index("--name") + 1], "daobrain-r6-build-" + job["id"])
        self.assertIn("com.daobrain.control.job=" + job["id"], create)
        self.assertEqual(create[create.index("--network") + 1], "none")
        self.assertEqual(create[create.index("--pull") + 1], "never")
        self.assertFalse(list(self.fixture.root.rglob("*.pyc")))

    def test_nonzero_exit_keeps_execution_fields_not_generic_rejection(self):
        self.docker.exit_code = 23
        job = self.complete()
        self.assert_terminal(job, "failed", 23, False)
        self.assertEqual(len(self.produced), 1)  # A real ZIP cannot override exit failure.

    def test_oom_keeps_execution_fields_without_artifact_evidence(self):
        self.docker.exit_code, self.docker.oom = 137, True
        self.assert_terminal(self.complete(), "failed", 137, True)
        self.assertEqual(len(self.produced), 1)

    def test_zero_exit_oom_is_failed_even_with_real_zip(self):
        self.docker.oom = True
        self.assert_terminal(self.complete(), "failed", 0, True)
        self.assertEqual(len(self.produced), 1)

    def test_attach_failure_with_inspected_zero_exit_does_not_attest_zip(self):
        self.docker.start_result = execution_fixture.result(b"UNTRUSTED_PRIVATE_STDOUT", code=1)
        self.assert_terminal(self.complete(), "failed", 0, False)
        self.assertEqual(len(self.produced), 1)

    def test_timeout_verified_stop_persists_timed_out_and_releases_lease(self):
        self.docker.start_result = execution_fixture.result(b"UNTRUSTED_PRIVATE_STDOUT", code=-9,
                                                          timeout=True, truncated=True, seen=99999)
        self.assert_terminal(self.complete(), "timed_out", 137, False)
        self.assertEqual(self.produced, [])
        self.assertEqual(self.docker.events.count("stop"), 1)
        self.assertIn((["stop", "--time", "5", execution_fixture.BUILD_CID], 10, runner.OUTPUT_LIMIT),
                      self.docker.calls)
        self.assertIs(self.docker.value["State"]["Running"], False)
        self.fake_limit.assert_not_called()

    def test_zero_exit_report_bound_to_another_job_is_failed(self):
        self.docker.mutate_report = lambda report: report.update(job_id="4" * 32)
        self.assert_terminal(self.complete(), "failed", 0, False, "rejected")

    def test_zero_exit_report_source_hash_tamper_is_failed(self):
        self.docker.mutate_report = lambda report: report.update(source_hash="0" * 64)
        self.assert_terminal(self.complete(), "failed", 0, False, "rejected")

    def test_zero_exit_zip_source_tamper_with_honestly_repinned_digest_is_failed(self):
        def tamper():
            records = self.fixture.zip_records()
            self.fixture.rewrite_zip([(name, b"VALUE=999\n" if name == "gateway/main.py" else data)
                                      for name, data in records])
        self.docker.after_start = tamper
        self.assert_terminal(self.complete(), "failed", 0, False, "rejected")
        p = self.fixture.prepared
        report = protocol.strict_json((Path(p["output"]) / artifacts.RESULT_NAME).read_bytes())
        self.assertEqual(report["artifact_sha256"], execution_fixture.sha(
            (Path(p["output"]) / artifacts.ZIP_NAME).read_bytes()))

    def test_zero_exit_without_artifacts_is_failed_with_execution_metadata(self):
        self.docker.produce = False
        self.assert_terminal(self.complete(), "failed", 0, False, "rejected")
        self.assertEqual(self.produced, [])

    def test_revocation_during_real_recheck_blocks_final_create_admission(self):
        original = self.run._build_recheck

        def recheck(prepared):
            original(prepared)
            self.assertEqual(self.state.jobs("alice")[0]["status"], "running")
            self.revoke_develop()  # Independently committed real SQLite grant change.

        with mock.patch.object(self.run, "_build_recheck", side_effect=recheck) as checked:
            job = self.complete()
        checked.assert_called_once()
        self.assert_rejected_safely(job, None)
        for action in ("create", "start", "stop"):
            self.assertNotIn(action, self.docker.events)

    def test_revocation_after_create_blocks_start_and_releases_never_started_lease(self):
        self.docker.create_hook = self.revoke_develop
        job = self.complete()
        self.assert_rejected_safely(job, execution_fixture.BUILD_CID)
        self.assertEqual(self.docker.events.count("create"), 1)
        self.assertNotIn("start", self.docker.events)
        self.assertNotIn("stop", self.docker.events)
        self.assertEqual(self.docker.value["State"]["Status"], "created")
        self.assertIs(self.docker.value["State"]["Running"], False)

    def test_queued_revocation_never_enters_real_runner(self):
        with self.queued_executor() as entered:
            status, queued = self.call("POST", "/v1/jobs", self.payload())
            self.assertEqual(status, 202)
            self.assertTrue(entered.wait(2))
            self.revoke_develop()
        status, job = self.call("GET", "/v1/jobs/" + queued["id"], {"token": jobs_fixture.TOKEN})
        self.assertEqual(status, 200)
        self.assert_rejected_safely(job, None)
        self.prepare.assert_called_once()
        self.execute.assert_not_called()
        self.assertNotIn("create", self.docker.events)
        self.assert_private(job)

    def test_unknown_create_timeout_preserves_idless_lease_after_reopen(self):
        self.docker.create_result = execution_fixture.result(b"UNTRUSTED_PRIVATE_STDOUT", code=-9, timeout=True)
        self.assert_uncertain_persists(self.complete(), None)
        self.assertEqual(self.docker.events.count("create"), 1)
        self.assertNotIn("start", self.docker.events)
        self.assertNotIn("stop", self.docker.events)

    def test_unknown_create_exception_preserves_idless_lease_after_reopen(self):
        def unknown_create():
            raise RuntimeError(jobs_fixture.TOKEN + self.fixture.prepared["output"] + "UNTRUSTED_PRIVATE_STDOUT")
        self.docker.create_hook = unknown_create
        self.assert_uncertain_persists(self.complete(), None)
        self.assertNotIn("start", self.docker.events)
        self.assertNotIn("stop", self.docker.events)

    def test_truncated_create_id_preserves_lease_without_guessing_container(self):
        self.docker.create_result = execution_fixture.result(
            (execution_fixture.BUILD_CID + "\n").encode("ascii"), truncated=True)
        self.assert_uncertain_persists(self.complete(), None)
        self.assertNotIn("start", self.docker.events)
        self.assertNotIn("stop", self.docker.events)

    def test_stop_failure_preserves_running_container_lease_after_reopen(self):
        self.docker.start_result = execution_fixture.result(code=-9, timeout=True)
        self.docker.stop_result = execution_fixture.result(code=1)
        self.assert_uncertain_persists(self.complete(), execution_fixture.BUILD_CID)
        self.assertEqual(self.docker.events.count("stop"), 1)
        self.assertIs(self.docker.value["State"]["Running"], True)

    def test_stop_timeout_preserves_running_container_lease_after_reopen(self):
        self.docker.start_result = execution_fixture.result(code=-9, timeout=True)
        self.docker.stop_result = execution_fixture.result(code=-9, timeout=True)
        self.assert_uncertain_persists(self.complete(), execution_fixture.BUILD_CID)
        self.assertEqual(self.docker.events.count("stop"), 1)
        self.assertIs(self.docker.value["State"]["Running"], True)

    def test_unverified_create_identity_never_stops_unowned_container(self):
        self.docker.create_hook = lambda: self.docker.value.update(Id=execution_fixture.CID)
        self.assert_uncertain_persists(self.complete(), execution_fixture.BUILD_CID)
        self.assertNotIn("start", self.docker.events)
        self.assertNotIn("stop", self.docker.events)

    def test_signed_terminal_reads_enforce_actor_view_grants_and_live_credentials(self):
        job = self.complete()
        self.assert_terminal(job, "succeeded", 0, False, "verified")
        path = "/v1/jobs/" + job["id"]
        self.assertEqual(self.call("GET", path, {"token": jobs_fixture.OTHER_TOKEN})[0], 404)
        self.assertEqual(self.call("GET", "/v1/jobs", {"token": jobs_fixture.OTHER_TOKEN}), (200, {"jobs": []}))
        self.assertEqual(self.call("GET", "/v1/jobs", {"token": jobs_fixture.TOKEN}), (200, {"jobs": [job]}))
        changes = (("DELETE FROM cp_grants WHERE user_id='alice' AND permission='view'", 403),
                   ("UPDATE cp_users SET enabled=0 WHERE id='alice'", 401),
                   ("UPDATE cp_sessions SET expires=0 WHERE user_id='alice'", 401))
        for sql, expected in changes:
            with self.subTest(sql=sql):
                self.store.reset_credentials()
                self.store.change(sql)
                self.assertEqual(self.call("GET", path, {"token": jobs_fixture.TOKEN})[0], expected)
        self.store.reset_credentials()
        self.assertEqual(self.call("GET", path, {"token": jobs_fixture.TOKEN}), (200, job))
        self.assertEqual(self.execute.call_count, 1)
        self.assert_private(job)

    def test_terminal_handle_hmac_tamper_and_replay_without_network(self):
        job = self.complete()
        self.assert_terminal(job, "succeeded", 0, False, "verified")
        path = "/v1/jobs/" + job["id"]
        body = canonical({"token": jobs_fixture.TOKEN})
        headers = protocol.request_headers(jobs_fixture.KEY, "GET", path, body)
        permissions = list(self.security.permissions)
        self.assertEqual(self.call("GET", path, {}, body + b" ", headers)[0], 401)
        self.assertEqual(self.security.permissions, permissions)
        self.assertEqual(self.call("GET", path, {}, body, headers), (200, job))
        self.assertEqual(self.call("GET", path, {}, body, headers)[0], 409)
        self.assertEqual(self.execute.call_count, 1)
        self.assert_private(job)

    def test_successful_terminal_and_real_artifact_digest_survive_reopen_without_retry(self):
        job = self.complete()
        self.assert_terminal(job, "succeeded", 0, False, "verified")
        calls = copy.deepcopy(self.docker.calls)
        self.reopen()
        self.assertEqual(self.call("GET", "/v1/jobs/" + job["id"], {"token": jobs_fixture.TOKEN}), (200, job))
        self.assertFalse(self.state.busy()["active"])
        self.assertIsNone(self.control._worker)
        self.assertEqual(self.control._tokens, {})
        self.assertEqual(self.docker.calls, calls)
        self.assertEqual(self.execute.call_count, 1)
        self.assert_private(job)

    def test_unenrolled_real_prepare_refuses_without_job_or_sandbox(self):
        self.run.runtime_file = ""
        self.assertEqual(self.call("POST", "/v1/jobs", self.payload()),
                         (503, {"error": "not_enrolled"}))
        self.assertEqual(self.call("GET", "/v1/jobs", {"token": jobs_fixture.TOKEN}), (200, {"jobs": []}))
        with self.state.transaction() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM jobs").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM lease").fetchone()[0], 0)
        self.assertIsNone(self.lease())
        self.assertIsNone(self.control._worker)
        self.assertEqual(self.control._tokens, {})
        self.prepare.assert_called_once_with(self.workspace.value)
        self.execute.assert_not_called()
        self.assertEqual(self.docker.calls, [])
        self.assertEqual(self.produced, [])
        self.assertEqual(list(self.fixture.stages.iterdir()), [])
        self.fixture.fake_chown.assert_not_called()
        self.verify.assert_not_called()

    def test_enrolled_missing_build_entry_refuses_without_job_or_sandbox(self):
        del self.fixture.manifest["build_entry_hash"]
        self.fixture.save_manifest()
        # Keep real enrollment valid so rejection comes from build provisioning.
        self.assertEqual(self.run.runtime_file, str(self.fixture.runtime_file))
        self.assertEqual(self.run.enrollment(), self.fixture.manifest)
        self.assertEqual(self.call("POST", "/v1/jobs", self.payload()),
                         (503, {"error": "isolated builder not provisioned"}))
        self.assertEqual(self.call("GET", "/v1/jobs", {"token": jobs_fixture.TOKEN}), (200, {"jobs": []}))
        with self.state.transaction() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM jobs").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM lease").fetchone()[0], 0)
        self.assertIsNone(self.lease())
        self.assertIsNone(self.control._worker)
        self.assertEqual(self.control._tokens, {})
        self.prepare.assert_called_once_with(self.workspace.value)
        self.execute.assert_not_called()
        self.assertEqual(self.docker.calls, [])
        self.assertEqual(self.produced, [])
        self.assertEqual(list(self.fixture.stages.iterdir()), [])
        self.fixture.fake_chown.assert_not_called()
        self.verify.assert_not_called()

    def test_new_suite_and_chain_parse_python36_syntax(self):
        kwargs = {"feature_version": 6} if sys.version_info >= (3, 8) else {}
        for module in (controller, runner, artifacts, entry, execution_fixture, jobs_fixture):
            path = Path(module.__file__)
            ast.parse(path.read_text(), filename=str(path), **kwargs)
        ast.parse(Path(__file__).read_text(), filename=__file__, **kwargs)


if __name__ == "__main__":
    unittest.main()
