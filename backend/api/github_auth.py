"""
GitHub Device Flow OAuth — Route handlers and helpers.

Handles all /github/* routes for the RDS Upgrade Assistant API.
"""

import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import os

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Config from environment (same pattern as handler.py — no shared module dependency)
TABLE_PREFIX = os.environ.get("DYNAMODB_TABLE_PREFIX", "aurora-upgrade-planner-")
REGION = os.environ.get("AWS_REGION", "us-east-1")

# Constants
GITHUB_SECRET_NAME = "aurora-upgrade-planner/github-token"
SETTINGS_TABLE = f"{TABLE_PREFIX}settings"

# Lazy-init AWS clients (created on first use inside Lambda)
_secrets_client = None
_ddb_resource = None


def _get_secrets_client():
    global _secrets_client
    if _secrets_client is None:
        _secrets_client = boto3.client("secretsmanager", region_name=REGION)
    return _secrets_client


def _get_ddb_resource():
    global _ddb_resource
    if _ddb_resource is None:
        _ddb_resource = boto3.resource("dynamodb", region_name=REGION)
    return _ddb_resource


# ---------------------------------------------------------------------------
# Response helper
# ---------------------------------------------------------------------------

CORS_HEADERS = {
    "Content-Type": "application/json",
    "Access-Control-Allow-Headers": "Content-Type,Authorization",
    "Access-Control-Allow-Methods": "GET,POST,PUT,DELETE,OPTIONS",
    "Access-Control-Allow-Origin": "*",
}


def _response(status_code, body):
    """Build an API Gateway proxy response with CORS headers."""
    return {
        "statusCode": status_code,
        "headers": dict(CORS_HEADERS),
        "body": json.dumps(body, default=str),
    }


# ---------------------------------------------------------------------------
# Internal helpers — Secrets Manager
# ---------------------------------------------------------------------------


def _get_github_secret():
    """Retrieve the GitHub secret from Secrets Manager.

    Returns parsed JSON dict, or None if the secret does not exist.
    """
    try:
        resp = _get_secrets_client().get_secret_value(SecretId=GITHUB_SECRET_NAME)
        return json.loads(resp["SecretString"])
    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceNotFoundException":
            return None
        raise


