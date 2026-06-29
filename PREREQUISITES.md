# RDS Upgrade Assistant — Prerequisites & Security Review

This document is intended for the customer's security team to review and approve before deployment.

## 1. Infrastructure Requirements

| Requirement | Details |
|---|---|
| AWS Account | One AWS account to host the tool infrastructure |
| Region | us-east-1 (default, configurable) |
| VPC | Existing VPC with subnets (for RDS clone operations and DB connectivity) |
| Amazon Bedrock | Model access enabled for Claude Haiku 4.5 and Claude Sonnet 4.5 |
| Amazon Bedrock AgentCore | Ability to deploy 3 AgentCore runtimes |
| CloudFormation | Ability to deploy 2 CloudFormation stacks |
| IAM | Ability to create 3 IAM roles (or use pre-created roles) |
| Node.js | 20+ required for AgentCore CLI |

## 2. IAM Roles & Permissions

### Role 1: Pipeline Lambda Execution Role

- **Purpose**: Executes all 8 pipeline Lambda functions (baseline, clone, poller, upgrade, validation, cleanup, deprecation, code-analysis)
- **Trust**: `lambda.amazonaws.com`
- **Permissions**:
  - CloudWatch Logs: CreateLogGroup, CreateLogStream, PutLogEvents (scoped to `/aws/lambda/aurora-upgrade-planner-*`)
  - DynamoDB: PutItem, GetItem, UpdateItem, DeleteItem, Query, Scan on `aurora-upgrade-planner-*` tables
  - S3: GetObject, PutObject, ListBucket on the report bucket
  - RDS (read): DescribeDBClusters, DescribeDBInstances, DescribeDBEngineVersions, DescribeDBClusterParameters, DescribeDBClusterSnapshots
  - RDS (write): RestoreDBClusterToPointInTime, ModifyDBCluster, CreateDBInstance, DeleteDBCluster, DeleteDBInstance — scoped to `aurora-planner-test-*` + parameter groups
  - Performance Insights: GetResourceMetrics
  - Secrets Manager: GetSecretValue, PutSecretValue, CreateSecret, DeleteSecret, DescribeSecret — scoped to `aurora-upgrade-planner/github-token*`
  - Bedrock AgentCore: InvokeAgentRuntime — scoped to account runtimes
  - Lambda: InvokeFunction on `aurora-upgrade-planner-*` functions
  - Step Functions: StartExecution on `aurora-upgrade-planner-*` state machines

### Role 2: API Lambda Execution Role

- **Purpose**: Serves the REST API (list databases, trigger pipelines, read results, GitHub OAuth device flow)
- **Trust**: `lambda.amazonaws.com`
- **Permissions**:
  - DynamoDB: CRUD operations on `aurora-upgrade-planner-*` tables
  - S3: GetObject, PutObject, ListBucket on the report bucket
  - Lambda: InvokeFunction on `aurora-upgrade-planner-*` functions
  - Step Functions: StartExecution on `aurora-upgrade-planner-*` state machines
  - RDS (read): Describe* operations
  - RDS (write): Delete operations — scoped to `aurora-planner-test-*` clone names only
  - STS: AssumeRole (scoped to cross-account role), GetCallerIdentity
  - Secrets Manager: CRUD — scoped to `aurora-upgrade-planner/github-token*`
  - IAM: PassRole (scoped to `aurora-upgrade-planner-*` roles)

### Role 3: AgentCore Execution Roles (auto-created)

- **Purpose**: Allows AgentCore runtimes to invoke Bedrock models and write logs
- **Trust**: `bedrock-agentcore.amazonaws.com`
- **Permissions**:
  - Bedrock: InvokeModel, InvokeModelWithResponseStream on foundation models (Claude Haiku 4.5, Claude Sonnet 4.5)
  - CloudWatch Logs: CreateLogGroup, CreateLogStream, PutLogEvents
  - X-Ray: PutTraceSegments, PutTelemetryRecords

---

## 3. AWS Services Used

