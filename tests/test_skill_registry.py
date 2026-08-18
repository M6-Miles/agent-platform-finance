from __future__ import annotations

import json
import io
import zipfile
import hashlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_platform.config import Settings
from agent_platform.core.skill_registry import SkillRegistry
from agent_platform.core.skill_marketplace import SkillMarketplace, package_sha256
from agent_platform.core.tools import ToolRegistry
from agent_platform.finance.sample_data_provider import SampleMarketDataProvider
from agent_platform.services.application_service import ApplicationService
from agent_platform.services.runtime_factory import build_runtime


def _skill(tmp_path: Path, name: str = "hello_skill", handler: str = "def run(name='world'):\n    return {'hello': name}\n") -> Path:
    directory = tmp_path / name
    directory.mkdir()
    (directory / "skill.json").write_text(json.dumps({
        "name": name,
        "version": "1.0.0",
        "description": "测试 Skill",
        "entrypoint": "handler:run",
        "allowed_agents": ["chat_agent"],
    }), encoding="utf-8")
    (directory / "handler.py").write_text(handler, encoding="utf-8")
    return directory


def test_discovers_and_registers_enabled_skill(tmp_path: Path) -> None:
    _skill(tmp_path)
    registry = SkillRegistry(tmp_path, tmp_path / "state.json")
    tools = ToolRegistry()
    assert registry.register_tools(tools) == ["skill_hello_skill"]
    result = tools.execute("skill_hello_skill", {"name": "Codex"})
    assert result.is_error is False
    assert json.loads(result.output) == {"hello": "Codex"}


def test_disable_persists_and_removes_tool(tmp_path: Path) -> None:
    _skill(tmp_path)
    state = tmp_path / "state.json"
    registry = SkillRegistry(tmp_path, state)
    record = registry.set_enabled("hello_skill", False)
    assert record.enabled is False
    assert SkillRegistry(tmp_path, state).list()[0].enabled is False
    tools = ToolRegistry()
    assert SkillRegistry(tmp_path, state).register_tools(tools) == []


def test_rejects_forbidden_import(tmp_path: Path) -> None:
    _skill(tmp_path, handler="import subprocess\ndef run():\n    return {}\n")
    record = SkillRegistry(tmp_path).list()[0]
    assert record.status == "invalid"
    assert "禁止导入" in (record.error or "")


def test_rejects_unknown_permission(tmp_path: Path) -> None:
    directory = _skill(tmp_path)
    manifest = json.loads((directory / "skill.json").read_text(encoding="utf-8"))
    manifest["permissions"] = ["orders"]
    (directory / "skill.json").write_text(json.dumps(manifest), encoding="utf-8")
    record = SkillRegistry(tmp_path).list()[0]
    assert record.status == "invalid"
    assert "权限" in (record.error or "")


def test_allowed_agent_is_enforced(tmp_path: Path) -> None:
    _skill(tmp_path)
    registry = SkillRegistry(tmp_path)
    tools = ToolRegistry()
    assert registry.register_tools(tools, agent_name="weather_agent") == []


def test_invalid_manifest_does_not_break_other_skills(tmp_path: Path) -> None:
    _skill(tmp_path, "valid_skill")
    broken = tmp_path / "broken_skill"
    broken.mkdir()
    (broken / "skill.json").write_text("{}", encoding="utf-8")
    records = SkillRegistry(tmp_path).list()
    assert {record.status for record in records} == {"ready", "invalid"}


