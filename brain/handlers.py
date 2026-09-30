"""
F.R.I.D.A.Y. — brain/handlers.py
All action handlers. Each returns a string result spoken back by FRIDAY.
Primary platform: Windows 11. macOS/Linux best-effort.
"""

import asyncio
import datetime
import json
import logging
import os
import platform
import re
import subprocess
import webbrowser
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)
_OS = platform.system()

# ── Shared quick LLM call (used by memory handlers) ──────────────────────────
# Uses whatever provider is active in config - never hard-codes Gemini.

async def _quick_llm(prompt: str, max_tokens: int = 300) -> str:
    """
    One-shot LLM call that respects the active provider (Ollama/Gemini).
    Returns the full response as a string. Does NOT stream, does NOT touch memory.
    Used for internal tasks like memory extraction and recall formatting.
    """
    from config import config as _cfg
    from brain.llm import get_active_provider

    provider = get_active_provider()

    # ── Ollama ──────────────────────────────────────────────────────────────
    if provider == "ollama":
        try:
            import ollama as ol
            loop = asyncio.get_running_loop()
            resp = await loop.run_in_executor(
                None,
                lambda: ol.chat(
                    model=_cfg.brain.ollama_model,
                    messages=[{"role": "user", "content": prompt}],
                    stream=False,
                    options={"temperature": 0.1, "num_predict": max_tokens},
                )
            )
            return (resp["message"]["content"] if isinstance(resp, dict)
                    else resp.message.content).strip()
        except Exception as e:
            logger.warning(f"[_quick_llm] Ollama failed: {e}")

    # ── Gemini (only if installed) ──────────────────────────────────────────
    if _cfg.brain.gemini_api_key:
        try:
            import google.genai as genai
            _genai_client = genai.Client(api_key=_cfg.brain.gemini_api_key)
            _genai_model = _cfg.brain.gemini_model
            loop = asyncio.get_running_loop()
            resp = await loop.run_in_executor(None, lambda: _genai_client.models.generate_content(model=_genai_model, contents=prompt))
            return resp.text.strip()
        except Exception as e:
            logger.warning(f"[_quick_llm] Gemini failed: {e}")

    raise RuntimeError("No LLM available for _quick_llm")

# ── Helpers ──────────────────────────────────────────────────────────────────

def _resolve_path(path_str: str) -> Path:
    """Resolves aliases like 'desktop', 'downloads' to real paths."""
    # All paths derived from home dynamically — no hardcoded user paths
    home = Path.home()
    # Scan for common project folder names
    project_path = next(
        (home.parent / d for d in ["Projects", "Dev", "Code", "Work"]
         if (home.parent / d).exists()),
        home / "Projects"  # fallback: ~/Projects
    )
    aliases = {
        "desktop":   home / "Desktop",
        "downloads": home / "Downloads",
        "documents": home / "Documents",
        "home":      home,
        "projects":  project_path,
    }
    lower = (path_str or "").lower().strip()
    if lower in aliases:
        return aliases[lower]
    p = Path(path_str)
    if not p.is_absolute():
        p = Path.home() / p
    return p


# ── App Control ──────────────────────────────────────────────────────────────

APP_MAP = {
    "chrome":      "chrome",
    "firefox":     "firefox",
    "edge":        "msedge",
    "brave":       "brave",
    "discord":     "discord",
    "spotify":     "spotify",
    "vscode":      "code",
    "vs code":     "code",
    "terminal":    "cmd",
    "notepad":     "notepad",
    "explorer":    "explorer",
    "calculator":  "calc",
    "steam":       "steam",
    "obs":         "obs64",
    "wemod":       "WeMod",  # resolved dynamically via PATH or Windows search
    "lm studio":   "lmstudio",
}


async def handle_open_app(app_name: str) -> str:
    """
    Opens an app. Two tiers:
    1. Real discovery (actions/app_discovery.py) — Start Menu shortcuts +
       packaged/UWP apps, fuzzy-matched against what the user said. This
       is what most requests should resolve through now.
    2. The old brute-force fallback (try 4 shell strategies and hope) —
       kept for anything not in the Start Menu index: portable apps,
       PATH-only tools, things installed somewhere unusual.
    """
    name_lower = app_name.lower().strip()

    if _OS == "Windows":
        try:
            from actions.app_discovery import fuzzy_resolve
            match = fuzzy_resolve(app_name)
            if match:
                try:
                    if match.source == "packaged":
                        subprocess.Popen(
                            ["explorer.exe", match.target],
                            creationflags=subprocess.CREATE_NO_WINDOW,
                        )
                    else:
                        os.startfile(match.target)
                    label = match.name if match.name.lower() != name_lower else app_name
                    return f"Opened {label}, boss."
                except Exception as e:
                    logger.debug(f"[open_app] Resolved match launch failed: {e}")
                    # Fall through to the brute-force strategies below —
                    # a resolved-but-unlaunchable match shouldn't be a
                    # dead end when the old strategies might still work.
        except Exception as e:
            logger.debug(f"[open_app] App discovery unavailable: {e}")

    cmd = APP_MAP.get(name_lower)

    if _OS == "Windows":
        resolved = shutil.which(app_name) or (shutil.which(cmd) if cmd else None)
        ps_env = os.environ.copy()
        ps_env["FRIDAY_APP_NAME"] = app_name
        strategies = [
            # 1. Known alias or PATH-resolved binary — launched as an argv
            #    list, never shell=True, so nothing in app_name can be
            #    parsed as a cmd.exe command separator (&, &&, |, etc).
            *([lambda: subprocess.Popen([resolved], creationflags=subprocess.CREATE_NO_WINDOW)]
              if resolved else []),
            # 2. os.startfile — opens by Windows file association via
            #    ShellExecute directly, never through cmd.exe.
            lambda: os.startfile(app_name),
            # 3. PowerShell Start-Process — app_name is passed as data via
            #    an environment variable, never spliced into the command
            #    text (repr() is Python-string escaping, not PowerShell
            #    escaping, so it was never actually safe here).
            lambda: subprocess.Popen(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 "Start-Process -FilePath $env:FRIDAY_APP_NAME"],
                creationflags=subprocess.CREATE_NO_WINDOW, capture_output=True, env=ps_env
            ),
        ]
    elif _OS == "Darwin":
        strategies = [
            lambda: subprocess.Popen(["open", "-a", app_name]),
            lambda: subprocess.Popen([name_lower]),
        ]
    else:
        strategies = [
            lambda: subprocess.Popen([name_lower]),
            lambda: subprocess.Popen(["xdg-open", app_name]),
        ]

    for strategy in strategies:
        try:
            strategy()
            return f"Opened {app_name}, boss."
        except Exception:
            continue

    return f"Couldn't find {app_name}, boss. Is it installed?"


# ── Web Search ───────────────────────────────────────────────────────────────

async def handle_web_search(query: str) -> str:
    """Web search: Tavily -> Exa -> DDG scraper (last resort) -> open browser."""
    import urllib.parse

    loop = asyncio.get_running_loop()

    # Tier 1/2: real APIs with failover (actions/search_providers.py)
    try:
        from actions.search_providers import web_search_with_fallback
        results, provider = await loop.run_in_executor(None, web_search_with_fallback, query)
        if results:
            lines = [f"{r['title']}: {r['snippet']}" for r in results[:3] if r.get('snippet')]
            if lines:
                logger.info(f"[WebSearch] Answered via {provider}")
                return "Here's what I found:\n" + "\n".join(lines)
    except Exception as e:
        logger.warning(f"[WebSearch] Tavily/Exa path failed entirely: {e}")

    # Tier 3: DDG scraper — free, no key required, but fragile (see
    # actions/web_search.py's _get_ddgs_client for the known failure mode).
    # Only reached if neither TAVILY_API_KEY nor EXA_API_KEY is set, or
    # both genuinely failed above.
    def _do_search(q: str) -> list:
        try:
            from ddgs import DDGS
        except ImportError:
            try:
                from duckduckgo_search import DDGS
                logger.warning(
                    "[WebSearch] Using deprecated duckduckgo_search package — "
                    "'ddgs' isn't actually installed despite being in requirements.txt. "
                    "Run: pip install ddgs --break-system-packages"
                )
            except ImportError:
                logger.warning("[WebSearch] Neither ddgs nor duckduckgo_search is installed.")
                return []
        try:
            with DDGS() as ddgs:
                return list(ddgs.text(q, max_results=5))
        except Exception as e:
            logger.warning(f"[WebSearch] DDG fallback search failed for {q!r}: {e}")
            return []

    try:
        results = await loop.run_in_executor(None, _do_search, query)
    except Exception as e:
        logger.warning(f"[WebSearch] Unexpected error: {e}")
        results = []

    if results:
        logger.info("[WebSearch] Answered via DDG fallback")
        lines = [f"{r.get('title','')}: {r.get('body','')}" for r in results[:3] if r.get('body')]
        if lines:
            return "Here's what I found:\n" + "\n".join(lines)

    if not results:
        # Hard fallback: open browser. Being honest about WHY here matters
        # for a voice assistant — the user wanted a spoken answer, and a
        # silently-opened tab with no explanation reads as the assistant
        # ignoring the question rather than hitting a real search failure.
        webbrowser.open(f"https://www.google.com/search?q={urllib.parse.quote(query)}")
        return (
            f"Couldn't get results from Tavily, Exa, or the DDG fallback, boss — opened a "
            f"browser tab to it instead. Check the terminal log for which one failed and why."
        )

    parts = [f"{r['title']}: {r['body'][:250]}" for r in results[:4] if r.get('body')]
    return "\n\n".join(parts) if parts else "No useful results found."


# ── Computer Settings ────────────────────────────────────────────────────────

async def handle_computer_settings(args: dict) -> str:
    """
    Routes computer control requests dynamically based on description.
    The LLM already chose this tool with a natural language description —
    we match it to a capability without hardcoded keyword lists.
    """
    desc = (args.get("description") or "").lower().strip()
    value = args.get("value", "")

    # Match by semantic category — ordered by specificity
    CATEGORIES = [
        (["volume", "mute", "unmute", "sound", "louder", "quieter", "audio"], "volume"),
        (["brightness", "screen brightness", "display brightness"],            "brightness"),
        (["wifi", "wi-fi", "wireless", "internet connection"],                "wifi"),
        (["screenshot", "capture screen", "screen capture"],                  "screenshot"),
        (["lock", "lock screen", "lock pc", "lock computer"],                 "lock"),
        (["dark mode", "light mode", "night mode", "theme"],                  "darkmode"),
        (["clipboard", "copy", "paste", "what's copied"],                     "clipboard"),
        (["refresh", "reload", "f5"],                                          "refresh"),
    ]

    for keywords, category in CATEGORIES:
        if any(k in desc for k in keywords):
            if category == "volume":     return await _volume(desc, value)
            if category == "brightness": return await _brightness(desc, value)
            if category == "wifi":       return await _wifi(desc)
            if category == "screenshot": return await handle_screenshot()
            if category == "lock":
                if _OS == "Windows":
                    subprocess.Popen(["rundll32.exe", "user32.dll,LockWorkStation"])
                return "Screen locked, boss."
            if category == "darkmode":   return await _dark_mode()
            if category == "clipboard":  return await _clipboard(desc, value)
            if category == "refresh":
                try:
                    import pyautogui
                    pyautogui.hotkey("ctrl", "r")
                    return "Refreshed, boss."
                except Exception as e:
                    return f"Refresh failed: {e}"

    # Keyboard shortcut — if value provided, execute it directly
    if value:
        try:
            import pyautogui
            keys = [k.strip() for k in re.split(r"[+]", value)]
            pyautogui.hotkey(*keys)
            return f"Shortcut {value} executed, boss."
        except Exception as e:
            return f"Shortcut failed: {e}"

    # Last resort: nothing matched a known category, and there's no
    # keyboard-shortcut value either. This used to shell out the raw
    # description as a command, which meant "computer_settings" could be
    # used to run arbitrary shell commands just by phrasing a request so
    # it missed every keyword above — that's not something escaping can
    # fix safely, so it's just not done anymore.
    return f"I'm not sure how to handle: {desc}"


async def _volume(desc: str, value: str) -> str:
    try:
        if _OS == "Windows":
            from pycaw.pycaw import AudioUtilities
            vol = AudioUtilities.GetSpeakers().EndpointVolume

            if "mute" in desc and "unmute" not in desc:
                vol.SetMute(1, None)
                return "Muted, boss."
            if "unmute" in desc:
                vol.SetMute(0, None)
                return "Unmuted, boss."

            m = re.search(r"(\d+)", value or desc)
            if m:
                level = min(100, max(0, int(m.group(1))))
                vol.SetMasterVolumeLevelScalar(level / 100, None)
                return f"Volume at {level}%, boss."
            if any(w in desc for w in ["up", "louder", "higher", "increase"]):
                cur = vol.GetMasterVolumeLevelScalar()
                new = min(1.0, cur + 0.1)
                vol.SetMasterVolumeLevelScalar(new, None)
                return f"Volume up to {int(new * 100)}%, boss."
            if any(w in desc for w in ["down", "quieter", "lower", "decrease"]):
                cur = vol.GetMasterVolumeLevelScalar()
                new = max(0.0, cur - 0.1)
                vol.SetMasterVolumeLevelScalar(new, None)
                return f"Volume down to {int(new * 100)}%, boss."

        elif _OS == "Darwin":
            import pyautogui
            if "up" in desc:
                for _ in range(5): pyautogui.press("volumeup")
            elif "down" in desc:
                for _ in range(5): pyautogui.press("volumedown")
            elif "mute" in desc:
                pyautogui.press("volumemute")
            return "Done, boss."

    except Exception as e:
        return f"Volume error: {e}"
    return "Volume adjusted, boss."


