"""
이벤트 캘린더 데이터 소스 (M1).

목적: DipBuy/Threshold 시그널 발동 시 "왜 지금 RSI가 낮은가?" 컨텍스트 한 줄 자동 주입.
외부 앱 없이 알림 안에서 즉시 파악 가능.

소스:
  - Finnhub: 미국 어닝스 캘린더 (FINNHUB_API_KEY 필요, 무료 60req/min)
  - FRED   : CPI / PPI / NFP 발표일 (FRED_API_KEY 필요, 무료, historical 포함)
  - FOMC   : 2026년 일정 하드코딩 (Fed 공식 8회)
  - DART   : 한국 공시 (DART_API_KEY 필요) — backward lookup, 현재 미사용 (stub)

설계 원칙:
  - 모든 소스 실패 시 빈 리스트 반환 (graceful degradation, 예외 전파 X)
  - 각 소스 silent skip (API 키 없으면 그 소스만 건너뜀)
  - 일별 캐싱 (같은 날 중복 호출 방지) — 15분 스캔마다 API 안 때림
  - M2를 위해 historical 발표일도 같이 저장 (FRED는 historical 제공)
"""
from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional

import requests

log = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# 타입
# ──────────────────────────────────────────────

@dataclass
class CalendarEvent:
    """단일 캘린더 이벤트 — Signal.reasons로 변환되는 원본."""
    ticker: Optional[str]    # None이면 매크로 (모든 종목 공통)
    event_type: str          # "earnings" | "cpi" | "fomc" | "ppi" | "nfp" | "dart"
    event_date: date
    days_until: int          # 0 = 오늘, 1 = 내일, 음수 = 과거 (DART backward용)
    description: str         # "NVDA 2026Q1 어닝", "CPI 발표"
    source: str              # "finnhub" | "fred" | "fed_static" | "dart"


# ──────────────────────────────────────────────
# 정적 데이터
# ──────────────────────────────────────────────

# FOMC 2026년 회의 종료일 (Fed 공식 — federalreserve.gov 발표 기준).
# 향후 매년 1월에 다음 해 일정으로 갱신 필요.
_FOMC_2026: list[date] = [
    date(2026, 1, 28),
    date(2026, 3, 18),
    date(2026, 4, 29),
    date(2026, 6, 17),
    date(2026, 7, 29),
    date(2026, 9, 16),
    date(2026, 11, 4),
    date(2026, 12, 16),
]

# DART corp_code — 사용자 보유 한국 종목만 매핑.
# 향후 corpCode.xml 자동 다운로드로 확장 가능.
_DART_CORP: dict[str, str] = {
    "005930.KS": "00126380",  # 삼성전자
    "000660.KS": "00164779",  # SK하이닉스
}

# FRED release_id — 매크로 지표별 공식 발표 시리즈
_FRED_RELEASES: dict[str, int] = {
    "cpi": 10,    # Consumer Price Index
    "ppi": 46,    # Producer Price Index for Final Demand
    "nfp": 50,    # Employment Situation
}


# ──────────────────────────────────────────────
# 포맷터
# ──────────────────────────────────────────────

def _format_event(ev: CalendarEvent) -> str:
    """Signal.reasons에 들어갈 한 줄 문자열."""
    if ev.days_until == 0:
        return f"📅 {ev.description} 오늘"
    if ev.days_until == 1:
        return f"⚠️ {ev.description} D-1 ({ev.event_date.isoformat()})"
    if ev.days_until > 1:
        return f"📅 {ev.description} D-{ev.days_until} ({ev.event_date.isoformat()})"
    # 과거 (DART 등)
    return f"📌 {ev.description} ({-ev.days_until}일 전, {ev.event_date.isoformat()})"


# ──────────────────────────────────────────────
# Fetcher
# ──────────────────────────────────────────────

