#!/usr/bin/env python3
"""
텔레그램 할일관리 리마인더 봇 v2 (LLM 기반, 즉시응답)
- 답장 컨텍스트 → LLM 주입 (리마인더 답장 시 "이건" 자동 해석)
- modify_todo 구현 (기한 연장/단축)
- python-telegram-bot long polling + Claude Haiku + JobQueue
"""

import asyncio
import json
import logging
import os
import re
import uuid
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

import httpx
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ── 로깅 ──────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

# ── 설정 ──────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = int(os.environ["TELEGRAM_CHAT_ID"])
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")

# 멀티 캘린더 설정
GCAL_CONFIGS: dict[str, dict] = {}
if os.environ.get("GOOGLE_CAL_PERSONAL_REFRESH_TOKEN"):
    GCAL_CONFIGS["personal"] = {
        "refresh_token": os.environ["GOOGLE_CAL_PERSONAL_REFRESH_TOKEN"],
        "calendar_id": os.environ.get("GOOGLE_CAL_PERSONAL_ID", "primary"),
        "label": "개인",
    }
if os.environ.get("GOOGLE_CAL_WORK_REFRESH_TOKEN"):
    GCAL_CONFIGS["work"] = {
        "refresh_token": os.environ["GOOGLE_CAL_WORK_REFRESH_TOKEN"],
        "calendar_id": os.environ.get("GOOGLE_CAL_WORK_ID", "primary"),
        "label": "그린우드",
    }
# 기존 단일 캘린더 호환
if not GCAL_CONFIGS and os.environ.get("GOOGLE_REFRESH_TOKEN"):
    GCAL_CONFIGS["personal"] = {
        "refresh_token": os.environ["GOOGLE_REFRESH_TOKEN"],
        "calendar_id": os.environ.get("GOOGLE_CALENDAR_ID", "primary"),
        "label": "개인",
    }
KST = timezone(timedelta(hours=9))
WEEKDAYS_KR = ["월", "화", "수", "목", "금", "토", "일"]

DATA_DIR = Path(os.environ.get("DATA_DIR", "./data"))
DATA_FILE = DATA_DIR / "todos.json"

# ── 프로젝트 키워드 → 태그 ───────────────────────────────
PROJECT_KEYWORDS: dict[str, str] = {
    "fun": "🎯 Project FUN", "funnel": "🎯 Project FUN", "퍼널": "🎯 Project FUN",
    "fdd": "🎯 Project FUN", "dio": "🎯 Project FUN", "e-clinic": "🎯 Project FUN",
    "fin clinic": "🎯 Project FUN", "上野": "🎯 Project FUN", "우에노": "🎯 Project FUN",
    "diva": "💄 Project DIVA", "듀이트리": "💄 Project DIVA", "dewytree": "💄 Project DIVA",
    "asclepius": "🔬 Project ASCLEPIUS", "웨이센": "🔬 Project ASCLEPIUS",
    "파인메딕스": "🔬 Project ASCLEPIUS", "pentax": "🔬 Project ASCLEPIUS", "내시경": "🔬 Project ASCLEPIUS",
    "asiabnc": "🌏 ASIABNC Pre-IPO", "아시아비엔씨": "🌏 ASIABNC Pre-IPO", "대봉": "🌏 ASIABNC Pre-IPO",
    "팽팽": "🏥 팽팽클리닉", "pangpang": "🏥 팽팽클리닉", "실리프팅": "🏥 팽팽클리닉",
    "매일유업": "🏥 팽팽클리닉", "셀렉스": "🏥 팽팽클리닉",
    "greenwood": "🌲 Greenwood EP", "그린우드": "🌲 Greenwood EP",
    "bionet": "📊 Bionet", "바이오넷": "📊 Bionet",
}


# ══════════════════════════════════════════════════════════
#  데이터 관리
# ══════════════════════════════════════════════════════════

def load_data() -> dict:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if DATA_FILE.exists():
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"todos": [], "reminder_msg_map": {}}


def save_data(data: dict):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


STATE = load_data()


def persist():
    save_data(STATE)


# ══════════════════════════════════════════════════════════
#  답장 컨텍스트 조회
# ══════════════════════════════════════════════════════════

def resolve_reply_context(msg) -> dict | None:
    """
    텔레그램 답장(reply)에서 어떤 할일인지 조회.
    reminder_msg_map: 리마인더 메시지 ID → todo ID
    """
    if not msg.reply_to_message:
        return None
    reply_mid = str(msg.reply_to_message.message_id)
    todo_id = STATE.get("reminder_msg_map", {}).get(reply_mid)
    if not todo_id:
        return None
    for t in STATE["todos"]:
        if t["id"] == todo_id and t["status"] in ("active", "pending_input"):
            return t
    return None


# ══════════════════════════════════════════════════════════
#  Claude API - 자연어 이해
# ══════════════════════════════════════════════════════════

