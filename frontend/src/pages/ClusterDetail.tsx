import { useState, useEffect } from 'react';
import { useParams, useNavigate, useSearchParams } from 'react-router-dom';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Container from '@cloudscape-design/components/container';
import Header from '@cloudscape-design/components/header';
import Tabs from '@cloudscape-design/components/tabs';
import Table from '@cloudscape-design/components/table';
import Box from '@cloudscape-design/components/box';
import Badge from '@cloudscape-design/components/badge';
import Button from '@cloudscape-design/components/button';
import ColumnLayout from '@cloudscape-design/components/column-layout';
import StatusIndicator from '@cloudscape-design/components/status-indicator';
import Spinner from '@cloudscape-design/components/spinner';
import Alert from '@cloudscape-design/components/alert';
import Modal from '@cloudscape-design/components/modal';
import Select, { SelectProps } from '@cloudscape-design/components/select';
import FormField from '@cloudscape-design/components/form-field';
import ExpandableSection from '@cloudscape-design/components/expandable-section';
import Input from '@cloudscape-design/components/input';
import Link from '@cloudscape-design/components/link';
import {
  getClusterAnalysis,
  getUpgradeTestPreflight,
  triggerUpgradeTest,
  getDeprecationCatalog,
  getCodeAnalysisRuns,
  getCodeAnalysisFindings,
  getClusterAnalysisRuns,
  getClusterAnalysisRun,
} from '../services/api';
import type {
  ClusterAnalysis,
  StoredProcedure,
  ProfiledQuery,
  ParameterSetting,
  UpgradeTestPreflight,
  Finding,
} from '../types';

/* ── Severity helpers ── */

function sevBadge(sev: string) {
  if (sev === 'HIGH' || sev === 'error') return <Badge color="red">HIGH</Badge>;
  if (sev === 'MEDIUM' || sev === 'warning') return <Badge color="grey">MEDIUM</Badge>;
  return <Badge color="blue">LOW</Badge>;
}

function compatBadge(compat: string) {
  if (compat === 'removed') return <StatusIndicator type="error">Removed in target</StatusIndicator>;
  if (compat === 'changed') return <StatusIndicator type="warning">Default changed</StatusIndicator>;
  return <StatusIndicator type="success">Compatible</StatusIndicator>;
}

/* ── Tab: Code Analysis (pulls last run findings) ── */

function CodeAnalysisTab({ clusterArn }: { clusterArn: string }) {
  const navigate = useNavigate();
  const [findings, setFindings] = useState<Finding[]>([]);
  const [loading, setLoading] = useState(true);
  const [hasRun, setHasRun] = useState(false);
  const [runTime, setRunTime] = useState('');
  const [runInputs, setRunInputs] = useState<{ accountId1: string; clusterArn: string; sourceUrl: string }>({ accountId1: '', clusterArn: '', sourceUrl: '' });

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const runs = await getCodeAnalysisRuns();
        const match = runs.find(r => r.status === 'complete' && r.inputs?.clusterArn === clusterArn);
        if (match) {
          setHasRun(true);
          setRunTime(new Date(match.startTime).toLocaleString());
          setRunInputs({
            accountId1: match.inputs?.accountId1 ?? '',
            clusterArn: match.inputs?.clusterArn ?? '',
            sourceUrl: match.inputs?.sourceUrl ?? '',
          });
          const resp = await getCodeAnalysisFindings(match.runId);
          if (!cancelled) setFindings(resp.findings);
        }
      } catch { /* ignore */ }
      if (!cancelled) setLoading(false);
    }
    load();
    return () => { cancelled = true; };
  }, [clusterArn]);

  if (loading) return <Box textAlign="center" padding="l"><Spinner /> Loading code analysis...</Box>;

  const errors = findings.filter(f => f.severity === 'error').length;
  const warnings = findings.filter(f => f.severity === 'warning').length;

  return (
    <SpaceBetween size="l">
      <Link onFollow={() => navigate('/code-analysis')}>View full Code Analysis page →</Link>
      <Container header={<Header variant="h3">Findings</Header>}>
        {!hasRun ? (
          <Box textAlign="center" color="text-body-secondary" padding="l">
            No code analysis has been run for this cluster yet. Go to the Code Analysis page to run one.
          </Box>
        ) : (
          <SpaceBetween size="m">
            <Box variant="small" color="text-body-secondary">Last run: {runTime}</Box>
            <ColumnLayout columns={3} variant="text-grid">
              <div><Box variant="awsui-key-label">Account ID</Box><Box variant="small">{runInputs.accountId1 || '—'}</Box></div>
              <div><Box variant="awsui-key-label">Cluster ARN</Box><Box variant="small">{runInputs.clusterArn || '—'}</Box></div>
              <div><Box variant="awsui-key-label">Source Code URL</Box><Box variant="small">{runInputs.sourceUrl || '—'}</Box></div>
            </ColumnLayout>
            <ColumnLayout columns={4} variant="text-grid">
              <div><Box variant="awsui-key-label">Total Findings</Box><Box variant="awsui-value-large">{findings.length}</Box></div>
              <div><Box variant="awsui-key-label">Errors</Box><Box variant="awsui-value-large" color={errors > 0 ? 'text-status-error' : undefined}>{errors}</Box></div>
              <div><Box variant="awsui-key-label">Warnings</Box><Box variant="awsui-value-large" color={warnings > 0 ? 'text-status-warning' : undefined}>{warnings}</Box></div>
              <div><Box variant="awsui-key-label">Info</Box><Box variant="awsui-value-large">{findings.length - errors - warnings}</Box></div>
            </ColumnLayout>
            {findings.length > 0 && (
              <Table
                columnDefinitions={[
                  { id: 'sev', header: 'Severity', cell: (f: Finding) => sevBadge(f.severity), width: 90 },
                  { id: 'cat', header: 'Category', cell: (f: Finding) => <Badge color="blue">{f.category}</Badge>, width: 130 },
                  { id: 'file', header: 'File', cell: (f: Finding) => <Box variant="small">{f.fileName}:{f.lineNumber}</Box>, width: 200 },
                  { id: 'desc', header: 'Issue', cell: (f: Finding) => f.codeSnippet },
                  { id: 'fix', header: 'Remediation', cell: (f: Finding) => <Box variant="small" color="text-body-secondary">{f.recommendedChange}</Box> },
                ]}
                items={findings.slice(0, 20)}
                variant="embedded"
                sortingDisabled
                empty="No findings"
              />
            )}
            {findings.length > 20 && (
              <Box variant="small" color="text-body-secondary">Showing first 20 of {findings.length} findings. View full details on the Code Analysis page.</Box>
            )}
          </SpaceBetween>
        )}
      </Container>
    </SpaceBetween>
  );
}

