from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from agent_platform.config import Settings
from agent_platform.finance.sample_data_provider import SampleMarketDataProvider
from agent_platform.security import AuthenticationError, issue_token, verify_token
from agent_platform.services.application_service import ApplicationService
from agent_platform.storage.database_admin import backup_database, database_health


SECRET = "enterprise-test-secret-at-least-32-characters"


def _client(tmp_path, monkeypatch, *, registration_enabled: bool = False) -> TestClient:
    from agent_platform.api import main as main_mod

    provider = SampleMarketDataProvider()
    settings = Settings(
        sqlite_path=tmp_path / "security.sqlite3",
        sample_prices_csv=provider.csv_path,
        auth_enabled=True,
        auth_secret=SECRET,
        auth_registration_enabled=registration_enabled,
        langgraph_use_memory_saver=True,
    )
    service = ApplicationService(settings=settings, market_data=provider)
    monkeypatch.setattr(main_mod, "_app_service", service)
    main_mod.SecurityRateLimitMiddleware._windows.clear()
    return TestClient(main_mod.app)


def test_signed_token_rejects_tampering_and_expiry() -> None:
    token, _ = issue_token(
        user_id="u1", username="alice", role="user", secret=SECRET, ttl_s=60
    )
    assert verify_token(token, secret=SECRET).user_id == "u1"
    with pytest.raises(AuthenticationError):
        verify_token(token + "x", secret=SECRET)
    with pytest.raises(AuthenticationError):
        verify_token(token, secret=SECRET, now=int(time.time()) + 61)


