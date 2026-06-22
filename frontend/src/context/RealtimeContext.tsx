import {
  createContext, useContext, useEffect, useRef, useState, useCallback,
  type ReactNode,
} from 'react';

type SensorMsg = {
  ts: number;
  rack_inlet_c: { sensor_id: string; temp_c: number; valid: boolean }[];
  zones: { zone_id: string; temp_c: number; valid: boolean }[];
  crac_airflow_pct: number;
  crac_supply_c: number;
};
type TruthMsg = {
  ts: number; state: number[]; zone_roles: string[]; rack_inlet_c: number[];
  crac_airflow_pct: number; active_faults: Record<string, unknown>;
};
type PredMsg = {
  ts: number; state: Record<string, number>; rack_inlet_pred_c: number[];
  params?: { eps: number; u_scale: number }; frozen?: boolean; trace_P?: number;
};
type ResidualMsg = { ts: number; residuals: Record<string, number>; dropped: string[] };

export type ViewZone = {
  key: string; label: string; romKey: string;
  sensorZone?: string; truthRole?: string;
  sensorRacks?: number[]; truthRacks?: number[]; weight: number;
};

export const VIEW_ZONES: ViewZone[] = [
  { key: 'crac-supply', label: 'CRAC Supply', romKey: 'crac-supply', sensorZone: 'crac-supply', truthRole: 'crac-supply', weight: 0.6 },
  { key: 'cold-aisle-0', label: 'Cold Aisle 0', romKey: 'cold-aisle-0', sensorZone: 'cold-aisle-1', truthRole: 'cold-aisle-1', weight: 0.3 },
  { key: 'cold-aisle-1', label: 'Cold Aisle 1', romKey: 'cold-aisle-1', sensorZone: 'cold-aisle-2', truthRole: 'cold-aisle-2', weight: 0.3 },
  { key: 'rack-mass-0', label: 'Rack Mass 0', romKey: 'rack-mass-0', sensorRacks: [1, 2, 3, 4], truthRacks: [1, 2, 3, 4], weight: 1.0 },
  { key: 'rack-mass-1', label: 'Rack Mass 1', romKey: 'rack-mass-1', sensorRacks: [5, 6, 7, 8], truthRacks: [5, 6, 7, 8], weight: 1.0 },
  { key: 'hot-aisle', label: 'Hot Aisle', romKey: 'hot-aisle', sensorZone: 'hot-aisle', truthRole: 'hot-aisle', weight: 1.0 },
];

export type Cell = { meas: number | null; truth: number | null; predStatic: number | null; predRls: number | null };
export type Frame = Record<string, Cell>;
export type HistRow = { ts: number } & Record<string, number>;
export type FaultEvent = { ts: number; text: string };
export type RlsParams = { eps: number; u_scale: number; frozen: boolean; trace_P: number };
export type RackTrio = { rack: string; sensors: { top: number; mid: number; bottom: number } };
export type MpcAdvisory = {
  ts: number; T_limit: number; feasible: boolean; fallback?: boolean;
  baseline: { flow: number; temp: number }; advisory: { flow: number; temp: number };
  ha_pred: number[]; flow_plan?: number[]; temp_plan?: number[];
  slack_max?: number; solve_ms?: number; params?: { eps: number; u_scale: number };
};

export type RelayStatus = {
  state: 'ARMED' | 'TRIPPED'; latched: boolean;
  trip_source: string | null; severity: 'none' | 'graded' | 'break_glass';
  command: { flow: number; temp: number }; sole_writer: boolean;
  age: { mpc: number | null; sensor: number | null };
};

export type HealthStatus = {
  state: 'OK' | 'COMMISSIONING' | 'ALARM'; alarm: boolean;
  severity: 'none' | 'advisory' | 'urgent'; drift_c: number;
  cusum: number; cusum_h: number; commissioned: boolean;
  commission_progress: number; baseline_c: number | null; sampling: boolean;
};

type RT = {
  connected: boolean; token: string; setToken: (t: string) => void;
  simTime: number; frame: Frame; history: HistRow[];
  residualsStatic: Record<string, number>; residualsRls: Record<string, number>;
  dropped: string[]; activeFaults: Record<string, unknown>;
  faultEvents: FaultEvent[]; cracAirflowPct: number; rls: RlsParams | null;
  mpc: MpcAdvisory | null; rackSensors: RackTrio[]; relay: RelayStatus | null;
  health: HealthStatus | null;
};

