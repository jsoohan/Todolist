# 📋 텔레그램 할일 리마인더 봇 (LLM 기반)

LLM 자연어 이해 + 즉시 응답 텔레그램 할일관리 봇.
Railway 클라우드 배포. Google Calendar 연동.

## 기능

### 자연어 대화
```
나: 내일까지 FDD 보고서 마무리해야돼
봇: 📝 등록! 📌 FDD 보고서 마무리 ⏰ 4/4(금) 23:59 📁 🎯 Project FUN

나: 내일 점심에 김대리 미팅 캘박해줘
봇: 📝 등록! 📌 김대리 미팅 ⏰ 4/5(토) 12:00 📅 구글 캘린더에 추가됨 (회사)

나: (리마인더 답장) 다했다
봇: ✅ FDD 보고서 마무리 완료했습니다! 수고하셨어요!

나: 한동규상무한테 확인했어
봇: ✅ 한동규 상무 data request 확인 완료 처리했습니다.
```

### 장기/반복 프로젝트
```
나: 장기프로젝트 매일 7시에 리마인더 해줘 1.헬스케어에이전트 2.어닝콜분석 3.PPT스킬
봇: 🔄 3건 반복 프로젝트 등록! ⏰ 매일 19:00 KST 리마인더

봇 (매일 19시): 🔄 장기 프로젝트 리마인더
  1. 헬스케어에이전트
  2. 어닝콜분석
  3. PPT스킬
  ⏰ 매일 19:00 반복 · 3건

나: (답장) 1번 그만해
봇: 헬스케어에이전트 중단 처리했습니다.
```

### 대량 등록/복원
```
나: 아래 것 다 등록해줘
  • 메가젠 미팅 - 4/13 11:00
  • PPT 만들기 - 매일 19:00 반복
봇: ✅ 2건 등록 완료 (1회성 1 + 반복 1)
```

### Google Calendar 연동 (선택)
```
나: 내일 9시 미팅 회캘에 캘박해줘
봇: 📝 등록! 📅 구글 캘린더에 추가됨 (그린우드)

나: 메가젠 미팅 개캘에 넣어줘
봇: 📅 메가젠 미팅 캘린더에 추가됨 (개인)
```
- "캘박", "캘린더에 넣어줘" 등 명시적 요청 시에만 등록
- 개인 캘린더 / 회사 캘린더 자동 분배 (LLM 판단)
- 완료/삭제 시 캘린더 이벤트 자동 삭제
- 기한 변경 시 캘린더 이벤트 자동 수정

### 리마인더 시스템

**통합 발송** — 동시간대 리마인더를 1개 메시지로 묶어서 가독성 유지

| 남은 시간 | 간격 |
|-----------|------|
| ≤ 12시간 | 3시간 |
| 12~24시간 | 4시간 |
| > 24시간 | 매일 아침 7~9시 |
| 반복 프로젝트 | 매일 지정 시각 |
| 전체 요약 | 매일 아침 7시 |
| 야간 무음 | 0시~6시 KST |

**시간 추론:**
- "점심" → 12:00, "아침" → 09:00, "저녁" → 18:00
- "이번주" → 금요일 23:59, 날짜만 → 23:59

### 안전장치
- **batch 전체 삭제**: "모두/전부/초기화" 같은 명시적 키워드 필수
- **답장 시 전체 삭제 차단**: 답장 컨텍스트에서 batch_filter "all" 차단
- **반복 프로젝트 보호**: batch 완료 시 반복 프로젝트 자동 제외
- **완료 미스매칭 방지**: LLM이 "완료했습니다" 응답만 하고 실제 처리 안 한 경우 감지하여 에러 표시
- **퍼지 매칭**: "한동규 확인" → "한동규 상무 data request 확인" 자동 매칭

### LLM 폴백 체인

| 순서 | 모델 | 비용 |
|------|------|------|
| 1차 | Gemini 2.5 Flash Lite | 무료 |
| 2차 | Gemini 2.5 Flash | 무료 (별도 쿼터) |
| 3차 | Claude Haiku 4.5 | 유료 (최종 폴백) |

---

## Railway 배포

### 1. 텔레그램 봇 생성
1. [@BotFather](https://t.me/BotFather) → `/newbot` → Token 복사

### 2. Chat ID 확인
1. 봇에게 아무 메시지 전송
2. `https://api.telegram.org/bot<TOKEN>/getUpdates` 접속
3. `"chat":{"id":123456789}` → 숫자 복사

### 3. GitHub Push
```bash
git init && git add . && git commit -m "init"
git remote add origin https://github.com/<USER>/<REPO>.git
git push -u origin main
```

### 4. Railway 배포
1. [railway.app](https://railway.app) 가입
2. **New Project** → **Deploy from GitHub repo**
3. **Variables**에 환경변수 설정:

**필수:**
| Variable | 값 |
|----------|---|
| `TELEGRAM_BOT_TOKEN` | BotFather 토큰 |
| `TELEGRAM_CHAT_ID` | Chat ID |
| `GEMINI_API_KEY` | Google AI Studio API 키 |

**선택 (Claude 폴백):**
| Variable | 값 |
|----------|---|
| `ANTHROPIC_API_KEY` | Anthropic API 키 |

**선택 (Google Calendar 연동):**
| Variable | 값 |
|----------|---|
| `GOOGLE_CLIENT_ID` | OAuth 클라이언트 ID |
| `GOOGLE_CLIENT_SECRET` | OAuth 클라이언트 시크릿 |
| `GOOGLE_CAL_PERSONAL_REFRESH_TOKEN` | 개인 캘린더 refresh token |
| `GOOGLE_CAL_PERSONAL_ID` | 개인 캘린더 ID (기본: `primary`) |
| `GOOGLE_CAL_WORK_REFRESH_TOKEN` | 회사 캘린더 refresh token |
| `GOOGLE_CAL_WORK_ID` | 회사 캘린더 ID (기본: `primary`) |

4. Deploy → 끝!

### 5. Google Calendar 설정 (선택)
1. Google Cloud Console에서 Calendar API 활성화
2. OAuth 동의 화면 설정 → 테스트 사용자에 이메일 추가
3. OAuth 2.0 클라이언트 ID 생성 (유형: 데스크톱 앱)
4. `python setup_calendar.py` 실행 (개인/회사 각 1회)
5. 출력된 refresh token을 Railway 환경변수에 추가

### 6. (선택) Volume
재배포 시 데이터 유지:
Service → **+ New** → **Volume** → Mount: `/app/data`

---

## 구조

```
├── bot.py               # 메인 (LLM + 리마인더 + GCal + long polling)
├── setup_calendar.py    # Google Calendar OAuth 설정 (1회 실행)
├── Dockerfile
├── railway.json
├── requirements.txt
├── data/todos.json       # 상태 (자동)
└── README.md
```

## 비용
- **Railway**: 월 $5 크레딧 (봇 ~$3-5)
- **Gemini Flash Lite**: 무료 티어 (1,000 req/day, 15 RPM)
- **Gemini Flash**: 무료 폴백 (250 req/day, 10 RPM)
- **Claude Haiku**: 최종 폴백 시에만 과금 (메시지당 ~$0.001)
- 리마인더 발송은 LLM 미사용 → 추가 비용 0

## 프로젝트 태깅 (`bot.py` PROJECT_KEYWORDS)
🎯 Project FUN | 💄 Project DIVA | 🔬 ASCLEPIUS | 🌏 ASIABNC | 🏥 팽팽클리닉 | 🌲 Greenwood | 📊 Bionet
