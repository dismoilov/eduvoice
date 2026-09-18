"""Calling a citizen from the CRM.

Asterisk dials the operator's own extension first and only then the citizen, so nobody
ever hears an empty line: the operator picks up, hears the ringing, and speaks.
"""

from __future__ import annotations

import logging

import httpx

from crm.config import settings

log = logging.getLogger("crm.telephony")


def call_citizen(*, extension: str, phone: str, timeout_s: float = 4.0) -> bool:
    """Starts the call through the Asterisk REST interface. False means it did not start."""
    if not extension.strip() or not phone.strip():
        log.warning("click to call needs both an extension and a number")
        return False
    try:
        response = httpx.post(
            f"{settings.ari_url}/ari/channels",
            params={
                "endpoint": f"PJSIP/{extension.strip()}",
                "extension": phone.strip(),
                "context": settings.dial_context,
                "priority": 1,
                "callerId": f"EduVoice <{extension.strip()}>",
                "timeout": 30,
            },
            auth=(settings.ari_user, settings.ari_password),
            timeout=timeout_s,
        )
    except Exception as exc:
        log.warning("could not reach Asterisk: %s", exc)
        return False
    if response.status_code >= 300:
        log.warning("Asterisk refused the call: %s %s", response.status_code, response.text[:200])
        return False
    return True