const HISTORY_LEN = 180;
const Ctx = createContext<RT | null>(null);
export const useRealtime = () => {
  const c = useContext(Ctx);
  if (!c) throw new Error('useRealtime must be used within RealtimeProvider');
  return c;
};

const mean = (xs: number[]) => (xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : NaN);

function wsBase(): string {
  const env = (import.meta.env.VITE_WS_URL as string) || '';
  if (env) return env.replace(/\/$/, '');
  const proto = window.location.protocol === 'https:' ? 'wss' : 'ws';
  return `${proto}://${window.location.host}`;
}

function faultSignature(f: Record<string, unknown>): string {
  return JSON.stringify([f.crac_airflow_pct, ((f.recirculation_kgps as number[]) || []).map((x) => x > 0),
    Object.keys((f.drift as object) || {}), Object.keys((f.dropout as object) || {})]);
}
function describeFaults(f: Record<string, unknown>): string {
  const parts: string[] = [];
  const air = f.crac_airflow_pct as number;
  if (air < 100) parts.push(`CRAC airflow ${air}%`);
  ((f.recirculation_kgps as number[]) || []).forEach((x, i) => { if (x > 0) parts.push(`Recirculation CA${i}`); });
  const drift = Object.keys((f.drift as object) || {}); if (drift.length) parts.push(`Drift ${drift.join(',')}`);
  const dropout = Object.keys((f.dropout as object) || {}); if (dropout.length) parts.push(`Dropout ${dropout.join(',')}`);
  return parts.length ? parts.join(' · ') : 'all faults cleared';
}

