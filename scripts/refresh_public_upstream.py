#!/usr/bin/env python3
"""在部署服务器刷新固定样例上游；仅改 active 文件，不改模板或既有站点。"""
# 宿主Python3.6也可执行；容器应用仍使用Python3.11。
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import uuid

ROOT = Path("/root/DaoBrain")
TEMPLATE = ROOT / "public_demo/nginx.locations.conf"
ACTIVE = ROOT / "public_demo/nginx.locations.active.conf"
SITE = Path("/etc/nginx/conf.d/zjg.conf")
DEFAULT_BACKUP = Path("/root/daobrain-deploy-workspace/backup/public-upstream")
CONTAINER = "daobrain-public_demo-1"
NETWORK = "daobrain_green"
PLACEHOLDER = "10.255.1.240"
PRIVATE_RANGES = tuple(ipaddress.ip_network(cidr) for cidr in
                       ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))


class SafetyError(RuntimeError):
    """任一安全前置条件不满足时，停止执行而不扩大操作范围。"""


def safe_path(path: Path) -> None:
    """拒绝相对路径与任何层级的符号链接，防止写到其他站点或目录。"""
    if not path.is_absolute() or ".." in path.parts:
        raise SafetyError(f"必须使用不含上级跳转的绝对路径：{path}")
    for part in (path, *path.parents):
        if part.is_symlink():
            raise SafetyError(f"拒绝符号链接路径：{part}")


def read_regular(path: Path) -> bytes:
    safe_path(path)
    if not path.is_file() or not stat.S_ISREG(path.stat().st_mode):
        raise SafetyError(f"文件不存在或不是普通文件：{path}")
    return path.read_bytes()


def command(args: list) -> str:
    """命令始终为固定参数数组，不启用 shell，不记录容器环境变量。"""
    result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            universal_newlines=True, timeout=30, check=False)
    if result.returncode:
        raise SafetyError(f"命令失败：{' '.join(args)}\n{result.stderr.strip()}")
    return result.stdout


def inspect_one(kind: str, name: str) -> dict:
    args = ["docker", "inspect", name] if kind == "container" else ["docker", "network", "inspect", name]
    data = json.loads(command(args))
    if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
        raise SafetyError(f"Docker inspect 返回不唯一：{name}")
    return data[0]


def public_address() -> tuple:
    """只接受运行中的固定样例容器及唯一、internal 的绿区桥接网络。"""
    info = inspect_one("container", CONTAINER)
    if info.get("Name") != "/" + CONTAINER or info.get("State", {}).get("Running") is not True:
        raise SafetyError("固定样例容器不存在或未运行")
    labels = info.get("Config", {}).get("Labels") or {}
    if (labels.get("com.docker.compose.project") != "daobrain"
            or labels.get("com.docker.compose.service") != "public_demo"):
        raise SafetyError("容器不是 daobrain 项目的 public_demo 服务")
    host = info.get("HostConfig", {})
    if host.get("NetworkMode") != NETWORK or host.get("PortBindings"):
        raise SafetyError("样例容器必须使用 daobrain_green 网络模式且不得发布端口")
    networks = info.get("NetworkSettings", {}).get("Networks") or {}
    if set(networks) != {NETWORK}:
        raise SafetyError("样例容器必须且只能连接 daobrain_green")
    endpoint = networks[NETWORK]
    address = ipaddress.ip_address(endpoint.get("IPAddress", ""))
    # is_private 单独使用不够严格；这里只允许 RFC1918 IPv4，排除回环等特殊地址。
    if not isinstance(address, ipaddress.IPv4Address) or not any(address in net for net in PRIVATE_RANGES):
        raise SafetyError("上游不是允许的 RFC1918 私有 IPv4 地址")
    network = inspect_one("network", NETWORK)
    if (network.get("Name") != NETWORK or network.get("Internal") is not True
            or network.get("Driver") != "bridge" or not network.get("Id")
            or endpoint.get("NetworkID") != network["Id"]):
        raise SafetyError("绿区网络不是匹配的 internal bridge")
    subnets = []
    for item in network.get("IPAM", {}).get("Config", []) or []:
        subnet = ipaddress.ip_network(item.get("Subnet", ""), strict=False)
        if isinstance(subnet, ipaddress.IPv4Network) and address in subnet:
            subnets.append(subnet)
        if item.get("Gateway") == str(address):
            raise SafetyError("不能将 Docker 网关当作样例容器上游")
    if len(subnets) != 1 or address in (subnets[0].network_address, subnets[0].broadcast_address):
        raise SafetyError("容器 IPv4 未唯一落在绿区有效子网内")
    container_id = info.get("Id")
    if not isinstance(container_id, str) or not container_id:
        raise SafetyError("容器缺少可复核的 ID")
    return str(address), container_id


def require_include(site: bytes) -> None:
    """仅核对现有 include，不自动修改主站点或限流配置。"""
    uncommented = "\n".join(line.split("#", 1)[0] for line in site.decode("utf-8").splitlines())
    pattern = r"(?m)^\s*include\s+" + re.escape(str(ACTIVE)) + r"\s*;\s*$"
    if len(re.findall(pattern, uncommented)) != 1:
        raise SafetyError(
            f"{SITE} 必须已包含唯一的 include {ACTIVE};。"
            "请管理员先备份原站点，在正确 server 块中人工完成初次集成及限流区配置；"
            "本脚本不会修改其他站点。"
        )


