import { API_URL, API_KEY } from '../config';
import type {
  AgentRun,
  AuroraCluster,
  BaselineQuery,
  ClusterAnalysis,
  CodeAnalysisInputs,
  DashboardSummary,
  DeprecationCatalogResponse,
  FindingsResponse,
  GitHubClientIdResponse,
  GitHubDeviceCodeResponse,
  GitHubPollResponse,
  GitHubStatusResponse,
  QueryReplaySummary,
  Settings,
  UpgradeTestPreflight,
  UpgradeTestResult,
  UpgradeTestRun,
} from '../types';

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  if (API_KEY) {
    headers['x-api-key'] = API_KEY;
  }
  const res = await fetch(`${API_URL}${path}`, {
    headers,
    ...options,
  });
  if (!res.ok) {
    const body = await res.text();
    throw new Error(body || `HTTP ${res.status}`);
  }
  return res.json() as Promise<T>;
}

/** Safe wrapper — returns mock data on network failure */
async function safeRequest<T>(path: string, fallback: T, options?: RequestInit): Promise<T> {
  try {
    return await request<T>(path, options);
  } catch {
    return fallback;
  }
}

/* ── Deprecation ── */

export async function triggerDeprecation(): Promise<{ runId: string }> {
  return request('/deprecation/run', { method: 'POST' });
}

export async function getDeprecationCatalog(version?: string): Promise<DeprecationCatalogResponse | null> {
  const query = version ? `?version=${encodeURIComponent(version)}` : '';
  return safeRequest<DeprecationCatalogResponse | null>(`/deprecation/catalog${query}`, null);
}

export async function getDeprecationRuns(): Promise<AgentRun[]> {
  return safeRequest('/deprecation/runs', []);
}

/* ── Code Analysis ── */

export async function triggerCodeAnalysis(
  inputs: CodeAnalysisInputs,
  force = true,
): Promise<{ runId: string; status: string; message: string }> {
  return request(`/code-analysis/run${force ? '?force=true' : ''}`, {
    method: 'POST',
    body: JSON.stringify(inputs),
  });
}

export async function getCodeAnalysisFindings(runId: string): Promise<FindingsResponse> {
  return request<FindingsResponse>(`/code-analysis/findings/${runId}`);
}

export async function getCodeAnalysisRuns(): Promise<AgentRun[]> {
  return safeRequest('/code-analysis/runs', []);
}

export async function getRunStatus(runId: string): Promise<{
  runId: string;
  status: string;
  phase?: string;
  progress?: number;
}> {
  return safeRequest(`/runs/${runId}/status`, {
    runId,
    status: 'unknown',
  });
}

/* ── Upgrade Test ── */

export async function getUpgradeTestPreflight(
  clusterArn: string,
  targetVersion?: string,
): Promise<UpgradeTestPreflight> {
  const q = new URLSearchParams({ clusterArn });
  if (targetVersion) q.set('targetVersion', targetVersion);
  return request(`/upgrade-test/preflight?${q.toString()}`);
}

export async function triggerUpgradeTest(
  clusterArn: string,
  targetVersion: string,
  upgradeStrategy: 'in-place' | 'blue-green',
  cloneMode: 'clone' | 'snapshot' = 'clone',
  snapshotConfig?: {
    snapshotId: string;
    snapshotAccount?: string;
    snapshotRoleArn?: string;
    snapshotKmsKey?: string;
  },
): Promise<{ runId: string; status: string; phase: string; cloneMode: string }> {
  return request('/upgrade-test/run', {
    method: 'POST',
    body: JSON.stringify({
      clusterArn,
      targetVersion,
      upgradeStrategy,
      cloneMode,
      ...snapshotConfig,
    }),
  });
}

export async function getUpgradeTestRuns(): Promise<UpgradeTestRun[]> {
  return safeRequest('/upgrade-test/runs', []);
}

export async function getUpgradeTestRun(runId: string): Promise<UpgradeTestRun> {
  return request(`/upgrade-test/runs/${runId}`);
}

export async function deleteUpgradeTestRun(runId: string): Promise<{
  runId: string;
  deleted: { snapshot: boolean; clone: boolean; record: boolean };
  errors: string[];
  message: string;
}> {
  return request(`/upgrade-test/runs/${runId}`, { method: 'DELETE' });
}

export async function getUpgradeTestReplay(
  runId: string,
  phase: 'during_upgrade' | 'post_upgrade' = 'post_upgrade',
): Promise<QueryReplaySummary | null> {
  return safeRequest<QueryReplaySummary | null>(
    `/upgrade-test/runs/${runId}/replay?phase=${phase}`,
    null,
  );
}

