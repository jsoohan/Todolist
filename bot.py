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
    if active:
        lines = []
        for t in active:
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

    return f"""너는 할일 관리 전문 텔레그램 봇 "할일봇"이야.
할일 등록, 완료, 기한 변경, 삭제, 일괄 처리, 목록 확인, 장기/반복 프로젝트 관리를 담당해. 그 외 요청은 정중하게 거절.

현재 시각: {now.strftime('%Y-%m-%d %H:%M')} KST ({WEEKDAYS_KR[now.weekday()]}요일)
{todo_ctx}

반드시 아래 JSON만 출력. JSON 외 텍스트 절대 금지.

{{
  "intent": "new_todo" | "new_recurring" | "complete_todo" | "modify_todo" | "delete_todo" | "batch" | "list_todos" | "help" | "off_topic",
  "task": "할일 내용 (new_todo)",
  "tasks": [{{"task": "할일1"}}, {{"task": "할일2"}}],
  "reminder_time": "HH:MM (KST, new_recurring)",
  "deadline_raw": "데드라인 원문 (new_todo, modify_todo)",
  "deadline_iso": "YYYY-MM-DDTHH:MM:SS+09:00 또는 null",
  "todo_id": "대상 #id 또는 null (단건 처리)",
  "batch_action": "complete" | "delete" | "modify" (batch일 때),
  "batch_ids": ["id1", "id2"] (batch일 때 대상 ID 배열),
  "batch_filter": "all" | "overdue" | "recurring" | null (batch일 때 필터),
  "reply": "한국어 답변"
}}

핵심 규칙:

1. **reply_context 처리 (가장 중요):**
   [reply_context: #ID - 할일이름] → 사용자가 특정 리마인더에 답장한 것.
   "이건", "이거", 주어 없는 문장 → reply_context의 할일.
   반드시 todo_id에 해당 ID.

2. **intent 판단:**
   - 새 할일 (1회성) → "new_todo"
   - **장기/반복 프로젝트**: "매일 리마인더", "장기 프로젝트", "계속 알려줘", "그만할때까지" → "new_recurring"
   - 완료: "다했다", "끝", "더 안봐도 돼", "됐어", "그만해", "중단해" → "complete_todo"
   - 기한 변경: "늘려줘", "연장", "기한 변경" → "modify_todo"
   - 삭제 단건: "삭제해", "취소해" → "delete_todo"
   - **일괄 처리**: "모두/전부/다 삭제", "기한 초과 전부 삭제", "할일 초기화", "전부 완료" → "batch"
   - 목록 → "list_todos"
   - 사용법 → "help"
   - 할일 무관 → "off_topic"

3. **new_recurring (장기/반복 프로젝트):**
   - tasks: 여러 건 동시 등록 배열. 반드시 메시지에 언급된 모든 항목 포함.
   - reminder_time: "HH:MM" (24시간 KST). 미지정 시 "19:00".
   - 예: "매일 7시에 리마인더 해줘 1.A 2.B 3.C" → tasks: [A, B, C], reminder_time: "19:00"

4. **batch (일괄 처리):**
   - "할일 모두 삭제해줘" / "전부 삭제" / "초기화" → batch_action: "delete", batch_filter: "all"
   - "기한 초과된 것 다 삭제" → batch_action: "delete", batch_filter: "overdue"
   - "반복 프로젝트 전부 삭제" → batch_action: "delete", batch_filter: "recurring"
   - "전부 완료 처리해" → batch_action: "complete", batch_filter: "all"
   - "PNK랑 딜로이트 삭제해" → batch_action: "delete", batch_ids: [해당 id들]
   - batch_filter와 batch_ids 중 하나만 사용. 둘 다 있으면 batch_ids 우선.
   - reply에 처리 결과 미리 작성.

5. **new_todo:** task=핵심만, deadline_iso=절대시각(미지정→23:59), 파악불가→null

6. **complete_todo:** reply_context 있으면 그 ID. 없으면 메시지로 매칭. 1개면 자동.
   반복 프로젝트에 "그만해", "중단해" → complete_todo로 처리.

7. **modify_todo:** todo_id + deadline_iso 필수. "이번주 토요일" = 이번 주 토요일 23:59.

8. **delete_todo:** todo_id 필수.

9. **reply 톤 (중요):**
   - 반말/존댓말은 사용자에 맞춤. 간결 3줄. HTML <b><i><code> 가능.
   - **완료/삭제 시 사용자 감정에 맞춰 톤 조절:**
     - "다했다!", "끝났다!" → 축하/격려 톤 ("잘했어!", "고생했어!")
     - "그만해", "더이상 안해", "중단" → 담백하게 종료 ("알겠어, 빼줄게")
     - "취소해", "포기", "안할래" → 가볍게 위로/공감 ("괜찮아, 정리했어")
     - "됐어", "이건 빼줘" → 간결하게 확인 ("빼줬어!")
   - 무조건 축하하지 말 것. 사용자 메시지의 뉘앙스를 읽고 자연스럽게 반응."""


