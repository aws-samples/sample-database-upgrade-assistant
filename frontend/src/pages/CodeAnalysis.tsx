import { useCallback, useEffect, useRef, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import Container from '@cloudscape-design/components/container';
import Header from '@cloudscape-design/components/header';
import SpaceBetween from '@cloudscape-design/components/space-between';
import FormField from '@cloudscape-design/components/form-field';
import Input from '@cloudscape-design/components/input';
import Select, { SelectProps } from '@cloudscape-design/components/select';
import Button from '@cloudscape-design/components/button';
import ColumnLayout from '@cloudscape-design/components/column-layout';
import Table from '@cloudscape-design/components/table';
import Badge from '@cloudscape-design/components/badge';
import Box from '@cloudscape-design/components/box';
import Link from '@cloudscape-design/components/link';
import RunStatusPill, { type RunStatus } from '../components/RunStatusPill';
import ProgressBar from '../components/ProgressBar';
import ClusterInfo from '../components/ClusterInfo';
import FindingsTable from '../components/FindingsTable';
import {
  getCodeAnalysisRuns,
  getCodeAnalysisFindings,
  getDeprecationCatalog,
  triggerCodeAnalysis,
  getRunStatus,
} from '../services/api';
import type {
  ClusterMetadata,
  CodeAnalysisInputs,
  ConnectedResource,
  DeprecationCatalog,
  Finding,
  FindingsResponse,
  ProgressEvent,
} from '../types';

const POLL_INTERVAL_MS = 5000;

const FALLBACK_TARGET_OPTIONS: SelectProps.Option[] = [
  { value: '8.0.mysql_aurora.3.08.0', label: 'Aurora MySQL 3.08.0 (LTS)' },
  { value: '15.17', label: 'Aurora PostgreSQL 15.17 (LTS)' },
];

/** Build target version options from the deprecation catalog */
function buildTargetOptions(catalog: DeprecationCatalog | null): SelectProps.Option[] {
  if (!catalog) return FALLBACK_TARGET_OPTIONS;

  const options: SelectProps.Option[] = [];

  // Aurora MySQL — available versions, sorted descending
  const mysqlVersions = (catalog.engines?.['aurora-mysql']?.versions ?? [])
    .filter(v => v.status === 'available' && v.version.startsWith('8.0'))
    .sort((a, b) => b.version.localeCompare(a.version));
  const mysqlLts = catalog.engines?.['aurora-mysql']?.ltsVersion;
  for (const v of mysqlVersions) {
    const isLts = v.version === mysqlLts;
    options.push({
      value: v.version,
      label: `Aurora MySQL ${v.version}${isLts ? ' (LTS)' : ''}`,
      description: isLts ? 'Recommended Long Term Support' : undefined,
    });
  }

  // Aurora PostgreSQL — available versions, sorted descending
  const pgVersions = (catalog.engines?.['aurora-postgresql']?.versions ?? [])
    .filter(v => v.status === 'available')
    .sort((a, b) => parseFloat(b.version) - parseFloat(a.version));
  const pgLts = catalog.engines?.['aurora-postgresql']?.ltsVersion;
  for (const v of pgVersions) {
    const isLts = v.version === pgLts;
    options.push({
      value: v.version,
      label: `Aurora PostgreSQL ${v.version}${isLts ? ' (LTS)' : ''}`,
      description: isLts ? 'Recommended Long Term Support' : undefined,
    });
  }

  return options.length > 0 ? options : FALLBACK_TARGET_OPTIONS;
}


/** Format compact timestamp like "20260413T173516Z" to readable string */
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
    hour: 'numeric', minute: '2-digit', second: '2-digit', hour12: true,
  });
}

