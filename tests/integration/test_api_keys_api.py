"""Integration tests for the API-key management endpoints.

Key management is an admin operation guarded by the ``admin:apikey`` scope
(docs/06 §4). The tenant always comes from the authenticated identity, never
from the body (docs/07 §10). The plaintext key is returned exactly once on
creation; list responses carry metadata only, never the digest or plaintext.
"""

from __future__ import annotations

from itertools import count

from fastapi.testclient import TestClient

from erp_copilot.domain.entities import Role, RoleScope, Tenant, User, UserRole
from erp_copilot.infrastructure.database import get_session
from erp_copilot.security.api_keys import verify_api_key


def _create_tenant(name: str, slug: str) -> str:
    """Create a tenant in the database and return its ID."""
    session = get_session()
    try:
        tenant = Tenant(name=name, slug=slug)
        session.add(tenant)
        session.commit()
        return str(tenant.id)
    finally:
        session.close()


def _make_user(tenant_id: str, email: str, scopes: list[tuple[str, str]]) -> str:
    """Create an active user holding *scopes* in *tenant_id*; return its id."""
    session = get_session()
    try:
        role = Role(tenant_id=tenant_id, name=f"role-{email}")
        session.add(role)
        session.flush()
        for resource, action in scopes:
            session.add(RoleScope(role_id=role.id, resource=resource, action=action))
        user = User(tenant_id=tenant_id, email=email, hashed_password="x", is_active=True)
        session.add(user)
        session.flush()
        session.add(UserRole(user_id=user.id, role_id=role.id))
        session.commit()
        return user.id
    finally:
        session.close()


def _headers(tenant_id: str, user_id: str | None = None) -> dict[str, str]:
    headers = {"X-Tenant-ID": tenant_id}
    if user_id is not None:
        headers["X-User-ID"] = user_id
    return headers


def _admin_user(tenant_id: str) -> str:
    """Create a user holding the admin:apikey scope in *tenant_id*; return its id."""
    return _make_user(tenant_id, f"admin-{tenant_id}@example.com", [("admin", "apikey")])


_viewer_ids = count()


def _viewer_user(tenant_id: str) -> str:
    """Create a non-admin user (product:read only); return its id.

    Emails must be unique per tenant (uq_users_tenant_email), so a counter
    suffixes each one — two viewers may be created within one test.
    """
    return _make_user(tenant_id, f"viewer-{next(_viewer_ids)}@example.com", [("product", "read")])


def _create_key(
    client: TestClient, tenant_id: str, admin_id: str, user_id: str, label: str | None = "ci"
) -> dict:
    """Create a key via the API and return the JSON body.

    A ``None`` label is omitted from the body so the endpoint's default is
    exercised (the caller must not send a label to test the default).
    """
    body: dict[str, str] = {"user_id": user_id}
    if label is not None:
        body["label"] = label
    response = client.post("/v1/api-keys", json=body, headers=_headers(tenant_id, admin_id))
    assert response.status_code == 201, response.text
    return response.json()


class TestCreateApiKey:
    """POST /v1/api-keys issues a new key; plaintext returned exactly once."""

    def test_create_returns_plaintext_once_and_verifies(self) -> None:
        from apps.api.main import create_app

        tenant_id = _create_tenant("Key Acme", "key-acme")
        admin_id = _admin_user(tenant_id)
        target_id = _viewer_user(tenant_id)
        client = TestClient(create_app())

        data = _create_key(client, tenant_id, admin_id, target_id, label="ci-agent")

        assert data["api_key"].startswith("erp_live_")
        assert data["tenant_id"] == tenant_id
        assert data["user_id"] == target_id
        assert data["label"] == "ci-agent"
        assert "id" in data
        assert "created_at" in data

        # The plaintext must NOT be stored: the row holds only the digest.
        session = get_session()
        try:
            found = verify_api_key(session, data["api_key"], pepper="")
            assert found is not None
            assert found.tenant_id == tenant_id
            assert found.user_id == target_id
        finally:
            session.close()

    def test_create_missing_tenant_header_is_401(self) -> None:
        from apps.api.main import create_app

        client = TestClient(create_app())

        response = client.post("/v1/api-keys", json={"user_id": "u", "label": "x"})

        assert response.status_code == 401

    def test_create_without_admin_apikey_scope_is_403(self) -> None:
        from apps.api.main import create_app

        tenant_id = _create_tenant("Key NoAdmin", "key-noadmin")
        viewer_id = _viewer_user(tenant_id)
        target_id = _viewer_user(tenant_id)
        client = TestClient(create_app())

        response = client.post(
            "/v1/api-keys",
            json={"user_id": target_id, "label": "ci"},
            headers=_headers(tenant_id, viewer_id),
        )

        assert response.status_code == 403

    def test_create_rejects_missing_user_id(self) -> None:
        from apps.api.main import create_app

        tenant_id = _create_tenant("Key NoUser", "key-nouser")
        admin_id = _admin_user(tenant_id)
        client = TestClient(create_app())

        response = client.post(
            "/v1/api-keys", json={"label": "ci"}, headers=_headers(tenant_id, admin_id)
        )

        assert response.status_code == 422

    def test_create_rejects_oversized_label(self) -> None:
        from apps.api.main import create_app

        tenant_id = _create_tenant("Key BigLabel", "key-biglabel")
        admin_id = _admin_user(tenant_id)
        target_id = _viewer_user(tenant_id)
        client = TestClient(create_app())

        response = client.post(
            "/v1/api-keys",
            json={"user_id": target_id, "label": "x" * 256},
            headers=_headers(tenant_id, admin_id),
        )

        assert response.status_code == 422

    def test_create_label_defaults_to_empty(self) -> None:
        from apps.api.main import create_app

        tenant_id = _create_tenant("Key NoLabel", "key-nolabel")
        admin_id = _admin_user(tenant_id)
        target_id = _viewer_user(tenant_id)
        client = TestClient(create_app())

        data = _create_key(client, tenant_id, admin_id, target_id, label=None)

        assert data["label"] == ""


