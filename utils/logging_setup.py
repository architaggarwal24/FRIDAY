"""
F.R.I.D.A.Y. — utils/logging_setup.py
Pretty console logging with colors and timestamps, plus a rotating
plain-text log file on disk — so there's a record of what happened
after the terminal window closes, not just while you're watching it.
"""

import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Optional

DEFAULT_LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
DEFAULT_LOG_FILE = "friday.log"
DEFAULT_MAX_BYTES = 5_000_000     # ~5 MB per file before rotating
DEFAULT_BACKUP_COUNT = 5          # keep 5 old files around (~25 MB total, then oldest drops off)


class ColorFormatter(logging.Formatter):
    COLORS = {
        logging.DEBUG:    "\033[90m",   # dark gray
        logging.INFO:     "\033[36m",   # cyan
        logging.WARNING:  "\033[33m",   # yellow
        logging.ERROR:    "\033[31m",   # red
        logging.CRITICAL: "\033[35m",   # magenta
    }
    RESET = "\033[0m"
    BOLD = "\033[1m"

    def format(self, record):
        color = self.COLORS.get(record.levelno, self.RESET)
        msg = super().format(record)
        return f"{color}{msg}{self.RESET}"


def setup_logging(
    level: str = "INFO",
    log_dir: Optional[Path] = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    backup_count: int = DEFAULT_BACKUP_COUNT,
    to_file: bool = True,
):
    """Configure the root logger: colored console output + a rotating file log.

    Fully backward compatible — existing `setup_logging(level)` calls in
    start.py / main.py don't need to change; they'll just start getting a
    logs/friday.log file for free.

    to_file=False skips the file handler entirely (useful for short-lived
    tooling/diagnostics that shouldn't grow logs/ on every run).
    """
    numeric_level = getattr(logging, level.upper(), logging.INFO)

    root = logging.getLogger()
    root.setLevel(numeric_level)
    root.handlers.clear()

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(
        ColorFormatter(
            fmt="%(asctime)s  %(name)-20s  %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    root.addHandler(console_handler)

    if to_file:
        try:
            target_dir = Path(log_dir) if log_dir else DEFAULT_LOG_DIR
            target_dir.mkdir(parents=True, exist_ok=True)
            file_handler = logging.handlers.RotatingFileHandler(
                target_dir / DEFAULT_LOG_FILE,
                maxBytes=max_bytes,
                backupCount=backup_count,
                encoding="utf-8",
            )
            # Plain formatter for the file — no ANSI color codes, since
            # those just show up as garbage escape sequences when the log
            # is opened outside a terminal. Includes the date too, since
            # unlike the console this file outlives a single run.
            file_handler.setFormatter(
                logging.Formatter(
                    fmt="%(asctime)s  %(levelname)-8s  %(name)-20s  %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S",
                )
            )
            root.addHandler(file_handler)
        except Exception as e:
            # Logging setup itself must never be why the app fails to
            # start — fall back to console-only and say so, once, loudly.
            root.warning(f"Could not set up file logging ({e}) — console only this run.")

    # Silence noisy third-party loggers
    for noisy in ["httpx", "httpcore", "urllib3", "sounddevice", "faster_whisper"]:
        logging.getLogger(noisy).setLevel(logging.WARNING)
