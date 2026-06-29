"""Code Analysis Lambda — Scans source code for DB upgrade incompatibilities.

Triggered by: API handler (async invoke)
Input: {runId, accountId1, clusterArn, accountId2, appci, sourceUrl, targetVersion, catalogVersion}
Output: Writes findings to DynamoDB, enriches via AgentCore, saves report to S3

Pipeline:
1. Load deprecation catalog (breaking changes for the target version)
2. Download source code from S3 or GitHub
3. Build scan patterns from catalog dynamically
4. Pattern-match against breaking change signatures (skipping comments)
5. Categorize findings and flag novel ones
6. Enrich findings via AgentCore code_enrichment agent (LLM)
7. Write findings to DynamoDB + summary report to S3
"""

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from shared.config import REGION, TABLE_PREFIX, S3_BUCKET, AGENTCORE_CODE_ENRICHMENT_ENDPOINT, AGENTCORE_REPORT_ENDPOINT
from shared.state import update_phase, update_run, mark_complete, mark_failed
from shared.s3_utils import put_json, get_json, put_html

s3 = boto3.client("s3", region_name=REGION)
ddb = boto3.resource("dynamodb", region_name=REGION)
findings_table = ddb.Table(f"{TABLE_PREFIX}findings")

# ---------------------------------------------------------------------------
# Scannable file extensions (all 17 from v4)
# ---------------------------------------------------------------------------
SCANNABLE_EXTENSIONS = {
    ".cs", ".java", ".py", ".js", ".ts", ".sql", ".xml", ".json",
    ".properties", ".conf", ".csproj", ".gradle", ".txt", ".toml",
    ".yaml", ".yml", ".cfg", ".ini", ".sh",
}

# ---------------------------------------------------------------------------
# Comment patterns per file extension (exact v4 logic)
# ---------------------------------------------------------------------------
SINGLE_LINE_COMMENT_PATTERNS = {
    ".cs": ["//"],
    ".java": ["//"],
    ".js": ["//"],
    ".ts": ["//"],
    ".py": ["#"],
    ".sh": ["#"],
    ".sql": ["--"],
    ".properties": ["#"],
    ".conf": ["#"],
    ".cfg": ["#"],
    ".ini": ["#", ";"],
    ".yaml": ["#"],
    ".yml": ["#"],
    ".toml": ["#"],
    ".gradle": ["//"],
    ".xml": [],
    ".json": [],
    ".csproj": [],
    ".txt": [],
}

# Multi-line comment delimiters: (start, end)
MULTI_LINE_COMMENT_PATTERNS = {
    ".cs": [("/*", "*/")],
    ".java": [("/*", "*/")],
    ".js": [("/*", "*/")],
    ".ts": [("/*", "*/")],
    ".py": [('"""', '"""'), ("'''", "'''")],
    ".sql": [("/*", "*/")],
    ".xml": [("<!--", "-->")],
    ".csproj": [("<!--", "-->")],
    ".gradle": [("/*", "*/")],
}

# ---------------------------------------------------------------------------
# Valid finding categories
# ---------------------------------------------------------------------------
VALID_CATEGORIES = {
    "sql-syntax", "schema", "query-cache", "auth", "charset",
    "driver", "config", "reserved-keyword", "info-schema", "connection",
}

# ---------------------------------------------------------------------------
# Hardcoded fallback patterns (used when catalog yields no dynamic patterns)
# ---------------------------------------------------------------------------
FALLBACK_CATEGORY_PATTERNS = {
    "query-cache": [
        r"query_cache",
        r"SQL_CACHE",
        r"SQL_NO_CACHE",
        r"RESET\s+QUERY\s+CACHE",
        r"query_cache_type",
        r"query_cache_size",
        r"query_cache_limit",
        r"Qcache",
    ],
    "sql-syntax": [
        r"SQL_CALC_FOUND_ROWS",
        r"FOUND_ROWS\s*\(",
        r"GROUP\s+BY\s+[^;]*\s+(ASC|DESC)",
        r"PROCEDURE\s+ANALYSE",
        r"PASSWORD\s*\(",
        r"ENCRYPT\s*\(",
        r"DES_ENCRYPT",
        r"DES_DECRYPT",
        r"IDENTIFIED\s+BY",
        r"GRANT\s+.*IDENTIFIED",
        r"\\N\b",
    ],
    "auth": [
        r"mysql_native_password",
        r"defaultAuthenticationPlugin",
        r"default.authentication.plugin",
        r"old_passwords",
        r"authentication_string",
    ],
    "charset": [
        r"characterEncoding\s*=\s*latin",
        r"charset\s*=\s*latin",
        r"utf8['\"\s,;)]",
        r"utf8_general_ci",
        r"utf8_unicode_ci",
        r"character.set.*=.*utf8[^m]",
        r"collation.*=.*utf8_",
        r"SET\s+NAMES\s+utf8[^m]",
    ],
    "reserved-keyword": [
        r"\brank\b",
        r"\bgroups\b",
        r"\bsystem\b",
        r"\bdense_rank\b",
        r"\brow_number\b",
        r"\bcube\b",
        r"\brecursive\b",
        r"\bof\b",
        r"\bfunction\b",
    ],
    "config": [
        r"innodb_locks_unsafe_for_binlog",
        r"tx_isolation",
        r"sync_frm",
        r"innodb_file_format",
        r"innodb_large_prefix",
        r"log_warnings",
        r"have_query_cache",
        r"metadata_locks_cache_size",
    ],
    "info-schema": [
        r"INFORMATION_SCHEMA\.INNODB_LOCKS",
        r"INFORMATION_SCHEMA\.INNODB_LOCK_WAITS",
        r"INFORMATION_SCHEMA\.GLOBAL_STATUS",
        r"INFORMATION_SCHEMA\.SESSION_STATUS",
        r"INFORMATION_SCHEMA\.INNODB_SYS_TABLES",
        r"INFORMATION_SCHEMA\.INNODB_SYS_INDEXES",
        r"INFORMATION_SCHEMA\.INNODB_SYS_COLUMNS",
        r"INFORMATION_SCHEMA\.INNODB_SYS_FIELDS",
    ],
    "driver": [
        r"useSSL\s*=\s*false",
        r"com\.mysql\.jdbc\.Driver",
        r"mysql-connector-java.*[45]\.",
        r"allowPublicKeyRetrieval",
    ],
    "connection": [
        r"jdbc:mysql://.*\?.*serverTimezone",
        r"useLegacyDatetimeCode",
    ],
    "schema": [
        r"0000-00-00",
        r"TIMESTAMP.*DEFAULT.*0",
    ],
}