/* ── Tab: Cluster Analysis (pulls last run compatibility findings) ── */

function ClusterAnalysisTab({ clusterArn }: { clusterArn: string }) {
  const navigate = useNavigate();
  const [data, setData] = useState<ClusterAnalysis | null>(null);
  const [loading, setLoading] = useState(true);
  const [hasRun, setHasRun] = useState(false);
  const [runTime, setRunTime] = useState('');

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const runs = await getClusterAnalysisRuns();
        const match = runs.find(r => r.clusterArn === clusterArn);
        if (match) {
          setHasRun(true);
          setRunTime(new Date(match.startTime).toLocaleString());
          const result = await getClusterAnalysisRun(match.runId);
          if (!cancelled) setData(result);
        }
      } catch { /* ignore */ }
      if (!cancelled) setLoading(false);
    }
    load();
    return () => { cancelled = true; };
  }, [clusterArn]);

  if (loading) return <Box textAlign="center" padding="l"><Spinner /> Loading cluster analysis...</Box>;

  const breakingChanges = data?.breakingChanges ?? [];
  const highCount = breakingChanges.filter(bc => bc.severity === 'error').length;
  const medCount = breakingChanges.filter(bc => bc.severity === 'warning').length;
  const paramFindings = (data?.parameterSettings ?? []).filter(p => p.compatibility !== 'ok');

  return (
    <SpaceBetween size="l">
      <Link onFollow={() => navigate(`/cluster-analysis?clusterArn=${encodeURIComponent(clusterArn)}`)}>View full Cluster Analysis page →</Link>

      {!hasRun ? (
        <Container header={<Header variant="h3">Compatibility Analysis</Header>}>
          <Box textAlign="center" color="text-body-secondary" padding="l">
            No cluster analysis has been run for this cluster yet. Go to the Cluster Analysis page to run one.
          </Box>
        </Container>
      ) : (
        <SpaceBetween size="l">
          <Container header={<Header variant="h3">Compatibility Analysis</Header>}>
            <SpaceBetween size="m">
              <Box variant="small" color="text-body-secondary">Last run: {runTime}</Box>
              <ColumnLayout columns={4} variant="text-grid">
                <div><Box variant="awsui-key-label">Engine</Box><Box>{data?.engine === 'aurora-mysql' ? 'Aurora MySQL' : 'Aurora PostgreSQL'}</Box></div>
                <div><Box variant="awsui-key-label">Current → Target</Box><Box>{data?.currentVersion} → {data?.targetVersion}</Box></div>
                <div><Box variant="awsui-key-label">Analyzed</Box><Box variant="small">{data?.collectedAt ? new Date(data.collectedAt).toLocaleString() : '—'}</Box></div>
                <div />
              </ColumnLayout>
            </SpaceBetween>
          </Container>

          <Container header={<Header variant="h3">Risk Summary</Header>}>
            <ColumnLayout columns={4} variant="text-grid">
              <div><Box variant="awsui-key-label">Breaking Changes</Box><Box variant="awsui-value-large">{breakingChanges.length}</Box></div>
              <div><Box variant="awsui-key-label">High Severity</Box><Box variant="awsui-value-large" color={highCount > 0 ? 'text-status-error' : undefined}>{highCount}</Box></div>
              <div><Box variant="awsui-key-label">Medium Severity</Box><Box variant="awsui-value-large" color={medCount > 0 ? 'text-status-warning' : undefined}>{medCount}</Box></div>
              <div><Box variant="awsui-key-label">Parameter Issues</Box><Box variant="awsui-value-large">{paramFindings.length}</Box></div>
            </ColumnLayout>
          </Container>

          {breakingChanges.length > 0 && (
            <Table
              header={<Header variant="h3">Breaking Changes</Header>}
              columnDefinitions={[
                { id: 'severity', header: 'Severity', cell: bc => sevBadge(bc.severity), width: 100 },
                { id: 'category', header: 'Category', cell: bc => <Badge>{bc.category}</Badge>, width: 140 },
                { id: 'description', header: 'Description', cell: bc => bc.description },
                { id: 'remediation', header: 'Remediation', cell: bc => bc.remediation ?? '—', width: 300 },
              ]}
              items={breakingChanges.slice(0, 10)}
              variant="container"
              sortingDisabled
            />
          )}
          {breakingChanges.length > 10 && (
            <Box variant="small" color="text-body-secondary">Showing first 10 of {breakingChanges.length}. View full details on the Cluster Analysis page.</Box>
          )}
        </SpaceBetween>
      )}
    </SpaceBetween>
  );
}

