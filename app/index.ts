/**
 * App entry point.
 *
 * CanvasKit loading is handled inside App itself rather than here, so that
 * the guard works regardless of how Expo resolves the entry module.
 */

import { registerRootComponent } from "expo";

import App from "./App";

registerRootComponent(App);
