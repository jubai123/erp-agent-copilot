"""Integration tests for ERP Simulator product, inventory, and supplier endpoints."""

from __future__ import annotations

from fastapi.testclient import TestClient


class TestProductByName:
    """Acceptance: GET /products/{name} returns product info by name."""

    def test_get_existing_product_by_name(self) -> None:
        from apps.erp_simulator.simulator import create_simulator_app

        app = create_simulator_app()
        client = TestClient(app)

        response = client.get("/products/苹果")

        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "苹果"
        assert data["price"] == 10.0
        assert data["stock"] == 100
        assert data["unit"] == "KG"

    def test_get_nonexistent_product_returns_404(self) -> None:
        from apps.erp_simulator.simulator import create_simulator_app

        app = create_simulator_app()
        client = TestClient(app)

        response = client.get("/products/不存在产品")

        assert response.status_code == 404
        data = response.json()
        assert "detail" in data


class TestProductStock:
    """Acceptance: GET /products/{id}/stock returns stock quantity."""

    def test_get_stock_by_existing_id(self) -> None:
        from apps.erp_simulator.simulator import create_simulator_app

        app = create_simulator_app()
        client = TestClient(app)

        response = client.get("/products/1/stock")

        assert response.status_code == 200
        data = response.json()
        assert data["product_id"] == 1
        assert data["name"] == "苹果"
        assert data["stock"] == 100
        assert data["unit"] == "KG"

    def test_get_stock_nonexistent_id_returns_404(self) -> None:
        from apps.erp_simulator.simulator import create_simulator_app

        app = create_simulator_app()
        client = TestClient(app)

        response = client.get("/products/999/stock")

        assert response.status_code == 404
        data = response.json()
        assert "detail" in data


class TestSuppliersByRegion:
    """Acceptance: GET /suppliers?region=... returns suppliers for a region."""

    def test_get_suppliers_for_existing_region(self) -> None:
        from apps.erp_simulator.simulator import create_simulator_app

        app = create_simulator_app()
        client = TestClient(app)

        response = client.get("/suppliers?region=上海")

        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        assert len(data) > 0
        supplier = data[0]
        assert supplier["name"] == "华东物流"
        assert "上海" in supplier["regions"]
        assert supplier["rating"] == 4.8
        assert supplier["delivery_days"] == 2
        assert supplier["price_per_kg"] == 1.2

    def test_get_suppliers_nonexistent_region_returns_empty_list(self) -> None:
        from apps.erp_simulator.simulator import create_simulator_app

        app = create_simulator_app()
        client = TestClient(app)

        response = client.get("/suppliers?region=不存在区域")

        assert response.status_code == 200
        data = response.json()
        assert data == []

    def test_filter_suppliers_by_status_available(self) -> None:
        from apps.erp_simulator.simulator import create_simulator_app

        app = create_simulator_app()
        client = TestClient(app)

        response = client.get("/suppliers?region=广州&status=AVAILABLE")

        assert response.status_code == 200
        data = response.json()
        assert all(s["status"] == "AVAILABLE" for s in data)

    def test_filter_suppliers_by_status_unavailable(self) -> None:
        from apps.erp_simulator.simulator import create_simulator_app

        app = create_simulator_app()
        client = TestClient(app)

        response = client.get("/suppliers?region=广州&status=UNAVAILABLE")

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["name"] == "华南物流"
        assert data[0]["status"] == "UNAVAILABLE"


class TestOrdersCreate:
    """Acceptance: POST /orders creates an order with idempotency and stock check."""

    def test_create_order_success(self) -> None:
        from apps.erp_simulator.simulator import create_simulator_app

        app = create_simulator_app()
        client = TestClient(app)

        payload = {
            "product_id": 1,
            "quantity": 20,
            "supplier_id": 3,
            "region": "上海",
            "idempotency_key": "key-001",
        }
        response = client.post("/orders", json=payload)

        assert response.status_code == 201
        data = response.json()
        assert "order_id" in data
        assert data["product_id"] == 1
        assert data["quantity"] == 20
        assert data["supplier_id"] == 3
        assert data["region"] == "上海"
        assert data["status"] == "CREATED"

    def test_create_order_idempotent(self) -> None:
        from apps.erp_simulator.simulator import create_simulator_app

        app = create_simulator_app()
        client = TestClient(app)

        payload = {
            "product_id": 1,
            "quantity": 10,
            "supplier_id": 3,
            "region": "上海",
            "idempotency_key": "key-idem-001",
        }
        first = client.post("/orders", json=payload)
        assert first.status_code == 201

        # Capture stock after first request
        stock_after_first = client.get("/products/1/stock").json()["stock"]

        second = client.post("/orders", json=payload)

        assert second.status_code == 200
        assert second.json()["order_id"] == first.json()["order_id"]
        # Idempotent repeat must NOT deduct stock again
        stock_after_second = client.get("/products/1/stock").json()["stock"]
        assert stock_after_second == stock_after_first

    def test_create_order_insufficient_stock(self) -> None:
        from apps.erp_simulator.simulator import create_simulator_app

        app = create_simulator_app()
        client = TestClient(app)

        payload = {
            "product_id": 1,
            "quantity": 9999,
            "supplier_id": 3,
            "region": "上海",
            "idempotency_key": "key-no-stock",
        }
        response = client.post("/orders", json=payload)

        assert response.status_code == 422
        assert "Insufficient" in response.json()["detail"]

    def test_create_order_nonexistent_product(self) -> None:
        from apps.erp_simulator.simulator import create_simulator_app

        app = create_simulator_app()
        client = TestClient(app)

        payload = {
            "product_id": 999,
            "quantity": 5,
            "supplier_id": 3,
            "region": "上海",
            "idempotency_key": "key-bad-product",
        }
        response = client.post("/orders", json=payload)

        assert response.status_code == 404
        assert "product" in response.json()["detail"].lower()


