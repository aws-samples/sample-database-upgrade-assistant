"""DynamoDB state management for upgrade test pipeline.

Each Lambda reads/writes run state from a single DynamoDB row.
This replaces the monolithic agent's in-memory state.
"""

import json
import logging
from datetime import datetime, timezone

import boto3

from .config import TABLE_PREFIX, REGION

logger = logging.getLogger(__name__)
ddb = boto3.resource("dynamodb", region_name=REGION)
runs_table = ddb.Table(f"{TABLE_PREFIX}runs")


def get_run(run_id):
    """Get the current run state from DynamoDB."""
    resp = runs_table.get_item(Key={"runId": run_id})
    return resp.get("Item")


def update_run(run_id, **kwargs):
    """Update run state in DynamoDB."""
    expr_parts = []
    names = {}
    vals = {}
    for key, value in kwargs.items():
        safe_key = key.replace("-", "_")
        expr_parts.append(f"#{safe_key} = :{safe_key}")
        names[f"#{safe_key}"] = key
        vals[f":{safe_key}"] = value

    if not expr_parts:
        return

    runs_table.update_item(
        Key={"runId": run_id},
        UpdateExpression="SET " + ", ".join(expr_parts),
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=vals,
    )


def update_phase(run_id, phase, progress_pct=None):
    """Update the current phase and optionally progress percentage."""
    updates = {
        "phase": phase,
        "phaseStartTime": datetime.now(timezone.utc).isoformat(),
    }
    if progress_pct is not None:
        updates["progress"] = progress_pct
    update_run(run_id, **updates)


def mark_complete(run_id, summary, report_url=None, s3_report_key=None):
    """Mark a run as complete with summary."""
    updates = {
        "status": "complete",
        "phase": "complete",
        "endTime": datetime.now(timezone.utc).isoformat(),
        "summary": summary,
    }
    if report_url:
        updates["reportUrl"] = report_url
    if s3_report_key:
        updates["s3ReportKey"] = s3_report_key
    update_run(run_id, **updates)


def mark_failed(run_id, error_message):
    """Mark a run as failed."""
    update_run(
        run_id,
        status="failed",
        endTime=datetime.now(timezone.utc).isoformat(),
        errorMessage=error_message,
    )
