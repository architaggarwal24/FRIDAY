"""
F.R.I.D.A.Y. — main.py
Alternate entry point (legacy — use start.py instead).
start.py has Electron UI launch + proactive monitor + cleaner shutdown.
"""

import dll_fix  # noqa: F401 — MUST be first

import argparse
import asyncio
import logging
import os
import sys
import time

from utils.logging_setup import setup_logging
from config import config

logger = logging.getLogger("friday.main")


def print_banner():
    banner = r"""
  ███████╗██████╗ ██╗██████╗  █████╗ ██╗   ██╗
  ██╔════╝██╔══██╗██║██╔══██╗██╔══██╗╚██╗ ██╔╝
  █████╗  ██████╔╝██║██║  ██║███████║ ╚████╔╝
  ██╔══╝  ██╔══██╗██║██║  ██║██╔══██║  ╚██╔╝
  ██║     ██║  ██║██║██████╔╝██║  ██║   ██║
  ╚═╝     ╚═╝  ╚═╝╚═╝╚═════╝ ╚═╝  ╚═╝   ╚═╝

  Female Replacement Intelligent Digital Assistant Youth
  ───────────────────────────────────────────────────────
  LLM: {llm}  |  STT: Whisper {whisper}  |  TTS: {tts}
    """.format(
        llm=f"{config.brain.llm_provider}/{config.brain.ollama_model}",
        whisper=config.voice.whisper_model,
        tts=config.voice.tts_provider,
    )
    print("\033[33m" + banner + "\033[0m")


async def _route(intent: str, user_text: str, speak_fn=None):
    """
    Routes intent to the right handler or LLM.
    Always an async generator — yields sentences for TTS.
    speak_fn passed to LLM so async tool callbacks (vision, code) can speak.
    """
    from brain import handlers
    from brain.llm import stream_response

    # ── Quick local intents — call handler, yield its result as one sentence ──
    result = None

    if intent == "stop":
        result = await handlers.handle_stop(user_text)
    elif intent == "clear":
        result = await handlers.handle_clear(user_text)
    elif intent == "power":
        result = await handlers.handle_power(user_text)
    elif intent == "timer":
        result = await handlers.handle_timer(user_text)
    elif intent == "pronoun_stop":
        result = await handlers.handle_pronoun_stop()
    elif intent in ("memory", "memory_save", "memory_recall"):
        low = user_text.lower().strip()

        # Determine save vs recall using clear intent signals
        SAVE_PHRASES  = ["remember that", "remember this", "save this", "note that",
                         "don't forget", "keep in mind", "make a note", "note:"]
        RECALL_PHRASES = ["what do you remember", "what do you know", "what did i say",
                          "what have i told", "do you remember", "recall",
                          "tell me about me", "what is my name", "how old am i",
                          "remember about me", "remember me"]

        is_recall = (
            intent == "memory_recall"
            or any(p in low for p in RECALL_PHRASES)
        )
        # FIX: wrap sub-expressions explicitly — `and` binds tighter than `or`.
        # Without parens the word-count guard only applies to phrase-matched saves,
        # not to intent=="memory_save" cases, which can produce empty saves.
        is_save = (
            (intent == "memory_save" and len(low.split()) > 3)
            or (any(low.startswith(p) for p in SAVE_PHRASES) and len(low.split()) > 3)
        )

        if is_recall or (not is_save):
            result = await handlers.handle_memory_recall(user_text)
        else:
            result = await handlers.handle_memory_save(user_text)

    # ── Coding intent — route directly to handle_code (Claude Code / Aider) ────
    # This bypasses the raw LLM so Ollama's small model can't hallucinate edits.
    # handle_code inspects the user text to extract path and language,
    # then routes to the appropriate tier (Claude Code → Aider → NIM inline).
    if intent == "coding":
        import re as _recode
        from actions.coding_agent import handle_code
        from brain.llm import get_session_last_file
        # Extract explicit path from current message
        _pm = _recode.search(r"[A-Za-z]:[/\\\\][^\s\"']+", user_text)
        path = _pm.group(0).rstrip(".' \"") if _pm else ""
        # Fall back to last file path from session context
        # Resolves follow-ups like "update it" / "fix it" with no path
        if not path:
            path = get_session_last_file()
        lang = "python"
        for _l in ["python", "javascript", "typescript", "c++", "c", "rust", "go", "java"]:
            if _l in user_text.lower():
                lang = _l
                break
        coding_result = handle_code({
            "task": user_text,
            "path": path,
            "language": lang,
            "action": "write",
        })
        if coding_result:
            yield coding_result
        return

    if result is not None:
        # Handler returned a plain string — yield it and done
        if result:
            yield result
        return

    # ── LLM stream — yield sentences as they arrive ──────────────────────────
    async for sentence in stream_response(user_text, speak_fn=speak_fn):
        yield sentence