async def _brightness(desc: str, value: str) -> str:
    try:
        import screen_brightness_control as sbc
        m = re.search(r"(\d+)", value or desc)
        if m:
            level = min(100, max(0, int(m.group(1))))
            sbc.set_brightness(level)
            return f"Brightness at {level}%, boss."
        if "up" in desc or "increase" in desc:
            cur = sbc.get_brightness()[0]
            sbc.set_brightness(min(100, cur + 10))
            return "Brightness up, boss."
        if "down" in desc or "decrease" in desc:
            cur = sbc.get_brightness()[0]
            sbc.set_brightness(max(0, cur - 10))
            return "Brightness down, boss."
    except ImportError:
        return "Install screen-brightness-control: pip install screen-brightness-control"
    except Exception as e:
        return f"Brightness error: {e}"
    return "Brightness adjusted, boss."


async def _wifi(desc: str) -> str:
    try:
        if _OS == "Windows":
            if any(w in desc for w in ["off", "disable"]):
                subprocess.run(["netsh", "interface", "set", "interface", "Wi-Fi", "disable"],
                               capture_output=True)
                return "WiFi disabled, boss."
            else:
                subprocess.run(["netsh", "interface", "set", "interface", "Wi-Fi", "enable"],
                               capture_output=True)
                return "WiFi enabled, boss."
    except Exception as e:
        return f"WiFi error: {e}"
    return "WiFi toggled, boss."


async def _clipboard(desc: str, value: str) -> str:
    try:
        import pyperclip
        if any(w in desc for w in ["read", "what's", "show", "get"]):
            content = pyperclip.paste()
            return f"Clipboard: {content[:200]}" if content else "Clipboard is empty, boss."
        elif value:
            pyperclip.copy(value)
            return "Copied to clipboard, boss."
    except ImportError:
        return "Install pyperclip: pip install pyperclip"
    except Exception as e:
        return f"Clipboard error: {e}"
    return "Done, boss."


async def _dark_mode() -> str:
    try:
        if _OS == "Windows":
            import winreg
            key_path = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_READ) as k:
                current = winreg.QueryValueEx(k, "AppsUseLightTheme")[0]
            new_val = 0 if current == 1 else 1
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_WRITE) as k:
                winreg.SetValueEx(k, "AppsUseLightTheme", 0, winreg.REG_DWORD, new_val)
                winreg.SetValueEx(k, "SystemUsesLightTheme", 0, winreg.REG_DWORD, new_val)
            return f"Switched to {'Light' if new_val else 'Dark'} mode, boss."
    except Exception as e:
        return f"Dark mode error: {e}"
    return "Mode toggled, boss."


# ── Browser Control ──────────────────────────────────────────────────────────
#
# actions/browser_control.py is a real Playwright-based multi-browser
# controller — launches the actual named browser (Edge, Chrome, Opera,
# Brave, Firefox, ...) on its real profile, not just "whatever
# webbrowser.open() feels like opening" (which is always the OS
# default, and can't be steered even when the user names a browser).
# It existed in this codebase already but nothing ever called it —
# this is that wiring. Falls back to the old plain webbrowser.open()
# behavior if Playwright genuinely isn't installed, so a partial
# environment still gets basic search/go_to rather than nothing.

# "What are we searching for" follow-up. Mirrors
# actions/focus_session.py's is_awaiting_voice_reply()/
# consume_voice_reply() shape on purpose — same problem, same fix:
# without remembering that a query is pending, the next thing the user
# says goes through NORMAL intent classification instead of completing
# the search, and something like "weather in bangalore" gets answered
# as its own weather query instead of becoming the search term. This
# is exactly the bug that motivated adding it here.
_pending_browser_search: Optional[dict] = None  # {"browser": "edge"} (browser may be "") while pending, else None


def is_awaiting_browser_query() -> bool:
    return _pending_browser_search is not None


async def consume_browser_query(text: str) -> Optional[str]:
    """Called from start.py's _handle_user_text, in the same early,
    before-intent-classification position as focus_session's
    consume_voice_reply(). If a browser search is waiting on its query,
    treats `text` as that query, actually runs the search, and returns
    the result to speak. Returns None otherwise so the caller falls
    through to normal handling — same contract as consume_voice_reply()."""
    global _pending_browser_search
    if _pending_browser_search is None:
        return None
    pending = _pending_browser_search
    _pending_browser_search = None

    query = (text or "").strip()
    if not query:
        return "Still didn't catch a query, boss — just ask again whenever."

    args = {"action": "search", "query": query}
    if pending.get("browser"):
        args["browser"] = pending["browser"]
    return await handle_browser_control(args)


def _webbrowser_fallback(action: str, url: str, query: str) -> str:
    """The old behavior, kept as a fallback for when Playwright isn't
    installed — always opens the OS default browser (webbrowser.open()
    has no concept of "which browser"), but that's still better than
    nothing working at all."""
    if action == "go_to" and url:
        if not url.startswith("http"):
            url = "https://" + url
        webbrowser.open(url)
        return f"Opened {url}, boss."
    if action == "search" and query:
        import urllib.parse
        webbrowser.open(f"https://www.google.com/search?q={urllib.parse.quote_plus(query)}")
        return f"Searching for: {query}"
    return (f"Browser action '{action}' needs Playwright for anything beyond a plain "
            f"search or go_to: pip install playwright && playwright install")


async def handle_browser_control(args: dict) -> str:
    global _pending_browser_search
    action = args.get("action", "")
    query = args.get("query", "")
    browser = (args.get("browser") or "").strip()

    if action == "search" and not query:
        # Don't even try to launch a browser for nothing to search —
        # ask cleanly, remember what was asked (which browser, if any),
        # and let consume_browser_query() above complete it next turn.
        _pending_browser_search = {"browser": browser}
        return "What do you want to search for?"

    try:
        from actions.browser_control import browser_control
    except ImportError:
        return _webbrowser_fallback(action, args.get("url", ""), query)

    # browser_control() is synchronous and blocks (up to 60s per its own
    # timeout) — it manages its own background thread + event loop per
    # browser session, so it must run off FRIDAY's own event loop rather
    # than block it.
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: browser_control(parameters=args))


# ── File Controller ──────────────────────────────────────────────────────────

async def handle_file_controller(args: dict) -> str:
    action = args.get("action", "")
    path_str = args.get("path", "")
    name = args.get("name", "")
    content = args.get("content", "")
    destination = args.get("destination", "")
    new_name = args.get("new_name", "")

    try:
        if action == "list":
            p = _resolve_path(path_str or "home")
            if not p.exists():
                return f"Path doesn't exist: {p}"
            items = [f.name for f in sorted(p.iterdir())[:30]]
            return f"Contents of {p.name}: {', '.join(items)}"

        elif action in ("create_file", "write"):
            p = _resolve_path(path_str or "desktop")
            if name:
                p = p / name
            p.parent.mkdir(parents=True, exist_ok=True)
            from actions.file_trash import trash_before_overwrite
            trash_before_overwrite(p)  # no-op if p doesn't exist yet — nothing to back up
            p.write_text(content, encoding="utf-8")
            return f"Created {p.name}, boss."

        elif action == "create_folder":
            p = _resolve_path(path_str)
            if name:
                p = p / name
            p.mkdir(parents=True, exist_ok=True)
            return f"Created folder {p.name}, boss."

        elif action in ("read", "read_file"):
            p = _resolve_path(path_str)
            if name:
                p = p / name
            if not p.exists():
                return f"File not found: {p}"
            text = p.read_text(encoding="utf-8", errors="replace")
            return text[:600] + ("…" if len(text) > 600 else "")

        elif action == "delete":
            p = _resolve_path(path_str)
            from actions.file_trash import trash_before_delete
            trashed = trash_before_delete(p)
            if not trashed:
                # FRIDAY's own trash failed (e.g. cross-device move error)
                # — fall back to the OS recycle bin, still recoverable
                # just not through the undo command, then a raw delete
                # only as a last resort.
                try:
                    import send2trash
                    send2trash.send2trash(str(p))
                except ImportError:
                    import shutil
                    if p.is_file():
                        p.unlink(missing_ok=True)
                    elif p.is_dir():
                        shutil.rmtree(str(p))
            return f"Deleted {p.name}, boss."

        elif action in ("undo", "undo_last", "restore"):
            from actions.file_trash import undo_last
            return undo_last()

        elif action == "find":
            root = _resolve_path(path_str or "home")
            pattern = name or ""
            found = list(root.rglob(f"*{pattern}*"))[:10]
            return f"Found: {', '.join(f.name for f in found)}" if found else f"No files matching '{pattern}'."

        elif action == "open":
            p = _resolve_path(path_str)
            if _OS == "Windows":
                os.startfile(str(p))
            elif _OS == "Darwin":
                subprocess.Popen(["open", str(p)])
            else:
                subprocess.Popen(["xdg-open", str(p)])
            return f"Opened {p.name}, boss."

        elif action == "disk_usage":
            import shutil
            p = _resolve_path(path_str or "C:\\")
            t, u, f = shutil.disk_usage(p)
            return (f"Drive: {u/(1024**3):.1f} GB used, {f/(1024**3):.1f} GB free "
                    f"of {t/(1024**3):.0f} GB.")

        elif action == "rename":
            p = _resolve_path(path_str)
            if new_name:
                new_p = p.parent / new_name
                p.rename(new_p)
                return f"Renamed to {new_name}, boss."

        elif action in ("move", "copy"):
            p = _resolve_path(path_str)
            dest = _resolve_path(destination) / p.name if destination else p
            import shutil
            (shutil.move if action == "move" else shutil.copy2)(str(p), str(dest))
            return f"{'Moved' if action == 'move' else 'Copied'} {p.name}, boss."

        else:
            return f"Unknown file action: {action}"

    except Exception as e:
        return f"File operation failed: {e}"


# ── Messaging ────────────────────────────────────────────────────────────────

async def handle_send_message(args: dict) -> str:
    receiver = args.get("receiver", "")
    message = args.get("message_text", "")
    msg_platform = args.get("platform", "WhatsApp")

    try:
        from actions.send_message import send_message
    except ImportError:
        return _send_message_fallback(receiver, message, msg_platform)

    if not receiver or not message:
        return "Please specify who and what to send, boss."

    # send_message() is synchronous and uses pyautogui (real keystrokes/
    # timing, several seconds end to end) — same reason as
    # browser_control(): must run off the event loop, not on it.
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(
        None, lambda: send_message(parameters={
            "receiver": receiver, "message_text": message, "platform": msg_platform,
        })
    )
    return f"{result} boss." if not result.rstrip().endswith((".", "!", "?")) else result


def _send_message_fallback(receiver: str, message: str, msg_platform: str) -> str:
    """Old behavior, kept for when pyautogui genuinely isn't installed:
    pre-fills the message and asks the user to hit Send themselves,
    rather than actually sending it."""
    msg_platform = msg_platform.lower()
    try:
        if "whatsapp" in msg_platform:
            import urllib.parse
            msg_enc = urllib.parse.quote(message)
            if _OS == "Windows":
                try:
                    wa_uri = f"whatsapp://send?text={msg_enc}"
                    os.startfile(wa_uri)
                    return (f"Opened WhatsApp desktop with message pre-filled. "
                            f"Select '{receiver}' and hit Send, boss.")
                except Exception:
                    pass
            url = f"https://web.whatsapp.com/send?text={msg_enc}"
            webbrowser.open(url)
            return f"Opened WhatsApp Web. Select '{receiver}' and hit Send, boss."
        elif "telegram" in msg_platform:
            if _OS == "Windows":
                subprocess.Popen("start telegram://", shell=True,
                                 creationflags=subprocess.CREATE_NO_WINDOW)
            else:
                subprocess.Popen(["xdg-open", "tg://"])
            return f"Opened Telegram. Find '{receiver}' and send: {message}"
        else:
            return (f"Platform '{msg_platform}' needs pyautogui for automated sending: "
                    f"pip install pyautogui pyperclip")
    except Exception as e:
        return f"Messaging failed: {e}"


# ── Spotify ──────────────────────────────────────────────────────────────────
# Strategy:
#   1. spotipy API (requires cached token + active device) - full control
#   2. spotify: URI scheme - opens/controls desktop app, no auth needed
#   3. YouTube fallback

_spotify_client_cache = None

_SPOTIFY_REDIRECT_URI = "http://127.0.0.1:8888/callback"
_SPOTIFY_SCOPE = "user-modify-playback-state user-read-playback-state user-read-currently-playing"


def _spotify_cache_path() -> str:
    from config import config
    return str(config.base_dir / ".spotify_cache")


