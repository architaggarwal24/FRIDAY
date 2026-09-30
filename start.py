"""
F.R.I.D.A.Y. — start.py
Single entry point. Starts Python backend + Electron UI.

Usage:
    python start.py              # full voice mode (push-to-talk)
    python start.py --wake       # wake-word mode ("Hey FRIDAY")
    python start.py --text       # text-only mode (no mic/TTS)
    python start.py --no-ui      # backend only, no Electron window
    python start.py --debug      # verbose logging
"""

import argparse
import asyncio
import logging
import os
import random
import re
import subprocess
import sys
import time

try:
    import dll_fix  # noqa — MUST be first on Windows
except ImportError:
    pass

# Set by run(), read by _shutdown_friday() — the existing cleanup path in
# run()'s finally block can't run when we exit via os._exit() (it skips
# Python's normal finally/exception handling entirely), so anything that
# needs to happen on shutdown — including killing the Electron window —
# has to be reachable from here instead.
_electron_proc_ref = None


def _banner():
    print("\033[33m")
    print("  ███████╗██████╗ ██╗██████╗  █████╗ ██╗   ██╗")
    print("  ██╔════╝██╔══██╗██║██╔══██╗██╔══██╗╚██╗ ██╔╝")
    print("  █████╗  ██████╔╝██║██║  ██║███████║ ╚████╔╝ ")
    print("  ██╔══╝  ██╔══██╗██║██║  ██║██╔══██║  ╚██╔╝  ")
    print("  ██║     ██║  ██║██║██████╔╝██║  ██║   ██║   ")
    print("  ╚═╝     ╚═╝  ╚═╝╚═╝╚═════╝ ╚═╝  ╚═╝   ╚═╝  ")
    print()
    print("  Female Replacement Intelligent Digital Assistant Youth")
    print("  ───────────────────────────────────────────────────────────")
    print("\033[0m")


def _start_electron():
    ui_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui")
    candidates = [
        os.path.join(ui_dir, "node_modules", ".bin", "electron.cmd"),
        os.path.join(ui_dir, "node_modules", ".bin", "electron"),
        os.path.join(ui_dir, "node_modules", "electron", "dist", "electron.exe"),
    ]
    electron_bin = next((c for c in candidates if os.path.exists(c)), None)
    cmd = [electron_bin, "."] if electron_bin else ["npx", "--yes", "electron", "."]
    try:
        from config import config
        env = os.environ.copy()
        env["WS_AUTH_TOKEN"] = config.ws_auth_token

        flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
        proc = subprocess.Popen(cmd, cwd=ui_dir, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, creationflags=flags, env=env)
        logging.getLogger("friday").info(f"Electron launched (PID {proc.pid})")
        return proc
    except Exception as e:
        logging.getLogger("friday").error(f"Electron launch failed: {e}")
        return None


def _pick_ack(text: str) -> str:
    """Short filler said immediately while LLM generates — kills dead air."""
    t = text.lower().strip()
    if len(t.split()) < 4:
        return ""
    if any(w in t for w in ["what", "how", "why", "explain", "tell me", "describe"]):
        return random.choice(["Let me check.", "One sec.", ""])
    if any(w in t for w in ["can you", "could you", "please", "i need", "i want", "open", "play"]):
        return random.choice(["On it, boss.", "Working on it.", ""])
    return ""


