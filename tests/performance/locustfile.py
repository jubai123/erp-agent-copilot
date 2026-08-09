"""Locust entry point — task 6.8.

Thin wrapper: the scenario logic (payload, path, request handling) lives in
``load_scenario.py`` so unit tests exercise it without importing Locust, whose
gevent monkey-patching of ssl breaks inside a pytest process. The plain
``from load_scenario import ...`` resolves because ``locust -f`` inserts the
locustfile's directory onto sys.path.

Prerequisites (Postgres + Redis up, API on :8000, tenant seeded) and the exact
headless command are documented in load_scenario.py's docstring.
"""

from __future__ import annotations

from load_scenario import DEFAULT_HOST, submit_run
from locust import HttpUser, between, task


class RunSubmissionUser(HttpUser):
    """A simulated user submitting Runs with a realistic think time."""

    host = DEFAULT_HOST
    wait_time = between(1, 3)

    @task
    def submit_run(self) -> None:
        submit_run(self.client)
