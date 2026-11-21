"""服务器按SHA256清单安装纯源码，先备份原文件，绝不覆盖.env/权重或删除文件。

用法：sudo python3 install_source.py <源码zip绝对路径> <包SHA256>
目标目录默认 /home/admin/DaoBrain，可用环境变量 DAOBRAIN_HOME 覆盖；
备份根目录默认 <DAOBRAIN_HOME同级>/daobrain-deploy-workspace/backup/source，
可用 DAOBRAIN_DEPLOY_BACKUP 覆盖。兼容宿主 Python 3.6。
"""
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sys
import uuid
import zipfile

ROOT = Path(os.environ.get("DAOBRAIN_HOME", "/home/admin/DaoBrain"))
BACKUP = Path(os.environ.get(
    "DAOBRAIN_DEPLOY_BACKUP",
    str(ROOT.parent / "daobrain-deploy-workspace" / "backup" / "source")))
BLOCKED = {'.git', '.venv', '__pycache__', 'data', 'models', 'ollama', 'pgdata',
           'qdrant_data', 'node_modules', 'daobrain39-wheels', 'backups'}


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def main():
    require(len(sys.argv) == 3, '需要源码zip绝对路径和本机SHA256')
    archive_path = Path(sys.argv[1])
    require(archive_path.is_absolute() and archive_path.is_file(), '源码包路径无效')
    require(hashlib.sha256(archive_path.read_bytes()).hexdigest() == sys.argv[2], '包SHA256不匹配')
    require(ROOT.is_dir() and not ROOT.is_symlink(), '目标目录未准备好')
    payloads = []
    with zipfile.ZipFile(archive_path) as archive:
        names = archive.namelist()
        require(len(names) == len(set(names)) and archive.testzip() is None, '包内容重复或CRC无效')
        manifest = json.loads(archive.read('DaoBrain/source-manifest.json'))
        require(set(names) == set(manifest) | {'DaoBrain/source-manifest.json'}, '清单与包成员不匹配')
        for name, digest in manifest.items():
            parts = PurePosixPath(name).parts
            require(parts and parts[0] == 'DaoBrain' and '..' not in parts and not name.startswith('/'), '非法路径')
            relative = Path(*parts[1:])
            require(relative.parts and not any(p in BLOCKED for p in relative.parts), '包含禁止数据目录')
            require(not relative.name.startswith('.env') or str(relative) == '.env.example', '禁止分发实际.env')
            require(relative.name != 'nginx.locations.active.conf' and '.bak' not in relative.name
                    and '.before-' not in relative.name, '禁止分发动态配置或备份')
            target = ROOT / relative
            require(not any(p.is_symlink() for p in [target, *target.parents]), '安装路径含链接')
            data = archive.read(name)
            require(hashlib.sha256(data).hexdigest() == digest, '源码文件hash不匹配')
            payloads.append((relative, data))
    backup = BACKUP / (datetime.utcnow().strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex)
    backup.mkdir(parents=True, mode=0o700, exist_ok=False)
    for relative, data in payloads:
        target = ROOT / relative
        if target.exists():
            require(target.is_file(), '旧源码目标不是文件')
            saved = backup / relative
            saved.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(target), str(saved))
            require(saved.read_bytes() == target.read_bytes(), '备份校验失败')
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        require(target.read_bytes() == data, '源码安装校验失败')
    print('SOURCE-INSTALLED-VERIFIED', len(payloads), 'backup=' + str(backup))


if __name__ == '__main__':
    main()
