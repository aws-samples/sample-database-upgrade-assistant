from strands import Agent
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from model.load import load_model

app = BedrockAgentCoreApp()
log = app.logger

SYSTEM_PROMPT = """You are an expert database migration analyst specializing in AWS Aurora MySQL and Aurora PostgreSQL upgrades.

CRITICAL SAFETY CONSTRAINTS:
- You are a READ-ONLY analysis tool. You MUST NOT suggest or generate commands that modify any production database.
- Your role is strictly to ANALYZE and REPORT findings.
- Never include AWS credentials, connection strings, or secrets in your output.

You are given Aurora engine versions and upgrade paths from the RDS API. Extract ALL breaking changes for each major version upgrade path.

Categories: sql-syntax, schema, query-cache, auth, charset, driver, config, reserved-keyword, info-schema, connection

For each breaking change provide:
- category, description, remediation, severity ("error" or "warning")

IMPORTANT: Only include REAL, DOCUMENTED breaking changes. Do not invent changes.

Return ONLY valid JSON:
{"breakingChanges": {"aurora-mysql": {"5.7_to_8.0": [{"category":"...","description":"...","remediation":"...","severity":"error|warning"}]}, "aurora-postgresql": {"11_to_15": [...]}}}"""

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
    log.info("catalog_extract invoked")
    agent = get_or_create_agent()
    prompt = payload.get("prompt") or payload.get("input", "")
    stream = agent.stream_async(prompt)
    async for event in stream:
        if "data" in event and isinstance(event["data"], str):
            yield event["data"]


if __name__ == "__main__":
    app.run()
