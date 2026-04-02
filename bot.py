#!/usr/bin/env python3
"""
텔레그램 할일관리 리마인더 봇 (LLM 기반)
- Claude Haiku로 자연어 이해
- GitHub Actions 30분 cron
"""

import json
import os
import re
import uuid
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

# ── 설정 ──────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
API_BASE = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
KST = timezone(timedelta(hours=9))
DATA_DIR = Path(__file__).parent / "data"
DATA_FILE = DATA_DIR / "todos.json"

WEEKDAYS_KR = ["월", "화", "수", "목", "금", "토", "일"]

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
    return {"last_update_id": 0, "todos": [], "reminder_msg_map": {}}


def save_data(data: dict):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ══════════════════════════════════════════════════════════
#  텔레그램 API
# ══════════════════════════════════════════════════════════

def send_msg(text: str, reply_to: int | None = None) -> int | None:
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}
    if reply_to:
        payload["reply_to_message_id"] = reply_to
    try:
        resp = requests.post(f"{API_BASE}/sendMessage", json=payload, timeout=10)
        resp.raise_for_status()
        return resp.json().get("result", {}).get("message_id")
    except Exception as e:
        print(f"[ERROR] send_msg: {e}")
        return None


def get_updates(offset: int = 0) -> list[dict]:
    params = {"timeout": 5, "allowed_updates": ["message"]}
    if offset:
        params["offset"] = offset
    try:
        resp = requests.get(f"{API_BASE}/getUpdates", params=params, timeout=15)
        resp.raise_for_status()
        return resp.json().get("result", [])
    except Exception as e:
        print(f"[ERROR] get_updates: {e}")
        return []


# ══════════════════════════════════════════════════════════
#  Claude API - 메시지 이해 엔진
# ══════════════════════════════════════════════════════════

def build_llm_system_prompt(now: datetime, active_todos: list[dict]) -> str:
    """LLM 시스템 프롬프트 생성. 현재 할일 목록을 컨텍스트로 제공."""

    todo_context = ""
    if active_todos:
        lines = []
        for i, t in enumerate(active_todos):
            dl = ""
            if t.get("deadline"):
                try:
                    dt = datetime.fromisoformat(t["deadline"])
                    dl = f" (마감: {dt.month}/{dt.day} {dt.strftime('%H:%M')})"
                except Exception:
                    pass
            proj = f" [{t['project']}]" if t.get("project") else ""
            lines.append(f"  #{t['id']} - {t.get('task', '(미입력)')}{dl}{proj}")
        todo_context = "\n현재 활성 할일:\n" + "\n".join(lines)
    else:
        todo_context = "\n현재 활성 할일: 없음"

    return f"""너는 할일 관리 전문 텔레그램 봇이야. 이름은 "할일봇".
할일 등록, 완료 처리, 목록 확인만 담당해. 그 외 요청은 정중하게 거절해.

현재 시각: {now.strftime('%Y-%m-%d %H:%M')} KST ({WEEKDAYS_KR[now.weekday()]}요일)
{todo_context}

사용자 메시지를 분석해서 반드시 아래 JSON 형식으로만 응답해. JSON 외에 아무것도 출력하지 마.

{{
  "intent": "new_todo" | "complete_todo" | "list_todos" | "help" | "modify_todo" | "off_topic",
  "task": "할일 내용 (new_todo일 때)",
  "deadline_raw": "사용자가 말한 데드라인 원문 (new_todo일 때)",
  "deadline_iso": "YYYY-MM-DDTHH:MM:SS+09:00 (파싱 가능하면)",
  "todo_id": "완료할 할일의 #id (complete_todo일 때)",
  "reply": "사용자에게 보낼 자연스러운 한국어 답변"
}}

규칙:
1. intent 판단:
   - 할일/과제/업무를 등록하려는 의도 → "new_todo"
   - "다했다", "끝", "완료", "처리했어" 등 완료 표현 → "complete_todo"
   - "뭐 남았어?", "할일 알려줘", "목록" → "list_todos"
   - 사용법 질문 → "help"
   - 기존 할일 수정/데드라인 변경 → "modify_todo" (현재 미지원이라 reply에서 안내)
   - 할일과 무관한 대화 → "off_topic"

2. new_todo:
   - 다양한 표현을 이해해: "내일까지 보고서 써야돼", "FDD 이번주 금요일까지 끝내자", "4/10 ASIABNC 제안서", "듀이트리 재무검토 좀 해야하는데 수요일까지"
   - task: 핵심 할일만 깔끔하게 추출 (데드라인 부분 제외)
   - deadline_raw: 사용자가 말한 시간 표현 원문
   - deadline_iso: 현재 시각 기준으로 절대 시각 계산. 시간 미지정 시 해당일 23:59. "이번주 금요일" = 이번주 돌아오는 금요일, "다음주 월요일" = 다음주 월요일
   - 데드라인을 파악할 수 없으면 deadline_iso를 null로
   - task를 파악할 수 없으면 task를 null로
   - reply: 등록 확인 메시지 (친근하게)

3. complete_todo:
   - 현재 활성 할일 중에서 매칭. 메시지 내용이나 맥락으로 어떤 할일인지 추론
   - 특정할 수 없으면 todo_id를 null로 하고 reply에서 어떤 건지 물어봐
   - 활성 할일이 1개뿐이면 그걸로 매칭

4. off_topic:
   - reply에 "저는 할일 관리 전문 봇이에요 😊" 같은 정중한 거절
   - 유머 있게 해도 좋아. 하지만 짧게.

5. reply 작성 규칙:
   - 반말/존댓말은 사용자에 맞춰
   - 간결하게. 3줄 이내.
   - HTML 태그 사용 가능: <b>, <i>, <code>"""


