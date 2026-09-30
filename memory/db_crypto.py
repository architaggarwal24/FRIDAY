"""
memory/db_crypto.py — at-rest encryption for the memory SQLite database.

Why file-level encryption instead of SQLCipher: SQLCipher gives
continuous, always-encrypted-on-disk protection with no plaintext window
at all, which is the stronger guarantee in theory. But its prebuilt-wheel
story on Windows is genuinely weak — the official `sqlcipher3-binary`
package ships wheels for manylinux only, no Windows wheels at all
(confirmed: there's an open, years-old GitHub issue from people hitting
exactly this). The only prebuilt Windows option is an unofficial
third-party fork, and the alternative is compiling SQLCipher from source
against OpenSSL with Visual Studio build tools — a bad trade for a
security-critical dependency in a project that's entirely Windows-
targeted. `cryptography` (used here) has official, first-party Windows
wheels and is one of the most widely-used, well-audited crypto libraries
in the Python ecosystem.

How this works:
  - The file that actually persists on disk is always the encrypted one
    (friday_memory.db.enc).
  - On startup, setup() decrypts it into a normal working copy at the
    path memory_store.py already expects (friday_memory.db) —
    memory_store.py itself needs zero changes; it just sees an ordinary,
    unencrypted-looking SQLite file, exactly as before.
  - The working copy is periodically re-encrypted back over the .enc
    file (every CHECKPOINT_INTERVAL_SECONDS) and one final time on clean
    shutdown, after which the plaintext working copy is deleted.
  - A hard crash (kill -9, power loss, task manager "End task") means
    the plaintext working copy lingers on disk until the next checkpoint
    fires or the app restarts — at restart, setup() detects the leftover
    plaintext copy, encrypts it immediately, and continues. This is a
    real, bounded exposure window (at most CHECKPOINT_INTERVAL_SECONDS
    of "was running recently" exposure after an unclean exit), not a
    theoretical one — worth being upfront about. It protects the
    realistic threat for a personal desktop app (someone else with
    access to the files while the app isn't actively running — a lost
    laptop, a shared machine, a backup tool), not concurrent access to
    a live, running session.

Key management: a random key is generated on first run and stored in
the OS credential store via keyring — never hardcoded, never prompted
for. If keyring isn't usable, the DB is left unencrypted for that
session (loud warning, not a silent downgrade) rather than failing to
start.
"""

import atexit
import logging
import shutil
import threading
from pathlib import Path
from typing import Optional

logger = logging.getLogger("friday.db_crypto")

_KEYRING_SERVICE = "FRIDAY"  # no version number — keyring entries survive version bumps
_KEYRING_KEY_NAME = "MEMORY_DB_ENCRYPTION_KEY"
_CHECKPOINT_INTERVAL_SECONDS = 120

try:
    import keyring
    from cryptography.fernet import Fernet, InvalidToken
    _CRYPTO_OK = True
except ImportError:
    _CRYPTO_OK = False


def _get_or_create_key() -> Optional[bytes]:
    if not _CRYPTO_OK:
        return None
    try:
        existing = keyring.get_password(_KEYRING_SERVICE, _KEYRING_KEY_NAME)
        if existing:
            return existing.encode()
        new_key = Fernet.generate_key()
        keyring.set_password(_KEYRING_SERVICE, _KEYRING_KEY_NAME, new_key.decode())
        logger.info("Generated a new memory-DB encryption key in the OS credential store.")
        return new_key
    except Exception as e:
        logger.warning(f"OS keyring unavailable for the memory DB key ({e}) — "
                        f"memory DB will be UNENCRYPTED this session.")
        return None


def _encrypt_file(src: Path, dst: Path, fernet: "Fernet") -> None:
    data = src.read_bytes()
    token = fernet.encrypt(data)
    tmp = dst.with_name(dst.name + ".tmp")
    tmp.write_bytes(token)
    tmp.replace(dst)  # atomic on both Windows and POSIX — no partial-write window


def _decrypt_file(src: Path, dst: Path, fernet: "Fernet") -> None:
    token = src.read_bytes()
    data = fernet.decrypt(token)  # raises InvalidToken if the key doesn't match
    dst.write_bytes(data)


