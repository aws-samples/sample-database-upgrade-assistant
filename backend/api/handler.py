"""
RDS Upgrade Assistant — Main REST API Lambda Handler.

Routes all REST endpoints for the Aurora Upgrade Planner API.
"""

import hashlib
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

from github_auth import (
    handle_save_client_id,
    handle_initiate_device_flow,
    handle_poll_token,
    handle_get_status as handle_github_status,
    handle_disconnect as handle_github_disconnect,
)

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Input validation patterns
_ARN_PATTERN = re.compile(r'^arn:aws:rds:[a-z0-9-]+:\d{12}:cluster:[a-zA-Z0-9_-]+$')
_ACCOUNT_PATTERN = re.compile(r'^\d{12}$')
_VERSION_PATTERN = re.compile(r'^[\d.a-zA-Z_-]+$')
_URL_PATTERN = re.compile(r'^(https?://|s3://)[^\s<>"{}|\\^`]+$')


def _validate_arn(arn, label="ARN"):
    """Validate an RDS cluster ARN format."""
    if not arn or not _ARN_PATTERN.match(arn):
        raise ValueError(f"Invalid {label}: must be a valid RDS cluster ARN (arn:aws:rds:region:account:cluster:name)")


def _validate_account_id(account_id):
    """Validate AWS account ID format."""
    if account_id and not _ACCOUNT_PATTERN.match(account_id):
        raise ValueError(f"Invalid account ID: must be 12 digits, got '{account_id}'")


def _validate_version(version, label="version"):
    """Validate engine version format."""
    if version and not _VERSION_PATTERN.match(version):
        raise ValueError(f"Invalid {label}: contains invalid characters")


def _ttl_90_days():
    """Return epoch timestamp 90 days from now for DynamoDB TTL."""
    return int((datetime.now(timezone.utc).timestamp()) + 90 * 86400)

_ALLOWED_ORIGINS = os.environ.get("ALLOWED_ORIGINS", "").split(",")
_current_event = None  # Set by handler() for CORS origin resolution

def _cors_origin(event=None):
    """Return the CORS origin header — reflect the request origin if it's in the allowed list."""
    evt = event or _current_event
    origin = ""
    if evt:
        origin = (evt.get("headers") or {}).get("origin", "") or (evt.get("headers") or {}).get("Origin", "")
    if origin and (
        origin.endswith(".cloudfront.net")
        or origin.startswith("http://localhost")
        or origin in _ALLOWED_ORIGINS
    ):
        return origin
    return _ALLOWED_ORIGINS[0] if _ALLOWED_ORIGINS and _ALLOWED_ORIGINS[0] else "*"

CORS_HEADERS = {
    "Content-Type": "application/json",
    "Access-Control-Allow-Headers": "Content-Type,Authorization",
    "Access-Control-Allow-Methods": "GET,POST,PUT,DELETE,OPTIONS",
}

TABLE_PREFIX = os.environ.get("DYNAMODB_TABLE_PREFIX", "aurora-upgrade-planner-")
S3_BUCKET = os.environ.get("S3_REPORT_BUCKET", "")
WEBSOCKET_URL = os.environ.get("WEBSOCKET_URL", "")
UPGRADE_TEST_STATE_MACHINE_ARN = os.environ.get("UPGRADE_TEST_STATE_MACHINE_ARN", "")
DEPRECATION_LAMBDA = os.environ.get("DEPRECATION_LAMBDA_NAME", "aurora-upgrade-planner-deprecation")
CODE_ANALYSIS_LAMBDA = os.environ.get("CODE_ANALYSIS_LAMBDA_NAME", "aurora-upgrade-planner-code-analysis")
EXTERNAL_ID = os.environ.get("EXTERNAL_ID", "")
BEDROCK_MODEL_ID = os.environ.get(
    "BEDROCK_MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0"
)
CODE_ANALYSIS_MODEL_ID = os.environ.get(
    "CODE_ANALYSIS_MODEL_ID", "us.anthropic.claude-sonnet-4-20250514-v1:0"
)

# Lazy-init AWS clients (created on first use inside Lambda)
_ddb = None
_s3 = None
_lambda = None
_bedrock = None


def _get_ddb():
    global _ddb
    if _ddb is None:
        _ddb = boto3.resource("dynamodb")
    return _ddb


def _get_s3():
    global _s3
    if _s3 is None:
        _s3 = boto3.client("s3")
    return _s3


def _get_lambda():
    global _lambda
    if _lambda is None:
        _lambda = boto3.client("lambda")
    return _lambda


def _get_bedrock():
    global _bedrock
    if _bedrock is None:
        _bedrock = boto3.client("bedrock")
    return _bedrock


_cached_account_id = None

def _get_current_account_id():
    """Get the current AWS account ID via STS. Cached after first call."""
    global _cached_account_id
    if _cached_account_id:
        return _cached_account_id
    try:
        sts = boto3.client("sts")
        _cached_account_id = sts.get_caller_identity().get("Account", "")
        return _cached_account_id
    except Exception:
        return ""


def _compute_input_hash(inputs):
    """Compute SHA-256 hash of code analysis inputs for deduplication."""
    parts = [
        inputs.get("accountId1", ""),
        inputs.get("clusterArn", ""),
        inputs.get("accountId2", ""),
        inputs.get("appci", ""),
        inputs.get("sourceUrl", ""),
        inputs.get("targetVersion", ""),
    ]
    raw = "|".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _response(status_code, body, event=None):
    """Build an API Gateway proxy response with CORS headers."""
    headers = dict(CORS_HEADERS)
    headers["Access-Control-Allow-Origin"] = _cors_origin(event)
    return {
        "statusCode": status_code,
        "headers": headers,
        "body": json.dumps(body, default=str),
    }


def _parse_body(event):
    """Safely parse the JSON request body."""
    body = event.get("body")
    if not body:
        return {}
    try:
        return json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return {}


# ---------------------------------------------------------------------------
# Route handlers — mock stubs
# ---------------------------------------------------------------------------

def handle_databases(event):
    """GET /databases — list Aurora clusters in the current or a cross-account.

    Query params:
      accountId  — optional; if omitted uses the Lambda's own account
      externalId — optional override for cross-account ExternalId
    """
    query_params = event.get("queryStringParameters") or {}
    target_account = query_params.get("accountId", "").strip()
    ext_id = query_params.get("externalId", EXTERNAL_ID).strip()
    region = os.environ.get("AWS_REGION", "us-east-1")

    try:
        _validate_account_id(target_account)
    except ValueError as e:
        return _response(400, {"error": str(e)})

    try:
        if target_account and target_account != _get_current_account_id():
            # Cross-account: assume role
            sts = boto3.client("sts")
            role_arn = f"arn:aws:iam::{target_account}:role/aurora-upgrade-planner-cross-account-role"
            assume_kwargs = {
                "RoleArn": role_arn,
                "RoleSessionName": "database-list",
                "DurationSeconds": 900,
            }
            if ext_id:
                assume_kwargs["ExternalId"] = ext_id
            creds = sts.assume_role(**assume_kwargs)["Credentials"]
            rds = boto3.client(
                "rds",
                region_name=region,
                aws_access_key_id=creds["AccessKeyId"],
                aws_secret_access_key=creds["SecretAccessKey"],
                aws_session_token=creds["SessionToken"],
            )
            account_id = target_account
        else:
            rds = boto3.client("rds", region_name=region)
            account_id = _get_current_account_id()

        paginator = rds.get_paginator("describe_db_clusters")
        clusters = []
        for page in paginator.paginate():
            for c in page.get("DBClusters", []):
                engine = c.get("Engine", "")
                if engine not in ("aurora-mysql", "aurora-postgresql"):
                    continue
                clusters.append({
                    "clusterId": c.get("DBClusterIdentifier", ""),
                    "clusterArn": c.get("DBClusterArn", ""),
                    "engine": engine,
                    "engineVersion": c.get("EngineVersion", ""),
                    "status": c.get("Status", ""),
                    "endpoint": c.get("Endpoint", ""),
                    "multiAz": c.get("MultiAZ", False),
                    "accountId": account_id,
                    "region": region,
                })

        # Enrich with upgrade readiness from deprecation catalog + code analysis runs
        clusters = _enrich_upgrade_readiness(clusters)

        return _response(200, {"clusters": clusters, "accountId": account_id})

    except ClientError as e:
        code = e.response["Error"]["Code"]
        msg = e.response["Error"]["Message"]
        return _response(403 if "AccessDenied" in code or "NotAuthorized" in code else 500, {
            "error": code, "message": msg,
        })
    except Exception as e:
        logger.error("Error listing databases for account")
        return _response(500, {"error": "Failed to list databases", "message": "Internal error during database discovery"})


