"""
F.R.I.D.A.Y. — config.py
Single source of truth. Reads from .env, never hardcodes personal info,
and never rewrites .env on its own. Every secret and every model name
lives in .env — nothing is ever hardcoded in this file or anywhere
else in the codebase; every call site reads it from here.
"""

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

_log = logging.getLogger("friday.config")

try:
    from dotenv import load_dotenv
    _env = Path(__file__).parent / ".env"
    if _env.exists():
        load_dotenv(_env, override=True)
except ImportError:
    pass


def _e(k, d=""): return os.environ.get(k, d).strip()
def _ei(k, d):
    try: return int(os.environ.get(k, d))
    except: return d


def _secret(key: str, default: str = "") -> str:
    """Secrets (API keys, tokens) — same as _e(), kept as a separate
    name so BrainConfig/VoiceConfig's intent stays obvious at a glance.
    Reads straight from .env / the environment, nothing else: no OS
    keyring, no migration, nothing ever rewrites .env for you."""
    return _e(key, default)


@dataclass
class VoiceConfig:
    device_index:           int   = field(default_factory=lambda: _ei("MIC_DEVICE_INDEX", 1))
    # Device *names* (resolved to an index at stream-open time via
    # voice/audio_devices.py — see that file for why names, not indices).
    # "" = system default. The old MIC_DEVICE_INDEX / SPEAKER_DEVICE_INDEX
    # above are now unused for device SELECTION (device_index is kept only
    # as a legacy fallback if voice/audio_devices.py's own resolution comes
    # back empty); reselect your devices once in Settings > Audio Devices
    # after upgrading and it'll write the name-based value going forward.
    mic_device_name:        str   = field(default_factory=lambda: _e("MIC_DEVICE_NAME", ""))
    speaker_device_name:    str   = field(default_factory=lambda: _e("SPEAKER_DEVICE_NAME", ""))
    sample_rate:            int   = 16000
    channels:               int   = 1
    chunk_size:             int   = 1024
    vad_aggressiveness:     int   = 1
    silence_threshold_ms:   int   = 1200
    min_speech_ms:          int   = 500
    max_record_seconds:     int   = 30
    wake_sensitivity:       float = 0.5
    whisper_model:          str   = field(default_factory=lambda: _e("WHISPER_MODEL", ""))
    whisper_device:         str   = field(default_factory=lambda: _e("WHISPER_DEVICE", "cuda"))
    whisper_compute_type:   str   = "float16"
    whisper_language:       object = None
    whisper_beam_size:      int   = 5
    whisper_initial_prompt: str   = field(default_factory=lambda: os.environ.get(
        "WHISPER_INITIAL_PROMPT",
        "FRIDAY, F.R.I.D.A.Y., hey FRIDAY, VS Code, GitHub, Python, Spotify, Discord, Chrome, "
        "ollama, open, close, create, delete, folder, file, project, play, pause, next, skip, "
        "shuffle, volume, search, weather"
    ))
    tts_provider:           str   = field(default_factory=lambda: _e("TTS_PROVIDER", "elevenlabs"))
    elevenlabs_api_key:     str   = field(default_factory=lambda: _secret("ELEVENLABS_API_KEY"))
    elevenlabs_voice_id:    str   = field(default_factory=lambda: _e("ELEVENLABS_VOICE_ID", ""))
    elevenlabs_model:       str   = field(default_factory=lambda: _e("ELEVENLABS_MODEL", ""))
    fish_audio_api_key:     str   = field(default_factory=lambda: _secret("FISH_AUDIO_API_KEY"))
    fish_audio_model_id:    str   = field(default_factory=lambda: _e("FISH_AUDIO_MODEL_ID", ""))
    fish_audio_engine:      str   = field(default_factory=lambda: _e("FISH_AUDIO_ENGINE", ""))
    edge_tts_voice:         str   = field(default_factory=lambda: _e("EDGE_TTS_VOICE", ""))


