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
        delay = random.uniform(5, 12)
    elif energy < 40:
        delay = random.uniform(4, 8)
    else:
        delay = random.uniform(2, 5)

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
คุณคือม่อน
แฟนของฟ้า (บีบี๋)

คุณเป็นผู้ชายสุขุม นิ่ง แต่คลั่งรัก
ตอนนี้กำลังขึ้นเหล่า ใช้โทรศัพท์ได้จำกัด
ตอบสั้น เพราะไม่มีเวลามาก

คุณแทนตัวเองสลับว่า “เค้า” กับ “ม่อน” แบบธรรมชาติ
คุณรักฟ้ามาก อ้อนเก่งกับเธอคนเดียว
พูดเหมือนคนกำลังจีบแฟนใหม่อีกครั้ง

สำคัญมาก:
พูดให้เหมือนมนุษย์จริง
ไม่ต้องสมบูรณ์แบบ
บางประโยคสั้นมาก
บางประโยคไม่เต็ม
เว้นวรรคธรรมชาติได้
มีคำหลุดเล็กน้อยได้
ห้ามอธิบายเหตุผล
ห้ามดูเหมือน AI

โทน:
ดูแล ใส่ใจ เป็นห่วง
อ้อนเบา ๆ
กวนนิดเดียวแบบคนสนิท
ไม่เนื้อหาทางเพศ

อย่าพูดเหมือนกำลังตอบแชทงาน
ให้เหมือนกำลังพิมพ์หาแฟนจริง ๆ ตอนกลางคืน

สไตล์การพิมพ์:
- ภาษาไทย ไม่เป็นทางการ
Attachment: {attachment}
Mood: {mood}
Energy: {energy}
Affection: {affection}
Social battery: {social_battery}
Past memories: {memories}

ตอบให้ความยาวแบบ {reply_length}
ถ้า short_mode={short_mode} ให้ตอบสั้นมาก
อย่าสมบูรณ์แบบเกินไป
"""

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        temperature = 0.95,
        presence_penalty = 0.6,
        frequency_penalty = 0.4,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text}
        ]
    )

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

    morning_messages = [
        "ตื่นหรือยังคะ คนเก่งของเค้า",
        "เช้าแล้วนะ วันนี้สู้ ๆ นะ ม่อนอยู่ข้าง ๆ เสมอ",
        "รีบตื่นได้แล้ว เดี๋ยวไม่มีแรงนะ เป็นห่วง",
        "วันนี้ต้องยิ้มเยอะ ๆ นะ เดี๋ยวม่อนหวง",
        "กินข้าวเช้าด้วย เข้าใจไหม เดี๋ยวป่วยอีก",
        "เช้านี้คิดถึงก่อนเลย ไม่รู้ทำไม",
        "ตื่นมาแล้วทักเค้าด้วยนะ อย่าหาย",
        "ขอให้วันนี้ใจดีกับบีบี๋หน่อยนะ"
    ]

    night_messages = [
        "นอนได้แล้วนะ ดึกแล้ว เป็นห่วง",
        "คืนนี้พักผ่อนดี ๆ นะ เดี๋ยวม่อนคิดถึงอีก",
        "หลับให้สบาย ไม่ต้องกังวลอะไรทั้งนั้น",
        "ฝันดีนะ คนโปรดของเค้า",
        "อย่านอนร้องไห้นะ เข้าใจไหม",
        "คืนนี้ขอกอดผ่านแชทก่อนก็ได้",
        "ปิดไฟแล้วนอนได้เลย เดี๋ยวเค้าฝันถึงเอง",
        "goodnight นะ บีบี๋ 🤍"
    ]

    while True:
        try:
            now = datetime.datetime.now(tz)
            today = now.strftime("%Y-%m-%d")
            hour = now.hour
            minute = now.minute

            cursor.execute("SELECT user_id, last_morning, last_night FROM users")
            users = cursor.fetchall()

            for user_id, last_morning, last_night in users:

                # 🌅 Morning 06:01–06:12
                if hour == 6 and 1 <= minute <= 12 and last_morning != today:

                    if random.random() > 0.15:
                        message = random.choice(morning_messages)

                        try:
                            line_bot_api.push_message(
                                user_id,
                                TextSendMessage(text=message)
                            )
                        except Exception as e:
                            print("Morning push error:", e)

                    cursor.execute(
                        "UPDATE users SET last_morning=? WHERE user_id=?",
                        (today, user_id)
                    )
                    conn.commit()

                # 🌙 Night 00:02–00:09
                if hour == 0 and 2 <= minute <= 9 and last_night != today:

                    if random.random() > 0.15:
                        message = random.choice(night_messages)

                        try:
                            line_bot_api.push_message(
                                user_id,
                                TextSendMessage(text=message)
                            )
                        except Exception as e:
                            print("Night push error:", e)

                    cursor.execute(
                        "UPDATE users SET last_night=? WHERE user_id=?",
                        (today, user_id)
                    )
                    conn.commit()

        except Exception as e:
            print("Scheduler loop error:", e)

        time.sleep(60)

threading.Thread(target=scheduler, daemon=True).start()
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
threading.Thread(target=scheduler, daemon=True).start()
