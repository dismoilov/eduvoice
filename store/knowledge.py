"""The answers the assistant is allowed to give, as edited in the CRM.

The bridge asks for them every few seconds, so publishing an answer in the CRM changes
what callers hear without a deploy and without restarting anything. Drafts are invisible
here on purpose: an unfinished wording must never reach a citizen.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from store.db import connect

log = logging.getLogger("store.knowledge")

RELOAD_AFTER_S = 10.0


def published(connection) -> dict[str, dict[str, Any]]:
    rows = connection.execute(
        "SELECT faq_id, answer, keywords, source FROM knowledge WHERE status = 'published'"
    ).fetchall()
    return {
        row["faq_id"]: {
            "answer": row["answer"],
            "keywords": [
                k.strip().lower() for k in (row["keywords"] or "").split(",") if k.strip()
            ],
            "source": row["source"],
        }
        for row in rows
    }


class PublishedKnowledge:
    """Reads the published answers, at most once every few seconds."""

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._cache: dict[str, dict[str, Any]] = {}
        self._checked_at = 0.0
        self._stamp: str | None = None

    def entries(self) -> dict[str, dict[str, Any]]:
        """Current published answers; the database is only touched when it may have changed."""
        if time.monotonic() - self._checked_at < RELOAD_AFTER_S:
            return self._cache
        self._checked_at = time.monotonic()
        try:
            connection = connect(self._path)
            try:
                stamp = connection.execute(
                    "SELECT max(updated_at) || '/' || count(*) FROM knowledge"
                    " WHERE status = 'published'"
                ).fetchone()[0]
                if stamp != self._stamp:
                    self._cache = published(connection)
                    self._stamp = stamp
                    log.info("knowledge reloaded: %d published answers", len(self._cache))
            finally:
                connection.close()
        except Exception as exc:  # the call must go on with whatever we had
            log.warning("could not read the knowledge base: %s", exc)
        return self._cache
