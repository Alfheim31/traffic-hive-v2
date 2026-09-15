/**
 * Shared colour tokens.
 *
 * Deliberately free of any Skia import. App.tsx needs these at module load,
 * and on web importing Skia before CanvasKit has loaded permanently breaks
 * the Skia API object — so anything imported eagerly must stay Skia-free.
 */

export const COLORS = {
  background: "#12140F",
  road: "#3A3D34",
  roadMajor: "#4A4E42",
  crossing: "#5E6353",
  sidewalk: "#2C2F27",
  signal: "#E8B33C",
  moving: "#1D9E75",
  stopped: "#D85A30",
  text: "#E8E6DC",
  textDim: "#8C9080",
  panel: "#191C16",
  border: "#2A2E25",
  accent: "#1D9E75",
};
