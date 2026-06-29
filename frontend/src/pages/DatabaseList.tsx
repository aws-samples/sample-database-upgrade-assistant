import { useState, useEffect, useCallback } from 'react';
import { useNavigate } from 'react-router-dom';
import Container from '@cloudscape-design/components/container';
import Header from '@cloudscape-design/components/header';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Table from '@cloudscape-design/components/table';
import Badge from '@cloudscape-design/components/badge';
import Box from '@cloudscape-design/components/box';
import Button from '@cloudscape-design/components/button';
import Input from '@cloudscape-design/components/input';
import FormField from '@cloudscape-design/components/form-field';
import StatusIndicator from '@cloudscape-design/components/status-indicator';
import Alert from '@cloudscape-design/components/alert';
import Popover from '@cloudscape-design/components/popover';
import ColumnLayout from '@cloudscape-design/components/column-layout';
import TextFilter from '@cloudscape-design/components/text-filter';
import Pagination from '@cloudscape-design/components/pagination';
import { getDatabases, getSettings } from '../services/api';
import type { AuroraCluster } from '../types';

/* ── Helpers ── */

function engineBadge(engine: string) {
  return engine === 'aurora-mysql'
    ? <Badge color="blue">MySQL</Badge>
    : <Badge color="green">PostgreSQL</Badge>;
}

function clusterStatusIndicator(status: string) {
  switch (status.toLowerCase()) {
    case 'available': return <StatusIndicator type="success">Available</StatusIndicator>;
    case 'creating': return <StatusIndicator type="in-progress">Creating</StatusIndicator>;
    case 'modifying': return <StatusIndicator type="in-progress">Modifying</StatusIndicator>;
    case 'deleting': return <StatusIndicator type="error">Deleting</StatusIndicator>;
    case 'stopped': return <StatusIndicator type="stopped">Stopped</StatusIndicator>;
    default: return <StatusIndicator type="warning">{status}</StatusIndicator>;
  }
}

function deprecationBadge(cluster: AuroraCluster) {
  const r = cluster.upgradeReadiness;
  if (!r) return <StatusIndicator type="pending">Unknown</StatusIndicator>;
  switch (r.deprecationStatus) {
    case 'deprecated':
      return (
        <Popover dismissButton={false} position="top" size="small" triggerType="custom"
          content={<Box variant="small">End of support: {r.endOfSupport ?? 'N/A'}</Box>}>
          <StatusIndicator type="error">Deprecated</StatusIndicator>
        </Popover>
      );
    case 'approaching':
      return (
        <Popover dismissButton={false} position="top" size="small" triggerType="custom"
          content={<Box variant="small">End of support: {r.endOfSupport ?? 'N/A'}</Box>}>
          <StatusIndicator type="warning">Approaching EOL</StatusIndicator>
        </Popover>
      );
    case 'ok': return <StatusIndicator type="success">Supported</StatusIndicator>;
    default: return <StatusIndicator type="pending">Unknown</StatusIndicator>;
  }
}

function codeAnalysisCell(cluster: AuroraCluster, onRun: (c: AuroraCluster) => void) {
  const ca = cluster.upgradeReadiness?.lastCodeAnalysis;
  if (!ca) {
    return <Button variant="inline-link" onClick={() => onRun(cluster)}>Run Analysis</Button>;
  }
  return (
    <SpaceBetween size="xxs">
      {ca.status === 'complete'
        ? <StatusIndicator type="success">Complete</StatusIndicator>
        : ca.status === 'running'
        ? <StatusIndicator type="in-progress">Running</StatusIndicator>
        : <StatusIndicator type="error">Failed</StatusIndicator>}
      {ca.status === 'complete' && (
        <Box variant="small" color={ca.errors > 0 ? 'text-status-error' : 'text-body-secondary'}>
          {ca.totalFindings} findings ({ca.errors} errors)
        </Box>
      )}
      <Button variant="inline-link" onClick={() => onRun(cluster)}>Re-run Analysis</Button>
    </SpaceBetween>
  );
}

