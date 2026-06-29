import { useState, useEffect, useCallback, useRef } from 'react';
import Container from '@cloudscape-design/components/container';
import Header from '@cloudscape-design/components/header';
import SpaceBetween from '@cloudscape-design/components/space-between';
import ColumnLayout from '@cloudscape-design/components/column-layout';
import Box from '@cloudscape-design/components/box';
import Button from '@cloudscape-design/components/button';
import Tabs from '@cloudscape-design/components/tabs';
import Table from '@cloudscape-design/components/table';
import Badge from '@cloudscape-design/components/badge';
import ExpandableSection from '@cloudscape-design/components/expandable-section';
import Link from '@cloudscape-design/components/link';
import StatusIndicator from '@cloudscape-design/components/status-indicator';
import Select, { type SelectProps } from '@cloudscape-design/components/select';
import Spinner from '@cloudscape-design/components/spinner';
import Alert from '@cloudscape-design/components/alert';
import RunStatusPill, { type RunStatus } from '../components/RunStatusPill';
import ProgressBar from '../components/ProgressBar';
import { getDeprecationCatalog, triggerDeprecation, getDeprecationRuns, getRunStatus } from '../services/api';
import type {
  EngineVersion,
  BreakingChange,
  ProgressEvent,
  DeprecationCatalogResponse,
  CatalogVersionEntry,
} from '../types';

/* ── Helpers ── */

