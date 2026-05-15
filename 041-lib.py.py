"""
국립중앙도서관 Open API → MARC 041 추출 Streamlit 앱
"""

from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
from typing import Any
from urllib.parse import parse_qs, urlparse

import requests
import streamlit as st
from bs4 import BeautifulSoup

API_URL = "https://www.nl.go.kr/NL/search/openApi/search.do"
MARC_VIEW_URL = "https://www.nl.go.kr/NL/search/marc_view.do"
DETAIL_PAGE_URL = "https://www.nl.go.kr/NL/contents/search.do"
KOLIS_BASE = "https://www.nl.go.kr/kolisnet"
KOLIS_SEARCH_LIST_URL = f"{KOLIS_BASE}/search/searchResultList.do"
KOLIS_EDITION_LIST_URL = f"{KOLIS_BASE}/search/searchResultEditonList.do"
KOLIS_MARC_URL = f"{KOLIS_BASE}/search/searchResultMarc.do"
API_KEY = os.environ.get(
    "NLK_OPEN_API_KEY",
    "c7414336392bd8ec166d31ba7a82d206e3ffbc663e8bab511ab69fae9ce77163",
)
REQUEST_TIMEOUT = 25
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
}

KORMARC_SUBFIELD_RE = re.compile(r"[\$▼]([a-hj-z0-9])([^$▼]*)", re.IGNORECASE)

TRANSLATION_RE = re.compile(r"옮김|번역|옮긴|편역|공역|역주|역사")
TRANSLATOR_ROLE_RE = re.compile(
    r"(?:^|[;\s])(?:[^;]{0,40}?\s역)(?:\s*[,;]|$)|;\s*[^;]*?(?:옮김|번역|역\b)"
)

LANGUAGE_NOTE_MAP: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"영어\s*원작|영문\s*원작|영어\s*번역|영어"), "eng"),
    (re.compile(r"일본어\s*원작|일문\s*원작|일본어\s*번역|일본어|일어"), "jpn"),
    (re.compile(r"중국어\s*원작|중문\s*원작|중국어\s*번역|중국어|한문"), "chi"),
    (re.compile(r"프랑스어\s*원작|프랑스어"), "fre"),
    (re.compile(r"독일어\s*원작|독일어"), "ger"),
    (re.compile(r"러시아어\s*원작|러시아어"), "rus"),
    (re.compile(r"스페인어\s*원작|스페인어"), "spa"),
    (re.compile(r"포르투갈어\s*원작|포르투갈어"), "por"),
    (re.compile(r"이탈리아어\s*원작|이탈리아어"), "ita"),
    (re.compile(r"베트남어\s*원작|베트남어"), "vie"),
]

# 서명·저자에 흔한 외국 인명 → 원어 추정 (보조)
FOREIGN_NAME_HINTS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bJ\.?\s*K\.?\s*Rowling\b", re.I), "eng"),
    (re.compile(r"롤링|Rowling", re.I), "eng"),
    (re.compile(r"생텍쥐페리|Saint-Exupéry|Saint[\s-]?Exupery", re.I), "fre"),
    (re.compile(r"\bde\s+[A-ZÀ-ÿ]", re.I), "fre"),
    (re.compile(r"\bvan\s+[A-Z]", re.I), "dut"),
]


class NlkApiError(Exception):
    """NLK Open API 조회·파싱 오류."""


def normalize_isbn(raw: str) -> str:
    return re.sub(r"[\s\-]", "", (raw or "").strip()).upper()


def _elem_text(element: ET.Element | None) -> str:
    if element is None:
        return ""
    text = "".join(element.itertext())
    return re.sub(r"\s+", " ", text).strip()


