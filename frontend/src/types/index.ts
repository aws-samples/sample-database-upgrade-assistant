/**
 * Shared TypeScript types for RDS Upgrade Assistant
 *
 * These interfaces map to the DynamoDB data models and JSON schemas
 * defined in the design document.
 */

/** Agent run metadata — stored in aurora-upgrade-planner-runs DynamoDB table */
export interface AgentRun {
  runId: string;
  agentType: 'deprecation' | 'code-analysis' | 'cluster-analysis-compat' | 'upgrade-test';
  status: 'pending' | 'running' | 'complete' | 'failed';
  startTime: string;
  endTime: string | null;
  errorMessage?: string;
  s3ReportKey?: string;
  summary?: Record<string, number>;
  inputs?: CodeAnalysisInputs;
  modelUsed?: string;
  inputTokens?: number;
  outputTokens?: number;
  durationSeconds?: number;
  inputHash?: string;
}

/** Input parameters for a code analysis run */
export interface CodeAnalysisInputs {
  accountId1: string;
  clusterArn: string;
  accountId2: string;
  appci: string;
  sourceUrl: string;
  targetVersion?: string;
  catalogVersion?: string;
}

/** Response from GET /code-analysis/findings/{runId} */
export interface FindingsResponse {
  runId: string;
  findings: Finding[];
  summary: {
    totalFindings: number;
    errors: number;
    warnings: number;
    byCategory: Record<string, number>;
  };
  reportUrl?: string;
  clusterMetadata?: ClusterMetadata;
  connectedResources?: ConnectedResource[];
}

/** Individual code analysis finding — stored in aurora-upgrade-planner-findings table */
export interface Finding {
  runId: string;
  findingId: string;
  fileName: string;
  lineNumber: number;
  codeSnippet: string;
  category: FindingCategory;
  severity: 'error' | 'warning';
  recommendedChange: string;
  isNovel: boolean;
  catalogReference?: string;
}

/** Valid finding categories */
export type FindingCategory =
  | 'sql-syntax'
  | 'schema'
  | 'query-cache'
  | 'auth'
  | 'charset'
  | 'driver'
  | 'config'
  | 'reserved-keyword'
  | 'info-schema';

/** Real-time progress event — stored in aurora-upgrade-planner-progress-events table */
export interface ProgressEvent {
  runId: string;
  timestamp: string;
  percentage: number;
  currentActivity: string;
  elapsedSeconds: number;
  agentType: 'deprecation' | 'code-analysis' | 'cluster-analysis';
}

/** Cluster metadata returned by the Cluster Scanner */
export interface ClusterMetadata {
  accountId: string;
  clusterId: string;
  engine: 'aurora-mysql' | 'aurora-postgresql';
  version: string;
  region: string;
  status: string;
  endOfSupport: string;
  appciTag?: string;
}

/** Connected AWS resource found by the Resource Scanner */
export interface ConnectedResource {
  type: string;
  id: string;
  connectionMethod: string;
  status: string;
}

/** Full deprecation catalog — single JSON file in S3 */
export interface DeprecationCatalog {
  generatedAt: string;
  engines: {
    'aurora-mysql': EngineData;
    'aurora-postgresql': EngineData;
  };
}

/** Engine-specific data within the deprecation catalog */
export interface EngineData {
  ltsVersion: string;
  versions: EngineVersion[];
}

/** Single engine version entry in the deprecation catalog */
export interface EngineVersion {
  version: string;
  parameterFamily: string;
  status: 'available' | 'deprecated' | 'end-of-life';
  endOfSupport: string;
  upgradeTargets: string[];
  breakingChanges: BreakingChange[];
}

/** Breaking change associated with a version upgrade */
export interface BreakingChange {
  category: FindingCategory;
  description: string;
  remediation: string;
  severity: 'error' | 'warning';
}

/** Version entry in the available versions list from the catalog API */
export interface CatalogVersionEntry {
  version: string;
  lastModified: string;
  size: number;
}

/** Summary returned by the catalog API */
export interface CatalogSummary {
  totalVersions: number;
  deprecatedVersions: number;
  engines: string[];
}

/** Full response from GET /deprecation/catalog */
export interface DeprecationCatalogResponse {
  catalogUrl: string;
  lastUpdated: string;
  currentVersion: string;
  availableVersions: CatalogVersionEntry[];
  summary: CatalogSummary;
  catalog: DeprecationCatalog;
}

