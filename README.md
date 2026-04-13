# OpenAPI MCP Docs Server

A lightweight docs-focused MCP server that reads an OpenAPI JSON spec and exposes searchable endpoint and schema documentation as MCP resources and tools.

## Quick Start (VS Code Copilot)

1. Install dependencies:

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

2. This repo already includes `.vscode/mcp.json` with two ready servers:

- `dynamic-openapi-server-fakerestapi`
- `dynamic-openapi-server-petstore`

Update or add entries in `.vscode/mcp.json` as needed. Minimal example:

```json
{
  "servers": {
    "openapi-docs": {
      "type": "stdio",
      "command": "${workspaceFolder}/venv/bin/python",
      "args": [
        "${workspaceFolder}/server.py",
        "https://api.example.com/openapi.json"
      ]
    }
  }
}
```

3. In VS Code, run `MCP: List Servers` from the Command Palette and start `openapi-docs`.
4. Open Chat and try: `List API endpoints from the OpenAPI MCP server.`

If your OpenAPI URL needs a bearer token, use this `args` example:

```json
[
  "${workspaceFolder}/server.py",
  "https://api.example.com/openapi.json",
  "--auth",
  "YOUR_TOKEN"
]
```

Tip: keep secrets in user-level MCP config (not workspace) when possible.

## MCP Config Blocks For Other Clients

Use the same server command across clients:

- command: `/absolute/path/to/openapi-mcp/venv/bin/python`
- args: `/absolute/path/to/openapi-mcp/server.py <OPENAPI_SPEC_URL> [--auth TOKEN]`

### Claude Desktop (macOS)

File: `~/Library/Application Support/Claude/claude_desktop_config.json`

```json
{
  "mcpServers": {
    "openapi-docs": {
      "command": "/Users/yourname/dev/openapi-mcp/venv/bin/python",
      "args": [
        "/Users/yourname/dev/openapi-mcp/server.py",
        "https://api.example.com/openapi.json"
      ]
    }
  }
}
```

### Gemini CLI

File (project): `.gemini/settings.json`
File (user): `~/.gemini/settings.json`

```json
{
  "mcpServers": {
    "openapi-docs": {
      "command": "/Users/yourname/dev/openapi-mcp/venv/bin/python",
      "args": [
        "/Users/yourname/dev/openapi-mcp/server.py",
        "https://api.example.com/openapi.json"
      ],
      "cwd": "/Users/yourname/dev/openapi-mcp"
    }
  }
}
```

Optional Gemini CLI command-based setup:

```bash
gemini mcp add -s project openapi-docs /Users/yourname/dev/openapi-mcp/venv/bin/python /Users/yourname/dev/openapi-mcp/server.py https://api.example.com/openapi.json
```

### Windsurf

File: `~/.codeium/windsurf/mcp_config.json`

```json
{
  "mcpServers": {
    "openapi-docs": {
      "command": "/Users/yourname/dev/openapi-mcp/venv/bin/python",
      "args": [
        "/Users/yourname/dev/openapi-mcp/server.py",
        "https://api.example.com/openapi.json"
      ]
    }
  }
}
```

### Notes For Cursor And Other MCP Clients

Most clients use a similar `mcpServers` shape (or equivalent command/args fields).
If your client supports stdio MCP servers, point it to this project Python entrypoint:

- command: `<repo>/venv/bin/python`
- args: `<repo>/server.py <OPENAPI_SPEC_URL>`

## What This Project Does

Given an OpenAPI URL, the server:

- Downloads and parses the spec.
- Extracts endpoint metadata across supported HTTP methods.
- Normalizes and de-duplicates operation IDs.
- Resolves local `$ref` pointers (including nested schemas).
- Exposes endpoint and schema documentation through MCP resources and tools.

It is designed for API documentation discovery inside MCP clients.

## Requirements

- Python 3.10+
- Dependencies in `requirements.txt`:
  - `fastmcp`
  - `httpx`

## Installation (CLI)

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Run The Server

```bash
python server.py <OPENAPI_SPEC_URL>
```

Example:

```bash
python server.py https://api.example.com/openapi.json
```

If your spec URL requires auth, pass a bearer token:

```bash
python server.py https://api.example.com/openapi.json --auth YOUR_TOKEN
```

## Exposed MCP Resources

- `api://endpoints/catalog`
  - Returns a compact list of all endpoints with:
    - `operationId`
    - `method`
    - `path`
    - `summary`
    - `tags`

- `api://endpoints/{operationId}`
  - Returns full operation metadata for one endpoint.

## Exposed MCP Tools

- `list_endpoint_docs()`
  - Lists all discovered endpoints.

- `find_endpoint_operation(path, method)`
  - Finds an operation by exact path and method.

- `get_endpoint_schema_docs(operationId)`
  - Returns endpoint details plus resolved request/response schemas and referenced schemas.

- `get_schema_docs(schemaName)`
  - Returns one schema from `components/schemas` in both raw and resolved forms.

## Behavior Notes

- If an endpoint does not include `operationId`, a fallback ID is generated from method + path.
- Duplicate operation IDs are suffixed (`_2`, `_3`, ...).
- Only local refs (`#/...`) are resolved.
- Circular refs are preserved with `x-circularRef: true` markers.
- Unresolvable refs include `x-unresolvedRef` with the resolution error.

## Typical Workflow

1. Start the server with your OpenAPI URL.
2. Call `list_endpoint_docs` to discover operation IDs.
3. Use `get_endpoint_schema_docs` for deep endpoint schema details.
4. Use `get_schema_docs` for targeted schema inspection.

## VS Code Notes

- Workspace config file: `.vscode/mcp.json`
- User config file: run `MCP: Open User Configuration`
- If the server fails to start, open logs with `MCP: List Servers` -> select server -> `Show Output`

## Troubleshooting

- "Failed to fetch or parse OpenAPI spec":
  - Verify the URL is reachable.
  - Ensure the endpoint returns valid JSON.
  - Pass `--auth` when authentication is required.

- No endpoints returned:
  - Confirm the spec contains a `paths` object.

## Project Layout

- `server.py`: MCP server implementation and OpenAPI parsing logic.
- `requirements.txt`: Python dependencies.
