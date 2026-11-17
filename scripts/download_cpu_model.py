"""只下载固定公开模型到专用Docker卷；不读取业务.env、不接触任何语料。

sudo python3 scripts/download_cpu_model.py。兼容宿主Python3.6，保持TLS与hash验证。
失败残片保留，不删除文件；硬链接独占安装确保不会覆盖并发任务产物。
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import urllib.request
import uuid

MODEL = "qwen2.5:0.5b"
VOLUME = "daobrain_ollama"
REGISTRY = "https://registry.ollama.ai/v2/library/qwen2.5"
WEIGHT_DIGEST = "sha256:c5396e06af294bd101b30dce59131a76d2b773e76950acc870eda801d3ab0515"


def require(condition, message):
    # 不能使用assert作为安全检查；python -O不得关闭模型完整性校验。
    if not condition:
        raise RuntimeError(message)


class SecureRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file, code, message, headers, newurl):
        require(newurl.lower().startswith("https://"), "拒绝模型下载TLS降级重定向")
        return super().redirect_request(request, file, code, message, headers, newurl)


def digest_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1048576), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main():
    subprocess.run(["docker", "volume", "create", VOLUME], check=True,
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    info = json.loads(subprocess.check_output(["docker", "volume", "inspect", VOLUME]))[0]
    require(info["Name"] == VOLUME and info["Driver"] == "local", "模型卷身份不匹配")
    root = Path(info["Mountpoint"])
    require(root.is_absolute() and root.is_dir() and not root.is_symlink(), "模型卷路径无效")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), SecureRedirect())
    with opener.open(REGISTRY + "/manifests/0.5b", timeout=30) as response:
        raw_manifest = response.read(65537)
    require(len(raw_manifest) <= 65536, "模型清单超限")
    manifest = json.loads(raw_manifest)
    require(manifest["schemaVersion"] == 2, "模型清单版本不匹配")
    layers = manifest["layers"]
    require(any(layer["digest"] == WEIGHT_DIGEST for layer in layers), "标签已漂移，停止并重新审核模型")
    blobs = root / "models" / "blobs"
    blobs.mkdir(parents=True, exist_ok=True)
    require(not blobs.is_symlink() and not blobs.parent.is_symlink(), "模型目录不得为链接")
    for layer in [manifest["config"], *layers]:
        digest = layer["digest"]
        require(digest.startswith("sha256:") and len(digest) == 71, "模型digest无效")
        expected = digest.split(":")[1]
        require(all(c in "0123456789abcdef" for c in expected), "模型hash无效")
        size = layer["size"]
        require(type(size) is int and 0 < size < 500000000, "模型层大小超限")
        target = blobs / digest.replace(":", "-")
        require(not target.is_symlink(), "模型层不得为链接")
        if target.exists():
            require(target.stat().st_size == size and digest_file(target) == expected, "现有模型层校验失败")
            print("EXISTING-BLOB-VERIFIED", digest, flush=True)
            continue
        partial = blobs / (target.name + ".partial-" + uuid.uuid4().hex)
        received = 0
        hash_state = hashlib.sha256()
        with opener.open(REGISTRY + "/blobs/" + digest, timeout=60) as response, partial.open("xb") as out:
            while True:
                chunk = response.read(1048576)
                if not chunk:
                    break
                received += len(chunk)
                require(received <= size, "权重响应超出清单大小")
                hash_state.update(chunk)
                out.write(chunk)
        require(received == size and hash_state.hexdigest() == expected, "模型hash或长度不匹配")
        # link遇到已有target会原子失败，不覆盖；保留partial同inode不额外占权重空间。
        os.link(str(partial), str(target))
        require(target.exists() and digest_file(target) == expected, "模型安装校验失败")
        print("BLOB-DOWNLOADED-VERIFIED", digest, received, flush=True)
    manifest_path = root / "models" / "manifests" / "registry.ollama.ai" / "library" / "qwen2.5" / "0.5b"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    require(not any(p.is_symlink() for p in [manifest_path, *manifest_path.parents]), "清单路径不得为链接")
    if manifest_path.exists():
        require(manifest_path.read_bytes() == raw_manifest, "现有清单不同，不能覆盖")
    else:
        with manifest_path.open("xb") as stream:
            stream.write(raw_manifest)
    require(manifest_path.read_bytes() == raw_manifest, "模型清单安装失败")
    print("CPU-MODEL-DOWNLOAD-OK", MODEL, "manifest_sha256=" + hashlib.sha256(raw_manifest).hexdigest())


if __name__ == "__main__":
    main()