# ---------------------------------------------------------------------------
# Lambda handler
# ---------------------------------------------------------------------------
def handler(event, context):
    """Lambda entry point."""
    run_id = event["runId"]
    account_id_1 = event.get("accountId1", "")
    cluster_arn = event.get("clusterArn", "")
    account_id_2 = event.get("accountId2", "")
    appci = event.get("appci", "")
    source_url = event.get("sourceUrl", "")
    target_version = event.get("targetVersion", "")
    catalog_version = event.get("catalogVersion", "")

    logger.info(
        "Code analysis starting: runId=%s accountId1=%s clusterArn=%s "
        "accountId2=%s appci=%s sourceUrl=%s targetVersion=%s catalogVersion=%s",
        run_id, account_id_1, cluster_arn, account_id_2, appci,
        source_url, target_version, catalog_version,
    )

    try:
        # Step 1: Load deprecation catalog
        update_phase(run_id, "loading_catalog", progress_pct=5)
        catalog = _load_catalog(catalog_version)

        # Determine engine from cluster ARN
        engine = _determine_engine(cluster_arn)

        # Auto-detect target version if not provided or mismatched with engine
        target_version = _resolve_target_version(target_version, engine, cluster_arn, catalog)

        update_phase(run_id, "retrieving_source", progress_pct=15)

        # Step 2: Retrieve source code (S3 or GitHub)
        source_files = retrieve_source_code(source_url)
        if not source_files:
            mark_failed(run_id, (
                f"No scannable source files found at {source_url}. "
                f"Supported extensions: {', '.join(sorted(SCANNABLE_EXTENSIONS))}. "
                "Ensure the repository contains code files (not just notebooks, images, or data)."
            ))
            return
        logger.info("Retrieved %d source files", len(source_files))

        update_phase(run_id, "building_patterns", progress_pct=30)

        # Step 3: Build scan patterns from catalog
        patterns = build_scan_patterns(catalog, engine, target_version)
        logger.info("Built %d scan patterns", len(patterns))

        update_phase(run_id, "scanning_source", progress_pct=40)

        # Step 4: Scan source code (no finding cap)
        raw_findings = scan_source_code(source_files, patterns)
        logger.info("Found %d raw findings in %d files", len(raw_findings), len(source_files))

        update_phase(run_id, "categorizing_findings", progress_pct=55)

        # Step 5: Categorize findings and flag novel ones
        categorized_findings = _categorize_findings(raw_findings, catalog, engine)
        novel_count = sum(1 for f in categorized_findings if f.get("isNovel"))
        logger.info("Categorized %d findings (%d novel)", len(categorized_findings), novel_count)

        update_phase(run_id, "enriching_findings", progress_pct=60)

        # Step 6: Enrich findings via AgentCore (batch size 20)
        enriched_findings = _enrich_findings(categorized_findings, target_version, engine)

        update_phase(run_id, "saving_results", progress_pct=85)

        # Step 7: Write findings to DynamoDB
        for finding in enriched_findings:
            finding["runId"] = run_id
            try:
                findings_table.put_item(Item=finding)
            except Exception as e:
                logger.warning("Failed to write finding %s: %s", finding.get("findingId"), e)

        # Step 8: Save report to S3 at structured path
        cluster_id = cluster_arn.split(":")[-1] if cluster_arn else "unknown"
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        s3_key = (
            f"findings/{account_id_1}/{cluster_id}/{account_id_2}/"
            f"{appci}/{target_version}/{timestamp}.json"
        )

        errors = sum(1 for f in enriched_findings if f.get("severity") == "error")
        warnings = sum(1 for f in enriched_findings if f.get("severity") == "warning")
        by_category = {}
        for f in enriched_findings:
            cat = f.get("category", "other")
            by_category[cat] = by_category.get(cat, 0) + 1

        report = {
            "runId": run_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "inputs": {
                "accountId1": account_id_1,
                "clusterArn": cluster_arn,
                "accountId2": account_id_2,
                "appci": appci,
                "sourceUrl": source_url,
                "targetVersion": target_version,
            },
            "summary": {
                "totalFindings": len(enriched_findings),
                "errors": errors,
                "warnings": warnings,
                "novelFindings": novel_count,
                "byCategory": by_category,
                "filesScanned": len(source_files),
            },
            "findings": enriched_findings,
        }
        put_json(s3_key, report)

        # Step 9: Generate HTML report via AgentCore + deterministic template
        update_phase(run_id, "generating_report", progress_pct=90)
        html_key = s3_key.replace(".json", ".html")
        try:
            narrative = _get_report_narrative(report)
            html_content = _generate_html_report(report, narrative)
            put_html(html_key, html_content)
            logger.info("HTML report uploaded: %s", html_key)
            # Use HTML key as the report key so frontend opens HTML
            s3_key = html_key
        except Exception as e:
            logger.warning("HTML report generation failed (non-fatal): %s", e)

        # Mark complete
        summary = {
            "totalFindings": len(enriched_findings),
            "errors": errors,
            "warnings": warnings,
            "novelFindings": novel_count,
            "filesScanned": len(source_files),
        }
        mark_complete(run_id, summary, s3_report_key=s3_key)
        logger.info(
            "Code analysis complete: %d findings (%d errors, %d warnings, %d novel)",
            len(enriched_findings), errors, warnings, novel_count,
        )

    except Exception as e:
        logger.exception("Code analysis failed: %s", e)
        mark_failed(run_id, str(e))


# ---------------------------------------------------------------------------
# Catalog loading
# ---------------------------------------------------------------------------
def _load_catalog(catalog_version):
    """Load deprecation_catalog.json from S3. Uses catalog_version if set."""
    if catalog_version:
        key = f"deprecation_catalog_{catalog_version}.json"
    else:
        key = "deprecation_catalog.json"

    logger.info("Loading deprecation catalog: s3://%s/%s", S3_BUCKET, key)
    try:
        catalog = get_json(key)
        logger.info("Loaded catalog generated at %s", catalog.get("generatedAt", "unknown"))
        return catalog
    except Exception as e:
        logger.warning("Could not load catalog (%s): %s — continuing with fallback patterns", key, e)
        return {}


def _determine_engine(cluster_arn):
    """Determine the engine type from the cluster ARN."""
    engine = "aurora-mysql"  # default
    if cluster_arn:
        try:
            rds = boto3.client("rds", region_name=REGION)
            cluster_id = cluster_arn.split(":")[-1]
            resp = rds.describe_db_clusters(DBClusterIdentifier=cluster_id)
            engine = resp["DBClusters"][0].get("Engine", "aurora-mysql")
        except Exception as e:
            logger.warning("Could not determine engine from cluster ARN: %s", e)
    return engine


