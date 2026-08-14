from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_small_demo_deployment_assets_exist():
    required = (
        "Dockerfile",
        ".dockerignore",
        "compose.yaml",
        "deploy/install_demo.sh",
        "deploy/backup_sqlite.sh",
        "deploy/nginx-agent-platform.conf.template",
        "deploy/env.production.example",
    )
    assert all((ROOT / name).is_file() for name in required)


def test_demo_container_is_single_worker_and_loopback_only():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    assert '"--workers", "1"' in dockerfile
    assert '"127.0.0.1:8003:8003"' in compose
    assert "no-new-privileges:true" in compose
    assert "cap_drop:" in compose and "- ALL" in compose
    assert "PYTHON_BASE_IMAGE" in dockerfile
    assert "PYTHON_BASE_IMAGE" in compose


def test_frontend_uses_same_origin_api_when_deployed():
    html = (ROOT / "frontend_prototype.html").read_text(encoding="utf-8")
    assert "window.location.origin" in html
    assert "remotePageWithLoopbackApi" in html
    assert "let API_BASE = apiFromUrl" in html


def test_production_template_enforces_auth_and_mock_broker_boundary():
    env = (ROOT / "deploy/env.production.example").read_text(encoding="utf-8")
    install = (ROOT / "deploy/install_demo.sh").read_text(encoding="utf-8")
    assert "APP_ENV=production" in env
    assert "AUTH_ENABLED=true" in env
    assert "LANGGRAPH_USE_MEMORY_SAVER=false" in env
    assert "LLM_PROVIDER=mock" in env
    assert "docker compose --env-file .env.production up -d --build" in install
    assert "broker" not in install.lower()


def test_linux_deployment_scripts_are_forced_to_lf():
    attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "*.sh text eol=lf" in attributes


def test_github_ci_installs_dependencies_used_by_offline_tests():
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert '.[dev,akshare,llm]' in workflow
    assert "LLM_PROVIDER: mock" in workflow
    assert 'DEEPSEEK_API_KEY: ""' in workflow


def test_nginx_does_not_expose_upstream_directly():
    nginx = (ROOT / "deploy/nginx-agent-platform.conf.template").read_text(
        encoding="utf-8"
    )
    assert "proxy_pass http://127.0.0.1:8003" in nginx
    assert "listen 80" in nginx
    assert "proxy_read_timeout 300s" in nginx
