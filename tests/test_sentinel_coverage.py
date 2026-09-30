"""
test_sentinel_coverage.py — regression test for Sentinel risk-classification
coverage across every tool that can reach the same dangerous action.

Specifically guards against: a dangerous action (shutdown/restart) being
reachable through more than one tool, where only SOME of those tools are
covered by sentinel.classify_risk()'s HIGH-risk checks. If a tool falls
through uncovered, it only gets that tool's own local confirmation (if any)
instead of Sentinel's cross-turn, code-enforced gate — which the model
cannot self-satisfy by just setting a "confirmed" argument on its own.

Found this way: computer_settings.py's ACTION_MAP includes "shutdown" and
"restart" (same OS-level effect as the dedicated power_control tool), but
classify_risk() only special-cased tool_name == "power_control" — so a
shutdown/restart requested through computer_settings only got
computer_settings.py's own same-turn `confirmed=yes` argument check, which
the model can set on its own without a real user ever confirming anything.
Fixed by routing both tool names through the same shutdown/restart check.

Run from the project root:
    python tests/test_sentinel_coverage.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sentinel import classify_risk, RiskLevel, start_turn  # noqa: E402

results = []


def record(name, ok, detail):
    results.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")


def check(tool_name: str, args: dict, expected: RiskLevel):
    start_turn()  # isolate from stacked-risk state left by any prior case
    got = classify_risk(tool_name, args)
    ok = got == expected
    record(
        f"classify_risk({tool_name!r}, {args})",
        ok,
        f"got {got.value}, expected {expected.value}",
    )


def test_shutdown_restart_covered_on_every_tool_that_can_reach_them():
    """Both tools that can trigger an OS shutdown/restart must be HIGH —
    not just the dedicated power_control tool."""
    check("power_control", {"action": "shutdown"}, RiskLevel.HIGH)
    check("power_control", {"action": "restart"}, RiskLevel.HIGH)
    check("computer_settings", {"action": "shutdown"}, RiskLevel.HIGH)
    check("computer_settings", {"action": "restart"}, RiskLevel.HIGH)
    check("computer_settings", {"action": "reboot"}, RiskLevel.HIGH)


def test_ordinary_actions_do_not_over_trigger():
    """The fix must not sweep unrelated computer_settings/power_control
    actions into HIGH — they should keep their pre-existing classification
    (ELEVATED: executes immediately, only counts toward stacked-risk)."""
    check("computer_settings", {"action": "volume_set", "value": 50}, RiskLevel.ELEVATED)
    check("computer_settings", {"action": "toggle_wifi"}, RiskLevel.ELEVATED)
    check("power_control", {"action": "lock"}, RiskLevel.ELEVATED)


if __name__ == "__main__":
    test_shutdown_restart_covered_on_every_tool_that_can_reach_them()
    test_ordinary_actions_do_not_over_trigger()

    print()
    print("=== SUMMARY ===")
    failed = [r for r in results if not r[1]]
    if failed:
        print(f"{len(failed)} FAILED / {len(results)} total")
        sys.exit(1)
    else:
        print("ALL PASS")
