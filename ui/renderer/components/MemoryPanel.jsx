/**
 * F.R.I.D.A.Y. — components/MemoryPanel.jsx
 * Shows what's in the Obsidian vault (long-term memory) without having
 * to leave the app. Fetches once on mount, same pattern as
 * WatchlistPanel — this doesn't push live updates the way the
 * watchlist does, since memory changes happen mid-conversation rather
 * than through a dedicated add/remove action; reopening the window
 * re-fetches.
 */

import React, { useEffect } from "react";

const CATEGORY_LABELS = {
  identity: "IDENTITY",
  preferences: "PREFERENCES",
  relationships: "PEOPLE",
  wishes: "WISHES",
  notes: "NOTES",
};
const CATEGORY_ACCENT = {
  identity: "var(--accent)",
  preferences: "#06b6d4",
  relationships: "#a855f7",
  wishes: "#ec4899",
  notes: "#737373",
};
const CATEGORY_ORDER = ["identity", "preferences", "relationships", "wishes", "notes"];

export default function MemoryPanel({ memory, requestMemory }) {
  useEffect(() => {
    requestMemory();
  }, [requestMemory]);

  if (!memory) {
    return <div style={styles.loading}>Loading…</div>;
  }

  const data = memory.memory || {};
  const vaultPath = memory.vaultPath || "";
  const totalFacts = CATEGORY_ORDER.reduce(
    (n, cat) => n + Object.keys(data[cat] || {}).length, 0
  );

  const handleOpenVault = () => {
    if (window.friday && window.friday.openPath && vaultPath) {
      window.friday.openPath(vaultPath);
    }
  };

  return (
    <div style={styles.wrap}>
      <div style={styles.header}>
        <span style={styles.headerCount}>
          {totalFacts} thing{totalFacts === 1 ? "" : "s"} remembered
        </span>
        <button
          style={{ ...styles.openButton, opacity: vaultPath ? 1 : 0.4 }}
          onClick={handleOpenVault}
          disabled={!vaultPath}
          title={vaultPath || "Vault path unknown"}
        >
          Open in Obsidian ↗
        </button>
      </div>

      {totalFacts === 0 && (
        <div style={styles.hint}>
          Nothing yet — just talk normally. FRIDAY picks up things like
          your name, preferences, and the people in your life as they
          come up in conversation.
        </div>
      )}

      {CATEGORY_ORDER.map((cat) => {
        const items = data[cat] || {};
        const keys = Object.keys(items);
        if (keys.length === 0) return null;
        return (
          <Section key={cat} title={CATEGORY_LABELS[cat]} accent={CATEGORY_ACCENT[cat]} count={keys.length}>
            {keys.map((key) => {
              const entry = items[key] || {};
              return (
                <div key={key} style={styles.row}>
                  <div style={{ ...styles.dot, background: CATEGORY_ACCENT[cat] }} />
                  <div style={styles.rowBody}>
                    <div style={styles.rowMain}>
                      <span style={styles.rowKey}>{titleCase(key)}: </span>
                      {entry.value}
                    </div>
                    {entry.updated && <div style={styles.rowMeta}>updated {entry.updated}</div>}
                  </div>
                </div>
              );
            })}
          </Section>
        );
      })}
    </div>
  );
}

function Section({ title, accent, count, children }) {
  return (
    <div style={styles.section}>
      <div style={styles.sectionHeader}>
        <span style={{ ...styles.sectionTitle, color: accent }}>{title}</span>
        <span style={styles.sectionCount}>{count}</span>
      </div>
      <div style={styles.sectionBody}>{children}</div>
    </div>
  );
}

function titleCase(key) {
  return key.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

const styles = {
  wrap: {
    padding: "10px 12px",
    fontFamily: "Segoe UI, system-ui, sans-serif",
    fontSize: 12,
    color: "#d4d4d4",
    overflowY: "auto",
    height: "100%",
  },
  loading: {
    padding: 16,
    color: "#a3a3a3",
    fontSize: 12,
  },
  header: {
    display: "flex",
    alignItems: "center",
    justifyContent: "space-between",
    marginBottom: 12,
    paddingBottom: 8,
    borderBottom: "1px solid rgba(255,255,255,0.08)",
  },
  headerCount: {
    fontSize: 11,
    color: "#8a8a8a",
    fontFamily: "'JetBrains Mono', monospace",
  },
  openButton: {
    fontSize: 10.5,
    fontFamily: "'JetBrains Mono', monospace",
    color: "var(--accent)",
    background: "rgba(var(--accent-rgb), 0.08)",
    border: "1px solid rgba(var(--accent-rgb), 0.25)",
    borderRadius: 4,
    padding: "4px 8px",
    cursor: "pointer",
  },
  section: {
    marginBottom: 14,
  },
  sectionHeader: {
    display: "flex",
    justifyContent: "space-between",
    alignItems: "center",
    borderBottom: "1px solid rgba(255,255,255,0.08)",
    paddingBottom: 4,
    marginBottom: 6,
  },
  sectionTitle: {
    fontSize: 11,
    fontWeight: 700,
    letterSpacing: "0.06em",
    fontFamily: "'JetBrains Mono', monospace",
  },
  sectionCount: {
    fontSize: 11,
    color: "#737373",
    fontFamily: "'JetBrains Mono', monospace",
  },
  sectionBody: {
    display: "flex",
    flexDirection: "column",
    gap: 6,
  },
  row: {
    display: "flex",
    alignItems: "flex-start",
    gap: 8,
    padding: "4px 2px",
  },
  dot: {
    width: 6,
    height: 6,
    borderRadius: "50%",
    marginTop: 5,
    flexShrink: 0,
  },
  rowBody: {
    flex: 1,
    minWidth: 0,
  },
  rowMain: {
    color: "#e5e5e5",
    lineHeight: 1.4,
    wordBreak: "break-word",
  },
  rowKey: {
    color: "#a3a3a3",
  },
  rowMeta: {
    color: "#8a8a8a",
    fontSize: 10.5,
    marginTop: 1,
  },
  hint: {
    marginTop: 4,
    padding: "8px 10px",
    borderRadius: 6,
    background: "rgba(255,255,255,0.03)",
    color: "#8a8a8a",
    fontSize: 11,
    lineHeight: 1.5,
  },
};
