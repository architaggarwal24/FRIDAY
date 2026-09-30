/**
 * F.R.I.D.A.Y. — renderer/hooks/useFriday.js
 * Central WebSocket hook. Handles all event types from Python backend.
 */

import { useState, useEffect, useRef, useCallback } from "react";

const WS_URL = "ws://localhost:8765";
const RECONNECT_DELAY = 2000;

let _wsRef = null;
export function getWsRef() { return _wsRef; }

export function useFriday() {
  const [state, setState]           = useState("idle");
  const [transcript, setTranscript] = useState([]);
  const [intent, setIntent]         = useState(null);
  const [stats, setStats]           = useState({
    cpu: 0, ram: 0, ram_used: 0, ram_total: 0,
    gpu: 0, gpu_temp: 0, vram_used: 0, vram_total: 0,
  });
  const [connected, setConnected]   = useState(false);
  const [audioLevel, setAudioLevel] = useState(0);
  const [hwinfo, setHwinfo]         = useState({ cpu_name: "CPU", gpu_name: "GPU" });
  const [toasts, setToasts]         = useState([]);
  const [activities, setActivities] = useState([]);
  const [screenDesc, setScreenDesc] = useState("");
  const [confirmRequest, setConfirmRequest] = useState(null);
  const [settings, setSettings]     = useState(null); // null until first get_settings response
  const [watchlist, setWatchlist]   = useState(null); // null until first get_watchlist response — { monitors, reminders }
  const [memory, setMemory]         = useState(null); // null until first get_memory response — { memory, vault_path }
  const [graph, setGraph]           = useState(null); // null until first get_graph response — { nodes, links }
  const [memoryMatch, setMemoryMatch] = useState(null); // null until first memory_match push — { nodeIds, ts }; unprompted, pushed whenever a turn's memory retrieval actually used semantic search

  const wsRef         = useRef(null);
  const reconnectRef  = useRef(null);
  let   toastCounter  = useRef(0);

  const dismissToast = useCallback((id) => {
    setToasts(prev => prev.filter(t => t.id !== id));
  }, []);

  const respondConfirm = useCallback((id, confirmed) => {
    const ws = getWsRef();
    if (ws && ws.readyState === 1) {
      ws.send(JSON.stringify({
        cmd: confirmed ? "confirm_yes" : "confirm_no",
        id,
      }));
    }
    setConfirmRequest(null);
  }, []);

  const sendText = useCallback((text) => {
    const clean = (text || "").trim();
    if (!clean) return false;
    const ws = getWsRef();
    if (ws && ws.readyState === 1) {
      ws.send(JSON.stringify({ cmd: "text_input", text: clean }));
      return true;
    }
    return false;
  }, []);

  // Reads the File as base64 and ships it over the same WS connection —
  // no separate HTTP upload endpoint to stand up. Resolves once the
  // backend acks the save (event: "upload_saved"/"upload_error"), not
  // once the bytes are merely sent, so the caller can show a real
  // "saved" state instead of guessing from send() returning.
  const sendFile = useCallback((file, caption = "") => {
    return new Promise((resolve, reject) => {
      const ws = getWsRef();
      if (!ws || ws.readyState !== 1) {
        reject(new Error("Not connected"));
        return;
      }
      const reader = new FileReader();
      reader.onerror = () => reject(new Error("Couldn't read that file"));
      reader.onload = () => {
        // reader.result is "data:<mime>;base64,<data>" — only the part
        // after the comma is the actual base64 payload.
        const base64 = String(reader.result).split(",", 2)[1] || "";
        const filename = file.name || "upload";

        const onMessage = (ev) => {
          let data;
          try { data = JSON.parse(ev.data); } catch { return; }
          if (data.event === "upload_saved" && data.filename?.endsWith(filename)) {
            ws.removeEventListener("message", onMessage);
            resolve(data);
          } else if (data.event === "upload_error" && data.filename === filename) {
            ws.removeEventListener("message", onMessage);
            reject(new Error(data.message || "Upload failed"));
          }
        };
        ws.addEventListener("message", onMessage);

        ws.send(JSON.stringify({
          cmd: "upload_file",
          filename, mime_type: file.type || "application/octet-stream",
          data: base64, caption: (caption || "").trim(),
        }));
      };
      reader.readAsDataURL(file);
    });
  }, []);

  const requestSettings = useCallback(() => {
    const ws = getWsRef();
    if (ws && ws.readyState === 1) {
      ws.send(JSON.stringify({ cmd: "get_settings" }));
      return true;
    }
    return false;
  }, []);

  const requestWatchlist = useCallback(() => {
    const ws = getWsRef();
    if (ws && ws.readyState === 1) {
      ws.send(JSON.stringify({ cmd: "get_watchlist" }));
      return true;
    }
    return false;
  }, []);

  const requestMemory = useCallback(() => {
    const ws = getWsRef();
    if (ws && ws.readyState === 1) {
      ws.send(JSON.stringify({ cmd: "get_memory" }));
      return true;
    }
    return false;
  }, []);

  // Posture booleans from PostureWatch.jsx. Booleans only — see that
  // file's data contract. Never sends start_listen or anything else
  // that could enable the microphone.
  const sendPosture = useCallback((state) => {
    const ws = getWsRef();
    if (ws && ws.readyState === 1) {
      ws.send(JSON.stringify({
        cmd: "posture_state",
        present: !!state.present,
        head_down: !!state.head_down,
        slouched: !!state.slouched,
        monitoring: !!state.monitoring,
      }));
      return true;
    }
    return false;
  }, []);

  // Generic passthrough for feature components that own their own
  // command shapes (ScreenWatch's screen_stuck / screen_changed).
  const sendCommand = useCallback((msg) => {
    const ws = getWsRef();
    if (ws && ws.readyState === 1) {
      ws.send(JSON.stringify(msg));
      return true;
    }
    return false;
  }, []);

  const requestGraph = useCallback(() => {
    const ws = getWsRef();
    if (ws && ws.readyState === 1) {
      ws.send(JSON.stringify({ cmd: "get_graph" }));
      return true;
    }
    return false;
  }, []);

  const setLlmProvider = useCallback((provider) => {
    const ws = getWsRef();
    if (ws && ws.readyState === 1) {
      ws.send(JSON.stringify({ cmd: "set_llm_provider", provider }));
      return true;
    }
    return false;
  }, []);

  const setTtsProvider = useCallback((provider) => {
    const ws = getWsRef();
    if (ws && ws.readyState === 1) {
      ws.send(JSON.stringify({ cmd: "set_tts_provider", provider }));
      return true;
    }
    return false;
  }, []);

  // Either index may be omitted (undefined) to leave that side unchanged —
  // e.g. changing only the speaker without re-touching the mic selection.
  // Either name may be omitted (undefined) to leave that side unchanged —
  // e.g. changing only the speaker without re-touching the mic selection.
  // "" means system default.
  const setAudioDevices = useCallback(({ micName, speakerName } = {}) => {
    const ws = getWsRef();
    if (ws && ws.readyState === 1) {
      const msg = { cmd: "set_audio_devices" };
      if (micName !== undefined) msg.mic_name = micName;
      if (speakerName !== undefined) msg.speaker_name = speakerName;
      ws.send(JSON.stringify(msg));
      return true;
    }
    return false;
  }, []);

  const connect = useCallback(() => {
    try {
      const ws = new WebSocket(WS_URL);
      wsRef.current = ws;
      _wsRef = ws;

      ws.onopen = () => {
        const token = (typeof window !== "undefined" && window.friday && window.friday.wsAuthToken) || "";
        ws.send(JSON.stringify({ cmd: "auth", token }));
        setConnected(true);
        clearTimeout(reconnectRef.current);
      };

      ws.onclose = () => {
        setConnected(false);
        wsRef.current = null;
        _wsRef = null;
        reconnectRef.current = setTimeout(connect, RECONNECT_DELAY);
      };

      ws.onerror = () => ws.close();

      ws.onmessage = (e) => {
        try {
          const data = JSON.parse(e.data);
          handleEvent(data);
        } catch (err) {
          console.error("WS parse error:", err);
        }
      };
    } catch {
      reconnectRef.current = setTimeout(connect, RECONNECT_DELAY);
    }
  }, []);

  function handleEvent(data) {
    switch (data.event) {
      case "state":
        setState(data.value);
        break;

      case "transcript":
        setTranscript(prev => [
          ...prev.slice(-99),
          { role: data.role, text: data.text, ts: Date.now() },
        ]);
        break;

      case "intent":
        setIntent(data.value);
        break;

      case "hwinfo":
        setHwinfo({
          cpu_name: data.cpu_name || "CPU",
          gpu_name: data.gpu_name || "GPU",
        });
        break;

      case "audio_level":
        // Real mic/TTS amplitude from the backend (VAD frame RMS while
        // listening, playback chunk RMS while speaking).
        setAudioLevel(data.value ?? 0);
        break;

      case "stats":
        setStats({
          cpu:        data.cpu        ?? 0,
          ram:        data.ram        ?? 0,
          ram_used:   data.ram_used   ?? 0,
          ram_total:  data.ram_total  ?? 0,
          gpu:        data.gpu        ?? 0,
          gpu_temp:   data.gpu_temp   ?? 0,
          vram_used:  data.vram_used  ?? 0,
          vram_total: data.vram_total ?? 0,
        });
        break;

      case "toast":
        setToasts(prev => [
          ...prev.slice(-9),
          { id: `t-${++toastCounter.current}`, message: data.message, kind: data.kind || "info" },
        ]);
        break;

      case "activity":
        setActivities(prev => [
          ...prev.slice(-49),
          { text: data.text, icon: data.icon || "◆", ts: Date.now() },
        ]);
        break;

      case "confirm":
        setConfirmRequest({ id: data.id, message: data.message, detail: data.detail });
        break;

      case "screen":
        setScreenDesc(data.description || "");
        break;

      case "settings_data":
        setSettings({
          activeProvider: data.active_provider,
          availableProviders: data.available_providers || [],
          activeTtsProvider: data.active_tts_provider,
          availableTtsProviders: data.available_tts_providers || [],
          usage: data.usage || [],
          micDevices: data.mic_devices || [],
          speakerDevices: data.speaker_devices || [],
          activeMicDevice: data.active_mic_device,
          activeSpeakerDevice: data.active_speaker_device,
          routerTiers: data.router_tiers || {},
        });
        break;

      case "watchlist_data":
        // Arrives both as a direct reply to get_watchlist AND unprompted,
        // pushed by the backend after any add/remove — same handler
        // either way, which is what makes this "live" rather than only
        // refreshing on request.
        setWatchlist({
          monitors: data.monitors || [],
          reminders: data.reminders || [],
          openLoops: data.open_loops || [],
        });
        break;

      case "memory_data":
        setMemory({
          memory: data.memory || {},
          vaultPath: data.vault_path || "",
        });
        break;

      case "graph_data":
        setGraph({
          nodes: (data.graph && data.graph.nodes) || [],
          links: (data.graph && data.graph.links) || [],
        });
        break;

      case "memory_match":
        // Unprompted push from a turn's memory retrieval (see
        // memory/long_term.py's format_for_prompt()) — only fires when
        // semantic search actually matched something. The galaxy panel
        // (if open) flies to/highlights these node ids; if it's not
        // open, nothing is listening and this is a harmless no-op —
        // never triggers opening the panel itself.
        setMemoryMatch({ nodeIds: data.node_ids || [], ts: Date.now() });
        break;

      case "error":
        setState("error");
        setToasts(prev => [
          ...prev.slice(-9),
          { id: `t-${++toastCounter.current}`, message: data.message, kind: "error" },
        ]);
        break;

      case "ready":
        setConnected(true);
        // Memory/Watchlist panels are now always-mounted (fixed layout,
        // not toggle-opened windows), so their own mount-time useEffect
        // fires immediately on app start — which loses the race against
        // the WS connection actually being open, and since requestMemory/
        // requestWatchlist silently no-op when not connected with nothing
        // to make them retry, that used to mean permanently stuck on
        // "Loading…". Fetching once here, right as the connection
        // actually becomes ready, is the fix — the panels' own effects
        // still fire too, they just win the race now instead of losing it.
        requestMemory();
        requestWatchlist();
        break;
    }
  }

  // audioLevel now comes entirely from real "audio_level" WS events
  // (see handleEvent above) driven by mic RMS / TTS playback RMS on the
  // backend. Just make sure it settles to 0 if we drop back to idle/error
  // without the backend getting a chance to send a final 0 (e.g. on
  // disconnect mid-utterance).
  useEffect(() => {
    if (state === "idle" || state === "error") setAudioLevel(0);
  }, [state]);

  useEffect(() => {
    connect();
    return () => {
      clearTimeout(reconnectRef.current);
      wsRef.current?.close();
    };
  }, [connect]);

  return {
    state, transcript, intent, stats, connected, audioLevel, hwinfo,
    toasts, dismissToast,
    activities, screenDesc,
    confirmRequest, respondConfirm,
    sendText,
    sendFile,
    settings, requestSettings, setLlmProvider, setTtsProvider, setAudioDevices,
    watchlist, requestWatchlist,
    memory, requestMemory,
    graph, requestGraph,
    memoryMatch,
    sendPosture,
    sendCommand,
  };
}

export const STATE_COLORS = {
  idle:      "#e8935f",
  listening: "#06b6d4",
  thinking:  "#3b82f6",
  speaking:  "#10b981",
  error:     "#ef4444",
};

export const STATE_LABELS = {
  idle:      "STANDBY",
  listening: "LISTENING",
  thinking:  "PROCESSING",
  speaking:  "RESPONDING",
  error:     "ERROR",
};