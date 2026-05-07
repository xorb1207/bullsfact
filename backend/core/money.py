"""
티커 → 통화 추정 + 포맷 유틸.

yfinance suffix 기반:
  .KS / .KQ → KRW (한국)
  .T        → JPY (일본 — 향후 확장)
  .HK       → HKD
  그 외     → USD (미국 주식/ETF 기본)

Binance 페어(슬래시)는 현재 quote가 USDT/USDC 등 USD-pegged 가정 → USD 표기.
"""
from __future__ import annotations


def currency_for(ticker: str) -> str:
    """티커 형식으로 통화 추정. 모를 때는 USD."""
    if not ticker:
        return "USD"
    upper = ticker.upper()
    if upper.endswith(".KS") or upper.endswith(".KQ"):
        return "KRW"
    if upper.endswith(".T"):
        return "JPY"
    if upper.endswith(".HK"):
        return "HKD"
    # 그 외 (미국 ETF/주식, 크립토 USDT pair) → USD 표기
    return "USD"


_SYMBOL = {
    "USD": "$",
    "KRW": "₩",
    "JPY": "¥",
    "HKD": "HK$",
}


def format_money(value: float, ticker: str = "") -> str:
    """티커 기반 가격 포맷. ticker 미지정 시 USD 가정."""
    cur = currency_for(ticker)
    sym = _SYMBOL.get(cur, "")

    if cur == "KRW":
        return f"{sym}{value:,.0f}"      # 원화는 정수, 천단위 콤마
    if cur == "JPY":
        return f"{sym}{value:,.0f}"      # 엔도 정수
    # USD 등 소수점 통화
    if value >= 1000:
        return f"{sym}{value:,.2f}"
    if value >= 1:
        return f"{sym}{value:.2f}"
    return f"{sym}{value:.4f}"


def format_money_signed(value: float, ticker: str = "") -> str:
    """+/- 부호 명시 (P&L 표시용)."""
    cur = currency_for(ticker)
    sym = _SYMBOL.get(cur, "")
    sign = "+" if value >= 0 else "-"
    abs_v = abs(value)

    if cur in ("KRW", "JPY"):
        return f"{sign}{sym}{abs_v:,.0f}"
    if abs_v >= 1000:
        return f"{sign}{sym}{abs_v:,.2f}"
    if abs_v >= 1:
        return f"{sign}{sym}{abs_v:.2f}"
    return f"{sign}{sym}{abs_v:.4f}"