function upgradeTestCell(cluster: AuroraCluster, onNavigate: (c: AuroraCluster) => void) {
  const ut = cluster.upgradeReadiness?.lastUpgradeTest;
  if (!ut) {
    return (
      <Button variant="inline-link" iconName="external" onClick={() => onNavigate(cluster)}>
        Run Upgrade Test
      </Button>
    );
  }
  const phaseLabel = ut.phase ? ut.phase.charAt(0).toUpperCase() + ut.phase.slice(1) : '';
  return (
    <SpaceBetween size="xxs">
      {ut.status === 'complete'
        ? <StatusIndicator type="success">Complete</StatusIndicator>
        : ut.status === 'running'
        ? <StatusIndicator type="in-progress">{phaseLabel || 'Running'}</StatusIndicator>
        : <StatusIndicator type="error">Failed</StatusIndicator>}
      <Button variant="inline-link" onClick={() => onNavigate(cluster)}>View / Re-run</Button>
    </SpaceBetween>
  );
}

function clusterAnalysisCell(cluster: AuroraCluster, onNavigate: (c: AuroraCluster) => void) {
  const cla = cluster.upgradeReadiness?.lastClusterAnalysis;
  if (!cla) {
    return <Button variant="inline-link" onClick={() => onNavigate(cluster)}>Run Analysis</Button>;
  }
  return (
    <SpaceBetween size="xxs">
      {cla.status === 'complete'
        ? <StatusIndicator type="success">Complete</StatusIndicator>
        : cla.status === 'running'
        ? <StatusIndicator type="in-progress">Running</StatusIndicator>
        : <StatusIndicator type="error">Failed</StatusIndicator>}
      {cla.status === 'complete' && (
        <Box variant="small" color={cla.highSeverity > 0 ? 'text-status-error' : 'text-body-secondary'}>
          {cla.breakingChanges} breaking changes ({cla.highSeverity} high)
        </Box>
      )}
      <Button variant="inline-link" onClick={() => onNavigate(cluster)}>Re-run Analysis</Button>
    </SpaceBetween>
  );
}

/* ── Main component ── */