class CalendarFetcher:
    """
    이벤트 캘린더 통합 fetcher.

    Thread-safe (캐시 dict + Lock).
    한 인스턴스를 스캐너 사이클 전체에서 공유 사용 권장.

    사용:
        fetcher = CalendarFetcher()  # .env에서 키 자동 로드
        contexts = fetcher.get_context_strings("NVDA")
        # → ["⚠️ NVDA 2026Q1 어닝 D-1 (2026-05-08)", "📅 CPI 발표 D-3 (...)"]
    """

    def __init__(
        self,
        finnhub_key: Optional[str] = None,
        fred_key: Optional[str] = None,
        dart_key: Optional[str] = None,
        lookahead_days: int = 7,
    ):
        self.finnhub_key = finnhub_key if finnhub_key is not None else os.getenv("FINNHUB_API_KEY", "")
        self.fred_key    = fred_key    if fred_key    is not None else os.getenv("FRED_API_KEY", "")
        self.dart_key    = dart_key    if dart_key    is not None else os.getenv("DART_API_KEY", "")
        self.lookahead_days = lookahead_days

        # 캐시 — 키 = (source, ticker_or_None, start_date, end_date), 값 = list[CalendarEvent]
        self._cache: dict[tuple, list[CalendarEvent]] = {}
        self._cache_date: Optional[date] = None
        self._lock = threading.Lock()

    # ── public ─────────────────────────────────

    def get_context_strings(self, ticker: str, lookahead_days: Optional[int] = None) -> list[str]:
        """
        Signal.reasons 에 바로 append할 한 줄 문자열 리스트.
        실패는 빈 리스트 — 호출자는 항상 안전하게 사용 가능.

        lookahead_days: 미지정 시 self.lookahead_days. /market 처럼 더 긴 윈도 필요할 때 override.
        """
        try:
            events = self.get_events(ticker, lookahead_days=lookahead_days)
            return [_format_event(ev) for ev in events]
        except Exception as e:
            log.warning(f"[Calendar] get_context_strings({ticker}) 예외: {type(e).__name__}: {e}")
            return []

    def get_events(self, ticker: str, lookahead_days: Optional[int] = None) -> list[CalendarEvent]:
        """원본 CalendarEvent 리스트. M2 백테스트에서 활용 가능."""
        self._ensure_cache_fresh()
        today = date.today()
        days = lookahead_days if lookahead_days is not None else self.lookahead_days
        end = today + timedelta(days=days)

        events: list[CalendarEvent] = []

        # 미국 종목만 어닝스 (빈 ticker = 게이지 호출 → 어닝스 스킵)
        if ticker and self._is_us_ticker(ticker):
            events.extend(self._safe(self._fetch_finnhub_earnings, ticker, today, end))

        # 매크로 (모든 종목 공통)
        events.extend(self._safe(self._fetch_fred_macro, today, end))
        events.extend(self._fetch_fomc(today, end))

        # 한국 보유 종목 (빈 ticker는 _DART_CORP에 없으니 자동 스킵)
        if ticker and ticker in _DART_CORP:
            events.extend(self._safe(self._fetch_dart, ticker, today, end))

        events.sort(key=lambda e: e.days_until)
        return events

    # ── 내부: 캐시/유틸 ────────────────────────

    def _ensure_cache_fresh(self) -> None:
        """날짜 바뀌면 캐시 무효화 (자정 reset)."""
        with self._lock:
            today = date.today()
            if self._cache_date != today:
                self._cache.clear()
                self._cache_date = today

    @staticmethod
    def _safe(fn, *args) -> list[CalendarEvent]:
        """fn 호출 — 어떤 예외든 잡아서 빈 리스트 반환."""
        try:
            return fn(*args)
        except Exception as e:
            log.debug(f"[Calendar] {fn.__name__} 실패: {type(e).__name__}: {e}")
            return []

    @staticmethod
    def _is_us_ticker(ticker: str) -> bool:
        upper = ticker.upper()
        if "/" in upper:                         # 크립토 페어 (ETH/USDT)
            return False
        if upper.endswith("-USD"):               # 야후 크립토 (BTC-USD)
            return False
        if upper.endswith((".KS", ".KQ", ".T", ".HK", ".L", ".PA", ".DE")):
            return False
        return True

    # ── Finnhub: 미국 어닝스 ───────────────────

    def _fetch_finnhub_earnings(self, ticker: str, start: date, end: date) -> list[CalendarEvent]:
        if not self.finnhub_key:
            return []
        cache_key = ("finnhub", ticker, start, end)
        with self._lock:
            if cache_key in self._cache:
                return self._cache[cache_key]

        url = "https://finnhub.io/api/v1/calendar/earnings"
        params = {
            "from":   start.isoformat(),
            "to":     end.isoformat(),
            "symbol": ticker,
            "token":  self.finnhub_key,
        }
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json() or {}

        events: list[CalendarEvent] = []
        for row in data.get("earningsCalendar", []) or []:
            d_str = row.get("date")
            if not d_str:
                continue
            try:
                ev_date = datetime.strptime(d_str, "%Y-%m-%d").date()
            except ValueError:
                continue
            if ev_date < start or ev_date > end:
                continue

            year = row.get("year")
            quarter = row.get("quarter")
            if year and quarter:
                desc = f"{ticker} {year}Q{quarter} 어닝"
            else:
                desc = f"{ticker} 어닝"

            events.append(CalendarEvent(
                ticker=ticker,
                event_type="earnings",
                event_date=ev_date,
                days_until=(ev_date - start).days,
                description=desc,
                source="finnhub",
            ))

        with self._lock:
            self._cache[cache_key] = events
        return events

    # ── FRED: CPI / PPI / NFP ─────────────────

    def _fetch_fred_macro(self, start: date, end: date) -> list[CalendarEvent]:
        if not self.fred_key:
            return []
        cache_key = ("fred", None, start, end)
        with self._lock:
            if cache_key in self._cache:
                return self._cache[cache_key]

        events: list[CalendarEvent] = []
        for event_type, release_id in _FRED_RELEASES.items():
            try:
                url = "https://api.stlouisfed.org/fred/release/dates"
                params = {
                    "release_id": release_id,
                    "api_key": self.fred_key,
                    "file_type": "json",
                    "include_release_dates_with_no_data": "true",
                    "limit": 50,
                    "sort_order": "desc",
                }
                resp = requests.get(url, params=params, timeout=10)
                resp.raise_for_status()
                data = resp.json() or {}

                label = {"cpi": "CPI", "ppi": "PPI", "nfp": "고용지표(NFP)"}.get(event_type, event_type.upper())
                for row in data.get("release_dates", []) or []:
                    d_str = row.get("date")
                    if not d_str:
                        continue
                    try:
                        ev_date = datetime.strptime(d_str, "%Y-%m-%d").date()
                    except ValueError:
                        continue
                    if ev_date < start or ev_date > end:
                        continue
                    events.append(CalendarEvent(
                        ticker=None,
                        event_type=event_type,
                        event_date=ev_date,
                        days_until=(ev_date - start).days,
                        description=f"{label} 발표",
                        source="fred",
                    ))
            except Exception as e:
                log.debug(f"[Calendar] FRED {event_type} 실패: {type(e).__name__}: {e}")
                continue

        with self._lock:
            self._cache[cache_key] = events
        return events

    # ── FOMC 하드코딩 ─────────────────────────

    @staticmethod
    def _fetch_fomc(start: date, end: date) -> list[CalendarEvent]:
        events: list[CalendarEvent] = []
        for ev_date in _FOMC_2026:
            if ev_date < start or ev_date > end:
                continue
            events.append(CalendarEvent(
                ticker=None,
                event_type="fomc",
                event_date=ev_date,
                days_until=(ev_date - start).days,
                description="FOMC 회의 종료",
                source="fed_static",
            ))
        return events

    # ── DART (한국 공시) ──────────────────────

    def _fetch_dart(self, ticker: str, start: date, end: date) -> list[CalendarEvent]:
        """
        DART는 forward-looking 일정이 아닌 historical 공시 검색.
        backward 윈도(7일 전~오늘)로 최근 공시만 가져온다.
        """
        if not self.dart_key:
            return []
        corp_code = _DART_CORP.get(ticker)
        if not corp_code:
            return []

        # backward 윈도 (DART는 미래 일정이 아니라 과거 접수일)
        bgn = start - timedelta(days=self.lookahead_days)
        cache_key = ("dart", ticker, bgn, start)
        with self._lock:
            if cache_key in self._cache:
                return self._cache[cache_key]

        url = "https://opendart.fss.or.kr/api/list.json"
        params = {
            "crtfc_key": self.dart_key,
            "corp_code": corp_code,
            "bgn_de": bgn.strftime("%Y%m%d"),
            "end_de": start.strftime("%Y%m%d"),
            "page_count": 20,
        }
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json() or {}

        # status "000" = 정상, 그 외는 에러 또는 데이터 없음
        if str(data.get("status")) != "000":
            with self._lock:
                self._cache[cache_key] = []
            return []

        events: list[CalendarEvent] = []
        for row in data.get("list", []) or []:
            d_str = row.get("rcept_dt", "")
            if not d_str or len(d_str) != 8:
                continue
            try:
                ev_date = datetime.strptime(d_str, "%Y%m%d").date()
            except ValueError:
                continue
            report_nm = (row.get("report_nm") or "").strip()
            if len(report_nm) > 30:
                report_nm = report_nm[:30] + "…"
            events.append(CalendarEvent(
                ticker=ticker,
                event_type="dart",
                event_date=ev_date,
                days_until=(ev_date - start).days,  # 과거이므로 음수
                description=f"{ticker} 공시: {report_nm}",
                source="dart",
            ))

        with self._lock:
            self._cache[cache_key] = events
        return events
