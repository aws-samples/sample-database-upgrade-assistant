import { useEffect, useState, useRef, useCallback } from 'react';
import { useNavigate } from 'react-router-dom';
import Table from '@cloudscape-design/components/table';
import Header from '@cloudscape-design/components/header';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Box from '@cloudscape-design/components/box';
import Badge from '@cloudscape-design/components/badge';
import Button from '@cloudscape-design/components/button';
import StatusIndicator from '@cloudscape-design/components/status-indicator';
import TextFilter from '@cloudscape-design/components/text-filter';
import ProgressBar from '@cloudscape-design/components/progress-bar';
import Modal from '@cloudscape-design/components/modal';
import Alert from '@cloudscape-design/components/alert';
import { getUpgradeTestRuns, deleteUpgradeTestRun } from '../services/api';
import type { UpgradeTestRun, UpgradeTestPhase } from '../types';

const PHASES: UpgradeTestPhase[] = [
  'snapshot', 'baseline', 'compatibility', 'clone', 'upgrade', 'validation', 'report',
];

const PHASE_LABELS: Record<string, string> = {
  snapshot: 'Snapshot', baseline: 'Baseline', compatibility: 'Compatibility',
  clone: 'Clone', upgrade: 'Upgrade', validation: 'Validation', report: 'Report',
  complete: 'Complete', failed: 'Failed',
};

function phaseProgress(phase: string, status: string): number {
  if (status === 'complete') return 100;
  if (status === 'failed') return 0;
  const idx = PHASES.indexOf(phase as UpgradeTestPhase);
  if (idx < 0) return 0;
  return Math.round(((idx + 0.5) / PHASES.length) * 100);
}

function formatTs(iso: string): string {
  return new Date(iso).toLocaleString('en-US', {
    month: 'short', day: 'numeric', year: 'numeric',
    hour: 'numeric', minute: '2-digit', hour12: true,
  });
}

function formatDuration(seconds?: number): string {
  if (!seconds) return '—';
  if (seconds < 60) return `${seconds}s`;
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return s > 0 ? `${m}m ${s}s` : `${m}m`;
}

function statusCell(run: UpgradeTestRun) {
  if (run.status === 'complete') return <StatusIndicator type="success">Complete</StatusIndicator>;
  if (run.status === 'failed') return <StatusIndicator type="error">Failed</StatusIndicator>;
  if (run.status === 'running') {
    const pct = phaseProgress(run.phase, run.status);
    return (
      <SpaceBetween size="xxs">
        <StatusIndicator type="in-progress">
          {PHASE_LABELS[run.phase] ?? run.phase} ({pct}%)
        </StatusIndicator>
        <ProgressBar value={pct} status="in-progress" />
      </SpaceBetween>
    );
  }
  return <StatusIndicator type="pending">Pending</StatusIndicator>;
}

function strategyBadge(strategy: string) {
  return strategy === 'blue-green'
    ? <Badge color="green">Blue/Green</Badge>
    : <Badge color="blue">In-place</Badge>;
}

function riskBadge(risk?: string) {
  if (!risk) return <Box color="text-body-secondary">—</Box>;
  const color = risk === 'CRITICAL' || risk === 'HIGH' ? 'red'
    : risk === 'MEDIUM' ? 'grey' : 'green';
  return <Badge color={color}>{risk}</Badge>;
}