def build_system_prompt(now: datetime) -> str:
    active = [t for t in STATE["todos"] if t["status"] in ("active", "pending_input")]
    active_only = [t for t in active if t["status"] == "active"]
    pending_only = [t for t in active if t["status"] == "pending_input"]

    if active_only:
        lines = []
        for t in active_only:
            if t.get("type") == "recurring":
                rt = t.get("reminder_time", "19:00")
                proj = f" [{t['project']}]" if t.get("project") else ""
                lines.append(f"  #{t['id']} - {t.get('task', '(미입력)')} (매일 {rt} 반복){proj}")
            else:
                dl = ""
                if t.get("deadline"):
                    try:
                        dt = datetime.fromisoformat(t["deadline"])
                        dl = f" (마감: {dt.month}/{dt.day} {dt.strftime('%H:%M')})"
                    except Exception:
                        pass
                proj = f" [{t['project']}]" if t.get("project") else ""
                lines.append(f"  #{t['id']} - {t.get('task', '(미입력)')}{dl}{proj}")
        todo_ctx = "\n현재 활성 할일:\n" + "\n".join(lines)
    else:
        todo_ctx = "\n현재 활성 할일: 없음"

    if pending_only:
        plines = []
        for t in pending_only:
            missing = []
            if not t.get("task"): missing.append("할일내용")
            if not t.get("deadline"): missing.append("데드라인")
            plines.append(f"  #{t['id']} - {t.get('task', '(미입력)')} [부족: {', '.join(missing)}] 원본:{t.get('original_message', '')[:30]}")
        todo_ctx += "\n\n⚠️ 미완성 할일 (사용자가 이것을 채우려고 답할 수 있음):\n" + "\n".join(plines)

    return f"""너는 할일 관리 전문 텔레그램 봇 "할일봇"이야.
할일 등록, 완료, 기한 변경, 삭제, 일괄 처리, 목록 확인, 장기/반복 프로젝트 관리를 담당해. 그 외 요청은 정중하게 거절.

현재 시각: {now.strftime('%Y-%m-%d %H:%M')} KST ({WEEKDAYS_KR[now.weekday()]}요일)
{todo_ctx}

반드시 아래 JSON만 출력. JSON 외 텍스트 절대 금지.

{{
  "intent": "new_todo" | "new_recurring" | "bulk_create" | "complete_todo" | "modify_todo" | "delete_todo" | "batch" | "list_todos" | "help" | "off_topic",
  "task": "할일 내용 (new_todo)",
  "tasks": [{{"task": "할일1"}}, {{"task": "할일2"}}],
  "items": [{{"type": "one_time|recurring", "task": "...", "deadline_iso": "...", "reminder_time": "HH:MM", "project": "..."}}],
  "reminder_time": "HH:MM (KST, new_recurring)",
  "deadline_raw": "데드라인 원문 (new_todo, modify_todo)",
  "deadline_iso": "YYYY-MM-DDTHH:MM:SS+09:00 또는 null",
  "new_task": "미완성 할일 채우기용 새 task명 (modify_todo)",
  "add_to_calendar": true | false,
  "calendar": "personal" | "work" | null,
  "todo_id": "대상 #id 또는 null (단건 처리)",
  "batch_action": "complete" | "delete" | "modify" (batch일 때),
  "batch_ids": ["id1", "id2"] (batch일 때 대상 ID 배열),
  "batch_filter": "all" | "overdue" | "recurring" | null (batch일 때 필터),
  "reply": "한국어 답변"
}}

핵심 규칙:

1. **답장(reply) 컨텍스트 처리 (가장 중요):**
   [reply_context: #ID - 할일이름] → 사용자가 특정 리마인더에 답장한 것. 반드시 todo_id에 해당 ID.
   [사용자가 아래 메시지에 답장함] → 답장한 메시지 원문.
     - 원문이 리마인더/목록/등록 메시지면, 그 안에 언급된 할일들을 대화 맥락으로 판단.
     - 원문에 여러 할일이 있고 사용자 메시지가 "이건", "첫번째", "두번째", "둘다" 등이면 원문 내 해당 항목들 매칭.
     - 원문에 1개 할일만 있고 사용자가 "다했다" 등이면 그 할일 complete_todo.
   "이건", "이거", 주어 없는 문장 → 답장 메시지 내에서 언급된 할일.

   ⚠️ **답장 컨텍스트 있을 때 안전 규칙 (매우 중요):**
   - 답장한 메시지에 N건의 할일이 언급되어 있으면, 그 N건만 대상. 절대 다른 활성 할일을 건드리지 말 것.
   - "삭제"라는 말이 있어도 답장한 메시지 안의 할일들에 대한 것. batch_filter: "all" 절대 사용 금지.
   - 답장으로 삭제할 때는 batch + batch_ids (답장 메시지 안의 할일 ID들) 사용.
   - 답장으로 1건 삭제면 delete_todo + todo_id 사용.

1-1. **미완성 할일 채우기 (매우 중요):**
   ⚠️ 미완성 할일(pending_input)이 있고 사용자 메시지가 짧은 시간/날짜 표현이거나 부족 항목을 채우는 답변이면,
   반드시 "modify_todo" intent 사용하고 해당 미완성 할일의 ID를 todo_id에 포함.
   - 미완성 할일이 "피엔케이에서도 체크리스트 받기로 했어" [부족: 데드라인] 이고,
     사용자가 "이번주" → modify_todo, todo_id=해당ID, deadline_iso=이번주 금요일 23:59
   - 미완성 할일이 [부족: 할일내용] 이고 사용자가 내용 제공 → modify_todo, new_task=내용
   - 절대 새 할일(new_todo)로 만들지 말 것.

2. **intent 판단:**
   - 새 할일 (1회성) → "new_todo"
   - **장기/반복 프로젝트**: "매일 리마인더", "장기 프로젝트", "계속 알려줘", "그만할때까지" → "new_recurring"
   - **대량 복원/등록**: 포맷된 목록이나 여러 서로 다른 종류의 할일(1회성+반복 섞임)을 한 번에 등록 → "bulk_create"
     예: "아래 것 다 되살려줘", "이것들 전부 등록", "리스트 복원" + 목록 붙여넣기
   - 완료 단건: "다했다", "끝", "됐어", "그만해", "중단해" → "complete_todo"
   - **완료 암시 표현도 complete_todo**: "~했어", "~했음", "~완료", "~보냈어", "~얘기했어", "~확인했어", "~처리했어", "~끝났어" → 해당 할일 complete_todo
   - **복수 완료**: "둘다/셋다/다 했어" → "batch" (batch_action: "complete", batch_ids: [해당 ID들])
     ⚠️ 반드시 batch_ids에 대상 할일 ID를 명시. 대상 판단 기준:
       - reply_context가 있으면 그 할일
       - 대화 맥락에서 언급된 할일들 (이름/키워드로 활성 목록에서 매칭)
       - "둘다" = 대화에서 언급된 2건, "셋다" = 3건 등. 전체가 아님.
     ⚠️ batch_filter: "all"은 "전부/모두/싹다 삭제/초기화" 같은 명시적 전체 요청에만 사용.
   - 기한 변경: "늘려줘", "연장", "기한 변경" → "modify_todo"
   - 삭제 단건: "삭제해", "취소해" → "delete_todo"
   - **일괄 처리**: "모두/전부/다 삭제", "기한 초과 전부 삭제", "할일 초기화", "전부 완료" → "batch"
   - 목록 → "list_todos"
   - 사용법 → "help"
   - 할일 무관 → "off_topic"
   ⚠️ 사용자가 할일과 관련된 행동을 했다는 말을 하면 절대 off_topic으로 판단하지 말 것.
   "~한테 얘기했어", "~에 보냈어" 등은 해당 할일 완료 의미.

3. **new_recurring (장기/반복 프로젝트):**
   - tasks: 여러 건 동시 등록 배열. 반드시 메시지에 언급된 모든 항목 포함.
   - reminder_time: "HH:MM" (24시간 KST). 미지정 시 "19:00".
   - 예: "매일 7시에 리마인더 해줘 1.A 2.B 3.C" → tasks: [A, B, C], reminder_time: "19:00"

3-1. **bulk_create (대량 복원/등록):**
   - items 배열에 각 할일을 개별 객체로 분리. 반드시 메시지의 모든 항목 포함.
   - 각 item: {{"type": "one_time" 또는 "recurring", "task": "이름", "deadline_iso": "...", "reminder_time": "HH:MM", "project": "태그"}}
   - one_time일 때는 deadline_iso 필수, recurring일 때는 reminder_time 필수.
   - 목록에 "🟢 예정", "🟡", "🔴" 등 마커가 있으면 → type: "one_time", 마커 옆 날짜를 deadline으로.
   - 목록에 "🔄 장기 프로젝트", "매일 HH:MM 리마인더" 등이 있으면 → type: "recurring", 그 시각을 reminder_time으로.
   - "[🏥 팽팽클리닉]" 같은 대괄호 태그 → project 필드에 그대로.

4. **batch (일괄 처리):**
   ⚠️ **batch_filter: "all" 사용 엄격 제한 (매우 중요):**
   - "all" 필터는 사용자가 명시적으로 "모두/전부/싹다/다/초기화" 등을 말했을 때만 사용.
   - "삭제" 한 단어만으로 절대 "all" 필터 사용 금지.
   - 답장 컨텍스트가 있으면 "all" 절대 금지 — 답장한 메시지 안의 항목만 대상.
   - 불확실하면 batch_ids로 명시적으로 대상을 지정.
   - 절대 확인 프롬프트("삭제할까요?", "정말로?") 생성 금지. 이 봇은 확인 기능 없음. 실행 or 거절만.

   - "할일 모두 삭제해줘" / "전부 삭제" / "초기화" → batch_action: "delete", batch_filter: "all"
   - "기한 초과된 것 다 삭제" → batch_action: "delete", batch_filter: "overdue"
   - "반복 프로젝트 전부 삭제" → batch_action: "delete", batch_filter: "recurring"
   - "전부 완료 처리해" → batch_action: "complete", batch_filter: "all"
   - "PNK랑 딜로이트 삭제해" → batch_action: "delete", batch_ids: [해당 id들]
   - batch_filter와 batch_ids 중 하나만 사용. 둘 다 있으면 batch_ids 우선.
   - reply에 처리 결과 미리 작성.

5. **new_todo:** task=핵심만, deadline_iso=절대시각, 파악불가→null
   시간 추론 규칙:
   - 시간 명시 없이 날짜만 → 23:59
   - "점심" → 12:00, "아침" → 09:00, "저녁" → 18:00, "오전" → 11:00, "오후" → 17:00
   - "퇴근 전" → 18:00, "업무시간" → 09:00~18:00
   - "점심 잡자/약속" 등 식사 맥락 → 12:00
   캘린더 등록 규칙 (매우 중요):
   - **add_to_calendar는 기본 false.** 사용자가 명시적으로 요청했을 때만 true.
   - 명시적 요청 예: "캘박", "캘린더에 넣어줘", "캘박해줘", "캘박 부탁", "일정 등록", "캘린더에 추가"
   - 단순히 "미팅 등록해줘" / "회의 예약" 같은 건 false (할일만 등록, 캘린더 X)
   - calendar 필드 (add_to_calendar가 true일 때만 의미 있음):
     - 개인 일정 (점심 약속, 개인 용무, 병원, 가족 등) → "personal"
     - 업무/프로젝트/회사 관련 (미팅, 보고서, 프로젝트명 포함 등) → "work"
     - "개캘/개인캘" → "personal", "회캘/회사캘/그린우드캘" → "work"
     - 판단 어려우면 → "work"

6. **complete_todo:** reply_context 있으면 그 ID. 없으면 메시지에서 키워드로 활성 할일 매칭. todo_id 필수.
   반복 프로젝트에 "그만해", "중단해" → complete_todo로 처리.
   "한동규상무한테 확인했어" → 한동규 관련 할일의 ID를 todo_id에 반드시 포함.
   "더파운더즈에 얘기했어" → 더파운더즈 관련 할일의 ID를 todo_id에 반드시 포함.

7. **modify_todo:** todo_id + deadline_iso 필수. "이번주 토요일" = 이번 주 토요일 23:59.

8. **delete_todo:** todo_id 필수.

9. **reply 톤 (중요):**
   - **항상 존댓말(~요, ~습니다) 사용. 반말 절대 금지.**
   - 간결 3줄. HTML <b><i><code> 가능.
   - **완료/삭제 시 사용자 감정에 맞춰 톤 조절:**
     - "다했다!", "끝났다!" → 축하/격려 ("잘하셨어요!", "고생하셨어요!")
     - "그만해", "더이상 안해", "중단" → 담백하게 종료 ("알겠어요, 빼드릴게요")
     - "취소해", "포기", "안할래" → 가볍게 위로/공감 ("괜찮아요, 정리했어요")
     - "됐어", "이건 빼줘" → 간결하게 확인 ("빼드렸어요!")
   - 무조건 축하하지 말 것. 사용자 메시지의 뉘앙스를 읽고 자연스럽게 반응."""