/** Application settings — stored in aurora-upgrade-planner-settings table */
export interface Settings {
  aws: {
    accountId: string;
    region: string;
    externalId?: string;
    s3Bucket: string;
    compute?: string;
  };
  github?: {
    token: string;
    connectionStatus: 'connected' | 'disconnected' | 'unknown';
  };
  models: {
    deprecation: string | { modelId: string; rationale?: string };
    codeAnalysis: string | { modelId: string; rationale?: string };
  };
}

/** Dashboard summary statistics */
export interface DashboardSummary {
  deprecation: {
    versionsCataloged: number;
    deprecatedCount: number;
    lastUpdated: string | null;
  };
  codeAnalysis: {
    analysesCompleted: number;
    clustersAnalyzed: number;
    lastRun: string | null;
    totalFindings: number;
  };
  clusterAnalysis: {
    analysesCompleted: number;
    clustersAnalyzed: number;
    lastRun: string | null;
    totalBreakingChanges: number;
  };
  upgradeTests: {
    totalRuns: number;
    completed: number;
    failed: number;
    running: number;
    lastRun: string | null;
    goCount: number;
    noGoCount: number;
    conditionalCount: number;
  };
  recentRuns: AgentRun[];
}

/** Aurora cluster from RDS DescribeDBClusters */
export interface AuroraCluster {
  clusterId: string;
  clusterArn: string;
  engine: 'aurora-mysql' | 'aurora-postgresql';
  engineVersion: string;
  status: string;
  region: string;
  accountId: string;
  endpoint?: string;
  multiAz: boolean;
  upgradeReadiness?: UpgradeReadiness;
}

/** Upgrade readiness summary for a cluster */
export interface UpgradeReadiness {
  deprecationStatus: 'deprecated' | 'approaching' | 'ok' | 'unknown';
  endOfSupport?: string;
  lastCodeAnalysis?: {
    runId: string;
    status: 'complete' | 'failed' | 'running';
    startTime: string;
    totalFindings: number;
    errors: number;
  };
  lastClusterAnalysis?: {
    runId: string;
    status: 'complete' | 'failed' | 'running';
    startTime: string;
    breakingChanges: number;
    highSeverity: number;
  };
  lastUpgradeTest?: {
    runId: string;
    status: 'complete' | 'failed' | 'running';
    startTime: string;
    phase: string;
  };
  upgradeTargets: string[];
}

/** Pre-flight assessment returned before starting an upgrade test */
export interface UpgradeTestPreflight {
  clusterArn: string;
  currentVersion: string;
  targetVersion: string;
  engine: string;
  upgradeStrategy: 'in-place' | 'blue-green';
  estimatedDurationMinutes: number;
  estimatedCostUsd: number;
  breakingChanges: BreakingChange[];
  breakingChangeSummary: { high: number; medium: number; low: number };
  steps: string[];
  warnings: string[];
}

/** Single query replay execution result */
export interface QueryReplayResult {
  queryId: string;
  digest: string;
  status: 'pass' | 'fail' | 'skipped';
  latencyMs: number;
  error: string;
}

/** Aggregated query replay summary for a phase */
export interface QueryReplaySummary {
  phase: 'during_upgrade' | 'post_upgrade';
  totalQueries: number;
  passed: number;
  failed: number;
  skipped: number;
  totalOutageSeconds: number;
  results: QueryReplayResult[];
  failures?: QueryReplayFailure[];
  regressions?: QueryRegression[];
}

/** Query that failed post-upgrade (incompatibility) */
export interface QueryReplayFailure {
  queryId: string;
  digest: string;
  error: string;
  severity: 'HIGH' | 'MEDIUM' | 'LOW';
}

/** Baseline query collected from performance_schema or pg_stat_statements */
export interface BaselineQuery {
  queryId: string;
  digest: string;
  sampleSql: string;
  schema: string;
  callCount: number;
  avgLatencyMs: number;
  maxLatencyMs: number;
}
export interface UpgradeTestRun {
  runId: string;
  agentType: 'upgrade-test';
  status: 'pending' | 'running' | 'complete' | 'failed';
  phase: UpgradeTestPhase;
  startTime: string;
  phaseStartTime?: string;
  endTime?: string;
  durationSeconds?: number;
  errorMessage?: string;
  clusterArn: string;
  engine?: string;
  sourceVersion?: string;
  targetVersion: string;
  upgradeStrategy: 'in-place' | 'blue-green';
  snapshotId?: string;
  clonedClusterArn?: string;
  clonedClusterId?: string;
  cloneMode?: 'clone' | 'snapshot' | 'blue-green';
  skipSnapshot?: boolean; // legacy
  reportUrl?: string;
  s3ReportKey?: string;
  phaseTiming?: Record<string, { startTime: string; elapsedAtStart: number }>;
  summary?: {
    snapshotReady: boolean;
    snapshotSkipped?: boolean;
    cloneMode?: string;
    baselineCaptured: boolean;
    compatibilityFindings: number;
    upgradeSucceeded: boolean;
    regressions: number;
    queryFailures?: number;
    queryRegressions?: number;
    outageSeconds?: number;
    cutoverSeconds?: number;
    riskLevel?: string;
    actionItemCount?: number;
    verdict?: string;
  };
}

