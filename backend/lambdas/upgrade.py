"""Upgrade Lambda — Submits the engine version upgrade on the clone.

Triggered by: Step Functions (after clone is ready via Poller)
Input: {runId, cloneId}
Output: Calls ModifyDBCluster with target default PG, returns payload for polling

Handles both Aurora MySQL and Aurora PostgreSQL via a single ModifyDBCluster call
that includes the target default parameter group (e.g. default.aurora-mysql8.0).
This avoids InvalidParameterCombination errors when the source cluster uses a
custom parameter group.
"""

import logging
import os
import sys

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from shared.config import REGION
from shared.state import get_run, update_phase, mark_failed

rds = boto3.client("rds", region_name=REGION)


def handler(event, context):
    """Lambda entry point."""
    run_id = event["runId"]
    clone_id = event["cloneId"]

    try:
        update_phase(run_id, "upgrade", progress_pct=40)

        # Get target version and engine from run state
        run = get_run(run_id)
        target_version = run.get("targetVersion", "")
        engine = run.get("engine", "aurora-mysql")
        if not target_version:
            mark_failed(run_id, "No targetVersion in run state")
            return

        # Determine the default parameter group for the target version
        target_pg = _get_default_pg_for_version(engine, target_version)

        # Get writer instance ID (needed for status verification)
        resp = rds.describe_db_clusters(DBClusterIdentifier=clone_id)
        cluster_info = resp["DBClusters"][0]
        members = cluster_info.get("DBClusterMembers", [])
        writer_id = next(
            (m["DBInstanceIdentifier"] for m in members if m.get("IsClusterWriter")), None
        )
        if not writer_id:
            mark_failed(run_id, f"No writer instance found in cluster {clone_id}")
            return

        logger.info("Upgrading %s (%s) → %s (target PG: %s)",
                    clone_id, engine, target_version, target_pg)

        # Wait for cluster to be fully available (may be in transitional state)
        _wait_for_available(clone_id)

        # Both MySQL and PostgreSQL: single ModifyDBCluster call with target PG
        modify_kwargs = {
            "DBClusterIdentifier": clone_id,
            "EngineVersion": target_version,
            "AllowMajorVersionUpgrade": True,
            "ApplyImmediately": True,
        }
        if target_pg:
            modify_kwargs["DBClusterParameterGroupName"] = target_pg
        rds.modify_db_cluster(**modify_kwargs)

        logger.info("ModifyDBCluster submitted for %s → %s", clone_id, target_version)

        # Return payload for Step Functions — Poller will wait for upgrade completion
        return {"runId": run_id, "cloneId": clone_id}

    except Exception as e:
        logger.exception("Upgrade submission failed: %s", e)
        mark_failed(run_id, str(e))
        raise


def _get_default_pg_for_version(engine, version):
    """Determine the default parameter group name for a target engine version.

    Aurora uses naming conventions:
      MySQL:      default.aurora-mysql8.0
      PostgreSQL: default.aurora-postgresql16
    """
    try:
        if "mysql" in engine:
            # e.g. "8.0.mysql_aurora.3.07.1" → major "8.0"
            major = version.split(".mysql_aurora")[0] if ".mysql_aurora" in version else version.rsplit(".", 1)[0]
            return f"default.aurora-mysql{major}"
        else:
            # e.g. "16.4" → major "16"
            major = version.split(".")[0]
            return f"default.aurora-postgresql{major}"
    except Exception as e:
        logger.warning("Could not determine default PG for %s %s: %s", engine, version, e)
        return ""


def _wait_for_available(clone_id, max_wait_seconds=180):
    """Wait for the cluster to reach 'available' status before modifying.

    After cloning, the cluster may briefly be in a transitional state.
    We must wait for it to be fully available before submitting the upgrade.

    Raises Exception if the cluster doesn't become available within the timeout.
    """
    import time
    interval = 10
    elapsed = 0
    while elapsed < max_wait_seconds:
        try:
            resp = rds.describe_db_clusters(DBClusterIdentifier=clone_id)
            status = resp["DBClusters"][0].get("Status", "")
            if status == "available":
                logger.info("Cluster %s is available (waited %ds)", clone_id, elapsed)
                return
            logger.info("Cluster %s status: %s — waiting... (%ds/%ds)",
                        clone_id, status, elapsed, max_wait_seconds)
        except Exception as e:
            logger.warning("Error checking cluster status: %s", e)
        time.sleep(interval)
        elapsed += interval

    raise Exception(
        f"Cluster {clone_id} did not become available within {max_wait_seconds}s "
        f"(last status: {status}). Cannot proceed with upgrade."
    )
