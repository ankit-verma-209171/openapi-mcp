import sys
import argparse
import httpx
from urllib.parse import urlparse
from fastmcp import FastMCP


def create_mcp_server(spec_url: str, auth_token: str = None) -> FastMCP:
    parsed_url = urlparse(spec_url)
    base_url = f"{parsed_url.scheme}://{parsed_url.netloc}"

    headers = {}
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"

    client = httpx.AsyncClient(base_url=base_url, headers=headers)

    print(f"Fetching OpenAPI spec from: {spec_url}", file=sys.stderr)
    try:
        response = httpx.get(spec_url)
        response.raise_for_status()
        openapi_spec = response.json()
    except Exception as e:
        print(f"Failed to fetch or parse OpenAPI spec: {e}", file=sys.stderr)
        sys.exit(1)

    server_name = openapi_spec.get("info", {}).get(
        "title", "Dynamic OpenAPI MCP Server"
    )
    print(f"Initializing MCP server: {server_name}", file=sys.stderr)

    return FastMCP.from_openapi(
        openapi_spec=openapi_spec, client=client, name=server_name
    )


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

    mcp = create_mcp_server(spec_url=args.spec_url, auth_token=args.auth)
    mcp.run()
