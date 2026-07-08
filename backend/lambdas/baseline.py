"""Baseline Lambda — Collects top queries and parameter group state from the source cluster.

Triggered by: Step Functions (first step in pipeline)
Input: {runId, clusterArn, engine, targetVersion}
Output: Writes baseline to S3, returns payload for Clone step

Query collection (tiered fallback):
  1. Direct DB connection via DB_SECRET_ARN (performance_schema / pg_stat_statements)
     - Provides full sample SQL for query replay post-upgrade
  2. Performance Insights API (no credentials needed)
     - Provides query digests and frequency but NOT executable SQL
     - Query replay will be skipped during validation
  3. Degraded mode — empty query list, validation skips query replay
"""

import json
import logging
import os
import time

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from shared.config import REGION, DB_SECRET_ARN, QUERY_REPLAY_LIMIT
from shared.state import update_phase, mark_failed
from shared.s3_utils import put_json
from shared.rds_client import get_rds_client

rds = None  # initialized per-invocation in handler


def handler(event, context):
    """Lambda entry point."""
    global rds
    run_id = event["runId"]
    cluster_arn = event["clusterArn"]
    engine = event.get("engine", "aurora-mysql")
    target_version = event.get("targetVersion", "")

    # Initialize RDS client (cross-account if needed)
    rds = get_rds_client(cluster_arn)

    try:
        update_phase(run_id, "baseline", progress_pct=5)
        cluster_id = cluster_arn.split(":")[-1]

        # Collect parameter group state from source
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

        # --- Tier 1: Direct DB connection (auto-discovers credentials) ---
        top_queries = []
        query_source = "none"

        if endpoint:
            logger.info("Tier 1: Attempting direct DB query collection from %s", endpoint)
            db_queries = _collect_queries_from_db(endpoint, engine, cluster_id)
            if db_queries:
                top_queries = db_queries
                query_source = "performance_schema" if "mysql" in engine else "pg_stat_statements"
                logger.info("Tier 1 success: Collected %d queries from %s",
                            len(top_queries), query_source)

        # --- Tier 2: Performance Insights fallback (no credentials needed) ---
        if not top_queries:
            logger.info("Tier 2: Falling back to Performance Insights")
            pi_queries = _collect_from_performance_insights(cluster, engine)
            if pi_queries:
                top_queries = pi_queries
                query_source = "performance_insights"
                logger.info("Tier 2 success: Collected %d queries from Performance Insights",
                            len(top_queries))

        # --- Tier 3: Degraded mode ---
        if not top_queries:
            logger.warning("Tier 3: No queries collected. Validation will skip query replay.")
            query_source = "none"

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

        # Return payload for the next step (Clone)
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


# ---------------------------------------------------------------------------
# Tier 1: Direct DB connection via Secrets Manager
# ---------------------------------------------------------------------------

def _get_db_credentials(cluster_endpoint, engine, cluster_id=""):
    """Retrieve DB credentials using tiered secret resolution:
    
    1. Check if cluster has RDS-managed secret (MasterUserSecret)
    2. Convention-based lookup: aurora-upgrade-planner/db-creds/{cluster-id}
    3. Fall back to DB_SECRET_ARN env var (legacy single-cluster mode)
    
    Returns dict with {host, port, user, password, dbname} or None.
    """
    sm = boto3.client("secretsmanager", region_name=REGION)

    # Tier 1: RDS-managed secret (auto-discovery from cluster metadata)
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


