import { RealtimeProvider } from '@/context/RealtimeContext';
import Dashboard from '@/pages/Dashboard';

export default function App() {
  return (
    <RealtimeProvider>
      <Dashboard />
    </RealtimeProvider>
  );
}
