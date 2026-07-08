"""Clone Lambda — Creates a point-in-time clone or restores from snapshot.

Triggered by: Baseline Lambda (async invoke)
Input: {runId, clusterArn, engine, targetVersion, currentVersion,
        cloneMode?, snapshotId?, snapshotAccount?, snapshotRoleArn?, snapshotKmsKey?}
Output: Submits clone/restore, queues Poller to wait for it

Clone modes:
  - "clone" (default): Fast point-in-time clone using copy-on-write
  - "snapshot": Restore from a user-provided snapshot identifier
    Supports cross-account via snapshotAccount + snapshotRoleArn + snapshotKmsKey
"""

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
from shared.state import update_phase, update_run, mark_failed
from shared.rds_client import get_rds_client

rds = None  # initialized per-invocation in handler


def handler(event, context):
    """Lambda entry point."""
    global rds
    run_id = event["runId"]
    cluster_arn = event.get("clusterArn", "")
    engine = event.get("engine", "")
    current_version = event.get("currentVersion", "")
    clone_mode = event.get("cloneMode", "clone")

    # If engine/clusterArn not in event, read from run state
    if not cluster_arn or not engine:
        from shared.state import get_run
        run = get_run(run_id)
        if run:
            cluster_arn = cluster_arn or run.get("clusterArn", "")
            engine = engine or run.get("engine", "aurora-mysql")
            current_version = current_version or run.get("sourceVersion", "")

    # Initialize RDS client (cross-account if needed)
    rds = get_rds_client(cluster_arn)

    short_id = run_id[:8]
    clone_id = f"aurora-planner-test-{short_id}"

    try:
        update_phase(run_id, "clone", progress_pct=20)
        cluster_id = cluster_arn.split(":")[-1]

        if clone_mode == "snapshot":
            snapshot_id = event.get("snapshotId", "")
            snapshot_account = event.get("snapshotAccount", "")
            snapshot_role_arn = event.get("snapshotRoleArn", "")
            snapshot_kms_key = event.get("snapshotKmsKey", "")

            if not snapshot_id:
                mark_failed(run_id, "cloneMode=snapshot but no snapshotId provided")
                return

            _clone_from_snapshot(
                run_id=run_id,
                clone_id=clone_id,
                snapshot_id=snapshot_id,
                snapshot_account=snapshot_account,
                snapshot_role_arn=snapshot_role_arn,
                snapshot_kms_key=snapshot_kms_key,
                engine=engine,
                current_version=current_version,
                cluster_id=cluster_id,
                short_id=short_id,
            )
        else:
            _clone_point_in_time(
                run_id=run_id,
                clone_id=clone_id,
                cluster_id=cluster_id,
                engine=engine,
                current_version=current_version,
            )

        # Save clone ID to run state
        update_run(run_id, clonedClusterId=clone_id)

        # Return payload for Step Functions — it will poll for clone readiness
        return {"runId": run_id, "cloneId": clone_id}

    except Exception as e:
        logger.exception("Clone failed: %s", e)
        mark_failed(run_id, str(e))
        raise


def _clone_point_in_time(run_id, clone_id, cluster_id, engine, current_version):
    """Fast clone using point-in-time restore (copy-on-write, no snapshot needed)."""
    logger.info("Cloning %s → %s (point-in-time copy-on-write)", cluster_id, clone_id)

    # Get source cluster details for clone config
    resp = rds.describe_db_clusters(DBClusterIdentifier=cluster_id)
    source = resp["DBClusters"][0]
    db_subnet_group = source.get("DBSubnetGroup", "")
    vpc_sg_ids = [sg["VpcSecurityGroupId"] for sg in source.get("VpcSecurityGroups", [])]

    restore_kwargs = {
        "DBClusterIdentifier": clone_id,
        "SourceDBClusterIdentifier": cluster_id,
        "RestoreType": "copy-on-write",
        "UseLatestRestorableTime": True,
        "DBSubnetGroupName": db_subnet_group,
    }
    if vpc_sg_ids:
        restore_kwargs["VpcSecurityGroupIds"] = vpc_sg_ids

    rds.restore_db_cluster_to_point_in_time(**restore_kwargs)
    logger.info("Point-in-time clone initiated: %s from %s", clone_id, cluster_id)


