import { useState, useEffect, useCallback, useRef } from 'react';
import { useSearchParams, useNavigate } from 'react-router-dom';
import Container from '@cloudscape-design/components/container';
import Header from '@cloudscape-design/components/header';
import SpaceBetween from '@cloudscape-design/components/space-between';
import FormField from '@cloudscape-design/components/form-field';
import Input from '@cloudscape-design/components/input';
import Select, { type SelectProps } from '@cloudscape-design/components/select';
import Table from '@cloudscape-design/components/table';
import Badge from '@cloudscape-design/components/badge';
import Box from '@cloudscape-design/components/box';
import Button from '@cloudscape-design/components/button';
import StatusIndicator from '@cloudscape-design/components/status-indicator';
import Alert from '@cloudscape-design/components/alert';
import ColumnLayout from '@cloudscape-design/components/column-layout';
import Tabs from '@cloudscape-design/components/tabs';
import RunStatusPill, { type RunStatus } from '../components/RunStatusPill';
import ProgressBar from '../components/ProgressBar';
import {
  getClusterAnalysis,
  getClusterAnalysisRuns,
  getClusterAnalysisRun,
  getDeprecationCatalog,
  type ClusterAnalysisRunSummary,
} from '../services/api';
import type { ClusterAnalysis, DeprecationCatalog, ProgressEvent } from '../types';

/* ── Helpers ── */

function sevBadge(sev: string) {
  if (sev === 'HIGH' || sev === 'error') return <Badge color="red">HIGH</Badge>;
  if (sev === 'MEDIUM' || sev === 'warning') return <Badge color="blue">MEDIUM</Badge>;
  return <Badge color="grey">LOW</Badge>;
}

function compatBadge(compat: string) {
  if (compat === 'removed') return <StatusIndicator type="error">Removed</StatusIndicator>;
  if (compat === 'changed') return <StatusIndicator type="warning">Changed</StatusIndicator>;
  return <StatusIndicator type="success">OK</StatusIndicator>;
}

function formatTs(ts: string): string {
  if (!ts) return '';
  try { return new Date(ts).toLocaleString(); } catch { return ts; }
}

function formatCatalogTimestamp(ts: string): string {
  let date: Date;
  if (/^\d{8}T\d{6}Z$/.test(ts)) {
    const y = ts.slice(0, 4), mo = ts.slice(4, 6), d = ts.slice(6, 8);
    const h = ts.slice(9, 11), mi = ts.slice(11, 13), s = ts.slice(13, 15);
    date = new Date(`${y}-${mo}-${d}T${h}:${mi}:${s}Z`);
  } else {
    date = new Date(ts);
  }
  return date.toLocaleString('en-US', {
    month: 'short', day: 'numeric', year: 'numeric',
    hour: 'numeric', minute: '2-digit', hour12: true,
  });
}

function buildTargetOptions(catalog: DeprecationCatalog | null): SelectProps.Option[] {
  if (!catalog) return [];
  const options: SelectProps.Option[] = [];
  const mysqlVersions = (catalog.engines?.['aurora-mysql']?.versions ?? [])
    .filter(v => v.status === 'available')
    .sort((a, b) => b.version.localeCompare(a.version));
  const mysqlLts = catalog.engines?.['aurora-mysql']?.ltsVersion;
  for (const v of mysqlVersions) {
    const isLts = v.version === mysqlLts;
    options.push({ value: v.version, label: `Aurora MySQL ${v.version}${isLts ? ' (LTS)' : ''}` });
  }
  const pgVersions = (catalog.engines?.['aurora-postgresql']?.versions ?? [])
    .filter(v => v.status === 'available')
    .sort((a, b) => parseFloat(b.version) - parseFloat(a.version));
  const pgLts = catalog.engines?.['aurora-postgresql']?.ltsVersion;
  for (const v of pgVersions) {
    const isLts = v.version === pgLts;
    options.push({ value: v.version, label: `Aurora PostgreSQL ${v.version}${isLts ? ' (LTS)' : ''}` });
  }
  return options;
}

