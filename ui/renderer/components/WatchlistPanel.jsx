/**
 * F.R.I.D.A.Y. — components/WatchlistPanel.jsx
 * Shows active topic monitors and pending reminders. Fetches once on
 * mount (like Settings does with get_settings), then updates live —
 * the backend pushes a fresh watchlist_data event after every add/remove,
 * so this never needs the user to ask or hit refresh to see a change.
 */

import React, { useEffect } from "react";

export default function WatchlistPanel({ watchlist, requestWatchlist }) {
  useEffect(() => {
    requestWatchlist();
  }, [requestWatchlist]);

  if (!watchlist) {
    return <div style={styles.loading}>Loading…</div>;
  }

  const { monitors, reminders, openLoops = [] } = watchlist;
  const empty = monitors.length === 0 && reminders.length === 0 && openLoops.length === 0;

  return (
    <div style={styles.wrap}>
      <Section title="REMINDERS" accent="var(--accent)" count={reminders.length}>
        {reminders.length === 0 ? (
          <div style={styles.emptyRow}>No reminders pending</div>
        ) : (
          reminders.map((r) => (
            <div key={r.task_name} style={styles.row}>
              <div style={{ ...styles.dot, background: "var(--accent)" }} />
              <div style={styles.rowBody}>
                <div style={styles.rowMain}>{r.message}</div>
                <div style={styles.rowMeta}>{formatWhen(r.date, r.time)}</div>
              </div>
            </div>
          ))
        )}
      </Section>

      <Section title="OPEN LOOPS" accent="#a78bfa" count={openLoops.length}>
        {openLoops.length === 0 ? (
          <div style={styles.emptyRow}>Nothing open</div>
        ) : (
          openLoops.map((l) => (
            <div key={l.id} style={styles.row}>
              <div style={{ ...styles.dot, background: "#a78bfa" }} />
              <div style={styles.rowBody}>
                <div style={styles.rowMain}>{l.text}</div>
                <div style={styles.rowMeta}>
                  open since {(l.created || "").slice(0, 10)}
                  {l.last_resurfaced ? ` · last brought up ${l.last_resurfaced.slice(0, 10)}` : ""}
                </div>
              </div>
            </div>
          ))
        )}
      </Section>

      <Section title="TOPIC MONITORS" accent="#06b6d4" count={monitors.length}>
        {monitors.length === 0 ? (
          <div style={styles.emptyRow}>Not watching any topics</div>
        ) : (
          monitors.map((m) => (
            <div key={m.slug} style={styles.row}>
              <div style={{ ...styles.dot, background: "#06b6d4" }} />
              <div style={styles.rowBody}>
                <div style={styles.rowMain}>{m.topic}</div>
                <div style={styles.rowMeta}>
                  since {m.added || "?"}{m.last_check ? ` · last checked ${m.last_check}` : ""}
                </div>
              </div>
            </div>
          ))
        )}
      </Section>

      {empty && (
        <div style={styles.hint}>
          Try: "remind me to call mom at 6pm", "keep an eye on news about the new iPhone",
          or "keep this in mind — I want to look into switching apartments sometime"
        </div>
      )}
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

function formatWhen(date, time) {
  try {
    const d = new Date(`${date}T${time}:00`);
    const now = new Date();
    const sameDay = d.toDateString() === now.toDateString();
    const datePart = sameDay
      ? "Today"
      : d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
    const timePart = d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
    return `${datePart} at ${timePart}`;
  } catch {
    return `${date} ${time}`;
  }
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
  rowMeta: {
    color: "#8a8a8a",
    fontSize: 10.5,
    marginTop: 1,
  },
  emptyRow: {
    color: "#737373",
    fontStyle: "italic",
    padding: "2px 2px 4px 2px",
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
