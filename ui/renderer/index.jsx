/**
 * F.R.I.D.A.Y. — renderer/index.jsx
 * React entry point.
 */

// Self-hosted JetBrains Mono (bundled from node_modules, not fetched over
// the network at runtime) — the app needs to work with no internet
// connection, so this can't be a Google Fonts <link> tag. Only the
// weights actually used across the app (400 default, 600, 700, 800).
import "@fontsource/jetbrains-mono/400.css";
import "@fontsource/jetbrains-mono/600.css";
import "@fontsource/jetbrains-mono/700.css";
import "@fontsource/jetbrains-mono/800.css";

import React from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import ErrorBoundary from "./components/ErrorBoundary";

const root = createRoot(document.getElementById("root"));
root.render(
  <ErrorBoundary>
    <App />
  </ErrorBoundary>
);
