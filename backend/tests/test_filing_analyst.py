"""
FilingAnalyst mock 테스트 — SEC EDGAR / DART / 키워드 / LLM 게이팅.

requests / LLM 호출 / DB 캐시는 전부 mock. 네트워크/외부 API 없이 단독 실행.

실행:
    python -m unittest backend.tests.test_filing_analyst -v
"""
from __future__ import annotations

import io
import unittest
import zipfile
from unittest.mock import MagicMock, patch

from backend.core.enrichment.analysts import filing as filing_mod
from backend.core.enrichment.analysts.filing import (
    FilingAnalyst,
    FilingDoc,
    _fallback_summary,
    _is_us_ticker,
    _match_keywords,
)
from backend.core.enrichment.types import AnalystResult
from backend.core.strategy.dip_buy import Signal, SignalStrength


def _signal(ticker: str, strength: SignalStrength = SignalStrength.STRONG) -> Signal:
    return Signal(
        ticker=ticker,
        strength=strength,
        price=100.0,
        reasons=["RSI<35"],
        indicators={"rsi": 30.0, "bb_lower": 95.0, "bb_mid": 100.0},
    )


def _fake_response(status_code: int = 200, *, json_data=None, content: bytes | None = None):
    """requests.get 가짜 응답."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.raise_for_status = MagicMock()
    if status_code >= 400:
        resp.raise_for_status.side_effect = Exception(f"HTTP {status_code}")
    if json_data is not None:
        resp.json.return_value = json_data
    if content is not None:
        resp.content = content
        resp.raw = io.BytesIO(content)
    return resp


def _streaming_response(content: bytes):
    """SEC body stream context manager."""
    cm = MagicMock()
    inner = MagicMock()
    inner.raise_for_status = MagicMock()
    inner.raw = io.BytesIO(content)
    cm.__enter__ = MagicMock(return_value=inner)
    cm.__exit__ = MagicMock(return_value=False)
    return cm


class TestKeywordMatching(unittest.TestCase):

    def test_korean_keyword_hit(self):
        hits = _match_keywords("당사는 유상증자 결정. 발행금액 5,000억원")
        self.assertIn("유상증자", hits)

    def test_english_keyword_case_insensitive(self):
        hits = _match_keywords("Company entered into a Material Agreement with X.")
        self.assertIn("material agreement", hits)

    def test_no_hit_returns_empty(self):
        self.assertEqual(_match_keywords("Just a quarterly investor relations update."), [])

    def test_multiple_keywords_deduped(self):
        text = "자사주매입 결정. 자사주 100억원 자사주매입"
        hits = _match_keywords(text)
        self.assertEqual(hits.count("자사주매입"), 1)


class TestTickerClassification(unittest.TestCase):

    def test_us_tickers(self):
        for t in ["NVDA", "SOXL", "TQQQ"]:
            self.assertTrue(_is_us_ticker(t), t)

    def test_kr_tickers_excluded(self):
        for t in ["005930.KS", "000660.KQ"]:
            self.assertFalse(_is_us_ticker(t), t)

    def test_crypto_excluded(self):
        for t in ["ETH/USDT", "BTC-USD"]:
            self.assertFalse(_is_us_ticker(t), t)


class TestFallbackSummary(unittest.TestCase):

    def test_keyword_and_money_extracted(self):
        doc = FilingDoc(
            source="dart",
            filing_id="20260508001",
            form="주요사항보고서",
            filed_date="2026-05-08",
            title="유상증자결정",
            body_excerpt="유상증자 결정. 발행금액 5,000억원.",
            url="https://dart.fss.or.kr/...",
            keywords_hit=["유상증자"],
        )
        s = _fallback_summary(doc)
        self.assertIn("유상증자", s)
        self.assertIn("5,000", s)
        self.assertIn("2026-05-08", s)

    def test_no_keyword_falls_back_to_title(self):
        doc = FilingDoc(
            source="sec", filing_id="x", form="8-K", filed_date="2026-05-08",
            title="Routine quarterly update", body_excerpt="", url="", keywords_hit=[],
        )
        s = _fallback_summary(doc)
        self.assertIn("Routine quarterly update", s)


class TestSECPipeline(unittest.TestCase):

    def setUp(self):
        # 모듈 캐시 초기화
        filing_mod._cik_cache = None

    def _patch_ticker_map(self, ticker_to_cik: dict[str, str]):
        # _load_ticker_cik_map 가 캐시를 채우도록 만든다
        filing_mod._cik_cache = ticker_to_cik

    def test_us_ticker_with_8k_buyback(self):
        self._patch_ticker_map({"NVDA": "0000123456"})

        submissions_json = {
            "filings": {
                "recent": {
                    "form": ["10-Q", "8-K", "8-K"],
                    "accessionNumber": ["0001-1", "0002-2", "0003-3"],
                    "primaryDocument": ["10q.htm", "8k_1.htm", "8k_2.htm"],
                    "filingDate": ["2026-05-01", "2026-05-05", "2026-05-07"],
                    "primaryDocDescription": ["10-Q", "8-K", "8-K"],
                    "items": ["", "2.02 Results", "5.02 Material Agreement"],
                }
            }
        }
        body_html = b"<html>NVDA announced a $50 billion share repurchase program.</html>"

        def fake_get(url, **kwargs):
            if "submissions/CIK" in url:
                return _fake_response(json_data=submissions_json)
            if "Archives/edgar/data" in url:
                return _streaming_response(body_html)
            raise AssertionError(f"unexpected URL: {url}")

        signal = _signal("NVDA")
        llm = MagicMock()
        # LLM 캐시는 비활성/실패로 — 폴백 경로 검증
        with patch.object(filing_mod.requests, "get", side_effect=fake_get), \
             patch.object(filing_mod, "_cache_get", return_value=None), \
             patch.object(filing_mod, "_cache_put"):
            llm.call.return_value = ("자사주매입 50억달러 — 발표", MagicMock(cost_usd=lambda: 0.001))
            analyst = FilingAnalyst(llm=llm)
            result = analyst.analyze(signal, source="yfinance")

        self.assertIsInstance(result, AnalystResult)
        self.assertEqual(result.name, "filing")
        # 8-K 만 골라낸다 (10-Q 제외) → 2건
        # 본문에 keyword hit이 있어야 LLM 호출됨
        self.assertIn("NVDA", result.summary)
        # LLM이 STRONG + hit + body 가 있을 때 호출됨
        # 본문에 "share repurchase"가 있는 doc 만 hit → 적어도 1회 호출
        self.assertGreaterEqual(llm.call.call_count, 1)
        # LLM purpose 확인
        kwargs = llm.call.call_args.kwargs
        self.assertEqual(kwargs["purpose"], "filing_summary")
        self.assertEqual(kwargs["ticker"], "NVDA")

    def test_weak_signal_skips_llm(self):
        self._patch_ticker_map({"NVDA": "0000123456"})
        submissions_json = {
            "filings": {
                "recent": {
                    "form": ["8-K"],
                    "accessionNumber": ["0001-1"],
                    "primaryDocument": ["8k.htm"],
                    "filingDate": ["2026-05-05"],
                    "primaryDocDescription": ["8-K"],
                    "items": ["5.02 Material Agreement"],
                }
            }
        }
        body_html = b"<html>Definitive Agreement signed for $1 billion buyback.</html>"

        def fake_get(url, **kwargs):
            if "submissions/CIK" in url:
                return _fake_response(json_data=submissions_json)
            return _streaming_response(body_html)

        signal = _signal("NVDA", strength=SignalStrength.WEAK)
        llm = MagicMock()
        with patch.object(filing_mod.requests, "get", side_effect=fake_get), \
             patch.object(filing_mod, "_cache_get", return_value=None), \
             patch.object(filing_mod, "_cache_put"):
            FilingAnalyst(llm=llm).analyze(signal, source="yfinance")

        # WEAK이면 LLM 호출 안 함 (비용 가드)
        llm.call.assert_not_called()

    def test_cache_hit_skips_llm(self):
        self._patch_ticker_map({"NVDA": "0000123456"})
        submissions_json = {
            "filings": {
                "recent": {
                    "form": ["8-K"],
                    "accessionNumber": ["0001-1"],
                    "primaryDocument": ["8k.htm"],
                    "filingDate": ["2026-05-05"],
                    "primaryDocDescription": ["8-K"],
                    "items": ["8.01 buyback"],
                }
            }
        }
        body_html = b"<html>$5 billion share repurchase program.</html>"

        def fake_get(url, **kwargs):
            if "submissions/CIK" in url:
                return _fake_response(json_data=submissions_json)
            return _streaming_response(body_html)

        signal = _signal("NVDA")
        llm = MagicMock()
        with patch.object(filing_mod.requests, "get", side_effect=fake_get), \
             patch.object(filing_mod, "_cache_get", return_value="자사주매입 50억달러 (캐시)"):
            result = FilingAnalyst(llm=llm).analyze(signal, source="yfinance")

        llm.call.assert_not_called()
        self.assertIn("자사주매입 50억달러 (캐시)", result.summary)

    def test_crypto_returns_empty(self):
        signal = _signal("ETH/USDT")
        result = FilingAnalyst(llm=None).analyze(signal, source="binance")
        self.assertEqual(result.summary, "")

    def test_no_filings_returns_no_message(self):
        self._patch_ticker_map({"NVDA": "0000123456"})
        submissions_json = {"filings": {"recent": {"form": ["10-Q"], "accessionNumber": ["x"]}}}

        with patch.object(filing_mod.requests, "get",
                          return_value=_fake_response(json_data=submissions_json)):
            result = FilingAnalyst(llm=None).analyze(_signal("NVDA"), source="yfinance")
        self.assertIn("최근 공시 없음", result.summary)


class TestDARTPipeline(unittest.TestCase):

    def test_dart_filing_with_zip_body(self):
        # ZIP 파일을 메모리에서 생성 — document.xml 가짜
        buf = io.BytesIO()
        xml_payload = (
            "<root>"
            "<주요사항>유상증자 결정</주요사항>"
            "<금액>5,000억원</금액>"
            "</root>"
        ).encode("utf-8")
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("12345.xml", xml_payload)
        zip_bytes = buf.getvalue()

        list_json = {
            "status": "000",
            "list": [{
                "rcept_no": "20260508000001",
                "report_nm": "주요사항보고서(유상증자결정)",
                "rcept_dt": "20260508",
            }],
        }

        def fake_get(url, **kwargs):
            if "list.json" in url:
                return _fake_response(json_data=list_json)
            if "document.xml" in url:
                return _fake_response(content=zip_bytes)
            raise AssertionError(f"unexpected URL: {url}")

        # _DART_CORP 매핑 강제
        with patch.object(filing_mod.requests, "get", side_effect=fake_get), \
             patch.object(filing_mod, "_dart_corp_code", return_value="00126380"), \
             patch.object(filing_mod, "_cache_get", return_value=None), \
             patch.object(filing_mod, "_cache_put"):
            llm = MagicMock()
            llm.call.return_value = ("유상증자 5,000억원 결정", MagicMock(cost_usd=lambda: 0.001))
            analyst = FilingAnalyst(llm=llm, dart_key="FAKE_KEY")
            result = analyst.analyze(_signal("005930.KS"), source="yfinance")

        self.assertIn("005930.KS", result.summary)
        self.assertIn("유상증자", result.summary)
        # 캐시 miss 였으므로 LLM 호출됨
        llm.call.assert_called_once()
        self.assertEqual(llm.call.call_args.kwargs["purpose"], "filing_summary")

    def test_dart_no_corp_code_returns_empty(self):
        with patch.object(filing_mod, "_dart_corp_code", return_value=None):
            result = FilingAnalyst(llm=None, dart_key="").analyze(
                _signal("123456.KS"), source="yfinance"
            )
        self.assertIn("최근 공시 없음", result.summary)

    def test_dart_status_error_returns_no_message(self):
        with patch.object(filing_mod, "_dart_corp_code", return_value="00126380"), \
             patch.object(filing_mod.requests, "get",
                          return_value=_fake_response(json_data={"status": "013"})):
            result = FilingAnalyst(llm=None, dart_key="FAKE").analyze(
                _signal("005930.KS"), source="yfinance"
            )
        self.assertIn("최근 공시 없음", result.summary)


class TestBudgetGuard(unittest.TestCase):

    def test_budget_exceeded_falls_back(self):
        filing_mod._cik_cache = {"NVDA": "0000123456"}
        submissions_json = {
            "filings": {
                "recent": {
                    "form": ["8-K"],
                    "accessionNumber": ["0001-1"],
                    "primaryDocument": ["8k.htm"],
                    "filingDate": ["2026-05-05"],
                    "primaryDocDescription": ["8-K"],
                    "items": ["5.02 Material Agreement, $2B buyback"],
                }
            }
        }
        body_html = b"<html>Material Agreement $2 billion share repurchase.</html>"

        def fake_get(url, **kwargs):
            if "submissions/CIK" in url:
                return _fake_response(json_data=submissions_json)
            return _streaming_response(body_html)

        from backend.core.enrichment.llm_client import BudgetExceeded
        llm = MagicMock()
        llm.call.side_effect = BudgetExceeded("over cap")

        with patch.object(filing_mod.requests, "get", side_effect=fake_get), \
             patch.object(filing_mod, "_cache_get", return_value=None), \
             patch.object(filing_mod, "_cache_put"):
            result = FilingAnalyst(llm=llm).analyze(_signal("NVDA"), source="yfinance")

        # 폴백 요약이 들어가야 함 (LLM 실패해도 결과 반환)
        self.assertIn("NVDA", result.summary)
        # 본문에 buyback 키워드 잡혀서 폴백에 들어감
        self.assertTrue(
            any(kw in result.summary.lower() for kw in ["share repurchase", "material agreement"])
        )


if __name__ == "__main__":
    unittest.main()
