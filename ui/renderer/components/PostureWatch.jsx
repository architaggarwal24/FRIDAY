/**
 * F.R.I.D.A.Y. — components/PostureWatch.jsx
 *
 * Notices posture without a single frame ever leaving the machine.
 *
 * THE DATA CONTRACT — the whole point of this file:
 *   - getUserMedia runs here, in the Electron renderer. The MediaStream
 *     is attached to a local <video> element and analysed by MediaPipe
 *     (WASM, bundled locally — no CDN, see webpack.config.js).
 *   - The ONLY thing that ever leaves this component is three booleans:
 *     present / head_down / slouched, sent over the existing WS as
 *     `posture_state`. No frame, no landmark array, no angle, no
 *     measurement, no timestamp of what you were doing.
 *   - The video element is never rendered visibly and the stream is
 *     never recorded, drawn to a transmitted canvas, or stored.
 *
 * THE EAR LAW — non-negotiable:
 *   This feature must never enable the microphone. getUserMedia is
 *   called with `audio: false` below, and Electron's main process
 *   additionally DENIES audioCapture at the permission layer (see
 *   ui/main.js), so a mic stream cannot be obtained here even by a
 *   future accident. Voice input is a completely separate path: it's
 *   captured in Python and gated behind the explicit `start_listen`
 *   WS command, which this file never sends. Nothing in this file
 *   starts, enables, or unmutes listening.
 *
 * Model loading: @mediapipe/tasks-vision needs its WASM blobs and a
 * .task model file. Both are copied to the dist root as plain files by
 * CopyWebpackPlugin (see webpack.config.js) rather than resolved
 * through webpack's module graph — the default MediaPipe examples
 * load these from a Google CDN, which this app's CSP correctly blocks.
 * The .task model has no npm distribution and must be placed by hand
 * once; see ui/README-assets.md.
 */

import React, { useEffect, useRef, useState, useCallback } from "react";

// Analysis rate. 8/sec is plenty to resolve POSTURE_BAD_SUSTAIN_MS
// (700ms) on the backend while leaving the GPU alone.
const ANALYSIS_HZ = 8;

// Landmark thresholds. Deliberately loose — this is "are you folded
// over", not a physiotherapy assessment, and a false nudge is far more
// annoying than a missed one.
const HEAD_DOWN_PITCH_RATIO = 0.62;   // nose sits low between eyes and chin
const SLOUCH_DROP_RATIO = 0.14;       // head center dropped vs. calibrated baseline
const CALIBRATION_FRAMES = 24;        // ~3s of good posture to set the baseline

