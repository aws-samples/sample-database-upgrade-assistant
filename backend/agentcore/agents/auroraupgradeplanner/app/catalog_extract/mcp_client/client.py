"""
MCP Client helper — placeholder for external MCP tool integration.

This file is a template. Replace EXAMPLE_MCP_ENDPOINT with an actual
MCP server URL and configure auth headers if required.
"""

import logging
from mcp.client.streamable_http import streamablehttp_client
from strands.tools.mcp.mcp_client import MCPClient

logger = logging.getLogger(__name__)

# Replace this with the actual MCP server endpoint.
EXAMPLE_MCP_ENDPOINT = "https://mcp.exa.ai/mcp"


def get_streamable_http_mcp_client() -> MCPClient:
    """Return an MCP Client configured for streamable HTTP transport.

    For servers that require bearer token auth, pass:
        headers={"Authorization": f"Bearer {access_token}"}
    """
    return MCPClient(lambda: streamablehttp_client(EXAMPLE_MCP_ENDPOINT))