/* ── Tab: Stored Procedures & Config ── */

function SpTab({ data }: { data: ClusterAnalysis }) {
  const [expandedSp, setExpandedSp] = useState<string[]>([]);
  const incompatParams = data.parameterSettings.filter(p => p.compatibility !== 'ok');
  const okParams = data.parameterSettings.filter(p => p.compatibility === 'ok');

  return (
    <SpaceBetween size="l">
      <ColumnLayout columns={4} variant="text-grid">
        <div><Box variant="awsui-key-label">Stored Procedures</Box><Box variant="awsui-value-large">{data.spSummary.total}</Box></div>
        <div><Box variant="awsui-key-label">With Findings</Box><Box variant="awsui-value-large" color={data.spSummary.withFindings > 0 ? 'text-status-warning' : undefined}>{data.spSummary.withFindings}</Box></div>
        <div><Box variant="awsui-key-label">HIGH Issues</Box><Box variant="awsui-value-large" color={data.spSummary.high > 0 ? 'text-status-error' : undefined}>{data.spSummary.high}</Box></div>
        <div><Box variant="awsui-key-label">MEDIUM Issues</Box><Box variant="awsui-value-large">{data.spSummary.medium}</Box></div>
      </ColumnLayout>
      {incompatParams.length > 0 && (
        <Container header={<Header variant="h3">Parameter Group — Incompatible Settings ({incompatParams.length})</Header>}>
          <Table
            columnDefinitions={[
              { id: 'name', header: 'Parameter', cell: (p: ParameterSetting) => <Box fontWeight="bold">{p.name}</Box>, width: 240 },
              { id: 'value', header: 'Current Value', cell: (p: ParameterSetting) => p.currentValue, width: 140 },
              { id: 'compat', header: 'Compatibility', cell: (p: ParameterSetting) => compatBadge(p.compatibility), width: 180 },
              { id: 'note', header: 'Action Required', cell: (p: ParameterSetting) => <Box variant="small" color="text-body-secondary">{p.note}</Box> },
            ]}
            items={incompatParams}
            variant="embedded"
            sortingDisabled
          />
        </Container>
      )}
      {okParams.length > 0 && (
        <ExpandableSection headerText={`Other non-default parameters (${okParams.length}) — compatible`} variant="footer">
          <Table
            columnDefinitions={[
              { id: 'name', header: 'Parameter', cell: (p: ParameterSetting) => p.name, width: 240 },
              { id: 'value', header: 'Current Value', cell: (p: ParameterSetting) => p.currentValue },
            ]}
            items={okParams}
            variant="embedded"
            sortingDisabled
          />
        </ExpandableSection>
      )}
      <Container header={<Header variant="h3">Stored Procedures & Functions</Header>}>
        {data.storedProcedures.length === 0 ? (
          <Box color="text-body-secondary" padding="m">No stored procedures found, or direct DB access required to retrieve them.</Box>
        ) : (
          <SpaceBetween size="s">
            {data.storedProcedures.map((sp: StoredProcedure) => (
              <ExpandableSection
                key={`${sp.schema}.${sp.name}`}
                headerText={`${sp.schema}.${sp.name} (${sp.type})` + (sp.findings.length > 0 ? ` — ${sp.findings.length} finding(s)` : ' — compatible')}
                variant="container"
                expanded={expandedSp.includes(sp.name)}
                onChange={({ detail }) => setExpandedSp(detail.expanded ? [...expandedSp, sp.name] : expandedSp.filter(n => n !== sp.name))}
              >
                <SpaceBetween size="s">
                  {sp.findings.length > 0 && (
                    <Table
                      columnDefinitions={[
                        { id: 'sev', header: 'Severity', cell: f => sevBadge(f.severity), width: 90 },
                        { id: 'cat', header: 'Category', cell: f => <Badge color="blue">{f.category}</Badge>, width: 130 },
                        { id: 'desc', header: 'Issue', cell: f => f.description },
                        { id: 'fix', header: 'Remediation', cell: f => <Box variant="small" color="text-body-secondary">{f.remediation}</Box> },
                      ]}
                      items={sp.findings}
                      variant="embedded"
                      sortingDisabled
                    />
                  )}
                  <Box variant="code">{sp.body}</Box>
                </SpaceBetween>
              </ExpandableSection>
            ))}
          </SpaceBetween>
        )}
      </Container>
    </SpaceBetween>
  );
}

