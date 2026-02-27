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


# ================= HEALTH CHECK =================

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

conn.commit()

# ================= USER =================

def get_user(user_id):
    cursor.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
    user = cursor.fetchone()

    if not user:
        cursor.execute(
            "INSERT INTO users VALUES (?, 'calm', 75, 60, 70, '', '')",
            (user_id,)
        )
        conn.commit()
        return ('calm', 75, 60, 70, '', '')

    return user[1:]

def update_user(user_id, mood, energy, affection, social_battery):
    cursor.execute("""
    UPDATE users SET mood=?, energy=?, affection=?, social_battery=?
    WHERE user_id=?
    """, (mood, energy, affection, social_battery, user_id))
    conn.commit()

# ================= EMOTION =================

def adjust_emotion(user_id, text):
    mood, energy, affection, social_battery, lm, ln = get_user(user_id)

    if "เหนื่อย" in text or "เศร้า" in text:
        affection += 5
        mood = "calm"

    if "คิดถึง" in text:
        affection += 3

    if len(text) < 5:
        social_battery -= 5

    energy = max(20, min(100, energy))
    affection = max(0, min(100, affection))
    social_battery = max(20, min(100, social_battery))

    update_user(user_id, mood, energy, affection, social_battery)

# ================= MEMORY =================

def save_memory(user_id, text):
    importance = 3
    if "รัก" in text:
        importance = 8

    if len(text) > 15:
        cursor.execute(
            "INSERT INTO memories VALUES (?,?,?)",
            (user_id, text, importance)
        )
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

# ================= GPT =================

def generate_reply(user_id, text):
    mood, energy, affection, social_battery, lm, ln = get_user(user_id)
    memories = get_memories(user_id)

    delay = random.uniform(2, 5)
    time.sleep(delay)

    system_prompt = f"""
คุณเป็นแฟนผู้ใหญ่ สุขุม นิ่ง
พูดภาษาไทย
soft dominance
ไม่หวานเลี่ยน
ไม่เหมือนหุ่นยนต์

Mood: {mood}
Energy: {energy}
Affection: {affection}
Memories: {memories}

ตอบธรรมชาติ ไม่สมบูรณ์แบบเกินไป
"""

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text}
        ]
    )

    return response.choices[0].message.content

# ================= LINE =================

@app.route("/callback", methods=['POST'])
def callback():
    print("Webhook received")

    signature = request.headers.get('X-Line-Signature')
    body = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        print("Invalid signature")
        abort(400)
    except Exception as e:
        print("Error:", e)
        abort(500)

    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    try:
        user_id = event.source.user_id
        text = event.message.text

        adjust_emotion(user_id, text)
        save_memory(user_id, text)

        reply = generate_reply(user_id, text)

        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=reply)
        )
    except Exception as e:
        print("Handler error:", e)

# ================= RUN =================

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
