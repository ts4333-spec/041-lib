"""
번역서–원서 정보 추출 대시보드 (국립중앙도서관 ISBN/CIP Search API)

- 단일 ISBN 조회 UI + 세션에 성공 건 누적
- `fetch_isbn_records` / `record_to_display_row` 등으로 대량 ISBN 배치 처리로 확장하기 쉬운 구조
"""

from __future__ import annotations

import io
import re
import time
from typing import Any, Iterable
from urllib.parse import urlparse

import pandas as pd
import requests
import streamlit as st

# ---------------------------------------------------------------------------
# 상수: API 기본 설정 (엔드포인트만 바꿔서 스테이징/미러 등으로 교체 가능)
# ---------------------------------------------------------------------------

# 공식 문서 기준 ISBN 서지 검색 API (JSON)
DEFAULT_SEARCH_API_URL = "https://www.nl.go.kr/seoji/SearchApi.do"

# 일부 자료에서 사용하는 대체 호스트 (필요 시 사이드바에서 URL 오버라이드 가능)
ALTERNATE_SEARCH_API_URL = "https://seoji.nl.go.kr/landingPage/SearchApi.do"

# 기본 타임아웃(초): (연결, 읽기) — 해외·방화벽 환경에서는 연결이 더 걸리는 경우가 많다.
DEFAULT_HTTP_TIMEOUT: tuple[float, float] = (45.0, 120.0)

# 연결 단계에서 재시도할 예외 (DNS/방화벽 일시 차단·서버 과부하 등)
_TRANSIENT_NET_ERRORS: tuple[type[BaseException], ...] = (
    requests.exceptions.ConnectTimeout,
    requests.exceptions.ReadTimeout,
    requests.exceptions.ConnectionError,
)

# API 에러 코드 → 사용자 안내 문구 (문서: books.nl.go.kr ISBN 서지정보 활용방법)
ERR_CODE_MESSAGES: dict[str, str] = {
    "000": "시스템 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.",
    "010": "인증키(cert_key)가 요청에 포함되지 않았습니다.",
    "011": "유효하지 않은 인증키입니다. 국립중앙도서관에서 발급받은 키인지 확인해 주세요.",
    "012": "필수 파라미터가 누락되었습니다. (cert_key, result_style, page_no, page_size)",
}


def redact_cert_key_in_text(text: str) -> str:
    """
    오류 메시지 등에 포함된 cert_key 쿼리 값을 가린다.

    requests 예외 문자열에는 전체 URL이 들어가 인증키가 노출될 수 있다.
    """
    return re.sub(r"(cert_key=)([^&\s]+)", r"\1***", text, flags=re.IGNORECASE)


def host_label(url: str) -> str:
    """로그·안내용으로 URL에서 호스트명만 뽑는다."""
    return urlparse(url or "").netloc or (url or "")


def build_api_url_candidates(primary: str, *, include_common_fallbacks: bool) -> list[str]:
    """
    시도할 SearchApi URL 목록을 만든다 (중복 제거, 사용자 지정 URL을 최우선).

    ``www.nl.go.kr`` 이 방화벽·지역 라우팅 등으로 막히는 경우가 있어
    ``seoji.nl.go.kr`` 대체 경로를 이어 붙인다.
    """
    ordered: list[str] = []
    seen: set[str] = set()
    chain = [primary]
    if include_common_fallbacks:
        chain.extend([ALTERNATE_SEARCH_API_URL, DEFAULT_SEARCH_API_URL])
    for raw in chain:
        u = (raw or "").strip()
        if not u or u in seen:
            continue
        seen.add(u)
        ordered.append(u)
    return ordered


# ---------------------------------------------------------------------------
# ISBN 정규화 / 문자열 유틸
# ---------------------------------------------------------------------------


def normalize_isbn(isbn: str) -> str:
    """
    ISBN 입력값에서 하이픈·공백을 제거해 숫자(및 X)만 남긴다.

    대량 처리 시에도 동일 함수를 재사용하면 입력 파편화를 한곳에서 관리할 수 있다.
    """
    cleaned = re.sub(r"[^0-9Xx]", "", (isbn or "").strip())
    return cleaned.upper()