def build_user_message(text: str, reply_todo: dict | None, reply_to_text: str | None = None) -> str:
    """사용자 메시지에 답장 컨텍스트 주입."""
    parts = []
    if reply_todo:
        parts.append(f"[reply_context: #{reply_todo['id']} - {reply_todo.get('task', '?')}]")
    if reply_to_text:
        # 답장한 원문 (길면 자름)
        excerpt = reply_to_text[:500].replace("\n", " ")
        parts.append(f"[사용자가 아래 메시지에 답장함]\n{excerpt}")
    parts.append(text)
    return "\n".join(parts)


async def _call_gemini(system: str, user_msg: str) -> dict | None:
    """Gemini Flash API 호출."""
    if not GEMINI_KEY:
        return None
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-lite:generateContent?key={GEMINI_KEY}",
                headers={"content-type": "application/json"},
                json={
                    "systemInstruction": {"parts": [{"text": system}]},
                    "contents": [{"parts": [{"text": user_msg}]}],
                    "generationConfig": {
                        "maxOutputTokens": 2000,
                        "responseMimeType": "application/json",
                    },
                },
            )
            if resp.status_code != 200:
                log.error(f"Gemini API {resp.status_code}: {resp.text[:500]}")
                return None
            raw = resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
            raw = re.sub(r"^```json\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw)
            return json.loads(raw)
    except json.JSONDecodeError as e:
        log.error(f"Gemini JSON 파싱 실패: {e}\nraw: {raw[:300]}")
        return None
    except Exception as e:
        log.error(f"Gemini 호출 실패: {e}")
        return None


async def _call_claude(system: str, user_msg: str) -> dict | None:
    """Claude API 호출."""
    if not ANTHROPIC_KEY:
        return None
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": 2000,
                    "system": system,
                    "messages": [{"role": "user", "content": user_msg}],
                },
            )
            if resp.status_code != 200:
                log.error(f"Claude API {resp.status_code}: {resp.text[:500]}")
                return None
            content = resp.json().get("content", [])
            raw = "".join(c.get("text", "") for c in content).strip()
            raw = re.sub(r"^```json\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw)
            return json.loads(raw)
    except json.JSONDecodeError as e:
        log.error(f"Claude JSON 파싱 실패: {e}\nraw: {raw[:300]}")
        return None
    except Exception as e:
        log.error(f"Claude 호출 실패: {e}")
        return None


async def ask_llm(text: str, now: datetime, reply_todo: dict | None = None, reply_to_text: str | None = None) -> dict | None:
    if not GEMINI_KEY and not ANTHROPIC_KEY:
        log.warning("API 키 없음 (GEMINI_API_KEY, ANTHROPIC_API_KEY 둘 다 미설정)")
        return None

    system = build_system_prompt(now)
    user_msg = build_user_message(text, reply_todo, reply_to_text)

    # 1차: Gemini Flash (무료)
    if GEMINI_KEY:
        result = await _call_gemini(system, user_msg)
        if result:
            log.info("[LLM] Gemini Flash 응답 성공")
            return result
        log.warning("[LLM] Gemini 실패 → Claude 폴백 시도")

    # 2차: Claude (유료 폴백)
    if ANTHROPIC_KEY:
        result = await _call_claude(system, user_msg)
        if result:
            log.info("[LLM] Claude 폴백 응답 성공")
            return result

    return None


# ══════════════════════════════════════════════════════════
#  Google Calendar 연동
# ══════════════════════════════════════════════════════════

def gcal_enabled() -> bool:
    return bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET and GCAL_CONFIGS)


def _resolve_cal(cal_key: str | None) -> dict | None:
    """캘린더 키로 설정 조회. 없으면 첫 번째 캘린더 반환."""
    if not GCAL_CONFIGS:
        return None
    if cal_key and cal_key in GCAL_CONFIGS:
        return GCAL_CONFIGS[cal_key]
    return next(iter(GCAL_CONFIGS.values()))


