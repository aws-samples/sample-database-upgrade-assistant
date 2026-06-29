"""S3 utilities for reading/writing reports and artifacts."""

import json
import logging

import boto3

from .config import S3_BUCKET, REGION

logger = logging.getLogger(__name__)
s3 = boto3.client("s3", region_name=REGION)


def put_json(key, data):
    """Write JSON data to S3."""
    s3.put_object(
        Bucket=S3_BUCKET,
        Key=key,
        Body=json.dumps(data, indent=2, default=str).encode(),
        ContentType="application/json",
    )
    logger.info("Uploaded s3://%s/%s", S3_BUCKET, key)


def get_json(key):
    """Read JSON data from S3."""
    obj = s3.get_object(Bucket=S3_BUCKET, Key=key)
    return json.loads(obj["Body"].read().decode())


def put_html(key, html_content):
    """Write HTML content to S3."""
    s3.put_object(
        Bucket=S3_BUCKET,
        Key=key,
        Body=html_content.encode("utf-8"),
        ContentType="text/html",
    )
    logger.info("Uploaded HTML s3://%s/%s", S3_BUCKET, key)


def generate_presigned_url(key, expires_in=3600):
    """Generate a presigned URL for downloading."""
    return s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": S3_BUCKET, "Key": key},
        ExpiresIn=expires_in,
    )
