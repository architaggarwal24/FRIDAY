/**
 * F.R.I.D.A.Y. — components/ActivityFeed.jsx
 * Live feed showing what FRIDAY is doing in real time.
 * Shows action executions, screen analysis results, system events.
 */

import React, { useRef, useEffect } from "react";
import { motion, AnimatePresence } from "framer-motion";

export default function ActivityFeed({ activities, screenDesc }) {
  const bottomRef = useRef(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [activities]);

  return (
    <div style={styles.container}>
      <div style={styles.header}>
        <motion.div
          animate={{ opacity: [0.4, 1, 0.4] }}
          transition={{ repeat: Infinity, duration: 2 }}
          style={styles.liveDot}
        />
      </div>

      <div style={styles.feed}>
        <AnimatePresence initial={false}>
          {activities.length === 0 && (
            <div style={styles.empty}>No activity yet...</div>
          )}
          {activities.map((a, i) => (
            <motion.div
              key={i}
              initial={{ opacity: 0, y: 4 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.15 }}
              style={styles.entry}
            >
              <span style={styles.entryIcon}>{a.icon || "◆"}</span>
              <span style={styles.entryText}>{a.text}</span>
              <span style={styles.entryTime}>
                {new Date(a.ts).toLocaleTimeString("en-IN", {
                  hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false
                })}
              </span>
            </motion.div>
          ))}
        </AnimatePresence>
        <div ref={bottomRef} />
      </div>

      {/* Screen description panel */}
      {screenDesc && (
        <div style={styles.screenPanel}>
          <div style={styles.screenLabel}>SCREEN ANALYSIS</div>
          <div style={styles.screenText}>{screenDesc}</div>
        </div>
      )}
    </div>
  );
}

const styles = {
  container: {
    background: "rgba(14,14,14,0.9)",
    border: "1px solid rgba(var(--accent-rgb), 0.15)",
    borderRadius: 12,
    overflow: "hidden",
    display: "flex",
    flexDirection: "column",
    height: "100%",
  },
  header: {
    display: "flex",
    justifyContent: "flex-end",
    alignItems: "center",
    padding: "10px 14px",
    borderBottom: "1px solid rgba(var(--accent-rgb), 0.1)",
  },
  liveDot: {
    width: 6,
    height: 6,
    borderRadius: "50%",
    background: "#10b981",
  },
  feed: {
    flex: 1,
    overflowY: "auto",
    padding: "8px 14px",
    display: "flex",
    flexDirection: "column",
    gap: 6,
  },
  empty: {
    color: "#333",
    fontSize: 10,
    fontFamily: "'JetBrains Mono', monospace",
    textAlign: "center",
    marginTop: 16,
    letterSpacing: 1,
  },
  entry: {
    display: "flex",
    alignItems: "center",
    gap: 8,
  },
  entryIcon: {
    fontSize: 10,
    color: "var(--accent)",
    flexShrink: 0,
    width: 14,
    textAlign: "center",
  },
  entryText: {
    flex: 1,
    fontSize: 10,
    color: "#a3a3a3",
    fontFamily: "'JetBrains Mono', monospace",
    letterSpacing: 0.3,
    overflow: "hidden",
    textOverflow: "ellipsis",
    whiteSpace: "nowrap",
  },
  entryTime: {
    fontSize: 8,
    color: "#404040",
    fontFamily: "'JetBrains Mono', monospace",
    flexShrink: 0,
  },
  screenPanel: {
    borderTop: "1px solid rgba(var(--accent-rgb), 0.08)",
    padding: "8px 14px",
    background: "rgba(6,182,212,0.03)",
  },
  screenLabel: {
    fontSize: 8,
    fontFamily: "'JetBrains Mono', monospace",
    letterSpacing: 2,
    color: "#06b6d4",
    marginBottom: 4,
  },
  screenText: {
    fontSize: 10,
    color: "#737373",
    fontFamily: "'JetBrains Mono', monospace",
    lineHeight: 1.5,
  },
};
