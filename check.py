#!/usr/bin/env python3
"""용산구 공공체육시설 수강신청 페이지에서 특정 강좌의 접수 버튼 상태를 확인하고,
상태가 바뀌면 텔레그램으로 알림을 보낸다.

목록이 자바스크립트/세션에 의존해 채워지므로 실제 크롬(헤드리스)으로 페이지를 열어 읽는다.
1) 강좌 상세 페이지(DETAIL_URL)의 버튼을 먼저 읽고,
2) 못 읽으면 목록 페이지(TARGET_URL)의 신규접수/접수종료 탭에서 강좌 행을 찾는다.

환경 변수
  TELEGRAM_BOT_TOKEN  텔레그램 봇 토큰 (필수)
  TELEGRAM_CHAT_ID    알림을 받을 채팅 ID (필수)
  DETAIL_URL          강좌 상세 페이지 URL
  TARGET_URL          강좌 목록 페이지 URL
  TARGET_KEYWORD      강좌 행을 찾는 키워드 (기본: "배드민턴 20:30")
  STATE_FILE          마지막 상태를 저장하는 파일 (기본: state.json)
  FORCE_NOTIFY        "1"이면 변화가 없어도 현재 상태를 보낸다 (테스트용)
  DUMP_HTML           "1"이면 읽은 페이지를 page_*.html / shot_*.png 로 저장한다 (디버그용)
"""
import html as html_mod
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup

DETAIL_URL = os.environ.get("DETAIL_URL") or (
    "https://yssports.yong-san.or.kr/fmcs/8?center=YGSN01&action=read&page=1"
    "&event=1010000000&class=1010010000&comcd=YGSN01&classcd=00147&type=R"
)
TARGET_URL = os.environ.get("TARGET_URL") or (
    "https://yssports.yong-san.or.kr/fmcs/2?center=YGSN01&event=1010000000&class=1010010000&subject="
)
TARGET_KEYWORD = os.environ.get("TARGET_KEYWORD") or "배드민턴 20:30"
STATE_FILE = os.environ.get("STATE_FILE", "state.json")
FORCE_NOTIFY = os.environ.get("FORCE_NOTIFY") == "1"
DUMP_HTML = os.environ.get("DUMP_HTML") == "1"
PAGE_TIMEOUT_MS = int(os.environ.get("PAGE_TIMEOUT_MS", "60000"))

KST = timezone(timedelta(hours=9))
USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 14; SM-S921N) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Mobile Safari/537.36"
)
# 버튼에 나올 수 있는 상태 문구. 앞에 있는 것이 우선 매칭된다.
STATUS_PATTERN = re.compile(
    r"(접수하기|접수\s*중|접수\s*대기|대기\s*접수|접수\s*예정|접수\s*종료|접수\s*마감|마감|정원\s*초과|신청하기|예약하기)"
)
# 이 문구가 나오면 "접수 가능"으로 본다.
OPEN_PATTERN = re.compile(r"(접수하기|접수\s*중|신청하기|예약하기)")
# 목록 탭: R=신규접수, E=접수종료
LECTURE_TABS = (("R", "신규접수"), ("E", "접수종료"))

SEEN = {}  # 진단용: 화면 이름 -> 그 화면에서 본 강좌명/요약


def now_kst() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S KST")


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").replace("\xa0", " ")).strip()


# ---------- HTML 해석 ----------

