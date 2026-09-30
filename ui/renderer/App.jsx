/**
 * F.R.I.D.A.Y. — renderer/App.jsx
 * Orb stays as the fixed "desktop" anchor on the left. Everything else
 * (transcript, stats, activity, clock) is a floating window opened from
 * the taskbar at the bottom — first pass at the "feels like an OS" shell.
 */

import React, { useCallback, useRef, useState, Suspense, lazy } from "react";
import { motion } from "framer-motion";
import "./styles/theme.css";

import { useFriday, STATE_COLORS, getWsRef } from "./hooks/useFriday";
import { useWindowManager } from "./hooks/useWindowManager";
import { useTheme } from "./hooks/useTheme";
import Orb from "./components/Orb";
import Window from "./components/Window";
import Taskbar from "./components/Taskbar";
import { StateBadge, IntentBadge, ConnectionDot } from "./components/StatusBar";
import ConfirmDialog from "./components/ConfirmDialog";
import { ToastStack } from "./components/ActionToast";
import SettingsModal from "./components/SettingsModal";

// Each window panel is opened on demand from the taskbar, not always
// visible — split into its own chunk instead of shipping every panel's
// dependencies (recharts, react-markdown, ...) in the initial bundle
// whether or not that window is ever opened this session.
const Transcript = lazy(() => import(/* webpackChunkName: "panel-transcript" */ "./components/Transcript"));
const SystemStats = lazy(() => import(/* webpackChunkName: "panel-stats" */ "./components/SystemStats"));
const Clock = lazy(() => import(/* webpackChunkName: "panel-clock" */ "./components/Clock"));
const ActivityFeed = lazy(() => import(/* webpackChunkName: "panel-activity" */ "./components/ActivityFeed"));
const Settings = lazy(() => import(/* webpackChunkName: "panel-settings" */ "./components/Settings"));
const WatchlistPanel = lazy(() => import(/* webpackChunkName: "panel-watchlist" */ "./components/WatchlistPanel"));
const MemoryPanel = lazy(() => import(/* webpackChunkName: "panel-memory" */ "./components/MemoryPanel"));
// 3d-force-graph + three are large — this one especially benefits from
// staying out of the initial bundle, only loading when actually opened.
const Galaxy = lazy(() => import(/* webpackChunkName: "panel-galaxy" */ "./components/Galaxy"));
// Not a panel — a headless background watcher. Lazy for the same reason
// as Galaxy: @mediapipe/tasks-vision is large and most launches never
// turn the posture watch on.
const PostureWatch = lazy(() => import(/* webpackChunkName: "posture-watch" */ "./components/PostureWatch"));
const ScreenWatch = lazy(() => import(/* webpackChunkName: "screen-watch" */ "./components/ScreenWatch"));

const WINDOW_TITLES = {
  transcript: "COMM LOG",
  stats: "SYS MONITOR",
  activity: "ACTIVITY",
  clock: "CLOCK",
  settings: "SETTINGS",
  watchlist: "WATCHLIST",
  memory: "MEMORY",
  galaxy: "GALAXY",
};

