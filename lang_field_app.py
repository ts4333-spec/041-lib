"""
lang_field_app.py
────────────────────────────────────────────────────────
041(언어코드) · 546(언어주기) 필드 단독 테스트 앱
ISBN 입력 → 국립중앙도서관 Seoji API 자동 조회 → 필드 생성
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
st.caption("ISBN을 입력하면 국립중앙도서관 서지정보를 자동으로 불러와 언어코드(041)와 언어주기(546) 필드를 생성합니다.")

# ── Secrets / 환경변수 ───────────────────────────────
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY") or st.secrets.get("OPENAI_API_KEY", "")
NLK_CERT_KEY = (
    os.getenv("NLK_CERT_KEY")
    or st.secrets.get("NLK_CERT_KEY", "")
    or (st.secrets.get("nlk") or {}).get("cert_key", "")
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

# KDC 대분류(첫 자리) → 분야명
_KDC_TOP: dict[str, str] = {
    "0": "총류", "1": "철학", "2": "종교",
    "3": "사회과학", "4": "자연과학", "5": "기술과학",
    "6": "예술", "7": "언어", "8": "문학", "9": "역사",
}

# KDC 문학 세부 분류: 앞 2자리(81~88) → ISDS 언어코드
# 810~819: 한국문학, 820~829: 중국문학, 830~839: 일본문학,
# 840~849: 영미문학, 850~859: 독일문학, 860~869: 프랑스문학,
# 870~879: 스페인문학, 880~889: 이탈리아문학, 890~899: 기타
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
    """
    KDC 문자열에서 숫자 분류번호 앞 3자리를 추출한다.
    소수점(.)이 있으면 소수점 앞 부분만 사용하고, 그 앞 3자리를 반환.
    예: '813.6' → '813', '843' → '843', '84' → '84'
    """
    # 소수점이 있으면 정수 부분만
    kdc_int = (kdc or "").split(".")[0]
    # 숫자만 추출
    digits = re.sub(r"[^\d]", "", kdc_int)
    return digits


def kdc_to_category_text(kdc: str) -> str:
    """
    KDC 번호를 lang_field.py가 인식하는 categoryText 문자열로 변환.

    예시:
      '843'   → '외국도서>문학>영미문학'
      '813.6' → '국내도서>문학>한국문학'
      '330'   → '외국도서>사회과학'
      ''      → ''

    KDC 문학(8xx)은 앞 2자리로 언어 범위를 판단한다:
      81x=한국, 82x=중국, 83x=일본, 84x=영미, 85x=독일,
      86x=프랑스, 87x=스페인, 88x=이탈리아
    """
    digits = _parse_kdc(kdc)
    if not digits:
        return ""

    top = digits[0]
    top_name = _KDC_TOP.get(top, "")

    # 문학(8xx) — 2자리 앞자리로 언어권 판정
    if top == "8":
        if len(digits) >= 2:
            p2 = digits[:2]
            if p2 == "81":
                return "국내도서>문학>한국문학"
            lang = _KDC_LIT_LANG.get(p2)
            if lang:
                return f"외국도서>문학>{_KDC_LIT_NAMES.get(lang, '외국문학')}"
        return "외국도서>문학"

    # 역사(9xx)
    if top == "9":
        return "외국도서>역사"

    if top_name:
        return f"외국도서>{top_name}"

    return ""


def kdc_to_subject_lang(kdc: str) -> str | None:
    """
    문학 KDC(8xx)에서 언어 힌트 추출. 문학이 아니면 None.
    2자리 앞자리(81~88)로 언어권 판정.
    """
    digits = _parse_kdc(kdc)
    if len(digits) >= 2 and digits[0] == "8":
        p2 = digits[:2]
        if p2 == "81":
            return "kor"
        return _KDC_LIT_LANG.get(p2)
    return None


# ══════════════════════════════════════════════════════
# 제목에서 원제 병기 파싱
# ══════════════════════════════════════════════════════

def extract_original_title_from_title(title: str) -> str:
    """
    제목에 원제가 병기된 경우를 파싱해 원제만 반환한다.

    지원 패턴:
      '캔트 허트 미 : Can't Hurt Me'   → "Can't Hurt Me"
      '어린 왕자(Le Petit Prince)'      → 'Le Petit Prince'
      '데미안 = Demian'                 → 'Demian'
      '어린왕자 / The Little Prince'    → 'The Little Prince'
    """
    title = (title or "").strip()
    if not title:
        return ""

    def _is_foreign(text: str) -> bool:
        text = text.strip()
        if not text:
            return False
        ascii_cnt = sum(1 for c in text if ord(c) < 128 and c not in " \t")
        return ascii_cnt / max(len(text.replace(" ", "")), 1) >= 0.5

    # 괄호 패턴
    m = re.search(r"[(\[（【]([^)\]）】]{2,})[)\]）】]", title)
    if m and _is_foreign(m.group(1)):
        return m.group(1).strip()

    # 구분자 패턴 (콜론·등호·슬래시 뒤)
    m = re.search(r"[:=\/]\s*([A-Za-z][^\:=\/]{1,80})$", title)
    if m and _is_foreign(m.group(1)):
        return m.group(1).strip()

    return ""


# ══════════════════════════════════════════════════════
# 국립중앙도서관 Seoji ISBN 검색 API
# ══════════════════════════════════════════════════════

_SEOJI_URL = "https://www.nl.go.kr/seoji/SearchApi.do"


def nlk_isbn_lookup(isbn13: str) -> tuple[dict, dict]:
    """
    국립중앙도서관 Seoji API로 ISBN-13을 조회하고,
    LangFieldBuilder가 기대하는 (item, detail) 딕셔너리로 변환한다.

    Parameters
    ----------
    isbn13 : str  숫자 13자리 ISBN.

    Returns
    -------
    (item, detail)
      item   : title / publisher / author / categoryText / subInfo 포함
      detail : original_title / subject_lang / category_text 포함

    실패 시 ({}, {}) 반환.
    """
    if not NLK_CERT_KEY:
        dbg_err("NLK_CERT_KEY가 설정되지 않았습니다.")
        return {}, {}

    params = {
        "cert_key":     NLK_CERT_KEY,
        "result_style": "json",
        "page_no":      1,
        "page_size":    1,
        "isbn_gb":      "2",       # 2 = ISBN-13 검색
        "isbn":         isbn13,
    }

    dbg(f"📡 [NLK] 조회 시작: ISBN={isbn13}")
    try:
        resp = requests.get(_SEOJI_URL, params=params, timeout=10)
        resp.raise_for_status()
        raw = resp.text.strip()
        if not raw:
            dbg_err("NLK API 응답이 비어있습니다.")
            return {}, {}
        data = resp.json()
    except requests.RequestException as e:
        dbg_err(f"NLK API 네트워크 오류: {e}")
        return {}, {}
    except ValueError as e:
        dbg_err(f"NLK API JSON 파싱 오류: {e}")
        return {}, {}

    # API 오류 코드 확인
    if data.get("RESULT_CODE") and str(data.get("RESULT_CODE")) != "200":
        dbg_err(f"NLK API 오류 코드: {data.get('RESULT_CODE')} / {data.get('RESULT_MSG', '')}")
        return {}, {}

    docs = data.get("docs", [])
    if not docs:
        dbg("📡 [NLK] 검색 결과 없음 (docs 비어있음)")
        return {}, {}

    doc = docs[0]
    dbg(f"📡 [NLK] 응답 수신: '{doc.get('TITLE', '(제목없음)')}'")

    # ── 원제 추출 ─────────────────────────────────────
    # 1순위: API 직접 제공 ORIGINAL_TITLE
    original_title = (doc.get("ORIGINAL_TITLE") or "").strip()

    # 2순위: TITLE에 병기된 원제 파싱
    raw_title = (doc.get("TITLE") or "").strip()
    if not original_title and raw_title:
        parsed = extract_original_title_from_title(raw_title)
        if parsed:
            original_title = parsed
            dbg(f"📡 [NLK] TITLE 병기에서 원제 파싱: '{original_title}'")

    # ── KDC → 카테고리 & 언어 힌트 ──────────────────────
    kdc_raw = (
        (doc.get("KDC") or "")
        or (doc.get("EA_ADD_CODE") or "")
        or (doc.get("SUBJECT_CODE") or "")
    ).strip()

    category_text = kdc_to_category_text(kdc_raw)
    subject_lang  = kdc_to_subject_lang(kdc_raw)

    if kdc_raw:
        dbg(f"📡 [NLK] KDC={kdc_raw} → category='{category_text}', lang_hint={subject_lang}")
    else:
        dbg("📡 [NLK] KDC 정보 없음 — GPT 판정으로 폴백")

    # ── 저자 정제 (역할어 제거) ────────────────────────
    raw_author = (doc.get("AUTHOR") or "").strip()
    author = re.sub(r"\s*(지음|옮김|역|편|저|글|그림|감수|공저).*$", "", raw_author).strip()

    # ── 발행연도 추출 ─────────────────────────────────
    pub_year = ""
    for field in ("PUBLISH_PREDATE", "REGDATE", "INPUT_DATE"):
        val = (doc.get(field) or "").strip()
        if len(val) >= 4 and val[:4].isdigit():
            pub_year = val[:4]
            break

    # ── item 조립 ─────────────────────────────────────
    item: dict = {
        "title":        raw_title,
        "publisher":    (doc.get("PUBLISHER") or "").strip(),
        "author":       author,
        "categoryText": category_text,
        "subInfo":      {"originalTitle": original_title} if original_title else {},
        # UI 전용 부가 필드 (LangFieldBuilder는 무시)
        "_pub_year":    pub_year,
        "_cover":       (doc.get("TITLE_URL") or "").strip(),
        "_raw_author":  raw_author,
        "_kdc":         kdc_raw,
    }

    # ── detail 조립 ───────────────────────────────────
    detail: dict = {
        "original_title": original_title,
        "subject_lang":   subject_lang,
        "category_text":  category_text,
    }

    dbg(
        f"📡 [NLK] 변환 완료 — "
        f"원제: '{original_title or '(없음)'}', subject_lang: {subject_lang}"
    )
    return item, detail


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

    if not NLK_CERT_KEY:
        st.error(
            "⚠️ NLK_CERT_KEY가 설정되지 않았습니다.  \n"
            "국립중앙도서관 서지정보 유통지원시스템(https://seoji.nl.go.kr)에서 "
            "API 인증키를 발급받고, Streamlit Secrets에 등록해주세요."
        )
        st.stop()

    debug_lines.clear()
    with st.spinner("국립중앙도서관에서 서지정보 조회 중…"):
        api_item, crawl_det = nlk_isbn_lookup(isbn)

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

    cover = item.get("_cover", "")
    meta_caption = (
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
            st.caption(meta_caption)
            if kdc_caption:
                st.caption(kdc_caption)
    else:
        st.markdown(f"**{item.get('title', '')}**")
        st.caption(meta_caption)
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
if not NLK_CERT_KEY:
    st.warning(
        "⚠️ NLK_CERT_KEY가 없어 서지정보 조회가 불가능합니다.  \n"
        "국립중앙도서관 서지정보 유통지원시스템(https://seoji.nl.go.kr)에서 "
        "인증키를 발급받은 후, Streamlit Secrets에 아래와 같이 등록하세요:\n\n"
        "```toml\n[nlk]\ncert_key = \"여기에_인증키\"\n```"
    )
