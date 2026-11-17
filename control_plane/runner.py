"""Fixed host Docker operations. Never accepts a command, image or mount from HTTP.

Enrollment is a host-provisioned owner-only manifest. inspect_hash pins the
normalized immutable inspection envelope (see inspection_binding). No enrollment,
unknown readiness, resource uncertainty and missing trusted fixtures fail closed.
"""
import hashlib
import os
import re
import selectors
import stat
import subprocess
import tempfile
import time
import uuid
from collections import namedtuple

from .protocol import HEX64, private_file, strict_json
from .registry import ControlError, EDITABLE_DIRS, PROTECTED_DIRS, REGISTRY, RUNTIMES, canonical, digest
from .sandbox_test_entry import TEST_FIXTURE_PATHS
from .build_artifacts import ArtifactError, verify_artifacts

MIB = 1024 * 1024
OUTPUT_LIMIT = 16384
INSPECT_LIMIT = 262144
MAX_SOURCE_BYTES = 8 * MIB
MAX_SOURCE_FILES = 512
OWNER_LABEL = "com.daobrain.control.owner"
SOURCE_LABEL = "com.daobrain.control.source_hash"
CONFIG_LABEL = "com.daobrain.control.config_hash"
CommandResult = namedtuple("CommandResult", "returncode output bytes_seen truncated timed_out")


def bounded_command(argv, timeout=30, output_limit=OUTPUT_LIMIT):
    """Drain incrementally, never communicate()/capture_output() an unbounded pipe."""
    if (not isinstance(argv, list) or not argv or not all(isinstance(v, str) for v in argv)
            or not 0 < timeout <= 180 or not 0 < output_limit <= INSPECT_LIMIT):
        raise ControlError("invalid fixed command", 503)
    proc = None
    selector = selectors.DefaultSelector()
    output = bytearray()
    seen = 0
    timed_out = False
    deadline = time.monotonic() + timeout
    try:
        proc = subprocess.Popen(argv, shell=False, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                close_fds=True, env={"PATH": "/usr/bin:/bin", "LANG": "C"})
        os.set_blocking(proc.stdout.fileno(), False)
        selector.register(proc.stdout, selectors.EVENT_READ)
        while selector.get_map() or proc.poll() is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                proc.kill()
                break
            for key, event in selector.select(min(remaining, 0.1)):
                block = os.read(key.fd, 8192)
                if not block:
                    selector.unregister(key.fileobj)
                    continue
                seen += len(block)
                if len(output) < output_limit:
                    output.extend(block[:output_limit - len(output)])
        proc.wait(timeout=2)
        return CommandResult(proc.returncode, bytes(output), seen,
                             seen > output_limit, timed_out)
    except (OSError, subprocess.SubprocessError):
        raise ControlError("fixed host command unavailable", 503)
    finally:
        selector.close()
        if proc is not None:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=2)
            if proc.stdout:
                proc.stdout.close()


def mem_available_mib():
    try:
        with open("/proc/meminfo", "r") as stream:
            for line in stream:
                if line.startswith("MemAvailable:"):
                    value = line.split()
                    if len(value) == 3 and value[2] == "kB":
                        return int(value[1]) // 1024
    except (OSError, ValueError):
        pass
    raise ControlError("host MemAvailable unavailable", 503)


def inspection_binding(value):
    """Trusted enrollment must hash this envelope, not a volatile whole inspect."""
    networks = value.get("NetworkSettings", {}).get("Networks", {})
    return {"Id": value.get("Id"), "Image": value.get("Image"),
            "Config": value.get("Config"), "HostConfig": value.get("HostConfig"),
            "Mounts": value.get("Mounts"),
            "Networks": {name: {"NetworkID": data.get("NetworkID"),
                                "Aliases": data.get("Aliases")}
                         for name, data in networks.items()}}


def safe_relative(path):
    if (not isinstance(path, str) or len(path) > 240
            or not re.fullmatch(r"[A-Za-z0-9_./-]+", path)
            or path.startswith("/") or "\\" in path):
        raise ControlError("unsafe artifact path")
    parts = path.split("/")
    if any(not part or part.startswith(".") or part in ("..", ".") for part in parts):
        raise ControlError("unsafe artifact path")
    return parts


def safe_root(path):
    if (not isinstance(path, str) or not os.path.isabs(path)
            or os.path.realpath(path) != path or any(c in path for c in ",\n\r\x00")):
        raise ControlError("unsafe immutable source root", 503)
    try:
        if not stat.S_ISDIR(os.lstat(path).st_mode):
            raise ControlError("source root is not a directory", 503)
    except OSError:
        raise ControlError("source root unavailable", 503)
    return path


