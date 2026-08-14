"""API-key credential issuance and verification (API authentication).

Keys are high-entropy random strings issued exactly once in plaintext; the
database stores only an HMAC-SHA256 digest keyed by a server-side pepper, so a
database leak does not expose usable credentials and offline brute-force still
requires the pepper. Verification is a direct digest lookup (unique index) and
rejects revoked keys.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from erp_copilot.domain.entities import ApiKey

API_KEY_PREFIX = "erp_live_"


def generate_api_key() -> str:
    """Return a fresh high-entropy plaintext API key (displayed once)."""
    return f"{API_KEY_PREFIX}{secrets.token_urlsafe(32)}"


def hash_api_key(plain_key: str, pepper: str = "") -> str:
    """Return the storage digest for *plain_key*.

    HMAC-SHA256 keyed by *pepper*: the pepper is server-side only, so a dumped
    ``key_hash`` column cannot be brute-forced without also knowing it.
    """
    return hmac.new(pepper.encode(), plain_key.encode(), hashlib.sha256).hexdigest()


def create_api_key(
    session: Session,
    *,
    tenant_id: str,
    user_id: str,
    label: str = "",
    pepper: str = "",
) -> str:
    """Issue a new key bound to (tenant, user); return the plaintext once."""
    plain = generate_api_key()
    session.add(
        ApiKey(
            tenant_id=tenant_id,
            user_id=user_id,
            key_hash=hash_api_key(plain, pepper),
            label=label,
        )
    )
    session.commit()
    return plain


def verify_api_key(session: Session, plain_key: str, pepper: str = "") -> ApiKey | None:
    """Return the ApiKey record for *plain_key*, or None when unknown/revoked.

    Also bumps ``last_used_at`` on success for auditability.
    """
    if not plain_key:
        return None
    digest = hash_api_key(plain_key, pepper)
    key = session.query(ApiKey).filter_by(key_hash=digest).first()
    if key is None or key.revoked_at is not None:
        return None
    key.last_used_at = datetime.now(UTC)
    session.commit()
    return key


def list_api_keys(session: Session, tenant_id: str) -> list[ApiKey]:
    """Return every API key bound to *tenant_id* (revoked ones included, for audit)."""
    return session.query(ApiKey).filter_by(tenant_id=tenant_id).order_by(ApiKey.id).all()


def revoke_api_key(session: Session, key_id: int, tenant_id: str) -> ApiKey | None:
    """Revoke the key iff it belongs to *tenant_id*; idempotent, None if unknown.

    Already-revoked keys keep their original ``revoked_at`` so repeated
    deletions do not churn the audit timestamp.
    """
    key = session.query(ApiKey).filter_by(id=key_id, tenant_id=tenant_id).first()
    if key is None:
        return None
    if key.revoked_at is None:
        key.revoked_at = datetime.now(UTC)
        session.commit()
    return key
