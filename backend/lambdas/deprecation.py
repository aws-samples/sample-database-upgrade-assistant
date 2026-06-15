"""Deprecation Lambda — Builds the version catalog using RDS API + AgentCore.

Triggered by: API handler (async invoke)
Input: {runId}
Output: Writes deprecation_catalog.json to S3, updates DynamoDB run record

Flow:
  1. Collect engine versions from RDS API (deterministic)
  2. Fetch AWS + community documentation for each engine (HTTP)
  3. Send versions + documentation text to AgentCore catalog_extract endpoint (LLM)
  4. If AgentCore fails, use FALLBACK breaking changes (safety net)
  5. Merge known + extracted changes with deduplication
  6. Build catalog with complete EOL dates
"""

import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from shared.config import REGION, TABLE_PREFIX, S3_BUCKET, AGENTCORE_CATALOG_EXTRACT_ENDPOINT
from shared.state import update_phase, update_run, mark_complete, mark_failed
from shared.s3_utils import put_json

rds = boto3.client("rds", region_name=REGION)

# ---------------------------------------------------------------------------
# AWS Documentation URLs to fetch for breaking changes
# ---------------------------------------------------------------------------
AWS_DOC_URLS = {
    "aurora-mysql": [
        "https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/AuroraMySQL.mysql80-upgrade-procedure.html",
        "https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/AuroraMySQL.Updates.Upgrading.html",
        "https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/AuroraMySQL.Update.SpecialVersions.html",
    ],
    "aurora-postgresql": [
        "https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/USER_UpgradeDBInstance.PostgreSQL.html",
        "https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/AuroraPostgreSQL.Updates.html",
    ],
}

# Community documentation URLs
COMMUNITY_DOC_URLS = {
    "aurora-mysql": [
        "https://dev.mysql.com/doc/refman/8.0/en/upgrading-from-previous-series.html",
    ],
    "aurora-postgresql": [
        "https://www.postgresql.org/docs/15/release.html",
    ],
}

# ---------------------------------------------------------------------------
# Known EOL dates (hardcoded from AWS docs — these are factual dates)
# ---------------------------------------------------------------------------
KNOWN_EOL_DATES = {
    "aurora-mysql": {
        "5.7": "2024-10-31",
    },
    "aurora-postgresql": {
        "11": "2024-02-29",
        "12": "2025-02-28",
        "13": "2026-02-28",
        "14": "2026-11-30",
    },
}

# ---------------------------------------------------------------------------
# Fallback breaking changes (used ONLY if doc fetch + AgentCore extraction fails)
# ---------------------------------------------------------------------------
FALLBACK_MYSQL_BREAKING_CHANGES = [
    {"category": "sql-syntax", "description": "SQL_CALC_FOUND_ROWS removed in MySQL 8.0", "remediation": "Use COUNT(*) OVER() window function", "severity": "error"},
    {"category": "sql-syntax", "description": "FOUND_ROWS() function removed in MySQL 8.0", "remediation": "Use separate COUNT(*) query", "severity": "error"},
    {"category": "sql-syntax", "description": "GROUP BY implicit sorting removed in MySQL 8.0", "remediation": "Add explicit ORDER BY clause", "severity": "error"},
    {"category": "sql-syntax", "description": "PASSWORD() function removed in MySQL 8.0", "remediation": "Use SHA2() or app-level hashing", "severity": "error"},
    {"category": "sql-syntax", "description": "GRANT...IDENTIFIED BY removed in MySQL 8.0", "remediation": "Use separate CREATE USER + GRANT", "severity": "error"},
    {"category": "query-cache", "description": "Query cache removed in MySQL 8.0 (SQL_CACHE, SQL_NO_CACHE, query_cache_type, query_cache_size)", "remediation": "Remove all query cache references", "severity": "error"},
    {"category": "auth", "description": "mysql_native_password deprecated in MySQL 8.0", "remediation": "Use caching_sha2_password", "severity": "warning"},
    {"category": "charset", "description": "Default charset changed from latin1 to utf8mb4", "remediation": "Set charset=utf8mb4 explicitly", "severity": "warning"},
    {"category": "info-schema", "description": "INNODB_SYS_* tables renamed (remove SYS_ prefix)", "remediation": "Update references to new names", "severity": "error"},
    {"category": "reserved-keyword", "description": "RANK, GROUPS, SYSTEM, ROW_NUMBER etc. now reserved", "remediation": "Quote with backticks", "severity": "error"},
    {"category": "driver", "description": "MySQL drivers require minimum versions for 8.0 compatibility", "remediation": "Upgrade to MySQL Connector 8.0.23+", "severity": "error"},
]