| Service | Purpose | Billing Impact |
|---|---|---|
| AWS Lambda | 9 functions (1 API + 8 pipeline) | Pay per invocation |
| Amazon Bedrock AgentCore | 3 runtimes (catalog_extract, code_enrichment, report) | Pay per token |
| Amazon API Gateway | REST + WebSocket APIs | Pay per request |
| Amazon Step Functions | Upgrade test orchestration | Pay per state transition |
| Amazon DynamoDB | Run records, findings, settings (on-demand) | Pay per request |
| Amazon S3 | Reports, deprecation catalog, frontend assets | Storage + requests |
| Amazon CloudFront | Frontend CDN | Data transfer |
| AWS Secrets Manager | GitHub OAuth token, DB credentials | $0.40/secret/month |
| Amazon CloudWatch | Logs for all Lambda functions | Log storage |
| AWS STS | Cross-account role assumption | No cost |
| Performance Insights | Query metrics collection | Free tier for most instances |

**Estimated cost**: ~$150-200/year for 100 clusters upgraded once annually. ~$1/month at rest.

---

## 4. Network Requirements

| Requirement | Details |
|---|---|
| VPC | Lambda functions for baseline/validation need VPC access to RDS endpoints (ports 3306/5432) |
| Internet access | Lambda functions need outbound HTTPS (443) for Bedrock AgentCore, GitHub API, AWS documentation |
| No inbound access | No Lambda functions accept inbound connections |
| Cross-account | STS AssumeRole over HTTPS (no VPC peering needed) |
| GitHub API | Code analysis downloads tarballs via `api.github.com` (HTTPS, no git binary needed) |

---

## 5. Data Handling

| Data Type | Handling |
|---|---|
| Production database safety | Upgrade test always clones first — production is never modified. IAM scoped to `aurora-planner-test-*` names. |
| Source code | Downloaded as tarball to Lambda /tmp (ephemeral), deleted after analysis. Never stored permanently. |
| GitHub credentials | OAuth token stored in Secrets Manager (never in DynamoDB, logs, or API responses) |
| DB credentials | Stored in Secrets Manager, accessed via IAM role (never in env vars or logs) |
| Reports | Stored in S3 with server-side encryption (AES-256). Presigned URLs expire after 24 hours. |
| DynamoDB | Encrypted at rest (AWS managed keys) |
| Bedrock prompts | Sent over HTTPS to AgentCore; include safety constraints prohibiting destructive commands |
| Cross-account credentials | Temporary STS tokens (15 min TTL), never persisted |
| Query replay | Only SELECT/SHOW/EXPLAIN/WITH replayed — never DML. 30-second timeout per query. |

---

## 6. Security Controls

- All RDS write operations scoped to `aurora-planner-test-*` resource names
- Clone clusters are automatically deleted after validation (Step Functions provides cleanup on all failure paths)
- All AgentCore prompts include safety constraints (read-only analysis, no executable commands)
- DynamoDB TTL auto-expires run records after 90 days
- No secrets stored in environment variables or code
- Input validation on all API endpoints (ARN format, account ID format, version format)
- CORS restricted to CloudFront origin (not wildcard)
- GitHub token never appears in logs, API responses, or DynamoDB
- No direct Bedrock API calls — all AI inference through AgentCore

---

## 7. Approval Checklist

- [ ] IAM: Approve creation of 3 IAM roles with permissions listed in Section 2
- [ ] Bedrock: Enable model access for Claude Haiku 4.5 and Claude Sonnet 4.5
- [ ] Bedrock AgentCore: Approve AgentCore runtime deployment (3 agents)
- [ ] Network: Lambda functions need outbound HTTPS (443) to Bedrock AgentCore, S3, RDS, STS, GitHub API
- [ ] Cross-account: Create `aurora-upgrade-planner-cross-account-role` in target account(s)
- [ ] S3: Approve creation of 2 S3 buckets (frontend + reports)
- [ ] DynamoDB: Approve creation of 4 DynamoDB tables (on-demand billing)
- [ ] Secrets Manager: Approve creation of 1 secret (GitHub OAuth token)
- [ ] CloudFront: Approve creation of 1 CloudFront distribution
- [ ] Step Functions: Approve creation of 1 state machine
- [ ] Data retention: Confirm 90-day DynamoDB TTL is acceptable