export default function App() {
  const {
    state, transcript, intent, stats, connected, audioLevel, hwinfo,
    toasts, dismissToast,
    activities, screenDesc,
    confirmRequest, respondConfirm,
    sendText, sendFile,
    settings, requestSettings, setLlmProvider, setTtsProvider, setAudioDevices,
    watchlist, requestWatchlist,
    memory, requestMemory,
    graph, requestGraph,
    memoryMatch,
    sendPosture,
    sendCommand,
  } = useFriday();

  const { theme, setTheme, themes } = useTheme(state);

  // Posture watch is opt-in and off by default — it's a camera.
  const [postureEnabled, setPostureEnabled] = useState(false);
  // Screen watch is opt-in too — it asks for a screen-share prompt.
  const [screenWatchEnabled, setScreenWatchEnabled] = useState(false);

  const desktopRef = useRef(null);

  // Only Galaxy still lives in the floating-window system now — clock,
  // stats, activity, watchlist, and memory moved to the fixed
  // three-column layout below (see styles.leftColumn/centerColumn/
  // rightColumn), and transcript is the center column's own scrollable
  // panel (full history, not a separate modal — see the center column
  // JSX below).
  // Nothing auto-opens; Galaxy is opt-in via the taskbar same as before.
  const { windows, toggleWindow, closeWindow, focusWindow, moveWindow, resizeWindow } =
    useWindowManager([], desktopRef);

  // Settings is deliberately NOT part of the windows/useWindowManager
  // system — it's a modal overlay (opens centered on top of everything,
  // closes on an outside click) rather than a draggable desktop window,
  // so it needs its own simple open/closed state instead of a position.
  const [showSettings, setShowSettings] = useState(false);
  const toggleSettings = useCallback(() => setShowSettings(v => !v), []);

  // Taskbar treats every app uniformly — settings is the one exception,
  // routed to the modal toggle instead of the normal window system.
  const handleTaskbarToggle = useCallback((id) => {
    if (id === "settings") toggleSettings();
    else toggleWindow(id);
  }, [toggleSettings, toggleWindow]);

  const isListening = state === "listening";
  const stateColor  = STATE_COLORS[state] || STATE_COLORS.idle;
  const isSpeaking  = state === "speaking";

  const handleMicClick = useCallback(() => {
    const ws = getWsRef();
    if (!ws || ws.readyState !== 1) return;
    if (isSpeaking) {
      ws.send(JSON.stringify({ cmd: "interrupt" }));
    } else if (isListening) {
      ws.send(JSON.stringify({ cmd: "stop_listen" }));
    } else {
      ws.send(JSON.stringify({ cmd: "start_listen" }));
    }
  }, [isListening, isSpeaking]);

  const getBounds = useCallback(() => {
    const el = desktopRef.current;
    if (!el) return { left: 0, top: 0, right: 1000, bottom: 700 };
    return { left: 0, top: 0, right: el.clientWidth, bottom: el.clientHeight };
  }, []);

  // Only Galaxy still renders through this lookup — every other window
  // type moved to the fixed columns and is rendered directly there now
  // (see the JSX below), not dispatched through id like this anymore.
  const renderWindowContent = (id) => {
    switch (id) {
      case "galaxy":
        return <Galaxy graph={graph} requestGraph={requestGraph} memoryMatch={memoryMatch} />;
      default:
        return null;
    }
  };

  return (
    <div style={styles.root}>

      {/* Headless: no UI, renders nothing. All camera work stays in the
          renderer; only booleans go to the backend. */}
      {postureEnabled && (
        <Suspense fallback={null}>
          <PostureWatch
            enabled={postureEnabled}
            sendPosture={sendPosture}
            micEnabled={isListening}
            onNotice={(msg) => console.info(`[FRIDAY] ${msg}`)}
          />
        </Suspense>
      )}

      {screenWatchEnabled && (
        <Suspense fallback={null}>
          <ScreenWatch
            enabled={screenWatchEnabled}
            sendCommand={sendCommand}
            onNotice={(msg) => console.info(`[FRIDAY] ${msg}`)}
          />
        </Suspense>
      )}

      {/* Ambient glow */}
      <div style={{
        ...styles.ambientGlow,
        background: `radial-gradient(ellipse at 22% 50%, ${stateColor}06 0%, transparent 60%)`,
      }} />

      {/* Titlebar */}
      <div style={styles.titlebar}>
        <div style={styles.titleLeft}>
          <div style={styles.logoMark}>▲</div>
          <span style={styles.logoText}>F.R.I.D.A.Y.</span>
          <span style={styles.logoSub}>FEMALE REPLACEMENT INTELLIGENT DIGITAL ASSISTANT YOUTH</span>
        </div>
        <div style={styles.titleRight}>
          <ConnectionDot connected={connected} />
          <IntentBadge intent={intent} />
        </div>
      </div>

      {/* Main */}
      <div style={styles.main}>

        {/* LEFT column — Clock, then PC stats. Fixed, not draggable —
            these are always-visible now rather than toggleable windows. */}
        <div style={styles.sideColumn}>
          <div className="glass-panel" style={styles.columnPanelAuto}>
            <Suspense fallback={<div style={styles.panelLoading}>LOADING…</div>}>
              <Clock />
            </Suspense>
          </div>
          <div className="glass-panel" style={styles.columnPanelFlex}>
            <Suspense fallback={<div style={styles.panelLoading}>LOADING…</div>}>
              <SystemStats stats={stats} hwinfo={hwinfo} />
            </Suspense>
          </div>
        </div>

        {/* CENTER — Orb, centered, with the compact transcript underneath.
            "desktop" identity now lives here instead of at the left edge. */}
        <div style={styles.centerColumn}>
          <div className="hud-only" style={{ ...styles.corner, top: 12, left: 12 }} />
          <div className="hud-only" style={{ ...styles.corner, top: 12, right: 12, transform: "scaleX(-1)" }} />
          <div className="hud-only" style={{ ...styles.corner, bottom: 12, left: 12, transform: "scaleY(-1)" }} />
          <div className="hud-only" style={{ ...styles.corner, bottom: 12, right: 12, transform: "scale(-1)" }} />

          <div style={styles.orbCanvas}>
            <Orb state={state} audioLevel={audioLevel} />
          </div>

          <div style={styles.orbControls}>
            <StateBadge state={state} />
            <motion.button
              onClick={handleMicClick}
              whileHover={{ scale: 1.05 }}
              whileTap={{ scale: 0.95 }}
              animate={isListening ? {
                boxShadow: ["0 0 0px #06b6d4", "0 0 20px #06b6d4", "0 0 0px #06b6d4"],
              } : isSpeaking ? {
                boxShadow: ["0 0 0px #10b981", "0 0 16px #10b981", "0 0 0px #10b981"],
              } : {}}
              transition={{ repeat: Infinity, duration: 1 }}
              style={{
                ...styles.micBtn,
                background: isListening ? "rgba(6,182,212,0.15)"
                          : isSpeaking  ? "rgba(239,68,68,0.12)"
                          : "rgba(255,255,255,0.04)",
                borderColor: isListening ? "#06b6d4"
                           : isSpeaking  ? "#ef4444"
                           : "rgba(255,255,255,0.1)",
              }}
            >
              <span style={{ fontSize: 16 }}>
                {isListening ? "⏹" : isSpeaking ? "✕" : "🎤"}
              </span>
              <span style={{
                fontSize: 9,
                fontFamily: "'JetBrains Mono', monospace",
                letterSpacing: 2,
                color: isListening ? "#06b6d4"
                     : isSpeaking  ? "#ef4444"
                     : "#525252",
              }}>
                {isListening ? "RECORDING" : isSpeaking ? "INTERRUPT" : "SPEAK"}
              </span>
            </motion.button>
          </div>

          <div className="glass-panel" style={styles.miniTranscript}>
            <Suspense fallback={<div style={styles.panelLoading}>LOADING…</div>}>
              <Transcript
                transcript={transcript} state={state}
                onSend={sendText} onSendFile={sendFile} connected={connected}
              />
            </Suspense>
          </div>
        </div>

        {/* RIGHT column — Activity, then Memory, then Watchlist. Same
            fixed-stack treatment as the left column. */}
        <div style={styles.sideColumn}>
          <div className="glass-panel" style={styles.columnPanelFlex}>
            <Suspense fallback={<div style={styles.panelLoading}>LOADING…</div>}>
              <ActivityFeed activities={activities} screenDesc={screenDesc} />
            </Suspense>
          </div>
          <div className="glass-panel" style={styles.columnPanelFlex}>
            <Suspense fallback={<div style={styles.panelLoading}>LOADING…</div>}>
              <MemoryPanel memory={memory} requestMemory={requestMemory} />
            </Suspense>
          </div>
          <div className="glass-panel" style={styles.columnPanelFlex}>
            <Suspense fallback={<div style={styles.panelLoading}>LOADING…</div>}>
              <WatchlistPanel watchlist={watchlist} requestWatchlist={requestWatchlist} />
            </Suspense>
          </div>
        </div>

        {/* Galaxy is the only thing still using the floating-window system
            — an on-demand overlay rather than part of the fixed columns,
            toggled from the taskbar same as before. */}
        <div ref={desktopRef} style={styles.floatingLayer}>
          {Object.entries(windows).map(([id, win]) => (
            <Window
              key={id}
              id={id}
              title={WINDOW_TITLES[id] || id.toUpperCase()}
              x={win.x} y={win.y} width={win.width} height={win.height} z={win.z}
              bounds={getBounds()}
              onClose={closeWindow}
              onFocus={focusWindow}
              onMove={moveWindow}
              onResize={resizeWindow}
            >
              <Suspense fallback={<div style={styles.panelLoading}>LOADING…</div>}>
                {renderWindowContent(id)}
              </Suspense>
            </Window>
          ))}
        </div>
      </div>

      {/* Taskbar / dock */}
      <Taskbar windows={windows} onToggle={handleTaskbarToggle} settingsOpen={showSettings} />

      {/* Bottom status bar */}
      <div style={styles.statusBar}>
        <div style={styles.statusCenter}>
          <motion.div
            className="hud-only"
            animate={{ x: ["-100%", "100%"] }}
            transition={{ repeat: Infinity, duration: 6, ease: "linear" }}
            style={styles.scanLine}
          />
          <span style={styles.statusText}>
            NEURAL CORE ACTIVE — WS:8765 — OLM:11434
          </span>
        </div>
        <div style={styles.statusRight}>
          <span style={{ fontSize: 9, color: "#333", fontFamily: "'JetBrains Mono', monospace" }}>
            {hwinfo?.gpu_name || "GPU"} · {hwinfo?.cpu_name || "CPU"}
            {stats?.ram_total ? ` · ${stats.ram_total}GB` : ""}
          </span>
        </div>
      </div>

      {/* Overlays */}
      <ConfirmDialog request={confirmRequest} onRespond={respondConfirm} />
      <ToastStack toasts={toasts} onDismiss={dismissToast} />
      <SettingsModal open={showSettings} onClose={() => setShowSettings(false)}>
        <Suspense fallback={<div style={styles.panelLoading}>LOADING…</div>}>
          <Settings settings={settings} requestSettings={requestSettings} setLlmProvider={setLlmProvider} setTtsProvider={setTtsProvider} setAudioDevices={setAudioDevices} theme={theme} setTheme={setTheme} themes={themes} />
        </Suspense>
      </SettingsModal>

    </div>
  );
}

