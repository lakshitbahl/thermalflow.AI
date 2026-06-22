import { useMemo, useState } from 'react';
import {
  LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, Legend,
  ResponsiveContainer, BarChart, Bar, Cell, ReferenceLine,
} from 'recharts';
import { useRealtime, VIEW_ZONES } from '../context/RealtimeContext';

const PLANT_API = (import.meta.env.VITE_PLANT_API as string) || 'http://localhost:8090';
const RELAY_API = (import.meta.env.VITE_RELAY_API as string) || 'http://localhost:8091';
const HEALTH_API = (import.meta.env.VITE_HEALTH_API as string) || 'http://localhost:8092';
const ACTUATION_MODE = (import.meta.env.VITE_ACTUATION_MODE as string) || 'SHADOW';
const THETA = 3.0;
const INLET_THETA = 2.0;

const C = {
  bg: '#0f1320', panel: '#161b29', border: '#2a3142', text: '#c7d0e0', muted: '#6b7689',
  meas: '#22d3ee', predS: '#a78bfa', predR: '#fb923c', truth: '#4ade80',
  red: '#f87171', amber: '#fbbf24', green: '#4ade80',
};
const mono = "'JetBrains Mono', ui-monospace, monospace";

function residualWeight(sid: string): number {
  if (sid.endsWith('-inlet')) return 0.3;
  if (sid === 'hot-aisle') return 1.0;
  if (sid.startsWith('rack-')) return 1.0;
  if (sid === 'crac-supply') return 0.6;
  if (sid.startsWith('cold-aisle')) return 0.3;
  return 0.5;
}

function Panel({ title, sub, children }: { title: string; sub?: string; children: React.ReactNode }) {
  return (
    <div style={{ background: C.panel, border: `1px solid ${C.border}`, borderRadius: 8, padding: 16 }}>
      <div style={{ marginBottom: 12 }}>
        <div style={{ fontSize: 11, letterSpacing: '0.12em', textTransform: 'uppercase', color: C.muted, fontFamily: mono }}>{title}</div>
        {sub && <div style={{ fontSize: 12, color: C.muted, marginTop: 2 }}>{sub}</div>}
      </div>
      {children}
    </div>
  );
}

function TokenBar() {
  const { setToken } = useRealtime();
  const [v, setV] = useState('');
  return (
    <div style={{ background: C.panel, border: `1px solid ${C.border}`, borderRadius: 8, padding: 16, marginBottom: 16 }}>
      <div style={{ fontSize: 13, color: C.text, marginBottom: 8 }}>
        Not connected. Paste a JWT (mint with <code style={{ color: C.amber }}>docker compose exec bff node -e "..."</code>) — tenant must be <b>demo</b>.
      </div>
      <div style={{ display: 'flex', gap: 8 }}>
        <input id="jwt-token" name="jwt-token" autoComplete="off" value={v} onChange={(e) => setV(e.target.value)} placeholder="eyJhbGci..."
          style={{ flex: 1, background: C.bg, border: `1px solid ${C.border}`, color: C.text, padding: '8px 10px', borderRadius: 6, fontFamily: mono, fontSize: 12 }} />
        <button onClick={() => v && setToken(v.trim())}
          style={{ background: C.meas, color: '#06222a', border: 'none', padding: '8px 16px', borderRadius: 6, fontWeight: 700, cursor: 'pointer' }}>Connect</button>
      </div>
    </div>
  );
}

