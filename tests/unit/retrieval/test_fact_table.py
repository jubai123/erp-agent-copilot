"""Test that the unified fact table (manifest.yaml) is consistent
with the ERP Simulator seed data.

The manifest is the source of truth for product definitions, supplier
definitions, order fields, scenarios, and business constraints. This
test cross-checks it against the simulator's in-memory seed data to
catch drift between the two.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from apps.erp_simulator.data.products import SEED_PRODUCTS
from apps.erp_simulator.data.suppliers import SEED_SUPPLIERS
from apps.erp_simulator.scenarios import VALID_SCENARIOS


def _load_manifest() -> dict:
    path = Path(__file__).parent.parent.parent.parent / "datasets" / "knowledge" / "manifest.yaml"
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# Product cross-checks
# ---------------------------------------------------------------------------


class TestProductFactTable:
    """Verify every simulator product is in the manifest and vice versa."""

    def test_product_count_matches(self) -> None:
        manifest = _load_manifest()
        assert len(manifest["products"]) == len(SEED_PRODUCTS)

    def test_every_seed_product_in_manifest(self) -> None:
        manifest = _load_manifest()
        manifest_ids = {p["product_id"] for p in manifest["products"]}
        for seed in SEED_PRODUCTS:
            assert seed.product_id in manifest_ids, (
                f"Product '{seed.name}' (id={seed.product_id}) missing in manifest"
            )

    def test_every_manifest_product_in_seed(self) -> None:
        manifest = _load_manifest()
        seed_ids = {p.product_id for p in SEED_PRODUCTS}
        for m in manifest["products"]:
            assert m["product_id"] in seed_ids, (
                f"Manifest product '{m['name']}' (id={m['product_id']}) missing in simulator"
            )

    def test_product_fields_consistency(self) -> None:
        """Immutable product fields must match seed data exactly.

        quantity_in_stock is excluded because it is runtime-mutable
        (order creation decrements the in-memory store during tests).
        """
        manifest = _load_manifest()
        seed_by_id = {p.product_id: p for p in SEED_PRODUCTS}
        for m in manifest["products"]:
            seed = seed_by_id[m["product_id"]]
            assert m["name"] == seed.name
            assert m["description"] == seed.description
            assert m["price"] == seed.price
            assert m["unit"] == seed.unit

    def test_product_unit_enum_declared(self) -> None:
        """The manifest must declare allowed product unit values."""
        manifest = _load_manifest()
        allowed_units = manifest.get("product_unit_enum", [])
        assert len(allowed_units) >= 3, "Expected at least KG, 台, 件"
        for product in manifest["products"]:
            assert product["unit"] in allowed_units, (
                f"Unit '{product['unit']}' for '{product['name']}' "
                f"not in declared product_unit_enum"
            )

    def test_product_required_fields_declared(self) -> None:
        manifest = _load_manifest()
        required = manifest.get("product_required_fields", [])
        expected = {"product_id", "name", "price", "quantity_in_stock", "unit"}
        assert set(required) == expected, (
            f"product_required_fields mismatch: {set(required)} vs {expected}"
        )


# ---------------------------------------------------------------------------
# Supplier cross-checks
# ---------------------------------------------------------------------------


class TestSupplierFactTable:
    """Verify every simulator supplier is in the manifest and vice versa."""

    def test_supplier_count_matches(self) -> None:
        manifest = _load_manifest()
        assert len(manifest["suppliers"]) == len(SEED_SUPPLIERS)

    def test_every_seed_supplier_in_manifest(self) -> None:
        manifest = _load_manifest()
        manifest_ids = {s["supplier_id"] for s in manifest["suppliers"]}
        for seed in SEED_SUPPLIERS:
            assert seed.supplier_id in manifest_ids, (
                f"Supplier '{seed.name}' (id={seed.supplier_id}) missing in manifest"
            )

    def test_every_manifest_supplier_in_seed(self) -> None:
        manifest = _load_manifest()
        seed_ids = {s.supplier_id for s in SEED_SUPPLIERS}
        for m in manifest["suppliers"]:
            assert m["supplier_id"] in seed_ids, (
                f"Manifest supplier '{m['name']}' (id={m['supplier_id']}) missing in simulator"
            )

    def test_supplier_fields_consistency(self) -> None:
        manifest = _load_manifest()
        seed_by_id = {s.supplier_id: s for s in SEED_SUPPLIERS}
        for m in manifest["suppliers"]:
            seed = seed_by_id[m["supplier_id"]]
            assert m["name"] == seed.name
            assert m["regions"] == seed.regions
            assert m["status"] == seed.status
            assert m["rating"] == seed.rating
            assert m["delivery_days"] == seed.delivery_days
            assert m["price_per_kg"] == seed.price_per_kg

    def test_supplier_status_enum_declared(self) -> None:
        manifest = _load_manifest()
        allowed = manifest.get("supplier_status_enum", [])
        assert "AVAILABLE" in allowed
        assert "UNAVAILABLE" in allowed
        for supplier in manifest["suppliers"]:
            assert supplier["status"] in allowed, (
                f"Status '{supplier['status']}' for '{supplier['name']}' "
                f"not in declared supplier_status_enum"
            )

    def test_supplier_region_enum_declared(self) -> None:
        manifest = _load_manifest()
        declared_regions = set(manifest.get("supplier_region_enum", []))
        used_regions: set[str] = set()
        for supplier in manifest["suppliers"]:
            used_regions.update(supplier["regions"])
        assert used_regions.issubset(declared_regions), (
            f"Regions {used_regions - declared_regions} used but not declared"
        )


# ---------------------------------------------------------------------------
# Scenario cross-checks
# ---------------------------------------------------------------------------


class TestScenarioFactTable:
    """Verify declared scenarios match the simulator."""

    def test_scenario_names_match(self) -> None:
        manifest = _load_manifest()
        declared = {s["name"] for s in manifest["scenarios"]}
        assert declared == VALID_SCENARIOS, (
            f"Scenario mismatch: manifest={declared}, simulator={VALID_SCENARIOS}"
        )

    def test_each_scenario_has_affected_endpoints(self) -> None:
        manifest = _load_manifest()
        for scenario in manifest["scenarios"]:
            assert "affected_endpoints" in scenario, (
                f"Scenario '{scenario['name']}' missing affected_endpoints"
            )
            assert isinstance(scenario["affected_endpoints"], list)
            assert len(scenario["affected_endpoints"]) > 0, (
                f"Scenario '{scenario['name']}' has empty affected_endpoints"
            )

    def test_each_scenario_has_description(self) -> None:
        manifest = _load_manifest()
        for scenario in manifest["scenarios"]:
            assert "description" in scenario
            assert len(scenario["description"]) > 0


# ---------------------------------------------------------------------------
# Order structure checks
# ---------------------------------------------------------------------------


class TestOrderFactTable:
    """Verify the manifest declares the order model and its state machine."""

    def test_order_fields_declared(self) -> None:
        manifest = _load_manifest()
        fields = manifest.get("order_fields", [])
        expected = {
            "order_id",
            "product_id",
            "product_name",
            "quantity",
            "supplier_id",
            "region",
            "amount",
            "status",
            "idempotency_key",
            "created_at",
        }
        actual_names = {f["name"] for f in fields}
        assert actual_names == expected, f"order_fields mismatch: {actual_names} vs {expected}"

    def test_order_status_lifecycle_declared(self) -> None:
        manifest = _load_manifest()
        lifecycle = manifest.get("order_status_lifecycle", {})
        assert "initial" in lifecycle, "Missing initial status"
        assert lifecycle["initial"] == "CREATED"
        assert "transitions" in lifecycle, "Missing status transitions"
        transitions = lifecycle["transitions"]
        # CREATED must have at least one valid next status
        assert "CREATED" in transitions
        assert len(transitions["CREATED"]) >= 1

    def test_idempotency_key_constraint_declared(self) -> None:
        manifest = _load_manifest()
        constraints = manifest.get("business_constraints", {})
        assert "idempotency" in constraints, "Missing idempotency constraint"
        idem = constraints["idempotency"]
        assert "mechanism" in idem
        assert "scope" in idem  # e.g. "per-client"


# ---------------------------------------------------------------------------
# Cross-entity business constraints
# ---------------------------------------------------------------------------


class TestBusinessConstraints:
    """Verify business rules declared in the manifest."""

    def test_stock_validation_rule_declared(self) -> None:
        manifest = _load_manifest()
        constraints = manifest.get("business_constraints", {})
        assert "stock_validation" in constraints, "Missing stock_validation rule"

    def test_region_enum_matches_supplier_regions(self) -> None:
        manifest = _load_manifest()
        declared = set(manifest.get("supplier_region_enum", []))
        used: set[str] = set()
        for s in manifest["suppliers"]:
            used.update(s["regions"])
        assert used.issubset(declared), (
            f"Supplier regions {used - declared} not in supplier_region_enum"
        )

    def test_order_region_must_be_valid(self) -> None:
        """Order region field must reference a declared region."""
        manifest = _load_manifest()
        order_fields = manifest.get("order_fields", [])
        region_field = next((f for f in order_fields if f["name"] == "region"), None)
        assert region_field is not None
        assert "constraint" in region_field
        assert region_field["constraint"] == "must be in supplier_region_enum"
