"""
한국 종목 한글 회사명 lookup — DART corpCode.xml.

005930.KS → "삼성전자", 035720.KS → "카카오" 같은 한글명 제공.
DART corpCode.xml 한 번 다운로드 → 디스크 캐시 (1주 갱신) + 인메모리 캐시.

DART_API_KEY 없으면 None 반환 (graceful).
"""
from __future__ import annotations

import io
import logging
import os
import threading
import time
import zipfile
from pathlib import Path
from typing import Optional
from xml.etree import ElementTree as ET

import requests

log = logging.getLogger(__name__)


# 캐시 위치 + 갱신 주기
_CACHE_DIR = Path(__file__).resolve().parent.parent.parent / "data"
_CACHE_FILE = _CACHE_DIR / "corpCode.xml"
_CACHE_TTL_SEC = 7 * 24 * 3600          # 1주

# 인메모리 캐시 — 한 번 파싱 후 재사용
_memory_map: Optional[dict[str, str]] = None
_lock = threading.Lock()


def _is_cache_fresh() -> bool:
    if not _CACHE_FILE.exists():
        return False
    age = time.time() - _CACHE_FILE.stat().st_mtime
    return age < _CACHE_TTL_SEC


def _download_corp_xml(api_key: str) -> Optional[bytes]:
    """DART에서 ZIP 받아 풀어서 XML bytes 반환."""
    try:
        url = "https://opendart.fss.or.kr/api/corpCode.xml"
        resp = requests.get(url, params={"crtfc_key": api_key}, timeout=30)
        resp.raise_for_status()
        # ZIP 인지 매직 바이트로 확인 (DART API 실패 시 JSON 에러 반환할 수 있음)
        if not resp.content.startswith(b"PK"):
            log.warning(f"[KR Names] DART 응답이 ZIP 아님 (size={len(resp.content)}). 키 확인 필요")
            return None
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            for name in zf.namelist():
                if name.lower().endswith(".xml"):
                    return zf.read(name)
        log.warning("[KR Names] ZIP 안에 XML 없음")
        return None
    except Exception as e:
        log.warning(f"[KR Names] 다운로드 실패: {type(e).__name__}: {e}")
        return None


def _ensure_xml_cached(api_key: str) -> Optional[Path]:
    """디스크 캐시 갱신 또는 재사용. 결과 path 반환 (없으면 None)."""
    if _is_cache_fresh():
        return _CACHE_FILE
    if not api_key:
        return _CACHE_FILE if _CACHE_FILE.exists() else None
    xml_bytes = _download_corp_xml(api_key)
    if xml_bytes is None:
        return _CACHE_FILE if _CACHE_FILE.exists() else None
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _CACHE_FILE.write_bytes(xml_bytes)
    log.info(f"[KR Names] corpCode.xml 다운로드 완료 ({len(xml_bytes):,} bytes)")
    return _CACHE_FILE


def _parse_xml(path: Path) -> dict[str, str]:
    """corpCode.xml → {stock_code(6자리): corp_name 한글}"""
    out: dict[str, str] = {}
    try:
        tree = ET.parse(str(path))
        root = tree.getroot()
        # 구조: <result><list><corp_code/><corp_name/><stock_code/>...</list>...</result>
        for item in root.findall("list"):
            stock_code = (item.findtext("stock_code") or "").strip()
            corp_name = (item.findtext("corp_name") or "").strip()
            if stock_code and corp_name and len(stock_code) == 6:
                out[stock_code] = corp_name
    except Exception as e:
        log.warning(f"[KR Names] XML 파싱 실패: {type(e).__name__}: {e}")
    return out


def _load_map() -> dict[str, str]:
    """인메모리 + 디스크 캐시 활용."""
    global _memory_map
    with _lock:
        if _memory_map is not None:
            return _memory_map
        api_key = os.getenv("DART_API_KEY", "")
        path = _ensure_xml_cached(api_key)
        if path is None or not path.exists():
            _memory_map = {}
        else:
            _memory_map = _parse_xml(path)
            log.info(f"[KR Names] {len(_memory_map):,}개 매핑 로드")
        return _memory_map


def resolve_korean_name(ticker: str) -> Optional[str]:
    """
    한국 종목 ticker → 한글 회사명. 비-한국 종목은 None.

    예:
        resolve_korean_name("005930.KS") → "삼성전자"
        resolve_korean_name("000660.KQ") → "SK하이닉스"
        resolve_korean_name("NVDA")        → None
    """
    if not ticker:
        return None
    upper = ticker.upper()
    if not (upper.endswith(".KS") or upper.endswith(".KQ")):
        return None
    stock_code = upper.split(".")[0]
    if len(stock_code) != 6 or not stock_code.isdigit():
        return None
    return _load_map().get(stock_code)


def reset_cache() -> None:
    """테스트/디버그용 — 인메모리 캐시 비우기."""
    global _memory_map
    with _lock:
        _memory_map = None
