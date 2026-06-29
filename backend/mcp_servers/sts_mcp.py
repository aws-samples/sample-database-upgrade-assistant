"""
STS MCP Server — Wraps boto3 STS AssumeRole for cross-account access.

Provides both MCP server mode (stdio) and direct Python function calls
for use by agents.

Tools:
  - assume_role(account_id, role_name, external_id, region) → temporary credentials
"""

import json
import logging
import sys

import boto3

logger = logging.getLogger(__name__)

DEFAULT_ROLE_NAME = "aurora-upgrade-planner-cross-account-role"
DEFAULT_SESSION_DURATION = 3600  # 1 hour


def assume_role(account_id, role_name=None, external_id=None, region=None):
    """
    Assume an IAM role in the target account and return temporary credentials.

    Args:
        account_id: AWS account ID to assume role in
        role_name: IAM role name (default: aurora-upgrade-planner-cross-account-role)
        external_id: ExternalId for the trust policy
        region: AWS region for the STS client

    Returns:
        dict with keys: access_key_id, secret_access_key, session_token, expiration

    Raises:
        Exception with descriptive message on failure
    """
    if not role_name:
        role_name = DEFAULT_ROLE_NAME

    role_arn = f"arn:aws:iam::{account_id}:role/{role_name}"

    kwargs = {}
    if region:
        kwargs["region_name"] = region

    sts_client = boto3.client("sts", **kwargs)

    assume_kwargs = {
        "RoleArn": role_arn,
        "RoleSessionName": f"aurora-upgrade-planner-{account_id}",
        "DurationSeconds": DEFAULT_SESSION_DURATION,
    }
    if external_id:
        assume_kwargs["ExternalId"] = external_id

    try:
        response = sts_client.assume_role(**assume_kwargs)
        creds = response["Credentials"]
        logger.info("Successfully assumed role %s in account %s", role_arn, account_id)
        return {
            "access_key_id": creds["AccessKeyId"],
            "secret_access_key": creds["SecretAccessKey"],
            "session_token": creds["SessionToken"],
            "expiration": creds["Expiration"].isoformat() if hasattr(creds["Expiration"], "isoformat") else str(creds["Expiration"]),
        }
    except Exception as e:
        error_msg = str(e)
        if "AccessDenied" in error_msg:
            raise Exception(
                f"Role assumption failed for account {account_id}. "
                f"Verify the cross-account role '{role_name}' exists and "
                f"the ExternalId matches the trust policy. Error: {error_msg}"
            )
        elif "MalformedPolicyDocument" in error_msg:
            raise Exception(
                f"Role '{role_name}' in account {account_id} has a malformed "
                f"trust policy. Error: {error_msg}"
            )
        else:
            raise Exception(
                f"Failed to assume role '{role_name}' in account {account_id}: {error_msg}"
            )


def get_session_from_credentials(credentials, region=None):
    """
    Create a boto3 Session from assumed-role credentials.

    Args:
        credentials: dict from assume_role() with access_key_id, secret_access_key, session_token
        region: AWS region

    Returns:
        boto3.Session configured with the temporary credentials
    """
    kwargs = {
        "aws_access_key_id": credentials["access_key_id"],
        "aws_secret_access_key": credentials["secret_access_key"],
        "aws_session_token": credentials["session_token"],
    }
    if region:
        kwargs["region_name"] = region
    return boto3.Session(**kwargs)
