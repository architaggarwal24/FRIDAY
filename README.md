# F.R.I.D.A.Y. v6

**F.R.I.D.A.Y. — Female Replacement Intelligent Digital Assistant Youth**

A Windows-first, multimodal desktop AI assistant with voice interaction, multi-provider LLM routing, tool/action execution, long-term memory, browser/computer control, and an Electron + React HUD.

> **GitHub-safe repository:** API keys, OAuth tokens, local memory, databases, FAISS indexes, model files, logs, uploads, and other machine-specific runtime state are intentionally excluded from version control.

---

## Features

- **Multi-provider LLMs:** NVIDIA NIM, Google Gemini, Ollama, plus configurable OpenAI-compatible endpoints through LiteLLM.
- **Model routing:** `fast`, `strong`, `local`, and `auto` modes with configurable fallback chains.
- **Voice pipeline:** faster-whisper STT, WebRTC VAD, openWakeWord, ElevenLabs / Fish Audio / edge-tts.
- **Agent execution:** multi-step planning, task queues, step-result chaining, and known-failure recovery.
- **Desktop automation:** application launching, browser control, computer control, file operations, screen processing, volume/brightness, and other Windows actions.
- **Productivity integrations:** Gmail, Google Calendar, reminders, focus sessions, weather, web search, YouTube, Spotify, and messaging.
- **Safety layer:** Sentinel risk classification and confirmation gates for higher-risk actions.
- **Long-term memory:** Obsidian-compatible Markdown vault + optional FAISS semantic retrieval.
- **Encrypted local memory:** SQLite conversation/action data with optional at-rest encryption using `cryptography` + the OS keyring.
- **Desktop UI:** Electron + React + Webpack, with a WebSocket bridge between the Python backend and renderer.
- **3D memory graph:** `3d-force-graph` visualization of the Obsidian-style memory graph.

---

## Tech Stack

### Backend

| Area | Technology |
|---|---|
| Language | Python |
| Configuration | `python-dotenv`, dataclasses |
| LLM SDKs | OpenAI-compatible SDK, Google GenAI, Ollama, Groq, LiteLLM |
| LLM providers | NVIDIA NIM, Gemini, Ollama, configurable OpenAI-compatible endpoints |
| Speech-to-text | faster-whisper |
| Voice activity detection | WebRTC VAD |
| Wake word | openWakeWord |
| Text-to-speech | ElevenLabs, Fish Audio, edge-tts |
| Semantic memory | FAISS + Sentence Transformers |
| Local memory | SQLite |
| Encryption | cryptography + keyring |
| Browser automation | Playwright |
| Desktop automation | PyAutoGUI, PyWin32, PyCaw, MSS |
| APIs / networking | aiohttp, requests, websockets |
| Google integrations | Google Calendar API, Gmail API, Google OAuth |
| Media | yt-dlp, MoviePy, pydub, whisper-timestamped |
| Documents | PyPDF2, python-docx, openpyxl |

### Frontend

| Area | Technology |
|---|---|
| Desktop shell | Electron 41 |
| UI | React 18 |
| Build | Webpack 5 + Babel |
| Animation | Framer Motion |
| Charts | Recharts |
| Markdown | react-markdown + remark-gfm |
| 3D graph | 3d-force-graph + Three.js |
| Computer vision | MediaPipe Tasks Vision |
| Styling / font | CSS + JetBrains Mono |

---

## Repository Structure

```text
FRIDAY_v6/
├── actions/                  # Tool/action implementations
│   ├── browser_control.py
│   ├── calendar.py
│   ├── coding_agent.py
│   ├── computer_control.py
│   ├── file_controller.py
│   ├── gmail.py
│   ├── google_auth.py
│   ├── screen_processor.py
│   ├── web_search.py
│   └── ...
├── agent/                    # Planning and task execution
│   ├── planner.py
│   ├── executor.py
│   └── task_queue.py
├── brain/                    # LLM orchestration and intent handling
│   ├── llm.py
│   ├── model_router.py
│   ├── router.py
│   └── handlers.py
├── core/                     # Shared prompts and safety configuration
├── memory/                   # Memory implementation (runtime data ignored)
│   ├── long_term.py
│   ├── obsidian_vault.py
│   ├── memory_store.py
│   ├── graph_export.py
│   └── db_crypto.py
├── sentinel/                 # Cross-tool safety/risk classification
├── verifier/                 # Action/result verification
├── utils/                    # Shared utility helpers
├── voice/                    # STT, TTS, VAD, wake word, audio devices
├── ui/                       # Electron + React desktop application
│   ├── main.js
│   ├── preload.js
│   ├── ws_server.py
│   ├── renderer/
│   ├── package.json
│   └── webpack.config.js
├── FRIDAY_Brain/             # Local Obsidian-compatible memory vault
│   └── Start Here.md
├── tests/                    # Regression/integration tests
├── .env.example              # Safe configuration template
├── .gitignore                # GitHub safety/runtime exclusions
├── config.py                 # Central configuration loader
├── main.py                   # Backend entry point
├── start.py                  # Main supervised/voice startup flow
├── setup.bat                 # Windows dependency setup
├── launch.bat                # Push-to-talk launch
├── launch_wake.bat           # Wake-word launch
├── launch_text.bat           # Text-only launch
└── requirements.txt          # Python dependencies
```

---

## Quick Start — Windows

### 1. Clone the repository

```powershell
git clone <your-repository-url>
cd FRIDAY_v6
```

### 2. Create the Python environment

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

Or use:

```powershell
setup.bat
```

