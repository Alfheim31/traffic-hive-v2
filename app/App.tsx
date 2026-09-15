/**
 * Traffic Hive — defense demo screen.
 *
 * Flow: enter a vehicle count and a duration, press Run. The server either
 * returns a cached result immediately or simulates it live. Playback then
 * runs from precomputed trajectories, and the results panel reads metrics
 * that were computed server-side so the figures on screen match the paper.
 */

import React, {
  Suspense,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  View,
  Text,
  TextInput,
  Pressable,
  ScrollView,
  ActivityIndicator,
  StyleSheet,
  LayoutChangeEvent,
  Platform,
} from "react-native";

import {
  Gesture,
  GestureDetector,
  GestureHandlerRootView,
} from "react-native-gesture-handler";

import { COLORS } from "./src/theme";
import {
  SERVER_URL,
  NetworkGeometry,
  MetricsPayload,
  Trajectories,
  startRun,
  waitForRun,
  fetchNetwork,
  fetchMetrics,
  fetchTrajectories,
  checkHealth,
  formatClock,
  formatPct,
  frameCounts,
} from "./src/sim";

/**
 * The renderer is loaded lazily and never imported at module scope.
 *
 * On web, @shopify/react-native-skia builds its API object from
 * global.CanvasKit the first time it is imported. Importing it before
 * LoadSkiaWeb() resolves yields an API bound to an undefined CanvasKit that
 * stays broken afterwards, which is why deferring the render alone was not
 * enough — the import itself has to wait.
 */
const SimulationMap = React.lazy(() => import("./src/SimulationMap"));

type Phase = "idle" | "loading" | "ready" | "error";

const SCENARIOS = ["ue", "hive"] as const;
type ScenarioName = (typeof SCENARIOS)[number];