async def _gcal_token(cal_key: str | None = None) -> tuple[str | None, str | None]:
    """Refresh token으로 access token 획득. (token, calendar_id) 반환."""
    cfg = _resolve_cal(cal_key)
    if not cfg or not GOOGLE_CLIENT_ID:
        return None, None
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "client_id": GOOGLE_CLIENT_ID,
                    "client_secret": GOOGLE_CLIENT_SECRET,
                    "refresh_token": cfg["refresh_token"],
                    "grant_type": "refresh_token",
                },
            )
            if resp.status_code == 200:
                return resp.json()["access_token"], cfg["calendar_id"]
            log.error(f"[GCAL] 토큰 갱신 실패: {resp.status_code}")
    except Exception as e:
        log.error(f"[GCAL] 토큰 갱신 오류: {e}")
    return None, None


async def gcal_create(task: str, deadline_iso: str, project: str | None = None, cal_key: str | None = None) -> tuple[str | None, str | None]:
    """캘린더 이벤트 생성 → (event_id, cal_key) 반환."""
    token, cal_id = await _gcal_token(cal_key)
    if not token:
        return None, None
    resolved_key = cal_key if cal_key and cal_key in GCAL_CONFIGS else next(iter(GCAL_CONFIGS))
    try:
        dl = datetime.fromisoformat(deadline_iso)
        event = {
            "summary": task,
            "start": {"dateTime": deadline_iso, "timeZone": "Asia/Seoul"},
            "end": {"dateTime": (dl + timedelta(minutes=30)).isoformat(), "timeZone": "Asia/Seoul"},
            "reminders": {"useDefault": False},
        }
        if project:
            event["description"] = f"📁 {project}"
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"https://www.googleapis.com/calendar/v3/calendars/{cal_id}/events",
                headers={"Authorization": f"Bearer {token}"},
                json=event,
            )
            if resp.status_code == 200:
                eid = resp.json()["id"]
                label = GCAL_CONFIGS[resolved_key]["label"]
                log.info(f"[GCAL] 생성({label}): {task} → {eid}")
                return eid, resolved_key
            log.error(f"[GCAL] 생성 실패: {resp.status_code}: {resp.text[:300]}")
    except Exception as e:
        log.error(f"[GCAL] 생성 오류: {e}")
    return None, None


async def gcal_delete(event_id: str, cal_key: str | None = None) -> bool:
    """캘린더 이벤트 삭제."""
    token, cal_id = await _gcal_token(cal_key)
    if not token or not event_id:
        return False
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.delete(
                f"https://www.googleapis.com/calendar/v3/calendars/{cal_id}/events/{event_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
            if resp.status_code in (200, 204):
                log.info(f"[GCAL] 삭제: {event_id}")
                return True
            log.error(f"[GCAL] 삭제 실패: {resp.status_code}")
    except Exception as e:
        log.error(f"[GCAL] 삭제 오류: {e}")
    return False


async def gcal_update(event_id: str, deadline_iso: str, cal_key: str | None = None) -> bool:
    """캘린더 이벤트 시간 수정."""
    token, cal_id = await _gcal_token(cal_key)
    if not token or not event_id:
        return False
    try:
        dl = datetime.fromisoformat(deadline_iso)
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.patch(
                f"https://www.googleapis.com/calendar/v3/calendars/{cal_id}/events/{event_id}",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "start": {"dateTime": deadline_iso, "timeZone": "Asia/Seoul"},
                    "end": {"dateTime": (dl + timedelta(minutes=30)).isoformat(), "timeZone": "Asia/Seoul"},
                },
            )
            if resp.status_code == 200:
                log.info(f"[GCAL] 수정: {event_id}")
                return True
            log.error(f"[GCAL] 수정 실패: {resp.status_code}")
    except Exception as e:
        log.error(f"[GCAL] 수정 오류: {e}")
    return False


# ══════════════════════════════════════════════════════════
#  유틸
# ══════════════════════════════════════════════════════════

def detect_project(text: str) -> str | None:
    text_lower = text.lower()
    for kw, proj in PROJECT_KEYWORDS.items():
        if kw.lower() in text_lower:
            return proj
    return None


def format_deadline(deadline_iso: str, now: datetime) -> str:
    try:
        dl = datetime.fromisoformat(deadline_iso)
        diff = dl - now
        hours = diff.total_seconds() / 3600
        wday = WEEKDAYS_KR[dl.weekday()]
        ds = f"{dl.month}/{dl.day}({wday}) {dl.strftime('%H:%M')}"
        if hours < 0:
            return f"⚠️ {ds} (기한 초과!)"
        elif hours < 1:
            return f"🔴 {ds} ({int(diff.total_seconds()/60)}분 남음)"
        elif hours < 12:
            return f"🔴 {ds} ({hours:.0f}시간 남음)"
        elif hours < 24:
            return f"🟡 {ds} ({hours:.0f}시간 남음)"
        elif diff.days < 3:
            return f"🟡 {ds} ({diff.days}일 {int(hours%24)}시간 남음)"
        else:
            return f"🟢 {ds} ({diff.days}일 남음)"
    except Exception:
        return deadline_iso


def find_todo_by_id(todo_id: str) -> dict | None:
    # #da4f173a 형태도 처리
    tid = (todo_id or "").lstrip("#")
    for t in STATE["todos"]:
        if t["id"] == tid and t["status"] in ("active", "pending_input"):
            return t
    return None


def fuzzy_match_todos(text: str, todos: list) -> list:
    """메시지에서 활성 할일을 키워드 매칭하여 점수순으로 반환."""
    text_norm = re.sub(r"\s+", "", text.lower())
    matches = []
    for t in todos:
        task = t.get("task", "")
        if not task:
            continue
        # 2글자 이상 한글/영숫자 단어 추출
        words = re.findall(r"[가-힣]{2,}|[a-z0-9]{2,}", task.lower())
        score = 0
        for w in words:
            if w in text_norm:
                score += len(w)
        if score >= 2:
            matches.append((score, t))
    matches.sort(key=lambda x: -x[0])
    return [t for _, t in matches]


# ══════════════════════════════════════════════════════════
#  텔레그램 핸들러
# ══════════════════════════════════════════════════════════

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_html(
        "👋 안녕하세요! <b>할일봇</b>입니다.\n\n"
        "할일을 자유롭게 말씀해주세요.\n"
        "예: <code>내일까지 FDD 보고서 마무리해야돼</code>\n\n"
        "리마인더에 답장으로:\n"
        "• <code>다했다</code> → 완료\n"
        "• <code>금요일까지로 늘려줘</code> → 기한 변경\n"
        "• <code>삭제해</code> → 삭제"
    )

async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await send_summary(ctx.bot, datetime.now(KST), force=True)

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_html(
        "📋 <b>할일봇 사용법</b>\n\n"
        "✏️ <b>등록:</b> 자유롭게 말하기\n"
        "✅ <b>완료:</b> 리마인더 답장 → <code>다했다</code> / <code>이건 됐어</code>\n"
        "📅 <b>기한변경:</b> 리마인더 답장 → <code>금요일까지로 늘려줘</code>\n"
        "🗑️ <b>삭제:</b> 리마인더 답장 → <code>삭제해</code>\n\n"
        "🔄 <b>일괄:</b>\n"
        "  <code>할일 모두 삭제</code> / <code>기한 초과 전부 삭제</code>\n"
        "  <code>PNK랑 딜로이트 완료 처리</code>\n\n"
        "📋 /list — 목록  ❓ /help — 도움말"
    )


