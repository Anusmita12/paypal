import asyncio
import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm.exc import StaleDataError

import app.agent
import app.disputes
import app.payments
import app.webhooks
from app.api import router
from app.config import get_settings
from app.db import get_sessionmaker
from app.errors import AppError
from app.jobs import poller
from app.seed import seed_global

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("tradepilot")
FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"


async def maintenance(stop: asyncio.Event, interval: float = 30.0) -> None:
    """Periodic TTL cleanup. It never releases capture-pinned reservations (see expire_reservations)."""
    from app.approvals import expire_reservations

    def tick() -> None:
        with get_sessionmaker()() as db:
            if n := expire_reservations(db):
                log.info("released %d expired reservation(s)", n)
            db.commit()

    while not stop.is_set():
        try:
            await asyncio.to_thread(tick)
        except Exception:
            log.exception("maintenance tick failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    problems = settings.problems()
    if problems:
        # Live modes never silently fall back to fixtures.
        raise RuntimeError("Configuration errors:\n- " + "\n- ".join(problems))
    with get_sessionmaker()() as db:
        seed_global(db)
        db.commit()
    log.info("modes: ai=%s payment=%s channel3=%s", settings.ai_mode, settings.payment_mode, settings.channel3_mode)
    stop = asyncio.Event()
    tasks = (
        [asyncio.create_task(poller(stop, settings.jobs_poll_seconds)), asyncio.create_task(maintenance(stop))]
        if settings.run_jobs_in_process else []
    )
    yield
    stop.set()
    for task in tasks:
        await task


def create_app() -> FastAPI:
    app = FastAPI(title="TradePilot AI", version="0.1.0", lifespan=lifespan)

    @app.middleware("http")
    async def request_id(request: Request, call_next):
        rid = uuid.uuid4().hex[:16]
        request.state.request_id = rid
        response = await call_next(request)
        response.headers["x-request-id"] = rid
        return response

    def error(request: Request, status: int, code: str, message: str, details: dict | None = None) -> JSONResponse:
        rid = getattr(request.state, "request_id", None)
        return JSONResponse(
            status_code=status,
            content={"error": {"code": code, "message": message, "details": details or {}}, "request_id": rid},
            headers={"x-request-id": rid or ""},
        )

    @app.exception_handler(AppError)
    async def app_error(request: Request, exc: AppError):
        return error(request, exc.status, exc.code, exc.message, exc.details)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        details = {"errors": [{"loc": ".".join(map(str, e["loc"])), "msg": e["msg"]} for e in exc.errors()][:10]}
        return error(request, 422, "invalid_request", "Request failed validation.", details)

    @app.exception_handler(StaleDataError)
    async def stale(request: Request, exc: StaleDataError):
        return error(request, 409, "concurrent_update", "This record changed concurrently; reload and retry.")

    app.include_router(router)

    if FRONTEND_DIST.exists():
        app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        async def spa(path: str):
            if path.startswith("api/") or path == "api":
                return error_404()
            candidate = FRONTEND_DIST / path
            if path and candidate.is_file() and FRONTEND_DIST in candidate.resolve().parents:
                return FileResponse(candidate)
            return FileResponse(FRONTEND_DIST / "index.html")

        def error_404() -> JSONResponse:
            return JSONResponse(status_code=404, content={"error": {"code": "not_found", "message": "No such API route"}})

    return app


app = create_app()
