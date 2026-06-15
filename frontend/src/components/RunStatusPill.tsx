import StatusIndicator from '@cloudscape-design/components/status-indicator';
import Popover from '@cloudscape-design/components/popover';
import Box from '@cloudscape-design/components/box';

export type RunStatus = 'idle' | 'running' | 'complete' | 'failed' | 'stalled';

interface RunStatusPillProps {
  status: RunStatus;
  detail?: string;
}

export default function RunStatusPill({ status, detail }: RunStatusPillProps) {
  if (status === 'stalled') {
    return (
      <Popover
        dismissButton={false}
        position="top"
        size="small"
        triggerType="custom"
        content={<Box variant="small">{detail || 'Lambda execution may have timed out. Check run status.'}</Box>}
      >
        <StatusIndicator type="warning">Stalled</StatusIndicator>
      </Popover>
    );
  }

  switch (status) {
    case 'running':
      return <StatusIndicator type="in-progress">Running</StatusIndicator>;
    case 'complete':
      return <StatusIndicator type="success">Complete</StatusIndicator>;
    case 'failed':
      return <StatusIndicator type="error">Failed</StatusIndicator>;
    default:
      return <StatusIndicator type="stopped">Idle</StatusIndicator>;
  }
}