async def _run_session(tts, use_wake_word: bool, ws):
    """
    One full interaction session.
    Returns normally on KeyboardInterrupt.
    Raises on unexpected errors so auto-reconnect can restart.
    """
    from voice.vad import record_until_silence
    from voice.wake import wait_for_wake_word
    from voice.stt import transcribe_async
    from voice.tts import request_interrupt, clear_interrupt
    from brain.router import classify_intent_async

    while True:
        try:
            # ── Wait for activation ───────────────────────────────────────
            await ws.set_state("idle")

            if use_wake_word:
                logger.info("Waiting for wake word...")
                # Gate mic open only when FRIDAY isn't speaking
                while tts.is_speaking:
                    await asyncio.sleep(0.05)
                await wait_for_wake_word()
            else:
                mic_event = ws.get_mic_event()
                mic_event.clear()
                loop = asyncio.get_running_loop()
                print("\n\033[33m[Press Enter or click Speak | Ctrl+C to quit]\033[0m ",
                      end="", flush=True)
                enter_future = loop.run_in_executor(None, input, "")
                mic_future   = loop.run_in_executor(None, mic_event.wait)
                # Use timeout=1s poll so Ctrl+C is checked every second
                while True:
                    done, pending = await asyncio.wait(
                        [enter_future, mic_future],
                        return_when=asyncio.FIRST_COMPLETED,
                        timeout=1.0,
                    )
                    if done:
                        break
                    # asyncio.CancelledError propagates Ctrl+C naturally here
                for t in pending:
                    t.cancel()
                mic_event.clear()

            # ── Stop current speech if any ────────────────────────────────
            if tts.is_speaking:
                tts.stop_current()
                await asyncio.sleep(0.1)
            tts.resume()

            # ── Record ────────────────────────────────────────────────────
            await ws.set_state("listening")
            logger.info("Listening...")
            audio = await record_until_silence()
            if audio is None:
                continue

            # ── STT ───────────────────────────────────────────────────────
            await ws.set_state("thinking")
            t_stt = time.time()
            user_text = await transcribe_async(audio)
            if not user_text:
                continue

            stt_ms = (time.time() - t_stt) * 1000
            logger.info(f"STT ({stt_ms:.0f}ms): {user_text}")
            print(f"\n\033[32mYou:\033[0m {user_text}")
            await ws.send_transcript("user", user_text)

            # ── Check for shutdown command ────────────────────────────────
            low = user_text.lower()
            if any(p in low for p in ["shutdown friday", "close friday", "exit friday",
                                       "turn off friday", "goodbye friday", "bye friday",
                                       "shut down friday", "stop friday"]):
                tts.enqueue("Shutting down. See you around, boss.")
                await asyncio.get_running_loop().run_in_executor(None, tts.wait_until_done)
                logger.info("FRIDAY shutdown by user command.")
                tts.stop()
                await ws.stop_server()
                os._exit(0)

            # ── Intent ────────────────────────────────────────────────────
            intent = await classify_intent_async(user_text)
            await ws.send_intent(intent)
            logger.info(f"Intent: {intent}")
            from brain.llm import _update_session_file
            _update_session_file(user_text)

            # ── Generate + speak ──────────────────────────────────────────
            await ws.set_state("thinking")
            print("\033[33mF.R.I.D.A.Y.:\033[0m ", end="", flush=True)

            def _speak(text):
                if text:
                    tts.enqueue(text)
            response_gen = _route(intent, user_text, speak_fn=_speak)
            full_response = []
            first = True

            async for sentence in response_gen:
                if not sentence:
                    continue
                # Stop if interrupted mid-generation
                if ws.get_stop_flag():
                    ws.clear_stop_flag()
                    tts.stop_current()
                    break
                if first:
                    await ws.set_state("speaking")
                    first = False
                print(sentence, end=" ", flush=True)
                full_response.append(sentence)
                tts.enqueue(sentence)

            print()
            if full_response:
                await ws.send_transcript("friday", " ".join(full_response))

            await asyncio.get_running_loop().run_in_executor(None, tts.wait_until_done)
            await ws.set_state("idle")

        except KeyboardInterrupt:
            raise
        except Exception as e:
            logger.error(f"Error in voice loop: {e}", exc_info=True)
            await ws.send_error(str(e))
            await asyncio.sleep(1)
            # Don't re-raise — keep the session alive on recoverable errors


