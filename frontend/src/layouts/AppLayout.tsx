import { useState, useEffect } from 'react';
import { Outlet, useNavigate, useLocation } from 'react-router-dom';
import AppLayout from '@cloudscape-design/components/app-layout';
import SideNavigation, { SideNavigationProps } from '@cloudscape-design/components/side-navigation';
import Box from '@cloudscape-design/components/box';
import SpaceBetween from '@cloudscape-design/components/space-between';
import { getSettings, getDeprecationRuns } from '../services/api';

const PAGE_TITLES: Record<string, string> = {
  '/': 'Upgrade Assistant',
  '/databases': 'Database List',
  '/upgrade-tests': 'Upgrade Tests',
  '/deprecation': 'Version Catalog',
  '/code-analysis': 'Code Analysis',
  '/cluster-analysis': 'Cluster Analysis',
  '/settings': 'Settings',
};

export default function Shell() {
  const navigate = useNavigate();
  const location = useLocation();
  const [navOpen, setNavOpen] = useState(true);
  const [accountId, setAccountId] = useState('');
  const [lastCatalogScan, setLastCatalogScan] = useState('');

  const pageTitle = location.pathname.startsWith('/upgrade-test/')
    ? 'Upgrade Test'
    : location.pathname.startsWith('/cluster/')
    ? 'Perform a Test Upgrade'
    : PAGE_TITLES[location.pathname] ?? 'Upgrade Assistant';

  useEffect(() => {
    getSettings().then((s) => {
      if (s.aws?.accountId) setAccountId(s.aws.accountId);
    }).catch(() => {});

    getDeprecationRuns().then((runs) => {
      const completed = runs
        .filter((r) => r.status === 'complete' && r.startTime)
        .sort((a, b) => (b.startTime ?? '').localeCompare(a.startTime ?? ''));
      if (completed.length > 0 && completed[0].startTime) {
        const d = new Date(completed[0].startTime);
        setLastCatalogScan(d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' }));
      }
    }).catch(() => {});
  }, []);

  // Build nav items with dynamic last-scan date under Version Catalog
  const navItems: SideNavigationProps.Item[] = [
    { type: 'section', text: '🚀 Upgrade Pipeline', items: [
      { type: 'link', text: 'Dashboard', href: '/' },
      { type: 'link', text: 'Database List', href: '/databases' },
    ]},
    { type: 'section', text: '🔍 Analysis Results', items: [
      { type: 'link', text: 'Code Analysis', href: '/code-analysis' },
      { type: 'link', text: 'Cluster Analysis', href: '/cluster-analysis' },
      { type: 'link', text: 'Upgrade Tests', href: '/upgrade-tests' },
    ]},
    { type: 'section', text: '📚 Reference', items: [
      {
        type: 'link',
        text: 'Version Catalog',
        href: '/deprecation',
        info: lastCatalogScan
          ? (<Box variant="small" color="text-status-inactive" fontSize="body-s"><i>{lastCatalogScan}</i></Box>) as unknown as React.ReactNode
          : (<Box variant="small">&nbsp;</Box>) as unknown as React.ReactNode,
      },
    ]},
    { type: 'section', text: '⚙️ Administration', items: [
      { type: 'link', text: 'Settings', href: '/settings' },
    ]},
  ];

  return (
    <AppLayout
      navigationOpen={navOpen}
      onNavigationChange={({ detail }) => setNavOpen(detail.open)}
      navigation={
        <SideNavigation
          header={{ text: 'Upgrade Assistant', href: '/' }}
          activeHref={location.pathname}
          items={navItems}
          onFollow={(e) => {
            e.preventDefault();
            navigate(e.detail.href);
          }}
        />
      }
      content={<Outlet />}
      headerSelector="#top-bar"
      contentHeader={
        <SpaceBetween size="xxs">
          <Box variant="h1">{pageTitle}</Box>
          {accountId && (
            <Box variant="small" color="text-body-secondary">
              Account: {accountId}
            </Box>
          )}
        </SpaceBetween>
      }
      toolsHide
    />
  );
}
