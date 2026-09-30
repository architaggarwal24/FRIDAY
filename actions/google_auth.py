"""
actions/google_auth.py — shared Google OAuth2 foundation for Calendar + Gmail.

This file does not call Calendar or Gmail itself. It exists so
actions/calendar.py and actions/gmail.py (both still to come) share ONE
consent screen and ONE stored token instead of each running their own
OAuth dance — get_credentials() is the one thing they'll both import.

One-time setup: run `python google_auth_setup.py` from the repo root
(requires GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET in .env — see the
comment block above them in .env.example for how to get those). It opens
your browser for Google's consent screen, then stores the resulting
token. Re-run any time to re-auth.

Token storage follows the exact same shape as the memory DB in
memory/db_crypto.py: Fernet symmetric encryption, key generated on first
use and kept in the OS credential store via `keyring` (never hardcoded,
never in .env). Unlike the memory DB, there's no plaintext working copy
step at all here — a token is small enough to decrypt straight into
memory (a Credentials object) and never touch disk unencrypted, at any
point, for any reason.

Scopes requested (kept to the minimum actions/calendar.py and
actions/gmail.py will need, requested together so the user only goes
through consent once):
  - calendar          — read/write events (create, move, delete)
  - gmail.readonly     — read/search/summarize
  - gmail.send         — send (always Sentinel-gated — see sentinel/core.py)
  - gmail.modify       — mark read, archive, label

If you add a scope later, the existing token won't have it — re-run
google_auth_setup.py to re-consent with the new scope added.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from utils.atomic_write import atomic_write_text

logger = logging.getLogger("friday.google_auth")

SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.modify",
]

_KEYRING_SERVICE = "FRIDAY"  # same service namespace as memory/db_crypto.py
_KEYRING_KEY_NAME = "GOOGLE_OAUTH_TOKEN_KEY"  # separate key from the memory DB's

try:
    import keyring
    from cryptography.fernet import Fernet, InvalidToken
    _CRYPTO_OK = True
except ImportError:
    _CRYPTO_OK = False


def _token_path() -> Path:
    from config import config
    return config.base_dir / "memory" / "google_token.enc"


def _get_or_create_key() -> Optional[bytes]:
    if not _CRYPTO_OK:
        return None
    try:
        existing = keyring.get_password(_KEYRING_SERVICE, _KEYRING_KEY_NAME)
        if existing:
            return existing.encode()
        new_key = Fernet.generate_key()
        keyring.set_password(_KEYRING_SERVICE, _KEYRING_KEY_NAME, new_key.decode())
        logger.info("Generated a new Google OAuth token encryption key in the OS credential store.")
        return new_key
    except Exception as e:
        logger.warning(f"OS keyring unavailable for the Google token key ({e}) — "
                        f"can't store or read a Google token this session.")
        return None


def _load_token_info() -> Optional[dict]:
    """Decrypts the stored token straight into memory — never written
    back out to disk unencrypted at any point."""
    path = _token_path()
    if not path.exists():
        return None
    key = _get_or_create_key()
    if key is None:
        return None
    try:
        fernet = Fernet(key)
        encrypted = path.read_text(encoding="ascii")
        data = fernet.decrypt(encrypted.encode("ascii"))
        return json.loads(data.decode("utf-8"))
    except InvalidToken:
        logger.error("Could not decrypt the stored Google token — the key in the OS keyring "
                      "doesn't match the one it was saved with (keyring reset/cleared? moved to "
                      "a new machine?). Re-run google_auth_setup.py to reconnect.")
        return None
    except Exception as e:
        logger.error(f"Could not read the stored Google token ({e}). Re-run google_auth_setup.py.")
        return None


def _save_token_info(info: dict) -> bool:
    key = _get_or_create_key()
    if key is None:
        return False
    try:
        fernet = Fernet(key)
        encrypted = fernet.encrypt(json.dumps(info).encode("utf-8"))
        atomic_write_text(_token_path(), encrypted.decode("ascii"))
        return True
    except Exception as e:
        logger.error(f"Could not save the Google token ({e}).")
        return False


def get_credentials():
    """The one function actions/calendar.py and actions/gmail.py will
    both call. Returns a valid, auto-refreshed google.oauth2.credentials
    .Credentials object, or None if nothing is connected yet (callers
    should give the user a clear 'not connected' message rather than
    raising — see handle_google_status in brain/handlers.py for the
    pattern)."""
    if not _CRYPTO_OK:
        logger.warning("keyring/cryptography not installed — can't use a Google account this session.")
        return None
    info = _load_token_info()
    if info is None:
        return None
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
    except ImportError:
        logger.error("google-auth-oauthlib / google-api-python-client not installed — "
                      "run: pip install -r requirements.txt")
        return None

    creds = Credentials.from_authorized_user_info(info, SCOPES)
    if creds.valid:
        return creds
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _save_token_info(json.loads(creds.to_json()))
            return creds
        except Exception as e:
            logger.error(f"Google token refresh failed ({e}). Re-run google_auth_setup.py to reconnect.")
            return None
    return None


def is_connected() -> bool:
    """Cheap check: does a usable token exist. Does NOT force a network
    refresh — safe to call often (e.g. every settings-panel refresh)."""
    return _load_token_info() is not None


def get_status() -> dict:
    """For the status handler (and, later, the Settings UI) — what's
    connected, without forcing a network call unless the cached token
    has actually expired."""
    if not _CRYPTO_OK:
        return {"connected": False, "reason": "keyring/cryptography not installed"}
    info = _load_token_info()
    if info is None:
        client_configured = bool(_client_config_present())
        return {
            "connected": False,
            "reason": "client credentials not set in .env" if not client_configured
                       else "no token saved yet — run google_auth_setup.py",
        }
    granted_scopes = set(info.get("scopes", []))
    missing = [s for s in SCOPES if s not in granted_scopes]
    creds = get_credentials()
    return {
        "connected": creds is not None,
        "scopes": sorted(granted_scopes),
        "missing_scopes": missing,
        "reason": None if creds is not None else "token present but refresh failed — re-run google_auth_setup.py",
    }


def _client_config_present() -> bool:
    from config import config
    return bool(config.integrations.google_client_id and config.integrations.google_client_secret)


def run_interactive_setup() -> dict:
    """The actual browser consent flow — deliberately NOT callable from
    a normal conversation turn (it blocks on browser interaction and
    opens a local port), only from google_auth_setup.py. Returns a
    status dict; raises on hard failures (missing client credentials,
    etc.) since the setup script is meant to surface those directly."""
    from config import config

    if not _CRYPTO_OK:
        raise RuntimeError("keyring/cryptography not installed — run: pip install -r requirements.txt")
    if not _client_config_present():
        raise RuntimeError(
            "GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET not set in .env — see the comment block "
            "above them in .env.example for how to get those from Google Cloud Console."
        )

    from google_auth_oauthlib.flow import InstalledAppFlow

    client_config = {
        "installed": {
            "client_id": config.integrations.google_client_id,
            "client_secret": config.integrations.google_client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [f"http://localhost:{config.integrations.google_oauth_port}/"],
        }
    }
    flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
    creds = flow.run_local_server(port=config.integrations.google_oauth_port)

    info = json.loads(creds.to_json())
    info["scopes"] = SCOPES  # from_authorized_user_info needs this key on reload
    if not _save_token_info(info):
        raise RuntimeError("Consent succeeded but the token couldn't be saved — check the OS "
                            "keyring is accessible (see the warning above, if any).")
    return get_status()