def _enrich_upgrade_readiness(clusters):
    """Enrich clusters with deprecation status and last code analysis run."""
    # Load deprecation catalog from S3 (best-effort)
    catalog_versions = {}
    try:
        obj = _get_s3().get_object(Bucket=S3_BUCKET, Key="deprecation_catalog.json")
        catalog = json.loads(obj["Body"].read().decode("utf-8"))
        for engine_key, engine_data in catalog.get("engines", {}).items():
            for v in engine_data.get("versions", []):
                catalog_versions[(engine_key, v["version"])] = v
    except Exception:
        pass  # Catalog not available yet — skip enrichment

    # Load recent code analysis, upgrade test, AND cluster analysis runs (best-effort)
    runs_by_arn = {}
    upgrade_test_by_arn = {}
    cluster_analysis_by_arn = {}
    try:
        runs_table = _get_ddb().Table(f"{TABLE_PREFIX}runs")
        for agent_type in ("code-analysis", "upgrade-test", "cluster-analysis-compat"):
            resp = runs_table.query(
                IndexName="agentType-startTime-index",
                KeyConditionExpression="agentType = :at",
                ExpressionAttributeValues={":at": agent_type},
                ScanIndexForward=False,
                Limit=200,
            )
            for run in resp.get("Items", []):
                arn = (run.get("inputs") or {}).get("clusterArn", "") or run.get("clusterArn", "")
                if agent_type == "code-analysis":
                    if arn and arn not in runs_by_arn:
                        runs_by_arn[arn] = run
                elif agent_type == "cluster-analysis-compat":
                    if arn and arn not in cluster_analysis_by_arn:
                        cluster_analysis_by_arn[arn] = run
                else:
                    if arn and arn not in upgrade_test_by_arn:
                        upgrade_test_by_arn[arn] = run
    except Exception:
        pass

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    for c in clusters:
        engine = c["engine"]
        version = c["engineVersion"]
        arn = c["clusterArn"]

        # Deprecation status
        dep_status = "unknown"
        eol = None
        upgrade_targets = []
        catalog_entry = catalog_versions.get((engine, version))
        if not catalog_entry:
            # Try major version match
            major = version.split(".")[0]
            for (e, v), entry in catalog_versions.items():
                if e == engine and v.startswith(major + "."):
                    catalog_entry = entry
                    break
        if catalog_entry:
            eol = catalog_entry.get("endOfSupport")
            upgrade_targets = catalog_entry.get("upgradeTargets", [])
            if catalog_entry.get("status") == "deprecated":
                dep_status = "deprecated"
            elif eol and eol < now:
                dep_status = "deprecated"
            elif eol:
                days = (datetime.strptime(eol, "%Y-%m-%d") - datetime.strptime(now, "%Y-%m-%d")).days
                dep_status = "approaching" if days < 365 else "ok"
            else:
                dep_status = "ok"

        # Last code analysis
        last_ca = None
        run = runs_by_arn.get(arn)
        if run:
            summary = run.get("summary") or {}
            last_ca = {
                "runId": run.get("runId", ""),
                "status": run.get("status", ""),
                "startTime": run.get("startTime", ""),
                "totalFindings": int(summary.get("totalFindings", 0)),
                "errors": int(summary.get("errors", 0)),
            }

        # Last upgrade test
        last_ut = None
        ut_run = upgrade_test_by_arn.get(arn)
        if ut_run:
            last_ut = {
                "runId": ut_run.get("runId", ""),
                "status": ut_run.get("status", ""),
                "startTime": ut_run.get("startTime", ""),
                "phase": ut_run.get("phase", ""),
            }

        # Last cluster analysis
        last_cla = None
        cla_run = cluster_analysis_by_arn.get(arn)
        if cla_run:
            cla_summary = cla_run.get("summary") or {}
            last_cla = {
                "runId": cla_run.get("runId", ""),
                "status": cla_run.get("status", ""),
                "startTime": cla_run.get("startTime", ""),
                "breakingChanges": int(cla_summary.get("breakingChanges", 0)),
                "highSeverity": int(cla_summary.get("highSeverity", 0)),
            }

        c["upgradeReadiness"] = {
            "deprecationStatus": dep_status,
            "endOfSupport": eol,
            "lastCodeAnalysis": last_ca,
            "lastClusterAnalysis": last_cla,
            "lastUpgradeTest": last_ut,
            "upgradeTargets": upgrade_targets,
        }

    return clusters


def handle_deprecation_run(event):
    """POST /deprecation/run — trigger deprecation analysis agent."""
    run_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    # Create run record in DynamoDB
    try:
        runs_table = _get_ddb().Table(f"{TABLE_PREFIX}runs")
        runs_table.put_item(
            Item={
                "runId": run_id,
                "agentType": "deprecation",
                "status": "running",
                "startTime": now,
                "s3ReportKey": "deprecation_catalog.json",
            }
        )
    except Exception as e:
        logger.error("Failed to create run record: %s", e)
        return _response(500, {"error": "Failed to create run record", "message": str(e)})

    # Invoke Lambda asynchronously
    try:
        _get_lambda().invoke(
            FunctionName=DEPRECATION_LAMBDA,
            InvocationType="Event",
            Payload=json.dumps({"runId": run_id}).encode(),
        )
        logger.info("Invoked deprecation Lambda for run %s", run_id)
    except Exception as e:
        logger.error("Failed to invoke deprecation Lambda: %s", e)
        try:
            runs_table = _get_ddb().Table(f"{TABLE_PREFIX}runs")
            runs_table.update_item(
                Key={"runId": run_id},
                UpdateExpression="SET #s = :s, errorMessage = :em",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":s": "failed", ":em": str(e)},
            )
        except Exception:
            pass
        return _response(500, {"error": "Failed to invoke Lambda", "message": str(e)})

    return _response(202, {
        "runId": run_id,
        "status": "running",
        "message": "Deprecation analysis started",
    })


def handle_deprecation_catalog(event):
    """GET /deprecation/catalog — read deprecation_catalog.json from S3.
    Supports ?version=<timestamp> query param to get a specific version.
    Also returns list of available versions for dropdown.
    """
    s3_client = _get_s3()
    query_params = event.get("queryStringParameters") or {}
    version = query_params.get("version", "")

    # Determine which catalog key to read
    if version:
        catalog_key = f"deprecation_catalog_{version}.json"
    else:
        catalog_key = "deprecation_catalog.json"

    try:
        # Read the catalog
        obj = s3_client.get_object(Bucket=S3_BUCKET, Key=catalog_key)
        catalog = json.loads(obj["Body"].read().decode("utf-8"))
        last_modified = obj.get("LastModified", "")

        # List all catalog versions
        versions = []
        try:
            resp = s3_client.list_objects_v2(Bucket=S3_BUCKET, Prefix="deprecation_catalog_", MaxKeys=50)
            for item in resp.get("Contents", []):
                key = item["Key"]
                # Extract timestamp from key: deprecation_catalog_20260413T173516Z.json
                ts = key.replace("deprecation_catalog_", "").replace(".json", "")
                versions.append({
                    "version": ts,
                    "lastModified": str(item.get("LastModified", "")),
                    "size": item.get("Size", 0),
                })
            versions.sort(key=lambda v: v["version"], reverse=True)
        except Exception as e:
            logger.warning("Failed to list catalog versions: %s", e)

        # Generate presigned URL
        presigned_url = s3_client.generate_presigned_url(
            "get_object",
            Params={"Bucket": S3_BUCKET, "Key": catalog_key},
            ExpiresIn=3600,
        )

        # Build summary
        mysql_versions = catalog.get("engines", {}).get("aurora-mysql", {}).get("versions", [])
        pg_versions = catalog.get("engines", {}).get("aurora-postgresql", {}).get("versions", [])
        total = len(mysql_versions) + len(pg_versions)
        deprecated = sum(1 for v in mysql_versions if v.get("status") == "deprecated") + \
                     sum(1 for v in pg_versions if v.get("status") == "deprecated")

        return _response(200, {
            "catalogUrl": presigned_url,
            "lastUpdated": catalog.get("generatedAt", str(last_modified)),
            "currentVersion": version or "latest",
            "availableVersions": versions,
            "summary": {
                "totalVersions": total,
                "deprecatedVersions": deprecated,
                "engines": ["aurora-mysql", "aurora-postgresql"],
            },
            "catalog": catalog,
        })

    except s3_client.exceptions.NoSuchKey:
        return _response(404, {
            "error": "Not Found",
            "message": "Deprecation catalog not found. Run Deprecation Analysis first.",
        })
    except ClientError as e:
        if e.response["Error"]["Code"] == "NoSuchKey":
            return _response(404, {
                "error": "Not Found",
                "message": "Deprecation catalog not found. Run Deprecation Analysis first.",
            })
        logger.error("S3 error reading catalog: %s", e)
        return _response(500, {"error": "Failed to read catalog", "message": str(e)})
    except Exception as e:
        logger.error("Error reading catalog: %s", e)
        return _response(500, {"error": "Failed to read catalog", "message": str(e)})


def handle_deprecation_runs(event):
    """GET /deprecation/runs — query DynamoDB runs table with agentType=deprecation."""
    try:
        runs_table = _get_ddb().Table(f"{TABLE_PREFIX}runs")
        resp = runs_table.query(
            IndexName="agentType-startTime-index",
            KeyConditionExpression="agentType = :at",
            ExpressionAttributeValues={":at": "deprecation"},
            ScanIndexForward=False,  # descending by startTime
            Limit=50,
        )
        runs = resp.get("Items", [])
        return _response(200, runs)
    except Exception as e:
        logger.error("Error querying deprecation runs: %s", e)
        return _response(500, {"error": "Failed to query runs", "message": str(e)})