def _strip_or_none(value: Any) -> str | None:
    """API 필드가 빈 문자열·None일 때 None으로 통일."""
    if value is None:
        return None
    s = str(value).strip()
    return s if s else None


# ---------------------------------------------------------------------------
# 원제(ORIGINAL_TITLE) 추출: API 필드 부재 시 저자 문자열에서 보조 추출
# ---------------------------------------------------------------------------

# "원저:", "Original Title:" 등 키워드 뒤의 텍스트를 잡기 위한 정규식 (대소문자 무시)
_ORIGINAL_TITLE_PATTERNS: list[re.Pattern[str]] = [
    # 구분자(세미콜론·슬래시 등) 또는 줄바꿈 전까지, 없으면 문자열 끝까지
    re.compile(
        r"(?:원저|원제|원\s*저|원\s*제)\s*[:：]\s*(?P<t>.+?)(?=\s*(?:;|；|/|\||\n)|$)",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"Original\s*Title\s*[:：]\s*(?P<t>.+?)(?=\s*(?:;|；|/|\||\n)|$)",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"원\s*제목\s*[:：]\s*(?P<t>.+?)(?=\s*(?:;|；|/|\||\n)|$)",
        re.IGNORECASE | re.DOTALL,
    ),
]


def extract_original_title_from_author(author: str | None) -> str | None:
    """
    AUTHOR 필드에 원제가 함께 기재된 경우(예: '번역자 지음 ; 원저: The Hobbit')에서 원제를 추출한다.

    API에 ORIGINAL_TITLE 전용 필드가 없거나 비어 있을 때만 사용한다.
    """
    if not author:
        return None
    text = author.strip()
    for pat in _ORIGINAL_TITLE_PATTERNS:
        m = pat.search(text)
        if m:
            candidate = m.group("t").strip()
            # 괄호·따옴표만 있는 경우 등 지나치게 짧은 노이즈는 버린다
            if len(candidate) >= 2:
                return candidate
    return None


def resolve_original_title(raw: dict[str, Any]) -> str | None:
    """
    응답 dict에서 원제를 결정한다.

    우선순위:
    1) API가 제공하는 표준/비표준 키 (버전 차이 대비)
    2) AUTHOR 문자열 정규식 보조 추출
    """
    for key in (
        "ORIGINAL_TITLE",
        "ORIGINALTITLE",
        "ORIGINAL TITLE",
        "ORIGINAL_WORK_TITLE",
        "원제",
    ):
        if key in raw:
            v = _strip_or_none(raw.get(key))
            if v:
                return v
    return extract_original_title_from_author(_strip_or_none(raw.get("AUTHOR")))


# ---------------------------------------------------------------------------
# 발행일: 문서상 PUBLISH_PREDATE(출판예정일) 등을 REAL_PUBLISH_DATE 슬롯에 매핑
# ---------------------------------------------------------------------------


def resolve_publish_date(raw: dict[str, Any]) -> str | None:
    """
    '실제 발행일'에 해당하는 값을 가능한 한 채운다.

    공식 문서에는 PUBLISH_PREDATE(출판예정일)가 명시되어 있으며,
    일부 응답 스키마에는 REAL_PUBLISH_DATE 등 추가 키가 있을 수 있어 순차 시도한다.
    """
    for key in (
        "REAL_PUBLISH_DATE",
        "REAL_PUBLISHDATE",
        "PUBLISH_DATE",
        "PUBLISH_DATE_REAL",
        "PUBLISH_PREDATE",
        "PUBLISHDATE",
    ):
        v = _strip_or_none(raw.get(key))
        if v:
            return v
    return None


def resolve_image_url(raw: dict[str, Any]) -> str | None:
    """표지 URL: 문서 필드명은 TITLE_URL이며, 다른 키가 있으면 그것도 허용한다."""
    for key in ("TITLE_URL", "IMAGE_URL", "COVER_URL", "IMG_URL"):
        v = _strip_or_none(raw.get(key))
        if v:
            return v
    return None


# ---------------------------------------------------------------------------
# JSON 응답에서 도서 레코드 목록 추출 (스키마 변형에 강하게)
# ---------------------------------------------------------------------------


