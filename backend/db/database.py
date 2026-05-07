"""
SQLAlchemy 엔진/세션 설정.

DATABASE_URL 환경변수로 SQLite ↔ PostgreSQL 교체 가능.
기본값은 backend/dipalert.db.
"""
import os
from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

_DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "dipalert.db"
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{_DEFAULT_DB_PATH}")

# SQLite는 멀티스레드 사용 시 check_same_thread=False 필요 (FastAPI 환경)
_connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(DATABASE_URL, connect_args=_connect_args, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
Base = declarative_base()


def init_db() -> None:
    from . import models  # noqa: F401  — 테이블 등록을 위해 import
    Base.metadata.create_all(bind=engine)
    _run_lightweight_migrations()


def _run_lightweight_migrations() -> None:
    """
    SQLite는 ALTER TABLE 일부 지원 — 신규 컬럼 추가만 idempotent하게 처리.
    Alembic 도입 전까지의 임시 솔루션.
    """
    from sqlalchemy import inspect, text
    inspector = inspect(engine)
    existing_tables = inspector.get_table_names()

    # watchlist.name 추가 (없으면)
    if "watchlist" in existing_tables:
        cols = {c["name"] for c in inspector.get_columns("watchlist")}
        if "name" not in cols:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE watchlist ADD COLUMN name VARCHAR(128)"))


def get_db():
    """FastAPI Depends용 세션 제너레이터."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
