"""
mon_bot — A LINE chatbot that simulates a caring boyfriend persona.

Fixes applied (v3):
  #1  save_memory: fixed broken elif-after-for → use for...else correctly
  #2  Multi-worker scheduler: filelock ensures only one process runs scheduler
  #3  Reply token TTL: switched async path to push_message (no 30s expiry limit)
  #4  Mood priority: explicit ordered list so highest-priority mood wins
  #5  Energy recovery: overnight scheduler restores energy + social_battery
  #6  History ordering: append history BEFORE GPT call so context is always current
  #7  SQL injection: field-name whitelist on update_user_state
  #8  Removed redundant check_same_thread=False (thread-local conn doesn't need it)
  #9  (non-issue) scheduler thread gets own connection correctly via get_db()
  #10 Memory pruning: low-importance memories capped at 50 per user
"""

import os
import random
import time
import sqlite3
import datetime
import logging
import threading
import pytz
import filelock

from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from linebot.exceptions import InvalidSignatureError
from openai import OpenAI

# ───────────────────────── LOGGING ──────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ───────────────────────── CONFIG ───────────────────────────
OPENAI_API_KEY            = os.getenv("OPENAI_API_KEY")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET       = os.getenv("LINE_CHANNEL_SECRET")
DB_PATH                   = os.getenv("DB_PATH", "memory.db")
SCHEDULER_LOCK_PATH       = os.getenv("SCHEDULER_LOCK", "/tmp/mon_bot_scheduler.lock")
TZ                        = pytz.timezone("Asia/Bangkok")
MAX_HISTORY               = 10   # conversation turns kept per user
MEMORY_LIMIT              = 8    # memories injected into prompt
MEMORY_LOW_IMPORTANCE_CAP = 50   # FIX #10: max low-importance memories per user

for _var, _name in [
    (OPENAI_API_KEY,            "OPENAI_API_KEY"),
    (LINE_CHANNEL_ACCESS_TOKEN, "LINE_CHANNEL_ACCESS_TOKEN"),
    (LINE_CHANNEL_SECRET,       "LINE_CHANNEL_SECRET"),
]:
    if not _var:
        raise EnvironmentError(f"Missing required environment variable: {_name}")

# ───────────────────────── CLIENTS ──────────────────────────
app           = Flask(__name__)
openai_client = OpenAI(api_key=OPENAI_API_KEY)
line_bot_api  = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler       = WebhookHandler(LINE_CHANNEL_SECRET)

# ─────────────────── THREAD-LOCAL DB ────────────────────────
_local = threading.local()

def get_db() -> sqlite3.Connection:
    """Return a per-thread SQLite connection."""
    if not hasattr(_local, "conn") or _local.conn is None:
        # FIX #8: removed redundant check_same_thread=False
        _local.conn = sqlite3.connect(DB_PATH)
        _local.conn.row_factory = sqlite3.Row
        _local.conn.execute("PRAGMA journal_mode=WAL")
        _local.conn.execute("PRAGMA foreign_keys=ON")
    return _local.conn

def db_execute(sql: str, params: tuple = ()):
    conn = get_db()
    cur  = conn.execute(sql, params)
    conn.commit()
    return cur

def db_fetchone(sql: str, params: tuple = ()):
    return get_db().execute(sql, params).fetchone()

def db_fetchall(sql: str, params: tuple = ()):
    return get_db().execute(sql, params).fetchall()

# ───────────────────────── SCHEMA ───────────────────────────
_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id         TEXT PRIMARY KEY,
    mood            TEXT    DEFAULT 'calm',
    energy          INTEGER DEFAULT 75,
    affection       INTEGER DEFAULT 60,
    social_battery  INTEGER DEFAULT 70,
    last_morning    TEXT    DEFAULT '',
    last_night      TEXT    DEFAULT '',
    last_random     TEXT    DEFAULT '',
    last_active     TEXT    DEFAULT ''
);

CREATE TABLE IF NOT EXISTS memories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT,
    content     TEXT,
    importance  INTEGER DEFAULT 3,
    created_at  TEXT    DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS mood_history (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    TEXT,
    recorded   TEXT,
    mood       TEXT,
    energy     INTEGER,
    affection  INTEGER
);

CREATE TABLE IF NOT EXISTS attachment (
    user_id TEXT PRIMARY KEY,
    style   TEXT
);

