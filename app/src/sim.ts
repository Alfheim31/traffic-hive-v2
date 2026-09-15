/**
 * Loading and decoding of Traffic Hive simulation assets.
 *
 * The app never simulates. It fetches a precomputed run from the local
 * server and plays it back, which is what keeps rendering smooth and keeps
 * the displayed metrics identical to the ones in the paper.
 */

export const SERVER_URL = "http://localhost:8000";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface Road {
  id: string;
  /** Flat [x0,y0,x1,y1,...] in metres, origin at network centre. */
  p: number[];
  /** Lane count, used for stroke width. */
  n: number;
  /** Speed limit in m/s. */
  s: number;
  /** Layer index; higher draws on top so flyovers cross correctly. */
  l: number;
  name: string;
}

export interface Crossing {
  p: number[];
  /** True for a sidewalk, false for a marked crossing. */
  w: boolean;
}

export interface TrafficLight {
  id: string;
  x: number;
  y: number;
}

export interface NetworkGeometry {
  version: number;
  bounds: { w: number; h: number };
  scale: number;
  roads: Road[];
  crossings: Crossing[];
  lights: TrafficLight[];
  counts: { roads: number; crossings: number; lights: number };
}

export interface ScenarioMetrics {
  completed_trips: number;
  avg_duration_s: number;
  avg_time_loss_s: number;
  avg_waiting_s: number;
  avg_depart_delay_s: number;
  avg_route_length_m: number;
  total_time_loss_h: number;
  peak_halting: number;
  series: { t: number[]; halting: number[]; running: number[] };
}

export interface Comparison {
  vs: string;
  time_loss_reduction_pct: number;
  waiting_reduction_pct: number;
  duration_reduction_pct: number;
  queue_reduction_pct: number;
}

export interface MetricsPayload {
  params: { vehicles: number; duration_s: number; seed: number; baseline: string };
  labels: Record<string, string>;
  scenarios: Record<string, { frames: number; vehicles: number; frame_dt: number; mb: number }>;
  metrics: Record<string, ScenarioMetrics>;
  comparison: Record<string, Comparison>;
}

/** Decoded trajectory data for one scenario. */
export interface Trajectories {
  nFrames: number;
  nVehicles: number;
  /** Metres per quantisation unit. */
  scale: number;
  /** Seconds between consecutive frames. */
  frameDt: number;
  /** Vehicle type codes: 0 car, 1 jeepney, 2 motorcycle. */
  types: Uint8Array;
  /** Raw frame block; indexed arithmetically rather than parsed up front. */
  view: DataView;
  /** Byte offset of the first frame record. */
  base: number;
}

export const ABSENT = -32768;
const BYTES_PER_RECORD = 6;

// ---------------------------------------------------------------------------
// Binary decoding
// ---------------------------------------------------------------------------

/**
 * Decode a .bin produced by pipeline/pack.py.
 *
 * Only the header and type table are read eagerly. Frame records stay in the
 * buffer and are read on demand, so memory stays flat regardless of run
 * length and there is no upfront parse cost before playback can begin.
 */
export function decodeTrajectories(buffer: ArrayBuffer): Trajectories {
  const view = new DataView(buffer);

  const magic = String.fromCharCode(
    view.getUint8(0), view.getUint8(1), view.getUint8(2), view.getUint8(3),
  );
  if (magic !== "THV1") {
    throw new Error(`Unexpected trajectory format: ${magic}`);
  }

  const nFrames = view.getUint32(4, true);
  const nVehicles = view.getUint32(8, true);
  const scale = view.getFloat32(12, true);
  const frameDt = view.getFloat32(16, true);

  const typesOffset = 20;
  const types = new Uint8Array(buffer, typesOffset, nVehicles);
  const base = typesOffset + nVehicles;

  const expected = base + nFrames * nVehicles * BYTES_PER_RECORD;
  if (buffer.byteLength < expected) {
    throw new Error(
      `Trajectory file is truncated: expected ${expected} bytes, got ${buffer.byteLength}`,
    );
  }

  return { nFrames, nVehicles, scale, frameDt, types, view, base };
}

export interface VehicleState {
  x: number;
  y: number;
  /** 0..1 fraction of the reference max speed. */
  speed: number;
  stopped: boolean;
}

/**
 * Read one vehicle's state in one frame, in metres.
 *
 * Returns null when the vehicle is not in the network at that time, so the
 * renderer can skip it rather than drawing a phantom at the origin.
 */