def _clone_from_snapshot(run_id, clone_id, snapshot_id, snapshot_account,
                         snapshot_role_arn, snapshot_kms_key, engine,
                         current_version, cluster_id, short_id):
    """Restore from a user-provided snapshot. Supports cross-account via role assumption."""
    logger.info("Restoring from snapshot %s → %s", snapshot_id, clone_id)

    # Cross-account: copy snapshot into current account first
    if snapshot_account and snapshot_role_arn:
        snapshot_id = _copy_cross_account_snapshot(
            source_snapshot_id=snapshot_id,
            snapshot_role_arn=snapshot_role_arn,
            snapshot_kms_key=snapshot_kms_key,
            short_id=short_id,
            run_id=run_id,
        )

    # Get snapshot details for subnet/SG config
    try:
        resp = rds.describe_db_cluster_snapshots(DBClusterSnapshotIdentifier=snapshot_id)
        snap = resp["DBClusterSnapshots"][0]
        db_subnet_group = snap.get("DBSubnetGroup", "")
        vpc_sg_ids = [sg["VpcSecurityGroupId"] for sg in snap.get("VpcSecurityGroups", [])]
    except Exception as e:
        logger.warning("Could not describe snapshot %s, using source cluster config: %s", snapshot_id, e)
        # Fall back to source cluster config
        resp = rds.describe_db_clusters(DBClusterIdentifier=cluster_id)
        source = resp["DBClusters"][0]
        db_subnet_group = source.get("DBSubnetGroup", "")
        vpc_sg_ids = [sg["VpcSecurityGroupId"] for sg in source.get("VpcSecurityGroups", [])]

    restore_kwargs = {
        "DBClusterIdentifier": clone_id,
        "SnapshotIdentifier": snapshot_id,
        "Engine": engine,
        "EngineVersion": current_version,
        "Tags": [
            {"Key": "rds-upgrade-test", "Value": "true"},
            {"Key": "runId", "Value": run_id},
            {"Key": "auto-delete", "Value": "true"},
        ],
    }
    if db_subnet_group:
        restore_kwargs["DBSubnetGroupName"] = db_subnet_group
    if vpc_sg_ids:
        restore_kwargs["VpcSecurityGroupIds"] = vpc_sg_ids

    rds.restore_db_cluster_from_snapshot(**restore_kwargs)
    logger.info("Snapshot restore initiated: %s from %s", clone_id, snapshot_id)

    # Store the snapshot ID used so cleanup can delete the local copy if needed
    update_run(run_id, snapshotId=snapshot_id)


def _copy_cross_account_snapshot(source_snapshot_id, snapshot_role_arn,
                                 snapshot_kms_key, short_id, run_id):
    """Copy a snapshot from another account into the current account."""
    logger.info("Copying cross-account snapshot %s via role %s", source_snapshot_id, snapshot_role_arn)

    # Assume role in source account
    sts = boto3.client("sts", region_name=REGION)
    creds = sts.assume_role(
        RoleArn=snapshot_role_arn,
        RoleSessionName=f"upgrade-snap-copy-{short_id}",
    )["Credentials"]

    src_rds = boto3.client(
        "rds", region_name=REGION,
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
    )

    # Get source snapshot ARN
    resp = src_rds.describe_db_cluster_snapshots(DBClusterSnapshotIdentifier=source_snapshot_id)
    source_arn = resp["DBClusterSnapshots"][0]["DBClusterSnapshotArn"]

    local_snap_id = f"aurora-planner-copy-{short_id}"
    copy_kwargs = {
        "SourceDBClusterSnapshotIdentifier": source_arn,
        "TargetDBClusterSnapshotIdentifier": local_snap_id,
        "SourceRegion": REGION,
        "Tags": [
            {"Key": "runId", "Value": run_id},
            {"Key": "auto-delete", "Value": "true"},
        ],
    }
    if snapshot_kms_key:
        copy_kwargs["KmsKeyId"] = snapshot_kms_key
        copy_kwargs["CopyTags"] = True

    rds.copy_db_cluster_snapshot(**copy_kwargs)
    logger.info("Cross-account snapshot copy initiated: %s", local_snap_id)

    # Wait for copy to complete (up to ~20 min)
    for i in range(40):
        time.sleep(30)
        try:
            resp = rds.describe_db_cluster_snapshots(DBClusterSnapshotIdentifier=local_snap_id)
            snap = resp["DBClusterSnapshots"][0]
            status = snap.get("Status", "")
            if status == "available":
                logger.info("Snapshot copy complete: %s", local_snap_id)
                return local_snap_id
            if status in ("failed", "deleted"):
                raise Exception(f"Snapshot copy failed with status: {status}")
        except ClientError as e:
            if "DBClusterSnapshotNotFound" in str(e):
                continue
            raise

    raise Exception("Snapshot copy timed out after 20 minutes")