async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.text:
        return
    if msg.chat_id != CHAT_ID:
        return

    text = msg.text.strip()
    now = datetime.now(KST)

    # ── 답장 컨텍스트 조회 ──
    reply_todo = resolve_reply_context(msg)
    reply_to_text = None
    if msg.reply_to_message:
        reply_to_text = msg.reply_to_message.text or msg.reply_to_message.caption
        if reply_todo:
            log.info(f"[REPLY_CTX] #{reply_todo['id']} - {reply_todo.get('task', '?')}")
        elif reply_to_text:
            log.info(f"[REPLY_TEXT] {reply_to_text[:60]}")

    # ── LLM 분석 ──
    result = await ask_llm(text, now, reply_todo=reply_todo, reply_to_text=reply_to_text)
    if not result:
        if not GEMINI_KEY and not ANTHROPIC_KEY:
            await msg.reply_html("🤖 API 키가 설정되지 않았어요. <code>GEMINI_API_KEY</code> 또는 <code>ANTHROPIC_API_KEY</code> 환경변수를 확인해주세요.")
        else:
            await msg.reply_html("🤖 AI 응답을 받지 못했어요. 잠시 후 다시 시도해주세요.\n<i>(서버 로그에서 원인을 확인해주세요)</i>")
        return

    intent = result.get("intent", "off_topic")
    reply = result.get("reply", "")
    todo_id = result.get("todo_id")
    log.info(f"[LLM] intent={intent} todo_id={todo_id} | {text[:60]}")

    # ── new_todo ──
    if intent == "new_todo":
        task = result.get("task")
        deadline_iso = result.get("deadline_iso")
        project = detect_project(text)
        has_all = bool(task and deadline_iso)

        todo = {
            "id": str(uuid.uuid4())[:8],
            "task": task,
            "deadline": deadline_iso,
            "deadline_display": result.get("deadline_raw"),
            "project": project,
            "status": "active" if has_all else "pending_input",
            "created_at": now.isoformat(),
            "last_reminded_at": None,
            "last_daily_date": None,
            "original_message": text,
        }
        # 캘린더 연동 (명시적 요청 시에만)
        cal_key = result.get("calendar")
        if has_all and gcal_enabled() and result.get("add_to_calendar"):
            eid, used_cal = await gcal_create(task, deadline_iso, project, cal_key)
            if eid:
                todo["gcal_event_id"] = eid
                todo["gcal_cal_key"] = used_cal

        STATE["todos"].append(todo)
        persist()

        lines = [reply] if reply else ["📝 등록했습니다."]
        if task:
            lines.append(f"\n📌 <b>{task}</b>")
        if deadline_iso:
            lines.append(f"⏰ {format_deadline(deadline_iso, now)}")
        if project:
            lines.append(f"📁 {project}")
        if todo.get("gcal_event_id"):
            cal_label = GCAL_CONFIGS.get(todo.get("gcal_cal_key", ""), {}).get("label", "")
            lines.append(f"📅 구글 캘린더에 추가됨 ({cal_label})" if cal_label else "📅 구글 캘린더에 추가됨")
        if not has_all:
            missing = []
            if not task: missing.append("할일 내용")
            if not deadline_iso: missing.append("데드라인")
            lines.append(f"\n⚠️ {', '.join(missing)}이 부족해요. 알려주시면 업데이트할게요.")
        await msg.reply_html("\n".join(lines))
        log.info(f"[NEW] {task} → {deadline_iso}")

    # ── new_recurring ──
    elif intent == "new_recurring":
        tasks = result.get("tasks") or []
        reminder_time = result.get("reminder_time", "19:00")
        if not tasks:
            # tasks가 비어있으면 task 필드에서 단건 처리
            single = result.get("task")
            if single:
                tasks = [{"task": single}]

        if not tasks:
            await msg.reply_html(reply or "🤔 등록할 항목을 알려주세요.")
        else:
            created = []
            for item in tasks:
                task_name = item.get("task", "")
                if not task_name:
                    continue
                project = detect_project(task_name) or detect_project(text)
                todo = {
                    "id": str(uuid.uuid4())[:8],
                    "type": "recurring",
                    "task": task_name,
                    "reminder_time": reminder_time,
                    "deadline": None,
                    "project": project,
                    "status": "active",
                    "created_at": now.isoformat(),
                    "last_reminded_at": None,
                    "last_daily_date": None,
                    "original_message": text,
                }
                STATE["todos"].append(todo)
                created.append(todo)

            persist()

            lines = [reply] if reply else [f"🔄 <b>{len(created)}건 반복 프로젝트 등록!</b>"]
            lines.append(f"⏰ 매일 {reminder_time} KST 리마인더\n")
            for t in created:
                proj = f" [{t['project']}]" if t.get("project") else ""
                lines.append(f"  📌 {t['task']}{proj}")
            lines.append(f"\n<i>중단하려면 리마인더에 답장 → '그만해'</i>")
            await msg.reply_html("\n".join(lines))
            log.info(f"[NEW_RECURRING] {len(created)}건 | {reminder_time}")

    # ── bulk_create (대량 복원/등록) ──
    elif intent == "bulk_create":
        items = result.get("items") or []
        if not items:
            await msg.reply_html(reply or "🤔 등록할 항목을 알려주세요.")
        else:
            created_one = []
            created_rec = []
            # 헤더성 단어 필터 (bulk_create에서 목록 헤더가 task로 잘못 들어오는 것 방지)
            header_blacklist = {"장기 프로젝트", "장기프로젝트", "예정", "오늘 마감", "기한 초과", "활성 할일", "반복"}
            for item in items:
                task_name = item.get("task", "").strip()
                if not task_name or task_name in header_blacklist:
                    continue
                # task 안에 [프로젝트] 형태 태그가 박혀있으면 분리
                tag_match = re.search(r"\s*\[([^\]]+)\]\s*$", task_name)
                extracted_project = None
                if tag_match:
                    extracted_project = tag_match.group(1).strip()
                    task_name = task_name[:tag_match.start()].strip()
                item_type = item.get("type", "one_time")
                project = item.get("project") or extracted_project or detect_project(task_name)

                if item_type == "recurring":
                    todo = {
                        "id": str(uuid.uuid4())[:8],
                        "type": "recurring",
                        "task": task_name,
                        "reminder_time": item.get("reminder_time") or "19:00",
                        "deadline": None,
                        "project": project,
                        "status": "active",
                        "created_at": now.isoformat(),
                        "last_reminded_at": None,
                        "last_daily_date": None,
                        "original_message": text[:200],
                    }
                    STATE["todos"].append(todo)
                    created_rec.append(todo)
                else:
                    deadline_iso = item.get("deadline_iso")
                    if not deadline_iso:
                        continue
                    todo = {
                        "id": str(uuid.uuid4())[:8],
                        "task": task_name,
                        "deadline": deadline_iso,
                        "project": project,
                        "status": "active",
                        "created_at": now.isoformat(),
                        "last_reminded_at": None,
                        "last_daily_date": None,
                        "original_message": text[:200],
                    }
                    # 캘린더는 명시적 요청 시에만 (bulk_create에서는 등록 X)
                    STATE["todos"].append(todo)
                    created_one.append(todo)

            persist()

            total = len(created_one) + len(created_rec)
            lines = [reply] if reply else [f"✅ <b>{total}건 등록 완료</b>"]
            if created_one:
                lines.append(f"\n📌 1회성 ({len(created_one)}건):")
                for t in created_one:
                    dl = format_deadline(t["deadline"], now) if t.get("deadline") else ""
                    proj = f" [{t['project']}]" if t.get("project") else ""
                    lines.append(f"  • {t['task']}{proj}\n    {dl}")
            if created_rec:
                lines.append(f"\n🔄 반복 ({len(created_rec)}건):")
                for t in created_rec:
                    proj = f" [{t['project']}]" if t.get("project") else ""
                    lines.append(f"  • {t['task']}{proj} (매일 {t['reminder_time']})")
            await msg.reply_html("\n".join(lines))
            log.info(f"[BULK_CREATE] 1회성 {len(created_one)}건 + 반복 {len(created_rec)}건")

    # ── complete_todo ──
    elif intent == "complete_todo":
        matched = None
        if todo_id:
            matched = find_todo_by_id(todo_id)
        if not matched and reply_todo:
            matched = reply_todo
        active = [t for t in STATE["todos"] if t["status"] == "active"]
        # 퍼지 매칭: 메시지에서 키워드로 활성 할일 찾기
        if not matched:
            fuzzy = fuzzy_match_todos(text, active)
            if len(fuzzy) == 1:
                matched = fuzzy[0]
                log.info(f"[FUZZY] complete_todo 퍼지 매칭: {matched.get('task')}")
            elif len(fuzzy) > 1:
                # 여러 개 후보 → 사용자에게 확인 요청
                items = "\n".join(f"  • {t.get('task', '?')}" for t in fuzzy)
                await msg.reply_html(f"🤔 여러 개가 매칭돼요. 어떤 거?\n\n{items}")
                return
        if not matched and len(active) == 1:
            matched = active[0]

        if matched:
            matched["status"] = "done"
            matched["done_at"] = now.isoformat()
            if matched.get("gcal_event_id"):
                await gcal_delete(matched["gcal_event_id"], matched.get("gcal_cal_key"))
            persist()
            await msg.reply_html(reply or f"✅ <b>{matched.get('task', '할일')}</b> 완료!")
            log.info(f"[DONE] {matched.get('task')}")
        else:
            # 매칭 실패 → LLM의 성공 응답 무시, 명확한 에러 표시
            if active:
                items = "\n".join(f"  • {t.get('task', '?')}" for t in active)
                await msg.reply_html(f"🤔 어떤 할일인지 못 찾았어요. 더 구체적으로 말씀해주세요.\n\n<b>활성 할일:</b>\n{items}")
            else:
                await msg.reply_html("📭 활성 할일 없음.")

    # ── modify_todo ──
    elif intent == "modify_todo":
        matched = None
        new_deadline = result.get("deadline_iso")
        new_task = result.get("new_task")
        if todo_id:
            matched = find_todo_by_id(todo_id)
        if not matched and reply_todo:
            matched = reply_todo
        # pending_input도 매칭 대상에 포함
        active = [t for t in STATE["todos"] if t["status"] in ("active", "pending_input")]
        if not matched:
            fuzzy = fuzzy_match_todos(text, active)
            if len(fuzzy) == 1:
                matched = fuzzy[0]
                log.info(f"[FUZZY] modify_todo 퍼지 매칭: {matched.get('task')}")
        # pending_input이 단 1건이면 자동 매칭
        if not matched:
            pending = [t for t in STATE["todos"] if t["status"] == "pending_input"]
            if len(pending) == 1:
                matched = pending[0]
                log.info(f"[AUTO_PENDING] modify_todo 자동 매칭: {matched.get('task')}")
        if not matched and len(active) == 1:
            matched = active[0]

        if matched and (new_deadline or new_task):
            if new_task:
                matched["task"] = new_task
            if new_deadline:
                matched["deadline"] = new_deadline
                matched["deadline_display"] = result.get("deadline_raw")
                matched["last_reminded_at"] = None
                matched["last_daily_date"] = None
            # pending_input → active 승격 (task와 deadline 둘 다 있을 때)
            if matched.get("status") == "pending_input" and matched.get("task") and matched.get("deadline"):
                matched["status"] = "active"
                log.info(f"[PROMOTE] pending_input → active: {matched.get('task')}")
            if new_deadline and matched.get("gcal_event_id"):
                await gcal_update(matched["gcal_event_id"], new_deadline, matched.get("gcal_cal_key"))
            elif new_deadline and result.get("add_to_calendar") and gcal_enabled():
                eid, used_cal = await gcal_create(
                    matched.get("task", ""), new_deadline, matched.get("project"), result.get("calendar")
                )
                if eid:
                    matched["gcal_event_id"] = eid
                    matched["gcal_cal_key"] = used_cal
            persist()
            dl_str = format_deadline(new_deadline, now) if new_deadline else ""
            await msg.reply_html(reply or f"📅 <b>{matched.get('task')}</b> 업데이트!\n{dl_str}".strip())
            log.info(f"[MODIFY] {matched.get('task')} → task={new_task}, deadline={new_deadline}")
        elif matched and not new_deadline:
            # 기한 변경 없이 "캘박해줘"만 요청한 경우
            if result.get("add_to_calendar") and gcal_enabled() and matched.get("deadline"):
                if matched.get("gcal_event_id"):
                    await msg.reply_html(reply or "📅 이미 캘린더에 등록되어 있어요.")
                else:
                    eid, used_cal = await gcal_create(
                        matched.get("task", ""),
                        matched["deadline"],
                        matched.get("project"),
                        result.get("calendar"),
                    )
                    if eid:
                        matched["gcal_event_id"] = eid
                        matched["gcal_cal_key"] = used_cal
                        persist()
                        label = GCAL_CONFIGS.get(used_cal, {}).get("label", "")
                        await msg.reply_html(reply or f"📅 <b>{matched.get('task')}</b> 캘린더에 추가됨 ({label})")
                        log.info(f"[GCAL_ADD] {matched.get('task')}")
                    else:
                        await msg.reply_html("❌ 캘린더 등록 실패. 로그를 확인해주세요.")
            else:
                await msg.reply_html(reply or "📅 새 기한을 알려주세요. 예: <code>금요일까지</code>")
        else:
            if active:
                items = "\n".join(f"  • {t.get('task', '?')}" for t in active)
                await msg.reply_html(reply or f"🤔 어떤 할일?\n리마인더에 답장으로 알려주세요.\n\n{items}")
            else:
                await msg.reply_html("📭 활성 할일 없음.")

    # ── delete_todo ──
    elif intent == "delete_todo":
        matched = None
        if todo_id:
            matched = find_todo_by_id(todo_id)
        if not matched and reply_todo:
            matched = reply_todo
        active = [t for t in STATE["todos"] if t["status"] == "active"]
        if not matched:
            fuzzy = fuzzy_match_todos(text, active)
            if len(fuzzy) == 1:
                matched = fuzzy[0]
                log.info(f"[FUZZY] delete_todo 퍼지 매칭: {matched.get('task')}")
            elif len(fuzzy) > 1:
                items = "\n".join(f"  • {t.get('task', '?')}" for t in fuzzy)
                await msg.reply_html(f"🤔 여러 개가 매칭돼요. 어떤 거?\n\n{items}")
                return

        if matched:
            matched["status"] = "deleted"
            matched["done_at"] = now.isoformat()
            if matched.get("gcal_event_id"):
                await gcal_delete(matched["gcal_event_id"], matched.get("gcal_cal_key"))
            persist()
            await msg.reply_html(reply or f"🗑️ <b>{matched.get('task')}</b> 삭제!")
            log.info(f"[DELETE] {matched.get('task')}")
        else:
            if active:
                items = "\n".join(f"  • {t.get('task', '?')}" for t in active)
                await msg.reply_html(f"🤔 어떤 할일인지 못 찾았어요.\n\n{items}")
            else:
                await msg.reply_html("📭 활성 할일 없음.")

    # ── batch (일괄 처리) ──
    elif intent == "batch":
        batch_action = result.get("batch_action", "delete")
        batch_ids = result.get("batch_ids") or []
        batch_filter = result.get("batch_filter")
        new_deadline = result.get("deadline_iso")

        active = [t for t in STATE["todos"] if t["status"] in ("active", "pending_input")]

        # 대상 결정
        targets = []
        if batch_ids:
            for bid in batch_ids:
                t = find_todo_by_id(bid)
                if t:
                    targets.append(t)
        elif batch_filter == "all":
            # ⚠️ 안전장치: batch_filter "all"은 사용자 메시지에 명시적 키워드가 있을 때만 허용
            explicit_all_keywords = ["모두", "전부", "싹다", "싹", "초기화", "리셋", "reset", "clear"]
            has_explicit = any(kw in text for kw in explicit_all_keywords)
            # 답장 컨텍스트가 있으면 "all" 금지
            if msg.reply_to_message:
                log.warning(f"[BATCH_SAFETY] 답장 상태에서 filter 'all' 차단: {text[:60]}")
                await msg.reply_html(
                    "⚠️ 답장으로 전체 삭제는 못 해요. 특정 할일을 지정하거나, 새 메시지로 명확하게 요청해주세요."
                )
                return
            if not has_explicit:
                log.warning(f"[BATCH_SAFETY] 명시적 키워드 없어 filter 'all' 차단: {text[:60]}")
                await msg.reply_html(
                    "⚠️ 전체 " + ("삭제" if batch_action == "delete" else "처리") +
                    "는 위험해서 명확한 표현이 필요해요. '모두', '전부', '초기화' 같은 키워드를 써주세요."
                )
                return
            # 완료 처리 시 반복 프로젝트는 제외
            if batch_action == "complete":
                targets = [t for t in active if t.get("type") != "recurring"]
            else:
                targets = active
        elif batch_filter == "overdue":
            targets = [t for t in active if t.get("deadline") and datetime.fromisoformat(t["deadline"]) < now]
        elif batch_filter == "recurring":
            targets = [t for t in active if t.get("type") == "recurring"]

        if not targets:
            await msg.reply_html(reply or "🤔 대상 할일을 찾지 못했어요.")
        else:
            names = []
            for t in targets:
                if batch_action == "delete":
                    t["status"] = "deleted"
                    t["done_at"] = now.isoformat()
                    if t.get("gcal_event_id"):
                        await gcal_delete(t["gcal_event_id"], t.get("gcal_cal_key"))
                elif batch_action == "complete":
                    t["status"] = "done"
                    t["done_at"] = now.isoformat()
                    if t.get("gcal_event_id"):
                        await gcal_delete(t["gcal_event_id"], t.get("gcal_cal_key"))
                elif batch_action == "modify" and new_deadline:
                    t["deadline"] = new_deadline
                    t["deadline_display"] = result.get("deadline_raw")
                    t["last_reminded_at"] = None
                    t["last_daily_date"] = None
                names.append(t.get("task", "?"))
            persist()

            action_emoji = {"delete": "🗑️", "complete": "✅", "modify": "📅"}.get(batch_action, "✅")
            action_word = {"delete": "삭제", "complete": "완료", "modify": "기한 변경"}.get(batch_action, "처리")
            items_str = "\n".join(f"  • {n}" for n in names)
            await msg.reply_html(
                reply or f"{action_emoji} <b>{len(targets)}건 {action_word}</b>\n\n{items_str}"
            )
            log.info(f"[BATCH] {batch_action} {len(targets)}건")

    # ── list_todos ──
    elif intent == "list_todos":
        await send_summary(ctx.bot, now, force=True)

    elif intent == "help":
        await msg.reply_html(reply or "자유롭게 할일을 말씀해주세요!")

    else:
        await msg.reply_html(reply or "저는 할일 관리 전문 봇이에요 😊")


