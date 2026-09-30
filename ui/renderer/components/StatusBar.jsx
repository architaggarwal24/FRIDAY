/**
 * F.R.I.D.A.Y. — components/StatusBar.jsx
 * State badge, intent display, and connection status.
 */

import React from "react";
import { motion, AnimatePresence } from "framer-motion";
import { STATE_COLORS, STATE_LABELS } from "../hooks/useFriday";

const INTENT_ICONS = {
  chat:       "💬",
  system:     "⚙️",
  volume:     "🔊",
  power:      "⚡",
  screenshot: "📸",
  clipboard:  "📋",
  open_app:   "🚀",
  file:       "📁",
  window:     "🗗",
  search:     "🔍",
  weather:    "🌤",
  news:       "📰",
  spotify:    "🎵",
  timer:      "⏱",
  memory:     "🧠",
  stop:       "✋",
  clear:      "🗑",
};

export function StateBadge({ state }) {
  const color = STATE_COLORS[state] || STATE_COLORS.idle;
  const label = STATE_LABELS[state] || "STANDBY";

  return (
    <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
      {/* Pulsing dot */}
      <motion.div
        animate={{
          scale: state === "idle" ? [1, 1.3, 1] : [1, 1.6, 1],
          opacity: state === "idle" ? [0.6, 1, 0.6] : [0.8, 1, 0.8],
        }}
        transition={{ repeat: Infinity, duration: state === "idle" ? 3 : 0.8 }}
        style={{
          width: 8,
          height: 8,
          borderRadius: "50%",
          background: color,
          boxShadow: `0 0 8px ${color}`,
        }}
      />
      <motion.span
        key={state}
        initial={{ opacity: 0, y: -4 }}
        animate={{ opacity: 1, y: 0 }}
        style={{
          fontSize: 11,
          fontFamily: "'JetBrains Mono', monospace",
          letterSpacing: 3,
          color,
          fontWeight: 700,
        }}
      >
        {label}
      </motion.span>
    </div>
  );
}

export function IntentBadge({ intent }) {
  if (!intent) return null;
  const icon = INTENT_ICONS[intent] || "◆";

  return (
    <AnimatePresence mode="wait">
      <motion.div
        key={intent}
        initial={{ opacity: 0, scale: 0.8 }}
        animate={{ opacity: 1, scale: 1 }}
        exit={{ opacity: 0, scale: 0.8 }}
        transition={{ duration: 0.15 }}
        style={styles.intentBadge}
      >
        <span style={{ fontSize: 12 }}>{icon}</span>
        <span style={styles.intentText}>{intent.replace("_", " ").toUpperCase()}</span>
      </motion.div>
    </AnimatePresence>
  );
}

export function ConnectionDot({ connected }) {
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 5 }}>
      <motion.div
        animate={connected ? { opacity: [0.7, 1, 0.7] } : { opacity: 0.3 }}
        transition={{ repeat: Infinity, duration: 2 }}
        style={{
          width: 6, height: 6, borderRadius: "50%",
          background: connected ? "#10b981" : "#ef4444",
        }}
      />
      <span style={{
        fontSize: 9,
        fontFamily: "'JetBrains Mono', monospace",
        color: connected ? "#10b981" : "#ef4444",
        letterSpacing: 1,
      }}>
        {connected ? "LINKED" : "OFFLINE"}
      </span>
    </div>
  );
}

const styles = {
  intentBadge: {
    display: "flex",
    alignItems: "center",
    gap: 5,
    background: "rgba(var(--accent-rgb), 0.08)",
    border: "1px solid rgba(var(--accent-rgb), 0.2)",
    borderRadius: 20,
    padding: "3px 10px",
  },
  intentText: {
    fontSize: 9,
    fontFamily: "'JetBrains Mono', monospace",
    letterSpacing: 2,
    color: "var(--accent)",
  },
};