class TestListApiKeys:
    """GET /v1/api-keys returns tenant key metadata, never secrets."""

    def test_list_returns_metadata_without_secrets(self) -> None:
        from apps.api.main import create_app

        tenant_id = _create_tenant("Key List", "key-list")
        admin_id = _admin_user(tenant_id)
        target_id = _viewer_user(tenant_id)
        client = TestClient(create_app())

        first = _create_key(client, tenant_id, admin_id, target_id, label="a")
        _create_key(client, tenant_id, admin_id, target_id, label="b")

        response = client.get("/v1/api-keys", headers=_headers(tenant_id, admin_id))

        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        assert len(data) == 2
        labels = {item["label"] for item in data}
        assert labels == {"a", "b"}
        assert all(item["user_id"] == target_id for item in data)
        # Never expose the digest or plaintext through the list.
        for item in data:
            assert "api_key" not in item
            assert "key_hash" not in item
            assert "created_at" in item
            assert "id" in item
        assert first["id"] in {item["id"] for item in data}

    def test_list_empty_tenant(self) -> None:
        from apps.api.main import create_app

        tenant_id = _create_tenant("Key Empty", "key-empty")
        admin_id = _admin_user(tenant_id)
        client = TestClient(create_app())

        response = client.get("/v1/api-keys", headers=_headers(tenant_id, admin_id))

        assert response.status_code == 200
        assert response.json() == []

    def test_list_missing_tenant_header_is_401(self) -> None:
        from apps.api.main import create_app

        client = TestClient(create_app())

        response = client.get("/v1/api-keys")

        assert response.status_code == 401

    def test_list_without_admin_apikey_scope_is_403(self) -> None:
        from apps.api.main import create_app

        tenant_id = _create_tenant("Key ListNoAdmin", "key-listnoadmin")
        viewer_id = _viewer_user(tenant_id)
        client = TestClient(create_app())

        response = client.get("/v1/api-keys", headers=_headers(tenant_id, viewer_id))

        assert response.status_code == 403


class TestRevokeApiKey:
    """DELETE /v1/api-keys/{key_id} revokes a key; idempotent and tenant-scoped."""

    def test_revoke_makes_key_invalid(self) -> None:
        from apps.api.main import create_app

        tenant_id = _create_tenant("Key Revoke", "key-revoke")
        admin_id = _admin_user(tenant_id)
        target_id = _viewer_user(tenant_id)
        client = TestClient(create_app())

        data = _create_key(client, tenant_id, admin_id, target_id)

        response = client.delete(
            f"/v1/api-keys/{data['id']}", headers=_headers(tenant_id, admin_id)
        )

        assert response.status_code == 204

        session = get_session()
        try:
            assert verify_api_key(session, data["api_key"], pepper="") is None
        finally:
            session.close()

    def test_revoke_is_idempotent(self) -> None:
        from apps.api.main import create_app

        tenant_id = _create_tenant("Key Idempotent", "key-idempotent")
        admin_id = _admin_user(tenant_id)
        target_id = _viewer_user(tenant_id)
        client = TestClient(create_app())

        data = _create_key(client, tenant_id, admin_id, target_id)
        headers = _headers(tenant_id, admin_id)

        assert client.delete(f"/v1/api-keys/{data['id']}", headers=headers).status_code == 204
        assert client.delete(f"/v1/api-keys/{data['id']}", headers=headers).status_code == 204

    def test_revoke_cross_tenant_is_404(self) -> None:
        from apps.api.main import create_app

        tenant_a = _create_tenant("Key TenantA", "key-tenanta")
        admin_a = _admin_user(tenant_a)
        tenant_b = _create_tenant("Key TenantB", "key-tenantb")
        admin_b = _admin_user(tenant_b)
        target_b = _viewer_user(tenant_b)
        client = TestClient(create_app())

        key_b = _create_key(client, tenant_b, admin_b, target_b)

        response = client.delete(f"/v1/api-keys/{key_b['id']}", headers=_headers(tenant_a, admin_a))

        assert response.status_code == 404

        # The key in tenant B is still valid.
        session = get_session()
        try:
            assert verify_api_key(session, key_b["api_key"], pepper="") is not None
        finally:
            session.close()

    def test_revoke_unknown_key_is_404(self) -> None:
        from apps.api.main import create_app

        tenant_id = _create_tenant("Key Unknown", "key-unknown")
        admin_id = _admin_user(tenant_id)
        client = TestClient(create_app())

        response = client.delete("/v1/api-keys/999999", headers=_headers(tenant_id, admin_id))

        assert response.status_code == 404

    def test_revoke_without_admin_apikey_scope_is_403(self) -> None:
        from apps.api.main import create_app

        tenant_id = _create_tenant("Key RevokeNoAdmin", "key-revokenoadmin")
        admin_id = _admin_user(tenant_id)
        viewer_id = _viewer_user(tenant_id)
        target_id = _viewer_user(tenant_id)
        client = TestClient(create_app())

        data = _create_key(client, tenant_id, admin_id, target_id)

        response = client.delete(
            f"/v1/api-keys/{data['id']}", headers=_headers(tenant_id, viewer_id)
        )

        assert response.status_code == 403
