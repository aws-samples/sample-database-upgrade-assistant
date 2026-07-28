"""Shared RDS client factory with cross-account and multi-region support.

Provides get_rds_client() which returns a boto3 RDS client that:
- Extracts the region from the cluster ARN (multi-region support)
- Automatically assumes the cross-account role when the cluster ARN
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


def _extract_region_from_arn(cluster_arn):
    """Extract the region from an RDS cluster ARN. Falls back to REGION env var."""
    if cluster_arn and cluster_arn.count(":") >= 4:
        region = cluster_arn.split(":")[3]
        if region:
            return region
    return REGION


def get_rds_client(cluster_arn=""):
    """Return an RDS client, assuming cross-account role if needed.

    Args:
        cluster_arn: The ARN of the cluster being operated on.
                     Region and account are extracted from the ARN.

    Returns:
        boto3 RDS client (either local or with assumed-role credentials)
    """
    target_account = _extract_account_from_arn(cluster_arn)
    target_region = _extract_region_from_arn(cluster_arn)

    if target_account and target_account != _get_current_account_id():
        return _assume_role_rds_client(target_account, target_region)

    return boto3.client("rds", region_name=target_region)


def _assume_role_rds_client(target_account, target_region=None):
    """Assume the cross-account role and return an RDS client with those credentials."""
    if not target_region:
        target_region = REGION
    role_arn = f"arn:aws:iam::{target_account}:role/{CROSS_ACCOUNT_ROLE_NAME}"
    assume_kwargs = {
        "RoleArn": role_arn,
        "RoleSessionName": "upgrade-pipeline",
        "DurationSeconds": 3600,
    }
    if EXTERNAL_ID:
        assume_kwargs["ExternalId"] = EXTERNAL_ID

    logger.info("Assuming cross-account role: %s (region: %s)", role_arn, target_region)
    sts = boto3.client("sts", region_name=REGION)
    creds = sts.assume_role(**assume_kwargs)["Credentials"]

    return boto3.client(
        "rds",
        region_name=target_region,
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
    )