def call_open_api(**params: Any) -> ET.Element:
    query: dict[str, Any] = {
        "key": API_KEY,
        "pageNum": 1,
        "pageSize": 10,
        **params,
    }
    try:
        response = requests.get(
            API_URL,
            params=query,
            headers=DEFAULT_HEADERS,
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise NlkApiError(f"Open API 통신 오류: {exc}") from exc

    if response.status_code >= 400:
        raise NlkApiError(f"Open API 조회 실패 (HTTP {response.status_code})")

    body = (response.text or "").strip()
    if not body:
        raise NlkApiError("Open API가 빈 응답을 반환했습니다.")

    try:
        return ET.fromstring(response.content)
    except ET.ParseError as exc:
        raise NlkApiError("Open API XML 파싱에 실패했습니다.") from exc


def _parse_total(root: ET.Element) -> int:
    total_text = root.findtext(".//paramData/total")
    if total_text is None:
        total_text = root.findtext(".//total")
    try:
        return int(total_text or "0")
    except ValueError:
        return 0


def parse_items(root: ET.Element) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for item_el in root.findall(".//result/item"):
        record = {
            "title": _elem_text(item_el.find("title_info")),
            "author": _elem_text(item_el.find("author_info")),
            "publisher": _elem_text(item_el.find("pub_info")),
            "control_no": _elem_text(item_el.find("control_no")),
            "view_key": _elem_text(item_el.find("id")),
            "detail_link": _elem_text(item_el.find("detail_link")),
            "isbn": _elem_text(item_el.find("isbn")),
            "class_no": _elem_text(item_el.find("class_no")),
            "kdc_code": _elem_text(item_el.find("kdc_code_1s")),
            "kdc_name": _elem_text(item_el.find("kdc_name_1s")),
            "pub_year": _elem_text(item_el.find("pub_year_info")),
            "type_name": _elem_text(item_el.find("type_name")),
        }
        items.append(record)
    return items


def extract_view_type(detail_link: str) -> str:
    """detail_link 해시/쿼리에서 viewType 추출 (기본 AH1)."""
    link = (detail_link or "").strip()
    if not link:
        return "AH1"
    if "#" in link:
        fragment = link.split("#", 1)[1]
        if fragment.startswith("?"):
            fragment = fragment[1:]
        params = parse_qs(fragment, keep_blank_values=True)
        if view_types := params.get("viewType"):
            return view_types[0] or "AH1"
    parsed = urlparse(link if "://" in link else f"https://placeholder?{link}")
    if view_types := parse_qs(parsed.query).get("viewType"):
        return view_types[0] or "AH1"
    match = re.search(r"viewType=([^&\s]+)", link, re.IGNORECASE)
    return match.group(1) if match else "AH1"


def _normalize_marc_indicator(ind: str) -> str:
    """KORMARC IND(예: '1 ') → MARC 21 지시자(예: '1_')."""
    cleaned = (ind or "").strip()
    if not cleaned:
        return "_"
    if len(cleaned) == 1:
        return f"{cleaned}_"
    return f"{cleaned[0]}_{cleaned[1]}" if len(cleaned) >= 2 else f"{cleaned}_"


def _clean_marc_field_text(text: str) -> str:
    """KORMARC/KOLIS 구분자(▲) 및 제어문자 제거."""
    return re.sub(r"[\x00▲]", "", text or "").strip()


def _parse_kormarc_subfields(content: str) -> dict[str, str]:
    """▼a/$a 형태 KORMARC 서브필드 → {'a': 'kor', 'h': 'fre'}."""
    fields: dict[str, str] = {}
    cleaned = _clean_marc_field_text(content)
    for match in KORMARC_SUBFIELD_RE.finditer(cleaned):
        code = match.group(1).lower()
        value = re.sub(r"\s+", " ", match.group(2)).strip()
        if value and code not in fields:
            fields[code] = value
    return fields


def _marc_041_from_row(
    ind: str,
    content: str,
    *,
    data_source: str = "web_scrape",
) -> dict[str, Any]:
    """041 행 IND·내용 → MARC 21 문자열 및 구성요소."""
    indicator = _normalize_marc_indicator(_clean_marc_field_text(ind))
    subfields = _parse_kormarc_subfields(content)
    parts = [f"041 {indicator}"]
    for code in sorted(subfields.keys()):
        parts.append(f"${code} {subfields[code]}")
    body_lang = subfields.get("a", "kor")
    original_lang = subfields.get("h")
    ind_first = indicator[0] if indicator else "0"
    return {
        "marc_041": " ".join(parts),
        "indicator": ind_first,
        "body_language": body_lang,
        "original_language": original_lang,
        "is_translation": ind_first == "1",
        "data_source": data_source,
    }


def _extract_041_from_soup(soup: BeautifulSoup) -> dict[str, Any] | None:
    """MARC 보기 테이블에서 TAG=041 행 추출."""
    for table in soup.find_all("table"):
        header_cells = table.find_all("th")
        headers = [th.get_text(strip=True) for th in header_cells]
        if "TAG" not in headers or "내용" not in headers:
            continue
        for row in table.find_all("tr"):
            cells = row.find_all("td")
            if len(cells) < 3:
                continue
            tag = cells[0].get_text(strip=True)
            if tag != "041":
                continue
            ind = _clean_marc_field_text(cells[1].get_text())
            content = _clean_marc_field_text(cells[2].get_text())
            if content:
                return _marc_041_from_row(ind, content)
    return None


UF_KEY_RE = re.compile(r"fnEdtionList\s*\(\s*['\"](\d+)['\"]", re.IGNORECASE)
BIB_KEY_RE = re.compile(
    r"fnDetail\s*\(\s*['\"]([^'\"]+)['\"]\s*,\s*['\"](\d+)['\"]",
    re.IGNORECASE,
)
ISBN_IN_MARC_RE = re.compile(r"[\dX]{10,13}")


def _kolis_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(DEFAULT_HEADERS)
    return session


def _kolis_search_params(isbn: str) -> dict[str, str]:
    return {
        "tab": "ALL",
        "keywordType1": "total",
        "keyword1": isbn,
        "pageNum": "1",
        "pageSize": "15",
    }


def _kolis_collect_uf_keys(session: requests.Session, isbn: str) -> list[str]:
    try:
        response = session.get(
            KOLIS_SEARCH_LIST_URL,
            params=_kolis_search_params(isbn),
            timeout=REQUEST_TIMEOUT,
        )
        if response.status_code >= 400:
            return []
        keys = UF_KEY_RE.findall(response.text or "")
        seen: set[str] = set()
        ordered: list[str] = []
        for key in keys:
            if key not in seen:
                seen.add(key)
                ordered.append(key)
        return ordered
    except requests.RequestException:
        return []


def _kolis_collect_bib_records(
    session: requests.Session,
    isbn: str,
    uf_key: str,
) -> list[tuple[str, str]]:
    payload = {**_kolis_search_params(isbn), "ufKey": uf_key, "tab": "BKGM"}
    try:
        response = session.post(
            KOLIS_EDITION_LIST_URL,
            data=payload,
            timeout=REQUEST_TIMEOUT,
        )
        if response.status_code >= 400:
            return []
        records: list[tuple[str, str]] = []
        seen: set[str] = set()
        for publish_code, bib_key in BIB_KEY_RE.findall(response.text or ""):
            if bib_key not in seen:
                seen.add(bib_key)
                records.append((publish_code, bib_key))
        return records
    except requests.RequestException:
        return []


def _marc_page_matches_isbn(soup: BeautifulSoup, isbn: str) -> bool:
    if not isbn:
        return True
    for row in soup.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) < 3 or cells[0].get_text(strip=True) != "020":
            continue
        content = _clean_marc_field_text(cells[2].get_text())
        tokens = ISBN_IN_MARC_RE.findall(content.upper())
        if isbn in tokens or any(isbn.endswith(t[-10:]) for t in tokens):
            return True
    return False