async def _proactive_monitor(ws_module, speak_fn):
    """Alerts on high CPU, low battery, GPU temp/load/VRAM, watched
    topics, and stale open loops. The topic_monitor / open_loops checks
    used to only run once at boot (see _build_greeting) — meaning a
    session left running all day never re-checked either. This loop was
    already running every 60s for system health; piggybacking the other
    two onto it (each on their own, much longer interval) means every
    "notices things on its own" surface now actually runs continuously,
    not just at startup."""
    log = logging.getLogger("friday.monitor")
    last_cpu = last_bat = 0
    last_topic_check = last_loop_check = time.time()
    TOPIC_CHECK_INTERVAL_S = 2 * 3600   # topic_monitor.check_all() already
                                          # no-ops per-topic once it's been
                                          # checked today, so this just
                                          # controls how soon a genuinely
                                          # new headline gets noticed
                                          # instead of waiting for a restart
    LOOP_CHECK_INTERVAL_S = 3600

    from actions.system_alerts import GpuAlertMonitor
    gpu_monitor = GpuAlertMonitor()

    while True:
        try:
            await asyncio.sleep(60)
            now = time.time()
            import psutil
            cpu = psutil.cpu_percent(interval=1)
            if cpu > 90 and now - last_cpu > 300:
                last_cpu = now
                speak_fn(f"Heads up boss — CPU is at {cpu:.0f} percent.")
                await ws_module.send_toast(f"High CPU: {cpu:.0f}%", "warning")
            bat = psutil.sensors_battery()
            if bat and not bat.power_plugged and bat.percent < 20 and now - last_bat > 600:
                last_bat = now
                speak_fn(f"Battery at {bat.percent:.0f}%, boss. You might want to plug in.")
                await ws_module.send_toast(f"Low battery: {bat.percent:.0f}%", "warning")

            gpu_alert = gpu_monitor.check(ws_module.get_last_stats())
            if gpu_alert:
                speak_fn(gpu_alert)
                await ws_module.send_toast(gpu_alert, "warning")

            if now - last_topic_check > TOPIC_CHECK_INTERVAL_S:
                last_topic_check = now
                from actions.topic_monitor import check_all
                loop = asyncio.get_running_loop()
                alerts = await loop.run_in_executor(None, check_all)
                for a in alerts[:2]:
                    speak_fn(a)
                    await ws_module.send_toast(a, "info")

            if now - last_loop_check > LOOP_CHECK_INTERVAL_S:
                last_loop_check = now
                from memory.open_loops import due_for_resurface
                loop = asyncio.get_running_loop()
                stale = await loop.run_in_executor(None, due_for_resurface)
                if stale:
                    line = (f'Boss, this is still open: "{stale["text"]}" — '
                            f"want me to do anything about it, or should I let it go?")
                    speak_fn(line)
                    await ws_module.send_toast(f"Still open: {stale['text']}", "info")

            # Automations — checked every pass of this same loop (not on
            # its own longer interval like topic/loop checks above) since
            # daily_time/before_event triggers need roughly minute-level
            # precision to fire on time, not "sometime in the next couple
            # hours". get_due_rules() only touches Calendar/Gmail for the
            # trigger types that need it (before_event, new_email_from),
            # and both of those already cache/rate-limit at the API-quota
            # level the same way every other feature here does.
            from actions import automations as autom
            due_rules = await loop.run_in_executor(None, autom.get_due_rules)
            for rule in due_rules:
                try:
                    action = rule["action"]
                    if action["type"] == "speak":
                        speak_fn(action["message"])
                        await ws_module.send_toast(action["message"], "info")
                    elif action["type"] == "tool":
                        result = await _execute_automation_tool(action["tool"], action.get("args", {}))
                        speak_fn(result)
                        await ws_module.send_toast(f"[{rule['description']}] {result}", "info")
                    await loop.run_in_executor(None, autom.mark_fired, rule["id"], rule.get("_dedupe_state"))
                except Exception as e:
                    log.error(f"Automation '{rule.get('description')}' failed to execute: {e}")
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.debug(f"Monitor error: {e}")


async def _execute_automation_tool(tool: str, args: dict) -> str:
    """Dispatches an automation's tool action through the exact same
    handler functions the conversational tool-calling path uses — not a
    separate reimplementation, so an automation calling 'calendar' gets
    the identical validation, failure strings, and behavior a live
    conversation would. Scoped to the handful of tools that make sense
    unattended; Sentinel's own HIGH-risk gating (e.g. gmail send,
    calendar delete) still applies underneath these the same as always,
    so a destructive automation action still won't fire without whatever
    confirmation path Sentinel already requires for it."""
    from brain.handlers import handle_calendar, handle_reminder, handle_gmail
    # Defense in depth — handle_automation's create path already blocks
    # HIGH-risk tool args outright, but this checks again right before
    # actually firing, in case a rule ever reaches this file some other
    # way (a manual edit to automations.json, a future bug upstream).
    # Automations run unattended; nothing at this risk level runs
    # without checking twice.
    from sentinel.core import classify_risk, RiskLevel
    if classify_risk(tool, args) == RiskLevel.HIGH:
        return f"Automation blocked: {tool} with those args is HIGH-risk and automations can't run unattended at that level."
    if tool == "calendar":
        return await handle_calendar(args)
    if tool == "reminder":
        return await handle_reminder(args)
    if tool == "gmail":
        return await handle_gmail(args)
    if tool == "morning_briefing":
        from brain.handlers import handle_morning_briefing
        return await handle_morning_briefing(args)
    if tool == "open_app":
        from brain.handlers import handle_open_app
        return await handle_open_app(args.get("app_name", ""))
    if tool == "computer_settings":
        from brain.handlers import handle_computer_settings
        return await handle_computer_settings(args)
    return f"Automation error: unknown tool '{tool}'."