def _run_spotify_login_flow(timeout_seconds: int = 120) -> str:
    """One-time Spotify OAuth login: opens the browser once, catches the
    redirect on a local callback server, and caches the resulting token
    exactly where _try_get_spotify_client() looks for it. Synchronous —
    callers must run this via run_in_executor, since handle_request()
    below blocks for up to timeout_seconds and would otherwise stall the
    whole event loop for that long."""
    from config import config
    client_id     = config.brain.spotify_client_id
    client_secret = config.brain.spotify_client_secret
    if not client_id or not client_secret:
        return (
            "Spotify isn't set up yet — I need a client ID and secret from your Spotify "
            "Developer Dashboard first (developer.spotify.com/dashboard). Add "
            "SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET to your .env — make sure "
            f"{_SPOTIFY_REDIRECT_URI} is added as a Redirect URI in the app's settings there "
            "too — then ask me to connect Spotify again."
        )

    import spotipy
    from spotipy.oauth2 import SpotifyOAuth, SpotifyOauthError
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from urllib.parse import urlparse

    auth = SpotifyOAuth(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=_SPOTIFY_REDIRECT_URI,
        scope=_SPOTIFY_SCOPE,
        open_browser=False,
        cache_path=_spotify_cache_path(),
    )

    captured = {}

    class _CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            captured["path"] = self.path
            ok = "code=" in self.path
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            msg = "Connected — you can close this tab." if ok else "Something went wrong — you can close this tab and try again."
            self.wfile.write(f"<html><body style='font-family:sans-serif'><h2>FRIDAY</h2>{msg}</body></html>".encode())

        def log_message(self, format, *args):
            pass  # don't spam stderr with raw HTTP request logging

    parsed = urlparse(_SPOTIFY_REDIRECT_URI)
    host, port = parsed.hostname, parsed.port

    try:
        httpd = HTTPServer((host, port), _CallbackHandler)
    except OSError as e:
        return (f"Couldn't start the Spotify login callback server on {host}:{port} ({e}) — "
                f"something else may already be using that port.")

    auth_url = auth.get_authorize_url()
    if webbrowser.open(auth_url):
        logger.info("Spotify login: opened browser for authorization.")
    else:
        logger.info(f"Spotify login: couldn't auto-open a browser — visit {auth_url}")

    httpd.timeout = timeout_seconds
    httpd.handle_request()
    httpd.server_close()

    if "path" not in captured:
        return ("Didn't hear back from Spotify within two minutes — the login may have been "
                "cancelled or the tab never got submitted. Ask me to connect Spotify again to retry.")

    full_callback_url = f"http://{host}:{port}{captured['path']}"
    try:
        code = auth.parse_response_code(full_callback_url)
    except SpotifyOauthError as e:
        return f"Spotify login didn't go through — {e}. Ask me to connect Spotify again to retry."

    if not code or code == full_callback_url:
        return "Spotify's response didn't include an authorization code. Ask me to connect Spotify again to retry."

    try:
        auth.get_access_token(code, as_dict=False, check_cache=False)
    except Exception as e:
        return f"Spotify login failed while exchanging the code: {e}"

    global _spotify_client_cache
    _spotify_client_cache = None  # drop any stale cache so the next call re-reads the new token

    try:
        sp = spotipy.Spotify(auth_manager=auth)
        me = sp.current_user()
        who = me.get("display_name") or me.get("id") or "your account"
        return f"Connected to Spotify as {who}, boss — real playback control is live now."
    except Exception as e:
        return f"Got a token but couldn't confirm the connection ({e}). Try a play/pause command to check."


def _try_get_spotify_client():
    """Return authenticated Spotipy client only if cached token exists. Never opens browser."""
    global _spotify_client_cache
    if _spotify_client_cache is not None:
        return _spotify_client_cache

    from config import config
    client_id     = config.brain.spotify_client_id
    client_secret = config.brain.spotify_client_secret
    if not client_id or not client_secret:
        return None

    try:
        import spotipy
        from spotipy.oauth2 import SpotifyOAuth
        # IMPORTANT: use 127.0.0.1 (not localhost) and open_browser=False
        # Add http://127.0.0.1:8888/callback in your Spotify app dashboard
        auth = SpotifyOAuth(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=_SPOTIFY_REDIRECT_URI,
            scope=_SPOTIFY_SCOPE,
            open_browser=False,
            cache_path=_spotify_cache_path(),
        )
        # Only use if already authenticated - never block for browser auth
        token_info = auth.get_cached_token()
        if not token_info:
            return None
        _spotify_client_cache = spotipy.Spotify(auth_manager=auth)
        _spotify_client_cache.current_user()
        logger.info("Spotify API connected via cached token")
        return _spotify_client_cache
    except Exception as e:
        logger.debug(f"Spotify API unavailable: {e}")
        return None


def _pick_best_track(query: str, candidates: list) -> dict:
    """
    From a list of Spotify track candidates, pick the one whose
    name+artist best matches the query using character-level similarity.
    Falls back to candidates[0] if nothing scores well.

    This prevents returning Jason Derulo when the user asked for Katy Perry.
    """
    def similarity(a: str, b: str) -> float:
        """Simple character bigram similarity - fast, no deps."""
        a, b = a.lower().strip(), b.lower().strip()
        if not a or not b:
            return 0.0
        if a in b or b in a:
            return 0.9
        # Bigram overlap
        def bigrams(s):
            return {s[i:i+2] for i in range(len(s)-1)}
        bg_a, bg_b = bigrams(a), bigrams(b)
        if not bg_a or not bg_b:
            return 0.0
        return 2 * len(bg_a & bg_b) / (len(bg_a) + len(bg_b))

    query_lower = query.lower()
    best_score = -1
    best_track = candidates[0]

    for track in candidates:
        name = track.get("name", "")
        artist = track["artists"][0]["name"] if track.get("artists") else ""
        combined = f"{name} {artist}"

        score = max(
            similarity(query_lower, name.lower()),
            similarity(query_lower, combined.lower()),
        )

        # Bonus: if artist name appears in query, strongly prefer this track
        if artist.lower() in query_lower:
            score += 0.4

        # Bonus: if track name words mostly appear in query
        name_words = name.lower().split()
        query_words = query_lower.split()
        word_overlap = sum(1 for w in name_words if any(w in qw or qw in w for qw in query_words))
        if name_words:
            score += 0.3 * (word_overlap / len(name_words))

        if score > best_score:
            best_score = score
            best_track = track

    logger.debug(f"[Spotify] Best match: '{best_track['name']}' score={best_score:.2f}")
    return best_track


async def _spotify_uri_play(query: str) -> str:
    """
    Plays a track or genre/mood playlist on Spotify desktop without user API auth.

    Strategy:
      0. Genre/mood/language queries → open a Spotify playlist search URI directly
      1. Specific song/artist queries → Spotify Web API track search (client credentials)
      2. Open spotify:track:<id> URI — auto-plays in the desktop app
      3. Fallback: open spotify:search:<query>, focus the Spotify window, press Enter
      4. Last resort: YouTube
    """
    import urllib.parse
    import urllib.request
    import json as _json

    _GENRE_SIGNALS = {
        "chill", "lo-fi", "lofi", "lo fi", "relaxing", "calm", "sleep", "focus",
        "study", "work", "sad", "happy", "upbeat", "hype", "party", "energetic",
        "workout", "morning", "night", "romantic", "love", "ambient",
        "hindi", "bollywood", "indian", "punjabi", "desi", "tamil", "telugu",
        "pop", "rock", "jazz", "classical", "rap", "hip hop", "hip-hop",
        "r&b", "rnb", "edm", "electronic", "metal", "country", "indie",
        "k-pop", "kpop", "latin", "reggae", "blues", "soul", "folk",
        "music", "songs", "playlist", "mix", "beats", "hits", "tracks",
        "vibes", "best of", "top", "chart",
    }

    def _is_genre_query(q: str) -> bool:
        return any(sig in q.lower() for sig in _GENRE_SIGNALS)

    def _focus_spotify() -> bool:
        try:
            import win32gui, win32con
            wins = []
            win32gui.EnumWindows(lambda h, ws: ws.append(h) if "spotify" in win32gui.GetWindowText(h).lower() and win32gui.IsWindowVisible(h) else None, wins)
            if wins:
                win32gui.ShowWindow(wins[0], win32con.SW_RESTORE)
                win32gui.SetForegroundWindow(wins[0])
                return True
        except Exception:
            pass
        return False

    track_uri = None
    track_name = None
    artist_name = None

    # Step 0: Genre/mood query → playlist search (not track search)
    if _is_genre_query(query):
        try:
            playlist_uri = f"spotify:search:{urllib.parse.quote(query + ' playlist')}"
            if _OS == "Windows":
                os.startfile(playlist_uri)
                await asyncio.sleep(2.0)
                if not _focus_spotify():
                    await asyncio.sleep(0.8)
                try:
                    import pyautogui
                    pyautogui.press("enter")
                except Exception:
                    pass
            elif _OS == "Darwin":
                subprocess.Popen(["open", playlist_uri])
            else:
                subprocess.Popen(["xdg-open", playlist_uri])
            return f"Opened Spotify playlist search for '{query}', boss."
        except Exception as e:
            logger.warning(f"[Spotify] Playlist URI failed: {e}")

    # Step 1: Specific track — Spotify Web API search
    client_id = os.environ.get("SPOTIFY_CLIENT_ID", "")
    client_secret = os.environ.get("SPOTIFY_CLIENT_SECRET", "")

    if not client_id or not client_secret:
        logger.warning(
            "[Spotify] SPOTIFY_CLIENT_ID/SPOTIFY_CLIENT_SECRET not set — falling back to "
            "search-only mode. See .env.example for setup steps."
        )

    if client_id and client_secret:
        try:
            import base64
            creds = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
            token_req = urllib.request.Request(
                "https://accounts.spotify.com/api/token",
                data=b"grant_type=client_credentials",
                headers={"Authorization": f"Basic {creds}", "Content-Type": "application/x-www-form-urlencoded"},
            )
            with urllib.request.urlopen(token_req, timeout=5) as resp:
                token = _json.loads(resp.read())["access_token"]
            q = urllib.parse.quote(query)
            search_req = urllib.request.Request(
                f"https://api.spotify.com/v1/search?q={q}&type=track&limit=5",
                headers={"Authorization": f"Bearer {token}"},
            )
            with urllib.request.urlopen(search_req, timeout=5) as resp:
                data = _json.loads(resp.read())
            candidates = data.get("tracks", {}).get("items", [])
            if candidates:
                best = _pick_best_track(query, candidates)
                track_uri = best["uri"]
                track_name = best["name"]
                artist_name = best["artists"][0]["name"]
                logger.info(f"[Spotify] Matched: '{track_name}' by {artist_name} (query: {query!r})")
            else:
                logger.warning(f"[Spotify] Credentials OK but search found zero candidates for {query!r} — falling back to search-only mode.")
        except Exception as e:
            logger.warning(f"[Spotify] Token/search request failed, falling back to search-only mode: {e}")

    # Step 2: Open spotify:track:<id> — auto-plays in desktop app
    if track_uri:
        try:
            if _OS == "Windows":
                os.startfile(track_uri)
            elif _OS == "Darwin":
                subprocess.Popen(["open", track_uri])
            else:
                subprocess.Popen(["xdg-open", track_uri])
            return f"Playing {track_name or query} by {artist_name or 'unknown artist'} on Spotify, boss."
        except Exception as e:
            logger.warning(f"[Spotify] URI open failed: {e}")

    # Step 3: Search fallback — focus Spotify window before pressing Enter
    try:
        import pyautogui
        search_uri = f"spotify:search:{urllib.parse.quote(query)}"
        if _OS == "Windows":
            os.startfile(search_uri)
            await asyncio.sleep(2.0)
            if not _focus_spotify():
                await asyncio.sleep(1.0)
            await asyncio.sleep(0.3)
        elif _OS == "Darwin":
            subprocess.Popen(["open", search_uri])
            await asyncio.sleep(2.5)
        else:
            subprocess.Popen(["xdg-open", search_uri])
            await asyncio.sleep(2.5)
        pyautogui.press("enter")
        return f"Opened Spotify search for {query!r} and pressed play, boss."
    except Exception as e:
        logger.warning(f"[Spotify] Search+enter failed: {e}")

    # Step 4: YouTube last resort
    import urllib.parse as up
    webbrowser.open(f"https://www.youtube.com/results?search_query={up.quote(query)}")
    return f"Spotify not working, opened YouTube search for {query!r} instead, boss."


_VK_MEDIA_PLAY_PAUSE = 0xB3
_VK_MEDIA_NEXT_TRACK = 0xB0
_VK_MEDIA_PREV_TRACK = 0xB1


def _send_media_key(vk_code: int) -> None:
    """A real Windows system media key — VK_MEDIA_PLAY_PAUSE and friends
    are routed by Windows to whatever app currently owns the active media
    session, the same way physical media keys on a keyboard work. This
    does NOT require Spotify to have window focus at all, which is
    exactly the problem with the old approach below: bringing a window to
    the foreground from a background process is something Windows
    deliberately blocks in many cases (focus-stealing prevention), so
    "start spotify:" + a fixed sleep had no guarantee focus actually
    shifted before the keystroke fired — and pyautogui.hotkey() never
    raises just because the wrong window received it, so it reported
    success unconditionally even when the keypress went to FRIDAY's own
    window instead of Spotify. Media keys have no such ambiguity."""
    import ctypes
    KEYEVENTF_KEYUP = 0x0002
    ctypes.windll.user32.keybd_event(vk_code, 0, 0, 0)
    ctypes.windll.user32.keybd_event(vk_code, 0, KEYEVENTF_KEYUP, 0)


