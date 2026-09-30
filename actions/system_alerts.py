"""
F.R.I.D.A.Y. — actions/system_alerts.py
GPU/VRAM voice alerts, ported from Mark-L's actions/system_monitor.py.

Difference from the Mark-L original: that version polls NVML/WMI itself,
duplicating work ui/ws_server.py already does every 2s for the stats
panel. This version just checks the numbers FRIDAY has already collected
(ws_server._last_stats), so there's no second hardware poll running.
"""
import time

DEFAULT_THRESHOLDS = {
    "gpu_temp": 85.0,   # °C
    "gpu_util": 95.0,   # %
    "vram_pct": 90.0,   # % of VRAM used
}

_COOLDOWN = 300  # seconds between repeat alerts for the same metric


class GpuAlertMonitor:
    """Stateful — cooldown persists for the life of the process.
    Call check(stats) with FRIDAY's stats dict (cpu/ram/gpu/gpu_temp/
    vram_used/vram_total, same shape ws_server broadcasts to the UI).
    Returns an alert string to speak, or None."""

    def __init__(self, thresholds: dict | None = None):
        self.thresholds = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
        self._last_alert: dict[str, float] = {}

    def _can_alert(self, key: str) -> bool:
        return (time.monotonic() - self._last_alert.get(key, float("-inf"))) > _COOLDOWN

    def _record(self, key: str):
        self._last_alert[key] = time.monotonic()

    def check(self, stats: dict) -> str | None:
        if not stats:
            return None

        alerts: list[str] = []

        gpu_temp = stats.get("gpu_temp") or 0
        if gpu_temp >= self.thresholds["gpu_temp"] and self._can_alert("gpu_temp"):
            alerts.append(
                f"Boss, your GPU is running hot — {gpu_temp:.0f}°C. "
                "Might want to check cooling or ease off the load."
            )
            self._record("gpu_temp")

        gpu_util = stats.get("gpu") or 0
        if gpu_util >= self.thresholds["gpu_util"] and self._can_alert("gpu_util"):
            alerts.append(f"GPU load is maxed out at {gpu_util:.0f}%.")
            self._record("gpu_util")

        vram_used = stats.get("vram_used") or 0
        vram_total = stats.get("vram_total") or 0
        if vram_total > 0:
            vram_pct = (vram_used / vram_total) * 100
            if vram_pct >= self.thresholds["vram_pct"] and self._can_alert("vram_pct"):
                alerts.append(
                    f"VRAM is nearly full — {vram_used:.1f} of {vram_total:.1f} GB used."
                )
                self._record("vram_pct")

        return " ".join(alerts) if alerts else None
