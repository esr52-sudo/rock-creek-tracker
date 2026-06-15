import { useEffect, useState } from 'react';
import TrailMap from './components/TrailMap.jsx';
import StatsPanel from './components/StatsPanel.jsx';

export default function App() {
  const [trails, setTrails] = useState(null);
  const [stats, setStats] = useState(null);
  const [deadline, setDeadline] = useState(null);
  const [route, setRoute] = useState(null);
  const [focusTrail, setFocusTrail] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    const get = (url) =>
      fetch(url).then((res) => {
        if (!res.ok) throw new Error(`${url} returned ${res.status}`);
        return res.json();
      });
    // deadline is non-critical: a failure there shouldn't blank the map
    Promise.all([get('/api/trails'), get('/api/stats'), get('/api/deadline').catch(() => null)])
      .then(([trailData, statData, deadlineData]) => {
        setTrails(trailData);
        setStats(statData);
        setDeadline(deadlineData);
      })
      .catch((err) => setError(err.message));
  }, []);

  return (
    <div className="app">
      <TrailMap trails={trails} route={route} focusTrail={focusTrail} />
      {stats && (
        <StatsPanel
          stats={stats}
          deadline={deadline}
          trails={trails}
          route={route}
          onRouteChange={setRoute}
          onFocusTrail={(id) => setFocusTrail({ id, ts: Date.now() })}
        />
      )}
      {error && (
        <div className="error-banner">
          Could not load trail data: {error}. Is the API running on port 8000?
        </div>
      )}
    </div>
  );
}