function P1ResidualDecomp() {
  const { history, frame } = useRealtime();
  const [zone, setZone] = useState('hot-aisle');
  const f = frame[zone] || { meas: null, truth: null, predStatic: null, predRls: null };
  const biasS = f.predStatic != null && f.truth != null ? f.predStatic - f.truth : null;
  const biasR = f.predRls != null && f.truth != null ? f.predRls - f.truth : null;
  const noise = f.meas != null && f.truth != null ? f.meas - f.truth : null;
  const fmt = (x: number | null) => (x != null ? `${x >= 0 ? '+' : ''}${x.toFixed(2)}°C` : '—');
  return (
    <Panel title="1 · Residual Decomposition" sub="truth vs measured vs static-ROM vs RLS-ROM — does online tuning close the gap?">
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginBottom: 10 }}>
        {VIEW_ZONES.map((v) => (
          <button key={v.key} onClick={() => setZone(v.key)}
            style={{ background: zone === v.key ? C.meas : 'transparent', color: zone === v.key ? '#06222a' : C.muted,
              border: `1px solid ${zone === v.key ? C.meas : C.border}`, borderRadius: 5, padding: '3px 9px', fontSize: 11, cursor: 'pointer', fontFamily: mono }}>{v.label}</button>
        ))}
      </div>
      <ResponsiveContainer width="100%" height={220}>
        <LineChart data={history} margin={{ top: 4, right: 8, bottom: 0, left: -16 }}>
          <CartesianGrid stroke={C.border} strokeDasharray="2 4" />
          <XAxis dataKey="ts" stroke={C.muted} tick={{ fontSize: 10 }} />
          <YAxis stroke={C.muted} tick={{ fontSize: 10 }} domain={['auto', 'auto']} unit="°" />
          <Tooltip contentStyle={{ background: C.bg, border: `1px solid ${C.border}`, fontSize: 11 }} />
          <Legend wrapperStyle={{ fontSize: 11 }} />
          <Line type="monotone" dataKey={`${zone}_truth`} name="truth" stroke={C.truth} dot={false} strokeWidth={1.5} strokeDasharray="4 3" isAnimationActive={false} />
          <Line type="monotone" dataKey={`${zone}_predstatic`} name="static ROM" stroke={C.predS} dot={false} strokeWidth={2} isAnimationActive={false} />
          <Line type="monotone" dataKey={`${zone}_predrls`} name="RLS ROM" stroke={C.predR} dot={false} strokeWidth={2} isAnimationActive={false} />
          <Line type="monotone" dataKey={`${zone}_meas`} name="measured" stroke={C.meas} dot={false} strokeWidth={0.75} isAnimationActive={false} />
        </LineChart>
      </ResponsiveContainer>
      <div style={{ display: 'flex', gap: 20, marginTop: 8, fontFamily: mono, fontSize: 12, flexWrap: 'wrap' }}>
        <span style={{ color: C.predS }}>static bias: {fmt(biasS)}</span>
        <span style={{ color: C.predR }}>RLS bias: {fmt(biasR)}</span>
        <span style={{ color: C.meas }}>sensor noise: {fmt(noise)}</span>
      </div>
    </Panel>
  );
}

function P2Spatial() {
  const { frame } = useRealtime();
  const data = VIEW_ZONES.map((z) => ({
    zone: z.label, truth: frame[z.key]?.truth ?? null,
    static: frame[z.key]?.predStatic ?? null, rls: frame[z.key]?.predRls ?? null,
  }));
  return (
    <Panel title="2 · Spatial State (6 zones)" sub="truth vs static ROM vs RLS ROM">
      <ResponsiveContainer width="100%" height={240}>
        <BarChart data={data} margin={{ top: 4, right: 8, bottom: 0, left: -16 }}>
          <CartesianGrid stroke={C.border} strokeDasharray="2 4" vertical={false} />
          <XAxis dataKey="zone" stroke={C.muted} tick={{ fontSize: 9 }} interval={0} angle={-18} textAnchor="end" height={50} />
          <YAxis stroke={C.muted} tick={{ fontSize: 10 }} unit="°" />
          <Tooltip contentStyle={{ background: C.bg, border: `1px solid ${C.border}`, fontSize: 11 }} />
          <Legend wrapperStyle={{ fontSize: 11 }} />
          <Bar dataKey="truth" name="truth" fill={C.truth} opacity={0.85} />
          <Bar dataKey="static" name="static" fill={C.predS} opacity={0.85} />
          <Bar dataKey="rls" name="RLS" fill={C.predR} opacity={0.85} />
        </BarChart>
      </ResponsiveContainer>
    </Panel>
  );
}