def content_soup(html: str) -> BeautifulSoup:
    """본문(강좌 모듈) 부분만 남긴 soup. 메뉴/탭/푸터의 '접수종료' 같은 글자를 제거한다."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "header", "footer"]):
        tag.decompose()
    for sel in (".list_tab", "#menu_topmenu", "#gnb-m", ".breadcrumb", ".footer", ".header"):
        for t in soup.select(sel):
            t.decompose()
    main = soup.select_one(".modules_fmcs_lecture") or soup.select_one("#contents") or soup
    return main


def find_status_in_list(html: str):
    """목록 표에서 대상 강좌 행을 찾아 (상태문구, 행 텍스트)를 돌려준다. 못 찾으면 (None, 이유)."""
    main = content_soup(html)
    for selector in ("tr", "li", "div"):
        for row in main.select(selector):
            text = normalize(row.get_text(" "))
            if TARGET_KEYWORD not in text or text.count(TARGET_KEYWORD) > 1:
                continue
            for el in row.select("button, a, span, input"):
                label = normalize(el.get_text(" ")) or normalize(el.get("value", ""))
                m = STATUS_PATTERN.search(label)
                if m:
                    return normalize(m.group(1)), text
            matches = STATUS_PATTERN.findall(text)
            if matches:
                return normalize(matches[-1]), text
    return None, f"목록에 '{TARGET_KEYWORD}' 행이 없습니다."


def find_status_in_detail(html: str):
    """상세 페이지에서 신청 버튼 상태를 읽는다. (상태문구, 설명) 또는 (None, 이유)."""
    main = content_soup(html)
    text = normalize(main.get_text(" "))
    if TARGET_KEYWORD not in text:
        return None, f"상세 페이지에 '{TARGET_KEYWORD}' 문구가 없습니다. 본문: {text[:150]}"
    # 버튼류 우선(btn 클래스가 있는 것부터), 그다음 일반 링크/스팬
    candidates = []
    for el in main.select("button, a, input[type=button], input[type=submit], span, em, strong"):
        label = normalize(el.get_text(" ")) or normalize(el.get("value", ""))
        m = STATUS_PATTERN.search(label)
        if not m:
            continue
        cls = " ".join(el.get("class", []))
        score = 2 if ("btn" in cls or el.name in ("button", "input")) else 1
        candidates.append((score, normalize(m.group(1)), label))
    if candidates:
        candidates.sort(key=lambda c: -c[0])
        return candidates[0][1], f"버튼 문구: {candidates[0][2]}"
    m = STATUS_PATTERN.findall(text)
    if m:
        return normalize(m[-1]), "본문 텍스트에서 상태 문구 발견"
    return None, f"상세 페이지에 상태 버튼이 없습니다. 본문: {text[:150]}"


def list_course_names(html: str) -> list:
    """목록 표의 각 행에서 강좌명(3번째 칸)을 뽑는다. 진단용."""
    main = content_soup(html)
    names = []
    for r in main.select("tbody tr"):
        if r.select_one("td.empty, .nodata_wrap"):
            continue
        tds = r.find_all("td")
        if len(tds) >= 3:
            names.append(normalize(tds[2].get_text(" ")))
        elif tds:
            names.append(normalize(r.get_text(" "))[:40])
    return names


def seen_summary() -> str:
    parts = []
    for name, val in SEEN.items():
        if isinstance(val, list):
            if val:
                shown = html_mod.escape(", ".join(val[:6]), quote=False) + (f" 외 {len(val) - 6}개" if len(val) > 6 else "")
                parts.append(f"{name}: {len(val)}개 ({shown})")
            else:
                parts.append(f"{name}: 강좌 없음")
        else:
            parts.append(f"{name}: {html_mod.escape(str(val), quote=False)}")
    return "\n".join(parts)


# ---------- 브라우저로 읽기 ----------

def dump(name: str, html: str, page=None) -> None:
    if not DUMP_HTML:
        return
    with open(f"page_{name}.html", "w", encoding="utf-8") as f:
        f.write(html)
    if page is not None:
        try:
            page.screenshot(path=f"shot_{name}.png", full_page=True)
        except Exception:  # noqa: BLE001
            pass


def settle(page) -> None:
    """자바스크립트로 목록이 채워질 때까지 잠시 기다린다."""
    try:
        page.wait_for_load_state("networkidle", timeout=PAGE_TIMEOUT_MS)
    except Exception:  # noqa: BLE001
        pass
    page.wait_for_timeout(1500)


def read_status_with_browser():
    """(상태, 출처, 설명) 또는 (None, None, 이유)."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, executable_path=os.environ.get("CHROMIUM_PATH") or None)
        context = browser.new_context(
            user_agent=USER_AGENT,
            locale="ko-KR",
            viewport={"width": 412, "height": 915},
            is_mobile=True,
        )
        page = context.new_page()
        page.set_default_timeout(PAGE_TIMEOUT_MS)

        # 1) 상세 페이지
        page.goto(DETAIL_URL, wait_until="domcontentloaded")
        settle(page)
        html = page.content()
        dump("detail", html, page)
        status, detail = find_status_in_detail(html)
        SEEN["상세 페이지"] = detail if status is None else f"{status}"
        if status:
            browser.close()
            return status, "상세 페이지", detail
        print(f"[상세 페이지] {detail}")

        # 2) 목록 페이지: 기본 화면 → 신규접수 탭 → 접수종료 탭
        page.goto(TARGET_URL, wait_until="domcontentloaded")
        settle(page)
        html = page.content()
        dump("list_default", html, page)
        SEEN["목록(기본)"] = list_course_names(html)
        status, detail = find_status_in_list(html)
        if status:
            browser.close()
            return status, "목록(기본)", detail

        for code, tab_name in LECTURE_TABS:
            tab = page.locator(f".list_tab a[data-value='{code}']")
            if tab.count() == 0:
                SEEN[f"목록({tab_name})"] = f"탭 버튼을 찾지 못함"
                continue
            try:
                tab.first.click()
                settle(page)
            except Exception as e:  # noqa: BLE001
                SEEN[f"목록({tab_name})"] = f"탭 클릭 실패: {e}"[:120]
                continue
            html = page.content()
            dump(f"list_{code}", html, page)
            SEEN[f"목록({tab_name})"] = list_course_names(html)
            status, detail = find_status_in_list(html)
            if status:
                browser.close()
                return status, f"목록({tab_name})", detail

        browser.close()
    return None, None, "상세 페이지와 목록 어디에서도 강좌 상태를 읽지 못했습니다."


# ---------- 알림/상태 ----------

def send_telegram(message: str) -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": message[:4000], "parse_mode": "HTML", "disable_web_page_preview": True}
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
        status, source, detail = read_status_with_browser()
    except Exception as e:  # noqa: BLE001
        msg = f"페이지를 읽는 중 오류: {type(e).__name__}: {str(e)[:300]}"
        print(msg, file=sys.stderr)
        if prev_error != msg or FORCE_NOTIFY:
            send_telegram(f"⚠️ 배드민턴 감시 오류\n{html_mod.escape(msg)}\n{now_kst()}")
        state.update({"error": msg, "checked_at": now_kst()})
        save_state(state)
        return 1

    print(f"[{now_kst()}] status={status!r} source={source!r} detail={detail[:200]!r}")
    print(seen_summary())

    extra_lines = []
    if status is None:
        status = "읽기 실패"
        source = "-"
        extra_lines.append("🔎 진단:\n" + seen_summary())

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
            f"상태: <b>{html_mod.escape(status)}</b>"
            + (f" (이전: {html_mod.escape(str(prev_status))})" if prev_status and changed else "")
            + f" · {source}",
            f"시간: {now_kst()}",
            *extra_lines,
            f'<a href="{DETAIL_URL}">👉 강좌 페이지 열기</a>',
        ]
        send_telegram("\n".join(lines))
        print("텔레그램 알림 전송 완료")

    save_state({"status": status, "checked_at": now_kst(), "error": None})
    return 0 if status != "읽기 실패" else 1


if __name__ == "__main__":
    sys.exit(main())