async def _build_greeting() -> str:
    """Boot greeting — now time-of-day aware, mentions yesterday's recap
    if one was saved at last shutdown, and any new headlines for watched
    topics. Falls back to the plain greeting if anything here fails;
    none of this should ever block FRIDAY from starting."""
    from datetime import datetime
    log = logging.getLogger("friday.main")

    hour = datetime.now().hour
    if 5 <= hour < 12:
        opener = "Good morning, boss."
    elif 12 <= hour < 18:
        opener = "Good afternoon, boss."
    elif 18 <= hour < 23:
        opener = "Good evening, boss."
    else:
        opener = "Burning the midnight oil, boss."

    parts = [opener, "FRIDAY online. All systems nominal."]

    try:
        from memory.memory_store import consume_latest_summary
        loop = asyncio.get_running_loop()
        recap = await loop.run_in_executor(None, consume_latest_summary)
        if recap:
            parts.append(f"Last time, {recap}")
    except Exception as e:
        log.debug(f"Greeting recap skipped: {e}")

    try:
        from actions.topic_monitor import check_all
        loop = asyncio.get_running_loop()
        alerts = await asyncio.wait_for(
            loop.run_in_executor(None, check_all), timeout=8.0
        )
        for a in alerts[:2]:  # keep the boot greeting from turning into a newscast
            parts.append(a)
    except Exception as e:
        log.debug(f"Greeting topic check skipped: {e}")

    return " ".join(parts)


def _kill_electron(proc) -> None:
    """proc.terminate() only kills the PID we're tracking. When Electron
    was launched via the "npx electron ." fallback (no local electron
    binary found), that tracked PID is npx itself — electron.exe runs as
    its CHILD process, and Windows does not cascade-kill child processes
    when a parent is terminated. That's why the window stayed open and
    showing "offline" after shutdown: the Python backend exited cleanly,
    but the actual Electron process tree was never touched. taskkill /T
    kills the whole tree, not just the one PID, and this is used as the
    primary approach on Windows rather than only as a fallback."""
    if not proc or proc.poll() is not None:
        return
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True, timeout=5,
            )
            return
        except Exception as e:
            logging.getLogger("friday").debug(f"taskkill failed, falling back to terminate(): {e}")
    try:
        proc.terminate()
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
    except Exception:
        pass


async def _save_session_summary():
    """Best-effort — generate and store a 1-sentence recap of this
    session for next boot's greeting. Never raises; shutdown must not
    hang or fail because of this."""
    log = logging.getLogger("friday.main")
    try:
        from brain.llm import generate_session_summary
        from memory.memory_store import save_summary
        summary = await generate_session_summary()
        if summary:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, save_summary, summary)
    except Exception as e:
        log.debug(f"Session summary skipped: {e}")


async def _shutdown_friday(tts, ws_module, log, reason: str = "user command"):
    """Full, clean app exit — TTS goodbye, session summary, stop the WS
    server, and terminate the Electron window if one is running. Shared
    by every shutdown trigger so they can't drift out of sync with each
    other (the old version of this only existed in one of two entry
    points and never touched the Electron process at all, which is what
    left it orphaned after "shutdown friday")."""
    tts.enqueue("Shutting down. See you around, boss.")
    await asyncio.get_running_loop().run_in_executor(None, tts.wait_until_done)
    await _save_session_summary()
    log.info(f"FRIDAY shutdown ({reason}).")
    tts.stop()
    await ws_module.stop_server()

    global _electron_proc_ref
    _kill_electron(_electron_proc_ref)

    os._exit(0)


# Matched against the FULL stripped message, not a substring — "exit" or
# "bye" appearing inside a longer sentence ("how do I exit vim") must not
# trigger a full app shutdown. Trailing punctuation is stripped first so
# "bye!" / "quit." still match.
_BARE_SHUTDOWN_RE = re.compile(r"^(quit|exit|bye|bye\s*bye|goodbye|good\s*bye)$")
# Multi-word phrasing IS safe to substring-match — "shut yourself down" or
# "close yourself" essentially never appears as a sub-phrase of an
# unrelated sentence.
_SELF_SHUTDOWN_RE = re.compile(
    r"\b(shut\s*down|close|turn off|power down)\s+(yourself|urself)\b"
    r"|\bshut\s+(yourself|urself)\s+down\b"
    r"|\bturn\s+(yourself|urself)\s+off\b"
)