function P3TripWire() {
  const { residualsStatic } = useRealtime();
  const { weighted, driver, inletMax } = useMemo(() => {
    let weighted = 0, driver = '', inletMax = 0;
    for (const [sid, r] of Object.entries(residualsStatic)) {
      const w = residualWeight(sid) * Math.abs(r);
      if (w > weighted) { weighted = w; driver = sid; }
      if (sid.endsWith('-inlet')) inletMax = Math.max(inletMax, Math.abs(r));
    }
    return { weighted, driver, inletMax };
  }, [residualsStatic]);
  const trip = weighted > THETA, inletTrip = inletMax > INLET_THETA;
  const bars = VIEW_ZONES.map((z) => {
    let r = 0;
    if (z.sensorZone) r = Math.abs(residualsStatic[z.sensorZone] ?? 0);
    else if (z.sensorRacks) r = Math.max(...z.sensorRacks.map((k) => Math.abs(residualsStatic[`rack-${k}`] ?? 0)));
    return { zone: z.label, weighted: +(r * z.weight).toFixed(2) };
  });
  return (
    <Panel title="3 · L2 Trip-Wire (static ROM)" sub="weighted vector norm vs naive inlet-only scalar">
      <div style={{ display: 'flex', gap: 16 }}>
        <div style={{ flex: '0 0 168px', background: trip ? 'rgba(248,113,113,0.12)' : 'rgba(74,222,128,0.08)',
          border: `1px solid ${trip ? C.red : C.green}`, borderRadius: 8, padding: 14, textAlign: 'center' }}>
          <div style={{ fontSize: 11, color: C.muted, fontFamily: mono }}>VECTOR TRIP-WIRE</div>
          <div style={{ fontSize: 26, fontWeight: 800, color: trip ? C.red : C.green, fontFamily: mono, margin: '4px 0' }}>{trip ? 'TRIP' : 'MONITORING'}</div>
          <div style={{ fontSize: 12, color: C.text, fontFamily: mono }}>{weighted.toFixed(2)} / θ {THETA.toFixed(1)}°C</div>
          <div style={{ fontSize: 10, color: C.muted, marginTop: 4 }}>driver: {driver || '—'}</div>
        </div>
        <div style={{ flex: 1 }}>
          <ResponsiveContainer width="100%" height={140}>
            <BarChart data={bars} margin={{ top: 4, right: 8, bottom: 0, left: -20 }}>
              <CartesianGrid stroke={C.border} strokeDasharray="2 4" vertical={false} />
              <XAxis dataKey="zone" stroke={C.muted} tick={{ fontSize: 8 }} interval={0} angle={-18} textAnchor="end" height={44} />
              <YAxis stroke={C.muted} tick={{ fontSize: 10 }} unit="°" />
              <ReferenceLine y={THETA} stroke={C.red} strokeDasharray="4 3" label={{ value: 'θ', fill: C.red, fontSize: 10 }} />
              <Bar dataKey="weighted" name="w·|residual|">{bars.map((b, i) => <Cell key={i} fill={b.weighted > THETA ? C.red : C.amber} />)}</Bar>
            </BarChart>
          </ResponsiveContainer>
        </div>
      </div>
      <div style={{ marginTop: 10, padding: 8, borderRadius: 6, background: C.bg, border: `1px solid ${C.border}`, fontFamily: mono, fontSize: 12 }}>
        Naive inlet-only: <b style={{ color: inletTrip ? C.red : C.muted }}>{inletMax.toFixed(2)}°C</b> / {INLET_THETA.toFixed(1)}°C → <b style={{ color: inletTrip ? C.red : C.green }}>{inletTrip ? 'would trip' : 'WOULD NOT TRIP'}</b>
      </div>
    </Panel>
  );
}

