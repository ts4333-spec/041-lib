"""
lang_field_app.py
────────────────────────────────────────────────────────
041(언어코드) · 546(언어주기) 필드 단독 테스트 앱
ISBN 입력 → 도서관 정보나루(data4library.kr) API 조회 → 필드 생성

인증키: 공공데이터포털(data.go.kr) 마이페이지에서 발급한 키를
        Streamlit Secrets에 아래와 같이 등록하세요.
        [data4library]
        auth_key = "발급받은키"
────────────────────────────────────────────────────────
"""

import os
import re
import requests

import streamlit as st
from openai import OpenAI
from lang_field import LangFieldBuilder, ISDS_LANGUAGE_CODES

# ── 페이지 설정 ──────────────────────────────────────
st.set_page_config(
    page_title="041 · 546 필드 생성기",
    page_icon="📚",
    layout="centered",
)

st.title("📚 KORMARC 041 · 546 필드 생성기")
st.caption("ISBN을 입력하면 도서관 정보나루 서지정보를 자동으로 불러와 언어코드(041)와 언어주기(546) 필드를 생성합니다.")

# ── Secrets / 환경변수 ───────────────────────────────
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY") or st.secrets.get("OPENAI_API_KEY", "")
DATA4LIB_KEY = (
    os.getenv("DATA4LIB_KEY")
    or st.secrets.get("DATA4LIB_KEY", "")
    or (st.secrets.get("data4library") or {}).get("auth_key", "")
)

if not OPENAI_API_KEY:
    st.error("⚠️ OPENAI_API_KEY가 설정되지 않았습니다. Streamlit Secrets에 등록해주세요.")
    st.stop()

client = OpenAI(api_key=OPENAI_API_KEY)

# ── 디버그 로그 수집 ──────────────────────────────────
debug_lines: list[str] = []
def dbg(*args):
    debug_lines.append(" ".join(str(a) for a in args))
def dbg_err(*args):
    debug_lines.append("❌ " + " ".join(str(a) for a in args))

builder = LangFieldBuilder(
    openai_client=client,
    model=(st.secrets.get("openai", {}) or {}).get("model", "gpt-4o"),
    dbg_fn=dbg,
    dbg_err_fn=dbg_err,
)


# ══════════════════════════════════════════════════════
# KDC 번호 → categoryText / subject_lang 변환
# ══════════════════════════════════════════════════════

_KDC_TOP: dict[str, str] = {
    "0": "총류", "1": "철학", "2": "종교",
    "3": "사회과학", "4": "자연과학", "5": "기술과학",
    "6": "예술", "7": "언어", "8": "문학", "9": "역사",
}

# 앞 2자리 기준: 81x=한국, 82x=중국, 83x=일본, 84x=영미,
# 85x=독일, 86x=프랑스, 87x=스페인, 88x=이탈리아
_KDC_LIT_LANG: dict[str, str] = {
    "81": "kor", "82": "chi", "83": "jpn",
    "84": "eng", "85": "ger", "86": "fre",
    "87": "spa", "88": "ita",
}
_KDC_LIT_NAMES: dict[str, str] = {
    "chi": "중국문학", "jpn": "일본문학", "eng": "영미문학",
    "ger": "독일문학", "fre": "프랑스문학", "spa": "스페인문학",
    "ita": "이탈리아문학",
}


def _parse_kdc(kdc: str) -> str:
    """'813.6' → '813',  '843' → '843'"""
    return re.sub(r"[^\d]", "", (kdc or "").split(".")[0])


def kdc_to_category_text(kdc: str) -> str:
    digits = _parse_kdc(kdc)
    if not digits:
        return ""
    top = digits[0]
    if top == "8":
        if len(digits) >= 2:
            p2 = digits[:2]
            if p2 == "81":
                return "국내도서>문학>한국문학"
            lang = _KDC_LIT_LANG.get(p2)
            if lang:
                return f"외국도서>문학>{_KDC_LIT_NAMES.get(lang, '외국문학')}"
        return "외국도서>문학"
    if top == "9":
        return "외국도서>역사"
    name = _KDC_TOP.get(top, "")
    return f"외국도서>{name}" if name else ""


