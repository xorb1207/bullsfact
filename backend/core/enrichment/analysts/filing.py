"""
Filing Analyst — 8-K(SEC EDGAR) + DART 공시 본문 fetch + 주가 직결 키워드 검출.

뉴스 Analyst가 매체발 헤드라인을 모은다면, 이쪽은 발행사 공식 공시 1차 자료.
주가 직결 이벤트(유상증자, 공급계약, 자사주매입, Material Agreement 등)
포착이 목적.

수집 전략:
  - 미국 종목 (NVDA, SOXL ...) → SEC EDGAR submissions API에서 최근 8-K 1~3건
  - 한국 종목 (.KS / .KQ, _DART_CORP 매핑) → OpenDART list + document.xml 본문
  - 그 외 (.T, -USD, ETH/USDT 등) → 빈 결과

LLM 호출:
  - signal.strength == STRONG 일 때만, 키워드 매칭이 있을 때만 한다.
  - llm_cache 활용 (key = filing id) — 재발송/재처리 시 무료.
  - 실패 / LLMClient 미제공 시 키워드 라벨만으로 한 줄 요약.
"""
from __future__ import annotations

import io
import logging
import os
import re
import threading
import time
import zipfile
from dataclasses import dataclass
from typing import Optional

import requests

from ..base import Analyst
from ..llm_client import LLMClient, BudgetExceeded
from ..types import AnalystResult
from ...strategy.dip_buy import Signal, SignalStrength

log = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# 상수
# ──────────────────────────────────────────────

MAX_FILINGS = int(os.getenv("FILING_MAX_PER_TICKER", "3"))
BODY_BYTES_LIMIT = int(os.getenv("FILING_BODY_BYTES", "3072"))  # 첫 3KB
FETCH_TIMEOUT = 10.0
LLM_CACHE_TTL_SEC = 7 * 24 * 60 * 60   # 7일 — 한 공시 요약은 안 바뀜
LLM_MODEL = os.getenv("LLM_MODEL_FILING", "claude-haiku-4-5")

# SEC EDGAR는 식별 가능한 User-Agent 요구 (10 req/s).
_DEFAULT_UA = "dip-alert/1.0 contact@example.com"
SEC_USER_AGENT = os.getenv("SEC_USER_AGENT", _DEFAULT_UA)

# 주가 직결 키워드 — 한국어 + 영문 혼재. 매칭은 case-insensitive.
KEYWORDS_KO: tuple[str, ...] = (
    "유상증자", "무상증자", "전환사채", "신주인수권",
    "공급계약", "단일판매", "주요계약",
    "자기주식", "자사주", "자사주매입", "자사주취득",
    "최대주주", "최대주주변경", "경영권",
    "인수", "합병", "분할",
    "감자", "배당", "주식분할", "액면분할",
    "특허", "라이선스",
    "회생절차", "거래정지",
)
KEYWORDS_EN: tuple[str, ...] = (
    "merger", "acquisition", "material agreement",
    "definitive agreement", "tender offer", "spin-off", "spinoff",
    "buyback", "share repurchase",
    "dividend", "stock split",
    "going concern", "bankruptcy", "chapter 11",
    "guidance", "preliminary results",
    "ceo", "cfo", "departure", "resignation",
    "restructuring", "layoff",
    "fda approval", "clinical",
    "patent", "license",
)

# 금액 패턴 — LLM 미사용 폴백에서 첫 한 줄 만들 때.
_MONEY_RE = re.compile(
    r"(?:(?:US\$|\$|USD|KRW|₩)\s?[\d,]+(?:\.\d+)?\s?(?:억|조|십억|백만|million|billion|bn|mn)?"
    r"|[\d,]+(?:\.\d+)?\s?(?:억원|조원|억\s*달러|백만달러|million|billion|bn|mn))",
    re.IGNORECASE,
)


# ──────────────────────────────────────────────
# 데이터 타입
# ──────────────────────────────────────────────

@dataclass
class FilingDoc:
    source: str           # "sec" | "dart"
    filing_id: str        # accessionNumber (sec) | rcept_no (dart) — 캐시 키
    form: str             # "8-K" | report_nm
    filed_date: str       # "2026-05-10"
    title: str            # form description (sec) or report_nm (dart)
    body_excerpt: str     # 최대 BODY_BYTES_LIMIT, 텍스트만
    url: str              # 공시 원문 페이지 URL
    keywords_hit: list[str]


