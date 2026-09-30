"""
sentinel/core.py — confirmation gate for high-risk actions.

A proportionate port of friday_jarvis's Sentinel:
single-user assistant, so this doesn't need the full permission-level /
resource-grant / biometric-reauth machinery — just the core safety
property that made the source project's design worth copying: a
destructive action never executes on the first ask. It gets held, the
user is told exactly what it would do, and it only proceeds on an
explicit "yes" on the NEXT turn.

This exists because "shut down the PC" fired instantly and unconfirmed
off a single ambiguous word — the same class of problem Sentinel's
denylist/confirmation design exists to prevent.
"""

from __future__ import annotations

import os
import platform
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional


class RiskLevel(str, Enum):
    LOW = "low"            # executes immediately, same as any other tool call
    ELEVATED = "elevated"  # executes immediately alone, but counts toward stacked-risk escalation within a turn
    HIGH = "high"          # held for explicit confirmation before executing


# ── System-critical directory denylist ────────────────────────────────────
# Code-enforced, not prompt-level: these paths are checked directly against
# the actual argument a write/delete/rename/move/copy call would use,
# regardless of what any prompt says. Resolved and prefix-checked, not
# substring-matched, so a user's own folder that happens to be *named*
# something like "Program Files Backup" under their own home directory is
# never mistakenly caught by this.
#
# Uses ntpath/posixpath (pure lexical path-string modules) rather than
# pathlib.Path/WindowsPath: WindowsPath refuses to instantiate on a non-
# Windows host at all, which would make this both untestable outside
# Windows and dependent on real filesystem resolution for paths that may
# not exist yet (e.g. a create_file target). ntpath/posixpath do the same
# separator/case/".."-normalization rules as their platform's real paths,
# purely as string manipulation, with no filesystem access and no host-OS
# restriction — importable and correct on any platform.
import ntpath
import posixpath


def _target_path_module():
    return ntpath if platform.system() == "Windows" else posixpath


def _system_critical_dirs() -> list[str]:
    system = platform.system()
    pm = _target_path_module()
    candidates: list[str] = []

    if system == "Windows":
        candidates.append(os.environ.get("WINDIR", r"C:\Windows"))
        for var in ("PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMDATA"):
            v = os.environ.get(var)
            if v:
                candidates.append(v)
        candidates += [
            r"C:\Windows", r"C:\Program Files",
            r"C:\Program Files (x86)", r"C:\ProgramData",
        ]
    elif system == "Darwin":
        candidates += [
            "/System", "/Library", "/Applications",
            "/usr", "/bin", "/sbin", "/etc",
        ]
    else:  # Linux and anything else
        candidates += [
            "/etc", "/usr", "/bin", "/sbin",
            "/boot", "/root", "/lib", "/sys", "/proc",
        ]

    seen, out = set(), []
    for c in candidates:
        norm = pm.normcase(pm.normpath(c))
        if norm in seen:
            continue
        seen.add(norm)
        out.append(norm)
    return out


def _is_under(child_norm: str, parent_norm: str, pm) -> bool:
    if child_norm == parent_norm:
        return True
    return child_norm.startswith(parent_norm.rstrip(pm.sep) + pm.sep)


def _is_system_critical_path(path_str: str, home: Optional[str] = None) -> bool:
    """True if path_str resolves to somewhere under a system-critical
    directory. Anything under the user's own home directory is always
    allowed regardless — the profile directory carve-out wins over the
    denylist, even if a name superficially resembles a protected one.

    `home` defaults to the real Path.home() and only exists as a
    parameter so this can be exercised against a simulated platform in
    tests without needing to actually run on that OS."""
    if not path_str:
        return False
    pm = _target_path_module()
    if home is None:
        home = str(Path.home())

    try:
        p = path_str
        if not pm.isabs(p):
            p = pm.join(home, p)
        norm = pm.normcase(pm.normpath(p))
    except Exception:
        return False

    home_norm = pm.normcase(pm.normpath(home))
    if _is_under(norm, home_norm, pm):
        return False

    for critical in _system_critical_dirs():
        if _is_under(norm, critical, pm):
            return True
    return False


# Which tool+action combinations actually write to a filesystem path, and
# which argument(s) hold the real target. None as the action key means the
# rule applies regardless of the tool's action value.
_WRITE_PATH_ARGS: dict[str, dict] = {
    "file_controller": {
        "create_file":   ["path"],
        "write":         ["path"],
        "create_folder": ["path"],
        "delete":        ["path"],
        "rename":        ["path"],
        "move":          ["path", "destination"],
        "copy":          ["path", "destination"],
    },
    "code":              {None: ["path"]},
    "screenshot":        {None: ["save_path"]},
    "computer_control":  {"screenshot": ["path"]},
}


def _write_target_paths(tool_name: str, args: dict) -> list[str]:
    rules = _WRITE_PATH_ARGS.get(tool_name)
    if not rules:
        return []
    action = (args.get("action") or "").lower() or None
    keys = rules.get(action) if action in rules else rules.get(None, [])
    if not keys:
        return []
    return [args[k] for k in keys if isinstance(args.get(k), str) and args[k].strip()]


