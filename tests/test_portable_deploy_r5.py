"""Standard-library offline regression suite; runnable directly or with pytest.

Fixtures use tempfile's default directory (controlled by TMPDIR) and are retained.
Real subprocesses and sockets are blocked by default. Docker and bind probes are
argument-recording doubles only. No dependency install, app import, host settings,
real credentials or network.
"""
import ast
import copy
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import importlib.util
import io
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("portable_deploy_r5", ROOT / "scripts/deploy_portable.py")
deploy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(deploy)


def forbidden(*_args, **_kwargs):
    raise AssertionError("real subprocess/socket is forbidden in portable deployment tests")


def source_function(relative, name, namespace):
    """Execute only an audited pure function, not imports or ASGI app startup."""
    tree = ast.parse((ROOT / relative).read_text())
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name)
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(ROOT / relative), "exec"), namespace)
    return namespace[name]


class BindMock:
    def __init__(self):
        self.calls = []
        self.fail = False
        self.closed = 0

    def __call__(self, family, kind):
        assert family == socket.AF_INET and kind == socket.SOCK_STREAM
        recorder = self

        class Probe:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                recorder.closed += 1

            def bind(self, address):
                recorder.calls.append(address)
                if recorder.fail:
                    raise OSError("synthetic occupied port; never print")

        return Probe()


class DockerMock:
    def __init__(self, model_path=None):
        self.calls = []
        self.endpoint = "unix:///synthetic/docker.sock"
        self.ostype = "linux"
        self.arch = "x86_64"
        self.version = "2.24.7"
        self.engine_memory = 2048 * deploy.MIB
        self.fail = None
        self.image_os = "linux"
        self.image_arch = "amd64"
        self.variant = ""
        self.entrypoints = {deploy.APP_IMAGE: None, deploy.MODEL_IMAGE: ["/bin/ollama"]}
        self.volume = {"driver": "local", "mountpoint": model_path, "device": "", "type": "", "options": ""}
        self.ids = []
        self.port_records = []
        self.on_config = None

    def __call__(self, argv, **kwargs):
        assert isinstance(argv, list) and all(isinstance(arg, str) for arg in argv)
        assert kwargs["shell"] is False and kwargs["stdout"] == subprocess.PIPE
        assert kwargs["stderr"] == subprocess.PIPE and kwargs["universal_newlines"] is True
        assert "capture_output" not in kwargs and "text" not in kwargs
        assert kwargs["check"] is False and kwargs["timeout"] == 60
        self.calls.append((list(argv), kwargs))
        if self.fail and self.fail(argv):
            return subprocess.CompletedProcess(argv, 1, "credential-do-not-print", "credential-do-not-print")
        if "context" in argv:
            output = "default" if "show" in argv else self.endpoint
        elif "info" in argv:
            output = json.dumps({"os": self.ostype, "arch": self.arch, "memory": self.engine_memory})
        elif "version" in argv:
            output = self.version
        elif "image" in argv:
            image = argv[argv.index("inspect") + 1]
            output = json.dumps({"os": self.image_os, "arch": self.image_arch,
                                 "variant": self.variant, "entrypoint": self.entrypoints[image]})
        elif "volume" in argv:
            output = json.dumps(self.volume)
        elif "container" in argv:
            output = "\n".join(self.ids if "ls" in argv else self.port_records)
        elif "config" in argv:
            if self.on_config:
                self.on_config(argv)
            output = ""
        elif "up" in argv:
            output = ""
        else:
            raise AssertionError("unexpected CLI operation: " + repr(argv))
        return subprocess.CompletedProcess(argv, 0, output, "")


class PortableDeployTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix=".portable-r5-test-"))
        self.source = self.tmp / "source"
        self.data = self.tmp / "data"
        self.source.mkdir()
        self.data.mkdir()
        required = ["portable_frontend/main.py", "portable_frontend/index.html",
                    "application_hub/main.py", "application_hub/boundary.py", "application_hub/nodes.py",
                    "application_hub/security.py", "application_hub/store.py", "local_node/main.py",
                    "public_qa/main.py", "public_qa/model.py", "public_qa/index.html",
                    "common/catalog.py", "common/qa.py", "common/schemas.py", "common/corpus_fixtures.py"]
        required += [module + "/main.py" for module in
                     ("card_workflow", "knowledge_library", "clinical_records", "media_review", "membership")]
        for relative in required:
            path = self.source / relative
            path.parent.mkdir(exist_ok=True)
            path.write_text("# synthetic source; never imported\n" if path.suffix == ".py" else "<p>synthetic</p>")
        self.config = {"role": "standalone", "source": str(self.source), "data": str(self.data),
                       "APP_ORIGIN": "http://127.0.0.1:18121"}
        self.key = self.tmp / "node-secret"
        self.key.write_text("synthetic-test-secret-never-real-" * 2)
        self.key.chmod(0o600)
        self.node = {"NODE_ID": "test-node", "public_capability": "classic-search",
                     "node_key_file": str(self.key), "node_endpoint": "https://10.20.30.40:443",
                     "node_transport": "tls"}
        self.docker = DockerMock()
        self.bind = BindMock()
        self.patch(deploy.subprocess, "run", forbidden)
        self.patch(deploy.socket, "socket", forbidden)
        clean = {key: value for key, value in os.environ.items()
                 if not key.startswith("DOCKER_") and not key.startswith("COMPOSE_")}
        self.environment = patch.dict(os.environ, clean, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def patch(self, obj, name, value):
        handle = patch.object(obj, name, value)
        handle.start()
        self.addCleanup(handle.stop)

    def mock_cli(self):
        self.patch(deploy.shutil, "which", lambda name: "/synthetic/docker" if name == "docker" else None)
        self.patch(deploy.subprocess, "run", self.docker)
        self.patch(deploy.socket, "socket", self.bind)

    def role_config(self, role):
        value = copy.deepcopy(self.config)
        value["role"] = role
        if role == "frontend":
            value["APP_BACKEND_URL"] = "https://10.20.30.41:443"
        elif role == "local-node":
            value.update(self.node)
        return value

    def model_config(self):
        cache = self.tmp / "model-cache"
        blobs = cache / "models/blobs"
        manifests = cache / "models/manifests/registry.ollama.ai/library/qwen2.5"
        blobs.mkdir(parents=True, exist_ok=True)
        manifests.mkdir(parents=True, exist_ok=True)
        descriptors = []
        for content, media in ((b"synthetic-config", "application/vnd.docker.container.image.v1+json"),
                               (b"synthetic-model", "application/vnd.ollama.image.model")):
            digest = hashlib.sha256(content).hexdigest()
            (blobs / ("sha256-" + digest)).write_bytes(content)
            descriptors.append({"digest": "sha256:" + digest, "size": len(content), "mediaType": media})
        (manifests / "0.5b").write_text(json.dumps({"schemaVersion": 2, "config": descriptors[0],
                                                  "layers": descriptors[1:]}))
        value = copy.deepcopy(self.config)
        value["model"] = {"enabled": True, "name": deploy.MODEL_NAME, "path": str(cache)}
        return value

    def execute(self, config=None, mode="check", memory=500):
        return deploy.execute(self.config if config is None else config, mode, self.tmp, available=lambda: memory)

    def assert_no_up(self):
        self.assertFalse(any("up" in call[0] for call in self.docker.calls))

    def test_deploy_python36_syntax_and_compile(self):
        path = ROOT / "scripts/deploy_portable.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path), feature_version=(3, 6))
        compile(tree, str(path), "exec", dont_inherit=True)
        # feature_version checks grammar, not availability of future features.
        future_features = {alias.name for node in tree.body
                           if isinstance(node, ast.ImportFrom) and node.module == "__future__"
                           for alias in node.names}
        self.assertNotIn("annotations", future_features)

    def test_strict_json_rejects_duplicates_and_nonfinite(self):
        for payload in ('{"role":"backend","role":"frontend"}', '{"model":{"enabled":true,"enabled":false}}',
                        '{"x":NaN}', '{"x":Infinity}', '{"x":-Infinity}', '{"x":[1e999]}',
                        '{"x":{"nested":-1e999}}', '{bad}', b'\xff', '{"x":' + '1' * 10000 + '}'):
            with self.subTest(payload=str(payload)[:60]), self.assertRaises(deploy.DeployError):
                deploy.strict_json(payload)

    def test_strict_json_finite(self):
        self.assertEqual(deploy.strict_json('{"a":[1,1.5,true,null]}'), {"a": [1, 1.5, True, None]})

    def test_default_plan_no_subprocess_socket_or_write(self):
        before = set(self.tmp.iterdir())
        result = deploy.execute(self.config)
        self.assertEqual(result["mode"], "plan")
        self.assertEqual(result["services"], ["frontend", "backend"])
        self.assertEqual(result["listen_host"], "127.0.0.1")
        self.assertEqual(result["host_port"], 18121)
        self.assertEqual(result["QA_ORIGIN"], self.config["APP_ORIGIN"])
        self.assertNotIn("compose_file", result)
        self.assertEqual(set(self.tmp.iterdir()), before)
        self.assert_acceptance_false(result)
        self.assertIn("NOT an egress firewall", result["notice"])
        self.assertIn("not Windows real-machine acceptance", result["notice"])

    def assert_acceptance_false(self, result):
        for field in ("deployment_accepted", "host_listener_verified", "private_tls_verified", "qa_verified"):
            self.assertIs(result[field], False)

    def test_config_fields_fail_closed(self):
        cases = [("role", "all"), ("role", []), ("source", "relative"), ("data", str(self.tmp / "missing")),
                 ("APP_ORIGIN", ""), ("APP_ORIGIN", "http://127.0.0.1:18121/"),
                 ("APP_ORIGIN", "https://user:secret@example.test"), ("APP_ORIGIN", "https://x.test?"),
                 ("APP_ORIGIN", "http://localhost:08000"), ("APP_ORIGIN", "https://X.test"),
                 ("APP_ORIGIN", "https://é.test"), ("host", "0.0.0.0"), ("host", "::"), ("host", "::1"),
                 ("host", "10.1.1.1"), ("host", "localhost"), ("host_port", True), ("host_port", 0),
                 ("host_port", 65536), ("host_port", "18121"), ("command", "sh -c owned"),
                 ("image", "untrusted:latest"), ("env", {"API_KEY": "secret"}),
                 ("APP_BACKEND_URL", "http://backend:8000")]
        for field, value in cases:
            config = copy.deepcopy(self.config)
            config[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(deploy.DeployError):
                deploy.validate_config(config)

    def test_source_data_overlap(self):
        for data in (self.source, self.source / "nested"):
            data.mkdir(exist_ok=True)
            config = {**self.config, "data": str(data)}
            with self.subTest(data=data), self.assertRaisesRegex(deploy.DeployError, "overlap"):
                deploy.validate_config(config)

    def test_role_topology_mounts_commands_origins_and_caps(self):
        for role in sorted(deploy.ROLES):
            with self.subTest(role=role):
                config = self.role_config(role)
                document = deploy.make_compose(config)
                self.assertEqual(set(document["services"]), {"frontend", "backend"} if role == "standalone" else {role})
                self.assertEqual(document["networks"]["portable"], {"driver": "bridge", "internal": False})
                for name, service in document["services"].items():
                    self.assertEqual(service["image"], deploy.APP_IMAGE)
                    self.assertEqual(service["pull_policy"], "never")
                    self.assertNotIn("build", service)
                    self.assertNotIn("network_mode", service)
                    self.assertEqual(service["command"], ["uvicorn", deploy.ENTRYPOINTS[name], "--workers", "1",
                                                         "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers"])
                    self.assertTrue(service["read_only"])
                    self.assertEqual(service["user"], deploy.service_identity(config, name))
                    expected = [deploy.bind(config["source"], "/app", True)]
                    if name == "backend":
                        expected.append(deploy.bind(config["data"], "/data", False))
                    self.assertEqual(service["volumes"], expected)
                    env = service["environment"]
                    self.assertEqual(env["APP_ORIGIN"], env["QA_ORIGIN"])
                    self.assertEqual(env["QA_ORIGIN"], config["APP_ORIGIN"])
                    self.assertEqual(env["QA_OLLAMA_URL"], "")
                    self.assertEqual(env["EXTERNAL_MODEL_BASE_URL"], "")
                    self.assertEqual(env["APP_PAYMENT_MODE"], "disabled")
                    self.assertEqual(env["LOCAL_MODEL_BACKEND"], "disabled")
                    if name == "backend":
                        self.assertEqual(env["APP_DB_PATH"], "/data/application.sqlite3")
                    else:
                        self.assertNotIn("APP_DB_PATH", env)
                    self.assertEqual(service["mem_limit"], "128m" if name == "backend" and role != "standalone" else "64m")
                    self.assertEqual(service["memswap_limit"], service["mem_limit"])
                    for port in service.get("ports", []):
                        self.assertEqual(port, {"target": 8000, "published": "18121", "host_ip": "127.0.0.1", "protocol": "tcp"})
                        self.assertTrue(any(not document["networks"][net]["internal"] for net in service["networks"]))
                if role == "standalone":
                    self.assertEqual(document["services"]["frontend"]["environment"]["APP_BACKEND_URL"], "http://backend:8000")
                    self.assertNotIn("ports", document["services"]["backend"])
                if role == "local-node":
                    service = document["services"][role]
                    self.assertEqual(service["secrets"], ["node_key"])
                    self.assertEqual(service["environment"]["NODE_ALLOW_RED"], "false")
                    self.assertEqual(service["environment"]["NODE_PUBLIC_CAPABILITY"], "classic-search")

    def test_literal_paths_not_shell_or_compose_interpolation(self):
        strange = self.tmp / "source $(not-a-command); with spaces"
        self.source.rename(strange)
        config = {**self.config, "source": str(strange)}
        doc = deploy.make_compose(config)
        self.assertEqual(doc["services"]["backend"]["volumes"][0]["source"], str(strange).replace("$", "$$"))
        self.assertEqual(doc["services"]["backend"]["command"][0], "uvicorn")

    def test_frontend_rejects_unroutable_loopback_service_dns_public(self):
        cases = [None, "http://backend:8000", "https://backend:443", "http://127.0.0.1:18131",
                 "https://127.0.0.1:443", "https://[::1]:443", "https://backend.private.test:443",
                 "https://8.8.8.8:443", "https://169.254.169.254:443", "http://10.1.2.3:8000",
                 "https://10.1.2.3:8080", "https://user:password@10.1.2.3:443", "https://10.1.2.3:443/path"]
        for value in cases:
            config = {**self.config, "role": "frontend", "APP_BACKEND_URL": value}
            with self.subTest(url=value), self.assertRaises(deploy.DeployError):
                deploy.validate_config(config)

    def test_endpoint_defaults_match_unchanged_runtime_private_url(self):
        runtime = source_function("application_hub/nodes.py", "private_url",
                                  {"ipaddress": ipaddress, "re": re, "urlsplit": urlsplit})
        with self.assertRaises(ValueError):
            runtime("https://10.1.2.3")
        for value in ("https://10.1.2.3", "https://172.20.1.2", "https://192.168.1.2", "https://10.1.2.3:8000"):
            expected = value + ":443" if urlsplit(value).port is None else value
            frontend = {**self.config, "role": "frontend", "APP_BACKEND_URL": value}
            node = {**self.role_config("local-node"), "node_endpoint": value}
            backend = {**self.role_config("backend"), "nodes": [{**self.node, "node_endpoint": value}]}
            normalized = [deploy.validate_config(frontend)["APP_BACKEND_URL"],
                          deploy.validate_config(node)["node_endpoint"],
                          deploy.validate_config(backend)["nodes"][0]["node_endpoint"]]
            self.assertEqual(normalized, [expected] * 3)
            self.assertEqual(runtime(expected), expected)
            self.assertEqual(deploy.validate_config(deploy.validate_config(frontend)), deploy.validate_config(frontend))
        self.assertEqual(runtime("http://backend:8000"), "http://backend:8000")

    def test_all_consumers_refuse_loopback_and_unprovisioned_ipv6(self):
        for endpoint, transport in (("http://127.0.0.1:18131", "tunnel"),
                                    ("https://127.0.0.1:443", "tls"),
                                    ("https://[fd00::1]:443", "tls")):
            local = {**self.role_config("local-node"), "node_endpoint": endpoint, "node_transport": transport}
            frontend = {**self.role_config("frontend"), "APP_BACKEND_URL": endpoint}
            backend = {**self.role_config("backend"), "nodes": [
                {**self.node, "node_endpoint": endpoint, "node_transport": transport}]}
            for config in (local, frontend, backend):
                with self.subTest(role=config["role"], endpoint=endpoint), self.assertRaisesRegex(deploy.DeployError, "refused and unaccepted"):
                    deploy.validate_config(config)

    def test_node_transport_private_tls_only(self):
        cases = [("https://10.1.2.3:443", "tls", True), ("https://[fd00::1]:443", "tls", False),
                 ("https://10.1.2.3", "tls", True), ("http://127.0.0.1:18131", "tunnel", False),
                 ("https://127.0.0.1:443", "tls", False), ("https://[::1]:443", "tls", False),
                 ("https://8.8.8.8:443", "tls", False), ("https://node.public.test:443", "tls", False),
                 ("http://10.1.2.3:8000", "tunnel", False), ("http://127.0.0.1:18131", "tls", False),
                 ("https://0.0.0.0:443", "tls", False), ("https://169.254.169.254:443", "tls", False)]
        for endpoint, transport, accepted in cases:
            config = {**self.role_config("local-node"), "node_endpoint": endpoint, "node_transport": transport}
            with self.subTest(endpoint=endpoint, transport=transport):
                if accepted:
                    self.assertEqual(urlsplit(deploy.validate_config(config)["node_endpoint"]).port, 443)
                else:
                    with self.assertRaises(deploy.DeployError):
                        deploy.validate_config(config)

    def test_invalid_node_capability_identity_key_transport(self):
        for change in ({"public_capability": "red"}, {"public_capability": ["classic-search", "red"]},
                       {"NODE_ID": "Invalid_ID"}, {"node_key_file": str(self.tmp / "missing")},
                       {"node_transport": "none"}):
            with self.subTest(change=change), self.assertRaises(deploy.DeployError):
                deploy.validate_config({**self.role_config("local-node"), **change})

    def test_secret_not_under_source_or_data(self):
        for directory in (self.source, self.data):
            target = directory / "secret"
            target.write_text("synthetic-" * 8)
            target.chmod(0o600)
            with self.subTest(directory=directory), self.assertRaisesRegex(deploy.DeployError, "outside"):
                deploy.validate_config({**self.role_config("local-node"), "node_key_file": str(target)})

    def test_secret_size_and_owner_only(self):
        config = self.role_config("local-node")
        self.key.write_text("short")
        with self.assertRaisesRegex(deploy.DeployError, "32 bytes"):
            deploy.validate_config(config)
        self.key.write_text("synthetic-" * 8)
        self.key.chmod(0o644)
        with self.assertRaisesRegex(deploy.DeployError, "owner-only"):
            deploy.validate_config(config)

    def test_no_secret_database_or_env_contents_read(self):
        content = self.key.read_text()
        database = self.data / "application.sqlite3"
        database.write_text("synthetic opaque database")
        dotenv = self.source / ".env"
        dotenv.write_text("synthetic opaque credentials")
        blocked = {self.key, database, dotenv}
        old_bytes, old_text = Path.read_bytes, Path.read_text

        def guarded_bytes(path, *args, **kwargs):
            self.assertNotIn(path, blocked)
            return old_bytes(path, *args, **kwargs)

        def guarded_text(path, *args, **kwargs):
            self.assertNotIn(path, blocked)
            return old_text(path, *args, **kwargs)

        with patch.object(Path, "read_bytes", guarded_bytes), patch.object(Path, "read_text", guarded_text):
            local = self.role_config("local-node")
            backend = {**self.role_config("backend"), "nodes": [self.node]}
            serialized = json.dumps(deploy.make_compose(local)) + json.dumps(deploy.execute(local))
            deploy.check_source(deploy.validate_config(backend))
        self.assertNotIn(content, serialized)
        self.assertNotIn("X-Node-Key", serialized)

    def test_registry_fixed_public_only_and_duplicates(self):
        config = {**self.role_config("backend"), "nodes": [self.node]}
        doc = deploy.make_compose(config)
        self.assertEqual(json.loads(doc["services"]["backend"]["environment"]["APP_NODE_ENDPOINTS"]),
                         {"test-node": {"url": self.node["node_endpoint"], "secret_file": "/run/secrets/node_key_test-node"}})
        self.assertEqual(doc["secrets"]["node_key_test-node"]["file"], str(self.key))
        self.assertEqual(doc["services"]["backend"]["secrets"], ["node_key_test-node"])
        config["nodes"].append(self.node)
        with self.assertRaisesRegex(deploy.DeployError, "duplicate"):
            deploy.validate_config(config)

    def test_registry_shapes_and_roles_rejected(self):
        cases = [{**self.config, "nodes": [self.node]}, {**self.role_config("frontend"), "nodes": []},
                 {**self.role_config("backend"), "nodes": [self.node] * 3},
                 {**self.role_config("backend"), "nodes": [{"NODE_ID": "test-node"}]},
                 {**self.role_config("backend"), "nodes": "not-list"}, {**self.config, **self.node}]
        for config in cases:
            with self.subTest(config=config), self.assertRaises(deploy.DeployError):
                deploy.validate_config(config)

    def test_registry_secret_uid_must_match_backend(self):
        original = Path.stat

        def different(path, *args, **kwargs):
            metadata = original(path, *args, **kwargs)
            if path == self.key:
                return SimpleNamespace(st_uid=metadata.st_uid + 1, st_gid=metadata.st_gid,
                                       st_size=metadata.st_size, st_mode=metadata.st_mode)
            return metadata

        with patch.object(Path, "stat", different), self.assertRaisesRegex(deploy.DeployError, "same pre-provisioned owner"):
            deploy.validate_config({**self.role_config("backend"), "nodes": [self.node]})

    def test_secret_unreadable_by_container_refused(self):
        self.key.chmod(0o000)
        with self.assertRaisesRegex(deploy.DeployError, "container UID/GID"):
            deploy.execute(self.role_config("local-node"))

    def test_source_unreadable_even_if_host_owner_can_read(self):
        original = deploy.service_identity

        def different(config, name):
            return "55555:55555" if name == "backend" else original(config, name)

        self.source.chmod(0o700)
        with patch.object(deploy, "service_identity", different), self.assertRaisesRegex(deploy.DeployError, "source.*UID/GID"):
            deploy.execute(self.config)

    def test_source_file_unreadable_and_required_page_missing(self):
        source = self.source / "common/qa.py"
        source.chmod(0o000)
        with self.assertRaisesRegex(deploy.DeployError, "source.*UID/GID"):
            deploy.execute(self.config)
        source.chmod(0o644)
        page = self.source / "public_qa/index.html"
        page.rename(page.with_name("not-the-page"))
        with self.assertRaisesRegex(deploy.DeployError, "source or page"):
            deploy.execute(self.config)

    def test_source_symlinks_rejected(self):
        target = self.tmp / "outside.py"
        target.write_text("synthetic")
        (self.source / "escape.py").symlink_to(target)
        with self.assertRaisesRegex(deploy.DeployError, "symlink"):
            deploy.execute(self.config)

    def test_database_sidecar_guards(self):
        target = self.tmp / "opaque-file"
        target.write_text("synthetic")
        for suffix in ("", "-wal", "-shm", "-journal"):
            with self.subTest(suffix=suffix):
                directory = tempfile.mkdtemp(dir=self.tmp)
                db = Path(directory) / ("application.sqlite3" + suffix)
                db.symlink_to(target)
                with self.assertRaisesRegex(deploy.DeployError, "symlinks"):
                    deploy.execute({**self.config, "data": directory})
        db = self.data / "application.sqlite3"
        db.write_text("synthetic")
        db.chmod(0o400)
        with self.assertRaisesRegex(deploy.DeployError, "persistent database"):
            deploy.execute(self.config)

    def test_data_directory_container_write_required(self):
        self.data.chmod(0o500)
        with self.assertRaisesRegex(deploy.DeployError, "persistent data"):
            deploy.execute(self.config)
        self.data.chmod(0o700)

    def test_require_access_uses_primary_gid_not_host_supplementary(self):
        file = self.tmp / "permissions"
        file.write_text("synthetic")
        file.chmod(0o640)
        metadata = file.stat()
        deploy.require_access(file, f"{metadata.st_uid}:{metadata.st_gid + 1}", 6, "test")
        deploy.require_access(file, f"{metadata.st_uid + 1}:{metadata.st_gid}", 4, "test")
        with self.assertRaises(deploy.DeployError):
            deploy.require_access(file, f"{metadata.st_uid + 1}:{metadata.st_gid + 1}", 4, "test")

    def test_root_runtime_owner_refused_without_chown(self):
        original = Path.stat

        def root(path, *args, **kwargs):
            metadata = original(path, *args, **kwargs)
            return SimpleNamespace(st_uid=0, st_gid=0) if path == self.data else metadata

        with patch.object(Path, "stat", root), self.assertRaisesRegex(deploy.DeployError, "non-root"):
            deploy.service_identity(self.config, "backend")

    def test_model_external_or_implicit_settings_refused(self):
        for model in ({"enabled": "true"}, {"enabled": False, "url": "https://external.test"},
                      {"enabled": True, "name": "external", "path": str(self.tmp)},
                      {"enabled": True, "name": deploy.MODEL_NAME, "url": "http://8.8.8.8"}):
            with self.subTest(model=model), self.assertRaises(deploy.DeployError):
                deploy.validate_config({**self.config, "model": model})

    def test_frontend_and_node_cannot_enable_model(self):
        model = self.model_config()["model"]
        for role in ("frontend", "local-node"):
            with self.subTest(role=role), self.assertRaisesRegex(deploy.DeployError, "model is allowed"):
                deploy.validate_config({**self.role_config(role), "model": model})

    def test_model_cache_cannot_overlap_mounts(self):
        for path in (self.source, self.data):
            with self.subTest(path=path), self.assertRaisesRegex(deploy.DeployError, "overlap"):
                deploy.validate_config({**self.config, "model": {"enabled": True, "name": deploy.MODEL_NAME, "path": str(path)}})

    def test_model_separate_internal_network_in_all_roles(self):
        model = self.model_config()
        for role, nodes in (("standalone", None), ("backend", []), ("backend", [self.node])):
            with self.subTest(role=role, nodes=nodes):
                config = {**model, "role": role}
                if nodes is not None:
                    config["nodes"] = nodes
                doc = deploy.make_compose(config)
                runner = doc["services"]["ollama"]
                self.assertEqual(runner["image"], deploy.MODEL_IMAGE)
                self.assertEqual(runner["pull_policy"], "never")
                self.assertEqual(runner["networks"], {"model_private": {"aliases": ["qa_ollama"]}})
                self.assertTrue(doc["networks"]["model_private"]["internal"])
                self.assertFalse(doc["networks"]["portable"]["internal"])
                self.assertIn("model_private", doc["services"]["backend"]["networks"])
                if role == "standalone":
                    self.assertNotIn("model_private", doc["services"]["frontend"]["networks"])
                self.assertNotIn("ports", runner)
                self.assertNotIn("build", runner)
                self.assertEqual(runner["mem_limit"], "512m")
                self.assertEqual(runner["memswap_limit"], "768m")
                self.assertEqual(runner["cpus"], .75)
                self.assertEqual(doc["volumes"][deploy.MODEL_VOLUME], {"external": True, "name": deploy.MODEL_VOLUME})
                self.assertEqual(runner["volumes"], [{"type": "volume", "source": deploy.MODEL_VOLUME,
                                                      "target": "/models", "read_only": True, "volume": {"nocopy": True}}])
                self.assertEqual(deploy.budget(doc), {"ram_mib": 640, "ram_plus_swap_mib": 896, "required_available_mib": 700})

    def test_real_app_qa_wiring_and_disabled_endpoint(self):
        model = self.model_config()
        environment = deploy.make_compose(model)["services"]["backend"]["environment"]
        runner_tree = ast.parse((ROOT / "public_qa/model.py").read_text())
        allowed = next(node.value for node in runner_tree.body if isinstance(node, ast.Assign) and
                       any(isinstance(target, ast.Name) and target.id == "_ALLOWED_ENDPOINTS" for target in node.targets))
        endpoint = source_function("public_qa/model.py", "endpoint", {
            "os": SimpleNamespace(environ=environment), "JsonRejected": ValueError,
            "_ALLOWED_ENDPOINTS": ast.literal_eval(allowed)})
        qa_tree = ast.parse((ROOT / "common/qa.py").read_text())
        model_name = next(node.value for node in qa_tree.body if isinstance(node, ast.Assign) and
                          any(isinstance(target, ast.Name) and target.id == "MODEL_NAME" for target in node.targets))
        self.assertEqual(deploy.MODEL_NAME, ast.literal_eval(model_name))
        self.assertEqual(endpoint(), "http://qa_ollama:11434/api/generate")
        self.assertEqual(environment["LOCAL_MODEL_BACKEND"], "disabled")
        self.assertNotIn("APP_MODEL_ENABLED", environment)
        self.assertNotIn("OLLAMA_URL", environment)
        environment["QA_OLLAMA_URL"] = ""
        with self.assertRaises(ValueError):
            endpoint()
        tree = ast.parse((ROOT / "application_hub/main.py").read_text())
        self.assertTrue(any(isinstance(node, ast.ImportFrom) and node.module == "public_qa.main" and
                            any(alias.name == "ask" for alias in node.names) for node in tree.body))
        question = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "question")
        self.assertTrue(any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "ask"
                            for node in ast.walk(question)))

    def test_origins_match_actual_browser_parser(self):
        parser = source_function("public_qa/main.py", "_parse_origin", {"ipaddress": ipaddress, "re": re, "urlsplit": urlsplit})
        for value in ("http://127.0.0.1:18121", "https://10.1.2.3", "https://[fd00::1]:443", "https://portal.private.test:443"):
            config = {**self.config, "APP_ORIGIN": value}
            for service in deploy.make_compose(config)["services"].values():
                environment = service["environment"]
                self.assertEqual(parser(environment["APP_ORIGIN"])[0], parser(environment["QA_ORIGIN"])[0])
        boundary = (ROOT / "application_hub/boundary.py").read_text()
        frontend = (ROOT / "portable_frontend/main.py").read_text()
        self.assertIn('headers.get(b"host") != [self.host.encode()]', boundary)
        self.assertIn('headers = {"Host": host, "Origin": origin', frontend)
        self.assertIn('private_url(os.environ.get("APP_BACKEND_URL", ""))', frontend)

    def test_cache_missing_manifest_and_corrupt_blob(self):
        model = self.model_config()
        cache = Path(model["model"]["path"])
        deploy.verify_model_cache(deploy.validate_config(model))
        manifest = cache / "models/manifests/registry.ollama.ai/library/qwen2.5/0.5b"
        original = manifest.read_bytes()
        manifest.rename(manifest.with_name("not-the-model"))
        with self.assertRaisesRegex(deploy.DeployError, "manifest"):
            deploy.verify_model_cache(deploy.validate_config(model))
        manifest.write_bytes(original)
        blob = next((cache / "models/blobs").iterdir())
        blob.write_bytes(b"x" * blob.stat().st_size)
        with self.assertRaisesRegex(deploy.DeployError, "digest"):
            deploy.verify_model_cache(deploy.validate_config(model))

    def test_cache_bad_manifest_shape_size_and_escape(self):
        model = self.model_config()
        cache = Path(model["model"]["path"])
        manifest = cache / "models/manifests/registry.ollama.ai/library/qwen2.5/0.5b"
        for value in ({"schemaVersion": 1}, {"schemaVersion": 2, "config": {}, "layers": []},
                      {"schemaVersion": 2, "config": {}, "layers": [{"digest": "url:bad"}]}):
            manifest.write_text(json.dumps(value))
            with self.subTest(value=value), self.assertRaises(deploy.DeployError):
                deploy.verify_model_cache(deploy.validate_config(model))
        manifest.write_bytes(b" " * (deploy.MIB + 1))
        with self.assertRaisesRegex(deploy.DeployError, "oversized"):
            deploy.verify_model_cache(deploy.validate_config(model))
        manifest.rename(manifest.with_name("oversized-saved"))
        manifest.symlink_to(self.key)
        with self.assertRaisesRegex(deploy.DeployError, "manifest"):
            deploy.verify_model_cache(deploy.validate_config(model))

    def test_cache_container_readability(self):
        model = self.model_config()
        (Path(model["model"]["path"]) / "models/blobs").chmod(0o700)
        original = deploy.service_identity
        with patch.object(deploy, "service_identity", lambda config, name: "55555:55555" if name == "ollama" else original(config, name)):
            with self.assertRaisesRegex(deploy.DeployError, "model cache.*UID/GID"):
                deploy.execute(model)

    def test_budget_ram_and_swap_ceiling(self):
        for field in ("mem_limit", "memswap_limit"):
            doc = deploy.make_compose(self.config)
            doc["services"]["backend"][field] = "900m"
            with self.subTest(field=field), self.assertRaisesRegex(deploy.DeployError, "RAM plus swap"):
                deploy.budget(doc)

    def test_exclusive_snapshot_never_overwrites(self):
        path = self.tmp / "compose.portable.fixed.json"
        path.write_text("existing user configuration")
        with patch.object(deploy.uuid, "uuid4", lambda: SimpleNamespace(hex="fixed")):
            with self.assertRaisesRegex(deploy.DeployError, "exclusive"):
                deploy.write_compose(deploy.make_compose(self.config), self.tmp)
        self.assertEqual(path.read_text(), "existing user configuration")

    def test_check_read_only_local_cli_env_isolation(self):
        self.mock_cli()
        os.environ["COMPOSE_FILE"] = "/untrusted/compose.yml"
        os.environ["COMPOSE_PROFILES"] = "download"
        result = self.execute(memory=350)
        self.assertEqual(result["preflight"], "passed")
        self.assertIs(result["containers_started"], False)
        self.assert_acceptance_false(result)
        path = Path(result["compose_file"])
        self.assertEqual(deploy.strict_json(path.read_bytes()), deploy.make_compose(self.config))
        for argv, kwargs in self.docker.calls:
            self.assertNotIn("COMPOSE_FILE", kwargs["env"])
            self.assertNotIn("COMPOSE_PROFILES", kwargs["env"])
            self.assertFalse(any(word in argv for word in ("pull", "build", "up", "run", "down", "prune", "rm", "ssh")))
        self.assertEqual(os.environ["COMPOSE_FILE"], "/untrusted/compose.yml")
        command = self.docker.calls[-1][0]
        self.assertEqual(command[-2:], ["config", "--quiet"])
        self.assertEqual(command[command.index("--env-file") + 1], os.devnull)
        self.assertEqual(self.bind.calls, [("127.0.0.1", 18121)])
        self.assertEqual(self.bind.closed, 1)
        self.assertFalse(path.stat().st_mode & 0o077)

    def test_apply_only_whitelisted_services_after_preflight(self):
        self.mock_cli()
        for role in sorted(deploy.ROLES):
            with self.subTest(role=role):
                result = self.execute(self.role_config(role), "apply")
                command = self.docker.calls[-1][0]
                self.assertEqual(command[command.index("--no-deps") + 1:], ["frontend", "backend"] if role == "standalone" else [role])
                self.assertEqual(command[command.index("up"):command.index("--no-deps") + 1],
                                 ["up", "-d", "--no-build", "--pull", "never", "--no-deps"])
                self.assertEqual(result["preflight"], "passed")
                self.assertIs(result["containers_started"], True)
                self.assert_acceptance_false(result)
                self.assertIn("business readiness", result["notice"])
                self.assertFalse(any(word in command for word in ("down", "--remove-orphans", "--build", "--force-recreate")))
        self.assertEqual(len(self.bind.calls), 8)

    def test_host_port_conflict_refuses_before_cli(self):
        self.mock_cli()
        self.bind.fail = True
        with self.assertRaisesRegex(deploy.DeployError, "port is occupied") as caught:
            self.execute(mode="apply")
        self.assertNotIn("synthetic", str(caught.exception))
        self.assertEqual(self.docker.calls, [])
        self.assertEqual(self.bind.closed, 1)

    def test_docker_reserved_port_without_host_listener(self):
        self.mock_cli()
        self.docker.ids = ["a" * 12]
        for host in ("127.0.0.1", "0.0.0.0", "::", ""):
            self.docker.port_records = [json.dumps({"8000/tcp": [{"HostIp": host, "HostPort": "18121"}]})]
            with self.subTest(host=host), self.assertRaisesRegex(deploy.DeployError, "Docker has already reserved"):
                self.execute(mode="apply")
        self.assert_no_up()

    def test_other_address_or_udp_is_not_conflicting(self):
        self.mock_cli()
        self.docker.ids = ["a" * 12]
        self.docker.port_records = [json.dumps({"8000/tcp": [{"HostIp": "10.1.2.3", "HostPort": "18121"}],
                                                "8000/udp": [{"HostIp": "0.0.0.0", "HostPort": "18121"}]})]
        self.assertEqual(self.execute()["preflight"], "passed")

    def test_invalid_container_inventory_fails_closed(self):
        self.mock_cli()
        cases = [("not-an-id", []), ("a" * 12, []), ("a" * 12, ['[]']),
                 ("a" * 12, ['{"bad":[]}']), ("a" * 12, ['{"8000/tcp":[{}]}']),
                 ("a" * 12, ['{"8000/tcp":[{"HostIp":"localhost","HostPort":"18121"}]}']),
                 ("a" * 12, ['{"8000/tcp":[{"HostIp":"127.0.0.1","HostPort":"x"}]}'])]
        for identifier, records in cases:
            self.docker.ids = [identifier]
            self.docker.port_records = records
            with self.subTest(records=records), self.assertRaises(deploy.DeployError):
                self.execute(mode="apply")
        self.assert_no_up()

    def test_conflict_rechecked_after_compose_config(self):
        self.mock_cli()
        self.docker.on_config = lambda _argv: setattr(self.bind, "fail", True)
        with self.assertRaisesRegex(deploy.DeployError, "port is occupied"):
            self.execute(mode="apply")
        self.assertEqual(len(self.bind.calls), 2)
        self.assert_no_up()

    def test_engine_conflict_rechecked_before_up(self):
        self.mock_cli()

        def occupy(_argv):
            self.docker.ids = ["a" * 12]
            self.docker.port_records = ['{"8000/tcp":[{"HostIp":"127.0.0.1","HostPort":"18121"}]}']

        self.docker.on_config = occupy
        with self.assertRaisesRegex(deploy.DeployError, "Docker has already reserved"):
            self.execute(mode="apply")
        self.assert_no_up()

    def test_failed_prerequisites_never_apply_or_leak(self):
        self.mock_cli()
        for fault in ("memory", "compose", "image", "windows-engine", "vm-memory", "cli-error", "image-os"):
            self.docker = DockerMock()
            self.patch(deploy.subprocess, "run", self.docker)
            if fault == "compose":
                self.docker.version = "2.23.9"
            elif fault == "image":
                self.docker.fail = lambda argv: "image" in argv
            elif fault == "windows-engine":
                self.docker.ostype = "windows"
            elif fault == "vm-memory":
                self.docker.engine_memory = 100 * deploy.MIB
            elif fault == "cli-error":
                self.docker.fail = lambda argv: "config" in argv
            elif fault == "image-os":
                self.docker.image_os = "windows"
            with self.subTest(fault=fault), self.assertRaises(deploy.DeployError) as caught:
                self.execute(mode="apply", memory=349 if fault == "memory" else 500)
            self.assertNotIn("credential-do-not-print", str(caught.exception))
            self.assert_no_up()

    def test_missing_docker_refuses(self):
        self.patch(deploy.socket, "socket", self.bind)
        self.patch(deploy.shutil, "which", lambda _name: None)
        with self.assertRaisesRegex(deploy.DeployError, "not installed"):
            self.execute(mode="apply")

    def test_remote_context_rejected_before_daemon_contact(self):
        self.mock_cli()
        for endpoint in ("ssh://private-host", "tcp://127.0.0.1:2375", "tcp://10.1.2.3:2376"):
            self.docker.calls.clear()
            self.docker.endpoint = endpoint
            with self.subTest(endpoint=endpoint), self.assertRaisesRegex(deploy.DeployError, "remote Docker"):
                self.execute()
            self.assertFalse(any("info" in call[0] for call in self.docker.calls))

    def test_docker_host_tls_overrides_rejected_without_commands(self):
        self.mock_cli()
        for field in ("DOCKER_HOST", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"):
            with patch.dict(os.environ, {field: "untrusted"}), self.subTest(field=field):
                with self.assertRaisesRegex(deploy.DeployError, "DOCKER_HOST/TLS"):
                    self.execute()
        self.assertEqual(self.docker.calls, [])

    def test_architecture_native_match_only(self):
        self.mock_cli()
        for engine, image, variant, accepted in (("x86_64", "amd64", "", True), ("amd64", "amd64", "", True),
                                                 ("aarch64", "arm64", "v8", True), ("arm64", "arm64", "", True),
                                                 ("aarch64", "amd64", "", False), ("x86_64", "arm64", "", False),
                                                 ("armv7l", "arm", "v7", False), ("arm64", "arm64", "v9", False)):
            self.docker.arch, self.docker.image_arch, self.docker.variant = engine, image, variant
            with self.subTest(engine=engine, image=image, variant=variant):
                if accepted:
                    self.assertEqual(self.execute()["preflight"], "passed")
                else:
                    with self.assertRaisesRegex(deploy.DeployError, "architecture"):
                        self.execute(mode="apply")
        self.assert_no_up()

    def test_image_entrypoint_must_match_actual_command_wiring(self):
        self.mock_cli()
        self.docker.entrypoints[deploy.APP_IMAGE] = ["sh", "-c"]
        with self.assertRaisesRegex(deploy.DeployError, "entrypoint"):
            self.execute(mode="apply")
        self.docker.entrypoints[deploy.APP_IMAGE] = None
        model = self.model_config()
        self.docker.entrypoints[deploy.MODEL_IMAGE] = None
        with self.assertRaisesRegex(deploy.DeployError, "entrypoint"):
            self.execute(model, "apply", memory=700)
        self.assert_no_up()

    def test_windows_acl_uid_mapping_refused_and_preview_unaccepted(self):
        validated = deploy.validate_config(self.config)
        with patch.object(deploy.os, "name", "nt"):
            self.assertEqual(deploy.service_identity(validated, "backend"), "10001:10001")
            with self.assertRaisesRegex(deploy.DeployError, "ACL and VM UID"):
                deploy.check_source(validated)

    def test_windows_named_pipe_local_context_is_not_real_acceptance(self):
        self.mock_cli()
        self.docker.endpoint = "npipe:////./pipe/docker_engine"
        self.assertEqual(deploy.local_docker({}), ["/synthetic/docker", "--context", "default"])
        self.assert_no_up()

    def test_model_requires_verified_preexisting_volume(self):
        self.mock_cli()
        model = self.model_config()
        self.docker.volume["mountpoint"] = model["model"]["path"]
        result = self.execute(model, memory=700)
        self.assertEqual(result["preflight"], "passed")
        self.assert_acceptance_false(result)
        self.assertTrue(any("volume" in call[0] for call in self.docker.calls))
        self.assert_no_up()

    def test_model_preflight_cannot_download_or_launch_on_failure(self):
        self.mock_cli()
        model = self.model_config()
        for fault in ("volume-mismatch", "network-volume", "model-memory", "missing-model-image", "driver"):
            self.docker.volume = {"driver": "local", "mountpoint": model["model"]["path"], "device": "", "type": "", "options": ""}
            self.docker.fail = None
            if fault == "volume-mismatch":
                self.docker.volume["mountpoint"] = str(self.tmp)
            elif fault == "network-volume":
                self.docker.volume.update(device=model["model"]["path"], type="nfs", options="rw")
            elif fault == "missing-model-image":
                self.docker.fail = lambda argv: deploy.MODEL_IMAGE in argv
            elif fault == "driver":
                self.docker.volume["driver"] = "remote"
            with self.subTest(fault=fault), self.assertRaises(deploy.DeployError):
                self.execute(model, "apply", memory=699 if fault == "model-memory" else 700)
        self.assert_no_up()
        self.assertFalse(any("run" in call[0] or "pull" in call[0] for call in self.docker.calls))

    def test_model_apply_only_three_services(self):
        self.mock_cli()
        model = self.model_config()
        self.docker.volume.update(mountpoint="/vm/inaccessible", device=model["model"]["path"], type="none", options="bind,ro")
        result = self.execute(model, "apply", memory=700)
        command = self.docker.calls[-1][0]
        self.assertEqual(command[command.index("--no-deps") + 1:], ["frontend", "backend", "ollama"])
        self.assert_acceptance_false(result)

    def test_model_cache_tamper_after_check_refuses_apply(self):
        self.mock_cli()
        model = self.model_config()
        self.docker.volume["mountpoint"] = model["model"]["path"]
        blob = next((Path(model["model"]["path"]) / "models/blobs").iterdir())
        self.docker.on_config = lambda _argv: blob.write_bytes(b"x" * blob.stat().st_size)
        with self.assertRaisesRegex(deploy.DeployError, "digest"):
            self.execute(model, "apply", memory=700)
        self.assert_no_up()

    def test_context_named_compose_preserved_in_final_port_inspection(self):
        self.mock_cli()
        os.environ["DOCKER_CONTEXT"] = "compose"
        self.execute(mode="apply")
        inspections = [argv for argv, _kwargs in self.docker.calls if "container" in argv]
        self.assertEqual(len(inspections), 2)
        for argv in inspections:
            self.assertEqual(argv[:3], ["/synthetic/docker", "--context", "compose"])

    def test_snapshot_tamper_rejected_in_check_and_apply(self):
        self.mock_cli()

        def tamper(argv):
            path = Path(argv[argv.index("-f") + 1])
            doc = json.loads(path.read_text())
            doc["services"]["backend"]["command"] = ["sh", "-c", "not-permitted"]
            path.write_text(json.dumps(doc))

        self.docker.on_config = tamper
        for mode in ("check", "apply"):
            with self.subTest(mode=mode), self.assertRaisesRegex(deploy.DeployError, "snapshot differs"):
                self.execute(mode=mode)
        self.assert_no_up()

    def test_document_tamper_before_preflight(self):
        doc = deploy.make_compose(self.config)
        doc["services"]["frontend"]["ports"][0]["host_ip"] = "0.0.0.0"
        path = deploy.write_compose(doc, self.tmp)
        with self.assertRaisesRegex(deploy.DeployError, "differs from the allowlisted"):
            deploy.preflight(self.config, doc, path, available=lambda: 500)

    def test_source_permission_tamper_after_check(self):
        self.mock_cli()
        self.docker.on_config = lambda _argv: (self.source / "common/qa.py").chmod(0o000)
        with self.assertRaisesRegex(deploy.DeployError, "source.*UID/GID"):
            self.execute(mode="apply")
        self.assert_no_up()

    def test_main_default_plan_and_safe_errors(self):
        path = self.tmp / "deployment.json"
        path.write_text(json.dumps(self.config))
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            self.assertEqual(deploy.main(["--config", str(path)]), 0)
        self.assertEqual(json.loads(stdout.getvalue())["mode"], "plan")
        path.write_text('{"role":"credential-do-not-print","role":"frontend"}')
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            self.assertEqual(deploy.main(["--config", str(path)]), 2)
        self.assertNotIn("credential-do-not-print", stdout.getvalue() + stderr.getvalue())

    def test_unsupported_mode_refused_without_snapshot(self):
        before = set(self.tmp.iterdir())
        with self.assertRaisesRegex(deploy.DeployError, "unsupported execution"):
            self.execute(mode="shell")
        self.assertEqual(set(self.tmp.iterdir()), before)

    def test_linux_memory_measurement_mocked_fail_closed(self):
        with patch.object(deploy.platform, "system", lambda: "Linux"):
            with patch.object(Path, "read_text", lambda _path, **_kwargs: "MemAvailable: 716800 kB\n"):
                self.assertEqual(deploy.host_available_mib(), 700)
            with patch.object(Path, "read_text", lambda _path, **_kwargs: "MemTotal: 716800 kB\n"):
                with self.assertRaisesRegex(deploy.DeployError, "cannot be measured"):
                    deploy.host_available_mib()

    def test_macos_memory_uses_mocked_vm_stat(self):
        calls = []

        def vm_stat(argv, **kwargs):
            calls.append(argv)
            self.assertIs(kwargs["shell"], False)
            output = ('Mach Virtual Memory Statistics: (page size of 4096 bytes)\n'
                      'Pages free: 102400.\nPages inactive: 51200.\nPages speculative: 25600.\n')
            return subprocess.CompletedProcess(argv, 0, output, "")

        with patch.object(deploy.platform, "system", lambda: "Darwin"), patch.object(deploy.subprocess, "run", vm_stat):
            self.assertEqual(deploy.host_available_mib(), 700)
        self.assertEqual(calls, [["/usr/bin/vm_stat"]])

    def test_reference_template_is_unaccepted_unpublished_not_role_launcher(self):
        template = (ROOT / "compose.portable.yml").read_text()
        self.assertIn("NON-DEPLOYABLE reference", template)
        self.assertIn("image: daobrain-app:dev", template)
        self.assertIn("pull_policy: never", template)
        self.assertNotIn("build:", template)
        self.assertNotIn("ports:", template)
        self.assertNotIn("network_mode:", template)
        for role in deploy.ROLES:
            self.assertNotRegex(template, r"profiles: \[[^\]]*\b" + re.escape(role) + r"\b")
        self.assertIn("profiles: [reference-only]", template)
        self.assertIn("daobrain_ollama: {external: true", template)
        self.assertIn("create_host_path: false", template)
        self.assertIn("mem_limit: 512m", template)
        self.assertIn("memswap_limit: 768m", template)
        self.assertIn('QA_OLLAMA_URL: ""', template)
        self.assertNotIn("  local-node:", template)
        self.assertNotIn("secrets:", template)
        totals = re.findall(r"^    memswap_limit: (\d+)m$", template, re.M)
        self.assertLessEqual(sum(map(int, totals)), 900)


if __name__ == "__main__":
    unittest.main(verbosity=2)
