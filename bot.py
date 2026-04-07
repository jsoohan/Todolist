#!/usr/bin/env python3
"""
텔레그램 할일관리 리마인더 봇 v2 (LLM 기반, 즉시응답)
- 답장 컨텍스트 → LLM 주입 (리마인더 답장 시 "이건" 자동 해석)
- modify_todo 구현 (기한 연장/단축)
- python-telegram-bot long polling + Claude Haiku + JobQueue
"""

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
할일 등록, 완료 처리, 기한 변경, 삭제, 목록 확인을 담당해. 그 외 요청은 정중하게 거절.

현재 시각: {now.strftime('%Y-%m-%d %H:%M')} KST ({WEEKDAYS_KR[now.weekday()]}요일)
{todo_ctx}

반드시 아래 JSON만 출력. JSON 외 텍스트 절대 금지.

{{
  "intent": "new_todo" | "complete_todo" | "modify_todo" | "delete_todo" | "list_todos" | "help" | "off_topic",
  "task": "할일 내용 (new_todo일 때만)",
  "deadline_raw": "데드라인 원문 (new_todo, modify_todo)",
  "deadline_iso": "YYYY-MM-DDTHH:MM:SS+09:00 또는 null",
  "todo_id": "대상 할일의 #id 또는 null",
  "reply": "한국어 답변"
}}

핵심 규칙:

1. **reply_context 처리 (가장 중요):**
   사용자 메시지에 [reply_context: #ID - 할일이름] 이 붙어있으면, 사용자가 특정 리마인더에 답장한 것.
   "이건", "이거", "이 할일", 주어 없는 문장 → 전부 reply_context의 할일을 가리킴.
   반드시 todo_id에 해당 ID를 넣어.
   
   예시:
   - reply_context: #abc1 - 듀이트리 아마란스 보고
   - "이건 더 안봐도 돼" → intent: complete_todo, todo_id: "abc1"
   - "이거 기한 토요일까지로 늘려줘" → intent: modify_todo, todo_id: "abc1", deadline_iso 계산
   - "삭제해" → intent: delete_todo, todo_id: "abc1"

2. **intent 판단:**
   - 새 할일 등록 → "new_todo"
   - 완료 표현: "다했다", "끝", "완료", "더 안봐도 돼", "됐어", "처리했어", "안해도 돼" → "complete_todo"
   - 기한 변경: "늘려줘", "줄여줘", "연장", "기한 변경", "까지로 바꿔" → "modify_todo"
   - 삭제: "삭제해", "취소해", "빼줘" → "delete_todo"
   - 목록: "뭐 남았어?", "할일", "목록" → "list_todos"
   - 사용법 → "help"
   - 할일 무관 → "off_topic"

3. **new_todo:** task=핵심만, deadline_iso=절대시각(미지정→23:59), 파악불가→null

4. **complete_todo:** reply_context 있으면 그 ID. 없으면 메시지로 매칭. 1개면 자동. 못 찾으면 null+reply에서 질문.

5. **modify_todo:** todo_id + deadline_iso 필수. "이번주 토요일" = 이번 주 토요일 23:59.

6. **delete_todo:** todo_id 필수.

7. **reply:** 반말/존댓말 맞춤. 간결 3줄. HTML <b><i><code> 가능."""


def build_user_message(text: str, reply_todo: dict | None) -> str:
    """사용자 메시지에 답장 컨텍스트 주입."""
    if reply_todo:
        return f"[reply_context: #{reply_todo['id']} - {reply_todo.get('task', '?')}]\n{text}"
    return text


async def ask_llm(text: str, now: datetime, reply_todo: dict | None = None) -> dict | None:
    if not ANTHROPIC_KEY:
        log.warning("ANTHROPIC_API_KEY 없음")
        return None

    system = build_system_prompt(now)
    user_msg = build_user_message(text, reply_todo)

    try:
        async with httpx.AsyncClient(timeout=15) as client:
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
                log.error(f"LLM API {resp.status_code}: {resp.text[:500]}")
                return None
            content = resp.json().get("content", [])
            raw = "".join(c.get("text", "") for c in content).strip()
            raw = re.sub(r"^```json\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw)
            return json.loads(raw)
    except json.JSONDecodeError as e:
        log.error(f"LLM JSON 파싱 실패: {e}\nraw: {raw[:300]}")
        return None
    except Exception as e:
        log.error(f"LLM 호출 실패: {e}")
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
        "🗑️ <b>삭제:</b> 리마인더 답장 → <code>삭제해</code>\n"
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
        await msg.reply_html("🤖 잠시 문제가 생겼어요. 다시 말씀해주세요.")
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
        if todo["status"] == "active" and todo.get("deadline"):
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

    active.sort(key=lambda t: datetime.fromisoformat(t["deadline"]) if t.get("deadline") else datetime.max.replace(tzinfo=KST))
    wday = WEEKDAYS_KR[now.weekday()]
    lines = [f"📋 <b>오늘의 할일 ({now.month}/{now.day} {wday})</b>\n"]

    overdue = [t for t in active if t.get("deadline") and datetime.fromisoformat(t["deadline"]) < now]
    today_end = now.replace(hour=23, minute=59, second=59)
    today_due = [t for t in active if t.get("deadline") and now <= datetime.fromisoformat(t["deadline"]) <= today_end and t not in overdue]
    upcoming = [t for t in active if t not in overdue and t not in today_due]

    for label, group in [("🔴 기한 초과", overdue), ("🟡 오늘 마감", today_due), ("🟢 예정", upcoming)]:
        if not group: continue
        lines.append(f"<b>{label}:</b>")
        for t in group:
            proj = f" [{t['project']}]" if t.get("project") else ""
            task = t.get("task", "(미입력)")
            dl = format_deadline(t["deadline"], now) if t.get("deadline") else "⚠️ 데드라인 미설정"
            lines.append(f"  • {task}{proj}\n    {dl}")
        lines.append("")

    lines.append(f"총 {len(active)}건 | /list")
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
    log.info(f"CHAT_ID: {CHAT_ID} | LLM: {'활성' if ANTHROPIC_KEY else '비활성'}")
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
