"""Passwords, sessions and form protection — all on the standard library.

No third-party security code is used on purpose: `hashlib.scrypt` and `hmac` are part of
Python, audited, and leave nothing extra to install on a server where new wheels are a
gamble.

    password   scrypt with a random salt, compared in constant time
    session    a signed cookie: id, login time, signature; the secret lives in the database
    forms      every POST carries a token tied to the session
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import sqlite3
import time
from dataclasses import dataclass

SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
SESSION_COOKIE = "eduvoice_session"


# ------------------------------------------------------------------ passwords


def hash_password(password: str) -> str:
    """`scrypt$salt$hash`, both parts base64. Deliberately slow to check."""
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P)
    return f"scrypt${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, salt_b64, digest_b64 = stored.split("$")
        if algorithm != "scrypt":
            return False
        digest = hashlib.scrypt(
            password.encode(),
            salt=base64.b64decode(salt_b64),
            n=SCRYPT_N,
            r=SCRYPT_R,
            p=SCRYPT_P,
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest, base64.b64decode(digest_b64))


# ------------------------------------------------------------------- sessions


def session_secret(connection: sqlite3.Connection) -> bytes:
    """One secret per installation, created on first use and kept in the database."""
    row = connection.execute("SELECT value FROM settings WHERE key = 'session_secret'").fetchone()
    if row:
        return base64.b64decode(row["value"])
    secret = secrets.token_bytes(32)
    connection.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES ('session_secret', ?)",
        (base64.b64encode(secret).decode(),),
    )
    connection.commit()
    return secret


def _sign(secret: bytes, payload: str) -> str:
    return base64.urlsafe_b64encode(
        hmac.new(secret, payload.encode(), hashlib.sha256).digest()
    ).decode()


def make_session(secret: bytes, user_id: int) -> str:
    payload = f"{user_id}:{int(time.time())}"
    return f"{payload}:{_sign(secret, payload)}"


@dataclass(frozen=True, slots=True)
class Session:
    user_id: int
    issued_at: int


def read_session(secret: bytes, cookie: str | None, max_age_s: int) -> Session | None:
    """Returns the session only if the signature matches and it has not expired."""
    if not cookie:
        return None
    try:
        user_id, issued_at, signature = cookie.rsplit(":", 2)
        payload = f"{user_id}:{issued_at}"
    except ValueError:
        return None
    if not hmac.compare_digest(signature, _sign(secret, payload)):
        return None
    if time.time() - int(issued_at) > max_age_s:
        return None
    return Session(user_id=int(user_id), issued_at=int(issued_at))


# ---------------------------------------------------------------------- forms


def csrf_token(secret: bytes, cookie: str | None) -> str:
    """A token bound to the current session: it cannot be reused from another browser."""
    return _sign(secret, f"csrf:{cookie or ''}")


def csrf_ok(secret: bytes, cookie: str | None, token: str | None) -> bool:
    return bool(token) and hmac.compare_digest(csrf_token(secret, cookie), token or "")


# ------------------------------------------------------------- login throttle


class LoginThrottle:
    """Slows down password guessing: a few wrong tries and that login waits.

    Counted per login *and* per machine, both normalised. Two mistakes were possible here
    and both were made:

    · The key was the text exactly as submitted, while the account was looked up with the
      spaces trimmed off. So " operator" and "operator  " were separate counters that
      opened the same account, and an attacker could guess for ever simply by padding the
      field — defeating the only guessing control the CRM has.
    · Counting by login alone lets anyone keep a real operator locked out of their own
      account for as long as they care to keep trying. Including the caller's address
      means that costs the attacker their own address, not the operator's shift.
    """

    def __init__(self, attempts: int, block_s: int) -> None:
        self._attempts = attempts
        self._block_s = block_s
        self._failures: dict[tuple[str, str], list[float]] = {}

    @staticmethod
    def key(login: str, source: str = "") -> tuple[str, str]:
        return (login.strip().casefold(), source)

    def blocked_for(self, login: str, source: str = "") -> int:
        key = self.key(login, source)
        recent = [t for t in self._failures.get(key, []) if time.time() - t < self._block_s]
        self._failures[key] = recent
        if len(recent) < self._attempts:
            return 0
        return int(self._block_s - (time.time() - recent[0]))

    def failed(self, login: str, source: str = "") -> None:
        self._failures.setdefault(self.key(login, source), []).append(time.time())

    def passed(self, login: str, source: str = "") -> None:
        self._failures.pop(self.key(login, source), None)
