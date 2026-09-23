#!/usr/bin/env python3
"""용산구 공공체육시설 수강신청 페이지에서 특정 강좌의 접수 버튼 상태를 확인하고,
상태가 바뀌면 텔레그램으로 알림을 보낸다.

환경 변수
  TELEGRAM_BOT_TOKEN  텔레그램 봇 토큰 (필수)
  TELEGRAM_CHAT_ID    알림을 받을 채팅 ID (필수)
  TARGET_URL          감시할 페이지 URL
  TARGET_KEYWORD      강좌 행을 찾는 키워드 (기본: "배드민턴 20:30")
  STATE_FILE          마지막 상태를 저장하는 파일 (기본: state.json)
  FORCE_NOTIFY        "1"이면 변화가 없어도 현재 상태를 보낸다 (테스트용)
  DUMP_HTML           "1"이면 받은 HTML을 page.html로 저장한다 (디버그용)
"""
import json
import os
import re
import ssl
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

TARGET_URL = os.environ.get(
    "TARGET_URL",
    "https://yssports.yong-san.or.kr/fmcs/2?center=YGSN01&event=1010000000&class=1010010000&subject=",
)
TARGET_KEYWORD = os.environ.get("TARGET_KEYWORD", "배드민턴 20:30")
STATE_FILE = os.environ.get("STATE_FILE", "state.json")
FORCE_NOTIFY = os.environ.get("FORCE_NOTIFY") == "1"
DUMP_HTML = os.environ.get("DUMP_HTML") == "1"

KST = timezone(timedelta(hours=9))
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9",
}
# 버튼에 나올 수 있는 상태 문구. 앞에 있는 것이 우선 매칭된다.
STATUS_PATTERN = re.compile(
    r"(접수하기|접수\s*중|접수\s*대기|대기\s*접수|접수\s*예정|접수\s*종료|접수\s*마감|마감|정원\s*초과|신청하기|예약하기)"
)
# 이 문구가 나오면 "접수 가능"으로 본다.
OPEN_PATTERN = re.compile(r"(접수하기|접수\s*중|신청하기|예약하기)")


def now_kst() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S KST")


def complete_ca_bundle() -> str:
    """사이트가 중간 인증서를 보내지 않을 때, 서버 인증서에 적힌 발급기관(AIA) 주소에서
    중간 인증서를 내려받아 certifi 기본 신뢰 목록에 덧붙인 번들 파일을 만든다.
    브라우저가 하는 'AIA fetching'과 같은 동작이며, 인증서 검증은 그대로 유지된다."""
    import certifi
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    from cryptography.x509.oid import AuthorityInformationAccessOID

    parsed = urlparse(TARGET_URL)
    # 서버가 제시하는 인증서(공개 정보)를 읽어 발급기관 주소를 찾는다.
    leaf_pem = ssl.get_server_certificate((parsed.hostname, parsed.port or 443))
    cert = x509.load_pem_x509_certificate(leaf_pem.encode())

    extra = []
    for _ in range(4):  # 중간 인증서가 여러 단계일 수 있음
        if cert.issuer == cert.subject:
            break  # 루트 인증서에 도달
        try:
            aia = cert.extensions.get_extension_for_class(x509.AuthorityInformationAccess).value
        except x509.ExtensionNotFound:
            break
        urls = [
            d.access_location.value
            for d in aia
            if d.access_method == AuthorityInformationAccessOID.CA_ISSUERS
        ]
        if not urls:
            break
        r = requests.get(urls[0], timeout=30)
        r.raise_for_status()
        data = r.content
        try:
            issuer = x509.load_der_x509_certificate(data)
        except ValueError:
            issuer = x509.load_pem_x509_certificate(data)
        extra.append(issuer.public_bytes(serialization.Encoding.PEM).decode())
        print(f"중간 인증서 확보: {issuer.subject.rfc4514_string()} <- {urls[0]}")
        cert = issuer

    if not extra:
        raise RuntimeError("발급기관 주소(AIA)에서 중간 인증서를 찾지 못했습니다.")

    bundle_path = os.path.join(tempfile.gettempdir(), "ca_bundle_with_intermediates.pem")
    with open(certifi.where(), encoding="utf-8") as src, open(bundle_path, "w", encoding="utf-8") as dst:
        dst.write(src.read())
        dst.write("\n")
        dst.write("\n".join(extra))
    return bundle_path


