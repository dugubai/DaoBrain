"""Offline, resource-contained source artifact build, NOT a Docker image build.

Executed only in a no-network, nonroot container with /sandbox read-only and a
new empty /artifacts output directory. No Docker socket, secrets, business DB,
controller source, user build recipes, installs or source execution.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import zipfile

LIMIT = 8 * 1024 * 1024
MAX_FILES = 1024
ARTIFACT_LIMIT = 12 * 1024 * 1024
RESULT_LIMIT = 16384
REQUEST_LIMIT = 1024
BLOCKED = frozenset({"control_plane", "tests", "scripts", ".git", "__pycache__", "data", "models"})


def gather(root):
    # Pin each directory descriptor; an ancestor rename cannot redirect openat.
    # O_NONBLOCK also prevents a substituted FIFO from blocking before fstat.
    entries, counters = {}, {"bytes": 0, "nodes": 0}
    dir_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW

    def visit(parent_fd, parts):
        if len(parts) > 24:
            raise ValueError("build directory depth exceeded")
        names = os.listdir(parent_fd)
        if len(names) + counters["nodes"] > MAX_FILES * 2:
            raise ValueError("build traversal limit exceeded")
        for name in sorted(names):
            counters["nodes"] += 1
            if counters["nodes"] > MAX_FILES * 2 or name.startswith(".") or name in BLOCKED:
                raise ValueError("unapproved build node")
            info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            rel = "/".join(parts + [name])
            if stat.S_ISDIR(info.st_mode):
                child_fd = os.open(name, dir_flags, dir_fd=parent_fd)
                try:
                    actual = os.fstat(child_fd)
                    if (info.st_dev, info.st_ino) != (actual.st_dev, actual.st_ino):
                        raise ValueError("directory changed during build")
                    visit(child_fd, parts + [name])
                finally:
                    os.close(child_fd)
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("unsafe build file")
            if Path(name).suffix not in {".py", ".html"} or info.st_size > LIMIT:
                raise ValueError("unapproved build file")
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
            try:
                actual = os.fstat(fd)
                if not stat.S_ISREG(actual.st_mode) or actual.st_nlink != 1 or (info.st_dev, info.st_ino) != (actual.st_dev, actual.st_ino):
                    raise ValueError("source changed during build")
                with os.fdopen(fd, "rb", closefd=False) as stream:
                    data = stream.read(LIMIT + 1)
                after = os.fstat(fd)
                if (actual.st_size, actual.st_mtime_ns, actual.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
                    raise ValueError("source changed during build")
            finally:
                os.close(fd)
            counters["bytes"] += len(data)
            if counters["bytes"] > LIMIT or len(entries) >= MAX_FILES:
                raise ValueError("source artifact limit exceeded")
            if Path(name).suffix == ".py":
                compile(data, rel, "exec", dont_inherit=True)
            entries[rel] = data

    root_fd = os.open(str(root), dir_flags)
    try:
        visit(root_fd, [])
    finally:
        os.close(root_fd)
    if not entries:
        raise ValueError("empty source artifact refused")
    return entries


def build(root, output):
    output = Path(output)
    if output.is_symlink() or not output.is_dir() or any(output.iterdir()):
        raise ValueError("build output must be a new empty directory")
    return _build_entries(gather(root), output)


def _build_entries(entries, output):
    manifest = {name: hashlib.sha256(data).hexdigest() for name, data in sorted(entries.items())}
    metadata = {"kind": "offline-source-artifact", "source_manifest": manifest,
                "syntax_checked": True, "image_built": False, "release_approved": False}
    blob = json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    # Exact STORED ZIP size, including local/central headers and fixed EOCD.
    records = list(sorted(entries.items())) + [("BUILD-MANIFEST.json", blob)]
    zip_bytes = 22 + sum(76 + 2 * len(name.encode("utf-8")) + len(data)
                         for name, data in records)
    if zip_bytes > ARTIFACT_LIMIT:
        raise ValueError("build output limit exceeded")
    path = output / "source-artifact.zip"
    with path.open("xb") as stream:
        with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_STORED) as archive:
            for name, data in list(sorted(entries.items())) + [("BUILD-MANIFEST.json", blob)]:
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.external_attr = (stat.S_IFREG | 0o444) << 16
                archive.writestr(info, data)
    with zipfile.ZipFile(path) as archive:
        if archive.testzip() is not None or set(archive.namelist()) != set(entries) | {"BUILD-MANIFEST.json"}:
            raise ValueError("artifact verification failed")
        for name, data in entries.items():
            if archive.read(name) != data:
                raise ValueError("artifact byte verification failed")
    return {"artifact": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "bytes": path.stat().st_size, "files": len(entries), "kind": "offline-source-artifact",
            "syntax_checked": True, "image_built": False, "release_approved": False}


def _request():
    fd = os.open(str(Path("/trusted/build-request.json")),
                 os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or not 0 < info.st_size <= REQUEST_LIMIT):
            raise ValueError("invalid fixed build request")
        data = stream.read(REQUEST_LIMIT + 1)
    if not 0 < len(data) <= REQUEST_LIMIT:
        raise ValueError("invalid fixed build request")

    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError("duplicate build request key")
            value[key] = item
        return value

    def constant(value):
        raise ValueError("non-finite build request")

    value = json.loads(data.decode("utf-8"), object_pairs_hook=pairs,
                       parse_constant=constant)
    if (not isinstance(value, dict) or set(value) != {"job_id", "source_hash"}
            or not isinstance(value["job_id"], str)
            or not re.fullmatch(r"[0-9a-f]{32}", value["job_id"])
            or not isinstance(value["source_hash"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", value["source_hash"])):
        raise ValueError("invalid fixed build request")
    return value


def main():
    """Fixed container entry; emits two bounded files, never relies on stdout."""
    try:
        # Only executed inside the sandbox, not when importing the legacy helper.
        import resource
        sys.dont_write_bytecode = True
        if Path("/var/run/docker.sock").exists() or Path("/run/secrets").exists():
            raise ValueError("unsafe build mounts")
        soft, hard = resource.getrlimit(resource.RLIMIT_FSIZE)
        cap = ARTIFACT_LIMIT if hard == resource.RLIM_INFINITY else min(hard, ARTIFACT_LIMIT)
        resource.setrlimit(resource.RLIMIT_FSIZE, (cap, cap))
        request = _request()
        output = Path("/artifacts")
        if output.is_symlink() or not output.is_dir() or any(output.iterdir()):
            raise ValueError("build output must be a new empty directory")
        entries = gather(Path("/sandbox"))  # Syntax-only compile; never exec/import.
        inventory = [{"path": name, "sha256": hashlib.sha256(data).hexdigest()}
                     for name, data in sorted(entries.items())]
        encoded = json.dumps(inventory, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False).encode("utf-8")
        if hashlib.sha256(encoded).hexdigest() != request["source_hash"]:
            raise ValueError("build source hash changed")
        artifact = _build_entries(entries, output)
        report = {"job_id": request["job_id"], "source_hash": request["source_hash"],
                  "artifact_sha256": artifact["sha256"], "artifact_bytes": artifact["bytes"],
                  "artifact_files": artifact["files"], "kind": artifact["kind"],
                  "syntax_checked": True, "image_built": False, "release_approved": False}
        blob = json.dumps(report, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False).encode("utf-8")
        if len(blob) > RESULT_LIMIT:
            raise ValueError("build result limit exceeded")
        with (output / "build-result.json").open("xb") as stream:
            stream.write(blob)
        if {p.name for p in output.iterdir()} != {"build-result.json", "source-artifact.zip"}:
            raise ValueError("unexpected build output")
        return 0
    except (ValueError, OSError, SyntaxError, RecursionError, OverflowError):
        # Do not print candidate lines, arbitrary exceptions or paths.
        return 1


if __name__ == "__main__":
    sys.exit(main())
