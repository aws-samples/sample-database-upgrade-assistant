"""Baseline Lambda — Collects top queries and parameter group state.

Triggered by: API handler (async invoke)
Input: {runId, clusterArn, engine, targetVersion}
Output: Writes baseline to S3, invokes Compatibility AgentCore, then Clone Lambda

Query collection priority:
  1. Direct DB connection via DB_SECRET_ARN (performance_schema / pg_stat_statements)
  2. Performance Insights as fallback if DB connection fails
"""

import json
import logging
import os
import time

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Import shared utilities via Lambda layer or relative path
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from shared.config import REGION, TABLE_PREFIX, DB_SECRET_ARN, QUERY_REPLAY_LIMIT
from shared.state import update_phase, update_run, mark_failed
from shared.s3_utils import put_json

rds = boto3.client("rds", region_name=REGION)


# ---------------------------------------------------------------------------
# DB connectivity helpers
# ---------------------------------------------------------------------------

def _get_db_credentials(cluster_endpoint, engine):
    """
    Retrieve DB credentials from Secrets Manager (DB_SECRET_ARN).
    Returns dict with {host, port, user, password, dbname} or None if unavailable.
    """
    if not DB_SECRET_ARN:
        return None
    try:
        sm = boto3.client("secretsmanager", region_name=REGION)
        secret = json.loads(sm.get_secret_value(SecretId=DB_SECRET_ARN)["SecretString"])
        return {
            "host": secret.get("host", cluster_endpoint),
            "port": int(secret.get("port", 3306 if "mysql" in engine else 5432)),
            "user": secret.get("username", secret.get("user", "admin")),
            "password": secret.get("password", ""),
            "dbname": secret.get("dbname", secret.get("database", "information_schema")),
        }
    except Exception as e:
        logger.warning("Could not retrieve DB credentials from Secrets Manager: %s", e)
        return None


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
                database=creds["dbname"], connect_timeout=10,
                autocommit=True,
            )
            return conn, conn.cursor()
        else:
            import psycopg2
            conn = psycopg2.connect(
                host=creds["host"], port=creds["port"],
                user=creds["user"], password=creds["password"],
                dbname=creds["dbname"], connect_timeout=10,
            )
            conn.autocommit = True
            return conn, conn.cursor()
    except Exception as e:
        logger.warning("DB connection failed: %s", e)
        return None, None


