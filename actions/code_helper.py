"""
code_helper.py — F.R.I.D.A.Y. Code Helper
Writes, edits, explains, runs, and auto-fixes code.
Uses the active LLM provider from brain/llm.py — no direct Gemini dependency.
"""

import subprocess
import sys
import re
import time
from pathlib import Path

BASE_DIR           = Path(__file__).resolve().parent.parent
DESKTOP            = Path.home() / "Desktop"
MAX_BUILD_ATTEMPTS = 3


# ── LLM helper — routes through the active provider ──────────────────────────

def _llm(prompt: str) -> str:
    """
    Synchronous LLM call that uses whatever provider is active (NIM / Ollama).
    Falls back chain is handled inside brain.llm automatically.
    """
    import asyncio
    from brain.llm import _quick_llm
    try:
        loop = asyncio.get_running_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(asyncio.run, _quick_llm(prompt))
                return future.result(timeout=60)
        else:
            return loop.run_until_complete(_quick_llm(prompt))
    except Exception as e:
        raise RuntimeError(f"LLM call failed: {e}") from e


# ── Utilities ─────────────────────────────────────────────────────────────────

def _clean_code(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
    text = re.sub(r"\n?```$", "", text)
    return text.strip()


def _resolve_save_path(output_path: str, language: str) -> Path:
    ext_map = {
        "python": ".py", "py": ".py",
        "javascript": ".js", "js": ".js",
        "typescript": ".ts", "ts": ".ts",
        "html": ".html", "css": ".css",
        "java": ".java", "cpp": ".cpp", "c": ".c",
        "bash": ".sh", "shell": ".sh", "powershell": ".ps1",
        "sql": ".sql", "json": ".json", "rust": ".rs", "go": ".go",
    }
    if output_path:
        p = Path(output_path)
        return p if p.is_absolute() else DESKTOP / p
    ext = ext_map.get((language or "python").lower(), ".py")
    return DESKTOP / f"friday_code{ext}"


def _read_file(file_path: str) -> tuple[str, str]:
    if not file_path:
        return "", "No file path provided."
    p = Path(file_path)
    if not p.exists():
        return "", f"File not found: {file_path}"
    try:
        return p.read_text(encoding="utf-8"), ""
    except Exception as e:
        return "", f"Could not read file: {e}"


def _save_file(path: Path, content: str) -> str:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return f"Saved to: {path}"
    except Exception as e:
        return f"Could not save: {e}"


def _preview(code: str, lines: int = 10) -> str:
    all_lines = code.splitlines()
    preview   = "\n".join(all_lines[:lines])
    suffix    = f"\n... ({len(all_lines) - lines} more lines)" if len(all_lines) > lines else ""
    return preview + suffix


def _has_error(output: str) -> bool:
    signals = ["error", "exception", "traceback", "syntaxerror",
               "nameerror", "typeerror", "stderr", "failed", "crash"]
    return any(s in output.lower() for s in signals)


def _run_file(path: Path, args: list, timeout: int = 30) -> str:
    interpreters = {
        ".py":  [sys.executable],
        ".js":  ["node"],
        ".ts":  ["ts-node"],
        ".sh":  ["bash"],
        ".ps1": ["powershell", "-File"],
        ".rb":  ["ruby"],
        ".php": ["php"],
    }
    interp = interpreters.get(path.suffix.lower())
    if not interp:
        return f"No interpreter for {path.suffix}."
    try:
        result = subprocess.run(
            interp + [str(path)] + (args or []),
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=timeout, cwd=str(path.parent)
        )
        parts = []
        if result.stdout.strip(): parts.append(f"Output:\n{result.stdout.strip()}")
        if result.stderr.strip(): parts.append(f"Stderr:\n{result.stderr.strip()}")
        return "\n\n".join(parts) if parts else "Executed with no output."
    except subprocess.TimeoutExpired:
        return f"Timed out after {timeout}s."
    except FileNotFoundError:
        return f"Interpreter not found: {interp[0]}."
    except Exception as e:
        return f"Execution error: {e}"


# ── Core LLM actions ──────────────────────────────────────────────────────────

def _write(description: str, language: str, output_path: str) -> tuple[str, Path]:
    lang = language or "python"
    prompt = (
        f"You are an expert {lang} developer.\n"
        f"Write clean, working, well-commented {lang} code for the description below.\n\n"
        f"Rules:\n"
        f"- Output ONLY the code. No explanation, no markdown, no backticks.\n"
        f"- Add helpful inline comments.\n"
        f"- Handle errors and edge cases properly.\n"
        f"- Use modern best practices.\n\n"
        f"Description: {description}\n\nCode:"
    )
    code = _clean_code(_llm(prompt))
    path = _resolve_save_path(output_path, lang)
    _save_file(path, code)
    return code, path


def _fix_code(code: str, error_output: str, description: str) -> str:
    prompt = (
        f"You are an expert debugger.\n"
        f"The code below failed with the following error. Fix it.\n"
        f"Return ONLY the corrected code — no explanation, no markdown, no backticks.\n\n"
        f"Original goal: {description}\n\n"
        f"Error:\n{error_output[:2000]}\n\n"
        f"Broken code:\n{code}\n\nFixed code:"
    )
    return _clean_code(_llm(prompt))


def _edit(file_path: str, instruction: str) -> str:
    content, err = _read_file(file_path)
    if err:
        return err
    prompt = (
        f"You are an expert code editor.\n"
        f"Apply the following change to the code below.\n"
        f"Return ONLY the complete updated code — no explanation, no markdown, no backticks.\n\n"
        f"Change: {instruction}\n\n"
        f"Original code:\n{content}\n\nUpdated code:"
    )
    edited = _clean_code(_llm(prompt))
    status = _save_file(Path(file_path), edited)
    return f"File edited. {status}\n\nPreview:\n{_preview(edited)}"


def _explain(file_path: str = "", code: str = "") -> str:
    if file_path and not code:
        code, err = _read_file(file_path)
        if err:
            return err
    if not code:
        return "Please provide code or a file path to explain."
    prompt = (
        f"Explain what this code does in simple, clear language.\n"
        f"Focus on: what it does, how it works, and any important details.\n"
        f"Be concise — 3 to 6 sentences maximum.\n\n"
        f"Code:\n{code[:4000]}\n\nExplanation:"
    )
    return _llm(prompt)


def _build(description: str, language: str, output_path: str, args: list) -> str:
    if not description:
        return "Please describe what you want me to build."
    lang = language or "python"
    try:
        code, path = _write(description, lang, output_path)
    except Exception as e:
        return f"Could not write initial code: {e}"

    for attempt in range(1, MAX_BUILD_ATTEMPTS + 1):
        output = _run_file(path, args)
        if not _has_error(output):
            return (
                f"Build complete. Working after {attempt} attempt{'s' if attempt > 1 else ''}. "
                f"Saved to {path}.\n\nOutput:\n{output}"
            )
        try:
            code = _fix_code(code, output, description)
            _save_file(path, code)
        except Exception as e:
            return f"Could not fix code on attempt {attempt}: {e}"

    return (
        f"Could not build a working version after {MAX_BUILD_ATTEMPTS} attempts. "
        f"Last code saved to: {path}"
    )


# ── Public entry point ────────────────────────────────────────────────────────

def handle_code_helper(params: dict) -> str:
    action      = params.get("action", "write").lower()
    description = params.get("description", "")
    language    = params.get("language", "python")
    output_path = params.get("output_path", "")
    file_path   = params.get("file_path", "") or output_path
    args        = params.get("args", [])

    try:
        if action == "write":
            if not description:
                return "Please describe what you want me to write."
            code, path = _write(description, language, output_path)
            return f"Code written. Saved to: {path}\n\nPreview:\n{_preview(code)}"

        elif action == "edit":
            if not file_path:
                return "Please provide a file path to edit."
            return _edit(file_path, description)

        elif action == "explain":
            return _explain(file_path=file_path)

        elif action == "run":
            if not file_path:
                return "Please provide a file path to run."
            p = Path(file_path)
            if not p.exists():
                return f"File not found: {file_path}"
            return _run_file(p, args)

        elif action == "build":
            return _build(description, language, output_path, args)

        else:
            return f"Unknown action: {action}. Use write | edit | explain | run | build."

    except Exception as e:
        return f"Code helper failed: {e}"