FALLBACK_PG_BREAKING_CHANGES = [
    {"category": "config", "description": "default_with_oids removed in PostgreSQL 12", "remediation": "Use regular columns instead of OIDs", "severity": "error"},
    {"category": "config", "description": "wal_level=archive removed", "remediation": "Use wal_level=replica", "severity": "error"},
    {"category": "config", "description": "vacuum_defer_cleanup_age removed in PostgreSQL 16", "remediation": "Remove from configuration", "severity": "error"},
    {"category": "auth", "description": "md5 auth deprecated in favor of scram-sha-256", "remediation": "Update pg_hba.conf to use scram-sha-256", "severity": "warning"},
]


# ---------------------------------------------------------------------------
# HTML to text helper
# ---------------------------------------------------------------------------
class _HTMLTextExtractor(HTMLParser):
    """Simple HTML to text converter that filters out script/style/nav."""

    def __init__(self):
        super().__init__()
        self._text = []
        self._skip = False

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "nav", "footer", "header"):
            self._skip = True

    def handle_endtag(self, tag):
        if tag in ("script", "style", "nav", "footer", "header"):
            self._skip = False
        if tag in ("p", "div", "li", "h1", "h2", "h3", "h4", "br", "tr"):
            self._text.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self._text.append(data)

    def get_text(self):
        return "".join(self._text)


def _html_to_text(html):
    """Convert HTML to plain text."""
    parser = _HTMLTextExtractor()
    parser.feed(html)
    return parser.get_text()


# ---------------------------------------------------------------------------
# Documentation fetching
# ---------------------------------------------------------------------------
def fetch_documentation(engine):
    """Fetch AWS and community documentation for an engine type.

    Returns concatenated text content from all doc URLs.
    Uses urllib.request (available in Lambda without extra dependencies).
    """
    all_text = []
    urls = AWS_DOC_URLS.get(engine, []) + COMMUNITY_DOC_URLS.get(engine, [])

    for url in urls:
        try:
            logger.info("Fetching documentation: %s", url)
            req = Request(url, headers={
                "User-Agent": "Mozilla/5.0 (compatible; RDSUpgradeAssistant/5.0)"
            })
            resp = urlopen(req, timeout=30)
            html = resp.read().decode("utf-8", errors="replace")
            text = _html_to_text(html)
            # Truncate to ~15K chars per page to stay within LLM context
            text = text[:15000]
            all_text.append(f"--- Source: {url} ---\n{text}\n")
            logger.info("Fetched %d chars from %s", len(text), url)
        except HTTPError as e:
            logger.warning("Failed to fetch %s: HTTP %s", url, e.code)
        except Exception as e:
            logger.warning("Error fetching %s: %s", url, e)

    combined = "\n".join(all_text)
    logger.info("Total documentation fetched for %s: %d chars from %d URLs",
                engine, len(combined), len(urls))
    return combined