async def _spotify_key_control(action: str) -> str:
    """Controls Spotify desktop via keyboard shortcuts - no API auth needed."""
    try:
        import pyautogui
    except ImportError:
        return "pyautogui not installed - can't control Spotify without API auth."

    # Play/pause/resume/next/previous go through real Windows media keys —
    # focus-independent, so this works whether or not Spotify's window is
    # visible or in the foreground. Shuffle has no equivalent system media
    # key, so it still needs Spotify actually focused; kept below as the
    # one case still using the old bring-to-foreground approach.
    media_keys = {
        "play": _VK_MEDIA_PLAY_PAUSE,
        "pause": _VK_MEDIA_PLAY_PAUSE,
        "resume": _VK_MEDIA_PLAY_PAUSE,
        "next": _VK_MEDIA_NEXT_TRACK,
        "previous": _VK_MEDIA_PREV_TRACK,
    }
    if _OS == "Windows" and action in media_keys:
        try:
            _send_media_key(media_keys[action])
            labels = {"play": "Playing", "pause": "Paused", "resume": "Resumed",
                      "next": "Skipped", "previous": "Going back"}
            return f"{labels[action]}, boss."
        except Exception as e:
            return f"Media key failed: {e}"

    if _OS == "Windows":
        # Bring Spotify to foreground
        subprocess.Popen("start spotify:", shell=True, creationflags=subprocess.CREATE_NO_WINDOW)
        await asyncio.sleep(0.6)

    shortcuts = {
        "play":     ("space",),
        "pause":    ("space",),
        "resume":   ("space",),
        "next":     ("ctrl", "right"),
        "previous": ("ctrl", "left"),
        "shuffle":  ("ctrl", "s"),
    }
    keys = shortcuts.get(action)
    if keys:
        try:
            pyautogui.hotkey(*keys)
            labels = {"play": "Playing", "pause": "Paused", "resume": "Resumed",
                      "next": "Skipped", "previous": "Going back", "shuffle": "Shuffle toggled"}
            return f"{labels.get(action, action.title())}, boss."
        except Exception as e:
            return f"Shortcut failed: {e}"
    return f"Can't do '{action}' without Spotify API auth, boss."


async def handle_spotify(args: dict) -> str:
    action = args.get("action", "")
    query  = args.get("query", "")
    value  = args.get("value", "")

    if action in ("login", "connect", "setup", "authorize"):
        return await asyncio.get_running_loop().run_in_executor(None, _run_spotify_login_flow)

    sp = await asyncio.get_running_loop().run_in_executor(None, _try_get_spotify_client)

    if sp is not None:
        try:
            if action == "play" and query:
                results = sp.search(q=query, limit=1, type="track")
                tracks = results["tracks"]["items"]
                if tracks:
                    sp.start_playback(uris=[tracks[0]["uri"]])
                    return f"Playing {tracks[0]['name']} by {tracks[0]['artists'][0]['name']}, boss."
                return f"Nothing found for: {query}"
            elif action == "pause":
                sp.pause_playback(); return "Paused, boss."
            elif action in ("resume", "play"):
                sp.start_playback(); return "Resumed, boss."
            elif action == "next":
                sp.next_track(); return "Skipped, boss."
            elif action == "previous":
                sp.previous_track(); return "Going back, boss."
            elif action == "current":
                track = sp.current_playback()
                if track and track.get("is_playing"):
                    return f"Playing {track['item']['name']} by {track['item']['artists'][0]['name']}, boss."
                return "Nothing playing right now, boss."
            elif action == "volume" and value:
                sp.volume(min(100, max(0, int(value)))); return f"Volume at {value}%, boss."
            elif action == "shuffle":
                sp.shuffle(True); return "Shuffle on, boss."
        except Exception as e:
            logger.warning(f"Spotify API error: {e} - falling back to URI/keys")

    # ── No API / API failed - use URI scheme or keyboard ──────────────────────
    if action == "play" and query:
        return await _spotify_uri_play(query)
    elif action in ("play", "pause", "resume", "next", "previous", "shuffle"):
        return await _spotify_key_control(action)
    elif action == "current":
        return "Can't check what's playing without a real Spotify connection — say 'connect my Spotify' to set that up (one-time, opens your browser), boss."
    else:
        return await handle_open_app("spotify")


# ── Google Account (Calendar + Gmail foundation) ──────────────────────────────
# Read-only status check only — actual Calendar/Gmail actions live in
# actions/calendar.py and actions/gmail.py (not built yet). This just
# tells the user whether the one-time `python google_auth_setup.py`
# has been done, so calendar/gmail features that get added later have
# something to build on top of immediately.

async def handle_google_status(args: dict = None) -> str:
    from actions import google_auth

    status = google_auth.get_status()
    if status.get("connected"):
        scopes = status.get("scopes", [])
        has_calendar = any("calendar" in s for s in scopes)
        has_gmail = any("gmail" in s for s in scopes)
        parts = []
        if has_calendar:
            parts.append("Calendar")
        if has_gmail:
            parts.append("Gmail")
        granted = " and ".join(parts) if parts else "an account"
        return f"Yes boss — connected to Google, {granted} access granted."
    reason = status.get("reason", "not connected")
    return f"Not connected to Google yet, boss — {reason}. Run `python google_auth_setup.py` from the repo root to set it up."


# ── Weather ──────────────────────────────────────────────────────────────────

async def handle_weather(city: str) -> str:
    # "my home city" / "home" / blank — resolve from config rather than
    # letting the model treat this as an unknown fact to look up via
    # memory or web search. Confirmed happening: "what's the weather in
    # my home city" tried recall_memory, then web_search for "Archit
    # home city", got back an unrelated stranger's LinkedIn, and gave up
    # — all because HOME_CITY in .env was never actually wired to
    # anything the model could reach. This is that wiring.
    if not city or city.strip().lower() in ("home", "home city", "my home city", "my city", "here"):
        from config import config
        if config.integrations.home_city:
            city = config.integrations.home_city
        else:
            return ("I don't have a home city set, boss — add HOME_CITY to your .env and restart, "
                    "or just tell me which city.")
    try:
        import urllib.request, json as _json
        req = urllib.request.Request(
            f"https://wttr.in/{city}?format=j1",
            headers={"User-Agent": "FRIDAY/2.0"}
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = _json.loads(resp.read())
        c = data["current_condition"][0]
        return (f"In {city}: {c['weatherDesc'][0]['value']}, "
                f"{c['temp_C']}°C (feels {c['FeelsLikeC']}°C), "
                f"humidity {c['humidity']}%, boss.")
    except Exception:
        webbrowser.open(f"https://wttr.in/{city}")
        return f"Opened weather for {city} in browser, boss."


# ── YouTube ──────────────────────────────────────────────────────────────────

async def handle_youtube(args: dict) -> str:
    action = args.get("action", "play")
    query = args.get("query", "")

    if action == "trending":
        webbrowser.open("https://www.youtube.com/feed/trending")
        return "Opened YouTube trending, boss."

    if query:
        try:
            import urllib.request, urllib.parse
            search_url = f"https://www.youtube.com/results?search_query={urllib.parse.quote(query)}"
            req = urllib.request.Request(search_url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=6) as resp:
                html = resp.read().decode("utf-8")
            matches = re.findall(r'"videoId":"([a-zA-Z0-9_-]{11})"', html)
            if matches:
                webbrowser.open(f"https://www.youtube.com/watch?v={matches[0]}")
                return f"Playing {query} on YouTube, boss."
        except Exception:
            pass
        import urllib.parse
        webbrowser.open(f"https://www.youtube.com/results?search_query={urllib.parse.quote(query)}")
        return f"Opened YouTube search for {query}, boss."

    return "What would you like to play, boss?"


# ── Screenshot ───────────────────────────────────────────────────────────────

async def handle_screenshot(save_path: Optional[str] = None) -> str:
    try:
        import mss
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        dest = Path(save_path) if save_path else Path.home() / "Desktop" / f"screenshot_{ts}.png"
        with mss.mss() as sct:
            sct.shot(output=str(dest))
        return f"Screenshot saved to {dest.name}, boss."
    except ImportError:
        try:
            import pyautogui
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            dest = Path.home() / "Desktop" / f"screenshot_{ts}.png"
            pyautogui.screenshot().save(str(dest))
            return f"Screenshot saved to {dest.name}, boss."
        except Exception as e:
            return f"Screenshot failed: {e}"
    except Exception as e:
        return f"Screenshot failed: {e}"


# ── Screen Process (Vision) ───────────────────────────────────────────────────

def _find_window_by_name(app_name: str):
    """Fuzzy-match a visible top-level window by title — same exact ->
    substring -> difflib approach as actions/app_discovery.py's app-name
    matching, adapted for window titles (noisier than app names, since
    they often include page titles/document names, hence the slightly
    lower fuzzy cutoff)."""
    import win32gui
    import difflib

    windows = []

    def _enum_handler(hwnd, _):
        if win32gui.IsWindowVisible(hwnd):
            title = win32gui.GetWindowText(hwnd)
            if title.strip():
                windows.append((hwnd, title))

    win32gui.EnumWindows(_enum_handler, None)
    if not windows:
        return None

    query = app_name.lower().strip()

    # "browser" (no specific name) isn't a real window title to substring-
    # match against — it's a category, and matching it as a literal
    # substring picks whichever open window's title HAPPENS to contain
    # that word, which is not necessarily any actual browser: an app
    # literally branded with "Browser" in its name (Dia's window title
    # includes it) will win this match every time regardless of which
    # browser is actually in use, while Chrome/Edge/Opera/Brave windows —
    # whose titles are just the page title + the browser's own name, never
    # the literal word "browser" — never match at all. Confirmed as the
    # cause of "it just keeps looking at Dia" even with several other
    # browsers open. For a generic term, resolve to whichever window is
    # actually in the foreground IF it's a real browser process; otherwise
    # return None so the caller falls back to a full-screen capture
    # instead of confidently grabbing an unrelated app.
    if query in ("browser", "web browser", "browser window"):
        return _foreground_window_if_process_in(_KNOWN_BROWSER_PROCESSES)

    for hwnd, title in windows:
        if title.lower() == query:
            return hwnd

    substring_matches = [(hwnd, title) for hwnd, title in windows if query in title.lower()]
    if len(substring_matches) == 1:
        return substring_matches[0][0]
    if len(substring_matches) > 1:
        return min(substring_matches, key=lambda m: len(m[1]))[0]

    best_hwnd, best_ratio = None, 0.0
    for hwnd, title in windows:
        ratio = difflib.SequenceMatcher(None, query, title.lower()).ratio()
        if ratio > best_ratio:
            best_hwnd, best_ratio = hwnd, ratio
    if best_hwnd and best_ratio >= 0.4:
        return best_hwnd
    return None


_KNOWN_BROWSER_PROCESSES = {
    "chrome.exe", "msedge.exe", "firefox.exe", "opera.exe", "opera_gx.exe",
    "brave.exe", "dia.exe", "vivaldi.exe", "iexplore.exe", "arc.exe",
}


def _foreground_window_if_process_in(process_names: set) -> Optional[int]:
    """The actual foreground window's hwnd, but only if its owning
    process is one of the given executables — otherwise None. Used so a
    generic category ('browser') resolves to whatever the user is
    actually looking at right now, not a title-text coincidence."""
    import win32gui
    try:
        import win32process
        import psutil
        hwnd = win32gui.GetForegroundWindow()
        if not hwnd:
            return None
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        proc_name = psutil.Process(pid).name().lower()
        return hwnd if proc_name in process_names else None
    except Exception:
        return None


def _capture_window(hwnd) -> Optional[bytes]:
    """Captures a SPECIFIC window's content via PrintWindow — unlike a
    full-screen grab, this works even when the window is minimized or
    fully covered by other windows, because it asks the window to render
    itself directly into an off-screen bitmap rather than reading
    whatever's currently visible on the monitor. PW_RENDERFULLCONTENT
    (flag=2) is required for modern GPU/DWM-composited apps — Chrome,
    Electron apps, anything hardware-accelerated — which render blank
    with the older PrintWindow behavior."""
    import win32gui
    import win32ui
    from ctypes import windll
    from PIL import Image
    import io

    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    width, height = right - left, bottom - top
    if width <= 0 or height <= 0:
        return None

    hwnd_dc = win32gui.GetWindowDC(hwnd)
    mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
    save_dc = mfc_dc.CreateCompatibleDC()
    save_bitmap = win32ui.CreateBitmap()
    save_bitmap.CreateCompatibleBitmap(mfc_dc, width, height)
    save_dc.SelectObject(save_bitmap)

    try:
        result = windll.user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), 2)  # PW_RENDERFULLCONTENT
        if not result:
            return None
        bmp_info = save_bitmap.GetInfo()
        bmp_bits = save_bitmap.GetBitmapBits(True)
        img = Image.frombuffer(
            "RGB", (bmp_info["bmWidth"], bmp_info["bmHeight"]),
            bmp_bits, "raw", "BGRX", 0, 1,
        )
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    finally:
        win32gui.DeleteObject(save_bitmap.GetHandle())
        save_dc.DeleteDC()
        mfc_dc.DeleteDC()
        win32gui.ReleaseDC(hwnd, hwnd_dc)