def _resolve_target_version(user_target, engine, cluster_arn, catalog):
    """Resolve the target version — auto-detect if not provided or mismatched.

    Logic:
      1. If user provided a target version that matches the engine, use it
      2. Otherwise, auto-detect the next major version from the cluster's current version
      3. Fall back to the latest available version from the catalog
    """
    # Check if user-provided version matches the engine
    if user_target:
        if "mysql" in engine and "mysql" in user_target:
            return user_target
        if "postgresql" in engine and not ("mysql" in user_target):
            # PostgreSQL versions are like "16.4", "15.7" — no "mysql" in them
            return user_target

    # Auto-detect: get current version from cluster
    current_version = ""
    if cluster_arn:
        try:
            rds = boto3.client("rds", region_name=REGION)
            cluster_id = cluster_arn.split(":")[-1]
            resp = rds.describe_db_clusters(DBClusterIdentifier=cluster_id)
            current_version = resp["DBClusters"][0].get("EngineVersion", "")
        except Exception as e:
            logger.warning("Could not get current version for auto-detect: %s", e)

    # Try to find the next major version from the catalog
    if catalog and current_version:
        engines = catalog.get("engines", {})
        engine_data = engines.get(engine, {})
        available_versions = [
            v["version"] for v in engine_data.get("versions", [])
            if v.get("status") == "available"
        ]

        if "mysql" in engine:
            # MySQL: current might be "5.7.mysql_aurora.2.12.0", target would be "8.0.mysql_aurora.3.x"
            current_major = current_version.split(".")[0]  # "5" or "8"
            # Find versions with a higher major
            higher = [v for v in available_versions if v.split(".")[0] > current_major]
            if higher:
                higher.sort(reverse=True)
                logger.info("Auto-detected target version: %s (from current %s)", higher[0], current_version)
                return higher[0]
        else:
            # PostgreSQL: current might be "14.9", target would be "15.x" or "16.x"
            try:
                current_major = int(current_version.split(".")[0])
                higher = [v for v in available_versions if int(v.split(".")[0]) > current_major]
                if higher:
                    higher.sort(key=lambda v: int(v.split(".")[0]), reverse=True)
                    logger.info("Auto-detected target version: %s (from current %s)", higher[0], current_version)
                    return higher[0]
            except (ValueError, IndexError):
                pass

    # Final fallback: use the latest available version from catalog
    if catalog:
        engines = catalog.get("engines", {})
        engine_data = engines.get(engine, {})
        available = [v["version"] for v in engine_data.get("versions", []) if v.get("status") == "available"]
        if available:
            available.sort(reverse=True)
            return available[0]

    # Absolute fallback
    if "mysql" in engine:
        return "8.0.mysql_aurora.3.08.0"
    return "16.4"


# ---------------------------------------------------------------------------
# Source code retrieval (S3 + GitHub support)
# ---------------------------------------------------------------------------
def retrieve_source_code(source_url):
    """
    Retrieve source code from S3 or GitHub.

    Args:
        source_url: s3://bucket/prefix/ or https://github.com/owner/repo

    Returns:
        dict mapping relative file paths to file contents
    """
    if not source_url:
        return {}

    if source_url.startswith("s3://"):
        return _retrieve_from_s3(source_url)
    elif "github.com" in source_url:
        return _retrieve_from_github(source_url)
    else:
        raise ValueError(f"Unsupported source URL scheme: {source_url}")


def _retrieve_from_s3(s3_url):
    """Download scannable files from an S3 prefix with pagination."""
    # Parse s3://bucket/prefix/
    parts = s3_url.replace("s3://", "").split("/", 1)
    bucket = parts[0]
    prefix = parts[1] if len(parts) > 1 else ""

    files = {}
    paginator = s3.get_paginator("list_objects_v2")

    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            ext = os.path.splitext(key)[1].lower()
            if ext not in SCANNABLE_EXTENSIONS:
                continue
            if obj.get("Size", 0) > 5 * 1024 * 1024:  # Skip files > 5MB
                continue

            try:
                resp = s3.get_object(Bucket=bucket, Key=key)
                content = resp["Body"].read().decode("utf-8", errors="replace")
                # Use relative path from prefix
                rel_path = key[len(prefix):].lstrip("/")
                files[rel_path] = content
            except Exception as e:
                logger.warning("Failed to download s3://%s/%s: %s", bucket, key, e)

    logger.info("Retrieved %d files from S3 (%s)", len(files), s3_url)
    return files


def _get_github_token():
    """Retrieve the GitHub access token from Secrets Manager.

    Returns the accessToken value, or None if the secret does not exist
    or does not contain an accessToken field. Never logs the token value.
    """
    try:
        sm = boto3.client("secretsmanager", region_name=REGION)
        resp = sm.get_secret_value(SecretId="aurora-upgrade-planner/github-token")
        secret = json.loads(resp["SecretString"])
        return secret.get("accessToken") or None
    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceNotFoundException":
            return None
        logger.warning("Failed to retrieve GitHub token from Secrets Manager: %s", e)
        return None
    except Exception as e:
        logger.warning("Unexpected error retrieving GitHub token: %s", e)
        return None


