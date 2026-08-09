"""Load-test scenario logic — task 6.8.

Pure and importable without Locust: the scenario core (URL path, payload
builder, and the :func:`submit_run` request handler) lives here so unit tests
exercise it deterministically with a fake HTTP client. ``locustfile.py`` is a
thin Locust wrapper around :func:`submit_run`.

Run the harness headless (prerequisites below)::

    uv run locust -f tests/performance/locustfile.py \\
        --headless -u 50 -r 5 -t 60s \\
        --html tests/performance/reports/locust_report.html \\
        --host http://localhost:8000

Prerequisites (see infra/docker-compose.yml):

1. Postgres + Redis up and the API running on port 8000.
2. A tenant row for the FK on ``runs.tenant_id`` (deterministic id, idempotent)::

       INSERT INTO tenants (id, name, slug)
       VALUES ('loadtest-tenant', 'Load Test', 'load-test')
       ON CONFLICT (slug) DO NOTHING;

The tenant id is overridable via the ``LOAD_TEST_TENANT_ID`` environment
variable. The submit path makes no LLM call (docs/08 §9's "fixed Mock LLM"
requirement is naturally satisfied — only the ERP simulator runs).
"""

from __future__ import annotations

import os
from typing import Protocol

DEFAULT_HOST = "http://localhost:8000"
RUN_PATH = "/v1/runs"
DEFAULT_TENANT_ID = "loadtest-tenant"


def build_run_payload(tenant_id: str | None = None, *, title: str = "load-test") -> dict[str, str]:
    """Build the POST /v1/runs body; tenant falls back to env then default."""
    return {
        "tenant_id": tenant_id or os.environ.get("LOAD_TEST_TENANT_ID", DEFAULT_TENANT_ID),
        "title": title,
        "product_name": "苹果",
    }


class _CatchableResponse(Protocol):
    """The part of a Locust catch_response context we rely on."""

    status_code: int

    def failure(self, message: str) -> None: ...

    def __enter__(self) -> _CatchableResponse: ...

    def __exit__(self, *exc: object) -> bool: ...


class _Client(Protocol):
    """The part of a Locust HttpSession we rely on."""

    def post(
        self,
        path: str,
        *,
        json: dict[str, str] | None = None,
        name: str | None = None,
        catch_response: bool = False,
    ) -> _CatchableResponse: ...


def submit_run(client: _Client) -> None:
    """POST a run-submission payload; flag non-202 responses as failures."""
    payload = build_run_payload()
    with client.post(RUN_PATH, json=payload, name="POST /v1/runs", catch_response=True) as resp:
        if resp.status_code != 202:
            resp.failure(f"expected 202 Accepted, got {resp.status_code}")