export default function App() {
  const [vehicles, setVehicles] = useState("1000");
  const [duration, setDuration] = useState("60");

  const [phase, setPhase] = useState<Phase>("idle");
  const [status, setStatus] = useState("");
  const [progress, setProgress] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [serverUp, setServerUp] = useState<boolean | null>(null);

  const [network, setNetwork] = useState<NetworkGeometry | null>(null);
  const [metrics, setMetrics] = useState<MetricsPayload | null>(null);
  const [trajectories, setTrajectories] = useState<Record<string, Trajectories>>({});
  const [scenario, setScenario] = useState<ScenarioName>("hive");

  const [frame, setFrame] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState(1);
  const [finished, setFinished] = useState(false);

  const [canvasSize, setCanvasSize] = useState({ width: 0, height: 0 });

  // Viewport. `zoom` multiplies the fit-to-bounds scale; `offset` pans in
  // screen pixels. Both are plain React state rather than shared values
  // because the map re-renders every frame during playback anyway, so
  // driving them on the UI thread buys nothing.
  const [zoom, setZoom] = useState(1);
  const [offset, setOffset] = useState({ x: 0, y: 0 });
  // True while a map drag is in progress, so the page stops scrolling
  // underneath the gesture on touch devices.
  const [mapActive, setMapActive] = useState(false);
  const panStart = useRef({ x: 0, y: 0 });
  const pinchStart = useRef(1);
  const canvasRef = useRef<any>(null);

  const MIN_ZOOM = 0.5;
  const MAX_ZOOM = 12;
  const clampZoom = (z: number) => Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, z));

  const resetView = useCallback(() => {
    setZoom(1);
    setOffset({ x: 0, y: 0 });
  }, []);

  /**
   * Scroll-wheel zoom on web.
   *
   * Attached to the DOM node directly rather than via an onWheel prop:
   * React Native Web does not forward wheel events, and the listener must be
   * non-passive so preventDefault can stop the page scrolling underneath.
   */
  useEffect(() => {
    if (Platform.OS !== "web") return;
    const node = canvasRef.current as unknown as HTMLElement | null;
    if (!node || typeof node.addEventListener !== "function") return;

    const onWheel = (event: WheelEvent) => {
      event.preventDefault();
      setZoom((z) => clampZoom(z * Math.exp(-event.deltaY * 0.0015)));
    };

    node.addEventListener("wheel", onWheel, { passive: false });
    return () => node.removeEventListener("wheel", onWheel);
  }, [canvasSize.width]);

  const panGesture = useMemo(
    () =>
      Gesture.Pan()
        .runOnJS(true)
        .onBegin(() => {
          setMapActive(true);
        })
        .onStart(() => {
          panStart.current = offset;
        })
        .onChange((e) => {
          setOffset({
            x: panStart.current.x + e.translationX,
            y: panStart.current.y + e.translationY,
          });
        })
        .onFinalize(() => {
          setMapActive(false);
        }),
    [offset],
  );

  const pinchGesture = useMemo(
    () =>
      Gesture.Pinch()
        .runOnJS(true)
        .onStart(() => {
          pinchStart.current = zoom;
        })
        .onChange((e) => {
          setZoom(clampZoom(pinchStart.current * e.scale));
        }),
    [zoom],
  );

  const mapGesture = useMemo(
    () => Gesture.Simultaneous(panGesture, pinchGesture),
    [panGesture, pinchGesture],
  );
  const rafRef = useRef<number | null>(null);
  const lastTickRef = useRef<number>(0);
  const frameAccumulator = useRef(0);

  const active = trajectories[scenario] ?? null;

  /**
   * Skia readiness.
   *
   * On native, Skia is compiled into the binary and usable immediately. On
   * web it is a WebAssembly module that must be fetched before any Skia call
   * — including Skia.Path.Make() during render — so the map is withheld until
   * CanvasKit resolves. Gating here rather than at the entry point means the
   * guard holds however Expo resolves the entry module.
   */
  const [skiaReady, setSkiaReady] = useState(Platform.OS !== "web");
  const [skiaError, setSkiaError] = useState<string | null>(null);

  useEffect(() => {
    if (Platform.OS !== "web") return;
    let cancelled = false;

    /**
     * Load CanvasKit, preferring a locally served binary and falling back to
     * a CDN copy. Expo does not serve the public folder in every setup, so
     * the fallback avoids a hard dependency on that behaviour — at the cost
     * of needing network access on first load.
     */
    const load = async () => {
      const { LoadSkiaWeb } = await import(
        "@shopify/react-native-skia/lib/module/web"
      );
      try {
        await LoadSkiaWeb({ locateFile: (file: string) => `/${file}` });
      } catch {
        await LoadSkiaWeb({
          locateFile: (file: string) =>
            `https://cdn.jsdelivr.net/npm/canvaskit-wasm@0.39.1/bin/full/${file}`,
        });
      }
    };

    load()
      .then(() => {
        if (!cancelled) setSkiaReady(true);
      })
      .catch((e: unknown) => {
        if (cancelled) return;
        setSkiaError(
          e instanceof Error ? e.message : "CanvasKit failed to load",
        );
      });

    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    checkHealth(SERVER_URL).then(setServerUp);
  }, []);

  // -----------------------------------------------------------------------
  // Playback loop
  // -----------------------------------------------------------------------

  /**
   * Advance playback in real time rather than per animation frame.
   *
   * Stepping one trajectory frame per rendered frame would make playback
   * speed depend on the device's refresh rate, so a fast laptop and a phone
   * would show different "minutes". Accumulating elapsed wall time against
   * frame_dt keeps the clock honest on both.
   */
  useEffect(() => {
    if (!playing || !active) return;

    const tick = (now: number) => {
      if (lastTickRef.current === 0) lastTickRef.current = now;
      const elapsed = (now - lastTickRef.current) / 1000;
      lastTickRef.current = now;

      frameAccumulator.current += (elapsed * speed) / active.frameDt;
      const advance = Math.floor(frameAccumulator.current);
      if (advance > 0) {
        frameAccumulator.current -= advance;
        setFrame((current) => {
          const next = current + advance;
          if (next >= active.nFrames - 1) {
            setPlaying(false);
            setFinished(true);
            return active.nFrames - 1;
          }
          return next;
        });
      }
      rafRef.current = requestAnimationFrame(tick);
    };

    rafRef.current = requestAnimationFrame(tick);
    return () => {
      if (rafRef.current !== null) cancelAnimationFrame(rafRef.current);
      lastTickRef.current = 0;
    };
  }, [playing, active, speed]);

  // -----------------------------------------------------------------------
  // Run
  // -----------------------------------------------------------------------

  const run = useCallback(async () => {
    const nVehicles = parseInt(vehicles, 10);
    const nDuration = parseInt(duration, 10);

    if (!Number.isFinite(nVehicles) || nVehicles < 10) {
      setError("Vehicle count must be at least 10.");
      setPhase("error");
      return;
    }
    if (!Number.isFinite(nDuration) || nDuration < 10) {
      setError("Duration must be at least 10 seconds.");
      setPhase("error");
      return;
    }

    setPhase("loading");
    setError(null);
    setPlaying(false);
    setFinished(false);
    setFrame(0);
    setProgress(0);
    setStatus("Contacting server");

    try {
      const started = await startRun(SERVER_URL, {
        vehicles: nVehicles,
        duration_s: nDuration,
        scenarios: [...SCENARIOS],
        baseline: "ue",
      });

      if (started.state !== "done") {
        await waitForRun(SERVER_URL, started.key, (s) => {
          setStatus(s.message || s.state);
          setProgress(s.progress);
        });
      } else {
        setStatus("Loaded from cache");
        setProgress(1);
      }

      setStatus("Loading map");
      const [net, met] = await Promise.all([
        fetchNetwork(SERVER_URL, started.key),
        fetchMetrics(SERVER_URL, started.key),
      ]);

      setStatus("Loading trajectories");
      const loaded: Record<string, Trajectories> = {};
      for (const name of SCENARIOS) {
        loaded[name] = await fetchTrajectories(SERVER_URL, started.key, name);
      }

      setNetwork(net);
      setMetrics(met);
      setTrajectories(loaded);
      setPhase("ready");
      setStatus("");
    } catch (e) {
      setError(
        e instanceof Error
          ? `${e.message}. Is the server running? Start it with: python -m server.app --serve`
          : "Unknown error",
      );
      setPhase("error");
    }
  }, [vehicles, duration]);

  // -----------------------------------------------------------------------
  // Derived display values
  // -----------------------------------------------------------------------

  const clock = useMemo(() => {
    if (!active) return { now: 0, total: 0 };
    return {
      now: frame * active.frameDt,
      total: (active.nFrames - 1) * active.frameDt,
    };
  }, [active, frame]);

  const counts = useMemo(() => frameCounts(active, frame), [active, frame]);

  const onCanvasLayout = useCallback((event: LayoutChangeEvent) => {
    const { width } = event.nativeEvent.layout;
    setCanvasSize({ width, height: Math.max(360, Math.min(760, width * 0.78)) });
  }, []);

  const togglePlay = useCallback(() => {
    if (!active) return;
    if (finished) {
      setFrame(0);
      setFinished(false);
      setPlaying(true);
      return;
    }
    setPlaying((p) => !p);
  }, [active, finished]);

  const restart = useCallback(() => {
    setFrame(0);
    setFinished(false);
    setPlaying(false);
  }, []);

  const hiveComparison = metrics?.comparison?.hive;
  const showResults = finished && metrics;

  return (
    <GestureHandlerRootView style={{ flex: 1 }}>
    <ScrollView
      style={styles.screen}
      contentContainerStyle={styles.content}
      scrollEnabled={!mapActive}
    >
      <Text style={styles.title}>Traffic Hive</Text>
      <Text style={styles.subtitle}>
        Cooperative routing on the España corridor
      </Text>

      {serverUp === false && (
        <View style={styles.warning}>
          <Text style={styles.warningText}>
            Server unreachable at {SERVER_URL}. Start it with{" "}
            <Text style={styles.mono}>python -m server.app --serve</Text>
          </Text>
        </View>
      )}

      {/* ---------------- Inputs ---------------- */}
      <View style={styles.inputRow}>
        <View style={styles.field}>
          <Text style={styles.label}>Vehicles</Text>
          <TextInput
            style={styles.input}
            value={vehicles}
            onChangeText={setVehicles}
            keyboardType="number-pad"
            placeholder="1000"
            placeholderTextColor={COLORS.textDim}
          />
        </View>
        <View style={styles.field}>
          <Text style={styles.label}>Duration (s)</Text>
          <TextInput
            style={styles.input}
            value={duration}
            onChangeText={setDuration}
            keyboardType="number-pad"
            placeholder="60"
            placeholderTextColor={COLORS.textDim}
          />
        </View>
        <Pressable
          style={[styles.runButton, phase === "loading" && styles.runButtonBusy]}
          onPress={run}
          disabled={phase === "loading"}
        >
          {phase === "loading" ? (
            <ActivityIndicator color="#0C0E0A" />
          ) : (
            <Text style={styles.runButtonText}>Run</Text>
          )}
        </Pressable>
      </View>

      {phase === "loading" && (
        <View style={styles.statusBox}>
          <Text style={styles.statusText}>{status}</Text>
          <View style={styles.progressTrack}>
            <View style={[styles.progressFill, { width: `${progress * 100}%` }]} />
          </View>
        </View>
      )}

      {phase === "error" && error && (
        <View style={styles.errorBox}>
          <Text style={styles.errorText}>{error}</Text>
        </View>
      )}

      {/* ---------------- Map ---------------- */}
      {phase === "ready" && network && (
        <View style={styles.mapCard}>
          <View style={styles.controls}>
            <Pressable style={styles.ctl} onPress={togglePlay}>
              <Text style={styles.ctlIcon}>
                {finished ? "\u21BA" : playing ? "\u2016" : "\u25B6"}
              </Text>
            </Pressable>
            <Pressable style={styles.ctl} onPress={restart}>
              <Text style={styles.ctlIcon}>{"\u23EE"}</Text>
            </Pressable>

            <Text style={styles.clock}>
              {formatClock(clock.now)} / {formatClock(clock.total)}
            </Text>

            <View style={styles.seekTrack}>
              <View
                style={[
                  styles.seekFill,
                  {
                    width: `${
                      clock.total > 0 ? (clock.now / clock.total) * 100 : 0
                    }%`,
                  },
                ]}
              />
            </View>

            <Pressable
              style={styles.pill}
              onPress={() => setSpeed((s) => (s >= 8 ? 1 : s * 2))}
            >
              <Text style={styles.pillText}>{speed}&times;</Text>
            </Pressable>
          </View>

          <View style={styles.scenarioRow}>
            <View style={styles.zoomGroup}>
              <Pressable
                style={styles.zoomBtn}
                onPress={() => setZoom((z) => clampZoom(z / 1.4))}
              >
                <Text style={styles.zoomIcon}>{"\u2212"}</Text>
              </Pressable>
              <Pressable style={styles.zoomBtn} onPress={resetView}>
                <Text style={styles.zoomLabel}>{zoom.toFixed(1)}&times;</Text>
              </Pressable>
              <Pressable
                style={styles.zoomBtn}
                onPress={() => setZoom((z) => clampZoom(z * 1.4))}
              >
                <Text style={styles.zoomIcon}>+</Text>
              </Pressable>
            </View>
            {SCENARIOS.map((name) => (
              <Pressable
                key={name}
                style={[
                  styles.scenarioTab,
                  scenario === name && styles.scenarioTabActive,
                ]}
                onPress={() => setScenario(name)}
              >
                <Text
                  style={[
                    styles.scenarioText,
                    scenario === name && styles.scenarioTextActive,
                  ]}
                >
                  {metrics?.labels?.[name] ?? name}
                </Text>
              </Pressable>
            ))}
          </View>

          <View
            ref={canvasRef}
            onLayout={onCanvasLayout}
            style={styles.canvasWrap}
          >
            {!skiaReady && (
              <View style={[styles.skiaWait, { height: canvasSize.height || 260 }]}>
                {skiaError ? (
                  <>
                    <Text style={styles.errorText}>Skia failed to load</Text>
                    <Text style={styles.statusText}>{skiaError}</Text>
                    <Text style={styles.statusText}>
                      Run: mkdir -p public && cp
                      node_modules/canvaskit-wasm/bin/full/canvaskit.wasm public/
                    </Text>
                  </>
                ) : (
                  <>
                    <ActivityIndicator color={COLORS.accent} />
                    <Text style={styles.statusText}>Loading renderer</Text>
                  </>
                )}
              </View>
            )}
            {skiaReady && canvasSize.width > 0 && (
              <Suspense
                fallback={
                  <View style={[styles.skiaWait, { height: canvasSize.height }]}>
                    <ActivityIndicator color={COLORS.accent} />
                  </View>
                }
              >
              <GestureDetector gesture={mapGesture}>
                <View>
                  <SimulationMap
                    network={network}
                    trajectories={active}
                    frame={frame}
                    width={canvasSize.width}
                    height={canvasSize.height}
                    zoom={zoom}
                    offset={offset}
                  />
                </View>
              </GestureDetector>
              </Suspense>
            )}
          </View>

          <View style={styles.legend}>
            <LegendDot color={COLORS.moving} label={`moving ${counts.active - counts.queued}`} />
            <LegendDot color={COLORS.stopped} label={`queued ${counts.queued}`} />
            <LegendDot color={COLORS.signal} label={`${network.counts.lights} signals`} />
            <Text style={styles.legendNote}>
              Replay — SUMO run, seed {metrics?.params?.seed ?? 42}
            </Text>
          </View>
        </View>
      )}

      {/* ---------------- Results ---------------- */}
      {showResults && hiveComparison && (
        <View style={styles.results}>
          <Text style={styles.resultsTitle}>
            Run complete — {metrics.params.vehicles} vehicles,{" "}
            {metrics.params.duration_s}s
          </Text>
          <Text style={styles.resultsSub}>
            Traffic Hive compared against {metrics.labels?.ue ?? "user equilibrium"}
          </Text>

          <View style={styles.cards}>
            <MetricCard
              label="Delay reduced"
              value={formatPct(hiveComparison.time_loss_reduction_pct)}
              good={hiveComparison.time_loss_reduction_pct > 0}
            />
            <MetricCard
              label="Waiting reduced"
              value={formatPct(hiveComparison.waiting_reduction_pct)}
              good={hiveComparison.waiting_reduction_pct > 0}
            />
            <MetricCard
              label="Peak queue reduced"
              value={formatPct(hiveComparison.queue_reduction_pct)}
              good={hiveComparison.queue_reduction_pct > 0}
            />
            <MetricCard
              label="Trip time reduced"
              value={formatPct(hiveComparison.duration_reduction_pct)}
              good={hiveComparison.duration_reduction_pct > 0}
            />
          </View>

          <Text style={styles.chartTitle}>Average time loss per vehicle (s)</Text>
          <DelayChart metrics={metrics} />

          <Text style={styles.chartTitle}>Vehicles queued over time</Text>
          <QueueChart metrics={metrics} />
        </View>
      )}
    </ScrollView>
    </GestureHandlerRootView>
  );
}