def _retrieve_from_github(github_url):
    """Download a GitHub repo via the tarball API (no git binary needed).

    Uses the GitHub API to download a compressed tarball of the default branch,
    then extracts scannable files. Authenticates with the stored OAuth token
    for private repos.
    """
    token = _get_github_token()

    # Parse owner/repo from URL
    # Supports: https://github.com/owner/repo, https://github.com/owner/repo.git,
    #           https://github.com/owner/repo/tree/branch
    url_clean = github_url.rstrip("/")
    if url_clean.endswith(".git"):
        url_clean = url_clean[:-4]
    parts = url_clean.replace("https://github.com/", "").split("/")
    if len(parts) < 2:
        raise ValueError(f"Invalid GitHub URL: {github_url}")
    owner = parts[0]
    repo = parts[1]
    # Optional branch/ref (from /tree/branch URLs)
    ref = parts[3] if len(parts) >= 4 and parts[2] == "tree" else ""

    # Build tarball API URL
    tarball_url = f"https://api.github.com/repos/{owner}/{repo}/tarball"
    if ref:
        tarball_url += f"/{ref}"

    logger.info("Downloading tarball from GitHub: %s/%s%s", owner, repo, f" (ref={ref})" if ref else "")

    # Build request with auth header
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "aurora-upgrade-planner",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    import tarfile
    import io

    req = Request(tarball_url, headers=headers, method="GET")

    try:
        with urlopen(req, timeout=120) as resp:
            tarball_bytes = resp.read()
    except HTTPError as e:
        if e.code == 401 or e.code == 403:
            logger.error("GitHub authentication failed (HTTP %d) for %s/%s", e.code, owner, repo)
            # Update DynamoDB metadata to expired status
            try:
                settings_table = ddb.Table(f"{TABLE_PREFIX}settings")
                settings_table.put_item(Item={
                    "settingKey": "github",
                    "connectionStatus": "expired",
                    "updatedAt": datetime.now(timezone.utc).isoformat(),
                })
            except Exception as ddb_err:
                logger.warning("Failed to update GitHub status to expired: %s", ddb_err)
            raise RuntimeError("GitHub authentication failed. Please reconnect in Settings.")
        elif e.code == 404:
            raise RuntimeError(
                f"Repository not found: {owner}/{repo}. "
                "Check the URL and ensure the repo exists. "
                "For private repos, connect GitHub in Settings first."
            )
        else:
            raise RuntimeError(f"GitHub API error (HTTP {e.code}): {e.read().decode('utf-8', errors='replace')[:200]}")
    except (URLError, OSError) as e:
        raise RuntimeError(f"Failed to reach GitHub API: {e}")

    # Extract tarball
    logger.info("Downloaded tarball (%d bytes), extracting...", len(tarball_bytes))
    files = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(tarball_bytes), mode="r:gz") as tar:
            all_members = tar.getmembers()
            file_members = [m for m in all_members if m.isfile()]
            logger.info("Tarball contains %d entries (%d files)", len(all_members), len(file_members))
            if file_members:
                # Log first few file paths for debugging
                sample_paths = [m.name for m in file_members[:5]]
                logger.info("Sample file paths: %s", sample_paths)
                # Log extensions found
                exts_found = set(os.path.splitext(m.name)[1].lower() for m in file_members)
                logger.info("Extensions in tarball: %s", exts_found)

            for member in file_members:
                # Tarball has a top-level directory like "owner-repo-sha/"
                # Strip it to get the relative path
                path_parts = member.name.split("/", 1)
                if len(path_parts) < 2:
                    continue
                rel_path = path_parts[1]

                # Check extension
                ext = os.path.splitext(rel_path)[1].lower()
                if ext not in SCANNABLE_EXTENSIONS:
                    continue

                # Skip files > 5MB
                if member.size > 5 * 1024 * 1024:
                    continue

                try:
                    f = tar.extractfile(member)
                    if f:
                        content = f.read().decode("utf-8", errors="replace")
                        files[rel_path] = content
                except Exception as e:
                    logger.warning("Failed to extract %s: %s", rel_path, e)
    except tarfile.TarError as e:
        raise RuntimeError(f"Failed to extract tarball: {e}")

    logger.info("Retrieved %d files from GitHub (%s/%s)", len(files), owner, repo)
    return files


# ---------------------------------------------------------------------------
# Comment detection (exact v4 logic)
# ---------------------------------------------------------------------------
def is_comment_line(line, ext):
    """
    Check if a line is a comment line based on file extension.

    Supports: //, #, --, /* */, <!-- --> patterns.
    """
    stripped = line.strip()
    if not stripped:
        return True  # Blank lines are skipped

    # Single-line comment patterns
    patterns = SINGLE_LINE_COMMENT_PATTERNS.get(ext, [])
    for pattern in patterns:
        if stripped.startswith(pattern):
            return True

    # Multi-line comment start (standalone line)
    ml_patterns = MULTI_LINE_COMMENT_PATTERNS.get(ext, [])
    for start, end in ml_patterns:
        if stripped.startswith(start):
            return True
        # Also check for lines that are entirely within a multi-line comment
        # e.g., " * continuation line" in Java/C#
        if ext in (".cs", ".java", ".js", ".ts", ".gradle") and stripped.startswith("*"):
            return True

    return False


def _is_in_multiline_comment(lines, line_idx, ext):
    """
    Check if a specific line is inside a multi-line comment block.
    Scans from the beginning of the file to track comment state.
    """
    ml_patterns = MULTI_LINE_COMMENT_PATTERNS.get(ext, [])
    if not ml_patterns:
        return False

    in_comment = False
    current_end = None

    for i, line in enumerate(lines):
        if i > line_idx:
            break

        if in_comment:
            if current_end and current_end in line:
                in_comment = False
                current_end = None
        else:
            for start, end in ml_patterns:
                if start in line:
                    # Check if it closes on the same line
                    after_start = line[line.index(start) + len(start):]
                    if end in after_start:
                        continue  # Single-line block comment
                    in_comment = True
                    current_end = end
                    break

    return in_comment


# ---------------------------------------------------------------------------
# Pattern building from deprecation catalog (v4 logic)
# ---------------------------------------------------------------------------
def build_scan_patterns(catalog, engine, target_version):
    """
    Build regex patterns from the deprecation catalog's breaking changes.

    Returns:
        list of dicts: {pattern, pattern_str, category, description, remediation, severity}
    """
    patterns = []
    engines = catalog.get("engines", {})
    engine_data = engines.get(engine, {})

    for version_entry in engine_data.get("versions", []):
        for bc in version_entry.get("breakingChanges", []):
            desc = bc.get("description", "")
            category = bc.get("category", "config")
            severity = bc.get("severity", "warning")
            remediation = bc.get("remediation", "")

            # Extract keywords from description to build regex patterns
            regex_patterns = _description_to_patterns(desc, category, engine)
            for pat in regex_patterns:
                try:
                    compiled = re.compile(pat, re.IGNORECASE)
                    patterns.append({
                        "pattern": compiled,
                        "pattern_str": pat,
                        "category": category,
                        "description": desc,
                        "remediation": remediation,
                        "severity": severity,
                    })
                except re.error:
                    logger.warning("Invalid regex pattern: %s", pat)

    logger.info("Built %d dynamic scan patterns for engine=%s", len(patterns), engine)

    # If catalog yielded no patterns, use hardcoded fallback set
    if not patterns:
        logger.warning("No dynamic patterns from catalog — using fallback pattern set")
        patterns = _build_fallback_patterns()

    return patterns


