import CloudscapeProgressBar from '@cloudscape-design/components/progress-bar';
import Box from '@cloudscape-design/components/box';
import type { ProgressEvent } from '../types';

interface ProgressBarProps {
  progress: ProgressEvent | null;
}

export default function ProgressBar({ progress }: ProgressBarProps) {
  if (!progress) return null;

  const isComplete = progress.percentage >= 100;

  return (
    <div>
      <CloudscapeProgressBar
        value={progress.percentage}
        status={isComplete ? 'success' : 'in-progress'}
        additionalInfo={progress.currentActivity}
        label="Analysis Progress"
      />
      <Box variant="small" color="text-body-secondary" margin={{ top: 'xxs' }}>
        Elapsed: {progress.elapsedSeconds}s
      </Box>
    </div>
  );
}