export function RealtimeProvider({ children }: { children: ReactNode }) {
  const [token, setTokenState] = useState<string>(() => localStorage.getItem('jwt') || '');
  const [connected, setConnected] = useState(false);
  const [simTime, setSimTime] = useState(0);
  const [frame, setFrame] = useState<Frame>({});
  const [history, setHistory] = useState<HistRow[]>([]);
  const [residualsStatic, setResidualsStatic] = useState<Record<string, number>>({});
  const [residualsRls, setResidualsRls] = useState<Record<string, number>>({});
  const [dropped, setDropped] = useState<string[]>([]);
  const [activeFaults, setActiveFaults] = useState<Record<string, unknown>>({});
  const [faultEvents, setFaultEvents] = useState<FaultEvent[]>([]);
  const [cracAirflowPct, setCracAirflowPct] = useState(100);
  const [rls, setRls] = useState<RlsParams | null>(null);
  const [mpc, setMpc] = useState<MpcAdvisory | null>(null);
  const [rackSensors, setRackSensors] = useState<RackTrio[]>([]);
  const [relay, setRelay] = useState<RelayStatus | null>(null);
  const [health, setHealth] = useState<HealthStatus | null>(null);

  const sensor = useRef<SensorMsg | null>(null);
  const truth = useRef<TruthMsg | null>(null);
  const predStatic = useRef<PredMsg | null>(null);
  const predRls = useRef<PredMsg | null>(null);
  const lastFaultSig = useRef<string>('');

  const setToken = useCallback((t: string) => { localStorage.setItem('jwt', t); setTokenState(t); }, []);

  const buildFrame = useCallback((): Frame => {
    const s = sensor.current, t = truth.current, ps = predStatic.current, pr = predRls.current;
    const out: Frame = {};
    const roleIdx = (role: string) => (t ? t.zone_roles.indexOf(role) : -1);
    for (const z of VIEW_ZONES) {
      let meas: number | null = null, truthv: number | null = null;
      const predS = ps && z.romKey in ps.state ? ps.state[z.romKey] : null;
      const predR = pr && z.romKey in pr.state ? pr.state[z.romKey] : null;
      if (s) {
        if (z.sensorZone) { const h = s.zones.find((q) => q.zone_id === z.sensorZone && q.valid); meas = h ? h.temp_c : null; }
        else if (z.sensorRacks) {
          const vals = z.sensorRacks.map((k) => s.zones.find((q) => q.zone_id === `rack-${k}` && q.valid)?.temp_c).filter((v): v is number => v != null);
          meas = vals.length ? mean(vals) : null;
        }
      }
      if (t) {
        if (z.truthRole) { const i = roleIdx(z.truthRole); truthv = i >= 0 ? t.state[i] : null; }
        else if (z.truthRacks) truthv = mean(z.truthRacks.map((k) => t.state[roleIdx(`rack-${k}`)]));
      }
      out[z.key] = { meas, truth: truthv, predStatic: predS, predRls: predR };
    }
    return out;
  }, []);

  useEffect(() => {
    if (!token) { setConnected(false); return; }
    let ws: WebSocket | null = null;
    let retry: ReturnType<typeof setTimeout>;
    let closed = false;

    const connect = () => {
      ws = new WebSocket(`${wsBase()}/ws?token=${encodeURIComponent(token)}`);
      ws.onopen = () => setConnected(true);
      ws.onclose = () => { setConnected(false); if (!closed) retry = setTimeout(connect, 2000); };
      ws.onerror = () => ws?.close();
      ws.onmessage = (ev) => {
        let msg: { subject: string; data: unknown };
        try { msg = JSON.parse(ev.data); } catch { return; }
        const { subject, data } = msg;

        if (subject.startsWith('sensor.thermal')) {
          sensor.current = data as SensorMsg;
          setCracAirflowPct((data as SensorMsg).crac_airflow_pct);
          const rs = (data as SensorMsg & { rack_sensors?: RackTrio[] }).rack_sensors;
          if (rs) setRackSensors(rs);
        } else if (subject.startsWith('rom.mpc')) {
          setMpc(data as MpcAdvisory);
        } else if (subject.startsWith('safety.relay')) {
          setRelay(data as RelayStatus);
        } else if (subject.startsWith('health.coil')) {
          setHealth(data as HealthStatus);
        } else if (subject.startsWith('plant.truth')) {
          const t = data as TruthMsg;
          truth.current = t;
          setActiveFaults(t.active_faults);
          const sig = faultSignature(t.active_faults);
          if (sig !== lastFaultSig.current) {
            lastFaultSig.current = sig;
            setFaultEvents((prev) => [{ ts: t.ts, text: describeFaults(t.active_faults) }, ...prev].slice(0, 12));
          }
        } else if (subject.startsWith('rom.rls') && subject.endsWith('.pred')) {
          const p = data as PredMsg;
          predRls.current = p;
          if (p.params) setRls({ eps: p.params.eps, u_scale: p.params.u_scale, frozen: !!p.frozen, trace_P: p.trace_P ?? 0 });
        } else if (subject.startsWith('rom.rls') && subject.endsWith('.residual')) {
          setResidualsRls((data as ResidualMsg).residuals);
        } else if (subject.startsWith('rom.static') && subject.endsWith('.pred')) {
          predStatic.current = data as PredMsg;
        } else if (subject.startsWith('rom.static') && subject.endsWith('.residual')) {
          // static residual is the cadence driver: assemble frame + history here
          const r = data as ResidualMsg;
          setResidualsStatic(r.residuals);
          setDropped(r.dropped);
          setSimTime(r.ts);
          const fr = buildFrame();
          setFrame(fr);
          const row: HistRow = { ts: Math.round(r.ts) };
          for (const z of VIEW_ZONES) {
            const c = fr[z.key];
            if (c.meas != null) row[`${z.key}_meas`] = +c.meas.toFixed(2);
            if (c.truth != null) row[`${z.key}_truth`] = +c.truth.toFixed(2);
            if (c.predStatic != null) row[`${z.key}_predstatic`] = +c.predStatic.toFixed(2);
            if (c.predRls != null) row[`${z.key}_predrls`] = +c.predRls.toFixed(2);
          }
          setHistory((prev) => [...prev, row].slice(-HISTORY_LEN));
        }
      };
    };
    connect();
    return () => { closed = true; clearTimeout(retry); ws?.close(); };
  }, [token, buildFrame]);

  const value: RT = {
    connected, token, setToken, simTime, frame, history,
    residualsStatic, residualsRls, dropped, activeFaults, faultEvents, cracAirflowPct, rls,
    mpc, rackSensors, relay, health,
  };
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}