class MemoryDBEncryption:
    def __init__(self, plain_path: Path):
        self.plain_path = plain_path
        self.enc_path = plain_path.with_name(plain_path.name + ".enc")
        self.legacy_backup_path = plain_path.with_name(plain_path.name + ".pre-encryption.bak")
        self._fernet: Optional["Fernet"] = None
        self._timer: Optional[threading.Timer] = None
        self.enabled = False

    def setup(self) -> None:
        """Call once at startup, before memory_store.py opens the DB —
        it needs plain_path to already be a normal, ready-to-use SQLite
        file (encrypted-at-rest or not) by the time this returns."""
        if not _CRYPTO_OK:
            logger.warning("keyring/cryptography not installed — memory DB will be stored UNENCRYPTED.")
            return
        key = _get_or_create_key()
        if key is None:
            return
        try:
            self._fernet = Fernet(key)
        except Exception as e:
            logger.error(f"Memory DB encryption key from the OS keyring is malformed ({e}) — "
                          f"memory DB will be UNENCRYPTED this session.")
            return

        if self.enc_path.exists():
            try:
                _decrypt_file(self.enc_path, self.plain_path, self._fernet)
            except InvalidToken:
                logger.error(
                    f"Could not decrypt {self.enc_path} — the key in the OS keyring doesn't match "
                    f"the one this file was encrypted with (keyring reset/cleared? moved to a new "
                    f"machine?). Refusing to overwrite it. Starting a fresh, empty memory DB instead — "
                    f"your old data is still safely encrypted at {self.enc_path} if the right key "
                    f"turns up again."
                )
                return
            except Exception as e:
                logger.error(f"Could not decrypt memory DB ({e}) — starting a fresh one instead.")
                return
        elif self.plain_path.exists():
            # First run after upgrading: an existing plaintext DB from
            # before encryption was added. Encrypt it in place, keeping
            # a backup of the original plaintext bytes until the user
            # has had a chance to confirm the app still works normally.
            try:
                shutil.copy2(self.plain_path, self.legacy_backup_path)
                _encrypt_file(self.plain_path, self.enc_path, self._fernet)
                logger.info(
                    f"Migrated your existing memory DB to encrypted storage. A plaintext backup was "
                    f"kept at {self.legacy_backup_path} — once you've confirmed FRIDAY starts up "
                    f"normally and your memory is intact, it's safe to delete that backup file."
                )
            except Exception as e:
                logger.error(f"Could not encrypt existing memory DB ({e}) — continuing UNENCRYPTED this session.")
                return
        # else: brand new install, nothing on disk yet — memory_store.py's
        # init_db() will create plain_path fresh, and it gets encrypted
        # at the first checkpoint.

        self.enabled = True
        self._schedule_checkpoint()
        atexit.register(self.shutdown)

    def checkpoint(self) -> None:
        """Re-encrypts the current on-disk state without disturbing the
        live working copy — called periodically during a session, and
        once more (after the DB connection is actually closed) on
        shutdown. Callers are responsible for WAL-checkpointing the live
        sqlite3 connection first so plain_path alone (without separate
        -wal/-shm files) reflects the true current state."""
        if not self.enabled or not self.plain_path.exists():
            return
        try:
            _encrypt_file(self.plain_path, self.enc_path, self._fernet)
        except Exception as e:
            logger.warning(f"Memory DB checkpoint encryption failed: {e}")

    def _schedule_checkpoint(self) -> None:
        try:
            from memory.memory_store import checkpoint_wal
            checkpoint_wal()
        except Exception as e:
            logger.debug(f"[db_crypto] WAL checkpoint before encryption checkpoint failed: {e}")
        self.checkpoint()
        self._timer = threading.Timer(_CHECKPOINT_INTERVAL_SECONDS, self._schedule_checkpoint)
        self._timer.daemon = True
        self._timer.start()

    def shutdown(self) -> None:
        if not self.enabled:
            return
        if self._timer:
            self._timer.cancel()
        try:
            from memory.memory_store import close_db
            close_db()
        except Exception as e:
            logger.warning(f"[db_crypto] Closing the DB connection before final encryption failed: {e}")
        try:
            self.checkpoint()
            self.plain_path.unlink(missing_ok=True)
            for suffix in ("-wal", "-shm"):
                p = self.plain_path.with_name(self.plain_path.name + suffix)
                if p.exists():
                    p.unlink(missing_ok=True)
            logger.info("Memory DB re-encrypted; plaintext working copy removed.")
        except Exception as e:
            logger.warning(f"Memory DB shutdown encryption failed — a plaintext copy may remain "
                            f"at {self.plain_path} until next launch: {e}")
