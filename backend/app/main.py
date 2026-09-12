from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.external_routes import router as external_router
from app.api.routes import router as api_router
from app.config import settings
from app.db import init_db

app = FastAPI(
    title="Aurora AI",
    description="Dispensary profitability intelligence. Code calculates the money; every figure is deterministic.",
    version="0.1.0",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(api_router)
app.include_router(external_router)


@app.on_event("startup")
def _startup() -> None:
    init_db()
