"""Settings of the CRM service.

Everything that differs between a laptop and the server comes from the environment, so
the same code runs in both places without edits.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]

# The CRM and the bridge share one .env on the server.
load_dotenv(ROOT / ".env")


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


@dataclass(slots=True)
class CrmSettings:
    # --- where things live -------------------------------------------------
    db_path: Path = field(
        default_factory=lambda: Path(os.getenv("EDUVOICE_DB", str(ROOT / "data" / "eduvoice.db")))
    )
    recordings_dir: Path = field(
        default_factory=lambda: Path(os.getenv("RECORDINGS_DIR", "/var/spool/asterisk/monitor"))
    )
    templates_dir: Path = field(default_factory=lambda: ROOT / "crm" / "templates")
    static_dir: Path = field(default_factory=lambda: ROOT / "crm" / "static")

    # --- network -----------------------------------------------------------
    host: str = os.getenv("CRM_HOST", "127.0.0.1")
    port: int = _int("CRM_PORT", 9095)

    # --- sessions ----------------------------------------------------------
    # The secret is generated on first start and kept in the database, so sessions
    # survive a restart without anyone having to configure anything.
    session_hours: int = _int("CRM_SESSION_HOURS", 12)
    login_attempts: int = _int("CRM_LOGIN_ATTEMPTS", 5)
    login_block_s: int = _int("CRM_LOGIN_BLOCK_S", 60)

    # --- telephony (click to call) ----------------------------------------
    ari_url: str = os.getenv("ARI_URL", "http://127.0.0.1:8088")
    ari_user: str = os.getenv("ARI_USER", "eduvoice")
    ari_password: str = os.getenv("ARI_PASSWORD", "")
    dial_context: str = os.getenv("CRM_DIAL_CONTEXT", "from-internal")

    # --- service level -----------------------------------------------------
    # How long a new request may stay unresolved before it is shown as overdue.
    sla_hours: int = _int("CRM_SLA_HOURS", 24)

    bridge_health_url: str = os.getenv("BRIDGE_HEALTH_URL", "http://127.0.0.1:9093/health")


settings = CrmSettings()
