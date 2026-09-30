/**
 * F.R.I.D.A.Y. — hooks/useTheme.js
 * Three themes for side-by-side comparison testing (console/liquid/minimal
 * — see theme.css for what each one actually is). Local, per-machine
 * preference — not something the backend needs to know about, so this is
 * plain localStorage rather than a WS round-trip, same reasoning as any
 * other purely-client display preference.
 */
import { useCallback, useEffect, useState } from "react";
import { STATE_COLORS } from "./useFriday";

const STORAGE_KEY = "friday.theme";
export const THEMES = [
  { id: "console", label: "Systems Console" },
  { id: "liquid",  label: "Liquid Glass" },
  { id: "minimal", label: "Minimal" },
];
const VALID_IDS = new Set(THEMES.map(t => t.id));
const DEFAULT_THEME = "console";

function readStored() {
  try {
    const v = localStorage.getItem(STORAGE_KEY);
    return VALID_IDS.has(v) ? v : DEFAULT_THEME;
  } catch {
    return DEFAULT_THEME;
  }
}

/**
 * @param {string} state - current FRIDAY state (idle/listening/thinking/
 *   speaking/error), so the liquid theme's panel tint can track it live.
 *   Themes that don't use --state-color just ignore this entirely — see
 *   theme.css, --state-color is only read under [data-theme="liquid"].
 */
export function useTheme(state) {
  const [theme, setThemeState] = useState(readStored);

  // Applies data-theme to <html> — every CSS var block in theme.css is
  // keyed off this attribute, so this one line is the entire "switch".
  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    try {
      localStorage.setItem(STORAGE_KEY, theme);
    } catch {
      // localStorage can throw in some sandboxed/private contexts —
      // theme just won't persist across restarts, still applies this session.
    }
  }, [theme]);

  // Keeps --state-color current regardless of which theme is active —
  // cheap (one custom-property write), and means switching TO liquid
  // mid-session immediately reflects whatever FRIDAY is doing right now
  // instead of waiting for the next state change.
  useEffect(() => {
    const color = STATE_COLORS[state] || STATE_COLORS.idle;
    document.documentElement.style.setProperty("--state-color", color);
  }, [state]);

  const setTheme = useCallback((id) => {
    if (VALID_IDS.has(id)) setThemeState(id);
  }, []);

  return { theme, setTheme, themes: THEMES };
}