/* ── Tab: Query Profile ── */

function QueryTab({ data }: { data: ClusterAnalysis }) {
  return (
    <SpaceBetween size="l">
      <ColumnLayout columns={4} variant="text-grid">
        <div><Box variant="awsui-key-label">Unique Queries</Box><Box variant="awsui-value-large">{data.querySummary.total}</Box></div>
        <div><Box variant="awsui-key-label">With Findings</Box><Box variant="awsui-value-large" color={data.querySummary.withFindings > 0 ? 'text-status-warning' : undefined}>{data.querySummary.withFindings}</Box></div>
        <div><Box variant="awsui-key-label">HIGH Issues</Box><Box variant="awsui-value-large" color={data.querySummary.high > 0 ? 'text-status-error' : undefined}>{data.querySummary.high}</Box></div>
        <div><Box variant="awsui-key-label">MEDIUM Issues</Box><Box variant="awsui-value-large">{data.querySummary.medium}</Box></div>
      </ColumnLayout>
      <Alert type="info">Query profiles are collected from Performance Schema / stats tables (read-only). Queries are analyzed against the deprecation catalog for compatibility issues.</Alert>
      <Table
        header={<Header variant="h3">Profiled Queries</Header>}
        columnDefinitions={[
          { id: 'digest', header: 'Query Digest', cell: (q: ProfiledQuery) => (
            <SpaceBetween size="xxs">
              <Box variant="code" fontSize="body-s">{q.digest.slice(0, 80)}{q.digest.length > 80 ? '…' : ''}</Box>
              <Box variant="small" color="text-body-secondary">Schema: {q.schema}</Box>
            </SpaceBetween>
          )},
          { id: 'calls', header: 'Calls', cell: (q: ProfiledQuery) => q.callCount.toLocaleString(), width: 90 },
          { id: 'avg', header: 'Avg Latency', cell: (q: ProfiledQuery) => `${q.avgLatencyMs.toFixed(1)} ms`, width: 110 },
          { id: 'max', header: 'Max Latency', cell: (q: ProfiledQuery) => `${q.maxLatencyMs.toFixed(0)} ms`, width: 110 },
          { id: 'findings', header: 'Findings', cell: (q: ProfiledQuery) => q.findings.length === 0
            ? <StatusIndicator type="success">Compatible</StatusIndicator>
            : (
              <SpaceBetween size="xxs">
                {q.findings.map((f, i) => (
                  <SpaceBetween key={i} direction="horizontal" size="xs">
                    {sevBadge(f.severity)}
                    <Box variant="small">{f.description.slice(0, 60)}…</Box>
                  </SpaceBetween>
                ))}
              </SpaceBetween>
            ), width: 320 },
        ]}
        items={data.profiledQueries}
        variant="container"
        sortingDisabled
        empty={<Box color="text-body-secondary" padding="m">No profiled queries available. Enable Performance Schema to collect query data.</Box>}
      />
    </SpaceBetween>
  );
}