def _kolis_fetch_marc_soup(
    session: requests.Session,
    isbn: str,
    bib_key: str,
    publish_form_code: str,
) -> BeautifulSoup | None:
    payload = {
        **_kolis_search_params(isbn),
        "tab": "BKGM",
        "bibKey": bib_key,
        "publishFormCode": publish_form_code,
    }
    headers = {"Referer": f"{KOLIS_BASE}/search/searchResultDetail.do"}
    try:
        response = session.post(
            KOLIS_MARC_URL,
            data=payload,
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        )
        if response.status_code >= 400:
            return None
        return BeautifulSoup(response.content, "html.parser")
    except (requests.RequestException, ValueError, TypeError):
        return None


def scrape_kolisnet_marc_041(isbn: str) -> dict[str, Any] | None:
    """
    KOLIS-NET MARC 보기 페이지에서 041 필드를 크롤링.
    ISBN 검색 → 판목(ufKey) → 서지(bibKey) → MARC 상세 순으로 조회.
    실패 시 None 반환.
    """
    normalized = normalize_isbn(isbn)
    if not normalized:
        return None

    session = _kolis_session()
    uf_keys = _kolis_collect_uf_keys(session, normalized)
    if not uf_keys:
        return None

    fallback: dict[str, Any] | None = None
    for uf_key in uf_keys[:5]:
        for publish_code, bib_key in _kolis_collect_bib_records(
            session, normalized, uf_key
        ):
            soup = _kolis_fetch_marc_soup(
                session, normalized, bib_key, publish_code
            )
            if soup is None:
                continue
            marc = _extract_041_from_soup(soup)
            if not marc:
                continue
            marc["data_source"] = "kolis_scrape"
            marc["kolis_bib_key"] = bib_key
            if _marc_page_matches_isbn(soup, normalized):
                return marc
            if fallback is None:
                fallback = marc

    return fallback


