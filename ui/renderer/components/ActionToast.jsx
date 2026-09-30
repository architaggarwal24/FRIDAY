/**
 * F.R.I.D.A.Y. — components/ActionToast.jsx
 * Stack of notification toasts that appear when actions complete or fail.
 */

import React, { useEffect } from "react";
import { motion, AnimatePresence } from "framer-motion";

const KIND_STYLES = {
  success: { color: "#10b981", border: "rgba(16,185,129,0.25)", icon: "✓" },
  error:   { color: "#ef4444", border: "rgba(239,68,68,0.25)",  icon: "✗" },
  warning: { color: "#e8935f", border: "rgba(232,147,95,0.25)", icon: "⚠" },
  info:    { color: "#06b6d4", border: "rgba(6,182,212,0.25)",  icon: "◆" },
};

export function Toast({ toast, onDismiss }) {
  const kind = KIND_STYLES[toast.kind] || KIND_STYLES.info;

  useEffect(() => {
    const timer = setTimeout(() => onDismiss(toast.id), 4000);
    return () => clearTimeout(timer);
  }, [toast.id]);

  return (
    <motion.div
      layout
      initial={{ opacity: 0, x: 40, scale: 0.95 }}
      animate={{ opacity: 1, x: 0,  scale: 1 }}
      exit={{    opacity: 0, x: 40, scale: 0.95 }}
      transition={{ duration: 0.2 }}
      style={{
        ...styles.toast,
        borderColor: kind.border,
      }}
      onClick={() => onDismiss(toast.id)}
    >
      <span style={{ color: kind.color, fontSize: 12, flexShrink: 0 }}>
        {kind.icon}
      </span>
      <span style={{ ...styles.text, color: kind.color }}>
        {toast.message}
      </span>
    </motion.div>
  );
}

export function ToastStack({ toasts, onDismiss }) {
  return (
    <div style={styles.stack}>
      <AnimatePresence>
        {toasts.map(t => (
          <Toast key={t.id} toast={t} onDismiss={onDismiss} />
        ))}
      </AnimatePresence>
    </div>
  );
}

const styles = {
  stack: {
    position: "fixed",
    bottom: 48,
    right: 20,
    display: "flex",
    flexDirection: "column-reverse",
    gap: 8,
    zIndex: 500,
    pointerEvents: "none",
  },
  toast: {
    display: "flex",
    alignItems: "center",
    gap: 8,
    padding: "8px 14px",
    background: "var(--panel-bg-solid)",
    border: "1px solid",
    borderRadius: "var(--radius-sm)",
    backdropFilter: "blur(var(--panel-blur)) saturate(var(--panel-saturate))",
    WebkitBackdropFilter: "blur(var(--panel-blur)) saturate(var(--panel-saturate))",
    boxShadow: "0 8px 24px rgba(0,0,0,0.4), var(--panel-highlight)",
    pointerEvents: "all",
    cursor: "pointer",
    maxWidth: 320,
  },
  text: {
    fontSize: 11,
    fontFamily: "'JetBrains Mono', monospace",
    letterSpacing: 0.5,
    lineHeight: 1.4,
  },
};