def render(template: bytes, address: str) -> bytes:
    """严格替换唯一 proxy_pass 占位 IP，保留模板其余字节不变。"""
    text = template.decode("utf-8")
    expected = f"proxy_pass http://{PLACEHOLDER}:18100/;"
    if (text.count(PLACEHOLDER) != 1 or text.count(expected) != 1
            or len(re.findall(r"(?m)^\s*proxy_pass\b", text)) != 1
            or not re.search(r"(?m)^[ \t]*" + re.escape(expected) + r"[ \t]*$", text)):
        raise SafetyError("模板必须包含唯一且完整的 10.255.1.240:18100 占位 proxy_pass")
    return template.replace(PLACEHOLDER.encode("ascii"), address.encode("ascii"), 1)


def exclusive_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    """独占创建与落盘；已有备份和临时文件绝不覆盖。"""
    safe_path(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, "wb") as output:
        os.fchmod(output.fileno(), mode)
        output.write(data)
        output.flush()
        os.fsync(output.fileno())


def sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextmanager
def directory_lock():
    """锁住现有目录，避免本脚本并发刷新；无需创建或删除锁文件。"""
    safe_path(ACTIVE.parent)
    fd = os.open(ACTIVE.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SafetyError("已有刷新任务运行，请待其结束后重新核对") from None
        yield
    finally:
        os.close(fd)


def unchanged(template: bytes, site: bytes) -> None:
    if read_regular(TEMPLATE) != template or read_regular(SITE) != site:
        raise SafetyError("模板或既有站点被并发修改；拒绝重载")


def refresh(backup_root: Path) -> None:
    safe_path(backup_root)
    # 工作备份必须在源码之外，避免进入镜像/源码包；禁止使用系统配置目录。
    if (backup_root == ROOT or ROOT in backup_root.parents or backup_root in ROOT.parents
            or any(path == backup_root or path in backup_root.parents
                   for path in (Path("/etc"), Path("/usr"), Path("/var/lib/docker")))):
        raise SafetyError("备份目录必须是独立工作区的专用子目录，不能位于源码或系统配置内")
    with directory_lock():
        template, site = read_regular(TEMPLATE), read_regular(SITE)
        require_include(site)
        safe_path(ACTIVE)
        original = read_regular(ACTIVE) if ACTIVE.exists() else None
        mode = stat.S_IMODE(ACTIVE.stat().st_mode) if original is not None else 0o644
        address, container_id = public_address()
        candidate = render(template, address)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ") + "-" + uuid.uuid4().hex
        backup_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not backup_root.is_dir():
            raise SafetyError("备份路径不是目录")
        run_backup = backup_root / stamp
        run_backup.mkdir(mode=0o700)  # 时间戳目录如已存在即失败，绝不复用覆盖。
        if original is not None:
            exclusive_write(run_backup / "nginx.locations.active.before.conf", original)
        else:
            exclusive_write(run_backup / "active-was-absent.txt", b"active did not exist before refresh\n")
        sync_directory(run_backup)
        stage = ACTIVE.parent / (".nginx.locations.pending-" + stamp + ".conf")
        exclusive_write(stage, candidate, mode)
        unchanged(template, site)
        if (read_regular(ACTIVE) if ACTIVE.exists() else None) != original:
            raise SafetyError("active 被并发修改，保留临时文件但不替换")
        if public_address() != (address, container_id):
            raise SafetyError("样例容器已重建或换 IP，保留临时文件但不替换")
        applied, reload_attempted = False, False
        try:
            os.replace(stage, ACTIVE)  # 同一目录内原子替换，不出现半写入配置。
            applied = True
            sync_directory(ACTIVE.parent)
            command(["nginx", "-t"])
            unchanged(template, site)
            if read_regular(ACTIVE) != candidate or public_address() != (address, container_id):
                raise SafetyError("校验后 active 或容器发生变化，拒绝重载")
            reload_attempted = True
            command(["nginx", "-s", "reload"])
            unchanged(template, site)
        except Exception as error:
            if applied:
                if read_regular(ACTIVE) != candidate:
                    raise SafetyError(f"active 被其他任务修改，未覆盖并发结果；需人工恢复。备份：{run_backup}") from error
                if original is not None:
                    exclusive_write(run_backup / "nginx.locations.rejected.conf", candidate)
                    restore = ACTIVE.parent / (".nginx.locations.restore-" + stamp + ".conf")
                    exclusive_write(restore, original, mode)
                    os.replace(restore, ACTIVE)
                else:
                    # 初次创建失败时通过同目录重命名恢复“不存在”，不删除任何文件。
                    rejected = ACTIVE.parent / (".nginx.locations.rejected-" + stamp + ".conf")
                    if rejected.exists() or rejected.is_symlink():
                        raise SafetyError("拒绝覆盖已有失败文件，需人工恢复 active") from error
                    os.rename(ACTIVE, rejected)
                sync_directory(ACTIVE.parent)
            state = ("已尝试 reload，磁盘配置已恢复，运行态需人工确认；不会再次重载"
                     if reload_attempted else "未执行 reload，active 已恢复到执行前状态")
            raise SafetyError(f"刷新失败：{error}\n{state}。备份：{run_backup}") from error
        print(f"已核验 internal 绿区上游：{address}:18100；nginx -t 通过并已请求 reload")
        print(f"备份目录：{run_backup}")
        print(f"模板 SHA256：{hashlib.sha256(template).hexdigest()}；旧站点 SHA256：{hashlib.sha256(site).hexdigest()}（内容未改）")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup-dir", type=Path, default=DEFAULT_BACKUP,
                        help="服务器独立工作区备份根目录（绝对路径，不能在源码内）")
    args = parser.parse_args()
    if not sys.platform.startswith("linux") or os.geteuid() != 0:
        print("拒绝执行：请在 Linux 部署服务器以 sudo -n python3 运行；不会连接远程。", file=sys.stderr)
        return 1
    try:
        refresh(args.backup_dir)
    except (SafetyError, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        print(f"安全停止：{error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