# ──────────────────────────────────────────────
# SEC EDGAR — ticker→CIK + 최근 8-K
# ──────────────────────────────────────────────

_cik_cache_lock = threading.Lock()
_cik_cache: Optional[dict[str, str]] = None  # ticker(upper) → "0000320193"


def _sec_headers() -> dict[str, str]:
    return {
        "User-Agent": SEC_USER_AGENT,
        "Accept": "application/json",
        "Accept-Encoding": "gzip, deflate",
        "Host": "data.sec.gov",
    }


def _load_ticker_cik_map() -> dict[str, str]:
    """company_tickers.json 한번만 로드. 실패해도 빈 dict 반환."""
    global _cik_cache
    with _cik_cache_lock:
        if _cik_cache is not None:
            return _cik_cache
        try:
            url = "https://www.sec.gov/files/company_tickers.json"
            # company_tickers.json 은 www.sec.gov 도메인
            headers = dict(_sec_headers())
            headers["Host"] = "www.sec.gov"
            resp = requests.get(url, headers=headers, timeout=FETCH_TIMEOUT)
            resp.raise_for_status()
            raw = resp.json()
            mp: dict[str, str] = {}
            # 포맷: {"0": {"cik_str": 320193, "ticker": "AAPL", ...}, ...}
            for row in raw.values():
                tk = str(row.get("ticker", "")).upper().strip()
                cik = row.get("cik_str")
                if tk and isinstance(cik, int):
                    mp[tk] = f"{cik:010d}"
            _cik_cache = mp
            log.info(f"[FilingAnalyst] SEC ticker→CIK 로드: {len(mp)}건")
            return mp
        except Exception as e:
            log.warning(f"[FilingAnalyst] SEC ticker 매핑 로드 실패: {type(e).__name__}: {e}")
            _cik_cache = {}
            return _cik_cache


def _is_us_ticker(ticker: str) -> bool:
    upper = ticker.upper()
    if "/" in upper or upper.endswith("-USD"):
        return False
    if upper.endswith((".KS", ".KQ", ".T", ".HK", ".L", ".PA", ".DE")):
        return False
    return True


def _fetch_sec_8k(ticker: str, max_n: int) -> list[FilingDoc]:
    """SEC EDGAR submissions API → 최근 8-K 본문 일부 fetch."""
    cik = _load_ticker_cik_map().get(ticker.upper())
    if not cik:
        return []

    sub_url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    try:
        resp = requests.get(sub_url, headers=_sec_headers(), timeout=FETCH_TIMEOUT)
        resp.raise_for_status()
        data = resp.json() or {}
    except Exception as e:
        log.debug(f"[FilingAnalyst] SEC submissions {ticker} 실패: {type(e).__name__}: {e}")
        return []

    recent = (data.get("filings") or {}).get("recent") or {}
    forms = recent.get("form") or []
    accessions = recent.get("accessionNumber") or []
    primaries = recent.get("primaryDocument") or []
    filed_dates = recent.get("filingDate") or []
    primary_descs = recent.get("primaryDocDescription") or []
    items_list = recent.get("items") or []

    docs: list[FilingDoc] = []
    for i, form in enumerate(forms):
        if form != "8-K":
            continue
        if i >= len(accessions) or i >= len(primaries):
            break

        acc = str(accessions[i])
        acc_no_dash = acc.replace("-", "")
        primary = str(primaries[i])
        filed = str(filed_dates[i]) if i < len(filed_dates) else ""
        desc = str(primary_descs[i]) if i < len(primary_descs) else "8-K"
        items = str(items_list[i]) if i < len(items_list) else ""

        cik_int = int(cik)
        body_url = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_no_dash}/{primary}"
        index_url = f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type=8-K&dateb=&owner=include&count=10"

        body = _fetch_sec_body(body_url)
        text_for_match = " ".join(filter(None, [desc, items, body]))
        hits = _match_keywords(text_for_match)

        docs.append(FilingDoc(
            source="sec",
            filing_id=acc,
            form="8-K",
            filed_date=filed,
            title=f"8-K{(' · ' + items) if items else ''}{(' · ' + desc) if desc and desc != '8-K' else ''}".strip(" ·"),
            body_excerpt=body,
            url=index_url,
            keywords_hit=hits,
        ))
        if len(docs) >= max_n:
            break
    return docs


