import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import Container from '@cloudscape-design/components/container';
import Header from '@cloudscape-design/components/header';
import SpaceBetween from '@cloudscape-design/components/space-between';
import ColumnLayout from '@cloudscape-design/components/column-layout';
import Box from '@cloudscape-design/components/box';
import Button from '@cloudscape-design/components/button';
import Table from '@cloudscape-design/components/table';
import Badge from '@cloudscape-design/components/badge';
import Link from '@cloudscape-design/components/link';
import Spinner from '@cloudscape-design/components/spinner';
import Pagination from '@cloudscape-design/components/pagination';
import { getDashboardSummary } from '../services/api';
import type { AgentRun, DashboardSummary } from '../types';

const PAGE_SIZE = 10;

function formatTimestamp(iso: string): string {
  const d = new Date(iso);
  return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' }) +
    ', ' + d.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' });
}

function agentBadge(type: AgentRun['agentType']) {
  switch (type) {
    case 'deprecation': return <Badge color="grey">Version Catalog</Badge>;
    case 'code-analysis': return <Badge color="blue">Code Analysis</Badge>;
    case 'cluster-analysis-compat': return <Badge color="green">Cluster Analysis</Badge>;
    case 'upgrade-test': return <Badge>Upgrade Test</Badge>;
    default: return <Badge>{type}</Badge>;
  }
}

function statusBadge(status: AgentRun['status']) {
  switch (status) {
    case 'complete': return <Badge color="green">Complete</Badge>;
    case 'failed': return <Badge color="red">Failed</Badge>;
    case 'running': return <Badge color="blue">Running</Badge>;
    default: return <Badge color="grey">Pending</Badge>;
  }
}

function runSummary(run: AgentRun): string {
  if (run.errorMessage) return run.errorMessage;
  if (run.agentType === 'deprecation' && run.summary) {
    const versions = run.summary.totalVersions ?? run.summary.versionsCataloged ?? 0;
    const deprecated = run.summary.deprecatedVersions ?? run.summary.deprecated ?? 0;
    return `${versions} versions cataloged, ${deprecated} deprecated`;
  }
  if (run.agentType === 'cluster-analysis-compat' && run.summary) {
    const bc = run.summary.breakingChanges ?? 0;
    const high = run.summary.highSeverity ?? 0;
    return `${bc} breaking changes (${high} high severity)`;
  }
  if (run.agentType === 'upgrade-test' && run.summary) {
    const verdict = run.summary.verdict ?? '';
    const risk = run.summary.riskLevel ?? '';
    return verdict ? `${verdict} · Risk: ${risk}` : `Risk: ${risk}`;
  }
  if (run.summary) {
    return `${run.summary.total ?? run.summary.totalFindings ?? 0} findings (${run.summary.errors ?? 0} errors, ${run.summary.warnings ?? 0} warnings)`;
  }
  return '—';
}

function appCluster(run: AgentRun) {
  if (run.agentType === 'deprecation') {
    return <Box color="text-body-secondary">— all engines —</Box>;
  }
  const arn = (run.inputs?.clusterArn as string) ?? '';
  const clusterId = run.summary?.clusterId != null ? String(run.summary.clusterId) : arn.split(':').pop() ?? '';
  return (
    <SpaceBetween size="xxxs">
      <Box fontWeight="bold">{run.inputs?.appci ?? clusterId ?? '—'}</Box>
      {arn && <Box variant="small" color="text-body-secondary">{arn}</Box>}
    </SpaceBetween>
  );
}

