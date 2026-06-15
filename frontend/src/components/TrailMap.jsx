import { useEffect, useMemo, useRef } from 'react';
import {
  MapContainer,
  TileLayer,
  GeoJSON,
  Polyline,
  ScaleControl,
  useMap,
} from 'react-leaflet';
import L from 'leaflet';
import convex from '@turf/convex';

const PARK_CENTER = [38.9525, -77.045];
const METERS_PER_MILE = 1609.344;
const ROUTE_COLOR = '#e8c547'; // warm yellow

const TILE_URL =
  'https://tiles.stadiamaps.com/tiles/stamen_terrain/{z}/{x}/{y}{r}.png';
const TILE_ATTRIBUTION =
  'Map tiles by <a href="https://stamen.com">Stamen Design</a>; ' +
  'Data by <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors; ' +
  'Hosted by <a href="https://stadiamaps.com/">Stadia Maps</a>';

// stacking order, bottom to top
const SEGMENT_KINDS = [
  { key: 'uncovered', colorVar: '--color-uncovered', opacity: 0.6 },
  { key: 'bike', colorVar: '--color-covered-bike', opacity: 0.85 },
  { key: 'foot', colorVar: '--color-covered-foot', opacity: 0.85 },
];

const HULL_STYLE = {
  fillColor: '#2d4a2d',
  fillOpacity: 0.08,
  stroke: true,
  color: '#4a7c59',
  weight: 1.5,
  opacity: 0.3,
  className: 'park-hull', // pointer-events: none in CSS
};

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function statusColor(props) {
  if (props.is_complete) return cssVar('--color-complete');
  if (props.pct_complete_total > 0) return cssVar('--color-partial');
  return cssVar('--color-incomplete');
}