const styles = {
  root: {
    width: "100vw", height: "100vh",
    background: "#0b0d10",
    display: "flex", flexDirection: "column",
    overflow: "hidden", position: "relative",
    userSelect: "none",
  },
  ambientGlow: {
    position: "absolute", inset: 0,
    pointerEvents: "none", zIndex: 0,
    transition: "background 1.5s ease",
  },
  titlebar: {
    height: 40, display: "flex", alignItems: "center",
    justifyContent: "space-between", padding: "0 16px",
    background: "var(--titlebar-bg)",
    backdropFilter: "blur(var(--panel-blur)) saturate(var(--panel-saturate))",
    WebkitBackdropFilter: "blur(var(--panel-blur)) saturate(var(--panel-saturate))",
    borderBottom: "1px solid rgba(var(--accent-rgb), 0.1)",
    WebkitAppRegion: "drag", zIndex: 10, flexShrink: 0, position: "relative",
  },
  titleLeft: { display: "flex", alignItems: "center", gap: 10 },
  logoMark: { color: "var(--accent)", fontSize: 14, fontWeight: 800 },
  logoText: {
    fontSize: 13, fontFamily: "'JetBrains Mono', monospace",
    letterSpacing: 4, color: "var(--accent)", fontWeight: 700,
  },
  logoSub: {
    fontSize: 7, fontFamily: "'JetBrains Mono', monospace",
    letterSpacing: 2, color: "#333",
  },
  titleRight: {
    display: "flex", alignItems: "center", gap: 12,
    WebkitAppRegion: "no-drag",
  },
  main: {
    flex: 1, display: "flex", overflow: "hidden",
    zIndex: 1, minHeight: 0, position: "relative",
    gap: 12, padding: 12,
  },
  sideColumn: {
    width: 300, flexShrink: 0,
    display: "flex", flexDirection: "column", gap: 12,
    minHeight: 0,
  },
  columnPanelAuto: {
    flexShrink: 0, overflow: "hidden", padding: 12,
  },
  columnPanelFlex: {
    flex: 1, minHeight: 0, overflow: "hidden", padding: 12,
    display: "flex", flexDirection: "column",
  },
  centerColumn: {
    flex: 1, minWidth: 0,
    display: "flex", flexDirection: "column",
    alignItems: "center", justifyContent: "center",
    position: "relative", overflow: "hidden",
    backgroundImage: "var(--desktop-bg-image)",
    backgroundSize: "var(--desktop-bg-size)",
    borderRadius: "var(--panel-radius-lg)",
  },
  orbCanvas: {
    flex: 1, width: "100%", minHeight: 0,
    overflow: "hidden", position: "relative", contain: "strict",
  },
  orbControls: {
    width: "100%", maxWidth: 420, height: 48, display: "flex",
    alignItems: "center", justifyContent: "space-between",
    padding: "0 16px", flexShrink: 0,
  },
  miniTranscript: {
    width: "100%", maxWidth: 560, height: 340, flexShrink: 0,
    margin: "0 16px 16px 16px", overflow: "hidden",
  },
  corner: {
    position: "absolute", width: 18, height: 18,
    border: "1.5px solid rgba(var(--accent-rgb), 0.25)",
    borderRight: "none", borderBottom: "none",
    zIndex: 2, pointerEvents: "none",
  },
  micBtn: {
    display: "flex", alignItems: "center", gap: 6,
    padding: "6px 14px", border: "1px solid",
    borderRadius: 20, cursor: "pointer",
    transition: "all 0.2s ease", background: "transparent",
  },
  // Galaxy is the only thing left using the floating-window system —
  // this spans the full 3-column width (not just one column) so it can
  // be dragged anywhere, but stays click-through everywhere it ISN'T
  // actually rendering a window (see the pointerEvents:"auto" Window.jsx
  // sets on its own root — that's what makes the window itself clickable
  // again despite this wrapper being "none").
  floatingLayer: {
    position: "absolute", inset: 12,
    pointerEvents: "none", zIndex: 5,
  },
  panelLoading: {
    display: "flex", alignItems: "center", justifyContent: "center",
    height: "100%", color: "var(--accent)", opacity: 0.5, fontSize: 11,
    fontFamily: "'JetBrains Mono', monospace", letterSpacing: 2,
  },
  statusBar: {
    height: 26, display: "flex", alignItems: "center",
    justifyContent: "space-between", padding: "0 16px",
    borderTop: "1px solid rgba(var(--accent-rgb), 0.08)",
    background: "var(--taskbar-bg)",
    backdropFilter: "blur(var(--panel-blur)) saturate(var(--panel-saturate))",
    WebkitBackdropFilter: "blur(var(--panel-blur)) saturate(var(--panel-saturate))",
    zIndex: 10, flexShrink: 0,
  },
  statusCenter: {
    flex: 1, display: "flex", alignItems: "center",
    justifyContent: "center", position: "relative",
    overflow: "hidden", height: "100%",
  },
  statusText: {
    fontSize: 8, fontFamily: "'JetBrains Mono', monospace",
    letterSpacing: 2, color: "#222",
  },
  scanLine: {
    position: "absolute", height: 1, width: "25%",
    background: "linear-gradient(90deg, transparent, rgba(var(--accent-rgb), 0.2), transparent)",
  },
  statusRight: { display: "flex", justifyContent: "flex-end" },
};
