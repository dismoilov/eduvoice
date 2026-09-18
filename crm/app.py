"""The CRM service: one FastAPI application, server-rendered pages, no build step.

Runs as its own process (`eduvoice-crm`) next to the voice bridge and shares only the
database with it. Restarting the CRM never touches a call in progress — that separation
is the reason the two are not one program.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles

from crm.config import settings
from crm.deps import NotLoggedIn, page
from crm.security import session_secret
from crm.views import admin, analytics, auth, calls, contacts, dashboard, knowledge, media, tickets
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

    application.mount("/static", StaticFiles(directory=str(settings.static_dir)), name="static")
    for module in (auth, dashboard, calls, tickets, contacts, knowledge, analytics, admin, media):
        application.include_router(module.router)

    @application.exception_handler(NotLoggedIn)
    async def _not_logged_in(request: Request, _exc: NotLoggedIn):
        from crm.deps import redirect

        return redirect(f"/login?next={request.url.path}")

    @application.exception_handler(403)
    async def _forbidden(request: Request, _exc):
        return page(request, "error.html", None, status_code=403, code=403, message="no_access")

    @application.exception_handler(404)
    async def _not_found(request: Request, _exc):
        if request.url.path.startswith("/static"):
            return PlainTextResponse("not found", status_code=404)
        return page(request, "error.html", None, status_code=404, code=404, message="empty")

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