@dataclass
class BrainConfig:
    # Active provider: "nvidia" | "gemini" | "ollama" | "auto" | "fast" | "strong" | "local"
    # (the last four route through the Model Router — see brain/model_router.py)
    llm_provider:              str = field(default_factory=lambda: _e("LLM_PROVIDER", "nvidia"))

    # NVIDIA NIM
    nvidia_nim_api_key:        str = field(default_factory=lambda: _secret("NVIDIA_NIM_API_KEY"))
    nvidia_nim_model:          str = field(default_factory=lambda: _e("NVIDIA_NIM_MODEL", ""))

    # Gemini — gemini_model is for the main re-extraction/save_memory calls;
    # gemini_fast_model is a deliberately cheaper/quicker model for small
    # auxiliary tasks (action detection, coordinate-finding, quick
    # summaries); gemini_live_model/gemini_live_voice are the separate
    # streaming Live-API model + voice for real-time screen/audio sessions.
    # All are optional — unset ones just mean that specific feature is
    # unavailable.
    gemini_api_key:            str = field(default_factory=lambda: _secret("GEMINI_API_KEY"))
    gemini_model:              str = field(default_factory=lambda: _e("GEMINI_MODEL", ""))
    gemini_fast_model:         str = field(default_factory=lambda: _e("GEMINI_FAST_MODEL", ""))
    gemini_live_model:         str = field(default_factory=lambda: _e("GEMINI_LIVE_MODEL", ""))
    gemini_live_voice:         str = field(default_factory=lambda: _e("GEMINI_LIVE_VOICE", ""))

    # Ollama (local fallback)
    ollama_model:              str = field(default_factory=lambda: _e("OLLAMA_MODEL", ""))
    ollama_base_url:           str = field(default_factory=lambda: _e("OLLAMA_BASE_URL", "http://localhost:11434"))

    # Model Router — FAST tier (see brain/model_router.py). Each slot is an
    # arbitrary OpenAI-compatible endpoint reached through LiteLLM: name is
    # whatever your provider calls the model, base_url is its
    # /chat/completions-compatible root, api_key is that provider's key.
    # Both slots optional and independent — set one, both, or neither.
    # NEITHER slot is a specific named service Claude verified exists
    # (see model_router.py's module docstring) — fill in a real
    # OpenAI-compatible provider's details here.
    fast_model_1_name:         str = field(default_factory=lambda: _e("FAST_MODEL_1_NAME", ""))
    fast_model_1_base_url:     str = field(default_factory=lambda: _e("FAST_MODEL_1_BASE_URL", ""))
    fast_model_1_api_key:      str = field(default_factory=lambda: _secret("FAST_MODEL_1_API_KEY"))
    fast_model_2_name:         str = field(default_factory=lambda: _e("FAST_MODEL_2_NAME", ""))
    fast_model_2_base_url:     str = field(default_factory=lambda: _e("FAST_MODEL_2_BASE_URL", ""))
    fast_model_2_api_key:      str = field(default_factory=lambda: _secret("FAST_MODEL_2_API_KEY"))

    # Model Router — fallback chains. Comma-separated tier names, tried in
    # order; a tier with nothing configured (e.g. FAST with both slots
    # blank) is just skipped, not an error. Defaults match the brief:
    # FAST tries itself then escalates; LOCAL, chosen deliberately (often
    # for privacy/offline), does NOT silently escape to the cloud on its
    # own — see model_router.py's module docstring for the reasoning.
    router_chain_fast:         str = field(default_factory=lambda: _e("ROUTER_CHAIN_FAST", "fast,strong,local"))
    router_chain_strong:       str = field(default_factory=lambda: _e("ROUTER_CHAIN_STRONG", "strong,fast,local"))
    router_chain_local:        str = field(default_factory=lambda: _e("ROUTER_CHAIN_LOCAL", "local"))
    router_chain_auto:         str = field(default_factory=lambda: _e("ROUTER_CHAIN_AUTO", "fast,strong,local"))

    # Groq (intent router + vision)
    groq_api_key:              str = field(default_factory=lambda: _secret("GROQ_API_KEY"))
    groq_model:                str = field(default_factory=lambda: _e("GROQ_MODEL", ""))
    groq_vision_model:         str = field(default_factory=lambda: _e("GROQ_VISION_MODEL", ""))

    # Web search (Tavily primary, Exa fallback — see actions/search_providers.py)
    tavily_api_key:            str = field(default_factory=lambda: _secret("TAVILY_API_KEY"))
    exa_api_key:                str = field(default_factory=lambda: _secret("EXA_API_KEY"))

    # Spotify (optional)
    spotify_client_id:         str = field(default_factory=lambda: _secret("SPOTIFY_CLIENT_ID"))
    spotify_client_secret:     str = field(default_factory=lambda: _secret("SPOTIFY_CLIENT_SECRET"))

    max_history_turns:         int = 20


