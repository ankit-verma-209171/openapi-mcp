"""Dynamic docs-focused MCP server built from an OpenAPI specification.

The server fetches an OpenAPI JSON document at startup, derives endpoint metadata,
and exposes tools/resources for endpoint and schema documentation lookup.
"""

import sys
import argparse
import json
import re
import copy
import httpx
from fastmcp import FastMCP
from fastmcp.tools import ToolResult
from mcp.types import TextContent


def _fallback_operation_id(method: str, path: str) -> str:
    """Build a deterministic fallback operationId when one is missing.

    Args:
        method: HTTP method name.
        path: OpenAPI path template.

    Returns:
        A normalized operation ID string.
    """
    normalized_path = path.strip("/") or "root"
    normalized_path = normalized_path.replace("{", "").replace("}", "")
    normalized_path = re.sub(r"[^a-zA-Z0-9_]+", "_", normalized_path).strip("_")
    normalized_path = normalized_path or "root"
    return f"{method.lower()}_{normalized_path}"


def _ensure_unique_operation_id(operation_id: str, seen: set[str]) -> str:
    """Ensure an operation ID is unique within the collected endpoint set.

    If the provided ID already exists, a numeric suffix is appended.

    Args:
        operation_id: Candidate operation ID.
        seen: Mutable set of operation IDs already used.

    Returns:
        A unique operation ID.
    """
    if operation_id not in seen:
        seen.add(operation_id)
        return operation_id

    suffix = 2
    while f"{operation_id}_{suffix}" in seen:
        suffix += 1

    unique_operation_id = f"{operation_id}_{suffix}"
    seen.add(unique_operation_id)
    return unique_operation_id


def _collect_operation_docs(openapi_spec: dict) -> list[dict]:
    """Extract operation-level documentation records from an OpenAPI spec.

    This function scans all supported HTTP methods in `paths`, combines
    path-level and operation-level parameters, and guarantees unique operation IDs.

    Args:
        openapi_spec: Parsed OpenAPI document.

    Returns:
        A list of operation documentation dictionaries.
    """
    operations = []
    seen_operation_ids: set[str] = set()
    paths = openapi_spec.get("paths", {})

    for path, path_item in paths.items():
        if not isinstance(path_item, dict):
            continue

        path_level_parameters = path_item.get("parameters", [])

        for method, operation in path_item.items():
            method_normalized = method.lower()
            if method_normalized not in {
                "get",
                "post",
                "put",
                "patch",
                "delete",
                "head",
                "options",
                "trace",
            }:
                continue

            if not isinstance(operation, dict):
                continue

            base_operation_id = operation.get("operationId") or _fallback_operation_id(
                method_normalized, path
            )
            operation_id = _ensure_unique_operation_id(base_operation_id, seen_operation_ids)

            parameters = []
            if isinstance(path_level_parameters, list):
                parameters.extend(path_level_parameters)
            if isinstance(operation.get("parameters"), list):
                parameters.extend(operation["parameters"])

            operations.append(
                {
                    "operationId": operation_id,
                    "method": method_normalized.upper(),
                    "path": path,
                    "summary": operation.get("summary", ""),
                    "description": operation.get("description", ""),
                    "tags": operation.get("tags", []),
                    "parameters": parameters,
                    "requestBody": operation.get("requestBody"),
                    "responses": operation.get("responses", {}),
                }
            )

    return operations


def _decode_json_pointer_token(token: str) -> str:
    """Decode a single RFC 6901 JSON pointer token."""
    return token.replace("~1", "/").replace("~0", "~")


def _resolve_json_pointer(openapi_spec: dict, ref: str):
    """Resolve a local JSON pointer against the OpenAPI document.

    Args:
        openapi_spec: Parsed OpenAPI document.
        ref: Local JSON reference (for example, `#/components/schemas/User`).

    Returns:
        The referenced object.

    Raises:
        ValueError: If the reference is not local (`#/...`).
        KeyError: If any pointer segment cannot be resolved.
    """
    if not ref.startswith("#/"):
        raise ValueError(f"Only local refs are supported: {ref}")

    node = openapi_spec
    for raw_token in ref[2:].split("/"):
        token = _decode_json_pointer_token(raw_token)
        if isinstance(node, dict):
            if token not in node:
                raise KeyError(f"Missing key '{token}' while resolving '{ref}'")
            node = node[token]
            continue

        if isinstance(node, list):
            try:
                index = int(token)
            except ValueError as exc:
                raise KeyError(
                    f"Expected list index, got '{token}' while resolving '{ref}'"
                ) from exc
            if index < 0 or index >= len(node):
                raise KeyError(f"Index '{index}' out of range while resolving '{ref}'")
            node = node[index]
            continue

        raise KeyError(f"Cannot resolve '{token}' while resolving '{ref}'")

    return node


def _collect_local_refs(node, refs: set[str]) -> None:
    """Collect all local `$ref` values from a nested node.

    Args:
        node: Any JSON-like node (dict, list, scalar).
        refs: Mutable set that receives discovered local refs.
    """
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/"):
            refs.add(ref)
        for value in node.values():
            _collect_local_refs(value, refs)
        return

    if isinstance(node, list):
        for item in node:
            _collect_local_refs(item, refs)