export default function Dashboard() {
  const navigate = useNavigate();
  const [loading, setLoading] = useState(true);
  const [summary, setSummary] = useState<DashboardSummary | null>(null);
  const [currentPage, setCurrentPage] = useState(1);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    getDashboardSummary().then((data) => {
      if (!cancelled) {
        setSummary(data);
        setLoading(false);
      }
    }).catch(() => {
      if (!cancelled) setLoading(false);
    });
    return () => { cancelled = true; };
  }, []);

  const dep = summary?.deprecation;
  const ca = summary?.codeAnalysis;
  const cla = summary?.clusterAnalysis;
  const ut = summary?.upgradeTests;
  const allRuns = (summary?.recentRuns ?? [])
    .sort((a, b) => new Date(b.startTime).getTime() - new Date(a.startTime).getTime());
  const totalPages = Math.max(1, Math.ceil(allRuns.length / PAGE_SIZE));
  const pagedRuns = allRuns.slice((currentPage - 1) * PAGE_SIZE, currentPage * PAGE_SIZE);

  if (loading) {
    return (
      <Box textAlign="center" padding="xxl">
        <Spinner size="large" />
        <Box variant="p" color="text-body-secondary" margin={{ top: 's' }}>Loading dashboard...</Box>
      </Box>
    );
  }

  return (
    <SpaceBetween size="l">
      <SpaceBetween size="xxs">
        <Box variant="p" color="text-body-secondary">
          AWS Bedrock agent driven upgrade readiness platform that tests, analyzes, and reports breaking changes across your Aurora MySQL and PostgreSQL clusters and codebases before you upgrade.
        </Box>
        <Box variant="small" padding={{ top: 'xxs' }}>
          <span style={{ background: '#fef3cd', color: '#856404', padding: '2px 8px', borderRadius: '4px', fontSize: '12px' }}>
            ⚠️ Read-only analysis only — no changes are made to your production databases or application code
          </span>
        </Box>
      </SpaceBetween>

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '16px', alignItems: 'stretch' }}>
        {/* Database Versions Catalog Card */}
        <div style={{ display: 'flex', flexDirection: 'column' }}>
          <Container fitHeight header={<Header variant="h3" actions={<Button variant="primary" onClick={() => navigate('/deprecation')}>Open →</Button>}>Database Versions Catalog</Header>}>
            <SpaceBetween size="s">
              <ColumnLayout columns={2} variant="text-grid">
                <div><Box variant="awsui-key-label">Versions Cataloged</Box><Box variant="awsui-value-large">{dep?.versionsCataloged ?? 0}</Box></div>
                <div><Box variant="awsui-key-label">Deprecated</Box><Box variant="awsui-value-large" color={(dep?.deprecatedCount ?? 0) > 0 ? 'text-status-warning' : undefined}>{dep?.deprecatedCount ?? 0}</Box></div>
              </ColumnLayout>
              <Box variant="small" color="text-body-secondary">Last updated: {dep?.lastUpdated ? formatTimestamp(dep.lastUpdated) : 'Never'}</Box>
            </SpaceBetween>
          </Container>
        </div>

        {/* Code Analysis Card */}
        <div style={{ display: 'flex', flexDirection: 'column' }}>
          <Container fitHeight header={<Header variant="h3" actions={<Button variant="primary" onClick={() => navigate('/code-analysis')}>New Analysis →</Button>}>Code Analysis</Header>}>
            <SpaceBetween size="s">
              <ColumnLayout columns={2} variant="text-grid">
                <div><Box variant="awsui-key-label">Analyses Completed</Box><Box variant="awsui-value-large">{ca?.analysesCompleted ?? 0}</Box></div>
                <div><Box variant="awsui-key-label">Clusters Analyzed</Box><Box variant="awsui-value-large">{ca?.clustersAnalyzed ?? 0}</Box></div>
              </ColumnLayout>
              <Box variant="small" color="text-body-secondary">Last run: {ca?.lastRun ? formatTimestamp(ca.lastRun) : 'Never'}</Box>
              <Link onFollow={() => navigate('/code-analysis')}>View Latest Findings</Link>
            </SpaceBetween>
          </Container>
        </div>

        {/* Cluster Analysis Card */}
        <div style={{ display: 'flex', flexDirection: 'column' }}>
          <Container fitHeight header={<Header variant="h3" actions={<Button variant="primary" onClick={() => navigate('/cluster-analysis')}>New Analysis →</Button>}>Cluster Analysis</Header>}>
            <SpaceBetween size="s">
              <ColumnLayout columns={2} variant="text-grid">
                <div><Box variant="awsui-key-label">Analyses Completed</Box><Box variant="awsui-value-large">{cla?.analysesCompleted ?? 0}</Box></div>
                <div><Box variant="awsui-key-label">Clusters Analyzed</Box><Box variant="awsui-value-large">{cla?.clustersAnalyzed ?? 0}</Box></div>
              </ColumnLayout>
              <Box variant="small" color="text-body-secondary">Last run: {cla?.lastRun ? formatTimestamp(cla.lastRun) : 'Never'}</Box>
              <Link onFollow={() => navigate('/cluster-analysis')}>View Latest Findings</Link>
            </SpaceBetween>
          </Container>
        </div>

        {/* Upgrade Tests Card */}
        <div style={{ display: 'flex', flexDirection: 'column' }}>
          <Container fitHeight header={<Header variant="h3" actions={<Button variant="primary" onClick={() => navigate('/upgrade-tests')}>View Tests →</Button>}>Upgrade Tests</Header>}>
            <SpaceBetween size="s">
              <ColumnLayout columns={2} variant="text-grid">
                <div><Box variant="awsui-key-label">Completed</Box><Box variant="awsui-value-large">{ut?.completed ?? 0}</Box></div>
                <div><Box variant="awsui-key-label">Running</Box><Box variant="awsui-value-large" color={(ut?.running ?? 0) > 0 ? 'text-status-info' : undefined}>{ut?.running ?? 0}</Box></div>
              </ColumnLayout>
              <ColumnLayout columns={3} variant="text-grid">
                <div><Box variant="awsui-key-label">Go</Box><Box fontWeight="bold" color="text-status-success">{ut?.goCount ?? 0}</Box></div>
                <div><Box variant="awsui-key-label">Conditional</Box><Box fontWeight="bold" color="text-status-warning">{ut?.conditionalCount ?? 0}</Box></div>
                <div><Box variant="awsui-key-label">No-Go</Box><Box fontWeight="bold" color={(ut?.noGoCount ?? 0) > 0 ? 'text-status-error' : undefined}>{ut?.noGoCount ?? 0}</Box></div>
              </ColumnLayout>
              <Box variant="small" color="text-body-secondary">Last run: {ut?.lastRun ? formatTimestamp(ut.lastRun) : 'Never'}</Box>
              <Link onFollow={() => navigate('/upgrade-tests')}>View All Runs</Link>
            </SpaceBetween>
          </Container>
        </div>
      </div>

      {/* Recent Activity Table with Pagination */}
      <Table
        header={<Header variant="h3" counter={`(${allRuns.length})`}>Recent Activity</Header>}
        columnDefinitions={[
          { id: 'timestamp', header: 'Timestamp', cell: (r) => formatTimestamp(r.startTime), width: 140 },
          { id: 'agent', header: 'Agent', cell: (r) => agentBadge(r.agentType), width: 150 },
          { id: 'appCluster', header: 'Application / Cluster', cell: (r) => appCluster(r) },
          { id: 'target', header: 'Target Version', cell: (r) => r.inputs?.targetVersion ?? '—', width: 200 },
          { id: 'status', header: 'Status', cell: (r) => statusBadge(r.status), width: 100 },
          { id: 'summary', header: 'Summary', cell: (r) => runSummary(r) },
        ]}
        items={pagedRuns}
        variant="container"
        sortingDisabled
        empty="No recent activity. Run a Version Catalog Scan, Code Analysis, or Cluster Analysis to get started."
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
