"""
RDS API MCP Server — MCP server over stdio wrapping boto3 RDS API calls.

Provides both MCP server mode (stdio) and direct Python function calls
for use by agents.

Tools:
  - describe_db_engine_versions(engine) → all versions for aurora-mysql or aurora-postgresql
  - describe_db_clusters(cluster_arn) → cluster metadata
  - describe_db_cluster_endpoints(cluster_arn) → cluster endpoints
  - get_valid_upgrade_targets(engine, version) → valid upgrade targets for a version
"""

import json
import logging
import sys

import boto3

logger = logging.getLogger(__name__)


def _get_rds_client(region_name=None):
    """Create a boto3 RDS client."""
    kwargs = {}
    if region_name:
        kwargs["region_name"] = region_name
    return boto3.client("rds", **kwargs)


def describe_db_engine_versions(engine, region_name=None):
    """
    Return all engine versions for the given engine type.

    Args:
        engine: 'aurora-mysql' or 'aurora-postgresql'
        region_name: Optional AWS region override

    Returns:
        List of engine version dicts from the RDS API.
    """
    client = _get_rds_client(region_name)
    versions = []
    paginator = client.get_paginator("describe_db_engine_versions")

    for page in paginator.paginate(Engine=engine):
        for v in page.get("DBEngineVersions", []):
            versions.append({
                "engine": v.get("Engine"),
                "engineVersion": v.get("EngineVersion"),
                "dbParameterGroupFamily": v.get("DBParameterGroupFamily"),
                "status": v.get("Status", "available"),
                "supportsGlobalDatabases": v.get("SupportsGlobalDatabases", False),
                "supportedEngineModes": v.get("SupportedEngineModes", []),
                "validUpgradeTarget": [
                    {
                        "engine": t.get("Engine"),
                        "engineVersion": t.get("EngineVersion"),
                        "autoUpgrade": t.get("AutoUpgrade", False),
                        "isMajorVersionUpgrade": t.get("IsMajorVersionUpgrade", False),
                    }
                    for t in v.get("ValidUpgradeTarget", [])
                ],
            })

    logger.info("describe_db_engine_versions(%s): returned %d versions", engine, len(versions))
    return versions


def describe_db_clusters(cluster_arn, region_name=None):
    """
    Return cluster metadata for a given cluster ARN.

    Args:
        cluster_arn: Full ARN of the Aurora DB cluster
        region_name: Optional AWS region override

    Returns:
        Dict with cluster metadata.
    """
    client = _get_rds_client(region_name)
    resp = client.describe_db_clusters(DBClusterIdentifier=cluster_arn)
    clusters = resp.get("DBClusters", [])

    if not clusters:
        raise ValueError(f"No cluster found for ARN: {cluster_arn}")

    c = clusters[0]
    return {
        "dbClusterIdentifier": c.get("DBClusterIdentifier"),
        "dbClusterArn": c.get("DBClusterArn"),
        "engine": c.get("Engine"),
        "engineVersion": c.get("EngineVersion"),
        "status": c.get("Status"),
        "endpoint": c.get("Endpoint"),
        "readerEndpoint": c.get("ReaderEndpoint"),
        "port": c.get("Port"),
        "masterUsername": c.get("MasterUsername"),
        "dbClusterParameterGroup": c.get("DBClusterParameterGroup"),
        "availabilityZones": c.get("AvailabilityZones", []),
        "tagList": c.get("TagList", []),
    }


def describe_db_cluster_endpoints(cluster_arn, region_name=None):
    """
    Return endpoints for a given Aurora DB cluster.

    Args:
        cluster_arn: Full ARN or identifier of the Aurora DB cluster
        region_name: Optional AWS region override

    Returns:
        List of endpoint dicts for the cluster.
    """
    client = _get_rds_client(region_name)
    try:
        resp = client.describe_db_cluster_endpoints(DBClusterIdentifier=cluster_arn)
        endpoints = []
        for ep in resp.get("DBClusterEndpoints", []):
            endpoints.append({
                "dbClusterEndpointIdentifier": ep.get("DBClusterEndpointIdentifier"),
                "dbClusterIdentifier": ep.get("DBClusterIdentifier"),
                "endpoint": ep.get("Endpoint"),
                "endpointType": ep.get("EndpointType"),
                "status": ep.get("Status"),
                "customEndpointType": ep.get("CustomEndpointType", ""),
            })
        logger.info("describe_db_cluster_endpoints(%s): %d endpoints", cluster_arn, len(endpoints))
        return endpoints
    except Exception as e:
        logger.error("describe_db_cluster_endpoints(%s) failed: %s", cluster_arn, e)
        raise