export function readVehicle(
  traj: Trajectories,
  frame: number,
  vehicle: number,
): VehicleState | null {
  const offset =
    traj.base + (frame * traj.nVehicles + vehicle) * BYTES_PER_RECORD;
  const qx = traj.view.getInt16(offset, true);
  if (qx === ABSENT) return null;
  const qy = traj.view.getInt16(offset + 2, true);
  const speed = traj.view.getUint8(offset + 4);
  const flags = traj.view.getUint8(offset + 5);
  return {
    x: qx * traj.scale,
    y: qy * traj.scale,
    speed: speed / 255,
    stopped: (flags & 1) === 1,
  };
}

// ---------------------------------------------------------------------------
// Server client
// ---------------------------------------------------------------------------

export interface RunParams {
  vehicles: number;
  duration_s: number;
  seed?: number;
  scenarios?: string[];
  baseline?: string;
}

export interface RunStatus {
  key: string;
  state: "queued" | "running" | "done" | "error";
  progress: number;
  message: string;
  error?: string | null;
  cached?: boolean;
}

async function json<T>(response: Response): Promise<T> {
  if (!response.ok) {
    throw new Error(`${response.status} ${response.statusText}`);
  }
  return (await response.json()) as T;
}

export async function checkHealth(server: string): Promise<boolean> {
  try {
    const r = await fetch(`${server}/health`);
    return r.ok;
  } catch {
    return false;
  }
}

export async function startRun(server: string, params: RunParams): Promise<RunStatus> {
  const body: RunParams = {
    seed: 42,
    scenarios: ["ue", "hive"],
    baseline: "ue",
    ...params,
  };
  return json<RunStatus>(
    await fetch(`${server}/run`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  );
}

export async function pollStatus(server: string, key: string): Promise<RunStatus> {
  return json<RunStatus>(await fetch(`${server}/status/${key}`));
}

/**
 * Poll until a run finishes.
 *
 * Cached runs return on the first poll; live runs report progress so the UI
 * can show something meaningful rather than an indeterminate spinner.
 */
export async function waitForRun(
  server: string,
  key: string,
  onProgress: (status: RunStatus) => void,
  intervalMs = 1200,
): Promise<RunStatus> {
  for (;;) {
    const status = await pollStatus(server, key);
    onProgress(status);
    if (status.state === "done") return status;
    if (status.state === "error") {
      throw new Error(status.error ?? "Simulation failed");
    }
    await new Promise((resolve) => setTimeout(resolve, intervalMs));
  }
}

export async function fetchNetwork(server: string, key: string): Promise<NetworkGeometry> {
  return json<NetworkGeometry>(await fetch(`${server}/assets/${key}/net.json`));
}

export async function fetchMetrics(server: string, key: string): Promise<MetricsPayload> {
  return json<MetricsPayload>(await fetch(`${server}/assets/${key}/metrics.json`));
}

export async function fetchTrajectories(
  server: string,
  key: string,
  scenario: string,
): Promise<Trajectories> {
  const response = await fetch(`${server}/assets/${key}/traj_${scenario}.bin`);
  if (!response.ok) {
    throw new Error(`Could not load trajectories for ${scenario}`);
  }
  return decodeTrajectories(await response.arrayBuffer());
}

// ---------------------------------------------------------------------------
// Formatting
// ---------------------------------------------------------------------------

export function formatClock(seconds: number): string {
  const total = Math.max(0, Math.floor(seconds));
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}

export function formatPct(value: number): string {
  const sign = value > 0 ? "+" : "";
  return `${sign}${value.toFixed(1)}%`;
}

// ---------------------------------------------------------------------------
// Frame statistics
// ---------------------------------------------------------------------------

/**
 * Count active and queued vehicles in a frame, for the live readouts.
 *
 * Lives here rather than alongside the renderer because App needs it at
 * module load, and the renderer module cannot be imported until CanvasKit
 * has initialised on web.
 */
export function frameCounts(
  trajectories: Trajectories | null,
  frame: number,
): { active: number; queued: number } {
  if (!trajectories || frame < 0 || frame >= trajectories.nFrames) {
    return { active: 0, queued: 0 };
  }
  let active = 0;
  let queued = 0;
  for (let v = 0; v < trajectories.nVehicles; v += 1) {
    const state = readVehicle(trajectories, frame, v);
    if (!state) continue;
    active += 1;
    if (state.stopped) queued += 1;
  }
  return { active, queued };
}
