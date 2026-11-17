"""Bounded, stdlib-only SOURCE ZIP review. Never extract or execute source.

Caller must first prove its exact sandbox has stopped. All output opens are
relative to a pinned directory descriptor; stdout and producer claims are not
trusted. No host compile/import of candidate code occurs here.
"""
import hashlib
import io
import json
import math
import os
import re
import stat
import struct
import zipfile

ARTIFACT_LIMIT = 12 * 1024 * 1024
RESULT_LIMIT = 16384
MANIFEST_LIMIT = 196608
SOURCE_LIMIT = 8 * 1024 * 1024
FILE_LIMIT = 512
RESULT_NAME = "build-result.json"
ZIP_NAME = "source-artifact.zip"
KIND = "offline-source-artifact"


class ArtifactError(ValueError):
    def __init__(self):
        super().__init__("build artifact verification failed")


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def strict_object(data, limit):
    if not isinstance(data, bytes) or not 0 < len(data) <= limit:
        raise ArtifactError()

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ArtifactError()
            result[key] = value
        return result

    def constant(value):
        raise ArtifactError()

    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=pairs,
                           parse_constant=constant)
        pending = [(value, 0)]
        while pending:
            item, depth = pending.pop()
            if depth > 16 or (isinstance(item, float) and not math.isfinite(item)):
                raise ArtifactError()
            if isinstance(item, str):
                item.encode("utf-8")
            elif isinstance(item, dict):
                pending.extend((v, depth + 1) for v in item.values())
                pending.extend((k, depth + 1) for k in item)
            elif isinstance(item, list):
                pending.extend((v, depth + 1) for v in item)
        if not isinstance(value, dict):
            raise ArtifactError()
        return value
    except (ValueError, UnicodeError, RecursionError, OverflowError):
        raise ArtifactError()


def _regular(directory, name, limit):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or not 0 < before.st_size <= limit):
            raise ArtifactError()
        with os.fdopen(fd, "rb", closefd=False) as stream:
            data = stream.read(limit + 1)
        after = os.fstat(fd)
        binding = lambda v: (v.st_dev, v.st_ino, v.st_size, v.st_mtime_ns,
                             v.st_ctime_ns, v.st_nlink, v.st_mode)
        current = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if (len(data) != before.st_size or binding(before) != binding(after)
                or binding(after) != binding(current)):
            raise ArtifactError()
        return data
    finally:
        os.close(fd)


def _output_names(directory):
    # Streaming enumeration: reject the third node without allocating listdir.
    # Python 3.6 Linux fallback still resolves only the already-pinned dir FD.
    try:
        scan = os.scandir(directory)
    except TypeError:
        scan = os.scandir("/proc/self/fd/" + str(directory))
    names = set()
    with scan:
        for item in scan:
            if len(names) >= 2:
                raise ArtifactError()
            names.add(item.name)
    return names


def _source_manifest(source, source_hash):
    if not isinstance(source, dict) or not 0 < len(source) <= FILE_LIMIT:
        raise ArtifactError()
    total, manifest = 0, {}
    for name, data in source.items():
        if (not isinstance(name, str) or len(name) > 240
                or not re.fullmatch(r"[A-Za-z0-9_./-]+", name)
                or not name.endswith((".py", ".html"))
                or any(not p or p.startswith(".") for p in name.split("/"))
                or len(name.split("/")) < 2 or not isinstance(data, bytes)):
            raise ArtifactError()
        total += len(data)
        if total > SOURCE_LIMIT:
            raise ArtifactError()
        manifest[name] = hashlib.sha256(data).hexdigest()
    binding = [{"path": p, "sha256": h} for p, h in sorted(manifest.items())]
    if hashlib.sha256(canonical(binding)).hexdigest() != source_hash:
        raise ArtifactError()
    return manifest


