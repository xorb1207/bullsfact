"""
FastAPI 앱 팩토리.
"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.db import init_db
from .routes import watchlist, alerts, backtest


def create_app() -> FastAPI:
    app = FastAPI(
        title="dip-alert API",
        version="0.1.0",
        description="RSI + 볼린저 밴드 매수 알람 서비스",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.on_event("startup")
    def _startup() -> None:
        init_db()

    @app.get("/health", tags=["meta"])
    def health() -> dict:
        return {"status": "ok"}

    app.include_router(watchlist.router)
    app.include_router(alerts.router)
    app.include_router(backtest.router)

    return app


app = create_app()
