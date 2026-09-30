/**
 * F.R.I.D.A.Y. — components/Orb.jsx
 * Pure Canvas2D orb — zero Three.js, zero React re-render overhead.
 * Runs entirely in requestAnimationFrame, silky 60fps.
 *
 * Visual features:
 * - Glowing pulsing core sphere
 * - Orbiting particle field (canvas arcs, fast)
 * - Waveform ring reacting to audioLevel
 * - Ripple shockwave on state change
 * - Breathing idle animation
 * - State-reactive colors
 */

import React, { useRef, useEffect, useCallback } from "react";
import { STATE_COLORS } from "../hooks/useFriday";

const TWO_PI = Math.PI * 2;
const PARTICLE_COUNT = 180;

function hexToRgb(hex) {
  const r = parseInt(hex.slice(1, 3), 16);
  const g = parseInt(hex.slice(3, 5), 16);
  const b = parseInt(hex.slice(5, 7), 16);
  return { r, g, b };
}

export default function Orb({ state, audioLevel }) {
  const canvasRef = useRef(null);
  const stateRef  = useRef(state);
  const audioRef  = useRef(audioLevel);
  const rafRef    = useRef(null);

  // Particle data — allocated once
  const particles = useRef([]);

  // Ripple state
  const ripple = useRef({ active: false, progress: 0, color: "#e8935f" });
  const prevState = useRef(state);

  // Init particles once
  useEffect(() => {
    particles.current = Array.from({ length: PARTICLE_COUNT }, () => ({
      angle:  Math.random() * TWO_PI,
      radius: 110 + Math.random() * 80,
      speed:  (0.003 + Math.random() * 0.007) * (Math.random() > 0.5 ? 1 : -1),
      size:   1 + Math.random() * 2,
      phase:  Math.random() * TWO_PI,
    }));
  }, []);

  // Trigger ripple on state change
  useEffect(() => {
    if (prevState.current !== state) {
      ripple.current = { active: true, progress: 0, color: STATE_COLORS[state] || "#e8935f" };
      prevState.current = state;
    }
    stateRef.current = state;
  }, [state]);

  useEffect(() => {
    audioRef.current = audioLevel;
  }, [audioLevel]);

  // Main render loop
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");

    let t = 0;

    function resize() {
      const rect = canvas.parentElement.getBoundingClientRect();
      canvas.width  = rect.width  * devicePixelRatio;
      canvas.height = rect.height * devicePixelRatio;
      canvas.style.width  = rect.width  + "px";
      canvas.style.height = rect.height + "px";
    }
    resize();
    const ro = new ResizeObserver(resize);
    ro.observe(canvas.parentElement);

    function draw() {
      const W = canvas.width;
      const H = canvas.height;
      const cx = W / 2;
      const cy = H / 2;
      const audio = audioRef.current;
      const st    = stateRef.current;
      const color = STATE_COLORS[st] || "#e8935f";
      const { r, g, b } = hexToRgb(color);
      const rgb = `${r},${g},${b}`;

      // Clear
      ctx.clearRect(0, 0, W, H);

      // ── Outer ambient glow ────────────────────────────
      const baseR = Math.min(W, H) * 0.22;
      const breathe = 1 + Math.sin(t * 1.1) * 0.03;
      const audioPulse = st === "idle" ? breathe : 1 + audio * 0.18 + Math.sin(t * 9) * audio * 0.06;
      const coreR = baseR * audioPulse;

      const outerGlow = ctx.createRadialGradient(cx, cy, coreR * 0.5, cx, cy, coreR * 2.2);
      outerGlow.addColorStop(0, `rgba(${rgb},0.06)`);
      outerGlow.addColorStop(1, `rgba(${rgb},0)`);
      ctx.fillStyle = outerGlow;
      ctx.fillRect(0, 0, W, H);

      // ── Waveform ring ─────────────────────────────────
      const BARS = 72;
      const ringR = coreR * 1.28;
      for (let i = 0; i < BARS; i++) {
        const angle = (i / BARS) * TWO_PI - Math.PI / 2;
        const wave = st === "idle"
          ? 3 + Math.sin(t * 1.5 + i * 0.5) * 2
          : 4 + audio * (30 + Math.abs(Math.sin(t * 7 + i * 0.9)) * 28);
        const x1 = cx + Math.cos(angle) * ringR;
        const y1 = cy + Math.sin(angle) * ringR;
        const x2 = cx + Math.cos(angle) * (ringR + wave);
        const y2 = cy + Math.sin(angle) * (ringR + wave);
        ctx.strokeStyle = `rgba(${rgb},${0.4 + audio * 0.5})`;
        ctx.lineWidth = 1.5;
        ctx.beginPath();
        ctx.moveTo(x1, y1);
        ctx.lineTo(x2, y2);
        ctx.stroke();
      }

      // ── Particles ─────────────────────────────────────
      const pulseR = coreR * (st === "idle" ? 1 : 1 + audio * 0.22);
      for (const p of particles.current) {
        p.angle += p.speed;
        const wobble = Math.sin(t * 2 + p.phase) * (4 + audio * 12);
        const pr = p.radius * (pulseR / baseR) + wobble;
        const px = cx + Math.cos(p.angle) * pr;
        const py = cy + Math.sin(p.angle) * pr;
        ctx.fillStyle = `rgba(${rgb},${0.5 + audio * 0.4})`;
        ctx.beginPath();
        ctx.arc(px, py, p.size, 0, TWO_PI);
        ctx.fill();
      }

      // ── Core sphere ───────────────────────────────────
      // Outer shell
      const shellGrad = ctx.createRadialGradient(cx - coreR * 0.2, cy - coreR * 0.2, 0, cx, cy, coreR);
      shellGrad.addColorStop(0, `rgba(${rgb},0.25)`);
      shellGrad.addColorStop(0.6, `rgba(${rgb},0.08)`);
      shellGrad.addColorStop(1, `rgba(${rgb},0.18)`);
      ctx.fillStyle = shellGrad;
      ctx.beginPath();
      ctx.arc(cx, cy, coreR, 0, TWO_PI);
      ctx.fill();

      // Ring lines (wireframe feel)
      ctx.strokeStyle = `rgba(${rgb},0.12)`;
      ctx.lineWidth = 0.8;
      for (let i = 0; i < 8; i++) {
        const a = (i / 8) * TWO_PI + t * 0.05;
        ctx.beginPath();
        ctx.ellipse(cx, cy, coreR, coreR * Math.abs(Math.cos(a * 0.5 + 0.3)), a, 0, TWO_PI);
        ctx.stroke();
      }

      // Inner bright core
      const innerR = coreR * (0.28 + audio * 0.08);
      const innerGrad = ctx.createRadialGradient(cx, cy, 0, cx, cy, innerR);
      innerGrad.addColorStop(0, `rgba(${rgb},1)`);
      innerGrad.addColorStop(0.4, `rgba(${rgb},0.8)`);
      innerGrad.addColorStop(1, `rgba(${rgb},0)`);
      ctx.fillStyle = innerGrad;
      ctx.beginPath();
      ctx.arc(cx, cy, innerR, 0, TWO_PI);
      ctx.fill();

      // ── Ripple shockwave ──────────────────────────────
      if (ripple.current.active) {
        const rp = ripple.current;
        rp.progress += 0.03;
        const rr = coreR * (1 + rp.progress * 2.5);
        const { r: rr2, g: rg2, b: rb2 } = hexToRgb(rp.color);
        ctx.strokeStyle = `rgba(${rr2},${rg2},${rb2},${Math.max(0, 0.7 - rp.progress * 0.7)})`;
        ctx.lineWidth = 2;
        ctx.beginPath();
        ctx.arc(cx, cy, rr, 0, TWO_PI);
        ctx.stroke();
        if (rp.progress >= 1) rp.active = false;
      }

      t += 0.016;
      rafRef.current = requestAnimationFrame(draw);
    }

    rafRef.current = requestAnimationFrame(draw);
    return () => {
      cancelAnimationFrame(rafRef.current);
      ro.disconnect();
    };
  }, []);

  return (
    <canvas
      ref={canvasRef}
      style={{ width: "100%", height: "100%", display: "block" }}
    />
  );
}