export default function CodeAnalysis() {
  /* ── Form state ── */
  const [searchParams] = useSearchParams();
  const [accountId1, setAccountId1] = useState(searchParams.get('accountId1') ?? '');
  const [clusterArn, setClusterArn] = useState(searchParams.get('clusterArn') ?? '');
  const [accountId2, setAccountId2] = useState('');
  const [appci, setAppci] = useState('');
  const [sourceUrl, setSourceUrl] = useState('');
  const [targetVersionOptions, setTargetVersionOptions] = useState<SelectProps.Option[]>(FALLBACK_TARGET_OPTIONS);
  const [targetVersion, setTargetVersion] = useState<SelectProps.Option>(FALLBACK_TARGET_OPTIONS[0]);

  /* ── Catalog version dropdown ── */
  const [catalogVersionOptions, setCatalogVersionOptions] = useState<SelectProps.Option[]>([]);
  const [catalogVersion, setCatalogVersion] = useState<SelectProps.Option | null>(null);
  const [catalogLoading, setCatalogLoading] = useState(false);

  /* ── Run lifecycle state ── */
  const [runStatus, setRunStatus] = useState<RunStatus>('idle');
  const [progress, setProgress] = useState<ProgressEvent | null>(null);

  /* ── Results state ── */
  const [cluster, setCluster] = useState<ClusterMetadata | null>(null);
  const [resources, setResources] = useState<ConnectedResource[]>([]);
  const [findings, setFindings] = useState<Finding[]>([]);
  const [reportUrl, setReportUrl] = useState<string | undefined>();

  /* ── Run history for the dropdown ── */
  const [runHistoryOptions, setRunHistoryOptions] = useState<SelectProps.Option[]>([]);
  const [selectedRun, setSelectedRun] = useState<SelectProps.Option | null>(null);

  /* ── Track the inputs used for the current results ── */
  const [resultInputs, setResultInputs] = useState<{
    accountId1: string; clusterArn: string; accountId2: string;
    appci: string; sourceUrl: string; targetVersion: string;
  } | null>(null);

  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const stalledCheckRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const lastProgressPctRef = useRef<number>(0);

  /* ── Helpers ── */

  const stopPolling = useCallback(() => {
    if (pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
    if (stalledCheckRef.current) {
      clearTimeout(stalledCheckRef.current);
      stalledCheckRef.current = null;
    }
  }, []);

  const applyFindingsResponse = useCallback((resp: FindingsResponse) => {
    setFindings(resp.findings);
    setReportUrl(resp.reportUrl);
    if (resp.clusterMetadata) setCluster(resp.clusterMetadata);
    if (resp.connectedResources) setResources(resp.connectedResources);
  }, []);

  const loadRunHistory = useCallback(async () => {
    const runs = await getCodeAnalysisRuns();
    const opts = runs.map((r) => ({
      value: r.runId,
      label: `${new Date(r.startTime).toLocaleString()} (${r.status})`,
      description: [
        r.inputs?.clusterArn ?? '',
        r.inputs?.sourceUrl ?? '',
      ].filter(Boolean).join(' | '),
    }));
    setRunHistoryOptions(opts);
    return runs;
  }, []);

  /* ── Load last completed run on mount ── */
  useEffect(() => {
    let cancelled = false;

    async function init() {
      const runs = await loadRunHistory();
      if (cancelled) return;

      const completed = runs.find((r) => r.status === 'complete');
      if (completed) {
        setSelectedRun({
          value: completed.runId,
          label: `${new Date(completed.startTime).toLocaleString()} (complete)`,
        });
        setRunStatus('complete');
        const resp = await getCodeAnalysisFindings(completed.runId);
        if (!cancelled) {
          applyFindingsResponse(resp);
          if (completed.inputs) {
            setResultInputs({
              accountId1: completed.inputs.accountId1 ?? '',
              clusterArn: completed.inputs.clusterArn ?? '',
              accountId2: completed.inputs.accountId2 ?? '',
              appci: completed.inputs.appci ?? '',
              sourceUrl: completed.inputs.sourceUrl ?? '',
              targetVersion: completed.inputs.targetVersion ?? '',
            });
          }
        }
      }
    }

    init();
    return () => { cancelled = true; };
  }, [applyFindingsResponse, loadRunHistory]);

  /* ── Fetch catalog versions on mount ── */
  useEffect(() => {
    let cancelled = false;

    async function fetchCatalog() {
      setCatalogLoading(true);
      const catalog = await getDeprecationCatalog();
      if (cancelled || !catalog) {
        setCatalogLoading(false);
        return;
      }
      // Build catalog version dropdown
      const opts: SelectProps.Option[] = catalog.availableVersions.map((v) => ({
        value: v.version,
        label: formatCatalogTimestamp(v.version),
        description: `Size: ${(v.size / 1024).toFixed(1)} KB`,
      }));
      setCatalogVersionOptions(opts);
      const current = opts.find((o) => o.value === catalog.currentVersion);
      setCatalogVersion(current ?? opts[0] ?? null);

      // Build target version dropdown from catalog data
      const targets = buildTargetOptions(catalog.catalog);
      setTargetVersionOptions(targets);
      setTargetVersion(targets[0]);

      setCatalogLoading(false);
    }

    fetchCatalog();
    return () => { cancelled = true; };
  }, []);

  /* ── Cleanup polling on unmount ── */
  useEffect(() => stopPolling, [stopPolling]);

  /* ── Poll for run completion ── */
  const startPolling = useCallback(
    (runId: string) => {
      stopPolling();
      lastProgressPctRef.current = 0;

      let elapsed = 0;
      setProgress({
        runId,
        timestamp: new Date().toISOString(),
        percentage: 5,
        currentActivity: 'Starting code analysis...',
        elapsedSeconds: 0,
        agentType: 'code-analysis',
      });

      pollRef.current = setInterval(async () => {
        elapsed += POLL_INTERVAL_MS / 1000;

        const runs = await getCodeAnalysisRuns();
        const run = runs.find((r) => r.runId === runId);

        if (!run) return;

        if (run.status === 'complete') {
          stopPolling();
          setRunStatus('complete');
          setProgress({
            runId,
            timestamp: new Date().toISOString(),
            percentage: 100,
            currentActivity: 'Code analysis complete.',
            elapsedSeconds: elapsed,
            agentType: 'code-analysis',
          });

          const resp = await getCodeAnalysisFindings(runId);
          applyFindingsResponse(resp);
          await loadRunHistory();
        } else if (run.status === 'failed') {
          stopPolling();
          setRunStatus('failed');
          setProgress({
            runId,
            timestamp: new Date().toISOString(),
            percentage: 0,
            currentActivity: run.errorMessage ?? 'Analysis failed.',
            elapsedSeconds: elapsed,
            agentType: 'code-analysis',
          });
        } else {
          const pct = Math.min(90, 5 + elapsed * 2);

          // If progress hasn't moved in 900s (Lambda timeout), check Lambda status
          if (pct === lastProgressPctRef.current && elapsed > 900) {
            if (!stalledCheckRef.current) {
              stalledCheckRef.current = setTimeout(async () => {
                stalledCheckRef.current = null;
                const statusResp = await getRunStatus(runId);
                if (statusResp.status === 'failed') {
                  stopPolling();
                  setRunStatus('failed');
                  setProgress({
                    runId,
                    timestamp: new Date().toISOString(),
                    percentage: 0,
                    currentActivity: 'Lambda execution failed or timed out.',
                    elapsedSeconds: elapsed,
                    agentType: 'code-analysis',
                  });
                } else {
                  setRunStatus('stalled');
                }
              }, 5000);
            }
          } else {
            lastProgressPctRef.current = pct;
            if (stalledCheckRef.current) {
              clearTimeout(stalledCheckRef.current);
              stalledCheckRef.current = null;
            }
          }

          setProgress({
            runId,
            timestamp: new Date().toISOString(),
            percentage: pct,
            currentActivity: 'Analysis in progress...',
            elapsedSeconds: elapsed,
            agentType: 'code-analysis',
          });
        }
      }, POLL_INTERVAL_MS);
    },
    [stopPolling, applyFindingsResponse, loadRunHistory],
  );

  /* ── Run button handler ── */
  const handleRun = async () => {
    // Validate required fields before submitting
    const missing = [];
    if (!accountId1.trim()) missing.push('Account ID 1');
    if (!clusterArn.trim()) missing.push('Source Cluster ARN');
    if (!accountId2.trim()) missing.push('Account ID 2');
    if (!appci.trim()) missing.push('AppCI Tag');
    if (!sourceUrl.trim()) missing.push('Source Code URL');
    if (missing.length > 0) {
      setProgress({
        runId: '', timestamp: new Date().toISOString(), percentage: 0,
        currentActivity: `Missing required fields: ${missing.join(', ')}`,
        elapsedSeconds: 0, agentType: 'code-analysis',
      });
      setRunStatus('failed');
      return;
    }

    // Clear previous results
    setCluster(null);
    setResources([]);
    setFindings([]);
    setReportUrl(undefined);
    setRunStatus('running');

    const inputs: CodeAnalysisInputs = {
      accountId1: accountId1.trim(),
      clusterArn: clusterArn.trim(),
      accountId2: accountId2.trim(),
      appci: appci.trim(),
      sourceUrl: sourceUrl.trim(),
      targetVersion: targetVersion.value ?? '',
    };
    if (catalogVersion?.value) inputs.catalogVersion = catalogVersion.value;

    // Track the inputs used for this run
    setResultInputs({
      accountId1: accountId1.trim(),
      clusterArn: clusterArn.trim(),
      accountId2: accountId2.trim(),
      appci: appci.trim(),
      sourceUrl: sourceUrl.trim(),
      targetVersion: targetVersion.value ?? '',
    });

    try {
      const result = await triggerCodeAnalysis(inputs);
      startPolling(result.runId);
    } catch (err) {
      setRunStatus('failed');
      setProgress({
        runId: '',
        timestamp: new Date().toISOString(),
        percentage: 0,
        currentActivity: err instanceof Error ? err.message : 'Failed to start analysis.',
        elapsedSeconds: 0,
        agentType: 'code-analysis',
      });
    }
  };

  /* ── Load findings for a selected historical run ── */
  const handleRunSelect = async (option: SelectProps.Option) => {
    setSelectedRun(option);
    if (!option.value) return;

    setCluster(null);
    setResources([]);
    setFindings([]);
    setReportUrl(undefined);

    const resp = await getCodeAnalysisFindings(option.value);
    applyFindingsResponse(resp);
    setRunStatus('complete');

    // Try to get inputs from the run history
    const runs = await getCodeAnalysisRuns();
    const run = runs.find((r) => r.runId === option.value);
    if (run?.inputs) {
      setResultInputs({
        accountId1: run.inputs.accountId1 ?? '',
        clusterArn: run.inputs.clusterArn ?? '',
        accountId2: run.inputs.accountId2 ?? '',
        appci: run.inputs.appci ?? '',
        sourceUrl: run.inputs.sourceUrl ?? '',
        targetVersion: run.inputs.targetVersion ?? '',
      });
    }
  };

  return (
    <SpaceBetween size="l">
      {/* Input Form */}
      <Container header={<Header variant="h3">Code Analysis — Input</Header>}>
        <SpaceBetween size="l">
          <ColumnLayout columns={2}>
            <FormField label="Account ID 1 (Source Cluster Account)">
              <Input value={accountId1} onChange={({ detail }) => setAccountId1(detail.value)} placeholder="123456789012" />
            </FormField>
            <FormField label="Source Cluster ARN">
              <Input value={clusterArn} onChange={({ detail }) => setClusterArn(detail.value)} placeholder="arn:aws:rds:region:account:cluster:name" />
            </FormField>
            <FormField label="Account ID 2 (Application Account)">
              <Input value={accountId2} onChange={({ detail }) => setAccountId2(detail.value)} placeholder="123456789012" />
            </FormField>
            <FormField label="AppCI Tag">
              <Input value={appci} onChange={({ detail }) => setAppci(detail.value)} placeholder="application-tag" />
            </FormField>
            <FormField label="Source Code URL (GitHub or S3)">
              <Input value={sourceUrl} onChange={({ detail }) => setSourceUrl(detail.value)} placeholder="https://github.com/org/repo or s3://bucket/path" />
            </FormField>
            <FormField label="Target Database Engine Version (optional — auto-detected from cluster)">
              <Select
                selectedOption={targetVersion}
                onChange={({ detail }) => setTargetVersion(detail.selectedOption)}
                options={targetVersionOptions}
                filteringType="auto"
                placeholder="Auto-detect from cluster"
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
              Run Code Analysis
            </Button>
            <RunStatusPill status={runStatus} />
          </SpaceBetween>
        </SpaceBetween>
      </Container>

      {/* Run History — separate section below input */}
      {runHistoryOptions.length > 0 && (
        <Container header={<Header variant="h3">Run History</Header>}>
          <FormField label="Select a previous run to view results">
            <Select
              selectedOption={selectedRun}
              onChange={({ detail }) => handleRunSelect(detail.selectedOption)}
              options={runHistoryOptions}
              placeholder="Select a previous run"
            />
          </FormField>
        </Container>
      )}

      {/* Progress */}
      {runStatus === 'running' && progress && (
        <Container header={<Header variant="h3">Analysis Progress</Header>}>
          <ProgressBar progress={progress} />
        </Container>
      )}

      {/* Source Cluster Details */}
      {cluster && <ClusterInfo cluster={cluster} />}

      {/* Connected Resources */}
      {resources.length > 0 && (
        <Table
          header={<Header variant="h3">Connected Resources{resultInputs?.accountId2 ? ` (Account ${resultInputs.accountId2})` : ''}</Header>}
          columnDefinitions={[
            { id: 'type', header: 'Resource Type', cell: (r) => <Badge color="blue">{r.type}</Badge>, width: 140 },
            { id: 'id', header: 'Resource ID', cell: (r) => r.id },
            { id: 'method', header: 'Connection Method', cell: (r) => r.connectionMethod },
            { id: 'status', header: 'Status', cell: (r) => <Badge color="green">{r.status}</Badge>, width: 100 },
          ]}
          items={resources}
          variant="container"
          sortingDisabled
          empty="No connected resources found"
        />
      )}

      {/* Findings */}
      {(findings.length > 0 || runStatus === 'complete') && (
        <Container
          header={
            <Header
              variant="h3"
              actions={
                reportUrl ? (
                  <Link href={reportUrl} variant="primary" external>
                    Download Report
                  </Link>
                ) : undefined
              }
            >
              Findings
            </Header>
          }
        >
          <SpaceBetween size="m">
            {/* Show the inputs used for this run */}
            {resultInputs && (
              <ColumnLayout columns={3} variant="text-grid">
                <div>
                  <Box variant="awsui-key-label">Account ID 1</Box>
                  <Box>{resultInputs.accountId1 || '—'}</Box>
                </div>
                <div>
                  <Box variant="awsui-key-label">Cluster ARN</Box>
                  <Box variant="small">{resultInputs.clusterArn || '—'}</Box>
                </div>
                <div>
                  <Box variant="awsui-key-label">Account ID 2</Box>
                  <Box>{resultInputs.accountId2 || '—'}</Box>
                </div>
                <div>
                  <Box variant="awsui-key-label">AppCI</Box>
                  <Box>{resultInputs.appci || '—'}</Box>
                </div>
                <div>
                  <Box variant="awsui-key-label">Source URL</Box>
                  <Box variant="small">{resultInputs.sourceUrl || '—'}</Box>
                </div>
                <div>
                  <Box variant="awsui-key-label">Target Version</Box>
                  <Box>{resultInputs.targetVersion || 'Auto-detected from cluster'}</Box>
                </div>
              </ColumnLayout>
            )}
            {findings.length > 0 ? (
              <FindingsTable findings={findings} title="Code Analysis Findings" />
            ) : (
              <Box textAlign="center" color="text-body-secondary" padding="l">
                No findings — code is compatible with the target version.
              </Box>
            )}
          </SpaceBetween>
        </Container>
      )}
    </SpaceBetween>
  );
}