def _fetch_sec_body(url: str) -> str:
    """8-K primary document 첫 N바이트만. HTML 태그는 거칠게 stripping."""
    try:
        # primary document는 www.sec.gov 도메인
        headers = dict(_sec_headers())
        headers["Host"] = "www.sec.gov"
        headers["Accept"] = "text/html,application/xhtml+xml"
        # stream으로 첫 N바이트만 읽기 (대용량 8-K 방지)
        with requests.get(url, headers=headers, timeout=FETCH_TIMEOUT, stream=True) as resp:
            resp.raise_for_status()
            chunk = resp.raw.read(BODY_BYTES_LIMIT * 4)  # HTML이 많이 부풀어 있으니 여유 4배
        text = chunk.decode("utf-8", errors="ignore")
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:BODY_BYTES_LIMIT]
    except Exception as e:
        log.debug(f"[FilingAnalyst] SEC body fetch 실패 {url}: {type(e).__name__}: {e}")
        return ""


# ──────────────────────────────────────────────
# DART — list + document.xml 본문
# ──────────────────────────────────────────────

# 계산 안 하고 calendar_fetcher의 _DART_CORP 재사용 (단일 진실)
def _dart_corp_code(ticker: str) -> Optional[str]:
    from ...datasource.calendar_fetcher import _DART_CORP
    return _DART_CORP.get(ticker)


def _fetch_dart_filings(ticker: str, dart_key: str, max_n: int, lookback_days: int = 14) -> list[FilingDoc]:
    corp_code = _dart_corp_code(ticker)
    if not corp_code or not dart_key:
        return []

    from datetime import date, timedelta
    today = date.today()
    bgn = today - timedelta(days=lookback_days)

    list_url = "https://opendart.fss.or.kr/api/list.json"
    params = {
        "crtfc_key": dart_key,
        "corp_code": corp_code,
        "bgn_de": bgn.strftime("%Y%m%d"),
        "end_de": today.strftime("%Y%m%d"),
        "page_count": 20,
    }
    try:
        resp = requests.get(list_url, params=params, timeout=FETCH_TIMEOUT)
        resp.raise_for_status()
        data = resp.json() or {}
    except Exception as e:
        log.debug(f"[FilingAnalyst] DART list {ticker} 실패: {type(e).__name__}: {e}")
        return []

    if str(data.get("status")) != "000":
        return []

    docs: list[FilingDoc] = []
    for row in (data.get("list") or []):
        rcept_no = str(row.get("rcept_no") or "").strip()
        report_nm = (row.get("report_nm") or "").strip()
        rcept_dt = str(row.get("rcept_dt") or "").strip()
        if not rcept_no or not report_nm:
            continue

        filed_iso = (
            f"{rcept_dt[:4]}-{rcept_dt[4:6]}-{rcept_dt[6:8]}"
            if len(rcept_dt) == 8 else rcept_dt
        )
        body = _fetch_dart_body(rcept_no, dart_key)
        text_for_match = " ".join([report_nm, body])
        hits = _match_keywords(text_for_match)

        # rcept_no 만으로 DART 뷰어 URL 생성
        view_url = f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}"

        docs.append(FilingDoc(
            source="dart",
            filing_id=rcept_no,
            form=report_nm,
            filed_date=filed_iso,
            title=report_nm,
            body_excerpt=body,
            url=view_url,
            keywords_hit=hits,
        ))
        if len(docs) >= max_n:
            break
    return docs