export default function DatabaseList() {
  const navigate = useNavigate();
  const [clusters, setClusters] = useState<AuroraCluster[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [currentAccountId, setCurrentAccountId] = useState('');
  const [crossAccountId, setCrossAccountId] = useState('');
  const [crossAccountInput, setCrossAccountInput] = useState('');
  const [filterText, setFilterText] = useState('');
  const [currentPage, setCurrentPage] = useState(1);

  const loadDatabases = useCallback(async (accountId?: string) => {
    setLoading(true);
    setError(null);
    try {
      const result = await getDatabases(accountId);
      setClusters(result.clusters);
      if (!accountId) setCurrentAccountId(result.accountId);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load databases');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    getSettings().then((s) => {
      if (s.aws?.accountId) setCurrentAccountId(s.aws.accountId);
    }).catch(() => {});
    loadDatabases();
  }, [loadDatabases]);

  const handleCrossAccountSearch = () => {
    const id = crossAccountInput.trim();
    if (!id) return;
    setCrossAccountId(id);
    loadDatabases(id);
  };

  const handleResetToCurrentAccount = () => {
    setCrossAccountId('');
    setCrossAccountInput('');
    loadDatabases();
  };

  const handleRunCodeAnalysis = (cluster: AuroraCluster) => {
    const params = new URLSearchParams({ accountId1: cluster.accountId, clusterArn: cluster.clusterArn });
    navigate(`/code-analysis?${params.toString()}`);
  };

  const handleUpgradeTest = (cluster: AuroraCluster) => {
    navigate(`/cluster/${encodeURIComponent(cluster.clusterArn)}?tab=upgrade`);
  };

  const handleClusterAnalysis = (cluster: AuroraCluster) => {
    navigate(`/cluster-analysis?clusterArn=${encodeURIComponent(cluster.clusterArn)}`);
  };

  const filtered = clusters.filter((c) => {
    if (!filterText) return true;
    const q = filterText.toLowerCase();
    return (
      c.clusterId.toLowerCase().includes(q) ||
      c.engine.toLowerCase().includes(q) ||
      c.engineVersion.toLowerCase().includes(q) ||
      c.status.toLowerCase().includes(q)
    );
  });

  const PAGE_SIZE = 10;
  const totalPages = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
  const pagedItems = filtered.slice((currentPage - 1) * PAGE_SIZE, currentPage * PAGE_SIZE);

  const activeAccount = crossAccountId || currentAccountId;

  return (
    <SpaceBetween size="l">
      {error && <Alert type="error" dismissible onDismiss={() => setError(null)}>{error}</Alert>}

      {/* Account selector */}
      <Container header={<Header variant="h3">Account</Header>}>
        <ColumnLayout columns={2}>
          <SpaceBetween size="s">
            <Box variant="awsui-key-label">Current Account</Box>
            <Box>{currentAccountId || '—'}</Box>
            {crossAccountId && (
              <Button variant="link" onClick={handleResetToCurrentAccount}>
                Back to current account
              </Button>
            )}
          </SpaceBetween>
          <FormField
            label="Search another account"
            description="Enter an AWS account ID to list databases via cross-account role"
          >
            <SpaceBetween direction="horizontal" size="xs">
              <Input
                value={crossAccountInput}
                onChange={({ detail }) => setCrossAccountInput(detail.value)}
                placeholder="123456789012"
                onKeyDown={({ detail }) => detail.key === 'Enter' && handleCrossAccountSearch()}
              />
              <Button onClick={handleCrossAccountSearch} disabled={!crossAccountInput.trim()}>
                Search
              </Button>
            </SpaceBetween>
          </FormField>
        </ColumnLayout>
      </Container>

      {/* Database table */}
      <Table
        header={
          <Header
            variant="h3"
            counter={loading ? undefined : `(${filtered.length})`}
            description={`Showing databases from account ${activeAccount}`}
            actions={
              <Button iconName="refresh" onClick={() => loadDatabases(crossAccountId || undefined)} disabled={loading}>
                Refresh
              </Button>
            }
          >
            Aurora Clusters
          </Header>
        }
        filter={
          <TextFilter
            filteringText={filterText}
            filteringPlaceholder="Filter by name, engine, version, or status"
            onChange={({ detail }) => { setFilterText(detail.filteringText); setCurrentPage(1); }}
          />
        }
        columnDefinitions={[
          {
            id: 'name',
            header: 'Cluster',
            cell: (c) => (
              <Button variant="inline-link" onClick={() => navigate(`/cluster/${encodeURIComponent(c.clusterArn)}`)}>
                {c.clusterId}
              </Button>
            ),
            sortingField: 'clusterId',
          },
          {
            id: 'engine',
            header: 'Type',
            cell: (c) => engineBadge(c.engine),
            width: 80,
          },
          {
            id: 'version',
            header: 'Version',
            cell: (c) => <Box variant="small">{c.engineVersion}</Box>,
            sortingField: 'engineVersion',
            width: 120,
          },
          {
            id: 'status',
            header: 'Status',
            cell: (c) => clusterStatusIndicator(c.status),
            width: 100,
          },
          {
            id: 'deprecation',
            header: 'Support',
            cell: (c) => deprecationBadge(c),
            width: 110,
          },
          {
            id: 'upgradeTargets',
            header: 'Targets',
            cell: (c) => {
              const targets = c.upgradeReadiness?.upgradeTargets ?? [];
              if (targets.length === 0) return <Box color="text-body-secondary">—</Box>;
              return (
                <Box variant="small">
                  {targets.slice(0, 1).join(', ')}
                  {targets.length > 1 && ` +${targets.length - 1}`}
                </Box>
              );
            },
            width: 120,
          },
          {
            id: 'codeAnalysis',
            header: 'Code Analysis',
            cell: (c) => codeAnalysisCell(c, handleRunCodeAnalysis),
            width: 140,
          },
          {
            id: 'clusterAnalysis',
            header: 'Cluster Analysis',
            cell: (c) => clusterAnalysisCell(c, handleClusterAnalysis),
            width: 140,
          },
          {
            id: 'upgradeTest',
            header: 'Upgrade Test',
            cell: (c) => upgradeTestCell(c, handleUpgradeTest),
            width: 140,
          },
        ]}
        items={pagedItems}
        loading={loading}
        loadingText="Loading Aurora clusters..."
        empty={
          <Box textAlign="center" color="text-body-secondary" padding="l">
            No Aurora MySQL or PostgreSQL clusters found in account {activeAccount}. Verify the account has Aurora clusters and the cross-account role has the required permissions.
          </Box>
        }
        variant="container"
        sortingDisabled={false}
        pagination={
          <Pagination
            currentPageIndex={currentPage}
            pagesCount={totalPages}
            onChange={({ detail }) => setCurrentPage(detail.currentPageIndex)}
          />
        }
      />
    </SpaceBetween>
  );
}