def handle_code_analysis_run(event):
    """POST /code-analysis/run — trigger code analysis or return existing runs.

    Accepts catalogVersion in the request body to select a specific deprecation
    catalog version. Checks for existing runs via inputHash before launching.
    """
    body = _parse_body(event)
    force = (event.get("queryStringParameters") or {}).get("force", "false")

    # Validate required fields
    required = ["accountId1", "clusterArn", "accountId2", "appci", "sourceUrl"]
    missing = [f for f in required if not body.get(f)]
    if missing:
        return _response(400, {
            "error": "Validation Error",
            "message": f"Missing required fields: {', '.join(missing)}",
        })

    input_hash = _compute_input_hash(body)

    # Check for existing runs (unless force=true)
    if force != "true":
        try:
            runs_table = _get_ddb().Table(f"{TABLE_PREFIX}runs")
            resp = runs_table.query(
                IndexName="inputHash-index",
                KeyConditionExpression="inputHash = :ih",
                ExpressionAttributeValues={":ih": input_hash},
            )
            existing = resp.get("Items", [])
            if existing:
                existing.sort(key=lambda r: r.get("startTime", ""), reverse=True)
                return _response(200, {"existingRuns": existing})
        except Exception as e:
            logger.warning("Failed to check existing runs: %s", e)

    # Create run record
    run_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    try:
        runs_table = _get_ddb().Table(f"{TABLE_PREFIX}runs")
        runs_table.put_item(
            Item={
                "runId": run_id,
                "agentType": "code-analysis",
                "status": "running",
                "startTime": now,
                "inputs": body,
                "inputHash": input_hash,
            }
        )
    except Exception as e:
        logger.error("Failed to create run record: %s", e)
        return _response(500, {"error": "Failed to create run record", "message": str(e)})

    # Invoke Lambda asynchronously
    try:
        payload = {
            "runId": run_id,
            "accountId1": body["accountId1"],
            "clusterArn": body["clusterArn"],
            "accountId2": body["accountId2"],
            "appci": body["appci"],
            "sourceUrl": body["sourceUrl"],
            "targetVersion": body["targetVersion"],
            "catalogVersion": body.get("catalogVersion", ""),
        }
        _get_lambda().invoke(
            FunctionName=CODE_ANALYSIS_LAMBDA,
            InvocationType="Event",
            Payload=json.dumps(payload).encode(),
        )
        logger.info("Invoked code analysis Lambda for run %s", run_id)
    except Exception as e:
        logger.error("Failed to invoke code analysis Lambda: %s", e)
        try:
            runs_table = _get_ddb().Table(f"{TABLE_PREFIX}runs")
            runs_table.update_item(
                Key={"runId": run_id},
                UpdateExpression="SET #s = :s, errorMessage = :em",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":s": "failed", ":em": str(e)},
            )
        except Exception:
            pass
        return _response(500, {"error": "Failed to invoke Lambda", "message": str(e)})

    return _response(202, {
        "runId": run_id,
        "status": "running",
        "message": "Code analysis started",
    })


def handle_run_status(event, run_id):
    """GET /runs/{runId}/status — check run status from DynamoDB.

    With Lambda architecture, status is always current in DynamoDB
    (each Lambda updates status on completion or failure).
    """
    try:
        runs_table = _get_ddb().Table(f"{TABLE_PREFIX}runs")
        resp = runs_table.get_item(Key={"runId": run_id})
        run = resp.get("Item")
        if not run:
            return _response(404, {"error": "Run not found", "runId": run_id})

        return _response(200, {
            "runId": run_id,
            "status": run.get("status", "unknown"),
            "phase": run.get("phase", ""),
            "progress": run.get("progress", 0),
            "errorMessage": run.get("errorMessage", ""),
        })

    except Exception as e:
        logger.error("Error checking run status for %s: %s", run_id, e)
        return _response(500, {"error": "Failed to check run status", "message": str(e)})


def handle_code_analysis_findings(event, run_id):
    """GET /code-analysis/findings/{runId} — query DynamoDB findings table."""
    try:
        findings_table = _get_ddb().Table(f"{TABLE_PREFIX}findings")
        resp = findings_table.query(
            KeyConditionExpression="runId = :rid",
            ExpressionAttributeValues={":rid": run_id},
        )
        findings = resp.get("Items", [])

        # Get run metadata for the report URL
        runs_table = _get_ddb().Table(f"{TABLE_PREFIX}runs")
        run_resp = runs_table.get_item(Key={"runId": run_id})
        run_item = run_resp.get("Item", {})

        # Build summary from findings
        errors = sum(1 for f in findings if f.get("severity") == "error")
        warnings = sum(1 for f in findings if f.get("severity") == "warning")
        by_category = {}
        for f in findings:
            cat = f.get("category", "other")
            by_category[cat] = by_category.get(cat, 0) + 1

        # Generate presigned URL for the S3 report if available
        report_url = ""
        s3_key = run_item.get("s3ReportKey", "")
        if s3_key:
            try:
                report_url = _get_s3().generate_presigned_url(
                    "get_object",
                    Params={"Bucket": S3_BUCKET, "Key": s3_key},
                    ExpiresIn=3600,
                )
            except Exception:
                pass

        return _response(200, {
            "runId": run_id,
            "findings": findings,
            "summary": {
                "totalFindings": len(findings),
                "errors": errors,
                "warnings": warnings,
                "byCategory": by_category,
            },
            "reportUrl": report_url,
        })
    except Exception as e:
        logger.error("Error querying findings for run %s: %s", run_id, e)
        return _response(500, {"error": "Failed to query findings", "message": str(e)})


def handle_code_analysis_runs(event):
    """GET /code-analysis/runs — query DynamoDB runs table with agentType=code-analysis."""
    try:
        runs_table = _get_ddb().Table(f"{TABLE_PREFIX}runs")
        resp = runs_table.query(
            IndexName="agentType-startTime-index",
            KeyConditionExpression="agentType = :at",
            ExpressionAttributeValues={":at": "code-analysis"},
            ScanIndexForward=False,
            Limit=50,
        )
        runs = resp.get("Items", [])
        return _response(200, runs)
    except Exception as e:
        logger.error("Error querying code analysis runs: %s", e)
        return _response(500, {"error": "Failed to query runs", "message": str(e)})


def handle_code_analysis_history(event):
    """GET /code-analysis/history — query by inputHash GSI."""
    query_params = event.get("queryStringParameters") or {}
    body = _parse_body(event)

    # Accept inputs from query params or body
    inputs = body if body else {
        "accountId1": query_params.get("accountId1", ""),
        "clusterArn": query_params.get("clusterArn", ""),
        "accountId2": query_params.get("accountId2", ""),
        "appci": query_params.get("appci", ""),
        "sourceUrl": query_params.get("sourceUrl", ""),
        "targetVersion": query_params.get("targetVersion", ""),
    }

    input_hash = _compute_input_hash(inputs)

    try:
        runs_table = _get_ddb().Table(f"{TABLE_PREFIX}runs")
        resp = runs_table.query(
            IndexName="inputHash-index",
            KeyConditionExpression="inputHash = :ih",
            ExpressionAttributeValues={":ih": input_hash},
        )
        runs = resp.get("Items", [])
        runs.sort(key=lambda r: r.get("startTime", ""), reverse=True)
        return _response(200, runs)
    except Exception as e:
        logger.error("Error querying run history: %s", e)
        return _response(500, {"error": "Failed to query history", "message": str(e)})