def test_auth_required_and_first_registration_becomes_admin(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    assert client.get("/sessions").status_code == 401

    registered = client.post(
        "/auth/register", json={"username": "admin", "password": "strong-pass-123"}
    )
    assert registered.status_code == 200
    assert registered.json()["user"]["role"] == "admin"
    assert client.post(
        "/auth/register", json={"username": "other", "password": "strong-pass-456"}
    ).status_code == 403

    token = registered.json()["access_token"]
    assert client.get("/sessions", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    me = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.json()["username"] == "admin"
    assert me.json()["role"] == "admin"


def test_open_registration_creates_regular_user_after_admin(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch, registration_enabled=True)
    admin = client.post(
        "/auth/register", json={"username": "admin", "password": "strong-pass-123"}
    )
    user = client.post(
        "/auth/register",
        json={"username": "new-user", "password": "strong-pass-456", "email": "user@example.com"},
    )

    assert admin.status_code == 200
    assert admin.json()["user"]["role"] == "admin"
    assert user.status_code == 200
    assert user.json()["user"]["role"] == "user"
    assert user.json()["user"]["email"] == "user@example.com"
    assert client.post(
        "/auth/login", json={"username": "new-user", "password": "strong-pass-456"}
    ).status_code == 200


def test_admin_can_manage_public_user_profiles_but_user_cannot(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch, registration_enabled=True)
    admin = client.post(
        "/auth/register", json={"username": "admin", "password": "strong-pass-123", "email": "a@example.com"}
    ).json()
    user = client.post(
        "/auth/register", json={"username": "member", "password": "strong-pass-456", "email": "m@example.com"}
    ).json()
    admin_headers = {"Authorization": f"Bearer {admin['access_token']}"}
    user_headers = {"Authorization": f"Bearer {user['access_token']}"}

    response = client.get("/admin/users", headers=admin_headers)
    assert response.status_code == 200
    assert response.json()["total"] == 2
    assert all("password_hash" not in item and "salt" not in item for item in response.json()["users"])
    assert client.get("/admin/users", headers=user_headers).status_code == 403

    member_id = user["user"]["id"]
    changed = client.patch(
        f"/admin/users/{member_id}/role", json={"role": "admin"}, headers=admin_headers
    )
    assert changed.status_code == 200
    assert changed.json()["role"] == "admin"


def test_admin_cannot_remove_last_admin(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    admin = client.post(
        "/auth/register", json={"username": "admin", "password": "strong-pass-123"}
    ).json()
    admin_id = admin["user"]["id"]
    response = client.patch(
        f"/admin/users/{admin_id}/role", json={"role": "user"},
        headers={"Authorization": f"Bearer {admin['access_token']}"},
    )
    assert response.status_code == 409


def test_regular_user_can_read_but_cannot_reset_observability(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch, registration_enabled=True)
    client.post(
        "/auth/register", json={"username": "admin", "password": "strong-pass-123"}
    )
    user = client.post(
        "/auth/register", json={"username": "member", "password": "strong-pass-456"}
    ).json()
    headers = {"Authorization": f"Bearer {user['access_token']}"}

    response = client.get("/observability", headers=headers)
    assert response.status_code == 200
    assert "per_agent" in response.json()
    assert client.delete("/observability", headers=headers).status_code == 403


def test_user_can_change_own_password_and_old_password_stops_working(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    registered = client.post(
        "/auth/register", json={"username": "admin", "password": "strong-pass-123"}
    ).json()
    headers = {"Authorization": f"Bearer {registered['access_token']}"}

    wrong = client.patch(
        "/auth/password",
        json={"current_password": "wrong-pass-123", "new_password": "new-strong-pass-789"},
        headers=headers,
    )
    assert wrong.status_code == 400
    assert client.post(
        "/auth/login", json={"username": "admin", "password": "strong-pass-123"}
    ).status_code == 200

    changed = client.patch(
        "/auth/password",
        json={"current_password": "strong-pass-123", "new_password": "new-strong-pass-789"},
        headers=headers,
    )
    assert changed.status_code == 200
    assert client.post(
        "/auth/login", json={"username": "admin", "password": "strong-pass-123"}
    ).status_code == 401
    assert client.post(
        "/auth/login", json={"username": "admin", "password": "new-strong-pass-789"}
    ).status_code == 200


def test_password_change_requires_eight_characters(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    registered = client.post(
        "/auth/register", json={"username": "admin", "password": "strong-pass-123"}
    ).json()
    response = client.patch(
        "/auth/password",
        json={"current_password": "strong-pass-123", "new_password": "short7"},
        headers={"Authorization": f"Bearer {registered['access_token']}"},
    )
    assert response.status_code == 422


def test_frontend_entry_is_not_cached(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    response = client.get("/")
    assert response.headers["cache-control"].startswith("no-store")
    assert response.headers["pragma"] == "no-cache"
    assert response.headers["expires"] == "0"


def test_frontend_registration_contract() -> None:
    from pathlib import Path

    html = Path("frontend_prototype.html").read_text(encoding="utf-8")
    for element_id in (
        "reg-username", "reg-email", "reg-password", "reg-confirm-password",
        "register-submit-btn", "register-error",
    ):
        assert f'id="{element_id}"' in html
    assert 'onclick="doRegister()"' in html
    assert "async function doRegister()" in html
    assert "`${API_BASE}/auth/register`" in html
    assert 'id="user-role"' in html
    assert "`${API_BASE}/auth/me`" in html
    assert 'id="admin-users-menu-btn"' in html
    assert 'id="nav-admin-users"' not in html
    assert "${API_BASE}/admin/users" in html
    assert "async function openAdminUsersModal()" in html
    assert 'id="obs-reset-btn"' in html
    assert "`${API_BASE}/auth/password`" in html
    assert "新密码（至少8位）" in html


def test_user_cannot_read_another_users_session(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    service = __import__("agent_platform.api.main", fromlist=["get_application_service"]).get_application_service()
    alice = service.store.create_user("alice", "strong-pass-123")
    bob = service.store.create_user("bobby", "strong-pass-456")
    alice_token, _ = issue_token(
        user_id=alice.id, username=alice.username, role=alice.role, secret=SECRET, ttl_s=3600
    )
    bob_token, _ = issue_token(
        user_id=bob.id, username=bob.username, role=bob.role, secret=SECRET, ttl_s=3600
    )
    alice_headers = {"Authorization": f"Bearer {alice_token}"}
    bob_headers = {"Authorization": f"Bearer {bob_token}"}

    session_id = client.post("/sessions", json={"title": "private"}, headers=alice_headers).json()["id"]
    assert client.get(f"/sessions/{session_id}/messages", headers=bob_headers).status_code == 404
    assert client.get(f"/sessions/{session_id}/messages", headers=alice_headers).status_code == 200


def test_security_headers_readiness_and_database_backup(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    health = client.get("/health")
    ready = client.get("/ready")

    assert health.headers["x-content-type-options"] == "nosniff"
    assert health.headers["x-frame-options"] == "DENY"
    assert health.headers["x-request-id"]
    assert ready.status_code == 200
    assert ready.json()["checks"]["database"]["integrity"] == "ok"

    source = tmp_path / "security.sqlite3"
    destination = tmp_path / "backup.sqlite3"
    result = backup_database(source, destination)
    assert result["size_bytes"] > 0
    assert database_health(destination)["integrity"] == "ok"


def test_production_requires_authentication(monkeypatch) -> None:
    from agent_platform.config import get_settings

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("AUTH_ENABLED", "false")
    with pytest.raises(RuntimeError, match="AUTH_ENABLED"):
        get_settings()


def test_login_rate_limit_returns_429(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    responses = [
        client.post("/auth/login", json={"username": "missing", "password": "wrong-pass-123"})
        for _ in range(11)
    ]
    assert responses[-1].status_code == 429
    assert int(responses[-1].headers["retry-after"]) > 0