def build_user_message(text: str, reply_todo: dict | None) -> str:
    """사용자 메시지에 답장 컨텍스트 주입."""
    if reply_todo:
        return f"[reply_context: #{reply_todo['id']} - {reply_todo.get('task', '?')}]\n{text}"
    return text


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
                        "maxOutputTokens": 500,
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
                    "max_tokens": 500,
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


async def ask_llm(text: str, now: datetime, reply_todo: dict | None = None) -> dict | None:
    if not GEMINI_KEY and not ANTHROPIC_KEY:
        log.warning("API 키 없음 (GEMINI_API_KEY, ANTHROPIC_API_KEY 둘 다 미설정)")
        return None

    system = build_system_prompt(now)
    user_msg = build_user_message(text, reply_todo)

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
    for t in STATE["todos"]:
        if t["id"] == todo_id and t["status"] in ("active", "pending_input"):
            return t
    return None


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
    if reply_todo:
        log.info(f"[REPLY_CTX] #{reply_todo['id']} - {reply_todo.get('task', '?')}")

    # ── LLM 분석 ──
    result = await ask_llm(text, now, reply_todo=reply_todo)
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
        STATE["todos"].append(todo)
        persist()

        lines = [reply] if reply else ["📝 등록했습니다."]
        if task:
            lines.append(f"\n📌 <b>{task}</b>")
        if deadline_iso:
            lines.append(f"⏰ {format_deadline(deadline_iso, now)}")
        if project:
            lines.append(f"📁 {project}")
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

    # ── complete_todo ──
    elif intent == "complete_todo":
        matched = None
        if todo_id:
            matched = find_todo_by_id(todo_id)
        if not matched and reply_todo:
            matched = reply_todo
        active = [t for t in STATE["todos"] if t["status"] == "active"]
        if not matched and len(active) == 1:
            matched = active[0]

        if matched:
            matched["status"] = "done"
            matched["done_at"] = now.isoformat()
            persist()
            await msg.reply_html(reply or f"✅ <b>{matched.get('task', '할일')}</b> 완료!")
            log.info(f"[DONE] {matched.get('task')}")
        else:
            if active:
                items = "\n".join(f"  • {t.get('task', '?')}" for t in active)
                await msg.reply_html(reply or f"🤔 어떤 할일?\n리마인더에 답장으로 알려주세요.\n\n{items}")
            else:
                await msg.reply_html("📭 활성 할일 없음.")

    # ── modify_todo ──
    elif intent == "modify_todo":
        matched = None
        new_deadline = result.get("deadline_iso")
        if todo_id:
            matched = find_todo_by_id(todo_id)
        if not matched and reply_todo:
            matched = reply_todo
        active = [t for t in STATE["todos"] if t["status"] == "active"]
        if not matched and len(active) == 1:
            matched = active[0]

        if matched and new_deadline:
            matched["deadline"] = new_deadline
            matched["deadline_display"] = result.get("deadline_raw")
            matched["last_reminded_at"] = None
            matched["last_daily_date"] = None
            persist()
            dl_str = format_deadline(new_deadline, now)
            await msg.reply_html(reply or f"📅 <b>{matched.get('task')}</b> 기한 변경!\n⏰ {dl_str}")
            log.info(f"[MODIFY] {matched.get('task')} → {new_deadline}")
        elif matched and not new_deadline:
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

        if matched:
            matched["status"] = "deleted"
            matched["done_at"] = now.isoformat()
            persist()
            await msg.reply_html(reply or f"🗑️ <b>{matched.get('task')}</b> 삭제!")
            log.info(f"[DELETE] {matched.get('task')}")
        else:
            await msg.reply_html(reply or "🤔 어떤 할일? 리마인더에 답장으로 알려주세요.")

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
                elif batch_action == "complete":
                    t["status"] = "done"
                    t["done_at"] = now.isoformat()
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
    for todo in STATE["todos"]:
        # ── 반복 프로젝트 리마인더 ──
        if todo["status"] == "active" and todo.get("type") == "recurring" and todo.get("reminder_time"):
            r_hour, r_min = map(int, todo["reminder_time"].split(":"))
            reminder_dt = now.replace(hour=r_hour, minute=r_min, second=0, microsecond=0)
            # 리마인더 시각이 지났고, 오늘 아직 안 보냈으면 발송
            if now >= reminder_dt and todo.get("last_daily_date") != now.strftime("%Y-%m-%d"):
                proj = f"\n📁 {todo['project']}" if todo.get("project") else ""
                sent = await ctx.bot.send_message(
                    chat_id=CHAT_ID,
                    text=(
                        f"🔄 <b>장기 프로젝트 리마인더</b>\n\n"
                        f"📌 {todo['task']}{proj}\n"
                        f"⏰ 매일 {todo['reminder_time']} 반복\n\n"
                        f"<i>↩️ 이 메시지에 답장으로:\n"
                        f"완료 → '다했다'  중단 → '그만해'</i>"
                    ),
                    parse_mode="HTML",
                )
                todo["last_reminded_at"] = now.isoformat()
                todo["last_daily_date"] = now.strftime("%Y-%m-%d")
                STATE.setdefault("reminder_msg_map", {})[str(sent.message_id)] = todo["id"]
                persist()
                log.info(f"[REMIND_RECURRING] {todo['task']}")

        # ── 1회성 할일 리마인더 ──
        elif todo["status"] == "active" and todo.get("deadline") and todo.get("type") != "recurring":
            interval = get_reminder_interval(todo["deadline"], now)
            if interval is None:
                if now.hour < 7 or now.hour > 9: continue
                if todo.get("last_daily_date") == now.strftime("%Y-%m-%d"): continue
            else:
                if todo.get("last_reminded_at"):
                    elapsed = (now - datetime.fromisoformat(todo["last_reminded_at"])).total_seconds() / 3600
                    if elapsed < interval - 0.1: continue

            dl_str = format_deadline(todo["deadline"], now)
            proj = f"\n📁 {todo['project']}" if todo.get("project") else ""
            sent = await ctx.bot.send_message(
                chat_id=CHAT_ID,
                text=(
                    f"⏰ <b>리마인더</b>\n\n"
                    f"📌 {todo['task']}\n"
                    f"⏳ {dl_str}{proj}\n\n"
                    f"<i>↩️ 이 메시지에 답장으로:\n"
                    f"완료 → '다했다'  기한변경 → '금요일까지'</i>"
                ),
                parse_mode="HTML",
            )
            todo["last_reminded_at"] = now.isoformat()
            todo["last_daily_date"] = now.strftime("%Y-%m-%d")
            STATE.setdefault("reminder_msg_map", {})[str(sent.message_id)] = todo["id"]
            persist()
            log.info(f"[REMIND] {todo['task']}")

        elif todo["status"] == "pending_input":
            if 10 < now.minute < 50: continue
            if todo.get("last_reminded_at"):
                if (now - datetime.fromisoformat(todo["last_reminded_at"])).total_seconds() < 3000: continue
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
    log.info(f"CHAT_ID: {CHAT_ID} | LLM: {', '.join(llm_status) or '비활성'}")
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
