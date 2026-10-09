from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.api import events_router, router
from app.core.config import Settings
from app.core.security import LocalWebSecurity
from app.services.connection_manager import ConnectionManager
from app.services.local_file_service import LocalFileService
from app.services.profile_manager import ProfileManager
from app.services.progress_manager import ProgressManager
from app.services.transfer_manager import TransferManager
from app.state import AppState
from app.storage import Storage


logger = logging.getLogger("minisftp")

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "web" / "templates"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = Settings()
    settings.validate_runtime()
    storage = Storage(settings.database_path)
    await storage.connect()
    await storage.mark_startup_interrupted()
    progress = ProgressManager(interval=0.25)
    state = AppState(
        settings=settings,
        storage=storage,
        security=LocalWebSecurity(),
        profiles=ProfileManager(settings.ssh_config_path),
        connections=ConnectionManager(settings),
        local=LocalFileService(settings),
        progress=progress,
        transfers=None,  # type: ignore[arg-type]
    )
    state.transfers = TransferManager(settings, storage, state.connections, state.local, progress)
    await state.transfers.start()
    app.state.state = state
    try:
        yield
    finally:
        await state.transfers.shutdown()
        await state.connections.close_all()
        await storage.close()


app = FastAPI(title="MiniSFTP Web", version="0.1.0", lifespan=lifespan)
app.include_router(router)
app.include_router(events_router)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "web" / "static")), name="static")


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    state: AppState = request.app.state.state
    state.security.check_host(request)
    return templates.TemplateResponse(request, "index.html", {"app_name": state.settings.app_name})


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled request error for %s", request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal server error", "code": "internal_error"})


def run() -> None:
    settings = Settings()
    settings.validate_runtime()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    print(f"MiniSFTP Web: http://{settings.host}:{settings.port}")
    uvicorn.run("app.main:app", host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    run()