def fetch_html(url: str = None) -> str:
    """페이지 HTML을 가져온다. 인증서 체인이 불완전하면 중간 인증서를 보충해 다시 검증한다."""
    url = url or TARGET_URL
    last_err = None
    verify = True
    tried_bundle = False
    for attempt in range(4):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=30, verify=verify)
            resp.raise_for_status()
            if not resp.encoding or resp.encoding.lower() == "iso-8859-1":
                resp.encoding = resp.apparent_encoding or "utf-8"
            return resp.text
        except requests.exceptions.SSLError as e:
            last_err = e
            if not tried_bundle:
                tried_bundle = True
                print(f"인증서 검증 실패, 중간 인증서를 보충합니다: {e}")
                try:
                    verify = complete_ca_bundle()
                    continue
                except Exception as e2:  # noqa: BLE001
                    last_err = RuntimeError(f"{e}; 중간 인증서 보충 실패: {e2}")
                    break
            time.sleep(5 * (attempt + 1))
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"페이지를 가져오지 못했습니다: {last_err}")


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def find_status(html: str):
    """대상 강좌 행을 찾아 (상태문구, 행 텍스트)를 돌려준다. 못 찾으면 (None, 이유)."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    # 1) 표 형태(tr), 목록 형태(li), 그 외 div 순으로 "행" 후보를 찾는다.
    for selector in ("tr", "li", "div"):
        for row in soup.select(selector):
            text = normalize(row.get_text(" "))
            if TARGET_KEYWORD not in text:
                continue
            # 행 안에 키워드가 여러 번 나오면(상위 컨테이너) 더 작은 행을 찾기 위해 건너뛴다.
            if text.count(TARGET_KEYWORD) > 1:
                continue
            # 버튼/링크 요소를 우선 확인
            for el in row.select("button, a, span, input"):
                label = normalize(el.get_text(" ")) or normalize(el.get("value", ""))
                m = STATUS_PATTERN.search(label)
                if m:
                    return normalize(m.group(1)), text
            # 버튼 요소가 없으면 행 텍스트 전체에서 상태 문구를 찾는다(마지막 것 사용).
            matches = STATUS_PATTERN.findall(text)
            if matches:
                return normalize(matches[-1]), text

    page_text = normalize(soup.get_text(" "))
    if TARGET_KEYWORD in page_text:
        return None, "키워드는 있지만 행/버튼 구조를 해석하지 못했습니다."
    if "접수" not in page_text and "강좌" not in page_text:
        return None, "강좌 목록이 HTML에 없습니다(자바스크립트로 로드되는 페이지일 수 있음)."
    return None, f"페이지에 '{TARGET_KEYWORD}' 문구가 없습니다."


# 목록 탭: R=신규접수, E=접수종료. 접수가 열리면 강좌가 R 탭으로 옮겨간다.
LECTURE_TABS = (("R", "신규접수"), ("E", "접수종료"))
MAX_PAGES = int(os.environ.get("MAX_PAGES", "5"))


def build_url(lecture_type: str, page: int) -> str:
    parsed = urlparse(TARGET_URL)
    params = dict(parse_qsl(parsed.query, keep_blank_values=True))
    params["lecture_type"] = lecture_type
    params["page"] = str(page)
    return urlunparse(parsed._replace(query=urlencode(params, encoding="utf-8")))


def has_rows(html: str) -> bool:
    """목록 표에 실제 강좌 행이 있는지(빈 목록 안내 행 제외)."""
    soup = BeautifulSoup(html, "html.parser")
    rows = soup.select(".modules_fmcs_lecture tbody tr") or soup.select("tbody tr")
    return any(not r.select_one("td.empty, .nodata_wrap") for r in rows)


def find_status_all_tabs():
    """두 탭(신규접수→접수종료)을 페이지별로 훑어 대상 강좌의 상태를 찾는다.
    반환: (상태, 탭이름, 행텍스트) 또는 (None, None, 이유)."""
    last_reason = "목록을 읽지 못했습니다."
    for code, tab_name in LECTURE_TABS:
        prev_html = None
        for page in range(1, MAX_PAGES + 1):
            url = build_url(code, page)
            html = fetch_html(url)
            if DUMP_HTML:
                for name in (f"page_{code}_{page}.html", "page.html"):
                    with open(name, "w", encoding="utf-8") as f:
                        f.write(html)
            if html == prev_html:
                print(f"[{tab_name} 탭 {page}페이지] 이전 페이지와 동일, 중단")
                break
            prev_html = html
            status, detail = find_status(html)
            if status:
                return status, tab_name, detail
            if not has_rows(html):
                print(f"[{tab_name} 탭 {page}페이지] 강좌 행 없음")
                break
            print(f"[{tab_name} 탭 {page}페이지] 강좌 있음, 대상 없음")
            last_reason = detail
    return None, None, "신규접수/접수종료 탭 어디에도 대상 강좌가 없습니다. " + last_reason


def send_telegram(message: str) -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    r = requests.post(url, json=payload, timeout=30)
    r.raise_for_status()


def load_state() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
        f.write("\n")


def main() -> int:
    for key in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        if not os.environ.get(key):
            print(f"환경 변수 {key} 가 없습니다.", file=sys.stderr)
            return 2

    state = load_state()
    prev_status = state.get("status")
    prev_error = state.get("error")

    try:
        status, tab_name, detail = find_status_all_tabs()
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        print(msg, file=sys.stderr)
        # 같은 오류를 반복해서 보내지 않는다.
        if prev_error != msg:
            send_telegram(f"⚠️ 배드민턴 감시 오류\n{msg}\n{now_kst()}")
            state.update({"error": msg, "checked_at": now_kst()})
            save_state(state)
        return 1

    print(f"[{now_kst()}] status={status!r} tab={tab_name!r} detail={detail[:200]!r}")

    if status is None:
        # 두 탭 모두에 강좌가 없음: 강좌가 목록에서 내려간 상태로 취급하고 변화 시에만 알림
        status = "목록에 없음"
        tab_name = "-"

    is_open = bool(OPEN_PATTERN.search(status))
    changed = status != prev_status

    if changed or FORCE_NOTIFY:
        if is_open:
            head = "🏸🚨 <b>배드민턴 접수 시작!</b>"
        elif changed:
            head = "🏸 배드민턴 강좌 상태 변경"
        else:
            head = "🏸 배드민턴 감시 정상 작동 중"
        lines = [
            head,
            f"강좌: {TARGET_KEYWORD} (월수금 20:30~21:50)",
            f"상태: <b>{status}</b>" + (f" (이전: {prev_status})" if prev_status and changed else "") + f" · {tab_name} 탭",
            f"시간: {now_kst()}",
            f'<a href="{TARGET_URL}">👉 예약 페이지 열기</a>',
        ]
        send_telegram("\n".join(lines))
        print("텔레그램 알림 전송 완료")

    state = {"status": status, "checked_at": now_kst(), "error": None}
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
