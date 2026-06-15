import { useEffect, useState } from 'react';

const BEHIND_COLOR = '#b45252';
const DEFAULT_START = '38.9497, -77.0523';
const METERS_PER_MILE = 1609.344;

function quickWinFillColor(pct) {
  if (pct >= 95) return 'var(--color-accent)';
  if (pct >= 80) return '#8fb87a'; // sage
  if (pct >= 50) return '#c4b83a'; // amber
  return '#c4793a'; // clay
}

function QuickWins({ trails, onFocusTrail }) {
  const items = (trails?.features ?? [])
    .map((f) => f.properties)
    .filter((p) => p.pct_complete_total > 0.05 && p.pct_complete_total < 0.995)
    .map((p) => ({
      ...p,
      remainingMiles:
        (p.length_meters * (1 - p.pct_complete_total)) / METERS_PER_MILE,
    }))
    .sort((a, b) => b.pct_complete_total - a.pct_complete_total);

  if (items.length === 0) {
    return <div className="qw-empty">No trails in progress</div>;
  }

  const totalRemaining = items.reduce((sum, t) => sum + t.remainingMiles, 0);

  return (
    <div>
      <div className="qw-summary">
        {items.length} trails within striking distance —{' '}
        {totalRemaining.toFixed(1)} mi to finish them all
      </div>
      <ul className="qw-list">
        {items.map((t) => {
          const pct = t.pct_complete_total * 100;
          return (
            <li key={t.id} className="qw-row" onClick={() => onFocusTrail(t.id)}>
              <div className="qw-top">
                <span className="qw-name">{t.name}</span>
                <span className="qw-left">{t.remainingMiles.toFixed(1)} mi left</span>
              </div>
              <div className="qw-track">
                <div
                  className="qw-fill"
                  style={{ width: `${pct}%`, background: quickWinFillColor(pct) }}
                />
              </div>
              <div className="qw-pct">{pct.toFixed(0)}%</div>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

function DeadlineSection({ deadline }) {
  if (!deadline) return null;
  if (deadline.days_remaining <= 0) {
    return (
      <div className="deadline-section">
        <div className="section-label">Departure Deadline</div>
        <div className="deadline-over">Departure date reached</div>
      </div>
    );
  }

  const dateLabel = new Date(
    `${deadline.departure_date}T00:00:00`
  ).toLocaleDateString('en-US', { month: 'long', day: 'numeric' });
  const hasPace = deadline.current_pace_mpw != null;
  const onTrack = !!deadline.on_track;
  const paceColor = hasPace
    ? onTrack
      ? 'var(--color-accent)'
      : BEHIND_COLOR
    : 'var(--color-text-secondary)';
  const fillPct =
    hasPace && deadline.required_pace_mpw > 0
      ? Math.min(100, (deadline.current_pace_mpw / deadline.required_pace_mpw) * 100)
      : 0;

  return (
    <div className="deadline-section">
      <div className="section-label">Departure Deadline</div>
      <div className="deadline-days">
        <span className="deadline-num">{deadline.days_remaining}</span> days until{' '}
        {dateLabel}
      </div>
      <div className="deadline-line">
        {deadline.remaining_miles.toFixed(1)} mi remaining
      </div>
      <div className="deadline-line">
        Need{' '}
        {deadline.required_pace_mpw != null
          ? deadline.required_pace_mpw.toFixed(1)
          : '--'}{' '}
        mi/week
      </div>
      <div className="deadline-line" style={{ color: paceColor }}>
        Current pace {hasPace ? deadline.current_pace_mpw.toFixed(1) : '--'} mi/week
      </div>
      {hasPace && (
        <div className="pace-row">
          <div className="pace-track">
            <div
              className="pace-fill"
              style={{ width: `${fillPct}%`, background: paceColor }}
            />
          </div>
          <span className="pace-label" style={{ color: paceColor }}>
            {onTrack ? 'ON TRACK' : 'BEHIND PACE'}
          </span>
        </div>
      )}
    </div>
  );
}

function RoutePlanner({ route, onRouteChange }) {
  const [open, setOpen] = useState(false);
  const [miles, setMiles] = useState(5.0);
  const [editingStart, setEditingStart] = useState(false);
  const [startText, setStartText] = useState(DEFAULT_START);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  const buildRoute = async () => {
    setError(null);
    const [lat, lng] = startText.split(',').map((s) => parseFloat(s.trim()));
    if (!Number.isFinite(lat) || !Number.isFinite(lng)) {
      setError('Invalid coordinates — use "lat, lng"');
      return;
    }
    setLoading(true);
    try {
      const res = await fetch('/api/suggest-route', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ start_lat: lat, start_lng: lng, target_miles: miles }),
      });
      if (!res.ok) {
        const detail = await res.json().catch(() => null);
        throw new Error(detail?.detail || `route request failed (${res.status})`);
      }
      onRouteChange(await res.json());
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  const downloadGpx = async () => {
    const res = await fetch(route.gpx_download_url);
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'rock-creek-route.gpx';
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  };

  return (
    <>
      <button className="plan-btn" onClick={() => setOpen(!open)}>
        Plan a Route
      </button>
      <div className={`route-panel ${open ? 'open' : ''}`}>
        {!route ? (
          <>
            <h2 className="route-title">Suggest a Route</h2>
            <label className="route-label">
              Target distance: {miles.toFixed(1)} miles
            </label>
            <input
              type="range"
              min="1"
              max="15"
              step="0.5"
              value={miles}
              onChange={(e) => setMiles(parseFloat(e.target.value))}
              className="route-slider"
            />
            <div className="route-start">
              <span>Starting from: Rock Creek Nature Center</span>
              {!editingStart ? (
                <a className="route-link" onClick={() => setEditingStart(true)}>
                  Change starting point
                </a>
              ) : (
                <input
                  className="route-input"
                  value={startText}
                  onChange={(e) => setStartText(e.target.value)}
                  placeholder="lat, lng"
                />
              )}
            </div>
            <button className="route-build" onClick={buildRoute} disabled={loading}>
              Build Route
            </button>
            {loading && (
              <div className="route-loading">Finding uncovered segments...</div>
            )}
            {error && <div className="route-error">{error}</div>}
          </>
        ) : (
          <>
            <h2 className="route-title">Suggested Route</h2>
            <div className="route-summary">
              {route.total_miles.toFixed(1)} miles total —{' '}
              {route.new_coverage_miles.toFixed(1)} miles of new trail
            </div>
            <ul className="route-trails">
              {route.trails_touched.map((t) => (
                <li key={t.trail_id}>
                  <span>{t.trail_name}</span>
                  <span>{t.segment_miles.toFixed(1)} mi</span>
                </li>
              ))}
            </ul>
            <button className="route-build" onClick={downloadGpx}>
              Download GPX
            </button>
            <a className="route-link route-clear" onClick={() => onRouteChange(null)}>
              Clear Route
            </a>
          </>
        )}
      </div>
    </>
  );
}

/** Animate a number from 0 to `target` once it (or the target) is ready,
 *  ease-out over ~1.4s — the "running odometer" effect. */
function useCountUp(target, duration = 1400) {
  const [value, setValue] = useState(0);
  useEffect(() => {
    if (target == null) return;
    let raf;
    const start = performance.now();
    const tick = (now) => {
      const t = Math.min(1, (now - start) / duration);
      const eased = 1 - Math.pow(1 - t, 3);
      setValue(target * eased);
      if (t < 1) raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [target, duration]);
  return value;
}

function OdometerSection({ stats }) {
  const total = stats.in_park_miles;
  const count = useCountUp(total);
  if (total == null) return null; // pipeline hasn't computed it yet

  const foot = stats.in_park_foot_miles;
  const bike = stats.in_park_bike_miles;
  const breakdown =
    foot != null && bike != null
      ? `${foot.toFixed(1)} mi on foot · ${bike.toFixed(1)} mi by bike`
      : null;

  return (
    <div className="odometer-section">
      <div className="section-label">Lifetime Miles In The Park</div>
      <div className="odometer-value">
        {count.toLocaleString('en-US', {
          minimumFractionDigits: 1,
          maximumFractionDigits: 1,
        })}
        <span className="odometer-unit">mi</span>
      </div>
      {breakdown && <div className="odometer-breakdown">{breakdown}</div>}
    </div>
  );
}

export default function StatsPanel({
  stats,
  deadline,
  trails,
  route,
  onRouteChange,
  onFocusTrail,
}) {
  const [fillPct, setFillPct] = useState(0);
  const [view, setView] = useState('overview'); // ephemeral; resets on reload

  useEffect(() => {
    // let the bar mount at 0 width, then transition to the real value
    const timer = setTimeout(() => setFillPct(stats.overall_pct), 80);
    return () => clearTimeout(timer);
  }, [stats.overall_pct]);

  return (
    <aside className="stats-panel">
      <div className="panel-tabs">
        <button
          className={`panel-tab ${view === 'overview' ? 'active' : ''}`}
          onClick={() => setView('overview')}
        >
          Overview
        </button>
        <button
          className={`panel-tab ${view === 'quickwins' ? 'active' : ''}`}
          onClick={() => setView('quickwins')}
        >
          Quick Wins
        </button>
      </div>

      <div className="stats-kicker">Rock Creek Park</div>
      <h1 className="stats-title">Trail Tracker</h1>

      {view === 'quickwins' ? (
        <QuickWins trails={trails} onFocusTrail={onFocusTrail} />
      ) : (
        <>
      <div className="stats-pct">{stats.overall_pct.toFixed(1)}%</div>
      <div className="progress-track">
        <div className="progress-fill" style={{ width: `${fillPct}%` }} />
      </div>

      <DeadlineSection deadline={deadline} />

      <div className="stats-rows">
        <div className="stats-row">
          <span>Trails complete</span>
          <span>
            {stats.complete} / {stats.total_trails}
          </span>
        </div>
        <div className="stats-row">
          <span>Distance covered</span>
          <span>
            {stats.covered_km} of {stats.total_km} km
          </span>
        </div>
        <div className="stats-row">
          <span>On foot</span>
          <span>{stats.covered_foot_km} km</span>
        </div>
        <div className="stats-row">
          <span>By bike</span>
          <span>{stats.covered_bike_km} km</span>
        </div>
      </div>

      <OdometerSection stats={stats} />

      <div className="legend">
        <div className="legend-item">
          <span className="legend-dot dot-foot" />
          Covered on foot
        </div>
        <div className="legend-item">
          <span className="legend-dot dot-bike" />
          Covered by bike
        </div>
        <div className="legend-item">
          <span className="legend-dot dot-uncovered" />
          Not covered
        </div>
      </div>

      <RoutePlanner route={route} onRouteChange={onRouteChange} />
        </>
      )}
    </aside>
  );
}