# ══════════════════════════════════════════════════════════
#  리마인더 (JobQueue)
# ══════════════════════════════════════════════════════════

def get_reminder_interval(deadline_iso: str, now: datetime) -> float | None:
    try:
        remaining = (datetime.fromisoformat(deadline_iso) - now).total_seconds() / 3600
        if remaining <= 0: return 3.0
        elif remaining <= 12: return 3.0
        elif remaining <= 24: return 4.0
        else: return None
    except Exception:
        return None


async def reminder_check(ctx: ContextTypes.DEFAULT_TYPE):
    now = datetime.now(KST)

    # ── 야간 무음 (0시~6시 KST) ──
    if now.hour < 6:
        return

    today = now.strftime("%Y-%m-%d")

    # ── 1단계: 보낼 리마인더 수집 ──
    recurring_due = []
    deadline_due = []
    pending_due = []

    for todo in STATE["todos"]:
        # 반복 프로젝트
        if todo["status"] == "active" and todo.get("type") == "recurring" and todo.get("reminder_time"):
            try:
                r_hour, r_min = map(int, todo["reminder_time"].split(":"))
                reminder_dt = now.replace(hour=r_hour, minute=r_min, second=0, microsecond=0)
                if now >= reminder_dt and todo.get("last_daily_date") != today:
                    recurring_due.append(todo)
            except (ValueError, AttributeError):
                continue

        # 1회성 데드라인
        elif todo["status"] == "active" and todo.get("deadline") and todo.get("type") != "recurring":
            interval = get_reminder_interval(todo["deadline"], now)
            if interval is None:
                if now.hour < 7 or now.hour > 9:
                    continue
                if todo.get("last_daily_date") == today:
                    continue
            else:
                if todo.get("last_reminded_at"):
                    elapsed = (now - datetime.fromisoformat(todo["last_reminded_at"])).total_seconds() / 3600
                    if elapsed < interval - 0.1:
                        continue
            deadline_due.append(todo)

        # 미완성 입력
        elif todo["status"] == "pending_input":
            if 10 < now.minute < 50:
                continue
            if todo.get("last_reminded_at"):
                if (now - datetime.fromisoformat(todo["last_reminded_at"])).total_seconds() < 3000:
                    continue
            pending_due.append(todo)

    # ── 2단계: 통합 발송 ──

    # 반복 프로젝트 → 1개 메시지로 통합
    if recurring_due:
        lines = ["🔄 <b>장기 프로젝트 리마인더</b>\n"]
        for i, todo in enumerate(recurring_due, 1):
            proj = f"  <i>{todo['project']}</i>" if todo.get("project") else ""
            lines.append(f"  {i}. {todo['task']}{proj}")
        rt = recurring_due[0].get("reminder_time", "19:00")
        lines.append(f"\n⏰ 매일 {rt} 반복 · {len(recurring_due)}건")
        lines.append(f"\n<i>↩️ 답장: '1번 다했다' / '헬스케어 그만해'</i>")

        sent = await ctx.bot.send_message(
            chat_id=CHAT_ID, text="\n".join(lines), parse_mode="HTML",
        )
        for todo in recurring_due:
            todo["last_reminded_at"] = now.isoformat()
            todo["last_daily_date"] = today
        persist()
        log.info(f"[REMIND_RECURRING] {len(recurring_due)}건 통합 발송")

    # 데드라인 할일 → 1건이면 개별, 2건 이상이면 통합
    if deadline_due:
        if len(deadline_due) == 1:
            todo = deadline_due[0]
            dl_str = format_deadline(todo["deadline"], now)
            proj = f"\n📁 {todo['project']}" if todo.get("project") else ""
            sent = await ctx.bot.send_message(
                chat_id=CHAT_ID,
                text=(
                    f"⏰ <b>리마인더</b>\n\n"
                    f"📌 {todo['task']}\n"
                    f"⏳ {dl_str}{proj}\n\n"
                    f"<i>↩️ 답장: '다했다' / '금요일까지'</i>"
                ),
                parse_mode="HTML",
            )
            todo["last_reminded_at"] = now.isoformat()
            todo["last_daily_date"] = today
            STATE.setdefault("reminder_msg_map", {})[str(sent.message_id)] = todo["id"]
            persist()
            log.info(f"[REMIND] {todo['task']}")
        else:
            # 다건 통합
            deadline_due.sort(key=lambda t: datetime.fromisoformat(t["deadline"]))
            lines = [f"⏰ <b>리마인더</b> ({len(deadline_due)}건)\n"]
            for todo in deadline_due:
                dl_str = format_deadline(todo["deadline"], now)
                proj = f" [{todo['project']}]" if todo.get("project") else ""
                lines.append(f"  📌 {todo['task']}{proj}\n    {dl_str}")
            lines.append(f"\n<i>↩️ 답장: '증권거래세 다했다' / '코엑스 금요일까지'</i>")

            sent = await ctx.bot.send_message(
                chat_id=CHAT_ID, text="\n".join(lines), parse_mode="HTML",
            )
            for todo in deadline_due:
                todo["last_reminded_at"] = now.isoformat()
                todo["last_daily_date"] = today
            persist()
            log.info(f"[REMIND] {len(deadline_due)}건 통합 발송")

    # 미완성 입력
    for todo in pending_due:
        missing = []
        if not todo.get("task"): missing.append("할일 내용")
        if not todo.get("deadline"): missing.append("데드라인")
        await ctx.bot.send_message(
            chat_id=CHAT_ID, parse_mode="HTML",
            text=f"⚠️ <b>입력 미완성</b>\n원본: <code>{todo.get('original_message', '?')}</code>\n부족: {', '.join(missing)}",
        )
        todo["last_reminded_at"] = now.isoformat()
        persist()