async def _handle_user_text(user_text: str, tts, ws_module, log, speak_fn):
    """Shared pipeline for a finalized user query — whether it came from
    STT or was typed in the UI text box. Runs shutdown/quick-intent checks,
    classifies intent, streams the LLM response, and speaks it. Keeping
    this in one place is what stops the mic path and text-box path from
    drifting out of sync (see the speak_fn bug this replaced)."""
    from brain.llm import stream_response, clear_memory
    from brain.handlers import handle_timer
    from brain.router import classify_intent_async

    print(f"\n\033[32mYou:\033[0m {user_text}")
    await ws_module.send_transcript("user", user_text)

    # Pending high-risk confirmation takes priority over everything else —
    # this has to be the turn that catches "yes"/"no", not a later one.
    from sentinel import get_pending, clear_pending, is_confirmation
    pending = get_pending()
    if pending is not None:
        verdict = is_confirmation(user_text)
        if verdict is True:
            clear_pending()
            from brain.llm import _dispatch_tool
            log.info(f"[Sentinel] Confirmed — executing held action: {pending.tool}({pending.args})")
            result = await _dispatch_tool(pending.tool, pending.args, speak_fn, _confirmed=True)
            tts.enqueue(result)
            await ws_module.send_transcript("friday", result)
            return
        elif verdict is False:
            clear_pending()
            log.info(f"[Sentinel] Cancelled: {pending.tool}({pending.args})")
            msg = "Okay, cancelled that."
            tts.enqueue(msg)
            await ws_module.send_transcript("friday", msg)
            return
        else:
            # Something unrelated came in while a confirmation was
            # pending — treat as an implicit cancel (never leave a
            # destructive action one stray "yes" away from firing later)
            # and fall through to handle this message normally.
            clear_pending()
            log.info(f"[Sentinel] Pending confirmation implicitly cancelled by unrelated message: {user_text!r}")

    # Focus-session "what are we focusing on" reply — same early-intercept
    # shape as the sentinel confirmation check above: if a deferred
    # session is waiting on this answer, it's consumed right here and
    # never reaches normal intent classification.
    from actions.focus_session import consume_voice_reply
    focus_ack = consume_voice_reply(user_text)
    if focus_ack is not None:
        tts.enqueue(focus_ack)
        await ws_module.send_transcript("friday", focus_ack)
        return

    # Browser-search "what are we searching for" reply — same shape
    # again, same reason: without this, the next thing said goes
    # through normal intent classification instead of completing the
    # search, and a plain answer like "weather in bangalore" gets
    # answered as its own weather query instead of becoming the term
    # FRIDAY actually searches for.
    from brain.handlers import consume_browser_query
    browser_ack = await consume_browser_query(user_text)
    if browser_ack is not None:
        tts.enqueue(browser_ack)
        await ws_module.send_transcript("friday", browser_ack)
        return

    # Shutdown command — close FRIDAY (backend + Electron window)
    # completely. Distinct from "shut down the computer" (power_control,
    # a separate tool that controls the PC, not this app) — these
    # patterns specifically target FRIDAY herself.
    low = user_text.lower()
    stripped = low.strip().rstrip(".!?")
    if (any(p in low for p in ["shutdown friday", "close friday", "exit friday",
                                "turn off friday", "goodbye friday", "bye friday",
                                "shut down friday", "stop friday"])
            or _BARE_SHUTDOWN_RE.match(stripped)
            or _SELF_SHUTDOWN_RE.search(low)):
        await _shutdown_friday(tts, ws_module, log, reason=f"phrase: {user_text!r}")
        return  # unreachable after os._exit(0), but keeps intent clear

    # "stop monitoring X" / "quit watching X" — this is a topic_monitor
    # removal, not "stop talking to me." Must be checked BEFORE the
    # generic stop/cancel check right below, since that one matches on
    # the substring "stop" and would otherwise always win first — which
    # is exactly why "stop monitoring SpaceX" used to silently do nothing
    # (it hit the generic interrupt-and-return and never reached here).
    _unmonitor = re.search(
        r'(?:stop|quit|kill|remove|unwatch)\s+(?:monitoring|watching|tracking)\s+(.+)',
        low,
    )
    if _unmonitor:
        from actions import topic_monitor
        topic = _unmonitor.group(1).strip().rstrip(".!?")
        resp = await asyncio.get_running_loop().run_in_executor(
            None, topic_monitor.remove_monitor, topic
        )
        tts.enqueue(resp)
        await ws_module.send_transcript("friday", resp)
        return

    # Quick local intents
    if any(w in low for w in ["stop", "cancel", "shut up", "be quiet", "never mind"]):
        ws_module._stop_event.set()
        tts.stop_current()
        return

    if any(w in low for w in ["clear history", "forget everything", "start over", "reset memory"]):
        clear_memory()
        resp = "Memory cleared. Fresh start, boss."
        tts.enqueue(resp)
        await ws_module.send_transcript("friday", resp)
        return

    # Intent classification
    intent = await classify_intent_async(user_text)
    await ws_module.send_intent(intent)
    log.info(f"Intent: {intent}")

    quick_result = None
    if intent == "timer":
        quick_result = await handle_timer(user_text)
    elif intent == "stop":
        ws_module._stop_event.set()
        tts.stop_current()
        return
    elif intent == "clear":
        clear_memory()
        quick_result = "Memory cleared. Fresh start, boss."
    elif intent == "focus_retarget":
        from actions.focus_session import retarget
        quick_result = await retarget()
    elif intent == "posture_relief":
        from actions.focus_session import silence_posture
        quick_result = await silence_posture()

    if quick_result is not None:
        tts.enqueue(quick_result)
        await ws_module.send_transcript("friday", quick_result)
        await asyncio.get_running_loop().run_in_executor(None, tts.wait_until_done)
        return

    # LLM generation
    print("\033[33mF.R.I.D.A.Y.:\033[0m ", end="", flush=True)

    await ws_module.set_state("speaking")
    full = []

    async for sentence in stream_response(user_text, speak_fn=speak_fn):
        if ws_module.get_stop_flag():
            ws_module.clear_stop_flag()
            log.info("Speech interrupted by user")
            break
        if not sentence:
            continue

        silent = sentence.startswith("\x00")
        if silent:
            sentence = sentence[1:]

        print(sentence, end=" ", flush=True)
        full.append(sentence)
        if not silent:
            # speak_fn is only wired to tool-call side announcements inside
            # stream_response, never to the main response text — so this is
            # the only thing that actually speaks the answer.
            tts.enqueue(sentence)

    print()
    if full:
        await ws_module.send_transcript("friday", " ".join(full))
    else:
        # Never go fully silent — this is exactly the "no response, had to
        # restart" failure mode. Whatever upstream reason produced zero
        # output, at minimum tell the user something happened.
        log.warning("stream_response produced zero output for: %r", user_text)
        fallback = "Didn't get anything back there, boss. Mind trying again?"
        print(fallback)
        tts.enqueue(fallback)
        await ws_module.send_transcript("friday", fallback)

    await asyncio.get_running_loop().run_in_executor(None, tts.wait_until_done)
    await ws_module.set_state("idle")


