import { BrowserRouter, Routes, Route } from 'react-router-dom';
import Shell from './layouts/AppLayout';
import Dashboard from './pages/Dashboard';
import DatabaseList from './pages/DatabaseList';
import ClusterDetail from './pages/ClusterDetail';
import ClusterAnalysisPage from './pages/ClusterAnalysisPage';
import DeprecationAnalysis from './pages/DeprecationAnalysis';
import CodeAnalysis from './pages/CodeAnalysis';
import UpgradeTest from './pages/UpgradeTest';
import UpgradeTestList from './pages/UpgradeTestList';
import Settings from './pages/Settings';

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route element={<Shell />}>
          <Route index element={<Dashboard />} />
          <Route path="/databases" element={<DatabaseList />} />
          <Route path="/cluster/:clusterArn" element={<ClusterDetail />} />
          <Route path="/cluster-analysis" element={<ClusterAnalysisPage />} />
          <Route path="/deprecation" element={<DeprecationAnalysis />} />
          <Route path="/code-analysis" element={<CodeAnalysis />} />
          <Route path="/upgrade-tests" element={<UpgradeTestList />} />
          <Route path="/upgrade-test/:runId" element={<UpgradeTest />} />
          <Route path="/settings" element={<Settings />} />
        </Route>
      </Routes>
    </BrowserRouter>
  );
}