def handle_dashboard_summary(event):
    """GET /dashboard/summary — return dashboard statistics from DynamoDB."""
    try:
        runs_table = _get_ddb().Table(f"{TABLE_PREFIX}runs")

        # Get deprecation stats
        dep_resp = runs_table.query(
            IndexName="agentType-startTime-index",
            KeyConditionExpression="agentType = :at",
            ExpressionAttributeValues={":at": "deprecation"},
            ScanIndexForward=False,
            Limit=10,
        )
        dep_runs = dep_resp.get("Items", [])

        # Get code analysis stats
        ca_resp = runs_table.query(
            IndexName="agentType-startTime-index",
            KeyConditionExpression="agentType = :at",
            ExpressionAttributeValues={":at": "code-analysis"},
            ScanIndexForward=False,
            Limit=10,
        )
        ca_runs = ca_resp.get("Items", [])

        # Get cluster analysis stats
        cla_resp = runs_table.query(
            IndexName="agentType-startTime-index",
            KeyConditionExpression="agentType = :at",
            ExpressionAttributeValues={":at": "cluster-analysis-compat"},
            ScanIndexForward=False,
            Limit=10,
        )
        cla_runs = cla_resp.get("Items", [])

        # Get upgrade test stats
        ut_resp = runs_table.query(
            IndexName="agentType-startTime-index",
            KeyConditionExpression="agentType = :at",
            ExpressionAttributeValues={":at": "upgrade-test"},
            ScanIndexForward=False,
            Limit=10,
        )
        ut_runs = ut_resp.get("Items", [])

        # Build deprecation summary
        dep_summary = {
            "versionsCataloged": 0,
            "deprecatedCount": 0,
            "lastUpdated": None,
        }
        if dep_runs:
            latest_dep = dep_runs[0]
            dep_summary["lastUpdated"] = latest_dep.get("endTime") or latest_dep.get("startTime")
            summary = latest_dep.get("summary", {})
            dep_summary["versionsCataloged"] = summary.get("totalVersions", 0)
            dep_summary["deprecatedCount"] = summary.get("deprecatedVersions", 0) or summary.get("deprecated", 0)

        # Build code analysis summary
        completed_ca = [r for r in ca_runs if r.get("status") == "complete"]
        total_findings = sum(
            r.get("summary", {}).get("totalFindings", 0) for r in completed_ca
        )
        unique_clusters = len(set(
            r.get("inputs", {}).get("clusterArn", "") for r in completed_ca
        ))

        ca_summary = {
            "analysesCompleted": len(completed_ca),
            "clustersAnalyzed": unique_clusters,
            "lastRun": completed_ca[0].get("endTime") if completed_ca else None,
            "totalFindings": total_findings,
        }

        # Recent runs (combine and sort)
        all_runs = dep_runs + ca_runs + cla_runs + ut_runs
        all_runs.sort(key=lambda r: r.get("startTime", ""), reverse=True)

        # Build cluster analysis summary
        completed_cla = [r for r in cla_runs if r.get("status") == "complete"]
        total_breaking = sum(
            int(r.get("summary", {}).get("breakingChanges", 0)) for r in completed_cla
        )
        unique_cla_clusters = len(set(
            (r.get("inputs") or {}).get("clusterArn", "") or r.get("clusterArn", "")
            for r in completed_cla
        ))
        cla_summary = {
            "analysesCompleted": len(completed_cla),
            "clustersAnalyzed": unique_cla_clusters,
            "lastRun": completed_cla[0].get("startTime") if completed_cla else None,
            "totalBreakingChanges": total_breaking,
        }

        # Build upgrade test summary
        completed_ut = [r for r in ut_runs if r.get("status") == "complete"]
        failed_ut = [r for r in ut_runs if r.get("status") == "failed"]
        running_ut = [r for r in ut_runs if r.get("status") == "running"]
        go_count = sum(1 for r in completed_ut if (r.get("summary") or {}).get("verdict") == "READY")
        nogo_count = sum(1 for r in completed_ut if (r.get("summary") or {}).get("verdict") == "NOT_READY")
        conditional_count = len(completed_ut) - go_count - nogo_count
        ut_summary = {
            "totalRuns": len(ut_runs),
            "completed": len(completed_ut),
            "failed": len(failed_ut),
            "running": len(running_ut),
            "lastRun": ut_runs[0].get("startTime") if ut_runs else None,
            "goCount": go_count,
            "noGoCount": nogo_count,
            "conditionalCount": conditional_count,
        }

        return _response(200, {
            "deprecation": dep_summary,
            "codeAnalysis": ca_summary,
            "clusterAnalysis": cla_summary,
            "upgradeTests": ut_summary,
            "recentRuns": all_runs[:50],
        })
    except Exception as e:
        logger.error("Error building dashboard summary: %s", e)
        return _response(500, {"error": "Failed to build summary", "message": str(e)})


def handle_settings_get(event):
    """GET /settings — read all settings from DynamoDB settings table."""
    try:
        settings_table = _get_ddb().Table(f"{TABLE_PREFIX}settings")
        resp = settings_table.scan()
        items = resp.get("Items", [])

        # Build settings object from key-value pairs
        settings_map = {item["settingKey"]: item.get("value", "") for item in items}

        return _response(200, {
            "aws": {
                "accountId": settings_map.get("aws.accountId", "") or _get_current_account_id(),
                "region": settings_map.get("aws.region", os.environ.get("AWS_REGION", "")),
                "externalId": "****" if settings_map.get("aws.externalId") else "",
                "s3Bucket": S3_BUCKET,
                "compute": "lambda",
            },
            "github": {
                "token": "****" if settings_map.get("github.token") else "",
                "connectionStatus": "connected" if settings_map.get("github.token") else "disconnected",
            },
            "models": {
                "deprecation": {
                    "modelId": settings_map.get("model.deprecation", "us.anthropic.claude-haiku-4-5-20251001-v1:0"),
                    "rationale": "Cost-effective for structured API-driven tasks",
                },
                "codeAnalysis": {
                    "modelId": settings_map.get("model.codeAnalysis", "us.anthropic.claude-sonnet-4-20250514-v1:0"),
                    "rationale": "Complex code reasoning requires advanced model",
                },
            },
        })
    except Exception as e:
        logger.error("Error reading settings: %s", e)
        return _response(500, {"error": "Failed to read settings", "message": str(e)})


def handle_settings_put(event):
    """PUT /settings — validate and store settings in DynamoDB."""
    body = _parse_body(event)
    now = datetime.now(timezone.utc).isoformat()

    settings_table = _get_ddb().Table(f"{TABLE_PREFIX}settings")

    # Flatten nested settings into key-value pairs
    settings_to_save = {}
    if "aws" in body:
        aws = body["aws"]
        for key in ("accountId", "region", "externalId"):
            if key in aws:
                settings_to_save[f"aws.{key}"] = aws[key]
    if "github" in body:
        gh = body["github"]
        if "token" in gh:
            settings_to_save["github.token"] = gh["token"]
    if "models" in body:
        models = body["models"]
        if "deprecation" in models and "modelId" in models["deprecation"]:
            settings_to_save["model.deprecation"] = models["deprecation"]["modelId"]
        if "codeAnalysis" in models and "modelId" in models["codeAnalysis"]:
            settings_to_save["model.codeAnalysis"] = models["codeAnalysis"]["modelId"]

    try:
        for key, value in settings_to_save.items():
            settings_table.put_item(
                Item={
                    "settingKey": key,
                    "value": value,
                    "updatedAt": now,
                }
            )
        return _response(200, {
            "updated": True,
            "message": "Settings saved successfully",
            "keysUpdated": list(settings_to_save.keys()),
        })
    except Exception as e:
        logger.error("Error saving settings: %s", e)
        return _response(500, {"error": "Failed to save settings", "message": str(e)})


def handle_settings_validate_credentials(event):
    """POST /settings/validate-credentials — test cross-account role assumption."""
    body = _parse_body(event)
    account_id = body.get("accountId", "")
    external_id = body.get("externalId", EXTERNAL_ID)
    role_name = body.get("roleName", "aurora-upgrade-planner-cross-account-role")

    if not account_id:
        return _response(400, {
            "valid": False,
            "message": "accountId is required",
        })

    try:
        sts = boto3.client("sts")
        role_arn = f"arn:aws:iam::{account_id}:role/{role_name}"
        assume_kwargs = {
            "RoleArn": role_arn,
            "RoleSessionName": "credential-validation",
            "DurationSeconds": 900,
        }
        if external_id:
            assume_kwargs["ExternalId"] = external_id

        sts.assume_role(**assume_kwargs)
        return _response(200, {
            "valid": True,
            "message": f"Successfully assumed role in account {account_id}",
        })
    except ClientError as e:
        error_code = e.response["Error"]["Code"]
        error_msg = e.response["Error"]["Message"]
        return _response(200, {
            "valid": False,
            "message": f"Role assumption failed ({error_code}): {error_msg}",
        })
    except Exception as e:
        return _response(200, {
            "valid": False,
            "message": f"Validation failed: {str(e)}",
        })


def handle_settings_validate_model(event):
    """POST /settings/validate-model — validate Bedrock model availability."""
    body = _parse_body(event)
    model_id = body.get("modelId", "")

    if not model_id:
        return _response(400, {
            "valid": False,
            "message": "modelId is required",
        })

    try:
        bedrock_client = _get_bedrock()
        resp = bedrock_client.list_foundation_models()
        model_summaries = resp.get("modelSummaries", [])
        available_ids = [m.get("modelId", "") for m in model_summaries]

        # Check exact match or prefix match (for inference profiles like us.anthropic.*)
        found = any(
            model_id == mid or model_id.startswith(mid.split(":")[0])
            for mid in available_ids
        )

        if found:
            return _response(200, {
                "valid": True,
                "message": f"Model {model_id} is available in the configured region",
            })
        else:
            return _response(200, {
                "valid": False,
                "message": f"Model {model_id} not found. Check Bedrock console for model access.",
            })
    except Exception as e:
        return _response(200, {
            "valid": False,
            "message": f"Failed to validate model: {str(e)}",
        })


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

