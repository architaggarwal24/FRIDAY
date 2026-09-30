/**
 * F.R.I.D.A.Y. — components/Settings.jsx
 * Switch the active LLM provider live, and see usage/quota across every
 * external service — real numbers where the provider exposes them
 * (ElevenLabs, Fish Audio), an honest call count everywhere else.
 */
import React, { useEffect, useState } from "react";

const PROVIDER_LABELS = {
  ollama: "Ollama (local)",
  nvidia: "NVIDIA NIM",
  gemini: "Gemini",
  auto: "Auto (Fast → Strong → Local)",
  fast: "Fast",
  strong: "Strong",
  local: "Local",
  smart: "Smart (tool requests → Strong, chit-chat → Local)",
};

const TTS_PROVIDER_LABELS = {
  elevenlabs: "ElevenLabs",
  fish_audio: "Fish Audio",
  edge: "Edge TTS (free, no API key)",
};

const SERVICE_LABELS = {
  tavily: "Tavily (search)",
  exa: "Exa (search)",
  elevenlabs: "ElevenLabs (TTS)",
  fish_audio: "Fish Audio (TTS)",
  groq_router: "Groq (intent router)",
  groq_vision: "Groq (screen vision)",
  llm_ollama: "Ollama (LLM)",
  llm_nvidia: "NVIDIA NIM (LLM)",
  llm_gemini: "Gemini (LLM)",
};

const STATUS_COLORS = {
  ok: "#10b981",
  exhausted: "#ef4444",
  "not used yet": "#525252",
  "not configured": "#3a3a3a",
};

function UsageRow({ row }) {
  const label = SERVICE_LABELS[row.service] || row.service;
  const color = STATUS_COLORS[row.status] || "#737373";
  const hasRealQuota = row.limit != null && row.limit > 0;
  const hasBalanceOnly = !hasRealQuota && row.balance != null;

  return (
    <div style={styles.usageRow}>
      <div style={styles.usageLabel}>{label}</div>
      {hasRealQuota ? (
        <div style={styles.quotaWrap}>
          <div style={styles.barTrack}>
            <div style={{
              ...styles.barFill,
              width: `${Math.min(100, (row.used / row.limit) * 100)}%`,
              background: "linear-gradient(90deg, #06b6d488, #06b6d4)",
            }} />
          </div>
          <span style={styles.quotaText}>
            {row.used?.toLocaleString?.() ?? row.used} / {row.limit?.toLocaleString?.() ?? row.limit} {row.unit}
          </span>
        </div>
      ) : hasBalanceOnly ? (
        <span style={styles.callCount}>
          {row.balance.toLocaleString(undefined, { maximumFractionDigits: 2 })} {row.unit} remaining
        </span>
      ) : (
        <span style={styles.callCount}>
          {row.status === "not configured" ? "—" : `${row.calls ?? 0} calls`}
        </span>
      )}
      <span style={{ ...styles.statusBadge, color, borderColor: `${color}55` }}>
        {row.status}
      </span>
    </div>
  );
}