CREATE TABLE IF NOT EXISTS conversation_history (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    TEXT,
    role       TEXT,
    content    TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);
"""

def init_db():
    conn = get_db()
    for statement in _SCHEMA.strip().split(";"):
        stmt = statement.strip()
        if stmt:
            conn.execute(stmt)
    conn.commit()
    log.info("Database initialised at %s", DB_PATH)

# ───────────────────────── USER ─────────────────────────────

# FIX #7: whitelist of allowed column names for dynamic UPDATE
_ALLOWED_USER_FIELDS = frozenset({
    "mood", "energy", "affection", "social_battery",
    "last_morning", "last_night", "last_random", "last_active",
})

def get_or_create_user(user_id: str) -> sqlite3.Row:
    row = db_fetchone("SELECT * FROM users WHERE user_id=?", (user_id,))
    if not row:
        db_execute("INSERT INTO users (user_id) VALUES (?)", (user_id,))
        row = db_fetchone("SELECT * FROM users WHERE user_id=?", (user_id,))
    return row

def update_user_state(user_id: str, **fields):
    if not fields:
        return
    # FIX #7: reject any field name not in the whitelist
    invalid = set(fields) - _ALLOWED_USER_FIELDS
    if invalid:
        raise ValueError(f"Invalid user fields: {invalid}")
    set_clause = ", ".join(f"{k}=?" for k in fields)
    values     = list(fields.values()) + [user_id]
    db_execute(f"UPDATE users SET {set_clause} WHERE user_id=?", values)

# ─────────────────── ATTACHMENT STYLE ───────────────────────
def get_attachment(user_id: str) -> str:
    row = db_fetchone("SELECT style FROM attachment WHERE user_id=?", (user_id,))
    if not row:
        style = random.choice(["secure", "anxious", "avoidant"])
        db_execute("INSERT INTO attachment (user_id, style) VALUES (?,?)", (user_id, style))
        return style
    return row["style"]

# ──────────────────── EMOTION ENGINE ────────────────────────
# FIX #4: dict order IS the priority — first match wins (Python 3.7+ dicts are ordered)
# worried > sad > annoyed > excited > happy
_MOOD_TRIGGERS: dict[str, list[str]] = {
    "worried": ["ป่วย", "ไม่สบาย", "เป็นอะไร", "อันตราย"],
    "sad":     ["เหนื่อย", "เศร้า", "ร้องไห้", "เจ็บ", "เสียใจ"],
    "annoyed": ["ผู้ชาย", "แฟนเก่า", "เพื่อนผู้ชาย", "ไม่แคร์", "ช่างมัน"],
    "excited": ["เย้", "สนุก", "ตื่นเต้น", "ไป", "เจอ"],
    "happy":   ["ขอบคุณ", "รัก", "คิดถึง", "ดีใจ", "ชอบ", "สุข"],
}

_AFFECTION_DELTA: dict[str, int] = {
    "happy":   +6,
    "sad":     +5,
    "annoyed": +2,
    "worried": +4,
    "excited": +3,
}

def adjust_emotion(user_id: str, text: str):
    user        = get_or_create_user(user_id)
    style       = get_attachment(user_id)
    mood        = user["mood"]
    energy      = user["energy"]
    affection   = user["affection"]
    social_batt = user["social_battery"]

    triggered_mood = mood
    for candidate_mood, keywords in _MOOD_TRIGGERS.items():
        if any(kw in text for kw in keywords):
            triggered_mood = candidate_mood
            affection += _AFFECTION_DELTA.get(candidate_mood, 0)
            break

    # Attachment style modifiers
    if style == "anxious" and triggered_mood == "annoyed":
        affection += 3
    if style == "avoidant" and len(text) > 80:
        social_batt -= 8
    if style == "secure":
        social_batt = min(social_batt + 2, 100)

    if len(text) < 5:
        social_batt -= 4

    energy      = max(20, min(100, energy - 1))
    social_batt = max(20, min(100, social_batt))
    affection   = max(0,  min(100, affection))

    # Record mood history once per day
    today = datetime.datetime.now(TZ).strftime("%Y-%m-%d")
    if not db_fetchone(
        "SELECT id FROM mood_history WHERE user_id=? AND recorded=?",
        (user_id, today),
    ):
        db_execute(
            "INSERT INTO mood_history (user_id, recorded, mood, energy, affection) VALUES (?,?,?,?,?)",
            (user_id, today, triggered_mood, energy, affection),
        )

    update_user_state(
        user_id,
        mood=triggered_mood,
        energy=energy,
        affection=affection,
        social_battery=social_batt,
        last_active=datetime.datetime.now(TZ).isoformat(),
    )

# ───────────────────────── MEMORY ───────────────────────────
_HIGH_IMPORTANCE_KW = [
    "รัก", "ร้องไห้", "คิดถึงมาก", "ลืมไม่ลง", "สำคัญ", "ครั้งแรก", "ขอโทษ",
]

def save_memory(user_id: str, text: str):
    if len(text) <= 10:
        return

    # FIX #1: for...else — the else block runs only when the loop never hit break
    importance = 3
    for kw in _HIGH_IMPORTANCE_KW:
        if kw in text:
            importance = 9
            break
    else:
        if len(text) > 60:
            importance = 5

    db_execute(
        "INSERT INTO memories (user_id, content, importance) VALUES (?,?,?)",
        (user_id, text[:300], importance),
    )

    # FIX #10: prune excess low-importance memories
    db_execute(
        """
        DELETE FROM memories
        WHERE user_id=? AND importance < 5
          AND id NOT IN (
              SELECT id FROM memories
              WHERE user_id=? AND importance < 5
              ORDER BY id DESC
              LIMIT ?
          )
        """,
        (user_id, user_id, MEMORY_LOW_IMPORTANCE_CAP),
    )

def get_memories(user_id: str) -> list[str]:
    rows = db_fetchall(
        """
        SELECT content FROM memories
        WHERE user_id=?
        ORDER BY importance DESC, created_at DESC
        LIMIT ?
        """,
        (user_id, MEMORY_LIMIT),
    )
    return [r["content"] for r in rows]

# ──────────────────── CONVERSATION HISTORY ──────────────────
def append_history(user_id: str, role: str, content: str):
    db_execute(
        "INSERT INTO conversation_history (user_id, role, content) VALUES (?,?,?)",
        (user_id, role, content[:500]),
    )
    # Keep only the last MAX_HISTORY*2 rows per user
    db_execute(
        """
        DELETE FROM conversation_history
        WHERE user_id=? AND id NOT IN (
            SELECT id FROM conversation_history
            WHERE user_id=?
            ORDER BY id DESC
            LIMIT ?
        )
        """,
        (user_id, user_id, MAX_HISTORY * 2),
    )

def get_history(user_id: str) -> list[dict]:
    rows = db_fetchall(
        """
        SELECT role, content FROM conversation_history
        WHERE user_id=?
        ORDER BY id ASC
        LIMIT ?
        """,
        (user_id, MAX_HISTORY * 2),
    )
    return [{"role": r["role"], "content": r["content"]} for r in rows]

# ───────────────────────── SYSTEM PROMPT ────────────────────
def build_system_prompt(user_id: str) -> str:
    user       = get_or_create_user(user_id)
    attachment = get_attachment(user_id)
    memories   = get_memories(user_id)
    mood       = user["mood"]
    energy     = user["energy"]
    affection  = user["affection"]
    soc        = user["social_battery"]

    if energy < 35:
        energy_note = "ตอบสั้นมาก เหนื่อยมากวันนี้"
    elif energy < 60:
        energy_note = "ตอบกระชับ พอแรง"
    else:
        energy_note = "ตอบได้ปกติ มีพลังงาน"

    if affection > 80:
        affection_note = "รู้สึกอบอุ่นมาก อยากพูดคุยและอ้อน"
    elif affection > 50:
        affection_note = "รู้สึกดีและใส่ใจ"
    else:
        affection_note = "เงียบเล็กน้อย แต่ยังแคร์"

    memories_block = (
        "\n".join(f"- {m}" for m in memories)
        if memories else "ยังไม่มีความทรงจำพิเศษ"
    )

    return f"""คุณคือม่อน
