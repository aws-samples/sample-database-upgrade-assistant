import ColumnLayout from '@cloudscape-design/components/column-layout';
import Container from '@cloudscape-design/components/container';
import Header from '@cloudscape-design/components/header';
import Box from '@cloudscape-design/components/box';
import Badge from '@cloudscape-design/components/badge';
import type { ClusterMetadata } from '../types';

interface ClusterInfoProps {
  cluster: ClusterMetadata;
}

function eolBadge(endOfSupport: string) {
  if (!endOfSupport) return <Badge color="green">OK</Badge>;
  const days = Math.floor((new Date(endOfSupport).getTime() - Date.now()) / 86400000);
  if (days < 0) return <Badge color="red">PAST EOL</Badge>;
  if (days < 365) return <Badge color="grey">Approaching</Badge>;
  return <Badge color="green">OK</Badge>;
}

function eolDateDisplay(endOfSupport: string) {
  if (!endOfSupport) return '—';
  const days = Math.floor((new Date(endOfSupport).getTime() - Date.now()) / 86400000);
  if (days < 0) {
    return (
      <Box color="text-status-error" fontWeight="bold">
        {endOfSupport} ({Math.abs(days)} days ago)
      </Box>
    );
  }
  return endOfSupport;
}

export default function ClusterInfo({ cluster }: ClusterInfoProps) {
  return (
    <Container header={<Header variant="h3">Source Cluster Details</Header>}>
      <ColumnLayout columns={4} variant="text-grid">
        <div>
          <Box variant="awsui-key-label">Account ID</Box>
          <Box variant="awsui-value-large">{cluster.accountId}</Box>
        </div>
        <div>
          <Box variant="awsui-key-label">Cluster ID</Box>
          <Box variant="awsui-value-large">{cluster.clusterId}</Box>
        </div>
        <div>
          <Box variant="awsui-key-label">Engine</Box>
          <Box variant="awsui-value-large">{cluster.engine}</Box>
        </div>
        <div>
          <Box variant="awsui-key-label">Version</Box>
          <Box variant="awsui-value-large">{cluster.version}</Box>
        </div>
        <div>
          <Box variant="awsui-key-label">Region</Box>
          <Box variant="awsui-value-large">{cluster.region}</Box>
        </div>
        <div>
          <Box variant="awsui-key-label">Status</Box>
          <div>{eolBadge(cluster.endOfSupport)}</div>
        </div>
        <div>
          <Box variant="awsui-key-label">End of Support</Box>
          <div>{eolDateDisplay(cluster.endOfSupport)}</div>
        </div>
        <div>
          <Box variant="awsui-key-label">AppCI Tag</Box>
          <Box variant="awsui-value-large">{cluster.appciTag ?? '—'}</Box>
        </div>
      </ColumnLayout>
    </Container>
  );
}
