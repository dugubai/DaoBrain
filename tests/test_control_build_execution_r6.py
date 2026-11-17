"""Stdlib admission/review tests, NOT real Docker or cloud acceptance.

FakeDocker returns synthetic inspection/exit data. Its producer exercises the
real legacy build() on synthetic source files (syntax-only, never executed).
chown and main's rlimit are explicit fakes: no Mac UID/config changes, sockets,
HTTP, business suites, installs or network operations occur.
"""
import ast
import builtins
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import resource
import stat
import struct
import sys
import tempfile
import unittest
import warnings
import zipfile
from unittest import mock

from control_plane import build_artifacts as artifacts
from control_plane import runner
from control_plane import sandbox_build_entry as entry
from control_plane.registry import ControlError, canonical, digest

JOB = "3" * 32
CID = "e" * 64
BUILD_CID = "f" * 64
IMAGE = "sha256:" + "d" * 64
ENTRY = "control_plane/sandbox_build_entry.py"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def result(output=b"", code=0, timeout=False, truncated=False, seen=None):
    return runner.CommandResult(code, output, len(output) if seen is None else seen,
                                truncated, timeout)


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


class FakeDocker:
    """FAKE local Docker adapter. Never invokes Docker or subprocesses."""
    def __init__(self, test):
        self.test = test
        self.calls = []
        self.events = []
        self.value = None
        self.start_result = result(b"UNTRUSTED_PRIVATE_STDOUT", seen=99999, truncated=True)
        self.create_result = result((BUILD_CID + "\n").encode("ascii"))
        self.stop_result = result()
        self.exit_code = 0
        self.oom = False
        self.produce = True
        self.mutate_report = None
        self.create_hook = None
        self.after_start = None
        self.stay_running = False

    def __call__(self, argv, timeout, output_limit):
        self.test.assertEqual(argv[:2], ["/usr/bin/docker", "--host=unix:///var/run/docker.sock"])
        self.test.assertTrue(0 < timeout <= 180)
        self.test.assertLessEqual(output_limit, runner.INSPECT_LIMIT)
        command = argv[2:]
        self.calls.append((list(command), timeout, output_limit))
        self.events.append(command[0])
        if command == ["image", "inspect", IMAGE]:
            return result(canonical({"items": [self.test.image]})[9:-1])
        if command[:3] == ["inspect", "--type", "container"]:
            self.test.assertIn(command[-1], (CID, BUILD_CID))
            value = self.test.runtime if command[-1] == CID else self.value
            blob = json.dumps([value], sort_keys=True).encode("utf-8")
            return result(blob)
        if command[0] == "create":
            labels, mounts = {}, []
            for index, value in enumerate(command):
                if value == "--label":
                    key, item = command[index + 1].split("=", 1)
                    labels[key] = item
                if value == "--mount":
                    parts = command[index + 1].split(",")
                    fields = dict(p.split("=", 1) for p in parts if "=" in p)
                    mounts.append({"Type": fields["type"], "Source": fields["src"],
                                   "Destination": fields["dst"], "RW": "readonly" not in parts,
                                   "Propagation": "rprivate"})
            self.value = {
                "Id": BUILD_CID, "Image": IMAGE,
                "Name": "/" + command[command.index("--name") + 1],
                "Config": {"User": "10001:10001", "Entrypoint": ["python"],
                           "Cmd": command[-2:], "Env": list(self.test.image["Config"]["Env"]),
                           "WorkingDir": "/sandbox", "StopTimeout": 5,
                           "Labels": dict(self.test.image["Config"]["Labels"], **labels)},
                "HostConfig": {"NetworkMode": "none", "ReadonlyRootfs": True, "Privileged": False,
                               "CapDrop": ["ALL"], "SecurityOpt": ["no-new-privileges"],
                               "Memory": 192 * runner.MIB, "MemorySwap": 192 * runner.MIB,
                               "PidsLimit": 64, "NanoCpus": 500000000,
                               "IpcMode": "private", "RestartPolicy": {"Name": "no"},
                               "Tmpfs": {"/tmp": "rw,noexec,nosuid,size=32m"},
                               "LogConfig": {"Type": "none"}},
                "Mounts": mounts, "NetworkSettings": {"Networks": {"none": {}}, "Ports": {}},
                "State": {"Running": False, "Status": "created", "ExitCode": 0, "OOMKilled": False},
            }
            if self.create_hook:
                self.create_hook()
            return self.create_result
        if command == ["start", "--attach", BUILD_CID]:
            self.value["State"].update(Running=True, Status="running")
            if self.start_result.timed_out:
                return self.start_result
            if self.produce:
                self.test.produce_artifact(self.test.prepared, self.mutate_report)
            self.value["State"].update(Running=self.stay_running,
                                        Status="running" if self.stay_running else "exited",
                                        ExitCode=self.exit_code, OOMKilled=self.oom)
            if self.after_start:
                self.after_start()
            return self.start_result
        if command == ["stop", "--time", "5", BUILD_CID]:
            if not self.stop_result.returncode and not self.stop_result.timed_out:
                self.value["State"].update(Running=False, Status="exited", ExitCode=137, OOMKilled=False)
            return self.stop_result
        raise AssertionError("fake adapter forbids operation: " + repr(command))


class BuildExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="build-execution-r6-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(os.path.realpath(self.temp.name))
        self.host = self.root / "host-source"
        self.snapshot_root = self.root / "snapshot"
        self.private = self.root / "private"
        self.stages = self.private / "stages"
        for path in (self.host, self.snapshot_root, self.private, self.stages):
            path.mkdir(parents=True, exist_ok=True)
        self.private.chmod(0o700)
        self.stages.chmod(0o700)
        self.runtime_file = self.private / "runtime.json"
        self.entry_bytes = Path(entry.__file__).read_bytes()
        write(self.host / ENTRY, self.entry_bytes)
        write(self.host / "control_plane/controller.py", b"DO_NOT_COPY_CONTROLLER")
        write(self.host / "data/business.db", b"DO_NOT_COPY_SYNTHETIC_DB")
        write(self.host / "secrets/key", b"DO_NOT_COPY_SYNTHETIC_SECRET")
        self.image = {"Id": IMAGE, "Config": {
            "Labels": {"com.daobrain.control.sandbox": "r6-source-only"},
            "Env": ["PATH=/usr/bin:/bin"],
        }}
        self.runtime = {
            "Id": CID, "Image": IMAGE,
            "Config": {"User": "10001:10001", "Labels": {
                "com.docker.compose.project": "daobrain-r6-build-unit",
                "com.docker.compose.service": "gateway", runner.OWNER_LABEL: "daobrain-r6",
                runner.SOURCE_LABEL: "a" * 64, runner.CONFIG_LABEL: "b" * 64,
            }},
            "HostConfig": {"Privileged": False, "ReadonlyRootfs": True,
                           "CapDrop": ["ALL"], "SecurityOpt": ["no-new-privileges"],
                           "IpcMode": "private", "NetworkMode": "none",
                           "RestartPolicy": {"Name": "no"}, "PidsLimit": 64,
                           "NanoCpus": 500000000, "Memory": 64 * runner.MIB,
                           "MemorySwap": 64 * runner.MIB},
            "Mounts": [], "NetworkSettings": {"Networks": {}},
            "State": {"Running": False, "Status": "exited"},
        }
        self.manifest = {"project": "daobrain-r6-build-unit", "source_root": str(self.host),
                         "app_image": IMAGE, "budget_mib": 900, "build_entry_hash": sha(self.entry_bytes),
                         "runtimes": {"gateway": {"container_id": CID, "source_hash": "a" * 64,
                                                  "config_hash": "b" * 64,
                                                  "inspect_hash": digest(runner.inspection_binding(self.runtime))}}}
        self.save_manifest()
        self.docker = FakeDocker(self)
        self.run = runner.Runner(str(self.runtime_file), str(self.host), self.docker,
                                 lambda: 2048, str(self.stages))
        # Explicit fake: not even one real chown is allowed in these tests.
        patcher = mock.patch.object(runner.os, "chown")
        self.fake_chown = patcher.start()
        self.addCleanup(patcher.stop)
        self.prepared = None

    def save_manifest(self):
        self.runtime_file.write_bytes(canonical(self.manifest))
        self.runtime_file.chmod(0o600)

    def snapshot(self, source=None):
        source = source if source is not None else {
            "gateway/main.py": b"raise RuntimeError('synthetic candidate must never execute')\n",
            "common/__init__.py": b"# immutable import baseline\n",
            "application_hub/__init__.py": b"",
            "portable_frontend/index.html": b"<p>synthetic immutable import baseline</p>\n",
        }
        files = []
        for path, data in sorted(source.items()):
            write(self.snapshot_root / path, data)
            files.append({"path": path, "sha256": sha(data), "editable": path.startswith("gateway/")})
        return {"root": str(self.snapshot_root), "component": "gateway", "files": files,
                "source_hash": digest([{"path": item["path"], "sha256": item["sha256"]} for item in files])}

    def prepare(self, source=None):
        self.prepared = self.run.prepare_build(self.snapshot(source))
        return self.prepared

    def produce_artifact(self, prepared, mutate=None):
        # Real ZIP from the actual build() on synthetic fixture source only.
        metadata = entry.build(Path(prepared["source"]), Path(prepared["output"]))
        report = {"job_id": JOB, "source_hash": prepared["source_hash"],
                  "artifact_sha256": metadata["sha256"], "artifact_bytes": metadata["bytes"],
                  "artifact_files": metadata["files"], "kind": metadata["kind"],
                  "syntax_checked": True, "image_built": False, "release_approved": False}
        if mutate:
            mutate(report)
        (Path(prepared["output"]) / artifacts.RESULT_NAME).write_bytes(canonical(report))
        return report

    def review(self):
        p = self.prepared
        return artifacts.verify_artifacts(p["output"], JOB, p["source_hash"], p["source_entries"], p["output_identity"])

    def fresh_output(self):
        # Keep previous files untouched; switch to another real empty output directory.
        output = Path(tempfile.mkdtemp(prefix="review-", dir=str(self.stages)))
        info = output.stat()
        self.prepared["output"] = str(output)
        self.prepared["output_identity"] = (info.st_dev, info.st_ino)
        return output

    def rewrite_zip(self, records, compression=zipfile.ZIP_STORED, mode=stat.S_IFREG | 0o444):
        p = self.prepared
        output = Path(p["output"])
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(str(output / artifacts.ZIP_NAME), "w", compression=compression) as archive:
                for name, data in records:
                    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                    info.external_attr = mode << 16
                    info.compress_type = compression
                    archive.writestr(info, data)
        report = json.loads((output / artifacts.RESULT_NAME).read_text())
        blob = (output / artifacts.ZIP_NAME).read_bytes()
        report.update(artifact_sha256=sha(blob), artifact_bytes=len(blob))
        (output / artifacts.RESULT_NAME).write_bytes(canonical(report))

    def zip_records(self):
        with zipfile.ZipFile(str(Path(self.prepared["output"]) / artifacts.ZIP_NAME)) as archive:
            return [(v.filename, archive.read(v)) for v in archive.infolist()]

    def test_prepare_without_test_suites_private_output_and_exact_copy(self):
        p = self.prepare()
        self.assertNotIn("test_fixtures", p["manifest"])
        self.assertNotIn("status", p)
        self.fake_chown.assert_called_once_with(p["output"], 10001, 10001)
        self.assertEqual(stat.S_IMODE(Path(p["output"]).parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.stages.stat().st_mode), 0o700)
        self.assertEqual(list(Path(p["output"]).iterdir()), [])
        copied = {str(v.relative_to(Path(p["trusted"]))) for v in Path(p["trusted"]).rglob("*") if v.is_file()}
        self.assertEqual(copied, {ENTRY})
        self.assertEqual((Path(p["trusted"]) / ENTRY).read_bytes(), self.entry_bytes)
        for path, data in p["source_entries"].items():
            self.assertEqual((Path(p["source"]) / path).read_bytes(), data)
            self.assertEqual(stat.S_IMODE((Path(p["source"]) / path).stat().st_mode), 0o444)
        self.assertFalse(any(v[0][0] == "create" for v in self.docker.calls))

    def test_missing_entry_and_unconfigured_build_legacy_503(self):
        del self.manifest["build_entry_hash"]
        self.save_manifest()
        for operation in (lambda: self.run.build(), lambda: self.run.prepare_build(self.snapshot())):
            with self.assertRaises(ControlError) as caught:
                operation()
            self.assertEqual(caught.exception.status, 503)
            self.assertEqual(caught.exception.detail, "isolated builder not provisioned")
        self.run.runtime_file = str(self.private / "missing.json")
        with self.assertRaises(ControlError) as caught:
            self.run.build()
        self.assertEqual(caught.exception.detail, "isolated builder not provisioned")
        self.assertEqual(self.docker.calls, [])

    def test_enrollment_hash_is_exact_and_entry_drift_refused(self):
        for invalid in (None, 12, "A" * 64, "a" * 64 + "\n", "short"):
            self.manifest["build_entry_hash"] = invalid
            self.save_manifest()
            with self.assertRaises(ControlError):
                self.run.enrollment()
        self.manifest["build_entry_hash"] = sha(self.entry_bytes)
        self.save_manifest()
        write(self.host / ENTRY, self.entry_bytes + b"# changed\n")
        with self.assertRaises(ControlError):
            self.run.prepare_build(self.snapshot())
        self.assertEqual(self.docker.calls, [])

    def test_resource_and_image_readiness_is_admission_not_execution(self):
        value = self.run.status()["build"]
        self.assertTrue(value["ready"])
        self.assertIn("execution not attested", value["reason"])
        self.assertFalse(any(v[0][0] in ("create", "start") for v in self.docker.calls))
        self.run.available = lambda: 384 + 192 + 64 - 1
        self.assertFalse(self.run.status()["build"]["ready"])
        with self.assertRaises(ControlError):
            self.run.prepare_build(self.snapshot())
        self.run.available = lambda: 2048
        self.runtime["HostConfig"].update(Memory=800 * runner.MIB, MemorySwap=800 * runner.MIB)
        self.manifest["runtimes"]["gateway"]["inspect_hash"] = digest(runner.inspection_binding(self.runtime))
        self.save_manifest()
        with self.assertRaises(ControlError):
            self.run.prepare_build(self.snapshot())
        self.assertEqual(self.fake_chown.call_count, 0)

    def test_unreviewed_images_and_inherited_environment_are_rejected(self):
        original = copy.deepcopy(self.image)
        for fields in ({"Labels": {}}, {"Volumes": {"/data": {}}},
                       {"OnBuild": ["RUN arbitrary"]}, {"Env": ["CONTROL_KEY_FILE=/private/key"]},
                       {"ExposedPorts": {"8000/tcp": {}}}, {"Env": None}):
            self.image = copy.deepcopy(original)
            self.image["Config"].update(fields)
            with self.assertRaises(ControlError):
                self.run.prepare_build(self.snapshot())
            self.assertFalse(self.run.status()["build"]["ready"])
        self.assertEqual(list(self.stages.iterdir()), [])

    def test_output_ownership_failure_fails_admission(self):
        self.fake_chown.side_effect = PermissionError("fake denial")
        with self.assertRaises(ControlError) as caught:
            self.prepare()
        self.assertEqual(caught.exception.status, 503)
        self.assertFalse(any(v[0][0] == "create" for v in self.docker.calls))

    def test_shared_sanitizer_preserves_baselines_hash_and_source_limits(self):
        snapshot = self.snapshot()
        data, targets, total = self.run.sanitize_snapshot(snapshot)
        self.assertEqual(targets, {"gateway"})
        self.assertIn("common/__init__.py", data)
        snapshot["source_hash"] = "0" * 64
        with self.assertRaises(ControlError):
            self.run.prepare_build(snapshot)
        for path in ("gateway/../escape.py", "gateway/secret.py", "tests/test_recipe.py", "control_plane/main.py"):
            with self.assertRaises(ControlError):
                self.run.prepare_build({"root": str(self.snapshot_root), "files": [path], "source_hash": "0" * 64})
        snapshot = self.snapshot()
        snapshot["files"][1]["editable"] = True  # Protected baseline cannot become editable.
        with self.assertRaises(ControlError):
            self.run.prepare_build(snapshot)
        snapshot = self.snapshot()
        snapshot["files"] *= 513
        with self.assertRaises(ControlError):
            self.run.prepare_build(snapshot)
        snapshot = self.snapshot({"gateway/main.py": b"#" * (runner.MAX_SOURCE_BYTES + 1)})
        with self.assertRaises(ControlError):
            self.run.prepare_build(snapshot)

    def test_full_enrollment_and_staged_source_rechecked_before_create(self):
        self.prepare()
        self.manifest["test_entry_hash"] = "a" * 64  # Even unrelated enrollment fields are bound.
        self.save_manifest()
        with self.assertRaises(ControlError):
            self.run.run_build(self.prepared, JOB, lambda cid: None)
        self.assertNotIn("create", self.docker.events)
        del self.manifest["test_entry_hash"]
        self.save_manifest()
        path = Path(self.prepared["source"]) / "gateway/main.py"
        path.chmod(0o600)
        path.write_bytes(b"VALUE=9\n")
        with self.assertRaises(ControlError):
            self.run.run_build(self.prepared, JOB, lambda cid: None)
        self.assertNotIn("create", self.docker.events)

    def test_correct_artifact_authorizations_fixed_mounts_command_and_bounded_result(self):
        p = self.prepare()
        ids = []
        def authorize():
            self.docker.events.append("authorize")
        original = artifacts.verify_artifacts
        def stopped_review(*args):
            self.assertIs(self.docker.value["State"]["Running"], False)
            self.docker.events.append("review")
            return original(*args)
        with mock.patch.object(runner, "verify_artifacts", side_effect=stopped_review):
            value = self.run.run_build(p, JOB, ids.append, authorize)
        self.assertEqual(value["status"], "succeeded")
        self.assertEqual(ids, [BUILD_CID])
        for event in ("create", "start"):
            self.assertEqual(self.docker.events[self.docker.events.index(event) - 1], "authorize")
        self.assertEqual(self.docker.events.count("authorize"), 2)
        self.assertGreater(self.docker.events.index("review"), self.docker.events.index("start"))
        self.assertEqual(value["source_hash"], p["source_hash"])
        self.assertEqual(value["artifact_files"], len(p["source_entries"]))
        self.assertEqual(value["artifact_sha256"], sha((Path(p["output"]) / artifacts.ZIP_NAME).read_bytes()))
        self.assertIs(value["image_built"], False)
        self.assertIs(value["release_approved"], False)
        self.assertLess(len(canonical(value)), runner.OUTPUT_LIMIT)
        self.assertNotIn("UNTRUSTED_PRIVATE_STDOUT", str(value))
        self.assertNotIn(p["output"], str(value))
        self.assertNotIn("artifact", value)
        create = next(v for v in self.docker.calls if v[0][0] == "create")[0]
        for flag, expected in (("--pull", "never"), ("--network", "none"), ("--user", "10001:10001"),
                               ("--memory", "192m"), ("--memory-swap", "192m"), ("--cpus", "0.5"),
                               ("--pids-limit", "64"), ("--cap-drop", "ALL"), ("--security-opt", "no-new-privileges"),
                               ("--tmpfs", "/tmp:rw,noexec,nosuid,size=32m"), ("--log-driver", "none")):
            self.assertEqual(create[create.index(flag) + 1], expected)
        self.assertIn("--read-only", create)
        self.assertEqual(create[-3:], [IMAGE, "-I", "/trusted/control_plane/sandbox_build_entry.py"])
        mounts = [create[i + 1] for i, v in enumerate(create) if v == "--mount"]
        self.assertEqual(mounts, ["type=bind,src=" + p["source"] + ",dst=/sandbox,readonly",
                                  "type=bind,src=" + p["trusted"] + ",dst=/trusted,readonly",
                                  "type=bind,src=" + p["output"] + ",dst=/artifacts"])
        self.assertEqual(json.loads((Path(p["trusted"]) / "build-request.json").read_text()),
                         {"job_id": JOB, "source_hash": p["source_hash"]})
        self.assertIn((["start", "--attach", BUILD_CID], 180, runner.OUTPUT_LIMIT), self.docker.calls)
        self.assertFalse(any(v[0][0] in ("rm", "build", "exec", "pull", "export") for v in self.docker.calls))

    def test_authorization_denied_before_create_and_before_start(self):
        for deny_at in (1, 2):
            self.prepare()
            calls = []
            self.docker.events = []
            def authorize():
                calls.append(True)
                if len(calls) == deny_at:
                    raise ControlError("expired", 401)
            with self.assertRaises(ControlError) as caught:
                self.run.run_build(self.prepared, JOB, lambda cid: None, authorize)
            self.assertEqual(caught.exception.status, 401)
            self.assertNotIn("start", self.docker.events)
            self.assertEqual("create" in self.docker.events, deny_at == 2)
            self.assertFalse(getattr(caught.exception, "execution_uncertain", False))

    def test_enrollment_change_after_create_blocks_start(self):
        self.prepare()
        def change():
            self.manifest["test_entry_hash"] = "0" * 64
            self.save_manifest()
        self.docker.create_hook = change
        with self.assertRaises(ControlError) as caught:
            self.run.run_build(self.prepared, JOB, lambda cid: None)
        self.assertFalse(caught.exception.execution_uncertain)
        self.assertNotIn("start", self.docker.events)

    def test_uncertain_create_and_record_failures_keep_lease(self):
        for created in (result(timeout=True), result(code=1), result(b"short-id"),
                        result((BUILD_CID + "\n").encode(), truncated=True)):
            self.prepare()
            self.docker.create_result = created
            with self.assertRaises(ControlError) as caught:
                self.run.run_build(self.prepared, JOB, lambda cid: None)
            self.assertTrue(caught.exception.execution_uncertain)
        self.prepare()
        self.docker.create_result = result(BUILD_CID.encode())
        def failed_record(cid):
            raise RuntimeError("fake persistence failure")
        with self.assertRaises(ControlError) as caught:
            self.run.run_build(self.prepared, JOB, failed_record)
        self.assertTrue(caught.exception.execution_uncertain)

    def test_timeout_stops_only_verified_own_container(self):
        self.prepare()
        self.docker.start_result = result(code=-9, timeout=True)
        value = self.run.run_build(self.prepared, JOB, lambda cid: None)
        self.assertEqual(value["status"], "timed_out")
        self.assertEqual(set(value), {"status", "exit_code", "oom_killed", "output_bytes",
                                     "output_truncated", "output_retained", "metadata_limit"})
        self.assertEqual(value["exit_code"], 137)
        self.assertIs(value["oom_killed"], False)
        self.assertIn((["stop", "--time", "5", BUILD_CID], 10, runner.OUTPUT_LIMIT), self.docker.calls)
        self.assertFalse(self.docker.value["State"]["Running"])
        self.assertFalse(any(v[0][0] == "stop" and v[0][-1] != BUILD_CID for v in self.docker.calls))

    def test_unverified_identity_never_stopped_and_stop_uncertainty_keeps_lease(self):
        self.prepare()
        self.docker.create_hook = lambda: self.docker.value.update(Id=CID)
        with self.assertRaises(ControlError) as caught:
            self.run.run_build(self.prepared, JOB, lambda cid: None)
        self.assertTrue(caught.exception.execution_uncertain)
        self.assertNotIn("stop", self.docker.events)
        self.prepare()
        self.docker.create_hook = None
        self.docker.start_result = result(code=-9, timeout=True)
        self.docker.stop_result = result(code=1)
        with self.assertRaises(ControlError) as caught:
            self.run.run_build(self.prepared, JOB, lambda cid: None)
        self.assertTrue(caught.exception.execution_uncertain)
        self.assertEqual(self.docker.events.count("stop"), 1)

    def test_zero_exit_with_tampered_report_is_failed_not_attested(self):
        for key, invalid in (("job_id", "4" * 32), ("source_hash", "0" * 64), ("artifact_bytes", 1)):
            self.prepare()
            self.docker.mutate_report = lambda report, k=key, v=invalid: report.update({k: v})
            value = self.run.run_build(self.prepared, JOB, lambda cid: None)
            self.assertEqual(value["status"], "failed")
            self.assertEqual(value["exit_code"], 0)
            self.assertEqual(set(value), {"status", "exit_code", "oom_killed", "output_bytes",
                                         "output_truncated", "output_retained", "metadata_limit"})
            self.assertIs(value["output_retained"], False)

    def test_admission_does_not_compile_candidate_source(self):
        snapshot = self.snapshot({"gateway/main.py": b"def invalid(:\n"})
        with mock.patch.object(builtins, "compile", side_effect=AssertionError("host must not compile")):
            prepared = self.run.prepare_build(snapshot)
        self.assertEqual(prepared["source_entries"]["gateway/main.py"], b"def invalid(:\n")
        # Only the isolated fixed entry may later syntax-check this source.
        self.assertNotIn("create", self.docker.events)

    def test_actual_exit_oom_missing_state_and_zero_exit_without_artifacts(self):
        for code, oom in ((137, True), (1, False), (0, True)):
            self.prepare()
            self.docker.exit_code, self.docker.oom = code, oom
            value = self.run.run_build(self.prepared, JOB, lambda cid: None)
            self.assertEqual(value["status"], "failed")
            self.assertEqual(value["exit_code"], code)
            self.assertIs(value["oom_killed"], oom)
            self.assertEqual(set(value), {"status", "exit_code", "oom_killed", "output_bytes",
                                         "output_truncated", "output_retained", "metadata_limit"})
        self.prepare()
        self.docker.exit_code, self.docker.oom, self.docker.produce = 0, False, False
        self.assertEqual(self.run.run_build(self.prepared, JOB, lambda cid: None)["status"], "failed")
        for field, invalid in (("OOMKilled", None), ("ExitCode", True), ("ExitCode", 999), ("Running", None)):
            self.prepare()
            self.docker.after_start = lambda f=field, v=invalid: self.docker.value["State"].update({f: v})
            with self.assertRaises(ControlError) as caught:
                self.run.run_build(self.prepared, JOB, lambda cid: None)
            self.assertTrue(caught.exception.execution_uncertain)

    def test_inspection_rejects_isolation_mutations(self):
        self.prepare()
        self.run.run_build(self.prepared, JOB, lambda cid: None)
        value = copy.deepcopy(self.docker.value)
        mutations = [lambda v: v.update(Image="sha256:" + "0" * 64),
                     lambda v: v.update(Name="/another-job"),
                     lambda v: v["Config"].update(StopTimeout=30),
                     lambda v: v["HostConfig"].update(AutoRemove=True),
                     lambda v: v["Config"].update(User="0:0"),
                     lambda v: v["Config"].update(Cmd=["-c", "arbitrary"]),
                     lambda v: v["Config"].update(Env=["SECRET=synthetic"]),
                     lambda v: v["HostConfig"].update(NetworkMode="host"),
                     lambda v: v["HostConfig"].update(ReadonlyRootfs=False),
                     lambda v: v["HostConfig"].update(SecurityOpt=["no-new-privileges", "seccomp=unconfined"]),
                     lambda v: v["HostConfig"].update(MemorySwap=-1),
                     lambda v: v["HostConfig"].update(LogConfig={"Type": "json-file"}),
                     lambda v: v["HostConfig"].update(PortBindings={"80/tcp": [{}]}),
                     lambda v: v["Mounts"][0].update(RW=True),
                     lambda v: v["Mounts"][2].update(Propagation="rshared"),
                     lambda v: v["Mounts"].append(dict(v["Mounts"][0]))]
        for mutate in mutations:
            changed = copy.deepcopy(value)
            mutate(changed)
            with self.assertRaises(ControlError):
                self.run._verify_build(changed, BUILD_CID, JOB, self.prepared)

    def test_real_artifact_review_does_not_compile_import_exec_or_extract(self):
        self.prepare()
        report = self.produce_artifact(self.prepared)
        with mock.patch.object(builtins, "compile", side_effect=AssertionError("no host compile")), \
                mock.patch.object(zipfile.ZipFile, "extract", side_effect=AssertionError("no extraction")), \
                mock.patch.object(zipfile.ZipFile, "extractall", side_effect=AssertionError("no extraction")):
            self.assertEqual(self.review(), report)
        self.assertEqual(artifacts.canonical([{"path": "gateway/main.py", "sha256": "a" * 64}]),
                         canonical([{"path": "gateway/main.py", "sha256": "a" * 64}]))

    def test_report_job_hash_bytes_files_digest_and_types_cannot_be_forged(self):
        self.prepare()
        for key, invalid in (("job_id", "4" * 32), ("source_hash", "0" * 64),
                             ("artifact_sha256", "0" * 64), ("artifact_bytes", 1),
                             ("artifact_files", True), ("syntax_checked", 1),
                             ("image_built", True), ("release_approved", True), ("extra", "not allowed")):
            self.fresh_output()
            self.produce_artifact(self.prepared, lambda r, k=key, v=invalid: r.update({k: v}))
            with self.assertRaises(artifacts.ArtifactError):
                self.review()

    def test_duplicate_nonfinite_and_oversized_result_json_rejected(self):
        self.prepare()
        self.produce_artifact(self.prepared)
        path = Path(self.prepared["output"]) / artifacts.RESULT_NAME
        original = path.read_bytes()
        for blob in (original[:-1] + b',"job_id":"' + JOB.encode() + b'"}',
                     b'{"value":NaN}', b'{"value":1e999}', b'{"value":"\\ud800"}',
                     b'[]', b' ' * (artifacts.RESULT_LIMIT + 1)):
            path.write_bytes(blob)
            with self.assertRaises(artifacts.ArtifactError):
                self.review()

    def test_duplicate_extra_traversal_encrypted_and_compressed_zip_rejected(self):
        self.prepare()
        self.produce_artifact(self.prepared)
        original = self.zip_records()
        for records in (original + [original[0]], original + [("gateway/extra.py", b"x=1")],
                        [("../escape.py", original[0][1])] + original[1:],
                        [("/escape.py", original[0][1])] + original[1:]):
            self.rewrite_zip(records)
            with self.assertRaises(artifacts.ArtifactError):
                self.review()
        self.rewrite_zip(original, compression=zipfile.ZIP_DEFLATED)
        with self.assertRaises(artifacts.ArtifactError):
            self.review()
        self.rewrite_zip(original, mode=stat.S_IFLNK | 0o777)
        with self.assertRaises(artifacts.ArtifactError):
            self.review()
        self.rewrite_zip(original)
        path = Path(self.prepared["output"]) / artifacts.ZIP_NAME
        blob = bytearray(path.read_bytes())
        struct.pack_into("<H", blob, 6, 1)  # Encrypted local header, digest honestly repinned.
        path.write_bytes(blob)
        report_path = Path(self.prepared["output"]) / artifacts.RESULT_NAME
        report = json.loads(report_path.read_text())
        report["artifact_sha256"] = sha(blob)
        report_path.write_bytes(canonical(report))
        with self.assertRaises(artifacts.ArtifactError):
            self.review()

    def test_zip_source_bytes_manifest_digest_and_duplicate_manifest_json_rejected(self):
        self.prepare()
        self.produce_artifact(self.prepared)
        original = self.zip_records()
        self.rewrite_zip([(name, b"VALUE=999\n" if name == "gateway/main.py" else data) for name, data in original])
        with self.assertRaises(artifacts.ArtifactError):
            self.review()
        for duplicate in (False, True):
            records = []
            for name, data in original:
                if name == "BUILD-MANIFEST.json":
                    metadata = json.loads(data.decode())
                    if duplicate:
                        data = data[:-1] + b',"kind":"offline-source-artifact"}'
                    else:
                        metadata["source_manifest"]["gateway/main.py"] = "0" * 64
                        data = canonical(metadata)
                records.append((name, data))
            self.rewrite_zip(records)
            with self.assertRaises(artifacts.ArtifactError):
                self.review()

    def test_special_hardlinked_symlink_extra_and_replaced_output_rejected(self):
        self.prepare()
        for kind in ("fifo", "symlink", "hardlink", "directory", "extra"):
            output = self.fresh_output()
            self.produce_artifact(self.prepared)
            path = output / artifacts.RESULT_NAME
            if kind == "extra":
                (output / "extra").write_bytes(b"unexpected")
            else:
                retained = output / "retained-original"
                path.rename(retained)
                if kind == "fifo":
                    os.mkfifo(str(path))
                elif kind == "symlink":
                    path.symlink_to(retained)
                elif kind == "hardlink":
                    os.link(str(retained), str(path))
                else:
                    path.mkdir()
                # Retain original outside the two-file output, without deleting.
                retained.rename(self.stages / ("retained-" + kind))
            with self.assertRaises(artifacts.ArtifactError):
                self.review()
        output = self.fresh_output()
        self.produce_artifact(self.prepared)
        output.rename(output.with_name(output.name + "-retained"))
        output.mkdir()
        with self.assertRaises(artifacts.ArtifactError):
            self.review()

    def test_special_zip_nodes_and_hardlinks_refused(self):
        self.prepare()
        for kind in ("fifo", "symlink", "hardlink", "directory"):
            output = self.fresh_output()
            self.produce_artifact(self.prepared)
            path = output / artifacts.ZIP_NAME
            retained = self.stages / ("retained-zip-" + kind)
            path.rename(retained)
            if kind == "fifo":
                os.mkfifo(str(path))
            elif kind == "symlink":
                path.symlink_to(retained)
            elif kind == "hardlink":
                os.link(str(retained), str(path))
            else:
                path.mkdir()
            with self.assertRaises(artifacts.ArtifactError):
                self.review()

    def test_forged_eocd_count_rejected_before_zipfile_allocation(self):
        self.prepare()
        self.produce_artifact(self.prepared)
        original = self.zip_records()
        # Many repeated central records, but claim the expected small EOCD count.
        self.rewrite_zip(original + [original[0]] * 100)
        output = Path(self.prepared["output"])
        path = output / artifacts.ZIP_NAME
        blob = bytearray(path.read_bytes())
        struct.pack_into("<HH", blob, len(blob) - 14, len(original), len(original))
        path.write_bytes(blob)
        report_path = output / artifacts.RESULT_NAME
        report = json.loads(report_path.read_text())
        report["artifact_sha256"] = sha(blob)
        report_path.write_bytes(canonical(report))
        with mock.patch.object(artifacts.zipfile, "ZipFile", side_effect=AssertionError("allocation forbidden")) as parser:
            with self.assertRaises(artifacts.ArtifactError):
                self.review()
            parser.assert_not_called()

    def test_output_enumeration_stops_on_third_node(self):
        self.prepare()
        output = Path(self.prepared["output"])
        for unused in range(20):
            (output / str(unused)).write_bytes(b"extra")
        directory = os.open(str(output), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            with mock.patch.object(artifacts.os, "listdir", side_effect=AssertionError("no allocation")):
                with self.assertRaises(artifacts.ArtifactError):
                    artifacts._output_names(directory)
        finally:
            os.close(directory)

    def test_zip_and_producer_output_caps_checked_before_read_or_write(self):
        self.prepare()
        report = self.produce_artifact(self.prepared)
        path = Path(self.prepared["output"]) / artifacts.ZIP_NAME
        with path.open("r+b") as stream:
            stream.truncate(artifacts.ARTIFACT_LIMIT + 1)
        with self.assertRaises(artifacts.ArtifactError):
            self.review()
        output = self.fresh_output()
        with mock.patch.object(entry, "ARTIFACT_LIMIT", 100):
            with self.assertRaises(ValueError):
                entry.build(Path(self.prepared["source"]), output)
        self.assertEqual(list(output.iterdir()), [])

    def main_remap(self, prepared):
        mapping = {"/sandbox": Path(prepared["source"]), "/artifacts": Path(prepared["output"]),
                   "/trusted/build-request.json": Path(prepared["trusted"]) / "build-request.json",
                   "/var/run/docker.sock": self.root / "absent-socket",
                   "/run/secrets": self.root / "absent-secrets"}
        return mock.patch.object(entry, "Path", side_effect=lambda value: mapping.get(value, Path(value)))

    def test_fixed_main_strict_request_hash_bound_result_two_files_and_fake_rlimit(self):
        self.prepare()
        path = Path(self.prepared["trusted"]) / "build-request.json"
        path.write_bytes(canonical({"job_id": JOB, "source_hash": self.prepared["source_hash"]}))
        old = sys.dont_write_bytecode
        with self.main_remap(self.prepared), mock.patch.object(resource, "setrlimit") as fake_limit, \
                mock.patch.object(resource, "getrlimit", return_value=(resource.RLIM_INFINITY, resource.RLIM_INFINITY)), \
                mock.patch.object(sys, "stdout", io.StringIO()) as stdout:
            try:
                self.assertEqual(entry.main(), 0)
            finally:
                sys.dont_write_bytecode = old
        fake_limit.assert_called_once_with(resource.RLIMIT_FSIZE, (entry.ARTIFACT_LIMIT, entry.ARTIFACT_LIMIT))
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(set(p.name for p in Path(self.prepared["output"]).iterdir()),
                         {artifacts.RESULT_NAME, artifacts.ZIP_NAME})
        self.assertEqual(self.review()["job_id"], JOB)
        self.assertFalse(list(Path(self.prepared["source"]).rglob("*.pyc")))

    def test_main_request_duplicate_extra_hash_mismatch_and_size_refused(self):
        self.prepare()
        path = Path(self.prepared["trusted"]) / "build-request.json"
        valid = canonical({"job_id": JOB, "source_hash": self.prepared["source_hash"]})
        variants = [valid[:-1] + b',"job_id":"' + JOB.encode() + b'"}',
                    canonical({"job_id": JOB, "source_hash": "0" * 64}),
                    valid[:-1] + b',"command":"arbitrary"}',
                    b'{"job_id":NaN,"source_hash":"0"}', b' ' * (entry.REQUEST_LIMIT + 1)]
        for blob in variants:
            path.write_bytes(blob)
            with self.main_remap(self.prepared), mock.patch.object(resource, "setrlimit"), \
                    mock.patch.object(resource, "getrlimit", return_value=(resource.RLIM_INFINITY, resource.RLIM_INFINITY)), \
                    mock.patch.object(sys, "dont_write_bytecode", True):
                self.assertEqual(entry.main(), 1)
            self.assertEqual(list(Path(self.prepared["output"]).iterdir()), [])

    def test_control_files_parse_python36_syntax(self):
        for module in (runner, artifacts, entry):
            kwargs = {"feature_version": 6} if sys.version_info >= (3, 8) else {}
            ast.parse(Path(module.__file__).read_text(), filename=module.__file__, **kwargs)
        kwargs = {"feature_version": 6} if sys.version_info >= (3, 8) else {}
        ast.parse(Path(__file__).read_text(), filename=__file__, **kwargs)


if __name__ == "__main__":
    unittest.main()
