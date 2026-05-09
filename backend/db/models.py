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
    ticker = Column(String(32), nullable=False, unique=True, index=True)
    source = Column(String(16), nullable=False)  # "yfinance" | "binance"
    name = Column(String(128), nullable=True)    # 회사명 (yfinance.info.longName)
    added_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    active = Column(Boolean, nullable=False, default=True)


class AlertLog(Base):
    __tablename__ = "alert_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String(32), nullable=False, index=True)
    strength = Column(String(16), nullable=False)  # "weak" | "strong"
    price = Column(Float, nullable=False)
    rsi = Column(Float, nullable=True)
    bb_lower = Column(Float, nullable=True)
    source = Column(String(16), nullable=False)
    reasons = Column(JSON, nullable=True)
    sent_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)


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
    보유 포지션. 매매전략 MD §4 익절 룰의 입력.
    수익률 = current_price / avg_cost - 1 이 마일스톤(0.5/1.0/2.0/4.0/6.0)을
    돌파하면 알림. 가장 높이 도달한 마일스톤만 저장 (한 번 알림 → 다음 마일스톤만 감시).
    """
    __tablename__ = "position"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String(32), nullable=False, unique=True, index=True)
    qty = Column(Float, nullable=False)                            # 보유 수량 (fractional 허용)
    avg_cost = Column(Float, nullable=False)                       # 평단가 (USD)
    highest_milestone = Column(Float, nullable=False, default=0.0) # 0.5 = +50% 발동, ...
    notes = Column(String(512), nullable=True)
    added_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


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


class LLMCallLog(Base):
    """Synthesizer/Analyst의 LLM 호출 기록. 일일 비용 리포트에서 집계."""
    __tablename__ = "llm_call_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    model = Column(String(64), nullable=False)
    purpose = Column(String(32), nullable=False)             # "synthesizer" | "analyst:news" | ...
    ticker = Column(String(32), nullable=True, index=True)
    input_tokens = Column(Integer, nullable=False)
    output_tokens = Column(Integer, nullable=False)
    cache_read_tokens = Column(Integer, nullable=False, default=0)
    cache_creation_tokens = Column(Integer, nullable=False, default=0)
    cost_cents = Column(Float, nullable=False)
    latency_ms = Column(Integer, nullable=True)
    called_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
