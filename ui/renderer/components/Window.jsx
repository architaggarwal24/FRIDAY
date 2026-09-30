/**
 * F.R.I.D.A.Y. — components/Window.jsx
 * Draggable/resizable window chrome. No external drag/resize library —
 * plain mouse events, kept dependency-free like the rest of the app.
 */
import React, { useCallback, useRef } from "react";

const MIN_W = 240;
const MIN_H = 160;

export default function Window({
  id, title, x, y, width, height, z,
  onClose, onFocus, onMove, onResize,
  bounds, children,
}) {
  // Refs so the document-level mousemove/mouseup listeners (added once per
  // drag/resize gesture) always see the latest values without having to
  // re-bind on every render.
  const stateRef = useRef({ x, y, width, height });
  stateRef.current = { x, y, width, height };

  const handleHeaderMouseDown = useCallback((e) => {
    if (e.button !== 0) return; // left-click only
    onFocus(id);
    const startX = e.clientX, startY = e.clientY;
    const { x: origX, y: origY, width: w, height: h } = stateRef.current;

    const onMouseMove = (ev) => {
      let nx = origX + (ev.clientX - startX);
      let ny = origY + (ev.clientY - startY);
      nx = Math.max(bounds.left, Math.min(nx, bounds.right - w));
      ny = Math.max(bounds.top, Math.min(ny, bounds.bottom - h));
      onMove(id, nx, ny);
    };
    const onMouseUp = () => {
      document.removeEventListener("mousemove", onMouseMove);
      document.removeEventListener("mouseup", onMouseUp);
    };
    document.addEventListener("mousemove", onMouseMove);
    document.addEventListener("mouseup", onMouseUp);
  }, [id, bounds, onFocus, onMove]);

  const handleResizeMouseDown = useCallback((e) => {
    if (e.button !== 0) return;
    e.stopPropagation();
    onFocus(id);
    const startX = e.clientX, startY = e.clientY;
    const { x: winX, y: winY, width: origW, height: origH } = stateRef.current;

    const onMouseMove = (ev) => {
      let nw = Math.max(MIN_W, origW + (ev.clientX - startX));
      let nh = Math.max(MIN_H, origH + (ev.clientY - startY));
      nw = Math.min(nw, bounds.right - winX);
      nh = Math.min(nh, bounds.bottom - winY);
      onResize(id, nw, nh);
    };
    const onMouseUp = () => {
      document.removeEventListener("mousemove", onMouseMove);
      document.removeEventListener("mouseup", onMouseUp);
    };
    document.addEventListener("mousemove", onMouseMove);
    document.addEventListener("mouseup", onMouseUp);
  }, [id, bounds, onFocus, onResize]);

  return (
    <div
      className="glass-sheen"
      onMouseDownCapture={() => onFocus(id)}
      style={{
        position: "absolute", left: x, top: y, width, height, zIndex: z,
        display: "flex", flexDirection: "column",
        pointerEvents: "auto", // parent (App.jsx's floatingLayer) is pointer-events:none
        background: "var(--panel-bg)",
        backdropFilter: "blur(var(--panel-blur)) saturate(var(--panel-saturate))",
        WebkitBackdropFilter: "blur(var(--panel-blur)) saturate(var(--panel-saturate))",
        border: "1px solid var(--panel-border)",
        borderRadius: "var(--panel-radius)", overflow: "hidden",
        boxShadow: "var(--panel-shadow), var(--panel-highlight)",
      }}
    >
      <div onMouseDown={handleHeaderMouseDown} style={styles.header}>
        <span style={styles.title}>{title}</span>
        <button
          onClick={(e) => { e.stopPropagation(); onClose(id); }}
          onMouseDown={(e) => e.stopPropagation()}
          style={styles.closeBtn}
          title="Close"
        >
          ✕
        </button>
      </div>

      <div style={styles.body}>{children}</div>

      <div onMouseDown={handleResizeMouseDown} style={styles.resizeHandle}>
        <svg width="14" height="14" viewBox="0 0 14 14">
          <path d="M12 2 L2 12 M12 7 L7 12" stroke="var(--accent)" strokeWidth="1.4" opacity="0.4" />
        </svg>
      </div>
    </div>
  );
}

const styles = {
  header: {
    height: 32, flexShrink: 0, display: "flex", alignItems: "center",
    justifyContent: "space-between", padding: "0 8px 0 14px",
    background: "var(--header-bg)",
    borderBottom: "1px solid var(--panel-border)",
    cursor: "grab", userSelect: "none",
  },
  title: {
    fontSize: 10, fontFamily: "'JetBrains Mono', monospace",
    letterSpacing: 2.5, color: "var(--accent)", fontWeight: 700,
  },
  closeBtn: {
    background: "transparent", border: "none", color: "#666",
    cursor: "pointer", fontSize: 12, padding: "4px 8px",
    borderRadius: 4, lineHeight: 1,
  },
  body: { flex: 1, minHeight: 0, minWidth: 0, overflow: "hidden" },
  resizeHandle: {
    position: "absolute", right: 0, bottom: 0, width: 16, height: 16,
    cursor: "nwse-resize", display: "flex", alignItems: "flex-end",
    justifyContent: "flex-end", padding: 1,
  },
};
