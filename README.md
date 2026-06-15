# RDS Upgrade Assistant 

AI-powered tool that automates Aurora MySQL and PostgreSQL upgrade readiness assessment. Combines deterministic AWS API calls with Amazon Bedrock AgentCore to produce actionable upgrade reports — all on a fully serverless architecture with zero always-on infrastructure.
**THIS IS FOR NON-PRODUCTION USE**

## What It Does

- **Version Catalog**: Builds a breaking changes catalog from RDS API + AWS documentation via AgentCore
- **Code Analysis**: Scans application source code (GitHub or S3) for deprecated patterns and incompatibilities
- **Upgrade Test**: Runs a full clone-upgrade-validate cycle on a test clone with query replay and regression detection
- **Report**: Produces a GO/NO-GO/CONDITIONAL GO verdict with confidence score, developer/DBA action items, and polished HTML report

## Architecture

- **Frontend**: React + Cloudscape Design System (S3 + CloudFront)
- **API**: Python Lambda behind API Gateway (REST + WebSocket for progress)
- **Pipeline**: 8 Lambda functions for deterministic steps, orchestrated by Step Functions
- **AI**: Amazon Bedrock AgentCore — 3 agents (catalog extraction, code enrichment, report generation)
- **Storage**: DynamoDB (run records, findings, settings), S3 (reports, catalog), Secrets Manager (GitHub token)
- **Orchestration**: Step Functions with guaranteed clone cleanup on all failure paths

![Architecture](docs/Upgrade Assistant Architecture.png)

### AgentCore Agents

| Agent | Model | Purpose |
|---|---|---|
| catalog_extract | Claude Haiku 4.5 | Extracts breaking changes from AWS documentation |
| code_enrichment | Claude Sonnet 4 | Enriches code findings with contextual remediation |
| report | Claude Sonnet 4 | Generates GO/NO-GO verdict with confidence score + HTML report |

No direct Bedrock API calls — all AI inference goes through AgentCore.

### Lambda Functions

| Function | Timeout | Purpose |
|---|---|---|
| aurora-upgrade-planner-api | 30s | REST API handler (all routes + GitHub OAuth) |
| aurora-upgrade-planner-deprecation | 15min | Builds version catalog via RDS API + AgentCore |
| aurora-upgrade-planner-code-analysis | 15min | Scans source code via tarball download + regex + AgentCore enrichment |
| aurora-upgrade-planner-baseline | 5min | Captures parameter group + top queries (by frequency + latency) |
| aurora-upgrade-planner-clone | 5min | Creates point-in-time clone (copy-on-write) |
| aurora-upgrade-planner-poller | 2min | Polls clone/upgrade status for Step Functions wait loop |
| aurora-upgrade-planner-upgrade | 5min | Submits engine-aware ModifyDBCluster on clone |
| aurora-upgrade-planner-validation | 15min | Replays queries (30s timeout each), detects regressions, calls AgentCore for verdict |
| aurora-upgrade-planner-cleanup | 10min | Deletes clone cluster and instance |

## Prerequisites

- AWS CLI v2 configured with admin or PowerUser credentials
- Node.js 20+ and npm 9+
- Python 3.12+
- AgentCore CLI (`npm install -g @aws/agentcore`)
- A VPC with subnets (for RDS clone operations)
- Amazon Bedrock model access enabled (Claude Haiku 4.5, Claude Sonnet 4)

## Installation

```bash
git clone https://github.com/aws-samples/rds-upgrade-assistant.git
cd rds-upgrade-assistant
./install.sh
```

The installer will prompt for:
- **VPC ID** — an existing VPC with subnets for RDS clone operations
- **ExternalId** — a shared secret for cross-account role assumption
- **Tag key/value** — custom tag applied to all created resources

Everything else is auto-detected (account ID, region, subnets, AgentCore endpoints). Total install time: ~5-8 minutes.

The installer deploys 2 CloudFormation stacks, packages Lambda code, deploys 3 AgentCore agents, builds the React frontend, and syncs to S3/CloudFront. No Docker required.

## Project Structure

```
backend/
  api/                   # REST API Lambda (handler.py + github_auth.py)
  lambdas/               # 8 pipeline Lambda functions
  agentcore/             # AgentCore agent definitions + app code
  shared/                # Shared modules (config, state, s3_utils, agentcore_client)
  mcp_servers/           # MCP servers for interactive Kiro agent sessions
frontend/
  src/pages/             # 8 pages (Dashboard, Databases, Code Analysis, Settings, etc.)
  src/components/        # Reusable components (GitHubIntegration, FindingsTable, etc.)
  src/services/api.ts    # API client with TypeScript interfaces
infra/
  01-foundation.yaml     # DynamoDB, S3, CloudFront, Lambda, Step Functions, IAM
  02-frontend-api-stub.yaml  # API Gateway, WebSocket, API Lambda role
  step-function.json     # Upgrade test pipeline state machine definition
```

## Usage

1. Open the CloudFront URL in your browser
2. **Settings**: (Optional) Connect GitHub for private repo code analysis
3. **Version Catalog**: Run deprecation scan to build the breaking changes catalog
4. **Code Analysis**: Point at your source code (GitHub URL or S3) to find incompatibilities
5. **Upgrade Test**: Run a full clone-upgrade-validate cycle — produces HTML report with verdict

## Safety

- All upgrade operations run on **clones only** — production clusters are never modified
- IAM write permissions scoped to `aurora-planner-test-*` resource names
- Step Functions guarantees clone cleanup on every execution path (including failures)
- All AgentCore prompts include safety constraints (read-only analysis, no executable commands)
- GitHub tokens stored in Secrets Manager — never in logs, API responses, or DynamoDB
- Query replay executes only SELECT/SHOW/EXPLAIN — never DML, with 30s per-query timeout
- CORS restricted to the CloudFront origin

## Cost

~$150-200/year for 100 clusters upgraded annually. ~$1/month at rest (no always-on compute). Per-run cost: ~$1.50-2.00 per cluster upgrade cycle.

## Security Review

See [PREREQUISITES.md](PREREQUISITES.md) for the full security review document including IAM permissions, data handling, and approval checklist.

## License

This project is licensed under the MIT-0 License. See the [LICENSE](LICENSE) file.