@dataclass
class IntegrationsConfig:
    # Google OAuth (Calendar + Gmail) — see actions/google_auth.py's
    # module docstring for the one-time setup flow and how the resulting
    # token is stored (encrypted, same pattern as the memory DB in
    # memory/db_crypto.py — never a plaintext token file on disk).
    google_client_id:     str = field(default_factory=lambda: _secret("GOOGLE_CLIENT_ID"))
    google_client_secret: str = field(default_factory=lambda: _secret("GOOGLE_CLIENT_SECRET"))
    # Local redirect server port used only during the one-time consent
    # flow (google_auth_setup.py) — must match the redirect URI registered
    # in Google Cloud Console exactly (http://localhost:<port>/).
    google_oauth_port:    int = field(default_factory=lambda: _ei("GOOGLE_OAUTH_PORT", 8765))
    # Default city for morning_briefing's weather section when the user
    # doesn't specify one — optional; the briefing just skips weather
    # entirely if this is blank rather than guessing a city.
    home_city:            str = field(default_factory=lambda: _e("HOME_CITY", ""))


@dataclass
class AgentConfig:
    max_replan_attempts:  int = 2
    step_timeout_seconds: int = 120
    planner_model:        str = field(default_factory=lambda: _e("GEMINI_MODEL", ""))


def _get_or_create_ws_token() -> str:
    """WS_AUTH_TOKEN: read straight from .env; generated fresh and
    appended to .env on first run if it isn't there yet — nothing to
    set by hand. Used by ui/ws_server.py to reject connections from
    anything that isn't the real Electron UI (the loopback WebSocket
    has no other auth)."""
    existing = _e("WS_AUTH_TOKEN")
    if existing:
        return existing

    import secrets as _secrets_mod
    new_token = _secrets_mod.token_urlsafe(32)

    try:
        env_path = Path(__file__).parent / ".env"
        with open(env_path, "a", encoding="utf-8") as f:
            f.write(f"\nWS_AUTH_TOKEN={new_token}\n")
        _log.info("Generated a new WebSocket auth token and saved it to .env.")
    except Exception as e:
        _log.warning(f"Could not persist WS_AUTH_TOKEN to .env ({e}) — using an in-memory "
                      f"token for this session only.")
    return new_token


