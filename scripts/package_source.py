"""显式打包源码，排除密钥、模型、用户数据与虚拟环境；不操作Git。"""
import hashlib
import json
from pathlib import Path
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
EXCLUDED = {".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache",
            "node_modules", "data", "models", "ollama", "pgdata", "qdrant_data", ".trash",
            "daobrain39-wheels", "backups"}
SPECIAL = {".env.example", ".gitignore", ".dockerignore", "Makefile", "Dockerfile", "Dockerfile.app", "Dockerfile.offline"}
EXTENSIONS = {".py", ".json", ".md", ".html", ".txt", ".yml", ".yaml", ".ini", ".sh", ".conf"}


def package(destination: Path):
    if not destination.parent.is_dir():
        raise ValueError("输出目录不存在")
    files = []
    for path in sorted(ROOT.rglob("*")):
        relative = path.relative_to(ROOT)
        if any(part in EXCLUDED for part in relative.parts) or path.is_symlink() or not path.is_file():
            continue
        if path.name.startswith(".env") and path.name != ".env.example":
            continue
        if path.name == "source-manifest.json":
            continue
        if (path.name == "nginx.locations.active.conf" or ".bak" in path.name
                or ".before-" in path.name or path.name.startswith(".nginx.locations.")):
            continue
        if path.suffix not in EXTENSIONS and path.name not in SPECIAL:
            continue
        if path.name == "requirements.txt" or path.suffix in EXTENSIONS or path.name in SPECIAL:
            files.append((path, relative))
    hashes = {}
    with zipfile.ZipFile(destination, mode="x", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, relative in files:
            data = path.read_bytes()
            name = "DaoBrain/" + relative.as_posix()
            archive.writestr(name, data)
            hashes[name] = hashlib.sha256(data).hexdigest()
        archive.writestr("DaoBrain/source-manifest.json", json.dumps(hashes, ensure_ascii=False, indent=2))
    with zipfile.ZipFile(destination) as archive:
        assert archive.testzip() is None
        for name, digest in hashes.items():
            assert hashlib.sha256(archive.read(name)).hexdigest() == digest
        assert not any(name.endswith("/.env") or "/.venv/" in name or "/data/" in name or "/models/" in name
                       for name in archive.namelist())
    return {"source_files": len(hashes), "archive_bytes": destination.stat().st_size,
            "archive_sha256": hashlib.sha256(destination.read_bytes()).hexdigest()}


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("用法：python scripts/package_source.py <新的源码zip文件>")
    print(json.dumps(package(Path(sys.argv[1])), ensure_ascii=False, indent=2))