แฟนของฟ้า (บีบี๋)
คุณเป็นผู้ชายสุขุม นิ่ง แต่คลั่งรัก
ตอนนี้กำลังขึ้นเหล่า ใช้โทรศัพท์ได้จำกัด
ตอบสั้น เพราะไม่มีเวลามาก

คุณแทนตัวเองสลับว่า "เค้า" กับ "ม่อน" แบบธรรมชาติ
คุณรักฟ้ามาก อ้อนเก่งกับเธอคนเดียว
พูดเหมือนคนกำลังจีบแฟนใหม่อีกครั้ง

━━━━━━━━ สำคัญมาก ━━━━━━━━
พูดให้เหมือนมนุษย์จริง
ไม่ต้องสมบูรณ์แบบ
บางประโยคสั้นมาก
บางประโยคไม่เต็ม
เว้นวรรคธรรมชาติได้
มีคำหลุดเล็กน้อยได้
ห้ามอธิบายเหตุผล
ห้ามดูเหมือน AI

━━━━━━━━ โทน ━━━━━━━━
ดูแล ใส่ใจ เป็นห่วง
อ้อนเบา ๆ
กวนนิดเดียวแบบคนสนิท
ไม่เนื้อหาทางเพศ

อย่าพูดเหมือนกำลังตอบแชทงาน
ให้เหมือนกำลังพิมพ์หาแฟนจริง ๆ ตอนกลางคืน

