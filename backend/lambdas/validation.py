"""Validation Lambda — Replays baseline queries against the upgraded clone and generates report.

Triggered by: Step Functions (after upgrade is complete via Poller)
Input: {runId, cloneId}
Output: Writes validation results + HTML report to S3, marks run complete

This step consolidates findings from:
  - Upgrade test (parameter drift, query replay during/after upgrade)
  - Code analysis (if previously run for this cluster)
  - Cluster analysis / compatibility (if previously run for this cluster)

Then calls AgentCore for a GO/NO-GO/CONDITIONAL GO verdict
and generates a polished HTML report.

Security: Connects to the upgraded clone using credentials from DB_SECRET_ARN
(Secrets Manager). If DB_SECRET_ARN is not configured, query replay is skipped.
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
from shared.config import (
    REGION, AGENTCORE_REPORT_ENDPOINT, TABLE_PREFIX,
    DB_SECRET_ARN, QUERY_REPLAY_LIMIT,
)
from shared.state import get_run, update_phase, mark_complete, mark_failed
from shared.s3_utils import put_json, put_html, get_json, generate_presigned_url

rds = boto3.client("rds", region_name=REGION)


def handler(event, context):
    """Lambda entry point."""
    run_id = event["runId"]
    clone_id = event["cloneId"]

    try:
        update_phase(run_id, "validation", progress_pct=75)

        # Load baseline from S3
        baseline = get_json(f"upgrade-tests/{run_id}/baseline.json")
        baseline_queries = baseline.get("topQueries", [])
        engine = baseline.get("engine", "aurora-mysql")
        source_cluster_id = baseline.get("clusterId", "")

        # Get clone endpoint and post-upgrade version
        resp = rds.describe_db_clusters(DBClusterIdentifier=clone_id)
        clone_cluster = resp["DBClusters"][0]
        clone_endpoint = clone_cluster.get("Endpoint", "")
        actual_version = clone_cluster.get("EngineVersion", "")

        # Re-capture parameter group from upgraded clone
        pg_name = clone_cluster.get("DBClusterParameterGroup", "")
        post_params = []
        if pg_name:
            try:
                pg_resp = rds.describe_db_cluster_parameters(
                    DBClusterParameterGroupName=pg_name, Source="user"
                )
                post_params = [
                    {"name": p["ParameterName"], "value": p.get("ParameterValue", "")}
                    for p in pg_resp.get("Parameters", [])[:50]
                ]
            except Exception:
                pass

        # Compare parameters (detect drift)
        pre_params = {p["name"]: p["value"] for p in baseline.get("parameters", [])}
        parameter_drift = []
        for p in post_params:
            pre_val = pre_params.get(p["name"])
            if pre_val is not None and pre_val != p["value"]:
                parameter_drift.append({
                    "parameter": p["name"],
                    "before": pre_val,
                    "after": p["value"],
                })

        # --- Post-upgrade query replay ---
        post_replay_results = []
        post_failures = []
        query_regressions = []

        if baseline_queries and clone_endpoint:
            update_phase(run_id, "validation_replay", progress_pct=80)
            logger.info("Replaying %d baseline queries post-upgrade on %s",
                        len(baseline_queries), clone_endpoint)
            post_replay_results = _replay_queries(clone_endpoint, engine, baseline_queries, source_cluster_id)

            # Build baseline latency map for regression detection
            baseline_latency = {q.get("queryId", ""): q.get("avgLatencyMs", 0)
                                for q in baseline_queries if q.get("avgLatencyMs", 0) > 0}
            baseline_max_latency = {q.get("queryId", ""): q.get("maxLatencyMs", 0)
                                    for q in baseline_queries}

            # Detect failures and timeouts
            for r in post_replay_results:
                if r["status"] == "fail":
                    post_failures.append({
                        "queryId": r["queryId"],
                        "digest": r["digest"],
                        "error": r.get("error", ""),
                        "severity": "HIGH",
                    })
                elif r["status"] == "timeout":
                    # Timeout is a regression only if the query was fast before upgrade
                    baseline_max = baseline_max_latency.get(r["queryId"], 0)
                    if baseline_max < QUERY_TIMEOUT_SECONDS * 1000 * 0.8:
                        # Query was fast before but timed out after upgrade → regression
                        query_regressions.append({
                            "queryId": r["queryId"],
                            "digest": r["digest"],
                            "beforeLatencyMs": baseline_latency.get(r["queryId"], baseline_max),
                            "afterLatencyMs": QUERY_TIMEOUT_SECONDS * 1000,
                            "regressionPct": 9999,
                            "severity": "HIGH",
                            "cause": f"Query timed out after {QUERY_TIMEOUT_SECONDS}s post-upgrade (was {baseline_max:.0f}ms before)",
                            "fix": "Query plan likely changed after upgrade. Run EXPLAIN ANALYZE to identify the regression.",
                        })
                    else:
                        # Query was already slow before upgrade — not a new regression
                        logger.info("Query %s timed out but was already slow (baseline max: %.0fms) — not a regression",
                                    r["queryId"], baseline_max)

            # Detect latency regressions (>50% slower than baseline)
            # Skip queries with baseline < 1ms — sub-millisecond differences are noise
            for r in post_replay_results:
                if r["status"] == "pass" and r["queryId"] in baseline_latency:
                    before = baseline_latency[r["queryId"]]
                    after = r.get("latencyMs", 0)
                    if before >= 1.0 and after > before * 1.5:
                        pct = round((after - before) / before * 100, 1)
                        query_regressions.append({
                            "queryId": r["queryId"],
                            "digest": r["digest"],
                            "beforeLatencyMs": before,
                            "afterLatencyMs": after,
                            "regressionPct": pct,
                            "severity": "HIGH" if pct > 200 else "MEDIUM",
                            "cause": "Performance regression detected post-upgrade",
                            "fix": "Review query plan with EXPLAIN ANALYZE; check index compatibility",
                        })

        post_replay = {
            "phase": "post_upgrade",
            "totalQueries": len(post_replay_results),
            "passed": sum(1 for r in post_replay_results if r["status"] == "pass"),
            "failed": len(post_failures),
            "timedOut": sum(1 for r in post_replay_results if r["status"] == "timeout"),
            "skipped": sum(1 for r in post_replay_results if r["status"] == "skipped"),
            "results": post_replay_results,
        }

        # --- Parameter regression analysis (handled by AgentCore report agent) ---
        regressions = []
        validation_summary = ""

        # Build validation results
        validation = {
            "validatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "cloneId": clone_id,
            "actualVersion": actual_version,
            "parameterDrift": parameter_drift,
            "regressions": regressions,
            "queryRegressions": query_regressions,
            "postUpgradeFailures": post_failures,
            "postUpgradeReplay": post_replay,
            "summary": validation_summary,
        }

        put_json(f"upgrade-tests/{run_id}/validation.json", validation)
        update_phase(run_id, "report", progress_pct=90)

        # --- Build consolidated report ---
        report = _build_report(run_id, baseline, validation, actual_version)
        report_key = f"upgrade-tests/{run_id}/report.json"
        put_json(report_key, report)

        # --- Get verdict from AgentCore ---
        verdict_data = _get_verdict(report)
        if verdict_data:
            report.update(verdict_data)
            # Align risk level with agent's verdict
            verdict = report.get("verdict", "")
            # Use agent's riskLevel if provided, otherwise derive from verdict
            if not report.get("riskLevel") or report["riskLevel"] == "LOW":
                if verdict == "NO_GO":
                    report["riskLevel"] = "HIGH"
                elif verdict == "CONDITIONAL_GO":
                    report["riskLevel"] = "MEDIUM"
                elif verdict == "GO":
                    report["riskLevel"] = "LOW"
            put_json(report_key, report)
        else:
            # No agent verdict — derive risk level from findings
            if post_failures or query_regressions:
                report["riskLevel"] = "HIGH"
            elif parameter_drift or regressions:
                report["riskLevel"] = "MEDIUM"
            else:
                report["riskLevel"] = "LOW"
            # Derive verdict from findings
            if post_failures:
                report["verdict"] = "NO_GO"
            elif query_regressions or regressions or parameter_drift:
                report["verdict"] = "CONDITIONAL_GO"
            else:
                report["verdict"] = "GO"
            put_json(report_key, report)

        # --- Generate polished HTML report ---
        html_key = f"upgrade-tests/{run_id}/report.html"
        html = _generate_html_report(run_id, report, validation, baseline)
        put_html(html_key, html)

        report_url = generate_presigned_url(html_key, expires_in=86400)

        # Mark complete
        summary = {
            "baselineCaptured": True,
            "compatibilityFindings": 0,
            "upgradeSucceeded": True,
            "regressions": len(regressions),
            "queryFailures": len(post_failures),
            "queryRegressions": len(query_regressions),
            "parameterDrift": len(parameter_drift),
            "outageSeconds": 0,
            "riskLevel": report.get("riskLevel", "HIGH"),
            "verdict": report.get("verdict", "NO_GO"),
            "confidence": report.get("confidence", 0),
            "confidenceReason": report.get("confidenceReason", ""),
        }
        mark_complete(run_id, summary, report_url=report_url, s3_report_key=report_key)

        # Return payload for Step Functions — Cleanup is the next step
        return {"runId": run_id, "cloneId": clone_id}

    except Exception as e:
        logger.exception("Validation failed: %s", e)
        mark_failed(run_id, str(e))
        raise


# ---------------------------------------------------------------------------
# Query Replay
# ---------------------------------------------------------------------------

def _get_db_credentials(cluster_endpoint, engine, cluster_id=""):
    """Retrieve DB credentials using tiered secret resolution:
    
    1. Check if cluster has RDS-managed secret (MasterUserSecret)
    2. Convention-based lookup: aurora-upgrade-planner/db-creds/{cluster-id}
    3. Fall back to DB_SECRET_ARN env var (legacy single-cluster mode)
    
    Returns dict with {host, port, user, password, dbname} or None.
    """
    sm = boto3.client("secretsmanager", region_name=REGION)

    # Tier 1: RDS-managed secret
    if cluster_id:
        try:
            resp = rds.describe_db_clusters(DBClusterIdentifier=cluster_id)
            master_secret = resp["DBClusters"][0].get("MasterUserSecret", {})
            master_secret_arn = master_secret.get("SecretArn", "")
            if master_secret_arn:
                logger.info("Found RDS-managed secret for %s", cluster_id)
                return _parse_secret(sm, master_secret_arn, cluster_endpoint, engine)
        except Exception as e:
            logger.debug("RDS-managed secret check failed: %s", e)

    # Tier 2: Convention-based lookup
    if cluster_id:
        convention_name = f"aurora-upgrade-planner/db-creds/{cluster_id}"
        try:
            return _parse_secret(sm, convention_name, cluster_endpoint, engine)
        except Exception as e:
            logger.debug("Convention secret '%s' not found: %s", convention_name, e)

    # Tier 3: Legacy single env var
    if DB_SECRET_ARN:
        try:
            return _parse_secret(sm, DB_SECRET_ARN, cluster_endpoint, engine)
        except Exception as e:
            logger.warning("DB_SECRET_ARN lookup failed: %s", e)

    return None


def _parse_secret(sm, secret_id, cluster_endpoint, engine):
    """Parse a Secrets Manager secret into connection credentials."""
    secret = json.loads(sm.get_secret_value(SecretId=secret_id)["SecretString"])
    return {
        "host": secret.get("host", cluster_endpoint),
        "port": int(secret.get("port", 3306 if "mysql" in engine else 5432)),
        "user": secret.get("username", secret.get("user", "admin")),
        "password": secret.get("password", ""),
        "dbname": secret.get("dbname", secret.get("database", "information_schema")),
    }


def _get_db_connection(creds, engine):
    """Open a DB connection. Returns (conn, cursor) or (None, None)."""
    if not creds:
        return None, None
    try:
        if "mysql" in engine:
            import pymysql
            conn = pymysql.connect(
                host=creds["host"], port=creds["port"],
                user=creds["user"], password=creds["password"],
                database=creds["dbname"], connect_timeout=15,
                autocommit=True,
                ssl={"ca": "/etc/pki/tls/certs/ca-bundle.crt"},
            )
            return conn, conn.cursor()
        else:
            import psycopg2
            conn = psycopg2.connect(
                host=creds["host"], port=creds["port"],
                user=creds["user"], password=creds["password"],
                dbname=creds["dbname"], connect_timeout=15,
                sslmode="require",
            )
            conn.autocommit = True
            return conn, conn.cursor()
    except Exception as e:
        logger.warning("DB connection failed: %s", e)
        return None, None


QUERY_TIMEOUT_SECONDS = 30  # Max execution time per query during replay


def _replay_queries(clone_endpoint, engine, queries, source_cluster_id=""):
    """Replay baseline queries against the upgraded clone and measure results.

    Each query has a 30-second timeout. If exceeded, it's recorded as a
    regression (timeout) rather than blocking the entire validation.

    Also captures EXPLAIN output for each query to enable plan comparison.

    Returns list of {queryId, digest, status, latencyMs, error, explainPlan}.
    """
    creds = _get_db_credentials(clone_endpoint, engine, source_cluster_id)
    if creds:
        creds["host"] = clone_endpoint  # Override host to target the clone
    conn, cur = _get_db_connection(creds, engine)
    results = []

    if not conn:
        logger.warning("Cannot replay queries — no DB connection to clone")
        for q in queries:
            results.append({
                "queryId": q.get("queryId", ""),
                "digest": q.get("digest", "")[:80],
                "status": "skipped",
                "latencyMs": 0,
                "error": "No DB connection available",
                "explainPlan": "",
            })
        return results

    # Set session-level statement timeout to prevent runaway queries
    try:
        if "mysql" in engine:
            cur.execute(f"SET SESSION max_execution_time = {QUERY_TIMEOUT_SECONDS * 1000}")
        else:
            cur.execute(f"SET statement_timeout = '{QUERY_TIMEOUT_SECONDS * 1000}ms'")
    except Exception as e:
        logger.warning("Could not set query timeout: %s", e)

    try:
        for q in queries:
            sql = q.get("sampleSql", "")
            query_id = q.get("queryId", "")
            digest = q.get("digest", "")[:80]

            if not sql or not sql.strip():
                results.append({"queryId": query_id, "digest": digest,
                                "status": "skipped", "latencyMs": 0, "error": "No SQL",
                                "explainPlan": ""})
                continue

            # Only replay SELECT/SHOW/EXPLAIN/WITH — never DML
            sql_upper = sql.strip().upper()
            if not any(sql_upper.startswith(kw) for kw in ("SELECT", "SHOW", "EXPLAIN", "WITH")):
                results.append({"queryId": query_id, "digest": digest,
                                "status": "skipped", "latencyMs": 0, "error": "Non-SELECT skipped",
                                "explainPlan": ""})
                continue

            # Capture EXPLAIN output for plan comparison
            # SECURITY NOTE: sql originates from DB system tables (trusted source), not user input.
            explain_plan = ""
            if not sql_upper.startswith("SHOW") and not sql_upper.startswith("EXPLAIN"):
                try:
                    explain_sql = f"EXPLAIN {sql}"
                    cur.execute(explain_sql)
                    explain_rows = cur.fetchall()
                    explain_plan = str(explain_rows)[:500]
                except Exception:
                    pass  # Non-critical — some queries can't be EXPLAINed

            # Execute the actual query
            # SECURITY NOTE: sql comes from performance_schema.events_statements_summary_by_digest
            # or pg_stat_statements — AWS-managed system tables, not user input.
            # Only SELECT/SHOW/EXPLAIN/WITH are replayed (DML filtered above).
            try:
                start = time.time()
                cur.execute(sql)
                cur.fetchall()
                elapsed_ms = (time.time() - start) * 1000
                results.append({"queryId": query_id, "digest": digest,
                                "status": "pass", "latencyMs": round(elapsed_ms, 2), "error": "",
                                "explainPlan": explain_plan})
            except Exception as e:
                elapsed_ms = (time.time() - start) * 1000
                error_str = str(e)[:200]
                if elapsed_ms >= (QUERY_TIMEOUT_SECONDS * 1000 - 500):
                    results.append({"queryId": query_id, "digest": digest,
                                    "status": "timeout", "latencyMs": round(elapsed_ms, 2),
                                    "error": f"Query exceeded {QUERY_TIMEOUT_SECONDS}s timeout",
                                    "explainPlan": explain_plan})
                else:
                    results.append({"queryId": query_id, "digest": digest,
                                    "status": "fail", "latencyMs": round(elapsed_ms, 2),
                                    "error": error_str, "explainPlan": explain_plan})
    finally:
        try:
            cur.close()
            conn.close()
        except Exception:
            pass

    logger.info("Query replay: %d total, %d pass, %d fail, %d timeout, %d skipped",
                len(results),
                sum(1 for r in results if r["status"] == "pass"),
                sum(1 for r in results if r["status"] == "fail"),
                sum(1 for r in results if r["status"] == "timeout"),
                sum(1 for r in results if r["status"] == "skipped"))
    return results


# ---------------------------------------------------------------------------
# JSON Response Parsing
# ---------------------------------------------------------------------------

def _parse_json_response(text):
    """Extract JSON from an AgentCore response (handles markdown fences)."""
    if not text:
        return None
    # Strip markdown code fences
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        lines = lines[1:]  # Remove opening fence
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        # Try to find JSON object in the text
        start = cleaned.find("{")
        end = cleaned.rfind("}") + 1
        if start >= 0 and end > start:
            try:
                return json.loads(cleaned[start:end])
            except json.JSONDecodeError:
                pass
    return None


# ---------------------------------------------------------------------------
# Report Building
# ---------------------------------------------------------------------------

def _build_report(run_id, baseline, validation, actual_version):
    """Build the structured report, including code analysis and cluster analysis findings."""
    run = get_run(run_id)
    cluster_arn = run.get("clusterArn", "") if run else ""

    # Pull latest code analysis findings for this cluster
    code_analysis_summary = _get_latest_code_analysis(cluster_arn)

    # Pull latest cluster analysis findings for this cluster
    cluster_analysis_summary = _get_latest_cluster_analysis(cluster_arn)

    # Build action items from validation findings
    action_items = []
    for r in validation.get("regressions", []):
        action_items.append({
            "priority": r.get("severity", "P2").replace("HIGH", "P1").replace("MEDIUM", "P2").replace("LOW", "P3"),
            "title": f"Parameter regression: {r.get('parameter', '')}",
            "description": r.get("impact", ""),
            "fix": r.get("fix", ""),
        })
    for f in validation.get("postUpgradeFailures", []):
        action_items.append({
            "priority": "P1",
            "title": f"Query failure post-upgrade: {f.get('digest', '')[:80]}",
            "description": f.get("error", ""),
            "fix": "Review query for syntax incompatibilities with the new engine version",
        })
    for qr in validation.get("queryRegressions", []):
        action_items.append({
            "priority": "P1" if qr.get("severity") == "HIGH" else "P2",
            "title": f"Query regression ({qr.get('regressionPct', 0)}% slower): {qr.get('digest', '')[:60]}",
            "description": qr.get("cause", ""),
            "fix": qr.get("fix", ""),
        })

    # Determine risk level
    risk_level = "LOW"
    if validation.get("postUpgradeFailures"):
        risk_level = "HIGH"
    elif validation.get("queryRegressions") or validation.get("regressions"):
        risk_level = "MEDIUM"
    elif validation.get("parameterDrift"):
        risk_level = "LOW"

    return {
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "runId": run_id,
        "clusterArn": cluster_arn,
        "engine": baseline.get("engine", ""),
        "currentVersion": run.get("sourceVersion", "") if run else "",
        "targetVersion": actual_version,
        "upgradeStrategy": run.get("upgradeStrategy", "in-place") if run else "in-place",
        "riskLevel": risk_level,
        "parameterDrift": validation.get("parameterDrift", []),
        "regressions": validation.get("regressions", []),
        "queryRegressions": validation.get("queryRegressions", []),
        "postUpgradeFailures": validation.get("postUpgradeFailures", []),
        "postUpgradeReplay": validation.get("postUpgradeReplay", {}),
        "actionItems": action_items,
        "codeAnalysis": code_analysis_summary,
        "clusterAnalysis": cluster_analysis_summary,
        "querySource": baseline.get("querySource", "none"),
    }


def _get_latest_code_analysis(cluster_arn):
    """Get the most recent completed code analysis findings for this cluster."""
    if not cluster_arn:
        return None
    try:
        ddb = boto3.resource("dynamodb", region_name=REGION)
        runs_table = ddb.Table(f"{TABLE_PREFIX}runs")
        resp = runs_table.query(
            IndexName="agentType-startTime-index",
            KeyConditionExpression="agentType = :at",
            ExpressionAttributeValues={":at": "code-analysis"},
            ScanIndexForward=False,
            Limit=20,
        )
        for run in resp.get("Items", []):
            inputs = run.get("inputs") or {}
            if inputs.get("clusterArn") == cluster_arn and run.get("status") == "complete":
                summary = run.get("summary") or {}
                ca_run_id = run.get("runId", "")

                # Load actual findings from DynamoDB (top 20 errors)
                top_findings = []
                try:
                    findings_table = ddb.Table(f"{TABLE_PREFIX}findings")
                    findings_resp = findings_table.query(
                        KeyConditionExpression="runId = :rid",
                        ExpressionAttributeValues={":rid": ca_run_id},
                    )
                    all_findings = findings_resp.get("Items", [])
                    errors = [f for f in all_findings if f.get("severity") == "error"]
                    warnings = [f for f in all_findings if f.get("severity") == "warning"]
                    top_findings = (errors + warnings)[:20]
                    top_findings = [
                        {
                            "fileName": f.get("fileName", ""),
                            "lineNumber": f.get("lineNumber", 0),
                            "category": f.get("category", ""),
                            "severity": f.get("severity", ""),
                            "codeSnippet": f.get("codeSnippet", "")[:100],
                            "recommendedChange": f.get("recommendedChange", "")[:200],
                        }
                        for f in top_findings
                    ]
                except Exception as e:
                    logger.warning("Could not load findings for run %s: %s", ca_run_id, e)

                return {
                    "runId": ca_run_id,
                    "totalFindings": int(summary.get("totalFindings", 0)),
                    "errors": int(summary.get("errors", 0)),
                    "warnings": int(summary.get("warnings", 0)),
                    "targetVersion": inputs.get("targetVersion", ""),
                    "startTime": run.get("startTime", ""),
                    "topFindings": top_findings,
                }
    except Exception as e:
        logger.warning("Could not load code analysis for %s: %s", cluster_arn, e)
    return None


def _get_latest_cluster_analysis(cluster_arn):
    """Get the most recent cluster analysis findings for this cluster."""
    if not cluster_arn:
        return None
    try:
        runs_table = boto3.resource("dynamodb", region_name=REGION).Table(f"{TABLE_PREFIX}runs")
        resp = runs_table.query(
            IndexName="agentType-startTime-index",
            KeyConditionExpression="agentType = :at",
            ExpressionAttributeValues={":at": "cluster-analysis-compat"},
            ScanIndexForward=False,
            Limit=20,
        )
        for run in resp.get("Items", []):
            run_arn = (run.get("inputs") or {}).get("clusterArn", "") or run.get("clusterArn", "")
            if run_arn == cluster_arn and run.get("status") == "complete":
                summary = run.get("summary") or {}
                ca_run_id = run.get("runId", "")

                # Load breaking changes from S3
                breaking_changes = []
                try:
                    data = get_json(f"cluster-analysis/{ca_run_id}.json")
                    breaking_changes = data.get("breakingChanges", [])[:10]
                    breaking_changes = [
                        {
                            "category": bc.get("category", ""),
                            "description": bc.get("description", "")[:150],
                            "severity": bc.get("severity", ""),
                            "remediation": bc.get("remediation", "")[:150],
                        }
                        for bc in breaking_changes
                    ]
                except Exception as e:
                    logger.warning("Could not load cluster analysis detail for %s: %s", ca_run_id, e)

                return {
                    "runId": ca_run_id,
                    "breakingChanges": int(summary.get("breakingChanges", 0)),
                    "highSeverity": int(summary.get("highSeverity", 0)),
                    "parameterIssues": int(summary.get("parameterIssues", 0)),
                    "targetVersion": summary.get("targetVersion", ""),
                    "startTime": run.get("startTime", ""),
                    "topBreakingChanges": breaking_changes,
                }
    except Exception as e:
        logger.warning("Could not load cluster analysis for %s: %s", cluster_arn, e)
    return None


# ---------------------------------------------------------------------------
# Verdict (AgentCore only)
# ---------------------------------------------------------------------------

def _get_verdict(report):
    """Get verdict from AgentCore report agent. No fallback — AgentCore is required."""
    endpoint = AGENTCORE_REPORT_ENDPOINT
    if not endpoint:
        logger.error("AGENTCORE_REPORT_ENDPOINT not configured — cannot generate verdict. "
                     "Deploy AgentCore agents and set the environment variable.")
        return None

    return _call_agentcore_report(report)


def _call_agentcore_report(report_data):
    """Call AgentCore report agent for verdict + narrative."""
    endpoint = AGENTCORE_REPORT_ENDPOINT
    if not endpoint:
        return None

    from shared.agentcore_client import invoke_agentcore

    input_text = json.dumps({
        "engine": report_data.get("engine"),
        "currentVersion": report_data.get("currentVersion"),
        "targetVersion": report_data.get("targetVersion"),
        "strategy": report_data.get("upgradeStrategy"),
        "riskLevel": report_data.get("riskLevel"),
        "actionItems": report_data.get("actionItems", [])[:15],
        "parameterDrift": report_data.get("parameterDrift", [])[:10],
        "regressions": report_data.get("regressions", [])[:10],
        "queryRegressions": report_data.get("queryRegressions", [])[:10],
        "postUpgradeFailures": report_data.get("postUpgradeFailures", [])[:10],
        "postUpgradeReplay": report_data.get("postUpgradeReplay"),
        "codeAnalysis": report_data.get("codeAnalysis"),
        "clusterAnalysis": report_data.get("clusterAnalysis"),
    }, indent=2, default=str)

    try:
        result_text = invoke_agentcore(endpoint, input_text)
        if not result_text:
            logger.error("AgentCore report agent returned empty response — verdict will be missing")
            return None
        parsed = _parse_json_response(result_text)
        if parsed:
            return parsed
        logger.error("AgentCore report agent returned non-JSON response — verdict will be missing")
        return None
    except Exception as e:
        logger.error("AgentCore report agent call FAILED: %s — verdict will be missing from report", e)
        return None


# ---------------------------------------------------------------------------
# HTML Report Generation
# ---------------------------------------------------------------------------

def _esc(s):
    """HTML-escape a string."""
    if not s:
        return ""
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _generate_html_report(run_id, report, validation, baseline):
    """Generate a polished HTML upgrade readiness report consolidating all findings."""
    cluster_arn = report.get("clusterArn", "")
    cluster_id = cluster_arn.split(":")[-1] if cluster_arn else ""
    region = cluster_arn.split(":")[3] if cluster_arn and cluster_arn.count(":") >= 4 else REGION
    account = cluster_arn.split(":")[4] if cluster_arn and cluster_arn.count(":") >= 5 else ""

    # Verdict styling
    verdict = report.get("verdict", "CONDITIONAL_GO")
    verdict_color = "#16a34a" if verdict == "GO" else "#dc2626" if verdict == "NO_GO" else "#d97706"
    verdict_label = (
        "✅ GO — Ready to Upgrade" if verdict == "GO"
        else "❌ NO-GO — Blocking Issues" if verdict == "NO_GO"
        else "⚠️ CONDITIONAL GO — Recommended Changes"
    )

    # Confidence
    confidence = report.get("confidence", 0)
    confidence_reason = report.get("confidenceReason", "")
    confidence_color = "#16a34a" if confidence >= 90 else "#0f766e" if confidence >= 70 else "#d97706" if confidence >= 50 else "#dc2626"

    exec_summary = report.get("executiveSummary", "")
    verdict_reason = report.get("verdictReason", "")
    pr = report.get("postUpgradeReplay", {})
    ts = report.get("generatedAt", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))

    # Developer changes section
    dev_changes = report.get("developerChanges", [])
    dev_html = ""
    for c in dev_changes:
        p = c.get("priority", "P2")
        color = "#dc2626" if p == "P1" else "#d97706" if p == "P2" else "#6b7280"
        dev_html += (
            f'<div class="card" style="border-left:4px solid {color};margin-bottom:8px;">'
            f'<span style="color:{color};font-weight:700;">[{p}]</span> '
            f'<strong>{_esc(c.get("title",""))}</strong><br>'
            f'<span style="color:#475569;">{_esc(c.get("description",""))}</span></div>'
        )
    if not dev_html:
        dev_html = '<p style="color:#6b7280;">No developer code changes required.</p>'

    # Code analysis findings
    code_analysis = report.get("codeAnalysis")
    code_html = ""
    if code_analysis and code_analysis.get("topFindings"):
        code_html = '<h3>Code Analysis Findings</h3><table><tr><th>File</th><th>Line</th><th>Severity</th><th>Category</th><th>Recommended Change</th></tr>'
        for f in code_analysis["topFindings"][:15]:
            sev = f.get("severity", "warning")
            sev_color = "#dc2626" if sev == "error" else "#d97706"
            code_html += (
                f'<tr><td><code>{_esc(f.get("fileName",""))}</code></td>'
                f'<td>{f.get("lineNumber", "")}</td>'
                f'<td style="color:{sev_color};">{sev.upper()}</td>'
                f'<td>{_esc(f.get("category",""))}</td>'
                f'<td>{_esc(f.get("recommendedChange",""))}</td></tr>'
            )
        code_html += '</table>'
        code_html += (
            f'<p style="margin-top:8px;color:#64748b;font-size:12px;">'
            f'Total findings: {code_analysis.get("totalFindings",0)} '
            f'({code_analysis.get("errors",0)} errors, {code_analysis.get("warnings",0)} warnings)</p>'
        )

    # DBA changes section
    dba_changes = report.get("dbaChanges", [])
    dba_html = ""
    for c in dba_changes:
        p = c.get("priority", "P2")
        color = "#dc2626" if p == "P1" else "#d97706" if p == "P2" else "#6b7280"
        dba_html += (
            f'<div class="card" style="border-left:4px solid {color};margin-bottom:8px;">'
            f'<span style="color:{color};font-weight:700;">[{p}]</span> '
            f'<strong>{_esc(c.get("title",""))}</strong><br>'
            f'<span style="color:#475569;">{_esc(c.get("description",""))}</span></div>'
        )
    if not dba_html:
        dba_html = '<p style="color:#6b7280;">No cluster/parameter changes required.</p>'

    # Cluster analysis / breaking changes
    cluster_analysis = report.get("clusterAnalysis")
    cluster_html = ""
    if cluster_analysis and cluster_analysis.get("topBreakingChanges"):
        cluster_html = '<h3>Breaking Changes from Cluster Analysis</h3><table><tr><th>Severity</th><th>Category</th><th>Description</th><th>Remediation</th></tr>'
        for bc in cluster_analysis["topBreakingChanges"]:
            sev = bc.get("severity", "warning")
            sev_color = "#dc2626" if sev in ("error", "high") else "#d97706"
            cluster_html += (
                f'<tr><td style="color:{sev_color};font-weight:600;">{sev.upper()}</td>'
                f'<td>{_esc(bc.get("category",""))}</td>'
                f'<td>{_esc(bc.get("description",""))}</td>'
                f'<td>{_esc(bc.get("remediation",""))}</td></tr>'
            )
        cluster_html += '</table>'

    # Nice to have
    nice_to_have = report.get("niceToHave", [])
    nth_html = ""
    if nice_to_have:
        nth_html = '<section><h2>Nice to Have</h2>'
        for c in nice_to_have:
            nth_html += (
                f'<div class="card" style="margin-bottom:6px;">'
                f'<strong>{_esc(c.get("title",""))}</strong><br>'
                f'{_esc(c.get("description",""))}</div>'
            )
        nth_html += '</section>'

    # Query failures table
    qf_rows = ""
    for f in report.get("postUpgradeFailures", [])[:15]:
        qf_rows += (
            f'<tr><td><code>{_esc(f.get("digest","")[:80])}</code></td>'
            f'<td style="color:#dc2626;">{_esc(f.get("error","")[:100])}</td></tr>'
        )

    # Query regressions table
    qr_rows = ""
    for qr in report.get("queryRegressions", [])[:15]:
        qr_rows += (
            f'<tr><td><code>{_esc(qr.get("digest","")[:60])}</code></td>'
            f'<td>{qr.get("beforeLatencyMs",0):.1f}ms</td>'
            f'<td>{qr.get("afterLatencyMs",0):.1f}ms</td>'
            f'<td style="color:#dc2626;">+{qr.get("regressionPct",0):.0f}%</td>'
            f'<td>{_esc(qr.get("fix","")[:80])}</td></tr>'
        )

    # Parameter drift table
    pd_rows = ""
    for p in report.get("parameterDrift", [])[:15]:
        pd_rows += (
            f'<tr><td>{_esc(p.get("parameter",""))}</td>'
            f'<td>{_esc(str(p.get("before","")))}</td>'
            f'<td>{_esc(str(p.get("after","")))}</td></tr>'
        )

    # Action items
    action_html = ""
    for item in report.get("actionItems", []):
        p = item.get("priority", "P3")
        color = "#dc2626" if p == "P1" else "#d97706" if p == "P2" else "#6b7280"
        action_html += (
            f'<div class="card" style="border-left:4px solid {color};margin-bottom:8px;">'
            f'<span style="color:{color};font-weight:700;">[{p}]</span> '
            f'<strong>{_esc(item.get("title",""))}</strong><br>'
            f'<span style="color:#475569;">{_esc(item.get("description",""))}</span><br>'
            f'<span style="color:#059669;">Fix: {_esc(item.get("fix",""))}</span></div>'
        )

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Upgrade Readiness Report — {_esc(cluster_id)}</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f8fafc;color:#1e293b;line-height:1.6;padding:24px}}
.container{{max-width:1100px;margin:0 auto}}
.header{{background:linear-gradient(135deg,#1e3a5f,#0f766e);color:white;padding:32px;border-radius:12px;margin-bottom:24px}}
.header h1{{font-size:24px;margin-bottom:4px}}
.header .sub{{opacity:0.85;font-size:13px}}
.verdict{{padding:24px;border-radius:12px;margin-bottom:24px;text-align:center;color:white;font-size:20px;font-weight:700}}
section{{background:white;border-radius:8px;padding:24px;margin-bottom:20px;box-shadow:0 1px 3px rgba(0,0,0,0.1)}}
section h2{{font-size:18px;color:#1e3a5f;border-bottom:2px solid #e2e8f0;padding-bottom:8px;margin-bottom:16px}}
section h3{{font-size:15px;color:#475569;margin:16px 0 8px}}
.card{{border:1px solid #e2e8f0;border-radius:6px;padding:12px;font-size:13px}}
.grid{{display:grid;gap:12px}}.grid-2{{grid-template-columns:1fr 1fr}}.grid-3{{grid-template-columns:1fr 1fr 1fr}}.grid-4{{grid-template-columns:1fr 1fr 1fr 1fr}}
.stat{{text-align:center;padding:16px;background:#f8fafc;border-radius:8px;border:1px solid #e2e8f0}}
.stat .num{{font-size:28px;font-weight:700;color:#0f766e}}.stat .lbl{{font-size:11px;color:#64748b;text-transform:uppercase}}
table{{width:100%;border-collapse:collapse;font-size:13px}}th{{text-align:left;padding:8px;background:#f1f5f9;color:#475569;font-size:12px;text-transform:uppercase}}td{{padding:8px;border-bottom:1px solid #f1f5f9}}
.footer{{text-align:center;color:#94a3b8;font-size:11px;padding:20px}}
@media(max-width:768px){{.grid-2,.grid-3,.grid-4{{grid-template-columns:1fr}}}}
</style></head><body><div class="container">

<div class="header">
<h1>Aurora Upgrade Readiness Report</h1>
<div class="sub">Generated {ts} · Run ID: {run_id}</div>
</div>

<div class="verdict" style="background:{verdict_color};">{verdict_label}</div>

<div style="display:flex;justify-content:center;gap:24px;margin-bottom:24px;">
<div style="text-align:center;padding:12px 24px;background:white;border-radius:8px;box-shadow:0 1px 3px rgba(0,0,0,0.1);">
<div style="font-size:11px;color:#64748b;text-transform:uppercase;">Confidence</div>
<div style="font-size:28px;font-weight:700;color:{confidence_color};">{confidence}%</div>
{'<div style="font-size:11px;color:#64748b;margin-top:4px;">' + _esc(confidence_reason) + '</div>' if confidence_reason else ''}
</div>
</div>

<section>
<h2>Executive Summary</h2>
<p>{_esc(exec_summary)}</p>
{'<p style="margin-top:8px;color:#475569;">' + _esc(verdict_reason) + '</p>' if verdict_reason else ''}
</section>

<section>
<h2>Input Details</h2>
<div class="grid grid-3">
<div class="card"><strong>Source Cluster</strong><br>{_esc(cluster_id)}<br><span style="font-size:11px;color:#64748b;">{_esc(cluster_arn)}</span></div>
<div class="card"><strong>Account / Region</strong><br>{_esc(account)} / {_esc(region)}</div>
<div class="card"><strong>Engine</strong><br>{_esc(report.get("engine",""))}</div>
<div class="card"><strong>Current Version</strong><br>{_esc(report.get("currentVersion",""))}</div>
<div class="card"><strong>Target Version</strong><br>{_esc(report.get("targetVersion",""))}</div>
<div class="card"><strong>Strategy</strong><br>{_esc(report.get("upgradeStrategy",""))}</div>
</div>
</section>

<section>
<h2>Results Summary</h2>
<div class="grid grid-4">
<div class="stat"><div class="num">{len(report.get("actionItems",[]))}</div><div class="lbl">Action Items</div></div>
<div class="stat"><div class="num" style="color:{"#dc2626" if pr.get("failed",0)>0 else "#0f766e"}">{pr.get("failed",0)}</div><div class="lbl">Query Failures</div></div>
<div class="stat"><div class="num" style="color:{"#dc2626" if len(report.get("queryRegressions",[]))>0 else "#0f766e"}">{len(report.get("queryRegressions",[]))}</div><div class="lbl">Query Regressions</div></div>
<div class="stat"><div class="num">{len(report.get("parameterDrift",[]))}</div><div class="lbl">Param Drift</div></div>
</div>
</section>

<section>
<h2>What Developers Must Change</h2>
{dev_html}
{code_html}
</section>

<section>
<h2>What DBAs Must Change</h2>
{dba_html}
{cluster_html}
</section>

{nth_html}

<section>
<h2>Query Failures Post-Upgrade ({len(report.get("postUpgradeFailures",[]))})</h2>
{"<table><tr><th>Query</th><th>Error</th></tr>" + qf_rows + "</table>" if qf_rows else "<p style='color:#6b7280;'>No query failures detected.</p>"}
</section>

<section>
<h2>Query Regressions ({len(report.get("queryRegressions",[]))})</h2>
{"<table><tr><th>Query</th><th>Before</th><th>After</th><th>Regression</th><th>Fix</th></tr>" + qr_rows + "</table>" if qr_rows else "<p style='color:#6b7280;'>No query regressions detected.</p>"}
</section>

<section>
<h2>Parameter Drift ({len(report.get("parameterDrift",[]))})</h2>
{"<table><tr><th>Parameter</th><th>Before</th><th>After</th></tr>" + pd_rows + "</table>" if pd_rows else "<p style='color:#6b7280;'>No parameter drift detected.</p>"}
</section>

<section>
<h2>Prioritized Action Items ({len(report.get("actionItems",[]))})</h2>
{action_html if action_html else "<p style='color:#6b7280;'>No action items.</p>"}
</section>

<div class="footer">RDS Upgrade Assistant · Internal Use Only · {ts}</div>
</div></body></html>"""