def extract_book_dicts_from_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """
    SearchApi JSON 최상위 객체에서 도서 레코드(dict)들의 리스트를 꺼낸다.

    문서 예시와 실제 응답이 항상 일치하지 않을 수 있어,
    `docs` 배열 우선 + 그 외 'dict의 리스트' 탐색으로 완화한다.
    """
    if not isinstance(payload, dict):
        return []

    docs = payload.get("docs")
    if isinstance(docs, list):
        return [d for d in docs if isinstance(d, dict)]

    skip_keys = {"RESULT", "ERR_CODE", "ERR_MESSAGE", "PAGE_NO", "TOTAL_COUNT"}
    for key, val in payload.items():
        if key in skip_keys:
            continue
        if isinstance(val, list) and val and isinstance(val[0], dict):
            return [d for d in val if isinstance(d, dict)]

    # 단일 레코드가 최상위에 평탄하게 오는 비표준 케이스
    if any(k in payload for k in ("EA_ISBN", "TITLE", "AUTHOR")) and "RESULT" not in payload:
        return [payload]
    return []


# ---------------------------------------------------------------------------
# API 호출 계층 (requests 단일 진입점 → 이후 배치 루프에서 그대로 재사용)
# ---------------------------------------------------------------------------


class SeojiApiError(Exception):
    """국립중앙도서관 서지 API가 RESULT=ERROR로 응답할 때."""

    def __init__(self, code: str | None, message: str | None):
        self.code = code
        self.message = message
        super().__init__(message or code or "Unknown API error")


def fetch_isbn_records(
    isbn: str,
    cert_key: str,
    *,
    api_url: str = DEFAULT_SEARCH_API_URL,
    page_no: int = 1,
    page_size: int = 10,
    session: requests.Session | None = None,
    timeout: float | tuple[float, float] = DEFAULT_HTTP_TIMEOUT,
    max_retries_per_url: int = 2,
    retry_backoff_sec: float = 2.0,
    try_fallback_hosts: bool = True,
) -> tuple[list[dict[str, Any]], str]:
    """
    ISBN으로 SearchApi를 호출해 원시 레코드(dict) 리스트를 반환한다.

    연결 타임아웃이 잦은 환경을 위해 (1) URL 후보를 순차 시도하고
    (2) 호스트별로 짧은 간격의 재시도(backoff)를 수행한다.

    Returns:
        (records, effective_api_url) — 성공한 요청에 사용된 전체 URL 문자열.
    """
    params: dict[str, Any] = {
        "cert_key": cert_key,
        "result_style": "json",
        "page_no": page_no,
        "page_size": page_size,
        "isbn": isbn,
    }
    sess = session or requests.Session()
    candidates = build_api_url_candidates(api_url, include_common_fallbacks=try_fallback_hosts)
    last_exc: BaseException | None = None

    for base in candidates:
        for attempt in range(max(1, int(max_retries_per_url))):
            try:
                resp = sess.get(base, params=params, timeout=timeout)
                resp.raise_for_status()
                try:
                    payload = resp.json()
                except ValueError as exc:
                    raise ValueError(
                        "API 응답이 JSON이 아닙니다. result_style=json 및 URL을 확인해 주세요."
                    ) from exc

                if not isinstance(payload, dict):
                    raise ValueError("API JSON 최상위 구조가 객체(dict)가 아닙니다.")

                if payload.get("RESULT") == "ERROR":
                    code = _strip_or_none(payload.get("ERR_CODE"))
                    msg = _strip_or_none(payload.get("ERR_MESSAGE"))
                    raise SeojiApiError(code, msg)

                records = extract_book_dicts_from_payload(payload)
                return records, str(resp.url)

            except SeojiApiError:
                raise
            except requests.HTTPError as exc:
                last_exc = exc
                status = getattr(exc.response, "status_code", None) or 0
                if attempt + 1 < max_retries_per_url and status >= 500:
                    time.sleep(retry_backoff_sec * (attempt + 1))
                    continue
                raise
            except _TRANSIENT_NET_ERRORS as exc:
                last_exc = exc
                time.sleep(retry_backoff_sec * (attempt + 1))
                continue

    hosts = ", ".join(host_label(u) for u in candidates)
    detail = str(last_exc) if last_exc else "알 수 없음"
    raise requests.RequestException(
        f"모든 엔드포인트에서 연결에 실패했습니다. 시도한 호스트: [{hosts}]. 마지막 오류: {detail}"
    ) from last_exc


