from __future__ import annotations

from pathlib import Path

import yaml

from agents.base.memory import HybridMemory
from core.expert_pool import ExpertPool
from core.global_agent_pool import GlobalAgentPool
from core.workspace import metis_data_path


ROOT = Path(__file__).resolve().parents[2]


def _render_web_service() -> dict:
    document = yaml.safe_load((ROOT / "render.yaml").read_text(encoding="utf-8"))
    return next(service for service in document["services"] if service["type"] == "web")


def test_metis_data_dir_contains_all_local_runtime_state(
    monkeypatch, tmp_path: Path,
) -> None:
    data_root = tmp_path / "durable"
    monkeypatch.setenv("METIS_DATA_DIR", str(data_root))

    memory = HybridMemory("memory/project-1")
    experts = ExpertPool()
    employees = GlobalAgentPool()

    assert memory.sqlite.db_path == data_root / "memory" / "project-1" / "memory.db"
    assert memory.vector.vectors_path == data_root / "memory" / "project-1" / "vectors.db"
    assert experts._data_dir == data_root / "pools"
    assert employees.data_dir == data_root / "pools"
    assert metis_data_path("projects", legacy=tmp_path / "legacy") == data_root / "projects"


def test_explicit_pool_data_directories_still_override_global_root(
    monkeypatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("METIS_DATA_DIR", str(tmp_path / "durable"))
    explicit = tmp_path / "explicit"

    assert ExpertPool(str(explicit))._data_dir == explicit
    assert GlobalAgentPool(str(explicit)).data_dir == explicit


def test_render_uses_paid_persistent_disk_and_explicit_acceptance_policy() -> None:
    service = _render_web_service()
    env = {item["key"]: item for item in service["envVars"]}

    assert service["plan"] != "free"
    assert service["disk"]["mountPath"] == "/var/data"
    assert int(service["disk"]["sizeGB"]) >= 1
    assert env["METIS_DATA_DIR"]["value"] == "/var/data"
    assert env["RUNTIME_ACCEPTANCE_REQUIRED"]["value"] in {"true", "false"}
    assert env["RUNTIME_ACCEPTANCE_ENABLED"]["value"] in {"true", "false"}


def test_render_nginx_and_container_follow_dynamic_port_and_proxy_protocol() -> None:
    nginx = (ROOT / "nginx-render.conf").read_text(encoding="utf-8")
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "listen ${PORT};" in nginx
    assert "$metis_forwarded_proto" in nginx
    assert "proxy_set_header X-Forwarded-Proto $scheme;" not in nginx
    assert "envsubst" in dockerfile
    assert 'PORT="${PORT:-10000}"' in dockerfile
    assert "postgresql-client" in dockerfile


def test_render_repairs_runtime_volume_permissions_before_starting_services() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    runtime_chown = 'chown -R www-data:www-data "$METIS_DATA_DIR"'
    assert 'case "$METIS_DATA_DIR" in /var/data)' in dockerfile
    assert runtime_chown in dockerfile
    assert dockerfile.index(runtime_chown) < dockerfile.index(
        "exec supervisord -n -c /etc/supervisor/supervisord.conf"
    )


def test_railway_uses_standard_dockerfile_and_writable_runtime_home() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    railway = (ROOT / "railway.toml").read_text(encoding="utf-8")

    assert "HOME=/var/data" in dockerfile
    assert 'builder = "DOCKERFILE"' in railway
    assert 'dockerfilePath = "Dockerfile"' in railway
    assert 'healthcheckPath = "/health"' in railway
    assert "healthcheckTimeout = 300" in railway


def test_container_shell_scripts_use_unix_line_endings() -> None:
    assert b"\r\n" not in (ROOT / "backend" / "startup.sh").read_bytes()


def test_compose_serves_the_vite_app_prefix_and_supports_external_database() -> None:
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    frontend_dockerfile = (ROOT / "frontend" / "Dockerfile").read_text(encoding="utf-8")
    frontend_nginx = (ROOT / "frontend" / "nginx.conf").read_text(encoding="utf-8")
    caddy = (ROOT / "Caddyfile").read_text(encoding="utf-8")

    assert "DATABASE_URL=${DATABASE_URL:-" in compose
    assert "METIS_DATA_DIR=/var/data" in compose
    assert "metis_data:/var/data" in compose
    assert "/usr/share/nginx/html/app" in frontend_dockerfile
    assert "location /app/" in frontend_nginx
    assert "/app/index.html" in frontend_nginx
    assert "{$METIS_SITE_ADDRESS:" in caddy
    assert "{$ACME_EMAIL:" in caddy
    assert "YOUR_DOMAIN" not in caddy
    assert "admin@example.com" not in caddy


def test_both_backend_images_include_postgresql_recovery_tools() -> None:
    assert "postgresql-client" in (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "postgresql-client" in (ROOT / "backend" / "Dockerfile").read_text(encoding="utf-8")