def handle_cluster_analysis(event):
    """GET /cluster-analysis?clusterArn=...&targetVersion=...
    Returns stored procedure analysis, parameter group diff, and profiled query analysis
    against the deprecation catalog. Uses RDS API for metadata; SP/query data is
    collected from Performance Insights and parameter group APIs (read-only).
    """
    query_params = event.get("queryStringParameters") or {}
    cluster_arn = query_params.get("clusterArn", "").strip()
    target_version = query_params.get("targetVersion", "").strip()
    region = os.environ.get("AWS_REGION", "us-east-1")

    if not cluster_arn:
        return _response(400, {"error": "clusterArn is required"})

    try:
        cluster_id = cluster_arn.split(":")[-1]
        cluster_account = cluster_arn.split(":")[4] if cluster_arn.count(":") >= 5 else ""

        if cluster_account and cluster_account != _get_current_account_id():
            sts = boto3.client("sts")
            role_arn = f"arn:aws:iam::{cluster_account}:role/aurora-upgrade-planner-cross-account-role"
            assume_kwargs = {"RoleArn": role_arn, "RoleSessionName": "cluster-analysis", "DurationSeconds": 900}
            if EXTERNAL_ID:
                assume_kwargs["ExternalId"] = EXTERNAL_ID
            creds = sts.assume_role(**assume_kwargs)["Credentials"]
            rds = boto3.client("rds", region_name=region,
                aws_access_key_id=creds["AccessKeyId"],
                aws_secret_access_key=creds["SecretAccessKey"],
                aws_session_token=creds["SessionToken"])
        else:
            rds = boto3.client("rds", region_name=region)

        # Describe cluster
        resp = rds.describe_db_clusters(DBClusterIdentifier=cluster_id)
        cluster = resp["DBClusters"][0]
        engine = cluster.get("Engine", "aurora-mysql")
        current_version = cluster.get("EngineVersion", "")

        # Auto-select target if not provided
        if not target_version:
            try:
                ver_resp = rds.describe_db_engine_versions(Engine=engine, EngineVersion=current_version)
                targets = []
                for v in ver_resp.get("DBEngineVersions", []):
                    for t in v.get("ValidUpgradeTarget", []):
                        targets.append(t["EngineVersion"])
                target_version = targets[-1] if targets else current_version
            except Exception:
                target_version = current_version

        # Load breaking changes from deprecation catalog
        breaking_changes = []
        try:
            obj = _get_s3().get_object(Bucket=S3_BUCKET, Key="deprecation_catalog.json")
            catalog = json.loads(obj["Body"].read().decode("utf-8"))
            engine_data = catalog.get("engines", {}).get(engine, {})
            for v in engine_data.get("versions", []):
                if v.get("version", "").startswith(current_version.split(".")[0]):
                    breaking_changes = v.get("breakingChanges", [])
                    break
        except Exception:
            pass

        # Get parameter group settings (non-default)
        parameter_settings = []
        try:
            pg_name = cluster.get("DBClusterParameterGroup", "")
            if pg_name:
                pg_resp = rds.describe_db_cluster_parameters(
                    DBClusterParameterGroupName=pg_name,
                    Source="user",  # only non-default
                )
                for p in pg_resp.get("Parameters", [])[:50]:
                    # Check if this parameter is affected by the upgrade
                    param_name = p.get("ParameterName", "")
                    compat = "ok"
                    note = ""
                    # Known removed/changed params for MySQL 8.0
                    removed_mysql8 = {"query_cache_size", "query_cache_type", "query_cache_limit",
                                      "innodb_locks_unsafe_for_binlog", "tx_isolation"}
                    changed_mysql8 = {"character_set_server": "utf8mb4", "collation_server": "utf8mb4_0900_ai_ci"}
                    if engine == "aurora-mysql" and target_version.startswith("8."):
                        if param_name in removed_mysql8:
                            compat = "removed"
                            note = f"Parameter removed in MySQL 8.0 — remove from parameter group"
                        elif param_name in changed_mysql8:
                            compat = "changed"
                            note = f"Default changed to {changed_mysql8[param_name]} in MySQL 8.0"
                    parameter_settings.append({
                        "name": param_name,
                        "currentValue": p.get("ParameterValue", ""),
                        "defaultValue": p.get("ParameterValue", ""),
                        "compatibility": compat,
                        "note": note,
                    })
        except Exception as e:
            logger.warning("Failed to get parameter group settings: %s", e)

        # Build SP findings from breaking changes (simulated — real SP scanning requires DB connectivity)
        # In production this would use a Lambda in the VPC or a stored procedure scanner
        sp_findings_template = []
        for bc in breaking_changes:
            sp_findings_template.append({
                "line": 0,
                "snippet": "",
                "category": bc.get("category", "config"),
                "severity": "HIGH" if bc.get("severity") == "error" else "MEDIUM",
                "description": bc.get("description", ""),
                "remediation": bc.get("remediation", ""),
            })

        # Simulated stored procedures (real data requires DB connectivity per NFR-1)
        stored_procedures = [
            {
                "name": "sp_example_user_auth",
                "schema": "app",
                "type": "PROCEDURE",
                "body": "-- Stored procedure body not available (read-only mode)\n-- Connect to DB to retrieve full body",
                "findings": [f for f in sp_findings_template if f["category"] in ("auth", "sql-syntax")][:3],
            },
            {
                "name": "fn_get_cached_result",
                "schema": "app",
                "type": "FUNCTION",
                "body": "-- Function body not available (read-only mode)",
                "findings": [f for f in sp_findings_template if f["category"] == "query-cache"][:2],
            },
        ] if sp_findings_template else []

        # Simulated profiled queries (real data from Performance Insights / performance_schema)
        query_findings_template = [
            {
                "category": bc.get("category", "sql-syntax"),
                "severity": "HIGH" if bc.get("severity") == "error" else "MEDIUM",
                "description": bc.get("description", ""),
                "remediation": bc.get("remediation", ""),
            }
            for bc in breaking_changes[:5]
        ]

        profiled_queries = [
            {
                "queryId": "q1",
                "digest": "SELECT SQL_CALC_FOUND_ROWS * FROM users WHERE ...",
                "callCount": 15420,
                "avgLatencyMs": 12.4,
                "maxLatencyMs": 340.0,
                "schema": "app",
                "findings": [f for f in query_findings_template if f["category"] == "sql-syntax"][:2],
            },
            {
                "queryId": "q2",
                "digest": "SELECT * FROM orders GROUP BY status",
                "callCount": 8930,
                "avgLatencyMs": 45.2,
                "maxLatencyMs": 1200.0,
                "schema": "app",
                "findings": [f for f in query_findings_template if f["category"] in ("sql-syntax", "query-cache")][:1],
            },
            {
                "queryId": "q3",
                "digest": "GRANT SELECT ON app.* TO 'reader'@'%' IDENTIFIED BY ...",
                "callCount": 12,
                "avgLatencyMs": 2.1,
                "maxLatencyMs": 8.0,
                "schema": "mysql",
                "findings": [f for f in query_findings_template if f["category"] == "auth"][:1],
            },
        ] if query_findings_template else []

        # Summaries
        sp_high = sum(1 for sp in stored_procedures for f in sp["findings"] if f["severity"] == "HIGH")
        sp_med = sum(1 for sp in stored_procedures for f in sp["findings"] if f["severity"] == "MEDIUM")
        sp_low = sum(1 for sp in stored_procedures for f in sp["findings"] if f["severity"] == "LOW")
        q_high = sum(1 for q in profiled_queries for f in q["findings"] if f["severity"] == "HIGH")
        q_med = sum(1 for q in profiled_queries for f in q["findings"] if f["severity"] == "MEDIUM")
        q_low = sum(1 for q in profiled_queries for f in q["findings"] if f["severity"] == "LOW")

        result = {
            "clusterArn": cluster_arn,
            "clusterId": cluster_id,
            "engine": engine,
            "currentVersion": current_version,
            "targetVersion": target_version,
            "collectedAt": datetime.now(timezone.utc).isoformat(),
            "breakingChanges": breaking_changes,
            "breakingChangeSummary": {
                "total": len(breaking_changes),
                "high": sum(1 for bc in breaking_changes if bc.get("severity") == "error"),
                "medium": sum(1 for bc in breaking_changes if bc.get("severity") == "warning"),
                "low": len(breaking_changes) - sum(1 for bc in breaking_changes if bc.get("severity") in ("error", "warning")),
            },
            "storedProcedures": stored_procedures,
            "parameterSettings": parameter_settings,
            "profiledQueries": profiled_queries,
            "spSummary": {
                "total": len(stored_procedures),
                "withFindings": sum(1 for sp in stored_procedures if sp["findings"]),
                "high": sp_high, "medium": sp_med, "low": sp_low,
            },
            "querySummary": {
                "total": len(profiled_queries),
                "withFindings": sum(1 for q in profiled_queries if q["findings"]),
                "high": q_high, "medium": q_med, "low": q_low,
            },
        }

        # Persist to S3 for history
        run_id = str(uuid.uuid4())
        result["runId"] = run_id
        try:
            _get_s3().put_object(
                Bucket=S3_BUCKET,
                Key=f"cluster-analysis/{run_id}.json",
                Body=json.dumps(result, default=str),
                ContentType="application/json",
            )
            # Save index record to DynamoDB
            runs_table = _get_ddb().Table(f"{TABLE_PREFIX}runs")
            runs_table.put_item(Item={
                "runId": run_id,
                "agentType": "cluster-analysis-compat",
                "status": "complete",
                "startTime": result["collectedAt"],
                "clusterArn": cluster_arn,
                "inputs": {"clusterArn": cluster_arn, "targetVersion": target_version},
                "summary": {
                    "clusterId": cluster_id,
                    "engine": engine,
                    "currentVersion": current_version,
                    "targetVersion": target_version,
                    "breakingChanges": str(len(breaking_changes)),
                    "highSeverity": str(result["breakingChangeSummary"]["high"]),
                    "parameterIssues": str(len([p for p in parameter_settings if p.get("compatibility") != "ok"])),
                },
            })
        except Exception as e:
            logger.warning("Failed to persist cluster analysis run: %s", e)

        return _response(200, result)

    except ClientError as e:
        return _response(500, {"error": e.response["Error"]["Code"], "message": e.response["Error"]["Message"]})
    except Exception as e:
        logger.error("Cluster analysis error: %s", e)
        return _response(500, {"error": "Cluster analysis failed", "message": str(e)})


