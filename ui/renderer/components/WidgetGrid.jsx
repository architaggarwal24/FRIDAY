/**
 * F.R.I.D.A.Y. — components/WidgetGrid.jsx
 * Widget show/hide toggles.
 */

import React, { useState } from "react";
import { motion, AnimatePresence } from "framer-motion";

const WIDGETS = [
  { id: "transcript", label: "COMM LOG" },
  { id: "stats",      label: "SYS MONITOR" },
  { id: "clock",      label: "CLOCK" },
  { id: "activity",   label: "ACTIVITY" },
];

export function WidgetToggleBar({ visible, onToggle }) {
  return (
    <div style={styles.toggleBar}>
      <span style={styles.toggleLabel}>WIDGETS</span>
      <div style={styles.toggleButtons}>
        {WIDGETS.map(w => (
          <button
            key={w.id}
            onClick={() => onToggle(w.id)}
            style={{
              ...styles.toggleBtn,
              background: visible[w.id] ? "rgba(var(--accent-rgb), 0.15)" : "transparent",
              borderColor: visible[w.id] ? "rgba(var(--accent-rgb), 0.4)" : "rgba(255,255,255,0.08)",
              color: visible[w.id] ? "var(--accent)" : "#525252",
            }}
          >
            {w.label}
          </button>
        ))}
      </div>
    </div>
  );
}

export function WidgetPanel({ id, visible, children }) {
  return (
    <AnimatePresence>
      {visible && (
        <motion.div
          key={id}
          initial={{ opacity: 0, scale: 0.97 }}
          animate={{ opacity: 1, scale: 1 }}
          exit={{ opacity: 0, scale: 0.97 }}
          transition={{ duration: 0.2 }}
          style={{ height: "100%" }}
        >
          {children}
        </motion.div>
      )}
    </AnimatePresence>
  );
}

export function useWidgetVisibility() {
  const [visible, setVisible] = useState({
    transcript: true,
    stats: true,
    clock: true,
    activity: true,
  });
  const toggle = (id) => setVisible(prev => ({ ...prev, [id]: !prev[id] }));
  return { visible, toggle };
}

const styles = {
  toggleBar: { display: "flex", alignItems: "center", gap: 12 },
  toggleLabel: {
    fontSize: 9, fontFamily: "'JetBrains Mono', monospace",
    letterSpacing: 3, color: "#525252",
  },
  toggleButtons: { display: "flex", gap: 6 },
  toggleBtn: {
    fontSize: 9, fontFamily: "'JetBrains Mono', monospace",
    letterSpacing: 2, border: "1px solid",
    borderRadius: 4, padding: "3px 8px",
    cursor: "pointer", transition: "all 0.15s ease",
  },
};