# ---------------------------------------------------------------------------
# Lambda handler
# ---------------------------------------------------------------------------
def handler(event, context):
    """Lambda entry point."""
    run_id = event["runId"]

    try:
        update_phase(run_id, "collecting_versions", progress_pct=5)

        # Step 1: Collect all Aurora engine versions from RDS API
        versions_data = _collect_engine_versions()
        update_phase(run_id, "fetching_documentation", progress_pct=25)

        # Step 2: Fetch AWS + community documentation for each engine
        documentation = {}
        for engine in ("aurora-mysql", "aurora-postgresql"):
            documentation[engine] = fetch_documentation(engine)
        update_phase(run_id, "extracting_breaking_changes", progress_pct=40)

        # Step 3: Send versions + documentation to AgentCore for LLM extraction
        breaking_changes = _extract_breaking_changes(versions_data, documentation)
        update_phase(run_id, "building_catalog", progress_pct=70)

        # Step 4: Build the catalog (merges known + extracted, applies EOL dates)
        catalog = _build_catalog(versions_data, breaking_changes)

        # Step 5: Save to S3
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        put_json("deprecation_catalog.json", catalog)
        put_json(f"deprecation_catalog_{timestamp}.json", catalog)

        # Step 6: Mark complete
        mysql_versions = catalog.get("engines", {}).get("aurora-mysql", {}).get("versions", [])
        pg_versions = catalog.get("engines", {}).get("aurora-postgresql", {}).get("versions", [])
        total = len(mysql_versions) + len(pg_versions)
        deprecated = sum(1 for v in mysql_versions + pg_versions if v.get("status") == "deprecated")

        summary = {
            "totalVersions": total,
            "deprecatedVersions": deprecated,
            "engines": 2,
            "catalogVersion": timestamp,
        }
        mark_complete(run_id, summary, s3_report_key="deprecation_catalog.json")
        logger.info("Deprecation catalog built: %d versions, %d deprecated", total, deprecated)

    except Exception as e:
        logger.exception("Deprecation analysis failed: %s", e)
        mark_failed(run_id, str(e))


# ---------------------------------------------------------------------------
# Step 1: Collect engine versions from RDS API
# ---------------------------------------------------------------------------
def _collect_engine_versions():
    """Collect all Aurora engine versions from RDS API."""
    engines = {}
    for engine in ("aurora-mysql", "aurora-postgresql"):
        versions = []
        paginator = rds.get_paginator("describe_db_engine_versions")
        for page in paginator.paginate(Engine=engine):
            for v in page.get("DBEngineVersions", []):
                upgrade_targets = [
                    t["EngineVersion"] for t in v.get("ValidUpgradeTarget", [])
                ]
                versions.append({
                    "version": v.get("EngineVersion", ""),
                    "parameterFamily": v.get("DBParameterGroupFamily", ""),
                    "status": v.get("Status", "available"),
                    "upgradeTargets": upgrade_targets,
                    "supportsGlobalDatabases": v.get("SupportsGlobalDatabases", False),
                })
        engines[engine] = versions
        logger.info("Collected %d versions for %s", len(versions), engine)
    return engines


