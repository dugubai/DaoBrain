# 所有后端模块共用这一个镜像，compose 里以不同 command 启动各服务，省磁盘。
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1

WORKDIR /app

# 顶层需求与已验收Linux依赖快照同时约束；禁止每次构建漂移传递依赖。
COPY requirements.txt requirements-linux.lock.txt /app/
RUN pip install --no-cache-dir -r /app/requirements.txt -c /app/requirements-linux.lock.txt && pip check

# .dockerignore排除.env、模型、数据和Git历史；模拟镜像仅含代码。
COPY . /app
RUN groupadd --gid 10001 daobrain && useradd --uid 10001 --gid 10001 --no-create-home daobrain
USER 10001:10001

EXPOSE 8000
# 默认跑 gateway；compose 各服务会覆盖 command
CMD ["uvicorn", "gateway.main:app", "--host", "0.0.0.0", "--port", "8000"]