def _fetch_dart_body(rcept_no: str, dart_key: str) -> str:
    """
    OpenDART document.xml — ZIP 내부에 XML 본문.
    대용량이면 첫 BODY_BYTES_LIMIT 만 텍스트로 반환.
    """
    try:
        url = "https://opendart.fss.or.kr/api/document.xml"
        params = {"crtfc_key": dart_key, "rcept_no": rcept_no}
        resp = requests.get(url, params=params, timeout=FETCH_TIMEOUT)
        resp.raise_for_status()
        content = resp.content or b""
        if not content:
            return ""
        # ZIP 시그니처 확인 ("PK\x03\x04")
        if not content.startswith(b"PK"):
            # JSON 에러 응답 가능성 (status != 000)
            return ""
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            names = [n for n in zf.namelist() if n.lower().endswith(".xml")]
            if not names:
                return ""
            with zf.open(names[0]) as f:
                raw = f.read(BODY_BYTES_LIMIT * 8)
        text = raw.decode("utf-8", errors="ignore")
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:BODY_BYTES_LIMIT]
    except Exception as e:
        log.debug(f"[FilingAnalyst] DART body {rcept_no} 실패: {type(e).__name__}: {e}")
        return ""


# ──────────────────────────────────────────────
# 키워드 매칭 + 폴백 요약
# ──────────────────────────────────────────────

def _match_keywords(text: str) -> list[str]:
    if not text:
        return []
    lower = text.lower()
    hits: list[str] = []
    for kw in KEYWORDS_KO:
        if kw in text and kw not in hits:
            hits.append(kw)
    for kw in KEYWORDS_EN:
        if kw.lower() in lower and kw not in hits:
            hits.append(kw)
    return hits


def _fallback_summary(doc: FilingDoc) -> str:
    """LLM 미사용 시 키워드 + 금액 한 줄."""
    bits: list[str] = []
    if doc.keywords_hit:
        bits.append(",".join(doc.keywords_hit[:3]))
    # 본문에서 첫 금액 패턴 1개만
    m = _MONEY_RE.search(doc.body_excerpt or doc.title)
    if m:
        bits.append(m.group(0))
    if not bits:
        bits.append(doc.title or doc.form)
    return f"[{doc.filed_date}] {doc.form}: {' · '.join(bits)}"


# ──────────────────────────────────────────────
# LLM 요약 (캐시)
# ──────────────────────────────────────────────

_LLM_SYSTEM = (
    "당신은 한국 개인 투자자에게 공시 1건을 한 줄로 정리하는 도우미입니다.\n"
    "엄격한 규칙:\n"
    "1. 주가 직결 키워드(유상증자/공급계약/자사주매입/M&A 등)와 금액을 추출.\n"
    "2. 한 줄, 60자 이내. 한국어. 마침표 없이.\n"
    "3. 직결 키워드/금액이 본문에 없으면 정확히 다음 텍스트만 출력: 특이 공시 없음\n"
    "4. 추측 금지. 본문에 없는 사실 추가 금지.\n"
    "5. 출력은 요약 한 줄만. 머리말/꼬리말/이모지 금지."
)


def _llm_summarize(
    llm: LLMClient,
    doc: FilingDoc,
    ticker: str,
) -> Optional[str]:
    """캐시 hit → 비용 0. miss → LLM 호출 후 캐시 저장."""
    cache_key = f"{doc.source}|{doc.filing_id}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    user_prompt = (
        f"종목: {ticker}\n"
        f"공시 종류: {doc.form}\n"
        f"제출일: {doc.filed_date}\n"
        f"제목/항목: {doc.title}\n"
        f"본문 발췌(최대 3KB):\n{doc.body_excerpt or '(본문 fetch 실패)'}"
    )
    try:
        text, usage = llm.call(
            model=LLM_MODEL,
            system=_LLM_SYSTEM,
            user=user_prompt,
            max_tokens=120,
            cache_system=True,
            purpose="filing_summary",
            ticker=ticker,
        )
    except BudgetExceeded as e:
        log.info(f"[FilingAnalyst] 예산 초과 — 폴백 요약: {e}")
        return None
    except Exception as e:
        log.warning(f"[FilingAnalyst] LLM 호출 실패: {type(e).__name__}: {e}")
        return None

    summary = (text or "").strip().splitlines()[0].strip() if text else ""
    if not summary:
        return None
    _cache_put(cache_key, summary, usage.cost_usd())
    return summary


