import { useEffect, useState, useRef, useCallback } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import Container from '@cloudscape-design/components/container';
import Header from '@cloudscape-design/components/header';
import SpaceBetween from '@cloudscape-design/components/space-between';
import ColumnLayout from '@cloudscape-design/components/column-layout';
import Box from '@cloudscape-design/components/box';
import Badge from '@cloudscape-design/components/badge';
import Button from '@cloudscape-design/components/button';
import StatusIndicator from '@cloudscape-design/components/status-indicator';
import Spinner from '@cloudscape-design/components/spinner';
import Alert from '@cloudscape-design/components/alert';
import Modal from '@cloudscape-design/components/modal';
import ProgressBar from '@cloudscape-design/components/progress-bar';
import Table from '@cloudscape-design/components/table';
import Tabs from '@cloudscape-design/components/tabs';
import ExpandableSection from '@cloudscape-design/components/expandable-section';
import {
  getUpgradeTestRun,
  deleteUpgradeTestRun,
  getUpgradeTestReplay,
} from '../services/api';
import type {
  UpgradeTestRun,
  UpgradeTestPhase,
  QueryReplaySummary,
  QueryReplayResult,
  BaselineQuery,
} from '../types';

// Pipeline phases (original order: baseline first from source, then clone)
// In-place: baseline → clone → upgrade → validation → report
// Blue/Green: baseline → clone → upgrade+switchover → validation → report
const PHASES: UpgradeTestPhase[] = [
  'baseline', 'clone', 'upgrade', 'validation', 'report',
];

const PHASE_LABELS: Record<string, string> = {
  baseline:      '1. Capture Baseline',
  clone:         '2. Clone Source Cluster',
  upgrade:       '3. Upgrade Cloned Cluster',
  validation:    '4. Post-Upgrade Validation',
  report:        '5. Generate Report',
  complete: 'Complete',
  failed:   'Failed',
};

// Strategy-specific phase labels
const PHASE_LABELS_BG: Record<string, string> = {
  baseline:      '1. Capture Baseline',
  clone:         '2. Green Environment',
  upgrade:       '3. Upgrade + Switchover',
  validation:    '4. Post-Upgrade Validation',
  report:        '5. Generate Report',
  complete: 'Complete',
  failed:   'Failed',
};

const PHASE_DESCRIPTIONS: Record<string, string> = {
  baseline:      'Collecting top queries from performance_schema (MySQL) or pg_stat_statements (PostgreSQL) and parameter group settings',
  clone:         'Creating a point-in-time clone of your source cluster — your production database is never modified',
  upgrade:       'Upgrading the cloned cluster to the target version',
  validation:    'Replaying baseline queries post-upgrade to detect failures and latency regressions',
  report:        'Compiling final report with action items and optimization suggestions',
};

// Cumulative ETA from run start (minutes)
const PHASE_ETA_MINUTES_INPLACE: Record<string, number> = {
  baseline: 2, clone: 10, upgrade: 28, validation: 33, report: 36,
};
const PHASE_ETA_MINUTES_BG: Record<string, number> = {
  baseline: 2, clone: 16, upgrade: 42, validation: 47, report: 50,
};

function formatDuration(seconds: number): string {
  if (seconds < 60) return `${seconds}s`;
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return s > 0 ? `${m}m ${s}s` : `${m}m`;
}

function formatTs(iso: string): string {
  return new Date(iso).toLocaleString('en-US', {
    month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit', hour12: true,
  });
}

function strategyBadge(strategy: string) {
  return strategy === 'blue-green'
    ? <Badge color="green">Blue/Green</Badge>
    : <Badge color="blue">In-place</Badge>;
}

function riskBadge(risk?: string) {
  if (!risk) return null;
  const color = risk === 'CRITICAL' || risk === 'HIGH' ? 'red'
    : risk === 'MEDIUM' ? 'grey' : 'green';
  return <Badge color={color}>{risk}</Badge>;
}

function statusIcon(status: string) {
  if (status === 'pass') return <StatusIndicator type="success">Pass</StatusIndicator>;
  if (status === 'fail') return <StatusIndicator type="error">Fail</StatusIndicator>;
  return <StatusIndicator type="stopped">Skipped</StatusIndicator>;
}

