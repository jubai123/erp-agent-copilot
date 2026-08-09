"""Tests for OpenAPI 3.x importer — parsing endpoints into tool definitions."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from erp_copilot.domain.errors import ValidationError
from erp_copilot.tools.openapi_importer import OpenAPIImporter, ParsedTool

MINIMAL_OPENAPI = {
    "openapi": "3.0.3",
    "info": {"title": "Test API", "version": "v1"},
    "paths": {
        "/products/{product_id}": {
            "get": {
                "operationId": "getProduct",
                "summary": "Get product by ID",
                "description": "Returns a single product",
                "tags": ["products"],
                "parameters": [
                    {
                        "name": "product_id",
                        "in": "path",
                        "required": True,
                        "description": "The product ID",
                        "schema": {"type": "integer", "format": "int64"},
                    }
                ],
                "responses": {"200": {"description": "OK"}},
            }
        }
    },
}


OPENAPI_WITH_QUERY_PARAMS = {
    "openapi": "3.0.3",
    "info": {"title": "Test API", "version": "v1"},
    "paths": {
        "/suppliers": {
            "get": {
                "operationId": "listSuppliers",
                "summary": "List suppliers",
                "description": "Filtered list",
                "tags": ["suppliers"],
                "parameters": [
                    {
                        "name": "status",
                        "in": "query",
                        "required": True,
                        "description": "Supplier status",
                        "schema": {"type": "string", "enum": ["InUse", "DisUse"]},
                    },
                    {
                        "name": "page",
                        "in": "query",
                        "required": False,
                        "schema": {"type": "integer", "default": 1},
                    },
                ],
                "responses": {"200": {"description": "OK"}},
            }
        }
    },
}


OPENAPI_WITH_REQUEST_BODY = {
    "openapi": "3.0.3",
    "info": {"title": "Test API", "version": "v1"},
    "paths": {
        "/orders": {
            "post": {
                "operationId": "createOrder",
                "summary": "Create an order",
                "description": "Creates a new sales order",
                "tags": ["orders"],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "$ref": "#/components/schemas/CreateOrderRequest"
                            }
                        }
                    },
                },
                "responses": {"200": {"description": "OK"}},
            }
        }
    },
    "components": {
        "schemas": {
            "CreateOrderRequest": {
                "type": "object",
                "properties": {
                    "product_id": {
                        "type": "integer",
                        "description": "Product ID",
                        "format": "int64",
                    },
                    "quantity": {
                        "type": "integer",
                        "description": "Order quantity",
                        "format": "int32",
                    },
                    "notes": {
                        "type": "string",
                        "description": "Optional notes",
                    },
                },
            }
        }
    },
}


OPENAPI_WITH_NESTED_REF = {
    "openapi": "3.0.3",
    "info": {"title": "Test API", "version": "v1"},
    "paths": {
        "/orders": {
            "post": {
                "operationId": "createOrder",
                "summary": "Create order",
                "tags": ["orders"],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "$ref": "#/components/schemas/CreateOrderRequest"
                            }
                        }
                    },
                },
                "responses": {"200": {"description": "OK"}},
            }
        }
    },
    "components": {
        "schemas": {
            "CreateOrderRequest": {
                "type": "object",
                "properties": {
                    "customer": {
                        "$ref": "#/components/schemas/CustomerInfo"
                    },
                    "quantity": {"type": "integer"},
                },
            },
            "CustomerInfo": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Customer name"},
                    "phone": {"type": "string"},
                },
            },
        }
    },
}


INVALID_OPENAPI_NO_VERSION = {
    "info": {"title": "No version"},
    "paths": {},
}


class TestParseMinimal:
    """Acceptance: Parses a minimal OpenAPI spec with a path parameter."""

    def test_parses_path_parameter(self) -> None:
        importer = OpenAPIImporter()
        tools = importer.parse(MINIMAL_OPENAPI)

        assert len(tools) == 1
        tool = tools[0]
        assert tool.name == "getProduct"
        assert tool.path == "/products/{product_id}"
        assert tool.method == "GET"
        assert tool.summary == "Get product by ID"
        assert tool.description == "Returns a single product"
        assert tool.tags == ["products"]

        assert len(tool.parameters) == 1
        param = tool.parameters[0]
        assert param.name == "product_id"
        assert param.param_type == "integer"
        assert param.location == "path"
        assert param.required is True
        assert param.description == "The product ID"


class TestParseQueryParams:
    """Acceptance: Parses query parameters with required/optional distinction."""

    def test_parses_query_parameters(self) -> None:
        importer = OpenAPIImporter()
        tools = importer.parse(OPENAPI_WITH_QUERY_PARAMS)

        assert len(tools) == 1
        tool = tools[0]
        assert tool.method == "GET"

        params = {p.name: p for p in tool.parameters}
        assert params["status"].location == "query"
        assert params["status"].required is True
        assert params["status"].param_type == "string"

        assert params["page"].location == "query"
        assert params["page"].required is False
        assert params["page"].param_type == "integer"
        assert params["page"].default == "1"


class TestParseRequestBody:
    """Acceptance: Resolves $ref in requestBody to extract parameters."""

    def test_resolves_ref_in_request_body(self) -> None:
        importer = OpenAPIImporter()
        tools = importer.parse(OPENAPI_WITH_REQUEST_BODY)

        assert len(tools) == 1
        tool = tools[0]
        assert tool.method == "POST"

        params = {p.name: p for p in tool.parameters}
        assert len(params) == 3
        assert params["product_id"].param_type == "integer"
        assert params["product_id"].location == "body"
        assert params["product_id"].description == "Product ID"
        assert params["notes"].location == "body"


class TestParseNestedRef:
    """Acceptance: Flattens nested $ref schemas in request bodies."""

    def test_flattens_nested_refs(self) -> None:
        importer = OpenAPIImporter()
        tools = importer.parse(OPENAPI_WITH_NESTED_REF)

        assert len(tools) == 1
        tool = tools[0]

        param_names = {p.name for p in tool.parameters}
        assert "quantity" in param_names
        assert "name" in param_names
        assert "phone" in param_names


class TestParseMultipleEndpoints:
    """Acceptance: Parses multiple paths and methods correctly."""

    def test_returns_one_tool_per_method(self) -> None:
        spec = {
            "openapi": "3.0.3",
            "info": {"title": "Multi", "version": "v1"},
            "paths": {
                "/items": {
                    "get": {
                        "operationId": "listItems",
                        "summary": "List items",
                        "responses": {"200": {"description": "OK"}},
                    },
                    "post": {
                        "operationId": "createItem",
                        "summary": "Create item",
                        "responses": {"200": {"description": "OK"}},
                    },
                }
            },
        }
        importer = OpenAPIImporter()
        tools = importer.parse(spec)

        assert len(tools) == 2
        assert {t.name for t in tools} == {"listItems", "createItem"}
        assert {t.method for t in tools} == {"GET", "POST"}


class TestParseErrors:
    """Acceptance: Invalid or malformed OpenAPI specs produce clear errors."""

    def test_missing_openapi_version_raises(self) -> None:
        importer = OpenAPIImporter()
        with pytest.raises(ValidationError, match="openapi"):
            importer.parse(INVALID_OPENAPI_NO_VERSION)

    def test_empty_paths_returns_empty_list(self) -> None:
        importer = OpenAPIImporter()
        tools = importer.parse(
            {"openapi": "3.0.3", "info": {"title": "E", "version": "v1"}, "paths": {}}
        )
        assert tools == []

    def test_missing_operation_id_skipped(self) -> None:
        spec = {
            "openapi": "3.0.3",
            "info": {"title": "Test", "version": "v1"},
            "paths": {
                "/items": {
                    "get": {
                        "summary": "No operationId here",
                        "responses": {"200": {"description": "OK"}},
                    }
                }
            },
        }
        importer = OpenAPIImporter()
        tools = importer.parse(spec)
        assert tools == []

    def test_unresolvable_ref_raises(self) -> None:
        spec = {
            "openapi": "3.0.3",
            "info": {"title": "Test", "version": "v1"},
            "paths": {
                "/items": {
                    "post": {
                        "operationId": "badRef",
                        "summary": "Bad ref",
                        "requestBody": {
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "$ref": "#/components/schemas/DoesNotExist"
                                    }
                                }
                            }
                        },
                        "responses": {"200": {"description": "OK"}},
                    }
                }
            },
        }
        importer = OpenAPIImporter()
        with pytest.raises(ValidationError, match="DoesNotExist"):
            importer.parse(spec)


class TestParseV5Dataset:
    """Acceptance: Correctly parses the real V5 dataset_apis_aliyun.json."""

    @classmethod
    @pytest.fixture(scope="class")
    def v5_tools(cls) -> list[ParsedTool]:
        dataset_path = (
            Path(__file__).parent.parent.parent.parent
            / "agent-copilot-v5-20260317"
            / "api_data"
            / "dataset_apis_aliyun.json"
        )
        spec = json.loads(dataset_path.read_text(encoding="utf-8"))
        importer = OpenAPIImporter()
        return importer.parse(spec)

    def test_parses_25_tools(self, v5_tools: list[ParsedTool]) -> None:
        assert len(v5_tools) == 25

    def test_all_tools_have_operation_id(self, v5_tools: list[ParsedTool]) -> None:
        for tool in v5_tools:
            assert tool.name, f"Tool {tool.path} {tool.method} has empty name"

    def test_all_tools_have_method(self, v5_tools: list[ParsedTool]) -> None:
        methods = {t.method for t in v5_tools}
        assert methods <= {"GET", "POST", "PUT", "DELETE"}

    def test_path_params_extracted(self, v5_tools: list[ParsedTool]) -> None:
        supplier_by_id = next(
            t for t in v5_tools if t.name == "getSupplier_1"
        )
        path_params = [p for p in supplier_by_id.parameters if p.location == "path"]
        assert len(path_params) == 1
        assert path_params[0].name == "supplierId"
        assert path_params[0].param_type == "integer"

    def test_query_params_extracted(self, v5_tools: list[ParsedTool]) -> None:
        by_status = next(
            t for t in v5_tools if t.name == "getSupplierByName"
        )
        query_params = [p for p in by_status.parameters if p.location == "query"]
        assert len(query_params) == 1
        assert query_params[0].name == "status"

    def test_body_params_extracted_from_ref(self, v5_tools: list[ParsedTool]) -> None:
        create_order = next(t for t in v5_tools if t.name == "create")
        body_params = [p for p in create_order.parameters if p.location == "body"]
        assert len(body_params) >= 3  # quantity, supplierId, productId, orderRegion

        param_names = {p.name for p in body_params}
        assert "quantity" in param_names
        assert "supplierId" in param_names
        assert "productId" in param_names