def _resolve_local_refs(node, openapi_spec: dict, stack: set[str] | None = None):
    """Recursively resolve local `$ref` values in an OpenAPI node.

    Circular references are preserved with an `x-circularRef` marker and unresolved
    references are preserved with an `x-unresolvedRef` marker instead of raising.

    Args:
        node: Any JSON-like node to resolve.
        openapi_spec: Parsed OpenAPI document.
        stack: Internal recursion guard for circular reference detection.

    Returns:
        A deep-resolved node.
    """
    if stack is None:
        stack = set()

    if isinstance(node, list):
        return [_resolve_local_refs(item, openapi_spec, stack) for item in node]

    if not isinstance(node, dict):
        return node

    ref = node.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/"):
        if ref in stack:
            return {"$ref": ref, "x-circularRef": True}

        try:
            target = _resolve_json_pointer(openapi_spec, ref)
        except Exception as exc:
            return {"$ref": ref, "x-unresolvedRef": str(exc)}

        stack.add(ref)
        resolved_target = _resolve_local_refs(copy.deepcopy(target), openapi_spec, stack)
        stack.remove(ref)

        sibling_fields = {k: v for k, v in node.items() if k != "$ref"}
        if not sibling_fields:
            return resolved_target

        if isinstance(resolved_target, dict):
            merged = copy.deepcopy(resolved_target)
            for key, value in sibling_fields.items():
                merged[key] = _resolve_local_refs(value, openapi_spec, stack)
            return merged

        return {
            "allOf": [
                resolved_target,
                _resolve_local_refs(sibling_fields, openapi_spec, stack),
            ]
        }

    return {
        key: _resolve_local_refs(value, openapi_spec, stack)
        for key, value in node.items()
    }


def _schema_name_from_ref(ref: str) -> str | None:
    """Extract `components/schemas` name from a local schema ref.

    Args:
        ref: Local reference string.

    Returns:
        The schema name when the ref points to `#/components/schemas/*`,
        otherwise `None`.
    """
    prefix = "#/components/schemas/"
    if ref.startswith(prefix):
        return ref[len(prefix) :]
    return None


def _build_endpoint_details(operation_doc: dict, openapi_spec: dict) -> dict:
    """Build a fully expanded endpoint documentation payload.

    The output includes the original operation, a resolved operation where local
    refs are expanded, and all referenced schemas resolved independently.

    Args:
        operation_doc: Operation metadata from `_collect_operation_docs`.
        openapi_spec: Parsed OpenAPI document.

    Returns:
        Structured endpoint details for tool output.
    """
    refs: set[str] = set()
    _collect_local_refs(operation_doc, refs)

    referenced_schemas = {}
    for ref in sorted(refs):
        schema_name = _schema_name_from_ref(ref)
        key = schema_name or ref
        referenced_schemas[key] = {
            "ref": ref,
            "schema": _resolve_local_refs({"$ref": ref}, openapi_spec),
        }

    return {
        "operation": operation_doc,
        "resolvedOperation": _resolve_local_refs(copy.deepcopy(operation_doc), openapi_spec),
        "referencedSchemas": referenced_schemas,
        "referenceCount": len(refs),
    }


def _single_tool_json(payload: dict) -> ToolResult:
    """Wrap a JSON payload as text-only MCP `ToolResult` content.

    Args:
        payload: JSON-serializable object.

    Returns:
        `ToolResult` with pretty-printed JSON text content.
    """
    return ToolResult(
        content=[TextContent(type="text", text=json.dumps(payload, indent=2))]
    )


