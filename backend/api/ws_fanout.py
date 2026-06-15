"""
RDS Upgrade Assistant — WebSocket Fan-out Lambda.

Triggered by DynamoDB Streams on the progress-events table.
Reads new progress events, queries the connections table for
clients subscribed to the matching runId, and pushes the event
payload to each connected WebSocket client.
"""

import json
import os

import boto3
from boto3.dynamodb.types import TypeDeserializer

TABLE_PREFIX = os.environ.get("DYNAMODB_TABLE_PREFIX", "aurora-upgrade-planner-")
CONNECTIONS_TABLE = f"{TABLE_PREFIX}connections"
WEBSOCKET_ENDPOINT = os.environ.get("WEBSOCKET_ENDPOINT", "")

dynamodb = boto3.resource("dynamodb")
connections_table = dynamodb.Table(CONNECTIONS_TABLE)
deserializer = TypeDeserializer()


def _deserialize_record(record_image):
    """Convert DynamoDB Stream record image to a plain dict."""
    return {k: deserializer.deserialize(v) for k, v in record_image.items()}


def _get_subscribed_connections(run_id):
    """Query connections table for clients subscribed to a given runId."""
    response = connections_table.scan(
        FilterExpression="subscribedRunId = :rid",
        ExpressionAttributeValues={":rid": run_id},
        ProjectionExpression="connectionId",
    )
    return [item["connectionId"] for item in response.get("Items", [])]


def _post_to_connection(apigw_client, connection_id, payload):
    """Send a message to a WebSocket connection. Remove stale connections."""
    try:
        apigw_client.post_to_connection(
            ConnectionId=connection_id,
            Data=json.dumps(payload, default=str).encode("utf-8"),
        )
    except apigw_client.exceptions.GoneException:
        # Connection is stale — clean up
        try:
            connections_table.delete_item(Key={"connectionId": connection_id})
        except Exception:
            pass
    except Exception:
        # Log but don't fail the batch
        pass


def handler(event, context):
    """Lambda entry point — DynamoDB Streams trigger."""
    if not WEBSOCKET_ENDPOINT:
        return {"statusCode": 200, "body": "No WebSocket endpoint configured"}

    apigw_client = boto3.client(
        "apigatewaymanagementapi",
        endpoint_url=WEBSOCKET_ENDPOINT,
    )

    records = event.get("Records", [])

    for record in records:
        # Only process INSERT events (new progress events)
        if record.get("eventName") != "INSERT":
            continue

        new_image = record.get("dynamodb", {}).get("NewImage")
        if not new_image:
            continue

        progress_event = _deserialize_record(new_image)
        run_id = progress_event.get("runId")
        if not run_id:
            continue

        # Build the payload to send to clients
        payload = {
            "type": "progress",
            "runId": run_id,
            "percentage": progress_event.get("percentage", 0),
            "currentActivity": progress_event.get("currentActivity", ""),
            "elapsedSeconds": progress_event.get("elapsedSeconds", 0),
            "agentType": progress_event.get("agentType", ""),
            "timestamp": progress_event.get("timestamp", ""),
        }

        # Find all subscribed connections and push
        connection_ids = _get_subscribed_connections(run_id)
        for conn_id in connection_ids:
            _post_to_connection(apigw_client, conn_id, payload)

    return {"statusCode": 200, "body": f"Processed {len(records)} records"}
