# 📋 텔레그램 할일 리마인더 봇

GitHub Actions (30분 간격 cron) 기반 텔레그램 할일관리 봇.

## 기능

| 기능 | 설명 |
|------|------|
| 할일 등록 | `[데드라인]까지 [할일]` 형식으로 등록 |
| 스마트 리마인더 | 데드라인까지 남은 시간에 따라 간격 자동 조절 |
| 완료 처리 | 리마인더에 답장 → `완료` / `다했다` / `끝` |
| 매일 요약 | 매일 아침 7시 KST 전체 할일 요약 |
| 프로젝트 태깅 | 키워드 기반 자동 프로젝트 태깅 |
| 미입력 알림 | 데드라인/할일 누락 시 매 시간 리마인더 |

### 리마인더 간격

| 남은 시간 | 리마인더 간격 |
|-----------|--------------|
| > 24시간 | 매일 아침 8시 KST |
| 12~24시간 | 4시간마다 |
| ≤ 12시간 | 3시간마다 |
| 입력 미완성 | 매 시간 정각 |

### 지원하는 데드라인 표현

```
오늘, 내일, 모레, 글피
오늘 3시, 내일 9시
3일후, 5시간후
월요일, 금요일, 다음 수요일
4/10, 4월10일, 4월 10일
2026-04-10, 2026.04.10
```

---

## 셋업 (10분)

### 1. 텔레그램 봇 생성

1. 텔레그램에서 [@BotFather](https://t.me/BotFather) 검색
2. `/newbot` 명령어 전송
3. 봇 이름, 유저네임 설정
4. **API Token** 복사 (예: `7123456789:AAHxxxxxx...`)

### 2. Chat ID 확인

1. 생성한 봇에게 아무 메시지 전송
2. 브라우저에서 접속:
   ```
   https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates
   ```
3. 응답 JSON에서 `"chat":{"id":123456789}` 부분의 숫자가 Chat ID

### 3. GitHub 레포 생성 & Secrets 설정

1. 이 폴더를 GitHub에 새 레포로 push:
   ```bash
   cd telegram-todo-bot
   git init
   git add .
   git commit -m "Initial commit"
   git branch -M main
   git remote add origin https://github.com/<USER>/<REPO>.git
   git push -u origin main
   ```

2. GitHub 레포 → **Settings** → **Secrets and variables** → **Actions**:
   - `TELEGRAM_BOT_TOKEN`: BotFather에서 받은 토큰
   - `TELEGRAM_CHAT_ID`: 위에서 확인한 Chat ID

### 4. Actions 활성화

GitHub 레포 → **Actions** 탭 → 워크플로우 활성화.
`workflow_dispatch` 버튼으로 수동 테스트 가능.

---

## 사용법

```
내일까지 FDD 보고서 완성          → 할일 등록 + Project FUN 자동 태깅
4/10까지 ASIABNC 제안서 작성      → 할일 등록 + ASIABNC Pre-IPO 태깅
금요일까지 듀이트리 재무 검토      → 할일 등록 + Project DIVA 태깅

완료 / 다했다 / 끝                → 리마인더 답장 시 완료 처리
/list                             → 현재 활성 할일 목록
/help                             → 도움말
```

## 프로젝트 자동 태깅 키워드

`bot.py`의 `PROJECT_KEYWORDS` dict에서 수정 가능.

| 프로젝트 | 키워드 |
|----------|--------|
| 🎯 Project FUN | fun, funnel, 퍼널, fdd, dio, e-clinic, 우에노 |
| 💄 Project DIVA | diva, 듀이트리, dewytree |
| 🔬 Project ASCLEPIUS | asclepius, 웨이센, 파인메딕스, pentax |
| 🌏 ASIABNC Pre-IPO | asiabnc, 아시아비엔씨, 대봉 |
| 🏥 팽팽클리닉 | 팽팽, pangpang, 실리프팅, 매일유업, 셀렉스 |
| 🌲 Greenwood EP | greenwood, 그린우드 |
| 📊 Bionet | bionet, 바이오넷 |

---

## 구조

```
telegram-todo-bot/
├── bot.py                          # 메인 봇 로직
├── requirements.txt                # Python 의존성
├── data/
│   └── todos.json                  # 할일 상태 (자동 관리)
├── .github/
│   └── workflows/
│       └── reminder.yml            # GitHub Actions cron
└── README.md
```

## 제한사항

- GitHub Actions cron은 정확히 30분마다가 아닌 수분 지연 가능
- 메시지 폴링이므로 실시간 반응이 아닌 최대 30분 지연
- GitHub Actions 무료 플랜: 월 2,000분 (30분×48회/일×30일 = 720분이므로 충분)