def _description_to_patterns(description, category, engine):
    """
    Convert a breaking change description into regex patterns for scanning.
    Full MySQL and PostgreSQL pattern extraction logic from v4.

    Returns a list of regex pattern strings.
    """
    patterns = []
    desc_lower = description.lower()

    # MySQL-specific patterns
    if engine == "aurora-mysql":
        if "sql_calc_found_rows" in desc_lower:
            patterns.append(r"\bSQL_CALC_FOUND_ROWS\b")
        if "found_rows" in desc_lower and "sql_calc" not in desc_lower:
            patterns.append(r"\bFOUND_ROWS\s*\(")
        if "group by" in desc_lower and "sort" in desc_lower:
            patterns.append(r"\bGROUP\s+BY\b(?!.*\bORDER\s+BY\b)")
        if "password()" in desc_lower:
            patterns.append(r"\bPASSWORD\s*\(")
        if "grant" in desc_lower and "identified by" in desc_lower:
            patterns.append(r"\bGRANT\b.*\bIDENTIFIED\s+BY\b")
        if "query_cache" in desc_lower or "query cache" in desc_lower:
            patterns.extend([
                r"\bquery_cache_size\b",
                r"\bquery_cache_type\b",
                r"\bSQL_CACHE\b",
                r"\bSQL_NO_CACHE\b",
                r"\bquery_cache\b",
            ])
        if "mysql_native_password" in desc_lower:
            patterns.append(r"\bmysql_native_password\b")
        if "latin1" in desc_lower or "utf8mb4" in desc_lower:
            patterns.extend([
                r"\blatin1\b",
                r"charset\s*=\s*latin1",
            ])
        if "innodb_sys" in desc_lower:
            patterns.append(r"\bINNODB_SYS_\w+\b")
        if "reserved" in desc_lower and "keyword" in desc_lower:
            patterns.extend([
                r"(?<![`\"\w])\bRANK\b(?![`\"\w])\s*(?![\(])",
                r"(?<![`\"\w])\bGROUPS\b(?![`\"\w])",
                r"(?<![`\"\w])\bSYSTEM\b(?![`\"\w])",
                r"(?<![`\"\w])\bROW_NUMBER\b(?![`\"\w])\s*(?![\(])",
            ])
        if "driver" in desc_lower or "connector" in desc_lower:
            patterns.extend([
                r"mysql[.-]connector[.-]java[/:]([0-4]\.|5\.[0-9]\b)",
                r"MySql\.Data\.MySqlClient.*Version\s*=\s*[0-7]\.",
            ])

    # PostgreSQL-specific patterns
    if engine == "aurora-postgresql":
        if "default_with_oids" in desc_lower:
            patterns.append(r"\bdefault_with_oids\b")
        if "wal_level" in desc_lower and "archive" in desc_lower:
            patterns.append(r"\bwal_level\s*=\s*archive\b")
        if "vacuum_defer_cleanup_age" in desc_lower:
            patterns.append(r"\bvacuum_defer_cleanup_age\b")
        if "md5" in desc_lower and ("auth" in desc_lower or "scram" in desc_lower):
            patterns.append(r"\bmd5\b.*(?:auth|password|pg_hba)")

    # Generic fallback: if no specific patterns, try to extract quoted terms
    if not patterns:
        # Look for terms in backticks, quotes, or ALL_CAPS identifiers
        quoted = re.findall(r'[`\'"](\w+)[`\'"]', description)
        caps = re.findall(r'\b([A-Z_]{3,})\b', description)
        for term in quoted + caps:
            if len(term) >= 3:
                patterns.append(r"\b" + re.escape(term) + r"\b")

    return patterns


def _build_fallback_patterns():
    """Build patterns from the hardcoded FALLBACK_CATEGORY_PATTERNS set."""
    patterns = []
    for category, pat_strings in FALLBACK_CATEGORY_PATTERNS.items():
        for pat_str in pat_strings:
            try:
                compiled = re.compile(pat_str, re.IGNORECASE)
                patterns.append({
                    "pattern": compiled,
                    "pattern_str": pat_str,
                    "category": category,
                    "description": f"Potential {category} compatibility issue",
                    "remediation": "",
                    "severity": "warning",
                })
            except re.error:
                pass
    logger.info("Built %d fallback patterns", len(patterns))
    return patterns


# ---------------------------------------------------------------------------
# Source code scanning (v4 logic — skips comments, no finding cap)
# ---------------------------------------------------------------------------
def scan_source_code(files, patterns):
    """
    Scan source files against breaking change patterns.
    Skips comment lines and lines inside multi-line comments.
    Deduplicates: only one finding per (file, line, category).

    Returns:
        list of raw findings (before enrichment)
    """
    raw_findings = []
    seen = set()  # (file_path, line_num, category) — one finding per line per category

    for file_path, content in files.items():
        ext = os.path.splitext(file_path)[1].lower()
        lines = content.split("\n")

        for line_idx, line in enumerate(lines):
            line_num = line_idx + 1

            # Skip comment lines
            if is_comment_line(line, ext):
                continue

            # Skip lines inside multi-line comments
            if _is_in_multiline_comment(lines, line_idx, ext):
                continue

            # Check each pattern — only one finding per (file, line, category)
            for pat_info in patterns:
                if pat_info["pattern"].search(line):
                    key = (file_path, line_num, pat_info["category"])
                    if key in seen:
                        continue
                    seen.add(key)
                    raw_findings.append({
                        "fileName": file_path,
                        "lineNumber": line_num,
                        "codeSnippet": line.strip()[:200],
                        "category": pat_info["category"],
                        "severity": pat_info["severity"],
                        "description": pat_info["description"],
                        "remediation": pat_info["remediation"],
                        "patternMatched": pat_info["pattern_str"],
                    })

    logger.info("Source code scan: %d raw findings in %d files", len(raw_findings), len(files))
    return raw_findings


# ---------------------------------------------------------------------------
# Novel finding detection and categorization (v4 logic)
# ---------------------------------------------------------------------------
def _categorize_findings(findings, catalog, engine):
    """
    Categorize findings and flag novel ones not in the catalog.
    """
    # Build a set of known patterns from the catalog
    known_descriptions = set()
    engines = catalog.get("engines", {})
    engine_data = engines.get(engine, {})
    for version_entry in engine_data.get("versions", []):
        for bc in version_entry.get("breakingChanges", []):
            known_descriptions.add(bc.get("description", "").lower()[:60])

    categorized = []
    for f in findings:
        finding_id = str(uuid.uuid4())

        # Validate category
        category = f.get("category", "config")
        if category not in VALID_CATEGORIES:
            category = "config"

        # Check if novel (not in catalog)
        desc = f.get("description", "").lower()[:60]
        is_novel = desc not in known_descriptions if desc else True

        # Build catalog reference
        catalog_ref = ""
        if not is_novel:
            catalog_ref = f"{engine}/{f.get('description', '')[:50]}"

        categorized.append({
            "findingId": finding_id,
            "fileName": f.get("fileName", ""),
            "lineNumber": f.get("lineNumber", 0),
            "codeSnippet": f.get("codeSnippet", ""),
            "category": category,
            "severity": f.get("severity", "warning"),
            "recommendedChange": f.get("remediation", ""),
            "isNovel": is_novel,
            "catalogReference": catalog_ref,
            "patternMatched": f.get("patternMatched", ""),
        })

    return categorized