async def _voice_loop(tts, ws_module, use_wake_word: bool = False):
    from voice.vad import record_until_silence
    from voice.stt import transcribe_async

    log = logging.getLogger("friday.main")

    def speak_fn(text: str):
        if text:
            tts.enqueue(text)

    text_queue = ws_module.get_text_input_queue()

    while True:
        try:
            await ws_module.set_state("idle")
            typed_text = None
            loop = asyncio.get_running_loop()
            text_f = loop.create_task(text_queue.get())

            if use_wake_word:
                from actions.focus_session import is_awaiting_voice_reply
                from brain.handlers import is_awaiting_browser_query
                if is_awaiting_voice_reply() or is_awaiting_browser_query():
                    # A deferred focus session is waiting on exactly one
                    # reply ("what are we focusing on"), or a browser
                    # search is waiting on its query — skip the
                    # wake-word gate for this turn only, same "mic open
                    # for one reply" shape sentinel's pending
                    # confirmations use (checked in _handle_user_text,
                    # not here — this just controls whether we wait for
                    # "Hey FRIDAY" first). Falls straight through to
                    # recording below.
                    log.info("Awaiting a reply — skipping wake word for this turn.")
                    text_f.cancel()
                else:
                    from voice.wake import wait_for_wake_word
                    log.info("Waiting for wake word...")
                    wake_f = loop.create_task(wait_for_wake_word())
                    done, pending = await asyncio.wait([wake_f, text_f],
                                                       return_when=asyncio.FIRST_COMPLETED)
                    for t in pending:
                        t.cancel()
                    if text_f in done:
                        typed_text = text_f.result()
            else:
                mic_event = ws_module.get_mic_event()
                mic_event.clear()
                enter_f = loop.run_in_executor(None, input, "\n\033[33m[Enter or click Speak]\033[0m ")
                mic_f   = loop.run_in_executor(None, mic_event.wait)
                done, pending = await asyncio.wait([enter_f, mic_f, text_f],
                                                   return_when=asyncio.FIRST_COMPLETED)
                for t in pending:
                    t.cancel()
                mic_event.clear()
                if text_f in done:
                    typed_text = text_f.result()

            # Stop current speech if any
            if tts.is_speaking:
                tts.stop_current()
                await asyncio.sleep(0.1)
            tts.resume()
            ws_module.clear_stop_flag()

            if typed_text is not None:
                user_text = typed_text
            else:
                # Record
                await ws_module.set_state("listening")
                audio = await record_until_silence()
                if audio is None:
                    continue

                # STT
                await ws_module.set_state("thinking")
                t0 = time.time()
                user_text = await transcribe_async(audio)
                if not user_text:
                    continue

                stt_ms = (time.time() - t0) * 1000
                log.info(f"STT ({stt_ms:.0f}ms): {user_text}")

            await _handle_user_text(user_text, tts, ws_module, log, speak_fn)

        except KeyboardInterrupt:
            break
        except Exception as e:
            log.error(f"Voice loop error: {e}", exc_info=True)
            await ws_module.send_error(str(e))
            await asyncio.sleep(1)


