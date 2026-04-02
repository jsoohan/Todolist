# 📋 텔레그램 할일 리마인더 봇 (LLM 기반)

Claude Haiku 자연어 이해 + 즉시 응답 텔레그램 할일관리 봇.  
Railway 클라우드 배포.

## 기능

**자연어 대화:**
```
나: 내일까지 FDD 보고서 마무리해야돼
봇: 📝 등록! 📌 FDD 보고서 마무리 ⏰ 4/4(금) 23:59 📁 🎯 Project FUN

나: 오늘 날씨 어때?
봇: 저는 할일 관리 전문 봇이에요 😊

나: (리마인더 답장) 다했다
봇: ✅ FDD 보고서 마무리 완료!
```

**리마인더 간격 (자동):**

| 남은 시간 | 간격 |
|-----------|------|
| > 24시간 | 매일 아침 8시 |
| 12~24시간 | 4시간 |
| ≤ 12시간 | 3시간 |
| 입력 미완성 | 매 시간 |
| 전체 요약 | 매일 아침 7시 |

---

## Railway 배포 (5분)

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
3. **Variables**에 환경변수 3개:

| Variable | 값 |
|----------|---|
| `TELEGRAM_BOT_TOKEN` | BotFather 토큰 |
| `TELEGRAM_CHAT_ID` | Chat ID |
| `ANTHROPIC_API_KEY` | Anthropic API 키 |

4. Deploy → 끝!

### 5. (선택) Volume
재배포 시 데이터 유지:  
Service → **+ New** → **Volume** → Mount: `/app/data`

---

## 구조

```
├── bot.py               # 메인 (LLM + 리마인더 + long polling)
├── Dockerfile
├── railway.json
├── requirements.txt
├── data/todos.json       # 상태 (자동)
└── README.md
```

## 비용
- **Railway**: 월 $5 크레딧 (봇 ~$3-5)
- **Claude Haiku**: 메시지당 ~$0.001 (월 $0.3 이하)
- 리마인더 발송은 LLM 미사용 → 추가 비용 0

## 프로젝트 태깅 (`bot.py` PROJECT_KEYWORDS)
🎯 Project FUN | 💄 Project DIVA | 🔬 ASCLEPIUS | 🌏 ASIABNC | 🏥 팽팽클리닉 | 🌲 Greenwood | 📊 Bionet
