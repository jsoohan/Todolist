#!/usr/bin/env python3
"""
텔레그램 할일관리 리마인더 봇
GitHub Actions cron (30분 간격)으로 실행
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
API_BASE = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
KST = timezone(timedelta(hours=9))
DATA_DIR = Path(__file__).parent / "data"
DATA_FILE = DATA_DIR / "todos.json"

# ── 프로젝트 키워드 → 태그 매핑 ──────────────────────────
PROJECT_KEYWORDS: dict[str, str] = {
    # Project FUN (Funnel Group)
    "fun": "🎯 Project FUN",
    "funnel": "🎯 Project FUN",
    "퍼널": "🎯 Project FUN",
    "fdd": "🎯 Project FUN",
    "dio": "🎯 Project FUN",
    "e-clinic": "🎯 Project FUN",
    "fin clinic": "🎯 Project FUN",
    "上野": "🎯 Project FUN",
    "우에노": "🎯 Project FUN",
    # Project DIVA (듀이트리)
    "diva": "💄 Project DIVA",
    "듀이트리": "💄 Project DIVA",
    "dewytree": "💄 Project DIVA",
    # Project ASCLEPIUS
    "asclepius": "🔬 Project ASCLEPIUS",
    "웨이센": "🔬 Project ASCLEPIUS",
    "파인메딕스": "🔬 Project ASCLEPIUS",
    "pentax": "🔬 Project ASCLEPIUS",
    "내시경": "🔬 Project ASCLEPIUS",
    # ASIABNC Pre-IPO
    "asiabnc": "🌏 ASIABNC Pre-IPO",
    "아시아비엔씨": "🌏 ASIABNC Pre-IPO",
    "대봉": "🌏 ASIABNC Pre-IPO",
    # 팽팽클리닉
    "팽팽": "🏥 팽팽클리닉",
    "pangpang": "🏥 팽팽클리닉",
    "실리프팅": "🏥 팽팽클리닉",
    "매일유업": "🏥 팽팽클리닉",
    "셀렉스": "🏥 팽팽클리닉",
    # Greenwood
    "greenwood": "🌲 Greenwood EP",
    "그린우드": "🌲 Greenwood EP",
    # Bionet
    "bionet": "📊 Bionet",
    "바이오넷": "📊 Bionet",
}

# ── 완료 감지 키워드 ──────────────────────────────────────
DONE_KEYWORDS = [
    "완료", "다했다", "다 했다", "끝", "했어", "했습니다",
    "완", "done", "finish", "끝났", "했다", "처리했",
    "처리 완료", "ㅇㅋ", "했음", "함", "끝남", "ok",
]


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
    """메시지 전송. 보낸 메시지 ID 반환."""
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
#  날짜/데드라인 파싱
# ══════════════════════════════════════════════════════════

WEEKDAY_MAP = {
    "월요일": 0, "화요일": 1, "수요일": 2, "목요일": 3,
    "금요일": 4, "토요일": 5, "일요일": 6,
    "월": 0, "화": 1, "수": 2, "목": 3,
    "금": 4, "토": 5, "일": 6,
}


def parse_deadline(text: str, now: datetime) -> datetime | None:
    """
    한국어 데드라인 파싱 → KST datetime.
    
    지원: 오늘/내일/모레/글피, 오늘 N시, 내일 N시,
          N일후, N시간후, 요일, M/D, M월D일,
          YYYY-MM-DD, YYYY.MM.DD, YYYYMMDD
    """
    text = text.strip()

    # ── 상대 날짜 ──
    if text == "오늘":
        return now.replace(hour=23, minute=59, second=0, microsecond=0)
    if text == "내일":
        return (now + timedelta(days=1)).replace(hour=23, minute=59, second=0, microsecond=0)
    if text == "모레":
        return (now + timedelta(days=2)).replace(hour=23, minute=59, second=0, microsecond=0)
    if text == "글피":
        return (now + timedelta(days=3)).replace(hour=23, minute=59, second=0, microsecond=0)

    # ── "오늘/내일 N시" ──
    m = re.match(r"(오늘|내일)\s*(\d{1,2})시", text)
    if m:
        base = now if m.group(1) == "오늘" else now + timedelta(days=1)
        return base.replace(hour=min(int(m.group(2)), 23), minute=0, second=0, microsecond=0)

    # ── "N일 후" ──
    m = re.match(r"(\d+)\s*일\s*후?$", text)
    if m:
        return (now + timedelta(days=int(m.group(1)))).replace(hour=23, minute=59, second=0, microsecond=0)

    # ── "N시간 후" ──
    m = re.match(r"(\d+)\s*시간\s*후?$", text)
    if m:
        return now + timedelta(hours=int(m.group(1)))

    # ── 요일 ──
    for day_str, day_num in WEEKDAY_MAP.items():
        if text == day_str or text == f"다음 {day_str}" or text == f"이번 {day_str}":
            days_ahead = day_num - now.weekday()
            if days_ahead <= 0:
                days_ahead += 7
            return (now + timedelta(days=days_ahead)).replace(hour=23, minute=59, second=0, microsecond=0)

    # ── "M월 D일" ──
    m = re.match(r"(\d{1,2})월\s*(\d{1,2})일?", text)
    if m:
        try:
            dt = now.replace(month=int(m.group(1)), day=int(m.group(2)), hour=23, minute=59, second=0, microsecond=0)
            return dt if dt >= now else dt.replace(year=dt.year + 1)
        except ValueError:
            return None

    # ── "M/D" ──
    m = re.match(r"(\d{1,2})/(\d{1,2})$", text)
    if m:
        try:
            dt = now.replace(month=int(m.group(1)), day=int(m.group(2)), hour=23, minute=59, second=0, microsecond=0)
            return dt if dt >= now else dt.replace(year=dt.year + 1)
        except ValueError:
            return None

    # ── "YYYY-MM-DD" / "YYYY.MM.DD" / "YYYY/MM/DD" ──
    m = re.match(r"(\d{4})[-./](\d{1,2})[-./](\d{1,2})", text)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), 23, 59, 0, tzinfo=KST)
        except ValueError:
            return None

    # ── "YYYYMMDD" ──
    m = re.match(r"(\d{4})(\d{2})(\d{2})$", text)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), 23, 59, 0, tzinfo=KST)
        except ValueError:
            return None

    return None


# ══════════════════════════════════════════════════════════
#  프로젝트 자동 태깅
# ══════════════════════════════════════════════════════════

def detect_project(text: str) -> str | None:
    text_lower = text.lower()
    for keyword, project in PROJECT_KEYWORDS.items():
        if keyword.lower() in text_lower:
            return project
    return None


# ══════════════════════════════════════════════════════════
#  메시지 처리
# ══════════════════════════════════════════════════════════

def is_done_message(text: str) -> bool:
    text_lower = text.lower().strip()
    return any(kw in text_lower for kw in DONE_KEYWORDS)


def find_todo_for_done(data: dict, msg: dict) -> dict | None:
    """완료 메시지 → 해당 할일 매칭. 답장 > 텍스트 매칭 > 단일 할일."""
    active = [t for t in data["todos"] if t["status"] == "active"]
    if not active:
        return None

    # 1) 답장 매핑
    reply_id = str(msg.get("reply_to_message", {}).get("message_id", ""))
    if reply_id and reply_id in data.get("reminder_msg_map", {}):
        todo_id = data["reminder_msg_map"][reply_id]
        for t in active:
            if t["id"] == todo_id:
                return t

    # 2) 텍스트 매칭
    text = msg.get("text", "")
    for t in active:
        if t.get("task") and t["task"] in text:
            return t

    # 3) 할일 1개면 자동
    if len(active) == 1:
        return active[0]

    return None


def parse_todo_message(text: str, now: datetime) -> dict:
    """'[데드라인]까지 [할일]' 파싱."""
    result = {"task": None, "deadline": None, "deadline_dt": None}

    if "까지" not in text:
        cleaned = text.strip()
        if cleaned:
            result["task"] = cleaned
        return result

    parts = text.split("까지", 1)
    deadline_str = parts[0].strip()
    task_str = parts[1].strip() if len(parts) > 1 else ""

    if deadline_str:
        dt = parse_deadline(deadline_str, now)
        if dt:
            result["deadline"] = deadline_str
            result["deadline_dt"] = dt.isoformat()

    if task_str:
        result["task"] = task_str

    return result


def process_message(msg: dict, data: dict, now: datetime):
    """새 메시지 처리: 명령어 / 완료 / 할일 등록."""
    text = msg.get("text", "").strip()
    if not text:
        return

    chat_id = str(msg.get("chat", {}).get("id", ""))
    if chat_id != str(CHAT_ID):
        return

    msg_id = msg.get("message_id")

    # ── 명령어 ──
    if text.lower() in ("/list", "/목록", "/할일"):
        send_daily_summary(data, now, force=True)
        return

    if text.lower() in ("/help", "/도움", "/start"):
        send_msg(
            "📋 <b>할일 봇 사용법</b>\n\n"
            "✏️ <b>등록:</b> <code>[데드라인]까지 [할일]</code>\n"
            "  예: <code>내일까지 FDD 보고서 완성</code>\n"
            "  예: <code>4/10까지 ASIABNC 제안서</code>\n"
            "  예: <code>금요일까지 듀이트리 재무검토</code>\n\n"
            "✅ <b>완료:</b> 리마인더에 답장 → <code>완료</code> / <code>다했다</code> / <code>끝</code>\n\n"
            "📋 /list  ❓ /help"
        )
        return

    # ── 완료 감지 ──
    if is_done_message(text):
        todo = find_todo_for_done(data, msg)
        if todo:
            todo["status"] = "done"
            todo["done_at"] = now.isoformat()
            send_msg(f"✅ <b>{todo.get('task', '할일')}</b> 완료!", reply_to=msg_id)
            print(f"[DONE] {todo.get('task')}")
        else:
            active = [t for t in data["todos"] if t["status"] == "active"]
            if len(active) > 1:
                lines = [f"  {i+1}. {t.get('task', '(미입력)')}" for i, t in enumerate(active)]
                send_msg(
                    "🤔 어떤 할일을 완료하셨나요?\n"
                    "리마인더에 답장으로 '완료'라고 보내주세요.\n\n"
                    + "\n".join(lines),
                    reply_to=msg_id,
                )
            elif not active:
                send_msg("📭 활성 할일이 없습니다.", reply_to=msg_id)
        return

    # ── 할일 등록 ──
    parsed = parse_todo_message(text, now)
    if not parsed["task"] and not parsed["deadline"]:
        return  # 일반 대화 무시

    project = detect_project(text)
    has_all = bool(parsed["task"] and parsed["deadline_dt"])

    todo = {
        "id": str(uuid.uuid4())[:8],
        "task": parsed["task"],
        "deadline": parsed["deadline_dt"],
        "deadline_display": parsed["deadline"],
        "project": project,
        "status": "active" if has_all else "pending_input",
        "created_at": now.isoformat(),
        "last_reminded_at": None,
        "last_daily_date": None,
        "original_message": text,
    }
    data["todos"].append(todo)

    if has_all:
        dl_str = format_deadline(parsed["deadline_dt"], now)
        proj_tag = f"\n📁 {project}" if project else ""
        send_msg(
            f"📝 등록 완료!\n\n"
            f"📌 <b>{parsed['task']}</b>\n"
            f"⏰ {dl_str}{proj_tag}",
            reply_to=msg_id,
        )
        print(f"[NEW] {parsed['task']} → {parsed['deadline']}")
    else:
        missing = []
        if not parsed["task"]:
            missing.append("할일 내용")
        if not parsed["deadline_dt"]:
            missing.append("데드라인")
        send_msg(
            f"⚠️ 부족한 정보: <b>{', '.join(missing)}</b>\n"
            f"형식: <code>[데드라인]까지 [할일]</code>\n"
            f"보완해주실 때까지 매 시간 리마인더 드립니다.",
            reply_to=msg_id,
        )
        print(f"[PENDING] {text}")


# ══════════════════════════════════════════════════════════
#  리마인더 로직
# ══════════════════════════════════════════════════════════

def format_deadline(deadline_iso: str, now: datetime) -> str:
    try:
        dl = datetime.fromisoformat(deadline_iso)
        diff = dl - now
        hours = diff.total_seconds() / 3600

        WEEKDAYS_KR = ["월", "화", "수", "목", "금", "토", "일"]
        wday = WEEKDAYS_KR[dl.weekday()]
        date_str = f"{dl.month}/{dl.day}({wday}) {dl.strftime('%H:%M')}"

        if hours < 0:
            return f"⚠️ {date_str} (기한 초과!)"
        elif hours < 1:
            return f"🔴 {date_str} ({int(diff.total_seconds() / 60)}분 남음)"
        elif hours < 12:
            return f"🔴 {date_str} ({hours:.0f}시간 남음)"
        elif hours < 24:
            return f"🟡 {date_str} ({hours:.0f}시간 남음)"
        elif diff.days < 3:
            return f"🟡 {date_str} ({diff.days}일 {int(hours % 24)}시간 남음)"
        else:
            return f"🟢 {date_str} ({diff.days}일 남음)"
    except Exception:
        return deadline_iso


def get_reminder_interval(deadline_iso: str, now: datetime) -> float | None:
    """
    남은 시간에 따른 리마인더 간격(시간).
    > 24h  → None (매일 8시에만)
    12~24h → 4시간
    <= 12h → 3시간
    """
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
        # 24시간 이상 → 매일 아침 8시만
        if now.hour < 7 or now.hour > 9:
            return False
        if todo.get("last_daily_date") == now.strftime("%Y-%m-%d"):
            return False
        return True
    else:
        # N시간 간격
        if todo.get("last_reminded_at"):
            elapsed = (now - datetime.fromisoformat(todo["last_reminded_at"])).total_seconds() / 3600
            if elapsed < interval - 0.5:
                return False
        return True


def should_remind_pending(todo: dict, now: datetime) -> bool:
    if todo["status"] != "pending_input":
        return False
    # 매 시간 정각 (±15분)
    if 15 < now.minute < 45:
        return False
    if todo.get("last_reminded_at"):
        elapsed = (now - datetime.fromisoformat(todo["last_reminded_at"])).total_seconds()
        if elapsed < 3000:
            return False
    return True


def send_reminder(todo: dict, data: dict, now: datetime):
    dl_str = format_deadline(todo["deadline"], now)
    proj = f"\n📁 {todo['project']}" if todo.get("project") else ""

    sent_id = send_msg(
        f"⏰ <b>리마인더</b>\n\n"
        f"📌 {todo['task']}\n"
        f"⏳ {dl_str}{proj}\n\n"
        f"<i>완료 시 이 메시지에 답장 → '완료'</i>"
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
        f"<code>[데드라인]까지 [할일]</code> 형식으로 보내주세요."
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
            send_msg("📭 활성 할일이 없습니다.")
        return

    def sort_key(t):
        if t.get("deadline"):
            try:
                return datetime.fromisoformat(t["deadline"])
            except Exception:
                pass
        return datetime.max.replace(tzinfo=KST)

    active.sort(key=sort_key)

    WEEKDAYS_KR = ["월", "화", "수", "목", "금", "토", "일"]
    wday = WEEKDAYS_KR[now.weekday()]
    lines = [f"📋 <b>오늘의 할일 ({now.month}/{now.day} {wday})</b>\n"]

    overdue = [t for t in active if t.get("deadline") and datetime.fromisoformat(t["deadline"]) < now]
    today_end = now.replace(hour=23, minute=59, second=59)
    today_due = [
        t for t in active
        if t.get("deadline")
        and now <= datetime.fromisoformat(t["deadline"]) <= today_end
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
                dl = format_deadline(t["deadline"], now)
                lines.append(f"  • {task}{proj}\n    {dl}")
            else:
                lines.append(f"  • {task}{proj}\n    ⚠️ 데드라인 미설정")
        lines.append("")

    lines.append(f"총 {len(active)}건 | /list")

    send_msg("\n".join(lines))
    data["last_summary_date"] = now.strftime("%Y-%m-%d")
    print(f"[SUMMARY] {len(active)}건")


# ══════════════════════════════════════════════════════════
#  정리
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


# ══════════════════════════════════════════════════════════
#  메인
# ══════════════════════════════════════════════════════════

def main():
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("[ERROR] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 환경변수 필요")
        sys.exit(1)

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
        if msg := update.get("message"):
            process_message(msg, data, now)

    # 2) 매일 7시 요약
    send_daily_summary(data, now)

    # 3) 개별 리마인더
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
