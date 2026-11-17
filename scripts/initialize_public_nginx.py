"""首次集成已授权的/daobrain/固定样例路径；只新增include和独立限流区。

管理员显式执行sudo python3 scripts/initialize_public_nginx.py。已有include交给刷新脚本。
备份当前站点而非依赖旧快照；未点名站点字节保持不变，nginx -t失败恢复不重载。
"""
from datetime import datetime
import hashlib
from pathlib import Path
import shutil
import subprocess
import uuid

from refresh_public_upstream import public_address, render

ROOT = Path('/root/DaoBrain')
SITE = Path('/etc/nginx/conf.d/zjg.conf')
RATE = Path('/etc/nginx/conf.d/daobrain-public-rate.conf')
ACTIVE = ROOT / 'public_demo/nginx.locations.active.conf'
INCLUDE = 'include /root/DaoBrain/public_demo/nginx.locations.active.conf;\n\n'
NEEDLE = '# ===== 子目录反向代理 ====='


def main():
    source = SITE.read_bytes()
    text = source.decode('utf-8')
    if INCLUDE.strip() in text:
        print('PUBLIC-INCLUDE-ALREADY-EXISTS；请运行refresh_public_upstream.py')
        return
    if (text.count(NEEDLE) != 1 or '/daobrain' in text or SITE.is_symlink()
            or not ROOT.is_dir() or not RATE.parent.is_dir()):
        raise RuntimeError('原站点不符合唯一集成位置，停止而不改写')
    backup = Path('/root/daobrain-deploy-workspace/backup/nginx-initial') / (
        datetime.utcnow().strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex)
    backup.mkdir(parents=True, exist_ok=False, mode=0o700)
    shutil.copy2(str(SITE), str(backup / 'zjg.before.conf'))
    if (backup / 'zjg.before.conf').read_bytes() != source:
        raise RuntimeError('原站点备份失败')
    rate_bytes = (ROOT / 'public_demo/nginx.rate.conf').read_bytes()
    if RATE.exists() and RATE.read_bytes() != rate_bytes:
        raise RuntimeError('已有限流文件不同，拒绝覆盖')
    old_active = ACTIVE.read_bytes() if ACTIVE.exists() else None
    if old_active is not None:
        shutil.copy2(str(ACTIVE), str(backup / 'active.before.conf'))
    ip, container_id = public_address()
    candidate = render((ROOT / 'public_demo/nginx.locations.conf').read_bytes(), ip)
    if SITE.read_bytes() != source:
        raise RuntimeError('原站点被并发修改，停止')
    if not RATE.exists():
        with RATE.open('xb') as out:
            out.write(rate_bytes)
    ACTIVE.write_bytes(candidate)
    modified = text.replace(NEEDLE, INCLUDE + NEEDLE, 1).encode('utf-8')
    SITE.write_bytes(modified)
    result = subprocess.run(['nginx', '-t'])
    if result.returncode:
        SITE.write_bytes(source)
        if old_active is not None:
            ACTIVE.write_bytes(old_active)
        raise RuntimeError('Nginx校验失败，已恢复原站点且未reload；新增文件保留供诊断')
    if SITE.read_bytes().replace(INCLUDE.encode('utf-8'), b'', 1) != source:
        SITE.write_bytes(source)
        raise RuntimeError('旧站点字节不一致，已恢复且未reload')
    if public_address() != (ip, container_id):
        SITE.write_bytes(source)
        raise RuntimeError('容器发生变化，停止且未reload')
    subprocess.run(['nginx', '-s', 'reload'], check=True)
    print('PUBLIC-NGINX-INITIALIZED-OK', 'backup=' + str(backup),
          'original_sha256=' + hashlib.sha256(source).hexdigest())


if __name__ == '__main__':
    main()
