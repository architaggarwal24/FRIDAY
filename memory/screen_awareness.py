"""
F.R.I.D.A.Y. — memory/screen_awareness.py
Screen awareness — FRIDAY can see what's on your screen.

Uses:
1. pyautogui to capture a screenshot
2. Claude's vision API (or LLaVA locally) to describe it
3. Returns a natural language description FRIDAY can use in her response

Triggered when user says things like:
  "what's on my screen"
  "what am I looking at"
  "can you see this"
  "what does my screen show"
"""

import asyncio
import base64
import io
import logging
import os
import time
from typing import Optional

logger = logging.getLogger(__name__)


def capture_screen() -> bytes:
    """Takes a screenshot and returns it as JPEG bytes."""
    try:
        import pyautogui
        from PIL import Image

        screenshot = pyautogui.screenshot()

        # Resize to reduce token usage — 1280px wide max
        w, h = screenshot.size
        if w > 1280:
            ratio = 1280 / w
            screenshot = screenshot.resize((1280, int(h * ratio)), Image.LANCZOS)

        buf = io.BytesIO()
        screenshot.save(buf, format="JPEG", quality=75)
        return buf.getvalue()
    except Exception as e:
        logger.error(f"Screenshot failed: {e}")
        return b""


def encode_image(image_bytes: bytes) -> str:
    """Base64-encodes image bytes for API transmission."""
    return base64.standard_b64encode(image_bytes).decode("utf-8")


async def describe_screen(question: str = "What is on this screen?") -> str:
    """
    Captures the screen and asks a vision model to describe it.

    Tries in order:
    1. Claude claude-haiku-4-5-20251001 (fast, cheap, great vision) if ANTHROPIC_API_KEY set
    2. LLaVA via Ollama (local, no API key needed)

    Returns a natural language description.
    """
    image_bytes = await asyncio.get_running_loop().run_in_executor(
        None, capture_screen
    )
    if not image_bytes:
        return "I couldn't capture the screen, boss."

    # Try Claude vision first (best quality)
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if anthropic_key:
        result = await _describe_with_claude(image_bytes, question, anthropic_key)
        if result:
            return result

    # Fall back to LLaVA locally
    result = await _describe_with_llava(image_bytes, question)
    if result:
        return result

    return "I can see your screen but couldn't process the image right now, boss."


async def _describe_with_claude(
    image_bytes: bytes, question: str, api_key: str
) -> Optional[str]:
    """Uses Claude Haiku vision to describe the screen."""
    try:
        import anthropic

        client = anthropic.AsyncAnthropic(api_key=api_key)
        b64 = encode_image(image_bytes)

        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/jpeg",
                                "data": b64,
                            },
                        },
                        {
                            "type": "text",
                            "text": (
                                f"{question}\n\n"
                                "Answer concisely in 1-3 sentences as if you're "
                                "an AI assistant describing what you see to your user. "
                                "Be specific about app names, text content, and what's happening."
                            ),
                        },
                    ],
                }
            ],
        )
        return response.content[0].text.strip()
    except Exception as e:
        logger.error(f"Claude vision error: {e}")
        return None


async def _describe_with_llava(
    image_bytes: bytes, question: str
) -> Optional[str]:
    """Uses LLaVA via Ollama for local vision processing."""
    try:
        import ollama

        # Check if LLaVA is available
        models = ollama.list()
        model_names = [m.get("name", "") for m in models.get("models", [])]
        llava_model = next(
            (m for m in model_names if "llava" in m.lower() or "vision" in m.lower()),
            None
        )

        if not llava_model:
            logger.warning(
                "No vision model found in Ollama. "
                "Run: ollama pull llava:7b"
            )
            return None

        b64 = encode_image(image_bytes)

        response = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: ollama.chat(
                model=llava_model,
                messages=[
                    {
                        "role": "user",
                        "content": question,
                        "images": [b64],
                    }
                ],
                options={"temperature": 0, "num_predict": 200},
            ),
        )
        return response.get("message", {}).get("content", "").strip()
    except Exception as e:
        logger.error(f"LLaVA vision error: {e}")
        return None


# ── Spotify now-playing via screen ─────────────────────────────────────────

async def get_spotify_now_playing_from_screen() -> Optional[str]:
    """
    Specific helper: captures screen and extracts what's playing in Spotify.
    Used when user asks "what song is this" and Spotify API isn't configured.
    """
    image_bytes = await asyncio.get_running_loop().run_in_executor(
        None, capture_screen
    )
    if not image_bytes:
        return None

    question = (
        "Look at this screenshot. If Spotify is visible, what song is currently playing? "
        "Give me just the song title and artist name, nothing else. "
        "If Spotify is not visible or no song is playing, say 'not visible'."
    )

    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if anthropic_key:
        result = await _describe_with_claude(image_bytes, question, anthropic_key)
        if result and "not visible" not in result.lower():
            return result

    result = await _describe_with_llava(image_bytes, question)
    if result and "not visible" not in (result or "").lower():
        return result

    return None