def scrape_marc_041_from_web(
    view_key: str,
    view_type: str = "AH1",
) -> dict[str, Any] | None:
    """
    NLK MARC 보기 페이지에서 041 필드를 크롤링.
    실패 시 None 반환 (앱 중단 없음).
    """
    key = (view_key or "").strip()
    if not key:
        return None

    vtype = (view_type or "AH1").strip() or "AH1"
    params = {"viewKey": key, "viewType": vtype}
    headers = {
        **DEFAULT_HEADERS,
        "Referer": f"{DETAIL_PAGE_URL}?viewKey={key}&viewType={vtype}",
    }

    try:
        response = requests.get(
            MARC_VIEW_URL,
            params=params,
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        )
        if response.status_code >= 400:
            return None
        soup = BeautifulSoup(response.content, "html.parser")
        return _extract_041_from_soup(soup)
    except (requests.RequestException, ValueError, TypeError):
        return None
    except Exception:
        return None


def _isbn_matches_field(isbn_field: str, target: str) -> bool:
    if not isbn_field or not target:
        return False
    tokens = re.findall(r"[\dX]{10,13}", isbn_field.upper())
    return target in tokens or any(target.endswith(t[-10:]) for t in tokens)


def pick_best_item(items: list[dict[str, str]], isbn: str) -> dict[str, str] | None:
    if not items:
        return None
    for item in items:
        if _isbn_matches_field(item.get("isbn", ""), isbn):
            return item
    for item in items:
        if item.get("control_no"):
            return item
    return items[0]


def search_by_isbn(isbn: str) -> dict[str, str]:
    root = call_open_api(kwd=isbn, pageSize=20)
    total = _parse_total(root)
    items = parse_items(root)

    if total == 0 or not items:
        raise NlkApiError(
            f"ISBN '{isbn}'에 해당하는 소장 자료를 찾을 수 없습니다. "
            "국립중앙도서관에 등록된 ISBN인지 확인해 주세요."
        )

    chosen = pick_best_item(items, isbn)
    if not chosen:
        raise NlkApiError("검색 결과에서 도서 항목을 선택하지 못했습니다.")

    control_no = chosen.get("control_no", "").strip()
    if not control_no:
        raise NlkApiError(
            "검색 결과에 제어번호(control_no)가 없습니다. "
            "디지털 자료 등 ISBN만 있는 항목은 지원하지 않을 수 있습니다."
        )
    return chosen


def fetch_detail_by_control_no(control_no: str) -> dict[str, str]:
    """Step 2: detailSearch=true 로 제어번호 상세 조회."""
    attempts: list[dict[str, Any]] = [
        {"detailSearch": "true", "kwd": control_no, "pageSize": 1},
        {"detailSearch": "true", "control_no": control_no, "pageSize": 1},
    ]
    last_items: list[dict[str, str]] = []

    for params in attempts:
        root = call_open_api(**params)
        items = parse_items(root)
        if items:
            for item in items:
                if item.get("control_no", "").strip() == control_no:
                    return item
            return items[0]
        last_items = items

    if last_items:
        return last_items[0]
    raise NlkApiError(
        f"제어번호 '{control_no}'의 상세 서지를 가져오지 못했습니다."
    )


