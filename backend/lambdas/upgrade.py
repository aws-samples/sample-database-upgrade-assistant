"""Upgrade Lambda — Submits the engine version upgrade on the clone.

Triggered by: Step Functions (after clone is ready via Poller)
Input: {runId, cloneId}
Output: Calls ModifyDBCluster with appropriate PG, returns payload for polling

Handles both Aurora MySQL and Aurora PostgreSQL:
  - MySQL: Updates cluster PG first, then ModifyDBCluster with AllowMajorVersionUpgrade
  - PostgreSQL: ModifyDBCluster with PG in a single call
"""

import logging
import os
import sys

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from shared.config import REGION
from shared.state import get_run, update_phase, update_run, mark_failed

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

        if "mysql" in engine:
            # Aurora MySQL: update cluster PG first, then upgrade
            if target_pg:
                try:
                    rds.modify_db_cluster(
                        DBClusterIdentifier=clone_id,
                        DBClusterParameterGroupName=target_pg,
                        ApplyImmediately=True,
                    )
                    logger.info("Cluster PG updated to %s", target_pg)
                except Exception as e:
                    logger.warning("Could not update cluster PG (non-fatal): %s", e)

            rds.modify_db_cluster(
                DBClusterIdentifier=clone_id,
                EngineVersion=target_version,
                AllowMajorVersionUpgrade=True,
                ApplyImmediately=True,
            )
        else:
            # Aurora PostgreSQL: upgrade with PG in a single call
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
