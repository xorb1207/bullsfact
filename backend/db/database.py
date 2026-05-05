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


def get_db():
    """FastAPI Depends용 세션 제너레이터."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