def is_translation(author_info: str, title_info: str = "") -> bool:
    text = f"{author_info} {title_info}"
    if not text.strip():
        return False
    if TRANSLATION_RE.search(text):
        return True
    return bool(TRANSLATOR_ROLE_RE.search(text))


def parse_original_language_from_text(text: str) -> str | None:
    if not text:
        return None
    for pattern, code in LANGUAGE_NOTE_MAP:
        if pattern.search(text):
            return code
    for pattern, code in FOREIGN_NAME_HINTS:
        if pattern.search(text):
            return code
    return None


def infer_language_from_kdc(class_no: str) -> tuple[bool, str | None]:
    """
    Returns (is_korean_original_only, $h code).
    813 → 국내 창작, 84* → 영미, 83* → 일본, 86* → 프랑스 등.
    """
    code = re.sub(r"\s+", "", class_no or "")
    if not code:
        return False, None
    if code.startswith("813"):
        return True, None
    if code.startswith(("84", "743", "744")):
        return False, "eng"
    if code.startswith(("83", "73")):
        return False, "jpn"
    if code.startswith(("86", "745")):
        return False, "fre"
    if code.startswith(("85", "746")):
        return False, "ger"
    if code.startswith(("87", "748")):
        return False, "spa"
    if code.startswith(("88", "747")):
        return False, "ita"
    if code.startswith(("82", "72")):
        return False, "chi"
    if code.startswith(("89", "749")):
        return False, "rus"
    return False, None


def build_marc_041(
    author_info: str,
    title_info: str,
    class_no: str,
) -> dict[str, Any]:
    body_lang = "kor"
    korean_original, kdc_h = infer_language_from_kdc(class_no)
    combined = f"{author_info} {title_info}"
    note_h = parse_original_language_from_text(combined)
    translation = is_translation(author_info, title_info)

    if korean_original:
        indicator = "0"
        original_lang = None
    else:
        indicator = "1" if translation else "0"
        original_lang = note_h or kdc_h
        if indicator == "1" and not original_lang and kdc_h:
            original_lang = kdc_h

    parts = [f"041 {indicator}_", f"$a {body_lang}"]
    if indicator == "1" and original_lang:
        parts.append(f"$h {original_lang}")

    return {
        "marc_041": " ".join(parts),
        "indicator": indicator,
        "body_language": body_lang,
        "original_language": original_lang,
        "is_translation": indicator == "1",
        "data_source": "inference",
    }


def resolve_marc_041(
    author_info: str,
    title_info: str,
    class_no: str,
    view_key: str = "",
    view_type: str = "AH1",
    isbn: str = "",
) -> dict[str, Any]:
    """
    MARC 041 해석 우선순위:
    1) NLK MARC 보기 크롤링
    2) $h 없거나 NLK 크롤링 실패 시 KOLIS-NET 크롤링
    3) 텍스트·KDC 추론
    """
    normalized_isbn = normalize_isbn(isbn)
    nlk_scraped = (
        scrape_marc_041_from_web(view_key, view_type) if view_key else None
    )

    needs_kolis = normalized_isbn and (
        nlk_scraped is None or not nlk_scraped.get("original_language")
    )
    if needs_kolis:
        kolis = scrape_kolisnet_marc_041(normalized_isbn)
        if kolis:
            return kolis

    if nlk_scraped:
        return nlk_scraped

    inferred = build_marc_041(author_info, title_info, class_no)
    if normalized_isbn and not inferred.get("original_language"):
        kolis = scrape_kolisnet_marc_041(normalized_isbn)
        if kolis:
            return kolis
    return inferred