class TestOrdersGet:
    """Acceptance: GET /orders/{id} returns order status."""

    def test_get_existing_order(self) -> None:
        from apps.erp_simulator.simulator import create_simulator_app

        app = create_simulator_app()
        client = TestClient(app)

        create_resp = client.post(
            "/orders",
            json={
                "product_id": 1,
                "quantity": 5,
                "supplier_id": 3,
                "region": "上海",
                "idempotency_key": "key-get-test",
            },
        )
        order_id = create_resp.json()["order_id"]

        response = client.get(f"/orders/{order_id}")

        assert response.status_code == 200
        data = response.json()
        assert data["order_id"] == order_id
        assert data["status"] == "CREATED"
        assert data["product_id"] == 1
        assert data["quantity"] == 5

    def test_get_nonexistent_order_returns_404(self) -> None:
        from apps.erp_simulator.simulator import create_simulator_app

        app = create_simulator_app()
        client = TestClient(app)

        response = client.get("/orders/nonexistent-id")

        assert response.status_code == 404
        assert "detail" in response.json()


class TestScenarioControl:
    """Acceptance: Scenario switching alters simulator behavior deterministically."""

    def test_default_scenario_is_happy_path(self) -> None:
        from apps.erp_simulator.simulator import create_simulator_app

        app = create_simulator_app()
        client = TestClient(app)

        response = client.get("/scenario")

        assert response.status_code == 200
        assert response.json()["scenario"] == "happy_path"

    def test_set_scenario_stock_insufficient(self) -> None:
        from apps.erp_simulator.simulator import create_simulator_app

        app = create_simulator_app()
        client = TestClient(app)

        client.put("/scenario/stock_insufficient")

        # Product stock should report 0
        stock_resp = client.get("/products/1/stock")
        assert stock_resp.json()["stock"] == 0

        # Creating an order should fail due to insufficient stock
        order_resp = client.post(
            "/orders",
            json={
                "product_id": 1,
                "quantity": 1,
                "supplier_id": 3,
                "region": "上海",
                "idempotency_key": "key-si-001",
            },
        )
        assert order_resp.status_code == 422

    def test_set_scenario_supplier_unavailable(self) -> None:
        from apps.erp_simulator.simulator import create_simulator_app

        app = create_simulator_app()
        client = TestClient(app)

        client.put("/scenario/supplier_unavailable")

        response = client.get("/suppliers?region=上海")

        assert response.status_code == 200
        suppliers = response.json()
        assert len(suppliers) > 0
        assert all(s["status"] == "UNAVAILABLE" for s in suppliers)

    def test_set_scenario_timeout(self) -> None:
        from apps.erp_simulator.simulator import create_simulator_app

        app = create_simulator_app()
        client = TestClient(app)

        client.put("/scenario/timeout")

        response = client.post(
            "/orders",
            json={
                "product_id": 1,
                "quantity": 1,
                "supplier_id": 3,
                "region": "上海",
                "idempotency_key": "key-timeout-001",
            },
        )
        assert response.status_code == 504
        assert "timeout" in response.json()["detail"].lower()
        # Order should NOT have been persisted
        get_resp = client.get("/orders/key-timeout-001")
        assert get_resp.status_code == 404

    def test_restore_happy_path(self) -> None:
        from apps.erp_simulator.simulator import create_simulator_app

        app = create_simulator_app()
        client = TestClient(app)

        # Start broken, then restore
        client.put("/scenario/stock_insufficient")
        client.put("/scenario/happy_path")

        stock_resp = client.get("/products/1/stock")
        assert stock_resp.json()["stock"] > 0

        order_resp = client.post(
            "/orders",
            json={
                "product_id": 1,
                "quantity": 1,
                "supplier_id": 3,
                "region": "上海",
                "idempotency_key": "key-restore-001",
            },
        )
        assert order_resp.status_code == 201
