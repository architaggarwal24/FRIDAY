/**
 * F.R.I.D.A.Y. — components/SystemStats.jsx
 * Live system stats with mini sparkline graphs.
 */

import React, { useState, useEffect } from "react";
import { AreaChart, Area, ResponsiveContainer } from "recharts";

function StatBar({ label, value, max = 100, color, suffix = "%" }) {
  return (
    <div style={styles.statRow}>
      <div style={styles.statLabel}>{label}</div>
      <div style={styles.barTrack}>
        <div style={{
          ...styles.barFill,
          width: `${Math.min(100, (value / max) * 100)}%`,
          // color-mix() instead of hex-alpha-suffix concatenation (the old
          // `${color}88` trick) because color can now be a var(--x)
          // reference, not just a literal hex string — string-concatenating
          // an alpha suffix onto "var(--accent)" produces garbage, whereas
          // color-mix() accepts any valid CSS color expression.
          background: `linear-gradient(90deg, color-mix(in srgb, ${color} 53%, transparent), ${color})`,
          boxShadow: `0 0 8px color-mix(in srgb, ${color} 27%, transparent)`,
        }} />
      </div>
      <div style={{ ...styles.statValue, color }}>{value}{suffix}</div>
    </div>
  );
}

function Sparkline({ data, color }) {
  // A stable per-instance id, NOT derived from `color` — the old
  // `grad-${color.replace("#","")}` scheme assumed color was always a
  // literal hex string. It isn't anymore (can be a var(--x) reference),
  // and three Sparklines render simultaneously here (CPU/RAM/GPU) so a
  // fixed shared id would've had them stomping on each other's gradients
  // regardless.
  const gradId = `grad-${React.useId()}`;
  return (
    <ResponsiveContainer width="100%" height={28}>
      <AreaChart data={data} margin={{ top: 2, right: 0, bottom: 0, left: 0 }}>
        <defs>
          <linearGradient id={gradId} x1="0" y1="0" x2="0" y2="1">
            <stop offset="5%"  stopColor={color} stopOpacity={0.4} />
            <stop offset="95%" stopColor={color} stopOpacity={0} />
          </linearGradient>
        </defs>
        <Area
          type="monotone"
          dataKey="v"
          stroke={color}
          strokeWidth={1.5}
          fill={`url(#${gradId})`}
          dot={false}
          isAnimationActive={false}
        />
      </AreaChart>
    </ResponsiveContainer>
  );
}

const MAX_HISTORY = 30;

export default function SystemStats({ stats, hwinfo }) {
  const cpuName = hwinfo?.cpu_name || "CPU";
  const gpuName = hwinfo?.gpu_name || "GPU";
  const [history, setHistory] = useState({
    cpu:  Array(MAX_HISTORY).fill({ v: 0 }),
    ram:  Array(MAX_HISTORY).fill({ v: 0 }),
    gpu:  Array(MAX_HISTORY).fill({ v: 0 }),
  });

  useEffect(() => {
    setHistory(prev => ({
      cpu: [...prev.cpu.slice(1), { v: stats.cpu }],
      ram: [...prev.ram.slice(1), { v: stats.ram }],
      gpu: [...prev.gpu.slice(1), { v: stats.gpu }],
    }));
  }, [stats]);

  const tempColor = stats.gpu_temp > 80 ? "#ef4444"
                  : stats.gpu_temp > 65 ? "var(--accent)"
                  : "#10b981";

  return (
    <div style={styles.container}>
      <div style={styles.header}>
        <span style={{ fontSize: 9, color: "#10b981", fontFamily: "'JetBrains Mono', monospace" }}>● LIVE</span>
      </div>

      <div style={styles.body}>
        {/* CPU */}
        <div style={styles.section}>
          <div style={styles.sectionLabel}>
            <span>CPU — {cpuName}</span>
            <span style={{ color: "#06b6d4" }}>{stats.cpu}%</span>
          </div>
          <Sparkline data={history.cpu} color="#06b6d4" />
          <StatBar label="" value={stats.cpu} color="#06b6d4" />
        </div>

        {/* RAM */}
        <div style={styles.section}>
          <div style={styles.sectionLabel}>
            <span>RAM</span>
            <span style={{ color: "#a78bfa" }}>{stats.ram_used}GB / {stats.ram_total}GB</span>
          </div>
          <Sparkline data={history.ram} color="#a78bfa" />
          <StatBar label="" value={stats.ram} color="#a78bfa" />
        </div>

        {/* GPU */}
        <div style={styles.section}>
          <div style={styles.sectionLabel}>
            <span>{gpuName}</span>
            <span style={{ color: "var(--accent)" }}>{stats.gpu}%</span>
          </div>
          <Sparkline data={history.gpu} color="var(--accent)" />
          <StatBar label="" value={stats.gpu} color="var(--accent)" />
        </div>

        {/* VRAM + Temp */}
        <div style={styles.row2}>
          <div style={styles.miniStat}>
            <span style={styles.miniLabel}>VRAM</span>
            <span style={{ color: "var(--accent)", fontSize: 13, fontWeight: 700 }}>
              {stats.vram_used}<span style={{ fontSize: 9, color: "#737373" }}>/{stats.vram_total}GB</span>
            </span>
          </div>
          <div style={styles.miniStat}>
            <span style={styles.miniLabel}>GPU TEMP</span>
            <span style={{ color: tempColor, fontSize: 13, fontWeight: 700 }}>
              {stats.gpu_temp}<span style={{ fontSize: 9 }}>°C</span>
            </span>
          </div>
        </div>
      </div>
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
  body: {
    padding: "10px 14px",
    display: "flex",
    flexDirection: "column",
    gap: 10,
    flex: 1,
  },
  section: {
    display: "flex",
    flexDirection: "column",
    gap: 3,
  },
  sectionLabel: {
    display: "flex",
    justifyContent: "space-between",
    fontSize: 9,
    fontFamily: "'JetBrains Mono', monospace",
    color: "#737373",
    letterSpacing: 1,
  },
  statRow: {
    display: "flex",
    alignItems: "center",
    gap: 8,
  },
  statLabel: {
    fontSize: 8,
    color: "#525252",
    fontFamily: "'JetBrains Mono', monospace",
    width: 0,
  },
  barTrack: {
    flex: 1,
    height: 3,
    background: "rgba(255,255,255,0.05)",
    borderRadius: 2,
    overflow: "hidden",
  },
  barFill: {
    height: "100%",
    borderRadius: 2,
    transition: "width 0.5s ease",
  },
  statValue: {
    fontSize: 9,
    fontFamily: "'JetBrains Mono', monospace",
    fontWeight: 700,
    minWidth: 30,
    textAlign: "right",
  },
  row2: {
    display: "flex",
    gap: 8,
    marginTop: 4,
  },
  miniStat: {
    flex: 1,
    background: "rgba(255,255,255,0.03)",
    border: "1px solid rgba(var(--accent-rgb), 0.1)",
    borderRadius: 8,
    padding: "8px 10px",
    display: "flex",
    flexDirection: "column",
    gap: 4,
  },
  miniLabel: {
    fontSize: 8,
    color: "#525252",
    fontFamily: "'JetBrains Mono', monospace",
    letterSpacing: 2,
  },
};
