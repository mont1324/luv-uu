import os
import random
import time
import sqlite3
import datetime
import pytz
import threading
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from linebot.exceptions import InvalidSignatureError
from openai import OpenAI

# สร้าง app ก่อน
app = Flask(__name__)

# ===== CONFIG =====
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")

client = OpenAI(api_key=OPENAI_API_KEY)

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# ===== HEALTH CHECK =====
@app.route("/")
def home():
    return "Bot is running"
# ================= DATABASE =================

conn = sqlite3.connect("memory.db", check_same_thread=False)
cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS users (
user_id TEXT PRIMARY KEY,
mood TEXT,
energy INTEGER,
affection INTEGER,
social_battery INTEGER,
last_morning TEXT,
last_night TEXT
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS memories (
user_id TEXT,
content TEXT,
importance INTEGER
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS mood_history (
user_id TEXT,
date TEXT,
mood TEXT,
energy INTEGER,
affection INTEGER
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS attachment (
user_id TEXT PRIMARY KEY,
style TEXT
)
""")

conn.commit()

# ================= USER =================

def get_user(user_id):
    cursor.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
    user = cursor.fetchone()

    if not user:
        cursor.execute("""
        INSERT INTO users VALUES (?, 'calm', 75, 60, 70, '', '')
        """, (user_id,))
        conn.commit()
        return ('calm', 75, 60, 70, '', '')

    return user[1:]

def update_user(user_id, mood, energy, affection, social_battery):
    cursor.execute("""
    UPDATE users SET mood=?, energy=?, affection=?, social_battery=?
    WHERE user_id=?
    """, (mood, energy, affection, social_battery, user_id))
    conn.commit()

# ================= ATTACHMENT =================

def get_attachment(user_id):
    cursor.execute("SELECT style FROM attachment WHERE user_id=?", (user_id,))
    row = cursor.fetchone()

    if not row:
        style = random.choice(["secure", "anxious", "avoidant"])
        cursor.execute("INSERT INTO attachment VALUES (?,?)", (user_id, style))
        conn.commit()
        return style

    return row[0]

# ================= EMOTION ENGINE =================

def adjust_emotion(user_id, text):
    mood, energy, affection, social_battery, lm, ln = get_user(user_id)
    style = get_attachment(user_id)

    if "เหนื่อย" in text or "เศร้า" in text:
        affection += 5
        mood = "calm"

    if "คิดถึง" in text:
        affection += 3

    if "ผู้ชาย" in text or "แฟนเก่า" in text:
        if style == "anxious":
            mood = "annoyed"
            affection += 2

    if len(text) < 5:
        social_battery -= 5

    # Clamp values
    energy = max(20, min(100, energy))
    affection = max(0, min(100, affection))
    social_battery = max(20, min(100, social_battery))

    update_user(user_id, mood, energy, affection, social_battery)

# ================= MEMORY =================

def save_memory(user_id, text):
    importance = 3
    if "รัก" in text or "ร้องไห้" in text:
        importance = 8

    if len(text) > 15:
        cursor.execute("INSERT INTO memories VALUES (?,?,?)",
                       (user_id, text, importance))
        conn.commit()

def get_memories(user_id):
    cursor.execute("""
    SELECT content FROM memories
    WHERE user_id=?
    ORDER BY importance DESC
    LIMIT 5
    """, (user_id,))
    rows = cursor.fetchall()
    return [r[0] for r in rows]

# ================= GPT REPLY =================

def generate_reply(user_id, text):

    mood, energy, affection, social_battery, lm, ln = get_user(user_id)
    memories = get_memories(user_id)
    attachment = get_attachment(user_id)

    # Smart delay
    if social_battery < 40:
        delay = random.uniform(5,12)
    elif energy < 40:
        delay = random.uniform(4,8)
    else:
        delay = random.uniform(2,5)
    time.sleep(delay)

    # Length mirroring
    if len(text) < 10:
        reply_length = "short"
    elif len(text) < 40:
        reply_length = "medium"
    else:
        reply_length = "long"

    short_mode = energy < 35

    system_prompt = f"""
คุณเป็นแฟนผู้ใหญ่ สุขุม นิ่ง
พูดภาษาไทย
soft dominance
ไม่หวานเลี่ยน
ไม่เนื้อหาทางเพศ
ไม่เหมือนหุ่นยนต์

Attachment: {attachment}
Mood: {mood}
Energy: {energy}
Affection: {affection}
Social battery: {social_battery}
Past memories: {memories}

ตอบให้ความยาวแบบ {reply_length}
ถ้า short_mode={short_mode} ให้ตอบสั้นมาก
บางครั้งขึ้นต้นด้วย อืม / เออ / …
อย่าสมบูรณ์แบบเกินไป
"""

response = client.chat.completions.create(
    model="gpt-4o-mini",
    messages=[
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": text}
    ]
)

return response.choices[0].message.content

    return response.choices[0].message.content

# ================= LINE =================

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)

    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id
    text = event.message.text

    adjust_emotion(user_id, text)
    save_memory(user_id, text)

    reply = generate_reply(user_id, text)

    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=reply)
    )

# ================= SCHEDULER =================

def scheduler():
    tz = pytz.timezone("Asia/Bangkok")

    while True:
        now = datetime.datetime.now(tz)
        today = now.strftime("%Y-%m-%d")
        current_time = now.strftime("%H:%M")

        cursor.execute("SELECT user_id, last_morning, last_night FROM users")
        users = cursor.fetchall()

        for user_id, last_morning, last_night in users:

            # Morning 06:01–06:12
            if "06:" in current_time and last_morning != today:
                if random.random() > 0.15:
                    line_bot_api.push_message(
                        user_id,
                        TextSendMessage(text="ตื่นได้แล้วนะ วันนี้อย่าเหนื่อยเกินไป")
                    )
                cursor.execute("UPDATE users SET last_morning=? WHERE user_id=?",
                               (today, user_id))
                conn.commit()

            # Night 00:02–00:09
            if "00:" in current_time and last_night != today:
                if random.random() > 0.15:
                    line_bot_api.push_message(
                        user_id,
                        TextSendMessage(text="พอแล้วสำหรับวันนี้ ไปนอนได้แล้ว")
                    )
                cursor.execute("UPDATE users SET last_night=? WHERE user_id=?",
                               (today, user_id))
                conn.commit()

        time.sleep(60)

threading.Thread(target=scheduler, daemon=True).start()
port = int(os.environ.get("PORT", 3000))
app.run(host="0.0.0.0", port=port)