_SENSITIVE_APPS_PATH = Path(__file__).resolve().parent.parent / "core" / "sensitive_apps.json"
_sensitive_patterns_cache: Optional[list] = None
_sensitive_patterns_mtime: float = -1.0

_DEFAULT_SENSITIVE_PATTERNS = [
    "1password", "bitwarden", "lastpass", "dashlane", "keepass", "nordpass",
    "keeper password manager", "roboform", "enpass", "proton pass",
    "bank of america", "wells fargo", "chase", "citibank", "capital one",
    "us bank", "pnc bank", "td bank", " bank ", "banking",
    "paypal", "venmo", "cash app", "credit karma", "mint.com",
    "quickbooks", "turbotax", "fidelity", "vanguard", "charles schwab",
    "e*trade", "td ameritrade",
    "coinbase", "robinhood", "metamask", "trust wallet", "exodus wallet",
    "ledger live", "binance", "kraken",
    "irs.gov", "social security administration",
]


def _load_sensitive_patterns() -> list:
    """Cached, re-read only when core/sensitive_apps.json's mtime
    changes — edits take effect on the next screen_process call with no
    restart needed, without re-reading the file on every single call."""
    global _sensitive_patterns_cache, _sensitive_patterns_mtime
    try:
        mtime = _SENSITIVE_APPS_PATH.stat().st_mtime
        if _sensitive_patterns_cache is not None and mtime == _sensitive_patterns_mtime:
            return _sensitive_patterns_cache
        data = json.loads(_SENSITIVE_APPS_PATH.read_text(encoding="utf-8"))
        patterns = [p.lower() for p in data.get("patterns", []) if isinstance(p, str) and p.strip()]
        _sensitive_patterns_cache = patterns
        _sensitive_patterns_mtime = mtime
        return patterns
    except FileNotFoundError:
        # First run, or the file got deleted — write it back with sane
        # defaults so there's always something real to edit, rather
        # than silently running with no protection at all.
        try:
            _SENSITIVE_APPS_PATH.parent.mkdir(parents=True, exist_ok=True)
            _SENSITIVE_APPS_PATH.write_text(json.dumps({
                "_comment": (
                    "Window title patterns (case-insensitive substring match) that block "
                    "screen_process from sending a screenshot to Groq/Gemini. Add or remove "
                    "entries any time — re-read on the next request, no restart needed."
                ),
                "patterns": _DEFAULT_SENSITIVE_PATTERNS,
            }, indent=4), encoding="utf-8")
            logger.info(f"Created default sensitive-apps blocklist at {_SENSITIVE_APPS_PATH}")
        except Exception as e:
            logger.warning(f"Could not create default sensitive_apps.json: {e}")
        _sensitive_patterns_cache = [p.lower() for p in _DEFAULT_SENSITIVE_PATTERNS]
        _sensitive_patterns_mtime = 0.0
        return _sensitive_patterns_cache
    except Exception as e:
        logger.warning(f"Could not load core/sensitive_apps.json ({e}) — using built-in defaults this call.")
        return [p.lower() for p in _DEFAULT_SENSITIVE_PATTERNS]


def _match_sensitive_pattern(window_title: str) -> Optional[str]:
    """Returns the matched blocklist pattern if window_title looks
    sensitive, else None."""
    if not window_title:
        return None
    title_lower = window_title.lower()
    for pattern in _load_sensitive_patterns():
        if pattern in title_lower:
            return pattern
    return None


def _get_frontmost_window_title() -> str:
    """Best-effort active/frontmost window title, used when
    screen_process wasn't told a specific app name. Windows and macOS
    only — there's no portable, dependency-free way to get this on
    Linux, so the check is skipped there rather than giving false
    confidence in a check that isn't actually running."""
    try:
        if _OS == "Windows":
            import win32gui
            hwnd = win32gui.GetForegroundWindow()
            return win32gui.GetWindowText(hwnd) or ""
        if _OS == "Darwin":
            out = subprocess.run(
                ["osascript", "-e",
                 'tell application "System Events" to get name of first process whose frontmost is true'],
                capture_output=True, timeout=3, text=True,
            )
            return (out.stdout or "").strip()
    except Exception as e:
        logger.debug(f"[ScreenProcess] Could not get frontmost window title: {e}")
    return ""


def _get_frontmost_window() -> tuple:
    """Sibling of _get_frontmost_window_title() that also returns the
    owning process name, so callers can lock/compare at the app level
    for non-browser apps (where the title changes constantly but the
    app doesn't) as well as the title level.

    Returns (title, process_name); either may be "" if unavailable.
    Windows and macOS only, same as the title-only version — on Linux
    both come back empty rather than giving false confidence in a check
    that isn't actually running.

    Note for callers that care about privacy (actions/focus_session.py):
    this returns identifying strings by design. It is the caller's job
    to compare them and discard them without logging, broadcasting, or
    persisting them.
    """
    try:
        if _OS == "Windows":
            import win32gui
            hwnd = win32gui.GetForegroundWindow()
            title = win32gui.GetWindowText(hwnd) or ""
            proc = ""
            try:
                # psutil rather than win32process alone: we only want the
                # executable name, and this avoids opening/closing a
                # process handle by hand on every tick.
                import win32process
                import psutil
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
                proc = psutil.Process(pid).name() or ""
            except Exception:
                pass  # title alone is still useful
            return title, proc
        if _OS == "Darwin":
            out = subprocess.run(
                ["osascript", "-e",
                 'tell application "System Events" to get name of first process whose frontmost is true'],
                capture_output=True, timeout=3, text=True,
            )
            proc = (out.stdout or "").strip()
            # On macOS the cheap AppleScript call gives the process name;
            # the window title needs a second, slower call, so reuse the
            # process name for both rather than paying that cost every
            # tick. Title-level and app-level locking collapse to the
            # same thing here, which is acceptable — this is the same
            # value _get_frontmost_window_title() already returned on
            # macOS anyway.
            return proc, proc
    except Exception as e:
        logger.debug(f"Could not get frontmost window: {e}")
    return "", ""


def _vision_capability_note() -> str:
    """Returns "" when a vision route is actually usable, or a plain
    explanation of what's missing when it isn't.

    Checked BEFORE any capture so a camera/screen frame is never grabbed
    for a request that can't be answered anyway — and so the failure is
    a clear sentence instead of a generic "couldn't get a vision model
    to respond", which reads like a transient outage when it's really a
    missing key.
    """
    from config import config as _cfg
    groq_ok = bool(_cfg.brain.groq_api_key and _cfg.brain.groq_vision_model)
    gemini_ok = bool(_cfg.brain.gemini_api_key and _cfg.brain.gemini_model)
    if groq_ok or gemini_ok:
        return ""

    active = _cfg.brain.llm_provider
    missing = []
    if not _cfg.brain.groq_api_key:
        missing.append("GROQ_API_KEY")
    elif not _cfg.brain.groq_vision_model:
        missing.append("GROQ_VISION_MODEL")
    if not _cfg.brain.gemini_api_key:
        missing.append("GEMINI_API_KEY")
    elif not _cfg.brain.gemini_model:
        missing.append("GEMINI_MODEL")

    return (
        f"I can't look at anything right now — there's no vision model configured. "
        f"Looking at things doesn't run on {active} (the active text provider); it uses "
        f"Groq vision, with Gemini as a backup, and neither is set up. "
        f"Add {' or '.join(missing)} to .env and I'll have eyes again."
    )


async def _describe_image_b64(img_b64: str, img_format: str, prompt: str) -> str:
    """Sends one caller-supplied frame through the same vision route
    handle_screen_process uses (Groq vision primary, Gemini fallback)
    and returns the text.

    Split out so the ambient screen watch (actions/focus_session.py) can
    reuse the exact route rather than growing a second, drifting copy of
    the provider chain. The caller owns the image; nothing is kept here.
    """
    from config import config as _cfg
    from memory import usage_tracker as ut

    try:
        from groq import Groq
        if not _cfg.brain.groq_api_key or not _cfg.brain.groq_vision_model:
            raise RuntimeError("Groq vision not configured")
        client = Groq(api_key=_cfg.brain.groq_api_key)
        ut.record_call("groq_vision")
        resp = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: client.chat.completions.create(
                model=_cfg.brain.groq_vision_model,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url",
                         "image_url": {"url": f"data:image/{img_format};base64,{img_b64}"}},
                    ],
                }],
                max_tokens=120,
            ),
        )
        ut.mark_ok("groq_vision")
        return (resp.choices[0].message.content or "").strip()
    except Exception as e:
        logger.debug(f"Groq vision failed for supplied frame, trying Gemini: {e}")

    try:
        import google.genai as genai
        from google.genai import types as gtypes
        import base64 as _b64
        if not _cfg.brain.gemini_api_key or not _cfg.brain.gemini_model:
            raise RuntimeError("Gemini not configured")
        client = genai.Client(api_key=_cfg.brain.gemini_api_key)
        resp = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: client.models.generate_content(
                model=_cfg.brain.gemini_model,
                contents=[
                    gtypes.Part.from_bytes(data=_b64.b64decode(img_b64),
                                            mime_type=f"image/{img_format}"),
                    prompt,
                ],
            ),
        )
        return (resp.text or "").strip()
    except Exception as e:
        logger.debug(f"Gemini vision failed for supplied frame: {e}")
        return ""


def _get_frontmost_excluding(exclude_procs: tuple = ()) -> tuple:
    """Like _get_frontmost_window(), but skips windows owned by any
    process in `exclude_procs` and returns the topmost window that
    isn't one of them.

    This exists for the focus card trap: clicking a button on the
    always-on-top overlay makes the overlay's own process frontmost, so
    a naive "lock whatever's frontmost" would lock the card itself —
    the same failure as locking FRIDAY's main window (see
    actions/focus_session.py's deferred-lock flow), just arriving by a
    different route. When a retarget request is tagged as coming from
    the card, this reads past FRIDAY's own windows to whatever the user
    is actually working in.

    EnumWindows returns top-level windows in Z-order, topmost first, so
    the first visible, titled, non-excluded window is the one behind
    the card. Returns ("", "") if nothing qualifies — callers must
    treat that as "couldn't tell" rather than as a window.
    """
    exclude = {p.strip().lower() for p in exclude_procs if p}
    try:
        if _OS == "Windows":
            import win32gui
            import win32process
            import psutil

            found = []

            def _cb(hwnd, _ctx):
                if not win32gui.IsWindowVisible(hwnd):
                    return True
                title = win32gui.GetWindowText(hwnd) or ""
                if not title.strip():
                    return True
                proc = ""
                try:
                    _, pid = win32process.GetWindowThreadProcessId(hwnd)
                    proc = psutil.Process(pid).name() or ""
                except Exception:
                    return True  # can't identify it — can't rule it out, skip
                if proc.strip().lower() in exclude:
                    return True
                found.append((title, proc))
                return False  # first match wins; stop enumerating

            win32gui.EnumWindows(_cb, None)
            if found:
                return found[0]
            return "", ""

        if _OS == "Darwin":
            out = subprocess.run(
                ["osascript", "-e",
                 'tell application "System Events" to get name of every process '
                 'whose visible is true and background only is false'],
                capture_output=True, timeout=3, text=True,
            )
            names = [n.strip() for n in (out.stdout or "").split(",") if n.strip()]
            for name in names:
                if name.strip().lower() not in exclude:
                    return name, name
            return "", ""
    except Exception as e:
        logger.debug(f"Could not enumerate windows: {e}")
    return "", ""


