# 可选单独构建OCR文本模拟：必须在仓库根目录 docker build -f ocr/Dockerfile .
# Compose默认使用共享Dockerfile.app；此文件也需要根目录的common包。
FROM python:3.11-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY . /app
RUN groupadd --gid 10001 daobrain && useradd --uid 10001 --gid 10001 --no-create-home daobrain
USER 10001:10001
EXPOSE 8014
CMD ["uvicorn", "ocr.main:app", "--host", "0.0.0.0", "--port", "8014"]