function formatTimestamp(ts: string): string {
  // Handle both ISO format and compact "20260413T173516Z" format
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

function versionOptions(versions: CatalogVersionEntry[]): SelectProps.Option[] {
  return [
    { label: 'Latest', value: 'latest' },
    ...versions.map((v) => ({
      label: formatTimestamp(v.version),
      value: v.version,
      description: `Size: ${(v.size / 1024).toFixed(1)} KB`,
    })),
  ];
}

function eolBadge(v: EngineVersion) {
  if (v.status === 'end-of-life') {
    const days = Math.floor((Date.now() - new Date(v.endOfSupport).getTime()) / 86400000);
    return <Badge color="red">PAST EOL ({days}d)</Badge>;
  }
  if (v.status === 'deprecated') {
    const days = Math.floor((new Date(v.endOfSupport).getTime() - Date.now()) / 86400000);
    return days < 0
      ? <Badge color="red">PAST EOL ({Math.abs(days)}d ago)</Badge>
      : <Badge color="grey">Approaching ({days}d)</Badge>;
  }
  if (v.endOfSupport) {
    const days = Math.floor((new Date(v.endOfSupport).getTime() - Date.now()) / 86400000);
    if (days < 365) return <Badge color="grey">Approaching ({days}d)</Badge>;
  }
  return <StatusIndicator type="success">OK</StatusIndicator>;
}

function statusBadge(status: EngineVersion['status']) {
  switch (status) {
    case 'end-of-life': return <Badge color="red">deprecated</Badge>;
    case 'deprecated': return <Badge color="grey">approaching</Badge>;
    default: return <Badge color="green">available</Badge>;
  }
}

function remediationList(changes: BreakingChange[]) {
  if (changes.length === 0) return <Box>No breaking changes.</Box>;
  return (
    <SpaceBetween size="xs">
      {changes.map((c, i) => (
        <Box key={i}>
          <Box variant="span" fontWeight="bold" color="text-status-info">{c.category}: </Box>
          {c.description} → {c.remediation}
        </Box>
      ))}
    </SpaceBetween>
  );
}


/* ── Main component ── */
export default function DeprecationAnalysis() {
  const [catalogData, setCatalogData] = useState<DeprecationCatalogResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [runStatus, setRunStatus] = useState<RunStatus>('idle');
  const [progress, setProgress] = useState<ProgressEvent | null>(null);
  const stalledCheckRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const lastProgressPctRef = useRef<number>(0);
  const [activeTab, setActiveTab] = useState('mysql');
  const [selectedVersion, setSelectedVersion] = useState<SelectProps.Option>({ label: 'Latest', value: 'latest' });

  // Expandable rows state per engine
  const [expandedMySQL, setExpandedMySQL] = useState<string[]>([]);
  const [expandedPostgres, setExpandedPostgres] = useState<string[]>([]);

  const fetchCatalog = useCallback(async (version?: string) => {
    setLoading(true);
    setError(null);
    try {
      const data = await getDeprecationCatalog(version === 'latest' ? undefined : version);
      setCatalogData(data);
      // Check latest run status
      const runs = await getDeprecationRuns();
      if (runs.length > 0) {
        const latest = runs[0];
        if (latest.status === 'running') setRunStatus('running');
        else if (latest.status === 'complete') setRunStatus('complete');
        else if (latest.status === 'failed') setRunStatus('failed');
        else setRunStatus('idle');
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load catalog');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchCatalog();
  }, [fetchCatalog]);

  const handleVersionChange = (detail: { selectedOption: SelectProps.Option }) => {
    setSelectedVersion(detail.selectedOption);
    fetchCatalog(detail.selectedOption.value ?? undefined);
  };

  const handleRun = async () => {
    setRunStatus('running');
    setProgress({
      runId: '', timestamp: new Date().toISOString(),
      percentage: 0, currentActivity: 'Starting version catalog scan...',
      elapsedSeconds: 0, agentType: 'deprecation',
    });
    try {
      const { runId } = await triggerDeprecation();
      lastProgressPctRef.current = 0;

      // Poll for progress; check status if progress stalls
      const pollInterval = setInterval(async () => {
        try {
          const runs = await getDeprecationRuns();
          const run = runs.find((r) => r.runId === runId) ?? runs[0];
          if (!run) return;

          if (run.status === 'complete') {
            clearInterval(pollInterval);
            if (stalledCheckRef.current) clearTimeout(stalledCheckRef.current);
            setProgress({
              runId, timestamp: new Date().toISOString(),
              percentage: 100, currentActivity: 'Version catalog scan complete.',
              elapsedSeconds: run.durationSeconds ?? 0, agentType: 'deprecation',
            });
            setRunStatus('complete');
            setSelectedVersion({ label: 'Latest', value: 'latest' });
            await fetchCatalog();
          } else if (run.status === 'failed') {
            clearInterval(pollInterval);
            if (stalledCheckRef.current) clearTimeout(stalledCheckRef.current);
            setRunStatus('failed');
            setError(run.errorMessage ?? 'Analysis failed');
            setProgress(null);
          } else {
            // Poll actual progress from backend
            const statusResp = await getRunStatus(runId);
            const pct = statusResp.progress ?? 0;
            const phase = statusResp.phase ?? '';

            // Map phase to activity description
            const activityMap: Record<string, string> = {
              'collecting_versions': 'Querying RDS API for engine versions...',
              'fetching_documentation': 'Fetching AWS upgrade documentation...',
              'extracting_breaking_changes': 'Extracting breaking changes with AI...',
              'building_catalog': 'Building version catalog...',
            };
            const activity = activityMap[phase] || 'Processing...';

            // If progress hasn't moved in 90s, check Lambda status
            if (pct === lastProgressPctRef.current && pct > 0) {
              if (!stalledCheckRef.current) {
                stalledCheckRef.current = setTimeout(async () => {
                  stalledCheckRef.current = null;
                  if (statusResp.status === 'failed') {
                    clearInterval(pollInterval);
                    setRunStatus('failed');
                    setError('Lambda execution failed or timed out');
                    setProgress(null);
                  } else {
                    setRunStatus('stalled');
                  }
                }, 900000);
              }
            } else {
              lastProgressPctRef.current = pct;
              if (stalledCheckRef.current) {
                clearTimeout(stalledCheckRef.current);
                stalledCheckRef.current = null;
              }
            }

            setProgress({
              runId, timestamp: new Date().toISOString(),
              percentage: pct,
              currentActivity: activity,
              elapsedSeconds: 0, agentType: 'deprecation',
            });
          }
        } catch {
          // Ignore polling errors, keep trying
        }
      }, 3000);
    } catch (e) {
      setRunStatus('failed');
      setError(e instanceof Error ? e.message : 'Failed to start analysis');
      setProgress(null);
    }
  };

  // Derive data from catalog response
  const catalog = catalogData?.catalog ?? null;
  const mysqlVersions = catalog?.engines?.['aurora-mysql']?.versions ?? [];
  const postgresVersions = catalog?.engines?.['aurora-postgresql']?.versions ?? [];
  const mysqlLts = catalog?.engines?.['aurora-mysql']?.ltsVersion ?? '—';
  const postgresLts = catalog?.engines?.['aurora-postgresql']?.ltsVersion ?? '—';
  const totalVersions = catalogData?.summary?.totalVersions ?? 0;
  const deprecatedVersions = catalogData?.summary?.deprecatedVersions ?? 0;
  const engineCount = catalogData?.summary?.engines?.length ?? 0;
  const availableVersions = catalogData?.availableVersions ?? [];
  const catalogUrl = catalogData?.catalogUrl ?? null;
  const lastUpdated = catalogData?.lastUpdated ? formatTimestamp(catalogData.lastUpdated) : null;

  // Empty state
  const isEmpty = !catalogData || !catalog;

  return (
    <SpaceBetween size="l">
      {error && <Alert type="error" dismissible onDismiss={() => setError(null)}>{error}</Alert>}

      <Container
        header={
          <Header
            variant="h3"
            actions={
              <SpaceBetween direction="horizontal" size="s">
                {availableVersions.length > 0 && (
                  <Select
                    selectedOption={selectedVersion}
                    onChange={({ detail }) => handleVersionChange(detail)}
                    options={versionOptions(availableVersions)}
                    placeholder="Select version"
                  />
                )}
                <RunStatusPill status={runStatus} />
                <Button variant="primary" onClick={handleRun} disabled={runStatus === 'running'}>
                  Run Version Catalog Scan
                </Button>
              </SpaceBetween>
            }
          >
            Engine Version Catalog
          </Header>
        }
      >
        <SpaceBetween size="l">
          {/* Progress bar (visible during run) */}
          {runStatus === 'running' && <ProgressBar progress={progress} />}

          {loading && <Box textAlign="center" padding="l"><Spinner size="large" /> Loading catalog...</Box>}

          {!loading && isEmpty && (
            <Box textAlign="center" padding="l" color="text-body-secondary">
              No catalog available. Click <strong>Run Version Catalog Scan</strong> to generate one.
            </Box>
          )}

          {!loading && !isEmpty && (
            <>
              {/* Summary stats */}
              <ColumnLayout columns={3} variant="text-grid">
                <div>
                  <Box variant="awsui-key-label">Versions Cataloged</Box>
                  <Box variant="awsui-value-large">{totalVersions}</Box>
                </div>
                <div>
                  <Box variant="awsui-key-label">Engines Analyzed</Box>
                  <Box variant="awsui-value-large">{engineCount}</Box>
                </div>
                <div>
                  <Box variant="awsui-key-label">Deprecated Versions</Box>
                  <Box variant="awsui-value-large" color="text-status-warning">{deprecatedVersions}</Box>
                </div>
              </ColumnLayout>

              <SpaceBetween direction="horizontal" size="s">
                {lastUpdated && (
                  <Box variant="small" color="text-body-secondary">
                    Last scan: {lastUpdated}
                  </Box>
                )}
                {catalogUrl && (
                  <Link href={catalogUrl} external variant="primary">Download deprecation_catalog.json</Link>
                )}
              </SpaceBetween>

              {/* LTS recommendations */}
              <SpaceBetween direction="horizontal" size="l">
                <Box><Box variant="span" fontWeight="bold">Aurora MySQL LTS:</Box> {mysqlLts}</Box>
                <Box><Box variant="span" fontWeight="bold">Aurora PostgreSQL LTS:</Box> {postgresLts}</Box>
              </SpaceBetween>

              {/* Tabs */}
              <Tabs
                activeTabId={activeTab}
                onChange={({ detail }) => setActiveTab(detail.activeTabId)}
                tabs={[
                  {
                    id: 'mysql',
                    label: `Aurora MySQL (${mysqlVersions.length})`,
                    content: (
                      <VersionTableWithExpand
                        versions={mysqlVersions}
                        expanded={expandedMySQL}
                        onToggle={setExpandedMySQL}
                      />
                    ),
                  },
                  {
                    id: 'postgres',
                    label: `Aurora PostgreSQL (${postgresVersions.length})`,
                    content: (
                      <VersionTableWithExpand
                        versions={postgresVersions}
                        expanded={expandedPostgres}
                        onToggle={setExpandedPostgres}
                      />
                    ),
                  },
                ]}
              />
            </>
          )}
        </SpaceBetween>
      </Container>
    </SpaceBetween>
  );
}


/** Version table with expandable remediation rows */
function VersionTableWithExpand({
  versions,
  expanded,
  onToggle,
}: {
  versions: EngineVersion[];
  expanded: string[];
  onToggle: (v: string[]) => void;
}) {
  return (
    <SpaceBetween size="m">
      <Table
        columnDefinitions={[
          { id: 'version', header: 'Version', cell: (v) => <Box fontWeight={v.status !== 'available' ? 'bold' : 'normal'}>{v.version}</Box>, width: 220 },
          { id: 'family', header: 'Parameter Family', cell: (v) => v.parameterFamily, width: 180 },
          { id: 'status', header: 'Status', cell: (v) => statusBadge(v.status), width: 120 },
          { id: 'eos', header: 'End of Support', cell: (v) => v.endOfSupport || '—', width: 130 },
          { id: 'eol', header: 'EOL Status', cell: (v) => eolBadge(v), width: 180 },
          { id: 'targets', header: 'Upgrade Targets', cell: (v) => v.upgradeTargets.length, width: 120 },
          { id: 'breaking', header: 'Breaking Changes', cell: (v) => v.breakingChanges.length, width: 130 },
        ]}
        items={versions}
        variant="embedded"
        sortingDisabled
        empty="No versions found"
      />
      {/* Expandable remediation sections for versions with breaking changes */}
      {versions.filter(v => v.breakingChanges.length > 0).map(v => (
        <ExpandableSection
          key={v.version}
          headerText={`Remediation Actions for ${v.version}`}
          variant="footer"
          expanded={expanded.includes(v.version)}
          onChange={({ detail }) => {
            onToggle(detail.expanded
              ? [...expanded, v.version]
              : expanded.filter(x => x !== v.version));
          }}
        >
          {remediationList(v.breakingChanges)}
        </ExpandableSection>
      ))}
    </SpaceBetween>
  );
}
