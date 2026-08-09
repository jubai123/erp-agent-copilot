"""Unit tests for the Locust load-test scenario — task 6.8.

The scenario core (``tests.performance.load_scenario``) is exercised offline
with a fake HTTP client: no live API, no Postgres, no Redis, no network.
Locust itself is deliberately NOT imported here — its gevent monkey-patching of
ssl breaks inside a pytest process — so the testable logic lives outside the
``locustfile.py`` wrapper.
"""

from __future__ import annotations

from tests.performance.load_scenario import (
    DEFAULT_HOST,
    DEFAULT_TENANT_ID,
    RUN_PATH,
    build_run_payload,
    submit_run,
)


class _FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        self._failed: str | None = None

    def failure(self, message: str) -> None:
        self._failed = message

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class _FakeClient:
    def __init__(self, status_code: int = 202) -> None:
        self.status_code = status_code
        self.calls: list[tuple[str, dict[str, str], str]] = []
        self.last_response: _FakeResponse | None = None

    def post(
        self,
        path: str,
        *,
        json: dict[str, str] | None = None,
        name: str | None = None,
        catch_response: bool = False,
    ) -> _FakeResponse:
        self.calls.append((path, json or {}, name or ""))
        self.last_response = _FakeResponse(self.status_code)
        return self.last_response


class TestConstants:
    def test_run_path(self) -> None:
        assert RUN_PATH == "/v1/runs"

    def test_default_host_is_http_url(self) -> None:
        assert DEFAULT_HOST.startswith("http://")

    def test_default_tenant_id_fits_tenant_column(self) -> None:
        # Tenant.id is String(36); the default must fit and be non-empty.
        assert 1 <= len(DEFAULT_TENANT_ID) <= 36


class TestBuildRunPayload:
    def test_default_payload(self) -> None:
        assert build_run_payload() == {
            "tenant_id": DEFAULT_TENANT_ID,
            "title": "load-test",
            "product_name": "苹果",
        }

    def test_custom_tenant(self) -> None:
        assert build_run_payload("custom-tenant")["tenant_id"] == "custom-tenant"

    def test_tenant_from_env(self, monkeypatch) -> None:
        monkeypatch.setenv("LOAD_TEST_TENANT_ID", "env-tenant")
        assert build_run_payload()["tenant_id"] == "env-tenant"

    def test_tenant_falls_back_to_default(self, monkeypatch) -> None:
        monkeypatch.delenv("LOAD_TEST_TENANT_ID", raising=False)
        assert build_run_payload()["tenant_id"] == DEFAULT_TENANT_ID

    def test_custom_title(self) -> None:
        assert build_run_payload(title="special")["title"] == "special"


class TestSubmitRun:
    def test_posts_to_runs_endpoint(self) -> None:
        client = _FakeClient()
        submit_run(client)
        assert len(client.calls) == 1
        path, payload, name = client.calls[0]
        assert path == RUN_PATH
        assert name == "POST /v1/runs"
        assert payload["product_name"] == "苹果"

    def test_202_is_success(self) -> None:
        client = _FakeClient(status_code=202)
        submit_run(client)
        assert client.last_response is not None
        assert client.last_response._failed is None

    def test_non_202_flags_failure(self) -> None:
        client = _FakeClient(status_code=500)
        submit_run(client)
        assert client.last_response is not None
        assert client.last_response._failed is not None
        assert "500" in client.last_response._failed