/* ── Tab: Upgrade Test ── */

interface UpgradeTestTabProps {
  data: ClusterAnalysis;
  onTestStarted: (runId: string) => void;
}

function UpgradeTestTab({ data, onTestStarted }: UpgradeTestTabProps) {
  const [showModal, setShowModal] = useState(false);
  const [preflight, setPreflight] = useState<UpgradeTestPreflight | null>(null);
  const [preflightLoading, setPreflightLoading] = useState(false);
  const [preflightError, setPreflightError] = useState<string | null>(null);
  const [strategy, setStrategy] = useState<SelectProps.Option>({ value: 'in-place', label: 'In-place (rolling restart, 3–5 min downtime)' });
  const [cloneMode, setCloneMode] = useState<SelectProps.Option>({ value: 'clone', label: 'Clone (fast point-in-time copy — recommended)' });
  const [snapshotId, setSnapshotId] = useState('');
  const [snapshotAccount, setSnapshotAccount] = useState('');
  const [snapshotRoleArn, setSnapshotRoleArn] = useState('');
  const [snapshotKmsKey, setSnapshotKmsKey] = useState('');
  const [starting, setStarting] = useState(false);
  const [targetVersionOption, setTargetVersionOption] = useState<SelectProps.Option>({ value: data.targetVersion, label: data.targetVersion });
  const [versionOptions, setVersionOptions] = useState<SelectProps.Option[]>([{ value: data.targetVersion, label: data.targetVersion }]);
  const [versionsLoading, setVersionsLoading] = useState(false);

  const strategyOptions: SelectProps.Option[] = [
    { value: 'in-place', label: 'In-place (rolling restart, 3–5 min downtime)' },
    { value: 'blue-green', label: 'Blue/Green (near-zero downtime cutover)' },
  ];
  const cloneModeOptions: SelectProps.Option[] = [
    { value: 'clone', label: 'Clone (fast point-in-time copy — recommended)' },
    { value: 'snapshot', label: 'Restore from snapshot (provide snapshot details below)' },
  ];
  const isSnapshotMode = cloneMode.value === 'snapshot' && strategy.value !== 'blue-green';
  const canStart = !preflightLoading && !preflightError && !!targetVersionOption.value &&
    (cloneMode.value === 'clone' || strategy.value === 'blue-green' || snapshotId.trim() !== '');

  const loadVersionsFromCatalog = async () => {
    setVersionsLoading(true);
    try {
      const catalog = await getDeprecationCatalog();
      if (!catalog?.catalog) return;
      const engineKey = data.engine.includes('postgresql') ? 'aurora-postgresql' : 'aurora-mysql';
      const engineData = catalog.catalog.engines[engineKey];
      if (!engineData) return;
      const entry = engineData.versions.find(v => v.version === data.currentVersion);
      if (!entry || entry.upgradeTargets.length === 0) return;
      const targetSet = new Set(entry.upgradeTargets);
      const validTargets = engineData.versions
        .filter(v => targetSet.has(v.version) && v.status === 'available')
        .map(v => ({ value: v.version, label: v.version }));
      if (validTargets.length === 0) return;
      setVersionOptions(validTargets);
      const preferred = validTargets[validTargets.length - 1];
      setTargetVersionOption(preferred);
      return preferred.value;
    } catch { /* fallback */ } finally { setVersionsLoading(false); }
  };

  const handleOpenModal = async () => {
    setShowModal(true);
    const resolvedVersion = await loadVersionsFromCatalog();
    const versionToUse = resolvedVersion ?? data.targetVersion;
    setPreflightLoading(true);
    setPreflightError(null);
    try {
      const pf = await getUpgradeTestPreflight(data.clusterArn, versionToUse);
      setPreflight(pf);
      setStrategy({ value: pf.upgradeStrategy, label: pf.upgradeStrategy === 'blue-green' ? 'Blue/Green (near-zero downtime cutover)' : 'In-place (rolling restart, 3–5 min downtime)' });
    } catch (e) { setPreflightError(e instanceof Error ? e.message : 'Failed to load estimate'); }
    finally { setPreflightLoading(false); }
  };

  const handleVersionChange = async (option: SelectProps.Option) => {
    setTargetVersionOption(option);
    setPreflight(null);
    setPreflightLoading(true);
    setPreflightError(null);
    try {
      const pf = await getUpgradeTestPreflight(data.clusterArn, option.value ?? data.targetVersion);
      setPreflight(pf);
      setStrategy({ value: pf.upgradeStrategy, label: pf.upgradeStrategy === 'blue-green' ? 'Blue/Green (near-zero downtime cutover)' : 'In-place (rolling restart, 3–5 min downtime)' });
    } catch (e) { setPreflightError(e instanceof Error ? e.message : 'Failed to load estimate'); }
    finally { setPreflightLoading(false); }
  };

  const handleStart = async () => {
    if (!preflight) return;
    setStarting(true);
    try {
      const result = await triggerUpgradeTest(
        data.clusterArn, targetVersionOption.value ?? data.targetVersion,
        strategy.value as 'in-place' | 'blue-green',
        strategy.value === 'blue-green' ? 'clone' : cloneMode.value as 'clone' | 'snapshot',
        isSnapshotMode ? { snapshotId: snapshotId.trim(), snapshotAccount: snapshotAccount.trim() || undefined, snapshotRoleArn: snapshotRoleArn.trim() || undefined, snapshotKmsKey: snapshotKmsKey.trim() || undefined } : undefined,
      );
      setShowModal(false);
      onTestStarted(result.runId);
    } catch (e) { setPreflightError(e instanceof Error ? e.message : 'Failed to start test'); setStarting(false); }
  };

  const downtimeLabel = strategy.value === 'blue-green' ? '< 30 seconds (cutover)' : '3–5 minutes (rolling restart)';

  return (
    <SpaceBetween size="l">
      <Container header={<Header variant="h3">What the Upgrade Test Does</Header>}>
        <SpaceBetween size="s">
          {[
            { n: 1, title: 'Baseline Capture', desc: 'Records top queries from performance_schema / pg_stat_statements and parameter group state.' },
            { n: 2, title: 'Compatibility Analysis', desc: 'Cross-references breaking changes from the deprecation catalog with AI-powered risk assessment.' },
            { n: 3, title: 'Clone / Green Environment', desc: 'In-place: fast point-in-time clone or snapshot restore. Blue/Green: RDS provisions green automatically.' },
            { n: 4, title: 'Upgrade + Query Replay', desc: `Upgrades to ${targetVersionOption.value ?? data.targetVersion}. Replays baseline queries in a loop to measure outage and failures.` },
            { n: 5, title: 'Post-Upgrade Validation', desc: 'Replays queries post-upgrade to detect failures and latency regressions vs baseline.' },
            { n: 6, title: 'Report', desc: 'Generates a prioritized action item checklist and upgrade readiness verdict.' },
          ].map(step => (
            <Box key={step.n}>
              <Box variant="span" fontWeight="bold" color="text-status-info">{step.n}. {step.title}: </Box>
              <Box variant="span" color="text-body-secondary">{step.desc}</Box>
            </Box>
          ))}
        </SpaceBetween>
      </Container>
      <Button variant="primary" iconName="settings" onClick={handleOpenModal}>Run Upgrade Test on Cloned Cluster</Button>
      {showModal && (
        <Modal visible onDismiss={() => setShowModal(false)} size="large" header="Upgrade Test — Configuration"
          footer={<Box float="right"><SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={() => setShowModal(false)} disabled={starting}>Cancel</Button>
            <Button variant="primary" onClick={handleStart} loading={starting} disabled={!canStart}>Confirm & Start Test</Button>
          </SpaceBetween></Box>}>
          <SpaceBetween size="l">
            <FormField label="Target Version" description="Upgrade targets sourced from the deprecation catalog.">
              <Select selectedOption={targetVersionOption} onChange={({ detail }) => handleVersionChange(detail.selectedOption)} options={versionOptions} statusType={versionsLoading ? 'loading' : 'finished'} loadingText="Loading versions..." disabled={versionsLoading || starting} />
            </FormField>
            {preflightLoading && <Box textAlign="center" padding="xl"><Spinner size="large" /><Box variant="p" color="text-body-secondary" margin={{ top: 's' }}>Calculating estimate...</Box></Box>}
            {preflightError && <Alert type="error">{preflightError}</Alert>}
            {!preflightLoading && preflight && (
              <SpaceBetween size="l">
                {preflight.warnings.map((w, i) => <Alert key={i} type="warning">{w}</Alert>)}
                <ColumnLayout columns={3} variant="text-grid">
                  <div><Box variant="awsui-key-label">Current Version</Box><Box fontWeight="bold">{preflight.currentVersion}</Box></div>
                  <div><Box variant="awsui-key-label">Est. Duration</Box><Box>{preflight.estimatedDurationMinutes} min</Box></div>
                  <div><Box variant="awsui-key-label">Est. Cost</Box><Box fontWeight="bold">${preflight.estimatedCostUsd.toFixed(2)}</Box></div>
                </ColumnLayout>
                <FormField label="Upgrade Strategy" description="Auto-selected based on version delta.">
                  <Select selectedOption={strategy} onChange={({ detail }) => setStrategy(detail.selectedOption)} options={strategyOptions} />
                </FormField>
                <ColumnLayout columns={2} variant="text-grid">
                  <div><Box variant="awsui-key-label">Estimated Downtime</Box><Box fontWeight="bold" color={strategy.value === 'blue-green' ? 'text-status-success' : 'text-status-warning'}>{downtimeLabel}</Box></div>
                  <div><Box variant="awsui-key-label">Incompatibilities</Box><Box><Box variant="span" color="text-status-error">{preflight.breakingChangeSummary.high} HIGH</Box>{' · '}<Box variant="span" color="text-status-warning">{preflight.breakingChangeSummary.medium} MEDIUM</Box>{' · '}{preflight.breakingChangeSummary.low} LOW</Box></div>
                </ColumnLayout>
                {strategy.value !== 'blue-green' && (
                  <FormField label="Clone Source"><Select selectedOption={cloneMode} onChange={({ detail }) => setCloneMode(detail.selectedOption)} options={cloneModeOptions} /></FormField>
                )}
                {isSnapshotMode && (
                  <SpaceBetween size="m">
                    <FormField label="Snapshot Identifier" constraintText="Required"><Input value={snapshotId} onChange={({ detail }) => setSnapshotId(detail.value)} placeholder="rds:my-cluster-snap or arn:aws:rds:..." /></FormField>
                    <ColumnLayout columns={2}>
                      <FormField label="Source Account ID"><Input value={snapshotAccount} onChange={({ detail }) => setSnapshotAccount(detail.value)} placeholder="123456789012" /></FormField>
                      <FormField label="Cross-Account Role ARN"><Input value={snapshotRoleArn} onChange={({ detail }) => setSnapshotRoleArn(detail.value)} placeholder="arn:aws:iam::123456789012:role/..." /></FormField>
                    </ColumnLayout>
                    <FormField label="KMS Key ARN (optional)"><Input value={snapshotKmsKey} onChange={({ detail }) => setSnapshotKmsKey(detail.value)} placeholder="arn:aws:kms:..." /></FormField>
                  </SpaceBetween>
                )}
                {strategy.value === 'blue-green' && <Alert type="info">Blue/Green creates a green environment automatically — no snapshot needed. Logical replication is enabled and restored automatically.</Alert>}
                {strategy.value !== 'blue-green' && cloneMode.value === 'clone' && <Alert type="info">The test runs on a <strong>point-in-time clone</strong> — your production database is never modified. The clone is deleted after the test.</Alert>}
              </SpaceBetween>
            )}
          </SpaceBetween>
        </Modal>
      )}
    </SpaceBetween>
  );
}

