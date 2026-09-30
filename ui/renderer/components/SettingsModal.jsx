/**
 * F.R.I.D.A.Y. — components/SettingsModal.jsx
 * Settings opens as a centered overlay on top of everything — like
 * Claude's own settings — rather than a draggable desktop window.
 * Clicking the backdrop closes it without touching anything else on
 * the desktop: no window positions, z-order, or in-progress work is
 * affected, since this renders as a sibling overlay, not part of the
 * windows/useWindowManager system at all.
 */

import React from "react";
import { motion, AnimatePresence } from "framer-motion";

export default function SettingsModal({ open, onClose, children, title = "SETTINGS", width = 480, maxHeight = "80vh" }) {
  return (
    <AnimatePresence>
      {open && (
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.15 }}
          style={styles.backdrop}
          onClick={onClose}
        >
          <motion.div
            initial={{ opacity: 0, scale: 0.96, y: 8 }}
            animate={{ opacity: 1, scale: 1, y: 0 }}
            exit={{ opacity: 0, scale: 0.96, y: 8 }}
            transition={{ duration: 0.15 }}
            style={{ ...styles.panel, width, maxHeight }}
            onClick={(e) => e.stopPropagation()}
          >
            <div style={styles.header}>
              <span style={styles.title}>{title}</span>
              <button style={styles.closeBtn} onClick={onClose} title="Close">✕</button>
            </div>
            <div style={styles.body}>{children}</div>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}

const styles = {
  backdrop: {
    position: "fixed",
    inset: 0,
    background: "rgba(0,0,0,0.6)",
    backdropFilter: "blur(3px)",
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    zIndex: 900,
  },
  panel: {
    maxWidth: "90vw",
    background: "var(--panel-bg-solid)",
    backdropFilter: "blur(var(--panel-blur)) saturate(var(--panel-saturate))",
    WebkitBackdropFilter: "blur(var(--panel-blur)) saturate(var(--panel-saturate))",
    border: "1px solid var(--panel-border)",
    borderRadius: "var(--panel-radius-lg)",
    overflow: "hidden",
    display: "flex",
    flexDirection: "column",
    boxShadow: "var(--panel-shadow), var(--panel-highlight)",
  },
  header: {
    display: "flex",
    alignItems: "center",
    justifyContent: "space-between",
    padding: "12px 16px",
    background: "rgba(var(--accent-rgb), 0.06)",
    borderBottom: "1px solid rgba(var(--accent-rgb), 0.15)",
    flexShrink: 0,
  },
  title: {
    fontSize: 11,
    letterSpacing: 3,
    color: "var(--accent)",
    fontFamily: "'JetBrains Mono', monospace",
    fontWeight: 700,
  },
  closeBtn: {
    background: "transparent",
    border: "none",
    color: "#737373",
    fontSize: 14,
    cursor: "pointer",
    lineHeight: 1,
    padding: 4,
  },
  body: {
    flex: 1,
    overflowY: "auto",
  },
};
