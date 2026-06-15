from strands import Agent
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from model.load import load_model

app = BedrockAgentCoreApp()
log = app.logger

SYSTEM_PROMPT = """You are an expert database migration analyst. You are given code findings affected by an Aurora database engine upgrade.

CRITICAL SAFETY CONSTRAINTS:
- You are a READ-ONLY analysis tool. You MUST NOT suggest commands that modify any production database or application code directly.
- Your role is strictly to ANALYZE and RECOMMEND changes for the developer to review manually.
- Never include AWS credentials, connection strings, or secrets in your output.

For each finding, provide:
1. A clear explanation of WHY this code is affected
2. A specific remediation recommendation with code examples
3. Confirm or adjust severity (error = must fix, warning = should fix)

Return ONLY valid JSON:
[{"findingId": "...", "recommendedChange": "...", "severity": "error|warning", "explanation": "..."}]"""

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
    log.info("code_enrichment invoked")
    agent = get_or_create_agent()
    prompt = payload.get("prompt") or payload.get("input", "")
    stream = agent.stream_async(prompt)
    async for event in stream:
        if "data" in event and isinstance(event["data"], str):
            yield event["data"]


if __name__ == "__main__":
    app.run()