function escapeHtml(text) {
  return String(text ?? '').replace(
    /[&<>"']/g,
    (ch) =>
      ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[ch]
  );
}

function popupHtml(props) {
  const totalMi = props.length_meters / METERS_PER_MILE;
  const footMi = totalMi * props.pct_complete_foot;
  const bikeMi = totalMi * props.pct_complete_bike;
  const pct = props.pct_complete_total * 100;
  const color = statusColor(props);

  const icons = `${footMi > 0 ? '\u{1F97E}' : ''}${bikeMi > 0 ? '\u{1F6B2}' : ''}`;
  const subtitle = `${icons ? `${icons} ` : ''}${totalMi.toFixed(1)} mi`;

  const cells = [
    ['Total', `${totalMi.toFixed(1)} mi`],
    ['Complete', `${Math.round(pct)}%`],
    ['On foot', footMi > 0 ? `${footMi.toFixed(1)} mi` : '—'],
    ['By bike', bikeMi > 0 ? `${bikeMi.toFixed(1)} mi` : '—'],
  ]
    .map(
      ([label, value]) =>
        `<div class="popup-cell"><div class="popup-cell-label">${label}</div>` +
        `<div class="popup-cell-value">${value}</div></div>`
    )
    .join('');

  const pills = (props.activities || [])
    .map(
      (a) =>
        `<a class="pill" href="${a.url}" target="_blank" rel="noreferrer">` +
        `${escapeHtml(a.date || a.name)}</a>`
    )
    .join('');
  const activities = pills
    ? `<div class="popup-activities">` +
      `<div class="popup-section-label">Linked activities</div>` +
      `<div class="popup-pills">${pills}</div></div>`
    : '';

  const description = props.description
    ? `<div class="popup-desc-wrap">` +
      `<div class="popup-desc">${escapeHtml(props.description)}</div></div>`
    : '';

  return (
    `<div class="popup-card">` +
    `<div class="popup-accent" style="background:${color}"></div>` +
    `<div class="popup-header">` +
    `<h3 class="popup-name">${escapeHtml(props.name)}</h3>` +
    `<div class="popup-subtitle">${subtitle}</div>` +
    `</div>` +
    `<div class="popup-grid">${cells}</div>` +
    `<div class="popup-bar">` +
    `<div class="popup-bar-fill" data-pct="${pct.toFixed(1)}" style="background:${color}"></div>` +
    `</div>` +
    activities +
    description +
    `</div>`
  );
}

function baseStyle(kind) {
  return {
    color: cssVar(kind.colorVar),
    weight: 3,
    opacity: kind.opacity,
    lineCap: 'round',
    lineJoin: 'round',
  };
}

/** One trail = up to three stacked GeoJSON layers (uncovered, bike, foot)
 *  that highlight, fly, and open popups as a single unit. */
function TrailGroup({ feature, focusTrail }) {
  const map = useMap();
  const groupRefs = useRef({});
  const props = feature.properties;

  const eachGroup = (fn) => {
    for (const kind of SEGMENT_KINDS) {
      const grp = groupRefs.current[kind.key];
      if (grp) fn(kind, grp);
    }
  };

  // Quick Wins row click: fly to this trail and pulse weight 3 -> 8 -> 3
  useEffect(() => {
    if (!focusTrail || focusTrail.id !== props.id) return;
    const bounds = L.latLngBounds([]);
    eachGroup((_, grp) => bounds.extend(grp.getBounds()));
    if (bounds.isValid()) {
      map.flyToBounds(bounds, { padding: [60, 60], duration: 0.8 });
    }
    eachGroup((_, grp) => {
      grp.eachLayer((layer) => layer.getElement()?.classList.add('trail-pulse'));
      grp.setStyle({ weight: 8 });
    });
    const timer = setTimeout(() => {
      eachGroup((kind, grp) => {
        grp.setStyle(baseStyle(kind));
        grp.eachLayer((layer) =>
          setTimeout(() => layer.getElement()?.classList.remove('trail-pulse'), 320)
        );
      });
    }, 300);
    return () => clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focusTrail]);

  const handlers = {
    mouseover: () =>
      eachGroup((kind, grp) => {
        grp.setStyle({ weight: 6, opacity: 1.0 });
        grp.eachLayer((layer) => {
          const el = layer.getElement();
          if (el) {
            // drop-shadow uses currentColor, so mirror the stroke onto `color`
            el.style.color = cssVar(kind.colorVar);
            el.classList.add('trail-glow');
          }
        });
      }),
    mouseout: () =>
      eachGroup((kind, grp) => {
        grp.setStyle(baseStyle(kind));
        grp.eachLayer((layer) => layer.getElement()?.classList.remove('trail-glow'));
      }),
    click: () => {
      const bounds = L.latLngBounds([]);
      eachGroup((_, grp) => bounds.extend(grp.getBounds()));
      if (bounds.isValid()) {
        map.flyToBounds(bounds, { padding: [60, 60], duration: 0.8 });
      }
    },
  };

  const html = popupHtml(props);

  return (
    <>
      {SEGMENT_KINDS.map((kind) => {
        const geom = props.segments?.[kind.key];
        if (!geom) return null;
        return (
          <GeoJSON
            key={`${props.id}-${kind.key}`}
            data={geom}
            style={() => baseStyle(kind)}
            eventHandlers={handlers}
            onEachFeature={(_, layer) => {
              layer.bindPopup(html, {
                className: 'custom-popup',
                maxWidth: 300,
                minWidth: 300,
              });
              // the completion bar mounts at width 0; animate to its real
              // value once the popup DOM exists
              layer.on('popupopen', (e) => {
                const fill = e.popup
                  .getElement()
                  ?.querySelector('.popup-bar-fill');
                if (fill) {
                  requestAnimationFrame(() => {
                    fill.style.width = `${fill.dataset.pct}%`;
                  });
                }
              });
            }}
            ref={(grp) => {
              groupRefs.current[kind.key] = grp;
            }}
          />
        );
      })}
    </>
  );
}

export default function TrailMap({ trails, route, focusTrail }) {
  // approximate park boundary: convex hull of every trail geometry
  const hull = useMemo(() => {
    if (!trails) return null;
    try {
      return convex(trails);
    } catch {
      return null;
    }
  }, [trails]);

  return (
    <MapContainer center={PARK_CENTER} zoom={13} className="map-root">
      <TileLayer url={TILE_URL} attribution={TILE_ATTRIBUTION} maxZoom={18} />
      <ScaleControl position="bottomleft" imperial metric={false} />
      {/* boundary renders first, so it sits below all trail polylines */}
      {hull && <GeoJSON data={hull} style={() => HULL_STYLE} />}
      {trails &&
        trails.features.map((feature) => (
          <TrailGroup
            key={feature.properties.id}
            feature={feature}
            focusTrail={focusTrail}
          />
        ))}
      {/* suggested route renders last, so it sits above all trail layers */}
      {route &&
        route.parts.map((part, i) => (
          <Polyline
            key={`${route.route_id}-${i}`}
            positions={part.geojson.coordinates.map(([lng, lat]) => [lat, lng])}
            pathOptions={{
              color: ROUTE_COLOR,
              weight: 4,
              dashArray: '8, 6',
              opacity: part.kind === 'connector' ? 0.4 : 1.0,
              lineCap: 'round',
              lineJoin: 'round',
              className: 'route-march',
            }}
          />
        ))}
    </MapContainer>
  );
}
