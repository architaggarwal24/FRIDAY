"""
coding_agent.py — F.R.I.D.A.Y. Unified Code Engine

handle_code(params) is the single entry point for ALL coding tasks.
It assesses task complexity and routes automatically:

  TIER 1 — code_helper (direct LLM write, fastest)
    Triggers: single file, <50 lines expected, write/explain/run/fix

  TIER 2 — Aider (multi-file repo agent, uses active LLM)
    Triggers: multi-file, refactor, "across the project", "add feature to X"

  TIER 3 — Claude Code (deep reasoning agent, uses Ollama/NIM backend)
    Triggers: "build from scratch", architecture design, complex debugging,
              full project generation, anything Tier 2 struggled with

No user-facing configuration needed. FRIDAY just says "write this" and the
right engine fires.
"""

import os
import subprocess
import sys
import re
from pathlib import Path
from config import config


# ─────────────────────────────────────────────────────────────────────────────
# Task complexity assessor
# ─────────────────────────────────────────────────────────────────────────────

_TIER2_SIGNALS = {
    # scope signals
    "refactor", "rename", "across", "everywhere", "all files", "entire project",
    "codebase", "repository", "repo", "throughout", "migrate", "move",
    # structural signals
    "add feature", "add support", "integrate", "connect", "wire up",
    "multiple files", "several files", "all modules",
    # quality signals
    "fix all", "fix every", "fix bugs", "all errors", "all warnings",
    "code review", "review the", "audit",
    # architecture signals
    "restructure", "reorganize", "split into", "break into",
}

_TIER3_SIGNALS = {
    # project generation — specific phrases only, not generic "create a X"
    # (overly broad signals cause simple inline tasks to escalate to Claude Code)
    "build from scratch", "create from scratch", "from scratch",
    "build a full", "create a full", "build an entire",
    "build a complete", "create a complete", "create a simple assistant",
    "create a simple digital", "create a simple voice",
    "design the architecture", "architect",
    "full stack", "full application", "end to end",
    "digital assistant", "voice assistant", "ai assistant",
    # deep reasoning
    "why is this", "root cause", "investigate", "debug why",
    "complex", "hard bug", "tricky",
    # explicit escalation
    "use claude code", "use claude", "deep analysis",
}

def _assess_tier(task: str, path: str, action: str) -> int:
    """
    Returns 1, 2, or 3 based on task complexity.
    No hardcoded paths — reasons from the task description itself.
    """
    t = task.lower()
    a = (action or "").lower()

    # Explicit non-code actions → always Tier 1
    if a in ("explain", "run"):
        return 1

    # Tier 3 check first (highest specificity)
    if any(sig in t for sig in _TIER3_SIGNALS):
        return 3

    # Tier 2 check
    if any(sig in t for sig in _TIER2_SIGNALS):
        return 2

    # Path heuristics — folder path with no filename = project scope
    if path:
        p = Path(path)
        if p.is_dir() or (not p.suffix and not p.exists()):
            return 2

    # Multiple file paths mentioned in task
    file_mentions = len(re.findall(r'\b\w+\.\w{1,5}\b', task))
    if file_mentions >= 3:
        return 2

    # Default: Tier 1 (single file, quick task)
    return 1


# ─────────────────────────────────────────────────────────────────────────────
# Tier 1 — Direct LLM (code_helper logic, inline)
# ─────────────────────────────────────────────────────────────────────────────

