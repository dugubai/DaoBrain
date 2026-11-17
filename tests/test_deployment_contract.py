"""部署文件静态契约；不将它当成Docker实际启动验收。"""
import importlib.util
from pathlib import Path
import re
import yaml

ROOT = Path(__file__).resolve().parents[1]


def compose():
    return yaml.safe_load((ROOT / "docker-compose.yml").read_text())


def test_only_gateway_publishes_loopback():
    services = compose()["services"]
    for name, config in services.items():
        if name == "gateway":
            assert config["ports"] == ["127.0.0.1:8000:8000"]
        else:
            assert not config.get("ports"), name


def test_red_services_no_green_or_egress():
    data = compose()
    for name in ("rag_red", "daobrain_tuned", "redline_gate"):
        assert data["services"][name]["networks"] == ["red"]
    assert all(data["networks"][name]["internal"] for name in ("red", "green", "local"))
    assert data["services"]["model_download"]["networks"] == ["download"]


def test_every_app_started_by_default_and_has_module():
    data = compose()
    for name, service in data["services"].items():
        if service.get("image") != "daobrain-app:dev":
            continue
        assert not service.get("profiles"), name
        assert service["read_only"] and service["cap_drop"] == ["ALL"]
        assert (ROOT / name / "main.py").is_file()
        assert service["command"][1] == name + ".main:app"


def test_build_ignores_secrets_and_no_root_user():
    excluded = (ROOT / ".dockerignore").read_text()
    assert all(entry in excluded for entry in (".env", ".git/", ".venv/", "data/", "models/"))
    assert "USER 10001:10001" in (ROOT / "Dockerfile.app").read_text()


def test_env_initialization_is_exclusive_and_permissioned(tmp_path, monkeypatch):
    path = ROOT / "scripts" / "init_dev_env.py"
    spec = importlib.util.spec_from_file_location("dev_env_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    (tmp_path / ".env.example").write_text((ROOT / ".env.example").read_text())
    module.main()
    env = tmp_path / ".env"
    content = env.read_bytes()
    assert env.stat().st_mode & 0o777 == 0o600
    values = dict(line.split("=", 1) for line in content.decode().splitlines()
                  if line and not line.startswith("#") and "=" in line)
    assert all(len(values[key]) >= 32 for key in ("API_KEY", "INTERNAL_API_KEY", "APPROVAL_SECRET", "POSTGRES_PASSWORD"))
    try:
        module.main()
    except FileExistsError:
        pass
    else:
        raise AssertionError("初始化不应覆盖既有.env")
    assert env.read_bytes() == content


def test_portal_has_no_storage_or_external_scripts():
    portal = (ROOT / "portal" / "index.html").read_text()
    assert "localStorage" not in portal and "sessionStorage" not in portal
    assert not re.search(r"<script[^>]+src=", portal)