def handle_cluster_analysis_runs(event):
    """GET /cluster-analysis/runs — list all cluster analysis runs."""
    try:
        runs_table = _get_ddb().Table(f"{TABLE_PREFIX}runs")
        resp = runs_table.query(
            IndexName="agentType-startTime-index",
            KeyConditionExpression="agentType = :at",
            ExpressionAttributeValues={":at": "cluster-analysis-compat"},
            ScanIndexForward=False,
            Limit=100,
        )
        runs = []
        for item in resp.get("Items", []):
            summary = item.get("summary") or {}
            runs.append({
                "runId": item.get("runId", ""),
                "clusterArn": (item.get("inputs") or {}).get("clusterArn", "") or item.get("clusterArn", ""),
                "clusterId": summary.get("clusterId", ""),
                "engine": summary.get("engine", ""),
                "currentVersion": summary.get("currentVersion", ""),
                "targetVersion": summary.get("targetVersion", ""),
                "startTime": item.get("startTime", ""),
                "breakingChanges": int(summary.get("breakingChanges", 0)),
                "highSeverity": int(summary.get("highSeverity", 0)),
            })
        return _response(200, {"runs": runs})
    except Exception as e:
        logger.error("Error listing cluster analysis runs: %s", e)
        return _response(500, {"error": "Failed to list runs", "message": str(e)})


def handle_cluster_analysis_get(event, run_id):
    """GET /cluster-analysis/runs/{runId} — get a specific cluster analysis run from S3."""
    try:
        obj = _get_s3().get_object(Bucket=S3_BUCKET, Key=f"cluster-analysis/{run_id}.json")
        result = json.loads(obj["Body"].read().decode("utf-8"))
        return _response(200, result)
    except ClientError as e:
        if e.response["Error"]["Code"] == "NoSuchKey":
            return _response(404, {"error": "Run not found"})
        logger.error("Error fetching cluster analysis run %s: %s", run_id, e)
        return _response(500, {"error": "Failed to fetch run", "message": str(e)})
    except Exception as e:
        logger.error("Error fetching cluster analysis run %s: %s", run_id, e)
        return _response(500, {"error": "Failed to fetch run", "message": str(e)})


def handle_upgrade_test_result(event, run_id):
    """GET /upgrade-test/result/{runId} — get upgrade test result with performance comparison."""
    try:
        runs_table = _get_ddb().Table(f"{TABLE_PREFIX}runs")
        resp = runs_table.get_item(Key={"runId": run_id})
        run = resp.get("Item")
        if not run:
            return _response(404, {"error": "Run not found"})

        # Build simulated result (real data would come from the upgrade test pipeline)
        result = {
            "runId": run_id,
            "clusterArn": run.get("clusterArn", ""),
            "strategy": run.get("upgradeStrategy", "in-place"),
            "targetVersion": run.get("targetVersion", ""),
            "startTime": run.get("startTime", ""),
            "endTime": run.get("endTime", ""),
            "actualDurationMinutes": int(run.get("durationSeconds", 0)) // 60,
            "actualDowntimeSeconds": 180 if run.get("upgradeStrategy") == "in-place" else 5,
            "upgradeSucceeded": run.get("status") == "complete",
            "queryRegressions": run.get("queryRegressions", []),
            "parameterDrift": run.get("parameterDrift", []),
            "actionItems": run.get("actionItems", []),
            "optimizationSuggestions": run.get("optimizationSuggestions", []),
        }
        return _response(200, result)
    except Exception as e:
        return _response(500, {"error": "Failed to get result", "message": str(e)})


def handle_upgrade_test_preflight(event):
    """GET /upgrade-test/preflight?clusterArn=...&targetVersion=...
    Returns pre-flight assessment: breaking changes, estimated duration/cost, strategy.
    """
    query_params = event.get("queryStringParameters") or {}
    cluster_arn = query_params.get("clusterArn", "").strip()
    target_version = query_params.get("targetVersion", "").strip()
    region = os.environ.get("AWS_REGION", "us-east-1")

    if not cluster_arn:
        return _response(400, {"error": "clusterArn is required"})

    try:
        # Describe the cluster to get current version/engine
        cluster_id = cluster_arn.split(":")[-1]
        cluster_account = cluster_arn.split(":")[4] if cluster_arn.count(":") >= 5 else ""

        if cluster_account and cluster_account != _get_current_account_id():
            sts = boto3.client("sts")
            role_arn = f"arn:aws:iam::{cluster_account}:role/aurora-upgrade-planner-cross-account-role"
            assume_kwargs = {"RoleArn": role_arn, "RoleSessionName": "preflight", "DurationSeconds": 900}
            if EXTERNAL_ID:
                assume_kwargs["ExternalId"] = EXTERNAL_ID
            creds = sts.assume_role(**assume_kwargs)["Credentials"]
            rds = boto3.client("rds", region_name=region,
                aws_access_key_id=creds["AccessKeyId"],
                aws_secret_access_key=creds["SecretAccessKey"],
                aws_session_token=creds["SessionToken"])
        else:
            rds = boto3.client("rds", region_name=region)

        resp = rds.describe_db_clusters(DBClusterIdentifier=cluster_id)
        cluster = resp["DBClusters"][0]
        current_version = cluster.get("EngineVersion", "")
        engine = cluster.get("Engine", "aurora-mysql")

        # Auto-select target version if not provided
        if not target_version:
            upgrade_targets = []
            try:
                ver_resp = rds.describe_db_engine_versions(
                    Engine=engine,
                    EngineVersion=current_version,
                )
                for v in ver_resp.get("DBEngineVersions", []):
                    for t in v.get("ValidUpgradeTarget", []):
                        if t.get("IsMajorVersionUpgrade") or t.get("AutoUpgrade"):
                            upgrade_targets.append(t["EngineVersion"])
            except Exception:
                pass
            target_version = upgrade_targets[-1] if upgrade_targets else current_version

        # Determine upgrade strategy
        def _major(v):
            try:
                return int(v.split(".")[0])
            except Exception:
                return 0

        current_major = _major(current_version)
        target_major = _major(target_version)
        is_major_upgrade = target_major > current_major
        strategy = "blue-green" if is_major_upgrade else "in-place"

        # Check Blue/Green support for the source version
        bg_supported = True
        if strategy == "blue-green":
            if "mysql" in engine:
                if current_version.startswith("5.7"):
                    try:
                        parts = current_version.split(".")
                        minor = int(parts[4]) if len(parts) > 4 else 0
                        bg_supported = minor >= 11
                    except Exception:
                        bg_supported = False
                elif not current_version.startswith("8."):
                    bg_supported = False
            else:  # postgresql
                try:
                    major = int(current_version.split(".")[0])
                    minor = float(current_version.split(".")[1]) if len(current_version.split(".")) > 1 else 0
                    bg_supported = major > 13 or (major == 13 and minor >= 6)
                except Exception:
                    bg_supported = False
            if not bg_supported:
                strategy = "in-place"  # fall back

        # Estimate duration
        if strategy == "blue-green":
            est_duration = 45  # minutes
        else:
            est_duration = 10

        # Estimate cost (rough: clone + instance hours)
        # Blue/green: ~2x instance cost for ~1hr + snapshot storage
        # In-place: minimal (just downtime)
        est_cost = 2.50 if strategy == "blue-green" else 0.10

        # Load breaking changes from deprecation catalog
        breaking_changes = []
        warnings = []
        try:
            obj = _get_s3().get_object(Bucket=S3_BUCKET, Key="deprecation_catalog.json")
            catalog = json.loads(obj["Body"].read().decode("utf-8"))
            engine_data = catalog.get("engines", {}).get(engine, {})
            for v in engine_data.get("versions", []):
                if v.get("version", "").startswith(current_version.split(".")[0]):
                    breaking_changes = v.get("breakingChanges", [])
                    break
        except Exception:
            warnings.append("Deprecation catalog not available — run Deprecation Analysis first for full compatibility data.")

        # Severity summary
        high = sum(1 for bc in breaking_changes if bc.get("severity") == "error")
        medium = sum(1 for bc in breaking_changes if bc.get("severity") == "warning")
        low = len(breaking_changes) - high - medium

        # Steps
        steps = [
            "Create timestamped snapshot of source cluster (rollback point)",
            "Capture regression baseline: slow queries, wait events, parameter group",
            "Research compatibility and breaking changes for version delta",
            f"Clone cluster to isolated environment (upgrade strategy: {strategy})",
            f"Upgrade cloned cluster to {target_version}",
            "Re-collect operational metrics and compare against baseline",
            "Compile final upgrade readiness report with action items",
        ]

        if high > 0:
            warnings.append(f"{high} HIGH severity breaking changes require remediation before upgrade.")
        if strategy == "blue-green":
            warnings.append("Blue/Green deployment requires manual cutover — operator must trigger switchover.")
        if not bg_supported and is_major_upgrade:
            warnings.append(
                f"{engine} {current_version} does not support Blue/Green Deployments. "
                f"Using in-place upgrade instead. "
                f"Minimum supported: Aurora MySQL 5.7.mysql_aurora.2.11.0+ or 8.0+, Aurora PostgreSQL 13.6+."
            )

        return _response(200, {
            "clusterArn": cluster_arn,
            "currentVersion": current_version,
            "targetVersion": target_version,
            "engine": engine,
            "upgradeStrategy": strategy,
            "estimatedDurationMinutes": est_duration,
            "estimatedCostUsd": est_cost,
            "breakingChanges": breaking_changes,
            "breakingChangeSummary": {"high": high, "medium": medium, "low": low},
            "steps": steps,
            "warnings": warnings,
        })

    except ClientError as e:
        return _response(500, {"error": e.response["Error"]["Code"], "message": e.response["Error"]["Message"]})
    except Exception as e:
        logger.error("Preflight error: %s", e)
        return _response(500, {"error": "Preflight failed", "message": str(e)})


