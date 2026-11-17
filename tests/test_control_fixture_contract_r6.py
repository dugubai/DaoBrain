"""Stdlib admission-contract tests using real files and SHA-256 digests.

FakeDockerAdapter returns synthetic inspect/image metadata only: no Docker,
subprocess, network, pytest, or business suite is executed. The harness argument
recorder below is also a fake, not evidence that the fixed business suites pass.
"""
import ast
import builtins
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import types
import unittest
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from control_plane import runner as runner_module
from control_plane import sandbox_test_entry as sandbox_entry
from control_plane.registry import ControlError, digest
from control_plane.runner import CommandResult, MIB, Runner, inspection_binding

# Independent expectation catches accidental changes to the trusted recipe.
EXPECTED_FIXTURES = (
    "tests/test_card_workflow_r5.py",
    "tests/test_business_r5.py",
    "tests/test_local_modules.py",
)
ENTRY = "control_plane/sandbox_test_entry.py"


def sha256(data):
    return hashlib.sha256(data).hexdigest()


class FakeDockerAdapter:
    """FAKE Docker metadata adapter; rejects all execution commands."""

    def __init__(self, inspection, image):
        self.inspection = inspection
        self.image = image
        self.calls = []

    def __call__(self, argv, timeout, output_limit):
        self.calls.append(list(argv))
        if argv[:2] != ["/usr/bin/docker", "--host=unix:///var/run/docker.sock"]:
            raise AssertionError("fixed local Docker endpoint required")
        command = argv[2:]
        if command == ["inspect", "--type", "container", self.inspection["Id"]]:
            value = self.inspection
        elif command == ["image", "inspect", self.image["Id"]]:
            value = self.image
        else:
            raise AssertionError("fake adapter forbids execution: " + repr(command))
        data = json.dumps([value], sort_keys=True).encode("utf-8")
        if len(data) > output_limit or timeout <= 0:
            raise AssertionError("invalid fake command bounds")
        return CommandResult(0, data, len(data), False, False)


class FixtureContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="fixture-contract-r6-")
        self.addCleanup(self.temp.cleanup)
        # macOS temporary paths can have symlinked ancestors.
        self.root = Path(os.path.realpath(self.temp.name))
        self.trusted_root = self.root / "host-source"
        self.workspace = self.root / "editable-workspace"
        self.private_root = self.root / "control"
        self.staging_root = self.private_root / "stages"
        for path in (self.trusted_root, self.workspace, self.private_root, self.staging_root):
            path.mkdir(parents=True, exist_ok=True)
        self.private_root.chmod(0o700)
        self.staging_root.chmod(0o700)
        self.runtime_file = self.private_root / "runtime.json"
        self.trusted_bytes = {ENTRY: Path(sandbox_entry.__file__).read_bytes()}
        for path in EXPECTED_FIXTURES:
            self.trusted_bytes[path] = (
                "# Synthetic trusted admission fixture: " + path + "\n"
                "raise AssertionError('admission tests must not execute this fixture')\n"
            ).encode("utf-8")
        for path, data in self.trusted_bytes.items():
            self.write(self.trusted_root, path, data)
        self.fixed_hashes = {path: sha256(self.trusted_bytes[path])
                             for path in EXPECTED_FIXTURES}
        image_id = "sha256:" + sha256(b"fake reviewed image")
        container_id = sha256(b"fake enrolled container")
        source_hash = sha256(b"fake enrolled source")
        config_hash = sha256(b"fake enrolled config")
        inspection = {
            "Id": container_id, "Image": image_id,
            "Config": {"User": "10001:10001", "Labels": {
                "com.docker.compose.project": "daobrain-r6-fixture-contract",
                "com.docker.compose.service": "gateway",
                runner_module.OWNER_LABEL: "daobrain-r6",
                runner_module.SOURCE_LABEL: source_hash,
                runner_module.CONFIG_LABEL: config_hash,
            }},
            "HostConfig": {
                "Privileged": False, "ReadonlyRootfs": True,
                "CapDrop": ["ALL"], "SecurityOpt": ["no-new-privileges:true"],
                "IpcMode": "private", "NetworkMode": "none",
                "RestartPolicy": {"Name": "no"}, "PidsLimit": 64,
                "NanoCpus": 500000000, "Memory": 64 * MIB, "MemorySwap": 64 * MIB,
            },
            "Mounts": [], "NetworkSettings": {"Networks": {}},
            "State": {"Running": False, "Status": "exited"},
        }
        image = {"Id": image_id, "Config": {
            "Labels": {"com.daobrain.control.sandbox": "r6-source-only"},
            "Env": ["PATH=/usr/bin:/bin"],
        }}
        self.fake_docker = FakeDockerAdapter(inspection, image)
        self.manifest = {
            "project": "daobrain-r6-fixture-contract",
            "source_root": str(self.trusted_root), "app_image": image_id,
            "budget_mib": 900,
            "runtimes": {"gateway": {
                "container_id": container_id, "source_hash": source_hash,
                "config_hash": config_hash, "inspect_hash": digest(inspection_binding(inspection)),
            }},
            "test_entry_hash": sha256(self.trusted_bytes[ENTRY]),
            "test_fixtures": {directory: dict(self.fixed_hashes)
                              for directory in ("gateway", "rag_public")},
        }
        self.runner = Runner(runtime_file=str(self.runtime_file),
                             source_root=str(self.trusted_root),
                             command=self.fake_docker, available=lambda: 2048,
                             staging_root=str(self.staging_root))
        self.save_manifest()

    def write(self, root, relative, data):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def save_manifest(self):
        self.runtime_file.write_bytes(json.dumps(self.manifest, sort_keys=True).encode("utf-8"))
        self.runtime_file.chmod(0o600)

    def snapshot(self, directories=("gateway",)):
        entries = []
        for directory in directories:
            relative = directory + "/main.py"
            data = ("# Real editable source for " + directory + "\nVALUE = 1\n").encode("utf-8")
            self.write(self.workspace, relative, data)
            entries.append({"path": relative, "editable": True, "sha256": sha256(data)})
        result = {"root": str(self.workspace), "files": entries,
                  "source_hash": digest([{"path": entry["path"], "sha256": entry["sha256"]}
                                         for entry in sorted(entries, key=lambda item: item["path"])])}
        if len(directories) == 1:
            result["component"] = directories[0]
        return result

    def assert_rejected(self, detail, status, snapshot=None):
        self.save_manifest()
        with self.assertRaises(ControlError) as caught:
            self.runner.prepare_test(snapshot if snapshot is not None else self.snapshot())
        self.assertEqual(caught.exception.detail, detail)
        self.assertEqual(caught.exception.status, status)
        self.assertEqual(list(self.staging_root.iterdir()), [])
        self.assertTrue(all(call[2:4] == ["inspect", "--type"]
                            for call in self.fake_docker.calls))

    def test_changed_files_keep_python36_syntax(self):
        for path in (Path(runner_module.__file__), Path(sandbox_entry.__file__), Path(__file__)):
            with self.subTest(path=str(path)):
                options = {"feature_version": 6} if sys.version_info >= (3, 8) else {}
                ast.parse(path.read_text(encoding="utf-8"), filename=str(path), **options)

    def test_recipe_is_static_and_import_does_not_execute_main(self):
        self.assertEqual(sandbox_entry.TEST_FIXTURE_PATHS, EXPECTED_FIXTURES)
        self.assertIs(runner_module.TEST_FIXTURE_PATHS, sandbox_entry.TEST_FIXTURE_PATHS)
        spec = importlib.util.spec_from_file_location("fixture_contract_import_probe",
                                                     sandbox_entry.__file__)
        module = importlib.util.module_from_spec(spec)
        original_import = builtins.__import__

        def guarded_import(name, *args, **kwargs):
            if name == "pytest":
                raise AssertionError("constant import must not import pytest")
            return original_import(name, *args, **kwargs)

        with mock.patch("builtins.__import__", side_effect=guarded_import), \
                mock.patch.object(sys, "exit") as exit_call:
            spec.loader.exec_module(module)
        exit_call.assert_not_called()
        self.assertEqual(module.TEST_FIXTURE_PATHS, EXPECTED_FIXTURES)

    def test_each_missing_suite_is_rejected(self):
        for path in EXPECTED_FIXTURES:
            with self.subTest(path=path):
                selected = dict(self.fixed_hashes)
                del selected[path]
                self.manifest["test_fixtures"]["gateway"] = selected
                self.assert_rejected("trusted focused test fixture not provisioned", 503)

    def test_extra_suite_cannot_become_a_recipe(self):
        extra = "tests/test_arbitrary_manifest_recipe.py"
        data = b"# Real extra file; a valid digest does not authorize a recipe.\n"
        self.write(self.trusted_root, extra, data)
        self.manifest["test_fixtures"]["gateway"][extra] = sha256(data)
        self.assert_rejected("trusted focused test fixture not provisioned", 503)

    def test_missing_or_invalid_selected_component_mapping_is_rejected(self):
        for selected in (None, {}, [], "not-a-mapping"):
            with self.subTest(selected=selected):
                self.manifest["test_fixtures"]["gateway"] = selected
                self.assert_rejected("trusted focused test fixture not provisioned", 503)
        self.manifest["test_fixtures"] = []
        self.assert_rejected("trusted focused test fixture not provisioned", 503)

    def test_every_selected_component_requires_all_three_suites(self):
        snapshot = self.snapshot(("gateway", "rag_public"))
        for directory in ("gateway", "rag_public"):
            with self.subTest(directory=directory):
                self.manifest["test_fixtures"] = {
                    name: dict(self.fixed_hashes) for name in ("gateway", "rag_public")}
                del self.manifest["test_fixtures"][directory][EXPECTED_FIXTURES[0]]
                self.assert_rejected("trusted focused test fixture not provisioned", 503, snapshot)

    def test_malformed_hashes_are_rejected(self):
        for value in (None, 123, "invalid", "A" * 64, "0" * 64 + "\n"):
            with self.subTest(value=value):
                self.manifest["test_fixtures"]["gateway"] = dict(self.fixed_hashes)
                self.manifest["test_fixtures"]["gateway"][EXPECTED_FIXTURES[0]] = value
                self.assert_rejected("unsafe trusted fixture", 503)

    def test_valid_but_wrong_pinned_hashes_are_rejected(self):
        for path in EXPECTED_FIXTURES:
            with self.subTest(path=path):
                self.manifest["test_fixtures"]["gateway"] = dict(self.fixed_hashes)
                self.manifest["test_fixtures"]["gateway"][path] = sha256(b"different content")
                self.assert_rejected("trusted fixture hash mismatch", 409)

    def test_real_trusted_file_hash_drift_is_rejected(self):
        for path in EXPECTED_FIXTURES:
            with self.subTest(path=path):
                self.write(self.trusted_root, path, self.trusted_bytes[path] + b"# drift\n")
                self.assert_rejected("trusted fixture hash mismatch", 409)
                self.write(self.trusted_root, path, self.trusted_bytes[path])

    def test_real_harness_hash_drift_is_rejected(self):
        self.write(self.trusted_root, ENTRY, self.trusted_bytes[ENTRY] + b"# drift\n")
        self.assert_rejected("trusted harness hash mismatch", 409)

    def test_cross_component_hash_conflicts_are_rejected_before_fixture_reads(self):
        snapshot = self.snapshot(("gateway", "rag_public"))
        for directory in ("gateway", "rag_public"):
            with self.subTest(directory=directory):
                self.manifest["test_fixtures"] = {
                    name: dict(self.fixed_hashes) for name in ("gateway", "rag_public")}
                self.manifest["test_fixtures"][directory][EXPECTED_FIXTURES[0]] = sha256(b"conflict")
                with mock.patch.object(runner_module, "source_file",
                                       wraps=runner_module.source_file) as read_file:
                    self.assert_rejected("conflicting trusted fixture hashes", 409, snapshot)
                self.assertTrue(all(call[0][0] == str(self.workspace)
                                    for call in read_file.call_args_list))

    def test_correct_fixed_set_is_fully_copied_without_asserting_a_pass(self):
        prepared = self.runner.prepare_test(self.snapshot())
        trusted = Path(prepared["trusted"])
        actual = {str(path.relative_to(trusted)) for path in trusted.rglob("*") if path.is_file()}
        self.assertEqual(actual, set(EXPECTED_FIXTURES) | {ENTRY})
        for relative, data in self.trusted_bytes.items():
            target = trusted / relative
            self.assertEqual(target.read_bytes(), data)
            self.assertEqual(sha256(target.read_bytes()), sha256(data))
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o444)
        self.assertEqual((Path(prepared["source"]) / "gateway/main.py").read_bytes(),
                         (self.workspace / "gateway/main.py").read_bytes())
        self.assertFalse((Path(prepared["source"]) / "tests").exists())
        self.assertNotIn("status", prepared)
        self.assertNotIn("ready", prepared)
        status = self.runner.status()
        self.assertFalse(status["test"]["ready"])
        self.assertTrue(status["test"]["fixtures_declared"])
        self.assertFalse(status["build"]["ready"])
        self.assertTrue(any(call[2:4] == ["image", "inspect"] for call in self.fake_docker.calls))

    def test_consistent_components_share_one_complete_copy_and_one_read(self):
        with mock.patch.object(runner_module, "source_file",
                               wraps=runner_module.source_file) as read_file:
            prepared = self.runner.prepare_test(self.snapshot(("gateway", "rag_public")))
        for relative in (ENTRY,) + EXPECTED_FIXTURES:
            calls = [call for call in read_file.call_args_list
                     if call[0][:2] == (str(self.trusted_root), relative)]
            self.assertEqual(len(calls), 1)
            self.assertEqual((Path(prepared["trusted"]) / relative).read_bytes(),
                             self.trusted_bytes[relative])

    def test_workspace_cannot_supply_a_missing_trusted_suite(self):
        relative = EXPECTED_FIXTURES[0]
        self.write(self.workspace, relative, self.trusted_bytes[relative])
        path = self.trusted_root / relative
        path.rename(path.with_suffix(".unavailable"))
        self.assert_rejected("unsafe source artifact", 422)

    def test_workspace_test_in_snapshot_inventory_is_rejected(self):
        snapshot = self.snapshot()
        relative = EXPECTED_FIXTURES[0]
        data = self.trusted_bytes[relative]
        self.write(self.workspace, relative, data)
        snapshot["files"].append({"path": relative, "editable": True, "sha256": sha256(data)})
        snapshot["source_hash"] = digest([
            {"path": item["path"], "sha256": item["sha256"]}
            for item in sorted(snapshot["files"], key=lambda item: item["path"])])
        self.assert_rejected("artifact outside source import allowlist", 422, snapshot)

    def test_unenrolled_status_has_independent_test_and_build_readiness(self):
        self.runner.runtime_file = str(self.private_root / "not-enrolled.json")
        status = self.runner.status()
        self.assertFalse(status["enrolled"])
        self.assertEqual(status["readiness"], "not_enrolled")
        self.assertEqual(status["runtimes"], {})
        self.assertFalse(status["test"]["ready"])
        self.assertFalse(status["test"]["fixtures_declared"])
        self.assertEqual(status["test"]["reason"], "not_enrolled")
        self.assertFalse(status["build"]["ready"])
        self.assertIsNot(status["test"], status["build"])
        self.assertEqual(self.fake_docker.calls, [])

    def test_harness_uses_exact_prepared_paths_with_fake_pytest_recorder(self):
        prepared = self.runner.prepare_test(self.snapshot())
        fake_pytest = types.ModuleType("pytest")
        # FAKE argument recorder; never imports or executes the synthetic suites.
        fake_pytest.main = mock.Mock(return_value=0)
        remapped = {"/sandbox": Path(prepared["source"]),
                    "/trusted": Path(prepared["trusted"]),
                    "/var/run/docker.sock": self.root / "absent-docker.sock",
                    "/run/secrets": self.root / "absent-secrets"}
        with mock.patch.object(sandbox_entry, "Path", side_effect=lambda value: remapped[value]), \
                mock.patch.object(sys, "path", list(sys.path)), \
                mock.patch.dict(os.environ), \
                mock.patch.dict(sys.modules, {"pytest": fake_pytest}):
            self.assertEqual(sandbox_entry.main(), 0)  # Fake return code only.
        arguments = fake_pytest.main.call_args[0][0]
        self.assertEqual(arguments[-3:], [str(Path(prepared["trusted"]) / path)
                                         for path in EXPECTED_FIXTURES])
        self.assertIn("--noconftest", arguments)
        self.assertIn("--confcutdir=/trusted/tests", arguments)
        self.assertEqual(fake_pytest.main.call_count, 1)


if __name__ == "__main__":
    unittest.main()