def kdc_to_subject_lang(kdc: str) -> str | None:
    digits = _parse_kdc(kdc)
    if len(digits) >= 2 and digits[0] == "8":
        p2 = digits[:2]
        return "kor" if p2 == "81" else _KDC_LIT_LANG.get(p2)
    return None


def extract_original_title_from_title(title: str) -> str:
    """제목 병기에서 원제 파싱. 예: '어린 왕자(Le Petit Prince)' → 'Le Petit Prince'"""
    title = (title or "").strip()
    if not title:
        return ""

    def _is_foreign(t: str) -> bool:
        t = t.strip()
        if not t:
            return False
        ascii_cnt = sum(1 for c in t if ord(c) < 128 and c not in " \t")
        return ascii_cnt / max(len(t.replace(" ", "")), 1) >= 0.5

    m = re.search(r"[(\[（【]([^)\]）】]{2,})[)\]）】]", title)
    if m and _is_foreign(m.group(1)):
        return m.group(1).strip()
    m = re.search(r"[:=\/]\s*([A-Za-z][^\:=\/]{1,80})$", title)
    if m and _is_foreign(m.group(1)):
        return m.group(1).strip()
    return ""


# ══════════════════════════════════════════════════════
# 도서관 정보나루 API (data4library.kr)
# ══════════════════════════════════════════════════════
# 엔드포인트: http://data4library.kr/api/srchDtlList
# 인증키: 공공데이터포털(data.go.kr) 발급 키 (authKey 파라미터)
# ISBN-13만 넘기면 도서명·저자·출판사·KDC·표지이미지 반환

_D4L_URL = "http://data4library.kr/api/srchDtlList"