def create_mcp_server(spec_url: str, auth_token: str = None) -> tuple[FastMCP, dict]:
    """Create and initialize the MCP server from a remote OpenAPI JSON spec.

    Args:
        spec_url: URL to the OpenAPI JSON document.
        auth_token: Optional bearer token used when fetching the spec.

    Returns:
        A tuple of `(FastMCP instance, parsed OpenAPI spec)`.

    Side Effects:
        Writes startup/failure status messages to stderr.
        Exits the process with status code 1 if the spec cannot be fetched/parsed.
    """
    headers = {}
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"

    print(f"Fetching OpenAPI spec from: {spec_url}", file=sys.stderr)
    try:
        response = httpx.get(spec_url, headers=headers)
        response.raise_for_status()
        openapi_spec = response.json()
    except Exception as e:
        print(f"Failed to fetch or parse OpenAPI spec: {e}", file=sys.stderr)
        sys.exit(1)

    server_name = openapi_spec.get("info", {}).get(
        "title", "Dynamic OpenAPI MCP Server"
    )
    print(f"Initializing MCP server: {server_name}", file=sys.stderr)

    # Docs-only server: do not auto-register endpoint execution tools.
    mcp = FastMCP(name=server_name)
    return mcp, openapi_spec


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Run an MCP server from an OpenAPI spec."
    )
    parser.add_argument(
        "spec_url",
        help="The URL of the OpenAPI JSON specification (e.g. https://api.example.com/swagger.json)",
    )
    parser.add_argument(
        "--auth",
        "-a",
        help="Optional authentication token (will be passed as a Bearer token)",
        default=None,
    )

    args = parser.parse_args()

    mcp, openapi_spec = create_mcp_server(spec_url=args.spec_url, auth_token=args.auth)
    operation_docs = _collect_operation_docs(openapi_spec)
    operation_docs_by_id = {
        operation_doc["operationId"]: operation_doc for operation_doc in operation_docs
    }
    operation_docs_by_method_path = {
        (operation_doc["method"], operation_doc["path"]): operation_doc
        for operation_doc in operation_docs
    }

    @mcp.resource("api://endpoints/catalog")
    def get_endpoint_catalog() -> str:
        """Return a compact JSON catalog of all discovered endpoints."""
        catalog = [
            {
                "operationId": operation_doc["operationId"],
                "method": operation_doc["method"],
                "path": operation_doc["path"],
                "summary": operation_doc["summary"],
                "tags": operation_doc["tags"],
            }
            for operation_doc in operation_docs
        ]
        return json.dumps({"endpoints": catalog}, indent=2)

    @mcp.resource("api://endpoints/{operationId}")
    def get_endpoint_docs(operationId: str) -> str:
        """Return JSON documentation for a single endpoint by operation ID."""
        operation_doc = operation_docs_by_id.get(operationId)
        if operation_doc is None:
            available_operation_ids = sorted(operation_docs_by_id.keys())
            return json.dumps(
                {
                    "error": "Unknown operationId",
                    "operationId": operationId,
                    "hint": "Read api://endpoints/catalog to discover available operation IDs.",
                    "availableOperationIdsSample": available_operation_ids[:25],
                    "totalAvailableOperationIds": len(available_operation_ids),
                },
                indent=2,
            )

        return json.dumps(operation_doc, indent=2)

    @mcp.tool
    def list_endpoint_docs() -> ToolResult:
        """List all endpoints with operationId, method, path, summary, and tags."""
        catalog = [
            {
                "operationId": operation_doc["operationId"],
                "method": operation_doc["method"],
                "path": operation_doc["path"],
                "summary": operation_doc["summary"],
                "tags": operation_doc["tags"],
            }
            for operation_doc in operation_docs
        ]
        return _single_tool_json({"endpoints": catalog, "total": len(catalog)})

    @mcp.tool
    def find_endpoint_operation(path: str, method: str) -> ToolResult:
        """Find an operation by exact path and method.

        Args:
            path: Exact OpenAPI path template.
            method: HTTP method string.

        Returns:
            Matching operation metadata or an error payload.
        """
        method_normalized = method.strip().upper()
        operation_doc = operation_docs_by_method_path.get((method_normalized, path))
        if operation_doc is None:
            return _single_tool_json(
                {
                    "error": "Operation not found",
                    "path": path,
                    "method": method_normalized,
                    "hint": "Use list_endpoint_docs to inspect valid method/path pairs.",
                }
            )

        return _single_tool_json(
            {
                "operationId": operation_doc["operationId"],
                "method": operation_doc["method"],
                "path": operation_doc["path"],
                "summary": operation_doc["summary"],
                "tags": operation_doc["tags"],
            }
        )

    @mcp.tool
    def get_endpoint_schema_docs(operationId: str) -> ToolResult:
        """Get full endpoint details including resolved request/response schemas.

        Args:
            operationId: Target operation ID.

        Returns:
            Endpoint details payload with resolved refs or an error payload.
        """
        operation_doc = operation_docs_by_id.get(operationId)
        if operation_doc is None:
            available_operation_ids = sorted(operation_docs_by_id.keys())
            return _single_tool_json(
                {
                    "error": "Unknown operationId",
                    "operationId": operationId,
                    "hint": "Call list_endpoint_docs to discover available operation IDs.",
                    "availableOperationIdsSample": available_operation_ids[:25],
                    "totalAvailableOperationIds": len(available_operation_ids),
                }
            )

        return _single_tool_json(_build_endpoint_details(operation_doc, openapi_spec))

    @mcp.tool
    def get_schema_docs(schemaName: str) -> ToolResult:
        """Get a schema from `components/schemas` with local refs resolved.

        Args:
            schemaName: Exact schema name from `components/schemas`.

        Returns:
            Raw and resolved schema payloads or an error payload.
        """
        schemas = openapi_spec.get("components", {}).get("schemas", {})
        if schemaName not in schemas:
            available_schema_names = sorted(schemas.keys())
            return _single_tool_json(
                {
                    "error": "Unknown schemaName",
                    "schemaName": schemaName,
                    "availableSchemaNamesSample": available_schema_names[:25],
                    "totalAvailableSchemaNames": len(available_schema_names),
                }
            )

        raw_schema = schemas[schemaName]
        resolved_schema = _resolve_local_refs(copy.deepcopy(raw_schema), openapi_spec)
        return _single_tool_json(
            {
                "schemaName": schemaName,
                "rawSchema": raw_schema,
                "resolvedSchema": resolved_schema,
            }
        )

    mcp.run()