async def daily_summary(ctx: ContextTypes.DEFAULT_TYPE):
    await send_summary(ctx.bot, datetime.now(KST), force=True)


async def send_summary(bot, now: datetime, force: bool = False):
    active = [t for t in STATE["todos"] if t["status"] in ("active", "pending_input")]
    if not active:
        if force:
            await bot.send_message(chat_id=CHAT_ID, text="📭 활성 할일 없음. 한가하시네요! 😎")
        return

    one_time = [t for t in active if t.get("type") != "recurring"]
    recurring = [t for t in active if t.get("type") == "recurring"]

    one_time.sort(key=lambda t: datetime.fromisoformat(t["deadline"]) if t.get("deadline") else datetime.max.replace(tzinfo=KST))
    wday = WEEKDAYS_KR[now.weekday()]
    lines = [f"📋 <b>오늘의 할일 ({now.month}/{now.day} {wday})</b>\n"]

    if one_time:
        overdue = [t for t in one_time if t.get("deadline") and datetime.fromisoformat(t["deadline"]) < now]
        today_end = now.replace(hour=23, minute=59, second=59)
        today_due = [t for t in one_time if t.get("deadline") and now <= datetime.fromisoformat(t["deadline"]) <= today_end and t not in overdue]
        upcoming = [t for t in one_time if t not in overdue and t not in today_due]

        for label, group in [("🔴 기한 초과", overdue), ("🟡 오늘 마감", today_due), ("🟢 예정", upcoming)]:
            if not group: continue
            lines.append(f"<b>{label}:</b>")
            for t in group:
                proj = f" [{t['project']}]" if t.get("project") else ""
                task = t.get("task", "(미입력)")
                dl = format_deadline(t["deadline"], now) if t.get("deadline") else "⚠️ 데드라인 미설정"
                lines.append(f"  • {task}{proj}\n    {dl}")
            lines.append("")

    if recurring:
        lines.append(f"<b>🔄 장기 프로젝트 ({len(recurring)}건):</b>")
        for t in recurring:
            proj = f" [{t['project']}]" if t.get("project") else ""
            task = t.get("task", "(미입력)")
            rt = t.get("reminder_time", "19:00")
            lines.append(f"  • {task}{proj}\n    매일 {rt} 리마인더")
        lines.append("")

    lines.append(f"총 {len(active)}건 (1회성 {len(one_time)} + 반복 {len(recurring)}) | /list")
    await bot.send_message(chat_id=CHAT_ID, text="\n".join(lines), parse_mode="HTML")


