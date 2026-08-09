"""OpenAPI 3.x importer — parse endpoints into tool definitions.

Resolves ``$ref`` pointers in request bodies and flattens nested
schema properties into flat parameter lists suitable for
:class:`ToolRegistry`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from erp_copilot.domain.errors import ValidationError

HTTP_METHODS = {"get", "post", "put", "delete", "patch", "options", "head"}
VALID_PARAM_TYPES = {"string", "integer", "number", "boolean", "array", "object"}


@dataclass
class ParsedParameter:
    """A single tool parameter extracted from an OpenAPI definition."""

    name: str
    param_type: str
    location: str  # path, query, body
    required: bool = False
    description: str = ""
    default: str | None = None


@dataclass
class ParsedTool:
    """A tool definition parsed from an OpenAPI endpoint."""

    name: str
    path: str
    method: str
    summary: str = ""
    description: str = ""
    parameters: list[ParsedParameter] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)


class OpenAPIImporter:
    """Parse an OpenAPI 3.x spec dict into a list of :class:`ParsedTool`.

    Typical usage::

        spec = json.loads(openapi_file.read_text())
        importer = OpenAPIImporter()
        tools = importer.parse(spec)
        for tool in tools:
            registry.register(
                name=tool.name,
                description=tool.summary,
                parameters=[...],
            )
    """

    def parse(self, spec: dict) -> list[ParsedTool]:
        """Extract tool definitions from an OpenAPI spec dict."""
        if "openapi" not in spec:
            raise ValidationError(
                "Not a valid OpenAPI 3.x spec: missing 'openapi' version field"
            )

        schemas = _extract_schemas(spec)
        tools: list[ParsedTool] = []

        for path, path_item in spec.get("paths", {}).items():
            if not isinstance(path_item, dict):
                continue

            for method, operation in path_item.items():
                if method not in HTTP_METHODS:
                    continue
                if not isinstance(operation, dict):
                    continue

                operation_id = operation.get("operationId")
                if not operation_id:
                    continue

                parameters: list[ParsedParameter] = []

                # Path-level and method-level parameters
                for param in list(path_item.get("parameters", [])) + list(
                    operation.get("parameters", [])
                ):
                    parameters.append(_parse_parameter(param))

                # Request body parameters via $ref resolution
                request_body = operation.get("requestBody")
                if request_body:
                    parameters.extend(
                        _resolve_request_body(request_body, schemas)
                    )

                tools.append(
                    ParsedTool(
                        name=str(operation_id),
                        path=str(path),
                        method=method.upper(),
                        summary=operation.get("summary", ""),
                        description=operation.get("description", ""),
                        parameters=parameters,
                        tags=[
                            str(t)
                            for t in operation.get("tags", [])
                            if isinstance(t, str)
                        ],
                    )
                )

        return tools


def _extract_schemas(spec: dict) -> dict[str, dict]:
    """Extract named schemas from ``components.schemas``."""
    components = spec.get("components", {})
    if not isinstance(components, dict):
        return {}
    schemas = components.get("schemas", {})
    return schemas if isinstance(schemas, dict) else {}


def _parse_parameter(param: dict) -> ParsedParameter:
    """Parse a path/query/header parameter object."""
    schema = param.get("schema", {})
    return ParsedParameter(
        name=str(param.get("name", "")),
        param_type=str(schema.get("type", "string")),
        location=str(param.get("in", "query")),
        required=bool(param.get("required", False)),
        description=str(param.get("description", "")),
        default=str(schema["default"])
        if isinstance(schema, dict) and "default" in schema
        else None,
    )


def _resolve_request_body(
    request_body: dict, schemas: dict[str, dict]
) -> list[ParsedParameter]:
    """Extract parameters from a requestBody by resolving ``$ref``."""
    content = request_body.get("content", {})
    json_content = content.get("application/json", {})
    schema = json_content.get("schema", {})

    ref = schema.get("$ref")
    if not ref:
        return []

    schema_name = ref.split("/")[-1]
    schema_def = schemas.get(schema_name)
    if schema_def is None:
        raise ValidationError(
            f"Unresolvable $ref: '{ref}' — schema '{schema_name}' "
            f"not found in components/schemas"
        )

    return _extract_properties(schema_def, schemas)


def _extract_properties(
    schema_def: dict, schemas: dict[str, dict]
) -> list[ParsedParameter]:
    """Flatten properties from a schema, resolving nested ``$ref``."""
    params: list[ParsedParameter] = []

    for prop_name, prop_def in schema_def.get("properties", {}).items():
        if not isinstance(prop_def, dict):
            continue

        nested_ref = prop_def.get("$ref")
        if nested_ref:
            nested_name = nested_ref.split("/")[-1]
            nested_schema = schemas.get(nested_name)
            if nested_schema:
                params.extend(_extract_properties(nested_schema, schemas))
            continue

        params.append(
            ParsedParameter(
                name=str(prop_name),
                param_type=str(prop_def.get("type", "string")),
                location="body",
                description=str(prop_def.get("description", "")),
            )
        )

    return params


def validate_tools(tools: list[ParsedTool]) -> None:
    """Validate parsed tools and raise :class:`ValidationError` on issues.

    Checks:
    - Tool name is non-empty
    - Parameter names are non-empty
    - Parameter types are in the supported set
    - No duplicate parameter names within a single tool
    """
    if not tools:
        raise ValidationError(
            "No tools to import: the spec contains no endpoints with a "
            "non-empty operationId. Check that each path+method has an "
            "operationId field."
        )

    for tool in tools:
        if not tool.name.strip():
            raise ValidationError(
                f"Tool at {tool.method} {tool.path} has an empty operationId"
            )

        seen_names: set[str] = set()

        for param in tool.parameters:
            if not param.name.strip():
                raise ValidationError(
                    f"Tool '{tool.name}' ({tool.method} {tool.path}) has "
                    f"a parameter with an empty name"
                )

            if param.param_type not in VALID_PARAM_TYPES:
                raise ValidationError(
                    f"Tool '{tool.name}' parameter '{param.name}' has "
                    f"unsupported type '{param.param_type}'. "
                    f"Supported types: {', '.join(sorted(VALID_PARAM_TYPES))}"
                )

            if param.name in seen_names:
                raise ValidationError(
                    f"Tool '{tool.name}' has duplicate parameter '{param.name}'"
                )
            seen_names.add(param.name)
