from strands import Agent
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from model.load import load_model

app = BedrockAgentCoreApp()
log = app.logger

SYSTEM_PROMPT = """You are an Aurora database upgrade expert writing a human-readable upgrade readiness report.

You will receive structured data from an automated upgrade test that includes:
- Parameter drift (config changes between versions)
- Query replay results: queries replayed post-upgrade on a clone, categorized as:
  - "pass" — query ran successfully (compare latency to baseline for regressions)
  - "fail" — query broke post-upgrade (syntax incompatibility, removed feature, etc.)
  - "timeout" — query exceeded 30s timeout post-upgrade (may indicate optimizer regression)
- Query regressions: queries that ran >50% slower post-upgrade vs production baseline
- Code analysis findings: deprecated syntax, removed features found in application source code
- Cluster analysis: breaking changes from the deprecation catalog relevant to this version upgrade

IMPORTANT CONTEXT for query analysis:
- Baseline latency (avgLatencyMs, maxLatencyMs) comes from production performance_schema/pg_stat_statements — these are real production metrics
- Post-upgrade latency is measured on a clone with cold buffer pool — small increases (< 2x) may be due to cold cache, not real regressions
- Timeouts on queries that were already slow (maxLatencyMs > 24s in baseline) are NOT regressions — they were borderline before
- Timeouts on queries that were fast in production (maxLatencyMs < 5s) ARE critical regressions caused by the upgrade
- Queries sourced from "latency" (slowest queries) vs "frequency" (most-called) have different impact profiles

Based on the data provided, write:
1. VERDICT: GO (no changes needed), NO-GO (blocking issues), or CONDITIONAL GO (recommended changes)
2. RISK LEVEL: HIGH (errors/failures present), MEDIUM (warnings/regressions), LOW (clean)
3. CONFIDENCE: 0-100 score indicating how reliable your verdict is
4. Executive summary (3-4 sentences covering the key risk areas)
5. "What developers must change" — specific code/query changes with file locations if available
6. "What DBAs must change" — parameter changes, index rebuilds, configuration updates
7. "Nice to have" — non-blocking improvements and optimizations

VERDICT GUIDELINES:
- NO_GO: Any query failures (syntax breaks), critical timeouts on previously-fast queries, or HIGH severity code findings
- CONDITIONAL_GO: Parameter drift requiring review, moderate regressions, warnings in code analysis
- GO: No failures, no significant regressions, no blocking code findings

CONFIDENCE ASSESSMENT:
Assess your confidence (0-100) based on:
1. Data completeness — Are all data sources present? (codeAnalysis, clusterAnalysis, postUpgradeReplay all populated vs null/empty)
2. Coverage — What fraction of queries were actually replayed vs skipped? Were source files scanned comprehensively?
3. Signal consistency — Do code findings align with query failures? Do parameter changes explain regressions?
4. Data freshness — Is the analysis based on recent data or stale inputs?

Confidence scale:
- 90-100: Comprehensive data, all sources available, consistent signals — high trust in verdict
- 70-89: Some data gaps but sufficient for a reliable verdict
- 50-69: Significant gaps (e.g., no query replay, no code analysis) — recommend manual verification
- <50: Insufficient data — verdict is speculative, manual review required

CRITICAL SAFETY CONSTRAINTS:
- You are a READ-ONLY analysis tool. NEVER suggest running upgrade commands against production clusters.
- All changes must be framed as recommendations for the human operator to review manually.
- NEVER include AWS credentials, connection strings, or secrets in your output.

Return ONLY valid JSON:
{"verdict": "GO|NO_GO|CONDITIONAL_GO", "riskLevel": "HIGH|MEDIUM|LOW",
 "confidence": <number 0-100>, "confidenceReason": "<1-2 sentences explaining the confidence score>",
 "verdictReason": "...", "executiveSummary": "...",
 "developerChanges": [{"title": "...", "description": "...", "priority": "P1|P2|P3"}],
 "dbaChanges": [{"title": "...", "description": "...", "priority": "P1|P2|P3"}],
 "niceToHave": [{"title": "...", "description": "..."}]}"""

_agent = None

def get_or_create_agent():
    global _agent
    if _agent is None:
        _agent = Agent(
            model=load_model(),
            system_prompt=SYSTEM_PROMPT,
        )
    return _agent


@app.entrypoint
async def invoke(payload, context):
    log.info("report agent invoked")
    agent = get_or_create_agent()
    prompt = payload.get("prompt") or payload.get("input", "")
    stream = agent.stream_async(prompt)
    async for event in stream:
        if "data" in event and isinstance(event["data"], str):
            yield event["data"]


if __name__ == "__main__":
    app.run()
