/**
 * F.R.I.D.A.Y. — hooks/useWindowManager.js
 * Minimal OS-style window state: which apps are open, where, what size,
 * and which is on top. No persistence across restarts yet (session-only) —
 * deliberately kept simple for this first pass.
 */
import { useState, useCallback, useRef, useLayoutEffect } from "react";

// Fixed default LAYOUT SHAPE — two columns, matching the arrangement
// you set up by hand. Left/center: COMM LOG (tall) with MEMORY docked
// directly beneath it, same width. Right: CLOCK (short) stacked above
// SYS MONITOR (tall) stacked above WATCHLIST (short). Settings is no
// longer part of this system at all — it's a modal overlay now (see
// App.jsx), not a positioned desktop window, so it never needs a slot
// here. The proportions are fixed; the actual pixel positions are
// computed from the real container size at layout time, not hardcoded —
// the desktop area's width varies with the window size (the orb panel
// next to it is 38% width, not a fixed pixel value), so a hardcoded
// absolute layout only ever fit one specific window size and clipped
// off the right column on anything narrower.
const MARGIN = 12;
const GAP = 10;

function computeLayout(containerWidth, containerHeight) {
  const w = Math.max(containerWidth, 320);
  const h = Math.max(containerHeight, 240);
  const usableW = w - MARGIN * 2;
  const usableH = h - MARGIN * 2;

  const leftW = Math.round(usableW * 0.62);
  const rightW = usableW - leftW - GAP;
  const rightX = MARGIN + leftW + GAP;

  const commLogH = Math.round(usableH * 0.64);
  const memoryH = usableH - commLogH - GAP;

  const clockH = Math.round(usableH * 0.14);
  const statsH = Math.round(usableH * 0.34);
  const activityH = Math.round(usableH * 0.26);
  const watchlistH = usableH - clockH - statsH - activityH - GAP * 3;

  return {
    transcript: { x: MARGIN, y: MARGIN, width: leftW, height: commLogH },
    memory:     { x: MARGIN, y: MARGIN + commLogH + GAP, width: leftW, height: memoryH },
    clock:      { x: rightX, y: MARGIN, width: rightW, height: clockH },
    stats:      { x: rightX, y: MARGIN + clockH + GAP, width: rightW, height: statsH },
    activity:   { x: rightX, y: MARGIN + clockH + GAP + statsH + GAP, width: rightW, height: activityH },
    watchlist:  { x: rightX, y: MARGIN + clockH + GAP + statsH + GAP + activityH + GAP, width: rightW, height: watchlistH },
  };
}

const FALLBACK_LAYOUT = { x: 120, y: 90, width: 420, height: 340 };
const KNOWN_IDS = new Set(["transcript", "memory", "clock", "stats", "activity", "watchlist"]);

function layoutFor(id, openCount, containerRef) {
  const el = containerRef?.current;
  const width = el?.clientWidth || 900;
  const height = el?.clientHeight || 700;

  if (KNOWN_IDS.has(id)) {
    return computeLayout(width, height)[id];
  }
  // Unknown future window type with no defined slot — cascade so it
  // doesn't stack exactly on top of another window.
  const offset = (openCount % 5) * 24;
  return { ...FALLBACK_LAYOUT, x: FALLBACK_LAYOUT.x + offset, y: FALLBACK_LAYOUT.y + offset };
}

export function useWindowManager(autoOpen = ["transcript"], containerRef) {
  const zCounter = useRef(autoOpen.length + 1);
  const [windows, setWindows] = useState({});

  // Measured on mount rather than during the initial render — refs
  // aren't attached to the DOM yet during useState's initializer, so the
  // container's real size isn't knowable until after the first commit.
  // useLayoutEffect runs synchronously before paint, so this doesn't
  // produce a visible flash of windows in the wrong place.
  useLayoutEffect(() => {
    const initial = {};
    autoOpen.forEach((id, i) => {
      initial[id] = { ...layoutFor(id, i, containerRef), z: i + 1 };
    });
    setWindows(initial);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const nextZ = () => (zCounter.current += 1);

  const focusWindow = useCallback((id) => {
    setWindows(prev => (prev[id] ? { ...prev, [id]: { ...prev[id], z: nextZ() } } : prev));
  }, []);

  const openWindow = useCallback((id) => {
    setWindows(prev => {
      if (prev[id]) {
        return { ...prev, [id]: { ...prev[id], z: nextZ() } };
      }
      return { ...prev, [id]: { ...layoutFor(id, Object.keys(prev).length, containerRef), z: nextZ() } };
    });
  }, [containerRef]);

  const closeWindow = useCallback((id) => {
    setWindows(prev => {
      if (!prev[id]) return prev;
      const next = { ...prev };
      delete next[id];
      return next;
    });
  }, []);

  const toggleWindow = useCallback((id) => {
    setWindows(prev => {
      if (prev[id]) {
        const next = { ...prev };
        delete next[id];
        return next;
      }
      return { ...prev, [id]: { ...layoutFor(id, Object.keys(prev).length, containerRef), z: nextZ() } };
    });
  }, [containerRef]);

  const moveWindow = useCallback((id, x, y) => {
    setWindows(prev => (prev[id] ? { ...prev, [id]: { ...prev[id], x, y } } : prev));
  }, []);

  const resizeWindow = useCallback((id, width, height) => {
    setWindows(prev => (prev[id] ? { ...prev, [id]: { ...prev[id], width, height } } : prev));
  }, []);

  return { windows, openWindow, closeWindow, toggleWindow, focusWindow, moveWindow, resizeWindow };
}