// ---------------------------------------------------------------------------
// Baseline Queries panel
// ---------------------------------------------------------------------------
function BaselineQueriesPanel({ queries, querySource }: {
  queries: BaselineQuery[];
  querySource: string;
}) {
  if (!queries.length) {
    return (
      <Alert type="info">
        No query baseline collected. Set <code>DB_SECRET_ARN</code> on the baseline Lambda
        to enable live collection from performance_schema / pg_stat_statements and
        post-upgrade query replay.
      </Alert>
    );
  }
  return (
    <SpaceBetween size="s">
      <Box variant="small" color="text-body-secondary">
        Source: <strong>{querySource}</strong> · {queries.length} queries
      </Box>
      <Table
        columnDefinitions={[
          { id: 'digest', header: 'Query Digest', cell: (q: BaselineQuery) => (
            <Box variant="code" fontSize="body-s">{q.digest.slice(0, 120)}{q.digest.length > 120 ? '…' : ''}</Box>
          ), width: 420 },
          { id: 'schema', header: 'Schema', cell: (q: BaselineQuery) => q.schema || '—' },
          { id: 'calls', header: 'Calls', cell: (q: BaselineQuery) => q.callCount.toLocaleString() },
          { id: 'avg', header: 'Avg Latency', cell: (q: BaselineQuery) => `${q.avgLatencyMs} ms` },
          { id: 'max', header: 'Max Latency', cell: (q: BaselineQuery) => `${q.maxLatencyMs} ms` },
        ]}
        items={queries}
        variant="embedded"
        stripedRows
      />
    </SpaceBetween>
  );
}

// ---------------------------------------------------------------------------
// Query Replay panel
// ---------------------------------------------------------------------------
function QueryReplayPanel({ replay, label }: { replay: QueryReplaySummary; label: string }) {
  const [expanded, setExpanded] = useState(false);
  const failures = replay.results.filter(r => r.status === 'fail');

  return (
    <SpaceBetween size="m">
      <ColumnLayout columns={5} variant="text-grid">
        <div><Box variant="awsui-key-label">Total</Box><Box fontWeight="bold">{replay.totalQueries}</Box></div>
        <div><Box variant="awsui-key-label">Passed</Box><Box fontWeight="bold" color="text-status-success">{replay.passed}</Box></div>
        <div>
          <Box variant="awsui-key-label">Failed</Box>
          <Box fontWeight="bold" color={replay.failed > 0 ? 'text-status-error' : undefined}>{replay.failed}</Box>
        </div>
        <div><Box variant="awsui-key-label">Skipped</Box><Box fontWeight="bold">{replay.skipped}</Box></div>
        {label === 'During Upgrade' && (
          <div>
            <Box variant="awsui-key-label">DB Outage</Box>
            <Box fontWeight="bold" color={replay.totalOutageSeconds > 0 ? 'text-status-error' : 'text-status-success'}>
              {replay.totalOutageSeconds > 0 ? `${replay.totalOutageSeconds}s` : 'None'}
            </Box>
          </div>
        )}
      </ColumnLayout>

      {failures.length > 0 && (
        <ExpandableSection headerText={`${failures.length} Failed Queries`} variant="footer">
          <Table
            columnDefinitions={[
              { id: 'digest', header: 'Query', cell: (r: QueryReplayResult) => (
                <Box variant="code" fontSize="body-s">{r.digest.slice(0, 150)}{r.digest.length > 150 ? '…' : ''}</Box>
              ), width: 380 },
              { id: 'status', header: 'Status', cell: (r: QueryReplayResult) => statusIcon(r.status) },
              { id: 'latency', header: 'Latency', cell: (r: QueryReplayResult) => `${r.latencyMs} ms` },
              { id: 'error', header: 'Error', cell: (r: QueryReplayResult) => (
                <Box variant="small" color="text-status-error">{r.error.slice(0, 200)}</Box>
              )},
            ]}
            items={failures}
            variant="embedded"
            stripedRows
          />
        </ExpandableSection>
      )}

      <ExpandableSection
        headerText={`All ${replay.results.length} Results`}
        variant="footer"
        expanded={expanded}
        onChange={({ detail }) => setExpanded(detail.expanded)}
      >
        <Table
          columnDefinitions={[
            { id: 'digest', header: 'Query', cell: (r: QueryReplayResult) => (
              <Box variant="code" fontSize="body-s">{r.digest.slice(0, 120)}{r.digest.length > 120 ? '…' : ''}</Box>
            ), width: 380 },
            { id: 'status', header: 'Status', cell: (r: QueryReplayResult) => statusIcon(r.status) },
            { id: 'latency', header: 'Latency', cell: (r: QueryReplayResult) => `${r.latencyMs} ms` },
            { id: 'error', header: 'Error', cell: (r: QueryReplayResult) => r.error
              ? <Box variant="small" color="text-status-error">{r.error.slice(0, 120)}</Box>
              : '—'
            },
          ]}
          items={replay.results}
          variant="embedded"
          stripedRows
        />
      </ExpandableSection>
    </SpaceBetween>
  );
}