/* ── Tab: Readiness Report ── */

function ReportTab({ data }: { data: ClusterAnalysis }) {
  const allFindings = [
    ...data.storedProcedures.flatMap(sp => sp.findings.map(f => ({ ...f, source: `SP: ${sp.schema}.${sp.name}` }))),
    ...data.profiledQueries.flatMap(q => q.findings.map(f => ({ ...f, source: `Query: ${q.digest.slice(0, 40)}…` }))),
    ...data.parameterSettings.filter(p => p.compatibility !== 'ok').map(p => ({
      severity: 'HIGH' as const, category: 'config' as const,
      description: p.note ?? `Parameter ${p.name} incompatible`,
      remediation: p.note ?? '', source: `Parameter: ${p.name}`, line: 0, snippet: '',
    })),
  ];
  const high = allFindings.filter(f => f.severity === 'HIGH');
  const medium = allFindings.filter(f => f.severity === 'MEDIUM');
  const low = allFindings.filter(f => f.severity === 'LOW');
  const verdict = high.length === 0
    ? { type: 'success' as const, label: 'Ready to Upgrade', desc: 'No HIGH severity issues found. Review MEDIUM items before proceeding.' }
    : { type: 'error' as const, label: 'Not Ready', desc: `${high.length} HIGH severity issue(s) must be resolved before upgrading.` };

  return (
    <SpaceBetween size="l">
      <Container header={<Header variant="h3">Upgrade Readiness Verdict</Header>}>
        <SpaceBetween size="m">
          <StatusIndicator type={verdict.type}>{verdict.label}</StatusIndicator>
          <Box color="text-body-secondary">{verdict.desc}</Box>
          <ColumnLayout columns={3} variant="text-grid">
            <div><Box variant="awsui-key-label">HIGH (must fix)</Box><Box variant="awsui-value-large" color={high.length > 0 ? 'text-status-error' : undefined}>{high.length}</Box></div>
            <div><Box variant="awsui-key-label">MEDIUM (should fix)</Box><Box variant="awsui-value-large" color={medium.length > 0 ? 'text-status-warning' : undefined}>{medium.length}</Box></div>
            <div><Box variant="awsui-key-label">LOW (informational)</Box><Box variant="awsui-value-large">{low.length}</Box></div>
          </ColumnLayout>
        </SpaceBetween>
      </Container>
      <Table
        header={<Header variant="h3">Prioritized Action Items</Header>}
        columnDefinitions={[
          { id: 'priority', header: 'Priority', cell: (f: typeof allFindings[0]) => sevBadge(f.severity), width: 90 },
          { id: 'source', header: 'Source', cell: (f: typeof allFindings[0]) => <Box variant="small">{f.source}</Box>, width: 200 },
          { id: 'category', header: 'Category', cell: (f: typeof allFindings[0]) => <Badge color="blue">{f.category}</Badge>, width: 130 },
          { id: 'issue', header: 'Issue', cell: (f: typeof allFindings[0]) => f.description },
          { id: 'fix', header: 'Fix', cell: (f: typeof allFindings[0]) => <Box variant="small" color="text-body-secondary">{f.remediation}</Box> },
        ]}
        items={[...high, ...medium, ...low]}
        variant="container"
        sortingDisabled
        empty={<Box color="text-body-secondary" padding="m">No issues found — cluster appears compatible with target version.</Box>}
      />
    </SpaceBetween>
  );
}