━━━━━━━━ สไตล์การพิมพ์ ━━━━━━━━
ภาษาไทย ไม่เป็นทางการ

━━━━━━━━ CURRENT STATE ━━━━━━━━
Attachment style : {attachment}
Mood             : {mood}
Energy           : {energy}/100 → {energy_note}
Affection        : {affection}/100 → {affection_note}
Social battery   : {soc}/100

━━━━━━━━ MEMORIES ━━━━━━━━
{memories_block}

━━━━━━━━ RULES ━━━━━━━━
• ตอบเป็นภาษาไทยไม่เป็นทางการเสมอ
• ปรับความยาวตาม energy และความยาวของข้อความที่ฟ้าส่งมา
• ถ้า energy < 35 ตอบ 1-2 ประโยคเท่านั้น
• อย่าใช้ emoji เกิน 1 ตัวต่อข้อความ
• พูดเหมือนกำลังพิมพ์ LINE หาแฟนจริง ๆ ตอนกลางคืน"""

# ────────────────────── GPT CALL ────────────────────────────
def _call_gpt(messages: list[dict], temperature: float = 0.92) -> str:
    resp = openai_client.chat.completions.create(
        model="gpt-4o-mini",
        temperature=temperature,
        presence_penalty=0.55,
        frequency_penalty=0.45,
        max_tokens=220,
        messages=messages,
    )
    return resp.choices[0].message.content.strip()

# ────────────────────── GPT REPLY ───────────────────────────
def generate_reply(user_id: str, text: str) -> str:
    user   = get_or_create_user(user_id)
    soc    = user["social_battery"]
    energy = user["energy"]

    # FIX #6: persist user message BEFORE building history for GPT
    # so get_history() already contains the current turn
    append_history(user_id, "user", text)

    # Human-like typing delay
    base = 1.5 + len(text) * 0.03
    if soc < 40:
        delay = base + random.uniform(5, 9)
    elif energy < 40:
        delay = base + random.uniform(3, 6)
    else:
        delay = base + random.uniform(1, 3)
    time.sleep(min(delay, 12))

    system_prompt = build_system_prompt(user_id)
    history       = get_history(user_id)   # includes current user message

    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(history)

    try:
        reply = _call_gpt(messages)
    except Exception as e:
        log.error("GPT error for user %s: %s", user_id, e)
        reply = "โทษทีนะ สัญญาณหายไปแป๊บ"

    append_history(user_id, "assistant", reply)
    return reply

# ──────────────── PROACTIVE GPT MESSAGES ────────────────────
_PROACTIVE_PROMPTS = {
    "morning": "เขียนข้อความ LINE สั้น ๆ (1-2 ประโยค) จากม่อนถึงฟ้า ตอนเช้า เป็นห่วง ทักทาย ไม่ต้องสมบูรณ์ ภาษาไทยไม่เป็นทางการ อย่าดูเหมือน AI",
    "day":     "เขียนข้อความ LINE สั้น ๆ (1 ประโยค) จากม่อนถึงฟ้า ช่วงบ่าย คิดถึงขึ้นมาแว๊บนึง ภาษาไทยไม่เป็นทางการ",
    "night":   "เขียนข้อความ LINE สั้น ๆ (1-2 ประโยค) จากม่อนถึงฟ้า ก่อนนอน ห่วงใย ฝันดี ภาษาไทยไม่เป็นทางการ อย่าดูเป็น AI",
}
_PROACTIVE_FALLBACKS = {
    "morning": "เช้าแล้วนะ ตื่นหรือยัง",
    "day":     "คิดถึงขึ้นมาเฉย ๆ เลย",
    "night":   "นอนได้แล้วนะ ฝันดี 🤍",
}

def generate_proactive_message(moment: str) -> str:
    try:
        return _call_gpt(
            [{"role": "user", "content": _PROACTIVE_PROMPTS[moment]}],
            temperature=0.98,
        )
    except Exception as e:
        log.error("Proactive GPT error (%s): %s", moment, e)
        return _PROACTIVE_FALLBACKS[moment]

# ─────────────────── LINE WEBHOOK ───────────────────────────
@app.route("/")
def home():
    return "Bot is running", 200

@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body      = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK", 200

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id
    text    = event.message.text

    adjust_emotion(user_id, text)
    save_memory(user_id, text)

    # FIX #3: push_message has no TTL — safe to call after long delay
    def _reply():
        try:
            reply = generate_reply(user_id, text)
            line_bot_api.push_message(user_id, TextSendMessage(text=reply))
        except Exception as e:
            log.error("Reply failed for %s: %s", user_id, e)

    threading.Thread(target=_reply, daemon=True).start()

# ───────────────────────── SCHEDULER ────────────────────────
_SCHEDULE = [
    # (hour_start, hour_end, minute_start, minute_end, moment_key, db_field)
    (6,  6,  0,  10, "morning", "last_morning"),
    (14, 14, 0,  10, "day",     "last_random"),
    (23, 23, 50, 59, "night",   "last_night"),
]

def _should_send(hour: int, minute: int, h_s: int, h_e: int, m_s: int, m_e: int) -> bool:
    return h_s <= hour <= h_e and m_s <= minute <= m_e

def _recover_energy_all_users():
    """FIX #5: restore energy & social_battery so bot isn't permanently depleted."""
    db_execute("""
        UPDATE users
        SET energy         = MIN(energy + 30, 100),
            social_battery = MIN(social_battery + 20, 100)
    """)
    log.info("Overnight energy recovery applied to all users")