// ---------------------------------------------------------------------------
// Main page
// ---------------------------------------------------------------------------
export default function UpgradeTest() {
  const { runId } = useParams<{ runId: string }>();
  const navigate = useNavigate();
  const [run, setRun] = useState<UpgradeTestRun | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [elapsed, setElapsed] = useState(0);
  const [showDeleteModal, setShowDeleteModal] = useState(false);
  const [deleting, setDeleting] = useState(false);

  // Per-phase elapsed time — driven by server-provided phaseStartTime
  const [phaseElapsed, setPhaseElapsed] = useState<Record<string, number>>({});

  // Track the last known phase start time from the server
  const phaseStartTimeRef = useRef<{ phase: string; startTime: number } | null>(null);

  const [duringReplay, setDuringReplay] = useState<QueryReplaySummary | null>(null);
  const [postReplay, setPostReplay] = useState<QueryReplaySummary | null>(null);
  const [replayLoading, setReplayLoading] = useState(false);

  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const fetchRun = useCallback(async () => {
    if (!runId) return;
    try {
      const data = await getUpgradeTestRun(runId);
      setRun(data);

      // Use server-provided phaseStartTime for accurate elapsed tracking
      if (data.phaseStartTime && data.status === 'running') {
        const serverStartMs = new Date(data.phaseStartTime).getTime();
        if (
          !phaseStartTimeRef.current ||
          phaseStartTimeRef.current.phase !== data.phase ||
          phaseStartTimeRef.current.startTime !== serverStartMs
        ) {
          phaseStartTimeRef.current = { phase: data.phase, startTime: serverStartMs };
        }
      }

      if (data.status === 'complete' || data.status === 'failed') {
        if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; }
        if (timerRef.current) { clearInterval(timerRef.current); timerRef.current = null; }
        if (data.durationSeconds) setElapsed(data.durationSeconds);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load run');
    } finally {
      setLoading(false);
    }
  }, [runId]);

  const fetchReplayData = useCallback(async () => {
    if (!runId) return;
    setReplayLoading(true);
    try {
      const [during, post] = await Promise.all([
        getUpgradeTestReplay(runId, 'during_upgrade'),
        getUpgradeTestReplay(runId, 'post_upgrade'),
      ]);
      setDuringReplay(during);
      setPostReplay(post);
    } catch { /* best-effort */ } finally {
      setReplayLoading(false);
    }
  }, [runId]);

  useEffect(() => {
    fetchRun();
    pollRef.current = setInterval(fetchRun, 8000);
    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
      if (timerRef.current) clearInterval(timerRef.current);
    };
  }, [fetchRun]);

  useEffect(() => {
    if (run?.status === 'complete') fetchReplayData();
  }, [run?.status, fetchReplayData]);

  // Live elapsed timer (total + per-phase)
  useEffect(() => {
    if (!run || run.status !== 'running') return;
    const startMs = new Date(run.startTime).getTime();
    timerRef.current = setInterval(() => {
      const now = Date.now();
      setElapsed(Math.floor((now - startMs) / 1000));
      if (phaseStartTimeRef.current) {
        const { phase, startTime } = phaseStartTimeRef.current;
        setPhaseElapsed(prev => ({
          ...prev,
          [phase]: Math.floor((now - startTime) / 1000),
        }));
      }
    }, 1000);
    return () => { if (timerRef.current) clearInterval(timerRef.current); };
  }, [run?.status, run?.startTime]);

  if (loading) return <Box textAlign="center" padding="xxl"><Spinner size="large" /></Box>;
  if (error || !run) return <Alert type="error">{error ?? 'Run not found'}</Alert>;

  const isBlueGreen = run.upgradeStrategy === 'blue-green';
  const activePhases = PHASES;
  const phaseLabels = isBlueGreen ? PHASE_LABELS_BG : PHASE_LABELS;
  const phaseEta = isBlueGreen ? PHASE_ETA_MINUTES_BG : PHASE_ETA_MINUTES_INPLACE;

  const currentPhaseIdx = activePhases.indexOf(run.phase as UpgradeTestPhase);
  const pct = run.status === 'complete' ? 100
    : run.status === 'failed' ? 0
    : currentPhaseIdx < 0 ? 0
    : Math.round(((currentPhaseIdx + 0.5) / activePhases.length) * 100);
  const clusterId = run.clusterArn?.split(':').pop() ?? run.clusterArn;
  const etaMinutes = phaseEta[run.phase] ?? (isBlueGreen ? 52 : 38);
  const remainingMinutes = Math.max(0, etaMinutes - Math.floor(elapsed / 60));

  // Total downtime = outage during upgrade + cutover time
  const totalDowntimeSeconds = (run.summary?.outageSeconds ?? 0) + (run.summary?.cutoverSeconds ?? 0);

  return (
    <SpaceBetween size="l">
      {/* Delete modal */}
      {showDeleteModal && (
        <Modal
          visible
          onDismiss={() => setShowDeleteModal(false)}
          header="Delete Upgrade Test"
          footer={
            <Box float="right">
              <SpaceBetween direction="horizontal" size="xs">
                <Button variant="link" onClick={() => setShowDeleteModal(false)} disabled={deleting}>Cancel</Button>
                <Button variant="primary" loading={deleting} onClick={async () => {
                  setDeleting(true);
                  try {
                    await deleteUpgradeTestRun(run.runId);
                    navigate('/upgrade-tests');
                  } catch (e) {
                    setError(e instanceof Error ? e.message : 'Delete failed');
                    setShowDeleteModal(false);
                  } finally { setDeleting(false); }
                }}>Delete</Button>
              </SpaceBetween>
            </Box>
          }
        >
          <SpaceBetween size="m">
            <Box>Permanently delete this run and its AWS resources:</Box>
            <SpaceBetween size="xs">
              {run.snapshotId && <Box>• Snapshot: <Box variant="span" fontWeight="bold">{run.snapshotId}</Box></Box>}
              {run.clonedClusterId && <Box>• Clone: <Box variant="span" fontWeight="bold">{run.clonedClusterId}</Box></Box>}
            </SpaceBetween>
            {run.snapshotId && <Alert type="warning">Deleting the snapshot removes your rollback point.</Alert>}
          </SpaceBetween>
        </Modal>
      )}

      {/* Header */}
      <Container
        header={
          <Header
            variant="h2"
            description={`${run.clusterArn?.split(':')[3] ?? ''} · ${run.upgradeStrategy}`}
            actions={
              <SpaceBetween direction="horizontal" size="xs">
                <Button
                  variant="icon"
                  iconName="angle-left"
                  ariaLabel="All Upgrade Tests"
                  onClick={() => navigate('/upgrade-tests')}
                />
                <Button
                  variant="icon"
                  iconName="view-full"
                  ariaLabel="Database List"
                  onClick={() => navigate('/databases')}
                />
                {run.status !== 'running' && (
                  <Button
                    variant="icon"
                    iconName="remove"
                    ariaLabel="Delete"
                    onClick={() => setShowDeleteModal(true)}
                  />
                )}
                {run.reportUrl && (
                  <Button
                    variant="icon"
                    iconName="download"
                    ariaLabel="Download Report"
                    href={run.reportUrl}
                    target="_blank"
                  />
                )}
              </SpaceBetween>
            }
          >
            {clusterId}
          </Header>
        }
      >
        <SpaceBetween size="l">
          <Box variant="p" color="text-body-secondary">
            Running a safe upgrade test on a cloned copy of your cluster. The agent captures a baseline of your top queries, checks compatibility against the deprecation catalog, clones your cluster, upgrades the clone, replays your queries to measure downtime and failures, then generates a readiness report. Your production database is never modified.
          </Box>
          <Box variant="small" color="text-body-secondary">
            The progress bar below tracks which step the agent is currently executing. Each step runs sequentially — the percentage reflects how many steps have completed out of the total.
          </Box>
          <Box variant="small" color="text-body-secondary">Cluster ARN: {run.clusterArn}</Box>
          <ColumnLayout columns={7} variant="text-grid">
            <div>
              <Box variant="awsui-key-label">Status</Box>
              {run.status === 'complete'
                ? <StatusIndicator type="success">Complete</StatusIndicator>
                : run.status === 'failed'
                ? <StatusIndicator type="error">Failed</StatusIndicator>
                : <StatusIndicator type="in-progress">Running</StatusIndicator>}
            </div>
            <div>
              <Box variant="awsui-key-label">Engine</Box>
              <Box>{run.engine ?? '—'}</Box>
            </div>
            <div>
              <Box variant="awsui-key-label">Source Version</Box>
              <Box fontWeight="bold">{run.sourceVersion ?? '—'}</Box>
            </div>
            <div>
              <Box variant="awsui-key-label">Target Version</Box>
              <Box fontWeight="bold">{run.targetVersion}</Box>
            </div>
            <div>
              <Box variant="awsui-key-label">Strategy</Box>
              {strategyBadge(run.upgradeStrategy)}
            </div>
            <div>
              <Box variant="awsui-key-label">Started</Box>
              <Box variant="small">{formatTs(run.startTime)}</Box>
            </div>
            <div>
              <Box variant="awsui-key-label">{run.status === 'running' ? 'Elapsed / ETA' : 'Duration'}</Box>
              <Box>
                {run.status === 'running'
                  ? <><Box variant="span" fontWeight="bold">{formatDuration(elapsed)}</Box>
                     <Box variant="span" color="text-body-secondary"> / ~{remainingMinutes}m left</Box></>
                  : formatDuration(run.durationSeconds ?? elapsed)}
              </Box>
            </div>
          </ColumnLayout>

          {run.status === 'running' && (
            <ProgressBar
              value={pct}
              status="in-progress"
              label={`Step ${currentPhaseIdx + 1} of ${activePhases.length}: ${phaseLabels[run.phase] ?? run.phase}`}
              additionalInfo={PHASE_DESCRIPTIONS[run.phase] ?? ''}
            />
          )}
          {run.status === 'complete' && (
            <ProgressBar value={100} status="success" label={`All ${activePhases.length} steps completed`} />
          )}
          {run.status === 'failed' && run.errorMessage && (
            <Alert type="error" header="Upgrade test failed">{run.errorMessage}</Alert>
          )}
        </SpaceBetween>
      </Container>

      {/* Step Progress with timing */}
      <Container header={<Header variant="h3">Step Progress</Header>}>
        <SpaceBetween size="s">
          {/* Column headers */}
          <Box padding={{ left: 's' }}>
            <ColumnLayout columns={4} variant="text-grid">
              <div><Box variant="small" fontWeight="bold" color="text-body-secondary">Step</Box></div>
              <div><Box variant="small" fontWeight="bold" color="text-body-secondary">Description</Box></div>
              <div><Box variant="small" fontWeight="bold" color="text-body-secondary">ETA</Box></div>
              <div><Box variant="small" fontWeight="bold" color="text-body-secondary">Actual</Box></div>
            </ColumnLayout>
          </Box>
          {activePhases.map((phase, idx) => {
            // A step is done if: run is complete, OR a later phase has started (phaseTiming exists for a subsequent phase)
            const hasLaterPhaseStarted = activePhases.slice(idx + 1).some(p => run.phaseTiming?.[p]);
            const isDone = run.status === 'complete' || hasLaterPhaseStarted;
            const isCurrent = !isDone && run.phase === phase && run.status === 'running';
            const isFailed = !isDone && run.status === 'failed' && run.phase === phase;

            // ETA for this specific step
            const prevPhase = idx > 0 ? activePhases[idx - 1] : undefined;
            const prevEta = prevPhase ? (phaseEta[prevPhase] ?? 0) : 0;
            const thisEta = phaseEta[phase] ?? 0;
            const stepEtaMinutes = Math.max(1, thisEta - prevEta);
            const etaLabel = `~${stepEtaMinutes}m`;

            // Actual duration — only show for completed steps
            let actualLabel = '—';
            if (isDone) {
              const timing = run.phaseTiming?.[phase];
              if (timing) {
                // Find the next phase that has timing data
                const nextWithTiming = activePhases.slice(idx + 1).find(p => run.phaseTiming?.[p]);
                if (nextWithTiming) {
                  const nextTiming = run.phaseTiming![nextWithTiming];
                  const dur = (nextTiming.elapsedAtStart ?? 0) - (timing.elapsedAtStart ?? 0);
                  actualLabel = formatDuration(Math.max(0, dur));
                } else if (run.status === 'complete' && run.durationSeconds != null) {
                  // Last completed phase — use total duration minus phase start
                  const dur = run.durationSeconds - (timing.elapsedAtStart ?? 0);
                  actualLabel = formatDuration(Math.max(0, dur));
                } else {
                  actualLabel = '✓';
                }
              } else {
                actualLabel = '✓';
              }
            } else if (isCurrent) {
              // Show live elapsed for current step
              const stepElapsed = phaseElapsed[phase];
              actualLabel = stepElapsed ? formatDuration(stepElapsed) : '...';
            }

            return (
              <Box key={phase} padding={{ left: 's' }}>
                <ColumnLayout columns={4} variant="text-grid">
                  <div>
                    {isDone
                      ? <StatusIndicator type="success">{phaseLabels[phase]}</StatusIndicator>
                      : isFailed
                      ? <StatusIndicator type="error">{phaseLabels[phase]}</StatusIndicator>
                      : isCurrent
                      ? <StatusIndicator type="in-progress">{phaseLabels[phase]}</StatusIndicator>
                      : <StatusIndicator type="pending">{phaseLabels[phase]}</StatusIndicator>}
                  </div>
                  <div>
                    <Box variant="small" color="text-body-secondary">
                      {isCurrent ? PHASE_DESCRIPTIONS[phase] : ''}
                    </Box>
                  </div>
                  <div>
                    <Box variant="small" color="text-body-secondary">{etaLabel}</Box>
                  </div>
                  <div>
                    <Box variant="small" color={isDone ? 'text-status-success' : isCurrent ? 'text-status-info' : 'text-body-secondary'}>
                      {actualLabel}
                    </Box>
                  </div>
                </ColumnLayout>
              </Box>
            );
          })}
        </SpaceBetween>
      </Container>

      {/* Resources */}
      {run.clonedClusterId && (
        <Container header={<Header variant="h3">Resources Created</Header>}>
          <ColumnLayout columns={2} variant="text-grid">
            <div>
              <Box variant="awsui-key-label">Test Clone / Green Cluster</Box>
              <Box variant="small" fontWeight="bold">{run.clonedClusterId}</Box>
              <Box variant="small" color="text-body-secondary">
                {run.status === 'complete' ? 'Deleted after test' : 'Isolated test environment'}
              </Box>
            </div>
            {run.snapshotId && (
              <div>
                <Box variant="awsui-key-label">Snapshot</Box>
                <Box variant="small" fontWeight="bold">{run.snapshotId}</Box>
                <Box variant="small" color="text-body-secondary">Rollback point</Box>
              </div>
            )}
          </ColumnLayout>
        </Container>
      )}

      {/* Results summary */}
      {run.status === 'complete' && run.summary && (
        <Container header={<Header variant="h3">Results Summary</Header>}>
          <SpaceBetween size="l">
            {/* Total downtime callout */}
            {totalDowntimeSeconds > 0 && (
              <Alert
                type={totalDowntimeSeconds < 30 ? 'success' : totalDowntimeSeconds < 120 ? 'warning' : 'error'}
                header={`Total database downtime: ${formatDuration(totalDowntimeSeconds)}`}
              >
                {run.upgradeStrategy === 'blue-green'
                  ? `Switchover cutover: ${run.summary.cutoverSeconds ?? 0}s`
                  : `Query outage window during upgrade: ${run.summary.outageSeconds ?? 0}s`}
              </Alert>
            )}
            {totalDowntimeSeconds === 0 && run.summary.upgradeSucceeded && (
              <Alert type="success" header="No database downtime detected">
                All baseline queries continued to pass throughout the upgrade window.
              </Alert>
            )}

            <ColumnLayout columns={5} variant="text-grid">
              <div>
                <Box variant="awsui-key-label">Compat Issues</Box>
                <Box fontWeight="bold" color={run.summary.compatibilityFindings > 0 ? 'text-status-warning' : undefined}>
                  {run.summary.compatibilityFindings}
                </Box>
              </div>
              <div>
                <Box variant="awsui-key-label">Query Failures</Box>
                <Box fontWeight="bold" color={(run.summary.queryFailures ?? 0) > 0 ? 'text-status-error' : undefined}>
                  {run.summary.queryFailures ?? 0}
                </Box>
              </div>
              <div>
                <Box variant="awsui-key-label">Latency Regressions</Box>
                <Box fontWeight="bold" color={(run.summary.queryRegressions ?? 0) > 0 ? 'text-status-warning' : undefined}>
                  {run.summary.queryRegressions ?? 0}
                </Box>
              </div>
              <div>
                <Box variant="awsui-key-label">Param Regressions</Box>
                <Box fontWeight="bold" color={(run.summary.regressions ?? 0) > 0 ? 'text-status-warning' : undefined}>
                  {run.summary.regressions ?? 0}
                </Box>
              </div>
              <div>
                <Box variant="awsui-key-label">Risk Level</Box>
                {riskBadge(run.summary.riskLevel)}
              </div>
            </ColumnLayout>

            {run.reportUrl && (
              <Button
                href={run.reportUrl}
                target="_blank"
                iconName="download"
                iconAlign="right"
                variant="primary"
              >
                Download Readiness Report
              </Button>
            )}
          </SpaceBetween>
        </Container>
      )}

      {/* Query Workload Analysis */}
      {run.status === 'complete' && (
        <Container header={<Header variant="h3">Query Workload Analysis</Header>}>
          {replayLoading ? (
            <Box textAlign="center" padding="l"><Spinner /> Loading query replay data...</Box>
          ) : (
            <Tabs
              tabs={[
                {
                  id: 'baseline',
                  label: 'Baseline Queries',
                  content: (
                    <Box padding={{ top: 'm' }}>
                      <BaselineQueriesPanel queries={[]} querySource="See baseline.json in S3 report" />
                    </Box>
                  ),
                },
                {
                  id: 'during',
                  label: duringReplay
                    ? `During Upgrade (${duringReplay.failed} fail · ${duringReplay.totalOutageSeconds}s outage)`
                    : 'During Upgrade',
                  content: (
                    <Box padding={{ top: 'm' }}>
                      {duringReplay ? (
                        <QueryReplayPanel replay={duringReplay} label="During Upgrade" />
                      ) : (
                        <Alert type="info">
                          No upgrade-window replay data available for this run.
                        </Alert>
                      )}
                    </Box>
                  ),
                },
                {
                  id: 'post',
                  label: postReplay ? `Post-Upgrade (${postReplay.failed} fail)` : 'Post-Upgrade',
                  content: (
                    <Box padding={{ top: 'm' }}>
                      {postReplay ? (
                        <QueryReplayPanel replay={postReplay} label="Post-Upgrade" />
                      ) : (
                        <Alert type="info">
                          No post-upgrade replay data available for this run.
                        </Alert>
                      )}
                    </Box>
                  ),
                },
                ...(postReplay?.regressions?.length ? [{
                  id: 'regressions',
                  label: `Latency Regressions (${postReplay.regressions!.length})`,
                  content: (
                    <Box padding={{ top: 'm' }}>
                      <Table
                        columnDefinitions={[
                          { id: 'digest', header: 'Query', cell: (r: any) => (
                            <Box variant="code" fontSize="body-s">{r.digest?.slice(0, 120)}</Box>
                          ), width: 360 },
                          { id: 'before', header: 'Before', cell: (r: any) => `${r.beforeLatencyMs} ms` },
                          { id: 'after', header: 'After', cell: (r: any) => `${r.afterLatencyMs} ms` },
                          { id: 'pct', header: 'Regression', cell: (r: any) => (
                            <Box color="text-status-error">+{r.regressionPct}%</Box>
                          )},
                          { id: 'sev', header: 'Severity', cell: (r: any) => (
                            <Badge color={r.severity === 'HIGH' ? 'red' : 'grey'}>{r.severity}</Badge>
                          )},
                          { id: 'fix', header: 'Fix', cell: (r: any) => <Box variant="small">{r.fix}</Box> },
                        ]}
                        items={postReplay.regressions!}
                        variant="embedded"
                        stripedRows
                      />
                    </Box>
                  ),
                }] : []),
              ]}
            />
          )}
        </Container>
      )}
    </SpaceBetween>
  );
}
