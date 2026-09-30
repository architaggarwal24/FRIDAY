/**
 * F.R.I.D.A.Y. — components/ScreenWatch.jsx
 *
 * Ambient "you've been staring at the same thing" detection.
 *
 * DISTINCT FROM memory/screen_awareness.py: that one is the on-demand
 * "what's on my screen" path, triggered when you ask. This is passive,
 * feeds the focus session, and never answers a question. Neither
 * replaces the other.
 *
 * COST SHAPE — the reason it's built this way:
 *   Every SAMPLE_INTERVAL_S a frame is drawn to a tiny offscreen canvas
 *   and diffed against the previous one. That comparison is entirely
 *   local and costs nothing but a few ms of CPU. Only when the screen
 *   has genuinely not changed for STUCK_THRESHOLD_S does ONE frame get
 *   sent anywhere — so normal work, where the screen changes
 *   constantly, never sends a single byte off the machine.
 *
 * WHOLE SCREEN, NOT A TAB:
 *   getDisplayMedia's picker lets the user share a single tab or
 *   window instead. That's useless here: a tab capture re-renders
 *   itself constantly (caret blink, hover states, its own animations),
 *   so it can't tell staring from scrolling and would either never
 *   fire or fire wrongly. Rather than silently producing garbage, this
 *   detects the surface type and says so.
 */

import React, { useEffect, useRef, useState, useCallback } from "react";

const SAMPLE_INTERVAL_S = 5;      // how often to grab + diff a frame
const STUCK_THRESHOLD_S = 60;     // unchanged this long -> one nudge
const SEND_COOLDOWN_S = 180;      // client-side mirror of the backend cooldown

// Downscale target for the diff. Small on purpose: at this size a real
// change (window switch, scroll, new content) still moves plenty of
// pixels, while cursor blink and antialiasing noise don't.
const DIFF_W = 64;
const DIFF_H = 36;

// Mean per-pixel luma delta above which the screen counts as "changed".
const CHANGE_THRESHOLD = 2.0;

export default function ScreenWatch({ enabled, sendCommand, onNotice }) {
  const videoRef = useRef(null);
  const streamRef = useRef(null);
  const diffCanvasRef = useRef(null);
  const fullCanvasRef = useRef(null);
  const prevPixelsRef = useRef(null);
  const unchangedSinceRef = useRef(null);
  const lastSentRef = useRef(0);
  const timerRef = useRef(null);
  const wasStuckRef = useRef(false);

  const [status, setStatus] = useState("off"); // off | running | wrong-surface | error

  const sample = useCallback(() => {
    const video = videoRef.current;
    if (!video || video.readyState < 2) return;

    const canvas = diffCanvasRef.current;
    const ctx = canvas.getContext("2d", { willReadFrequently: true });
    ctx.drawImage(video, 0, 0, DIFF_W, DIFF_H);
    const frame = ctx.getImageData(0, 0, DIFF_W, DIFF_H).data;

    const prev = prevPixelsRef.current;
    prevPixelsRef.current = new Uint8ClampedArray(frame);

    if (!prev) {
      unchangedSinceRef.current = Date.now();
      return;
    }

    let total = 0;
    for (let i = 0; i < frame.length; i += 4) {
      const a = 0.299 * frame[i] + 0.587 * frame[i + 1] + 0.114 * frame[i + 2];
      const b = 0.299 * prev[i] + 0.587 * prev[i + 1] + 0.114 * prev[i + 2];
      total += Math.abs(a - b);
    }
    const meanDelta = total / (frame.length / 4);

    const now = Date.now();
    if (meanDelta > CHANGE_THRESHOLD) {
      unchangedSinceRef.current = now;
      if (wasStuckRef.current) {
        wasStuckRef.current = false;
        sendCommand({ cmd: "screen_changed" });
      }
      return;
    }

    if (unchangedSinceRef.current === null) {
      unchangedSinceRef.current = now;
      return;
    }

    const stuckFor = (now - unchangedSinceRef.current) / 1000;
    const cooledDown = (now - lastSentRef.current) / 1000 >= SEND_COOLDOWN_S;
    if (stuckFor < STUCK_THRESHOLD_S || !cooledDown) return;

    // Only now does anything leave the machine: one frame, at a
    // sensible size, for one nudge.
    lastSentRef.current = now;
    wasStuckRef.current = true;
    const full = fullCanvasRef.current;
    const fctx = full.getContext("2d");
    full.width = Math.min(video.videoWidth || 1280, 1280);
    full.height = Math.round(full.width * ((video.videoHeight || 720) / (video.videoWidth || 1280)));
    fctx.drawImage(video, 0, 0, full.width, full.height);
    const dataUrl = full.toDataURL("image/jpeg", 0.6);
    sendCommand({
      cmd: "screen_stuck",
      image_b64: dataUrl.split(",")[1] || "",
      image_format: "jpeg",
    });
  }, [sendCommand]);

  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;

    async function start() {
      try {
        const stream = await navigator.mediaDevices.getDisplayMedia({
          // Ask the picker to preselect the whole screen. This is a
          // preference, not a guarantee — the user can still choose a
          // tab, which is why the check below exists.
          video: { displaySurface: "monitor" },
          audio: false,           // THE EAR LAW — never audio, here either.
          preferCurrentTab: false,
          selfBrowserSurface: "exclude",
          surfaceSwitching: "include",
        });
        if (cancelled) {
          stream.getTracks().forEach((t) => t.stop());
          return;
        }

        const track = stream.getVideoTracks()[0];
        const surface = (track.getSettings && track.getSettings().displaySurface) || "";

        if (surface && surface !== "monitor") {
          // Honest refusal rather than silently producing noise.
          stream.getTracks().forEach((t) => t.stop());
          setStatus("wrong-surface");
          if (onNotice) {
            onNotice(
              "That's just one " + (surface === "browser" ? "tab" : "window") +
              " — I can't tell staring from scrolling with that, since it keeps " +
              "redrawing itself. Pick \u201CEntire Screen\u201D and I'll watch properly."
            );
          }
          return;
        }

        streamRef.current = stream;
        if (videoRef.current) {
          videoRef.current.srcObject = stream;
          await videoRef.current.play();
        }
        // User stopped sharing from the browser's own control.
        track.addEventListener("ended", () => setStatus("off"));

        setStatus("running");
        timerRef.current = setInterval(sample, SAMPLE_INTERVAL_S * 1000);
      } catch (e) {
        if (!cancelled) setStatus("error");
      }
    }

    start();
    return () => {
      cancelled = true;
      if (timerRef.current) clearInterval(timerRef.current);
      if (streamRef.current) streamRef.current.getTracks().forEach((t) => t.stop());
      streamRef.current = null;
      prevPixelsRef.current = null;
      unchangedSinceRef.current = null;
      wasStuckRef.current = false;
      setStatus("off");
    };
  }, [enabled, sample, onNotice]);

  if (!enabled) return null;

  return (
    <div style={{ position: "absolute", width: 0, height: 0, overflow: "hidden" }}>
      <video ref={videoRef} muted playsInline style={{ width: 1, height: 1, opacity: 0 }} />
      <canvas ref={diffCanvasRef} width={DIFF_W} height={DIFF_H} />
      <canvas ref={fullCanvasRef} />
      {status === "wrong-surface" && null}
    </div>
  );
}
