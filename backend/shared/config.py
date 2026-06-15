"""Shared configuration for all Lambda functions."""

import os

TABLE_PREFIX = os.environ.get("DYNAMODB_TABLE_PREFIX", "aurora-upgrade-planner-")
S3_BUCKET = os.environ.get("S3_REPORT_BUCKET", "")
REGION = os.environ.get("AWS_REGION", "us-east-1")
AGENTCORE_REPORT_ENDPOINT = os.environ.get("AGENTCORE_REPORT_ENDPOINT", "")
AGENTCORE_CODE_ENRICHMENT_ENDPOINT = os.environ.get("AGENTCORE_CODE_ENRICHMENT_ENDPOINT", "")
AGENTCORE_CATALOG_EXTRACT_ENDPOINT = os.environ.get("AGENTCORE_CATALOG_EXTRACT_ENDPOINT", "")
DB_SECRET_ARN = os.environ.get("DB_SECRET_ARN", "")
QUERY_REPLAY_LIMIT = int(os.environ.get("QUERY_REPLAY_LIMIT", "20"))