# ---------------------------------------------------------------------------
# AgentCore enrichment (HTTP POST, batch size 20)
# ---------------------------------------------------------------------------
ENRICHMENT_SYSTEM_CONTEXT = """\
You are an expert database migration analyst. You are given a list of code findings
that may be affected by an Aurora database engine upgrade.

CRITICAL SAFETY CONSTRAINTS:
- You are a READ-ONLY analysis tool. You MUST NOT suggest or generate commands that modify, upgrade, delete, or alter any production database, cluster, instance, or application code directly.
- You MUST NOT output executable scripts, CLI commands, or API calls that would change any AWS resource or deploy any code.
- Your role is strictly to ANALYZE code findings and RECOMMEND changes. All remediation suggestions must be framed as recommendations for the developer to review and implement manually.
- Never include AWS credentials, connection strings, or secrets in your output.

For each finding, provide:
1. A clear, contextual explanation of WHY this code is affected
2. A specific, actionable remediation recommendation with code examples where possible
3. Confirm or adjust the severity (error = must fix before upgrade, warning = should fix)

Return ONLY valid JSON as an array of enriched findings:
[
  {
    "findingId": "...",
    "recommendedChange": "specific remediation with code example",
    "severity": "error|warning",
    "explanation": "contextual explanation"
  }
]
"""


def _enrich_findings(findings, target_version, engine):
    """Enrich findings with contextual explanations via AgentCore endpoint.

    Skipped if AGENTCORE_CODE_ENRICHMENT_ENDPOINT is not set or if findings
    already have remediation from the pattern match (which they do).
    """
    if not findings:
        return findings

    endpoint = AGENTCORE_CODE_ENRICHMENT_ENDPOINT
    if not endpoint:
        logger.info("Enrichment skipped — AGENTCORE_CODE_ENRICHMENT_ENDPOINT not set")
        return findings

    # All errors get enriched first, then warnings if room remains
    MAX_ENRICH = 100
    batch_size = 50
    errors = [f for f in findings if f.get("severity") == "error"]
    warnings = [f for f in findings if f.get("severity") != "error"]
    to_enrich = errors[:MAX_ENRICH]
    remaining_slots = MAX_ENRICH - len(to_enrich)
    if remaining_slots > 0:
        to_enrich.extend(warnings[:remaining_slots])
    unenriched_remainder = [f for f in findings if f not in to_enrich]

    if unenriched_remainder:
        logger.info("Enriching first %d of %d findings (remaining %d returned as-is)",
                    len(to_enrich), len(findings), len(unenriched_remainder))

    enriched = []

    # Run enrichment batches in parallel (up to 4 concurrent)
    import concurrent.futures
    batches = [to_enrich[i:i + batch_size] for i in range(0, len(to_enrich), batch_size)]

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        futures = {
            executor.submit(_enrich_batch, batch, target_version, engine, endpoint): idx
            for idx, batch in enumerate(batches)
        }
        results = [None] * len(batches)
        for future in concurrent.futures.as_completed(futures):
            idx = futures[future]
            try:
                results[idx] = future.result()
            except Exception as e:
                logger.warning("Enrichment batch %d failed: %s", idx, e)
                results[idx] = batches[idx]  # fallback to unenriched

    for batch_result in results:
        if batch_result:
            enriched.extend(batch_result)

    # Append unenriched remainder
    enriched.extend(unenriched_remainder)
    return enriched


def _enrich_batch(findings_batch, target_version, engine, endpoint):
    """Enrich a batch of findings via AgentCore code_enrichment agent (HTTP POST)."""
    # Build the findings context for the LLM
    findings_for_llm = []
    for f in findings_batch:
        findings_for_llm.append({
            "findingId": f.get("findingId", ""),
            "fileName": f.get("fileName", ""),
            "lineNumber": f.get("lineNumber", 0),
            "codeSnippet": f.get("codeSnippet", ""),
            "category": f.get("category", ""),
            "severity": f.get("severity", ""),
            "catalogReference": f.get("catalogReference", ""),
            "isNovel": f.get("isNovel", False),
        })

    user_message = (
        f"Engine: {engine}\n"
        f"Target version: {target_version}\n\n"
        f"Findings to enrich:\n{json.dumps(findings_for_llm, indent=2)}"
    )

    try:
        from shared.agentcore_client import invoke_agentcore
        result_text = invoke_agentcore(endpoint, user_message)

        if not result_text:
            return findings_batch

        enrichments = _parse_enrichment_json(result_text)

        # Merge enrichments back into findings
        enrichment_map = {e["findingId"]: e for e in enrichments if "findingId" in e}
        for f in findings_batch:
            fid = f.get("findingId", "")
            if fid in enrichment_map:
                e = enrichment_map[fid]
                f["recommendedChange"] = e.get("recommendedChange", f.get("recommendedChange", ""))
                if e.get("severity"):
                    f["severity"] = e["severity"]
                if e.get("explanation"):
                    f["catalogReference"] = e["explanation"]

        return findings_batch
    except (URLError, json.JSONDecodeError) as e:
        logger.warning("AgentCore enrichment call failed: %s", e)
        return findings_batch


def _parse_enrichment_json(response_text):
    """Extract JSON from enrichment response, handling markdown code blocks and SSE artifacts."""
    if not response_text:
        return []
    text = response_text.strip()
    # Strip markdown code blocks
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines).strip()
    # Try direct parse
    try:
        result = json.loads(text)
        return result if isinstance(result, list) else []
    except (json.JSONDecodeError, TypeError):
        pass
    # Try to find JSON array in the text
    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end != -1 and end > start:
        try:
            result = json.loads(text[start:end + 1])
            return result if isinstance(result, list) else []
        except (json.JSONDecodeError, TypeError):
            pass
    logger.warning("Failed to parse enrichment JSON response (len=%d)", len(text))
    return []


# ---------------------------------------------------------------------------
# HTML Report Generation via AgentCore
# ---------------------------------------------------------------------------

