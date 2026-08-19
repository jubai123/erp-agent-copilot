"""Error-code taxonomy from the run_events audit log — task 7.6.

Every terminal failure appends a RUN_FAILED run_event whose payload carries a
machine-readable ``error_code`` (docs/03 section 9: PERMANENT_TOOL_ERROR,
DEADLINE_EXCEEDED, RECOVERY_RECONCILIATION_REQUIRED, ...). This script reads
those events and aggregates the distribution, so the Top-N tells the team
which failure class dominates and where to fix next.

The aggregation is a pure function (``aggregate_error_codes``) pinned by unit
tests; ``main`` only wires it to the database. Read-only: it never writes.
Like the other eval scripts it targets the dedicated test database, never the
development database.
"""

from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from collections.abc import Iterable
from typing import Any

from erp_copilot.domain.entities import RunEvent

RUN_FAILED = "RUN_FAILED"


def aggregate_error_codes(events: Iterable[RunEvent]) -> dict[str, int]:
    """Count ``error_code`` across RUN_FAILED events (others are ignored)."""
    counts: Counter[str] = Counter()
    for event in events:
        if event.event_type != RUN_FAILED:
            continue
        payload: Any = json.loads(event.payload or "{}")
        code = payload.get("error_code")
        if code:
            counts[code] += 1
    return dict(counts)


def top_n(counts: dict[str, int], n: int = 10) -> list[tuple[str, int]]:
    """The *n* most frequent error codes, most frequent first."""
    return sorted(counts.items(), key=lambda item: item[1], reverse=True)[:n]


def main(url: str | None = None, n: int = 10) -> None:
    """Connect to the test database and print the error-code Top-N."""
    from erp_copilot.domain import entities as _entities  # noqa: F401  (register metadata)
    from erp_copilot.infrastructure.config import Settings
    from erp_copilot.infrastructure.database import Base, get_engine, get_session, init_db

    settings = Settings(
        database_url=url
        or os.getenv(
            "TEST_DATABASE_URL",
            "postgresql://copilot:copilot_dev@localhost:5432/erp_copilot_test",
        ),
        llm_api_key="sk-test",
    )
    init_db(settings)
    Base.metadata.create_all(get_engine())

    session = get_session()
    try:
        events = session.query(RunEvent).filter(RunEvent.event_type == RUN_FAILED).all()
    finally:
        session.close()

    distribution = aggregate_error_codes(events)
    if not distribution:
        print(f"No {RUN_FAILED} events found — nothing to report")
        return
    total = sum(distribution.values())
    print(f"Error-code distribution ({len(distribution)} codes, {total} failures):")
    for code, count in top_n(distribution, n):
        print(f"  {count:>4}  {code}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--url", default=None, help="Test database URL (defaults to TEST_DATABASE_URL)"
    )
    parser.add_argument("--top", type=int, default=10, help="How many codes to print")
    args = parser.parse_args()
    main(url=args.url, n=args.top)
