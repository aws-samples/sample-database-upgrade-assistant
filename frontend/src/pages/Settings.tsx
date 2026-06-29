import { useState, useEffect } from 'react';
import Container from '@cloudscape-design/components/container';
import Header from '@cloudscape-design/components/header';
import SpaceBetween from '@cloudscape-design/components/space-between';
import ColumnLayout from '@cloudscape-design/components/column-layout';
import Button from '@cloudscape-design/components/button';
import Select, { SelectProps } from '@cloudscape-design/components/select';
import Table from '@cloudscape-design/components/table';
import Box from '@cloudscape-design/components/box';
import Spinner from '@cloudscape-design/components/spinner';
import Flashbar, { FlashbarProps } from '@cloudscape-design/components/flashbar';
import { getSettings, updateSettings } from '../services/api';
import GitHubIntegration from '../components/GitHubIntegration';

const MODEL_OPTIONS: SelectProps.Option[] = [
  { value: 'us.anthropic.claude-haiku-4-5-20251001-v1:0', label: 'Claude Haiku 4.5' },
  { value: 'us.anthropic.claude-sonnet-4-20250514-v1:0', label: 'Claude Sonnet 4' },
];

export default function Settings() {
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [accountId, setAccountId] = useState('');
  const [region, setRegion] = useState('');
  const [s3Bucket, setS3Bucket] = useState('');
  const [flashItems, setFlashItems] = useState<FlashbarProps.MessageDefinition[]>([]);

  const [depModel, setDepModel] = useState<SelectProps.Option>(MODEL_OPTIONS[0]);
  const [codeModel, setCodeModel] = useState<SelectProps.Option>(MODEL_OPTIONS[1]);

  useEffect(() => {
    getSettings().then((s) => {
      setAccountId(s.aws?.accountId ?? '');
      setRegion(s.aws?.region ?? '');
      setS3Bucket(s.aws?.s3Bucket ?? '');
      const depId = typeof s.models?.deprecation === 'object' ? s.models.deprecation.modelId : s.models?.deprecation;
      const codeId = typeof s.models?.codeAnalysis === 'object' ? s.models.codeAnalysis.modelId : s.models?.codeAnalysis;
      setDepModel(MODEL_OPTIONS.find(o => o.value === depId) ?? MODEL_OPTIONS[0]);
      setCodeModel(MODEL_OPTIONS.find(o => o.value === codeId) ?? MODEL_OPTIONS[1]);
      setLoading(false);
    }).catch(() => setLoading(false));
  }, []);

  const handleSave = async () => {
    setSaving(true);
    try {
      await updateSettings({
        aws: { accountId, region, s3Bucket },
        models: { deprecation: depModel.value ?? '', codeAnalysis: codeModel.value ?? '' },
      });
      setFlashItems([{
        type: 'success', content: 'Settings saved.', dismissible: true,
        onDismiss: () => setFlashItems([]), id: 'save-ok',
      }]);
    } catch (e) {
      setFlashItems([{
        type: 'error', content: e instanceof Error ? e.message : 'Save failed.', dismissible: true,
        onDismiss: () => setFlashItems([]), id: 'save-err',
      }]);
    } finally {
      setSaving(false);
    }
  };

  if (loading) {
    return <Box textAlign="center" padding="xxl"><Spinner size="large" /></Box>;
  }

  return (
    <SpaceBetween size="l">
      <Flashbar items={flashItems} />
      <Container header={<Header variant="h3">Settings</Header>}>
        <SpaceBetween size="xl">
          {/* AWS Configuration */}
          <SpaceBetween size="s">
            <Header variant="h3">AWS Configuration</Header>
            <ColumnLayout columns={1}>
              <SpaceBetween size="xs">
                <Box><Box variant="awsui-key-label">AWS Account ID</Box><Box>{accountId || '—'}</Box></Box>
                <Box><Box variant="awsui-key-label">Region</Box><Box>{region || '—'}</Box></Box>
                <Box><Box variant="awsui-key-label">S3 Report Bucket</Box><Box>{s3Bucket || '—'}</Box></Box>
              </SpaceBetween>
            </ColumnLayout>
          </SpaceBetween>

          {/* GitHub Integration */}
          <GitHubIntegration />

          {/* LLM Model Configuration */}
          <SpaceBetween size="s">
            <Header variant="h3">LLM Model Configuration</Header>
            <Table
              columnDefinitions={[
                { id: 'role', header: 'Agent Role', cell: (item) => <Box fontWeight="bold">{item.role}</Box>, width: 200 },
                { id: 'select', header: 'Model', cell: (item) => (
                  <Select
                    selectedOption={item.id === 'deprecation' ? depModel : codeModel}
                    onChange={({ detail }) => { item.id === 'deprecation' ? setDepModel(detail.selectedOption) : setCodeModel(detail.selectedOption); }}
                    options={MODEL_OPTIONS}
                  />
                ), width: 320 },
                { id: 'rationale', header: 'Rationale', cell: (item) => <Box variant="small" color="text-body-secondary">{item.rationale}</Box> },
              ]}
              items={[
                { id: 'deprecation', role: 'Version Catalog', rationale: 'Structured API-driven task — Haiku is cost-effective' },
                { id: 'codeAnalysis', role: 'Code Analysis', rationale: 'Complex code reasoning requires Sonnet' },
              ]}
              variant="embedded"
              sortingDisabled
            />
          </SpaceBetween>

          <Button variant="primary" onClick={handleSave} loading={saving}>Save Settings</Button>
        </SpaceBetween>
      </Container>
    </SpaceBetween>
  );
}