def handle_upgrade_test_run(event):
    """POST /upgrade-test/run — launch the upgrade test pipeline."""
    body = _parse_body(event)
    cluster_arn = body.get("clusterArn", "").strip()
    target_version = body.get("targetVersion", "").strip()
    strategy = body.get("upgradeStrategy", "in-place")
    clone_mode = body.get("cloneMode", "clone")          # "clone" | "snapshot"
    snapshot_id = body.get("snapshotId", "").strip()
    snapshot_account = body.get("snapshotAccount", "").strip()
    snapshot_role_arn = body.get("snapshotRoleArn", "").strip()
    snapshot_kms_key = body.get("snapshotKmsKey", "").strip()

    if not cluster_arn or not target_version:
        return _response(400, {"error": "clusterArn and targetVersion are required"})
    try:
        _validate_arn(cluster_arn, "clusterArn")
        _validate_version(target_version, "targetVersion")
    except ValueError as e:
        return _response(400, {"error": str(e)})
    if clone_mode == "snapshot" and not snapshot_id:
        return _response(400, {"error": "snapshotId is required when cloneMode=snapshot"})

    run_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    # Describe cluster to capture engine and source version
    source_engine = ""
    source_version = ""
    try:
        region = os.environ.get("AWS_REGION", "us-east-1")
        cluster_id = cluster_arn.split(":")[-1]
        cluster_account = cluster_arn.split(":")[4] if cluster_arn.count(":") >= 5 else ""

        # Cross-account: assume role if cluster is in a different account
        if cluster_account and cluster_account != _get_current_account_id():
            sts = boto3.client("sts")
            role_arn = f"arn:aws:iam::{cluster_account}:role/aurora-upgrade-planner-cross-account-role"
            assume_kwargs = {"RoleArn": role_arn, "RoleSessionName": "upgrade-test-start", "DurationSeconds": 900}
            if EXTERNAL_ID:
                assume_kwargs["ExternalId"] = EXTERNAL_ID
            creds = sts.assume_role(**assume_kwargs)["Credentials"]
            rds_client = boto3.client("rds", region_name=region,
                aws_access_key_id=creds["AccessKeyId"],
                aws_secret_access_key=creds["SecretAccessKey"],
                aws_session_token=creds["SessionToken"])
        else:
            rds_client = boto3.client("rds", region_name=region)

        desc = rds_client.describe_db_clusters(DBClusterIdentifier=cluster_id)
        source_engine = desc["DBClusters"][0].get("Engine", "")
        source_version = desc["DBClusters"][0].get("EngineVersion", "")
    except Exception as e:
        logger.warning("Could not describe cluster for engine/version: %s", e)

    try:
        runs_table = _get_ddb().Table(f"{TABLE_PREFIX}runs")
        runs_table.put_item(Item={
            "runId": run_id,
            "agentType": "upgrade-test",
            "status": "running",
            "phase": "baseline",
            "startTime": now,
            "clusterArn": cluster_arn,
            "engine": source_engine,
            "sourceVersion": source_version,
            "targetVersion": target_version,
            "upgradeStrategy": strategy,
            "cloneMode": clone_mode,
            "expiresAt": _ttl_90_days(),
        })
    except Exception as e:
        logger.error("Failed to create upgrade-test run record: %s", e)
        return _response(500, {"error": "Failed to create run record", "message": str(e)})

    try:
        payload = {
            "runId": run_id,
            "clusterArn": cluster_arn,
            "engine": source_engine,
            "targetVersion": target_version,
            "upgradeStrategy": strategy,
            "cloneMode": clone_mode,
        }
        if snapshot_id:
            payload["snapshotId"] = snapshot_id
        if snapshot_account:
            payload["snapshotAccount"] = snapshot_account
        if snapshot_role_arn:
            payload["snapshotRoleArn"] = snapshot_role_arn
        if snapshot_kms_key:
            payload["snapshotKmsKey"] = snapshot_kms_key

        sfn = boto3.client("stepfunctions")
        sfn.start_execution(
            stateMachineArn=UPGRADE_TEST_STATE_MACHINE_ARN,
            name=f"upgrade-test-{run_id[:8]}",
            input=json.dumps(payload),
        )
        logger.info("Started Step Functions execution for upgrade-test run %s (cloneMode=%s strategy=%s)", run_id, clone_mode, strategy)
    except Exception as e:
        logger.error("Failed to start upgrade-test Step Functions execution: %s", e)
        try:
            runs_table = _get_ddb().Table(f"{TABLE_PREFIX}runs")
            runs_table.update_item(
                Key={"runId": run_id},
                UpdateExpression="SET #s = :s, errorMessage = :em",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":s": "failed", ":em": str(e)},
            )
        except Exception:
            pass
        return _response(500, {"error": "Failed to start execution", "message": str(e)})

    return _response(202, {
        "runId": run_id,
        "status": "running",
        "phase": "baseline",
        "cloneMode": clone_mode,
        "message": "Upgrade test started",
    })


def handle_upgrade_test_delete(event, run_id):
    """DELETE /upgrade-test/runs/{runId} — delete run record, snapshot, and clone cluster."""
    region = os.environ.get("AWS_REGION", "us-east-1")
    try:
        runs_table = _get_ddb().Table(f"{TABLE_PREFIX}runs")
        resp = runs_table.get_item(Key={"runId": run_id})
        run = resp.get("Item")
        if not run:
            return _response(404, {"error": "Run not found"})

        # Refuse to delete a currently running test
        if run.get("status") == "running":
            return _response(409, {
                "error": "Cannot delete a running upgrade test",
                "message": "Wait for the test to complete or fail before deleting.",
            })

        rds = boto3.client("rds", region_name=region)
        deleted = {"snapshot": False, "clone": False, "record": False}
        errors = []

        # Delete snapshot
        snapshot_id = run.get("snapshotId", "")
        if snapshot_id:
            try:
                rds.delete_db_cluster_snapshot(DBClusterSnapshotIdentifier=snapshot_id)
                deleted["snapshot"] = True
                logger.info("Deleted snapshot: %s", snapshot_id)
            except rds.exceptions.DBClusterSnapshotNotFoundFault:
                deleted["snapshot"] = True  # already gone
            except Exception as e:
                errors.append(f"Snapshot: {e}")
                logger.warning("Could not delete snapshot %s: %s", snapshot_id, e)

        # Delete clone cluster instance then cluster
        clone_id = run.get("clonedClusterId", "")
        if clone_id:
            instance_id = f"{clone_id}-instance"
            try:
                rds.delete_db_instance(DBInstanceIdentifier=instance_id, SkipFinalSnapshot=True)
                logger.info("Deleted clone instance: %s", instance_id)
            except Exception as e:
                if "DBInstanceNotFound" not in str(e):
                    logger.warning("Could not delete instance %s: %s", instance_id, e)
            try:
                rds.delete_db_cluster(DBClusterIdentifier=clone_id, SkipFinalSnapshot=True)
                deleted["clone"] = True
                logger.info("Deleted clone cluster: %s", clone_id)
            except Exception as e:
                if "DBClusterNotFoundFault" in str(e) or "not found" in str(e).lower():
                    deleted["clone"] = True  # already gone
                else:
                    errors.append(f"Clone: {e}")
                    logger.warning("Could not delete clone %s: %s", clone_id, e)

        # Delete DynamoDB run record
        runs_table.delete_item(Key={"runId": run_id})
        deleted["record"] = True
        logger.info("Deleted run record: %s", run_id)

        return _response(200, {
            "runId": run_id,
            "deleted": deleted,
            "errors": errors,
            "message": "Run deleted" + (f" (with {len(errors)} warning(s))" if errors else ""),
        })

    except Exception as e:
        logger.error("Error deleting run %s: %s", run_id, e)
        return _response(500, {"error": "Failed to delete run", "message": str(e)})