async def _text_loop(tts, ws_module):
    from brain.llm import stream_response, clear_memory
    from brain.router import classify_intent_async

    log = logging.getLogger("friday.main")
    print("\033[33mF.R.I.D.A.Y. text mode — type 'quit' to exit.\033[0m\n")

    def speak_fn(text: str):
        if text:
            tts.enqueue(text)

    while True:
        try:
            loop = asyncio.get_running_loop()
            user_text = await loop.run_in_executor(None, lambda: input("\033[32mYou:\033[0m "))

            if user_text.strip().lower() in {"quit", "exit", "bye"}:
                break
            if not user_text.strip():
                continue

            low = user_text.lower()
            if any(p in low for p in ["shutdown friday", "close friday", "exit friday"]):
                await _shutdown_friday(tts, ws_module, log, reason=f"phrase: {user_text!r}")

            await ws_module.send_transcript("user", user_text)
            await ws_module.set_state("thinking")

            intent = await classify_intent_async(user_text)
            await ws_module.send_intent(intent)

            if intent in ("focus_retarget", "posture_relief"):
                if intent == "focus_retarget":
                    from actions.focus_session import retarget
                    quick_result = await retarget()
                else:
                    from actions.focus_session import silence_posture
                    quick_result = await silence_posture()
                print(f"\033[33mF.R.I.D.A.Y.:\033[0m {quick_result}")
                tts.enqueue(quick_result)
                await ws_module.send_transcript("friday", quick_result)
                continue

            print("\033[33mF.R.I.D.A.Y.:\033[0m ", end="", flush=True)

            await ws_module.set_state("speaking")
            full = []

            async for sentence in stream_response(user_text, speak_fn=speak_fn):
                if ws_module.get_stop_flag():
                    ws_module.clear_stop_flag()
                    break
                if not sentence:
                    continue
                print(sentence, end=" ", flush=True)
                full.append(sentence)
                # FIX: Removed tts.enqueue(sentence) — speak_fn already enqueues it.

            print()
            if full:
                await ws_module.send_transcript("friday", " ".join(full))

            await loop.run_in_executor(None, tts.wait_until_done)
            await ws_module.set_state("idle")

        except KeyboardInterrupt:
            break
        except Exception as e:
            log.error(f"Text loop error: {e}")
            await ws_module.send_error(str(e))