export async function getUpgradeTestBaseline(runId: string): Promise<{
  topQueries: BaselineQuery[];
  querySource: string;
  collectedAt: string;
  note: string;
} | null> {
  // Baseline is stored in S3 — fetch via the run's s3ReportKey prefix
  return safeRequest(`/upgrade-test/runs/${runId}/baseline`, null);
}

/* ── Cluster Analysis ── */

export interface ClusterAnalysisRunSummary {
  runId: string;
  clusterArn: string;
  clusterId: string;
  engine: string;
  currentVersion: string;
  targetVersion: string;
  startTime: string;
  breakingChanges: number;
  highSeverity: number;
}

export async function getClusterAnalysis(clusterArn: string, targetVersion?: string): Promise<ClusterAnalysis> {
  const q = new URLSearchParams({ clusterArn });
  if (targetVersion) q.set('targetVersion', targetVersion);
  return request(`/cluster-analysis?${q.toString()}`);
}

export async function getClusterAnalysisRuns(): Promise<ClusterAnalysisRunSummary[]> {
  const resp = await safeRequest<{ runs: ClusterAnalysisRunSummary[] }>('/cluster-analysis/runs', { runs: [] });
  return resp.runs;
}

export async function getClusterAnalysisRun(runId: string): Promise<ClusterAnalysis> {
  return request(`/cluster-analysis/runs/${runId}`);
}

export async function getUpgradeTestResult(runId: string): Promise<UpgradeTestResult> {
  return request(`/upgrade-test/result/${runId}`);
}

/* ── Databases ── */

export async function getDatabases(accountId?: string): Promise<{
  clusters: AuroraCluster[];
  accountId: string;
}> {
  const query = accountId ? `?accountId=${encodeURIComponent(accountId)}` : '';
  return safeRequest(`/databases${query}`, { clusters: [], accountId: accountId ?? '' });
}

/* ── Dashboard ── */

export async function getDashboardSummary(): Promise<DashboardSummary> {
  return safeRequest('/dashboard/summary', {
    deprecation: { versionsCataloged: 0, deprecatedCount: 0, lastUpdated: null },
    codeAnalysis: { analysesCompleted: 0, clustersAnalyzed: 0, lastRun: null, totalFindings: 0 },
    clusterAnalysis: { analysesCompleted: 0, clustersAnalyzed: 0, lastRun: null, totalBreakingChanges: 0 },
    upgradeTests: { totalRuns: 0, completed: 0, failed: 0, running: 0, lastRun: null, goCount: 0, noGoCount: 0, conditionalCount: 0 },
    recentRuns: [],
  });
}

/* ── Settings ── */

export async function getSettings(): Promise<Settings> {
  return safeRequest('/settings', {
    aws: { accountId: '', region: '', externalId: '', s3Bucket: '', compute: 'lambda' },
    github: { token: '', connectionStatus: 'unknown' },
    models: { deprecation: '', codeAnalysis: '' },
  });
}

export async function updateSettings(settings: Settings): Promise<{ updated: boolean }> {
  return safeRequest('/settings', { updated: false }, {
    method: 'PUT',
    body: JSON.stringify(settings),
  });
}

export async function validateCredentials(externalId: string): Promise<{ valid: boolean; message: string }> {
  return safeRequest('/settings/validate-credentials', { valid: false, message: 'API not available' }, {
    method: 'POST',
    body: JSON.stringify({ externalId }),
  });
}

export async function validateModel(modelId: string): Promise<{ valid: boolean; message: string }> {
  return safeRequest('/settings/validate-model', { valid: false, message: 'API not available' }, {
    method: 'POST',
    body: JSON.stringify({ modelId }),
  });
}


/* ── GitHub Device Flow Auth ── */

export async function saveGitHubClientId(clientId: string): Promise<GitHubClientIdResponse> {
  return request('/github/client-id', {
    method: 'PUT',
    body: JSON.stringify({ clientId }),
  });
}

export async function initiateGitHubDeviceFlow(): Promise<GitHubDeviceCodeResponse> {
  return request('/github/device-code', { method: 'POST' });
}

export async function pollGitHubToken(deviceCode: string, interval?: number): Promise<GitHubPollResponse> {
  return request('/github/poll-token', {
    method: 'POST',
    body: JSON.stringify({ deviceCode, ...(interval !== undefined && { interval }) }),
  });
}

export async function getGitHubStatus(): Promise<GitHubStatusResponse> {
  return request('/github/status');
}

export async function disconnectGitHub(): Promise<{ disconnected: boolean }> {
  return request('/github/connection', { method: 'DELETE' });
}
