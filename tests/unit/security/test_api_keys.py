"""Unit tests for API-key issuance, hashing, and verification (auth hardening).

The verification path queries the ``api_keys`` table, so these tests run
against the dedicated test database (same pattern as other entity tests).
"""

from __future__ import annotations

from datetime import UTC, datetime
from itertools import count

import pytest
from sqlalchemy import text

from erp_copilot.domain.entities import ApiKey, Tenant
from erp_copilot.infrastructure.config import Settings
from erp_copilot.infrastructure.database import Base, get_engine, get_session, init_db
from erp_copilot.security import api_keys


@pytest.fixture(scope="module")
def _init_db(ensure_test_database: str) -> None:
    import erp_copilot.domain.entities  # noqa: F401 - register models on Base

    settings = Settings(
        database_url=ensure_test_database,
        llm_api_key="sk-test",
    )
    init_db(settings)
    Base.metadata.create_all(get_engine())


@pytest.fixture(autouse=True)
def _clean_tables(_init_db: None) -> None:
    session = get_session()
    for table in reversed(Base.metadata.sorted_tables):
        session.execute(text(f"DELETE FROM {table.name} CASCADE"))
    session.commit()


_seed_slugs = count()


def _seed_tenant() -> str:
    session = get_session()
    tenant = Tenant(name="Acme Corp", slug=f"acme-corp-{next(_seed_slugs)}")
    session.add(tenant)
    session.flush()
    tenant_id = tenant.id
    session.commit()
    return tenant_id


class TestGenerateApiKey:
    def test_has_prefix_and_sufficient_entropy(self) -> None:
        key = api_keys.generate_api_key()
        assert key.startswith(api_keys.API_KEY_PREFIX)
        assert len(key) > len(api_keys.API_KEY_PREFIX) + 32

    def test_unique_across_calls(self) -> None:
        assert api_keys.generate_api_key() != api_keys.generate_api_key()


class TestHashApiKey:
    def test_deterministic_with_same_pepper(self) -> None:
        key = "erp_live_abc"
        assert api_keys.hash_api_key(key, pepper="p1") == api_keys.hash_api_key(key, pepper="p1")

    def test_differs_with_pepper(self) -> None:
        key = "erp_live_abc"
        assert api_keys.hash_api_key(key, pepper="p1") != api_keys.hash_api_key(key, pepper="p2")


class TestApiKeyRoundTrip:
    def test_create_and_verify(self) -> None:
        tenant_id = _seed_tenant()
        session = get_session()
        plain = api_keys.create_api_key(session, tenant_id=tenant_id, user_id="user-1", label="ci")

        found = api_keys.verify_api_key(session, plain, pepper="")
        assert found is not None
        assert found.tenant_id == tenant_id
        assert found.user_id == "user-1"

    def test_plaintext_not_persisted(self) -> None:
        tenant_id = _seed_tenant()
        session = get_session()
        plain = api_keys.create_api_key(session, tenant_id=tenant_id, user_id="user-1", label="ci")

        row = session.query(ApiKey).one()
        assert row.key_hash != plain
        assert plain not in row.key_hash

    def test_verify_rejects_unknown_key(self) -> None:
        session = get_session()
        assert api_keys.verify_api_key(session, "erp_live_doesnotexist", pepper="") is None

    def test_verify_rejects_revoked_key(self) -> None:
        tenant_id = _seed_tenant()
        session = get_session()
        plain = api_keys.create_api_key(session, tenant_id=tenant_id, user_id="user-1", label="ci")

        row = session.query(ApiKey).one()
        row.revoked_at = datetime.now(UTC)
        session.commit()

        assert api_keys.verify_api_key(session, plain, pepper="") is None


class TestListApiKeys:
    def test_lists_only_requested_tenant(self) -> None:
        tenant_a = _seed_tenant()
        tenant_b = _seed_tenant()
        session = get_session()
        api_keys.create_api_key(session, tenant_id=tenant_a, user_id="u-a", label="a")
        api_keys.create_api_key(session, tenant_id=tenant_b, user_id="u-b", label="b")

        keys = api_keys.list_api_keys(session, tenant_a)

        assert [k.tenant_id for k in keys] == [tenant_a]
        assert keys[0].user_id == "u-a"

    def test_includes_revoked_keys_with_revoked_at(self) -> None:
        tenant_id = _seed_tenant()
        session = get_session()
        api_keys.create_api_key(session, tenant_id=tenant_id, user_id="u-1", label="a")
        api_keys.create_api_key(session, tenant_id=tenant_id, user_id="u-1", label="b")

        row = session.query(ApiKey).filter_by(label="a").one()
        row.revoked_at = datetime.now(UTC)
        session.commit()

        keys = api_keys.list_api_keys(session, tenant_id)

        by_label = {k.label: k for k in keys}
        assert by_label["a"].revoked_at is not None
        assert by_label["b"].revoked_at is None


class TestRevokeApiKey:
    def test_revoke_sets_revoked_at(self) -> None:
        tenant_id = _seed_tenant()
        session = get_session()
        plain = api_keys.create_api_key(session, tenant_id=tenant_id, user_id="u-1", label="ci")

        row = session.query(ApiKey).one()
        revoked = api_keys.revoke_api_key(session, row.id, tenant_id)

        assert revoked is not None
        assert revoked.revoked_at is not None
        assert api_keys.verify_api_key(session, plain, pepper="") is None

    def test_revoke_is_idempotent(self) -> None:
        tenant_id = _seed_tenant()
        session = get_session()
        api_keys.create_api_key(session, tenant_id=tenant_id, user_id="u-1", label="ci")

        row = session.query(ApiKey).one()
        first = api_keys.revoke_api_key(session, row.id, tenant_id)
        second = api_keys.revoke_api_key(session, row.id, tenant_id)

        assert first.revoked_at is not None
        assert second.revoked_at == first.revoked_at

    def test_revoke_scoped_to_tenant(self) -> None:
        tenant_a = _seed_tenant()
        tenant_b = _seed_tenant()
        session = get_session()
        plain = api_keys.create_api_key(session, tenant_id=tenant_a, user_id="u-1", label="ci")

        row = session.query(ApiKey).one()

        assert api_keys.revoke_api_key(session, row.id, tenant_b) is None
        assert api_keys.verify_api_key(session, plain, pepper="") is not None

    def test_revoke_unknown_key_returns_none(self) -> None:
        tenant_id = _seed_tenant()
        session = get_session()

        assert api_keys.revoke_api_key(session, 999999, tenant_id) is None