# ---------------------------------------------------------------------------
# Step 3: Extract breaking changes via AgentCore (with fallback)
# ---------------------------------------------------------------------------
def _extract_breaking_changes(versions_data, documentation):
    """Call AgentCore catalog_extract agent to extract breaking changes.

    Sends both the version list AND fetched documentation text so the LLM
    can extract real breaking changes from the docs rather than relying
    solely on parametric knowledge.

    Falls back to FALLBACK_*_BREAKING_CHANGES if AgentCore is unavailable.
    """
    # Build context for the agent
    context = json.dumps({
        "aurora-mysql": {
            "versions": [v["version"] for v in versions_data.get("aurora-mysql", [])],
            "upgrade_paths": [
                {"from": v["version"], "to": v["upgradeTargets"]}
                for v in versions_data.get("aurora-mysql", [])
                if v["upgradeTargets"]
            ][:20],
        },
        "aurora-postgresql": {
            "versions": [v["version"] for v in versions_data.get("aurora-postgresql", [])],
            "upgrade_paths": [
                {"from": v["version"], "to": v["upgradeTargets"]}
                for v in versions_data.get("aurora-postgresql", [])
                if v["upgradeTargets"]
            ][:20],
        },
    }, indent=2)

    # Include fetched documentation text in the payload
    mysql_docs = documentation.get("aurora-mysql", "")
    pg_docs = documentation.get("aurora-postgresql", "")

    input_text = (
        "Here are the Aurora engine versions and upgrade paths from the RDS API:\n\n"
        f"{context}\n\n"
    )
    if mysql_docs:
        input_text += (
            "## AWS & Community Documentation — Aurora MySQL Upgrade\n"
            f"{mysql_docs[:20000]}\n\n"
        )
    if pg_docs:
        input_text += (
            "## AWS & Community Documentation — Aurora PostgreSQL Upgrade\n"
            f"{pg_docs[:20000]}\n\n"
        )
    input_text += (
        "Based on the documentation above and your knowledge of MySQL 5.7→8.0 and "
        "PostgreSQL 11→15 breaking changes, extract all documented breaking changes "
        "for these major upgrade paths."
    )

    payload = {"input": input_text}

    try:
        endpoint = AGENTCORE_CATALOG_EXTRACT_ENDPOINT
        if not endpoint:
            logger.warning("AGENTCORE_CATALOG_EXTRACT_ENDPOINT not set — using fallback")
            return _fallback_breaking_changes()

        from shared.agentcore_client import invoke_agentcore
        result_text = invoke_agentcore(endpoint, input_text)

        if not result_text:
            logger.warning("AgentCore returned empty response — using fallback")
            return _fallback_breaking_changes()

        # Parse JSON from agent response (strip markdown code blocks if present)
        clean_text = result_text.strip()
        if clean_text.startswith("```"):
            lines = clean_text.split("\n")
            lines = [l for l in lines if not l.strip().startswith("```")]
            clean_text = "\n".join(lines).strip()
        parsed = json.loads(clean_text)

        # Validate we got something useful
        bc = parsed.get("breakingChanges", {})
        if not bc.get("aurora-mysql") and not bc.get("aurora-postgresql"):
            logger.warning("AgentCore returned empty breaking changes — using fallback")
            return _fallback_breaking_changes()

        return parsed

    except (URLError, json.JSONDecodeError, KeyError, Exception) as e:
        logger.warning("AgentCore catalog_extract call failed: %s — using fallback", e)
        return _fallback_breaking_changes()


def _fallback_breaking_changes():
    """Return hardcoded fallback breaking changes when AgentCore is unavailable."""
    return {
        "breakingChanges": {
            "aurora-mysql": {
                "5.7_to_8.0": list(FALLBACK_MYSQL_BREAKING_CHANGES),
            },
            "aurora-postgresql": {
                "11_to_15": list(FALLBACK_PG_BREAKING_CHANGES),
                "12_to_15": list(FALLBACK_PG_BREAKING_CHANGES),
                "13_to_15": list(FALLBACK_PG_BREAKING_CHANGES),
                "14_to_15": list(FALLBACK_PG_BREAKING_CHANGES),
            },
        }
    }


# ---------------------------------------------------------------------------
# Step 5: Merge known + extracted breaking changes with deduplication
# ---------------------------------------------------------------------------
def _get_known_breaking_changes(engine, version):
    """Return hardcoded known breaking changes for a version."""
    if engine == "aurora-mysql" and version.startswith("5.7"):
        return list(FALLBACK_MYSQL_BREAKING_CHANGES)
    if engine == "aurora-postgresql":
        pg_major = version.split(".")[0]
        try:
            major_int = int(pg_major)
        except ValueError:
            return []
        # PG breaking changes apply to versions 11-14
        if major_int <= 14:
            return list(FALLBACK_PG_BREAKING_CHANGES)
    return []


def _merge_breaking_changes(known, enriched_list):
    """Merge known breaking changes with doc-extracted ones, avoiding duplicates."""
    merged = list(known)
    known_descs = {bc["description"].lower()[:50] for bc in known}
    for bc in enriched_list:
        desc = bc.get("description", "").lower()[:50]
        if desc and desc not in known_descs:
            merged.append(bc)
            known_descs.add(desc)
    return merged


