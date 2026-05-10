"""
SQLAlchemy ORM 모델.
"""
from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, Float, Boolean, DateTime, JSON, Index
)
from .database import Base


class Watchlist(Base):
    __tablename__ = "watchlist"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, nullable=True, index=True)         # User.id (마이그레이션 후 NOT NULL 효과)
    ticker = Column(String(32), nullable=False, index=True)
    source = Column(String(16), nullable=False)  # "yfinance" | "binance"
    name = Column(String(128), nullable=True)    # 회사명 (yfinance.info.longName)
    added_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    active = Column(Boolean, nullable=False, default=True)


# (user_id, ticker) 검색 가속
Index("ix_watchlist_user_ticker", Watchlist.user_id, Watchlist.ticker)


class AlertLog(Base):
    __tablename__ = "alert_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, nullable=True, index=True)             # User.id (사용자별 발동 이력)
    ticker = Column(String(32), nullable=False, index=True)
    strength = Column(String(16), nullable=False)  # "weak" | "strong"
    price = Column(Float, nullable=False)
    rsi = Column(Float, nullable=True)
    bb_lower = Column(Float, nullable=True)
    source = Column(String(16), nullable=False)
    reasons = Column(JSON, nullable=True)
    sent_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    # M3 부가: 알림 후속 추적 (post-mortem) — 발동 후 D+7 / D+30 종가
    price_7d = Column(Float, nullable=True)
    price_30d = Column(Float, nullable=True)
    return_7d = Column(Float, nullable=True)         # decimal (0.05 = +5%)
    return_30d = Column(Float, nullable=True)


class BacktestResult(Base):
    __tablename__ = "backtest_result"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String(32), nullable=False, index=True)
    start_date = Column(DateTime, nullable=False)
    end_date = Column(DateTime, nullable=False)
    strategy_params = Column(JSON, nullable=False)
    win_rate = Column(Float, nullable=True)
    mdd = Column(Float, nullable=True)
    total_return = Column(Float, nullable=True)
    trade_count = Column(Integer, nullable=True)
    details = Column(JSON, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


Index("ix_alert_log_ticker_sent", AlertLog.ticker, AlertLog.sent_at.desc())


class Position(Base):
    """
    보유 포지션. 사용자별 — 가족 각자 자기 평단/마일스톤.
    수익률 = current_price / avg_cost - 1 이 마일스톤(0.5/1.0/2.0/4.0/6.0)을
    돌파하면 알림. 가장 높이 도달한 마일스톤만 저장 (한 번 알림 → 다음 마일스톤만 감시).
    """
    __tablename__ = "position"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, nullable=True, index=True)           # User.id
    ticker = Column(String(32), nullable=False, index=True)
    qty = Column(Float, nullable=False)
    avg_cost = Column(Float, nullable=False)
    highest_milestone = Column(Float, nullable=False, default=0.0)
    notes = Column(String(512), nullable=True)
    added_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


Index("ix_position_user_ticker", Position.user_id, Position.ticker)


class ThresholdAlert(Base):
    """
    가격/VIX/F&G 임계치 돌파 알림 룰.

    metric_type:
      - "price"     → ticker 종가가 threshold 돌파
      - "vix"       → ^VIX 값이 threshold 돌파
      - "fg_cnn"    → CNN Fear & Greed 점수
      - "fg_crypto" → Crypto Fear & Greed 점수

    값 정의는 둘 중 하나:
      A) abs_value          : 절대값 (예: SOXL $110)
      B) ref_window + ref_pct : 상대값 (예: 52주 고점 대비 -32%)
                                threshold = ref_value * (1 + ref_pct)

    ref_window: "high_252d" | "low_252d" | "ema_50d"
    """
    __tablename__ = "threshold_alert"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, nullable=True, index=True)           # User.id
    metric_type = Column(String(16), nullable=False, index=True)
    ticker = Column(String(32), nullable=True, index=True)        # price 일 때만
    direction = Column(String(8), nullable=False)                  # "above" | "below"

    abs_value = Column(Float, nullable=True)
    ref_window = Column(String(16), nullable=True)
    ref_pct = Column(Float, nullable=True)

    tier = Column(String(8), nullable=True)                        # "T1" | "T2" | "T3"
    priority = Column(String(8), nullable=False, default="MED")    # "HIGH" | "MED" | "LOW"
    note = Column(String(512), nullable=True)

    active = Column(Boolean, nullable=False, default=True, index=True)
    triggered_at = Column(DateTime, nullable=True)
    last_value = Column(Float, nullable=True)                      # 마지막 평가 시 메트릭 값 (디버깅용)
    re_arm_after_h = Column(Integer, nullable=True)                # NULL = 수동만
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


Index("ix_threshold_alert_active_metric", ThresholdAlert.active, ThresholdAlert.metric_type)