def _review_zip(blob, source, manifest):
    # Validate the fixed non-ZIP64 EOCD before ZipFile allocates central entries.
    # Producer emits no archive comment, extra fields, preamble or trailing data.
    if len(blob) < 22:
        raise ArtifactError()
    end = struct.unpack("<4s4H2LH", blob[-22:])
    signature, disk, start_disk, local_count, count, cd_size, cd_offset, comment = end
    expected_names = set(source) | {"BUILD-MANIFEST.json"}
    expected_cd_size = sum(46 + len(name.encode("ascii")) for name in expected_names)
    if (signature != b"PK\x05\x06" or disk or start_disk or comment
            or count != local_count or count != len(source) + 1
            or count > FILE_LIMIT + 1 or cd_offset + cd_size != len(blob) - 22
            or cd_size != expected_cd_size):
        raise ArtifactError()
    # ZipFile trusts central directory size, not EOCD count. Parse bounded
    # central records first so a forged small count cannot allocate many entries.
    cursor, seen = cd_offset, set()
    for unused in range(count):
        if cursor + 46 > len(blob) - 22:
            raise ArtifactError()
        header = struct.unpack("<4s6H3L5H2L", blob[cursor:cursor + 46])
        length, extra, notes, member_disk = header[10:14]
        if (header[0] != b"PK\x01\x02" or header[3] or header[4] != zipfile.ZIP_STORED
                or not 0 < length <= 240 or extra or notes or member_disk
                or cursor + 46 + length > len(blob) - 22):
            raise ArtifactError()
        name = blob[cursor + 46:cursor + 46 + length].decode("ascii")
        cap = len(source[name]) if name in source else MANIFEST_LIMIT
        if (name not in expected_names or name in seen or header[8] != header[9]
                or header[9] > cap):
            raise ArtifactError()
        seen.add(name)
        cursor += 46 + length
    if cursor != len(blob) - 22 or seen != expected_names:
        raise ArtifactError()
    with zipfile.ZipFile(io.BytesIO(blob), "r") as archive:
        entries = archive.infolist()
        names = [v.filename for v in entries]
        if (len(entries) != count or len(set(names)) != count
                or set(names) != set(source) | {"BUILD-MANIFEST.json"}
                or archive.comment or archive.start_dir != cd_offset):
            raise ArtifactError()
        offset, total, central_size = 0, 0, 0
        metadata = None
        for entry in entries:
            name = entry.filename
            encoded = name.encode("ascii")
            expected_size = len(source[name]) if name in source else MANIFEST_LIMIT
            if (entry.orig_filename != name or entry.flag_bits != 0
                    or entry.compress_type != zipfile.ZIP_STORED
                    or entry.compress_size != entry.file_size
                    or not 0 <= entry.file_size <= expected_size
                    or entry.header_offset != offset or entry.extra or entry.comment
                    or entry.volume != 0 or entry.create_system != 3
                    or not stat.S_ISREG(entry.external_attr >> 16)
                    or entry.external_attr & 0xffff):
                raise ArtifactError()
            if offset + 30 > cd_offset:
                raise ArtifactError()
            header = struct.unpack("<4s5H3L2H", blob[offset:offset + 30])
            sig, version, flags, compression, stamp, day, crc, packed, size, length, extra = header
            if (sig != b"PK\x03\x04" or flags or compression != zipfile.ZIP_STORED
                    or crc != entry.CRC or packed != entry.compress_size or size != entry.file_size
                    or length != len(encoded) or extra
                    or blob[offset + 30:offset + 30 + length] != encoded):
                raise ArtifactError()
            offset += 30 + length + size
            central_size += 46 + len(encoded)
            total += size
            if offset > cd_offset or total > SOURCE_LIMIT + MANIFEST_LIMIT:
                raise ArtifactError()
            # STORED, declared length bounded before reading; CRC checked by ZipFile.
            data = archive.read(entry)
            if name in source:
                if data != source[name] or hashlib.sha256(data).hexdigest() != manifest[name]:
                    raise ArtifactError()
            else:
                metadata = strict_object(data, MANIFEST_LIMIT)
        if offset != cd_offset or central_size != cd_size:
            raise ArtifactError()
        expected = {"kind": KIND, "source_manifest": manifest, "syntax_checked": True,
                    "image_built": False, "release_approved": False}
        if (metadata != expected or type(metadata.get("syntax_checked")) is not bool
                or metadata.get("image_built") is not False
                or metadata.get("release_approved") is not False):
            raise ArtifactError()


def verify_artifacts(output, job_id, source_hash, source, identity):
    """Review exactly two stopped-container outputs; return only bounded metadata."""
    directory = None
    try:
        if (not isinstance(job_id, str) or not re.fullmatch(r"[0-9a-f]{32}", job_id)
                or not isinstance(source_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", source_hash)
                or not isinstance(output, str) or not os.path.isabs(output)):
            raise ArtifactError()
        manifest = _source_manifest(source, source_hash)
        directory = os.open(output, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        info = os.fstat(directory)
        if (info.st_dev, info.st_ino) != identity:
            raise ArtifactError()
        if _output_names(directory) != {RESULT_NAME, ZIP_NAME}:
            raise ArtifactError()
        report = strict_object(_regular(directory, RESULT_NAME, RESULT_LIMIT), RESULT_LIMIT)
        blob = _regular(directory, ZIP_NAME, ARTIFACT_LIMIT)
        sha = hashlib.sha256(blob).hexdigest()
        expected = {"job_id": job_id, "source_hash": source_hash,
                    "artifact_sha256": sha, "artifact_bytes": len(blob),
                    "artifact_files": len(source), "kind": KIND, "syntax_checked": True,
                    "image_built": False, "release_approved": False}
        if (report != expected or type(report.get("artifact_bytes")) is not int
                or type(report.get("artifact_files")) is not int
                or report.get("syntax_checked") is not True
                or report.get("image_built") is not False
                or report.get("release_approved") is not False):
            raise ArtifactError()
        _review_zip(blob, source, manifest)
        if _output_names(directory) != {RESULT_NAME, ZIP_NAME}:
            raise ArtifactError()
        return expected
    except (OSError, ValueError, TypeError, KeyError, AttributeError, UnicodeError,
            struct.error, zipfile.BadZipFile, NotImplementedError, RuntimeError,
            RecursionError, OverflowError):
        raise ArtifactError()
    finally:
        if directory is not None:
            os.close(directory)
