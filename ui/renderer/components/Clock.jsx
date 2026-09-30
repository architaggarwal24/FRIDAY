/**
 * F.R.I.D.A.Y. — components/Clock.jsx
 */

import React, { useState, useEffect } from "react";

export default function Clock() {
  const [now, setNow] = useState(new Date());

  useEffect(() => {
    const id = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(id);
  }, []);

  const hh = now.getHours().toString().padStart(2, "0");
  const mm = now.getMinutes().toString().padStart(2, "0");
  const ss = now.getSeconds().toString().padStart(2, "0");
  const date = now.toLocaleDateString("en-IN", {
    weekday: "long", day: "numeric", month: "long", year: "numeric"
  });

  return (
    <div style={styles.container}>
      <div style={styles.time}>
        <span style={styles.hhmm}>{hh}:{mm}</span>
        <span style={styles.ss}>{ss}</span>
      </div>
      <div style={styles.date}>{date}</div>
    </div>
  );
}

const styles = {
  container: {
    height: "100%",
    background: "rgba(14,14,14,0.9)",
    border: "1px solid rgba(var(--accent-rgb), 0.15)",
    borderRadius: 12,
    padding: "12px 16px",
    display: "flex",
    flexDirection: "column",
    justifyContent: "center",
    gap: 4,
  },
  time: {
    display: "flex",
    alignItems: "baseline",
    gap: 4,
  },
  hhmm: {
    fontSize: 32,
    fontFamily: "'JetBrains Mono', monospace",
    fontWeight: 700,
    color: "var(--accent)",
    letterSpacing: 2,
    lineHeight: 1,
    textShadow: "0 0 20px rgba(var(--accent-rgb), 0.4)",
  },
  ss: {
    fontSize: 16,
    fontFamily: "'JetBrains Mono', monospace",
    color: "#b45309",
    letterSpacing: 1,
  },
  date: {
    fontSize: 10,
    color: "#737373",
    fontFamily: "'JetBrains Mono', monospace",
    letterSpacing: 1,
  },
};