async def cleanup_job(ctx: ContextTypes.DEFAULT_TYPE):
    now = datetime.now(KST)
    cutoff = now - timedelta(days=7)
    STATE["todos"] = [
        t for t in STATE["todos"]
        if not (t["status"] in ("done", "deleted") and t.get("done_at") and datetime.fromisoformat(t["done_at"]) < cutoff)
    ]
    if len(STATE.get("reminder_msg_map", {})) > 200:
        ids = {t["id"] for t in STATE["todos"]}
        STATE["reminder_msg_map"] = {k: v for k, v in STATE["reminder_msg_map"].items() if v in ids}
    persist()


# ══════════════════════════════════════════════════════════
#  메인
# ══════════════════════════════════════════════════════════

def main():
    log.info("=== 할일봇 v2 시작 ===")
    llm_status = []
    if GEMINI_KEY: llm_status.append("Gemini(주)")
    if ANTHROPIC_KEY: llm_status.append("Claude(폴백)")
    gcal_info = ', '.join(GCAL_CONFIGS.get(k, {}).get("label", k) for k in GCAL_CONFIGS) if GCAL_CONFIGS else "비활성"
    log.info(f"CHAT_ID: {CHAT_ID} | LLM: {', '.join(llm_status) or '비활성'} | GCal: {gcal_info}")
    # GCal 진단
    log.info(f"[GCAL_DIAG] CLIENT_ID={'O' if GOOGLE_CLIENT_ID else 'X'} | CLIENT_SECRET={'O' if GOOGLE_CLIENT_SECRET else 'X'} | CONFIGS={list(GCAL_CONFIGS.keys()) or 'empty'}")
    log.info(f"[GCAL_DIAG] env GOOGLE_REFRESH_TOKEN={'O' if os.environ.get('GOOGLE_REFRESH_TOKEN') else 'X'} | GOOGLE_CAL_PERSONAL_REFRESH_TOKEN={'O' if os.environ.get('GOOGLE_CAL_PERSONAL_REFRESH_TOKEN') else 'X'} | GOOGLE_CAL_WORK_REFRESH_TOKEN={'O' if os.environ.get('GOOGLE_CAL_WORK_REFRESH_TOKEN') else 'X'}")
    log.info(f"활성 할일: {len([t for t in STATE['todos'] if t['status'] == 'active'])}건")

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    jq = app.job_queue
    jq.run_repeating(reminder_check, interval=300, first=30)
    jq.run_daily(daily_summary, time=time(hour=22, minute=0, tzinfo=timezone.utc))
    jq.run_daily(cleanup_job, time=time(hour=15, minute=0, tzinfo=timezone.utc))

    log.info("Long polling 시작...")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)


if __name__ == "__main__":
    main()