def get_valid_upgrade_targets(engine, version, region_name=None):
    """
    Return valid upgrade targets for a specific engine version.

    Args:
        engine: 'aurora-mysql' or 'aurora-postgresql'
        version: Current engine version string
        region_name: Optional AWS region override

    Returns:
        List of valid upgrade target dicts.
    """
    client = _get_rds_client(region_name)
    resp = client.describe_db_engine_versions(
        Engine=engine,
        EngineVersion=version,
    )

    versions = resp.get("DBEngineVersions", [])
    if not versions:
        logger.warning("No engine version found for %s %s", engine, version)
        return []

    targets = versions[0].get("ValidUpgradeTarget", [])
    result = [
        {
            "engine": t.get("Engine"),
            "engineVersion": t.get("EngineVersion"),
            "autoUpgrade": t.get("AutoUpgrade", False),
            "isMajorVersionUpgrade": t.get("IsMajorVersionUpgrade", False),
        }
        for t in targets
    ]

    logger.info("get_valid_upgrade_targets(%s, %s): %d targets", engine, version, len(result))
    return result


# ---------------------------------------------------------------------------
# MCP Server over stdio
# ---------------------------------------------------------------------------

TOOLS = {
    "describe_db_engine_versions": {
        "description": "Returns all Aurora engine versions for the specified engine type using the RDS DescribeDBEngineVersions API with pagination.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "engine": {
                    "type": "string",
                    "description": "Engine type: 'aurora-mysql' or 'aurora-postgresql'",
                    "enum": ["aurora-mysql", "aurora-postgresql"],
                },
            },
            "required": ["engine"],
        },
    },
    "describe_db_clusters": {
        "description": "Returns cluster metadata for a given Aurora DB cluster ARN.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "cluster_arn": {
                    "type": "string",
                    "description": "Full ARN of the Aurora DB cluster",
                },
            },
            "required": ["cluster_arn"],
        },
    },
    "describe_db_cluster_endpoints": {
        "description": "Returns endpoints for a given Aurora DB cluster.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "cluster_arn": {
                    "type": "string",
                    "description": "Full ARN or identifier of the Aurora DB cluster",
                },
            },
            "required": ["cluster_arn"],
        },
    },
    "get_valid_upgrade_targets": {
        "description": "Returns valid upgrade targets for a specific Aurora engine version.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "engine": {
                    "type": "string",
                    "description": "Engine type: 'aurora-mysql' or 'aurora-postgresql'",
                    "enum": ["aurora-mysql", "aurora-postgresql"],
                },
                "version": {
                    "type": "string",
                    "description": "Current engine version string",
                },
            },
            "required": ["engine", "version"],
        },
    },
}


def _handle_tool_call(tool_name, arguments):
    """Dispatch a tool call and return the result."""
    if tool_name == "describe_db_engine_versions":
        result = describe_db_engine_versions(arguments["engine"])
        return json.dumps(result, default=str)
    elif tool_name == "describe_db_clusters":
        result = describe_db_clusters(arguments["cluster_arn"])
        return json.dumps(result, default=str)
    elif tool_name == "describe_db_cluster_endpoints":
        result = describe_db_cluster_endpoints(arguments["cluster_arn"])
        return json.dumps(result, default=str)
    elif tool_name == "get_valid_upgrade_targets":
        result = get_valid_upgrade_targets(arguments["engine"], arguments["version"])
        return json.dumps(result, default=str)
    else:
        raise ValueError(f"Unknown tool: {tool_name}")


def _send_response(response):
    """Write a JSON-RPC response to stdout."""
    msg = json.dumps(response) + "\n"
    sys.stdout.write(msg)
    sys.stdout.flush()


def _handle_message(message):
    """Handle a single JSON-RPC message."""
    method = message.get("method", "")
    msg_id = message.get("id")
    params = message.get("params", {})

    if method == "initialize":
        _send_response({
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {
                    "name": "rds-api-mcp",
                    "version": "1.0.0",
                },
            },
        })
    elif method == "notifications/initialized":
        pass  # No response needed for notifications
    elif method == "tools/list":
        tools_list = [
            {"name": name, "description": spec["description"], "inputSchema": spec["inputSchema"]}
            for name, spec in TOOLS.items()
        ]
        _send_response({
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {"tools": tools_list},
        })
    elif method == "tools/call":
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})
        try:
            result_text = _handle_tool_call(tool_name, arguments)
            _send_response({
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "content": [{"type": "text", "text": result_text}],
                    "isError": False,
                },
            })
        except Exception as e:
            _send_response({
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "content": [{"type": "text", "text": f"Error: {e}"}],
                    "isError": True,
                },
            })
    else:
        if msg_id is not None:
            _send_response({
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": -32601, "message": f"Method not found: {method}"},
            })


def run_stdio_server():
    """Run the MCP server over stdio, reading JSON-RPC messages line by line."""
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    logger.info("RDS API MCP Server starting (stdio mode)")

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
            _handle_message(message)
        except json.JSONDecodeError:
            sys.stderr.write(f"Invalid JSON: {line}\n")
            sys.stderr.flush()


if __name__ == "__main__":
    run_stdio_server()