def test_admin_api_can_reload_toggle_and_run_skill(tmp_path: Path, monkeypatch) -> None:
    from agent_platform.api import main as main_mod

    source_dir = tmp_path / "hello_skill"
    _skill(tmp_path, "hello_skill")
    catalog_dir = tmp_path / "skill_catalog"
    catalog_dir.mkdir()
    (catalog_dir / "catalog.json").write_text(json.dumps([{
        "id": "hello_skill", "name": "hello_skill", "version": "1.0.0",
        "description": "测试 Skill", "permissions": [], "source": "hello_skill",
        "sha256": package_sha256(source_dir), "author": "test",
    }]), encoding="utf-8")
    provider = SampleMarketDataProvider()
    settings = Settings(
        sqlite_path=tmp_path / "app.sqlite3",
        sample_prices_csv=provider.csv_path,
        skill_catalog_path=catalog_dir / "catalog.json",
        user_skills_dir=tmp_path / "user_skills",
        auth_enabled=True,
        auth_secret="skill-test-secret-at-least-32-characters",
        auth_registration_enabled=True,
        langgraph_use_memory_saver=True,
    )
    service = ApplicationService(settings=settings, market_data=provider)
    monkeypatch.setattr(main_mod, "_app_service", service)
    main_mod.SecurityRateLimitMiddleware._windows.clear()
    client = TestClient(main_mod.app)
    registered = client.post(
        "/auth/register", json={"username": "admin", "password": "strong-pass-123"}
    ).json()
    headers = {"Authorization": f"Bearer {registered['access_token']}"}

    catalog = client.get("/skill-marketplace", headers=headers).json()
    assert catalog["skills"][0]["installed"] is False
    installed = client.post("/skill-marketplace/hello_skill/install", headers=headers)
    assert installed.status_code == 200
    admin_runtime = build_runtime(lambda symbol: symbol, settings, registered["user"]["id"])
    assert "skill_hello_skill" in admin_runtime.tools.names()
    listing = client.get("/skills", headers=headers).json()
    assert listing["skills"][0]["name"] == "hello_skill"
    assert "path" not in listing["skills"][0]
    run = client.post(
        "/skills/hello_skill/run", json={"arguments": {"name": "API"}}, headers=headers,
    )
    assert run.json()["result"] == {"hello": "API"}
    disabled = client.patch(
        "/my/skills/hello_skill", json={"enabled": False}, headers=headers,
    )
    assert disabled.json()["enabled"] is False
    assert client.post(
        "/skills/hello_skill/run", json={"arguments": {}}, headers=headers,
    ).status_code == 404
    other = client.post(
        "/auth/register", json={"username": "member", "password": "strong-pass-456"}
    ).json()
    other_headers = {"Authorization": f"Bearer {other['access_token']}"}
    assert client.get("/skills", headers=other_headers).json()["total"] == 0
    member_runtime = build_runtime(lambda symbol: symbol, settings, other["user"]["id"])
    assert "skill_hello_skill" not in member_runtime.tools.names()
    assert client.post(
        "/skills/hello_skill/run", json={"arguments": {}}, headers=other_headers,
    ).status_code == 404
    assert client.delete("/my/skills/hello_skill", headers=headers).json()["deleted"] is True
    assert client.get("/skills", headers=headers).json()["total"] == 0
    service.close()


def test_marketplace_rejects_sha256_mismatch(tmp_path: Path) -> None:
    _skill(tmp_path, "hello_skill")
    catalog_dir = tmp_path / "skill_catalog"
    catalog_dir.mkdir()
    (catalog_dir / "catalog.json").write_text(json.dumps([{
        "id": "hello_skill", "name": "hello_skill", "version": "1.0.0",
        "description": "测试", "permissions": [], "source": "hello_skill",
        "sha256": "0" * 64,
    }]), encoding="utf-8")
    marketplace = SkillMarketplace(catalog_dir / "catalog.json", tmp_path / "users")
    with pytest.raises(ValueError, match="SHA256"):
        marketplace.install("hello_skill", "user-a")
    assert not (tmp_path / "users" / "user-a" / "hello_skill").exists()


def test_marketplace_delete_cannot_escape_user_directory(tmp_path: Path) -> None:
    catalog = tmp_path / "skill_catalog" / "catalog.json"
    catalog.parent.mkdir()
    catalog.write_text("[]", encoding="utf-8")
    protected = tmp_path / "users" / "user-b" / "keep.txt"
    protected.parent.mkdir(parents=True)
    protected.write_text("keep", encoding="utf-8")
    marketplace = SkillMarketplace(catalog, tmp_path / "users")
    with pytest.raises(ValueError):
        marketplace.delete("..", "user-a")
    assert protected.read_text(encoding="utf-8") == "keep"


def test_remote_catalog_downloads_and_installs_per_user(tmp_path: Path, monkeypatch) -> None:
    manifest = json.dumps({
        "name": "remote_skill", "version": "1.0.0", "description": "远程测试",
        "entrypoint": "handler:run", "permissions": [], "allowed_agents": ["chat_agent"],
    }, ensure_ascii=False).encode("utf-8")
    handler = b"def run(value='ok'):\n    return {'value': value}\n"
    archive_buffer = io.BytesIO()
    with zipfile.ZipFile(archive_buffer, "w") as bundle:
        bundle.writestr("remote_skill/skill.json", manifest)
        bundle.writestr("remote_skill/handler.py", handler)
    archive = archive_buffer.getvalue()
    archive_hash = hashlib.sha256(archive).hexdigest()
    catalog_url = "http://127.0.0.1:9999/catalog.json"
    download_url = "http://127.0.0.1:9999/remote_skill.zip"
    catalog = json.dumps([{
        "id": "remote_skill", "name": "remote_skill", "version": "1.0.0",
        "description": "远程测试", "permissions": [], "download_url": download_url,
        "sha256": archive_hash, "author": "远程测试目录",
    }]).encode("utf-8")

    class Response:
        def __init__(self, content: bytes): self.content = content
        def __enter__(self): return self
        def __exit__(self, *_args): return None
        def read(self, *_args): return self.content

    def fake_urlopen(request, timeout=0):
        assert timeout in {8, 20}
        return Response(catalog if request.full_url == catalog_url else archive)

    import agent_platform.core.skill_marketplace as marketplace_module
    monkeypatch.setattr(marketplace_module.urllib.request, "urlopen", fake_urlopen)
    local_catalog = tmp_path / "catalog.json"
    local_catalog.write_text("[]", encoding="utf-8")
    marketplace = SkillMarketplace(local_catalog, tmp_path / "users", (catalog_url,))
    items = marketplace.user_items("user-a")
    assert items[0]["remote"] is True
    registry_id = hashlib.sha256(catalog_url.encode("utf-8")).hexdigest()[:12]
    assert items[0]["id"] == f"remote:{registry_id}:remote_skill"
    installed = marketplace.install(items[0]["id"], "user-a")
    assert installed["installed"] is True
    assert (tmp_path / "users" / "user-a" / "remote_skill" / "skill.json").exists()
    assert marketplace.user_items("user-b")[0]["installed"] is False


