"""
RDS Upgrade Assistant — WebSocket Connection Manager Lambda.

Handles $connect, $disconnect, and subscribe routes for the
WebSocket API Gateway. Stores/removes connection records in
the DynamoDB connections table.
"""

import json
import os
import time
from datetime import datetime, timezone

import boto3

TABLE_PREFIX = os.environ.get("DYNAMODB_TABLE_PREFIX", "aurora-upgrade-planner-")
CONNECTIONS_TABLE = f"{TABLE_PREFIX}connections"

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(CONNECTIONS_TABLE)


def _ok(body="OK"):
    return {"statusCode": 200, "body": body}


def _error(status_code, message):
    return {"statusCode": status_code, "body": json.dumps({"error": message})}


def handle_connect(connection_id, query_params):
    """Store new WebSocket connection in DynamoDB."""
    now = datetime.now(timezone.utc).isoformat()
    # TTL: auto-cleanup after 2 hours
    ttl = int(time.time()) + 7200

    item = {
        "connectionId": connection_id,
        "connectedAt": now,
        "expiresAt": ttl,
    }

    # Allow subscribing to a runId at connect time via query param
    run_id = (query_params or {}).get("runId")
    if run_id:
        item["subscribedRunId"] = run_id

    table.put_item(Item=item)
    return _ok("Connected")


def handle_disconnect(connection_id):
    """Remove WebSocket connection from DynamoDB."""
    table.delete_item(Key={"connectionId": connection_id})
    return _ok("Disconnected")


def handle_subscribe(connection_id, body):
    """Subscribe a connection to progress events for a specific runId."""
    if not body or "runId" not in body:
        return _error(400, "Missing runId in subscribe message")

    run_id = body["runId"]
    ttl = int(time.time()) + 7200

    table.update_item(
        Key={"connectionId": connection_id},
        UpdateExpression="SET subscribedRunId = :rid, expiresAt = :ttl",
        ExpressionAttributeValues={
            ":rid": run_id,
            ":ttl": ttl,
        },
    )
    return _ok(json.dumps({"subscribed": run_id}))


def handler(event, context):
    """Lambda entry point — WebSocket API Gateway integration."""
    route_key = event.get("requestContext", {}).get("routeKey", "$default")
    connection_id = event.get("requestContext", {}).get("connectionId", "")

    if route_key == "$connect":
        query_params = event.get("queryStringParameters") or {}
        return handle_connect(connection_id, query_params)

    if route_key == "$disconnect":
        return handle_disconnect(connection_id)

    # $default and subscribe routes — parse body for action
    body = {}
    if event.get("body"):
        try:
            body = json.loads(event["body"])
        except (json.JSONDecodeError, TypeError):
            return _error(400, "Invalid JSON body")

    action = body.get("action", route_key)

    if action == "subscribe":
        return handle_subscribe(connection_id, body)

    return _ok(json.dumps({"message": "Unknown action", "action": action}))