### 3. Install the frontend

```powershell
cd ui
npm install
cd ..
```

`ui/package-lock.json` is committed so the frontend dependency tree can be reproduced with `npm ci`.

### 4. Configure secrets

```powershell
copy .env.example .env
```

Edit `.env` and add only the providers/integrations you intend to use.

**Never upload `.env` to GitHub.**

### 5. Install Playwright browsers

If browser automation is enabled:

```powershell
playwright install
```

### 6. Launch

```powershell
launch.bat
```

Other launch modes:

```powershell
launch_wake.bat
launch_text.bat
```

For microphone/device diagnostics:

```powershell
python check_mic.py
```

---

## LLM Provider Configuration

Set `LLM_PROVIDER` in `.env`.

| Mode | Required configuration | Purpose |
|---|---|---|
| `nvidia` | `NVIDIA_NIM_API_KEY`, `NVIDIA_NIM_MODEL` | NVIDIA NIM |
| `gemini` | `GEMINI_API_KEY`, `GEMINI_MODEL` | Google Gemini |
| `ollama` | Local Ollama | Local inference |
| `fast` | `FAST_MODEL_1_*` / `FAST_MODEL_2_*` | Configurable OpenAI-compatible endpoints |
| `strong` | NVIDIA/Gemini configuration | Strong-provider chain |
| `local` | Ollama | Local-only routing |
| `auto` | Router configuration | Automatic tier selection |

Fallback behavior is controlled by:

```text
ROUTER_CHAIN_FAST
ROUTER_CHAIN_STRONG
ROUTER_CHAIN_LOCAL
ROUTER_CHAIN_AUTO
```

---

## Voice

### STT

FRIDAY uses `faster-whisper`.

Typical configuration:

```text
WHISPER_MODEL=large-v3
WHISPER_DEVICE=cuda
```

Use a smaller model or CPU if your machine cannot support the default CUDA configuration.

### TTS

Supported providers include:

- ElevenLabs
- Fish Audio
- edge-tts

The API keys belong in `.env`, never in source files.

### Wake word

Wake-word mode uses openWakeWord. A custom model can be supplied locally under `models/`, but model binaries are intentionally ignored by Git.

---

## Memory Architecture

FRIDAY's long-term memory is **not** the old `memory/long_term.json` file.

The current implementation uses:

```text
FRIDAY_Brain/
├── People/
│   ├── Me.md
│   └── <relationship>.md
├── Preferences.md
├── Wishes.md
└── Notes.md
```

FRIDAY manages specific Markdown blocks and preserves user-written content outside those managed blocks.

FAISS + Sentence Transformers provide optional semantic retrieval over the memory. The generated index and metadata stay local and are ignored by Git.

### Why there is only one brain/vault

The repository previously contained both `FRIDAY_Brain/` and `vault/`. They were effectively duplicate Obsidian-style memory locations.

The application configuration resolves its default Obsidian path to:

```text
FRIDAY_Brain/
```

Therefore **`FRIDAY_Brain/` is the canonical memory vault**. The obsolete `vault/` directory has been removed.

---

## Tests

All regression tests now live under:

```text
tests/
```

The regression tests are standalone Python scripts rather than `unittest.TestCase` classes.

Run one test from the repository root:

```powershell
python tests/test_memory_recall.py
```

Run all regression scripts on Windows PowerShell:

```powershell
Get-ChildItem tests\test_*.py | ForEach-Object {
    python $_.FullName
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
```

The tests are primarily focused on regression coverage for memory, provider fallback/switching, browser/messaging wiring, injection prevention, focus-session privacy, posture-watch privacy, Sentinel coverage, and related fixes.

> Some tests exercise Windows-specific functionality. Run them on Windows for the most representative results.

---

## GitHub Safety

### Do not commit

The `.gitignore` intentionally excludes:

- `.env` and credentials
- Google OAuth tokens
- SQLite databases and WAL/SHM files
- encrypted runtime memory files
- personal long-term memory JSON
- FAISS indexes/metadata
- runtime monitor/quota state
- model binaries and local model directories
- logs and uploads
- Python caches and virtual environments
- Node/Electron dependencies and build output

### Safe to commit

- Python source code
- React/Electron source code
- `requirements.txt`
- `ui/package.json`
- `ui/package-lock.json`
- `.env.example`
- tests
- configuration templates
- prompts and static safety configuration
- the non-personal `FRIDAY_Brain/Start Here.md`

---

## Integrations

FRIDAY can optionally connect to services such as:

- NVIDIA NIM
- Google Gemini
- Ollama
- Groq
- Tavily / Exa
- ElevenLabs / Fish Audio
- Google Calendar / Gmail
- Spotify
- YouTube
- Playwright-supported browsers

Each integration is optional unless its feature is required by your chosen configuration.

---

## Security Notes

FRIDAY can control applications, files, browsers, messaging, and other system functions. Treat it as privileged desktop software.

Before running an unfamiliar tool/action:

1. Review the action implementation.
2. Review Sentinel risk classification.
3. Use a dedicated test environment where appropriate.
4. Keep credentials in environment variables or the OS credential store.
5. Do not commit personal memory, OAuth tokens, logs, screenshots, or generated databases.

---

## Current Project Scope

This repository is a **Windows-first personal desktop AI assistant**, not a hosted web service. GPU-backed speech/LLM features can require substantial local resources, and some integrations require third-party accounts or API keys.

For reproducible public releases, pinning exact Python package versions and documenting the supported CUDA/cuDNN combination would be the next hardening step.
