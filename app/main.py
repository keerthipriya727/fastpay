import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.database import Base, engine
from app.routers import events, reconciliation, transactions

settings = get_settings()
logging.basicConfig(level=settings.log_level)
logger = logging.getLogger("setu")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # create_all is used instead of requiring an Alembic run for the demo
    # to work with zero setup steps; Alembic (see /alembic) is the real
    # migration path and is what CI/deploy should run instead of this.
    Base.metadata.create_all(bind=engine)
    logger.info("Startup complete. DB: %s", settings.database_url.split("://")[0])
    yield


app = FastAPI(
    title="FastPay Payment Service",
    description=(
        "Ingests payment lifecycle events, maintains transaction state, "
        "and reports reconciliation discrepancies between payment and "
        "settlement status."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

app.include_router(events.router)
app.include_router(transactions.router)
app.include_router(reconciliation.router)


@app.get("/health", tags=["ops"])
def health() -> dict:
    return {"status": "ok", "env": settings.app_env}


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})