// ---------------------------------------------------------------------------
// Small presentational pieces
// ---------------------------------------------------------------------------

function LegendDot({ color, label }: { color: string; label: string }) {
  return (
    <View style={styles.legendItem}>
      <View style={[styles.legendSwatch, { backgroundColor: color }]} />
      <Text style={styles.legendText}>{label}</Text>
    </View>
  );
}

function MetricCard({
  label,
  value,
  good,
}: {
  label: string;
  value: string;
  good: boolean;
}) {
  return (
    <View style={styles.card}>
      <Text style={styles.cardLabel}>{label}</Text>
      <Text style={[styles.cardValue, { color: good ? COLORS.moving : COLORS.stopped }]}>
        {value}
      </Text>
    </View>
  );
}

/** Horizontal bars of mean time loss, one per scenario. */
function DelayChart({ metrics }: { metrics: MetricsPayload }) {
  const entries = Object.entries(metrics.metrics);
  const max = Math.max(...entries.map(([, m]) => m.avg_time_loss_s), 1);
  return (
    <View style={styles.chart}>
      {entries.map(([name, m]) => (
        <View key={name} style={styles.barRow}>
          <Text style={styles.barLabel} numberOfLines={1}>
            {metrics.labels?.[name] ?? name}
          </Text>
          <View style={styles.barTrack}>
            <View
              style={[
                styles.barFill,
                {
                  width: `${(m.avg_time_loss_s / max) * 100}%`,
                  backgroundColor: name === "hive" ? COLORS.moving : COLORS.textDim,
                },
              ]}
            />
          </View>
          <Text style={styles.barValue}>{m.avg_time_loss_s.toFixed(0)}</Text>
        </View>
      ))}
    </View>
  );
}