def _collect_queries_from_db(cluster_endpoint, engine, cluster_id=""):
    """Collect top queries from performance_schema (MySQL) or pg_stat_statements (PostgreSQL).

    SECURITY NOTE: All SQL executed here is hardcoded SELECT queries against
    AWS-managed system tables (performance_schema, pg_stat_statements). No user
    input is interpolated into SQL. The query text COLLECTED (sampleSql) is stored
    in S3 and replayed later — see validation.py for the replay trust boundary.

    Collects three categories (deduplicated):
      1. Top N by call frequency (hot path queries)
      2. Top N/2 by max latency (complex/slow queries likely to regress)
      3. Top N/4 by rows_examined (full table scans that could blow up post-upgrade)
    """
    creds = _get_db_credentials(cluster_endpoint, engine, cluster_id)
    conn, cur = _get_db_connection(creds, engine)
    if not conn:
        logger.info("No DB connection — skipping live query collection")
        return []

    queries_by_id = {}
    freq_limit = QUERY_REPLAY_LIMIT
    latency_limit = max(QUERY_REPLAY_LIMIT // 2, 10)
    rows_limit = max(QUERY_REPLAY_LIMIT // 4, 5)

    try:
        if "mysql" in engine:
            _collect_mysql_queries(cur, queries_by_id, freq_limit, latency_limit, rows_limit)
        else:
            _collect_pg_queries(cur, queries_by_id, freq_limit, latency_limit, rows_limit)

        queries = list(queries_by_id.values())
        freq_count = sum(1 for q in queries if q.get("source") == "frequency")
        latency_count = sum(1 for q in queries if q.get("source") == "latency")
        rows_count = sum(1 for q in queries if q.get("source") == "rows_examined")
        logger.info("Collected %d queries from DB (%s): %d frequency, %d latency, %d rows_examined",
                    len(queries), engine, freq_count, latency_count, rows_count)
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


def _collect_mysql_queries(cur, queries_by_id, freq_limit, latency_limit, rows_limit):
    """Collect MySQL queries from performance_schema.

    Compatible with both MySQL 5.7 and 8.0:
    - 5.7 does not have QUERY_SAMPLE_TEXT column
    - 8.0+ has QUERY_SAMPLE_TEXT for full sample SQL
    """
    # Detect if QUERY_SAMPLE_TEXT is available (MySQL 8.0+)
    has_sample_text = False
    try:
        cur.execute("""
            SELECT COLUMN_NAME FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = 'performance_schema'
              AND TABLE_NAME = 'events_statements_summary_by_digest'
              AND COLUMN_NAME = 'QUERY_SAMPLE_TEXT'
        """)
        has_sample_text = len(cur.fetchall()) > 0
    except Exception:
        pass

    sample_col = "QUERY_SAMPLE_TEXT" if has_sample_text else "DIGEST_TEXT"

    # Top by frequency
    cur.execute(f"""
        SELECT
            DIGEST                          AS query_id,
            DIGEST_TEXT                     AS digest,
            SCHEMA_NAME                     AS schema_name,
            COUNT_STAR                      AS call_count,
            ROUND(AVG_TIMER_WAIT / 1e9, 2)  AS avg_latency_ms,
            ROUND(MAX_TIMER_WAIT / 1e9, 2)  AS max_latency_ms,
            {sample_col}                    AS sample_sql,
            SUM_ROWS_EXAMINED               AS rows_examined
        FROM performance_schema.events_statements_summary_by_digest
        WHERE DIGEST_TEXT IS NOT NULL
          AND SCHEMA_NAME NOT IN ('performance_schema','information_schema','mysql','sys')
        ORDER BY COUNT_STAR DESC
        LIMIT %s
    """, (freq_limit,))
    for row in cur.fetchall():
        _add_query(queries_by_id, row, "frequency")

    # Top by latency
    cur.execute(f"""
        SELECT
            DIGEST, DIGEST_TEXT, SCHEMA_NAME, COUNT_STAR,
            ROUND(AVG_TIMER_WAIT / 1e9, 2),
            ROUND(MAX_TIMER_WAIT / 1e9, 2),
            {sample_col}, SUM_ROWS_EXAMINED
        FROM performance_schema.events_statements_summary_by_digest
        WHERE DIGEST_TEXT IS NOT NULL
          AND SCHEMA_NAME NOT IN ('performance_schema','information_schema','mysql','sys')
          AND MAX_TIMER_WAIT > 0
        ORDER BY MAX_TIMER_WAIT DESC
        LIMIT %s
    """, (latency_limit,))
    for row in cur.fetchall():
        _add_query(queries_by_id, row, "latency")

    # Top by rows examined
    cur.execute(f"""
        SELECT
            DIGEST, DIGEST_TEXT, SCHEMA_NAME, COUNT_STAR,
            ROUND(AVG_TIMER_WAIT / 1e9, 2),
            ROUND(MAX_TIMER_WAIT / 1e9, 2),
            {sample_col}, SUM_ROWS_EXAMINED
        FROM performance_schema.events_statements_summary_by_digest
        WHERE DIGEST_TEXT IS NOT NULL
          AND SCHEMA_NAME NOT IN ('performance_schema','information_schema','mysql','sys')
          AND SUM_ROWS_EXAMINED > 0
        ORDER BY SUM_ROWS_EXAMINED DESC
        LIMIT %s
    """, (rows_limit,))
    for row in cur.fetchall():
        _add_query(queries_by_id, row, "rows_examined")


def _collect_pg_queries(cur, queries_by_id, freq_limit, latency_limit, rows_limit):
    """Collect PostgreSQL queries from pg_stat_statements.
    
    Compatible with PG 11 (mean_time/max_time) and PG 13+ (mean_exec_time/max_exec_time).
    """
    # Detect column names — PG 13+ renamed mean_time → mean_exec_time
    try:
        cur.execute("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'pg_stat_statements' AND column_name = 'mean_exec_time'
        """)
        has_exec_time = len(cur.fetchall()) > 0
    except Exception:
        has_exec_time = False

    mean_col = "mean_exec_time" if has_exec_time else "mean_time"
    max_col = "max_exec_time" if has_exec_time else "max_time"

    # Top by frequency
    cur.execute(f"""
        SELECT
            queryid::text       AS query_id,
            query               AS digest,
            calls               AS call_count,
            ROUND(({mean_col})::numeric, 2) AS avg_latency_ms,
            ROUND(({max_col})::numeric, 2)  AS max_latency_ms,
            query               AS sample_sql,
            rows                AS rows_returned
        FROM pg_stat_statements
        WHERE query NOT LIKE '%%pg_stat%%'
          AND query NOT LIKE '%%pg_catalog%%'
        ORDER BY calls DESC
        LIMIT %s
    """, (freq_limit,))
    for row in cur.fetchall():
        _add_pg_query(queries_by_id, row, "frequency")

    # Top by latency
    cur.execute(f"""
        SELECT
            queryid::text, query, calls,
            ROUND(({mean_col})::numeric, 2),
            ROUND(({max_col})::numeric, 2),
            query, rows
        FROM pg_stat_statements
        WHERE query NOT LIKE '%%pg_stat%%'
          AND query NOT LIKE '%%pg_catalog%%'
          AND {max_col} > 0
        ORDER BY {max_col} DESC
        LIMIT %s
    """, (latency_limit,))
    for row in cur.fetchall():
        _add_pg_query(queries_by_id, row, "latency")

    # Top by rows
    cur.execute(f"""
        SELECT
            queryid::text, query, calls,
            ROUND(({mean_col})::numeric, 2),
            ROUND(({max_col})::numeric, 2),
            query, rows
        FROM pg_stat_statements
        WHERE query NOT LIKE '%%pg_stat%%'
          AND query NOT LIKE '%%pg_catalog%%'
          AND rows > 0
        ORDER BY rows DESC
        LIMIT %s
    """, (rows_limit,))
    for row in cur.fetchall():
        _add_pg_query(queries_by_id, row, "rows_examined")


def _add_query(queries_by_id, row, source):
    """Add a MySQL query to the deduplicated collection."""
    qid = (row[0] or "")[:32]
    if not qid or qid in queries_by_id:
        return
    queries_by_id[qid] = {
        "queryId": qid,
        "digest": (row[1] or "")[:300],
        "schema": row[2] or "",
        "callCount": int(row[3] or 0),
        "avgLatencyMs": float(row[4] or 0),
        "maxLatencyMs": float(row[5] or 0),
        "sampleSql": (row[6] or "")[:500],
        "rowsExamined": int(row[7] or 0),
        "source": source,
    }


def _add_pg_query(queries_by_id, row, source):
    """Add a PostgreSQL query to the deduplicated collection."""
    qid = (row[0] or "")[:32]
    if not qid or qid in queries_by_id:
        return
    queries_by_id[qid] = {
        "queryId": qid,
        "digest": (row[1] or "")[:300],
        "schema": "public",
        "callCount": int(row[2] or 0),
        "avgLatencyMs": float(row[3] or 0),
        "maxLatencyMs": float(row[4] or 0),
        "sampleSql": (row[5] or "")[:500],
        "rowsExamined": int(row[6] or 0),
        "source": source,
    }


# ---------------------------------------------------------------------------
# Tier 2: Performance Insights fallback
# ---------------------------------------------------------------------------

def _collect_from_performance_insights(cluster, engine):
    """Collect query digests from Performance Insights (no DB credentials needed).

    PI provides query digests and call frequency but NOT full sample SQL.
    Queries collected here cannot be replayed — only used for reporting.
    """
    queries = []
    try:
        pi = boto3.client("pi", region_name=REGION)
        members = cluster.get("DBClusterMembers", [])
        writer_id = next(
            (m["DBInstanceIdentifier"] for m in members if m.get("IsClusterWriter")), None
        )
        if not writer_id:
            logger.warning("No writer instance found for PI fallback")
            return []

        resource_id = rds.describe_db_instances(
            DBInstanceIdentifier=writer_id
        )["DBInstances"][0].get("DbiResourceId", "")
        if not resource_id:
            logger.warning("No DbiResourceId for PI fallback")
            return []

        pi_resp = pi.get_resource_metrics(
            ServiceType="RDS",
            Identifier=resource_id,
            MetricQueries=[{
                "Metric": "db.sql_tokenized.stats.calls_per_sec.avg",
                "GroupBy": {"Group": "db.sql_tokenized", "Limit": min(QUERY_REPLAY_LIMIT, 25)},
            }],
            StartTime=time.time() - 3600,
            EndTime=time.time(),
            PeriodInSeconds=3600,
        )
        for key_data in pi_resp.get("MetricList", []):
            for dp in key_data.get("DataPoints", []):
                for dim_key, dim_val in dp.get("Value", {}).items():
                    if dim_key != "total":
                        queries.append({
                            "queryId": dim_key[:32],
                            "digest": dim_key[:300],
                            "sampleSql": "",  # PI doesn't provide executable SQL
                            "schema": "",
                            "callCount": int(dim_val * 3600) if dim_val else 0,
                            "avgLatencyMs": 0,
                            "maxLatencyMs": 0,
                            "rowsExamined": 0,
                            "source": "performance_insights",
                        })
        if queries:
            logger.info("Collected %d queries from Performance Insights", len(queries))
    except Exception as e:
        logger.warning("Performance Insights collection failed: %s", e)

    return queries
