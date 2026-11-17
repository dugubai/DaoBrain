#!/usr/bin/env bash
# 在39.102.49.1运行：sudo -n bash scripts/deploy_cpu.sh /root/DaoBrain
# 只重建本项目；不停止其他站点、不格式化磁盘、不改Docker全局网络。
set -euo pipefail
repo=${1:-/root/DaoBrain}
test "$repo" = /root/DaoBrain || { printf '目标目录不匹配\n'; exit 1; }
ls -ld "$repo" "$repo/scripts"
cd "$repo"
test -f .env && test -f requirements-linux.lock.txt
# 密钥只用于运行，不应进入COPY上下文；输出只报告是否存在，不打印.env。
python3 - <<'PY'
from pathlib import Path
import stat
p=Path('.env')
if not p.is_file() or p.is_symlink() or stat.S_IMODE(p.stat().st_mode) != 0o600:
    raise RuntimeError('.env必须是0600的普通非链接文件')
PY
# 只在本脚本进程清除同名shell覆盖，确保插值和env_file读取同一份.env。
# 不改密钥文件、全局环境或权限；也不打印秘密值。
unset API_KEY INTERNAL_API_KEY APPROVAL_SECRET POSTGRES_PASSWORD
stack=(-f docker-compose.yml -f public_demo/compose.yml -f compose.cpu.yml)
docker compose "${stack[@]}" --profile model config --quiet
# 先保存上一代镜像标签，以便故障时重建回滚；不删除旧镜像或数据卷。
stamp=$(date +%Y%m%d-%H%M%S)
if docker image inspect daobrain-app:dev >/dev/null; then
  docker tag daobrain-app:dev "daobrain-app:before-cpu-$stamp"
fi
if docker image inspect daobrain-demo:dev >/dev/null; then
  docker tag daobrain-demo:dev "daobrain-demo:before-cpu-$stamp"
fi
printf '回滚镜像时间戳：%s\n' "$stamp"
# 下载前业务尚未切换。模型卷准备成功也不能冒称推理验收成功。
python3 scripts/download_cpu_model.py
# 现成Linux wheel避免构建期DNS依赖；pip check在Dockerfile内验证。
if test -d daobrain39-wheels; then
  docker build --network=none -f Dockerfile.offline -t daobrain-app:dev .
else
  docker build -f Dockerfile.app -t daobrain-app:dev .
fi
docker build --network=none -f public_demo/Dockerfile -t daobrain-demo:dev .
docker compose "${stack[@]}" --profile model up -d --no-build
# 真实HTTP就绪等待只做检查，不改任何访问控制或密钥。
for attempt in $(seq 1 30); do
  if docker compose "${stack[@]}" exec -T gateway python -c 'import urllib.request; urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=2)'; then
    break
  fi
  test "$attempt" -lt 30 || exit 1
  sleep 1
done
# 动态容器IP每次重建后刷新；nginx -t失败不reload。
python3 scripts/refresh_public_upstream.py
# 一项都不能略过：源码回归、真模型推理/3样例/三RAG、完整拒绝联调。
docker compose "${stack[@]}" exec -T gateway python -m pytest -q -p no:cacheprovider tests
docker compose "${stack[@]}" exec -T hybrid_iface python scripts/verify_runtime.py
docker compose "${stack[@]}" exec -T hybrid_iface python scripts/verify_cpu_model.py
docker compose "${stack[@]}" exec -T gateway python tests/e2e_chain.py
docker compose "${stack[@]}" exec -T postgres pg_isready -U daobrain -d daobrain
docker compose "${stack[@]}" exec -T redis redis-cli ping
docker compose "${stack[@]}" ps
printf 'CPU-DEPLOY-ACCEPTANCE-OK\n'
