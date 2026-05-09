"""
시장 현황(Market Snapshot) — 지수/채권/심리/크립토/원자재 일괄 fetch.

LLM 호출 없음. 사실 그대로 가져와서 Telegram digest 포맷으로 변환.

데이터 소스:
- yfinance: 지수, 채권, 크립토, 원자재
- alternative.me: Crypto Fear & Greed
- production.dataviz.cnn.io: CNN Fear & Greed (browser 헤더 필요)
- coingecko: BTC 도미넌스, 전체 시총
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import requests
import yfinance as yf

log = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# 타입
# ──────────────────────────────────────────────

@dataclass
class Quote:
    label: str          # "S&P 500"
    symbol: str         # "^GSPC"
    price: float
    change_pct: float   # 전일종가 대비 %
    error: Optional[str] = None


@dataclass
class FearGreed:
    source: str         # "cnn" | "crypto"
    score: float        # 0~100
    rating: str         # 한국어로 변환된 라벨


@dataclass
class MarketSnapshot:
    fetched_at: datetime
    indices: list[Quote] = field(default_factory=list)
    bonds: list[Quote] = field(default_factory=list)
    yield_curve_2y10y: Optional[float] = None
    crypto: list[Quote] = field(default_factory=list)
    btc_dominance: Optional[float] = None
    crypto_mcap_billion_usd: Optional[float] = None
    commodities: list[Quote] = field(default_factory=list)
    sentiment: list[FearGreed] = field(default_factory=list)
    correlations: list = field(default_factory=list)        # M2-C: list[CorrelationPair]
    errors: list[str] = field(default_factory=list)


# ──────────────────────────────────────────────
# yfinance 묶음 fetch
# ──────────────────────────────────────────────

INDICES = [("S&P 500", "^GSPC"), ("Nasdaq", "^IXIC"), ("Dow", "^DJI"),
           ("Russell 2K", "^RUT"), ("VIX", "^VIX")]
BONDS = [("10Y", "^TNX"), ("2Y", "2YY=F")]
COMMODITIES = [("금", "GC=F"), ("WTI 원유", "CL=F"), ("DXY", "DX-Y.NYB")]
CRYPTO = [("BTC", "BTC-USD"), ("ETH", "ETH-USD")]


def _fetch_quote(label: str, symbol: str) -> Quote:
    try:
        h = yf.Ticker(symbol).history(period="5d", interval="1d", auto_adjust=False)
        if h.empty:
            return Quote(label=label, symbol=symbol, price=0.0, change_pct=0.0, error="empty")
        last = float(h["Close"].iloc[-1])
        prev = float(h["Close"].iloc[-2]) if len(h) > 1 else last
        pct = (last - prev) / prev * 100 if prev else 0.0
        return Quote(label=label, symbol=symbol, price=last, change_pct=pct)
    except Exception as e:
        return Quote(label=label, symbol=symbol, price=0.0, change_pct=0.0, error=type(e).__name__)


def _fetch_quotes_parallel(group: list[tuple[str, str]], workers: int = 5) -> list[Quote]:
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="market-fetch") as ex:
        futs = [ex.submit(_fetch_quote, name, sym) for name, sym in group]
        return [f.result() for f in futs]


# ──────────────────────────────────────────────
# 외부 API 헬퍼
# ──────────────────────────────────────────────

_BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}

_KO_RATING = {
    "extreme fear":   "극심한 공포",
    "fear":           "공포",
    "neutral":        "중립",
    "greed":          "탐욕",
    "extreme greed":  "극심한 탐욕",
}


def _ko_rating(s: str) -> str:
    return _KO_RATING.get(s.lower().strip(), s)


def _fetch_cnn_fg() -> Optional[FearGreed]:
    try:
        r = requests.get(
            "https://production.dataviz.cnn.io/index/fearandgreed/graphdata",
            headers={**_BROWSER_HEADERS, "Referer": "https://www.cnn.com/markets/fear-and-greed"},
            timeout=8,
        )
        if not r.ok:
            log.warning(f"[market] CNN F&G status={r.status_code}")
            return None
        d = r.json().get("fear_and_greed") or {}
        score = d.get("score")
        rating = d.get("rating") or ""
        if score is None:
            return None
        return FearGreed(source="cnn", score=float(score), rating=_ko_rating(rating))
    except Exception as e:
        log.warning(f"[market] CNN F&G 실패: {type(e).__name__}")
        return None


def _fetch_crypto_fg() -> Optional[FearGreed]:
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=1", timeout=8)
        if not r.ok:
            return None
        d = r.json()["data"][0]
        return FearGreed(
            source="crypto",
            score=float(d["value"]),
            rating=_ko_rating(d.get("value_classification", "")),
        )
    except Exception as e:
        log.warning(f"[market] Crypto F&G 실패: {type(e).__name__}")
        return None


def _fetch_coingecko_global() -> tuple[Optional[float], Optional[float]]:
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/global",
            headers={"User-Agent": _BROWSER_HEADERS["User-Agent"]},
            timeout=8,
        )
        if not r.ok:
            return None, None
        d = r.json()["data"]
        dom = float(d["market_cap_percentage"]["btc"])
        mcap_b = float(d["total_market_cap"]["usd"]) / 1e9
        return dom, mcap_b
    except Exception as e:
        log.warning(f"[market] CoinGecko 실패: {type(e).__name__}")
        return None, None


# ──────────────────────────────────────────────
# 메인
# ──────────────────────────────────────────────

def _safe_compute_correlations() -> list:
    """correlations 모듈 실패해도 market snapshot은 살아남도록 격리."""
    try:
        from .correlations import compute_all_pairs
        return compute_all_pairs()
    except Exception as e:
        log.warning(f"[Market] correlations 계산 실패 (무시): {type(e).__name__}: {e}")
        return []


class MarketFetcher:
    """모든 소스를 병렬 fetch. 실패는 부분적으로 허용 — 가능한 만큼만 채워서 반환."""

    def fetch(self) -> MarketSnapshot:
        snap = MarketSnapshot(fetched_at=datetime.now(timezone.utc))

        # 5개 그룹을 병렬로 (각 그룹은 자체적으로 또 병렬)
        with ThreadPoolExecutor(max_workers=5, thread_name_prefix="market-group") as ex:
            f_indices = ex.submit(_fetch_quotes_parallel, INDICES)
            f_bonds = ex.submit(_fetch_quotes_parallel, BONDS)
            f_crypto = ex.submit(_fetch_quotes_parallel, CRYPTO)
            f_commod = ex.submit(_fetch_quotes_parallel, COMMODITIES)
            f_cnn = ex.submit(_fetch_cnn_fg)
            f_cfg = ex.submit(_fetch_crypto_fg)
            f_cg = ex.submit(_fetch_coingecko_global)
            # M2-C: dual-window 상관계수 (별도 모듈, 실패해도 본 fetch는 살림)
            f_corr = ex.submit(_safe_compute_correlations)

            snap.indices = f_indices.result()
            snap.bonds = f_bonds.result()
            snap.crypto = f_crypto.result()
            snap.commodities = f_commod.result()
            cnn = f_cnn.result()
            cfg = f_cfg.result()
            dom, mcap_b = f_cg.result()
            snap.correlations = f_corr.result()

        if cnn:
            snap.sentiment.append(cnn)
        if cfg:
            snap.sentiment.append(cfg)
        snap.btc_dominance = dom
        snap.crypto_mcap_billion_usd = mcap_b

        # 수익률 곡선 (10Y - 2Y)
        ten = next((q for q in snap.bonds if q.label == "10Y" and not q.error), None)
        two = next((q for q in snap.bonds if q.label == "2Y" and not q.error), None)
        if ten and two:
            snap.yield_curve_2y10y = ten.price - two.price

        for q in snap.indices + snap.bonds + snap.crypto + snap.commodities:
            if q.error:
                snap.errors.append(f"{q.label}({q.symbol}): {q.error}")

        return snap


# ──────────────────────────────────────────────
# 포맷 (Telegram HTML)
# ──────────────────────────────────────────────

def _fmt_pct(p: float) -> str:
    sign = "🟢" if p > 0.05 else ("🔴" if p < -0.05 else "⚪")
    return f"{sign} {p:+.2f}%"


def _fmt_price(label: str, price: float) -> str:
    if "VIX" in label or "도미넌스" in label or label.endswith("Y"):
        return f"{price:.2f}"
    if price >= 1000:
        return f"{price:,.0f}"
    if price >= 100:
        return f"{price:,.1f}"
    return f"{price:.2f}"


def _vix_label(p: float) -> str:
    if p < 15:
        return "🟢 낮음"
    if p < 20:
        return "🟡 보통"
    if p < 30:
        return "🟠 높음"
    return "🔴 매우 높음"


def _curve_label(c: float) -> str:
    if c < 0:
        return "⚠️ 역전"
    if c < 0.5:
        return "⚠️ 평탄"
    return "🟢 정상"


def _fg_emoji(score: float) -> str:
    if score < 25:  return "😱"
    if score < 45:  return "😟"
    if score < 55:  return "😐"
    if score < 75:  return "😎"
    return "🤑"


def format_telegram(snap: MarketSnapshot) -> str:
    ts_kst = snap.fetched_at.astimezone().strftime("%Y-%m-%d %H:%M")
    lines = [f"📊 <b>시장 현황</b> ({ts_kst})", "━━━━━━━━━━━━━━━━━━━━"]

    # 지수
    if snap.indices:
        lines.append("📈 <b>지수</b>")
        for q in snap.indices:
            if q.error:
                lines.append(f"  {q.label:10s} ⚠️ {q.error}")
                continue
            extra = f"  {_vix_label(q.price)}" if q.label == "VIX" else ""
            lines.append(f"  {q.label:10s} {_fmt_price(q.label, q.price):>10s}  {_fmt_pct(q.change_pct)}{extra}")

    # 채권
    if snap.bonds:
        lines.append("\n💰 <b>채권</b>")
        for q in snap.bonds:
            if q.error: continue
            lines.append(f"  {q.label:10s} {q.price:>9.2f}%  {_fmt_pct(q.change_pct)}")
        if snap.yield_curve_2y10y is not None:
            c = snap.yield_curve_2y10y
            lines.append(f"  {'10Y-2Y':10s} {c:>+9.2f}%  {_curve_label(c)}")

    # 심리
    if snap.sentiment:
        lines.append("\n😱 <b>심리지수</b>")
        for fg in snap.sentiment:
            src = "CNN" if fg.source == "cnn" else "Crypto"
            lines.append(f"  {src:10s} {fg.score:>5.1f} / 100  {_fg_emoji(fg.score)} {fg.rating}")

    # 크립토
    if snap.crypto:
        lines.append("\n💎 <b>크립토</b>")
        for q in snap.crypto:
            if q.error: continue
            lines.append(f"  {q.label:10s} ${_fmt_price(q.label, q.price):>10s}  {_fmt_pct(q.change_pct)}")
        if snap.btc_dominance is not None:
            lines.append(f"  {'BTC 도미넌스':10s} {snap.btc_dominance:>5.1f}%")
        if snap.crypto_mcap_billion_usd is not None:
            lines.append(f"  {'총 시총':10s} ${snap.crypto_mcap_billion_usd:,.0f}B")

    # 원자재 / 환율
    if snap.commodities:
        lines.append("\n🛢️ <b>원자재 / 환율</b>")
        for q in snap.commodities:
            if q.error: continue
            unit = " /oz" if q.label == "금" else (" /bbl" if "WTI" in q.label else "")
            lines.append(f"  {q.label:10s} ${_fmt_price(q.label, q.price):>10s}{unit}  {_fmt_pct(q.change_pct)}")

    # 상관관계 (M2-C, dual-window)
    if snap.correlations:
        valid = [c for c in snap.correlations if c.corr_short is not None and c.corr_long is not None]
        if valid:
            lines.append("\n🔗 <b>상관관계</b>  <i>(현재 20d / 기준 200d)</i>")
            for c in valid:
                delta_arrow = "↑" if (c.delta or 0) > 0 else ("↓" if (c.delta or 0) < 0 else "·")
                lines.append(
                    f"  {c.label_a}–{c.label_b}: "
                    f"{c.corr_short:+.2f} / {c.corr_long:+.2f}  "
                    f"{delta_arrow} {c.interpretation}"
                )

    if snap.errors:
        lines.append(f"\n⚠️ 일부 데이터 누락 ({len(snap.errors)}건)")

    return "\n".join(lines)