async def handle_screen_process(args: dict, speak_fn: Optional[Callable] = None) -> str:
    """
    Returns the vision result as text — same contract as every other tool
    handler. This used to be fire-and-forget: dispatched via
    asyncio.create_task() and delivering its result through a SEPARATE
    direct speak_fn() call, completely bypassing the normal round-based
    conversation flow. That's what actually caused the garbled "screen
    awareness" output — the model got back a canned "Vision module
    activated." with no real result, had to guess/stall in its response,
    and then the background task's speak_fn call landed whenever the
    Gemini request finished, unsynchronized with and overlapping whatever
    the main loop was already saying. If the model called screen_process
    twice (which it did, retrying because the first call "looked" like it
    hadn't produced anything yet), TWO background tasks were racing to
    speak independently. Awaiting this normally — a few seconds of
    latency — and returning real text through the same single channel
    every other tool uses fixes all of that at once, not just the
    duplicate-call symptom.
    """
    text = args.get("text", "What's on the screen?")
    angle = args.get("angle", "screen")
    app_name = args.get("app_name", "").strip()
    img_bytes = None
    fallback_note = ""

    # Email-flavored screen_process calls get redirected to real Gmail
    # before any screenshot is even taken — same principle as
    # handle_reminder's redirect to real Calendar: rather than relying on
    # the model reliably picking "gmail" over "screen_process" for email
    # requests (confirmed unreliable, repeatedly — including "click on
    # the email from Concord Logistics", which fell all the way through
    # to a hallucinated shipment-tracking email that didn't exist), make
    # the tool that DOES keep getting called smart enough to do the
    # right thing anyway. Term extraction is shared with actions/gmail.py
    # itself (extract_terms) rather than a separate, weaker stopword list
    # living here too.
    _EMAIL_HINTS = ("email", "emails", "mail", "inbox", "gmail")
    _OPEN_HINTS = ("open", "click", "read", "show me the")
    if any(h in text.lower() for h in _EMAIL_HINTS):
        from actions import google_auth
        if await asyncio.get_running_loop().run_in_executor(None, google_auth.is_connected):
            from actions import gmail as gm
            terms = gm.extract_terms(text)
            wants_open = any(h in text.lower() for h in _OPEN_HINTS)

            if wants_open and terms:
                result = await asyncio.get_running_loop().run_in_executor(
                    None, lambda: gm.open_email(terms)
                )
                if result.lower().startswith("opened"):
                    return result
                # A resolve-miss here still falls through to vision below —
                # same "don't trust a crude query's negative" reasoning as
                # the search branch.
            elif len(terms) >= 2:
                result = await asyncio.get_running_loop().run_in_executor(
                    None, lambda: gm.search(terms, 3)
                )
                # Only short-circuits on an actual match — a "nothing
                # found" from a crudely-extracted query isn't trustworthy
                # enough to report as a real negative, so that case (and
                # any search error) falls through to the normal vision
                # path below instead of confidently claiming absence.
                if result.lower().startswith("found"):
                    return result

    # ── Vision capability precheck — say so rather than capturing a
    # frame we then can't do anything with. Worth knowing: vision does
    # NOT go through config.brain.llm_provider. It has its own route
    # (Groq vision primary, Gemini fallback — see below), deliberately,
    # so that a text provider with no vision support doesn't disable
    # looking at things. This check reports on that real route, and
    # names the active text provider only to explain why it isn't the
    # one being asked.
    _vision_note = _vision_capability_note()
    if _vision_note:
        return _vision_note

    # ── Sensitive-app blocklist — checked BEFORE any capture happens, so
    # a blocked window's content never gets grabbed at all, let alone
    # sent anywhere external. Not applicable to the camera angle (it's
    # not a window). See core/sensitive_apps.json for the editable list.
    if angle != "camera":
        precheck_target = app_name or _get_frontmost_window_title()
        matched = _match_sensitive_pattern(precheck_target)
        if matched:
            logger.info(f"[ScreenProcess] Blocked — {precheck_target!r} matches sensitive pattern {matched!r}")
            return (
                f"That looks like {precheck_target or 'a sensitive app'} — I'm not sending a screenshot "
                f"of that to an external vision service. Switch away from it and ask again if you still "
                f"want a look at the screen, boss."
            )

    try:
        if angle == "camera":
            import cv2
            cap = cv2.VideoCapture(0)
            ret, frame = cap.read()
            cap.release()
            if not ret:
                return "Couldn't access the camera, boss."
            ok, encoded = cv2.imencode(".jpg", frame)
            if not ok:
                return "Couldn't encode the camera frame, boss."
            img_bytes = encoded.tobytes()
            img_format = "jpeg"
        elif app_name and _OS == "Windows":
            # A specific app was named — capture THAT window directly via
            # PrintWindow, which works even if it's minimized or fully
            # covered by other windows. Falls back to a full-screen grab
            # if the window can't be found or PrintWindow fails on it
            # (some apps genuinely don't support it), rather than just
            # erroring out.
            try:
                hwnd = _find_window_by_name(app_name)
                if hwnd:
                    # app_name was already checked against the blocklist
                    # above, but what the user called it and the window's
                    # actual title can differ ("browser" resolving to
                    # "Chase Bank - Sign In - Google Chrome") — check the
                    # real, resolved title too before capturing it.
                    import win32gui
                    real_title = win32gui.GetWindowText(hwnd)
                    real_match = _match_sensitive_pattern(real_title)
                    if real_match:
                        logger.info(f"[ScreenProcess] Blocked — resolved window {real_title!r} "
                                    f"matches sensitive pattern {real_match!r}")
                        return (
                            f"That resolved to '{real_title}' — looks sensitive, so I'm not sending a "
                            f"screenshot of it externally. Switch to something else and ask again if "
                            f"you still need a look, boss."
                        )
                    img_bytes = _capture_window(hwnd)
                if not img_bytes:
                    fallback_note = f"(Couldn't capture '{app_name}' specifically — showing the full screen instead.) "
            except Exception as e:
                logger.warning(f"[ScreenProcess] Window-specific capture failed for {app_name!r}: {e}")
                fallback_note = f"(Couldn't capture '{app_name}' specifically — showing the full screen instead.) "

            if not img_bytes:
                import mss
                from PIL import Image
                import io
                with mss.mss() as sct:
                    shot = sct.grab(sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0])
                img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
                buf = io.BytesIO()
                img.save(buf, format="PNG")
                img_bytes = buf.getvalue()
            img_format = "png"
        else:
            import mss
            from PIL import Image
            import io
            with mss.mss() as sct:
                shot = sct.grab(sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0])
            img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            img_bytes = buf.getvalue()
            img_format = "png"

        # Analyze with vision — Groq primary (same key already used for
        # intent routing, sidesteps the Gemini free-tier quota wall
        # entirely), Gemini kept as a secondary fallback in case Groq
        # itself has an outage.
        import base64
        img_b64 = base64.b64encode(img_bytes).decode("utf-8")

        # The raw user query (e.g. "calendar events and schedule") is NOT
        # sent to the vision model as-is. Without an explicit "only
        # describe what's actually there" instruction, a query for
        # something that isn't in the screenshot is an open invitation
        # for the model to generate a plausible-sounding example instead
        # of admitting it isn't there — confirmed happening: asked for
        # "calendar events and schedule" against a screenshot with no
        # calendar on it, Groq vision returned a fully fabricated,
        # detailed schedule (specific project names, specific dates)
        # with no relationship to anything on screen or to the real
        # date. Grounding both with the real current time (the model has
        # no other way to know it) and an explicit anti-fabrication
        # instruction closes that off at the source — this is the one
        # place it can be closed, since nothing downstream can tell a
        # confident fabrication apart from a genuine reading.
        from datetime import datetime as _dt
        vision_prompt = (
            f"The real current date and time is {_dt.now().strftime('%A, %B %d, %Y, %I:%M %p')}. "
            f"Describe ONLY what is actually visible in this image — never invent, assume, or fill "
            f"in plausible-sounding placeholder content that isn't really there, even if it's the "
            f"kind of thing you'd expect to see. If what's being asked about isn't visible in the "
            f"image, say plainly that it isn't there — do not generate an example of what it might "
            f"look like instead.\n\nRequest: {text}"
        )

        result = None
        try:
            from groq import Groq
            from config import config as _cfg
            from memory import usage_tracker as ut

            if not _cfg.brain.groq_api_key:
                raise RuntimeError("GROQ_API_KEY not set")

            client = Groq(api_key=_cfg.brain.groq_api_key)
            ut.record_call("groq_vision")
            raw = client.chat.completions.with_raw_response.create(
                model=_cfg.brain.groq_vision_model,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": vision_prompt},
                        {"type": "image_url", "image_url": {"url": f"data:image/{img_format};base64,{img_b64}"}},
                    ],
                }],
                max_tokens=500,
            )
            try:
                from brain.llm import _record_groq_rate_limit_headers
                _record_groq_rate_limit_headers(raw.headers, "groq_vision")
            except Exception:
                pass
            response = raw.parse()
            result = response.choices[0].message.content.strip()
            ut.mark_ok("groq_vision")
        except Exception as e:
            err_str = str(e)
            logger.warning(f"Groq vision failed, trying Gemini fallback: {e}")
            try:
                from memory import usage_tracker as ut
                if "rate_limit" in err_str.lower() or "429" in err_str:
                    ut.mark_exhausted("groq_vision")
            except Exception:
                pass

            # Gemini's upload API needs an actual file path, not raw
            # bytes — unlike Groq's base64 data-URI approach above. Uses
            # a temp file in the OS temp dir (auto-cleaned by the OS,
            # never visible anywhere) rather than Desktop, and deletes it
            # immediately after — this is the one remaining disk touch,
            # confined to the fallback path only.
            tmp_path = None
            try:
                import tempfile
                import google.genai as genai
                from config import config as _cfg
                suffix = ".jpg" if img_format == "jpeg" else ".png"
                with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tf:
                    tf.write(img_bytes)
                    tmp_path = tf.name
                _genai_client = genai.Client(api_key=_cfg.brain.gemini_api_key)
                img_file = _genai_client.files.upload(file=tmp_path)
                response = _genai_client.models.generate_content(
                    model=_cfg.brain.gemini_model, contents=[vision_prompt, img_file]
                )
                result = response.text.strip()
            except Exception as e2:
                err_str2 = str(e2)
                if "RESOURCE_EXHAUSTED" in err_str2 or "429" in err_str2:
                    result = (
                        "Vision's out of quota on both Groq and Gemini, boss — "
                        "check the terminal for which one and why."
                    )
                else:
                    result = "Couldn't get a vision model to respond right now, boss."
            finally:
                if tmp_path:
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass

        if fallback_note:
            result = fallback_note + result

        try:
            from ui.ws_server import send_screen_description
            await send_screen_description(result)
        except Exception:
            pass

        return result

    except Exception as e:
        logger.error(f"Screen process error: {e}")
        return f"Couldn't analyze the screen: {e}"


# ── Reminder ─────────────────────────────────────────────────────────────────

async def _push_watchlist_update():
    """Best-effort push of the current monitors+reminders state to the
    UI. Never lets a UI-push failure break the actual add/remove — the
    action already succeeded by the time this is called."""
    try:
        from ui.ws_server import send_watchlist_update
        await send_watchlist_update()
    except Exception:
        pass