function P4Faults() {
  const { faultEvents, activeFaults, simTime } = useRealtime();
  const [busy, setBusy] = useState(false);
  const post = async (body: object | null) => {
    setBusy(true);
    try {
      await fetch(body ? `${PLANT_API}/fault` : `${PLANT_API}/fault/clear`,
        { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: body ? JSON.stringify(body) : undefined });
    } catch { /* no-op */ } finally { setBusy(false); }
  };
  const btn = { background: 'transparent', color: C.text, border: `1px solid ${C.border}`, borderRadius: 5, padding: '6px 10px', fontSize: 11, cursor: 'pointer', fontFamily: mono } as const;
  return (
    <Panel title="4 · Fault Injection & Timeline" sub={`plant-sim API @ ${PLANT_API}`}>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginBottom: 10 }}>
        <button style={btn} disabled={busy} onClick={() => post({ type: 'crac_degradation', airflow_drop_pct: 50 })}>CRAC −50%</button>
        <button style={btn} disabled={busy} onClick={() => post({ type: 'recirculation', cold_aisle: 0, intensity_kgps: 3, duration_s: 30 })}>Recirc CA0 (30s)</button>
        <button style={btn} disabled={busy} onClick={() => post({ type: 'sensor_drift', sensor_id: 'rack-3-inlet', rate_c_per_min: 2 })}>Drift rack-3 inlet</button>
        <button style={btn} disabled={busy} onClick={() => post({ type: 'sensor_dropout', sensor_id: 'rack-5', mode: 'nan' })}>Dropout rack-5</button>
        <button style={btn} disabled={busy} onClick={() => post({ type: 'sensor_spike', rack: 4, position: 'top', delta_c: 24 })}>Spike rack-4 top</button>
        <button style={{ ...btn, borderColor: C.amber, color: C.amber }} disabled={busy} onClick={() => post(null)}>Clear all</button>
      </div>
      <div style={{ fontSize: 11, color: C.muted, fontFamily: mono, marginBottom: 6 }}>
        active: {Object.keys(activeFaults).length ? `${(activeFaults.crac_airflow_pct as number) ?? 100}% airflow` : '—'}
      </div>
      <div style={{ maxHeight: 150, overflowY: 'auto', fontFamily: mono, fontSize: 12 }}>
        {faultEvents.length === 0 && <div style={{ color: C.muted }}>no fault events yet — inject one above</div>}
        {faultEvents.map((e, i) => (
          <div key={i} style={{ display: 'flex', gap: 10, padding: '3px 0', borderBottom: `1px solid ${C.border}` }}>
            <span style={{ color: C.muted, minWidth: 64 }}>t+{Math.round(e.ts)}s</span>
            <span style={{ color: e.text.includes('cleared') ? C.green : C.amber }}>{e.text}</span>
          </div>
        ))}
      </div>
      <div style={{ fontSize: 10, color: C.muted, marginTop: 6 }}>sim time: {Math.round(simTime)}s</div>
    </Panel>
  );
}