def ask_llm(user_text: str, now: datetime, active_todos: list[dict]) -> dict | None:
    """Claude Haiku에 메시지 분석 요청."""
    if not ANTHROPIC_KEY:
        print("[WARN] ANTHROPIC_API_KEY 없음. LLM 비활성.")
        return None

    system = build_llm_system_prompt(now, active_todos)

    try:
        resp = requests.post(
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
                "messages": [{"role": "user", "content": user_text}],
            },
            timeout=15,
        )
        resp.raise_for_status()
        content = resp.json().get("content", [])
        text = "".join(c.get("text", "") for c in content).strip()

        # JSON 추출 (마크다운 코드블록 제거)
        text = re.sub(r"^```json\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

        return json.loads(text)
    except json.JSONDecodeError as e:
        print(f"[ERROR] LLM JSON 파싱 실패: {e}\nraw: {text[:200]}")
        return None
    except Exception as e:
        print(f"[ERROR] LLM 호출 실패: {e}")
        return None


# ══════════════════════════════════════════════════════════
#  프로젝트 태깅 & 유틸
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
        date_str = f"{dl.month}/{dl.day}({wday}) {dl.strftime('%H:%M')}"

        if hours < 0:
            return f"⚠️ {date_str} (기한 초과!)"
        elif hours < 1:
            return f"🔴 {date_str} ({int(diff.total_seconds()/60)}분 남음)"
        elif hours < 12:
            return f"🔴 {date_str} ({hours:.0f}시간 남음)"
        elif hours < 24:
            return f"🟡 {date_str} ({hours:.0f}시간 남음)"
        elif diff.days < 3:
            return f"🟡 {date_str} ({diff.days}일 {int(hours%24)}시간 남음)"
        else:
            return f"🟢 {date_str} ({diff.days}일 남음)"
    except Exception:
        return deadline_iso


# ══════════════════════════════════════════════════════════
#  메시지 처리
# ══════════════════════════════════════════════════════════

def process_message(msg: dict, data: dict, now: datetime):
    text = msg.get("text", "").strip()
    if not text:
        return

    chat_id = str(msg.get("chat", {}).get("id", ""))
    if chat_id != str(CHAT_ID):
        return

    msg_id = msg.get("message_id")

    # /start는 LLM 안 거치고 바로 처리
    if text.lower() == "/start":
        send_msg(
            "👋 안녕하세요! <b>할일봇</b>입니다.\n\n"
            "할일을 자유롭게 말씀해주세요.\n"
            "예: <code>내일까지 FDD 보고서 마무리해야돼</code>\n\n"
            "완료하면 리마인더에 답장으로 <code>다했다</code> 하시면 됩니다.",
            reply_to=msg_id,
        )
        return

    # ── LLM 분석 ──
    active_todos = [t for t in data["todos"] if t["status"] in ("active", "pending_input")]
    result = ask_llm(text, now, active_todos)

    if not result:
        # LLM 실패 시 기본 안내
        send_msg("🤖 잠시 문제가 생겼어요. 다시 말씀해주세요.", reply_to=msg_id)
        return

    intent = result.get("intent", "off_topic")
    reply = result.get("reply", "")
    print(f"[LLM] intent={intent} | {text[:50]}")

    # ── new_todo ──────────────────────────────────────
    if intent == "new_todo":
        task = result.get("task")
        deadline_iso = result.get("deadline_iso")
        project = detect_project(text)  # 키워드 기반 (LLM 대신 확정적)

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
        data["todos"].append(todo)

        # 응답 구성: LLM reply + 구조화 정보
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

        send_msg("\n".join(lines), reply_to=msg_id)
        print(f"[NEW] {task} → {deadline_iso}")

    # ── complete_todo ─────────────────────────────────
    elif intent == "complete_todo":
        todo_id = result.get("todo_id")
        matched = None

        # LLM이 ID를 지정한 경우
        if todo_id:
            for t in active_todos:
                if t["id"] == todo_id:
                    matched = t
                    break

        # 답장 기반 매칭 (LLM이 못 찾았을 때 보완)
        if not matched:
            reply_mid = str(msg.get("reply_to_message", {}).get("message_id", ""))
            if reply_mid and reply_mid in data.get("reminder_msg_map", {}):
                target_id = data["reminder_msg_map"][reply_mid]
                for t in active_todos:
                    if t["id"] == target_id:
                        matched = t
                        break

        # 활성 1개면 자동
        if not matched and len([t for t in active_todos if t["status"] == "active"]) == 1:
            matched = [t for t in active_todos if t["status"] == "active"][0]

        if matched:
            matched["status"] = "done"
            matched["done_at"] = now.isoformat()
            task_name = matched.get("task", "할일")
            if reply:
                send_msg(reply, reply_to=msg_id)
            else:
                send_msg(f"✅ <b>{task_name}</b> 완료!", reply_to=msg_id)
            print(f"[DONE] {task_name}")
        else:
            send_msg(reply or "🤔 어떤 할일을 완료하셨나요? 리마인더에 답장으로 알려주세요.", reply_to=msg_id)

    # ── list_todos ────────────────────────────────────
    elif intent == "list_todos":
        send_daily_summary(data, now, force=True)

    # ── help ──────────────────────────────────────────
    elif intent == "help":
        send_msg(reply or "자유롭게 할일을 말씀해주세요. 완료 시 '다했다'라고 답장하면 됩니다.", reply_to=msg_id)

    # ── modify_todo ───────────────────────────────────
    elif intent == "modify_todo":
        send_msg(reply or "⚠️ 할일 수정은 아직 준비 중이에요. 완료 후 새로 등록해주세요.", reply_to=msg_id)

    # ── off_topic ─────────────────────────────────────
    else:
        send_msg(reply or "저는 할일 관리 전문 봇이에요 😊 할일이 있으시면 알려주세요!", reply_to=msg_id)


# ══════════════════════════════════════════════════════════
#  리마인더 로직 (LLM 불필요 — 규칙 기반)
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


def should_remind(todo: dict, now: datetime) -> bool:
    if todo["status"] != "active" or not todo.get("deadline"):
        return False
    interval = get_reminder_interval(todo["deadline"], now)
    if interval is None:
        if now.hour < 7 or now.hour > 9:
            return False
        if todo.get("last_daily_date") == now.strftime("%Y-%m-%d"):
            return False
        return True
    else:
        if todo.get("last_reminded_at"):
            elapsed = (now - datetime.fromisoformat(todo["last_reminded_at"])).total_seconds() / 3600
            if elapsed < interval - 0.5:
                return False
        return True


def should_remind_pending(todo: dict, now: datetime) -> bool:
    if todo["status"] != "pending_input":
        return False
    if 15 < now.minute < 45:
        return False
    if todo.get("last_reminded_at"):
        if (now - datetime.fromisoformat(todo["last_reminded_at"])).total_seconds() < 3000:
            return False
    return True


def send_reminder(todo: dict, data: dict, now: datetime):
    dl_str = format_deadline(todo["deadline"], now)
    proj = f"\n📁 {todo['project']}" if todo.get("project") else ""

    sent_id = send_msg(
        f"⏰ <b>리마인더</b>\n\n"
        f"📌 {todo['task']}\n"
        f"⏳ {dl_str}{proj}\n\n"
        f"<i>완료하셨으면 이 메시지에 답장으로 알려주세요.</i>"
    )
    todo["last_reminded_at"] = now.isoformat()
    todo["last_daily_date"] = now.strftime("%Y-%m-%d")
    if sent_id:
        data.setdefault("reminder_msg_map", {})[str(sent_id)] = todo["id"]
    print(f"[REMIND] {todo['task']}")


def send_pending_remind(todo: dict, now: datetime):
    missing = []
    if not todo.get("task"):
        missing.append("할일 내용")
    if not todo.get("deadline"):
        missing.append("데드라인")
    send_msg(
        f"⚠️ <b>입력 미완성</b>\n\n"
        f"원본: <code>{todo.get('original_message', '?')}</code>\n"
        f"부족: {', '.join(missing)}\n\n"
        f"예: <code>내일까지 보고서 작성</code> 형태로 보내주세요."
    )
    todo["last_reminded_at"] = now.isoformat()
    print(f"[PENDING] {todo.get('original_message')}")


def send_daily_summary(data: dict, now: datetime, force: bool = False):
    if not force:
        if now.hour != 7 or now.minute > 30:
            return
        if data.get("last_summary_date") == now.strftime("%Y-%m-%d"):
            return

    active = [t for t in data["todos"] if t["status"] in ("active", "pending_input")]
    if not active:
        if force:
            send_msg("📭 활성 할일이 없습니다. 한가하시네요! 😎")
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

    lines.append(f"총 {len(active)}건")
    send_msg("\n".join(lines))
    data["last_summary_date"] = now.strftime("%Y-%m-%d")
    print(f"[SUMMARY] {len(active)}건")


# ══════════════════════════════════════════════════════════
#  정리 & 메인
# ══════════════════════════════════════════════════════════

def cleanup(data: dict, now: datetime):
    cutoff = now - timedelta(days=7)
    data["todos"] = [
        t for t in data["todos"]
        if not (t["status"] == "done" and t.get("done_at") and datetime.fromisoformat(t["done_at"]) < cutoff)
    ]
    if len(data.get("reminder_msg_map", {})) > 100:
        ids = {t["id"] for t in data["todos"]}
        data["reminder_msg_map"] = {k: v for k, v in data["reminder_msg_map"].items() if v in ids}


def main():
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("[ERROR] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 필요")
        sys.exit(1)
    if not ANTHROPIC_KEY:
        print("[WARN] ANTHROPIC_API_KEY 없음. LLM 기능 비활성화.")

    now = datetime.now(KST)
    print(f"\n{'='*50}")
    print(f"[RUN] {now.strftime('%Y-%m-%d %H:%M:%S KST')}")

    data = load_data()

    # 1) 새 메시지 폴링
    offset = data.get("last_update_id", 0)
    updates = get_updates(offset + 1 if offset else 0)
    print(f"[POLL] {len(updates)}건")

    for update in updates:
        data["last_update_id"] = update["update_id"]
        if m := update.get("message"):
            process_message(m, data, now)

    # 2) 매일 7시 요약
    send_daily_summary(data, now)

    # 3) 리마인더
    for todo in data["todos"]:
        if should_remind(todo, now):
            send_reminder(todo, data, now)
        elif should_remind_pending(todo, now):
            send_pending_remind(todo, now)

    # 4) 정리 & 저장
    cleanup(data, now)
    save_data(data)
    print(f"[SAVE] {len(data['todos'])}건")


if __name__ == "__main__":
    main()