async def handle_reminder(args: dict) -> str:
    action = (args.get("action") or "set").strip().lower()

    if action == "list":
        from actions import reminder_store
        reminders = await asyncio.get_running_loop().run_in_executor(None, reminder_store.list_reminders)
        local_block = ("No reminders pending, boss." if not reminders else
                       "Pending reminders:\n" + "\n".join(f"{r['date']} {r['time']} — {r['message']}" for r in reminders))

        # The model picking `reminder` instead of `calendar` for a
        # "what's on my calendar" style question has proven to happen
        # reliably regardless of how the two tools' schemas are worded
        # (three separate wording attempts, three repeats of the same
        # misroute) — so rather than continuing to try to out-word a
        # small local model's tool-selection bias, list also checks the
        # real calendar directly whenever one's connected, so the answer
        # is right no matter which tool got called.
        from actions import google_auth
        if google_auth.is_connected():
            from actions import calendar as gcal
            cal_block = await asyncio.get_running_loop().run_in_executor(None, gcal.list_events)
            cal_empty_or_failed = cal_block.strip().lower().startswith(("nothing on the calendar", "couldn't"))
            if cal_empty_or_failed and not reminders:
                return cal_block  # "Nothing on the calendar today." is a complete answer on its own
            if cal_empty_or_failed:
                return local_block  # calendar's empty, but there ARE local reminders — show those instead
            if not reminders:
                return cal_block  # calendar has content, no local reminders — no need to add "none pending" too
            return f"{cal_block}\n\n{local_block}"  # both have something — show both
        return local_block

    if action in ("remove", "cancel", "delete"):
        query = args.get("query") or args.get("message") or ""

        # Mirrors the "add" redirect above: once connected, "add" writes
        # straight to Google Calendar and never touches the local
        # reminder_store at all — so a remove request has to check
        # Calendar FIRST, or it'll only ever look in a store the item
        # was never actually written to (confirmed: this exact gap is
        # why "remove 'Test'" failed right after "add 'Test'" succeeded
        # as a real calendar event — the add went to Calendar, the
        # remove only ever checked the local reminder file).
        from actions import google_auth
        if google_auth.is_connected():
            from actions import calendar as gcal
            cal_result = await asyncio.get_running_loop().run_in_executor(
                None, lambda: gcal.delete_event(query)
            )
            if not cal_result.strip().lower().startswith("couldn't find an event matching"):
                return cal_result  # deleted, found multiple matches, or a real Calendar error — handled

        from actions import reminder_store
        matches = await asyncio.get_running_loop().run_in_executor(None, reminder_store.find_by_query, query)
        if not matches:
            return f"Couldn't find a pending reminder matching '{query}', boss."
        if len(matches) > 1:
            options = "; ".join(f"{m['date']} {m['time']} — {m['message']}" for m in matches)
            return f"Found more than one match — which did you mean? {options}"
        removed = matches[0]
        ok = await asyncio.get_running_loop().run_in_executor(
            None, reminder_store.remove_reminder, removed["task_name"]
        )
        if ok:
            await _push_watchlist_update()
            return f"Cancelled: {removed['message']} ({removed['date']} {removed['time']})."
        return f"Couldn't cancel that reminder, boss."

    date = args.get("date", "")
    time_str = args.get("time", "")
    message = args.get("message", "Reminder")

    # Strict format validation closes off injection via date/time entirely:
    # these two fields have exactly one valid shape, so anything else is
    # rejected outright rather than passed through to a script.
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        return "I need a date in YYYY-MM-DD format to set a reminder."
    if not re.fullmatch(r"\d{2}:\d{2}", time_str):
        return "I need a time in HH:MM format to set a reminder."

    # Same reasoning as the list branch above: "add an event"/"add this
    # to my calendar" keeps landing on this tool instead of calendar's
    # create action, no matter how create's schema is worded. Once a
    # Google account is connected, treat that as a strong enough signal
    # that a real calendar event is what's actually wanted here — this
    # tool falls back to the plain local OS notification below ONLY when
    # nothing is connected, so nothing changes for anyone who hasn't set
    # up Calendar at all.
    from actions import google_auth
    if google_auth.is_connected():
        from actions import calendar as gcal
        return await asyncio.get_running_loop().run_in_executor(
            None, lambda: gcal.create_event(message, date, time_str)
        )

    try:
        if _OS == "Windows":
            # task_name is built only from the validated digits above, so
            # it's guaranteed alphanumeric/underscore — safe as both a
            # scheduled-task name and a filename. It's kept as a pure
            # function of date+time (no random suffix) because Verifier's
            # _verify_reminder independently reconstructs this same name
            # from the tool-call args alone to re-check that the task
            # really exists — a random suffix here would break that check.
            task_name = f"FRIDAY_Reminder_{date.replace('-', '')}_{time_str.replace(':', '')}"

            reminders_dir = Path.home() / ".friday" / "reminders"
            reminders_dir.mkdir(parents=True, exist_ok=True)

            # The message is written to disk as plain data, never spliced
            # into any script or command-line text, so it can't be parsed
            # as PowerShell syntax no matter what characters it contains.
            msg_path = reminders_dir / f"{task_name}.msg.txt"
            msg_path.write_text(message, encoding="utf-8")

            ps1_path = reminders_dir / f"{task_name}.ps1"
            ps1_path.write_text(
                "$msgPath = Join-Path $PSScriptRoot ((Split-Path -Leaf $PSCommandPath) "
                "-replace '\\.ps1$', '.msg.txt');\n"
                "$msg = Get-Content -Raw -Encoding UTF8 -LiteralPath $msgPath;\n"
                "Add-Type -AssemblyName System.Windows.Forms;\n"
                "[System.Windows.Forms.MessageBox]::Show($msg, 'FRIDAY Reminder') | Out-Null;\n",
                encoding="utf-8",
            )

            # Everything below is static PowerShell. The only dynamic values
            # (task name, trigger time, script path) are passed in as data
            # via environment variables — never interpolated into the
            # script text — the same fix applied to _focus_window.
            script = (
                '$trigger = New-ScheduledTaskTrigger -Once -At $env:FRIDAY_R_WHEN; '
                '$action = New-ScheduledTaskAction -Execute "powershell" '
                '-Argument (\'-NoProfile -WindowStyle Hidden -File "\' + $env:FRIDAY_R_SCRIPT + \'"\'); '
                'Register-ScheduledTask -TaskName $env:FRIDAY_R_TASK -Trigger $trigger -Action $action -Force | Out-Null'
            )
            env = os.environ.copy()
            env["FRIDAY_R_WHEN"] = f"{date} {time_str}"
            env["FRIDAY_R_TASK"] = task_name
            env["FRIDAY_R_SCRIPT"] = str(ps1_path)

            # subprocess.run (not Popen) — this WAITS for Register-ScheduledTask
            # to actually finish and checks its result, rather than firing the
            # PowerShell process and immediately claiming success. Popen here
            # was a real bug: launching powershell.exe has real startup
            # latency (spinning up the process, loading the ScheduledTasks
            # module), so a fire-and-forget call could report "Reminder set"
            # before the task was actually registered — or even if
            # Register-ScheduledTask itself failed outright (permissions,
            # execution policy, etc.), nothing would ever have checked.
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                None,
                lambda: subprocess.run(
                    ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                    creationflags=subprocess.CREATE_NO_WINDOW, env=env,
                    capture_output=True, text=True, timeout=20,
                ),
            )
            if result.returncode != 0:
                err = (result.stderr or result.stdout or "unknown error").strip()
                return f"Couldn't actually set that reminder, boss — Windows Task Scheduler rejected it: {err[:200]}"

            from actions import reminder_store
            reminder_store.add_reminder(task_name, date, time_str, message)
            await _push_watchlist_update()

            return f"Reminder set for {date} at {time_str}: {message}"
        else:
            return f"Reminder noted: {message} at {date} {time_str}."
    except subprocess.TimeoutExpired:
        return "Couldn't set that reminder, boss — Windows Task Scheduler didn't respond in time."
    except Exception as e:
        return f"Reminder failed: {e}"


# ── Calendar (real Google Calendar — see actions/calendar.py) ─────────────────

async def handle_calendar(args: dict) -> str:
    action = (args.get("action") or "list").strip().lower()
    loop = asyncio.get_running_loop()
    from actions import calendar as gcal

    if action in ("list", "check", "view"):
        date = args.get("date") or None
        days = int(args.get("days") or 1)
        return await loop.run_in_executor(None, gcal.list_events, date, days)

    if action in ("next", "upcoming"):
        return await loop.run_in_executor(None, gcal.get_next_event)

    if action in ("create", "add", "set", "schedule"):
        summary = args.get("summary") or args.get("message") or args.get("title") or ""
        date = args.get("date") or ""
        time_str = args.get("time") or None
        all_day = bool(args.get("all_day"))
        duration = int(args.get("duration_minutes") or 60)
        location = args.get("location") or ""
        description = args.get("description") or ""
        return await loop.run_in_executor(
            None, lambda: gcal.create_event(
                summary, date, time_str, duration, all_day, location, description
            )
        )

    if action in ("delete", "remove", "cancel"):
        query = args.get("query") or args.get("summary") or args.get("message") or ""
        date = args.get("date") or None
        return await loop.run_in_executor(None, lambda: gcal.delete_event(query, date))

    return f"I don't know the calendar action '{action}', boss — try list, create, or delete."


# ── Gmail (real Gmail — see actions/gmail.py) ──────────────────────────────────

async def handle_gmail(args: dict) -> str:
    action = (args.get("action") or "list").strip().lower()
    loop = asyncio.get_running_loop()
    from actions import gmail

    if action in ("list", "unread", "check"):
        max_results = int(args.get("max_results") or 5)
        return await loop.run_in_executor(None, lambda: gmail.list_unread(max_results))

    if action == "search":
        query = args.get("query") or ""
        max_results = int(args.get("max_results") or 5)
        return await loop.run_in_executor(None, lambda: gmail.search(query, max_results))

    if action in ("read", "open_email", "view"):
        ref = args.get("query") or ""
        return await loop.run_in_executor(None, lambda: gmail.read_email(ref))

    if action == "open":
        ref = args.get("query") or ""
        return await loop.run_in_executor(None, lambda: gmail.open_email(ref))

    if action in ("draft", "draft_reply"):
        query = args.get("query") or ""
        body = args.get("body") or ""
        return await loop.run_in_executor(None, lambda: gmail.draft_reply(query, body))

    if action in ("send", "reply"):
        to = args.get("to") or ""
        subject = args.get("subject") or ""
        body = args.get("body") or ""
        in_reply_to = args.get("query") or None
        return await loop.run_in_executor(
            None, lambda: gmail.send_email(to, subject, body, in_reply_to)
        )

    return f"I don't know the gmail action '{action}', boss — try list, search, read, open, draft, or send."


# ── Automations (see actions/automations.py) ───────────────────────────────────

async def handle_automation(args: dict) -> str:
    action = (args.get("action") or "create").strip().lower()
    loop = asyncio.get_running_loop()
    from actions import automations as autom

    if action == "list":
        return await loop.run_in_executor(None, autom.list_rules)

    if action in ("delete", "remove", "cancel"):
        query = args.get("query") or ""
        return await loop.run_in_executor(None, lambda: autom.remove_rule(query))

    if action in ("enable", "disable"):
        query = args.get("query") or ""
        return await loop.run_in_executor(None, lambda: autom.set_enabled(query, action == "enable"))

    if action in ("create", "add", "set"):
        ttype = args.get("trigger_type") or ""
        trigger = {"type": ttype}
        if ttype == "daily_time":
            trigger["time"] = args.get("trigger_time") or ""
        elif ttype == "interval_minutes":
            trigger["minutes"] = args.get("trigger_minutes")
        elif ttype == "before_event":
            trigger["minutes_before"] = args.get("trigger_minutes")
            trigger["query"] = args.get("trigger_query") or ""
        elif ttype == "new_email_from":
            trigger["query"] = args.get("trigger_query") or ""

        atype = args.get("action_type") or ""
        action_dict = {"type": atype}
        if atype == "speak":
            action_dict["message"] = args.get("speak_message") or ""
        elif atype == "tool":
            tool_name = args.get("tool_name") or ""
            tool_args = args.get("tool_args") or {}
            # Automations fire unattended — there's no one there for
            # Sentinel's HIGH-risk confirmation prompt to even reach, so
            # "hold for confirmation" can't apply the way it does in a
            # live conversation. The only safe option is an outright
            # block at creation time, not a silent skip when it fires:
            # nothing that sends an email or deletes a calendar event
            # gets to run unattended, ever.
            from sentinel.core import classify_risk, RiskLevel
            if classify_risk(tool_name, tool_args) == RiskLevel.HIGH:
                return (
                    f"Couldn't create that automation — {tool_name} with those args is a "
                    f"HIGH-risk action (sending, deleting, etc.), and automations run unattended "
                    f"with nobody there to confirm. Automations can't do anything at that risk "
                    f"level, by design — set that one up manually instead when you're around."
                )
            action_dict["tool"] = tool_name
            action_dict["args"] = tool_args

        description = args.get("description") or ""
        return await loop.run_in_executor(None, lambda: autom.add_rule(trigger, action_dict, description))

    return f"I don't know the automation action '{action}', boss — try create, list, delete, enable, or disable."


# ── Morning Briefing — the payoff feature, stitching everything above together ─

async def handle_morning_briefing(args: dict = None) -> str:
    args = args or {}
    loop = asyncio.get_running_loop()
    from config import config
    parts = []

    city = args.get("city") or config.integrations.home_city
    if city:
        try:
            weather = await handle_weather(city)
            parts.append(f"Weather: {weather}")
        except Exception as e:
            parts.append(f"Weather: couldn't fetch it ({e}).")
    # No 'else' clause here — no city set is a config gap, not a
    # failure worth cluttering the briefing with an error line about.

    from actions import google_auth
    connected = await loop.run_in_executor(None, google_auth.is_connected)

    if connected:
        from actions import calendar as gcal
        cal = await loop.run_in_executor(None, gcal.list_events)
        parts.append(f"Calendar: {cal}")
    else:
        parts.append("Calendar: not connected — run google_auth_setup.py to include this.")

    from actions import reminder_store
    reminders = await loop.run_in_executor(None, reminder_store.list_reminders)
    if reminders:
        lines = "; ".join(f"{r['time']} {r['message']}" for r in reminders)
        parts.append(f"Reminders: {lines}")

    if connected:
        from actions import gmail as gm
        inbox = await loop.run_in_executor(None, lambda: gm.list_unread(5))
        parts.append(f"Inbox: {inbox}")

    try:
        from actions import topic_monitor
        alerts = await loop.run_in_executor(None, topic_monitor.check_all)
        if alerts:
            parts.append("Watched topics: " + " | ".join(alerts[:3]))
    except Exception:
        pass  # topic monitoring is a bonus section here, never worth failing the whole briefing over

    if not parts:
        return ("Nothing to brief you on, boss — no home city set, Google not connected, "
                "and no local reminders. Set HOME_CITY in .env and run google_auth_setup.py "
                "to get a real briefing here.")

    return "Morning briefing:\n\n" + "\n\n".join(parts)


# ── Code Helper ──────────────────────────────────────────────────────────────

async def handle_code_helper(args: dict, speak_fn: Optional[Callable] = None) -> str:
    action = args.get("action", "write")
    description = args.get("description", "")
    language = args.get("language", "python")
    output_path = args.get("output_path", "")
    file_path = args.get("file_path", "")

    try:
        import google.genai as genai
        from config import config as _cfg
        _genai_client = genai.Client(api_key=_cfg.brain.gemini_api_key)
        _genai_model = _cfg.brain.gemini_model

        if action == "write":
            prompt = f"Write complete, clean {language} code to: {description}\nReturn ONLY the code. No explanation."
            resp = _genai_client.models.generate_content(model=_genai_model, contents=prompt)
            code = re.sub(r"```\w*", "", resp.text).strip().strip("`")
            ext = {"python": "py", "javascript": "js", "typescript": "ts",
                   "html": "html", "css": "css", "bash": "sh"}.get(language, language[:3])
            dest = Path(output_path) if output_path else Path.home() / "Desktop" / f"friday_code.{ext}"
            dest.write_text(code, encoding="utf-8")
            return f"Code written to {dest.name}, boss."

        elif action == "explain" and file_path:
            code = Path(file_path).read_text(encoding="utf-8")[:3000]
            resp = _genai_client.models.generate_content(model=_genai_model, contents=f"Explain this code briefly, like talking to a developer:\n{code}")
            return resp.text.strip()

        elif action == "run" and file_path:
            result = subprocess.run(
                ["python", file_path],
                capture_output=True, text=True, timeout=30
            )
            output = result.stdout.strip() or result.stderr.strip()
            return output[:400] if output else "Ran with no output."

        else:
            return f"Action '{action}' needs a description or file_path, boss."

    except Exception as e:
        return f"Code helper failed: {e}"