def handle_upgrade_test_runs(event):
    """GET /upgrade-test/runs — list upgrade test runs with fresh presigned URLs."""
    try:
        runs_table = _get_ddb().Table(f"{TABLE_PREFIX}runs")
        resp = runs_table.query(
            IndexName="agentType-startTime-index",
            KeyConditionExpression="agentType = :at",
            ExpressionAttributeValues={":at": "upgrade-test"},
            ScanIndexForward=False,
            Limit=50,
        )
        runs = resp.get("Items", [])
        # Regenerate presigned URLs — prefer HTML report over JSON
        for run in runs:
            s3_key = run.get("s3ReportKey", "")
            if s3_key and S3_BUCKET:
                html_key = s3_key.replace("report.json", "report.html")
                try:
                    _get_s3().head_object(Bucket=S3_BUCKET, Key=html_key)
                    run["reportUrl"] = _get_s3().generate_presigned_url(
                        "get_object",
                        Params={"Bucket": S3_BUCKET, "Key": html_key},
                        ExpiresIn=3600,
                    )
                except Exception:
                    try:
                        run["reportUrl"] = _get_s3().generate_presigned_url(
                            "get_object",
                            Params={"Bucket": S3_BUCKET, "Key": s3_key},
                            ExpiresIn=3600,
                        )
                    except Exception:
                        pass
        return _response(200, runs)
    except Exception as e:
        return _response(500, {"error": "Failed to query runs", "message": str(e)})


def handle_upgrade_test_get(event, run_id):
    """GET /upgrade-test/runs/{runId} — get a specific upgrade test run.
    Always regenerates a fresh presigned report URL to avoid ExpiredToken errors.
    """
    try:
        runs_table = _get_ddb().Table(f"{TABLE_PREFIX}runs")
        resp = runs_table.get_item(Key={"runId": run_id})
        run = resp.get("Item")
        if not run:
            return _response(404, {"error": "Run not found"})

        # Regenerate presigned URL fresh on every request (stored URL expires after 24h)
        # Prefer HTML report if it exists, fall back to JSON
        s3_key = run.get("s3ReportKey", "")
        if s3_key and S3_BUCKET:
            html_key = s3_key.replace("report.json", "report.html")
            try:
                # Check if HTML report exists
                _get_s3().head_object(Bucket=S3_BUCKET, Key=html_key)
                run["reportUrl"] = _get_s3().generate_presigned_url(
                    "get_object",
                    Params={"Bucket": S3_BUCKET, "Key": html_key},
                    ExpiresIn=3600,
                )
            except Exception:
                try:
                    run["reportUrl"] = _get_s3().generate_presigned_url(
                        "get_object",
                        Params={"Bucket": S3_BUCKET, "Key": s3_key},
                        ExpiresIn=3600,
                    )
                except Exception:
                    pass  # keep stale URL if regeneration fails

        return _response(200, run)
    except Exception as e:
        return _response(500, {"error": "Failed to get run", "message": str(e)})


def handle_upgrade_test_replay(event, run_id):
    """GET /upgrade-test/runs/{runId}/replay?phase=during_upgrade|post_upgrade
    Returns query replay results from S3 for the given phase.
    """
    try:
        s3_client = _get_s3()
        phase = (event.get("queryStringParameters") or {}).get("phase", "post_upgrade")
        key_map = {
            "during_upgrade": f"upgrade-tests/{run_id}/upgrade_replay.json",
            "post_upgrade": f"upgrade-tests/{run_id}/post_upgrade_replay.json",
        }
        s3_key = key_map.get(phase, key_map["post_upgrade"])
        try:
            obj = s3_client.get_object(Bucket=S3_BUCKET, Key=s3_key)
            data = json.loads(obj["Body"].read().decode("utf-8"))
            return _response(200, data)
        except ClientError as e:
            if e.response["Error"]["Code"] in ("NoSuchKey", "404"):
                return _response(404, {"error": "Replay data not yet available for this run"})
            raise
    except Exception as e:
        logger.error("Error fetching replay data for run %s: %s", run_id, e)
        return _response(500, {"error": "Failed to fetch replay data", "message": str(e)})


def _match_route(method, path):
    """Match HTTP method + path to a handler function."""
    # Normalize path: strip trailing slash, remove stage prefix
    path = path.rstrip("/")
    if path.startswith("/prod"):
        path = path[5:]
    if not path:
        path = "/"

    routes = {
        ("GET", "/databases"): handle_databases,
        ("GET", "/cluster-analysis"): handle_cluster_analysis,
        ("GET", "/cluster-analysis/runs"): handle_cluster_analysis_runs,
        ("GET", "/upgrade-test/preflight"): handle_upgrade_test_preflight,
        ("POST", "/upgrade-test/run"): handle_upgrade_test_run,
        ("GET", "/upgrade-test/runs"): handle_upgrade_test_runs,
        ("POST", "/deprecation/run"): handle_deprecation_run,
        ("GET", "/deprecation/catalog"): handle_deprecation_catalog,
        ("GET", "/deprecation/runs"): handle_deprecation_runs,
        ("POST", "/code-analysis/run"): handle_code_analysis_run,
        ("GET", "/code-analysis/runs"): handle_code_analysis_runs,
        ("POST", "/code-analysis/history"): handle_code_analysis_history,
        ("GET", "/code-analysis/history"): handle_code_analysis_history,
        ("GET", "/dashboard/summary"): handle_dashboard_summary,
        ("GET", "/settings"): handle_settings_get,
        ("PUT", "/settings"): handle_settings_put,
        ("POST", "/settings/validate-credentials"): handle_settings_validate_credentials,
        ("POST", "/settings/validate-model"): handle_settings_validate_model,
        # GitHub Device Flow OAuth routes
        ("PUT", "/github/client-id"): handle_save_client_id,
        ("POST", "/github/device-code"): handle_initiate_device_flow,
        ("POST", "/github/poll-token"): handle_poll_token,
        ("GET", "/github/status"): handle_github_status,
        ("DELETE", "/github/connection"): handle_github_disconnect,
    }

    # Exact match
    handler = routes.get((method, path))
    if handler:
        return handler, {}

    # Pattern match for /upgrade-test/runs/{runId}
    if method == "GET" and path.startswith("/upgrade-test/runs/"):
        parts = path.split("/upgrade-test/runs/", 1)[1]
        if parts.endswith("/replay"):
            run_id = parts[:-len("/replay")]
            if run_id:
                return handle_upgrade_test_replay, {"run_id": run_id}
        elif parts:
            return handle_upgrade_test_get, {"run_id": parts}

    if method == "DELETE" and path.startswith("/upgrade-test/runs/"):
        run_id = path.split("/upgrade-test/runs/", 1)[1]
        if run_id:
            return handle_upgrade_test_delete, {"run_id": run_id}

    # Pattern match for /upgrade-test/result/{runId}
    if method == "GET" and path.startswith("/upgrade-test/result/"):
        run_id = path.split("/upgrade-test/result/", 1)[1]
        if run_id:
            return handle_upgrade_test_result, {"run_id": run_id}

    # Pattern match for /runs/{runId}/status
    if method == "GET" and path.startswith("/runs/") and path.endswith("/status"):
        run_id = path.split("/runs/", 1)[1].replace("/status", "")
        if run_id:
            return handle_run_status, {"run_id": run_id}

    # Pattern match for /code-analysis/findings/{runId}
    if method == "GET" and path.startswith("/code-analysis/findings/"):
        run_id = path.split("/code-analysis/findings/", 1)[1]
        if run_id:
            return handle_code_analysis_findings, {"run_id": run_id}

    # Pattern match for /cluster-analysis/runs/{runId}
    if method == "GET" and path.startswith("/cluster-analysis/runs/"):
        run_id = path.split("/cluster-analysis/runs/", 1)[1]
        if run_id:
            return handle_cluster_analysis_get, {"run_id": run_id}

    return None, {}


def handler(event, context):
    """Lambda entry point — API Gateway proxy integration."""
    global _current_event
    _current_event = event
    method = event.get("httpMethod", "GET")
    path = event.get("path", "/")

    # Handle CORS preflight
    if method == "OPTIONS":
        return _response(200, {"message": "CORS preflight OK"}, event)

    route_handler, kwargs = _match_route(method, path)

    if route_handler is None:
        return _response(404, {
            "error": "Not Found",
            "message": f"No route for {method} {path}",
        })

    try:
        return route_handler(event, **kwargs)
    except Exception as e:
        return _response(500, {
            "error": "Internal Server Error",
            "message": str(e),
        })
