"""Service-to-service auth for the ERP Simulator (X-Simulator-Token).

The simulator is a deterministic test double for a real ERP backend. When a
deployment exposes it directly on a network, callers must present the shared
secret configured via ``SIMULATOR_SHARED_SECRET``. The gate is fail-closed:
with a secret configured, every non-``/health`` route rejects requests that
omit or mismatch the header (constant-time compare). With no secret the gate
is disabled, keeping the simulator usable as an open in-process test double.
"""

from __future__ import annotations

import hmac
from collections.abc import Callable
from typing import Annotated

from fastapi import Header, HTTPException

SIMULATOR_TOKEN_HEADER = "X-Simulator-Token"


def require_simulator_token(secret: str) -> Callable[..., None]:
    """Return a FastAPI dependency requiring *secret* in the token header."""

    def _guard(
        token: Annotated[str | None, Header(alias=SIMULATOR_TOKEN_HEADER)] = None,
    ) -> None:
        if token is None or not hmac.compare_digest(token, secret):
            raise HTTPException(
                status_code=401,
                detail=f"Invalid or missing {SIMULATOR_TOKEN_HEADER} header",
            )

    return _guard
