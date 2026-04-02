#!/usr/bin/env python3
"""
텔레그램 할일관리 리마인더 봇 (LLM 기반, 즉시응답)
python-telegram-bot long polling + Claude Haiku + JobQueue 스케줄러
Railway / Docker 배포용
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

# 데이터 경로: Railway volume mount 시 /data, 로컬이면 ./data
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


# 전역 상태
STATE = load_data()


def persist():
    save_data(STATE)


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
할일 등록, 완료 처리, 목록 확인만 담당해. 그 외 요청은 정중하게 거절해.

현재 시각: {now.strftime('%Y-%m-%d %H:%M')} KST ({WEEKDAYS_KR[now.weekday()]}요일)
{todo_ctx}

사용자 메시지를 분석해서 반드시 아래 JSON만 출력해.

{{
  "intent": "new_todo" | "complete_todo" | "list_todos" | "help" | "modify_todo" | "off_topic",
  "task": "할일 내용 (new_todo)",
  "deadline_raw": "데드라인 원문 (new_todo)",
  "deadline_iso": "YYYY-MM-DDTHH:MM:SS+09:00 또는 null",
  "todo_id": "완료할 할일 #id (complete_todo) 또는 null",
  "reply": "사용자에게 보낼 한국어 답변"
}}

규칙:
1. intent 판단:
   - 할일/과제/업무 등록 → "new_todo"
   - "다했다", "끝", "완료", "처리했어" 등 → "complete_todo"
   - "뭐 남았어?", "할일 알려줘", "목록" → "list_todos"
   - 사용법 질문 → "help"
   - 기존 할일 수정 → "modify_todo"
   - 할일 무관 → "off_topic"

2. new_todo:
   - 다양한 표현 이해: "FDD 이번주 금요일까지 끝내자", "내일까지 보고서 써야돼"
   - task: 핵심 할일만 (데드라인 제외)
   - deadline_iso: 현재 시각 기준 절대 시각. 시간 미지정 → 해당일 23:59
   - 파악 불가 시 null

3. complete_todo:
   - 활성 할일 중 매칭. 특정 불가 시 todo_id=null, reply에서 질문
   - 활성 1개면 자동 매칭

4. off_topic:
   - "저는 할일 관리 전문 봇이에요 😊" 등 정중한 거절. 짧게.

5. reply: 반말/존댓말 사용자 맞춤. 간결하게 3줄 이내. HTML 태그 가능: <b>, <i>, <code>"""


