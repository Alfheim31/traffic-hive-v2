/**
 * Skia map renderer.
 *
 * Roads, crossings and signal markers are static, so their paths are built
 * once and reused. Vehicles change every frame, so they are batched into two
 * paths per frame — moving and stopped — rather than mounted as a thousand
 * separate components. Two draw calls per frame instead of a thousand element
 * diffs is the difference between smooth playback and a slideshow.
 */

import React, { useMemo } from "react";
import { View, StyleSheet } from "react-native";
import { Canvas, Path, Skia, Group, Circle, Rect } from "@shopify/react-native-skia";
import type { SkPath } from "@shopify/react-native-skia";

import { NetworkGeometry, Trajectories, readVehicle } from "./sim";
import { COLORS } from "./theme";


interface MapProps {
  network: NetworkGeometry;
  trajectories: Trajectories | null;
  frame: number;
  width: number;
  height: number;
  /** User zoom multiplier applied on top of the fit-to-bounds scale. */
  zoom?: number;
  /** User pan offset in screen pixels. */
  offset?: { x: number; y: number };
  showSignals?: boolean;
  showCrossings?: boolean;
}

/** Build a Skia path from a flat coordinate list. */
function polyline(points: number[]): SkPath {
  const path = Skia.Path.Make();
  if (points.length >= 4) {
    path.moveTo(points[0], points[1]);
    for (let i = 2; i < points.length; i += 2) {
      path.lineTo(points[i], points[i + 1]);
    }
  }
  return path;
}

/** Merge many polylines into one path so they render in a single draw call. */
function mergePolylines(shapes: number[][]): SkPath {
  const path = Skia.Path.Make();
  for (const points of shapes) {
    if (points.length < 4) continue;
    path.moveTo(points[0], points[1]);
    for (let i = 2; i < points.length; i += 2) {
      path.lineTo(points[i], points[i + 1]);
    }
  }
  return path;
}

export default function SimulationMap({
  network,
  trajectories,
  frame,
  width,
  height,
  zoom = 1,
  offset = { x: 0, y: 0 },
  showSignals = true,
  showCrossings = true,
}: MapProps) {
  /**
   * Camera transform.
   *
   * Network coordinates are metres with the origin at the network centre and
   * y increasing upward; screen coordinates put y downward, hence the flip.
   * A uniform scale preserves the corridor's real proportions — stretching to
   * fill would misrepresent distances a panellist may recognise.
   */
  const camera = useMemo(() => {
    const margin = 0.96;
    const sx = width / Math.max(1, network.bounds.w);
    const sy = height / Math.max(1, network.bounds.h);
    const fit = Math.min(sx, sy) * margin;
    // User zoom multiplies the fit scale; pan is applied in screen pixels
    // after scaling, so dragging moves the map by the distance the finger
    // or cursor actually travelled regardless of zoom level.
    return {
      scale: fit * zoom,
      tx: width / 2 + offset.x,
      ty: height / 2 + offset.y,
      fit,
    };
  }, [width, height, network.bounds.w, network.bounds.h, zoom, offset.x, offset.y]);

  /** Roads grouped by lane count so each width is one draw call. */
  const roadLayers = useMemo(() => {
    const minor: number[][] = [];
    const major: number[][] = [];
    const sorted = [...network.roads].sort((a, b) => a.l - b.l);
    for (const road of sorted) {
      (road.n >= 3 ? major : minor).push(road.p);
    }
    return {
      minor: mergePolylines(minor),
      major: mergePolylines(major),
    };
  }, [network.roads]);

  const crossingPaths = useMemo(() => {
    const marked: number[][] = [];
    const walk: number[][] = [];
    for (const c of network.crossings) {
      (c.w ? walk : marked).push(c.p);
    }
    return { marked: mergePolylines(marked), walk: mergePolylines(walk) };
  }, [network.crossings]);

  /**
   * Vehicle batching.
   *
   * Rebuilt every frame. Circle radius is in world units so vehicles keep a
   * constant on-screen size regardless of how the camera scaled the network.
   */
  const vehiclePaths = useMemo(() => {
    const moving = Skia.Path.Make();
    const stopped = Skia.Path.Make();
    if (!trajectories || frame < 0 || frame >= trajectories.nFrames) {
      return { moving, stopped, active: 0, queued: 0 };
    }
    const radius = 3.2 / camera.scale;
    let active = 0;
    let queued = 0;
    for (let v = 0; v < trajectories.nVehicles; v += 1) {
      const state = readVehicle(trajectories, frame, v);
      if (!state) continue;
      active += 1;
      if (state.stopped) {
        queued += 1;
        stopped.addCircle(state.x, state.y, radius);
      } else {
        moving.addCircle(state.x, state.y, radius);
      }
    }
    return { moving, stopped, active, queued };
  }, [trajectories, frame, camera.scale]);

  const transform = useMemo(
    () => [
      { translateX: camera.tx },
      { translateY: camera.ty },
      { scale: camera.scale },
      { scaleY: -1 },
    ],
    [camera],
  );

  const signalRadius = 4.5 / camera.scale;

  return (
    <View style={{ width, height, backgroundColor: COLORS.background }}>
      <Canvas style={{ width, height }}>
        <Group transform={transform}>
          {showCrossings && (
            <Path
              path={crossingPaths.walk}
              style="stroke"
              strokeWidth={2.0 / camera.scale}
              color={COLORS.sidewalk}
            />
          )}

          <Path
            path={roadLayers.minor}
            style="stroke"
            strokeWidth={5.0 / camera.scale}
            strokeCap="round"
            strokeJoin="round"
            color={COLORS.road}
          />
          <Path
            path={roadLayers.major}
            style="stroke"
            strokeWidth={8.0 / camera.scale}
            strokeCap="round"
            strokeJoin="round"
            color={COLORS.roadMajor}
          />

          {showCrossings && (
            <Path
              path={crossingPaths.marked}
              style="stroke"
              strokeWidth={2.4 / camera.scale}
              color={COLORS.crossing}
            />
          )}

          {showSignals &&
            network.lights.map((light) => (
              <Circle
                key={light.id}
                cx={light.x}
                cy={light.y}
                r={signalRadius}
                color={COLORS.signal}
                opacity={0.85}
              />
            ))}

          <Path path={vehiclePaths.moving} color={COLORS.moving} />
          <Path path={vehiclePaths.stopped} color={COLORS.stopped} />
        </Group>
      </Canvas>
    </View>
  );
}

export const mapStyles = StyleSheet.create({
  container: {
    borderRadius: 12,
    overflow: "hidden",
    borderWidth: StyleSheet.hairlineWidth,
    borderColor: COLORS.border,
  },
});