def _collect_queries_from_db(cluster_endpoint, engine):
    """
    Collect top queries from performance_schema (MySQL) or pg_stat_statements (PostgreSQL).
    Collects two sets:
      1. Top N by call frequency (hot path queries)
      2. Top N by max latency (complex/slow queries most likely to regress)
    Deduplicates by queryId and returns the merged set.
    Returns list of {queryId, digest, sampleSql, schema, callCount, avgLatencyMs, maxLatencyMs, source}.
    Falls back to empty list if DB is unreachable or credentials unavailable.
    """
    creds = _get_db_credentials(cluster_endpoint, engine)
    conn, cur = _get_db_connection(creds, engine)
    queries_by_id = {}

    if not conn:
        logger.info("No DB connection — skipping live query collection")
        return []

    # How many to collect from each category
    freq_limit = QUERY_REPLAY_LIMIT  # Top N by frequency
    latency_limit = max(QUERY_REPLAY_LIMIT // 2, 10)  # Top N/2 by latency (at least 10)

    try:
        if "mysql" in engine:
            # Top by frequency
            cur.execute("""
                SELECT
                    DIGEST                          AS query_id,
                    DIGEST_TEXT                     AS digest,
                    SCHEMA_NAME                     AS schema_name,
                    COUNT_STAR                      AS call_count,
                    ROUND(AVG_TIMER_WAIT / 1e9, 2)  AS avg_latency_ms,
                    ROUND(MAX_TIMER_WAIT / 1e9, 2)  AS max_latency_ms,
                    QUERY_SAMPLE_TEXT               AS sample_sql
                FROM performance_schema.events_statements_summary_by_digest
                WHERE DIGEST_TEXT IS NOT NULL
                  AND SCHEMA_NAME NOT IN ('performance_schema','information_schema','mysql','sys')
                ORDER BY COUNT_STAR DESC
                LIMIT %s
            """, (freq_limit,))
            for row in cur.fetchall():
                qid = (row[0] or "")[:32]
                if qid and qid not in queries_by_id:
                    queries_by_id[qid] = {
                        "queryId": qid,
                        "digest": (row[1] or "")[:300],
                        "schema": row[2] or "",
                        "callCount": int(row[3] or 0),
                        "avgLatencyMs": float(row[4] or 0),
                        "maxLatencyMs": float(row[5] or 0),
                        "sampleSql": (row[6] or "")[:500],
                        "source": "frequency",
                    }

            # Top by latency (complex/slow queries)
            cur.execute("""
                SELECT
                    DIGEST                          AS query_id,
                    DIGEST_TEXT                     AS digest,
                    SCHEMA_NAME                     AS schema_name,
                    COUNT_STAR                      AS call_count,
                    ROUND(AVG_TIMER_WAIT / 1e9, 2)  AS avg_latency_ms,
                    ROUND(MAX_TIMER_WAIT / 1e9, 2)  AS max_latency_ms,
                    QUERY_SAMPLE_TEXT               AS sample_sql
                FROM performance_schema.events_statements_summary_by_digest
                WHERE DIGEST_TEXT IS NOT NULL
                  AND SCHEMA_NAME NOT IN ('performance_schema','information_schema','mysql','sys')
                  AND MAX_TIMER_WAIT > 0
                ORDER BY MAX_TIMER_WAIT DESC
                LIMIT %s
            """, (latency_limit,))
            for row in cur.fetchall():
                qid = (row[0] or "")[:32]
                if qid and qid not in queries_by_id:
                    queries_by_id[qid] = {
                        "queryId": qid,
                        "digest": (row[1] or "")[:300],
                        "schema": row[2] or "",
                        "callCount": int(row[3] or 0),
                        "avgLatencyMs": float(row[4] or 0),
                        "maxLatencyMs": float(row[5] or 0),
                        "sampleSql": (row[6] or "")[:500],
                        "source": "latency",
                    }
        else:
            # PostgreSQL — top by frequency
            cur.execute("""
                SELECT
                    queryid::text                           AS query_id,
                    query                                   AS digest,
                    calls                                   AS call_count,
                    ROUND((mean_exec_time)::numeric, 2)     AS avg_latency_ms,
                    ROUND((max_exec_time)::numeric, 2)      AS max_latency_ms,
                    query                                   AS sample_sql
                FROM pg_stat_statements
                WHERE query NOT LIKE '%%pg_stat%%'
                ORDER BY calls DESC
                LIMIT %s
            """, (freq_limit,))
            for row in cur.fetchall():
                qid = (row[0] or "")[:32]
                if qid and qid not in queries_by_id:
                    queries_by_id[qid] = {
                        "queryId": qid,
                        "digest": (row[1] or "")[:300],
                        "schema": "public",
                        "callCount": int(row[2] or 0),
                        "avgLatencyMs": float(row[3] or 0),
                        "maxLatencyMs": float(row[4] or 0),
                        "sampleSql": (row[5] or "")[:500],
                        "source": "frequency",
                    }

            # PostgreSQL — top by latency
            cur.execute("""
                SELECT
                    queryid::text                           AS query_id,
                    query                                   AS digest,
                    calls                                   AS call_count,
                    ROUND((mean_exec_time)::numeric, 2)     AS avg_latency_ms,
                    ROUND((max_exec_time)::numeric, 2)      AS max_latency_ms,
                    query                                   AS sample_sql
                FROM pg_stat_statements
                WHERE query NOT LIKE '%%pg_stat%%'
                  AND max_exec_time > 0
                ORDER BY max_exec_time DESC
                LIMIT %s
            """, (latency_limit,))
            for row in cur.fetchall():
                qid = (row[0] or "")[:32]
                if qid and qid not in queries_by_id:
                    queries_by_id[qid] = {
                        "queryId": qid,
                        "digest": (row[1] or "")[:300],
                        "schema": "public",
                        "callCount": int(row[2] or 0),
                        "avgLatencyMs": float(row[3] or 0),
                        "maxLatencyMs": float(row[4] or 0),
                        "sampleSql": (row[5] or "")[:500],
                        "source": "latency",
                    }

        queries = list(queries_by_id.values())
        freq_count = sum(1 for q in queries if q.get("source") == "frequency")
        latency_count = sum(1 for q in queries if q.get("source") == "latency")
        logger.info("Collected %d queries from DB (%s): %d by frequency, %d by latency",
                    len(queries), engine, freq_count, latency_count)
    except Exception as e:
        logger.warning("Failed to collect queries from DB: %s", e)
        queries = []
    finally:
        try:
            cur.close()
            conn.close()
        except Exception:
            pass

    return queries


def handler(event, context):
    """Lambda entry point."""
    run_id = event["runId"]
    cluster_arn = event["clusterArn"]
    engine = event.get("engine", "aurora-mysql")
    target_version = event.get("targetVersion", "")

    try:
        update_phase(run_id, "baseline", progress_pct=5)
        cluster_id = cluster_arn.split(":")[-1]

        # Collect parameter group state
        logger.info("Collecting parameter group for %s", cluster_id)
        resp = rds.describe_db_clusters(DBClusterIdentifier=cluster_id)
        cluster = resp["DBClusters"][0]
        pg_name = cluster.get("DBClusterParameterGroup", "")
        endpoint = cluster.get("Endpoint", "")

        params = []
        if pg_name:
            try:
                pg_resp = rds.describe_db_cluster_parameters(
                    DBClusterParameterGroupName=pg_name, Source="user"
                )
                params = [
                    {"name": p["ParameterName"], "value": p.get("ParameterValue", "")}
                    for p in pg_resp.get("Parameters", [])[:50]
                ]
            except Exception as e:
                logger.warning("Could not get parameters: %s", e)

        # --- Primary: collect queries from performance_schema / pg_stat_statements ---
        top_queries = []
        query_source = "none"

        if endpoint:
            logger.info("Attempting direct DB query collection from %s", endpoint)
            db_queries = _collect_queries_from_db(endpoint, engine)
            if db_queries:
                top_queries = db_queries
                query_source = "performance_schema" if "mysql" in engine else "pg_stat_statements"
                logger.info("Collected %d queries from %s", len(top_queries), query_source)

        # --- Fallback: Performance Insights if DB connection failed ---
        if not top_queries:
            try:
                pi = boto3.client("pi", region_name=REGION)
                members = cluster.get("DBClusterMembers", [])
                writer_id = next(
                    (m["DBInstanceIdentifier"] for m in members if m.get("IsClusterWriter")), None
                )
                if writer_id:
                    resource_id = rds.describe_db_instances(
                        DBInstanceIdentifier=writer_id
                    )["DBInstances"][0].get("DbiResourceId", "")
                    if resource_id:
                        pi_resp = pi.get_resource_metrics(
                            ServiceType="RDS",
                            Identifier=resource_id,
                            MetricQueries=[{
                                "Metric": "db.sql_tokenized.stats.calls_per_sec.avg",
                                "GroupBy": {"Group": "db.sql_tokenized", "Limit": QUERY_REPLAY_LIMIT},
                            }],
                            StartTime=time.time() - 3600,
                            EndTime=time.time(),
                            PeriodInSeconds=3600,
                        )
                        for key_data in pi_resp.get("MetricList", []):
                            for dp in key_data.get("DataPoints", []):
                                for dim_key, dim_val in dp.get("Value", {}).items():
                                    if dim_key != "total":
                                        top_queries.append({
                                            "queryId": dim_key[:16],
                                            "digest": dim_key[:300],
                                            "sampleSql": dim_key[:500],
                                            "schema": "",
                                            "callCount": int(dim_val * 3600) if dim_val else 0,
                                            "avgLatencyMs": 0,
                                            "maxLatencyMs": 0,
                                        })
                        if top_queries:
                            query_source = "performance_insights"
                            logger.info("Collected %d queries from Performance Insights (fallback)",
                                        len(top_queries))
            except Exception as e:
                logger.warning("Performance Insights not available: %s", e)

        # Build baseline
        baseline = {
            "collectedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "engine": engine,
            "clusterId": cluster_id,
            "clusterEndpoint": endpoint,
            "parameterGroup": pg_name,
            "parameters": params,
            "topQueries": top_queries[:QUERY_REPLAY_LIMIT],
            "querySource": query_source,
        }

        # Save to S3
        put_json(f"upgrade-tests/{run_id}/baseline.json", baseline)
        update_phase(run_id, "baseline_complete", progress_pct=15)

        # Return payload for the next step (Clone) — Step Functions passes this forward
        return {
            "runId": run_id,
            "clusterArn": cluster_arn,
            "engine": engine,
            "targetVersion": target_version,
            "currentVersion": cluster.get("EngineVersion", ""),
        }

    except Exception as e:
        logger.exception("Baseline failed: %s", e)
        mark_failed(run_id, str(e))
        raise