export type UpgradeTestPhase =
  | 'snapshot'
  | 'baseline'
  | 'compatibility'
  | 'clone'
  | 'upgrade'
  | 'validation'
  | 'report'
  | 'complete'
  | 'failed';

/* ── Cluster Analysis ── */

/** Stored procedure collected from the database */
export interface StoredProcedure {
  name: string;
  schema: string;
  type: 'PROCEDURE' | 'FUNCTION' | 'TRIGGER' | 'EVENT';
  body: string;
  findings: SpFinding[];
}

/** Incompatibility finding on a stored procedure */
export interface SpFinding {
  line: number;
  snippet: string;
  category: FindingCategory;
  severity: 'HIGH' | 'MEDIUM' | 'LOW';
  description: string;
  remediation: string;
}

/** Non-default parameter group setting */
export interface ParameterSetting {
  name: string;
  currentValue: string;
  defaultValue: string;
  compatibility: 'ok' | 'changed' | 'removed';
  note?: string;
}

/** Profiled query from performance_schema / stats */
export interface ProfiledQuery {
  queryId: string;
  digest: string;
  callCount: number;
  avgLatencyMs: number;
  maxLatencyMs: number;
  schema: string;
  findings: QueryFinding[];
}

/** Incompatibility finding on a profiled query */
export interface QueryFinding {
  category: FindingCategory;
  severity: 'HIGH' | 'MEDIUM' | 'LOW';
  description: string;
  remediation: string;
}

/** Full cluster analysis result */
export interface ClusterAnalysis {
  runId?: string;
  clusterArn: string;
  clusterId: string;
  engine: string;
  currentVersion: string;
  targetVersion: string;
  collectedAt: string;
  breakingChanges: BreakingChange[];
  breakingChangeSummary: { total: number; high: number; medium: number; low: number };
  storedProcedures: StoredProcedure[];
  parameterSettings: ParameterSetting[];
  profiledQueries: ProfiledQuery[];
  spSummary: { total: number; withFindings: number; high: number; medium: number; low: number };
  querySummary: { total: number; withFindings: number; high: number; medium: number; low: number };
}

/** Upgrade test result with performance comparison */
export interface UpgradeTestResult {
  runId: string;
  clusterArn: string;
  strategy: 'in-place' | 'blue-green';
  targetVersion: string;
  startTime: string;
  endTime: string;
  actualDurationMinutes: number;
  actualDowntimeSeconds: number;
  upgradeSucceeded: boolean;
  queryRegressions: QueryRegression[];
  parameterDrift: ParameterSetting[];
  actionItems: ActionItem[];
  optimizationSuggestions: string[];
}

/** Query that regressed after upgrade */
export interface QueryRegression {
  digest: string;
  beforeLatencyMs: number;
  afterLatencyMs: number;
  regressionPct: number;
  severity: 'HIGH' | 'MEDIUM' | 'LOW';
  cause: string;
  fix: string;
}

/** Prioritized action item from the upgrade report */
export interface ActionItem {
  priority: 'P1' | 'P2' | 'P3';
  category: string;
  title: string;
  description: string;
  fix: string;
}


/* ── GitHub Device Flow Auth ── */

/** Response from POST /github/device-code */
export interface GitHubDeviceCodeResponse {
  userCode: string;
  verificationUri: string;
  expiresIn: number;
  interval: number;
  deviceCode: string;
}

/** Response from POST /github/poll-token */
export interface GitHubPollResponse {
  status: 'connected' | 'pending' | 'expired' | 'denied';
  username?: string;
  authorizedAt?: string;
  interval?: number;
  message?: string;
}

/** Response from GET /github/status */
export interface GitHubStatusResponse {
  status: 'connected' | 'disconnected' | 'expired';
  username?: string;
  authorizedAt?: string;
  maskedClientId?: string;
}

/** Response from PUT /github/client-id */
export interface GitHubClientIdResponse {
  saved: boolean;
  maskedClientId: string;
}
