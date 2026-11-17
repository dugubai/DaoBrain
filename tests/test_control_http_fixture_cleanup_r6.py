"""Stdlib-only fixture cleanup regression tests; no real HTTP/server startup."""
import builtins
from collections import deque
import copy
import importlib.util
import os
from pathlib import Path
import socket
import sys
import tempfile
import types
import unittest
from unittest import mock


HTTP_TEST = Path(__file__).with_name("test_control_http_r6.py")


def unit_storage_directory():
    """Validate explicit host TMPDIR without resolving, creating or changing it."""
    directory = os.environ.get("TMPDIR", "")
    if (not os.path.isabs(directory) or os.path.realpath(directory) != directory
            or not os.path.isdir(directory) or Path(directory).is_symlink()):
        raise AssertionError(
            "Cleanup unit storage requires an explicit existing absolute canonical nonsymlink TMPDIR")
    project = str(HTTP_TEST.resolve().parents[1])
    if os.path.commonpath((directory, project)) == project:
        raise AssertionError("Cleanup unit TMPDIR must not be inside the repository")
    # An ancestor TMPDIR is allowed; its per-test child must be a repo sibling.
    return Path(directory)


def load_fixture_module():
    """Load definitions only, suppress bytecode and forbid service imports/sockets."""
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name.split(".")[0] in ("control_plane", "fastapi", "uvicorn", "anyio"):
            raise AssertionError("Cleanup unit tests must not import service code")
        return original_import(name, *args, **kwargs)

    spec = importlib.util.spec_from_file_location("_isolated_http_r6_fixture", str(HTTP_TEST))
    module = importlib.util.module_from_spec(spec)
    with mock.patch.object(sys, "dont_write_bytecode", True), \
            mock.patch.object(builtins, "__import__", side_effect=guarded_import), \
            mock.patch.object(socket, "socket", side_effect=AssertionError("Real sockets forbidden")):
        spec.loader.exec_module(module)
    return module  # Never export the HTTP TestCase into unittest discovery.


def clone_fixture(module):
    base = module.ControlHTTPR6Tests
    attributes = {}
    for name, value in vars(base).items():
        if not name.startswith("__"):
            if isinstance(value, (dict, list, set, deque)):
                value = copy.copy(value)
            attributes[name] = value
    attributes["_class_cleanups"] = []
    attributes["tearDown_exceptions"] = []
    return type("IsolatedHTTPFixture", (base,), attributes)


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now


class FakeThread:
    def __init__(self, name, clock, started=True, alive=True, stop_on_join=True,
                 join_cost=0, start_error=False, join_error=False):
        self.name = name
        self.clock = clock
        self.ident = id(self) if started else None
        self.alive = alive
        self.stop_on_join = stop_on_join
        self.join_cost = join_cost
        self.start_error = start_error
        self.join_error = join_error
        self.joins = []
        self.start_calls = 0
        self.alive_checks = 0

    def start(self):
        self.start_calls += 1
        if self.start_error:
            raise RuntimeError("Synthetic start diagnostic must not be reported by cleanup")
        self.ident = id(self)
        self.alive = True

    def is_alive(self):
        self.alive_checks += 1
        if self.ident is None:
            raise AssertionError("An unstarted thread must not be inspected as live")
        return self.alive

    def join(self, timeout=None):
        if self.ident is None:
            raise RuntimeError("cannot join thread before it is started")
        self.joins.append(timeout)
        if self.join_error:
            raise RuntimeError("Synthetic join diagnostic must not be reported by cleanup")
        self.clock.now += min(timeout, self.join_cost)
        if self.stop_on_join:
            self.alive = False


class FakeLock:
    def __init__(self):
        self.entries = 0
        self.held = False

    def __enter__(self):
        self.entries += 1
        self.held = True
        return self

    def __exit__(self, *args):
        self.held = False


class FakeServer:
    def __init__(self):
        self.shutdown_calls = 0
        self.close_calls = 0
        self.shutdown_error = False
        self.on_shutdown = None

    def serve_forever(self, **kwargs):
        raise AssertionError("Fake server must never run")

    def shutdown(self):
        self.shutdown_calls += 1
        if self.on_shutdown is not None:
            self.on_shutdown()
        if self.shutdown_error:
            raise RuntimeError("Synthetic shutdown diagnostic must not be reported by cleanup")

    def server_close(self):
        self.close_calls += 1