/**
 * Queue time series as a sparkline built from Views.
 *
 * Deliberately not Skia — this is a static chart drawn once after playback
 * ends, so a second canvas context would cost more than it saves.
 */
function QueueChart({ metrics }: { metrics: MetricsPayload }) {
  const entries = Object.entries(metrics.metrics);
  const max = Math.max(
    ...entries.flatMap(([, m]) => m.series.halting),
    1,
  );
  return (
    <View style={styles.sparkWrap}>
      {entries.map(([name, m]) => (
        <View key={name} style={styles.sparkRow}>
          <Text style={styles.barLabel} numberOfLines={1}>
            {metrics.labels?.[name] ?? name}
          </Text>
          <View style={styles.spark}>
            {m.series.halting.map((v, i) => (
              <View
                key={i}
                style={{
                  flex: 1,
                  height: `${(v / max) * 100}%`,
                  marginHorizontal: 0.5,
                  backgroundColor: name === "hive" ? COLORS.moving : COLORS.textDim,
                  opacity: 0.85,
                }}
              />
            ))}
          </View>
          <Text style={styles.barValue}>{m.peak_halting}</Text>
        </View>
      ))}
    </View>
  );
}

// ---------------------------------------------------------------------------
// Styles
// ---------------------------------------------------------------------------

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: COLORS.background },
  content: { padding: 16, paddingBottom: 48, maxWidth: 900, alignSelf: "center", width: "100%" },

  title: { color: COLORS.text, fontSize: 26, fontWeight: "600" },
  subtitle: { color: COLORS.textDim, fontSize: 14, marginTop: 2, marginBottom: 18 },

  warning: {
    backgroundColor: "#2B2410",
    borderRadius: 10,
    padding: 12,
    marginBottom: 14,
  },
  warningText: { color: "#E8B33C", fontSize: 13, lineHeight: 19 },
  mono: { fontFamily: "Menlo", fontSize: 12 },

  inputRow: { flexDirection: "row", gap: 10, alignItems: "flex-end" },
  field: { flex: 1 },
  label: { color: COLORS.textDim, fontSize: 12, marginBottom: 6 },
  input: {
    backgroundColor: COLORS.panel,
    borderWidth: StyleSheet.hairlineWidth,
    borderColor: COLORS.border,
    borderRadius: 8,
    paddingHorizontal: 12,
    paddingVertical: 10,
    color: COLORS.text,
    fontSize: 16,
  },
  runButton: {
    backgroundColor: COLORS.accent,
    borderRadius: 8,
    paddingHorizontal: 26,
    paddingVertical: 12,
    minWidth: 92,
    alignItems: "center",
  },
  runButtonBusy: { opacity: 0.7 },
  runButtonText: { color: "#0C0E0A", fontSize: 16, fontWeight: "600" },

  statusBox: { marginTop: 16 },
  statusText: { color: COLORS.textDim, fontSize: 13, marginBottom: 8 },
  progressTrack: {
    height: 4,
    backgroundColor: COLORS.panel,
    borderRadius: 2,
    overflow: "hidden",
  },
  progressFill: { height: 4, backgroundColor: COLORS.accent },

  errorBox: {
    marginTop: 16,
    backgroundColor: "#2B1512",
    borderRadius: 10,
    padding: 12,
  },
  errorText: { color: "#E88A70", fontSize: 13, lineHeight: 19 },

  mapCard: {
    marginTop: 18,
    backgroundColor: COLORS.panel,
    borderRadius: 12,
    borderWidth: StyleSheet.hairlineWidth,
    borderColor: COLORS.border,
    overflow: "hidden",
  },
  controls: {
    flexDirection: "row",
    alignItems: "center",
    gap: 10,
    padding: 10,
    borderBottomWidth: StyleSheet.hairlineWidth,
    borderBottomColor: COLORS.border,
  },
  ctl: {
    width: 34,
    height: 34,
    borderRadius: 8,
    backgroundColor: COLORS.background,
    alignItems: "center",
    justifyContent: "center",
  },
  ctlIcon: { color: COLORS.text, fontSize: 15 },
  clock: { color: COLORS.text, fontFamily: "Menlo", fontSize: 13, minWidth: 96 },
  seekTrack: {
    flex: 1,
    height: 4,
    backgroundColor: COLORS.background,
    borderRadius: 2,
    overflow: "hidden",
  },
  seekFill: { height: 4, backgroundColor: COLORS.textDim },
  pill: {
    paddingHorizontal: 10,
    paddingVertical: 5,
    borderRadius: 8,
    backgroundColor: COLORS.background,
  },
  pillText: { color: COLORS.textDim, fontSize: 12 },

  scenarioRow: {
    flexDirection: "row",
    gap: 8,
    paddingHorizontal: 10,
    paddingVertical: 8,
  },
  scenarioTab: {
    paddingHorizontal: 12,
    paddingVertical: 6,
    borderRadius: 999,
    backgroundColor: COLORS.background,
  },
  scenarioTabActive: { backgroundColor: COLORS.accent },
  scenarioText: { color: COLORS.textDim, fontSize: 12 },
  scenarioTextActive: { color: "#0C0E0A", fontWeight: "600" },

  canvasWrap: { width: "100%", overflow: "hidden" },
  zoomGroup: {
    flexDirection: "row",
    alignItems: "center",
    gap: 4,
    marginRight: 6,
  },
  zoomBtn: {
    paddingHorizontal: 9,
    paddingVertical: 5,
    borderRadius: 8,
    backgroundColor: COLORS.background,
    minWidth: 30,
    alignItems: "center",
  },
  zoomIcon: { color: COLORS.text, fontSize: 14 },
  zoomLabel: { color: COLORS.textDim, fontSize: 12 },
  skiaWait: {
    width: "100%",
    alignItems: "center",
    justifyContent: "center",
    gap: 8,
    padding: 20,
    backgroundColor: COLORS.background,
  },

  legend: {
    flexDirection: "row",
    alignItems: "center",
    gap: 14,
    flexWrap: "wrap",
    padding: 10,
    borderTopWidth: StyleSheet.hairlineWidth,
    borderTopColor: COLORS.border,
  },
  legendItem: { flexDirection: "row", alignItems: "center", gap: 6 },
  legendSwatch: { width: 8, height: 8, borderRadius: 4 },
  legendText: { color: COLORS.textDim, fontSize: 12 },
  legendNote: { color: COLORS.textDim, fontSize: 11, marginLeft: "auto" },

  results: { marginTop: 22 },
  resultsTitle: { color: COLORS.text, fontSize: 17, fontWeight: "600" },
  resultsSub: { color: COLORS.textDim, fontSize: 13, marginTop: 2, marginBottom: 14 },

  cards: { flexDirection: "row", flexWrap: "wrap", gap: 10 },
  card: {
    flexGrow: 1,
    minWidth: 150,
    backgroundColor: COLORS.panel,
    borderRadius: 10,
    padding: 14,
  },
  cardLabel: { color: COLORS.textDim, fontSize: 12, marginBottom: 4 },
  cardValue: { fontSize: 22, fontWeight: "500" },

  chartTitle: { color: COLORS.textDim, fontSize: 13, marginTop: 20, marginBottom: 10 },
  chart: {
    backgroundColor: COLORS.panel,
    borderRadius: 10,
    padding: 14,
    gap: 10,
  },
  barRow: { flexDirection: "row", alignItems: "center", gap: 10 },
  barLabel: { color: COLORS.textDim, fontSize: 12, width: 108 },
  barTrack: { flex: 1, height: 18, backgroundColor: COLORS.background, borderRadius: 4 },
  barFill: { height: 18, borderRadius: 4 },
  barValue: { color: COLORS.textDim, fontFamily: "Menlo", fontSize: 12, width: 44, textAlign: "right" },

  sparkWrap: { backgroundColor: COLORS.panel, borderRadius: 10, padding: 14, gap: 12 },
  sparkRow: { flexDirection: "row", alignItems: "flex-end", gap: 10 },
  spark: {
    flex: 1,
    height: 46,
    flexDirection: "row",
    alignItems: "flex-end",
  },
});
