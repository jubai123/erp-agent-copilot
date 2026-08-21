"""Unit tests for the order status state machine.

apps/erp_simulator/data/orders enforces rules.yaml order-state-machine:
CREATED → CONFIRMED → SHIPPED → DELIVERED, with CREATED/CONFIRMED/SHIPPED →
CANCELLED. Terminal states (DELIVERED/CANCELLED) accept no transition, rollback
to CREATED and skip-level jumps are rejected, and both writes are at-most-once
per idempotency_key (a replay returns the recorded order without re-applying).
"""

from __future__ import annotations

import uuid

import pytest

from apps.erp_simulator.data.orders import (
    cancel_order,
    can_transition,
    create_order,
    update_order_status,
)

# The shortest legal path to each non-CREATED state, for building fixtures.
_PATH_TO: dict[str, list[str]] = {
    "CONFIRMED": ["CONFIRMED"],
    "SHIPPED": ["CONFIRMED", "SHIPPED"],
    "DELIVERED": ["CONFIRMED", "SHIPPED", "DELIVERED"],
}


def _new_order_id() -> str:
    """Create an order in CREATED and return its order_id (the store is shared)."""
    order = create_order(
        product_id=1,
        product_name="苹果",
        quantity=1,
        supplier_id=3,
        region="上海",
        unit_price=10.0,
        idempotency_key=f"state-test-{uuid.uuid4().hex}",
    )
    return order.order_id


def _order_in(status: str) -> str:
    """Create an order and walk it to *status* via legal transitions."""
    order_id = _new_order_id()
    for target in _PATH_TO.get(status, []):
        update_order_status(order_id, target, f"walk-{status}-{uuid.uuid4().hex}")
    return order_id


class TestCanTransition:
    def test_forward_chain_is_legal(self) -> None:
        assert can_transition("CREATED", "CONFIRMED")
        assert can_transition("CONFIRMED", "SHIPPED")
        assert can_transition("SHIPPED", "DELIVERED")

    def test_cancellable_states_can_cancel(self) -> None:
        assert can_transition("CREATED", "CANCELLED")
        assert can_transition("CONFIRMED", "CANCELLED")
        assert can_transition("SHIPPED", "CANCELLED")

    def test_terminal_states_accept_nothing(self) -> None:
        for target in ("CREATED", "CONFIRMED", "SHIPPED", "DELIVERED", "CANCELLED"):
            assert not can_transition("DELIVERED", target)
            assert not can_transition("CANCELLED", target)

    def test_rollback_to_created_is_illegal(self) -> None:
        assert not can_transition("CONFIRMED", "CREATED")
        assert not can_transition("SHIPPED", "CREATED")

    def test_skip_level_jumps_are_illegal(self) -> None:
        assert not can_transition("CREATED", "SHIPPED")
        assert not can_transition("CREATED", "DELIVERED")
        assert not can_transition("CONFIRMED", "DELIVERED")

    def test_noop_same_state_is_illegal(self) -> None:
        assert not can_transition("CREATED", "CREATED")
        assert not can_transition("CONFIRMED", "CONFIRMED")

    def test_unknown_status_is_illegal(self) -> None:
        assert not can_transition("FOO", "CONFIRMED")
        assert not can_transition("CREATED", "FOO")


class TestUpdateOrderStatus:
    def test_legal_transition_applies(self) -> None:
        order_id = _new_order_id()

        updated = update_order_status(order_id, "CONFIRMED", "k-confirm")

        assert updated is not None
        assert updated.order_id == order_id
        assert updated.status == "CONFIRMED"

    def test_replay_returns_recorded_order_without_reapplying(self) -> None:
        order_id = _new_order_id()
        first = update_order_status(order_id, "CONFIRMED", "k-replay")
        assert first is not None

        replayed = update_order_status(order_id, "SHIPPED", "k-replay")

        assert replayed is not None
        assert replayed.status == "CONFIRMED"  # the recorded result, not re-applied

    def test_illegal_transition_raises(self) -> None:
        order_id = _new_order_id()

        with pytest.raises(ValueError, match="cannot transition"):
            update_order_status(order_id, "DELIVERED", "k-skip")

    def test_terminal_state_rejects_update(self) -> None:
        order_id = _order_in("DELIVERED")

        with pytest.raises(ValueError, match="cannot transition"):
            update_order_status(order_id, "CANCELLED", "k-after-delivered")

    def test_unknown_order_returns_none(self) -> None:
        assert update_order_status("no-such-order", "CONFIRMED", "k-none") is None


class TestCancelOrder:
    def test_cancels_created_order(self) -> None:
        order_id = _new_order_id()

        cancelled = cancel_order(order_id, "k-cancel")

        assert cancelled is not None
        assert cancelled.status == "CANCELLED"

    def test_replay_returns_recorded_cancel(self) -> None:
        order_id = _new_order_id()
        first = cancel_order(order_id, "k-cancel-replay")
        assert first is not None

        replayed = cancel_order(order_id, "k-cancel-replay")

        assert replayed is not None
        assert replayed.status == "CANCELLED"

    def test_cancels_shipped_order(self) -> None:
        order_id = _order_in("SHIPPED")

        cancelled = cancel_order(order_id, "k-cancel-shipped")

        assert cancelled is not None
        assert cancelled.status == "CANCELLED"

    def test_delivered_order_cannot_cancel(self) -> None:
        order_id = _order_in("DELIVERED")

        with pytest.raises(ValueError, match="cannot be cancelled"):
            cancel_order(order_id, "k-cancel-delivered")

    def test_cancelled_order_cannot_cancel_twice(self) -> None:
        order_id = _new_order_id()
        cancel_order(order_id, "k-cancel-1")

        with pytest.raises(ValueError, match="cannot be cancelled"):
            cancel_order(order_id, "k-cancel-2")

    def test_unknown_order_returns_none(self) -> None:
        assert cancel_order("no-such-order", "k-none") is None
