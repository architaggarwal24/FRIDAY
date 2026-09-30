/**
 * F.R.I.D.A.Y. — components/Taskbar.jsx
 * Dock at the bottom — click an app to open it, click again to close.
 *
 * Minimal borderless tab style: plain text labels distinguished only by
 * color (dim when closed, copper when open), no button boxes, no
 * borders, no icons. Deliberately quiet compared to the old bordered-
 * pill buttons — the taskbar shouldn't compete with the windows above it.
 */
import React from "react";

const APPS = [
  { id: "settings",   label: "settings" },
  { id: "galaxy",     label: "galaxy" },
];

export default function Taskbar({ windows, onToggle, settingsOpen }) {
  return (
    <div style={styles.bar}>
      <span style={styles.brand}>◆</span>
      <div style={styles.apps}>
        {APPS.map(app => {
          const open = app.id === "settings" ? !!settingsOpen : !!windows[app.id];
          return (
            <button
              key={app.id}
              onClick={() => onToggle(app.id)}
              style={{
                ...styles.appBtn,
                color: open ? "var(--accent)" : "#5a5a5a",
              }}
            >
              {app.label}
              <span style={{ ...styles.dot, opacity: open ? 1 : 0 }} />
            </button>
          );
        })}
      </div>
    </div>
  );
}

const styles = {
  bar: {
    height: 42, flexShrink: 0, display: "flex", alignItems: "center",
    justifyContent: "center", gap: 22, padding: "0 16px", position: "relative",
    borderTop: "1px solid var(--panel-border)",
    background: "var(--taskbar-bg)",
    backdropFilter: "blur(var(--panel-blur)) saturate(var(--panel-saturate))",
    WebkitBackdropFilter: "blur(var(--panel-blur)) saturate(var(--panel-saturate))",
    zIndex: 20,
  },
  brand: {
    position: "absolute", left: 16,
    color: "var(--accent)", opacity: 0.3, fontSize: 11,
  },
  apps: { display: "flex", gap: 22 },
  appBtn: {
    position: "relative", background: "transparent", border: "none", padding: "0 0 8px 0",
    fontSize: 9, fontFamily: "'JetBrains Mono', monospace", letterSpacing: 1.5,
    cursor: "pointer", transition: "color 0.15s ease",
  },
  dot: {
    position: "absolute", left: "50%", bottom: 0, width: 3, height: 3,
    borderRadius: "50%", background: "var(--accent)", transform: "translateX(-50%)",
    boxShadow: "0 0 6px var(--accent)", transition: "opacity 0.15s ease",
  },
};