def record_to_display_row(isbn_query: str, raw: dict[str, Any]) -> dict[str, Any]:
    """
    API 원시 레코드를 화면·CSV·DataFrame에 쓰기 좋은 표준 행(dict)으로 변환한다.

    출력 컬럼명은 요구사항(TITLE, ORIGINAL_TITLE, …, IMAGE_URL)에 맞춘다.
    """
    ea_isbn = _strip_or_none(raw.get("EA_ISBN")) or normalize_isbn(isbn_query)
    title = _strip_or_none(raw.get("TITLE"))
    author = _strip_or_none(raw.get("AUTHOR"))
    publisher = _strip_or_none(raw.get("PUBLISHER"))

    original = resolve_original_title(raw)
    pub_date = resolve_publish_date(raw)
    image_url = resolve_image_url(raw)

    return {
        "ISBN": ea_isbn,
        "TITLE": title,
        "ORIGINAL_TITLE": original,
        "AUTHOR": author,
        "PUBLISHER": publisher,
        "REAL_PUBLISH_DATE": pub_date,
        "IMAGE_URL": image_url,
    }


def process_isbn_lookup(
    isbn_input: str,
    cert_key: str,
    *,
    api_url: str,
    session: requests.Session | None = None,
    timeout: float | tuple[float, float] = DEFAULT_HTTP_TIMEOUT,
    max_retries_per_url: int = 2,
    retry_backoff_sec: float = 2.0,
    try_fallback_hosts: bool = True,
) -> tuple[dict[str, Any] | None, str | None]:
    """
    단일 ISBN 조회의 '오케스트레이션': 검증 → 호출 → 첫 레코드 선택 → 표시행 변환.

    Returns:
        (row, warning_message) — 성공 시 row는 dict, warning은 보조 안내(선택).
        실패는 예외가 아니라 Streamlit에서 메시지 처리할 수 있도록 상위에서 잡는 패턴도 가능하나,
        여기서는 예외를 그대로 올려 HTTP/JSON 문제를 구분한다.
    """
    n_isbn = normalize_isbn(isbn_input)
    if not n_isbn:
        raise ValueError("ISBN을 입력해 주세요.")

    primary = (api_url or "").strip()
    records, effective = fetch_isbn_records(
        n_isbn,
        cert_key,
        api_url=primary,
        page_size=10,
        session=session,
        timeout=timeout,
        max_retries_per_url=max_retries_per_url,
        retry_backoff_sec=retry_backoff_sec,
        try_fallback_hosts=try_fallback_hosts,
    )
    if not records:
        return None, "해당 ISBN으로 조회된 서지가 없습니다. 납본·CIP 여부 및 ISBN 자릿수를 확인해 주세요."

    # ISBN 검색이므로 보통 첫 행이 일치 도서. 여러 건이면 첫 레코드를 사용하고 안내.
    chosen = records[0]
    row = record_to_display_row(n_isbn, chosen)
    warn: list[str] = []
    if len(records) > 1:
        warn.append(f"동일 조건으로 {len(records)}건이 반환되어 첫 번째 결과만 표시합니다.")
    if try_fallback_hosts and primary and host_label(effective) != host_label(primary):
        warn.append(
            f"지정한 주소({host_label(primary)})에 연결되지 않아 "
            f"{host_label(effective)} 로 우회 조회했습니다."
        )
    return row, ("\n".join(warn) if warn else None)


def dataframe_from_rows(rows: Iterable[dict[str, Any]]) -> pd.DataFrame:
    """누적 리스트를 pandas DataFrame으로 변환한다 (컬럼 순서 고정)."""
    cols = [
        "ISBN",
        "TITLE",
        "ORIGINAL_TITLE",
        "AUTHOR",
        "PUBLISHER",
        "REAL_PUBLISH_DATE",
        "IMAGE_URL",
    ]
    return pd.DataFrame(list(rows), columns=cols)