# ── Power Control ─────────────────────────────────────────────────────────────

async def handle_power(user_text_or_action: str) -> str:
    """Accepts either a raw action keyword OR full user text and extracts the action."""
    text = user_text_or_action.lower().strip()

    # Extract action keyword from natural language
    if   any(w in text for w in ["shut down", "shutdown", "turn off", "power off"]):
        a = "shutdown"
    elif any(w in text for w in ["restart", "reboot", "restarting"]):
        a = "restart"
    elif any(w in text for w in ["sleep", "suspend", "hibernate", "standby"]):
        a = "sleep" if "hiber" not in text else "hibernate"
    elif any(w in text for w in ["lock", "lock screen", "lock my"]):
        a = "lock"
    else:
        a = text  # already a clean action keyword
    try:
        if _OS == "Windows":
            cmds = {
                "shutdown":  ["shutdown", "/s", "/t", "30"],
                "restart":   ["shutdown", "/r", "/t", "30"],
                "sleep":     ["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"],
                "hibernate": ["shutdown", "/h"],
                "lock":      ["rundll32.exe", "user32.dll,LockWorkStation"],
            }
        elif _OS == "Darwin":
            cmds = {
                "shutdown":  ["sudo", "shutdown", "-h", "+1"],
                "restart":   ["sudo", "shutdown", "-r", "+1"],
                "sleep":     ["pmset", "sleepnow"],
                "lock":      ["pmset", "displaysleepnow"],
            }
        else:
            cmds = {
                "shutdown":  ["shutdown", "-h", "+1"],
                "restart":   ["shutdown", "-r", "+1"],
                "sleep":     ["systemctl", "suspend"],
                "lock":      ["loginctl", "lock-session"],
            }

        cmd = cmds.get(a)
        if cmd:
            subprocess.Popen(cmd, creationflags=subprocess.CREATE_NO_WINDOW if _OS == "Windows" else 0)
            msgs = {
                "shutdown": "Shutting down in 30 seconds. Type 'shutdown /a' to cancel.",
                "restart": "Restarting in 30 seconds, boss.",
                "sleep": "Going to sleep, boss.",
                "hibernate": "Hibernating, boss.",
                "lock": "Screen locked, boss.",
            }
            return msgs.get(a, f"{a} initiated, boss.")
        return f"Unknown power action: {a}"
    except Exception as e:
        return f"Power control failed: {e}"


# ── Timer ─────────────────────────────────────────────────────────────────────

async def handle_timer(user_text: str) -> str:
    match = re.search(r"(\d+)\s*(second|minute|hour|min|sec|s\b|m\b|h\b)", user_text.lower())
    if not match:
        return "How long should the timer be, boss?"

    amount = int(match.group(1))
    unit = match.group(2)
    seconds = amount * (60 if unit in ("minute", "min", "m") else
                        3600 if unit in ("hour", "h") else 1)

    async def _fire():
        await asyncio.sleep(seconds)
        try:
            from ui.ws_server import send_toast
            await send_toast(f"⏰ Timer done! ({amount} {unit}s)", "info")
        except Exception:
            pass

    asyncio.create_task(_fire())
    return f"Timer set for {amount} {unit}{'s' if amount > 1 else ''}, boss."


# ── Stop / Clear ──────────────────────────────────────────────────────────────

async def handle_stop(user_text: str) -> str:
    try:
        from ui.ws_server import _stop_event
        _stop_event.set()
    except Exception:
        pass
    return ""


async def handle_clear(user_text: str) -> str:
    from brain.llm import clear_memory
    clear_memory()
    return "Memory cleared. Fresh start, boss."

# ── Memory handlers ───────────────────────────────────────────────────────────

async def handle_memory_save(user_text: str) -> str:
    """
    Extracts structured facts from user text and saves them via long_term.remember_many().
    Save path: rule-based parsing first (instant, reliable), LLM extraction as enhancement.
    Never falls back to unstructured raw notes.
    """
    try:
        from memory.long_term import remember_many, remember

        # Strip command prefixes
        text = user_text.strip()
        for prefix in ["remember that", "remember everything about me", "remember",
                        "note that", "note:", "note ", "save this:", "save this",
                        "don't forget", "keep in mind", "always know that"]:
            if text.lower().startswith(prefix):
                text = text[len(prefix):].strip()
                break

        if not text:
            return "What would you like me to remember, boss?"

        # ── Rule-based fast path (no LLM, instant, handles common patterns) ──
        import re as _re
        rule_facts = []

        patterns = [
            (r"\bmy name is ([A-Za-z\s]+)",        "identity", "name"),
            (r"\bi(?:'m| am) ([A-Za-z\s]+) years old", "identity", "age"),
            (r"\bi(?:'m| am) (\d+)",               "identity", "age"),
            (r"\bi live in ([A-Za-z\s,]+)",        "identity", "city"),
            (r"\bi(?:'m| am) from ([A-Za-z\s,]+)", "identity", "city"),
            (r"\bi work (?:at|for|as) ([A-Za-z\s]+)", "identity", "job"),
            (r"\bmy (?:favorite |fav )?song is ([^,\.]+)",       "preferences", "favorite_song"),
            (r"\bmy (?:favorite |fav )?music is ([^,\.]+)",      "preferences", "favorite_music"),
            (r"\bmy (?:favorite |fav )?project is ([^,\.]+)",    "notes",    "favorite_project"),
            (r"\bi(?:'m| am) working on ([^,\.]+)",              "notes",    "current_project"),
            (r"\bmy (?:favorite |fav )?language is ([A-Za-z\+\#]+)", "preferences", "favorite_language"),
        ]

        text_lower = text.lower()
        for pattern, cat, key in patterns:
            m = _re.search(pattern, text_lower)
            if m:
                val = m.group(1).strip().rstrip(".,;")
                # Title-case names/places, keep rest as-is
                if key in ("name", "city"):
                    val = val.title()
                rule_facts.append({"category": cat, "key": key, "value": val})

        saved = remember_many(rule_facts) if rule_facts else 0

        # ── LLM enhancement: catch anything the rules missed ──────────────
        extraction_prompt = (
            f"Extract ALL personal facts from: {text!r}\n"
            "Return a JSON array. Each item: "
            "{\"category\":\"identity|preferences|projects|relationships|wishes|notes\","
            "\"key\":\"snake_case\",\"value\":\"concise\"}.\n"
            "Examples: name, age, city, job, favorite_song, favorite_project, "
            "favorite_language, relationship to people.\n"
            "Return [] if nothing. Return ONLY a JSON array, no markdown, no explanation."
        )

        try:
            import json as _json, re as _re2
            raw = await _quick_llm(extraction_prompt, max_tokens=400)
            raw = _re2.sub(r"```json|```", "", raw).strip()
            match = _re2.search(r"\[.*\]", raw, _re2.DOTALL)
            if match:
                llm_facts = _json.loads(match.group(0))
                # Don't overwrite what rules already found
                existing_keys = {f["key"] for f in rule_facts}
                new_facts = [f for f in llm_facts
                             if f.get("key") and f.get("value")
                             and f["key"] not in existing_keys]
                saved += remember_many(new_facts)
        except Exception as e:
            logger.debug(f"[Memory] LLM enhancement skipped: {e}")
            # Rule-based facts were already saved — this is fine

        total = saved
        if total == 0:
            # Save the raw text as a note in long_term rather than dropping it
            remember(text[:200], text[:200], "notes")
            return "Got it, boss. I'll remember that."
        elif total == 1:
            return "Got it, boss."
        else:
            return f"Got it, boss. Saved {total} things about you."

    except Exception as e:
        logger.error(f"handle_memory_save error: {e}")
        return "Couldn't save that, boss. Try again."


async def handle_memory_recall(user_text: str) -> str:
    """
    Answers 'what do you know about me' using long_term structured memory.
    Recall path: reads directly from long_term.py — no LLM required.
    LLM used only to polish the sentence if available.
    """
    try:
        from memory.long_term import get_all, as_natural_summary, get_category
        from memory.memory_store import get_notes

        memory = get_all()
        parts  = as_natural_summary()
        raw_notes = get_notes(limit=5)

        # Add raw notes from SQLite if any
        for n in raw_notes:
            if n and n not in parts:
                parts.append(n)

        if not parts:
            return "I don't have anything stored about you yet, boss. Tell me some things and I'll remember them."

        # ── Try LLM polish (nice-to-have, not required) ────────────────────
        facts_str = "; ".join(parts)
        recall_prompt = (
            "You are F.R.I.D.A.Y., a sharp voice assistant. "
            "Tell the user what you know about them in 2-3 natural sentences. "
            "Address them as 'boss'. No lists, no bullet points, no raw key names. "
            "Speak as if you genuinely know them.\n\n"
            f"Facts: {facts_str}"
        )

        try:
            return await _quick_llm(recall_prompt, max_tokens=150)
        except Exception:
            pass

        # ── No LLM available — build natural sentence directly ─────────────
        identity = get_category("identity")
        sentence_parts = []

        name = identity.get("name")
        age  = identity.get("age")
        city = identity.get("city")
        job  = identity.get("job")

        if name:
            opening = f"You're {name}"
            if age:  opening += f", {age} years old"
            if city: opening += f", based in {city}"
            sentence_parts.append(opening)
        elif age:
            sentence_parts.append(f"You're {age} years old")

        prefs = get_category("preferences")
        pref_lines = [f"{k.replace('_', ' ')} is {v}" for k, v in list(prefs.items())[:3]]
        if pref_lines:
            sentence_parts.append("Your " + ", your ".join(pref_lines))

        if not sentence_parts:
            sentence_parts = parts[:3]

        return "Here's what I've got on you, boss: " + ". ".join(sentence_parts) + "."

    except Exception as e:
        logger.error(f"handle_memory_recall error: {e}")
        return "Had trouble reading memory, boss."


async def handle_pronoun_stop() -> str:
    """Resolves a bare 'kill it' / 'stop it' / 'close it' / 'pause it'
    against brain.llm's last-mentioned-thing tracker (last app opened,
    last topic added/removed, last song played), then routes to the
    same tool the explicit-name version would use. Called only when the
    router has already confirmed something is actually tracked."""
    from brain.llm import get_last_mentioned, clear_last_mentioned, _dispatch_tool

    last = get_last_mentioned()
    if not last:
        return "Not sure what 'it' refers to, boss — nothing recent to go on."

    kind, label, ref = last["kind"], last["label"], last["ref"]

    if kind == "app":
        # Focus first and verify it actually landed before closing —
        # blindly Alt+F4ing whatever currently has focus could close the
        # wrong window entirely (including FRIDAY's own).
        focus_result = await _dispatch_tool("computer_control", {"action": "focus_window", "title": ref})
        if not focus_result.startswith("Focused window:"):
            return f"Couldn't bring {label} to the front to close it, boss — {focus_result}"
        await _dispatch_tool("computer_settings", {"value": "alt+f4"})
        clear_last_mentioned()
        return f"Closed {label}, boss."

    elif kind == "topic":
        result = await _dispatch_tool("topic_monitor", {"action": "remove", "topic": ref})
        clear_last_mentioned()
        return result

    elif kind == "spotify":
        result = await _dispatch_tool("spotify_control", {"action": "pause"})
        clear_last_mentioned()
        return result

    return "Not sure what 'it' refers to, boss — nothing recent to go on."


async def handle_topic_monitor(args: dict) -> str:
    """Add/remove/list watched topics (daily headline checks). Ported
    from Mark-L's background_monitor.py."""
    from actions import topic_monitor

    action = (args.get("action") or "").strip().lower()
    topic = args.get("topic", "")

    loop = asyncio.get_running_loop()
    if action == "add":
        result = await loop.run_in_executor(None, topic_monitor.add_monitor, topic)
        await _push_watchlist_update()
        return result
    elif action == "remove":
        result = await loop.run_in_executor(None, topic_monitor.remove_monitor, topic)
        await _push_watchlist_update()
        return result
    elif action == "list":
        topics = await loop.run_in_executor(None, topic_monitor.list_monitors)
        if not topics:
            return "You're not monitoring any topics right now, boss."
        return "Monitoring: " + ", ".join(topics)
    else:
        return f"Unknown topic_monitor action: {action}"


async def handle_open_loop(args: dict) -> str:
    """Add/resolve/list open loops — see memory/open_loops.py for why
    this is a separate, explicit-only mechanism rather than something
    inferred from conversation history."""
    from memory import open_loops

    action = (args.get("action") or "").strip().lower()
    text = args.get("text", "")

    loop = asyncio.get_running_loop()
    if action == "add":
        result = await loop.run_in_executor(None, open_loops.add_loop, text)
        await _push_watchlist_update()
        return result
    elif action == "resolve":
        query = args.get("query") or text
        result = await loop.run_in_executor(None, open_loops.resolve_loop, query)
        await _push_watchlist_update()
        return result
    elif action == "list":
        loops = await loop.run_in_executor(None, open_loops.list_open)
        if not loops:
            return "No open loops right now, boss."
        return "Still open: " + "; ".join(l["text"] for l in loops)
    else:
        return f"Unknown open_loop action: {action}"