# ── Privilege escalation denylist ─────────────────────────────────────────
# No tool in this codebase currently has an explicit "run as admin"
# capability, but the code tool can write and run arbitrary generated
# scripts — this catches an attempt to route through that (or any other
# free-text field) to elevate, rather than waiting for a specific handler
# to grow the capability first.
_PRIVESC_PATTERN = re.compile(
    r"\b("
    r"run\s*as\s*admin(?:istrator)?|"      # "run as admin(istrator)" — verb directly adjacent
    r"as\s+admin(?:istrator)?\b|"          # "...as admin(istrator)" — any verb/words before it
    r"runas|"
    r"elevat(?:e|ed|es|ion)|"
    r"admin(?:istrator)?\s*(?:privileges?|rights?|access|permissions?)|"
    r"grant\s*admin|"
    r"bypass\s*uac|"
    r"disable\s*uac|"
    r"sudo"
    r")\b",
    re.IGNORECASE,
)


def _mentions_privilege_escalation(args: dict) -> bool:
    for key in ("task", "description", "text", "query"):
        val = args.get(key)
        if isinstance(val, str) and _PRIVESC_PATTERN.search(val):
            return True
    return False


# ── Stacked-risk escalation ────────────────────────────────────────────────
# Actions that are real state changes but not independently dangerous
# enough to gate alone. Chaining enough of these together in one turn is
# treated as suspicious even though no single one would trigger a gate —
# the same way a person doing several small consequential things back to
# back deserves a second look, even if each one alone was fine.
_ELEVATED_TOOLS_ACTIONS: dict[str, Optional[set]] = {
    "file_controller":   {"create_file", "write", "create_folder", "rename", "move", "copy"},
    "desktop_control":   {"wallpaper", "wallpaper_url", "organize", "clean"},
    "code":              None,   # any action
    "computer_settings": None,
    "computer_control":  {"click", "double_click", "right_click", "drag",
                           "hotkey", "press", "type", "smart_type", "paste"},
    "open_app":          None,
    "spotify_control":   None,
    "power_control":     {"lock", "sleep", "hibernate"},  # shutdown/restart are already HIGH on their own
    "send_message":      None,
    "browser_control":   None,
    "calendar":          {"create", "add", "set", "schedule"},  # delete is HIGH on its own, see classify_risk
    "reminder":          {"add", "set"},  # can redirect to a real calendar event — see handle_reminder
    "gmail":             {"draft", "draft_reply"},  # send is HIGH on its own, see classify_risk
}

# 3+ elevated actions in one turn, spanning 2+ distinct tools, triggers
# escalation. Raw count alone isn't used — creating five files in a row is
# repetitive, not "stacked risk"; the thing worth flagging is a *mix* of
# different consequential actions chained together.
_STACK_COUNT_THRESHOLD = 3
_STACK_TOOL_DIVERSITY_THRESHOLD = 2


def _is_elevated(tool_name: str, args: dict) -> bool:
    if tool_name not in _ELEVATED_TOOLS_ACTIONS:
        return False
    allowed_actions = _ELEVATED_TOOLS_ACTIONS[tool_name]
    if allowed_actions is None:
        return True
    return (args.get("action") or "").lower() in allowed_actions


@dataclass
class _TurnState:
    elevated: list = field(default_factory=list)  # [(tool_name, args), ...] this turn


_turn_state = _TurnState()


def start_turn() -> None:
    """Resets the per-turn stacked-risk tracker. Called once at the top of
    stream_response() — the single entry point shared by every provider —
    so risk accumulates within a turn but never carries across turns."""
    global _turn_state
    _turn_state = _TurnState()


# Same word-matching handle_power() itself uses to parse a raw action out
# of natural-language text — args["action"] isn't guaranteed to already be
# a clean "shutdown"/"restart" keyword by the time it reaches here, and
# classification needs to agree with what the handler will actually do.
_SHUTDOWN_WORDS = ("shut down", "shutdown", "turn off", "power off")
_RESTART_WORDS = ("restart", "reboot", "restarting")


def classify_risk(tool_name: str, args: dict) -> RiskLevel:
    # power_control is the dedicated tool for this; computer_settings can
    # also reach shutdown/restart (its own ACTION_MAP includes them) via a
    # weaker same-turn `confirmed=yes` argument the model can set on its
    # own — routing both through the same check here means shutdown/restart
    # always gets Sentinel's real cross-turn confirmation, regardless of
    # which tool the model happened to pick.
    if tool_name in ("power_control", "computer_settings"):
        action = (args.get("action") or "").lower()
        if any(w in action for w in _SHUTDOWN_WORDS) or any(w in action for w in _RESTART_WORDS):
            return RiskLevel.HIGH

    if tool_name == "file_controller":
        action = (args.get("action") or "").lower()
        if action == "delete":
            return RiskLevel.HIGH

    if tool_name == "calendar":
        action = (args.get("action") or "").lower()
        if action in ("delete", "remove", "cancel"):
            return RiskLevel.HIGH

    if tool_name == "gmail":
        action = (args.get("action") or "").lower()
        if action in ("send", "reply"):
            return RiskLevel.HIGH

    # Code-enforced denylist — checked regardless of tool or how the
    # request was phrased, not something a prompt tells the model to
    # respect on its own.
    for path_val in _write_target_paths(tool_name, args):
        if _is_system_critical_path(path_val):
            return RiskLevel.HIGH

    if _mentions_privilege_escalation(args):
        return RiskLevel.HIGH

    # Stacked-risk escalation
    if _is_elevated(tool_name, args):
        _turn_state.elevated.append((tool_name, args))
        if len(_turn_state.elevated) >= _STACK_COUNT_THRESHOLD:
            distinct_tools = {t for t, _ in _turn_state.elevated}
            if len(distinct_tools) >= _STACK_TOOL_DIVERSITY_THRESHOLD:
                return RiskLevel.HIGH
        return RiskLevel.ELEVATED

    return RiskLevel.LOW