def rows_to_csv_bytes(rows: Iterable[dict[str, Any]]) -> bytes:
    """다운로드용 UTF-8 BOM CSV 바이트 (엑셀 한글 깨짐 방지)."""
    df = dataframe_from_rows(rows)
    buf = io.StringIO()
    df.to_csv(buf, index=False, encoding="utf-8-sig")
    return buf.getvalue().encode("utf-8-sig")


def batch_process_isbns(
    isbns: Iterable[str],
    cert_key: str,
    *,
    api_url: str = DEFAULT_SEARCH_API_URL,
    on_error: str = "skip",
    timeout: float | tuple[float, float] = DEFAULT_HTTP_TIMEOUT,
    max_retries_per_url: int = 2,
    retry_backoff_sec: float = 2.0,
    try_fallback_hosts: bool = True,
) -> tuple[list[dict[str, Any]], list[tuple[str, str]]]:
    """
    여러 ISBN을 순차 처리한다 (향후 ThreadPoolExecutor / asyncio로 확장하기 쉬운 동기 버전).

    Args:
        isbns: ISBN 문자열 iterable (정규화는 내부에서 수행).
        cert_key: API 인증키.
        api_url: SearchApi 엔드포인트.
        on_error: ``skip``이면 실패 ISBN은 건너뛰고 ``errors``에만 기록,
                  ``raise``이면 첫 예외에서 중단.

    Returns:
        (성공 행들, (isbn, 에러메시지) 목록)
    """
    session = requests.Session()
    ok: list[dict[str, Any]] = []
    errors: list[tuple[str, str]] = []

    for raw_isbn in isbns:
        label = normalize_isbn(str(raw_isbn))
        if not label:
            errors.append((str(raw_isbn), "빈 ISBN"))
            continue
        try:
            row, _warn = process_isbn_lookup(
                raw_isbn,
                cert_key,
                api_url=api_url,
                session=session,
                timeout=timeout,
                max_retries_per_url=max_retries_per_url,
                retry_backoff_sec=retry_backoff_sec,
                try_fallback_hosts=try_fallback_hosts,
            )
        except (SeojiApiError, ValueError, requests.RequestException) as exc:
            errors.append((label, str(exc)))
            if on_error == "raise":
                raise
            continue
        if row is None:
            errors.append((label, "조회 결과 없음"))
            continue
        ok.append(row)
    return ok, errors


# ---------------------------------------------------------------------------
# Streamlit UI
# ---------------------------------------------------------------------------


def _init_session_state() -> None:
    if "accumulated_rows" not in st.session_state:
        st.session_state.accumulated_rows = []