def _clean_code(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
    text = re.sub(r"\n?```$", "", text)
    return text.strip()


def _llm(prompt: str) -> str:
    """Sync LLM call safe to run from any thread — always creates a fresh event loop."""
    import asyncio
    from brain.llm import _quick_llm
    try:
        # Always use asyncio.run() — creates its own loop, works in executor threads
        return asyncio.run(_quick_llm(prompt))
    except RuntimeError as e:
        if "cannot be called from a running event loop" in str(e):
            # We're somehow inside an async context — use a thread
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                return pool.submit(asyncio.run, _quick_llm(prompt)).result(timeout=90)
        raise RuntimeError(f"LLM call failed: {e}") from e
    except Exception as e:
        raise RuntimeError(f"LLM call failed: {e}") from e


def _save(path: Path, content: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return str(path)


def _run_file(path: Path, timeout: int = 30) -> str:
    interp = {".py": [sys.executable], ".js": ["node"], ".sh": ["bash"],
              ".ps1": ["powershell", "-File"], ".rb": ["ruby"]}
    cmd = interp.get(path.suffix.lower())
    if not cmd:
        return f"No interpreter for {path.suffix}"
    try:
        r = subprocess.run(cmd + [str(path)], capture_output=True, text=True,
                           timeout=timeout, cwd=str(path.parent))
        parts = []
        if r.stdout.strip(): parts.append(r.stdout.strip())
        if r.stderr.strip(): parts.append(f"stderr: {r.stderr.strip()}")
        return "\n".join(parts) or "Ran with no output."
    except subprocess.TimeoutExpired:
        return f"Timed out after {timeout}s."
    except Exception as e:
        return f"Run failed: {e}"


def _tier1(task: str, path: str, language: str, action: str) -> str:
    lang = language or "python"
    p    = Path(path) if path else None

    if action == "explain":
        if not p or not p.exists():
            return "No file to explain."
        code = p.read_text(encoding="utf-8", errors="replace")
        return _llm(f"Explain this {lang} code concisely in 3-5 sentences:\n\n{code[:4000]}")

    if action == "run":
        if not p or not p.exists():
            return f"File not found: {path}"
        return _run_file(p)

    # write / edit / fix — all are LLM → save
    if action in ("edit", "fix") and p and p.exists():
        existing = p.read_text(encoding="utf-8", errors="replace")
        prompt = (
            f"You are an expert {lang} developer.\n"
            f"Task: {task}\n\n"
            f"Apply this change to the code below. Return ONLY the complete updated code. "
            f"No explanation, no markdown fences.\n\n"
            f"Current code:\n{existing}\n\nUpdated code:"
        )
    else:
        prompt = (
            f"You are an expert {lang} developer.\n"
            f"Write clean, working, well-commented {lang} code for the task below.\n"
            f"Return ONLY the code. No explanation, no markdown fences.\n\n"
            f"Task: {task}\n\nCode:"
        )

    code = _clean_code(_llm(prompt))

    if not p:
        ext  = {"python": ".py", "javascript": ".js", "typescript": ".ts",
                "html": ".html", "css": ".css", "bash": ".sh"}.get(lang.lower(), ".py")
        p    = Path.home() / "Desktop" / f"friday_code{ext}"

    saved = _save(p, code)
    preview = "\n".join(code.splitlines()[:12])
    suffix  = f"\n... ({len(code.splitlines())-12} more lines)" if len(code.splitlines()) > 12 else ""
    return f"Done, boss. Saved to: {saved}\n\n{preview}{suffix}"


# ─────────────────────────────────────────────────────────────────────────────
# Tier 2 — Aider
# ─────────────────────────────────────────────────────────────────────────────

def _ensure_git(path: Path):
    if not (path / ".git").exists():
        for cmd in [["git","init"], ["git","add","-A"],
                    ["git","commit","-m","FRIDAY auto-init"]]:
            subprocess.run(cmd, cwd=str(path), capture_output=True)


def _tier2(task: str, path: str, language: str) -> str:
    try:
        import aider  # noqa
    except ImportError:
        return (
            "Aider not installed, boss. "
            "Run: pip install aider-chat  then retry."
        )

    proj = Path(path) if path else Path.cwd()
    # Auto-create if missing — handle_code mkdir'd it, but guard here too
    # in case _tier2 is called directly.
    if not proj.exists():
        try:
            proj.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            return f"Path not found and could not be created: {proj} ({e})"
    if proj.is_file():
        proj = proj.parent

    _ensure_git(proj)

    env = os.environ.copy()
    p   = config.brain.llm_provider

    cmd = [
        sys.executable, "-m", "aider",
        "--yes",              # never prompt for confirmation
        "--no-pretty",        # no ANSI colour codes in output
        "--no-stream",        # return full response at once
        "--no-browser",       # CRITICAL: stops Aider opening browser tabs on startup
        "--no-show-release-notes",  # suppress HISTORY.html popup
        "--no-check-update",  # skip update check (also triggers browser popup)
        "--no-gitignore",     # don't create .gitignore files in project folders
    ]

    if p == "nvidia":
        cmd += ["--model",           f"openai/{config.brain.nvidia_nim_model}",
                "--openai-api-key",  config.brain.nvidia_nim_api_key,
                "--openai-api-base", "https://integrate.api.nvidia.com/v1"]
    elif p == "gemini" and config.brain.gemini_api_key:
        env["GEMINI_API_KEY"] = config.brain.gemini_api_key
        cmd += ["--model", f"gemini/{config.brain.gemini_model}"]
    else:  # ollama (default when NIM is down)
        cmd += ["--model",           f"ollama/{config.brain.ollama_model}",
                "--ollama-api-base", config.brain.ollama_base_url]

    # If a specific file was given, pass it explicitly so Aider targets only that file.
    # This avoids Aider scanning the whole project and going interactive.
    target_file = path if path and Path(path).is_file() else None
    if target_file:
        cmd += [target_file]

    cmd += ["--message", task]

    try:
        result = subprocess.run(cmd, cwd=str(proj), env=env,
                                capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=300)
        out = (result.stdout or result.stderr or "").strip()
        if result.returncode == 0:
            summary = [l for l in out.splitlines()
                       if any(k in l.lower() for k in
                              ["applied","edited","created","commit","token","cost"])]
            return "Done, boss. Aider made the changes.\n\n" + \
                   ("\n".join(summary[-8:]) if summary else out[-600:])
        return f"Aider error, boss.\n\n{out[-600:]}"
    except subprocess.TimeoutExpired:
        return "Aider timed out (300s). Try scoping to specific files."
    except Exception as e:
        return f"Aider failed: {e}"


# ─────────────────────────────────────────────────────────────────────────────
# Tier 3 — Claude Code (Ollama/NIM backend)
# ─────────────────────────────────────────────────────────────────────────────

def _find_claude() -> str | None:
    for c in ["claude",
              str(Path.home()/"AppData"/"Roaming"/"npm"/"claude.cmd"),
              str(Path.home()/"AppData"/"Roaming"/"npm"/"claude"),
              "/usr/local/bin/claude"]:
        try:
            if subprocess.run([c,"--version"], capture_output=True,
                              timeout=4).returncode == 0:
                return c
        except Exception:
            pass
    return None


def _tier3(task: str, path: str) -> str:
    claude = _find_claude()
    if not claude:
        # Graceful fallback to Tier 2
        return _tier2(task, path, "")

    proj = Path(path) if path else Path.cwd()
    if proj.is_file():
        proj = proj.parent

    env = os.environ.copy()
    p   = config.brain.llm_provider

    # Point Claude Code at active provider
    if p == "nvidia":
        model = config.brain.nvidia_nim_model
        env["ANTHROPIC_BASE_URL"] = os.environ.get("NIM_BASE_URL", "https://integrate.api.nvidia.com")
        env["ANTHROPIC_API_KEY"]  = config.brain.nvidia_nim_api_key or "not-used"
    else:
        # Ollama (default for Tier 3 local)
        model = config.brain.ollama_model
        env["ANTHROPIC_BASE_URL"]   = config.brain.ollama_base_url
        env["ANTHROPIC_AUTH_TOKEN"] = "ollama"
        env["ANTHROPIC_API_KEY"]    = ""

    for v in ["ANTHROPIC_DEFAULT_HAIKU_MODEL","ANTHROPIC_DEFAULT_SONNET_MODEL",
              "ANTHROPIC_DEFAULT_OPUS_MODEL","CLAUDE_CODE_SUBAGENT_MODEL",
              "ANTHROPIC_CUSTOM_MODEL_OPTION"]:
        env[v] = model

    cmd = [claude, "--print", "--no-update", task]

    try:
        result = subprocess.run(cmd, cwd=str(proj), env=env,
                                capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=300)
        out = (result.stdout or result.stderr or "").strip()
        if result.returncode == 0:
            return f"Done, boss. Claude Code finished.\n\n{out[-1200:]}"
        # Claude Code failed — log the real reason before silently falling
        # back to Tier 2, so a Claude Code failure is actually diagnosable
        # instead of just quietly looking like Tier 2 ran the whole time.
        print(f"[CodingAgent] Claude Code exited {result.returncode}, falling back to Tier 2: {out[-500:]}")
        return _tier2(task, path, "")
    except subprocess.TimeoutExpired:
        return "Claude Code timed out. Falling back to Aider...\n\n" + _tier2(task, path, "")
    except Exception as e:
        print(f"[CodingAgent] Claude Code invocation failed, falling back to Tier 2: {e}")
        return _tier2(task, path, "")


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────

def handle_code(params: dict) -> str:
    task     = params.get("task", "").strip()
    path     = params.get("path", "").strip()
    language = params.get("language", "python").strip()
    action   = params.get("action", "write").lower().strip()

    if not task:
        return "What should I code, boss?"

    # Auto-create the project directory if it does not exist.
    # Previously tier2/tier3 returned "Path not found" and gave up.
    # Now we just mkdir and continue — essential for build-from-scratch tasks.
    if path:
        _p = Path(path)
        # If path has a file extension, ensure its parent directory exists.
        # If path is a directory (or has no extension), ensure the dir itself exists.
        _mkdir_target = _p.parent if _p.suffix else _p
        try:
            _mkdir_target.mkdir(parents=True, exist_ok=True)
        except Exception as _e:
            return f"Couldn't create project folder {_mkdir_target}: {_e}"

    tier = _assess_tier(task, path, action)

    if tier == 1:
        return _tier1(task, path, language, action)
    elif tier == 2:
        return _tier2(task, path, language)
    else:
        return _tier3(task, path)