async def ask_llm(text: str, now: datetime) -> dict | None:
    if not ANTHROPIC_KEY:
        log.warning("ANTHROPIC_API_KEY 없음")
        return None

    system = build_system_prompt(now)
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
                    "messages": [{"role": "user", "content": text}],
                },
            )
            resp.raise_for_status()
            content = resp.json().get("content", [])
            raw = "".join(c.get("text", "") for c in content).strip()
            raw = re.sub(r"^```json\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw)
            return json.loads(raw)
    except json.JSONDecodeError as e:
        log.error(f"LLM JSON 파싱 실패: {e}")
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


# ══════════════════════════════════════════════════════════
#  텔레그램 핸들러
# ══════════════════════════════════════════════════════════

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_html(
        "👋 안녕하세요! <b>할일봇</b>입니다.\n\n"
        "할일을 자유롭게 말씀해주세요.\n"
        "예: <code>내일까지 FDD 보고서 마무리해야돼</code>\n\n"
        "완료하면 리마인더에 답장으로 <code>다했다</code>라고 보내주세요."
    )


async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    now = datetime.now(KST)
    await send_summary(ctx.bot, now, force=True)


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_html(
        "📋 <b>할일봇 사용법</b>\n\n"
        "✏️ <b>등록:</b> 자유롭게 말하면 됩니다\n"
        "  <code>내일까지 FDD 보고서 마무리해야돼</code>\n"
        "  <code>금요일까지 듀이트리 재무검토</code>\n\n"
        "✅ <b>완료:</b> 리마인더에 답장 → 완료 / 다했다 / 끝\n"
        "📋 /list — 할일 목록\n"
        "❓ /help — 이 도움말"
    )


async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """모든 텍스트 메시지를 LLM으로 분석해서 처리."""
    msg = update.message
    if not msg or not msg.text:
        return
    if msg.chat_id != CHAT_ID:
        return

    text = msg.text.strip()
    now = datetime.now(KST)

    result = await ask_llm(text, now)
    if not result:
        await msg.reply_html("🤖 잠시 문제가 생겼어요. 다시 말씀해주세요.")
        return

    intent = result.get("intent", "off_topic")
    reply = result.get("reply", "")
    log.info(f"[LLM] intent={intent} | {text[:60]}")

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
            if not task:
                missing.append("할일 내용")
            if not deadline_iso:
                missing.append("데드라인")
            lines.append(f"\n⚠️ {', '.join(missing)}이 부족해요. 알려주시면 업데이트할게요.")

        await msg.reply_html("\n".join(lines))

    # ── complete_todo ──
    elif intent == "complete_todo":
        active = [t for t in STATE["todos"] if t["status"] == "active"]
        matched = None

        # LLM 지정 ID
        todo_id = result.get("todo_id")
        if todo_id:
            matched = next((t for t in active if t["id"] == todo_id), None)

        # 답장 기반 매칭
        if not matched and msg.reply_to_message:
            reply_mid = str(msg.reply_to_message.message_id)
            target_id = STATE.get("reminder_msg_map", {}).get(reply_mid)
            if target_id:
                matched = next((t for t in active if t["id"] == target_id), None)

        # 활성 1개면 자동
        if not matched and len(active) == 1:
            matched = active[0]

        if matched:
            matched["status"] = "done"
            matched["done_at"] = now.isoformat()
            persist()
            await msg.reply_html(reply or f"✅ <b>{matched.get('task', '할일')}</b> 완료!")
            log.info(f"[DONE] {matched.get('task')}")
        else:
            await msg.reply_html(reply or "🤔 어떤 할일을 완료하셨나요? 리마인더에 답장으로 알려주세요.")

    # ── list_todos ──
    elif intent == "list_todos":
        await send_summary(ctx.bot, now, force=True)

    # ── help, modify, off_topic ──
    elif intent == "help":
        await msg.reply_html(reply or "자유롭게 할일을 말씀해주세요!")
    elif intent == "modify_todo":
        await msg.reply_html(reply or "⚠️ 수정은 아직 준비 중이에요. 완료 후 새로 등록해주세요.")
    else:
        await msg.reply_html(reply or "저는 할일 관리 전문 봇이에요 😊")


# ══════════════════════════════════════════════════════════
#  리마인더 스케줄러 (JobQueue)
# ══════════════════════════════════════════════════════════

def get_reminder_interval(deadline_iso: str, now: datetime) -> float | None:
    """> 24h → None (매일 8시), 12-24h → 4h, ≤12h → 3h"""
    try:
        remaining = (datetime.fromisoformat(deadline_iso) - now).total_seconds() / 3600
        if remaining <= 0:
            return 3.0
        elif remaining <= 12:
            return 3.0
        elif remaining <= 24:
            return 4.0
        else:
            return None
    except Exception:
        return None


async def reminder_check(ctx: ContextTypes.DEFAULT_TYPE):
    """5분마다 실행: 리마인더 발송 판단."""
    now = datetime.now(KST)

    for todo in STATE["todos"]:
        # ── 활성 할일 리마인더 ──
        if todo["status"] == "active" and todo.get("deadline"):
            interval = get_reminder_interval(todo["deadline"], now)

            if interval is None:
                # 24시간 이상 → 매일 아침 8시
                if now.hour < 7 or now.hour > 9:
                    continue
                if todo.get("last_daily_date") == now.strftime("%Y-%m-%d"):
                    continue
            else:
                # N시간 간격
                if todo.get("last_reminded_at"):
                    elapsed = (now - datetime.fromisoformat(todo["last_reminded_at"])).total_seconds() / 3600
                    if elapsed < interval - 0.1:
                        continue

            # 리마인더 전송
            dl_str = format_deadline(todo["deadline"], now)
            proj = f"\n📁 {todo['project']}" if todo.get("project") else ""
            sent = await ctx.bot.send_message(
                chat_id=CHAT_ID,
                text=(
                    f"⏰ <b>리마인더</b>\n\n"
                    f"📌 {todo['task']}\n"
                    f"⏳ {dl_str}{proj}\n\n"
                    f"<i>완료하셨으면 이 메시지에 답장으로 알려주세요.</i>"
                ),
                parse_mode="HTML",
            )
            todo["last_reminded_at"] = now.isoformat()
            todo["last_daily_date"] = now.strftime("%Y-%m-%d")
            STATE.setdefault("reminder_msg_map", {})[str(sent.message_id)] = todo["id"]
            persist()
            log.info(f"[REMIND] {todo['task']}")

        # ── 미완성 입력 리마인더 (매 시간 정각) ──
        elif todo["status"] == "pending_input":
            if 10 < now.minute < 50:
                continue
            if todo.get("last_reminded_at"):
                elapsed = (now - datetime.fromisoformat(todo["last_reminded_at"])).total_seconds()
                if elapsed < 3000:
                    continue

            missing = []
            if not todo.get("task"):
                missing.append("할일 내용")
            if not todo.get("deadline"):
                missing.append("데드라인")

            await ctx.bot.send_message(
                chat_id=CHAT_ID,
                text=(
                    f"⚠️ <b>입력 미완성</b>\n\n"
                    f"원본: <code>{todo.get('original_message', '?')}</code>\n"
                    f"부족: {', '.join(missing)}\n\n"
                    f"예: <code>내일까지 보고서 작성</code>"
                ),
                parse_mode="HTML",
            )
            todo["last_reminded_at"] = now.isoformat()
            persist()
            log.info(f"[PENDING] {todo.get('original_message')}")


async def daily_summary(ctx: ContextTypes.DEFAULT_TYPE):
    """매일 아침 7시 KST 실행."""
    now = datetime.now(KST)
    await send_summary(ctx.bot, now, force=True)


async def send_summary(bot, now: datetime, force: bool = False):
    """전체 할일 요약 전송."""
    active = [t for t in STATE["todos"] if t["status"] in ("active", "pending_input")]
    if not active:
        if force:
            await bot.send_message(chat_id=CHAT_ID, text="📭 활성 할일이 없습니다. 한가하시네요! 😎")
        return

    def sort_key(t):
        if t.get("deadline"):
            try:
                return datetime.fromisoformat(t["deadline"])
            except Exception:
                pass
        return datetime.max.replace(tzinfo=KST)

    active.sort(key=sort_key)

    wday = WEEKDAYS_KR[now.weekday()]
    lines = [f"📋 <b>오늘의 할일 ({now.month}/{now.day} {wday})</b>\n"]

    overdue = [t for t in active if t.get("deadline") and datetime.fromisoformat(t["deadline"]) < now]
    today_end = now.replace(hour=23, minute=59, second=59)
    today_due = [
        t for t in active
        if t.get("deadline") and now <= datetime.fromisoformat(t["deadline"]) <= today_end
        and t not in overdue
    ]
    upcoming = [t for t in active if t not in overdue and t not in today_due]

    for label, group in [("🔴 기한 초과", overdue), ("🟡 오늘 마감", today_due), ("🟢 예정", upcoming)]:
        if not group:
            continue
        lines.append(f"<b>{label}:</b>")
        for t in group:
            proj = f" [{t['project']}]" if t.get("project") else ""
            task = t.get("task", "(미입력)")
            if t.get("deadline"):
                lines.append(f"  • {task}{proj}\n    {format_deadline(t['deadline'], now)}")
            else:
                lines.append(f"  • {task}{proj}\n    ⚠️ 데드라인 미설정")
        lines.append("")

    lines.append(f"총 {len(active)}건 | /list")
    await bot.send_message(chat_id=CHAT_ID, text="\n".join(lines), parse_mode="HTML")
    log.info(f"[SUMMARY] {len(active)}건")


async def cleanup_job(ctx: ContextTypes.DEFAULT_TYPE):
    """매일 자정: 7일 지난 완료 항목 정리."""
    now = datetime.now(KST)
    cutoff = now - timedelta(days=7)
    before = len(STATE["todos"])
    STATE["todos"] = [
        t for t in STATE["todos"]
        if not (t["status"] == "done" and t.get("done_at") and datetime.fromisoformat(t["done_at"]) < cutoff)
    ]
    # 오래된 매핑 정리
    if len(STATE.get("reminder_msg_map", {})) > 200:
        ids = {t["id"] for t in STATE["todos"]}
        STATE["reminder_msg_map"] = {k: v for k, v in STATE["reminder_msg_map"].items() if v in ids}
    persist()
    log.info(f"[CLEANUP] {before} → {len(STATE['todos'])}건")


# ══════════════════════════════════════════════════════════
#  메인
# ══════════════════════════════════════════════════════════

def main():
    log.info("=== 할일봇 시작 ===")
    log.info(f"CHAT_ID: {CHAT_ID}")
    log.info(f"DATA_DIR: {DATA_DIR}")
    log.info(f"LLM: {'활성' if ANTHROPIC_KEY else '비활성'}")
    log.info(f"활성 할일: {len([t for t in STATE['todos'] if t['status'] == 'active'])}건")

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # 커맨드 핸들러
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("help", cmd_help))

    # 모든 텍스트 메시지 → LLM 분석
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # 스케줄 잡 등록
    jq = app.job_queue

    # 리마인더 체크: 5분마다
    jq.run_repeating(reminder_check, interval=300, first=30)

    # 매일 아침 7시 KST 요약 (UTC 22:00 = KST 07:00)
    jq.run_daily(daily_summary, time=time(hour=22, minute=0, tzinfo=timezone.utc))

    # 매일 자정 정리 (UTC 15:00 = KST 00:00)
    jq.run_daily(cleanup_job, time=time(hour=15, minute=0, tzinfo=timezone.utc))

    log.info("Long polling 시작...")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)


if __name__ == "__main__":
    main()
