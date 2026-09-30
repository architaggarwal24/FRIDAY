/**
 * F.R.I.D.A.Y. — components/Transcript.jsx
 * Live scrolling conversation log. Also serves as the compact preview in
 * the new centered layout (compact=true) — same bubble rendering and
 * same TextInputBox either way, just a shorter slice of the transcript
 * and a "view full" affordance instead of the entries count. Kept as one
 * component rather than a separate MiniTranscript so bubble styling and
 * the send pipeline can't drift out of sync between the two views.
 */

import React, { useEffect, useRef } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { STATE_COLORS } from "../hooks/useFriday";
import TextInputBox from "./TextInputBox";
import MarkdownMessage from "./MarkdownMessage";

function formatTime(ts) {
  return new Date(ts).toLocaleTimeString("en-IN", {
    hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
  });
}

// Last 2 FRIDAY messages + last 1 user message, restored to chronological
// order for reading. Counts are per-role, not "last 3 messages" — a burst
// of 3 user messages in a row shouldn't push both FRIDAY replies out.
function compactSlice(transcript) {
  let friday = 0, user = 0;
  const picked = [];
  for (let i = transcript.length - 1; i >= 0 && (friday < 2 || user < 1); i--) {
    const m = transcript[i];
    if (m.role === "user") {
      if (user >= 1) continue;
      user++;
    } else {
      if (friday >= 2) continue;
      friday++;
    }
    picked.push(m);
  }
  return picked.reverse();
}

export default function Transcript({ transcript, state, onSend, onSendFile, connected, compact = false, onExpand }) {
  const bottomRef = useRef(null);
  const shown = compact ? compactSlice(transcript) : transcript;
  const hiddenCount = compact ? Math.max(0, transcript.length - shown.length) : 0;

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: compact ? "auto" : "smooth" });
  }, [transcript, compact]);

  return (
    <div style={styles.container}>
      <div style={styles.header}>
        {compact ? (
          <button style={styles.expandBtn} onClick={onExpand} disabled={transcript.length === 0}>
            {hiddenCount > 0 ? `▲ ${hiddenCount} earlier — view full conversation` : "view full conversation"}
          </button>
        ) : (
          <span style={styles.count}>{transcript.length} entries</span>
        )}
      </div>

      <div style={styles.log}>
        <AnimatePresence initial={false}>
          {shown.length === 0 && (
            <motion.div
              key="empty"
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              style={styles.empty}
            >
              Awaiting input...
            </motion.div>
          )}

          {shown.map((msg, i) => (
            <motion.div
              key={i}
              initial={{ opacity: 0, x: msg.role === "user" ? -10 : 10 }}
              animate={{ opacity: 1, x: 0 }}
              transition={{ duration: 0.2 }}
              style={styles.message}
            >
              <div style={styles.msgHeader}>
                <span style={{
                  ...styles.role,
                  color: msg.role === "user" ? "#06b6d4" : "var(--accent)",
                }}>
                  {msg.role === "user" ? "YOU" : "F.R.I.D.A.Y."}
                </span>
                <span style={styles.time}>{formatTime(msg.ts)}</span>
              </div>
              <div style={{
                ...styles.text,
                borderLeft: `2px solid ${msg.role === "user" ? "#06b6d4" : "var(--accent)"}`,
              }}>
                <MarkdownMessage text={msg.text} role={msg.role} />
              </div>
            </motion.div>
          ))}
        </AnimatePresence>
        <div ref={bottomRef} />
      </div>

      {/* Typing indicator when thinking/speaking */}
      {(state === "thinking" || state === "speaking") && (
        <div style={styles.indicator}>
          <span style={{ color: STATE_COLORS[state], fontSize: 11 }}>
            {state === "thinking" ? "● PROCESSING..." : "● RESPONDING..."}
          </span>
        </div>
      )}

      {/* Type instead of speaking — same pipeline as voice, lives right
          under the log it's talking to, not floating at the app's edge */}
      <TextInputBox onSend={onSend} onSendFile={onSendFile} connected={connected} disabled={state === "listening"} />
    </div>
  );
}

const styles = {
  container: {
    display: "flex",
    flexDirection: "column",
    height: "100%",
    background: "rgba(14,16,19,0.4)", // sits inside the already-glassy Window chrome — stays light so blur isn't stacked/muddied
  },
  header: {
    display: "flex",
    justifyContent: "flex-end",
    alignItems: "center",
    padding: "10px 14px",
    borderBottom: "1px solid rgba(var(--accent-rgb), 0.1)",
  },
  count: {
    fontSize: 9,
    color: "#525252",
    fontFamily: "'JetBrains Mono', monospace",
  },
  expandBtn: {
    background: "transparent",
    border: "none",
    color: "var(--accent)",
    fontSize: 9,
    fontFamily: "'JetBrains Mono', monospace",
    letterSpacing: 0.5,
    cursor: "pointer",
    padding: 0,
    opacity: 0.85,
  },
  log: {
    flex: 1,
    overflowY: "auto",
    padding: "10px 14px",
    display: "flex",
    flexDirection: "column",
    gap: 10,
  },
  empty: {
    color: "#525252",
    fontSize: 11,
    fontFamily: "'JetBrains Mono', monospace",
    textAlign: "center",
    marginTop: 20,
    letterSpacing: 2,
  },
  message: {
    display: "flex",
    flexDirection: "column",
    gap: 4,
  },
  msgHeader: {
    display: "flex",
    justifyContent: "space-between",
    alignItems: "center",
  },
  role: {
    fontSize: 9,
    fontFamily: "'JetBrains Mono', monospace",
    letterSpacing: 2,
    fontWeight: 700,
  },
  time: {
    fontSize: 9,
    color: "#404040",
    fontFamily: "'JetBrains Mono', monospace",
  },
  text: {
    paddingLeft: 8,
  },
  indicator: {
    padding: "6px 14px",
    borderTop: "1px solid rgba(var(--accent-rgb), 0.08)",
  },
};
