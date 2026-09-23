# 배드민턴 접수 감시 봇

용산구 공공체육시설 온라인예약 페이지에서 **문화체육센터 배드민턴 20:30_월수금 (20:30~21:50)** 강좌의
버튼이 `접수종료` → `접수하기`로 바뀌면 텔레그램으로 바로 알려줍니다.

- 감시 페이지: https://yssports.yong-san.or.kr/fmcs/2?center=YGSN01&event=1010000000&class=1010010000&subject=
- 확인 주기: 5분마다 (GitHub Actions 최소 간격. 혼잡 시 몇 분 더 늦어질 수 있음)
- 알림 조건: 버튼 문구가 바뀔 때마다 1회. `접수하기`/`접수중`이면 🚨 강조 표시.
- 비용: 저장소를 **Public**으로 만들면 GitHub Actions 무료 무제한. Private이면 월 2,000분 한도를
  넘을 수 있으니(5분마다 ≈ 월 8,000분) Public 권장. 봇 토큰은 Secrets에 저장되므로 코드에 노출되지 않습니다.

## 1. 텔레그램 봇 만들기 (3분)

1. 텔레그램에서 **@BotFather** 검색 → `/newbot` → 이름과 아이디(…bot으로 끝나야 함) 입력.
2. 발급된 **토큰**(예: `123456789:AAH...`)을 복사해 둡니다. → `TELEGRAM_BOT_TOKEN`
3. 방금 만든 봇과 대화방을 열고 아무 메시지나 보냅니다 (예: "안녕").
4. 브라우저에서 아래 주소를 열어 `"chat":{"id":123456789,...}` 의 숫자를 복사합니다. → `TELEGRAM_CHAT_ID`
   ```
   https://api.telegram.org/bot<토큰>/getUpdates
   ```
   (결과가 비어 있으면 봇에게 메시지를 다시 보내고 새로고침)

## 2. GitHub 설정

1. 이 저장소 → **Settings → Secrets and variables → Actions → New repository secret**
   - `TELEGRAM_BOT_TOKEN` = 1번에서 받은 토큰
   - `TELEGRAM_CHAT_ID` = 1번에서 받은 숫자
2. (선택) 다른 강좌를 감시하려면 **Variables** 탭에 `TARGET_KEYWORD` 를 추가 (예: `배드민턴 19:00`).
3. **Actions** 탭 → "배드민턴 접수 감시" → **Run workflow** 로 한 번 수동 실행.
   - 텔레그램으로 "감시 정상 작동 중 / 상태: 접수종료" 메시지가 오면 설정 완료.
   - 오류 메시지가 오면 실행 로그와 `page-html-*` 아티팩트를 확인하세요 (아래 문제 해결 참고).

이후에는 자동으로 5분마다 확인합니다. 접수가 열리면 🚨 메시지와 예약 페이지 링크가 옵니다.

## 3. 동작 방식

- `check.py` 가 페이지 HTML을 받아 `배드민턴 20:30` 이 들어 있는 행을 찾고, 그 행의 버튼 문구
  (`접수종료`, `접수하기`, `접수대기` 등)를 읽습니다.
- 마지막 상태는 `state.json` 에 저장하고, 바뀌었을 때만 알림을 보냅니다 (봇이 자동 커밋).
- 페이지를 못 읽거나 구조가 바뀌면 오류 알림을 **한 번만** 보내고, HTML을 아티팩트로 남깁니다.

## 문제 해결

- **"강좌 목록이 HTML에 없습니다"**: 목록이 자바스크립트로 로드되는 페이지입니다. 아티팩트의
  `page.html` 을 열어 실제 데이터 요청 주소를 찾아 `TARGET_URL` 을 바꾸거나, 파서를 수정해야 합니다.
- **알림이 안 옴**: Secrets 값 확인 → 봇에게 먼저 메시지를 보냈는지 확인 → Actions 로그 확인.
- **60일간 커밋이 없으면** GitHub가 스케줄 실행을 자동으로 끕니다. Actions 탭에서 다시 켜 주세요.
- 로컬 테스트:
  ```bash
  pip install -r requirements.txt
  TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=... FORCE_NOTIFY=1 python check.py
  ```