def _get_report_narrative(report):
    """Call AgentCore report agent to generate executive summary + action plan for code analysis."""
    endpoint = AGENTCORE_REPORT_ENDPOINT
    if not endpoint:
        logger.error("AGENTCORE_REPORT_ENDPOINT not configured — HTML report will lack narrative")
        return None

    from shared.agentcore_client import invoke_agentcore

    # Build a focused summary for the LLM — prioritize errors, include category breakdown
    summary = report.get("summary", {})
    findings = report.get("findings", [])

    # Group top findings by category for better LLM context
    errors_by_cat = {}
    for f in findings:
        if f.get("severity") == "error":
            cat = f.get("category", "other")
            if cat not in errors_by_cat:
                errors_by_cat[cat] = []
            if len(errors_by_cat[cat]) < 5:
                errors_by_cat[cat].append({
                    "fileName": f.get("fileName", ""),
                    "lineNumber": f.get("lineNumber", 0),
                    "codeSnippet": f.get("codeSnippet", "")[:120],
                    "recommendedChange": f.get("recommendedChange", "")[:200],
                })

    input_data = json.dumps({
        "reportType": "code-analysis",
        "instruction": (
            "This is a CODE ANALYSIS report, not an upgrade test. "
            "Analyze the findings and produce actionable developer changes. "
            "Each category with errors represents code that MUST be changed before upgrading. "
            "List specific changes developers need to make, grouped by priority. "
            "Do NOT return empty developerChanges — use the findings data to generate them."
        ),
        "targetVersion": report.get("inputs", {}).get("targetVersion", ""),
        "sourceUrl": report.get("inputs", {}).get("sourceUrl", ""),
        "summary": summary,
        "errorsByCategory": errors_by_cat,
    }, indent=2, default=str)

    try:
        result_text = invoke_agentcore(endpoint, input_data)
        if not result_text:
            logger.warning("AgentCore report agent returned empty response for code analysis")
            return None
        return _parse_narrative_json(result_text)
    except Exception as e:
        logger.warning("AgentCore report narrative call failed: %s", e)
        return None


def _parse_narrative_json(text):
    """Parse the AgentCore report agent JSON response."""
    if not text:
        return None
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}") + 1
        if start >= 0 and end > start:
            try:
                return json.loads(cleaned[start:end])
            except json.JSONDecodeError:
                pass
    return None


