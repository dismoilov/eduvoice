"""The CRM service: one FastAPI application, server-rendered pages, no build step.

Runs as its own process (`eduvoice-crm`) next to the voice bridge and shares only the
database with it. Restarting the CRM never touches a call in progress — that separation
is the reason the two are not one program.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles

from crm.config import settings
from crm.deps import NotLoggedIn, page
from crm.security import session_secret
from crm.views import (
    admin,
    analytics,
    api,
    auth,
    calls,
    contacts,
    dashboard,
    knowledge,
    media,
    tickets,
)
from store.db import open_database

log = logging.getLogger("crm")


def create_app(db_path: Path | str | None = None) -> FastAPI:
    application = FastAPI(title="EduVoice CRM", docs_url=None, redoc_url=None)
    application.state.db_path = Path(db_path or settings.db_path)

    connection = open_database(application.state.db_path)
    try:
        application.state.session_secret = session_secret(connection)
    finally:
        connection.close()

    # Lists and transcripts are mostly text: compressing them costs a millisecond and
    # saves tens of kilobytes on every page.
    application.add_middleware(GZipMiddleware, minimum_size=1024)
    application.mount("/static", StaticFiles(directory=str(settings.static_dir)), name="static")
    for module in (
        auth,
        dashboard,
        calls,
        tickets,
        contacts,
        knowledge,
        analytics,
        admin,
        media,
        api,
    ):
        application.include_router(module.router)

    @application.exception_handler(NotLoggedIn)
    async def _not_logged_in(request: Request, _exc: NotLoggedIn):
        from fastapi.responses import JSONResponse

        from crm.deps import redirect

        # A page goes to the login form; a background refresh gets 401, so the script
        # can send the operator there instead of freezing on numbers from an hour ago.
        if request.url.path.startswith("/api/"):
            return JSONResponse({"error": "login_required"}, status_code=401)
        return redirect(f"/login?next={request.url.path}")

    @application.exception_handler(403)
    async def _forbidden(request: Request, _exc):
        return page(request, "error.html", None, status_code=403, code=403, message="no_access")

    @application.exception_handler(404)
    async def _not_found(request: Request, _exc):
        if request.url.path.startswith("/static"):
            return PlainTextResponse("not found", status_code=404)
        return page(request, "error.html", None, status_code=404, code=404, message="empty")

    @application.exception_handler(sqlite3.Error)
    async def _database_busy(request: Request, exc: sqlite3.Error):
        """Two processes share the database; a lock must look like a message, not a crash."""
        log.warning("database error on %s: %s", request.url.path, exc)
        return page(request, "error.html", None, status_code=503, code=503, message="db_busy")

    @application.get("/health", include_in_schema=False)
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "eduvoice-crm"}

    return application


app = create_app()


def main() -> None:
    import uvicorn

    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log.info("CRM on http://%s:%d (database %s)", settings.host, settings.port, settings.db_path)
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="warning")


if __name__ == "__main__":
    main()