class SellReminder(Base):
    """
    매도 캘린더 리마인더 (양도세 250만원 분할 매도 등 일정 알림).

    매매전략 §1.2 — 한국 해외주식 양도세 22%, 250만원/년 공제 활용.
    target_date 까지 D-N 매일 일일 브리핑에 첨부. 완료는 /reminder done.
    """
    __tablename__ = "sell_reminder"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, nullable=True, index=True)           # User.id
    title = Column(String(128), nullable=False)        # "1차 매도 — TQQQ 15 + SOXL 5"
    target_date = Column(DateTime, nullable=False, index=True)
    notes = Column(String(512), nullable=True)         # "차익 ~$1,390 (공제 내)"
    days_before = Column(Integer, nullable=False, default=7)   # D-N 부터 알림
    active = Column(Boolean, nullable=False, default=True, index=True)
    done_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class EventCalibration(Base):
    """
    이벤트별 RSI 임계치 캘리브레이션 결과 (M2-A).

    grid search 로 산출된 "이벤트 X 임박 시 적중률 최고 RSI" 저장.
    스캐너가 시그널 생성 시 이 테이블 참조해 RSI threshold 동적 교체.

    적중률 정의: 시그널 발생일 D 에서 D+forward_days 종가가 +target_return% 이상.
    """
    __tablename__ = "event_calibration"

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_type = Column(String(16), nullable=False, index=True)   # "cpi" | "fomc" | "nfp"
    ticker = Column(String(32), nullable=False, index=True)
    rsi_threshold = Column(Float, nullable=False)                 # grid search 결과
    bb_std = Column(Float, nullable=True)                          # 향후 확장 (현재 미사용)
    hit_rate = Column(Float, nullable=False)                       # 0~1
    sample_count = Column(Integer, nullable=False)                 # 평가 사이클 수
    forward_days = Column(Integer, nullable=False, default=5)
    target_return = Column(Float, nullable=False, default=0.02)
    lookback_days = Column(Integer, nullable=False, default=730)   # 백테스트 기간
    last_calibrated_at = Column(DateTime, nullable=False, default=datetime.utcnow)


Index("ix_event_calibration_event_ticker", EventCalibration.event_type, EventCalibration.ticker)


class LLMCache(Base):
    """
    LLM 결과 캐시 — 동일 (purpose, key)에 대한 반복 호출 방지.

    매크로 해설 등 사용자 무관 결과는 1번 호출 → N명에게 같은 본문.
    /why TICKER 는 시간 윈도 단위 캐싱 (30분).

    expires_at 지나면 만료 (재호출). cleanup은 별도 배치 (선택).
    """
    __tablename__ = "llm_cache"

    id = Column(Integer, primary_key=True, autoincrement=True)
    purpose = Column(String(32), nullable=False, index=True)   # "macro_briefing" | "why_ticker" | "why_macro" | ...
    cache_key = Column(String(256), nullable=False, index=True)
    result_text = Column(JSON, nullable=False)                 # {"text": ..., "citations": [...]}
    cost_usd = Column(Float, nullable=False)                   # 원래 호출 비용 (절감액 추적용)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=False, index=True)


Index("ix_llm_cache_purpose_key", LLMCache.purpose, LLMCache.cache_key)


class User(Base):
    """
    멀티유저 진입 골격 — telegram_chat_id 기반 식별.
    단일 사용자 모드 시 첫 호출에 OWNER 자동 생성.

    tier:
      - OWNER:   본인. 일일 캡 대 (env MAX_DAILY_LLM_USD).
      - TRUSTED: 가족 핵심. 일일 캡 중 ($0.30 default).
      - LIMITED: 지인. 일일 캡 소 ($0.10 default).
    """
    __tablename__ = "user"

    id = Column(Integer, primary_key=True, autoincrement=True)
    telegram_chat_id = Column(String(64), nullable=False, unique=True, index=True)
    name = Column(String(64), nullable=True)
    tier = Column(String(16), nullable=False, default="LIMITED")
    llm_daily_cap_usd = Column(Float, nullable=True)           # tier default override
    active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class Feedback(Base):
    """
    사용자 의견/제안 — 가족 needs 발굴.
    /feedback 명령으로 누적, /admin feedback list 로 본인이 검토.
    """
    __tablename__ = "feedback"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, nullable=True, index=True)
    text = Column(String(2048), nullable=False)
    done_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)


class LLMCallLog(Base):
    """Synthesizer/Analyst의 LLM 호출 기록. 일일 비용 리포트에서 집계."""
    __tablename__ = "llm_call_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    model = Column(String(64), nullable=False)
    purpose = Column(String(32), nullable=False)             # "synthesizer" | "analyst:news" | ...
    ticker = Column(String(32), nullable=True, index=True)
    user_id = Column(Integer, nullable=True, index=True)     # User.id (멀티유저 비용 추적)
    input_tokens = Column(Integer, nullable=False)
    output_tokens = Column(Integer, nullable=False)
    cache_read_tokens = Column(Integer, nullable=False, default=0)
    cache_creation_tokens = Column(Integer, nullable=False, default=0)
    cost_cents = Column(Float, nullable=False)
    latency_ms = Column(Integer, nullable=True)
    called_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