export default function Settings({ settings, requestSettings, setLlmProvider, setTtsProvider, setAudioDevices, theme, setTheme, themes }) {
  useEffect(() => {
    requestSettings();
  }, [requestSettings]);

  // null = "not yet initialized from the backend's current value" — once
  // settings_data arrives once, these track the user's *pending* picks
  // separately from what's actually active, so Apply has something to
  // compare against (see audioDirty below).
  const [micSel, setMicSel] = useState(null);
  const [speakerSel, setSpeakerSel] = useState(null);

  useEffect(() => {
    if (!settings) return;
    if (micSel === null && settings.activeMicDevice !== undefined) setMicSel(settings.activeMicDevice);
    if (speakerSel === null && settings.activeSpeakerDevice !== undefined) setSpeakerSel(settings.activeSpeakerDevice);
  }, [settings, micSel, speakerSel]);

  if (!settings) {
    return (
      <div style={styles.container}>
        <div style={styles.loading}>Loading settings…</div>
      </div>
    );
  }

  const { activeProvider, availableProviders, activeTtsProvider, availableTtsProviders, usage, routerTiers = {} } = settings;
  const audioDirty = micSel !== settings.activeMicDevice || speakerSel !== settings.activeSpeakerDevice;

  return (
    <div style={styles.container}>
      <div style={styles.section}>
        <div style={styles.sectionTitle}>THEME</div>
        <div style={styles.providerGrid}>
          {(themes || []).map(t => {
            const active = t.id === theme;
            return (
              <button
                key={t.id}
                onClick={() => setTheme(t.id)}
                style={{
                  ...styles.providerBtn,
                  background: active ? "rgba(232,147,95,0.14)" : "transparent",
                  borderColor: active ? "rgba(232,147,95,0.5)" : "rgba(255,255,255,0.08)",
                  color: active ? "#e8935f" : "#a3a3a3",
                }}
              >
                {active && <span style={styles.activeDot} />}
                {t.label}
              </button>
            );
          })}
        </div>
        <div style={styles.footnote}>
          For testing side by side — swap freely, it's instant and only stored on this machine.
        </div>
      </div>

      <div style={styles.section}>
        <div style={styles.sectionTitle}>ACTIVE LLM PROVIDER</div>
        <div style={styles.providerGrid}>
          {availableProviders.map(p => {
            const active = p === activeProvider;
            // fast/strong/local report whether they have anything
            // configured yet (see brain/model_router.py's tier_status())
            // — auto/nvidia/gemini/ollama aren't single tiers, so there's
            // nothing equivalent to check for them here.
            const tierKnown = ["fast", "strong", "local"].includes(p);
            const tierReady = routerTiers[p];
            return (
              <button
                key={p}
                onClick={() => setLlmProvider(p)}
                style={{
                  ...styles.providerBtn,
                  background: active ? "rgba(232,147,95,0.14)" : "transparent",
                  borderColor: active ? "rgba(232,147,95,0.5)" : "rgba(255,255,255,0.08)",
                  color: active ? "#e8935f" : "#a3a3a3",
                }}
              >
                {active && <span style={styles.activeDot} />}
                {PROVIDER_LABELS[p] || p}
                {tierKnown && !tierReady && <span style={styles.tierWarn}> — not configured</span>}
              </button>
            );
          })}
        </div>
        {routerTiers && (routerTiers.fast || routerTiers.strong || routerTiers.local) !== undefined && (
          <div style={styles.footnote}>
            Router tiers right now — FAST: {routerTiers.fast ? "ready" : "not configured"},
            STRONG: {routerTiers.strong ? "ready" : "not configured"},
            LOCAL: {routerTiers.local ? "ready" : "not configured"}.
            See .env's FAST_MODEL_1_*/FAST_MODEL_2_* for FAST.
          </div>
        )}
      </div>

      <div style={styles.section}>
        <div style={styles.sectionTitle}>ACTIVE TTS (VOICE) PROVIDER</div>
        <div style={styles.providerGrid}>
          {(availableTtsProviders || []).map(p => {
            const active = p === activeTtsProvider;
            return (
              <button
                key={p}
                onClick={() => setTtsProvider(p)}
                style={{
                  ...styles.providerBtn,
                  background: active ? "rgba(6,182,212,0.14)" : "transparent",
                  borderColor: active ? "rgba(6,182,212,0.5)" : "rgba(255,255,255,0.08)",
                  color: active ? "#06b6d4" : "#a3a3a3",
                }}
              >
                {active && <span style={{ ...styles.activeDot, background: "#06b6d4", boxShadow: "0 0 6px #06b6d4" }} />}
                {TTS_PROVIDER_LABELS[p] || p}
              </button>
            );
          })}
        </div>
        <div style={styles.footnote}>
          Switching here lasts for this session only. If a provider fails twice in a
          row (e.g. quota exhausted), FRIDAY moves down the chain automatically —
          ElevenLabs → Fish Audio → edge-tts.
        </div>
      </div>

      <div style={styles.section}>
        <div style={styles.sectionTitle}>AUDIO DEVICES</div>

        <div style={styles.audioField}>
          <label style={styles.audioLabel}>MICROPHONE — what FRIDAY hears you with</label>
          <select
            value={micSel ?? ""}
            onChange={(e) => setMicSel(e.target.value)}
            style={styles.select}
          >
            <option value="">System default</option>
            {(settings.micDevices || []).map(d => (
              <option key={d.name} value={d.name}>
                {d.name}{d.is_default ? " (system default)" : ""}
              </option>
            ))}
          </select>
        </div>

        <div style={styles.audioField}>
          <label style={styles.audioLabel}>SPEAKERS — what FRIDAY talks through</label>
          <select
            value={speakerSel ?? ""}
            onChange={(e) => setSpeakerSel(e.target.value)}
            style={styles.select}
          >
            <option value="">System default</option>
            {(settings.speakerDevices || []).map(d => (
              <option key={d.name} value={d.name}>
                {d.name}{d.is_default ? " (system default)" : ""}
              </option>
            ))}
          </select>
        </div>

        {(settings.micDevices?.length === 0 && settings.speakerDevices?.length === 0) && (
          <div style={styles.audioWarn}>
            No devices found — is `sounddevice` installed and able to see your audio hardware?
          </div>
        )}

        <button
          disabled={!audioDirty}
          onClick={() => setAudioDevices({ micName: micSel, speakerName: speakerSel })}
          style={{
            ...styles.applyBtn,
            opacity: audioDirty ? 1 : 0.35,
            cursor: audioDirty ? "pointer" : "default",
          }}
        >
          ▶ APPLY
        </button>

        <div style={styles.footnote}>
          Applying resets the mic session with the new device — your conversation is kept.
          Session-only, same as the provider switches above: reverts to whatever
          MIC_DEVICE_INDEX / SPEAKER_DEVICE_INDEX say in .env on next launch.
        </div>
      </div>

      <div style={styles.section}>
        <div style={styles.sectionTitleRow}>
          <span style={styles.sectionTitle}>USAGE &amp; QUOTA</span>
          <button style={styles.refreshBtn} onClick={requestSettings}>↻ refresh</button>
        </div>
        <div style={styles.usageList}>
          {usage.map(row => <UsageRow key={row.service} row={row} />)}
        </div>
        <div style={styles.footnote}>
          Real remaining quota shown where the provider exposes it (ElevenLabs, Fish Audio).
          Everything else is a call count FRIDAY has tracked locally, not a
          guaranteed remaining balance.
        </div>
      </div>
    </div>
  );
}