def main() -> None:
    st.set_page_config(
        page_title="번역서–원서 정보 추출",
        layout="wide",
    )
    _init_session_state()

    st.title("번역서–원서 정보 추출 대시보드")
    st.caption("국립중앙도서관 ISBN 서지 Search API (`SearchApi.do`, JSON) 기반")

    with st.sidebar:
        st.header("API 설정")
        cert_key = st.text_input(
            "cert_key (인증키)",
            type="password",
            help="국립중앙도서관 ISBN 서지 Open API에서 발급받은 인증키를 입력합니다.",
        )
        api_url = st.text_input(
            "API URL",
            value=DEFAULT_SEARCH_API_URL,
            help=f"기본: 공식 문서의 주소. `www.nl.go.kr` 연결이 안 되면 아래 '대체 호스트 자동 시도'를 켜거나 "
            f"직접 다음 주소로 바꿔 보세요.\n{ALTERNATE_SEARCH_API_URL}",
        )
        try_fallback = st.checkbox(
            "연결 실패 시 대체 호스트 자동 시도",
            value=True,
            help="같은 SearchApi이지만 `seoji.nl.go.kr` 경로로 우회합니다. 방화벽·해외 회선에서 도움이 되는 경우가 있습니다.",
        )
        c_timeout = st.number_input("연결 타임아웃(초)", min_value=5, max_value=180, value=45, step=5)
        r_timeout = st.number_input("읽기 타임아웃(초)", min_value=15, max_value=600, value=120, step=15)
        retries = st.number_input("호스트당 재시도 횟수", min_value=1, max_value=8, value=2, step=1)
        backoff = st.number_input("재시도 간격(초)", min_value=0.5, max_value=30.0, value=2.0, step=0.5)
        st.divider()
        if st.button("누적 목록 비우기", type="secondary"):
            st.session_state.accumulated_rows = []
            st.rerun()

    isbn_input = st.text_input(
        "ISBN 입력 (하이픈 포함 가능)",
        placeholder="예: 9788936434267",
    )

    col_go, _ = st.columns([1, 4])
    with col_go:
        lookup_clicked = st.button("서지 조회", type="primary")

    if lookup_clicked:
        if not cert_key or not cert_key.strip():
            st.error("사이드바에 API 인증키(cert_key)를 입력해 주세요.")
        else:
            try:
                with st.spinner("국립중앙도서관 API에 요청 중입니다…"):
                    sess = requests.Session()
                    row, warn = process_isbn_lookup(
                        isbn_input,
                        cert_key.strip(),
                        api_url=api_url.strip(),
                        session=sess,
                        timeout=(float(c_timeout), float(r_timeout)),
                        max_retries_per_url=int(retries),
                        retry_backoff_sec=float(backoff),
                        try_fallback_hosts=try_fallback,
                    )
                if row is None:
                    st.warning(warn or "조회 결과가 없습니다.")
                else:
                    if warn:
                        st.warning(warn)
                    st.session_state["last_row"] = row
                    st.session_state.accumulated_rows.append(row)
                    st.success("조회에 성공했습니다. 아래 상세를 확인하세요.")
            except SeojiApiError as e:
                code = e.code or ""
                hint = ERR_CODE_MESSAGES.get(code, "")
                st.error(f"API 오류 [{code}]: {e.message or '알 수 없는 오류'}")
                if hint:
                    st.caption(hint)
            except requests.HTTPError as e:
                st.error(f"HTTP 오류: {e}")
            except ValueError as e:
                st.warning(str(e))
            except requests.RequestException as e:
                st.error(redact_cert_key_in_text(f"네트워크 요청 중 문제가 발생했습니다: {e}"))
                with st.expander("연결이 계속 안 될 때"):
                    st.markdown(
                        f"- 사이드바에서 **API URL**을 직접 `{ALTERNATE_SEARCH_API_URL}` 로 바꿔 보세요.\n"
                        "- 회사/학교 방화벽, 보안 프로그램, DNS 필터에서 `*.nl.go.kr` 이 차단되는지 확인하세요.\n"
                        "- 해외·일부 회선에서는 **VPN(한국 노드)** 이 필요할 수 있습니다.\n"
                        "- **연결 타임아웃**을 60~90초로 늘리고, **대체 호스트 자동 시도**를 켠 채 다시 조회해 보세요."
                    )

    last = st.session_state.get("last_row")
    if last:
        st.subheader("최근 조회 상세")
        img_col, meta_col = st.columns([1, 2])
        with img_col:
            url = last.get("IMAGE_URL")
            if url:
                st.image(url, caption="표지", use_container_width=True)
            else:
                st.warning("표지 이미지 URL이 없습니다.")
        with meta_col:
            st.markdown(
                f"""
**국문 제목:** {last.get('TITLE') or '—'}  
**원제:** {last.get('ORIGINAL_TITLE') or '—'}  
**저자/번역자:** {last.get('AUTHOR') or '—'}  
**출판사:** {last.get('PUBLISHER') or '—'}  
**발행일(또는 출판예정일 등):** {last.get('REAL_PUBLISH_DATE') or '—'}  
**ISBN:** {last.get('ISBN') or '—'}  
"""
            )
            if last.get("IMAGE_URL"):
                st.markdown(f"[표지 원본 링크]({last['IMAGE_URL']})")

    st.subheader("누적 조회 목록")
    acc = st.session_state.accumulated_rows
    if not acc:
        st.info("성공적으로 조회된 도서가 여기에 쌓입니다. ISBN을 조회해 보세요.")
    else:
        df_all = dataframe_from_rows(acc)
        st.dataframe(df_all, use_container_width=True, hide_index=True)
        st.download_button(
            label="CSV로 다운로드 (UTF-8 BOM)",
            data=rows_to_csv_bytes(acc),
            file_name="translation_books_bibliography.csv",
            mime="text/csv",
        )


if __name__ == "__main__":
    main()
