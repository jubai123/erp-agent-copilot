"""API-key management routes: issue, list, and revoke long-lived credentials.

docs/06 §4: credentials are the highest-sensitivity resource — minting or
revoking a key grants/removes the ability to authenticate as a user — so the
endpoints require the dedicated ``admin:apikey`` scope rather than reusing
``admin:tool``. The tenant always comes from the authenticated identity
(docs/07 §10); the body may only name the user the key authenticates as.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response

from apps.api.schemas.api_keys import (
    ApiKeyCreatedResponse,
    ApiKeySummaryResponse,
    CreateApiKeyRequest,
)
from erp_copilot.domain.entities import ApiKey
from erp_copilot.infrastructure.database import get_session
from erp_copilot.security import api_keys
from erp_copilot.security.dependencies import Actor, _settings, require_scope

router = APIRouter(prefix="/v1/api-keys", tags=["api-keys"])

_REQUIRED_SCOPE = require_scope("admin:apikey")


@router.post("", response_model=ApiKeyCreatedResponse, status_code=201)
async def create_api_key_endpoint(
    body: CreateApiKeyRequest,
    actor: Actor = Depends(_REQUIRED_SCOPE),
) -> ApiKeyCreatedResponse:
    """Issue a new key bound to (actor's tenant, body user); plaintext once.

    The key binds to the caller's tenant — a client can never mint a key in a
    tenant it does not authenticate into. ``id``/``created_at`` are read back
    from the row the issuance just committed.
    """
    settings = _settings()
    session = get_session()
    try:
        plain = api_keys.create_api_key(
            session,
            tenant_id=actor.tenant_id,
            user_id=body.user_id,
            label=body.label,
            pepper=settings.api_key_pepper,
        )
        row = (
            session.query(ApiKey)
            .filter_by(key_hash=api_keys.hash_api_key(plain, settings.api_key_pepper))
            .one()
        )
    finally:
        session.close()

    return ApiKeyCreatedResponse(
        id=row.id,
        tenant_id=row.tenant_id,
        user_id=row.user_id,
        label=row.label,
        created_at=row.created_at,
        api_key=plain,
    )


@router.get("", response_model=list[ApiKeySummaryResponse])
async def list_api_keys_endpoint(
    actor: Actor = Depends(_REQUIRED_SCOPE),
) -> list[ApiKeySummaryResponse]:
    """List the actor's tenant keys (revoked included) with metadata only.

    The digest and the plaintext never leave the server — the list carries
    identity and audit timestamps, so a compromised read cannot be replayed
    as a credential.
    """
    session = get_session()
    try:
        keys = api_keys.list_api_keys(session, actor.tenant_id)
    finally:
        session.close()

    return [
        ApiKeySummaryResponse(
            id=key.id,
            tenant_id=key.tenant_id,
            user_id=key.user_id,
            label=key.label,
            created_at=key.created_at,
            last_used_at=key.last_used_at,
            revoked_at=key.revoked_at,
        )
        for key in keys
    ]


@router.delete("/{key_id}", status_code=204)
async def revoke_api_key_endpoint(
    key_id: int,
    actor: Actor = Depends(_REQUIRED_SCOPE),
) -> Response:
    """Revoke a key in the actor's tenant; idempotent.

    A key that does not exist — or belongs to another tenant — returns 404 so
    the endpoint does not leak whether a given id exists elsewhere.
    """
    session = get_session()
    try:
        key = api_keys.revoke_api_key(session, key_id, actor.tenant_id)
    finally:
        session.close()

    if key is None:
        raise HTTPException(status_code=404, detail="API key not found in this tenant")
    return Response(status_code=204)