export default function ClusterAnalysisPage() {
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const clusterArnParam = searchParams.get('clusterArn') ?? '';

  /* ── Input state ── */
  const [clusterArn, setClusterArn] = useState(clusterArnParam);
  const [targetVersionOptions, setTargetVersionOptions] = useState<SelectProps.Option[]>([]);
  const [targetVersion, setTargetVersion] = useState<SelectProps.Option | null>(null);
  const [catalogVersionOptions, setCatalogVersionOptions] = useState<SelectProps.Option[]>([]);
  const [catalogVersion, setCatalogVersion] = useState<SelectProps.Option | null>(null);
  const [catalogLoading, setCatalogLoading] = useState(false);

  /* ── Run lifecycle ── */
  const [runStatus, setRunStatus] = useState<RunStatus>('idle');
  const [progress, setProgress] = useState<ProgressEvent | null>(null);

  /* ── Results ── */
  const [data, setData] = useState<ClusterAnalysis | null>(null);
  const [error, setError] = useState<string | null>(null);

  /* ── Run history ── */
  const [runs, setRuns] = useState<ClusterAnalysisRunSummary[]>([]);
  const [selectedRun, setSelectedRun] = useState<SelectProps.Option | null>(null);
  const [runsLoading, setRunsLoading] = useState(true);

  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const stopPolling = useCallback(() => {
    if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; }
  }, []);

  useEffect(() => stopPolling, [stopPolling]);

  /* ── Load catalog versions on mount ── */
  useEffect(() => {
    let cancelled = false;
    async function fetchCatalog() {
      setCatalogLoading(true);
      const catalog = await getDeprecationCatalog();
      if (cancelled || !catalog) { setCatalogLoading(false); return; }
      const catOpts: SelectProps.Option[] = catalog.availableVersions.map((v) => ({
        value: v.version,
        label: formatCatalogTimestamp(v.version),
        description: `Size: ${(v.size / 1024).toFixed(1)} KB`,
      }));
      setCatalogVersionOptions(catOpts);
      const current = catOpts.find((o) => o.value === catalog.currentVersion);
      setCatalogVersion(current ?? catOpts[0] ?? null);
      const targets = buildTargetOptions(catalog.catalog);
      setTargetVersionOptions(targets);
      if (targets.length > 0) setTargetVersion(targets[0]);
      setCatalogLoading(false);
    }
    fetchCatalog();
    return () => { cancelled = true; };
  }, []);

  /* ── Load run history on mount ── */
  const loadRunHistory = useCallback(async () => {
    setRunsLoading(true);
    try {
      const r = await getClusterAnalysisRuns();
      setRuns(r);
      return r;
    } finally {
      setRunsLoading(false);
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    async function init() {
      const r = await loadRunHistory();
      if (cancelled) return;
      // If navigated with clusterArn, auto-select latest run for that cluster
      if (clusterArnParam) {
        const match = r.find((run) => run.clusterArn === clusterArnParam);
        if (match) {
          setSelectedRun({ label: `${match.clusterId} — ${formatTs(match.startTime)}`, value: match.runId });
          loadHistoricRun(match.runId);
        }
      } else if (r.length > 0) {
        // Show latest run
        const latest = r[0];
        setSelectedRun({ label: `${latest.clusterId} — ${formatTs(latest.startTime)}`, value: latest.runId });
        loadHistoricRun(latest.runId);
      }
    }
    init();
    return () => { cancelled = true; };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [clusterArnParam]);

  const loadHistoricRun = async (runId: string) => {
    setError(null);
    setRunStatus('complete');
    try {
      const result = await getClusterAnalysisRun(runId);
      setData(result);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load analysis');
      setRunStatus('failed');
    }
  };

  /* ── Run button handler ── */
  const handleRun = async () => {
    if (!clusterArn.trim()) {
      setError('Source Cluster ARN is required');
      return;
    }
    setError(null);
    setData(null);
    setRunStatus('running');
    setProgress({
      runId: '', timestamp: new Date().toISOString(), percentage: 10,
      currentActivity: 'Starting cluster compatibility analysis...', elapsedSeconds: 0,
      agentType: 'cluster-analysis' as ProgressEvent['agentType'],
    });

    try {
      const tv = targetVersion?.value;
      const result = await getClusterAnalysis(clusterArn.trim(), tv || undefined);
      setData(result);
      setRunStatus('complete');
      setProgress({
        runId: '', timestamp: new Date().toISOString(), percentage: 100,
        currentActivity: 'Cluster analysis complete.', elapsedSeconds: 0,
        agentType: 'cluster-analysis' as ProgressEvent['agentType'],
      });
      // Refresh run history
      const updatedRuns = await loadRunHistory();
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const rid = (result as any).runId as string | undefined;
      if (rid) {
        const newRun = updatedRuns.find((r) => r.runId === rid);
        if (newRun) {
          setSelectedRun({ label: `${newRun.clusterId} — ${formatTs(newRun.startTime)}`, value: newRun.runId });
        }
      }
    } catch (e) {
      setRunStatus('failed');
      setError(e instanceof Error ? e.message : 'Analysis failed');
      setProgress(null);
    }
  };

  const handleRunSelect = async (option: SelectProps.Option) => {
    setSelectedRun(option);
    if (option.value) loadHistoricRun(option.value);
  };

  const runHistoryOptions: SelectProps.Option[] = runs.map((r) => ({
    label: `${r.clusterId} — ${formatTs(r.startTime)}`,
    value: r.runId,
    description: `${r.engine === 'aurora-mysql' ? 'MySQL' : 'PostgreSQL'} ${r.currentVersion} → ${r.targetVersion} · ${r.breakingChanges} breaking changes`,
  }));

  const breakingChanges = data?.breakingChanges ?? [];
  const highCount = breakingChanges.filter((bc) => bc.severity === 'error').length;
  const medCount = breakingChanges.filter((bc) => bc.severity === 'warning').length;
  const paramFindings = (data?.parameterSettings ?? []).filter((p) => p.compatibility !== 'ok');

  return (
    <SpaceBetween size="l">
      {error && <Alert type="error" dismissible onDismiss={() => setError(null)}>{error}</Alert>}

      {/* Input section */}
      <Container header={<Header variant="h3">Cluster Analysis — Input</Header>}>
        <SpaceBetween size="l">
          <ColumnLayout columns={2}>
            <FormField label="Source Cluster ARN">
              <Input
                value={clusterArn}
                onChange={({ detail }) => setClusterArn(detail.value)}
                placeholder="arn:aws:rds:region:account:cluster:name"
              />
            </FormField>
            <FormField label="Target Database Engine Version">
              <Select
                selectedOption={targetVersion}
                onChange={({ detail }) => setTargetVersion(detail.selectedOption)}
                options={targetVersionOptions}
                filteringType="auto"
                placeholder="Select target version"
                empty="Run Version Catalog first"
              />
            </FormField>
            <FormField label="Deprecation Catalog Version">
              <Select
                selectedOption={catalogVersion}
                onChange={({ detail }) => setCatalogVersion(detail.selectedOption)}
                options={catalogVersionOptions}
                loadingText="Loading catalog versions..."
                statusType={catalogLoading ? 'loading' : 'finished'}
                placeholder="Select catalog version"
                empty="No catalog versions available"
              />
            </FormField>
          </ColumnLayout>
          <SpaceBetween direction="horizontal" size="s">
            <Button variant="primary" onClick={handleRun} disabled={runStatus === 'running'}>
              Run Cluster Analysis
            </Button>
            <RunStatusPill status={runStatus} />
          </SpaceBetween>
        </SpaceBetween>
      </Container>

      {/* Progress */}
      {runStatus === 'running' && progress && (
        <Container header={<Header variant="h3">Analysis Progress</Header>}>
          <ProgressBar progress={progress} />
        </Container>
      )}

      {/* Run history */}
      {runHistoryOptions.length > 0 && (
        <Container header={<Header variant="h3">Run History</Header>}>
          <FormField label="Select a previous run to view results">
            <Select
              selectedOption={selectedRun}
              onChange={({ detail }) => handleRunSelect(detail.selectedOption)}
              options={runHistoryOptions}
              placeholder="Select a previous run"
              statusType={runsLoading ? 'loading' : undefined}
              filteringType="auto"
            />
          </FormField>
        </Container>
      )}

      {/* Results */}
      {data && runStatus === 'complete' && (
        <>
          <Container header={<Header variant="h3">Compatibility Analysis</Header>}>
            <ColumnLayout columns={4} variant="text-grid">
              <div><Box variant="awsui-key-label">Cluster</Box><Box>{data.clusterId}</Box></div>
              <div><Box variant="awsui-key-label">Engine</Box><Box>{data.engine === 'aurora-mysql' ? 'Aurora MySQL' : 'Aurora PostgreSQL'}</Box></div>
              <div><Box variant="awsui-key-label">Current → Target</Box><Box>{data.currentVersion} → {data.targetVersion}</Box></div>
              <div><Box variant="awsui-key-label">Analyzed</Box><Box>{formatTs(data.collectedAt)}</Box></div>
            </ColumnLayout>
          </Container>

          <Container header={<Header variant="h3">Risk Summary</Header>}>
            <ColumnLayout columns={4} variant="text-grid">
              <div><Box variant="awsui-key-label">Breaking Changes</Box><Box variant="h1">{breakingChanges.length}</Box></div>
              <div><Box variant="awsui-key-label">High Severity</Box><Box variant="h1" color="text-status-error">{highCount}</Box></div>
              <div><Box variant="awsui-key-label">Medium Severity</Box><Box variant="h1" color="text-status-warning">{medCount}</Box></div>
              <div><Box variant="awsui-key-label">Parameter Issues</Box><Box variant="h1">{paramFindings.length}</Box></div>
            </ColumnLayout>
          </Container>

          <Tabs tabs={[
            {
              id: 'breaking', label: `Breaking Changes (${breakingChanges.length})`,
              content: (
                <Table columnDefinitions={[
                  { id: 'severity', header: 'Severity', cell: (bc) => sevBadge(bc.severity), width: 100 },
                  { id: 'category', header: 'Category', cell: (bc) => <Badge>{bc.category}</Badge>, width: 140 },
                  { id: 'description', header: 'Description', cell: (bc) => bc.description },
                  { id: 'remediation', header: 'Remediation', cell: (bc) => bc.remediation ?? '—', width: 300 },
                ]} items={breakingChanges} variant="embedded"
                  empty={<Box textAlign="center" color="text-body-secondary">No breaking changes — looking good.</Box>} />
              ),
            },
            {
              id: 'params', label: `Parameter Settings (${data.parameterSettings.length})`,
              content: (
                <Table columnDefinitions={[
                  { id: 'name', header: 'Parameter', cell: (p) => p.name, width: 250 },
                  { id: 'value', header: 'Current Value', cell: (p) => p.currentValue || '—', width: 200 },
                  { id: 'compat', header: 'Compatibility', cell: (p) => compatBadge(p.compatibility), width: 130 },
                  { id: 'note', header: 'Note', cell: (p) => p.note || '—' },
                ]} items={data.parameterSettings} variant="embedded"
                  empty={<Box textAlign="center" color="text-body-secondary">No custom parameter settings.</Box>} />
              ),
            },
            {
              id: 'queries', label: `Profiled Queries (${data.profiledQueries.length})`,
              content: (
                <Table columnDefinitions={[
                  { id: 'digest', header: 'Query Digest', cell: (q) => <Box variant="code">{q.digest}</Box> },
                  { id: 'calls', header: 'Call Count', cell: (q) => q.callCount.toLocaleString(), width: 110 },
                  { id: 'avgLat', header: 'Avg Latency', cell: (q) => `${q.avgLatencyMs.toFixed(1)} ms`, width: 110 },
                  { id: 'findings', header: 'Findings', cell: (q) => q.findings.length > 0
                    ? <Badge color="red">{q.findings.length}</Badge>
                    : <StatusIndicator type="success">Clean</StatusIndicator>, width: 100 },
                ]} items={data.profiledQueries} variant="embedded"
                  empty={<Box textAlign="center" color="text-body-secondary">No profiled queries available.</Box>} />
              ),
            },
            {
              id: 'sp', label: `Stored Procedures (${data.storedProcedures.length})`,
              content: (
                <Table columnDefinitions={[
                  { id: 'name', header: 'Name', cell: (sp) => sp.name, width: 200 },
                  { id: 'schema', header: 'Schema', cell: (sp) => sp.schema, width: 100 },
                  { id: 'type', header: 'Type', cell: (sp) => <Badge>{sp.type}</Badge>, width: 120 },
                  { id: 'findings', header: 'Findings', cell: (sp) => sp.findings.length > 0
                    ? <Badge color="red">{sp.findings.length}</Badge>
                    : <StatusIndicator type="success">Clean</StatusIndicator>, width: 100 },
                ]} items={data.storedProcedures} variant="embedded"
                  empty={<Box textAlign="center" color="text-body-secondary">No stored procedures found.</Box>} />
              ),
            },
          ]} />

          <Container>
            <SpaceBetween direction="horizontal" size="s">
              <Button onClick={() => navigate('/databases')}>Back to Database List</Button>
              <Button variant="primary" onClick={() => navigate(`/cluster/${encodeURIComponent(data.clusterArn)}?tab=upgrade`)}>
                Proceed to Upgrade Test
              </Button>
            </SpaceBetween>
          </Container>
        </>
      )}

      {/* Empty state when no data and not running */}
      {!data && runStatus !== 'running' && !error && runs.length === 0 && (
        <Container>
          <Box textAlign="center" padding="l" color="text-body-secondary">
            Enter a Source Cluster ARN above and click Run Cluster Analysis, or go to{' '}
            <Button variant="inline-link" onClick={() => navigate('/databases')}>Database List</Button>{' '}
            and click Analyze on a cluster.
          </Box>
        </Container>
      )}
    </SpaceBetween>
  );
}