/* ── Main page ── */

export default function ClusterDetail() {
  const { clusterArn: encodedArn } = useParams<{ clusterArn: string }>();
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const clusterArn = encodedArn ? decodeURIComponent(encodedArn) : '';

  const [analysis, setAnalysis] = useState<ClusterAnalysis | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [activeTab, setActiveTab] = useState(searchParams.get('tab') ?? 'upgrade');

  useEffect(() => {
    if (!clusterArn) return;
    getClusterAnalysis(clusterArn)
      .then(setAnalysis)
      .catch(e => setError(e instanceof Error ? e.message : 'Failed to load cluster analysis'))
      .finally(() => setLoading(false));
  }, [clusterArn]);

  const handleTestStarted = (runId: string) => { navigate(`/upgrade-test/${runId}`); };

  if (loading) return <Box textAlign="center" padding="xxl"><Spinner size="large" /></Box>;
  if (error || !analysis) return <Alert type="error">{error ?? 'Failed to load cluster'}</Alert>;

  const clusterId = clusterArn.split(':').pop() ?? clusterArn;

  return (
    <SpaceBetween size="l">
      <Container
        header={
          <Header variant="h2">
            Perform a Test Upgrade — {clusterId}
          </Header>
        }
      >
        <SpaceBetween size="s">
          <Box variant="p" color="text-body-secondary">
            Run a safe, non-destructive upgrade test on a cloned copy of your cluster. This validates compatibility, replays your queries against the upgraded version, and generates a readiness report — all without touching your production database.
          </Box>
          <Button variant="link" onClick={() => navigate('/databases')}>← Back to Database List</Button>
          <ColumnLayout columns={2} variant="text-grid">
            <div><Box variant="awsui-key-label">Engine</Box><Box>{analysis.engine}</Box></div>
            <div><Box variant="awsui-key-label">Current Version</Box><Box fontWeight="bold">{analysis.currentVersion}</Box></div>
          </ColumnLayout>
        </SpaceBetween>
      </Container>

      <Tabs
        activeTabId={activeTab}
        onChange={({ detail }) => setActiveTab(detail.activeTabId)}
        tabs={[
          { id: 'upgrade', label: 'Upgrade Test', content: <UpgradeTestTab data={analysis} onTestStarted={handleTestStarted} /> },
          { id: 'code', label: 'Code Analysis', content: <CodeAnalysisTab clusterArn={clusterArn} /> },
          { id: 'cluster', label: 'Cluster Analysis', content: <ClusterAnalysisTab clusterArn={clusterArn} /> },
          { id: 'sp', label: `SP & Config (${analysis.spSummary.high + analysis.spSummary.medium} issues)`, content: <SpTab data={analysis} /> },
          { id: 'queries', label: `Query Profile (${analysis.querySummary.high + analysis.querySummary.medium} issues)`, content: <QueryTab data={analysis} /> },
          { id: 'report', label: 'Readiness Report', content: <ReportTab data={analysis} /> },
        ]}
      />
    </SpaceBetween>
  );
}
