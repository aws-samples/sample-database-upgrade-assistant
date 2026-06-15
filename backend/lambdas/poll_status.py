"""Poll Status Lambda — Checks RDS cluster status for Step Functions wait loop.

Called by: Step Functions (Wait+Poll loop)
Input: {"runId": "...", "cloneId": "..."}
Output: {"status": "available|creating|upgrading|failed", "version": "...", "runId": "...", "cloneId": "..."}

When the clone cluster becomes available, ensures a DB instance exists.
Reports "creating" until both cluster AND instance are available.
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
from shared.state import get_run

rds = boto3.client("rds", region_name=REGION)


def handler(event, context):
    """Lambda entry point."""
    run_id = event["runId"]
    clone_id = event["cloneId"]

    try:
        resp = rds.describe_db_clusters(DBClusterIdentifier=clone_id)
        cluster = resp["DBClusters"][0]
        status = cluster.get("Status", "unknown")
        version = cluster.get("EngineVersion", "")
        engine = cluster.get("Engine", "aurora-mysql")
    except ClientError as e:
        if "DBClusterNotFound" in str(e):
            return {"status": "creating", "version": "", "runId": run_id, "cloneId": clone_id}
        logger.error("Error describing cluster %s: %s", clone_id, e)
        return {"status": "failed", "version": "", "runId": run_id, "cloneId": clone_id}

    # If cluster is available, ensure it has a DB instance
    if status == "available":
        instance_id = f"{clone_id}-instance"
        instance_status = _ensure_instance(clone_id, instance_id, engine, run_id)
        if instance_status != "available":
            # Instance still creating — report as "creating" so Step Functions keeps waiting
            logger.info("Cluster %s available but instance %s is %s", clone_id, instance_id, instance_status)
            status = "creating"

    logger.info("Poll status for %s: status=%s version=%s", clone_id, status, version)
    return {"status": status, "version": version, "runId": run_id, "cloneId": clone_id}


def _ensure_instance(clone_id, instance_id, engine, run_id):
    """Create a DB instance if it doesn't exist. Returns instance status."""
    try:
        resp = rds.describe_db_instances(DBInstanceIdentifier=instance_id)
        return resp["DBInstances"][0].get("DBInstanceStatus", "creating")
    except ClientError as e:
        if "DBInstanceNotFound" not in str(e):
            raise

    # Instance doesn't exist — create it
    instance_class = "db.r6g.large"
    try:
        run = get_run(run_id)
        if run and run.get("clusterArn"):
            source_id = run["clusterArn"].split(":")[-1]
            src_resp = rds.describe_db_clusters(DBClusterIdentifier=source_id)
            members = src_resp["DBClusters"][0].get("DBClusterMembers", [])
            if members:
                src_inst = rds.describe_db_instances(
                    DBInstanceIdentifier=members[0]["DBInstanceIdentifier"]
                )["DBInstances"][0]
                instance_class = src_inst.get("DBInstanceClass", instance_class)
    except Exception:
        pass

    logger.info("Creating instance %s in cluster %s (class=%s)", instance_id, clone_id, instance_class)
    rds.create_db_instance(
        DBInstanceIdentifier=instance_id,
        DBClusterIdentifier=clone_id,
        DBInstanceClass=instance_class,
        Engine=engine,
    )
    return "creating"