def _esc(s):
    """HTML-escape a string."""
    if not s:
        return ""
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _generate_html_report(report, narrative):
    """Generate a polished HTML code analysis report."""
    inputs = report.get("inputs", {})
    summary = report.get("summary", {})
    findings = report.get("findings", [])
    ts = report.get("timestamp", "")

    cluster_arn = inputs.get("clusterArn", "")
    cluster_id = cluster_arn.split(":")[-1] if cluster_arn else "unknown"
    target_version = inputs.get("targetVersion", "")
    source_url = inputs.get("sourceUrl", "")
    appci = inputs.get("appci", "")

    # Determine engine and current version from cluster
    engine = "aurora-mysql"
    current_version = ""
    if cluster_arn:
        try:
            rds_client = boto3.client("rds", region_name=REGION)
            cid = cluster_arn.split(":")[-1]
            desc = rds_client.describe_db_clusters(DBClusterIdentifier=cid)
            engine = desc["DBClusters"][0].get("Engine", "aurora-mysql")
            current_version = desc["DBClusters"][0].get("EngineVersion", "")
        except Exception:
            pass

    total = summary.get("totalFindings", 0)
    errors = summary.get("errors", 0)
    warnings = summary.get("warnings", 0)
    novel = summary.get("novelFindings", 0)
    files_scanned = summary.get("filesScanned", 0)
    by_category = summary.get("byCategory", {})

    # Narrative sections from AgentCore
    exec_summary = ""
    dev_changes = []
    dba_changes = []
    nice_to_have = []
    if narrative:
        exec_summary = narrative.get("executiveSummary", "")
        dev_changes = narrative.get("developerChanges", [])
        dba_changes = narrative.get("dbaChanges", [])
        nice_to_have = narrative.get("niceToHave", [])

    # If AgentCore didn't return developer changes, generate them from findings
    if not dev_changes and findings:
        dev_changes = _generate_dev_changes_from_findings(findings)

    # Developer changes HTML
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
        dev_html = '<p style="color:#6b7280;">No developer changes required.</p>'

    # DBA changes HTML
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

    # Nice to have
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

    # Category breakdown
    cat_html = ""
    for cat, count in sorted(by_category.items(), key=lambda x: -x[1]):
        cat_errors = sum(1 for f in findings if f.get("category") == cat and f.get("severity") == "error")
        cat_warnings = count - cat_errors
        cat_html += (
            f'<tr><td>{_esc(cat)}</td><td>{count}</td>'
            f'<td style="color:#dc2626;">{cat_errors}</td>'
            f'<td style="color:#d97706;">{cat_warnings}</td></tr>'
        )

    # Findings table (top 100)
    findings_rows = ""
    sorted_findings = sorted(findings, key=lambda f: (0 if f.get("severity") == "error" else 1, f.get("fileName", "")))
    for f in sorted_findings[:100]:
        sev = f.get("severity", "warning")
        sev_color = "#dc2626" if sev == "error" else "#d97706"
        findings_rows += (
            f'<tr>'
            f'<td style="color:{sev_color};font-weight:600;">{sev.upper()}</td>'
            f'<td>{_esc(f.get("category",""))}</td>'
            f'<td><code>{_esc(f.get("fileName",""))}</code>:{f.get("lineNumber",0)}</td>'
            f'<td><code>{_esc(f.get("codeSnippet","")[:100])}</code></td>'
            f'<td>{_esc(f.get("recommendedChange","")[:150])}</td>'
            f'</tr>'
        )

    # Summary banner color based on severity
    banner_color = "#dc2626" if errors > 0 else "#d97706" if warnings > 0 else "#16a34a"
    banner_text = (
        f"{errors} errors and {warnings} warnings found across {files_scanned} files"
        if errors > 0
        else f"{warnings} warnings found across {files_scanned} files"
        if warnings > 0
        else f"No issues found across {files_scanned} files"
    )

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Code Analysis Report — {_esc(cluster_id)}</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f8fafc;color:#1e293b;line-height:1.6;padding:24px}}
.container{{max-width:1100px;margin:0 auto}}
.header{{background:linear-gradient(135deg,#1e3a5f,#0f766e);color:white;padding:32px;border-radius:12px;margin-bottom:24px}}
.header h1{{font-size:24px;margin-bottom:4px}}
.header .sub{{opacity:0.85;font-size:13px}}
.banner{{padding:20px;border-radius:12px;margin-bottom:24px;text-align:center;color:white;font-size:18px;font-weight:700}}
section{{background:white;border-radius:8px;padding:24px;margin-bottom:20px;box-shadow:0 1px 3px rgba(0,0,0,0.1)}}
section h2{{font-size:18px;color:#1e3a5f;border-bottom:2px solid #e2e8f0;padding-bottom:8px;margin-bottom:16px}}
.card{{border:1px solid #e2e8f0;border-radius:6px;padding:12px;font-size:13px}}
.grid{{display:grid;gap:12px}}.grid-2{{grid-template-columns:1fr 1fr}}.grid-3{{grid-template-columns:1fr 1fr 1fr}}.grid-4{{grid-template-columns:1fr 1fr 1fr 1fr}}
.stat{{text-align:center;padding:16px;background:#f8fafc;border-radius:8px;border:1px solid #e2e8f0}}
.stat .num{{font-size:28px;font-weight:700;color:#0f766e}}.stat .lbl{{font-size:11px;color:#64748b;text-transform:uppercase}}
table{{width:100%;border-collapse:collapse;font-size:13px}}th{{text-align:left;padding:8px;background:#f1f5f9;color:#475569;font-size:12px;text-transform:uppercase}}td{{padding:8px;border-bottom:1px solid #f1f5f9}}
code{{background:#f1f5f9;padding:2px 4px;border-radius:3px;font-size:12px;word-break:break-all}}
.footer{{text-align:center;color:#94a3b8;font-size:11px;padding:20px}}
@media(max-width:768px){{.grid-2,.grid-3,.grid-4{{grid-template-columns:1fr}}}}
</style></head><body><div class="container">

<div class="header">
<h1>Code Analysis Report</h1>
<div class="sub">Generated {_esc(ts)} · Run ID: {_esc(report.get("runId",""))}</div>
</div>

<div class="banner" style="background:{banner_color};">{banner_text}</div>

{'<section><h2>Executive Summary</h2><p>' + _esc(exec_summary) + '</p></section>' if exec_summary else ''}

<section>
<h2>Results Summary</h2>
<div class="grid grid-4">
<div class="stat"><div class="num" style="color:#dc2626">{errors}</div><div class="lbl">Errors</div></div>
<div class="stat"><div class="num" style="color:#d97706">{warnings}</div><div class="lbl">Warnings</div></div>
<div class="stat"><div class="num">{novel}</div><div class="lbl">Novel Findings</div></div>
<div class="stat"><div class="num">{len(by_category)}</div><div class="lbl">Categories</div></div>
</div>
</section>

<section>
<h2>Scan Details</h2>
<div class="grid grid-3">
<div class="card"><strong>Cluster</strong><br>{_esc(cluster_id)}<br><span style="font-size:12px;color:#64748b;">{_esc(engine)} {_esc(current_version)}</span></div>
<div class="card"><strong>Target Version</strong><br>{_esc(target_version)}</div>
<div class="card"><strong>Source Code</strong><br><span style="word-break:break-all;">{_esc(source_url)}</span></div>
<div class="card"><strong>AppCI</strong><br>{_esc(appci)}</div>
<div class="card"><strong>Files Scanned</strong><br>{files_scanned}</div>
<div class="card"><strong>Total Findings</strong><br>{total}</div>
</div>
</section>

<section>
<h2>What Developers Must Change</h2>
{dev_html}
</section>

{'<section><h2>What DBAs Must Change</h2>' + dba_html + '</section>' if dba_html else ''}

{nth_html}

<section>
<h2>Findings by Category</h2>
<table><tr><th>Category</th><th>Total</th><th>Errors</th><th>Warnings</th></tr>{cat_html}</table>
</section>

<section>
<h2>Detailed Findings ({total}{' — showing top 100' if total > 100 else ''})</h2>
<table>
<tr><th>Severity</th><th>Category</th><th>Location</th><th>Code</th><th>Recommended Change</th></tr>
{findings_rows}
</table>
</section>

<div class="footer">RDS Upgrade Assistant — Code Analysis · Internal Use Only · {_esc(ts)}</div>
</div></body></html>"""


def _generate_dev_changes_from_findings(findings):
    """Generate developer change items from findings when AgentCore doesn't provide them."""
    # Group errors by category
    errors_by_cat = {}
    for f in findings:
        if f.get("severity") == "error":
            cat = f.get("category", "other")
            if cat not in errors_by_cat:
                errors_by_cat[cat] = []
            errors_by_cat[cat].append(f)

    dev_changes = []
    category_descriptions = {
        "sql-syntax": "Remove deprecated SQL syntax (SQL_CALC_FOUND_ROWS, FOUND_ROWS(), GROUP BY sorting, etc.)",
        "query-cache": "Remove all query cache references — query cache is removed in MySQL 8.0",
        "reserved-keyword": "Escape new reserved keywords with backticks (RANK, GROUPS, SYSTEM, ROW_NUMBER, etc.)",
        "charset": "Update character set references from utf8 to utf8mb4",
        "auth": "Update authentication plugin references from mysql_native_password to caching_sha2_password",
        "info-schema": "Update INFORMATION_SCHEMA queries — INNODB_LOCKS, INNODB_LOCK_WAITS moved to performance_schema",
        "driver": "Update database driver/connector to a version compatible with MySQL 8.0+",
        "config": "Remove deprecated configuration parameters",
        "connection": "Update connection string parameters for MySQL 8.0 compatibility",
        "schema": "Fix invalid date/timestamp defaults (0000-00-00 no longer allowed by default)",
    }

    for cat, cat_findings in sorted(errors_by_cat.items(), key=lambda x: -len(x[1])):
        count = len(cat_findings)
        files = list(set(f.get("fileName", "") for f in cat_findings))
        file_list = ", ".join(files[:3])
        if len(files) > 3:
            file_list += f" (+{len(files) - 3} more)"

        dev_changes.append({
            "priority": "P1" if count > 10 else "P2",
            "title": f"{cat} — {count} errors in {len(files)} files",
            "description": f"{category_descriptions.get(cat, 'Fix compatibility issues')}. Affected files: {file_list}",
        })

    # Also add warnings summary if significant
    warnings_by_cat = {}
    for f in findings:
        if f.get("severity") == "warning":
            cat = f.get("category", "other")
            if cat not in warnings_by_cat:
                warnings_by_cat[cat] = 0
            warnings_by_cat[cat] += 1

    for cat, count in sorted(warnings_by_cat.items(), key=lambda x: -x[1]):
        if cat not in errors_by_cat:  # Don't duplicate categories already in errors
            dev_changes.append({
                "priority": "P3",
                "title": f"{cat} — {count} warnings",
                "description": category_descriptions.get(cat, "Review and fix compatibility warnings"),
            })

    return dev_changes