def data4lib_isbn_lookup(isbn13: str) -> tuple[dict, dict]:
    """
    도서관 정보나루 API로 ISBN-13 조회 →
    LangFieldBuilder가 기대하는 (item, detail) 딕셔너리로 변환.

    반환: (item, detail)  실패 시 ({}, {})
    """
    if not DATA4LIB_KEY:
        dbg_err("DATA4LIB_KEY가 설정되지 않았습니다.")
        return {}, {}

    params = {
        "authKey":    DATA4LIB_KEY,
        "isbn13":     isbn13,
        "loaninfoYN": "N",   # 대출통계 제외 (빠른 응답)
        "format":     "json",
    }

    dbg(f"📡 [data4library] 조회 시작: ISBN={isbn13}")
    try:
        resp = requests.get(_D4L_URL, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        dbg_err(f"data4library API 네트워크 오류: {e}")
        return {}, {}
    except ValueError as e:
        dbg_err(f"data4library API JSON 파싱 오류: {e}")
        return {}, {}

    # 오류 응답 확인
    response = data.get("response", {})
    error    = response.get("error")
    if error:
        dbg_err(f"data4library API 오류: {error}")
        return {}, {}

    # 결과 추출 — response.detail[0].book
    detail_list = response.get("detail")
    if not detail_list:
        dbg("📡 [data4library] 검색 결과 없음")
        return {}, {}

    # detail은 리스트 또는 단일 dict일 수 있음
    if isinstance(detail_list, list):
        book = detail_list[0].get("book", detail_list[0])
    else:
        book = detail_list.get("book", detail_list)

    def _f(key: str) -> str:
        return (book.get(key) or "").strip()

    raw_title  = _f("bookname")
    raw_author = _f("authors")
    publisher  = _f("publisher")
    pub_year   = _f("publication_year")[:4]
    kdc_raw    = _f("class_no")       # KDC 분류번호
    cover_url  = _f("bookImageURL")

    dbg(f"📡 [data4library] 응답 수신: '{raw_title or '(제목없음)'}'")

    # 저자 정제 (역할어 제거)
    author = re.sub(r"\s*(지음|옮김|역|편|저|글|그림|감수|공저).*$", "", raw_author).strip()

    # KDC → 카테고리 & 언어 힌트
    category_text = kdc_to_category_text(kdc_raw)
    subject_lang  = kdc_to_subject_lang(kdc_raw)
    if kdc_raw:
        dbg(f"📡 [data4library] KDC={kdc_raw} → category='{category_text}', lang={subject_lang}")
    else:
        dbg("📡 [data4library] KDC 정보 없음 — GPT 판정으로 폴백")

    # 원제 추출
    original_title = extract_original_title_from_title(raw_title)
    if original_title:
        dbg(f"📡 [data4library] 제목 병기에서 원제 파싱: '{original_title}'")

    dbg(f"📡 [data4library] 완료 — 원제: '{original_title or '(없음)'}', subject_lang: {subject_lang}")

    item: dict = {
        "title":        raw_title,
        "publisher":    publisher,
        "author":       author,
        "categoryText": category_text,
        "subInfo":      {"originalTitle": original_title} if original_title else {},
        # UI 전용
        "_pub_year":   pub_year,
        "_cover":      cover_url,
        "_raw_author": raw_author,
        "_kdc":        kdc_raw,
    }
    detail_out: dict = {
        "original_title": original_title,
        "subject_lang":   subject_lang,
        "category_text":  category_text,
    }
    return item, detail_out


# ══════════════════════════════════════════════════════
# UI
# ══════════════════════════════════════════════════════
st.divider()

isbn_input = st.text_input(
    "📖 ISBN-13 입력",
    placeholder="예: 9788937462849",
    max_chars=13,
)

fetch_btn = st.button("🔎 도서 정보 불러오기", use_container_width=True)

# ── ISBN 조회 ─────────────────────────────────────────
if fetch_btn and isbn_input:
    isbn = isbn_input.strip().replace("-", "")
    if len(isbn) != 13 or not isbn.isdigit():
        st.error("ISBN-13은 숫자 13자리여야 합니다.")
        st.stop()

    if not DATA4LIB_KEY:
        st.error(
            "⚠️ DATA4LIB_KEY가 설정되지 않았습니다.  \n"
            "공공데이터포털(https://www.data.go.kr) 마이페이지에서 인증키를 확인하고 "
            "Streamlit Secrets에 등록해주세요."
        )
        st.stop()

    debug_lines.clear()
    with st.spinner("도서관 정보나루에서 서지정보 조회 중…"):
        api_item, crawl_det = data4lib_isbn_lookup(isbn)

    if not api_item:
        st.error("도서 정보를 찾을 수 없습니다. ISBN을 확인하거나 인증키를 점검해주세요.")
        with st.expander("🧭 조회 로그 보기"):
            st.text("\n".join(debug_lines) if debug_lines else "로그 없음")
        st.stop()

    st.session_state["api_item"]  = api_item
    st.session_state["crawl_det"] = crawl_det
    st.session_state["isbn"]      = isbn

# ── 도서 정보 표시 & 수정 폼 ──────────────────────────
if "api_item" in st.session_state:
    item   = st.session_state["api_item"]
    detail = st.session_state["crawl_det"]

    subinfo       = (item.get("subInfo") or {})
    orig_from_api = (subinfo.get("originalTitle") or "").strip()

    st.divider()
    st.subheader("📋 불러온 도서 정보")

    cover     = item.get("_cover", "")
    meta_line = (
        f"{item.get('_raw_author', item.get('author', ''))}  |  "
        f"{item.get('publisher', '')}  |  "
        f"{item.get('_pub_year', '')}"
    )
    kdc_caption = f"KDC: {item['_kdc']}" if item.get("_kdc") else ""

    if cover:
        col_img, col_info = st.columns([1, 3])
        with col_img:
            st.image(cover, width=110)
        with col_info:
            st.markdown(f"**{item.get('title', '')}**")
            st.caption(meta_line)
            if kdc_caption:
                st.caption(kdc_caption)
    else:
        st.markdown(f"**{item.get('title', '')}**")
        st.caption(meta_line)
        if kdc_caption:
            st.caption(kdc_caption)

    st.divider()
    st.subheader("✏️ 정보 확인 · 수정 후 필드 생성")
    st.caption("자동으로 채워진 값을 확인하고 필요하면 수정하세요.")

    col1, col2 = st.columns(2)
    with col1:
        title     = st.text_input("제목",   value=item.get("title", ""))
        publisher = st.text_input("출판사", value=item.get("publisher", ""))
        author    = st.text_input("저자",   value=item.get("author", ""))
    with col2:
        original_title = st.text_input(
            "원제",
            value=orig_from_api or detail.get("original_title", ""),
        )
        category_text = st.text_input(
            "카테고리 (KDC 변환값)",
            value=item.get("categoryText", "") or detail.get("category_text", ""),
            help="KDC에서 자동 변환됩니다. 직접 수정도 가능합니다. 예: 국내도서>문학>한국문학",
        )
        subject_lang = st.text_input(
            "언어 힌트 (선택)",
            value=detail.get("subject_lang", "") or "",
            placeholder="예: jpn  (KDC 문학 분류에서 자동 감지)",
        )

    run_btn = st.button("🚀 041 · 546 필드 생성", type="primary", use_container_width=True)

    if run_btn:
        debug_lines.clear()

        final_item = {
            "title":        title,
            "publisher":    publisher,
            "author":       author,
            "categoryText": category_text,
            "subInfo":      {"originalTitle": original_title} if original_title else {},
        }
        final_detail = {
            "original_title": original_title,
            "subject_lang":   subject_lang or None,
            "category_text":  category_text,
        }

        with st.spinner("언어 판정 중…"):
            tag_041, tag_546, orig = builder.get_kormarc_tags(final_item, final_detail)

        st.divider()
        st.subheader("✅ 생성 결과")

        if tag_041 and "$h" in tag_041:
            mrk_041 = builder.as_mrk_041(tag_041)
            mrk_546 = builder.as_mrk_546(tag_546)

            col_a, col_b = st.columns(2)
            with col_a:
                st.markdown("**041 언어코드 필드**")
                st.code(mrk_041 or tag_041, language="text")
            with col_b:
                st.markdown("**546 언어주기 필드**")
                st.code(mrk_546 or tag_546 or "(없음)", language="text")

            h_code = builder.extract_lang_h(tag_041)
            a_code = builder.lang3_from_tag041(tag_041)
            st.info(
                f"📖 본문 언어: **{ISDS_LANGUAGE_CODES.get(a_code or '', '?')}** (`{a_code}`)"
                f"　　원서 언어: **{ISDS_LANGUAGE_CODES.get(h_code or '', '?')}** (`{h_code}`)"
            )
            if orig:
                st.caption(f"원제: {orig}")

        elif tag_041 and tag_041.startswith("📕"):
            st.error(tag_041)

        else:
            lang_a = builder.detect_language(title)
            if builder.is_domestic_category(category_text):
                lang_a = "kor"
            st.success("✅ 번역서가 아닌 것으로 판정 — 041 · 546 필드를 생성하지 않습니다.")
            st.info(
                f"📖 본문 언어 추정: **{ISDS_LANGUAGE_CODES.get(lang_a, '?')}** (`{lang_a}`)"
            )

        with st.expander("🧭 판정 로그 보기"):
            st.text("\n".join(debug_lines) if debug_lines else "로그 없음")

elif not fetch_btn:
    st.info("ISBN-13을 입력하고 '도서 정보 불러오기' 버튼을 눌러주세요.")

# ── 인증키 미설정 안내 ────────────────────────────────
if not DATA4LIB_KEY:
    st.warning(
        "⚠️ DATA4LIB_KEY가 없어 서지정보 조회가 불가능합니다.  \n"
        "공공데이터포털(https://www.data.go.kr) 마이페이지에서 인증키를 확인한 후 "
        "Streamlit Secrets에 아래와 같이 등록하세요:\n\n"
        "```toml\n[data4library]\nauth_key = \"여기에_공공데이터포털_인증키\"\n```"
    )