function P5MpcAdvisory() {
  const { mpc } = useRealtime();
  const T_CRIT = 32;
  const data = (mpc?.ha_pred || []).map((v, i) => ({ k: (i + 1) * 30, ha: v }));
  const fmtPct = (f: number) => `${Math.round(f * 100)}%`;
  const Row = ({ label, flow, temp, color }: { label: string; flow: number; temp: number; color: string }) => (
    <div style={{ display: 'flex', justifyContent: 'space-between', fontFamily: mono, fontSize: 13, padding: '4px 0' }}>
      <span style={{ color: C.muted }}>{label}</span>
      <span><b style={{ color }}>{fmtPct(flow)}</b> airflow · <b style={{ color }}>{temp.toFixed(1)}°C</b> supply</span>
    </div>
  );
  return (
    <Panel title="5 · MPC Advisory (Gate A · zero actuation)" sub="what the plant runs vs what the MPC would command — and its predicted path">
      {!mpc && <div style={{ color: C.muted, fontFamily: mono, fontSize: 12 }}>awaiting rom.mpc.advisory…</div>}
      {mpc && (
        <>
          <div style={{ background: C.bg, border: `1px solid ${C.border}`, borderRadius: 6, padding: '6px 12px', marginBottom: 10 }}>
            <Row label="baseline (executing)" flow={mpc.baseline.flow} temp={mpc.baseline.temp} color={C.text} />
            <Row label="advisory (MPC wants)" flow={mpc.advisory.flow} temp={mpc.advisory.temp} color={mpc.fallback ? C.amber : C.predR} />
            <div style={{ fontSize: 11, color: C.muted, fontFamily: mono, marginTop: 4 }}>
              {mpc.fallback ? 'solver fell back to baseline' :
                `feasible · slack ${(mpc.slack_max ?? 0).toFixed(2)}°C · ε ${mpc.params?.eps?.toFixed(3)} · ${mpc.solve_ms?.toFixed(1)}ms`}
            </div>
          </div>
          <div style={{ fontSize: 11, color: C.muted, fontFamily: mono, marginBottom: 2 }}>predicted hot-aisle trajectory vs {mpc.T_limit}°C limit</div>
          <ResponsiveContainer width="100%" height={150}>
            <LineChart data={data} margin={{ top: 4, right: 8, bottom: 0, left: -20 }}>
              <CartesianGrid stroke={C.border} strokeDasharray="2 4" />
              <XAxis dataKey="k" stroke={C.muted} tick={{ fontSize: 9 }} unit="s" />
              <YAxis stroke={C.muted} tick={{ fontSize: 10 }} domain={['auto', 'auto']} unit="°" />
              <Tooltip contentStyle={{ background: C.bg, border: `1px solid ${C.border}`, fontSize: 11 }} />
              <ReferenceLine y={mpc.T_limit} stroke={C.red} strokeDasharray="4 3" label={{ value: 'soft', fill: C.red, fontSize: 9 }} />
              <ReferenceLine y={T_CRIT} stroke={C.amber} strokeDasharray="1 3" label={{ value: 'L1', fill: C.amber, fontSize: 9 }} />
              <Line type="monotone" dataKey="ha" name="pred HA" stroke={C.predR} dot={false} strokeWidth={2} isAnimationActive={false} />
            </LineChart>
          </ResponsiveContainer>
        </>
      )}
    </Panel>
  );
}

