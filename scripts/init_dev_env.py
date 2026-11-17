"""显式运行以初始化本仓库开发.env；不覆盖既有文件，不打印密钥。"""
import os
from pathlib import Path
import secrets

ROOT = Path(__file__).resolve().parents[1]


def main():
    path = ROOT / ".env"
    source = (ROOT / ".env.example").read_text(encoding="utf-8")
    keys = {name: secrets.token_urlsafe(32) for name in
            ("API_KEY", "INTERNAL_API_KEY", "APPROVAL_SECRET", "POSTGRES_PASSWORD")}
    lines = []
    for line in source.splitlines():
        key = line.partition("=")[0]
        if key in keys:
            line = key + "=" + keys[key]
        if line.startswith("POSTGRES_DSN="):
            line = "POSTGRES_DSN=postgresql+psycopg://daobrain:" + keys["POSTGRES_PASSWORD"] + "@postgres:5432/daobrain"
        lines.append(line)
    # O_EXCL保证已有.env完全不变；创建起即0600权限。
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        output.write("\n".join(lines) + "\n")
    print("已生成仅本机可读的开发.env；密钥未打印，请勿上传Git。")


if __name__ == "__main__":
    main()
