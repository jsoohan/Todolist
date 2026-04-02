# 📋 텔레그램 할일 리마인더 봇 (LLM 기반)

Claude Haiku로 자연어를 이해하는 텔레그램 할일관리 봇. GitHub Actions 30분 cron 기반.

## 특징

**LLM 자연어 이해** — 정해진 형식 없이 자유롭게 말해도 이해합니다:
```
"내일까지 FDD 보고서 마무리해야돼"
"듀이트리 재무검토 이번주 금요일까지 끝내자"
"4/10 ASIABNC 제안서 작성해야함"
"팽팽클리닉 매출 리포트 좀 만들어야 하는데 수요일까지"
```

**할일 무관한 대화는 정중히 거절:**
```
나: "오늘 날씨 어때?"
봇: "저는 할일 관리 전문 봇이에요 😊 할일이 있으시면 알려주세요!"
```

### 리마인더 간격

| 남은 시간 | 간격 |
|-----------|------|
| > 24시간 | 매일 아침 8시 KST |
| 12~24시간 | 4시간마다 |
| ≤ 12시간 | 3시간마다 |
| 입력 미완성 | 매 시간 정각 |
| 매일 요약 | 매일 아침 7시 KST |

### 프로젝트 자동 태깅

`bot.py`의 `PROJECT_KEYWORDS`에서 수정 가능.

| 프로젝트 | 키워드 예시 |
|----------|------------|
| 🎯 Project FUN | fun, funnel, 퍼널, fdd, dio, 우에노 |
| 💄 Project DIVA | diva, 듀이트리, dewytree |
| 🔬 Project ASCLEPIUS | asclepius, 웨이센, 파인메딕스 |
| 🌏 ASIABNC Pre-IPO | asiabnc, 아시아비엔씨, 대봉 |
| 🏥 팽팽클리닉 | 팽팽, pangpang, 실리프팅 |
| 🌲 Greenwood EP | greenwood, 그린우드 |
| 📊 Bionet | bionet, 바이오넷 |

---

## 셋업

### 1. 텔레그램 봇 생성

1. [@BotFather](https://t.me/BotFather) → `/newbot` → 이름/유저네임 설정
2. **API Token** 복사

### 2. Chat ID 확인

1. 봇에게 아무 메시지 전송
2. 브라우저: `https://api.telegram.org/bot<TOKEN>/getUpdates`
3. `"chat":{"id":123456789}` 에서 숫자 복사

### 3. GitHub Secrets 설정

레포 → Settings → Secrets and variables → Actions:

| Secret | 값 |
|--------|---|
| `TELEGRAM_BOT_TOKEN` | BotFather 토큰 |
| `TELEGRAM_CHAT_ID` | Chat ID |
| `ANTHROPIC_API_KEY` | Anthropic API 키 |

### 4. Push & 실행

```bash
git init && git add . && git commit -m "init"
git remote add origin https://github.com/<USER>/<REPO>.git
git push -u origin main
```

Actions 탭에서 워크플로우 활성화. `Run workflow`로 수동 테스트.

---

## 구조

```
├── bot.py                    # 메인 (LLM + 리마인더)
├── requirements.txt
├── data/todos.json           # 상태 (자동 관리)
├── .github/workflows/
│   └── reminder.yml          # 30분 cron
└── README.md
```

## 비용

- **GitHub Actions**: 월 ~720분 / 무료 2,000분 → 충분
- **Claude Haiku**: 메시지당 ~$0.001 이하. 하루 10건 = 월 $0.3 수준
- **리마인더 발송**: LLM 미사용 (규칙 기반) → 추가 비용 없음