function P6L1Interlock() {
  const { rackSensors } = useRealtime();
  const T_CRIT = 32;     // 2-of-3 broad-event threshold
  const T_SINGLE = 38;   // any-single absolute limit (localized meltdown — no vote)
  const racks = rackSensors.map((r) => {
    const vals = [r.sensors.top, r.sensors.mid, r.sensors.bottom];
    const over = vals.filter((v) => v > T_CRIT).length;
    const single = vals.some((v) => v > T_SINGLE);
    return { rack: r.rack, vals, over, single, fired: over >= 2 || single,
             reason: single ? 'single>38' : over >= 2 ? '2-of-3' : '', anomaly: over === 1 && !single };
  });
  const fired = racks.filter((r) => r.fired);
  const anomalies = racks.filter((r) => r.anomaly);
  const tripped = fired.length > 0;
  const dot = (v: number) => (v > T_SINGLE ? C.red : v > T_CRIT ? C.amber : v > T_CRIT - 4 ? C.amber : C.muted);
  return (
    <Panel title="6 · L1 Absolute Interlock" sub={`2-of-3 over ${T_CRIT}°C  OR  any 1 over ${T_SINGLE}°C — armed in Gate A, gates in Gate B`}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 10, flexWrap: 'wrap' }}>
        <span style={{ fontFamily: mono, fontWeight: 800, fontSize: 16, color: tripped ? C.red : C.green }}>
          {tripped ? `TRIP · ${fired.map((r) => `${r.rack}(${r.reason})`).join(', ')}` : 'ARMED'}
        </span>
        {anomalies.length > 0 && !tripped && (
          <span style={{ fontFamily: mono, fontSize: 11, color: C.amber }}>
            anomaly on {anomalies.map((r) => r.rack).join(',')} — 1 sensor hot but &lt;{T_SINGLE}°C, 2-of-3 not breached, armed
          </span>
        )}
      </div>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 6 }}>
        {racks.map((r) => (
          <div key={r.rack} style={{ border: `1px solid ${r.fired ? C.red : r.anomaly ? C.amber : C.border}`,
            borderRadius: 5, padding: '5px 7px', background: r.fired ? 'rgba(248,113,113,0.1)' : 'transparent' }}>
            <div style={{ fontSize: 10, color: C.muted, fontFamily: mono, marginBottom: 2 }}>{r.rack}</div>
            <div style={{ display: 'flex', gap: 5, fontFamily: mono, fontSize: 11 }}>
              {r.vals.map((v, i) => (
                <span key={i} style={{ color: dot(v), fontWeight: v > T_SINGLE ? 800 : 400 }}>{v.toFixed(1)}</span>
              ))}
            </div>
          </div>
        ))}
      </div>
      <div style={{ fontSize: 10, color: C.muted, marginTop: 8, fontFamily: mono }}>
        top · mid · bottom (buoyancy: top hottest). Dual threshold closes the gradient blindspot: a single top-of-rack
        spike trips on the {T_SINGLE}°C absolute limit even when the 2-of-3 vote stays at 1.
      </div>
    </Panel>
  );
}

function P7SafetyRelay() {
  const { relay } = useRealtime();
  const tripped = relay?.state === 'TRIPPED';
  const sev = relay?.severity ?? 'none';
  const sevColor = sev === 'break_glass' ? C.red : sev === 'graded' ? C.amber : C.green;
  const resetRelay = async () => {
    try {
      await fetch(`${RELAY_API}/reset`, { method: 'POST', headers: { 'Content-Type': 'application/json' } });
    } catch { /* relay may be unreachable; status stream will reflect truth */ }
  };
  // sole-writer path: MPC --proposal--> RELAY --(forward|fallback)--> PLANT
  const Box = ({ label, accent }: { label: string; accent: string }) => (
    <div style={{ border: `1px solid ${accent}`, borderRadius: 6, padding: '6px 10px', fontFamily: mono,
      fontSize: 11, color: accent, fontWeight: 700, whiteSpace: 'nowrap' }}>{label}</div>
  );
  const Arrow = ({ label, sev: severed }: { label: string; sev: boolean }) => (
    <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', minWidth: 64 }}>
      <span style={{ fontSize: 9, color: severed ? C.red : C.muted, fontFamily: mono }}>{label}</span>
      <span style={{ color: severed ? C.red : C.green, fontWeight: 800 }}>{severed ? '──✕──▶' : '─────▶'}</span>
    </div>
  );
  return (
    <Panel title="7 · Safety Relay" sub="sole writer to plant.control — series gateway, latching trip, severity-scaled fallback">
      <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 12, flexWrap: 'wrap' }}>
        <span style={{ fontFamily: mono, fontWeight: 800, fontSize: 18, color: tripped ? C.red : C.green }}>
          {relay ? (tripped ? 'TRIPPED · LATCHED' : 'ARMED') : '—'}
        </span>
        {tripped && <span style={{ fontFamily: mono, fontSize: 12, color: sevColor }}>
          {sev === 'break_glass' ? 'BREAK-GLASS' : 'graded'} · {relay?.trip_source}
        </span>}
        <span style={{ marginLeft: 'auto', fontFamily: mono, fontSize: 10, padding: '2px 8px', borderRadius: 4,
          border: `1px solid ${ACTUATION_MODE === 'ACTIVE' ? C.amber : C.border}`,
          color: ACTUATION_MODE === 'ACTIVE' ? C.amber : C.muted }}>
          {ACTUATION_MODE === 'ACTIVE' ? 'IRON LIVE' : 'SHADOW · iron disconnected'}
        </span>
      </div>

      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 4, padding: '10px 0',
        background: C.bg, borderRadius: 6, marginBottom: 12 }}>
        <Box label="MPC" accent={C.predS} />
        <Arrow label="proposal" sev={tripped} />
        <Box label="RELAY" accent={tripped ? C.red : C.green} />
        <Arrow label={tripped ? 'fallback' : 'forward'} sev={false} />
        <Box label="PLANT" accent={C.meas} />
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 8, fontFamily: mono, fontSize: 12 }}>
        <div><div style={{ color: C.muted, fontSize: 10 }}>cmd airflow</div>
          <b style={{ color: C.text }}>{relay ? `${(relay.command.flow * 100).toFixed(0)}%` : '—'}</b></div>
        <div><div style={{ color: C.muted, fontSize: 10 }}>cmd supply</div>
          <b style={{ color: C.text }}>{relay ? `${relay.command.temp.toFixed(1)}°C` : '—'}</b></div>
        <div><div style={{ color: C.muted, fontSize: 10 }}>mpc / sensor age</div>
          <b style={{ color: C.text }}>{relay?.age.mpc ?? '—'}s / {relay?.age.sensor ?? '—'}s</b></div>
      </div>

      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginTop: 12 }}>
        <button onClick={resetRelay} disabled={!tripped}
          style={{ background: tripped ? C.amber : C.border, color: tripped ? '#241a00' : C.muted, border: 'none',
            padding: '7px 14px', borderRadius: 6, fontWeight: 700, fontFamily: mono, fontSize: 12,
            cursor: tripped ? 'pointer' : 'not-allowed' }}>
          Operator Reset
        </button>
        <span style={{ fontSize: 10, color: C.muted, fontFamily: mono }}>
          manual re-arm only — a latched trip never auto-clears (no authority chatter)
        </span>
      </div>
    </Panel>
  );
}

