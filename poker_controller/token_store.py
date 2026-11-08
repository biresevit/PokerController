"""Account login tokens kept on this PC, so a restarted worker resumes its accounts' sessions without logging in.

BluffPoker allows 30 logins per 15 minutes per IP: a worker restarted with more accounts than that could not log
them all in within the ~29 s a disconnected seat is kept. Each account's access/refresh pair is written to its own
file, encrypted with Windows DPAPI for the account the worker runs as (no other Windows user, and no other PC, can
read it). Files are replaced atomically; an unreadable or foreign file is ignored and the account logs in again.
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import logging
import os
import time
from pathlib import Path

log = logging.getLogger(__name__)

ENTROPY = b"PokerSuite.AccountTokens.v1"


def default_dir() -> Path:
    override = os.environ.get("POKERSUITE_TOKEN_DIR")
    if override:
        return Path(override)
    if os.name == "nt":
        return Path(r"C:\PokerSuite\state\tokens")
    return Path.home() / ".pokersuite" / "state" / "tokens"


# Tests point this to a temporary folder; None disables the store.
DIRECTORY: Path | None = default_dir()


if os.name == "nt":
    from ctypes import wintypes

    class _Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    def _blob(data: bytes):
        buffer = ctypes.create_string_buffer(data, len(data))
        return _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))), buffer

    def _crypt(function, data: bytes) -> bytes:
        source, keep = _blob(data)
        entropy, keep_entropy = _blob(ENTROPY)
        out = _Blob()
        # CRYPTPROTECT_UI_FORBIDDEN: the worker has no desktop.
        if not function(ctypes.byref(source), None, ctypes.byref(entropy), None, None, 0x1, ctypes.byref(out)):
            raise OSError(ctypes.get_last_error(), "DPAPI failed")
        try:
            return ctypes.string_at(out.pbData, out.cbData)
        finally:
            ctypes.windll.kernel32.LocalFree(out.pbData)

    def protect(data: bytes) -> bytes:
        return _crypt(ctypes.WinDLL("crypt32", use_last_error=True).CryptProtectData, data)

    def unprotect(data: bytes) -> bytes:
        return _crypt(ctypes.WinDLL("crypt32", use_last_error=True).CryptUnprotectData, data)
else:  # development machines only: the PCs run Windows
    def protect(data: bytes) -> bytes:
        return data

    def unprotect(data: bytes) -> bytes:
        return data


def _path(site: str, account: str) -> Path | None:
    if DIRECTORY is None:
        return None
    name = hashlib.sha256(f"{site}\n{account.casefold()}".encode()).hexdigest()[:32]
    return DIRECTORY / f"{name}.bin"


def load(site: str, account: str) -> dict | None:
    """The account's saved pair: access, refresh, expires_at (Unix time), player_id; None when there is none."""
    path = _path(site, account)
    if path is None or not path.exists():
        return None
    try:
        data = json.loads(unprotect(path.read_bytes()))
        if data.get("site") != site or data.get("account") != account.casefold():
            return None
        if not (isinstance(data.get("refresh"), str) and data["refresh"]):
            return None
        return data
    except Exception as exc:  # another Windows user's file, a damaged file: log in again
        log.info("Saved login of %s ignored: %s", account, type(exc).__name__)
        return None


def save(site: str, account: str, *, access: str, refresh: str, expires_at: float, player_id: str | None):
    path = _path(site, account)
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        body = json.dumps({"site": site, "account": account.casefold(), "access": access, "refresh": refresh,
                           "expires_at": expires_at, "player_id": player_id, "saved_at": time.time()})
        temporary = path.with_suffix(f".{os.getpid()}.tmp")
        temporary.write_bytes(protect(body.encode()))
        os.replace(temporary, path)
    except Exception as exc:  # the session goes on; only a restart would log in again
        log.warning("Could not save the login of %s: %s", account, type(exc).__name__)


def forget(site: str, account: str):
    path = _path(site, account)
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
