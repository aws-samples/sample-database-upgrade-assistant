"""Shared AgentCore invocation helper using boto3 (SigV4 signed)."""

import json
import logging
from urllib.parse import unquote

import boto3

from .config import REGION

logger = logging.getLogger(__name__)

_client = None


def _get_client():
    global _client
    if _client is None:
        from botocore.config import Config
        _client = boto3.client(
            "bedrock-agentcore",
            region_name=REGION,
            config=Config(read_timeout=120, connect_timeout=10),
        )
    return _client


def _extract_runtime_arn(endpoint_url):
    """Extract runtime ARN from AgentCore endpoint URL.

    URL format: https://bedrock-agentcore.{region}.amazonaws.com/runtimes/{encoded_arn}/invocations
    """
    if not endpoint_url:
        return None
    # Decode URL-encoded ARN
    decoded = unquote(endpoint_url)
    # Extract the full ARN
    if "arn:aws:bedrock-agentcore:" in decoded:
        start = decoded.index("arn:aws:bedrock-agentcore:")
        end = decoded.find("/invocations", start)
        if end == -1:
            return decoded[start:]
        return decoded[start:end]
    return None


def invoke_agentcore(endpoint_url, input_text):
    """Invoke an AgentCore agent using boto3 (SigV4 signed).

    Args:
        endpoint_url: The AgentCore endpoint URL (from env var)
        input_text: The prompt/input text to send to the agent

    Returns:
        The agent's response text, or None if invocation fails
    """
    if not endpoint_url:
        logger.warning("No AgentCore endpoint configured")
        return None

    runtime_arn = _extract_runtime_arn(endpoint_url)
    if not runtime_arn:
        logger.warning("Could not extract runtime ARN from endpoint: %s", endpoint_url)
        return None

    try:
        client = _get_client()
        payload_bytes = json.dumps({"prompt": input_text}).encode("utf-8")

        response = client.invoke_agent_runtime(
            agentRuntimeArn=runtime_arn,
            payload=payload_bytes,
            contentType="application/json",
            accept="application/json",
        )

        # Response may be a StreamingBody or EventStream depending on SDK version
        stream = response.get("response") or response.get("body")
        if stream is None:
            logger.warning("AgentCore response has no 'response' or 'body' key. Keys: %s", list(response.keys()))
            return None

        # Read the full response
        if hasattr(stream, "read"):
            raw = stream.read().decode("utf-8")
        elif hasattr(stream, "__iter__"):
            # EventStream — iterate and collect chunks
            parts = []
            for event in stream:
                if "chunk" in event:
                    parts.append(event["chunk"].get("bytes", b"").decode("utf-8"))
                elif "bytes" in event:
                    parts.append(event["bytes"].decode("utf-8"))
            raw = "".join(parts)
        else:
            raw = str(stream)

        if not raw.strip():
            logger.warning("AgentCore returned empty response body")
            return None

        # Try to parse as plain JSON first (non-streaming response)
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                # Response might be {"output": "..."} or {"completion": "..."}
                text = parsed.get("output") or parsed.get("completion") or parsed.get("text") or ""
                if text:
                    return text
                # Or it might be the direct JSON result
                return raw
            return raw
        except (json.JSONDecodeError, TypeError):
            pass

        # Parse as SSE format: data: "chunk"\n\n
        chunks = []
        for line in raw.split("\n"):
            line = line.strip()
            if line.startswith("data: "):
                data_part = line[6:]  # Remove "data: " prefix
                # Each data value is a JSON string — parse it properly
                try:
                    chunks.append(json.loads(data_part))
                except (json.JSONDecodeError, TypeError):
                    # Fallback: use raw value stripped of quotes
                    if data_part.startswith('"') and data_part.endswith('"'):
                        data_part = data_part[1:-1]
                    chunks.append(data_part)
            elif line and not line.startswith(":") and not line.startswith("event:"):
                # Non-SSE line — might be raw text
                chunks.append(line)

        if chunks:
            result = "".join(str(c) for c in chunks)
            logger.info("AgentCore invocation successful (arn=%s, response_len=%d)",
                        runtime_arn[-40:], len(result))
            return result

        # Last resort — return raw text
        logger.info("AgentCore invocation — returning raw response (len=%d)", len(raw))
        return raw

    except Exception as e:
        logger.error("AgentCore invocation failed (arn=%s): %s", runtime_arn[-40:] if runtime_arn else "none", e)
        return None