function P8CoilHealth() {
  const { health } = useRealtime();
  const st = health?.state ?? 'COMMISSIONING';
  const alarm = st === 'ALARM';
  const sev = health?.severity ?? 'none';
  const col = alarm ? (sev === 'urgent' ? C.red : C.amber) : st === 'OK' ? C.green : C.muted;
  const cusumPct = health ? Math.min(100, (health.cusum / health.cusum_h) * 100) : 0;
  const recommission = async () => {
    try { await fetch(`${HEALTH_API}/commission`, { method: 'POST', headers: { 'Content-Type': 'application/json' } }); }
    catch { /* status stream reflects truth */ }
  };
  return (
    <Panel title="8 · Coil Health (slow)" sub="frozen clean-coil reference · drift detector · maintenance alarm, not a trip">
      <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 12, flexWrap: 'wrap' }}>
        <span style={{ fontFamily: mono, fontWeight: 800, fontSize: 18, color: col }}>
          {health ? (alarm ? `ALARM · ${sev}` : st) : '—'}
        </span>
        <span style={{ fontFamily: mono, fontSize: 12, color: C.muted }}>
          drift <b style={{ color: alarm ? col : C.text }}>{health ? `${health.drift_c >= 0 ? '+' : ''}${health.drift_c.toFixed(2)}°C` : '—'}</b>
        </span>
        <span style={{ marginLeft: 'auto', fontFamily: mono, fontSize: 10, color: health?.sampling ? C.green : C.muted }}>
          {health?.sampling ? '● sampling' : '○ idle (out of reference regime)'}
        </span>
      </div>

      {!health?.commissioned ? (
        <div style={{ fontFamily: mono, fontSize: 12, color: C.muted, marginBottom: 10 }}>
          commissioning baseline… {Math.round((health?.commission_progress ?? 0) * 100)}%
          <div style={{ height: 6, background: C.bg, borderRadius: 3, marginTop: 4 }}>
            <div style={{ height: '100%', width: `${(health?.commission_progress ?? 0) * 100}%`, background: C.predS, borderRadius: 3 }} />
          </div>
        </div>
      ) : (
        <div style={{ marginBottom: 10 }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', fontFamily: mono, fontSize: 10, color: C.muted }}>
            <span>CUSUM (change detector)</span><span>{health.cusum.toFixed(1)} / {health.cusum_h.toFixed(0)}</span>
          </div>
          <div style={{ height: 8, background: C.bg, borderRadius: 4, marginTop: 4, overflow: 'hidden' }}>
            <div style={{ height: '100%', width: `${cusumPct}%`, background: col, borderRadius: 4, transition: 'width 0.4s' }} />
          </div>
          <div style={{ fontFamily: mono, fontSize: 10, color: C.muted, marginTop: 4 }}>
            baseline {health.baseline_c?.toFixed(2)}°C · alarm at CUSUM &gt; {health.cusum_h.toFixed(0)}
          </div>
        </div>
      )}

      <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
        <button onClick={recommission}
          style={{ background: C.border, color: C.text, border: 'none', padding: '7px 14px',
            borderRadius: 6, fontWeight: 700, fontFamily: mono, fontSize: 12, cursor: 'pointer' }}>
          Recommission
        </button>
        <span style={{ fontSize: 10, color: C.muted, fontFamily: mono }}>
          re-zero the baseline after a coil cleaning or a SHADOW/ACTIVE switch
        </span>
      </div>
    </Panel>
  );
}