def _put_github_secret(data: dict):
    """Create or update the GitHub secret in Secrets Manager.

    If the secret does not exist, it is created. If it already exists, its
    value is updated.
    """
    secret_string = json.dumps(data)
    client = _get_secrets_client()
    try:
        client.put_secret_value(
            SecretId=GITHUB_SECRET_NAME,
            SecretString=secret_string,
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceNotFoundException":
            # Secret doesn't exist yet — create it
            client.create_secret(
                Name=GITHUB_SECRET_NAME,
                SecretString=secret_string,
            )
        else:
            raise


def _delete_github_secret():
    """Delete the GitHub secret from Secrets Manager.

    Uses ForceDeleteWithoutRecovery=True. Gracefully handles the case where
    the secret does not exist.
    """
    try:
        _get_secrets_client().delete_secret(
            SecretId=GITHUB_SECRET_NAME,
            ForceDeleteWithoutRecovery=True,
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceNotFoundException":
            return  # Already gone — nothing to do
        raise


# ---------------------------------------------------------------------------
# Internal helpers — DynamoDB metadata
# ---------------------------------------------------------------------------


def _get_github_metadata():
    """Get GitHub metadata from the DynamoDB settings table.

    Returns the item dict, or an empty dict if not found.
    """
    table = _get_ddb_resource().Table(SETTINGS_TABLE)
    resp = table.get_item(Key={"settingKey": "github"})
    return resp.get("Item", {})


def _put_github_metadata(metadata: dict):
    """Write GitHub metadata to the DynamoDB settings table.

    Stores the metadata fields alongside settingKey="github".
    """
    table = _get_ddb_resource().Table(SETTINGS_TABLE)
    item = {"settingKey": "github", **metadata}
    table.put_item(Item=item)


# ---------------------------------------------------------------------------
# Internal helpers — Validation and masking
# ---------------------------------------------------------------------------

# 20 alphanumeric characters
_CLIENT_ID_PLAIN = re.compile(r"^[A-Za-z0-9]{20}$")
# GitHub "Iv1." prefix format: Iv1. followed by 16 hex characters
_CLIENT_ID_IV1 = re.compile(r"^Iv1\.[0-9a-fA-F]{16}$")


def _validate_client_id(client_id: str) -> bool:
    """Validate a GitHub OAuth App client_id.

    Accepts either:
    - Exactly 20 alphanumeric characters, OR
    - Starts with "Iv1." followed by 16 hex characters (GitHub's format).
    """
    if not client_id:
        return False
    return bool(_CLIENT_ID_PLAIN.match(client_id) or _CLIENT_ID_IV1.match(client_id))


def _mask_client_id(client_id: str) -> str:
    """Mask a client_id, showing only the last 4 characters.

    Returns bullet characters (•) for all chars except the last 4,
    followed by the last 4 chars.
    E.g., "Iv1.a1b2c3d4e5f6g7h8" → "••••••••••••••••g7h8"
    """
    if len(client_id) <= 4:
        return client_id
    masked_len = len(client_id) - 4
    return "\u2022" * masked_len + client_id[-4:]


def _parse_body(event):
    """Safely parse the JSON body from an API Gateway event.

    Returns the parsed dict, or an empty dict if parsing fails.
    """
    body = event.get("body")
    if not body:
        return {}
    try:
        return json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return {}


# ---------------------------------------------------------------------------
# Route handlers (stubs — implemented in subsequent tasks)
# ---------------------------------------------------------------------------


def handle_save_client_id(event):
    """PUT /github/client-id — Save or update the GitHub OAuth App client_id."""
    body = _parse_body(event)
    client_id = body.get("clientId", "")

    # Validate format
    if not _validate_client_id(client_id):
        return _response(400, {
            "error": "Invalid client_id: must be 20 alphanumeric characters or Iv1. prefix format"
        })

    # Read existing secret to preserve accessToken if already connected
    existing_secret = _get_github_secret()
    if existing_secret:
        existing_secret["clientId"] = client_id
        secret_data = existing_secret
    else:
        secret_data = {"clientId": client_id}

    # Store/update in Secrets Manager
    _put_github_secret(secret_data)

    # Store masked client_id in DynamoDB metadata
    masked = _mask_client_id(client_id)
    _put_github_metadata({"maskedClientId": masked})

    logger.info("Client ID saved (masked: %s)", masked)

    return _response(200, {"saved": True, "maskedClientId": masked})


def handle_initiate_device_flow(event):
    """POST /github/device-code — Initiate the GitHub device flow."""
    # Step 1: Read client_id from Secrets Manager
    secret = _get_github_secret()
    if not secret or not secret.get("clientId"):
        return _response(400, {
            "error": "No client_id configured. Save a client ID first."
        })

    client_id = secret["clientId"]

    # Step 2: POST to GitHub's device code endpoint
    form_data = urllib.parse.urlencode({
        "client_id": client_id,
        "scope": "repo",
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://github.com/login/device/code",
        data=form_data,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req) as resp:
            gh_response = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        logger.error("GitHub device/code request failed: HTTP %s", e.code)
        return _response(502, {
            "error": f"GitHub returned HTTP {e.code}",
        })
    except (urllib.error.URLError, OSError) as e:
        logger.error("GitHub device/code request error: %s", str(e))
        return _response(502, {
            "error": "Failed to reach GitHub. Please try again.",
        })

    # Step 3: Check for error in GitHub's response
    if "error" in gh_response:
        error_desc = gh_response.get("error_description", gh_response["error"])
        logger.warning("GitHub device/code error: %s", error_desc)
        return _response(502, {"error": error_desc})

    # Step 4: Return device flow data to frontend
    logger.info("Device flow initiated successfully")
    return _response(200, {
        "userCode": gh_response.get("user_code", ""),
        "verificationUri": gh_response.get("verification_uri", ""),
        "expiresIn": gh_response.get("expires_in", 900),
        "interval": gh_response.get("interval", 5),
        "deviceCode": gh_response.get("device_code", ""),
    })


def handle_poll_token(event):
    """POST /github/poll-token — Poll GitHub for access token."""
    # Step 1: Parse deviceCode from request body
    body = _parse_body(event)
    device_code = body.get("deviceCode")
    if not device_code:
        return _response(400, {"error": "Missing required field: deviceCode"})

    # Step 2: Read client_id from Secrets Manager
    secret = _get_github_secret()
    if not secret or not secret.get("clientId"):
        return _response(400, {
            "error": "No client_id configured. Save a client ID first."
        })

    client_id = secret["clientId"]

    # Step 3: POST to GitHub's access token endpoint
    form_data = urllib.parse.urlencode({
        "client_id": client_id,
        "device_code": device_code,
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://github.com/login/oauth/access_token",
        data=form_data,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req) as resp:
            gh_response = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        logger.error("GitHub access_token request failed: HTTP %s", e.code)
        return _response(502, {
            "error": f"GitHub returned HTTP {e.code}",
        })
    except (urllib.error.URLError, OSError) as e:
        logger.error("GitHub access_token request error: %s", str(e))
        return _response(502, {
            "error": "Failed to reach GitHub. Please try again.",
        })

    # Step 4: Handle GitHub response statuses
    if "access_token" in gh_response:
        # Success — fetch username, store secret, update metadata
        access_token = gh_response["access_token"]
        scope = gh_response.get("scope", "")

        # Fetch GitHub username
        username = _fetch_github_username(access_token)

        # Generate timestamp
        authorized_at = datetime.now(timezone.utc).isoformat()

        # Store full secret in Secrets Manager
        _put_github_secret({
            "accessToken": access_token,
            "clientId": client_id,
            "scope": scope,
            "username": username,
            "authorizedAt": authorized_at,
        })

        # Update DynamoDB metadata (never store the token here)
        masked = _mask_client_id(client_id)
        _put_github_metadata({
            "connectionStatus": "connected",
            "username": username,
            "authorizedAt": authorized_at,
            "maskedClientId": masked,
        })

        logger.info("GitHub token stored for user %s", username)

        return _response(200, {
            "status": "connected",
            "username": username,
            "authorizedAt": authorized_at,
        })

    # Handle error statuses from GitHub
    error = gh_response.get("error", "")

    if error == "authorization_pending":
        return _response(200, {"status": "pending"})

    if error == "slow_down":
        # Increase interval by 5 seconds
        current_interval = body.get("interval", 5)
        try:
            current_interval = int(current_interval)
        except (TypeError, ValueError):
            current_interval = 5
        return _response(200, {"status": "pending", "interval": current_interval + 5})

    if error == "expired_token":
        return _response(200, {
            "status": "expired",
            "message": "Device code expired. Please restart the flow.",
        })

    if error == "access_denied":
        return _response(200, {
            "status": "denied",
            "message": "Authorization was declined by the user.",
        })

    # Any other error — return 502 with error_description
    error_desc = gh_response.get("error_description", error)
    logger.error("GitHub poll-token unexpected error: %s", error_desc)
    return _response(502, {"error": error_desc})


def _fetch_github_username(access_token: str) -> str:
    """Fetch the authenticated GitHub username via the /user API.

    Returns the username string, or "unknown" if the request fails.
    Never logs the access_token value.
    """
    req = urllib.request.Request(
        "https://api.github.com/user",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "User-Agent": "aurora-upgrade-planner",
        },
        method="GET",
    )

    try:
        with urllib.request.urlopen(req) as resp:
            user_data = json.loads(resp.read().decode("utf-8"))
            return user_data.get("login", "unknown")
    except (urllib.error.HTTPError, urllib.error.URLError, OSError) as e:
        logger.warning("Failed to fetch GitHub username: %s", str(e))
        return "unknown"


def handle_get_status(event):
    """GET /github/status — Get current GitHub connection status."""
    metadata = _get_github_metadata()

    connection_status = metadata.get("connectionStatus", "")

    if connection_status == "connected":
        return _response(200, {
            "status": "connected",
            "username": metadata.get("username", ""),
            "authorizedAt": metadata.get("authorizedAt", ""),
            "maskedClientId": metadata.get("maskedClientId", ""),
        })

    if connection_status == "expired":
        return _response(200, {
            "status": "expired",
            "maskedClientId": metadata.get("maskedClientId", ""),
        })

    return _response(200, {"status": "disconnected"})


def handle_disconnect(event):
    """DELETE /github/connection — Disconnect GitHub (delete secret + clear metadata)."""
    # Delete the secret from Secrets Manager
    _delete_github_secret()

    # Update DynamoDB metadata to disconnected status
    _put_github_metadata({
        "connectionStatus": "disconnected",
        "username": "",
        "authorizedAt": "",
        "maskedClientId": "",
    })

    logger.info("GitHub connection disconnected")

    return _response(200, {"disconnected": True})
