import { useState } from 'react';
import Table from '@cloudscape-design/components/table';
import Header from '@cloudscape-design/components/header';
import SpaceBetween from '@cloudscape-design/components/space-between';
import ColumnLayout from '@cloudscape-design/components/column-layout';
import Box from '@cloudscape-design/components/box';
import Badge from '@cloudscape-design/components/badge';
import Pagination from '@cloudscape-design/components/pagination';
import type { Finding, FindingCategory } from '../types';

interface FindingsTableProps {
  findings: Finding[];
  title: string;
}

type SeverityFilter = 'All' | 'Errors' | 'Warnings';

const CATEGORY_LABELS: Record<FindingCategory, string> = {
  'sql-syntax': 'SQL Syntax',
  'schema': 'Schema',
  'query-cache': 'Query Cache',
  'auth': 'Auth',
  'charset': 'Charset',
  'driver': 'Driver',
  'config': 'Config',
  'reserved-keyword': 'Reserved Keyword',
  'info-schema': 'Info Schema',
};

export default function FindingsTable({ findings, title }: FindingsTableProps) {
  const [severityFilter, setSeverityFilter] = useState<SeverityFilter>('All');
  const [categoryFilter, setCategoryFilter] = useState<FindingCategory | 'All'>('All');
  const [currentPage, setCurrentPage] = useState(1);
  const pageSize = 20;

  const errorCount = findings.filter(f => f.severity === 'error').length;
  const warningCount = findings.filter(f => f.severity === 'warning').length;

  // Compute category counts
  const categoryCounts = findings.reduce<Record<string, number>>((acc, f) => {
    acc[f.category] = (acc[f.category] ?? 0) + 1;
    return acc;
  }, {});

  // Apply filters
  let filtered = findings;
  if (severityFilter === 'Errors') filtered = filtered.filter(f => f.severity === 'error');
  if (severityFilter === 'Warnings') filtered = filtered.filter(f => f.severity === 'warning');
  if (categoryFilter !== 'All') filtered = filtered.filter(f => f.category === categoryFilter);

  const totalPages = Math.ceil(filtered.length / pageSize);
  const paged = filtered.slice((currentPage - 1) * pageSize, currentPage * pageSize);

  return (
    <SpaceBetween size="m">
      {/* Summary cards */}
      <ColumnLayout columns={3} variant="text-grid">
        <div>
          <Box variant="awsui-key-label">Errors</Box>
          <Box variant="awsui-value-large" color="text-status-error">{errorCount}</Box>
        </div>
        <div>
          <Box variant="awsui-key-label">Warnings</Box>
          <Box variant="awsui-value-large" color="text-status-warning">{warningCount}</Box>
        </div>
        <div>
          <Box variant="awsui-key-label">Total Findings</Box>
          <Box variant="awsui-value-large">{findings.length}</Box>
        </div>
      </ColumnLayout>

      {/* Severity filter — visible pills */}
      <SpaceBetween direction="horizontal" size="xs">
        <Box variant="small" color="text-body-secondary" padding={{ top: 'xxs' }}>Severity:</Box>
        <span
          onClick={() => { setSeverityFilter('All'); setCurrentPage(1); }}
          style={{ cursor: 'pointer' }}
        >
          <Badge color={severityFilter === 'All' ? 'blue' : 'grey'}>
            All ({findings.length})
          </Badge>
        </span>
        <span
          onClick={() => { setSeverityFilter('Errors'); setCurrentPage(1); }}
          style={{ cursor: 'pointer' }}
        >
          <Badge color={severityFilter === 'Errors' ? 'red' : 'grey'}>
            Errors ({errorCount})
          </Badge>
        </span>
        <span
          onClick={() => { setSeverityFilter('Warnings'); setCurrentPage(1); }}
          style={{ cursor: 'pointer' }}
        >
          <Badge color={severityFilter === 'Warnings' ? 'blue' : 'grey'}>
            Warnings ({warningCount})
          </Badge>
        </span>
      </SpaceBetween>

      {/* Category filter — visible pills */}
      <SpaceBetween direction="horizontal" size="xs">
        <Box variant="small" color="text-body-secondary" padding={{ top: 'xxs' }}>Category:</Box>
        <span
          onClick={() => { setCategoryFilter('All'); setCurrentPage(1); }}
          style={{ cursor: 'pointer' }}
        >
          <Badge color={categoryFilter === 'All' ? 'blue' : 'grey'}>All</Badge>
        </span>
        {Object.entries(CATEGORY_LABELS).map(([cat, label]) => {
          const count = categoryCounts[cat] ?? 0;
          return (
            <span
              key={cat}
              onClick={() => { setCategoryFilter(cat as FindingCategory); setCurrentPage(1); }}
              style={{ cursor: 'pointer' }}
            >
              <Badge color={categoryFilter === cat ? 'blue' : 'grey'}>
                {label} ({count})
              </Badge>
            </span>
          );
        })}
      </SpaceBetween>

      {/* Findings table */}
      <Table
        header={<Header variant="h3" counter={`(${filtered.length})`}>{title}</Header>}
        columnDefinitions={[
          { id: 'file', header: 'File', cell: (f) => <code>{f.fileName}</code>, width: 160 },
          { id: 'line', header: 'Line', cell: (f) => f.lineNumber, width: 60 },
          { id: 'snippet', header: 'Code Snippet', cell: (f) => <code style={{ fontSize: '11px' }}>{f.codeSnippet}</code> },
          { id: 'category', header: 'Category', cell: (f) => <Badge color="blue">{CATEGORY_LABELS[f.category] ?? f.category}</Badge>, width: 130 },
          { id: 'severity', header: 'Severity', cell: (f) => f.severity === 'error' ? <Badge color="red">Error</Badge> : <Badge color="grey">Warning</Badge>, width: 100 },
          { id: 'recommendation', header: 'Recommended Change', cell: (f) => f.recommendedChange },
        ]}
        items={paged}
        variant="embedded"
        sortingDisabled
        empty="No findings"
        pagination={
          totalPages > 1 ? (
            <Pagination
              currentPageIndex={currentPage}
              pagesCount={totalPages}
              onChange={({ detail }) => setCurrentPage(detail.currentPageIndex)}
            />
          ) : undefined
        }
      />
    </SpaceBetween>
  );
}
