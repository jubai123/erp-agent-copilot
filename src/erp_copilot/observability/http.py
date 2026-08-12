"""HTTP-level trace propagation middleware — task 6.3.

A pure-ASGI middleware that extracts an inbound W3C ``traceparent`` and wraps
the request in a span continuing that trace. Applied at the simulator and
gateway entry points so a request that already carries a ``traceparent`` (from
the API or a worker) is recorded in the same trace instead of starting a new
one.
"""

from __future__ import annotations

from starlette.types import ASGIApp, Receive, Scope, Send

from erp_copilot.observability.tracing import extract_trace_context, span


class TraceContextMiddleware:
    """Continue the inbound trace for each HTTP request.

    A pure-ASGI middleware (not Starlette's ``BaseHTTPMiddleware``) so the span
    stays current in the same contextvar across the ``await self.app(...)``
    call. ``BaseHTTPMiddleware`` runs the downstream app in a separate task
    where the span context would not propagate.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }
        inbound = extract_trace_context(headers)
        method = scope.get("method", "")
        target = scope.get("path", "")
        with span(
            f"http.{method}",
            attributes={"http.method": method, "http.target": target},
            context=inbound,
        ):
            await self.app(scope, receive, send)
