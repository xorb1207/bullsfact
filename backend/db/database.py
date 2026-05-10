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

    # alert_log post-mortem 컬럼 (M3 부가) + user_id (멀티유저)
    if "alert_log" in existing_tables:
        cols = {c["name"] for c in inspector.get_columns("alert_log")}
        with engine.begin() as conn:
            for col_name in ("price_7d", "price_30d", "return_7d", "return_30d"):
                if col_name not in cols:
                    conn.execute(text(f"ALTER TABLE alert_log ADD COLUMN {col_name} FLOAT"))
            if "user_id" not in cols:
                conn.execute(text("ALTER TABLE alert_log ADD COLUMN user_id INTEGER"))

    # llm_call_log.user_id 추가 (멀티유저 비용 추적)
    if "llm_call_log" in existing_tables:
        cols = {c["name"] for c in inspector.get_columns("llm_call_log")}
        if "user_id" not in cols:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE llm_call_log ADD COLUMN user_id INTEGER"))

    # 멀티유저 마이그레이션 — 사용자별 데이터 분리
    # 4개 테이블에 user_id 컬럼 추가 + 기존 행은 OWNER 소속으로 일괄 매핑
    _migrate_to_multiuser_tables(inspector, existing_tables)


def _migrate_to_multiuser_tables(inspector, existing_tables: list[str]) -> None:
    """user_id 컬럼 추가 + 기존 데이터 OWNER 로 일괄 매핑.

    OWNER User 가 없으면 매핑 스킵 (스키마만 추가). bot 시작 시 OWNER 생성되면
    그때 다시 호출돼서 매핑 됨. idempotent.
    """
    from sqlalchemy import text
    target_tables = ["watchlist", "position", "threshold_alert", "sell_reminder", "feedback"]

    with engine.begin() as conn:
        # 1. user_id 컬럼 추가 (없으면)
        for tbl in target_tables:
            if tbl not in existing_tables:
                continue
            cols = {c["name"] for c in inspector.get_columns(tbl)}
            if "user_id" not in cols:
                conn.execute(text(f"ALTER TABLE {tbl} ADD COLUMN user_id INTEGER"))

        # 2. OWNER 가 있으면 user_id NULL 인 기존 행을 OWNER 소속으로
        owner_row = conn.execute(
            text("SELECT id FROM user WHERE tier='OWNER' ORDER BY id ASC LIMIT 1")
        ).fetchone() if "user" in existing_tables else None
        if owner_row:
            owner_id = owner_row[0]
            for tbl in target_tables:
                if tbl not in existing_tables:
                    continue
                conn.execute(text(f"UPDATE {tbl} SET user_id = :uid WHERE user_id IS NULL"),
                             {"uid": owner_id})


def get_db():
    """FastAPI Depends용 세션 제너레이터."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
