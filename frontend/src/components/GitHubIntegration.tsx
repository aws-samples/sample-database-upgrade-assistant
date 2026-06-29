import { useState, useEffect, useRef, useCallback } from 'react';
import Container from '@cloudscape-design/components/container';
import Header from '@cloudscape-design/components/header';
import SpaceBetween from '@cloudscape-design/components/space-between';
import FormField from '@cloudscape-design/components/form-field';
import Input from '@cloudscape-design/components/input';
import Button from '@cloudscape-design/components/button';
import Badge from '@cloudscape-design/components/badge';
import Box from '@cloudscape-design/components/box';
import Modal from '@cloudscape-design/components/modal';
import Alert from '@cloudscape-design/components/alert';
import StatusIndicator from '@cloudscape-design/components/status-indicator';
import Link from '@cloudscape-design/components/link';
import ColumnLayout from '@cloudscape-design/components/column-layout';
import {
  saveGitHubClientId,
  initiateGitHubDeviceFlow,
  pollGitHubToken,
  getGitHubStatus,
  disconnectGitHub,
} from '../services/api';
import type {
  GitHubStatusResponse,
  GitHubDeviceCodeResponse,
} from '../types';

export default function GitHubIntegration() {
  // --- Status state ---
  const [status, setStatus] = useState<GitHubStatusResponse>({ status: 'disconnected' });
  const [statusLoading, setStatusLoading] = useState(true);

  // --- Client ID state ---
  const [clientIdInput, setClientIdInput] = useState('');
  const [maskedClientId, setMaskedClientId] = useState('');
  const [clientIdSaving, setClientIdSaving] = useState(false);
  const [clientIdError, setClientIdError] = useState('');
  const [clientIdSuccess, setClientIdSuccess] = useState('');

  // --- Device flow modal state ---
  const [deviceFlowVisible, setDeviceFlowVisible] = useState(false);
  const [deviceFlowData, setDeviceFlowData] = useState<GitHubDeviceCodeResponse | null>(null);
  const [deviceFlowError, setDeviceFlowError] = useState('');
  const [deviceFlowLoading, setDeviceFlowLoading] = useState(false);
  const [countdown, setCountdown] = useState(0);
  const [copied, setCopied] = useState(false);

  // --- Disconnect modal state ---
  const [disconnectVisible, setDisconnectVisible] = useState(false);
  const [disconnecting, setDisconnecting] = useState(false);

  // --- Refs for polling/timers ---
  const pollIntervalRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const countdownIntervalRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const currentIntervalRef = useRef(5);

  // --- Load status on mount ---
  useEffect(() => {
    loadStatus();
  }, []);

  const loadStatus = async () => {
    setStatusLoading(true);
    try {
      const res = await getGitHubStatus();
      setStatus(res);
      if (res.maskedClientId) {
        setMaskedClientId(res.maskedClientId);
      }
    } catch {
      // If status fetch fails, keep disconnected state
    } finally {
      setStatusLoading(false);
    }
  };

  // --- Cleanup on unmount ---
  useEffect(() => {
    return () => {
      if (pollIntervalRef.current) clearInterval(pollIntervalRef.current);
      if (countdownIntervalRef.current) clearInterval(countdownIntervalRef.current);
    };
  }, []);

  // ─── Client ID Section ───────────────────────────────────────────────

  const handleSaveClientId = async () => {
    setClientIdError('');
    setClientIdSuccess('');

    if (!clientIdInput.trim()) {
      setClientIdError('Client ID is required.');
      return;
    }

    setClientIdSaving(true);
    try {
      const res = await saveGitHubClientId(clientIdInput.trim());
      setMaskedClientId(res.maskedClientId);
      setClientIdInput('');
      setClientIdSuccess('Client ID saved successfully.');
      // Reload status to reflect new masked client ID
      await loadStatus();
    } catch (e) {
      setClientIdError(e instanceof Error ? e.message : 'Failed to save client ID.');
    } finally {
      setClientIdSaving(false);
    }
  };

  // ─── Device Flow ─────────────────────────────────────────────────────

  const startDeviceFlow = async () => {
    setDeviceFlowError('');
    setDeviceFlowLoading(true);
    try {
      const res = await initiateGitHubDeviceFlow();
      setDeviceFlowData(res);
      setDeviceFlowVisible(true);
      setCountdown(res.expiresIn);
      currentIntervalRef.current = res.interval;
      startCountdown(res.expiresIn);
      startPolling(res.deviceCode, res.interval);
    } catch (e) {
      setDeviceFlowError(e instanceof Error ? e.message : 'Failed to initiate device flow.');
    } finally {
      setDeviceFlowLoading(false);
    }
  };

  const startCountdown = (seconds: number) => {
    if (countdownIntervalRef.current) clearInterval(countdownIntervalRef.current);
    let remaining = seconds;
    countdownIntervalRef.current = setInterval(() => {
      remaining -= 1;
      setCountdown(remaining);
      if (remaining <= 0) {
        if (countdownIntervalRef.current) clearInterval(countdownIntervalRef.current);
      }
    }, 1000);
  };

  const startPolling = (deviceCode: string, interval: number) => {
    if (pollIntervalRef.current) clearInterval(pollIntervalRef.current);
    currentIntervalRef.current = interval;

    const poll = async () => {
      try {
        const res = await pollGitHubToken(deviceCode, currentIntervalRef.current);
        if (res.status === 'connected') {
          stopFlowTimers();
          setDeviceFlowVisible(false);
          setDeviceFlowData(null);
          setStatus({
            status: 'connected',
            username: res.username,
            authorizedAt: res.authorizedAt,
            maskedClientId: maskedClientId,
          });
        } else if (res.status === 'expired') {
          stopFlowTimers();
          setDeviceFlowError(res.message || 'Device code expired. Please restart the flow.');
          setDeviceFlowVisible(false);
          setDeviceFlowData(null);
        } else if (res.status === 'denied') {
          stopFlowTimers();
          setDeviceFlowError(res.message || 'Authorization was declined by the user.');
          setDeviceFlowVisible(false);
          setDeviceFlowData(null);
        } else if (res.status === 'pending') {
          // If interval was increased (slow_down), update polling
          if (res.interval && res.interval > currentIntervalRef.current) {
            currentIntervalRef.current = res.interval;
            // Restart polling with new interval
            if (pollIntervalRef.current) clearInterval(pollIntervalRef.current);
            pollIntervalRef.current = setInterval(poll, currentIntervalRef.current * 1000);
          }
        }
      } catch {
        // Network error during poll — keep trying
      }
    };

    pollIntervalRef.current = setInterval(poll, interval * 1000);
  };

  const stopFlowTimers = useCallback(() => {
    if (pollIntervalRef.current) {
      clearInterval(pollIntervalRef.current);
      pollIntervalRef.current = null;
    }
    if (countdownIntervalRef.current) {
      clearInterval(countdownIntervalRef.current);
      countdownIntervalRef.current = null;
    }
  }, []);

  const cancelDeviceFlow = () => {
    stopFlowTimers();
    setDeviceFlowVisible(false);
    setDeviceFlowData(null);
  };

  const copyUserCode = async () => {
    if (deviceFlowData?.userCode) {
      await navigator.clipboard.writeText(deviceFlowData.userCode);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    }
  };

  // ─── Disconnect ──────────────────────────────────────────────────────

  const handleDisconnect = async () => {
    setDisconnecting(true);
    try {
      await disconnectGitHub();
      setStatus({ status: 'disconnected' });
      setMaskedClientId('');
      setDisconnectVisible(false);
    } catch {
      // If disconnect fails, close modal anyway
      setDisconnectVisible(false);
    } finally {
      setDisconnecting(false);
    }
  };

  // ─── Helpers ─────────────────────────────────────────────────────────

  const formatCountdown = (seconds: number): string => {
    const mins = Math.floor(seconds / 60);
    const secs = seconds % 60;
    return `${mins}:${secs.toString().padStart(2, '0')}`;
  };

  const hasClientId = !!maskedClientId;

  // ─── Render ──────────────────────────────────────────────────────────

  if (statusLoading) {
    return (
      <Container header={<Header variant="h3">GitHub Integration</Header>}>
        <StatusIndicator type="loading">Loading GitHub status...</StatusIndicator>
      </Container>
    );
  }

  return (
    <>
      <Container header={<Header variant="h3">GitHub Integration</Header>}>
        <SpaceBetween size="l">
          {/* Device flow error alert */}
          {deviceFlowError && (
            <Alert
              type="error"
              dismissible
              onDismiss={() => setDeviceFlowError('')}
            >
              {deviceFlowError}
            </Alert>
          )}

          {/* ─── Client ID Section ─── */}
          <SpaceBetween size="s">
            <Box variant="h4">OAuth App Configuration</Box>
            {maskedClientId ? (
              <SpaceBetween size="xs">
                <Box>
                  <Box variant="awsui-key-label">Client ID</Box>
                  <Box fontWeight="bold">{maskedClientId}</Box>
                </Box>
                <Button
                  variant="link"
                  onClick={() => setMaskedClientId('')}
                >
                  Change Client ID
                </Button>
              </SpaceBetween>
            ) : (
              <SpaceBetween size="xs">
                <FormField
                  label="GitHub OAuth App Client ID"
                  description={
                    <span>
                      Register an OAuth App at{' '}
                      <Link
                        href="https://github.com/settings/developers"
                        external
                      >
                        GitHub Developer Settings
                      </Link>
                    </span>
                  }
                  errorText={clientIdError}
                >
                  <Input
                    value={clientIdInput}
                    onChange={({ detail }) => setClientIdInput(detail.value)}
                    placeholder="Iv1.a1b2c3d4e5f6g7h8"
                  />
                </FormField>
                {clientIdSuccess && (
                  <StatusIndicator type="success">{clientIdSuccess}</StatusIndicator>
                )}
                <Button
                  variant="primary"
                  onClick={handleSaveClientId}
                  loading={clientIdSaving}
                >
                  Save Client ID
                </Button>
              </SpaceBetween>
            )}
          </SpaceBetween>

          {/* ─── Connection Status ─── */}
          <SpaceBetween size="s">
            <Box variant="h4">Connection Status</Box>
            <ColumnLayout columns={3}>
              <div>
                <Box variant="awsui-key-label">Status</Box>
                <Box>
                  {status.status === 'connected' && <Badge color="green">Connected</Badge>}
                  {status.status === 'disconnected' && <Badge color="grey">Disconnected</Badge>}
                  {status.status === 'expired' && <Badge color="red">Expired</Badge>}
                </Box>
              </div>
              {status.status === 'connected' && (
                <>
                  <div>
                    <Box variant="awsui-key-label">Username</Box>
                    <Box>{status.username}</Box>
                  </div>
                  <div>
                    <Box variant="awsui-key-label">Authorized</Box>
                    <Box>{status.authorizedAt ? new Date(status.authorizedAt).toLocaleDateString() : '—'}</Box>
                  </div>
                </>
              )}
            </ColumnLayout>

            {status.status === 'expired' && (
              <Alert type="warning">
                Your GitHub authorization has expired. Please reconnect to continue accessing private repositories.
              </Alert>
            )}
          </SpaceBetween>

          {/* ─── Action Buttons ─── */}
          <SpaceBetween direction="horizontal" size="xs">
            {(status.status === 'disconnected' || status.status === 'expired') && (
              <SpaceBetween direction="horizontal" size="xs">
                {!hasClientId ? (
                  <Box>
                    <Button variant="primary" disabled>
                      {status.status === 'expired' ? 'Reconnect' : 'Connect GitHub'}
                    </Button>
                    <Box variant="small" color="text-status-inactive" padding={{ top: 'xxs' }}>
                      Save a Client ID above to enable GitHub connection.
                    </Box>
                  </Box>
                ) : (
                  <Button
                    variant="primary"
                    onClick={startDeviceFlow}
                    loading={deviceFlowLoading}
                  >
                    {status.status === 'expired' ? 'Reconnect' : 'Connect GitHub'}
                  </Button>
                )}
              </SpaceBetween>
            )}
            {status.status === 'connected' && (
              <Button
                variant="normal"
                onClick={() => setDisconnectVisible(true)}
              >
                Disconnect
              </Button>
            )}
          </SpaceBetween>
        </SpaceBetween>
      </Container>

      {/* ─── Device Flow Modal ─── */}
      <Modal
        visible={deviceFlowVisible}
        onDismiss={cancelDeviceFlow}
        header="Authorize GitHub"
        footer={
          <Box float="right">
            <Button variant="link" onClick={cancelDeviceFlow}>
              Cancel
            </Button>
          </Box>
        }
      >
        {deviceFlowData && (
          <SpaceBetween size="l">
            <Alert type="info">
              Enter the code below on GitHub to authorize this application.
            </Alert>

            <Box textAlign="center">
              <Box variant="awsui-key-label">Your Code</Box>
              <Box
                fontSize="heading-xl"
                fontWeight="bold"
                padding={{ top: 'xs', bottom: 'xs' }}
              >
                <span style={{ fontFamily: 'monospace', letterSpacing: '0.15em' }}>
                  {deviceFlowData.userCode}
                </span>
              </Box>
              <Button
                iconName={copied ? 'status-positive' : 'copy'}
                variant="inline-icon"
                onClick={copyUserCode}
                ariaLabel="Copy code to clipboard"
              />
              {copied && (
                <Box variant="small" color="text-status-success" display="inline" padding={{ left: 'xs' }}>
                  Copied!
                </Box>
              )}
            </Box>

            <Box textAlign="center">
              <Box variant="awsui-key-label">Open GitHub Verification Page</Box>
              <Box padding={{ top: 'xs' }}>
                <Link
                  href={deviceFlowData.verificationUri}
                  external
                >
                  {deviceFlowData.verificationUri}
                </Link>
              </Box>
            </Box>

            <Box textAlign="center">
              <Box variant="awsui-key-label">Time Remaining</Box>
              <Box padding={{ top: 'xs' }}>
                {countdown > 0 ? (
                  <StatusIndicator type="pending">
                    {formatCountdown(countdown)}
                  </StatusIndicator>
                ) : (
                  <StatusIndicator type="error">Expired</StatusIndicator>
                )}
              </Box>
            </Box>

            <Box textAlign="center">
              <StatusIndicator type="loading">
                Waiting for authorization...
              </StatusIndicator>
            </Box>
          </SpaceBetween>
        )}
      </Modal>

      {/* ─── Disconnect Confirmation Modal ─── */}
      <Modal
        visible={disconnectVisible}
        onDismiss={() => setDisconnectVisible(false)}
        header="Disconnect GitHub"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={() => setDisconnectVisible(false)}>
                Cancel
              </Button>
              <Button
                variant="primary"
                onClick={handleDisconnect}
                loading={disconnecting}
              >
                Disconnect
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <Box>
          Are you sure? This will revoke GitHub access. You will need to re-authorize
          to access private repositories for code analysis.
        </Box>
      </Modal>
    </>
  );
}
