/**
 * F.R.I.D.A.Y. — components/ConfirmDialog.jsx
 * Appears when FRIDAY wants to run a destructive action.
 * Blocks execution until user clicks Yes or No.
 */

import React from "react";
import { motion, AnimatePresence } from "framer-motion";

export default function ConfirmDialog({ request, onRespond }) {
  if (!request) return null;

  return (
    <AnimatePresence>
      <motion.div
        key={request.id}
        initial={{ opacity: 0, scale: 0.92 }}
        animate={{ opacity: 1, scale: 1 }}
        exit={{ opacity: 0, scale: 0.92 }}
        transition={{ duration: 0.15 }}
        style={styles.overlay}
      >
        <div style={styles.dialog}>
          {/* Warning icon */}
          <div style={styles.icon}>⚠</div>

          {/* Message */}
          <div style={styles.message}>{request.message}</div>
          {request.detail && (
            <div style={styles.detail}>{request.detail}</div>
          )}

          {/* Buttons */}
          <div style={styles.buttons}>
            <button
              style={{ ...styles.btn, ...styles.btnNo }}
              onClick={() => onRespond(request.id, false)}
            >
              CANCEL
            </button>
            <button
              style={{ ...styles.btn, ...styles.btnYes }}
              onClick={() => onRespond(request.id, true)}
            >
              CONFIRM
            </button>
          </div>
        </div>
      </motion.div>
    </AnimatePresence>
  );
}

const styles = {
  overlay: {
    position: "fixed",
    inset: 0,
    background: "rgba(0,0,0,0.7)",
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    zIndex: 1000,
    backdropFilter: "blur(4px)",
  },
  dialog: {
    background: "var(--panel-bg-solid)",
    backdropFilter: "blur(var(--panel-blur)) saturate(var(--panel-saturate))",
    WebkitBackdropFilter: "blur(var(--panel-blur)) saturate(var(--panel-saturate))",
    border: "1px solid var(--panel-border)",
    borderRadius: "var(--panel-radius-lg)",
    padding: "28px 32px",
    maxWidth: 420,
    width: "90%",
    display: "flex",
    flexDirection: "column",
    alignItems: "center",
    gap: 16,
    // NOTE: this used to be two separate boxShadow keys in the same object
    // — the second silently overwrote the first, so the inset highlight
    // never actually rendered. Merged into one.
    boxShadow: "0 20px 60px rgba(0,0,0,0.6), 0 0 60px rgba(var(--accent-rgb), 0.1), var(--panel-highlight)",
  },
  icon: {
    fontSize: 32,
    color: "var(--accent)",
  },
  message: {
    fontSize: 14,
    color: "#f5f5f5",
    textAlign: "center",
    fontFamily: "'JetBrains Mono', monospace",
    letterSpacing: 0.5,
    lineHeight: 1.5,
  },
  detail: {
    fontSize: 11,
    color: "#737373",
    textAlign: "center",
    fontFamily: "'JetBrains Mono', monospace",
  },
  buttons: {
    display: "flex",
    gap: 12,
    marginTop: 8,
    width: "100%",
  },
  btn: {
    flex: 1,
    padding: "10px 0",
    borderRadius: 8,
    fontSize: 11,
    fontFamily: "'JetBrains Mono', monospace",
    letterSpacing: 2,
    fontWeight: 700,
    cursor: "pointer",
    border: "1px solid",
    transition: "all 0.15s ease",
  },
  btnNo: {
    background: "transparent",
    borderColor: "rgba(255,255,255,0.1)",
    color: "#737373",
  },
  btnYes: {
    background: "rgba(var(--accent-rgb), 0.15)",
    borderColor: "rgba(var(--accent-rgb), 0.4)",
    color: "var(--accent)",
  },
};