def source_file(root, relative, limit):
    """Walk with directory fds/O_NOFOLLOW, rejecting symlinks and hardlinks."""
    parts = safe_relative(relative)
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                              dir_fd=directory)
            os.close(directory)
            directory = next_fd
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                     dir_fd=directory)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or info.st_size > limit):
                raise ControlError("invalid source artifact", 422)
            data = stream.read(limit + 1)
            after = os.fstat(stream.fileno())
            if len(data) > limit or (info.st_size, info.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise ControlError("source changed or oversized", 422)
            return data
    except OSError:
        raise ControlError("unsafe source artifact", 422)
    finally:
        os.close(directory)


class Runner:
    def __init__(self, runtime_file=None, source_root=None, command=None,
                 available=None, staging_root=None):
        self.runtime_file = runtime_file if runtime_file is not None else os.environ.get("CONTROL_RUNTIME_FILE", "")
        self.source_root = source_root if source_root is not None else os.environ.get("CONTROL_SOURCE_ROOT", "")
        self.command = command or bounded_command
        self.available = available or mem_available_mib
        self.staging_root = staging_root
        # Host fixed executable. No shell, remote Docker context or inherited environment.
        self.docker = "/usr/bin/docker"

    def _docker_command(self, argv, timeout, output_limit):
        # Never honor a user's saved remote Docker context, even with a clean env.
        fixed = [self.docker, "--host=unix:///var/run/docker.sock"] + argv[1:]
        return self.command(fixed, timeout=timeout, output_limit=output_limit)

    def enrollment(self):
        try:
            manifest = strict_json(private_file(self.runtime_file, MAX_SOURCE_BYTES), MAX_SOURCE_BYTES)
            required = {"project", "source_root", "app_image", "runtimes", "budget_mib"}
            if (not required.issubset(manifest) or set(manifest) - required - {"test_entry_hash", "test_fixtures", "build_entry_hash"}
                    or ("build_entry_hash" in manifest and
                        (not isinstance(manifest["build_entry_hash"], str) or
                         not re.fullmatch(r"[0-9a-f]{64}", manifest["build_entry_hash"])))
                    or not re.fullmatch(r"daobrain-r6-[a-z0-9-]{1,48}", manifest["project"])
                    or manifest["budget_mib"] != 900 or type(manifest["budget_mib"]) is not int
                    or not re.fullmatch(r"sha256:[0-9a-f]{64}", manifest["app_image"])
                    or not isinstance(manifest["runtimes"], dict)
                    or not manifest["runtimes"] or set(manifest["runtimes"]) - RUNTIMES):
                raise ValueError("invalid enrollment")
            if safe_root(manifest["source_root"]) != safe_root(self.source_root):
                raise ValueError("source root mismatch")
            ids = set()
            for runtime, record in manifest["runtimes"].items():
                if (not isinstance(record, dict) or not
                        {"container_id", "source_hash", "config_hash", "inspect_hash"}.issubset(record)
                        or set(record) - {"container_id", "source_hash", "config_hash", "inspect_hash", "image"}
                        or not all(isinstance(record[k], str) and HEX64.fullmatch(record[k])
                                   for k in ("container_id", "source_hash", "config_hash", "inspect_hash"))
                        or record["container_id"] in ids
                        or not re.fullmatch(r"sha256:[0-9a-f]{64}", record.get("image", manifest["app_image"]))):
                    raise ValueError("invalid runtime")
                ids.add(record["container_id"])
            return manifest
        except (ControlError, ValueError, TypeError, KeyError):
            raise ControlError("not_enrolled", 503)

    def inspect(self, container_id):
        if not isinstance(container_id, str) or not HEX64.fullmatch(container_id):
            raise ControlError("fixed full container id required", 503)
        result = self._docker_command([self.docker, "inspect", "--type", "container", container_id],
                              timeout=10, output_limit=INSPECT_LIMIT)
        if result.timed_out or result.truncated or result.returncode != 0:
            raise ControlError("runtime inspection unavailable", 503)
        try:
            values = strict_json(b'{"items":' + result.output + b'}', INSPECT_LIMIT + 16)["items"]
            if not isinstance(values, list) or len(values) != 1 or not isinstance(values[0], dict):
                raise ValueError("invalid inspect")
            return values[0]
        except (ControlError, ValueError, KeyError):
            raise ControlError("runtime inspection invalid", 503)

    def verify_runtime(self, manifest, runtime):
        if runtime not in RUNTIMES or runtime not in manifest["runtimes"]:
            raise ControlError("not_enrolled", 503)
        record = manifest["runtimes"][runtime]
        value = self.inspect(record["container_id"])
        try:
            labels = value["Config"]["Labels"] or {}
            host = value["HostConfig"]
            if (value["Id"] != record["container_id"]
                    or value["Image"] != record.get("image", manifest["app_image"])
                    or labels.get("com.docker.compose.project") != manifest["project"]
                    or labels.get("com.docker.compose.service") != runtime
                    or labels.get(OWNER_LABEL) != "daobrain-r6"
                    or labels.get(SOURCE_LABEL) != record["source_hash"]
                    or labels.get(CONFIG_LABEL) != record["config_hash"]
                    or digest(inspection_binding(value)) != record["inspect_hash"]):
                raise ValueError("identity binding")
            user = value["Config"].get("User", "")
            uid = user.split(":")[0]
            if (not uid.isdigit() or int(uid) == 0 or host.get("Privileged") is not False
                    or host.get("ReadonlyRootfs") is not True
                    or {str(v).upper() for v in host.get("CapDrop", [])} != {"ALL"}
                    or host.get("CapAdd") or host.get("Devices") or host.get("DeviceRequests")
                    or not any(v in ("no-new-privileges", "no-new-privileges:true")
                               for v in host.get("SecurityOpt", []))
                    or host.get("PidMode", "") or host.get("IpcMode") not in ("private", "none", "")
                    or host.get("NetworkMode") in ("host", "container")
                    or host.get("NetworkMode", "").startswith("container:")
                    or host.get("RestartPolicy", {}).get("Name") not in ("no", "")
                    or any(type(host.get(k)) is not int for k in ("PidsLimit", "NanoCpus", "Memory", "MemorySwap"))
                    or not 0 < host.get("PidsLimit", 0) <= 128
                    or not 0 < host.get("NanoCpus", 0) <= 1000000000
                    or not 0 < host.get("Memory", 0) <= 900 * MIB
                    or not host["Memory"] <= host.get("MemorySwap", 0) <= 900 * MIB):
                raise ValueError("unsafe resource envelope")
            for mount in value.get("Mounts", []):
                src, dst = mount.get("Source", ""), mount.get("Destination", "")
                if ("docker.sock" in src or "docker.sock" in dst or dst in ("/", "/proc", "/sys", "/dev")
                        or src in ("/", "/proc", "/sys", "/dev", "/var/run", "/run")):
                    raise ValueError("unsafe mount")
            return value
        except (ValueError, TypeError, KeyError, AttributeError):
            raise ControlError("runtime enrollment verification failed", 409)

    def preflight(self, manifest, extra_mib=0, starting=None):
        inspections = {runtime: self.verify_runtime(manifest, runtime)
                       for runtime in manifest["runtimes"]}
        potential = sum(v["HostConfig"]["MemorySwap"] for v in inspections.values())
        if potential + extra_mib * MIB > manifest["budget_mib"] * MIB:
            raise ControlError("aggregate potential memory exceeds 900 MiB", 409)
        available = self.available()
        # Reserve includes every enrolled stopped service, not just the target.
        stopped = sum(v["HostConfig"]["Memory"] for v in inspections.values()
                      if not v.get("State", {}).get("Running")) // MIB
        if type(available) is not int or available < 384 + extra_mib + stopped:
            raise ControlError("host memory reserve below 384 MiB", 409)
        return inspections

    @staticmethod
    def observed(value):
        state = value.get("State", {})
        health = state.get("Health", {}).get("Status", "unconfigured")
        labels = value.get("Config", {}).get("Labels", {}) or {}
        running = state.get("Running") is True
        return {"running": running, "state": state.get("Status", "unknown"),
                "health": health, "readiness": "ready" if running and health == "healthy" else "unknown",
                "running_source_hash": labels.get(SOURCE_LABEL) if running else None,
                "running_config_hash": labels.get(CONFIG_LABEL) if running else None,
                "captured_at": int(time.time())}

    def status(self):
        try:
            manifest = self.enrollment()
        except ControlError:
            return {"enrolled": False, "readiness": "not_enrolled", "runtimes": {},
                    "test": {"ready": False, "fixtures_declared": False,
                             "reason": "not_enrolled"},
                    "build": {"ready": False, "reason": "isolated builder not provisioned"},
                    "captured_at": int(time.time())}
        states = {}
        for runtime in manifest["runtimes"]:
            try:
                states[runtime] = self.observed(self.verify_runtime(manifest, runtime))
            except ControlError:
                states[runtime] = {"readiness": "unknown", "running": None,
                                   "running_source_hash": None, "running_config_hash": None,
                                   "captured_at": int(time.time())}
        fixtures = bool(manifest.get("test_entry_hash") and manifest.get("test_fixtures"))
        return {"enrolled": True, "runtimes": states, "captured_at": int(time.time()),
                "test": {"ready": False, "fixtures_declared": fixtures,
                         "reason": "image and resource preflight required" if fixtures else "trusted test fixture not provisioned"},
                "build": self._build_readiness(manifest)}

    def action(self, runtime, action, expected_config_hash, authorize=None):
        if action not in ("start", "stop", "restart"):
            raise ControlError("unknown fixed action")
        manifest = self.enrollment()
        if runtime not in manifest["runtimes"]:
            raise ControlError("not_enrolled", 503)
        # expected_config_hash was checked against active CONTROL POLICY by the
        # controller; application config has an independent inspected label hash.
        if not isinstance(expected_config_hash, str) or not HEX64.fullmatch(expected_config_hash):
            raise ControlError("invalid expected policy hash")
        values = self.preflight(manifest, starting=runtime)
        target = values[runtime]["Id"]
        argv = [self.docker, action]
        if action in ("stop", "restart"):
            argv += ["--time", "10"]
        argv.append(target)
        self.verify_runtime(manifest, runtime)
        if authorize:
            authorize()
        try:
            result = self._docker_command(argv, timeout=30, output_limit=OUTPUT_LIMIT)
            value = self.verify_runtime(manifest, runtime)
            observed = self.observed(value)
            if result.timed_out:
                raise ControlError("runtime action timed out; inspect status", 504)
            if result.returncode != 0:
                raise ControlError("runtime action failed; inspect status", 409)
            observed["action"] = action
            return observed
        except ControlError as exc:
            # Daemon operation may outlive a failed/timed-out CLI invocation.
            exc.execution_uncertain = True
            raise

    def sanitize_snapshot(self, snapshot):
        """Shared full-inventory validation; never compile/import/execute source."""
        if not isinstance(snapshot, dict):
            raise ControlError("snapshot file manifest required", 422)
        root = safe_root(snapshot.get("root"))
        files = snapshot.get("files")
        if isinstance(files, dict):
            files = [dict(metadata, path=path) for path, metadata in files.items()]
        if not isinstance(files, list) or not files or len(files) > MAX_SOURCE_FILES:
            raise ControlError("snapshot file manifest required", 422)
        inventory = {}
        for entry in files:
            if isinstance(entry, str):
                entry = {"path": entry, "editable": True}
            if (not isinstance(entry, dict) or not isinstance(entry.get("path"), str)
                    or entry["path"] in inventory):
                raise ControlError("snapshot file manifest required", 422)
            inventory[entry["path"]] = entry
        source = {}
        directories = set()
        size = 0
        baselines = {"common", "application_hub", "portable_frontend"}
        for path in sorted(inventory):
            parts, metadata = safe_relative(path), inventory[path]
            baseline = parts[0] in baselines and metadata.get("editable") is False
            if (len(parts) < 2 or (not baseline and
                    (parts[0] not in EDITABLE_DIRS or parts[0] in PROTECTED_DIRS))
                    or not path.endswith((".py", ".html"))
                    or any(p.lower().startswith(("env", "secret", "credential", "test_"))
                           or p.lower() in ("tests", "data", "logs", "db", "node_modules")
                           for p in parts)):
                raise ControlError("artifact outside source import allowlist", 422)
            data = source_file(root, path, MAX_SOURCE_BYTES - size)
            size += len(data)
            sha = hashlib.sha256(data).hexdigest()
            if "sha256" in metadata and metadata["sha256"] != sha:
                raise ControlError("snapshot artifact hash changed", 409)
            source[path] = data
            if metadata.get("editable") is True:
                directories.add(parts[0])
        actual_hash = digest([{"path": p, "sha256": hashlib.sha256(data).hexdigest()}
                              for p, data in sorted(source.items())])
        if actual_hash != snapshot.get("source_hash"):
            raise ControlError("snapshot source hash changed", 409)
        if snapshot.get("component") in REGISTRY:
            directories = {REGISTRY[snapshot["component"]]["directory"]}
        if not directories:
            raise ControlError("editable component test target unavailable", 422)
        return source, directories, size

    def _stage(self, snapshot, manifest):
        """Make private retained staging; preserve the complete fixed test recipe."""
        source, directories, size = self.sanitize_snapshot(snapshot)
        fixtures = manifest.get("test_fixtures", {})
        trusted = {}
        entry_hash = manifest.get("test_entry_hash", "")
        if not isinstance(entry_hash, str) or not HEX64.fullmatch(entry_hash):
            raise ControlError("trusted test fixture not provisioned", 503)
        # Every selected component must pin the complete fixed recipe. Validate
        # all declarations before reading shared files; never overwrite conflicts.
        expected_fixtures = {}
        for directory in sorted(directories):
            selected = fixtures.get(directory) if isinstance(fixtures, dict) else None
            if not isinstance(selected, dict) or set(selected) != set(TEST_FIXTURE_PATHS):
                raise ControlError("trusted focused test fixture not provisioned", 503)
            for path in TEST_FIXTURE_PATHS:
                expected = selected[path]
                if not isinstance(expected, str) or not HEX64.fullmatch(expected):
                    raise ControlError("unsafe trusted fixture", 503)
                if path in expected_fixtures and expected_fixtures[path] != expected:
                    raise ControlError("conflicting trusted fixture hashes", 409)
                expected_fixtures[path] = expected
        entry = "control_plane/sandbox_test_entry.py"
        trusted[entry] = source_file(self.source_root, entry, MAX_SOURCE_BYTES - size)
        size += len(trusted[entry])
        if hashlib.sha256(trusted[entry]).hexdigest() != entry_hash:
            raise ControlError("trusted harness hash mismatch", 409)
        # Read, verify and copy each unique trusted file exactly once, only from
        # the host-provisioned source root, never from the editable workspace.
        for path in TEST_FIXTURE_PATHS:
            data = source_file(self.source_root, path, MAX_SOURCE_BYTES - size)
            size += len(data)
            if hashlib.sha256(data).hexdigest() != expected_fixtures[path]:
                raise ControlError("trusted fixture hash mismatch", 409)
            trusted[path] = data
        if len(source) + len(trusted) > MAX_SOURCE_FILES or size > MAX_SOURCE_BYTES:
            raise ControlError("snapshot volume limit", 422)
        # Parent must be host-provisioned outside CONTROL_SOURCE_ROOT and production data.
        parent = safe_root(self.staging_root) if self.staging_root else os.path.dirname(self.runtime_file)
        parent = safe_root(parent)
        info = os.stat(parent)
        if info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ControlError("private staging directory required", 503)
        stage = tempfile.mkdtemp(prefix="sandbox-", dir=parent)
        os.chmod(stage, 0o755)  # Only staged allowlisted files, readable by sandbox UID.
        for subtree, mapping in (("source", source), ("trusted", trusted)):
            for path, data in mapping.items():
                target = os.path.join(stage, subtree, path)
                os.makedirs(os.path.dirname(target), mode=0o755, exist_ok=True)
                with open(target, "xb") as stream:
                    stream.write(data)
                os.chmod(target, 0o444)
        return os.path.join(stage, "source"), os.path.join(stage, "trusted")

    def _sandbox_image(self, manifest):
        """Static host-review attestation shared by test and SOURCE ZIP admission."""
        # An image is arbitrary code too: it must be provisioned by trusted host review.
        inspected = self._docker_command([self.docker, "image", "inspect", manifest["app_image"]],
                                 timeout=10, output_limit=INSPECT_LIMIT)
        if inspected.timed_out or inspected.truncated or inspected.returncode:
            raise ControlError("verified offline app image unavailable", 503)
        try:
            image = strict_json(b'{"items":' + inspected.output + b'}', INSPECT_LIMIT + 16)["items"]
            if not isinstance(image, list) or len(image) != 1 or image[0].get("Id") != manifest["app_image"]:
                raise ValueError("image identity")
            config = image[0].get("Config", {})
            # This is a trusted host-review attestation, not an automatic image scan.
            # Existing COPY-entire-repo development images are NOT eligible.
            if (config.get("Volumes") or config.get("Labels", {}).get("com.daobrain.control.sandbox") != "r6-source-only"
                    or config.get("OnBuild") or config.get("ExposedPorts")):
                raise ValueError("minimal sandbox image not reviewed")
            safe_env = {"PATH", "LANG", "HOME", "GPG_KEY", "PYTHON_VERSION", "PYTHON_SHA256",
                        "PYTHONUNBUFFERED", "PYTHONDONTWRITEBYTECODE", "PIP_NO_CACHE_DIR"}
            if any(not isinstance(v, str) or "=" not in v or v.split("=", 1)[0] not in safe_env
                   for v in config.get("Env", [])):
                raise ValueError("image inherits unapproved environment")
        except (ControlError, TypeError, ValueError, KeyError, AttributeError):
            raise ControlError("unsafe test image", 503)
        return config.get("Env", [])

    def prepare_test(self, snapshot):
        """Admission and staging only; does not execute tests or assert a pass."""
        manifest = self.enrollment()
        self.preflight(manifest, extra_mib=192)
        source, trusted = self._stage(snapshot, manifest)
        image_env = self._sandbox_image(manifest)
        return {"manifest": manifest, "source": source, "trusted": trusted,
                "image_env": image_env, "source_hash": snapshot["source_hash"]}

    def run_test(self, prepared, job_id, record_container, authorize=None):
        if not isinstance(job_id, str) or not re.fullmatch(r"[0-9a-f]{32}", job_id):
            raise ControlError("invalid internal job id")
        manifest = prepared["manifest"]
        # Re-read enrollment and resource constraints at execution, after freezing.
        if digest(self.enrollment()) != digest(manifest):
            raise ControlError("enrollment changed", 409)
        self.preflight(manifest, extra_mib=192)
        if authorize:
            authorize()
        argv = [self.docker, "create", "--pull", "never", "--name", "daobrain-r6-test-" + job_id,
                "--label", OWNER_LABEL + "=daobrain-r6", "--label", "com.daobrain.control.job=" + job_id,
                "--label", SOURCE_LABEL + "=" + prepared["source_hash"],
                "--network", "none", "--read-only", "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges", "--user", "10001:10001",
                "--pids-limit", "64", "--memory", "192m", "--memory-swap", "192m",
                "--cpus", "0.5", "--tmpfs", "/tmp:rw,noexec,nosuid,size=32m",
                "--restart", "no", "--stop-timeout", "5", "--workdir", "/sandbox",
                "--log-driver", "none", "--entrypoint", "python",
                "--mount", "type=bind,src=" + prepared["source"] + ",dst=/sandbox,readonly",
                "--mount", "type=bind,src=" + prepared["trusted"] + ",dst=/trusted,readonly",
                manifest["app_image"], "-I", "/trusted/control_plane/sandbox_test_entry.py"]
        try:
            created = self._docker_command(argv, timeout=10, output_limit=OUTPUT_LIMIT)
            container_id = created.output.strip().decode("ascii", "replace")
            if created.timed_out or created.truncated or created.returncode or not HEX64.fullmatch(container_id):
                raise ControlError("test container creation uncertain", 503)
            record_container(container_id)
        except ControlError as exc:
            # A failed create may still have created a container. Keep the lease.
            exc.execution_uncertain = True
            raise
        try:
            value = self.inspect(container_id)
            self._verify_test(value, container_id, job_id, prepared)
            if authorize:
                authorize()
            result = self._docker_command([self.docker, "start", "--attach", container_id],
                                  timeout=180, output_limit=OUTPUT_LIMIT)
            if result.timed_out:
                self._stop_test(container_id, job_id, prepared)
            value = self.inspect(container_id)
            self._verify_test(value, container_id, job_id, prepared)
            state = value.get("State", {})
            if state.get("Running") or state.get("Status") not in ("exited", "dead"):
                raise ControlError("sandbox completion unverified", 503)
            code = state.get("ExitCode")
            if type(code) is not int:
                raise ControlError("sandbox exit code unavailable", 503)
            status = "timed_out" if result.timed_out else (
                "succeeded" if result.returncode == 0 and code == 0 and not state.get("OOMKilled") else "failed")
            # Do not persist or return arbitrary source stdout, inspect Env, or errors.
            return {"status": status, "exit_code": code, "oom_killed": state.get("OOMKilled") is True,
                    "output_bytes": result.bytes_seen, "output_truncated": result.truncated,
                    "output_retained": False, "metadata_limit": OUTPUT_LIMIT}
        except Exception as exc:
            try:
                self._stop_test(container_id, job_id, prepared)
            except ControlError as stop_error:
                stop_error.execution_uncertain = True
                raise
            if isinstance(exc, ControlError):
                exc.execution_uncertain = False  # Fresh inspect proved own sandbox stopped.
            raise
        # No docker rm, volume deletion, export, publish or source apply.

    def _verify_test(self, value, container_id, job_id, prepared):
        try:
            host, config = value["HostConfig"], value["Config"]
            labels = config.get("Labels", {}) or {}
            mounts = value.get("Mounts", [])
            actual = {(m.get("Source"), m.get("Destination"), m.get("RW")) for m in mounts}
            expected = {(prepared["source"], "/sandbox", False), (prepared["trusted"], "/trusted", False)}
            if (value["Id"] != container_id or value["Image"] != prepared["manifest"]["app_image"]
                    or labels.get(OWNER_LABEL) != "daobrain-r6"
                    or labels.get("com.daobrain.control.job") != job_id
                    or labels.get(SOURCE_LABEL) != prepared["source_hash"]
                    or host.get("NetworkMode") != "none" or host.get("ReadonlyRootfs") is not True
                    or host.get("Privileged") is not False or host.get("CapAdd") or host.get("Devices")
                    or host.get("DeviceRequests") or host.get("PidMode", "")
                    or host.get("IpcMode") not in ("private", "none", "")
                    or host.get("Memory") != 192 * MIB or host.get("MemorySwap") != 192 * MIB
                    or host.get("PidsLimit") != 64 or host.get("NanoCpus") != 500000000
                    or {str(v).upper() for v in host.get("CapDrop", [])} != {"ALL"}
                    or not any(v in ("no-new-privileges", "no-new-privileges:true") for v in host.get("SecurityOpt", []))
                    or host.get("RestartPolicy", {}).get("Name") != "no"
                    or host.get("LogConfig", {}).get("Type") != "none"
                    or host.get("Tmpfs") != {"/tmp": "rw,noexec,nosuid,size=32m"}
                    or config.get("User") != "10001:10001" or config.get("Entrypoint") != ["python"]
                    or config.get("Cmd") != ["-I", "/trusted/control_plane/sandbox_test_entry.py"]
                    or config.get("Env", []) != prepared.get("image_env", [])
                    or config.get("WorkingDir") != "/sandbox" or host.get("PortBindings")
                    or actual != expected or len(mounts) != 2
                    or any(m.get("Type") != "bind" for m in mounts)):
                raise ValueError("sandbox envelope mismatch")
        except (KeyError, ValueError, TypeError):
            raise ControlError("sandbox isolation verification failed", 409)

    def _stop_test(self, container_id, job_id, prepared):
        self._verify_test(self.inspect(container_id), container_id, job_id, prepared)
        result = self._docker_command([self.docker, "stop", "--time", "5", container_id],
                              timeout=10, output_limit=OUTPUT_LIMIT)
        if result.timed_out or result.returncode:
            raise ControlError("sandbox stop unverified; host recovery required", 503)
        value = self.inspect(container_id)
        self._verify_test(value, container_id, job_id, prepared)
        if value.get("State", {}).get("Running") is not False:
            raise ControlError("sandbox stop unverified; host recovery required", 503)

    def _build_entry(self, manifest):
        expected = manifest.get("build_entry_hash")
        if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise ControlError("isolated builder not provisioned", 503)
        data = source_file(self.source_root, "control_plane/sandbox_build_entry.py", MAX_SOURCE_BYTES)
        if hashlib.sha256(data).hexdigest() != expected:
            raise ControlError("trusted build entry hash mismatch", 503)
        return data

    def _build_readiness(self, manifest):
        try:
            self._build_entry(manifest)
            self._sandbox_image(manifest)
            self.preflight(manifest, extra_mib=192)
            return {"ready": True, "reason": "admission verified; execution not attested"}
        except ControlError:
            return {"ready": False, "reason": "isolated builder admission unavailable"}

    def prepare_build(self, snapshot):
        """Prepare SOURCE ZIP only; no execution, image build or release approval."""
        manifest = self.enrollment()
        entry = self._build_entry(manifest)
        self.preflight(manifest, extra_mib=192)
        image_env = self._sandbox_image(manifest)
        source, directories, size = self.sanitize_snapshot(snapshot)
        if size + len(entry) + 1024 > MAX_SOURCE_BYTES or len(source) + 2 > MAX_SOURCE_FILES:
            raise ControlError("snapshot volume limit", 422)
        parent = safe_root(self.staging_root) if self.staging_root else safe_root(os.path.dirname(self.runtime_file))
        if (os.path.commonpath([parent, self.source_root]) == self.source_root
                or os.path.commonpath([parent, snapshot["root"]]) == snapshot["root"]):
            raise ControlError("private staging must be outside source roots", 503)
        info = os.stat(parent)
        if info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ControlError("private staging directory required", 503)
        # Parent and stage remain 0700. Docker binds subdirectories directly;
        # sandbox cannot traverse the host parent. Output alone is UID-writable.
        stage = tempfile.mkdtemp(prefix="build-", dir=parent)
        trusted_files = {"control_plane/sandbox_build_entry.py": entry}
        for subtree, mapping in (("source", source), ("trusted", trusted_files)):
            base = os.path.join(stage, subtree)
            os.mkdir(base, 0o755)
            os.chmod(base, 0o755)
            for relative, data in mapping.items():
                target = os.path.join(base, relative)
                os.makedirs(os.path.dirname(target), mode=0o755, exist_ok=True)
                directory = base
                for part in safe_relative(relative)[:-1]:
                    directory = os.path.join(directory, part)
                    os.chmod(directory, 0o755)  # Host umask 077 must not block sandbox reads.
                with open(target, "xb") as stream:
                    stream.write(data)
                os.chmod(target, 0o444)
        output = os.path.join(stage, "artifacts")
        os.mkdir(output, 0o755)
        os.chmod(output, 0o755)
        try:
            # Fixed sandbox identity. Tests MUST fake this; never chown the Mac.
            os.chown(output, 10001, 10001)
        except OSError:
            raise ControlError("sandbox output ownership unavailable", 503)
        info = os.stat(output)
        return {"manifest": manifest, "source": os.path.join(stage, "source"),
                "trusted": os.path.join(stage, "trusted"), "output": output,
                "output_identity": (info.st_dev, info.st_ino), "image_env": image_env,
                "source_hash": snapshot["source_hash"], "source_entries": source}

    def _build_recheck(self, prepared):
        manifest = prepared["manifest"]
        if digest(self.enrollment()) != digest(manifest):
            raise ControlError("enrollment changed", 409)
        self.preflight(manifest, extra_mib=192)
        if self._sandbox_image(manifest) != prepared["image_env"]:
            raise ControlError("sandbox image changed", 409)
        entry = self._build_entry(manifest)
        for path in (prepared["source"], prepared["trusted"], prepared["output"]):
            safe_root(path)
        if source_file(prepared["trusted"], "control_plane/sandbox_build_entry.py", MAX_SOURCE_BYTES) != entry:
            raise ControlError("trusted staged build entry changed", 409)
        # Compare every prepared byte and the same canonical full inventory hash.
        entries = prepared["source_entries"]
        inventory = []
        total = 0
        for path, data in sorted(entries.items()):
            actual = source_file(prepared["source"], path, MAX_SOURCE_BYTES - total)
            total += len(actual)
            if actual != data:
                raise ControlError("staged build source changed", 409)
            inventory.append({"path": path, "sha256": hashlib.sha256(actual).hexdigest()})
        if digest(inventory) != prepared["source_hash"]:
            raise ControlError("snapshot source hash changed", 409)
        info = os.stat(prepared["output"])
        if (info.st_dev, info.st_ino) != prepared["output_identity"] or os.listdir(prepared["output"]):
            raise ControlError("build output not empty or replaced", 409)

    def run_build(self, prepared, job_id, record_container, authorize=None):
        """Fixed isolated SOURCE ZIP execution; caller manages the global lease."""
        if not isinstance(job_id, str) or not re.fullmatch(r"[0-9a-f]{32}", job_id):
            raise ControlError("invalid internal job id")
        self._build_recheck(prepared)
        request = canonical({"job_id": job_id, "source_hash": prepared["source_hash"]})
        request_path = os.path.join(prepared["trusted"], "build-request.json")
        try:
            with open(request_path, "xb") as stream:
                stream.write(request)
            os.chmod(request_path, 0o444)
        except OSError:
            raise ControlError("fixed build request unavailable", 503)
        manifest = prepared["manifest"]
        argv = [self.docker, "create", "--pull", "never", "--name", "daobrain-r6-build-" + job_id,
                "--label", OWNER_LABEL + "=daobrain-r6", "--label", "com.daobrain.control.job=" + job_id,
                "--label", SOURCE_LABEL + "=" + prepared["source_hash"],
                "--network", "none", "--read-only", "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges", "--user", "10001:10001",
                "--pids-limit", "64", "--memory", "192m", "--memory-swap", "192m",
                "--cpus", "0.5", "--tmpfs", "/tmp:rw,noexec,nosuid,size=32m",
                "--restart", "no", "--stop-timeout", "5", "--workdir", "/sandbox",
                "--log-driver", "none", "--entrypoint", "python",
                "--mount", "type=bind,src=" + prepared["source"] + ",dst=/sandbox,readonly",
                "--mount", "type=bind,src=" + prepared["trusted"] + ",dst=/trusted,readonly",
                "--mount", "type=bind,src=" + prepared["output"] + ",dst=/artifacts",
                manifest["app_image"], "-I", "/trusted/control_plane/sandbox_build_entry.py"]
        if authorize:
            authorize()  # Immediately before create, outside uncertain execution.
        try:
            created = self._docker_command(argv, timeout=10, output_limit=OUTPUT_LIMIT)
            container_id = created.output.strip().decode("ascii")
            if (created.timed_out or created.truncated or created.returncode
                    or not re.fullmatch(r"[0-9a-f]{64}", container_id)):
                raise ControlError("build container creation uncertain", 503)
            record_container(container_id)
        except Exception:
            error = ControlError("build container creation uncertain", 503)
            error.execution_uncertain = True
            raise error
        try:
            self._verify_build(self.inspect(container_id), container_id, job_id, prepared)
            self._build_recheck(prepared)  # Full enrollment/image/source check before start.
            if source_file(prepared["trusted"], "build-request.json", 1024) != request:
                raise ControlError("fixed build request changed", 409)
            if authorize:
                authorize()  # Immediately before start, after all slow checks.
            result = self._docker_command([self.docker, "start", "--attach", container_id],
                                          timeout=180, output_limit=OUTPUT_LIMIT)
            if result.timed_out:
                self._stop_build(container_id, job_id, prepared)
            value = self.inspect(container_id)
            self._verify_build(value, container_id, job_id, prepared)
            code, oom = self._build_completion(value)
            if type(result.bytes_seen) is not int or result.bytes_seen < 0:
                raise ControlError("sandbox output accounting unavailable", 503)
            status = "timed_out" if result.timed_out else (
                "succeeded" if result.returncode == 0 and code == 0 and not oom else "failed")
            metadata = {}
            if status == "succeeded":
                try:
                    checked = verify_artifacts(prepared["output"], job_id, prepared["source_hash"],
                                               prepared["source_entries"], prepared["output_identity"])
                    # Only verified success carries artifacts. Failed/timed-out jobs
                    # must remain execution-only for ControllerState.finish().
                    # No paths, source bytes, producer errors or stdout cross this boundary.
                    metadata = {key: checked[key] for key in (
                        "artifact_sha256", "artifact_bytes", "artifact_files", "source_hash",
                        "kind", "image_built", "release_approved")}
                except ArtifactError:
                    status = "failed"
            metadata.update(status=status, exit_code=code, oom_killed=oom,
                            output_bytes=min(result.bytes_seen, 2147483647),
                            output_truncated=result.truncated is True or result.bytes_seen > OUTPUT_LIMIT,
                            output_retained=False, metadata_limit=OUTPUT_LIMIT)
            return metadata
        except Exception as exc:
            if getattr(exc, "execution_uncertain", False):
                error = ControlError("build sandbox recovery required", 503)
                error.execution_uncertain = True
                raise error
            try:
                self._stop_build(container_id, job_id, prepared)
            except Exception:
                error = ControlError("build sandbox recovery required", 503)
                error.execution_uncertain = True
                raise error
            error = ControlError("build sandbox execution rejected", getattr(exc, "status", 503))
            error.execution_uncertain = False  # Fresh, exact inspection proved stopped.
            raise error
        # Retain staging/container. Never rm/export/publish/apply or release here.

    def _verify_build(self, value, container_id, job_id, prepared):
        try:
            host, config = value["HostConfig"], value["Config"]
            labels = config.get("Labels", {}) or {}
            mounts = value.get("Mounts", [])
            actual = {(m.get("Source"), m.get("Destination"), m.get("RW")) for m in mounts}
            expected = {(prepared["source"], "/sandbox", False), (prepared["trusted"], "/trusted", False),
                        (prepared["output"], "/artifacts", True)}
            if (value["Id"] != container_id or value["Image"] != prepared["manifest"]["app_image"]
                    or value.get("Name") != "/daobrain-r6-build-" + job_id
                    or labels.get(OWNER_LABEL) != "daobrain-r6"
                    or labels.get("com.daobrain.control.job") != job_id
                    or labels.get(SOURCE_LABEL) != prepared["source_hash"]
                    or host.get("NetworkMode") != "none" or host.get("ReadonlyRootfs") is not True
                    or host.get("Privileged") is not False or host.get("CapAdd") or host.get("Devices")
                    or host.get("DeviceRequests") or host.get("PidMode", "")
                    or host.get("IpcMode") not in ("private", "none", "")
                    or any(type(host.get(k)) is not int for k in ("Memory", "MemorySwap", "PidsLimit", "NanoCpus"))
                    or host.get("Memory") != 192 * MIB or host.get("MemorySwap") != 192 * MIB
                    or host.get("PidsLimit") != 64 or host.get("NanoCpus") != 500000000
                    or {str(v).upper() for v in host.get("CapDrop", [])} != {"ALL"}
                    or host.get("SecurityOpt") not in (["no-new-privileges"], ["no-new-privileges:true"])
                    or host.get("RestartPolicy", {}).get("Name") != "no"
                    or host.get("LogConfig", {}).get("Type") != "none"
                    or host.get("Tmpfs") != {"/tmp": "rw,noexec,nosuid,size=32m"}
                    or host.get("PortBindings") or host.get("PublishAllPorts")
                    or host.get("AutoRemove", False) is not False
                    or host.get("Binds") or host.get("VolumesFrom") or host.get("Links")
                    or host.get("ExtraHosts") or host.get("UTSMode", "") or host.get("UsernsMode", "")
                    or type(config.get("StopTimeout")) is not int or config.get("StopTimeout") != 5
                    or config.get("User") != "10001:10001" or config.get("Entrypoint") != ["python"]
                    or config.get("Cmd") != ["-I", "/trusted/control_plane/sandbox_build_entry.py"]
                    or config.get("Env", []) != prepared["image_env"]
                    or config.get("WorkingDir") != "/sandbox" or config.get("Volumes") or config.get("ExposedPorts")
                    or value.get("NetworkSettings", {}).get("Ports")
                    or set(value.get("NetworkSettings", {}).get("Networks", {})) - {"none"}
                    or actual != expected or len(mounts) != 3
                    or any(m.get("Type") != "bind" or type(m.get("RW")) is not bool
                           or m.get("Propagation") != "rprivate" for m in mounts)):
                raise ValueError("build isolation mismatch")
        except (KeyError, ValueError, TypeError, AttributeError):
            raise ControlError("build sandbox isolation verification failed", 409)

    @staticmethod
    def _build_completion(value):
        state = value.get("State", {})
        code, oom = state.get("ExitCode"), state.get("OOMKilled")
        if (state.get("Running") is not False or state.get("Status") not in ("exited", "dead")
                or state.get("Paused") or state.get("Restarting")
                or type(code) is not int or not 0 <= code <= 255 or type(oom) is not bool):
            raise ControlError("build completion unverified", 503)
        return code, oom

    def _stop_build(self, container_id, job_id, prepared):
        value = self.inspect(container_id)
        self._verify_build(value, container_id, job_id, prepared)
        state = value.get("State", {})
        if (state.get("Running") is False and state.get("Status") == "created"
                and not state.get("Paused") and not state.get("Restarting")
                and type(state.get("ExitCode")) is int and state["ExitCode"] == 0
                and state.get("OOMKilled") is False):
            return  # Authorized create, denied start: proved never running.
        if state.get("Running") is False:
            self._build_completion(value)
            return
        if state.get("Running") is not True:
            raise ControlError("build stop unverified", 503)
        try:
            stopped = self._docker_command([self.docker, "stop", "--time", "5", container_id],
                                           timeout=10, output_limit=OUTPUT_LIMIT)
            if stopped.timed_out or stopped.truncated or stopped.returncode:
                raise ControlError("build stop unverified", 503)
            value = self.inspect(container_id)
            self._verify_build(value, container_id, job_id, prepared)
            self._build_completion(value)
        except Exception:
            error = ControlError("build stop unverified", 503)
            error.execution_uncertain = True
            raise error

    def build(self, snapshot=None):
        """Compatibility admission hook. Controller wiring is deliberately separate."""
        try:
            manifest = self.enrollment()
            self._build_entry(manifest)
        except ControlError:
            raise ControlError("isolated builder not provisioned", 503)
        if snapshot is None:
            raise ControlError("isolated builder not provisioned", 503)
        return self.prepare_build(snapshot)