function RlsBadge() {
  const { rls } = useRealtime();
  if (!rls) return <span style={{ color: C.muted, fontFamily: mono, fontSize: 12 }}>RLS —</span>;
  return (
    <span style={{ fontFamily: mono, fontSize: 12, display: 'flex', gap: 10, alignItems: 'center' }}>
      <span style={{ color: C.predR }}>ε {rls.eps.toFixed(4)}</span>
      <span style={{ color: C.muted }}>U {rls.u_scale.toFixed(3)}</span>
      <span style={{ color: rls.frozen ? C.muted : C.green }}>{rls.frozen ? 'frozen' : 'adapting'}</span>
    </span>
  );
}

export default function Dashboard() {
  const { connected, simTime, cracAirflowPct } = useRealtime();
  return (
    <div style={{ minHeight: '100vh', background: C.bg, color: C.text, padding: 20, fontFamily: "'Inter', system-ui, sans-serif" }}>
      <div style={{ display: 'flex', alignItems: 'baseline', justifyContent: 'space-between', marginBottom: 16, flexWrap: 'wrap', gap: 10 }}>
        <div>
          <div style={{ fontSize: 18, fontWeight: 800 }}>ThermalFlow · Gate B (Shadow)</div>
          <div style={{ fontSize: 12, color: C.muted }}>closed-loop architecture: MPC proposes · safety-relay is sole writer · iron disconnected (ACTUATION_MODE=SHADOW)</div>
        </div>
        <div style={{ display: 'flex', gap: 18, fontFamily: mono, fontSize: 12, alignItems: 'center' }}>
          <RlsBadge />
          <span>airflow <b style={{ color: cracAirflowPct < 100 ? C.amber : C.text }}>{cracAirflowPct}%</b></span>
          <span style={{ color: C.muted }}>t+{Math.round(simTime)}s</span>
          <span style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
            <span style={{ width: 8, height: 8, borderRadius: '50%', background: connected ? C.green : C.red }} />
            {connected ? 'LIVE' : 'OFFLINE'}
          </span>
        </div>
      </div>
      {!connected && <TokenBar />}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(440px, 1fr))', gap: 16 }}>
        <P1ResidualDecomp /><P2Spatial /><P3TripWire /><P4Faults />
        <P5MpcAdvisory /><P6L1Interlock /><P7SafetyRelay /><P8CoilHealth />
      </div>
    </div>
  );
}