@dataclass
class FridayConfig:
    voice: VoiceConfig = field(default_factory=VoiceConfig)
    brain: BrainConfig = field(default_factory=BrainConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    integrations: IntegrationsConfig = field(default_factory=IntegrationsConfig)
    log_level:    str  = "INFO"
    show_latency: bool = True
    base_dir:     Path = field(default_factory=lambda: Path(__file__).parent)
    ws_auth_token: str = field(default_factory=_get_or_create_ws_token)
    file_trash_max_entries: int = field(default_factory=lambda: _ei("FILE_TRASH_MAX_ENTRIES", 20))

    @property
    def prompt_path(self) -> Path:
        return self.base_dir / "core" / "prompt.txt"

    @property
    def memory_db_path(self) -> Path:
        return self.base_dir / "memory" / "friday_memory.db"

    @property
    def obsidian_vault_path(self) -> Path:
        import os
        override = os.environ.get("OBSIDIAN_VAULT_PATH", "").strip()
        return Path(override) if override else self.base_dir / "FRIDAY_Brain"

    def validate(self):
        import logging
        log = logging.getLogger("friday.config")
        p = self.brain.llm_provider

        if   p == "nvidia"     and not self.brain.nvidia_nim_api_key:
            log.warning("LLM_PROVIDER=nvidia but NVIDIA_NIM_API_KEY not set in .env")
        elif p == "nvidia"     and not self.brain.nvidia_nim_model:
            log.warning("LLM_PROVIDER=nvidia but NVIDIA_NIM_MODEL not set in .env")
        elif p == "gemini"     and not self.brain.gemini_api_key:
            log.warning("LLM_PROVIDER=gemini but GEMINI_API_KEY not set in .env")
        elif p == "gemini"     and not self.brain.gemini_model:
            log.warning("LLM_PROVIDER=gemini but GEMINI_MODEL not set in .env")
        elif p == "ollama":
            if not self.brain.ollama_model:
                log.warning("LLM_PROVIDER=ollama but OLLAMA_MODEL not set in .env")
            log.info(f"LLM: Ollama ({self.brain.ollama_model}) @ {self.brain.ollama_base_url}")
        elif p in ("auto", "fast", "strong", "local", "smart"):
            # Router modes — validate the tier(s) actually reachable rather
            # than one specific credential, since which credential matters
            # depends on which tier(s) the configured chain visits.
            has_fast = bool(self.brain.fast_model_1_base_url or self.brain.fast_model_2_base_url)
            has_strong = bool(self.brain.nvidia_nim_api_key or self.brain.gemini_api_key)
            has_local = bool(self.brain.ollama_model)
            if p == "fast" and not has_fast:
                log.warning("LLM_PROVIDER=fast but neither FAST_MODEL_1_* nor FAST_MODEL_2_* is set in .env")
            elif p == "strong" and not has_strong:
                log.warning("LLM_PROVIDER=strong but neither NVIDIA_NIM_API_KEY nor GEMINI_API_KEY is set in .env")
            elif p == "local" and not has_local:
                log.warning("LLM_PROVIDER=local but OLLAMA_MODEL not set in .env")
            elif p == "auto" and not (has_fast or has_strong or has_local):
                log.warning("LLM_PROVIDER=auto but no tier (FAST/STRONG/LOCAL) has anything configured in .env")
            elif p == "smart" and not (has_strong and has_local):
                log.warning("LLM_PROVIDER=smart sends tool-flavored requests to STRONG (needs NVIDIA_NIM_API_KEY "
                            "or GEMINI_API_KEY) and everything else to LOCAL (needs OLLAMA_MODEL) — one of those is missing")
            log.info(f"LLM: Model Router ({p}) — fast={has_fast} strong={has_strong} local={has_local}")
        else:
            log.info(f"LLM: {p}")

        if self.voice.tts_provider == "elevenlabs" and not self.voice.elevenlabs_api_key:
            log.warning("TTS_PROVIDER=elevenlabs but ELEVENLABS_API_KEY not set in .env")
        elif self.voice.tts_provider == "elevenlabs" and not self.voice.elevenlabs_model:
            log.warning("TTS_PROVIDER=elevenlabs but ELEVENLABS_MODEL not set in .env")
        if self.voice.tts_provider == "fish_audio" and not self.voice.fish_audio_api_key:
            log.warning("TTS_PROVIDER=fish_audio but FISH_AUDIO_API_KEY not set in .env")
        if not self.voice.edge_tts_voice:
            log.info("EDGE_TTS_VOICE not set — edge-tts (the final TTS fallback) will use its own default voice")
        if not self.voice.whisper_model:
            log.warning("WHISPER_MODEL not set in .env — speech-to-text will fail to load")
        if not self.brain.groq_api_key:
            log.info("GROQ_API_KEY not set — intent router uses local rules only (still fast)")
        if not self.brain.gemini_api_key:
            log.info("GEMINI_API_KEY not set — screen vision and planner will use active provider")
        if bool(self.integrations.google_client_id) != bool(self.integrations.google_client_secret):
            log.warning("Only one of GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET is set in .env — "
                        "both are needed before running google_auth_setup.py")

        log.info(f"Startup: LLM={p} | STT=Whisper/{self.voice.whisper_model or '?'} | TTS={self.voice.tts_provider}")


config = FridayConfig()