class FakeController:
    def __init__(self):
        self._worker = None
        self.close_calls = 0

    def close(self):
        self.close_calls += 1
        self._worker = None


class FakeSocket:
    def __init__(self):
        self.close_calls = 0
        self.close_error = False

    def close(self):
        self.close_calls += 1
        if self.close_error:
            raise RuntimeError("Synthetic socket diagnostic must not be reported by cleanup")


class TrackedTemporary:
    def __init__(self, directory):
        self.directory = directory
        self.name = directory.name
        self.cleanup_calls = 0
        self.cleanup_error = False

    def cleanup(self):
        self.cleanup_calls += 1
        if self.cleanup_error:
            raise RuntimeError("Synthetic storage diagnostic must not be reported by cleanup")
        self.directory.cleanup()


class ControlHTTPFixtureCleanupR6Tests(unittest.TestCase):
    def setUp(self):
        self.host_environment = dict(os.environ)
        self.host_path = list(sys.path)
        self.host_bytecode = sys.dont_write_bytecode
        self.host_cwd = Path.cwd().resolve()
        self.addCleanup(self.restore_host_if_needed)
        self.work_directory = unit_storage_directory()
        self.module = load_fixture_module()
        self.fixture = clone_fixture(self.module)
        self.storage = tempfile.TemporaryDirectory(
            prefix="cleanup-unit-storage-", dir=str(self.work_directory))
        self.storage_root = Path(self.storage.name)
        # Real test cleanup is independent of the facade and synthetic errors.
        self.addCleanup(self.cleanup_storage)
        self.assert_storage_isolated()
        self.clock = FakeClock()
        self.current = FakeThread("unit-test-current", self.clock)
        self.preexisting = FakeThread("preexisting-worker", self.clock, stop_on_join=False)
        self.enumerated = [self.current, self.preexisting]
        self.original_environment = {"FIXTURE_STATE": "original", "TMPDIR": str(self.storage_root)}
        self.environment = dict(self.original_environment)
        # Facades isolate ALL fixture environment/import/bytecode mutations.
        path_facade = types.SimpleNamespace(
            isabs=os.path.isabs, realpath=os.path.realpath, isdir=os.path.isdir,
            commonpath=os.path.commonpath, join=os.path.join, lexists=lambda path: False)
        self.module.os = types.SimpleNamespace(
            environ=self.environment, path=path_facade, listdir=lambda path: ["lo"],
            chmod=os.chmod, walk=os.walk)
        self.module.sys = types.SimpleNamespace(
            platform="linux", version_info=(3, 11, 0), dont_write_bytecode=False,
            path=["original-import-path"])
        self.module.threading = types.SimpleNamespace(
            current_thread=lambda: self.current, enumerate=lambda: list(self.enumerated),
            Thread=mock.Mock(side_effect=AssertionError("Real fixture threads forbidden")))
        self.module.time = types.SimpleNamespace(monotonic=self.clock.monotonic)
        self.module.importlib = types.SimpleNamespace(
            import_module=mock.Mock(side_effect=AssertionError("Service imports forbidden")))
        self.module.socket = types.SimpleNamespace(
            socket=mock.Mock(side_effect=AssertionError("Real sockets forbidden")))
        self.module.tempfile = types.SimpleNamespace(TemporaryDirectory=self.new_temporary)
        self.build_cleanup_state()

    def restore_host_if_needed(self):
        environment_ok = dict(os.environ) == self.host_environment
        path_ok = sys.path == self.host_path
        bytecode_ok = sys.dont_write_bytecode == self.host_bytecode
        cwd_ok = Path.cwd().resolve() == self.host_cwd
        try:
            self.assertTrue(environment_ok and path_ok and bytecode_ok and cwd_ok,
                            "Cleanup unit tests must not mutate host globals")
        finally:
            if not environment_ok:
                os.environ.clear()
                os.environ.update(self.host_environment)
            if not path_ok:
                sys.path[:] = self.host_path
            if not bytecode_ok:
                sys.dont_write_bytecode = self.host_bytecode
            if not cwd_ok:
                os.chdir(str(self.host_cwd))

    def assert_storage_isolated(self):
        root = str(self.storage_root)
        self.assertTrue(os.path.isabs(root) and os.path.realpath(root) == root)
        self.assertTrue(os.path.isdir(root))
        self.assertFalse(self.storage_root.is_symlink())
        self.assertEqual(self.storage_root.parent, self.work_directory)
        self.assertEqual(str(self.work_directory), self.host_environment["TMPDIR"])
        self.assertNotIn(os.path.commonpath((root, str(self.module.PROJECT))),
                         (root, str(self.module.PROJECT)),
                         "Real storage must be separate from the repository")

    def cleanup_storage(self):
        self.storage.cleanup()
        self.assertFalse(os.path.lexists(str(self.storage_root)), "Real test storage must be removed")

    def new_temporary(self, prefix="cleanup-unit-", dir=None):
        # Never let the facade redirect real storage outside its isolated root.
        self.assertTrue(dir is None or str(dir) == str(self.storage_root),
                        "Unit test storage must use the per-test isolated root")
        self.assert_storage_isolated()
        directory = tempfile.TemporaryDirectory(prefix=prefix, dir=str(self.storage_root))
        self.addCleanup(directory.cleanup)
        self.assertEqual(Path(directory.name).parent, self.storage_root)
        return TrackedTemporary(directory)

    def build_cleanup_state(self):
        cls = self.fixture
        cls.saved_environment = dict(self.original_environment)
        cls.saved_bytecode = False
        cls.saved_path = ["original-import-path"]
        cls.saved_threads = set(self.enumerated)
        cls.saved_attempts = deque([("original-attempt", 1)], maxlen=4)
        cls.security = types.SimpleNamespace(
            _lock=FakeLock(), _attempts=deque([("active-attempt", 2)], maxlen=4))
        self.attempts = cls.security._attempts
        cls.temporary = self.new_temporary()
        cls.base = Path(cls.temporary.name)
        cls.directories = {name: cls.base / name for name in ("source", "workspace")}
        for path in cls.directories.values():
            path.mkdir(mode=0o700)
        cls.source_text = {"marker.txt": "inert cleanup marker\n"}
        self.data_file = cls.directories["source"] / "marker.txt"
        self.data_file.write_text(cls.source_text["marker.txt"], encoding="utf-8")
        self.data_file.chmod(0o400)
        cls.source_before = cls._source_identity()
        cls.tokens = {"synthetic": "inert-token"}
        cls.passwords = {"synthetic": "inert-password"}
        cls.users = {}
        cls.key = b"inert-key"
        cls.browser = types.SimpleNamespace(should_exit=False, force_exit=False)
        cls.browser_thread = None
        cls.browser_socket = FakeSocket()
        cls.controller = FakeController()
        cls.controller_thread = None
        cls.controller_server = FakeServer()
        self.environment.clear()
        self.environment.update({"FIXTURE_STATE": "active"})
        self.module.sys.path[:] = ["fixture-import-path"]
        self.module.sys.dont_write_bytecode = True

    def add_thread(self, name="AnyIO worker thread", **kwargs):
        thread = FakeThread(name, self.clock, **kwargs)
        self.enumerated.append(thread)
        return thread

    def assert_restored(self):
        cls = self.fixture
        self.assertTrue(self.environment == self.original_environment,
                        "Original environment must be restored without diagnostic values")
        self.assertTrue(self.module.sys.path == cls.saved_path, "Original import path must be restored")
        self.assertEqual(self.module.sys.dont_write_bytecode, cls.saved_bytecode)
        self.assertIs(cls.security._attempts, self.attempts)
        self.assertTrue(cls.security._attempts == cls.saved_attempts,
                        "Saved attempts must be restored in place")
        self.assertEqual(cls.security._lock.entries, 1)
        self.assertFalse(cls.security._lock.held)
        self.assertFalse(cls.tokens or cls.passwords or cls.key, "Fixture secrets must be cleared")
        self.assertFalse(cls.base.exists(), "Stopped fixture storage must be removed")

    def assert_retained(self):
        cls = self.fixture
        self.assertTrue(self.environment == {"FIXTURE_STATE": "active"},
                        "A live fixture must keep its active environment")
        self.assertIs(cls.security._attempts, self.attempts)
        self.assertTrue(list(self.attempts) == [("active-attempt", 2)],
                        "Live-thread cleanup must not restore authentication attempts")
        self.assertEqual(cls.security._lock.entries, 0)
        self.assertTrue(cls.tokens and cls.passwords and cls.key, "Live fixture state must be retained")
        self.assertTrue(self.module.sys.path == ["fixture-import-path"])
        self.assertTrue(self.module.sys.dont_write_bytecode)
        self.assertEqual(cls.temporary.cleanup_calls, 0)
        self.assertTrue(self.data_file.is_file(), "Live-thread cleanup must retain real temporary data")

    def test_controlled_import_does_not_export_http_testcase(self):
        discovered = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
        self.assertEqual(discovered.countTestCases(),
                         len(unittest.defaultTestLoader.getTestCaseNames(type(self))))
        self.module.importlib.import_module.assert_not_called()
        self.module.threading.Thread.assert_not_called()
        self.module.socket.socket.assert_not_called()

    def test_invalid_tmpdir_is_rejected_before_import_or_storage(self):
        link = self.storage_root / "tmpdir-link"
        link.symlink_to(self.storage_root, target_is_directory=True)
        missing = self.storage_root / "missing-tmpdir"
        invalid_message = (
            "Cleanup unit storage requires an explicit existing absolute canonical nonsymlink TMPDIR")
        repository_message = "Cleanup unit TMPDIR must not be inside the repository"
        candidates = (
            ("missing", None, invalid_message),
            ("empty", "", invalid_message),
            ("relative", ".", invalid_message),
            ("noncanonical", str(self.storage_root) + "/.", invalid_message),
            ("nonexistent", str(missing), invalid_message),
            ("file", str(self.data_file), invalid_message),
            ("symlink", str(link), invalid_message),
            ("symlink-ancestor", str(link / self.fixture.base.name), invalid_message),
            ("repository", str(self.module.PROJECT), repository_message),
            ("repository-child", str(self.module.PROJECT / "tests"), repository_message),
        )
        for label, directory, message in candidates:
            with self.subTest(tmpdir=label):
                with mock.patch.dict(os.environ), \
                        mock.patch.object(sys.modules[__name__], "load_fixture_module") as load, \
                        mock.patch.object(tempfile, "TemporaryDirectory") as create:
                    if directory is None:
                        os.environ.pop("TMPDIR", None)
                    else:
                        os.environ["TMPDIR"] = directory
                    case = type(self)("test_controlled_import_does_not_export_http_testcase")
                    result = unittest.TestResult()
                    case.run(result)
                    self.assertEqual(result.testsRun, 1)
                    self.assertEqual(len(result.failures), 1)
                    self.assertIn("AssertionError: " + message, result.failures[0][1])
                    self.assertEqual(result.errors, [])
                    self.assertEqual(result.skipped, [])
                    self.assertFalse(hasattr(case, "storage"))
                    load.assert_not_called()
                    create.assert_not_called()
                self.assertEqual(dict(os.environ), self.host_environment)
                self.assertEqual(Path.cwd().resolve(), self.host_cwd)
                self.assertEqual(sys.path, self.host_path)
                self.assertEqual(sys.dont_write_bytecode, self.host_bytecode)
                self.assertFalse(missing.exists())
        self.assert_storage_isolated()

    def test_unstarted_browser_start_failure_is_not_joined(self):
        thread = FakeThread("r6-browser-http", self.clock, started=False, start_error=True)
        self.fixture.browser_thread = thread
        with self.assertRaises(RuntimeError):
            thread.start()
        self.fixture._cleanup()
        self.assertEqual(thread.joins, [])
        self.assertEqual(thread.alive_checks, 0)
        self.assertTrue(self.fixture.browser.should_exit)
        self.assert_restored()

    def test_unstarted_controller_start_failure_is_not_shutdown_or_joined(self):
        thread = FakeThread("r6-controller-http", self.clock, started=False, start_error=True)
        self.module.threading.Thread = mock.Mock(return_value=thread)
        with self.assertRaises(RuntimeError):
            self.fixture._start_controller_thread()
        self.fixture._cleanup()
        self.assertEqual(thread.joins, [])
        self.assertEqual(thread.alive_checks, 0)
        self.assertEqual(self.fixture.controller_server.shutdown_calls, 0)
        self.assertEqual(self.fixture.controller_server.close_calls, 1)
        self.assert_restored()

    def test_controller_join_rechecks_started_state_after_shutdown(self):
        thread = self.add_thread("r6-controller-http")
        self.fixture.controller_thread = thread
        self.fixture.controller_server.on_shutdown = lambda: setattr(thread, "ident", None)
        self.fixture._cleanup()
        self.assertEqual(self.fixture.controller_server.shutdown_calls, 1)
        self.assertEqual(thread.joins, [])
        self.assert_restored()

    def test_cleanup_without_any_saved_fields_is_safe(self):
        cls = clone_fixture(self.module)
        for name in ("saved_environment", "saved_bytecode", "saved_path", "saved_attempts", "saved_threads"):
            self.assertFalse(hasattr(cls, name))
        cls._cleanup()
        self.assertTrue(self.environment == {"FIXTURE_STATE": "active"})
        self.assertTrue(self.module.sys.path == ["fixture-import-path"])
        self.assertTrue(self.module.sys.dont_write_bytecode)

    def run_registered_cleanup(self):
        cleanups = self.fixture._class_cleanups
        self.assertEqual(len(cleanups), 1)
        cleanup, args, kwargs = cleanups.pop()
        cleanup(*args, **kwargs)

    def test_setup_guard_failure_does_not_restore_unsaved_globals(self):
        self.module.sys.platform = "darwin"
        before_environment = dict(self.environment)
        before_path = list(self.module.sys.path)
        with self.assertRaisesRegex(AssertionError, "^HTTP execution requires the approved cloud Python 3.11 image$"):
            self.fixture.setUpClass()
        self.assertIsNone(self.fixture.saved_environment)
        self.run_registered_cleanup()
        self.assertTrue(self.environment == before_environment)
        self.assertTrue(self.module.sys.path == before_path)
        self.assertTrue(self.module.sys.dont_write_bytecode)

    def prepare_setup(self):
        self.environment.clear()
        self.environment.update(self.original_environment)
        self.module.sys.path[:] = ["original-import-path"]
        self.module.sys.dont_write_bytecode = False

    def test_setup_failure_before_saved_point_is_safe(self):
        self.prepare_setup()
        self.module.os.listdir = mock.Mock(side_effect=OSError("Synthetic setup failure"))
        with self.assertRaisesRegex(OSError, "^Synthetic setup failure$"):
            self.fixture.setUpClass()
        self.assertIsNone(self.fixture.saved_environment)
        self.assertIsNone(self.fixture.saved_threads)
        self.run_registered_cleanup()
        self.assertTrue(self.environment == self.original_environment)
        self.assertFalse(self.module.sys.dont_write_bytecode)

    def test_thread_snapshot_precedes_temporary_and_imports(self):
        self.prepare_setup()

        def fail_temporary(**kwargs):
            self.assertEqual(self.fixture.saved_threads, {self.current, self.preexisting})
            self.module.importlib.import_module.assert_not_called()
            self.module.threading.Thread.assert_not_called()
            self.module.socket.socket.assert_not_called()
            self.partial_setup_fault = "temporary creation"
            raise OSError("Synthetic temporary creation failure")

        self.module.tempfile.TemporaryDirectory = mock.Mock(side_effect=fail_temporary)
        with self.assertRaisesRegex(OSError, "^Synthetic temporary creation failure$"):
            self.fixture.setUpClass()
        self.module.tempfile.TemporaryDirectory.assert_called_once_with(
            prefix="control-http-r6-", dir=str(self.storage_root))
        self.assertIsNone(self.fixture.temporary)
        self.run_registered_cleanup()
        self.assertTrue(self.environment == self.original_environment)
        self.assertTrue(self.module.sys.path == ["original-import-path"])
        self.assertFalse(self.module.sys.dont_write_bytecode)

    def test_partially_constructed_source_setup_can_be_cleaned(self):
        self.prepare_setup()
        original_open = Path.open
        failed_paths = []

        def partial_open(path, *args, **kwargs):
            if path.name == "__init__.py" and path.parent.name == "common":
                failed_paths.append(path)
                self.partial_setup_fault = "partial source"
                raise OSError("Synthetic partial source failure")
            return original_open(path, *args, **kwargs)

        with mock.patch.object(Path, "open", new=partial_open):
            with self.assertRaisesRegex(OSError, "^Synthetic partial source failure$"):
                self.fixture.setUpClass()
        base = self.fixture.base
        self.assertEqual(base.parent, self.storage_root)
        self.assertEqual(failed_paths, [base / "source" / "common" / "__init__.py"])
        self.assertTrue((base / "source" / "control_plane" / "registry.py").is_file())
        self.assertIsNone(self.fixture.source_before)
        self.assertEqual(self.fixture.saved_threads, {self.current, self.preexisting})
        self.run_registered_cleanup()
        self.assertEqual(self.fixture.temporary.cleanup_calls, 1)
        self.assertFalse(base.exists())
        self.assertTrue(self.environment == self.original_environment)
        self.assertTrue(self.module.sys.path == ["original-import-path"])
        self.assertFalse(self.module.sys.dont_write_bytecode)
        self.module.importlib.import_module.assert_not_called()
        self.module.threading.Thread.assert_not_called()
        self.module.socket.socket.assert_not_called()

    def run_partial_setup_from_cwd(self, directory, method, expected_fault):
        original_cwd = Path.cwd().resolve()
        original_environment = dict(os.environ)
        original_path = list(sys.path)
        original_bytecode = sys.dont_write_bytecode
        try:
            os.chdir(str(directory))
            # Only cwd changes: real host TMPDIR and all filesystem checks stay intact.
            case = type(self)(method)
            result = unittest.TestResult()
            case.run(result)
            self.assertEqual(result.testsRun, 1)
            self.assertTrue(result.wasSuccessful(), result.errors + result.failures)
            self.assertEqual(result.skipped, [])
            self.assertEqual(case.partial_setup_fault, expected_fault)
            self.assertEqual(case.host_cwd, directory.resolve())
            self.assertEqual(case.work_directory, self.work_directory)
            self.assertEqual(case.storage_root.parent, Path(os.environ["TMPDIR"]))
            self.assertEqual(case.original_environment["TMPDIR"], str(case.storage_root))
            self.assertNotEqual(case.storage_root, self.storage_root)
            self.assertEqual(os.path.commonpath((str(case.storage_root), str(self.storage_root))),
                             str(self.work_directory), "Each test must have sibling storage")
            self.assertFalse(os.path.lexists(str(case.storage_root)))
            self.assertEqual(case.fixture._class_cleanups, [])
            case.module.importlib.import_module.assert_not_called()
            case.module.threading.Thread.assert_not_called()
            case.module.socket.socket.assert_not_called()
            self.assertEqual(dict(os.environ), original_environment)
            self.assertEqual(sys.path, original_path)
            self.assertEqual(sys.dont_write_bytecode, original_bytecode)
            self.assertEqual(Path.cwd().resolve(), directory.resolve())
        finally:
            os.chdir(str(original_cwd))
        self.assertEqual(Path.cwd().resolve(), original_cwd)

    def test_repository_parent_cwd_reaches_temporary_creation_failure(self):
        self.run_partial_setup_from_cwd(
            self.module.PROJECT.parent, "test_thread_snapshot_precedes_temporary_and_imports",
            "temporary creation")

    def test_repository_parent_cwd_reaches_partial_source_failure(self):
        self.run_partial_setup_from_cwd(
            self.module.PROJECT.parent, "test_partially_constructed_source_setup_can_be_cleaned",
            "partial source")

    def test_repository_cwd_reaches_temporary_creation_failure(self):
        self.run_partial_setup_from_cwd(
            self.module.PROJECT, "test_thread_snapshot_precedes_temporary_and_imports",
            "temporary creation")

    def test_repository_cwd_reaches_partial_source_failure(self):
        self.run_partial_setup_from_cwd(
            self.module.PROJECT, "test_partially_constructed_source_setup_can_be_cleaned",
            "partial source")

    def test_missing_workspace_during_partial_setup_is_safe(self):
        self.fixture.source_before = None
        self.fixture.directories = {"source": self.fixture.directories["source"]}
        self.fixture._cleanup()
        self.assert_restored()

    def test_live_anyio_worker_retains_all_shared_state_and_real_data(self):
        worker = self.add_thread(stop_on_join=False, join_cost=3)
        with self.assertRaisesRegex(AssertionError,
                                    "^Fixture threads did not stop; shared state and storage retained$"):
            self.fixture._cleanup()
        self.assertEqual(worker.joins, [3])
        self.assertEqual(self.fixture.browser_socket.close_calls, 1)
        self.assertEqual(self.fixture.controller_server.close_calls, 1)
        self.assertEqual(self.fixture.controller.close_calls, 1)
        self.assert_retained()

    def test_new_thread_join_budget_is_shared_and_excludes_old_current_unstarted(self):
        workers = [self.add_thread(stop_on_join=False, join_cost=3) for _ in range(2)]
        unstarted = self.add_thread("unstarted", started=False)
        with self.assertRaises(AssertionError):
            self.fixture._cleanup()
        timeouts = [timeout for worker in workers for timeout in worker.joins]
        self.assertEqual(len(timeouts), 2)
        self.assertLessEqual(sum(timeouts), 3)
        self.assertLessEqual(self.clock.now, 3)
        self.assertEqual(self.current.joins, [])
        self.assertEqual(self.preexisting.joins, [])
        self.assertEqual(unstarted.joins, [])
        self.assertEqual(unstarted.alive_checks, 0)
        self.assert_retained()

    def test_stopped_pool_workers_restore_deque_in_place(self):
        worker = self.add_thread(join_cost=1)
        self.fixture._cleanup()
        self.assertEqual(len(worker.joins), 1)
        self.assertEqual(self.fixture.security._attempts.maxlen, 4)
        self.assert_restored()

    def test_stopped_threads_restore_dict_in_place(self):
        cls = self.fixture
        cls.saved_attempts = {"original-attempt": [1]}
        cls.security._attempts = {"active-attempt": [2]}
        self.attempts = cls.security._attempts
        cls.browser_thread = self.add_thread("r6-browser-http")
        cls.controller_thread = self.add_thread("r6-controller-http")
        self.fixture._cleanup()
        self.assertEqual(cls.browser_thread.joins, [8])
        self.assertEqual(cls.controller_thread.joins, [6])
        self.assert_restored()

    def test_socket_cleanup_error_still_restores_and_reports_fixed_error(self):
        self.fixture.browser_socket.close_error = True
        self.fixture.controller_thread = self.add_thread("r6-controller-http")
        with self.assertRaisesRegex(AssertionError, "^Browser socket did not close cleanly$"):
            self.fixture._cleanup()
        self.assertEqual(self.fixture.controller_server.shutdown_calls, 1)
        self.assertEqual(self.fixture.controller_server.close_calls, 1)
        self.assertEqual(self.fixture.controller.close_calls, 1)
        self.assert_restored()

    def test_shutdown_error_still_joins_closes_and_restores(self):
        self.fixture.controller_thread = self.add_thread("r6-controller-http")
        self.fixture.controller_server.shutdown_error = True
        with self.assertRaisesRegex(AssertionError, "^Controller HTTP shutdown failed$"):
            self.fixture._cleanup()
        self.assertEqual(self.fixture.controller_thread.joins, [6])
        self.assertEqual(self.fixture.controller_server.close_calls, 1)
        self.assert_restored()

    def test_join_error_for_stopped_thread_does_not_block_restoration(self):
        self.fixture.browser_thread = self.add_thread("r6-browser-http", alive=False, join_error=True)
        with self.assertRaisesRegex(AssertionError, "^Browser HTTP thread join failed$"):
            self.fixture._cleanup()
        self.assert_restored()

    def test_temporary_cleanup_error_still_restores_other_globals(self):
        cls = self.fixture
        cls.temporary.cleanup_error = True
        with self.assertRaisesRegex(AssertionError, "^Fixture temporary storage cleanup failed$"):
            cls._cleanup()
        self.assertTrue(self.environment == self.original_environment)
        self.assertIs(cls.security._attempts, self.attempts)
        self.assertTrue(self.attempts == cls.saved_attempts)
        self.assertFalse(cls.tokens or cls.passwords or cls.key)
        self.assertTrue(self.module.sys.path == cls.saved_path)
        self.assertFalse(self.module.sys.dont_write_bytecode)
        self.assertTrue(self.data_file.is_file())

    def test_source_identity_error_still_deletes_and_restores(self):
        self.fixture._source_identity = classmethod(
            lambda cls: (_ for _ in ()).throw(OSError("Synthetic source diagnostic")))
        with self.assertRaisesRegex(AssertionError, "^Synthetic source identity check failed$"):
            self.fixture._cleanup()
        self.assert_restored()

    def test_permission_step_error_still_deletes_and_restores(self):
        self.module.os.chmod = mock.Mock(side_effect=OSError("Synthetic permission diagnostic"))
        with self.assertRaisesRegex(AssertionError, "^Fixture workspace permissions cleanup failed$"):
            self.fixture._cleanup()
        self.assert_restored()

    def test_controller_close_cannot_hide_live_worker_reference(self):
        worker = FakeThread("unexpected-worker", self.clock, stop_on_join=False)
        self.fixture.controller._worker = worker
        with self.assertRaisesRegex(AssertionError,
                                    "^Unenrolled fixture unexpectedly created an execution worker; "
                                    "Fixture threads did not stop; shared state and storage retained$"):
            self.fixture._cleanup()
        self.assertIsNone(self.fixture.controller._worker)
        self.assert_retained()

    def test_unknown_controller_worker_state_fails_closed_but_still_closes(self):
        close = mock.Mock()
        controller_type = type("UnknownWorkerController", (), {
            "_worker": mock.PropertyMock(side_effect=RuntimeError("Synthetic worker diagnostic")),
            "close": close})
        self.fixture.controller = controller_type()
        with self.assertRaisesRegex(AssertionError,
                                    "^Controller worker state could not be checked; "
                                    "Fixture threads did not stop; shared state and storage retained$"):
            self.fixture._cleanup()
        close.assert_called_once_with()
        self.assert_retained()

    def test_thread_appearing_at_final_check_blocks_restoration(self):
        late_worker = FakeThread("AnyIO worker thread", self.clock, stop_on_join=False)
        self.module.threading.enumerate = mock.Mock(side_effect=[
            list(self.enumerated), list(self.enumerated) + [late_worker]])
        with self.assertRaisesRegex(AssertionError,
                                    "^Fixture threads did not stop; shared state and storage retained$"):
            self.fixture._cleanup()
        self.assertEqual(late_worker.joins, [])
        self.assert_retained()

    def test_attempts_restoration_error_does_not_block_environment_restoration(self):
        lock_type = type("FailingLock", (), {
            "__enter__": mock.Mock(side_effect=RuntimeError("Synthetic lock diagnostic")),
            "__exit__": mock.Mock()})
        cls = self.fixture
        cls.security._lock = lock_type()
        with self.assertRaisesRegex(AssertionError, "^Authentication attempts restoration failed$"):
            cls._cleanup()
        self.assertTrue(self.environment == self.original_environment)
        self.assertTrue(self.module.sys.path == cls.saved_path)
        self.assertFalse(self.module.sys.dont_write_bytecode)
        self.assertFalse(cls.tokens or cls.passwords or cls.key)
        self.assertFalse(cls.base.exists())
        self.assertIs(cls.security._attempts, self.attempts)
        self.assertTrue(list(self.attempts) == [("active-attempt", 2)])

    def test_failed_thread_enumeration_fails_closed(self):
        self.module.threading.enumerate = mock.Mock(side_effect=RuntimeError("Synthetic enumeration diagnostic"))
        with self.assertRaisesRegex(AssertionError,
                                    "^Fixture threads could not be enumerated; "
                                    "Fixture threads could not be enumerated; "
                                    "Fixture threads did not stop; shared state and storage retained$"):
            self.fixture._cleanup()
        self.assert_retained()


if __name__ == "__main__":
    unittest.main(verbosity=2)
