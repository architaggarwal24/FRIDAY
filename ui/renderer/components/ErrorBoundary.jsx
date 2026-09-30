/**
 * F.R.I.D.A.Y. — components/ErrorBoundary.jsx
 * Without this, a render-time crash anywhere in the tree leaves #root
 * empty and the window just shows its own dark background — solid black,
 * with zero on-screen indication that anything went wrong at all.
 */

import React from "react";

export default class ErrorBoundary extends React.Component {
  constructor(props) {
    super(props);
    this.state = { error: null };
  }

  static getDerivedStateFromError(error) {
    return { error };
  }

  componentDidCatch(error, info) {
    // Also goes to the main process terminal via main.js's console-message
    // forwarding, so it's visible without opening DevTools.
    console.error("[FRIDAY] Render crash:", error, info?.componentStack);
  }

  render() {
    if (this.state.error) {
      return (
        <div style={styles.wrap}>
          <div style={styles.title}>F.R.I.D.A.Y. UI crashed</div>
          <div style={styles.message}>{String(this.state.error?.message || this.state.error)}</div>
          <pre style={styles.stack}>{this.state.error?.stack}</pre>
          <div style={styles.hint}>Check the terminal window for the full error, or press Ctrl+Shift+I for DevTools.</div>
        </div>
      );
    }
    return this.props.children;
  }
}

const styles = {
  wrap: {
    width: "100%",
    height: "100%",
    background: "#0b0d10",
    color: "#d4d4d4",
    fontFamily: "'JetBrains Mono', monospace",
    fontSize: 13,
    padding: 24,
    overflow: "auto",
  },
  title: {
    color: "#ef4444",
    fontSize: 16,
    fontWeight: 700,
    marginBottom: 10,
  },
  message: {
    color: "var(--accent)",
    marginBottom: 14,
  },
  stack: {
    whiteSpace: "pre-wrap",
    color: "#a3a3a3",
    fontSize: 11,
    lineHeight: 1.5,
  },
  hint: {
    marginTop: 16,
    color: "#06b6d4",
    fontSize: 12,
  },
};