def lookup_by_isbn(isbn: str) -> dict[str, Any]:
    normalized = normalize_isbn(isbn)
    if not normalized:
        raise NlkApiError("ISBN을 입력해 주세요.")
    if not re.fullmatch(r"(?:\d{9}[\dX]|\d{13})", normalized):
        raise NlkApiError(
            "ISBN 형식이 올바르지 않습니다. (ISBN-10: 10자리, ISBN-13: 13자리)"
        )

    try:
        brief = search_by_isbn(normalized)
        control_no = brief["control_no"].strip()
        detail = fetch_detail_by_control_no(control_no)

        author = detail.get("author") or brief.get("author", "")
        title = detail.get("title") or brief.get("title", "")
        class_no = detail.get("class_no") or brief.get("class_no", "")
        view_key = (detail.get("view_key") or brief.get("view_key", "")).strip()
        view_type = extract_view_type(
            detail.get("detail_link") or brief.get("detail_link", "")
        )
        publisher = detail.get("publisher") or brief.get("publisher", "")
        pub_year = detail.get("pub_year") or brief.get("pub_year", "")
    except NlkApiError:
        marc = resolve_marc_041("", "", "", isbn=normalized)
        if marc.get("data_source") == "inference":
            raise
        return {
            "isbn": normalized,
            "control_no": "",
            "view_key": "",
            "view_type": "",
            "title": "",
            "author": "",
            "publisher": "",
            "class_no": "",
            "pub_year": "",
            **marc,
        }

    marc = resolve_marc_041(
        author, title, class_no, view_key, view_type, isbn=normalized
    )

    return {
        "isbn": normalized,
        "control_no": control_no,
        "view_key": view_key,
        "view_type": view_type,
        "title": title,
        "author": author,
        "publisher": publisher,
        "class_no": class_no,
        "pub_year": pub_year,
        **marc,
    }


def main() -> None:
    st.set_page_config(
        page_title="NLK MARC 041 추출기",
        page_icon="📚",
        layout="centered",
    )
    st.title("국립중앙도서관 MARC 041 추출기")
    st.caption(
        "ISBN으로 NLK Open API 조회 후 MARC 보기·KOLIS-NET 크롤링을 시도하고, "
        "실패 시 서지 텍스트·KDC 기반 추론으로 041을 생성합니다."
    )

    isbn_input = st.text_input(
        "ISBN",
        placeholder="예: 9788932917245",
        help="하이픈 유무와 관계없이 입력할 수 있습니다.",
    )

    if st.button("MARC 041 추출하기", type="primary"):
        with st.spinner(
            "국립중앙도서관 Open API 조회 및 MARC 041 크롤링을 진행 중입니다..."
        ):
            try:
                result = lookup_by_isbn(isbn_input)
            except NlkApiError as exc:
                st.error(str(exc))
                return
            except Exception as exc:
                st.error(f"예기치 않은 오류가 발생했습니다: {exc}")
                return

        st.success("MARC 041 필드를 생성했습니다.")

        st.info(
            f"**{result['title'] or '(제목 없음)'}**  \n"
            f"저자: {result['author'] or '—'}  \n"
            f"제어번호: `{result['control_no']}`"
        )

        col1, col2, col3 = st.columns(3)
        with col1:
            st.metric("본문 언어 ($a)", result["body_language"])
        with col2:
            st.metric("원문 언어 ($h)", result["original_language"] or "—")
        with col3:
            st.metric("첫 번째 지시자", result["indicator"])

        st.subheader("MARC 041")
        st.code(result["marc_041"], language=None)

        with st.expander("상세 정보"):
            if result.get("publisher"):
                st.write("**출판사**", result["publisher"])
            if result.get("pub_year"):
                st.write("**발행년**", result["pub_year"])
            if result.get("class_no"):
                st.write("**분류기호 (KDC)**", result["class_no"])
            if result.get("view_key"):
                st.write("**viewKey (ID)**", result["view_key"])
            if result.get("view_type"):
                st.write("**viewType**", result["view_type"])
            source = result.get("data_source", "inference")
            source_labels = {
                "web_scrape": "NLK MARC 보기 크롤링",
                "kolis_scrape": "KOLIS-NET MARC 크롤링",
                "inference": "텍스트·KDC 추론",
            }
            source_label = source_labels.get(source, source)
            if result.get("kolis_bib_key"):
                st.write("**KOLIS bibKey**", result["kolis_bib_key"])
            st.write("**041 데이터 출처**", source_label)
            st.write("**번역서 여부**", "예" if result["is_translation"] else "아니오")
            st.write("**조회 ISBN**", result["isbn"])

    st.divider()
    st.markdown(
        "데이터 출처: [국립중앙도서관 Open API]"
        "(https://www.nl.go.kr/NL/search/openApi/search.do) · "
        "[KOLIS-NET](https://www.nl.go.kr/kolisnet/)"
    )


if __name__ == "__main__":
    main()