async def run_voice_loop(use_wake_word: bool = True, ui: bool = True):
    """
    Auto-reconnecting voice loop.
    On unexpected crash: logs, waits 3s, restarts session.
    Only exits on KeyboardInterrupt or shutdown command.
    """
    from voice.stt import warm_up
    from voice.tts import TTSPipeline, stream_tts
    import ui.ws_server as ws

    if ui:
        await ws.start_server()

    logger.info("Warming up Whisper model on GPU...")
    await ws.set_state("thinking")
    warm_up()

    tts = TTSPipeline()
    tts.start()

    await ws.set_state("idle")
    await stream_tts("FRIDAY online. Ready when you are, boss.")
    await ws.send_transcript("friday", "FRIDAY online. Ready when you are, boss.")
    logger.info("Startup complete.")

    # ── Auto-reconnect loop ───────────────────────────────────────────────
    while True:
        try:
            await _run_session(tts, use_wake_word, ws)
            break  # clean exit
        except KeyboardInterrupt:
            logger.info("Shutting down...")
            break
        except Exception as e:
            logger.error(f"Session crashed: {e} — restarting in 3s...", exc_info=True)
            try:
                await ws.send_toast("Session crashed — restarting...", "warning")
            except Exception:
                pass
            await asyncio.sleep(3)
            logger.info("Restarting session...")
            continue

    tts.stop()
    await ws.stop_server()
    logger.info("FRIDAY offline.")


async def run_text_loop(ui: bool = True):
    """Text-only mode with auto-reconnect."""
    from voice.tts import TTSPipeline
    from brain.router import classify_intent_async
    import ui.ws_server as ws

    if ui:
        await ws.start_server()

    tts = TTSPipeline()
    tts.start()
    print("\033[33mF.R.I.D.A.Y. text mode. Type 'quit' to exit.\033[0m\n")
    await ws.set_state("idle")

    while True:
        try:
            user_text = await asyncio.get_running_loop().run_in_executor(
                None, lambda: input("\033[32mYou:\033[0m ")
            )
            if user_text.strip().lower() in {"quit", "exit", "bye"}:
                break
            if not user_text.strip():
                continue

            low = user_text.lower()
            if any(p in low for p in ["shutdown friday", "close friday", "exit friday",
                                       "turn off friday", "goodbye friday"]):
                tts.enqueue("Shutting down. See you around, boss.")
                await asyncio.get_running_loop().run_in_executor(None, tts.wait_until_done)
                tts.stop()
                await ws.stop_server()
                os._exit(0)

            await ws.send_transcript("user", user_text)
            await ws.set_state("thinking")
            intent = await classify_intent_async(user_text)
            await ws.send_intent(intent)
            from brain.llm import _update_session_file
            _update_session_file(user_text)

            print("\033[33mF.R.I.D.A.Y.:\033[0m ", end="", flush=True)
            await ws.set_state("speaking")
            def _speak_text(text):
                if text:
                    tts.enqueue(text)
            gen = _route(intent, user_text, speak_fn=_speak_text)
            full_response = []

            async for sentence in gen:
                if not sentence:
                    continue
                print(sentence, end=" ", flush=True)
                full_response.append(sentence)
                tts.enqueue(sentence)

            print()
            if full_response:
                await ws.send_transcript("friday", " ".join(full_response))

            await asyncio.get_running_loop().run_in_executor(None, tts.wait_until_done)
            await ws.set_state("idle")

        except KeyboardInterrupt:
            break
        except Exception as e:
            logger.error(f"Error: {e}")
            await ws.send_error(str(e))

    tts.stop()
    await ws.stop_server()


def main():
    parser = argparse.ArgumentParser(description="F.R.I.D.A.Y. Voice AI")
    parser.add_argument("--no-wake", action="store_true", help="Skip wake word, press Enter")
    parser.add_argument("--text",    action="store_true", help="Text-only mode")
    parser.add_argument("--no-ui",   action="store_true", help="Disable WebSocket UI")
    parser.add_argument("--debug",   action="store_true", help="Debug logging")
    args = parser.parse_args()

    setup_logging("DEBUG" if args.debug else config.log_level)
    config.validate()
    print_banner()

    # ── Ctrl+C handler — works on Windows + Unix ────────────────────────
    import signal, threading

    _shutdown = threading.Event()

    def _handle_sigint(sig, frame):
        if _shutdown.is_set():
            # Second Ctrl+C — force kill
            print("\n\033[31mForce quit.\033[0m")
            os._exit(1)
        _shutdown.set()
        print("\n\033[33mCtrl+C caught — shutting down FRIDAY... (press again to force quit)[0m")
        # Interrupt the main thread correctly — safe from signal context
        import _thread
        _thread.interrupt_main()

    signal.signal(signal.SIGINT, _handle_sigint)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _handle_sigint)

    print("[90mTip: Press Ctrl+C to quit FRIDAY cleanly.[0m")

    ui_enabled = not args.no_ui
    try:
        if args.text:
            asyncio.run(run_text_loop(ui=ui_enabled))
        else:
            asyncio.run(run_voice_loop(use_wake_word=not args.no_wake, ui=ui_enabled))
    except KeyboardInterrupt:
        print("\n\033[33mFRIDAY offline. See you around, boss.[0m")


if __name__ == "__main__":
    main()