def test_instruction_skill_is_context_not_executable_tool(tmp_path: Path) -> None:
    directory = tmp_path / "openai_demo"
    directory.mkdir()
    instructions = "---\nname: demo\ndescription: 演示工作流\n---\n回答前先列出假设。"
    (directory / "SKILL.md").write_text(instructions, encoding="utf-8")
    (directory / "skill.json").write_text(json.dumps({
        "name": "openai_demo", "version": "abc123", "description": "演示工作流",
        "kind": "instruction", "instructions_file": "SKILL.md", "permissions": ["llm"],
        "allowed_agents": ["chat_agent"], "sha256": hashlib.sha256(instructions.encode()).hexdigest(),
    }), encoding="utf-8")
    registry = SkillRegistry(tmp_path)
    tools = ToolRegistry()
    assert registry.register_tools(tools) == []
    assert "回答前先列出假设" in registry.instruction_context()
    with pytest.raises(KeyError):
        registry.execute("openai_demo", {})


def test_github_instruction_catalog_installs_per_user(tmp_path: Path, monkeypatch) -> None:
    from agent_platform.core import skill_marketplace as marketplace_module

    marketplace_module._GITHUB_CACHE.clear()
    repo = "openai/skills"
    commit = "1" * 40
    skill_md = b"---\nname: demo\ndescription: Official demo workflow\n---\nUse a checklist.\n"
    blob_sha = hashlib.sha1(f"blob {len(skill_md)}\0".encode() + skill_md).hexdigest()

    def fake_read(url: str, **_kwargs) -> bytes:
        if url.endswith("/commits/main"):
            return json.dumps({"sha": commit}).encode()
        if "/git/trees/" in url:
            return json.dumps({"tree": [{
                "path": "skills/.curated/demo/SKILL.md", "type": "blob", "sha": blob_sha,
            }]}).encode()
        if url.endswith("/skills/.curated/demo/SKILL.md"):
            return skill_md
        raise AssertionError(url)

    monkeypatch.setattr(SkillMarketplace, "_read_url", staticmethod(fake_read))
    catalog = tmp_path / "catalog.json"
    catalog.write_text("[]", encoding="utf-8")
    marketplace = SkillMarketplace(catalog, tmp_path / "users", github_sources=(f"{repo}|skills/.curated",))
    items = marketplace.user_items("user-a")
    assert len(items) == 1
    assert items[0]["kind"] == "instruction"
    assert items[0]["sha256"] == ""
    assert items[0]["integrity"] == f"git:{blob_sha}"
    marketplace.install(items[0]["id"], "user-a")
    assert (tmp_path / "users" / "user-a" / "openai_demo" / "SKILL.md").is_file()
    assert marketplace.user_items("user-b")[0]["installed"] is False
    assert "Use a checklist" in SkillRegistry(tmp_path / "users" / "user-a").instruction_context()


def test_github_instruction_install_rechecks_sha256(tmp_path: Path, monkeypatch) -> None:
    from agent_platform.core import skill_marketplace as marketplace_module

    marketplace_module._GITHUB_CACHE.clear()
    commit = "2" * 40
    original = b"---\nname: demo\ndescription: Demo\n---\nOriginal\n"
    changed = b"---\nname: demo\ndescription: Demo\n---\nChanged\n"
    blob_sha = hashlib.sha1(f"blob {len(original)}\0".encode() + original).hexdigest()

    def fake_read(url: str, **_kwargs) -> bytes:
        if url.endswith("/commits/main"):
            return json.dumps({"sha": commit}).encode()
        if "/git/trees/" in url:
            return json.dumps({"tree": [{
                "path": "skills/.curated/demo/SKILL.md", "type": "blob", "sha": blob_sha,
            }]}).encode()
        return changed

    monkeypatch.setattr(SkillMarketplace, "_read_url", staticmethod(fake_read))
    catalog = tmp_path / "catalog.json"
    catalog.write_text("[]", encoding="utf-8")
    marketplace = SkillMarketplace(catalog, tmp_path / "users", github_sources=("openai/skills|skills/.curated",))
    item = marketplace.user_items("user-a")[0]
    with pytest.raises(ValueError, match="完整性校验"):
        marketplace.install(item["id"], "user-a")
    assert not (tmp_path / "users" / "user-a" / "openai_demo").exists()
