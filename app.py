import os
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage, JoinEvent, LeaveEvent
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import date, datetime, timedelta
import psycopg2

app = Flask(__name__)

LINE_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
LINE_SECRET = os.environ.get("LINE_CHANNEL_SECRET")
DATABASE_URL = os.environ.get("DATABASE_URL")

line_bot_api = LineBotApi(LINE_TOKEN)
handler = WebhookHandler(LINE_SECRET)


def get_db():
    return psycopg2.connect(DATABASE_URL, sslmode="require")


def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS projects (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            deadline DATE NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS groups (
            group_id TEXT PRIMARY KEY
        )
    """)
    conn.commit()
    cur.close()
    conn.close()


@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers["X-Line-Signature"]
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"


# 機器人被加進群組時，記錄群組 ID
@handler.add(JoinEvent)
def handle_join(event):
    if event.source.type == "group":
        group_id = event.source.group_id
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO groups (group_id) VALUES (%s) ON CONFLICT DO NOTHING",
            (group_id,)
        )
        conn.commit()
        cur.close()
        conn.close()


# 機器人被踢出群組時，移除群組 ID
@handler.add(LeaveEvent)
def handle_leave(event):
    if event.source.type == "group":
        group_id = event.source.group_id
        conn = get_db()
        cur = conn.cursor()
        cur.execute("DELETE FROM groups WHERE group_id = %s", (group_id,))
        conn.commit()
        cur.close()
        conn.close()


# 只接受私訊的管理指令，群組訊息一律忽略
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    if event.source.type != "user":
        return

    user_id = event.source.user_id
    text = event.message.text.strip()

    if text.startswith("新增 "):
        parts = text.split(" ", 2)
        if len(parts) == 3:
            name, deadline_str = parts[1], parts[2]
            try:
                deadline = datetime.strptime(deadline_str, "%Y-%m-%d").date()
                conn = get_db()
                cur = conn.cursor()
                cur.execute(
                    "INSERT INTO projects (name, deadline) VALUES (%s, %s) RETURNING id",
                    (name, deadline)
                )
                pid = cur.fetchone()[0]
                conn.commit()
                cur.close()
                conn.close()
                days_left = (deadline - date.today()).days
                reply = (
                    f"✅ 專案已新增！\n"
                    f"編號：#{pid}\n"
                    f"專案：{name}\n"
                    f"截止日：{deadline.month}/{deadline.day}\n"
                    f"距今還有 {days_left} 天"
                )
            except ValueError:
                reply = "日期格式錯誤，請用：\n新增 專案名稱 YYYY-MM-DD\n例：新增 臨終問答一校 2024-05-31"
        else:
            reply = "格式：新增 專案名稱 YYYY-MM-DD\n例：新增 臨終問答一校 2024-05-31"

    elif text == "查看":
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT id, name, deadline FROM projects ORDER BY deadline")
        rows = cur.fetchall()
        cur.close()
        conn.close()
        if rows:
            today = date.today()
            lines = []
            for pid, name, deadline in rows:
                days_left = (deadline - today).days
                if days_left < 0:
                    status = f"已過截止 {abs(days_left)} 天"
                elif days_left == 0:
                    status = "今天截止！"
                else:
                    status = f"還有 {days_left} 天"
                lines.append(f"#{pid} {name}\n　{deadline.month}/{deadline.day}（{status}）")
            reply = "📋 專案清單：\n\n" + "\n\n".join(lines)
        else:
            reply = "目前沒有任何專案。"

    elif text.startswith("刪除 "):
        try:
            pid = int(text.split(" ")[1])
            conn = get_db()
            cur = conn.cursor()
            cur.execute("DELETE FROM projects WHERE id = %s RETURNING name", (pid,))
            row = cur.fetchone()
            conn.commit()
            cur.close()
            conn.close()
            reply = f"✅ 已刪除專案 #{pid}「{row[0]}」" if row else f"找不到專案 #{pid}。"
        except (ValueError, IndexError):
            reply = "格式：刪除 編號\n例：刪除 1"

    else:
        reply = (
            "📌 指令說明（私訊使用）：\n\n"
            "• 新增 專案名稱 YYYY-MM-DD\n"
            "  例：新增 臨終問答一校 2024-05-31\n\n"
            "• 查看\n\n"
            "• 刪除 編號\n"
            "  例：刪除 1\n\n"
            "⏰ 提醒時機（推播到群組）：\n"
            "截止前 7天、5天、3天（早上 9:00）\n"
            "截止當天（早上 8:00）"
        )

    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=reply)
    )


def send_reminders():
    today = date.today()
    now_hour = datetime.now().hour

    conn = get_db()
    cur = conn.cursor()

    # 取得所有群組
    cur.execute("SELECT group_id FROM groups")
    group_ids = [row[0] for row in cur.fetchall()]

    messages = []

    if now_hour == 8:
        cur.execute("SELECT name, deadline FROM projects WHERE deadline = %s", (today,))
        for name, deadline in cur.fetchall():
            messages.append(f"🔴 今天截止！\n{name}\n截止日：{deadline.month}/{deadline.day}")

    elif now_hour == 9:
        for days in [3, 5, 7]:
            target = today + timedelta(days=days)
            cur.execute("SELECT name, deadline FROM projects WHERE deadline = %s", (target,))
            for name, deadline in cur.fetchall():
                messages.append(f"⚠️ {name} 距離{deadline.month}/{deadline.day}截止日還有{days}天")

    cur.close()
    conn.close()

    for group_id in group_ids:
        for msg in messages:
            try:
                line_bot_api.push_message(group_id, TextSendMessage(text=msg))
            except Exception as e:
                print(f"Push error: {e}")


scheduler = BackgroundScheduler(timezone="Asia/Taipei")
scheduler.add_job(send_reminders, "cron", hour=8, minute=0)
scheduler.add_job(send_reminders, "cron", hour=9, minute=0)
scheduler.start()

init_db()

if __name__ == "__main__":
    app.run(port=5000)