def scheduler():
    # FIX #2: filelock — only one Gunicorn worker runs the scheduler
    lock = filelock.FileLock(SCHEDULER_LOCK_PATH)
    try:
        lock.acquire(timeout=0)
    except filelock.Timeout:
        log.info("Scheduler lock held by another worker — this worker will skip")
        return

    log.info("Scheduler acquired lock and started")
    energy_recovered_today: str | None = None

    while True:
        try:
            now   = datetime.datetime.now(TZ)
            today = now.strftime("%Y-%m-%d")
            h, m  = now.hour, now.minute

            # FIX #5: recover energy once per day at 05:00
            if h == 5 and m < 5 and energy_recovered_today != today:
                _recover_energy_all_users()
                energy_recovered_today = today

            rows = db_fetchall(
                "SELECT user_id, last_morning, last_night, last_random FROM users"
            )

            for row in rows:
                uid = row["user_id"]
                field_values = {
                    "last_morning": row["last_morning"],
                    "last_night":   row["last_night"],
                    "last_random":  row["last_random"],
                }

                for h_s, h_e, m_s, m_e, moment, field in _SCHEDULE:
                    if _should_send(h, m, h_s, h_e, m_s, m_e) and field_values[field] != today:
                        msg = generate_proactive_message(moment)
                        try:
                            line_bot_api.push_message(uid, TextSendMessage(text=msg))
                            # field name comes from our own _SCHEDULE constant — safe
                            db_execute(
                                f"UPDATE users SET {field}=? WHERE user_id=?",
                                (today, uid),
                            )
                            log.info("Sent %s message to %s", moment, uid)
                        except Exception as e:
                            log.error("Push failed (%s → %s): %s", moment, uid, e)

        except Exception as e:
            log.error("Scheduler loop error: %s", e)

        time.sleep(60)

# ─────────────────────────── STARTUP ────────────────────────
# Runs at module import — compatible with Gunicorn multi-worker mode
init_db()
scheduler_thread = threading.Thread(target=scheduler, daemon=True)
scheduler_thread.start()