const styles = {
  container: {
    background: "rgba(14,14,14,0.9)",
    height: "100%",
    overflowY: "auto",
    padding: "14px 16px",
    display: "flex",
    flexDirection: "column",
    gap: 20,
  },
  loading: {
    fontSize: 10,
    fontFamily: "'JetBrains Mono', monospace",
    color: "#525252",
    padding: "20px 0",
  },
  section: {
    display: "flex",
    flexDirection: "column",
    gap: 10,
  },
  sectionTitleRow: {
    display: "flex",
    justifyContent: "space-between",
    alignItems: "center",
  },
  sectionTitle: {
    fontSize: 9,
    fontFamily: "'JetBrains Mono', monospace",
    letterSpacing: 2,
    color: "#737373",
    fontWeight: 600,
  },
  refreshBtn: {
    fontSize: 9,
    fontFamily: "'JetBrains Mono', monospace",
    color: "#e8935f",
    background: "transparent",
    border: "1px solid rgba(232,147,95,0.25)",
    borderRadius: 5,
    padding: "3px 8px",
    cursor: "pointer",
  },
  providerGrid: {
    display: "flex",
    flexDirection: "column",
    gap: 6,
  },
  providerBtn: {
    display: "flex",
    alignItems: "center",
    gap: 6,
    fontSize: 10,
    fontFamily: "'JetBrains Mono', monospace",
    border: "1px solid",
    borderRadius: 6,
    padding: "8px 10px",
    cursor: "pointer",
    textAlign: "left",
    transition: "all 0.15s ease",
  },
  activeDot: {
    width: 5, height: 5, borderRadius: "50%",
    background: "#e8935f",
    boxShadow: "0 0 6px #e8935f",
    flexShrink: 0,
  },
  usageList: {
    display: "flex",
    flexDirection: "column",
    gap: 8,
  },
  usageRow: {
    display: "flex",
    alignItems: "center",
    gap: 10,
    padding: "7px 0",
    borderBottom: "1px solid rgba(255,255,255,0.04)",
  },
  usageLabel: {
    fontSize: 9.5,
    fontFamily: "'JetBrains Mono', monospace",
    color: "#d4d4d4",
    width: 150,
    flexShrink: 0,
  },
  quotaWrap: {
    flex: 1,
    display: "flex",
    alignItems: "center",
    gap: 8,
  },
  barTrack: {
    flex: 1,
    height: 4,
    background: "rgba(255,255,255,0.05)",
    borderRadius: 2,
    overflow: "hidden",
  },
  barFill: {
    height: "100%",
    borderRadius: 2,
    transition: "width 0.5s ease",
  },
  quotaText: {
    fontSize: 8.5,
    fontFamily: "'JetBrains Mono', monospace",
    color: "#737373",
    whiteSpace: "nowrap",
  },
  callCount: {
    flex: 1,
    fontSize: 9,
    fontFamily: "'JetBrains Mono', monospace",
    color: "#737373",
  },
  statusBadge: {
    fontSize: 8,
    fontFamily: "'JetBrains Mono', monospace",
    letterSpacing: 0.5,
    border: "1px solid",
    borderRadius: 4,
    padding: "2px 6px",
    flexShrink: 0,
  },
  footnote: {
    fontSize: 8.5,
    fontFamily: "'JetBrains Mono', monospace",
    color: "#525252",
    lineHeight: 1.5,
    marginTop: 4,
  },
  audioField: {
    display: "flex",
    flexDirection: "column",
    gap: 5,
  },
  audioLabel: {
    fontSize: 9,
    fontFamily: "'JetBrains Mono', monospace",
    color: "#a3a3a3",
    letterSpacing: 0.3,
  },
  select: {
    fontSize: 10.5,
    fontFamily: "'JetBrains Mono', monospace",
    color: "#e5e5e5",
    background: "rgba(255,255,255,0.04)",
    border: "1px solid rgba(232,147,95,0.2)",
    borderRadius: 6,
    padding: "7px 8px",
    outline: "none",
    cursor: "pointer",
  },
  audioWarn: {
    fontSize: 9,
    fontFamily: "'JetBrains Mono', monospace",
    color: "#ef4444",
  },
  tierWarn: {
    fontSize: 8,
    opacity: 0.6,
  },
  applyBtn: {
    alignSelf: "flex-start",
    fontSize: 9,
    fontFamily: "'JetBrains Mono', monospace",
    letterSpacing: 1.5,
    fontWeight: 700,
    color: "#e8935f",
    background: "rgba(232,147,95,0.1)",
    border: "1px solid rgba(232,147,95,0.35)",
    borderRadius: 6,
    padding: "6px 16px",
    transition: "opacity 0.15s ease",
  },
};