# ---------------------------------------------------------------------------
# Step 6: Build final catalog
# ---------------------------------------------------------------------------
def _build_catalog(versions_data, breaking_changes):
    """Build the final deprecation catalog."""
    now = datetime.now(timezone.utc)
    catalog = {
        "generatedAt": now.isoformat(),
        "engines": {},
    }

    bc_data = breaking_changes.get("breakingChanges", {})

    for engine, versions in versions_data.items():
        # Determine LTS version
        lts_version = ""
        if engine == "aurora-mysql":
            lts_version = next(
                (v["version"] for v in versions if v["version"].startswith("8.0")),
                versions[-1]["version"] if versions else "",
            )
        else:
            lts_version = next(
                (v["version"] for v in versions if v["version"].startswith("15.")),
                versions[-1]["version"] if versions else "",
            )

        # Determine deprecation status and end-of-support dates
        enriched_versions = []
        for v in versions:
            eol = _estimate_end_of_support(engine, v["version"])
            status = "available"
            if eol and eol < now.strftime("%Y-%m-%d"):
                status = "deprecated"
            elif v.get("status") == "deprecated":
                status = "deprecated"

            # Get known breaking changes for this version
            known = _get_known_breaking_changes(engine, v["version"])
            # Get AgentCore-extracted breaking changes for this version
            enriched = _get_breaking_changes_for_version(engine, v["version"], bc_data)
            # Merge with deduplication
            version_bc = _merge_breaking_changes(known, enriched)

            enriched_versions.append({
                "version": v["version"],
                "parameterFamily": v["parameterFamily"],
                "status": status,
                "endOfSupport": eol,
                "upgradeTargets": v["upgradeTargets"],
                "breakingChanges": version_bc,
            })

        catalog["engines"][engine] = {
            "ltsVersion": lts_version,
            "versions": enriched_versions,
        }

    return catalog


def _estimate_end_of_support(engine, version):
    """Estimate end-of-support date using KNOWN_EOL_DATES lookup."""
    engine_eols = KNOWN_EOL_DATES.get(engine, {})

    # Try exact match first
    if version in engine_eols:
        return engine_eols[version]

    # For aurora-mysql, check if version starts with 5.7
    if engine == "aurora-mysql" and version.startswith("5.7"):
        return engine_eols.get("5.7")

    # For aurora-postgresql, extract major from full version like "11.21"
    if engine == "aurora-postgresql":
        pg_major = version.split(".")[0]
        if pg_major in engine_eols:
            return engine_eols[pg_major]

    # Try major version prefix as generic fallback
    major = version.split(".")[0]
    if major in engine_eols:
        return engine_eols[major]

    return None


def _get_breaking_changes_for_version(engine, version, bc_data):
    """Get AgentCore-extracted breaking changes relevant to upgrading from this version."""
    engine_bc = bc_data.get(engine, {})

    # Try various key formats
    major = version.split(".")[0]
    relevant = []

    # For aurora-mysql, try "5.7_to_8.0" style keys
    if engine == "aurora-mysql" and version.startswith("5.7"):
        for key in ("5.7_to_8.0", "5.7"):
            if key in engine_bc:
                relevant.extend(engine_bc[key])
                break

    # For aurora-postgresql, try "11_to_15", "12_to_15" etc.
    elif engine == "aurora-postgresql":
        pg_major = version.split(".")[0]
        for target in ("15", "16"):
            key = f"{pg_major}_to_{target}"
            if key in engine_bc:
                relevant.extend(engine_bc[key])
                break
        if not relevant and pg_major in engine_bc:
            relevant.extend(engine_bc[pg_major])

    # Generic fallback: match any key starting with major version
    if not relevant:
        for path_key, changes in engine_bc.items():
            if path_key.startswith(f"{major}_to_") or path_key.startswith(f"{major}."):
                relevant.extend(changes)

    return relevant[:20]  # Cap at 20 per version