export default function UpgradeTestList() {
  const navigate = useNavigate();
  const [runs, setRuns] = useState<UpgradeTestRun[]>([]);
  const [loading, setLoading] = useState(true);
  const [filterText, setFilterText] = useState('');
  const [deleteTarget, setDeleteTarget] = useState<UpgradeTestRun | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const fetchRuns = useCallback(async () => {
    try {
      const data = await getUpgradeTestRuns();
      // Sort newest first
      data.sort((a, b) => new Date(b.startTime).getTime() - new Date(a.startTime).getTime());
      setRuns(data);
    } catch {
      // silent
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchRuns();
    // Poll every 10s if any runs are active
    pollRef.current = setInterval(() => {
      fetchRuns();
    }, 10000);
    return () => { if (pollRef.current) clearInterval(pollRef.current); };
  }, [fetchRuns]);

  const handleDelete = async () => {
    if (!deleteTarget) return;
    setDeleting(true);
    setDeleteError(null);
    try {
      await deleteUpgradeTestRun(deleteTarget.runId);
      setDeleteTarget(null);
      await fetchRuns();
    } catch (e) {
      setDeleteError(e instanceof Error ? e.message : 'Delete failed');
    } finally {
      setDeleting(false);
    }
  };

  const filtered = runs.filter(r => {
    if (!filterText) return true;
    const q = filterText.toLowerCase();
    return r.clusterArn?.toLowerCase().includes(q) ||
      r.targetVersion?.toLowerCase().includes(q) ||
      r.status?.toLowerCase().includes(q) ||
      r.upgradeStrategy?.toLowerCase().includes(q);
  });

  const running = runs.filter(r => r.status === 'running').length;
  const complete = runs.filter(r => r.status === 'complete').length;
  const failed = runs.filter(r => r.status === 'failed').length;

  return (
    <SpaceBetween size="l">
      {/* Delete confirmation modal */}
      {deleteTarget && (
        <Modal
          visible
          onDismiss={() => { setDeleteTarget(null); setDeleteError(null); }}
          header="Delete Upgrade Test"
          footer={
            <Box float="right">
              <SpaceBetween direction="horizontal" size="xs">
                <Button variant="link" onClick={() => { setDeleteTarget(null); setDeleteError(null); }} disabled={deleting}>
                  Cancel
                </Button>
                <Button variant="primary" onClick={handleDelete} loading={deleting}>
                  Delete
                </Button>
              </SpaceBetween>
            </Box>
          }
        >
          <SpaceBetween size="m">
            {deleteError && <Alert type="error">{deleteError}</Alert>}
            <Box>
              This will permanently delete:
            </Box>
            <SpaceBetween size="xs">
              <Box>• Run record for <Box variant="span" fontWeight="bold">{deleteTarget.clusterArn?.split(':').pop()}</Box></Box>
              {deleteTarget.snapshotId && (
                <Box>• Snapshot: <Box variant="span" fontWeight="bold">{deleteTarget.snapshotId}</Box></Box>
              )}
              {deleteTarget.clonedClusterId && (
                <Box>• Clone cluster: <Box variant="span" fontWeight="bold">{deleteTarget.clonedClusterId}</Box></Box>
              )}
            </SpaceBetween>
            {deleteTarget.snapshotId && (
              <Alert type="warning">
                Deleting the snapshot removes your rollback point for this upgrade test.
              </Alert>
            )}
          </SpaceBetween>
        </Modal>
      )}
      {/* Stats bar */}
      {runs.length > 0 && (
        <Box>
          <SpaceBetween direction="horizontal" size="l">
            <Box>
              <Box variant="awsui-key-label">Total Runs</Box>
              <Box variant="awsui-value-large">{runs.length}</Box>
            </Box>
            <Box>
              <Box variant="awsui-key-label">Running</Box>
              <Box variant="awsui-value-large" color={running > 0 ? 'text-status-info' : undefined}>{running}</Box>
            </Box>
            <Box>
              <Box variant="awsui-key-label">Complete</Box>
              <Box variant="awsui-value-large" color={complete > 0 ? 'text-status-success' : undefined}>{complete}</Box>
            </Box>
            <Box>
              <Box variant="awsui-key-label">Failed</Box>
              <Box variant="awsui-value-large" color={failed > 0 ? 'text-status-error' : undefined}>{failed}</Box>
            </Box>
          </SpaceBetween>
        </Box>
      )}

      <Table
        header={
          <Header
            variant="h2"
            counter={loading ? undefined : `(${filtered.length})`}
            actions={
              <SpaceBetween direction="horizontal" size="xs">
                <Button iconName="refresh" onClick={fetchRuns} disabled={loading}>Refresh</Button>
                <Button variant="primary" onClick={() => navigate('/databases')}>
                  New Upgrade Test
                </Button>
              </SpaceBetween>
            }
          >
            Upgrade Test Runs
          </Header>
        }
        filter={
          <TextFilter
            filteringText={filterText}
            filteringPlaceholder="Filter by cluster, version, status, or strategy"
            onChange={({ detail }) => setFilterText(detail.filteringText)}
          />
        }
        columnDefinitions={[
          {
            id: 'cluster',
            header: 'Cluster',
            cell: (r) => (
              <Button variant="inline-link" onClick={() => navigate(`/upgrade-test/${r.runId}`)}>
                {r.clusterArn?.split(':').pop() ?? r.clusterArn}
              </Button>
            ),
          },
          {
            id: 'target',
            header: 'Target',
            cell: (r) => <Box variant="small" fontWeight="bold">{r.targetVersion}</Box>,
            width: 140,
          },
          {
            id: 'strategy',
            header: 'Strategy',
            cell: (r) => strategyBadge(r.upgradeStrategy),
            width: 100,
          },
          {
            id: 'status',
            header: 'Status',
            cell: (r) => statusCell(r),
            width: 200,
          },
          {
            id: 'verdict',
            header: 'Readiness',
            cell: (r) => {
              if (r.status !== 'complete' || !r.summary?.verdict) return <Box color="text-body-secondary">—</Box>;
              const v = r.summary.verdict;
              if (v === 'READY') return <Badge color="green">Go</Badge>;
              if (v === 'NOT_READY') return <Badge color="red">No-Go</Badge>;
              return <Badge color="grey">Conditional</Badge>;
            },
            width: 100,
          },
          {
            id: 'risk',
            header: 'Risk',
            cell: (r) => riskBadge(r.summary?.riskLevel),
            width: 80,
          },
          {
            id: 'started',
            header: 'Started',
            cell: (r) => <Box variant="small">{formatTs(r.startTime)}</Box>,
            width: 130,
          },
          {
            id: 'duration',
            header: 'Duration',
            cell: (r) => <Box variant="small">{formatDuration(r.durationSeconds)}</Box>,
            width: 80,
          },
          {
            id: 'actions',
            header: '',
            cell: (r) => (
              <SpaceBetween direction="horizontal" size="xs">
                {r.reportUrl && (
                  <Button variant="inline-link" href={r.reportUrl} target="_blank" iconName="download" ariaLabel="Report" />
                )}
                <Button
                  variant="inline-link"
                  onClick={() => setDeleteTarget(r)}
                  disabled={r.status === 'running'}
                  iconName="remove"
                  ariaLabel="Delete"
                />
              </SpaceBetween>
            ),
            width: 70,
          },
        ]}
        items={filtered}
        loading={loading}
        loadingText="Loading upgrade test runs..."
        empty={
          <Box textAlign="center" color="text-body-secondary" padding="xl">
            No upgrade tests yet. Go to Database List and click "Run Upgrade Test" on a cluster.
          </Box>
        }
        variant="container"
        sortingDisabled={false}
      />
    </SpaceBetween>
  );
}