async def run(args):
    from utils.logging_setup import setup_logging
    from config import config
    from voice.tts import TTSPipeline
    import ui.ws_server as ws

    setup_logging("DEBUG" if args.debug else config.log_level)
    config.validate()

    log = logging.getLogger("friday")

    await ws.start_server()

    electron_proc = None
    if not args.no_ui:
        log.info("Launching Electron UI...")
        electron_proc = _start_electron()
        global _electron_proc_ref
        _electron_proc_ref = electron_proc
        # Wait for UI to connect — up to 10s, don't block on it
        for _ in range(20):
            await asyncio.sleep(0.5)
            if ws.client_count() > 0:
                break

    if not args.text:
        from voice.stt import warm_up
        log.info("Warming up Whisper on GPU...")
        await ws.set_state("thinking")
        await ws.send_toast("Loading Whisper large-v3 on GPU… (~2 min first run)", "info")

        loop = asyncio.get_running_loop()

        # Run warm_up in thread while sending keepalive pings to UI
        warm_task = loop.run_in_executor(None, warm_up)
        tick = 0
        while not warm_task.done():
            await asyncio.sleep(2)
            tick += 2
            try:
                await ws.set_state("thinking")   # keepalive — prevents UI disconnect
                if tick % 20 == 0:
                    await ws.send_toast(f"Still loading Whisper… ({tick}s)", "info")
            except Exception:
                pass

        try:
            await warm_task   # raise any exception from the thread
        except RuntimeError as e:
            err = str(e)
            if "cublas64_12" in err or "cublas" in err.lower() or "cudnn" in err.lower():
                log.error("CUDA DLL not found. Fix: pip install nvidia-cublas-cu12 nvidia-cudnn-cu12")
                log.error("Or set CUDA_PATH in .env to your CUDA Toolkit install directory.")
                await ws.send_toast(
                    "CUDA DLL missing. Run: pip install nvidia-cublas-cu12 nvidia-cudnn-cu12", "error"
                )
                log.warning("Falling back to CPU Whisper (slower)...")
                os.environ["WHISPER_DEVICE"] = "cpu"
                config.voice.whisper_device = "cpu"
                config.voice.whisper_compute_type = "int8"
                await loop.run_in_executor(None, warm_up)
            else:
                raise

    if not args.text and config.brain.llm_provider == "ollama":
        try:
            import ollama as ol
            log.info(f"Warming up Ollama ({config.brain.ollama_model})...")
            await asyncio.get_running_loop().run_in_executor(
                None, lambda: ol.chat(
                    model=config.brain.ollama_model,
                    messages=[{"role": "user", "content": "hi"}],
                    options={"num_predict": 1},
                )
            )
        except Exception as e:
            log.warning(f"Ollama warm-up failed: {e}")

    # Warm the audio-device cache on a background thread now — each probe
    # opens a real stream (see voice/audio_devices.py), so the first time
    # someone opens Settings > Audio Devices shouldn't be the first time
    # that cost gets paid.
    from voice import audio_devices as _audio_devices
    _audio_devices.prefetch()

    tts = TTSPipeline()
    tts.start()
    ws.register_tts(tts)

    def speak_fn(text: str):
        if text:
            tts.enqueue(text)

    await ws.set_state("idle")

    if not args.text:
        greeting = await _build_greeting()
        tts.enqueue(greeting)
        await ws.send_transcript("friday", greeting)

    log.info("Startup complete.")

    if not args.text and not args.no_ui:
        asyncio.create_task(_proactive_monitor(ws, speak_fn))

        # Focus sessions — same shape as the monitor above: a persistent
        # background task that ticks independent of any conversational
        # turn and can speak unprompted. Idle until a session is started
        # via the focus_session tool.
        from actions.focus_session import configure as _configure_focus, start_focus_loop
        _configure_focus(speak_fn=speak_fn, broadcast_fn=ws.broadcast)
        start_focus_loop()

    try:
        if args.text:
            await _text_loop(tts, ws)
        else:
            await _voice_loop(tts, ws, use_wake_word=args.wake)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await _save_session_summary()
        tts.stop()
        await ws.stop_server()
        if electron_proc:
            _kill_electron(electron_proc)
        log.info("F.R.I.D.A.Y. offline.")


def main():
    parser = argparse.ArgumentParser(description="F.R.I.D.A.Y.")
    parser.add_argument("--wake",   action="store_true", help="Wake-word mode ('Hey FRIDAY')")
    parser.add_argument("--text",   action="store_true", help="Text-only mode")
    parser.add_argument("--no-ui",  action="store_true", help="Skip Electron window")
    parser.add_argument("--debug",  action="store_true", help="Verbose logging")
    args = parser.parse_args()

    _banner()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