def _cache_get(cache_key: str) -> Optional[str]:
    try:
        from backend.db import SessionLocal, crud
        db = SessionLocal()
        try:
            row = crud.get_llm_cache(db, purpose="filing_summary", cache_key=cache_key)
            if not row:
                return None
            data = row.result_text or {}
            return data.get("summary")
        finally:
            db.close()
    except Exception as e:
        log.debug(f"[FilingAnalyst] cache get 실패: {type(e).__name__}: {e}")
        return None


def _cache_put(cache_key: str, summary: str, cost_usd: float) -> None:
    try:
        from backend.db import SessionLocal, crud
        db = SessionLocal()
        try:
            crud.put_llm_cache(
                db,
                purpose="filing_summary",
                cache_key=cache_key,
                result_text={"summary": summary},
                cost_usd=cost_usd,
                ttl_seconds=LLM_CACHE_TTL_SEC,
            )
        finally:
            db.close()
    except Exception as e:
        log.debug(f"[FilingAnalyst] cache put 실패: {type(e).__name__}: {e}")


# ──────────────────────────────────────────────
# Analyst
# ──────────────────────────────────────────────

class FilingAnalyst(Analyst):
    """8-K + DART 공시 본문 분석.

    LLMClient는 옵션 — 미제공 시 키워드/금액 기반 폴백 요약만.
    LLM은 STRONG 시그널 + 키워드 hit + 본문 있음 + 캐시 miss 일 때만.
    """
    name = "filing"

    def __init__(
        self,
        llm: Optional[LLMClient] = None,
        dart_key: Optional[str] = None,
        max_filings: int = MAX_FILINGS,
    ):
        self.llm = llm
        self.dart_key = dart_key if dart_key is not None else os.getenv("DART_API_KEY", "")
        self.max_filings = max_filings

    def analyze(self, signal: Signal, source: str) -> AnalystResult:
        ticker = signal.ticker
        if source == "binance":
            return AnalystResult(name=self.name, summary="")

        t0 = time.monotonic()
        docs: list[FilingDoc] = []
        if _is_us_ticker(ticker):
            docs = _fetch_sec_8k(ticker, self.max_filings)
        elif ticker.upper().endswith((".KS", ".KQ")):
            docs = _fetch_dart_filings(ticker, self.dart_key, self.max_filings)
        else:
            return AnalystResult(name=self.name, summary="")

        elapsed_ms = int((time.monotonic() - t0) * 1000)

        if not docs:
            return AnalystResult(
                name=self.name,
                summary=f"{ticker}: 최근 공시 없음.",
            )

        # 키워드 hit 우선 정렬 (hit가 있는 공시가 앞으로)
        docs.sort(key=lambda d: (len(d.keywords_hit) == 0, d.filed_date), reverse=False)

        # LLM 사용 조건: STRONG + 본문 있음 + 키워드 hit가 1개라도 있는 doc 존재
        use_llm = (
            self.llm is not None
            and signal.strength == SignalStrength.STRONG
            and any(d.body_excerpt and d.keywords_hit for d in docs)
        )

        lines: list[str] = []
        citations: list[str] = []
        llm_calls = 0
        for d in docs:
            if use_llm and d.body_excerpt and d.keywords_hit:
                summary = _llm_summarize(self.llm, d, ticker) or _fallback_summary(d)
                llm_calls += 1
            else:
                summary = _fallback_summary(d)
            lines.append(f"- {summary}")
            if d.url:
                citations.append(d.url)

        # 키워드 hit이 하나도 없으면 헤더에 명시 (Synthesizer가 무시 가능)
        any_hit = any(d.keywords_hit for d in docs)
        header = (
            f"{ticker} 최근 공시 {len(docs)}건"
            + (f", 직결 키워드 hit {sum(1 for d in docs if d.keywords_hit)}건" if any_hit else " — 특이 공시 없음")
            + ":"
        )
        summary_text = header + "\n" + "\n".join(lines)

        log.info(
            f"[FilingAnalyst] {ticker} → {len(docs)}건 "
            f"(hit={sum(1 for d in docs if d.keywords_hit)}, llm={llm_calls}, {elapsed_ms}ms)"
        )

        return AnalystResult(
            name=self.name,
            summary=summary_text,
            citations=citations,
        )
