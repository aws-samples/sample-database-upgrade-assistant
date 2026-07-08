"""Shared RDS client factory with cross-account support.

Provides get_rds_client() which returns a boto3 RDS client that
automatically assumes the cross-account role when the cluster ARN
belongs to a different AWS account.
"""

import logging
import os

import boto3

from .config import REGION

logger = logging.getLogger(__name__)

EXTERNAL_ID = os.environ.get("EXTERNAL_ID", "")
CROSS_ACCOUNT_ROLE_NAME = "aurora-upgrade-planner-cross-account-role"

_current_account_id = None


def _get_current_account_id():
    """Get the current Lambda's AWS account ID (cached)."""
    global _current_account_id
    if _current_account_id is None:
        _current_account_id = boto3.client("sts", region_name=REGION).get_caller_identity()["Account"]
    return _current_account_id


def _extract_account_from_arn(cluster_arn):
    """Extract the account ID from an RDS cluster ARN."""
    if not cluster_arn or cluster_arn.count(":") < 5:
        return ""
    return cluster_arn.split(":")[4]


def get_rds_client(cluster_arn=""):
    """Return an RDS client, assuming cross-account role if needed.

    Args:
        cluster_arn: The ARN of the cluster being operated on.
                     If the account in the ARN differs from the Lambda's account,
                     assumes the cross-account role automatically.

    Returns:
        boto3 RDS client (either local or with assumed-role credentials)
    """
    target_account = _extract_account_from_arn(cluster_arn)

    if target_account and target_account != _get_current_account_id():
        return _assume_role_rds_client(target_account)

    return boto3.client("rds", region_name=REGION)


def _assume_role_rds_client(target_account):
    """Assume the cross-account role and return an RDS client with those credentials."""
    role_arn = f"arn:aws:iam::{target_account}:role/{CROSS_ACCOUNT_ROLE_NAME}"
    assume_kwargs = {
        "RoleArn": role_arn,
        "RoleSessionName": "upgrade-pipeline",
        "DurationSeconds": 3600,
    }
    if EXTERNAL_ID:
        assume_kwargs["ExternalId"] = EXTERNAL_ID

    logger.info("Assuming cross-account role: %s", role_arn)
    sts = boto3.client("sts", region_name=REGION)
    creds = sts.assume_role(**assume_kwargs)["Credentials"]

    return boto3.client(
        "rds",
        region_name=REGION,
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
    )