def confirmation_prompt(tool_name: str, args: dict) -> str:
    """Directive text handed back as the tool's own "result" — the model
    sees this in its follow-up round and relays it, rather than claiming
    the action already happened. Explicit enough that a weaker model
    can't easily paraphrase its way into overclaiming."""
    if tool_name in ("power_control", "computer_settings"):
        action = (args.get("action") or "").lower()
        verb = "restart" if any(w in action for w in _RESTART_WORDS) else "shut down"
        return (
            f"[CONFIRMATION REQUIRED — do NOT say this is done, it has NOT run yet. "
            f"Tell the user this will {verb} the computer completely, and ask them to "
            f"say 'yes' to confirm or 'no' to cancel. It will only run if they confirm.]"
        )
    if tool_name == "file_controller" and (args.get("action") or "").lower() == "delete":
        path = args.get("path", "the file")
        return (
            f"[CONFIRMATION REQUIRED — do NOT say this is done, it has NOT run yet. "
            f"Tell the user this will permanently delete {path}, and ask them to "
            f"say 'yes' to confirm or 'no' to cancel. It will only run if they confirm.]"
        )

    critical_paths = [p for p in _write_target_paths(tool_name, args) if _is_system_critical_path(p)]
    if critical_paths:
        return (
            f"[CONFIRMATION REQUIRED — do NOT say this is done, it has NOT run yet. "
            f"This would write to {critical_paths[0]}, a system-critical location, not "
            f"somewhere in the user's own files. Tell the user exactly what this would touch "
            f"and ask them to say 'yes' to confirm or 'no' to cancel. It will only run if they confirm.]"
        )

    if _mentions_privilege_escalation(args):
        return (
            f"[CONFIRMATION REQUIRED — do NOT say this is done, it has NOT run yet. "
            f"This request involves elevated/administrator privileges. Tell the user exactly "
            f"what would run with elevated rights and ask them to say 'yes' to confirm or "
            f"'no' to cancel. It will only run if they confirm.]"
        )

    if len(_turn_state.elevated) >= _STACK_COUNT_THRESHOLD:
        prior = ", ".join(f"{t}" for t, _ in _turn_state.elevated[:-1])
        return (
            f"[CONFIRMATION REQUIRED — do NOT say this is done, it has NOT run yet. "
            f"This is several different consequential actions chained in one request "
            f"(already did: {prior}; now also: {tool_name}) — none alone needed confirming, "
            f"but the combination does. Tell the user what the full set of actions is and ask "
            f"them to say 'yes' to confirm the rest or 'no' to stop here. It will only "
            f"continue if they confirm.]"
        )

    return "[CONFIRMATION REQUIRED — ask the user to confirm before proceeding.]"


@dataclass
class _PendingAction:
    tool: str
    args: dict
    created_at: float = field(default_factory=time.time)


_PENDING_TTL_SECONDS = 120  # a confirmation more than 2 minutes stale is treated as expired, not silently actioned
_pending: Optional[_PendingAction] = None


def set_pending(tool_name: str, args: dict) -> None:
    global _pending
    _pending = _PendingAction(tool=tool_name, args=args)


def get_pending() -> Optional[_PendingAction]:
    global _pending
    if _pending is not None and (time.time() - _pending.created_at) > _PENDING_TTL_SECONDS:
        _pending = None
    return _pending


def clear_pending() -> None:
    global _pending
    _pending = None


_CONFIRM_PHRASES = {
    "yes", "yeah", "yep", "yup", "confirm", "confirmed", "do it",
    "go ahead", "proceed", "sure", "yes do it", "please do", "okay", "ok",
}
_CANCEL_PHRASES = {
    "no", "nope", "nah", "cancel", "nevermind", "never mind", "stop",
    "don't", "dont", "abort",
}


def is_confirmation(text: str) -> Optional[bool]:
    """True = affirmative, False = negative, None = neither — an
    unrelated message while a confirmation is pending, which the caller
    should treat as an implicit cancel rather than silently discarding."""
    t = text.strip().lower().rstrip(".!?")
    if t in _CONFIRM_PHRASES:
        return True
    if t in _CANCEL_PHRASES:
        return False
    return None
