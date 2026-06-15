"""Cleanup Lambda — Deletes the clone cluster, instance, and any test snapshots.

Triggered by: Validation Lambda (async invoke)
Input: {runId, cloneId, snapshotId?}
Output: Best-effort deletion of clone resources and test snapshots

Handles:
  - Clone instance deletion (waits for completion)
  - Clone cluster deletion
  - Snapshot deletion (if snapshotId provided in event or run state)
  - Cross-account snapshot copies are cleaned up gracefully
"""

import json
import logging
import os
import sys
import time

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from shared.config import REGION
from shared.state import get_run

rds = boto3.client("rds", region_name=REGION)


def handler(event, context):
    """Lambda entry point."""
    run_id = event.get("runId", "")
    clone_id = event.get("cloneId", "")
    snapshot_id = event.get("snapshotId", "")

    # If no cloneId in event, look it up from DynamoDB run state
    if not clone_id and run_id:
        try:
            run_state = get_run(run_id)
            if run_state:
                clone_id = run_state.get("clonedClusterId", "")
                if not snapshot_id:
                    snapshot_id = run_state.get("snapshotId", "")
        except Exception as e:
            logger.warning("Could not read run state for clone lookup: %s", e)

    # If no snapshotId in event, check run state
    if not snapshot_id and run_id:
        try:
            run_state = get_run(run_id)
            if run_state:
                snapshot_id = run_state.get("snapshotId", "")
        except Exception as e:
            logger.warning("Could not read run state for snapshot cleanup: %s", e)

    if not clone_id and not snapshot_id:
        logger.warning("No cloneId or snapshotId provided — nothing to clean up")
        return

    logger.info("Cleaning up for run %s: clone=%s, snapshot=%s", run_id, clone_id, snapshot_id)

    # --- Delete clone instance and cluster ---
    if clone_id:
        _delete_clone(clone_id)

    # --- Delete snapshot (local copy from cross-account or test snapshot) ---
    if snapshot_id:
        _delete_snapshot(snapshot_id)


def _delete_clone(clone_id):
    """Delete the clone instance and cluster. Best-effort, logs warnings on failure."""
    # Delete instance first
    instance_id = f"{clone_id}-instance"
    try:
        rds.delete_db_instance(
            DBInstanceIdentifier=instance_id,
            SkipFinalSnapshot=True,
        )
        logger.info("Deleted instance: %s", instance_id)
        # Wait briefly for instance deletion
        for _ in range(10):
            time.sleep(30)
            try:
                rds.describe_db_instances(DBInstanceIdentifier=instance_id)
            except ClientError as e:
                if "DBInstanceNotFound" in str(e):
                    break
    except ClientError as e:
        if "DBInstanceNotFound" not in str(e):
            logger.warning("Could not delete instance %s: %s", instance_id, e)

    # Delete cluster
    try:
        rds.delete_db_cluster(
            DBClusterIdentifier=clone_id,
            SkipFinalSnapshot=True,
        )
        logger.info("Deleted clone cluster: %s", clone_id)
    except ClientError as e:
        if "DBClusterNotFound" not in str(e):
            logger.warning("Could not delete clone %s: %s", clone_id, e)


def _delete_snapshot(snapshot_id):
    """Delete a snapshot created during the test. Handles not-found gracefully."""
    try:
        rds.delete_db_cluster_snapshot(
            DBClusterSnapshotIdentifier=snapshot_id,
        )
        logger.info("Deleted snapshot: %s", snapshot_id)
    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "")
        if error_code == "DBClusterSnapshotNotFoundFault":
            logger.info("Snapshot %s already deleted or not found — skipping", snapshot_id)
        elif "InvalidDBClusterSnapshotState" in str(e):
            logger.warning("Snapshot %s is in use or not deletable: %s", snapshot_id, e)
        else:
            logger.warning("Could not delete snapshot %s: %s", snapshot_id, e)