export default function PostureWatch({ enabled, sendPosture, micEnabled, onNotice }) {
  const videoRef = useRef(null);
  const streamRef = useRef(null);
  const landmarkerRef = useRef(null);
  const rafRef = useRef(null);
  const baselineRef = useRef(null);
  const calibRef = useRef([]);
  const lastSentRef = useRef(null);
  const earNoticeRef = useRef(false);

  const [status, setStatus] = useState("off"); // off | starting | running | error
  const [error, setError] = useState("");

  // Sends booleans, and only when they've actually changed — no point
  // pushing an identical payload 8x/second.
  const publish = useCallback((present, headDown, slouched) => {
    const key = `${present}|${headDown}|${slouched}`;
    if (lastSentRef.current === key) return;
    lastSentRef.current = key;
    sendPosture({
      present: !!present,
      head_down: !!headDown,
      slouched: !!slouched,
      monitoring: true,
    });
  }, [sendPosture]);

  useEffect(() => {
    if (!enabled) return;

    let cancelled = false;

    async function start() {
      setStatus("starting");
      setError("");
      try {
        const { FilesetResolver, FaceLandmarker } = await import(
          /* webpackChunkName: "mediapipe-vision" */ "@mediapipe/tasks-vision"
        );

        // Local, bundled — NOT a CDN URL (the CSP blocks those, and a
        // blocked model load is exactly the silent "nothing happens"
        // failure this project has been bitten by before).
        //
        // Deliberately plain relative-path STRINGS, not webpack module
        // resolution (new URL(..., import.meta.url) or a dynamic
        // import()) — FilesetResolver/FaceLandmarker fetch() these
        // themselves at runtime; they are not JS modules for webpack to
        // resolve, and a directory (mediapipe-wasm/) was never a valid
        // target for that mechanism anyway. Both are placed at the dist
        // root by CopyWebpackPlugin (see webpack.config.js) rather than
        // going through the module graph, so a plain path relative to
        // index.html's own location — same folder, since Electron loads
        // this page via file://.../dist/index.html — is all that's
        // needed. See ui/README-assets.md for where these files come
        // from.
        const wasmBase = "./mediapipe-wasm";
        const modelUrl = "./face_landmarker.task";

        const fileset = await FilesetResolver.forVisionTasks(wasmBase);
        if (cancelled) return;

        landmarkerRef.current = await FaceLandmarker.createFromOptions(fileset, {
          baseOptions: { modelAssetPath: modelUrl, delegate: "GPU" },
          runningMode: "VIDEO",
          numFaces: 1,
        });
        if (cancelled) return;

        // audio:false — see THE EAR LAW in this file's header. This is
        // also enforced in ui/main.js at the permission layer.
        const stream = await navigator.mediaDevices.getUserMedia({
          video: { width: 320, height: 240, frameRate: 15 },
          audio: false,
        });
        if (cancelled) {
          stream.getTracks().forEach((t) => t.stop());
          return;
        }
        streamRef.current = stream;
        if (videoRef.current) {
          videoRef.current.srcObject = stream;
          await videoRef.current.play();
        }
        setStatus("running");
        loop();
      } catch (e) {
        if (cancelled) return;
        setStatus("error");
        setError(String(e && e.message ? e.message : e));
        sendPosture({ present: true, head_down: false, slouched: false, monitoring: false });
      }
    }

    let lastAt = 0;
    function loop() {
      rafRef.current = requestAnimationFrame(loop);
      const now = performance.now();
      if (now - lastAt < 1000 / ANALYSIS_HZ) return;
      lastAt = now;

      const video = videoRef.current;
      const lm = landmarkerRef.current;
      if (!video || !lm || video.readyState < 2) return;

      let result;
      try {
        result = lm.detectForVideo(video, now);
      } catch {
        return;
      }

      const faces = (result && result.faceLandmarks) || [];
      if (faces.length === 0) {
        publish(false, false, false);
        return;
      }

      // Everything below is computed from the landmark array in-memory
      // and immediately reduced to booleans. The landmarks themselves
      // never leave this function.
      const pts = faces[0];
      const nose = pts[1];
      const chin = pts[152];
      const leftEye = pts[33];
      const rightEye = pts[263];
      if (!nose || !chin || !leftEye || !rightEye) return;

      const eyeY = (leftEye.y + rightEye.y) / 2;
      const faceHeight = Math.abs(chin.y - eyeY) || 1e-6;
      const headDown = (nose.y - eyeY) / faceHeight > HEAD_DOWN_PITCH_RATIO;

      const headCenterY = eyeY;
      if (baselineRef.current === null) {
        // Calibrate against the first few seconds — "slouched" is
        // relative to how this person actually sits, not an absolute.
        calibRef.current.push(headCenterY);
        if (calibRef.current.length >= CALIBRATION_FRAMES) {
          const arr = [...calibRef.current].sort((a, b) => a - b);
          baselineRef.current = arr[Math.floor(arr.length / 2)];
        }
        publish(true, headDown, false);
        return;
      }

      const slouched = (headCenterY - baselineRef.current) > SLOUCH_DROP_RATIO;
      publish(true, headDown, slouched);
    }

    start();

    return () => {
      cancelled = true;
      if (rafRef.current) cancelAnimationFrame(rafRef.current);
      if (streamRef.current) streamRef.current.getTracks().forEach((t) => t.stop());
      streamRef.current = null;
      if (landmarkerRef.current && landmarkerRef.current.close) {
        try { landmarkerRef.current.close(); } catch { /* already gone */ }
      }
      landmarkerRef.current = null;
      baselineRef.current = null;
      calibRef.current = [];
      lastSentRef.current = null;
      setStatus("off");
      sendPosture({ present: true, head_down: false, slouched: false, monitoring: false });
    };
  }, [enabled, publish, sendPosture]);

  // THE EAR LAW, spoken half: if the posture watch is on while voice
  // input is off, say so ONCE rather than silently doing nothing about
  // it. This component cannot turn the mic on — it just tells the user
  // the mic is theirs to turn on.
  useEffect(() => {
    if (!enabled || micEnabled || earNoticeRef.current) return;
    earNoticeRef.current = true;
    if (onNotice) onNotice("My ears are off — tap the mic and just talk.");
  }, [enabled, micEnabled, onNotice]);

  if (!enabled) return null;

  return (
    <div style={styles.wrap}>
      {/* Never shown. Present only because MediaPipe needs a real
          playing <video> element to read frames from. */}
      <video ref={videoRef} style={styles.hiddenVideo} muted playsInline />
      {status === "error" && (
        <div style={styles.error} title={error}>
          camera unavailable
        </div>
      )}
    </div>
  );
}

const styles = {
  wrap: { position: "absolute", width: 0, height: 0, overflow: "hidden" },
  hiddenVideo: { position: "absolute", width: 1, height: 1, opacity: 0, pointerEvents: "none" },
  error: {
    position: "fixed", bottom: 48, right: 12,
    fontSize: 10, color: "#737373",
    fontFamily: "'JetBrains Mono', monospace",
  },
};
